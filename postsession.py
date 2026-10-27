#!/usr/bin/env python3
"""postsession.py —— session 结束后的「收尾」：15 秒后把 warden 拉前台，逐个问没做完的任务。

为什么要有这个东西
------------------
一场 session 结束的那一刻是**唯一**还知道「刚才那 50 分钟到底干了什么」的时候。
再往后半小时，人就只记得「好像没干完」了。所以这里做的是一道**强制对账**：

    这场 11:00–11:50 结束了
      → 15 秒后把 warden 拉到前台（keep_session 的 daemon 干这件事）
      → 核对一遍完成状态（Focusmate 的 completedAt + OmniFocus 的 completed）
      → 每一条没做完的任务问你一次，A/B/C/D/E 选一个
      → 全部处理完，这一场才算翻篇

除了「session 结束自动催」，还有一条**主动**入口：``inspect-due``
—— 把「过期还没做完」的任务（不管它挂在哪一场、或者根本没挂）拉出来，
用同一套状态机逐个过一遍。见 :func:`due_candidates`。

五个选项（用户原话见 feature_intents/postsession.md）：

    A 延期      用户自己敲命令把任务转移走；转移成功之后，旧的那条标成「放弃」+
                本地标签「未完成转移」。**OmniFocus 一个字节都不动** —— 交付物是
                「同一条 OmniFocus 任务 + 两条本地记录（旧场次已放弃、新场次在干）」
    B 放弃      本地放弃 **+ OmniFocus 也放弃**（这条真的不要了）
    C 重议      复制一条本地任务（指向同一条 OmniFocus 任务），旧的放弃；
                OmniFocus 不动 —— 活还得干，只是今天这场不算数
    D 缺漏完成  手动输入大概花了多少分钟 → 本地完成 + Focusmate 勾上 + OmniFocus 完成
    E 快捷推后  和 A 一样，但**系统帮你建复制品**并直接塞进下一场

同步方向
--------
只有一条：**warden → OmniFocus**（完成 / 放弃 / 写回截止时间），加上用户手敲的
命令。没有反向同步：OmniFocus 里的改动不会回流成「warden 也改了」。这条边界写在
docs/omnifocus.md，别在这里偷偷加反向。

本模块是**纯逻辑**（不 import rich、不联网）：要碰 Focusmate/OmniFocus 的地方
全部走 ``hooks``，这样测试可以塞假的进去，真跑时由 warden 提供实现。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile

import task_store as ts

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DEFAULT_STATE_FILE = os.path.join(TEMP, "keep_session-postsession.json")

#: session 结束之后等多久把 warden 拉到前台。用户要求「准时 15s」。
DELAY_SEC = 15

#: 多老的场次还值得催收尾（秒）。daemon 半夜起来、机器刚睡醒时不要翻旧账。
LOOKBACK_SEC = 3 * 3600

#: 收尾状态文件的版本
STATE_VERSION = 1

#: 选项表：(键, 短名, 一句话)
OPTIONS = (
    ("a", "延期", "你自己敲命令把任务转移走（复制品 + 旧场次放弃 + 本地标签「未完成转移」）"),
    ("b", "放弃", "这条真不要了：本地放弃 + OmniFocus 也放弃"),
    ("c", "重议", "复制一条（指向同一条 OmniFocus 任务），旧的放弃；OmniFocus 不动"),
    ("d", "缺漏完成", "手动填实际分钟数 → 本地完成 + Focusmate 勾上 + OmniFocus 完成"),
    ("e", "快捷推后", "和延期一样，但复制品由系统建好、直接塞进下一场"),
)

#: 选项的别名（认中文、认数字、认大写）
OPTION_ALIASES = {
    "a": "a", "延期": "a", "推后": "a", "1": "a",
    "b": "b", "放弃": "b", "drop": "b", "2": "b",
    "c": "c", "重议": "c", "duplicate": "c", "3": "c",
    "d": "d", "缺漏完成": "d", "补充": "d", "补录": "d", "完成": "d", "4": "d",
    "e": "e", "快捷推后": "e", "next": "e", "5": "e",
}

#: 跳过 / 退出 / 帮助
SKIP_TOKENS = {"s", "skip", "跳过", "下一个", "n"}
QUIT_TOKENS = {"q", "quit", "exit", "退出", "收尾结束", "done"}
HELP_TOKENS = {"?", "h", "help", "帮助"}


# ---------------------------------------------------------------- 状态文件
#
# daemon（keep_session）写「该收尾了」，warden 写「收完了」。两边共用这一个文件：
#   {"version":1, "pending": {"2026-10-27T11:00": {...}}, "done": {...}}


def state_path(path=None):
    return path or os.environ.get("TI_POSTSESSION_FILE") or DEFAULT_STATE_FILE


def load_state(path=None):
    try:
        with open(state_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = {
        "version": STATE_VERSION,
        "pending": {},
        "done": {},
    }
    for key in ("pending", "done"):
        val = raw.get(key)
        if isinstance(val, dict):
            out[key] = {str(k): v for k, v in val.items() if isinstance(v, dict)}
    return out


def save_state(state, path=None):
    target = state_path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=".tmp",
                               dir=os.path.dirname(target) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return target


def session_key(day, start):
    """一场 session 的身份：``2026-10-27T11:00``（和 schedule.key_of 同构）。"""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return "%sT%s" % (iso, str(start))


def mark_pending(session, now=None, path=None, extra=None):
    """daemon：这场该收尾了（记下来，warden 起来后会看到）。"""
    state = load_state(path)
    key = session.get("key") or session_key(session["date"], session["start"])
    rec = dict(session)
    rec["key"] = key
    rec["queued_at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    if extra:
        rec.update(extra)
    state["pending"][key] = rec
    state["done"].pop(key, None)
    save_state(state, path)
    return rec


def mark_done(session_or_key, now=None, note=None, path=None):
    """warden：这场收完了 —— 从 pending 挪到 done（daemon 就不会再催）。"""
    key = (session_or_key.get("key")
           if isinstance(session_or_key, dict) else str(session_or_key))
    state = load_state(path)
    rec = state["pending"].pop(key, {}) or {}
    rec["key"] = key
    rec["reviewed_at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    if note:
        rec["note"] = note
    state["done"][key] = rec
    # done 只留最近 200 条，别让这个文件无限长
    if len(state["done"]) > 200:
        for old in sorted(state["done"], key=lambda k: state["done"][k].get("reviewed_at") or "")[:-200]:
            state["done"].pop(old, None)
    save_state(state, path)
    return rec


def pending_sessions(path=None):
    """还没收尾的场次，按结束时间从早到晚。"""
    state = load_state(path)
    rows = [rec for rec in state["pending"].values() if isinstance(rec, dict)]
    return sorted(rows, key=lambda r: (r.get("date") or "", r.get("start") or ""))


def is_pending(key, path=None):
    return key in load_state(path)["pending"]


def clear(path=None):
    """把状态文件整个清掉（测试 / 排障用）。"""
    try:
        os.unlink(state_path(path))
    except OSError:
        pass


# ---------------------------------------------------------------- 谁是「刚结束」的
#
# 判据只有一条：结束时刻 + DELAY_SEC ≤ now < 结束时刻 + DELAY_SEC + LOOKBACK_SEC。
# 也就是说「过点 15 秒」就是准点，但机器睡了两小时醒来时仍然会补一次。


def _slot_window(slot, day):
    """(start_dt, end_dt) —— slot 是 schedule.day_slots() 的元素。"""
    start = datetime.datetime(day.year, day.month, day.day,
                             int(slot["hour"]), int(slot["minute"]))
    end = start + datetime.timedelta(minutes=int(slot.get("duration") or 0))
    return start, end


def ended_sessions(slots_by_day, now=None, delay=DELAY_SEC, lookback=LOOKBACK_SEC):
    """``slots_by_day``：``[(date, [slot, ...]), ...]`` → 刚结束该收尾的那些。

    ``slot`` 需要 ``start`` / ``duration``（schedule.day_slots 给的就是这些）。
    """
    now = now or datetime.datetime.now()
    out = []
    for day, slots in slots_by_day:
        for slot in slots or []:
            start_dt, end_dt = _slot_window(slot, day)
            fire_at = end_dt + datetime.timedelta(seconds=delay)
            if now < fire_at:
                continue
            if (now - fire_at).total_seconds() > lookback:
                continue
            out.append({
                "key": session_key(day, slot["start"]),
                "date": day.isoformat(),
                "start": slot["start"],
                "end": datetime.datetime.strftime(end_dt, "%H:%M"),
                "duration": int(slot.get("duration") or 0),
                "started_at": start_dt.isoformat(timespec="seconds"),
                "ended_at": end_dt.isoformat(timespec="seconds"),
                "fire_at": fire_at.isoformat(timespec="seconds"),
            })
    return sorted(out, key=lambda r: (r["date"], r["start"]))


def parse_due(value):
    """``fields.due`` → 本地 naive datetime；认不出（"明天" 这种手写）返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    # 只给了日期：当那天结束（23:59）——「今天到期」不该在 00:00 就判过期。
    # 注意要在 fromisoformat 之前判：Python 3.11 起 "2026-10-27" 也能被它解析成零点。
    if len(text) == 10:
        try:
            return datetime.datetime.combine(datetime.date.fromisoformat(text),
                                             datetime.time(23, 59))
        except ValueError:
            return None
    probe = text[:-1] + "+00:00" if text.upper().endswith("Z") else text
    try:
        got = datetime.datetime.fromisoformat(probe)
    except ValueError:
        return None
    if got.tzinfo is not None:
        got = got.astimezone().replace(tzinfo=None)
    return got


def due_candidates(store, now=None, within_minutes=0, include_no_due=False):
    """「该看一眼」的未完成任务：截止时间已过（或快到了），按截止时间从早到晚。

    ``inspect-due`` 背后的取数逻辑，故意放在这里（纯逻辑、好测）：

    - 只看 ``status == active`` 的（完成/放弃的不催）；
    - ``within_minutes=0``（默认）= 已经过期；给 60 就是「未来 60 分钟内要到的也算」；
    - ``include_no_due=True``（``inspect-due --all``）= **不按截止时间筛**，
      所有未完成的都带上（有截止的按时间排前面，没截止／认不出的排最后）
      —— 「主动 inspect 没完成的任务」那条路上会用得着。
    """
    now = now or datetime.datetime.now()
    horizon = now + datetime.timedelta(minutes=max(0, int(within_minutes or 0)))
    rows = []
    for task in store.list():
        if not store.is_open(task):
            continue
        due = parse_due((task.get("fields") or {}).get("due"))
        if due is None:
            if include_no_due:
                rows.append((datetime.datetime.max, task))
            continue
        if include_no_due or due <= horizon:
            rows.append((due, task))
    rows.sort(key=lambda pair: (pair[0], pair[1].get("id") or 0))
    return [task for _due, task in rows]


def slots_around(entries, now=None, days_back=1, days_fwd=0):
    """data.csv 规则 → ``[(date, slots)]``，覆盖昨天今天（跨午夜那点边角也算）。"""
    import schedule as sch

    now = now or datetime.datetime.now()
    today = now.date()
    out = []
    for offset in range(-int(days_back), int(days_fwd) + 1):
        day = today + datetime.timedelta(days=offset)
        try:
            out.append((day, sch.day_slots(entries, day)))
        except Exception:                      # noqa: BLE001 - 排布坏了不该让 daemon 挂
            out.append((day, []))
    return out


# ---------------------------------------------------------------- 收尾本身


def _fmt_age(seconds):
    """过期时长的白话写法：``3 小时 12 分`` / ``8 分钟``。"""
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return "%d 分钟" % max(1, seconds // 60)
    hours, rest = divmod(seconds, 3600)
    if hours < 24:
        return "%d 小时 %d 分" % (hours, rest // 60) if rest >= 60 else "%d 小时" % hours
    days, rest = divmod(hours, 24)
    return "%d 天 %d 小时" % (days, rest) if rest else "%d 天" % days


def _parse_minutes(text):
    """「35」/「35min」/「大概 35 分钟」→ 35；认不出返回 None。"""
    match = re.search(r"(\d{1,4})", str(text or ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 < value <= 1440 else None


class Review(object):
    """一场 session 的收尾状态机。

    ``store`` 是 ``task_store.TaskStore``；``hooks`` 里全是「要碰外部世界」的东西：

        say(text, kind="info")                  往 Recent 里说一句
        drop_omnifocus(task)                    OmniFocus 放弃这条（返回一句人话）
        complete_omnifocus(task, minutes)       OmniFocus 完成 + 写回截止时间
        complete_focusmate(task)                Focusmate 那边勾完成
        assign_focusmate(task, session)         把任务挂到下一场（Focusmate + 本地）
        next_session(task)                      下一场 session（dict）或 None
        due_of(task)                            这条任务的截止时间（session 终点）
    """

    ASK = "ask"
    TRANSFER = "transfer"
    MINUTES = "minutes"
    DONE = "done"

    def __init__(self, store, session, tasks, hooks=None, now=None, index=0):
        self.store = store
        self.session = dict(session or {})
        self.tasks = [t for t in (tasks or [])]
        self.hooks = hooks or {}
        self.now = now or datetime.datetime.now()
        self.index = int(index or 0)
        self.stage = self.ASK if self.tasks else self.DONE
        self.notes = []
        self.skipped = []
        self.closed = False
        self.pending_task = None      # 处于「等你的转移命令」/「等分钟数」的那条

    # -- 小工具 -------------------------------------------------------
    def say(self, text, kind="info"):
        hook = self.hooks.get("say")
        if hook:
            hook(text, kind)
        self.notes.append((kind, text))

    def _hook(self, name, *args, **kwargs):
        fn = self.hooks.get(name)
        if not fn:
            return None
        return fn(*args, **kwargs)

    def _refresh(self):
        """重新读一遍任务（用户手敲的命令会改库）。"""
        loader = self.hooks.get("reload")
        if loader:
            loader()
        try:
            self.tasks = [self.store.get(t["id"]) for t in self.tasks]
        except ValueError:
            self.tasks = [t for t in self.tasks if self._still_there(t)]

    @staticmethod
    def _still_there(task):
        return bool(task and task.get("id"))

    def title_of(self, task):
        return self.store.focusmate_title(task, max_len=40)

    def current(self):
        """当前要问的那条任务（已经全部处理完就是 None）。"""
        while self.index < len(self.tasks):
            task = self.tasks[self.index]
            if self.store.is_open(task):
                return task
            self.index += 1
        return None

    def total(self):
        return len(self.tasks)

    def done_count(self):
        """已经收掉几条（每次都从库里读，别拿构造时那份快照算账）。"""
        done = 0
        for task in self.tasks:
            try:
                current = self.store.get(task["id"])
            except ValueError:
                done += 1          # 被删掉了也算收掉了
                continue
            if not self.store.is_open(current):
                done += 1
        return done

    def pending(self):
        return not self.closed and self.current() is not None

    # -- 给界面用的文字 -----------------------------------------------
    def due_mode(self):
        """这一轮是不是 ``inspect-due``（不是某一场 session 的收尾）。"""
        return (self.session or {}).get("kind") == "due"

    def headline(self):
        sess = self.session
        if self.due_mode():
            return sess.get("label") or "过期未完成任务"
        return "%s %s–%s 收尾" % (sess.get("date") or "?", sess.get("start") or "?",
                                  sess.get("end") or "?")

    def options(self):
        return list(OPTIONS)

    def stage_hint(self):
        if self.stage == self.TRANSFER:
            return ("把转移命令敲出来（例如 `task %s duplicate -a 3`）；"
                    "敲完这条旧任务会自动标成放弃 + 「未完成转移」。"
                    "敲 `b` 直接放弃、`s` 跳过。" % self._task_label())
        if self.stage == self.MINUTES:
            return "大概花了多少分钟？（填个整数就行，例如 35）"
        return "A 延期 · B 放弃 · C 重议 · D 缺漏完成 · E 快捷推后 · s 跳过 · q 结束收尾"

    def _task_label(self):
        task = self.pending_task or self.current()
        if not task:
            return "?"
        return "%s" % task.get("id")

    def lines(self):
        """面板正文：[(文本, 样式)] —— 由 warden 塞进 rich Panel。"""
        out = []
        task = self.current()
        if task is None:
            out.append(("没有再要看的任务了。" if self.due_mode()
                        else "这场没有待收尾的任务。", "dim"))
            return out
        omni = task.get("omnifocus") or {}
        fm = task.get("focusmate") or {}
        sess = task.get("session") or {}
        out.append(("任务 %s：%s" % (task["id"], self.title_of(task)), "bold yellow"))
        bits = []
        due_text = (task.get("fields") or {}).get("due") or "?"
        if sess.get("start"):
            bits.append("session %s %s（截止 %s）"
                        % (sess.get("date"), sess.get("start"), due_text))
        elif (task.get("fields") or {}).get("due"):
            due = parse_due(task["fields"]["due"])
            overdue = ""
            if due is not None:
                secs = (self.now - due).total_seconds()
                if secs > 0:
                    overdue = "，已过期 %s" % _fmt_age(secs)
            bits.append("截止 %s%s" % (due_text, overdue))
        if fm.get("task_id"):
            bits.append("Focusmate %s…" % str(fm["task_id"])[:8])
        if omni.get("id"):
            bits.append("OmniFocus %s" % omni["id"])
        else:
            bits.append("没关联 OmniFocus")
        if bits:
            out.append(("  " + " · ".join(bits), "dim"))
        if self.stage == self.ASK:
            for key, name, desc in OPTIONS:
                out.append(("  %s  %-6s %s" % (key.upper(), name, desc), ""))
        out.append(("第 %d/%d 条 · %s" % (min(self.index + 1, self.total()),
                                          self.total(), self.stage_hint()), "cyan"))
        return out

    # -- 输入 ---------------------------------------------------------
    def handle(self, line):
        """吃一行输入。返回：

        ``{"action": "handled"}``    收尾自己处理了
        ``{"action": "dispatch"}``   让 warden 照常派发这条命令（延期阶段就是靠它）
        ``{"action": "close"}``      这一场收完了
        """
        raw = str(line or "").strip()
        low = raw.lower()

        if self.stage == self.TRANSFER:
            if not raw:
                self.say(self.stage_hint(), "info")
                return {"action": "handled"}
            if low in QUIT_TOKENS:
                return self._close()
            if low in SKIP_TOKENS:
                task = self.pending_task or self.current()
                self.pending_task = None
                self.stage = self.ASK
                return self._skip(task)
            if low in OPTION_ALIASES and OPTION_ALIASES[low] == "b":
                task = self.pending_task or self.current()
                self.pending_task = None
                self.stage = self.ASK
                return self._drop(task, why="transfer-giveup")
            # 其余都当「用户自己敲的转移命令」：交给 warden 派发，
            # 派发完 warden 会回头调 after_dispatch()。
            return {"action": "dispatch", "line": raw}

        if self.stage == self.MINUTES:
            minutes = _parse_minutes(raw)
            if minutes is None:
                self.say("填个分钟数（例如 35）；或者敲 s 跳过这条。", "warn")
                return {"action": "handled"}
            task = self.pending_task or self.current()
            self.pending_task = None
            self.stage = self.ASK
            return self._complete_manually(task, minutes)

        # stage == ASK
        if not raw:
            self.say(self.stage_hint(), "info")
            return {"action": "handled"}
        if low in QUIT_TOKENS:
            return self._close()
        if low in SKIP_TOKENS:
            return self._skip(self.current())
        if low in HELP_TOKENS:
            for key, name, desc in OPTIONS:
                self.say("%s %s：%s" % (key.upper(), name, desc), "info")
            return {"action": "handled"}
        choice = OPTION_ALIASES.get(low)
        if choice is None:
            self.say("认不出 %r；A 延期 / B 放弃 / C 重议 / D 缺漏完成 / E 快捷推后 / s 跳过 / q 结束"
                     % raw, "warn")
            return {"action": "handled"}
        return self._choose(choice)

    def _choose(self, choice):
        task = self.current()
        if task is None:
            return self._close()
        if choice == "a":
            self.pending_task = task
            self.stage = self.TRANSFER
            self.say("延期：请把任务 %s「%s」转移走 —— %s"
                     % (task["id"], self.title_of(task), self.stage_hint()), "cmd")
            return {"action": "handled"}
        if choice == "b":
            return self._drop(task, why="review-drop")
        if choice == "c":
            return self._reconsider(task)
        if choice == "d":
            self.pending_task = task
            self.stage = self.MINUTES
            self.say("缺漏完成补充：任务 %s「%s」大概花了多少分钟？"
                     % (task["id"], self.title_of(task)), "cmd")
            return {"action": "handled"}
        if choice == "e":
            return self._postpone(task)
        return {"action": "handled"}

    # -- 五个动作 -----------------------------------------------------
    def _advance(self):
        self.stage = self.ASK
        self.pending_task = None
        self.index += 1
        if self.current() is None:
            return self._close()
        return {"action": "handled"}

    def _close(self):
        self.closed = True
        self.stage = self.DONE
        if self.notes:
            self.say("这一场收尾完成（%d 条任务里 %d 条已收）。"
                     % (self.total(), self.done_count()), "ok")
        return {"action": "close"}

    def _skip(self, task):
        if task is None:
            return {"action": "handled"}
        self.skipped.append(task.get("id"))
        self.say("跳过任务 %s（它还是「进行中」，下一场收尾会再问你）。"
                 % task.get("id"), "warn")
        self.index += 1
        if self.current() is None:
            return self._close()
        return {"action": "handled"}

    def _drop(self, task, why="review-drop"):
        """放弃：本地放弃 + OmniFocus 也放弃。"""
        note = self._hook("drop_omnifocus", task)
        self.store.set_status(task["id"], ts.STATUS_DROPPED, reason=why)
        self.say("✗ 任务 %s「%s」已放弃%s"
                 % (task["id"], self.title_of(task), ("；" + note) if note else ""), "ok")
        return self._advance()

    def _reconsider(self, task):
        """重议：复制一条（同一条 OmniFocus 任务），旧的就地放弃。"""
        copy = self.store.duplicate(task["id"])
        self.store.set_status(task["id"], ts.STATUS_DROPPED, reason="reconsider")
        self.say("⤼ 任务 %s「%s」改成 #%s 重议（同一条 OmniFocus %s）；旧的已放弃。"
                 % (task["id"], self.title_of(task), copy["id"],
                    (task.get("omnifocus") or {}).get("id") or "（未关联）"), "ok")
        return self._advance()

    def _postpone(self, task):
        """快捷推后：系统建复制品 + 塞进下一场，旧场次那条放弃 + 打标签。"""
        nxt = self._hook("next_session", task)
        if not nxt:
            self.say("找不到下一场可以放（data.csv 里后面没有 session）；"
                     "用 A 延期自己挑地方，或者 B 放弃。", "warn")
            return {"action": "handled"}
        copy = self.store.duplicate(task["id"])
        note = self._hook("assign_focusmate", copy, nxt)
        self.store.drop(task["id"], reason="postponed", tag=ts.TRANSFER_TAG)
        label = "%s %s" % (nxt.get("date"), nxt.get("start"))
        self.say("⤼ 任务 %s「%s」推到 %s（新任务 #%s%s）；旧的已放弃 + 「%s」。"
                 % (task["id"], self.title_of(task), label, copy["id"],
                    ("；" + note) if note else "", ts.TRANSFER_TAG), "ok")
        return self._advance()

    def _complete_manually(self, task, minutes):
        """缺漏完成补充：本地完成 + Focusmate 勾上 + OmniFocus 完成（写回截止时间）。"""
        due = self._hook("due_of", task)
        self.store.complete(task["id"], minutes=minutes, reason="review-manual")
        fresh = self.store.get(task["id"])
        self.store.archive_local(fresh, reason="review-manual")
        note_fm = self._hook("complete_focusmate", task)
        note_of = self._hook("complete_omnifocus", task, minutes)
        extra = "；".join(x for x in (note_fm, note_of) if x)
        self.say("✓ 任务 %s「%s」补录完成：%d 分钟%s%s"
                 % (task["id"], self.title_of(task), minutes,
                    ("，截止 %s" % due) if due else "",
                    ("；" + extra) if extra else ""), "ok")
        return self._advance()

    # -- 延期阶段：用户敲完命令之后回头看一眼 -------------------------
    def after_dispatch(self, line):
        """warden 派发完用户那条命令之后调用。

        判据是「转移到底发生没发生」，不是「用户敲了什么」：
        出现了一条 ``duplicate_of == 旧任务`` 的新任务，或者旧任务的 session
        被挪到别处了，才算转移。
        """
        if self.stage != self.TRANSFER:
            return {"action": "handled"}
        task = self.pending_task
        if task is None:
            return {"action": "handled"}
        self._refresh()
        try:
            fresh = self.store.get(task["id"])
        except ValueError:
            fresh = None
        moved = bool(fresh) and not self._same_session(fresh, task)
        dup = self._duplicate_of(task)
        if dup or moved:
            how = ("复制成 #%s" % dup["id"]) if dup else "session 挪到 %s %s" % (
                ((fresh or {}).get("session") or {}).get("date"),
                ((fresh or {}).get("session") or {}).get("start"))
            self._finish_transfer(task, how)
            return {"action": "handled"}
        self.say("还没看到转移（%r 既没复制出新任务，也没把任务挪到别的 session）；"
                 "再来一条，或敲 s 跳过 / b 放弃。" % line, "warn")
        return {"action": "handled"}

    @staticmethod
    def _same_session(a, b):
        sa, sb = a.get("session") or {}, b.get("session") or {}
        return (sa.get("date"), sa.get("start")) == (sb.get("date"), sb.get("start"))

    def _duplicate_of(self, task):
        """找这条任务的复制品（用户可能刚 `task N duplicate`）。"""
        finder = self.hooks.get("duplicates_of")
        if finder:
            found = finder(task)
            if found:
                return found[0] if isinstance(found, list) else found
        out = []
        for other in self.store.list():
            if other.get("duplicate_of") == task.get("id") and other.get("id") != task.get("id"):
                out.append(other)
        return out[-1] if out else None

    def _finish_transfer(self, task, how):
        """延期成立：旧任务放弃 + 本地标签「未完成转移」。OmniFocus 不动。"""
        tag = ts.TRANSFER_TAG
        self.store.drop(task["id"], reason="transferred", tag=tag)
        self.say("⤼ 任务 %s「%s」已转移（%s）：旧记录标放弃 + 标签「%s」；"
                 "OmniFocus 那条任务一个字节都没动。"
                 % (task["id"], self.title_of(task), how, tag), "ok")
        self.stage = self.ASK
        self.pending_task = None
        self.index += 1
        if self.current() is None:
            return self._close()
        return {"action": "handled"}


# ---------------------------------------------------------------- 手工入口


def pick(sessions, key=None):
    """从 ``ended_sessions()`` 的结果里挑一场。

    ``key`` 可以是（都取**最晚**的那个匹配）：

        "2026-10-27T11:00"  完整 key（精确）
        "11:00"             只给时刻 —— 今天/最近的那一场
        "2026-10-27"        只给日期 —— 那天最后一场
        不给                最近结束的那一场
    """
    rows = list(sessions or [])
    if not rows:
        return None
    text = str(key or "").strip()
    if not text:
        return rows[-1]
    hits = []
    for row in rows:
        start = str(row.get("start") or "")
        day = str(row.get("date") or "")
        if text == row.get("key") or text == start or text == day:
            hits.append(row)
        elif text.endswith(start) and text.startswith(day):
            hits.append(row)
    return hits[-1] if hits else None


def summary(review):
    """给命令行/日志用的一行摘要。"""
    if review is None:
        return "没有待收尾的场次"
    return "%s（%d 条任务，%d 条已收）" % (review.headline(), review.total(),
                                            review.done_count())
