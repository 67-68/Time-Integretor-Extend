#!/usr/bin/env python3
"""「自动添加条件」的回归测试：operator DSL + 规则求值 + facts 收集。

用户 2026-11-21 要的：契约表下方展示自动添加任务的条件 + 当前是否满足；
formatter end / enlist 是无条件添加；条件要能抽象 operator。

    python3 -B test/test_conditions.py

全程纯内存：不碰真工作区 / Focusmate / OmniFocus。
"""
import datetime
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from warden_core import conditions as c  # noqa: E402
from warden_core.common import pw  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


DAY = datetime.date.today()
ISO = DAY.isoformat()

print("== operator：always / never / not / all / any / ge / eq ==")
check("always 恒真", c.Always().evaluate({}), True)
check("never 恒假", c.Never().evaluate({}), False)
check("not 取反", c.Not(c.Fact("x")).evaluate({"x": False}), True)
check("all 全真才真", c.All(c.Fact("a"), c.Fact("b")).evaluate(
    {"a": True, "b": True}), True)
check("all 有一个假就假", c.All(c.Fact("a"), c.Fact("b")).evaluate(
    {"a": True, "b": False}), False)
check("any 有一个真就真", c.Any(c.Fact("a"), c.Fact("b")).evaluate(
    {"a": False, "b": True}), True)
check("ge 比大小", c.Ge("n", 2).evaluate({"n": 3}), True)
check("ge 不够就假", c.Ge("n", 2).evaluate({"n": 1}), False)
check("eq 相等", c.Eq("s", "ok").evaluate({"s": "ok"}), True)
check("缺 key 按假处理", c.Fact("missing").evaluate({}), False)

check("all 的 describe 用 ∧", c.All(c.Fact("a"), c.Ge("n", 2)).describe(),
      "a ∧ n≥2")
check("not 的 describe 用 ¬", c.Not(c.Fact("a")).describe(), "¬a")
check("嵌套 all/any 会加括号",
      c.All(c.Any(c.Fact("a"), c.Fact("b")), c.Fact("c")).describe(),
      "(a ∨ b) ∧ c")
check("ge 失败时点名数值", c.Ge("n", 2, label="容量").explain({"n": 1}),
      "容量=1（需要 ≥2）")
check("fact 失败用 false_text", c.Fact("x", false_text="没有 {n} 个").explain(
    {"n": 3}), "没有 3 个")
check("not 失败说明子条件成立",
      c.Not(c.Fact("x", true_text="已经有 x")).explain({"x": True}),
      "已经有 x")

print("== 规则：formatter 无条件；plan 看分配；watchdog 看 plan/容量/提醒/账本 ==")
RULES = {rule.key: rule for rule in c.auto_add_rules()}
check("规则条数", len(RULES), 5)
check("formatter end 无条件",
      RULES["formatter.end"].row({}, DAY)["ok"], True)
check("formatter enlist 无条件",
      RULES["formatter.enlist"].row({}, DAY)["ok"], True)

_f0 = {"allocated_counts": 0, "capacity_counts": 2, "reminder_open": False,
       "reminder_ended": False, "created_today": False, "fallback_today": False}
_f0["has_plan"] = False
check("plan 没分配 → 不满足", RULES["plan.complete"].row(_f0, DAY)["ok"], False)
check("plan 不满足的原因", RULES["plan.complete"].row(_f0, DAY)["detail"],
      "还没有分配 count（0）")
_f1 = dict(_f0, allocated_counts=2, has_plan=True)
check("plan 有分配 → 满足", RULES["plan.complete"].row(_f1, DAY)["ok"], True)

check("watchdog 提醒：没 plan + 有容量 + 无提醒 + 没建过 → 满足",
      RULES["plan_watchdog.reminder"].row(_f0, DAY)["ok"], True)
check("watchdog 提醒：没容量 → 不满足",
      RULES["plan_watchdog.reminder"].row(
          dict(_f0, capacity_counts=0), DAY)["detail"],
      "今天没有可排容量")
check("watchdog 提醒：已有提醒 → 不满足",
      RULES["plan_watchdog.reminder"].row(
          dict(_f0, reminder_open=True), DAY)["detail"],
      "已经有一条提醒任务")
check("watchdog 提醒：今天建过 → 不满足",
      RULES["plan_watchdog.reminder"].row(
          dict(_f0, created_today=True), DAY)["detail"],
      "今天已经建过一条提醒")
check("watchdog 提醒：已 plan → 不满足且说明是 plan 挡的",
      RULES["plan_watchdog.reminder"].row(
          dict(_f0, has_plan=True, allocated_counts=2), DAY)["detail"],
      "今天已 plan（2 count）")

check("watchdog 兜底：提醒场已结束 + 没 plan + 没兜底 → 满足",
      RULES["plan_watchdog.fallback"].row(
          dict(_f0, reminder_ended=True), DAY)["ok"], True)
check("watchdog 兜底：提醒场没结束 → 不满足",
      RULES["plan_watchdog.fallback"].row(_f0, DAY)["detail"],
      "提醒场还没结束")
check("watchdog 兜底：今天兜底过 → 不满足",
      RULES["plan_watchdog.fallback"].row(
          dict(_f0, reminder_ended=True, fallback_today=True), DAY)["detail"],
      "今天已经兜底过")

_tomorrow = DAY + datetime.timedelta(days=1)
_row = RULES["plan_watchdog.reminder"].row(_f0, _tomorrow)
check("watchdog 规则看别的日期 = 不适用", _row["applicable"], False)
check("不适用时写明只在当天检查", "只在当天" in _row["detail"], True)


print("== facts_for：走同一批函数收集事实 ==")
class FakeProjects(object):
    def __init__(self, ops):
        self.ops = ops

    def plan_ops(self):
        return self.ops


class FakeTaskStore(object):
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])

    def list(self):
        return list(self.tasks)


class FakeWarden(object):
    def __init__(self, ops=None, tasks=None, pending=None, ledger=None,
                 clock=None):
        self.projects = FakeProjects(list(ops or []))
        self.task_store = FakeTaskStore(tasks)
        self._pending = pending
        self.ledger = dict(ledger or {})
        self.clock = clock or datetime.datetime.combine(DAY, datetime.time(12, 0))

    def pending_plan(self, day=None):
        return self._pending

    def _now_dt(self):
        return self.clock

    def _plan_watchdog_ledger(self):
        return dict(self.ledger)


_w = FakeWarden(
    ops=[{"id": 1, "plans": {ISO: {"count": 0}}},
         {"id": 2, "plans": {}}],
    tasks=[{"id": 7, "status": "active", "source": {"kind": pw.SOURCE_KIND},
            "session": {"date": ISO, "start": "08:00", "duration": 25}}],
    pending={"counts": 3, "sessions": 1, "starts": ["13:15"]},
    ledger={"created": {ISO: {"task_id": 7}}},
)
_facts = c.facts_for(_w, DAY)
check("没 plan", _facts["has_plan"], False)
check("容量来自 pending_plan", _facts["capacity_counts"], 3)
check("有开放提醒任务", _facts["reminder_open"], True)
check("提醒场 08:00 已结束（12:00）", _facts["reminder_ended"], True)
check("账本：今天建过", _facts["created_today"], True)
check("账本：今天没兜底", _facts["fallback_today"], False)

_w2 = FakeWarden(
    ops=[{"id": 1, "plans": {ISO: {"count": 2}}}],
    tasks=[],
)
_facts2 = c.facts_for(_w2, DAY)
check("有 plan 时按 count 判", _facts2["has_plan"], True)
check("有 plan 时分配数", _facts2["allocated_counts"], 2)
check("有 plan 时不再算容量", _facts2["capacity_counts"], 0)
check("没有提醒任务", _facts2["reminder_open"], False)

_rows = c.condition_rows(_w, DAY)
check("condition_rows 给出全部规则", len(_rows), 5)
check_true("每行都有条件文案", all(row.get("condition") for row in _rows))
check_true("每行都有现在状态", all("ok" in row for row in _rows))

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    raise SystemExit(1)
print("全部通过")
