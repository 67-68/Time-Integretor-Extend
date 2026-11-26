#!/usr/bin/env python3
"""手动 layout 的「洞」只吃掉自己那段占用（2026-11-26 用户报的事故）。

现场：T3 19:00–21:30 的 layout 是 formatter 切的 ``[25]``（19:00 一场），外加一条
21:00 的 extras（formatter 为某条任务切的）。``tail_gap`` 当时拿**所有**行去验
「序列对不对得上」，19:00(25) + 21:00(25) 一错就整段放弃 —— 用户看到的是
「19:25–21:00 那里去了」，一个 2.5 小时的时段只产出两场 25 分钟。

口径（这次定死）：洞（extras / 手订场）只占自己的 ``session_span``，洞与洞之间、
以及洞和时段尾巴之间的空档都得还给排期；layout 链上的场次原样保留，绝不挪动。

用假 OmniFocus + 明天的时段，绝不碰真数据。
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-manual-hole-test-")

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
DAY = TODAY + datetime.timedelta(days=1)          # 明天：不会被「已经过去」筛掉
DAY_ISO = DAY.isoformat()

sp.save_download({
    "version": 1, "generated_at": "x", "start_date": DAY_ISO, "days": 8,
    "profiles": ["工作"], "events": [],
    "periods": [
        {"id": "T3", "date": DAY_ISO, "start": "19:00", "end": "21:30",
         "type": "空闲", "name": "其他项目", "calendar": "空闲",
         "title": "其他项目"},
    ]})

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
        print("%s %s\n      got  = %r\n      want = %r"
              % ("FAIL", label, got, want))
        return
    print("ok   %s" % label)


def check_true(label, got):
    check(label, bool(got), True)


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
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


def fresh():
    # 每个场景从干净账本开始：extras / layout 都不能从上一条用例漏过来。
    sp.save_extras(sp.empty_extras())
    sp.save_layouts(sp.empty_layouts())
    sp.save_download({
        "version": 1, "generated_at": "x", "start_date": DAY_ISO, "days": 8,
        "profiles": ["工作"], "events": [],
        "periods": [
            {"id": "T3", "date": DAY_ISO, "start": "19:00", "end": "21:30",
             "type": "空闲", "name": "其他项目", "calendar": "空闲",
             "title": "其他项目"},
        ]})
    with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
        fh.write("# 空\n")
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus()
    wd.handle("day 1")
    check("看板切到明天", wd.sel_date, DAY)
    return wd


def book_all(wd):
    """把 data.csv 里的场次都标成「已经订上」（真数据里它们是 booked）。"""
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


def slots_of(wd):
    slots, notes = wd._plan_slots(wd.sel_date)
    return [(s.get("start"), int(s.get("duration") or 0)) for s in slots], slots, notes


print("== 1) formatter 的 [25] + 一条 21:00 extras：19:30 的 75 分钟要还回来 ==")
wd = fresh()
wd.handle("period T3 25")
sp.add_extra(wd.sel_date, "21:00", 25, note="formatter 切的（任务 #5）")
wd.rewrite_data_csv_from_plan()
book_all(wd)
got, slots, notes = slots_of(wd)
check("三场：19:00(25) / 19:30(75) / 21:00(25)", got,
      [("19:00", 25), ("19:30", 75), ("21:00", 25)])
check("总容量 = 5 count（整段 150 分钟）",
      sum(int(s.get("counts") or 0) for s in slots), 5)
check("总分钟 = 125（floor(150/30)×25）",
      sum(int(s.get("duration") or 0) for s in slots), 125)
by_start = {s["start"]: s for s in slots}
check("19:30 那截是窗口（layout 表达不了起点）",
      bool((by_start.get("19:30") or {}).get("window")), True)
check("19:30 那截要走 extras 落盘（不是 append）",
      bool((by_start.get("19:30") or {}).get("append")), False)
check("19:00 是 layout 链上的真实场次",
      (by_start.get("19:00") or {}).get("window"), None)
check("21:00 是 extras 那条真实场次",
      (by_start.get("21:00") or {}).get("id"), "sess-21:00")
check("窗口的 period 自己从 19:30 开始",
      ((by_start.get("19:30") or {}).get("period") or {}).get("start"), "19:30")

print("\n== 2) 没有 extras 时：仍旧走「尾巴追加」，一个已订场次都不挪 ==")
wd = fresh()
wd.handle("period T3 25")
book_all(wd)
got, slots, notes = slots_of(wd)
# 尾巴 = 21:30 - 19:30 = 120 分钟 → 4 count = 100 分钟（加上 19:00 的 25 = 125）
check("两场：19:00(25) / 19:30(100)", got, [("19:00", 25), ("19:30", 100)])
tail = slots[-1]
check("尾巴是 append 语义", bool(tail.get("append")), True)
check("追加前缀 = 原来的 [25]", tail.get("append_prefix"), [25])
check("不是窗口", bool(tail.get("window")), False)

print("\n== 3) 夹在中间的两条 extras：剩下的空档也照排 ==")
wd = fresh()
wd.handle("period T3 25")
sp.add_extra(wd.sel_date, "19:30", 25, note="formatter 切的（任务 #1）")
sp.add_extra(wd.sel_date, "20:30", 25, note="formatter 切的（任务 #2）")
wd.rewrite_data_csv_from_plan()
book_all(wd)
got, slots, notes = slots_of(wd)
check("19:00(25) / 19:30(25) / 20:00(25 窗口) / 20:30(25) / 21:00(25 窗口)", got,
      [("19:00", 25), ("19:30", 25), ("20:00", 25), ("20:30", 25), ("21:00", 25)])
check("两条窗口、两条 extras、一条 layout",
      [bool(s.get("window")) for s in slots],
      [False, False, True, False, True])
check("总容量还是 5 count",
      sum(int(s.get("counts") or 0) for s in slots), 5)

print("\n== 4) 洞吃掉太多：剩下的不足 1 count 就不许硬塞 ==")
wd = fresh()
wd.handle("period T3 25")
# 19:30 起一条 75 分钟的 extras，span 90 → 19:30–21:00；尾巴只剩 21:00–21:30。
sp.add_extra(wd.sel_date, "19:30", 75, note="一坨大的")
wd.rewrite_data_csv_from_plan()
book_all(wd)
got, slots, notes = slots_of(wd)
check("只有 19:00(25) / 19:30(75) / 21:00(25)，瓶颈处不塞假的 0", got,
      [("19:00", 25), ("19:30", 75), ("21:00", 25)])
check("19:00/19:30 是真实行，最后一截是窗口",
      [bool(s.get("window")) for s in slots], [False, False, True])

print("\n== 5) plan + complete 真的把窗口落成 extras（不是只算容量）==")
wd = fresh()
wd.handle("period T3 25")
sp.add_extra(wd.sel_date, "21:00", 25, note="formatter 切的（任务 #5）")
wd.rewrite_data_csv_from_plan()
book_all(wd)
wd.handle("task create title=窗口活 duration=75")
wd.handle("plan +1")          # plan 不带参数永远是今天，这里要的是看板那天
planner = wd.planning
if planner is None:
    print("   messages:", [m[2] for m in wd.messages])
    raise SystemExit("plan 没打开")
check("plan 看到 5 count", planner.cap()["counts"], 5)
wd.handle("task 1 3")                      # 3 count = 75 分钟，只有窗口装得下
check("固定项吃 3 count", planner.allocated(), 3)
wd.handle("complete")
extras = sp.extras_for_date(sp.load_extras(), wd.sel_date)
check("extras 多了一条 19:30(75)",
      any(str(r.get("start")) == "19:30" and int(r.get("duration") or 0) == 75
          for r in extras), True)
rows = []
with open(sch.DATA_CSV, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[0] == DAY_ISO:
            rows.append((parts[1], int(parts[2])))
check("data.csv 三场都在", rows, [("19:00", 25), ("19:30", 75), ("21:00", 25)])
check("原 layout 没被重切（仍是 manual [25]）",
      ((sp.get_layout_record(sp.load_layouts(), wd.sel_date, "T3") or {})
       .get("durations")), [25])

print("\n== 6) plan 自己切的窗口 extra：clear 之后要能重算，不能变成永久硬洞 ==")
wd = fresh()
# 自动时段（没有 layout）+ 开头一条 formatter 切的 extras（19:00 是硬洞）
sp.add_extra(wd.sel_date, "19:00", 25, note="formatter 切的（任务 #9）")
wd.rewrite_data_csv_from_plan()
book_all(wd)
wd.handle("task create title=窗口长活 duration=75")
wd.handle("plan +1")
check("首轮容量 4 count（19:30–21:30 的窗口）",
      wd.planning.cap()["counts"], 4)
wd.handle("task 1 3")
wd.handle("complete")
first = sp.extras_for_date(sp.load_extras(), wd.sel_date)
check("首轮把窗口落成 plan 的 extras",
      [(r.get("start"), int(r.get("duration") or 0), r.get("source"))
       for r in first], [("19:00", 25, None), ("19:30", 75, "plan")])

wd.handle("plan clear")
left = sp.extras_for_date(sp.load_extras(), wd.sel_date)
check("plan clear 不动 extras 本身（那是真实的场次）",
      [(r.get("start"), int(r.get("duration") or 0)) for r in left],
      [("19:00", 25), ("19:30", 75)])

wd.handle("plan +1")
check("clear 之后再 plan，容量没有缩水（还是 4 count）",
      wd.planning.cap()["counts"], 4)
check("19:30 那截还在空场列表里",
      [s.get("start") for s in wd.planning.slots],
      ["19:30"])

print("\n== 7) 但 formatter 的 extras 依旧钉死：它不会因为 clear 变成可重算 ==")
wd = fresh()
sp.add_extra(wd.sel_date, "20:00", 50, note="formatter 切的（任务 #2）")
wd.rewrite_data_csv_from_plan()
book_all(wd)
wd.handle("task create title=占位活 duration=50")
wd.handle("plan +1")
check("formatter 的 50 分钟当作硬洞扣掉（150-60 → 3 count）",
      wd.planning.cap()["counts"], 3)
check("洞前洞后各一截窗口，洞本身没被压",
      [s.get("start") for s in wd.planning.slots], ["19:00", "21:00"])

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for item in fails:
        print(" -", item)
    sys.exit(1)
print("全绿。")
