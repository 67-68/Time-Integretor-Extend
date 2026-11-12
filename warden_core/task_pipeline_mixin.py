"""warden_core.task_pipeline_mixin —— formatter（end 切场）、排期流水线校验、挂上/下架/调换/批量转移与每周重复任务生成。"""
import copy
import datetime
import time

from .common import (
    FM_PENDING,
    TASK_SESSION_CURRENT,
    TASK_SESSION_NONE,
    pj,
    sch,
    sp,
    th,
    ts,
)
from .shell import WardenShell


class TaskPipelineMixin(object):
    def _formatter_session_has_actual(self, day, session):
        """这一天这一场在 API 里有没有真实 id（权威模式下才算数）。"""
        if not self.authoritative:
            return True
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        start = str((session or {}).get("start") or "").strip()
        if not start:
            return False
        try:
            for row in self._snapshot_rows(iso):
                if (row.get("id")
                        and str(row.get("startLabel") or "") == start):
                    return True
        except Exception:                     # noqa: BLE001 - 行坏了就按没有处理
            return False
        return False

    def _formatter_bound_session_missing(self, task, day):
        """formatter 任务绑的那场**已经开始或过去**，但 API 里根本没有它。

        未来还没订上的场次允许先挂本地；但已经开始/结束的场次如果没有真实
        session id，就说明它被取消了 —— 任务不能继续假装还挂在那里。
        """
        if not self.authoritative:
            return False
        sess = (task or {}).get("session") or {}
        if not sess:
            return False
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if str(sess.get("date") or "") != iso:
            return False
        start = self._session_start_datetime(sess)
        if start is None or start > self._now_dt():
            return False
        sid = str(sess.get("focusmate_session_id") or "").strip()
        if sid:
            try:
                if any(str(r.get("id") or "") == sid for r in self.rows):
                    return False
            except Exception:                 # noqa: BLE001
                pass
        return not self._formatter_session_has_actual(day, sess)

    def _formatter_sessions(self, day, strict_actual=False):
        """formatter 的候选 session：先走日历下载层 + layout，再回退到 data.csv。

        ``strict_actual=True`` 是 refresh 流水线：权威模式下，**今天已经开始/
        过去、但 API 里没有实际 id 的本地计划场**不再是候选。否则一场被取消的
        21:00 session 永远躺在 data.csv 里，formatter 每轮都把它当目标重新挂
        任务，任务就永远“还挂在取消的场上”。手动 `task -f` 仍允许排，
        由调用方决定是否接受本地未订上的未来/过去窗。
        """
        sessions = []
        try:
            sessions = [dict(s) for s in self.sessions_for_day_plan(day)]
        except Exception:                     # noqa: BLE001 - 下载层坏了不该挡住 csv
            sessions = []
        if sessions:
            if strict_actual and self.authoritative and day <= datetime.date.today():
                now = self._now_dt()
                sessions = [
                    sess for sess in sessions
                    if not (self._session_start_datetime(sess) is not None
                            and self._session_start_datetime(sess) <= now
                            and not self._formatter_session_has_actual(day, sess))
                ]
            return sessions
        try:
            self.reload_entries()
            for slot in sch.day_slots(self.entries, day):
                start = slot["start"]
                duration = int(slot["duration"])
                hour, minute = map(int, start.split(":"))
                total = hour * 60 + minute + duration
                sessions.append({
                    "date": day.isoformat(), "start": start,
                    "end": "%02d:%02d" % ((total // 60) % 24, total % 60),
                    "start_min": hour * 60 + minute, "end_min": total,
                    "duration": duration, "period_id": None,
                })
        except Exception:                     # noqa: BLE001 - csv 也坏了就没有候选
            sessions = []
        return sessions

    def _exclusive_blockers(self, date_iso, start, task=None):
        """这一场里「不许再塞人」的任务；没有就是空列表。

        规则只有一条：**一场 session 里只要有独占任务，就别再往里放第二条** ——
        不管是把独占任务放进已经有人的场，还是往有独占任务的场里再塞一条。
        """
        if not date_iso or not start:
            return []
        others = [t for t in self.task_store.tasks_for_session(date_iso, start)
                  if task is None or t.get("id") != task.get("id")]
        others = [t for t in others if self.task_store.is_open(t)]
        if not others:
            return []
        exclusive_others = [t for t in others if th.is_exclusive(t)]
        if exclusive_others:
            return exclusive_others
        return others if th.is_exclusive(task or {}) else []

    def _exclusive_guard(self, task, date_iso, start):
        """挂之前先看这一场：有独占任务就别再塞第二条（抛 ValueError 给人话）。

        ``_exclusive_blockers`` 只说「谁挡着」，这里负责把它翻译成用户能照着做的
        一句话 —— 换一场，或者先 ``task <序号> -e false``。
        """
        blockers = self._exclusive_blockers(date_iso, start, task)
        if not blockers:
            return
        names = "、".join(
            "%s「%s」" % (self.task_label(t),
                         self.task_store.focusmate_title(t, 20) or "?")
            for t in blockers)
        if th.is_exclusive(task or {}):
            raise ValueError(
                "任务 %s 是 🔒 独占的，而 %s %s 这一场已经有 %s 了 —— 一场只能有一条任务。"
                "换一场，或先 `task %s -e false` 放开独占。"
                % (self.task_label(task), date_iso, start, names,
                   self.task_label(task)))
        raise ValueError(
            "%s %s 这一场已经有 🔒 独占任务 %s —— 一场只能有一条任务。"
            "换一场，或先 `task <序号> -e false` 放开那条的独占。"
            % (date_iso, start, names))

    def _ensure_carved_session(self, day, chosen, task=None):
        """把 formatter **切出来的那一场**作为一次性 session 写进当天排期。

        这是**兜底**路径：正常的切割走 :meth:`_cut_period_for_carve`（直接改写那个
        时段的 layout，连旧场一起取消掉）。只有排布表达不了的情况才落到这里 ——
        切点和「前半截打包完的终点」之间隔着 5/20 分钟（Focusmate 只认 15 分钟
        整点，填不上），例如 16:45–18:00 的时段要切 17:30 那场。

        这一场必须真的落进 ``temp/session-extras.json`` + data.csv：否则任务挂不上
        （没有 session id）、daemon 也不会去订它，那段时间就白白空着。

        返回 None（没造/不需要）或一句话（造了什么）。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        start = str(chosen.get("start") or "").strip()
        duration = int(chosen.get("duration") or 0)
        if not start or duration <= 0:
            return None
        already = [s for s in self.sessions_for_day_plan(day) if s.get("start") == start]
        if already:
            return None                        # 同起点那场已经有了，复用，不重复造
        note = "formatter end 切的（任务 %s）" % (
            self.task_label(task) if task is not None else "?")
        try:
            sp.add_extra(day, start, duration, note=note)
        except ValueError as exc:
            self.say("⚠ 没能把切出来的 %s %dmin 写进当天排期：%s（任务照样挂，"
                     "只是这一场没有计划）" % (start, duration, exc), "warn")
            return None
        if self.rewrite_data_csv_from_plan():
            self.say("➕ 当天额外排了一场：%s %s %dmin（切出来的，已写进 data.csv）"
                     % (day.isoformat(), start, duration), "ok")
        else:
            self.reload_entries()
        return "切出来的 %s %dmin 这一场已经写进当天排期" % (start, duration)

    def _now_dt(self):
        """当前时间；测试可把 ``clock_now`` 钉死，生产环境跟墙上时钟。"""
        return self.clock_now or datetime.datetime.now()

    @staticmethod
    def _session_start_datetime(row):
        """从一行 session（API / 本地绑定 / 快照）里取**实际开始时间**。

        优先用 API 的 ``startMs`` —— 这是 Focusmate 真实约定的开始时刻；
        计划里的 ``startLabel`` 可能和它差几分钟（用户说的「时间偏差」）。
        只有没有 ``startMs`` 时才退回 ``date + startLabel/start``。
        无法解析返回 None（调用方按「已开始」保守处理）。
        """
        if not isinstance(row, dict):
            return None
        raw_ms = row.get("startMs")
        if raw_ms not in (None, ""):
            try:
                ms = float(raw_ms)
                # 1 / 1234 这类占位值不是真实 epoch 毫秒（1970 年），退回
                # date + startLabel；真实 Focusmate API 的 startMs 一定远大于此。
                if ms >= 946684800000:            # 2000-01-01T00:00:00Z
                    return datetime.datetime.fromtimestamp(ms / 1000.0)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        raw_day = row.get("date") or row.get("period_date")
        raw_hhmm = row.get("startLabel") or row.get("start")
        if raw_day in (None, "") or raw_hhmm in (None, ""):
            return None
        try:
            if hasattr(raw_day, "year"):
                day = datetime.date(raw_day.year, raw_day.month, raw_day.day)
            else:
                day = datetime.date.fromisoformat(str(raw_day)[:10])
            got = sch.parse_hhmm(str(raw_hhmm))
            if not got:
                return None
            return datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        except (TypeError, ValueError):
            return None

    @classmethod
    def _session_already_started(cls, row, now=None):
        """这一场是否已经开始（含刚好到点）；解析不了也按已开始保护。"""
        now = now or datetime.datetime.now()
        start = cls._session_start_datetime(row)
        return start is None or start <= now

    @classmethod
    def _session_in_progress(cls, row, now=None):
        """这一场是否**此刻正在**进行；已结束的过去场次不再保护。"""
        now = now or datetime.datetime.now()
        start = cls._session_start_datetime(row)
        if start is None:
            return True
        try:
            minutes = int((row or {}).get("duration") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            minutes = 25
        return start <= now < start + datetime.timedelta(minutes=minutes)

    def _split_started_sessions(self, rows):
        """把待处理场次拆成 (未开始可取消, 已开始必须保护)。

        **refresh / formatter / 改排布都不得取消已经开始或过去的 session。**
        判定按 API 实际 ``startMs``，不是按表格的 zone / 计划 startLabel；
        这样即使当前 session 因时间偏差被显示成 locked/draft，也不会误杀。
        """
        now = self._now_dt()
        protected, safe = [], []
        for row in rows or []:
            (protected if self._session_already_started(row, now) else safe).append(row)
        if protected:
            labels = "、".join(
                str(row.get("startLabel") or row.get("start") or "?")
                for row in protected[:5])
            if len(protected) > 5:
                labels += " 等"
            self.say("⚠ %d 场 session 已经开始或已过去（%s）；refresh / 自动排布"
                     "不会取消它们（按 API 实际 startMs 判定，不看计划时间偏差）。"
                     % (len(protected), labels), "warn")
        return safe, protected

    def _apply_cut_layout(self, day, period, durations):
        """按 formatter 算出来的时长序列重排这个时段（可能不是选中的那天）。

        整串步骤照抄唯一事务 :meth:`commit_layout`（先取消计划里不再包含的旧
        API session、再写 layout、再清动过的场次上的任务绑定、最后重算 data.csv）。
        ``book=False``：预约由命令层（``_apply_formatter``）统一触发，这里不新造
        外部动作 —— 这条方法也会被 refresh 的排布流水线间接调用。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        period_id = period.get("id")
        result = self.commit_layout(
            day, period, list(durations or []),
            mode="set" if durations else "remove",
            reason="formatter-cut", book=False, announce=False,
            missing_ok=True)
        new_sessions = result.get("sessions") or []
        cancelled = result.get("cancelled") or []
        dropped = result.get("cleared") or []
        if not result.get("ok", True):
            self.say("✂ %s %s 的排布没改成：旧场取消失败，保持原样。"
                     % (iso, period_id), "error")
            return "排布没改成"
        self.reload_periods()
        self.reload_entries()
        body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                         for s in new_sessions) or "（这个时段当天不排了）"
        self.say("✂ %s %s 的排布剪断成：%s —— 原来那场不再存在"
                 % (iso, period_id, body), "ok")
        if cancelled:
            self.say("   %s %d 场旧 session（计划里不再包含它们）。"
                     % ("已在 Focusmate 取消" if self.jobs.sync
                        else "已排进后台取消",
                        len(cancelled)), "info")
        if dropped:
            self.say("   有 %d 条任务因为那几场没了而下架（任务本身还在，"
                     "`task <序号> -f end` 可以重新排）。" % len(dropped), "warn")
        return body

    def _cut_period_for_carve(self, day, chosen, task=None):
        """把 `end` 切出来的那一场**钉进当天排期**：原来那场剪断，只留新的一两场。

        用户原话：「切出来的 11:30 那场应该导致原来那场 11:00–11:50 的剪断（实际上
        就是取消 + 约定一个 25 分钟的）…… 原来的 11:00–11:50 不显示，仅显示新的两场。」

        所以这里不玩「两场并存 + 警告」，直接把那个时段的**当天排布**改成
        「前半截 + 切出来的这一场」：

            11:00–12:00 的时段、原来一场 50 分钟、任务 25 分钟
            → 11:00–11:25（空着）+ 11:30–11:55（这一场）

        排布表达不了时（例如前半截打完包，最后一场落在 11:25，而切割点是 11:30
        —— Focusmate 只认 15 分钟整点，中间那 5 分钟没法用一场 session 填），就
        只把前半截写进 layout，切出来的那场单独作为**一次性 session** 补上。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        period_id = chosen.get("cut_period_id")
        start = str(chosen.get("start") or "").strip()
        fit = int(chosen.get("duration") or 0)
        prefix = [int(d) for d in (chosen.get("cut_prefix") or [])]
        period = None
        for probe in self.periods_for_day(day):
            if probe.get("id") == period_id:
                period = probe
                break
        if period is None or not start or fit <= 0:
            # 没有时段信息（比如 data.csv 手写模式）：退回「只补一场」的老路。
            return self._ensure_carved_session(day, chosen, task=task)

        try:
            planned = sp.plan_period(period, prefix + [fit])
        except ValueError:
            planned = []
        exact = bool(planned) and str(planned[-1]["start"]) == start
        durations = (prefix + [fit]) if exact else prefix

        current = sp.get_layout(self.layouts, day, period_id)
        if current is None:
            current = sp.default_durations(period)
        current = [int(d) for d in (current or [])]
        if [int(d) for d in durations] != current:
            self._apply_cut_layout(day, period, durations)
        if exact:
            # 以前“补一场”的一次性 session 现在已经被正式 layout 覆盖，
            # 必须删掉；否则同一时刻会同时存在 layout 场和 extra 场。
            removed = 0
            for sess in planned:
                try:
                    if sp.remove_extra(day, sess.get("start")):
                        removed += 1
                except Exception:              # noqa: BLE001 - 坏 extra 不该挡住切割
                    pass
            if removed:
                self.rewrite_data_csv_from_plan()
                self.reload_periods()
                self.reload_entries()
            body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                             for s in planned)
            return "时段 %s 已经剪断成 %s" % (period_id, body)
        return (self._ensure_carved_session(day, chosen, task=task)
                or "时段 %s 的前半截留在 %s" % (period_id, iso))

    # -- refresh 排期流水线：formatter 先占位，项目 plan 后校验 -------------

    @staticmethod
    def _formatter_dates_of(task):
        """一条 formatter 任务所有可能的落点日期（reservation/session/repeat_date）。"""
        fmt = (task or {}).get("formatter") or {}
        reservation = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        out = set()
        for raw in (reservation.get("date"), sess.get("date"),
                    (task or {}).get("repeat_date")):
            text = str(raw or "").strip()
            if text:
                out.add(text)
        return out

    @staticmethod
    def _formatter_date_of(task):
        """兼容旧接口：返回第一个已知落点日期；没有就 None。"""
        dates = WardenShell._formatter_dates_of(task)
        return sorted(dates)[0] if dates else None

    def formatter_tasks_for_day(self, day=None):
        """``day`` 上需要跑 formatter 的 active 任务（稳定按 id 排序）。"""
        day = day or self.sel_date
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        out = []
        now = self._now_dt()
        for task in self.task_store.list():
            if not ts.TaskStore.is_open(task):
                continue
            if ts.TaskStore.is_repeat_template(task):
                continue
            spec = task.get("formatter") or {}
            if not spec.get("name"):
                continue
            if ts.task_is_deferred(task, now=now):
                # defer 还没到：不进这一轮排期队列，也不动它旧的位置。
                continue
            # occurrence 子任务只属于它自己的 repeat_date，绝不能被 formatter
            # 的旧 reservation / 全局「今天」拽到别的日子重排。否则前几天的
            # 未收尾子任务会在每次 refresh 时全部涌到今天、互相抢 session。
            if ts.TaskStore.is_repeat_occurrence(task):
                own_days = set()
                raw_repeat_day = str(task.get("repeat_date") or "").strip()
                if raw_repeat_day:
                    own_days.add(raw_repeat_day)
            else:
                own_days = self._formatter_dates_of(task)
            # 只有「还没有任何落点日期」的新 formatter 任务，才允许按当前
            # 选中的那天试排。已经有 reservation / session 的任务只看自己的
            # 落点日期；不再无条件把 sel_date 塞进 own_days。
            if own_days and iso not in own_days:
                continue
            if not own_days:
                explicit_day = str((spec.get("args") or {}).get("date")
                                   or (spec.get("args") or {}).get("day") or "").strip()
                if explicit_day:
                    try:
                        if th.resolve_target_day(
                                self.sel_date, now, spec.get("args") or {},
                                allow_today=True).isoformat() != iso:
                            continue
                    except Exception:          # noqa: BLE001 - 坏 args 不挡流水线
                        pass
            out.append(task)
        # 用户要求 enlist 排在所有其它 formatter 的最后、项目之前。
        # 同一优先级内按 id 稳定排序。
        out.sort(key=lambda t: (th.formatter_priority(
            (t.get("formatter") or {}).get("name")), t.get("id") or 0))
        return out

    def _formatter_apply_valid(self, task, day):
        """这条 formatter 的账和当前 layout 是否已经一致（一致就绝不再切一次）。

        只看「任务还挂在同一起点」不够：同一起点但时段 layout 已经不是
        ``cut_prefix + 任务场``（例如用户/别的流程把 75 分钟改回了 50），
        必须判为失效并重切。反过来，正常 refresh 里多条 formatter 任务共用
        一个时段时，第一条不能因为自己重跑就把后面的任务清掉 —— 所以这份
        快速校验必须在调用 formatter 算法之前做。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        fmt = (task or {}).get("formatter") or {}
        name = str(fmt.get("name") or "").strip().lower()
        if not name:
            return False
        # 本地 layout 说“还在”不算数：已经开始/过去的目标场次必须在 API 里
        # 真有 id；否则就是被取消的幽灵场，必须重跑 formatter 重新找位置。
        if self._formatter_bound_session_missing(task, day):
            return False
        res = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        if name == "enlist":
            return self._enlist_apply_valid(task, day)
        if name == "repeat":
            return self._repeat_apply_valid(task, day)
        if str(res.get("date") or "") != iso:
            return False
        if not str(res.get("session_start") or "").strip():
            return False
        try:
            if int(res.get("duration") or 0) != int(ts.task_minutes(task) or 0):
                return False
            if (str(sess.get("start")) != str(res.get("session_start"))
                    or int(sess.get("duration") or 0)
                    != int(res.get("session_duration") or 0)):
                return False
        except (TypeError, ValueError):
            return False
        period_id = str(res.get("period_id") or "").strip()
        result = fmt.get("result") or {}
        result_session = result.get("session") or {}
        prefix = list(result_session.get("cut_prefix")
                      or result.get("cut_prefix") or [])
        exact = bool(result_session.get("cut_prefix_exact",
                                        result.get("cut_prefix_exact", True)))
        if not period_id:
            # 没有时段（手写 csv / 一次性 session 模式）：只检查那一场还在。
            return any(str(s.get("start")) == str(sess.get("start"))
                       and int(s.get("duration") or 0) == int(sess.get("duration") or 0)
                       for s in self.sessions_for_day_plan(day))
        period = next((p for p in self.periods_for_day(day)
                       if str(p.get("id")) == period_id), None)
        if period is None:
            return False
        # 非尾巴 placement（end 把任务塞进更早一场，或 enlist/兜底一次性场）：
        # formatter 没有改写整段 layout，不能拿「prefix + 任务场」当整段预期，
        # 否则同一时段里第二条 formatter 永远被判失效、每轮 refresh 都被搬走。
        if not str(result_session.get("cut_period_id") or "").strip():
            try:
                return any(
                    str(s.get("start")) == str(sess.get("start"))
                    for s in self.sessions_for_day_plan(day))
            except Exception:                  # noqa: BLE001 - 坏排布按失效处理
                return False
        try:
            planned = sp.plan_period(period, prefix + [int(sess.get("duration") or 0)])
        except (TypeError, ValueError):
            return False
        if not planned or str(planned[-1].get("start")) != str(sess.get("start")):
            return False
        try:
            actual = sp.sessions_for_period(period, self.layouts)
        except (TypeError, ValueError):
            return False
        expected = [(str(s.get("start")), int(s.get("duration") or 0)) for s in planned]
        current = [(str(s.get("start")), int(s.get("duration") or 0)) for s in actual]
        if exact:
            if expected != current:
                return False
            # 精确排布下不应该再有同一时刻的 extra 兜底场。
            try:
                extras = {str(s.get("start")) for s in
                          sp.extras_for_date(sp.load_extras(), day)}
            except Exception:                  # noqa: BLE001
                extras = set()
            if extras.intersection(start for start, _dur in expected):
                return False
            return True
        # 兜底路径：layout 只保留前缀，任务那一场在 extras 里。
        if current != expected[:len(prefix)] and current != expected[:-1]:
            return False
        try:
            return any(str(s.get("start")) == str(sess.get("start"))
                       and int(s.get("duration") or 0)
                       == int(sess.get("duration") or 0)
                       for s in sp.extras_for_date(sp.load_extras(), day))
        except Exception:                      # noqa: BLE001
            return False

    def _repeat_apply_valid(self, task, day):
        """``repeat`` 的幂等校验：固定的那一场还在、时长没变、任务还挂在上面。

        这个 formatter 的语义是「点名某一场」而不是「任意可容纳的场」，所以不能
        套普通 formatter 只看同起点的宽松校验。目标场被取消、时长被改短/改长、
        或任务绑定的 session 已经漂到别处，都必须重排（重排时如果目标场不存在
        会明确报错，不会偷偷换一场）。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        fmt = (task or {}).get("formatter") or {}
        res = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        if str(res.get("date") or "") != iso:
            return False
        target_start = str(res.get("session_start") or "").strip()
        try:
            target_duration = int(res.get("session_duration") or 0)
            task_minutes = int(ts.task_minutes(task) or 0)
            if int(res.get("duration") or 0) != task_minutes:
                return False
        except (TypeError, ValueError):
            return False
        if not target_start or target_duration <= 0 or task_minutes <= 0:
            return False
        if (str(sess.get("date")) != iso
                or str(sess.get("start")) != target_start
                or int(sess.get("duration") or 0) != target_duration):
            return False
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001 - 坏排布按失效处理
            return False
        target = None
        for candidate in sessions:
            try:
                if (str(candidate.get("start")) == target_start
                        and int(candidate.get("duration") or 0) == target_duration):
                    target = candidate
                    break
            except (TypeError, ValueError):
                continue
        if target is None:
            return False
        # 别人后来往这一场塞了独占任务，或者这一条自己是独占而场里已经有别人，
        # 都算失效；重排会给出明确原因，而不是继续假装排好了。
        if self._exclusive_blockers(iso, target_start, task):
            return False
        # 同一块不能被别的 formatter 抢占。自己的旧 reservation 要排除。
        try:
            reservations = [r for r in th.collect_reservations(
                self.task_store.list(), iso) if r.get("task_id") != task.get("id")]
        except Exception:                          # noqa: BLE001
            reservations = []
        block = th._range_of(res)
        if block is not None and not th.is_free(
                {"start_min": block[0], "end_min": block[1]}, reservations):
            return False
        return True

    def _enlist_apply_valid(self, task, day):
        """``enlist`` 的幂等校验：目标 session 还在、空着，占位也没漂。

        ``enlist`` 不切 layout，所以不能套 ``end`` 的「cut_prefix + 任务场」
        校验。这里检查的是：任务绑定的整场 session、formatter 记下的任务块、
        当前 layout 三者是否还对得上；以及这个块有没有被别的 formatter 抢走。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        fmt = (task or {}).get("formatter") or {}
        res = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        if str(res.get("date") or "") != iso:
            return False
        try:
            if int(res.get("duration") or 0) != int(ts.task_minutes(task) or 0):
                return False
            target_start = str(res.get("session_start") or "")
            target_duration = int(res.get("session_duration") or 0)
            if (str(sess.get("date")) != iso
                    or str(sess.get("start")) != target_start
                    or int(sess.get("duration") or 0) != target_duration):
                return False
        except (TypeError, ValueError):
            return False
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            return False
        target = None
        for candidate in sessions:
            try:
                if (str(candidate.get("start")) == target_start
                        and int(candidate.get("duration") or 0) == target_duration):
                    target = candidate
                    break
            except (TypeError, ValueError):
                continue
        if target is None:
            return False
        target_range = th._range_of(target)
        block = th._range_of(res)
        if target_range is None or block is None:
            return False
        task_minutes = int(res.get("duration") or 0)
        if block[0] < target_range[0]:
            return False
        if block[0] + task_minutes > target_range[1]:
            return False
        # 目标场次里除了自己不能再有别的 open 任务；enlist 的语义就是「挂进
        # 一场空 session」，而且这样项目 plan 也不会看到假空场。
        try:
            others = [t for t in self.task_store.tasks_for_session(
                iso, target_start) if t.get("id") != task.get("id")]
        except Exception:                          # noqa: BLE001
            others = []
        if any(ts.TaskStore.is_open(t) for t in others):
            return False
        # 别的 formatter 后来占用了同一块 → 失效重排。
        try:
            reservations = [r for r in th.collect_reservations(
                self.task_store.list(), iso) if r.get("task_id") != task.get("id")]
        except Exception:                          # noqa: BLE001
            reservations = []
        if not th.is_free({"start_min": block[0], "end_min": block[1]},
                          reservations):
            return False
        # 休息尾巴不能压住下一场 session。
        for other in sessions:
            if other is target:
                continue
            got = th._range_of(other)
            if got is not None and th.overlaps(block, got):
                return False
        return True

    def _project_plan_invalid_reasons(self, project, day):
        """项目今天的 plan 为什么和当前资源对不上；空列表 = 对得上。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        plan = pj.plan_of(project, day)
        if not plan:
            return []
        items = list(plan.get("items") or [])
        if not items:
            # 只有“说好的 count”、没有任何落地场次 = 没有真正的安排；
            # 不在这里制造假失败，留给用户重新 plan。
            return []
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            sessions = []
        by_key = {}
        for sess in sessions:
            try:
                by_key[(str(sess.get("start")), int(sess.get("duration") or 0))] = sess
            except (TypeError, ValueError):
                continue
        rows_by_start = {}
        for row in self.rows or ():
            if row.get("date") != iso or not row.get("planned"):
                continue
            rows_by_start.setdefault(str(row.get("startLabel") or ""), row)
        try:
            reservations = th.collect_reservations(self.task_store.list(), iso)
        except Exception:                          # noqa: BLE001
            reservations = []
        reasons = []
        if not self.projects.is_op_allowed_by_pro(project):
            pro = self.projects.pro_for_op(project)
            op_kind = pj.normalize_op_type(project.get("op_type")) or pj.OP_TYPE_SOP
            reasons.append("所属项目「%s」当前处于 %s 阶段，不允许 %s op 进行"
                           % ((pro or {}).get("title") or "?",
                              (pro or {}).get("stage") or "?", op_kind))
        for item in items:
            start = str(item.get("start") or "")
            try:
                duration = int(item.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            title = item.get("pk") or "?"
            task = None
            try:
                task = self.task_store.get(item.get("task_id"))
            except (ValueError, TypeError):
                pass
            if task is None or not ts.TaskStore.is_open(task):
                reasons.append("任务 %s 已不存在/已收工" % title)
                continue
            if task.get("formatter"):
                reasons.append("任务 %s 已改由 formatter 独立占时间" % title)
                continue
            if ts.task_is_deferred(task):
                reasons.append("任务 %s 的 defer 还没到" % title)
                continue
            sess = task.get("session") or {}
            if (sess.get("date") != iso or str(sess.get("start")) != start
                    or int(sess.get("duration") or 0) != duration):
                reasons.append("任务 %s 的绑定和 plan 不一致" % title)
                continue
            if not start or (start, duration) not in by_key:
                reasons.append("%s %s 这场已不在当前排布里" % (start or "?", duration))
                continue
            row = rows_by_start.get(start)
            if row is None:
                reasons.append("%s 这场没有对应的契约行" % start)
                continue
            if self.rows_source == "bridge" and not row.get("id"):
                reasons.append("%s 这场没有订上/已被取消" % start)
                continue
            start_min = th._clock_minutes(start)
            end_min = start_min + duration if start_min is not None else None
            if (start_min is not None and end_min is not None
                    and not th.is_free({"start_min": start_min, "end_min": end_min},
                                       reservations)):
                reasons.append("%s 这场和 formatter 的硬占位重叠" % start)
        return reasons

    def _invalidate_project_plan(self, project, day, reasons):
        """清空项目当天的 plan，并把相关非 formatter 任务从失效场次下架。

        返回被清掉的计划条数。formatter 任务绝不在这里被下架/改写 —— 它是
        流水线第一阶段占好的资源，后续阶段只能绕开。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        plan = pj.plan_of(project, day) or {}
        items = list(plan.get("items") or [])
        self.projects.set_allocation(project.get("id"), day, 0)
        dropped = 0
        for item in items:
            try:
                task = self.task_store.get(item.get("task_id"))
            except (ValueError, TypeError):
                continue
            if task.get("formatter"):
                continue
            sess = task.get("session") or {}
            if (sess.get("date") == day.isoformat()
                    and str(sess.get("start")) == str(item.get("start"))):
                try:
                    self.task_store.unassign(task["id"], reason="project-plan-invalid")
                    dropped += 1
                except ValueError:
                    pass
        self.say("⚠ 项目「%s」%s 的 plan 和当前时间资源对不上：%s。已清空，"
                 "请重新 `plan` 安排。"
                 % (project.get("title") or project.get("id"),
                    day.isoformat(), "；".join(reasons[:3])
                    + (" 等" if len(reasons) > 3 else "")), "warn")
        return len(items), dropped

    def reconcile_schedule_pipeline(self, day=None, quiet=False):
        """refresh 的排期流水线：**formatter 先、项目 plan 后**。

        1. formatter 阶段：每条 active formatter 任务都按当前可用场次重新核对/
           重跑。它们占掉的区间是硬约束，后面的项目 plan 只能看到剩余空场，
           绝不会改这些场次。
        2. 项目阶段：只检查**今天已经 plan 过**的项目。plan 的每个 item 必须还
           对得上当前场次、本地绑定和未取消的 API session；对不上就清空 plan，
           让用户重新 `plan`。没有 plan 的项目直接跳过。
        3. defer 还没到点的任务不进入任何排期队列；本地任务 / 项目清单两边都查。

        返回 ``{"formatters": n, "invalidated": n, "detached": n, "errors": n}``。

        整段包在 ``_suppress_book`` 里：refresh 是对账轮，**不新造外部动作**
        （不订场）。这一轮里 formatter / 自动重排改出来的 data.csv 由 daemon
        下一轮吃进去，或者由随后的 ``refresh --check`` 补订。
        """
        day = day or self.sel_date
        if hasattr(day, "datetime"):
            day = day.date()
        old_suppress = getattr(self, "_suppress_book", False)
        self._suppress_book = True
        try:
            return self._reconcile_schedule_pipeline(day, quiet=quiet)
        finally:
            self._suppress_book = old_suppress

    def _reconcile_schedule_pipeline(self, day, quiet=False):
        """`reconcile_schedule_pipeline` 的实作（不负责 _suppress_book 守卫）。"""
        result = {"formatters": 0, "invalidated": 0, "detached": 0, "errors": 0}
        # 先清掉过期且从未处理的重复 occurrence：它们绝不能再进入今天/明天的
        # formatter 队列（否则同名任务会越积越多，互相抢同一场 session）。
        try:
            result["expired"] = len(self.expire_stale_repeat_occurrences())
        except Exception as e:                     # noqa: BLE001 - 清理失败不挡刷新
            result["expired"] = 0
            self.say("重复 occurrence 清理失败（继续刷新）：%s" % e, "warn")
        try:
            self.reload_periods()
            self.reload_entries()
            self.rebuild_rows()
        except Exception as e:                     # noqa: BLE001 - 先用上一份账
            self.say("排期流水线：重读本地排布失败（先用上一份）：%s" % e, "warn")
        for task in self.formatter_tasks_for_day(day):
            spec = dict(task.get("formatter") or {})
            try:
                if self._formatter_apply_valid(task, day):
                    # 已经正确：只算“检查过”，绝不重写场次，避免第一条
                    # formatter 重跑时把后面 formatter 的绑定清掉。
                    result["formatters"] += 1
                    continue
                self._apply_formatter_to_task(task, spec, action="hook",
                                              day=day, quiet=True,
                                              strict_actual=True)
                result["formatters"] += 1
            except Exception as e:                 # noqa: BLE001 - 一条坏任务不挡别的
                result["errors"] += 1
                self.had_error = True
                self.say("⚠ formatter 任务 %s「%s」没能重排：%s"
                         % (self.task_label(task),
                            self.task_store.focusmate_title(task, 28), e), "warn")
        try:
            self.rebuild_rows()
        except Exception:                          # noqa: BLE001
            pass
        # 自动 layout：只在“计划指纹”真的变了时重算；没有变化就一次都不重排。
        # _replan_auto_day 只碰未来、未开始、plan 管理的 session，正在进行的
        # session 由 _plan_slots / _session_already_started 保护。
        try:
            if self._auto_plan_dirty(day):
                if self._replan_auto_day(day):
                    result["auto_replanned"] = 1
        except Exception as e:                     # noqa: BLE001 - 自动重排失败不挡刷新
            result["errors"] += 1
            self.say("自动 layout 重算失败（其余照常刷新）：%s" % e, "warn")
        try:
            projects = self.projects.active()
        except Exception as e:                     # noqa: BLE001 - 项目库坏了不挡 formatter
            projects = []
            self.had_error = True
            self.say("排期流水线：项目库读不了，跳过项目 plan 校验：%s" % e, "warn")
        for project in projects:
            reasons = self._project_plan_invalid_reasons(project, day)
            if not reasons:
                continue
            cleared, detached = self._invalidate_project_plan(project, day, reasons)
            result["invalidated"] += cleared
            result["detached"] += detached
        if result["formatters"] or result["invalidated"]:
            try:
                self.rebuild_rows()
            except Exception:                      # noqa: BLE001
                pass
            self.invalidate()
        if not quiet and (result["formatters"] or result["invalidated"]):
            self.say("🧭 排期流水线：formatter %d 条、失效项目计划 %d 条%s"
                     % (result["formatters"], result["invalidated"],
                        ("、下架 %d 条旧绑定" % result["detached"])
                        if result["detached"] else ""), "info")
        return result

    def _apply_formatter(self, result, spec, action):
        """``-f/--formatter`` 的收尾：记下 preset，并**当场调用它**。

        普通任务当场排；**重复任务本体**不排自己，而是把 formatter 记成模板，
        再转嫁给它的 occurrence 子任务（今天 + 明天 / 下一个规则日）。本体永远
        不挂 session —— 用户在契约表里看到、能完成的是子任务。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-f/--formatter` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = self._hook_task(result, "formatter", action)
        if ts.TaskStore.is_repeat_template(task):
            done = self._apply_formatter_to_repeat_template(task, spec)
        else:
            done = self._apply_formatter_to_task(task, spec, action=action)
        # formatter 剪断/新增了当天的 session → 新场次要有人订。
        # 统一走 book()（唯一预约出口）；refresh 的自动重排被 _suppress_book 挡住，
        # 那条路不新造外部动作。这一句放在命令层而不是 _apply_cut_layout 里，
        # 是因为后者也服务于 refresh，不该自带外部副作用。
        self.book(self.sel_date, why="formatter 改过排布，要把新场次订上")
        return done

    def _apply_formatter_to_repeat_template(self, task, spec):
        """把 formatter 记在重复任务**本体**上，再落到子任务上。

        本体只保存规则模板（``ts.TaskStore.hook_template`` 会摘掉 reservation /
        result / applied_at）。随后为「今天 / 明天 / 下一个规则日」生成 occurrence
        子任务，并在每个子任务上真的跑一遍 formatter —— 这样 ``task <本体> -f
        end`` 的产出是子任务，本体自己仍然没有 session。
        """
        day = th.resolve_target_day(self.sel_date, datetime.datetime.now(),
                                    spec.get("args") or {}, allow_today=True)
        today = datetime.date.today()
        if day < today:
            raise ValueError("formatter 不能排到过去（%s）。先切到那一天或指定 date=。"
                             % day.isoformat())
        template = ts.TaskStore.hook_template(spec)
        task = self.task_store.set_hook(task["id"], "formatter", template)
        # 旧数据可能把本体当普通任务排过（带 session / focusmate）；能落到规则日
        # 就把那一场**转给**子任务，本体下架。不能落就留着让用户手动处理，
        # 至少不再把 formatter 写在本体的 reservation 上。
        task = self._adopt_template_session(task)
        days = self._repeat_prepare_days(task, day, limit=2, horizon=7)
        if not days:
            self.say("🔁 任务 %s 的 formatter %s 已记在本体上；从 %s 起 7 天里"
                     "没有它的规则日，所以还没有子任务可排。"
                     % (self.task_label(task), (template or {}).get("name") or "?",
                        day.isoformat()), "warn")
            self.rebuild_rows()
            return task
        made = []
        for probe in days:
            self.ensure_repeat_occurrences(probe, force_formatters=True)
            child = self.task_store.occurrence_for(task["id"], probe)
            if child is not None:
                made.append(child)
        if made:
            self.selected_task_id = made[-1]["id"]
        names = "、".join("%s %s" % ((c.get("repeat_date") or "?"),
                                    (c.get("session") or {}).get("start") or "没排上")
                         for c in made)
        self.say("🔁 本体 %s 的 formatter %s 已转嫁给 %d 条子任务：%s"
                 % (self.task_label(task), (template or {}).get("name") or "?",
                    len(made), names or "（没生成）"), "ok")
        self.rebuild_rows()
        return task

    @staticmethod
    def _repeat_prepare_days(parent, start, limit=2, horizon=7):
        """从 ``start`` 起找这个重复任务接下来要准备的规则日（最多 ``limit`` 个）。"""
        days = []
        probe = start
        for _ in range(max(1, int(horizon))):
            if ts.TaskStore.repeat_due_on(parent, probe):
                days.append(probe)
                if len(days) >= max(1, int(limit)):
                    break
            probe += datetime.timedelta(days=1)
        return days

    def _adopt_template_session(self, parent):
        """把旧数据里重复任务**本体**身上的 session 转给当天的子任务。

        新模型里本体永远不挂 session；以前 `-f end` 会把本体排进某一场。这里
        只在「那天确实是规则日」时搬过去：子任务接过 session / Focusmate task，
        本体下架。搬不了就原样留着（宁可不猜），但界面上仍然不会把本体当任务列。
        """
        if not ts.TaskStore.is_repeat_template(parent):
            return parent
        sess = parent.get("session") or {}
        iso = str(sess.get("date") or "").strip()
        if not iso:
            return parent
        try:
            day = datetime.date.fromisoformat(iso)
        except ValueError:
            return parent
        if not ts.TaskStore.repeat_due_on(parent, day):
            return parent
        child = self.task_store.occurrence_for(parent["id"], day)
        if child is None:
            child = self.task_store.create_repeat_occurrence(
                parent["id"], day, session=sess)
        elif not (child.get("session") or {}):
            child = self.task_store.assign(child["id"], sess)
        fm = parent.get("focusmate") or {}
        if fm.get("task_id") or fm.get("id"):
            self.task_store.attach_focusmate(
                child["id"],
                {k: fm.get(k) for k in ("task_id", "id", "session_id", "attached_at",
                                        "title", "via") if fm.get(k) is not None},
                session=sess)
        parent = self.task_store.unassign(parent["id"], reason="repeat-template-adopt")
        self.say("🔁 重复任务本体 %s 原来挂在 %s %s，已转给子任务 %s（本体不占 session）"
                 % (self.task_label(parent), iso, sess.get("start") or "?",
                    self.task_label(child)), "warn")
        return parent

    def _apply_formatter_to_task(self, task, spec, action="hook", day=None,
                                 quiet=False, strict_actual=False):
        """在**一条具体任务**（通常是 occurrence 子任务）上执行 formatter。

        ``day`` 给了就排那一天（重复任务转嫁时按子任务的 repeat_date 排）；
        没给就按 ``-f`` 的 date= / 当前选中那天解析。
        """
        if day is None:
            day = th.resolve_target_day(self.sel_date, datetime.datetime.now(),
                                        spec.get("args") or {}, allow_today=True)
        today = datetime.date.today()
        if day < today:
            raise ValueError("formatter 不能排到过去（%s）。先切到那一天或指定 date=。"
                             % day.isoformat())
        # 目标场次已经被取消：先把任务从旧 session 上摘下来，再拿真实候选重排。
        # 这一步必须发生在 formatter 算法之前，否则它还会从 data.csv 里看见那场
        # 幽灵 session，重新把任务挂回去。
        if self._formatter_bound_session_missing(task, day):
            old_sess = dict(task.get("session") or {})
            self.say("⚠ formatter 任务 %s 原来挂的 %s %s 已经在 API 里不存在，"
                     "先下架再重新找位置。"
                     % (self.task_label(task), old_sess.get("date") or "?",
                        old_sess.get("start") or "?"), "warn")
            task = self._task_detach(task, reason="formatter-target-missing")
        sessions = self._formatter_sessions(day, strict_actual=strict_actual)
        ctx = {
            "task": task,
            "task_id": task["id"],
            "date": day.isoformat(),
            "sessions": sessions,
            "periods": self.periods_for_day(day),
            "reservations": th.collect_reservations(self.task_store.list(),
                                                    day.isoformat()),
            # enlist 要判断哪些 session 已经有任务；把 open 任务一起交给算法，
            # 但算法仍只读、不写盘。
            "tasks": self.task_store.list(),
        }
        try:
            out = th.call_preset("formatter", spec, ctx)
        except th.HookError as exc:
            # 算法层不知道「面板序号」这套编号；把报错里的内部 id 换成用户
            # 眼前那个序号（例如 `task modify 3 duration 25` -> `task modify 2 ...`）。
            text = str(exc)
            panel_no = self.task_no(task)
            if panel_no:
                text = text.replace("task modify %s duration" % task.get("id"),
                                    "task modify %s duration" % panel_no)
            raise th.HookError(text) from None
        chosen = out.get("session") or {}
        reservation = out.get("reservation") or {}
        if not chosen.get("start"):
            raise th.HookError("formatter %s 没有返回 session" % spec.get("name"))

        # 先看这一场是不是已经有 🔒 独占任务：撞上了就什么都别写（不然会留下
        # 一场没人用的排期 + 一条排了但挂不上的任务）。
        blocked = self._exclusive_blockers(day.isoformat(), chosen.get("start"), task)
        if blocked:
            names = "、".join(self.task_label(t) for t in blocked)
            raise th.HookError(
                "formatter %s 没排：%s %s 那场已经有 🔒 独占任务 %s —— 一场只能有一条任务。"
                "换一天 / 换一场，或先 `task <序号> -e false` 放开那条的独占。"
                % (spec.get("name"), day.isoformat(), chosen.get("start"), names))

        # `end` 会把「当天最后一段」的尾巴**切一场任务大小的 session** 出来
        # （11:00–12:00 的时段 + 25 分钟任务 → 11:30–11:55），并**剪断**那个时段：
        # 原来那场 50 分钟会真的被取消/从计划里拿掉，只剩「前半截 + 这一场」。
        carved_note = None
        # 即使 `existing=True` 也要检查切割排布：例如旧排布是 [50] + 23:30
        # 一次性补场，任务虽然“在 23:30”，但那个时段并没有被真正切成
        # [75, 25]；refresh 必须把它掰回来，而不是看见同起点就跳过。
        if chosen.get("carved"):
            if chosen.get("cut_period_id"):
                carved_note = self._cut_period_for_carve(day, chosen, task=task)
            elif not chosen.get("existing"):
                carved_note = self._ensure_carved_session(day, chosen, task=task)
            if carved_note:
                sessions = self._formatter_sessions(day)
            try:
                # 剪断可能把我们这条任务（或它那一场）的绑定清掉了，重读一遍再判断
                # 「原来挂在哪」—— 否则会对着一个已经不存在的绑定再下架一次。
                task = self.task_store.get(task["id"])
            except (ValueError, TypeError):
                pass

        # 选中 session 的 Focusmate 身份：未来那天可能已经订好了，也可能还没有。
        hour, minute = map(int, chosen["start"].split(":"))
        slot = {"start": chosen["start"], "duration": int(chosen.get("duration") or 0),
                "hour": hour, "minute": minute}
        session_info = self.session_info_for(day, slot)
        previous = task.get("session") or {}
        # 场次身份不只看起点：同一时刻时长被 formatter 改成另一档，也必须重挂。
        # 只看 date/start 会把「25 分钟任务还挂在旧的 50 分钟场上」当成已正确。
        moved = bool(previous) and (
            (previous.get("date"), previous.get("start"),
             int(previous.get("duration") or 0))
            != (session_info.get("date"), session_info.get("start"),
                int(session_info.get("duration") or 0)))
        if moved:
            self.say("任务 %s 原来在 %s %s；formatter 要先下架旧位置再挂新位置。"
                     % (self.task_label(task), previous.get("date"), previous.get("start")),
                     "warn")
            task = self._task_detach(task, reason="formatter-move")
        if moved or not previous:
            note = self.assign_task_to(task, session_info)
            task = self.task_store.get(task["id"])
            assigned = task.get("session") or {}
            if (not assigned
                    or str(assigned.get("start") or "") != str(session_info.get("start") or "")):
                raise th.HookError(
                    "formatter 没能把任务挂到 %s %s：%s"
                    % (day.isoformat(), session_info.get("start") or "?",
                       note or "这场不可用（可能已被取消或被独占任务占用）"))
        else:
            note = "已经在这场，不重复建 Focusmate task"

        spec = dict(spec)
        spec["result"] = out
        spec["reservation"] = reservation
        spec["applied_at"] = ts._now()
        task = self.task_store.set_hook(task["id"], "formatter", spec)
        # `end` 排出来的任务**自动独占**：一场 Focusmate 里塞两条任务等于两个专注块
        # 被切成四段。想放开就 `task <序号> -e false`。
        if spec.get("name") == "end" and not th.is_exclusive(task):
            task = self.task_store.modify(task["id"], "exclusive", True)
        if not quiet:
            notes = ""
            if carved_note:
                notes += "；" + carved_note
            conflicts = [] if chosen.get("cut_period_id") else \
                [str(s) for s in (out.get("conflicts") or []) if s]
            if conflicts:
                notes += ("；⚠ 你原来排的 %s 那场还在，跟这一场时间上重叠了 —— "
                          "不想留它：`period %s 25` 重排这个时段，"
                          "或 `cancel <表格行号>` 取消已订的那场"
                          % ("、".join(conflicts),
                             (reservation.get("period_id") or "T?")))
            lock_txt = "；🔒 独占" if th.is_exclusive(task) else ""
            self.say("✅ 任务 %s「%s」已由 formatter %s 排到 %s %s–%s"
                     "（session %s 分钟，任务 %s 分钟 + %s 分钟休息；占用 %s–%s%s）%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        spec.get("name"), reservation.get("date"), reservation.get("start"),
                        reservation.get("end"), reservation.get("session_duration"),
                        reservation.get("duration"), reservation.get("break_minutes"),
                        reservation.get("start"), reservation.get("end"), lock_txt,
                        notes + (("；" + note) if note else "")), "ok")
            self.selected_task_id = task["id"]
        self.rebuild_rows()
        return task
    # -- 位置：挂上 / 下架 / 调换 / 全量转移 ---------------------------

    @staticmethod
    def _session_kind(token):
        """``-a`` 的取值是不是特殊词：``"none"`` / ``"current"`` / ``""``。"""
        low = str(token or "").strip().lower()
        if low in TASK_SESSION_NONE:
            return "none"
        if low in TASK_SESSION_CURRENT:
            return "current"
        return ""

    def _actual_rows(self, rows):
        """权威数据里只留**真的订上了**的行；快照/离线模式没法看 id，全留。"""
        rows = list(rows or [])
        if not self.authoritative:
            return rows
        return [r for r in rows if r.get("id")]

    def live_session(self):
        """当前**真正在进行**的 session → ``(表格编号, 行)``；没有就 None。

        判定必须同时满足：
        * 权威模式下有 Focusmate session id（不是只有 data.csv 计划窗口）；
        * now 落在这一行自己的 ``startMs`` / ``endMs`` 内。
        取消后另约一场时，旧的 10:30 计划行没有 id，不会被当成当前 session；
        新订的 10:45 行有 id 且时间覆盖现在，才是当前这一场。
        """
        now_ms = int(time.time() * 1000)
        live = [r for r in self._actual_rows(self.rows)
                if r.get("startMs") is not None
                and r["startMs"] <= now_ms < (r.get("endMs") or 0)]
        if not live:
            return None
        # 若真的有两场重叠，取开始更晚的那一场（更接近「刚换到的新约」）。
        live.sort(key=lambda r: (r.get("startMs") or 0, self.rows.index(r)))
        row = live[-1]
        return self.rows.index(row) + 1, row

    def current_session(self):
        """「当前这一场」→ ``(表格编号, 行)``：进行中的优先，其次最近一场还没开始的。

        需求（``task 8 -ac``）：正在干的那一场就是「当前」；一场都不在进行时，
        取**最近一场还没开始、而且真的订上了**的（下一场马上就来，先挂上去就是
        为了别手忙脚乱）。已结束/取消掉、只剩计划窗口的幽灵场不参与判定。
        """
        live = self.live_session()
        if live:
            return live
        now_ms = int(time.time() * 1000)
        upcoming = [r for r in self._actual_rows(self.rows)
                    if (r.get("startMs") or 0) > now_ms]
        upcoming.sort(key=lambda r: (r.get("startMs") or 0, self.rows.index(r)))
        if not upcoming:
            raise ValueError("现在没有正在进行的 session，也没有还没开始的已订场次"
                             "（%s 只有 %d 行）；用表格编号挂吧。"
                             % (self.sel_date.isoformat(), len(self.rows)))
        row = upcoming[0]
        return self.rows.index(row) + 1, row

    def _resolve_assign_target(self, token):
        """``-a`` 的取值 → ``(编号, 行)``；``none`` 返回 ``(None, None)``（下架）。"""
        kind = self._session_kind(token)
        if kind == "none":
            return None, None
        if kind == "current":
            return self.current_session()
        return self.pick(token)

    def _row_for_session(self, session):
        """``task["session"]`` 那条记录 → 当前表格里的行（不在这一天就 None）。"""
        want = ((session or {}).get("date"), (session or {}).get("start"))
        for i, row in enumerate(self.rows, 1):
            if (row.get("date"), row.get("startLabel")) == want:
                return i, row
        return None, None

    def _task_detach(self, task, reason="cmd-down"):
        """把任务从它那一场**下架**：先删 Focusmate 那条 task，再断本地绑定。

        需求（``task 8 -an``）：「手动把它下下来」。顺序不能反 —— 本地先断了、
        Focusmate 那边删失败的话，那条 task 就成了没人认领的孤儿（第二天对账
        还以为它在等你）。所以先删、删成功（或明确报了删不掉）再断本地。
        """
        sess = task.get("session") or {}
        fm = task.get("focusmate") or {}
        if not sess and not (fm.get("task_id") or fm.get("id")):
            self.say("任务 %s 本来就没挂 session（也没 Focusmate task），不用下架。"
                     % self.task_label(task), "info")
            return task
        if self.offline or not (fm.get("task_id") or fm.get("id")):
            # 离线 / 本来就没有 Focusmate task：只改本地，不进队列
            note = "" if self.offline else "（没有 Focusmate task，只断了本地）"
            task = self.task_store.unassign(task["id"], reason=reason)
            self.say("⏏ 任务 %s「%s」已从 %s %s 下架%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        sess.get("date") or "（没有日期）", sess.get("start") or "",
                        "；" + note if note else ""), "ok")
            return task
        return self.op_task_detach(task, reason=reason)

    def cmd_task_swap(self, args):
        """``task <id> swap <id>`` —— 两条任务**互换位置**（位置 = 那一场 session）。

        需求里的例子：一条挂在 session 1、一条赋闲 → ``task 32 swap 21`` 之后，
        赋闲那条进 session 1，原来那条下来赋闲。两条各在一场时就是场次对调。

        Focusmate 没有「把 task 挪到另一场」这个动作（只能建一条新的），所以做法是：
        两边各自**先下架**（删掉旧场次上那条 task），再挂到对方原来的位置。
        先下架再加挂，顺序反了会在「加挂失败」时留下两条都挂在同一场。

            task 32 swap 21     ← 动作写在 ID 后面（需求里的顺序）
            task swap 32 21     ← 一样的
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if len(toks) < 2:
            raise ValueError("用法：task <任务序号> swap <任务序号>")
        a = self.resolve_task(toks[0])
        b = self.resolve_task(toks[1])
        if a["id"] == b["id"]:
            raise ValueError("swap 要给两条不同的任务（都写成了 %s）"
                             % self.task_label(a))
        sa = a.get("session") or {}
        sb = b.get("session") or {}
        same = (sa and sb and (sa.get("date"), sa.get("start"))
                == (sb.get("date"), sb.get("start")))
        if not sa and not sb:
            self.say("任务 %s 和 %s 都还赋闲（没挂 session）—— 没有位置可换。"
                     % (self.task_label(a), self.task_label(b)), "warn")
            return [a["id"], b["id"]]
        if same:
            self.say("任务 %s 和 %s 都在 %s %s，位置本来就一样，不用换。"
                     % (self.task_label(a), self.task_label(b),
                        sa.get("date") or "?", sa.get("start") or "?"), "info")
            return [a["id"], b["id"]]

        def where(sess):
            return ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?")
                    if sess else "赋闲")

        self.say("互换位置：任务 %s（%s）↔ 任务 %s（%s）"
                 % (self.task_label(a), where(sa), self.task_label(b), where(sb)), "cmd")
        # 1) 两边都先下架：旧场次上那条 Focusmate task 一起删掉，账才干净
        if sa:
            self._task_detach(a, reason="swap")
        if sb:
            self._task_detach(b, reason="swap")
        # 2) 再各自挂到**对方原来的位置**（对方赋闲的那条就留在这里不动）
        done = []
        for task, sess in ((b, sa), (a, sb)):
            if not sess:
                continue
            idx, row = self._row_for_session(sess)
            if row is None:
                self.say("⚠ 任务 %s 原来的位置 %s 不在当前表格里（那天没在显示），"
                         "所以它现在是赋闲的 —— 切到那天再 `task %s -a <编号>`。"
                         % (self.task_label(task), where(sess),
                            self.task_label(task)), "warn")
                continue
            try:
                self._assign_task(task, str(idx), refresh=False)
                done.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡另一条
                self.had_error = True
                self.say("❌ 任务 %s 没能挂回 %s：%s"
                         % (self.task_label(task), where(sess), e), "error")
        self.rebuild_rows()
        idle_now = [t for t, sess in ((a, sb), (b, sa)) if not sess]
        if done:
            moved_txt = "、".join(
                "任务 %s → %s" % (self.task_label(b if tid == b["id"] else a),
                                  where(sa if tid == b["id"] else sb))
                for tid in done)
            idle_txt = ("；赋闲的：" + "、".join(self.task_label(t) for t in idle_now)
                        if idle_now else "")
            self.say("✅ 位置已互换：%s%s" % (moved_txt, idle_txt), "ok")
        return [a["id"], b["id"]]

    def _apply_bulk(self, source_token, assign_to):
        """``task -s <源> [-a <目标>]`` —— 对一场 session 上的**全部**任务办事。

        需求（第 4 条）：``task -s 5 -a 6`` = 把 session 5 的全部任务转移到 6。
        没有 ``-a`` 就只列一下现在挂着谁（只看不动，手滑不会掉东西）。

        每个任务走的是和单条完全一样的路：先下架（删 Focusmate 旧 task）再挂到
        目标场次 —— 所以「转移」之后旧场次上不会留下一条永远不会完成的幽灵 task。
        一条失败不挡其余的，最后报总账。
        """
        src_idx, src_row = self.pick(source_token)
        src_day = src_row.get("date") or self.sel_date.isoformat()
        src_start = src_row.get("startLabel")
        tasks = self.task_store.tasks_for_session(src_day, src_start)
        where = "%s %s" % (src_day, src_start)
        if not tasks:
            self.say("session #%d（%s）上没有挂任务，没什么可办的。" % (src_idx, where), "info")
            return False
        if assign_to is None:
            self.say("session #%d（%s）上挂着 %d 条（没给 -a，只看不动）："
                     % (src_idx, where, len(tasks)), "info")
            for task in tasks:
                self.say("   %s %s%s"
                         % (self.task_label(task),
                            self.task_store.focusmate_title(task, 40),
                            "" if self.task_store.is_open(task) else "（已收工）"))
            self.say("   全量转移用 `task -s %d -a <目标编号>`；全部下架用 "
                     "`task -s %d -a none`。" % (src_idx, src_idx), "info")
            return True

        kind = self._session_kind(assign_to)
        tgt_idx, tgt_row = self._resolve_assign_target(assign_to)
        if tgt_row is not None and ((tgt_row.get("date") or self.sel_date.isoformat(),
                                     tgt_row.get("startLabel")) == (src_day, src_start)):
            self.say("源和目标都是 session #%d（%s），什么也没动。" % (src_idx, where), "info")
            return False
        target_where = "「下架」" if kind == "none" else "%s %s" % (
            tgt_row.get("date") or self.sel_date.isoformat(), tgt_row.get("startLabel"))
        self.say("批量：session #%d（%s）的 %d 条任务 → %s"
                 % (src_idx, where, len(tasks), target_where), "cmd")

        moved, failed = [], []
        for task in tasks:
            try:
                self._task_detach(task, reason="bulk-move")
                if kind == "none":
                    moved.append(task["id"])
                    continue
                self._assign_task(task, str(tgt_idx), refresh=False)
                moved.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                failed.append((task["id"], e))
                self.say("❌ 任务 %s 转移失败：%s"
                         % (self.task_label(task), e), "error")
        self.rebuild_rows()
        if not self.offline:
            try:
                self.refresh(quiet=True)
            except Exception:                          # noqa: BLE001 - 刷新失败不影响已做的
                pass
        self.say("✅ 批量完成：%d 条 → %s%s"
                 % (len(moved), target_where,
                    ("；%d 条没成（见上面）" % len(failed)) if failed else ""),
                 "ok" if not failed else "warn")
        return not failed

    def cmd_task_assign(self, args, refresh=True):
        """task assign <session ID>（默认当前任务）或 task assign <任务ID> <session ID>。

        ``session ID`` 还可以是 ``none``（下架）或 ``current``（当前这一场）：

            task assign none            ← 把选中的任务从它那一场下架
            task 8 assign current       ← 把任务 8 挂到当前这一场
            task 8 -an / task 8 -ac     ← 上面两条的紧凑写法

        写入顺序：先在 Focusmate 给那一场建 task 并复核，成功后才落本地；
        失败不产生半截 assignment。
        """
        if not args:
            raise ValueError("用法：task assign <session ID|none|current>（先 task select/create）")
        if len(args) >= 2:
            task = self.resolve_task(args[0])
            session_token = args[1]
        else:
            task = self._task_for_assign()
            session_token = args[0]
        return self._assign_task(task, session_token, refresh=refresh)

    def _assign_task(self, task, session_token, refresh=True, allow_unbooked=False):
        """把**已经拿到手**的任务挂到某一场（或下架）。内部路径全走这儿。

        ``cmd_task_assign`` 负责「用户敲的那个数 → 任务」，这里负责「任务 → 挂」。
        分开的原因很实际：内部调用方（swap / 批量转移 / refresh --check 重挂）
        手里是任务对象，绕回按序号解析会挂错人。

        ``allow_unbooked`` 只给项目 plan 用：用户拍板把没订上的场次也算进
        空场之后，任务可以**先本地挂上**，等 daemon 订上再由 ``refresh --check``
        补挂 Focusmate；其它命令路径仍然不允许往空气里挂任务。
        """
        # 手上那份可能是「刚创建 / 刚改完备注之前」的快照，挂之前重读一遍：
        # Focusmate 那条 task 的标题和 description 要的是**现在**的字段。
        try:
            task = self.task_store.get(task.get("id"))
        except (ValueError, TypeError):
            pass
        session_idx, row = self._resolve_assign_target(session_token)
        if self._session_kind(session_token) == "current" and row is not None:
            self.say("「当前这一场」= 表格 #%d（%s %s %s）"
                     % (session_idx, row.get("date") or self.sel_date.isoformat(),
                        row.get("startLabel"), row.get("phase") or ""), "info")
        if row is None:
            # `-a none`：下架。它不是一个「场次」，所以不走下面那套建 task 的流程。
            if ts.TaskStore.is_repeat_template(task):
                self.say("🔁 任务 %s 是重复任务本体，本来就不占 session；要结束这套重复："
                         "`task repeat %s off`（保留任务）或 `task %s drop`（手动放弃）。"
                         % (self.task_label(task), self.task_label(task),
                            self.task_label(task)), "info")
                return task
            self.selected_task_id = task["id"]
            task = self._task_detach(task, reason="cmd-assign-none")
            self.rebuild_rows()
            return task
        date_iso = row.get("date") or self.sel_date.isoformat()
        if row.get("id") is None:
            if not allow_unbooked:
                raise ValueError("session #%d 还没有 Focusmate session id（没订上/快照），"
                                 "不能挂 task。先 refresh 成功一次。" % session_idx)
            # 项目 plan 专用：这场还没订上，但用户已经把它算进空场了 —— 任务先
            # 本地记到这场（Focusmate 上没有真实 session，不发起网络挂载），
            # 等订上后 `refresh --check` 会补挂。
            self._exclusive_guard(task, date_iso, row.get("startLabel"))
            self.task_store.assign(task["id"], {
                "index": session_idx,
                "date": date_iso,
                "start": row.get("startLabel"),
                "end": row.get("endLabel"),
                "duration": row.get("durationMin"),
                "focusmate_session_id": None,
            })
            self.say("🗓 任务 %s「%s」先本地挂到 session #%d（%s %s；这一场还没订上，"
                     "等 Focusmate 订上后 refresh --check 会自动补挂）。"
                     % (self.task_label(task),
                        (task.get("fields") or {}).get("title") or self.task_label(task),
                        session_idx, date_iso, row.get("startLabel")), "cmd")
            self.rebuild_rows()
            self.invalidate()
            return task
        if ts.TaskStore.is_repeat_template(task):
            # 本体不能进 session；-a/-ac 指到哪一天，就挂/生成那一天的子任务。
            probe = datetime.date.fromisoformat(date_iso)
            if not ts.TaskStore.repeat_due_on(task, probe):
                raise ValueError("任务 %s 是重复任务本体，而这个 session 在 %s，"
                                 "不是它的规则日；本体不占 session。切到它的规则日再挂，"
                                 "或者直接 `task %s -f end` 让 formatter 排子任务。"
                                 % (self.task_label(task), date_iso,
                                    self.task_label(task)))
            self.ensure_repeat_occurrences(probe, apply_formatters=True)
            child = self.task_store.occurrence_for(task["id"], probe)
            if child is None:
                raise ValueError("任务 %s 的 %s 子任务没生成出来（先看 task sync 的错误）。"
                                 % (self.task_label(task), date_iso))
            self.say("🔁 本体 %s 不占 session；这次挂的是它 %s 的子任务 %s。"
                     % (self.task_label(task), date_iso, self.task_label(child)), "info")
            task = child
        self._exclusive_guard(task, date_iso, row.get("startLabel"))
        session = {
            "index": session_idx,
            "date": date_iso,
            "start": row.get("startLabel"),
            "end": row.get("endLabel"),
            "duration": row.get("durationMin"),
            "focusmate_session_id": row.get("id"),
        }
        fields = task.get("fields") or {}
        title = self.task_store.focusmate_title(task)
        notes = self.task_store.notes(task) or fields.get("description") or ""
        if not isinstance(notes, str):
            notes = str(notes)
        # 这场可能已经挂着别的任务了 —— Focusmate 侧是 n:n，可以再挂一个，
        # 只是 UI 面板里不再出现 "Add task"，所以桥那边默认走内部 API（见 bridge.mjs）。
        prev = task.get("session") or {}
        moved = bool(prev) and (prev.get("date") != date_iso
                               or prev.get("start") != row.get("startLabel"))
        if moved:
            self.say("注意：任务 %s 原来挂在 %s %s；本地会改记到这场，Focusmate 上那条旧 task "
                     "不会跟着挪（完成时间照样会归档）。"
                     % (self.task_label(task), prev.get("date"), prev.get("start")), "warn")
        already = [t for t in self.task_store.tasks_for_session(date_iso, row.get("startLabel"))
                   if t.get("id") != task["id"]]
        self.say("把任务 %s「%s」挂到 session #%d（%s %s）%s…"
                 % (self.task_label(task), title, session_idx, date_iso,
                    row.get("startLabel"),
                    "（这场已有 %d 个任务，再去挂一个）" % len(already) if already else ""), "cmd")
        return self._op_task_assign(task, session, session_idx, date_iso, title, notes,
                                    refresh=refresh, already=len(already),
                                    start_ms=row.get("startMs"))

    def _op_task_assign(self, task, session, session_idx, date_iso, title, notes,
                        refresh=True, already=0, start_ms=None):
        """**乐观**挂任务：本地立刻记上（打 pending 标记），网络丢进后台。

        用户要的就是这个：点完就有反应，别等浏览器。成功了把 pending 换成真
        task id；失败了把整条任务放回操作前的快照，并且记进失败账（re-action）。
        """
        before = copy.deepcopy(task)
        task_id = task["id"]
        self.task_store.assign(task_id, session)
        self.task_store.attach_focusmate(task_id, {
            FM_PENDING: {"at": ts._now(), "via": "optimistic",
                         "op": "task.assign"},
            "session_id": session.get("focusmate_session_id"),
            "title": title,
        }, session=session)
        self.selected_task_id = task_id
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("task.assign",
                                 "挂任务 %s 到 %s %s" % (self.task_label(task), date_iso,
                                                        session.get("start") or "?"),
                                 {"task_id": task_id, "date": date_iso,
                                  "start": session.get("start"),
                                  "duration": session.get("duration"),
                                  "meeting_id": session.get("focusmate_session_id"),
                                  # startMs 不在 session 字典里（那是界面用的行字段），
                                  # 必须作为参数存下来：重放的时候只剩这份参数了
                                  "startMs": start_ms,
                                  "title": title, "notes": notes,
                                  "session": dict(session)})
        job = self.jobs.submit(action, on_fail=lambda error: self._assign_failed(
            task_id, before, title, error))
        if refresh and not self.offline and self.jobs.sync:
            # 只有同步模式才顺手对一次账（老行为）。异步模式表格已经是乐观结果，
            # 再排一次 refresh 只会变成这次操作多出来的一步，还可能把刚铺好的行刷掉。
            try:
                self.refresh(quiet=True)
            except Exception:                       # noqa: BLE001 - 刷新失败不影响已挂上的
                pass
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                raise ValueError("Focusmate 里没能确认 task 已挂上；本地已经回滚（%s）"
                                 % job.outcome.error)
            return True
        self.say("⏳ 任务 %s「%s」已经记到 session #%d 上（Focusmate 那边在后台挂，"
                 "进度看面板）；挂了我会说，没挂上会回滚并记账。"
                 % (self.task_label(task), title, session_idx), "cmd")
        return True

    def _assign_failed(self, task_id, before, title, error):
        """挂失败：回滚到操作前，并且说实话（下一步敲什么也一起说）。"""
        self._revert_task(task_id, before,
                          "任务「%s」没挂到 Focusmate：%s" % (title, error))
        self.say("  下一步：re-action 重放，或者 task %s -a <session> 再挂一次。"
                 % self.task_label(before), "warn")

    def op_task_detach(self, task, reason="net-detach"):
        """**乐观**下架：本地先断绑定，Focusmate 那条 task 在后台删。

        顺序仍然按老规矩办事（先删 Focusmate、再断本地），只是「删」变成了
        后台的一件事：本地先断，失败就整体放回快照（＝那条 task 其实还在）。
        """
        sess = task.get("session") or {}
        fm = task.get("focusmate") or {}
        fm_id = fm.get("task_id") or fm.get("id")
        before = copy.deepcopy(task)
        task_id = task["id"]
        self.task_store.unassign(task_id, reason=reason)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("task.detach",
                                 "下架任务 %s" % self.task_label(task),
                                 {"task_id": task_id, "focusmate_task_id": fm_id,
                                  "date": sess.get("date"), "start": sess.get("start"),
                                  "reason": reason})
        job = self.jobs.submit(action, on_fail=lambda error: self._detach_failed(
            task_id, before, error))
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                return self.task_store.get(task_id)
            return self.task_store.get(task_id)
        self.say("⏏ 任务 %s 已经从 %s %s 下架（Focusmate 那边在后台删这条 task）"
                 % (self.task_label(task), sess.get("date") or "?",
                    sess.get("start") or "?"), "ok")
        return self.task_store.get(task_id)

    def _detach_failed(self, task_id, before, error):
        """删失败：先复核 Focusmate，真没了就保持下架，真还在才回滚绑定。

        桥的 DELETE 和「删完复核」是两段：如果 DELETE 已经成功、复核阶段自己
        抛错（例如 ``sleep is not defined``），只看错误会把本地绑定放回去 ——
        Focusmate 已经没有这条 task，契约表却还挂着，``-an`` 看起来永远下不掉。
        所以失败回调再 GET 一次任务列表：**id 不在 = 远端确实删了**，本地就
        保持操作后的「已下架」；只有远端仍能查到、或查不到状态时才回滚。
        """
        fm = (before or {}).get("focusmate") or {}
        fm_id = fm.get("task_id") or fm.get("id")
        if fm_id:
            remote = self._remote_tasks()
            if remote is not None and str(fm_id) not in remote:
                self.rebuild_rows()
                self.invalidate()
                self.say("⏏ Focusmate task %s 已经不在（删除已生效；原报错：%s）；"
                         "本地保持下架，不恢复绑定。"
                         % (str(fm_id)[:8], error), "warn")
                return
        self._revert_task(task_id, before, "Focusmate task 没删掉：%s" % error)

    @staticmethod
    def _hhmm_distance(a, b):
        """两个 HH:MM 差多少分钟（认不出就当无穷远）。"""
        pa, pb = sp.parse_hhmm(a), sp.parse_hhmm(b)
        if pa is None or pb is None:
            return 10 ** 6
        return abs((pa[0] * 60 + pa[1]) - (pb[0] * 60 + pb[1]))

    def _repeat_session_for(self, parent, day, sessions=None):
        """本体在 ``day`` 优先挂哪一场 → ``(session, 原因)``。

        先找同起点、同时长的精确落点；找不到就选当天离 ``repeat.start`` 最近的
        一场 —— 用户明确说过「这一天具体什么时候不重要」，所以生成任务不因为
        排布被拆开就消失。当天完全没有 session 时返回 ``(None, ...)``，
        occurrence 子任务仍然会生成，只是暂时不挂场。
        """
        sessions = list(sessions if sessions is not None
                        else self.sessions_for_day_plan(day))
        # 今天已经开始的场次，只有 API 里真有 id 才能拿来自动挂重复任务。
        # 否则一次日终/次日重开 TUI 会把今天的新 occurrence 硬塞回已经过去的
        # 19:00 幽灵场（data.csv 有、Focusmate 早已没有），看起来就像任务又
        # “留在取消的 session 上”。
        if day <= datetime.date.today():
            now = self._now_dt()
            usable = []
            for sess in sessions:
                start = self._session_start_datetime(sess)
                if start is None or start > now:
                    usable.append(sess)
                    continue
                if self.authoritative and self._formatter_session_has_actual(day, sess):
                    usable.append(sess)
            sessions = usable
        hit = self.task_store.repeat_occurrence(parent, day, sessions)
        if hit:
            return hit, "exact"
        rep = parent.get("repeat") or {}
        want = rep.get("start")
        best = None
        best_key = None
        for sess in sessions or []:
            key = (self._hhmm_distance(want, sess.get("start")), sess.get("start") or "")
            if best_key is None or key < best_key:
                best, best_key = sess, key
        if best:
            return best, "nearest"
        return None, "no-session"

    def _repeat_formatter_done(self, child, day):
        """子任务在 ``day`` 上是不是已经跑过 formatter（有当天的 reservation）。"""
        fmt = (child or {}).get("formatter") or {}
        reservation = fmt.get("reservation") or {}
        return reservation.get("date") == day.isoformat()

    def _format_repeat_child(self, parent, child, day, quiet=True):
        """把本体上的 formatter 模板落到一条子任务上；失败只警告、不掀 TUI。"""
        template = ts.TaskStore.hook_template(parent.get("formatter"))
        if not template:
            return child
        current = ts.TaskStore.hook_template(child.get("formatter"))
        if current != template:
            child = self.task_store.set_hook(child["id"], "formatter", template)
        try:
            return self._apply_formatter_to_task(
                child, template, action="repeat", day=day, quiet=quiet)
        except Exception as e:                          # noqa: BLE001 - 一条排不上不该挡别的
            self.had_error = True
            self.say("⚠ 重复任务子任务 %s 的 formatter %s 没排上：%s"
                     % (self.task_label(child), template.get("name") or "?", e), "warn")
            return self.task_store.get(child["id"])

    def ensure_repeat_occurrences(self, day=None, apply_formatters=True,
                                  force_formatters=False, only_formatted=False):
        """生成 ``day`` 这一天该有的重复子任务（本地；formatter 可能碰 Focusmate）。

        本体（带 ``repeat``、没有 ``repeat_parent``）永远不挂 session；每到一个
        规则日就生成一条子任务（``repeat_parent`` + ``repeat_date``），并把
        ``repeat.occurrences[日期]`` 记到本体上。子任务能挂到当天 session 就挂，
        挂不到也照建 —— 晚点用户手动 assign 或 task sync 再补。

        本体挂了 formatter 时，子任务创建后自动继承并**当场排一遍**（``days``
        只看今天/以后）。``force_formatters=True`` 用于用户刚改了本体的 formatter，
        要求连已经排过的子任务也重排。``only_formatted=True`` 只处理挂了 formatter
        的本体（TUI 每天会顺手把明天那条也准备好，让用户今天就能看到）。
        """
        day = day or self.sel_date
        iso = day.isoformat()
        sessions = self.sessions_for_day_plan(day)
        # 旧数据里的本体可能带着 session：先把那一场转给当天的子任务。
        for candidate in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(candidate):
                continue
            if ((candidate.get("session") or {}).get("date") or "") == iso:
                self._adopt_template_session(candidate)
        changed = []
        today = datetime.date.today()
        for parent in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(parent):
                continue
            if not self.task_store.is_open(parent):
                # 本体被手动放弃/完成（结束这套重复）后就不再生成子任务。
                continue
            if only_formatted and not parent.get("formatter"):
                continue
            if not ts.TaskStore.repeat_due_on(parent, day):
                continue
            session, why = self._repeat_session_for(parent, day, sessions)
            child = self.task_store.occurrence_for(parent["id"], day)
            if child is None:
                child = self.task_store.create_repeat_occurrence(
                    parent["id"], day, session=session)
                changed.append(child)
            elif session and not (child.get("session") or {}):
                # 之前生成时没有 session，现在排出来了 → 补挂（只补空的，
                # 不覆盖用户手动挪到别的场次）。
                child = self.task_store.assign(child["id"], session)
                changed.append(child)
            # formatter 属于**子任务**：本体只留模板。今天及以后自动排；
            # 过去那天不补排（formatter 不允许排到过去）。
            if (apply_formatters and day >= today and parent.get("formatter")
                    and (force_formatters or not self._repeat_formatter_done(child, day))):
                before = child.get("formatter") or {}
                child = self._format_repeat_child(parent, child, day, quiet=True)
                if (child.get("formatter") or {}) != before:
                    changed.append(child)
            occ = ((parent.get("repeat") or {}).get("occurrences") or {}).get(iso) or {}
            target = child.get("session") or session
            fm = child.get("focusmate") or {}
            fm_id = fm.get("task_id") or fm.get("id")
            occ_fm = (occ.get("focusmate") or {}).get("task_id")
            if (occ.get("child_id") != child["id"]
                    or (target and occ.get("session") != target)
                    or (fm_id and occ_fm != fm_id)):
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=target,
                    focusmate=fm or None, note=why)
        return changed

    def expire_stale_repeat_occurrences(self, today=None):
        """把**已经过期、从未处理过**的重复 occurrence 收掉，避免它们天天复活。

        例如 FREQ=DAILY 的模板在 11-04 / 11-05 各生成过一条子任务，但用户那两天
        没做也没收尾；它们会一直是 active。以前 refresh 会把这些旧子任务也当成
        「今天要排的 formatter 任务」，于是一次 refresh 就出现好几条同名任务抢同一场
        session。正确口径是：**一条 occurrence 只属于它的 repeat_date**；日期过了、
        又没有处理痕迹，就记一笔 missed 并放弃（模板本身照常生成今天的新 occurrence）。

        只处理有 active 重复模板的旧子任务；模板已经删除/结束的孤儿记录不动。
        返回被收掉的 ``[(old_task, parent_id), ...]``。
        """
        today = today or datetime.date.today()
        expired = []
        for task in list(self.task_store.list()):
            if not ts.TaskStore.is_repeat_occurrence(task):
                continue
            if not self.task_store.is_open(task):
                continue
            raw_day = str(task.get("repeat_date") or "").strip()
            if not raw_day:
                continue
            try:
                repeat_day = datetime.date.fromisoformat(raw_day)
            except ValueError:
                continue
            if repeat_day >= today:
                continue
            parent_id = task.get("repeat_parent")
            try:
                parent = self.task_store.get(parent_id)
            except (TypeError, ValueError):
                continue
            if not (ts.TaskStore.is_repeat_template(parent)
                    and self.task_store.is_open(parent)):
                continue
            # 用户在那一天明确处理过（收尾里选了 skip / 延期等）就尊重历史，
            # 不在这里二次改账；这里只收「完全没人碰过」的僵尸 occurrence。
            if self.task_store.processed_on(task, raw_day):
                continue
            stamp = datetime.datetime.combine(
                repeat_day, datetime.time(23, 59, 59)).isoformat(timespec="seconds")
            try:
                self.task_store.mark_processed(
                    task["id"], repeat_day, action="missed", at=stamp)
                # 旧 bug 可能已经把这场旧 occurrence 拖到今天并挂在某场幽灵
                # session 上。收掉时一并断绑；否则它还会以 ✗ 的身份留在今天
                # 的契约行里，看起来像“任务没被移走”。
                try:
                    task = self.task_store.unassign(
                        task["id"], reason="repeat-missed")
                except (ValueError, TypeError):
                    pass
                task = self.task_store.drop(
                    task["id"], reason="repeat-missed", at=stamp)
            except (ValueError, TypeError):
                continue
            expired.append((task, parent_id))
        if expired:
            names = "、".join(
                "id:%s@%s" % (task.get("id"), task.get("repeat_date"))
                for task, _parent in expired[:5])
            self.say("🔁 收掉 %d 条过期未处理的重复 occurrence（%s）；"
                     "模板会照常生成今天的新一条。" % (len(expired), names), "warn")
            self.invalidate()
        return expired

    def maybe_generate_repeat_occurrences(self):
        """TUI 每帧问一次；每天只自动扫一遍（外加 60 秒节流）。"""
        today = datetime.date.today()
        now = time.time()
        if (self._repeat_gen_day == today
                and now - self._repeat_gen_at < 60.0):
            return []
        self._repeat_gen_day = today
        self._repeat_gen_at = now
        try:
            expired = self.expire_stale_repeat_occurrences(today)
            created = self.ensure_repeat_occurrences(today)
            if expired:
                created = [t for t, _parent in expired] + list(created or [])
            # 挂了 formatter 的本体：顺手把明天那条也生成/排好，用户今天就能在
            # `day +1` 里看到「明天会被 format 成什么样」。没挂 formatter 的
            # 本体不提前生成（仍然等到那天）。
            has_formatter = any(
                ts.TaskStore.is_repeat_template(t) and t.get("formatter")
                and self.task_store.is_open(t)
                for t in self.task_store.repeated_tasks())
            if has_formatter:
                created += self.ensure_repeat_occurrences(
                    today + datetime.timedelta(days=1), only_formatted=True)
        except Exception as e:                          # noqa: BLE001 - 生成失败不该拖死看板
            self.had_error = True
            self.say("重复任务生成失败：%s" % e, "warn")
            return []
        if created:
            self.rebuild_rows()
            self.invalidate()
        return created

    def materialize_repeats(self):
        """把选中那天的重复子任务挂上 Focusmate（本地 occurrence 先由 ensure 生成）。

        日期只看 RRULE / weekday；具体挂哪一场由 :meth:`_repeat_session_for`
        决定（精确匹配优先，其次当天最近一场）。Focusmate 写入必须复核成功才
        落本地，失败就把错误抛给调用方，绝不假成功。
        """
        day = self.sel_date
        iso = day.isoformat()
        self.ensure_repeat_occurrences(day)
        self.rebuild_rows()
        done = []
        for parent in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(parent):
                continue
            if not ts.TaskStore.repeat_due_on(parent, day):
                continue
            child = self.task_store.occurrence_for(parent["id"], day)
            if child is None:
                continue
            base = child.get("session") or {}
            if not base:
                continue
            row = next((r for r in self.rows
                        if r.get("planned") and r["startLabel"] == base.get("start")), None)
            session = dict(base)
            session.update({
                "index": self.rows.index(row) + 1 if row in self.rows else base.get("index"),
                "date": iso,
                "start": base.get("start"),
                "end": base.get("end"),
                "duration": base.get("duration"),
                "focusmate_session_id": (row or {}).get("id") or base.get("focusmate_session_id"),
            })
            occ = ((parent.get("repeat") or {}).get("occurrences") or {}).get(iso) or {}
            already = occ.get("focusmate") or {}
            if already.get("task_id"):
                continue
            if row is not None and row.get("id") and not self.offline:
                title = self.task_store.focusmate_title(child)
                notes = (child.get("fields") or {}).get("notes") or ""
                # 和 task <序号> -ac 同一条异步路径：本地先挂 pending，Focusmate
                # 建 task 丢后台。sync 模式（测试 / --exec）仍当场复核完。
                try:
                    self._op_task_assign(
                        child, session, self.rows.index(row) + 1, iso, title, notes,
                        refresh=False, start_ms=row.get("startMs"))
                except ValueError:
                    # 同步模式下没复核成功：把错误往上传，cmd_task_sync 会报出来。
                    raise
                current = self.task_store.get(child["id"])
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=session,
                    focusmate=current.get("focusmate") or None)
            else:
                self.task_store.assign(child["id"], session)
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=session,
                    focusmate=None)
            done.append((child["id"], session["start"]))
        return done

    def cmd_task_sync(self, quiet=False):
        """GET /v1/tasks → 读 completedAt，给已完成任务写归档。

        顺带确保选中那天的重复子任务已经生成；子任务有 session 的再挂 Focusmate。

        **一次操作、至少两步**：`[focusmate] 拉 Focusmate task` →
        `[omnifocus] 写回 OmniFocus`（有要写回的才排这一步）。
        """
        self.jobs.begin(1, title="同步 task（task %s）"
                               % ("sync" if not quiet else "sync · 自动"))
        if not self.offline:
            try:
                for task_id, start in self.materialize_repeats():
                    self.say("🔁 任务 %s 已挂到 %s %s"
                             % (self.task_label(self.task_store.get(task_id)),
                                self.sel_date, start), "ok")
            except Exception as e:                      # noqa: BLE001
                self.had_error = True
                self.say("重复任务挂载失败：%s" % e, "warn")
        ok = self.sync_task_completions(quiet=quiet)
        # 完成写回 OmniFocus 已经在那里面做了（sync_task_completions 末尾），
        # 这里只补一句「到底关联上了几条」。
        if not quiet:
            self.report_omnifocus_links()
        return ok
