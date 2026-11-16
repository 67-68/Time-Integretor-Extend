"""warden_core.derived_mixin —— note 标题清单派生任务（需求契约见 feature_intents/derived.md）。

一句话：一条任务的 note 第一行写了

    @list
    - a
    - b

（或者任务标题以 `@list` 开头，note 里直接写 bullet 列表）那它就是一串
**一次做一条**的清单：`task 3 -d`（或 plan）把下一条标题变成一条
本地派生任务；做完之后把 note 里那行勾上（`- [x] a`），而不是完成原本的任务；
全部勾完之后自动收掉容器（重复任务 → 放弃整套，普通任务 → 完成）。

**不需要**它是重复任务：`@list` 记号就是全部条件。旧写法 `# titles` 继续认。

没有清单、但**是重复任务**时，`-d` 走另一条路：复制一条和本体一样的任务
（同标题 / count / 时长 / 项目），可以无限复制（合成 pk `<父>#c<序号>`），
也不写回 OmniFocus。

派生任务**没有** OmniFocus 关联：它是本地任务，完成时只写回父任务的 note。
"""
from __future__ import annotations

from .common import (
    pj,
    rl,
    ts,
    dv,
)


class DerivedMixin(object):
    # ---------------------------------------------------------------- 初始化
    def _derived_init(self):
        """把派生任务挂到任务库的状态观察者上（唯一的总钩子）。"""
        self.task_store.after_status = self._derived_after_status
        self._derived_span_map_cache = None

    # ---------------------------------------------------------------- 取父任务
    def derived_project_task(self, task):
        """本地任务 → 它在项目库里的那条项目任务 ``(project, item)``。

        ``fields.project_task`` 可能是派生任务的合成 pk（``<父>#d<行>``），
        查项目清单前要还原成父任务的真实 pk。
        """
        fields = (task or {}).get("fields") or {}
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        if not pk:
            return None, None
        parent_pk, _line = dv.decode_pk(pk)
        if parent_pk:
            pk = parent_pk
        pid = fields.get(pj.PROJECT_ID_FIELD)
        try:
            project = self.projects.by_id(pid)
        except Exception:                       # noqa: BLE001 - 展示/命令兜底
            project = None
        if project is None:
            return None, None
        return project, pj.task_by_pk(project, pk)

    def derived_parent_by_pk(self, pk):
        """OmniFocus primary key → 父任务视图（本地任务 / 项目清单里找）。"""
        want = str(pk or "").strip()
        if not want:
            return None
        for task in self.task_store.list():
            if dv.is_derived(task):
                continue
            if ts.TaskStore.omnifocus_id(task) != want:
                continue
            return self._derived_view(task, note=self.task_store.notes(task) or "")
        try:
            store = self.projects.load()
        except Exception:                       # noqa: BLE001
            store = {}
        for project in (store.get("projects") or ()):
            item = pj.task_by_pk(project, want)
            if item is None:
                continue
            return {"pk": want,
                    "title": item.get("title") or want,
                    "repeat": item.get("repeat") or {},
                    "note": item.get("note") or "",
                    "project": project,
                    "item": item,
                    "local_id": None}
        return None

    def derived_container_repeat(self, pk):
        """容器任务的重复规则：本地任务上没带就去项目清单里找。

        本地任务可能是「旧数据 / 手工建的」，`repeat` 是空的；项目清单里那条
        才是真源。收尾要按「有没有重复」决定放弃整套还是标完成，所以这里
        两个来源都问一遍。
        """
        want = str(pk or "").strip()
        if not want:
            return {}
        view = self.derived_parent_by_pk(want) or {}
        repeat = view.get("repeat") or {}
        if dv.has_repeat(repeat):
            return repeat
        if view.get("item") is not None:
            got = view["item"].get("repeat") or {}
            if dv.has_repeat(got):
                return got
        try:
            store = self.projects.load()
        except Exception:                       # noqa: BLE001 - 查不到就当不重复
            store = {}
        for project in (store.get("projects") or ()):
            item = pj.task_by_pk(project, want)
            if item is None:
                continue
            got = item.get("repeat") or {}
            if dv.has_repeat(got):
                return got
        return repeat

    def derived_parent_of(self, task):
        """任意本地任务 → 「它对应的那条重复任务」视图；找不到 → ``None``。"""
        info = dv.derivation(task)
        if info and info.get("parent_pk"):
            return self.derived_parent_by_pk(info["parent_pk"])
        project, item = self.derived_project_task(task)
        if item is not None:
            view = self._derived_view(task, note=item.get("note") or "")
            view["repeat"] = item.get("repeat") or {}
            view["title"] = item.get("title") or view["title"]
            view["project"] = project
            view["item"] = item
            return view
        return self._derived_view(task, note=self.task_store.notes(task) or "")

    def _derived_view(self, task, note=""):
        fields = (task or {}).get("fields") or {}
        return {"pk": ts.TaskStore.omnifocus_id(task),
                "title": str(fields.get("title") or "").strip(),
                "repeat": task.get("repeat") or {},
                "note": note,
                "project": None,
                "item": None,
                "local_id": (task or {}).get("id")}

    def derived_rotation(self):
        """``{父 pk: 最近一次完成派生任务的时间}`` —— parallel 轮转用的账。"""
        out = {}
        for task in self.task_store.list():
            info = dv.derivation(task)
            if not info or not info.get("parent_pk"):
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            stamp = str(task.get("status_at") or task.get("updated_at") or "")
            pk = info["parent_pk"]
            if stamp > out.get(pk, ""):
                out[pk] = stamp
        return out

    def derived_span_map(self):
        """``{父 pk: "25-125"}`` —— 界面上的 count 范围（整份清单一次算好）。"""
        cache = getattr(self, "_derived_span_map_cache", None)
        if cache is not None:
            return cache
        out = {}
        try:
            store = self.projects.load()
        except Exception:                       # noqa: BLE001 - 展示兜底
            store = {}
        for project in (store.get("projects") or ()):
            for item in project.get("tasks") or ():
                if not dv.eligible(item.get("note") or "", item.get("title") or ""):
                    continue
                base = ts.parse_count(item.get("count"))
                opened = len(dv.open_items(dv.parse(item.get("note") or "",
                                                    item.get("title") or "")))
                span = dv.count_span(base, opened)
                if span:
                    out[str(item.get("pk") or "")] = span
        self._derived_span_map_cache = out
        return out

    def derived_count_span(self, task):
        """这条任务在界面上该显示的 count 范围（``25-125``）；不是就 ``None``。"""
        if not isinstance(task, dict) or dv.is_derived(task):
            return None
        fields = task.get("fields") or {}
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        if not pk:
            return None
        parent, _line = dv.decode_pk(pk)
        return self.derived_span_map().get(parent or pk)

    def derived_state(self, task):
        """父任务的状态：``{"pk","title","note","repeat","eligible","items","open",...}``。"""
        view = self.derived_parent_of(task)
        if not view:
            return None
        parsed = dv.parse(view.get("note") or "", view.get("title") or "")
        view["items"] = parsed["items"]
        view["open"] = parsed["open"]
        view["total"] = parsed["total"]
        view["eligible"] = bool(dv.eligible(view.get("note") or "",
                                            view.get("title") or ""))
        return view

    def derived_is_parent(self, task):
        """这条任务是不是「@list 清单」容器（-ac 要拒绝的那类）。"""
        state = self.derived_state(task)
        if not state:
            return False
        return bool(dv.parse(state.get("note") or "",
                             state.get("title") or "")["found"])

    def derived_note_of(self, pk):
        """父任务 note 的**当前**文本：项目清单优先（项目同步会刷它），其次本地任务。"""
        want = str(pk or "").strip()
        if not want:
            return ""
        try:
            store = self.projects.load()
        except Exception:                       # noqa: BLE001
            store = {}
        for project in (store.get("projects") or ()):
            item = pj.task_by_pk(project, want)
            if item is not None and (item.get("note") or "").strip():
                return item.get("note") or ""
        for task in self.task_store.list():
            if ts.TaskStore.omnifocus_id(task) == want and not dv.is_derived(task):
                return self.task_store.notes(task) or ""
        return ""

    # ---------------------------------------------------------------- 创建派生任务
    def derived_create(self, parent, which=None):
        """把父任务的下一条未勾选标题变成一条本地派生任务，并挂到最近的 session。

        ``which``：可选，``1`` = 第几条未勾选（1 起），或 note 里的行号+1。
        返回新任务；不合格就抛 :class:`ValueError` 说清楚原因。
        """
        state = self.derived_state(parent)
        if state is None:
            raise ValueError("任务 %s 找不到对应的项目任务（先 refresh / project sync）。"
                             % self.task_label(parent))
        title = state.get("title") or ""
        note = state.get("note") or ""
        repeat = (self.derived_container_repeat(state.get("pk"))
                  or state.get("repeat") or {})
        items = dv.open_items(dv.parse(note, title))
        if not items:
            if dv.has_marker(note, title):
                raise ValueError("「%s」的标题都勾完了 —— 没东西可派生。"
                                 % (title or state.get("pk")))
            if dv.has_repeat(repeat):
                return self._derived_copy(parent, state, repeat)
            raise ValueError("「%s」既没有 `@list` 清单（note 第一行 / 标题前面），"
                             "也不是重复任务 —— 没东西可派生。"
                             % (title or state.get("pk")))
        if not state.get("pk"):
            raise ValueError("任务 %s 没有 OmniFocus 关联 —— note 写不回去，不能派生。"
                             "先 `task %s link` 关联那条任务。"
                             % (self.task_label(parent), self.task_label(parent)))
        # 已经在做的行不再派生第二条：连着敲 `-d` 应该依次拿到 12、22、32。
        busy = {int((dv.derivation(task) or {}).get("line"))
                for task in self.task_store.list()
                if (dv.derivation(task) or {}).get("parent_pk") == state["pk"]
                and self.task_store.status_of(task) == ts.STATUS_ACTIVE
                and (dv.derivation(task) or {}).get("line") is not None}
        items = [one for one in items if int(one["line"]) not in busy]
        if not items:
            raise ValueError("「%s」的标题都已经有派生的任务在做了（等等它们收工，"
                             "或者先把之前的放弃）。" % (state["title"] or state["pk"]))
        item = items[0]
        if which is not None:
            chosen = self._derived_pick(items, which)
            if chosen is None:
                raise ValueError("`-d %s` 对不上任何一条未勾选标题（现在剩 %d 条）。"
                                 % (which, len(items)))
            item = chosen
        # count / duration 继承父任务：一条派生 = 一份完整的「这个重复任务」。
        count = None
        minutes = None
        if state.get("item") is not None:
            count = ts.parse_count((state["item"] or {}).get("count"))
            minutes = ts.parse_minutes((state["item"] or {}).get("duration"))
        if count is None:
            count = ts.task_count(parent)
        if minutes is None:
            minutes = ts.task_minutes(parent)
        pk = dv.synthetic_pk(state["pk"], item["line"])
        fields = {"title": dv.derived_title(state["title"], item["text"]),
                  dv.F_DERIVED_LINE: int(item["line"]),
                  dv.F_DERIVED_TEXT: item["text"],
                  dv.F_DERIVED_PARENT_PK: state["pk"],
                  dv.F_DERIVED_PARENT_TITLE: state["title"]}
        if state.get("local_id"):
            fields[dv.F_DERIVED_OF] = int(state["local_id"])
        if count:
            fields["count"] = int(count)
            fields["count_total"] = int(count)
        if minutes:
            fields["duration"] = int(minutes)
        if state.get("item") is not None and state.get("project") is not None:
            project = state["project"]
            fields[pj.PROJECT_FIELD] = project.get("title") or ""
            fields[pj.PROJECT_ID_FIELD] = project.get("id")
            fields[pj.PROJECT_TASK_FIELD] = pk
        source = {"kind": dv.DERIVED_KIND,
                  "parent": state.get("local_id"),
                  "line": int(item["line"]),
                  "text": item["text"],
                  "pk": state["pk"],
                  "parent_title": state["title"]}
        task = self.task_store.create(fields, source=source)
        said = "🧬 派生任务 %s「%s」（父任务 note 第 %d 行）" % (
            self.task_label(task), item["text"], item["line"] + 1)
        task = self._derived_attach(task, said)
        rl.log_event("derived", "create", pk=state["pk"], line=item["line"],
                     task_id=task["id"], title=item["text"])
        return task

    def _derived_copy(self, parent, state, repeat=None):
        """重复任务的「再来一条」：标题 / count / 时长和本体一样，可无限派生。"""
        pfields = dict((parent or {}).get("fields") or {})
        item = state.get("item") or {}
        project = state.get("project")
        title = str(pfields.get("title") or state.get("title") or "").strip()
        count = ts.parse_count(pfields.get("count"))
        if count is None:
            count = ts.parse_count(item.get("count"))
        minutes = ts.parse_minutes(pfields.get("duration"))
        if minutes is None:
            minutes = ts.parse_minutes(item.get("duration"))
        pk = str(state.get("pk") or "").strip()
        seq = self._derived_copy_seq(pk)
        spk = dv.copy_pk(pk or "local", seq)
        fields = {"title": title,
                  dv.F_DERIVED_PARENT_PK: pk,
                  dv.F_DERIVED_PARENT_TITLE: state.get("title") or title,
                  dv.F_DERIVED_COPY: seq}
        if (parent or {}).get("id") is not None:
            fields[dv.F_DERIVED_OF] = int(parent["id"])
        if count:
            fields["count"] = int(count)
            fields["count_total"] = int(count)
        if minutes:
            fields["duration"] = int(minutes)
        if project is not None:
            fields[pj.PROJECT_FIELD] = (project.get("title")
                                        or pfields.get(pj.PROJECT_FIELD) or "")
            fields[pj.PROJECT_ID_FIELD] = project.get("id")
            fields[pj.PROJECT_TASK_FIELD] = spk
        elif pfields.get(pj.PROJECT_TASK_FIELD):
            # 项目清单里找不到（本地重复任务）：保留来源项目，只换合成 pk。
            fields[pj.PROJECT_FIELD] = pfields.get(pj.PROJECT_FIELD) or ""
            fields[pj.PROJECT_ID_FIELD] = pfields.get(pj.PROJECT_ID_FIELD)
            fields[pj.PROJECT_TASK_FIELD] = spk
        source = {"kind": dv.COPY_KIND,
                  "parent": (parent or {}).get("id"),
                  "seq": seq,
                  "pk": pk,
                  "parent_title": title}
        task = self.task_store.create(fields,
                                      tags=(parent or {}).get("tags") or (),
                                      source=source)
        said = "🧬 派生副本 %s「%s」（第 %d 条，和本体一样；重复任务不写回）" % (
            self.task_label(task), title or pk, seq)
        task = self._derived_attach(task, said)
        rl.log_event("derived", "copy", pk=pk, seq=seq,
                     task_id=task["id"], title=title)
        return task

    def _derived_copy_seq(self, pk):
        """下一条副本的序号：同一父任务递增，**没有上限**。"""
        want = str(pk or "").strip()
        seq = 0
        for task in self.task_store.list():
            if not dv.is_copy(task):
                continue
            fields = task.get("fields") or {}
            if str(fields.get(dv.F_DERIVED_PARENT_PK) or "") != want:
                continue
            try:
                seq = max(seq, int(fields.get(dv.F_DERIVED_COPY) or 0))
            except (TypeError, ValueError):
                continue
        return seq + 1

    def _derived_attach(self, task, said):
        """派生任务的新建收尾：选中它、尽量挂到最近的 session、说一声。"""
        self.selected_task_id = task["id"]
        attached = ""
        try:
            idx, row = self.current_session()
            task = self._assign_task(task, str(idx), refresh=False,
                                     allow_unbooked=True)
            attached = "；已挂到 session #%d（%s %s）" % (
                idx, row.get("startLabel"), row.get("phase") or "")
        except ValueError as exc:
            attached = "；没挂 session（%s）" % exc
        self.say(said + attached, "ok")
        return task

    @staticmethod
    def _derived_pick(items, which):
        """``-d N`` → 第 N 条未勾选（1 起）或 note 行号+1。"""
        try:
            want = int(which)
        except (TypeError, ValueError):
            return None
        if 1 <= want <= len(items):
            return items[want - 1]
        for item in items:
            if item["line"] + 1 == want:
                return item
        return None

    # ---------------------------------------------------------------- 完成 → 勾 note
    def _derived_after_status(self, task, before):
        """任务库状态观察者：派生任务完成 → 勾父任务 note 里那行。"""
        if str((task or {}).get("status") or "") != ts.STATUS_COMPLETED:
            return
        info = dv.derivation(task)
        if not info or not info.get("parent_pk"):
            return
        fields = task.get("fields") or {}
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or dv.synthetic_pk(
            info["parent_pk"], info["line"] or 0))
        total = ts.parse_count(fields.get("count_total")) or ts.task_count(task) or 0
        cap = ts.task_count(task) or 0
        if total and cap and cap < total:
            # plan 拆出来的 part：整条派生任务的所有 part 都干完才勾。
            try:
                got = int((pj.invested(self.task_store) or {}).get(pk) or 0)
            except Exception:                   # noqa: BLE001 - 账算不出来不挡勾
                got = total
            if got < total:
                rl.log_event("derived", "partial", pk=info["parent_pk"],
                             line=info["line"], task_id=task.get("id"),
                             got=got, total=total)
                self.say("🧬 派生任务「%s」这一场做完了，但整条还差 %d count；"
                         "note 那行先不勾。" % (info.get("text") or task.get("id"),
                                               total - got), "info")
                return
        self._derived_mark_line(info, task)

    def _derived_mark_line(self, info, task=None):
        """把 note 那行勾上：先改本地缓存，再排一条 note_mark 写回 OmniFocus。"""
        pk = str(info.get("parent_pk") or "").strip()
        line = info.get("line")
        text = str(info.get("text") or "").strip()
        if not pk or line is None or not text:
            return
        title = info.get("parent_title") or self.derived_parent_title(pk) or pk
        note = self.derived_note_of(pk)
        new_note, changed = dv.mark_checked(note, int(line))
        if changed:
            self._derived_store_note(pk, new_note)
        remaining = (dv.parse(new_note or note, title)["open"]
                     if (new_note or note) else None)
        queued = ""
        outbox = getattr(self, "omni_outbox", None)
        if outbox is not None:
            outbox.put({"id": pk, "op": "note_mark", "match": text,
                        "task_id": (task or {}).get("id")})
            queued = "；已排队写回 note（空闲 / refresh 时批量写）"
        elif self.omnifocus_enabled():
            try:
                self.omnifocus.note_mark(pk, text)
                queued = "；已写回 note"
            except Exception as exc:            # noqa: BLE001 - 外部写失败只说一句
                queued = "；note 写回失败：%s" % exc
        self.say("🧬 「%s」done → 父任务「%s」note 勾上「%s」%s"
                 % (info.get("text") or title, title, text, queued), "ok")
        rl.log_event("derived", "mark", pk=pk, line=line, text=text,
                     remaining=remaining, task_id=(task or {}).get("id"))
        if remaining is not None and remaining <= 0:
            self._derived_finish(pk, title)

    def derived_parent_title(self, pk):
        """父任务标题（本地 / 项目清单里找）；找不到返回空串。"""
        view = self.derived_parent_by_pk(pk)
        return (view or {}).get("title") or ""

    def _derived_store_note(self, pk, note):
        """把 note 的新文本写进本地两份缓存：项目清单里的任务 + 本地父任务。"""
        note = str(note or "")
        self._derived_span_map_cache = None
        try:
            store = self.projects.load()
        except Exception:                       # noqa: BLE001
            store = {}
        for project in (store.get("projects") or ()):
            item = pj.task_by_pk(project, pk)
            if item is None:
                continue
            if (item.get("note") or "") != note:
                try:
                    self.projects.set_task_note(project["id"], pk, note)
                except Exception:               # noqa: BLE001 - 缓存更新失败不挡
                    pass
            break
        for task in self.task_store.list():
            if ts.TaskStore.omnifocus_id(task) != pk or dv.is_derived(task):
                continue
            try:
                self.task_store.set_notes(task["id"], note)
            except Exception:                   # noqa: BLE001
                pass

    def _derived_after_mark(self, pk, note, remaining):
        """note_mark 写回成功之后：更新缓存；清单勾完 → 放弃整个重复任务。"""
        note = str(note or "")
        if note:
            self._derived_store_note(pk, note)
        title = self.derived_parent_title(pk) or ""
        try:
            left = (int(remaining) if remaining is not None
                    else dv.parse(note, title)["open"])
        except (TypeError, ValueError):
            left = dv.parse(note, title)["open"]
        if left <= 0:
            self._derived_finish(pk, self.derived_parent_title(pk) or pk)

    def _derived_finish(self, pk, title=""):
        """整份清单勾完：收掉容器 —— 重复任务放弃整套，普通任务标完成。"""
        pk = str(pk or "").strip()
        if not pk:
            return
        title = title or self.derived_parent_title(pk) or pk
        repeating = dv.has_repeat(self.derived_container_repeat(pk))
        local_status = ts.STATUS_DROPPED if repeating else ts.STATUS_COMPLETED
        what = "放弃整个重复任务" if repeating else "完成这条清单任务"
        outbox = getattr(self, "omni_outbox", None)
        parent_id = None
        for task in self.task_store.list():
            if ts.TaskStore.omnifocus_id(task) != pk or dv.is_derived(task):
                continue
            if parent_id is None:
                parent_id = task["id"]
            if self.task_store.status_of(task) == ts.STATUS_ACTIVE:
                self.task_store.set_status(task["id"], local_status,
                                           reason="derived-done")
        if outbox is not None:
            if repeating:
                outbox.put({"id": pk, "op": "drop", "task_id": parent_id,
                            "all_occurrences": True})
                note = "；已排队放弃 OmniFocus 里整套重复（空闲 / refresh 时写回）"
            else:
                outbox.put({"id": pk, "op": "complete", "task_id": parent_id})
                note = "；已排队完成 OmniFocus 里这条清单（空闲 / refresh 时写回）"
        elif self.omnifocus_enabled():
            try:
                if repeating:
                    self.omnifocus.drop(pk, all_occurrences=True)
                    note = "；已在 OmniFocus 放弃整套重复"
                else:
                    self.omnifocus.complete(pk)
                    note = "；已在 OmniFocus 完成这条清单"
            except Exception as exc:            # noqa: BLE001
                note = "；OmniFocus 写回失败：%s" % exc
        else:
            note = "（OmniFocus 关着，只停了本地）"
        for task in self.task_store.list():
            if dv.is_derived(task) and (dv.derivation(task) or {}).get("parent_pk") == pk:
                if self.task_store.status_of(task) == ts.STATUS_ACTIVE:
                    self.task_store.drop(task["id"], reason="derived-series-done")
        self.say("🧬 标题清单勾完了 → %s「%s」%s" % (what, title, note), "ok")
        rl.log_event("derived", "finish", pk=pk, title=title, repeating=repeating)
        self.rebuild_rows()
        self.invalidate()
