"""warden_core.plan_watchdog_mixin —— 「今天还没 plan」的检查、提醒任务与兜底自动分配。

用户在 2026-11-20 要的整条链路（口径已拍板）：

1. **检查**挂在 ``refresh_warnings`` 那条每帧钩子上（节流 60 秒一次；和收尾 /
   项目对账同一个时机，所以「每个 session 结束后固定检查」天然覆盖到）。
2. 今天**一个项目都没分过 count**、还有可排空场、任务库里没有提醒任务
   → 建一条 25 分钟的 ``plan-watchdog`` 任务，标题写「排今天–后天的 plan」，
   再用 formatter ``enlist`` 排进当天**最早的**那场（「最近的第一个 session」）。
3. 提醒任务**每天最多造一条**（账本 ``created``）—— 用户手动勾掉之后不会再被刷回来。
4. plan 一旦存在（≥1 个 count）→ 提醒任务**本地标完成**（不碰 Focusmate / OmniFocus）。
5. 提醒任务排在今天、那场**已经过去了**、今天仍然没有 plan、且今天还没兜底过
   → **自动 plan 今天**：随机挑项目 + 随机切 count（:func:`plan_watchdog.random_allocs`），
   走正常 ``_plan_apply`` 流水线（切排布 → 挂任务 → 末尾统一 book）。
   兜底每天最多一次（账本 ``auto_planned``，失败也记，绝不每 60 秒重试）。

所有节点写同一份 JSONL（``area="plan_watchdog"``）。这里只做接线：认人 / 算时间 /
随机算法的纯函数在 :mod:`plan_watchdog`，排期算法在 ``project_plan`` / ``task_hooks``，
本模块**不重复实现**任何一条。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import time

from .common import (
    PLAN_WATCHDOG_FILE,
    pp,
    pw,
    rl,
    th,
    ts,
)


class PlanWatchdogMixin(object):
    #: 两次检查之间至少隔多久。挂在每帧钩子上，不节流就是每帧都读一遍任务库。
    PLAN_WATCHDOG_INTERVAL = 60.0

    # -- 初始化 --------------------------------------------------------

    def _plan_watchdog_init(self):
        self._plan_watchdog_at = 0.0
        #: 兜底自动 plan 排队/执行中：这期间不再检查，避免一轮还没跑完又排一轮。
        self._plan_watchdog_busy = False
        #: 测试注入 ``random.Random(seed)`` 用；None = 真随机（每次 new 一个）。
        self.plan_watchdog_rng = None

    def _plan_watchdog_random(self):
        rng = getattr(self, "plan_watchdog_rng", None)
        return rng if rng is not None else random.Random()

    # -- 账本 ----------------------------------------------------------

    def _plan_watchdog_ledger(self):
        try:
            with open(PLAN_WATCHDOG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _plan_watchdog_save_ledger(self, data):
        """原子写；写失败只记日志（账本坏了绝不能挡住提醒/排期）。"""
        try:
            directory = os.path.dirname(os.path.abspath(PLAN_WATCHDOG_FILE))
            os.makedirs(directory, exist_ok=True)
            tmp = PLAN_WATCHDOG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data if isinstance(data, dict) else {}, handle,
                          ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, PLAN_WATCHDOG_FILE)
            return True
        except OSError as exc:
            rl.log_event("plan_watchdog", "ledger_write_failed",
                         level="warning", error=str(exc))
            return False

    # -- 节拍 ----------------------------------------------------------

    def _maybe_plan_watchdog(self, now=None):
        """每帧钩子的入口：最多 60 秒跑一次 :meth:`_plan_watchdog_check`。

        ``offline`` 模式（``--offline`` / 单测）不自动跑：那是排障/回放用的，
        自动建任务、切排布会污染现场。测试要验行为就**显式**调
        :meth:`_plan_watchdog_check`（它本身不看 offline）。
        """
        if getattr(self, "offline", False):
            return None
        now = time.time() if now is None else float(now)
        last = float(getattr(self, "_plan_watchdog_at", 0.0) or 0.0)
        if now - last < self.PLAN_WATCHDOG_INTERVAL:
            return None
        self._plan_watchdog_at = now
        return self._plan_watchdog_check()

    # -- 检查（状态机） --------------------------------------------------

    def _plan_watchdog_blocked(self):
        """有没有正在进行的问答 —— 有的话这轮什么都别做（和 ``cmd_plan`` 同一套闸门）。"""
        for name in ("planning", "pending_import", "nostart",
                     "pending_interrupt", "pending_soft_interrupt"):
            if getattr(self, name, None) is not None:
                return name
        try:
            if self.review_active():
                return "review"
        except Exception:                          # noqa: BLE001 - 收尾判据坏了别挡路
            return None
        return None

    def _plan_watchdog_check(self):
        """一次完整检查 → 做了什么（``"create"/"fallback"/"close"/None``）。

        纯判定 + 少量写操作：建任务、标完成、排队兜底。**不联网**。
        """
        today = datetime.date.today()
        if getattr(self, "_plan_watchdog_busy", False):
            rl.log_event("plan_watchdog", "skip", level="debug", reason="busy")
            return None
        blocked = self._plan_watchdog_blocked()
        if blocked:
            rl.log_event("plan_watchdog", "skip", level="debug", reason=blocked)
            return None

        try:
            plan_ops = self.projects.plan_ops()
        except Exception as exc:                   # noqa: BLE001 - 项目库坏了不挡路
            rl.log_event("plan_watchdog", "skip", level="debug",
                         reason="projects-error", error="%s: %s"
                         % (type(exc).__name__, exc))
            return None
        if not plan_ops:
            rl.log_event("plan_watchdog", "skip", level="debug", reason="no-ops")
            return None

        allocated = pw.allocated_counts(plan_ops, today)
        try:
            reminders = [task for task in self.task_store.list()
                         if pw.is_reminder(task) and ts.TaskStore.is_open(task)]
        except Exception as exc:                   # noqa: BLE001
            rl.log_event("plan_watchdog", "skip", level="debug",
                         reason="tasks-error", error="%s: %s"
                         % (type(exc).__name__, exc))
            return None
        rl.log_event("plan_watchdog", "check", level="debug",
                     day=today.isoformat(), allocated=allocated,
                     reminders=len(reminders))

        # ① plan 已经有了：提醒任务的使命结束（哪怕它是手排之前留下来的）。
        if allocated > 0:
            if reminders:
                self._plan_watchdog_close(reminders, reason="plan-exists")
                return "close"
            return None

        # ② 昨天/更早留下的提醒任务：场次已经过去，今天不算数 —— 退休，重新看今天。
        stale = []
        for task in reminders:
            end = pw.session_end(task.get("session") or {})
            if end is not None and end.date() < today:
                stale.append(task)
        if stale:
            self._plan_watchdog_close(stale, reason="stale")
            reminders = [task for task in reminders if task not in stale]

        ledger = self._plan_watchdog_ledger()

        # ③ 没有提醒任务：今天有可排空场就建一条（每天最多一条）。
        if not reminders:
            created = (ledger.get("created") or {}).get(today.isoformat())
            if created:
                rl.log_event("plan_watchdog", "skip", level="debug",
                             reason="created-today", task_id=(created or {}).get("task_id"))
                return None
            try:
                pending = self.pending_plan(today)
            except Exception:                      # noqa: BLE001 - 警告板同款兜底
                pending = None
            if not pending:
                # 记下容量口径的解释：以前只有一句 no-free-slots，看不出是
                # 「计划里没空场」还是「容量算漏了」（2026-11-21 的 virgin day
                # bug 就是后者）。
                rl.log_event("plan_watchdog", "skip", level="debug",
                             reason="no-free-slots", allocated=allocated,
                             capacity=0)
                return None
            rl.log_event("plan_watchdog", "pending", level="debug",
                         day=today.isoformat(),
                         counts=int((pending or {}).get("counts") or 0),
                         slots=int((pending or {}).get("sessions") or 0),
                         starts=(pending or {}).get("starts"))
            return self._plan_watchdog_create(today, pending)

        # ④ 提醒任务在、但还没排上场次（enlist 上次失败了）：再试一次。
        unscheduled = [task for task in reminders
                       if not (task.get("session") or {}).get("start")]
        if unscheduled:
            for task in unscheduled:
                self._plan_watchdog_schedule(task, today)
            return "schedule" if len(unscheduled) == len(reminders) else None

        # ⑤ 提醒任务挂在今天、而且那场已经过去 → 兜底自动 plan（每天一次）。
        ended = [task for task in reminders
                 if self._plan_watchdog_ended_today(task, today)]
        if not ended:
            return None
        return self._plan_watchdog_fallback(reminders, today)

    def _plan_watchdog_ended_today(self, task, today):
        end = pw.session_end(task.get("session") or {})
        if end is None or end.date() != today:
            return False
        try:
            now = self._now_dt()
        except Exception:                          # noqa: BLE001
            now = datetime.datetime.now()
        return end <= now

    # -- 建任务 / 排期 / 收尾 --------------------------------------------

    def _plan_watchdog_create(self, today, pending=None):
        fields = pw.reminder_fields()
        task = self.task_store.create(fields, source={"kind": pw.SOURCE_KIND})
        rl.log_event("plan_watchdog", "create", level="info",
                     task_id=task.get("id"), day=today.isoformat(),
                     slots=(pending or {}).get("sessions"),
                     counts=(pending or {}).get("counts"))
        ledger = self._plan_watchdog_ledger()
        ledger.setdefault("created", {})[today.isoformat()] = {
            "task_id": task.get("id"),
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self._plan_watchdog_save_ledger(ledger)
        self._plan_watchdog_schedule(task, today)
        try:
            self.invalidate()
        except Exception:                          # noqa: BLE001 - 画帧缓存坏了不挡路
            pass
        return "create"

    def _plan_watchdog_schedule(self, task, day):
        """用 ``enlist`` 把提醒任务排进当天**最早**还有空位的那一场。"""
        try:
            spec = th.build_spec("formatter", ["enlist"])
        except th.HookError as exc:                # 注册表被改坏才会走到这
            rl.log_event("plan_watchdog", "schedule_failed", level="error",
                         task_id=(task or {}).get("id"), error=str(exc))
            return None
        try:
            task = self._apply_formatter_to_task(task, spec, action="hook",
                                                 day=day, quiet=True)
        except Exception as exc:                   # noqa: BLE001 - 排不上不能吞掉提醒
            rl.log_event("plan_watchdog", "schedule_failed", level="warning",
                         task_id=(task or {}).get("id"), day=day.isoformat(),
                         error="%s: %s" % (type(exc).__name__, exc))
            return None
        session = (task or {}).get("session") or {}
        rl.log_event("plan_watchdog", "scheduled", level="info",
                     task_id=(task or {}).get("id"),
                     day=session.get("date") or day.isoformat(),
                     start=session.get("start"),
                     duration=session.get("duration"))
        # 新切出来的一场得有人订 —— 和命令层一样走唯一出口 book()（offline 不订）。
        if not getattr(self, "offline", False):
            try:
                self.book(day, why="plan 提醒任务排进了最近的场次")
            except Exception as exc:               # noqa: BLE001 - 订不上由 daemon 补
                rl.log_event("plan_watchdog", "book_failed", level="warning",
                             day=day.isoformat(),
                             error="%s: %s" % (type(exc).__name__, exc))
        return task

    def _plan_watchdog_close(self, tasks, reason=""):
        """提醒任务本地标完成（**不勾 Focusmate、不写 OmniFocus**）。"""
        for task in tasks or ():
            task_id = (task or {}).get("id")
            if task_id is None:
                continue
            try:
                self.task_store.complete(task_id,
                                         reason="plan_watchdog:%s" % (reason or "done"))
            except Exception as exc:               # noqa: BLE001 - 一条坏不挡其它
                rl.log_event("plan_watchdog", "close_failed", level="warning",
                             task_id=task_id, reason=reason,
                             error="%s: %s" % (type(exc).__name__, exc))
                continue
            rl.log_event("plan_watchdog", "close", level="info",
                         task_id=task_id, reason=reason)

    # -- 兜底：今天自动 plan -------------------------------------------------

    def _plan_watchdog_fallback(self, reminders, today):
        """「提醒任务那场过去了、今天还没 plan」→ 随机分资源，自动排今天。"""
        ledger = self._plan_watchdog_ledger()
        done = (ledger.get("auto_planned") or {}).get(today.isoformat())
        if done:
            rl.log_event("plan_watchdog", "fallback_skip", level="debug",
                         day=today.isoformat(),
                         reason="already-today", status=(done or {}).get("status"))
            return None

        # 先记账再动手：哪怕下面崩了，今天也不会每 60 秒重试一次。
        ledger.setdefault("auto_planned", {})[today.isoformat()] = {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
        }
        self._plan_watchdog_save_ledger(ledger)
        self._plan_watchdog_busy = True
        rl.log_event("plan_watchdog", "fallback", level="info",
                     day=today.isoformat(),
                     reminders=[(task or {}).get("id") for task in reminders or ()])

        def work():
            status = "error"
            detail = {}
            try:
                detail = self._plan_watchdog_auto_plan(today) or {}
                status = "ok" if int(detail.get("placements") or 0) else "empty"
            except Exception as exc:               # noqa: BLE001 - 兜底失败只记日志
                rl.log_event("plan_watchdog", "fallback_error", level="error",
                             day=today.isoformat(),
                             error="%s: %s" % (type(exc).__name__, exc))
                detail = {"error": "%s: %s" % (type(exc).__name__, exc)}
            finally:
                self._plan_watchdog_busy = False
            ledger_now = self._plan_watchdog_ledger()
            ledger_now.setdefault("auto_planned", {})[today.isoformat()] = {
                "at": datetime.datetime.now().isoformat(timespec="seconds"),
                "status": status,
                "allocations": int(detail.get("allocations") or 0),
                "counts": int(detail.get("counts") or 0),
                "reason": detail.get("reason") or "",
                "error": detail.get("error") or "",
            }
            self._plan_watchdog_save_ledger(ledger_now)
            rl.log_event("plan_watchdog", "fallback_done",
                         level="info" if status == "ok" else "warning",
                         day=today.isoformat(), status=status,
                         allocations=int(detail.get("allocations") or 0),
                         counts=int(detail.get("counts") or 0),
                         reason=detail.get("reason") or "")
            # 提醒任务退休：兜底跑过了（成没成都算它尽力了），不留在过期场次上占位。
            self._plan_watchdog_close(reminders, reason="fallback:%s" % status)
            try:
                self.invalidate()
            except Exception:                      # noqa: BLE001
                pass
            return detail

        jobs = getattr(self, "jobs", None)
        sync = bool(getattr(jobs, "sync", False)) or not bool(getattr(self, "net_async", False))
        if sync or not hasattr(self, "net_after"):
            return work()
        self.net_after(work, label="自动 plan（今天）")
        return "fallback"

    def _plan_watchdog_auto_plan(self, day):
        """今天用随机分配跑一次 ``plan``（和 ``cmd_plan complete`` 同一条流水线）。"""
        # plan 的口径一律按「今天」的表格算；看板停在别的天时先临时切回来，
        # 跑完再切回去（cmd_plan 对别的日子也是这么做的）。
        offset = getattr(self, "day_offset", 0) or 0
        switched = offset != 0
        try:
            if switched:
                self.day_offset = 0
                try:
                    self.reload_periods()
                    self.reload_entries()
                    self.rebuild_rows()
                except Exception:                  # noqa: BLE001 - 快照模式也能排
                    pass
            self.projects.ensure_tagged_pros()
            plan_ops = self.projects.plan_ops()
            if not plan_ops:
                return {"allocations": 0, "counts": 0, "reason": "no-ops"}
            slots, notes = self._plan_slots(day, replan=True)
            for note in notes:
                rl.log_event("plan_watchdog", "note", level="debug", note=str(note))
            if not slots:
                return {"allocations": 0, "counts": 0, "reason": "no-slots"}
            capacity = pp.capacity(slots)
            allocs = pw.random_allocs(plan_ops, capacity.get("counts"),
                                      rng=self._plan_watchdog_random())
            if not allocs:
                return {"allocations": 0, "counts": 0, "reason": "no-allocs"}
            rl.log_event("plan_watchdog", "auto_alloc", level="info",
                         day=day.isoformat(),
                         free_counts=capacity.get("counts"),
                         picks=[(a.get("project_id"), a.get("count")) for a in allocs])
            allocator = pp.Allocator(
                day, plan_ops, slots, task_store=self.task_store,
                hooks=self.plan_hooks(),
                pro_map=self._pro_display_map(plan_ops),
                resolve_task=self._plan_task_ref)
            summary = self._plan_apply(
                allocator, allocs, append_to_plan=False,
                title="自动 plan（今天兜底）")
            return {"allocations": len(allocs),
                    "counts": capacity.get("counts"),
                    "placements": int((summary or {}).get("placements") or 0),
                    "unscheduled": int((summary or {}).get("unscheduled") or 0)}
        finally:
            if switched:
                self.day_offset = offset
                try:
                    self.rebuild_rows()
                except Exception:                  # noqa: BLE001
                    pass
                try:
                    self.invalidate()
                except Exception:                  # noqa: BLE001
                    pass
