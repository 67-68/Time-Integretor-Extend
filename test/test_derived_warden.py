#!/usr/bin/env python3
"""派生重复任务在 warden 里的那一半：`-d` / 完成勾 note / 勾完放弃 / plan 展开 / count 范围。

    python3 test/test_derived_warden.py

离线跑：不碰 Focusmate、不碰 OmniFocus。note 写回只检查 outbox 里排了什么。
"""
import datetime
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="derived-warden-test-")
for _name, _file in (
    ("TI_SESSION_LAYOUTS", "layouts.json"),
    ("TI_SESSION_EXTRAS", "extras.json"),
    ("TI_SESSION_RULES", "rules.json"),
    ("TI_SETTINGS_FILE", "settings.json"),
    ("TI_TASKS_FILE", "tasks.json"),
    ("TI_PROJECTS_FILE", "projects.json"),
    ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
    ("TI_NOSTART_FILE", "nostart.json"),
    ("TI_TASK_ARCHIVE", "archive.json"),
    ("TI_SINS_FILE", "sins.json"),
    ("TI_FAILED_ACTIONS", "failed-actions.json"),
    ("TI_OMNIFOCUS_OUTBOX", "outbox.json"),
):
    os.environ[_name] = os.path.join(TMP, _file)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
dv = w.dv
pj = w.pj
ts = w.ts

spec2 = importlib.util.spec_from_file_location(
    "omnifocus_tasks_test", os.path.join(ROOT, "omnifocus_tasks.py"))
ot = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(ot)

ks = w.ks
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "keep_session-expected.json")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_in(label, needle, haystack):
    ok = needle in (haystack or "")
    if not ok:
        fails.append("%s\n      缺 %r in %r" % (label, needle, haystack))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


class FakeBridge(object):
    def stop(self):
        pass


def fresh():
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.net_async = True
    return wd


NOTE = """# titles
- 2019-亚太-12
- 2019-亚太-22
- 2019-亚太-32
"""

DAILY = {"rule": "FREQ=DAILY;INTERVAL=1",
         "anchor_date_key": "[object Task.AnchorDateKey: DueDate]"}
DEFER = {"rule": "FREQ=DAILY;INTERVAL=1",
         "anchor_date_key": "[object Task.AnchorDateKey: DeferDate]"}
WEEKLY = {"rule": "FREQ=WEEKLY;BYDAY=WE",
          "anchor_date_key": "[object Task.AnchorDateKey: DueDate]"}


def reset():
    """清掉两份本地库，保证每个场景从干净状态开始。"""
    for key in ("TI_TASKS_FILE", "TI_PROJECTS_FILE", "TI_OMNIFOCUS_OUTBOX",
                "TI_TASK_ARCHIVE", "TI_FAILED_ACTIONS"):
        try:
            os.remove(os.environ[key])
        except OSError:
            pass


def make(note=NOTE, repeat=DAILY, count=25, task_type="parallel",
         title="完成历史 Papers", pk="taskHIST"):
    reset()
    wd = fresh()
    project, _created = wd.projects.upsert({
        "id": "projHIST", "title": "History Papers", "task_type": task_type,
        "tasks": [{"pk": pk, "title": title, "count": count,
                   "duration": count * 25, "note": note, "repeat": repeat,
                   "completed": False, "dropped": False}]})
    task = wd.task_store.create(
        {"title": title, "count": count, "duration": count * 25,
         pj.PROJECT_FIELD: "History Papers",
         pj.PROJECT_ID_FIELD: project["id"],
         pj.PROJECT_TASK_FIELD: pk,
         "note": note},
        omnifocus={"id": pk, "title": title})
    return wd, project, task


print("== 资格 / count 范围 ==")
wd, project, parent = make()
state = wd.derived_state(parent)
check("识别成标题清单重复任务", state["eligible"], True)
check("3 条未勾选", len(dv.open_items(dv.parse(state["note"]))), 3)
check("count 范围 25-75", wd.derived_count_span(parent), "25-75")
check_in("任务格子显示范围", "25-75", wd.task_count_label(parent))
check_in("任务面板格子里也能看到范围", "25-75", wd.task_index_title(parent))
check("不是父任务就没有范围", wd.derived_count_span(wd.task_store.create(
    {"title": "普通任务"})), None)
wd2, _, parent2 = make(repeat=DEFER)
check("defer 锚不合格", wd2.derived_state(parent2)["eligible"], False)
wd3, _, parent3 = make(repeat=WEEKLY)
check("每周重复不合格", wd3.derived_state(parent3)["eligible"], False)
wd4, _, parent4 = make(note="没有清单")
check("没有 # titles 不合格", wd4.derived_state(parent4)["eligible"], False)

print("== task N -d ==")
wd, project, parent = make()
derived = wd.derived_create(parent)
check("派生任务标题", derived["fields"]["title"], "完成历史 Papers：2019-亚太-12")
check("派生任务 count 继承", derived["fields"]["count"], 25)
check("派生任务没有 OmniFocus 关联", derived.get("omnifocus"), None)
check("source.kind", derived["source"]["kind"], "derived")
check("source 记了父 pk", derived["source"]["pk"], "taskHIST")
check("source 记了行号", derived["source"]["line"], 1)
check("合成项目任务 pk", derived["fields"][pj.PROJECT_TASK_FIELD], "taskHIST#d1")
check("没有 session（离线没场次）", derived.get("session"), None)

wd, project, parent = make()
wd.cmd_task(["id:%d" % parent["id"], "-d"])
lines = [t for t in wd.task_store.list() if dv.is_derived(t)]
check("命令派生一条", len(lines), 1)
check("命令派生的是第一条", lines[0]["fields"][dv.F_DERIVED_TEXT], "2019-亚太-12")
wd.cmd_task(["id:%d" % parent["id"], "-d"])
lines = [t for t in wd.task_store.list() if dv.is_derived(t)]
check("再敲一次派生下一条", lines[-1]["fields"][dv.F_DERIVED_TEXT], "2019-亚太-22")

# -d 2：按第 2 条未勾选
wd, project, parent = make()
two = wd.derived_create(parent, 2)
check("-d 2 取第二条", two["fields"][dv.F_DERIVED_TEXT], "2019-亚太-22")

print("== -ac 被拒绝 ==")
wd, project, parent = make()
try:
    wd.cmd_task(["id:%d" % parent["id"], "--assign", "current"])
    check("-ac 应该被拒绝", "没报错", "ValueError")
except ValueError as exc:
    check_in("-ac 提示用 -d", "派生", str(exc))

print("== 完成 → 勾 note ==")
wd, project, parent = make()
derived = wd.derived_create(parent)
wd.task_store.complete(derived["id"])
ops = wd.omni_outbox.ops
check("outbox 里排了一条 note_mark", [o["op"] for o in ops], ["note_mark"])
check("note_mark 指向父任务", ops[0]["id"], "taskHIST")
check("note_mark 按标题匹配", ops[0]["match"], "2019-亚太-12")
note = wd.derived_note_of("taskHIST")
check_in("本地 note 立刻勾上", "- [x] 2019-亚太-12", note)
check_in("原来没勾的行还在", "- 2019-亚太-22", note)

print("== 全部勾完 → 放弃整个重复任务 ==")
wd, project, parent = make(note="# titles\n- a\n- b\n")
first = wd.derived_create(parent)
wd.task_store.complete(first["id"])
second = wd.derived_create(parent)
wd.task_store.complete(second["id"])
ops = [o["op"] for o in wd.omni_outbox.ops]
check_in("排了放弃整套", "drop", ops)
drop = [o for o in wd.omni_outbox.ops if o["op"] == "drop"][0]
check("放弃带 all_occurrences", drop.get("all_occurrences"), True)
check("本地父任务已放弃",
      wd.task_store.get(parent["id"])["status"], ts.STATUS_DROPPED)
left = [t for t in wd.task_store.list()
        if dv.is_derived(t) and t["status"] == ts.STATUS_ACTIVE]
check("没留活的派生任务", left, [])
check("本地 note 全勾", dv.parse(wd.derived_note_of("taskHIST"))["open"], 0)

print("== 计划展开（parallel）==")
wd, project, parent = make()
items = pj.remaining_tasks(wd.projects.by_id(project["id"]), {})
check("只展开一条（下一条标题）", len(items), 1)
check("合成 pk", items[0]["pk"], "taskHIST#d1")
check("标题带父任务", items[0]["title"], "完成历史 Papers：2019-亚太-12")
check("count 继承父任务", items[0]["count"], 25)
check("total 也是 25", items[0]["total"], 25)
check("带 derived 元信息", items[0]["derived"]["line"], 1)
check("父任务本体不进队列",
      [i["pk"] for i in items if not i.get("derived")], [])

items = pj.remaining_tasks(wd.projects.by_id(project["id"]), {"taskHIST#d1": 25})
check("这一行已投满 → 换下一条", items[0]["derived"]["line"], 2)

print("== 计划展开（sequential）==")
wd, project, parent = make(task_type="sequential")
items = pj.remaining_tasks(wd.projects.by_id(project["id"]), {})
check("顺序项目展开全部标题", len(items), 3)
check("顺序按 note 顺序", [i["derived"]["text"] for i in items],
      ["2019-亚太-12", "2019-亚太-22", "2019-亚太-32"])

print("== 轮转（parallel）==")
wd, project, parent = make()
# 两个父任务：A 刚做过、B 没做过 → 派生项里 B 先上
wd.projects.upsert({"id": "projHIST", "title": "History Papers",
                    "task_type": "parallel",
                    "tasks": [{"pk": "taskHIST", "title": "完成历史 Papers",
                               "count": 25, "duration": 625, "note": NOTE,
                               "repeat": DAILY},
                              {"pk": "taskCS", "title": "完成 CS Papers",
                               "count": 25, "duration": 625, "note": NOTE,
                               "repeat": DAILY}]})
project = wd.projects.by_id(project["id"])
items = pj.remaining_tasks(project, {}, rotation={"taskHIST": "2026-11-14T10:00:00"})
check("轮转：刚做过的排后面", [i["derived"]["parent_pk"] for i in items],
      ["taskCS", "taskHIST"])

print("== distribute 带上 derived ==")
wd, project, parent = make()
project = wd.projects.by_id(project["id"])
recs, notes = w.pp.distribute(
    [{"project_id": project["id"], "count": 4, "project": project}],
    [project], {}, rotation={})
check("分到一条派生项", len(recs), 1)
check("distribute 认得出 derived", bool(recs[0].get("derived")), True)
check("这一场只分 4 count", recs[0]["count"], 4)
check("总 count 还是 25", recs[0]["total"], 25)

print("== 排期后 derived 元信息还在 ==")
wd, project, parent = make()
project = wd.projects.by_id(project["id"])
recs, _notes = w.pp.distribute(
    [{"project_id": project["id"], "count": 2, "project": project}],
    [project], {}, rotation={})
today = datetime.date.today().isoformat()
slots = [{"index": 1, "date": today, "start": "16:00", "end": "17:00",
          "duration": 50, "counts": 2, "id": "sess-1",
          "period": {"date": today, "id": "T1", "start": "16:00", "end": "17:00"},
          "period_auto": True}]
placements, unscheduled, _more, _layouts = w.pp.schedule_periods(recs, slots)
check("排进去了一条", len(placements), 1)
check("排期后还认得出 derived",
      bool((placements[0]["item"] or {}).get("derived")), True)
check("排期后 title 还是派生标题",
      placements[0]["item"]["title"], "完成历史 Papers：2019-亚太-12")

print("== plan 落盘字段 ==")
info = {"parent_pk": "taskHIST", "line": 1, "text": "2019-亚太-12",
        "parent_title": "完成历史 Papers"}
flds = w.pp.derived_fields(info, "taskHIST#d1")
check("落盘字段带行号", flds[dv.F_DERIVED_LINE], 1)
check("落盘字段带标题", flds[dv.F_DERIVED_TEXT], "2019-亚太-12")
check("落盘字段用合成 pk", flds["project_task"], "taskHIST#d1")
src = w.pp.derived_source(info)
check("source.kind", src["kind"], "derived")
check("source 不带 id（无 OmniFocus 关联）", src.get("id"), None)

print("== flush 回填 note_mark ==")
wd, project, parent = make(note="# titles\n- a\n- b\n")
d = wd.derived_create(parent)
wd.omni_outbox.ops = []
wd._apply_omnifocus_flush({
    "done": [], "dropped": [], "failed": [],
    "marked": [{"pk": "taskHIST", "task_id": d["id"],
                "res": {"note": "# titles\n- [x] a\n- b\n", "remaining": 1}}]}, {})
check("还剩一条 → 不放弃", wd.task_store.get(parent["id"])["status"], ts.STATUS_ACTIVE)
check_in("note 缓存更新", "- [x] a", wd.derived_note_of("taskHIST"))
wd._apply_omnifocus_flush({
    "done": [], "dropped": [], "failed": [],
    "marked": [{"pk": "taskHIST", "task_id": d["id"],
                "res": {"note": "# titles\n- [x] a\n- [x] b\n", "remaining": 0}}]}, {})
check("全勾完 → 本地放弃父任务",
      wd.task_store.get(parent["id"])["status"], ts.STATUS_DROPPED)
check("排了 drop", [o["op"] for o in wd.omni_outbox.ops], ["drop"])

print("== 唤起派生任务（project -a / cst 那条路）==")
wd, project, parent = make()
project = wd.projects.by_id(project["id"])
items = pj.remaining_tasks(project, {})
woken = wd._wake_project_leaf(project, items[0])
check("唤起后没有 OmniFocus 关联", woken.get("omnifocus"), None)
check("唤起后带派生行号", woken["fields"][dv.F_DERIVED_LINE], 1)
check("唤起后用合成 pk", woken["fields"][pj.PROJECT_TASK_FIELD], "taskHIST#d1")
check("唤起后 title 是派生标题", woken["fields"]["title"],
      "完成历史 Papers：2019-亚太-12")

print("== note_mark JS 生成 ==")
script = ot.build_batch_script({"ops": [{"id": "pk1", "op": "note_mark",
                                           "match": "a"}]})
check_in("含 note_mark 分支", 'note_mark', script)
check_in("按标题匹配", "line_not_found", script)
check_in("统计剩余条数", "titlesRemaining", script)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
