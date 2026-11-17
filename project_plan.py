#!/usr/bin/env python3
"""project_plan.py —— ``plan``：把「今天的空闲」切成 count，分给项目，再排成具体任务。

这是项目（``project_store.py``）的另一半：**资源分配 → 任务唤起 → 排进 session**。
分三步，每步都能单独测：

```
1) 元数据      capacity(slots)        今天还有几场空 session = 几个 count
2) 分资源      Allocator（问人）       task 1 4 / task 1 clear / complete
3) 排任务      distribute() + schedule()   每个项目按分到的 count 唤起任务、排进 session
```

资源分配只有**一次**：候选是每个 active ``pro`` 当前阶段对应的那一个 op
（running → SOP / bottleneck → incubator），不会先排完 SOP 再进入 incubator 阶段。

## 为什么「分资源」和「排任务」是两件事

用户原话：*在大方向上的资源分配完成之后，需要具体分配每个 Project 的任务*。
先把「今天这台 4 个 count 给论文」定下来（人的判断），再让程序去挑具体哪几条任务、
排进哪一场（机器的活）。混在一起问，人就得在脑子里同时装两件事。

## 排任务：period-first

旧的 ``schedule()`` 仍保留给单场 / 旧测试用；正式 ``plan`` 走
:func:`schedule_periods()`：

1. **日历 period 不可变**，period 的工作容量 = ``floor(总分钟 / 30) * 25``；
2. 手动 ``period T1 50 25`` 是硬约束，任务只能往里填；没有手动 layout 时，
   由任务 duration 反推 25 / 50 / 75 的 session 布局；
3. 一个 session 最多一个 ``>=25`` 分钟任务，``<25`` 的小任务可以叠进去；
4. parallel 按 priority tag + OmniFocus 清单顺序，从最早 period 开始找能整条
   放下的任务；整条都放不下时才拆段；
5. sequential 按 OmniFocus 清单顺序从最早 period 往后填，当前 period 不够就
   拆到后续 period，不为了少拆而跳场。

这样用户给的 A25 / B50 / C75 例子会按 period 得到 25 + (50,75)，而不是先按
data.csv 里默认的 50/25 session 硬塞。

这个模块不联网、不依赖 rich；warden / 测试只调这里。
"""
from __future__ import annotations

import datetime

import project_store as pj
import session_plan as sp
import task_store as ts

MINUTES_PER_COUNT = ts.MINUTES_PER_COUNT
#: 每块 25 分钟工作之后的强制休息。**唯一真源在 session_plan**（排布要靠它推进
#: cursor），这里只是别名，别在别处再写一个字面量 5。
BREAK_MINUTES = sp.BREAK_MINUTES_PER_COUNT

#: ``complete`` 的几种写法。用户原话是「使用 complete 可以结束资源分配」。
COMPLETE_TOKENS = ("complete", "done", "ok", "go", "完成", "结束", "下一步")
CANCEL_TOKENS = ("q", "quit", "cancel", "exit", "取消", "放弃")
HELP_TOKENS = ("?", "help", "h", "帮助")
#: plan 进行中还认得的手敲命令（其余一律当「听不懂」）。
DISPATCH_TOKENS = ("refresh", "project", "pro", "op", "day", "task", "plan")


# ---------------------------------------------------------------- 容量


def slot_record(index, date_iso, start, duration, session_id=None, end=None):
    """一场可用的空 session（``index`` = 契约表格上的编号，``-a`` 用它）。"""
    duration = int(duration or 0)
    return {
        "index": index,
        "date": date_iso,
        "start": start,
        "end": end or start,
        "duration": duration,
        "counts": duration // MINUTES_PER_COUNT,
        "id": session_id,
    }


def capacity(slots):
    """这批空场次能装多少：``counts`` / 分钟 / 含 5 分钟休息的分钟数 / 场次数。"""
    counts = sum(int(s.get("counts") or 0) for s in slots or [])
    minutes = sum(int(s.get("duration") or 0) for s in slots or [])
    return {"counts": counts,
            "minutes": minutes,
            "with_breaks": minutes + BREAK_MINUTES * counts,
            "sessions": len(list(slots or []))}


def capacity_text(cap):
    """「4 场 / 150 分钟 = 6 count（含 5 分钟休息 180 分钟）」。"""
    return ("%d 场 / %d 分钟 = %d count（1 count = %d 分钟；含每块 %d 分钟休息 %d 分钟）"
            % (cap.get("sessions", 0), cap.get("minutes", 0), cap.get("counts", 0),
               MINUTES_PER_COUNT, BREAK_MINUTES, cap.get("with_breaks", 0)))


def slots_text(slots, limit=8):
    bits = ["%s(%d)" % (s.get("start") or "?", int(s.get("duration") or 0))
            for s in list(slots or [])[:limit]]
    if len(list(slots or [])) > limit:
        bits.append("…")
    return " ".join(bits) if bits else "（没有空场）"


# ---------------------------------------------------------------- 分资源


def distribute(allocations, projects, invested_map=None, now=None, exclude_pks=None,
               rotation=None, skip_pks=None):
    """把「每个项目多少 count」摊成具体的任务（每条还带要几个 count / 分钟）。

    ``allocations``：``[{"project_id": 1, "count": 4}, ...]``（**顺序就是排的顺序**）；
    ``invested_map``：``{项目序号: {项目任务 pk: 已投入 count}}``
    （就是 ``project_store.invested_by_project(task_store)`` 那份账）。
    返回 ``(items, notes)``：
    ``items`` = ``[{"project_id", "project", "pk", "title", "count", ...}]``。

    短任务（实际时长 < 25 分钟）按**实际分钟**吃预算：分 1 count（25 分钟）
    可以塞下两条 10 分钟任务，而不是一条各占 1 count。项目里的任务不够填满
    分到的 count 时，少排一点并记一条 note（不报错 —— 那是项目清单的事实）。
    """
    invested_map = invested_map or {}
    exclude_pks = exclude_pks or {}
    out, notes = [], []
    for alloc in allocations or []:
        project = alloc.get("project") or {}
        pid = project.get("id", alloc.get("project_id"))
        need = int(alloc.get("count") or 0)
        if need <= 0:
            continue
        budget = need * MINUTES_PER_COUNT
        took = 0
        task_type = pj.task_type_of(project)
        excluded = (exclude_pks.get(pid) or exclude_pks.get(str(pid))
                    if isinstance(exclude_pks, dict) else exclude_pks)
        # ``derived_budget``：标题清单容器按「这次分到多少 count」展开够填满的
        # 条数（3 count/条、分到 6 count → 展开 2 条）。不传就还是只展开一条。
        skipped = ((skip_pks.get(pid) or skip_pks.get(str(pid)))
                   if isinstance(skip_pks, dict) else None) or ()
        for item in pj.remaining_tasks(project, invested_map.get(pid) or {},
                                       now=now, exclude_pks=excluded,
                                       rotation=rotation,
                                       derived_budget=need,
                                       skip_pks=skipped):
            if budget <= 0:
                break
            minutes = int(item.get("minutes") or 0)
            if not minutes and int(item.get("remaining") or 0):
                minutes = int(item.get("remaining") or 0) * MINUTES_PER_COUNT
            priority = pj.task_priority_of(item)
            rec = {"project_id": pid,
                   "project": project,
                   "pk": item["pk"],
                   "title": item.get("title") or item["pk"],
                   "count": int(item.get("count") or 0),
                   "total": int(item.get("total") or item.get("count") or 0),
                   "remaining": int(item.get("remaining") or 0)}
            if item.get("derived"):
                # 标题清单派生项：pk 是合成 pk，plan 落盘时要按派生任务建。
                rec["derived"] = dict(item["derived"])
                rec["minutes"] = int(item.get("minutes") or 0)
            if priority is not None:
                rec["priority"] = priority
            if item.get("children"):
                # 父任务壳：真正的 session 内容是子任务。这里把整套剩余子任务
                # 交给 schedule：能整条放就整条放，整条超过单场容量时才按
                # session 容量拆成多个 part。
                take = min(int(item.get("remaining") or 0), need)
                if take <= 0:
                    continue
                children = []
                for child in item.get("children") or []:
                    child = dict(child)
                    child_priority = pj.task_priority_of(child)
                    if child_priority is None and priority is not None:
                        child_priority = priority
                    if child_priority is not None:
                        child["priority"] = child_priority
                    children.append(child)
                rec.update({
                    "count": take,
                    "shell": True,
                    "children": children,
                    "duration": int(item.get("duration") or 0),
                    "budget_minutes": min(budget, take * MINUTES_PER_COUNT),
                })
                rec["task_type"] = task_type
                out.append(rec)
                need -= take
                budget -= take * MINUTES_PER_COUNT
                took += take
                continue
            if 0 < minutes < MINUTES_PER_COUNT:
                # 短任务：按实际分钟吃预算，任务不可拆。标题里的 count 仍保留
                # ceil(duration/25)（如 10 分钟 = 1 count），但 schedule 会按
                # 实际分钟把几条短任务叠进同一场。
                if minutes > budget:
                    continue
                rec.update({
                    "count": 1,
                    "minutes": minutes,
                    "small": True,
                    "budget_minutes": minutes,
                    "task_type": task_type,
                })
                out.append(rec)
                budget -= minutes
                took += 1
                continue
            # 任务是一个原子**单位**：这一次分到多少 count 就投多少（最高到它
            # 还剩的量），但绝不为了「对齐预算」把它切成 2+1 两条 —— 切分只发生在
            # 「任务比一场 session 还长」的时候，由放置阶段按 75 分钟一段推进。
            whole = int(item.get("remaining") or 0)
            if whole <= 0:
                continue
            take = min(whole, need, max(0, budget // MINUTES_PER_COUNT))
            if take <= 0:
                continue
            rec.update({
                "count": take,
                "minutes": minutes if take == whole else take * MINUTES_PER_COUNT,
                "task_type": task_type,
            })
            out.append(rec)
            need -= take
            budget -= take * MINUTES_PER_COUNT
            took += take
        if need > 0:
            used_count = ts.minutes_to_count(need * MINUTES_PER_COUNT - budget) or 0
            if budget < MINUTES_PER_COUNT:
                notes.append("「%s」分到 %d count = %d 分钟；短任务按实际分钟只塞下 "
                             "%d 分钟（%d count），还剩 %d 分钟对不齐 25 分钟的 count 格子。"
                             % (project.get("title") or pid, need,
                                need * MINUTES_PER_COUNT,
                                need * MINUTES_PER_COUNT - budget, used_count, budget))
            else:
                notes.append("「%s」的任务只剩 %d count，少排 %d（清单里没有更多可干的了）"
                             % (project.get("title") or pid, used_count,
                                ts.minutes_to_count(budget) or 0))
    return out, notes


# ---------------------------------------------------------------- 排任务


def derived_fields(info, pk):
    """plan 落盘时派生任务的本地字段（``info`` = 派生项里的 ``derived``）。"""
    import derived as _dv
    info = info or {}
    return {_dv.F_DERIVED_LINE: int(info.get("line")),
            _dv.F_DERIVED_TEXT: info.get("text") or "",
            _dv.F_DERIVED_PARENT_PK: info.get("parent_pk") or "",
            _dv.F_DERIVED_PARENT_TITLE: info.get("parent_title") or "",
            "project_task": pk}


def derived_source(info):
    """plan 落盘时派生任务的 ``source``（没有 OmniFocus 关联）。"""
    import derived as _dv
    info = info or {}
    return {"kind": _dv.DERIVED_KIND,
            "line": int(info.get("line")),
            "text": info.get("text") or "",
            "pk": info.get("parent_pk") or "",
            "parent_title": info.get("parent_title") or ""}


def _is_sequential_item(item):
    item = item or {}
    project = item.get("project") or {}
    mode = (item.get("task_type")
            or project.get("task_type")
            or item.get("schedule_mode"))
    return pj.normalize_task_type(mode) == pj.TASK_TYPE_SEQUENTIAL


# ---------------------------------------------------------------- 问人



# ---------------------------------------------------------------- period-first 排期
#
# 用户口径（2026-11-11）：
#   - 日历上的 period 是硬约束，总长度不变；
#   - period 内可装的 session 由任务决定；
#   - 1 count = 25 分钟工作，但一场 session 实际占 30 分钟（25 + 5 休息）；
#     50 工作占 60，75 工作占 90；
#   - 同一个 session 最多一个 >=25 分钟的任务；<25 分钟的小任务可以叠进去；
#   - parallel / sequential 的任务顺序仍然各自跑，只是排序单位从 session 变成 period。

def _ceil_session_minutes(minutes):
    """任务分钟 → Focusmate 可预约的工作分钟（25/50/75）。"""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return None
    for size in (25, 50, 75):
        if minutes <= size:
            return size
    return None


def _period_total_minutes(period):
    got_s = sp.parse_hhmm((period or {}).get("start"))
    got_e = sp.parse_hhmm((period or {}).get("end"))
    if got_s is None or got_e is None:
        return 0
    start = got_s[0] * 60 + got_s[1]
    end = got_e[0] * 60 + got_e[1]
    if end <= start:
        end += 24 * 60
    return max(0, end - start)


def _period_blocks(period):
    """period 里能放几个 25 分钟工作块（每块连同休息占 30 分钟）。"""
    return max(0, _period_total_minutes(period) // 30)


def _period_key(period):
    return (str(period.get("date") or ""), str(period.get("id") or ""),
            str(period.get("start") or ""), str(period.get("end") or ""))


def _auto_used_blocks(groups):
    total = 0
    for group in groups or ():
        size = _ceil_session_minutes(group.get("work"))
        if size:
            total += size // MINUTES_PER_COUNT
    return total


def _new_auto_group():
    return {"capacity": None, "work": 0, "has_long": False, "chunks": [],
            "places": [], "slot": None}


def _new_fixed_group(slot):
    return {"capacity": int((slot or {}).get("duration") or 0),
            "work": 0, "has_long": False, "chunks": [], "places": [],
            "slot": slot}


def _group_remaining(group):
    if group.get("capacity") is None:
        return max(0, 75 - int(group.get("work") or 0))
    return max(0, int(group["capacity"]) - int(group.get("work") or 0))


def _auto_can_hold(period, groups, group, add_minutes, is_long):
    add_minutes = int(add_minutes or 0)
    if add_minutes <= 0:
        return False
    if is_long and group.get("has_long"):
        return False
    if int(group.get("work") or 0) + add_minutes > 75:
        return False
    before = _auto_used_blocks(groups)
    old_size = _ceil_session_minutes(group.get("work")) or 0
    new_size = _ceil_session_minutes(int(group.get("work") or 0) + add_minutes) or 0
    after = before - (old_size // MINUTES_PER_COUNT) + (new_size // MINUTES_PER_COUNT)
    return after <= int(period.get("blocks") or 0)


def _auto_can_new(period, groups, add_minutes):
    size = _ceil_session_minutes(add_minutes)
    if not size:
        return False
    return _auto_used_blocks(groups) + size // MINUTES_PER_COUNT <= int(period.get("blocks") or 0)


def _commit_chunk(group, unit, minutes):
    """把「哪个 unit 在这组里放了多少分钟」记在**组自己**身上。

    ``group["chunks"]`` 只存分钟（容量/拼 session 用），``group["places"]``
    额外带 unit。以前 placement 是拿 period 级的扁平 ``chunks`` 顺序去和
    group 顺序对位，可两者根本不是同一个顺序：小任务会塞进前面还剩空间的
    session（比如 15:15），大任务去后面的 75 分钟 session（16:15），扁平
    顺序就变成 [大@14:00, 大@16:15, 小@15:15]，对位把两条任务记到了错的
    场次上（2026-11-16 的「绑定和 plan 不一致」就是这个）。
    """
    group["chunks"].append(minutes)
    group.setdefault("places", []).append((unit, minutes))
    return minutes


def _add_chunk(period, groups, minutes, is_long, split_ok=False, unit=None):
    """把一个任务块放进某个 period 的 session 组。返回实际放下的分钟数。"""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return 0
    auto = bool(period.get("auto"))
    if not auto:
        # 手动 layout：session 长度固定，任务只能往里填。
        if is_long:
            # 只找能**整条**放下的空 session；放不下就不排。
            for group in groups:
                if group.get("has_long"):
                    continue
                if _group_remaining(group) >= minutes:
                    group["work"] += minutes
                    group["has_long"] = True
                    return _commit_chunk(group, unit, minutes)
            if split_ok:
                # **优先整条**；实在没有任何一场装得下，才把这条任务切一段
                # 塞进最早还有空位的场次（用户 2026-11-17：「不要删除整条放
                # 不下就拆段填缝系统，只是优先放整条，如果实在没有就找零碎的
                # 地方」）。这条只在 parallel 的「整条放置全试过之后」才会走到。
                for group in groups:
                    if group.get("has_long"):
                        continue
                    room = _group_remaining(group)
                    if room >= MINUTES_PER_COUNT:
                        take = min(room, minutes)
                        group["work"] += take
                        group["has_long"] = True
                        return _commit_chunk(group, unit, take)
            return 0
        # 小任务：优先塞进已有 session 的剩余工作分钟。
        for group in groups:
            room = _group_remaining(group)
            if room >= minutes:
                group["work"] += minutes
                return _commit_chunk(group, unit, minutes)
        # 空 session 也算已有；如果没有空位，就说明这个 period 的手动 layout 已满。
        return 0

    # 自动 layout：先尽量塞进已有小空位；否则新建一个 session 组。
    if is_long:
        # 一个 group 只能有一个 >=25 的任务；先找只有小任务的组。
        for group in groups:
            if not group.get("has_long") and _auto_can_hold(period, groups, group, minutes, True):
                group["work"] += minutes
                group["has_long"] = True
                return _commit_chunk(group, unit, minutes)
        if _auto_can_new(period, groups, minutes):
            group = _new_auto_group()
            group["work"] = minutes
            group["has_long"] = True
            groups.append(group)
            return _commit_chunk(group, unit, minutes)
        if split_ok:
            # 同上：整条放不下才拆。按剩余块数切一个能装进去的最大块
            # （75/50/25），余下的下一轮再找零碎位置。
            room_blocks = int(period.get("blocks") or 0) - _auto_used_blocks(groups)
            max_work = max(0, room_blocks) * MINUTES_PER_COUNT
            for size in (75, 50, 25):
                if size <= minutes and size <= max_work:
                    group = _new_auto_group()
                    group["work"] = size
                    group["has_long"] = True
                    groups.append(group)
                    return _commit_chunk(group, unit, size)
        return 0

    # 小任务
    for group in groups:
        if _auto_can_hold(period, groups, group, minutes, False):
            group["work"] += minutes
            return _commit_chunk(group, unit, minutes)
    if _auto_can_new(period, groups, minutes):
        group = _new_auto_group()
        group["work"] = minutes
        groups.append(group)
        return _commit_chunk(group, unit, minutes)
    return 0


def _task_units(items):
    """把 distribute() 的任务/子任务摊成可排期的单元。"""
    units = []
    for item in items or ():
        item = dict(item)
        children = [dict(c) for c in item.get("children") or ()]
        if children:
            left = int(item.get("budget_minutes") or item.get("duration") or 0)
            if left <= 0:
                left = sum(int(c.get("duration") or 0) for c in children)
            for child in children:
                if left <= 0:
                    break
                full = int(child.get("duration") or 0)
                take = min(full, left)
                if take <= 0:
                    continue
                # 只剩一个零头预算时，不要从下一条大任务上切出 2 分钟碎片。
                if take < full and take < MINUTES_PER_COUNT:
                    break
                rec = dict(item)
                rec.update(child)
                rec["duration"] = take
                rec["count"] = int(child.get("count") or 0) or (ts.minutes_to_count(take) or 1)
                # 父任务壳的 total 是整套子任务的合计；落到 session 的 part 必须
                # 记**这个子任务自己**的原总 count，否则界面会把 3 count 显示成 6。
                rec["total"] = (int(child.get("count_total") or 0)
                                or int(child.get("count") or 0)
                                or (ts.minutes_to_count(full) or 0))
                units.append({"item": item, "rec": rec, "minutes": take,
                              "remaining": take, "original": take,
                              "long": take >= MINUTES_PER_COUNT,
                              "parent": item, "parts": 0})
                left -= take
            continue
        minutes = int(item.get("minutes") or 0)
        if not minutes:
            minutes = int(item.get("duration") or 0)
        if not minutes:
            minutes = int(item.get("count") or 0) * MINUTES_PER_COUNT
        if minutes <= 0:
            continue
        units.append({"item": item, "rec": item, "minutes": minutes,
                      "remaining": minutes, "original": minutes,
                      "long": minutes >= MINUTES_PER_COUNT,
                      "parent": None, "parts": 0})
    return units


def _place_sequential(units, periods):
    pi = 0
    for unit in units:
        while unit["remaining"] > 0 and pi < len(periods):
            period = periods[pi]
            chunk = min(int(unit["remaining"]), 75)
            placed = _add_chunk(period, period["groups"], chunk,
                                chunk >= MINUTES_PER_COUNT, split_ok=True,
                                unit=unit)
            if placed:
                unit["remaining"] -= placed
                continue
            pi += 1
        if unit["remaining"] > 0:
            unit["unscheduled"] = True


def _unit_can_whole(period, unit):
    remaining = int(unit.get("remaining") or 0)
    if remaining <= 0:
        return False
    chunk = min(remaining, 75)
    is_long = chunk >= MINUTES_PER_COUNT
    for group in period.get("groups") or ():
        if period.get("auto"):
            # 自动 layout 先看新建/塞进小任务组的可能性；真正放置仍走 _add_chunk。
            if is_long and group.get("has_long"):
                continue
            if is_long and _auto_can_hold(period, period["groups"], group, chunk, True):
                return True
            if not is_long and _auto_can_hold(period, period["groups"], group, chunk, False):
                return True
        else:
            if is_long and group.get("has_long"):
                continue
            if _group_remaining(group) >= chunk:
                return True
    if period.get("auto"):
        return _auto_can_new(period, period["groups"], chunk)
    return False


def _place_parallel(units, periods):
    for period in periods:
        moved = True
        while moved:
            moved = False
            for unit in units:
                if int(unit.get("remaining") or 0) <= 0:
                    continue
                chunk = min(int(unit["remaining"]), 75)
                if chunk >= MINUTES_PER_COUNT:
                    # 整条放得下才放；放不下留给后面的 period，避免 parallel 无缘无故拆任务。
                    if not _unit_can_whole(period, unit):
                        continue
                placed = _add_chunk(period, period["groups"], chunk,
                                    chunk >= MINUTES_PER_COUNT, split_ok=False,
                                    unit=unit)
                if not placed:
                    continue
                unit["remaining"] -= placed
                moved = True
                break
    for unit in units:
        if int(unit.get("remaining") or 0) > 0:
            unit["unscheduled"] = True


def _periods_from_slots(slots):
    """把当前 plan 的 session slots 汇总成 period 列表。"""
    grouped = {}
    order = []
    for slot in slots or ():
        period = dict(slot.get("period") or {})
        if not period:
            period = {"date": slot.get("date"), "id": slot.get("period_id") or "",
                      "start": slot.get("period_start") or slot.get("start"),
                      "end": slot.get("period_end") or slot.get("end")}
        key = _period_key(period)
        if key not in grouped:
            period["date"] = period.get("date") or slot.get("date")
            period["blocks"] = int(slot.get("period_blocks")
                                   or _period_blocks(period))
            period["total_minutes"] = int(slot.get("period_total_minutes")
                                         or _period_total_minutes(period))
            grouped[key] = {"period": period,
                            "auto": bool(slot.get("period_auto")),
                            "slots": [], "groups": [],
                            "append": False, "append_prefix": None}
            order.append(key)
        grouped[key]["slots"].append(slot)
        if slot.get("period_auto"):
            grouped[key]["auto"] = True
        if slot.get("append"):
            # 尾巴追加：这一段不是自由重切，而是「前面原样保留 + 往尾巴加」。
            grouped[key]["append"] = True
            if grouped[key]["append_prefix"] is None:
                grouped[key]["append_prefix"] = list(slot.get("append_prefix") or [])
    out = []
    for key in order:
        rec = grouped[key]
        rec["period"]["auto"] = bool(rec["auto"])
        rec["period"]["append"] = bool(rec.get("append"))
        rec["period"]["append_prefix"] = list(rec.get("append_prefix") or [])
        if rec["auto"]:
            rec["groups"] = []
        else:
            rec["groups"] = [_new_fixed_group(s) for s in rec["slots"]]
        out.append(rec["period"] | {"_groups": rec["groups"],
                                    "_slots": rec["slots"]})
    # 按时间顺序排 period：自动容量伪 slot 和尾巴缺口是后补进来的，不排一下
    # 18:00 的「整段」会排在 16:15 的真实 session 后面，任务就先分到晚的那段。
    def _start_min(period):
        got = sp.parse_hhmm((period or {}).get("start"))
        return got[0] * 60 + got[1] if got else 0
    out.sort(key=lambda period: (_start_min(period),
                                 str((period or {}).get("id") or "")))
    return out


def _append_pairs(period, groups, auto_layouts, notes):
    """尾巴追加：``prefix`` 原样保留，只把新 session 接在最后。

    返回 ``[(group, session), ...]``。固定 group（data.csv 里本来就有、只是还没
    挂任务的场次）按起点找回自己那一场；``append=True`` 的缺口 group 依次对应
    ``prefix`` 之后新切出来的场次。序列对不上就整段退回「只填现有场次」，不写
    layout —— 宁可少排，也不许把已经订上的场次挪位置。
    """
    prefix = list(period.get("append_prefix") or [])
    fixed = [g for g in groups if not (g.get("slot") or {}).get("append")]
    gaps = [g for g in groups if (g.get("slot") or {}).get("append")]
    new_durations = []
    for group in gaps:
        size = _ceil_session_minutes(group.get("work"))
        if size:
            new_durations.append(size)
    if not new_durations:
        return [(g, g.get("slot")) for g in fixed if g.get("slot")]
    durations = prefix + new_durations
    if sum(d // MINUTES_PER_COUNT for d in durations) > int(period.get("blocks") or 0):
        notes.append("「%s %s」尾巴追加超出 period 容量，已跳过"
                     % (period.get("id"), period.get("start")))
        return [(g, g.get("slot")) for g in fixed if g.get("slot")]
    try:
        sessions = sp.plan_period(period, durations)
    except ValueError as e:
        notes.append(str(e))
        return [(g, g.get("slot")) for g in fixed if g.get("slot")]
    by_start = {s["start"]: s for s in sessions}
    pairs = []
    for group in fixed:
        session = by_start.get(str((group.get("slot") or {}).get("start") or ""))
        if session is None:
            notes.append("「%s %s」的时间轴和 layout 对不上，这一段没有追加"
                         % (period.get("id"), period.get("start")))
            return [(g, g.get("slot")) for g in fixed if g.get("slot")]
        pairs.append((group, session))
    for group, session in zip(gaps, sessions[len(prefix):]):
        pairs.append((group, session))
    auto_layouts[_period_key(period)] = durations
    return pairs


def schedule_periods(items, slots):
    """period-first 排期：返回 ``(placements, unscheduled, notes, auto_layouts)``。

    ``slots`` 里带 ``period_auto=True`` 的 period 会忽略旧 session 长度，按任务
    重新生成 durations；手动 layout 的 period 保持原 session 长度，只往里填。
    """
    periods = _periods_from_slots(slots)
    for period in periods:
        # placement 的归属只由 ``group["places"]`` 决定；period 级不再维护一份
        # 扁平 (unit, minutes) 列表 —— 那份列表的顺序和 group 顺序不一致，
        # 正是 2026-11-16 那次「任务记到别的场次上」的根源。
        period["groups"] = period["_groups"]
        period["auto"] = bool(period.get("auto"))
    units = _task_units(items)
    sequential = [u for u in units
                  if _is_sequential_item(u.get("rec") or u.get("item") or {})]
    parallel_units = [u for u in units
                      if not _is_sequential_item(u.get("rec") or u.get("item") or {})]
    _place_sequential(sequential, periods)
    # priority tag（P1/P2）优先，同级按清单顺序 —— 这是以前 `_parallel_order_key`
    # 的规矩，收进这条唯一流水线后仍然要成立。
    def _unit_priority_key(pair):
        index, unit = pair
        rec = unit.get("rec") or unit.get("item") or {}
        pr = pj.task_priority_of(rec)
        return (0, pr, index) if pr is not None else (1, 0, index)

    parallel_units = [unit for _i, unit in sorted(
        enumerate(parallel_units), key=_unit_priority_key)]
    _place_parallel(parallel_units, periods)
    # parallel 的原则是「能整条放就不拆」；扫完所有 period 仍有整条放不下的
    # 任务，再做一次顺序拆段兜底 —— 优先整条、实在没有才找零碎位置
    # （用户 2026-11-17 明确要求保留这套填缝逻辑）。
    leftovers = [u for u in parallel_units if int(u.get("remaining") or 0) > 0]
    if leftovers:
        _place_sequential(leftovers, periods)

    placements = []
    unscheduled = []
    auto_layouts = {}
    notes = []
    for period in periods:
        groups = [g for g in period["groups"] if int(g.get("work") or 0) > 0]
        if period.get("auto"):
            durations = []
            for group in groups:
                size = _ceil_session_minutes(group.get("work"))
                if size:
                    durations.append(size)
            # 再算一次总块数，防止小任务增长把 period 撑爆。
            if sum(d // MINUTES_PER_COUNT for d in durations) > int(period.get("blocks") or 0):
                notes.append("「%s %s」自动排布超出 period 容量，已丢弃多余 session"
                             % (period.get("id"), period.get("start")))
                durations = durations[:int(period.get("blocks") or 0)]
            auto_layouts[_period_key(period)] = durations
            try:
                sessions = sp.plan_period(period, durations)
            except ValueError as e:
                notes.append(str(e))
                sessions = []
            pairs = list(zip(groups, sessions))
        elif period.get("append"):
            pairs = _append_pairs(period, groups, auto_layouts, notes)
        else:
            sessions = [g.get("slot") for g in groups if g.get("slot")]
            # 手动 layout 的 session 与 group 一一对应。
            pairs = list(zip(groups, sessions))
        for group, session in pairs:
            places = list(group.get("places") or ())
            n = len(places)
            for unit, minutes in places:
                rec = unit["rec"]
                unit["parts"] += 1
                placements.append({
                    "item": rec,
                    "slot": session,
                    "count": (int(minutes) // MINUTES_PER_COUNT
                              if int(minutes) % MINUTES_PER_COUNT == 0
                              else (ts.minutes_to_count(minutes) or 1)),
                    "duration": int(minutes),
                    "by_minutes": int(minutes) % MINUTES_PER_COUNT != 0,
                    "split": False,
                    "stacked": n > 1,
                    "parent": unit.get("parent"),
                    "period_id": period.get("id"),
                    "_unit": unit,
                })
        # 没放下的 unit 只报一次。
    for unit in units:
        if int(unit.get("remaining") or 0) > 0:
            rec = dict(unit["rec"])
            rec["count"] = ts.minutes_to_count(unit["remaining"]) or 0
            rec["remaining_count"] = rec["count"]
            unscheduled.append(rec)
    # 所有 part 都标成 split（不能只有后一段标，否则界面上第一段看不出是拆的）。
    for place in placements:
        unit = place.pop("_unit", None)
        place["split"] = bool(unit and unit.get("parts", 0) > 1)
    # 拆分说明
    for unit in units:
        if unit["parts"] > 1:
            notes.append("「%s」%d 分钟按 period 顺序拆成 %d 段"
                         % ((unit["rec"].get("title") or unit["rec"].get("pk")),
                            unit["original"], unit["parts"]))
    return placements, unscheduled, notes, auto_layouts


class Allocator(object):
    """``plan`` 的问答状态机：先分资源（count），按到 ``complete`` 再排任务。

    输入（用户原话的语法）::

        task 1 4      ← 项目 #1 分 4 个 count（4 count = 100 分钟）
        task 1 2      ← 再敲一次是**覆盖**（变成 2，不是 6）
        task 1 clear  ← 清空 #1 的分配
        task 1        ← 只看 #1 的清单，不动分配
        complete      ← 分配完了，开始排任务
        q             ← 放弃这次 plan（一个字节都不落）

    超预算（今天分不出这么多 count）**整条命令都不执行**：报错、状态不变。
    """

    def __init__(self, day, projects, slots, task_store=None, hooks=None,
                 stage=None, incubator_projects=None, pro_map=None):
        self.day = day
        # 现在一次 plan 里可能同时有不同项目阶段对应的 op（一个 running 的 SOP、
        # 一个 bottleneck 的 incubator），所以不再把整场 plan 标成某个全局阶段。
        types = {pj.normalize_op_type((p or {}).get("op_type"))
                 for p in (projects or [])}
        types.discard(None)
        self.stage = next(iter(types)) if len(types) == 1 else "mixed"
        self.projects = list(projects or [])
        # 兼容旧调用方：参数还收着，但不再触发 SOP → incubator 的第二阶段。
        self.incubator_projects = list(incubator_projects or [])
        self.pro_map = dict(pro_map or {})
        self.slots = [dict(s) for s in slots or []]
        self.task_store = task_store
        self.hooks = hooks or {}
        self.allocations = []          # [{"project_id","count"}]，顺序 = 排的顺序
        self.closed = False
        self.notes = []
        # 和 Review / NostartReview 对齐的字段名（画帧缓存通用）
        self.pending_task = None
        self.index = 0

    # -- 小工具 -------------------------------------------------------
    def say(self, text, kind="info"):
        hook = self.hooks.get("say")
        if hook:
            hook(text, kind)
        self.notes.append((kind, text))

    def _hook(self, name, *args, **kwargs):
        fn = self.hooks.get(name)
        return fn(*args, **kwargs) if fn else None

    def cap(self):
        return capacity(self.slots)

    def allocated(self):
        return sum(int(a.get("count") or 0) for a in self.allocations)

    def free_counts(self):
        return max(0, self.cap()["counts"] - self.allocated())

    def allocation_of(self, project_id):
        for alloc in self.allocations:
            if alloc.get("project_id") == project_id:
                return int(alloc.get("count") or 0)
        return 0

    def titles(self):
        """``{project_id: 名字}``（排任务时给消息用）。"""
        return {p["id"]: p.get("title") or p["id"] for p in self.projects}

    def pending(self):
        return not self.closed

    # -- 给界面用的文字 -----------------------------------------------
    def headline(self):
        return "%s 项目资源分配（%d 个当前 op · %d 个空场 = %d count）" % (
            self.day.isoformat() if hasattr(self.day, "isoformat") else self.day,
            len(self.projects), self.cap()["sessions"], self.cap()["counts"])

    def options(self):
        return ["task <序号> <count>", "task <序号> clear", "complete", "q"]

    def stage_hint(self):
        return ("task 1 4 = 第 1 个当前 op 分 4 count · task 1 clear = 清空 · "
                "complete = 开始排任务 · q = 放弃（已分配 %d / %d count）"
                % (self.allocated(), self.cap()["counts"]))

    def lines(self):
        """面板正文：[(文本, 样式)]。"""
        cap = self.cap()
        out = [("🗂 %s" % self.headline(), "bold cyan"),
               ("  每个项目只显示当前阶段对应的 op：running → SOP / "
                "bottleneck → incubator", "dim"),
               ("  空闲 %s" % capacity_text(cap), "dim"),
               ("  空场 %s" % slots_text(self.slots), "dim")]
        if not self.projects:
            out.append(("  （没有关联真正项目且阶段匹配的 op）", "yellow"))
        invested_all = {}
        if self.task_store is not None:
            invested_all = pj.invested_by_project(self.task_store)
        for index, project in enumerate(self.projects, 1):
            plan = pj.plan_of(project, self.day)
            stat = pj.stats(project, self.task_store, invested_all.get(project["id"]) or {})
            got = self.allocation_of(project["id"])
            op_kind = pj.normalize_op_type(project.get("op_type")) or pj.OP_TYPE_SOP
            pro = self.pro_map.get(project["id"])
            pro_title = ((pro or {}).get("display_title")
                         or (pro or {}).get("title"))
            pro_txt = (" · pro:%s(%s)" % (pro_title, pro.get("stage"))
                       if pro else "")
            bits = ["[%s · %s%s]" % (op_kind, pj.task_type_of(project), pro_txt),
                    "剩余 %d count" % stat["remaining"],
                    "进度 %s" % stat["progress_text"]]
            if plan and plan.get("count"):
                bits.append("今天说好 %d count、已排 %d"
                            % (int(plan["count"]), pj.planned_counts(project, self.day)))
            head = "#%d %s" % (index, project.get("title") or "?")
            tail = "→ %d count" % got if got else ""
            out.append(("  %-24s %s %s" % (head, " · ".join(bits), tail),
                        "bold green" if got else ""))
        out.append(("  已分配 %d / %d count（还空 %d）"
                    % (self.allocated(), cap["counts"], self.free_counts()), "cyan"))
        out.append((self.stage_hint(), "cyan"))
        return out

    # -- 输入 ---------------------------------------------------------
    def handle(self, line):
        """吃一行输入：``{"action": "handled"|"close"|"dispatch"}``。"""
        raw = str(line or "").strip()
        if not raw:
            self.say(self.stage_hint(), "info")
            return {"action": "handled"}
        parts = raw.split()
        head = parts[0].lower()
        if head in HELP_TOKENS:
            self.say("task <序号> <count>（或 op <序号> <count>）：给第几个当前 op "
                     "分几个 count（1 count = 25 分钟）；再敲一次是覆盖。", "info")
            self.say("task <序号> clear：清空这个项目的分配；"
                     "task <序号>：只看清单。", "info")
            self.say("complete：分配完了，开始挑任务、排进 session；q：放弃这次 plan。",
                     "info")
            return {"action": "handled"}
        if head in COMPLETE_TOKENS:
            return self._complete()
        if head in CANCEL_TOKENS:
            return self._cancel()
        if head in ("task", "op"):
            return self._task_command(parts[1:])
        if head == "clear":
            return self._task_command(["--all", "clear"])
        if head in DISPATCH_TOKENS:
            return {"action": "dispatch", "line": raw}
        self.say("认不出 %r；task 1 4 / task 1 clear / complete / q（? 看全部）" % raw,
                 "warn")
        return {"action": "handled"}

    def _find(self, token):
        raw = str(token or "").strip()
        try:
            ident = int(raw)
        except (TypeError, ValueError):
            ident = None
        if ident is not None and 1 <= ident <= len(self.projects):
            return self.projects[ident - 1]
        raise ValueError("项目序号 %r 不在范围里：#1–#%d" % ((raw, len(self.projects))
                                                             if self.projects
                                                             else (raw, 0)))

    def _task_command(self, toks):
        toks = [str(t) for t in toks if str(t).strip()]
        if not toks:
            self.say("要哪个项目？task <序号> <count>；序号 = 上面那张表的 #。", "warn")
            return {"action": "handled"}
        if toks[0] in ("--all", "all", "全部"):
            if self.allocations:
                self.allocations = []
                self.say("🧹 分配清空了（0 count）。", "info")
            else:
                self.say("本来就没分配。", "info")
            return {"action": "handled"}
        try:
            project = self._find(toks[0])
        except ValueError as e:
            self.say(str(e), "warn")
            return {"action": "handled"}
        rest = toks[1:]
        if not rest:
            return self._show(project)
        word = rest[0].lower()
        if word in ("clear", "清空", "none", "off", "0"):
            self._set(project, 0)
            return {"action": "handled"}
        try:
            count = int(rest[0])
        except ValueError:
            self.say("count 得是整数（1 count = 25 分钟）；你给的是 %r" % rest[0], "warn")
            return {"action": "handled"}
        self._set(project, count)
        return {"action": "handled"}

    def _queue_exclusions(self, project):
        """这个项目里不该再进 plan 候选队列的 pk。

        formatter 任务已经有自己的时间占位；defer 未到点的任务现在还不能做。
        项目 plan 的分配预览和真正 distribute 必须看同一份队列。
        """
        if self.task_store is None:
            return set()
        out = set()
        now = datetime.datetime.now()
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if not pk or str(pid) != str(project.get("id")):
                continue
            if not ts.TaskStore.is_open(task):
                continue
            if task.get("formatter") or ts.task_is_deferred(task, now=now):
                out.add(str(pk))
        return out

    def _show(self, project):
        invested = (pj.invested_by_project(self.task_store).get(project["id"]) or {}
                    if self.task_store else {})
        items = pj.remaining_tasks(project, invested,
                                   exclude_pks=self._queue_exclusions(project))
        stat = pj.stats(project, self.task_store, invested)
        self.say("📁 #%d %s：剩余 %d count（进度 %s），分到 %d count"
                 % (self.projects.index(project) + 1, project.get("title"),
                    stat["remaining"], stat["progress_text"],
                    self.allocation_of(project["id"])), "info")
        for item in items[:8]:
            pri = pj.priority_label(pj.task_priority_of(item))
            self.say("   · %s%s（%d count，还剩 %d）"
                     % (("[%s] " % pri) if pri else "",
                        item.get("title"), int(item.get("count") or 0),
                        int(item.get("remaining") or 0)), "info")
        if len(items) > 8:
            self.say("   …还有 %d 条" % (len(items) - 8), "info")
        return {"action": "handled"}

    def _set(self, project, count):
        """设一个项目的分配（**覆盖**）；超预算就整条命令都不执行。"""
        count = int(count)
        if count < 0:
            self.say("count 不能是负数。", "warn")
            return False
        others = sum(int(a.get("count") or 0) for a in self.allocations
                     if a.get("project_id") != project["id"])
        total = others + count
        cap = self.cap()["counts"]
        if total > cap:
            self.say("✗ 超预算：这一天只有 %d count 的空场，别的项目已经占了 %d，"
                     "「%s」再要 %d 就成 %d —— 这条命令没执行（分配一个字都没动）。"
                     "现在还能给它是 %d count。"
                     % (cap, others, project.get("title"), count, total,
                        max(0, cap - others)), "error")
            return False
        self.allocations = [a for a in self.allocations if a.get("project_id") != project["id"]]
        if count:
            self.allocations.append({"project_id": project["id"], "count": count})
        num = self.projects.index(project) + 1
        if count:
            self.say("✅ #%d %s → %d count（%d 分钟；含休息 %d 分钟）。已分配 %d / %d。"
                     % (num, project.get("title"), count,
                        count * MINUTES_PER_COUNT,
                        count * (MINUTES_PER_COUNT + BREAK_MINUTES),
                        self.allocated(), cap), "ok")
        else:
            self.say("✅ #%d %s 的分配清空了。已分配 %d / %d。"
                     % (num, project.get("title"), self.allocated(), cap), "ok")
        return True

    # -- 收尾 ---------------------------------------------------------
    def _complete(self):
        if not self.allocations:
            self.say("还没有任何分配 —— 先 `task 1 4`，或者 `q` 放弃这次 plan。", "warn")
            return {"action": "handled"}
        allocs = []
        for alloc in self.allocations:
            project = next((p for p in self.projects
                            if p["id"] == alloc.get("project_id")), None)
            if project is None:
                continue
            allocs.append({"project_id": project["id"], "project": project,
                           "count": int(alloc.get("count") or 0)})
        summary = self._hook("apply", self, allocs) or {}
        self.closed = True
        return {"action": "close", "done": "complete",
                "summary": summary, "allocations": allocs}

    def _cancel(self):
        self.closed = True
        self.say("⏹ 这次 plan 放弃：没有分配、也没有建任何任务。", "info")
        return {"action": "close", "done": "cancel"}
