"""warden_core.project_mixin —— 项目表与 Day Binder 渲染、pro/op 命令、relate 导入导出、每日项目对账与 plan 排期。"""
import datetime
import os
import re
import time

from .common import (
    HERE,
    Panel,
    SRC_OMNIFOCUS,
    STYLE_MUTED,
    Table,
    Text,
    box,
    of,
    parse_relate_markdown,
    parse_relate_task,
    pj,
    pp,
    repl_kit,
    sch,
    th,
    ts,
)
from .hints import mute_window_for


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
        """契约视图用的项目行：每个 Pro 只显示**当前阶段对应的那一个 op**。

        ``running`` 只显示 SOP op，``bottleneck`` 只显示 incubator op；未关联
        真正项目 ``pro`` 的孤立 op 不在这里出现（仍可在 ``op`` 命令里管理）。
        行上的 ``number`` 用 Pro 序号，``op_number`` 另存 op 原序号。
        """
        day = day or self.sel_date
        op_rows = {row["id"]: row for row in self.project_rows(day)}
        out = []
        for pr in self.projects.pro_preview(day, self.task_store):
            op = pr.get("active_op")
            if op is None:
                continue
            row = dict(op_rows.get(op["id"]) or {})
            row.update({
                "number": pr["number"],
                "pro_id": pr["id"],
                "pro_number": pr["number"],
                "op_number": row.get("number"),
                "pro": pr["pro"],
                "pro_title": pr["title"],
                "pro_stage": pr["stage"],
                "effective_stage": pr.get("effective_stage") or pr["stage"],
                "stage_mismatch": bool(pr.get("stage_mismatch")),
                "active_op": op,
                "active_op_type": pr.get("active_op_type"),
            })
            out.append(row)
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

    def render_projects_table(self):
        """契约视图里**任务面板下面**那块项目表。

        每个真正项目 ``pro`` 一行，只展示当前阶段的 op：running → SOP，
        bottleneck → incubator。这样表里不会同时出现同一个项目的 SOP 和
        incubator 两套资源，plan 的序号也和这张表一致。
        """
        rows = self.current_project_rows()
        if not rows:
            return None
        t = Table(title="Project · 项目（每个 Pro 只显示当前阶段活跃的 op）",
                  box=box.SIMPLE_HEAVY, header_style="bold blue",
                  title_justify="left", expand=False)
        t.add_column("#", justify="right", style="bold", no_wrap=True)
        t.add_column("项目 / 当前 op", no_wrap=True, overflow="crop")
        t.add_column("阶段 / 类型", no_wrap=True)
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
            op_kind = row.get("active_op_type") or row.get("op_type") or "?"
            stage = (row.get("effective_stage") or row.get("pro_stage")
                     or "running")
            type_txt = "%s · %s" % (stage, op_kind)
            type_style = ("magenta" if op_kind == pj.OP_TYPE_INCUBATOR
                          else "cyan")
            if row.get("stage_mismatch"):
                type_txt += " ⚠阶段按实际 op 推导"
            if not row.get("allowed", True):
                type_txt += " ⏸"
                type_style = STYLE_MUTED
            op_title = (row.get("active_op") or {}).get("title") or "?"
            t.add_row(str(row["number"]),
                      Text("%s → %s" % (row.get("pro_title") or "?", op_title),
                           overflow="crop"),
                      Text(type_txt, style=type_style),
                      Text(str(row.get("remaining") or 0), style="bold cyan"),
                      Text(row.get("progress_text") or "—", style=STYLE_MUTED),
                      Text(plan_txt, style="green" if row.get("allocated") else STYLE_MUTED),
                      Text(str(row.get("tasks_open") or 0), style=STYLE_MUTED),
                      Text(str(row.get("week_count") or 0),
                           style="bold yellow" if row.get("week_missing")
                           else STYLE_MUTED),
                      Text(str(row.get("pk") or "")[:10] or "—"))
        total_remaining = sum(int(r.get("remaining") or 0) for r in rows)
        t.caption = ("%d 个活跃项目 · 剩余 %d count（%d 分钟）· 只显示当前阶段对应的 "
                     "op（running=SOP / bottleneck=incubator）· `plan` 也按同一份清单分配"
                     % (len(rows), total_remaining,
                        total_remaining * pp.MINUTES_PER_COUNT))
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

    def _finish_pro_advance(self, pro_id, op_id, target_stage=None):
        """完成 ``pro`` 的阶段切换并绑定新的 ``op``。"""
        pro, op = self.projects.advance_pro(pro_id, op_id, target_stage=target_stage)
        self.pending_advance = None
        if self.pending_op_type:
            self.pending_op_type = [oid for oid in self.pending_op_type
                                    if oid != op["id"]]
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        op_num = self.projects.number_of(op["id"]) or op["id"]
        op_type = pj.normalize_op_type(op.get("op_type")) or pj.OP_TYPE_SOP
        self.say("🔀 项目 #%s「%s」阶段已切换为 %s，并关联 %s op #%s「%s」。"
                 % (pro_num, pro.get("title"), pro.get("stage"),
                    op_type, op_num, op.get("title")), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _start_pro_advance(self, pro, tail_tokens=None):
        """执行 ``pro <序号> -a / --advance [op <序号> | task import ...]``。"""
        tail = [str(t).strip() for t in (tail_tokens or ()) if str(t).strip()]
        cur_stage = pj.normalize_pro_stage(pro.get("stage")) or pj.STAGE_RUNNING
        target_stage = (pj.STAGE_BOTTLENECK if cur_stage == pj.STAGE_RUNNING
                        else pj.STAGE_RUNNING)
        req_type = (pj.OP_TYPE_INCUBATOR if target_stage == pj.STAGE_BOTTLENECK
                    else pj.OP_TYPE_SOP)
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        if not tail:
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            self.say("🔀 项目 #%s「%s」准备从 %s 切换到 %s：需要提供新的 %s OmniFocus Project (op) 作为关联。"
                     % (pro_num, pro.get("title"), cur_stage, target_stage, req_type), "cmd")
            self.say("   请直接输入 `task import [链接]`（导入新 op）或 `op <序号>`（关联已有 op），`q` 取消。",
                     "info")
            self.invalidate()
            return pro
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
                    num, pro.get("title"), pro.get("stage"), file_note), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _pro_detail(self, pro):
        """展示真正项目 ``pro`` 的详情 + 关联 ``op`` 的任务清单。"""
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        linked_ops = self.projects.ops_for_pro(pro)
        sop_op = self.projects.by_id(pro.get("sop_op_id")) if pro.get("sop_op_id") else None
        inc_op = self.projects.by_id(pro.get("incubator_op_id")) if pro.get("incubator_op_id") else None
        file_txt = pro.get("file") or "（未关联，用 `pro %s file <路径>` 绑定）" % pro_num
        self.say("📂 Pro #%s「%s」· 阶段：%s · 关联文件：%s"
                 % (pro_num, pro.get("title"), pro.get("stage"), file_txt), "cmd")
        self.say("   SOP op：%s · incubator op：%s"
                 % (("#%s「%s」" % (self.projects.number_of(sop_op["id"]) or sop_op["id"],
                                    sop_op.get("title"))) if sop_op else "—",
                    ("#%s「%s」" % (self.projects.number_of(inc_op["id"]) or inc_op["id"],
                                    inc_op.get("title"))) if inc_op else "—"), "info")
        for op in linked_ops:
            self._project_detail(op)
        return pro

    def cmd_project(self, args=None):
        """``project`` / ``pro`` —— 管理真正项目 Project (pro) 与兼容项目库操作。

            pro / project                       列出真正项目 pro 与 OmniFocus 项目 op
            pro create <名称> [file=<路径>]     创建真正项目 pro
            pro <序号> -m complete|discard      本地状态（默认数据视图 disposable 会隐藏）
            pro <序号> -m active                恢复 active（非 active 时建议 id:<内部id>）
            pro <序号> -a / --advance [...]     快捷切换阶段（running ↔ bottleneck）并关联新 op
            pro <序号> file <路径|clear>        关联/清空项目的打开文件（open + enter 顺便打开）
            pro <序号> open                     打开该项目关联的文件
            pro <序号> stage <running|bottleneck>  直接设置阶段
            pro <序号> op <op序号>              关联已有 op
            pro <序号> interrupt [--hard]       硬中断当前进行中的 session：
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
                    need = ("incubator" if pr.get("stage") == pj.STAGE_BOTTLENECK
                            else pj.OP_TYPE_SOP)
                    self.say("📂 Pro #%d %s [阶段:%s] · 当前缺少 %s op（先 `op import %s` "
                             "或 `pro %d -a`）%s"
                             % (pr["number"], pr["title"], pr["stage"], need, need,
                                pr["number"], file_txt), "warn")
                    continue
                op = row.get("active_op") or {}
                self.say("📂 Pro #%d %s [阶段:%s → %s op #%s「%s」]：剩余 %d count · "
                         "进度 %s · %d 条未完成 · 本周 %d 条%s%s"
                         % (pr["number"], pr["title"], pr["stage"],
                            row.get("active_op_type") or "?",
                            row.get("op_number") or "?", op.get("title") or "?",
                            row.get("remaining") or 0, row.get("progress_text") or "—",
                            row.get("tasks_open") or 0, int(row.get("week_count") or 0),
                            (" · 今天说好 %d count（已排 %d）"
                             % (row.get("allocated"), row.get("scheduled")))
                            if row.get("allocated") else "", file_txt), "info")
            orphans = [row for row in self.project_rows()
                       if not row.get("pro_title")]
            if orphans:
                self.say("· 另有 %d 个未关联真正项目的 op；用 `op` 查看/关联。"
                         % len(orphans), "dim")
            return pro_rows
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

        rest = toks[1:]
        rest_low = [t.lower() for t in rest]

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
            updated = self.projects.set_pro_stage(pro["id"], st_val)
            self.selected_pro_id = updated["id"]
            pnum = self.projects.pro_number_of(updated["id"]) or updated["id"]
            self.say("📂 项目 #%s「%s」阶段已设为 %s。"
                     % (pnum, updated.get("title"), updated.get("stage")), "ok")
            self.rebuild_rows()
            self.invalidate()
            return updated

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
                     % (pnum, pro.get("title"), onum, op.get("title")), "ok")
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
                self.say("🗑 项目（pro）#%s「%s」已删除。" % (pnum, pro.get("title")), "ok")
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
                     % (pnum, pro.get("title"), pro.get("stage"),
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
                         % (pnum, pro.get("title"), pro.get("stage"),
                            (" · 文件：%s" % pro["file"]) if pro.get("file") else ""),
                         "info")
            return self._project_detail(project)

    # -- 硬中断 --------------------------------------------------------
    def _start_project_interrupt(self, pro, tail_tokens=None):
        """``project <序号> interrupt [--hard]``：清空当前场，等 title 后建新任务。

        默认就是 ``--hard``：当前进行中的 session 上所有未完成任务先全部下架
        （Focusmate task 走同一套 ``_task_detach``），然后让用户输入一个 title，
        新建「duration = 整场长度」的任务并挂回这一场。``--soft`` 目前不实现，
        避免用户以为有一个实际不会清场的选项。
        """
        tail = [str(t).strip().lower() for t in (tail_tokens or ()) if str(t).strip()]
        mode = "hard"
        for token in tail:
            if token in ("--hard", "hard", "硬"):
                mode = "hard"
            elif token in ("--soft", "soft", "软"):
                raise ValueError("目前只实现了硬中断（--hard）：会下掉当前 session "
                                 "的全部任务。软中断还没定义，先别用。")
            else:
                raise ValueError("用法：project <序号> interrupt [--hard]（默认就是 hard）")
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
            "mode": mode,
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

    def pending_interrupt_panel(self):
        """硬中断等待 title 时挂在打断栏位的面板。"""
        pending = self.pending_interrupt
        if pending is None:
            return None
        body = Text()
        body.append("项目「%s」(#%s) 硬中断\n"
                    % (pending.get("pro_title") or "?",
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
                self.say("📁 op #%d %s [%s%s%s]：剩余 %d count · 进度 %s · %d 条未完成 · 本周 %d 条"
                         % (row["number"], row["title"], op_kind, pro_bit, allow_bit,
                            row["remaining"], row["progress_text"], row["tasks_open"],
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
            pro_note = (" · 关联项目「%s」(%s)" % (pro.get("title"), pro.get("stage"))
                        if pro else "")
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
                     % (num, updated.get("title"), pnum, pro.get("title"), pro.get("stage")), "ok")
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
                             % (pnum, pro.get("title"), pnum))
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
        self.say("📁 #%d「%s」OmniFocus %s · 剩余 %d count · 进度 %s（%d/%d）"
                 % (num, project.get("title"),
                    ((project.get("omnifocus") or {}).get("id") or "?"),
                    stat["remaining"], stat["progress_text"],
                    stat["done"], stat["total"]), "cmd")
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
        self.say("🔄 项目 #%s「%s」刷新了：%d 条任务、剩余 %d count（进度 %s）"
                 % (num, updated.get("title"), len(updated.get("tasks") or []),
                    stat["remaining"], stat["progress_text"]), "ok")
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
                    or self.project_sync is not None)

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
        """主线程：自动拉新增；删除 / 修改按项目排队问用户。"""
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

    def _plan_slots(self, day):
        """这一天的**可用空场** → ``(slots, notes)``。

        只排除三类：已经过去的（今天就按「现在」筛）、已经挂任务的、
        被 formatter 占掉的。**没订上 Focusmate 的场次照常计入**（notes 里点名），
        任务先本地挂到那场，等 daemon 订上之后 ``refresh --check`` 再补挂。
        """
        slots, notes = [], []
        now = self._now_dt()
        now_ms = int(now.timestamp() * 1000)
        is_today = (day == datetime.date.today())
        unbooked, busy, past, reserved = [], 0, 0, 0
        try:
            reservations = th.collect_reservations(
                self.task_store.list(), day.isoformat())
        except Exception:                          # noqa: BLE001 - 坏任务不该挡 plan
            reservations = []
        for index, row in enumerate(self.rows, 1):
            if not row.get("planned"):
                continue
            # 场次的归属看**开始时间**：今天 23:15 开始、明天 00:05 结束
            # 仍然是今天的一场。以前这里还要求 row["date"] 完全相等，
            # 一旦行来自 API、日期字段被写成了终点日，跨午夜场次就整场漏算。
            start_dt = self._row_start_datetime(row, fallback_day=day)
            if start_dt is None or start_dt.date() != day:
                continue
            if row.get("period_muted"):
                continue
            if is_today and start_dt <= now:
                past += 1
                continue
            if self.task_store.tasks_for_session(start_dt.date().isoformat(),
                                                 row.get("startLabel")):
                busy += 1
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
                reserved += 1
                continue
            slots.append(pp.slot_record(index, start_dt.date().isoformat(),
                                        row.get("startLabel"),
                                        row.get("durationMin"), row.get("id"),
                                        row.get("endLabel")))
            if row.get("id") is None:
                unbooked.append(row.get("startLabel") or "?")
        if past:
            notes.append("今天已经过去的 %d 场不算空闲。" % past)
        if busy:
            notes.append("%d 场已经挂着任务了，不算空闲。" % busy)
        if reserved:
            notes.append("%d 场已经被 formatter 占掉，项目 plan 不会碰。" % reserved)
        if unbooked:
            notes.append("%s 还没订上 Focusmate，但仍计入空场（plan 排进去后，"
                         "等订上会自动挂任务）。" % "、".join(unbooked))
        return slots, notes

    def cmd_plan(self, args=None):
        """``plan`` —— 把这一天剩下的空闲切成 count，分给项目，再自动排任务。

        用户要的整条链路：先看元数据（今天还剩多少空闲时间 / 几个 count），再逐个
        项目问「分多少」，敲 ``complete`` 之后按每个项目分到的 count 唤起任务、排进
        session（尽量一场一个任务、尽量不拆开）。

            plan                    今天（**默认就是今天**，和当前看的是哪天无关）
            plan +1                 明天（plan 1 / plan 明天 / plan 2026-11-07 同义）
            task 1 4                项目 #1 分 4 个 count（= 100 分钟）
            task 1 2                再敲一次是**覆盖**（变成 2，不是 6）
            task 1 clear            清空 #1
            complete                分配完了，开始排任务

        分配**超预算**（加起来超过今天剩下的 count）时整条命令都不执行。

        为什么不跟着 `day` 走：`plan` 是「给哪一天分资源」的**动作**，不是看板。
        晚上正在看明天（`day 1`）时顺手敲 `plan`，想排的多半还是明天，所以有
        `plan +1` 这种明确写法；不给参数就是今天 —— 免得「我以为在排今天、
        其实排了后天」。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if self.planning is not None:
            raise ValueError("已经有一次 plan 在等你的分配（看下面那块面板）："
                             "`task <序号> <count>` 分资源、`complete` 开始排、`q` 放弃。")
        if (self.pending_import is not None or self.nostart is not None
                or self.pending_interrupt is not None or self.review_active()):
            raise ValueError("先把手头那一问答完（收尾 / 到点没开始 / 导入确认 / 硬中断 title）"
                             "再来 plan。")
        day, offset = self._plan_target(toks)
        self.projects.ensure_tagged_pros()
        untyped = self.projects.untyped_linked_ops()
        if untyped:
            self.check_untyped_ops(announce_import=False)
            raise ValueError("还有 %d 个已关联真正项目的 OmniFocus 项目（op）没说明类型"
                             "（SOP / incubator）；请先按提示补全类型，再 `plan`。"
                             % len(untyped))
        # 唯一候选清单：每个 active pro 当前阶段对应的那一个 op。running 取
        # SOP、bottleneck 取 incubator，所以不再有全局 SOP / incubator 两阶段。
        plan_ops = self.projects.plan_ops()
        if not plan_ops:
            if not self.projects.active_pros():
                raise ValueError("项目库是空的：在 OmniFocus 里 ⌘C 一个**项目**，"
                                 "再敲 `task import sop` 或 `task import incubator`"
                                 "（或者先 `pro create <名称>` 再把 op 关联给它）。")
            raise ValueError("当前没有可排期的 OmniFocus 项目（op）：只有关联了真正项目 pro、"
                             "且类型与项目阶段匹配的 op 才会进 plan（running 需要 SOP，"
                             "bottleneck 需要 incubator）。先用 `op <序号> pro <N>` 关联项目、"
                             "`pro <序号> -a` 切阶段，或用 `op <序号> sop|incubator` 设类型。")
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
        slots, notes = self._plan_slots(day)
        for note in notes:
            self.say("· %s" % note, "info")
        if not slots:
            raise ValueError("%s 一场空着的 session 都没有 —— 换个日子，或者先 "
                             "`session <HH:MM> <25|50|75>` 把排布加上。" % day.isoformat())
        cap = pp.capacity(slots)
        pro_map = {op["id"]: self.projects.pro_for_op(op) for op in plan_ops}
        self.planning = pp.Allocator(
            day, plan_ops, slots, task_store=self.task_store,
            hooks=self.plan_hooks(), pro_map=pro_map)
        by_type = {}
        for op in plan_ops:
            kind = pj.normalize_op_type(op.get("op_type")) or "?"
            by_type[kind] = by_type.get(kind, 0) + 1
        kind_txt = "、".join("%s %d" % (kind, count)
                            for kind, count in sorted(by_type.items()))
        self.say("🗂 %s 的项目资源分配：%d 个当前 op（%s）、%d 场空 session = %d count 可以分"
                 "（%d 分钟；1 count = %d 分钟）"
                 % (day.isoformat(), len(plan_ops), kind_txt,
                    cap["sessions"], cap["counts"], cap["minutes"],
                    pp.MINUTES_PER_COUNT), "cmd")
        self.say("   `task <序号> <count>` 分给项目（序号 = 面板上的 #）、"
                 "`task <序号> clear` 清空、`complete` 开始排任务、`q` 放弃。", "info")
        self.invalidate()
        return self.planning

    #: `plan` 的日期参数里认的那几个人话
    PLAN_DAY_WORDS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1,
                      "后天": 2, "大后天": 3, "昨天": -1, "yesterday": -1}

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

    def free_plan_slots(self, day):
        """那一天「还没开始、没静音、没挂任务」的排好的场次（只看 data.csv + 本地绑定）。

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
            try:
                if mute_window_for(day, label):
                    continue
            except Exception:                      # noqa: BLE001
                pass
            out.append(sess)
        return out

    def pending_plan(self, day=None):
        """今天还没 plan 吗？→ ``{"sessions", "counts", "starts"}`` 或 ``None``。

        三条同时成立才算「该提醒」：项目库里有 active 项目、这一天**一个项目都没分过
        count**、而且这一天还有没开始、没挂任务、没静音的场次。已经 plan 过（哪怕只
        排了一部分）就不再吵 —— 提醒是用来「别忘了」，不是用来记账的。
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
        slots = self.free_plan_slots(day)
        if not slots:
            return None
        counts = sum(int(s.get("duration") or 0) // pp.MINUTES_PER_COUNT for s in slots)
        return {"sessions": len(slots), "counts": counts,
                "starts": [s.get("start") for s in slots]}

    def plan_hooks(self):
        """`plan` 要碰外面世界的那个口子（其余全在 project_plan.Allocator 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            "apply": lambda allocator, allocs: self._plan_apply(allocator, allocs),
        }

    def _plan_apply(self, allocator, allocs):
        """分配完了：先记资源，再挑任务、排进 session。

        「尽量一场一个任务、尽量不拆开」在 :func:`project_plan.schedule` 里：先跑
        二分图匹配求「一对一站位」，配不上才拆、才往同一场里叠。
        """
        day = allocator.day
        invested = pj.invested_by_project(self.task_store)
        # formatter 任务 / 尚未到 defer 点的本地任务都不该被项目 plan 再算一遍。
        # 前者已经有自己的时间占位，后者现在根本还没进入可安排队列。
        excluded = {}
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if not pk or pid is None or not ts.TaskStore.is_open(task):
                continue
            if task.get("formatter") or ts.task_is_deferred(task):
                excluded.setdefault(pid, set()).add(str(pk))
        items, notes = pp.distribute(
            allocs, [a["project"] for a in allocs], invested,
            now=datetime.datetime.now(), exclude_pks=excluded)
        placements, unscheduled, more = pp.schedule(items, allocator.slots)
        notes = list(notes) + list(more)
        for alloc in allocs:
            self.projects.set_allocation(alloc["project_id"], day, alloc["count"])
        made, failed = {}, 0
        for place in placements:
            item = place["item"]
            slot = place["slot"]
            project = item["project"]
            url = of.task_url(item["pk"])
            parent_pk = item.get("parent_pk") or place.get("parent", {}).get("pk")
            parent_title = (item.get("parent_title")
                            or place.get("parent", {}).get("title"))
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
            if item.get("local_only"):
                # relate --import 从模板文件 append 的任务在 OmniFocus 里没有
                # 真 primary key：不能挂 omnifocus，否则完成时会拿假 pk 写回。
                source["local_only"] = True
                source["url"] = ""
                url = ""
            if parent_pk:
                source.update({"parent_id": parent_pk,
                               "parent_title": parent_title})
            existing = self._project_local_task(item["project_id"], item["pk"])
            try:
                if (existing is not None and ts.TaskStore.is_open(existing)
                        and not existing.get("session")):
                    # 项目导入时已经把叶子任务镜像进任务库了：plan 唤起时复用
                    # 同一条，并把这次分到的 count / duration / 父任务来源写上去。
                    task = self.task_store.apply_project_plan(
                        existing["id"], fields, source=source)
                else:
                    task = self.task_store.create(
                        fields,
                        omnifocus=None if item.get("local_only") else {
                            "id": item["pk"], "url": url,
                            "title": item.get("title")},
                        source=source)
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」没建成本地任务：%s" % (item.get("title"), e), "error")
                continue
            made.setdefault(item["project_id"], []).append({
                "pk": item["pk"], "task_id": task["id"],
                "count": int(place["count"]), "start": slot.get("start") or "",
                "duration": int(place.get("duration")
                                or int(place["count"]) * pp.MINUTES_PER_COUNT),
                "parent_pk": parent_pk or ""})
            try:
                self._assign_task(task, str(slot["index"]), refresh=False,
                                  allow_unbooked=True)
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」建好了但没挂到 session #%s：%s"
                         % (item.get("title"), slot["index"], e), "warn")
        for pid, records in made.items():
            self.projects.record_items(pid, day, records)
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
        self.invalidate()
        return {"placements": len(placements), "counts": total_counts,
                "unscheduled": len(unscheduled), "notes": notes, "failed": failed,
                "used_slot_indices": {p["slot"]["index"] for p in placements}}

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
