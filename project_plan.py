#!/usr/bin/env python3
"""project_plan.py —— ``plan``：把「今天的空闲」切成 count，分给项目，再排成具体任务。

这是项目（``project_store.py``）的另一半：**资源分配 → 任务唤起 → 排进 session**。
分三步，每步都能单独测：

```
1) 元数据      capacity(slots)        今天还有几场空 session = 几个 count
2) 分资源      Allocator（问人）       task 1 4 / task 1 clear / complete
3) 排任务      distribute() + schedule()   每个项目按分到的 count 唤起任务、排进 session
```

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
    """把「每个项目多少 count」摊成具体的任务（每条还带要几个 count）。

    ``allocations``：``[{"project_id": 1, "count": 4}, ...]``（**顺序就是排的顺序**）；
    ``invested_map``：``{项目序号: {项目任务 pk: 已投入 count}}``
    （就是 ``project_store.invested_by_project(task_store)`` 那份账）。
    返回 ``(items, notes)``：
    ``items`` = ``[{"project_id", "project", "pk", "title", "count", "total"}]``。
    项目里的任务不够填满分到的 count 时，少排一点并记一条 note（不报错 —— 那是
    项目清单的事实，不是命令错了）。
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
        took = 0
        excluded = (exclude_pks.get(pid) or exclude_pks.get(str(pid))
                    if isinstance(exclude_pks, dict) else exclude_pks)
        for item in pj.remaining_tasks(project, invested_map.get(pid) or {},
                                       now=now, exclude_pks=excluded):
            if need <= 0:
                break
            take = min(int(item.get("remaining") or 0), need)
            if take <= 0:
                continue
            rec = {"project_id": pid,
                   "project": project,
                   "pk": item["pk"],
                   "title": item.get("title") or item["pk"],
                   "count": take,
                   "total": int(item.get("count") or 0),
                   "remaining": int(item.get("remaining") or 0)}
            if item.get("children"):
                # 父任务壳：真正的 session 内容是不可拆的子任务。这里把整套
                # 剩余子任务交给 schedule，由它按每场的实际分钟数选子集。
                rec.update({
                    "shell": True,
                    "children": [dict(child) for child in item.get("children") or []],
                    "duration": int(item.get("duration") or 0),
                    "budget_minutes": take * MINUTES_PER_COUNT,
                })
            out.append(rec)
            need -= take
            took += take
        if need > 0:
            notes.append("「%s」的任务只剩 %d count，少排 %d（清单里没有更多可干的了）"
                         % (project.get("title") or pid, took, need))
    return out, notes


# ---------------------------------------------------------------- 排任务


def _match(items, slots):
    """Kuhn 增广路：任务 → 场次的一对一匹配（场次容量 >= 任务要的 count）。

    返回 ``{item_index: slot_index}``。没有完美匹配时返回能配上的那些 —— 配不上的
    由调用方拆开填。候选场次按「容量小的优先」试，浪费就小。
    """
    caps = [int(s.get("counts") or 0) for s in slots]
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

    子任务不可拆（用户明确要求）。这里返回选中的 child 列表；没有能放下的
    就返回空列表。数量通常很小，用 sum→选择清单 的 DP 就够。
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


def _schedule_children(items, slots, placements, unscheduled, notes):
    """把带子任务的父任务壳按 session 实际分钟数排进表。

    一个 session 只放**同一个父任务**的子任务；放不下就换下一场，剩余子任务
    继续找场。每个子任务都是原子单位，不拆。返回处理完后仍然可用的 slots。
    """
    group_items = [item for item in items if item.get("children")]
    for item in group_items:
        remaining = [dict(child) for child in item.get("children") or []
                     if int(child.get("duration") or 0) > 0]
        budget = int(item.get("budget_minutes") or 0)
        if budget <= 0:
            budget = sum(int(child.get("duration") or 0) for child in remaining)
        touched_slots = []
        while remaining and budget > 0:
            wanted = min(sum(int(child.get("duration") or 0) for child in remaining),
                         budget)
            candidates = []
            for slot in slots:
                if slot.get("used", 0) >= slot.get("counts", 0):
                    continue
                free_minutes = int(slot.get("duration") or 0) - int(
                    slot.get("used_minutes") or 0)
                if free_minutes <= 0:
                    continue
                pick = _pick_child_subset(remaining, min(free_minutes, budget))
                if not pick:
                    continue
                got = sum(int(child.get("duration") or 0) for child in pick)
                # 先优先「一场装得下全部剩余」；否则选装得最多的场，再选浪费小的。
                fits_all = got >= wanted
                candidates.append((0 if fits_all else 1, -got,
                                   abs(free_minutes - got), free_minutes, slot, pick))
            if not candidates:
                break
            candidates.sort(key=lambda row: row[:4])
            _all, _neg, _waste, free_minutes, slot, pick = candidates[0]
            got = sum(int(child.get("duration") or 0) for child in pick)
            for child in pick:
                minutes = int(child.get("duration") or 0)
                count = ts.minutes_to_count(minutes) or 1
                placements.append({
                    "item": {
                        "project_id": item["project_id"],
                        "project": item["project"],
                        "pk": child.get("pk"),
                        "title": child.get("title") or child.get("pk"),
                        "duration": minutes,
                        "count": count,
                        "note": child.get("note") or "",
                        "parent_pk": item.get("pk"),
                        "parent_title": item.get("title") or item.get("pk"),
                    },
                    "slot": slot,
                    "count": count,
                    "duration": minutes,
                    "split": False,
                    "stacked": False,
                    "parent": item,
                })
                remaining = [ch for ch in remaining
                             if str(ch.get("pk")) != str(child.get("pk"))]
            budget -= got
            slot["used_minutes"] = int(slot.get("used_minutes") or 0) + got
            # 一场只放一个父任务套；宁可留白，也不把另一套的子任务混进同一场。
            slot["used"] = int(slot.get("counts") or 0)
            touched_slots.append(slot)
        if touched_slots:
            for place in placements:
                if place.get("parent") is item:
                    place["split"] = len(touched_slots) > 1
            if len(touched_slots) > 1:
                notes.append("「%s」的子任务跨了 %d 场（子任务不拆，按场次容量分组）"
                             % (item.get("title") or item.get("pk"), len(touched_slots)))
        if remaining:
            rest_minutes = sum(int(child.get("duration") or 0) for child in remaining)
            unscheduled.append(dict(item,
                                    count=ts.minutes_to_count(rest_minutes) or 0,
                                    remaining_count=ts.minutes_to_count(rest_minutes) or 0,
                                    children=remaining))
            notes.append("「%s」还有 %d 分钟子任务没地方放（空场次不够或单条子任务太长）"
                         % (item.get("title") or item.get("pk"), rest_minutes))
    return [slot for slot in slots
            if int(slot.get("used") or 0) < int(slot.get("counts") or 0)]


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
    # 先排父任务壳（子任务不可拆、同一套尽量放同一场），再把剩余容量交给普通任务。
    slots = _schedule_children(items, slots, placements, unscheduled, notes)
    items = [item for item in items if not item.get("children")]
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
            # 空的先（容量大的优先 = 少碎），再有余量的（余量小的优先 = 少浪费）
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
                 stage=pj.OP_TYPE_SOP, incubator_projects=None, pro_map=None):
        self.day = day
        self.stage = (pj.OP_TYPE_INCUBATOR
                      if pj.normalize_op_type(stage) == pj.OP_TYPE_INCUBATOR
                      else pj.OP_TYPE_SOP)
        self.projects = list(projects or [])
        self.incubator_projects = list(incubator_projects or [])
        self.pro_map = dict(pro_map or {})
        self.slots = [dict(s) for s in slots or []]
        self.task_store = task_store
        self.hooks = hooks or {}
        self.allocations = []          # [{"project_id","count"}]，顺序 = 排的顺序
        self.closed = False
        self.had_sop_stage = False
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
        stage_label = ("incubator 资源分配"
                       if self.stage == pj.OP_TYPE_INCUBATOR
                       else "SOP 资源分配")
        return "%s %s（%d 个空场 = %d count）" % (
            self.day.isoformat() if hasattr(self.day, "isoformat") else self.day,
            stage_label, self.cap()["sessions"], self.cap()["counts"])

    def options(self):
        return ["task <序号> <count>", "task <序号> clear", "complete", "q"]

    def stage_hint(self):
        next_note = ("（complete 后进入 incubator 阶段）"
                     if (self.stage == pj.OP_TYPE_SOP and self.incubator_projects)
                     else "")
        return ("task 1 4 = #%s #1 分 4 count · task 1 clear = 清空 · "
                "complete = 开始排任务%s · q = 放弃（已分配 %d / %d count）"
                % (self.stage, next_note, self.allocated(), self.cap()["counts"]))

    def lines(self):
        """面板正文：[(文本, 样式)]。"""
        cap = self.cap()
        icon = "🌱" if self.stage == pj.OP_TYPE_INCUBATOR else "🗂"
        out = [("%s %s" % (icon, self.headline()), "bold cyan"),
               ("  当前阶段：%s（只处理类型为 %s 的 op）" % (self.stage, self.stage), "dim"),
               ("  空闲 %s" % capacity_text(cap), "dim"),
               ("  空场 %s" % slots_text(self.slots), "dim")]
        if not self.projects:
            out.append(("  （当前阶段没有可分配的 %s op）" % self.stage,
                        "yellow"))
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
            bits = ["[%s%s]" % (op_kind, pro_txt),
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
            self.say("task <序号> <count>（或 op <序号> <count>）：给这个 %s op 分几个 count"
                     "（1 count = 25 分钟）；再敲一次是覆盖。" % self.stage, "info")
            self.say("task <序号> clear：清空这个项目的分配；"
                     "task <序号>：只看清单。", "info")
            self.say("complete：分配完了，开始挑任务、排进 session%s；q：放弃这次 plan。"
                     % ("（SOP 排完后进入 incubator 阶段）"
                        if (self.stage == pj.OP_TYPE_SOP and self.incubator_projects)
                        else ""), "info")
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
            if self.stage == pj.OP_TYPE_SOP and self.incubator_projects:
                summary = {"placements": 0, "counts": 0, "used_slot_indices": set()}
                self.say("🗂 SOP 阶段分配 0 count，进入 incubator 时间安排阶段。", "info")
                return self._enter_incubator_stage(summary, [])
            if self.stage == pj.OP_TYPE_INCUBATOR and self.had_sop_stage:
                self.say("🌱 incubator 阶段分配 0 count，本次 plan 完成。", "ok")
                self.closed = True
                return {"action": "close", "done": "complete",
                        "summary": {"placements": 0, "counts": 0},
                        "allocations": []}
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
        if self.stage == pj.OP_TYPE_SOP and self.incubator_projects:
            return self._enter_incubator_stage(summary, allocs)
        self.closed = True
        return {"action": "close", "done": "complete",
                "summary": summary, "allocations": allocs}

    def _enter_incubator_stage(self, sop_summary, sop_allocs):
        """SOP 阶段排完后，无缝切入 incubator 时间安排阶段。"""
        next_slots = self._hook("remaining_slots", self, sop_summary)
        if next_slots is None:
            used_idx = set((sop_summary or {}).get("used_slot_indices") or ())
            next_slots = [dict(s) for s in self.slots
                          if s.get("index") not in used_idx]
        if not next_slots:
            self.say("🌱 SOP 阶段已用完今天全部空场（剩余 0 场），跳过 incubator 时间安排阶段。",
                     "info")
            self.closed = True
            return {"action": "close", "done": "complete",
                    "summary": sop_summary or {}, "allocations": sop_allocs}
        self.stage = pj.OP_TYPE_INCUBATOR
        self.projects = list(self.incubator_projects)
        self.incubator_projects = []
        self.slots = [dict(s) for s in next_slots]
        self.allocations = []
        self.had_sop_stage = True
        cap = self.cap()
        self.say("🌱 进入 incubator 时间安排阶段：%d 个 incubator op、剩余 %d 场空 session "
                 "= %d count 可以分（%d 分钟；1 count = %d 分钟）"
                 % (len(self.projects), cap["sessions"], cap["counts"],
                    cap["minutes"], MINUTES_PER_COUNT), "cmd")
        self.say("   `task <序号> <count>` 分给 incubator op、`complete` 开始排任务、`q` 结束。",
                 "info")
        return {"action": "handled", "stage": pj.OP_TYPE_INCUBATOR,
                "summary": sop_summary or {}, "allocations": sop_allocs}

    def _cancel(self):
        self.closed = True
        self.say("⏹ 这次 plan 放弃：没有分配、也没有建任何任务。", "info")
        return {"action": "close", "done": "cancel"}
