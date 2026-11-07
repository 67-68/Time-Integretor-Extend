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
for key, name in (
        ("TI_CALENDAR_DOWNLOAD", "download.json"),
        ("TI_SESSION_LAYOUTS", "layouts.json"),
        ("TI_SESSION_EXTRAS", "extras.json"),
        ("TI_SESSION_RULES", "rules.json"),
        ("TI_SETTINGS_FILE", "settings.json"),
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
check("task create ... -f end 受理",
      wd.handle("task create title=夜读 duration=25 -f end"), True)
night = [t for t in wd.task_store.list()
         if (t.get("fields") or {}).get("title") == "夜读"][0]
fmt = night.get("formatter") or {}
res = fmt.get("reservation") or {}
check("任务落在最后一块 23:30–00:00（25+5）",
      [res.get("start"), res.get("end")], ["23:30", "00:00"])
check("切分前缀是 75，不是原来的 50",
      fmt.get("result", {}).get("cut_prefix"), [75])
check("排布精确变成 [75,25]",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [75, 25])
check("当前只剩两场：22:15–23:30 + 23:30–23:55",
      [(s["start"], s["end"], s["duration"])
       for s in wd.sessions_for_day_plan()],
      [("22:15", "23:30", 75), ("23:30", "23:55", 25)])
check("不再额外造一次性 session", sp.load_extras()["dates"].get(ISO, []), [])
check("任务挂的就是切出来的那场",
      [night["session"]["start"], night["session"]["duration"]], ["23:30", 25])

print("== refresh：formatter 每轮重查；排布被改坏会自己掰回来 ==")
sp.set_layout(TODAY, "T1", [50])
wd.reload_periods()
wd.reload_entries()
sync_api(wd)
check("人为把排布改回 [50]", sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50])
wd.messages = []
check("refresh 受理", wd.handle("refresh"), True)
check("refresh 后 formatter 的精确切割恢复",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [75, 25])
night = wd.task_store.get(night["id"])
check("任务仍然在 23:30",
      [night["session"]["start"], night["session"]["duration"]], ["23:30", 25])
check_true("refresh 明确说了重新剪断",
           any("剪断成" in m[2] for m in wd.messages))
# 再 refresh 一次必须幂等：排布、绑定都不动，也不取消/新建任何东西。
wd.bridge.calls = []
check("第二次 refresh 受理", wd.handle("refresh"), True)
check("第二次 refresh 没有取消/新建 session",
      [c[0] for c in wd.bridge.calls if c[0] in ("cancel", "add_task")], [])
check("第二次 refresh 后排布仍然是 [75,25]",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [75, 25])

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
wd2 = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
              "type": "工作", "name": "项目", "calendar": "工作", "title": "项目"}])
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
wd2.task_store.assign(_task["id"], {"date": ISO, "start": "11:00",
                                    "duration": 50, "end": "11:50"})
wd2.projects.set_allocation(_project["id"], TODAY, 2)
wd2.projects.record_items(_project["id"], TODAY, [{
    "pk": "t-1", "task_id": _task["id"], "count": 2,
    "start": "11:00", "duration": 50}])
sync_api(wd2)
check("资源对得上时 plan 保留", bool(pj.plan_of(wd2.projects.by_id(_project["id"]),
                                                TODAY)), True)
check("refresh 后 plan 还在", (wd2.handle("refresh"),
                                bool(pj.plan_of(wd2.projects.by_id(_project["id"]),
                                                TODAY)))[1], True)
# 取消掉这一场：API 快照里没有它，计划就不能继续假装排好了。
wd2.bridge.sessions = []
wd2.api_rows = []
wd2.rows_source = "bridge"
wd2.rebuild_rows()
wd2.messages = []
check("取消场次后 refresh 受理", wd2.handle("refresh"), True)
check("对不上资源的 plan 被清空",
      pj.plan_of(wd2.projects.by_id(_project["id"]), TODAY), None)
check_true("明确让用户重新 plan",
           any("请重新 `plan`" in m[2] for m in wd2.messages))
check("原计划任务被下架，等待重新安排",
      wd2.task_store.get(_task["id"]).get("session"), None)

print("== end formatter：11:00–12:00 + 25 分钟任务 → 任务场次从 11:30 开始 ==")
wd_hour = fresh([{"id": "H1", "date": ISO, "start": "11:00", "end": "12:00",
                  "type": "工作", "name": "一小时", "calendar": "工作",
                  "title": "一小时"}])
check("先排一场 50", wd_hour.handle("period H1 50"), True)
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
      sp.get_layout(sp.load_layouts(), TODAY, "H1"), [25, 25])
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

if fails:
    print("\n%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("\n全绿。")
