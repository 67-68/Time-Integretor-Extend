#!/usr/bin/env python3
"""时段窗口裁剪（`period T1 09:45-12:30`）+ 工具切割不冻结 + extras 是硬洞。

2026-11-26 事故的三条回归：

1. **窗口可以裁，但必须被日历原时段包裹**：`period T1 09:45-12:30` 生效
   （容量/切场/data.csv 全按新窗口），越界或非 15 分钟整点直接拒绝；
   `period T1 window clear` 恢复原窗口。
2. **工具切的 layout 不许写成 manual**：`commit_layout()` 不传 source 时落
   `auto`，否则 end / watchdog 切完的段被当成「人手钉死」，容量缺口永远补不
   回来（T3 丢 100 分钟）。
3. **extras 也是硬洞**：不在 layout 里的真实场次（被挪过的已订场、formatter
   切的尾巴）必须从容量里扣掉，否则 plan 会排出 11:00(50) 压住 11:15(50)。
"""
import datetime
import importlib.util
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="period-window-test-")

for key, name in (("TI_LOG_DIR", "logs"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_PERIOD_WINDOWS", "period-windows.json"),
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


def check_raises(label, fn, needle):
    try:
        fn()
    except ValueError as e:
        text = str(e)
        ok = needle in text
        if not ok:
            fails.append("%s\n      error = %r\n      want  ≈ %r" % (label, text, needle))
        print("%s %s" % ("ok  " if ok else "FAIL", label))
    except Exception as e:                      # noqa: BLE001
        fails.append("%s 抛了别的异常：%r" % (label, e))
        print("FAIL %s（抛了 %r）" % (label, e))
    else:
        fails.append("%s 没有报错" % label)
        print("FAIL %s（没有报错）" % label)


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        return {"verified": True, "session": None}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def periods():
    return [{"id": "T1", "date": ISO, "start": "09:00", "end": "12:30",
             "type": "工作", "name": "上午", "calendar": "工作", "title": "上午"},
            {"id": "T2", "date": ISO, "start": "14:00", "end": "18:00",
             "type": "工作", "name": "下午", "calendar": "工作", "title": "下午"}]


def fresh():
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    pj.save(pj.blank())
    sp.save_layouts(sp.empty_layouts())
    sp.save_extras(sp.empty_extras())
    sp.save_period_windows(sp.empty_period_windows())
    with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
        fh.write("")
    sp.save_download({"version": 1, "generated_at": "x",
                      "start_date": TODAY.isoformat(), "days": 8,
                      "profiles": ["工作"], "events": [], "periods": periods()})
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True, day_offset=1)
    wd._suppress_book = True
    wd.reload_periods()
    wd.reload_entries()
    wd.api_rows = []
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()
    return wd


def win_of(wd, pid="T1"):
    p = next(x for x in wd.periods_for_day(TOMORROW) if x["id"] == pid)
    return (p.get("start"), p.get("end"), bool(p.get("windowed")))


def slots_of(wd):
    slots, _notes = wd._plan_slots(TOMORROW, replan=True)
    return [(s["period_id"], s["start"], s["end"], int(s["counts"]),
             bool(s.get("windowed"))) for s in slots]


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


print("== 1) 裁窗口：09:00–12:30 → 09:30–12:00 生效，容量跟着变 ==")
wd = fresh()
check("裁之前 7 counts", slots_of(wd)[0], ("T1", "09:00", "12:30", 7, False))
check_true("命令返回 True", wd.cmd_period_layout(["T1", "09:30-12:00"]))
check("窗口变了", win_of(wd), ("09:30", "12:00", True))
check("容量变 5 counts（150 分钟）", slots_of(wd)[0],
      ("T1", "09:30", "12:00", 5, False))
check("落盘也在", sp.load_period_windows()["days"][ISO]["T1"]["start"], "09:30")

print("\n== 2) 越界 / 非 15 分钟整点 / 太短 → 一律拒绝且不改落盘 ==")
wd = fresh()
check_raises("超出原时段（08:45）",
             lambda: wd.cmd_period_layout(["T1", "08:45-12:00"]), "包裹")
check_raises("结束超出原时段（13:00）",
             lambda: wd.cmd_period_layout(["T1", "09:00-13:00"]), "包裹")
check_raises("非 15 分钟整点（09:50）",
             lambda: wd.cmd_period_layout(["T1", "09:00-12:05"]), "15 分钟")
check_raises("窗口太短（20 分钟）",
             lambda: wd.cmd_period_layout(["T1", "09:00-09:15"]), "1 count")
check("拒绝之后窗口没变", win_of(wd), ("09:00", "12:30", False))

print("\n== 3) 裁窗口后原有 layout 按新起点搬过去 ==")
wd = fresh()
wd.commit_layout(TOMORROW, wd.periods_for_day(TOMORROW)[0], [50, 25],
                 source="manual", book=False, announce=False)
check("原排布 09:00 / 10:00", csv_rows(), [("09:00", 50), ("10:00", 25)])
wd.cmd_period_layout(["T1", "09:30-12:00"])
check("新排布跟着新窗口走", csv_rows(), [("09:30", 50), ("10:30", 25)])

print("\n== 4) 新窗口装不下原排布 → 清空该段（不留排不出来的 layout） ==")
wd = fresh()
wd.commit_layout(TOMORROW, wd.periods_for_day(TOMORROW)[0], [50, 25, 25],
                 source="manual", book=False, announce=False)
wd.cmd_period_layout(["T1", "11:00-12:00"])
check("T1 排布已清空", sp.get_layout(sp.load_layouts(), TOMORROW, "T1"), [])
check("T1 没有场次了", [r for r in csv_rows() if r[0] < "13:00"], [])

print("\n== 5) window clear 恢复原窗口 ==")
wd = fresh()
wd.cmd_period_layout(["T1", "09:30-12:00"])
check("裁过", win_of(wd), ("09:30", "12:00", True))
check_true("撤销返回 True", wd.cmd_period_layout(["T1", "window", "clear"]))
check("回到日历原窗口", win_of(wd), ("09:00", "12:30", False))
check("落盘也清了", sp.load_period_windows()["days"].get(ISO, {}).get("T1"), None)
check("容量回到 7", slots_of(wd)[0], ("T1", "09:00", "12:30", 7, False))

print("\n== 6) 工具切割不写成 manual（否则这一段再也补不回来） ==")
wd = fresh()
period = wd.periods_for_day(TOMORROW)[0]
wd.commit_layout(TOMORROW, period, [25], book=False, announce=False)   # 不传 source
rec = sp.get_layout_record(sp.load_layouts(), TOMORROW, "T1")
check("source 落成 auto", rec.get("source"), "auto")
check("plan 认它可重切", wd.available_periods(TOMORROW)["T1"]["auto"], True)
wd.commit_layout(TOMORROW, period, [50], source="manual", book=False, announce=False)
check("人手切的仍是 manual",
      sp.get_layout_record(sp.load_layouts(), TOMORROW, "T1").get("source"), "manual")
check("人手切的 plan 不碰", wd.available_periods(TOMORROW)["T1"]["auto"], False)

print("\n== 7) extras 是硬洞：10:00(50) 在 T1 里 → 切两段窗口 ==")
wd = fresh()
sp.add_extra(TOMORROW, "10:00", 50, note="测试")
wd.reload_entries()
wd.rebuild_rows()
check("T1 被拆成 2 + 3 counts", slots_of(wd)[0],
      ("T1", "09:00", "10:00", 2, True))
check("第二段从 11:00 起", slots_of(wd)[1], ("T1", "11:00", "12:30", 3, True))
check("extras 认成洞", wd.extra_session_holes(TOMORROW), [(600, 660)])
check("plan 不会压住 extras",
      [s for s in slots_of(wd) if s[1] == "10:00"], [])

print("\n== 8) 排布落盘后，被覆盖的 extras 自动收掉（不留重复行） ==")
wd = fresh()
sp.add_extra(TOMORROW, "09:30", 25, note="formatter 切的")
wd.reload_entries()
wd.rewrite_data_csv_from_plan()
check("先有一行 extras", csv_rows(), [("09:30", 25)])
wd.commit_layout(TOMORROW, wd.periods_for_day(TOMORROW)[0], [25, 25],
                 book=False, announce=False)
check("extras 被 layout 收编（没有两行 09:30）",
      [r for r in csv_rows() if r[0] == "09:30"], [("09:30", 25)])
check("extras 表已删掉", sp.extras_for_date(sp.load_extras(), TOMORROW), [])

print("\n== 9) 绑着任务的场次仍然不许重切（auto 不等于可以乱动） ==")
wd = fresh()
period = wd.periods_for_day(TOMORROW)[0]
wd.commit_layout(TOMORROW, period, [25, 25], book=False, announce=False)
store = ts.TaskStore()
task = store.create({"title": "手挂的活", "duration": 25,
                     "count": 1, "count_total": 1})
store.assign(task["id"], {"date": ISO, "start": "09:00", "end": "09:25",
                          "duration": 25})
# 真实行（data.csv / API 里的 planned 行）才是「这一段有没有被占」的证人也
# 是 plan 唯一能看见的东西 —— 布局单测必须先把行造出来。
def planned_row(start, duration):
    h, m = (int(x) for x in start.split(":"))
    start_dt = datetime.datetime.combine(TOMORROW, datetime.time(h, m))
    end_dt = start_dt + datetime.timedelta(minutes=duration)
    return {"startMs": int(start_dt.timestamp() * 1000),
            "endMs": int(end_dt.timestamp() * 1000),
            "startLabel": start, "endLabel": end_dt.strftime("%H:%M"),
            "durationMin": duration, "id": None, "title": "", "quiet": False,
            "matched": False, "phase": "未开始", "date": ISO,
            "planned": True, "contract": "layout"}

wd.api_rows = [planned_row("09:00", 25), planned_row("09:30", 25)]
wd.rows_source = "bridge"
wd.rows_at = time.time()
wd.rebuild_rows()
check("绑了任务的那一场不被吞掉（09:00 仍是它的，不重切成 7 count）",
      [s for s in slots_of(wd) if s[1] == "09:00"], [])
check("其余容量照样能用（不是整段冻结）",
      [(s[1], s[3]) for s in slots_of(wd) if s[0] == "T1"],
      [("09:30", 1), ("10:00", 5)])


print()
if fails:
    print("有 %d 个失败：" % len(fails))
    for item in fails:
        print("  - " + item)
    sys.exit(1)
print("全部通过")
