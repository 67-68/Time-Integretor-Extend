"""warden_core.review_mixin —— 收尾（postsession）、inspect-due 查账、nostart 追问与打断栏（interrogate）派发。"""
import datetime
import re
import threading
import time

from .bridge import BridgeError
from .common import (
    Panel,
    SRC_KEEP,
    Text,
    box,
    of,
    pj,
    psv,
    rl,
    sch,
    sp,
    ts,
)
from .views import ContractView


class ReviewMixin(object):
    # -- 收尾用的几个动作（收尾和命令共用同一套，不写两遍）--------------

    def _snapshot_rows(self, iso):
        """某一天的 Focusmate 场次快照（带缓存；读不到就是空列表，不抛）。

        **绝不在提示符线程上等 bridge。** 这个方法会被 formatter / 剪断这类
        用户命令间接调用；以前这里 ``bridge.call("snapshot")`` 是同步的，浏览器
        冷启动时一条 ``task 5 -ac`` 能整整数十秒没反应。现在分三档：

        * 当前选中的那一天：直接用已经拉回来的 ``self.rows``（有 session id）；
        * 别的天：只认 ``_snapshot_cache`` 里已有的快照；
        * 只有后台线程（例如收尾核对）或 sync 模式（测试 / ``--exec``）才真正去拉。

        交互式别的天没缓存时返回空列表。formatter 会退化成「先只记本地绑定」，
        等那一场真的订上、refresh/check 再补 Focusmate task —— 这比卡死 UI 好。
        """
        cache = getattr(self, "_snapshot_cache", None)
        if cache is None:
            cache = self._snapshot_cache = {}
        rows = cache.get(iso)
        if rows is not None:
            return rows
        # 当前表格就是最新的一份当天快照，不必再问一次 bridge。
        try:
            if iso == self.sel_date.isoformat() and self.rows_source == "bridge":
                rows = [dict(row) for row in self.rows
                        if row.get("date") == iso]
                if not self.jobs.sync:
                    cache[iso] = rows
                    return rows
                # sync 模式（测试 / --exec）可能只关心「某一列还没在本地表格
                # 里拿到 id」；按老语义再问一次 bridge，脚本才能拿到真 session。
        except Exception:                          # noqa: BLE001 - 日期/行坏了就走缓存
            pass
        # worker 线程可以拉；交互式主线程只读缓存，绝不让一次外部读卡住输入。
        # sync 模式（测试 / --exec）本来就是「等结果」的语义，仍按老行为同步拉，
        # 保证脚本里的 formatter 能立刻拿到未来那天的 session id。
        if (self.jobs.sync
                or threading.current_thread() is not threading.main_thread()):
            try:
                res = self.bridge.call("snapshot", timeout=180, date=iso)
                rows = res.get("sessions") or []
            except BridgeError:
                rows = []
            cache[iso] = rows
            return rows
        return []

    def session_info_for(self, day, slot):
        """`(date, slot)` → 一场 session 的完整信息（含 Focusmate id，尽力而为）。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        info = {
            "date": iso,
            "start": slot.get("start"),
            "duration": slot.get("duration"),
            "end": None,
            "focusmate_session_id": None,
            "startMs": None,
        }
        try:
            begin = datetime.datetime.strptime(iso, "%Y-%m-%d").replace(
                hour=int(slot["hour"]), minute=int(slot["minute"]))
            finish = begin + datetime.timedelta(minutes=int(slot.get("duration") or 0))
            info["end"] = finish.strftime("%H:%M")
        except (KeyError, ValueError):
            pass
        if self.offline:
            return info
        for row in self._snapshot_rows(iso):
            if row.get("startLabel") == slot.get("start") and row.get("id"):
                info["focusmate_session_id"] = row["id"]
                info["startMs"] = row.get("startMs")
                info["end"] = row.get("endLabel") or info["end"]
                break
        return info

    def assign_task_to(self, task, session):
        """把任务挂到一场 session：Focusmate 建 task（能建就建）+ 本地落账。返回一句人话。

        这一场已经有 🔒 独占任务（或这条自己独占、而场里已经有人）时**不挂**，
        只返回一句警告 —— 「一场 session 只能有一条任务」这条规矩在这里也必须成立，
        不能因为调用方是 formatter / 收尾流程就绕过去。
        """
        date_iso = session.get("date") or self.sel_date.isoformat()
        blockers = self._exclusive_blockers(date_iso, session.get("start"), task)
        if blockers:
            names = "、".join(self.task_label(t) for t in blockers)
            return ("⚠ %s %s 那场已经有 🔒 独占任务 %s，没挂上去（换一场或先放开独占）"
                    % (date_iso, session.get("start") or "?", names))
        sid = session.get("focusmate_session_id") or session.get("id")
        start_ms = session.get("startMs")
        title = self.task_store.focusmate_title(task)
        notes = self.task_store.notes(task) or ""
        payload = {
            "index": session.get("index"),
            "date": date_iso,
            "start": session.get("start"),
            "end": session.get("end"),
            "duration": session.get("duration"),
            "focusmate_session_id": sid,
        }
        if sid and start_ms is not None and not self.offline:
            # formatter / 收尾也会走到这里：同样走「本地先挂、Focusmate 丢后台」，
            # 不在提示符线程上等 add_task。同步模式（测试 / --exec）仍然当场跑完。
            try:
                self._op_task_assign(
                    task, payload, session.get("index") or 0, date_iso,
                    title, notes, refresh=False, start_ms=start_ms)
                return ("Focusmate task 后台挂（%s %s）"
                        % (date_iso, session.get("start") or "?"))
            except (BridgeError, ValueError) as e:
                return "Focusmate 没建上：%s（只记了本地）" % e
        if payload.get("focusmate_session_id"):
            # session id 有但缺少 startMs（只可能是缓存/旧数据）：先记本地，
            # 等 refresh/check 从 Focusmate 真数据里补挂。
            payload["focusmate_session_id"] = None
        self.task_store.assign(task["id"], payload)
        return "这场还没有 Focusmate session id（没订上），只记了本地"

    def next_session_after(self, task, max_days=2):
        """这条任务「下一场」放哪儿：不早于现在、且在原场次之后最近的那个 slot。

        收尾里的 E 和 ``inspect-due`` 的 G 都用它。以前只从任务原场次的起点往后
        找，一条几天前过期、但当天后面还有 slot 的任务会被排回**已经过去**的
        那一天；``inspect-due`` 专门处理过期任务，所以下界必须取「原场次」和
        「现在」的较大者。
        """
        sess = task.get("session") or {}
        now = self.review_now or datetime.datetime.now()
        day = now.date()
        start = now.strftime("%H:%M")
        if sess.get("date") and sess.get("start"):
            try:
                sess_day = datetime.date.fromisoformat(str(sess["date"]))
                sess_start = str(sess["start"])
                sess_dt = datetime.datetime.combine(
                    sess_day, datetime.time.fromisoformat(sess_start))
                if sess_dt > now:
                    day, start = sess_day, sess_start
            except (ValueError, TypeError):
                pass
        entries = self.reload_entries()
        for offset in range(0, int(max_days) + 1):
            probe = day + datetime.timedelta(days=offset)
            try:
                slots = sch.day_slots(entries, probe)
            except Exception:                    # noqa: BLE001 - 排布坏了就当这天没有
                continue
            for slot in slots:
                if probe == day and str(slot.get("start") or "") <= start:
                    continue
                return self.session_info_for(probe, slot)
        return None

    # -- 收尾（postsession）：session 结束 15 秒后的强制对账 -----------------

    def review_hooks(self):
        """收尾流程要「碰外面世界」的那几个口子（其余全在 postsession.Review 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            # 收尾面板上写的编号必须是**面板序号**（和用户正看着的那块表格一个数）
            "label": lambda task: self.task_label(task),
            "drop_omnifocus": lambda task: self.omni_drop(task),
            "complete_focusmate": lambda task: self.fm_complete(task),
            "delete_focusmate_task": lambda task: self.fm_delete_task(task),
            "complete_omnifocus": lambda task, minutes=None: (
                "；".join(x for x in (
                    self.omni_complete(task, minutes=minutes),
                    self._project_child_completed(task, writeback=True)) if x)),
            "next_session": lambda task: self.next_session_after(task),
            "assign_focusmate": lambda task, sess: self.assign_task_to(task, sess),
            "due_of": lambda task: self.omni_due_of(task),
            "set_count": lambda task, cap: self.task_store.set_count(task["id"], cap),
            "set_count_plan": lambda task, invested, remaining: (
                self.task_store.set_count_plan(task["id"], invested, remaining)),
        }

    def sync_completion_state(self, entry=None):
        """核对完成状态：Focusmate 的 completedAt + OmniFocus 的 completed。

        走 bridge / 子进程，**可能要几秒**，所以自动收尾从后台线程调它
        （见 :meth:`_review_worker`），手动 `review` 就直接调。
        返回 ``{本地任务 id: omni 行}``。
        """
        if not self.offline:
            started = time.monotonic()
            rl.log_event("focusmate", "task_pull_start", source="sync_completion_state")
            try:
                res = self.bridge.call("tasks", timeout=180)
                remote = res.get("tasks") if isinstance(res, dict) else res
                before = len(self.task_store.load_archive().get("records") or [])
                n, matched = self.task_store.reconcile(remote or [])
                self.task_sync_at = time.time()
                summary = self._focusmate_task_summary(remote or [])
                summary.update({
                    "source": "sync_completion_state",
                    "matched": matched,
                    "archived": n,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                })
                rl.log_event("focusmate", "task_pull_result", **summary)
                if n:
                    self.announce_completions(before)
            except (BridgeError, ValueError) as e:
                rl.log_event("focusmate", "task_pull_error", source="sync_completion_state",
                             elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                             error="%s: %s" % (type(e).__name__, e))
                self.say("核对 Focusmate 完成状态失败：%s" % e, "warn")
        rows = self.sync_omnifocus_status()
        # OmniFocus 那边已经勾了完成的，本地也标完成（然后走一遍写回，把截止时间对齐）
        for task_id, row in rows.items():
            if not row.get("completed"):
                continue
            try:
                task = self.task_store.get(task_id)
            except ValueError:
                continue
            if ts.TaskStore.is_repeat_template(task):
                continue
            if self.task_store.is_open(task):
                self.task_store.set_status(task_id, ts.STATUS_COMPLETED,
                                           at=row.get("completed_at"),
                                           reason="omnifocus-completed")
                note_sub = self._project_child_completed(task, writeback=False)
                self.say("✓ 任务 %s 在 OmniFocus 里已完成%s"
                         % (self.task_label(task),
                            ("；" + note_sub) if note_sub else ""), "ok")
        self.writeback_completions()
        return rows

    def begin_review(self, entry, sync=True, credit=True):
        """进入一场收尾：核对完成状态 → 挑出没做完的任务 → 建 Review。

        ``credit=False``：**不记这一场的 count**（「到点没开始」那场一分钟都没干，
        不能往任务上记 count —— 见 :meth:`credit_session_counts`）。
        """
        if self.nostart is not None:
            # 一次只问一件事：「没开始」那一问还开着就先收掉（记成 deferred），
            # 否则两套提示抢同一行输入。
            self.close_nostart(note="review-started")
        entry = dict(entry or {})
        day_iso = entry.get("date") or self.sel_date.isoformat()
        start = entry.get("start") or ""
        key = entry.get("key") or psv.session_key(day_iso, start)
        entry["key"] = key
        try:
            self.focus_day(day_iso)
        except Exception:                        # noqa: BLE001 - 切不过去也照样收尾
            pass
        if sync:
            self.sync_completion_state(entry)
        try:
            day = datetime.date.fromisoformat(day_iso)
        except ValueError:
            day = self.sel_date
        tasks = self.task_store.tasks_for_session(day, start)
        opened = [t for t in tasks if self.task_store.is_open(t)]
        # 先把这一场该给的 count 记进账（幂等），**但不完成** —— 够数的那条留给
        # 收尾去问「再要 count 还是直接完成」（见 postsession.COUNT_OPTION）。
        counts = self.credit_session_counts(entry, opened) if credit else {}
        self.rebuild_rows()
        if not opened:
            self.say("⏱ %s %s 那场没有未完成的任务（%d 条记录都已收）—— 这场翻篇。"
                     % (day_iso, start or "?", len(tasks)), "ok")
            psv.mark_done(entry, note="nothing-open")
            self.review = None
            return None
        self.review = psv.Review(self.task_store, entry, opened,
                                 hooks=self.review_hooks(), counts=counts)
        self.say("⏱ %s 收尾：%d 条没做完的任务，一条一条来（A/B/C/D/E）。"
                 % (self.review.headline(), len(opened)), "cmd")
        return self.review

    def credit_session_counts(self, entry, tasks):
        """这一场该给的 count 记进每条任务（**幂等**），返回 ``{task_id: 状态}``。

        用户的需求：``count`` 是「这件事要几个 25 分钟」，一场 session 干完就该往
        前推一格（时长 / 25，往上取整）。但**推完不自动完成** —— 够数只是一次
        「到点了」的预报，收尾里问过人之后才落成「完成」或「再加 count」。

        记账的 key 是 ``postsession.session_key``（``2026-11-03T11:00``），写进任务的
        ``count_credits`` 里：同一场收尾重开多少次都只加一次。
        """
        delta = ts.minutes_to_count(psv.session_minutes(entry)) or 0
        key = str((entry or {}).get("key") or "?")
        out = {}
        for task in tasks or []:
            if ts.TaskStore.is_repeat_template(task):
                continue
            cap = ts.task_count(task)
            if cap is None:
                continue                     # 没设 count 的任务：这一场不记账
            credited = None
            try:
                credited = self.task_store.credit_count(task["id"], key, delta)
            except (ValueError, TypeError) as e:      # noqa: BLE001 - 记账失败不该挡住收尾
                self.say("⚠ 给任务 %s 记 count 失败：%s"
                         % (self.task_label(task), e), "warn")
            fresh = self.task_store.get(task["id"])
            invested = ts.task_count_invested(fresh)
            state = {"cap": cap, "invested": invested, "delta": delta,
                     "credited": credited is not None,
                     "maxed": invested >= cap}
            out[fresh["id"]] = state
            if credited is not None:
                self.say("🧮 任务 %s 记了 %d 个 count：%d/%d%s"
                         % (self.task_label(fresh), delta, invested, cap,
                            "（够数了，收尾时问你：再要 count 还是直接完成）"
                            if state["maxed"] else ""), "ok")
        return out

    def begin_inspect_due(self, tasks, label=None, sync=True, within_minutes=0):
        """``inspect-due``：把「过期还没做完」的任务拉进同一套收尾状态机。

        和 session 收尾唯一的区别：**没有那一场 session 可以「翻篇」**，
        所以结束时不写 postsession 状态文件（见 :meth:`close_review`）。
        """
        entry = {"kind": "due", "key": "due",
                 "label": label or "过期未完成任务",
                 "within_minutes": int(within_minutes or 0),
                 "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        if sync:
            self.sync_completion_state(entry)
        opened = []
        for task in tasks or []:
            try:
                current = self.task_store.get(task["id"])
            except ValueError:
                continue
            if self.task_store.is_open(current):
                opened.append(current)
        self.rebuild_rows()
        if not opened:
            self.say("🔍 没有到期还没做完的任务 —— 账是干净的。", "ok")
            self.review = None
            return None
        self.review = psv.Review(self.task_store, entry, opened, hooks=self.review_hooks())
        self.say("🔍 %s：%d 条要过一遍（A/B/C/D/E，G 延长排期，s 跳过，q 结束）。"
                 % (self.review.headline(), len(opened)), "cmd")
        return self.review

    def cmd_inspect_due(self, args=None, now=None):
        """``inspect-due [分钟] [--all]`` —— 主动 inspect 没做完、已经到期的任务。

        和「session 结束自动催」共用同一套状态机（A 延期 / B 放弃 / C 重议 /
        D 缺漏完成 / E 快捷推后），区别只在**谁触发**：

            inspect-due           所有已经过了截止时间、还没做完的任务
            inspect-due 60        未来 60 分钟内要到的也算（提前看一眼）
            inspect-due --all     连没有截止时间的一起（「主动看没完成的任务」）
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        include_no_due = False
        within = 0
        for tok in toks:
            low = tok.lower()
            if low in ("--all", "all", "全部"):
                include_no_due = True
                continue
            if low in ("--no-due", "nodue"):
                include_no_due = True
                continue
            try:
                within = max(0, int(tok))
            except ValueError:
                raise ValueError("inspect-due 的参数要么是分钟数，要么是 --all；"
                                 "你给的是 %r" % tok) from None
        candidates = psv.due_candidates(self.task_store, now=now, within_minutes=within,
                                        include_no_due=include_no_due)
        label = "过期未完成任务"
        if include_no_due:
            label = "未完成任务（含没设截止时间的）"
        elif within:
            label = "截止时间在未来 %d 分钟内的未完成任务" % within
        if not candidates:
            self.say("🔍 没有%s —— 账是干净的。" % label, "ok")
            return None
        return self.begin_inspect_due(candidates, label=label, within_minutes=within)

    def cmd_review(self, args=None):
        """``review [HH:MM | 2026-10-27T11:00]`` —— 手动开始一场收尾。

        自动收尾靠 daemon（结束 + 15 秒）拉起；这条命令是给
        「刚才那场没催上 / 想补收一场」用的，走的是同一套流程。
        """
        wanted = str(args[0]).strip() if args else ""
        pending = psv.pending_sessions()
        entry = psv.pick(pending, wanted) if wanted else (pending[-1] if pending else None)
        if entry is None:
            # 手动收尾往回看一整天（自动催只看 3 小时，见 postsession.LOOKBACK_SEC）：
            # 「上午那场我忘了收」正是这条命令存在的理由。
            sessions = psv.ended_sessions(
                psv.slots_around(self.reload_entries(), days_back=1,
                                 actual=self.actual_slots()),
                lookback=36 * 3600)
            entry = psv.pick(sessions, wanted)
        if entry is None:
            self.say("没有可以收尾的场次%s（要看的是「已经结束」的那一场）。"
                     % (("：%r" % wanted) if wanted else ""), "warn")
            return None
        return self.begin_review(entry)

    def _review_worker(self, entry):
        """后台核对完成状态（bridge / osascript 都可能几秒，别卡着画帧）。"""
        try:
            self.sync_completion_state(entry)
        except Exception as e:                   # noqa: BLE001 - 收尾不能因为核对失败就不做
            self.say("核对完成状态出错（照常收尾）：%s: %s" % (type(e).__name__, e), "warn")
        finally:
            if self.review_check is not None:
                self.review_check["ready"] = True

    def _maybe_start_review(self):
        """每帧看一眼：daemon 说哪一场该收尾了，就接过来（**只在主线程动手**）。"""
        if self.review is not None:
            return
        if self.nostart is not None:
            return                              # 「没开始」那一问还开着：先答它
        if self.planning is not None or self.project_sync is not None:
            return                              # plan / 项目对账在等回答：别抢输入
        now = time.time()
        if now - self.review_at < 1.0:
            return
        self.review_at = now
        if self.review_check is not None:
            if self.review_check.get("ready"):
                entry = self.review_check.get("entry") or {}
                self.review_check = None
                self.begin_review(entry, sync=False)
            return
        if not self.settings.get_bool("postsession"):
            return
        if self.pending_sins():
            # 罪案区锁死：除了宣言什么都不受理，收尾也排在后面（别把面板顶在锁上面）
            return
        entry, derived = self._next_review_entry()
        if entry is None:
            return
        self.review_check = {"entry": entry, "ready": False}
        threading.Thread(target=self._review_worker, args=(entry,), daemon=True).start()
        if derived:
            self.say("⏱ %s %s 那场已经结束、还没收尾（daemon 队列里没有，是本地自己算的）"
                     " —— 正在核对完成状态…"
                     % (entry.get("date"), entry.get("start")), "cmd")
        else:
            self.say("⏱ %s %s 那场结束了 —— 正在核对完成状态…"
                     % (entry.get("date"), entry.get("start")), "cmd")

    # -- 「这一场到点没开始」那一问 -------------------------------------
    #
    # 事实只有一个来源：**表格**。planned 有、id 没有 = 契约有、API 里没这一场
    # （用户原话：「refresh 能发现这个 session 没了就行」）。所以这里不需要任何
    # 新接口 —— 到点 5 分钟之后 API 还是空的，就问；
    # 问的时候不判「没完成」，而是给三个选择（见 postsession.NostartReview）。

    def _maybe_start_nostart(self):
        """每帧看一眼：有没有「早该开始、API 里却没有」的场次该问人。"""
        if self.nostart is not None:
            return
        now_ts = time.time()
        if now_ts - self.nostart_at < 1.0:      # 节流：别每帧都遍历
            return
        self.nostart_at = now_ts
        if not self.settings.get_bool("nostart"):
            return
        if self.review is not None or self.review_check is not None:
            return                              # 一次只问一件事（收尾优先）
        if self.planning is not None or self.project_sync is not None:
            return                              # plan / 项目对账在等回答：别抢输入
        if self.pending_sins():
            return                              # 罪案区锁死：除了宣言什么都不受理
        entry = self._next_nostart_entry()
        if entry is None:
            return
        self.nostart = psv.NostartReview(entry, hooks=self.nostart_hooks())
        self.say("🚪 %s 那场到点没开始（API 里没有这一场）—— "
                 "p 推迟 / x 不去了 / s 先不管。" % self.nostart.solo(), "cmd")
        self.invalidate()

    def _next_nostart_entry(self):
        """该问哪一场：daemon 入队的优先（它会专门叫我一声），其次自己按表格算。"""
        try:
            queued = psv.nostart_pending()
        except Exception:                       # noqa: BLE001 - 状态文件坏了不该没帧
            queued = []
        if queued:
            entry = dict(queued[0])
            if not entry.get("period_id"):
                try:
                    day = datetime.date.fromisoformat(str(entry.get("date")))
                    period = sp.period_for_session(self.periods_for_day(day),
                                                   entry.get("start"))
                    if period:
                        entry["period_id"] = period.get("id") or ""
                except (TypeError, ValueError):
                    pass
            return entry
        if not self.authoritative:
            # 拿不到真数据（离线 / 快照）就别判：「API 里没有这一场」这句话必须有依据
            return None
        now = datetime.datetime.now()
        try:
            answered = psv.nostart_answered()
        except Exception:                       # noqa: BLE001
            answered = set()
        for row in self.rows:
            entry = self._nostart_entry_for_row(row, now, answered)
            if entry:
                return entry
        return None

    def _nostart_entry_for_row(self, row, now, answered):
        """表格里这一行该不该问？→ 该问就返回 entry，不该问返回 None。"""
        if not row.get("planned") or row.get("id") is not None:
            return None
        try:
            start = datetime.datetime.fromtimestamp(row["startMs"] / 1000.0)
        except (KeyError, TypeError, ValueError):
            return None
        if now < start + datetime.timedelta(seconds=psv.NOSTART_GRACE_SEC):
            return None                          # 预约窗口里还不问（刚过几分钟很正常）
        if (now - start).total_seconds() > psv.NOSTART_LOOKBACK_SEC:
            return None                          # 太老了：别翻旧账
        key = psv.session_key(row.get("date"), row.get("startLabel"))
        if key in answered:
            return None                          # 问过/答过了
        try:
            day = datetime.date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError):
            return None
        return psv.nostart_entry(day, row.get("startLabel"), row.get("durationMin"),
                                 now=now, period_id=row.get("period_id"))

    def nostart_hooks(self):
        """「没开始」那一问要碰外部世界的几个口子（其余全在 postsession 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            "postpone": lambda entry, period_id, minutes: self._nostart_postpone(
                entry, period_id, minutes),
            "mark_missed": lambda entry, note=None: self._nostart_mark_missed(entry, note),
            # 不去了 → 挂在这场的任务交给收尾（A 延期 / B 放弃 / C 重议 / E 推后）。
            # credit=False：这一场一分钟都没干，不记 count。
            "open_review": lambda entry: self.begin_review(
                {"kind": "session", "key": entry.get("key"), "date": entry.get("date"),
                 "start": entry.get("start"), "end": entry.get("end"),
                 "duration": entry.get("duration"),
                 "nostart": True},
                sync=False, credit=False),
        }

    def _nostart_postpone(self, entry, period_id, minutes):
        """推迟：单场 / 整段都走 **relocate 那一套底层**（取消 → 落盘 → 任务跟着挪）。"""
        try:
            day = datetime.date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            raise ValueError("这一场没有合法日期（%r），推迟不了。" % entry.get("date"))
        delta = int(minutes)
        if period_id:
            period_id = str(period_id).upper()
            self._relocate_period(day, period_id, delta, allow_past=True,
                                  reason="nostart-postpone")
            return {"target": "整段 %+d 分钟" % delta}
        row = None
        for cand in self.rows:
            if (cand.get("date") == entry.get("date")
                    and cand.get("startLabel") == entry.get("start")):
                row = cand
                break
        if row is None:
            row = self._synth_planned_row(day, entry)
        moves = self._relocate_moves(day, [row], delta, allow_past=True)

        def done():
            self._relocate_say(day, moves, delta, label="这一场")
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=None,
                                  reason="nostart-postpone", on_done=done)
        if ok is False:
            raise ValueError("原 session 取消失败；这一场的推迟已经回滚。")
        return {"target": moves[0]["to"]}

    def _nostart_mark_missed(self, entry, note=None):
        """这一场算缺失：state 记 missed + 判「已放弃」（daemon 不重订、也不记罪）。

        **不在输入线程上写盘**：x 这一下要立刻把面板收掉、马上进入收尾；
        两个状态文件交给后台队列，进度条会显示「记录缺失」。
        """
        key = str(entry.get("key")
                  or psv.session_key(entry.get("date"), entry.get("start")))
        why = "到点没开始，我选了「不去了」（%s %s）" % (entry.get("date"),
                                                        entry.get("start"))
        action = self.net_action(
            "session.missed",
            "记录缺失 %s %s" % (entry.get("date") or "?", entry.get("start") or "?"),
            {"key": key, "date": entry.get("date"), "start": entry.get("start"),
             "why": why},
            source=SRC_KEEP)
        job = self.jobs.submit(action, on_fail=lambda error: self.say(
            "⚠ 后台记「缺失」失败：%s（这次收尾照常进行）" % error, "warn"))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is not None and not outcome.ok:
                return "后台记录失败"
            return why
        return "正在后台记录（进度看面板）"

    def close_nostart(self, note=None, action="deferred"):
        """把「没开始」那一问收掉（没收过的记成 deferred，免得下次又弹）。"""
        prompt, self.nostart = getattr(self, "nostart", None), None
        if prompt is None:
            return None
        if not prompt.closed:
            try:
                psv.mark_nostart(prompt.entry.get("key"), action, entry=prompt.entry,
                                 note=note or "closed")
            except OSError:
                pass
        self.rebuild_rows()
        self.invalidate()
        return prompt

    def nostart_panel(self):
        """「到点没开始」那一问的正文（挂在 repl_kit 的打断栏位里）。"""
        prompt = self.nostart
        if prompt is None:
            return None
        body = Text()
        for text, style in prompt.lines():
            body.append(text + "\n", style=style or None)
        return Panel(body, title="[bold yellow]🚪 到点没开始[/]",
                     border_style="yellow", box=box.HEAVY)

    def _next_review_entry(self):
        """该收尾的是哪一场 → ``(entry, 是不是本地自己算出来的)``。

        先看 daemon 的队列（`temp/keep_session-postsession.json`）；队列里没有就
        **自己按 data.csv + 任务库算一遍**（:func:`postsession.unreviewed_sessions`）。

        为什么必须自己算：daemon 只是「闹钟」。它没在跑、队列文件丢了、或者 warden
        是事后才打开的 —— 这些情况下队列永远是空的，于是「一场结束了、任务还没
        收尾」这件事**永远不会被问到**，完成状态也就永远落不了库（真实踩过）。
        收过的场次记在 postsession 状态文件的 done 里，不会重复催。
        """
        pending = psv.pending_sessions()
        if pending:
            return pending[-1], False
        if self.offline:
            return None, False
        try:
            entries = self.reload_entries()
        except Exception:                          # noqa: BLE001 - data.csv 坏了不该没帧
            return None, False
        try:
            done_keys = set(psv.load_state()["done"])
        except Exception:                          # noqa: BLE001
            done_keys = set()
        try:
            rows = psv.unreviewed_sessions(self.task_store, entries,
                                           done_keys=done_keys, now=self.review_now,
                                           actual=self.actual_slots())
        except Exception:                          # noqa: BLE001
            return None, False
        if not rows:
            return None, False
        return rows[-1], True

    def render_interrogate(self):
        """repl_kit 的「打断栏位」里画什么 —— 收尾/查账那句问话。

        以前这块面板是塞在契约视图/任务视图自己的 render 里的：视图一换就没了、
        表格一长就被挤出屏幕，而它恰恰是屏幕上最该看见的东西。现在它归框架的那个
        栏位管（压在整块看板下面、提示行上面），切视图、滚表格都不影响。
        """
        if self.pending_sins():
            return self.render_lockdown_panel()
        if self.help_overlay is not None:
            return self.help_overlay
        if self.pending_import is not None:
            return self.pending_import_panel()
        if self.pending_advance is not None:
            return self.pending_advance_panel()
        if self.project_sync is not None:
            return self.project_sync_panel()
        if self.pending_relate_path is not None:
            return self.pending_relate_path_panel()
        if self.pending_interrupt is not None:
            return self.pending_interrupt_panel()
        if self.nostart is not None:
            return self.nostart_panel()
        if self.planning is not None:
            return self.plan_panel()
        rev = self.review_panel()
        if rev is not None:
            return rev
        if self.pending_op_type:
            return self.pending_op_type_panel()
        return None

    def check_untyped_ops(self, announce_import=False):
        """检查是否有未设置类型（``SOP`` / ``incubator``）的 op，并排队让用户补全。"""
        try:
            self.projects.ensure_tagged_pros()
            untyped = self.projects.untyped_ops()
        except Exception:                          # noqa: BLE001
            return []
        self.pending_op_type = [p["id"] for p in untyped]
        if untyped:
            first = untyped[0]
            num = self.projects.number_of(first["id"]) or first["id"]
            if announce_import:
                self.say("⚠ 导入 OmniFocus 项目（op）需说明类型：请为 #%s「%s」输入 "
                         "`SOP` 或 `incubator`（下次可直接 `task import sop` / `op import incubator`）。"
                         % (num, first.get("title")), "warn")
            else:
                self.say("⚠ 发现 %d 个 OmniFocus 项目（op）尚未设置类型（SOP / incubator）："
                         "请为 #%s「%s」输入 `SOP` 或 `incubator`。"
                         % (len(untyped), num, first.get("title")), "warn")
            self.invalidate()
        return untyped

    def pending_op_type_panel(self):
        """未设置类型的 op 提示面板（挂在打断栏位）。"""
        valid_ids = []
        for oid in self.pending_op_type or ():
            op = self.projects.by_id(oid)
            if (op is not None and op.get("status") == pj.STATUS_ACTIVE
                    and pj.normalize_op_type(op.get("op_type")) is None):
                valid_ids.append(oid)
        self.pending_op_type = valid_ids
        if not valid_ids:
            return None
        cur = self.projects.by_id(valid_ids[0])
        num = self.projects.number_of(cur["id"]) or cur["id"]
        pk = (cur.get("omnifocus") or {}).get("id") or "?"
        body = Text()
        body.append("OmniFocus 项目（op）#%s「%s」（%s）尚未指定类型（共 %d 个待补全）：\n"
                    % (num, cur.get("title") or "?", pk, len(valid_ids)),
                    style="bold")
        body.append("  [SOP]       普通标准项目流程（项目处于 running 阶段时进行，plan 只取这一类 op）\n",
                    style="cyan")
        body.append("  [incubator] 瓶颈孵化器流程（项目处于 bottleneck 阶段时进行，plan 只取这一类 op）\n",
                    style="magenta")
        body.append("\n请直接输入 SOP 或 incubator（输入 skip 暂不处理）", style="bold")
        return Panel(body, title="[bold yellow]📁 补全 op 类型（SOP / incubator）[/]",
                     border_style="yellow", box=box.HEAVY)

    def _answer_pending_op_type(self, line):
        """处理用户在 ``pending_op_type`` 面板下的输入；若敲的是常规命令则放行派发。"""
        valid_ids = []
        for oid in self.pending_op_type or ():
            op = self.projects.by_id(oid)
            if (op is not None and op.get("status") == pj.STATUS_ACTIVE
                    and pj.normalize_op_type(op.get("op_type")) is None):
                valid_ids.append(oid)
        self.pending_op_type = valid_ids
        if not valid_ids:
            return None
        text = " ".join(str(line or "").strip().split())
        if not text:
            return True
        low = text.lower()
        if low in ("skip", "稍后", "先不处理"):
            self.pending_op_type = []
            self.say("⏭ 暂不补全 op 类型（下次 refresh 会再次检查；可随时用 `op <序号> sop|incubator` 设置）。",
                     "info")
            self.invalidate()
            return True
        norm_type = pj.normalize_op_type(text)
        if norm_type in pj.OP_TYPES:
            op_id = self.pending_op_type.pop(0)
            updated = self.projects.set_op_type(op_id, norm_type)
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pro = self.projects.pro_for_op(updated)
            pro_note = (" · 关联项目「%s」(%s)" % (pro.get("title"), pro.get("stage"))
                        if pro else "")
            self.say("✅ op #%s「%s」已设为 %s%s。"
                     % (num, updated.get("title"), norm_type, pro_note), "ok")
            if self.pending_op_type:
                nxt = self.projects.by_id(self.pending_op_type[0])
                if nxt is not None:
                    nnum = self.projects.number_of(nxt["id"]) or nxt["id"]
                    self.say("👉 下一个待补全：op #%s「%s」，请输入 SOP 或 incubator："
                             % (nnum, nxt.get("title")), "cmd")
            self.rebuild_rows()
            self.invalidate()
            return True
        # 如果用户输入的是合法 TUI 命令（如 day / plan / task / pro / op / refresh 等），放行派发
        parts = self._split(line)
        if parts:
            cmd_head = parts[0].lower()
            if (self.shell.registry.get(cmd_head) is not None
                    or (isinstance(self.view, ContractView)
                        and re.match(r"^[Tt]\d+$", parts[0]))):
                return None
        cur = self.projects.by_id(self.pending_op_type[0])
        cnum = (self.projects.number_of(cur["id"]) if cur else None) or self.pending_op_type[0]
        self.say("op #%s「%s」还在等类型：请输入 `SOP` 或 `incubator`（或 `skip` 稍后处理）。"
                 % (cnum, (cur or {}).get("title") or "?"), "warn")
        self.invalidate()
        return True

    def pending_advance_panel(self):
        """``pro <序号> -a / --advance`` 等待提供新 ``op`` 的打断面板。"""
        adv = self.pending_advance or {}
        body = Text()
        body.append("项目「%s」切换阶段：%s → %s\n"
                    % (adv.get("pro_title") or "?",
                       adv.get("from_stage") or "?",
                       adv.get("target_stage") or "?"),
                    style="bold")
        body.append("  需要关联新的 %s OmniFocus Project (op)：\n"
                    % (adv.get("required_op_type") or "op"), style="cyan")
        body.append("  · 输入 `task import [链接]`（或 `op import`）直接从剪贴板/链接导入并关联\n",
                    style="green")
        body.append("  · 输入 `op <序号>`（或直接输入 op 序号）关联已有的 OmniFocus Project\n",
                    style="green")
        body.append("  · 输入 `q` 取消切换\n", style="dim")
        return Panel(body, title="[bold cyan]🔀 pro --advance · 关联新 op[/]",
                     border_style="cyan", box=box.HEAVY)

    def _answer_pending_advance(self, line):
        """处理 ``pro <序号> -a / --advance`` 问答下的输入。"""
        adv = self.pending_advance
        if adv is None:
            return None
        raw = str(line or "").strip()
        if not raw:
            self.say("请提供要关联的 %s op：`task import [链接]` 或 `op <序号>`（`q` 取消）。"
                     % (adv.get("required_op_type") or "op"), "warn")
            return True
        low = raw.lower()
        if low in ("q", "quit", "cancel", "abort", "取消", "放弃"):
            self.pending_advance = None
            self.say("⏹ 已取消项目「%s」的阶段切换（仍为 %s）。"
                     % (adv.get("pro_title") or "?", adv.get("from_stage") or "?"), "info")
            self.invalidate()
            return True
        parts = raw.split()
        head = parts[0].lower()
        try:
            if head == "task" and len(parts) >= 2 and parts[1].lower() in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[2:])
                return True
            if head in ("op", "ofp") and len(parts) >= 2 and parts[1].lower() in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[2:])
                return True
            if head in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[1:])
                return True
            if of.link_kind(raw) == "project":
                self.cmd_task_import(parts)
                return True
            op_tok = " ".join(parts[1:]).strip() if head in ("op", "ofp", "omnifocus-project") else raw
            op = self.projects.find(op_tok)
            self._finish_pro_advance(adv["pro_id"], op["id"],
                                     target_stage=adv.get("target_stage"))
        except Exception as e:                      # noqa: BLE001
            self.had_error = True
            self.say(str(e), "warn")
        self.invalidate()
        return True

    def pending_import_panel(self):
        """``task import`` 发现重复任务时的询问面板（打断栏位）。"""
        pending = self.pending_import or {}
        items = pending.get("items") or []
        repeats = pending.get("repeats") or {}
        body = Text()
        if len(items) == 1:
            item = items[0] or {}
            title = item.get("title") or item.get("id") or "（无标题）"
            body.append("OmniFocus 任务「%s」是重复任务。\n" % title, style="bold")
        else:
            body.append("这批 %d 条里有 %d 条是 OmniFocus 重复任务：\n"
                        % (len(items), len(repeats)), style="bold")
        any_supported = False
        for index in sorted(repeats):
            rec = repeats[index] or {}
            info = rec.get("info") or {}
            plan = rec.get("plan") or {}
            item = items[index] if index < len(items) else {}
            title = (item or {}).get("title") or (info or {}).get("id") or "（无标题）"
            rule = (info or {}).get("rule_string") or "（重复规则）"
            body.append("· %s  %s\n" % (title, rule), style="cyan")
            if plan.get("supported"):
                any_supported = True
                body.append("  复制到本地 → %s\n" % plan.get("summary"), style="green")
            else:
                body.append("  不能复制：%s\n" % (plan.get("reason") or "规则无法映射"),
                            style="yellow")
            note = plan.get("note")
            if note:
                body.append("  %s\n" % note, style="dim")
        if any_supported:
            body.append("\n[y] 复制重复设置并导入    [n] 只导入这一次    [q] 取消",
                        style="bold")
        else:
            body.append("\n[n] 只导入这一次    [q] 取消（这些规则本地 repeat 表达不了）",
                        style="bold")
        return Panel(body, title="[bold cyan]📥 导入重复任务[/]",
                     border_style="cyan", box=box.HEAVY)

    def review_panel(self):
        """收尾 / 查账面板的正文（由 :meth:`render_interrogate` 挂进栏位）。"""
        if self.review is not None:
            body = Text()
            for text, style in self.review.lines():
                body.append(text + "\n", style=style or None)
            if self.review.due_mode():
                head = "🔍 inspect-due · %s" % self.review.headline()
            else:
                head = "⏱ 收尾 · %s" % self.review.headline()
            return Panel(body, title="[bold yellow]%s[/]" % head,
                         border_style="yellow", box=box.HEAVY)
        if self.review_check is not None:
            entry = self.review_check.get("entry") or {}
            return Panel(
                Text("正在核对 %s %s 那场的完成状态（Focusmate + OmniFocus）…"
                     % (entry.get("date"), entry.get("start")), style="dim"),
                title="[bold yellow]⏱ 收尾[/]", border_style="dim", box=box.ROUNDED)
        return None

    def after_dispatch_review(self, line):
        """派发完一条命令之后回头问收尾一句：刚才那条算不算把任务转移走了？"""
        if self.review is None:
            return
        self.review.after_dispatch(line)
        if self.review is not None and self.review.closed:
            self.close_review()

    def close_review(self, note=None):
        """收尾结束（全部处理完 / 用户敲 q）：告诉 daemon 这场翻篇了。"""
        review, self.review = self.review, None
        if review is None:
            return None
        if review.due_mode():
            # 过期检查没有「那一场」可以翻篇：不写 postsession 状态文件。
            # 任务本身已经被改成 completed / dropped（或跳过时保持 active），
            # 所以下次 inspect-due 自然不会再问一遍。
            pass
        else:
            try:
                psv.mark_done(review.session, note=note or "reviewed")
            except OSError:
                pass
        self.rebuild_rows()
        if review.skipped:
            self.say("收尾结束；跳过了 %d 条（它们还是「进行中」）。" % len(review.skipped), "warn")
        return review

    def review_active(self):
        return self.review is not None and self.review.pending()
