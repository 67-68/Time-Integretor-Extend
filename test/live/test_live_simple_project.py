#!/usr/bin/env python3
"""黑盒：简单项目（simple pro）—— 无清单直排、大配额切场、手动时间段。

    python3 -B test/live/test_live_simple_project.py

和 ``test_live_blackbox.py`` 同一套规矩：只通过 ``warden.py --exec`` 驱动，
断言全部落在沙盒的 data.csv / data/*.json / temp/*.json 上，一个 warden 模块
都不 import、不 stub 任何函数、不订场（目标日 +10 天，PATH 里没有 node）。

覆盖的三件事（用户 2026-11-25 的契约）：

1. **空项目直排**：``pro`` 底下没有 OmniFocus 项目（或一条任务都没打标）也能
   排：``pro 4 simple`` → ``plan`` → ``pro 4 3`` → ``complete`` → 75 分钟一场。
2. **大配额跨场切分**：分 5 count 走标准流水线切成 ``75 + 50``。
3. **手动时间段**：``pro 4 at 13:00 4`` 让 13:00 起的场次连续归它
   （``75 + 25``），其余项目只看剩下的空场。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import blackbox_kit as bb  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


def say(res, needle):
    return needle in (res.get("out") or "")


# ---------------------------------------------------------------- 夹具


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def add_plain_pro(paths, title="黑盒 D · 简单"):
    """往沙盒里加一个**没有 op** 的 pro（第 4 个项目）。

    这就是「目标极度明确、不需要细粒度任务拆解」的场景：OmniFocus 里没有
    对应的项目清单，只有一句「今天干它几小时」。``simple`` 先留 false，
    测试里再用 ``pro 4 simple`` 打开 —— 顺便验证命令那条路。
    """
    store = _read(paths["projects"])
    store["pros"].append({
        "id": 4, "title": title, "stage": "running", "file": None,
        "sop_op_id": None, "incubator_op_id": None, "op_ids": [],
        "status": "active", "status_at": None, "status_reason": None,
        "stage_locked": False, "soft_interrupts": [],
    })
    store["next_pro_id"] = 10
    _write(paths["projects"], store)


def simple_plan_items(paths, day=None):
    """简单项目的当天计划条目（计划记在 pro 自己身上，不在 op 上）。"""
    day = day or paths["day"]
    store = _read(paths["projects"], {}) or {}
    out = []
    for pro in store.get("pros") or []:
        if not pro.get("simple"):
            continue
        plan = (pro.get("plans") or {}).get(day.isoformat()) or {}
        for item in plan.get("items") or []:
            out.append((pro.get("title"), item.get("pk"), item.get("start"),
                        item.get("duration"), item.get("task_id")))
    return out


def virtual_tasks(paths):
    """简单项目的虚拟任务（``fields.project_task`` 以 ``simple:`` 开头）。"""
    out = []
    for task in bb.tasks(paths):
        fields = task.get("fields") or {}
        if str(fields.get("project_task") or "").startswith("simple:"):
            out.append(task)
    return out


# ---------------------------------------------------------------- 场景


def case_1_empty_project_plans_directly():
    """空项目直排：没有 op、没有清单，也要能切出 75 分钟一场。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    day = sb["day"]

    res = bb.run(sb, ["pro 4 simple",
                      "plan %d" % bb.DEFAULT_OFFSET,
                      "pro 4 3", "complete"])
    check("空项目直排：退出码 0", res["rc"], 0)
    check("`pro 4 simple` 说清楚了它变成了什么", say(res, "简单项目"), True)
    check("plan 面板把它算进 4 个项目里", say(res, "4 个项目"), True)
    check("切出一场 75 分钟", bb.csv_rows(sb), [("13:00", 75)])
    check("layout 是 [75]、来源 plan",
          (bb.layouts(sb).get("T1") or {}).get("durations"), [75])
    check("plan items 落在 pro 自己身上",
          [(row[1], row[2], row[3]) for row in simple_plan_items(sb)],
          [("simple:4", "13:00", 75)])
    check("没有「未打标 / 一条可排的任务都没有」告警",
          say(res, "未打标") or say(res, "一条可排的任务都没有"), False)
    tasks = virtual_tasks(sb)
    check("本地只建了一条虚拟任务", len(tasks), 1)
    task = tasks[0] if tasks else {}
    fields = task.get("fields") or {}
    check("虚拟任务的标题是项目名", fields.get("title"), "黑盒 D · 简单")
    check("虚拟任务不带 OmniFocus 关联", bool(task.get("omnifocus")), False)
    check("虚拟任务标了 local_only", (task.get("source") or {}).get("local_only"), True)
    check("虚拟任务挂在 13:00", (task.get("session") or {}).get("start"), "13:00")
    check("simple 标记落盘", (_read(sb["projects"])["pros"][3].get("simple")), True)


def case_2_big_quota_splits_75_50():
    """分 5 count：按标准流水线切成 75 + 50。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    _make_simple(sb)
    res = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 4 5", "complete"])
    check("大配额：退出码 0", res["rc"], 0)
    check("切出 75 + 50 两场", bb.csv_rows(sb), [("13:00", 75), ("14:30", 50)])
    check("layout 是 [75, 50]",
          (bb.layouts(sb).get("T1") or {}).get("durations"), [75, 50])
    check("plan items 两段、同一个 pk",
          [(row[1], row[3]) for row in simple_plan_items(sb)],
          [("simple:4", 75), ("simple:4", 50)])
    check("本地建了两条虚拟任务（一场一条）", len(virtual_tasks(sb)), 2)


def case_3_manual_span_starts_at_given_time():
    """手动时间段：13:00 起连续归它，其余项目只看剩下的空场。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    _make_simple(sb)
    res = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET,
                      "pro 4 at 13:00 4", "pro 1 1", "complete"])
    check("手动时间段：退出码 0", res["rc"], 0)
    # 面板消息会被后续 say 挤出 Recent，所以这条断言看 JSONL 日志。
    windows = [ev for ev in bb.log_events(sb, area="plan", event="simple_window")]
    check("手动时间段落了 simple_window 日志", len(windows), 1)
    check("日志里 placed = 4", int((windows[0] if windows else {}).get("placed") or 0), 4)
    check("13:00 起 75 + 25 归简单项目，剩下 15:00 归项目 #1",
          bb.csv_rows(sb),
          [("13:00", 75), ("14:30", 25), ("15:00", 25)])
    check("layout 是 [75, 25, 25]",
          (bb.layouts(sb).get("T1") or {}).get("durations"), [75, 25, 25])
    mine = [(row[2], row[3]) for row in simple_plan_items(sb)]
    check("简单项目的 items 是 13:00(75) + 14:30(25)", mine,
          [("13:00", 75), ("14:30", 25)])
    others = [(row[1], row[2], row[3]) for row in bb.plan_items(sb)]
    check("#1 只拿到剩下的 15:00(25)", others, [("bb_1_0", "15:00", 25)])


def add_second_period(sb, start="16:00", end="18:00"):
    """再加一个空闲时段 T2（「今天下午再排一场」那个场景要用到）。"""
    path = sb["calendar"]
    data = _read(path)
    day = sb["day"].isoformat()
    data["events"].append({
        "date": day, "start": start, "end": end,
        "start_iso": "%sT%s:00" % (day, start),
        "end_iso": "%sT%s:00" % (day, end),
        "calendar": "空闲", "title": "黑盒时段 2", "all_day": False,
    })
    data["periods"].append({
        "id": "T2", "date": day, "start": start, "end": end,
        "type": "空闲", "name": "黑盒时段 2", "calendar": "空闲",
        "title": "黑盒时段 2",
        "start_iso": "%sT%s:00" % (day, start),
        "end_iso": "%sT%s:00" % (day, end),
    })
    _write(path, data)


def outbox(paths):
    data = _read(paths["of_outbox"], {}) or {}
    return list(data.get("ops") or data.get("items") or [])


def virtual_task_ids(paths):
    return [task.get("id") for task in virtual_tasks(paths)
            if (task.get("session") or {}).get("date") == paths["day"].isoformat()]


def _make_simple(sb):
    """直接把 pro 4 标成简单项目（不走命令，让场景只测排期本身）。"""
    store = _read(sb["projects"])
    for pro in store["pros"]:
        if pro["id"] == 4:
            pro["simple"] = True
    _write(sb["projects"], store)


def case_4_replan_is_idempotent():
    """重排幂等：2 → 4 个 count 能干净重切，不残留没挂场的幽灵任务。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    _make_simple(sb)
    first = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 4 2", "complete"])
    check("第一轮 2 count 排成一场 50", bb.csv_rows(sb), [("13:00", 50)])
    second = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 4 4", "complete"])
    check("第二轮退出码 0", second["rc"], 0)
    check("改成 4 count 后重切 75 + 25", bb.csv_rows(sb),
          [("13:00", 75), ("14:30", 25)])
    check("layout 干净重算（不留上一轮的尾巴）",
          (bb.layouts(sb).get("T1") or {}).get("durations"), [75, 25])
    ghosts = [task for task in virtual_tasks(sb)
              if not (task.get("session") or {}).get("start")]
    check("没有开放未挂场的幽灵虚拟任务", ghosts, [])
    check("两场各挂一条（同一 pk）",
          [(task.get("fields") or {}).get("project_task")
           for task in virtual_tasks(sb)].count("simple:4"), 2)


def case_5_second_session_same_day():
    """同日再做一场：完成一场后再 plan，不会被「同日去重」吞掉。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    _make_simple(sb)
    first = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 4 2", "complete"])
    check("上午第一场排好", bb.csv_rows(sb), [("13:00", 50)])
    ids = virtual_task_ids(sb)
    check("第一场只有一条虚拟任务", len(ids), 1)
    first_id = ids[0] if ids else None

    done = bb.run(sb, ["task id:%s done 50" % first_id])
    check("完成第一场退出码 0", done["rc"], 0)
    finished = next((task for task in bb.tasks(sb) if task.get("id") == first_id), {})
    check("第一场的任务真的完成了", finished.get("status"), "completed")
    check("完成没有往 OmniFocus outbox 塞东西",
          [op for op in outbox(sb) if str(op.get("id") or "").startswith("simple")], [])

    # 第二场：同一天、同一个简单项目，下午再排 2 count。
    second = bb.run(sb, ["plan %d" % bb.DEFAULT_OFFSET, "pro 4 2", "complete"])
    check("同日第二场退出码 0", second["rc"], 0)
    # 第一场（已完成的）那行是历史，不再当空场；它后面的尾巴 14:00 才是资源。
    check("下午那一场排在第一场后面的空档", bb.csv_rows(sb),
          [("13:00", 50), ("14:00", 50)])
    live = [(task.get("id"), (task.get("fields") or {}).get("project_task"))
            for task in virtual_tasks(sb)
            if task.get("status") == "active"]
    check("新建了一条虚拟任务（不是复用已完成的那条）",
          [tid for tid, _pk in live if tid != first_id], [t for t, _p in live])
    invested = sum(int((task.get("fields") or {}).get("count") or 0)
                   for task in virtual_tasks(sb)
                   if task.get("status") == "completed")
    check("投入累计到简单项目（已投 2 count）", invested, 2)


def case_6_switch_back_is_banned():
    """不可逆：`pro N simple off` 直接被拒，数据一个字节不动。"""
    sb = bb.make_sandbox()
    add_plain_pro(sb)
    on = bb.run(sb, ["pro 4 simple"])
    check("先标成简单项目", on["rc"], 0)
    before = _read(sb["projects"])
    res = bb.run(sb, ["pro 4 simple off"])
    check_true("切回普通项目被拒（输出说无法切回）", "无法切回" in (res.get("out") or ""))
    check("projects.json 没有被改",
          _read(sb["projects"])["pros"][3].get("simple"), True)
    check("op 库也没有被动", len(_read(sb["projects"])["projects"]),
          len(before["projects"]))
    check("没有产生任何 OmniFocus 写回", outbox(sb), [])
    check("没有建 session / 没有留下排期",
          bb.layouts(sb).get(sb["day"].isoformat()) or {}, {})


def main():
    for case in (case_1_empty_project_plans_directly,
                 case_2_big_quota_splits_75_50,
                 case_3_manual_span_starts_at_given_time,
                 case_4_replan_is_idempotent,
                 case_5_second_session_same_day,
                 case_6_switch_back_is_banned):
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
    sys.exit(main())
