#!/usr/bin/env python3
"""refresh 排期流水线回归：formatter 先占时间，项目 plan 后校验，defer 不进队列。"""
import datetime
import importlib.util
import io
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="schedule-pipeline-test-")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")   # 测试日志别写进真 temp/logs
for key, name in (
        ("TI_CALENDAR_DOWNLOAD", "download.json"),
        ("TI_SESSION_LAYOUTS", "layouts.json"),
        ("TI_SESSION_EXTRAS", "extras.json"),
        ("TI_SESSION_RULES", "rules.json"),
        ("TI_SETTINGS_FILE", "settings.json"),
        ("TI_PLAN_PINS", "plan-pins.json"),
        ("TI_PLAN_DECISIONS", "plan-decisions.json"),
        ("TI_PLAN_WATCHDOG", "plan-watchdog.json"),
        ("TI_TASKS_FILE", "tasks.json"),
        ("TI_TASK_ARCHIVE", "archive.json"),
        ("TI_PROJECTS_FILE", "projects.json"),
        ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
        ("TI_NOSTART_FILE", "nostart.json"),
        ("TI_POSTSESSION_FILE", "postsession.json"),
        ("TI_SESSION_MUTES", "mutes.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

# 排布 / formatter / plan 改完之后统一走 `warden.book()` → `keep_session.py check`。
# 测试里绝不许真起那个子进程：它会读**真的** data.csv 和 state 文件，bridge
# 活着时还会真下单。需要断言「叫了 check」的用例自己局部覆盖 w.run_keep_session。
w.run_keep_session = lambda *a, **kw: (0, "", "")

sp, sch, ts, ks, pj, pp = w.sp, w.sch, w.ts, w.ks, w.pj, w.pp
sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **k: None
ks.fetch_session_details = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("测试里不许真连 Focusmate API"))
ks.fetch_sessions = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("测试里不许真连 Focusmate API"))

TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)
TOMORROW_ISO = TOMORROW.isoformat()
ISO = TODAY.isoformat()


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.sessions = []
        self.fm_seq = 0
        self.titles = {}
        self.attached = {}

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": [dict(row) for row in self.sessions]}
        if cmd == "tasks":
            return {"tasks": [
                {"id": tid, "title": self.titles.get(tid, ""), "completedAt": None,
                 "attachments": [{"type": "session", "externalId": mid}]}
                for tid, mid in self.attached.items()]}
        if cmd == "add_task":
            self.fm_seq += 1
            tid = "ft-%d" % self.fm_seq
            self.titles[tid] = kw.get("title")
            self.attached[tid] = kw.get("meetingId")
            return {"verified": True, "taskId": tid, "task": {"id": tid}}
        if cmd == "delete_task":
            self.attached.pop(str(kw.get("taskId") or ""), None)
            return {"verified": True, "taskId": kw.get("taskId")}
        if cmd == "release":
            return {"released": True}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def fresh(periods):
    sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO,
                      "days": 8, "profiles": ["工作", "空闲"], "events": [],
                      "periods": periods})
    for path in (sch.DATA_CSV, sch.BACKUP_CSV,
                 os.environ["TI_SESSION_LAYOUTS"],
                 os.environ["TI_SESSION_EXTRAS"],
                 os.environ["TI_TASKS_FILE"],
                 os.environ["TI_TASK_ARCHIVE"],
                 os.environ["TI_PROJECTS_FILE"],
                 os.environ["TI_PROJECT_SYNC_FILE"]):
        try:
            os.remove(path)
        except OSError:
            pass
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=False)
    # 收尾/到点提醒按 time.time() 触发，不看 clock_now：测试如果正好跑在
    # 「某场刚结束」的那几分钟里，refresh 会被问答吃掉（今天 23:05 就踩了）。
    # 这两问不是本文件要测的东西，直接关掉，保证一天里任何时刻都能跑。
    wd.settings.set("postsession", "0")
    wd.settings.set("nostart", "0")
    wd.reload_periods()
    wd.reload_entries()
    return wd


def sync_api(wd):
    """按当前 data.csv 造一份「已经订上」的 Focusmate 快照。"""
    rows = []
    for slot in sch.day_slots(wd.entries, wd.sel_date):
        h, m = map(int, slot["start"].split(":"))
        st = datetime.datetime.combine(wd.sel_date, datetime.time(h, m))
        en = st + datetime.timedelta(minutes=int(slot["duration"]))
        rows.append({
            "startMs": int(st.timestamp() * 1000),
            "endMs": int(en.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": en.strftime("%H:%M"),
            "durationMin": int(slot["duration"]), "id": "sess-" + slot["start"],
            "title": "", "quiet": False, "matched": False, "phase": "未开始",
            "date": wd.sel_date.isoformat()})
    wd.bridge.sessions = rows
    wd.api_rows = [dict(row) for row in rows]
    wd.rows_source = "bridge"
    wd.rebuild_rows()


print("== end formatter：把 22:15–00:05 的最后一块精准切给 25 分钟任务 ==")
wd = fresh([{"id": "T1", "date": ISO, "start": "22:15", "end": "00:05",
             "type": "工作", "name": "夜读", "calendar": "工作", "title": "夜读"}])
check("先排一场 50 分钟", wd.handle("period T1 50"), True)
wd.reload_entries()
sync_api(wd)
# 这一段要切 23:15 那场：固定时钟，别让「正好跑到 23:15 之后」把用例跑红。
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(21, 0))
check("task create ... -f end 受理",
      wd.handle("task create title=夜读 duration=25 -f end"), True)
night = [t for t in wd.task_store.list()
         if (t.get("fields") or {}).get("title") == "夜读"][0]
fmt = night.get("formatter") or {}
res = fmt.get("reservation") or {}
# 每场之后必须留 5 分钟/块的强制休息：75 分钟场 22:15–23:30 之后要到 23:45
# 才能再开一场，而 23:45 的 25 分钟会顶到 00:10（超过时段终点）。所以「最晚能
# 切」的位置是 23:15，前缀 [50]（22:15–23:05 + 10 分钟休息 = 23:15）。
check("任务落在最后一块 23:15–23:45（25+5，休息算进去了）",
      [res.get("start"), res.get("end")], ["23:15", "23:45"])
check("切分前缀是 50（75 分钟场后面接不到 23:30）",
      fmt.get("result", {}).get("cut_prefix"), [50])
check("排布精确变成 [50,25]",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50, 25])
check("当前只剩两场：22:15–23:05 + 23:15–23:40",
      [(s["start"], s["end"], s["duration"])
       for s in wd.sessions_for_day_plan()],
      [("22:15", "23:05", 50), ("23:15", "23:40", 25)])
check("不再额外造一次性 session", sp.load_extras()["dates"].get(ISO, []), [])
check("任务挂的就是切出来的那场",
      [night["session"]["start"], night["session"]["duration"]], ["23:15", 25])

print("== refresh：formatter 每轮重查；排布被改坏会自己掰回来 ==")
sp.set_layout(TODAY, "T1", [50])
wd.reload_periods()
wd.reload_entries()
sync_api(wd)
check("人为把排布改回 [50]", sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50])
wd.messages = []
check("refresh 受理", wd.handle("refresh"), True)
check("refresh 后 formatter 的精确切割恢复",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50, 25])
night = wd.task_store.get(night["id"])
check("任务仍然在 23:15",
      [night["session"]["start"], night["session"]["duration"]], ["23:15", 25])
check_true("refresh 明确说了重新剪断",
           any("剪断成" in m[2] for m in wd.messages))
# 再 refresh 一次必须幂等：排布、绑定都不动，也不取消/新建任何东西。
wd.bridge.calls = []
check("第二次 refresh 受理", wd.handle("refresh"), True)
check("第二次 refresh 没有取消/新建 session",
      [c[0] for c in wd.bridge.calls if c[0] in ("cancel", "add_task")], [])
check("第二次 refresh 后排布仍然是 [50,25]",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50, 25])

print("== 项目 plan：formatter 任务不进项目队列，defer 未到也不进 ==")
_proj = {"id": 1, "title": "P", "tasks": [
    {"pk": "fmt", "title": "formatter 任务", "duration": 25, "count": 1,
     "completed": False, "dropped": False},
    {"pk": "defer", "title": "以后再做", "duration": 25, "count": 1,
     "defer": (datetime.datetime.now() + datetime.timedelta(days=1)).isoformat(),
     "completed": False, "dropped": False},
    {"pk": "ready", "title": "现在能做", "duration": 25, "count": 1,
     "completed": False, "dropped": False},
]}
check("项目候选排除 formatter 任务",
      [x["pk"] for x in pj.remaining_tasks(_proj, exclude_pks=["fmt"])],
      ["ready"])
check("defer 还没到的项目任务不在队列",
      [x["pk"] for x in pj.remaining_tasks(_proj)],
      ["fmt", "ready"])
_items, _notes = pp.distribute(
    [{"project_id": 1, "project": _proj, "count": 3}], [_proj],
    exclude_pks={1: {"fmt"}})
check("distribute 也只挑 ready，不把 defer/formatter 算进去",
      [(x["pk"], x["count"]) for x in _items], [("ready", 1)])
# 本地 defer 的 formatter 任务不进 refresh 的 formatter 队列。
_deferred = wd.task_store.create({
    "title": "还没到点的 formatter", "duration": 25,
    "defer": (datetime.datetime.now() + datetime.timedelta(days=1)).isoformat()})
_deferred = wd.task_store.set_hook(_deferred["id"], "formatter", {
    "name": "end", "kind": "formatter", "params": [], "args": {}})
check("本地 defer 未到的 formatter 任务不会进入流水线队列",
      [t["id"] for t in wd.formatter_tasks_for_day(TODAY)
       if t["id"] == _deferred["id"]], [])
for t in list(wd.task_store.list()):
    wd.task_store.delete(t["id"])

print("== 项目 plan 对不上当前资源：清空 plan，强制重新安排 ==")
# 用**明天**：今天的 11:00 会随墙上时间滑进过去，那时「到点没开始」那一问会接管
# 输入（refresh 直接被它吃掉），用例就不再是「refresh 的 plan 校验」了。
_D = TOMORROW
wd2 = fresh([{"id": "T1", "date": TOMORROW_ISO, "start": "11:00", "end": "12:00",
              "type": "工作", "name": "项目", "calendar": "工作", "title": "项目"}])
wd2.day_offset = 1
check("时段先排 50", wd2.handle("period T1 50"), True)
wd2.reload_entries()
sync_api(wd2)
_payload = pj.project_payload(
    {"id": "p-1", "name": "项目 P", "kind": "project_root", "completed": False,
     "dropped": False},
    [{"id": "p-1", "name": "项目 P", "kind": "project_root",
      "completed": False, "dropped": False},
     {"id": "t-1", "name": "任务 A", "kind": "action", "project": "项目 P",
      "estimated_minutes": 50, "completed": False, "dropped": False}],
    now=datetime.datetime.now())
_project, _ = wd2.projects.upsert(_payload)
wd2.sync_project_tasks(_project)
_project = wd2.projects.by_id(_project["id"])
_task, _ = wd2.task_store.ensure_project_task(
    _project["id"], _project["title"],
    next(t for t in _project["tasks"] if t["pk"] == "t-1"))
wd2.task_store.assign(_task["id"], {"date": _D.isoformat(), "start": "11:00",
                                    "duration": 50, "end": "11:50"})
wd2.projects.set_allocation(_project["id"], _D, 2)
wd2.projects.record_items(_project["id"], _D, [{
    "pk": "t-1", "task_id": _task["id"], "count": 2,
    "start": "11:00", "duration": 50}])
sync_api(wd2)
check("资源对得上时 plan 保留", bool(pj.plan_of(wd2.projects.by_id(_project["id"]),
                                                _D)), True)
check("refresh 后 plan 还在", (wd2.handle("refresh"),
                                bool(pj.plan_of(wd2.projects.by_id(_project["id"]),
                                                _D)))[1], True)
# API 里暂时没有这一场（还没订上 / 登录态过期 / daemon 在退避）：
# **plan 必须留着**。以前这里整份清空 + 让用户重新 plan，重排一遍还是同样的结果
# （用户报的就是这一条：15:15 没订上，refresh 之后 plan 被清空）。
wd2.bridge.sessions = []
wd2.api_rows = []
wd2.rows_source = "bridge"
wd2.rebuild_rows()
wd2.messages = []
check("还没订上时 refresh 受理", wd2.handle("refresh"), True)
check_true("plan 保留（只是没订上，不是排期错了）",
           bool(pj.plan_of(wd2.projects.by_id(_project["id"]), _D)))
check_true("说清是「没订上」而不是「排错了」",
           any("还没订上" in m[2] for m in wd2.messages))
check("任务仍然挂在原场上（等补订）",
      (wd2.task_store.get(_task["id"]).get("session") or {}).get("start"), "11:00")

# daemon 认定「订过又没了 / 你放弃了」= 真取消：这一回 plan 必须失效。
ks.mark_slot("%sT11:00" % _D.isoformat(), "missed", note="booked-but-missing")
wd2.messages = []
check("真取消后 refresh 受理", wd2.handle("refresh"), True)
check("对不上资源的 plan 被清空",
      pj.plan_of(wd2.projects.by_id(_project["id"]), _D), None)
check_true("明确让用户重新 plan",
           any("请重新 `plan`" in m[2] for m in wd2.messages))
check("原计划任务被下架，等待重新安排",
      wd2.task_store.get(_task["id"]).get("session"), None)

print("== 排布必须留 Focusmate 的强制休息（25/50/75 → 5/10/15 分钟）==")
# 用户原话（2026-11-16）：「如果从 14:00 开始，订 75 分钟 session，是不能在
# 15:15 结束的，应该在 15:30 结束，focusmate 自动给你 15 分钟休息时间」。
# 以前 plan_period 只把下一场起点取整到 15 分钟，75 分钟场后面直接接 15:15 ——
# Focusmate 不让订（点 slot 不出 Book 按钮），17:30 那场就是这么一直订不上的。
_brk = fresh([{"id": "T1", "date": TOMORROW_ISO, "start": "14:00",
               "end": "18:00", "type": "工作", "name": "下午",
               "calendar": "工作", "title": "下午"}])
_brk.day_offset = 1
check("手动 layout 75 + 50 + 75 受理",
      _brk.handle("period T1 75 50 75"), True)
check("场次之间留休息：14:00(75) → 15:30(50) → 16:30(75)",
      [(r["startLabel"], int(r["durationMin"])) for r in _brk.rows
       if (r.get("date") or "") == TOMORROW_ISO],
      [("14:00", 75), ("15:30", 50), ("16:30", 75)])
_brk.messages = []
_brk.handle("period T1 75 50 75 25")     # 9 个 block 塞不进 8 个 block 的时段
check_true("9 个 block 的 layout 被明确拒绝（说放不下）",
           any("放不下" in m[2] for m in _brk.messages))
check("被拒绝后排布原样不动",
      [(r["startLabel"], int(r["durationMin"])) for r in _brk.rows
       if (r.get("date") or "") == TOMORROW_ISO],
      [("14:00", 75), ("15:30", 50), ("16:30", 75)])
_brk_period = {"id": "T9", "date": TOMORROW_ISO, "start": "09:00", "end": "12:30"}
check("默认排布也带休息：210 分钟 → 50+50+50+25（09:00/10:00/11:00/12:00）",
      [(s["start"], int(s["duration"]))
       for s in sp.plan_period(_brk_period, sp.default_durations(_brk_period))],
      [("09:00", 50), ("10:00", 50), ("11:00", 50), ("12:00", 25)])

print("== 项目 plan：小任务叠进大场时，plan 记的时长必须等于真挂上的那场 ==")
# 用户报的现场：手动 layout 14:00(75) / 15:15(50) / 16:15(75)，plan 里
# 75 分钟任务和小任务差位，refresh 报「绑定和 plan 不一致」把整天计划清空。
_Dmis = TOMORROW
wd_up = fresh([{"id": "T1", "date": TOMORROW_ISO, "start": "14:00",
                "end": "18:00", "type": "工作", "name": "下午",
                "calendar": "工作", "title": "下午"}])
wd_up.day_offset = 1
check("手动 layout 排成 75 + 50 + 75",
      wd_up.handle("period T1 75 50 75"), True)
wd_up.reload_entries()
sync_up = wd_up
check("先有一份「已经订上」的快照",
      (sync_api(wd_up), len(wd_up.rows))[1], 3)
_up_payload = pj.project_payload(
    {"id": "p-up", "name": "下午项目", "kind": "project_root",
     "completed": False, "dropped": False},
    [{"id": "p-up", "name": "下午项目", "kind": "project_root",
      "completed": False, "dropped": False},
     {"id": "t-big", "name": "大任务", "kind": "action", "project": "下午项目",
      "estimated_minutes": 75, "completed": False, "dropped": False},
     {"id": "t-tiny", "name": "十分钟小事", "kind": "action", "project": "下午项目",
      "estimated_minutes": 10, "completed": False, "dropped": False}],
    now=datetime.datetime.now())
_up_project, _ = wd_up.projects.upsert(_up_payload)
wd_up.sync_project_tasks(_up_project)
_up_project = wd_up.projects.by_id(_up_project["id"])
_up_slots, _ = wd_up._plan_slots(_Dmis)
up_allocator = pp.Allocator(_Dmis, [_up_project], _up_slots,
                            task_store=wd_up.task_store,
                            hooks=wd_up.plan_hooks())
_up_summary = wd_up._plan_apply(
    up_allocator,
    [{"project_id": _up_project["id"], "project": _up_project, "count": 4}],
    book=False)
check("plan 把 75 + 10 分钟都排进去了",
      (_up_summary["placements"], _up_summary["unscheduled"]), (2, 0))
_up_plan = pj.plan_of(wd_up.projects.by_id(_up_project["id"]), _Dmis)
_up_tasks = {t["fields"]["project_task"]: t for t in wd_up.task_store.list()
             if (t.get("fields") or {}).get("project_id") == _up_project["id"]}
check("大任务落在 14:00（75 分钟场）",
      [i for i in _up_plan["items"] if i["pk"] == "t-big"][0]["start"], "14:00")
check("十分钟小事落在 15:30（50 分钟场，75 分钟场之后要留 15 分钟休息）",
      [i for i in _up_plan["items"] if i["pk"] == "t-tiny"][0]["start"], "15:30")
check("plan item 记的时长 = 任务真挂上的那场时长（不是小任务自己的 10 分钟）",
      [(i["pk"], i["duration"],
        int((_up_tasks[i["pk"]].get("session") or {}).get("duration") or 0))
       for i in _up_plan["items"]],
      [("t-big", 75, 75), ("t-tiny", 50, 50)])
wd_up.messages = []
check("refresh 受理", wd_up.handle("refresh"), True)
check("refresh 之后 plan 还在（不再因为时长口径不同被清空）",
      bool(pj.plan_of(wd_up.projects.by_id(_up_project["id"]), _Dmis)), True)
check_true("没有报「绑定和 plan 不一致」",
           not any("绑定和 plan 不一致" in m[2] for m in wd_up.messages))

print("== end formatter：11:00–12:00 + 25 分钟任务 → 任务场次从 11:30 开始 ==")
wd_hour = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
                  "type": "工作", "name": "一小时", "calendar": "工作",
                  "title": "一小时"}])
wd_hour.clock_now = datetime.datetime.combine(TODAY, datetime.time(6, 0))
check("先排一场 50", wd_hour.handle("period T1 50"), True)
wd_hour.reload_entries()
sync_api(wd_hour)
check("11–12 的 25 分钟任务 -f end 受理",
      wd_hour.handle("task create title=半小时 duration=25 -f end"), True)
_hour_task = [t for t in wd_hour.task_store.list()
              if (t.get("fields") or {}).get("title") == "半小时"][0]
_hour_res = (_hour_task.get("formatter") or {}).get("reservation") or {}
check("任务占用 11:30–12:00",
      [_hour_res.get("start"), _hour_res.get("end")], ["11:30", "12:00"])
check("排布精确切成 [25,25]（任务从 11:30 开始）",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [25, 25])
check("任务那场是 11:30–11:55",
      [_hour_task["session"]["start"], _hour_task["session"]["end"]],
      ["11:30", "11:55"])
check("前半截也占满 11:00–11:30 的时间块（25+5）",
      [(_hour_task["formatter"]["result"]["cut_prefix"]),
       _hour_res.get("break_minutes")], [[25], 5])

print("== 同一时段的多条 formatter：refresh 不能互相清掉绑定 ==")
wd_multi = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
                   "type": "工作", "name": "多任务", "calendar": "工作",
                   "title": "多任务"}])
wd_multi.clock_now = datetime.datetime.combine(TODAY, datetime.time(6, 0))
check("先排 50+50", wd_multi.handle("period T1 50 50"), True)
wd_multi.reload_entries()
sync_api(wd_multi)
check("任务 A -f end", wd_multi.handle("task create title=多A duration=25 -f end"), True)
check("任务 B -f end", wd_multi.handle("task create title=多B duration=25 -f end"), True)
_ma = [t for t in wd_multi.task_store.list()
       if t["fields"].get("title") == "多A"][0]
_mb = [t for t in wd_multi.task_store.list()
       if t["fields"].get("title") == "多B"][0]
_multi_layout = sp.get_layout(sp.load_layouts(), TODAY, "T1")
_multi_a = (_ma["session"]["start"], _ma["session"]["duration"])
_multi_b = (_mb["session"]["start"], _mb["session"]["duration"])
# 让 API 快照里真的有当前排布对应的 id：这些时段在今天已经是“已开始”的
# 时刻，refresh 的幽灵场保护只认可 API 里实际存在的场次。
wd_multi.reload_entries()
sync_api(wd_multi)
wd_multi.messages = []
check("多 formatter refresh 受理", wd_multi.handle("refresh"), True)
_ma2 = wd_multi.task_store.get(_ma["id"])
_mb2 = wd_multi.task_store.get(_mb["id"])
check("refresh 后 A 绑定没被 B 重跑清掉",
      (_ma2["session"]["start"], _ma2["session"]["duration"]), _multi_a)
check("refresh 后 B 绑定没被 A 重跑清掉",
      (_mb2["session"]["start"], _mb2["session"]["duration"]), _multi_b)
check("refresh 后排布保持原样",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), _multi_layout)
check("refresh 没产生清绑定/剪断消息",
      [m[2] for m in wd_multi.messages
       if "清掉任务" in m[2] or "剪断成" in m[2]], [])


print("== enlist 走同一条流水线：在当天最早的时段尾巴上切一场 ==")
wd_en = fresh([
    {"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
     "type": "工作", "name": "早一点", "calendar": "工作", "title": "早一点"},
    {"id": "T2", "date": ISO, "start": "15:00", "end": "16:30",
     "type": "工作", "name": "晚一点", "calendar": "工作", "title": "晚一点"},
])
wd_en.clock_now = datetime.datetime.combine(TODAY, datetime.time(6, 0))
check("两个时段各切一场",
      (wd_en.handle("period T1 50"), wd_en.handle("period T2 25")), (True, True))
wd_en.reload_entries()
sync_api(wd_en)
check("enlist 任务创建受理",
      wd_en.handle("task create title=收拾 duration=25 -f enlist"), True)
_en_task = [t for t in wd_en.task_store.list()
            if (t.get("fields") or {}).get("title") == "收拾"][0]
_en_res = (_en_task.get("formatter") or {}).get("reservation") or {}
check("挑最早的时段尾巴：T1 11:50 之后 12:00 切一场",
      [_en_res.get("start"), _en_res.get("end"),
       _en_res.get("session_duration"), _en_res.get("period_id")],
      ["12:00", "12:30", 25, "T1"])
check("任务挂在自己切出来的那一场（不是塞进 50 分钟场）",
      [_en_task["session"]["start"], _en_task["session"]["duration"]],
      ["12:00", 25])
check("T1 尾巴追加成 [50,25]，前面那场一分钟没动",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50, 25])
check("T2 完全没被碰", sp.get_layout(sp.load_layouts(), TODAY, "T2"), [25])
check("结果是 carved + cut_period_id（和 end 同形）",
      [(_en_task["formatter"]["result"]["session"]).get("carved"),
       (_en_task["formatter"]["result"]["session"]).get("cut_period_id")],
      [True, "T1"])
wd_en.bridge.calls = []
check("enlist 后 refresh 受理", wd_en.handle("refresh"), True)
_en_after = wd_en.task_store.get(_en_task["id"])
check("refresh 幂等：reservation 不动",
      ((_en_after.get("formatter") or {}).get("reservation") or {}).get("start"),
      "12:00")
check("refresh 幂等：没有重挂/取消",
      [c[0] for c in wd_en.bridge.calls if c[0] in ("add_task", "cancel")], [])

print("== enlist 排满时明确报错，绝不把任务挤进已有 session ==")
wd_en2 = fresh([
    {"id": "T1", "date": ISO, "start": "09:00", "end": "09:25",
     "type": "工作", "name": "刚好一场", "calendar": "工作", "title": "刚好一场"},
    {"id": "T2", "date": ISO, "start": "11:00", "end": "13:00",
     "type": "工作", "name": "排满", "calendar": "工作", "title": "排满"},
])
check("大块先排 50+50", wd_en2.handle("period T2 50 50"), True)
wd_en2.reload_entries()
sync_api(wd_en2)
wd_en2.clock_now = datetime.datetime.combine(TODAY, datetime.time(6, 0))
wd_en2.messages = []
wd_en2.handle("task create title=没地方放 duration=25 -f enlist")
_en2 = [t for t in wd_en2.task_store.list()
        if (t.get("fields") or {}).get("title") == "没地方放"][0]
check("没有把任务塞进任何已有场次", _en2.get("session"), None)
check("明确报「放不下」", [1 for m in wd_en2.messages if "放得下" in m[2]], [1])
check("两个时段的 layout 都没被改",
      (sp.get_layout(sp.load_layouts(), TODAY, "T1"),
       sp.get_layout(sp.load_layouts(), TODAY, "T2")), (None, [50, 50]))

print("== enlist 不往已经过去的时段里排（今天） ==")
wd_en3 = fresh([
    {"id": "T1", "date": ISO, "start": "09:00", "end": "10:00",
     "type": "工作", "name": "早上", "calendar": "工作", "title": "早上"},
    {"id": "T2", "date": ISO, "start": "20:00", "end": "21:00",
     "type": "工作", "name": "晚上", "calendar": "工作", "title": "晚上"},
])
check("两段都只切一半", (wd_en3.handle("period T1 25"),
                        wd_en3.handle("period T2 25")), (True, True))
wd_en3.reload_entries()
sync_api(wd_en3)
wd_en3.clock_now = datetime.datetime.combine(TODAY, datetime.time(19, 0))
check("19:00 时 enlist 受理",
      wd_en3.handle("task create title=晚上收拾 duration=25 -f enlist"), True)
_en3 = [t for t in wd_en3.task_store.list()
        if (t.get("fields") or {}).get("title") == "晚上收拾"][0]
check("跳过已经过去的 09:00 那一段，落在 20:30",
      [_en3["session"]["start"], _en3["session"]["duration"]], ["20:30", 25])
check("早上那段的 layout 没动",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [25])

print("== enlist 优先级：其它 formatter 先跑，enlist 最后、项目 plan 之前 ==")
wd_pri = fresh([
    {"id": "P1", "date": ISO, "start": "09:00", "end": "10:00",
     "type": "工作", "name": "优先级", "calendar": "工作", "title": "优先级"},
])
_en_early = wd_pri.task_store.create({"title": "先登记的 enlist", "duration": 25})
wd_pri.task_store.set_hook(_en_early["id"], "formatter", {
    "name": "enlist", "kind": "formatter", "params": [], "args": {}})
_end_late = wd_pri.task_store.create({"title": "后登记的 end", "duration": 25})
wd_pri.task_store.set_hook(_end_late["id"], "formatter", {
    "name": "end", "kind": "formatter", "params": [], "args": {}})
check("formatter 队列按 end → enlist 排（不是按 id）",
      [t["formatter"]["name"] for t in wd_pri.formatter_tasks_for_day(TODAY)
       if t["id"] in (_en_early["id"], _end_late["id"])],
      ["end", "enlist"])

print("== 琐事屏：15:15 session，15:10 打开/refresh 仍然是琐事屏且 refresh 可用 ==")
wd_chore = fresh([
    {"id": "C1", "date": ISO, "start": "15:15", "end": "16:05",
     "type": "工作", "name": "三点一刻", "calendar": "工作", "title": "三点一刻"},
])
sync_api(wd_chore)
_chore_payload = pj.project_payload(
    {"id": "p-chore-enlist", "name": "琐事库", "kind": "project_root",
     "completed": False, "dropped": False},
    [{"id": "p-chore-enlist", "name": "琐事库", "kind": "project_root",
      "completed": False, "dropped": False},
     {"id": "chore-1", "name": "倒垃圾", "kind": "action",
      "project": "琐事库", "estimated_minutes": 25,
      "completed": False, "dropped": False}],
    now=datetime.datetime.now())
wd_chore.projects.upsert(_chore_payload)
check("绑定琐事项目", wd_chore.handle("set chore-project #1"), True)
wd_chore.chore_now = datetime.datetime.combine(TODAY, datetime.time(15, 10))
check("15:10 打开时当前时间不在 session 内", wd_chore._chore_active_session(), None)
check("15:10 第一次 refresh 后自动进琐事屏",
      (wd_chore.handle("refresh"), wd_chore.view.name)[1], "琐事")
wd_chore.messages = []
check("琐事屏里 refresh 仍可执行", wd_chore.handle("refresh"), True)
check("refresh 后仍留在琐事屏", wd_chore.view.name, "琐事")
check("没有「不认这个命令」的误报",
      [m[2] for m in wd_chore.messages if "不认这个命令" in m[2]], [])

print("== 查看明天：Day Plan 同时展示 formatter 任务和项目安排 ==")
wd_plan = fresh([
    {"id": "D1", "date": TOMORROW_ISO, "start": "11:00", "end": "12:00",
     "type": "工作", "name": "明日", "calendar": "工作", "title": "明日"},
])
wd_plan.day_offset = 1
wd_plan.reload_entries()
_plan_payload = pj.project_payload(
    {"id": "p-day", "name": "论文项目", "kind": "project_root",
     "completed": False, "dropped": False},
    [{"id": "p-day", "name": "论文项目", "kind": "project_root",
      "completed": False, "dropped": False},
     {"id": "day-task-1", "name": "写引言", "kind": "action",
      "project": "论文项目", "estimated_minutes": 50,
      "completed": False, "dropped": False}],
    now=datetime.datetime.now())
wd_plan.projects.upsert(_plan_payload)
# Day Plan 现在只展示每个 pro 当前阶段的 op；把这条 op 关联到真正项目。
# 直接走 store，避免当前恰好是琐事屏时命令被它吃掉。
_plan_pro, _ = wd_plan.projects.create_pro("论文项目")
wd_plan.projects.link_op_to_pro(1, _plan_pro["id"])
wd_plan.projects.set_op_type(1, "SOP")
_plan_task = wd_plan.task_store.create({
    "title": "写引言", "duration": 50, "count": 2,
    "project": "论文项目", "project_id": 1, "project_task": "day-task-1"})
wd_plan.task_store.assign(_plan_task["id"], {
    "date": TOMORROW_ISO, "start": "11:00", "end": "12:00", "duration": 60})
wd_plan.task_store.set_hook(_plan_task["id"], "formatter", {
    "name": "enlist", "kind": "formatter", "params": [], "args": {},
    "reservation": {"date": TOMORROW_ISO, "start": "11:00", "end": "12:00",
                    "session_start": "11:00", "session_end": "12:00",
                    "session_duration": 60, "duration": 50,
                    "break_minutes": 5, "placement": "start",
                    "task_id": _plan_task["id"]},
    "result": {"name": "enlist", "session": {
        "date": TOMORROW_ISO, "start": "11:00", "end": "12:00",
        "duration": 60, "enlisted": True}},
})
wd_plan.projects.set_allocation(1, TOMORROW, 2)
wd_plan.projects.record_items(1, TOMORROW, [{
    "pk": "day-task-1", "task_id": _plan_task["id"], "count": 2,
    "start": "11:00", "duration": 50}])
_buf = io.StringIO()
wd_plan.console = Console(file=_buf, width=180, force_terminal=False)
_binder = wd_plan.render_day_binder()
check_true("明天有编排表", _binder is not None)
if _binder is not None:
    wd_plan.console.print(_binder)
_binder_out = _buf.getvalue()
check_true("编排表显示 formatter 名称", "⏱enlist" in _binder_out)
check_true("编排表显示任务所属项目", "论文项目" in _binder_out)
check_true("编排表显示项目 plan 账", "项目安排" in _binder_out and "说好 2 count" in _binder_out)

print("== refresh 安全闸：绝不取消已经开始 / 已过去的 session（即使时间有偏差） ==")
wd_guard = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
                   "type": "工作", "name": "进行中", "calendar": "工作",
                   "title": "进行中"}])
check("guard：先排一场 50", wd_guard.handle("period T1 50"), True)
wd_guard.reload_entries()
sync_api(wd_guard)
_now_ms = int(datetime.datetime.now().timestamp() * 1000)
_guard_row = dict(wd_guard.rows[0])
# 故意制造「计划 11:00，但 Focusmate 实际 5 分钟前已开始」的时间偏差。
_guard_row.update({"startMs": _now_ms - 5 * 60 * 1000,
                   "endMs": _now_ms + 45 * 60 * 1000,
                   "phase": "进行中"})
wd_guard.bridge.sessions = [dict(_guard_row)]
wd_guard.api_rows = [dict(_guard_row)]
wd_guard.rows_source = "bridge"
wd_guard.rebuild_rows()
try:
    wd_guard._snapshot_cache.clear()
except AttributeError:
    pass
_guard_period = wd_guard.periods_for_day(TODAY)[0]
wd_guard.bridge.calls = []
# 以前这里有两条路（formatter 切割 / 改排布）各一份取消实现；现在统一成一份
# `_cancel_stale_period_sessions(day, period, new_sessions)`，返回 (cancelled, failed)。
_guard_cancelled, _guard_failed = wd_guard._cancel_stale_period_sessions(
    TODAY, _guard_period, [{"start": "11:30", "duration": 25}],
    reason="refresh-test")
check("guard：取消路径跳过进行中的场次", _guard_cancelled, [])
check("guard：取消路径没报失败", _guard_failed, False)
check("guard：取消路径没有发 cancel",
      [c[0] for c in wd_guard.bridge.calls if c[0] == "cancel"], [])
# 即使表格 zone 被错误标成 locked（时间偏差的典型表现），也只按实际 startMs 判定。
_guard_row["zone"] = "locked"
wd_guard.rows = [dict(_guard_row)]
wd_guard.bridge.calls = []
_guard_ok, _guard_failed2 = wd_guard._cancel_stale_period_sessions(
    TODAY, _guard_period, [{"start": "11:30", "duration": 25}])
check("guard：zone 标错时仍然不报失败", _guard_failed2, False)
check("guard：zone 标错也不发 cancel",
      [c[0] for c in wd_guard.bridge.calls if c[0] == "cancel"], [])

print("== refresh 集成：当前进行中的 API session 不会被 formatter 流水线取消 ==")
wd_live = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
                  "type": "工作", "name": "进行中", "calendar": "工作",
                  "title": "进行中"}])
check("live：先排一场 50", wd_live.handle("period T1 50"), True)
wd_live.reload_entries()
sync_api(wd_live)
_live_row = dict(wd_live.rows[0])
_live_row.update({"startMs": _now_ms - 5 * 60 * 1000,
                  "endMs": _now_ms + 45 * 60 * 1000,
                  "phase": "进行中"})
wd_live.bridge.sessions = [dict(_live_row)]
wd_live.api_rows = [dict(_live_row)]
wd_live.rows_source = "bridge"
wd_live.rebuild_rows()
try:
    wd_live._snapshot_cache.clear()
except AttributeError:
    pass
check("live：创建一个 end formatter 任务",
      wd_live.handle("task create title=当前任务 duration=25 -f end"), True)
# 把 layout 人为改坏，逼 refresh 重新跑 formatter / 切割逻辑。
sp.set_layout(TODAY, "T1", [50])
wd_live.reload_periods()
wd_live.reload_entries()
try:
    wd_live._snapshot_cache.clear()
except AttributeError:
    pass
wd_live.bridge.calls = []
check("live：refresh 受理", wd_live.handle("refresh"), True)
check("live：refresh 没有取消正在进行的那场",
      [c[0] for c in wd_live.bridge.calls if c[0] == "cancel"], [])
check("live：刷新后 API 里那场仍在",
      any(r.get("id") == "sess-11:00" for r in wd_live.api_rows), True)


print("== 重复 occurrence：旧日期不会被 refresh 拖到今天，也不会堆成同名任务 ==")
wd_rep = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
                 "type": "工作", "name": "重复", "calendar": "工作",
                 "title": "重复"}])
# 这组用例讲的是「11:00 已经开始的幽灵场」；把时钟钉在中午，
# 避免半夜跑测试时 11:00 其实还没到，用例假红。
wd_rep.clock_now = datetime.datetime.combine(TODAY, datetime.time(12, 0))
_parent = wd_rep.task_store.create({"title": "flashcard review & new learn",
                                    "duration": 25, "count": 1})
_parent = wd_rep.task_store.set_repeat(
    _parent["id"], TODAY.weekday(), "10:00", 25,
    rule="FREQ=DAILY", rule_anchor="%sT10:00:00" % ISO, source="omnifocus")
_old_day = TODAY - datetime.timedelta(days=2)
_old_child = wd_rep.task_store.create_repeat_occurrence(_parent["id"], _old_day)
_old_child = wd_rep.task_store.set_hook(_old_child["id"], "formatter", {
    "name": "end", "kind": "formatter", "params": [], "args": {},
    # 模拟已经被旧 bug 拖到今天的状态
    "reservation": {"date": ISO, "period_id": "T1", "start": "11:00",
                    "end": "11:30", "session_start": "11:00",
                    "session_duration": 25, "duration": 25},
})
check("旧 occurrence 即使 reservation 被改成今天，也不进今天的 formatter 队列",
      [t["id"] for t in wd_rep.formatter_tasks_for_day(TODAY)
       if t["id"] == _old_child["id"]], [])
_expired = wd_rep.expire_stale_repeat_occurrences(TODAY)
check("过期 occurrence 被收掉", [t["id"] for t, _p in _expired], [_old_child["id"]])
_old_after = wd_rep.task_store.get(_old_child["id"])
check("收掉后状态是 dropped", _old_after.get("status"), "dropped")
check("原因记成 repeat-missed", _old_after.get("status_reason"), "repeat-missed")
check("旧 occurrence 的幽灵 session 绑定也断掉", _old_after.get("session"), None)
check("旧日期留下 missed 处理痕迹",
      [h["action"] for h in wd_rep.task_store.processed_on(_old_after, _old_day)],
      ["missed"])
wd_rep.ensure_repeat_occurrences(TODAY, apply_formatters=False)
_today_child = wd_rep.task_store.occurrence_for(_parent["id"], TODAY)
check("今天的新 occurrence 仍能正常生成", bool(_today_child), True)
check("自动生成时不会把新 occurrence 挂回已经开始的 11:00 幽灵场",
      (_today_child or {}).get("session"), None)

print("== refresh：被取消的已开始 session 上的任务会先下架，再重找位置 ==")
wd_cancel = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
                    "type": "工作", "name": "取消场", "calendar": "工作",
                    "title": "取消场"}])
wd_cancel.clock_now = datetime.datetime.combine(TODAY, datetime.time(12, 0))
check("取消场：先排一场 50", wd_cancel.handle("period T1 50"), True)
wd_cancel.reload_entries()
# 本地说挂着 11:00，但 API 里没有任何这场（等同于 21:00 被取消）
_cancel_task = wd_cancel.task_store.create(
    {"title": "flashcard review & new learn", "duration": 25, "count": 1})
_now_ms = int(wd_cancel.clock_now.timestamp() * 1000)
_cancel_task = wd_cancel.task_store.assign(_cancel_task["id"], {
    "date": ISO, "start": "11:00", "end": "11:25", "duration": 25,
    "startMs": _now_ms - 5 * 60 * 1000,
    "focusmate_session_id": None})
_cancel_task = wd_cancel.task_store.set_hook(_cancel_task["id"], "formatter", {
    "name": "end", "kind": "formatter", "params": [], "args": {},
    "reservation": {"date": ISO, "period_id": "T1", "start": "11:00",
                    "end": "11:30", "session_start": "11:00",
                    "session_duration": 25, "duration": 25},
    "result": {"name": "end", "session": {
        "date": ISO, "start": "11:00", "duration": 25,
        "period_id": "T1", "cut_period_id": "T1", "cut_prefix": [25]}},
})
wd_cancel.api_rows = []
wd_cancel.rows_source = "bridge"
wd_cancel.rebuild_rows()
check("目标已开始但 API 没 id → 判成幽灵场",
      wd_cancel._formatter_bound_session_missing(_cancel_task, TODAY), True)
check("幽灵场 formatter 校验必须失效",
      wd_cancel._formatter_apply_valid(_cancel_task, TODAY), False)
wd_cancel.messages = []
wd_cancel.reconcile_schedule_pipeline(TODAY)
_cancel_after = wd_cancel.task_store.get(_cancel_task["id"])
check("refresh 后任务不再假装挂在取消场上", _cancel_after.get("session"), None)
check_true("明确说了旧场在 API 里不存在",
           any("已经在 API 里不存在" in m[2] for m in wd_cancel.messages))


print("== 预约认账一码一用：同一场 API session 不能同时顶两条计划 ==")
_d = datetime.datetime.combine(TODAY, datetime.time(20, 15))
_actual = [{"start": _d, "duration": 50, "id": "sess-2015"}]
_m_2000 = sch.match_slot(_actual, datetime.datetime.combine(TODAY, datetime.time(20, 0)), 50)
check("20:15 先认领 20:00 计划", _m_2000["kind"], "shifted")
_m_2100 = sch.match_slot(
    _actual, datetime.datetime.combine(TODAY, datetime.time(21, 0)), 25,
    used=[(s["start"], s["duration"]) for s in _m_2000["sessions"]])
check("同一场 20:15 不能再去覆盖 21:00 的计划", _m_2100["kind"], "none")


print("== period 是排期第一等输入：没切出 session 的时段也算容量 ==")
# 只有日历里的时段、data.csv 里一行都没有（用户今天 19:00–21:30 这种）：
# 以前这一段会被整段忽略（`plan` 看不到、契约表也不画）。
wd_p = fresh([{"id": "T1", "date": TOMORROW_ISO, "start": "19:00",
               "end": "21:30", "type": "空闲", "name": "空闲",
               "calendar": "空闲", "title": "空闲"}])
wd_p.day_offset = 1
wd_p.api_rows = []
wd_p.rows_source = "bridge"
wd_p.rebuild_rows()
_p_shells = [r for r in wd_p.rows if r.get("period_only")]
_p_sessions = [r for r in wd_p.rows if not r.get("period_only")]
check("契约表：没 session 的时段也有一行", len(_p_shells), 1)
check("占位行标着是哪个时段", _p_shells[0].get("period_id") if _p_shells else None, "T1")
check("占位行不是 session（没 id / 不算计划）",
      [(_p_shells[0].get("id"), _p_shells[0].get("planned"))] if _p_shells else [],
      [(None, False)])
check("没有别的 session 行", _p_sessions, [])
check("契约统计里也没有这一行", wd_p.plan_summary(), None)
_p_slots, _p_notes = wd_p._plan_slots(TOMORROW)
check("plan 能看到这一段的整段容量（150 分钟 → 5 count = 125 分钟）",
      [(s["start"], s["duration"], s["counts"]) for s in _p_slots],
      [("19:00", 125, 5)])
check("这一段是自动排布", [s.get("period_auto") for s in _p_slots], [True])

# 已经有 session 行的时段不再额外造一行/一个伪 slot：行就是容量。
sp.set_layout(TOMORROW, "T1", [50, 50], source="auto")
wd_p.rewrite_data_csv_from_plan()
wd_p.reload_periods()
wd_p.reload_entries()
wd_p.rebuild_rows()
check("切出 session 之后占位行消失",
      [r for r in wd_p.rows if r.get("period_only")], [])
_p_slots2, _p_notes2 = wd_p._plan_slots(TOMORROW)
check("自动 layout 仍然整段参与排期（不是只捡那两场）",
      [(s["start"], s["duration"], s["counts"]) for s in _p_slots2],
      [("19:00", 125, 5)])


print("== 已判死的自动场次要被剪掉（否则画幽灵行、继续占容量） ==")
wd_dead = fresh([
    {"id": "T1", "date": ISO, "start": "19:00", "end": "21:30",
     "type": "工作", "name": "晚上", "calendar": "工作", "title": "晚上"},
])
sp.set_layout(TODAY, "T1", [75, 25, 25], source="plan")
wd_dead.rewrite_data_csv_from_plan()
wd_dead.reload_periods()
wd_dead.reload_entries()
_stray = wd_dead.task_store.create({"title": "挂在死场次上", "duration": 25})
wd_dead.task_store.assign(_stray["id"], {
    "date": ISO, "start": "21:00", "end": "21:25", "duration": 25})
ks.save_state({"date": ISO, "slots": {
    "%sT19:00" % ISO: {"status": "booked", "duration": "75"},
    "%sT20:30" % ISO: {"status": "booked", "duration": "25"},
    "%sT21:00" % ISO: {"status": "abandoned", "duration": "25"},
}})
wd_dead.api_rows = [
    {"startLabel": "19:15", "startMs": 0, "durationMin": 75, "id": "s19"},
    {"startLabel": "20:45", "startMs": 0, "durationMin": 25, "id": "s20"},
]
check("剪掉 1 段", wd_dead._prune_dead_auto_slots(TODAY, quiet=True), 1)
check("layout 从 [75,25,25] 剪成 [75,25]",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [75, 25])
check("data.csv 里不再有 21:00",
      [slot["start"] for slot in sch.day_slots(wd_dead.entries, TODAY)],
      ["19:00", "20:30"])
check("挂在死场次上的任务被下架（不会继续假装挂着）",
      (wd_dead.task_store.get(_stray["id"]) or {}).get("session"), None)
# 手动 / formatter 的切分是硬约束：同样是死场次，也不能替用户剪。
sp.set_layout(TODAY, "T1", [75, 25, 25], source="manual")
wd_dead.rewrite_data_csv_from_plan()
wd_dead.reload_periods()
wd_dead.reload_entries()
check("手动 layout 不剪", wd_dead._prune_dead_auto_slots(TODAY, quiet=True), 0)
check("手动 layout 原样", sp.get_layout(sp.load_layouts(), TODAY, "T1"), [75, 25, 25])

print("== 认账过的真实场次不再单独画一行「计划外」（看起来像两场重叠） ==")
wd_dup = fresh([
    {"id": "T1", "date": ISO, "start": "19:00", "end": "21:30",
     "type": "工作", "name": "晚上", "calendar": "工作", "title": "晚上"},
])
wd_dup.clock_now = datetime.datetime.combine(TODAY, datetime.time(6, 0))
sp.set_layout(TODAY, "T1", [75, 25], source="manual")
wd_dup.rewrite_data_csv_from_plan()
wd_dup.reload_periods()
wd_dup.reload_entries()
ks.save_state({"date": ISO, "slots": {
    "%sT19:00" % ISO: {"status": "booked", "duration": "75", "match": "shifted",
                       "actual": "实际 19:15 75min（计划 19:00）"},
    "%sT20:30" % ISO: {"status": "booked", "duration": "25", "match": "shifted",
                       "actual": "实际 20:45 25min（计划 20:30）"},
}})
wd_dup.api_rows = [
    {"startLabel": "19:15", "startMs": 0, "endMs": 0, "durationMin": 75,
     "id": "s19", "title": "", "quiet": None, "phase": "已结束"},
    {"startLabel": "20:45", "startMs": 0, "endMs": 0, "durationMin": 25,
     "id": "s20", "title": "", "quiet": None, "phase": "已结束"},
]
wd_dup._snapshot_cache.clear()
wd_dup.rebuild_rows()
_dup_rows = [r for r in wd_dup.rows if not r.get("period_only")]
check("只画两行 session（19:00 / 20:30），不再额外画 19:15 / 20:45",
      [r.get("startLabel") for r in _dup_rows], ["19:00", "20:30"])
check("计划行仍然标成「认账」并带上实际时间",
      [(r.get("adopted"), r.get("actual_note")) for r in _dup_rows],
      [(True, "实际 19:15 75min（计划 19:00）"),
       (True, "实际 20:45 25min（计划 20:30）")])



if fails:
    print("\n%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("\n全绿。")
