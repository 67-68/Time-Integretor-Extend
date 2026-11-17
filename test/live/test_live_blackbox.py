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
                        "task 1 2", "task 2 2", "task 3 1", "complete"])
    check("第 1 轮 plan+complete 退出码 0", first["rc"], 0)
    check("第 1 轮排出的场次", bb.csv_rows(sb), [("13:00", 75), ("14:30", 50)])
    lay = bb.layouts(sb).get("T1") or {}
    check("第 1 轮 layout 是 [75, 50]", lay.get("durations"), [75, 50])
    check("第 1 轮 layout 来源是 plan（不是手动硬约束）", lay.get("source"), "plan")
    items = bb.plan_items(sb)
    check("第 1 轮 plan items 3 条", len(items), 3)
    check("第 1 轮挂上本地任务 3 条", len(bb.bound_tasks(sb)), 3)

    # ---- 第 2 轮：只给 A 2 count（**新进程**，只能靠落盘状态） ----------
    second = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "task 1 2", "complete"])
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
                        "task 1 2", "task 2 2", "task 3 1", "complete"])
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
                   "task 1 2", "task 2 2", "task 3 1", "complete"])
    base = bb.csv_rows(paths)
    cleared = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "plan clear"])
    check("plan clear 退出码 0", cleared["rc"], 0)
    check_true("plan clear 说了「计划已清空」", say(cleared, "计划已清空"))
    check("plan clear 之后没有 plan items", bb.plan_items(paths), [])
    check("plan clear 之后任务全部解绑", bb.bound_tasks(paths), [])
    check("plan clear 不删排布（layout 还在）",
          (bb.layouts(paths).get("T1") or {}).get("durations"), [75, 50])
    bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                   "task 1 2", "task 2 2", "task 3 1", "complete"])
    check("清空后再 plan 和第一次 plan 一致", bb.csv_rows(paths), base)
    check("plan items 也回到 3 条", len(bb.plan_items(paths)), 3)


def main(argv):
    keep = "--keep" in argv
    print("== 场景 1：plan → 重新 plan（上一轮场次必须释放）==")
    try:
        case_1_replan_releases_previous_sessions()
    except Exception as e:                         # noqa: BLE001 - 黑盒也要留现场
        import traceback
        fails.append("场景 1 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 2：plan → plan clear → 再 plan（幂等）==")
    try:
        case_2_plan_after_clear_is_idempotent()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 2 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    # 现场：只保留场景 1 的沙盒（出红时人要看它）
    for paths in (sb,):
        if paths and not keep:
            if fails:
                print("\n场景 1 沙盒留在 %s（data.csv / layouts / 日志都在）" % paths["root"])
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
