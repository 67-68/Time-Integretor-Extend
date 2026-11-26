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

    # ---- 第 2 轮：打开 plan → 预填上一轮决定（新进程只能靠落盘状态） -----
    opened = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET])
    check("第 2 轮打开 plan 时预填了上一轮的决定（变亮）",
          "已载入" in (opened.get("out") or ""), True)
    # 现行语义（2026-11-20）：二次打开保持原本的计划；要换掉旧项目必须显式
    # `pro <序号> clear` 回收 count，不能指望重排自动清空。
    second = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET,
                         "pro 1 2", "pro 2 clear", "pro 3 clear", "complete"])
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

    # ---- 进程 3：重新 plan → 固定项从台账预填（二次打开不丢旧决定） ----
    second = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET, "pro 1 2", "complete"])
    check("重新 plan 退出码 0", second["rc"], 0)
    check("固定项被预填保留（不是被重排丢掉）",
          sorted((t[0], t[1]) for t in bb.bound_tasks(paths)),
          [("A · 写方案骨架", "14:00"), ("独立任务", "13:00")])
    check("台账里固定项还在", len(bb.plan_pins(paths)), 1)
    check("独立任务还在库里（解绑 ≠ 丢任务）",
          len([t for t in bb.tasks(paths) if bb.task_title(t) == "独立任务"]), 1)

    # ---- 进程 4：plan clear 才是显式清空（连固定项台账一起） -----------
    cleared = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "plan clear"])
    check("plan clear 退出码 0", cleared["rc"], 0)
    check("plan clear 之后任务全部解绑", bb.bound_tasks(paths), [])
    check("plan clear 之后台账清空", bb.plan_pins(paths), [])

    # ---- 进程 5：清空之后固定项可以重新用 -----------------------------
    third = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                           "task id:%d 2" % tid, "complete"])
    check("固定项可以重新用", third["rc"], 0)
    check("重新用上的是同一场",
          [(t[0], t[1]) for t in bb.bound_tasks(paths) if t[0] == "独立任务"],
          [("独立任务", "13:00")])
    return paths



def case_5_period_window_trim():
    """裁时段窗口（`period T1 13:15-15:00`）：真子进程 + 只看落盘。

    2026-11-26 用户要求：「提供一个切割时段的命令，允许修改一个时段的开始和
    结束，只要被原本的时段包裹就行」。这里验三件事：

    1. 裁完窗口，plan 的场次全都落在新窗口里（data.csv 是证据）；
    2. 原来的 layout 装不下新窗口 → 被清掉而不是留着排不出来；
    3. 越界（12:00 早于原起点 13:00）→ 整条拒绝，窗口一个字节都不动。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]
    iso = day.isoformat()

    first = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                           "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("裁之前 plan 退出码 0", first["rc"], 0)
    check("裁之前用满 13:00–15:30", bb.csv_rows(paths),
          [("13:00", 75), ("14:30", 50)])

    trim = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                          "period T1 13:15-15:00",
                          "plan %d" % bb.DEFAULT_OFFSET, "complete"])
    check("裁窗口+重排 退出码 0", trim["rc"], 0)
    saved = bb._read_json(paths["period_windows"])
    check("窗口落盘（13:15–15:00）",
          (saved["days"][iso]["T1"]["start"], saved["days"][iso]["T1"]["end"]),
          ("13:15", "15:00"))
    rows = bb.csv_rows(paths)
    check("重排后一场都不早于 13:15", all(r[0] >= "13:15" for r in rows), True)
    check("重排后总时长 ≤ 新窗口容量（105 分钟 → 3 count）",
          sum(r[1] for r in rows) <= 75, True)
    check("新窗口排得下 3 count", sum(r[1] for r in rows), 75)

    bad = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                         "period T1 12:00-15:00"])
    check("越界被拒（12:00 早于原起点 13:00）", "包裹" in (bad.get("out") or ""), True)
    still = bb._read_json(paths["period_windows"])
    check("拒绝之后窗口没被改", still["days"][iso]["T1"]["start"], "13:15")

    back = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                          "period T1 window clear", "plan %d" % bb.DEFAULT_OFFSET])
    check("撤销窗口 退出码 0", back["rc"], 0)
    cleared = bb._read_json(paths["period_windows"])
    check("撤销之后落盘里没有这一天了",
          (cleared.get("days") or {}).get(iso), None)
    check("撤销之后又能用满 13:00–15:30",
          [r for r in bb.csv_rows(paths) if r[0] < "13:15"], [("13:00", 75)])
    return paths


def case_7_manual_period_hole_keeps_capacity():
    """手动 layout + 中间一条 extras：洞只占自己那段，别把整段吞掉。

    2026-11-26 现场（用户原话「19:25 - 21:25 那里去了？？？？」）：T3 是
    ``[25]``（formatter 切的）+ 21:00 一条 extras，``tail_gap`` 拿所有行验序列，
    一错就整段放弃 —— 2.5 小时的时段只产出两场 25 分钟，中间 3 个 count 消失。

    这里用黑盒复刻同一形状（13:00–15:30 + 手动 ``[25]`` + 15:00 的 extras）：
    1. plan 面板把 13:30(75) 那截算成空场，容量 125 分钟 = 5 count；
    2. 排进去之后落成 extras（layout 表达不了洞后的起点），data.csv 三场都在；
    3. 原来的 manual layout 不被重切（硬约束）。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]
    iso = day.isoformat()
    # 先把 extras 写进账本，再由 `period` 那次落盘把 data.csv 一起重算出来
    # （真实流程里 extras 一写就会重算 csv，黑盒里要显式给这一步）。
    extras = bb._read_json(paths["extras"])
    extras.setdefault("dates", {})[iso] = [
        {"start": "15:00", "duration": 25, "note": "formatter 切的（黑盒）"}]
    bb._write_json(paths["extras"], extras)
    made = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "period T1 25"])
    check("手动切 [25] 退出码 0", made["rc"], 0)
    check("切完之后 data.csv 有 13:00 和 15:00",
          bb.csv_rows(paths), [("13:00", 25), ("15:00", 25)])

    opened = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                            "plan %d" % bb.DEFAULT_OFFSET])
    check("plan 打开 退出码 0", opened["rc"], 0)
    check("面板把 13:30 的空档算成空场", say(opened, "13:30(75)"), True)
    check("容量 125 分钟 = 5 count（不是只剩两个 25）",
          say(opened, "125 分钟 = 5 count"), True)
    check("三场空场都列出来了", say(opened, "13:00(25) 13:30(75) 15:00(25)"), True)

    done = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                          "plan %d" % bb.DEFAULT_OFFSET,
                          "pro 1 2", "complete"])
    check("排完 退出码 0", done["rc"], 0)
    rows = bb.csv_rows(paths)
    check("data.csv 是 13:00(25) / 13:30(50) / 15:00(25)",
          rows, [("13:00", 25), ("13:30", 50), ("15:00", 25)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("13:30 那场落成 extras（不是改 layout）",
          any(str(r.get("start")) == "13:30" and int(r.get("duration") or 0) == 50
              for r in saved), True)
    check("plan 切的 extras 记了 source=plan",
          any(str(r.get("start")) == "13:30" and (r.get("source") == "plan")
              for r in saved), True)
    check("原来的 extras 还在（15:00）并标成 formatter",
          any(str(r.get("start")) == "15:00" for r in saved), True)
    layout = (bb.layouts(paths, day).get("T1") or {})
    check("manual layout 没被重切", layout.get("durations"), [25])
    check("manual layout 来源还是 manual", layout.get("source"), "manual")
    return paths


def case_8_plan_window_extra_recovers_after_clear():
    """plan 自己切的窗口 extra，`plan clear` 之后必须还能重算（不是永久硬洞）。

    2026-11-26 用户原话：「你还是没有处理 plan clear 之后 session 没有办法恢复
    到时段的问题」。以前 extras 不分出处：plan 为了避开手订场切出来的那一截，
    clear 之后被当成手订场钉死 —— 那截时间既不在 layout 里、又不在容量里。

    黑盒复刻：自动时段 13:00–15:30 + 13:00 一条 formatter 的 extras（硬洞），
    首轮 plan 在 13:30 切出窗口 → clear → 再 plan，容量仍是 4 count、
    data.csv 不出现重复行、plan 的 extras 只有一条。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]
    iso = day.isoformat()
    extras = bb._read_json(paths["extras"])
    extras.setdefault("dates", {})[iso] = [
        {"start": "13:00", "duration": 25, "note": "formatter 切的（黑盒）"}]
    bb._write_json(paths["extras"], extras)

    first = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                           "plan %d" % bb.DEFAULT_OFFSET,
                           "pro 1 2", "complete"])
    check("首轮 plan 退出码 0", first["rc"], 0)
    check("首轮把窗口落成 13:30(50)",
          bb.csv_rows(paths), [("13:00", 25), ("13:30", 50)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("13:30 那条 extras 记了 source=plan",
          [(r.get("start"), r.get("source")) for r in saved],
          [("13:00", None), ("13:30", "plan")])

    cleared = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "plan clear"])
    check("plan clear 退出码 0", cleared["rc"], 0)
    check("clear 之后 extras 一条没少",
          len((bb._read_json(paths["extras"])["dates"].get(iso) or [])), 2)

    again = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                           "plan %d" % bb.DEFAULT_OFFSET])
    check("clear 之后再 plan 退出码 0", again["rc"], 0)
    check("容量没缩水（还是 4 count = 13:30–15:30）",
          say(again, "100 分钟 = 4 count"), True)
    check("13:30 的窗口还在空场里", say(again, "13:30(100)"), True)

    replan = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                            "plan %d" % bb.DEFAULT_OFFSET,
                            "pro 1 2", "complete"])
    check("再排一次退出码 0", replan["rc"], 0)
    check("data.csv 没有重复行",
          bb.csv_rows(paths), [("13:00", 25), ("13:30", 50)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("plan 的 extras 仍然只有一条",
          [(r.get("start"), int(r.get("duration") or 0))
           for r in saved if r.get("source") == "plan"], [("13:30", 50)])
    return paths


def case_6_extras_are_hard_holes():
    """不在 layout 里的真实场次（extras）必须是硬洞，plan 不许压上去。

    2026-11-26 事故：11:15 那场被挪过的已订场在本地没有 layout 来源，plan 按
    整段容量重切，排出 11:00(50) / 12:00(25) 两场直接压住它。判据：plan 出来的
    每一场，都不和 extras 的实际占用（含 5 分钟休息）重叠。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]
    iso = day.isoformat()
    extras = bb._read_json(paths["extras"])
    extras.setdefault("dates", {})[iso] = [{"start": "14:00", "duration": 50,
                             "note": "被挪过的已订场（黑盒）"}]
    bb._write_json(paths["extras"], extras)

    res = bb.run(paths, ["plan %d" % bb.DEFAULT_OFFSET,
                         "pro 1 2", "pro 2 2", "pro 3 1", "complete"])
    check("有 extras 时 plan 退出码 0", res["rc"], 0)
    rows = bb.csv_rows(paths)
    check("extras 本身还在", [r for r in rows if r[0] == "14:00"], [("14:00", 50)])

    def span(start, dur):
        h, m = (int(x) for x in start.split(":"))
        s = h * 60 + m
        return s, s + dur // 25 * 5 + dur

    hole = span("14:00", 50)                       # 14:00 → 15:00（含休息）
    overlaps = [r for r in rows
                if r[0] != "14:00" and span(*r)[0] < hole[1] and span(*r)[1] > hole[0]]
    check("没有任何 plan 场次压住 extras", overlaps, [])
    check("容量只按洞扣完的算（2 + 1 = 3 count）",
          sum(r[1] for r in rows if r[0] != "14:00"), 75)
    return paths

def case_9_plan_own_windows_recut_in_manual_period():
    """手动 layout 的时段里，plan 上一轮自己切的窗口必须能重切成整块。

    2026-11-26 现场（用户原话「他为什么被拆成三块了？明明 2 和 3 也可以连起来」
    「让 SAT 和 English Review 完整排期」）：T3 是 manual ``[25]`` + 21:00 的
    formatter 洞，上一轮 plan 为了避开洞把中间切成 ``19:30(50)+20:30(25)`` 两条
    window extras。下一轮 plan 把这两条当成固定桩：3 count 的项目找不到一整场，
    被填缝拆成 25+25+25 撒到 20:30 / 21:00 / 22:15，22:15 的 50 分钟场次只填了
    1 count。

    黑盒复刻（13:00–15:30 + 手动 ``[25]`` + 15:00 的 formatter 洞）：
    1. 第一轮 2 count → 窗口 13:30(50)；
    2. 第二轮 3 count → 必须**重切**成 13:30(75) 一条，而不是 14:30 再来一条；
    3. 第三轮再排一次：幂等，data.csv / extras 一个字不变。
    """
    paths = bb.make_sandbox(offset=bb.DEFAULT_OFFSET)
    day = paths["day"]
    iso = day.isoformat()
    extras = bb._read_json(paths["extras"])
    extras.setdefault("dates", {})[iso] = [
        {"start": "15:00", "duration": 25, "note": "formatter 切的（黑盒）"}]
    bb._write_json(paths["extras"], extras)
    # fixture 里「A · 写方案骨架」只有 2 count；这一场要验证「3 count 整场放得下」，
    # 用简单项目（容量 ∞、每次 plan 一条虚拟任务），不掺 invested 账。
    made = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET, "pro 1 simple",
                          "period T1 25"])
    check("手动切 [25] 退出码 0", made["rc"], 0)
    check("切完之后 data.csv 是 13:00 / 15:00", bb.csv_rows(paths),
          [("13:00", 25), ("15:00", 25)])

    first = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                           "plan %d" % bb.DEFAULT_OFFSET,
                           "pro 1 2", "complete"])
    check("第一轮 2 count 排完 退出码 0", first["rc"], 0)
    check("第一轮把窗口落成 13:30(50)", bb.csv_rows(paths),
          [("13:00", 25), ("13:30", 50), ("15:00", 25)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("13:30 那条记了 source=plan",
          [(r.get("start"), int(r.get("duration") or 0), r.get("source"))
           for r in saved], [("13:30", 50, "plan"), ("15:00", 25, None)])

    opened = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                            "plan %d" % bb.DEFAULT_OFFSET])
    check("第二轮打开 退出码 0", opened["rc"], 0)
    check("面板把上一轮切的 50+25 还原成一整块 13:30(75)",
          say(opened, "13:30(75)"), True)
    check("容量 = 125 分钟 = 5 count", say(opened, "125 分钟 = 5 count"), True)

    second = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                            "plan %d" % bb.DEFAULT_OFFSET,
                            "pro 1 3", "complete"])
    check("第二轮 3 count 排完 退出码 0", second["rc"], 0)
    check("重切成一条 13:30(75)（不是 50+25）", bb.csv_rows(paths),
          [("13:00", 25), ("13:30", 75), ("15:00", 25)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("plan 的 extras 只剩一条 13:30(75)",
          [(r.get("start"), int(r.get("duration") or 0))
           for r in saved if r.get("source") == "plan"], [("13:30", 75)])
    check("formatter 的 15:00 洞还在",
          any(str(r.get("start")) == "15:00" for r in saved), True)
    layout = (bb.layouts(paths, day).get("T1") or {})
    check("manual layout 没被重切", layout.get("durations"), [25])

    third = bb.run(paths, ["day %d" % bb.DEFAULT_OFFSET,
                           "plan %d" % bb.DEFAULT_OFFSET,
                           "pro 1 3", "complete"])
    check("第三轮再排一次 退出码 0", third["rc"], 0)
    check("幂等：data.csv 一个字不变", bb.csv_rows(paths),
          [("13:00", 25), ("13:30", 75), ("15:00", 25)])
    saved = bb._read_json(paths["extras"])["dates"].get(iso) or []
    check("幂等：extras 还是两条",
          [(r.get("start"), int(r.get("duration") or 0)) for r in saved],
          [("13:30", 75), ("15:00", 25)])
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

    print("\n== 场景 5：裁时段窗口（period T1 13:15-15:00）==")
    try:
        case_5_period_window_trim()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 5 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 6：extras 是硬洞（plan 不许压上去）==")
    try:
        case_6_extras_are_hard_holes()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 6 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 7：手动 layout + 中间 extras（洞别吞掉整段）==")
    try:
        case_7_manual_period_hole_keeps_capacity()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 7 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 8：plan 切的窗口 extra 在 clear 之后要能重算 ==")
    try:
        case_8_plan_window_extra_recovers_after_clear()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 8 异常：%s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("\n== 场景 9：手动 layout 时段里 plan 自己的窗口可重切 ==")
    try:
        case_9_plan_own_windows_recut_in_manual_period()
    except Exception as e:                         # noqa: BLE001
        import traceback
        fails.append("场景 9 异常：%s: %s" % (type(e).__name__, e))
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
