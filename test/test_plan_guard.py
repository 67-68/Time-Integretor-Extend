#!/usr/bin/env python3
"""plan 分资源的硬闸：项目 0 条可排任务时不许「说好 N count」。

现场（2026-11-22）：项目「历史 W13 复习 2」的 OmniFocus 清单里有 4 条未完成任务，
但一条都没打 SOP / incubator 标签，op 类型也是空的。于是：

    * 项目表：剩余 count = 0、进度 = —（看起来像账算错了，其实一条都不算）；
    * plan 里照样能 `pro 10 8`，`complete` 之后 0 条任务 → 0 场 session，
      还把这一天 T2/T3/T4 已有的排布整片清空；
    * keep_session 到点发现「说好的 08:30 没有这场 session，来不及补订了」。

修法：`pro N count` 先看这个项目有没有可排的活，0 就**整条命令不执行**并说清楚
原因（没打标 / 在 defer 里 / 都已完成）；面板行里把「N 条未打标」直接写出来。

    python3 test/test_plan_guard.py
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import project_plan as pp  # noqa: E402
import project_store as pj
import task_store as ts

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


class FakeStore(object):
    """空任务库：项目库自己带清单，这一组用例不碰本地任务账。"""

    def list(self):
        return []


def mk_project(tags=None, op_type=None, count=2, tasks=2):
    op = {
        "id": 7,
        "title": "历史 W13 复习 2",
        "op_type": op_type,
        "project_tag": "",
        "tags": [],
        "status": pj.STATUS_ACTIVE,
        "omnifocus": {"id": "op-pk", "url": "omnifocus:///project/op-pk"},
        "task_type": "parallel",
        # plan_ops() 会给每条 op 填这两个键：side = pro 当前活跃侧，
        # default_side = op 的 legacy 类型（这里模拟「没设类型」）。
        "side": "SOP",
        "default_side": None,
        "plans": {},
        "tasks": [],
    }
    for index in range(tasks):
        op["tasks"].append({
            "pk": "pk-%d" % index,
            "title": "粗略复习法国 mother question %d" % index,
            "count": count,
            "duration": count * ts.MINUTES_PER_COUNT,
            "completed": False,
            "dropped": False,
            "effective_completed": False,
            "effective_dropped": False,
            "tags": list(tags or ()),
            "rank": index,
        })
    return op


DAY = datetime.date.today() + datetime.timedelta(days=3)
SLOTS = [{"index": 1, "date": DAY.isoformat(), "start": "14:00",
          "duration": 75, "counts": 3, "period": {"id": "T1", "start": "14:00",
                                                  "end": "16:30"}}]


def mk_allocator(project, messages):
    return pp.Allocator(DAY, [project], SLOTS, task_store=FakeStore(),
                        hooks={"say": lambda text, kind="info":
                               messages.append((kind, text))})


print("== 1. 未打标的任务：剩余 count = 0，但面板要说明为什么 ==")
untagged = mk_project()
messages = []
alloc = mk_allocator(untagged, messages)
check("pj.stats 口径：未打标 = 0 count（不是账坏了）",
      pj.stats(untagged, FakeStore(), side="SOP", default_side=None)["remaining"], 0)
text = "\n".join(line for line, _style in alloc.lines())
check_true("面板行写出「2 条未打标」", "2 条未打标" in text)

print("\n== 2. `pro 1 3` 直接拒绝（分配一个字都不动）==")
check("这条命令返回 False", alloc.handle("pro 1 3")["action"], "handled")
check("没有落任何分配", alloc.allocations, [])
check_true("说清楚了原因（没有 SOP / incubator 标签）",
           any("SOP / incubator 标签" in text for _kind, text in messages))
check_true("没有骗人说已分配", not any("✅ #1" in text for _kind, text in messages))

print("\n== 3. 打了 SOP 标签之后就能分 ==")
tagged = mk_project(tags=["SOP"])
alloc2 = mk_allocator(tagged, [])
check("剩余 count 算出来了",
      pj.stats(tagged, FakeStore(), side="SOP", default_side=None)["remaining"], 4)
check("这条命令受理", alloc2.handle("pro 1 3")["action"], "handled")
check("分配落下来了", alloc2.allocations, [{"project_id": 7, "count": 3}])

print("\n== 4. 万一直接调用 distribute：一条都没挑出来必须留 note ==")
items, notes = pp.distribute(
    [{"project_id": 7, "project": dict(untagged, side="SOP", default_side=None),
      "count": 8}],
    [dict(untagged, side="SOP", default_side=None)])
check("确实一条都没挑出来", items, [])
check_true("note 说出「一条可排的任务都没有」",
           any("一条可排的任务都没有" in note for note in notes))

print("\n%s" % ("全绿。" if not fails else "\n".join(fails)))
raise SystemExit(1 if fails else 0)
