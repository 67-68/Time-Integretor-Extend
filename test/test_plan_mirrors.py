#!/usr/bin/env python3
"""plan 反复排期留下的「镜像幽灵」与写回去重（2026-11-18 用户报的问题）。

    python3 test/test_plan_mirrors.py

现场：用户一上午反复 plan / refresh，契约面板上冒出一排同名的
``🧬(4/3) Lit: Complete 1 Scene``。查下来是同一条 OmniFocus 任务在本地堆了
6～8 条镜像（每轮 plan 新建一条、旧的解绑后既没删也没藏）。

这一份守三件事：

1. **复用优先**：同 pk 有多条开放镜像时，plan 挑「开放且没挂 session」的那条
   复用（不是被第一条老副本挡住又建一条）；
2. **派生幽灵清理**：重排前把同 pk 的多余派生镜像删掉（留最新），有处理痕迹的
   不动；普通项目镜像不删，但不挂 session 时不出现在契约任务面板；
3. **写回去重按天**：同一天拆成多个 part 只写一次 OmniFocus；跨天的旧占位不算，
   否则今天做完的那条永远写不回去（17:30 语法被重排的直接原因）。
"""
import copy
import datetime
import importlib.util
import io
import json
import os
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="plan-mirrors-test-")

for key, name in (("TI_LOG_DIR", "logs"),
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
                  ("TI_FAILED_ACTIONS", "failed-actions.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, pj = w.sp, w.sch, w.ts, w.pj
sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": []}
        return {"verified": True, "taskId": "ft-1", "task": {"id": "ft-1"}}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


MIRROR_ROWS = [
    {"id": "p-mirror", "name": "论文", "kind": "project_root", "project": "",
     "tags": ["project/论文"], "completed": False, "dropped": False},
    {"id": "t-intro", "name": "写引言", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 50, "completed": False},
]


class FakeOmniFocus(object):
    """只回 rows，不碰真 OmniFocus。"""

    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows if rows is not None else MIRROR_ROWS)

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        return copy.deepcopy(self.rows)

    def get(self, pk, refresh=False):
        return next((dict(r) for r in self.rows
                     if str(r.get("id")) == str(pk)), None)

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def invalidate(self):
        pass


def reset_all():
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    pj.save(pj.blank())
    sp.save_layouts({"version": 1, "layouts": {}})
    if os.path.exists(sch.DATA_CSV):
        os.remove(sch.DATA_CSV)


def mkwarden():
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus_enabled = lambda: False
    wd.run_keep_session = lambda *a, **k: (0, "", "")
    wd.say = lambda text, kind="info": wd.messages.append((kind, "say", str(text)))
    wd.reload_entries()
    return wd


def mk_mirror(wd, title="跑数据", pk="t-data", project_id=1, derived=None,
              history=None):
    """凭空造一条项目/派生镜像（模拟反复 plan 留下的那条）。"""
    fields = {"title": title, "project": "论文", "project_id": project_id,
              "project_task": pk, "count": 1, "duration": 25}
    source = {"kind": "project", "id": pk, "url": "", "title": title}
    if derived:
        fields.update({"derived_line": derived, "derived_text": "x",
                       "derived_parent_pk": "ke3tlcbOu5Z",
                       "derived_parent_title": "Lit: Complete 1 Scene"})
        source = {"kind": "derived", "line": derived, "text": "x",
                  "pk": "ke3tlcbOu5Z", "parent_title": "Lit: Complete 1 Scene"}
    task = wd.task_store.create(fields, omnifocus=None if derived else
                                {"id": pk, "url": "", "title": title}, source=source)
    if history:
        store = wd.task_store.load()
        for row in store["tasks"]:
            if row["id"] == task["id"]:
                row["history"] = list(history)
        wd.task_store.save(store)
        task = wd.task_store.get(task["id"])
    return task


def set_status(wd, task_id, status, when, writeback=None):
    """直接把一条任务的完成状态/写回标记钉到某个时间（模拟旧账）。"""
    store = wd.task_store.load()
    for row in store["tasks"]:
        if row["id"] != task_id:
            continue
        row["status"] = status
        row["status_at"] = when
        if writeback:
            row.setdefault("omnifocus", {})
            row["omnifocus"]["writeback"] = {"completed": writeback}
    wd.task_store.save(store)


print("== 1) 同 pk 多条开放镜像：plan 挑最新的那条复用 ==")
reset_all()
wd = mkwarden()
old = mk_mirror(wd, pk="t-data")
closed = mk_mirror(wd, pk="t-data")
set_status(wd, closed["id"], ts.STATUS_COMPLETED, "2026-11-01T10:00:00",
           writeback="2026-11-01T10:05:00")
newer = mk_mirror(wd, pk="t-data")
picked = wd._plan_mirror_pick(1, "t-data")
check("挑的是最新的开放镜像（不是已完成的老副本）", picked["id"], newer["id"])
check("第一条老副本还在（普通镜像不在这里删）",
      any(t["id"] == old["id"] for t in wd.task_store.list()), True)

print("\n== 2) 派生幽灵：重排前清掉多余的，留最新；有痕迹的不动 ==")
reset_all()
wd = mkwarden()
g1 = mk_mirror(wd, title="Lit scene 2", pk="ke3tlcbOu5Z#d2", project_id=12, derived=2)
g2 = mk_mirror(wd, title="Lit scene 2", pk="ke3tlcbOu5Z#d2", project_id=12, derived=2)
touched = mk_mirror(wd, title="Lit scene 2", pk="ke3tlcbOu5Z#d2", project_id=12,
                    derived=2, history=[{"day": TODAY.isoformat(), "action": "skip",
                                         "at": "2026-11-18T10:00:00"}])
g4 = mk_mirror(wd, title="Lit scene 2", pk="ke3tlcbOu5Z#d2", project_id=12, derived=2)
removed = wd._prune_duplicate_mirrors(TODAY)
left = sorted(t["id"] for t in wd.task_store.list()
              if ts.TaskStore.is_open(t))
check("删掉的正是两条没痕迹的中间幽灵", sorted(removed), sorted([g1["id"], g2["id"]]))
check("剩下「最新那条 + 今天处理过那条」", left, sorted([touched["id"], g4["id"]]))

print("\n== 3) 面板：不挂 session 的派生镜像不进契约任务面板 ==")
wd.day_offset = 1
panel_ids = [t["id"] for t in wd.task_panel_tasks(TOMORROW)]
check("没挂 session 的派生镜像不在面板上", g4["id"] in panel_ids, False)
wd.task_store.assign(g4["id"], {
    "date": TOMORROW.isoformat(), "start": "19:00", "end": "19:25",
    "duration": 25, "period_id": "T3"})
panel_ids = [t["id"] for t in wd.task_panel_tasks(TOMORROW)]
check("挂上 session 之后回到面板", g4["id"] in panel_ids, True)

print("\n== 4) 写回去重：同一天只写一次，跨天的旧占位不算 ==")
reset_all()
wd = mkwarden()
old = mk_mirror(wd, title="语法难41-73", pk="dIcADD53lWK")
set_status(wd, old["id"], ts.STATUS_COMPLETED, "2026-11-01T10:00:00",
           writeback="2026-11-01T10:05:00")
today_task = mk_mirror(wd, title="语法难41-73", pk="dIcADD53lWK")
set_status(wd, today_task["id"], ts.STATUS_COMPLETED,
           TODAY.isoformat() + "T17:22:16")
ops = wd.pending_writebacks()
check("今天这条会写回（跨天老副本不挡）",
      [op.get("task_id") for op in ops], [today_task["id"]])
same_day = mk_mirror(wd, title="语法难41-73", pk="dIcADD53lWK")
set_status(wd, same_day["id"], ts.STATUS_COMPLETED,
           TODAY.isoformat() + "T18:30:00")
ops = wd.pending_writebacks()
check("同一天的第二条 part 不再重复写", len(ops), 1)

print("\n== 5) 空的未开始 session：plan 从时段起点重切，不贴尾巴 ==")
reset_all()
wd = mkwarden()
wd.omnifocus = FakeOmniFocus(MIRROR_ROWS)
wd.omnifocus_enabled = lambda: True
sp.save_download({
    "version": 1, "generated_at": "x", "start_date": TODAY.isoformat(), "days": 8,
    "profiles": ["空闲"], "events": [],
    "periods": [{"id": "T3", "date": TOMORROW.isoformat(), "start": "19:00",
                 "end": "21:30", "type": "空闲", "name": "晚间",
                 "calendar": "空闲", "title": "晚间"}],
})
sp.save_layouts({"version": 1, "layouts": {TOMORROW.isoformat(): {
    "T3": {"durations": [75], "source": "plan",
           "updated_at": "2026-11-18T18:00:00"}}}})
wd.reload_entries()
wd.handle("op import sop omnifocus:///project/p-mirror")
wd.handle("day 1")
wd.handle("plan +1")
wd.handle("pro 1 2")
wd.handle("complete")
rec = sp.get_layout_record(sp.load_layouts(), TOMORROW, "T3") or {}
check("时段按任务重切（2 count = 50 分钟）", rec.get("durations"), [50])
rows = []
with open(sch.DATA_CSV, encoding="utf-8") as fh:
    for line in fh:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[0] == TOMORROW.isoformat():
            rows.append((parts[1], int(parts[2])))
check("切在时段**起点** 19:00，而不是 20:15/20:30 的尾巴", rows, [("19:00", 50)])

print()
if fails:
    print("FAILED %d 项：" % len(fails))
    for item in fails:
        print(" -", item)
    raise SystemExit(1)
print("全绿。")
