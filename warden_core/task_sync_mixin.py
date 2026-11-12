"""warden_core.task_sync_mixin —— Focusmate 完成同步与 OmniFocus 单向联动写回。"""
import copy
import time

from .bridge import BridgeError
from .common import (
    FM_PENDING,
    SRC_OMNIFOCUS,
    ks,
    of,
    pj,
    rl,
    ts,
)


class TaskSyncMixin(object):
    @staticmethod
    def _focusmate_task_summary(remote):
        """日志用：Focusmate task 拉取结果摘要（不记 token，只记任务事实）。"""
        rows = remote if isinstance(remote, list) else []
        done = [r for r in rows if isinstance(r, dict)
                and (r.get("completedAt") or r.get("completed_at") or r.get("completed"))]
        return {
            "count": len(rows),
            "completed_count": len(done),
            "completed": [{
                "id": r.get("id") or r.get("taskId"),
                "title": r.get("title"),
                "completed": bool(r.get("completed")),
                "completedAt": r.get("completedAt") or r.get("completed_at"),
                "attachments": r.get("attachments") or [],
            } for r in done],
        }

    def sync_task_completions(self, quiet=True):
        """``GET /v1/tasks`` → 对齐完成状态（**读**操作，后台跑）。

        ``refresh`` / ``task sync`` / 收尾都走它。和 :meth:`cmd_task_sync` 的区别
        只有一件事：这里不往 Focusmate 写 —— 不把「每周重复」落到焦点场次上
        （那会真的在 Focusmate 建 task）。``refresh`` 要能随时敲，不能敲一下
        就在外面多出一条任务。

        **但 OmniFocus 的完成写回会做**（在 :meth:`_apply_task_pull` 里，主线程）。
        「在 Focusmate 勾掉了」是一条已经发生的事实，不是 warden 的新决定：
        本地跟着打 ✓ 的同时，OmniFocus 那条也得完成（幂等，写过的记在 writeback）。
        """
        # 注：这里**不**看 self.offline —— 「离线」只挡 refresh 那种自己发起的拉取；
        # 用户显式敲 `task sync` 就是要求去对一次账（测试也靠这条路径）。
        action = self.net_action("task.pull", "拉 Focusmate task", {})
        job = self.jobs.submit(action, persist=False)
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True

    def report_omnifocus_links(self):
        """说清楚「OmniFocus 那边到底能不能核对」—— 一条都没关联就别让人瞎猜。"""
        tasks = self.task_store.list()
        linked = [t for t in tasks if ts.TaskStore.omnifocus_id(t)]
        if linked:
            done = sum(1 for t in linked if not self.task_store.is_open(t))
            self.say("OmniFocus 关联：%d/%d 条任务挂了 primary key（其中 %d 条已收工）。"
                     % (len(linked), len(tasks), done), "info")
        else:
            self.say("OmniFocus 关联：0/%d 条 —— 一条都没挂 primary key，所以核对不到"
                     " OmniFocus 的完成状态。用 `task import` 粘 omnifocus:///task/… 链接，"
                     "或 `task <id> link <pk>`。" % len(tasks), "warn")
        if not self.omnifocus_enabled():
            self.say("（OmniFocus 现在是关着/不可用：设置 omnifocus=off 或没给自动化权限，"
                     "这一侧一律跳过。）", "warn")
        return len(linked)

    def announce_completions(self, since, limit=3):
        """刚归档的那几条任务，把信息（含备注）说一遍。

        需求背景：备注里可以塞网址，完成之后照着去对应地方把活干完 ——
        所以完成的第一时间就把备注亮出来，而不是等用户自己去翻归档。
        """
        records = self.task_store.load_archive().get("records") or []
        fresh = [r for r in records[since:] if isinstance(r, dict)]
        for rec in fresh[:limit]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            fields = rec.get("fields") or {}
            title = (fields.get("title") or fields.get("name")
                     or self.task_store._one_line(fields)[:60] or "（空任务）")
            try:
                live = self.task_store.get(rec.get("task_id"))
            except (ValueError, TypeError):
                live = None
            line = "✅ 完成：任务 %s「%s」· 完成时间 %s · session %s %s" % (
                self.task_label(live) if live else "id %s" % (rec.get("task_id") or "?"),
                title,
                fm.get("completed_at") or "?", sess.get("date") or "?",
                sess.get("start") or "?")
            note = rec.get("notes") or self.task_store.notes({"fields": fields})
            if note:
                line += "\n     备注：%s" % note
            self.say(line, "ok")
        if len(fresh) > limit:
            self.say("…还有 %d 条完成归档（「数据」视图 / task archive 看全部）"
                     % (len(fresh) - limit), "info")
        return fresh

    # -- OmniFocus 联动（**单向**：warden → OmniFocus）------------------
    #
    # 契约（见 docs/omnifocus.md）：
    #   * 任务被挂到一场 session 上时，截止时间＝那场的终点（task_store.assign 写的）；
    #   * 「完成」的时候把完成 + 这条截止时间写回 OmniFocus（一条命令，见 set --due）；
    #   * 「放弃」的时候把 OmniFocus 也放弃；
    #   * 「延期 / 快捷推后 / 重议」**不动** OmniFocus（活还在，只是本地换了个账本）；
    #   * 反向没有：OmniFocus 里的改动不会回流成 warden 的命令，只在收尾时被**读**一次。
    #
    # 所有写操作都是子进程（omnifocus.py → omnifocus_tasks.py → osascript），
    # 失败只变成一句话，绝不把 TUI 掀了。

    def omnifocus_enabled(self):
        return self.settings.get_bool("omnifocus") and self.omnifocus.available()

    def omni_due_of(self, task):
        """这条任务该写回 OmniFocus 的截止时间：``fields.due``，否则由 session 推出来。"""
        fields = task.get("fields") or {}
        due = str(fields.get("due") or "").strip()
        if due:
            return due
        return ts.session_due(task.get("session") or {}) or ""

    @staticmethod
    def _omni_log_fields(task, pk=None):
        fields = task.get("fields") or {}
        omni = task.get("omnifocus") or {}
        return {
            "task_id": task.get("id"),
            "task_status": task.get("status"),
            "task_title": fields.get("title") or fields.get("name") or "",
            "pk": pk or omni.get("id"),
            "due": fields.get("due") or "",
            "session": task.get("session") or None,
            "writeback_before": dict(omni.get("writeback") or {}),
        }

    def _omni_repeat_advance(self, task, pk, res):
        """写完之后「OmniFocus 那条是重复任务，已经翻到下一次」这种情况。

        ``omnifocus_tasks.py set --complete`` 在重复任务上返回的 ``after`` 是**新
        那一次**的快照（``completed`` 当然是 false），同时把新 primary key 放在
        ``repeat_clone_id`` 里。以前这里只把它写进日志、当没看见 —— 后果是：

        * 复核 `after.completed` 判成「写回失败」→ 不记 writeback → 下一次同步
          再写一遍 → OmniFocus 的重复表被一次次往前推；
        * 本地还开着的记录全挂在**旧 key**（已经完成的那一次）上，下一次的活
          在 OmniFocus 里永远勾不掉 —— 用户看到的正是「本地 ✓ 了、OmniFocus
          那条还没完成」。

        返回 ``(advanced, moved)``：真的翻篇了没有、跟着改挂的本地任务 id。
        """
        if not isinstance(res, dict):
            return False, []
        clone = str(res.get("repeat_clone_id") or "").strip()
        if not clone or clone == str(pk):
            return False, []
        try:
            moved = self.task_store.advance_omnifocus(pk, clone,
                                                      exclude_task_id=task.get("id"))
        except (ValueError, TypeError):
            moved = []
        return True, moved

    @staticmethod
    def _omni_repeat_note(advanced, clone, moved):
        if not advanced:
            return ""
        return "；OmniFocus 那是重复任务，已经翻到下一次 %s，%d 条还没收工的本地记录改挂过去了" % (
            clone, len(moved))

    def _omnifocus_write_claimed(self, task):
        """这条 OmniFocus 完成是否已经由**另一条本地记录**写回过了。

        项目任务 / 重复任务都可能在本地留下多条共享同一个 primary key 的记录。
        一个 OmniFocus key 只代表**一次** occurrence；只要其中一条已经写回完成，
        另一条就不能再 complete 一次，否则重复任务会被往前多推一周。
        """
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return False
        own = ((task.get("omnifocus") or {}).get("writeback") or {})
        if own.get("completed"):
            return True
        want_id = task.get("id")
        for other in self.task_store.list():
            if other.get("id") == want_id:
                continue
            if ts.TaskStore.omnifocus_id(other) != pk:
                continue
            if self.task_store.status_of(other) != ts.STATUS_COMPLETED:
                continue
            wb = ((other.get("omnifocus") or {}).get("writeback") or {})
            if wb.get("completed"):
                return True
        return False

    def _maybe_flush_omnifocus(self, now=None):
        """本地优先写回的触发：空闲 ≥90s 写一次，之后仍空闲每 600s 一次。

        用户原话：「本地修改状态（之后 refresh 的时候更新，包括 focusmate 任务。
        比如说空闲 90s 自动 refresh）」，补充：「空闲的时候（刷新完成之后另外
        一次）间隔 600s」。

        只在真的有人在**用**的时候攒账（``omni_complete`` 只写 outbox）；这一拍
        是「人停了 90 秒」才把账一次性写回 OmniFocus。``jobs.sync``（--exec/
        测试）没有「之后」，直接当场写。
        """
        if self.offline:
            return False
        if self.jobs.sync:
            return self.flush_omnifocus()
        now = now if now is not None else time.time()
        if now - getattr(self, "_omni_idle_checked", 0.0) < 5.0:
            return False
        self._omni_idle_checked = now
        try:
            idle = ks.hid_idle_seconds()
        except Exception:                      # noqa: BLE001 - 读不到空闲就不判
            idle = None
        if idle is None:
            return False
        if idle < 90:
            self._omni_idle_flush_at = 0.0     # 有人在动：下一段空闲重新算
            return False
        last = getattr(self, "_omni_idle_flush_at", 0.0)
        if last and now - last < 600:
            return False
        self._omni_idle_flush_at = now
        flushed = self.flush_omnifocus()
        # 另一半球：读也等到空闲再做（数据 stale 才刷，别每 5 秒叫一次浏览器）。
        try:
            if (not self.read_at.get("session")
                    or now - self.read_at["session"] > 600):
                self.refresh(quiet=True)
                self.refresh_task_state()
        except Exception:                      # noqa: BLE001 - 刷不动就等下一拍
            pass
        return flushed

    def omni_complete(self, task, minutes=None, at=None):
        """OmniFocus：完成 + 把截止时间写回 session 终点。返回一句人话。

        **写**操作，走后台队列：本地先记「已经写回过完成」（乐观），
        osascript 那一趟在 worker 里跑；失败就回滚这个标记 + 记账（re-action）。
        """
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        # 项目唤起出来的任务可能只覆盖那条 OmniFocus 任务的一部分 count：
        # 这时候勾完成等于把剩下的 count 抹掉，所以挡一下（见 project_store）。
        try:
            allowed, why = pj.completion_guard(task, self.projects, self.task_store)
        except Exception:                       # noqa: BLE001 - 账算不出来不该挡住完成
            allowed, why = True, ""
        if not allowed:
            rl.log_event("omnifocus", "complete_blocked", reason="project-partial",
                         pk=pk, task_id=task.get("id"))
            return why
        if self._omnifocus_write_claimed(task):
            rl.log_event("omnifocus", "complete_skipped", reason="already-claimed",
                         pk=pk, task_id=task.get("id"))
            return "OmniFocus %s 那一次已经由另一条本地记录写回完成（不重复勾）" % pk
        enabled = self.omnifocus_enabled()
        fields = self._omni_log_fields(task, pk)
        due = self.omni_due_of(task)
        rl.log_event("omnifocus", "complete_start", minutes=minutes, at=at,
                     enabled=enabled, api_due=due or None, **fields)
        if not enabled:
            rl.log_event("omnifocus", "complete_skipped", reason="disabled_or_unavailable",
                         **fields)
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        if self.net_async:
            # **本地优先**：只记一笔待发，不碰 OmniFocus。水位到了（空闲 90s /
            # refresh / 退出 / 下次启动）再一次性批量写回 —— 每条起一个 osascript
            # 是这套延迟的主要来源，而且写失败还要回滚本地标记。
            self.omni_outbox.put({"id": pk, "op": "complete", "task_id": task["id"],
                                  "due": due or None, "at": at})
            rl.log_event("omnifocus", "complete_queued", **fields)
            return "OmniFocus %s 完成已记本地账（空闲 / refresh 时批量写回）" % pk[:8]
        before = copy.deepcopy(task)
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "completed")
        except ValueError:
            pass
        action = self.net_action("omnifocus.complete",
                                 "OmniFocus 完成 %s" % pk[:8],
                                 {"task_id": task["id"], "pk": pk, "at": at,
                                  "due": due or None}, source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self._omni_failed(
            task["id"], before, "OmniFocus 写回完成（pk %s）" % pk[:8], error))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 写回失败：%s" % (outcome and outcome.error or "没跑")
            res = (outcome.result or {}).get("res") or {}
            changed = ", ".join(res.get("changed") or [])
            return "OmniFocus %s 完成%s" % (pk, ("（%s）" % changed) if changed else "")
        return "OmniFocus 完成写回丢后台了（%s）" % pk[:8]

    def _omni_failed(self, task_id, before, what, error):
        """OmniFocus 写失败：把本地标记放回去 + 说清楚（账已经记下了）。"""
        self._revert_task(task_id, before, "%s 没写成：%s" % (what, error))

    def omni_drop(self, task, at=None, all_occurrences=False):
        """OmniFocus：放弃这条任务（只给「真的不要了」用）。

        ``all_occurrences`` 给重复任务本体用：放弃「这一套」而不是「这一次」，
        否则 OmniFocus 会照着重复规则继续生出下一次。

        **写**操作，走后台队列（乐观 → 失败回滚 + 记账）。
        """
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        enabled = self.omnifocus_enabled()
        fields = self._omni_log_fields(task, pk)
        rl.log_event("omnifocus", "drop_start", at=at, enabled=enabled,
                     all_occurrences=all_occurrences, **fields)
        if not enabled:
            rl.log_event("omnifocus", "drop_skipped", reason="disabled_or_unavailable",
                         **fields)
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        if self.net_async:
            self.omni_outbox.put({"id": pk, "op": "drop", "task_id": task["id"],
                                  "at": at, "all_occurrences": bool(all_occurrences)})
            rl.log_event("omnifocus", "drop_queued", **fields)
            return "OmniFocus %s 放弃已记本地账（空闲 / refresh 时批量写回）" % pk[:8]
        before = copy.deepcopy(task)
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "dropped")
        except ValueError:
            pass
        action = self.net_action("omnifocus.drop",
                                 "OmniFocus 放弃 %s" % pk[:8],
                                 {"task_id": task["id"], "pk": pk, "at": at,
                                  "all_occurrences": bool(all_occurrences)},
                                 source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self._omni_failed(
            task["id"], before, "OmniFocus 放弃（pk %s）" % pk[:8], error))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 放弃失败：%s" % (outcome and outcome.error or "没跑")
            res = (outcome.result or {}).get("res") or {}
            changed = ", ".join(res.get("changed") or [])
            return "OmniFocus %s 已放弃%s" % (pk, ("（%s）" % changed) if changed else "")
        return "OmniFocus 放弃丢后台了（%s）" % pk[:8]

    def fm_complete(self, task):
        """Focusmate：把这条 task 勾完成（走 bridge 的内部 API，GET 复核 completedAt）。"""
        fm = task.get("focusmate") or {}
        fm_task_id = fm.get("task_id")
        if not fm_task_id:
            return ""
        if self.offline:
            rl.log_event("focusmate", "complete_skipped", task_id=task["id"],
                         focusmate_task_id=fm_task_id, reason="offline")
            return "离线模式：没去勾 Focusmate"
        # **乐观**：本地立刻记「已勾」（打 pending 标记），网络丢进后台。
        # 勾失败就把这个字段放回去，并且记账（re-action 重放）。
        before = copy.deepcopy(task)
        local_id = task["id"]
        try:
            self.task_store.attach_focusmate(local_id, {
                FM_PENDING: {"at": ts._now(), "via": "optimistic", "op": "task.complete"},
                "completed_at": ts._now(),
            })
        except ValueError:
            pass
        action = self.net_action("task.complete",
                                 "勾完成 %s" % self.task_label(task),
                                 {"task_id": local_id, "focusmate_task_id": fm_task_id})
        job = self.jobs.submit(action, on_fail=lambda error: self._complete_failed(
            local_id, before, error))
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                return "Focusmate 勾完成失败：%s" % job.outcome.error
            return "Focusmate 已勾完成"
        return "Focusmate 勾完成丢后台了（进度看面板）"

    def _complete_failed(self, task_id, before, error):
        """勾失败：把 completed_at 放回去（本地别先替 Focusmate 宣布完成）。"""
        self._revert_task(task_id, before, "Focusmate 没勾上：%s" % error)

    def fm_delete_task(self, task):
        """Focusmate：删掉这条任务在 Task Manager 里那条 task（收尾「转移」时用）。

        需求：「任务转移之后 focusmate 任务删除」。转移的语义是**这一场不干了**
        —— 旧任务标放弃、复制品挂到下一场。可 Focusmate 那边那条 task 还挂在
        旧场次上：既不会完成，又一直躺在 Task Manager 里，第二天对账还会以为
        它在等你。删掉它，账才干净（OmniFocus 那条照旧一个字节都不动）。

        返回一句人话（收尾面板会显示），删不掉也**不挡**转移本身。
        """
        fm = task.get("focusmate") or {}
        task_id = fm.get("task_id") or fm.get("id")
        if not task_id:
            return "（这条没有 Focusmate task，不用删）"
        if self.offline:
            return "离线模式：没去删 Focusmate task %s" % str(task_id)[:8]
        try:
            res = self.bridge.call("delete_task", timeout=180, taskId=task_id)
        except BridgeError as e:
            self.had_error = True
            return "Focusmate task 删除失败：%s" % e
        if not res.get("verified"):
            self.had_error = True
            still = (res.get("stillThere") or {}).get("title") or ""
            return "Focusmate 说删了，但复核还在%s（去 Task Manager 手动删一下）" % (
                ("：" + still) if still else "")
        try:
            self.task_store.forget_focusmate(task["id"], reason="transfer")
        except ValueError:
            pass
        return "Focusmate task %s 已删除" % str(task_id)[:8]

    def sync_omnifocus_status(self):
        """读一遍 OmniFocus 的完成状态，返回 ``{本地任务 id: OmniFocus 行}``。

        只在收尾的核对里调用（一次 list 拿到全部，缓存在 client 里）。
        读不到就返回空 dict —— 收尾照常进行，只是少了「OmniFocus 那边也勾了」这条信息。
        """
        started = time.monotonic()
        enabled = self.omnifocus_enabled()
        rl.log_event("omnifocus", "status_pull_start", enabled=enabled)
        if not enabled:
            rl.log_event("omnifocus", "status_pull_skipped", reason="disabled_or_unavailable")
            return {}
        rows = None
        try:
            rows = self.omnifocus.list_tasks(refresh=True)
        except of.OmniFocusError as e:
            self.had_error = True
            rl.log_event("omnifocus", "status_pull_error", kind=e.kind,
                         exit_code=e.exit_code, detail=e.detail, message=str(e),
                         elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            self.say("读 OmniFocus 失败：%s" % e, "warn")
            return {}
        by_id = {}
        for row in rows or []:
            if row.get("id"):
                by_id[str(row["id"])] = row
        tasks = self.task_store.list()
        # 同一条 OmniFocus 任务在本地可能挂着好几条记录（转移留下的旧记录、重复任务
        # 的子任务）。其中一条要是「**我们自己写回**完成的」，那这条 OmniFocus 的
        # completed 就已经有归属了 —— 别再拿它去把**还开着**的兄弟记录也带成 ✓：
        # 重复任务今天这条写完回，明天那条会被判成「已完成」，活还没干就没了。
        claimed = set()
        for task in tasks:
            if ts.TaskStore.is_repeat_template(task):
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            wb = (task.get("omnifocus") or {}).get("writeback") or {}
            if (self.task_store.status_of(task) == ts.STATUS_COMPLETED
                    and wb.get("completed")):
                claimed.add(str(pk))
        out = {}
        held = []
        for task in tasks:
            if ts.TaskStore.is_repeat_template(task):
                # 本体不是一次可完成的活；同一条 OmniFocus 的完成状态只对齐到子任务。
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk or str(pk) not in by_id:
                continue
            if str(pk) in claimed and self.task_store.is_open(task):
                held.append(task["id"])
                continue
            out[task["id"]] = by_id[str(pk)]
        done = [{
            "task_id": task_id,
            "pk": row.get("id"),
            "name": row.get("name"),
            "completed": bool(row.get("completed")),
            "completed_at": row.get("completed_at"),
        } for task_id, row in out.items()]
        rl.log_event(
            "omnifocus", "status_pull_result", row_count=len(rows or []),
            linked_count=len(out), completed_count=sum(1 for x in done if x["completed"]),
            claimed_pks=sorted(claimed), held_task_ids=held,
            linked=done, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return out

    def writeback_completions(self, tasks=None):
        """把「本地已完成、但还没写回 OmniFocus」的任务写回去（幂等，靠 writeback 标记）。

        需求（第 2 条）：任务被分配到的 session 时间＝它的截止时间；完成的时候
        把这条截止时间一起写回 OmniFocus —— 于是 OmniFocus 里记的是「这一场是什么时候结束的」。

        **写**操作，走后台队列：一次子进程一条任务，全部塞进**同一个** job 里跑
        （进度是「第几条/共几条」），本地 writeback 标记在 apply 里落定。
        这一批会**并进当前那一步操作**（refresh / task sync），不会另起一个标题。
        """
        if not self.omnifocus_enabled():
            rl.log_event("omnifocus", "writeback_scan_skipped",
                         reason="disabled_or_unavailable")
            return []
        pool = list(tasks if tasks is not None else self.task_store.list())
        # 同一个 OmniFocus primary key 只写一次。重复任务/项目任务在本地可能留下
        # 多条共享同一个 key 的完成记录；逐条写回会把 OmniFocus 的重复表连着
        # 往前推两周（用户看到的「完成一次，下下周才有」）。
        claimed = set()
        for task in pool:
            if ts.TaskStore.is_repeat_template(task):
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            wb = ((task.get("omnifocus") or {}).get("writeback") or {})
            if pk and wb.get("completed"):
                claimed.add(str(pk))
        todo = []
        seen_pks = set()
        for task in pool:
            if ts.TaskStore.is_repeat_template(task):
                # 本体不可完成；它的子任务共享同一条 OmniFocus 链接，写回由子任务做。
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            pk = str(pk)
            if pk in claimed or pk in seen_pks:
                continue
            try:
                allowed, why = pj.completion_guard(task, self.projects, self.task_store)
            except Exception:                   # noqa: BLE001 - 账算不出来不该挡住完成
                allowed, why = True, ""
            if not allowed:
                rl.log_event("omnifocus", "complete_blocked", reason="project-partial",
                             pk=pk, task_id=task.get("id"), source="writeback_scan")
                continue
            todo.append(task["id"])
            seen_pks.add(pk)
        # 扫描到的完成 + 攒在 outbox 里的意图，合成一批，一次 osascript 写完。
        ops = self.pending_writebacks(pool)
        if not ops:
            rl.log_event("omnifocus", "writeback_scan_result", written=[])
            return []
        rl.log_event("omnifocus", "writeback_scan_start", pending=len(ops))
        self.flush_omnifocus(ops)
        return [(op.get("task_id"), "丢后台了") for op in ops]

    # -- 本地优先写回：outbox + 批量 flush --------------------------------
    def pending_writebacks(self, pool=None):
        """攒着没写回 OmniFocus 的账 → ops 列表。

        两个来源：
        1. ``omni_outbox``：``task done/drop`` 的**显式意图**（本地优先，最多可
           能已经躺了十几分钟）；
        2. 扫描：本地已完成、但 writeback 标记还是空的记录（收尾/手工标完成
           走的就是这条路）。

        同一个 ``(op, pk)`` 只留一条 —— 重复任务被连推两轮就是这么来的。
        """
        ops = {}
        outbox = getattr(self, "omni_outbox", None)
        for item in (outbox if outbox is not None else ()):
            pk = str(item.get("id") or "")
            if pk:
                ops["%s:%s" % (str(item.get("op") or "complete"), pk)] = dict(item)
        pool = list(pool if pool is not None else self.task_store.list())
        claimed = set()
        for task in pool:
            if ts.TaskStore.is_repeat_template(task):
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if pk and ((task.get("omnifocus") or {}).get("writeback") or {}).get("completed"):
                claimed.add(str(pk))
        for task in pool:
            if ts.TaskStore.is_repeat_template(task):
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            pk = str(pk)
            wb = ((task.get("omnifocus") or {}).get("writeback") or {})
            if pk in claimed or wb.get("completed"):
                continue
            try:
                allowed, _why = pj.completion_guard(task, self.projects, self.task_store)
            except Exception:                   # noqa: BLE001 - 账算不出来不该挡住完成
                allowed = True
            if not allowed:
                rl.log_event("omnifocus", "complete_blocked", reason="project-partial",
                             pk=pk, task_id=task.get("id"), source="writeback_scan")
                continue
            ops.setdefault("complete:%s" % pk, {
                "id": pk, "op": "complete", "task_id": task["id"],
                "due": self.omni_due_of(task) or None})
        return list(ops.values())

    def flush_omnifocus(self, ops=None):
        """把待发的写回交给后台队列（一个 osascript 处理 N 条）。

        注意不看 ``self.offline``：离线模式说的是 Focusmate 网络，OmniFocus 是
        本地应用，断网也该写。
        """
        try:
            if not self.omnifocus_enabled():
                return False
        except Exception:                      # noqa: BLE001 - 检查失败就当关着
            return False
        ops = list(ops if ops is not None else self.pending_writebacks())
        if not ops:
            return False
        action = self.net_action("omnifocus.flush",
                                 "写回 OmniFocus（%d 条）" % len(ops),
                                 {"ops": ops}, source=SRC_OMNIFOCUS)
        self.jobs.submit(action, persist=False)
        return True
