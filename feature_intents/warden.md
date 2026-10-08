# warden

## 模块职责

一个 rich TUI（`warden.py`），把散落的三块东西收进一个界面：daemon 状态、今天的
session 列表、以及对单个 session 的写操作（静音 / 标题 / 取消）。

定位是**看板 + 审讯台**，不是自动化中枢：它不替代 daemon，也不做定时。
daemon 负责「保证订上」，warden 负责「随时看得见、随时能改」。

## 架构

```
  warden.py (rich TUI)
     │
     │ 1) 只读文件：temp/keep_session-{state.json,pid,log}
     │    → daemon Running/Stopped、今日计划落地情况
     │
     │ 2) 子进程：python3 keep_session.py daemon start|stop
     │    → 启停走现成 CLI，不在自己进程里 fork
     │
     └──3) JSON 行（stdin/stdout）──> scripts/bridge.mjs
                                        └── Playwright ──> 常驻 Chromium
                                            └── 内部 API（PUT/DELETE，裸 Firebase token）
```

为什么要有 bridge：`scripts/*.mjs` 那四个 CLI 每次调用都要冷启动一个 Chromium。
实测冷启动 ~9.7s，接上 browser 再发命令 ~1.9s。TUI 里连着敲
`mute 3` / `title 2 "..."`，冷启动那种节奏没法用，所以把浏览器留在内存里。

为什么是**常驻**而不是「每条命令起一个」：见上面那组数字。代价是要管子进程
生命周期（见「踩过的坑」）。

## 四个概念（以及被砍掉的那个）

项目原本提了五个概念，其中一个按用户要求**不实现**：

| 概念 | 实现 |
|---|---|
| The Warden Status | 看板头部：daemon Running/Stopped + pid + 日志尾；`start` / `stop` |
| The Contract Table | `rich.table`：ID / 时间 / 时长 / 状态 / 静音 / 伙伴 / 标题 |
| The Interrogation Prompt | 死循环 `input()`，极简命令（见下） |
| ~~Panic Action~~ | **已砍**。"一键强行触发倒计时"是个错误设计 —— 它会把 daemon 的节奏和日历契约一起绕过，且和 `cancel` 的阻力设计自相矛盾。 |

## 命令

```
mute 3              把 #3 静音（不写 on/off 就是取反）
mute 3 on|off       显式开/关
title 2 "Math Past Paper"
title 2 ""          清空标题
cancel 1            → 地狱级阻力流程（见下）
refresh / r         重新拉列表（ID 会重编号）
start               启动 daemon（先让 bridge 放开浏览器）
stop                停用 daemon（要密码）
status / s          重画看板
set-password        改停用密码
help / quit
```

**ID 是「当前这张表里的行号」**，按时间排序，每次 refresh 重新编号。所以
命令里的 N 只对「你现在看到的这张表」有效 —— 表在屏幕上是显式的，看完再敲。

`mute` / `title` / `cancel` 都走 `startMs` + `meetingId` **双重校验**：先按
`startMs` 找到那一场，再核对 `meetingId` 是否一致，不一致直接中止、不做任何写。
这是防「ID 漂移」和「表过期」的闸门。

## 停用 daemon 的密码

用户要求「停用必须输入复杂密码」。设计：

- 首次 `stop` 时引导设置；之后每次 `stop` 都要验。
- 规则：**≥12 字符，且 小写/大写/数字/符号 至少占 3 类**。
- 存储：PBKDF2-HMAC-SHA256（26 万次迭代）+ 16 字节随机盐，落
  `temp/warden.json`，文件权限 `0600`。**明文不落盘。**
  `temp/` 已在 `.gitignore`，所以密码哈希不进 git。
- 最多试 3 次，每次失败间隔 2s。失败**绝不**调用 stop。
- 忘了密码：删掉 `temp/warden.json` 重设即可。这是防**误触**的摩擦，不是防外人
  —— 能碰到这个仓库的人本来就能删文件。

## cancel 的「地狱级阻力」

`cancel` 是全套里唯一不可逆的操作，且**走内部 API 会绕过网页那层二次确认**，
所以阻力全部加在 TUI 侧：

1. 先打一整块红色面板：目标场次、标题、**是否已匹配 partner**。
2. 如果已匹配，明确写出后果（对方被放鸽子 / 你 attRate 会掉）。
3. 要求**手打该场次的开始时间**（如 `19:00`）才能继续 —— 任何其它输入都放弃。
   这一步的意义是逼你确认「我要取消的到底是哪一场」，而不是无脑敲 y。
4. **6 秒冷静期**倒计时，期间 Ctrl+C 随时可中止。
5. 才真的发 `DELETE`。

已结束的场次直接拒绝（Focusmate 也不允许）。

## 和 daemon 抢 Chromium profile

daemon 下单（focusmate-mcp）和 bridge 用的是**同一个**持久化 profile
（`~/.focusmate-mcp/browser-data`）。同时开着 = 抢锁 = daemon 那一轮下单失败。
处理：

- bridge **空闲 120s 自动放开浏览器**（进程留着），下次命令再冷启动。
  可用 `WARDEN_BRIDGE_IDLE_MS` 调（`0` = 不放开）。
- `start` 之前，TUI 主动让 bridge `release`（收盘浏览器但保留进程），
  然后再拉 daemon，避免刚启动就撞锁。
- 默认**不自动刷新**表格，也是为了缩短占锁时间：刷新按需（启动时、每条写命令后、
  手敲 `refresh`）。

## bridge 协议

```
stdin   每行： {"id":1,"cmd":"snapshot"}
stdout  每行： {"id":1,"ok":true,"cmd":"snapshot","ms":842,"result":{...}}
        事件帧： {"event":"ready|idle-closed|fatal|protocol-error"}
```

命令：`ping` / `snapshot [date]` / `title` / `mute` / `cancel` / `release` / `shutdown`。

关键约束：

- **stdout 被协议独占**。`_lib.mjs` 的 `log()` 会往 stdout 写，所以 bridge 一起来
  就把 `process.stdout.write` 改道 stderr，只留 `send()` 能写真 stdout。
- **命令串行执行**（promise 队列）。浏览器/profile 都不能并发碰。
- **`id` 只做请求号**，session 身份一律叫 `meetingId`。见「踩过的坑」。
- `_lib.mjs` 的 `die()` 会 `process.exit` —— 那是 CLI 脚本的语义，对常驻进程是灾难
  （一条命令失败不该带走整个桥）。用 `inCommand` 标志把命令执行期间的
  `process.exit` 转成异常。

## 踩过的坑（都有回归测试）

1. **请求号和 session 身份撞名**（最险的一个）。
   最初 bridge 的三个写命令用 `id` 表示「哪一场」，而信封的请求号也叫 `id`。
   Python 侧 `req.update(kw)` 会把请求号覆盖成 UUID。后果**不是报错而是卡死**：
   桥照常执行、响应也发了，但调用方永远等不到自己那个请求号 ——
   表现为「超时」，而**写入其实已经生效**。第一次实测就踩了：
   `mute` 把 quiet 改了、客户端挂了 240s，人还以为失败了。
   现在：参数叫 `meetingId`，且 `Bridge.call()` 见到 `id` 直接抛错，不给下次机会。
2. **stdin 关闭和正在执行的命令是并发的**。早期版本 `rl.on("close")` 直接
   `process.exit`，把跑到一半的命令连同浏览器一起带走（丢响应）。
   现在先 `await queue.p` 把队列排干再退。
3. **刷新失败不要覆盖好数据**。`refresh()` 出错时如果拿 state.json 退化快照去覆盖
   已有行，那些行没有 session id，后续 mute/title/cancel 全都做不了。
   现在：已有好数据就保留，只报错。
4. **state.json 的快照含跨天数据**。daemon 用 UTC 日界查，会带上本地上一天的
   23:15。退化快照必须按本地日期再滤一遍，否则表里混进昨天的场次。
5. **rich 的 `Text()` 不解析 markup**。`Text("[bold]▶[/]")` 会把方括号原样打出来。
   要么用 `console.print`（解析 markup），要么用 `Text.append(style=)`。
6. `Table.grid_from_rows` 不存在；`[[...]]` 这种转义在长行里容易渲染成 `[]`。
   少用转义，直接换措辞。

## 依赖

`rich` 装在 `temp/pylibs`（和 `download_calendar.py` 同一个路子，不污染系统）：

```
python3 warden.py install-deps
```

`temp/` 在 `.gitignore` 里，所以换机器要重跑一次 `install-deps`。

## 未验证 / 待办

- **`stop` 的 `keep_session.py daemon stop` 子进程路径没有在真 daemon 上跑过**
  （怕把正在跑的 daemon 停掉影响今天的场次）。`start` / 密码闸 / 参数拼装都有测试。
- 交互式 TUI 只在**管道 stdin** 下测过；没在真 TTY 上人工敲过（沙箱里没 TTY）。
  特别是 `getpass` 在真 TTY 下的回显行为。
- 表格默认不自动刷新。如果发现「数字看漂」，考虑加一个只读的轻量刷新
  （不走浏览器），但会拉长占锁时间。
- `cancel` 的冷静期秒数（`CANCEL_COOLDOWN`）目前硬编码 6s。
- 真 TTY 下 `Console.clear()` 的表现没验证；不好用就 `--no-clear`。
