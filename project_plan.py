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

## 排任务的两条原则（用户给的原话）

1. **尽量让每个任务占有自己的独立 session** —— 一场 session 只放一个任务；
2. **尽量让每个任务连续** —— 不把一条任务拆成「这一场 2 个 count、下一场 1 个」。

于是排法是「先求不拆的完美配对，配不上才拆」：

- 把任务（要几个 count）和场次（能装几个 count）摆成二分图，跑一次最大匹配
  （:func:`match_tasks_to_slots`，Kuhn 增广路）——**有不用拆的解就一定找到**，
  而且按「浪费最小」挑场次；
- 配不上的任务（比所有场次都长，或者场次数量不够）才拆开填，并且**明说是拆的**；
- 顺序上按项目预览表格的顺序、项目内按 OmniFocus 清单顺序 —— 稳定、可预期。

这个模块不联网、不依赖 rich；warden / 测试只调这里。
"""
from __future__ import annotations

import datetime

import project_store as pj
import task_store as ts

MINUTES_PER_COUNT = ts.MINUTES_PER_COUNT
BREAK_MINUTES = 5

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


def distribute(allocations, projects, invested_map=None, now=None, exclude_pks=None):
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
        for item in pj.remaining_tasks(project, invested_map.get(pid) or {},
                                       now=now, exclude_pks=excluded):
            if budget <= 0:
                break
            minutes = int(item.get("minutes") or 0)
            if not minutes and int(item.get("remaining") or 0):
                minutes = int(item.get("remaining") or 0) * MINUTES_PER_COUNT
            rec = {"project_id": pid,
                   "project": project,
                   "pk": item["pk"],
                   "title": item.get("title") or item["pk"],
                   "count": int(item.get("count") or 0),
                   "total": int(item.get("count") or 0),
                   "remaining": int(item.get("remaining") or 0)}
            if item.get("children"):
                # 父任务壳：真正的 session 内容是子任务。这里把整套剩余子任务
                # 交给 schedule：能整条放就整条放，整条超过单场容量时才按
                # session 容量拆成多个 part。
                take = min(int(item.get("remaining") or 0), need)
                if take <= 0:
                    continue
                rec.update({
                    "count": take,
                    "shell": True,
                    "children": [dict(child) for child in item.get("children") or []],
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
            take = min(int(item.get("remaining") or 0), need,
                       max(0, budget // MINUTES_PER_COUNT))
            if take <= 0:
                continue
            rec.update({
                "count": take,
                "minutes": minutes if take == int(item.get("remaining") or 0) else take * MINUTES_PER_COUNT,
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


def _match(items, slots):
    """Kuhn 增广路：任务 → 场次的一对一匹配（场次容量 >= 任务要的 count）。

    返回 ``{item_index: slot_index}``。没有完美匹配时返回能配上的那些 —— 配不上的
    由调用方拆开填。候选场次按「容量小的优先」试，浪费就小。
    """
    caps = [max(0, int(s.get("counts") or 0) - int(s.get("used") or 0))
            for s in slots]
    order = sorted(range(len(items)),
                   key=lambda i: (-int(items[i].get("count") or 0), i))
    candidates = {}
    for i in order:
        need = int(items[i].get("count") or 0)
        candidates[i] = sorted([j for j, cap in enumerate(caps) if cap >= need],
                               key=lambda j: (caps[j], j))
    owner = {}          # slot_index → item_index

    def try_assign(i, seen):
        for j in candidates.get(i) or []:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or try_assign(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in order:
        if int(items[i].get("count") or 0) <= 0:
            continue
        try_assign(i, set())
    return {i: j for j, i in owner.items()}


def _pick_child_subset(children, capacity_minutes):
    """0/1 子集和：选若干**整条**子任务，使总分钟数尽量贴近容量。

    这是「能整条放就不拆」时的辅助函数；整条放不下时由
    :func:`_schedule_children` 按 session 容量拆段。返回选中的 child 列表；
    没有能放下的就返回空列表。数量通常很小，用 sum→选择清单 的 DP 就够。
    """
    capacity = int(capacity_minutes or 0)
    if capacity <= 0:
        return []
    dp = {0: []}
    for child in children or ():
        try:
            minutes = int(child.get("duration") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0 or minutes > capacity:
            continue
        for total, chosen in list(dp.items()):
            nxt = total + minutes
            if nxt <= capacity and nxt not in dp:
                dp[nxt] = chosen + [child]
    if not dp:
        return []
    return dp[max(dp)]


def _select_chunk_slots(slots, target_minutes):
    """为一次拆段挑场次：总容量够 target，且**浪费最少**。

    不能只用「容量从大到小」硬切：例如目标 75 分钟、空场是 50 + 25 + 50，
    先拿两个 50 会切出 50 + 25，第二段被塞进 50 分钟的场（实际只干 25），
    收尾和 count 展示都会对不上。这里先找容量和最接近目标的场次组合，
    75 就会选 50 + 25 两个正好的场。
    """
    target = int(target_minutes or 0)
    free = [slot for slot in slots or ()
            if (int(slot.get("duration") or 0)
                - int(slot.get("used_minutes") or 0)) > 0]
    if target <= 0:
        return []
    dp = {0: []}
    for slot in free:
        minutes = (int(slot.get("duration") or 0)
                   - int(slot.get("used_minutes") or 0))
        if minutes <= 0:
            continue
        for total, chosen in list(dp.items()):
            nxt = total + minutes
            if nxt not in dp:
                dp[nxt] = chosen + [slot]
    candidates = [(total - target, len(chosen), total, chosen)
                  for total, chosen in dp.items() if total >= target]
    if not candidates:
        return []
    candidates.sort(key=lambda row: row[:3])
    chosen = candidates[0][3]
    # 同一组合里按时间顺序排，part 1/2 的顺序和真实场次一致。
    return sorted(chosen, key=lambda slot: (str(slot.get("start") or ""),
                                            int(slot.get("index") or 0)))


def _schedule_children(items, slots, placements, unscheduled, notes):
    """把带子任务的父任务壳按 session 实际分钟数排进表。

    子任务优先整条放进一场；只有当某条子任务的分钟数超过**任何**单场剩余容量，
    但所有剩余场次加起来又装得下它时，才把它拆成多段排进多场。这样：

    - 子任务能整装的场景保持原行为（不无故拆）；
    - 75 分钟的 action 遇到 50 + 25 分钟两场时，会拆成 50 + 25 两个 part；
    - 27 分钟子任务只有一个 25 分钟场时，不会硬塞或拆出一个无意义的 2 分钟头，
      而是整条留在 unscheduled。

    同一场只放同一个父任务套的子任务。返回处理完后仍可用的 slots。
    """
    group_items = [item for item in items if item.get("children")]
    for item in group_items:
        children = [dict(child) for child in item.get("children") or []
                    if int(child.get("duration") or 0) > 0]
        budget = int(item.get("budget_minutes") or 0)
        if budget <= 0:
            budget = sum(int(child.get("duration") or 0) for child in children)

        # 只把本次分配预算覆盖到的那部分子任务纳入排期。比如项目有两条
        # 75 分钟 action、但只分了 3 count（75 分钟），不该把第二条也报成
        # 「没地方放」。
        selected = []
        left_budget = budget
        for child in children:
            if left_budget <= 0:
                break
            full_minutes = int(child.get("duration") or 0)
            take = min(full_minutes, left_budget)
            if take <= 0:
                continue
            # 只剩一个零头预算时，不要从下一条大任务上切下 2 分钟这种
            # 无意义碎片；宁可少用几十分钟，等下次分配。
            if take < full_minutes and take < MINUTES_PER_COUNT:
                break
            rec = dict(child)
            rec["_full_minutes"] = full_minutes
            rec["_alloc_minutes"] = take
            selected.append(rec)
            left_budget -= take

        touched_slots = []
        pending = []

        def free_minutes(slot):
            return (int(slot.get("duration") or 0)
                    - int(slot.get("used_minutes") or 0))

        def add_part(child, slot, minutes, part_index=1, part_total=1):
            minutes = int(minutes or 0)
            if minutes <= 0:
                return None
            count = ts.minutes_to_count(minutes) or 1
            note = str(child.get("note") or "")
            if part_total > 1:
                part_note = "拆分段 %d/%d（%d 分钟）" % (part_index, part_total, minutes)
                note = (note + "\n" + part_note).strip() if note else part_note
            part_item = {
                "project_id": item["project_id"],
                "project": item["project"],
                "pk": child.get("pk"),
                "title": child.get("title") or child.get("pk"),
                "duration": minutes,
                "count": count,
                # 这一段只占 count 的一部分；原 OmniFocus 任务的完整 count
                # 用 total 一路带到本地任务，界面才能显示 ``(0+1/3)`` 而不是
                # 把每一段误当成独立的一条 ``(0+1/1)`` / ``(0+2/2)``。
                "total": int(child.get("count") or 0) or int(item.get("total") or 0),
                "note": note,
                "parent_pk": item.get("pk"),
                "parent_title": item.get("title") or item.get("pk"),
            }
            if part_total > 1:
                part_item["part_index"] = part_index
                part_item["part_total"] = part_total
            place = {
                "item": part_item,
                "slot": slot,
                "count": count,
                "duration": minutes,
                "split": part_total > 1,
                "stacked": False,
                "parent": item,
            }
            placements.append(place)
            slot["used_minutes"] = int(slot.get("used_minutes") or 0) + minutes
            if slot not in touched_slots:
                touched_slots.append(slot)
            return place

        # 第一轮：优先整条整条地放。选择「装得下且剩余最小」的场，减少浪费。
        for child in selected:
            target = int(child.get("_alloc_minutes") or 0)
            candidates = [slot for slot in slots
                          if slot.get("used", 0) < slot.get("counts", 0)
                          and free_minutes(slot) >= target]
            if not candidates:
                pending.append(child)
                continue
            slot = min(candidates, key=lambda s_: (free_minutes(s_) - target,
                                                   free_minutes(s_),
                                                   str(s_.get("start") or "")))
            add_part(child, slot, target)

        # 第二轮：整条放不下的，只有在所有剩余场次加起来足够时才拆。
        # 先处理长的，避免碎片化；不够就整条保留，不造无意义的超短 part。
        for child in sorted(pending,
                            key=lambda c_: -int(c_.get("_alloc_minutes") or 0)):
            target = int(child.get("_alloc_minutes") or 0)
            free_slots = [slot for slot in slots
                          if slot.get("used", 0) < slot.get("counts", 0)
                          and free_minutes(slot) > 0]
            chosen_slots = _select_chunk_slots(free_slots, target)
            if not chosen_slots:
                unscheduled.append(dict(item,
                                        count=ts.minutes_to_count(target) or 0,
                                        remaining_count=ts.minutes_to_count(target) or 0,
                                        children=[dict(child,
                                                       duration=target)]))
                notes.append("「%s」还有 %d 分钟没地方放（剩余场次总容量不够）"
                             % (child.get("title") or child.get("pk"), target))
                continue
            chunks = []
            remaining = target
            for slot in chosen_slots:
                if remaining <= 0:
                    break
                take = min(free_minutes(slot), remaining)
                if take <= 0:
                    continue
                chunks.append((slot, take))
                remaining -= take
            for index, (slot, minutes) in enumerate(chunks, 1):
                add_part(child, slot, minutes, part_index=index,
                         part_total=len(chunks))
            if len(chunks) > 1:
                notes.append("「%s」%d 分钟超过单场容量，拆成 %d 段：%s"
                             % (child.get("title") or child.get("pk"), target,
                                len(chunks),
                                " + ".join("%d" % int(minutes)
                                           for _slot, minutes in chunks)))

        # 分配预算只覆盖了子任务的一部分时说清楚；剩下部分不是「排失败」，
        # 只是本次没有分到资源。
        for child in selected:
            full = int(child.get("_full_minutes") or 0)
            alloc = int(child.get("_alloc_minutes") or 0)
            if full > alloc:
                notes.append("「%s」原 %d 分钟，本次只排前 %d 分钟（剩余 %d 分钟未分到本次资源）"
                             % (child.get("title") or child.get("pk"), full, alloc,
                                full - alloc))

        # 一场只放一个父任务套；宁可留白，也不把另一套的子任务混进同一场。
        for slot in touched_slots:
            slot["used"] = int(slot.get("counts") or 0)
        if len(touched_slots) > 1:
            notes.append("「%s」的子任务跨了 %d 场（按场次容量分组）"
                         % (item.get("title") or item.get("pk"), len(touched_slots)))
    return [slot for slot in slots
            if int(slot.get("used") or 0) < int(slot.get("counts") or 0)]

def _slot_free_counts(slot):
    return max(0, int((slot or {}).get("counts") or 0)
               - int((slot or {}).get("used") or 0))


def _slot_free_minutes(slot):
    return max(0, int((slot or {}).get("duration") or 0)
               - int((slot or {}).get("used_minutes") or 0))


def _ordered_slots(slots):
    """session 的时间顺序（顺序项目只认这个，不做最优匹配）。"""
    return sorted(slots or (),
                  key=lambda slot: (str(slot.get("start") or ""),
                                    int(slot.get("index") or 0)))


def _sequential_place_item(item, slots, placements, unscheduled, notes,
                           parent=None):
    """把一条任务按**时间顺序**塞进最早的 session。

    sequential 项目的核心：不找「最合适」的空场，也不为了少拆而跳过前面的
    小场。比如 50 分钟任务遇到 25 / 25 / 50 三场，就先拆成 25 + 25 填满
    前两场，第三场保持空闲。
    """
    item = dict(item or {})
    count = int(item.get("count") or 0)
    if count <= 0:
        return
    ordered = _ordered_slots(slots)

    # 短任务按真实分钟占位；仍然只往最早能放下的那一场走。
    if item.get("small") or (0 < int(item.get("minutes") or 0) < MINUTES_PER_COUNT):
        minutes = int(item.get("minutes") or 0)
        for slot in ordered:
            if _slot_free_minutes(slot) < minutes:
                continue
            stacked = int(slot.get("used_minutes") or 0) > 0
            slot["used_minutes"] = int(slot.get("used_minutes") or 0) + minutes
            used_count = ts.minutes_to_count(int(slot.get("used_minutes") or 0)) or 0
            slot["used"] = min(int(slot.get("counts") or 0), used_count)
            placements.append({
                "item": item, "slot": slot, "count": count,
                "duration": minutes, "split": False, "stacked": stacked,
                "by_minutes": True, "sequential": True, "parent": parent,
            })
            return
        unscheduled.append(item)
        notes.append("「%s」%d 分钟在顺序排期里没地方放（前面的场次都装不下）"
                     % (item.get("title") or item.get("pk"), minutes))
        return

    chunks = []
    need = count
    for slot in ordered:
        free = _slot_free_counts(slot)
        if free <= 0:
            continue
        take = min(free, need)
        chunks.append((slot, take))
        need -= take
        if need <= 0:
            break
    if not chunks:
        unscheduled.append(item)
        notes.append("「%s」在顺序排期里没地方放（没有剩余 count）"
                     % (item.get("title") or item.get("pk")))
        return

    want = count
    for index, (slot, take) in enumerate(chunks, 1):
        duration = take * MINUTES_PER_COUNT
        part = dict(item, count=take, duration=duration)
        if len(chunks) > 1:
            part["part_index"] = index
            part["part_total"] = len(chunks)
        slot["used"] = int(slot.get("used") or 0) + take
        slot["used_minutes"] = (int(slot.get("used_minutes") or 0)
                                + duration)
        placements.append({
            "item": part,
            "slot": slot,
            "count": take,
            "duration": duration,
            "split": len(chunks) > 1,
            "stacked": False,
            "sequential": True,
            "parent": parent,
        })
    if len(chunks) > 1:
        notes.append("「%s」%d count 按项目顺序拆成 %d 场（%s）"
                     % (item.get("title") or item.get("pk"), want, len(chunks),
                        "、".join("%s %d" % (slot.get("start"), take)
                                 for slot, take in chunks)))
    if need > 0:
        rest = dict(item, count=need,
                    title=item.get("title") or item.get("pk"))
        unscheduled.append(rest)
        notes.append("「%s」还差 %d count 没地方放（顺序排期不跳场）"
                     % (item.get("title") or item.get("pk"), need))


def _schedule_sequential(items, slots, placements, unscheduled, notes):
    """按项目顺序填 session（Sequential 项目规则）。

    项目内的任务顺序就是 OmniFocus 清单顺序；每个任务从最早还有空位的
    session 开始，装不下就顺着下一场继续，绝不为了「少拆」跳到后面的大场。
    父任务壳仍以子任务为真实排期单位，但同样按清单顺序往前走。
    """
    for item in items or ():
        children = [dict(child) for child in item.get("children") or ()
                    if int(child.get("duration") or 0) > 0]
        if not children:
            _sequential_place_item(item, slots, placements, unscheduled, notes)
            continue
        budget = int(item.get("budget_minutes") or 0)
        if budget <= 0:
            budget = sum(int(child.get("duration") or 0) for child in children)
        selected = []
        left = budget
        for child in children:
            if left <= 0:
                break
            full = int(child.get("duration") or 0)
            take = min(full, left)
            if take <= 0:
                continue
            if take < full and take < MINUTES_PER_COUNT:
                break
            rec = dict(child)
            rec["_full_minutes"] = full
            rec["_alloc_minutes"] = take
            rec["count"] = ts.minutes_to_count(take) or 1
            rec["duration"] = take
            rec["total"] = int(child.get("count") or 0) or int(item.get("total") or 0)
            rec["task_type"] = pj.TASK_TYPE_SEQUENTIAL
            if take < MINUTES_PER_COUNT:
                rec["small"] = True
                rec["minutes"] = take
            selected.append(rec)
            left -= take
        for child in selected:
            _sequential_place_item(child, slots, placements, unscheduled, notes,
                                   parent=item)
            full = int(child.get("_full_minutes") or 0)
            alloc = int(child.get("_alloc_minutes") or 0)
            if full > alloc:
                notes.append("「%s」原 %d 分钟，本次只排前 %d 分钟"
                             "（剩余 %d 分钟未分到本次资源）"
                             % (child.get("title") or child.get("pk"), full, alloc,
                                full - alloc))
        if selected:
            notes.append("「%s」按 sequential 顺序排了 %d 条子任务"
                         % (item.get("title") or item.get("pk"), len(selected)))


def _is_sequential_item(item):
    item = item or {}
    project = item.get("project") or {}
    mode = (item.get("task_type")
            or project.get("task_type")
            or item.get("schedule_mode"))
    return pj.normalize_task_type(mode) == pj.TASK_TYPE_SEQUENTIAL


def _schedule_small_items(items, slots, placements, unscheduled, notes):
    """把短任务（实际时长 < 25 分钟）按分钟叠进空场次。

    短任务的 count 是 ``ceil(duration/25)``（10 分钟也记 1 count），但它们的
    真实占用是 10 分钟。这里不参加后面的「一场一个任务」匹配，而是像子任务
    一样按实际分钟装箱：同一个 session 可以叠两条 10 分钟任务，剩下 5 分钟
    空位；空位和这两条任务的分钟数会在 notes 里明说。
    """
    smalls = [item for item in items or []
              if item.get("small") and 0 < int(item.get("minutes") or 0) < MINUTES_PER_COUNT]
    for item in smalls:
        minutes = int(item.get("minutes") or 0)
        if minutes <= 0:
            continue
        candidates = []
        for slot in slots:
            free_minutes = int(slot.get("duration") or 0) - int(slot.get("used_minutes") or 0)
            if free_minutes < minutes:
                continue
            # 优先装进「已经有短任务、且装完浪费最小」的场；其次用空场里容量
            # 最小的（把大场留给后面 25 分钟整块的任务）。
            used_minutes = int(slot.get("used_minutes") or 0)
            candidates.append((0 if used_minutes > 0 else 1,
                               free_minutes - minutes,
                               int(slot.get("duration") or 0), slot))
        if not candidates:
            unscheduled.append(dict(item))
            continue
        candidates.sort(key=lambda row: row[:3])
        _already, _waste, _size, slot = candidates[0]
        stacked = int(slot.get("used_minutes") or 0) > 0
        slot["used_minutes"] = int(slot.get("used_minutes") or 0) + minutes
        # 短任务按分钟占位；把 count 格子也同步了，让后面的匹配/余量逻辑
        # 不会再把这块时间派给 25 分钟整块的任务。
        used_count = ts.minutes_to_count(int(slot.get("used_minutes") or 0)) or 0
        slot["used"] = min(int(slot.get("counts") or 0), used_count)
        placements.append({
            "item": item,
            "slot": slot,
            "count": int(item.get("count") or 0),
            "duration": minutes,
            "split": False,
            "stacked": stacked,
            "by_minutes": True,
        })
    # 给用户说清楚：短任务在哪场叠了几条、分别几分钟、空了多久。
    for slot in slots:
        mine = [p for p in placements if p.get("by_minutes") and p["slot"] is slot]
        if len(mine) < 2:
            continue
        free_minutes = int(slot.get("duration") or 0) - int(slot.get("used_minutes") or 0)
        if free_minutes <= 0 or free_minutes >= MINUTES_PER_COUNT:
            continue
        durs = "、".join("%d" % int(p.get("duration") or 0) for p in mine)
        notes.append("session %s（%d 分钟）塞了 %d 条短任务（%s 分钟），"
                     "任务时长不是 25 的整块，剩 %d 分钟空位。"
                     % (slot.get("start") or "?", int(slot.get("duration") or 0),
                        len(mine), durs, free_minutes))
    small_pks = {str(item.get("pk")) for item in smalls}
    return ([item for item in items or ()
             if str(item.get("pk")) not in small_pks], slots)


def schedule(items, slots):
    """把任务排进空场次。

    返回 ``(placements, unscheduled, notes)``。``placements`` 每条形如
    ``{"item": item, "slot": slot, "count": n, "split": bool, "stacked": bool}``：

    - ``split``：同一条任务被拆到多场（没有一场能整装下）；
    - ``stacked``：这场里除了它还塞了别的任务（空场不够时才这么干）。

    顺序就是两条原则的优先级：**先求一对一**（每场一个任务、每条任务一整场），
    配不上才拆、才叠。
    """
    slots = [dict(s, used=0, used_minutes=0) for s in slots or []]
    items = [dict(i) for i in items or []]
    placements, unscheduled, notes = [], [], []

    # Sequential 项目先按时间顺序占掉最早的 session；Parallel 项目继续
    # 使用原来的「最大匹配 + 最小浪费」算法吃剩下的容量。任务列表本身按
    # 项目分配顺序稳定，所以两类项目混合时结果可预期。
    sequential = [item for item in items if _is_sequential_item(item)]
    parallel = [item for item in items if not _is_sequential_item(item)]
    _schedule_sequential(sequential, slots, placements, unscheduled, notes)

    # Parallel：先排父任务壳（子任务能整条放就整条放；单场装不下才拆段，
    # 同一套尽量放同一场），再排短任务（按分钟叠），最后把剩余容量交给普通任务。
    slots = _schedule_children(parallel, slots, placements, unscheduled, notes)
    parallel = [item for item in parallel if not item.get("children")]
    parallel, slots = _schedule_small_items(
        parallel, slots, placements, unscheduled, notes)
    items = parallel
    matched = _match(items, slots)
    for i in sorted(matched):
        item = items[i]
        slot = slots[matched[i]]
        count = int(item.get("count") or 0)
        slot["used"] = int(slot.get("used") or 0) + count
        slot["used_minutes"] = int(slot.get("used_minutes") or 0) + count * MINUTES_PER_COUNT
        placements.append({"item": item, "slot": slot, "count": count,
                           "split": False, "stacked": False})
    left = [item for i, item in enumerate(items) if i not in matched]
    for item in left:
        need = int(item.get("count") or 0)
        if need <= 0:
            continue
        want = need
        chunks = []
        while need > 0:
            # 空的先（容量大的优先 = 少碎），再有余量的（余量小的优先 = 少浪费）。
            # 有短任务叠过的场按分钟算过 used，counts - used 就是还能放几块 25。
            free = [j for j, s in enumerate(slots) if s["used"] == 0 and s["counts"] > 0]
            free.sort(key=lambda j: (-slots[j]["counts"], j))
            relax = [j for j, s in enumerate(slots)
                     if s["counts"] - s["used"] > 0]
            relax.sort(key=lambda j: (slots[j]["counts"] - slots[j]["used"], j))
            picked = None
            for j in free:
                picked = j
                break
            if picked is None and relax:
                picked = relax[0]
            if picked is None:
                break
            slot = slots[picked]
            avail = int(slot["counts"]) - int(slot["used"])
            take = min(avail, need)
            chunks.append((picked, take, int(slot["used"]) > 0))
            slot["used"] += take
            need -= take
        if not chunks:
            unscheduled.append(item)
            continue
        for j, take, stacked in chunks:
            placements.append({"item": item, "slot": slots[j], "count": take,
                               "split": len(chunks) > 1, "stacked": stacked})
        if len(chunks) > 1:
            notes.append("「%s」要 %d count，没有一场能整装下 —— 拆成 %d 场（%s）"
                         % (item.get("title") or item.get("pk"), want, len(chunks),
                            "、".join("%s %d" % (slots[j].get("start"), take)
                                     for j, take, _stacked in chunks)))
        if need > 0:
            notes.append("「%s」还差 %d count 没地方放（空场次不够）"
                         % (item.get("title") or item.get("pk"), need))
    placements.sort(key=lambda p: (p["slot"].get("date") or "",
                                   p["slot"].get("start") or "",
                                   p["item"].get("project_id") or 0))
    return placements, unscheduled, notes


# ---------------------------------------------------------------- 问人


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
            pro_txt = (" · pro:%s(%s)" % (pro.get("title"), pro.get("stage"))
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
            self.say("   · %s（%d count，还剩 %d）"
                     % (item.get("title"), int(item.get("count") or 0),
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
