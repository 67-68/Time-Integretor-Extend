#!/usr/bin/env python3
"""``-f repeat`` formatter 的回归测试。

    python3 test/test_repeat_formatter.py

需求：指定一场已有 session + 一条任务，把任务自动挂上去；用在重复任务本体
上时，每周每个 occurrence 都固定落同一场，不因为当天排布/默认重复锚点而漂走。
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


TMP = tempfile.mkdtemp(prefix="repeat-formatter-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, ks = w.sp, w.sch, w.ts, w.ks
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
ks.fetch_session_details = lambda *a, **kw: (_ for _ in ()).throw(
    RuntimeError("测试里不许真连 Focusmate API"))
ks.fetch_sessions = ks.fetch_session_details
w.run_keep_session = lambda *a, **kw: (0, "round done", "")

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
TOMORROW = TODAY + datetime.timedelta(days=1)
WD = sch.weekday_of_day(TODAY)


def _period(pid, day, start, end):
    return {"id": pid, "date": day.isoformat(), "start": start, "end": end,
            "type": "工作", "name": pid, "calendar": "工作", "title": pid}


sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作", "空闲"], "events": [],
                  "periods": [
                      _period("T1", TODAY, "09:00", "10:00"),
                      _period("T2", TODAY, "10:00", "11:00"),
                      _period("T3", TOMORROW, "09:00", "10:00"),
                      _period("T4", TOMORROW, "10:00", "11:00"),
                  ]})


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "sessions": []}
        if cmd == "add_task":
            return {"verified": True, "taskId": "ft-repeat", "task": {"id": "ft-repeat"}}
        if cmd == "tasks":
            return {"tasks": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden():
    console = Console(file=io.StringIO(), width=180, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(8, 0))
    wd.reload_entries()
    return wd


def reset_tasks():
    for path in (ts.tasks_path(), ts.archive_path()):
        if os.path.exists(path):
            os.remove(path)


print("== formatter 注册 + 纯函数：按起点/序号选已有 session ==")
check("task -f 列表里有 repeat", "repeat" in w.th.preset_names("formatter"), True)
check("Tab 补全包含 repeat", "repeat" in mkwarden().shell.complete("task -f "), True)
_sessions = [
    {"date": ISO, "start": "09:00", "end": "09:50", "duration": 50},
    {"date": ISO, "start": "10:00", "end": "10:50", "duration": 50},
]
check("按起点选到 10:00 那场",
      w.th._repeat_target_session("10:00", _sessions)["start"], "10:00")
check("按第 2 场选到 10:00 那场",
      w.th._repeat_target_session("#2", _sessions)["start"], "10:00")
try:
    w.th._repeat_target_session("11:00", _sessions)
    _missing = False
except w.th.HookError as exc:
    _missing = "没有起点为 11:00" in str(exc)
check("目标 session 不存在时明确报错", _missing, True)

_ctx = {
    "task": {"id": 1, "fields": {"duration": 25}},
    "task_id": 1,
    "date": ISO,
    "sessions": _sessions,
    "periods": [],
    "reservations": [],
}
_repeat = w.th.format_repeat(dict(_ctx, task={"id": 1, "fields": {"duration": 25}}),
                             session="10:00")
check("repeat 返回已有 session，不新造",
      [_repeat["session"]["start"], _repeat["new_session"]], ["10:00", False])
check("reservation 固定到目标场起点并记整场身份",
      [_repeat["reservation"]["start"], _repeat["reservation"]["end"],
       _repeat["reservation"]["session_start"], _repeat["reservation"]["session_duration"]],
      ["10:00", "10:25", "10:00", 50])
try:
    w.th.format_repeat(dict(_ctx, task={"id": 1, "fields": {"duration": 80}}),
                        session="10:00")
    _too_long = False
except w.th.HookError as exc:
    _too_long = "装不下任务 80 分钟" in str(exc)
check("任务比目标场长时明确报错", _too_long, True)

print("== 普通任务：task <序号> -f repeat 10:00 自动挂到指定场 ==")
reset_tasks()
wd = mkwarden()
wd.handle("task create title=固定十点 duration=25")
task = [t for t in wd.task_store.list() if t["fields"].get("title") == "固定十点"][0]
check("挂 repeat formatter 受理", wd.handle("task %d -f repeat 10:00" % wd.task_no(task)), True)
task = wd.task_store.get(task["id"])
fmt = task.get("formatter") or {}
res = fmt.get("reservation") or {}
check("formatter 记成 repeat", fmt.get("name"), "repeat")
check("formatter 参数记住目标场起点", (fmt.get("args") or {}).get("session"), "10:00")
check("任务挂到 10:00 那场",
      [(task.get("session") or {}).get("start"),
       (task.get("session") or {}).get("duration")], ["10:00", 50])
check("reservation 的 session 身份正确",
      [res.get("session_start"), res.get("session_duration")], ["10:00", 50])
check("截止时间 = 这一场终点", task["fields"].get("due"), "%sT10:50:00" % ISO)
check("刷新时已经正确 → formatter 校验通过", wd._formatter_apply_valid(task, TODAY), True)

# 同一天再钉一条到同一场会被重复占用挡住（repeat 从场起点开始）。
wd.handle("task create title=也想去十点 duration=25")
other = [t for t in wd.task_store.list() if t["fields"].get("title") == "也想去十点"][0]
wd.messages = []
check("第二条挂同一场也受理（错误进消息区）",
      wd.handle("task %d -f repeat 10:00" % wd.task_no(other)), True)
other = wd.task_store.get(other["id"])
check("第二条没有偷偷落 formatter", other.get("formatter"), None)
check_true("第二条给了「被别的 formatter 占了」的提示",
           any("已经被别的 formatter 占了" in m[2] for m in wd.messages))

# 也可以用当天第 N 场点名；`#` 经过 shlex 仍会原样到达 formatter。
wd.handle("task create title=按序号选场 duration=25")
by_index = [t for t in wd.task_store.list() if t["fields"].get("title") == "按序号选场"][0]
check("按 #1 挂 repeat formatter 受理",
      wd.handle("task %d -f repeat #1" % wd.task_no(by_index)), True)
by_index = wd.task_store.get(by_index["id"])
check("按序号选中了 09:00 那场",
      (by_index.get("session") or {}).get("start"), "09:00")

print("== 重复任务本体：每个 occurrence 都固定在 10:00 那场 ==")
reset_tasks()
wd = mkwarden()
check("建带 repeat 的任务",
      wd.handle("task create title=每周固定晨读 duration=25 --repeat %s@09:00" % WD), True)
parent = wd.task_store.list()[0]
# 用 RRULE 把今天/明天都变成规则日；repeat 锚点 09:00，formatter 目标 10:00。
wd.task_store.set_repeat(parent["id"], WD, "09:00", 50,
                         rule="FREQ=DAILY", rule_anchor="%sT09:00:00" % ISO,
                         source="omnifocus")
parent = wd.task_store.get(parent["id"])
check("给本体挂 repeat formatter",
      wd.handle("task id:%d -f repeat 10:00" % parent["id"]), True)
parent = wd.task_store.get(parent["id"])
fmt = parent.get("formatter") or {}
check("本体只保存 repeat 模板，不挂 session",
      [fmt.get("name"), fmt.get("reservation"), parent.get("session")],
      ["repeat", None, None])
child_today = wd.task_store.occurrence_for(parent["id"], TODAY)
child_tomorrow = wd.task_store.occurrence_for(parent["id"], TOMORROW)
check("今天/明天都生成了 occurrence", bool(child_today and child_tomorrow), True)
for label, child in (("今天", child_today), ("明天", child_tomorrow)):
    cfmt = child.get("formatter") or {}
    cres = cfmt.get("reservation") or {}
    check("%s子任务继承 repeat formatter" % label, cfmt.get("name"), "repeat")
    check("%s子任务固定在 10:00（不是 repeat 锚点 09:00）" % label,
          [(child.get("session") or {}).get("start"),
           cres.get("session_start")], ["10:00", "10:00"])
    check("%s子任务的 reservation 落在自己那一天" % label,
          cres.get("date"), child.get("repeat_date"))
    check("%s子任务校验通过" % label,
          wd._formatter_apply_valid(child, datetime.date.fromisoformat(
              child["repeat_date"])), True)

# 本体的 formatter 模板会继续转嫁给以后生成的 occurrence。
parent = wd.task_store.get(parent["id"])
check("本体模板里没有 reservation / result",
      [((parent.get("formatter") or {}).get("reservation")),
       ((parent.get("formatter") or {}).get("result"))], [None, None])

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
