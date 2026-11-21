#!/usr/bin/env python3
"""stage3a：plan 容量扣「用户手订场」的回归（2026-11-21 用户定）。

规则：
* 手订场是硬洞：不塞任务、不取消、不挪；占用的时间从 period 扣掉；
* layout 只能表达「从时段起点接龙」，所以只有前缀窗口能算进 plan 容量；
  洞在时段开头 → 这段 0 容量；洞后面的窗口留给 stage3b；
* 手订判定：API 有 id、本地没有 planned/contract 来源、没挂任务、不是进行中。
"""
import datetime
import importlib.util
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-manual-holes-test-")

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
sp, sch, ts, ks, pj, pp = w.sp, w.sch, w.ts, w.ks, w.pj, w.pp

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
ISO = TOMORROW.isoformat()

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "cancel":
            return {"verified": True}
        return {"verified": True, "session": None}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def periods(only_t2=False):
    out = [{"id": "T2", "date": ISO, "start": "14:00", "end": "18:00",
            "type": "工作", "name": "下午", "calendar": "工作", "title": "下午"}]
    if not only_t2:
        out.insert(0, {"id": "T1", "date": ISO, "start": "09:00", "end": "12:30",
                       "type": "工作", "name": "上午", "calendar": "工作",
                       "title": "上午"})
    return out


def fresh(period_list=None, offline=True):
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    pj.save(pj.blank())
    sp.save_layouts(sp.empty_layouts())
    sp.save_extras(sp.empty_extras())
    with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
        fh.write("")
    sp.save_download({"version": 1, "generated_at": "x",
                      "start_date": TODAY.isoformat(), "days": 8,
                      "profiles": ["工作"], "events": [],
                      "periods": period_list if period_list is not None else periods()})
    bridge = FakeBridge()
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, bridge, no_clear=True, offline=offline, day_offset=1)
    wd._suppress_book = True
    wd.reload_periods()
    wd.reload_entries()
    wd.api_rows = []
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()
    return wd, bridge


def api_row(start, duration, sid="manual-1", planned=False, contract=""):
    hour, minute = (int(x) for x in start.split(":"))
    start_dt = datetime.datetime.combine(TOMORROW, datetime.time(hour, minute))
    end_dt = start_dt + datetime.timedelta(minutes=int(duration))
    return {
        "startMs": int(start_dt.timestamp() * 1000),
        "endMs": int(end_dt.timestamp() * 1000),
        "startLabel": start, "endLabel": end_dt.strftime("%H:%M"),
        "durationMin": int(duration), "id": sid, "title": "", "quiet": False,
        "matched": False, "phase": "未开始", "date": ISO,
        "planned": bool(planned), "contract": contract or "",
    }


def add_rows(wd, rows):
    wd.api_rows = list(rows)
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()


def slots_of(wd):
    slots, _notes = wd._plan_slots(TOMORROW, replan=True)
    return [(s["period_id"], s["start"], s["end"], int(s["counts"]),
             bool(s.get("windowed"))) for s in slots]


def cap_of(wd):
    slots, _notes = wd._plan_slots(TOMORROW, replan=True)
    return pp.capacity(slots)["counts"]


def task_by_title(wd, title):
    return next(t for t in wd.task_store.list()
                if (t.get("fields") or {}).get("title") == title)


def csv_rows():
    out = []
    with open(sch.DATA_CSV, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and parts[0] == ISO:
                out.append((parts[1], int(parts[2])))
    return out


print("== 1) 没有手订场：容量与旧口径一致 ==")
wd, _b = fresh()
check("T1 7 + T2 8", slots_of(wd),
      [("T1", "09:00", "12:30", 7, False), ("T2", "14:00", "18:00", 8, False)])
check("总容量 15", cap_of(wd), 15)

print("\n== 2) 手订 15:00 50min：前缀 + 洞后窗口都算容量 ==")
wd, _b = fresh()
add_rows(wd, [api_row("15:00", 50)])
check("T2 = 前缀 2 + 洞后 4", slots_of(wd),
      [("T1", "09:00", "12:30", 7, False),
       ("T2", "14:00", "15:00", 2, True),
       ("T2", "16:00", "18:00", 4, True)])
check("总容量 13", cap_of(wd), 13)
check("手订场被认出来", [r.get("startLabel") for r in wd.manual_session_rows(TOMORROW)],
      ["15:00"])

print("\n== 3) 手订挡住时段开头（14:00）：只剩洞后的窗口 ==")
wd, _b = fresh()
add_rows(wd, [api_row("14:00", 50)])
check("T2 从 15:00 起算 6 counts", slots_of(wd),
      [("T1", "09:00", "12:30", 7, False),
       ("T2", "15:00", "18:00", 6, True)])
check("总容量 13", cap_of(wd), 13)

print("\n== 4) 手订占掉大部分（14:00–17:00）：只剩 17:00 后的尾巴 ==")
wd, _b = fresh()
add_rows(wd, [api_row("14:00", 150)])
check("T2 只剩 17:00–18:00 的 2 counts", slots_of(wd),
      [("T1", "09:00", "12:30", 7, False),
       ("T2", "17:00", "18:00", 2, True)])
check("总容量 9", cap_of(wd), 9)

print("\n== 5) plan 只能在前缀里排：T2 的 layout 不越过 15:00 ==")
wd, _b = fresh(period_list=periods(only_t2=True))
add_rows(wd, [api_row("15:00", 50)])
wd.handle("task create title=前缀任务 duration=50")
task = task_by_title(wd, "前缀任务")
check("plan 打开", wd.handle("plan +1") is not None, True)
check("容量 = 6 counts（前缀 2 + 洞后 4）", wd.planning.cap()["counts"], 6)
wd.handle("task %d 2" % wd.task_no(task, day=wd.sel_date))
wd.handle("complete")
layout = sp.get_layout(sp.load_layouts(), TOMORROW, "T2") or []
check("layout 总时长不超过前缀 60 分钟", sum(int(x) for x in layout) <= 60, True)
bound = wd.task_store.get(task["id"])
sess = bound.get("session") or {}
start_min = int(sess.get("start", "99:99").split(":")[0]) * 60 + int(
    sess.get("start", "99:99").split(":")[1])
check("任务场次落在 14:00–15:00 内", 840 <= start_min < 900, True)
check("data.csv 里没有 15:00 之后的场次",
      [r for r in csv_rows() if int(r[0].split(":")[0]) * 60 + int(r[0].split(":")[1]) >= 900],
      [])

print("\n== 5b) 洞后的窗口用 extras 落地；再 plan 一次幂等 ==")
wd, _b = fresh(period_list=periods(only_t2=True))
add_rows(wd, [api_row("15:00", 50)])
wd.handle("task create title=整段任务 duration=150")
task = task_by_title(wd, "整段任务")
wd.handle("plan +1")
wd.handle("task %d 6" % wd.task_no(task, day=wd.sel_date))
wd.handle("complete")
extras1 = [(r["start"], int(r["duration"]))
           for r in sp.extras_for_date(sp.load_extras(), TOMORROW)]
check("洞后窗口补了 extras", bool(extras1), True)
check("extras 全部在 15:00 之后（不碰手订场）",
      [r for r in extras1 if r[0] < "16:00"], [])
check("extras 没有落在手订的 15:00",
      [r for r in extras1 if r[0] == "15:00"], [])
bound = [(t.get("session") or {}) for t in wd.task_store.list()
         if str((t.get("session") or {}).get("date") or "") == ISO]
check_true("有任务真的挂到洞后窗口", any(str(s.get("start") or "") >= "16:00"
                                          for s in bound))
check("没有任务挂在手订的 15:00",
      [s for s in bound if str(s.get("start") or "") == "15:00"], [])
layout1 = sp.get_layout(sp.load_layouts(), TOMORROW, "T2")
csv1 = csv_rows()
wd.handle("plan +1")
wd.handle("task %d 6" % wd.task_no(task, day=wd.sel_date))
wd.handle("complete")
check("再次 plan 后 extras 不变",
      [(r["start"], int(r["duration"]))
       for r in sp.extras_for_date(sp.load_extras(), TOMORROW)], extras1)
check("再次 plan 后 layout 不变",
      sp.get_layout(sp.load_layouts(), TOMORROW, "T2"), layout1)
check("再次 plan 后 data.csv 不变", csv_rows(), csv1)

print("\n== 6) plan 之后手订场仍被认作手订（没变成计划行）==")
check("手订 15:00 还在", [r.get("startLabel") for r in wd.manual_session_rows(TOMORROW)],
      ["15:00"])

print("\n== 7) 重切 layout 时不取消手订场 ==")
wd, bridge = fresh(offline=False)
add_rows(wd, [api_row("15:00", 50)])
period = next(p for p in wd.periods_for_day(TOMORROW) if p["id"] == "T2")
wd.clock_now = datetime.datetime.combine(TOMORROW, datetime.time(13, 0))
new_sessions = [{"start": "14:00", "end": "14:50", "duration": 50}]
cancelled, failed = wd._cancel_stale_period_sessions(TOMORROW, period, new_sessions)
check("没有取消任何场次", [c for c in bridge.calls if c[0] == "cancel"], [])
check("返回空", (cancelled, failed), ([], False))

print("\n== 8) 回归：没有手订时 plan 落盘仍是旧口径 ==")
wd, _b = fresh()
wd.handle("task create title=普通任务 duration=50")
normal = task_by_title(wd, "普通任务")
wd.handle("plan +1")
wd.handle("task %d 2" % wd.task_no(normal, day=wd.sel_date))
wd.handle("complete")
bound = wd.task_store.get(normal["id"])
check("落到第一段 09:00 的 50 分钟场",
      [bound["session"]["start"], bound["session"]["duration"]], ["09:00", 50])
check("T1 layout [50]", sp.get_layout(sp.load_layouts(), TOMORROW, "T1"), [50])

print("\n" + ("全部通过" if not fails else "失败 %d 条" % len(fails)))
if fails:
    for f in fails:
        print(" - " + f)
    sys.exit(1)
