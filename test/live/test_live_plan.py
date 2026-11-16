#!/usr/bin/env python3
"""集成测试（干跑）：把 fixtures 喂给**真实的 Warden**，跑完整条 plan 流水线。

和 `test/test_*.py` 的区别
-------------------------
那些单元测试各自 stub 掉半个世界；这个测试只 stub **一个**东西 —— 浏览器
（`run_keep_session` 不真的下单、FakeBridge 不出网）。其余全是真的：

    plan 分资源 → Allocator 挑任务 → distribute 切 session → commit_layout
    → 写 layout → 重算 data.csv → 把任务挂到 session → 记 project plan items
    → refresh 对账（硬/软失效）

所以它能抓到单元测试抓不到的那类错：模块之间口径不一致（plan item 的
start/duration 与真实绑定对不上、容量与 layout 对不上、叠加小任务的时长记错）。

    python3 -B test/live/test_live_plan.py            # 干跑
    python3 -B test/live/test_live_plan.py --keep     # 留下沙盒慢慢看

真机下单的那条在 `test_live_focusmate.py`（会真的订 2 场，跑完取消）。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402

fails = []
ok_count = 0


def check(label, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % label)
        return
    fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


def check_true(label, got):
    check(label, bool(got), True)


# ---------------------------------------------------------------- 造沙盒
PATHS = fixture.make_sandbox()
DAY = PATHS["day"]
OFFSET = (DAY - datetime.date.today()).days
for key, val in fixture.env_for(PATHS).items():
    os.environ[key] = val

import importlib.util  # noqa: E402
from rich.console import Console  # noqa: E402

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

pp, pj, ts, sp = w.pp, w.pj, w.ts, w.sp


class FakeBridge(object):
    """不出网的 bridge：本地没有 session（所以一切都是「还没订上」）。"""

    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": []}
        if cmd == "tasks":
            return {"tasks": []}
        return {}

    def browser_state(self):
        return True, None, ""

    def release(self):
        return None


def new_warden():
    wd = w.Warden(Console(record=True, width=140, file=open(os.devnull, "w")),
                  FakeBridge(), no_clear=True, offline=True, day_offset=OFFSET)
    # 唯一被 stub 的东西：预约出口不真的去开浏览器。
    w.run_keep_session = lambda *a, **kw: (0, "", "")
    wd.reload_periods()
    wd.reload_entries()
    return wd


def say_lines(wd):
    return [str(m.get("text") if isinstance(m, dict) else m) for m in wd.messages]


def run(wd, line):
    """敲一条命令；和交互式 dispatch 是同一条路（`--exec` 也走这里）。

    **先切回契约视图**：目标日一场 session 都没有时，refresh 会把视图自动切到
    「琐事屏」（ChoreView），那里的 `_handle_chore_input` 会吞掉 `plan`/`cancel`
    这类命令 —— 交互式用户也会遇到，测试里显式切回来免得踩到。
    """
    try:
        wd.go_to_view(w.ContractView)
        handled = wd.handle(line)
    except Exception as e:                       # noqa: BLE001 - 命令抛错要报出来
        return False, "%s: %s" % (type(e).__name__, e)
    return bool(handled), ""


# ---------------------------------------------------------------- 场景

def scenario_plan():
    """3 个项目 → 5 个 count → 2 场 session → 3 条任务（一条 10 分钟的要叠加）。"""
    wd = new_warden()
    _, err = run(wd, "day %d" % OFFSET)
    check_true("day 命令没报错", not err)
    _, err = run(wd, "plan %d" % OFFSET)
    check_true("plan 打开了分资源问答（没有当场报错）", not err)
    check_true("planning 状态已建立", wd.planning is not None)
    for pro_idx, count in fixture.ALLOCATION:
        _, err = run(wd, "task %d %d" % (pro_idx, count))
        check_true("分配 项目 %d ← %d count 没报错" % (pro_idx, count), not err)
    _, err = run(wd, "complete")
    check_true("complete 没报错（%s）" % (err or "ok"), not err)

    # -- 1) layout / data.csv：容量 5 count 反推出 75 + 50 两场 -----------------
    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))
    rec = sp.get_layout_record(layouts, DAY, "T1") or {}
    check("layout 是 plan 自己排的（source=plan，表格打 🗂）",
          rec.get("source"), "plan")
    check("排布 [75, 50]（75+15 休息+50 = 150 分钟，正好填满时段）",
          rec.get("durations"), [75, 50])

    rows = [r for r in sp.load_download(PATHS["calendar"]).get("periods") or []
            if r.get("date") == DAY.isoformat()]
    check("沙盒目标日只有 1 个时段", len(rows), 1)
    slots = w.sch.day_slots(wd.entries, DAY)
    check("data.csv 只写了 2 行（两场）",
          [(s["start"], int(s["duration"])) for s in slots],
          [("13:00", 75), ("14:30", 50)])

    # -- 2) 任务库：3 条任务，各自真的挂在一场上 -------------------------------
    tasks = [t for t in wd.task_store.list()
             if (t.get("source") or {}).get("kind") == "project"]
    check("plan 建了 3 条本地任务", len(tasks), 3)
    by_start = {}
    for t in tasks:
        sess = t.get("session") or {}
        check_true("任务 %s 挂上了 session" % t["id"], bool(sess.get("start")))
        check("任务 %s 的日期 = 目标日" % t["id"], sess.get("date"), DAY.isoformat())
        by_start[sess.get("start")] = (t, sess)
    check("两条大任务分别占 13:00 / 14:30（各一场，不挤）",
          sorted(by_start), ["13:00", "14:30"])

    # 小任务必须叠进别人的大场：session 长度 75，任务自己只占 10 分钟。
    small = [t for t in tasks if int((t.get("fields") or {}).get("duration") or 0) == 10]
    check("10 分钟小任务只有一条", len(small), 1)
    small_sess = (small[0].get("session") or {}) if small else {}
    check("小任务被叠进 75 分钟那场（不是自己开一场）", small_sess.get("duration"), 75)
    check("小任务在 Focusmate 上占的分钟 = 10（不是整场 75）",
          int((small[0].get("fields") or {}).get("duration") or 0), 10)

    # -- 3) 项目 plan items：start/duration 必须等于真挂上的那一场 -------------
    store = json.load(open(PATHS["projects"], encoding="utf-8"))
    items = []
    for op in store["projects"]:
        plan = pj.plan_of(op, DAY) or {}
        items.extend([(op.get("title"), it) for it in (plan.get("items") or [])])
    check("3 个项目各自记了 plan item", len(items), 3)
    for title, item in items:
        sess = None
        for t in tasks:
            if str((t.get("source") or {}).get("id")) == str(item.get("pk")):
                sess = t.get("session") or {}
        check_true("项目 %s 的 item 能找回本地任务" % title, sess is not None)
        check("项目 %s 的 item.start = 绑定场起点" % title,
              item.get("start"), (sess or {}).get("start"))
        check("项目 %s 的 item.duration = **场时长**（不是任务分钟）" % title,
              int(item.get("duration") or 0), int((sess or {}).get("duration") or 0))

    # -- 4) refresh 对账：没订上只是软警告，计划不许被清 -----------------------
    before = json.dumps(store, sort_keys=True)
    wd.refresh(quiet=True)
    after = json.load(open(PATHS["projects"], encoding="utf-8"))
    check_true("refresh 之后 project plan 还在（没被软警告清掉）",
               json.dumps(after, sort_keys=True) != "" and
               bool([it for op in after["projects"]
                     for it in (pj.plan_of(op, DAY) or {}).get("items") or []]))
    check_true("refresh 之后 items 数量不变",
               len([it for op in after["projects"]
                    for it in (pj.plan_of(op, DAY) or {}).get("items") or []]) == 3)
    lines = "\n".join(say_lines(wd))
    check_true("refresh 提示「还没订上」而不是「绑定和 plan 不一致」",
               "还没订上" in lines and "不一致" not in lines)
    return wd, before


def scenario_rerun_idempotent():
    """同一天再 plan 一次：不该凭空多订场次、不该把 plans 叠加成 6 条。"""
    wd = new_warden()
    run(wd, "day %d" % OFFSET)
    run(wd, "plan %d" % OFFSET)
    for pro_idx, count in fixture.ALLOCATION:
        run(wd, "task %d %d" % (pro_idx, count))
    _, err = run(wd, "complete")
    check_true("第二次 plan 也没报错（%s）" % (err or "ok"), not err)
    slots = w.sch.day_slots(wd.entries, DAY)
    check("重排后还是 2 场（没有累积成 4 场）",
          [(s["start"], int(s["duration"])) for s in slots],
          [("13:00", 75), ("14:30", 50)])
    store = json.load(open(PATHS["projects"], encoding="utf-8"))
    total = sum(len((pj.plan_of(op, DAY) or {}).get("items") or [])
                for op in store["projects"])
    check("已经排满的日子再 plan 不会凭空长出条目（上一轮原样保留）", total, 3)
    open_tasks = [t for t in wd.task_store.list() if wd.task_store.is_open(t)]
    check("本地任务没有重复长出来", len(open_tasks), 3)


def main(argv):
    keep = "--keep" in argv
    print("沙盒：%s" % PATHS["root"])
    print("目标日：%s（+%d 天），时段 %s–%s" % (DAY.isoformat(), OFFSET,
                                              fixture.PERIOD[0], fixture.PERIOD[1]))
    try:
        wd, _ = scenario_plan()
        scenario_rerun_idempotent()
    finally:
        if not keep and not fails:
            shutil.rmtree(PATHS["root"], ignore_errors=True)
    print()
    if fails:
        print("FAILED %d 项：" % len(fails))
        for item in fails:
            print(" - %s" % item)
        print("沙盒保留在 %s" % PATHS["root"])
        return 1
    print("全绿（%d 项）。" % ok_count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
