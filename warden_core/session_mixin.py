"""warden_core.session_mixin —— 宣言闸门、session/cancel/mute/title/relocate、period 排布与静音、refresh/sync/daemon。"""
import copy
import datetime
import re
import threading
import time

from .bridge import BridgeError
from .common import (
    DECLARATION,
    Panel,
    SRC_FOCUSMATE,
    SRC_KEEP,
    Table,
    Text,
    beep,
    box,
    cfg,
    check_declaration,
    get_warden_global,
    ks,
    parse_relocate_spec,
    parse_session_spec,
    run_keep_session,
    sch,
    sp,
    st,
    th,
    ts,
)
from .hints import forget_mute_cache


class SessionMixin(object):
    # -- 找行

    def pick(self, token):
        try:
            idx = int(token)
        except ValueError:
            raise ValueError("ID 得是数字，比如 3；你给的是 %r" % token)
        if idx < 1 or idx > len(self.rows):
            if not self.rows:
                raise ValueError("表格还是空的，先跑 refresh")
            raise ValueError("没有 ID %d（当前 1..%d）" % (idx, len(self.rows)))
        return idx, self.rows[idx - 1]

    # -- 宣言闸门（The Walk of Shame）

    def require_declaration(self, reason):
        """一字不差敲完宣言才放行。敲错就鸣笛、继续问（不设次数上限）。"""
        body = Text()
        body.append("要执行「%s」，你得先把下面这段话一字不差地敲一遍。\n\n" % reason,
                    style="bold red")
        body.append(DECLARATION + "\n\n", style="bold")
        body.append("共 %d 字符（含标点）。少一个标点、错一个引号，都继续锁死并鸣笛。\n"
                    "Ctrl+C 可以放弃 —— 那就什么都不做。"
                    % len(DECLARATION), style="dim")
        self.emit(Panel(body, title="[bold red]⛓  The Walk of Shame[/]",
                                 border_style="bold red", box=box.HEAVY))
        tries = 0
        while True:
            try:
                typed = input("  宣言> ")
            except (EOFError, KeyboardInterrupt):
                self.emit("\n  [dim]放弃了。[/]")
                return False
            ok, why = check_declaration(typed)
            if ok:
                self.say("✅ 认罪完成（第 %d 次敲对）。" % (tries + 1), "ok")
                return True
            tries += 1
            beep()
            self.emit("[bold red]✗ 不通过：%s[/]" % why)
            self.say("宣言不通过（第 %d 次）：%s" % (tries, why), "error")

    def confess(self, sins):
        """认罪收尾：把每一笔罪的 slot 判成「已放弃」，并且让它真的消失。

        1. 标 abandoned —— daemon 从此不再重订这一场（宣言的牙齿）
        2. 如果它已经被 daemon 重订回来了 → 再取消掉（否则"放弃"是假的）
        3. 罪从待认罪挪进 archive（.jsonl 只增不删）
        """
        keys = [rec.get("key") for rec in sins if rec.get("key")]
        for key in keys:
            ks.mark_abandoned(key, why="walk-of-shame", note="TUI 里敲完宣言后放弃")
        self.say("已判 %d 场为「已放弃」：daemon 不会再重订。" % len(keys), "ok")

        # 已经被 daemon 重订回来吗？查快照 + 取消都在后台队列里做：
        # 浏览器冷启动时输入线程不能跟着一起冻住，进度条会写清楚在做哪一场。
        action = self.net_action(
            "session.confess_cancel",
            "认罪后取消已重订的场次（%d 场）" % len(keys),
            {"keys": keys}, source=SRC_FOCUSMATE)

        def done(payload):
            payload = payload or {}
            if payload.get("cancelled"):
                self.say("  ✅ 后台复查：%d 场被重订回来的 session 已真的取消。"
                         % payload["cancelled"], "ok")
            if payload.get("missing"):
                self.say("  %d 场 API 里还没有（daemon 还没重订回来）。"
                         % payload["missing"], "info")
            if payload.get("past"):
                self.say("  %d 场已经过去：只判放弃，没动 API。" % payload["past"], "info")

        def failed(error):
            self.had_error = True
            self.say("  ❌ 复查/取消失败：%s（判决已落，daemon 不会重订；"
                     "失败账可用 re-action 重放）" % error, "error")

        job = self.jobs.submit(action, on_ok=done, on_fail=failed)
        if self.jobs.sync:
            # 同步模式（测试 / --exec）：submit 已经跑完，让错误照旧表现为失败。
            if job.outcome is None or not job.outcome.ok:
                self.had_error = True
        else:
            self.say("  正在后台复查并取消已被重订的场次（进度看面板）。", "cmd")
        n = ks.clear_sins(keys)
        self.say("罪已归档 %d 笔（temp/warden-sins.jsonl，只增不删）。" % n, "ok")

    # -- 命令实现

    def cmd_mute(self, args):
        if not args:
            raise ValueError("用法：mute N [on|off]")
        idx, row = self.pick(args[0])
        if len(args) > 1:
            want = args[1].lower()
            if want in ("on", "true", "1", "开"):
                want = True
            elif want in ("off", "false", "0", "关"):
                want = False
            else:
                raise ValueError("第三个词只能是 on 或 off；你给的是 %r" % args[1])
        else:
            want = not bool(row.get("quiet"))
        if row.get("id") is None:
            raise ValueError("这场是从 state.json 快照来的，没有 session id，改不了。先 refresh 成功一次。")
        verb = "开" if want else "关"
        self.say("#%d %s–%s quiet %s（乐观：表格先改，Focusmate 在后台）…"
                 % (idx, row["startLabel"], row.get("endLabel") or "?", verb), "cmd")
        before_row = dict(row)
        self._patch_api_row(row["id"], quiet=want)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.mute",
                                 "quiet %s %s" % (row["startLabel"], "ON" if want else "off"),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"], "quiet": bool(want)})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d quiet %s" % (idx, verb), before_row, error))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d quiet = %s（API 复核一致）"
                         % (idx, "ON" if want else "off"), "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d quiet = %s（已记上；Focusmate 复核回来我再说话）"
                 % (idx, "ON" if want else "off"), "ok")
        return True

    def cmd_title(self, args):
        if len(args) < 2:
            raise ValueError('用法：title N "文本"（清空用 title N ""）')
        idx, row = self.pick(args[0])
        title = " ".join(args[1:]).strip()
        if row.get("id") is None:
            raise ValueError("这场是从 state.json 快照来的，没有 session id，改不了。先 refresh 成功一次。")
        if len(title) > 100:
            raise ValueError("标题最长 100 字符，现在 %d" % len(title))
        self.say("#%d 改标题为 %r（乐观：表格先改，Focusmate 在后台）…" % (idx, title), "cmd")
        before_row = dict(row)
        self._patch_api_row(row["id"], title=title)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.title",
                                 "写标题 %s %r" % (row["startLabel"], title[:20]),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"], "title": title})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d 写标题" % idx, before_row, error))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d 标题已写入（API 复核一致）" % idx, "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d 标题已记上（Focusmate 复核回来我再说话）" % idx, "ok")
        return True

    def cmd_cancel(self, args):
        """取消。默认只取消**这一个时间点的这一场**（判 abandoned，daemon 不再重订），
        日历里的规则不动；加 ``--permanent`` 才把日历里这个 session 永久删掉。
        摩擦层由**这一行**在哪个区决定：草稿区零阻力，执行区敲宣言。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            raise ValueError("用法：cancel N [--permanent]")
        permanent = False
        kept = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "永久"):
                permanent = True
            else:
                kept.append(tok)
        if not kept:
            raise ValueError("用法：cancel N [--permanent]")
        idx, row = self.pick(kept[0])
        zone = row.get("zone")

        if zone == "past":
            raise ValueError("#%d 已经开始（%s）—— 过去的场次不取消也不补订。"
                             % (idx, row["startLabel"]))

        key = sch.key_of(self.sel_date, row["startLabel"])
        if permanent:
            return self._cancel_permanent(idx, row, key)
        return self._cancel_single(idx, row, key)

    def _cancel_declaration_gate(self, idx, row):
        """执行区取消前要敲宣言；草稿区不用。返回是否放行。"""
        if row.get("zone") == "draft":
            return True
        matched = row.get("matched")
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True)
        info.add_column()
        info.add_row("目标", "#%d  %s–%s（%s）" % (idx, row["startLabel"],
                                                row.get("endLabel") or "?", row["phase"]))
        info.add_row("契约", row.get("contract") or "—（计划外，手动订的）")
        info.add_row("标题", row.get("title") or "[dim]（无）[/]")
        info.add_row("伙伴", "[cyan]已匹配 —— 对方会被放鸽子[/]" if matched else "[dim]还没匹配[/]")
        info.add_row("session", (row.get("id") or "—")[:8] + ("…" if row.get("id") else ""))
        self.emit(Panel(info, title="[bold red]⚠  执行区：取消要过宣言闸门[/]",
                                 border_style="bold red", box=box.HEAVY))
        if not self.require_declaration("取消 #%d（%s %s）"
                                        % (idx, self.sel_date.isoformat(), row["startLabel"])):
            self.say("没认罪，什么都没动。", "warn")
            return False
        return True

    def _cancel_single(self, idx, row, key):
        """默认 cancel：只弃这一个时间点的 session，日历规则保留。"""
        if not self._cancel_declaration_gate(idx, row):
            return False
        ks.mark_abandoned(key, why="walk-of-shame",
                          note="TUI 里放弃这一场（#%d %s）" % (idx, row["startLabel"]))
        if row.get("planned"):
            self.say("已判 %s 为「已放弃」：daemon 不会再重订这个时间点的这一场"
                     "（日历规则还在；`cancel %d --permanent` 才从日历删掉）。"
                     % (key, idx), "ok")
        else:
            self.say("已判 %s 为「已放弃」：这一场不是日历规则（计划外），"
                     "daemon 本来也不会重订。" % key, "ok")
        if row.get("id") is None:
            self.say("它本来就没订上（契约缺的那一场），不用动 API。", "info")
            self.refresh(quiet=True)
            return True
        return self._cancel_booked_session(idx, row)

    def _cancel_permanent(self, idx, row, key):
        """``cancel N --permanent``：日历里这个 session 永久不要 + 取消今天这场。"""
        if not self._cancel_declaration_gate(idx, row):
            return False
        ks.mark_abandoned(key, why="permanent",
                          note="TUI 里永久取消这一场（#%d %s）" % (idx, row["startLabel"]))
        self.say("已判 %s 为「永久不要」：daemon 不会再重订这一场。" % key, "ok")
        self._remove_contract_row_for_cancel(idx, row)
        if row.get("id") is None:
            self.say("它本来就没订上，不用动 API。", "info")
            self.refresh(quiet=True)
            return True
        return self._cancel_booked_session(idx, row)

    def _remove_contract_row_for_cancel(self, idx, row):
        """把日历里这一行（data.csv）删掉；临时挪动的行会连挪动记录一起清。"""
        relocation = row.get("relocation")
        from_hhmm = (row.get("relocated_from")
                     or (relocation or {}).get("from")
                     or row.get("startLabel"))
        if row.get("lineno"):
            if sch.fingerprint() != self.csv_fp:
                raise ValueError("data.csv 刚被别处改过（指纹不一致）。先 refresh 再动手，免得删错行。")
            raw = sch.remove_row(None, row["lineno"], expect=row.get("raw"))
            self.say("✅ 已从 data.csv 删掉第 %d 行：%s" % (row["lineno"], raw.strip()), "ok")
            self.say("   日历里的这个 session 永久不要了（以后同星期几也不会再有）。", "warn")
        elif not relocation:
            self.say("· 这一场不是日历里的规则（计划外 session），只取消这一场本身。", "info")
        if relocation:
            try:
                sch.remove_relocation(self.sel_date, from_hhmm)
                self.say("✅ 已撤销 #%d 的临时挪动（原时间 %s）。" % (idx, from_hhmm), "ok")
            except Exception as e:                     # noqa: BLE001 - 挪动记录坏了也别挡取消
                self.say("⚠ 清理临时挪动记录失败：%s" % e, "warn")

    def _cancel_booked_session(self, idx, row):
        """取消一场已经订上的 session（乐观先拿掉，网络丢后台）。"""
        self.say("#%d 取消中…（%s；乐观：表格先拿掉，Focusmate 在后台）"
                 % (idx, row["startLabel"]), "cmd")
        before_row = dict(row)
        removed = self._forget_api_row(row["id"])
        removed_at = removed[0] if removed else None
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.cancel",
                                 "取消 %s %s" % (self.sel_date.isoformat(), row["startLabel"]),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"]})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d 取消 %s" % (idx, row["startLabel"]), before_row, error,
            index=removed_at))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d %s 已取消（API 复核：该时段已消失）"
                         % (idx, row["startLabel"]), "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d %s 已取消（Focusmate 复核回来我再说话）"
                 % (idx, row["startLabel"]), "ok")
        self.refresh(quiet=True)
        return True

    # -- 临时挪动（单场 / 整段）----------------------------------------

    def _period_rows(self, day, period_id):
        """这一天属于某个时段的场次（形状和表格里的行一样，能直接拿去挪）。

        以**契约**为准（``schedule.day_slots``，含已经落过的临时挪动），再从表格 /
        API 快照里借出那一半（id / startMs / zone）—— 这样 `relocate T1 15` 既不会
        漏掉刚改过的场次，也不会漏掉「订上了但表格还没重建」的那些。
        """
        by_start = {}
        for row in getattr(self, "rows", []) or []:
            if row.get("date") == day.isoformat():
                by_start.setdefault(row.get("startLabel"), row)
        api_by_start = {}
        for r in getattr(self, "api_rows", []) or []:
            api_by_start.setdefault(r.get("startLabel"), r)
        periods = self.periods_for_day(day)
        now = datetime.datetime.now()
        out = []
        for slot in sch.day_slots(self.entries, day):
            period = sp.period_for_session(periods, slot["start"])
            if not period or period.get("id") != period_id:
                continue
            row = by_start.get(slot["start"])
            if row is None:
                start_dt = sch.slot_datetime(slot, day)
                dur = int(slot["duration"])
                row = {
                    "startMs": int(start_dt.timestamp() * 1000),
                    "endMs": int((start_dt + datetime.timedelta(minutes=dur)
                                  ).timestamp() * 1000),
                    "startLabel": slot["start"],
                    "durationMin": dur,
                    "id": (api_by_start.get(slot["start"]) or {}).get("id"),
                    "zone": self.effective_zone(sch.zone_name(start_dt, now), day),
                    "date": day.isoformat(),
                    "planned": True,
                }
            row = dict(row)
            if slot.get("relocation"):
                row["relocation"] = dict(slot["relocation"])
                row["relocated_from"] = slot.get("relocated_from")
            row["period_id"] = period_id
            out.append(row)
        out.sort(key=lambda r: r.get("startMs") or 0)
        return out

    def _relocate_moves(self, day, rows, delta, allow_past=False):
        """**纯计算**：这批行挪 delta 分钟之后是什么样 + 撞车检查（不改任何文件）。

        返回 ``[{"row","from","cur","to","duration","offset"}]``；有任何问题抛
        ValueError，调用方此时还没有副作用。
        """
        if not rows:
            raise ValueError("没有要挪的场次。")
        moves = []
        for row in rows:
            if not row.get("planned"):
                raise ValueError("计划外 session（data.csv 里没有对应契约），relocate 改不了它。")
            if not allow_past and (row.get("zone") == "past"
                                   or row.get("phase") == "已结束"):
                raise ValueError("%s 那场已经开始/结束了 —— 过去的场次不能挪。"
                                 "（到点还没开始的那场走收尾里的 `p` 推迟。）"
                                 % row.get("startLabel"))
            rec = row.get("relocation") or {}
            from_hhmm = rec.get("from") or row.get("relocated_from") or row["startLabel"]
            cur_hhmm = row["startLabel"]
            dur = row.get("durationMin") or rec.get("duration")
            if not dur:
                raise ValueError("%s 看不出时长（快照？），先 refresh 成功一次再挪。"
                                 % cur_hhmm)
            got = sch.parse_hhmm(cur_hhmm)
            if got is None:
                raise ValueError("当前行时间 %r 不是合法 HH:MM" % cur_hhmm)
            cur_start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
            target = cur_start + datetime.timedelta(minutes=delta)
            if target.date() != day:
                raise ValueError("不能跨天移动：%s %s 挪 %+d 分钟会跑到 %s。"
                                 % (day.isoformat(), cur_hhmm, delta,
                                    target.date().isoformat()))
            moves.append({
                "row": row,
                "from": from_hhmm,
                "cur": cur_hhmm,
                "to": target.strftime("%H:%M"),
                "duration": str(int(dur)),
                "offset": int(rec.get("offset") or 0) + delta,
            })
        self._check_relocate_conflicts(day, moves)
        return moves

    def _check_relocate_conflicts(self, day, moves):
        """撞车检查：挪过去之后不能和**别的**场次重叠。

        单场挪动以前只查「同一时刻」，整段挪动必须查「时间重叠」——
        T1 整段往后 15 分钟时，11:15–12:05 和 12:00 那场就是真撞车（以前只看
        起点不同就放行了）。改成重叠判据之后，单场也一起受益。
        """
        moving_now = set(m["cur"] for m in moves)
        moving_ids = set(m["row"].get("id") for m in moves if m["row"].get("id"))
        targets = {}
        for m in moves:
            if m["to"] in targets:
                raise ValueError("挪完之后 %s 撞车：这一批里有两场被挪到了同一时刻。"
                                 % m["to"])
            targets[m["to"]] = m

        def _span(hhmm, dur):
            got = sch.parse_hhmm(hhmm)
            if got is None:
                return None
            begin = got[0] * 60 + got[1]
            try:
                dur = int(dur or 0)
            except (TypeError, ValueError):
                dur = 0
            return begin, begin + max(1, dur)

        occupied = []
        for slot in sch.day_slots(self.entries, day):
            if slot["start"] in moving_now:
                continue
            span = _span(slot["start"], slot.get("duration"))
            if span:
                occupied.append((span[0], span[1], "%s 那场契约" % slot["start"]))
        for r in getattr(self, "api_rows", []) or []:
            if r.get("id") in moving_ids or r.get("startLabel") in moving_now:
                continue
            span = _span(r.get("startLabel"), r.get("durationMin"))
            if span:
                occupied.append((span[0], span[1], "%s 那场 API session" % r.get("startLabel")))

        for m in moves:
            span = _span(m["to"], m["duration"])
            if span is None:
                raise ValueError("目标时间 %r 不是合法 HH:MM" % m["to"])
            for begin, end, label in occupied:
                if span[0] < end and begin < span[1]:
                    raise ValueError("目标时间 %s 和 %s 重叠；不能撞车。" % (m["to"], label))

    def _migrate_task_bindings(self, day, moves):
        """把挂在这些场次上的任务**跟着挪过去**（绑定 + 截止时间一起动）。

        为什么必须做：任务是用「哪一天 + 哪一刻」认场次的（``tasks_for_session``）。
        session 挪了而绑定留在原地 —— 表格会说「这场一条任务都没有」，收尾也不会
        再问这些任务，它们会静默地丢掉自己那一场（真实踩过的坑）。
        """
        moved = []
        for m in moves:
            try:
                tasks = self.task_store.tasks_for_session(day, m["cur"])
            except Exception:                      # noqa: BLE001 - 绑定挪不动不该挡住挪场次
                tasks = []
            session = {"date": day.isoformat(), "start": m["to"],
                       "duration": int(m["duration"]),
                       "focusmate_session_id": None,
                       "relocated_from": m["from"]}
            for task in tasks:
                if ts.TaskStore.is_repeat_template(task):
                    continue
                try:
                    self.task_store.assign(task["id"], session)
                except (ValueError, TypeError) as e:
                    self.say("⚠ 任务 %s 的绑定没跟着挪：%s"
                             % (self.task_label(task), e), "warn")
                    continue
                moved.append(task)
        if moved:
            self.say("↔ %d 条任务跟着挪到新时间：%s"
                     % (len(moved), "、".join(self.task_label(t) for t in moved)), "ok")
        return moved

    def _apply_relocate(self, day, moves, delta, later_scope=None, reason="relocate",
                        on_done=None):
        """落临时挪动，取消旧 API session；成功后挪任务绑定、再执行 ``on_done``。

        取消失败时把 relocation 文件整批回滚。取消请求现在走后台队列
        （``session.relocate_cancel``），提示符线程不会等浏览器；面板里能看到
        「取消旧 session X」的进度。同步模式（测试 / --exec）仍是当场完成。
        """
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        before = sch.load_relocations()
        after = copy.deepcopy(before)
        recs = after.setdefault("relocations", {})
        for m in moves:
            rec = {
                "kind": "temporary",
                "date": day.isoformat(),
                "from": m["from"],
                "to": m["to"],
                "duration": m["duration"],
                "offset": m["offset"],
                "at": stamp,
            }
            if m.get("scope"):
                rec["scope"] = m["scope"]
            recs[sch.key_of(day, m["from"])] = rec
        if delta > 0:
            last = moves[-1]
            after.setdefault("later_moves", {})[sch.later_move_key(day, later_scope)] = {
                "key": sch.key_of(day, last["from"]),
                "from": last["from"],
                "to": last["to"],
                "scope": later_scope or "",
                "count": len(moves),
                "reason": reason,
                "at": stamp,
            }
        sch.save_relocations(after)

        live = [m for m in moves if m["row"].get("id") is not None]

        def finish_local():
            # 本地表也把旧 session 拿掉：成功取消之后，表格立刻只显示新时间。
            for m in live:
                row = m["row"]
                self.api_rows = [r for r in self.api_rows
                                 if not (r.get("id") == row.get("id")
                                         and r.get("startMs") == row.get("startMs"))]
            # 旧 booked state 清掉：不清的话，等你哪天把挪动撤回去，daemon 会看到
            # 「state 说 booked 但 API 没有」→ 记一笔罪（其实是 warden 自己干的活）。
            self._forget_layout_state(
                [{"date": day.isoformat(), "startLabel": m["cur"]} for m in moves])
            # 任务绑定跟着挪（绑定 + 截止时间）。
            self._migrate_task_bindings(day, moves)
            self.reload_entries()
            self.rebuild_rows()
            self.invalidate()
            if callable(on_done):
                on_done()

        def failed(error):
            try:
                sch.save_relocations(before)
            except Exception as exc:               # noqa: BLE001 - 回滚失败也要说实话
                self.say("⚠ 回滚临时挪动失败：%s" % exc, "warn")
            self.reload_entries()
            self.rebuild_rows()
            self.invalidate()
            self.had_error = True
            self.say("↩ 取消原 session 失败：%s；临时挪动已整批回滚（任务绑定没动）。"
                     % error, "warn")

        if not live:
            finish_local()
            return len(moves)

        action = self.net_action(
            "session.relocate_cancel",
            "取消旧 session 以改排布（%d 场）" % len(live),
            {"moves": [{"start": m["cur"], "startMs": m["row"].get("startMs"),
                        "meeting_id": m["row"].get("id")} for m in live]},
            source=SRC_FOCUSMATE)
        job = self.jobs.submit(action, on_ok=lambda payload: finish_local(),
                               on_fail=failed)
        if self.jobs.sync:
            if job.outcome is None or not job.outcome.ok:
                return False
            return len(moves)
        self.say("已落临时计划；旧 session 的取消在后台跑（进度看面板）。", "cmd")
        return len(moves)

    def _relocate_say(self, day, moves, delta, period_id=None, label=""):
        """挪完之后那一句人话（单场和整段共用）。"""
        moved = "提前" if delta < 0 else "推后"
        what = ("%s 整段 " % period_id) if period_id else (label or "")
        pairs = "、".join("%s→%s" % (m["cur"], m["to"]) for m in moves)
        self.say("✅ %s%s %d 分钟：%s（临时，不改 data.csv；原时间 %s）。"
                 % (what, moved, abs(delta), pairs,
                    "、".join(sorted(set(m["from"] for m in moves)))), "ok")

    def _relocate_period(self, day, period_id, delta, allow_past=False, reason="relocate"):
        """``relocate T1 ±MM`` —— 把一个时段里的**每一场**一起挪（相对位置不变）。"""
        rows = self._period_rows(day, period_id)
        if not rows:
            raise ValueError("%s 在 %s 没有排出来的 session（先 `period %s 50 50` 排一下）。"
                             % (period_id, day.isoformat(), period_id))
        if delta > 0 and sch.get_later_move(day, scope=period_id):
            raise ValueError("%s %s 已经往后挪过一次了；每个时段一天只能往后挪一次。"
                             % (day.isoformat(), period_id))
        moves = self._relocate_moves(day, rows, delta, allow_past=allow_past)
        for m in moves:
            m["scope"] = period_id

        def done():
            self._relocate_say(day, moves, delta, period_id=period_id)
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=period_id,
                                  reason=reason, on_done=done)
        return ok is not False

    def _relocate_after(self, day, moves):
        """挪完之后收尾：重读契约、重画表格，并把 check 放进后台队列。

        check 会开 keep_session 子进程、可能还要浏览器；以前它在提示符线程上
        ``run_keep_session``，一挪场次整个 TUI 就冻住。现在它是一条有进度条的
        后台动作（同时让 bridge 先放开 Chromium profile）。
        """
        self.reload_entries()
        self.rebuild_rows()
        live = [m for m in moves if m["row"].get("zone") != "draft"]
        if not live:
            return True

        def done(payload):
            out = (payload or {}).get("out") or ""
            for line in str(out).splitlines()[-20:]:
                self.say(line)
            self.say("✅ 重排后的 check 跑完。", "ok")
            self.reload_entries()
            self.rebuild_rows()

        def failed(error):
            self.had_error = True
            self.say("❌ 重排后的 check 失败：%s（临时计划已落盘，daemon 后续还会补）"
                     % error, "error")

        action = self.net_action(
            "keep_session.run", "重排后 check（补订/对账）",
            {"argv": ["check"], "timeout": 600, "release_browser": True},
            source=SRC_KEEP)
        job = self.jobs.submit(action, on_ok=done, on_fail=failed)
        if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
            self.reload_entries()
            self.rebuild_rows()
            return False
        self.say("重排后的 check 已排进后台（进度看面板）。", "cmd")
        if not self.offline:
            self.refresh(quiet=True)
        return True

    def cmd_relocate(self, args):
        """relocate N ±MM | relocate T1 ±MM —— 临时挪一场 / 挪一整个时段。

        ``relocate 3 -15``  只有 #3 这一场动。
        ``relocate T1 15``  时段 T1 里**每一场**一起推后 15 分钟：11:00、12:00 两场
                            变成 11:15、12:15，场次之间的相对位置一点不变。

        两者都不写 data.csv：只影响所选这一天的这几场，未来同星期几照旧。
        挪动的**任务绑定跟着走**（``tasks_for_session`` 认的是「哪一天 + 哪一刻」，
        绑定留在原地等于把这些任务从这一场上摘掉）。
        提前随时可以；推后单场是「一天一次」、整段是「每个时段一天一次」，
        而且撞车（重叠）就拒绝 —— 撞车判据对单场也生效了。
        """
        if not args:
            raise ValueError("用法：relocate N ±MM（单场）/ relocate T1 ±MM（整段）"
                             "，MM 是 15 的倍数")
        idx_token, delta = parse_relocate_spec(args)
        if idx_token is None:
            raise ValueError("用法：relocate N ±MM（单场）/ relocate T1 ±MM（整段）")
        day = self.sel_date
        if re.match(r"^[Tt]\d+$", idx_token):
            return self._relocate_period(day, idx_token.upper(), delta)

        idx, row = self.pick(idx_token)
        if delta > 0 and sch.get_later_move(day):
            raise ValueError("%s 已经往后挪过一次了；一天只能往后挪一次。"
                             % day.isoformat())
        moves = self._relocate_moves(day, [row], delta)

        def done():
            self._relocate_say(day, moves, delta, label="#%d" % idx)
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=None,
                                  on_done=done)
        return ok is not False
    # -- 设置 / 警告开关 -------------------------------------------------

    def cmd_settings(self, args=None, show=False):
        """settings —— 只把设置视图的内容说一遍（界面本身在 SettingsView 里）。"""
        rows = self.settings.rows()
        self.say("设置 %d 项：%s" % (len(rows), "、".join(
            "%s=%s" % (r["id"], r["value_display"]) for r in rows)), "info")
        return rows

    def cmd_set(self, args):
        """set <id> <value> —— 改一个设置项；不写 value = 恢复默认值。"""
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：set <设置 id> <value>；现有：%s"
                             % "、".join(r["id"] for r in self.settings.rows()))
        setting_id = toks[0]
        if len(toks) == 1:
            value = self.settings.reset(setting_id)
            self.say("✅ %s 已恢复默认值（%s）" % (setting_id, value or "（空）"), "ok")
        else:
            value = self.settings.set(setting_id, " ".join(toks[1:]))
            self.say("✅ %s = %s" % (setting_id, value or "（未设置）"), "ok")
        # 设置可能立即改变行为（比如 edit-tomorrow 影响草稿/执行区判定）
        if setting_id == "tomorrow-plan" and value:
            self.say("   想让它真的挂到 session 上，再建一条同名重复任务："
                     "task create title=明日时间安排 --repeat %s" % value, "info")
        self.refresh_warnings(force=True)
        return value

    def cmd_warn(self, args):
        """warn on|off —— 状态框里的警告要不要显示（不写 = 取反）。"""
        token = str(args[0]).strip().lower() if args else ""
        if token in ("on", "true", "1"):
            on = self.shell.set_warnings(True)
        elif token in ("off", "false", "0"):
            on = self.shell.set_warnings(False)
        elif not token:
            on = self.shell.set_warnings()
        else:
            raise ValueError("用法：warn on|off（不写就是取反）")
        self.say("状态框警告：%s" % ("显示" if on else "隐藏"), "info")
        return on

    def _drop_contract_row(self, idx, row):
        """草稿区：把 data.csv 里那行删掉。零阻力、不问任何问题 —— 这就是「自由」。

        如果这一行是临时挪动出来的：撤销挪动（恢复 data.csv 原时间），
        **不删** data.csv 的按星期几规则；已订上的目标 session 顺手取消。
        """
        if row.get("relocation"):
            rec = row.get("relocation") or {}
            from_hhmm = rec.get("from") or row.get("relocated_from") or row["startLabel"]
            # 先乐观撤销本地挪动，再把「取消已订上的临时目标」丢后台。
            # 失败时把 relocation 文件和表格行一起放回去 —— 用户不用等浏览器。
            before_relocations = sch.load_relocations()
            before_row = dict(row)
            removed = self._forget_api_row(row.get("id")) if row.get("id") else None
            removed_at = removed[0] if removed else None
            sch.remove_relocation(self.sel_date, from_hhmm)
            self.rebuild_rows()
            self.invalidate()
            if row.get("id") is not None:
                action = self.net_action(
                    "session.cancel",
                    "取消临时目标 %s %s" % (self.sel_date.isoformat(),
                                           row["startLabel"]),
                    {"date": self.sel_date.isoformat(),
                     "start": row.get("startLabel"),
                     "startMs": row.get("startMs"),
                     "meeting_id": row.get("id")})
                def failed(error, _before=before_relocations, _row=before_row,
                           _idx=removed_at):
                    try:
                        sch.save_relocations(_before)
                    except Exception as e:         # noqa: BLE001 - 回滚失败也要说话
                        self.say("⚠ 回滚临时挪动失败：%s" % e, "warn")
                    self._row_op_failed("#%d 取消临时目标 %s"
                                        % (idx, _row.get("startLabel")),
                                        _row, error, index=_idx)
                    self.refresh(quiet=True)
                job = self.jobs.submit(action, on_fail=failed)
                if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
                    return False
                self.say("✅ 已撤销 #%d 的临时挪动：恢复原时间 %s；%s"
                         % (idx, from_hhmm,
                            "临时目标已取消" if self.jobs.sync
                            else "取消临时目标已排进后台（失败会自动放回）"), "ok")
            else:
                self.say("✅ 已撤销 #%d 的临时挪动：恢复 data.csv 原时间 %s。"
                         % (idx, from_hhmm), "ok")
            self.refresh(quiet=True)
            return True

        if not row.get("lineno"):
            raise ValueError("#%d 在 data.csv 里没有对应行（计划外的场次），删不了" % idx)
        if sch.fingerprint() != self.csv_fp:
            raise ValueError("data.csv 刚被别处改过（指纹不一致）。先 refresh 再动手，免得删错行。")
        raw = sch.remove_row(None, row["lineno"], expect=row.get("raw"))
        self.say("✅ 已从 data.csv 删掉第 %d 行：%s" % (row["lineno"], raw.strip()), "ok")
        self.say("   注意：这是**按星期几**的规则 → 以后每个%s都不会再有这一场。"
                 % sch.cn_weekday_of_day(self.sel_date), "warn")
        self.refresh(quiet=True)
        return True

    def cmd_day(self, args):
        """day / day -1 / day 0 / day 1 / day 2026-10-11 —— 切换看哪一天。"""
        if not args:
            self.say("现在看的是 day %+d（%s %s）"
                     % (self.day_offset, self.sel_date.isoformat(),
                        sch.cn_weekday_of_day(self.sel_date)), "info")
            return False
        token = args[0].strip()
        today = datetime.date.today()
        try:
            offset = int(token)
        except ValueError:
            try:
                offset = (datetime.date.fromisoformat(token) - today).days
            except ValueError:
                raise ValueError("day 要么是偏移量（-1 / 0 / 1），要么是 YYYY-MM-DD；"
                                 "你给的是 %r" % token)
        if offset < -7 or offset > 60:
            raise ValueError("只让看 -7 .. +60 天（再远没意义），你给的是 %+d" % offset)
        self.day_offset = offset
        self.say("切到 %s %s（day %+d）" % (self.sel_date.isoformat(),
                                          sch.cn_weekday_of_day(self.sel_date), offset), "cmd")
        if self.offline:
            self._fallback_rows("离线模式")
        else:
            self.refresh()
        return True
    def _layout_rows(self, day):
        """改排布要看的「当前已订场次」。

        **看板那一天用 ``self.rows``**（它就是刚拉回来的真数据，行号也在这里），
        **别的天用快照**（formatter 会切明天/后天的场次）。以前这两条路各写一份
        取数逻辑，现在统一在这里；``_snapshot_rows`` 在 sync 模式下会重问一次
        bridge，拿看板那天的行去问会把手上这份真数据换成空快照。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if iso == self.sel_date.isoformat() and self.authoritative:
            return [dict(row) for row in (self.rows or ())
                    if str(row.get("date") or "") == iso]
        return self._snapshot_rows(iso)

    def _period_row_is_stale(self, row, new_by_start):
        """这一行 API session 还符合新排布吗？（按起点 + 时长判断）"""
        start = row.get("startLabel")
        if start not in new_by_start:
            return True
        dur = row.get("durationMin")
        try:
            return dur is not None and int(dur) != int(new_by_start[start])
        except (TypeError, ValueError):
            return True

    def _acquire_round_lock(self, timeout=30.0, poll=0.5, on_wait=None):
        """拿 daemon 那一轮的同一把文件锁（`keep_session.round_lock`）。

        为什么：改排布是「取消旧场 → 清 state → 写新排布」三步。daemon 要是刚好
        在中间跑一轮，就会看到「state 说 booked、API 里没了」，把 **warden 自己
        干的活** 记成「你手动取消」的罪，然后 TUI 锁死 —— 用户就是这么被咬的。
        等不到（daemon 那一轮在下单，可能几十秒）就返回 None，调用方用挂号单兜底。
        """
        lock = ks.round_lock()
        if lock.__enter__():
            return lock
        if on_wait:
            on_wait()
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            time.sleep(poll)
            lock = ks.round_lock()
            if lock.__enter__():
                return lock
        return None

    def _period_cancel_keys(self, period, new_sessions, day=None):
        """这一时段里「改完排布之后就不该再存在」的 state key（给 daemon 挂号用）。

        ``day`` 省略时退回 period 自带日期 / 看板那天（兼容老调用）。
        """
        kept = set()
        for sess in new_sessions or []:
            try:
                kept.add((str(sess.get("start")), int(sess.get("duration"))))
            except (TypeError, ValueError):
                continue
        date = (day.isoformat() if hasattr(day, "isoformat") else str(day or "")) \
            or period.get("date") or self.sel_date.isoformat()
        keys = set()
        # 1) 表格里有 id、但新排布里没有同起点同时长的场次
        for row in self._layout_rows(datetime.date.fromisoformat(date)):
            if not row.get("id") or str(row.get("date") or "") != date:
                continue
            start = row.get("startLabel")
            if sp.period_for_session([period], start) is None:
                continue
            try:
                dur = int(row.get("durationMin"))
            except (TypeError, ValueError):
                dur = None
            if (str(start), dur) not in kept:
                # 已经开始/过去的场次不进入挂号单：它们根本不会被自动取消，
                # 不能给 daemon 留一张可能误掩护后续手动取消的 `expect_cancel`。
                if self._session_already_started(row, now=self._now_dt()):
                    continue
                try:
                    keys.add(sch.key_of(datetime.date.fromisoformat(date), start))
                except ValueError:
                    pass
        # 2) state 里记着（哪怕表格没显示出来）的槽位，新排布里也不该再有
        try:
            slots = (ks.load_state().get("slots") or {})
        except Exception:                              # noqa: BLE001
            slots = {}
        for key, rec in slots.items():
            if not str(key).startswith(date + "T"):
                continue
            start = str(key).split("T", 1)[1]
            if sp.period_for_session([period], start) is None:
                continue
            dur = (rec or {}).get("duration")
            try:
                dur = int(dur)
            except (TypeError, ValueError):
                dur = None
            if (start, dur) not in kept:
                if self._session_already_started({"date": date, "start": start},
                                                 now=self._now_dt()):
                    continue
                keys.add(str(key))
        return keys

    def _task_bindings_dropped(self, period, new_sessions):
        """排布变了之后，哪些任务绑定会被清掉（纯计算，不改任何东西）。

        判据：**(起点, 时长) 完全一致才算「这场没动」**。
        50 25 25 → 50 50 时第一场还是 11:00(50)，它挂的任务留着；
        50 25 → 25 50 时没有一场对得上，两场的绑定全清。
        """
        return [task for task, _key in self._task_bindings_split(period, new_sessions)[1]]

    def _task_bindings_split(self, period, new_sessions):
        """把这一时段里挂着任务分两堆：(留着的, 要清掉的)，元素是 (task, key)。"""
        kept_keys = set()
        for sess in new_sessions or []:
            try:
                kept_keys.add((str(sess.get("start")), int(sess.get("duration"))))
            except (TypeError, ValueError):
                continue
        date = period.get("date") or self.sel_date.isoformat()
        keep, drop = [], []
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if sess.get("date") != date:
                continue
            if sp.period_for_session([period], sess.get("start")) is None:
                continue
            try:
                key = (str(sess.get("start")), int(sess.get("duration")))
            except (TypeError, ValueError):
                key = (str(sess.get("start")), None)
            (keep if key in kept_keys else drop).append((task, key))
        return keep, drop

    def clear_task_bindings_for_layout(self, period, new_sessions):
        """排布改了：留下来的场次保留任务绑定，动过的场次清掉绑定。

        任务本身**不删**（还是任务库里的），只是不再挂在那一场上；
        Focusmate 侧那条 task 记进 ``focusmate.previous``，
        以后它要是被勾了完成，`task sync` 照样能按 completedAt 归档。
        """
        _keep, drop = self._task_bindings_split(period, new_sessions)
        if not drop:
            return []
        cleared = []
        for task, key in drop:
            sess = task.get("session") or {}
            # 已经开始/正在进行的 session 不因改排布而丢当前任务绑定。
            if self._session_in_progress(sess, now=self._now_dt()):
                continue
            try:
                self.task_store.unassign(task.get("id"), reason="layout-changed")
            except ValueError:
                continue
            cleared.append(task)
            self.say("清掉任务 %s「%s」的绑定（原来 %s %s，这一场排布改了）"
                     % (self.task_label(task),
                        self.task_store.summarize(task, max_len=24),
                        (task.get("session") or {}).get("date") or period.get("date"),
                        key[0]), "warn")
        return cleared

    def _forget_layout_state(self, rows):
        """清掉被 warden 主动取消的旧 state 记录，避免 daemon 误记 sin。"""
        if not rows:
            return
        try:
            state = ks.load_state()
            slots = state.setdefault("slots", {})
            removed = 0
            for row in rows:
                try:
                    day = datetime.date.fromisoformat(row.get("date"))
                except (TypeError, ValueError):
                    day = self.sel_date
                key = sch.key_of(day, row.get("startLabel"))
                if key in slots:
                    slots.pop(key, None)
                    removed += 1
            if removed:
                ks.save_state(state)
                self.say("已清掉 %d 条旧 booked state（不会误记 sin）。" % removed, "info")
        except Exception as e:                        # noqa: BLE001
            self.say("清理旧 state 失败（daemon 可能多记一笔 sin）：%s" % e, "warn")

    def _cancel_stale_period_sessions(self, day, period, new_sessions,
                                      dropped_tasks=(), reason="layout-changed"):
        """把「新排布里不再存在」的已订场次在 Focusmate 上取消掉。

        **所有排布写入者共用的唯一一份实现。** 以前有两份：一份吃 ``self.rows``
        （改今天）、一份吃 ``_snapshot_rows(iso)``（formatter 改别的天），逻辑
        一模一样但已经分叉。现在统一按日期取快照，改今天和改明天走同一条路。

        为什么必须做：data.csv 换成 22:15(25)+22:45(25) 之后，API 里原先那场
        22:15(50) 不会自己消失；daemon 只补订、不擅自杀旧场，于是 50min/25min
        会同时存在。这里在改契约之前先把「起点或时长不再匹配」的场次取消。

        **改排布不要宣言**（用户明确要求）：排布是「计划」，点 UI 就能调；
        有阻力的是**取消单场**（`cancel N` 那条路仍然要敲宣言，一点没放松）。
        为了不让人稀里糊涂丢东西，取消前照样把面板列出来，并把会被清掉的任务
        绑定一起写清楚。

        返回 ``(cancelled, failed)``：cancelled 是被取消的行；failed=True 表示
        同步模式下至少有一条取消失败（调用方据此别把新排布当成已生效）。
        """
        if self.offline:
            return [], False
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        new_by_start = {s["start"]: int(s["duration"]) for s in new_sessions or []}
        stale = []
        for row in self._layout_rows(day):
            if not row.get("id"):
                continue
            if str(row.get("date") or "") != iso:
                continue
            if sp.period_for_session([period], row.get("startLabel")) is None:
                continue
            if self._period_row_is_stale(row, new_by_start):
                stale.append(row)
        stale, _protected = self._split_started_sessions(stale)
        if not stale:
            return [], False

        past = [r for r in stale if r.get("zone") == "past"]
        live = [r for r in stale if r.get("zone") != "past"]
        if past:
            self.say("有 %d 场旧 session 已经过去；过去的场次不取消、不补订。"
                     % len(past), "warn")
        if not live:
            return [], False

        # 挂号单必须写在真正取消**之前**：daemon 任何一轮看到「booked 但 API 没了」
        # 都会先查它，命中就不记罪（一码一用，不掩护以后真的手动取消）。
        keys = self._period_cancel_keys(period, new_sessions, day=day)
        if keys:
            ks.expect_cancel(sorted(keys), reason="%s:%s %s"
                             % (reason, period.get("id"), iso))

        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True)
        info.add_column()
        info.add_row("时段", "%s  %s–%s  %s"
                     % (period.get("id"), period.get("start"), period.get("end"),
                        period.get("name") or period.get("type") or ""))
        for row in live:
            try:
                label = "#%d" % (self.rows.index(row) + 1)
            except ValueError:
                label = "—"          # 改的不是看板那一天，表格里没有行号
            info.add_row("要取消", "%s  %s–%s（%smin）"
                         % (label, row.get("startLabel"),
                            row.get("endLabel") or "?", row.get("durationMin") or "?"))
        info.add_row("新排布", "  ".join("%s–%s(%s)"
                                       % (s.get("start"), s.get("end"),
                                          s.get("duration"))
                                       for s in new_sessions) or "（空）")
        for task in dropped_tasks:
            sess = task.get("session") or {}
            info.add_row("清任务绑定", "#%s %s @%s"
                         % (task.get("id"),
                            self.task_store.summarize(task, max_len=24),
                            sess.get("start") or "?"))
        self.emit(Panel(info, title="[bold yellow]⚠ 改排布：旧 session 会被取消[/]",
                        border_style="yellow", box=box.HEAVY))

        cancelled, failed = [], False
        try:
            for row in live:
                self.say("取消旧 session %s–%s（%smin）…"
                         % (row.get("startLabel"), row.get("endLabel") or "?",
                            row.get("durationMin") or "?"), "cmd")
                # 乐观 + 后台：旧行立刻从表格消失，取消请求交给单 worker。
                # 这里以前直接 bridge.call("cancel")，改排布会在提示符线程上
                # 等浏览器几十秒 —— 和「外部操作全异步」的约定不符。
                before_row = dict(row)
                before_index, _ = self._forget_api_row(row.get("id")) or (None, None)
                action = self.net_action(
                    "session.cancel",
                    "取消旧 session %s %s" % (iso, row.get("startLabel") or "?"),
                    {"date": iso, "start": row.get("startLabel"),
                     "startMs": row.get("startMs"), "meeting_id": row.get("id")})
                job = self.jobs.submit(
                    action,
                    on_fail=lambda error, _row=before_row, _idx=before_index:
                    self._row_op_failed("旧 session %s 取消"
                                        % _row.get("startLabel"),
                                        _row, error, index=_idx))
                if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
                    self.had_error = True
                    failed = True
                    continue
                cancelled.append(row)
                self.say("✅ %s旧 session %s"
                         % ("已取消" if self.jobs.sync else "已排进后台取消",
                            row.get("startLabel")), "ok")
            if cancelled and not self.jobs.sync:
                self.rebuild_rows()
        finally:
            # 关键：旧 session 是 warden 主动取消的，不是用户手动搞丢的。
            # 清掉 state 里的旧 booked 记录，否则 daemon 下一轮会记一笔 sin 并锁死 TUI。
            # 同一个起点如果新排布还要用，它会以新 duration 重新进入待订队列。
            self._forget_layout_state(cancelled)
        return cancelled, failed

    # -- 静音时段（period mute） --------------------------------------

    def mute_targets(self, rows=None):
        """「该静音、但 Focusmate 那边 quiet 还不是 ON」的行。

        判据只有一个：这行的起点落在 ``period mute`` 写下的时间窗里
        （``row["period_muted"]``，见 :func:`mute_window_for`）。
        没有 id 的行动不了（快照 / 还没订上），跳过。
        """
        out = []
        for row in (self.rows if rows is None else rows):
            if not row.get("period_muted"):
                continue
            if row.get("quiet"):
                continue
            if row.get("id") is None:
                continue
            out.append(row)
        return out

    def apply_mutes(self, rows=None, quiet_spinner=True):
        """把「时段静音」落到 Focusmate（quiet on）。返回 ``[(标签, ok, 说明)]``。

        幂等：已经是 ON 的行不在 :meth:`mute_targets` 里，所以重复调用只是查一次。
        这是**意图 → 结果**的那一步：`period mute` 只写本地那张表，真正让这一场
        安静下来的是这里（Focusmate 的 quiet 是单场开关）。
        """
        if self.offline:
            return []
        targets = self.mute_targets() if rows is None else list(rows)
        out = []
        for row in targets:
            label = "%s–%s" % (row.get("startLabel"), row.get("endLabel") or "?")
            status = self.console.status("[yellow]把 %s 设成静音…[/]" % label, spinner="dots")
            try:
                if quiet_spinner:
                    status.__enter__()
                try:
                    res = self.bridge.call("mute", startMs=row["startMs"],
                                           meetingId=row["id"], quiet=True)
                finally:
                    if quiet_spinner:
                        status.__exit__(None, None, None)
            except (BridgeError, KeyError) as e:
                self.had_error = True
                out.append((label, False, str(e)))
                self.say("❌ %s 静音失败：%s" % (label, e), "error")
                continue
            ok = bool(res.get("verified"))
            if ok:
                out.append((label, True, ""))
                self.say("🔇 %s 已静音（时段要求，Focusmate quiet = ON）" % label, "ok")
            else:
                self.had_error = True
                out.append((label, False, "复核不一致：%s" % (res.get("quiet"),)))
                self.say("❌ %s 静音后复核不一致：quiet = %s"
                         % (label, res.get("quiet")), "error")
        if out:
            self.invalidate()
        return out

    def _maybe_apply_mutes(self):
        """每帧看一眼：有「时段要求静音但还没设上」的场次 → 后台补上。

        节流两层：整体 5 秒一次（`mutes_at`），同一场 5 分钟内只试一次
        （`mute_tries`，避免 bridge 连着失败时每帧都去开浏览器）。
        """
        if self.offline or self.review is not None:
            return
        now = time.time()
        if now - self.mutes_at < 5.0:
            return
        self.mutes_at = now
        cooldown = 300.0
        targets = []
        for row in self.mute_targets():
            key = (row.get("date"), row.get("startLabel"))
            if now - self.mute_tries.get(key, 0.0) < cooldown:
                continue
            self.mute_tries[key] = now
            targets.append(row)
        if not targets:
            return
        if self.mute_worker is not None and self.mute_worker.is_alive():
            return
        self.mute_worker = threading.Thread(target=self._mute_worker, args=(targets,),
                                           daemon=True)
        self.mute_worker.start()

    def _mute_worker(self, targets):
        try:
            self.apply_mutes(rows=targets, quiet_spinner=False)
        except Exception as e:                   # noqa: BLE001
            self.say("自动静音出错（下次再试）：%s: %s" % (type(e).__name__, e), "warn")

    def cmd_period_mute(self, args):
        """``period mute T1 [--permanent]`` / ``period unmute T1 [--permanent]``。

        需求原话：「我希望可以设置静音时段，比如说 ``period mute t1 --permanent``
        就是设置之后这里都静音。在设置完成之后，这个时段内部的 session 自动切换为
        静音模式」。

        **「静音」= Focusmate session 的 quiet mode，仅此而已**（用户明确过：
        daemon 在静音时段照旧发通知、拉音量、放嘶吼）。所以这条命令只写**意图**
        （``temp/session-mutes.json``：这个时段按星期几重复，或者只今天），
        然后把它落到 Focusmate 那一场上：

        1. 命令本身立刻设一遍（今天的、还在这个窗口里的场次）；
        2. 之后每帧后台补（:meth:`_maybe_apply_mutes`）—— 新订上的场次自己安静下来，
           已经是 ON 的不会反复动。

        本地行为一概不变：分心看门狗照旧通知/拉音量/放嘶吼（``keep_session`` 里
        根本没有读静音表的地方）。

        ``--permanent`` 写星期几（以后每个这个星期几都静音）；不写就只静音这一天。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        want_on = True
        permanent = False
        period_id = None
        if toks and toks[0].lower() in ("unmute", "解除静音", "取消静音", "off", "关"):
            want_on = False
            toks = toks[1:]
        if toks and toks[0].lower() in ("mute", "静音", "on", "开"):
            toks = toks[1:]
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "--perm", "permanent"):
                permanent = True
            elif low in ("off", "unmute", "关", "解除静音", "取消静音"):
                want_on = False
            elif low in ("on", "mute", "开", "静音"):
                want_on = True
            elif re.match(r"^[Tt]\d+$", tok):
                period_id = tok.upper()
            else:
                raise ValueError("认不出参数 %r。用法：period mute T1 [--permanent] / "
                                 "period unmute T1 [--permanent]" % (tok,))
        day = self.sel_date
        periods = self.periods_for_day(day)
        if period_id is None:
            ids = "、".join(p.get("id", "?") for p in periods) or "无"
            raise ValueError("要哪个时段？%s 的时段：%s（例：period mute T1 --permanent）"
                             % (day.isoformat(), ids))
        period = next((p for p in periods if p.get("id") == period_id), None)
        if period is None:
            ids = "、".join(p.get("id", "?") for p in periods) or "无"
            raise ValueError("没有时段 %s（%s 的时段：%s）"
                             % (period_id, day.isoformat(), ids))

        scope = (("每个%s的 " % sch.cn_weekday_of_day(day)) if permanent
                 else ("%s 的 " % day.isoformat()))
        if not want_on:
            gone = sp.unmute_period(day, period_id, permanent=permanent)
            if not gone:
                self.say("ℹ️ %s%s 本来就没设静音（%s）"
                         % (scope, period_id, "按星期几" if permanent else "按日期"), "info")
            else:
                self.say("🔊 已解除静音：%s%s（%s–%s）"
                         % (scope, period_id, gone[0].get("start"), gone[0].get("end")), "ok")
            forget_mute_cache()
            self.rebuild_rows()
            return True

        rec = sp.mute_period(day, period, permanent=permanent)
        forget_mute_cache()
        self.say("🔇 %s%s 静音了：%s–%s（%s）"
                 % (scope, period_id, rec["start"], rec["end"],
                    "写进按星期几重复的表" if permanent else "只这一天"), "ok")
        self.say("   这段钟点里的 session 会自动打开 Focusmate quiet（那才是「静音」"
                 "的本体）；本地看门狗/通知照旧 —— 静音只作用于 Focusmate 那一场。"
                 "撤销：period unmute %s%s"
                 % (period_id, " --permanent" if permanent else ""), "info")
        self.rebuild_rows()
        applied = self.apply_mutes()
        if applied:
            self.refresh(quiet=True)
        return True

    def cmd_mute_universal(self, args):
        """``mute-universal 13:00 - 15:00 Mon`` —— 每周固定时间窗静音。

        与 ``period mute`` 的区别：不绑定日历时段 T1/T2，也不要求 session 的
        **起点**落在窗内。用户要求「只要涉及了就算」：14:30–15:30 与
        13:00–15:00 有重叠，同样静音。可以一次给多个星期几（Mon,Wed / 周一 周三）。
        """
        raw = " ".join(str(a).strip() for a in (args or []) if str(a).strip())
        low = raw.lower()
        if not raw or low in ("list", "ls", "show", "列表", "查看"):
            recs = sp.universal_mutes()
            if not recs:
                self.say("ℹ️ 还没有通用静音窗。例：mute-universal 13:00 - 15:00 Mon",
                         "info")
                return []
            for rec in recs:
                wd = cfg.parse_weekday(rec.get("weekday"))
                label = ("每个" + cfg.WEEKDAY_CN[wd]) if wd is not None else "?"
                self.say("🔇 %s %s–%s（与窗口有重叠的 session 都静音）"
                         % (label, rec.get("start"), rec.get("end")), "info")
            return recs

        want_off = any(tok in low for tok in
                       ("off", "unmute", "remove", "delete", "取消", "清空", "解除"))
        # 先找时间窗；星期几可以在前面或后面。也接受 13:00~15:00 / 13:00–15:00。
        match = re.search(r"(?<!\d)(\d{1,2}:\d{2})\s*[-–—~至]\s*(\d{1,2}:\d{2})(?!\d)",
                          raw)
        if not match:
            if want_off:
                gone = sp.unmute_universal()
                forget_mute_cache()
                self.rebuild_rows()
                self.say("🔊 清空了 %d 条通用静音窗。" % len(gone), "ok")
                return gone
            raise ValueError("用法：mute-universal 13:00 - 15:00 Mon"
                             "（撤销：mute-universal off 13:00 - 15:00 Mon；"
                             "看列表：mute-universal list）")
        start, end = match.group(1), match.group(2)
        if sp.parse_hhmm(start) is None or sp.parse_hhmm(end) is None:
            raise ValueError("时间要写成 HH:MM（00:00–23:59）；你给的是 %s–%s"
                             % (start, end))
        rest = (raw[:match.start()] + " " + raw[match.end():])
        tokens = [tok.strip(" ,，/、") for tok in rest.split()]
        weekdays = []
        for tok in tokens:
            part = tok.strip()
            if not part:
                continue
            if part.lower() in ("off", "unmute", "remove", "delete", "取消",
                                "清空", "解除", "mute", "on", "静音"):
                continue
            # Mon-Fri / 周一-周五 这种区间也顺手支持。
            bits = [b for b in re.split(r"[-–—~至]", part) if b.strip()]
            if len(bits) == 2:
                lo, hi = cfg.parse_weekday(bits[0]), cfg.parse_weekday(bits[1])
                if lo is None or hi is None:
                    raise ValueError("认不出星期几区间：%r" % part)
                if lo <= hi:
                    weekdays.extend(cfg.WEEKDAYS[lo:hi + 1])
                else:
                    weekdays.extend(cfg.WEEKDAYS[lo:] + cfg.WEEKDAYS[:hi + 1])
                continue
            wd = cfg.parse_weekday(part)
            if wd is None:
                raise ValueError("认不出星期几：%r（可写 Mon / 周一 / Mon,Wed / Mon-Fri）"
                                 % part)
            weekdays.append(cfg.WEEKDAYS[wd])
        # 去重，保留用户给的顺序。
        weekdays = list(dict.fromkeys(weekdays))
        if not weekdays:
            raise ValueError("要指定星期几，例如 mute-universal 13:00 - 15:00 Mon")
        cn = "、".join(cfg.WEEKDAY_CN[cfg.parse_weekday(wd)] for wd in weekdays)

        if want_off:
            gone = sp.unmute_universal(start, end, weekdays)
            forget_mute_cache()
            self.rebuild_rows()
            if gone:
                self.say("🔊 已撤销 %s %s–%s 的通用静音。" % (cn, start, end), "ok")
            else:
                self.say("ℹ️ %s %s–%s 本来就没有通用静音。" % (cn, start, end), "info")
            return gone

        made = sp.mute_universal(start, end, weekdays)
        forget_mute_cache()
        self.rebuild_rows()
        self.say("🔇 已设通用静音：每个%s %s–%s —— 所有与这个窗口**有重叠**的 "
                 "session 都会打开 Focusmate quiet。" % (cn, start, end), "ok")
        self.say("   看全部：mute-universal list；撤销：mute-universal off %s - %s %s"
                 % (start, end, weekdays[0]), "info")
        applied = self.apply_mutes()
        if applied:
            self.refresh(quiet=True)
        return made

    # -- 排布落盘：整条流水线的唯一事务 --------------------------------
    def commit_layout(self, day, period, durations, *, source=None,
                      mode="set", dropped_tasks=None, reason="layout-changed",
                      book=False, announce=True, write_csv=True,
                      missing_ok=False):
        """**唯一的排布落盘事务**：取消旧场 → 写 layout → 清绑定 → 重算 data.csv。

        以前这一串被手抄了 5 份（``period`` / ``plan`` 自动排布 / formatter 切割 /
        formatter 兜底加场 / CLI），每份的步骤顺序、是否拿 daemon 的 round lock、
        是否写挂号单都不一样 —— 同一条铁律只有 1 份守住。现在全部走这里。

        顺序不能变，每一步都有事故背书：

        1. 纯计算先验证（``plan_period`` 失败 = 零副作用）；
        2. 撤「永不重订」判决（重新排布 = 你又想要这几场）；
        3. 拿 daemon 的 round lock（等不到就靠挂号单兜底）；
        4. 写挂号单 → 取消旧场（顺序反了 daemon 会记一笔 sin 并锁死 TUI）；
        5. 写 layout（**落盘成功之后**才动任务绑定，写失败就什么都不清）；
        6. 重算 data.csv（下载层 + 所有 layout + 一次性 + 长期规则）；
        7. 放开锁。

        ``mode``：
          * ``set``    —— 写 ``durations``（``source`` 默认 manual）；
          * ``clear``  —— 写空排布，保留 period 本身（``period T1 clear``）；
          * ``remove`` —— 删掉自定义排布，回落到默认。

        ``book=True`` 时才在末尾统一预约（refresh 的自动重排传 False）。
        ``write_csv=False`` 给「一次改好几个时段」的调用方用：由它最后统一重算
        一次 data.csv，避免每段都整表重写一遍。

        返回 ``{"ok", "sessions", "cancelled", "dropped", "failed"}``。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        period_id = period.get("id")
        if mode == "clear":
            effective = []
        elif mode == "remove":
            effective = sp.default_durations(period)
        else:
            # 注意 ``[]`` 是「这个时段当天不排了」，不能当成「没给」回落默认排布。
            effective = (list(durations) if durations is not None
                         else sp.default_durations(period))
        # 纯计算先验证；这里抛 ValueError = 零副作用。
        new_sessions = sp.plan_period(period, effective)

        # 排布一变，动过的场次上的任务绑定就该清掉；先算出来，好写进取消前的面板。
        dropped = (self._task_bindings_dropped(period, new_sessions)
                   if dropped_tasks is None else list(dropped_tasks))

        # 重新排布 = 你又想要这几场：先撤掉「永不重订」的判决，否则认过罪的场次
        # 永远补不回来（daemon 在 due 之前就把 abandoned 的 key 滤掉了）。
        revived = ks.clear_abandoned(
            [sch.key_of(day, s["start"]) for s in new_sessions])
        if revived:
            self.say("撤销 %d 条「永不重订」判决：这几场重新进 daemon 的下单队列。"
                     % revived, "ok")

        # 取消旧场 → 清 state → 写新排布 这三步必须和 daemon 的一轮互斥（同一把锁），
        # 否则它会在缝隙里把 warden 自己干的活记成「你手动取消」的罪。
        lock = self._acquire_round_lock(
            on_wait=lambda: self.say("daemon 正在跑一轮，等它跑完再改排布（最多等 30 秒）…",
                                     "cmd"))
        if lock is None:
            self.say("daemon 正在跑一轮（等不到 round lock），改成挂号单兜底："
                     "会让它别把这次改动记成罪。", "warn")
        cancelled, failed = [], False
        cleared = []
        try:
            # 旧 API session 先对齐，否则 50min / 25min 会并存。
            # 挂号单写在真正取消之前，由它内部负责（见 _cancel_stale_period_sessions）。
            cancelled, failed = self._cancel_stale_period_sessions(
                day, period, new_sessions, dropped, reason=reason)
            if failed:
                # 取消失败 = 新排布没生效，别再往下写 layout（否则 50/25 并存）。
                return {"ok": False, "sessions": new_sessions,
                        "cancelled": cancelled, "dropped": dropped,
                        "cleared": [], "failed": True}

            if mode == "clear":
                sp.set_layout(day, period_id, [], source="auto")
                if announce:
                    self.say("🧹 已清空 %s %s 的 session 内容；period 本身保留，"
                             "下次 plan 会按任务重新生成。" % (iso, period_id), "ok")
            elif mode == "remove":
                removed = sp.remove_layout(day, period_id)
                if removed is None and not missing_ok:
                    # 交互式 `period T1`（不带时长）才需要「本来就没有」这个提示；
                    # 自动路径（formatter 切割）要的是「确保没有自定义排布」这个
                    # 终态，本来就没有 = 已经达成，不该抛。
                    raise ValueError("%s 没有自定义排布（本来就是默认）" % period_id)
                if announce:
                    self.say("已撤销 %s 的自定义排布，恢复默认 50/25。" % period_id, "ok")
            else:
                kwargs = {"source": source} if source else {}
                sp.set_layout(day, period_id, effective, **kwargs)
                if announce:
                    body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                                     for s in new_sessions)
                    self.say("✅ %s %s → %s" % (iso, period_id, body), "ok")

            # 排布落盘之后才动任务绑定：没写进去就什么都不清。
            cleared = self.clear_task_bindings_for_layout(period, new_sessions)

            # 从下载层 + 所有 layout + 一次性 + 长期规则重新生成 data.csv。
            if write_csv and self.rewrite_data_csv_from_plan() and announce:
                self.say("data.csv 已按下载层重算（精确日期行）。", "info")
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

        if book:
            self.book(day, why="排布变了，要把新场次订上")
        return {"ok": True, "sessions": new_sessions, "cancelled": cancelled,
                "dropped": dropped, "cleared": cleared, "failed": False}

    def cmd_period_layout(self, args):
        """period T1 50 50 / session T1 50 50 —— 改一个时段的 session 排布。

        需求里的规则原样实现：
          - 时长只认 25/50/75（Focusmate 的限制）；
          - 第一场从时段起点开始，之后每场从上一场结束后的下一个 15 分钟整点开始；
          - 任何一场超出时段终点 → 直接报错，**layout 和 data.csv 都不动**。

        改排布时若 API 里已有旧 session（例如原来 22:15 50，现在要 22:15 25 +
        22:45 25），先取消不再匹配的旧场，再写新契约。

        **改排布不需要宣言**（用户明确要求）：计划怎么摆是你的事；
        真正有阻力的是取消单场（`cancel N`）。
        任务绑定按「(起点, 时长) 有没有动」处理：没动的场次绑定留着
        （50 25 25 → 50 50，第一场还是 11:00(50)，任务照旧），
        动过的场次绑定清掉（50 25 → 25 50，两场都变 → 两个任务的绑定都清）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：period T1 50 50（时段 ID 看表格第一列）"
                             " / period mute T1 [--permanent]")
        # `period mute T1 --permanent` 走静音那条路（同一个命令名词，两个动作）
        if toks[0].lower() in ("mute", "unmute", "静音", "解除静音", "取消静音"):
            return self.cmd_period_mute(toks)
        period_id = toks[0]
        if not re.match(r"^[Tt]\d+$", period_id):
            raise ValueError("时段 ID 要像 T1 / T2；你给的是 %r" % period_id)
        period_id = period_id.upper()
        durations = toks[1:]
        # `period T1 clear` = 把这一段的 session 内容清空，但保留 period 本身；
        # 之后可以重新 plan，让任务按 period 容量自动生成新 session。
        clearing = bool(durations and str(durations[0]).lower()
                        in ("clear", "reset", "清空", "清除"))
        if clearing:
            durations = []

        # 先刷新一次，拿到当前 API 里的真实 session（id / duration）。
        # 拉不到就别改 —— 否则没法判断旧场次要不要取消，会留下 50/25 并存。
        if not self.offline and time.time() - self.rows_at > 20:
            if not self.refresh(quiet=True):
                raise ValueError("拉不到当前 API session；先 refresh 成功再改排布，"
                                 "避免旧场次和新场次并存。")

        periods = self.periods_for_day(self.sel_date)
        period = next((p for p in periods if p.get("id") == period_id), None)
        if period is None:
            raise ValueError("没有时段 %s（%s 的时段：%s）"
                             % (period_id, self.sel_date.isoformat(),
                                "、".join(p.get("id", "?") for p in periods) or "无"))
        mode = ("clear" if clearing else
                "remove" if not durations else "set")
        # 纯计算先验证；这里失败 = 零副作用（plan_period 在 commit_layout 里跑）。
        result = self.commit_layout(
            self.sel_date, period,
            [] if clearing else (durations or []),
            source="auto" if clearing else "manual",
            mode=mode, book=False, announce=True)
        if not result["ok"]:
            return False

        if not self.offline:
            # 新排布里执行区的场次需要立刻补订 —— 统一走 book()（唯一预约出口）。
            self.book(self.sel_date, why="改完排布，要把新场次订上")
            self.rebuild_rows()
            self.refresh(quiet=True)
        return True

    def cmd_session(self, args):
        """session [--permanent] HH:MM LEN | HH:MM-HH:MM | T1 50 50

        - 默认：**只加在这一天**（一次性；存 temp/session-extras.json，不写进日历）。
          起点–终点的写法（`session 11:15-12:05`）会推出时长（这里是 50）。
        - `--permanent`：写进日历（temp/session-rules.json 里一条**按星期几重复**的
          长期规则）→ 以后每个这个星期几都会有这一场。
        - 第一个 token 是 T1 这种时段 ID 时，等同于 period T1 50 50（改排布）。

        创建零阻力 —— 有阻力的是**取消**。下单本身不在这里实现：薄封装
        keep_session.py check，业务逻辑一份就够。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        permanent = False
        rest = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "--perm", "permanent"):
                permanent = True
            else:
                rest.append(tok)
        if rest and re.match(r"^[Tt]\d+$", rest[0]):
            return self.cmd_period_layout(rest)

        hhmm, dur = parse_session_spec(rest)     # 不合法会抛 ValueError（给人话提示）
        day = self.sel_date
        weekday = sch.weekday_of_day(day)
        if permanent:
            rec = sp.add_rule(weekday, hhmm, dur)
            self.say("✅ 已写进日历（按星期几重复）：每个%s %s 一场 %dmin"
                     % (sch.cn_weekday_of_day(day), rec["start"], rec["duration"]), "ok")
            self.say("   长期规则存在 %s（sync/populate 之后仍然在）。"
                     % sp.rules_path(), "info")
            row_token = weekday
        else:
            iso, rec = sp.add_extra(day, hhmm, dur)
            self.say("✅ 只加在 %s 这一天：%s %dmin（一次性，不进日历）"
                     % (iso, rec["start"], rec["duration"]), "ok")
            self.say("   想让它每周都出现，用 session --permanent %s %d。"
                     % (rec["start"], rec["duration"]), "info")
            row_token = iso

        # 手动加一场 = 我就要这一场：撤掉这一场上的「永不重订」判决，
        # 否则认过罪的时刻永远补不回来（daemon 在 due 之前就把它滤掉了）。
        if ks.clear_abandoned([sch.key_of(day, hhmm)]):
            self.say("撤销这条时刻的「永不重订」判决：重新进 daemon 的下单队列。", "ok")

        if self.rewrite_data_csv_from_plan():
            self.say("data.csv 已按下载层重算（含一次性 / 长期规则）。", "info")
        else:
            # 没有下载层（旧的手写 data.csv 模式）：只能直接加一行
            lineno = sch.add_row(None, row_token, hhmm, dur)
            self.reload_entries()
            self.say("✅ 契约表已写：data.csv 第 %d 行 %s,%s,%s"
                     % (lineno, row_token, hhmm, dur), "ok")

        got = sch.parse_hhmm(hhmm)
        start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        zone = self.effective_zone(sch.zone_name(start, datetime.datetime.now()), day)
        if zone == "draft":
            self.say("   它在草稿区（>24h 或 edit-tomorrow 放行）：这一轮不会订它，"
                     "等它滑进执行区会自动被吃。", "info")
        elif zone == "past":
            self.say("   ⚠ 这个时间已经过了：这一轮不会订（对下一个%s才生效）。"
                     % sch.cn_weekday_of_day(day), "warn")

        # 新加的这一场要立刻订上 —— 统一走 book()（唯一预约出口）。
        self.book(day, why="加了一场，要把它订上")
        self.refresh()
        return True

    def cmd_refresh(self, args):
        """``refresh [--check|-c]`` —— 重拉选中那天的 session；带 ``--check`` 就顺手对账。

        ``--check`` 做的是**只读外面世界、再把不对的掰回来**（需求第 6 条）：

        1. **该订没订上的 session**：执行区里契约有、API 没有的 → 让 daemon 补一轮
           （``keep_session.py check``，仍走 daemon 自己的重试上限/退避，不越权）。
        2. **该挂没挂上的 task**：本地记着「任务 8 挂在 11:00 那场」，但 Focusmate
           的 task 列表里那条 task 没有指向那场的 attachment（手动删过、
           或者上次只做了半截）→ 重新挂。**已完成的 task 不重挂**。
        3. **已完成但还没写回 OmniFocus 的 task**：补写「完成 + 截止时间」。
        4. **一场挤了两条任务、其中有 🔒 独占的**：那一场的任务**全部下架**并警告
           （见 :meth:`check_exclusive_sessions`；它排在最前面，先拆干净再谈重挂）。
        5. **那场当时还没订上、现在订上了的任务**：补挂（formatter 切出来的场次
           就落在这一条上，不然任务永远不会出现在 Focusmate 的 Task Manager 里）。
        6. 有问题的都修完之后再拉一次，表格显示的就是**修完之后**的样子。

        不带 ``--check`` 的 ``refresh`` **不在外面新造东西**（不订场、不在 Focusmate
        建 task、不改 data.csv），但它不是只管一行数：它把看板上每块组件的状态都
        重新对一遍账（见 :meth:`refresh_all`）——表格、任务完成状态、统计、收尾面板、
        各种缓存。其中「Focusmate 已经说完成了」的 task 会**顺手写回 OmniFocus**
        （幂等；这不是新决定，是同一条事实的第二个账本）。
        """
        toks = [str(tok).lower() for tok in (args or [])]
        want_check = any(tok in ("--check", "-c", "check", "查", "对账", "核对")
                         for tok in toks)
        pulled = self.refresh_all()
        if not want_check:
            return pulled
        checked = self.check_and_fix()
        return checked and pulled is not False

    def refresh_all(self):
        """``refresh`` 实作的**全量**那一半：把屏幕上**每块组件**的状态都对一遍。

        需求：「refresh 需要刷新这个界面每个组件的状态」。所以不再只是「重拉 session
        列表、重算表格」，而是一条命令把看板上每一块拆开重新对账（**不新造东西**：
        不订场、不挂任务、不改 data.csv）：

        ==================  ============================================
        组件                刷什么
        ==================  ============================================
        契约表格            重读 data.csv / 日历下载层 / 时段 layout → 重拉 session
        任务面板            任务的**完成状态**（``GET /v1/tasks`` 拉，Focusmate 说完成的
                            顺手把 OmniFocus 也补成完成 —— 幂等）
        统计视图            已经加载过、且超过 TTL 的那份数字重算一次
        收尾面板            正在问的那一轮从任务库里重读一遍
        警告 / 静音         缓存作废（mute 缓存、快照缓存、日志尾巴）
        画帧缓存            作废 → 下一帧按新状态重排版
        ==================  ============================================

        失败不拦路：任何一块刷不动就记一句警告，其余照刷 —— refresh 是「再对一次账」，
        不是「全有或全无」。
        """
        project_sync = self._project_tasks_refreshable()
        self.jobs.begin(2 + (1 if project_sync else 0), title="刷新（refresh）")
        self.forget_caches()
        pulled = self.refresh()
        self.refresh_task_state()
        if project_sync:
            self._refresh_project_tasks()
        # 三块读互相独立：session / task 走浏览器（bridge 自己串行），项目对账走
        # osascript —— 队列按 source 分 lane，不同 lane 并行。所以这里**只等一次**：
        # 老写法按 snapshot → task → project 一等再等，把本来可以重叠的 3–6 秒
        # 也串起来了（用户反馈「网络拉取太慢」）。
        self.wait_net()
        # refresh 的排期流水线：formatter 先占时间，项目 plan 后校验。
        # 必须在刚拉回的 session 账上跑，否则会把已取消的场次当成资源。
        try:
            self.reconcile_schedule_pipeline(day=self.sel_date, quiet=True)
        except Exception as e:                     # noqa: BLE001 - 一块坏不挡整次刷新
            self.had_error = True
            self.say("排期流水线失败（界面其余部分照常刷新）：%s" % e, "warn")
        try:
            self.check_untyped_ops(announce_import=False)
        except Exception as e:                     # noqa: BLE001 - 检查失败不挡刷新
            self.say("检查 op 类型失败：%s" % e, "warn")
        self.refresh_stat_state()
        self.refresh_review_state()
        self.forget_caches()
        self.invalidate()
        return pulled

    def forget_caches(self):
        """把所有「上次算出来就先存着」的东西作废（refresh 的前后各来一次）。"""
        forget_mute_cache()
        try:
            self._snapshot_cache.clear()
        except Exception:                              # noqa: BLE001 - 没有就算了
            pass
        self._log_tail_memo = None
        try:
            self.reload_periods()
        except Exception as e:                         # noqa: BLE001 - 文件坏了也别崩
            self.say("日历下载层/时段 layout 读不了（表格照旧用上次的）：%s" % e, "warn")

    def refresh_task_state(self):
        """任务的完成状态对齐 Focusmate（``GET /v1/tasks``）。

        走 :meth:`sync_task_completions` 而不是 ``cmd_task_sync``：refresh 不能
        顺手在 Focusmate 上建 task（重复任务落地）。**完成写回 OmniFocus 照做** ——
        「Focusmate 勾掉了」是已经发生的事实，本地打 ✓ 的同时 OmniFocus 也得完成
        （幂等；没有要写的就一次子进程都不起），见 :meth:`sync_task_completions`。
        """
        if self.offline:
            return False
        try:
            return self.sync_task_completions(quiet=True)
        except Exception as e:                         # noqa: BLE001 - 刷不动就不刷
            self.had_error = True
            self.say("任务完成状态没能刷新：%s: %s" % (type(e).__name__, e), "warn")
            return False

    def refresh_stat_state(self):
        """统计视图那份数字：**加载过**且过期才重算（没看过就别替用户拉）。"""
        if self.offline or self.stat is None:
            return False
        if self.stat_at is not None:
            age = (datetime.datetime.now() - self.stat_at).total_seconds()
            if age < st.CACHE_TTL:
                return False
        return self.cmd_stat()

    def refresh_review_state(self):
        """收尾面板：正在问的那一轮从任务库重读（用户可能刚在外面改了状态）。"""
        review = self.review
        if review is None:
            return False
        try:
            review.reload()
        except Exception as e:                         # noqa: BLE001 - 收尾比刷新重要
            self.say("收尾面板没能重读任务（照旧继续问）：%s" % e, "warn")
            return False
        return True

    def check_and_fix(self):
        """把「该订没订上 / 该挂没挂上 / 该写回没写回」查一遍并修掉。

        这是 ``refresh --check`` 的实作。完成写回 OmniFocus 已经由 ``refresh_all``
        → :meth:`refresh_task_state` → :meth:`sync_task_completions` 顺手做掉了
        （幂等），这里只管 session 和 task 挂载这两件事。
        """
        if self.offline:
            self.say("refresh --check：离线模式下没有外部世界可查，只重画了表格。", "warn")
            return False
        self.say("refresh --check：对账 —— 该订的 session、该挂的 task、该写回的完成…", "cmd")
        fixed = 0
        # 独占先查：一场挤了两条任务时**先**拆掉，否则后面的「重挂」会把它又挂回去。
        fixed += self.check_exclusive_sessions()
        fixed += self.check_bookings()
        fixed += self.check_task_attachments()
        # 保险起见再扫一遍「本地已完成但还没写回」的：上面的重挂可能刚把一条任务
        # 的 Focusmate 链接修好，而 writeback 是幂等的（写过的记在 writeback 上）。
        fixed += len(self.writeback_completions())
        # 修完再拉一次：表格要显示的是**修完**的样子，不是修之前的
        # （走全量：刚刚重挂过 task，任务面板/画帧缓存也得跟着换）
        if fixed:
            try:
                self.refresh_all()
            except Exception:                       # noqa: BLE001 - 拉不到就算了
                pass
        if fixed:
            self.say("✅ 对账完成：处理了 %d 处（详细见上面几条）。" % fixed, "ok")
            # 说完「修了」还得看一眼**修完**还剩什么 —— 补订是 daemon 的事，
            # 它这一轮可能没补上（上限/退避/还没到点），不能报喜不报忧。
            still = [r for r in self.rows if r.get("planned") and r.get("id") is None
                     and r.get("zone") == "execute"]
            if still:
                self.say("   还有 %d 场没订上：%s —— daemon 下一轮还会试（或 force-check）。"
                         % (len(still), "、".join(str(r.get("startLabel")) for r in still[:5])),
                         "warn")
        else:
            self.say("✅ 对账完成：没有发现要修的（session 都订上了、任务都挂对了）。", "ok")
        return True

    def check_exclusive_sessions(self):
        """``refresh --check`` 的独占对账：一场挤了两条任务就**全部下架** + 警告。

        规则（用户原话）：「如果不对就直接那个 session 的任务全部下下来 + 警告用户」。
        判据只有一条 —— **同一场**里有 ≥2 条**未收工**的任务，而且其中有 🔒 独占的。
        「同一场」优先按 ``session.focusmate_session_id`` 认（那才是 Focusmate 眼里
        的一场：两条任务挂在同一个 meetingId 上就是挤在一起，哪怕本地记的起点不同，
        切割出来的 11:30 和原来那场 11:00 就可能是同一个 meetingId）；没订上（没有
        id）才退回「同一天 + 同一时刻」。下架走 :meth:`_task_detach`（先删 Focusmate
        那条 task 再断本地），所以 Focusmate 上也不会留下两条打架的 task。

        返回处理了几条任务。
        """
        day = self.sel_date.isoformat()
        groups = {}
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if (sess.get("date") or "") != day:
                continue
            if not self.task_store.is_open(task):
                continue
            # 「一场 session」的身份：有 Focusmate session id 就按它认（那才是最硬的
            # 身份 —— 两条任务挂在同一个 meetingId 上就是挤在同一场，哪怕本地记的
            # 起点不一样）；没订上就退回「日期 + 起点」。
            key = str(sess.get("focusmate_session_id") or "") or \
                "start:%s" % (sess.get("start") or "")
            groups.setdefault(key, []).append(task)
        fixed = 0
        for key in sorted(groups):
            tasks = groups[key]
            if len(tasks) < 2:
                continue
            exclusive = [t for t in tasks if th.is_exclusive(t)]
            if not exclusive:
                continue
            where = "、".join(sorted({str((t.get("session") or {}).get("start") or "?")
                                     for t in tasks}))
            names = "、".join("%s「%s」" % (self.task_label(t),
                                          self.task_store.focusmate_title(t, 20) or "?")
                              for t in tasks)
            self.say("⚠ %s %s 那场挤了 %d 条任务，其中 🔒 独占的只能独占一场 → 全部下架：%s"
                     % (day, where, len(tasks), names), "warn")
            for task in tasks:
                try:
                    self._task_detach(task, reason="exclusive-conflict")
                    fixed += 1
                except Exception as e:              # noqa: BLE001 - 一条失败不挡其余
                    self.had_error = True
                    self.say("   ❌ 下架 %s 失败：%s" % (self.task_label(task), e), "error")
        if fixed:
            self.say("   下架后它们还在任务表里（未挂 session，🟦）。重新排："
                     "`task <序号> -f end`，或 `task <序号> -a <session>` 自己挑一场。",
                     "warn")
        return fixed

    # -- 预约：整条流水线的唯一出口 ------------------------------------
    def book(self, day=None, *, why="", force=False, wait=None, announce=True):
        """**唯一的预约出口**：让 daemon 跑一轮 check，把执行区的场次订满。

        以前这件事散在四条路上各写一遍（``period`` / ``session`` /
        ``refresh --check`` / ``force-check``），每条自己 ``_release_browser()``
        再拼子进程参数；而 ``plan`` 完成时**一条都没有** —— 排好了却没人订，
        只能等 daemon 下一轮。现在所有「改了 data.csv」的写入者末尾都调这里，
        一条流水线一个出口。

        下单逻辑不在这里重写：永远是薄封装 ``keep_session.py check``
        （业务逻辑一份就够，和 schedule.py / download_calendar.py 同一个原则）。

        ``force=True`` → ``check --force``（忽略重试上限/退避）。
        ``wait`` 默认：同步模式（测试 / ``--exec``）必然等结果；异步模式不等，
        daemon 会补，绝不冻住提示符。

        **offline 守卫留在调用方**（``cmd_period_layout`` 本来就是
        ``if not self.offline`` 才订）：这里只管「把动作统一」，不替各条路
        重新定义离线语义。唯一的全局守卫是 ``_suppress_book``（refresh 的对账轮
        只对账、不新造外部动作）。
        """
        if getattr(self, "_suppress_book", False):
            return None
        if getattr(self, "_booking", False):
            return None                       # 已经在订了，别嵌套
        self._booking = True
        try:
            self._release_browser(why or "daemon 下单要独占同一个 Chromium profile")
            argv = ["check"] + (["--force"] if force else [])
            label = "预约（keep_session %s）" % " ".join(argv)
            if announce:
                self.say("立刻触发一轮 %s…" % " ".join(["check"] + argv[1:]), "cmd")
            if self.jobs.sync:
                rc, out, err = get_warden_global(
                    "run_keep_session", run_keep_session)(*argv, timeout=600)
                for line in (out or "").splitlines()[-20:]:
                    self.say(line)
                if rc != 0:
                    self.had_error = True
                    self.say("❌ check 退出码 %d%s（契约已落盘，daemon 后续还会补）"
                             % (rc, ("：" + err) if err else ""), "error")
                    return False
                if announce:
                    self.say("✅ check 跑完。", "ok")
                return True
            do_wait = True if wait is None else bool(wait)
            job = self.op_keep_session(argv, label, wait=do_wait)
            if not do_wait:
                return True
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                self.had_error = True
                self.say("❌ check 没跑成%s（契约已落盘，daemon 下一轮还会补）"
                         % (("：" + outcome.error) if outcome else ""), "error")
                return False
            return True
        finally:
            self._booking = False

    def check_bookings(self):
        """该订没订上的 session → 让 daemon 补一轮。返回修了几场。"""
        missing = [r for r in self.rows
                   if r.get("planned") and r.get("id") is None
                   and r.get("zone") == "execute"]
        if not missing:
            return 0
        names = "、".join("#%d(%s)" % (self.rows.index(r) + 1, r.get("startLabel"))
                         for r in missing[:5])
        self.say("⚠ %d 场该订却没订上：%s —— 叫 daemon 补一轮（check）…"
                 % (len(missing), names), "warn")
        ok = self.book(self.sel_date, why="对账：补订缺的场次",
                       wait=True, announce=False)
        if ok is False:
            return 0
        return len(missing)

    def check_task_attachments(self):
        """任务真的挂在那场 session 上了吗？挂漏/挂错的重新挂一遍。返回修了几条。

        判据只有一条，而且来自 Focusmate 自己：``GET /v1/tasks`` 里那条 task 的
        ``attachments`` 里有没有这一场的 session id。本地账本说「挂了」不算数 ——
        手动在 Task Manager 删过、或上次调 API 只做了半截，账本都会照旧留着。

        两类都要修：

        1. **本地说挂着、Focusmate 说没挂上** → 重挂；
        2. **本地挂着「那场当时还没订上」**（formatter 切出来的场次就是这样：
           ``assign_task_to`` 只会记本地）→ 现在场次订上了就**补挂**，否则那条
           任务永远不会出现在 Focusmate 的 Task Manager 里。
        """
        remote = self._remote_tasks()
        if remote is None:
            self.say("⚠ 拉不到 GET /v1/tasks（bridge 不在？），任务挂载这一项跳过。", "warn")
            return 0
        day = self.sel_date.isoformat()
        bad, late = [], []
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if (sess.get("date") or "") != day:
                continue
            if not self.task_store.is_open(task):
                continue
            if self._fm_pending(task):
                continue                       # 乐观挂载还在后台跑，别重复挂
            verdict = self._attachment_verdict(task, remote)
            if verdict:
                bad.append((task, verdict))
                continue
            if sess.get("focusmate_session_id"):
                continue
            fm = task.get("focusmate") or {}
            if fm.get("task_id") or fm.get("id"):
                continue                       # 已经有 Focusmate task，别再造一条
            idx, row = self._row_for_session(sess)
            if row is None or row.get("id") is None:
                continue
            late.append((task, idx))
        fixed = 0
        for task, idx in late:
            sess = task.get("session") or {}
            self.say("⚠ 任务 %s「%s」挂的 %s %s 那场已经订上了，但 Focusmate 里还没有"
                     "这条 task —— 补挂…"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 30),
                        sess.get("date") or "?", sess.get("start") or "?"), "warn")
            try:
                self._assign_task(task, str(idx), refresh=False)
                fixed += 1
            except Exception as e:                  # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                self.say("   ❌ 补挂失败：%s" % e, "error")
        for task, verdict in bad:
            sess = task.get("session") or {}
            self.say("⚠ 任务 %s「%s」%s（本地记着挂在 %s %s）—— 重挂…"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 30),
                        verdict,
                        sess.get("date") or "?", sess.get("start") or "?"), "warn")
            idx, row = self._row_for_session(sess)
            if row is None or row.get("id") is None:
                self.say("   那一场现在不在表格里（或还没订上），先 refresh --check 等它订上。",
                         "warn")
                continue
            try:
                self._assign_task(task, str(idx), refresh=False)
                fixed += 1
            except Exception as e:                  # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                self.say("   ❌ 重挂失败：%s" % e, "error")
        self.rebuild_rows()
        return fixed

    @staticmethod
    def _attachment_verdict(task, remote):
        """本地说挂着、Focusmate 说不算 → 一句人话；没问题返回 ``""``。"""
        sess = task.get("session") or {}
        want = str(sess.get("focusmate_session_id") or "")
        if not want:
            # 那一场还没有 Focusmate session id（没订上/快照）—— 无从查起
            return ""
        fm = task.get("focusmate") or {}
        tid = str(fm.get("task_id") or fm.get("id") or "")
        if not tid:
            return "没有 Focusmate task id"
        row = remote.get(tid)
        if row is None:
            return "Focusmate 那边这条 task 已经不在了"
        # 已完成/已放弃的 task 不该触发「重挂」：任务已经收工，哪怕 attachment
        # 对不上（例如 Focusmate 后续改了挂载信息），再建一条也只会多一个幽灵。
        if row.get("completed") or row.get("completedAt") or row.get("completed_at"):
            return ""
        ids = [str(a.get("externalId") or a.get("id") or "")
               for a in (row.get("attachments") or [])]
        if want not in ids:
            return "Focusmate 那条 task 没挂在这一场上"
        return ""

    def _remote_tasks(self):
        """``GET /v1/tasks`` → ``{task_id: 行}``；拉不到返回 None（不是空 dict）。"""
        try:
            res = self.bridge.call("tasks", timeout=180)
        except BridgeError as e:
            self.say("拉 GET /v1/tasks 失败：%s" % e, "warn")
            return None
        rows = res.get("tasks") if isinstance(res, dict) else res
        return {str(r.get("id")): r for r in (rows or [])
                if isinstance(r, dict) and r.get("id") is not None}

    def rewrite_data_csv_from_plan(self):
        """下载层 + layout + 一次性 + 长期规则 → 重算 data.csv。

        没有下载层（说明这台机器还在用"手写 data.csv"那套）就返回 False，
        调用方退回 schedule.add_row —— 绝不拿空计划把人家手写的契约冲掉。
        """
        download = sp.load_download()
        if not download.get("periods"):
            return False
        layouts = sp.load_layouts()
        csv = sp.render_data_csv(download, layouts, date_specific=True)
        before = [ln.strip() for ln in sch.read_text().splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
        after = set(ln.strip() for ln in csv.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#"))
        sch.backup()
        sch._write(sch.DATA_CSV, csv)
        # 下载层/排布/一次性/长期规则之外的旧行会被冲掉 —— 记下来，下次画框报警。
        self.csv_dropped = [ln for ln in before if ln not in after]
        self.reload_entries()
        self.reload_periods()
        if self.csv_dropped:
            self.say("⚠ 重算 data.csv 冲掉了 %d 行（下载层/layout/一次性/长期规则里没有的）："
                     "%s%s；旧文件备份在 %s"
                     % (len(self.csv_dropped), "、".join(self.csv_dropped[:3]),
                        "…" if len(self.csv_dropped) > 3 else "", sch.BACKUP_CSV), "warn")
        return True

    def cmd_force_check(self):
        """force-check —— 忽略重试上限/退避，强制对执行区里所有还没订上的 session
        各试一次重订。

        普通 `check` 只补订没到上限、且不在退避中的场次；有的 slot 失败 3 次后被标成
        failed 就再也不碰了。force-check 是手动扳轨：已经 failed、正在退避、attempts
        已爆表的，全都再试一次。已放弃（判决 abandoned）和已订上的不碰。
        下单逻辑不在这里重写：薄封装 `keep_session.py check --force`。
        """
        self.say("force-check：忽略重试上限/退避，对执行区所有没订上的 session 各试一次…", "cmd")
        self.book(self.sel_date, why="force-check：强制重订", force=True,
                  wait=True, announce=False)
        self.refresh()
        return True

    def cmd_sync(self, args):
        """sync [download_calendar 的参数] —— 调 download_calendar.py populate，把日历
        重新切成 data.csv（会覆盖 data.csv；旧文件备份到 temp/data.csv.bak）。

        这里**不重写**日历逻辑：薄封装 download_calendar.py，和 schedule.py 一样是唯一真源。
        `sync --days 3` 之类的参数原样透传给 populate。

        **两步、一个标题**：`sync（同步日历）` 下面先是 `[calendar] 日历 download`，
        再是 `[calendar] 日历 populate` —— 第二步挂在第一步的成功回调里，
        因为「下载成功才谈得上切 csv」。
        """
        argv = [str(a) for a in args]
        self.jobs.begin(2, title="同步日历（sync）")
        self.say("sync：先 download（工作/空闲 → 中间层），再 populate "
                 "（中间层 + 时段 layout → data.csv）…", "cmd")
        first = self.op_calendar(["download"] + argv, "日历 download",
                                 on_ok=lambda payload: self._sync_populate(argv))
        if self.jobs.sync:
            outcome = first.outcome
            return bool(outcome is not None and outcome.ok)
        return True

    def _sync_populate(self, argv):
        """sync 的第二步：download 成功了才切 csv（同一批里的第 2 步）。"""
        self.op_calendar(["populate", "--from-download"] + list(argv), "日历 populate")

    def _release_browser(self, why):
        """让 bridge 放开浏览器（daemon 下单用同一个 profile，会撞锁）。"""
        try:
            up, browser, _ = self.bridge.browser_state()
        except BridgeError:
            return False
        if not up or not browser:
            return False
        self.say("先让 bridge 放开浏览器（%s）…" % why, "info")
        try:
            self.bridge.call("release", timeout=60)
            return True
        except BridgeError as e:
            self.say("释放浏览器失败（不影响，但可能撞锁）：%s" % e, "warn")
            return False

    def cmd_start(self):
        self._release_browser("daemon 下单要独占同一个 Chromium profile")
        self.jobs.begin(1, title="启动 daemon（start）")
        self.say("启动 daemon…（在后台起，结果进 Recent）", "cmd")
        job = self.op_keep_session(["daemon", "start"], "起 daemon")
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True

    def cmd_stop(self):
        """停用 daemon —— 和取消 session 共用同一道宣言闸门（密码系统已删掉）。

        daemon 是你今天 session 的保命绳：停掉之后，执行区里没人补订、
        罪也没人记。所以停用要付一样的代价。
        """
        self.emit(Panel(
            "停掉 daemon = 执行区里剩下的场次没人补订、也没人记账。\n"
            "要停就得和取消 session 一样，过 The Walk of Shame。",
            title="[bold yellow]停用 daemon[/]",
            border_style="yellow", box=box.ROUNDED,
        ))
        if not self.require_declaration("停用 daemon"):
            self.say("没认罪，daemon 还活着。", "warn")
            return False
        self.jobs.begin(1, title="停用 daemon（stop）")
        self.say("停 daemon…（在后台停，结果进 Recent）", "cmd")
        job = self.op_keep_session(["daemon", "stop"], "停 daemon")
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True
