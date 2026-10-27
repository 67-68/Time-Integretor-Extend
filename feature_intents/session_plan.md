# session_plan

## 模块职责

日历下载与 `data.csv` 之间的中间层（`session_plan.py`）。把「下载了什么」
和「用户想怎么排 session」分开：

```
iCloud 日历（只 工作 / 空闲，今天 + 7 天）
  → temp/calendar-download.json   原始事件 + 按时段编号的 T1/T2/...
  → temp/session-layouts.json     用户对每个时段的自定义时长序列
  → temp/session-extras.json      一次性 session（`session HH:MM LEN`，只属于那一天）
  → temp/session-rules.json       长期规则（`session --permanent`，按星期几重复）
  → data.csv                      精确日期行（YYYY-MM-DD,HH:MM,duration）+ weekday 规则行
```

`temp/calendar.md` 是同一份下载层的人读版（时段表 + 默认切分预览）。

## 时段语义

- 一个日历事件 = 一个时段。按天按开始时间编号 `T1`, `T2`, ...
- `类型` = 日历显示名（工作 / 空闲）；`名称` = 事件 SUMMARY。
- 用户只给时长序列，起点由程序算：第一场从时段起点开始，之后每场从上一场
  结束后的下一个 `:00/:15/:30/:45` 开始。
- 例：`T1 11:00–13:00`
  - `T1 50 50` → 11:00–11:50, 12:00–12:50
  - `T1 25 25 25 25` → 11:00–11:25, 11:30–11:55, 12:00–12:25, 12:30–12:55
  - `T1 50 50 50` → **报错，零副作用**（第三场会越过 13:00）

## session 的作用范围（一次性 vs 长期）

`data.csv` 是**生成物**（下载层 + layout + 一次性 + 长期规则 → 重算），所以临时加的
一场不能直接手写进去 —— 下一次 `populate` 会把它冲掉。两个独立的小文件接住它：

| 文件 | 谁写 | 语义 |
|---|---|---|
| `temp/session-extras.json` | `session HH:MM LEN` | **一次性**：只属于那一天，日期行，不复制到下周 |
| `temp/session-rules.json` | `session --permanent HH:MM LEN` | **长期**：按星期几重复（`mon,09:00,25`），sync 之后仍在 |

`plan_rows()` 把三者合并成一种形状（`source = period / extra / rule`），
`render_data_csv()` 去重后写出去；`sessions_for_day()` 给界面/重复任务用。
`plan_counts()` 是给状态框和 populate 用的「时段 n / 一次性 n / 长期规则 n」。

重算 `data.csv` 时如果冲掉了不在计划里的旧行，warden 会把它们记进警告
（`self.csv_dropped`，附 `temp/data.csv.bak` 备份），不会静默消失。

## 落盘安全

- `plan_period()` 纯计算、不写文件。
- `set_layout()` 先完整验证，再写 `session-layouts.json`；验证失败不留半截 layout。
- `data.csv` 只在 layout 成功后重算；daily schedule 的精确日期行优先于旧的
  weekday 循环规则（同一时刻日期行覆盖）。
- **旧 API session 对齐**：改 layout 前，先找出同一时段内「起点或时长不再匹配
  新排布」的已订 session 并取消，否则会出现 `22:15(50)` 和 `22:15(25)+22:45(25)`
  并存的脏状态。
- **改排布不需要宣言**（用户明确要求）：排布是「计划」，点 UI 就该能调；
  有阻力的仍然是**取消单场**（`cancel N` 那条路一点没放松）。
  取而代之的是取消前面板把「要取消哪几场 / 会清掉哪些任务绑定」列清楚。
- **任务绑定按「这场有没有动」处理**：判据是 `(起点, 时长)` 完全一致。
  - `T1 50 25 25 → T1 50 50`：第一场还是 `11:00(50)` → **它的任务绑定留着**；
    后两场没了 → 那两条绑定清掉。
  - `T1 50 25 → T1 25 50`：没有一场对得上 → **两场的绑定全清**。
  - 清绑定 = `TaskStore.unassign()`：任务本身留在任务库，`session` 置空，
    Focusmate 侧那条 task 记进 `focusmate.previous`（带当时的 session 和
    `reason="layout-changed"`），以后它被勾完成时 `task sync` 照样按
    `completedAt` 归档；`completed_at` 不抹（那是历史）。
  - 绑定只在**排布落盘之后**才清：写失败就什么都不清。
- 取消成功后才写新 layout / data.csv，然后立刻触发一轮 `keep_session.py check`
  补订新场次。
- `schedule.py` 同时支持 `YYYY-MM-DD,HH:MM,duration` 与旧格式，daemon 不需要改接口。

## 命令

| 命令 | 作用 |
|---|---|
| `download_calendar.py download/fetch` | 只下载工作+空闲 → 中间层 JSON + md |
| `download_calendar.py populate --from-download` | 中间层 + layout → data.csv |
| `download_calendar.py layout T1 50 50 [--date ...]` | 验证并保存一个时段的排布 |
| `download_calendar.py show` | 打印所有时段及其当前 session |
| TUI `session T1 50 50` / `period T1 50 50` / 裸 `T1 50 50` | 同上 |
| TUI `session HH:MM LEN` | 只加这一天（一次性），立刻 check |
| TUI `session --permanent HH:MM LEN` | 写进日历（按星期几重复） |

## 边界

- 只认 25/50/75（Focusmate 约束）；超时/非法时长直接 ValueError。
- 不支持在时段里手工指定绝对起点；需要更自由的排布就改日历事件本身。
- 没有目标 profile 时退回旧的 ignore/use 过滤，避免把结构不同的日历静默清空。
