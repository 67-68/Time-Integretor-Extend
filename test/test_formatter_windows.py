#!/usr/bin/env python3
"""formatter end 的 period-first 回归（2026-11-21 用户确认的口径）。

规则：
* 自动排序只在「period ∩ 非用户手订 ∩ 非进行中/静音」上做；
* 用户手订场是硬洞：不塞任务、不取消，占用的时间从 period 扣掉；
* 跨 period 的手订场按交集扣；扣完不足 25 分钟的段直接丢弃；
* 当天没有计划 session 时，end 从可用窗口的尾巴切；有 session 仍走旧剪断路径。

全部离线（不碰 Focusmate / OmniFocus / 真 data.csv）。
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="formatter-windows-test-")

for key, name in (("TI_LOG_DIR", "logs"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_SESSION_MUTES", "mutes.json"),
                  ("TI_SETTINGS_FILE", "settings.json"),
                  ("TI_TASKS_FILE", "tasks.json"),
                  ("TI_TASK_ARCHIVE", "archive.json"),
                  ("TI_PROJECTS_FILE", "projects.json"),
                  ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
                  ("TI_NOSTART_FILE", "nostart.json"),
                  ("TI_POSTSESSION_FILE", "postsession.json"),
                  ("TI_PLAN_PINS", "plan-pins.json"),
                  ("TI_PLAN_WATCHDOG", "plan-watchdog.json"),
                  ("TI_OMNIFOCUS_OUTBOX", "omnifocus-outbox.json"),
                  ("TI_FAILED_ACTIONS", "failed-actions.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

w.run_keep_session = lambda *a, **kw: (0, "", "")
sp, sch, ts, ks, pj, pp, th = w.sp, w.sch, w.ts, w.ks, w.pj, w.pp, w.th

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
    def call(self, cmd, timeout=240, **kw):
        return {"verified": True, "session": None}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def periods(extra=()):
    base = [
        {"id": "T1", "date": ISO, "start": "09:00", "end": "12:30",
         "type": "工作", "name": "上午", "calendar": "工作", "title": "上午"},
        {"id": "T2", "date": ISO, "start": "14:00", "end": "18:00",
         "type": "工作", "name": "下午", "calendar": "工作", "title": "下午"},
    ]
    base.extend(extra)
    return base


def reset_store():
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())


def fresh(period_list=None, day_offset=1):
    """干净的一天：明天（默认）或今天；离线，不订场。"""
    reset_store()
    sp.save_layouts(sp.empty_layouts())
    sp.save_extras(sp.empty_extras())
    try:
        with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError:
        pass
    sp.save_download({"version": 1, "generated_at": "x",
                      "start_date": TODAY.isoformat(), "days": 8,
                      "profiles": ["工作"], "events": [],
                      "periods": period_list if period_list is not None else periods()})
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True,
                  day_offset=day_offset)
    wd._suppress_book = True                 # 测试只断言排期，不触发 daemon 下单
    wd.reload_periods()
    wd.reload_entries()
    wd.api_rows = []
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()
    return wd


def api_row(day, start, duration, sid="manual-1"):
    hour, minute = (int(x) for x in start.split(":"))
    start_dt = datetime.datetime.combine(day, datetime.time(hour, minute))
    end_dt = start_dt + datetime.timedelta(minutes=int(duration))
    return {
        "startMs": int(start_dt.timestamp() * 1000),
        "endMs": int(end_dt.timestamp() * 1000),
        "startLabel": start, "endLabel": end_dt.strftime("%H:%M"),
        "durationMin": int(duration), "id": sid, "title": "",
        "quiet": False, "matched": False, "phase": "未开始",
        "date": day.isoformat(),
    }


def add_rows(wd, rows):
    wd.api_rows = list(rows)
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()


def windows_of(wd, day=None):
    return [(x["period_id"], x["start"], x["end"], x["full"], x["blocks"])
            for x in wd.available_windows(day or TOMORROW)]


def task_by_title(wd, title):
    return next(t for t in wd.task_store.list()
                if (t.get("fields") or {}).get("title") == title)


def csv_rows(day):
    out = []
    try:
        with open(sch.DATA_CSV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 3 and parts[0] == day.isoformat():
                    out.append((parts[1], int(parts[2])))
    except OSError:
        pass
    return out


print("== 1) 没有手订场：period 原样成窗口 ==")
wd = fresh()
check("T1 / T2 都整段可用", windows_of(wd),
      [("T1", "09:00", "12:30", True, 7), ("T2", "14:00", "18:00", True, 8)])

print("\n== 2) 手订场在 period 中间：时间被挖掉，含 5 分钟休息 ==")
wd = fresh()
add_rows(wd, [api_row(TOMORROW, "15:00", 50)])
check("T2 切成 14:00–15:00 + 16:00–18:00",
      windows_of(wd),
      [("T1", "09:00", "12:30", True, 7),
       ("T2", "14:00", "15:00", False, 2),
       ("T2", "16:00", "18:00", False, 4)])

print("\n== 3) 手订场跨 period：按交集扣；不足 25 分钟的段丢弃 ==")
wd = fresh(periods([{"id": "T3", "date": ISO, "start": "19:00", "end": "19:20",
                     "type": "工作", "name": "短尾巴", "calendar": "工作",
                     "title": "短尾巴"}]))
add_rows(wd, [api_row(TOMORROW, "12:00", 150)])
check("T1 收在 12:00、T2 从 15:00 起；T3（20 分钟）整段丢弃",
      windows_of(wd),
      [("T1", "09:00", "12:00", False, 6),
       ("T2", "15:00", "18:00", False, 6)])

print("\n== 4) 「手订」判定：有本地来源 / 挂了任务的场次不算手订 ==")
wd = fresh()
sp.set_layout(TOMORROW, "T2", [50], source="manual")
wd.reload_periods()
add_rows(wd, [api_row(TOMORROW, "14:00", 50)])
check("layout 里有来源 → 不算手订", wd.manual_session_rows(TOMORROW), [])
check("窗口保持整段", windows_of(wd)[1], ("T2", "14:00", "18:00", True, 8))

wd = fresh()
add_rows(wd, [api_row(TOMORROW, "15:00", 50, sid="manual-bound")])
wd.handle("task create title=已挂任务 duration=25")
bound = task_by_title(wd, "已挂任务")
wd.task_store.assign(bound["id"], {"date": ISO, "start": "15:00",
                                   "end": "15:50", "duration": 50})
check("挂了任务 → 不算手订", wd.manual_session_rows(TOMORROW), [])
check("窗口里仍然有 15:00 那一段", ("T2", "14:00", "18:00", True, 8) in windows_of(wd),
      True)

print("\n== 5) end：当天 0 session，从可用窗口尾巴切 ==")
wd = fresh()
wd.handle("task create title=闪卡 duration=25 -f end")
task = task_by_title(wd, "闪卡")
res = (task.get("formatter") or {}).get("reservation") or {}
check("formatter 落上了", (task.get("formatter") or {}).get("name"), "end")
check("切在 T2 尾巴 17:30", [res.get("period_id"), res.get("start"), res.get("end")],
      ["T2", "17:30", "18:00"])
check("前缀 75+75+25 接到 17:30", res.get("cut_prefix"), [75, 75, 25])
check("layout 写成 [75,75,25,25]",
      sp.get_layout(sp.load_layouts(), TOMORROW, "T2"), [75, 75, 25, 25])
check("任务挂在 17:30 的 25 分钟场",
      [task["session"]["start"], task["session"]["duration"]], ["17:30", 25])
check("formatter 任务自动独占", task["fields"].get("exclusive"), True)
check("data.csv 最后一行是 17:30 25", csv_rows(TOMORROW)[-1], ("17:30", 25))
check("data.csv 是 4 场", len(csv_rows(TOMORROW)), 4)

print("\n== 6) end：手订场在下午 → 只补一次性场，不动手订场 ==")
wd = fresh()
add_rows(wd, [api_row(TOMORROW, "15:00", 50)])
wd.handle("task create title=闪卡2 duration=25 -f end")
task = task_by_title(wd, "闪卡2")
res = (task.get("formatter") or {}).get("reservation") or {}
check("仍然切在 16:00–18:00 的尾巴 17:30",
      [res.get("period_id"), res.get("start")], ["T2", "17:30"])
check("窗口被切过 → 不写 layout（cut_period_id 为空）", res.get("cut_period_id"), None)
check("T2 没有 layout 记录", sp.get_layout(sp.load_layouts(), TOMORROW, "T2"), None)
check("补了一场 17:30 25 的一次性 session",
      [(r["start"], int(r["duration"]))
       for r in sp.extras_for_date(sp.load_extras(), TOMORROW)],
      [("17:30", 25)])
check("没有在 15:00 附近补场",
      [r for r in sp.extras_for_date(sp.load_extras(), TOMORROW)
       if r["start"] == "15:00"], [])
check("任务挂在 17:30", task["session"]["start"], "17:30")

print("\n== 7) 今天所有时段都过去：明确报「没有空位」，不写任何东西 ==")
wd = fresh(period_list=[
    {"id": "T1", "date": TODAY.isoformat(), "start": "09:00", "end": "12:30",
     "type": "工作", "name": "上午", "calendar": "工作", "title": "上午"},
    {"id": "T2", "date": TODAY.isoformat(), "start": "14:00", "end": "18:00",
     "type": "工作", "name": "下午", "calendar": "工作", "title": "下午"},
], day_offset=0)
wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(21, 0))
wd.messages = []
wd.handle("task create title=晚上任务 duration=25 -f end")
late = task_by_title(wd, "晚上任务")
check("没有落 formatter", late.get("formatter"), None)
check_true("明确说没有空位",
           any("没有能放下" in m[2] for m in wd.messages))
check("没有写 layout", sp.get_layout(sp.load_layouts(), TODAY, "T2"), None)
check("没有写一次性 session",
      sp.extras_for_date(sp.load_extras(), TODAY), [])

print("\n== 8) 回归：当天已有计划 session 时仍走旧剪断路径 ==")
wd = fresh()
sp.set_layout(TOMORROW, "T2", [50], source="manual")
wd.reload_periods()
add_rows(wd, [api_row(TOMORROW, "14:00", 50, sid="old-1400")])
wd.handle("task create title=剪断回归 duration=25 -f end")
cut = task_by_title(wd, "剪断回归")
check("切出来的那场仍是 25 分钟", cut["session"]["duration"], 25)
check("T2 的 layout 末尾变成 25（被剪断）",
      sp.get_layout(sp.load_layouts(), TOMORROW, "T2")[-1], 25)
check("旧路径也把任务挂上了", bool(cut["session"].get("start")), True)

print("\n" + ("全部通过" if not fails else "失败 %d 条" % len(fails)))
if fails:
    for f in fails:
        print(" - " + f)
    sys.exit(1)
