#!/usr/bin/env python3
"""plan 的任务级固定项（``task <任务> <count>``）与 ``task -p/-ps/-pi`` 改归属回归。

这两条是 2026-11-17 用户定的新语法：

* ``pro <序号> <count>`` 给项目分资源（旧 ``task <序号> <count>`` 的语义搬过来）；
* ``task <任务> <count>`` 把**任意一条任务**当作「只有一个任务的独立项目」排；
  序号按当前界面：契约 = 任务面板序号，数据 = 任务库内部 id（``id:N`` 永远认 id）；
* ``task <任务> -p <pro序号>`` 改归属，写回 OmniFocus、放目标项目第一行；
  ``-ps`` / ``-pi`` 是 ``--project sop|incubator`` 的缩写（策略模式，可越过阶段门控）。

用假 OmniFocus 记录 ``move_task`` 调用，绝不碰真库、不碰真 OmniFocus。
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-pins-test-")

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
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()

sp.save_download({
    "version": 1, "generated_at": "x", "start_date": TODAY.isoformat(), "days": 8,
    "profiles": ["工作"], "events": [],
    "periods": [
        {"id": "T1", "date": TOMORROW, "start": "11:00", "end": "13:00",
         "type": "工作", "name": "SAT 准备", "calendar": "工作", "title": "SAT 准备"},
        {"id": "T2", "date": TOMORROW, "start": "15:00", "end": "16:00",
         "type": "工作", "name": "托福 准备", "calendar": "工作", "title": "托福 准备"},
    ]})

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


def texts(wd):
    return [m[2] for m in wd.messages]


ONROWS = [
    {"id": "p-lunwen", "name": "论文", "kind": "project_root", "project": "",
     "tags": ["project/论文"], "completed": False, "dropped": False},
    {"id": "t-intro", "name": "写引言", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 50, "completed": False},
    {"id": "t-data", "name": "跑数据", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 75, "completed": False},
    {"id": "t-read", "name": "读文献", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 25, "completed": False},
]


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "add_task":
            return {"verified": True, "taskId": "ft-1", "task": {"id": "ft-1"}}
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
    """只记录调用，不碰真 OmniFocus。"""

    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows if rows is not None else ONROWS)]
        self.moves = []
        self.calls = []

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        self.calls.append(("list", refresh))
        return [dict(r) for r in self.rows]

    def get(self, pk, refresh=False):
        return next((dict(r) for r in self.rows
                     if str(r.get("id")) == str(pk)), None)

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def move_task(self, pk, project_pk, position="first", yes=True, timeout=None):
        self.moves.append((str(pk), str(project_pk), str(position)))
        return {"ok": True, "changed": ["project"]}

    def invalidate(self):
        pass


def reset_tasks():
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])


def reset_projects():
    pj.save(pj.blank())


def mkwarden(rows=None):
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus(rows)
    return wd


def fresh(rows=None):
    reset_tasks()
    reset_projects()
    return mkwarden(rows)


def auto_day(wd):
    """看板切到明天：时段没有 layout → plan 自己决定切分（auto 段）。"""
    wd.handle("day 1")
    return wd


def plan_day(wd):
    """明天 + T1 75/25 + T2 50（手动 layout，3 场 = 6 count），全部订上。"""
    wd.handle("day 1")
    wd.handle("session T1 75 25")
    wd.handle("session T2 50")
    plan = sch.day_slots(wd.entries, wd.sel_date)
    wd.api_rows = []
    for slot in plan:
        hour, minute = (int(x) for x in slot["start"].split(":"))
        start = datetime.datetime.combine(wd.sel_date, datetime.time(hour, minute))
        end = start + datetime.timedelta(minutes=int(slot["duration"]))
        wd.api_rows.append({
            "startMs": int(start.timestamp() * 1000),
            "endMs": int(end.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": end.strftime("%H:%M"),
            "durationMin": int(slot["duration"]), "id": "sess-" + slot["start"],
            "title": "", "quiet": False, "matched": False,
            "phase": wd.phase_of(int(start.timestamp() * 1000),
                                 int(end.timestamp() * 1000),
                                 int(datetime.datetime.now().timestamp() * 1000)),
        })
    wd.rows_source = "bridge"
    wd.rebuild_rows()
    return wd


def mk_task(wd, title, minutes):
    wd.handle("task create title=%s duration=%d" % (title, minutes))
    return next(t for t in wd.task_store.list()
                if (t.get("fields") or {}).get("title") == title)


def csv_rows(day=None):
    day = day or wd.sel_date
    out = []
    try:
        with open(sch.DATA_CSV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split(",")]
                if len(parts) < 3 or parts[0] != day.isoformat():
                    continue
                out.append((parts[1], int(parts[2])))
    except (OSError, ValueError):
        pass
    return out


def layout_of(day=None):
    day = day or wd.sel_date
    store = sp.load_layouts() or {}
    return sp.get_layout_record(store, day, "T1") or {}


def pins_file():
    try:
        with open(os.environ["TI_PLAN_PINS"], encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def ledger(day=None):
    day = day or wd.sel_date
    return pins_file().get(day.isoformat()) or []


def session_of(wd, task):
    return ((wd.task_store.get(task["id"]) or {}).get("session") or {})


print("== 1) plan 里安排单独任务：不属于任何项目的一条任务 ==")
wd = fresh([])
solo = mk_task(wd, "独立任务", 50)
auto_day(wd)
check_true("plan 打开", wd.handle("plan +1") is not None)
planner = wd.planning
check("容量 6 count（T1 4 + T2 2）", planner.cap()["counts"], 6)
no = wd.task_no(solo, day=wd.sel_date)
check_true("独立任务在契约任务面板上有序号", no is not None)
wd.handle("task %d 2" % no)
check("固定项进了 pins（1 条）", len(planner.pins), 1)
pin = planner.pin_of(solo["id"]) or {}
check("固定项标题来自 fields.title", pin.get("title"), "独立任务")
check("固定项 count = 2", pin.get("count"), 2)
check("固定项没有项目（独立任务）", pin.get("project_id"), None)
check("已分配 = 2 / 6", planner.allocated(), 2)
check_true("面板显示固定任务那一段",
           any("固定任务 1 条" in line for line, _style in planner.lines()))
wd.handle("complete")
sess = session_of(wd, solo)
check("任务真的挂上了 11:00 这一场", sess.get("start"), "11:00")
check("场次按 2 count 切成 50 分钟", sess.get("duration"), 50)
check("data.csv 里就是这一场", csv_rows(), [("11:00", 50)])
check("layout 重切成 [50]、来源 plan",
      (layout_of().get("durations"), layout_of().get("source")), ([50], "plan"))
check("没有新建第二条任务（复用原任务）",
      len([t for t in wd.task_store.list()
           if (t.get("fields") or {}).get("title") == "独立任务"]), 1)
led = ledger()
check("固定项台账记了 1 条", len(led), 1)
check("台账里的任务 id 对得上", led[0].get("task_id"), solo["id"])
check("台账里的起点对得上", led[0].get("start"), "11:00")
check("台账里的 duration 是场次长度", led[0].get("duration"), 50)
check("项目库没有把固定项写进 plans",
      [op.get("title") for op in wd.projects.active()
       if (pj.plan_of(op, wd.sel_date) or {}).get("items")], [])

print("\n== 2) 序号口径：契约 = 任务面板序号；数据 = 任务库内部 id ==")
# 面板顺序：未挂 session 的在前。把甲挂到第 1 场，面板 #1 就变成乙（id 2），
# 于是「面板第 1 条」和「内部 id 1」指向两条不同的任务 —— 这才有歧义可测。
wd = fresh([])
first = mk_task(wd, "甲", 50)
second = mk_task(wd, "乙", 50)
third = mk_task(wd, "丙", 50)
plan_day(wd)
wd.handle("task id:%d -a 1" % first["id"])
check("甲已经挂到第 1 场", (wd.task_store.get(first["id"]) or {}).get("session", {}).get("start"),
      "11:00")
wd.handle("plan +1")
planner = wd.planning
check("面板第 1 条是乙（甲已挂场次，排到后面）",
      wd.task_no(second, day=wd.sel_date), 1)
check_true("甲的 id 与它的面板号错开（制造歧义）",
           wd.task_no(first, day=wd.sel_date) != first["id"])
wd.handle("task 1 1")
check("契约视图按**面板序号**命中乙",
      (planner.pin_of(second["id"]) or {}).get("count"), 1)
check("没有误中内部 id 为 1 的甲", planner.pin_of(first["id"]), None)
wd.handle("q")

# 数据视图：同一个数字按内部 id 解析
wd = fresh([])
first = mk_task(wd, "甲", 50)
second = mk_task(wd, "乙", 50)
third = mk_task(wd, "丙", 50)
plan_day(wd)
wd.handle("task id:%d -a 1" % first["id"])
auto_day(wd)
wd.shell.view_index = next(
    i for i, view in enumerate(wd.view_impls) if isinstance(view, w.DataView))
check("已经切到数据视图", type(wd.view).__name__, "DataView")
wd.handle("plan +1")
planner = wd.planning
wd.handle("task %d 1" % second["id"])
check("数据视图按**内部 id** 命中乙（面板号此时不是它）",
      (planner.pin_of(second["id"]) or {}).get("count"), 1)
check("没有按面板序号命中丙", planner.pin_of(third["id"]), None)
wd.handle("q")

# id:N 永远按内部 id（面板上找不到也能点名）
wd = fresh([])
first = mk_task(wd, "甲", 50)
wd.handle("task id:%d -m complete" % first["id"])
auto_day(wd)
wd.handle("plan +1")
planner = wd.planning
wd.handle("task id:%d 1" % first["id"])
check_true("id: 形式点名已收工的任务会被拒绝（收工/投满），而不是挂错人",
           any(("已经收工" in t) or ("投满" in t) for t in texts(wd)))
check("被拒绝时不产生固定项", planner.pins, [])
wd.handle("q")

print("\n== 3) 重新 plan / plan clear 必须释放固定项 ==")
wd = fresh()
solo = mk_task(wd, "独立任务", 50)
auto_day(wd)
wd.handle("plan +1")
wd.handle("task %d 2" % wd.task_no(solo, day=wd.sel_date))
wd.handle("complete")
check("第一轮挂上了", session_of(wd, solo).get("start"), "11:00")

wd.handle("op import sop omnifocus:///project/p-lunwen")
auto_day(wd)
wd.handle("plan +1")
check_true("第二轮 plan 打开", wd.planning is not None)
wd.handle("pro 1 2")
wd.handle("complete")
# 2026-11-21 起：资源决定（含固定项）跨 plan 保留，重新打开会预填；
# 只有 `task <任务> clear` / `plan clear` 才解绑。
check_true("第二轮保留第一轮的固定项（决定预填）", bool(session_of(wd, solo)))
check("台账里还有那条固定项", len(ledger()), 1)
check("独立任务还在库里（解绑 ≠ 丢任务）",
      len([t for t in wd.task_store.list()
           if (t.get("fields") or {}).get("title") == "独立任务"]), 1)

auto_day(wd)
wd.handle("plan +1")
wd.handle("task %d 2" % wd.task_no(solo, day=wd.sel_date))
wd.handle("complete")
check("固定项可以重新用", session_of(wd, solo).get("start"), "11:00")
check("任务仍然只有一条",
      len([t for t in wd.task_store.list()
           if (t.get("fields") or {}).get("title") == "独立任务"]), 1)

wd.handle("day 1")
wd.handle("plan clear")
check("plan clear 之后固定项解绑", session_of(wd, solo), {})
check("plan clear 之后台账清空", ledger(), [])

print("\n== 4) 预算与冲突 ==")
wd = fresh([])
big = mk_task(wd, "大任务", 250)             # 10 count，不会被剩余量截断
other = mk_task(wd, "另一条", 250)
auto_day(wd)
wd.handle("plan +1")
planner = wd.planning
no_big = wd.task_no(big, day=wd.sel_date)
no_other = wd.task_no(other, day=wd.sel_date)
wd.handle("task %d 4" % no_big)
check("先固定 4 count", (planner.pin_of(big["id"]) or {}).get("count"), 4)
wd.handle("task %d 3" % no_other)
check("再加 3 count 超过容量 → 整条不执行（第二条没进来）",
      planner.pin_of(other["id"]), None)
check("已分配还是 4", planner.allocated(), 4)
check_true("说了「超预算」", any("超预算" in t for t in texts(wd)))
check_true("说了「这条命令没执行」",
           any("这条命令没执行" in t for t in texts(wd)))
wd.handle("task %d 2" % no_other)
check("放得下（4 + 2 = 6）才进来",
      (planner.pin_of(other["id"]) or {}).get("count"), 2)
wd.handle("task %d clear" % no_big)
check("clear 撤掉第一条的固定项", planner.pin_of(big["id"]), None)
check("已分配回到 2", planner.allocated(), 2)
wd.handle("task %d clear" % no_other)
check("再 clear 回 0", planner.allocated(), 0)
wd.handle("q")

# 已挂在场次上的任务不能再固定
wd = fresh([])
busy = mk_task(wd, "已在场上的任务", 50)
auto_day(wd)
wd.handle("plan +1")
wd.handle("task %d 2" % wd.task_no(busy, day=wd.sel_date))
wd.handle("complete")
auto_day(wd)
wd.handle("plan +1")
_before_pins = len(wd.planning.pins)
wd.handle("task %d 1" % wd.task_no(busy, day=wd.sel_date))
check_true("已挂场次的任务被拒绝", any("已经挂在" in t for t in texts(wd)))
check("被拒绝时没有新增固定项", len(wd.planning.pins), _before_pins)
wd.handle("q")

# 已经收工/投满的任务不能固定
wd = fresh([])
done = mk_task(wd, "投满的任务", 25)          # 1 count
wd.handle("task id:%d -c +1" % done["id"])      # 进度 +1 → 投满并完成
auto_day(wd)
wd.handle("plan +1")
wd.handle("task id:%d 1" % done["id"])
check_true("收工/投满的任务被拒绝",
           any(("已经收工" in t) or ("投满" in t) for t in texts(wd)))
check("被拒绝时没有固定项", wd.planning.pins, [])
wd.handle("q")

print("\n== 5) 固定项优先：同一条项目清单行不会出现两份 ==")
wd = fresh()
wd.handle("op import sop omnifocus:///project/p-lunwen")
auto_day(wd)
wd.handle("plan +1")
planner = wd.planning
mirror = next(t for t in wd.task_store.list()
              if (t.get("fields") or {}).get("project_task") == "t-data")
wd.handle("pro 1 2")
wd.handle("task id:%d 2" % mirror["id"])
check("项目分 2 + 任务固定 2 = 4 count", planner.allocated(), 4)
check("固定项记着它属于哪个项目",
      (planner.pin_of(mirror["id"]) or {}).get("project_title"), "论文")
wd.handle("complete")
copies = [t for t in wd.task_store.list()
          if (t.get("fields") or {}).get("project_task") == "t-data"]
check("同一条清单行只有一份", len(copies), 1)
bound = sorted((t.get("fields") or {}).get("title") for t in wd.task_store.list()
               if (t.get("session") or {}).get("start"))
check("固定项 + 项目摊派各排上一条", bound, ["写引言", "跑数据"])

print("\n== 6) task -p / -ps / -pi：改归属写回 OmniFocus（假 client）==")
MOVE_ROWS = [
    {"id": "p-sop", "name": "SOP 流", "kind": "project_root", "project": "",
     "tags": ["project/甲"], "completed": False, "dropped": False},
    {"id": "t-sop-1", "name": "SOP 里的活", "kind": "action", "project": "SOP 流",
     "parent": "SOP 流", "estimated_minutes": 50, "completed": False},
    {"id": "p-inc", "name": "孵化流", "kind": "project_root", "project": "",
     "tags": ["project/甲"], "completed": False, "dropped": False},
    {"id": "t-inc-1", "name": "孵化里的活", "kind": "action", "project": "孵化流",
     "parent": "孵化流", "estimated_minutes": 25, "completed": False},
]
wd = fresh(MOVE_ROWS)
wd.handle("op import sop omnifocus:///project/p-sop")
wd.handle("op import incubator omnifocus:///project/p-inc")
ops = {op["title"]: op for op in wd.projects.active()}
mirror = next(t for t in wd.task_store.list()
              if (t.get("fields") or {}).get("project_task") == "t-sop-1")
check("镜像任务原本属于 SOP 项目",
      (mirror.get("fields") or {}).get("project"), "SOP 流")
wd.omnifocus_enabled = lambda: True


def op_pk(title):
    return (ops[title].get("omnifocus") or {}).get("id")


wd.handle("task %d -pi 1" % mirror["id"])
check("move_task 被调用一次（pk / 目标 op / 第一行）",
      wd.omnifocus.moves, [("t-sop-1", op_pk("孵化流"), "first")])
cur = wd.task_store.get(mirror["id"])
check("本地 project 字段改了",
      (cur.get("fields") or {}).get("project"), "孵化流")
check("本地 project_id 改了",
      (cur.get("fields") or {}).get("project_id"), ops["孵化流"]["id"])
src_pks = [x.get("pk") for x in
           (wd.projects.by_id(ops["SOP 流"]["id"]) or {}).get("tasks") or []]
dst_pks = [x.get("pk") for x in
           (wd.projects.by_id(ops["孵化流"]["id"]) or {}).get("tasks") or []]
check("源项目清单里摘掉了", "t-sop-1" in src_pks, False)
check("目标项目清单第一行是它", dst_pks[:1], ["t-sop-1"])

wd.omnifocus.moves = []
wd.handle("task %d -pi 1" % mirror["id"])
check("已经在目标项目里 → 不调 move_task", wd.omnifocus.moves, [])
check_true("明说「已经在项目里」",
           any("已经在项目" in t for t in texts(wd)))

wd.omnifocus.moves = []
wd.handle("task %d -ps 1" % mirror["id"])
check("-ps 走 SOP op", wd.omnifocus.moves,
      [("t-sop-1", op_pk("SOP 流"), "first")])

wd.omnifocus.moves = []
wd.handle("task %d -pi 1" % mirror["id"])       # 先挪回孵化侧
wd.omnifocus.moves = []
wd.handle("task %d -p 1" % mirror["id"])
check("-p 不写模式 = 当前阶段 op（running → SOP）", wd.omnifocus.moves,
      [("t-sop-1", op_pk("SOP 流"), "first")])

print("\n== 7) 语法解析与拒绝路径 ==")
check("-ps 2 ≡ --project sop 2", wd._split_project(["-ps", "2"]),
      ({"mode": "sop", "target": "2"}, []))
check("-pi 2 ≡ --project incubator 2", wd._split_project(["-pi", "2"]),
      ({"mode": "incubator", "target": "2"}, []))
check("--project 2 = 当前阶段", wd._split_project(["5", "--project", "2"]),
      ({"mode": "current", "target": "2"}, ["5"]))
check("--project=sop 2 同义", wd._split_project(["5", "--project=sop", "2"]),
      ({"mode": "sop", "target": "2"}, ["5"]))
check("别的后缀原样保留", wd._split_project(["5", "-p", "2", "-c", "+2"]),
      ({"mode": "current", "target": "2"}, ["5", "-c", "+2"]))

wd.omnifocus.moves = []
solo2 = mk_task(wd, "纯本地任务", 50)
wd.handle("task %d -p 1" % solo2["id"])
check("没有 OmniFocus pk 的本地任务被拒绝", wd.omnifocus.moves, [])
check_true("拒绝理由说清没有 OmniFocus 关联",
           any("没有 OmniFocus 关联" in t for t in texts(wd)))

wd.omnifocus.moves = []
wd.omnifocus_enabled = lambda: False
before = dict(wd.task_store.get(mirror["id"]).get("fields") or {})
wd.handle("task %d -pi 1" % mirror["id"])       # 目标 != 当前项目，才会走到 enabled 检查
check("OmniFocus 关着时不动远端", wd.omnifocus.moves, [])
check("OmniFocus 关着时不动本地字段",
      (wd.task_store.get(mirror["id"]).get("fields") or {}).get("project"),
      before.get("project"))
check_true("拒绝理由说清 OmniFocus 关着",
           any("OmniFocus 关着" in t for t in texts(wd)))

print()
if fails:
    print("FAILED %d 项：" % len(fails))
    for item in fails:
        print(" - %s" % item)
    sys.exit(1)
print("全绿。")
