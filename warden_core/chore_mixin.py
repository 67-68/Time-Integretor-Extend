"""warden_core.chore_mixin —— 琐事 Project 空闲屏（ChoreView）状态流转与按键交互。"""
import datetime

from .common import (
    Align,
    SRC_OMNIFOCUS,
    Text,
    ks,
    pj,
    sch,
)
from .views import ChoreView


class ChoreMixin(object):
    # -- 琐事 Project 空闲屏 -------------------------------------------------

    def _chore_now_dt(self):
        return self.chore_now or datetime.datetime.now()

    def _chore_active_session(self, now=None):
        """现在是不是正处在 session 里；是就返回那一行/时段，否则 None。

        权威模式（bridge 拉到了真数据）只认真的在进行、且有 session id 的场次；
        离线/快照才退回 ``data.csv`` 的计划窗口 —— 和专注视图用同一套口径。
        """
        now = now or self._chore_now_dt()
        if self.authoritative and self.sel_date == now.date():
            now_ms = int(now.timestamp() * 1000)
            try:
                live = [row for row in self._actual_rows(self.rows)
                        if row.get("startMs") is not None
                        and row["startMs"] <= now_ms < (row.get("endMs") or 0)]
            except Exception:                  # noqa: BLE001 - 画空闲屏不该被行数据弄崩
                live = []
            if live:
                live.sort(key=lambda row: (row.get("startMs") or 0,
                                           self.rows.index(row)))
                return live[-1]
            return None
        try:
            got = ks.current_session(self.entries, now)
        except Exception:                      # noqa: BLE001
            got = None
        if not got:
            return None
        day, slot, start, end = got
        return {"date": day.isoformat(), "startLabel": slot.get("start"),
                "endLabel": end.strftime("%H:%M")}

    def _next_session_start(self, now=None):
        """下一场 session 的开始时间（用来决定 ``q`` 退出后压到什么时候）。

        先看 data.csv 的计划窗口；当前选中的那一天若有 bridge 真数据，也把
        只存在于 Focusmate 的场次算进去。找不到就压到明天零点。
        """
        now = now or self._chore_now_dt()
        starts = []
        for delta in range(0, 8):
            day = now.date() + datetime.timedelta(days=delta)
            try:
                slots = self.plan_rows_for(day)
            except Exception:                  # noqa: BLE001
                slots = []
            for slot in slots:
                try:
                    start = sch.slot_datetime(slot, day)
                except Exception:              # noqa: BLE001
                    continue
                if start > now:
                    starts.append(start)
        if self.authoritative and self.sel_date == now.date():
            now_ms = int(now.timestamp() * 1000)
            for row in self.rows:
                try:
                    if row.get("id") and int(row.get("startMs") or 0) > now_ms:
                        starts.append(datetime.datetime.fromtimestamp(
                            row["startMs"] / 1000.0))
                except (TypeError, ValueError, KeyError, OSError):
                    continue
        if starts:
            return min(starts)
        return datetime.datetime.combine(now.date() + datetime.timedelta(days=1),
                                         datetime.time(0, 0))

    def _suppress_chore_idle(self, now=None):
        """``q`` 退出空闲屏：压到下一场 session 开始（没有就明天）。"""
        now = now or self._chore_now_dt()
        self.chore_suppressed_until = self._next_session_start(now)
        self.chore_last_note = ""
        self.invalidate()

    def _on_view_change(self, view):
        """记下用户什么时候手动离开琐事屏 —— 同一段空档别再自动弹回来。"""
        try:
            previous_index = self._chore_last_view_index
            previous = (self.view_impls[previous_index]
                        if 0 <= previous_index < len(self.view_impls) else None)
            current_index = self.shell.view_index
            if isinstance(view, ChoreView) and not isinstance(previous, ChoreView):
                # 进入琐事屏时记住来路；q / 自动退出都回到这里。
                self._chore_return_index = previous_index
            if (isinstance(previous, ChoreView) and not isinstance(view, ChoreView)
                    and not self._chore_leaving):
                self._suppress_chore_idle()
            self._chore_last_view_index = current_index
        except Exception:                      # noqa: BLE001 - 切视图不能因为这里坏掉
            pass

    def chore_binding(self):
        """设置里的琐事项目 → ``(project, error)``。"""
        raw = str(self.settings.get("chore-project") or "").strip()
        if not raw:
            return None, ("还没绑定「琐事 Project」：先 `project` 看项目序号，"
                          "再 `set chore-project #1`（或项目名 / OmniFocus pk）")
        try:
            return self.projects.find(raw), ""
        except ValueError as e:
            return None, str(e)

    def chore_project_tasks(self, project=None):
        """绑定项目里可做的一条条任务；父壳展开成其未完成子任务。"""
        if project is None:
            project, _err = self.chore_binding()
        if not project:
            return []
        try:
            invested = pj.invested(self.task_store, project)
            items = pj.remaining_tasks(project, invested)
        except Exception:                      # noqa: BLE001 - 坏项目不该炸空闲屏
            return []
        out = []
        for item in items:
            children = item.get("children") or []
            if children:
                for child in children:
                    rec = dict(child)
                    rec["_parent_title"] = item.get("title")
                    out.append(rec)
            else:
                out.append(item)
        return out

    def chore_snapshot(self):
        """空闲屏当前该显示什么：项目 / 任务列表 / 当前任务 / 错误说明。"""
        project, error = self.chore_binding()
        tasks = self.chore_project_tasks(project) if project else []
        if project and not tasks:
            error = ("琐事项目「%s」里没有未完成任务了 —— 去 OmniFocus 加一条，"
                     "或换一个 `chore-project`" % (project.get("title") or project.get("id")))
        if not tasks:
            return {"project": project, "tasks": [], "task": None, "index": 0,
                    "error": error}
        index = self.chore_index % len(tasks)
        return {"project": project, "tasks": tasks, "task": tasks[index],
                "index": index, "error": error}

    def _chore_center(self, body):
        """把琐事内容放在整个终端画布的正中央（水平 + 垂直居中）。"""
        body.justify = "center"
        try:
            height = int(self.shell.dashboard_budget())
        except Exception:                          # noqa: BLE001
            height = 20
        try:
            width = int(self.frame_width())
        except Exception:                          # noqa: BLE001
            width = 80
        return Align.center(body, vertical="middle",
                            height=max(6, height), width=max(24, width))

    def render_chore_idle(self):
        """琐事屏的全部内容：一条任务 + m/c/q 提示，在屏幕正中央居中展示。"""
        snap = self.chore_snapshot()
        body = Text(justify="center")
        if self.chore_last_note:
            body.append(self.chore_last_note + "\n\n", style="bold green")
        if not snap.get("task"):
            body.append("琐事 Project\n\n", style="bold")
            body.append((snap.get("error") or "现在没有可显示的琐事。") + "\n\n",
                        style="yellow")
            body.append("q 退出（回到正常界面）", style="cyan")
            return self._chore_center(body)
        task = snap["task"]
        project = snap.get("project") or {}
        body.append("琐事\n", style="dim")
        body.append(str(task.get("title") or task.get("pk") or "（无标题）"),
                    style="bold")
        body.append("\n\n", style="")
        body.append("项目：%s" % (project.get("title") or "?"), style="dim")
        bits = []
        if task.get("duration"):
            bits.append("%d 分钟" % int(task["duration"]))
        if task.get("count"):
            bits.append("%d count" % int(task["count"]))
        if task.get("due"):
            bits.append("截止 %s" % str(task["due"]).replace("T", " ")[:16])
        if task.get("_parent_title"):
            bits.append("父任务：%s" % task["_parent_title"])
        if bits:
            body.append("\n" + " · ".join(bits), style="dim")
        body.append("\n\n", style="")
        body.append("m 完成    c 换一个 task    q 退出（回到正常界面）", style="cyan")
        return self._chore_center(body)

    def _enter_chore_view(self):
        for index, view in enumerate(self.view_impls):
            if isinstance(view, ChoreView):
                self.chore_last_note = ""
                self.invalidate()
                self.shell.set_view(index)
                return True
        return False

    def _leave_chore_view(self, suppress=False):
        if not isinstance(self.view, ChoreView):
            return False
        if suppress:
            self._suppress_chore_idle()
        target = self._chore_return_index
        if (target < 0 or target >= len(self.view_impls)
                or isinstance(self.view_impls[target], ChoreView)):
            target = 0
        self._chore_leaving = True
        try:
            self.shell.set_view(target)
        finally:
            self._chore_leaving = False
        self.chore_last_note = ""
        self.invalidate()
        return True

    def _maybe_sync_chore_mode(self):
        """每帧看一眼：进/出「没有 session」的琐事屏。

        没有 session 时整屏接管（从哪个视图来都会接管，q 回原视图）；但收尾、
        plan 问答、项目对账、导入问答、罪案锁死这些打断态优先，不抢输入。
        """
        now = self._chore_now_dt()
        if isinstance(self.view, ChoreView):
            if self._chore_active_session(now) is not None:
                self._leave_chore_view(suppress=False)
            return False
        if (self.review_active() or self.nostart is not None
                or self.planning is not None or self.pending_import is not None
                or self.pending_advance is not None or self.pending_op_type
                or self.project_sync is not None or self.pending_sins()):
            return False
        if (self.chore_suppressed_until is not None
                and now < self.chore_suppressed_until):
            return False
        if self._chore_active_session(now) is not None:
            return False
        project, _error = self.chore_binding()
        if not project or not self.chore_project_tasks(project):
            return False
        return self._enter_chore_view()

    def _chore_finish_writeback(self, project, task, kind="完成"):
        """项目任务完成后写回 OmniFocus（单向联动；off 就只改本地）。"""
        pk = str((task or {}).get("pk") or "").strip()
        if not pk:
            return ""
        if not self.omnifocus_enabled():
            return "OmniFocus 关着 → 只改了本地"
        action = self.net_action(
            "omnifocus.complete",
            "OmniFocus %s琐事 %s" % (kind, pk[:8]),
            {"pk": pk, "at": None, "due": task.get("due") or None},
            source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self.say(
            "琐事「%s」写回 OmniFocus 失败：%s"
            % (task.get("title") or pk, error), "warn"))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 写回失败：%s" % (
                    outcome and outcome.error or "没跑")
            return "OmniFocus 已写回"
        return "OmniFocus 写回丢后台了"

    def _chore_complete(self):
        snap = self.chore_snapshot()
        task, project = snap.get("task"), snap.get("project")
        if not task or not project:
            self.chore_last_note = "现在没有可完成的琐事。"
            self.invalidate()
            return False
        try:
            updated, done_task, auto_parent = self.projects.complete_task(
                project["id"], task.get("pk"))
        except (ValueError, TypeError) as e:
            self.chore_last_note = "完成失败：%s" % e
            self.invalidate()
            return False
        notes = ["✅ 已完成：%s" % (done_task.get("title") or done_task.get("pk"))]
        note = self._chore_finish_writeback(updated, done_task)
        if note:
            notes.append(note)
        if auto_parent is not None:
            notes.append("父任务「%s」的子任务全部完成 → 父任务自动完成"
                         % (auto_parent.get("title") or auto_parent.get("pk")))
            note = self._chore_finish_writeback(updated, auto_parent, kind="父任务")
            if note:
                notes.append(note)
        self.chore_last_note = "；".join(notes)
        self.invalidate()
        return True

    def _chore_next(self):
        snap = self.chore_snapshot()
        count = len(snap.get("tasks") or [])
        if count <= 1:
            self.chore_last_note = "只有这一条琐事，没有别的可换。"
        else:
            self.chore_index = (self.chore_index + 1) % count
            self.chore_last_note = ""
        self.invalidate()
        return True

    def _handle_chore_input(self, line):
        """琐事屏只吃 m / c / q；返回 True = 已处理。"""
        if not isinstance(self.view, ChoreView):
            return False
        cmd = str(line or "").strip().lower()
        if cmd in ("q", "quit", "exit"):
            self._leave_chore_view(suppress=True)
        elif cmd in ("m", "done", "完成"):
            self._chore_complete()
        elif cmd in ("c", "next", "换", "换一个"):
            self._chore_next()
        elif not cmd:
            return True
        else:
            self.chore_last_note = "琐事屏只认 m（完成）/ c（换一个）/ q（退出）。"
            self.invalidate()
        return True
