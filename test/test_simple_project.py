#!/usr/bin/env python3
"""简单项目（simple pro）回归：合成视图 / ∞ 账 / 虚拟任务 / 单向开关 / 写回隔离。

    python3 -B test/test_simple_project.py

覆盖的是「简单项目」这条契约里**不靠子进程**就能钉死的部分：
``plan_ops()`` 的合成条目、``stats()`` 的 ∞、``distribute()`` 的虚拟任务、
计划账本记在 pro 上、完成度守卫与 OmniFocus 写回的隔离、单向开关（切不回去）。
排期链路本身（切 75/50、手动时间段）在 ``test/live/test_live_simple_project.py``
里用真子进程跑。
"""
import datetime
import importlib.util
import io
import os
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


TMP = tempfile.mkdtemp(prefix="simple-project-test-")
# 罪/判决/守场状态都挂在 temp/ 下（keep_session 的 TEMP）——不隔离它，真实工作区
# 里一条待认罪的场次就会让 `wd.handle(...)` 落进宣言闸门，测试直接假红。
os.environ["TI_TEMP"] = TMP
os.environ["TI_DATA_CSV"] = os.path.join(TMP, "data.csv")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SESSION_MUTES"] = os.path.join(TMP, "mutes.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_PLAN_PINS"] = os.path.join(TMP, "plan-pins.json")
os.environ["TI_PLAN_DECISIONS"] = os.path.join(TMP, "plan-decisions.json")
os.environ["TI_PLAN_WATCHDOG"] = os.path.join(TMP, "plan-watchdog.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")
os.environ["TI_OMNIFOCUS_OUTBOX"] = os.path.join(TMP, "outbox.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

w.run_keep_session = lambda *a, **kw: (0, "", "")

pj, pp, ts = w.pj, w.pp, w.ts
TODAY = datetime.date.today()
ISO = TODAY.isoformat()
SID = pj.simple_project_id(4)


class FakeBridge(object):
    def call(self, cmd, timeout=60, **kw):
        return {"sessions": [], "date": kw.get("date"),
                "now": int(datetime.datetime.now().timestamp() * 1000)}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmniFocus(object):
    """只记账：简单项目**一次都不该**碰它。"""

    def __init__(self):
        self.flush_ops = []

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def flush(self, ops, timeout=None):
        self.flush_ops.append([dict(op) for op in (ops or [])])
        return [{"op": "ok"} for _ in ops or ()]

    def invalidate(self):
        pass


def mkwarden():
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus()
    return wd


def _op(idx, title):
    return {
        "id": idx, "title": title, "op_type": "SOP", "task_type": "parallel",
        "pro_id": idx, "project_tag": "", "tags": [], "file": None,
        "omnifocus": None, "status": "active", "status_at": None,
        "status_reason": None, "imported_at": None, "synced_at": None,
        "tasks": [{
            "pk": "t%d" % idx, "title": "%s · 一条活" % title, "count": 2,
            "duration": 50, "completed": False, "dropped": False,
            "effective_completed": False, "effective_dropped": False,
            "blocked": False, "tags": ["SOP"], "defer": None, "rank": 0,
        }],
        "plans": {},
    }


def _pro(idx, title, simple=False):
    return {
        "id": idx, "title": title, "stage": "running", "file": None,
        "sop_op_id": None, "incubator_op_id": None, "op_ids": [],
        "status": "active", "status_at": None, "status_reason": None,
        "stage_locked": False, "simple": simple, "soft_interrupts": [],
    }


def seed(pros_last_simple=False, simple_has_op=False):
    """3 个普通 pro+op，加一个第 4 个项目（默认没有 op）。"""
    store = pj.blank()
    store["projects"] = [_op(1, "普通一"), _op(2, "普通二"), _op(3, "普通三")]
    pro4 = _pro(4, "简单四", simple=pros_last_simple)
    if simple_has_op:
        store["projects"].append(_op(4, "简单四"))
        pro4.update({"sop_op_id": 4, "op_ids": [4]})
    store["pros"] = [_pro(1, "普通一"), _pro(2, "普通二"), _pro(3, "普通三"),
                     pro4]
    pj.save(store)
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    return mkwarden()


# ---------------------------------------------------------------- 1) 合成视图

def case_plan_ops_synthesises_simple_view():
    wd = seed(pros_last_simple=True)
    ops = wd.projects.plan_ops()
    check("4 个 active 项目都进 plan", len(ops), 4)
    view = ops[3]
    check("第 4 个是简单项目的合成条目", bool(view.get("simple")), True)
    check("合成 id = SIMPLE_ID_BASE + pro_id", view.get("id"), SID)
    check("没有活跃侧", view.get("side"), None)
    check("不带子任务清单", view.get("tasks"), [])
    check("计划账本从 pro 带出来", view.get("plans"), {})
    check("普通项目不受影响", [op.get("id") for op in ops[:3]], [1, 2, 3])

    # 有 op 的普通项目被标成简单：它的 op 不该再单独出现一次。
    store = pj.load()
    store["pros"][0]["simple"] = True
    pj.save(store)
    ids = [op["id"] for op in wd.projects.plan_ops()]
    check("标了简单的 pro 只出现一条（op 被合成视图替代）",
          ids, [pj.simple_project_id(1), 2, 3, SID])


# ---------------------------------------------------------------- 2) ∞ 的账

def case_stats_is_infinite():
    wd = seed(pros_last_simple=True)
    view = wd.projects.plan_ops()[3]
    stat = pj.stats(view, None, {"simple:4": 2})
    check("unlimited 标记", stat.get("unlimited"), True)
    check("剩余写成 ∞", stat["remaining_text"], "∞")
    check("进度是 已投/∞", stat["progress_text"], "2/∞")
    check("已投 count 照常累计", stat["done"], 2)
    check("remaining 仍是数字 0（别处的 int() 不会炸）", stat["remaining"], 0)
    # 清单是空的也一样 —— 简单项目根本不读它。
    check("空清单也能算出账", pj.stats(view, None, {})["remaining_text"], "∞")
    check("没有任务也说不出「没有可排的活」",
          pj.empty_reason(view), "简单项目不查任务清单（容量 ∞，排的是项目级虚拟任务）")


# ---------------------------------------------------------------- 3) 虚拟任务

def case_distribute_makes_one_virtual_task():
    wd = seed(pros_last_simple=True)
    view = wd.projects.plan_ops()[3]
    items, notes = pp.distribute(
        [{"project_id": SID, "project": view, "count": 5}], [view], {})
    check("只合成一条任务", len(items), 1)
    item = items[0]
    check("pk = simple:<pro_id>", item.get("pk"), "simple:4")
    check("标题就是项目名", item.get("title"), "简单四")
    check("count 等于分配量", item.get("count"), 5)
    check("分钟 = 5 × 25", item.get("minutes"), 125)
    check("标了 simple", item.get("simple"), True)
    check("标了 local_only（不挂 OmniFocus）", item.get("local_only"), True)
    check("没有「一条可排的任务都没有」的 note", notes, [])


# ---------------------------------------------------------------- 4) 账本记在 pro 上

def case_plan_ledger_lives_on_pro():
    wd = seed(pros_last_simple=True)
    wd.projects.set_allocation(SID, TODAY, 4)
    wd.projects.record_items(SID, TODAY, [{
        "pk": "simple:4", "task_id": 7, "count": 3,
        "start": "13:00", "duration": 75}])
    store = pj.load()
    pro = next(p for p in store["pros"] if p["id"] == 4)
    plan = (pro.get("plans") or {}).get(ISO) or {}
    check("分配记在 pro 上", int(plan.get("count") or 0), 4)
    check("条目也记在 pro 上", len(plan.get("items") or []), 1)
    check("op 库一个字没动（没有 id 撞车的 op 被写）",
          [op["id"] for op in store["projects"]], [1, 2, 3])
    check("重新读盘后 simple 标记还在",
          next(p for p in pj.load()["pros"] if p["id"] == 4).get("simple"), True)
    view = pj.simple_project_view(pro)
    check("plan_of(view) 读得到", int((pj.plan_of(view, TODAY) or {}).get("count") or 0), 4)
    check("planned_counts 读得到", pj.planned_counts(view, TODAY), 3)
    wd.projects.clear_items(SID, TODAY)
    view = pj.simple_project_view(next(p for p in pj.load()["pros"] if p["id"] == 4))
    check("clear_items 只清条目、留 count",
          (int((pj.plan_of(view, TODAY) or {}).get("count") or 0),
           len((pj.plan_of(view, TODAY) or {}).get("items") or [])), (4, 0))


# ---------------------------------------------------------------- 5) 完成度守卫

def case_completion_guard_bypassed():
    wd = seed(pros_last_simple=True)
    task = {"fields": {"project_id": SID, "project_task": "simple:4", "count": 3}}
    check("简单项目的虚拟任务不受「只做了一部分」的拦截",
          pj.completion_guard(task, wd.projects, wd.task_store), (True, ""))
    # 普通项目那条拦截还在（防止回归成「全放行」）。
    normal = {"fields": {"project_id": 1, "project_task": "t1", "count": 1}}
    allowed, why = pj.completion_guard(normal, wd.projects, wd.task_store)
    check("普通项目该拦的仍然拦", (allowed, bool(why)), (False, True))


# ---------------------------------------------------------------- 6) 写回隔离

def case_no_omnifocus_writeback():
    wd = seed(pros_last_simple=True)
    store = ts.TaskStore()
    task = store.create({"title": "简单四", pj.PROJECT_FIELD: "简单四",
                         pj.PROJECT_ID_FIELD: SID,
                         pj.PROJECT_TASK_FIELD: "simple:4", "count": 3},
                        omnifocus=None,
                        source={"kind": "project", "id": "simple:4",
                                "url": "", "local_only": True})
    check("虚拟任务没有 OmniFocus primary key",
          bool(ts.TaskStore.omnifocus_id(task)), False)
    puts = []

    class SpyOutbox(object):
        ops = []

        def put(self, item):
            puts.append(item)

        def remove(self, keys):
            return 0

    wd.omni_outbox = SpyOutbox()
    wd.omni_complete(task)
    wd.omni_drop(task)
    check("完成/丢弃都不塞 outbox", puts, [])
    check("也不真去碰 OmniFocus", wd.omnifocus.flush_ops, [])


# ---------------------------------------------------------------- 7) 单向开关

def case_simple_toggle_is_one_way():
    wd = seed(pros_last_simple=False)
    before_tasks = [t.get("id") for t in ts.TaskStore().list()]
    wd.handle("pro 4 simple")
    store = pj.load()
    check("pro 4 标成简单项目",
          next(p for p in store["pros"] if p["id"] == 4).get("simple"), True)
    check("开关不碰 OmniFocus（batch 一次都没跑）", wd.omnifocus.flush_ops, [])
    check("开关不碰任务库", [t.get("id") for t in ts.TaskStore().list()],
          before_tasks)

    error = None
    try:
        wd.cmd_project(["4", "simple", "off"])
    except ValueError as exc:
        error = str(exc)
    check_true("切回普通项目被拒", error)
    check_true("拒绝理由说「无法切回」", error and "无法切回" in error)
    check("被拒之后 simple 还是 True",
          next(p for p in pj.load()["pros"] if p["id"] == 4).get("simple"), True)

    error = None
    try:
        wd.cmd_project(["4", "-a"])
    except ValueError as exc:
        error = str(exc)
    check_true("简单项目不能切阶段", error)
    check_true("切阶段的拒绝说「简单项目」", error and "简单项目" in error)


# ---------------------------------------------------------------- 8) 面板 / 警告

def case_rows_and_warnings():
    wd = seed(pros_last_simple=True)
    rows = wd.current_project_rows()
    check("4 个项目各一行", len(rows), 4)
    row = next(r for r in rows if r.get("simple"))
    check("简单项目行剩余是 ∞", row.get("remaining_text"), "∞")
    check("简单项目行不报未打标", row.get("untagged"), [])
    check("简单项目行没有未完成计数", row.get("tasks_open"), 0)
    scan = wd.project_warning_scan()
    check("警告板里没有简单项目",
          [s for s in scan if s.get("pro_title") == "简单四"], [])
    table = wd.render_projects_table()
    check_true("项目表画得出来", table is not None)


# ---------------------------------------------------------------- 9) 手动时间段

def case_window_commands():
    wd = seed(pros_last_simple=True)
    view = wd.projects.plan_ops()[3]
    slots = [{
        "index": 1, "date": ISO, "start": "13:00", "end": "15:30",
        "duration": 125, "counts": 5, "id": None,
        "period_id": "T1", "period": {"id": "T1", "start": "13:00",
                                      "end": "15:30"},
        "period_auto": True, "window": False,
    }]
    al = pp.Allocator(TODAY, [view], slots, task_store=None, hooks={})
    al.handle("pro 1 at 13:00 4")
    entry = al.allocations[0] if al.allocations else {}
    check("at 记下起点", entry.get("from"), "13:00")
    check("at 记下 count", entry.get("count"), 4)
    check("at 不写 period_id", entry.get("period_id"), None)

    al.handle("pro 1 period T1")
    entry = al.allocations[0] if al.allocations else {}
    check("period 拿走整段容量", entry.get("count"), 5)
    check("period 记下时段", entry.get("period_id"), "T1")

    al.handle("pro 1 at 14:00 2")
    entry = al.allocations[0] if al.allocations else {}
    check("起点不命中空场起点时整条不执行（还是整个 T1）",
          (entry.get("count"), entry.get("period_id")), (5, "T1"))

    # 普通项目不能用时间段时间
    normal = wd.projects.plan_ops()[0]
    al2 = pp.Allocator(TODAY, [normal], slots, task_store=None, hooks={})
    al2.handle("pro 1 at 13:00 2")
    check("普通项目拒绝 at", al2.allocations, [])

    # 超预算整条不执行
    al3 = pp.Allocator(TODAY, [view], slots, task_store=None, hooks={})
    al3.handle("pro 1 at 13:00 6")
    check("超过这段的容量就拒绝", al3.allocations, [])
    al3.handle("pro 1 at 13:00 5")
    check("正好用满可以", (al3.allocations[0].get("count") if al3.allocations else 0), 5)


def case_session_panel_shows_project_tasks():
    """简单项目这一场进行中时，单独一块摆出它 OmniFocus 项目里的任务。"""
    import datetime as _dt
    wd = seed(pros_last_simple=True, simple_has_op=True)
    now = _dt.datetime.now()
    start = (now - _dt.timedelta(minutes=10)).strftime("%H:%M")
    wd.clock_now = now
    store = ts.TaskStore()
    task = store.create(
        {"title": "简单四", pj.PROJECT_FIELD: "简单四",
         pj.PROJECT_ID_FIELD: SID, pj.PROJECT_TASK_FIELD: "simple:4", "count": 3},
        omnifocus=None,
        source={"kind": "project", "id": "simple:4", "url": "",
                "local_only": True})
    store.assign(task["id"], {"date": now.date().isoformat(), "start": start,
                              "duration": 50})
    panel = wd.render_simple_project_panel()
    check_true("这一场有简单项目的活时画出清单框", panel is not None)
    if panel is not None:
        check_true("框的标题里有项目名", "简单四" in str(panel.title))

    # 时间挪开 → 不在这一场了 → 不画。
    wd.clock_now = now + _dt.timedelta(hours=3)
    check("不在这一场时不画", wd.render_simple_project_panel(), None)

    wd.clock_now = now
    # 说成没关联 op → 没有清单可摆 → 不画。
    view = wd.projects.plan_ops()[3]
    check_true("（前提）这个简单项目确实有 op",
               bool(wd.projects.ops_for_pro(
                   wd.projects.pro_by_id(view["pro_id"]))))
    store_data = pj.load()
    store_data["projects"] = [op for op in store_data["projects"]
                             if op.get("id") != 4]
    for pro in store_data["pros"]:
        if pro["id"] == 4:
            pro.update({"sop_op_id": None, "op_ids": []})
    pj.save(store_data)
    check("没有关联 op 时不画", wd.render_simple_project_panel(), None)


def main():
    for case in (case_plan_ops_synthesises_simple_view,
                 case_stats_is_infinite,
                 case_distribute_makes_one_virtual_task,
                 case_plan_ledger_lives_on_pro,
                 case_completion_guard_bypassed,
                 case_no_omnifocus_writeback,
                 case_simple_toggle_is_one_way,
                 case_rows_and_warnings,
                 case_window_commands,
                 case_session_panel_shows_project_tasks):
        print("== %s" % case.__name__)
        case()
    print()
    if fails:
        print("FAILED %d:" % len(fails))
        for text in fails:
            print(" - %s" % text)
        return 1
    print("全绿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
