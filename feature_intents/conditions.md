# conditions（契约表下方的「自动添加条件」）

## 一句话

契约表正下方多一张只读小表：**每条「会自动多出一条任务」的规则 + 它现在的条件是
否满足**。formatter `end` / `enlist` 这类是无条件添加，`plan` / `plan_watchdog`
有自己的前置条件；条件用 operator 表达式树描述，可求值、可渲染、失败时点名第一个
不满足的叶子。

## 用户原话

2026-11-21：

> 在 Contract Table 下方添加几行「条件」，展示自动添加任务的条件，以及当前是否
> 满足。包括 formatter 放上来的任务都可以写到这里，比如说 end / enlist 是无条件
> 添加。你可能需要抽象一下这个条件的 operator 允许这样的操作。

补充（同日）：

> 对于明天我的本义是明天那一张契约表看的时候，不过我希望你在今天也展示 condition
> 的表。

所以：**今天、明天、任意一天都画**。今天看「现在是否满足」；看未来某一天时
表头变成「预计」，plan_watchdog 两条给的是**预测**（「到时候会不会触发」），
不再显示「— 不适用」。

## 文件

- `warden_core/conditions.py` —— 纯逻辑：operator 表达式树（`Always` / `Never` /
  `Fact` / `Not` / `All` / `Any` / `Ge` / `Eq`）、`Rule`、`auto_add_rules()`、
  `facts_for(warden, day)`、`condition_rows(warden, day)`。
- `warden_core/board_mixin.py` —— `condition_rows()`（5 秒缓存）+ 
  `render_conditions_table()`。
- `warden_core/views.py` —— 契约视图里紧贴 `render_table()` 之后插入。
- `test/test_conditions.py` —— operator / 规则 / facts 的单测；
  `test/test_plan_watchdog.py` 里有渲染与「今天/明天」的接线断言。

## 第一版规则

| key | 来源 | 动作 | 条件 | 只在今天 |
|---|---|---|---|---|
| `formatter.end` | formatter · end | 切进当天最后一段空档 | `always` | |
| `formatter.enlist` | formatter · enlist | 切进当天最早一段空档 | `always` | |
| `plan.complete` | plan · complete | 按分配唤起任务并排进 session | `已分配 count≥1` | |
| `plan_watchdog.reminder` | plan_watchdog · 提醒 | 建 25 分钟提醒任务 | `¬有 plan ∧ 可用容量≥1 ∧ ¬提醒任务在 ∧ ¬今天建过提醒` | ✓ |
| `plan_watchdog.fallback` | plan_watchdog · 兜底 | 提醒场过去仍没 plan → 自动 plan | `¬有 plan ∧ 提醒场已结束 ∧ ¬今天兜底过` | ✓ |

## facts（口径不另造）

| fact | 来源 |
|---|---|
| `allocated_counts` / `has_plan` | `pw.allocated_counts(projects.plan_ops(), day)`（和 plan 同口径） |
| `capacity_counts` | `warden.pending_plan(day)`（内部走 `_plan_slots`，period-first） |
| `reminder_open` / `reminder_ended` | task_store 里 `pw.is_reminder` + `pw.session_end` + `warden._now_dt()` |
| `created_today` / `fallback_today` | `_plan_watchdog_ledger()` 的 `created` / `auto_planned`（按**选中那天**取 key） |

未来日期的两处差别（`facts_for` 里处理）：

- `reminder_open` 只看**挂在那天**的提醒（今天多认「还没排上场次」的那条）；
- `reminder_ended` 在未来表示「提醒场会在那天结束」（已经有那天的提醒 → True），
  今天才是「已经结束」；
- 兜底的预测不直接卡 `reminder_ended`，而是算链条：当天没 plan、有可排容量
  （或已经有提醒）→ 会先建提醒、提醒结束后仍没 plan → 会兜底；说明里写
  「前提：提醒任务先建出来、结束后仍没 plan」。

## 显示

```
自动添加条件 · 今天
 来源                 条件                                         现在       说明
 formatter · end      always                                       ✅ 满足   每次执行 task -f end 都会加
 formatter · enlist   always                                       ✅ 满足   每次执行 task -f enlist 都会加
 plan · complete      已分配 count≥1                               ❌ 不满足 还没有分配 count（0）
 plan_watchdog · 提醒  ¬有 plan ∧ 可用容量≥1 ∧ ¬提醒任务在 ∧ ¬当天建过提醒  ✅ 满足 当天还没 plan 时自动加
 plan_watchdog · 兜底  ¬有 plan ∧ 提醒场已结束 ∧ ¬当天兜底过          ❌ 不满足 提醒场还没结束
```

看明天（表头「预计」）时：

```
自动添加条件 · 2026-11-22
 来源                 条件                                          预计         说明
 plan_watchdog · 提醒  ¬有 plan ∧ 可用容量≥1 ∧ ¬提醒任务在 ∧ ¬当天建过提醒  ✅ 预计会   当天还没 plan 时自动加
 plan_watchdog · 兜底  ¬有 plan ∧ 提醒场已结束 ∧ ¬当天兜底过          ✅ 预计会   前提：提醒任务先建出来、结束后仍没 plan
```

- 缓存 5 秒；`rebuild_rows()`（任何命令/刷新的状态落点）会立刻让缓存失效。
- 只读展示，不驱动任何决策；决策仍在 plan / plan_watchdog / formatter 自己的流水线里。
