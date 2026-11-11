"""warden_core.net_mixin —— 异步网络队列（repl_kit.jobs）、worker 执行与主线程乐观落盘/回滚。"""
import datetime
import time

from .bridge import BridgeError
from .common import (
    FM_PENDING,
    IMPORT_PENDING,
    SRC_CALENDAR,
    SRC_FOCUSMATE,
    SRC_KEEP,
    get_warden_global,
    ks,
    of,
    pj,
    pp,
    repl_kit,
    rl,
    run_download_calendar,
    run_keep_session,
    sch,
    st,
    ts,
)
from .hints import task_repeat_plan_from_omnifocus


class NetMixin(object):
    # -- 网络：读也异步、写乐观（repl_kit.jobs）-------------------------
    #
    #     主线程                          worker 线程
    #     ────────                        ───────────
    #     乐观改本地（立刻看见）──submit──> bridge.call(...)   ← 真网络
    #     drain_net() ←──── outcome ─────  成功 / 失败
    #       ├ 成功：填上真 id / 真结果
    #       └ 失败：restore() 回滚 + 警告 + 记进失败账（re-action 重放）
    #
    # 三条规矩：
    #   1. **worker 只碰 bridge 和 rep**，任务库 / data.csv / 画面一律在主线程改；
    #   2. 每个动作都有 `名字 + 参数 + 怎么干 + 本地那半件事(apply)`，
    #      所以失败之后换个进程也能重放（re-action）；
    #   3. **多步事务不拆**：时段剪断、收尾、布局重写这些一次改好几场的内部写
    #      仍然同步 —— 中途失败回滚一半比不回滚更糟。

    def _net_touched(self):
        """worker 线程只说一句「画面脏了」。业务状态一律留给主线程。"""
        self._net_dirty = True

    def net_action(self, name, label, args=None, source=SRC_FOCUSMATE):
        """一条可重放的外部动作。``source`` 决定面板上的 ``[focusmate]`` 这种标签。"""
        return repl_kit.Action(name, label, dict(args or {}), source=source)

    def op_keep_session(self, argv, label, timeout=600, wait=False, on_ok=None):
        """跑一次 keep_session.py（下单/对账/启停 daemon）—— 走后台队列。

        ``wait=True`` 是给「对账」这种必须先有结果的流程用的（会等这一批落地）。
        """
        action = self.net_action("keep_session.run", label,
                                 {"argv": [str(a) for a in argv],
                                  "timeout": int(timeout)}, source=SRC_KEEP)
        job = self.jobs.submit(action, on_ok=on_ok)
        if wait:
            self.wait_net(timeout + 60)
        return job

    def op_calendar(self, argv, label, timeout=300, on_ok=None):
        """跑一次 download_calendar.py（日历 → data.csv）—— 走后台队列。"""
        action = self.net_action("calendar.sync", label,
                                 {"argv": [str(a) for a in argv],
                                  "timeout": int(timeout)}, source=SRC_CALENDAR)
        return self.jobs.submit(action, on_ok=on_ok)

    def drain_net(self):
        """每帧一次：把后台跑完的网络操作搬回主线程（成功/失败回调都在这里跑）。"""
        outcomes = self.jobs.drain()
        if outcomes or self._net_dirty:
            self._net_dirty = False
            self.invalidate()
        return outcomes

    def _register_net_actions(self):
        """给队列登记「名字 → 怎么干 / 本地怎么落」（re-action 靠这两半重放）。"""
        self.jobs.register("session.snapshot", self.net_snapshot,
                           apply=self._apply_snapshot)
        self.jobs.register("task.pull", self.net_task_pull,
                           apply=self._apply_task_pull)
        self.jobs.register("stat.history", self.net_stat_history,
                           apply=self._apply_stat_history)
        self.jobs.register("task.assign", self.net_task_assign,
                           apply=self._apply_task_assign)
        self.jobs.register("task.detach", self.net_task_detach,
                           apply=self._apply_task_detach)
        self.jobs.register("task.complete", self.net_task_complete,
                           apply=self._apply_task_complete)
        self.jobs.register("session.cancel", self.net_session_cancel,
                           apply=self._apply_session_cancel)
        # 取消/认罪/「不去了」这些动作以前有几条路会在提示符线程上直接等
        # 浏览器（console.status 转圈不算进度条，输入也一起冻住）。现在它们
        # 全走同一个后台队列：面板里有标题、步骤和进度，失败照样进失败账。
        self.jobs.register("session.missed", self.net_session_missed)
        self.jobs.register("session.confess_cancel", self.net_session_confess_cancel)
        self.jobs.register("session.relocate_cancel", self.net_session_relocate_cancel)
        self.jobs.register("session.mute", self.net_session_mute,
                           apply=self._apply_session_mute)
        self.jobs.register("session.title", self.net_session_title,
                           apply=self._apply_session_title)
        # ── OmniFocus（osascript 子进程：同样不属于「软件内部自己完成」）──
        self.jobs.register("omnifocus.list", self.net_omnifocus_list,
                           apply=self._apply_omnifocus_list)
        self.jobs.register("omnifocus.project_sync", self.net_omnifocus_project_sync)
        self.jobs.register("omnifocus.clipboard", self.net_omnifocus_clipboard)
        self.jobs.register("omnifocus.import", self.net_omnifocus_import,
                           apply=self._apply_omnifocus_import)
        self.jobs.register("omnifocus.complete", self.net_omnifocus_complete,
                           apply=self._apply_omnifocus_complete)
        self.jobs.register("omnifocus.drop", self.net_omnifocus_drop,
                           apply=self._apply_omnifocus_drop)
        self.jobs.register("omnifocus.writeback", self.net_omnifocus_writeback,
                           apply=self._apply_omnifocus_writeback)
        # ── 别的进程：keep_session（下单/对账/daemon）与日历同步 ──
        self.jobs.register("keep_session.run", self.net_keep_session,
                           apply=self._apply_keep_session)
        self.jobs.register("calendar.sync", self.net_calendar_sync,
                           apply=self._apply_calendar_sync)
        # 重放的时候没有当初那个闭包了 —— 失败仍然要有人说话
        self.jobs.on_failure = self._net_action_failed

    # -- worker 侧：只有 bridge 调用 + rep（日志/进度）------------------

    def net_snapshot(self, args, rep):
        """拉某一天的 session 列表。**读**操作：只回数据，不预测。"""
        day_iso = str(args.get("date") or self.sel_date.isoformat())
        rep.label("拉 session %s" % day_iso[5:])
        rep.progress(0.05)
        res = self.bridge.call("snapshot", timeout=int(args.get("timeout") or 180),
                               date=day_iso)
        rep.progress(1.0)
        rep.log("%s 拉到 %d 场" % (day_iso[5:], len(res.get("sessions") or [])), "ok")
        return {"date": day_iso, "res": res}

    def net_task_pull(self, args, rep):
        """拉 Focusmate task（完成状态）。**读**操作。"""
        rep.label("拉 Focusmate task")
        rep.progress(0.05)
        res = self.bridge.call("tasks", timeout=180)
        remote = res.get("tasks") if isinstance(res, dict) else res
        rep.progress(1.0)
        rep.log("Focusmate task %d 条" % len(remote or []), "ok")
        return {"remote": remote or []}

    def net_stat_history(self, args, rep):
        """拉生涯统计历史（只读公开 API，分块）。**读**操作。"""
        rep.label("拉生涯统计")
        rep.progress(0.05)
        history, note = st.load(force=bool(args.get("force")), full=bool(args.get("full")))
        rep.progress(1.0)
        try:
            count = len((history or {}).get("sessions") or [])
        except AttributeError:
            count = 0
        rep.log("生涯统计：%d 场（%s）" % (count, note), "ok")
        return {"history": history, "note": note}

    def net_task_assign(self, args, rep):
        """把任务挂到 Focusmate 那一场（**写**操作，乐观之后在后台跑的就是它）。"""
        rep.label("挂任务 #%s 到 %s" % (args.get("task_id"), args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("add_task", timeout=240, startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"),
                               title=args.get("title") or "",
                               notes=args.get("notes") or "")
        if not res.get("verified"):
            raise ValueError("Focusmate 里没能确认 task 已挂上（本地已回滚）")
        fm_id = res.get("taskId") or (res.get("task") or {}).get("id")
        if not fm_id:
            raise ValueError("Focusmate 说挂上了，但没给 task id")
        rep.progress(1.0)
        rep.log("任务 #%s 挂上了（Focusmate task %s）"
                % (args.get("task_id"), str(fm_id)[:8]), "ok")
        return {"task_id": fm_id, "via": res.get("via"),
                "existingCount": res.get("existingCount")}

    def net_task_detach(self, args, rep):
        """删掉 Focusmate 上那条 task（下架 / 转移）。**写**操作。"""
        fm_id = args.get("focusmate_task_id")
        if not fm_id:
            raise ValueError("这条任务没有 Focusmate task id，删不了")
        rep.label("删 Focusmate task %s" % str(fm_id)[:8])
        rep.progress(0.05)
        res = self.bridge.call("delete_task", timeout=180, taskId=fm_id)
        if not res.get("verified"):
            still = (res.get("stillThere") or {}).get("title") or ""
            raise ValueError("Focusmate 说删了，但复核还在%s"
                             % (("：" + still) if still else ""))
        rep.progress(1.0)
        rep.log("Focusmate task %s 已删除" % str(fm_id)[:8], "ok")
        return {"deleted": fm_id}

    def net_task_complete(self, args, rep):
        """把 Focusmate 那条 task 勾完成。**写**操作。"""
        fm_id = args.get("focusmate_task_id")
        if not fm_id:
            raise ValueError("这条任务没有 Focusmate task id，勾不了")
        rep.label("勾完成 #%s" % args.get("task_id"))
        rep.progress(0.05)
        res = self.bridge.call("complete_task", timeout=180, taskId=fm_id)
        if not res.get("verified"):
            raise ValueError("Focusmate 没确认勾上")
        rep.progress(1.0)
        rep.log("Focusmate task %s 已勾完成" % str(fm_id)[:8], "ok")
        return {"completed_at": res.get("completedAt") or ts._now(),
                "already": bool(res.get("already"))}

    def net_session_cancel(self, args, rep):
        """取消一场 session。**写**操作（浏览器可能冷启动几十秒，全在 worker 上）。"""
        rep.label("取消 %s %s" % (str(args.get("date") or "")[5:], args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("cancel", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"))
        rep.progress(0.9)
        if not res.get("verified"):
            raise ValueError("取消后复核：该 session 还在")
        rep.progress(1.0)
        rep.log("%s 那场已取消" % (args.get("start") or "?"), "ok")
        return {"cancelled": args.get("meeting_id")}

    def net_session_missed(self, args, rep):
        """把一场「到点没开始」记成 missed + abandoned（纯本地落盘，也放后台）。

        这个动作本身很快；放队列里是为了两件事：一是输入线程永远不等文件锁/磁盘，
        二是如果哪天 state 文件在慢盘/被杀进程卡住，面板至少明确显示在做哪一步。
        """
        key = str(args.get("key") or "")
        day_iso = str(args.get("date") or "")
        start = str(args.get("start") or "?")
        why = str(args.get("why") or
                  ("到点没开始，我选了「不去了」（%s %s）" % (day_iso, start)))
        rep.label("记录缺失 %s %s" % (day_iso[5:], start))
        rep.progress(0.1)
        try:
            ks.mark_abandoned(key, why="nostart", note=why)
        except Exception as e:                  # noqa: BLE001 - 记不上也别挡住下一步
            rep.log("记「已放弃」失败：%s" % e, "warn")
        rep.progress(0.55)
        try:
            ks.mark_slot(key, "missed", note="nostart")
        except Exception as e:                  # noqa: BLE001
            rep.log("记 missed 失败：%s" % e, "warn")
        rep.progress(1.0)
        rep.log("%s 已记为缺失（daemon 不再重订）" % start, "ok")
        return {"key": key, "why": why}

    def net_session_confess_cancel(self, args, rep):
        """认罪后把 daemon 已经重订回来的场次再取消掉。

        以前这一段在 ``confess()`` 里直接 ``bridge.call``，还要在一个
        ``console.status`` 里等浏览器 —— 宣言刚敲完画面像死了。现在拆成
        一个可重放的后台动作：逐场查快照、命中就取消、每步更新进度。
        """
        keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
        now = datetime.datetime.now()
        checked = cancelled = past = missing = 0
        failed = []
        total = max(1, len(keys))
        for index, key in enumerate(keys, 1):
            rep.progress((index - 1) / total)
            try:
                day = datetime.date.fromisoformat(key[:10])
                hhmm = key[11:]
                got = sch.parse_hhmm(hhmm)
            except (ValueError, TypeError):
                rep.log("%s 不是合法场次 key，跳过" % key, "warn")
                continue
            if not got:
                rep.log("%s 不是合法场次 key，跳过" % key, "warn")
                continue
            start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
            if start <= now:
                past += 1
                rep.log("%s 已经过去：只判放弃，不动 API" % key, "info")
                continue
            rep.label("查 %s 是否被重订" % key)
            try:
                res = self.bridge.call("snapshot", timeout=180, date=day.isoformat())
            except BridgeError as e:
                failed.append("%s 查不到现状：%s" % (key, e))
                rep.log("%s 查不到现状：%s" % (key, e), "warn")
                continue
            checked += 1
            hit = None
            for sess in res.get("sessions") or []:
                if sess.get("startLabel") == hhmm:
                    hit = sess
                    break
            if not hit:
                missing += 1
                rep.log("%s 现在 API 里没有（还没重订回来）" % key, "info")
                continue
            rep.label("取消重订的 %s" % key)
            try:
                out = self.bridge.call("cancel", startMs=hit.get("startMs"),
                                       meetingId=hit.get("id"))
            except BridgeError as e:
                failed.append("%s 取消失败：%s" % (key, e))
                rep.log("%s 取消失败：%s" % (key, e), "warn")
                continue
            if not out.get("verified"):
                failed.append("%s 取消后复核还在" % key)
                rep.log("%s 取消后复核还在" % key, "warn")
                continue
            cancelled += 1
            rep.log("%s 已真的取消（API 复核：没了）" % key, "ok")
            rep.progress(index / total)
        if failed:
            raise ValueError("；".join(failed))
        rep.progress(1.0)
        return {"keys": keys, "checked": checked, "cancelled": cancelled,
                "past": past, "missing": missing}

    def net_session_relocate_cancel(self, args, rep):
        """挪场次时批量取消旧 API session（一个动作 = 一条进度条）。"""
        moves = list(args.get("moves") or [])
        total = max(1, len(moves))
        done = []
        for index, move in enumerate(moves, 1):
            label = str(move.get("start") or "?")
            rep.label("取消旧 session %s" % label)
            rep.progress((index - 1) / total)
            res = self.bridge.call("cancel", timeout=180,
                                   startMs=move.get("startMs"),
                                   meetingId=move.get("meeting_id"))
            if not res.get("verified"):
                raise ValueError("旧 session %s 取消后复核还在" % label)
            done.append(move.get("meeting_id"))
            rep.log("旧 session %s 已取消" % label, "ok")
            rep.progress(index / total)
        return {"cancelled": done}

    def net_session_mute(self, args, rep):
        """改 quiet mode。**写**操作。"""
        want = bool(args.get("quiet"))
        rep.label("%s quiet %s" % (args.get("start") or "?", "ON" if want else "off"))
        rep.progress(0.05)
        res = self.bridge.call("mute", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"), quiet=want)
        if not res.get("verified"):
            raise ValueError("改完复核不一致：现在是 %s" % (res.get("quiet"),))
        rep.progress(1.0)
        rep.log("%s quiet = %s" % (args.get("start") or "?", "ON" if want else "off"), "ok")
        return {"quiet": bool(res.get("quiet"))}

    def net_session_title(self, args, rep):
        """写 session 标题。**写**操作。"""
        rep.label("写标题 %s" % (args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("title", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"),
                               title=args.get("title") or "")
        if not res.get("verified"):
            raise ValueError("写完复核不一致：现在是 %r" % (res.get("actual"),))
        rep.progress(1.0)
        rep.log("%s 标题已写入" % (args.get("start") or "?"), "ok")
        return {"title": args.get("title") or ""}

    # -- worker 侧：OmniFocus（osascript）-----------------------------

    def net_omnifocus_list(self, args, rep):
        """读一遍 OmniFocus 任务列表。**读**操作。"""
        rep.label("读 OmniFocus")
        rep.progress(0.1)
        rows = self.omnifocus.list_tasks(refresh=bool(args.get("refresh")))
        rep.progress(1.0)
        rep.log("OmniFocus %d 条" % len(rows or []), "ok")
        return {"rows": rows or []}

    def net_omnifocus_clipboard(self, args, rep):
        """读 macOS 剪贴板里的 OmniFocus 链接（**外部读**，不能在提示符线程跑）。"""
        rep.label("读剪贴板（OmniFocus）")
        rep.progress(0.1)
        items = [dict(item) for item in of.clipboard_links()]
        rep.progress(1.0)
        rep.log("剪贴板里拿到 %d 条 OmniFocus 链接" % len(items), "ok")
        return {"items": items}

    def net_omnifocus_import(self, args, rep):
        """``task import`` 的第 1 步：读 OmniFocus 补齐标题/时长 + 核对重复设置。

        **读**操作（一次 list + 每条一次 repetition_info）。真正的建任务、
        去重、问「复制重复设置吗」都在主线程那半件事里（见
        :meth:`_apply_omnifocus_import`）。
        """
        items = [dict(item) for item in (args.get("items") or []) if item]
        single_task = bool(args.get("single_task"))
        rep.label("获取任务（OmniFocus）")
        rep.progress(0.05)
        # 剪贴板也是外部世界（osascript / pbpaste）：在 worker 里读，别卡输入。
        if args.get("clipboard"):
            items = [dict(item, from_clipboard=True) for item in of.clipboard_links()]
            if not items:
                raise ValueError(
                    "剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本四个 flavor "
                    "都试过了）。在 OmniFocus 里 ⌘C 一条任务再回来敲 `task import`；"
                    "也可以手写 `task import [标题](omnifocus:///task/PK)`。")
        elif args.get("match_clipboard"):
            items = self._task_import_match_clipboard(items, of.clipboard_links())
        rep.progress(0.1)
        items = self._task_import_resolve(items, warn=lambda text, kind="warn":
                                          rep.log(text, kind))
        # 剪贴板里可能是**项目**（OmniFocus 的 project_root）：项目不进任务库，
        # 进项目库；时长不齐的项目直接拒绝（一条都不建），见 _task_import_projects。
        items, projects, refused = self._task_import_projects(
            items, warn=lambda text, kind="warn": rep.log(text, kind))
        if refused:
            rep.log("拒绝导入 %d 个项目（本周任务检查不通过）" % len(refused), "error")
            return {"items": [], "projects": [], "refused": refused,
                    "repeats": {}, "warnings": []}
        for payload in projects:
            week = payload.get("week") or {}
            rep.log("项目「%s」：%d 条任务（其中 %d 条已完成）· 本周会产出 %d 条"
                    % (payload.get("title"), len(payload.get("tasks") or []),
                       int(payload.get("done") or 0), int(week.get("count") or 0)),
                    "ok")
        rep.progress(0.5)
        repeats, warnings = {}, []
        checker = None if single_task else getattr(self.omnifocus, "repetition_info", None)
        for index, item in enumerate(items):
            if not item or not item.get("id") or checker is None:
                continue
            try:
                info = checker(item["id"])
            except Exception as e:                  # noqa: BLE001 - 核对不了就按普通任务导
                warnings.append("没法核对「%s」是不是重复任务：%s（先按普通任务导入）"
                                % (item.get("title") or item.get("id"), e))
                continue
            plan = task_repeat_plan_from_omnifocus(
                info, sessions_for_day=self.sessions_for_day_plan)
            if plan is not None:
                repeats[index] = {"info": info, "plan": plan}
            rep.progress(0.5 + 0.5 * (index + 1) / max(1, len(items)))
        rep.log("拿到 %d 条任务%s%s"
                % (len(items),
                   ("，其中 %d 条是重复任务" % len(repeats)) if repeats else "",
                   ("；另有 %d 个项目" % len(projects)) if projects else ""),
                "ok")
        return {"items": items, "projects": projects, "refused": [],
                "repeats": repeats, "warnings": warnings}

    def net_omnifocus_project_sync(self, args, rep):
        """每日项目对账的**读**侧：读一份 OmniFocus 清单，和本地项目快照做 diff。

        在 worker 里逐项目查重复任务规则，避免主线程被 osascript 卡住。返回的
        ``payload`` 已经带 ``week`` / 重复信息，主线程决定自动拉新增还是发起冲突问答。
        """
        local_projects = list(args.get("projects") or [])
        rep.label("每日检查 OmniFocus 项目（%d 个）" % len(local_projects))
        rep.progress(0.05)
        rows = self.omnifocus.list_tasks(refresh=True)
        by_pk = {str(row.get("id") or ""): row for row in rows or ()}
        out = []
        for index, local in enumerate(local_projects, 1):
            pk = str(((local or {}).get("omnifocus") or {}).get("id") or "")
            title = (local or {}).get("title") or pk
            rep.progress(0.05 + 0.9 * index / max(1, len(local_projects)))
            root = by_pk.get(pk)
            if root is None:
                payload = {
                    "kind": "project", "id": pk, "title": title,
                    "url": pj.project_url(pk), "tasks": [], "missing": [],
                    "depth_errors": [], "done": 0, "warnings": [],
                }
                out.append({"project_id": local.get("id"), "title": title,
                            "payload": payload,
                            "diff": pj.diff_tasks(local.get("tasks") or [], []),
                            "remote_found": False, "invalid": ""})
                continue
            try:
                payload = pj.project_payload(root, rows)
                payload = pj.attach_week(
                    payload, self._project_repeat_infos(
                        payload, lambda text, kind="warn": rep.log(text, kind)))
            except Exception as e:                  # noqa: BLE001 - 单个项目坏了不挡其余
                out.append({"project_id": local.get("id"), "title": title,
                            "payload": None, "diff": {},
                            "remote_found": True,
                            "invalid": "读项目失败：%s" % e})
                continue
            invalid = ""
            if payload.get("depth_errors"):
                invalid = pj.format_depth_errors(payload)
            elif payload.get("missing") or payload.get("week_missing"):
                invalid = pj.format_missing(payload)
            out.append({"project_id": local.get("id"), "title": title,
                        "payload": payload,
                        "diff": pj.diff_tasks(local.get("tasks") or [],
                                              payload.get("tasks") or []),
                        "remote_found": True, "invalid": invalid})
        rep.progress(1.0)
        rep.log("项目对账读完：%d 个" % len(out), "ok")
        return {"projects": out}

    def net_omnifocus_complete(self, args, rep):
        """写回完成 + 截止时间。**写**操作（乐观：本地先记 writeback）。"""
        pk = str(args.get("pk") or "")
        rep.label("OmniFocus 完成 %s" % pk[:8])
        rep.progress(0.1)
        res = self.omnifocus.complete(pk, at=args.get("at"), due=args.get("due"),
                                      yes=True)
        after = res.get("after") if isinstance(res, dict) else None
        advanced = bool(isinstance(res, dict) and res.get("repeat_clone_id")
                        and str(res.get("repeat_clone_id")) != pk)
        if isinstance(after, dict) and not after.get("completed") and not advanced:
            raise ValueError("写完复核 completed 仍为 false（pk %s）" % pk)
        rep.progress(1.0)
        rep.log("OmniFocus %s 完成写回%s" % (pk[:8], "（重复任务已翻篇）" if advanced else ""),
                "ok")
        return {"pk": pk, "res": res or {}, "advanced": advanced}

    def net_omnifocus_drop(self, args, rep):
        """写回放弃。**写**操作。"""
        pk = str(args.get("pk") or "")
        rep.label("OmniFocus 放弃 %s" % pk[:8])
        rep.progress(0.1)
        res = self.omnifocus.drop(pk, at=args.get("at"),
                                  all_occurrences=bool(args.get("all_occurrences")),
                                  yes=True)
        after = res.get("after") if isinstance(res, dict) else None
        advanced = bool(isinstance(res, dict) and res.get("repeat_clone_id")
                        and str(res.get("repeat_clone_id")) != pk)
        if isinstance(after, dict) and not after.get("dropped") and not advanced:
            raise ValueError("写完复核 dropped 仍为 false（pk %s）" % pk)
        rep.progress(1.0)
        rep.log("OmniFocus %s 已放弃" % pk[:8], "ok")
        return {"pk": pk, "res": res or {}, "advanced": advanced}

    def net_omnifocus_writeback(self, args, rep):
        """把「Focusmate 说完成了」的那几条一次性写回 OmniFocus。**写**操作。

        一条一次子进程，所以进度是「第几条 / 共几条」；单条失败不挡其余。
        """
        task_ids = [int(t) for t in (args.get("task_ids") or [])]
        done, failed = [], []
        rep.label("写回 OmniFocus（%d 条）" % len(task_ids))
        for index, task_id in enumerate(task_ids, 1):
            try:
                task = self.task_store.get(task_id)
            except (ValueError, TypeError):
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            rep.progress(index / max(1, len(task_ids)))
            try:
                res = self.omnifocus.complete(pk, at=None,
                                              due=self.omni_due_of(task) or None, yes=True)
            except of.OmniFocusError as exc:
                failed.append((task_id, str(exc)))
                rep.log("任务 %s 写回失败：%s" % (task_id, exc), "warn")
                continue
            done.append({"task_id": task_id, "pk": pk, "res": res or {}})
            rep.log("任务 %s → OmniFocus %s" % (task_id, pk[:8]), "ok")
        if not done and failed:
            raise ValueError("全部写回失败：%s" % failed[0][1])
        return {"done": done, "failed": failed}

    # -- worker 侧：别的进程（keep_session / 日历）---------------------

    def net_keep_session(self, args, rep):
        """跑一次 ``keep_session.py``（下单/对账/启停 daemon）。**外部**操作。

        ``release_browser``：在跑子进程前先让 bridge 放开 Chromium profile。
        这一步以前在主线程用 ``_release_browser()`` 做，浏览器卡住时输入也卡住；
        现在和真正的 check 一起在 worker 上，进度条会依次显示两步。
        """
        argv = [str(a) for a in (args.get("argv") or [])]
        if args.get("release_browser"):
            rep.label("释放浏览器给 daemon")
            rep.progress(0.05)
            try:
                up, browser, _ = self.bridge.browser_state()
                if up and browser:
                    self.bridge.call("release", timeout=60)
                    rep.log("浏览器已释放", "ok")
            except BridgeError as e:
                rep.log("释放浏览器失败（继续跑，可能撞锁）：%s" % e, "warn")
        rep.label("keep_session %s" % (" ".join(argv) or "check"))
        rep.progress(0.1)
        rc, out, err = get_warden_global("run_keep_session", run_keep_session)(*argv, timeout=int(args.get("timeout") or 600))
        rep.progress(1.0)
        if rc != 0:
            raise ValueError("keep_session %s 退出码 %d：%s"
                             % (" ".join(argv), rc, (err or out or "").strip()[:200]))
        rep.log("keep_session %s 完成" % (" ".join(argv) or "check"), "ok")
        return {"rc": rc, "out": out, "err": err, "argv": argv}

    def net_calendar_sync(self, args, rep):
        """跑 ``download_calendar.py``（日历 → data.csv）。**外部**操作。"""
        argv = [str(a) for a in (args.get("argv") or ["download"])]
        rep.label("日历 %s" % " ".join(argv))
        rep.progress(0.1)
        rc, out, err = get_warden_global("run_download_calendar", run_download_calendar)(*argv,
                                            timeout=int(args.get("timeout") or 300))
        rep.progress(1.0)
        if rc != 0:
            raise ValueError("download_calendar %s 退出码 %d：%s"
                             % (" ".join(argv), rc, (err or out or "").strip()[:200]))
        rep.log("日历 %s 完成" % " ".join(argv), "ok")
        return {"rc": rc, "out": out, "err": err, "argv": argv}

    # -- 主线程侧：成功之后把本地落定（apply）--------------------------

    def _apply_snapshot(self, payload, args):
        """session 列表到了：换成真数据（读操作，没有回滚这回事）。"""
        payload = payload or {}
        res = payload.get("res") or {}
        now_ms = res.get("now") or int(time.time() * 1000)
        api_rows = []
        for sess in res.get("sessions") or []:
            row = dict(sess)
            row["phase"] = self.phase_of(sess["startMs"], sess["endMs"], now_ms)
            api_rows.append(row)
        self.api_rows = api_rows
        self.rows_source = "bridge"
        self.rows_note = ""
        self.rows_at = time.time()
        self.read_at["session"] = self.rows_at
        self.rebuild_rows(now_ms)
        self.invalidate()
        return len(self.rows)

    def _apply_task_pull(self, payload, args):
        """Focusmate task 到了：对账完成状态（本地写 + 写回 OmniFocus）。"""
        remote = (payload or {}).get("remote") or []
        started = time.monotonic()
        before = len(self.task_store.load_archive().get("records") or [])
        try:
            archived, matched = self.task_store.reconcile(remote)
        except Exception as e:                     # noqa: BLE001 - 对账坏了别掀 TUI
            self.had_error = True
            self.say("Focusmate task 对账失败：%s" % e, "error")
            return 0
        self.task_sync_at = time.time()
        self.read_at["task"] = self.task_sync_at
        summary = self._focusmate_task_summary(remote)
        summary.update({
            "source": "net.task_pull",
            "matched": matched,
            "archived": archived,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        })
        rl.log_event("focusmate", "task_pull_result", **summary)
        if archived:
            self.announce_completions(before)
        self.writeback_completions()
        # reconcile() 改了任务状态；契约表 task 列里的 ✓/✗ 是 rebuild_rows()
        # 算出来的缓存，不重算就会一直显示同步前的旧状态（用户报的
        # 「任务都删了/勾了，session 上还显示旧任务」就是这一层缓存没拍掉）。
        self.rebuild_rows()
        self.say("Focusmate task 同步：匹配 %d 条，新增完成归档 %d 条。"
                 % (len(matched), archived), "ok")
        self.invalidate()
        return archived

    def _apply_stat_history(self, payload, args):
        """统计历史到了：算数字（纯本地 CPU，放主线程没关系）。"""
        payload = payload or {}
        history, note = payload.get("history"), payload.get("note") or ""
        try:
            self.stat = st.compute_stats(history)
        except Exception as e:                      # noqa: BLE001 - 数据变形的兜底
            self.had_error = True
            self.stat_note = "数据没法算：%s" % e
            self.say("生涯统计算不动：%s" % e, "error")
            return 0
        self.stat_note = note
        self.stat_at = datetime.datetime.now()
        self.read_at["stat"] = time.time()
        self.say("生涯统计：%s（完成 %d 场 / %d 天）"
                 % (note, self.stat["total_completed"], self.stat["days"]),
                 "ok" if history else "warn")
        self.invalidate()
        return 1

    def _apply_task_assign(self, payload, args):
        """挂上了：把乐观的「在途」换成真 task id（重放时同样走这里）。"""
        payload = payload or {}
        task_id = args.get("task_id")
        session = dict(args.get("session") or {})
        fm_id = payload.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        session["task_id"] = fm_id
        current = task.get("session") or {}
        same = (current.get("date") == session.get("date")
                and current.get("start") == session.get("start"))
        if current and not same:
            # 排进队列之后这条任务又被挪到别处了：这次落地不许把它拽回来
            self.jobs._log_line("任务 %s 已经挂到 %s %s，这次落地跳过"
                                % (self.task_label(task), current.get("date"),
                                   current.get("start")), "warn")
            return task["id"]
        # 已经是同一场就别再 assign 一次：assign 会整体覆盖 session 字典，
        # 把 previous_due / previous_session 这些留档冲掉。
        if not same:
            self.task_store.assign(task["id"], session)
        self.task_store.attach_focusmate(task["id"], {
            "task_id": fm_id,
            "session_id": args.get("meeting_id"),
            "attached_at": ts._now(),
            "title": args.get("title"),
            "via": payload.get("via"),
        }, session=None)
        self.task_store.settle_focusmate(task["id"], FM_PENDING)   # 落地了：撤掉在途标记
        # 重复任务的子任务落地之后，把本体 occurrence 里的 Focusmate 信息也换
        # 成真 id（materialize_repeats 现在是乐观队列，不再当场等复核）。
        if task.get("repeat_parent") and task.get("repeat_date"):
            try:
                current = self.task_store.get(task["id"])
                self.task_store.record_occurrence(
                    task["repeat_parent"], task["repeat_date"],
                    child_id=task["id"],
                    session=current.get("session") or session,
                    focusmate=current.get("focusmate") or None)
            except (ValueError, TypeError):
                pass
        self.selected_task_id = task["id"]
        self.rebuild_rows()
        remote_had = payload.get("existingCount")
        total = (remote_had if isinstance(remote_had, int) else 0) + 1
        extra = ("（这场现在共 %d 个任务，表格里会一起列出来）" % total) if total > 1 else ""
        via = payload.get("via")
        self.say("✅ 任务 %s 已挂上；Focusmate task id=%s%s%s"
                 % (self.task_label(task), fm_id,
                    "（via %s）" % via if via else "", extra), "ok")
        self.invalidate()
        return task["id"]

    def _apply_task_detach(self, payload, args):
        """删掉了：本地断绑定（重放时同样走这里）。"""
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        current = task.get("session") or {}
        if current and (current.get("date") != args.get("date")
                        or current.get("start") != args.get("start")):
            # 排进队列之后它又挂到别处去了：这次「下架」不该动新位置
            self.jobs._log_line("任务 %s 已经挂到 %s %s，这次下架跳过"
                                % (self.task_label(task), current.get("date"),
                                   current.get("start")), "warn")
            return task_id
        try:
            task = self.task_store.unassign(task_id, reason=args.get("reason") or "net-detach")
        except (ValueError, TypeError):
            return None
        self.rebuild_rows()
        self.say("⏏ 任务 %s 已从 %s %s 下架"
                 % (self.task_label(task), (args.get("date") or "?"),
                    args.get("start") or "?"), "ok")
        self.invalidate()
        return task["id"]

    def _apply_task_complete(self, payload, args):
        """勾上了：本地记 completed_at。"""
        payload = payload or {}
        try:
            task = self.task_store.get(args.get("task_id"))
        except (ValueError, TypeError):
            return None
        self.task_store.attach_focusmate(task["id"], {
            "completed_at": payload.get("completed_at") or ts._now()})
        self.task_store.settle_focusmate(task["id"], FM_PENDING)
        self.invalidate()
        return task["id"]

    def _apply_session_cancel(self, payload, args):
        """取消了：那一行从表格里消失（乐观时已经先拿掉；重放时在这儿拿掉）。"""
        self._forget_api_row(args.get("meeting_id"))
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_session_mute(self, payload, args):
        self._patch_api_row(args.get("meeting_id"), quiet=bool((payload or {})
                                                              .get("quiet")))
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_session_title(self, payload, args):
        self._patch_api_row(args.get("meeting_id"),
                            title=(payload or {}).get("title") or "")
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_omnifocus_import(self, payload, args):
        """第 1 步回来了：去重 → （可能要问重复设置）→ 建本地任务 → 套后缀。

        「套后缀」就是 ``task import -a 3`` 里的 ``-a 3`` —— 它会**再排一步**
        （``[focusmate] 挂任务``），所以一次 import 在面板上就是两步。
        """
        payload = payload or {}
        items = payload.get("items") or []
        repeats = {int(k): v for k, v in (payload.get("repeats") or {}).items()}
        warnings = payload.get("warnings") or []
        suffix = dict(args.get("suffix") or {})
        skipped = []
        # 项目：要么「时长不齐 → 整批拒绝」，要么写进项目库（不进任务库）。
        refused = payload.get("refused") or []
        if refused:
            self.had_error = True
            for payload_item in refused:
                self.say("✗ " + (pj.format_missing(payload_item)
                                 or pj.format_depth_errors(payload_item)
                                 or payload_item.get("reason")
                                 or "没法导入这个项目。"), "error")
            if any(payload_item.get("depth_errors") for payload_item in refused):
                self.say("🚫 这次导入被拒：一条任务、一个项目都没建。先去 OmniFocus "
                         "把三层任务改成最多父-子两层，再 ⌘C 重新 `task import`。",
                         "error")
            else:
                self.say("🚫 这次导入被拒：一条任务、一个项目都没建。先去 OmniFocus "
                         "给上面那几条设「持续时间」（预计时长），再 ⌘C 重新 "
                         "`task import`。", "error")
            self._last_import_result = None
            self.jobs.end()
            return None
        imported = self._apply_project_imports(
            payload.get("projects") or [],
            suffix=suffix,
            op_type=args.get("op_type"),
            advance_pro_id=args.get("advance_pro_id"),
        )
        # 「已经导入过」只在**批量**里跳过：一次选十条里混着上个月的旧条目才是误伤；
        # 单条导入是「我就要这条」的明确动作（再来一条同名关联是合法用法）。
        if len(items) > 1:
            items, skipped = self._task_import_skip_known(items)
            if skipped and items:
                self.say("📥 跳过已经导入过的 %d 条：%s"
                         % (len(skipped), "、".join("%s→任务 %s" % (pk, tid)
                                                   for pk, tid in skipped)), "info")
        if not items:
            if imported:
                # 只有项目、没有普通任务：项目已经进库了，没有「空导入」这回事。
                self.jobs.end()
                return imported[0]
            detail = "、".join("%s → 任务 %s" % (pk, tid) for pk, tid in skipped)
            self.say("📥 这一批都已经导入过了（%s）；没有新建任何任务。" % detail
                     if detail else "📥 这一批没有可导入的条目。", "warn")
            self.jobs.end()
            return None
        if repeats:
            self._last_import_result = IMPORT_PENDING
            self.pending_import = {"items": items, "repeats": repeats,
                                   "warnings": [], "suffix": suffix}
            titles = "、".join((items[i] or {}).get("title") or (items[i] or {}).get("id")
                               or "?" for i in sorted(repeats))
            self.say("📥 发现 OmniFocus 重复任务：%s —— 先确认要不要复制重复设置，"
                     "还没建本地任务。" % titles, "cmd")
            self.invalidate()
            self.jobs.end()          # 等用户答 y/n，这一次操作到此为止
            return IMPORT_PENDING
        task = self._finish_task_import(items, copy_repeat=False, repeats={},
                                        warnings=warnings)
        if suffix:
            try:
                task = self._apply_import_suffix(task, suffix) or task
            except Exception as e:                  # noqa: BLE001 - 建好了但没套上后缀
                self.had_error = True
                self.say("⚠ 任务导入建好了，但后缀（%s）没套上：%s"
                         % (self._suffix_summary(suffix).strip() or "?", e), "error")
        # 同步模式（--exec / 测试）里 cmd_task_import 要把「建出来的任务」还给
        # 调用方；异步模式没人等这个返回值（后缀已经在上面套完了）。
        self._last_import_result = task
        return task

    def _project_local_task(self, project_id, project_task_pk):
        """按 ``(项目序号, 项目任务 pk)`` 找本地镜像任务；没有返回 None。"""
        want_pk = str(project_task_pk or "").strip()
        if not want_pk:
            return None
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if pk == want_pk and str(pid) == str(project_id):
                return task
        return None

    def sync_project_tasks(self, project, quiet=True):
        """把项目库里的**叶子任务**镜像进本地任务库（幂等）。

        这样任务视图的搜索 / ``group project`` / 排序里能直接找到项目任务，而
        契约视图的任务面板不会因此被几十条备选任务淹掉：这些镜像任务带
        ``source.kind == "project"``、``fields.project_task``、且没有 session，
        会被 :meth:`task_panel_tasks` 当成「项目备选」跳过；等 ``plan`` 真正
        唤起它们时，就复用同一条本地任务并挂上 session。
        """
        project = project or {}
        try:
            pid = int(project.get("id"))
        except (TypeError, ValueError):
            return []
        title = project.get("title") or ""
        tasks = project.get("tasks") or []
        kids = pj.child_map(tasks)
        statuses = pj.effective_task_statuses(tasks)
        made = []
        for item in tasks:
            pk = str(item.get("pk") or "").strip()
            if not pk or kids.get(pk):
                continue
            status = statuses.get(pk, pj.STATUS_ACTIVE)
            local = self._project_local_task(pid, pk)
            if status == pj.STATUS_ACTIVE:
                task, created = self.task_store.ensure_project_task(pid, title, item)
                made.append((task, created))
                continue
            # OmniFocus 那边已经完成/放弃了：本地还开着的镜像跟着收口，
            # 但已经挂过 session 的任务不动（那是当天的真实账）。
            if local is not None and ts.TaskStore.is_open(local) and not local.get("session"):
                target = (ts.STATUS_COMPLETED if status == pj.STATUS_COMPLETED
                          else ts.STATUS_DROPPED)
                self.task_store.set_status(local["id"], target, reason="project-sync")
        if made and not quiet:
            created = sum(1 for _task, is_new in made if is_new)
            self.say("📥 项目「%s」任务已入库：%d 条叶子任务%s（任务视图可 "
                     "`group project` / `search 项目名` 找到）"
                     % (title, len(made),
                        "（新建 %d）" % created if created else ""), "ok")
        return made

    def sync_all_project_tasks(self, quiet=True):
        """所有 active 项目都镜像一遍（TUI 启动时补历史项目）。"""
        made = []
        try:
            projects = self.projects.active()
        except Exception:                             # noqa: BLE001 - 坏项目库不挡启动
            return []
        for project in projects:
            made.extend(self.sync_project_tasks(project, quiet=quiet))
        return made

    def _apply_project_imports(self, payloads, suffix=None, op_type=None,
                               advance_pro_id=None):
        """把项目 payload 写进**项目库**，并把叶子任务镜像进任务库。

        项目库仍是「母体」；任务库里的镜像只用于任务视图的搜索 / 分组 / 排序，
        没有 session 时不会进契约视图的任务面板。等 `plan` 给项目分了 count、
        真正唤起任务时，复用同一条镜像并挂上 session。
        第二次导入同一个项目 = 刷新它的任务清单。
        """
        if advance_pro_id is None and self.pending_advance is not None:
            advance_pro_id = self.pending_advance.get("pro_id")
            if not op_type:
                op_type = self.pending_advance.get("required_op_type")
        norm_type = pj.normalize_op_type(op_type)
        made = []
        for payload in payloads or []:
            if not payload:
                continue
            item_payload = dict(payload)
            if norm_type:
                item_payload["op_type"] = norm_type
            if advance_pro_id is not None:
                item_payload["pro_id"] = advance_pro_id
            try:
                project, created = self.projects.upsert(item_payload)
            except ValueError as e:
                self.had_error = True
                self.say("⚠ 项目没写进库：%s" % e, "error")
                continue
            made.append(project)
            num = self.projects.number_of(project["id"]) or project["id"]
            tasks = project.get("tasks") or []
            open_tasks = [t for t in tasks
                          if not t.get("completed") and not t.get("dropped")]
            done_tasks = [t for t in tasks if t.get("completed")]
            dropped_tasks = [t for t in tasks if t.get("dropped")]
            stat = pj.stats(project, self.task_store)
            head = "📁 导入项目" if created else "🔄 刷新项目"
            type_label = pj.normalize_op_type(project.get("op_type")) or "未设类型"
            pro = self.projects.pro_for_op(project)
            pro_label = (" · 关联项目「%s」(%s)"
                         % (self.pro_display_title(pro, fallback=pro.get("title")),
                            pro.get("stage")) if pro else "")
            self.say("%s #%d「%s」[%s%s] → OmniFocus 项目 %s"
                     % (head, num, project.get("title"), type_label, pro_label,
                        (project.get("omnifocus") or {}).get("id") or "?"), "ok")
            self.say("   %d 条任务（%d 条未完成、%d 条已完成、%d 条已放弃）"
                     " · 共 %d count = %d 分钟 · 剩余 %d count（进度 %s）"
                     % (len(tasks), len(open_tasks), len(done_tasks),
                        len(dropped_tasks), stat["total"],
                        stat["total"] * pp.MINUTES_PER_COUNT,
                        stat["remaining"], stat["progress_text"]), "info")
            week = pj.weekly_yield(tasks)
            if week.get("count"):
                self.say("   " + pj.week_summary(week), "info")
            if week.get("missing"):
                self.say("   ⚠ 本周任务里还有 %d 条没设持续时间（导入时本应被挡下，"
                         "`project %d refresh` 再看一次）。"
                         % (len(week["missing"]), num), "warn")
            self.sync_project_tasks(project, quiet=False)
            self.say("   任务清单已镜像进任务库（任务视图可按项目分组/搜索）；"
                     "没挂 session 前先躺着、不占契约面板，`plan` 分给这个项目 count "
                     "时才会挑出来挂 session。", "info")
        if made and (suffix or {}):
            # 项目自己不挂 session（母体永远是母体），-a / -f 这些后缀对它没有意义。
            self.say("· 项目不吃后缀（%s）：它自己不占 session，等 `plan` 唤起任务时再"
                     "挂。" % self._suffix_summary(suffix).strip(), "info")
        if made and advance_pro_id is not None:
            self._finish_pro_advance(advance_pro_id, made[-1]["id"])
        elif made and any(pj.normalize_op_type(p.get("op_type")) is None for p in made):
            self.check_untyped_ops(announce_import=True)
        if made:
            self.invalidate()
        return made

    def _apply_omnifocus_list(self, payload, args):
        """读到了：把行塞进 client 的缓存，调用方自己取（import / 对账用）。"""
        rows = (payload or {}).get("rows") or []
        try:
            self.omnifocus.remember_rows(rows)
        except Exception:                       # noqa: BLE001 - 老版本/替身没有这个方法
            pass
        self.invalidate()
        return rows

    def _apply_omnifocus_complete(self, payload, args):
        payload = payload or {}
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        advanced, moved = self._omni_repeat_advance(task, payload.get("pk") or "",
                                                   payload.get("res") or {})
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "completed")
        except ValueError:
            pass
        self.say("✅ OmniFocus %s 完成写回%s"
                 % (str(payload.get("pk") or "")[:8],
                    self._omni_repeat_note(advanced, (payload.get("res") or {})
                                          .get("repeat_clone_id"), moved)), "ok")
        self.invalidate()
        return task["id"]

    def _apply_omnifocus_drop(self, payload, args):
        payload = payload or {}
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        advanced, moved = self._omni_repeat_advance(task, payload.get("pk") or "",
                                                   payload.get("res") or {})
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "dropped")
        except ValueError:
            pass
        self.say("✅ OmniFocus %s 已放弃%s"
                 % (str(payload.get("pk") or "")[:8],
                    self._omni_repeat_note(advanced, (payload.get("res") or {})
                                          .get("repeat_clone_id"), moved)), "ok")
        self.invalidate()
        return task["id"]

    def _apply_omnifocus_writeback(self, payload, args):
        payload = payload or {}
        advanced_notes = []
        for item in payload.get("done") or []:
            task_id = int(item["task_id"])
            old_pk = str(item.get("pk") or "")
            try:
                task = self.task_store.get(task_id)
            except (ValueError, TypeError):
                continue
            advanced, moved = self._omni_repeat_advance(
                task, old_pk, item.get("res") or {})
            if advanced:
                advanced_notes.append(self._omni_repeat_note(
                    advanced, (item.get("res") or {}).get("repeat_clone_id"), moved))
            try:
                self.task_store.set_omnifocus_writeback(task_id, "completed")
            except (ValueError, TypeError):
                pass
            # 同一 occurrence 的其他已完成本地镜像也已经由这一次写回归属，
            # 不再各自去 complete 一遍。还开着的兄弟记录已由 advance 改挂下一次。
            if old_pk:
                for other in self.task_store.list():
                    if other.get("id") == task_id:
                        continue
                    if ts.TaskStore.omnifocus_id(other) != old_pk:
                        continue
                    if self.task_store.status_of(other) != ts.STATUS_COMPLETED:
                        continue
                    wb = ((other.get("omnifocus") or {}).get("writeback") or {})
                    if wb.get("completed"):
                        continue
                    try:
                        self.task_store.set_omnifocus_writeback(
                            other["id"], "completed")
                    except (ValueError, TypeError):
                        pass
        count = len(payload.get("done") or [])
        failed = payload.get("failed") or []
        if count:
            suffix = "".join(note for note in advanced_notes if note)
            self.say("✅ OmniFocus 写回 %d 条完成%s%s"
                     % (count, ("；%d 条失败" % len(failed)) if failed else "",
                        suffix), "ok" if not failed else "warn")
        self.invalidate()
        return count

    def _apply_keep_session(self, payload, args):
        payload = payload or {}
        out = (payload.get("out") or "").strip()
        for line in out.splitlines()[-6:]:
            if line.strip():
                self.say(line.strip()[:160], "info")
        self.forget_caches()
        self.invalidate()
        return payload.get("rc")

    def _apply_calendar_sync(self, payload, args):
        payload = payload or {}
        argv = payload.get("argv") or []
        verb = str(argv[0]).lower() if argv else "download"
        out = (payload.get("out") or "").strip()
        for line in out.splitlines()[-6:]:
            if line.strip():
                self.say(line.strip()[:160], "info")
        if verb == "populate":
            self.say("✅ data.csv 已同步（下载层 → 时段 layout → 精确日期契约）。", "ok")
        else:
            self.say("✅ 日历已下载到中间层（工作/空闲）。", "ok")
        self.reload_entries()
        self.refresh(quiet=True)
        self.invalidate()
        return payload.get("rc")

    def _net_action_failed(self, action, error):
        """没有专属失败钩子的失败（重放时的就是这种）：说话 + 记账就够了。"""
        self.had_error = True
        self.say("❌ %s 失败：%s（已记进失败账，re-action 可以重放）"
                 % (action.label, error), "error")
        self.invalidate()

    # -- 小工具：乐观写操作的「本地那半件事」和它的反面 ----------------

    def _patch_api_row(self, meeting_id, **fields):
        """改表格里那一行的本地字段（不动 data.csv，等 refresh 覆盖）。"""
        if not meeting_id:
            return None
        for row in self.api_rows:
            if row.get("id") == meeting_id:
                row.update(fields)
                return row
        return None

    def _forget_api_row(self, meeting_id):
        """把一行从本地表格里拿掉（取消的乐观预测）。返回被拿掉的那行和位置。"""
        if not meeting_id:
            return None
        for index, row in enumerate(self.api_rows):
            if row.get("id") == meeting_id:
                return index, self.api_rows.pop(index)
        return None

    def _restore_api_row(self, before_row, index=None):
        """把一行（或它原来的位置）放回本地表格 —— 乐观行操作的失败回滚。"""
        if not before_row:
            return None
        meeting_id = before_row.get("id")
        existing = self._patch_api_row(meeting_id, **dict(before_row)) if meeting_id else None
        if existing is None:
            if index is None or index > len(self.api_rows):
                self.api_rows.append(dict(before_row))
            else:
                self.api_rows.insert(index, dict(before_row))
        self.rebuild_rows()
        self.invalidate()
        return before_row

    def _row_op_failed(self, what, before_row, error, index=None):
        """行级乐观操作失败：把那一行放回原样，说清楚，记账照旧。"""
        self._restore_api_row(before_row, index=index)
        self.had_error = True
        self.say("↩ 回滚：%s 没成功（%s）；表格已经退回操作前。" % (what, error), "warn")

    @staticmethod
    def _fm_pending(task):
        """这条任务的 Focusmate 挂载是不是「乐观预测、还没落地」。"""
        return ((task or {}).get("focusmate") or {}).get(FM_PENDING) or None

    def _revert_task(self, task_id, snapshot, why):
        """把一条任务整体放回操作前的快照，并且说实话。"""
        try:
            self.task_store.restore(task_id, snapshot)
        except (ValueError, TypeError):
            pass
        self.rebuild_rows()
        self.invalidate()
        self.had_error = True
        self.say("↩ 回滚：%s（本地已经退回操作前）" % why, "warn")
