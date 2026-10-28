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
    "fields": {"title": "写论文", "due": "2026-10-26T11:50:00", "due_source": "session"},
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
    "history": [                         // 哪一天的哪一场「处理」过它（见下）
      {"day": "2026-10-27", "start": "11:00", "action": "skip", "at": "..."}
    ],
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
  契约视图下面那块**任务面板按天取值**就靠它：今天 = 没做完的 + 今天处理过的，
  过去那天固化 = 只剩当时处理过的。读写入口是 `mark_processed()` / `processed_on()`；
  老数据没有痕迹时 `processed_on()` 用 `status_at`（或 `focusmate.completed_at`）
  兜底，否则升级当天「过去几天」会整片空白。

## 命令

TUI（任务视图 / 任意视图都认 `task`）：

| 命令 | 作用 |
|---|---|
| `task create [id] [key=value ...]` | 创建；给了数字 id 就用它，否则自动编号。没有 `=` 的 token 拼成 `title` |
| `task delete <id>` | 删除 |
| `task modify <id> <field> <新值>` | 改任意字段 |
| `task select <id>` | 选中任务（`task assign <session>` 的默认目标） |
| `task <id>` | ← 和 `task select <id>` **完全等价**（裸 ID 就是 select） |
| `task assign [<任务ID>] <session ID>` | 先挂 Focusmate，成功后落本地；成功即刻重排表格（task 列马上有内容） |
| `task repeat <id> <周几@HH:MM\|off>` | 设置 / 取消每周重复 |
| `task create ... --repeat 周几@HH:MM` | 一步建「每周重复任务」 |
| `task sync` | `GET /v1/tasks` → 读 `completedAt` → 写归档（顺带把重复任务挂到 session） |
| `task archive [N]` | 看归档：按完成日期分组、最近 N 条（默认 5） |
| `task <任何动作> ... --assign <N>` | 跑完顺手把产出的任务挂到表格 #N（`-a` / `--assign=N` 同义） |
| `task <id> duplicate [-a N]` | 复制一条（字段 + 本地标签 + **同一条 OmniFocus**），可顺手挂到 #N |
| `task <id> done [分钟]` | 完成：本地完成 + 归档 + Focusmate 勾上 + **OmniFocus 完成并写回截止时间** |
| `task <id> drop [--local]` | 放弃：本地放弃；默认**OmniFocus 也放弃**，`--local` 只改本地 |
| `task <id> link <链接\|PK>` | 把本地任务关联到 OmniFocus（认 Markdown / 尖括号 / 裸链接 / 裸 PK） |
| `task import [粘贴文本]` | **读剪贴板**（不给参数就是它）导入任务并自动关联 OmniFocus，可一次多行 |
| `task <id> link [链接]` | 不给链接 = 读剪贴板里那条 `omnifocus:///task/PK` |

`--assign N` 是**任何** task 子命令都认的后缀（`_split_assign()` 在 `cmd_task()` 入口
统一摘掉），所以这些写法都成立：

```text
task 8 -a 1                     # 最省事：选中任务 8 + 挂到表格 #1
task create title=写论文 --assign 3
task create title=复习 --repeat mon@22:15 --assign 3
task modify 1 due 后天 --assign 3
task select 1 --assign 3        # 等价于 task assign 1 3（也等价于 task 1 -a 3）
```

### 分工：动作归子命令，后缀归分流

`task` 这个入口只有一条规矩，记住了就不会乱：

| 形态 | 谁负责 | 例子 |
|---|---|---|
| **动作**（第一个词） | 子命令自己 | `task import` = 读剪贴板；`task 5 link` = 读剪贴板里的链接 |
| **后缀**（`-a N` / `--assign N` / `-n`） | `cmd_task()` 统一摘掉 | `task import -a 3` = 读完顺手分流/挂到表格 #3 |

所以 `import` / `link` **不需要任何 flag 就已经在读剪贴板了**，`-a` 永远只表示
「分流到哪一场」（`--assign` 的简写），不兼职第二个意思。

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
给出补救办法（`task <id> link`，同样不用参数）。

**裸 ID 就是 `select`**（`_dispatch_task()` 里 `action.isdigit()` 那一段）：
`task 8` ≡ `task select 8`，所以 `-a` / `-n` 这两个后缀挂在裸 ID 上也成立。
裸 ID 后面**再跟别的东西**（`task 8 3`）不算「选中再挂」—— 那会被判成用法错误，
提示你用 `-a`：挂 session 和挂 task 是两回事，不靠位置猜。

它复用的是 `cmd_task_assign()` 那一条路径（先 Focusmate 复核、成功才落本地），
所以不会出现"本地记了、Focusmate 没有"的半截状态；`delete` / `sync` 这类不产出任务的
动作带 `--assign` 只会得到一句提示，什么都不改。

**已知坑（已修，有回归测试）**：`task assign` 成功之后必须 `rebuild_rows()` ——
表格的 `task` 列是 `rebuild_rows()` 里算的，不重排就一直是旧值，
表现就是「消息区说挂上了，表格 task 列却空着」。

CLI：`python3 task_store.py list|create|delete|modify|show|archive|reconcile`。
`archive` 默认按完成日期分组列出来，`--json` 原样打印整个文件，`--limit N` 只看最近 N 条。

## 每周重复任务（repeated task）

抽象出来的一层：任务可以带一个 `repeat`，描述「周几 + 起点 + 锚定时长」。

```json
"repeat": {"weekday": 0, "weekday_label": "mon", "start": "22:15",
           "duration": 50, "created_at": "...",
           "occurrences": {"2026-10-26": {"session": {...}, "focusmate": {...}}}}
```

- **只支持每周重复**：设在周一 22:15，就只影响周一那一场；周二周三不受影响。
- 落点判据是「同一星期几 + 同一时刻 + 同一时长」：
  `task_store.repeat_occurrence(task, day, sessions)`。
- 所以**排布被改了，这一周就不落**：50 拆成 25+25 之后，两场 session 的时长都不是
  50，重复任务在这一周消失 —— 但 `repeat` 设置本身一个字不改，下一周照旧。
- `task sync` 会把落上的那一场挂到 Focusmate（`add_task` 复核成功才写本地），
  并把 `occurrences[日期]` 记下来；完成时间同样从 `completedAt` 归档。
- 界面上这一行的 task 列会带 `🔁` 前缀（说明它是重复任务自动挂上的，不是手 assign 的）。

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
- 写回幂等：`omnifocus.writeback.completed` 记过就不再写第二次。
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
- 契约表格下面那块两列任务表格：`number | task | number | task`；已 assign 到
  session 的染红，没挂的深蓝（当前选中的再 reverse）。

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
