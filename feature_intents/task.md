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
    "fields": {"title": "写论文", "due": "明天", "任意字段": "任意值"},
    "created_at": "...",
    "updated_at": "...",
    "session": {"date": "2026-10-26", "start": "11:00", "end": "11:50",
                "focusmate_session_id": "..."},
    "focusmate": {"task_id": "...", "session_id": "...", "completed_at": "..."}
  }]
}
```

`fields` 是用户字段的容器，所以字段名不会和内部簿记冲突。

## 命令

TUI（任务视图 / 任意视图都认 `task`）：

| 命令 | 作用 |
|---|---|
| `task create [id] [key=value ...]` | 创建；给了数字 id 就用它，否则自动编号。没有 `=` 的 token 拼成 `title` |
| `task delete <id>` | 删除 |
| `task modify <id> <field> <新值>` | 改任意字段 |
| `task select <id>` | 选中任务（`task assign <session>` 的默认目标） |
| `task assign [<任务ID>] <session ID>` | 先挂 Focusmate，成功后落本地；成功即刻重排表格（task 列马上有内容） |
| `task repeat <id> <周几@HH:MM\|off>` | 设置 / 取消每周重复 |
| `task create ... --repeat 周几@HH:MM` | 一步建「每周重复任务」 |
| `task sync` | `GET /v1/tasks` → 读 `completedAt` → 写归档（顺带把重复任务挂到 session） |
| `task archive [N]` | 看归档：按完成日期分组、最近 N 条（默认 5） |
| `task <任何动作> ... --assign <N>` | 跑完顺手把产出的任务挂到表格 #N（`-a` / `--assign=N` 同义） |

`--assign N` 是**任何** task 子命令都认的后缀（`_split_assign()` 在 `cmd_task()` 入口
统一摘掉），所以这些写法都成立：

```text
task create title=写论文 --assign 3
task create title=复习 --repeat mon@22:15 --assign 3
task modify 1 due 后天 --assign 3
task select 1 --assign 3        # 等价于 task assign 1 3
```

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

## Focusmate 集成

- 挂任务走 `scripts/bridge.mjs` 的 `add_task`（DOM：点 session 卡片 → Add task →
  填 title/notes → Create task），**API 复核 attachment** 成功后才返回 `verified`。
- Warden 只有看到 `verified=true` 才写 `session` / `focusmate`，避免假成功半截落盘。
- 完成时间来源：`GET /v1/tasks` 的 `completedAt`（见 docs/focusmate-ui-api-notes.md）。
  看不到就不伪造：只匹配、不写完成时间。
- 归档记录包含：归档时间、本地任务字段、对应 session（日期/时间/API session id）、
  Focusmate task id 与 `completedAt`。

## 归档文件形状（`data/task-archive.json`）

一个 JSON 文件、只增不改、一条完成记录一个 object：

```json
{
  "version": 1,
  "records": [{
    "archived_at": "2026-10-27T08:19:02",
    "task_id": 1,
    "fields": {"title": "写论文", "due": "周二", "notes": "先写 intro"},
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
- `focusmate.completed_at` 是 Focusmate 的原样字符串（UTC），**不猜、不补**；
  没有它就只算"匹配到"、不归档。
- 每周重复任务的每一次落点单独一条（`session` 是那一周的 session）。
- `data/tasks.json` / `data/settings.json` 是本机运行时状态（已 gitignore）；
  `data/task-archive.json` 不忽略 —— 那是记录，可以进 git。

## 边界

- 本模块不碰 `data.csv`、不碰日历、不联网。
- 一个 session 当前只显示一个任务；多任务的多行摘要只来自任务自身的多个字段
  （`1. ...  2. ...`），空白压成一行，UI 用 no_wrap + crop。
