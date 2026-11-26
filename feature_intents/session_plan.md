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

### 时段是第一等输入：没有 session 行也照样算容量

`data.csv` 里某个时段一行都没有（还没 plan 过 / 上一轮分到 0 count），**不等于
这段时间不存在**。2026-11-16 之前 `_auto_period_slots` 有一条「没有真实行、
也没有 layout 记录就跳过」的守卫，结果用户今天 19:00–21:30、22:15–23:15 两段
在 plan 里整段消失（原话：「今天的时段似乎完全被忽略掉了一部分」）。

现在：`period_meta` 里的每一段都参与排期，整段容量 =
`floor(时段分钟 / 30) × 25`（`pp._period_blocks`）；真正的切分交给
`commit_layout` 按任务反推 session 时长再写回 data.csv。已经过去 / 被静音 /
被 formatter 硬占 / 有任务挂着的时段仍然照样排除（语义没变）。

### 窗口裁剪：`period T1 09:45-12:30`

用户 2026-11-26 的要求：「提供一个切割时段的命令，允许修改一个时段的开始和
结束，只要被原本的时段包裹就行」。落地口径：

- 写法 `period T1 09:45-12:30`，落盘 `temp/period-windows.json`
  （`{"days": {"2026-11-26": {"T1": {"start","end","origin_start","origin_end"}}}}`），
  `period T1 window [clear]` 查看 / 撤销；
- 必须被**日历原时段**包裹（09:30–12:30 的 T1 只能往内收），且是 15 分钟整点
  （Focusmate 槽位颗粒），至少 25 分钟（想关掉这一段请用 `period T1 clear`）；
- 生效点是 `periods_for_date()` —— 容量、切场、看板、data.csv 全都从这里取时段，
  所以只改一处就不会出现「看板按新窗口、data.csv 按旧起点」的分叉。`all_sessions()`
  也必须跟着走（data.csv / daemon 吃的是它，2026-11-26 自测踩到过）；
- 窗口一变，这一段已有 layout 按新起点重算：装得下就原样搬过去，装不下就清空该段
  并提示 `plan` 重切（留着排不出来的 layout，下次 plan 会一直报错）。

### 工具切的 layout 记 `auto`，extras 算硬洞

- `commit_layout()` 没传 `source` 时落 `auto`（不是 `manual`）：formatter / watchdog
  切完的段必须还能被 plan 重算，否则切掉的容量永远补不回来。
- `available_windows()` 除了手订场，还要扣 `temp/session-extras.json`
  里的场次（`extra_session_holes()`）：它们不在 layout 里，但是真实占用的时间。
- 新 layout 覆盖到的起点，同一天的一次性 extras 由 `commit_layout` 收掉，
  避免同一时刻既在 layout 又在「计划外」出现两次。

### 洞只吃掉自己那段：manual 时段的中间空档也要还给 plan（2026-11-26）

用户现场原话：「**19:25 - 21:25 那里去了？？？？**」「**这两个 session 依旧占掉我
整一个时间段**」。T3 是 formatter 切的 manual `[25]`（19:00 一场）+ 21:00 一条
extras：`tail_gap()` 拿**所有**行去验「序列对不对得上」，一条 extras 就让整段
作废 → 2.5 小时的时段只产出两场 25 分钟，中间 3 个 count 凭空消失。

口径：

- 洞（extras、手订场）只按 `session_span()` 扣自己的占用，**不让整段变成 0 容量**；
- layout 链上的场次原样保留（manual / formatter 是硬约束，不挪不动）；
- 链尾之后的空档拆成若干窗口：整条尾巴都空 → 仍旧「原样追加」（`append`，
  `prefix + 新时长`，一个已订场次都不动）；被洞切开 → 每截 ≥1 count 的窗口
  各自成一个空场，`window=True`，落盘走 `extras`（layout 表达不了洞后的起点）；
- 洞的占用从 **extras 账本**取，不只从 data.csv 的行取：extras 刚写进去、csv
  还没重算的那一瞬，两边会不一致，漏掉就会排上去压住。
- `_auto_period_slots()` 末尾那条「blocked period 一律 `period_auto=False`」必须
  跳过窗口 slot，否则它会走固定 group 分支、挂到一条不存在的行上。

### extras 也分「谁切的」：plan 的窗口可以重算

`temp/session-extras.json` 每条记 `source`（`sp.extra_source()`，老记录按 note 认）：

| source | 谁写 | plan 能不能重算 |
|---|---|---|
| `plan` | 排期为了避开手订场切出来的窗口 | 能（**任何** layout 的时段里都不算硬洞） |
| `formatter` | formatter 按任务尾巴切的 | 不能（钉死，只许往里塞任务） |
| `manual` | 用户 `session HH:MM LEN` 自己加的 | 不能 |

「能不能重算」只看**谁切的**，不看它落在哪种 layout 里（2026-11-26 补）：
manual layout 的时段（formatter / watchdog 切过）里，上一轮 plan 切的
`19:30(50)+20:30(25)` 曾经变成永久固定桩 —— 这一轮明明有 19:30–21:00 一整块
3 count，却只能按 50/25 两个槽填，3 count 的项目只好被「填缝」拆成 25+25+25
撒进三个不同场次，还把 22:15 的 50 分钟场次填成 1 count（用户：「他为什么被拆成
三块了？明明 2 和 3 也可以连起来」「让 SAT 和 English Review 完整排期」）。

例外（仍然是硬洞）：**挂着本轮不会撤掉的任务**的 extras —— 那是内容约束，不是
切分。判据 `_recuttable_extra_starts(day, replan)`：`source` 是 plan/auto，
且那个起点上的任务全部属于本轮计划（`replan=False` 时只要挂任务就不算）。

配套的两条：
- `_row_slots()` **不认**这些窗口行当固定槽（否则它们又会以「50 / 25」的形状
  回到容量里），容量统一由 `_auto_period_slots()` 的尾巴逻辑重算；
- API 里还没换掉的旧场次（同一场改了时长、或整条被并进前一场的碎片）也不能当
  资源或内容：`_superseded_row_starts()` 按「计划里这一段应该长什么样」把它们
  剔掉（`_row_slots` / `_period_session_rows` / `manual_session_rows` 三处一致）。
  没有这条，「改时长 → daemon 还没取消完 → 再 plan」会把刚合成的整块再切碎一次。

以前不分出处：`plan clear` 之后 plan 自己切的窗口变成了永久硬洞 —— 那截时间
既不在 layout 里、又被当成手订场，**永远回不到时段容量**（用户 2026-11-26：
「plan clear 之后 session 没有办法恢复到时段」）。现在：
- `extra_session_holes()` 在**自动 layout** 的时段里跳过 `plan`/`auto` 来源的 extras；
- `commit_layout()` 重切一段时顺手把这一段里 `plan`/`auto` 来源的 extras 收掉
  （配合 `_cancel_stale_period_sessions` 取消对应的旧场），不会留下重复行；
- formatter / manual 的 extras 语义没变（硬洞 + 不许压）。

契约表也照着这条走：**每个时段至少一行**。没切出 session 的时段由
`_period_shell_rows()` 画一行占位（`period_only=True`）：时段三列照常显示、
时长写「整段 Nmin」、状态写「未切 session」；它**不是** session（`id=None`、
`planned=False`），`_row_slots` / `check_bookings` / 取消 / mute / 挂任务
全都不认它 —— 只用来回答「这一段到底有没有被算进去」。

### 资源回收：没任务的零散自动场次收回成「未切分容量」（2026-11-26）

用户口径：「我希望 refresh 之后直接把零散 session 变成一个统一的时间块，如果没有
任务」「不是两场 25 合并 50，而是两场被自动切出来的 25 换成一个 50 分钟的无切分
时段，相当于资源回收」。

所以回收的终态是**那段时间一场 session 都没有**（不预约、不新建）：容量还给时段，
下一轮 plan 想切几场切几场（可以 2×25，也可以 1×50）。实现是
`SessionMixin.reclaim_idle_auto_sessions(day)`，挂在 refresh 的排期流水线里
（`_reconcile_schedule_pipeline`，剪枝之后、formatter 之前）。

能收 / 不能收：

| 形状 | 收不收 |
|---|---|
| layout 链的**尾巴**上、`source` 是 plan/auto、没任务、还没开始 | 收 |
| layout 链的**中间**（后面还有场次） | 不收（layout 是从起点接龙，中间摘一场会把后面全挪位） |
| extras `source=plan/auto`、没任务、还没开始 | 收（窗口各自独立） |
| `source=manual`（手敲 `period`）/ `formatter` | 不收（硬约束） |
| 挂着任何任务（含 formatter / plan 提醒任务） | 不收（那是内容，不是碎片） |
| 已经开始 / 已经过去 | 不收（不回头改历史） |
| 已经订上、且 60 分钟内就要开始（`RECLAIM_LEAD_MINUTES`） | 不收（别把已经匹配上的人踢出去）；没订上的照样收 |
| 已经订上、但这一轮**取消不掉**（离线 / 取消失败 / 被手订保护） | 不收，整段跳过（`plan.reclaim_booked_kept`）—— 撤了契约又没取消，daemon 只会认成「booked but missing」并记 sin |

落盘顺序和 `commit_layout` 同一条：`_cancel_stale_period_sessions`（挂号单 + 清
state，daemon 不会记成「你手动取消」）→ 写 layout（**`source` 和 `plan_sig` 原样
保留**，否则下一次 refresh 以为计划变了，把刚回收的整段又切回来）→ 收 extras →
`rewrite_data_csv_from_plan()`。幂等：收干净之后第二次 refresh 一场都不收。

`refresh` 因此会写 data.csv、会取消旧场 —— 这是它唯一的「减项」副作用，契约从
「只对账、不改 data.csv」改成「不订场、不建 task，但会回收」。容量前后相等
（这些场次在排期层本来就已经被当成可重切的），所以回收不丢 count。

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

### 改排布不能把 daemon 惹毛（三件套）

改排布是「取消旧场 → 清 state → 写新排布」三步。daemon 要是刚好在中间跑一轮，
就会看到「state 说 booked、API 里没了」→ 按「你手动取消」记一笔罪 → **TUI 锁死**，
而且那几场被判「永不重订」，新排布里的场次永远补不回来（2026-10-27 真踩过：
`period T3 50 75` 之后 `20:00` / `21:00` 各记一笔 `booked-but-missing`）。

> **2026-11 起这三件套只有一份实现**：`warden_core.session_mixin.commit_layout()`。
> 以前 `period` / `plan` 自动排布 / formatter 切割 / formatter 兜底加场 / CLI
> 各抄了一遍，而且**只有 `period` 那条路拿了 round lock** —— 同一条铁律，5 个
> 入口只有 1 个守住。现在所有写入者都走 `commit_layout`，它内部依次做：
> 纯计算验证 → 撤「永不重订」判决 → 拿 round lock → 写挂号单 → 取消旧场 →
> 写 layout → 清任务绑定 → 重算 data.csv → 放锁。
> 取消旧场也只有一份：`_cancel_stale_period_sessions(day, period, new_sessions)`
> （吃 `_layout_rows(day)`：看板那天用 `self.rows`，别的天用快照）。

1. **同一把 round lock**：这三个步骤包在 `keep_session.round_lock()` 里，和 daemon
   的一轮互斥。等不到锁（daemon 在下单，可能几十秒）会提示并最多等 30 秒；
   实在等不到就照常改，用第 2 条兜底。
2. **挂号单**（`temp/keep_session-expected.json`）：真正取消**之前**，
   把「这一时段里改完就不该再存在的 state key」全部写进去
   （`ks.expect_cancel(keys, reason="layout-changed:T3 …")`）。
   daemon 看到 `booked-but-missing` 时先查挂号单：命中 → **不记罪**、照旧重订，
   并把那条摘掉（一码一用，不掩护以后真的手动取消）。TTL 6 小时，过期自动清。
   key 的来源有两处：表格里有 id 但新排布不再匹配的场次，以及 state 里记着、
   新排布里已经不该有的槽位（表格没显示出来的也照样挂号）。
3. **撤销「永不重订」**：重新排布一个时段 = 你又想要这几场，
   `ks.clear_abandoned(新排布里所有 key)` 会把 `verdicts.abandoned` 里的判决撤掉，
   否则认过罪的时刻永远补不回来。`session HH:MM LEN` 加单场时同理
   （只撤那一场的判决）。

> 注：「永不重订」是宣言真正的牙齿 —— 所以它只由 `cancel N` + 走完宣言的
> `confess()` 写入，改排布只会**撤销**它，绝不写入。
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
