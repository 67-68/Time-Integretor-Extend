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
| `task assign [<任务ID>] <session ID>` | 先挂 Focusmate，成功后落本地 |
| `task sync` | `GET /v1/tasks` → 读 `completedAt` → 写归档 |

CLI：`python3 task_store.py list|create|delete|modify|show|reconcile`。

## Focusmate 集成

- 挂任务走 `scripts/bridge.mjs` 的 `add_task`（DOM：点 session 卡片 → Add task →
  填 title/notes → Create task），**API 复核 attachment** 成功后才返回 `verified`。
- Warden 只有看到 `verified=true` 才写 `session` / `focusmate`，避免假成功半截落盘。
- 完成时间来源：`GET /v1/tasks` 的 `completedAt`（见 docs/focusmate-ui-api-notes.md）。
  看不到就不伪造：只匹配、不写完成时间。
- 归档记录包含：归档时间、本地任务字段、对应 session（日期/时间/API session id）、
  Focusmate task id 与 `completedAt`。

## 边界

- 本模块不碰 `data.csv`、不碰日历、不联网。
- 一个 session 当前只显示一个任务；多任务的多行摘要只来自任务自身的多个字段
  （`1. ...  2. ...`），空白压成一行，UI 用 no_wrap + crop。
