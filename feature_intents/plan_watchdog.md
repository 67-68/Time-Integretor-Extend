# plan_watchdog（「今天还没 plan」的提醒与兜底）

## 一句话

今天一个项目都没分到 count 的时候，TI 自动建一条 **25 分钟的「排今天–后天的
plan」任务**，用 formatter `enlist` 排进当天**最早的**那场；如果那场过去了、
用户还是没 plan，就**随机分资源、自动 plan 今天**。

用户原话（2026-11-20）：

> 在没有 plan 的情况下，创建一个 plan 任务，要求用户完成 TI 中今天 - 后天的
> plan，同时使用 formatter 安排在最近的第一个 session。如果检查的时候这个任务
> 已经存在，且安排在今天的某个 session，且这个 session 已经过去，同时没有计划，
> 那么自动安排计划，用随机算法安排 count 到不同的 session。

同日拍板的口径：

| 问题 | 决定 |
|---|---|
| 「没有 plan」看哪几天 | **只看今天**；明天/后天只写进标题，不检查 |
| 任务形态 | 一条 `duration=25`（1 count）的普通本地任务 |
| 排在哪个 session | 复用 formatter `enlist`（当天最早还有空位的时段，切一场新场） |
| 兜底的「自动安排计划」 | **只自动 plan 今天**：随机挑项目 + 随机切 count，走正常 plan 流水线 |
| plan 存在后提醒任务 | 本地标完成（不碰 Focusmate / OmniFocus） |

## 两个文件

- `plan_watchdog.py` —— **纯逻辑**：认人（`is_reminder`）、算时间（`session_end`）、
  数已分配（`allocated_counts`）、随机分资源（`random_allocs`）。不 import rich、
  不联网、不写盘。
- `warden_core/plan_watchdog_mixin.py` —— 接线：检查状态机、建任务、跑兜底、
  写账本与日志。

## 检查在哪、多久一次

挂在 `refresh_warnings()`（每帧钩子，和收尾 / 项目对账同一个节拍）里的
`_maybe_plan_watchdog()`，**60 秒最多一次**。所以「每个 session 结束后固定检查」
天然覆盖到：daemon 到点把 warden 拉起来 → 收尾问答 → 警告重算 → 这里也会跑。

`offline` 模式（`--offline` / 单测）**不自动跑**：自动建任务、切排布会污染排障现场。
测试要验行为就显式调 `_plan_watchdog_check()`。

## 状态机（一次检查）

```
闸门：正在 plan / 收尾问答 / 导入问答 / 到点没开始 / 中断问答 → skip
allocated = Σ plan_ops.plans["今天"].count
reminders = active 且 source.kind == "plan-watchdog" 的任务
```

1. **`allocated > 0`**（plan 已经有了）→ 有提醒任务就本地标完成，返回 `close`。
2. **更早日期留下的提醒任务**（session 的结束日期 < 今天）→ 标完成（`stale`），
   然后按「今天」重新看。
3. **没有提醒任务**
   - 账本 `created[今天]` 已存在 → 不建（**每天最多一条**：用户手动勾掉之后
     不会被刷回来）；
   - `pending_plan(今天)` 为空（没有可排空场 / 没有 active 项目）→ 不建；
   - 否则 → **create**：建任务 → `enlist` 排期 → 记 `created[今天]`。
4. **提醒任务还没排上场次**（上次 enlist 失败）→ 再试一次 `enlist`。
5. **提醒任务挂在今天、且那场已经结束** → **fallback**（每天最多一次）。
6. 其它（场次在将来说明天）→ 什么都不做。

## 兜底：随机分资源 + 自动 plan 今天

- 账本 `auto_planned[今天]` 有记录 → 今天不再兜底（失败也记，绝不每 60 秒重试）。
- 先记账（`status=pending`）再动手，然后：
  - `_plan_slots(今天, replan=True)` 取空场；没有空场 / 没有项目 → `empty`
  - `free_counts = pp.capacity(slots)["counts"]`
  - `random_allocs(plan_ops, free_counts, rng)`：随机选 k 个项目、把容量用隔板法
    随机切成 k 份正整数（每份 ≥1、总和正好是容量）
  - `pp.Allocator` + `_plan_apply(append_to_plan=False)` —— 和 `plan complete`
    **同一条流水线**（切排布 → 挂任务 → 末尾统一 `book()`）
- 兜底跑完（成没成都算）→ 提醒任务本地标完成，账本记 `status=ok/empty/error`。

看板停在别的日子时，兜底会临时把 `day_offset` 切回今天、跑完再切回去。

## 账本 `temp/plan-watchdog.json`

```json
{
  "created":       {"2026-11-20": {"task_id": 12, "at": "..."}},
  "auto_planned":  {"2026-11-20": {"at": "...", "status": "ok",
                                   "allocations": 1, "counts": 5,
                                   "reason": "", "error": ""}}
}
```

- 路径常量 `common.PLAN_WATCHDOG_FILE`，环境变量 `TI_PLAN_WATCHDOG` 可覆盖；
- 原子写（`.tmp` + `os.replace`），写失败只记日志、绝不影响业务。

## 日志（同一份 JSONL）

`area="plan_watchdog"`：

| event | level | 说明 |
|---|---|---|
| `check` | debug | 每次检查：`allocated`、`reminders` |
| `skip` | debug | 为什么没动：`busy` / `no-ops` / `offline` / `no-free-slots` / 各个问答 |
| `create` | info | 建了提醒任务（task_id / 空场数 / count） |
| `scheduled` | info | enlist 排到了哪天几点、多长 |
| `schedule_failed` | warning | 今天排不上（各时段尾巴都满了 / layout 接不上） |
| `close` | info | 提醒任务退休及原因（`plan-exists` / `stale` / `fallback:ok`） |
| `fallback` / `fallback_done` | info / warning | 兜底开始与结果 |
| `auto_alloc` | info | 随机到了哪些项目、各多少 count |
| `fallback_error` | error | 兜底异常（栈外只留一句 type + message） |

## 测试

`test/test_plan_watchdog.py`（离线，全部落盘指到 TMP）：

- 纯函数：认人 / session 结束时刻 / allocated 求和 / 随机分配（用满容量、每份
  ≥1、同种子可复现、无项目/无容量返回空）；
- 闸门：planning / 导入 / 没开始 / 中断问答中不建任务；
- create：1 条 25 分钟任务、标题与备注、`enlist` 排到 12:00、layout 切成
  `[50, 25]`、自动独占、幂等；
- plan 存在 → 本地完成、不写 outbox；
- 提醒那场没过去 → 不动；
- 那场过去且没 plan → 兜底：plan 出现、任务挂到场次、提醒退休、一天只跑一次；
- 昨天留下的提醒任务 → 退休 + 今天重建；
- 整段过去 / 没有项目 → 不建；
- 日志里 `check/create/scheduled` 事件都在。

## 边界

- 不会替用户「逐条看着排」：兜底只保证「今天有计划」，排什么由随机 + 项目库决定；
- 提醒任务独占那一场（`enlist` 的规矩），所以它不会被别的任务挤掉；
- 不做跨天计划检查（明天/后天的 plan 不会被本模块盯）；
- 所有落盘都走 `TI_*`；新增文件时记得同时接 `common.py` / 测试 / `blackbox_kit`
  的 `_paths_for` + `env_for` + `SNAPSHOT_FILES`。
