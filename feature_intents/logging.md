# logging

## 模块职责

运行日志只有**一套**：`runtime_log.py`。它用**标准库 logging** 的等级体系，
自己挂一个 handler 把记录写成 **JSONL，按本地日期切文件**：

```text
temp/logs/YYYY-MM-DD.jsonl
```

一行一个 JSON object，字段固定以 `ts` / `level` / `area` / `event` 开头，
后面是这次事件的业务字段：

```json
{"ts":"2026-11-17T10:57:48.797","level":"INFO","area":"layout","event":"commit_done","day":"2026-11-17","period":"T1","sessions":2,"spans":["13:00-14:15","14:30-15:20"]}
```

`level` 是 `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`。
日志是**旁路**：目录写不进去只会静默失败，绝不影响排期 / 订场 / 写回。

## 接口

| 用法 | 说明 |
|---|---|
| `rl.log_event(area, event, level="info", **fields)` | 写一条。`level` 认 logging 常量或字符串，默认 INFO。 |
| `rl.log_change(area, event, level="info", key=None, volatile=(), heartbeat=0, **fields)` | **只在内容变化时写**：字段全一样就不写，变化时带上 `changed=true` 和 `skipped_same=N`（中间有 N 轮结果一样）。`volatile=("elapsed_ms",)` 里的字段只写不参与比较。`heartbeat=N` 秒可以强制每隔一阵写一条心跳（默认 0 = 纯变化触发）。 |
| `rl.set_level("debug")` | 运行期改级别。 |
| 环境变量 `TI_LOG_LEVEL` | 默认 `INFO`。排障时 `TI_LOG_LEVEL=debug` 复现一次，就能看到拉取开始/结束这类流程事件。 |
| 环境变量 `TI_LOG_DIR` | 改日志目录（Node 桥、keep_session daemon、Python 侧共用这一份约定）。 |
| 环境变量 `TI_LOG_FSYNC=0` | 关掉每行 fsync（默认开；关掉更快，断电可能丢最后几行）。 |
| 环境变量 `TI_LOG_KEEP_DAYS` | 日志留几天，默认 **14**；`0` = 不清理。每次进程启动（`setup()`）清一次，只删能解析成日期的 `YYYY-MM-DD.jsonl`，删失败静默。 |

## 等级约定（写新日志时照这个来）

| 级别 | 放什么 | 例子 |
|---|---|---|
| `DEBUG` | 纯流程 / 高频心跳：开始了、拉了一页、跳过、返回值没变 | `focusmate.task_pull_start`、`omnifocus.status_pull_start`、`plan.start`、`refresh.start`、`book.start`、`layout.commit_start` |
| `INFO` | 用户会关心的**状态变化**：排好了、订上了、写回了、a 掉了、确认了 | `plan.apply_done`、`layout.commit_done`、`book.booked`、`task.done`、`omnifocus.writeback_flush`、`session.abandoned` |
| `WARNING` | 做了但结果不理想 / 需要人看一眼：没排进去、没订上、被拒绝、推送失败 | `plan.layout_failed`、`book.failed`、`layout.reject`、`layout.lock_timeout`、`keytime.push_failed`、`omnifocus.complete_blocked` |
| `ERROR` | 外部动作失败 / 数据不一致 / 抛异常那条路 | `*.error`、`layout.cancel_failed`、`net.action_failed`、`plan.layout_error` |

规矩：**高频轮询用 `log_change`，不要用 `log_event`**。以前
`task_pull_result` / `writeback_scan_result` 每轮都写一遍，56,208 行里 80% 是
这两类快照；现在它们只在结果变化时写一条。

## 事件清单（按 area）

| area | 事件 |
|---|---|
| `warden` | `start`（日期 / 离线 / 脚本模式）、`stop` |
| `plan` | `start`(DEBUG) → `open`（分到几个 count）→ `scheduled`(DEBUG) → `apply_done`（条数 / count / 没排进去的）；`no_slots`(WARNING)、`layout_failed` / `layout_error` |
| `layout` | `commit_start`(DEBUG)、`commit_done`（时段 / 来源 / 场次 / 取消了几场）、`reject`（放不下，零副作用）、`lock_timeout`、`cancel_failed`(ERROR) |
| `book` | `start`(DEBUG)、`triggered` / `done`、`failed`(ERROR)、`suppressed`(DEBUG)；daemon 侧：`attempt`、`booked`（session id）、`failed`(WARNING，带 error_code / infra 标记) |
| `refresh` | `start`(DEBUG)、`done` |
| `session` | `abandoned`（取消单场）、`cancelled_permanent`（从日历删掉） |
| `task` | `done`（实际分钟 / 为什么 / 有没有勾 Focusmate / 有没有写回 OmniFocus）、`drop` |
| `carve` | `cut` / `no_period`(DEBUG)：`end` 切场时把哪个时段剪成什么 |
| `daemon` | `round_start` / `round_done`(DEBUG)：keep_session 每一轮 |
| `keytime` | `announce`(DEBUG)、`ack`、`focus_lock` / `focus_lock_failed`、`focus_release`、`notify_failed`(WARNING)、`push_failed`(WARNING)、`rules_error` / `keytimes_error` |
| `calendar` | `populate`（日历 → data.csv）、`layout`（`layout T1 50 50`） |
| `net` | `action_failed`(ERROR)：没有专属失败钩子的网络动作 |
| `focusmate` | `task_pull_start`(DEBUG) / `task_pull_result`（变化才写）/ `task_pull_error`(ERROR)、`complete_*`、`tasks_pull_*` |
| `omnifocus` | `status_pull_*`、`writeback_scan_*`(DEBUG)、`writeback_flush`（写了几条 / 失败几条）、`complete_*`、`drop_*`、`import_batch` |
| `derived` | `create` / `copy` / `partial` / `mark` / `finish` |

## 落地记录（2026-11-17）

- 之前：56,208 行 / 47MB / 19 天，`omnifocus.writeback_scan_result` 15,336 行 +
  `focusmate.task_pull_result` 16,691 行占了 80%+，全是「和上一轮一模一样」的快照。
- 现在：`*_start` 类降到 DEBUG（默认不写），`*_result` 改成 `log_change`（变化才写），
  重要节点补 INFO/WARNING，daemon 的下单结果进了同一份 JSONL（一条时间线：
  `plan` 切了什么、`book` 谁去订的、`daemon` 订成了没有）。
- 保留期：`prune()` + `TI_LOG_KEEP_DAYS`（默认 14 天，`0` 关掉）；进程启动时清，
  只认 `YYYY-MM-DD.jsonl`，`notes.txt` 之类和解析不出日期的文件一律不碰。
- 测试：所有构造 `Warden` 的测试文件都设了 `TI_LOG_DIR=<TMP>/logs`，跑测试不再
  往真日志里灌假事件；`temp/logs` 的 2026-11-17 里留着这次验证跑出来的约 700 行
  测试事件（时间 10:5x），之后的运行是干净的。
