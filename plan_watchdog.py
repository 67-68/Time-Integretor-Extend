#!/usr/bin/env python3
"""plan_watchdog.py —— 「今天还没 plan」的提醒任务与兜底自动分配（**纯逻辑**）。

用户需求（2026-11-20 原话）：

    在没有 plan 的情况下，创建一个 plan 任务，要求用户完成 TI 中今天 - 后天的
    plan，同时使用 formatter 安排在最近的第一个 session。如果检查的时候这个
    任务已经存在，且安排在今天的某个 session，且这个 session 已经过去，同时
    没有计划，那么自动安排计划，用随机算法安排 count 到不同的 session。

后来拍板的口径（同日）：

* 缺 plan 只看**今天**（明天/后天只写在标题里，不检查）；
* 提醒任务是一条 25 分钟（1 count）的普通本地任务；
* 排期复用 formatter ``enlist``（当天最早还有空位的时段，切一场新 session）；
* 兜底只自动 plan **今天**，随机挑项目 + 随机 count，走正常 plan 流水线；
* plan 一存在就把提醒任务本地标完成。

本模块只做三件事：认人（``is_reminder``）、算时间（``session_end`` / ``allocated_counts``）、
随机分资源（``random_allocs``）。**不 import rich、不联网、不写盘** —— 状态机和
落盘在 ``warden_core/plan_watchdog_mixin.py``，这样测试可以直接喂 dict。
"""
from __future__ import annotations

import datetime
import random

#: 提醒任务的来源标记。靠它认人，**不靠标题** —— 用户自己手写的同名任务不算。
SOURCE_KIND = "plan-watchdog"

#: 标题与时长。用户原话：「一条 duration=25（1 count），标题写『排今天–后天的 plan』」。
TITLE = "排今天–后天的 plan"
DURATION = 25
MINUTES_PER_COUNT = 25

#: 备注里直接给出三条命令；提醒的任务就是「把这三件事做完」。
NOTES = (
    "在 TI 里把今天、明天、后天的计划都排掉：\n"
    "  plan        → 今天\n"
    "  plan +1     → 明天\n"
    "  plan +2     → 后天\n"
    "排完这三天的计划，这条任务才算真的做完（这一场就是专门留给排计划的）。"
)


def reminder_fields():
    """新建提醒任务时用的 fields（调用方负责再补 source 标记）。"""
    return {"title": TITLE, "duration": DURATION, "notes": NOTES}


def is_reminder(task):
    """这条任务是不是 plan 提醒任务（只看 ``source.kind``）。"""
    source = (task or {}).get("source")
    if not isinstance(source, dict):
        return False
    return str(source.get("kind") or "").strip() == SOURCE_KIND


def session_end(session):
    """任务绑定的那场 session 的**结束时刻**；认不出来返回 ``None``。

    ``session`` 是 :meth:`task_store.TaskStore.assign` 落下来的
    ``{"date": "YYYY-MM-DD", "start": "HH:MM", "duration": 25, ...}``。
    缺 date / start / duration 一律当「不知道」，绝不拿墙上时钟去猜。
    """
    session = session or {}
    raw_day = str(session.get("date") or "").strip()
    raw_start = str(session.get("start") or "").strip()
    try:
        day = datetime.date.fromisoformat(raw_day[:10])
        hour, minute = (int(part) for part in raw_start.split(":")[:2])
        span = int(session.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    if span <= 0:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (datetime.datetime.combine(day, datetime.time(hour, minute))
            + datetime.timedelta(minutes=span))


def allocated_counts(plan_ops, day):
    """这一天一共分给项目多少 count（= 「有没有 plan」的唯一判据）。

    和 ``ProjectStore.plan_of`` 同口径：``plans["YYYY-MM-DD"].count`` 之和。
    「已经 plan 过（哪怕只排了一部分）」就不再吵，所以 >0 就够了。
    """
    if day is None:
        return 0
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    total = 0
    for op in plan_ops or ():
        plan = (((op or {}).get("plans") or {}).get(key) or {})
        try:
            total += int(plan.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def random_allocs(plan_ops, free_counts, rng=None):
    """把 ``free_counts`` 个 count **随机**切给随机挑中的若干项目。

    返回 ``[{"project_id", "project", "count"}, ...]``，正好是
    :meth:`project_plan.Allocator` + ``_plan_apply`` 吃的形状。

    算法（刻意做得可复现，测试钉种子就行）：

    1. ``k = randint(1, min(项目数, count 数))`` —— 这次参与几个项目；
    2. 从候选 op 里**随机抽 k 个**（项目顺序不是优先级，兜底不该偏心）；
    3. 把 ``free_counts`` 用隔板法随机切成 k 份正整数，**正好用满**容量
       （容量是上限，真排不下由 ``plan_apply`` 自己截断）。

    没有项目 / 容量为 0 → 返回空列表，调用方记 WARNING、不写任何东西。
    """
    rng = rng or random.Random()
    ops = [op for op in (plan_ops or ())
           if isinstance(op, dict) and op.get("id") is not None]
    try:
        budget = int(free_counts)
    except (TypeError, ValueError):
        return []
    if budget <= 0 or not ops:
        return []
    k = rng.randint(1, min(len(ops), budget))
    picked = rng.sample(ops, k)
    if k == 1:
        cuts = []
    else:
        # 在 1..budget-1 里挑 k-1 个隔板 → 每份 ≥1，总和 = budget
        cuts = sorted(rng.sample(range(1, budget), k - 1))
    counts = []
    previous = 0
    for cut in list(cuts) + [budget]:
        counts.append(cut - previous)
        previous = cut
    return [{"project_id": op.get("id"), "project": op, "count": count}
            for op, count in zip(picked, counts)]
