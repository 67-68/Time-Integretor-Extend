# simple —— 简单项目（Simple Project Mode）

## 这解决什么问题

考前冲刺、纯刷题这类场景：**目标极度明确，不需要细粒度任务拆解、不需要双侧阶段
管理**。这时候还要在 OmniFocus 里维护一份带 SOP / incubator 标签的清单、每天
排序挑任务，纯属摩擦。

简单项目把这种场景在排期流水线里抽象成一条：

> **禁用活跃侧与任务排序、拥有无穷（∞）Count 的大号项目级虚拟重复任务。**

它**不另起一条排期旁路**：分配 count 之后照样走
`distribute → 25/50/75 切分 → commit_layout → book` 这条唯一主干。

用户原话（2026-11-25）：

*「不另起炉灶建第二条排期旁路」*
*「禁用活跃侧与任务排序、拥有无穷（∞）Count 的大号项目级虚拟重复任务」*
*「理论上应该可以不用切回普通项目，直接把这条路径 ban 掉，提示无法切回就行了」*
*「我需要可以手动安排 plan 的时间……使用一个命令安排一个简单pro 的时间，接下来
几个 session 时间段都是他的（可以在任何时间开始，延伸到后面的时段直到时间排完，
或者直接把一个时段分配给他）」*

## 命令

```text
pro 3 simple            # 标成简单项目（一次性的单向开关）
pro 3 simple on         # 同上
pro 3 simple off        # ✗ 拒绝：简单项目不可逆（要恢复只能手工改 projects.json）
```

plan 面板里（`plan` 打开之后）：

```text
pro 3 4                 # 按容量摊派：分 4 count（=100 分钟），切成 75+25
pro 3 at 14:00 4        # 手动时间段：从 14:00 起连续占 4 count，跨 period 继续往后占
pro 3 at 14:00 100min   # 同义（100min 折成 4 count）
pro 3 period T1         # 整个 T1 时段都给它（容量 = 该时段能切的 count 全部）
pro 3 clear             # 清空它的分配/时间段
```

- `at` 的起点必须**精确命中一个空场的起点**（layout 只能从时段起点接龙）；没命中
  会整条拒绝并列出今天的可用起点。
- 往前扫时遇到已被别人订上 / 手动占用的场次会**跳过继续**，不中止。
- 不跨天；当天排不完就报「能排 N / 少排 M」，按实际排。
- 一轮 plan 里**一个时段只能给一个手动时间段**（layout 是一串连续 duration）。
- 超预算 / 超过这段的容量：整条命令不执行（和 `pro 3 4` 一个口径）。

## 数据模型

- `projects.json → pros[i].simple`：布尔标记。
- `projects.json → pros[i].plans`：简单项目的当天计划账本（和 op 的 `plans`
  一模一样），因为简单项目可能**没有** op。
- 排期层的合成项目 id：`SIMPLE_ID_BASE + pro_id`（`SIMPLE_ID_BASE = 1_000_000`）。
  op 的自增 id 到不了这个量级，所以不会撞车；这个数字只做内部键，界面不显示。
- 虚拟任务的字段：
  - `fields.project_id = SIMPLE_ID_BASE + pro_id`
  - `fields.project_task = "simple:<pro_id>"`（同一天多次排都认这一个身份）
  - `fields.title = 项目名`、`fields.count = 这场分到的 count`、
    `fields.count_total = 这次分配的总 count`
  - `omnifocus = None` + `source.local_only = true` → 外部世界不存在这条任务

## 流水线位置（关键：没有第二条）

| 阶段 | 简单项目的行为 |
| --- | --- |
| `ProjectStore.plan_ops()` | 不出现它的 op，换成一条合成视图（`simple: true`、`side=None`、带 `plans`） |
| `project_plan.Allocator` | 照常 `pro N count`；`stats()` 给 ∞，所以「剩余 0 就不让分」那道闸放行 |
| `project_plan.distribute()` | 不挑子任务，直接合成**一条** rec（`minutes = count × 25`） |
| `project_plan.schedule_periods()` | 完全复用：`_task_units → 75/50/25 贪心 → auto_layouts` |
| `commit_layout` / `book` | 完全复用；`source="plan"`，下次 plan 可以重切 |
| `ProjectStore.stats()` | `unlimited=True`、`remaining_text="∞"`、`progress_text="已投 X / ∞"`；`remaining` 仍是 0（别处 `int()` 不炸） |
| `ProjectStore.set_allocation/record_items/clear_items` | 按 id 路由：`SIMPLE_ID_BASE+pro_id` → 写到 pro 上；其余照旧写 op |
| `_reset_day_plans` / `_plan_owned_task_ids` / refresh 对账 | 遍历 `plan_refs()`（op + 简单 pro），否则上一轮的场次永远不释放 |

手动时间段是**同一轮 plan 的第一阶段**：先按 `from`/`period_id` 把场次切出来、
挂上虚拟任务（切场走 `commit_layout` / `_commit_window_sessions`，挂任务走
`task_store.create` + `_assign_task`），然后重读空场，其余项目只在剩下的空场里摊派。

## 记账与外部写回

- 完成一场照常进 `count_invested`，累计到该 pro（界面 `已投 X / ∞`）。
- 容量 ∞，所以**永不**因为「做完了」而耗尽；下次 plan 照样能排。
- `completion_guard` 直接放行（虚拟任务不对应任何一条 OmniFocus 任务）。
- 写回天然隔离：`omnifocus_id(task)` 为空 → `omni_complete` / `omni_drop` /
  `writeback_completions` / `_pk_claimed_by` 全部跳过；outbox 一条都不会有。
- 切 on / 拒绝 off **都不碰** OmniFocus、不碰标签、不碰任务库（`set_pro_simple`
  只改一个布尔值）。
- 简单项目不能切阶段（`pro 3 -a` 直接拒绝）：没有活跃侧，也就不该打 `manual_defer`。

## 显示

- `pro` 列表：`[简单项目 · 容量 ∞]`。
- 项目表：阶段列写「简单 · 容量 ∞」、排序列写「项目级虚拟任务」、剩余列 `∞`、
  进度列 `已投 X / ∞`。
- plan 面板：`[简单项目 · 容量 ∞] · 已投 N count · 从 14:00 起连续排 · 今天说好 …`。
- 任务视图里的虚拟任务：`(这场 count / 这次总 count)`。
- **这一场的清单框**：当前 session 挂着简单项目的虚拟任务时，契约视图多一块
  `Simple`（`open/close simple` 可折叠），原样列出它 OmniFocus 项目里未完成的任务
  （不排序、不按侧别过滤）。没关联 op、或清单为空 → 整块不画。

## 边界用例（黑盒封闭集）

`test/live/test_live_simple_project.py`（真子进程 `warden.py --exec`，+10 天不订场）：

1. 空项目直排：无 op 的 pro → `simple` → `plan` → `pro 4 3` → `complete` →
   13:00(75)；无「未打标 / 一条可排都没有」告警。
2. 大配额：`pro 4 5` → 75 + 50。
3. 手动时间段：`pro 4 at 13:00 4` → 13:00(75) + 14:30(25)，`pro 1 1` 只拿剩下的 15:00(25)。
4. 重排幂等：2 → 4 count 干净重切成 75+25，无开放未挂场的幽灵任务。
5. 同日第二场：完成一场后再 plan，新任务照建（不被同日去重吞），投入累计。
6. 不可逆：`simple off` 被拒、projects.json/op 库不变、无写回、无排期残留。

单测 `test/test_simple_project.py` 钉住合成视图 / ∞ 账 / 虚拟任务 / 账本落 pro /
守卫放行 / 写回隔离 / 单向开关 / 项目行与警告 / 时间段解析 / 这一场的清单框。

## 不做 / 已知限制

- **不可逆**：`simple off` 永久拒绝（用户定的；要恢复就手工改 `projects.json`）。
- 手动时间段的起点必须落在空场起点上；period 中间「半截起点」不切
  （layout 只能从时段起点接龙，硬切会把前缀排不进去）。
- 一轮 plan 里同一个时段只能给一个手动时间段。
- `op` 数据视图里，简单项目的 op 仍然照常显示（只是不进 plan，plan 列是 `—`）。
