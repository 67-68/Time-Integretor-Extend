# task

## 模块职责

本地任务库（`task_store.py`）：JSON 持久化、CRUD、任意字段、挂 session、
Focusmate 完成归档。任务本身**不做字段校验**——想记 `title` / `due` / `priority` /
`任意 key` 都行；模块只维护少量内部簿记键（id / fields / session / focusmate / 时间戳）。

```
data/tasks.json          本地任务库（原子写；坏 JSON = 空库，不炸）
data/task-archive.json   完成归档（只增；按 task+focusmate+completedAt 幂等）
```

## 数据形状

```json
{
  "version": 1,
  "next_id": 3,
  "tasks": [{
    "id": 1,
    "fields": {"title": "写论文", "due": "2026-10-26T11:50:00", "due_source": "session",
               "count": 2, "count_invested": 1, "duration": 50},   // count ↔ duration 联动
    "created_at": "...",
    "updated_at": "...",
    "status": "active",                  // active / completed / dropped
    "status_at": null,
    "tags": ["未完成转移"],                // **本地**标签（不动 OmniFocus）
    "omnifocus": {"id": "kc-4VLAE_1L", "url": "omnifocus:///task/kc-4VLAE_1L",
                  "title": "写论文", "linked_at": "...",
                  "writeback": {"completed": "..."}},
    "duplicate_of": null,                // 从哪条复制来的
    "actual_minutes": null,              // 「缺漏完成补充」手填的实际耗时
    "source": {"kind": "omnifocus", "id": "kc-4VLAE_1L",
               "url": "omnifocus:///task/kc-4VLAE_1L", "title": "写论文"},
    "trigger": {"name": "...", "params": [], "args": {}, "set_at": "..."},
    "formatter": {"name": "end", "params": [], "args": {}, "set_at": "...",
                  "reservation": {"date": "...", "start": "17:30", "end": "18:00",
                                  "session_start": "17:30", "session_duration": 25,
                                  "duration": 25, "break_minutes": 5}},
    "repeat": {"rule": "FREQ=DAILY",          // OmniFocus 原 RRULE；老任务可为空
               "rule_anchor": "2026-11-04T11:10:00",
               "rule_offset_days": 0,          // 跨午夜：场次起点 - 规则锚点的日期差
               "weekday": 2, "weekday_label": "wed",  // 首个场次的显示字段
               "start": "10:45", "duration": 25,
               "omnifocus": {"method": "DueDate", "schedule_type": "Regularly",
                              "anchor_date_key": "DueDate"},
               "occurrences": {"2026-11-04": {"child_id": 2,
                                               "session": {...},
                                               "focusmate": {...}}}},
    // 下面是 occurrence 子任务独有的两项；本体没有
    "repeat_parent": null,               // 子任务：本体 id；本体为 null
    "repeat_date": null,                 // 子任务：这一天的 YYYY-MM-DD
    "history": [                         // 哪一天的哪一场「处理」过它（见下）
      {"day": "2026-10-27", "start": "11:00", "action": "skip", "at": "..."}
    ],
    "count_credits": {"2026-10-27T11:00": 2},   // 哪一场已经给它记过 count（幂等）
    "session": {"date": "2026-10-26", "start": "11:00", "end": "11:50",
                "duration": 50, "focusmate_session_id": "..."},
    "focusmate": {"task_id": "...", "session_id": "...", "completed_at": "..."}
  }]
}
```

`fields` 是用户字段的容器，所以字段名不会和内部簿记冲突。

### 状态 / 标签 / OmniFocus 关联（`RESERVED` 里的新成员）

- **`status`**：`active`（默认）/ `completed` / `dropped`。
  **完成和放弃的任务不隐藏、不删**（需求：session 结束之后「那一场干成了什么」
  仍然要看得见），表格里只是前面多一个记号：

  | 状态 | 记号 |
  |---|---|
  | `active` | （空，表格里显示 `·`） |
  | `completed` | `✓` |
  | `dropped` | `✗`；如果带标签「未完成转移」则是 `⤼` |

  老数据没有 `status` 时按 `focusmate.completed_at` 推：有完成时间就是 `completed`。
- **`tags`**：**本地**标签（和 OmniFocus 的 tag 不是一回事）。目前只有
  「未完成转移」这一个有语义：它表示「这条记录是被延期/推后走的，活还在，
  只是换了账本」。
- **`omnifocus`**：关联到 OmniFocus 任务的 primary key。**只有 primary key 是硬的**，
  认不出来就不关联（拿标题猜身份比不关联更糟）。写回痕迹记在
  `omnifocus.writeback` 上（幂等用）。
- **`duplicate_of`**：这条是从哪条复制来的。收尾里「延期到底成立没成立」
  就是靠它判的（同一条 OmniFocus 任务的多条本地记录）。
- **`actual_minutes`**：「缺漏完成补充」手填的实际耗时，归档里也有一份。
- **`focusmate`**：Focusmate 侧那条 task 的账。`task_id` 是当前挂着的，
  `previous` 是历史（改挂到别场、或者**转移之后被删掉**的旧 id 都留在这儿），
  删掉时写 `deleted_at` / `deleted_reason`（见 `forget_focusmate()`）。
- **`history`**：「这条任务在哪一天的 session 里被处理过」。收尾/查账的每个动作
  （放弃 / 完成 / 补录 / 重议 / 推后 / 转移走 / **跳过**）都留一笔，
  `(day, start)` 相同只留最后一条，最多 `HISTORY_KEEP` 条。
  契约视图下面那块**任务面板按天取值**就靠它：今天 = 没挂 session 的 + 挂今天
  session 的 + 今天处理过的（**排到明天的任务今天不出现**）；过去那天固化 =
  只剩当时处理过的。读写入口是 `mark_processed()` / `processed_on()`；
  老数据没有痕迹时 `processed_on()` 用 `status_at`（或 `focusmate.completed_at`）
  兜底，否则升级当天「过去几天」会整片空白。

## count：1 count = 25 分钟

两件事被同一个数字绑在一起，而且**永远得是同一个事实**：

| 字段 | 意思 |
| --- | --- |
| `fields.count` | 这条活要几个 25 分钟的块（**最高**进度） |
| `fields.duration` | 分钟数 |
| `fields.count_invested` | 已经用掉几个 count |
| `count_credits` | 哪一场 session 已经记过账（幂等台账，保留字段） |

换算只有一个方向是精确的：`count × 25 = 分钟`；反过来 `分钟 → count` **往上取整**
（30 分钟的任务 = 2 个 count）。两边的关系**不对称**，因为改哪一边是用户说了算：

```
task modify 3 duration 30   → count = 2，时长**还是 30**（不许偷偷改成 50）
task modify 3 count 5       → duration = 125
task create ... duration=50 → count = 2
task create ... count=3     → duration = 75
```

这套换算只写在 `task_store.link_count_fields()` 一处（`create` / `modify` / 读盘
都会走它），`task_hooks` 也借同一份 `DURATION_KEYS`，别再各写一份。

### `-c/--count`（后缀）

```
task 3 -c 5      设定最高 count = 5（时长跟着 = 125 分钟）
task 3 -c +2     进度 +2；**加完够数 = 等同于完成任务**（走 task done 那条路）
task 3 -c -1     进度 -1（最低 0）
```

- 进度**不许超过**最高 count：`-c +5` 在 2/2 的任务上就是「顶在 2/2 并且完成」。
- `-c +N` 一旦够数，走的是和 `task done` **完全一样**的那条路：本地完成 + 归档 +
  勾 Focusmate + 写回 OmniFocus（不是另开一套「悄悄标完成」的岔路）。
- 设定 count 时如果已投入比新上限还大，进度会被压到新上限。
- 和 `-s/--from` 批量不能一起用（进度是单条任务的东西）。

### 界面上的 `(1+2/5)`

契约表格的 task 列和下方任务面板用同一个写法：

```
✓🔗3: (1+2/5) 数学易错修正
```

- `1` = 之前已经投入的 count
- `2` = **这一场结束会涨的** count = session 时长 / 25（往上取整）
- `5` = 最高 count

中间那截是**预报**，不是事实：它只在「任务还开着、挂在一场 session 上、而且这一场
还没记过账」时出现（记过账就变回 `(3/5)`，不然看起来像下一帧还要再涨 2 个）。
没设 `count` 的任务不显示这一格；没挂 session 的任务只显示已投入（`(1/5)`）；
收工/放弃、又一格都没记过的任务也不显示（`(0/1)` 只会让人以为「你 1 个都没干」）。

**默认的 `(0+1/1)` 不画**：一格都没投入、上限又只有 1，是普通 25 分钟任务的默认形状；
session 表格和下方任务栏都不显示它（`(0+2/1)` 同理）。有进度，或者任务本来就要
多个 count（例如 `(0+2/5)`，目标仍然有用）时才显示。

### session 结束：记账，但不自动完成

收尾一开始（`Warden.begin_review`）会先给这一场上的每条**没做完、且设了 count**
的任务记一笔 `session 时长 / 25`（写进 `count_credits`，同一场重开多少次只记一次）。

记完账**不等于完成**：够数（`count_invested >= count`）的那条留在收尾里问人
—— 「额外 count 还是直接完成」（见 [`postsession.md`](postsession.md) 的 `f`）。

`inspect-due` 的 **G 延长排期**走的是另一条路：它先问当前已投入几个 count、
还剩几个，然后用 `TaskStore.set_count_plan()` **原子地**把 `count_invested`
校准成已投入数、把 `count` 设成两者之和（`duration` 同步成 `count × 25`），
最后把同一个任务挂到下一场。不要用 `add_count` + `set_count` 两步拼 ——
旧上限可能先把进度夹住。
用户原话：*session 结束之后 count 不直接计算进入 task 并完成*。

## 序号 = 面板上的第几条（不是内部 id）

需求：「任务重置他们的序号，根据当前任务的数量从 1 到最多」。

所以命令里的数字是**序号**：这条任务在**契约视图那块任务面板上排第几**（1 起、
连续、不跳号）。口径就是 `task_panel_order()`，所以「面板上写着 3」和「`task 3` 指的
是同一条」永远成立 —— 这也正是它必须跟着面板重排的原因（删一条、收工一条，后面的
就往前顶，屏幕上永远不留空号）。

- 序号**不是**内部的 `id`。内部 id 是一条任务从生到死不变的那个数（`task list` 里
  「id」那一列）。两套数各管一件事：**序号给手敲用**（跟眼睛看到的一致），id 给
  账本和归档用。
- 序号缓存在 `task_number_index()` 里，键是「库文件 mtime + 哪一天」：库一写、天一
  切，缓存自己作废，不会给出旧号。
- 面板上**没有**的任务（别的天处理过的、归档里的）用显式写法 `id:15` 指定内部 id。
  另外「数字超过面板上界」时也会按内部 id 认，并明说一句（`（17 超过面板的 #1–#12，
  按内部 id 认：…）`）—— 免得对着 `task list` 敲了半天没反应。
- 面板上找不到、内部 id 也没有 → 报清楚面板现在有 #1…#几（不猜、不静默）。
- 契约表格 task 列、任务面板编号列、收尾面板的「任务 #3」、Recent 里的每一句
  —— 全部用同一个 `task_label()`，所以屏幕上不会同时出现两套编号。

## 任务视图（视图 4）的表头

任务视图一直画一张带表头的表：固定列是 `序号 | id | 状态 | 重复 | trigger |
formatter | session | 截止 | Focusmate | OmniFocus | 完成时间 | 标签`，后面按任务
实际字段追加 `title` / `duration` / …；没有任何字段时保留 `内容` 列。空任务库也先画
表头，不再只剩一个说明框。

`open` / `close` 在任务视图里作用于这张任务表（不是契约表）：

```text
close id
close title
close 截止 --permanent
open id
open title
```

被折叠的表头名会显示在表下面：`已折叠列：id、title（open <表头名> 打开）`。

## 命令

TUI（任务视图 / 任意视图都认 `task`）。下表的 `<序号>` = 面板上的第几条：

| 命令 | 作用 |
|---|---|
| `task create [id] [key=value ...]` | 创建；给了数字 id 就用它，否则自动编号。没有 `=` 的 token 拼成 `title` |
| `task delete <序号>` | 删除 |
| `task modify <序号> <field> <新值>` | 改任意字段 |
| `task select <序号>` | 选中任务（`task assign <session>` 的默认目标） |
| `task <序号>` | ← 和 `task select <序号>` **完全等价**（裸序号就是 select） |
| `task assign [<序号>] <session ID>` | 先挂 Focusmate，成功后落本地；成功即刻重排表格（task 列马上有内容） |
| `task repeat <序号> <周几@HH:MM\|off>` | 设置 / 取消每周重复 |
| `task create ... --repeat 周几@HH:MM` | 一步建「每周重复任务」 |
| `task -t` / `task --trigger` | 列出 trigger 预设（框架默认是**空列表**） |
| `task <序号> -t <preset> [参数...]` | 给任务挂一个 trigger（只记账；runner 执行） |
| `task -f` / `task --formatter` | 列出 formatter 预设（现在有 `end`） |
| `task <序号> -f end` | 按任务 `duration` 在当天最后一段的**尾巴上切一场**（11:30–11:55 那种）、写进排期并自动 assign；排出来的任务自动独占 |
| `task <序号> -e true`（`--exclusive true`） | **独占**：这一场 session 只能有它一条任务（裸写 `-e` = true，`-e false` 取消） |
| `task sync` | `GET /v1/tasks` → 读 `completedAt` → 写归档（顺带把重复任务挂到 session） |
| `task archive [N]` | 看归档：按完成日期分组、最近 N 条（默认 5） |
| `task <任何动作> ... --assign <N>` | 跑完顺手把产出的任务挂到表格 #N（`-a` / `--assign=N` 同义） |
| `task <序号> duplicate [-a N]` | 复制一条（字段 + 本地标签 + **同一条 OmniFocus**），可顺手挂到 #N |
| `task <序号> done [分钟]` | 完成：本地完成 + 归档 + Focusmate 勾上 + **OmniFocus 完成并写回截止时间** |
| `task <序号> drop [--local]` | 放弃：本地放弃；默认**OmniFocus 也放弃**，`--local` 只改本地 |
| `task <序号> -m complete`（`--mark complete`） | **手动记账**：本地 completed + 归档；不勾 Focusmate、不当场写 OmniFocus（之后的同步仍会按单向契约写回） |
| `task <序号> -m discard`（`--mark discard`） | **手动记账**：本地 dropped；完全不动 Focusmate / OmniFocus |
| `task -m complete` / `task -m discard` | 不写序号 = 标记当前选中的任务；`-m` 与 `--mark`、后缀位置都等价 |
| `task <序号> link <链接\|PK>` | 把本地任务关联到 OmniFocus（认 Markdown / 尖括号 / 裸链接 / 裸 PK） |
| `task import [粘贴文本]` | **读剪贴板**（不给参数就是它）导入任务并自动关联 OmniFocus；OmniFocus 里**多选 ⌘C** 就是一次导入多条，`-a` / `-f` 等后缀对**整批**生效；如果 OmniFocus 里是重复任务，会先问要不要复制重复设置 |
| `task import`（剪贴板里是**项目**） | 走另一条路：写进**项目库**（`data/projects.json`），任务清单只存不展示；项目里每条未完成任务都必须有「持续时间」，缺一条就**整批拒绝导入**。见 [`project.md`](project.md) |
| `project [序号] [refresh\|remove]` | 看项目库 / 看清单 / 重新读 OmniFocus / 撤掉（见 [`project.md`](project.md)） |
| `plan` | 把这一天剩下的空场切成 count 分给项目，`complete` 之后自动挑任务排进 session（见 [`project.md`](project.md)） |
| `task import -st`（`--single-task`） | 只导入这一条：不查重复设置、不问、不建本地 `repeat` |
| `task <序号> link [链接]` | 不给链接 = 读剪贴板里那条 `omnifocus:///task/PK` |
| `task <序号> -a none`（`-an`） | **下架**：删掉 Focusmate 那条 task + 本地断绑定 |
| `task <序号> -a current`（`-ac`） | 挂到**当前这一场**（进行中优先，其次最近一场） |
| `task 3 swap 5` | 两条任务**互换位置**（一条赋闲也行） |
| `task -s 5 -a 6` | 把 session #5 上的**全部**任务转移到 #6 |
| `task -s 5 -a none` | 把 session #5 上的全部任务下架 |
| `task -s 5` | 只列 #5 上现在挂着谁（只看不动） |
| `refresh --check` 的独占对账 | 一场挤了两条任务（其中有独占的）→ **全部下架** + 警告 |
| `refresh --check`（`-c`） | 重拉 session **+ 对账**：该订没订上、该挂没挂上、一场挤两条（独占）→ 修 |

`-m/--mark` 也是任何 task 动作都能带的后缀，但它和 `done` / `drop` 的分工要说清：

- `done` / `drop` 是**动作**，会按契约去勾 Focusmate、写 OmniFocus；
- `-m complete` / `-m discard` 是**手动记账**，只改本地状态，不碰外部系统；
  `complete` 会进完成归档，`discard` 不会产生任何自动外部写回。

`--assign N` 是**任何** task 子命令都认的后缀（`_split_assign()` 在 `cmd_task()` 入口
统一摘掉），所以这些写法都成立：

```text
task 3 -a 1                     # 最省事：选中面板第 3 条 + 挂到表格 #1
task create title=写论文 --assign 3
task create title=复习 --repeat mon@22:15 --assign 3
task modify 4 due 后天 --assign 3
task select 4 --assign 3        # 等价于 task assign 4 3（也等价于 task 4 -a 3）
```

### 分工：动作归子命令，后缀归分流

`task` 这个入口只有一条规矩，记住了就不会乱：

| 形态 | 谁负责 | 例子 |
|---|---|---|
| **动作**（第一个词） | 子命令自己 | `task import` = 读剪贴板；`task 5 link` = 读剪贴板里的链接 |
| **后缀**（`-a N` / `--assign N` / `-n`） | `cmd_task()` 统一摘掉 | `task import -a 3` = 读完顺手分流/挂到表格 #3 |

所以 `import` / `link` **不需要任何 flag 就已经在读剪贴板了**，`-a` 永远只表示
「分流到哪一场」（`--assign` 的简写），不兼职第二个意思。

### 后缀：`-a` 分流、`-n` 备注、`-s` 批量、`-t` trigger、`-f` formatter

`cmd_task()` 入口依次摘掉后缀（顺序固定：`-a` → `-s` → `-n` → `-e` → `-m` →
`-t` → `-f`），剩下的第一个词才是动作。所以它们能和任何动作组合，也能单独出现
（`-s` 本身就是批量动作）：

| 后缀 | 含义 | 取值 |
|---|---|---|
| `-a` / `--assign` | 分流：把这条任务挂到哪一场 | 表格编号 / `none` / `current` |
| `-s` / `--from` | 批量：**源** session（那一场上的全部任务） | 表格编号 |
| `-n` / `--note` | 顺手写备注（可塞网址） | 自由文本，吃到下一个 `--flag` 为止 |
| `-e` / `--exclusive` | 独占：这一场只能有它一条任务 | `true` / `false`（裸写 `-e` = true） |
| `-m` / `--mark` | 手动改本地状态 | `complete` / `discard` |
| `-t` / `--trigger` | 挂创建触发预设 | 预设名 + `key=value` / 位置参数 |
| `-f` / `--formatter` | 挂排期算法并当场执行 | 预设名 + `key=value` / 位置参数 |

`-a` 的两个特殊取值不是「场次编号」，而是两件事：

- **`none` = 下架**（`_task_detach()`）：先 `delete_task` 删掉 Focusmate 那条 task，
  删成功（或明确报了删不掉）之后才断本地绑定。顺序不能反 —— 本地先断了、Focusmate
  删失败的话，那条 task 就成了没人认领的孤儿，第二天对账还以为它在等你。
  所以「手动下架」是**真的下架**，不是本地假装它没了。
- **`current` = 当前这一场**（`current_session()`）：正在进行的优先；一场都不在进行时
  取**最近一场还没开始的**；都没有就明说、不猜。

#### 紧凑写法：`-an` / `-ac`

**`-an` ≡ `-a none`，`-ac` ≡ `-a current`** —— 两条最常用的分流各有一个紧凑写法。
它们不是在这里硬编码的特例，而是 repl_kit 的通用机制（`repl_kit/flags.py` +
`Command.short`），声明一次就获得三件事：

```python
TASK_SHORT_FLAGS = (
    repl_kit.ShortFlag("-an", ("--assign", "none"), note="从 session 下架"),
    repl_kit.ShortFlag("-ac", ("--assign", "current"), note="挂到当前这一场"),
)
```

- **展开**：handler 之前统一把 `-an` 换成 `--assign none`（交互式 `dispatch` 和
  `--exec` / 测试走的 `handle()` 是同一条路，不存在「界面上能敲、脚本里不认」）；
- **补全**：`task -a<Tab>` 会补出 `-an`/`-ac`；`task -a <Tab>` 补出
  `1..N` + `none` + `current`（`task_completer()`）；
- **提示**：`_hint_task()` 把两个紧凑写法念出来。

同一个字母不兼职两个意思 —— 这是这个入口唯一的规矩（见上面那张表）。

### 位置：下架 / 当前 / 互换 / 全量转移

「位置」只有一种定义：**那一场 session**（或者赋闲）。

**`task 3 swap 5`**（`cmd_task_swap()`）—— 两条任务互换位置（数字是面板序号）：

- 一条在 #1、一条赋闲 → 赋闲那条进 #1，原来那条下来赋闲；
- 两条各在一场 → 场次对调；
- 两条都赋闲 / 都在同一场 → 没有位置可换，明说一句、什么都不改。

Focusmate 没有「把 task 挪到另一场」这个动作（只能建一条新的），所以做法是
**两边各自先下架**（删掉旧场次上那条 task），再挂到对方原来的位置。先下架再加挂
—— 顺序反了会在「加挂失败」时留下两条都挂在同一场。对方原来的位置不在当前表格里
（那天没显示）就说明白，让它赋闲等你切过去挂。

**`task -s 5 -a 6`**（`_apply_bulk()`）—— 一整场转移：

- 每个任务走的都是单条那条路（先下架、再挂），所以转移之后旧场次上不会留下
  永远等不到的幽灵 task；
- 一条失败不挡其余，最后报总账（`N 条 → 目标`，失败的另说）；
- 没有 `-a` 就只列一下（手滑不会掉东西）；`-a none` = 全量下架；
- 源 = 目标 → 什么都不动。

### 导入时认出「这不是任务，是一个项目」

`parse_link()` 认两种链接：`omnifocus:///task/PK` 和 `omnifocus:///project/PK`
（`item["kind"]` 就是 `task` / `project`）。项目**不进任务库**：它的任务清单进
`data/projects.json`（项目库），任务本身要等 `plan` 分了 count 才被「唤起」。
判定只认 primary key 落在 `kind == "project_root"` 那一行上，不拿标题猜。
导入时还会用 `repetition_info` 展开每条任务的重复规则，把「本周（周一起）会
产生几条」一起算进项目账；被推迟到本周开放的任务和重复 occurrence 都算，且
同一条任务可以同时属于这两类。项目任务最多父-子两层：父任务是空壳，排期时
session 里只放子任务（note 标注父任务），子任务不可拆，全部子任务完成为止父任务
自动完成。整条流程、时长校验、子任务和每日对账规则见
[`project.md`](project.md)。

### 导入时检查 OmniFocus 重复任务

OmniFocus 的 `repetitionRule` 不在 `omnifocus_tasks.py list` 的公开字段里，所以
`omnifocus.Client.repetition_info(pk)` 单独用一次 **只读** 的 `evaluate javascript`
查这一条：`ruleString` / `method` / `due` / `planned` / `next` /
`estimated_minutes`。导入流程：

1. 解析剪贴板 / 粘贴文本，拿到 OmniFocus primary key；
2. 每条有 id 的都查一次 `repetition_info`；
3. 有重复规则 → **先不建任务**，把候选挂到 `pending_import`，在 repl_kit 的
   打断栏位问：`[y] 复制重复设置并导入 / [n] 只导入这一次 / [q] 取消`；
4. 只有 `y` 才把规则写成本地 `repeat`；`n` 和普通导入完全一样；
5. 查询失败（OmniFocus 没开 / 没有权限）不挡导入：明确说「没法核对，先按普通
   任务导入」，绝不把查询失败当成重复任务。

复制规则时不再翻译成「每周几」：

- `ruleString` 原样存进 `repeat.rule`，`DAILY` / 多个 `BYDAY` / `INTERVAL` /
  `COUNT` / `UNTIL` 都由 `dateutil.rrule` 按 OmniFocus 的语义展开；
- `start` 优先用 `planned`，其次 `due - estimated_minutes`（warden assign 时把 due
  写成 session 终点），再去那天**真实排期**里核对；都对不上且不在 15 分钟网格上
  才说不能复制；
- `rule_anchor` 保留 OF 的 due/planned 当 DTSTART；跨午夜时用
  `rule_offset_days` 按场次起点去查 RRULE；
- 只有 RRULE 本身解析不了（例如 `BYDAY=XX`）才拒绝复制，仍然可以只导入这一次；
- 消息会显示「每天 10:45（25 min）」「每周一、三 22:15（50 min）」这种摘要，
  同时 `task repeat <序号>` 会打印原 RRULE 和锚点。
- `-st` / `--single-task` 跳过第 2–4 步：只导入这一条，不查询、不询问、不建
  `repeat`。剪贴板里是重复任务、但这次只想留一条时用它。

**导入后缀必须等导入真正完成之后再套**：`task import` 可能先挂起等 `y/n/q`，
这时 `cmd_task()` 不能把 `-a` / `-n` / `-m` / `-t` / `-f` 套到「上一次选中的
任务」上。实现上 `cmd_task_import()` 返回 `IMPORT_PENDING` 哨兵，`cmd_task()`
把这些后缀存进 `pending_import["suffix"]`，等用户回答后由
`_apply_import_suffix()` 按 note → assign → mark → trigger → formatter 的顺序
落到真正导入的任务上。`task import -ac` 因此会挂到今天的 occurrence 子任务
（本体不占 session），而不是把旧任务挂上去。

纯文本粘贴里**没有链接**的多行只当一条任务（`parse_links(..., merge_bare=True)`）：
`语法高频` + `30 个` 是折行，不是两条。剪贴板里有多条链接时按行保留标题，
并按「同一条链接 + 一个标题是另一个的子串」合并，避免把 `语法高频 30 个`
拆成 `30` + 全文两条。

### 一次导入多条

在 OmniFocus 里多选几条任务 ⌘C，剪贴板里就是多条链接 —— `task import` 一次全导入，
`-a` / `-f` 等后缀落到**每一条**上（`task import -f end` = 几条各自排进当天最后
一段，不是只有最后一条进了面板）。`cmd_task_import()` 多条时返回 `ImportBatch`
（`list` 子类，单条时还是那一条任务），`cmd_task` 看见它就按条套一遍后缀。

坑在两份 flavor 会**互相不认**：

- 多选时 `URL` flavor 只剩其中一条；链接全不全看 `HTML` / `RTF`；
- OmniFocus 的中文标题在 RTF 里是 `\uNNNN` 码点（`\uc0\u25968 \u23398 …`），
  **必须解码**。以前把它当控制词删掉，标题就空了；
- 纯文本 flavor 又把那一串标题**原样抄一遍**。

标题空 + 裸标题没去重 = 3 条链接进、6 条任务出（3 条标题是 pk、3 条没关联）。
所以导入分三步，顺序不能换：

1. **补标题/时长**：有 pk 没标题（或标题比 OmniFocus 里的短，即被 flavor 截断）
   → 用 `omnifocus.Client.list_tasks()` 按 pk 查真名，顺手取
   `estimated_minutes` 落成 `fields.duration`（`-f end` 才有东西可排）；
2. **按标题去重**：补完之后，和某条带 id 的条目**同标题**的裸条目一律丢掉
   （`omnifocus.drop_duplicate_titles`）；
3. **按 pk 去重**：这个 pk 已经和本地某条任务关联过 → 跳过并明说「之前导入过
   （任务 #x）」。**只在批量里跳过**：单条导入是「我就要这条」的明确动作
   （先按普通任务导入、再导一次复制成重复任务，是合法用法）。

OmniFocus 读不到（设置里关了 / 没权限 / 没装）不算错误：标题退回剪贴板给的，
时长不补，导入照常 —— 宁可少两个字段，也不让读失败挡住导入。

### 为什么 `task import` 必须自己读剪贴板

在 OmniFocus 里 ⌘C 一条任务、再到终端 ⌘V，**粘出来只有标题**：终端只接受剪贴板里
`string` / `utf8` 那个 flavor，`omnifocus:///task/PK` 在**粘贴那一刻**就丢了。
所以「把粘贴的内容交给 import」这条路注定没有 primary key（只能建任务、不能关联）。
要拿到链接只能绕开粘贴这一层，按 flavor 直接读：

```text
osascript -e 'the clipboard as URL'            → «data url 6F6D…»  ← OmniFocus 链接本体
osascript -e 'the clipboard as «class HTML»'   → «data …»          ← 多选时的全部 + 标题
osascript -e 'the clipboard as «class RTF »'   → «data …»          ← 同上，兜底
pbpaste                                        → 纯文本（常常只有标题）
```

四个 flavor 都读、按 id 合并、标题优先取 HTML 锚文本；只有「一条有 id 没标题 +
一条只有标题」时才把两者配成一条（那是同一次 ⌘C 的两半，不是猜身份）。
`pbpaste -Prefer html` 在这台 macOS 上**完全不起作用**（实测 macOS 15.5，传
html/rtf/url 都原样返回纯文本），所以老实现其实一直在读纯文本 —— 链接从来没读到过。

没给参数就直接读剪贴板；给了参数但粘进来一段**只有标题**的文字时，会去剪贴板对一下
**同名**的那条链接（标题完全相同才认），对不上就建任务并明确说「没有关联」，同时
给出补救办法（`task <序号> link`，同样不用参数）。

**裸序号就是 `select`**（`_dispatch_task()` 里那段 `numeric`）：`task 3` ≡
`task select 3`，所以 `-a` / `-n` 这两个后缀挂在裸序号上也成立。`id:15` 走同一条路，
只是它明说「按内部 id」（面板上没有的任务用）。
裸序号后面**再跟别的东西**（`task 3 5`）不算「选中再挂」—— 那会被判成用法错误，
提示你用 `-a`：挂 session 和挂 task 是两回事，不靠位置猜。

它复用的是 `cmd_task_assign()` 那一条路径（先 Focusmate 复核、成功才落本地），
所以不会出现"本地记了、Focusmate 没有"的半截状态；`delete` / `sync` 这类不产出任务的
动作带 `--assign` 只会得到一句提示，什么都不改。

**已知坑（已修，有回归测试）**：`task assign` 成功之后必须 `rebuild_rows()` ——
表格的 `task` 列是 `rebuild_rows()` 里算的，不重排就一直是旧值，
表现就是「消息区说挂上了，表格 task 列却空着」。

CLI：`python3 task_store.py list|create|delete|modify|show|archive|reconcile`。
`archive` 默认按完成日期分组列出来，`--json` 原样打印整个文件，`--limit N` 只看最近 N 条。

## 重复任务（repeated task）

重复任务拆成**本体（模板）**和**occurrence 子任务**两层，和 OmniFocus 的
「一条重复任务 + 每次 occurrence」对齐：

```text
本体 task#1
  source = {"kind": "omnifocus", "id": "pk..."}   ← 来源指向 OmniFocus
  repeat = 完整的 RRULE + start/duration
  不挂 session → 【契约】表的 task 列永远看不到它

到规则日 → occurrence 子任务 task#2
  repeat_parent = 1
  repeat_date = "2026-11-04"
  source = {"kind": "repeat", "parent": 1, "date": "2026-11-04"}
  fields / tags / omnifocus 从本体继承
  真正去挂 session、显示在契约表里，来源记号 🔁
```

`repeat` 本体：

```json
"repeat": {"rule": "FREQ=DAILY",
           "rule_anchor": "2026-11-04T11:10:00",
           "rule_offset_days": 0,
           "weekday": 2, "weekday_label": "wed",
           "start": "10:45", "duration": 25,
           "source": "omnifocus",
           "summary": "每天 10:45（25 min）",
           "occurrences": {"2026-11-04": {"child_id": 2,
                                          "session": {...},
                                          "focusmate": {...}}}}
```

- `rule` 是 OmniFocus `repetitionRule.ruleString` 原文。`DAILY`、`WEEKLY`、
  多个 `BYDAY`、`INTERVAL`、`MONTHLY`、`YEARLY`、`COUNT` / `UNTIL` 全部由
  `dateutil.rrule` 展开。
- **日期**只看 `repeat_due_on(task, day)`：规则说这天有就有，不再要求当天必须
  有同起点、同时长的 session。「具体什么时候」是生成之后挂哪一场的事。
- 生成子任务时：先找同起点同时长的精确 session；找不到就选当天离
  `repeat.start` 最近的一场；当天没有 session 也先生成子任务（不挂场）。
- `task sync` 会给子任务挂 Focusmate；`repeat.occurrences[日期]` 只记本体的
  账本（`child_id` + session + focusmate），**不会再拿本体去归档一份**。
- 老的手动 `--repeat mon@22:15` 没有 `rule`，仍按 weekday 规则生成子任务；
  `repeat_summary()` 对两种记录都给同一套人话
  （`每天 10:45（25 min）` / `每周一、三 22:15（50 min）`）。
- 契约表 task 列来源记号：
  - `🔁` = 重复任务 occurrence 子任务
  - `🔗` = 有 OmniFocus 关联（复制或继承）
  - `🔁🔗` = 两者都有
  - `⏱` = 挂了 formatter（**不是**「重复任务」的意思）
- 本体只是模板：不进契约任务面板、不进契约表 task 列，因此在契约视图里没有
  面板序号。它在 task 视图里仍可见（number 列写 `id:N`，可以改规则 / 删除 /
  看 RRULE），用 `task id:N ...` / `task repeat id:N` 操作。

### formatter 属于子任务，本体只存模板

`task id:<本体> -f end` **不排本体**：

- 本体只保存去掉运行时账（`reservation` / `result` / `applied_at`）的 formatter
  模板；旧数据里 `due_source=session` 留下的过期 `due` 也会在读入时清掉；
- 依次为今天、明天（最多两个）或 7 天内下一个规则日生成 occurrence 子任务；
- 在每条子任务上真的执行 formatter：子任务带自己的 `formatter.reservation`
  和 session/⏱ 记号；
- 切到 `day +1` 就能在契约表和任务面板看到明天那条已经排好的子任务；
- 以后每天生成子任务时，`ensure_repeat_occurrences()` 会自动继承本体的
  formatter 模板并排当天；改过本体 formatter 后用 `-f` 会强制重排已知子任务。

### 本体不能完成，只能手动结束

重复任务本体不是「一次 session 的活」：

- `task <本体> done`、`task <本体> -m complete` 都会被拒绝；
- 从 Focusmate / OmniFocus 同步完成状态时，本体被排除（子任务才写回）；
- `task <本体> -a` / `-ac` 会转到那一天的子任务（那一天不是规则日就明说）；
- 要结束这套重复：`task repeat <本体> off`（保留任务）或
  `task <本体> drop`（手动放弃，之后不再生成子任务）。

## trigger / formatter（任务扩展点）

任务库只存数据；「什么时候创建任务」和「创建后怎么排期」在
[`task_hooks.py`](../task_hooks.py) 里做成**具名预设函数**：

```text
task <序号> -t <trigger>   [参数...]    # 挂创建触发（默认没有注册任何函数）
task <序号> -f <formatter> [参数...]    # 挂排期算法，并当场执行
task -t / task -f                      # 只看预设列表，不动任务
```

两条规矩：

1. **只认已注册的函数**。命令里存的是
   `{"name": ..., "params": [...], "args": {...}}`；函数不存在就报错并列出
   当前可用名单，绝不悄悄忽略。trigger 注册用
   `task_hooks.register_trigger("name", params=("p1",), help="...")`，
   formatter 用 `register_formatter(...)`。
2. **自动排期只排明天及以后**。`task_hooks.resolve_target_day(..., allow_today=False)`
   会把今天/过去推到明天；用户手敲 `task 3 -f end` 属于手动操作，允许今天。
   这样「昨天（晚上）把明天的任务排好、第二天绝不临时加任务」是一条调用约定，
   而不是散在各处的 `if`。

### `task -f end`

`end` 是唯一内置 formatter。它按任务 `fields.duration`（也认
`duration_minutes` / `estimated_minutes` / `minutes` / `时长` / `预计时长`）排期：

1. 取**当天最后一个还有空位的时段**（这就是 `end`：每天的结束时间不同，
   不写死 22:15）；
2. 在这个时段的**尾巴上切一场任务大小的 session**（不是塞进时段里那场现成的
   session —— 见下面「切割」）；
3. **剪断**那个时段：原来那场 50 分钟从计划里消失（已经订上的还会真的去
   Focusmate 取消），当天只剩「前半截 + 切出来的这一场」，并重算 `data.csv`；
4. 把「任务时长 + `break_minutes`（默认 5 分钟）」写进 `formatter.reservation`；
5. 自动 assign 到那场 session（`assign_task_to`：能建 Focusmate task 就建，
   未来还没订上就只记本地），并把这**条任务自动设成独占**（见下一节）。

#### 切割：25 分钟的任务不该占掉一整场 50 分钟

用户原话：「11:00–12:00 有时间段，那么他应该切割 11:30–12:00 做一个 25 分钟
的时间段，而不是放入 11:00–11:50 的五十分钟时间段。」

用户接着补的第二句是：「切出来的 11:30 那场应该导致原来那场 11:00–11:50 的
**剪断**（实际上就是取消 + 约定一个 25 分钟的）…… 原来的 11:00–11:50 不显示
（因为本来计划就不包含这个），**仅显示新的两场**。」

所以 `end` 的产出是**一场新切出来的 session + 一次排布剪断**：

```
时段       11:00 ───────────────────────────── 12:00
原来那场    [========== 50 分钟 session =====]      ← 11:00–11:50（默认排布）→ 消失
剪断之后    [= 25 分钟 =]        [= 25 分钟 =]│休息│  ← 11:00–11:25（空着）+ 11:30–11:55（这一场）
```

剪断怎么落地（`_cut_period_for_carve` / `_apply_cut_layout`）：

1. `format_end()` 交出 `session.cut_prefix` —— 「整场都在切点之前」的那些场次时长
   （11:00–12:00 的例子里它是 `[25]`：原来那场 50 分钟跨过了切点，不算；再用
   `50 → 25` 的口味把 11:00–11:25 那场补上）；
2. warden 拿 `cut_prefix + [切出来的时长]` 重新打包这个时段；最后一场**正好落在
   切点上**（11:30）就整段写进 layout（`[25, 25]`）—— 这是最常见的情况，**不需要**
   任何一次性 session；
3. 打包落不到切点上（中间隔了 5/20 分钟那种，Focusmate 只认 15 分钟整点）时，
   只把前半截写进 layout，切出来的那场单独作为**一次性 session** 补上；
4. 顺序和 `period T1 25 25` 那条命令完全一样：先 `ks.expect_cancel` 给 daemon
   挂钩单 → **真的取消**已经订上的旧场 → 写 layout → 清掉动过的场次上的任务绑定
   → 重算 `data.csv`。所以「11:00–11:50 已经订在 Focusmate 上」时它会被取消，
   不会跟新的 25 分钟并存。

切法（session 本身的时长/起点）：

- **时长**取 Focusmate 真能订的 `25 / 50 / 75` 里最小能装下任务的那个
  （`task_hooks.fit_session_duration()`；30 分钟任务 → 50 分钟那一档，
  超过 75 就往上取 15 的整数倍）。`fields.duration` 仍然是任务自己的时长，
  session 只是「这一档」；
- **起点往下取整到 15 分钟整点**（`:00/:15/:30/:45`）—— Focusmate 只认这个，
  `sp.add_extra()` 也会拒掉别的时间。所以跨午夜那种 `23:15–00:05` 的时段，
  25 分钟任务切出来是 `23:30–23:55`（占用 `23:30–00:00`），不是 `23:35`；
- 切出来那场**同起点、同时长已经存在** → 复用它，排布一个字都不用改
  （`format_end()` 返回 `new_session=False`，`formatter.result` 里
  `session.existing=true`）。本来就是 `25 + 25` 的排布就落在这条路上；
- 同起点但**时长不一样**（50 分钟的场里切 25 分钟）→ 照样剪断；
- 剪断是**按天**的：写进 `temp/session-layouts.json` 的 `<那天> / <时段>`，
  别的日子不受影响。

同一天**更早的场次**不受影响：任务从那一场的**起点**开始占「时长 + 休息」，
绝不越过它去抢下一场的时间 —— 一场一个任务，不是两条挤一场。之后别的
formatter 通过 `task_hooks.collect_reservations()` 看到这段被占，**不会再用**。

- 没有 `duration` → 明确警告，不落 `formatter`；
- 所有位置都被占 / 装不下 → 明确报错；
- 可选参数：`break_minutes=5`、`date=YYYY-MM-DD|today|tomorrow`。

**跨午夜的时段要按「同一根时间轴」比。** 一天最后那段常常是 `23:15–00:05`
（「看书」那种），时段终点 `00:05` 读成 5 分钟的话，它的「窗口尾」就会比
`22:15` 那一段还小 —— `end` 于是永远排不进真正的最后一段（2026-11-02 的
T6 就是这么被跳过的）。所以区间一律经 `task_hooks._range_of()` 归一：终点
早于起点就 +24h（`23:15–00:05` → `1395–1445`），`24:05` 这种写法也认。
`formatter.reservation` 里的 `start` / `end` 仍然是 `HH:MM`，
`fields.due` 由 `task_store.session_due()` 用「起点 + 时长」算 ——
切出来的是 `23:30–23:55`，所以截止时间是 `23:55`；原来那场
`23:15–00:05` 才会算到第二天 `00:05`。

老记录里 `formatter.reservation` 可能只剩 `{date, start}`（没有 `end`）。
`task_hooks.reservation_range()` 会退回这条任务自己的 session、再不行用
「start + 任务时长」——不然它就不占时间，下一条任务会被排到同一场上。

## 独占（exclusive）：一场 session 只能有一条任务

```text
task 3 -e true          # ≡ task 3 --exclusive true（裸写 -e 也是 true）
task 3 -e false         # 取消独占（这一场又能并别的任务）
task create title=写论文 duration=25 -e true
task create title=写论文 duration=25 -f end     # end 排出来的**自动**独占
```

- 字段就一个：`fields.exclusive`（任务库不校验字段，`-e` 只是写它的短写法）。
  任务面板里独占任务标题前面画 `🔒`；消息里也会说「这一场 session 只能有它
  一条任务」。
- **它占的是整场，不是自己那 25 分钟**：`task_hooks.collect_reservations()`
  会把独占任务的占用跟**它挂着的那一场**求并（`exclusive_block()`），所以
  50 分钟的场里放了一条 25 分钟的独占任务，剩下 25 分钟也不会再排第二条进去。
  只跟自己那一场求并，不蔓延到前后相邻的场次 —— 一场的身份是「哪一天 + 几点
  开始」，15:00 那场和 15:30 那场是两场，各放一条完全合法。
- **三道闸门**（都在 `warden.py`）：

  | 时机 | 行为 |
  |---|---|
  | `-a` / `-ac` 挂载（`_assign_task`） | 目标场里有独占任务 → 抛 `ValueError` 说清是谁挡住、怎么办，**本地账本一个字节都不动** |
  | formatter / 收尾流程挂载（`assign_task_to`） | 同上，但只返回一句警告、不挂（收尾面板不能因为撞车就崩） |
  | `-f end` 排期 | 候选窗口被独占任务盖住 → 换下一个窗口；真撞上 → `HookError`，不写 `formatter` |

- **对账**（`refresh --check` → `check_exclusive_sessions()`）：同一场里挂了两条
  以上未收工的任务、且其中有独占的 → **那一场的任务全部下架**（先删 Focusmate
  那条 task，再断本地绑定）并警告：

  ```text
  ⚠ 2026-11-03 11:00 那场挤了 2 条任务，其中 🔒 独占的只能独占一场 → 全部下架：3「词汇题」、5「语法」
     下架后它们还在任务表里（未挂 session，🟦）。重新排：`task <序号> -f end`，或 `task <序号> -a <session>` 自己挑一场。
  ```

  这条对账放在 `check_bookings` / `check_task_attachments` **之前**：先拆干净，
  否则后面的「重挂」会把它又挂回去。两条都不独占的场次一律不动（并排干活是
  合法的，那是 `exclusive` 的默认值）。

## 截止时间 = 它被分配的那场 session 的终点

`task_store.assign()` 里写死的规则（用户需求「任务被分配的 session 时间自动等于
他的截止时间」）：

```
11:00–11:50 那场 → fields.due = "2026-10-26T11:50:00"、fields.due_source = "session"
```

- **覆盖**原来的 `due`（自动的，不是「没写才写」），原值留在
  `session.previous_due` 里可查。
- 真正「重写到 OmniFocus」发生在**完成**的时候：`task done` / 收尾 D 会跑
  `omnifocus_tasks.py set --id <PK> --complete --due <session 终点> --yes`
  一条命令把「完成 + 截止时间」一起写回去。
- **只要知道了一条任务完成，就一定会写回**：`task done`、收尾 D、`task sync`、
  收尾自动核对，以及**任何一次同步完成状态的 refresh**（`sync_task_completions`
  末尾）。以前 refresh 只把 Focusmate 的 `completedAt` 同步成本地 `✓` 不写
  OmniFocus —— 而本地一 ✓，收尾就再也不会问它了，于是「Focusmate 勾掉了、
  OmniFocus 那条永远没完成」，只能靠人肉 `task sync` 补。
- 写回幂等：`omnifocus.writeback.completed` 记过就不再写第二次。
- OmniFocus 那条如果是**重复任务**，`set --complete` 会在 OmniFocus 里直接生出
  「下一次」（新 primary key，返回在 `repeat_clone_id` 里）。这**不是失败**：
  写回照记、本地还开着的记录（本体 + 之后的子任务 + 复制品）一律改挂新 key
  （`task_store.advance_omnifocus()`，旧 key 留在 `omnifocus.previous` 里）。
  不改挂的话，下一次写回会打在一条已经完成的旧任务上，OmniFocus 永远勾不掉。
- 同一条 OmniFocus 上「本地跟着 OmniFocus 标完成」也有边界：如果这条的完成是
  **我们自己写回**的（`writeback.completed` 有值），就不再拿它去把同一个 key 上
  还开着的兄弟记录带成 ✓（否则明天的子任务会被今天的完成吃掉）。
- 细节见 [`docs/omnifocus.md`](../docs/omnifocus.md)。

## 收尾（postsession）

session 结束 15 秒后 daemon 把 warden 拉到前台，逐个问没做完的任务
（A 延期 / B 放弃 / C 重议 / D 缺漏完成补充 / E 快捷推后）。
本地这边的入口是 `TaskStore.duplicate()` / `drop()` / `complete()` / `archive_local()`，
流程本身在 [`postsession.md`](./postsession.md)。

- `archive_local()` 记的是**手工补录**的完成（`manual: true` + `actual_minutes`），
  幂等键 = `(task_id, status_at, "manual")`。
- `tasks_for_session(day, start, include_done=True)` **默认连完成/放弃的一起返回**：
  「那场到底干成了什么」要看得见，状态由界面打标记，不是靠查询筛掉。

## Focusmate 集成

- 挂任务走 `scripts/bridge.mjs` 的 `add_task`：
  **默认先走内部 API**（`POST /v1/tasks` 带 `attachments:[{type:"session",externalId}]`，
  不行再试 `/tasks/attach`；逻辑在 `scripts/task_attach.mjs`），**DOM 兜底**。
  两条路都必须用 `GET /v1/tasks` 复核「title 一致 + attachment 指向这场」才算成功。
- **为什么不能只靠 DOM**：这一场 session **已经有 task** 时，右侧面板里就不再有
  `Add task` 入口了 —— DOM 路径必然找不到按钮。也就是说「已经有 task 就无法再挂
  一个」不是权限限制，只是 UI 只给一次入口；API 侧 task ↔ session 是 n:n。
- 一场 session **可以挂多个任务**：本地每条任务各存各的 `session`，
  `task_store.tasks_for_session(day, start)` 返回列表；表格 task 列用 ` · `
  把它们串起来显示（`row["tasks"]` 是列表，`row["task"]` 兼容地给第一条）。
- **改挂到别的场次**：本地记录改到新场次，但 Focusmate 上旧那条 task 不会跟着挪
  （API 没有「挪」，我们是又建了一条）。所以 `assign` 会先提醒一句，并把旧 id +
  旧场次记进 `focusmate.previous[]`；`task sync` 对 previous 里的 id 也照样按
  `completedAt` 归档，完成时间不会丢（归档里的 session 用**当时**那一场）。
- Warden 只有看到 `verified=true` 才写 `session` / `focusmate`，避免假成功半截落盘。
- 完成时间来源：`GET /v1/tasks` 的 `completedAt`（见 docs/focusmate-ui-api-notes.md）。
  看不到就不伪造：只匹配、不写完成时间。
- 归档记录包含：归档时间、本地任务字段、对应 session（日期/时间/API session id）、
  Focusmate task id 与 `completedAt`。

## 备注（`notes`）

- **两种写法，任选**：
  - 后缀（推荐，和 `--assign` 一样任何子命令都能带）：
    `task create title=写论文 --note 材料在这 https://example.com/x --assign 3`、
    `task select 1 -n https://notion.so/xxx`。
    备注是自由文本，**一路吃到下一个 `--flag` 为止**，不用加引号；
    `--note=文本` / `--note=`（清空）也行；不产出任务的动作（`delete`/`sync`/
    `archive`…）带 `--note` 只会得到一句"没意义"。
  - 子命令：`task note <id> <文本|网址>` 写、`task note <id>` 清空。
- `task create title=x note=…` / `备注=…` / `modify <id> note …` 都收敛成
  `fields["notes"]`（`FIELD_ALIASES`），免得表格里同时出现 `note`/`notes`/`备注` 三列。
- 读用 `task_store.notes(task)`（`notes` / `note` / `备注` 都认）。
- 挂 Focusmate task 时备注也当 `description` 传过去。
- **为什么重要**：用户会把网址塞进备注，任务完成后照着去对应地方干活。所以
  - 完成的第一时间就在 Recent 里说：`✅ 完成：任务 3「…」· 完成时间 … · 备注：…`；
  - 归档面板（`Task Archive · 完成归档`）第 4 列显示备注，网址用 **OSC 8 超链接**
    （`Warden.note_renderable` → `Style(link=url)`）渲染成可点的链接；
  - 归档 JSON 里额外存一份顶层 `notes`，`task archive` CLI 也会打印「备注：…」。

## 契约视图里的任务展示

- 契约表格 task 列：`2: 写论文 | ✓5: 复习错题` —— 编号 + 裁剪后的标题，用 ` | ` 分隔；
  完成/放弃的前面多一个记号（`✓` / `✗` / `⤼`），**不会被隐藏**。
- 排布改了（`period T1 …`）时任务绑定跟着走：没动的场次保留，动过的清掉
  （见 `session_plan.md`；清掉的是绑定，不是任务）。
- 契约表格下面那块任务面板：`number | task | number | task …`；已 assign 到
  session 的染红，没挂的深蓝（当前选中的再 reverse）。
- **任务属于它被排的那一天**：已挂到明天 session 的任务只出现在 `day +1` 的面板，
  今天的面板里没有它（否则今天的序号会混进明天的活，按序号敲命令就打偏）。

### 面板里的顺序：先按状态分段，段内按位置连号

需求：「每次 refresh 之后重排序，不然看着一大堆 32、15 的 task 混杂；同类任务放一起，
未挂 session 的放最前面」+「放弃的任务和待办任务还是放在一起，没有 grouping」。

注意口径 —— 这是**排序**，不是拆表：面板还是原来那一张表（`number | task | number |
task …`、随屏宽摊 2/3/4 对、`box.SIMPLE_HEAVY`、标题写图例、表尾写账），只是把同类的
任务排到连号的位置上。

`task_panel_order()` 的排序是**两个维度**：

```
Task · 任务（· 进行中 / ✓ 完成 / ✗ 放弃 / ⤼ 转移走；红 = 已挂 session）
┌───────────────────────────────────────────────────────────┐
│ 17  ·写论文      21  ·复习        15  ·化学      44  ·整理  │ ← 进行中：未挂的最前
│ 33  ·物理        45  ·选择题      46  ·rubric    47  ·看rubric│   之后按场次时间
│ 29  ✓flashcards  31  ✓复习错题                              │ ← 完成
│ 28  ✗测试        35  ✗physicFlashcards                      │ ← 放弃
└───────────────────────────────────────────────────────────┘
24 条：6 进行中 · 7 完成 · 11 放弃（收工的不删，只打记号 · 今天 = …）
```

0. **编号就是序号**：面板上写的 `1, 2, 3 …` 是按这个顺序数下来的（连续、不跳号），
   命令里敲的也是它 —— 见上面「序号 = 面板上的第几条」。
1. **状态段**（第一维，`TASK_PANEL_BANDS`）：进行中 → 完成 → 放弃。
   待办归待办、收工归收工，不再出现 `15(待办) / 33(放弃) / 38(放弃)` 这样混着的一行。
2. **位置**（段内）：未挂 session 的最前，之后按场次时间聚成段，同一场那几条挨在一起。
3. **编号**（段内最后）：按任务 ID 升序 —— 同一场上的几条是「一起干」的，编号顺序最好
   认，也不会因为一次 refresh 就换位置。
4. 「面板里有哪几条」仍由 `task_panel_tasks()`（按天取值）决定；排序不改这个口径
   （有回归用例钉着 `sorted(task_panel_order()) == sorted(task_panel_tasks())`）。

**填法是「一行一行」**（左→右、上→下），不是一列一列：按列填的话，四对的宽屏上一行
读过去是 `15 / 39 / 42 / 28`，看着就是「序号很乱」；按行填之后一行里读过去是同一段里的
连号（有回归用例把面板画成文本，按行读出来的编号必须等于 `task_panel_order()`）。

## 归档文件形状（`data/task-archive.json`）

一个 JSON 文件、只增不改、一条完成记录一个 object：

```json
{
  "version": 1,
  "records": [{
    "archived_at": "2026-10-27T08:19:02",
    "task_id": 1,
    "fields": {"title": "写论文", "due": "周二", "notes": "先写 intro"},
    "notes": "先写 intro",
    "session": {"index": 3, "date": "2026-10-26", "start": "11:15",
                "end": "12:05", "duration": 50, "focusmate_session_id": "9f2c…-sess"},
    "focusmate": {"task_id": "936bd726-task", "title": "写论文",
                  "description": null,
                  "completed_at": "2026-10-26T04:20:00.000Z",
                  "attachments": []}
  }]
}
```

- 幂等键 = `(task_id, focusmate.task_id, focusmate.completed_at)`：同一条不会再写第二次
  （`reconcile` 反复跑不会让文件变长）。
- 顶层 `notes` 是备注的冗余副本（`fields.notes` 里也有）：归档是**快照**，
  以后任务被改/删了，完成时那条备注仍然留在这里。
- `focusmate.completed_at` 是 Focusmate 的原样字符串（UTC），**不猜、不补**；
  没有它就只算"匹配到"、不归档。
- 每周重复任务的每一次落点单独一条（`session` 是那一周的 session）。
- `data/tasks.json` / `data/settings.json` 是本机运行时状态（已 gitignore）；
  `data/task-archive.json` 不忽略 —— 那是记录，可以进 git。

## 边界

- 本模块不碰 `data.csv`、不碰日历、不联网。
- 一个 session 当前只显示一个任务；多任务的多行摘要只来自任务自身的多个字段
  （`1. ...  2. ...`），空白压成一行，UI 用 no_wrap + crop。
