#!/usr/bin/env python3
"""stage4：二次 plan 的资源决定持久化 / 面板预填 / complete 幂等（C1–C4、C6、C7）。

用户口径（2026-11-20）：第一次 plan + complete 之后，第二次打开要
①保持原本的计划；②第一次指定的项目像刚指定那样「变亮」；③pro clear 能回收
count；④complete 幂等；只有 plan clear 才是真清空。
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
TMP = tempfile.mkdtemp(prefix="plan-decision-test-")

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
                  ("TI_PLAN_DECISIONS", "plan-decisions.json"),
                  ("TI_PLAN_WATCHDOG", "plan-watchdog.json"),
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


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
        return {"verified": True, "session": None}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


ONROWS = [
    {"id": "p-lunwen", "name": "论文", "kind": "project_root", "project": "",
     "tags": ["project/论文"], "completed": False, "dropped": False},
    {"id": "t-intro", "name": "写引言", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 50, "completed": False},
    {"id": "t-data", "name": "跑数据", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 75, "completed": False},
    {"id": "t-read", "name": "读文献", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 25, "completed": False},
    {"id": "p-other", "name": "杂事", "kind": "project_root", "project": "",
     "tags": ["project/杂事"], "completed": False, "dropped": False},
    {"id": "t-errand1", "name": "买纸巾", "kind": "action", "project": "杂事",
     "parent": "杂事", "estimated_minutes": 25, "completed": False},
    {"id": "t-errand2", "name": "买洗衣液", "kind": "action", "project": "杂事",
     "parent": "杂事", "estimated_minutes": 25, "completed": False},
]


class FakeOmniFocus(object):
    def __init__(self):
        self.rows = [dict(r) for r in ONROWS]

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        return [dict(r) for r in self.rows]

    def get(self, pk, refresh=False):
        return next((dict(r) for r in self.rows if r["id"] == pk), None)

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def move_task(self, pk, project_pk, position="first", yes=True, timeout=None):
        return {"ok": True, "changed": ["project"]}

    def invalidate(self):
        pass


def fresh():
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    pj.save(pj.blank())
    sp.save_layouts(sp.empty_layouts())
    sp.save_extras(sp.empty_extras())
    open(sch.DATA_CSV, "w", encoding="utf-8").write("")
    sp.save_download({"version": 1, "generated_at": "x",
                      "start_date": TODAY.isoformat(), "days": 8,
                      "profiles": ["工作"], "events": [],
                      "periods": [
                          {"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
                           "type": "工作", "name": "SAT", "calendar": "工作",
                           "title": "SAT"},
                          {"id": "T2", "date": ISO, "start": "15:00", "end": "16:00",
                           "type": "工作", "name": "托福", "calendar": "工作",
                           "title": "托福"}]})
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd._suppress_book = True
    wd.omnifocus = FakeOmniFocus()
    wd.reload_periods()
    wd.reload_entries()
    wd.api_rows = []
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    wd.rebuild_rows()
    return wd


def snapshot(wd):
    iso = wd.sel_date.isoformat()
    lay = sp.load_layouts()
    recs = ((lay.get("layouts") or {}).get(iso) or {})
    plans = {}
    for op in wd.projects.active():
        plan = pj.plan_of(op, wd.sel_date) or {}
        plans[op["title"]] = (
            plan.get("count"),
            [(i.get("pk"), i.get("task_id"), i.get("count"), i.get("start"))
             for i in plan.get("items") or []])
    bound = sorted((t["id"], (t.get("fields") or {}).get("title"),
                    (t.get("session") or {}).get("start"),
                    (t.get("session") or {}).get("duration"))
                   for t in wd.task_store.list()
                   if str((t.get("session") or {}).get("date") or "") == iso)
    rows = [line for line in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
            if line.startswith(iso)]
    return {"layouts": {k: v.get("durations") for k, v in recs.items()},
            "plans": plans, "bound": bound, "csv": rows}


def decision():
    try:
        with open(os.environ["TI_PLAN_DECISIONS"], encoding="utf-8") as fh:
            return json.load(fh).get(ISO) or {}
    except (OSError, ValueError):
        return {}


def texts(wd):
    return [m[2] for m in wd.messages]


def plan_open(wd, offset="+1"):
    wd.handle("day 1")
    wd.handle("plan %s" % offset)
    return wd.planning


print("== C1) plan→complete→plan→complete：预填 + 复现，完全一致 ==")
wd = fresh()
wd.handle("op import sop omnifocus:///project/p-lunwen")
planner = plan_open(wd)
wd.handle("pro 1 4")
wd.handle("complete")
first = snapshot(wd)
planner = plan_open(wd)
check("第二次打开已分配 4", planner.allocated(), 4)
check("载入了上一轮决定", planner.had_decision, True)
check_true("项目行高亮（像刚指定）",
           any("bold green" in str(style) and "论文" in line
               for line, style in planner.lines()))
wd.handle("complete")
second = snapshot(wd)
check("两轮快照完全一致", first, second)
check("决定台账 alloc 4", (decision().get("alloc") or {}).get(
    str(wd.projects.active()[0]["id"])), 4)

print("\n== C2) 第二轮把 4 改成 2：保留同一条任务、count 变 2 ==")
old_bound = list(first["bound"])
planner = plan_open(wd)
wd.handle("pro 1 2")
wd.handle("complete")
third = snapshot(wd)
check("项目 count 变 2", third["plans"]["论文"][0], 2)
check("条目 count 不超过 2",
      sum(int(i[2] or 0) for i in third["plans"]["论文"][1]) <= 2, True)
check_true("任务 id 来自第一轮（任务集稳定）",
           all(b[0] in {x[0] for x in old_bound} for b in third["bound"]))

print("\n== C3) pro clear 回收：count 归 0、绑定清空、再打开不亮 ==")
planner = plan_open(wd)
wd.handle("pro 1 clear")
check("clear 后已分配 0", planner.allocated(), 0)
wd.handle("complete")
fourth = snapshot(wd)
check("绑定清空", fourth["bound"], [])
check("项目不再有 count", fourth["plans"]["论文"][0], None)
check("决定台账 count 0",
      (decision().get("alloc") or {}).get(str(wd.projects.active()[0]["id"])), 0)
planner = plan_open(wd)
check("再打开已分配 0", planner.allocated(), 0)
check("没有项目行高亮",
      [line for line, style in planner.lines() if "bold green" in str(style)
       and "#" in line], [])
wd.handle("q")

print("\n== C4) 两个项目：A 4 + B 2，二次打开都亮，A 任务 id 不变 ==")
wd = fresh()
wd.handle("op import sop omnifocus:///project/p-lunwen")
wd.handle("op import sop omnifocus:///project/p-other")
planner = plan_open(wd)
wd.handle("pro 1 4")
wd.handle("pro 2 2")
wd.handle("complete")
base = snapshot(wd)
ids_a = {i[1] for i in base["plans"]["论文"][1]}
planner = plan_open(wd)
check("A 预填 4 / B 预填 2", [planner.allocation_of(p["id"])
                              for p in planner.projects], [4, 2])
check_true("两行都高亮",
           sum(1 for line, style in planner.lines()
               if "bold green" in str(style) and "#" in line) >= 2)
wd.handle("complete")
again = snapshot(wd)
check("两轮一致", base, again)
check("A 的任务 id 不变", {i[1] for i in again["plans"]["论文"][1]}, ids_a)
check("B 仍然有安排", bool(again["plans"]["杂事"][1]), True)

print("\n== C6) 已完成的任务不再进计划，用队列里的新任务补位 ==")
# 完成第一轮里论文的一条任务，再 plan 同分配
first_task_id = base["plans"]["论文"][1][0][1]
wd.handle("task id:%d -m complete" % first_task_id)
planner = plan_open(wd)
wd.handle("complete")
after = snapshot(wd)
planned_ids = {i[1] for i in after["plans"]["论文"][1]}
check("已完成的任务不在计划里", first_task_id in planned_ids, False)
check_true("论文仍有排期（用队列里的下一条补位）",
           len(after["plans"]["论文"][1]) > 0)

print("\n== C7) plan clear 才是真清空：决定也没了 ==")
wd.handle("day 1")
wd.handle("plan clear")
check("决定台账被清掉", decision(), {})
planner = plan_open(wd)
check("再打开已分配 0", planner.allocated(), 0)
check("had_decision 为 False", planner.had_decision, False)
wd.handle("q")

print("\n" + ("全部通过" if not fails else "失败 %d 条" % len(fails)))
if fails:
    for f in fails:
        print(" - " + f)
    sys.exit(1)
