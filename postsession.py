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
DEFAULT_NOSTART_FILE = os.path.join(TEMP, "keep_session-nostart.json")

#: session 结束之后等多久把 warden 拉到前台。用户要求「准时 15s」。
DELAY_SEC = 15

#: 多老的场次还值得催收尾（秒）。daemon 半夜起来、机器刚睡醒时不要翻旧账。
LOOKBACK_SEC = 3 * 3600

#: 收尾状态文件的版本
STATE_VERSION = 1

#: 「到点没开始」这一问的判定窗口（秒）。
#:
#: 计划 14:00 的那一场，14:00–14:05 之间还算「你刚开始」—— 这 5 分钟就是**预约窗口**
#: （Focusmate 也给你几分钟进场）。14:05 之后 API 里还是**没有这一场**，才算
#: 「没开始」，才开始问人。用户原话：「3:00 的 session，在 3:05 还可以匹配。3:06 不行」。
NOSTART_GRACE_SEC = 5 * 60

#: 判定之后还允许「推迟」多久（秒）。14:05 判定 + 5 分钟 → **14:10 之前**还能说
#: 「推后 30 分钟」；再晚推迟就作废：直接取消推迟，这一场算缺失（用户原话：
#: 「推迟这个动作本身是在 14:10 之后输入的 … 那么取消推迟，这个 session 直接算缺失」）。
NOSTART_POSTPONE_SEC = 5 * 60

#: 多老的场次还值得问（秒）。机器合盖睡醒之后不要翻旧账。
NOSTART_LOOKBACK_SEC = 3 * 3600

#: 「到点没开始」的选项表：(键, 短名, 一句话)
NOSTART_OPTIONS = (
    ("p", "推迟", "p 15 = 这一场往后 15 分钟；p T1 15 = 整个时段往后 15 分钟"),
    ("x", "不去了", "这一场算缺失（daemon 不再重订），挂在这场的任务交给收尾 A-E"),
    ("s", "先不管", "这一轮不问；过了推迟窗口（+5 分钟）就自动算缺失"),
)

#: 这一问的别名（认中文、认大写）
NOSTART_ALIASES = {
    "x": "x", "n": "x", "miss": "x", "missed": "x", "不去": "x", "不去了": "x",
    "缺席": "x", "放弃": "x",
    "s": "s", "skip": "s", "先不管": "s", "等等": "s", "稍后": "s",
    "p": "p", "postpone": "p", "推迟": "p", "推后": "p", "delay": "p",
}

#: 选项表：(键, 短名, 一句话)
OPTIONS = (
    ("a", "延期", "你自己敲命令把任务转移走（复制品 + 旧场次放弃 + 本地标签「未完成转移」）"),
    ("b", "放弃", "这条真不要了：本地放弃 + OmniFocus 也放弃"),
    ("c", "重议", "复制一条（指向同一条 OmniFocus 任务），旧的放弃；OmniFocus 不动"),
    ("d", "缺漏完成", "手动填实际分钟数 → 本地完成 + Focusmate 勾上 + OmniFocus 完成"),
    ("e", "快捷推后", "和延期一样，但复制品由系统建好、直接塞进下一场"),
)

#: **count 达标**的那条才多出来的动作（用户需求：「问是否需要额外的 count 或者直接完成」）。
#:
#:     f        这一场就把它记满 → 直接完成（勾 Focusmate + 写回 OmniFocus）
#:     f 2      再要 2 个 count：上限抬到「已投入 + 2」，任务留着继续干
#:
#: 为什么要有它：session 结束后 count **不会**自动算进任务并完成 —— 达标只是一次
#: 「它够数了」的预报，得你说一句「收工」才算数（用户需求原话）。
COUNT_OPTION = ("f", "count 完成", "这一场就记满 → 直接完成（勾 Focusmate + 写回 OmniFocus）")
COUNT_HINT = ("f 直接完成 · f 2 再要 2 个 count（上限抬到已投入+2，任务留着继续干）"
              " · s 跳过（先不记账完成）")

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


def nostart_path(path=None):
    """「到点没开始」那一问的状态文件（daemon 写「该问了」，warden 写「答过了」）。"""
    return path or os.environ.get("TI_NOSTART_FILE") or DEFAULT_NOSTART_FILE


def load_nostart(path=None):
    """→ ``{"version": 1, "answers": {key: {"action", "at", "note", ...}}}``。

    ``action`` 的取值：
        queued     daemon 发现这一场到点没开始，已经把 warden 叫起来了
        postponed  你说推迟（附 ``target``）
        missed     你说不去了（这一场算缺失）
        deferred   你说先不管（这一轮不再问）
    """
    try:
        with open(nostart_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    answers = raw.get("answers")
    if not isinstance(answers, dict):
        answers = {}
    return {"version": 1, "answers": answers}


def save_nostart(data, path=None):
    p = nostart_path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)
    return p


def mark_nostart(key, action, entry=None, note=None, path=None):
    """记一笔「这一场已经问过/答过了」（幂等：同一个 key 只留最后一次）。"""
    key = str(key or "").strip()
    if not key:
        return None
    data = load_nostart(path)
    rec = dict(data["answers"].get(key) or {})
    rec["action"] = str(action)
    rec["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    if note:
        rec["note"] = str(note)
    for field in ("date", "start", "end", "duration", "period_id", "target", "deadline"):
        if entry and entry.get(field) is not None:
            rec[field] = entry.get(field)
    data["answers"][key] = rec
    save_nostart(data, path)
    return rec


def nostart_answers(path=None):
    """``{key: rec}`` —— 已经问过的场次（daemon 和 warden 都拿它去重）。"""
    return dict(load_nostart(path)["answers"])


def nostart_answered(path=None):
    return set(nostart_answers(path))


def nostart_pending(path=None):
    """daemon 入过队、还没人来答的那些（从早到晚）。"""
    out = [dict(rec, key=key) for key, rec in nostart_answers(path).items()
           if rec.get("action") == "queued"]
    out.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("start") or "")))
    return out


def clear_nostart(path=None):
    """清空（测试用）。"""
    try:
        os.unlink(nostart_path(path))
    except OSError:
        pass


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


def session_minutes(entry):
    """一场 session 有多长（分钟）—— 就是 count 记账的分母（时长 / 25）。

    先认 ``duration``，没有再拿起终点算（daemon 队列里塞进来的老记录可能只有
    ``start`` / ``end``）。都认不出来就是 ``None``（那就这一场不记 count）。
    """
    entry = entry or {}
    got = ts.parse_minutes(entry.get("duration"))
    if got:
        return got
    try:
        start = datetime.datetime.strptime(str(entry.get("start") or ""), "%H:%M")
        end = datetime.datetime.strptime(str(entry.get("end") or ""), "%H:%M")
    except (TypeError, ValueError):
        return None
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0:
        minutes += 24 * 60
    return minutes or None


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


def nostart_entry(day, start_hhmm, duration, now=None, period_id=None, extra=None):
    """一条「到点没开始」的场次记录（daemon 入队 / warden 追问都用这个形状）。

    ``start_hhmm`` 是这一场**计划**的开始时刻；``grace_end`` / ``deadline`` 由它算出来：
    预约窗口在 start+5 分钟结束，推迟窗口再往后 5 分钟（见模块顶部常量）。
    """
    now = now or datetime.datetime.now()
    got = datetime.time(*(int(x) for x in str(start_hhmm).split(":")))
    start_dt = datetime.datetime.combine(day, got)
    dur = int(duration or 0)
    end_dt = start_dt + datetime.timedelta(minutes=dur)
    grace_end = start_dt + datetime.timedelta(seconds=NOSTART_GRACE_SEC)
    deadline = grace_end + datetime.timedelta(seconds=NOSTART_POSTPONE_SEC)
    rec = {
        "kind": "nostart",
        "key": session_key(day, start_hhmm),
        "date": day.isoformat(),
        "start": "%02d:%02d" % (start_dt.hour, start_dt.minute),
        "end": end_dt.strftime("%H:%M"),
        "duration": dur,
        "period_id": str(period_id or ""),
        "started_at": start_dt.isoformat(timespec="seconds"),
        "grace_end": grace_end.isoformat(timespec="seconds"),
        "deadline": deadline.isoformat(timespec="seconds"),
        "queued_at": now.isoformat(timespec="seconds"),
        "late": now > deadline,
    }
    rec.update(extra or {})
    return rec


def nostart_sessions(entries, now=None, actual=None, answered=(), grace=NOSTART_GRACE_SEC,
                     lookback=NOSTART_LOOKBACK_SEC):
    """计划里有、但到点（+5 分钟）之后 API 里**根本没有这一场**的 session。

    ``actual`` 必须是真数据（``[{"start": datetime, "duration": 分钟}]``）。
    **拉不到 API 就别判** —— 拿不到事实时宁可沉默，也不能诬告你没去。

    判据（用户原话）：「refresh 能发现这个 session 没了」—— 计划 14:00 那场，
    14:05 之后 API 里没有同一分钟（``schedule.match_slot`` 的 ``exact``）就算没开始。
    已订上但你没进场的不算（那是另一件事，Focusmate 那边仍然是「有这一场」）。
    """
    import schedule as sch

    now = now or datetime.datetime.now()
    if not actual:
        return []
    if isinstance(entries, tuple) and len(entries) == 2:      # (entries, warnings)
        entries = entries[0]
    done = {str(k) for k in (answered or ())}
    out = []
    for day in (now.date() - datetime.timedelta(days=1), now.date()):
        try:
            slots = sch.day_slots(entries, day)
        except Exception:                                     # noqa: BLE001 - 契约坏了不该炸
            continue
        for slot in slots:
            start_dt, _end_dt = _slot_window(slot, day)
            if now < start_dt + datetime.timedelta(seconds=grace):
                continue
            if (now - start_dt).total_seconds() > lookback:
                continue
            try:
                match = sch.match_slot(actual, start_dt, int(slot.get("duration") or 0))
            except Exception:                                 # noqa: BLE001
                continue
            if match.get("kind") != "none":
                continue
            key = session_key(day, slot["start"])
            if key in done:
                continue
            out.append(nostart_entry(day, slot["start"], slot.get("duration"), now=now))
    return out


def parse_postpone_spec(raw):
    """``"15"`` / ``"+15"`` / ``"T1 15"`` / ``"t1 +15"`` → ``(period_id|None, 分钟)``。

    分钟必须是 15 的倍数、不能是 0（Focusmate 只认 15 分钟整点）。
    """
    toks = [t for t in str(raw or "").replace("，", " ").split() if t]
    if not toks:
        raise ValueError("推迟多少分钟？例如 `p 15` 或 `p T1 15`")
    period_id = None
    if len(toks) == 2:
        if not re.match(r"^[Tt]\d+$", toks[0]):
            raise ValueError("推迟要写成 `p 15`（这一场）或 `p T1 15`（整个时段）；"
                             "你给的是 %r" % raw)
        period_id = toks[0].upper()
        num = toks[1]
    elif len(toks) == 1:
        num = toks[0]
    else:
        raise ValueError("推迟要写成 `p 15`（这一场）或 `p T1 15`（整个时段）；"
                         "你给的是 %r" % raw)
    if not re.match(r"^[+-]?\d+$", num):
        raise ValueError("分钟数要像 15 / +15 / -15；你给的是 %r" % num)
    minutes = int(num)
    if minutes == 0:
        raise ValueError("推迟 0 分钟等于没推迟；给 15 / +30 这样的数。")
    if minutes % 15:
        raise ValueError("Focusmate 只认 15 分钟整点；推迟分钟数要是 15 的倍数，你给的是 %r"
                         % num)
    return period_id, minutes


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


def effective_due(task):
    """这条任务的**截止时刻**：``fields.due`` 优先，没有就退回「所挂 session 的终点」。

    为什么要有这一层：``fields.due`` 只在 ``task assign`` 那一刻才写进去。更早的
    任务（老数据、只从 Focusmate 那边 attach 过来、手改过 JSON 的）根本没有这个
    字段 —— 但它们明明属于某一早已结束的场次，那场结束就是它们的截止时间。
    只认 ``fields.due`` 会让 ``inspect-due`` 把这类任务**整批漏掉**（真实踩过：
    12 条任务、几场都结束了，`inspect-due` 却说「账是干净的」）。
    """
    due = parse_due((task.get("fields") or {}).get("due"))
    if due is not None:
        return due
    return parse_due(ts.session_due(task.get("session") or {}))


def due_candidates(store, now=None, within_minutes=0, include_no_due=False):
    """「该看一眼」的未完成任务：截止时间已过（或快到了），按截止时间从早到晚。

    ``inspect-due`` 背后的取数逻辑，故意放在这里（纯逻辑、好测）：

    - 只看 ``status == active`` 的（完成/放弃的不催）；
    - 截止时间用 :func:`effective_due`：没有 ``fields.due`` 就退回 session 终点
      （老任务没有 due，但「那场结束了」就是截止，不能因此漏掉）；
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
        due = effective_due(task)
        if due is None:
            if include_no_due:
                rows.append((datetime.datetime.max, task))
            continue
        if include_no_due or due <= horizon:
            rows.append((due, task))
    rows.sort(key=lambda pair: (pair[0], pair[1].get("id") or 0))
    return [task for _due, task in rows]


def unreviewed_sessions(store, entries, now=None, delay=DELAY_SEC,
                        lookback=LOOKBACK_SEC, done_keys=(), actual=None):
    """已经结束、还没收尾、而且**确实还有没做完的任务**的场次（从早到晚）。

    为什么 warden 自己也要算一遍：daemon 只负责「准点叫人」，它写的是
    ``temp/keep_session-postsession.json``。可是 daemon 没在跑 / 队列文件被删掉 /
    warden 是后来才打开的 —— 那文件里就什么都没有，而「这一场结束了、还有活没
    做完」是**本地事实**，不能因为没人叫就假装没有（真实踩过：16:45 那场 17:35
    结束，18:15 打开 warden，一个收尾面板都没有，完成状态至今没落过库）。

    ``done_keys`` 是已经收过的场次 key（``postsession`` 状态文件里的 done），
    传进来避免同一次开机重复催。
    """
    now = now or datetime.datetime.now()
    done = {str(k) for k in (done_keys or ())}
    out = []
    for rec in ended_sessions(slots_around(entries, now=now, days_back=1, actual=actual),
                              now=now, delay=delay, lookback=lookback):
        if rec["key"] in done:
            continue
        try:
            day = datetime.date.fromisoformat(rec["date"])
        except ValueError:
            continue
        tasks = store.tasks_for_session(day, rec["start"])
        opened = [t for t in tasks if store.is_open(t)]
        if not opened:
            continue
        rec = dict(rec)
        rec["open_ids"] = [t.get("id") for t in opened]
        rec["task_count"] = len(tasks)
        out.append(rec)
    return out


def slots_from_details(details):
    """API 的**实际** session → ``{date: [slot, ...]}``（slot 带 ``actual: True``）。

    为什么要这个：收尾该问哪几场，答案在**实际订到的场次**里，不在计划里。
    2026-10-28 的教训 —— 计划 16:15 一场 75min，实际是 16:00 25 + 16:30 50：
    按计划问，只会在 17:30 问一次；按实际问，16:25 和 17:20 各问一次，
    任务转移才落得下来。
    """
    by_day = {}
    for rec in details or []:
        if not isinstance(rec, dict):
            continue
        start = rec.get("start")
        if isinstance(start, str):
            try:
                start = datetime.datetime.fromisoformat(start)
            except ValueError:
                continue
        if not isinstance(start, datetime.datetime):
            continue
        dur = rec.get("duration")
        try:
            dur = int(dur) if dur is not None else 0
        except (TypeError, ValueError):
            dur = 0
        by_day.setdefault(start.date(), []).append({
            "start": start.strftime("%H:%M"),
            # hour / minute 是 schedule.day_slots 的形状，_slot_window 要用
            "hour": start.hour,
            "minute": start.minute,
            "duration": dur,
            "actual": True,
            "focusmate_session_id": rec.get("id"),
        })
    for day in by_day:
        by_day[day].sort(key=lambda r: r["start"])
    return by_day


def slots_around(entries, now=None, days_back=1, days_fwd=0, actual=None):
    """``[(date, slots)]``，覆盖昨天今天（跨午夜那点边角也算）。

    ``actual``：``[{"start": datetime, "duration": 分钟}, …]``（API 里的实际场次）。
    给了就用**实际**的替换那一天的计划 —— 「实际订到的才算这场 session」。
    没给（拉不到 API / 那天在窗口外）就退回计划，绝不因为缺数据就不催。
    """
    import schedule as sch

    now = now or datetime.datetime.now()
    today = now.date()
    # 容忍 ``schedule.load_entries()`` 的整个返回值 ``(entries, warnings)`` ——
    # 传错了不该静悄悄返回空（以前就是这样：一场都不催，还看不出来）。
    if isinstance(entries, tuple) and len(entries) == 2:
        entries = entries[0]
    real = slots_from_details(actual) if actual else {}
    out = []
    for offset in range(-int(days_back), int(days_fwd) + 1):
        day = today + datetime.timedelta(days=offset)
        if day in real:
            out.append((day, real[day]))
            continue
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
        delete_focusmate_task(task)             删掉 Focusmate 那条 task（转移之后）
        assign_focusmate(task, session)         把任务挂到下一场（Focusmate + 本地）
        next_session(task)                      下一场 session（dict）或 None
        due_of(task)                            这条任务的截止时间（session 终点）
    """

    ASK = "ask"
    TRANSFER = "transfer"
    MINUTES = "minutes"
    DONE = "done"

    def __init__(self, store, session, tasks, hooks=None, now=None, index=0,
                 counts=None):
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
        # count 的进度账（``{task_id: {...}}``）：谁在这一场涨了几个、够数了没有。
        # 由 warden 在 begin_review 里**记完账**之后递进来 —— 这里只负责问人。
        self.counts = dict(counts or {})

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

    def reload(self):
        """外面把库刷新了（``refresh`` / ``task sync``）：面板上的任务重读一遍。

        公开版本只为让 warden 在 refresh 时能喊一嗓子 —— 不给它碰私有方法的机会。
        重读**不改**当前问到哪一条（index 不动），否则刷个新就把人问回去了。
        """
        self._refresh()
        return self

    def mark_processed(self, task, action):
        """给任务留一笔「这一天的这一场处理过它」。

        任务面板按天固化（今天 = 没做完的 + 今天处理过的；过去那天只剩当时处理过的），
        标记写在 ``task_store`` 那边（``mark_processed``）。这里把「哪一天、哪一场」
        从 ``self.session`` 里取出来；``inspect-due`` 没有场次，就记成今天。
        """
        if not task or not task.get("id"):
            return
        sess = self.session or {}
        start = "" if self.due_mode() else str(sess.get("start") or "")
        day = sess.get("date") or str(sess.get("day") or "")
        if not day:
            day = (self.now or datetime.datetime.now()).date().isoformat()
        try:
            self.store.mark_processed(task["id"], day, start, action, at=self.now)
        except (ValueError, TypeError, AttributeError):
            pass                     # 痕迹只是界面用的；记不上也别挡住收尾

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
        hint = "A 延期 · B 放弃 · C 重议 · D 缺漏完成 · E 快捷推后 · s 跳过 · q 结束收尾"
        if self.at_cap(self.current()):
            hint = "F 直接完成 / f 2 再要 2 个 count · " + hint
        return hint

    # -- count（最高进度 / 这一场涨多少）------------------------------
    def count_state(self, task):
        """``{cap, invested, delta, maxed, credited}``；这条没设 count 就是 ``None``。

        ``invested`` 是**记完这一场的账之后**的数（warden 在 begin_review 里记的），
        ``delta`` 是这一场本该涨多少。两件事分开摆，才看得出「涨了没涨」。
        """
        if not task:
            return None
        cap = ts.task_count(task)
        state = dict(self.counts.get(task.get("id")) or {})
        if state.get("cap") is None:
            state["cap"] = cap
        if state.get("invested") is None:
            state["invested"] = ts.task_count_invested(task)
        if state.get("delta") is None:
            state["delta"] = ts.minutes_to_count(session_minutes(self.session)) or 0
        state["maxed"] = bool(state["cap"] is not None
                              and state["invested"] >= state["cap"])
        return state if state["cap"] is not None else None

    def at_cap(self, task):
        """这条任务的 count 够数了没（够数就该问「额外 count 还是直接完成」）。"""
        state = self.count_state(task)
        return bool(state and state.get("maxed"))

    def count_line(self, state):
        """收尾面板里那行 count：``count 达标：(3/3) 这一场记了 +2 —— f 直接完成…``。"""
        cap = state.get("cap")
        invested = state.get("invested") or 0
        delta = state.get("delta") or 0
        if state.get("maxed"):
            return ("count 达标：(%d/%d)%s —— %s"
                    % (invested, cap, ("，这一场记了 +%d" % delta) if delta else "",
                       COUNT_HINT))
        return ("count：%d/%d%s（这一场记了 %d 个）"
                % (invested, cap, "" if cap else "", delta))

    def label_of(self, task):
        """给用户看的任务编号：warden 那边给的是**面板序号**（``#3``）。

        收尾面板和别处的编号必须是一个东西 —— 面板上写着 #3，这里就不能说「任务 15」。
        warden 通过 ``label`` 钩子把 :meth:`Warden.task_label` 递进来；没有钩子
        （纯逻辑测试里）就老实写内部 id。
        """
        hook = self.hooks.get("label")
        if hook is not None:
            try:
                text = hook(task)
                if text:
                    return text
            except Exception:                     # noqa: BLE001 - 编号算不出来别炸收尾
                pass
        return "%s" % ((task or {}).get("id") or "?")

    def _task_label(self):
        task = self.pending_task or self.current()
        if not task:
            return "?"
        return self.label_of(task)

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
        out.append(("任务 %s：%s" % (self.label_of(task), self.title_of(task)),
                    "bold yellow"))
        bits = []
        due = effective_due(task)
        due_text = ((task.get("fields") or {}).get("due")
                    or (due.strftime("%Y-%m-%dT%H:%M") if due else "?"))
        overdue = ""
        if due is not None:
            secs = (self.now - due).total_seconds()
            if secs > 0:
                overdue = "，已过期 %s" % _fmt_age(secs)
        if sess.get("start"):
            bits.append("session %s %s（截止 %s%s）"
                        % (sess.get("date"), sess.get("start"), due_text, overdue))
        elif due is not None:
            bits.append("截止 %s%s" % (due_text, overdue))
        if fm.get("task_id"):
            bits.append("Focusmate %s…" % str(fm["task_id"])[:8])
        if omni.get("id"):
            bits.append("OmniFocus %s" % omni["id"])
        else:
            bits.append("没关联 OmniFocus")
        if bits:
            out.append(("  " + " · ".join(bits), "dim"))
        state = self.count_state(task)
        if state:
            out.append(("  " + self.count_line(state),
                        "bold yellow" if state.get("maxed") else "dim"))
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
        count_choice = self._parse_count_choice(raw)
        if count_choice is not None:
            task = self.current()
            if not self.at_cap(task):
                self.say("`f` 只给「这一场就够数」的那条任务用（它的 count 还没到上限）；"
                         "别的照旧 A/B/C/D/E。", "warn")
                return {"action": "handled"}
            if count_choice <= 0:
                return self._complete_by_count(task)
            return self._extend_count(task, count_choice)
        choice = OPTION_ALIASES.get(low)
        if choice is None:
            self.say("认不出 %r；A 延期 / B 放弃 / C 重议 / D 缺漏完成 / E 快捷推后 / s 跳过 / q 结束"
                     % raw, "warn")
            return {"action": "handled"}
        return self._choose(choice)

    @staticmethod
    def _parse_count_choice(raw):
        """``f`` / ``f 2`` / ``+2`` / ``count 2`` → ``0``（直接完成）或正整数（额外 count）。

        认不出来就是 ``None``（交给 A/B/C/D/E 那套去解析）。
        """
        text = str(raw or "").strip().lower()
        if not text:
            return None
        head, _sep, tail = text.partition(" ")
        tail = tail.strip()
        if head in ("f", "count", "记count", "counts"):
            if not tail:
                return 0
            got = ts.parse_count(tail)
            return got if got and got > 0 else 0
        if head.startswith("+"):
            got = ts.parse_count(head)
            return got if got and got > 0 else None
        return None

    def _complete_by_count(self, task):
        """count 够数 → 直接完成（和缺漏完成同一条路，只是不用再填分钟数）。"""
        due = self._hook("due_of", task)
        self.mark_processed(task, "count-complete")
        self.store.complete(task["id"], reason="review-count")
        fresh = self.store.get(task["id"])
        self.store.archive_local(fresh, reason="review-count")
        note_fm = self._hook("complete_focusmate", task)
        note_of = self._hook("complete_omnifocus", task, None)
        extra = "；".join(x for x in (note_fm, note_of) if x)
        state = self.count_state(task) or {}
        self.say("✓ 任务 %s「%s」count 到位（%d/%d）→ 直接完成%s%s"
                 % (self.label_of(task), self.title_of(task),
                    state.get("invested") or 0, state.get("cap") or 0,
                    ("，截止 %s" % due) if due else "",
                    ("；" + extra) if extra else ""), "ok")
        return self._advance()

    def _extend_count(self, task, extra):
        """「额外 count」= **抬高上限**：count = 已投入 + extra，任务留着继续干。

        这是用户选的那种语义（另一种「加已投入」会在超过上限时立刻收工，等于没得选）。
        """
        state = self.count_state(task) or {}
        invested = state.get("invested")
        if invested is None:
            invested = ts.task_count_invested(task)
        cap = int(invested) + int(extra)
        setter = self.hooks.get("set_count")
        if setter:
            setter(task, cap)
        else:
            self.store.set_count(task["id"], cap)
        self.mark_processed(task, "count-extend")
        self.counts[task.get("id")] = dict(state, cap=cap, invested=invested,
                                           maxed=False)
        self.say("➕ 任务 %s「%s」的 count 上限抬到 %d（已投入 %d，还剩 %d 个）—— 继续干。"
                 % (self.label_of(task), self.title_of(task), cap, invested,
                    cap - int(invested)), "ok")
        return self._advance()

    def _choose(self, choice):
        task = self.current()
        if task is None:
            return self._close()
        if choice == "a":
            self.pending_task = task
            self.stage = self.TRANSFER
            self.say("延期：请把任务 %s「%s」转移走 —— %s"
                     % (self.label_of(task), self.title_of(task), self.stage_hint()),
                     "cmd")
            return {"action": "handled"}
        if choice == "b":
            return self._drop(task, why="review-drop")
        if choice == "c":
            return self._reconsider(task)
        if choice == "d":
            self.pending_task = task
            self.stage = self.MINUTES
            self.say("缺漏完成补充：任务 %s「%s」大概花了多少分钟？"
                     % (self.label_of(task), self.title_of(task)), "cmd")
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
        # 跳过也算「这一场处理过它」：过去那天的面板上要看得见「问过、你说先跳过」。
        self.mark_processed(task, "skip")
        self.say("跳过任务 %s（它还是「进行中」，下一场收尾会再问你）。"
                 % self.label_of(task), "warn")
        self.index += 1
        if self.current() is None:
            return self._close()
        return {"action": "handled"}

    def _drop(self, task, why="review-drop"):
        """放弃：本地放弃 + OmniFocus 也放弃。"""
        note = self._hook("drop_omnifocus", task)
        self.mark_processed(task, "drop")
        self.store.set_status(task["id"], ts.STATUS_DROPPED, reason=why)
        self.say("✗ 任务 %s「%s」已放弃%s"
                 % (self.label_of(task), self.title_of(task),
                    ("；" + note) if note else ""), "ok")
        return self._advance()

    def _reconsider(self, task):
        """重议：复制一条（同一条 OmniFocus 任务），旧的就地放弃。"""
        copy = self.store.duplicate(task["id"])
        self.mark_processed(task, "reconsider")
        self.store.set_status(task["id"], ts.STATUS_DROPPED, reason="reconsider")
        self.say("⤼ 任务 %s「%s」改成 #%s 重议（同一条 OmniFocus %s）；旧的已放弃。"
                 % (self.label_of(task), self.title_of(task), copy["id"],
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
        self.mark_processed(task, "postpone")
        self.store.drop(task["id"], reason="postponed", tag=ts.TRANSFER_TAG)
        label = "%s %s" % (nxt.get("date"), nxt.get("start"))
        self.say("⤼ 任务 %s「%s」推到 %s（新任务 #%s%s）；旧的已放弃 + 「%s」。"
                 % (self.label_of(task), self.title_of(task), label, copy["id"],
                    ("；" + note) if note else "", ts.TRANSFER_TAG), "ok")
        return self._advance()

    def _complete_manually(self, task, minutes):
        """缺漏完成补充：本地完成 + Focusmate 勾上 + OmniFocus 完成（写回截止时间）。"""
        due = self._hook("due_of", task)
        self.mark_processed(task, "backfill")
        self.store.complete(task["id"], minutes=minutes, reason="review-manual")
        fresh = self.store.get(task["id"])
        self.store.archive_local(fresh, reason="review-manual")
        note_fm = self._hook("complete_focusmate", task)
        note_of = self._hook("complete_omnifocus", task, minutes)
        extra = "；".join(x for x in (note_fm, note_of) if x)
        self.say("✓ 任务 %s「%s」补录完成：%d 分钟%s%s"
                 % (self.label_of(task), self.title_of(task), minutes,
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
        """延期成立：旧任务放弃 + 本地标签「未完成转移」+ 删掉 Focusmate 那条 task。

        Focusmate 那条 task 必须删（用户点名的「任务转移之后 focusmate 任务删除」）：
        它挂在**旧那一场**上，任务已经转走了，它既不会完成、也不该继续躺在
        Task Manager 里。删失败只报告，不挡住转移本身（账本先落下来）。
        OmniFocus 那边照旧一个字节都不动。
        """
        tag = ts.TRANSFER_TAG
        self.mark_processed(task, "transfer")
        note = self._hook("delete_focusmate_task", task)
        self.store.drop(task["id"], reason="transferred", tag=tag)
        self.say("⤼ 任务 %s「%s」已转移（%s）：旧记录标放弃 + 标签「%s」；"
                 "OmniFocus 那条任务一个字节都没动。"
                 % (self.label_of(task), self.title_of(task), how, tag), "ok")
        if note:
            self.say("   %s" % note, "ok" if "已删除" in str(note) else "info")
        self.stage = self.ASK
        self.pending_task = None
        self.index += 1
        if self.current() is None:
            return self._close()
        return {"action": "handled"}


# ---------------------------------------------------------------- 手工入口


class NostartReview(object):
    """「这一场到点没开始」那一问的状态机：``p 推迟`` / ``x 不去了`` / ``s 先不管``。

    为什么是**问**而不是直接判「没完成」（用户原话）：到点没开始有好几种可能 ——
    你刚要开始只是晚了、想整场往后挪、或者今天确实不去了。直接记成「没完成」
    是最偷懒也最伤人的做法，所以走和任务收尾一样的路：把事实（这场 API 里没有）
    摆出来，让你选。

    ``hooks`` 里全是「要碰外面世界」的东西（本模块保持纯逻辑、不联网）：

        say(text, kind="info")                  往 Recent 里说一句
        postpone(entry, period_id, minutes)     真去挪（单场 / 整段）；返回 {"target": "14:30"}
        mark_missed(entry, note)                这一场算缺失（记 state + verdict，返回一句人话）
        open_review(entry)                      把挂在这场的任务交给收尾（A 延期 / B 放弃 / …）
    """

    def __init__(self, entry, hooks=None, now=None):
        self.entry = dict(entry or {})
        self.hooks = hooks or {}
        self.now = now or datetime.datetime.now()
        self.closed = False
        self.notes = []
        # 和 Review 对齐的字段名：warden 的画帧缓存/面板代码两边通用
        self.pending_task = None
        self.index = 0

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

    # -- 时间 ---------------------------------------------------------
    def deadline(self):
        try:
            return datetime.datetime.fromisoformat(str(self.entry.get("deadline")))
        except (TypeError, ValueError):
            return None

    def late(self, now=None):
        """推迟窗口过了没？过了就**不能再推迟**：取消推迟，这一场算缺失。"""
        got = self.deadline()
        if got is None:
            return False
        return (now or self.now) > got

    # -- 给界面用的文字 -----------------------------------------------
    def solo(self):
        return "%s %s–%s" % (self.entry.get("date") or "?", self.entry.get("start") or "?",
                             self.entry.get("end") or "?")

    def headline(self):
        return "%s 到点没开始" % self.solo()

    def options(self):
        return list(NOSTART_OPTIONS)

    def stage_hint(self):
        got = self.deadline()
        tail = ("推迟窗口已过（%s 之后就不能再推了）—— 只能 x 不去了 / s 先不管"
                % got.strftime("%H:%M")) if (got and self.late()) else (
                "%s 之前还能推迟 · p 推迟 / x 不去了 / s 先不管"
                % got.strftime("%H:%M")) if got else "p 推迟 / x 不去了 / s 先不管"
        return tail

    def lines(self):
        """面板正文：[(文本, 样式)]。"""
        out = [("🚪 %s" % self.headline(), "bold yellow")]
        bits = ["计划 %s（%d 分钟）" % (self.entry.get("start") or "?",
                                     int(self.entry.get("duration") or 0)),
                "API 里没有这一场"]
        if self.entry.get("period_id"):
            bits.append("时段 %s" % self.entry["period_id"])
        out.append(("  " + " · ".join(bits), "dim"))
        for key, name, desc in NOSTART_OPTIONS:
            out.append(("  %s  %-4s %s" % (key.upper(), name, desc), ""))
        out.append((self.stage_hint(), "cyan"))
        return out

    def pending(self):
        return not self.closed

    # -- 输入 ---------------------------------------------------------
    def handle(self, line):
        """吃一行输入：``{"action": "handled"|"close"|"dispatch"}``。"""
        raw = str(line or "").strip()
        if not raw:
            self.say(self.stage_hint(), "info")
            return {"action": "handled"}
        head, _sep, tail = raw.partition(" ")
        low = head.strip().lower()
        if low in HELP_TOKENS:
            for key, name, desc in NOSTART_OPTIONS:
                self.say("%s %s：%s" % (key.upper(), name, desc), "info")
            return {"action": "handled"}
        choice = NOSTART_ALIASES.get(low)
        if choice == "x":
            return self._missed(note="user")
        if choice == "s":
            return self._defer()
        if choice == "p":
            spec = tail.strip()
            if not spec:
                self.say("推迟多少？`p 15` = 这一场往后 15 分钟；"
                         "`p T1 15` = 整个时段往后 15 分钟。", "warn")
                return {"action": "handled"}
            try:
                period_id, minutes = parse_postpone_spec(spec)
            except ValueError as e:
                self.say(str(e), "warn")
                return {"action": "handled"}
            return self._postpone(period_id, minutes)
        # 别的手敲命令（`relocate T1 15` / `session ...`）照常派发
        if low in ("relocate", "session", "period", "task", "mute", "title", "cancel"):
            return {"action": "dispatch", "line": raw}
        self.say("认不出 %r；p 推迟 / x 不去了 / s 先不管（? 看全部）" % raw, "warn")
        return {"action": "handled"}

    # -- 三个动作 -----------------------------------------------------
    def _postpone(self, period_id, minutes):
        if self.late():
            got = self.deadline()
            self.say("推迟窗口 %s 已经过了 —— 推迟作废，这一场按「没去」算。"
                     % (got.strftime("%H:%M") if got else "?"), "warn")
            return self._missed(note="late-postpone")
        detail = self._hook("postpone", self.entry, period_id, minutes)
        target = (detail or {}).get("target") if isinstance(detail, dict) else None
        what = "整个时段 %s" % period_id if period_id else "这一场"
        self.say("↦ %s 推迟 %+d 分钟%s" % (what, int(minutes),
                                          ("：%s → %s" % (self.entry.get("start"), target))
                                          if target else ""), "ok")
        entry = dict(self.entry)
        if target:
            entry["target"] = target
        return self._finish("postponed", note=target or "", entry=entry)

    def _missed(self, note=None):
        detail = self._hook("mark_missed", self.entry, note)
        self.say("✗ %s 这场算缺失%s（挂在这场的任务交给收尾）"
                 % (self.solo(), ("：" + detail) if detail else ""), "warn")
        # 先落账再开收尾：开收尾会把这个提示面板收掉（begin_review → close_nostart），
        # 顺序反了就会把「missed」写成「deferred」。
        result = self._finish("missed", note=note or "")
        self._hook("open_review", self.entry)
        return result

    def _defer(self):
        self.say("⏭ %s 先不管 —— 过了推迟窗口还没开始，就自动算缺失。" % self.solo(), "info")
        return self._finish("deferred", note="user")

    def _finish(self, action, note="", entry=None):
        self.closed = True
        try:
            mark_nostart(self.entry.get("key"), action, entry=entry or self.entry,
                         note=note)
        except OSError as e:                                   # noqa: BLE001
            self.say("记不下这一问的结果（%s）；不影响已经做的动作。" % e, "warn")
        return {"action": "close", "done": action, "note": note}


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
