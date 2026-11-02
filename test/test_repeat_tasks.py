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
# 改排布会取 daemon 那一轮的 round lock、并写「我取消的」挂号单 —— 别落到真目录里
ks.LOCK_FILE = os.path.join(os.path.dirname(ks.STATE_FILE), "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(os.path.dirname(ks.STATE_FILE),
                                "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")

# 兜底：本文件里任何一条**意外**走到「真·一轮」的路径都要立刻炸，而不是
# 去连 api.focusmate.com（真下过单的东西，测试绝不该碰）。日志也一样要关进
# 临时目录 —— 以前漏了，用例的日志混进真 temp/keep_session.log 里，看日志
# 排障时会以为 daemon 半夜自己干了什么。
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")


def _no_network(*a, **kw):
    raise RuntimeError("测试里不许真连 Focusmate API（这一轮本该被替身拦住）")


ks.fetch_session_details = _no_network
ks.fetch_sessions = _no_network

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

print("== RRULE：OmniFocus 的 DAILY / 多 BYDAY / INTERVAL / UNTIL 照原样展开 ==")
# 用同一个任务改成「每天」；anchor 是 OF 的 due，start 是 session 起点
store.set_repeat(task["id"], "mon", "22:15", 50,
                 rule="FREQ=DAILY", rule_anchor="2026-10-26T23:05:00",
                 source="omnifocus", summary="每天 22:15（50 min）")
task = store.get(task["id"])
check("DAILY 原样存 RRULE", task["repeat"]["rule"], "FREQ=DAILY")
check("DAILY 存锚点", task["repeat"]["rule_anchor"], "2026-10-26T23:05:00")
check("DAILY 摘要", ts.repeat_summary(task["repeat"]), "每天 22:15（50 min）")
for offset in (0, 1, 2, 3):
    day = mon + datetime.timedelta(days=offset)
    check("DAILY %s 都落" % day,
          bool(store.repeat_occurrence(task, day,
                                       [{"start": "22:15", "duration": 50}])), True)

store.set_repeat(task["id"], "mon", "22:15", 50,
                 rule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
                 rule_anchor="2026-10-26T23:05:00", source="omnifocus")
task = store.get(task["id"])
_week = [mon + datetime.timedelta(days=i) for i in range(7)]
check("多 BYDAY：只落一、三、五",
      [d.weekday() for d in _week
       if store.repeat_occurrence(task, d, [{"start": "22:15", "duration": 50}])],
      [0, 2, 4])
check("多 BYDAY 摘要", ts.repeat_summary(task["repeat"]),
      "每周一、三、五 22:15（50 min）")

store.set_repeat(task["id"], "mon", "22:15", 50,
                 rule="FREQ=DAILY;INTERVAL=2",
                 rule_anchor="2026-10-26T23:05:00", source="omnifocus")
task = store.get(task["id"])
check("INTERVAL=2：隔一天落",
      [i for i in range(6)
       if store.repeat_occurrence(task, mon + datetime.timedelta(days=i),
                                  [{"start": "22:15", "duration": 50}])],
      [0, 2, 4])
check("INTERVAL=2 摘要", ts.repeat_summary(task["repeat"]),
      "每 2 天 22:15（50 min）")

store.set_repeat(task["id"], "mon", "22:15", 50,
                 rule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20261102T000000Z",
                 rule_anchor="2026-10-26T23:05:00", source="omnifocus")
task = store.get(task["id"])
check("UNTIL（带 Z）能解析",
      bool(store.repeat_occurrence(task, mon,
                                   [{"start": "22:15", "duration": 50}])), True)
check("UNTIL 之后不落",
      store.repeat_occurrence(task, mon + datetime.timedelta(days=7),
                              [{"start": "22:15", "duration": 50}]), None)

print("== 跨午夜：RRULE 锚点周四，场次起点是周三 ==")
store.set_repeat(task["id"], "wed", "23:15", 50,
                 rule="FREQ=WEEKLY;BYDAY=TH",
                 rule_anchor="2026-10-29T00:05:00", rule_offset_days=-1,
                 source="omnifocus")
task = store.get(task["id"])
wed = datetime.date(2026, 10, 28)
check("场次周三能落上", bool(store.repeat_occurrence(
    task, wed, [{"start": "23:15", "duration": 50}])), True)
check("规则锚点周四本身不落（那天的场次不是这个）",
      store.repeat_occurrence(task, datetime.date(2026, 10, 29),
                              [{"start": "23:15", "duration": 50}]), None)
check("摘要按场次起点显示周三", ts.repeat_summary(task["repeat"]),
      "每周三 23:15（50 min）")
try:
    store.set_repeat(task["id"], "mon", "22:15", 50,
                     rule="FREQ=WEEKLY;BYDAY=XX",
                     rule_anchor="2026-10-26T23:05:00")
    bad_rule_ok = False
except ValueError:
    bad_rule_ok = True
check("坏 RRULE 在写库前就被拒", bad_rule_ok, True)

# ---------------------------------------------------------------- 界面整合

print("== warden：task create --repeat + 表格里自动挂上 ==")
reset_tasks()
write_csv(["# test", "%s,22:15,50" % WD])
refresh_plan(mkwarden())
wd = mkwarden()
check("建带 repeat 的任务", wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD), True)
tasks = wd.task_store.list()
check("建出一条任务", len(tasks), 1)
parent = tasks[0]
check("本体是 repeat 模板", ts.TaskStore.is_repeat_template(parent), True)
rep = parent.get("repeat") or {}
check("repeat 记的是今天这个星期几", rep.get("weekday_label"), WD)
check("repeat 锚定的是那场 50min（按 layout 排出来的）",
      rep.get("duration"), 50)
check("task 视图表格里显示 🔁 + 人话摘要",
      "🔁 每周%s 22:15（50 min）" % ts.CN_WEEKDAYS[ts.WEEKDAYS.index(WD)]
      in render_text(wd.views[3].render()), True)

wd.api_rows = [{"startMs": 0, "endMs": 0, "startLabel": "22:15", "endLabel": "23:05",
                "durationMin": 50, "id": "sess-1", "title": "", "quiet": False,
                "matched": False, "phase": "未开始"}]
wd.rows_source = "bridge"
# 到日子了：TUI 的每日自动扫描生成一条子任务（本体本身不进契约表 task 列）
wd._repeat_gen_day = None
wd.maybe_generate_repeat_occurrences()
wd.rebuild_rows()
child = wd.task_store.occurrence_for(parent["id"], TODAY)
check("今天生成了 occurrence 子任务", bool(child), True)
check("子任务 repeat_parent 指向本体", child["repeat_parent"], parent["id"])
check("本体自己不挂 session（契约表看不到它）", parent.get("session"), None)
check("契约表格显示的是子任务（状态记号 + 🔁 + 编号 + 标题）",
      wd.rows[0]["task_text"], "·🔁%d: 明日时间安排" % wd.task_no(child))
check("行上标了这是重复任务", wd.rows[0]["task_repeat"], True)
check("契约表 row 里只有子任务（本体被过滤）",
      [t["id"] for t in wd.rows[0]["tasks"]], [child["id"]])
check("来源记号：重复子任务 = 🔁", ts.TaskStore.origin_marks(child), "🔁")
check("没有落点的日子不会显示任务（周二不看）",
      wd.repeated_task_for(OTHER, {"start": "22:15"}), None)

print("== 拆分之后：精确匹配没了，但子任务照生（时间不重要）==")
reset_tasks()
wd = mkwarden()
wd.handle("task create title=明日时间安排 --repeat %s@22:15" % WD)
parent = wd.task_store.list()[0]
wd.handle("period T1 25 25")            # 50 拆成 22:15+22:45 两场 25min
wd.reload_periods()
check("repeat 设置还在", (parent.get("repeat") or {}).get("start"), "22:15")
sessions = wd.sessions_for_day_plan(TODAY)
check("那天现在排出来的是 25+25", [(s["start"], s["duration"]) for s in sessions],
      [("22:15", 25), ("22:45", 25)])
parent = wd.task_store.get(parent["id"])
check("严格同起点同长的精确匹配确实没了",
      wd.task_store.repeat_occurrence(parent, TODAY, sessions), None)
wd.ensure_repeat_occurrences(TODAY)
child = wd.task_store.occurrence_for(parent["id"], TODAY)
check("但子任务仍然生成", bool(child), True)
check_true("挂到了当天某个 session（不是消失）",
           (child.get("session") or {}).get("start") in ("22:15", "22:45"))

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
child = wd.task_store.occurrence_for(task["id"], TODAY)
check("落了今天的 occurrence", list(occ), [ISO])
check("occurrence 指向子任务", occ[ISO].get("child_id"), child["id"])
check("occurrence 记了 Focusmate task id",
      (occ[ISO].get("focusmate") or {}).get("task_id"), "ft-repeat-1")
check("occurrence 记了那一场 session",
      (occ[ISO].get("session") or {}).get("focusmate_session_id"), "sess-9")
check("Focusmate task 挂在子任务上，不在本体上",
      ((child.get("focusmate") or {}).get("task_id"), task.get("focusmate")),
      ("ft-repeat-1", {}))
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

print("== formatter 属于子任务：本体只留模板，今天/明天的子任务被排好 ==")
TOMORROW = TODAY + datetime.timedelta(days=1)
WD2 = sch.weekday_of_day(TOMORROW)
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作", "空闲"], "events": [],
                  "periods": [
                      {"id": "T1", "date": ISO, "start": "22:15", "end": "23:15",
                       "type": "空闲", "name": "晚间", "calendar": "空闲", "title": "晚间"},
                      {"id": "T2", "date": TOMORROW.isoformat(), "start": "22:15",
                       "end": "23:15", "type": "空闲", "name": "晚间", "calendar": "空闲",
                       "title": "晚间"},
                  ]})
write_csv(["# test", "%s,22:15,50" % WD, "%s,22:15,50" % WD2])
refresh_plan(mkwarden())
reset_tasks()
wd = mkwarden()
wd.handle("task create title=闪卡复习 duration=25 --repeat %s@22:15" % WD)
parent = wd.task_store.list()[0]
# 这个用例要「每天」：改用 OmniFocus 那种原生 RRULE，今天+明天都是规则日。
wd.task_store.set_repeat(parent["id"], WD, "22:15", 50,
                         rule="FREQ=DAILY", rule_anchor="%sT22:15:00" % ISO,
                         source="omnifocus")
parent = wd.task_store.get(parent["id"])
check("本体不进契约任务面板", wd.task_no(parent), None)
check("给本体挂 formatter end（用 id:N）",
      wd.handle("task id:%d -f end" % parent["id"]), True)
parent = wd.task_store.get(parent["id"])
check("本体自己仍然没有 session", parent.get("session"), None)
fmt = parent.get("formatter") or {}
check("本体只留 formatter 模板", fmt.get("name"), "end")
check("本体不带 reservation", fmt.get("reservation"), None)
check("本体不带 result", fmt.get("result"), None)
check_true("本体在任务面板里不画 ⏱（那是子任务的记号）",
           "⏱" not in wd.task_index_title(parent))
child_today = wd.task_store.occurrence_for(parent["id"], TODAY)
child_tomorrow = wd.task_store.occurrence_for(parent["id"], TOMORROW)
check("今天生成了子任务", bool(child_today), True)
check("明天也提前生成了子任务", bool(child_tomorrow), True)
check("今天子任务在今天面板里",
      child_today["id"] in [t["id"] for t in wd.task_panel_tasks()], True)
check("明天的子任务不在今天面板里",
      child_tomorrow["id"] in [t["id"] for t in wd.task_panel_tasks()], False)
check("今天子任务继承了 formatter 并跑过",
      ((child_today.get("formatter") or {}).get("reservation") or {}).get("date"),
      ISO)
check("明天子任务也继承了 formatter 并跑过",
      ((child_tomorrow.get("formatter") or {}).get("reservation") or {}).get("date"),
      TOMORROW.isoformat())
check_true("今天子任务挂到了 session（T1 可能被上一节拆成 25+25）",
           ((child_today.get("session") or {}).get("start")) in ("22:15", "22:45"))
check("明天子任务挂到了 session",
      ((child_tomorrow.get("session") or {}).get("start")), "22:15")
check_true("契约表 task 列会把明天的子任务画上 ⏱",
           "⏱" in wd.task_cell_text([child_tomorrow]))
check_true("本体不在契约表 task 列里",
           all(t.get("id") != parent["id"] for t in wd.rows[0].get("tasks") or []))
# 切到明天：契约表能看到那条被 format 的子任务
wd.day_offset = 1
wd.reload_entries()
wd.rebuild_rows()
row = next((r for r in wd.rows if r.get("startLabel") == "22:15"), None)
check("切到明天有 22:15 那一行", bool(row), True)
check_true("明天的行里是子任务 + ⏱",
           row and ("⏱" in row.get("task_text", ""))
           and (child_tomorrow["id"] in [t["id"] for t in row.get("tasks") or []]))
# 本体不能被当成一次可完成的活
try:
    wd.cmd_task_done(["id:%d" % parent["id"]])
    _blocked = False
except ValueError as e:
    _blocked = "重复任务" in str(e) and "本体" in str(e)
check("本体 task done 被拒（子任务才能完成）", _blocked, True)
try:
    wd._apply_mark(parent, ts.STATUS_COMPLETED, "hook")
    _mark_blocked = False
except ValueError:
    _mark_blocked = True
check("本体 -m complete 也被拒", _mark_blocked, True)

# 旧数据自愈：本体身上如果还挂着 session（以前的 -f end 排过），转给当天子任务
legacy = wd.task_store.create({"title": "旧重复任务", "duration": 25})
wd.task_store.set_repeat(legacy["id"], WD, "22:15", 50,
                         rule="FREQ=DAILY", rule_anchor="%sT22:15:00" % ISO,
                         source="omnifocus")
legacy_sess = {"index": 1, "date": ISO, "start": "22:15", "end": "23:05",
               "duration": 50, "focusmate_session_id": "sess-legacy"}
wd.task_store.assign(legacy["id"], legacy_sess)
wd.task_store.attach_focusmate(legacy["id"],
                               {"task_id": "ft-legacy", "session_id": "sess-legacy"},
                               session=legacy_sess)
legacy_parent = wd.task_store.get(legacy["id"])
legacy_parent = wd._adopt_template_session(legacy_parent)
legacy_child = wd.task_store.occurrence_for(legacy["id"], TODAY)
check("旧本体被下架", legacy_parent.get("session"), None)
check("旧本体的 session 转给子任务",
      (legacy_child.get("session") or {}).get("start"), "22:15")
check("旧本体的 Focusmate task 也转给子任务",
      (legacy_child.get("focusmate") or {}).get("task_id"), "ft-legacy")

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
