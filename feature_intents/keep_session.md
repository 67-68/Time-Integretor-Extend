# keep_session

## 模块职责

后台常驻，保证「今天该有的 Focusmate session」都订上。`data.csv` 是唯一目标来源。

## 架构：daemon + 只读前台

```
keep_session daemon（后台常驻，脱离终端，PID 文件）
  每 300s 一轮：读 data.csv → 一次 API 列今天 session → 补齐缺失的
                → 写 state.json + 日志
        ▲ 读 state.json / 日志
        │
keep_session status / （未来 TUI，只读）
```

- daemon 用双重 fork + setsid + setpgid 脱离终端，关终端不影响；`daemon stop` 按
  进程组 SIGTERM（升级 SIGKILL），避免 node/Chromium 子进程残留。
- daemon 不依赖 launchd/cron；进程内自己 sleep 循环。
- 状态全部落盘，前台 TUI 只需读文件，不依赖 daemon 生命周期。

## 目标来源：data.csv

每行 `HH:MM,duration`，`#` 注释与空行忽略，duration ∈ {25,50,75}。
语义**仅今天**：state 带 date，跨天自动重置，不做跨天补订。

## 检查与补订（每轮）

1. 一次 `GET /v1/sessions` 拿到今天全部 session（只读、便宜）。
2. 遍历今天**还没开始**（now < T）的 slot：
   - API 有 → `booked`（若之前是别的状态，纠正过来）。
   - API 没有：
     - 若 state 以为是 `booked` → 判定为**手动取消**，回退重订，
       并**重置尝试计数**（取消不是脚本的失败）。
     - 若在退避期 → 等到点。
     - 若已达上限 → `failed` + 通知，当天不再碰。
     - 否则 → 下单。

「提前订满」：不看「是否临近开始」，只看「还没开始且缺失」就订。

## 预定结果以 API 为准（关键）

`focusmate-mcp` 的 `book_session` 抓不到真实 sessionId 时会伪造
`temp-<毫秒时间戳>` 并照样返回 `success: true`，造成"假成功"。因此本模块
**不信任 `payload.success`**：报成功后会用官方 API 复核该 slot 是否真的出现
（最多 3 次、间隔 4s，容忍落库延迟），复核不过就按失败走退避重试。

MCP 侧也已同步修复（`patches/focusmate-mcp.patch`）：不再伪造 id，改为返回
`verified: boolean` 表示"是否拿到了证据"。但**复核的责任在上层**——API 才是
ground truth，MCP 的自述永远只是线索。

## 退避与防抖

- 失败最多 3 次；第 1 次失败等 5min，第 2 次等 15min，第 3 次转 `failed` + 通知。
- 每次真正下单前随机 sleep 5±3 秒，避免规律性机器行为触发风控。
- 检查（列 session）每轮都做，便宜；下单（起浏览器）贵且敏感，才需要退避限流。

## 状态文件

`temp/keep_session-state.json`：
```json
{"date":"2026-10-06",
 "api_snapshot":{"at":"...","sessions":["..."]},
 "slots":{"16:30":{"status":"booked|failed|null|missed","attempts":1,
                   "next_retry_at":"...","last_error":"...","duration":"50","past":true}}}
```
- `status=null` 待订/退避中；`booked`/`failed` 为终态（failed 当天不再动）。
- `status=missed` 只用于**已经过去**的 slot：确认今天没有这场 session，
  已无法补订，但仍显式记录，避免 status 静默不显示让人误判。
- `past=true` 标记该行对应的 slot 今天已经过去。
- `api_snapshot` 是每轮 API 结果的落盘快照，供 status 展示"今天实际有哪些"。

## 并发

`temp/keep_session.lock`（fcntl.flock）：daemon 与手动 `check` 互斥。拿不到锁
直接跳过本轮（非阻塞），因为下单要几十秒，阻塞等待只会让手动 check 卡住。

## 日志

daemon 的**父进程**（你直接运行 `daemon start` 的那个）只把启动确认打到
stdout，不写共享日志——否则会和已经在跑第一轮的孙进程抢同一个文件、导致行序错乱。

## 入口

- `daemon start|stop|restart|status`：后台常驻管理。
- `run`：前台常驻循环，Ctrl+C 退出（调试）。
- `check [--dry-run]`：只跑一轮。
- `status`：daemon + data.csv + state + 日志尾总览。

## 环境约束

daemon 脱离终端后 PATH 极简，node 用 `/opt/homebrew/bin`，python 用系统
`/usr/bin/python3`，`FOCUSMATE_MCP_JS` 显式注入。预留 `temp/keep_session.stop`
文件可作为优雅停止开关。
