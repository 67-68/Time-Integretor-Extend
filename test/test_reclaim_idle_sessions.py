#!/usr/bin/env python3
"""refresh 把「TI 自己切出来、没挂任务」的零散场次收回成未切分容量（2026-11-26）。

用户原话：「我希望 refresh 之后直接把零散 session 变成一个统一的时间块，如果没有
任务」「不是两场 25 合并 50，而是两场被自动切出来的 25 换成一个 50 分钟的无切分
时段，相当于资源回收」。

所以回收的终态是**那段时间没有任何 session**（不预约、不新建），容量还给时段，
下次 plan 想切几场切几场。要守住的几件事：

* 只收 TI 自己切的（layout / extras 的 source 是 plan/auto）；manual、formatter 不动；
* 挂着任务 / 已经开始 / 已经过去 的一律不动；**已经订上、马上就要开始的照收**
  （用户 2026-11-26：「就算那一段时间不行，这一个 session 为什么还留着？」）；
* layout 链只能收尾巴（中间摘一场会让后面每场起点都变）；
* 回收**不改变 plan 看到的容量**（这些场次在排期层本来就已经被当成可重切的）；
* 幂等：第二次 refresh 一场都不收；plan_sig 保持，不会触发无谓重排。
"""
import datetime
import importlib.util
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="reclaim-idle-test-")

for key, name in (("TI_LOG_DIR", "logs"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_SESSION_MUTES", "mutes.json"),
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
                  ("TI_OMNIFOCUS_OUTBOX", "omnifocus-outbox.json"),
                  ("TI_FAILED_ACTIONS", "failed-actions.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

w.run_keep_session = lambda *a, **kw: (0, "", "")
sp, sch, ts, ks, of, pj, pp = w.sp, w.sch, w.ts, w.ks, w.of, w.pj, w.pp

sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
for _name, _file in (("STATE_FILE", "state.json"),
                     ("LOCK_FILE", "keep_session.lock"),
                     ("EXPECTED_FILE", "keep_session-expected.json"),
                     ("VERDICTS_FILE", "verdicts.json"),
                     ("SINS_FILE", "sins.json"),
                     ("LOG_FILE", "keep_session.log"),
                     ("TUI_STATE_FILE", "warden-tui.json"),
                     ("SINS_ARCHIVE", "sins.jsonl"),
                     ("IDLE_STATE_FILE", "idle.json"),
                     ("IDLE_MODE_FILE", "mode.json"),
                     ("PAUSE_FILE", "pause.json")):
    setattr(ks, _name, os.path.join(TMP, _file))
ks.notify = lambda *a, **k: None
ks.fetch_session_details = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("测试里不许真连 Focusmate API"))
ks.fetch_sessions = ks.fetch_session_details

TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)
DAY = TOMORROW
DAY_ISO = DAY.isoformat()

fails = []


def check(label, got, want):
    if got == want:
        print("ok   %s" % label)
        return
    fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmniFocus(object):
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or ())]

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        return [dict(r) for r in self.rows]

    def get(self, pk, refresh=False):
        return next((dict(r) for r in self.rows
                     if str(r.get("id")) == str(pk)), None)

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def invalidate(self):
        pass


def fresh(day=DAY, extra_periods=()):
    """干净的沙盒 + 一个时段 T3 19:00–21:30（够 5 count）；extra_periods 可加。"""
    periods = [
        {"id": "T3", "date": day.isoformat(), "start": "19:00",
         "end": "21:30", "type": "空闲", "name": "其他项目",
         "calendar": "空闲", "title": "其他项目"},
    ] + [dict(p, date=day.isoformat()) for p in extra_periods]
    sp.save_extras(sp.empty_extras())
    sp.save_layouts(sp.empty_layouts())
    sp.save_download({
        "version": 1, "generated_at": "x", "start_date": day.isoformat(),
        "days": 8, "profiles": ["工作"], "events": [],
        "periods": periods})
    with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
        fh.write("# 空\n")
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus()
    wd.handle("day %d" % (day - TODAY).days)
    check("看板切到 %s" % day.isoformat(), wd.sel_date, day)
    return wd


def layout_of(wd, pid="T3", day=DAY):
    rec = sp.get_layout_record(sp.load_layouts(), day, pid) or {}
    return list(rec.get("durations") or []), rec.get("source"), rec.get("plan_sig")


def set_auto_layout(wd, durations, source="plan", day=DAY, pid="T3"):
    sig = wd._auto_plan_signature(day)
    sp.set_layout(day, pid, durations, source=source, plan_sig=sig)
    wd.rewrite_data_csv_from_plan()
    wd.rebuild_rows()


def bind(day_iso, start, duration, title="活"):
    task = ts.TaskStore().create({"title": title, "duration": duration})
    hour, minute = (int(x) for x in start.split(":"))
    end = datetime.datetime.combine(DAY, datetime.time(hour, minute)) \
        + datetime.timedelta(minutes=duration)
    ts.TaskStore().assign(task["id"], {
        "index": 0, "date": day_iso, "start": start,
        "end": end.strftime("%H:%M"), "duration": duration,
        "focusmate_session_id": "sess-" + start})
    return task


def book_all(wd):
    """把 data.csv 里的场次都标成「已经订上」（模拟 daemon 已经订好）。"""
    wd.reload_entries()
    wd.api_rows = []
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    for slot in sch.day_slots(wd.entries, wd.sel_date):
        hour, minute = (int(x) for x in slot["start"].split(":"))
        start = datetime.datetime.combine(wd.sel_date, datetime.time(hour, minute))
        end = start + datetime.timedelta(minutes=int(slot["duration"]))
        wd.api_rows.append({
            "startMs": int(start.timestamp() * 1000),
            "endMs": int(end.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": end.strftime("%H:%M"),
            "durationMin": int(slot["duration"]),
            "id": "sess-" + slot["start"], "title": "", "quiet": False,
            "matched": False,
            "phase": wd.phase_of(int(start.timestamp() * 1000),
                                 int(end.timestamp() * 1000), now_ms),
        })
    wd.rows_source = "bridge"
    wd.rebuild_rows()


def csv_rows(wd, day=DAY):
    rows = []
    with open(sch.DATA_CSV, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and parts[0] == day.isoformat():
                rows.append((parts[1], int(parts[2])))
    return rows


def extras_of(wd, day=DAY):
    return [(str(r.get("start")), int(r.get("duration") or 0),
             sp.extra_source(r))
            for r in sp.extras_for_date(sp.load_extras(), day)]


def capacity(wd):
    """plan 眼里这一天有多少 count（回收前后必须相等）。"""
    slots, _notes = wd._plan_slots(wd.sel_date)
    return sum(int(s.get("counts") or 0) for s in slots)


print("== 1) auto layout 尾巴上没任务的两场 → 收回容量，前面挂着任务的不动 ==")
wd = fresh()
bind(DAY_ISO, "19:00", 25)
set_auto_layout(wd, [25, 50])
check("回收前 csv：19:00(25) / 19:30(50)", csv_rows(wd),
      [("19:00", 25), ("19:30", 50)])
before = capacity(wd)
check("回收 1 场（19:30 的 50）", wd.reclaim_idle_auto_sessions(wd.sel_date), 1)
check("layout 只剩前面那场", layout_of(wd)[0], [25])
check("layout 还是 plan 切的（可重切）", layout_of(wd)[1], "plan")
check("csv 只剩 19:00(25)", csv_rows(wd), [("19:00", 25)])
check("回收后容量不变", capacity(wd), before)
check("plan_sig 保留 → 不会触发无谓重排", wd._auto_plan_dirty(wd.sel_date), False)

print("\n== 2) 整段都没任务 → 整段变回「未切分」（一场都不留）==")
wd = fresh()
set_auto_layout(wd, [25, 50])
before = capacity(wd)
check("回收 2 场", wd.reclaim_idle_auto_sessions(wd.sel_date), 2)
check("layout 空了", layout_of(wd)[0], [])
check("csv 这一天没有行了", csv_rows(wd), [])
check("看板给出「这一段还没切」的占位行",
      [bool(r.get("period_only")) for r in wd.rows if r.get("period_id") == "T3"],
      [True])
check("容量仍然算得出来（整段 5 count）", capacity(wd), before)
check("占位行不是 session（plan / 对账都不认它）", capacity(wd) > 0, True)

print("\n== 3) manual 时段里的 plan 窗口：收掉窗口，手敲的切分不动 ==")
wd = fresh()
wd.handle("period T3 25")                       # manual [25]
sp.add_extra(wd.sel_date, "19:30", 50, note="plan 切的（避开手订场）",
             source="plan")
sp.add_extra(wd.sel_date, "20:30", 25, note="plan 切的（避开手订场）",
             source="plan")
sp.add_extra(wd.sel_date, "21:00", 25, note="formatter 切的（任务 #5）",
             source="formatter")
wd.rewrite_data_csv_from_plan()
wd.rebuild_rows()
check("回收前 extras 三条", [e[0] for e in extras_of(wd)],
      ["19:30", "20:30", "21:00"])
check("回收 2 场（两条 plan 窗口）",
      wd.reclaim_idle_auto_sessions(wd.sel_date), 2)
check("plan 的窗口没了、formatter 的留着", extras_of(wd),
      [("21:00", 25, "formatter")])
check("manual layout 一个字没动", layout_of(wd)[:2], ([25], "manual"))
check("csv：19:00(25) + 21:00(25)", csv_rows(wd),
      [("19:00", 25), ("21:00", 25)])

print("\n== 4) 挂着任务的窗口是内容，不是碎片 ==")
wd = fresh()
wd.handle("period T3 25")
sp.add_extra(wd.sel_date, "19:30", 50, note="plan 切的（避开手订场）",
             source="plan")
bind(DAY_ISO, "19:30", 50, title="已经排进去的活")
wd.rewrite_data_csv_from_plan()
wd.rebuild_rows()
check("挂着任务 → 一场都不收", wd.reclaim_idle_auto_sessions(wd.sel_date), 0)
check("window 还在", [e[0] for e in extras_of(wd)], ["19:30"])

print("\n== 5) 幂等：再 refresh 一次一场都不收 ==")
wd = fresh()
set_auto_layout(wd, [25, 50])
check("第一次收 2 场", wd.reclaim_idle_auto_sessions(wd.sel_date), 2)
csv_once, layout_once = csv_rows(wd), layout_of(wd)[0]
check("第二次收 0 场", wd.reclaim_idle_auto_sessions(wd.sel_date), 0)
check("csv 没变", csv_rows(wd), csv_once)
check("layout 没变", layout_of(wd)[0], layout_once)

print("\n== 6) 已经开始的场次不回头改（只收尾巴上还没开始的）==")
wd = fresh(day=TODAY)
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(19, 45))
set_auto_layout(wd, [25, 25, 75], day=TODAY)
check("回收前 csv 三场", csv_rows(wd, day=TODAY),
      [("19:00", 25), ("19:30", 25), ("20:00", 75)])
check("只收 20:00 那场（75 → 3 场 count 里最后一场）",
      wd.reclaim_idle_auto_sessions(TODAY), 1)
check("csv 只剩已经开打的两场", csv_rows(wd, day=TODAY),
      [("19:00", 25), ("19:30", 25)])
check("layout 变成 [25, 25]", layout_of(wd, day=TODAY)[0], [25, 25])

print("\n== 7) 已订上、而且马上就要开始：也照收（先取消，下一轮 plan 再决定）==")
wd = fresh(day=TODAY)
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(18, 45))
set_auto_layout(wd, [25, 75], day=TODAY)
book_all(wd)
wd.offline = False                # 要真的走取消（FakeBridge 会立刻回 verified）
check("19:00/19:30 都订上了", [bool(r.get("id")) for r in wd.rows
                              if r.get("startLabel") in ("19:00", "19:30")],
      [True, True])
check("15 分钟后就要开始的两场也照收", wd.reclaim_idle_auto_sessions(TODAY), 2)
check("layout 清空（整段回到未切分）", layout_of(wd, day=TODAY)[0], [])
check("data.csv 这一天没有 session 行", csv_rows(wd, day=TODAY), [])
check("取消走的是那条唯一路径（有 cancel 调用）",
      [c for c in getattr(wd.bridge, "calls", []) if c[0] == "cancel"] != [], True)

print("\n== 8) 同样形状但**没订上**：照样收（纯契约碎片，没人被影响）==")
wd = fresh(day=TODAY)
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(18, 30))
set_auto_layout(wd, [25, 75], day=TODAY)
check("两场都没开始、都没订上 → 都收", wd.reclaim_idle_auto_sessions(TODAY), 2)
check("layout 空了", layout_of(wd, day=TODAY)[0], [])

print("\n== 8.5) 取消不了的时候不许撤契约（否则 daemon 会记 sin）==")
wd = fresh(day=TODAY)
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(12, 0))
set_auto_layout(wd, [25, 75], day=TODAY)
book_all(wd)
wd.offline = True                     # 离线 = 取消请求发不出去
check("已订上但取消不了 → 一场都不收", wd.reclaim_idle_auto_sessions(TODAY), 0)
check("契约原样保留（不留 booked-but-missing）",
      layout_of(wd, day=TODAY)[0], [25, 75])
check("csv 也没动", csv_rows(wd, day=TODAY), [("19:00", 25), ("19:30", 75)])

print("\n== 9) refresh 的排期流水线里就会回收（不用敲别的命令）==")
wd = fresh()
set_auto_layout(wd, [25, 50])
res = wd.reconcile_schedule_pipeline(day=wd.sel_date, quiet=True)
check("流水线报告回收 2 场", res.get("reclaimed"), 2)
check("csv 收干净了", csv_rows(wd), [])
check("没有产生预订动作（refresh 不新造外部动作）",
      wd._suppress_book, False)

print("\n== 9.5) 前面空着、后面被任务钉住：整段退回未切分，钉住的那场变独立窗口 ==")
# 现场：T4 22:15–23:45，layout [50,25]，23:15 挂着 flashcard（重复任务），22:15 空着。
# layout 是接龙，摘不掉 22:15 那场；解法是 layout 清空 + 23:15 挪成 extras（起点不变），
# 于是 22:15–23:15 变成 plan 眼里的 2 count「待分配」。
T4 = {"id": "T4", "start": "22:15", "end": "23:45", "type": "工作",
      "name": "其他项目", "calendar": "工作", "title": "其他项目"}
wd = fresh(extra_periods=[T4])
wd.clock_now = datetime.datetime.combine(DAY, datetime.time(16, 0))
set_auto_layout(wd, [50, 25], day=DAY, pid="T4")
keep_task = bind(DAY_ISO, "23:15", 25, title="flashcard review")
wd.rebuild_rows()
check("回收前 csv：22:15(50) + 23:15(25)", csv_rows(wd),
      [("22:15", 50), ("23:15", 25)])
check("只收 22:15 那场", wd.reclaim_idle_auto_sessions(DAY), 1)
check("T4 layout 清空（回到未切分）", layout_of(wd, "T4")[0], [])
check("source 原样保留（还是 plan，可重切）/ plan_sig 没丢",
      (layout_of(wd, "T4")[1], layout_of(wd, "T4")[2] is not None), ("plan", True))
check("23:15 那场变成独立窗口（起点/时长不变）",
      [(r.get("start"), int(r.get("duration") or 0))
       for r in sp.extras_for_date(sp.load_extras(), DAY)],
      [("23:15", 25)])
check("csv 只剩挂着任务的那场", csv_rows(wd), [("23:15", 25)])
check("任务绑定没动（还在 23:15）",
      (ts.TaskStore().get(keep_task["id"])["session"] or {}).get("start"), "23:15")
slots, _n = wd._plan_slots(DAY)
free = [(s.get("start"), int(s.get("counts") or 0)) for s in slots
        if str(s.get("period_id") or "") == "T4"]
check("plan 眼里 T4 有 2 count「待分配」、起点 22:15", free, [("22:15", 2)])
check("再回收一次是幂等的（0 场）", wd.reclaim_idle_auto_sessions(DAY), 0)

print("\n== 10) 过去的日子一律不动 ==")
yesterday = TODAY - datetime.timedelta(days=1)
wd = fresh(day=yesterday)
set_auto_layout(wd, [25, 50], day=yesterday)
check("昨天收 0 场", wd.reclaim_idle_auto_sessions(yesterday), 0)
check("昨天的 layout 一个字没动", layout_of(wd, day=yesterday)[0], [25, 50])

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for item in fails:
        print(" -", item)
    sys.exit(1)
print("全绿。")
