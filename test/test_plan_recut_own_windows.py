#!/usr/bin/env python3
"""plan 自己切的窗口，在**手动 layout 的时段里**也必须能重切（2026-11-26 事故）。

用户原话：「他为什么被拆成三块了？明明 2 和 3 也可以连起来」「为什么不把
19:00–19:25 的塞到后面，让 SAT 和 English Review 完整排期？」

现场形状：T3 19:00–21:30 的 layout 是 manual ``[25]``（19:00 一场，watchdog 那轮
写的），21:00 一条 formatter 的 extras（硬洞）。上一轮 plan 为了避开这两条洞，
把中间 19:30–21:00 切成 ``19:30(50) + 20:30(25)`` 两条窗口 extras 落盘。
下一轮 plan 再进来时，这两条 plan 自己切的 extras 被当成**永久固定槽**：

* 3 count 的项目（75 分钟）没有一场装得下 → 被「填缝」拆成 25+25+25 撒到
  20:30、21:00、22:15 三个不同场次，还把 22:15 的 50 分钟场次填成 1 count；
* 用户看到的就是「一个任务被拆成三块」「整场没被排满」。

口径（这次定死）：extras 能不能重算看**谁切的**（``source``），不看它落在哪种
layout 里。plan/auto 切的窗口是上一轮的临时切分 → 下一次 plan 要能把它还原成
一整块；formatter / manual 的仍是硬约束；已经挂了**不会被撤掉**的任务的 extras
也仍然钉死（那是内容约束，不是切分）。
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-recut-own-windows-test-")

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


def slots_of(wd, replan=True):
    slots, notes = wd._plan_slots(wd.sel_date, replan=replan)
    return [(s.get("start"), int(s.get("duration") or 0)) for s in slots], slots, notes


def csv_rows(wd):
    rows = []
    with open(sch.DATA_CSV, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and parts[0] == DAY_ISO:
                rows.append((parts[1], int(parts[2])))
    return rows


def seed_manual_holes(wd):
    """复刻现场：manual [25] + 上一轮 plan 切的 19:30(50)/20:30(25) + 21:00 formatter。"""
    wd.handle("period T3 25")
    sp.add_extra(wd.sel_date, "19:30", 50, note="plan 切的（避开手订场）",
                 source="plan")
    sp.add_extra(wd.sel_date, "20:30", 25, note="plan 切的（避开手订场）",
                 source="plan")
    sp.add_extra(wd.sel_date, "21:00", 25, note="formatter 切的（任务 #5）",
                 source="formatter")
    wd.rewrite_data_csv_from_plan()
    book_all(wd)


print("== 1) manual 时段里，上一轮 plan 切的 50+25 必须还原成一块 3 count ==")
wd = fresh()
seed_manual_holes(wd)
got, slots, notes = slots_of(wd)
check("三场：19:00(25) / 19:30(75) / 21:00(25)", got,
      [("19:00", 25), ("19:30", 75), ("21:00", 25)])
check("总容量 = 5 count", sum(int(s.get("counts") or 0) for s in slots), 5)
by_start = {s["start"]: s for s in slots}
check("19:30 那截是窗口", bool((by_start.get("19:30") or {}).get("window")), True)
check("窗口起点 19:30、终点 21:00",
      ((by_start.get("19:30") or {}).get("period") or {}).get("start"), "19:30")
check("窗口终点是 21:00",
      ((by_start.get("19:30") or {}).get("period") or {}).get("end"), "21:00")
check("20:30 那条 plan 的 extras 不再单独占一格",
      "20:30" in by_start, False)
check("21:00 的 formatter 场次原样保留（硬洞）",
      (by_start.get("21:00") or {}).get("id"), "sess-21:00")
check("19:00 的 manual 场次原样保留",
      (by_start.get("19:00") or {}).get("id"), "sess-19:00")

print("\n== 2) formatter 的 extras 依旧钉死：同一段里它还是洞 ==")
wd = fresh()
wd.handle("period T3 25")
sp.add_extra(wd.sel_date, "19:30", 50, note="formatter 切的（任务 #7）",
             source="formatter")
sp.add_extra(wd.sel_date, "21:00", 25, note="formatter 切的（任务 #5）",
             source="formatter")
wd.rewrite_data_csv_from_plan()
book_all(wd)
got, slots, notes = slots_of(wd)
check("19:00(25) / 19:30(50 洞) / 20:30(25 窗口) / 21:00(25 洞)", got,
      [("19:00", 25), ("19:30", 50), ("20:30", 25), ("21:00", 25)])
check("20:30 那截是窗口（洞把它切剩 1 count）",
      ({s["start"]: s for s in slots}.get("20:30") or {}).get("window"), True)
check("19:30 那条 formatter 的还挂着 id（没被重切）",
      ({s["start"]: s for s in slots}.get("19:30") or {}).get("id"), "sess-19:30")

print("\n== 3) plan 的窗口上挂着「本轮不会撤掉」的任务 → 仍然是内容约束 ==")
wd = fresh()
seed_manual_holes(wd)
plain = ts.TaskStore().create({"title": "用户自己排的活", "duration": 25})
ts.TaskStore().assign(plain["id"], {
    "index": 0, "date": DAY_ISO, "start": "20:30", "end": "20:55",
    "duration": 25, "focusmate_session_id": "sess-20:30"})
wd.rebuild_rows()
got, slots, notes = slots_of(wd)
check("20:30 那截因为挂着活，继续当洞：窗口只剩 19:30(50)", got,
      [("19:00", 25), ("19:30", 50), ("21:00", 25)])
check("那个挂着活的场次本身不算空场（它是内容，不是容量）",
      sum(int(s.get("counts") or 0) for s in slots), 4)

def simple_pro(pro_id=1, title="整场复习"):
    """建一个简单项目：plan 会为它合成 ∞ 的项目级虚拟任务。"""
    store = pj.blank()
    store["next_pro_id"] = pro_id + 1
    store["pros"] = [{"id": pro_id, "title": title, "stage": "running",
                      "status": "active", "simple": True}]
    pj.save(store)


print("\n== 4) plan + complete：真的把窗口重切成 19:30(75) 一条 ==")
wd = fresh()
seed_manual_holes(wd)
simple_pro()
wd.handle("plan +1")
if wd.planning is None:
    print("   messages:", [m[2] for m in wd.messages])
    raise SystemExit("plan 没打开")
check("plan 看到 5 count", wd.planning.cap()["counts"], 5)
check("空场列表里只有一整块窗口",
      [s.get("start") for s in wd.planning.slots],
      ["19:00", "19:30", "21:00"])
wd.handle("pro 1 3")             # 3 count = 75 分钟，只有窗口装得下
wd.handle("complete")
check("data.csv 三场：19:00(25) / 19:30(75) / 21:00(25)",
      csv_rows(wd), [("19:00", 25), ("19:30", 75), ("21:00", 25)])
extras = sp.extras_for_date(sp.load_extras(), wd.sel_date)
check("plan 的 extras 只剩一条 19:30(75)",
      [(r.get("start"), int(r.get("duration") or 0), r.get("source"))
       for r in extras],
      [("19:30", 75, "plan"), ("21:00", 25, "formatter")])
check("manual layout 没被重切（还是 [25]）",
      ((sp.get_layout_record(sp.load_layouts(), wd.sel_date, "T3") or {})
       .get("durations")), [25])

print("\n== 5) 再 plan 一次：幂等（不重复、不再切碎）==")
wd.handle("plan +1")
check("第二轮还是 5 count", wd.planning.cap()["counts"], 5)
check("第二轮空场还是那三块",
      [s.get("start") for s in wd.planning.slots],
      ["19:00", "19:30", "21:00"])
wd.handle("pro 1 3")
wd.handle("complete")
check("data.csv 仍然只有三行", csv_rows(wd),
      [("19:00", 25), ("19:30", 75), ("21:00", 25)])
check("extras 仍然只有两条",
      [(r.get("start"), int(r.get("duration") or 0))
       for r in sp.extras_for_date(sp.load_extras(), wd.sel_date)],
      [("19:30", 75), ("21:00", 25)])

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for item in fails:
        print(" -", item)
    sys.exit(1)
print("全绿。")
