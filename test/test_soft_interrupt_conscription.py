#!/usr/bin/env python3
"""软中断（A/B/C + --check）与 cst 快速征召回归。"""
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


TMP = tempfile.mkdtemp(prefix="soft-cst-test-")
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
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作"], "events": [], "periods": []})


class FakeBridge(object):
    def __init__(self):
        self.seq = 0

    def call(self, cmd, timeout=240, **kw):
        if cmd == "add_task":
            self.seq += 1
            return {"verified": True, "taskId": "ft-%d" % self.seq,
                    "task": {"id": "ft-%d" % self.seq}, "via": "api"}
        if cmd == "tasks":
            return {"tasks": []}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


ROWS = [
    {"id": "p-alpha", "name": "Alpha SOP", "kind": "project_root",
     "tags": ["project/Alpha"], "completed": False, "dropped": False},
    {"id": "t-a-long", "name": "Alpha 长任务", "kind": "action",
     "project": "Alpha SOP", "project_id": "p-alpha",
     "estimated_minutes": 50, "completed": False},
    {"id": "t-a-20", "name": "Alpha 二十分钟", "kind": "action",
     "project": "Alpha SOP", "project_id": "p-alpha",
     "estimated_minutes": 20, "completed": False},
    {"id": "p-alpha-inc", "name": "Alpha Inc", "kind": "project_root",
     "tags": ["project/Alpha"], "completed": False, "dropped": False},
    {"id": "t-a-inc", "name": "Alpha 孵化任务", "kind": "action",
     "project": "Alpha Inc", "project_id": "p-alpha-inc",
     "estimated_minutes": 25, "completed": False},
    {"id": "p-beta", "name": "Beta SOP", "kind": "project_root",
     "tags": ["project/Beta"], "completed": False, "dropped": False},
    {"id": "t-b-20", "name": "Beta 二十分钟", "kind": "action",
     "project": "Beta SOP", "project_id": "p-beta",
     "estimated_minutes": 20, "completed": False},
    {"id": "p-errand", "name": "Errands", "kind": "project_root",
     "tags": ["project/跑腿"], "completed": False, "dropped": False},
    {"id": "t-e-5", "name": "跑腿五分钟", "kind": "action",
     "project": "Errands", "project_id": "p-errand",
     "estimated_minutes": 5, "completed": False},
]


class FakeOmniFocus(object):
    def __init__(self):
        self.rows = [dict(r) for r in ROWS]

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


def mkwarden():
    wd = w.Warden(Console(file=io.StringIO(), width=220, force_terminal=False),
                  FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus()
    return wd


def texts(wd):
    return [m[2] for m in wd.messages]


def reset():
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())


def import_projects(wd):
    assert wd.handle("task import sop omnifocus:///project/p-alpha")
    assert wd.handle("task import incubator omnifocus:///project/p-alpha-inc")
    assert wd.handle("task import sop omnifocus:///project/p-beta")
    assert wd.handle("task import sop omnifocus:///project/p-errand")


def set_live_session(wd, remaining_minutes, start_label="10:00"):
    now = datetime.datetime.now()
    row = {
        "date": now.date().isoformat(),
        "startLabel": start_label,
        "endLabel": (now + datetime.timedelta(minutes=remaining_minutes)).strftime("%H:%M"),
        "durationMin": remaining_minutes,
        "id": "sess-live",
        "startMs": int((now - datetime.timedelta(minutes=2)).timestamp() * 1000),
        "endMs": int((now + datetime.timedelta(minutes=remaining_minutes)).timestamp() * 1000),
        "title": "", "quiet": False, "matched": False, "phase": "",
    }
    wd.rows = [row]
    wd.api_rows = [row]
    wd.rows_source = "bridge"
    return row


def assign_alpha_context(wd, row, pks=("t-a-long",)):
    """在当前场放一条 Alpha 任务，作为 cst 的项目上下文。"""
    op = next(p for p in wd.projects.active()
              if (p.get("omnifocus") or {}).get("id") == "p-alpha")
    session = {"index": 1, "date": row["date"], "start": row["startLabel"],
               "end": row["endLabel"], "duration": row["durationMin"],
               "focusmate_session_id": row["id"]}
    made = []
    for pk in pks:
        item = pj.task_by_pk(op, pk)
        task = wd.task_store.create({
            "title": item.get("title") or pk,
            "duration": item.get("duration") or 25,
            "project": op.get("title") or "",
            "project_id": op["id"],
            "project_task": pk,
        })
        made.append(wd.task_store.assign(task["id"], session))
    return made


print("== 软中断：默认 soft，不下架任务；A 分类；--check 生成 incubator 任务 ==")
reset()
wd = mkwarden()
check_true("conscription / cst 已注册", wd.shell.registry.get("cst") is not None)
import_projects(wd)
pro = wd.projects.resolve_pro("1")
op_inc = next(p for p in wd.projects.active()
              if (p.get("omnifocus") or {}).get("id") == "p-alpha-inc")
row = set_live_session(wd, 23)
context = assign_alpha_context(wd, row)[0]
wd.messages = []
check("pro 1 interrupt 默认进入 soft", wd.handle("pro 1 interrupt"), True)
check("等待软中断内容", wd.pending_soft_interrupt.get("stage"), "text")
check("默认 soft 不下架任务", bool(wd.task_store.get(context["id"]).get("session")), True)
check("软中断面板出现", "软中断" in wd.render_interrogate().renderable.plain, True)
check("输入内容", wd.handle("Alpha 的总分题总是超时"), True)
check("内容已落盘", wd.projects.soft_interrupts(pro["id"])[0]["text"],
      "Alpha 的总分题总是超时")
check("等待类别", wd.pending_soft_interrupt.get("stage"), "category")
check("选择 A", wd.handle("A"), True)
rec = wd.projects.soft_interrupts(pro["id"])[0]
check("类别保存为 A", rec["category"], "A")
check("项目名前出现问号",
      wd.current_project_rows()[0]["pro_title"].startswith("❓"), True)
wd.messages = []
check("project 列表受理", wd.handle("project"), True)
check_true("project 列表也有问号",
           any("❓" in t and "Alpha" in t for t in texts(wd)))
wd.messages = []
check("进入软中断检查", wd.handle("pro 1 interrupt --soft --check"), True)
check("逐条检查第一项", wd.pending_soft_interrupt.get("stage"), "check")
check("选择处理入队", wd.handle("2"), True)
check("进入任务时长选择", wd.pending_soft_interrupt.get("stage"), "minutes")
check("选择 50 分钟", wd.handle("50"), True)
check("问答结束", wd.pending_soft_interrupt, None)
pro_after = wd.projects.pro_by_id(pro["id"])
check("项目自动切到 bottleneck", pro_after["stage"], "bottleneck")
op_after = wd.projects.by_id(op_inc["id"])
check("incubator 第一条是软中断任务",
      op_after["tasks"][0]["title"].startswith("Alpha 的总分题总是超时"), True)
check("incubator 任务是本地任务", op_after["tasks"][0].get("local_only"), True)
soft_tasks = [t for t in wd.task_store.list()
              if t["fields"].get("title", "").startswith("Alpha 的总分题总是超时")]
check("本地生成了 50 分钟任务", len(soft_tasks), 1)
check("本地任务时长 50", int(soft_tasks[0]["fields"].get("duration") or 0), 50)
check("软中断记录状态 handled",
      wd.projects.soft_interrupts(pro["id"])[0]["status"], "handled")
check("处理完项目名不再带问号",
      wd.current_project_rows()[0]["pro_title"].startswith("❓"), False)

check("项目对账不把 local_only 软中断任务误判为远端已删除",
      pj.diff_tasks([{"pk": "soft-1", "title": "x", "local_only": True}], [])
      .get("removed"), [])

print("== cst：当前场唯一任务的项目优先；23 分钟只找 ≤20 分钟 ==")
reset()
wd = mkwarden()
import_projects(wd)
row = set_live_session(wd, 23)
assign_alpha_context(wd, row)
wd.messages = []
check("cst 受理", wd.handle("cst"), True)
picked = [t for t in wd.task_store.list()
          if t.get("session") and t["fields"].get("project_task") == "t-a-20"]
check("选中 Alpha 的 20 分钟任务", len(picked), 1)

print("== cst：其他项目按 OmniFocus 项目顺序找 ==")
reset()
wd = mkwarden()
import_projects(wd)
row = set_live_session(wd, 23)
# 当前场放两条 Alpha 任务：不再有「唯一任务」的项目偏好；Alpha 无合适任务，按顺序找 Beta。
assign_alpha_context(wd, row, pks=("t-a-long", "t-a-20"))
wd.handle("cst")
picked = [t for t in wd.task_store.list()
          if t.get("session") and t["fields"].get("project_task") == "t-b-20"]
check("Alpha 没合适的就选更靠前的 Beta", len(picked), 1)

print("== cst：剩余不足 10 分钟，当前项目没有 ≤5 分钟时先找 errand ==")
reset()
wd = mkwarden()
import_projects(wd)
row = set_live_session(wd, 8)
assign_alpha_context(wd, row)
wd.handle("cst")
picked = [t for t in wd.task_store.list()
          if t.get("session") and t["fields"].get("project_task") == "t-e-5"]
check("选中 errand 的 5 分钟任务", len(picked), 1)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
