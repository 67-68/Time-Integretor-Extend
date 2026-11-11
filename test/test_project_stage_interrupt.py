#!/usr/bin/env python3
"""项目阶段单次 plan / 契约显示 / 硬中断回归。

    python3 test/test_project_stage_interrupt.py

覆盖：
- plan 只收「关联真正项目 pro」的 op；每个 pro 只取当前阶段的 SOP/incubator；
- 同一次 plan 处理所有项目的当前 op（不再 SOP 排完再 incubator）；
- 契约项目表与 project 列表只显示当前活跃 op；
- project <序号> interrupt --hard 下掉当前场任务，再按 title 建整场任务挂回去。
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


TMP = tempfile.mkdtemp(prefix="project-stage-test-")
for key, name in (
        ("TI_CALENDAR_DOWNLOAD", "download.json"),
        ("TI_SESSION_LAYOUTS", "layouts.json"),
        ("TI_SESSION_EXTRAS", "extras.json"),
        ("TI_SESSION_RULES", "rules.json"),
        ("TI_SESSION_MUTES", "mutes.json"),
        ("TI_SETTINGS_FILE", "settings.json"),
        ("TI_TASKS_FILE", "tasks.json"),
        ("TI_TASK_ARCHIVE", "archive.json"),
        ("TI_PROJECTS_FILE", "projects.json"),
        ("TI_NOSTART_FILE", "nostart.json"),
        ("TI_POSTSESSION_FILE", "postsession.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, pj, ks = w.sp, w.sch, w.ts, w.pj, w.ks
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
ks.notify = lambda *a, **k: None

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
TOMORROW = TODAY + datetime.timedelta(days=1)
T_ISO = TOMORROW.isoformat()
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作"], "events": [],
                  "periods": [
                      {"id": "T1", "date": T_ISO, "start": "11:00", "end": "13:00",
                       "type": "工作", "name": "T1", "calendar": "工作", "title": "T1"},
                      {"id": "T2", "date": T_ISO, "start": "15:00", "end": "16:00",
                       "type": "工作", "name": "T2", "calendar": "工作", "title": "T2"},
                  ]})


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "sessions": []}
        if cmd == "add_task":
            return {"verified": True, "taskId": "ft", "task": {"id": "ft"}}
        if cmd == "tasks":
            return {"tasks": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmniFocus(object):
    def __init__(self, rows):
        self.rows = rows

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None):
        return [dict(r) for r in self.rows]

    def get(self, pk, refresh=False):
        return next((dict(r) for r in self.rows if str(r.get("id")) == str(pk)), None)

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def complete(self, *a, **k):
        return {"after": {"completed": True}}

    def drop(self, *a, **k):
        return {"after": {"dropped": True}}

    def invalidate(self):
        pass


ROWS = [
    {"id": "p-run", "name": "运行 SOP 旧", "kind": "project_root",
     "tags": ["project/运行项目"], "completed": False, "dropped": False},
    {"id": "t-run-old", "name": "旧 SOP 任务", "kind": "action", "project": "运行 SOP 旧",
     "project_id": "p-run", "estimated_minutes": 25, "completed": False},
    {"id": "p-run-new", "name": "运行 SOP 新", "kind": "project_root",
     "tags": ["project/运行项目"], "completed": False, "dropped": False},
    {"id": "t-run-new", "name": "当前 SOP 任务", "kind": "action", "project": "运行 SOP 新",
     "project_id": "p-run-new", "estimated_minutes": 25, "completed": False},
    {"id": "p-inc", "name": "孵化项目", "kind": "project_root",
     "tags": ["project/孵化项目"], "completed": False, "dropped": False},
    {"id": "t-inc-sop", "name": "孵化项目的旧 SOP", "kind": "action", "project": "孵化项目",
     "project_id": "p-inc", "estimated_minutes": 25, "completed": False},
    {"id": "p-inc-inc", "name": "孵化 Inc", "kind": "project_root",
     "tags": ["project/孵化项目"], "completed": False, "dropped": False},
    {"id": "t-inc-inc", "name": "当前孵化任务", "kind": "action", "project": "孵化 Inc",
     "project_id": "p-inc-inc", "estimated_minutes": 25, "completed": False},
    {"id": "p-orphan", "name": "无 pro 的孤立 op", "kind": "project_root",
     "tags": [], "completed": False, "dropped": False},
    {"id": "t-orphan", "name": "孤立任务", "kind": "action", "project": "无 pro 的孤立 op",
     "project_id": "p-orphan", "estimated_minutes": 25, "completed": False},
]


def mkwarden():
    console = Console(file=io.StringIO(), width=220, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus(ROWS)
    return wd


def texts(wd):
    return [m[2] for m in wd.messages]


def reset():
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())


def book_all(wd):
    wd.handle("day 1")
    wd.handle("session T1 50 25")
    wd.handle("session T2 50")
    plan = sch.day_slots(wd.entries, wd.sel_date)
    wd.api_rows = []
    for slot in plan:
        h, m = map(int, slot["start"].split(":"))
        st = datetime.datetime.combine(wd.sel_date, datetime.time(h, m))
        en = st + datetime.timedelta(minutes=int(slot["duration"]))
        wd.api_rows.append({
            "startMs": int(st.timestamp() * 1000), "endMs": int(en.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": en.strftime("%H:%M"),
            "durationMin": int(slot["duration"]), "id": "sess-" + slot["start"],
            "title": "", "quiet": False, "matched": False, "phase": "",
        })
    wd.rows_source = "bridge"
    wd.rebuild_rows()


print("== 项目阶段：每个 pro 只有一个当前 op ==")
reset()
wd = mkwarden()
check("导入旧 SOP op", wd.handle("task import sop omnifocus:///project/p-run"), True)
check("导入新 SOP op", wd.handle("task import sop omnifocus:///project/p-run-new"), True)
check("导入孵化项目 SOP", wd.handle("task import sop omnifocus:///project/p-inc"), True)
check("切换到 bottleneck 并导入 incubator op",
      wd.handle("pro 2 -a task import omnifocus:///project/p-inc-inc"), True)
check("导入孤立 SOP op（没有 project/ 标签）",
      wd.handle("task import sop omnifocus:///project/p-orphan"), True)
_pros = wd.projects.active_pros()
check("两个真正项目", [(p["title"], p["stage"]) for p in _pros],
      [("运行项目", "running"), ("孵化项目", "bottleneck")])
check("运行项目当前只取新 SOP op",
      wd.projects.current_op_for_pro(_pros[0])["title"], "运行 SOP 新")
check("孵化项目当前只取 incubator op",
      wd.projects.current_op_for_pro(_pros[1])["title"], "孵化 Inc")
check("plan_ops 排除旧 SOP / 孤立 op，只留两个当前 op",
      [(op["title"], op["op_type"]) for op in wd.projects.plan_ops()],
      [("运行 SOP 新", "SOP"), ("孵化 Inc", "incubator")])

print("== 契约项目显示：只画当前活跃 op ==")
rows = wd.current_project_rows()
check("每个 pro 一行", [r["pro_title"] for r in rows], ["运行项目", "孵化项目"])
check("行上只出现当前 op",
      [(r["active_op_type"], r["active_op"]["title"]) for r in rows],
      [("SOP", "运行 SOP 新"), ("incubator", "孵化 Inc")])
_buf = io.StringIO()
wd.console = Console(file=_buf, width=220, force_terminal=False)
_proj_table = wd.render_projects_table()
check_true("契约里画出合并后的项目表", _proj_table is not None)
if _proj_table is not None:
    wd.console.print(_proj_table)
_table_out = _buf.getvalue()
check_true("项目表里同时有当前 SOP 与 incubator",
           "运行 SOP 新" in _table_out and "孵化 Inc" in _table_out)
check_true("项目表不画旧 SOP / 孤立 op",
           "运行 SOP 旧" not in _table_out and "无 pro 的孤立 op" not in _table_out)
wd.messages = []
check("project 列表受理", wd.handle("project"), True)
joined = "\n".join(texts(wd))
check_true("列表里有当前 SOP", "运行 SOP 新" in joined)
check_true("列表里有当前 incubator", "孵化 Inc" in joined)
check_true("列表不显示旧 SOP / 孤立 op",
           "运行 SOP 旧" not in joined and "无 pro 的孤立 op" not in joined)

print("== plan：一次处理所有项目当前阶段 ==")
book_all(wd)                                  # 明天 T1 50+25、T2 50 = 5 count
check("plan +1 受理", wd.handle("plan +1"), True)
check("同一次 plan 同时出现 SOP 与 incubator",
      [(p["title"], p.get("op_type")) for p in wd.planning.projects],
      [("运行 SOP 新", "SOP"), ("孵化 Inc", "incubator")])
check("没有全局阶段切换", wd.planning.stage, "mixed")
check("容量一次算全 5 count", wd.planning.cap()["counts"], 5)
wd.handle("task 1 1")
wd.handle("task 2 1")
check("complete 一次收尾", wd.handle("complete"), True)
check("planning 关闭", wd.planning, None)
check("两个当前 op 都排出了任务",
      sorted(t["fields"]["title"] for t in wd.task_store.list()
             if (t.get("session") or {}).get("start")),
      ["当前 SOP 任务", "当前孵化任务"])

print("== 硬中断：清空当前场 + 输入 title 后建整场任务 ==")
live_row = {"date": T_ISO, "startLabel": "11:00", "endLabel": "11:25",
            "durationMin": 25, "id": "sess-11:00", "startMs": 1}
wd.live_session = lambda: (1, live_row)
old1 = wd.task_store.create({"title": "旧任务 A", "duration": 25})
old2 = wd.task_store.create({"title": "旧任务 B", "duration": 25})
sess = {"index": 1, "date": T_ISO, "start": "11:00", "end": "11:25",
        "duration": 25, "focusmate_session_id": "sess-11:00"}
wd.task_store.assign(old1["id"], sess)
wd.task_store.assign(old2["id"], sess)
wd.messages = []
check("project 1 interrupt --hard 受理",
      wd.handle("project 1 interrupt --hard"), True)
check_true("进入等待 title 状态", wd.pending_interrupt is not None)
check("两个旧任务都已下架",
      [(wd.task_store.get(t["id"]).get("session") or {}).get("start")
       for t in (old1, old2)], [None, None])
check_true("打断栏位显示输入 title 的提示",
           wd.render_interrogate() is not None
           and "请输入新任务 title" in wd.render_interrogate().renderable.plain)
check("输入 title 后受理", wd.handle("硬中断的新任务"), True)
check("等待状态清空", wd.pending_interrupt, None)
new_tasks = [t for t in wd.task_store.list()
             if t["fields"].get("title") == "硬中断的新任务"]
check("新任务只有一条", len(new_tasks), 1)
new_task = new_tasks[0]
check("新任务长度 = 当前 session 长度",
      int(new_task["fields"].get("duration") or 0), 25)
check("新任务挂回当前 session",
      (new_task.get("session") or {}).get("start"), "11:00")
check("新任务是独占的",
      new_task["fields"].get("exclusive"), True)
check("旧任务仍在任务库（只是下架）",
      [wd.task_store.get(t["id"])["status"] for t in (old1, old2)],
      ["active", "active"])

print("== 硬中断：没有进行中的 session 时明确拒绝 ==")
wd.live_session = lambda: None


def _no_current_session():
    raise ValueError("现在没有正在进行的 session，也没有还没开始的已订场次")


wd.current_session = _no_current_session
wd.messages = []
check("interrupt 受理（错误进消息区）",
      wd.handle("project 1 interrupt --hard"), True)
check_true("明确说没有正在进行的 session",
           any("没有正在进行的 session" in t for t in texts(wd)))

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
