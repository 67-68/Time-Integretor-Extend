"""warden_core.board_mixin —— 看板数据合并（rebuild_rows）、表头折叠、表格/状态框/帮助面板渲染与警告收集。"""
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from .bridge import focusmate_home
from .common import (
    FRESH_SESSION,
    FRESH_TASK,
    Group,
    MESSAGE_KEEP,
    MESSAGE_SHOW,
    Panel,
    SESSION_TABLE_COLUMNS,
    SESSION_UI_MODES,
    STYLE_ACTIVE,
    STYLE_ASSIGNED,
    STYLE_DONE,
    STYLE_DROPPED,
    STYLE_MUTED,
    TASK_AUTO_HIDDEN_COLUMNS,
    TASK_CELL_TITLE_MAX,
    TASK_INDEX_MAX_COLS,
    TASK_PANEL_BANDS,
    TASK_TABLE_COLUMNS,
    TASK_UI_MODE,
    TEMP,
    Table,
    Text,
    UI_PREF_MODES,
    WORKING_AUTO_HIDDEN_COLUMNS,
    box,
    cfg,
    fmt_age,
    format_compact_timestamp,
    get_warden_global,
    ks,
    normalize_session_column,
    normalize_task_column,
    now_local,
    pj,
    psv,
    repl_kit,
    sch,
    sp,
    st,
    task_dynamic_keys,
    task_table_headers,
    th,
    ts,
    visible_session_rows,
)
from .hints import mute_window_for
from .views import ChoreView, ContractView, FocusView, StatsView, TaskView


class BoardMixin(object):
    # -- 新鲜度：这份数据是什么时候来的 --------------------------------

    def _age_text(self, stamp, limit_ok=True):
        """epoch → 「12 秒前」/「6 分钟前」/「还没拉过」。"""
        if not stamp:
            return "还没拉过"
        seconds = max(0, int(time.time() - stamp))
        if seconds < 90:
            return "%d 秒前" % seconds
        if seconds < 5400:
            return "%d 分钟前" % int(seconds / 60)
        return "%d 小时前" % int(seconds / 3600)

    def freshness_rows(self):
        """状态框里的「数据是什么时候来的」。

        用户要求：**最近的就不吵**（灰的），**不是最近的红色 + 加粗** ——
        「这份 session 列表是 8 分钟前的」必须一眼看得出来，不然你会拿旧账
        去做「现在几点了」的决定。
        """
        now = time.time()
        pieces = []
        stale = []
        for label, key, limit in (("session", "session", FRESH_SESSION),
                                  ("task", "task", FRESH_TASK)):
            stamp = self.read_at.get(key) or 0
            text = self._age_text(stamp)
            pieces.append("%s %s" % (label, text))
            if not stamp or (now - stamp) > limit:
                stale.append(label)
        if "session" not in self.read_at and self.rows_source == "snapshot":
            return [("数据：session 列表来自 state.json 快照（不是真数据）→ refresh",
                     "bold red")]
        if stale:
            return [("数据（⚠ %s 不是最近的）：%s" % ("、".join(stale), " · ".join(pieces)),
                     "bold red")]
        return [("数据：%s" % " · ".join(pieces), "dim")]

    def frame_signature(self):
        """画帧缓存要看的那些「便宜的事实」（贵的东西留在 key 之外）。

        只放**不求值就便宜**的东西：长度、身份、指纹、警告签名。任何一项变了
        就重排版。`messages` 取最后一条（带秒级时间戳），所以 Recent 不会滞后。
        """
        msgs = self.messages or ()
        pending_import = self.pending_import
        pending_sig = None
        if pending_import:
            pending_sig = (
                tuple(str((item or {}).get("id") or (item or {}).get("title") or "")
                      for item in pending_import.get("items") or ()),
                tuple(sorted((pending_import.get("repeats") or {}).keys())),
            )
        return (
            len(msgs), msgs[-1] if msgs else (),
            len(self.rows), id(self.rows),
            self.rows_note or "", self.rows_source or "",
            self.bridge_note or "", bool(self.had_error),
            self.csv_fp, len(self.entries),
            self.review_signature(), pending_sig,
            self.sel_date.isoformat(), self.day_offset, self.session_ui_mode,
            getattr(self, "data_view_model", "task"),
            getattr(self, "data_filter", "disposable"),
            tuple(sorted(self.effective_hidden_columns())),
            tuple(sorted(self.effective_task_hidden_columns(self.task_store.list()))),
            self.task_view_page, self.task_sort_key, self.task_sort_desc,
            self.task_group_by, self.task_search_query,
            isinstance(self.view, ChoreView), self.chore_index,
            self.chore_last_note,
            id(getattr(self, "stat", None)),
            id(getattr(self, "help_overlay", None)),
            (id(self.planning), getattr(self.planning, "stage", None),
             tuple((a.get("project_id"), a.get("count"))
                   for a in (getattr(self.planning, "allocations", None) or ())),
             getattr(self.planning, "closed", None)) if self.planning else None,
            tuple(self.pending_op_type or ()),
            tuple(sorted((self.pending_advance or {}).items())) if self.pending_advance else None,
            # 队列一动（进度条、日志、失败账）画面就得重排版
            self.jobs.signature(),
        )

    # -- 表头折叠 -------------------------------------------------------

    def ui_prefs_path(self):
        """列折叠偏好落在设置文件同目录（测试跟着 TI_SETTINGS_FILE 隔离）。"""
        return os.path.join(os.path.dirname(os.path.abspath(cfg.settings_path())),
                            "warden-ui.json")

    def _load_ui_prefs(self):
        try:
            with open(self.ui_prefs_path(), encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (OSError, ValueError, TypeError):
            return
        for section, target in (("hidden", self.permanent_hidden_columns),
                                ("visible", self.permanent_visible_columns)):
            data = raw.get(section) or {}
            if not isinstance(data, dict):
                continue
            for mode in UI_PREF_MODES:
                names = data.get(mode) or []
                if not isinstance(names, list):
                    continue
                for name in names:
                    if mode == TASK_UI_MODE:
                        col = normalize_task_column(name) or str(name).strip()
                    else:
                        col = normalize_session_column(name)
                    if col:
                        target[mode].add(col)

    def _save_ui_prefs(self):
        path = self.ui_prefs_path()
        data = {
            "hidden": {m: sorted(self.permanent_hidden_columns[m])
                       for m in UI_PREF_MODES},
            "visible": {m: sorted(self.permanent_visible_columns[m])
                        for m in UI_PREF_MODES},
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except OSError as e:
            self.say("列折叠偏好没存下来：%s" % e, "warn")

    def auto_hidden_columns(self, mode=None):
        mode = mode or self.session_ui_mode
        if mode == "working":
            return set(WORKING_AUTO_HIDDEN_COLUMNS)
        return set()

    def effective_hidden_columns(self, mode=None):
        """当前口径实际不展示的列（自动 + 永久 + 本次 close - 手动 open）。"""
        mode = mode or self.session_ui_mode
        hidden = set(self.auto_hidden_columns(mode))
        hidden |= self.permanent_hidden_columns.get(mode, set())
        hidden |= self.session_hidden_columns.get(mode, set())
        hidden -= self.permanent_visible_columns.get(mode, set())
        hidden -= self.session_visible_columns.get(mode, set())
        return {col for col in hidden
                if any(col == header for header, _kw in SESSION_TABLE_COLUMNS)}

    def _set_column_open(self, col, permanent=False, mode=None):
        mode = mode or self.session_ui_mode
        self.session_hidden_columns[mode].discard(col)
        self.session_visible_columns[mode].add(col)
        if permanent:
            self.permanent_hidden_columns[mode].discard(col)
            self.permanent_visible_columns[mode].add(col)
            self._save_ui_prefs()

    def _set_column_closed(self, col, permanent=False, mode=None):
        mode = mode or self.session_ui_mode
        self.session_visible_columns[mode].discard(col)
        self.session_hidden_columns[mode].add(col)
        if permanent:
            self.permanent_visible_columns[mode].discard(col)
            self.permanent_hidden_columns[mode].add(col)
            self._save_ui_prefs()

    def effective_task_hidden_columns(self, tasks=None):
        """任务表当前不展示的任务表头（默认收敛 + 永久 + 本次 close - 手动 open）。

        ``tasks=None`` 时不读任务库，只返回偏好里记的列名（frame signature 用）；
        渲染和 close 校验会传入当前任务，于是还能过滤掉已经不存在的动态字段。
        """
        headers = set(task_table_headers(tasks)) if tasks is not None else None
        hidden = set(TASK_AUTO_HIDDEN_COLUMNS)
        if tasks is not None:
            # 自定义字段默认也收进「备注」：它们不是每张任务都有的固定列，
            # 自动铺成列会挤掉 title/session/截止，宽屏上反而更早出现省略号。
            # 需要单独看时仍可 `open <字段>`。
            fixed = {header for header, _kw in TASK_TABLE_COLUMNS}
            hidden |= {key for key in task_dynamic_keys(tasks) if key not in fixed}
        hidden |= self.permanent_hidden_columns.get(TASK_UI_MODE, set())
        hidden |= self.session_hidden_columns.get(TASK_UI_MODE, set())
        hidden -= self.permanent_visible_columns.get(TASK_UI_MODE, set())
        hidden -= self.session_visible_columns.get(TASK_UI_MODE, set())
        return {col for col in hidden if headers is None or col in headers}

    def task_flags_text(self, task):
        """任务表 boolean flags 专列（``标记`` 列）：沿用契约界面记号并补齐。

        🔁 = 重复任务子任务（🔁* = 重复任务模板本体）
        🔗 = 关联 OmniFocus
        🎯 = 关联 Focusmate task（⏳ = 异步挂载在途）
        ⏱ = 挂了 formatter
        ⚡ = 挂了 trigger
        🔒 = 独占场次（exclusive）
        """
        marks = ""
        if ts.TaskStore.is_repeat_template(task):
            marks += "🔁*"
        elif (task or {}).get("repeat_parent"):
            marks += "🔁"
        if self.task_project_name(task):
            marks += "📁"
        if ts.TaskStore.omnifocus_id(task):
            marks += "🔗"
        if self._fm_pending(task):
            marks += "⏳"
        elif ((task or {}).get("focusmate") or {}).get("task_id"):
            marks += "🎯"
        if (task or {}).get("formatter"):
            marks += "⏱"
        if (task or {}).get("trigger"):
            marks += "⚡"
        if th.is_exclusive(task):
            marks += "🔒"
        return marks or "·"

    def task_project_name(self, task):
        """任务所属项目名称（优先 ``fields.project``，其次项目库匹配）。"""
        fields = (task or {}).get("fields") or {}
        proj = str(fields.get(pj.PROJECT_FIELD) or fields.get("项目") or "").strip()
        if proj:
            return proj
        source = (task or {}).get("source") or {}
        if str(source.get("kind") or "") == "project":
            src_title = str(source.get("project_title") or "").strip()
            if src_title:
                return src_title
        try:
            projects = self.projects.list()
        except Exception:                             # noqa: BLE001
            projects = []
        pid = fields.get(pj.PROJECT_ID_FIELD)
        if pid is not None:
            for p in projects:
                if str(p.get("id")) == str(pid):
                    return str(p.get("title") or "").strip()
        pk = ts.TaskStore.omnifocus_id(task)
        if pk:
            for p in projects:
                for pt in (p.get("tasks") or ()):
                    if str(pt.get("pk") or "") == str(pk):
                        return str(p.get("title") or "").strip()
        return ""

    def task_remark_text(self, task):
        """任务表「备注」列：一行 ``k=v`` 紧凑汇总被折叠字段与备注。"""
        fields = (task or {}).get("fields") or {}
        parts = []
        dur = ts.task_minutes(task)
        if dur is not None:
            parts.append("dur=%dm" % dur)
        cap, inv = self.task_count_scope(task)
        delta = self.task_count_delta(task)
        if cap is not None or inv > 0:
            if cap is not None:
                if delta > 0:
                    parts.append("cnt=%d+%d/%d" % (inv, delta, cap))
                elif inv > 0:
                    parts.append("cnt=%d/%d" % (inv, cap))
                else:
                    parts.append("cnt=%d" % cap)
            else:
                parts.append("cnt=%d" % inv)
        if th.is_exclusive(task):
            parts.append("excl=1")
        tags = (task or {}).get("tags") or []
        if tags:
            parts.append("tag=" + "/".join(str(t) for t in tags))
        rep = (task or {}).get("repeat") or {}
        if rep:
            parts.append("rep=🔁 " + ts.repeat_summary(rep))
        fmt = (task or {}).get("formatter") or {}
        if fmt.get("name"):
            f_txt = str(fmt.get("name"))
            if fmt.get("reservation"):
                f_txt += "@" + str(fmt["reservation"].get("start") or "?")
            parts.append("fmt=" + f_txt)
        trig = (task or {}).get("trigger") or {}
        if trig.get("name"):
            parts.append("trig=" + str(trig.get("name")))
        note = self.task_store.notes(task)
        if note:
            parts.append("note=" + self.task_store._one_line(note))
        skip_keys = (set(ts.DURATION_KEYS) | set(ts.NOTES_KEYS) | set(th.EXCLUSIVE_KEYS)
                     | {"title", "name", pj.PROJECT_FIELD, "项目",
                        pj.PROJECT_ID_FIELD, pj.PROJECT_TASK_FIELD,
                        ts.COUNT_KEY, ts.COUNT_INVESTED_KEY, "due", "due_source"})
        for k, v in fields.items():
            if k in skip_keys:
                continue
            val = self.task_store._one_line(v)
            if val:
                parts.append("%s=%s" % (k, val))
        # 用紧凑分隔符（不带空格）：备注列是固定宽度里信息密度最高的一格，
        # 三个逗号省下的 3 列正好能让 `fmt=enlist` 这类尾项完整显示，而不是
        # 在宽屏上仍被 Rich 裁掉最后一个字符。
        return ",".join(parts)

    def task_group_label(self, task, group_by):
        """分组列显示的标签值。"""
        col = normalize_task_column(group_by, [task]) or str(group_by or "").strip()
        if col == "project":
            return self.task_project_name(task) or "（无项目）"
        if col == "状态":
            return "%s %s" % (self.task_mark(task) or "·", self.task_store.status_of(task))
        if col == "session":
            sess = (task or {}).get("session") or {}
            if not sess:
                return "（未排场次）"
            raw = "%s %s" % (sess.get("date") or "", sess.get("start") or "")
            return format_compact_timestamp(raw.strip(), self.sel_date)
        if col == "截止":
            due = self.omni_due_of(task)
            return format_compact_timestamp(due, self.sel_date) if due else "（无截止）"
        if col == "标签":
            tags = (task or {}).get("tags") or []
            return "·".join(tags) if tags else "（无标签）"
        if col == "标记":
            return self.task_flags_text(task)
        if col == "duration":
            dur = ts.task_minutes(task)
            return ("%dm" % dur) if dur is not None else "（无时长）"
        if col == "count":
            cap = ts.task_count(task)
            return ("cnt=%d" % cap) if cap is not None else "（无 count）"
        fields = (task or {}).get("fields") or {}
        val = self.task_store._one_line(fields.get(col) or fields.get(group_by))
        return val or ("（无 %s）" % col)

    def _task_sort_tuple(self, task, key):
        """返回可比较的排序键元组 ``(是否空值, 排序值, id)``。"""
        col = normalize_task_column(key, [task]) or str(key or "").strip()
        tid = int((task or {}).get("id") or 0)
        if col == "id":
            return (0, tid, tid)
        if col == "序号":
            no = self.task_no(task)
            return (0 if no else 1, no or 999999, tid)
        if col == "状态":
            rank = {ts.STATUS_ACTIVE: 0, ts.STATUS_COMPLETED: 1, ts.STATUS_DROPPED: 2}
            st = self.task_store.status_of(task)
            return (0, rank.get(st, 9), tid)
        if col == "title":
            fields = (task or {}).get("fields") or {}
            t = str(fields.get("title") or fields.get("name") or "").strip().lower()
            return (0 if t else 1, t, tid)
        if col == "project":
            p = self.task_project_name(task).lower()
            return (0 if p else 1, p, tid)
        if col == "session":
            sess = (task or {}).get("session") or {}
            s = ("%s %s" % (sess.get("date") or "", sess.get("start") or "")).strip()
            return (0 if s else 1, s, tid)
        if col == "截止":
            due = self.omni_due_of(task) or ""
            return (0 if due else 1, due, tid)
        if col == "duration":
            dur = ts.task_minutes(task)
            return (0 if dur is not None else 1, dur or 0, tid)
        if col == "count":
            cap = ts.task_count(task)
            return (0 if cap is not None else 1, cap or 0, tid)
        if col == "完成时间":
            fm = (task or {}).get("focusmate") or {}
            done = str(fm.get("completed_at") or (task or {}).get("status_at") or "")
            return (0 if done else 1, done, tid)
        fields = (task or {}).get("fields") or {}
        raw = self.task_store._one_line(fields.get(col) or fields.get(key)).lower()
        return (0 if raw else 1, raw, tid)

    def _task_matches_query(self, task, query):
        """任务是否命中搜索词（支持 ``field=val`` / ``field:val`` 或全文子串匹配）。"""
        tokens = [t.strip() for t in str(query or "").split() if t.strip()]
        if not tokens:
            return True
        fields = (task or {}).get("fields") or {}
        haystack_parts = [
            str((task or {}).get("id") or ""),
            str(self.task_no(task) or ""),
            str(fields.get("title") or fields.get("name") or ""),
            self.task_project_name(task),
            self.task_store.status_of(task),
            self.task_mark(task) or "",
            self.task_flags_text(task),
            self.task_remark_text(task),
            " ".join(str(t) for t in ((task or {}).get("tags") or [])),
            str(ts.TaskStore.omnifocus_id(task) or ""),
        ]
        for k, v in fields.items():
            haystack_parts.append("%s=%s" % (k, self.task_store._one_line(v)))
        haystack = " ".join(haystack_parts).lower()
        for tok in tokens:
            m = re.match(r"^([A-Za-z0-9_一-鿿]+)[:=](.+)$", tok)
            if m:
                k_raw, v_want = m.group(1), m.group(2).strip().lower()
                col = normalize_task_column(k_raw, [task]) or k_raw
                if col == "project":
                    got = self.task_project_name(task).lower()
                elif col == "状态":
                    got = (self.task_store.status_of(task) + " " + (self.task_mark(task) or "")).lower()
                elif col == "标签":
                    got = " ".join((task or {}).get("tags") or []).lower()
                elif col == "id":
                    got = str((task or {}).get("id") or "").lower()
                else:
                    got = self.task_store._one_line(fields.get(col) or fields.get(k_raw)).lower()
                if v_want not in got:
                    return False
            else:
                if tok.lower() not in haystack:
                    return False
        return True

    def filter_and_sort_tasks(self, tasks):
        """按当前搜索、排序和分组规则处理任务列表。"""
        out = list(tasks or [])
        query = getattr(self, "task_search_query", "")
        if query:
            out = [t for t in out if self._task_matches_query(t, query)]
        sort_key = getattr(self, "task_sort_key", None)
        sort_desc = bool(getattr(self, "task_sort_desc", False))
        if sort_key:
            non_empty = [t for t in out if self._task_sort_tuple(t, sort_key)[0] == 0]
            empty = [t for t in out if self._task_sort_tuple(t, sort_key)[0] != 0]
            non_empty.sort(key=lambda t: self._task_sort_tuple(t, sort_key)[1:],
                           reverse=sort_desc)
            out = non_empty + empty
        group_by = getattr(self, "task_group_by", None)
        if group_by:
            # 同一分组聚拢在一起，保留首次出现的分组顺序（或排完序后的顺序），无值的排在末尾
            groups = {}
            order = []
            tail_key = None
            for t in out:
                g = self.task_group_label(t, group_by)
                if g not in groups:
                    groups[g] = []
                    if g.startswith("（无"):
                        tail_key = g
                    else:
                        order.append(g)
                groups[g].append(t)
            if tail_key is not None:
                order.append(tail_key)
            grouped = []
            for g in order:
                grouped.extend(groups[g])
            out = grouped
        return out

    def task_filter_summary(self):
        """表尾 caption 里的当前排序 / 分组 / 搜索提示。"""
        bits = []
        if getattr(self, "task_search_query", ""):
            bits.append("搜索：%s" % self.task_search_query)
        if getattr(self, "task_group_by", None):
            bits.append("分组：grouping(%s)" % self.task_group_by)
        if getattr(self, "task_sort_key", None):
            bits.append("排序：%s %s" % (
                self.task_sort_key, "↓" if self.task_sort_desc else "↑"))
        return " · ".join(bits)

    # -- 选中的那一天

    @property
    def sel_date(self):
        return datetime.date.today() + datetime.timedelta(days=self.day_offset)

    def reload_entries(self):
        """重读 data.csv（每一次操作前都重读：人手可能在别处改了它）。"""
        self.entries, self.entry_warnings = sch.load_entries()
        self.csv_fp = sch.fingerprint()
        return self.entries

    def reload_periods(self):
        """重读日历下载层 + 时段 layout（T1/T2 和每段的 session 起点）。"""
        self.download = sp.load_download()
        self.layouts = sp.load_layouts()
        return self.download

    def periods_for_day(self, day=None):
        return sp.periods_for_date(self.download, day or self.sel_date)

    def period_first_start(self, period):
        """这个时段第一场 session 的 HH:MM；没排出来就是 None。"""
        try:
            sessions = sp.sessions_for_period(period, self.layouts)
        except ValueError:
            return None
        return sessions[0]["start"] if sessions else None

    def tasks_for_session(self, day, start, duration=None):
        """这一场挂着的**所有**任务：本地 assignment 优先，其次是「每周重复」落点。

        一场 session 可以挂多个任务（用户会在已有 task 的场次上再挂一个），
        所以表格里那一格要能显示多条，不能只认第一条。
        """
        tasks = self.task_store.tasks_for_session(day, start)
        # 重复任务的**本体**只当模板，不出现在契约表 task 列；表里只显示
        # 到那一天生成出来的子任务（子任务带 repeat_parent）。
        return [t for t in tasks if not ts.TaskStore.is_repeat_template(t)]

    def task_for_session(self, day, start, duration=None):
        """这一场挂的第一条任务（老接口，保留给只关心一条的调用方）。"""
        tasks = self.tasks_for_session(day, start, duration=duration)
        return tasks[0] if tasks else None

    def attach_period_info(self, row, day=None):
        """给一行表格补上「时段 ID / 时段 / 类型 / 名称 / task」。"""
        day = day or self.sel_date
        period = sp.period_for_session(self.periods_for_day(day), row.get("startLabel"))
        if period:
            row["period"] = dict(period)
            row["period_id"] = period.get("id")
            first = self.period_first_start(period)
            # 需求：同一时段的第一行显示 T1/时段/类型/名称，后续行留空。
            row["period_first"] = bool(first is None or first == row.get("startLabel"))
        else:
            row["period"] = None
            row["period_id"] = None
            row["period_first"] = False
        # 「这一行在不在静音时段里」——口径只有一个：`period mute T1 [--permanent]`
        # 写下的时间窗。quiet 是**结果**（Focusmate 那边设上了没），period_muted
        # 是**意图**；两个都摆出来，才知道是「本来就该静音」还是「该静音没设上」。
        window = mute_window_for(day, row.get("startLabel"), row.get("endLabel"))
        row["period_muted"] = bool(window)
        if window:
            row["mute_window"] = dict(window)
        tasks = self.tasks_for_session(day, row.get("startLabel"), row.get("durationMin"))
        if tasks:
            row["task"] = tasks[0]                # 兼容：只关心一条的地方仍然能用
            row["tasks"] = tasks
            row["task_repeat"] = any(t.get("repeat_parent") for t in tasks)
            row["task_text"] = self.task_cell_text(tasks, row.get("durationMin"))
        else:
            row["task"] = None
            row["tasks"] = []
            row["task_repeat"] = False
            row["task_text"] = ""
        return row

    @staticmethod
    def task_mark(task):
        """任务的状态标记：``·`` 进行中 / ``✓`` 完成 / ``✗`` 放弃 / ``⤼`` 转移走。

        用户要求 session 结束之后「那一场到底干成了什么」仍然看得见，所以
        完成/放弃的任务**不隐藏、不删**，只是前面多一个记号。

        进行中也必须打 ``·``：以前这里返回空串，表格里「没有记号」既可能是
        「还没做」也可能是「这一格压根没画状态」，用户看不出差别（真实反馈：
        「契约界面的 task 部分没有显示 task 状态」）。
        """
        status = ts.TaskStore.status_of(task)
        if status == ts.STATUS_COMPLETED:
            return "✓"
        if status == ts.STATUS_DROPPED:
            return "⤼" if ts.TRANSFER_TAG in (task.get("tags") or []) else "✗"
        return ts.STATUS_MARKS.get(ts.STATUS_ACTIVE) or "·"

    def task_cell_text(self, tasks, session_minutes=None):
        """契约表格 task 列的正文：``1: (1+2/5) 任务… | ✓3: 任务…``。

        标题优先取 title/name 字段（`focusmate_title`），没有才退回整条摘要 ——
        编号 + 一长串 ``1. … 2. …`` 在表格里没法看。前面的记号见 :meth:`task_mark`。

        ``(1+2/5)`` 是 count：已投入 + **这一场会涨的** / 最高。1 是这条任务之前
        已经用掉的 count，2 = 这一场时长 / 25（往上取整）—— 那是预报，真正记账
        发生在收尾（见 :meth:`begin_review`）。没设 count 的任务不显示这一格。
        """
        parts = []
        delta = ts.minutes_to_count(session_minutes) or 0
        for task in tasks or []:
            if ts.TaskStore.is_repeat_template(task):
                continue
            title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
            prefix = self.task_mark(task) + ts.TaskStore.origin_marks(task)
            # 本体的 formatter 只是模板，不能被画成「已经排过」（⏱ 属于子任务）。
            if task.get("formatter") and not ts.TaskStore.is_repeat_template(task):
                prefix += "⏱"
            no = self.task_no(task)
            if session_minutes is not None:
                count = self.task_session_count_label(task, delta)
            else:
                count = self.task_count_label(task, delta)
            parts.append("%s%d: %s%s" % (prefix, no, count, title) if no else
                         "%s%s%s" % (prefix, count, title))
        return " | ".join(parts)

    @staticmethod
    def task_count_delta(task):
        """这条任务「完成这一场之后会自动涨多少 count」= 那一场时长 / 25。

        只对**还开着**、并且挂在某一场 session 上的任务有意义：收工/放弃的任务
        再预报一次涨幅就是在骗人。**已经记过账的那一场也不再预报**（否则收尾记完
        账之后表格里还写着 ``(3+2/5)``，看起来像下一帧还要再涨 2 个）。
        """
        if not task or not ts.TaskStore.is_open(task):
            return 0
        sess = task.get("session") or {}
        delta = ts.minutes_to_count(sess.get("duration")) or 0
        if not delta:
            return 0
        # 项目任务拆段后，本地这条记录只分到一部分 count：这一场即使挂了
        # 50 分钟 session，这段也可能只计划干 1 个 count，预报不能按整场算。
        cap = ts.task_count(task)
        if cap is not None:
            delta = min(delta, cap)
        day = str(sess.get("date") or "").strip()
        if not day:
            return delta
        key = psv.session_key(day, sess.get("start"))
        if key in (task.get("count_credits") or {}):
            return 0
        return delta

    def task_count_scope(self, task):
        """任务在界面上该用的 ``(最高 count, 已投入 count)``。

        普通任务就是自己的 ``count`` / ``count_invested``。项目任务被 plan 拆成
        多个 session part 时，本地每条记录的 ``count`` 只是**这一段**分到的量
        （例如 75 分钟任务拆成 50 + 25，两个 part 的 count 是 2 和 1）；但界面上
        用户要看的是原 OmniFocus 任务的总进度（3），以及所有 part 加起来的实际
        投入。所以：

        * 最高 count 优先用 ``fields.count_total``，旧数据则从项目清单里按
          ``project_id + project_task`` 找回原任务的总 count；
        * 已投入用同一项目任务的本地 part 汇总（完成 part 按它分到的 count，
          进行中 part 按 ``count_invested``）。
        """
        cap = ts.task_count(task)
        invested = ts.task_count_invested(task)
        fields = (task or {}).get("fields") or {}
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        if not pk:
            return cap, invested
        pid = fields.get(pj.PROJECT_ID_FIELD)

        # 旧任务没有 count_total：从项目母体里找回原任务的总 count。
        total = ts.parse_count(fields.get("count_total"))
        if total is None or total <= 0:
            try:
                project = self.projects.by_id(pid) if pid is not None else None
                if project is not None:
                    item = pj.task_by_pk(project, pk)
                    total = ts.parse_count((item or {}).get("count"))
            except Exception:                   # noqa: BLE001 - 展示兜底
                total = None
        if total and total > 0:
            cap = int(total)

        # 同一个 OmniFocus 任务可能有多个本地 part，各自记自己的 count 账。
        try:
            aggregate = self._project_task_count_aggregates()
            key = (str(pid) if pid is not None else "", pk)
            if key in aggregate:
                invested = aggregate[key]
            elif pid is None:
                # 旧数据可能没有 project_id；pk 已经足够定位同一条 OmniFocus 任务。
                invested = sum(value for (other_pid, other_pk), value
                               in aggregate.items() if other_pk == pk)
        except Exception:                       # noqa: BLE001 - 展示兜底
            pass
        return cap, invested

    def _project_task_count_aggregates(self):
        """``{(project_id, project_task_pk): 累计已投入 count}``。

        每个 frame 会为多条任务查这个账；按任务库文件的 mtime/size 缓存一次，
        避免表格每画一行就把整个 tasks.json 重读一遍。
        """
        path = getattr(self.task_store, "path", "") or ""
        try:
            info = os.stat(path)
            stamp = (path, info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = (path, None, None)
        cache = getattr(self, "_project_task_count_cache", None)
        if cache is not None and cache[0] == stamp:
            return cache[1]
        out = {}
        for other in self.task_store.list():
            fields = (other or {}).get("fields") or {}
            pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
            if not pk:
                continue
            pid = fields.get(pj.PROJECT_ID_FIELD)
            key = (str(pid) if pid is not None else "", pk)
            out[key] = out.get(key, 0) + pj.contribution(other)
        self._project_task_count_cache = (stamp, out)
        return out

    @staticmethod
    def _session_order(session):
        session = session or {}
        return (str(session.get("date") or ""), str(session.get("start") or ""))

    def task_session_count_baseline(self, task):
        """session 表格里的「这一场开始前已经投入了几个 count」。

        和任务面板不同，session 表格是历史行：一场 25 分钟干了 1 个 count，
        之后再来第二场 50 分钟干活 2 个。第一行应该一直显示 ``(0+1/3)``，
        第二行显示 ``(1+2/3)``；不能拿“现在总共 3”去回填第一行，变成
        ``(2+1/3)`` 或 ``(3+1/3)``。

        基线按场次时间排序累计：只算时间更早的 part，当前 part 自己的
        ``count_invested`` 要减掉这一场已经记进去的那一笔。
        """
        fields = (task or {}).get("fields") or {}
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        sess = (task or {}).get("session") or {}
        current_key = psv.session_key(sess.get("date"), sess.get("start"))
        own_credit = ((task or {}).get("count_credits") or {}).get(current_key)
        try:
            own_credit = int(own_credit) if own_credit is not None else None
        except (TypeError, ValueError):
            own_credit = None
        if not pk:
            invested = ts.task_count_invested(task)
            if own_credit is not None:
                invested = max(0, invested - own_credit)
            return invested

        pid = fields.get(pj.PROJECT_ID_FIELD)
        current_order = self._session_order(sess)
        baseline = 0
        try:
            for other in self.task_store.list():
                other_fields = (other or {}).get("fields") or {}
                if str(other_fields.get(pj.PROJECT_TASK_FIELD) or "").strip() != pk:
                    continue
                other_pid = other_fields.get(pj.PROJECT_ID_FIELD)
                if (pid is not None and other_pid is not None
                        and str(other_pid) != str(pid)):
                    continue
                if other.get("id") == task.get("id"):
                    invested = ts.task_count_invested(other)
                    if own_credit is not None:
                        invested = max(0, invested - own_credit)
                    baseline += invested
                    continue
                other_sess = other.get("session") or {}
                # 没有 session 的历史完成记录没有可比较的时间点，保守算入；
                # 有 session 的只算严格更早的场次。
                if not other_sess or self._session_order(other_sess) < current_order:
                    baseline += pj.contribution(other)
        except Exception:                       # noqa: BLE001 - 展示兜底
            return max(0, ts.task_count_invested(task) - (own_credit or 0))
        return baseline

    def task_session_count_label(self, task, delta):
        """session 表格里那截 count：``(这一场开始前 + 这一场 / 总 count)``。"""
        cap, _aggregate = self.task_count_scope(task)
        invested = self.task_session_count_baseline(task)
        text = ts.count_text(task, delta, cap=cap, invested=invested)
        return (text + " ") if text else ""

    def task_count_label(self, task, delta=None):
        """任务格子里那截 ``(1+2/5) ``（带尾随空格）；没设 count 就是空串。"""
        if delta is None:
            delta = self.task_count_delta(task)
        cap, invested = self.task_count_scope(task)
        text = ts.count_text(task, delta, cap=cap, invested=invested)
        return (text + " ") if text else ""

    def frame_width(self):
        """这一帧能占多少列（= 终端宽度，和 repl_kit 排「状态」框用的是同一个数）。

        抽成一个方法是为了测试能钉住它：「几列并排」「哪几行被裁」这种断言不能
        跟着「现在窗口开多大」乱跳。
        """
        width = getattr(self.console, "width", None)
        if getattr(getattr(self, "shell", None), "_in_frame_text", False):
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:                         # noqa: BLE001
                cols = width if isinstance(width, int) else 80
        else:
            try:
                cols = os.get_terminal_size().columns
            except Exception:                         # noqa: BLE001
                cols = width if isinstance(width, int) else 80
        if not isinstance(cols, int) or cols < 24:
            cols = width if isinstance(width, int) and width >= 24 else 80
        return cols

    def task_panel_tasks(self, day=None):
        """这一天任务面板里该出现哪些任务 —— 用户定的两条规矩。

        * **今天（以及以后）**：没挂 session 的、以及**挂在这一天**的没做完任务；
          再加上今天被处理过的。
        * **过去那天**：固化，只剩「那一天的 session 里被处理过的」。

        关键两条：
          * **重复任务本体只是模板**，不进【契约】的任务面板（也不进 task 列）；
            它只在「任务」视图里用 `id:N` 管理。
          * **已挂到别的日期的任务属于那一天，不属于今天**。明天 22:15 的任务
            今天不出现在面板里（切到 `day +1` 才出现）—— 否则今天的序号里混着
            明天的活，按面板序号敲命令就会打到错的账上。

        「被处理过」不挑结果：放弃 / 完成 / 补录 / 重议 / 推后 / 转移走 / 跳过
        都算一笔，痕迹记在 ``task["history"]``（``TaskStore.mark_processed``）。
        跳过也要记：那一天你问过它、你说先放着 —— 这是那一天发生过的事。

        还有一条兜底：**挂在这一天某场 session 上的任务一律进面板**。契约表格那一格
        写着它，面板上却没有它的序号 —— 那两处就对不上了（用户按面板序号敲命令，
        看到的却是「这一条没号」）。
        """
        day = day or self.sel_date
        iso = day.isoformat()
        is_today_or_later = day >= datetime.date.today()
        open_tasks = []
        touched = []
        for task in self.task_store.list():
            # 重复任务**本体**只是模板：它不出现在【契约】的任务面板 / task 列，
            # 只在「任务」视图里用 id:N 管理（选它没有意义：它不是一次活）。
            if ts.TaskStore.is_repeat_template(task):
                continue
            records = self.task_store.processed_on(task, iso)
            sess = task.get("session") or {}
            sess_day = sess.get("date") or ""
            attached_today = sess_day == iso
            # 项目库镜像进来的「备选叶子任务」（还没被 plan 唤起）：只给任务视图
            # 搜索/分组用，不占契约面板；一旦挂了 session 或当天处理过就走正常路径。
            if ts.TaskStore.is_project_backlog(task) and not records and not attached_today:
                continue
            if self.task_store.is_open(task):
                # 已经排到某一天的任务只在那一天的面板里出现（除非那天处理过它，
                # 那是历史账）。没挂 session 的才是「随时可干」的待办。
                if sess_day and not attached_today and not records:
                    continue
                if is_today_or_later or records or attached_today:
                    open_tasks.append(task)
            elif records or attached_today:
                touched.append(task)
        return open_tasks + touched

    def task_panel_order(self, day=None):
        """任务面板的**排序** —— 一张表，但同类的连号排在一起。

        需求两条：
          * 「每次 refresh 之后重排序，不然看着一大堆 32、15 的 task 混杂」
          * 「放弃的任务和待办任务还是放在一起，没有 grouping」

        所以排序有**两个维度**，先状态、后位置：

          1. **状态段**：进行中 → 完成 → 放弃（``TASK_PANEL_BANDS``）。
             待办归待办、收工归收工，不再 15(待办) / 33(放弃) / 38(放弃) 混着。
          2. **位置**（段内）：未挂 session 的最前，之后按场次时间聚成段，
             同一场的那几条挨在一起。
          3. 段内最后按任务 ID 升序：同一场上的几条是「一起干」的，编号顺序最好认，
             也最稳定（不会因为一次 refresh 就换位置）。

        这是**排序**不是**拆表**：面板还是那一张表，只是同类连号。
        """
        rank = {status: i for i, status in enumerate(TASK_PANEL_BANDS)}

        def key(task):
            band = rank.get(ts.TaskStore.status_of(task), len(rank))
            sess = task.get("session") or {}
            if not sess:
                return (band, 0, "", "", task.get("id") or 0)
            return (band, 1, sess.get("date") or "", sess.get("start") or "",
                    task.get("id") or 0)

        return sorted(self.task_panel_tasks(day), key=key)

    # -- 任务序号（面板上的第几条）--------------------------------------

    def task_number_index(self, day=None):
        """当前面板的「序号 → 任务」+ 反查表 ``{内部 id: 序号}``。

        需求：「任务重置他们的序号，根据当前任务的数量从 1 到最多」。所以序号**不是**
        内部 id，而是这条任务在面板上是**第几条**（1 起、连续、不跳号）。口径就是
        :meth:`task_panel_order`（先状态分段、段内按位置），所以「序号」和「你看到的
        那块面板」永远是同一份东西 —— 见 1 就是那一行里的第 1 条。

        每读一次库就重算一趟太浪费（画一帧要问好几次），所以按「库文件 mtime + 哪一天」
        存一份：库一写（增删、状态变了）、天一切，缓存自己就作废。
        """
        day = day or self.sel_date
        try:
            info = os.stat(self.task_store.path)
            stamp = (day.isoformat(), info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = (day.isoformat(), 0, 0)
        memo = getattr(self, "_number_memo", None)
        if memo is not None and memo[0] == stamp:
            return memo[1], memo[2]
        tasks = self.task_panel_order(day)
        by_no = {}
        no_of = {}
        for i, task in enumerate(tasks, 1):
            by_no[i] = task
            no_of[task.get("id")] = i
        self._number_memo = (stamp, by_no, no_of)
        return by_no, no_of

    def task_no(self, task, day=None):
        """这条任务在**当前面板**上是第几条（不在面板上 → None）。"""
        if not isinstance(task, dict):
            return None
        return self.task_number_index(day)[1].get(task.get("id"))

    def task_label(self, task, day=None):
        """给用户看的编号：面板上是 ``#3``；不在面板上就老实写内部 id。

        面板上**没有**这条（别的天的收工记录、归档里的）就别编个号给它 —— 编号一
        编，用户敲下去要么打偏要么报错，不如直接写 ``id 15``。
        """
        no = self.task_no(task, day)
        if no:
            return "#%d" % no
        ident = task.get("id") if isinstance(task, dict) else task
        return "id %s" % ident

    def resolve_task(self, token, day=None):
        """把用户敲的那个数解析成任务：**面板上的第几条**。

        两种写法：
          * ``3``     —— 面板上的第 3 条（正常用法）
          * ``id:15`` —— 明说「按内部 id」，给 `task list` / 归档里那些**不在面板上**
                         的任务留的口子（切不了天又必须处理它时用）

        面板上找不到、而这个数又正好是某条任务的内部 id 时也照收，并明说一句是按 id
        认的。两头都不沾边就报清楚：面板现在只有 #1…#几。
        """
        text = str(token).strip()
        if text.lower().startswith("id:"):
            return self.task_store.get(text[3:])
        try:
            want = int(text)
        except (TypeError, ValueError):
            raise ValueError("任务序号得是数字；你给的是 %r" % (token,)) from None
        day = day or self.sel_date
        by_no, _no_of = self.task_number_index(day)
        task = by_no.get(want)
        if task is not None:
            return task
        for candidate in self.task_store.list():
            if candidate.get("id") == want:
                self.say("（%d 超过面板的 #1–#%d，按内部 id 认：%s）"
                         % (want, len(by_no),
                            self.task_store.focusmate_title(candidate, 20)), "warn")
                return candidate
        raise ValueError("面板上现在只有 #1–#%d，没有 #%d（切天看别的日子，"
                         "或 `task list` 看全部）" % (len(by_no), want))

    def task_index_title(self, task):
        """任务面板里那一格的字：状态 + 来源记号 + count 进度 + 裁过的标题。"""
        title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
        prefix = self.task_mark(task) + ts.TaskStore.origin_marks(task)
        # 本体的 formatter 是给子任务的模板；⏱ 只画在真正排过的子任务上。
        if task.get("formatter") and not ts.TaskStore.is_repeat_template(task):
            prefix += "⏱"
        if th.is_exclusive(task) and not ts.TaskStore.is_repeat_template(task):
            prefix += "🔒"
        if self._fm_pending(task):
            # 乐观挂载还在后台跑：⏳ 就是「这条还没落地，别当它是事实」
            prefix += "⏳"
        # count 进度和 session 表格里的写法**一模一样**：(1+2/5)
        return prefix + self.task_count_label(task) + title

    def task_index_layout(self, tasks, width=None):
        """``(几对并排, 几行)``：屏幕够宽就摊成 3 对、4 对，装不下回来 2 对。

        需求：不该永远只有两列 —— 宽屏上白白浪费半屏，任务一多还得翻。
        一「对」的宽度 = 编号列 + 1 空格 + 标题列，两对之间 rich 再给 2 列 padding。

        填法是**一行一行**（左→右、上→下），不是一列一列。以前按列填，四列的
        宽屏上一行读过去是 15 / 39 / 42 / 28，看着就是「序号很乱」；按行填之后
        一行里的编号是递增的，同一段的连号也在同一行上并排。
        """
        if not tasks:
            return 1, 0
        width = width or self.frame_width()
        num_w = max(len(str(t.get("id"))) for t in tasks)
        title_w = max(repl_kit.display_width(self.task_index_title(t)) for t in tasks)
        pair_w = num_w + 1 + title_w
        cols = 1
        for n in range(min(TASK_INDEX_MAX_COLS, len(tasks)), 1, -1):
            if n * pair_w + (n - 1) * 2 <= width - 2:
                cols = n
                break
        rows = (len(tasks) + cols - 1) // cols
        cols = (len(tasks) + rows - 1) // rows        # 有余数时别留空列
        return cols, rows

    def render_task_index(self):
        """契约视图里那块「任务面板」：编号+任务 并排，列数看屏幕宽度。

        内容按天决定（见 :meth:`task_panel_tasks`）：今天 = 没挂 session 的 +
        挂今天 session 的 + 今天处理过的（排到明天的今天不出现），过去那天 =
        只有那天处理过的。**顺序**见 :meth:`task_panel_order`：先按状态
        分段（进行中 → 完成 → 放弃），段内未挂 session 最前、之后一场一段、段内按编号。
        还是这一张表，只是同类的连号挨在一起；格子是**一行一行**填的，所以一行
        从左读到右编号是递增的。配色见文件头那段「配色」。
        """
        tasks = self.task_panel_order()
        if not tasks:
            return None
        cols, rows = self.task_index_layout(tasks)
        t = Table(title="Task · 任务（· 进行中 / ✓ 完成 / ✗ 放弃 / ⤼ 转移走；"
                        "[bright_red]红[/] = 已挂 session）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        for _ in range(cols):
            t.add_column("number", justify="left", no_wrap=True)
            t.add_column("task", no_wrap=True, overflow="crop")
        selected = self.selected_task_id
        for i in range(rows):
            chunk = tasks[i * cols:(i + 1) * cols]      # 一行就是连续的几对
            cells = []
            for task in chunk + [None] * (cols - len(chunk)):
                cells.extend(self._task_index_cells(task, selected))
            t.add_row(*cells)
        # 表尾一行账：这张面板里到底几条收工了 —— 光看记号容易被标题淹掉。
        counts = {ts.STATUS_ACTIVE: 0, ts.STATUS_COMPLETED: 0, ts.STATUS_DROPPED: 0}
        for task in tasks:
            counts[ts.TaskStore.status_of(task)] = counts.get(
                ts.TaskStore.status_of(task), 0) + 1
        scope = ("今天 = 没挂 session 的 + 挂今天的 + 今天处理过的"
                 if self.sel_date >= datetime.date.today()
                 else "%s = 那天处理过的（过去就固化）" % self.sel_date.isoformat())
        t.caption = ("%d 条：%d 进行中 · %d 完成 · %d 放弃 "
                     "（收工的不删，只打记号 · %s）"
                     % (len(tasks), counts[ts.STATUS_ACTIVE],
                        counts[ts.STATUS_COMPLETED], counts[ts.STATUS_DROPPED], scope))
        return t

    def _task_index_cells(self, task, selected):
        """一行里的一对（编号, 标题）。没任务时给两个空格占位。"""
        if task is None:
            return [Text(""), Text("")]
        status = ts.TaskStore.status_of(task)
        style = STYLE_ASSIGNED if task.get("session") else STYLE_ACTIVE
        if status == ts.STATUS_DROPPED:
            style = STYLE_DROPPED
        if status == ts.STATUS_COMPLETED:
            style = STYLE_DONE
        if task.get("id") == selected:
            style += " reverse"
        return [Text(str(self.task_no(task) or "?"), style=style),
                Text(self.task_index_title(task), style=style)]

    def plan_rows_for(self, day):
        return sch.day_slots(self.entries, day)

    def pending_sins(self):
        """还没认罪的罪（daemon 在你手动取消时记下的）。有它 = 全局锁死。"""
        return ks.load_sins()["sins"]

    @property
    def authoritative(self):
        """表格里的数据是不是真数据（bridge 拉的）。快照/离线 = 不敢下"订没订"的结论。"""
        return self.rows_source == "bridge"

    # -- 输出

    def say(self, text, kind="info"):
        stamp = now_local().strftime("%H:%M:%S")
        self.messages.append((stamp, kind, text))
        del self.messages[:-MESSAGE_KEEP]
        self.invalidate()

    def _is_fullscreen_tty(self):
        """当前是否处在交互式全屏单页模式（Alt-Screen，不留历史）。"""
        return bool(not self.no_clear
                    and getattr(self.shell, "alt_screen", False)
                    and sys.stdin.isatty() and sys.stdout.isatty())

    def _inline_shell_output(self, *objects, **kwargs):
        """把框架直接 print 的输出搬进 Recent（全屏模式下不能写 stdout）。"""
        parts = []
        for obj in objects:
            if isinstance(obj, Text):
                text = obj.plain
            elif isinstance(obj, str):
                try:
                    text = Text.from_markup(obj).plain
                except Exception:                     # noqa: BLE001 - 不是 markup 就原样
                    text = obj
            else:
                try:
                    text = Text.from_markup(str(obj)).plain
                except Exception:                     # noqa: BLE001
                    text = str(obj)
            if text:
                parts.append(text)
        message = " ".join(parts).strip()
        if message:
            self.say(message, "warn")
        self.invalidate()

    def emit(self, renderable=None):
        """帧外打印：帮助面板 / 宣言闸门 / `>>>` 回显。

        在 `--exec` / 测试 / `--no-clear` 下直接往 console.print 打字；
        在全屏单页模式下不往 stdout 留滚动历史（面板改由帧内栏位承接）。
        """
        if self._is_fullscreen_tty():
            return
        if renderable is None:
            self.console.print()
        else:
            self.console.print(renderable)

    # -- 看板（框架把它画在提示行上方）---------------------------------

    def frame(self):
        """当前视图的整块看板。repl_kit 每帧调它一次，按单元格 diff 上屏。"""
        # 后台跑完的网络操作在这儿搬回主线程（成功落定 / 失败回滚）
        self.drain_net()
        # 每次画之前重读 data.csv：契约可能在别处被人手改了（这就是控制面）
        self.reload_entries()
        self.maybe_generate_repeat_occurrences()
        self.refresh_warnings()
        # 琐事屏是整屏内容：正常看板、Recent、状态框、视图栏都让位。
        if isinstance(self.view, ChoreView):
            return self.view.render()
        parts = [self.render_header()]
        body = self.view.render()
        if body is not None:
            parts.extend(body if isinstance(body, (list, tuple)) else [body])
        if self.messages:
            parts.append(self.render_messages())
        # 视图栏**不**放这儿：它是 repl_kit 的 footer（见 shell_kwargs）。
        # 看板是会被裁掉顶部的那一块，而「你在第几个视图」任何时候都得看得见。
        return Group(*parts)

    def paint(self, prompt_row=True):
        """画一整帧到 console：--status / --exec / 测试用。

        交互模式下不走这里，走 repl_kit 的 chrome（框架会把「状态」框和看板
        一起拼出来）。这里手动补上同一块框，保证 --status 看到的和交互时一样。
        """
        self.drain_net()
        try:
            self._maybe_sync_chore_mode()
        except Exception:                          # noqa: BLE001
            pass
        box = self.shell.status_box()
        if box is not None:
            self.console.print(box.renderable())
        self.console.print(self.frame())
        # 打断栏位（收尾/查账那句问话）：和交互模式同一位置 —— 看板下面、状态行上面
        block = self.shell.interrogate()
        if block is not None:
            self.console.print(block.renderable())
        # 底部状态栏（视图栏）：栏位下面、状态行上面 —— 和交互模式同一顺序
        self.console.print(self.render_view_bar())
        st = self.live_status()
        if st is not None:
            self.console.print(Text(st))

    def live_status(self):
        """当前视图的动态状态行（截到控制台宽度，纯文本）；没有就 None。"""
        st = self.view.status_line()
        if not st:
            return None
        return st[: max(10, self.console.width - 1)]

    def prompt_text(self):
        """欠着罪的时候提示符换成「宣言> 」—— 那时候只受理宣言。"""
        return "宣言> " if self.pending_sins() else None

    def render_view_bar(self):
        """看板口径 + 「当前在第几个视图」，当前项都染色。琐事屏不显示。"""
        if isinstance(self.view, ChoreView):
            return None
        bar = Text()
        for mode in SESSION_UI_MODES:
            if mode != SESSION_UI_MODES[0]:
                bar.append(" | ", style="dim")
            style = "bold reverse cyan" if self.session_ui_mode == mode else "dim"
            bar.append(mode, style=style)
        bar.append("    视图 ", style="dim")
        for i, v in enumerate(self.view_impls):
            if i:
                bar.append("  ", style="dim")
            style = "bold reverse cyan" if i == self.shell.view_index else "dim"
            bar.append("%d %s" % (i + 1, v.name), style=style)
        bar.append("    （Shift+Tab 切换）", style="dim")
        return bar

    # -- 视图（业务侧的对象；框架侧在 self.shell.views）------------------

    @property
    def views(self):
        return self.view_impls

    @property
    def view(self):
        return self.view_impls[self.shell.view_index]

    @property
    def view_index(self):
        return self.shell.view_index

    def cycle_view(self, step=1):
        """切到下一个视图。返回新视图。"""
        if not self.view_impls:
            return None
        self.shell.switch_view(step)
        self.say("切到「%s」视图" % self.view.name, "cmd")
        self.enter_view()
        return self.view

    def go_to_view(self, cls):
        """直接跳到某个视图（stat 命令用）。已经是它就原地不动。返回该视图。"""
        for i, v in enumerate(self.view_impls):
            if isinstance(v, cls):
                self.shell.set_view(i)
                self.enter_view()
                return v
        return None

    def enter_view(self):
        """进视图时的懒加载：统计拉数；任务视图第一次进时同步完成时间。"""
        if isinstance(self.view, StatsView) and self.stat is None:
            self.cmd_stat()
        elif isinstance(self.view, TaskView) and not self.offline:
            has_fm = any((t.get("focusmate") or {}).get("task_id")
                         for t in self.task_store.list())
            if has_fm and time.time() - self.task_sync_at > 60:
                try:
                    self.cmd_task_sync(quiet=True)
                except Exception as e:                    # noqa: BLE001
                    self.say("任务完成时间同步失败（不影响本地任务）：%s" % e, "warn")

    def cmd_stat(self, force=False, full=False):
        """拉生涯统计（**读**操作，后台跑）：stats.load 走缓存 + 增量刷新。

        - 默认：TTL 内直接用缓存，不打网络；过期只拉缺口 + 最近几天。
        - force：立刻增量刷新（``stat refresh``）。
        - full：丢掉缓存全量重拉（``stat refresh full``，很少需要）。

        拿到历史之后算数字在主线程做（纯 CPU，见 :meth:`_apply_stat_history`）。
        """
        action = self.net_action("stat.history", "拉生涯统计",
                                 {"force": bool(force), "full": bool(full)})
        job = self.jobs.submit(action, persist=False,
                               on_fail=lambda error: self._stat_failed(error))
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        if self.stat is None:
            self.stat_note = "正在后台拉（进度看面板）…"
            self.say("生涯统计在后台拉（进度看面板）；拉完数字会自己出来。", "cmd")
        return True

    def _stat_failed(self, error):
        self.had_error = True
        self.stat_note = "拉取失败：%s" % error
        self.say("生涯统计拉取失败：%s" % error, "error")

    def cycle_focus_mode(self):
        """Shift+Tab：只在专注视图生效，循环模式。返回新模式 key 或 None。"""
        if not isinstance(self.view, FocusView):
            self.say("Shift+Tab 切专注模式只在「专注」视图里有效；先按 Tab 切过去。", "warn")
            return None
        return self.view.cycle_mode()

    def current_goal(self):
        """当前 session 的目标。

        暂时读 temp/keep_session-goal.txt（有就用），没有就占位 —— 以后接
        Focusmate task / 手写目标时，只要换这里的取法，专注视图不用改。
        """
        path = os.path.join(TEMP, "keep_session-goal.txt")
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except OSError:
            pass
        return "（占位）还没接目标 —— 以后从 Focusmate task / temp/keep_session-goal.txt 读"

    def cmd_pause(self):
        """专注视图的 stop：消耗一次暂停（每次 PAUSE_MINUTES 分钟，每天 3 次）。"""
        ok, st, why = ks.start_pause()
        if not ok:
            self.say("暂停没生效：%s" % why, "warn")
            return
        self.say("暂停 %d 分钟（今天还剩 %d 次）—— 这几分钟里 daemon 不念你。"
                 % (ks.PAUSE_MINUTES, st["remaining"]), "ok")

    # -- 数据

    def daemon_state(self):
        pid = ks.read_pid()
        alive = bool(pid and ks.pid_alive(pid))
        st = ks.load_state()
        snap = st.get("api_snapshot") or {}
        return {"pid": pid, "alive": alive, "state": st, "snapshot": snap}

    def log_tail(self, n=1):
        """daemon 日志的最后 n 行 —— **只读文件尾部**。

        以前是 ``f.read().splitlines()``：每画一帧（也就是每敲一个字）都要把整份
        日志读进来再切分。日志一天长几 MB，这一项就从 0.1ms 涨到几十毫秒，
        而且越用越卡。现在从末尾 seek 一小块，并且按 (mtime, size) 记住结果。
        """
        try:
            st = os.stat(ks.LOG_FILE)
        except OSError:
            return []
        sig = (st.st_mtime_ns, st.st_size, n)
        memo = getattr(self, "_log_tail_memo", None)
        if memo is not None and memo[0] == sig:
            return memo[1]
        lines = []
        try:
            with open(ks.LOG_FILE, "rb") as f:
                want = 4096
                while True:
                    start = max(0, st.st_size - want)
                    f.seek(start)
                    lines = f.read().decode("utf-8", "replace").splitlines()
                    if start == 0:
                        break
                    if len(lines) > n:            # 尾部够用了
                        lines = lines[1:]         # 首行可能被截断在半条上
                        break
                    if want >= st.st_size:
                        break
                    want *= 4
        except OSError:
            return []
        tail = [line for line in lines if line.strip()][-n:]
        self._log_tail_memo = (sig, tail)
        return tail

    def refresh(self, quiet=False, timeout=180, wait=False):
        """拉一次**选中那一天**的 session，并和 data.csv 的契约合并成表格行。

        **读**操作：不预测，拿到真数据才换表格（失败时不要拿退化快照去覆盖
        已有的好数据 —— 快照里没有 session id，覆盖了后面 mute/title/cancel
        全都做不了）。默认**不阻塞**：网络丢进后台队列，进度和日志在面板里；
        ``wait=True`` 是给「必须先有真数据」的命令用的（``refresh --check``）。
        """
        self.reload_entries()
        day = self.sel_date
        if self.offline:
            if not self.rows:
                self._fallback_rows("离线模式")
            return False
        action = self.net_action("session.snapshot",
                                 "拉 session %s" % day.strftime("%m-%d"),
                                 {"date": day.isoformat(), "timeout": int(timeout)})
        job = self.jobs.submit(
            action, persist=False,
            on_ok=lambda payload: self._snapshot_arrived(payload, quiet=quiet),
            on_fail=lambda error: self._snapshot_failed(error, quiet=quiet))
        if wait:
            self.wait_net(timeout + 30)
        if self.jobs.sync or wait:
            return bool(job.outcome is not None and job.outcome.ok)
        if not quiet:
            self.say("正在拉 %s 的 session（后台，进度看面板）" % day.isoformat(), "cmd")
        return True

    def _snapshot_arrived(self, payload, quiet=False):
        count = self._apply_snapshot(payload, {})
        if not quiet:
            self.say("拉到 %d 场（%s）" % (count, (payload or {}).get("date") or ""), "info")

    def _snapshot_failed(self, error, quiet=False):
        self.had_error = True
        if self.rows and self.rows_source == "bridge":
            self.say("拉列表失败（表格保持上一次的结果）：%s" % error, "error")
        else:
            self._fallback_rows("bridge 出错")
            self.say("拉列表失败：%s" % error, "error")

    def wait_net(self, timeout=240.0):
        """等后台那一批跑完再往下走（`refresh --check` 这种要真数据的命令用）。

        这是**故意**阻塞的：一条「对账」命令必须拿着刚拉的账去对，
        不然它会对着一份旧表说「一场不缺」。
        """
        if self.jobs.sync:
            self.drain_net()
            return True
        done = self.jobs.wait(timeout)
        self.drain_net()
        return done

    # -- 表格行：契约（data.csv）和 API 上真实存在的 session 合并

    def rebuild_rows(self, now_ms=None):
        """把「契约」和「真实存在」按时间合并成一张表。

        契约有、API 有   → 已订（有 id，能 mute / title / cancel）
        契约有、API 没有 → 缺（草稿区里 = 还没吃进去；执行区里 = 该订却没订上）
        API 有、契约没有 → 计划外（你手动订的，或者你把 csv 那行删掉了）

        zone 是**每一行**的判据，不是整天的：start ≤ now+24h 才是执行区。
        """
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        now = datetime.datetime.now()
        day = self.sel_date
        self.reload_periods()
        plan = self.plan_rows_for(day)
        plan_labels = set(s["start"] for s in plan)
        api_by_label = {}
        for r in self.api_rows:
            api_by_label.setdefault(r["startLabel"], r)

        rows = []
        for slot in plan:
            start = sch.slot_datetime(slot, day)
            dur = int(slot["duration"])
            end = start + datetime.timedelta(minutes=dur)
            api = api_by_label.pop(slot["start"], None)
            if api:
                row = dict(api)
            else:
                s_ms = int(start.timestamp() * 1000)
                e_ms = int(end.timestamp() * 1000)
                row = {
                    "startMs": s_ms, "endMs": e_ms,
                    "startLabel": slot["start"], "endLabel": end.strftime("%H:%M"),
                    "durationMin": dur, "id": None, "title": "", "quiet": None,
                    "matched": None, "phase": self.phase_of(s_ms, e_ms, now_ms),
                }
            row["planned"] = True
            row["lineno"] = slot["lineno"]
            row["raw"] = slot["raw"]
            relocation = slot.get("relocation")
            if relocation:
                row["relocation"] = dict(relocation)
                row["relocated_from"] = slot.get("relocated_from")
                row["contract"] = "临时 %s→%s" % (slot.get("relocated_from") or "?",
                                                   slot["start"])
            else:
                row["contract"] = "%s,%s,%s" % (slot["weekday"], slot["start"], slot["duration"])
            row["zone"] = self.effective_zone(sch.zone_name(start, now), day)
            row["zone_raw"] = sch.zone_name(start, now)
            row["date"] = day.isoformat()
            rows.append(row)

        for r in self.api_rows:
            if r["startLabel"] in plan_labels:
                continue
            row = dict(r)
            start = datetime.datetime.fromtimestamp(r["startMs"] / 1000.0)
            row["planned"] = False
            row["lineno"] = None
            row["raw"] = ""
            row["contract"] = ""
            row["zone"] = sch.zone_name(start, now)
            row["date"] = start.date().isoformat()
            rows.append(row)

        rows.sort(key=lambda r: r["startMs"])
        # daemon 的「认账」结论（state.json 里 slots[key] 的 match / actual）：
        # 计划 16:15 没有同一分钟的 session，但实际被 16:00 25 + 16:30 50 覆盖了。
        # 这是**已经付过钱**的事实，表格必须照着它显示，不能再报「缺」。
        state_slots = self.daemon_slots()
        for row in rows:
            rec = state_slots.get("%sT%s" % (row.get("date"), row.get("startLabel"))) or {}
            if rec.get("match") and rec.get("match") != "exact":
                row["adopted"] = True
                row["actual_note"] = rec.get("actual") or rec.get("match")
            elif rec.get("match") == "exact" and rec.get("actual"):
                row["actual_note"] = rec.get("actual")
        for row in rows:
            try:
                self.attach_period_info(row, datetime.date.fromisoformat(row["date"]))
            except (KeyError, ValueError):
                row.setdefault("period", None)
                row.setdefault("period_id", None)
                row.setdefault("period_first", False)
                row.setdefault("task", None)
                row.setdefault("tasks", [])
                row.setdefault("task_text", "")
        self.rows = rows
        self.invalidate()          # 表格换了内容 → 画帧缓存作废

    def _fallback_rows(self, why):
        """拿不到 bridge 时，用 daemon 写下的 state.json 快照（只有时间，没有标题/静音）。"""
        self.reload_entries()
        st = ks.load_state()
        snap = st.get("api_snapshot") or {}
        iso = snap.get("sessions") or []
        now_ms = int(time.time() * 1000)
        want = self.sel_date.isoformat()
        api_rows = []
        for t in iso:
            try:
                dt = datetime.datetime.fromisoformat(t)
            except ValueError:
                continue
            # 快照是窗口（最多跨两天）的内容，按选中的那天过滤
            if dt.date().isoformat() != want:
                continue
            s_ms = int(dt.timestamp() * 1000)
            # 快照里没有 duration，先用 50min 占位；这个值只影响显示
            e_ms = s_ms + 50 * 60 * 1000
            api_rows.append({
                "startMs": s_ms,
                "endMs": e_ms,
                "startLabel": dt.strftime("%H:%M"),
                "endLabel": (dt + datetime.timedelta(minutes=50)).strftime("%H:%M"),
                "durationMin": None,
                "id": None,
                "title": "",
                "quiet": None,
                "matched": None,
                "phase": self.phase_of(s_ms, e_ms, now_ms),
            })
        api_rows.sort(key=lambda r: r["startMs"])
        self.api_rows = api_rows
        self.rows_source = "snapshot"
        self.rows_note = ("来自 keep_session 的 state.json 快照（%s）——只有时间，"
                          "没有标题/静音/伙伴/契约行" % why)
        self.rows_at = time.time()
        self.rebuild_rows(now_ms)

    def daemon_slots(self):
        """daemon 写下的 ``state.json`` 里的 ``slots``（带 2 秒缓存，画表格会问它）。"""
        now = time.time()
        cached = self._snapshot_cache.get("__daemon_slots__")
        if cached and now - cached[0] < 2.0:
            return cached[1]
        try:
            slots = (ks.load_state() or {}).get("slots") or {}
        except Exception:                       # noqa: BLE001 - 读不到就当没有
            slots = {}
        self._snapshot_cache["__daemon_slots__"] = (now, slots)
        return slots

    def actual_slots(self):
        """这一天的**实际** session（bridge 真数据）→ 收尾/对账要的形状。

        返回 ``[{"start": datetime, "duration": 分钟}]`` 或 None（离线 / 快照）。
        收尾按它发问，才不会把「计划 75min」当成一场去问。
        """
        if not self.authoritative:
            return None
        out = []
        for row in self.rows:
            if row.get("id") is None:
                continue
            try:
                start = datetime.datetime.fromtimestamp(row["startMs"] / 1000.0)
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"start": start, "duration": row.get("durationMin")})
        return out or None

    def effective_zone(self, zone, day):
        """``edit-tomorrow`` 设置：开着时，明天（以及之后）按草稿区算。

        需求：晚上 11 点想排明天早上 10 点（本来已经进 24 小时窗口 = 执行区，
        取消要敲宣言）—— 打开这个设置就是零阻力。过了凌晨，明天变成今天，
        又回到正常的 24 小时窗口规则（那时日期已经变了，不需要额外判断）。
        """
        if zone == "locked" and self.settings.get_bool("edit-tomorrow"):
            try:
                if day > datetime.date.today():
                    return "draft"
            except TypeError:
                return zone
        return zone

    @staticmethod
    def phase_of(start_ms, end_ms, now_ms):
        if now_ms < start_ms:
            return "未开始"
        if now_ms < end_ms:
            return "进行中"
        return "已结束"

    def plan_summary(self):
        """选中那天的契约执行情况：几场契约、几场真订上了、缺的是哪些。

        只有在 bridge 拉到真数据时才敢说"缺" —— state.json 快照里没有 session id，
        根本看不出订没订，那种情况一律返回 booked=None（渲染成"未知"）。
        草稿区里的"缺"也是正常的（还没到时间被吃进去）。
        """
        plan = [r for r in self.rows if r.get("planned")]
        if not plan:
            return None
        if self.rows_source != "bridge":
            return {"total": len(plan), "booked": None, "missing": None}
        missing = [r for r in plan if not r.get("id")]
        return {"total": len(plan), "booked": len(plan) - len(missing), "missing": missing}

    # -- 渲染

    def day_zone(self):
        """选中那天的区域（按天给个大致判断；每行还有更精确的 zone）。"""
        now = datetime.datetime.now()
        if self.sel_date < now.date():
            return "past"
        seen = set(r["zone"] for r in self.rows if r.get("planned"))
        if "locked" in seen:
            return "locked" if "draft" not in seen else "mixed"
        if seen:
            return "draft"
        if self.sel_date > now.date() and self.settings.get_bool("edit-tomorrow"):
            return "draft"
        return "locked" if self.sel_date == now.date() else "draft"

    def _day_cell(self):
        day = self.sel_date
        off = self.day_offset
        kinds = {0: ("今天", "bold"), -1: ("昨天", STYLE_MUTED), 1: ("明天", "bold")}
        name, style = kinds.get(off, ("%+d 天" % off, ""))
        zone = self.day_zone()
        zone_txt = {
            "past": ("过去 · 只能看", STYLE_MUTED),
            "locked": ("执行区 · 已吃进 API，取消要敲宣言", "bold red"),
            "draft": ("草稿区 · 100% 自由，随便改", "bold green"),
            "mixed": ("执行区 + 草稿区混合（按行看）」", "bold yellow"),
        }.get(zone, (zone, ""))
        cell = Text()
        cell.append("%s  %s %s" % (day.isoformat(), sch.cn_weekday_of_day(day), name), style="bold")
        cell.append("\n")
        cell.append(zone_txt[0], style=zone_txt[1])
        if off > 0 and self.settings.get_bool("edit-tomorrow"):
            cell.append("\n（edit-tomorrow 开着：未来的日子先按草稿区，凌晨后回到 24h 规则）",
                        style="green")
        return cell

    def _window_cell(self):
        now = datetime.datetime.now()
        win = sch.window(self.entries, now)
        cell = Text()
        cell.append("now → %s" % win["horizon"].strftime("%m-%d %H:%M"), style="bold")
        in_win = len(win["due"])
        cell.append("  执行区 %d 场" % in_win)
        if not in_win and not win["past"]:
            cell.append("  ⚠ 未来 24 小时一条契约都没有（不是放假就是断档）", style="bold red")
        if self.day_offset != 0:
            cell.append("   （窗口只跟今天/明天有关）", style="dim")
        return cell

    def render_header(self):
        d = self.daemon_state()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        if d["alive"]:
            daemon_cell = Text("● RUNNING", style="bold green")
            daemon_cell.append("  pid %s" % d["pid"], style="dim")
        else:
            daemon_cell = Text("○ STOPPED", style="bold red")
            if d["pid"]:
                daemon_cell.append("  残留 pid 文件 %s（进程已不在）" % d["pid"], style="dim")
        tail = self.log_tail(1)
        if tail:
            daemon_cell.append("\n" + tail[0], style="dim")

        up, browser, err = self.bridge.browser_state()
        if up and browser:
            bridge_cell = Text("● READY", style="bold green")
            extra = "  浏览器开着（占着 Chromium profile）"
            if self.bridge.last_idle_close:
                extra += "，上次释放 %s" % fmt_age(int(time.time() - self.bridge.last_idle_close))
            bridge_cell.append(extra, style="dim")
        elif up:
            bridge_cell = Text("○ IDLE", style="yellow")
            bridge_cell.append("  进程在，浏览器已释放（第一次命令要冷启动 ~15s）", style="dim")
        else:
            bridge_cell = Text("○ DOWN", style="bold red")
            bridge_cell.append("  " + (err or "还没起（第一次命令会自动起）"), style="dim")
        # 桥读的 profile 和 daemon 的不是一个 → 一律红字点名。这条静默地错过一次：
        # 登录态只在工作区那个 profile 里，桥去读老的 ~/.focusmate-mcp，每条命令
        # 都「未登录」，而表头还写着 READY/IDLE 看着一切正常。
        seen = getattr(self.bridge, "profile", "") or ""
        want = get_warden_global("focusmate_home", focusmate_home)()
        if seen and want:
            want_profile = os.path.join(want, "browser-data")
            if os.path.abspath(seen) != os.path.abspath(want_profile):
                bridge_cell.append("\n⚠ profile 不是 daemon 那个：%s" % seen,
                                   style="bold red")
        if self.bridge_note:
            bridge_cell.append("\n" + self.bridge_note, style="yellow")

        t.add_row("Daemon", daemon_cell)
        t.add_row("Bridge", bridge_cell)
        t.add_row("选中", self._day_cell())
        t.add_row("窗口", self._window_cell())

        sins = self.pending_sins()
        if sins:
            cell = Text()
            cell.append("⛔ 锁死：%d 笔未认罪" % len(sins), style="bold red")
            for rec in sins[:3]:
                cell.append("\n%s  %s（%s）" % (rec.get("at", "")[11:19], rec.get("key"),
                                              rec.get("what") or ""), style="red")
            if len(sins) > 3:
                cell.append("\n…还有 %d 笔" % (len(sins) - 3), style="dim")
            cell.append("\n除了敲完宣言，TUI 不执行任何命令。", style="bold red")
            t.add_row("罪案", cell)

        t.add_row("时钟", Text(now_local().strftime("%Y-%m-%d %H:%M:%S"), style="bold"))
        return Panel(t, title="[bold]The Warden[/]", border_style="cyan", box=box.ROUNDED)

    def render_table(self):
        day = self.sel_date
        title = "The Contract Table · %s %s" % (day.isoformat(),
                                                sch.cn_weekday_of_day(day))
        if self.day_offset == 0:
            title += "（今天）"
        elif self.day_offset == 1:
            title += "（明天）"
        elif self.day_offset == -1:
            title += "（昨天）"
        else:
            title += "（day %+d）" % self.day_offset
        title += "  [dim]day %+d[/]" % self.day_offset
        if self.rows_at:
            if self.rows_source == "bridge":
                title += "  [dim](%s)[/]" % fmt_age(int(time.time() - self.rows_at))
            else:
                title += "  [yellow](快照)[/]"

        items, collapsed = visible_session_rows(self.rows, self.session_ui_mode)
        if not self.rows:
            empty = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                          title_justify="left", expand=False)
            empty.add_column("状态")
            empty.add_row("（这天上既没有契约、也没有 session）")
            return empty
        t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        hidden_cols = self.effective_hidden_columns()
        visible_cols = [(i, header, kw) for i, (header, kw)
                        in enumerate(SESSION_TABLE_COLUMNS) if header not in hidden_cols]
        for _pos, header, kw in visible_cols:
            t.add_column(header, **kw)
        visible_positions = [i for i, _header, _kw in visible_cols]

        live_idx = None
        for idx, r in items:
            # ▶ 只标真正在进行的场次：权威模式下计划窗口到了、但 API 没有 id
            # 的行不是「正在发生」，不能抢当前标记。
            if r.get("phase") != "进行中":
                continue
            if self.authoritative and not r.get("id"):
                continue
            live_idx = idx
            break
        zone_tag = {"locked": ("锁", "bold red"), "draft": ("草", "bold green"),
                    "past": ("过", STYLE_MUTED)}
        for idx, r in items:
            # 数据（标题/时间/状态）不用 dim：dim 是给说明和单位的，压在数据上就看不清了
            style = STYLE_MUTED if r["phase"] == "已结束" else ""
            st = Text()
            tag, tag_style = zone_tag.get(r.get("zone"), ("", ""))
            if tag:
                st.append(tag + " ", style=tag_style)
            if not self.authoritative:
                # 快照里没有 session id —— 订没订看不出来，别假装知道
                st.append(r["phase"], style=STYLE_MUTED)
                st.append(" 快照", style=STYLE_MUTED)
            elif r.get("planned") and not r.get("id") and r.get("adopted"):
                # 计划这一行在 API 里没有「同一分钟」的场次，但 daemon 认账了
                # （实际是别的场次覆盖了它：拆分 / 挪了半格）。别再说「缺」——
                # 「缺」会让你去重订一场和现有场次打架的 session。
                st.append("认账", style="bold green")
            elif not r.get("planned"):
                st.append("计划外", style="bold magenta")
            else:
                st.append(r["phase"], style={"进行中": "bold green", "未开始": "white",
                                             "已结束": STYLE_MUTED}[r["phase"]])
            quiet = r.get("quiet")
            if quiet is None:
                qtxt = "[%s]?[/]" % STYLE_MUTED
            elif quiet:
                qtxt = "[bold yellow]ON[/]"
            elif r.get("period_muted"):
                # 时段要求静音、但 Focusmate 那边还没设上（正在补 / 补失败）——
                # 不能显示成「·」，那等于假装这一行不在静音时段里。
                qtxt = "[yellow]静…[/]"
            else:
                qtxt = "[%s]·[/]" % STYLE_MUTED
            dur = r.get("durationMin")
            label = Text()
            if idx == live_idx:
                label.append("▶ ", style="bold green")
            label.append(r.get("title") or "", style=style)
            if r.get("matched"):
                # 注意：Text.append **不解析 markup**，所以要给 style 而不是塞 "[cyan]…[/]"
                label.append("  · 已匹配", style="cyan")
            if r.get("actual_note"):
                label.append("  · %s" % r["actual_note"], style="yellow")
            period = r.get("period") or {}
            first = bool(r.get("period_first"))
            pid = period.get("id") if first else ""
            pspan = ("%s–%s" % (period.get("start"), period.get("end"))
                     if first and period else "")
            ptype = (period.get("type") or period.get("calendar") or "") if first else ""
            pname = (period.get("name") or period.get("title") or "") if first else ""
            task_text = r.get("task_text") or ""
            if task_text:
                task_cell = Text(task_text, style="bold green")
            else:
                # 每一行都是一场要执行的 session（计划或实际）；没有任务就明确报警，
                # 不要留空让用户以为只是「这一格没画东西」。
                task_cell = Text("没有设定任务🔞", style="bold red")
            cells = [
                Text(pid or "", style="bold magenta"),
                Text(pspan, style="cyan"),
                Text(ptype, style="cyan"),
                Text(pname, style="cyan"),
                str(idx),
                "%s–%s" % (r["startLabel"], r.get("endLabel") or "?"),
                ("%dmin" % dur) if dur else "?",
                st,
                qtxt,
                Text(r.get("contract") or "—", style=STYLE_MUTED),
                label,
                task_cell,
            ]
            t.add_row(*(cells[i] for i in visible_positions))
        captions = []
        if collapsed:
            captions.append("… 还有 %d 条过去 session 已收起（open detail 查看）"
                            % collapsed)
        if hidden_cols:
            captions.append("已折叠列：%s（open <表头名> 打开）"
                            % "、".join(h for h, _kw in SESSION_TABLE_COLUMNS
                                        if h in hidden_cols))
        if not items:
            captions.append("working 模式没有可展示的实际 session（open detail 查看全部）")
        if captions:
            t.caption = Text("\n".join(captions), style="dim")
            t.caption_justify = "left"
        return t

    def render_status_rows(self):
        """「状态」框的正文：契约执行情况 + 一次性/长期规则 + 临时挪动 + 文件位置。

        用户要求：这一行（原来这里挂的「永不重订名单（N 条）」）换成警告，
        并且「契约（data.csv 周几）」和警告一起装进同一个「状态」框（平滑重线边框）。
        永不重订名单本体只在「统计」视图里逐行列。
        """
        rows = []
        plan = self.plan_summary()
        day = self.sel_date
        wd_cn = sch.cn_weekday_of_day(day)
        if plan is None:
            rows.append(("契约：data.csv 里没有 %s 的规则（这天不排 session）" % wd_cn,
                         "dim"))
        elif plan["booked"] is None:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 订没订看不出来"
                         "（state.json 快照，不是真数据）" % (wd_cn, plan["total"]),
                         "yellow"))
        elif not plan["missing"]:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 已落地 %d 场 ✅ 一场不缺"
                         % (wd_cn, plan["total"], plan["booked"]), "bold green"))
        else:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 已落地 %d 场 · ⚠ 缺 %d 场：%s"
                         % (wd_cn, plan["total"], plan["booked"], len(plan["missing"]),
                            "、".join("%s(%smin)" % (r["startLabel"],
                                                     r.get("durationMin") or "?")
                                      for r in plan["missing"])),
                         "red"))
            if self.day_zone() in ("draft", "past"):
                rows.append(("  （草稿区/过去：还没被吃进去是正常的，daemon 下一轮会补）",
                             "dim"))

        # 契约的三个来源：时段排布 / 一次性 session / 长期规则
        try:
            counts = sp.plan_counts(self.download, self.layouts)
        except Exception:                       # noqa: BLE001 - 坏文件不该炸框
            counts = {}
        if counts:
            pieces = ["时段排布 %d" % counts.get("period", 0)]
            if counts.get("extra"):
                pieces.append("一次性 %d" % counts["extra"])
            if counts.get("rule"):
                pieces.append("长期规则 %d" % counts["rule"])
            rows.append(("来源：" + " · ".join(pieces)
                         + "   （session = 只加这一天；session --permanent = 写进日历）",
                         "dim"))

        relocated = [r for r in self.rows if r.get("planned") and r.get("relocation")]
        if relocated:
            rows.append(("临时挪动（只影响这一天，不改 data.csv）：" +
                         "、".join("%s→%s" % (r.get("relocated_from") or "?",
                                              r["startLabel"]) for r in relocated),
                         "magenta"))
        rows.extend(self.freshness_rows())
        rows.append((sch.data_csv_path(), "dim"))
        return rows

    # -- 警告（repl_kit.WarningBoard）-----------------------------------

    def sessions_for_day_plan(self, day=None):
        """那一天**排好的** session（时段 + 一次性；不含 API、不含长期规则展开）。"""
        day = day or self.sel_date
        return sp.sessions_for_day(self.download, self.layouts, day)

    def repeated_task_for(self, day, session):
        """这一场有没有本次重复生成的子任务；没有就 None。"""
        for task in self.task_store.tasks_for_session(day, session.get("start")):
            if task.get("repeat_parent"):
                return task
        return None

    def _suggest_repeat_spec(self, day=None, sessions=None):
        """建议的 --repeat 取值：那天最后一场的时间（没有就 22:15）。"""
        day = day or datetime.date.today()
        sessions = sessions if sessions is not None else self.sessions_for_day_plan(day)
        start = sessions[-1]["start"] if sessions else "22:15"
        return "%s@%s" % (cfg.WEEKDAYS[day.weekday()], start)
    def collect_warnings(self):
        """现在有哪些「问题」要显示在状态框里。纯本地计算，不联网、不写盘。"""
        out = []
        # 1) data.csv 解析问题（schedule.py 给的 warning）。最多列 3 条：
        #    状态框是给人一眼看"哪里不对"的，不是日志。
        entry_warnings = [str(t) for t in (self.entry_warnings or []) if str(t).strip()]
        for text in entry_warnings[:3]:
            if "重复" in text:
                hint = ("同一天同一时刻只能留一行：删掉多余的，"
                        "或改成 session --permanent 只留一条长期规则")
            else:
                hint = "格式：mon,HH:MM,50 或 YYYY-MM-DD,HH:MM,50（时长 25/50/75）"
            out.append(repl_kit.Warning(text, level="error", source="data.csv", hint=hint))
        if len(entry_warnings) > 3:
            out.append(repl_kit.Warning(
                "data.csv 还有 %d 条解析问题没列出来" % (len(entry_warnings) - 3),
                level="error", source="data.csv",
                hint="python3 schedule.py show 看全部"))
        # 2) 时段 layout 坏了（排不出来）
        for period in self.periods_for_day(self.sel_date):
            try:
                sp.sessions_for_period(period, self.layouts)
            except ValueError as e:
                out.append(repl_kit.Warning(
                    "%s %s 的排布坏了：%s" % (period.get("date"), period.get("id"), e),
                    level="error", source="layout",
                    hint="period %s 25 50 重新排，或删掉 temp/session-layouts.json 里那条"
                         % period.get("id")))
        # 3) 重算 data.csv 时被冲掉的旧行（备份仍在 temp/data.csv.bak）
        if self.csv_dropped:
            out.append(repl_kit.Warning(
                "重算 data.csv 冲掉了 %d 行旧契约（%s%s）"
                % (len(self.csv_dropped), "、".join(self.csv_dropped[:2]),
                   "…" if len(self.csv_dropped) > 2 else ""),
                level="warn", source="data.csv",
                hint="用 session --permanent 加回长期规则，或把这些行放回日历/一次性 session"))
        # 3.5) 过期还没做完的任务：给 `inspect-due` 一个入口（不然这条命令没人知道）
        try:
            overdue = psv.due_candidates(self.task_store)
        except Exception:                      # noqa: BLE001 - 警告板不能因为这里坏掉
            overdue = []
        if overdue:
            first = overdue[0]
            title = self.task_store.focusmate_title(first, max_len=18)
            out.append(repl_kit.Warning(
                "%d 条任务过了截止时间还没做完（最早：%s「%s」）"
                % (len(overdue), first.get("id"), title),
                level="warn", source="task",
                hint="敲 inspect-due 逐个过一遍"
                     "（A 延期 / B 放弃 / C 重议 / D 补录 / E 推后 / G 延长排期）"))
        # 3.6) 网络操作失败账：有它就告诉你，而且告诉你下一步敲什么
        failures = self.jobs.failures
        if failures:
            first = failures[0]
            more = (" 等 %d 条" % len(failures)) if len(failures) > 1 else ""
            out.append(repl_kit.Warning(
                "%d 条网络操作失败（%s：%s%s）" % (len(failures), first.action.label,
                                                first.error, more),
                level="error", source="网络",
                hint="re-action 重放全部（re-action --list 先看清单，--clear 清空）"))
        # 3.7) 今天还没 plan：项目在那儿、空闲也在那儿，但今天一个项目都没分到 count
        try:
            pending = self.pending_plan(datetime.date.today())
        except Exception:                      # noqa: BLE001 - 警告板不能因为这里坏掉
            pending = None
        if pending:
            out.append(repl_kit.Warning(
                "今天还没 plan：还有 %d 场空闲（%d count）没分给项目（%s）"
                % (pending["sessions"], pending["counts"],
                   "、".join(str(s) for s in pending["starts"][:3])
                   + ("…" if len(pending["starts"]) > 3 else "")),
                level="warn", source="项目",
                hint="敲 plan 分今天的资源（plan +1 = 明天；task 1 4 = 项目 #1 分 4 个 count）"))
        # 4) 重复任务：今天该不该有 occurrence（日期对上就算；挂不挂得上 session
        # 是另一回事，不再用「同起点同时长」否定整条规则）
        today = datetime.date.today()
        today_sessions = self.sessions_for_day_plan(today)
        landed = []
        for task in self.task_store.repeated_tasks():
            if self.task_store.repeat_due_on(task, today):
                landed.append(task)
        if self.settings.get("tomorrow-plan") and not self.task_store.repeated_tasks():
            out.append(repl_kit.Warning(
                "设置里填了「明日时间安排」（%s），但没有对应的重复任务"
                % self.settings.get("tomorrow-plan"),
                level="warn", source="重复任务",
                hint="task create title=明日时间安排 --repeat %s"
                     % self.settings.get("tomorrow-plan")))
        elif not landed:
            out.append(repl_kit.Warning(
                "今天没有「明日时间安排」这类重复任务要生成",
                level="warn", source="重复任务",
                hint="task create title=明日时间安排 --repeat %s"
                     % self._suggest_repeat_spec(today, today_sessions)))
        # 5) 琐事 Project：没绑定 / 绑定失效 / 项目里没活了都要说一声。
        project, chore_error = self.chore_binding()
        if chore_error:
            out.append(repl_kit.Warning(
                "琐事 Project 不可用：%s" % chore_error,
                level="warn", source="琐事",
                hint="project 看项目列表；set chore-project #1 绑定（空 = 恢复未绑定）"))
        elif project and not self.chore_project_tasks(project):
            out.append(repl_kit.Warning(
                "琐事项目「%s」没有未完成任务了" % (project.get("title") or project.get("id")),
                level="warn", source="琐事",
                hint="去 OmniFocus 加一条任务并 project refresh；或 set chore-project 换项目"))
        return out

    def refresh_warnings(self, force=False, max_age=5.0):
        """重算警告板（每次操作之后 / 最多 5 秒一次；不联网、不写盘）。

        顺便（每帧，自己节流 1 秒）看一眼「有没有刚结束的 session 该收尾」：
        daemon 在结束 +15 秒把 warden 拉到前台并把这场写进
        ``temp/keep_session-postsession.json``，这里把它接过来。
        """
        try:
            self._maybe_start_review()
        except Exception:                          # noqa: BLE001 - 收尾坏了也不能没帧
            pass
        try:
            self._maybe_start_nostart()
        except Exception:                          # noqa: BLE001 - 这一问坏了也不能没帧
            pass
        try:
            self._maybe_apply_mutes()
        except Exception:                          # noqa: BLE001 - 静音坏了也不能没帧
            pass
        try:
            self._maybe_daily_project_sync()
            self._maybe_start_project_sync_conflict()
        except Exception:                          # noqa: BLE001 - 项目对账坏了也不能没帧
            pass
        try:
            self._maybe_sync_chore_mode()
        except Exception:                          # noqa: BLE001 - 空闲屏坏了也不能没帧
            pass
        now = time.time()
        if not force and self.warnings_at and now - self.warnings_at < max_age:
            return self.warnings
        before = self.warnings.signature()
        self.warnings.set_all(self.collect_warnings())
        self.warnings_at = now
        self.warnings_sig = self.warnings.signature()
        if self.warnings_sig != before:            # 警告变了 → 画面得重排版
            self.invalidate()
        return self.warnings

    def render_messages(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=False)
        colors = {"info": "white", "ok": "green", "warn": "yellow", "error": "bold red",
                  "cmd": "bold cyan"}
        for stamp, kind, text in self.messages[-MESSAGE_SHOW:]:
            t.add_row(stamp, Text(text, style=colors.get(kind, "white")))
        return Panel(t, title="[bold]Recent[/]", border_style="dim", box=box.ROUNDED)
    def _column_command_args(self, args):
        """解析 open/close 的列名 + ``--permanent``。

        列名允许带空格（``open 时段 ID`` / ``open "时段 ID"`` 都行），
        所以不能只拿 args[0]。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        permanent = False
        kept = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "永久"):
                permanent = True
            else:
                kept.append(tok)
        return " ".join(kept).strip(), permanent, toks

    def _switch_session_mode(self, mode):
        self.session_ui_mode = mode
        if not isinstance(self.view, ContractView):
            self.go_to_view(ContractView)
        self.say("看板切到 %s：%s"
                 % (mode, "实际 session（过去只留最近一场）"
                    if mode == "working" else "计划 + 实际全部内容"), "cmd")
        self.invalidate()
        return mode

    def _resolve_open_close_column(self, text, task_mode):
        """当前视图优先：任务视图找任务表头，其他视图找契约表头。

        返回 ``(col, mode, headers, table_name)``；找不到返回 ``(None, ...)``。
        这样两个表里都有「状态」时不会串，同时从别的视图敲任务表头也能切过去。
        """
        tasks = self.task_store.list()
        task_headers = task_table_headers(tasks)
        session_headers = [h for h, _kw in SESSION_TABLE_COLUMNS]
        if task_mode:
            col = normalize_task_column(text, tasks)
            if col:
                return col, TASK_UI_MODE, task_headers, "任务表"
            col = normalize_session_column(text)
            if col:
                return col, self.session_ui_mode, session_headers, "契约表"
        else:
            col = normalize_session_column(text)
            if col:
                return col, self.session_ui_mode, session_headers, "契约表"
            col = normalize_task_column(text, tasks)
            if col:
                return col, TASK_UI_MODE, task_headers, "任务表"
        return None, None, task_headers if task_mode else session_headers, (
            "任务表" if task_mode else "契约表")

    def _open_pro_file(self, pro):
        """打开项目 ``pro`` 关联的文件（``open`` + 回车顺便打开）。"""
        raw_file = str((pro or {}).get("file") or "").strip()
        if not raw_file:
            return None
        expanded = os.path.expanduser(raw_file)
        self.opened_files.append(expanded)
        opener = getattr(self, "file_opener", None)
        if callable(opener):
            opener(expanded, pro)
        elif not self.offline and sys.platform == "darwin":
            try:
                subprocess.Popen(["open", expanded],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                self.say("⚠ 打开项目关联文件失败（%s）：%s" % (raw_file, e), "warn")
                return None
        self.say("📂 已顺便打开项目「%s」关联文件：%s"
                 % (pro.get("title") or pro.get("id"), raw_file), "ok")
        return expanded

    def _pro_for_task(self, task):
        """从一条本地任务找到它所属的真正项目 ``pro``（通过 ``project_id`` 或 ``project`` 字段）。"""
        if not isinstance(task, dict):
            return None
        pid, ptitle, _pk = ts.TaskStore.project_task_fields(task)
        if pid is not None:
            pro = self.projects.pro_for_op(pid)
            if pro is not None:
                return pro
        if ptitle:
            for pro in self.projects.active_pros():
                if str(pro.get("title") or "").strip().casefold() == str(ptitle).strip().casefold():
                    return pro
        return None

    def _open_associated_project_files(self):
        """在 ``open`` + 回车时顺便打开当前相关项目（``pro``）关联的文件。"""
        try:
            self.projects.ensure_tagged_pros()
        except Exception:                          # noqa: BLE001
            pass
        candidates = []
        seen_ids = set()

        def add_pro(pro):
            if pro and pro.get("file") and pro.get("id") not in seen_ids:
                seen_ids.add(pro["id"])
                candidates.append(pro)

        # 1) 当前进行中或即将开始 session 上的任务所属项目
        try:
            cur = self.live_session()
            if cur is not None:
                _idx, row = cur
                for t in self.task_store.tasks_for_session(row.get("date"), row.get("startLabel")):
                    add_pro(self._pro_for_task(t))
        except Exception:                          # noqa: BLE001
            pass
        # 2) 当前选中任务所属项目
        if not candidates and self.selected_task_id is not None:
            try:
                sel_task = self.task_store.get(self.selected_task_id)
                add_pro(self._pro_for_task(sel_task))
            except Exception:                      # noqa: BLE001
                pass
        # 3) 最近选中的 pro
        if not candidates and self.selected_pro_id is not None:
            add_pro(self.projects.pro_by_id(self.selected_pro_id))
        # 4) 所有配置了关联文件的 active pro
        if not candidates:
            for pro in self.projects.active_pros():
                add_pro(pro)
        opened = []
        for pro in candidates:
            got = self._open_pro_file(pro)
            if got:
                opened.append(got)
        return opened

    #: `open <模型>` 的别名。只收英文 token，避免和任务表/契约表的表头名
    #: （例如「项目」列）抢同一个词。
    DATA_MODEL_ALIASES = {
        "task": "task", "tasks": "task",
        "project": "project", "projects": "project",
        "op": "op", "ops": "op",
    }

    def _switch_data_model(self, model):
        """切到第四视图「数据」并选择展示 task / project / op 模型。"""
        if model not in ("task", "project", "op"):
            raise ValueError("数据模型只能是 task / project / op；你给的是 %r" % model)
        self.data_view_model = model
        if model == "task":
            # 任务模型重新从第一页开始；project / op 没有分页状态。
            self.task_view_page = None
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        label = {"task": "Task 任务", "project": "Project 真正项目 pro",
                 "op": "Op OmniFocus Project"}[model]
        self.say("🗂 数据视图 → %s（open task / open project / open op 切换）" % label,
                 "cmd")
        self.invalidate()
        return model

    def cmd_open(self, args=None):
        """``open task|project|op`` 切数据模型；``open working|detail`` 切口径；
        ``open <表头> [--permanent]`` 展开列；直接敲 ``open`` + 回车时顺便
        打开项目（``pro``）关联的文件。

        working 默认自动折叠「时段类型 / 时段名称 / 契约」；手动 open 只是
        本次运行的覆盖，加 ``--permanent`` 才会写进偏好、以后也不再自动折叠。
        在数据视图里，``open/close`` 作用于任务表的表头。
        """
        text, permanent, toks = self._column_command_args(args)
        if not text:
            if permanent:
                raise ValueError("open --permanent 后面要跟一个表头名")
            mode = self._switch_session_mode(
                "detail" if self.session_ui_mode == "working" else "working")
            self._open_associated_project_files()
            return mode
        # 三个数据模型优先于同名的任务表表头：open project = 看 pro 数据，
        # 想看任务表的 project 列请用 `open 项目`。
        model = self.DATA_MODEL_ALIASES.get(text.strip().lower())
        if model is not None:
            if permanent:
                raise ValueError("open task/project/op 是切换数据模型，不带 --permanent")
            return self._switch_data_model(model)
        if toks and toks[0].lower() in ("pro", "project", "项目") and len(toks) >= 2:
            pro = self._ensure_pro_for_token(" ".join(toks[1:]))
            self.selected_pro_id = pro["id"]
            if not pro.get("file"):
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                raise ValueError("项目 #%s「%s」还没有关联文件；先用 `pro %s file <路径>` 绑定。"
                                 % (pnum, pro.get("title"), pnum))
            return self._open_pro_file(pro)
        aliases = {"w": "working", "work": "working", "d": "detail", "all": "detail"}
        mode = aliases.get(text.lower(), text.lower())
        if mode in SESSION_UI_MODES:
            if permanent:
                raise ValueError("open --permanent 只用于表头列；切口径不带 --permanent")
            return self._switch_session_mode(mode)
        task_mode = isinstance(self.view, TaskView) and getattr(
            self, "data_view_model", "task") == "task"
        if task_mode and re.match(r"^(?:page\s*|p)?(\d+)$", text.lower()):
            if permanent:
                raise ValueError("open <页码> 翻页不带 --permanent")
            page_no = int(re.match(r"^(?:page\s*|p)?(\d+)$", text.lower()).group(1))
            return self.cmd_task_page([str(page_no)])
        col, target_mode, headers, table_name = self._resolve_open_close_column(
            text, task_mode=task_mode)
        if not col:
            # 退回看看是不是要直接打开某个关联了文件的项目 pro
            try:
                pro = self.projects.resolve_pro(text)
            except ValueError:
                pro = None
            if pro is not None and pro.get("file"):
                self.selected_pro_id = pro["id"]
                return self._open_pro_file(pro)
            raise ValueError(
                "open 只认 working / detail、表头名（当前表：%s）或已关联文件的项目 pro；你给的是 %r"
                % ("、".join(headers), text))
        self._set_column_open(col, permanent=permanent, mode=target_mode)
        if target_mode == TASK_UI_MODE:
            if not isinstance(self.view, TaskView):
                self.go_to_view(TaskView)
            detail = "（任务表）"
        else:
            if not isinstance(self.view, ContractView):
                self.go_to_view(ContractView)
            detail = "（%s 口径）" % self.session_ui_mode
        self.say("已打开列「%s」%s%s"
                 % (col, "并写入偏好" if permanent else "（本次运行）", detail), "cmd")
        self.invalidate()
        return col

    def cmd_close(self, args=None):
        """``close <表头> [--permanent]``：折叠一列；永久偏好写 warden-ui.json。

        在任务视图里折叠任务表列；其他视图折叠契约表列。
        """
        text, permanent, _toks = self._column_command_args(args)
        if not text:
            raise ValueError("用法：close <表头名> [--permanent]；当前表头名：%s"
                             % "、".join(task_table_headers(self.task_store.list())
                                         if isinstance(self.view, TaskView)
                                         else [h for h, _kw in SESSION_TABLE_COLUMNS]))
        task_mode = isinstance(self.view, TaskView)
        col, target_mode, headers, table_name = self._resolve_open_close_column(
            text, task_mode=task_mode)
        if not col:
            raise ValueError(
                "close 只认表头名（当前表：%s）；你给的是 %r"
                % ("、".join(headers), text))
        # 至少留一列，否则 rich 会画一张没有列的表。
        if target_mode == TASK_UI_MODE:
            hidden = set(self.effective_task_hidden_columns(self.task_store.list()))
        else:
            hidden = set(self.effective_hidden_columns())
        hidden.add(col)
        if len(hidden) >= len(headers):
            raise ValueError("不能把所有列都折叠；至少留一列看。")
        self._set_column_closed(col, permanent=permanent, mode=target_mode)
        if target_mode == TASK_UI_MODE:
            if not isinstance(self.view, TaskView):
                self.go_to_view(TaskView)
            detail = "（任务表）"
        else:
            if not isinstance(self.view, ContractView):
                self.go_to_view(ContractView)
            detail = "（%s 口径）" % self.session_ui_mode
        self.say("已折叠列「%s」%s%s"
                 % (col, "并写入偏好" if permanent else "（本次运行）", detail), "cmd")
        self.invalidate()
        return col
    def cmd_help(self, args=None):
        toks = [str(a).strip().lower() for a in (args or [])]
        if any(tok in ("--task", "--data", "-t", "task", "data", "任务", "数据")
               for tok in toks):
            return self.cmd_help_task()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True, justify="right")
        t.add_column()
        for cmd, desc in [
            ("Tab", "切换视图：契约 → 专注 → 统计 → 数据 → 设置 → 契约（循环）"),
            ("PgUp / PgDn", "长看板在**当前界面内**往上/往下翻；只在一屏范围内滚动，"
                            "不会把旧帧写进终端 scrollback（全屏模式）"),
            ("open working|detail", "切换 session 看板口径：working = 实际为主、"
                                     "过去只留最近一场；detail = 计划 + 实际全部"),
            ("open task|project|op", "切到第四视图「数据」，分别查看 task 任务库 / "
                                      "project 真正项目 pro / op OmniFocus Project 三种数据模型"),
            ("filter disposable|all", "数据视图过滤器：默认 disposable 只显示 active，"
                                      "隐藏被 mark complete / discard 的 task / pro / op；"
                                      "all 显示全部"),
            ("open/close <表头>", "展开/折叠契约表或任务表的一列；加 --permanent 写进偏好，"
                                  "working 默认折叠 时段类型 / 时段名称 / 契约"),
            ("stat [refresh]", "切到「统计」视图：生涯累计 / 每天分布 / 永不重订名单（只读 API）"),
            ("stat refresh full", "统计：丢掉缓存、全量重拉（默认只增量拉缺口 + 最近几天）"),
            ("Shift+Tab（专注视图）", "循环切换专注模式：普通 / 做题 / 写作"),
            ("day -1|0|1|YYYY-MM-DD", "切换看哪一天（草稿区随便看）"),
            ("session HH:MM LEN", "只加这一天的 session（一次性），再立刻触发一轮 check"),
            ("session --permanent HH:MM LEN", "写进日历：按星期几重复的长期规则"),
            ("session HH:MM-HH:MM", "同上，但用起点–终点（如 11:15-12:05 → 50min）"),
            ("session T1 50 50", "按时段改排布：T1 的第一场从时段起点开始，超时就报错"),
            ("period T1 25 25 25 25", "同 session T1 ...；只给 T1 = 撤销自定义，恢复默认"),
            ("set <id> <value>（设置视图）", "改设置；不写 value = 恢复默认。settings 重新列出全部"),
            ("warn on|off", "状态框里的警告显示开关（不写 = 取反）"),
            ("task create ...", "在「数据」视图的 task 模型建任务："
                                 "task create [id] [field=value ...]"),
            ("task delete/modify ...", "task delete <id> / task modify <id> <field> <值>"),
            ("task assign <session ID>", "把选中的任务挂到表格 #N 的 session，并自动写 Focusmate task"),
            ("task <任务ID> -a <session ID>", "选中并挂上：task 8 -a 1（裸 ID 就等于 task select 8）"),
            ("task ... --assign N", "任何 task 子命令都能带：跑完顺手挂到表格 #N，"
                                    "如 task create title=写论文 --assign 3"),
            ("task create ... --repeat mon@22:15",
             "建每周重复任务：每周一 22:15 那场自动挂上（拆分后那一周就不挂）"),
            ("task repeat <id> 周几@HH:MM|off", "给已有任务加上/取消每周重复；"
                                                  "OmniFocus 导入的规则会保留原 RRULE，"
                                                  "task repeat <id> 直接看原规则"),
            ("重复任务本体", "带 repeat 的任务（source 指向 OmniFocus）。它只当模板，"
                              "不在【契约】的任务面板 / task 列显示、永远不挂 session，也"
                              "**不能**被 done / -m complete；任务视图里显示成 id:N，"
                              "要结束这套重复用 task repeat off 或 task <id> drop（手动）"),
            ("重复任务子任务", "到了规则日自动生成一条：repeat_parent 指向本体，"
                                "复制标题/字段/O-F 关联，真正挂 session、显示在契约表里，"
                                "带 🔁；只有它能被完成"),
            ("重复任务日期", "本体 repeat.rule 是 OmniFocus 原 RRULE（DAILY / 多个 BYDAY / "
                              "INTERVAL / COUNT / UNTIL …），日期由 dateutil 展开；"
                              "start/duration 只决定优先挂当天哪一场"),
            ("契约表来源记号", "🔁 = 重复任务子任务 · 🔗 = 有 OmniFocus 关联 · "
                                "🔁🔗 = 两者都有 · ⏱ = 挂了 formatter"),
            ("task import", "读剪贴板导入 OmniFocus 任务；如果是重复任务（例如每周三），"
                            "会先问要不要把重复设置也复制到本地；-st / --single-task "
                            "= 只导入这一条，不问也不建 repeat"),
            ("三个数据模型区别", "pro = 真正项目 Project（有阶段 running/bottleneck、可关联文件） · "
                                 "op = OmniFocus Project（可以是 incubator 或普通的 SOP 两种类型） · "
                                 "task = 具体任务 Task（挂到 session 执行的单条任务）"),
            ("pro / project", "管理真正项目 Project (pro)：阶段为 bottleneck 时只允许对应的 "
                              "incubator op 进行；阶段为 running 时只允许普通的 SOP op 进行。"
                              "SOP op 通过 OmniFocus 标签 `project/…` 自动关联真正的项目 pro"),
            ("pro <序号> -a / --advance", "快捷切换 pro 状态（running ↔ bottleneck），"
                                           "需提供新的 op 作为关联（可直接跟 `task import` 或 `op <序号>`，"
                                           "也可回车后按提示输入）"),
            ("pro <序号> -m complete|discard", "只改真正项目 pro 的本地状态；completed / dropped "
                                               "不再进入 active 列表、plan 和默认数据视图"),
            ("op <序号> -m complete|discard", "只改 OmniFocus op 的本地状态；completed / dropped "
                                              "不再进入 active 列表、plan 和默认数据视图"),
            ("pro <序号> interrupt [--hard]", "硬中断当前进行中的 session：把这一场所有任务下架，"
                                               "再让你输入 title，新建一条与整场等长的任务挂回去"),
            ("pro <序号> file <路径>", "为项目 pro 关联一个文件，作为敲 `open` + Enter 之后顺便打开的文件"
                                       "（也可 `pro create <名> file=<路径>` 或 `pro <序号> open`）"),
            ("op [序号] [sop|incubator]", "管理 OmniFocus Project (op)：每个 op 必须是 SOP 或 incubator "
                                          "两种类型之一，导入时必须说明；每次 refresh 也会自动检查并让用户补全"),
            ("task import [sop|incubator]（项目）",
             "剪贴板里是 **OmniFocus 项目**（omnifocus:///project/PK）→ 进 op 库（也可 `op import sop|incubator`）："
             "需说明 SOP 或 incubator 类型；带 `project/…` 标签自动关联 pro；时长不齐整批拒绝"),
            ("project [序号] [interrupt|refresh|remove|check]", "看 pro 与当前 op / 看 #1 详情与清单 / 硬中断当前场 / 重新去 "
                                                       "OmniFocus 读一遍 / 撤掉 / check = 每日对账"),
            ("plan [-c|--current] [天数]", "一次排期：每个真正项目只取当前阶段对应的那一个 op"
                            "（running → SOP / bottleneck → incubator），"
                            "分配 count + 自动挑任务挂 session。没有关联 pro 的孤立 op 不进 plan。"
                            "**默认今天**，plan +1 = 明天；plan -c 把今天正在进行的空 session "
                            "也纳入（只算剩下的完整 25 分钟块）"),
            ("task -t  /  --trigger", "列出 trigger（创建触发）预设；现在是空列表 —— "
                                        "框架已就位，注册函数后才有名字"),
            ("task <id> -t <preset> [参数...]", "挂创建触发：只有在预设条件成立时才建任务；"
                                                  "当前只记本地，runner 还没实现"),
            ("task -f  /  --formatter",
             "列出 formatter（排期算法）预设；现在有 end / enlist / repeat"),
            ("task <id> -f end", "按任务 duration 找当天最后一段可用时间，截出尾部并自动挂 session；"
                                  "例如 25min 任务占 11:30–12:00（25 + 5min 休息）。"
                                  "挂在重复任务**本体**上时，formatter 会转嫁给今天/明天的"
                                  "子任务，本体自己不排"),
            ("task <id> -f repeat 10:45", "把任务固定挂到起点为 10:45 的那一场已有 session；"
                                  "不切/不造 session。挂在重复任务本体上时，每周每个"
                                  "occurrence 都固定用这一场（也可写 #2 表示当天第 2 场）"),
            ("task archive [N]", "看完成归档（data/task-archive.json，只增不改）"),
            ("task sync", "GET /v1/tasks，把 completedAt 连同 session 写进完成归档"),
            ("cancel N", "取消 #N：草稿区直接删契约；执行区要敲宣言"),
            ("relocate N ±MM", "临时把 #N 提前/推后：不写 data.csv；推后一天一次，且不能跨天"),
            ("relocate T1 ±MM", "把整个时段里的每场一起挪：相对位置不变、任务跟着走；"
                                "推后每时段一天一次，和别的场次重叠就拒绝"),
            ("plan 里那几句", "task 1 4 = 项目 #1 分 4 count（100 分钟）· task 1 clear = 清空 · "
                              "再敲一次是覆盖（不是相加）· 超预算时整条命令都不执行 · "
                              "q 放弃这次 plan（不建任何任务）"),
            ("🚪 到点没开始（p/x/s）",
             "计划 14:00 那场到 14:05 在 API 里还是没有 → 问一次：p 15 推迟这一场 / "
             "p T1 15 推迟整个时段 / x 不去了（算缺失，任务交给收尾）/ s 先不管。"
             "推迟到 14:10 之后作废，直接算缺失"),
            ("mute N on|off", "把 #N 静音；不写 on/off 就是取反"),
            ('title N "文本"', '把 #N 的标题改成文本；title N "" 清空'),
            ("force-check", "忽略重试上限/退避：把所有没订上的 session 各强制重订一次"),
            ("sync [--days N]", "跑 download_calendar.py populate：日历 → 覆盖 data.csv"),
            ("refresh  (r)", "全量刷新：session（会重编号）+ 任务完成状态 + 统计 + 收尾面板；"
                             "refresh --check 再对账并修正"),
            ("start", "启动 daemon（会先让 bridge 放开浏览器）"),
            ("stop（契约视图）", "停用 daemon（和取消一样要敲宣言）"),
            ("stop（专注视图）", "消耗一次暂停：暂停 5 分钟，每天最多 3 次，daemon 不念你"),
            ("status  (s)", "只重画看板"),
            ("quit  (q)", "退出（bridge 和浏览器一起收掉）"),
        ]:
            t.add_row(cmd, desc)
        panel_main = Panel(t, title="[bold]命令（回车或敲任意命令关闭）[/]",
                           border_style="cyan", box=box.ROUNDED)
        self.help_overlay = panel_main
        self.invalidate()
        self.emit(panel_main)
        self.emit(
            "[dim]ID 就是表格第一列，按时间排序、每次 refresh 重编号 —— 看着表敲，别凭记忆。\n"
            "区域标记（状态列第一格）：草=草稿区（随便改）· 锁=执行区（取消要敲宣言）"
            "· 过=已过去。\n"
            "专注视图：只认 stop（消耗暂停）和 Shift+Tab（切模式），"
            "预约那套命令在那边一律没用。\n"
            "统计视图：只读，stat 走缓存、stat refresh 只增量拉缺口 + 最近几天；"
            "它不改 data.csv、不碰 API 写接口。\n"
            "重复任务机制：本体（repeat + source=omnifocus）只当模板，永远不挂 session，"
            "所以不出现在【契约】表的 task 列。到了规则日（RRULE 判定，不看这一刻哪一场）"
            "自动生成一条 occurrence 子任务：repeat_parent 指向本体、继承字段和 OmniFocus 关联；"
            "子任务才去挂当天 session（同起点优先，否则取最近一场；当天没 session 也先生成，"
            "之后手动 assign 或 task sync 再补）。子任务的 session 终点仍会写成 due。\n"
            "本体的 formatter 只是**模板**：task <本体> -f end 会先为今天/明天/下一个规则日"
            "生成子任务，再在每条子任务上跑 formatter（子任务带 ⏱ 和它自己的 reservation）；"
            "本体自己永远没有 reservation、也不占 session。切换 day +1 就能看到明天那条已经"
            "排好的子任务。只有子任务能被完成；本体要结束只能手动 task repeat off 或 drop。\n"
            "契约表 task 列来源记号：🔁=重复任务子任务，🔗=有 OmniFocus 关联，"
            "两个都有就是 🔁🔗；⏱=挂了 formatter（不是「重复」的意思）。"
            "本体不进契约面板，只在 task 视图里显示为 id:N、来源记号只有 🔗。\n"
            "OmniFocus 写回：**只要知道一条任务完成了就一定写**（task done / 收尾 D / "
            "task sync / 收尾自动核对 / 任何一次 refresh 同步完成状态），幂等；"
            "放弃则会放弃 OmniFocus 那条（收尾 A/C/E 那些「换账本」的动作用 --local）。"
            "OmniFocus 那条自己要是重复任务，写完会在那边翻到下一次（新 primary key），"
            "本地还开着的记录（本体 + 后面的子任务）自动改挂过去 —— 不然下一次的活"
            "永远勾不掉；放弃本体时带 --all-occurrences，连那一套一起放弃。\n"
            "数据视图：open task/project/op 切换 task / project / op 三种数据模型；"
            "task 命令在任何视图都能敲；任务字段不校验，JSON 存在 data/tasks.json。\n"
            "设置视图：列出所有设置项和解释；set <id> <value> 改，"
            "edit-tomorrow / tomorrow-plan 见那里的说明。\n"
            "状态框（契约视图最上面）：契约执行情况 + 警告；warn off 可以藏掉警告。\n"
            "时段 ID（T1/T2）来自下载层 temp/calendar-download.json；"
            "同段后续行留空是正常的。[/]"
        )
        return False

    def cmd_help_task(self):
        """``help --task`` / ``help --data``：数据视图的详细说明（三模型、列、命令）。"""
        from rich.table import Table as _Table
        t = _Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True, justify="right")
        t.add_column()
        t.add_row("三个模型", "open task = 任务 Task · open project = 真正项目 Project（pro） · "
                              "open op = OmniFocus Project（op）；本界面顶部随选择切换")
        t.add_row("状态 / filter", "默认 filter=disposable 只显示 active；task / pro / op 被 "
                                  "`-m complete` 或 `-m discard` 后隐藏。filter all 显示全部，"
                                  "filter completed / dropped 可单独查看，filter active 恢复默认")
        t.add_row("默认列", "序号 · 标记 · title · project · 状态 · session · 截止 · 备注")
        t.add_row("默认折叠", "id · 重复 · trigger · formatter · Focusmate · OmniFocus · "
                              "完成时间 · 标签 · duration · count · exclusive · defer"
                              "（自定义字段默认也收进备注；用 open <表头> 展开）")
        t.add_row("标记", "🔁=重复子任务  🔁*=重复模板  📁=项目任务  🔗=OmniFocus  🎯=Focusmate  "
                          "⏳=挂载中  ⏱=formatter  ⚡=trigger  🔒=独占")
        t.add_row("状态", "·=进行中  ✓=完成  ✗=放弃  ⤼=转移走")
        t.add_row("备注缩写", "dur=分钟  cnt=已投/上限  excl=独占  tag=标签  "
                              "rep=重复  fmt=formatter  trig=trigger  note=备注")
        t.add_row("时间戳", "同天 HH:MM · 明天/昨天 +1/-1 HH:MM · 同年 MM/DD HH:MM")
        t.add_row("分页", "open 1 / open 2 / open 3 直接翻页；表下会显示 第 X/Y 页。"
                          "每页条数按终端高度自适应；长看板可用 PgUp/PgDn 帧内翻看")
        t.add_row("排序", "sort <字段> [asc|desc] · sort off；字段可用 id/title/project/"
                          "状态/session/截止/duration/count")
        t.add_row("分组", "group <字段> 或 group grouping(project) · group off；"
                          "分组列在最前面，同组首行显示组名、后续留空")
        t.add_row("搜索", "search <词> [--group <字段>] · search grouping(project) <词> · "
                          "search off；支持 field=value")
        t.add_row("三模型区别", "pro = 真正项目 Project（running/bottleneck + 关联文件） · "
                                "op = OmniFocus Project（SOP 或 incubator） · task = 单条任务")
        t.add_row("关联项目", "op（SOP）用标签 project/… 关联 pro；导入/刷新把叶子任务镜像进任务库"
                              "（fields.project），数据视图的 task 模型可搜索/group project；"
                              "plan 按项目当前阶段唤起对应 op。")
        t.add_row("任务库", self.task_store.path)
        p_detail = Panel(t, title="[bold]Data · 数据视图 / Task · 任务视图详细说明[/]",
                         border_style="cyan", box=box.ROUNDED)
        self.emit(p_detail)

        c = _Table.grid(padding=(0, 2))
        c.add_column(style="bold", no_wrap=True, justify="right")
        c.add_column()
        for cmd, desc in [
            ("task create [id] [字段=值 ...]", "新建任务；字段不校验"),
            ("task delete <序号|id:N>", "删除任务"),
            ("task modify <序号|id:N> <字段> <值>", "改任意字段"),
            ("task select <序号|id:N>", "选中任务"),
            ("task <序号>", "等价 task select"),
            ("task assign [序号] <session编号>", "挂到 session（自动建 Focusmate task）"),
            ("task <序号> -a <session|none|current>", "挂上/下架/挂当前场"),
            ("task <序号> -n <备注>", "顺手写备注"),
            ("task <序号> -c 5|+2|-1", "设 count / 加进度 / 减进度"),
            ("task <序号> -e true|false", "独占 / 取消独占"),
            ("task <序号> -m complete|discard", "只改本地状态（不碰外部系统）"),
            ("project <序号> -m complete|discard", "修改 pro 本地状态；默认 disposable filter 隐藏"),
            ("op <序号> -m complete|discard", "修改 op 本地状态；默认 disposable filter 隐藏"),
            ("filter disposable|all|completed|dropped", "数据视图过滤器；默认 disposable"),
            ("task <序号> done [分钟]", "完成（勾 Focusmate + 写回 OmniFocus）"),
            ("task <序号> drop [--local]", "放弃（默认也放弃 OmniFocus）"),
            ("task <序号> duplicate [-a N]", "复制一条任务"),
            ("task <序号> link [链接|PK]", "关联 OmniFocus"),
            ("task import [-st]", "读剪贴板导入 OmniFocus 任务/项目"),
            ("task <序号> -f end", "formatter：排到当天最后一段并自动挂"),
            ("task <序号> -f repeat 10:45", "formatter：固定挂到 10:45 那场已有 session"),
            ("task <序号> -t <preset> [参数]", "挂 trigger 预设"),
            ("task sync / archive [N]", "同步完成时间 / 看归档"),
            ("sort / group / search", "任务表排序 / 分组 / 搜索"),
            ("open <页码|表头> / close <表头>", "翻页 / 展开列 / 折叠列"),
            ("help --task", "本页"),
        ]:
            c.add_row(cmd, desc)
        p_cmd = Panel(c, title="[bold]Task · 命令（回车或敲任意命令关闭）[/]",
                      border_style="cyan", box=box.ROUNDED)
        self.help_overlay = Group(p_detail, p_cmd)
        self.invalidate()
        self.emit(p_cmd)
        self.emit("[dim]更多细节见 feature_intents/task.md；任务库字段不校验，"
                  "内部簿记见 store 文件。[/]")
        return False
