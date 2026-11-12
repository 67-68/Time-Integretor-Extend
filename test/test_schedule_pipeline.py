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
wd_hour = fresh([{"id": "T1", "date": ISO, "start": "11:00", "end": "12:00",
                  "type": "工作", "name": "一小时", "calendar": "工作",
                  "title": "一小时"}])
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


print("== enlist formatter：优先孤立且正好同长的 session，不动大时间块 ==")
wd_en = fresh([
    {"id": "T1", "date": ISO, "start": "09:00", "end": "09:25",
     "type": "工作", "name": "孤立", "calendar": "工作", "title": "孤立"},
    {"id": "T2", "date": ISO, "start": "11:00", "end": "13:00",
     "type": "工作", "name": "大块", "calendar": "工作", "title": "大块"},
])
check("大块先排 50+50", wd_en.handle("period T2 50 50"), True)
wd_en.reload_entries()
sync_api(wd_en)
check("enlist 任务创建受理",
      wd_en.handle("task create title=收拾 duration=25 -f enlist"), True)
_en_task = [t for t in wd_en.task_store.list()
            if (t.get("fields") or {}).get("title") == "收拾"][0]
_en_res = (_en_task.get("formatter") or {}).get("reservation") or {}
check("选中孤立且正好 25 分钟的 09:00 场",
      [_en_res.get("start"), _en_res.get("end"),
       _en_res.get("session_duration"), _en_res.get("exact_fit"),
       _en_res.get("isolated")],
      ["09:00", "09:30", 25, True, True])
check("enlist 挂的是整场已有 session",
      [_en_task["session"]["start"], _en_task["session"]["duration"]], ["09:00", 25])
check("enlist 不改任何 layout",
      sp.get_layout(sp.load_layouts(), TODAY, "T2"), [50, 50])
wd_en.bridge.calls = []
check("enlist 后 refresh 受理", wd_en.handle("refresh"), True)
_en_after = wd_en.task_store.get(_en_task["id"])
check("refresh 幂等：reservation 不动",
      ((_en_after.get("formatter") or {}).get("reservation") or {}).get("start"),
      "09:00")
check("refresh 幂等：没有重挂/取消",
      [c[0] for c in wd_en.bridge.calls if c[0] in ("add_task", "cancel")], [])

print("== enlist formatter：只剩大块时只从块的开头/结尾塞，保整块连贯 ==")
wd_en2 = fresh([
    {"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
     "type": "工作", "name": "大块", "calendar": "工作", "title": "大块"},
])
check("大块先排 50+50", wd_en2.handle("period T1 50 50"), True)
wd_en2.reload_entries()
sync_api(wd_en2)
check("enlist 任务创建受理",
      wd_en2.handle("task create title=大块里的活 duration=25 -f enlist"), True)
_en2 = [t for t in wd_en2.task_store.list()
        if (t.get("fields") or {}).get("title") == "大块里的活"][0]
_res2 = (_en2.get("formatter") or {}).get("reservation") or {}
check("只在整块开头放，不切中间场",
      [_res2.get("start"), _res2.get("placement"), _res2.get("isolated")],
      ["11:00", "start", False])
check("挂的是整场 50 分钟 session，不是凭空切一场",
      [_en2["session"]["start"], _en2["session"]["duration"]], ["11:00", 50])
check("layout 仍是 50+50",
      sp.get_layout(sp.load_layouts(), TODAY, "T1"), [50, 50])

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


if fails:
    print("\n%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("\n全绿。")
