#!/usr/bin/env python3
"""黑盒集成测试：只通过 ``warden.py --exec`` 命令驱动，断言全在数据文件上。

    python3 -B test/live/test_live_blackbox.py

和 ``test_live_plan.py`` 那些「同进程 import warden」的测试不同，这里**一个
warden 模块都不 import**：每条命令都是一个全新子进程，状态只能靠沙盒里的
data.csv / data/*.json / temp/*.json 传递。所以它测得到进程边界上的事：

* 上一轮 plan 切出来的场次，**下一次进程**里还算不算资源；
* 重排之后旧行有没有被真删掉（而不是两张表各说各话）；
* 任务绑定有没有随重排释放/复用，会不会重复唤起同一条任务。

不订场：目标日在 +10 天，执行区 24 小时永远够不到；PATH 里没有 node，
bridge / focusmate-mcp 一律瞬间失败（外部世界在测试里不存在）。
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import blackbox_kit as bb  # noqa: E402

fails = []
sb = None


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


def say(res, needle):
    return needle in (res.get("out") or "")


# ---------------------------------------------------------------- 场景

def case_1_replan_releases_previous_sessions():
    """plan 之后**重新 plan**：上一轮的场次必须被释放，时段可以重新用。

    这正是用户 2026-11-17 明确要求的一条：「plan 之后重新 plan，确保上次 plan
    的 session 被释放，重新可以用」。断言只看两轮子进程落盘的结果。
    """
    global sb
    sb = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = sb["day"]

    # ---- 第 1 轮：3 个项目共 5 个 count → 75 + 50 两场 -------------------
    first = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET,
                        "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("第 1 轮 plan+complete 退出码 0", first["rc"], 0)
    check("第 1 轮排出的场次", bb.csv_rows(sb), [("13:00", 75), ("14:30", 50)])
    lay = bb.layouts(sb).get("T1") or {}
    check("第 1 轮 layout 是 [75, 50]", lay.get("durations"), [75, 50])
    check("第 1 轮 layout 来源是 plan（不是手动硬约束）", lay.get("source"), "plan")
    items = bb.plan_items(sb)
    check("第 1 轮 plan items 3 条", len(items), 3)
    check("第 1 轮挂上本地任务 3 条", len(bb.bound_tasks(sb)), 3)

    # ---- 第 2 轮：只给 A 2 count（**新进程**，只能靠落盘状态） ----------
    second = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 1 2", "complete"])
    check("第 2 轮 plan+complete 退出码 0", second["rc"], 0)
    check("上一轮的 14:30 场被释放（csv 只剩这一轮要的）",
          bb.csv_rows(sb), [("13:00", 50)])
    lay2 = bb.layouts(sb).get("T1") or {}
    check("layout 按这一轮重切（不是把 50 追加到 75/50 后面）",
          lay2.get("durations"), [50])
    check("layout 仍然是 plan 来源（说明它被重算过）", lay2.get("source"), "plan")
    check("plan items 只剩这一轮的 1 条", len(bb.plan_items(sb)), 1)
    bound = bb.bound_tasks(sb)
    check("只有 A 那条任务还挂着", [t[0] for t in bound], ["A · 写方案骨架"])
    check("B/C 的任务已释放（解绑但任务还在）",
          sorted(bb.task_title(t) for t in bb.tasks(sb)),
          ["A · 写方案骨架", "B · 收集材料", "C · 十分钟小活"])
    # 同一条项目清单行不许被唤起两份（11-17 真实踩过的坑）
    keys = [(str((t.get("fields") or {}).get("project_id")),
             str((t.get("fields") or {}).get("project_task") or ""))
            for t in bb.tasks(sb) if (t.get("fields") or {}).get("project_id")]
    check("同一条清单行没有被唤起两份",
          {k: keys.count(k) for k in set(keys) if keys.count(k) > 1}, {})

    # ---- 第 3 轮：把 5 个 count 再分回去 → 时段必须**原样可用** ---------
    third = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET,
                        "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("第 3 轮 plan+complete 退出码 0", third["rc"], 0)
    check("被释放的两个时段重新可用（回到 75 + 50）",
          bb.csv_rows(sb), [("13:00", 75), ("14:30", 50)])
    check("第 3 轮 layout 又是 [75, 50]",
          (bb.layouts(sb).get("T1") or {}).get("durations"), [75, 50])
    check("第 3 轮 plan items 回到 3 条", len(bb.plan_items(sb)), 3)
    # 任务仍然是 3 条：重排只解绑/复用，不许凭空复制任务
    check("任务条数没有随重排增长", len(bb.tasks(sb)), 3)
    return first, second, third


def case_2_plan_after_clear_is_idempotent():
    """plan → plan clear → 再 plan：计划条目清干净，重排结果和第一次一致。

    ``plan clear`` 撤的是**计划**（items / 分配 / 绑定），不是排布：layout 和
    data.csv 里的场次还在（那是「这个时段切成了几场」的事实）。所以这里断言
    「计划没了、任务解绑了」，不断言 csv 消失。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                   "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    base = bb.csv_rows(paths)
    cleared = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "plan clear"])
    check("plan clear 退出码 0", cleared["rc"], 0)
    check_true("plan clear 说了「计划已清空」", say(cleared, "计划已清空"))
    check("plan clear 之后没有 plan items", bb.plan_items(paths), [])
    check("plan clear 之后任务全部解绑", bb.bound_tasks(paths), [])
    check("plan clear 不删排布（layout 还在）",
          (bb.layouts(paths).get("T1") or {}).get("durations"), [75, 50])
    bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                   "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("清空后再 plan 和第一次 plan 一致", bb.csv_rows(paths), base)
    check("plan items 也回到 3 条", len(bb.plan_items(paths)), 3)


def case_3_revived_session_is_not_pruned():
    """重新 plan 到「曾经被放弃」的时刻：refresh 不许把新计划当成幽灵剪掉。

    复刻 2026-11-17 用户报的 bug：「每次 plan 之后 r -c，计划会被冲掉」。
    现场是两个账本不一致：`commit_layout` 撤了 verdicts 里的 abandoned，
    但 `keep_session-state.json` 的 slots[key].status 还留着 abandoned，
    于是下一次 refresh 的 `_prune_dead_auto_slots` 把刚排的场次判死、剪掉，
    连带把项目 plan 整片清空。
    """
    paths = bb.make_sandbox(offset=1)          # 用明天：不依赖现在几点
    day = paths["day"]
    iso = day.isoformat()
    key = "%sT13:00" % iso
    with open(os.path.join(paths["temp"], "keep_session-verdicts.json"), "w",
              encoding="utf-8") as f:
        json.dump({"abandoned": {key: {"at": "%sT11:50:00" % iso,
                                       "why": "manual-cancel"}}}, f)
    with open(os.path.join(paths["temp"], "keep_session-state.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": iso, "slots": {key: {"status": "abandoned", "duration": 75}},
                   "api_snapshot": {"at": "%sT11:50:00" % iso,
                                    "horizon": "%sT23:59:00" % iso,
                                    "sessions": [], "details": []}}, f)

    first = bb.run(paths, ["plan 1", "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("重新 plan 到被放弃过的时刻，退出码 0", first["rc"], 0)
    check("场次按 plan 写出来了", bb.csv_rows(paths), [("13:00", 75), ("14:30", 50)])
    check("plan items 3 条", len(bb.plan_items(paths)), 3)
    # say() 进的是 Recent 面板，脚本模式只保留最后几条 —— 判据用日志更稳
    check_true("撤销动作进了日志（layout revive）",
               any(r.get("event") == "revive" for r in bb.log_events(paths)))
    state = json.load(open(os.path.join(paths["temp"], "keep_session-state.json"),
                           encoding="utf-8"))
    check("state 里那条 abandoned 被一起撤掉了",
          [k for k, v in (state.get("slots") or {}).items()
           if str((v or {}).get("status") or "") == "abandoned"], [])

    second = bb.run(paths, ["day 1", "refresh"])
    check("refresh 退出码 0", second["rc"], 0)
    check("refresh 之后场次还在", bb.csv_rows(paths), [("13:00", 75), ("14:30", 50)])
    check("refresh 之后 plan items 还在", len(bb.plan_items(paths)), 3)
    check_true("refresh 没有把计划冲掉", "和当前时间资源对不上" not in second["out"])
    return paths


def case_4_pin_standalone_task():
    """plan 里安排**单独任务**：一条不属于任何项目的任务也能固定 count 排进 session。

    用户 2026-11-17 定的新语法：``task <任务> <count>`` = 把任意任务当作
    「只有一个任务的独立项目」排；固定项记在 ``temp/plan-pins.json``，重排 /
    ``plan clear`` 必须把它解绑（否则那条任务永远占着场次）。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]

    # ---- 造一条独立任务（进程 1）------------------------------------
    made = bb.run(paths, ["task create title=独立任务 duration=50"])
    check("建独立任务退出码 0", made["rc"], 0)
    solo = [t for t in bb.tasks(paths) if bb.task_title(t) == "独立任务"]
    check("任务库里只有这一条任务", len(solo), 1)
    tid = solo[0]["id"]
    check("它是独立任务（不属于任何项目）",
          (solo[0].get("fields") or {}).get("project_id"), None)

    # ---- 进程 2：plan + 按内部 id 固定 2 count -------------------------
    first = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                           "task id:%d 2" % tid, "complete"])
    check("plan + 固定单独任务 退出码 0", first["rc"], 0)
    check("按 2 count 切出一场 50 分钟", bb.csv_rows(paths), [("13:00", 50)])
    lay = bb.layouts(paths).get("T1") or {}
    check("layout 是 [50]、来源 plan",
          (lay.get("durations"), lay.get("source")), ([50], "plan"))
    check("独立任务真的挂上了",
          [(t[0], t[1]) for t in bb.bound_tasks(paths)], [("独立任务", "13:00")])
    check("没有复制出第二条同名任务",
          len([t for t in bb.tasks(paths) if bb.task_title(t) == "独立任务"]), 1)
    led = bb.plan_pins(paths)
    check("固定项台账记了 1 条", len(led), 1)
    check("台账里的任务 id 对得上", led[0].get("task_id"), tid)
    check("台账里的起点对得上", led[0].get("start"), "13:00")
    check("项目库没有被写进固定项", bb.plan_items(paths), [])

    # ---- 进程 3：重新 plan（项目分资源）→ 固定项释放、场次可重新用 -----
    second = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET, "pro 1 2", "complete"])
    check("重新 plan 退出码 0", second["rc"], 0)
    check("固定项被解绑（只剩项目那条）",
          [t[0] for t in bb.bound_tasks(paths)], ["A · 写方案骨架"])
    check("台账里当天没有固定项了", bb.plan_pins(paths), [])
    check("独立任务还在库里（解绑 ≠ 丢任务）",
          len([t for t in bb.tasks(paths) if bb.task_title(t) == "独立任务"]), 1)

    # ---- 进程 4：再固定一次，然后 plan clear ---------------------------
    third = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                           "task id:%d 2" % tid, "complete"])
    check("固定项可以重新用", third["rc"], 0)
    check("重新用上的是同一场",
          [(t[0], t[1]) for t in bb.bound_tasks(paths) if t[0] == "独立任务"],
          [("独立任务", "13:00")])
    cleared = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "plan clear"])
    check("plan clear 退出码 0", cleared["rc"], 0)
    check("plan clear 之后任务全部解绑", bb.bound_tasks(paths), [])
    check("plan clear 之后台账清空", bb.plan_pins(paths), [])
    return paths


def main(argv):
    keep = "--keep" in argv
    print("== 场景 1：plan → 重新 plan（上一轮场次必须释放）==")
    try:
        case_1_replan_releases_previous_sessions()
    except Exception as e:                         # noqa: BLE001 - 黑盒也要留现场
        import traceback
        fails.append("场景 1 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 3：重新 plan 到「曾经被放弃」的时刻，refresh 不许剪掉==")
    try:
        case_3_revived_session_is_not_pruned()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 3 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    pin_paths = None
    print("\n== 场景 4：plan 里安排单独任务（task <任务> <count>）==")
    try:
        pin_paths = case_4_pin_standalone_task()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 4 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 2：plan → plan clear → 再 plan（幂等）==")
    try:
        case_2_plan_after_clear_is_idempotent()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 2 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    # 现场：只保留场景 1 / 4 的沙盒（出红时人要看它）
    for paths, label in ((sb, "场景 1"), (pin_paths, "场景 4")):
        if not paths or keep:
            continue
        if fails:
            print("\n%s 沙盒留在 %s（data.csv / layouts / 日志都在）"
                  % (label, paths["root"]))
        else:
            shutil.rmtree(paths["root"], ignore_errors=True)
    print()
    if fails:
        print("FAILED %d 项：" % len(fails))
        for item in fails:
            print(" - %s" % item)
        return 1
    print("全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
