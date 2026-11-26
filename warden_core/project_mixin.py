"""warden_core.project_mixin —— 项目表与 Day Binder 渲染、pro/op 命令、relate 导入导出、每日项目对账与 plan 排期。"""
import datetime
import json
import os
import re
import time

from .common import (
    HERE,
    PLAN_DECISIONS_FILE,
    PLAN_PINS_FILE,
    Panel,
    SRC_OMNIFOCUS,
    STYLE_MUTED,
    Table,
    Text,
    box,
    dv,
    of,
    parse_relate_markdown,
    parse_relate_task,
    ks,
    pj,
    pp,
    repl_kit,
    rl,
    sch,
    sp,
    th,
    ts,
)
from .views import DataView


class ProjectMixin(object):
    # -- 项目 / plan ----------------------------------------------------
    #
    # 项目是「活的母体」：导入时把 OmniFocus 项目下的任务清单快照进项目库，但
    # **不建本地任务、不上任务面板**；等到 `plan` 给它分了 count，才把当天的任务
    # 「唤起」成本地任务、排进 session（用户原话：*在项目入库的时候就应该获取他
    # 所有的 task，但不展示直到他们被 project 唤起*）。

    def project_rows(self, day=None):
        """OmniFocus Project（op）预览行；``op`` 命令仍需要看到全部 op。"""
        return self.projects.preview(day or self.sel_date, self.task_store)

    def current_project_rows(self, day=None):
        """契约视图用的项目行：每个 Pro 一行，账只算**当前活跃侧**。

        2026-11-21 起一个 Pro 只有一个 op，SOP / incubator 是任务标签：
        ``running`` 只算 SOP 侧，``bottleneck`` 只算 incubator 侧；另一侧的任务
        显示成「+N 冻结」。未关联真正项目 ``pro`` 的孤立 op 不在这里出现
        （仍可在 ``op`` 命令里管理）。
        """
        day = day or self.sel_date
        op_rows = {row["id"]: row for row in self.project_rows(day)}
        out = []
        for pr in self.projects.pro_preview(day, self.task_store):
            if (pr.get("pro") or {}).get("simple"):
                out.append(self._simple_project_row(pr, day))
                continue
            op = pr.get("active_op")
            if op is None:
                continue
            row = dict(op_rows.get(op["id"]) or {})
            side = pr.get("active_side") or pj.side_for_stage(pr["stage"])
            other = (pj.OP_TYPE_SOP if side == pj.OP_TYPE_INCUBATOR
                     else pj.OP_TYPE_INCUBATOR)
            default_side = pj.normalize_op_type(op.get("op_type"))
            # 一个项目的 invested 只算一次：`stats()` 不传 invested_map 时会自己
            # 调一次 `invested()`（重扫 tasks.json，实测 2.9ms/次），两侧各调一次
            # 就是白算一遍（10 个项目 → 约 58ms）。
            invested_map = pj.invested(self.task_store, op)
            stat = pj.stats(op, self.task_store, invested_map=invested_map, side=side,
                            default_side=default_side)
            other_stat = pj.stats(op, self.task_store, invested_map=invested_map,
                                  side=other, default_side=default_side)
            open_tasks = [t for t in (op.get("tasks") or ())
                          if not t.get("completed") and not t.get("dropped")
                          and not t.get("effective_completed")
                          and not t.get("effective_dropped")]
            frozen = [t for t in open_tasks if pj.task_is_frozen(t)]
            untagged = [t for t in open_tasks
                        if pj.side_of_tags(t.get("tags") or ()) is None
                        and default_side is None]
            row.update({
                "number": pr["number"],
                "pro_id": pr["id"],
                "pro_number": pr["number"],
                "op_number": row.get("number"),
                "pro": pr["pro"],
                "pro_title": self.pro_display_title(pr, fallback=pr["title"]),
                "pro_title_raw": pr["title"],
                "pro_stage": pr["stage"],
                "effective_stage": pr["stage"],
                "stage_mismatch": False,
                "active_side": side,
                "active_op": op,
                "remaining": stat["remaining"],
                "remaining_high": stat["remaining_high"],
                "remaining_text": stat["remaining_text"],
                "progress": stat["progress"],
                "progress_text": stat["progress_text"],
                "other_remaining": other_stat["remaining"],
                "other_side": other,
                "frozen_count": len(frozen),
                "untagged": [t.get("title") or t.get("pk") for t in untagged],
                "legacy_extra_ops": pr.get("legacy_extra_ops") or [],
            })
            out.append(row)
        return out

    def _simple_project_row(self, pr, day):
        """简单项目的项目行：容量 ∞，不算清单、不报未打标。"""
        view = pj.simple_project_view(pr.get("pro") or {}, stage=pr.get("stage"))
        invested_map = pj.invested(self.task_store, view)
        stat = pj.stats(view, self.task_store, invested_map)
        plan = pj.plan_of(view, day)
        return {
            "number": pr["number"],
            "pro_id": pr["id"],
            "pro_number": pr["number"],
            "op_number": None,
            "pro": pr.get("pro"),
            "pro_title": self.pro_display_title(pr, fallback=pr.get("title")),
            "pro_title_raw": pr.get("title"),
            "pro_stage": pr.get("stage"),
            "effective_stage": pr.get("stage"),
            "stage_mismatch": False,
            "active_side": None,
            "active_op": None,
            "simple": True,
            "remaining": stat["remaining"],
            "remaining_high": 0,
            "remaining_text": stat["remaining_text"],
            "progress": stat["progress"],
            "progress_text": stat["progress_text"],
            "other_remaining": 0,
            "other_side": None,
            "frozen_count": 0,
            "untagged": [],
            "legacy_extra_ops": [],
            "tasks_open": 0,
            "week_count": 0,
            "week_missing": [],
            "allocated": int((plan or {}).get("count") or 0),
            "scheduled": pj.planned_counts(view, day),
        }

    def project_warning_scan(self):
        """警告板要的那两项：旧双 op / 没打侧别标签的任务（**不算账**）。

        `current_project_rows()` 顺手把每个 pro 两侧的账都算了（每侧一次
        `stats()`），可警告只问两件事：「还有旧双 op 吗」「有任务没打
        SOP/incubator 吗」。为这两句话去算 10 个项目 × 两侧的 remaining /
        progress，实测白烧 79ms（每帧 / 每 5 秒一次），所以单独给一条轻量通道：
        只扫一遍 op 的任务，不看 invested、不算 count。
        """
        out = []
        for pr in self.projects.pro_preview(self.sel_date, self.task_store):
            if (pr.get("pro") or {}).get("simple"):
                # 简单项目不查清单、不要求打标 —— 绝不告警。
                continue
            op = pr.get("active_op")
            if op is None:
                continue
            default_side = pj.normalize_op_type(op.get("op_type"))
            untagged = [
                t.get("title") or t.get("pk")
                for t in (op.get("tasks") or ())
                if not t.get("completed") and not t.get("dropped")
                and not t.get("effective_completed") and not t.get("effective_dropped")
                and pj.side_of_tags(t.get("tags") or ()) is None
                and default_side is None
            ]
            out.append({
                "pro_title": self.pro_display_title(pr, fallback=pr["title"]),
                "untagged": untagged,
                "legacy_extra_ops": pr.get("legacy_extra_ops") or [],
            })
        return out

    def pro_display_title(self, pro_or_id, fallback=None):
        """真正项目名的展示版本：还有待检查软中断时前面加 ❓。

        ``pro_or_id`` 可以是 pro 记录、id 或名称；找不到时原样返回 fallback。
        待检查（pending）和已入队（queued）都会显示；生成任务或丢弃后消失。
        """
        pro = pro_or_id
        if not isinstance(pro, dict):
            try:
                pro = self.projects.resolve_pro(pro_or_id)
            except (ValueError, TypeError):
                pro = None
        title = str((pro or {}).get("title") or fallback or "").strip()
        if pro is None or not title:
            return title
        try:
            pending = self.projects.soft_interrupt_count(pro.get("id"))
        except Exception:                          # noqa: BLE001 - 展示失败别挡面板
            pending = 0
        return ("❓" + title) if pending else title

    def _pro_display_map(self, ops):
        """``op id -> pro`` 展示副本；带 ``display_title``，供 plan 面板使用。"""
        out = {}
        for op in ops or ():
            if (op or {}).get("simple"):
                pro = self.projects.pro_by_id(op.get("pro_id"))
            else:
                pro = self.projects.pro_for_op(op)
            if pro is None:
                continue
            shown = dict(pro)
            shown["display_title"] = self.pro_display_title(
                pro, fallback=pro.get("title"))
            out[op["id"]] = shown
        return out

    def _task_project_label(self, task):
        """任务 → 一句项目来源（项目名 / OmniFocus pk / count）。"""
        fields = (task or {}).get("fields") or {}
        source = (task or {}).get("source") or {}
        pid = fields.get(pj.PROJECT_ID_FIELD)
        title = str(fields.get(pj.PROJECT_FIELD) or "").strip()
        if not title and pid is not None:
            try:
                project = self.projects.by_id(pid)
                title = (project or {}).get("title") or ""
            except Exception:                  # noqa: BLE001 - 显示失败别挡面板
                title = ""
        if not title and str(source.get("kind") or "") == "project":
            title = str(source.get("title") or "").strip()
        if pid is not None:
            try:
                op = self.projects.by_id(pid)
                pro = self.projects.pro_for_op(op) if op else None
                if pro is not None:
                    title = self.pro_display_title(pro, fallback=title)
            except Exception:                  # noqa: BLE001 - 展示失败别挡面板
                pass
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        count = fields.get("count")
        if not title and not pk:
            return ""
        bits = []
        if title:
            bits.append("项目:%s" % title)
        if pk:
            bits.append(str(pk)[:12])
        if count:
            try:
                bits.append("%d count" % int(count))
            except (TypeError, ValueError):
                pass
        return " · ".join(bits)

    def render_day_binder(self):
        """未来某一天「session ← 任务 ← formatter / 项目」的编排总览。

        用户看明天的 session 时，不只想看空白场次：还想知道哪一场已经被
        ``end`` / ``enlist`` / ``repeat`` 占了、挂的是哪条任务、这条任务属于哪个项目。
        这张表把这些信息放在**同一行**，方便决定剩下的空场怎么分给项目。

        只读、只画；不改任何排期。今天不画（契约表本身已经是当天执行面）。
        """
        day = self.sel_date
        if day <= datetime.date.today():
            return None
        iso = day.isoformat()
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            sessions = []
        try:
            tasks = self.task_store.list()
        except Exception:                          # noqa: BLE001
            tasks = []

        entries = {}

        def entry(start, end="", duration=None):
            key = str(start or "")
            rec = entries.get(key)
            if rec is None:
                rec = {"start": key, "end": str(end or ""), "duration": duration,
                       "tasks": [], "plans": []}
                entries[key] = rec
            elif not rec.get("duration") and duration:
                rec["duration"] = duration
            return rec

        for sess in sessions:
            entry(sess.get("start"), sess.get("end"),
                  int(sess.get("duration") or 0) or None)

        # 本地任务：正常绑定在 session 上的，以及 formatter reservation 落在这一天的。
        for task in tasks:
            sess = task.get("session") or {}
            fmt = task.get("formatter") or {}
            res = fmt.get("reservation") or {}
            if str(sess.get("date") or "") == iso and sess.get("start"):
                start = str(sess.get("start"))
            elif str(res.get("date") or "") == iso and res.get("start"):
                start = str(res.get("session_start") or res.get("start"))
            else:
                continue
            rec = entry(start, sess.get("end") or res.get("end"),
                        int(sess.get("duration") or 0) or None)
            if not any(t.get("id") == task.get("id") for t in rec["tasks"]):
                rec["tasks"].append(task)

        # 项目 plan items：即使本地 task 已丢失，也把「项目原本怎么排的」露出来。
        # 只取每个 pro 当前阶段的 op；旧阶段的 plan 记录不再占屏。
        try:
            projects = self.projects.plan_ops()
        except Exception:                          # noqa: BLE001
            projects = []
        for project in projects:
            plan = pj.plan_of(project, day) or {}
            for item in plan.get("items") or []:
                rec = entry(item.get("start"), duration=item.get("duration"))
                rec["plans"].append({"project": project, "item": item})

        if not entries:
            return None

        t = Table(title="Day Plan · %s %s 编排（session ← 任务 / formatter / 项目）"
                        % (iso, "明天" if day == datetime.date.today()
                           + datetime.timedelta(days=1) else sch.cn_weekday_of_day(day)),
                  box=box.SIMPLE_HEAVY, header_style="bold cyan",
                  title_justify="left", expand=False)
        t.add_column("时间", no_wrap=True)
        t.add_column("时长", justify="right", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("任务 / formatter", overflow="fold")
        t.add_column("项目安排", overflow="fold")

        # 当天 API 行：只看 start，订没订上以 id 为准。
        rows_by_start = {}
        for row in self.rows or ():
            if row.get("date") != iso or not row.get("planned"):
                continue
            rows_by_start.setdefault(str(row.get("startLabel") or ""), row)

        def sort_key(key):
            got = th._clock_minutes(key)
            return (got if got is not None else 24 * 60 + 1, key)

        for start in sorted(entries, key=sort_key):
            rec = entries[start]
            row = rows_by_start.get(start) or {}
            if row.get("id"):
                status = Text("已订", style="bold green")
            elif rows_by_start:
                status = Text("未订", style="yellow")
            else:
                status = Text("仅本地", style="dim")
            task_lines = []
            for task in rec["tasks"]:
                title = self.task_store.focusmate_title(task, max_len=48) or "（无标题）"
                bits = [self.task_mark(task), title]
                fmt = task.get("formatter") or {}
                if fmt.get("name"):
                    label = "⏱%s" % fmt.get("name")
                    placement = (fmt.get("reservation") or {}).get("placement")
                    if placement:
                        label += "(%s)" % placement
                    bits.append(label)
                if th.is_exclusive(task):
                    bits.append("🔒")
                task_lines.append(" ".join(bits))
            if not task_lines:
                task_lines = [Text("空闲", style=STYLE_MUTED)]
            project_lines = []
            for task in rec["tasks"]:
                label = self._task_project_label(task)
                if label:
                    project_lines.append(label)
            for plan in rec["plans"]:
                item = plan.get("item") or {}
                project = plan.get("project") or {}
                label = "plan:%s" % (project.get("title") or project.get("id") or "?")
                if item.get("pk"):
                    label += " · %s" % str(item.get("pk"))[:12]
                if item.get("count"):
                    label += " · %s count" % item.get("count")
                project_lines.append(label)
            # 任务/项目行可能重复（本地任务本身带项目来源），去重但保持顺序。
            seen = set()
            project_lines = [x for x in project_lines
                             if not (x in seen or seen.add(x))]
            dur = rec.get("duration")
            t.add_row(Text("%s%s" % (start, ("–" + rec["end"]) if rec.get("end") else ""),
                           style="bold"),
                      Text(str(dur) if dur else "?"),
                      status,
                      Text("\n".join(str(x) if not isinstance(x, Text) else x.plain
                                     for x in task_lines)),
                      Text("\n".join(project_lines) if project_lines else "—",
                           style=STYLE_MUTED))

        # 项目当天的资源账：说好多少 / 已经落地多少。放 caption，避免再占一张表。
        alloc_bits = []
        for project in projects:
            plan = pj.plan_of(project, day) or {}
            if not plan:
                continue
            planned = sum(int(item.get("count") or 0)
                          for item in plan.get("items") or [])
            alloc_bits.append("「%s」说好 %s count · 已排 %s"
                              % (project.get("title") or project.get("id"),
                                 int(plan.get("count") or 0), planned))
        if alloc_bits:
            t.caption = Text("项目安排：" + "；".join(alloc_bits), style="dim")
        return t

    # -- 简单项目的「这一场」清单框 -------------------------------------

    def _live_simple_project(self):
        """当前这一场挂着简单项目的虚拟任务 → ``(pro, op)``；没有就 None。

        不联网：只看本地任务的 session 时间窗（进行中/正在这一场）。
        """
        now = self._now_dt()
        today = now.date().isoformat()
        mins = now.hour * 60 + now.minute
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if not sess.get("start") or str(sess.get("date") or "") != today:
                continue
            start = th._clock_minutes(sess.get("start"))
            if start is None:
                continue
            if not (start <= mins < start + int(sess.get("duration") or 0)):
                continue
            pid = (task.get("fields") or {}).get(pj.PROJECT_ID_FIELD)
            if not pj.is_simple_project_id(pid):
                continue
            pro = self.projects.pro_by_id(pj.pro_id_from_simple(pid))
            if pro is None:
                continue
            ops = self.projects.ops_for_pro(pro)
            return pro, (ops[0] if ops else None)
        return None

    def render_simple_project_panel(self):
        """当前 session 是简单项目的排期时，画它 OmniFocus 项目里的任务。

        简单项目排的是一条**项目级虚拟任务**（名字=项目名），具体做什么还得看
        它关联的 OmniFocus 清单 —— 所以这一场进行中时，把那份清单原样摆出来
        （不排序、不按侧别过滤，因为简单项目本来就不排序）。

        「没有就不展示」：没关联 op、或 op 下一条未完成任务都没有 → ``None``，
        整块不画。
        """
        found = self._live_simple_project()
        if found is None:
            return None
        pro, op = found
        if op is None:
            return None
        rows = [task for task in (op.get("tasks") or ())
                if not task.get("completed") and not task.get("dropped")
                and not task.get("effective_completed")
                and not task.get("effective_dropped")]
        if not rows:
            return None
        table = Table(title="Simple · 简单项目「%s」这一场干什么（清单来自 OmniFocus，"
                            "不排序、不按侧别过滤）" % (pro.get("title") or "?"),
                      box=box.SIMPLE, header_style="bold magenta",
                      title_justify="left", expand=False)
        table.add_column("#", justify="right", style="bold", no_wrap=True)
        table.add_column("任务", overflow="crop")
        table.add_column("count", justify="right", no_wrap=True)
        table.add_column("due", justify="right", no_wrap=True, style="dim")
        for index, task in enumerate(rows[:12], 1):
            table.add_row(str(index),
                          Text(str(task.get("title") or task.get("pk") or "?"),
                               overflow="crop"),
                          str(int(task.get("count") or 0) or "—"),
                          str(task.get("due") or "—")[:16])
        if len(rows) > 12:
            table.caption = "…还有 %d 条（`pro %s` 看全部）" % (
                len(rows) - 12, self.projects.pro_number_of(pro["id"]) or pro["id"])
        else:
            table.caption = "虚拟任务只占场；具体做什么从这份清单里挑。"
        return table

    def render_projects_table(self):
        """契约视图里**任务面板下面**那块项目表。

        每个真正项目 ``pro`` 一行，只算**当前活跃侧**的账：``running`` 算 SOP
        任务，``bottleneck`` 算 incubator 任务。另一侧（已打 ``manual_defer``
        标签）显示成「+N 冻结」；defer 由用户自己在 OmniFocus 里排除。
        """
        rows = self.current_project_rows()
        if not rows:
            return None
        t = Table(title="Project · 项目（一个项目一个 op；SOP / incubator 是任务标签）",
                  box=box.SIMPLE_HEAVY, header_style="bold blue",
                  title_justify="left", expand=False)
        t.add_column("#", justify="right", style="bold", no_wrap=True)
        t.add_column("项目 / 当前 op", no_wrap=True, overflow="crop")
        t.add_column("阶段 · 活跃侧", no_wrap=True)
        t.add_column("排序", no_wrap=True)
        t.add_column("剩余 count", justify="right", no_wrap=True)
        t.add_column("进度", justify="right", no_wrap=True)
        t.add_column("今天 plan", no_wrap=True)
        t.add_column("未完成", justify="right", no_wrap=True)
        t.add_column("本周", justify="right", no_wrap=True)
        t.add_column("OmniFocus", no_wrap=True, style=STYLE_MUTED)
        for row in rows:
            plan_txt = "—"
            if row.get("allocated"):
                plan_txt = "说好 %d count" % row["allocated"]
                if row.get("scheduled"):
                    plan_txt += " · 已排 %d" % row["scheduled"]
            elif row.get("scheduled"):
                plan_txt = "已排 %d count" % row["scheduled"]
            stage = row.get("pro_stage") or "running"
            side = row.get("active_side") or pj.side_for_stage(stage)
            if row.get("simple"):
                type_txt = "简单 · 容量 ∞"
                type_style = "bold magenta"
                task_type = "项目级虚拟任务"
            else:
                type_txt = "%s · %s" % (stage, side)
                type_style = ("magenta" if side == pj.OP_TYPE_INCUBATOR
                              else "cyan")
                task_type = pj.task_type_of(row)
            task_type_style = ("yellow" if task_type == pj.TASK_TYPE_SEQUENTIAL
                               else "cyan")
            op_title = ("—" if row.get("simple")
                        else ((row.get("active_op") or {}).get("title") or "?"))
            remain_txt = str(row.get("remaining_text")
                             or row.get("remaining") or 0)
            frozen = int(row.get("frozen_count") or 0)
            if frozen:
                remain_txt += "（+%d 冻结）" % frozen
            untagged_n = len(row.get("untagged") or ())
            if untagged_n and not int(row.get("remaining") or 0):
                # 「剩余 0 count」+「未完成 4 条」看起来像账算不出来：那 4 条没打
                # SOP / incubator 标签，不属于任何一侧。把差额直接写在格子里。
                remain_txt += "（%d 条未打标）" % untagged_n
            t.add_row(str(row["number"]),
                      Text("%s → %s" % (row.get("pro_title") or "?", op_title),
                           overflow="crop"),
                      Text(type_txt, style=type_style),
                      Text(task_type, style=task_type_style),
                      Text(remain_txt, style="bold cyan"),
                      Text(row.get("progress_text") or "—", style=STYLE_MUTED),
                      Text(plan_txt, style="green" if row.get("allocated") else STYLE_MUTED),
                      Text(str(row.get("tasks_open") or 0), style=STYLE_MUTED),
                      Text(str(row.get("week_count") or 0),
                           style="bold yellow" if row.get("week_missing")
                           else STYLE_MUTED),
                      Text(str(row.get("pk") or "")[:10] or "—"))
        total_remaining = sum(int(r.get("remaining") or 0) for r in rows)
        total_high = sum(int(r.get("remaining_high") or r.get("remaining") or 0)
                         for r in rows)
        left_txt = ("%d-%d" % (total_remaining, total_high)
                    if total_high > total_remaining else str(total_remaining))
        untagged = sum(len(r.get("untagged") or ()) for r in rows)
        legacy = sum(1 for r in rows if r.get("legacy_extra_ops"))
        extra = ""
        if untagged:
            extra += " · ⚠ %d 条任务没有 SOP/incubator 标签（不进 plan）" % untagged
        if legacy:
            extra += " · %d 个项目还是双 op，跑 `pro <序号> merge` 合并" % legacy
        t.caption = ("%d 个活跃项目 · 剩余 %s count（%d 分钟）· 只算当前阶段那一侧；"
                     "另一侧任务打 manual_defer 标签（不写 defer）· 排序列 = 该项目检测到的 "
                     "OmniFocus 规则（sequential / parallel）· `plan` 也按同一份清单分配"
                     "%s"
                     % (len(rows), left_txt,
                        total_remaining * pp.MINUTES_PER_COUNT, extra))
        return t

    @staticmethod
    def _mark_word(token):
        """返回 mark 状态；``complete`` / ``discard`` 等走和任务同一套词。"""
        return pj.normalize_status(str(token or "").lstrip("="))

    def _extract_mark_action(self, toks):
        """从 pro / op 参数里摘出 ``-m complete`` / ``--mark discard``。

        返回 ``(status, remaining_tokens)``。支持三种位置：

            project 1 -m complete
            project -m complete 1
            project 1 complete        # 省略 -m 的简写

        只有 ``complete / discard / dropped / completed`` 等状态词能作为裸第二
        参数；``project create complete`` 这种创建命令不会被误判。
        """
        raw = [str(t).strip() for t in (toks or []) if str(t).strip()]
        status = None
        kept = []
        i = 0
        while i < len(raw):
            tok = raw[i]
            low = tok.lower()
            if low in ("-m", "--mark", "mark", "状态"):
                if i + 1 >= len(raw):
                    raise ValueError("-m/--mark 后面要跟 complete 或 discard"
                                     "（例如 project 1 -m complete）")
                status = self._mark_word(raw[i + 1])
                if status is None:
                    raise ValueError("--mark 只认 active / complete / discard；"
                                     "你给的是 %r" % raw[i + 1])
                i += 2
                continue
            if low.startswith("--mark=") or low.startswith("-m="):
                val = tok.split("=", 1)[1]
                status = self._mark_word(val)
                if status is None:
                    raise ValueError("--mark 只认 active / complete / discard；"
                                     "你给的是 %r" % val)
                i += 1
                continue
            kept.append(tok)
            i += 1
        if status is None and len(kept) >= 2:
            first_low = kept[0].lower()
            action_words = ("create", "new", "add", "check", "refresh", "sync",
                            "reload", "import", "新建", "创建", "对账", "更新", "导入")
            direct = self._mark_word(kept[1])
            if (first_low not in action_words and direct is not None
                    and direct != pj.STATUS_ACTIVE):
                status = direct
                kept = [kept[0]] + kept[2:]
        return status, kept

    def _finish_mark_status(self, kind, record, status):
        """pro / op mark 完成后的统一回执。"""
        status = pj.normalize_status(status) or status
        number = (self.projects.pro_number_of(record["id"])
                  if kind == "pro" else self.projects.number_of(record["id"]))
        number = number or record["id"]
        name = record.get("title") or "?"
        zh = {"active": "active（恢复）", "completed": "completed（已完成）",
              "dropped": "dropped（已放弃）"}.get(status, status)
        self.say("✅ %s #%s「%s」状态已标为 %s；只动本地账，不碰 OmniFocus。"
                 % ("项目 pro" if kind == "pro" else "OmniFocus op",
                    number, name, zh), "ok")
        if status != pj.STATUS_ACTIVE:
            self.say("   数据视图默认 filter=disposable 会隐藏它；"
                     "敲 `filter all`（或 filter completed / dropped）可查看。", "info")
        else:
            self.say("   数据视图默认 filter=disposable 会重新显示它。", "info")
        self.invalidate()
        return record

    @staticmethod
    def _is_advance_flag(token):
        low = str(token or "").strip().lower()
        return low in ("-a", "--advance", "—advance", "advance", "切换", "推进")

    def _ensure_pro_for_token(self, token):
        """解析 ``pro <序号|名称>``；若还没有创建对应的 ``pro`` 但存在同序号/同名 ``op``，
        自动用该 ``op`` 初始化一条 ``pro``，让 ``pro 1 -a`` / ``pro 1 file ...`` 直接可用。"""
        self.projects.ensure_tagged_pros()
        try:
            return self.projects.resolve_pro(token)
        except ValueError:
            pass
        # 退回看看是不是指着某个 op 的序号或名字
        try:
            op = self.projects.find(token)
        except ValueError:
            # 既不是已有 pro 也不是已有 op：直接抛出 resolve_pro 的错误
            return self.projects.resolve_pro(token)
        existing = self.projects.pro_for_op(op)
        if existing is not None:
            return existing
        # op 指向的 pro 可能已经被 mark completed/dropped；此时 pro_for_op()
        # 只认 active 会返回 None，但绝不能因此新建同名 pro 把状态复活。
        if op.get("pro_id") is not None:
            linked = self.projects.pro_by_id(op["pro_id"])
            if linked is not None:
                return linked
        title = op.get("project_tag") or op.get("title") or ("项目 %s" % op["id"])
        stage = (pj.STAGE_BOTTLENECK
                 if pj.normalize_op_type(op.get("op_type")) == pj.OP_TYPE_INCUBATOR
                 else pj.STAGE_RUNNING)
        pro, _created = self.projects.create_pro(
            title, stage=stage, op_id=op["id"])
        return pro

    # -- 单项目模型：侧别冻结 / push / pop（2026-11-21） ------------------

    def _merge_all_pros(self, tail_tokens=None):
        """``pro merge all [--yes]``：把所有活跃项目的旧双 op 一次迁完。

        逐个调用 :meth:`_merge_pro_ops`：不带 ``--yes`` 只逐条预览；执行时
        单个项目失败（比如离线且有任务要挪）只记下来、继续下一个，末尾汇总。
        """
        toks = [str(t).strip() for t in (tail_tokens or ()) if str(t).strip()]
        yes = any(t.lower() in ("--yes", "-y", "yes", "确认", "执行") for t in toks)
        pros = [p for p in self.projects.active_pros()
                if self.projects.ops_for_pro(p)]
        if not pros:
            self.say("📁 没有可合并的项目（没有关联 op 的 active pro）。", "info")
            return []
        if not yes:
            self.say("🔀 预览：%d 个项目会被合并 / 补标签（加 `--yes` 执行）"
                     % len(pros), "cmd")
            for pro in pros:
                try:
                    self._merge_pro_ops(pro, [])
                except Exception as e:             # noqa: BLE001 - 预览也别中断
                    self.say("⚠ 项目 #%s 预览失败：%s"
                             % (self.projects.pro_number_of(pro["id"]) or pro["id"],
                                e), "warn")
            self.say("（只预览，没有写入。确认后：pro merge all --yes）", "info")
            return pros
        done, failed = [], []
        for pro in pros:
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            try:
                self._merge_pro_ops(pro, ["--yes"])
                done.append(pnum)
            except Exception as e:                 # noqa: BLE001 - 一个坏了继续
                reason = str(e) or e.__class__.__name__
                failed.append("%s：%s" % (pnum, reason))
                rl.log_event("project", "merge_all_failed", level="warning",
                             pro=pro["id"], error=reason)
        if done:
            self.say("✅ merge all 完成：%d 个项目已合并 / 补标签（#%s）。"
                     % (len(done), "、#".join(str(x) for x in done)), "ok")
        if failed:
            self.say("⚠ %d 个项目没动：%s" % (len(failed), "；".join(failed[:3])
                                               + ("…" if len(failed) > 3 else "")),
                     "warn")
        if not done and not failed:
            self.say("merge all：没有需要处理的项目。", "info")
        self.rebuild_rows()
        self.invalidate()
        return pros

    def _merge_pro_ops(self, pro, tail_tokens=None):
        """``pro <序号> merge [--yes]``：把旧的双 op 迁成「一个项目 + 侧标签」。

        顺序固定（先外部、后本地，失败可分叉最少）：

        1. 把 incubator op 里**未完成**的任务在 OmniFocus 里 move 到 SOP op 最前；
        2. 两边未完成任务补齐侧标签（incubator / SOP）；
        3. 本地清单合并（src 任务插到 dst 最前），src op 标 dropped、解除关联；
        4. 按 pro 当前阶段冻结非活跃侧。

        不带 ``--yes`` 只预览（列出会动几条）。OmniFocus 不可用时拒绝执行，
        避免本地和远端分叉。单 op 的 pro 也会跑：只补标签 + 冻结/解冻。
        """
        toks = [str(t).strip() for t in (tail_tokens or ()) if str(t).strip()]
        yes = any(t.lower() in ("--yes", "-y", "yes", "确认", "执行") for t in toks)
        pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
        title = self.pro_display_title(pro, fallback=pro.get("title"))
        ops = self.projects.ops_for_pro(pro)
        stage = pj.normalize_pro_stage(pro.get("stage")) or pj.STAGE_RUNNING

        def _open(project):
            return [x for x in (project.get("tasks") or ())
                    if not x.get("completed") and not x.get("dropped")
                    and not x.get("effective_completed")
                    and not x.get("effective_dropped")]

        if len(ops) > 2:
            raise ValueError("项目 #%s 关联了 %d 个 op，先手动清理到 1–2 个再 merge。"
                             % (pnum, len(ops)))
        if len(ops) == 2:
            # 迁移方向只能由「op 类型」决定：SOP 那条是目标、incubator 那条并进去。
            # 类型缺失时**不许**拿默认值硬猜（旧代码把 None 当 SOP，两个都没设
            # 类型时按列表顺序瞎选，方向可能反）。先让用户把类型补上。
            kinds = [pj.normalize_op_type(op.get("op_type")) for op in ops]
            if kinds[0] and kinds[1] and kinds[0] == kinds[1]:
                dst = ops[0]                     # 同侧的两个 op：只是把清单并成一份
                src = ops[1]
            elif pj.OP_TYPE_SOP in kinds and pj.OP_TYPE_INCUBATOR in kinds:
                dst = ops[kinds.index(pj.OP_TYPE_SOP)]
                src = ops[kinds.index(pj.OP_TYPE_INCUBATOR)]
            else:
                missing = [str(self.projects.number_of(op["id"]) or op["id"])
                           for op, kind in zip(ops, kinds) if not kind]
                raise ValueError(
                    "项目 #%s 的两个 op 分不出 SOP / incubator（没设类型的：#%s）。"
                    "先用 `op <序号> sop` / `op <序号> incubator` 标好类型再 merge，"
                    "否则迁移方向会猜反。" % (pnum, "、#".join(missing)))
        elif len(ops) == 1:
            src = None
            dst = ops[0]
        else:
            # 一个 op 都没有：只按当前阶段把 pro 的绑定理顺（没有任务可动）。
            self.projects.ensure_tagged_pros()
            self.say("项目 #%s「%s」没有关联任何 op；先把 OmniFocus 项目导进来。"
                     % (pnum, title), "warn")
            return pro

        src_open = _open(src) if src is not None else []
        dst_open = _open(dst)
        dst_side = (pj.normalize_op_type(dst.get("op_type")) or pj.OP_TYPE_SOP)
        src_side = (pj.normalize_op_type((src or {}).get("op_type"))
                    or pj.OP_TYPE_INCUBATOR)
        need_tag = [(t, dst_side) for t in dst_open
                    if pj.side_of_tags(t.get("tags") or ()) is None]
        need_tag += [(t, src_side) for t in src_open
                     if pj.side_of_tags(t.get("tags") or ()) is None]
        move_tasks = [t for t in src_open
                      if (dst.get("omnifocus") or {}).get("id")
                      and not str(t.get("pk") or "").startswith("soft-")]

        if not yes:
            self.say("🔀 预览：项目 #%s「%s」%s"
                     % (pnum, title,
                        ("把 op #%s「%s」并进 op #%s「%s」" % (
                            self.projects.number_of(src["id"]) or src["id"],
                            src.get("title"),
                            self.projects.number_of(dst["id"]) or dst["id"],
                            dst.get("title")))
                        if src is not None else "只有一个 op，只补标签"),
                     "cmd")
            self.say("   会移动 %d 条未完成任务、补 %d 条侧标签，然后按 %s 阶段冻结非活跃侧。"
                     "确认后加 `--yes` 执行。"
                     % (len(move_tasks), len(need_tag), stage), "info")
            return pro

        omni_ops = []
        dst_pk = (dst.get("omnifocus") or {}).get("id")
        if src is not None and move_tasks and (self.offline
                                               or not self.omnifocus_enabled()):
            raise ValueError("OmniFocus 不在线：merge 需要先把任务挪进目标项目，"
                             "离线执行会让本地和远端分叉。等 OmniFocus 可用了再来。")
        # 倒序移动：每次放进项目最前，最后顺序 = 原来的清单顺序。
        for item in reversed(move_tasks):
            omni_ops.append({"id": str(item.get("pk")), "op": "move",
                             "project": dst_pk, "position": "first"})
        for item, side in need_tag:
            pk = str(item.get("pk") or "")
            if pk.startswith("soft-"):
                continue
            # create=True：OmniFocus 里还没有这个标签就先建一个。侧别标签
            # （SOP / incubator / manual_defer）第一次用时多数都不存在；
            # 不建的话每条 set 都回 tag_problem，标签永远写不进外部，下一次
            # `project refresh` 再拿 OmniFocus 的旧标签把本地补的洗掉。
            omni_ops.append({"id": pk, "op": "set",
                             "tags": {"add": [side], "create": True}})
        omni_res = {}
        if omni_ops and (self.offline or not self.omnifocus_enabled()):
            # 单 op 补标签 / 冻结：本地照做，OmniFocus 侧跳过并说清楚（别撒谎）。
            omni_res = {"skipped": "offline"}
            self.say("⚠ OmniFocus 不在线：标签只改了本地快照；"
                     "等它可用了再跑一次 `pro %d merge --yes` 补写外部。"
                     % pnum, "warn")
        elif omni_ops:
            try:
                res = self.omnifocus.flush(omni_ops)
                results = res.get("results") or []
                bad = [r for r in results if not r.get("ok")]
                omni_res = {"ops": len(omni_ops), "failed": len(bad)}
                if bad:
                    rl.log_event("project", "merge_partial", level="warning",
                                 pro=pro["id"], failed=len(bad),
                                 first=(bad[0].get("error")
                                        or bad[0].get("message") or ""))
                    self.say("⚠ OmniFocus 有 %d 条没处理成功（%s）；本地照常合并，"
                             "下次 `project refresh` 会对账。"
                             % (len(bad),
                                bad[0].get("error") or bad[0].get("message") or "?"),
                             "warn")
            except Exception as e:                 # noqa: BLE001 - 外部失败不挡本地
                # 外部写挂了（OmniFocus 没开 / TCC 拒绝 / 超时）也绝不挡本地合并：
                # 先把本地账理顺、说清「外部没写」，等它可用了再跑一次补。
                omni_res = {"ops": len(omni_ops), "error": str(e)}
                rl.log_event("project", "merge_write_failed", level="warning",
                             pro=pro["id"], error=str(e)[:200])
                self.say("⚠ OmniFocus 写回失败（%s）：移动 / 标签只改了本地；"
                         "等它可用了再跑一次 `pro %d merge --yes` 补写外部。"
                         % (e, pnum), "warn")
        moved = []
        if src is not None:
            _dst, moved = self.projects.merge_ops(dst["id"], src["id"],
                                                  tag=src_side, reason="merged")
        # 合并之后给目标清单里仍没侧标签的任务补上（本地）。
        changes = {}
        for item in _open(self.projects.by_id(dst["id"])):
            if pj.side_of_tags(item.get("tags") or ()) is not None:
                continue
            tags = [str(t).strip() for t in (item.get("tags") or ())
                    if str(t).strip()]
            if src_side != dst_side and src is not None and any(
                    str(m.get("pk")) == str(item.get("pk")) for m in moved):
                tags.append(src_side)
            else:
                tags.append(dst_side)
            changes[str(item.get("pk"))] = {"tags": tags}
        if changes:
            self.projects.update_tasks(dst["id"], changes)
        sync = self._sync_side_visibility(pro)
        rl.log_event("project", "merge", level="info", pro=pro["id"],
                     dst=dst["id"], src=(src or {}).get("id"),
                     moved=len(moved), tagged=len(changes), omni=omni_res,
                     frozen=sync.get("frozen"), thawed=sync.get("thawed"))
        self.say("✅ 项目 #%s「%s」已并成一个 op #%s「%s」：移动 %d 条、补标签 %d 条、"
                 "冻结 %d / 解冻 %d。OmniFocus 里空出来的旧项目可以自己删掉。"
                 % (pnum, title, self.projects.number_of(dst["id"]) or dst["id"],
                    dst.get("title"), len(moved), len(changes),
                    sync.get("frozen") or 0, sync.get("thawed") or 0), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _side_plan(self, pro, now=None):
        """算出这个 pro 当前该冻结 / 解冻哪些任务（纯计算，不落盘）。

        规则（2026-11-22 用户改口后）：
        * 冻结 = 只给非活跃一侧的任务打 ``manual_defer`` 标签，**不碰 defer**
          （「算了，还是我这边手动排除吧，不要这个十年冻结了，仅打 tag 就行」）；
        * 解冻 = 摘掉标签；如果还留着旧版十年 defer 的哨兵（defer 在五年之后），
          顺手清掉（一次性迁移）；
        * 普通 defer 一个字都不动；没有侧标签的任务只记录、不碰。

        返回 ``(op, changes, untagged)``；``changes`` = ``{pk: {"tags"[, "defer"]}}``。
        """
        op = self.projects.current_op_for_pro(pro)
        if op is None:
            return None, {}, []
        now = now or self._now_dt()
        active = pj.side_for_stage(pro.get("stage"))
        default_side = pj.normalize_op_type(op.get("op_type"))
        changes = {}
        untagged = []
        for item in op.get("tasks") or ():
            if (item.get("completed") or item.get("dropped")
                    or item.get("effective_completed")
                    or item.get("effective_dropped")):
                continue
            pk = str(item.get("pk") or "")
            if not pk:
                continue
            side = pj.task_side(item, default=default_side)
            if side is None:
                untagged.append(item.get("title") or pk)
                continue
            tags = [str(t).strip() for t in (item.get("tags") or ())
                    if str(t).strip()]
            has_marker = pj.task_has_tag(item, pj.TAG_MANUAL_DEFER)
            legacy_defer = pj.is_frozen_defer(item.get("defer"), now=now)
            if side != active:
                if has_marker and not legacy_defer:
                    continue                     # 已经冻好了，别每次 refresh 都重写
                if not has_marker:
                    tags.append(pj.TAG_MANUAL_DEFER)
                fields = {"tags": tags}
                if legacy_defer:
                    # 旧版十年哨兵：冻结侧也顺手清掉（只留标签）。
                    fields["defer"] = None
                changes[pk] = fields
            else:
                if not (has_marker or legacy_defer):
                    continue
                kept = [t for t in tags
                        if t.lower() != pj.TAG_MANUAL_DEFER.lower()]
                fields = {"tags": kept}
                if legacy_defer:
                    # 旧版十年冻结留下的 defer 顺手清掉，普通 defer 不动。
                    fields["defer"] = None
                changes[pk] = fields
        return op, changes, untagged

    def _omni_write_side_changes(self, changes, timeout=None):
        """把冻结 / 解冻写回 OmniFocus（一次 batch，不逐条起进程）。

        ``changes`` 是 :meth:`_side_plan` 的输出；只有真的挂在 OmniFocus 上的
        任务才写（``soft-`` 开头的本地 dump 任务不在外部）。
        """
        if not changes:
            return {}
        if self.offline or not self.omnifocus_enabled():
            return {"skipped": "offline"}
        ops = []
        for pk, fields in changes.items():
            if str(pk).startswith("soft-"):
                continue
            op = {"id": str(pk), "op": "set"}
            if pj.TAG_MANUAL_DEFER in (fields.get("tags") or ()):
                # create=True：第一次冻结时 OmniFocus 里还没有 manual_defer，
                # 不建标签就只能每条失败（本地冻了、外部没冻）。
                op["tags"] = {"add": [pj.TAG_MANUAL_DEFER], "create": True}
            else:
                op["tags"] = {"remove": [pj.TAG_MANUAL_DEFER]}
            if "defer" in fields and not fields.get("defer"):
                op["clear_defer"] = True          # 只清旧版哨兵
            ops.append(op)
        if not ops:
            return {}
        try:
            res = self.omnifocus.flush(ops, timeout=timeout)
        except Exception as e:                     # noqa: BLE001 - 外部失败不挡本地
            rl.log_event("project", "side_write_failed", level="warning",
                         total=len(ops), error=str(e)[:200])
            return {"written": 0, "failed": len(ops), "error": str(e)}
        bad = [r for r in (res.get("results") or ()) if not r.get("ok")]
        if bad:
            rl.log_event("project", "side_write_partial", level="warning",
                         total=len(ops), failed=len(bad),
                         first=(bad[0].get("error") or bad[0].get("message") or ""))
        return {"written": len(ops) - len(bad), "failed": len(bad)}

    def _sync_side_visibility(self, pro_or_id, omni=True, announce=False):
        """按 pro 当前阶段冻结非活跃侧、解冻活跃侧（本地 + OmniFocus）。

        幂等：已经冻好 / 已经解冻的任务不会产生任何写入。返回
        ``{"frozen", "thawed", "untagged", "omni"}``。
        """
        pro = (pro_or_id if isinstance(pro_or_id, dict)
               else self.projects.pro_by_id(pro_or_id))
        if pro is None:
            raise ValueError("没有项目（pro）%s" % (pro_or_id,))
        op, changes, untagged = self._side_plan(pro)
        if op is None:
            return {"frozen": 0, "thawed": 0, "untagged": [], "omni": {}}
        # 冻结 = 新标签里有 manual_defer；解冻 = 这次要把它摘掉（含旧哨兵清理）。
        frozen = sum(1 for f in changes.values()
                     if pj.TAG_MANUAL_DEFER in (f.get("tags") or ()))
        thawed = len(changes) - frozen
        if changes:
            self.projects.update_tasks(op["id"], changes)
        omni_res = {}
        if omni and changes:
            try:
                omni_res = self._omni_write_side_changes(changes)
            except Exception as e:                 # noqa: BLE001 - 外部失败不挡本地
                omni_res = {"error": "%s: %s" % (type(e).__name__, e)}
                rl.log_event("project", "side_write_failed", level="error",
                             pro=pro.get("id"), op=op["id"], error=omni_res["error"])
        if changes:
            rl.log_event("project", "side_sync", level="debug",
                         pro=pro["id"], op=op["id"], stage=pro.get("stage"),
                         frozen=frozen, thawed=thawed, untagged=len(untagged),
                         omni=omni_res)
        if announce and (frozen or thawed):
            self.say("🧊 项目「%s」：冻结 %d 条、解冻 %d 条（%s 侧活跃）"
                     % (self.pro_display_title(pro, fallback=pro.get("title")),
                        frozen, thawed, pj.side_for_stage(pro.get("stage"))),
                     "info")
        return {"frozen": frozen, "thawed": thawed, "untagged": untagged,
                "omni": omni_res}

    def _push_soft_interrupts(self, pro, now=None):
        """切到 incubator：把攒下的软中断 dump 成任务，插到项目清单最前面。

        粒度（用户 2026-11-21 选）：**每条软中断一条 25 分钟任务**，标签
        ``incubator``；``local_only``（不写 OmniFocus，免得把临时整理项塞进
        真实项目）。插入后原来的队首任务自然退到后面，``count_invested`` 不动。
        返回新建条数。
        """
        recs = [rec for rec in self.projects.soft_interrupts(pro["id"])
                if rec.get("status") in (pj.SOFT_INTERRUPT_PENDING,
                                         pj.SOFT_INTERRUPT_QUEUED)]
        if not recs:
            return 0
        op = self.projects.current_op_for_pro(pro)
        if op is None:
            return 0
        stamp = int(time.time() * 1000)
        items = []
        for index, rec in enumerate(recs):
            text = str(rec.get("text") or "软中断").strip()
            title = text if len(text) <= 39 else text[:38] + "…"
            cat = rec.get("category") or "?"
            label = pj.SOFT_INTERRUPT_CATEGORY_LABELS.get(cat, "未分类")
            items.append({
                "pk": "soft-%d-%d-%d" % (pro["id"], stamp, index),
                "title": title,
                "duration": 25,
                "count": 1,
                "note": ("软中断检查生成的任务。[%s · %s]\n来源：项目「%s」"
                         % (cat, label, pro.get("title") or "?")),
                "tags": [pj.TAG_INCUBATOR, "软中断"],
                "local_only": True,
                "completed": False,
                "dropped": False,
            })
        self.projects.prepend_tasks(op["id"], items)
        for rec, item in zip(recs, items):
            self.projects.update_soft_interrupt(
                pro["id"], rec["id"], status=pj.SOFT_INTERRUPT_HANDLED,
                task_id=None, note="已 dump 成任务 %s" % item["pk"])
        rl.log_event("project", "soft_dump", level="info", pro=pro["id"],
                     op=op["id"], count=len(items))
        return len(items)

    def _switch_pro_stage(self, pro_or_id, stage, reason="cmd-stage"):
        """切换 pro 阶段：冻结/解冻两侧，切到 incubator 时顺手 dump 软中断。

        这是 push（切 incubator）/ pop（切回 running）的唯一出口。
        """
        pro = (pro_or_id if isinstance(pro_or_id, dict)
               else self.projects.pro_by_id(pro_or_id))
        if pro is None:
            raise ValueError("没有项目（pro）%s" % (pro_or_id,))
        updated = self.projects.set_pro_stage(pro["id"], stage)
        rl.log_event("project", "stage_switch", level="info", pro=updated["id"],
                     stage=updated.get("stage"), reason=reason)
        pushed = 0
        if pj.normalize_pro_stage(updated.get("stage")) == pj.STAGE_BOTTLENECK:
            try:
                pushed = self._push_soft_interrupts(updated)
            except Exception as e:                 # noqa: BLE001 - 别挡切换
                rl.log_event("project", "soft_dump_failed", level="error",
                             pro=updated["id"], error="%s: %s" % (type(e).__name__, e))
        sync = self._sync_side_visibility(updated, announce=False)
        self.selected_pro_id = updated["id"]
        pnum = self.projects.pro_number_of(updated["id"]) or updated["id"]
        bits = []
        if sync.get("frozen"):
            bits.append("冻结 %d 条" % sync["frozen"])
        if sync.get("thawed"):
            bits.append("解冻 %d 条" % sync["thawed"])
        if pushed:
            bits.append("dump %d 条软中断任务" % pushed)
        self.say("📂 项目 #%s「%s」阶段 → %s%s"
                 % (pnum, self.pro_display_title(updated, fallback=updated.get("title")),
                    updated.get("stage"),
                    ("（%s）" % "、".join(bits)) if bits else ""), "ok")
        if sync.get("untagged"):
            self.say("⚠ 项目里还有 %d 条任务没有 SOP / incubator 标签（不参与 plan）：%s"
                     % (len(sync["untagged"]),
                        "、".join(sync["untagged"][:5])), "warn")
        self.rebuild_rows()
        self.invalidate()
        return updated

    def _maybe_resume_from_bottleneck(self, task):
        """bottleneck 下最后一条 incubator 任务勾掉 → 自动切回 running 并解冻。

        用户 2026-11-21 选的 pop 语义：不用再手敲 ``pro N -a running``。
        返回切换后的 pro；没切换返回 None。
        """
        fields = (task or {}).get("fields") or {}
        op_id = fields.get(pj.PROJECT_ID_FIELD)
        if op_id is None:
            return None
        op = self.projects.by_id(op_id)
        pro = self.projects.pro_for_op(op) if op else None
        if pro is None:
            return None
        if pj.normalize_pro_stage(pro.get("stage")) != pj.STAGE_BOTTLENECK:
            return None
        invested = pj.invested_by_project(self.task_store).get(op["id"]) or {}
        left = pj.remaining_tasks(
            op, invested, side=pj.OP_TYPE_INCUBATOR,
            default_side=pj.normalize_op_type(op.get("op_type")),
            now=self._now_dt())
        if left:
            return None
        rl.log_event("project", "auto_resume", level="info", pro=pro["id"],
                     task=task.get("id"), reason="incubator-done")
        return self._switch_pro_stage(pro["id"], pj.STAGE_RUNNING,
                                      reason="incubator-done")

    def _finish_pro_advance(self, pro_id, op_id, target_stage=None):
        """完成 ``pro`` 的阶段切换并绑定新的 ``op``（兼容旧双 op 数据）。"""
        pro, op = self.projects.advance_pro(pro_id, op_id, target_stage=target_stage)
        self.pending_advance = None
        if self.pending_op_type:
            self.pending_op_type = [oid for oid in self.pending_op_type
                                    if oid != op["id"]]
        self.selected_pro_id = pro["id"]
        pushed = 0
        if pj.normalize_pro_stage(pro.get("stage")) == pj.STAGE_BOTTLENECK:
            try:
                pushed = self._push_soft_interrupts(pro)
            except Exception as e:                 # noqa: BLE001 - 别挡切换
                rl.log_event("project", "soft_dump_failed", level="error",
                             pro=pro["id"], error="%s: %s" % (type(e).__name__, e))
        try:
            sync = self._sync_side_visibility(pro)
        except Exception as e:                     # noqa: BLE001
            sync = {"frozen": 0, "thawed": 0, "untagged": []}
            rl.log_event("project", "side_sync_failed", level="error",
                         pro=pro["id"], error="%s: %s" % (type(e).__name__, e))
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        op_num = self.projects.number_of(op["id"]) or op["id"]
        self.say("🔀 项目 #%s「%s」阶段已切换为 %s（关联 op #%s「%s」；冻结 %d / 解冻 %d%s）。"
                 % (pro_num, self.pro_display_title(pro, fallback=pro.get("title")),
                    pro.get("stage"), op_num, op.get("title"),
                    sync.get("frozen") or 0, sync.get("thawed") or 0,
                    ("；dump %d 条软中断任务" % pushed) if pushed else ""), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _start_pro_advance(self, pro, tail_tokens=None):
        """执行 ``pro <序号> -a / --advance [op <序号> | task import ...]``。"""
        if pro.get("simple"):
            raise ValueError(
                "「%s」是简单项目：没有 SOP/incubator 活跃侧，不切阶段、也不打 "
                "manual_defer（容量 ∞，按项目级虚拟任务排）。"
                % (pro.get("title") or pro.get("id")))
        tail = [str(t).strip() for t in (tail_tokens or ()) if str(t).strip()]
        cur_stage = pj.normalize_pro_stage(pro.get("stage")) or pj.STAGE_RUNNING
        target_stage = (pj.STAGE_BOTTLENECK if cur_stage == pj.STAGE_RUNNING
                        else pj.STAGE_RUNNING)
        req_type = (pj.OP_TYPE_INCUBATOR if target_stage == pj.STAGE_BOTTLENECK
                    else pj.OP_TYPE_SOP)
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        if not tail:
            # 2026-11-21 新模型：切换阶段不再需要「再导入另一个 op」——SOP 与
            # incubator 是同一个清单里的标签，切阶段只做三件事：冻结旧侧、
            # 解冻新侧、切到 incubator 时把攒的软中断 dump 成任务。
            self.selected_pro_id = pro["id"]
            self.say("🔀 项目 #%s「%s」：%s → %s（冻结非活跃侧 / 解冻活跃侧%s）"
                     % (pro_num, self.pro_display_title(pro, fallback=pro.get("title")),
                        cur_stage, target_stage,
                        "；并把软中断 dump 成任务" if target_stage == pj.STAGE_BOTTLENECK
                        else ""), "cmd")
            return self._switch_pro_stage(pro["id"], target_stage, reason="advance")
        low0 = tail[0].lower()
        if low0 == "task" and len(tail) >= 2 and tail[1].lower() in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[2:])
        if low0 in ("op", "ofp") and len(tail) >= 2 and tail[1].lower() in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[2:])
        if low0 in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[1:])
        joined = " ".join(tail)
        if of.link_kind(joined) == "project":
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail)
        if low0 in ("op", "ofp", "omnifocus-project"):
            if len(tail) < 2:
                raise ValueError("`op` 后面要跟 op 序号（如 `pro %s -a op 2`）或 `task import`" % pro_num)
            op_tok = " ".join(tail[1:])
        else:
            op_tok = joined
        op = self.projects.find(op_tok)
        return self._finish_pro_advance(pro["id"], op["id"], target_stage=target_stage)

    def _cmd_pro_simple(self, pro, toks):
        """``pro <序号> simple [on]``：把项目标成「简单项目」（**单向，切不回去**）。

        用户 2026-11-25：「理论上应该可以不用切回普通项目，直接把这条路径 ban 掉，
        提示无法切回就行了」。所以这里不做「取消时清理已排场次」那一整套回滚 ——
        只改一个布尔值，不碰 OmniFocus、不碰标签、不碰任务。
        """
        toks = [str(t).strip().lower() for t in (toks or ()) if str(t).strip()]
        word = toks[0] if toks else ""
        num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        shown = self.pro_display_title(pro, fallback=pro.get("title"))
        if word in ("off", "no", "0", "false", "普通", "恢复", "取消", "关"):
            rl.log_event("project", "simple_refused", level="warning",
                         pro_id=pro["id"], reason="irreversible")
            raise ValueError(
                "简单项目不可逆：pro #%s「%s」无法切回普通项目（用户定：直接把这条"
                "路径 ban 掉）。要恢复只能手工改 data/projects.json 里的 "
                "\"simple\": false。" % (num, shown))
        if word not in ("", "on", "yes", "1", "true", "开", "开启"):
            raise ValueError("用法：pro <序号> simple [on]"
                             "（off 不可用：简单项目不可逆）")
        if pro.get("simple"):
            self.say("📂 pro #%s「%s」已经是简单项目了（容量 ∞、不按侧别挑任务）。"
                     % (num, shown), "info")
            return pro
        updated = self.projects.set_pro_simple(pro["id"], True)
        rl.log_event("project", "simple_toggle", level="info", pro_id=pro["id"],
                     simple=True, op_id=updated.get("op_id"))
        self.say("📂 pro #%s「%s」已标成 **简单项目**：不再分 SOP/incubator 侧别、"
                 "不按子任务排序、容量 ∞（单向，切不回去）。\n"
                 "   `plan` 里：pro %s <count> 按容量摊派；pro %s at 14:00 <count> "
                 "从某个起点连续占；pro %s period T1 占一整段。"
                 % (num, self.pro_display_title(updated, fallback=updated.get("title")),
                    num, num, num), "ok")
        self.invalidate()
        return updated

    def _cmd_pro_create(self, toks):
        """``pro create <名称> [file=<路径>] [stage=running|bottleneck] [op=<op序号>]``。"""
        title_parts = []
        file_path = None
        stage = pj.STAGE_RUNNING
        op_token = None
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--file", "-f", "file") and i + 1 < len(toks):
                file_path = toks[i + 1]
                i += 2
                continue
            if low.startswith("file=") or low.startswith("--file="):
                file_path = tok.split("=", 1)[1]
                i += 1
                continue
            if low in ("--stage", "-s", "stage") and i + 1 < len(toks):
                stage = toks[i + 1]
                i += 2
                continue
            if low.startswith("stage=") or low.startswith("--stage="):
                stage = tok.split("=", 1)[1]
                i += 1
                continue
            if low in ("--op", "op") and i + 1 < len(toks):
                op_token = toks[i + 1]
                i += 2
                continue
            if low.startswith("op=") or low.startswith("--op="):
                op_token = tok.split("=", 1)[1]
                i += 1
                continue
            if low.startswith("title=") or low.startswith("name="):
                title_parts.append(tok.split("=", 1)[1])
                i += 1
                continue
            title_parts.append(tok)
            i += 1
        title = " ".join(title_parts).strip()
        if not title:
            raise ValueError("用法：pro create <项目名称> [file=<文件路径>] [stage=running|bottleneck] [op=<op序号>]")
        op_id = None
        if op_token is not None:
            op = self.projects.find(op_token)
            op_id = op["id"]
        pro, created = self.projects.create_pro(
            title, stage=stage, file=file_path, op_id=op_id)
        self.selected_pro_id = pro["id"]
        num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        file_note = (" · 关联文件：%s" % pro["file"]) if pro.get("file") else ""
        self.say("%s Pro #%s「%s」阶段：%s%s"
                 % ("📂 新建项目" if created else "📂 更新项目",
                    num, self.pro_display_title(pro, fallback=pro.get("title")),
                    pro.get("stage"), file_note), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _pro_detail(self, pro):
        """展示真正项目 ``pro`` 的详情 + 它那一个 ``op`` 的任务清单。"""
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        op = self.projects.current_op_for_pro(pro)
        file_txt = pro.get("file") or "（未关联，用 `pro %s file <路径>` 绑定）" % pro_num
        self.say("📂 Pro #%s「%s」· 阶段：%s（活跃侧 %s）· 关联文件：%s"
                 % (pro_num, self.pro_display_title(pro, fallback=pro.get("title")),
                    pro.get("stage"), pj.side_for_stage(pro.get("stage")),
                    file_txt), "cmd")
        if op is None:
            self.say("   还没有关联 OmniFocus 项目（op）：`pro %s op <op序号>` 或 "
                     "`task import` 导入。" % pro_num, "warn")
            return pro
        self.say("   当前 op：#%s「%s」"
                 % (self.projects.number_of(op["id"]) or op["id"], op.get("title")),
                 "info")
        linked = self.projects.ops_for_pro(pro)
        extra = [x for x in linked if x["id"] != op["id"]]
        if extra:
            self.say("   ⚠ 还有 %d 个旧 op 没合并（%s）：跑 `pro %s merge` 合成一个。"
                     % (len(extra),
                        "、".join("#%s" % (self.projects.number_of(x["id"]) or x["id"])
                                 for x in extra), pro_num), "warn")
        self._project_detail(op)
        return pro

    def cmd_project(self, args=None):
        """``project`` / ``pro`` —— 管理真正项目 Project (pro) 与兼容项目库操作。

            pro / project                       列出真正项目 pro 与 OmniFocus 项目 op
            pro create <名称> [file=<路径>]     创建真正项目 pro
            pro <序号> -m complete|discard      本地状态（默认数据视图 disposable 会隐藏）
            pro <序号> -m active                恢复 active（非 active 时建议 id:<内部id>）
            pro <序号> -a <session> / -ac       直接把项目第一条任务挂到指定场 / 当前场
            pro <序号> -a / --advance           切换阶段（running ↔ bottleneck）：
                                                冻结旧侧 / 解冻新侧；切到 incubator 时
                                                把攒的软中断 dump 成 25 分钟任务
            pro <序号> merge [--yes]            迁移：把旧的双 op 并成一个项目、给任务
                                                补 SOP / incubator 标签（先预览）
            pro merge all [--yes]               对所有活跃项目一次做上面这件事
            pro <序号> file <路径|clear>        关联/清空项目的打开文件（open + enter 顺便打开）
            pro <序号> open                     打开该项目关联的文件
            pro <序号> stage <running|bottleneck>  直接设置阶段（同 -a 的效果）
            pro <序号> op <op序号>              关联已有 op
            pro <序号> interrupt [--soft]       软中断（默认）：保存一条建议 →
                                                选 A/B/C 类别；不下架当前任务
            pro <序号> interrupt --soft --check  逐条检查软中断：不处理 / 入队 / 丢弃；
                                                incubator 阶段当场 dump 成任务
            pro <序号> interrupt --hard         硬中断当前 session：
                                                下架全部任务 → 输入 title → 建整场任务挂回去
            project 1                           看 #1 当前阶段 op 的详情与任务清单
            project 1 refresh                   刷新该 pro 关联的 OmniFocus 清单
            project 1 remove                    撤掉 pro（op 仍在，用 op remove）
            project refresh / check             全量刷新 / 每日对账
        """
        self.projects.ensure_tagged_pros()
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        mark_status, mark_toks = self._extract_mark_action(toks)
        if mark_status is not None:
            if len(mark_toks) != 1:
                raise ValueError("用法：project <序号> -m complete|discard"
                                 "（也可 project <序号> complete|discard）")
            try:
                pro = self.projects.resolve_pro(mark_toks[0])
            except ValueError:
                pro = self.projects.resolve_pro_any(mark_toks[0])
            updated = self.projects.set_pro_status(
                pro["id"], mark_status, reason="cmd-project-mark-%s" % mark_status)
            self.selected_pro_id = updated["id"]
            return self._finish_mark_status("pro", updated, mark_status)
        if not toks:
            pro_rows = self.projects.pro_preview(self.sel_date, self.task_store)
            current_rows = self.current_project_rows()
            by_pro = {row["pro_id"]: row for row in current_rows}
            if not pro_rows:
                self.say("📁 项目库是空的：在 OmniFocus 里 ⌘C 一个**项目**，再敲 `task import`，"
                         "或 `pro create <名称>`。", "warn")
                return []
            for pr in pro_rows:
                file_txt = (" · 文件：%s" % pr["file"]) if pr.get("file") else ""
                row = by_pro.get(pr["id"])
                if row is None:
                    self.say("📂 Pro #%d %s [阶段:%s] · 还没有关联 OmniFocus 项目（op）："
                             "先 `task import` 导入，或 `pro %d op <op序号>` 关联。"
                             % (pr["number"], self.pro_display_title(pr),
                                pr["stage"], pr["number"]), "warn")
                    continue
                if row.get("simple"):
                    self.say("📂 Pro #%d %s [简单项目 · 容量 ∞]：进度 %s · "
                             "不按 SOP/incubator 侧别、不按子任务排序%s%s"
                             % (pr["number"], self.pro_display_title(pr),
                                row.get("progress_text") or "0/∞",
                                (" · 今天说好 %d count（已排 %d）"
                                 % (row.get("allocated"), row.get("scheduled")))
                                if row.get("allocated") else "", file_txt), "info")
                    continue
                op = row.get("active_op") or {}
                frozen = int(row.get("frozen_count") or 0)
                self.say("📂 Pro #%d %s [阶段:%s · 活跃侧 %s → op #%s「%s」]：剩余 %s count%s · "
                         "进度 %s · %d 条未完成 · 本周 %d 条%s%s"
                         % (pr["number"], self.pro_display_title(pr), pr["stage"],
                            row.get("active_side") or "?",
                            row.get("op_number") or "?", op.get("title") or "?",
                            row.get("remaining_text") or row.get("remaining") or 0,
                            ("（另 %d 条冻结）" % frozen) if frozen else "",
                            row.get("progress_text") or "—",
                            row.get("tasks_open") or 0, int(row.get("week_count") or 0),
                            (" · 今天说好 %d count（已排 %d）"
                             % (row.get("allocated"), row.get("scheduled")))
                            if row.get("allocated") else "", file_txt), "info")
                if row.get("legacy_extra_ops"):
                    self.say("   ⚠ 这个项目还有旧的双 op；`pro %d merge` 先预览再 --yes 合并。"
                             % pr["number"], "warn")
                if row.get("untagged"):
                    self.say("   ⚠ %d 条任务没有 SOP / incubator 标签（不进 plan）：%s"
                             % (len(row["untagged"]),
                                "、".join(row["untagged"][:3])), "warn")
            orphans = [row for row in self.project_rows()
                       if not row.get("pro_title")]
            if orphans:
                self.say("· 另有 %d 个未关联真正项目的 op；用 `op` 查看/关联。"
                         % len(orphans), "dim")
            return pro_rows
        # 手工把项目第一条任务挂到某一场：project <序号> -a <session>
        # / project <序号> -ac（当前这一场）。旧的 `pro <序号> -a op ...`
        # 仍保留为阶段推进；只有 -a 后面跟 session 编号时才走这里。
        if len(toks) >= 2:
            flag = toks[1].lower()
            if flag in ("-ac", "--assign-current"):
                return self._project_assign_first(toks[0], "current")
            if flag in ("-a", "--assign"):
                if len(toks) >= 3 and toks[2].lower() not in ("op", "task", "import"):
                    return self._project_assign_first(toks[0], toks[2])
                if len(toks) == 2:
                    # `pro 1 -a` 不带参数 = 切换阶段（push / pop）；带 session 才是挂场。
                    pro = self._ensure_pro_for_token(toks[0])
                    return self._start_pro_advance(pro, [])
        action = toks[0].lower()
        if action in ("create", "new", "add", "新建", "创建"):
            return self._cmd_pro_create(toks[1:])
        if action in ("check", "对账", "daily", "每日检查"):
            self._project_sync_day = None
            started = self._start_project_sync_now()
            return started
        if action in ("refresh", "sync", "reload", "更新"):
            made = [self._project_refresh(project) for project in self.projects.active()]
            return [p for p in made if p]
        if self._is_advance_flag(action):
            if len(toks) < 2:
                raise ValueError("用法：pro <序号> -a / --advance [op <序号> | task import]")
            pro = self._ensure_pro_for_token(toks[1])
            return self._start_pro_advance(pro, toks[2:])
        if action in ("merge", "unify", "合并", "统一"):
            tail = toks[1:]
            if tail and str(tail[0]).lower() in ("all", "全部", "*"):
                return self._merge_all_pros(tail[1:])
            raise ValueError("用法：pro <序号> merge [--yes] / pro merge all [--yes]")

        rest = toks[1:]
        rest_low = [t.lower() for t in rest]

        # 简单项目：单向开关（用户 2026-11-25：「把切回普通项目这条路径 ban 掉」）
        if rest and rest_low[0] in ("simple", "简单", "--simple", "-s"):
            pro = self._ensure_pro_for_token(toks[0])
            return self._cmd_pro_simple(pro, rest[1:])

        # 检查是否带 -a / --advance / —advance
        if rest and self._is_advance_flag(rest[0]):
            pro = self._ensure_pro_for_token(toks[0])
            return self._start_pro_advance(pro, rest[1:])

        # 检查是否是 file / --file / -f / file=...
        if rest and (rest_low[0] in ("file", "--file", "-f", "文件")
                     or rest_low[0].startswith("file=")
                     or rest_low[0].startswith("--file=")):
            pro = self._ensure_pro_for_token(toks[0])
            if "=" in rest[0]:
                fpath = rest[0].split("=", 1)[1]
                if len(rest) > 1:
                    fpath = (fpath + " " + " ".join(rest[1:])).strip()
            else:
                fpath = " ".join(rest[1:]).strip()
            updated = self.projects.set_pro_file(pro["id"], fpath)
            self.selected_pro_id = updated["id"]
            pnum = self.projects.pro_number_of(updated["id"]) or updated["id"]
            if updated.get("file"):
                self.say("📂 项目 #%s「%s」已关联文件：%s（敲 `open` + 回车时顺便打开）。"
                         % (pnum, updated.get("title"), updated["file"]), "ok")
            else:
                self.say("📂 项目 #%s「%s」已清空关联文件。"
                         % (pnum, updated.get("title")), "ok")
            self.invalidate()
            return updated

        # 检查是否是 open（打开项目关联文件）
        if rest and rest_low[0] in ("open", "打开"):
            pro = self._ensure_pro_for_token(toks[0])
            self.selected_pro_id = pro["id"]
            if not pro.get("file"):
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                raise ValueError("项目 #%s「%s」还没有关联文件；先用 `pro %s file <路径>` 关联。"
                                 % (pnum, pro.get("title"), pnum))
            self._open_pro_file(pro)
            return pro

        # 硬中断：下掉当前 session 的所有任务，等用户输入 title 后新建一条
        # 与整场等长的任务并挂回去。默认 --hard，显式写 --hard 也一样。
        if rest and rest_low[0] in ("interrupt", "中断", "打断", "硬中断"):
            pro = self._ensure_pro_for_token(toks[0])
            return self._start_project_interrupt(pro, rest[1:])

        # 检查是否是 stage <running|bottleneck>
        if rest and (rest_low[0] in ("stage", "--stage", "阶段")
                     or rest_low[0].startswith("stage=")
                     or pj.normalize_pro_stage(rest[0]) is not None):
            pro = self._ensure_pro_for_token(toks[0])
            if rest_low[0].startswith("stage="):
                st_val = rest[0].split("=", 1)[1]
            elif rest_low[0] in ("stage", "--stage", "阶段"):
                if len(rest) < 2:
                    raise ValueError("用法：pro <序号> stage <running|bottleneck>")
                st_val = rest[1]
            else:
                st_val = rest[0]
            updated = self._switch_pro_stage(pro["id"], st_val, reason="cmd-stage")
            return updated

        # 迁移：pro <序号> merge [--yes] / pro merge all —— 把旧的 incubator op 并进 SOP op，
        # 给任务补侧标签，再按当前阶段冻结/解冻。只做一次，之后就只有一个 op。
        if rest and rest_low[0] in ("merge", "unify", "合并", "统一"):
            tail = rest[1:]
            if tail and str(tail[0]).lower() in ("all", "全部", "*"):
                return self._merge_all_pros(tail[1:])
            pro = self._ensure_pro_for_token(toks[0])
            return self._merge_pro_ops(pro, tail)

        # 检查是否是关联已有 op：pro <序号> op <op序号>
        if rest and rest_low[0] in ("op", "link", "关联"):
            if len(rest) < 2:
                raise ValueError("用法：pro <序号> op <op序号>")
            pro = self._ensure_pro_for_token(toks[0])
            op = self.projects.find(" ".join(rest[1:]))
            op, pro = self.projects.link_op_to_pro(op["id"], pro["id"])
            self.selected_pro_id = pro["id"]
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            onum = self.projects.number_of(op["id"]) or op["id"]
            self.say("🔗 项目 #%s「%s」已关联 op #%s「%s」。"
                     % (pnum, self.pro_display_title(pro, fallback=pro.get("title")),
                        onum, op.get("title")), "ok")
            self.invalidate()
            return pro

        # refresh / remove / 详情：project 命令优先按真正项目 pro 解析
        if rest and rest_low[0] in ("refresh", "sync", "reload", "更新"):
            try:
                pro = self.projects.resolve_pro(toks[0])
                ops = self.projects.ops_for_pro(pro)
                made = [self._project_refresh(op) for op in ops]
                return [p for p in made if p]
            except ValueError:
                project = self.projects.resolve(toks[0])
                return self._project_refresh(project)
        if rest and rest_low[0] in ("remove", "rm", "delete", "撤掉", "删除"):
            try:
                pro = self.projects.resolve_pro(toks[0])
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                self.projects.remove_pro(pro["id"])
                self.invalidate()
                self.say("🗑 项目（pro）#%s「%s」已删除。"
                         % (pnum, self.pro_display_title(pro, fallback=pro.get("title"))), "ok")
                return pro
            except ValueError:
                project = self.projects.resolve(toks[0])
                num = self.projects.number_of(project["id"]) or project["id"]
                removed = self.task_store.remove_project_backlog(project["id"])
                self.projects.remove(project["id"])
                self.rebuild_rows()
                self.invalidate()
                self.say("🗑 项目 #%d「%s」从项目库撤掉了（OmniFocus 那边没动；已经排进 "
                         "session 的任务照旧%s）。"
                         % (num, project.get("title"),
                            "；清掉 %d 条未唤起的镜像任务" % removed if removed else ""), "ok")
                return project
        # 详情：project 序号首先指真正项目 pro；该 pro 当前阶段有 op 时把
        # op 的清单一起铺开。旧用法（只有 op）仍退回 op 详情。
        try:
            pro = self.projects.resolve_pro(toks[0])
            self.selected_pro_id = pro["id"]
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            active_op = self.projects.current_op_for_pro(pro)
            self.say("📂 Pro #%s「%s」· 阶段：%s · 当前 %s op：%s%s"
                     % (pnum, self.pro_display_title(pro, fallback=pro.get("title")),
                        pro.get("stage"),
                        pj.normalize_op_type((active_op or {}).get("op_type")) or "—",
                        (active_op or {}).get("title") or "（缺失）",
                        (" · 文件：%s" % pro["file"]) if pro.get("file") else ""),
                     "info")
            if active_op is not None:
                return self._project_detail(active_op)
            return self._pro_detail(pro)
        except ValueError:
            project = self.projects.resolve(toks[0])
            pro = self.projects.pro_for_op(project)
            if pro is not None:
                self.selected_pro_id = pro["id"]
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                self.say("📂 所属项目 Pro #%s「%s」· 阶段：%s%s"
                         % (pnum, self.pro_display_title(pro, fallback=pro.get("title")),
                            pro.get("stage"),
                            (" · 文件：%s" % pro["file"]) if pro.get("file") else ""),
                         "info")
            return self._project_detail(project)

    # -- 项目任务直接挂场 ------------------------------------------------
    def _project_op_for_token(self, token):
        """project 序号 → 当前阶段 op；兼容直接给 op 序号/名字。"""
        pro = None
        try:
            pro = self.projects.resolve_pro(token)
        except ValueError:
            pro = None
        if pro is not None:
            op = self.projects.current_op_for_pro(pro)
            if op is None:
                raise ValueError("项目「%s」当前阶段没有可用的 OmniFocus op"
                                 % self.pro_display_title(pro, fallback=pro.get("title")))
            return op, pro
        op = self.projects.resolve(token)
        return op, self.projects.pro_for_op(op)

    def _first_project_leaf(self, op, parent=None, side=None, default_side=None):
        """项目清单里按 OmniFocus 顺序拿到第一条可排的叶子任务（只取给的那一侧）。"""
        invested = pj.invested_by_project(self.task_store).get(op["id"]) or {}
        exclude = set()
        now = datetime.datetime.now()
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if (pk and str(pid) == str(op["id"]) and ts.TaskStore.is_open(task)
                    and (task.get("formatter") or ts.task_is_deferred(task, now=now))):
                exclude.add(str(pk))
        items = pj.remaining_tasks(op, invested, now=now, exclude_pks=exclude,
                                   side=side, default_side=default_side)
        if not items:
            return None, None
        first = items[0]
        children = [dict(child) for child in first.get("children") or ()
                    if int(child.get("duration") or 0) > 0]
        if children:
            parent = first
            return children[0], parent
        return first, None

    def _plan_task_reusable(self, existing, day=None, exclude=()):
        """plan 唤起已有镜像任务时，这条任务能不能直接复用（而不是再建一条）。

        项目导入时就把叶子任务镜像进本地任务库了，plan 唤起时复用它、只更新这次
        分到的 count/duration。**已经绑着 session 的不复用**：那说明它已经在
        某个场次上了（可能是手动的、也可能是明天的一次性场次），这一轮不该把它
        抢走。``exclude`` 是这一轮已经用掉的任务（一条 4 count 的任务落成
        75+25 两段时，第二段必须新建一条）。
        """
        return self._plan_reuse_reason(existing, day, exclude=exclude)[0]

    def _plan_reuse_reason(self, existing, day=None, exclude=()):
        """``_plan_task_reusable`` 的判定 + 理由（``(bool, reason)``，给日志用）。

        ``reason``：``reuse`` / ``no_mirror`` / ``closed``（已收工）/
        ``used_this_round``（本轮已用）/ ``already_bound``（挂在别的场次上）。
        """
        if existing is None:
            return False, "no_mirror"
        if not ts.TaskStore.is_open(existing):
            return False, "closed"
        if existing.get("id") in set(exclude or ()):
            return False, "used_this_round"
        if existing.get("session"):
            return False, "already_bound"
        return True, "reuse"

    def _open_mirrors_except(self, project_id, pk, exclude=()):
        """同一个 ``(project_id, pk)`` 下**还没挂 session 的**开放镜像（按 id）。

        ``_project_local_task`` 只返回清单里第一条同 pk 的记录，而那条很可能是
        以前已经完成/挂过场次的老副本 —— 于是每轮 plan 都判定「不能复用」再造
        一条，任务库里堆出一串同 pk 的幽灵（2026-11-18 用户看到的
        「一堆 Lit 都是 4/3」就是这么来的）。
        """
        want = str(pk or "").strip()
        if not want:
            return []
        skip = set(exclude or ())
        out = []
        for task in self.task_store.list():
            pid, _title, got = ts.TaskStore.project_task_fields(task)
            if got != want or str(pid) != str(project_id):
                continue
            if task.get("id") in skip:
                continue
            if not ts.TaskStore.is_open(task) or task.get("session"):
                continue
            out.append(task)
        return out

    def _plan_mirror_pick(self, project_id, pk, exclude=()):
        """plan 唤起项目任务时该复用哪条镜像：优先**开放且没挂 session** 的那条。

        老的 ``_project_local_task`` 只返回清单里第一条同 pk 记录，而那条常常是
        几天前就完成/挂过场次的老副本 —— 判定「不能复用」→ 再造一条，于是任务
        库里同 pk 幽灵越堆越多（2026-11-18：数学 8 条、Lit scene 2 六条）。
        这里先看默认那条，不行再在同 pk 的开放镜像里挑 **id 最新**的一条。
        """
        found = self._project_local_task(project_id, pk)
        spares = self._open_mirrors_except(project_id, pk, exclude=exclude)
        if spares:
            # 同 pk 有多条开放镜像时固定挑**最新**那条：复用哪一条必须可预测，
            # 剩下的由 _prune_duplicate_mirrors 收掉。
            return sorted(spares, key=lambda t: int(t.get("id") or 0))[-1]
        return found

    def _prune_duplicate_mirrors(self, day=None):
        """同一个 ``(project_id, pk)`` 只留一条开放、没挂 session 的**派生**镜像。

        用户 2026-11-18 的要求：反复 plan 之后堆出来的那些任务「不要展示在这里；
        如果是重复任务产生的，应该删除」。删除只针对**清单派生的幽灵**（它们是
        plan 自己生成的，删了没有任何外部账要还），条件全部满足才动：

        * ``source.kind == derived`` / 带 ``derived_text``（清单派生）；
        * 开放、没挂 session；
        * 今天/过去没有任何处理痕迹（``history`` 空）；
        * 同一个 pk 还有别的开放镜像（留 id 最大的那条）。

        普通项目镜像（``source.kind == project``）是 OmniFocus 的镜子，不在这里
        删；它们没挂 session 时由 :meth:`TaskStore.is_project_backlog` 挡在契约
        面板之外。
        """
        groups = {}
        for task in self.task_store.list():
            if not ts.TaskStore.is_open(task) or task.get("session"):
                continue
            if task.get("history") or task.get("formatter"):
                continue
            fields = task.get("fields") or {}
            pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
            if not pk:
                continue
            try:
                import derived as _dv
                if not _dv.is_derived(task):
                    continue
            except Exception:                    # noqa: BLE001 - 判不出来就别删
                continue
            groups.setdefault((str(fields.get(pj.PROJECT_ID_FIELD)), pk), []).append(task)
        removed = []
        for _key, tasks in groups.items():
            if len(tasks) < 2:
                continue
            tasks.sort(key=lambda t: int(t.get("id") or 0))
            for task in tasks[:-1]:              # 留最新那条
                try:
                    self.task_store.drop(task["id"], reason="duplicate-mirror")
                    removed.append(task["id"])
                except (ValueError, TypeError):
                    continue
        if removed:
            rl.log_event("plan", "mirror_pruned", level="info",
                         day=day.isoformat() if hasattr(day, "isoformat") else str(day),
                         removed=removed)
            try:
                self.invalidate()
            except Exception:                    # noqa: BLE001 - 缓存刷不掉不挡主流程
                pass
        return removed

    def _wake_project_leaf(self, op, item, parent=None, day=None,
                           exclude=()):
        """把项目叶子任务变成可挂 session 的本地任务（复用已有镜像）。"""
        item = dict(item or {})
        pk = str(item.get("pk") or "").strip()
        if not pk:
            raise ValueError("项目任务没有 pk，不能挂 session")
        derived = item.get("derived") or None
        total_count = ts.parse_count(item.get("total")
                                     or item.get("count")) or ts.minutes_to_count(
            item.get("duration"))
        fields = {"title": item.get("title") or pk,
                  pj.PROJECT_FIELD: op.get("title") or "",
                  pj.PROJECT_ID_FIELD: op["id"],
                  pj.PROJECT_TASK_FIELD: pk}
        if derived:
            fields.update(pp.derived_fields(derived, pk))
        if total_count and total_count > 0:
            fields["count_total"] = int(total_count)
            fields["count"] = int(total_count)
        if item.get("duration"):
            fields["duration"] = int(item.get("duration"))
        if item.get("note"):
            fields["note"] = item["note"]
        parent_pk = str((parent or {}).get("pk") or item.get("parent_id") or "").strip()
        parent_title = (parent or {}).get("title") or item.get("parent") or ""
        if parent_pk:
            fields["note"] = ((fields.get("note") or "") + "\n父任务：%s"
                              % (parent_title or parent_pk)).strip()
        source = {"kind": "project", "id": pk,
                  "url": of.task_url(pk), "title": item.get("title")}
        if derived:
            source = pp.derived_source(derived)
        if item.get("local_only"):
            source["local_only"] = True
            source["url"] = ""
        if parent_pk:
            source.update({"parent_id": parent_pk, "parent_title": parent_title})
        existing = self._plan_mirror_pick(op["id"], pk, exclude=exclude)
        if self._plan_task_reusable(existing, day, exclude=exclude):
            return self.task_store.apply_project_plan(
                existing["id"], fields, source=source)
        return self.task_store.create(
            fields,
            omnifocus=None if (item.get("local_only") or derived) else {
                "id": pk, "url": of.task_url(pk),
                "title": item.get("title") or pk},
            source=source)

    def _project_assign_first(self, project_token, session_token):
        """``project N -a <session>`` / ``-ac``：第一条项目任务直接挂场。"""
        op, pro = self._project_op_for_token(project_token)
        item, parent = self._first_project_leaf(
            op, side=(pj.side_for_stage(pro.get("stage")) if pro else None),
            default_side=pj.normalize_op_type(op.get("op_type")))
        if item is None:
            raise ValueError("项目「%s」没有剩余的可排任务" % op.get("title"))
        if session_token == "current":
            idx, row = self.current_session()
        else:
            idx, row = self.pick(session_token)
        date_iso = str(row.get("date") or self.sel_date.isoformat())
        task = self._wake_project_leaf(op, item, parent=parent,
                                       day=self.sel_date)
        note = self._assign_task(task, str(idx), refresh=False,
                                 allow_unbooked=True)
        task = self.task_store.get(task["id"])
        sess = task.get("session") or {}
        pnum = (self.projects.pro_number_of(pro["id"]) if pro else
                self.projects.number_of(op["id"]))
        if str(sess.get("start") or "") == str(row.get("startLabel") or ""):
            try:
                self.projects.record_items(op["id"], self.sel_date, [{
                    "pk": item.get("pk"), "task_id": task["id"],
                    "count": int(item.get("count") or 0),
                    "start": row.get("startLabel") or "",
                    "duration": int(row.get("durationMin") or 0),
                }], append=True)
            except Exception:                   # noqa: BLE001 - 记账失败不挡挂载
                pass
            self.selected_task_id = task["id"]
            self.rebuild_rows()
            self.invalidate()
            self.say("📌 Pro #%s「%s」第一条任务「%s」已挂到 session #%d（%s %s）%s"
                     % (pnum or "?", op.get("title") or "?",
                        self.task_store.focusmate_title(task, 40), idx,
                        date_iso, row.get("startLabel") or "?",
                        ("；%s" % note) if note else ""), "ok")
        else:
            self.say("⚠ Pro #%s「%s」第一条任务没挂到 session #%d：%s"
                     % (pnum or "?", op.get("title") or "?", idx,
                        note or "目标场拒绝了这次挂载"), "warn")
        return task

    # -- 快速征召（conscription / cst） ---------------------------------
    @staticmethod
    def _cst_floor_five(minutes):
        """剩余分钟按 5 分钟一档向下取整：23 → 20，25 → 25，8 → 5。"""
        try:
            value = int(minutes)
        except (TypeError, ValueError):
            return 0
        return max(0, (value // 5) * 5)

    @staticmethod
    def _cst_is_errand_op(op):
        text = " ".join([str((op or {}).get("title") or ""),
                         str((op or {}).get("project_tag") or "")]).casefold()
        return any(word in text for word in
                   ("errand", "跑腿", "杂事", "琐事", "购物"))

    def _cst_excluded_pks(self, op):
        """已经挂进 session / 被 formatter 占住的项目任务不能再次征召。"""
        exclude = set()
        now = self._now_dt()
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if not pk or str(pid) != str(op.get("id")):
                continue
            if not ts.TaskStore.is_open(task):
                continue
            if (task.get("session") or task.get("formatter")
                    or ts.task_is_deferred(task, now=now)):
                exclude.add(str(pk))
        return exclude

    def _cst_project_candidates(self, op, max_minutes, now=None):
        """按 OmniFocus 清单顺序找第一条时长合适的叶子任务。"""
        invested = pj.invested_by_project(self.task_store).get(op["id"]) or {}
        try:
            items = pj.remaining_tasks(
                op, invested, now=now, exclude_pks=self._cst_excluded_pks(op),
                side=op.get("side"),
                default_side=(op.get("default_side")
                              or pj.normalize_op_type(op.get("op_type"))))
        except Exception:                          # noqa: BLE001 - 一条项目坏了不挡其他
            return None
        for item in items:
            children = [dict(child) for child in item.get("children") or ()
                        if int(child.get("duration") or 0) > 0]
            if children:
                for child in children:
                    minutes = int(child.get("duration") or 0)
                    if 0 < minutes <= max_minutes:
                        return op, child, item
                continue
            minutes = int(item.get("minutes") or item.get("duration") or 0)
            if not minutes:
                minutes = (int(item.get("count") or 0)
                           * ts.MINUTES_PER_COUNT)
            if 0 < minutes <= max_minutes:
                return op, item, None
        return None

    def _cst_session_remaining_minutes(self, row):
        """目标 session 从现在起还剩多少分钟；未来场按整场算。"""
        now = self._now_dt()
        start_ms = row.get("startMs")
        end_ms = row.get("endMs")
        if start_ms is not None and end_ms is not None:
            now_ms = int(now.timestamp() * 1000)
            if int(start_ms) <= now_ms < int(end_ms):
                return max(0, int((int(end_ms) - now_ms) / 60000))
        duration = int(row.get("durationMin") or row.get("duration") or 0)
        if duration:
            return duration
        if end_ms is not None:
            return max(0, int((int(end_ms) - int(now.timestamp() * 1000)) / 60000))
        return 0

    def cmd_conscription(self, args=None):
        """``conscription`` / ``cst``：按剩余时间和项目顺序征召一条短任务。

        默认挂到当前场（进行中优先，否则最近一场已订的 upcoming）。如果当前
        进行中的场里只有一条任务，那条任务所属的项目优先；任务按每 5 分钟
        一档匹配（剩 23 分钟只找 ≤20 分钟）。当前项目没有合适任务时按
        OmniFocus 项目顺序找其他项目；剩余不足 10 分钟时优先找 errand 项目。
        """
        toks = [str(x).strip() for x in (args or []) if str(x).strip()]
        if len(toks) > 1:
            raise ValueError("用法：conscription / cst [session序号|current]")
        if toks:
            idx, row = self._resolve_assign_target(toks[0])
        else:
            live = self.live_session()
            if live:
                idx, row = live
            else:
                idx, row = self.current_session()
        if row is None:
            raise ValueError("当前没有可挂任务的 session")
        date_iso = str(row.get("date") or self.sel_date.isoformat())
        remaining = self._cst_session_remaining_minutes(row)
        max_minutes = self._cst_floor_five(remaining)
        if max_minutes <= 0:
            raise ValueError("当前 session 剩余不足 5 分钟，cst 不塞新任务"
                             "（剩余 %d 分钟）" % remaining)

        # 进行中的场只有一个任务时，用它的项目当首要匹配上下文。
        preferred_op = None
        live = self.live_session()
        same_live = bool(live and (
            live[1] is row
            or (live[1].get("id") and live[1].get("id") == row.get("id"))
            or ((live[1].get("date"), live[1].get("startLabel"))
                == (row.get("date"), row.get("startLabel")))))
        session_tasks = self.tasks_for_session(
            date_iso, row.get("startLabel"), row.get("durationMin"))
        if same_live and len(session_tasks) == 1:
            fields = session_tasks[0].get("fields") or {}
            pid = fields.get(pj.PROJECT_ID_FIELD)
            if pid is not None:
                preferred_op = self.projects.by_id(pid)
        all_ops = [op for op in self.projects.plan_ops()
                   if op.get("status") == pj.STATUS_ACTIVE]
        ordered = []
        if preferred_op is not None:
            ordered.append(preferred_op)
        # 剩余不足 10 分钟时，errand 项目排在普通其他项目前面。
        rest = [op for op in all_ops
                if preferred_op is None or op.get("id") != preferred_op.get("id")]
        if remaining < 10:
            errands = [op for op in rest if self._cst_is_errand_op(op)]
            rest = errands + [op for op in rest if op not in errands]
        ordered.extend(rest)

        now = self._now_dt()
        picked = None
        for op in ordered:
            picked = self._cst_project_candidates(op, max_minutes, now=now)
            if picked is not None:
                break
        if picked is None:
            # 最后的兜底：项目库没有合适叶子时，也从本地赋闲任务里找一条。
            local_candidates = []
            for candidate in self.task_store.list():
                if not ts.TaskStore.is_open(candidate):
                    continue
                if candidate.get("session") or candidate.get("formatter"):
                    continue
                if ts.TaskStore.is_repeat_template(candidate):
                    continue
                if ts.TaskStore.is_project_backlog(candidate):
                    continue
                fields = candidate.get("fields") or {}
                minutes = ts.parse_minutes(fields.get("duration"))
                if minutes is None:
                    minutes = (ts.parse_count(fields.get("count")) or 0) * 25
                if 0 < int(minutes or 0) <= max_minutes:
                    local_candidates.append((candidate, int(minutes)))
            if local_candidates:
                # 偏好项目 id 的本地任务先来，其余按任务 id（稳定、可预测）。
                def local_key(pair):
                    pid = (pair[0].get("fields") or {}).get(pj.PROJECT_ID_FIELD)
                    pref = (preferred_op or {}).get("id")
                    return (0 if pref is not None and str(pid) == str(pref) else 1,
                            int(pair[0].get("id") or 0))
                local_candidates.sort(key=local_key)
                task, duration = local_candidates[0]
                note = self._assign_task(task, str(idx), refresh=False,
                                         allow_unbooked=True)
                task = self.task_store.get(task["id"])
                sess = task.get("session") or {}
                if str(sess.get("start") or "") != str(row.get("startLabel") or ""):
                    self.say("⚠ 征召本地任务没挂到 session #%d：%s"
                             % (idx, note or "目标场拒绝了这次挂载"), "warn")
                    return task
                self.selected_task_id = task["id"]
                self.rebuild_rows()
                self.invalidate()
                self.say("🪖 cst：session #%d（剩 %d 分钟 → ≤%d 分钟）"
                         "← 本地赋闲任务「%s」（%s 分钟）%s"
                         % (idx, remaining, max_minutes,
                            self.task_store.focusmate_title(task, 36), duration,
                            ("；%s" % note) if note else ""), "ok")
                return task
            raise ValueError(
                "当前 session 还剩 %d 分钟（按 5 分钟档只找 ≤%d 分钟）；"
                "项目清单和本地赋闲任务里都没有合适的未排任务。"
                % (remaining, max_minutes))
        op, item, parent = picked
        task = self._wake_project_leaf(op, item, parent=parent,
                                       day=self.sel_date)
        note = self._assign_task(task, str(idx), refresh=False,
                                 allow_unbooked=True)
        task = self.task_store.get(task["id"])
        sess = task.get("session") or {}
        if str(sess.get("start") or "") != str(row.get("startLabel") or ""):
            self.say("⚠ 征召任务没挂到 session #%d：%s"
                     % (idx, note or "目标场拒绝了这次挂载"), "warn")
            return task
        duration = int((item.get("duration") if item.get("children") else
                        item.get("minutes") or item.get("duration") or 0) or 0)
        if not duration:
            duration = int(task.get("fields", {}).get("duration") or 0)
        try:
            self.projects.record_items(op["id"], self.sel_date, [{
                "pk": item.get("pk"), "task_id": task["id"],
                "count": ts.minutes_to_count(duration) or 1,
                "start": row.get("startLabel") or "",
                "duration": duration,
            }], append=True)
        except Exception:                          # noqa: BLE001 - 记账失败不挡挂载
            pass
        self.selected_task_id = task["id"]
        self.rebuild_rows()
        self.invalidate()
        pro = self.projects.pro_for_op(op)
        pro_title = (self.pro_display_title(pro, fallback=op.get("title"))
                     if pro else (op.get("title") or "?"))
        self.say("🪖 cst：session #%d（%s %s，剩 %d 分钟 → ≤%d 分钟）"
                 "← 「%s」的「%s」（%s 分钟）%s"
                 % (idx, date_iso, row.get("startLabel") or "?", remaining,
                    max_minutes, pro_title,
                    self.task_store.focusmate_title(task, 36), duration,
                    ("；%s" % note) if note else ""), "ok")
        return task

    # -- 中断：软中断（默认）/ 硬中断 -----------------------------------
    def _start_project_interrupt(self, pro, tail_tokens=None):
        """``pro <序号> interrupt [--soft|--hard]``。

        默认（以及 ``--soft/-s``）是**软中断**：不碰当前 session，先保存一条
        建议/问题，再选 A/B/C 类别；之后用 ``interrupt --soft --check`` 逐条
        检查。``--hard`` 才保留旧的「清空当前场 → 输入 title → 建整场任务」
        流程。
        """
        tail = [str(t).strip().lower() for t in (tail_tokens or ())
                if str(t).strip()]
        mode = "soft"
        check = False
        list_only = False
        for token in tail:
            if token in ("--hard", "hard", "硬", "硬中断"):
                mode = "hard"
            elif token in ("--soft", "-s", "soft", "软", "软中断"):
                mode = "soft"
            elif token in ("--check", "-c", "check", "检查"):
                check = True
            elif token in ("--list", "list", "列表"):
                list_only = True
            else:
                raise ValueError(
                    "用法：pro <序号> interrupt [--soft|-s|--hard] "
                    "[--check|--list]（默认 soft）")
        if mode == "hard":
            if check or list_only:
                raise ValueError("--check/--list 只用于软中断；硬中断不支持")
            return self._start_project_hard_interrupt(pro)
        return self._start_project_soft_interrupt(
            pro, check=check, list_only=list_only)

    def _start_project_hard_interrupt(self, pro):
        """旧的硬中断：下架当前场全部任务，等 title 后建整场任务挂回去。"""
        live = self.live_session()
        if live:
            index, row = live
        else:
            # 和 `task -ac` 的「当前这一场」保持同一口径：没有进行中的场次时，
            # 最近一场已经订上的 upcoming 也可以作为当前场处理。
            try:
                index, row = self.current_session()
            except ValueError:
                raise ValueError("现在没有正在进行的 session，也没有已订上的下一场，"
                                 "没法 interrupt。先开始/订上一场再试。") from None
        date_iso = str(row.get("date") or self.sel_date.isoformat())
        start = str(row.get("startLabel") or row.get("start") or "").strip()
        duration = int(row.get("durationMin") or row.get("duration") or 0)
        if not start or duration <= 0:
            raise ValueError("当前 session 的起点/时长认不出来，不能硬中断。")
        # 同一场 Focusmate meeting 可能因为 relocate 在本地有不同的 startLabel；
        # 有 session id 时以 id 认场，没有才退回「同一天 + 同一起点」。
        meeting_id = str(row.get("id") or row.get("focusmate_session_id") or "")
        tasks = []
        for task in self.task_store.list():
            if not self.task_store.is_open(task):
                continue
            sess = task.get("session") or {}
            same_start = (str(sess.get("date") or "") == date_iso
                          and str(sess.get("start") or "") == start)
            same_meeting = (bool(meeting_id)
                            and str(sess.get("focusmate_session_id") or "") == meeting_id)
            if same_start or same_meeting:
                tasks.append(task)
        detached = []
        for task in tasks:
            title = self.task_store.focusmate_title(task, 30) or ("#%s" % task.get("id"))
            try:
                self._task_detach(task, reason="project-interrupt-hard")
                detached.append(title)
            except Exception as exc:                   # noqa: BLE001 - 一条失败也继续清
                self.had_error = True
                self.say("❌ 硬中断：任务「%s」下架失败：%s" % (title, exc), "error")
        self.pending_interrupt = {
            "mode": "hard",
            "pro_id": pro["id"],
            "pro_title": pro.get("title") or "?",
            "pro_number": self.projects.pro_number_of(pro["id"]) or pro["id"],
            "date": date_iso,
            "start": start,
            "end": str(row.get("endLabel") or ""),
            "duration": duration,
            "index": index,
            "focusmate_session_id": row.get("id"),
            "startMs": row.get("startMs"),
            "detached": detached,
        }
        self.selected_pro_id = pro["id"]
        self.say("⛔ 硬中断：%s %s 当前场的 %d 条任务已下架%s。"
                 % (date_iso, start, len(detached),
                    ("：" + "、".join(detached)) if detached else "（本来就没有任务）"),
                 "warn")
        self.say("请输入新任务的 title（直接回车不发；`q` 取消）。新建任务会占满这一场 %d 分钟。"
                 % duration, "cmd")
        self.invalidate()
        return self.pending_interrupt

    def _start_project_soft_interrupt(self, pro, check=False, list_only=False):
        """软中断入口：记录一条，或进入逐条 check，或列出历史。"""
        if self.pending_soft_interrupt is not None:
            raise ValueError("已经有一条软中断在处理中，先答完（`q` 可取消）")
        pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
        title = self.pro_display_title(pro, fallback=pro.get("title"))
        if list_only:
            records = self.projects.soft_interrupts(pro["id"])
            if not records:
                self.say("项目 #%s「%s」还没有软中断记录。" % (pnum, title), "info")
                return []
            status_zh = {"pending": "待检查", "queued": "已入队",
                         "handled": "已生成任务", "discarded": "已丢弃"}
            self.say("📝 Pro #%s「%s」软中断 %d 条："
                     % (pnum, title, len(records)), "info")
            for n, rec in enumerate(records, 1):
                cat = rec.get("category") or "?"
                label = pj.SOFT_INTERRUPT_CATEGORY_LABELS.get(cat, "未分类")
                self.say("   %d. [%s %s · %s] %s"
                         % (n, cat, label,
                            status_zh.get(rec.get("status"), rec.get("status")),
                            rec.get("text")), "info")
            return records
        if check:
            records = self.projects.soft_interrupts(
                pro["id"], statuses=(pj.SOFT_INTERRUPT_PENDING,))
            queued = self.projects.soft_interrupts(
                pro["id"], statuses=(pj.SOFT_INTERRUPT_QUEUED,))
            if not records and not queued:
                self.say("✅ 项目 #%s「%s」没有待检查的软中断。"
                         % (pnum, title), "ok")
                return []
            if not records:
                self.selected_pro_id = pro["id"]
                if pj.normalize_pro_stage(pro.get("stage")) == pj.STAGE_BOTTLENECK:
                    pushed = self._push_soft_interrupts(pro)
                    self._sync_side_visibility(pro)
                    self.rebuild_rows()
                    self.invalidate()
                    self.say("项目 #%s「%s」已有 %d 条入队软中断 → 已 dump 成 %d 条 "
                             "25 分钟任务（插在项目清单最前）。"
                             % (pnum, title, len(queued), pushed), "ok")
                else:
                    self.say("项目 #%s「%s」已有 %d 条软中断在队列里；"
                             "切到 incubator（`pro %s -a`）时会自动 dump 成任务。"
                             % (pnum, title, len(queued), pnum), "info")
                return []
            self.pending_soft_interrupt = {
                "stage": "check",
                "pro_id": pro["id"],
                "pro_number": pnum,
                "pro_title": pro.get("title") or "?",
                "items": records,
                "index": 0,
                "queued_ids": [rec["id"] for rec in queued],
                "skipped": 0,
                "discarded": 0,
            }
            self.selected_pro_id = pro["id"]
            self.say("🔎 开始检查项目 #%s「%s」的 %d 条软中断：逐条选择 "
                     "1 不处理 / 2 处理（入队）/ 3 丢弃（保存数据）/ q 中止。"
                     % (pnum, title, len(records)), "cmd")
            self.invalidate()
            return self.pending_soft_interrupt
        self.pending_soft_interrupt = {
            "stage": "text",
            "pro_id": pro["id"],
            "pro_number": pnum,
            "pro_title": pro.get("title") or "?",
        }
        self.selected_pro_id = pro["id"]
        self.say("📝 Pro #%s「%s」软中断：先写具体内容（一条建议 / 一个 bug / "
                 "一个摩擦点 / 一个可证伪假设）；`q` 取消。"
                 % (pnum, title), "cmd")
        self.invalidate()
        return self.pending_soft_interrupt

    def _soft_check_next(self):
        pending = self.pending_soft_interrupt
        if pending is None:
            return None
        pending["index"] = int(pending.get("index") or 0) + 1
        if pending["index"] < len(pending.get("items") or []):
            self.invalidate()
            return pending
        queued = list(pending.get("queued_ids") or [])
        skipped = int(pending.get("skipped") or 0)
        discarded = int(pending.get("discarded") or 0)
        pro = self.projects.pro_by_id(pending.get("pro_id"))
        self.pending_soft_interrupt = None
        if not queued:
            self.say("检查完毕：没有入队条目，未创建任务。", "info")
            self.invalidate()
            return None
        if pro is None:
            self.say("检查完毕：入队 %d 条，但项目已经不存在。"
                     % len(queued), "warn")
            self.invalidate()
            return None
        summary = ("检查完毕：入队 %d 条、不处理 %d 条、丢弃 %d 条。"
                   % (len(queued), skipped, discarded))
        if pj.normalize_pro_stage(pro.get("stage")) == pj.STAGE_BOTTLENECK:
            pushed = self._push_soft_interrupts(pro)
            self._sync_side_visibility(pro)
            self.say(summary, "cmd")
            self.say("✅ 已 dump 成 %d 条 25 分钟任务（标签 incubator，插在项目清单最前）。"
                     % pushed, "ok")
        else:
            # 攒着：切到 incubator 时统一 dump（用户 2026-11-21 的 push 语义）。
            self.say(summary, "cmd")
            self.say("✅ 已入队；切到 incubator（`pro %s -a`）时会把它们 dump 成任务。"
                     % (self.projects.pro_number_of(pro["id"]) or pro["id"]), "ok")
        self.rebuild_rows()
        self.invalidate()
        return None

    def _answer_pending_soft_interrupt(self, line):
        """软中断问答：内容 → 类别 → 逐条 check → 任务时长。"""
        pending = self.pending_soft_interrupt
        if pending is None:
            return None
        raw = str(line or "").strip()
        stage = pending.get("stage")
        if stage == "text":
            if raw.lower() in ("q", "quit", "cancel", "取消", "放弃"):
                self.pending_soft_interrupt = None
                self.say("⏹ 已取消软中断记录。", "info")
                self.invalidate()
                return True
            if not raw:
                self.say("软中断内容不能为空；`q` 取消。", "warn")
                return True
            rec = self.projects.add_soft_interrupt(pending["pro_id"], raw)
            pending.update({"stage": "category", "entry_id": rec["id"],
                            "entry_text": rec["text"]})
            self.say("✅ 内容已保存。现在选择类别：", "ok")
            self.say("   A · Bug —— 现有 SOP 跑不通 / 某个知识点反复做错，吞吐暴跌",
                     "info")
            self.say("   B · 摩擦（重复任务）—— 能做完，但每次都在机械操作上浪费体力",
                     "info")
            self.say("   C · 局部优化假设 —— 必须能写成「改 X，下个 session 快 Y 分钟」",
                     "info")
            self.say("输入 A / B / C；`q` 放弃这条（数据仍保留为 discarded）。", "cmd")
            self.invalidate()
            return True
        if stage == "category":
            low = raw.lower()
            if low in ("q", "quit", "cancel", "取消", "放弃"):
                rec = self.projects.update_soft_interrupt(
                    pending["pro_id"], pending["entry_id"],
                    status=pj.SOFT_INTERRUPT_DISCARDED, note="category-abandoned")
                self.pending_soft_interrupt = None
                self.say("🗑 已把刚记录的软中断标为 discarded（原文仍保存在项目库）。",
                         "info")
                self.invalidate()
                return True
            category = pj.normalize_soft_category(raw)
            if category is None:
                self.say("类别只认 A / B / C（也可写 Bug / 摩擦 / 优化）；`q` 放弃。",
                         "warn")
                return True
            rec = self.projects.update_soft_interrupt(
                pending["pro_id"], pending["entry_id"], category=category)
            self.pending_soft_interrupt = None
            label = pj.SOFT_INTERRUPT_CATEGORY_LABELS.get(category, category)
            self.say("✅ 已记录 [%s %s] %s"
                     % (category, label, rec.get("text")), "ok")
            if category == "C" and not any(
                    word in rec.get("text", "")
                    for word in ("如果", "假设", "预计", "?", "？", "分钟", "实测")):
                self.say("⚠ C 类应写成可证伪假设：改动作 X → 下个 session 预计快 Y 分钟；"
                         "这条已保存，检查时请补出 A/B 测试边界。", "warn")
            self.rebuild_rows()
            self.invalidate()
            return True
        if stage == "check":
            items = pending.get("items") or []
            index = int(pending.get("index") or 0)
            if index >= len(items):
                return bool(self._soft_check_next())
            rec = items[index]
            low = raw.lower()
            if low in ("q", "quit", "cancel", "取消", "中止"):
                self.pending_soft_interrupt = None
                self.say("⏹ 已中止软中断检查；本次已处理的状态保留。", "info")
                self.invalidate()
                return True
            if low in ("1", "n", "no", "skip", "不处理", "跳过", "不管"):
                pending["skipped"] = int(pending.get("skipped") or 0) + 1
                self.say("⏭ 不处理：这条继续留在待检查队列。", "info")
                return bool(self._soft_check_next())
            if low in ("2", "p", "process", "queue", "处理", "放入队列", "排队"):
                self.projects.update_soft_interrupt(
                    pending["pro_id"], rec["id"], status=pj.SOFT_INTERRUPT_QUEUED)
                if rec["id"] not in pending["queued_ids"]:
                    pending["queued_ids"].append(rec["id"])
                self.say("➕ 已放入队列，最后会合成为一条 incubator 任务。", "ok")
                return bool(self._soft_check_next())
            if low in ("3", "d", "x", "discard", "丢弃", "放弃", "删掉"):
                self.projects.update_soft_interrupt(
                    pending["pro_id"], rec["id"],
                    status=pj.SOFT_INTERRUPT_DISCARDED, note="checked-discard")
                pending["discarded"] = int(pending.get("discarded") or 0) + 1
                self.say("🗑 已丢弃（原文和类别仍保存在项目库，不再提醒）。", "info")
                return bool(self._soft_check_next())
            self.say("请输入 1（不处理）/ 2（处理，入队）/ 3（丢弃，保存数据）/ q（中止）。",
                     "warn")
            return True
        return True

    def pending_soft_interrupt_panel(self):
        """软中断问答面板（内容 / 类别 / 逐条检查 / 时长）。"""
        pending = self.pending_soft_interrupt
        if pending is None:
            return None
        stage = pending.get("stage")
        pro_txt = self.pro_display_title(
            pending.get("pro_id"), fallback=pending.get("pro_title") or "?")
        body = Text()
        if stage == "text":
            body.append("项目「%s」(#%s) 软中断\n"
                        % (pro_txt, pending.get("pro_number") or "?"), style="bold")
            body.append("  先把具体内容写下来。它不会下架当前 session 的任何任务。\n",
                        style="cyan")
            body.append("\n请输入内容；`q` 取消。", style="bold")
            return Panel(body, title="[bold yellow]📝 soft interrupt · 输入内容[/]",
                         border_style="yellow", box=box.HEAVY)
        if stage == "category":
            body.append("项目「%s」的软中断已保存：\n" % pro_txt, style="bold")
            body.append("  %s\n\n" % (pending.get("entry_text") or ""), style="cyan")
            body.append("  A · Bug —— SOP 跑不通 / 知识点反复做错\n", style="red")
            body.append("  B · 摩擦（重复任务）—— 每次都要浪费体力做机械操作\n",
                        style="magenta")
            body.append("  C · 局部优化假设 —— 必须能写成可证伪的 A/B 边界\n",
                        style="yellow")
            body.append("\n选择 A / B / C；`q` 放弃（数据仍保留）。", style="bold")
            return Panel(body, title="[bold yellow]📝 soft interrupt · 选择类别[/]",
                         border_style="yellow", box=box.HEAVY)
        if stage == "check":
            items = pending.get("items") or []
            index = int(pending.get("index") or 0)
            if index >= len(items):
                body.append("检查已结束。", style="bold")
            else:
                rec = items[index]
                cat = rec.get("category") or "?"
                label = pj.SOFT_INTERRUPT_CATEGORY_LABELS.get(cat, "未分类")
                body.append("项目「%s」软中断检查  %d/%d\n"
                            % (pro_txt, index + 1, len(items)), style="bold")
                body.append("  [%s · %s] %s\n\n"
                            % (cat, label, rec.get("text") or ""), style="cyan")
                body.append("  1 不处理（留在队列）\n", style="dim")
                body.append("  2 处理（放入队列，最后合成任务）\n", style="green")
                body.append("  3 丢弃（保存数据，不再提醒）\n", style="red")
                body.append("  q 中止检查", style="dim")
            return Panel(body, title="[bold yellow]🔎 soft interrupt · 逐条检查[/]",
                         border_style="yellow", box=box.HEAVY)
        return None

    def pending_interrupt_panel(self):
        """硬中断等待 title 时挂在打断栏位的面板。"""
        pending = self.pending_interrupt
        if pending is None:
            return None
        body = Text()
        body.append("项目「%s」(#%s) 硬中断\n"
                    % (self.pro_display_title(
                        pending.get("pro_id"),
                        fallback=pending.get("pro_title") or "?"),
                       pending.get("pro_number") or "?"), style="bold")
        body.append("  目标场：%s %s–%s（%d 分钟）\n"
                    % (pending.get("date") or "?", pending.get("start") or "?",
                       pending.get("end") or "?", int(pending.get("duration") or 0)),
                    style="cyan")
        body.append("  已下架：%s\n"
                    % ("、".join(pending.get("detached") or ()) or "（原本就没有任务）"),
                    style="yellow")
        body.append("\n请输入新任务 title（会新建一条整场长度的任务并挂回这一场）；"
                    "`q` 取消。", style="bold")
        return Panel(body, title="[bold red]⛔ project interrupt · 输入新任务 title[/]",
                     border_style="red", box=box.HEAVY)

    def _answer_pending_interrupt(self, line):
        """硬中断 title 输入：建立整场长度的任务并挂回原 session。"""
        pending = self.pending_interrupt
        if pending is None:
            return None
        raw = str(line or "").strip()
        if not raw:
            self.say("请输入新任务 title（不能为空）；`q` 取消。", "warn")
            return True
        if raw.lower() in ("q", "quit", "cancel", "取消", "放弃"):
            self.pending_interrupt = None
            self.say("⏹ 已取消硬中断；当前场剩余任务已经下架，不会再自动挂回去。",
                     "info")
            self.invalidate()
            return True
        task = None
        try:
            pro = self.projects.pro_by_id(pending.get("pro_id")) or {}
            if not pro:
                raise ValueError("项目已经不在了")
            duration = int(pending.get("duration") or 0)
            active_op = self.projects.current_op_for_pro(pro)
            fields = {"title": raw, "duration": duration,
                      "project": pro.get("title") or pending.get("pro_title") or "",
                      "pro_id": pro.get("id"),
                      "interrupt": "hard"}
            # 沿用项目任务的口径：project_id 指向 OmniFocus op（如果当前阶段有）。
            if active_op is not None:
                fields["project_id"] = active_op["id"]
            task = self.task_store.create(
                fields,
                source={"kind": "project", "title": pro.get("title") or "",
                        "interrupt": "hard", "local_only": True},
            )
            if not th.is_exclusive(task):
                task = self.task_store.modify(task["id"], "exclusive", True)
            session = {
                "index": pending.get("index"),
                "date": pending.get("date"),
                "start": pending.get("start"),
                "end": pending.get("end"),
                "duration": duration,
                "focusmate_session_id": pending.get("focusmate_session_id"),
                "startMs": pending.get("startMs"),
            }
            note = self.assign_task_to(task, session)
            task = self.task_store.get(task["id"])
            assigned = task.get("session") or {}
            if str(assigned.get("start") or "") != str(pending.get("start") or ""):
                raise ValueError(note or "任务没有挂到目标 session")
            self.selected_task_id = task["id"]
            self.pending_interrupt = None
            self.say("✅ 硬中断完成：新任务「%s」%d 分钟，已挂到 %s %s。"
                     % (raw, duration, pending.get("date"), pending.get("start")), "ok")
            self.rebuild_rows()
            self.invalidate()
            return True
        except Exception as exc:                       # noqa: BLE001 - 失败时别重复建任务
            self.had_error = True
            self.pending_interrupt = None
            if task is not None:
                self.say("❌ 硬中断：新任务 #%s「%s」已经建好，但挂到当前场失败：%s。"
                         "用 `task %s -ac` 再挂一次。"
                         % (task.get("id"), raw, exc, self.task_label(task)), "error")
            else:
                self.say("❌ 硬中断失败：%s。当前场的任务已经下架，可以重敲 "
                         "`project <序号> interrupt` 再试。" % exc, "error")
            self.invalidate()
            return True

    def cmd_op(self, args=None):
        """``op`` —— 管理 OmniFocus Project（类型：``SOP`` 或 ``incubator``）。

            op                              列出全部 OmniFocus Project（op）
            op import [sop|incubator] [...] 从剪贴板/链接导入 op 并指定类型
            op <序号>                       查看 op 详情与任务清单
            op <序号> -m complete|discard   本地状态（默认数据视图 disposable 会隐藏）
            op <序号> -m active             恢复 active（非 active 时建议 id:<内部id>）
            op <序号> sop | incubator       设置 op 类型为 SOP 或 incubator
            op <序号> pro <pro序号|名称|none> 关联/解绑真正项目 pro
            op <序号> refresh | remove      刷新或移除该 op
            op refresh | check              全量刷新或每日对账
        """
        self.projects.ensure_tagged_pros()
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        mark_status, mark_toks = self._extract_mark_action(toks)
        if mark_status is not None:
            if len(mark_toks) != 1:
                raise ValueError("用法：op <序号> -m complete|discard"
                                 "（也可 op <序号> complete|discard）")
            try:
                op = self.projects.find(mark_toks[0])
            except ValueError:
                op = self.projects.resolve_any(mark_toks[0])
            updated = self.projects.set_op_status(
                op["id"], mark_status, reason="cmd-op-mark-%s" % mark_status)
            return self._finish_mark_status("op", updated, mark_status)
        if not toks:
            rows = self.project_rows()
            if not rows:
                self.say("📁 OmniFocus 项目库（op）是空的：在 OmniFocus 里 ⌘C 一个项目，"
                         "再敲 `op import sop` 或 `task import sop`。", "warn")
                return []
            for row in rows:
                op_kind = row.get("op_type") or "⚠未设类型"
                pro_bit = (" · pro:%s(%s)" % (row["pro_title"], row.get("pro_stage") or "running")
                           if row.get("pro_title") else "")
                allow_bit = "" if row.get("allowed", True) else " · ⏸受项目阶段限制"
                self.say("📁 op #%d %s [%s%s%s]：剩余 %s count · 进度 %s · %d 条未完成 · 本周 %d 条"
                         % (row["number"], row["title"], op_kind, pro_bit, allow_bit,
                            row.get("remaining_text") or row.get("remaining") or 0,
                            row["progress_text"], row["tasks_open"],
                            int(row.get("week_count") or 0)), "info")
            return rows
        first_low = toks[0].lower()
        if first_low in ("import", "导入", "粘贴"):
            return self.cmd_task_import(toks[1:])
        if pj.normalize_op_type(first_low) in pj.OP_TYPES and len(toks) >= 2 and toks[1].lower() in ("import", "导入", "粘贴"):
            return self.cmd_task_import([toks[0]] + toks[2:])
        if first_low in ("check", "对账", "daily", "每日检查"):
            self._project_sync_day = None
            return self._start_project_sync_now()
        if first_low in ("refresh", "sync", "reload", "更新"):
            made = [self._project_refresh(project) for project in self.projects.active()]
            return [p for p in made if p]
        op = self.projects.find(toks[0])
        rest = toks[1:]
        if not rest:
            return self._project_detail(op)
        rel = repl_kit.relation.parse(toks, "op")
        if rel is not None:
            return self._op_relate(op, rel)
        r0 = rest[0].lower()
        if r0 in ("refresh", "sync", "reload", "更新"):
            return self._project_refresh(op)
        if r0 in ("remove", "rm", "delete", "撤掉", "删除"):
            num = self.projects.number_of(op["id"]) or op["id"]
            removed = self.task_store.remove_project_backlog(op["id"])
            self.projects.remove(op["id"])
            self.rebuild_rows()
            self.invalidate()
            self.say("🗑 op #%d「%s」已从项目库撤掉%s。"
                     % (num, op.get("title"),
                        "（清掉 %d 条未唤起镜像任务）" % removed if removed else ""), "ok")
            return op
        if r0 in ("type", "--type", "-t", "类型") or pj.normalize_op_type(r0) in pj.OP_TYPES:
            val = rest[1] if r0 in ("type", "--type", "-t", "类型") and len(rest) > 1 else rest[0]
            norm_t = pj.normalize_op_type(val)
            if norm_t not in pj.OP_TYPES:
                raise ValueError("op 类型只能是 SOP 或 incubator；你给的是 %r" % val)
            updated = self.projects.set_op_type(op["id"], norm_t)
            if self.pending_op_type:
                self.pending_op_type = [oid for oid in self.pending_op_type
                                        if oid != updated["id"]]
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pro = self.projects.pro_for_op(updated)
            pro_note = (" · 关联项目「%s」(%s)"
                        % (self.pro_display_title(pro, fallback=pro.get("title")),
                           pro.get("stage")) if pro else "")
            self.say("✅ op #%s「%s」类型已设为 %s%s。"
                     % (num, updated.get("title"), norm_t, pro_note), "ok")
            self.rebuild_rows()
            self.invalidate()
            return updated
        if r0 in ("pro", "project", "link", "关联"):
            if len(rest) < 2:
                raise ValueError("用法：op <序号> pro <pro序号|名称|none>")
            target_tok = " ".join(rest[1:]).strip()
            if target_tok.lower() in ("none", "clear", "off", "无", "清空"):
                updated, _ = self.projects.link_op_to_pro(op["id"], None)
                num = self.projects.number_of(updated["id"]) or updated["id"]
                self.say("⏏ op #%s「%s」已解除项目（pro）关联。" % (num, updated.get("title")), "ok")
                self.invalidate()
                return updated
            pro = self._ensure_pro_for_token(target_tok)
            updated, pro = self.projects.link_op_to_pro(op["id"], pro["id"])
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            self.say("🔗 op #%s「%s」已关联到项目 Pro #%s「%s」（阶段：%s）。"
                     % (num, updated.get("title"), pnum,
                        self.pro_display_title(pro, fallback=pro.get("title")),
                        pro.get("stage")), "ok")
            self.invalidate()
            return updated
        raise ValueError("op 子命令只认 sop / incubator / pro <N> / refresh / remove；你给的是 %r"
                         % rest[0])

    # -- relate：关联文件（模板只给 incubator op 用）--------------------

    def _relate_dir(self):
        """``relate-dir`` 设置里指定的目录；相对路径按 warden 所在目录算。"""
        raw = str(self.settings.get("relate-dir") or "").strip() or "data/relate"
        if os.path.isabs(raw):
            return raw
        return os.path.join(HERE, raw)

    def _op_relate(self, op, rel):
        """``op <序号> relate ...`` 的分流。解析逻辑在 repl_kit.relation。"""
        if rel.has_flag("--export", "-e", "export"):
            return self._relate_export(op)
        if rel.has_flag("--import", "-i", "import"):
            return self._relate_import(op, rel)
        raise ValueError("relate 只认 --export / -e（生成模板）或 --import / -i（解析并 append）；"
                         "你给的是 %r" % (" ".join(rel.args) or "（空）"))

    def _relate_export(self, op):
        """在 ``relate-dir`` 生成 incubator SOP 模板，并把路径记到 op 关联文件。"""
        num = self.projects.number_of(op["id"]) or op["id"]
        if pj.normalize_op_type(op.get("op_type")) != pj.OP_TYPE_INCUBATOR:
            self.say("⏭ op #%s「%s」不是 incubator 类型；relate 模板只给 incubator op 用，"
                     "这条命令什么也没做。先用 `op %s incubator` 再试。"
                     % (num, op.get("title"), num), "warn")
            return None
        pro = self.projects.pro_for_op(op)
        if pro is None:
            raise ValueError("op #%s「%s」没有关联真正项目 pro；先 `op %s pro <pro序号>` 再 relate。"
                             % (num, op.get("title"), num))
        pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        pro_title = pro.get("title") or ("pro #%s" % pnum)
        body = ("---\n"
                "created: %s\n"
                "updated: %s\n"
                "关联项目: %s\n"
                "---\n"
                "**用途**: 被incubator 使用，用来准备一个 SOP"
                "（可以是无规律的，也就是说是 research + brainstorm 产生的 SOP）\n"
                "# 资源准备任务列表\n"
                "- example\n"
                "\n"
                "# 具体的 SOP 流程\n"
                "**仔细思考，代入其中。作为一个实际上执行 SOP 的人，"
                "你会需要提前准备什么还没有列出来的东西？**\n"
                "- example\n") % (now, now, pro_title)
        directory = self._relate_dir()
        os.makedirs(directory, exist_ok=True)
        slug = re.sub(r"[^\w\-]+", "-", str(op.get("title") or "op")).strip("-")[:40] or "op"
        path = os.path.join(directory, "op-%d-%s.md" % (op["id"], slug))
        if os.path.exists(path):
            path = os.path.join(directory, "op-%d-%s-%d.md"
                                % (op["id"], slug, int(time.time())))
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        updated = self.projects.set_project_file(op["id"], path)
        self.say("📄 已生成 relate 模板：%s" % path, "ok")
        self.say("   关联项目：%s（pro #%s）· 已记到 op #%s「%s」的关联文件。"
                 % (pro_title, pnum, num, updated.get("title")), "info")
        self.rebuild_rows()
        self.invalidate()
        return updated

    def _relate_import(self, op, rel):
        """``op <序号> relate --import [绝对路径]``：解析模板并 append 到项目库。"""
        num = self.projects.number_of(op["id"]) or op["id"]
        if pj.normalize_op_type(op.get("op_type")) != pj.OP_TYPE_INCUBATOR:
            self.say("⏭ op #%s「%s」不是 incubator 类型；relate 模板只给 incubator op 用，"
                     "这条命令什么也没做。先用 `op %s incubator` 再试。"
                     % (num, op.get("title"), num), "warn")
            return None
        pro = self.projects.pro_for_op(op)
        if pro is None:
            raise ValueError("op #%s「%s」没有关联真正项目 pro；先 `op %s pro <pro序号>` 再 relate。"
                             % (num, op.get("title"), num))
        sop_op = self._relate_sop_op(pro, exclude_op=op)
        if sop_op is None:
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            raise ValueError("项目 Pro #%s「%s」没有对应的 SOP op；"
                             "relate 模板的「具体的 SOP 流程」没地方放，"
                             "先 `pro %s op <SOP op序号>` 关联好再做任何导入。"
                             % (pnum, self.pro_display_title(pro, fallback=pro.get("title")),
                                pnum))
        path = rel.flag_value("--import", "-i")
        if not path:
            for _arg in rel.args:
                if _arg.lower().startswith("--import="):
                    path = _arg.split("=", 1)[1]
                    break
                if _arg.lower().startswith("-i="):
                    path = _arg.split("=", 1)[1]
                    break
        if not path:
            path = op.get("file")
        if path and not os.path.exists(path):
            raise ValueError("关联文件不存在：%s（先 `op %s relate --export` 重新生成，"
                             "或给一个存在的绝对路径）。" % (path, num))
        if not path:
            if getattr(self.shell, "_app_running", False):
                self.pending_relate_path = {"op_id": op["id"]}
                self.say("📄 op #%s「%s」还没有关联文件；请输入要 import 的绝对路径（q 取消）："
                         % (num, op.get("title")), "cmd")
                self.invalidate()
                return None
            raise ValueError("op #%s「%s」还没有关联文件；用法：op %s relate --import <绝对路径>。"
                             % (num, op.get("title"), num))
        return self._relate_import_from_path(op, pro, sop_op, path)

    def _relate_sop_op(self, pro, exclude_op=None):
        """找到 ``pro`` 对应的 SOP op；没有返回 None。"""
        store = self.projects.load()
        sop_ops = [op for op in store.get("projects") or ()
                   if op.get("status") == pj.STATUS_ACTIVE
                   and pj.normalize_op_type(op.get("op_type")) == pj.OP_TYPE_SOP]
        by_id = {op["id"]: op for op in sop_ops}
        if pro.get("sop_op_id"):
            hit = by_id.get(pro["sop_op_id"])
            if hit is not None and (exclude_op is None or hit["id"] != exclude_op["id"]):
                return hit
        pro_id = pro.get("id")
        for op in sop_ops:
            if exclude_op is not None and op["id"] == exclude_op["id"]:
                continue
            linked = self.projects.pro_for_op(op, store=store)
            if linked is not None and linked["id"] == pro_id:
                return op
        return None

    def _relate_import_from_path(self, op, pro, sop_op, path):
        """读模板文件 → 解析两节 → 校验 → append 到两个 op 的清单。"""
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise ValueError("读不了 %s：%s" % (path, e))
        sections = parse_relate_markdown(text)
        resource_tasks = sections.get("资源准备任务列表") or []
        sop_tasks = sections.get("具体的 SOP 流程") or []
        if not resource_tasks and not sop_tasks:
            raise ValueError("模板里没有找到「# 资源准备任务列表」或「# 具体的 SOP 流程」"
                             "下面的无序列表。")
        res_members = self._relate_validate_section(op, resource_tasks, "res")
        sop_members = self._relate_validate_section(sop_op, sop_tasks, "sop")
        # 两节都合法才动手，避免只 append 一半。
        if res_members:
            updated = self.projects.append_tasks(op["id"], res_members)
            self.sync_project_tasks(self.projects.by_id(op["id"]), quiet=True)
        else:
            updated = self.projects.by_id(op["id"])
        if sop_members:
            self.projects.append_tasks(sop_op["id"], sop_members)
            self.sync_project_tasks(self.projects.by_id(sop_op["id"]), quiet=True)
        if updated is not None and str(updated.get("file") or "") != str(path):
            updated = self.projects.set_project_file(op["id"], path)
        self.rebuild_rows()
        self.invalidate()
        num = self.projects.number_of(op["id"]) or op["id"]
        snum = self.projects.number_of(sop_op["id"]) or sop_op["id"]
        self.say("📥 relate 导入完成：%s" % path, "ok")
        self.say("   资源准备任务列表 → incubator op #%s「%s」append %d 条。"
                 % (num, updated.get("title") if updated else op.get("title"),
                    len(res_members)), "info")
        self.say("   具体的 SOP 流程 → SOP op #%s「%s」append %d 条。"
                 % (snum, sop_op.get("title"), len(sop_members)), "info")
        self.say("   这两批任务都只落在本地项目库（OmniFocus 没动）；"
                 "等 `plan` 分到 count 就会按项目任务正常唤起。", "info")
        return updated

    def _relate_validate_section(self, target_op, tasks, section):
        """把一节里的 bullets 变成项目任务记录；没时长的整节报错（复用项目检查）。"""
        stamp = int(time.time() * 1000)
        root = {"id": "__relate_root__", "name": target_op.get("title") or "relate",
                "kind": "project_root", "project": "",
                "estimated_minutes": None}
        rows = [root]
        for index, task in enumerate(tasks or []):
            pk = "relate-%d-%s-%d-%d" % (target_op["id"], section, index + 1, stamp)
            rec = parse_relate_task(task, pk)
            rows.append({
                "id": rec["pk"],
                "name": rec["title"] or ("relate 任务 %d" % (index + 1)),
                "kind": "action",
                "project": target_op.get("title") or "",
                "estimated_minutes": rec["minutes"],
                "note": rec["note"],
                "completed": False,
                "dropped": False,
            })
        try:
            payload = pj.project_payload(root, rows)
        except ValueError as e:
            raise ValueError("「%s」这节解析不了：%s" % (section, e))
        if payload.get("missing"):
            raise ValueError(pj.format_missing(payload))
        if payload.get("depth_errors"):
            raise ValueError("「%s」这节有超过父-子两层的任务，先改成平铺的无序列表再 import。"
                             % section)
        members = list(payload.get("tasks") or [])
        for member in members:
            member["local_only"] = True
        return members

    def pending_relate_path_panel(self):
        """``op <序号> relate --import`` 等用户输入绝对路径的面板。"""
        pending = self.pending_relate_path or {}
        op = self.projects.by_id(pending.get("op_id"))
        num = self.projects.number_of(op["id"]) if op else None
        title = (op or {}).get("title") or "?"
        body = Text()
        body.append("op #%s「%s」还没有关联文件，请直接输入要 import 的绝对路径：\n"
                    % (num or pending.get("op_id"), title), style="bold")
        body.append("（`op <序号> relate --export` 会先建好模板并自动关联；q 取消）\n",
                    style="dim")
        return Panel(body, title="[bold cyan]📄 relate --import[/]",
                     border_style="cyan", box=box.HEAVY)

    def _answer_pending_relate_path(self, line):
        """处理 ``pending_relate_path`` 下的输入：路径或 q。"""
        pending = self.pending_relate_path
        if pending is None:
            return None
        raw = " ".join(str(line or "").strip().split())
        if not raw:
            return True
        if raw.lower() in ("q", "quit", "cancel", "取消", "放弃"):
            self.pending_relate_path = None
            self.say("⏹ 已取消 relate --import。", "info")
            self.invalidate()
            return True
        self.pending_relate_path = None
        try:
            op = self.projects.by_id(pending.get("op_id"))
            if op is None:
                raise ValueError("op %s 不见了" % pending.get("op_id"))
            if not os.path.isabs(raw):
                raise ValueError("要绝对路径：%r" % raw)
            if not os.path.exists(raw):
                raise ValueError("文件不存在：%s" % raw)
            rel = repl_kit.relation.parse(
                [str(pending.get("op_id")), "relate", "--import", raw], "op")
            if rel is None:
                raise ValueError("内部解析失败：%r" % raw)
            self._relate_import(op, rel)
        except Exception as e:                      # noqa: BLE001
            self.had_error = True
            self.say(str(e), "warn")
        self.invalidate()
        return True

    def _project_detail(self, project):
        """一个项目的清单 + 今天的分工（`project 1`）。"""
        invested = pj.invested_by_project(self.task_store).get(project["id"]) or {}
        stat = pj.stats(project, self.task_store, invested)
        num = self.projects.number_of(project["id"]) or project["id"]
        detail_extra = ("" if stat["total_high"] > stat["total"]
                        else "（%d/%d）" % (stat["done"], stat["total"]))
        self.say("📁 #%d「%s」OmniFocus %s · 排序 %s · 剩余 %s count · 进度 %s%s"
                 % (num, project.get("title"),
                    ((project.get("omnifocus") or {}).get("id") or "?"),
                    pj.task_type_of(project), stat["remaining_text"],
                    stat["progress_text"], detail_extra), "cmd")
        if project.get("file"):
            self.say("   📄 关联文件：%s" % project.get("file"), "info")
        week = pj.weekly_yield(project.get("tasks") or [])
        if week.get("count"):
            self.say("   " + pj.week_summary(week), "info")
            if week.get("missing"):
                self.say("   ⚠ 本周有 %d 条任务没设持续时间：%s"
                         % (len(week["missing"]),
                            "、".join("「%s」" % rec.get("title")
                                     for rec in week["missing"][:6])), "warn")
        items = pj.remaining_tasks(project, invested)
        for item in items[:12]:
            bits = ["%d count" % int(item.get("count") or 0),
                    "还剩 %d" % int(item.get("remaining") or 0)]
            if item.get("due"):
                bits.append("截止 %s" % str(item["due"]).replace("T", " ")[:16])
            if item.get("blocked"):
                bits.append("被前置阻塞")
            self.say("   · %s（%s）" % (item.get("title"), " · ".join(bits)), "info")
        if len(items) > 12:
            self.say("   …还有 %d 条" % (len(items) - 12), "info")
        plan = pj.plan_of(project, self.sel_date)
        if plan and (plan.get("count") or plan.get("items")):
            self.say("   今天（%s）说好 %d count，已经排了 %d 条："
                     % (self.sel_date.isoformat(), int(plan.get("count") or 0),
                        len(plan.get("items") or [])), "info")
            for rec in plan.get("items") or []:
                task = None
                try:
                    task = self.task_store.get(rec["task_id"])
                except (ValueError, TypeError):
                    task = None
                self.say("     %s(%d) ← 任务 %s「%s」%s"
                         % (rec.get("start") or "?", int(rec.get("duration") or 0),
                            self.task_no(task) or rec["task_id"],
                            (task or {}).get("fields", {}).get("title") or "?",
                            "" if task else "（本地任务已经不在了）"), "info")
        else:
            self.say("   今天还没给它分资源（`plan` 分 count 才会唤起任务）。", "info")
        return project

    def _project_refresh(self, project):
        """重新读 OmniFocus 里这个项目的清单（写回项目库）。"""
        project = project or {}
        pk = (project.get("omnifocus") or {}).get("id")
        num = self.projects.number_of(project.get("id")) or project.get("id")
        if not pk:
            self.say("⚠ 项目 #%s 没有 OmniFocus 关联，刷不了。" % num, "warn")
            return None
        try:
            if not self.omnifocus_enabled():
                raise ValueError("OmniFocus 关着（`set omnifocus on` 打开）")
            rows = self.omnifocus.list_tasks(refresh=True)
        except Exception as e:                      # noqa: BLE001 - 读不到就说清楚
            self.had_error = True
            self.say("⚠ 刷新项目 #%s 失败：%s" % (num, e), "error")
            return None
        row = next((r for r in rows or () if str(r.get("id") or "") == str(pk)), None)
        if row is None:
            self.say("⚠ OmniFocus 里找不到项目 %s（被删了？）—— 项目库先留着旧的清单。"
                     % pk, "warn")
            return None
        payload = pj.project_payload(row, rows)
        payload = pj.attach_week(
            payload, self._project_repeat_infos(
                payload, lambda text, kind="warn": self.say(text, kind)))
        if (payload.get("missing") or payload.get("week_missing")
                or payload.get("depth_errors")):
            self.had_error = True
            self.say("✗ " + (pj.format_missing(payload)
                             or pj.format_depth_errors(payload)), "error")
            self.say("🚫 这次刷新没写库（清单先按旧的算）。", "error")
            return None
        updated, _created = self.projects.upsert(payload)
        self.sync_project_tasks(updated, quiet=False)
        try:
            self.check_untyped_ops(announce_import=False)
        except Exception:                      # noqa: BLE001 - 检查失败不挡刷新
            pass
        stat = pj.stats(updated, self.task_store)
        week = pj.weekly_yield(updated.get("tasks") or [])
        self.say("🔄 项目 #%s「%s」刷新了：%d 条任务、剩余 %s count（进度 %s）"
                 % (num, updated.get("title"), len(updated.get("tasks") or []),
                    stat["remaining_text"], stat["progress_text"]), "ok")
        if week.get("count"):
            self.say(pj.week_summary(week), "info")
        self.rebuild_rows()
        self.invalidate()
        return updated

    def _project_tasks_refreshable(self):
        """``refresh`` 能不能顺带刷新 OmniFocus 项目任务清单。"""
        if self.offline or not self.net_async:
            return False
        try:
            if not self.omnifocus_enabled():
                return False
        except Exception:                           # noqa: BLE001 - 检查失败不挡刷新
            return False
        try:
            return bool(self.projects.active())
        except Exception:                           # noqa: BLE001
            return False

    def _refresh_project_tasks(self):
        """``refresh`` 时把 OmniFocus 项目任务清单也对一遍（60→50 这种改动要能更新）。"""
        projects = self.projects.active()
        if not projects:
            return False
        today = datetime.date.today()
        self._project_sync_day = today
        try:
            pj.mark_sync_day(today)
        except Exception:                           # noqa: BLE001 - 标记失败不挡对账
            pass
        action = self.net_action(
            "omnifocus.project_sync",
            "刷新 %d 个 OmniFocus 项目任务" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say(
                "OmniFocus 项目任务刷新失败（可用 `project check` 再试）：%s" % error,
                "warn"))
        if not self.jobs.sync:
            self.say("正在后台刷新 OmniFocus 项目任务…", "cmd")
        return True

    # -- 每日项目对账：新增自动拉，删除/修改整包问 -------------------------

    def _prompt_busy(self):
        return bool(self.pending_import is not None or self.nostart is not None
                    or self.planning is not None or self.review_active()
                    or self.pending_advance is not None
                    or self.pending_interrupt is not None
                    or self.pending_soft_interrupt is not None
                    or self.project_sync is not None)

    def _maybe_sync_sides(self, max_age=120.0):
        """刷新检查：按当前阶段冻结非活跃侧、解冻活跃侧（最多两分钟一次）。

        用户在 OmniFocus 里自己加任务 / 改阶段标签后，下一次 refresh 就会把
        该冻的冻上、该解冻的解冻；幂等，没有变化时不写盘也不发外部进程。
        """
        now = time.time()
        last = getattr(self, "_side_sync_at", 0)
        if last and now - last < max_age:
            return False
        self._side_sync_at = now
        frozen = thawed = 0
        for pro in self.projects.active_pros():
            try:
                res = self._sync_side_visibility(pro, announce=False)
            except Exception as e:                 # noqa: BLE001 - 一个坏了不挡别的
                rl.log_event("project", "side_sync_failed", level="warning",
                             pro=pro.get("id"),
                             error="%s: %s" % (type(e).__name__, e))
                continue
            frozen += int(res.get("frozen") or 0)
            thawed += int(res.get("thawed") or 0)
        if frozen or thawed:
            rl.log_event("project", "side_sync_refresh", level="info",
                         frozen=frozen, thawed=thawed)
            self.invalidate()
        return True

    def _maybe_daily_project_sync(self):
        """每天第一次 TUI/refresh 时后台检查一次 OmniFocus 项目。"""
        today = datetime.date.today()
        if (not self.auto_project_sync or self.offline or not self.net_async
                or self._project_sync_day == today
                or pj.last_sync_day() == today.isoformat()
                or self._prompt_busy()):
            return False
        try:
            if not self.omnifocus_enabled():
                return False
        except Exception:                           # noqa: BLE001 - 检查失败不挡 TUI
            return False
        projects = self.projects.active()
        if not projects:
            return False
        self._project_sync_day = today
        pj.mark_sync_day(today)
        action = self.net_action(
            "omnifocus.project_sync",
            "每日检查 %d 个 OmniFocus 项目" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say(
                "每日项目检查失败（明天再试，或 `project 1 refresh`）：%s" % error,
                "warn"))
        return True

    def _start_project_sync_now(self):
        """手动触发一次项目对账（``project check``；也方便测试）。"""
        projects = self.projects.active()
        if not projects:
            raise ValueError("项目库是空的：先 `task import` 一个 OmniFocus 项目。")
        action = self.net_action(
            "omnifocus.project_sync",
            "检查 %d 个 OmniFocus 项目" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say("项目对账失败：%s" % error, "warn"))
        if not self.jobs.sync:
            self.say("正在后台检查 OmniFocus 项目…", "cmd")
        return True

    def _merge_project_additions(self, project_id, added):
        """把 OmniFocus 新增任务并进本地清单；已有任务一个字不动。"""
        project = self.projects.by_id(project_id)
        if project is None:
            return 0
        tasks = [dict(item) for item in project.get("tasks") or []]
        have = {str(item.get("pk") or "") for item in tasks}
        n = 0
        for item in added or ():
            pk = str(item.get("pk") or "")
            if not pk or pk in have:
                continue
            tasks.append(dict(item))
            have.add(pk)
            n += 1
        if n:
            self.projects.set_tasks(project_id, tasks)
        return n

    def _apply_project_sync_result(self, payload):
        """主线程：自动拉新增；note 直接拉；删除 / 结构修改按项目排队问用户。"""
        records = (payload or {}).get("projects") or []
        for rec in records:
            title = rec.get("title") or rec.get("project_id") or "?"
            if rec.get("invalid"):
                self.say("⚠ 每日项目检查跳过「%s」：%s" % (title, rec["invalid"]),
                         "warn")
                continue
            diff = rec.get("diff") or {}
            added = diff.get("added") or []
            if added:
                n = self._merge_project_additions(rec.get("project_id"), added)
                if n:
                    self.say("📥 项目「%s」自动拉取 %d 条新增任务。" % (title, n), "ok")
            # note 不参与冲突问答（见 project_store.note_changes）：@list 清单
            # 就是 note，晚一天拉回来 = 这一天 plan 少派生一条任务。
            pulled = self._pull_project_notes(rec)
            if pulled:
                self.say("📝 项目「%s」的 note 拉了 %d 条（清单/说明以 OmniFocus 为准）。"
                         % (title, pulled), "ok")
            if diff.get("removed") or diff.get("modified"):
                self._project_sync_queue.append(rec)
            project = self.projects.by_id(rec.get("project_id"))
            if project is not None:
                self.sync_project_tasks(project, quiet=True)
        try:
            self.check_untyped_ops(announce_import=False)
            self.projects.ensure_tagged_pros()
        except Exception as e:                     # noqa: BLE001 - 检查失败不挡对账
            self.say("检查 op 类型/项目关联失败：%s" % e, "warn")
        if self._project_sync_queue:
            self._maybe_start_project_sync_conflict()
        self.rebuild_rows()
        self.invalidate()
        return payload

    def _pull_project_notes(self, rec):
        """把 OmniFocus 那边的 note 拉回项目清单 + 本地镜像。返回改了几条。

        note 是**用户手写的内容**，并且 ``@list`` 清单的资格 / 还剩几行全看它
        （见 ``derived.py``）—— 所以它不进 ``DIFF_FIELDS`` 的冲突问答：远端改过
        就直接覆盖本地。唯一例外是 outbox 里还排着 ``note_mark`` 的那几条：本地
        比远端新，拉回来会把刚勾掉的那一行变回没勾。
        """
        payload = (rec or {}).get("payload") or {}
        remote = [item for item in (payload.get("tasks") or ())
                  if isinstance(item, dict)]
        if not remote:
            return 0
        try:
            project = self.projects.by_id(rec.get("project_id"))
        except (ValueError, TypeError):
            project = None
        if project is None:
            return 0
        pending = set()
        outbox = getattr(self, "omni_outbox", None)
        for op in (getattr(outbox, "ops", None) or ()):
            if str((op or {}).get("op") or "") == "note_mark":
                pending.add(str((op or {}).get("id") or ""))
        changes = pj.note_changes(project.get("tasks") or [], remote, pending)
        if not changes:
            return 0
        self.projects.set_task_notes(project["id"], changes)
        # 本地镜像也换掉（``-d`` / 任务面板读的是它）；已挂 session 的任务不动，
        # 那是当天的真实账，note 留着也碍不着派生（派生优先读项目清单）。
        by_pk = {str(item.get("pk") or ""): item for item in remote}
        for task in self.task_store.list():
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk or pk not in changes:
                continue
            if not ts.TaskStore.is_open(task) or task.get("session"):
                continue
            note = changes[pk]
            parent_pk = str((by_pk.get(pk) or {}).get("parent_id") or "")
            if parent_pk:
                parent_title = (str((by_pk.get(pk) or {}).get("parent") or "").strip()
                                or parent_pk)
                note = (note + "\n父任务：" + parent_title).strip()
            fields = task.get("fields") or {}
            if str(fields.get("notes") or fields.get("note") or "") == note:
                continue
            try:
                self.task_store.set_notes(task["id"], note)
            except (ValueError, TypeError):
                continue
        self._derived_span_map_cache = None
        return len(changes)

    def _maybe_start_project_sync_conflict(self):
        """没有别的问答在占地时，把下一个项目冲突顶上来。"""
        if self.project_sync is not None or not self._project_sync_queue:
            return False
        if self._prompt_busy():
            return False
        self.project_sync = self._project_sync_queue.pop(0)
        self.invalidate()
        return True

    def _answer_project_sync(self, line):
        """项目对账冲突：l=本地覆盖 / o=OmniFocus 覆盖 / s=先不处理。"""
        text = " ".join(str(line or "").strip().lower().split())
        local = {"l", "local", "本地", "本地覆盖", "1"}
        remote = {"o", "of", "omnifocus", "remote", "远端", "omnifocus覆盖", "2"}
        skip = {"s", "skip", "稍后", "先不处理", "不处理", "q", "3"}
        rec, self.project_sync = self.project_sync, None
        title = (rec or {}).get("title") or "?"
        if text in remote:
            payload = (rec or {}).get("payload")
            try:
                if payload is None:
                    raise ValueError("没有拿到 OmniFocus 清单")
                updated, _created = self.projects.upsert(payload)
                self.sync_project_tasks(updated, quiet=False)
                self.say("📁 项目「%s」已按 OmniFocus 覆盖本地清单。" % title, "ok")
            except Exception as e:                  # noqa: BLE001 - 对账坏了别掀 TUI
                self.had_error = True
                self.say("项目「%s」没能用 OmniFocus 覆盖：%s" % (title, e), "error")
        elif text in local:
            self.say("📁 项目「%s」保留本地清单（OmniFocus 的删除/修改先不应用）。"
                     % title, "ok")
        elif text in skip:
            self.say("📁 项目「%s」这次对账先不处理。" % title, "info")
        else:
            self.project_sync = rec
            self.say("项目对账还在等回答：\n"
                     "  [l] 保留本地覆盖  [o] 用 OmniFocus 覆盖  [s] 先不处理", "warn")
            return True
        self.rebuild_rows()
        self.invalidate()
        self._maybe_start_project_sync_conflict()
        return True

    def project_sync_panel(self):
        """项目对账冲突的打断面板。"""
        rec = self.project_sync or {}
        diff = rec.get("diff") or {}
        body = Text()
        body.append("项目「%s」和 OmniFocus 不一致：\n"
                    % (rec.get("title") or rec.get("project_id") or "?"),
                    style="bold")
        body.append("  " + pj.diff_text(diff) + "\n", style="cyan")
        for item in (diff.get("removed") or [])[:5]:
            body.append("  - 本地有、OmniFocus 没有：%s\n" % (item.get("title") or item.get("pk")),
                        style="yellow")
        for item in (diff.get("modified") or [])[:5]:
            fields = "、".join(change.get("field") or "?"
                              for change in item.get("changes") or [])
            body.append("  ~ 两边不同：%s（%s）\n"
                        % (item.get("title") or item.get("pk"), fields),
                        style="yellow")
        body.append("\n[l] 本地覆盖    [o] OmniFocus 覆盖    [s] 先不处理",
                    style="bold")
        return Panel(body, title="[bold cyan]📁 项目对账[/]",
                     border_style="cyan", box=box.HEAVY)

    def _plan_owned_task_ids(self, day, rescindable_only=False):
        """当天 project plan 记下的本地 task id。

        ``rescindable_only=True`` 只收还没结束的任务：已经完成/放弃的旧计划
        属于历史，不能因为重新 plan 就被当成空场复用。
        """
        out = set()
        try:
            # op + 简单 pro：简单项目的 items 记在 pro 上，只看 op 会让它上一轮
            # 排下去的场次在重排时被当成「有人占着」→ 整段没法重切（2 count 的
            # 旧场次把 T1 钉成 50+50，而不是按新配额切 75+25）。
            projects = self.projects.plan_refs()
        except Exception:                          # noqa: BLE001 - 坏库不挡正常 plan
            return out
        for project in projects or ():
            plan = pj.plan_of(project, day) or {}
            for item in plan.get("items") or ():
                try:
                    task_id = int(item.get("task_id"))
                except (TypeError, ValueError):
                    continue
                if rescindable_only:
                    try:
                        task = self.task_store.get(task_id)
                    except (ValueError, TypeError):
                        continue
                    if not ts.TaskStore.is_open(task):
                        continue
                out.add(task_id)
        # 任务级固定项也算「上一轮 plan 排下去的」：重排时它们的场次要回到可用容量里。
        for item in self._plan_pins(day):
            try:
                task_id = int(item.get("task_id"))
            except (TypeError, ValueError):
                continue
            if rescindable_only:
                try:
                    task = self.task_store.get(task_id)
                except (ValueError, TypeError):
                    continue
                if not ts.TaskStore.is_open(task):
                    continue
            out.add(task_id)
        return out

    def _plan_pins(self, day):
        """当天 ``task <任务> <count>`` 排下去的固定项。

        固定项**不一定属于任何 project**，所以不能记进项目库的 plans；但重排 /
        ``plan clear`` 必须能把它们解绑 —— 否则那些任务永远占着场次。台账就落
        在这里（``temp/plan-pins.json``，按日期分）。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        try:
            with open(PLAN_PINS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        items = (data or {}).get(iso) or []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _save_plan_pins(self, day, items=(), append=False):
        """写当天的固定项台账；``append=True`` 时按 ``task_id`` 合并（后写覆盖）。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        try:
            with open(PLAN_PINS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        merged = {}
        if append:
            for item in data.get(iso) or ():
                if isinstance(item, dict) and item.get("task_id") is not None:
                    merged[str(item.get("task_id"))] = item
        for item in items or ():
            if isinstance(item, dict) and item.get("task_id") is not None:
                merged[str(item.get("task_id"))] = item
        if merged:
            data[iso] = list(merged.values())
        else:
            data.pop(iso, None)
        try:
            tmp = PLAN_PINS_FILE + ".tmp"
            os.makedirs(os.path.dirname(PLAN_PINS_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, PLAN_PINS_FILE)
        except OSError as e:                       # noqa: BLE001 - 台账坏了不挡排期
            rl.log_event("plan", "pins_save_failed", level="warning",
                         day=iso, error=str(e))
        return list(merged.values())

    # -- plan 的「资源决定」台账（2026-11-21）-------------------------
    #
    # 决定 = 项目 count（`pro N M`）+ 任务固定项（`task <任务> <count>`）。
    # 它和时间安排（items / start / duration）分开存：重新打开 plan 要按它预填，
    # 重新 complete 要按它复现同一份计划；只有 `plan clear` 才清掉。

    def _plan_decisions(self):
        try:
            with open(PLAN_DECISIONS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _plan_decision(self, day):
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        rec = self._plan_decisions().get(iso)
        return dict(rec) if isinstance(rec, dict) else None

    def _save_plan_decision(self, day, allocator=None, pins=None, allocs=None):
        """写当天的资源决定；``allocator=None`` = 清掉这一天的决定。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        data = self._plan_decisions()
        if allocator is None:
            data.pop(iso, None)
        else:
            counts = {str(a.get("project_id")): int(a.get("count") or 0)
                      for a in (allocs or ())}
            alloc = {}
            for project in (getattr(allocator, "projects", None) or ()):
                alloc[str(project.get("id"))] = counts.get(str(project.get("id")), 0)
            windows = [{"project_id": a.get("project_id"),
                        "from": a.get("from"),
                        "period_id": a.get("period_id"),
                        "count": int(a.get("count") or 0)}
                       for a in (allocs or ())
                       if a.get("from") or a.get("period_id")]
            data[iso] = {"alloc": alloc,
                         "pins": [dict(p) for p in (pins or ())],
                         "windows": windows,
                         "at": ts._now()}
        try:
            tmp = PLAN_DECISIONS_FILE + ".tmp"
            os.makedirs(os.path.dirname(PLAN_DECISIONS_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, PLAN_DECISIONS_FILE)
        except OSError as e:                       # noqa: BLE001 - 台账坏了不挡排期
            rl.log_event("plan", "decision_save_failed", level="warning",
                         day=iso, error=str(e))

    def _prefill_plan_allocator(self, allocator, day):
        """把上一轮的决定填回面板（项目「变亮」、固定项还在）。"""
        rec = self._plan_decision(day)
        if not rec:
            return False
        allocator.had_decision = True
        by_id = {str(p.get("id")): p for p in (allocator.projects or ())}
        windows = {str(w.get("project_id")): dict(w)
                   for w in (rec.get("windows") or ())
                   if isinstance(w, dict)}
        stale = []
        for pid, count in (rec.get("alloc") or {}).items():
            project = by_id.get(str(pid))
            if project is None or int(count or 0) <= 0:
                continue
            try:
                stat = allocator._project_stat(project)
            except Exception:                      # noqa: BLE001 - 预填失败就按没活处理
                stat = {}
            if not project.get("simple") and int(stat.get("remaining") or 0) <= 0:
                # 上一轮的「说好 N count」这次已经落不了地（任务被删/改了标签）：
                # 不能让它以「已分配」的样子回到面板上，否则一敲 complete 又把
                # 整天的排布清空一遍。
                stale.append((project, int(count)))
                continue
            item = {"project_id": project["id"], "count": int(count)}
            win = windows.get(str(project["id"])) or {}
            if win.get("from"):
                item["from"] = win["from"]
            if win.get("period_id"):
                item["period_id"] = win["period_id"]
            allocator.allocations.append(item)
        if stale:
            allocator.stale_dropped = True
        for project, count in stale:
            self.say("⚠ 上一轮给「%s」分的 %d count 这次作废：%s。"
                     % (project.get("title") or project.get("id"), count,
                        pj.empty_reason(project)), "warn")
        keys = ("task_id", "pk", "title", "count", "project_id",
                "project_title", "priority", "minutes")
        for pin in rec.get("pins") or ():
            try:
                task = self.task_store.get(pin.get("task_id"))
            except (ValueError, TypeError):
                task = None
            if not task or not ts.TaskStore.is_open(task) or task.get("formatter"):
                continue
            item = {k: pin.get(k) for k in keys if pin.get(k) is not None}
            allocator.pins.append(item)
        rl.log_event("plan", "prefilled", level="debug",
                     day=day.isoformat(), allocs=len(allocator.allocations),
                     pins=len(allocator.pins))
        return True

    def _reset_day_plans(self, day, announce=True, keep_decision=True):
        """重新 plan 前撤掉当天旧的 project plan / task 绑定。

        普通 ``plan`` 是「这一天整体重排」：旧的 plan items 和它们挂着的
        session 必须先下掉，否则第二次 plan 只看得见剩下的空场，旧内容会和
        新排法并存。``plan -s`` / ``project -a`` 明确是追加，不走这里。

        只动**仍然 open** 的旧计划任务；已经完成/放弃的任务保留历史绑定。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        cleared_plans = cleared_items = detached = 0
        try:
            # op + 简单 pro：简单项目的 plans 记在 pro 自己身上，只看 op 会让
            # 上一轮的场次永远不释放。
            projects = self.projects.plan_refs()
        except Exception:                          # noqa: BLE001
            return {"plans": 0, "items": 0, "detached": 0}
        for project in projects or ():
            plan = pj.plan_of(project, day)
            if not plan:
                continue
            items = list(plan.get("items") or ())
            for item in items:
                try:
                    task = self.task_store.get(item.get("task_id"))
                except (ValueError, TypeError):
                    continue
                if task.get("formatter") or not ts.TaskStore.is_open(task):
                    continue
                sess = task.get("session") or {}
                if (str(sess.get("date") or "") != iso
                        or str(sess.get("start") or "")
                        != str(item.get("start") or "")):
                    continue
                # 已经开始/正在进行/过去的 session 都是硬边界：重排只动未来。
                if self._session_already_started(sess, now=self._now_dt()):
                    continue
                try:
                    self.task_store.unassign(task["id"], reason="plan-replaced")
                    detached += 1
                except ValueError:
                    pass
            if keep_decision:
                # 决定（count）保留：重新打开 plan 要预填、complete 要复现。
                # 这里只清「这一天排了哪些条目」。
                self.projects.clear_items(project.get("id"), day)
            else:
                self.projects.set_allocation(project.get("id"), day, 0)
            cleared_plans += 1
            cleared_items += len(items)
        # 任务级固定项（`task <任务> <count>`）不记在项目库里：它们可能压根不属于
        # 任何 project。单独一份台账，重排时同样要解绑，否则那些任务永远占着场次。
        pinned = self._plan_pins(day)
        for item in pinned:
            try:
                task = self.task_store.get(item.get("task_id"))
            except (ValueError, TypeError):
                continue
            if task is None or task.get("formatter") or not ts.TaskStore.is_open(task):
                continue
            sess = task.get("session") or {}
            if (str(sess.get("date") or "") != iso
                    or str(sess.get("start") or "") != str(item.get("start") or "")):
                continue
            if self._session_already_started(sess, now=self._now_dt()):
                continue
            try:
                self.task_store.unassign(task["id"], reason="plan-replaced")
                detached += 1
            except ValueError:
                pass
        if pinned and not keep_decision:
            self._save_plan_pins(day, ())
        if pinned:
            cleared_items += len(pinned)
        # 重排前顺手清掉「上一轮 plan 留下的同 pk 派生幽灵」：用户 2026-11-18 要求
        # 「重复任务产生的、被取消 plan 的任务应该删除」，而且它们会一直挂在面板上。
        pruned = self._prune_duplicate_mirrors(day)
        if (cleared_plans or pinned) and announce:
            self.say("♻️ 重排 %s：先撤掉旧 plan 的 %d 条任务，再从当前空场重新排%s"
                     % (iso, cleared_items,
                        ("（另清掉 %d 条重复的派生任务）" % len(pruned)) if pruned else ""),
                     "info")
        return {"plans": cleared_plans, "items": cleared_items,
                "detached": detached, "pins": len(pinned), "pruned": len(pruned)}

    # -- 排期流水线的「阶段 1：period 层」 -------------------------------
    def available_periods(self, day):
        """这一天的时段层：``{period_id: {"period", "auto", "has_layout"}}``。

        **这是排期的第一等输入**（用户要的「首先确定 period 时间，之后统一预约」）。
        ``auto=True`` 表示这一段交给任务排期自己决定 session 长度（没有 layout，
        或者 layout 是上一轮自动排出来的）；``auto=False`` 是手动硬约束，任务
        只能往里填，排期不许改它的切分。
        """
        try:
            layouts = sp.load_layouts()
        except Exception:                          # noqa: BLE001
            layouts = {}
        meta = {}
        for period in self.periods_for_day(day):
            pid = str(period.get("id") or "")
            if not pid:
                continue
            rec = sp.get_layout_record(layouts, day, pid)
            meta[pid] = {
                "period": dict(period),
                # 没有 layout = 交给任务排期自动生成；有 layout 但 source 是
                # auto / plan（plan 自己切出来的，见 commit_layout 的 source="plan"）
                # 也允许下一次 plan 重算；manual 才是硬约束。
                "auto": rec is None or sp.is_auto_source(rec.get("source")),
                "has_layout": rec is not None,
            }
        return meta

    def _plan_notes(self, stats, slots):
        """把 ``_row_slots`` / ``_auto_period_slots`` 攒下的统计说成人话。"""
        notes = []
        if stats.get("past"):
            notes.append("今天已经过去的 %d 场不算空闲。" % stats["past"])
        if stats.get("busy"):
            notes.append("%d 场已经挂着任务了，不算空闲。" % stats["busy"])
        if stats.get("reserved"):
            notes.append("%d 场已经被 formatter 占掉，项目 plan 不会碰。"
                         % stats["reserved"])
        if stats.get("unbooked"):
            notes.append("%s 还没订上 Focusmate，但仍计入空场（plan 排进去后，"
                         "等订上会自动挂任务）。" % "、".join(stats["unbooked"]))
        if stats.get("live_included"):
            notes.append("当前进行中的 session #%s 已纳入：%s；只算剩下的完整 "
                         "%d 分钟块。" % (" / #".join(
                             str(s["index"]) for s in slots if s.get("current")),
                             "、".join(stats["live_included"]),
                             pp.MINUTES_PER_COUNT))
        if stats.get("live_short"):
            notes.append("当前进行中的 %s 剩余不足 1 count，没纳入。"
                         % "、".join(stats["live_short"]))
        if stats.get("live_busy"):
            notes.append("当前进行中的 %s 已经挂着任务，没纳入。"
                         % "、".join(stats["live_busy"]))
        if stats.get("live_unplanned"):
            notes.append("%s 是计划外的真实 session，但正在进行的空场也按 -c "
                         "纳入；不会改 data.csv。" % "、".join(stats["live_unplanned"]))
        return notes

    def _row_slots(self, day, period_meta, *, include_current=False,
                   replan=False, now=None):
        """阶段 2：data.csv / API 里**真实存在**的场次 → 可排的 slot。

        返回 ``(slots, blocked_periods, stats)``。任何一条被排除的场次都会把它
        所属的 period 记进 ``blocked_periods``：那一段不再算「整段空着」，
        自动排布不会去重切它。
        """
        now = now or self._now_dt()
        anchor = self._plan_anchor(now)
        is_today = (day == now.date())
        plan_owned = (self._plan_owned_task_ids(day, rescindable_only=True)
                      if replan else set())
        slots, blocked_periods = [], set()
        recut_extras = self._recuttable_extra_starts(day, replan=replan)
        superseded_rows = self._superseded_row_starts(day)
        stats = {"past": 0, "busy": 0, "reserved": 0, "unbooked": [],
                 "live_included": [], "live_short": [], "live_busy": [],
                 "live_unplanned": []}
        try:
            reservations = th.collect_reservations(
                self.task_store.list(), day.isoformat())
        except Exception:                          # noqa: BLE001 - 坏任务不该挡 plan
            reservations = []
        for index, row in enumerate(self.rows, 1):
            is_planned = bool(row.get("planned"))
            # 场次的归属看**开始时间**：今天 23:15 开始、明天 00:05 结束
            # 仍然是今天的一场。以前这里还要求 row["date"] 完全相等，
            # 一旦行来自 API、日期字段被写成了终点日，跨午夜场次就整场漏算。
            start_dt = self._row_start_datetime(row, fallback_day=day)
            if start_dt is None or start_dt.date() != day:
                continue
            if row.get("period_only"):
                # 时段的占位行（没切出 session）不是资源：既不算空场，也不把
                # 这一段标成 blocked —— 整段容量交给 _auto_period_slots 决定。
                continue
            if str(row.get("startLabel") or "") in recut_extras:
                # plan 上一轮自己切的窗口（extras 里的 50 / 25）也不是固定资产：
                # 这一轮要能把那截还原成整块再重新切，否则一次 50+25 的切分就
                # 永久钉死这个时段（2026-11-26「English review 被拆成三块」）。
                continue
            if str(row.get("startLabel") or "") in superseded_rows:
                # API 里还没换掉的旧场次（同一场改了时长 / 被并进前一场的碎片）：
                # 它是「正在被取消」的东西，不是资源，也不是内容。
                continue
            pid_of_row = str(row.get("period_id") or "")
            live = False
            end_dt = None
            if is_today and start_dt <= now:
                end_dt = self._row_end_datetime(row, start=start_dt, fallback_day=day)
                if include_current and end_dt is not None and now < end_dt:
                    live = True
                else:
                    if is_planned:
                        stats["past"] += 1
                    if pid_of_row:
                        blocked_periods.add(pid_of_row)
                    continue
            elif is_today and start_dt < anchor:
                # 已经来不及开始的那一小段（现在 → 下一个十五分钟整点）不是资源：
                # 15:58 敲 plan，15:59 / 16:00 之前的场次都不该被排东西进去。
                if is_planned:
                    stats["past"] += 1
                if pid_of_row:
                    blocked_periods.add(pid_of_row)
                continue
            # 普通 plan 仍只认 data.csv 里排好的场次；-c 的唯一例外是
            # **此刻正在进行**的真实 session（计划外但已经开打，且没挂任务）。
            if not is_planned and not live:
                continue
            session_tasks = self.task_store.tasks_for_session(
                start_dt.date().isoformat(), row.get("startLabel"))
            if replan and session_tasks:
                session_tasks = [task for task in session_tasks
                                 if int(task.get("id") or 0) not in plan_owned]
            if session_tasks:
                stats["busy"] += 1
                if live:
                    stats["live_busy"].append(row.get("startLabel") or "?")
                if pid_of_row:
                    blocked_periods.add(pid_of_row)
                continue
            # formatter 已经占掉的时间是**硬约束**：项目 plan 只能看剩下的空场，
            # 绝不能把这条场次再分给项目、更不能回头改 formatter 的切割。
            start_min = th._clock_minutes(row.get("startLabel"))
            end_min = th._clock_minutes(row.get("endLabel"))
            if start_min is None:
                continue
            if end_min is None:
                end_min = start_min + int(row.get("durationMin") or 0)
            if not th.is_free({"start_min": start_min, "end_min": end_min},
                              reservations):
                stats["reserved"] += 1
                if pid_of_row:
                    blocked_periods.add(pid_of_row)
                continue
            duration = int(row.get("durationMin") or 0)
            slot_extra = {}
            if live:
                remaining_sec = max(0, int((end_dt - now).total_seconds()))
                remaining_min = ((remaining_sec + 59) // 60
                                 if remaining_sec else 0)
                # 只排完整的 25 分钟块。余下的零头留给收尾/休息，不把它包装成
                # 一个「这一场还能干 2 分钟」的假 count。
                counts = min(duration // pp.MINUTES_PER_COUNT,
                             remaining_min // pp.MINUTES_PER_COUNT)
                if counts <= 0:
                    stats["live_short"].append("%s–%s（剩余约 %d 分钟）"
                                               % (row.get("startLabel") or "?",
                                                  row.get("endLabel") or "?",
                                                  remaining_min))
                    if pid_of_row:
                        blocked_periods.add(pid_of_row)
                    continue
                duration = counts * pp.MINUTES_PER_COUNT
                slot_extra = {"current": True,
                              "remaining_minutes": remaining_min}
                stats["live_included"].append(
                    "%s–%s（剩余约 %d 分钟 → %d count）"
                    % (row.get("startLabel") or "?", row.get("endLabel") or "?",
                       remaining_min, counts))
                if not is_planned:
                    stats["live_unplanned"].append(row.get("startLabel") or "?")
            if row.get("id"):
                # 用户 2026-11-17：「我希望我手动约的 session 和由于 plan 来的
                # session 不同，后者可以被 plan 重新安排。」
                # 所以「已订上」本身不再冻结这一段 —— 冻结只看 layout 的来源：
                #   * manual（用户 `session`/`period` 切的、formatter 切的）
                #     → 硬约束，只许往里塞任务；
                #   * auto / plan（上一轮 plan 自己切的）→ 允许重切，重切时
                #     commit_layout 负责取消旧场，末尾 book() 补订新场。
                meta_auto = bool((period_meta.get(pid_of_row) or {}).get("auto"))
                if pid_of_row and not meta_auto:
                    blocked_periods.add(pid_of_row)
            slot = pp.slot_record(index, start_dt.date().isoformat(),
                                  row.get("startLabel"), duration,
                                  row.get("id"), row.get("endLabel"))
            slot.update(slot_extra)
            meta = period_meta.get(pid_of_row) or {}
            period = meta.get("period") or row.get("period") or {}
            slot["period_id"] = pid_of_row
            slot["period"] = dict(period) if period else None
            slot["period_start"] = period.get("start")
            slot["period_end"] = period.get("end")
            slot["period_total_minutes"] = pp._period_total_minutes(period) if period else 0
            slot["period_blocks"] = pp._period_blocks(period) if period else 0
            slot["period_auto"] = bool(meta.get("auto")) and pid_of_row not in blocked_periods
            slots.append(slot)
            if row.get("id") is None:
                stats["unbooked"].append(row.get("startLabel") or "?")
        return slots, blocked_periods, stats

    def _period_session_rows(self, day, period_id):
        """这个时段里**真实存在**的 session 行 → ``[{"start","duration"}]``。

        给 :func:`session_plan.tail_gap` 用：它要拿完整的场次序列去验「前缀能不
        能原样接龙」。占位行（``period_only``）不是 session，不算。
        """
        out = []
        superseded = self._superseded_row_starts(day)
        for row in self.rows or ():
            if row.get("period_only"):
                continue
            if str(row.get("startLabel") or "") in superseded:
                continue                           # 旧切分的碎片，不算这一段的接龙
            if str(row.get("period_id") or "") != str(period_id):
                continue
            start_dt = self._row_start_datetime(row, fallback_day=day)
            if start_dt is None or start_dt.date() != day:
                continue
            out.append({"start": row.get("startLabel"),
                        "duration": row.get("durationMin")})
        return out

    def _started_period_slot(self, day, period, anchor):
        """已经开始了、但整段还没切过 session 的时段 → anchor 之后的可用窗口 slot。

        「只排未来」：已经流走的分钟不是资源；但 13:00 打开 TI、11:00–15:00 还没
        切的时候，13:15–15:00 仍然是空的。窗口起点不在时段起点，layout 表达不了，
        所以 ``window=True``（落盘由 :meth:`_commit_window_sessions` 写成 extras）。
        手订场/进行中场由 :meth:`available_windows` 先扣掉。
        """
        pid = str(period.get("id") or "")
        if not pid:
            return None
        if anchor.date() != day:
            return None                     # anchor 已经跨到明天，今天没得排
        anchor_min = anchor.hour * 60 + anchor.minute
        try:
            windows = [w for w in self.available_windows(day)
                       if str(w.get("period_id") or "") == pid]
        except Exception:                   # noqa: BLE001 - 窗口算不出来退回整段
            windows = []
        if not windows:
            got = th._range_of(period)
            if got is None:
                return None
            windows = [{"start_min": int(got[0]), "end_min": int(got[1])}]
        total_blocks = pp._period_blocks(period)
        for win in sorted(windows, key=lambda w: int(w.get("start_min") or 0)):
            start_min = max(int(win.get("start_min") or 0), anchor_min)
            end_min = int(win.get("end_min") or 0)
            minutes = end_min - start_min
            if minutes < pp.MINUTES_PER_COUNT:
                continue
            blocks = min(minutes // 30, total_blocks)
            if blocks <= 0:
                continue
            start = th.fmt_minutes(start_min)
            end = th.fmt_minutes(end_min)
            trimmed = dict(period, start=start, end=end)
            return {
                "index": 0,
                "date": day.isoformat(),
                "start": start,
                "end": end,
                "duration": blocks * pp.MINUTES_PER_COUNT,
                "counts": blocks,
                "id": None,
                "period_id": pid,
                "period": trimmed,
                "period_start": start,
                "period_end": end,
                "period_total_minutes": pp._period_total_minutes(trimmed),
                "period_blocks": blocks,
                "period_auto": True,
                "windowed": True,
                "window": True,
                "window_end": end,
                "current": False,
            }
        return None

    def _auto_period_slots(self, day, period_meta, blocked_periods, slots, *,
                           include_current=False, now=None, replan=False):
        """阶段 3：自动排布的 period 整段换成「工作容量」伪 slot。

        只要整段都空着，就用整段工作容量参与 plan，而不是沿用 data.csv 里上一轮
        排出来的某几个 session —— 这样重新 plan 时任务可以重新决定这一段里的
        session 长度；真正的切分在 :meth:`commit_layout` 里按任务重算并写回
        data.csv。

        返回 ``(slots, blocked_periods)``（后者只因为「被占/已过去」变大；
        静音不参与容量，见 2026-11-26 用户口径）。
        """
        now = now or self._now_dt()
        anchor = self._plan_anchor(now)
        # plan 自己上一轮切的窗口不算洞（同 _recuttable_extra_starts）：
        # 那截时间要能回到容量里重新切。
        recut_extras = self._recuttable_extra_starts(day, replan=replan)
        auto_period_ids = {pid for pid, meta in period_meta.items() if meta["auto"]}
        original_by_pid = {}
        for slot in slots:
            original_by_pid.setdefault(str(slot.get("period_id") or ""), []).append(slot)
        auto_replace = set(auto_period_ids) - blocked_periods
        if auto_replace:
            slots[:] = [s for s in slots
                        if str(s.get("period_id") or "") not in auto_replace]
        for pid in sorted(auto_replace):
            meta = period_meta.get(pid) or {}
            period = meta.get("period") or {}
            # 这一段没有任何 session 行（今天 19:00–21:30 那种：还没切，或者上一轮
            # plan 把它分到 0 count）**照样算容量** —— period 是排期的第一等输入，
            # 不能因为 data.csv 里没有它的行就当作日历里不存在这段。切分交给
            # commit_layout 按任务重算。
            s_got = sp.parse_hhmm(period.get("start"))
            e_got = sp.parse_hhmm(period.get("end"))
            if s_got is None or e_got is None:
                slots.extend(original_by_pid.get(pid) or [])
                continue
            p_start = datetime.datetime.combine(day, datetime.time(*s_got))
            p_end = datetime.datetime.combine(day, datetime.time(*e_got))
            if p_end <= p_start:
                p_end += datetime.timedelta(days=1)
            if p_start < anchor and not (include_current and now < p_end):
                # 这一段已经开始了。已经切出过真实 session 的时段保持原样（由行层
                # 和尾巴逻辑处理）；**整段还没切过**的时段只把 anchor 之后的剩余
                # 时间算成容量 —— 「先分时间、再落 session」，已经流走的分钟不是
                # 资源，但下午 13:00 打开 TI、11:00–15:00 还没切时，13:15–15:00
                # 仍然应该能排。窗口起点不在时段起点，layout 表达不了，标
                # ``window=True``，_plan_apply 会写成 extras。
                if original_by_pid.get(pid):
                    slots.extend(original_by_pid.get(pid))
                    continue
                cut = self._started_period_slot(day, period, anchor)
                if cut is not None:
                    cut["index"] = len(slots) + 1
                    slots.append(cut)
                continue
            try:
                reservations = th.collect_reservations(
                    self.task_store.list(), day.isoformat())
            except Exception:                      # noqa: BLE001
                reservations = []
            s_min = s_got[0] * 60 + s_got[1]
            e_min = e_got[0] * 60 + e_got[1]
            if e_min <= s_min:
                e_min += 24 * 60
            if not th.is_free({"start_min": s_min, "end_min": e_min}, reservations):
                slots.extend(original_by_pid.get(pid) or [])
                continue
            # 2026-11-21：手订场是硬洞。可用时间 = period 减洞，拆成若干窗口：
            #   * 前缀窗口（从时段起点开始）能用 layout 表达 → 普通 auto slot；
            #   * 洞后面的窗口 layout 表达不了起点 → 标 window=True，落盘时写成
            #     extras（_plan_apply → _commit_window_sessions）。
            blocks = pp._period_blocks(period)
            try:
                mine = [w for w in self.available_windows(day)
                        if str(w.get("period_id") or "") == str(pid)]
            except Exception:                      # noqa: BLE001 - 窗口算不出来就照旧
                mine = []
            if mine and not (len(mine) == 1 and mine[0].get("full")):
                prefix = next((w for w in mine
                               if int(w.get("start_min") or 0) == s_min), None)
                pieces = []
                if prefix is not None:
                    p_blocks = min(int(prefix.get("minutes") or 0) // 30,
                                   pp._period_blocks(period))
                    if p_blocks > 0:
                        prefix_end = prefix.get("end") or period.get("end")
                        pieces.append({
                            "start": period.get("start"), "end": prefix_end,
                            "counts": p_blocks,
                            "period": dict(period, end=prefix_end),
                            "window": False,
                        })
                for win in mine:
                    if win is prefix:
                        continue
                    w_blocks = min(int(win.get("minutes") or 0) // 30,
                                   pp._period_blocks(period))
                    if w_blocks <= 0:
                        continue
                    pieces.append({
                        "start": win.get("start"), "end": win.get("end"),
                        "counts": w_blocks,
                        "period": dict(period, start=win.get("start"),
                                       end=win.get("end")),
                        "window": True,
                    })
                if not pieces:
                    rl.log_event("plan", "window_skipped", level="debug",
                                 day=day.isoformat(), period=pid,
                                 reason="manual-hole", windows=len(mine))
                    slots.extend(original_by_pid.get(pid) or [])
                    continue
                for piece in pieces:
                    counts = int(piece["counts"])
                    if include_current and p_start <= now < p_end:
                        piece_end = datetime.datetime.combine(
                            day, datetime.time(*sp.parse_hhmm(piece["end"])))
                        remaining = int(
                            (piece_end - max(now, anchor)).total_seconds() // 60)
                        counts = min(counts, remaining // pp.MINUTES_PER_COUNT)
                    if counts <= 0:
                        continue
                    slots.append({
                        "index": len(slots) + 1,
                        "date": day.isoformat(),
                        "start": piece["start"],
                        "end": piece["end"],
                        "duration": counts * pp.MINUTES_PER_COUNT,
                        "counts": counts,
                        "id": None,
                        "period_id": pid,
                        "period": piece["period"],
                        "period_start": piece["period"].get("start"),
                        "period_end": piece["period"].get("end"),
                        "period_total_minutes": pp._period_total_minutes(
                            piece["period"]),
                        "period_blocks": counts,
                        "period_auto": True,
                        "windowed": True,
                        "window": piece["window"],
                        "window_end": piece["end"],
                        "current": bool(include_current and p_start <= now < p_end),
                    })
                rl.log_event("plan", "window_split", level="debug",
                             day=day.isoformat(), period=pid,
                             windows=[(p["start"], p["end"], p["counts"])
                                      for p in pieces])
                continue
            if include_current and p_start <= now < p_end:
                remaining = int((p_end - max(now, anchor)).total_seconds() // 60)
                blocks = min(blocks, remaining // pp.MINUTES_PER_COUNT)
            if blocks <= 0:
                slots.extend(original_by_pid.get(pid) or [])
                continue
            slots.append({
                "index": len(slots) + 1,
                "date": day.isoformat(),
                "start": period.get("start"),
                "end": period.get("end"),
                "duration": blocks * pp.MINUTES_PER_COUNT,
                "counts": blocks,
                "id": None,
                "period_id": pid,
                "period": dict(period),
                "period_start": period.get("start"),
                "period_end": period.get("end"),
                "period_total_minutes": pp._period_total_minutes(period),
                "period_blocks": blocks,
                "period_auto": True,
                "windowed": False,
                "window": False,
                "window_end": period.get("end"),
                "current": bool(include_current and p_start <= now < p_end),
            })
        # 阶段 3.5：**已经切了一半的时段**，尾巴那截也得算容量。
        #
        # 以前只要这个时段里有一场被排除（挂了任务 / 已经订上 / 已经过去），
        # 整段就被记进 ``blocked_periods``，容量只剩 data.csv 里那些还没挂任务
        # 的行 —— 19:00(75) 挂了任务、20:15 之后明明还有 75 分钟空着，plan 却
        # 说「今天只剩 2 count」（用户 2026-11-16 报的「超预算」就是这么来的）。
        #
        # 尾巴能安全追加：layout 只存「从时段起点接龙的 duration 列表」，
        # ``prefix + 新时长`` 不会挪动前面任何一场。
        #
        # **洞只吃掉自己那段占用，不让整段作废**（2026-11-26 用户报的
        # 「19:25–21:00 那里去了」）：manual / 已经订满的时段里，layout 链上
        # 的场次原样保留，但 extras（formatter 切的、plan 切的窗口）只是洞 ——
        # 尾巴区间扣掉洞的占用后，剩下的空档照样还给排期，落盘走 extras
        # （layout 表达不了洞后的起点）。以前 ``tail_gap`` 拿**所有**行去验
        # 「序列对得上」，T3 的 19:00(25) + extras 21:00(25) 序列一错就整段
        # 放弃，2.5 小时的时段只剩两场 25 分钟。
        for pid in sorted(period_meta):
            if pid in auto_replace:
                continue
            meta = period_meta.get(pid) or {}
            period = meta.get("period") or {}
            rows = self._period_session_rows(day, pid)
            if not rows:
                # 没有任何真实行：自动时段已经在上面整段算过容量了；手动/已订上
                # 的时段没有行说明 layout 和 data.csv 对不上，别凭空造容量。
                continue
            try:
                extra_recs = list(sp.extras_for_date(sp.load_extras(), day))
            except Exception:                    # noqa: BLE001 - 坏表按没有 extras
                extra_recs = []
            extra_starts = {str(rec.get("start")) for rec in extra_recs}
            chain = [row for row in rows
                     if str(row.get("start")) not in extra_starts]
            gap = sp.tail_gap(period, chain)
            if gap is None:
                continue
            try:
                reservations = th.collect_reservations(
                    self.task_store.list(), day.isoformat())
            except Exception:                    # noqa: BLE001
                reservations = []
            e_got = sp.parse_hhmm(period.get("end"))
            g_got = sp.parse_hhmm(gap["start"])
            if e_got is None or g_got is None:
                continue
            g_min = g_got[0] * 60 + g_got[1]
            e_min = e_got[0] * 60 + e_got[1]
            if e_min <= g_min:
                e_min += 24 * 60
            if not th.is_free({"start_min": g_min, "end_min": e_min}, reservations):
                continue
            # 尾巴区间里扣掉 extras 的占用（按 session_span 算，含强制休息）。
            # 用 extras 账本本身，而不是只看 data.csv 里的行：extras 刚写进去、
            # csv 还没重算的那一小段时间里，两边会不一致 —— 洞漏掉就会排上去压住。
            pieces = [(g_min, e_min)]
            for rec in extra_recs:
                if str(rec.get("start") or "") in recut_extras:
                    continue                       # 本轮可重切，不占洞
                s_got = sp.parse_hhmm(rec.get("start"))
                if s_got is None:
                    continue
                try:
                    span = sp.session_span(int(rec.get("duration") or 0))
                except (TypeError, ValueError):
                    continue
                if span <= 0:
                    continue
                h_start = s_got[0] * 60 + s_got[1]
                if h_start < g_min:
                    h_start += 24 * 60
                pieces = self._subtract_minute_window(
                    pieces, (h_start, h_start + span))
            is_today = (day == now.date())
            anchor_min = anchor.hour * 60 + anchor.minute
            for p_start, p_end in pieces:
                s_min = max(p_start, anchor_min) if is_today else p_start
                if p_end <= s_min:
                    # 这一截已经来不及开始（追加会把它排到过去），不算资源。
                    continue
                if s_min == g_min and p_end == e_min:
                    # 整条尾巴都空着 → 仍旧「原样追加」，一个已订场次都不挪。
                    slots.append({
                        "index": len(slots) + 1,
                        "date": day.isoformat(),
                        "start": gap["start"],
                        "end": period.get("end"),
                        "duration": int(gap["work_minutes"]),
                        "counts": int(gap["blocks"]),
                        "id": None,
                        "period_id": pid,
                        "period": dict(period),
                        "period_start": period.get("start"),
                        "period_end": period.get("end"),
                        "period_total_minutes": pp._period_total_minutes(period),
                        # 「整段能放几个块」是 period 层的属性，不是这截缺口的
                        # —— 追加校验拿它比 prefix+新时长，写成缺口块数会误判。
                        "period_blocks": pp._period_blocks(period),
                        "period_auto": False,
                        # 追加语义：commit 时把 prefix 原样带上，只往尾巴加。
                        "append": True,
                        "append_prefix": list(gap["prefix"]),
                        "window_minutes": int(gap["minutes"]),
                        "current": False,
                    })
                    continue
                blocks = min((p_end - s_min) // 30, pp._period_blocks(period))
                if blocks <= 0:
                    continue
                start_label = th.fmt_minutes(s_min)
                end_label = th.fmt_minutes(p_end)
                slots.append({
                    "index": len(slots) + 1,
                    "date": day.isoformat(),
                    "start": start_label,
                    "end": end_label,
                    "duration": blocks * pp.MINUTES_PER_COUNT,
                    "counts": blocks,
                    "id": None,
                    "period_id": pid,
                    # 窗口自己的 period 视图：起点是洞的边界，layout 表达不了
                    # → ``window=True``，落盘走 extras（_commit_window_sessions）。
                    "period": dict(period, start=start_label, end=end_label),
                    "period_start": start_label,
                    "period_end": end_label,
                    "period_total_minutes": p_end - s_min,
                    "period_blocks": blocks,
                    "period_auto": True,
                    "windowed": True,
                    "window": True,
                    "window_end": end_label,
                    "current": False,
                })
                rl.log_event("plan", "window_split", level="debug",
                             day=day.isoformat(), period=pid,
                             start=start_label, end=end_label, blocks=blocks,
                             reason="manual-hole")
        for slot in slots:
            # 洞后的窗口本来就要按「自己那截」重切，别被所在时段的 blocked
            # 标记按回去（那会让它走固定 group 分支，挂到不存在的行上）。
            if slot.get("window"):
                continue
            if str(slot.get("period_id") or "") in blocked_periods:
                slot["period_auto"] = False
        return slots, blocked_periods

    def _plan_slots(self, day, include_current=False, replan=None):
        """这一天的**可用空场** → ``(slots, notes)``。

        三层各管一件事，别再混在一个循环里：

        1. :meth:`available_periods` —— **period 层**：这一天有哪些时段、哪些是
           自动排布（排期可以自己决定 session 长度）。
        2. :meth:`_row_slots` —— **行层**：data.csv / API 里真实存在的场次，
           按「过去了 / 挂任务了 / 被 formatter 占了」筛掉，并记下哪些时段
           因为被排除而不能再整段重排。
        3. :meth:`_auto_period_slots` —— **容量层**：把「整段空着」的自动时段
           换成一个「整段工作容量」的伪 slot，让 plan 重新决定切分。

        只排除三类：已经过去的（今天就按「现在」筛）、已经挂任务的、
        被 formatter 占掉的。**没订上 Focusmate 的场次照常计入**（notes 里点名），
        任务先本地挂到那场，等 daemon 订上之后 ``refresh --check`` 再补挂。

        ``include_current=True`` 时，今天**已经开始但还没结束、且没挂任务**的那一场
        也纳入范围。此时只算剩下的完整 25 分钟块：例如 10:00–10:50 的场次在
        10:20 敲 ``plan -c``，容量是 1 count（25 分钟），不会按完整的 50 分钟骗人。

        ``replan=True`` 给普通 ``plan`` 用：上一轮 plan 还没完成的本地任务不算
        「占用」，因为这些旧计划会在 complete 时整体撤掉重排；用户自己的任务、
        formatter 任务仍照常排除。
        """
        if replan is None:
            replan = bool(getattr(self, "_plan_replanning", False))
        now = self._now_dt()
        period_meta = self.available_periods(day)
        slots, blocked, stats = self._row_slots(
            day, period_meta, include_current=include_current,
            replan=replan, now=now)
        slots, blocked = self._auto_period_slots(
            day, period_meta, blocked, slots,
            include_current=include_current, now=now, replan=replan)
        # 面板上的「空场 22:15(50) 20:30(50)」也要按时间顺序 —— 尾巴缺口是
        # 后补的，不排一下会跑到后面那段「整段」的后面。index 是表格行号，
        # 跟着 slot 走，排序不影响挂任务。
        def _slot_start(slot):
            got = sp.parse_hhmm((slot or {}).get("start"))
            return got[0] * 60 + got[1] if got else 0
        slots.sort(key=_slot_start)
        notes = self._plan_notes(stats, slots)
        tails = []
        for slot in slots:
            if not slot.get("append"):
                continue
            tails.append("%s %s 起还剩 %d 分钟（%d count）"
                         % (slot.get("period_id") or "?",
                            slot.get("start") or "?",
                            int(slot.get("window_minutes") or 0),
                            int(slot.get("counts") or 0)))
        if tails:
            notes.append("半截时段只追加、不重切：%s。" % "、".join(tails))
        return slots, notes

    def cmd_plan(self, args=None):
        """``plan`` —— 把这一天剩下的空闲切成 count，分给项目，再自动排任务。

        用户要的整条链路：先看元数据（今天还剩多少空闲时间 / 几个 count），再逐个
        项目问「分多少」，敲 ``complete`` 之后按每个项目分到的 count 唤起任务、排进
        session（尽量一场一个任务、尽量不拆开）。

            plan                    今天（**默认就是今天**，和当前看的是哪天无关）
            plan -c / plan --current
                                    今天，并把**正在进行的空 session**也算进范围
            plan -s <session> -p <project>
                                    当前看板那天：把指定项目排进指定 session
                                    （session 容量就是这次分配的 count）
            plan +1                 明天（plan 1 / plan 明天 / plan 2026-11-07 同义）
            plan -c 今天            同 plan -c（--current 只能用于今天）
            pro 1 4                 项目 #1 分 4 个 count（= 100 分钟）
            pro 1 2                 再敲一次是**覆盖**（变成 2，不是 6）
            pro 1 clear             清空 #1
            task 5 2                **任意一条任务**固定 2 个 count：当作「只有一个
                                    任务的独立项目」排（序号按当前界面：契约 = 任务
                                    面板序号，数据 = 任务库内部 id；`id:N` 永远认 id）
            task 5 clear            撤掉这条任务的固定项
            task 5 -p 3             改归属：移到项目 #3（写回 OmniFocus，放项目最前）
            task 5 -ps 3            ≡ task 5 --project sop 3（走该 pro 的 SOP op）
            task 5 -pi 3            ≡ task 5 --project incubator 3
            complete                分配完了，开始排任务

        分配**超预算**（加起来超过今天剩下的 count）时整条命令都不执行。

        为什么不跟着 `day` 走：`plan` 是「给哪一天分资源」的**动作**，不是看板。
        晚上正在看明天（`day 1`）时顺手敲 `plan`，想排的多半还是明天，所以有
        `plan +1` 这种明确写法；不给参数就是今天 —— 免得「我以为在排今天、
        其实排了后天」。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        rl.log_event("plan", "start", level="debug",
                     day=self.sel_date.isoformat(),
                     args=" ".join(toks) or "(默认今天)")
        if self.planning is not None:
            raise ValueError("已经有一次 plan 在等你的分配（看下面那块面板）："
                             "`pro <序号> <count>` 分给项目、`task <任务> <count>` 固定单独任务、`complete` 开始排、`q` 放弃。")
        if toks and str(toks[0]).lower() in ("clear", "--clear", "-x", "清空"):
            return self.cmd_plan_clear()
        if (self.pending_import is not None or self.nostart is not None
                or self.pending_interrupt is not None
                or self.pending_soft_interrupt is not None
                or self.review_active()):
            raise ValueError("先把手头那一问答完（收尾 / 到点没开始 / 导入确认 / "
                             "中断 title / 软中断检查）再来 plan。")
        sessional = self._parse_sessional_plan_args(toks)
        if sessional is not None:
            return self._run_sessional_plan(*sessional)
        day, offset, include_current = self._parse_plan_args(toks)
        self.projects.ensure_tagged_pros()
        # 2026-11-21 起 op 不再需要 SOP / incubator 类型：侧别是任务标签，
        # 候选清单 = 每个 active pro 的那一个 op，按 pro 阶段挑一侧的任务。
        plan_ops = self.projects.plan_ops()
        if not plan_ops:
            if not self.projects.active_pros():
                # 没有项目也不一定不能 plan：`task <任务> <count>` 能把任意一条
                # 任务当成「只有一个任务的独立项目」排掉。只有连任务都没有时才
                # 直接收口（否则会把后续命令吞进一张空问答里）。
                has_task = bool(self.task_store.list())
                if not has_task:
                    rl.log_event("plan", "no_projects", level="info",
                                 day=day.isoformat())
                    self.say("· 项目库是空的：先 `task import` 一个 OmniFocus 项目，"
                             "或 `pro create <名称>`，再来 plan。", "info")
                    return None
                self.say("· 项目库是空的：这次 plan 只能 `task <任务> <count>` 固定单独"
                         "任务（项目分配那半张表是空的）。", "info")
            else:
                raise ValueError("当前没有可排期的 OmniFocus 项目：只有关联了真正项目 pro "
                                 "的 op 才会进 plan。先用 `op <序号> pro <N>` 关联项目，"
                                 "或 `pro <序号> merge` 把旧的双 op 合成一个。")
        if offset != self.day_offset:
            # 空场、`-a <编号>` 都按**看板那一天**的表格算，所以先把看板搬过去。
            # 搬过去之后必须**等到**那天的真数据：空场判据要看「订没订上」，
            # 拿别的天的旧行去算会把整场排进空气里（wait=True 就是为了这一条）。
            self.day_offset = offset
            try:
                self.refresh(quiet=True, wait=not self.offline)
            except Exception:                      # noqa: BLE001 - 拉不到就照旧
                pass
            try:
                self.rebuild_rows()
            except Exception:                      # noqa: BLE001 - 快照模式也能 plan
                pass
            self.say("· 看板切到 %s（plan 的就是这一天）" % day.isoformat(), "info")
        elif day != datetime.date.today():
            self.say("· plan 的是 %s（不是今天；不带参数就是今天）" % day.isoformat(),
                     "info")
        try:
            self.rebuild_rows()
        except Exception:                          # noqa: BLE001 - 快照模式也能 plan
            pass
        # 用户 2026-11-17（第二次提）：「在 plan 开始之后，之前安排的任务仍然在
        # 占地方，应该把他们视为没有」。所以普通 plan 一律按**整天重排**算资源：
        # 上一轮 plan 唤起的本地任务不算占用（``replan=True``），它们的场次回到
        # 可用容量里；complete 时 ``_plan_apply(append_to_plan=False)`` 会把旧计划
        # 整体撤掉。用户自己的任务、formatter 的占位、手动 layout 仍然照常排除。
        slots, notes = (self._plan_slots(day, include_current=True, replan=True)
                        if include_current else
                        self._plan_slots(day, replan=True))
        for note in notes:
            self.say("· %s" % note, "info")
        if not slots:
            rl.log_event("plan", "no_slots", level="warning", day=day.isoformat())
            raise ValueError("%s 一场空着的 session 都没有 —— 换个日子，或者先 "
                             "`session <HH:MM> <25|50|75>` 把排布加上。" % day.isoformat())
        cap = pp.capacity(slots)
        rl.log_event("plan", "open", day=day.isoformat(), ops=len(plan_ops),
                     sessions=cap["sessions"], counts=cap["counts"],
                     minutes=cap["minutes"])
        pro_map = self._pro_display_map(plan_ops)
        self.planning = pp.Allocator(
            day, plan_ops, slots, task_store=self.task_store,
            hooks=self.plan_hooks(), pro_map=pro_map,
            resolve_task=self._plan_task_ref)
        # 重新打开 plan：把上一轮的决定填回来（项目变亮、固定项还在）。
        if self._prefill_plan_allocator(self.planning, day):
            self.say("· 已载入 %s 上一轮的计划决定（%d 个项目 / %d 条固定项，"
                     "改完直接 `complete` 就按它复现）"
                     % (day.isoformat(), len(self.planning.allocations),
                        len(self.planning.pins)), "info")
        by_side = {}
        for op in plan_ops:
            kind = op.get("side") or pj.side_for_stage(op.get("stage"))
            by_side[kind] = by_side.get(kind, 0) + 1
        kind_txt = "、".join("%s %d" % (kind, count)
                            for kind, count in sorted(by_side.items()))
        self.say("🗂 %s 的项目资源分配：%d 个项目（%s）、%d 场空 session = %d count 可以分"
                 "（%d 分钟；1 count = %d 分钟）"
                 % (day.isoformat(), len(plan_ops), kind_txt,
                    cap["sessions"], cap["counts"], cap["minutes"],
                    pp.MINUTES_PER_COUNT), "cmd")
        self.say("   `pro <序号> <count>` 给项目分资源（序号 = 面板上的 #）、"
                 "`pro <序号> clear` 清空；`task <任务> <count>` 固定某条任务"
                 "（契约界面用任务面板序号，数据界面用任务库 id）、"
                 "`task <任务> -p <项目序号>` 改归属；`complete` 开始排任务、`q` 放弃。",
                 "info")
        self.invalidate()
        return self.planning

    #: `plan` 的日期参数里认的那几个人话
    PLAN_DAY_WORDS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1,
                      "后天": 2, "大后天": 3, "昨天": -1, "yesterday": -1}

    def _parse_sessional_plan_args(self, toks):
        """``plan -s <session> -p <project>`` → ``(session, project, -c)``。

        只处理 **sessional plan**；没出现 ``-s/--session`` 或 ``-p/--project``
        时返回 None，交给普通 plan。这个模式不需要日期参数：session 序号按
        当前看板那一张契约表算。
        """
        toks = [str(t).strip() for t in (toks or ()) if str(t).strip()]
        has_session = any(t.lower() in ("-s", "--session")
                          or t.lower().startswith("--session=") for t in toks)
        has_project = any(t.lower() in ("-p", "--project")
                          or t.lower().startswith("--project=") for t in toks)
        if not has_session and not has_project:
            return None
        session_idx = project_idx = None
        include_current = False
        rest = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("-s", "--session"):
                if i + 1 >= len(toks):
                    raise ValueError("plan -s/--session 后面要跟 session 序号")
                session_idx = ts.parse_count(toks[i + 1])
                i += 2
                continue
            if low.startswith("--session="):
                session_idx = ts.parse_count(tok.split("=", 1)[1])
                i += 1
                continue
            if low in ("-p", "--project"):
                if i + 1 >= len(toks):
                    raise ValueError("plan -p/--project 后面要跟项目序号")
                project_idx = ts.parse_count(toks[i + 1])
                i += 2
                continue
            if low.startswith("--project="):
                project_idx = ts.parse_count(tok.split("=", 1)[1])
                i += 1
                continue
            if low in ("-c", "--current"):
                include_current = True
                i += 1
                continue
            rest.append(tok)
            i += 1
        if rest:
            raise ValueError("sessional plan 只认 plan -s <session> -p <project>；"
                             "不跟日期或其他参数（多出来：%s）" % " ".join(rest))
        if session_idx is None or session_idx <= 0:
            raise ValueError("plan -s 要一个正整数 session 序号（契约表上的 #）")
        if project_idx is None or project_idx <= 0:
            raise ValueError("plan -p 要一个正整数项目序号（Project 表上的 #）")
        return int(session_idx), int(project_idx), include_current

    def _run_sessional_plan(self, session_idx, project_idx, include_current=False):
        """把某一项目排进指定 session：该 session 的 count 就是本次分配量。"""
        day = self.sel_date
        self.projects.ensure_tagged_pros()
        plan_ops = self.projects.plan_ops()
        if not plan_ops:
            raise ValueError("当前没有可排期的 OmniFocus 项目（op）")
        if project_idx > len(plan_ops):
            raise ValueError("项目序号 %d 超出范围：#1–#%d"
                             % (project_idx, len(plan_ops)))
        project = plan_ops[project_idx - 1]
        self.rebuild_rows()
        slots, notes = (self._plan_slots(day, include_current=True)
                        if include_current else self._plan_slots(day))
        for note in notes:
            self.say("· %s" % note, "info")
        slot = next((dict(s) for s in slots
                     if int(s.get("index") or 0) == int(session_idx)), None)
        if slot is None:
            row = self.rows[session_idx - 1] if 0 < session_idx <= len(self.rows) else {}
            why = ""
            if row:
                if self.task_store.tasks_for_session(
                        str(row.get("date") or day.isoformat()),
                        row.get("startLabel")):
                    why = "：这一场已经挂着任务"
                elif (row.get("endMs")
                      and int(row["endMs"]) <= int(self._now_dt().timestamp() * 1000)):
                    why = "：这一场已经结束"
            raise ValueError("session #%d 不在 %s 的可排空场里%s"
                             % (session_idx, day.isoformat(), why))
        if int(slot.get("counts") or 0) <= 0:
            raise ValueError("session #%d 不足 1 count（25 分钟），不能 plan"
                             % session_idx)
        pro_map = self._pro_display_map(plan_ops)
        allocator = pp.Allocator(
            day, [project], [slot], task_store=self.task_store,
            hooks=self.plan_hooks(), pro_map=pro_map)
        allocs = [{"project_id": project["id"], "project": project,
                   "count": int(slot["counts"])}]
        self.say("📌 sessional plan：session #%d（%s %s，%d count）← 项目 #%d「%s」"
                 "（%s 排序）"
                 % (session_idx, day.isoformat(), slot.get("start"),
                    int(slot["counts"]), project_idx, project.get("title"),
                    pj.task_type_of(project)), "cmd")
        summary = self._plan_apply(allocator, allocs, append_to_plan=True)
        self.invalidate()
        return summary

    def _parse_plan_args(self, toks):
        """``plan`` 的选项 → ``(day, offset, include_current)``。"""
        include_current = False
        date_toks = []
        for tok in toks or []:
            if str(tok).lower() in ("-c", "--current"):
                include_current = True
            else:
                date_toks.append(tok)
        day, offset = self._plan_target(date_toks)
        if include_current and offset != 0:
            raise ValueError("`-c/--current` 只能把**今天正在进行的 session** 纳入 "
                             "plan；不能和明天/其他日期一起用。")
        return day, offset, include_current

    def _plan_target(self, toks):
        """``plan`` 的参数 → ``(day, offset)``。不给参数 = **今天**（用户要求）。"""
        today = datetime.date.today()
        if not toks:
            return today, 0
        if len(toks) > 1:
            raise ValueError("plan 只认一个日期：plan / plan +1 / plan 明天 / plan 2026-11-07")
        tok = toks[0]
        low = tok.lower()
        if low in self.PLAN_DAY_WORDS:
            offset = self.PLAN_DAY_WORDS[low]
        else:
            try:
                offset = int(tok)
            except ValueError:
                try:
                    day = datetime.date.fromisoformat(tok)
                except ValueError:
                    raise ValueError(
                        "plan 的参数要么是偏移量（+1 / 0 / -1）、今天/明天/后天，"
                        "要么是 YYYY-MM-DD；你给的是 %r" % tok) from None
                offset = (day - today).days
        if offset < 0:
            raise ValueError("%s 是过去的日子 —— 那是已经发生的事，不用再 plan"
                             "（`plan` 不带参数就是今天，`plan +1` 是明天）。"
                             % (today + datetime.timedelta(days=offset)).isoformat())
        if offset > 60:
            raise ValueError("最多 plan 到 +60 天（你给的是 %+d）" % offset)
        return today + datetime.timedelta(days=offset), offset

    @staticmethod
    def _row_start_datetime(row, fallback_day=None):
        """一行 session 的本地开始时间。

        ``startMs`` 是权威值；老快照/手工造的行没有它时，退回 ``date`` +
        ``startLabel``。plan 的「哪一天」一律按这个**开始时间**判，不按终点：
        23:15–00:05 这种跨午夜的场次属于 23:15 那天。
        """
        raw_ms = (row or {}).get("startMs")
        try:
            if raw_ms is not None and int(raw_ms) > 946684800000:
                return datetime.datetime.fromtimestamp(int(raw_ms) / 1000.0)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        raw_day = (row or {}).get("date")
        if fallback_day is not None and not raw_day:
            raw_day = fallback_day
        if hasattr(raw_day, "isoformat"):
            raw_day = raw_day.isoformat()
        label = str((row or {}).get("startLabel")
                    or (row or {}).get("start") or "").strip()
        try:
            day = datetime.date.fromisoformat(str(raw_day))
            hour, minute = (int(x) for x in label.split(":")[:2])
            return datetime.datetime.combine(day, datetime.time(hour, minute))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_end_datetime(row, start=None, fallback_day=None):
        """一行 session 的本地结束时间；优先 ``endMs``，再退到开始 + 时长。"""
        raw_ms = (row or {}).get("endMs")
        try:
            if raw_ms is not None and int(raw_ms) > 946684800000:
                return datetime.datetime.fromtimestamp(int(raw_ms) / 1000.0)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        if start is None:
            start = ProjectMixin._row_start_datetime(row, fallback_day=fallback_day)
        if start is None:
            return None
        try:
            duration = int((row or {}).get("durationMin") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 0:
            return start + datetime.timedelta(minutes=duration)
        raw_day = (row or {}).get("date")
        if fallback_day is not None and not raw_day:
            raw_day = fallback_day
        if hasattr(raw_day, "isoformat"):
            raw_day = raw_day.isoformat()
        label = str((row or {}).get("endLabel")
                    or (row or {}).get("end") or "").strip()
        try:
            day = datetime.date.fromisoformat(str(raw_day))
            hour, minute = (int(x) for x in label.split(":")[:2])
            end = datetime.datetime.combine(day, datetime.time(hour, minute))
            if end <= start:
                end += datetime.timedelta(days=1)
            return end
        except (TypeError, ValueError):
            return None

    def free_plan_slots(self, day):
        """那一天「还没开始、没挂任务」的排好的场次（只看 data.csv + 本地绑定）。

        静音不再是排除条件（2026-11-26 用户：「静音时段不应该不排啊，他们静音了
        又没关系」）—— `period mute` 只改 Focusmate 那一场的 quiet mode。

        和 :meth:`_plan_slots` 的区别只有一个：**不问 API**（所以不知道「订上没」）。
        警告板每 5 秒算一次，不能为了提醒去拉一次网络。
        """
        day = day or datetime.date.today()
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        now = self._now_dt()
        out = []
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001 - 下载层坏了不该没帧
            return out
        try:
            reservations = th.collect_reservations(self.task_store.list(), iso)
        except Exception:                          # noqa: BLE001
            reservations = []
        for sess in sessions:
            label = sess.get("start")
            try:
                hour, minute = (int(x) for x in str(label).split(":")[:2])
                start = datetime.datetime.combine(day, datetime.time(hour, minute))
            except (TypeError, ValueError):         # noqa: BLE001 - 坏行跳过
                continue
            # 只按**开始日期**归属这一天；开始后即使终点跨到明天，也已经
            # 不再是一场空 session。
            if start.date() != day or start <= now:
                continue
            if self.task_store.tasks_for_session(iso, label):
                continue
            start_min = th._clock_minutes(label)
            end_min = th._clock_minutes(sess.get("end"))
            if start_min is None:
                continue
            if end_min is None:
                end_min = start_min + int(sess.get("duration") or 0)
            if not th.is_free({"start_min": start_min, "end_min": end_min},
                              reservations):
                continue
            out.append(sess)
        return out

    def pending_plan(self, day=None):
        """今天还没 plan 吗？→ ``{"sessions", "counts", "starts"}`` 或 ``None``。

        三条同时成立才算「该提醒」：项目库里有 active 项目、这一天**一个项目都没分过
        count**、而且这一天还有没开始、没挂任务的场次（静音不算障碍，2026-11-26）。已经 plan 过（哪怕只
        排了一部分）就不再吵 —— 提醒是用来「别忘了」，不是用来记账的。

        容量口径和 ``plan`` 共用 :meth:`_plan_slots`（period-first）：**没有 layout
        的时段、半截时段的尾巴都算空场**。以前这里用 :meth:`free_plan_slots`
        只数「已经切出来的 session」，于是「今天一次 plan 都没做、连 layout 都没有」
        的日子会得到 0 个空场 —— 提醒任务在这种最该出现的日子里反而不建。
        """
        day = day or datetime.date.today()
        try:
            projects = self.projects.plan_ops()
        except Exception:                          # noqa: BLE001 - 项目库坏了别挡住警告板
            return None
        if not projects:
            return None
        allocated = 0
        for project in projects:
            plan = pj.plan_of(project, day)
            allocated += int((plan or {}).get("count") or 0)
        if allocated:
            return None
        try:
            slots, _notes = self._plan_slots(day, replan=True)
        except Exception:                          # noqa: BLE001 - 排期算不出来就当没空场
            return None
        if not slots:
            return None
        counts = sum(int(s.get("counts") or 0) for s in slots)
        if counts <= 0:
            return None
        return {"sessions": len(slots), "counts": counts,
                "starts": [s.get("start") for s in slots]}

    @staticmethod
    def _plan_anchor(now):
        """今天 `plan` 的起点：**下一个 15 分钟整点**。

        用户原话（2026-11-16）：「我希望每次安排的时候就按接下来剩余一天有多少
        时间安排（从下一个十五分钟整点开始）」。已经流走的时间不是资源，
        连「现在这一分钟」也不算 —— 15:58 敲 plan 就只从 16:00 往后数。
        """
        now = now.replace(second=0, microsecond=0)
        return now + datetime.timedelta(minutes=(-now.minute) % 15)

    def cmd_plan_clear(self, day=None):
        """``plan clear`` —— 撤掉这一天的计划（未开始的条目 + 绑定 + 分配 + 决定）。

        ``plan`` 本身保留资源决定（重新打开会预填）；只有这条命令是真清空。
        """
        day = day or self.sel_date
        res = self._reset_day_plans(day, announce=False,
                                    keep_decision=False) or {}
        self._save_plan_decision(day, None)
        try:
            self.rebuild_rows()
        except Exception:                          # noqa: BLE001 - 清完拉不到表也不算失败
            pass
        self.invalidate()
        self.say("🧹 %s 的计划已清空：%d 个项目、%d 条条目、%d 条任务下架"
                 "（已经开始的场次一律不动）。"
                 % (day.isoformat(), int(res.get("plans") or 0),
                    int(res.get("items") or 0), int(res.get("detached") or 0)), "ok")
        return res

    def plan_hooks(self):
        """`plan` 要碰外面世界的那个口子（其余全在 project_plan.Allocator 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            # 重排语义：`plan` 算资源时已经把上一轮 plan 的任务当成没占用
            # （见 cmd_plan 的 replan=True），所以 complete 必须整体替换旧计划，
            # 否则同一条任务会被排两次、旧的场次也腾不出来。
            "apply": lambda allocator, allocs: self._plan_apply(
                allocator, allocs, append_to_plan=False),
        }

    def _plan_task_ref(self, token, day=None):
        """``plan`` 面板里 ``task <序号>`` 的序号解析。

        用户 2026-11-17 定的口径：当前在**契约**界面 → 用任务面板上显示的序号；
        在**数据**界面 → 用任务库里实际的 id（数据表第一列）。``id:N`` 永远按
        内部 id 认 —— 面板上找不到的任务只能这样点名。
        """
        text = str(token or "").strip()
        if text.lower().startswith("id:"):
            return self.task_store.get(text[3:])
        if isinstance(self.view, DataView):
            return self.task_store.get(text)
        return self.resolve_task(text, day=day or self.sel_date)

    def _auto_plan_signature(self, day):
        """自动 layout 的“计划指纹”：只有任务/分配真的变了才重排。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        bits = []
        try:
            projects = self.projects.plan_ops()
        except Exception:                          # noqa: BLE001
            projects = []
        for project in projects:
            plan = pj.plan_of(project, day) or {}
            bits.append(("alloc", str(project.get("id")), int(plan.get("count") or 0)))
        for task in self.task_store.list():
            source = task.get("source") or {}
            if source.get("kind") != "project":
                continue
            sess = task.get("session") or {}
            if str(sess.get("date") or "") != iso:
                continue
            fields = task.get("fields") or {}
            bits.append((
                "task", str(task.get("id")),
                str(fields.get("project_id") or ""),
                str(fields.get("project_task") or ""),
                int(fields.get("duration") or 0),
                int(fields.get("count") or 0),
                str(sess.get("start") or ""),
                int(sess.get("duration") or 0),
                str(ts.TaskStore.status_of(task)),
            ))
        return json.dumps(bits, ensure_ascii=False, sort_keys=True)

    def _stamp_auto_plan_signature(self, day):
        """把当前计划指纹写进这一天所有 auto layout 记录。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        try:
            data = sp.load_layouts()
        except Exception:                          # noqa: BLE001
            return False
        day_recs = (data.get("layouts") or {}).get(iso) or {}
        sig = self._auto_plan_signature(day)
        changed = False
        for rec in day_recs.values():
            if isinstance(rec, dict) and sp.is_auto_source(rec.get("source")):
                rec["plan_sig"] = sig
                changed = True
        if changed:
            sp.save_layouts(data)
        return changed

    def _auto_plan_dirty(self, day):
        """auto layout 有没有因为任务排布变化而需要重算。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        try:
            data = sp.load_layouts()
        except Exception:                          # noqa: BLE001
            return False
        recs = [rec for rec in ((data.get("layouts") or {}).get(iso) or {}).values()
                if isinstance(rec, dict) and sp.is_auto_source(rec.get("source"))]
        if not recs:
            return False
        current = self._auto_plan_signature(day)
        # 老记录没有 plan_sig：先当作基线，不主动重排，避免升级后第一次 refresh
        # 把所有历史计划都掀掉。下一次 plan 会补上指纹。
        return any(rec.get("plan_sig") is not None and rec.get("plan_sig") != current
                   for rec in recs)

    def _replan_auto_day(self, day):
        """计划指纹变了：按当前分配重跑同一套 period-first 排期。"""
        try:
            plan_ops = self.projects.plan_ops()
        except Exception:                          # noqa: BLE001
            return False
        allocs = []
        for project in plan_ops:
            plan = pj.plan_of(project, day) or {}
            count = int(plan.get("count") or 0)
            if count > 0:
                allocs.append({"project_id": project.get("id"),
                               "project": project, "count": count})
        if not allocs:
            return False
        old = getattr(self, "_plan_replanning", False)
        self._plan_replanning = True
        try:
            slots, _notes = self._plan_slots(day)
        finally:
            self._plan_replanning = old
        if not slots:
            return False
        allocator = pp.Allocator(
            day, plan_ops, slots, task_store=self.task_store,
            hooks=self.plan_hooks(), pro_map=self._pro_display_map(plan_ops))
        allocator.allocations = [{"project_id": a["project_id"], "count": a["count"]}
                                 for a in allocs]
        allocator.closed = True
        self._plan_apply(allocator, allocs, append_to_plan=False, book=False,
                         title="按计划重算排布（refresh）",
                         record_decision=False)
        self._stamp_auto_plan_signature(day)
        self.say("♻️ 检测到计划变化：已按 period 重算未来 session，当前正在进行的场次没动。",
                 "info")
        return True

    def _commit_window_sessions(self, day, slot, durations):
        """把手订场之外「窗口」的排布落成 extras（layout 表达不了洞后的起点）。

        幂等：已经存在的同起点同长度 extra 复用；窗口里不再需要的 extra 删掉；
        已订上的旧场次先走 :meth:`_cancel_stale_period_sessions` 取消（手订场由
        它内部的手订保护跳过）。
        """
        period = dict(slot.get("period") or {})
        try:
            sessions = sp.plan_period(period, list(durations or ()))
        except ValueError as e:
            rl.log_event("plan", "window_layout_failed", level="warning",
                         day=day.isoformat(), period=period.get("id"),
                         error=str(e))
            return
        try:
            self._cancel_stale_period_sessions(day, period, sessions,
                                               reason="plan-window")
        except Exception as e:                     # noqa: BLE001 - 取消失败也要写排布
            rl.log_event("plan", "window_cancel_error", level="warning",
                         day=day.isoformat(), period=period.get("id"),
                         error="%s: %s" % (type(e).__name__, e))
        want = {(str(s["start"]), int(s["duration"])) for s in sessions}
        span = th._range_of(period)
        for rec in list(sp.extras_for_date(sp.load_extras(), day)):
            start = str(rec.get("start") or "")
            dur = int(rec.get("duration") or 0)
            if (start, dur) in want:
                continue
            got = th._clock_minutes(start)
            if span is None or got is None or not (span[0] <= got < span[1]):
                continue
            sp.remove_extra(day, start)
        have = {(str(r.get("start")), int(r.get("duration") or 0))
                for r in sp.extras_for_date(sp.load_extras(), day)}
        for sess in sessions:
            key = (str(sess["start"]), int(sess["duration"]))
            if key in have:
                continue
            try:
                sp.add_extra(day, sess["start"], int(sess["duration"]),
                             note="plan 切的（避开手订场）", source="plan")
            except ValueError as e:
                rl.log_event("plan", "window_extra_failed", level="warning",
                             day=day.isoformat(), start=sess["start"],
                             error=str(e))
        try:
            keys = [sch.key_of(day, s["start"]) for s in sessions]
            ks.clear_abandoned(keys)
            ks.clear_slot_status(keys)
        except Exception:                          # noqa: BLE001 - 判决清不掉不挡排期
            pass
        return sessions

    # -- 手动时间段（只给简单项目）------------------------------------

    @staticmethod
    def _window_durations(minutes):
        """把一段分钟数切成 25/50/75 的场次（先整 75，再 50，最后 25）。"""
        out, left = [], int(minutes)
        while left >= pp.MINUTES_PER_COUNT:
            for size in (75, 50, 25):
                if left >= size:
                    out.append(size)
                    left -= size
                    break
        return out

    def _window_pick(self, slots, alloc, need):
        """从空场里按 ``from`` / ``period_id`` 连续取够 ``need`` 个 count。"""
        want_from = (th._clock_minutes(alloc.get("from"))
                     if alloc.get("from") else None)
        want_period = str(alloc.get("period_id") or "")
        ordered = sorted([s for s in (slots or ())
                          if int(s.get("counts") or 0) > 0],
                         key=lambda s: (th._clock_minutes(s.get("start")) or 0))
        if want_period:
            mine = [s for s in ordered
                    if str(s.get("period_id") or "") == want_period]
        else:
            mine = []
            for index, slot in enumerate(ordered):
                if th._clock_minutes(slot.get("start")) == want_from:
                    mine = ordered[index:]
                    break
        pieces, left = [], int(need)
        for slot in mine:
            if left <= 0:
                break
            take = min(left, int(slot.get("counts") or 0))
            if take <= 0:
                continue
            pieces.append((slot, take))
            left -= take
        return pieces, left

    def _apply_manual_windows(self, day, allocator, window_allocs):
        """手动时间段：先把这段场次切出来、挂上虚拟任务，其余项目看剩余空场。

        切场走 ``commit_layout`` / ``_commit_window_sessions``（和 plan 同一个
        落盘口），挂任务走 ``task_store.create`` + ``_assign_task``（和 plan 同一
        条流水线）；不做「直接塞进已存在 session」的猴子补丁。
        """
        iso = day.isoformat()
        result = {"sessions": 0, "counts": 0, "failed": 0, "task_ids": []}
        carved = []
        seen_periods = {}
        for alloc in window_allocs:
            project = alloc.get("project") or {}
            need = int(alloc.get("count") or 0)
            if need <= 0:
                continue
            pieces, left = self._window_pick(allocator.slots, alloc, need)
            label = (("从 %s 起" % alloc.get("from")) if alloc.get("from")
                     else ("时段 %s" % alloc.get("period_id")))
            if not pieces or left > 0:
                result["failed"] += 1
                self.say("⚠ 「%s」的手动时间段（%s）空场不够：能排 %d count，少排 %d。"
                         % (project.get("title"), label, need - left, left), "warn")
                continue
            keys = {pp._period_key(s.get("period") or {}) for s, _take in pieces}
            clash = [key for key in keys if key in seen_periods]
            if clash:
                result["failed"] += 1
                self.say("⚠ 「%s」和「%s」都要占 %s —— 一轮 plan 里一个时段只能给"
                         "一个手动时间段。" % (project.get("title"),
                                              seen_periods[clash[0]], clash[0]), "warn")
                continue
            for slot, take in pieces:
                period = dict(slot.get("period") or {})
                fresh = self._window_durations(take * pp.MINUTES_PER_COUNT)
                if not fresh:
                    continue
                try:
                    if slot.get("window"):
                        # 洞后面的窗口：layout 表达不了起点，落成 extras。
                        wslot = {"period": dict(period, start=slot.get("start"),
                                                end=slot.get("end")),
                                 "window": True}
                        self._commit_window_sessions(day, wslot, fresh)
                        sessions = sp.plan_period(wslot["period"], fresh)
                    else:
                        prefix = (list(slot.get("append_prefix") or ())
                                  if slot.get("append") else [])
                        self.commit_layout(day, period, prefix + fresh,
                                           source="plan", reason="simple-window",
                                           book=False, announce=False,
                                           write_csv=False, lock_timeout=5.0)
                        sessions = sp.plan_period(period, prefix + fresh)[len(prefix):]
                except Exception as e:             # noqa: BLE001 - 一段坏了不挡其余
                    result["failed"] += 1
                    rl.log_event("plan", "window_layout_error", level="error",
                                 day=iso, period=period.get("id"),
                                 error="%s: %s" % (type(e).__name__, e))
                    self.say("⚠ 手动时间段「%s」切场失败：%s"
                             % (project.get("title"), e), "warn")
                    continue
                carved.append((alloc, sessions))
                seen_periods[pp._period_key(slot.get("period") or {})] = (
                    project.get("title") or "?")
        if not carved:
            return result
        try:
            self.rewrite_data_csv_from_plan()
            self.rebuild_rows()
        except Exception as e:                     # noqa: BLE001
            rl.log_event("plan", "window_csv_error", level="error",
                         day=iso, error="%s: %s" % (type(e).__name__, e))
            self.say("手动时间段切好了，但重算 data.csv 失败：%s" % e, "warn")
        by_start = {}
        for index, row in enumerate(self.rows or (), 1):
            if str(row.get("date") or "") != iso or not row.get("planned"):
                continue
            by_start.setdefault(str(row.get("startLabel") or ""), index)
        for alloc, sessions in carved:
            project = alloc.get("project") or {}
            pid = project.get("id")
            pk = "simple:%s" % (project.get("pro_id") or pid)
            total = int(alloc.get("count") or 0)
            used, records = set(), []
            for sess in sessions:
                index = by_start.get(str(sess.get("start") or ""))
                if not index:
                    continue
                minutes = int(sess.get("duration") or 0)
                count = ts.minutes_to_count(minutes) or 1
                fields = {"title": project.get("title") or pk,
                          pj.PROJECT_FIELD: project.get("title") or "",
                          pj.PROJECT_ID_FIELD: pid,
                          pj.PROJECT_TASK_FIELD: pk,
                          "count": count,
                          "count_total": max(count, total)}
                source = {"kind": "project", "id": pk,
                          "title": project.get("title") or pk,
                          "url": "", "local_only": True}
                existing = self._plan_mirror_pick(pid, pk, exclude=used)
                reusable, _why = self._plan_reuse_reason(existing, day, exclude=used)
                try:
                    if reusable:
                        task = self.task_store.apply_project_plan(
                            existing["id"], fields, source=source)
                    else:
                        task = self.task_store.create(fields, omnifocus=None,
                                                      source=source)
                except ValueError as e:
                    result["failed"] += 1
                    self.say("⚠ 「%s」没建成虚拟任务：%s"
                             % (project.get("title"), e), "error")
                    continue
                used.add(task.get("id"))
                result["task_ids"].append(task.get("id"))
                try:
                    self._assign_task(task, str(index), refresh=False,
                                      allow_unbooked=True)
                except ValueError as e:
                    result["failed"] += 1
                    self.say("⚠ 「%s」没挂到 session #%s：%s"
                             % (project.get("title"), index, e), "warn")
                    continue
                result["sessions"] += 1
                result["counts"] += count
                records.append({"pk": pk, "task_id": task["id"], "count": count,
                                "start": str(sess.get("start") or ""),
                                "duration": minutes})
            if records:
                self.projects.record_items(pid, day, records, append=True)
                self.say("📌 手动时间段：「%s」占住 %s（共 %d count）。"
                         % (project.get("title"),
                            "、".join("%s(%d)" % (r["start"], r["duration"])
                                     for r in records),
                            sum(int(r["count"]) for r in records)), "ok")
            rl.log_event("plan", "simple_window", level="info", day=iso,
                         project_id=pid, start=alloc.get("from"),
                         period=alloc.get("period_id"), need=total,
                         placed=sum(int(r["count"]) for r in records))
        return result

    def _prune_simple_ghosts(self, day, used_ids=()):
        """收掉简单项目「开放、没挂场、这轮也没用」的虚拟任务。

        反复 plan 时同 pk（``simple:<pro_id>``）会按场次数建几条；配额变小或
        换时段后，多出来的那些不该留在任务库里当幽灵。
        """
        used = {int(x) for x in (used_ids or ()) if x is not None}
        simple_ids = {str(view.get("id")) for view in self.projects.plan_ops()
                      if view.get("simple")}
        if not simple_ids:
            return []
        dropped = []
        for task in self.task_store.list():
            if not ts.TaskStore.is_open(task) or task.get("session"):
                continue
            fields = task.get("fields") or {}
            if str(fields.get(pj.PROJECT_ID_FIELD)) not in simple_ids:
                continue
            if not str(fields.get(pj.PROJECT_TASK_FIELD) or "").startswith("simple:"):
                continue
            if int(task.get("id") or 0) in used:
                continue
            try:
                self.task_store.drop(task["id"], reason="simple-ghost")
                dropped.append(task["id"])
            except (ValueError, TypeError):
                continue
        if dropped:
            rl.log_event("plan", "simple_ghost_pruned", level="info",
                         day=day.isoformat(), removed=dropped)
        return dropped

    def _plan_row_index(self, slot):
        """把「计划里这一场」对到表格行，并让**计划**先于旧 API 数据生效。

        为什么需要：``rebuild_rows`` 合并时如果 API 里还挂着旧时长的那一场
        （19:30 的 50 分钟刚被 plan 切成 75），行会继续按 50 分钟画 —— 于是
        ``_assign_task`` 把任务挂到「19:30–20:20 / 50min」，plan 指纹、due、
        界面全记成 50；下一轮 refresh 又看出「绑定和计划不一致」，整天重排
        （2026-11-26：整场 3 count 的 EngLit 被记成 50 分钟的场次）。

        这里按 (日期, 起点) 找行，时长以计划为准：旧场次正被
        ``_cancel_stale_period_sessions`` 换掉，行上的 id 也一并清空（还没订上）。
        """
        want_date = str(slot.get("date") or "")
        want_start = str(slot.get("start") or "")
        for idx, row in enumerate(self.rows or (), 1):
            if (str(row.get("date") or ""), str(row.get("startLabel") or "")) != (
                    want_date, want_start):
                continue
            try:
                dur = int(slot.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            if dur and int(row.get("durationMin") or 0) != dur:
                row["durationMin"] = dur
                start_min = th._clock_minutes(want_start)
                if start_min is not None:
                    row["endLabel"] = th.fmt_minutes(start_min + dur)
                row["id"] = None
                row["focusmate_session_id"] = None
            return idx
        try:
            return int(slot.get("index") or 0)
        except (TypeError, ValueError):
            return 0

    def _plan_apply(self, allocator, allocs, append_to_plan=False, book=True,
                    title="排期（plan complete）", record_decision=True):
        """分配完了：先记资源，再挑任务、排进 session。

        ``append_to_plan=True`` 给 sessional plan / 手工单场安排用：当天已有
        的分配和 items 保留，只把这一次的结果追加进去，不覆盖整天计划。

        ``book=True`` 时末尾统一走 :meth:`book` 把执行区的场次订上；
        refresh 的自动重排传 False（对账轮不新造外部动作）。

        **进度条走公共接口**：``jobs.begin(0, title=...)`` + ``jobs.step()``，
        本地那几步（挑任务 / 写排布 / 重算 csv）和末尾的预约 job 共用一个面板。
        以前这条命令一个字节的进度都没有，慢的时候看起来像卡死。

        「尽量一场一个任务、尽量不拆开」在 :func:`project_plan.schedule` 里：先跑
        二分图匹配求「一对一站位」，配不上才拆、才往同一场里叠。
        """
        day = allocator.day
        progress = getattr(self, "jobs", None)
        if progress is not None:
            progress.begin(0, title=title)
            progress.step("挑任务：%d 个项目分到的 count" % len(allocs))
        # **落地前的硬闸**：分到 count 的项目必须真的有可排的活。
        # 2026-11-22 现场：给「历史 W13 复习 2」说好 8 count，但那 4 条任务
        # 一条侧标签都没有 → distribute 挑出 0 条 → 0 场 session，还把 T2–T4
        # 已有的排布整片清空。这里在**撤旧计划之前**先筛一遍：一条都排不出的
        # 项目直接踢掉并说明原因；全都没活就整次 plan 不执行，旧排布一个字不动。
        usable, unfillable = [], []
        for alloc in allocs:
            project = alloc.get("project") or {}
            if project.get("simple"):
                # 简单项目容量 ∞、不查清单：不存在「一条可排的活都没有」。
                usable.append(alloc)
                continue
            try:
                # 口径必须和 distribute() 一致：side 只读 plan_ops() 填的键，
                # 没有就不按侧过滤（否则裸 op / 旧调用方会被误判成 0 条可排）。
                stat = pj.stats(
                    project, self.task_store,
                    side=project.get("side"),
                    default_side=project.get("default_side"))
                remaining = int(stat.get("remaining") or 0)
            except Exception:                      # noqa: BLE001 - 算不出来就按没活处理
                remaining = 0
            if remaining <= 0:
                unfillable.append((project, int(alloc.get("count") or 0)))
            else:
                usable.append(alloc)
        if unfillable and not usable and not (allocator.pins or ()):
            self.say("✗ 这次 plan 没有执行：分到 count 的项目里一条可排的任务都没有 —— %s。"
                     "已有排布一个字没动。"
                     % "；".join("「%s」%s" % (p.get("title") or p.get("id"),
                                              pj.empty_reason(p))
                                for p, _count in unfillable), "error")
            rl.log_event("plan", "apply_refused", level="warning",
                         day=day.isoformat(), reason="no_eligible_tasks",
                         projects=[p.get("id") for p, _c in unfillable])
            if progress is not None:
                progress.step("没有可排的任务，已放弃")
            return {"placements": 0, "counts": 0, "unscheduled": 0,
                    "notes": [], "failed": 0, "used_slot_indices": set(),
                    "refused": "no_eligible_tasks"}
        if unfillable:
            for project, count in unfillable:
                self.say("⚠ 「%s」分到 %d count 作废：%s。"
                         % (project.get("title") or project.get("id"), count,
                            pj.empty_reason(project)), "warn")
            allocs = usable
        # 普通 plan 是整天重排：先撤旧计划，避免旧任务占着场次、和新算法
        # 排出来的内容并存在同一天。sessional plan / project -a 是追加语义，
        # 保留当天已有计划。
        if not append_to_plan:
            self._reset_day_plans(day)
        all_allocs = list(allocs)
        window_allocs = [a for a in allocs if a.get("from") or a.get("period_id")]
        if window_allocs:
            self.rebuild_rows()
            carved = self._apply_manual_windows(day, allocator, window_allocs)
            window_task_ids = set(carved.get("task_ids") or ())
            if carved.get("sessions"):
                try:
                    self.rebuild_rows()
                    # replan=False：刚切好的手动时间段**必须**留在场次上（它
                    # 是这一轮的第一阶段占位）。用 replan=True 会把刚建的虚拟
                    # 任务当「上一轮计划」撤掉，整段又被重切成别人的场次。
                    fresh_slots, _fresh_notes = self._plan_slots(
                        day, include_current=False, replan=False)
                    allocator.slots[:] = fresh_slots
                except Exception as e:             # noqa: BLE001 - 重读失败就按旧空场
                    rl.log_event("plan", "window_reslot_failed", level="warning",
                                 day=day.isoformat(),
                                 error="%s: %s" % (type(e).__name__, e))
            # 手订段已经占住了那些场次：剩下的项目只在剩余空场里摊派。
            allocs = [a for a in allocs
                      if not (a.get("from") or a.get("period_id"))]
        invested = pj.invested_by_project(self.task_store)
        # formatter 任务 / 尚未到 defer 点的本地任务都不该被项目 plan 再算一遍。
        # 前者已经有自己的时间占位，后者现在根本还没进入可安排队列。
        excluded = {}
        skip_pks = {}
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if not pk or pid is None or not ts.TaskStore.is_open(task):
                continue
            if task.get("formatter") or ts.task_is_deferred(task):
                excluded.setdefault(pid, set()).add(str(pk))
            elif str((task.get("session") or {}).get("start") or ""):
                # 已经站在某场 session 上的项目任务（含清单派生行）这一轮不再排：
                # 追加语义下这是必须的；整天重排下同样必须 —— 它的绑定要么是
                # 用户手动挂的，要么是当天的一次性场次，plan 重复唤起只会让
                # 同一条清单行出现两份（2026-11-17 ``edge_list#d1`` 事故：
                # 上一轮绑在 23:15，这一轮又建了一条同 pk 的任务）。
                excluded.setdefault(pid, set()).add(str(pk))
                skip_pks.setdefault(pid, set()).add(str(pk))
        pins = [dict(pin) for pin in (getattr(allocator, "pins", None) or ())]
        for pin in pins:
            # 用户点名的任务优先：项目摊派必须跳过它，否则同一条任务会被唤起两份。
            pid = pin.get("project_id")
            pk = str(pin.get("pk") or "")
            if pid is None or not pk:
                continue
            excluded.setdefault(pid, set()).add(pk)
            skip_pks.setdefault(pid, set()).add(pk)
        items, notes = pp.distribute(
            allocs, [a["project"] for a in allocs], invested,
            # 用 warden 的时钟（测试可以钉死；生产就是墙上时钟）。以前这里
            # 直接 datetime.now()，测试把 clock_now 定在 15:58 也没用，defer
            # 到 23:00 的任务会按真实时间被当成「已经可以做了」。
            now=self._now_dt(), exclude_pks=excluded,
            rotation=self.derived_rotation(), skip_pks=skip_pks, pins=pins)
        # period-first：只要这一天的空场里有“自动 layout”的 period，就先把任务
        # 按 period 排好，再根据任务反推 session durations；手动 layout 仍然
        # 作为硬约束，只允许任务往里填。
        placements, unscheduled, more, auto_layouts = pp.schedule_periods(
            items, allocator.slots)
        rl.log_event("plan", "scheduled", level="debug", day=day.isoformat(),
                     items=len(items), placements=len(placements),
                     unscheduled=len(unscheduled),
                     auto_layouts=len(auto_layouts or {}))
        if progress is not None:
            progress.step("排进 %d 场 session（%d 段要重切）"
                          % (len(placements), len(auto_layouts or {})))
        notes = list(notes) + list(more)
        # 先把新排布写进中间层并重算 data.csv；旧场次只取消未来、未开始的，
        # _cancel_stale_period_sessions 内部会再保护开始中的场次。
        period_by_key = {}
        for slot in allocator.slots or ():
            period = dict(slot.get("period") or {})
            if not period:
                continue
            key = pp._period_key(period)
            period_by_key[key] = period
        try:
            _layouts_now = sp.load_layouts()
        except Exception:                          # noqa: BLE001
            _layouts_now = {}
        window_by_key = {}
        for slot in (getattr(allocator, "slots", None) or ()):
            if not slot.get("window"):
                continue
            window_by_key[pp._period_key(slot.get("period") or {})] = dict(slot)
        for key, durations in (auto_layouts or {}).items():
            if key in window_by_key:
                # 手订场后面的窗口：layout 表达不了起点，落成 extras。
                try:
                    self._commit_window_sessions(day, window_by_key[key], durations)
                except Exception as e:             # noqa: BLE001 - 一段坏了不挡其余
                    rl.log_event("plan", "window_commit_error", level="error",
                                 day=day.isoformat(),
                                 period=(window_by_key[key].get("period") or {}).get("id"),
                                 error="%s: %s" % (type(e).__name__, e))
                    notes.append("窗口排布落盘出错：%s" % e)
                continue
            period = period_by_key.get(key)
            if not period:
                continue
            # 原来是手动/ formatter 切的段，追加之后仍然算硬约束（不能因为
            # plan 往尾巴加了一场，下一次 plan 就把人家原来的切分重排）。
            try:
                _rec = sp.get_layout_record(_layouts_now, day, period.get("id"))
            except Exception:                      # noqa: BLE001
                _rec = None
            _keep_manual = bool(_rec and not sp.is_auto_source(_rec.get("source")))
            try:
                # 走唯一事务：取消旧场 → 写 layout → 清绑定 → 重算 data.csv。
                # 这里不预约（book=False）：整天重排的预约由 _plan_apply 末尾统一触发；
                # refresh 的自动重排整轮都在 _suppress_book 里。
                # write_csv=False：几个时段改完由下面统一重算一次，别每段整表重写。
                # source="plan"：给 plan 自己切出来的 session 留个记号（表格里 🗂），
                # 下一次 plan 认得它、可以重算；用户手动 `period` 切的是 manual。
                self.commit_layout(day, period, durations,
                                   source="manual" if _keep_manual else "plan",
                                   reason="auto-plan", book=False, announce=False,
                                   write_csv=False, lock_timeout=5.0)
            except ValueError as e:
                rl.log_event("plan", "layout_failed", level="warning",
                             day=day.isoformat(), period=period.get("id"),
                             error=str(e))
                notes.append("自动排布「%s」没落盘：%s" % (period.get("id"), e))
            except Exception as e:             # noqa: BLE001 - 一段坏了不挡其余
                rl.log_event("plan", "layout_error", level="error",
                             day=day.isoformat(), period=period.get("id"),
                             error="%s: %s" % (type(e).__name__, e))
                notes.append("自动排布「%s」落盘出错：%s" % (period.get("id"), e))
        if auto_layouts:
            if progress is not None:
                progress.step("重算 data.csv（%d 段排布已落盘）" % len(auto_layouts))
            try:
                self.rewrite_data_csv_from_plan()
                self.rebuild_rows()
            except Exception as e:             # noqa: BLE001
                self.say("自动排布已生成，但重算 data.csv 失败：%s" % e, "warn")
            # 新 data.csv 的行号可能整体变了：按 (日期, 起点) 找回对应行。
            # 旧 API session 可能还占着同一分钟但 duration 不同；这一轮已经
            # 安排取消它，本地先把行改成新计划时长并当成「还没订上」，
            # 避免任务错误地挂到旧 session 的 duration / meetingId 上。
            by_start = {}
            for idx, row in enumerate(self.rows, 1):
                key = (str(row.get("date") or ""),
                       str(row.get("startLabel") or ""))
                if row.get("planned") and key not in by_start:
                    by_start[key] = (idx, row)
            for place in placements:
                slot = place.get("slot") or {}
                key = (str(slot.get("date") or day.isoformat()),
                       str(slot.get("start") or ""))
                got = by_start.get(key)
                if not got:
                    continue
                idx, row = got
                if int(row.get("durationMin") or 0) != int(slot.get("duration") or 0):
                    row["durationMin"] = int(slot.get("duration") or 0)
                    row["endLabel"] = slot.get("end") or row.get("endLabel")
                    row["id"] = None
                    row["focusmate_session_id"] = None
                slot["index"] = idx
        for alloc in all_allocs:
            count = int(alloc["count"])
            if append_to_plan:
                existing = pj.plan_of(alloc["project"], day) or {}
                count += int(existing.get("count") or 0)
            self.projects.set_allocation(alloc["project_id"], day, count)
        if record_decision and not append_to_plan:
            # `pro N clear` 之后 complete：这一天的 count 也要真的归 0（不只是
            # 不排任务）。决定台账里记 0，下一次打开面板就不会又亮回旧数字。
            kept = {str(a.get("project_id")) for a in all_allocs
                    if int(a.get("count") or 0) > 0}
            for project in (getattr(allocator, "projects", None) or ()):
                if str(project.get("id")) in kept:
                    continue
                try:
                    self.projects.set_allocation(project["id"], day, 0)
                except Exception:                  # noqa: BLE001 - 单条清不掉不挡别
                    pass
        # 这一次 plan 要用的场次 = 「我又想要它们了」：把「永不重订」判决和
        # daemon 记在 state 里的判死结论一起撤掉。commit_layout 只对**重切过**的
        # 时段做这件事；计划没变（layout 没动）时它根本不会跑，旧判决就留到下一次
        # refresh —— 那正是「plan 之后 r -c，计划被冲掉」的另一条路。
        revive_keys = []
        for place in placements:
            start = str((place.get("slot") or {}).get("start") or "")
            if start:
                revive_keys.append(sch.key_of(day, start))
        if revive_keys:
            ks.clear_abandoned(revive_keys)
            ks.clear_slot_status(revive_keys)

        made, failed = {}, 0
        used_ids = set()
        pin_records = []
        for place in placements:
            item = place["item"]
            slot = place["slot"]
            project = item["project"]
            if item.get("pinned"):
                # 用户点名的任务（`task <任务> <count>`）：**复用那一条本地任务**，
                # 不新建镜像 —— 它就是它自己的「单任务项目」。场次照样走同一条
                # 排期/挂号流水线，所以这里只补一次挂载。
                try:
                    pin_task = self.task_store.get(item.get("existing_task_id"))
                except (ValueError, TypeError):
                    failed += 1
                    self.say("⚠ 固定任务 #%s 已经不在任务库里了，跳过。"
                             % item.get("existing_task_id"), "warn")
                    continue
                try:
                    self._assign_task(pin_task,
                                      str(self._plan_row_index(slot)),
                                      refresh=False, allow_unbooked=True)
                except ValueError as e:
                    failed += 1
                    self.say("⚠ 固定任务「%s」没挂到 session #%s：%s"
                             % (item.get("title"), slot["index"], e), "warn")
                    continue
                used_ids.add(pin_task.get("id"))
                try:
                    bound = self.task_store.get(pin_task["id"]) or {}
                except (ValueError, TypeError):
                    bound = {}
                sess = bound.get("session") or {}
                pin_records.append({
                    "task_id": pin_task["id"],
                    "pk": item.get("pk") or "",
                    "title": item.get("title") or "",
                    "count": int(place["count"]),
                    "project_id": item.get("project_id"),
                    "start": str(sess.get("start") or slot.get("start") or ""),
                    "date": str(sess.get("date") or day.isoformat()),
                    "duration": int(sess.get("duration") or 0) or int(
                        place.get("duration")
                        or int(place["count"]) * pp.MINUTES_PER_COUNT),
                })
                continue
            derived = item.get("derived") or None
            url = "" if derived else of.task_url(item["pk"])
            parent = place.get("parent") or {}
            parent_pk = "" if derived else (item.get("parent_pk") or parent.get("pk"))
            parent_title = "" if derived else (item.get("parent_title")
                                              or parent.get("title"))
            fields = {"title": item.get("title") or item["pk"],
                      pj.PROJECT_FIELD: project.get("title") or "",
                      pj.PROJECT_ID_FIELD: item["project_id"],
                      pj.PROJECT_TASK_FIELD: item["pk"]}
            # ``count`` 是这一场分到的份额（可能只是原任务的 1/3），
            # ``count_total`` 才是 OmniFocus 原任务的总 count。两者分开存，
            # 既保持 count↔duration 的本地排期语义，也避免把拆分 part 显示成
            # 独立任务 ``(0+1/1)``、``(0+2/2)``。
            total_count = ts.parse_count(item.get("total"))
            if total_count is None:
                total_count = ts.parse_count(item.get("count_total"))
            if total_count is None:
                try:
                    project_row = self.projects.by_id(item["project_id"])
                    project_task = pj.task_by_pk(project_row, item["pk"])
                    total_count = ts.parse_count(
                        (project_task or {}).get("count"))
                except Exception:               # noqa: BLE001 - 展示兜底不该挡住建任务
                    total_count = None
            if total_count and total_count > 0:
                fields["count_total"] = int(total_count)
            if item.get("note"):
                fields["note"] = item["note"]
            if parent_pk:
                # session 里只放子任务；note 明确写它是谁的子。
                parent_note = "父任务：%s" % (parent_title or parent_pk)
                fields["note"] = ((fields.get("note") or "") + "\n"
                                  + parent_note).strip()
                fields["duration"] = int(place.get("duration")
                                         or item.get("duration") or 0)
            elif place.get("by_minutes") or item.get("small"):
                # 短任务按实际分钟排；duration 写真实分钟（10 分钟就是 10 分钟），
                # count 交给 task_store.create 的 link_count_fields 去 ceil。
                fields["duration"] = int(place.get("duration")
                                         or item.get("minutes") or 0)
            else:
                fields["count"] = int(place["count"])
            source = {"kind": "project", "id": item["pk"], "url": url,
                      "title": item.get("title")}
            if derived:
                # 标题清单派生的任务：没有 OmniFocus 关联（完成只勾父任务 note），
                # pk 是合成 pk，账目按它记，重新 plan 时能找回同一条。
                fields.update(pp.derived_fields(derived, item["pk"]))
                source = pp.derived_source(derived)
            if item.get("local_only"):
                # relate --import 从模板文件 append 的任务在 OmniFocus 里没有
                # 真 primary key：不能挂 omnifocus，否则完成时会拿假 pk 写回。
                source["local_only"] = True
                source["url"] = ""
                url = ""
            if parent_pk:
                source.update({"parent_id": parent_pk,
                               "parent_title": parent_title})
            existing = self._plan_mirror_pick(item["project_id"], item["pk"],
                                              exclude=used_ids)
            reusable, why = self._plan_reuse_reason(existing, day, exclude=used_ids)
            spares = [] if reusable else self._open_mirrors_except(
                item["project_id"], item["pk"], exclude=used_ids)
            try:
                if reusable:
                    # 项目导入时已经把叶子任务镜像进任务库了：plan 唤起时复用
                    # 同一条，并把这次分到的 count / duration / 父任务来源写上去。
                    task = self.task_store.apply_project_plan(
                        existing["id"], fields, source=source)
                else:
                    task = self.task_store.create(
                        fields,
                        omnifocus=None if (item.get("local_only") or derived) else {
                            "id": item["pk"], "url": url,
                            "title": item.get("title")},
                        source=source)
                    if spares:
                        # 明明有能复用的开放镜像，却因为「第一条同 pk 记录」是
                        # 老副本（已收工/挂着别处）又新建了一条 —— 这就是任务库里
                        # 同 pk 幽灵越堆越多的那条路，落了日志才好查。
                        rl.log_event("plan", "mirror_duplicate", level="warning",
                                     pk=item["pk"], project_id=item["project_id"],
                                     new_id=task.get("id"),
                                     existing_id=(existing or {}).get("id"),
                                     reason=why,
                                     spare_ids=[t.get("id") for t in spares])
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」没建成本地任务：%s" % (item.get("title"), e), "error")
                continue
            used_ids.add(task.get("id"))
            try:
                self._assign_task(task, str(self._plan_row_index(slot)),
                                  refresh=False, allow_unbooked=True)
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」建好了但没挂到 session #%s：%s"
                         % (item.get("title"), slot["index"], e), "warn")
                continue
            # 记进 plan 的 start/duration 必须**等于任务真正挂上的那一场**：
            # refresh 就是拿这两项去核对绑定的，记岔了下一轮就会报「绑定和
            # plan 不一致」并把整天的计划清掉。``place["duration"]`` 是这个
            # 任务自己占多少分钟，小任务（<25 分钟）可以叠进大场，这时它
            # （10）和 session 长度（75）不是一个数 —— 所以回读绑定。
            try:
                bound = self.task_store.get(task["id"]) or {}
            except (ValueError, TypeError):
                bound = {}
            sess = bound.get("session") or {}
            made.setdefault(item["project_id"], []).append({
                "pk": item["pk"], "task_id": task["id"],
                "count": int(place["count"]),
                "start": str(sess.get("start") or slot.get("start") or ""),
                "duration": int(sess.get("duration") or 0) or int(
                    place.get("duration")
                    or int(place["count"]) * pp.MINUTES_PER_COUNT),
                "parent_pk": parent_pk or ""})
        for pid, records in made.items():
            self.projects.record_items(pid, day, records,
                                       append=append_to_plan)
        # 简单项目的重排幂等：这一轮没用上的开放未挂场虚拟任务收掉。
        self._prune_simple_ghosts(
            day, used_ids=set(used_ids) | set(locals().get("window_task_ids") or ()))
        # 固定项单独记台账：它们不一定属于任何 project（记不进项目库的 plans），
        # 但重排 / `plan clear` 必须能把它们解绑。
        if pin_records or pins:
            self._save_plan_pins(day, pin_records, append=bool(append_to_plan))
        # 资源决定与时间安排分开存：下次打开 plan 按它预填/复现。
        if record_decision and not append_to_plan:
            try:
                self._save_plan_decision(day, allocator, pin_records,
                                         allocs=all_allocs)
            except Exception as e:                 # noqa: BLE001 - 台账坏了不挡排期
                rl.log_event("plan", "decision_error", level="warning",
                             day=day.isoformat(),
                             error="%s: %s" % (type(e).__name__, e))
        if progress is not None:
            progress.step("把 %d 条任务挂到 session 上" % len(placements))
        self.rebuild_rows()
        total_counts = sum(int(p["count"]) for p in placements)
        self.say("🗂 %s 排好了：%d 条任务 → %d 场 session（共 %d count = %d 分钟）"
                 % (day.isoformat(), len(placements),
                    len({p["slot"]["index"] for p in placements}), total_counts,
                    total_counts * pp.MINUTES_PER_COUNT), "ok")
        for place in placements:
            item = place["item"]
            dur = place.get("duration")
            tail = ("，%d 分钟" % int(dur)) if dur else ""
            parent = place.get("parent") or {}
            if parent:
                tail += "，父任务「%s」" % (parent.get("title") or parent.get("pk"))
            priority = pj.priority_label(item.get("priority"))
            if priority:
                tail += "，%s" % priority
            self.say("   %s(%d) ← %s / %s（%d count%s%s%s）"
                     % (place["slot"].get("start"), int(place["slot"].get("duration") or 0),
                        item["project"].get("title") or "?",
                        item.get("title") or item["pk"], int(place["count"]), tail,
                        "，拆开的" if place.get("split") else "",
                        "，和别的任务同一场" if place.get("stacked") else ""), "info")
        for note in notes:
            self.say("⚠ %s" % note, "warn")
        for item in unscheduled:
            self.say("⚠ 「%s」（%d count）没排进去：空场次不够。"
                     % (item.get("title"), int(item.get("count") or 0)), "warn")
        if failed:
            self.had_error = True
        self._stamp_auto_plan_signature(day)
        # 排好的场次得有人去订 —— 统一走 book()（唯一预约出口）。
        # 以前 plan 完成什么都不触发，只能等 daemon 下一轮或手动 refresh --check，
        # 而 `period` / `session` 却是立刻订的：同一条流水线两个待遇。
        if book:
            self.book(day, why="plan 排完了，要把执行区的场次订上")
        self.invalidate()
        result = {"placements": len(placements), "counts": total_counts,
                  "unscheduled": len(unscheduled), "notes": notes, "failed": failed,
                  "used_slot_indices": {p["slot"]["index"] for p in placements}}
        rl.log_event("plan", "apply_done",
                     level="warning" if (failed or unscheduled) else "info",
                     day=day.isoformat(), placements=result["placements"],
                     counts=result["counts"], unscheduled=result["unscheduled"],
                     failed=failed, auto_layouts=len(auto_layouts or {}),
                     append=bool(append_to_plan), book=bool(book))
        return result

    def plan_panel(self):
        """`plan` 的分资源问答面板（挂在 repl_kit 的打断栏位里）。"""
        prompt = self.planning
        if prompt is None:
            return None
        body = Text()
        for text, style in prompt.lines():
            body.append(text + "\n", style=style or None)
        return Panel(body, title="[bold cyan]🗂 plan · 资源分配[/]",
                     border_style="cyan", box=box.HEAVY)

    def close_plan(self, note=None):
        """把 `plan` 那一问收掉（`complete` / `q` 之后）。"""
        prompt, self.planning = self.planning, None
        if prompt is None:
            return None
        if not prompt.closed:
            prompt.closed = True
        self.rebuild_rows()
        self.invalidate()
        return prompt

    def focus_day(self, day_iso):
        """把看板切到某一天（收尾时用：那一场在哪天就看哪天）。"""
        try:
            target = datetime.date.fromisoformat(str(day_iso))
        except ValueError:
            return False
        offset = (target - datetime.date.today()).days
        if offset == self.day_offset:
            return True
        self.day_offset = max(-3650, min(3650, offset))
        try:
            self.refresh(quiet=True)
        except Exception:                        # noqa: BLE001
            pass
        return True
