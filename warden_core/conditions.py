#!/usr/bin/env python3
"""warden_core.conditions —— 「自动添加任务」的条件：一棵可求值、可渲染的表达式树。

用户 2026-11-21 要的（原话）：

    在 Contract Table 下方添加几行「条件」，展示自动添加任务的条件，以及当前
    是否满足。……比如说 end / enlist 是无条件添加。你可能需要抽象一下这个条件
    的 operator 允许这样的操作。

所以这里把「会不会自动多出一条任务」抽象成 operator：

    always / never / not / all / any / ge / eq / fact

每条 :class:`Rule` = 来源 + 动作 + 条件树。``describe()`` 给表格的「条件」列，
``evaluate(facts)`` 给「现在」列，不满足时 ``explain(facts)`` 点名第一个失败的
叶子（「因为：今天已 plan（2 count）」）。**本模块不 import rich、不写盘**。

这张表是只读展示，决策仍然在各自流水线里（plan_watchdog / formatter / plan）；
但 facts 全部来自它们自己用的同一批函数（``pending_plan`` / 账本 / 任务库），
不另造一套口径。
"""
from __future__ import annotations

import datetime

from .common import pw, ts


class _Facts(dict):
    """给 ``str.format_map`` 用：缺 key 时显示「?」而不是抛 KeyError。"""

    def __missing__(self, key):
        return "?"


def _fmt(template, facts):
    if not template:
        return ""
    try:
        return str(template).format_map(_Facts(facts))
    except (KeyError, IndexError, ValueError):
        return str(template)


class Cond(object):
    """条件节点基类。"""

    def evaluate(self, facts):
        raise NotImplementedError

    def describe(self):
        raise NotImplementedError

    def explain(self, facts):
        """这个条件**不成立**时，用人话说是哪一项卡住了。"""
        return "『%s』不成立" % self.describe()

    def explain_true(self, facts):
        """这个条件**成立**时的人话（``not`` 失败时用）。"""
        return "『%s』成立" % self.describe()


class Always(Cond):
    def evaluate(self, facts):
        return True

    def describe(self):
        return "always"

    def explain(self, facts):
        return "无条件"


class Never(Cond):
    def evaluate(self, facts):
        return False

    def describe(self):
        return "never"

    def explain(self, facts):
        return "永远不会加"


class Fact(Cond):
    """一个布尔事实（facts 里的 key）。"""

    def __init__(self, name, label=None, false_text=None, true_text=None):
        self.name = name
        self.label = label or name
        self.false_text = false_text
        self.true_text = true_text

    def evaluate(self, facts):
        return bool(facts.get(self.name))

    def describe(self):
        return self.label

    def explain(self, facts):
        if self.false_text:
            return _fmt(self.false_text, facts)
        return "%s 不成立" % self.label

    def explain_true(self, facts):
        if self.true_text:
            return _fmt(self.true_text, facts)
        return "%s 成立" % self.label


class Not(Cond):
    def __init__(self, child):
        self.child = child

    def evaluate(self, facts):
        return not self.child.evaluate(facts)

    def describe(self):
        return "¬%s" % _paren(self.child)

    def explain(self, facts):
        return self.child.explain_true(facts)

    def explain_true(self, facts):
        return self.child.explain(facts)


class All(Cond):
    def __init__(self, *children):
        self.children = [c for c in children if c is not None]

    def evaluate(self, facts):
        return all(c.evaluate(facts) for c in self.children)

    def describe(self):
        return " ∧ ".join(_paren(c) for c in self.children) or "always"

    def explain(self, facts):
        for child in self.children:
            if not child.evaluate(facts):
                return child.explain(facts)
        return "全部成立"


class Any(Cond):
    def __init__(self, *children):
        self.children = [c for c in children if c is not None]

    def evaluate(self, facts):
        return any(c.evaluate(facts) for c in self.children)

    def describe(self):
        return " ∨ ".join(_paren(c) for c in self.children) or "never"

    def explain(self, facts):
        reasons = [c.explain(facts) for c in self.children
                   if not c.evaluate(facts)]
        return "；".join(reasons) or "没有任何一条成立"


class Ge(Cond):
    """``facts[name] >= value``（数字事实）。"""

    def __init__(self, name, value, label=None, false_text=None):
        self.name = name
        self.value = value
        self.label = label or name
        self.false_text = false_text

    def evaluate(self, facts):
        try:
            return int(facts.get(self.name) or 0) >= int(self.value)
        except (TypeError, ValueError):
            return False

    def describe(self):
        return "%s≥%s" % (self.label, self.value)

    def explain(self, facts):
        if self.false_text:
            return _fmt(self.false_text, facts)
        return "%s=%s（需要 ≥%s）" % (self.label, facts.get(self.name, "?"),
                                     self.value)


class Eq(Cond):
    """``facts[name] == value``。"""

    def __init__(self, name, value, label=None, false_text=None):
        self.name = name
        self.value = value
        self.label = label or name
        self.false_text = false_text

    def evaluate(self, facts):
        return facts.get(self.name) == self.value

    def describe(self):
        return "%s=%s" % (self.label, self.value)

    def explain(self, facts):
        if self.false_text:
            return _fmt(self.false_text, facts)
        return "%s=%s（需要 =%s）" % (self.label, facts.get(self.name, "?"),
                                     self.value)


def _paren(cond):
    text = cond.describe()
    if isinstance(cond, (All, Any)):
        return "(%s)" % text
    return text


class Rule(object):
    """一条「自动加任务」的规则：来源、动作、条件树、备注。"""

    def __init__(self, key, source, action, condition, note="",
                 today_only=False):
        self.key = key
        self.source = source
        self.action = action
        self.condition = condition
        self.note = note
        self.today_only = today_only

    def applies_to(self, day):
        return not self.today_only or day == datetime.date.today()

    def evaluate(self, facts):
        return bool(self.condition.evaluate(facts))

    def describe(self):
        return self.condition.describe()

    def reason(self, facts):
        if self.evaluate(facts):
            return ""
        return self.condition.explain(facts)

    def row(self, facts, day):
        """给表格一行。今天 = 现在；未来 = 预测（「到时候会不会触发」）。"""
        today = datetime.date.today()
        if day < today:
            return {
                "key": self.key,
                "source": self.source,
                "action": self.action,
                "condition": self.describe(),
                "applicable": False,
                "forecast": False,
                "ok": None,
                "detail": "过去的日期不再检查",
            }
        if day > today:
            return self._forecast_row(facts, day)
        applicable = self.applies_to(day)
        ok = self.evaluate(facts) if applicable else None
        if not applicable:
            detail = "只在当天自动检查（今天）"
        elif ok:
            detail = self.note or self.action
        else:
            detail = self.reason(facts)
        return {
            "key": self.key,
            "source": self.source,
            "action": self.action,
            "condition": self.describe(),
            "applicable": applicable,
            "forecast": False,
            "ok": ok,
            "detail": detail,
        }

    def _forecast_row(self, facts, day):
        """未来某一天的预测。

        和今天的区别只有两处，都是「未来还没发生」：

        * ``reminder_ended`` 在未来表示「提醒场会在那天结束」而不是「已经结束」；
        * 兜底的预测看的是**链条**：当天没 plan、还有可排容量（或已经有提醒），
          就会先建提醒、提醒结束后仍没 plan → 会兜底。所以这里不直接拿
          ``reminder_ended`` 卡它，而是把「提醒会不会先建出来」算进去。
        """
        ok = self.evaluate(facts)
        detail = self.note or self.action
        if self.key == "plan_watchdog.fallback":
            capacity = int(facts.get("capacity_counts") or 0)
            ok = (not facts.get("has_plan")
                  and not facts.get("fallback_today")
                  and (capacity > 0 or facts.get("reminder_open")))
            if ok:
                detail = "前提：提醒任务先建出来、结束后仍没 plan"
            elif facts.get("has_plan"):
                detail = "当天已有 plan"
            elif facts.get("fallback_today"):
                detail = "当天已经兜底过"
            else:
                detail = "当天没有可排容量，提醒也建不出来"
        elif not ok:
            detail = self.reason(facts)
        return {
            "key": self.key,
            "source": self.source,
            "action": self.action,
            "condition": self.describe(),
            "applicable": True,
            "forecast": True,
            "ok": ok,
            "detail": detail,
        }


def auto_add_rules():
    """当前所有「会自动多出一条任务」的规则（顺序 = 表格里的顺序）。"""
    not_planned = Not(Fact(
        "has_plan", label="有 plan", false_text="当天没 plan",
        true_text="当天已 plan（{allocated_counts} count）"))
    return [
        Rule("formatter.end", "formatter · end",
             "把任务切进当天最后一段空档", Always(),
             "每次执行 task -f end 都会加"),
        Rule("formatter.enlist", "formatter · enlist",
             "把任务切进当天最早一段空档", Always(),
             "每次执行 task -f enlist 都会加"),
        Rule("plan.complete", "plan · complete",
             "按项目分到的 count 唤起任务并排进 session",
             Ge("allocated_counts", 1, label="已分配 count",
                false_text="还没有分配 count（{allocated_counts}）"),
             "plan complete 时执行"),
        Rule("plan_watchdog.reminder", "plan_watchdog · 提醒",
             "建 1 条 25 分钟提醒任务并排进最早空场",
             All(not_planned,
                 Ge("capacity_counts", 1, label="可用容量",
                    false_text="当天没有可排容量"),
                 Not(Fact("reminder_open", label="提醒任务在",
                          true_text="已经有一条提醒任务")),
                 Not(Fact("created_today", label="当天建过提醒",
                          true_text="当天已经建过一条提醒"))),
             "当天还没 plan 时自动加", today_only=True),
        Rule("plan_watchdog.fallback", "plan_watchdog · 兜底",
             "提醒场过去仍没 plan → 随机分资源自动 plan",
             All(not_planned,
                 Fact("reminder_ended", label="提醒场已结束",
                      false_text="提醒场还没结束"),
                 Not(Fact("fallback_today", label="当天兜底过",
                          true_text="当天已经兜底过"))),
             "提醒场过去仍没 plan 时自动跑", today_only=True),
    ]


def facts_for(warden, day=None):
    """收集条件求值要的事实：plan / 容量 / 提醒任务 / 当天账本。

    全部走各流水线自己的函数（``pending_plan`` / ``_plan_watchdog_ledger``），
    不另算一套。``pending_plan`` 本身与 plan 共用 ``_plan_slots``。
    """
    day = day or datetime.date.today()
    facts = {
        "allocated_counts": 0,
        "has_plan": False,
        "capacity_counts": 0,
        "reminder_open": False,
        "reminder_ended": False,
        "created_today": False,
        "fallback_today": False,
    }
    try:
        ops = warden.projects.plan_ops()
    except Exception:                          # noqa: BLE001 - 项目库坏了按没项目
        ops = []
    try:
        allocated = pw.allocated_counts(ops, day)
    except Exception:                          # noqa: BLE001
        allocated = 0
    facts["allocated_counts"] = int(allocated or 0)
    facts["has_plan"] = facts["allocated_counts"] > 0
    if not facts["has_plan"]:
        try:
            pending = warden.pending_plan(day)
            facts["capacity_counts"] = int((pending or {}).get("counts") or 0)
        except Exception:                      # noqa: BLE001 - 容量算不出来按 0
            facts["capacity_counts"] = 0
    try:
        reminders = [task for task in warden.task_store.list()
                     if pw.is_reminder(task) and ts.TaskStore.is_open(task)]
    except Exception:                          # noqa: BLE001
        reminders = []
    try:
        now = warden._now_dt()
    except Exception:                          # noqa: BLE001
        now = datetime.datetime.now()
    key = day.isoformat()
    mine = []
    for task in reminders:
        sess_date = str((task.get("session") or {}).get("date") or "")
        if sess_date == key:
            mine.append(task)
        elif not sess_date and day == now.date():
            # 还没排上场次的提醒任务属于「今天」（建出来但 enlist 失败）。
            mine.append(task)
    if day > now.date():
        # 未来那天：只看「挂在那天」的提醒任务。它现在还不存在是正常的；
        # 如果已经有（提前建了），它那天一定会结束，所以 reminder_ended 记 True。
        facts["reminder_open"] = bool(mine)
        facts["reminder_ended"] = bool(mine)
    else:
        facts["reminder_open"] = bool(mine)
        for task in mine:
            end = pw.session_end(task.get("session") or {})
            if end is not None and end.date() == day and end <= now:
                facts["reminder_ended"] = True
                break
    try:
        ledger = warden._plan_watchdog_ledger()
    except Exception:                          # noqa: BLE001 - 账本坏了不挡展示
        ledger = {}
    facts["created_today"] = bool((ledger.get("created") or {}).get(key))
    facts["fallback_today"] = bool((ledger.get("auto_planned") or {}).get(key))
    return facts


def condition_rows(warden, day=None):
    """给契约表下面那张小表用：每条规则 + 当前是否满足。"""
    day = day or datetime.date.today()
    facts = facts_for(warden, day)
    return [rule.row(facts, day) for rule in auto_add_rules()]
