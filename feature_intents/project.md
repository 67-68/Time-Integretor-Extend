# project

## 模块职责

把 **OmniFocus 项目**接进这套系统：项目是「活的母体」，它的任务清单先躺着不动，
等到你给这一天分了资源，才把当天的任务「唤起」成本地任务、排进 session。

```
project_store.py   data/projects.json   项目库：清单 + 每天的分配 + 谁被唤起过（纯数据层）
project_plan.py    纯逻辑               容量 / 分资源问答 / 挑任务排 session
warden.py          接线                 task import 认项目、任务面板下的项目表、plan 命令
```

和任务库的分工（两个文件、两件事，别混）：

| 文件 | 放什么 | 谁写 |
|---|---|---|
| `data/tasks.json` | **这一场（这一天）要干的活** + 项目叶子任务的镜像（`fields.project` / `project_id` / `project_task`，没挂 session 的镜像只进任务视图，不进契约面板） | `task create` / 项目导入/刷新/对账镜像 / `plan` 唤起 / 重复任务生成 |
| `data/projects.json` | **母体**：OmniFocus Project 的任务清单 + 每天分了多少 count + 唤起过哪几条本地任务 | `task import`（项目）/ `project refresh` / `plan` |

用户原话就是这两句话，实现也照这个分：
*「在项目入库的时候就应该获取他所有的 task，但不展示直到他们被 project 唤起」*、
（实现上：项目导入时把**叶子任务**镜像进任务库，供任务视图搜索 / `group project`；
镜像没有 session 时不会进契约视图的任务面板，`plan` 唤起时复用同一条镜像并挂 session。）
*「你可以把 project 想象成为一个 repeated task 的母体（会在对应的时间产生 task
指向他）的变体」*。所以项目**永远不挂 session**（和重复任务本体一样），挂 session 的
永远是它唤起的本地任务。

## 数据形状

```json
{
  "version": 1, "next_id": 2,
  "projects": [{
    "id": 1,                                  // 项目序号 = 预览表上的 #（task 1 4 里的 1）
    "title": "论文",
    "omnifocus": {"id": "p-abc", "url": "omnifocus:///project/p-abc", "title": "论文"},
    "status": "active",                       // active / dropped（撤掉是删记录，不是标记）
    "imported_at": "…", "synced_at": "…",     // synced_at = 上次读 OmniFocus 的时间
    "tasks": [{                               // **全部**任务（含已完成/已放弃），按 OmniFocus 顺序
      "pk": "t-intro", "title": "写引言", "parent": "论文", "parent_id": "",
      "due": "2026-11-04T11:50:00",
      "count": 3, "duration": 75,             // estimated minutes ÷ 25（往上取整）
      "completed": false, "dropped": false,   // 任务自己的标记
      "effective_completed": false,           // OmniFocus 的有效状态（含父任务继承）
      "effective_dropped": false,
      "blocked": false, "rank": 0
    }],
    "plans": {                                // 按天记：这一天说好多少 count、真排了哪几条
      "2026-11-04": {
        "count": 4, "at": "…",
        "items": [{"pk": "t-intro", "task_id": 12, "count": 2,
                   "start": "11:00", "duration": 50}]
      }
    }
  }]
}
```

`plans[day].items[].task_id` 指回任务库 —— **进度就是靠它算的**（见下）。

## 导入：`task import` 认出项目

剪贴板里是项目（链接形如 `omnifocus:///project/PK`，`omnifocus.py` 现在两种链接都认）
时，走的是另一条路：

1. **只认 primary key**：这个 pk 必须在 OmniFocus 的 `list` 里是一条
   `kind == "project_root"` 的行。不拿标题猜身份（猜错了就是「导入了一条叫项目名的
   任务」，项目里那些任务永远进不来）。
2. **先算本周会产出多少条任务**：除了项目当前 active 的任务，导入还会查：
   - **被推迟（defer 在未来）、但没有放弃**的任务；
   - **重复任务**的 OmniFocus `repetitionRule`，展开本周的 occurrence；
   - 同一个任务可以同时被推迟 + 重复：先按 defer 截断，再把规则落在本周的
     那几个 occurrence 各算一条。

   周的口径固定为**周一 00:00 到下周一 00:00**，不从导入当天滚动七天。比如
   周三导入，周四才开放的被推迟任务算本周；重复任务本周一、周三各一次，就按
   **两条**算，哪怕 OmniFocus 现在只存在当前这一条。
3. **时长是硬门槛（只查有效 active / deferred）**：项目里**每条有效状态为
   active 的任务**（含被推迟的）都必须有「持续时间」（OmniFocus 的
   `estimated_minutes`）。子任务的有效状态会继承父任务：父任务已完成或已放弃时，
   没有单独改过完成/放弃状态的子任务同样视为已完成/已放弃，不参与校验和排期。
   少一条 → **整批拒绝导入**，列出是哪几条；
   本周会产出的重复 occurrence / 被推迟任务缺时长时，还会点名具体日期。这一次
   **一条任务、一个项目都不建**（用户要求：「报错并让用户去设置，拒绝导入」）。
   理由很简单：没有时长就算不出几个 count，而项目一旦被唤起就是满地 0 count 的任务。
4. 时长齐了 → 写进**项目库**（`upsert`，同一个 OmniFocus 项目只留一条）；
   第二次导入同一个项目 = **刷新它的任务清单**，不重复建。每个任务的 `defer` /
   `planned` / `repeat` 规则也会一起保存，之后 `project` 仍能重算本周账。
5. 清单里的任务**一条都不进任务库**（不占 session、不上任务面板）。

归组只认 OmniFocus 的 **`project_id`（项目 primary key）**，不再按项目名分组，
所以两个同名项目也不会串。旧版清单没有 `project_id` 时才退回按 `project` 名字
分组，并在同名时明确报警。父任务层级同样优先用 `parent_id`。

已完成 / 已放弃的任务照样进清单（算进度和保留层级要用），但**不参与层级 /
时长 / 本周任务校验**；`project refresh` 时按最新状态覆盖。

## 子任务：最多父-子两层

项目导入按 OmniFocus 的 `parent_id` 保留任务层级，但**最多两层**：

```
父任务（空壳，不直接占 session）
├─ 子任务 A（不可拆）
├─ 子任务 B（不可拆）
└─ 子任务 C（不可拆）
```

- 父任务 + 子任务之外再出现孙任务（三层）→ **整个项目拒绝导入**，并点名是哪条。
  只检查有效状态为 active 的任务；已完成/已放弃的旧层级不挡导入；
- **状态继承**：优先信 OmniFocus 的 `taskStatus` / `effective_completed` /
  `effective_dropped`（它已经算好父任务继承）；旧清单或 AppleScript 兜底没有这些
  标记时，再按 `parent_id` 推导。父任务完成 → 子任务按 completed 算；父任务放弃
  → 子任务按 dropped 算。子任务自己显式标了 completed / dropped 的，按子任务
  自己的状态优先。继承关闭的子任务不计入本周预算、不进入 `plan` 的可用任务，
  也不要求 estimated_minutes；
- 父任务本身是空壳：不需要自己的 estimated_minutes，剩余时长 = 未完成子任务
  时长之和；项目进度/本周任务预算只按叶子任务算一遍，不重复计父壳；
- 子任务不可拆到两场 session。排期以父任务为单元选场次，但**session 里只建子
  任务，不建父任务**；每条子任务 `note` 写「父任务：X」；
- 25 分钟场遇到 10 + 13 + 27 分钟三条子任务：选 `10+13=23` 这一组排进去，
  27 留给以后；如果一场放得下全部，就整组一起排；
- 一条子任务完成后，项目库里记它完成；**所有直接子任务完成 → 父任务自动完成**。
  普通 `task done` / 收尾完成会立刻把父任务写回 OmniFocus；`-m complete` 仍只
  动本地账本，不立刻写外部。

## 每日项目对账

交互式 TUI 每天第一次打开（或当天第一次 `refresh` / 手动 `project check`）会
后台读一次所有 active 项目的 OmniFocus 清单。判断口径：

- **新增任务**：远端有、本地没有 → 自动拉进项目库，不打扰；
- **删除 / 修改**：远端少了任务，或已有任务的标题 / 持续时间 / defer / due /
  planned / 父任务关系变了 → 整个项目只问一次：
  - `[l]` 保留本地覆盖（新增照拉，删除/修改不应用）；
  - `[o]` 用 OmniFocus 覆盖本地清单；
  - `[s]` 先不处理。
- 同一天只自动检查一次，日期戳在项目库同目录的 `project-sync.json`；检查失败
  也不挡 TUI。

## 项目表：挂在任务面板下面

契约视图里，`Task · 任务` 面板**下面**那张表（`render_projects_table()`）：

```
 #  项目   剩余 count  进度   今天 plan         未完成  OmniFocus
 1  论文    9          1/10   说好 4 count·已排 4   3     p-abc
 2  读书    5          0/5    —                  2     p-dushu
```

| 列 | 怎么算 |
|---|---|
| 剩余 count | 未完成任务的 `count − 已投入`，求和 |
| 进度 | 已完成 / 总共（都按 count 算；OmniFocus 那边 `completed` 的任务算满） |
| 今天 plan | `plans[今天].count`（说好多少）+ `items` 里实际排下去的 count |
| 未完成 | 还有 count 可干的任务条数 |
| 本周 | 从周一起这一周会产出的任务条数：当前 active + 本周开放的被推迟任务 + 重复任务本周的每个 occurrence（重复 + 被推迟只算入重复 occurrence，不重复相加） |

**已投入怎么算**（`project_store.contribution`，只有一个事实来源 —— 任务库的
`count_invested`，项目库不记第二本账）：

- 本地任务**完成**了 → 整条算数（它分到几个 count 就是几个）；
- 还在进行 → 算 `count_invested`（干了一半就是一半）；
- 放弃了 → 0（放掉的那部分活还在项目里等着）。

## `plan`：分资源 → 唤起任务 → 排 session

用户原话：*首先列出元数据，比如说总共多少空闲时间，同时，展示时间 / 25 的 count；
然后询问每个 project 如何分配时间*。所以分三步：

### 1) 元数据（容量）

「空闲」= 这一天**空着、而且已经订上**的 session（`_plan_slots`）：

- 计划里有、没静音、今天的还要「还没开始」；
- 已经挂着任务的场次不算（那一格已经有活了）；
- **被 formatter 占掉的时间块不算** —— formatter 是第一阶段的硬约束，项目 plan
  只能使用它剩下的空场，不能覆盖或改写 formatter 的切分；
- **没订上 Focusmate 的不算** —— 拿一场可能不存在的 session 当资源，等于把任务排进
  空气里。面板会点名「哪几场还没订上，`refresh --check` 订上之后再 plan」。

容量 = Σ 场次时长 ÷ 25 count（`1 count = 25 分钟`），同时给出「含每块 5 分钟休息」
的分钟数（用户原话：*4 count = 100 分钟，如果加上 focusmate 25 分钟 session 的
五分钟休息是 120 分钟*）。

### 先选哪一天：`plan` 默认今天

```
plan            今天（**和看板在看哪天无关**）
plan +1         明天（plan 1 / plan 明天 / plan 2026-11-07 同义）
plan -1         拒绝：过去的日子不用 plan
```

为什么不跟着 `day` 走（用户原话：「plan 默认 plan 今天」）：`plan` 是「给哪一天分资源」
的**动作**，不是看板；晚上正看着明天时顺手敲 `plan`，想排的多半是今天剩下的空场。
要排哪天就写哪天。目标那天和看板不一致时，`cmd_plan` 会先把看板搬过去
（`day_offset` + `refresh(wait=True)`），因为「空场」和 `-a <编号>` 都得看**那一天**的
真数据 —— 拿别的天的旧行去算，会把任务排进一场根本不存在的 session 里。

### 候选队列：formatter 与 defer

`plan` 从项目清单挑任务时，有两类**不进入候选队列**：

- 本地任务已经挂了 `formatter` 的：它只还挂在项目下作为归属，时间由 formatter
  自己占，项目 plan 不再把它当“需要安排的东西”；
- `defer` 还没到点的任务（无论项目清单上的 `defer`，还是本地任务的
  `fields.defer`）：到点前不参与自动排期。`refresh` 的流水线也会重查：
  已经被 plan 唤起但后来被 defer 到未来的任务，会让当天 plan 失效并要求重新安排。

### 2) 分资源（问人）

面板在 repl_kit 的打断栏位上（和收尾 / 到点没开始同一个位置），输入：

```
task 1 4       项目 #1 分 4 个 count（= 100 分钟）
task 1 2       再敲一次是**覆盖**（变成 2，不是 6）
task 1 clear   清空 #1 的分配
task 1         只看 #1 的清单，不动分配
complete       分配完了，开始排任务
q              放弃这次 plan（一个字节都不落）
```

**超预算整条命令都不执行**（用户要求）：已经分了 4、容量 5，再敲 `task 2 3` →
报错、状态一个字都不动，并说清楚「现在还能给它 1 count」。判据是
`others + count > capacity`，只算内存里的分配，不落盘 —— 所以「说好的数」只有在
`complete` 之后才写进项目库，中途放弃什么都没有发生。

### 3) 排任务（自动）

`distribute()`：按项目预览表的顺序、项目内按清单顺序，把分到的 count 切成具体任务
（`task 1 of project 1 needs 3 count, task 2 needs 4 count` → 分 5 个 count 就是
「3 个给任务 1 + 2 个给任务 2」）。项目清单里的活不够填满 → 少排，并如实说
（那不是错误，是清单的事实）。

`schedule()`：两条原则（用户原话）——**尽量让每个任务占有自己的独立 session**、
**尽量让每个任务连续**（别把一条任务拆到两场）。所以：

1. 先跑一次二分图**最大匹配**（任务 ↔ 场次，场次容量 ≥ 任务要的 count）：
   有「一场一个任务、谁都不拆」的排法就一定找得到，而且挑浪费最小的场次；
2. 配不上的（比每一场都长，或者场次不够）才**拆开填**，并且明说拆成了哪几场；
3. 实在没地方 → 报「空场次不够」；
4. 场次还空着但已经被分到一条任务时，后续的零头会**叠进同一场**（`stacked`，
   同样明说）—— 只在必要时这么干。

唤起出来的本地任务长这样（`fields` 里的三个键就是界面上看得见的列）：

```json
{"fields": {"title": "写引言", "count": 2, "duration": 50,
            "project": "论文", "project_id": 1, "project_task": "t-intro",
            "due": "2026-11-04T11:50:00"},
 "omnifocus": {"id": "t-intro", "url": "omnifocus:///task/t-intro", …},
 "source": {"kind": "project", "id": "t-intro", …}}
```

然后照常走 `task <序号> -a <#>` 那条路挂 session（Focusmate 那边建 task），
`due` 由 `assign` 按 session 终点自动写。排完把「说好的 count」和「真排了哪几条」
一起记进 `plans[day]`。

## 「今天还没 plan」会提醒

状态框（契约视图最上面那块）里多一条 warn，条件三条全中：

1. 项目库里有 active 项目；
2. **今天一个项目都没分过 count**（`plans[今天].count` 全为 0）；
3. 今天还有**没开始、没挂任务、没静音**的场次。

```
今天还没 plan：还有 3 场空闲（6 count）没分给项目（11:00、12:00、15:00）
   → 敲 plan 分今天的资源（plan +1 = 明天；task 1 4 = 项目 #1 分 4 个 count）
```

判据只看 `data.csv` + 本地任务绑定（`pending_plan()` / `free_plan_slots()`），
**不问 API** —— 警告板每 5 秒算一次，不能为了提醒去拉网络。所以它说的是「还有几场
排好的空闲没分」，不是「订没订上」（那个由 `plan` 面板自己说）。

plan 过一次（哪怕只分了一个 count）就不再吵：提醒是用来提醒「别忘了」，不是记账。

## 部分完成不勾 OmniFocus 完成

一条 OmniFocus 任务总共 3 count，今天只分到 2 —— 这条本地任务干完时**不能**把
OmniFocus 那条勾完成（那会把剩下的 1 count 抹掉）。所以 `omni_complete()` 先过一道
`pj.completion_guard()`：

- 这条不是项目唤起的 → 照常写回；
- 是，但本地累计还没到 OmniFocus 那条的总数 → **挡下来**，说清「还差 N count」，
  本地照常完成（Focusmate 也照常勾）；
- 累计够了（跨天也算，因为 `count_invested` 都在任务库里）→ 照常写回。

## 命令

| 命令 | 作用 |
|---|---|
| `task import`（剪贴板里是项目） | 进项目库；时长缺一条就整批拒绝 |
| `project` | 列出项目（剩余 count / 进度 / 今天的分工） |
| `project <序号>` | 一个项目的清单 + 今天排了什么 |
| `project <序号> refresh` | 重新读 OmniFocus（时长又缺了 → 拒绝，清单保持旧的） |
| `project refresh` | 所有项目都刷一遍 |
| `project check` | 立刻做一次每日对账：新增自动拉；删除/修改按项目问「本地 / OmniFocus / 先不处理」 |
| `project <序号> remove` | 从项目库撤掉（**OmniFocus 不动**；已经排进 session 的任务照旧） |
| `plan` / `plan +1` | 开始分配（走「一次只问一件事」的打断栏位）；不带参数 = 今天，`+1` = 明天 |
| `task <序号> <count>` / `task <序号> clear` / `complete` / `q` | plan 里的那几句 |

## 边界

- 不碰 Focusmate 的写接口以外的东西：排期只调现成的 `task assign` 那条路；
- 不改 `data.csv`：`plan` 只往**已经排好的** session 里放任务，不新增/挪动场次；
- 不联网（`omnifocus.py` 只在导入 / 刷新时被调用，且是**只读**）；
- 项目没有「自动同步」：清单是快照，`project refresh` 或重新 `task import` 才更新。
  进度里「OmniFocus 已完成」的那部分同理，靠刷新跟上；
- `temp/` 里的东西一概不碰；项目库是 `data/projects.json`（有 `.bak` 上一版）。
