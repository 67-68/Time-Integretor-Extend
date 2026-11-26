#!/usr/bin/env python3
"""契约表占位行：时段里**没被任何 session 占用**的空档都要画出来。

2026-11-26 用户报的现场（T4）：

    T4 22:15–23:45   [23:15–23:40 25min flashcard review]

22:15 那一截被回收成「未切分容量」之后，表里**整段消失**——只剩 23:15 那场，
看起来像那 50 分钟容量被吃掉了（用户：「为什么现在这个时段压根不显示了？？」）。

根因：`_period_shell_rows` 只会在「整段没切」时画整段占位行，切了一半时只用
`tail_gap` 算**尾巴**；而 `tail_gap` 要求已有场次是从时段起点接龙的规范序列，
「前面空、后面被任务钉住」（窗口在场次层是 extras）必然对不上 → 一行都不画。
"""
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


TMP = tempfile.mkdtemp(prefix="period-shell-test-")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")
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

w.run_keep_session = lambda *a, **kw: (0, "", "")

sp, sch, ks, pp = w.sp, w.sch, w.ks, w.pp
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


class FakeBridge:
    sessions = []
    calls = []


def period(pid, start, end, name="晚上", kind="工作"):
    return {"id": pid, "date": ISO, "start": start, "end": end,
            "type": kind, "name": name, "calendar": kind, "title": name}


def fresh(periods):
    """干净的一天：给定时段、没有 layout/extras/任务。"""
    sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO,
                      "days": 1, "profiles": ["工作", "空闲"], "events": [],
                      "periods": periods})
    save_extras({})
    for path in (sch.DATA_CSV, sch.BACKUP_CSV,
                 os.environ["TI_SESSION_LAYOUTS"],
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
    wd.settings.set("postsession", "0")
    wd.settings.set("nostart", "0")
    wd.reload_periods()
    wd.reload_entries()
    return wd


def save_extras(days):
    sp.save_extras({"version": 1, "days": days})


def refresh_rows(wd):
    wd.rewrite_data_csv_from_plan()
    wd.reload_entries()
    wd.rebuild_rows()


def bind_task(wd, start, duration=25, title="钉住的任务"):
    """给某一场挂一条本地任务（挂任务的场次是「内容」，不是可回收碎片）。"""
    task = wd.task_store.create(fields={"title": title, "duration": duration,
                                        "count": max(1, duration // 25)})
    tid = int(task["id"]) if isinstance(task, dict) else int(task)
    hh, mm = [int(x) for x in start.split(":")]
    end_min = hh * 60 + mm + duration
    wd.task_store.assign(tid, {
        "date": ISO, "start": start,
        "end": "%02d:%02d" % (end_min // 60 % 24, end_min % 60),
        "duration": duration})
    return tid


def shells(wd, pid=None):
    out = []
    for row in wd.rows:
        if not row.get("period_only"):
            continue
        if pid is not None and str(row.get("period_id")) != pid:
            continue
        out.append((row.get("startLabel"), int(row.get("durationMin") or 0),
                    int(row.get("plan_counts") or 0),
                    bool(row.get("partial_gap"))))
    return out


# ---------------------------------------------------------------- 1. T4 现场
print("== 现场 T4：前面空、后面被任务钉住（22:15 未切 + 23:15 flashcard）==")
wd = fresh([period("T4", "22:15", "23:45")])
sp.set_layout(TODAY, "T4", [], source="auto")
sp.add_extra(TODAY, "23:15", 25, source="manual",
            note="任务钉住的场次（回收时从 layout 挪成窗口）")
bind_task(wd, "23:15", 25, "flashcard review")
refresh_rows(wd)
check("22:15 那一截要有一行占位（空档 60min = 2 count）",
      shells(wd), [("22:15", 60, 2, True)])
_t4 = [r for r in wd.rows if r.get("period_only")]
check("占位行不是 session（没 id / 不算计划）",
      [(_t4[0].get("id"), _t4[0].get("planned"))], [(None, False)])
check("占位行标着它是「切了一半的缺口」，不是整段",
      [(r.get("period_only"), r.get("partial_gap")) for r in _t4], [(True, True)])
check("整张表按起点排序（22:15 在 23:15 前面）",
      [r["startLabel"] for r in wd.rows], ["22:15", "23:15"])

# 表里画的是容量，plan 必须看到同一份容量（否则又是「看得见排不进」）。
_slots, _notes = wd._plan_slots(TODAY)
check("plan 在这一段看到 2 count（22:15 窗口）",
      [(s["start"], s["counts"], bool(s.get("window"))) for s in _slots],
      [("22:15", 2, True)])

# ---------------------------------------------------------------- 2. 整段没切
print("== 整段没切（T3）：一行整段占位，行为不许变 ==")
wd = fresh([period("T3", "19:00", "21:30", name="其他项目", kind="空闲")])
wd.reload_entries()
wd.rebuild_rows()
check("整段 150min → 5 count，partial=False",
      shells(wd), [("19:00", 150, 5, False)])

# ---------------------------------------------------------------- 3. 尾巴
print("== 尾巴空档（老口径）不许丢：19:00(75) 之后到 21:30 ==")
wd = fresh([period("T3", "19:00", "21:30")])
sp.set_layout(TODAY, "T3", [75], source="manual")
bind_task(wd, "19:00", 75)
refresh_rows(wd)
check("75 分钟场（占 90 分钟）之后还剩 60min = 2 count",
      shells(wd), [("20:30", 60, 2, True)])

# ---------------------------------------------------------------- 4. 中间洞
print("== 中间洞：19:00(25) + 20:00(25) 两场钉住，中间和尾巴都算空档 ==")
wd = fresh([period("T3", "19:00", "21:30")])
sp.set_layout(TODAY, "T3", [25], source="manual")
sp.add_extra(TODAY, "20:00", 25, source="manual", note="手切的洞")
bind_task(wd, "19:00", 25, "前一场")
bind_task(wd, "20:00", 25, "后一场")
refresh_rows(wd)
check("19:30–20:00 一截（1 count）+ 20:30–21:30 一截（2 count）",
      shells(wd), [("19:30", 30, 1, True), ("20:30", 60, 2, True)])

# ---------------------------------------------------------------- 5. 全可重排
print("== 自动 layout 且整段可重排：不画占位（跟 _plan_slots 同一口径）==")
wd = fresh([period("T1", "19:00", "21:30")])
sp.set_layout(TODAY, "T1", [50, 50], source="auto")
refresh_rows(wd)
check("没有任务、没有订上 → plan 会把这一段整体重切，不画多余占位",
      shells(wd), [])

# ---------------------------------------------------------------- 6. 碎块
print("== 不足 1 count（30 分钟）的碎块不画 ==")
wd = fresh([period("T4", "22:15", "23:45")])
sp.set_layout(TODAY, "T4", [], source="auto")
sp.add_extra(TODAY, "22:30", 25, source="manual", note="手切的洞")
bind_task(wd, "22:30", 25, "钉住的任务")
refresh_rows(wd)
check("前面只剩 22:15–22:30（15 分钟，排不进 1 count）→ 不画；后面 23:00–23:45 画",
      shells(wd), [("23:00", 45, 1, True)])

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for item in fails:
        print("- " + item)
    raise SystemExit(1)
print("全绿。")
