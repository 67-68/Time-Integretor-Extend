#!/usr/bin/env python3
"""每周重复任务（repeated task）的回归测试。

    python3 test/test_repeat_tasks.py

用户的要求：
  - 抽象出「重复任务」这一模式：`task create ... --repeat 周几@HH:MM`；
  - 暂时只支持**每周**重复：设在周一那场不影响周二周三；
  - 如果那场 session 被改了（例如 50 拆成 25+25），重复任务的**设置不变**，
    但这一周的重复任务消失 —— 拆分后两个 session 都没有这个任务；
  - 今天所有 session 都没有这个重复任务时，状态框里给警告 + 怎么设置。

不联网、不碰浏览器、不写仓库里的任何文件。
"""
import datetime
import importlib.util
import io
import os
import shutil
import sys
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


TMP = tempfile.mkdtemp(prefix="repeat-task-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, ks, cfgs = w.sp, w.sch, w.ts, w.ks, w.cfg
sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **k: None
w.run_keep_session = lambda *a, **kw: (0, "round done", "")

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
WD = sch.weekday_of_day(TODAY)
# 下一个不同的星期几（保证不是今天）
OTHER = TODAY + datetime.timedelta(days=(1 if TODAY.weekday() < 6 else -1))
OTHER_WD = sch.weekday_of_day(OTHER)

# 今天一个 22:15–23:15 的空闲时段（T1）
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作", "空闲"], "events": [],
                  "periods": [
                      {"id": "T1", "date": ISO, "start": "22:15", "end": "23:15",
                       "type": "空闲", "name": "晚间", "calendar": "空闲", "title": "晚间"},
                  ]})


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.last_idle_close = None

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "add_task":
            return {"verified": True, "taskId": "ft-repeat-1",
                    "task": {"id": "ft-repeat-1"}}
        if cmd == "tasks":
            return {"tasks": [{"id": "ft-repeat-1", "completedAt": "2026-10-26T14:00:00Z"}]}
        if cmd == "release":
            return {"released": True}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden(offline=True):
    console = Console(file=io.StringIO(), width=140, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=offline)
    wd.reload_entries()
    return wd


def write_csv(lines):
    with open(sch.DATA_CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def reset_tasks():
    """清空任务库（每一节用例都从干净状态开始）。"""
    for path in (ts.tasks_path(), ts.archive_path()):
        if os.path.exists(path):
            os.remove(path)


def render_text(renderable, width=160):
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    for part in (renderable if isinstance(renderable, (list, tuple)) else [renderable]):
        console.print(part)
    return buf.getvalue()


def refresh_plan(wd):
    """按下载层 + layout 重算 data.csv（等同 sync/populate 的效果）。"""
    wd.rewrite_data_csv_from_plan()
    wd.reload_entries()


# ---------------------------------------------------------------- task_store

print("== task_store：repeat 是任务的一部分 ==")
store = ts.TaskStore()
task = store.create({"title": "明日时间安排"})
check("新建任务没有 repeat", store.get(task["id"])["repeat"], None)
store.set_repeat(task["id"], "mon", "22:15", 50)
task = store.get(task["id"])
check("repeat 记下周几", task["repeat"]["weekday_label"], "mon")
check("repeat 记下开始时刻", task["repeat"]["start"], "22:15")
check("repeat 记下锚定时长", task["repeat"]["duration"], 50)
check("repeated_tasks() 能捞出来", [t["id"] for t in store.repeated_tasks()], [task["id"]])
check("按星期几过滤：周二没有",
      store.repeated_tasks(weekday=1) if task["repeat"]["weekday"] != 1 else [], [])
try:
    store.set_repeat(task["id"], "mon", "22:07")
    check("非 15 分钟整点被拒", False, True)
except ValueError as e:
    check_true("非整点给了人话提示", "15 分钟整点" in str(e))
check("清除 repeat", store.clear_repeat(task["id"])["repeat"], None)
store.set_repeat(task["id"], "mon", "22:15", 50)

# ---------------------------------------------------------------- 落点匹配

print("== 落点：同一星期几 + 同一时刻 + 同一时长 ==")
mon = datetime.date(2026, 10, 26)
check("2026-10-26 是周一", mon.weekday(), 0)
task = store.get(task["id"])
hit = store.repeat_occurrence(task, mon, [{"start": "22:15", "duration": 50}])
check("周一同点同长 → 落上", hit["start"], "22:15")
check("同点不同长（50 拆成 25+25）→ 不落",
      store.repeat_occurrence(task, mon, [{"start": "22:15", "duration": 25},
                                          {"start": "22:45", "duration": 25}]), None)
check("周二 → 不落（每周重复不影响别的天）",
      store.repeat_occurrence(task, mon + datetime.timedelta(days=1),
                              [{"start": "22:15", "duration": 50}]), None)
check("下一周同一时刻 → 照旧落",
      store.repeat_occurrence(task, mon + datetime.timedelta(days=7),
                              [{"start": "22:15", "duration": 50}])["start"], "22:15")

# ---------------------------------------------------------------- 界面整合

print("== warden：task create --repeat + 表格里自动挂上 ==")
reset_tasks()
write_csv(["# test", "%s,22:15,50" % WD])
refresh_plan(mkwarden())
wd = mkwarden()
check("建带 repeat 的任务", wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD), True)
tasks = wd.task_store.list()
check("建出一条任务", len(tasks), 1)
rep = tasks[0].get("repeat") or {}
check("repeat 记的是今天这个星期几", rep.get("weekday_label"), WD)
check("repeat 锚定的是那场 50min（按 layout 排出来的）",
      rep.get("duration"), 50)
check("task 视图表格里显示 🔁",
      "🔁 %s@22:15" % WD in render_text(wd.views[3].render()), True)

wd.api_rows = [{"startMs": 0, "endMs": 0, "startLabel": "22:15", "endLabel": "23:05",
                "durationMin": 50, "id": "sess-1", "title": "", "quiet": False,
                "matched": False, "phase": "未开始"}]
wd.rows_source = "bridge"
wd.rebuild_rows()
check("表格那一行自动显示这个重复任务", wd.rows[0]["task_text"], "🔁 明日时间安排")
check("行上标了这是重复任务", wd.rows[0]["task_repeat"], True)
check("没有落点的日子不会显示任务（周二不看）",
      wd.repeated_task_for(OTHER, {"start": "22:15"}), None)

print("== 拆分之后：这一周没有重复任务，设置不变 ==")
reset_tasks()
wd = mkwarden()
wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD)
wd.handle("period T1 25 25")            # 50 拆成 22:15+22:45 两场 25min
wd.reload_periods()
check("repeat 设置还在", (wd.task_store.list()[0].get("repeat") or {}).get("start"), "22:15")
sessions = wd.sessions_for_day_plan(TODAY)
check("那天现在排出来的是 25+25", [(s["start"], s["duration"]) for s in sessions],
      [("22:15", 25), ("22:45", 25)])
check("拆分后第一场也有没任务（时长对不上）",
      wd.repeated_task_for(TODAY, {"start": "22:15"}), None)
check("拆分后第二场也没有任务",
      wd.repeated_task_for(TODAY, {"start": "22:45"}), None)

print("== task sync：把重复任务挂到 session 上（Focusmate 复核成功才写本地）==")
reset_tasks()
write_csv(["# test", "%s,22:15,50" % WD])
refresh_plan(mkwarden())
wd = mkwarden(offline=False)
wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD)
wd.api_rows = [{"startMs": 1234, "endMs": 5678, "startLabel": "22:15", "endLabel": "23:05",
                "durationMin": 50, "id": "sess-9", "title": "", "quiet": False,
                "matched": False, "phase": "未开始"}]
wd.rows_source = "bridge"
wd.rebuild_rows()
wd.bridge.calls = []
check("task sync 受理", wd.handle("task sync"), True)
adds = [c for c in wd.bridge.calls if c[0] == "add_task"]
check("给这一场调了一次 add_task", len(adds), 1)
check("add_task 用的是那场 session id", adds[0][1].get("meetingId"), "sess-9")
task = wd.task_store.list()[0]
occ = (task.get("repeat") or {}).get("occurrences") or {}
check("落了今天的 occurrence", list(occ), [ISO])
check("occurrence 记了 Focusmate task id",
      (occ[ISO].get("focusmate") or {}).get("task_id"), "ft-repeat-1")
check("occurrence 记了那一场 session",
      (occ[ISO].get("session") or {}).get("focusmate_session_id"), "sess-9")
wd.bridge.calls = []
wd.handle("task sync")
check("再 sync 不会重复挂（幂等）",
      [c for c in wd.bridge.calls if c[0] == "add_task"], [])
check("完成时间进归档", len(wd.task_store.load_archive()["records"]), 1)
check("归档里的完成时间来自 completedAt",
      wd.task_store.load_archive()["records"][0]["focusmate"]["completed_at"],
      "2026-10-26T14:00:00Z")

# ---------------------------------------------------------------- 警告

print("== 警告：今天没有重复任务 → 状态框里说怎么设 ==")
write_csv(["# test", "%s,22:15,50" % WD])
refresh_plan(mkwarden())
reset_tasks()                           # 从「没有任何重复任务」重新开始
wd = mkwarden()
wd.refresh_warnings(force=True)
warning = [x for x in wd.warnings if x.source == "重复任务"]
check("有一条重复任务警告", len(warning), 1)
check_true("警告里给了怎么设置", "--repeat %s@" % WD in warning[0].hint)
check_true("警告出现在状态框里", "重复任务" in wd.shell.status_box_text())
check_true("状态框里连 --repeat 的写法都有",
           "--repeat" in wd.shell.status_box_text())
wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD)
wd.refresh_warnings(force=True)
check("建好之后就没事了", [x for x in wd.warnings if x.source == "重复任务"], [])

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
