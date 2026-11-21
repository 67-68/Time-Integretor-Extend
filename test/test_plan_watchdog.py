#!/usr/bin/env python3
"""plan 提醒任务（plan_watchdog）的回归测试。

需求（2026-11-20 用户原话）：

    在没有 plan 的情况下，创建一个 plan 任务，要求用户完成 TI 中今天 - 后天的
    plan，同时使用 formatter 安排在最近的第一个 session。如果检查的时候这个
    任务已经存在，且安排在今天的某个 session，且这个 session 已经过去，同时
    没有计划，那么自动安排计划，用随机算法安排 count 到不同的 session。

拍板口径：只检查**今天**；提醒任务 = 25 分钟（1 count）；排期复用 formatter
``enlist``；兜底只自动 plan **今天**；plan 一存在就把提醒任务本地标完成；
提醒任务每天最多一条；兜底每天最多一次。

    python3 test/test_plan_watchdog.py

全程离线：不碰 Focusmate / OmniFocus / 真工作区（所有落盘都指到 TMP）。
"""
import datetime
import importlib.util
import io
import json
import os
import random
import tempfile
import time

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-watchdog-test-")

for _name, _file in (
    ("TI_LOG_DIR", "logs"),
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
    ("TI_FAILED_ACTIONS", "failed-actions.json"),
):
    os.environ[_name] = os.path.join(TMP, _file)
os.environ["TI_LOG_LEVEL"] = "debug"

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

sp, sch, ts, ks, pj, pp, pw = w.sp, w.sch, w.ts, w.ks, w.pj, w.pp, w.pw

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
w.run_keep_session = lambda *a, **kw: (0, "", "")

DAY = datetime.date.today()
ISO = DAY.isoformat()
CLOCK = datetime.datetime.combine(DAY, datetime.time(9, 0))
PERIOD = {"id": "T1", "date": ISO, "start": "11:00", "end": "15:00",
          "type": "工作", "name": "测试时段", "calendar": "工作", "title": "测试时段"}

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


class FakeBridge(object):
    def stop(self):
        pass

    def browser_state(self):
        return True, False, ""

    def call(self, cmd, timeout=240, **kw):
        return {"verified": True, "taskId": "ft-1", "task": {"id": "ft-1"},
                "sessions": []}


def mkwarden(clock=None):
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.clock_now = clock or CLOCK
    # 测试里的「检查」一律显式调 _plan_watchdog_check()；把每帧钩子的节流
    # 推到一小时后，免得 setup 里的 handle() 顺手跑一遍检查。
    wd._plan_watchdog_at = time.time() + 3600
    return wd


def reset_files():
    pj.save(pj.blank())
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    # 每个用例都从「干净的这一天」开始：layout / extras / data.csv 不能带着上一条
    # 用例的场次（有 layout 的话「没有 plan」的场景会被误判成已经有空场）。
    for path in (os.path.join(TMP, "plan-watchdog.json"),
                 os.path.join(TMP, "archive.json"),
                 os.path.join(TMP, "layouts.json"),
                 os.path.join(TMP, "extras.json"),
                 os.path.join(TMP, "data.csv")):
        try:
            os.remove(path)
        except OSError:
            pass


def save_period(period=None):
    save_periods([dict(period or PERIOD)])


def save_periods(periods):
    sp.save_download({
        "version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
        "profiles": ["工作"], "events": [],
        "periods": [dict(p) for p in periods]})


TASKS = [
    {"pk": "t-a", "title": "写 A", "duration": 50, "count": 2,
     "completed": False, "dropped": False},
    {"pk": "t-b", "title": "写 B", "duration": 25, "count": 1,
     "completed": False, "dropped": False},
]


def seed_projects(pois=None):
    """一个 running 阶段的项目 + 它的 SOP op（plan_ops 只认这种组合）。"""
    op, _created = wd.projects.upsert({
        "id": "opA", "title": "项目 A", "op_type": "SOP", "task_type": "parallel",
        "project_tag": "项目 A", "tasks": list(pois or TASKS)})
    wd.projects.create_pro("项目 A", stage="running", op_id=op["id"])
    wd.reload_periods()
    return op


def fresh(layout="session T1 50", period=None, periods=None, clock=None):
    """干净任务库/项目库 + 今天的时段 + 一条 layout；返回 (wd, op)。"""
    global wd
    reset_files()
    if periods is not None:
        save_periods(periods)
    else:
        save_period(period)
    wd = mkwarden(clock)
    op = seed_projects()
    if layout:
        if not wd.handle(layout):
            raise SystemExit("layout 命令失败：%s" % layout)
    wd.rebuild_rows()
    return wd, op


def reminders(wd=None):
    wd = wd or globals()["wd"]
    return [task for task in wd.task_store.list() if pw.is_reminder(task)]


def allocated(wd, day=DAY):
    return pw.allocated_counts(wd.projects.plan_ops(), day)


def ledger():
    try:
        with open(os.environ["TI_PLAN_WATCHDOG"], encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


print("== 纯函数：认人 / 算时间 / 随机分资源 ==")
check("source.kind 对得上就是提醒任务",
      pw.is_reminder({"source": {"kind": "plan-watchdog"}}), True)
check("标题像但来源不对不算",
      pw.is_reminder({"fields": {"title": pw.TITLE}, "source": {"kind": "project"}}),
      False)
_end = pw.session_end({"date": ISO, "start": "12:00", "duration": 25})
check("session 结束时刻", _end, datetime.datetime.combine(DAY, datetime.time(12, 25)))
check("缺 duration 认不出来", pw.session_end({"date": ISO, "start": "12:00"}), None)
check("allocated 按 plans[day].count 求和",
      pw.allocated_counts([{"plans": {ISO: {"count": 2}}},
                           {"plans": {ISO: {"count": 3}}}], DAY), 5)

_rng = random.Random(7)
_allocs = pw.random_allocs([{"id": 1}, {"id": 2}, {"id": 3}], 5, _rng)
check("随机分配用满容量", sum(item["count"] for item in _allocs), 5)
check_true("每份至少 1 count", all(item["count"] >= 1 for item in _allocs))
check("同一个种子可复现",
      [(i["project_id"], i["count"]) for i in pw.random_allocs(
          [{"id": 1}, {"id": 2}, {"id": 3}], 5, random.Random(7))],
      [(i["project_id"], i["count"]) for i in _allocs])
check("没有项目 → 空", pw.random_allocs([], 5, random.Random(1)), [])
check("没有容量 → 空", pw.random_allocs([{"id": 1}], 0, random.Random(1)), [])

print("== 闸门：正在问答时什么都不做 ==")
wd, op = fresh()
for _attr in ("planning", "pending_import", "nostart", "pending_interrupt"):
    setattr(wd, _attr, object())
    check("问答中（%s）不建任务" % _attr, wd._plan_watchdog_check(), None)
    setattr(wd, _attr, None)
check("闸门放开后没有残留提醒任务", reminders(wd), [])

print("== 没有 plan + 有空场 → 建 1 条 25 分钟任务并用 enlist 排最早一场 ==")
wd, op = fresh()
check("检查结果 = create", wd._plan_watchdog_check(), "create")
_rs = reminders(wd)
check("只建一条", len(_rs), 1)
_rem = _rs[0]
check("标题", _rem["fields"]["title"], "排今天–后天的 plan")
check("时长 25 / 1 count", _rem["fields"]["duration"], 25)
check("备注写清楚 plan / plan +1 / plan +2",
      _rem["fields"]["notes"].count("plan +2"), 1)
check("session 落在当天最早的空位（11:00 那场之后 → 12:00）",
      (_rem.get("session") or {}).get("start"), "12:00")
check("排出来的场是 25 分钟", (_rem.get("session") or {}).get("duration"), 25)
check("formatter 自动独占", (w.th.is_exclusive(_rem)), True)
_layouts = sp.load_layouts()
_sessions = sp.sessions_for_period(sp.periods_for_date(sp.load_download(), DAY)[0],
                                   _layouts)
check("layout 被切成 [50, 25]",
      [(s["start"], s["duration"]) for s in _sessions],
      [("11:00", 50), ("12:00", 25)])
check("账本记了今天建过一条", (ledger().get("created") or {}).get(ISO) is not None, True)

check("再检查一次不会重复建", wd._plan_watchdog_check(), None)
check("任务库里还是一条", len(reminders(wd)), 1)

print("== plan 一存在 → 提醒任务本地标完成（不碰外部系统） ==")
wd, op = fresh()
wd._plan_watchdog_check()
_rem = reminders(wd)[0]
wd.projects.set_allocation(op["id"], DAY, 2)
check("检查结果 = close", wd._plan_watchdog_check(), "close")
_after = wd.task_store.get(_rem["id"])
check("提醒任务本地完成", _after["status"], "completed")
check("没有写 OmniFocus 的 outbox", os.path.exists(os.environ["TI_OMNIFOCUS_OUTBOX"])
      and bool(json.load(open(os.environ["TI_OMNIFOCUS_OUTBOX"])).get("ops")), False)
check("再检查不建新任务", wd._plan_watchdog_check(), None)
check("任务库里没有新的提醒任务", len([t for t in reminders(wd)
                                        if t["status"] == "active"]), 0)

print("== 提醒任务那场还没过去 → 不动 ==")
wd, op = fresh()
wd._plan_watchdog_check()
check("09:00 时 12:00 那场还没开始", wd._plan_watchdog_check(), None)
check("没有兜底账", (ledger().get("auto_planned") or {}).get(ISO), None)

print("== 那场过去了 + 今天仍没 plan → 随机分配，自动 plan 今天 ==")
wd, op = fresh()
wd._plan_watchdog_check()
_rem = reminders(wd)[0]
wd.clock_now = datetime.datetime.combine(DAY, datetime.time(12, 30))
wd.rebuild_rows()
_detail = wd._plan_watchdog_check()
check_true("兜底真的跑了", isinstance(_detail, dict) and _detail.get("allocations", 0) >= 1)
check("今天有 plan 了", allocated(wd) > 0, True)
check("账本记了 auto_planned", (ledger().get("auto_planned") or {}).get(ISO, {}).get("status"),
      "ok")
check("提醒任务退休（本地完成）", wd.task_store.get(_rem["id"])["status"], "completed")
_project_tasks = [t for t in wd.task_store.list()
                  if (t.get("source") or {}).get("kind") == "project"
                  and (t.get("session") or {}).get("start")]
check_true("项目任务挂到了 session 上", len(_project_tasks) >= 1)
check("兜底一天只跑一次：再检查不再动",
      wd._plan_watchdog_check(), None)
check("plan 没被清掉", allocated(wd) > 0, True)

print("== 昨天留下的提醒任务 → 退休，今天重新建 ==")
wd, op = fresh(layout=None)
_stale = wd.task_store.create(
    {"title": "排今天–后天的 plan", "duration": 25},
    source={"kind": pw.SOURCE_KIND})
wd.task_store.assign(_stale["id"], {"date": (DAY - datetime.timedelta(days=1)).isoformat(),
                                   "start": "11:00", "duration": 25})
wd.handle("session T1 50")
wd.rebuild_rows()
check("检查结果 = create", wd._plan_watchdog_check(), "create")
check("旧的退休", wd.task_store.get(_stale["id"])["status"], "completed")
check("今天有一条新的", len([t for t in reminders(wd) if t["status"] == "active"]), 1)

print("== 今天没有空场 / 没有项目 → 不建 ==")
wd, op = fresh(layout=None, period=dict(PERIOD, start="07:00", end="08:00"))
wd.handle("session T1 50")
wd.rebuild_rows()
check("整段已经过去 → 不建", wd._plan_watchdog_check(), None)
check("任务库干净", reminders(wd), [])

pj.save(pj.blank())
wd = mkwarden()
save_period()
wd.handle("session T1 50")
wd.rebuild_rows()
check("没有可排项目 → 不建", wd._plan_watchdog_check(), None)
check("任务库干净（无项目）", reminders(wd), [])

print("== 真的没 plan：只有 period、没有 layout → 按整段容量建提醒 ==")
wd, op = fresh(layout=None)
_pend = wd.pending_plan() or {}
check("容量按 period 整段算（11:00–15:00 = 8 count）", _pend.get("counts"), 8)
check("检查结果 = create", wd._plan_watchdog_check(), "create")
_rem = reminders(wd)[0]
check("提醒场落在时段起点 11:00",
      [(_rem.get("session") or {}).get("start"),
       (_rem.get("session") or {}).get("duration")], ["11:00", 25])
check("layout 被切成 [25]", sp.get_layout(sp.load_layouts(), DAY, "T1"), [25])

print("== 只有晚场空着（早场已经过去）→ 提醒排在晚场起点 ==")
_late = dict(PERIOD, id="T3", start="22:15", end="23:40")
_morning = dict(PERIOD, id="T1", start="09:00", end="10:00")
wd, op = fresh(layout=None, periods=[_morning, _late],
               clock=datetime.datetime.combine(DAY, datetime.time(21, 20)))
check("容量只算晚场（85 分钟 → 2 count）",
      (wd.pending_plan() or {}).get("counts"), 2)
check("检查结果 = create", wd._plan_watchdog_check(), "create")
_rem = reminders(wd)[0]
check("提醒场在 22:15",
      [(_rem.get("session") or {}).get("start"),
       (_rem.get("session") or {}).get("duration")], ["22:15", 25])

print("== 时段已经开始、又还没切 → 从 anchor 之后切（13:15）==")
wd, op = fresh(layout=None,
               clock=datetime.datetime.combine(DAY, datetime.time(13, 5)))
check("容量 = 13:15–15:00 → 3 count",
      (wd.pending_plan() or {}).get("counts"), 3)
check("检查结果 = create", wd._plan_watchdog_check(), "create")
_rem = reminders(wd)[0]
check("提醒场从 anchor 之后开始（13:15）",
      [(_rem.get("session") or {}).get("start"),
       (_rem.get("session") or {}).get("duration")], ["13:15", 25])
check("layout 没被改（起点不在时段起点，走 extras）",
      sp.get_layout(sp.load_layouts(), DAY, "T1"), None)
check("一次性场写进当天排期",
      [e.get("start") for e in sp.extras_for_date(sp.load_extras(), DAY)
       if e.get("start") == "13:15"], ["13:15"])

print("== 提醒任务在、但没排上场次 → 只补排，不重复建 ==")
wd, op = fresh(layout="session T1 50")
_rem = wd.task_store.create({"title": pw.TITLE, "duration": 25},
                            source={"kind": pw.SOURCE_KIND})
check("检查结果 = schedule", wd._plan_watchdog_check(), "schedule")
check("排到最早空位 12:00",
      (wd.task_store.get(_rem["id"]).get("session") or {}).get("start"), "12:00")
check("再检查幂等", wd._plan_watchdog_check(), None)
check("任务库仍只有一条提醒",
      len([t for t in reminders(wd) if t["status"] == "active"]), 1)

print("== 提醒场被取消（绑定清空）→ 重排，不直接兜底 ==")
wd, op = fresh(layout="session T1 50")
wd._plan_watchdog_check()
_rem = reminders(wd)[0]
wd.task_store.assign(_rem["id"], {"date": ISO, "start": "", "duration": 0})
check("检查结果 = schedule", wd._plan_watchdog_check(), "schedule")
check_true("重新挂上了",
           bool((wd.task_store.get(_rem["id"]).get("session") or {}).get("start")))
check("没有走兜底（今天还没兜底）",
      (ledger().get("auto_planned") or {}).get(ISO), None)

print("== 提醒场过去 + 今天没容量 → 兜底记 empty，一天只跑一次 ==")
wd, op = fresh(layout="session T1 50")
wd._plan_watchdog_check()
_rem = reminders(wd)[0]
wd.task_store.assign(_rem["id"], {"date": ISO, "start": "08:00", "duration": 25})
wd.clock_now = datetime.datetime.combine(DAY, datetime.time(12, 30))
wd._plan_slots = lambda day, **_kw: ([], [])
_detail = wd._plan_watchdog_check()
check_true("兜底真的跑了", isinstance(_detail, dict))
check("兜底结果 = empty",
      (ledger().get("auto_planned") or {}).get(ISO, {}).get("status"), "empty")
check("提醒任务退休", wd.task_store.get(_rem["id"])["status"], "completed")
check("再检查不再动", wd._plan_watchdog_check(), None)

print("== 条件表：契约表正下方，今天/明天都能看 ==")
wd, op = fresh(layout=None)
_rows = wd.condition_rows()
check("规则条数（5 条：2 formatter + plan + 2 watchdog）", len(_rows), 5)
_by_key = {row["key"]: row for row in _rows}
check("今天 formatter end 是无条件添加",
      _by_key["formatter.end"]["ok"], True)
check("今天没 plan + 有容量 → 提醒条件满足",
      _by_key["plan_watchdog.reminder"]["ok"], True)
check("今天没分配 → plan 条件不满足",
      _by_key["plan.complete"]["ok"], False)
_out_io = io.StringIO()
Console(file=_out_io, width=220, force_terminal=False).print(
    wd.render_conditions_table())
_out = _out_io.getvalue()
check_true("表里有标题", "自动添加条件" in _out)
check_true("条件列用了 operator", "always" in _out and "∧" in _out)
check_true("不满足的原因写在说明里", "还没有分配 count" in _out)
wd.day_offset = 1
wd.rebuild_rows()
_by_key2 = {row["key"]: row for row in wd.condition_rows()}
check("看明天时 watchdog 规则不适用",
      _by_key2["plan_watchdog.reminder"]["applicable"], False)
check("明天表仍然有 formatter 行",
      _by_key2["formatter.enlist"]["ok"], True)

print("== 接线：refresh_warnings（每帧钩子）真的会触发检查 ==")
wd, op = fresh()
wd.offline = False                     # 生产路径：钩子只在非 offline 时自动跑
wd._plan_watchdog_at = 0.0
wd.refresh_warnings(force=True)
check("钩子触发了建任务", len([t for t in reminders(wd) if t["status"] == "active"]), 1)
wd._plan_watchdog_at = time.time()
wd.refresh_warnings(force=True)
check("60 秒内不重复跑", len([t for t in reminders(wd) if t["status"] == "active"]), 1)

print("== 日志：同一份 JSONL 里有 plan_watchdog 的事件 ==")
wd, op = fresh()
wd._plan_watchdog_check()
_log_path = os.path.join(os.environ["TI_LOG_DIR"], "%s.jsonl" % DAY.isoformat())
_events = []
try:
    with open(_log_path, encoding="utf-8") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("area") == "plan_watchdog":
                _events.append(rec)
except OSError:
    pass
check_true("有 check 事件", any(e.get("event") == "check" for e in _events))
check_true("有 create 事件", any(e.get("event") == "create" for e in _events))
check_true("有 scheduled 事件", any(e.get("event") == "scheduled" for e in _events))

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    raise SystemExit(1)
print("全部通过")
