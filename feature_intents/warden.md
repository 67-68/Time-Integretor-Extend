# warden

## 模块职责

一个 rich TUI（`warden.py`），把散落的东西收进一个界面：daemon 状态、**任意一天**的
契约表（data.csv 计划 × API 上真实存在的 session）、以及对单场的写操作（静音 / 标题 / 创建 / 取消）。

定位是**看板 + 审讯台**，不是自动化中枢：它不替代 daemon，也不做定时，**也不重写业务逻辑**
—— 下单和启停走 `keep_session.py` 子进程（薄封装），CSV 解析/编辑走 `schedule.py`。
daemon 负责「保证订上」，warden 负责「随时看得见、随时能改、想取消得付出代价」。

## 架构

```
  warden.py (rich TUI)
     │
     │ 1) 只读文件：temp/keep_session-state.json / .pid / .log
     │             temp/warden-sins.json / keep_session-verdicts.json
     │    → daemon Running/Stopped、未认罪的罪、已放弃的场次
     │
     │ 2) 读写 data.csv（控制面）──> schedule.py（解析/序列化/增删的唯一实现）
     │    → day 切换看哪天、session 加一场、草稿区 cancel 删一场
     │    → 时间可写 `HH:MM,duration`，也可写 `HH:MM-HH:MM` 区间（时长由区间推出）
     │
     │ 3) 子进程：python3 keep_session.py check | daemon start | stop
     │            python3 download_calendar.py populate      ← sync：日历 → data.csv
     │    → 下单/启停/拉日历都走现成 CLI，不在自己进程里 fork、也不重写
     │
     └──4) JSON 行（stdin/stdout）──> scripts/bridge.mjs
                                        └── Playwright ──> 常驻 Chromium
                                            └── 内部 API（PUT/DELETE，裸 Firebase token）
```

为什么要有 bridge：`scripts/*.mjs` 那四个 CLI 每次调用都要冷启动一个 Chromium。
实测冷启动 ~9.7s，接上 browser 再发命令 ~1.9s。TUI 里连着敲
`mute 3` / `title 2 "..."`，冷启动那种节奏没法用，所以把浏览器留在内存里。

为什么是**常驻**而不是「每条命令起一个」：见上面那组数字。代价是要管子进程
生命周期（见「踩过的坑」）。

## 三层摩擦（这套系统真正的规则）

「能不能自由改动」不取决于操作，取决于**那一场在哪个区**。判据是滚动 24 小时窗口
（`schedule.window`，见 `feature_intents/schedule.md`）：

| 层 | 判据 | 代价 |
|---|---|---|
| 草稿区 | `start > now+24h` | 零阻力：`session` 加、`cancel` 删，直接改 data.csv |
| 执行区 | `now < start ≤ now+24h` | `cancel` 必须**一字不差敲完 152 字宣言** |
| 罪案区 | state 说 booked、API 里查不到 | **全局锁死**：除宣言外任何命令都不受理（含 `session` / `day` / `stop`）；罪落盘，关掉重开照样锁 |
| 过去 | `start ≤ now` | 只能看：不取消也不补订 |

项目原本提的五个概念里，**Panic Action 按用户要求不实现** ——「一键强行触发倒计时」
会绕过 daemon 的节奏和契约窗口，且和取消的阻力设计自相矛盾。

## 命令

```
day / day -1 / day 0 / day 1 / day 2026-10-11   切换看哪一天（草稿区随便看）
session HH:MM LEN   创建一场：先写 data.csv，再立刻触发一轮 check（LEN ∈ 25/50/75）
session HH:MM-HH:MM 同上，但用起点–终点：11:15-12:05 → 写入 11:15 + 50min
                    （11:15 12:05 / 11:15 - 12:05 / "11:15 - 12:05" 都认）
cancel N            取消：草稿区直接删契约行；执行区敲宣言；过去直接拒
relocate N -15      临时把 #N 提前 15 分钟（只改选中这一天的计划，不写 data.csv）
relocate N +15      临时把 #N 推后 15 分钟；同一天只能推一次，且不能跨天
mute 3              把 #3 静音（不写 on/off 就是取反）
mute 3 on|off       显式开/关
title 2 "Math Past Paper"
title 2 ""          清空标题
sync [--days N]     跑 download_calendar.py populate：日历 → 覆盖 data.csv
force-check         忽略重试上限/退避：把所有没订上的 session 各强制重订一次
refresh / r         重新拉选中那天的 session（ID 会重编号）
start               启动 daemon（先让 bridge 放开浏览器）
stop                停用 daemon（**要敲宣言**）
status / s          重画看板
help / quit
```

`sync` 不重写日历逻辑：薄封装 `download_calendar.py populate`（`--days` 等参数原样透传），
写完重读 data.csv。它**会覆盖 data.csv**（download_calendar 自己备份到 `temp/data.csv.bak`，
且一个 session 都没切出来时会拒绝覆盖）。和 cancel/stop 不同，sync 不需要敲宣言 ——
它是「重新拉目标」，不是「放弃目标」。

`relocate` 和 `sync` 相反：它**不写 data.csv**，只把「某一天 + 原 HH:MM」映射到新时间
写进 `temp/keep_session-relocations.json`。`schedule.day_slots()` 是唯一入口，所以 warden
和 daemon 会同时看到「原 slot 消失、新 slot 出现」；过了这一天，记录不再命中任何日期，
未来同星期几完全不受影响。提前不设次数；推后是拖延诱惑，所以**每个 session 日期只准一次**，
且原时间和目标时间必须在同一天（23:45 +15 会被拒）。执行区里挪动会先取消原 API session，
再让 daemon 走一轮 `check` 订新时间；原时间对应的 state/判决不会被写成「罪」。

**ID 是「当前这张表里的行号」**，按时间排序，每次 refresh 重新编号。所以
命令里的 N 只对「你现在看到的这张表」有效 —— 表在屏幕上是显式的，看完再敲。

`mute` / `title` / `cancel` 都走 `startMs` + `meetingId` **双重校验**：先按
`startMs` 找到那一场，再核对 `meetingId` 是否一致，不一致直接中止、不做任何写。
这是防「ID 漂移」和「表过期」的闸门。

表格把**契约和现实合并**成一行行：契约有 + API 有 = 已订（有 id、能改）；
契约有 + API 没有 = `缺`（草稿区里是正常的「还没吃进去」）；API 有 + 契约没有 = `计划外`。
拿不到 bridge 时用 state.json 快照兜底，此时**不给「缺/已订」的结论**（快照里没有 id），
一律标 `快照`。

## 视图（Tab 切换）

TUI 现在是**多视图**的：按 **Tab** 在最下面那行「视图」上切换，视图名/内容就显示在
命令提示上面。视图是可扩展的 —— 加一个视图 = 继承 `View`、填 `key/name/label/hint`、
实现 `render()` / `handle()`，再用 `@register_view` 注册；`paint()` / 切换 / 提示
都会自动跟上（`VIEWS` 列表驱动，顺序即 Tab 顺序）。

现有两个视图：

| # | 视图 | 主体 | 命令 |
|---|---|---|---|
| 1 | 契约（默认） | The Contract Table + 契约摘要 | 原来那一整套（`session` / `cancel` / `mute` / `title` / `day` / `sync` / `start` / `stop`…） |
| 2 | 专注 | 目标 + 当前 session + 专注模式 + 可暂停次数 + 一行实时状态 | **`stop`**（消耗一次暂停）+ **`Shift+Tab`**（循环普通 / 做题 / 写作模式）；预约那套命令在这个视图里**一律没用** |

- 通用命令（两个视图都认）：`Tab`（切视图）/ `status` / `help` / `quit`。
- **专注视图的 `stop` ≠ 契约视图的 `stop`**：前者是「消耗一次暂停」，
  后者是「停 daemon（要敲宣言）」。同一个词在两个视图里语义不同，由视图分发。
- **Shift+Tab 切专注模式**：普通=1 分钟无输入直接记违规、3 次惩罚；做题=3 分钟、
  写作=10 分钟，做题/写作在违规前 30s 先预警，且第 1 次违规就直接惩罚。
  选择写进 `temp/keep_session-mode.json`：有进行中的 session 就绑定本场，没有就设为
  下一场默认；daemon 在新 session 第一次 tick 时把「下一场默认」绑到本场，之后下一场
  自然回到普通。daemon 每 tick 重读，改完不用 restart。
- **动态数字不重画整屏**。`View.status_line()` 给出一行纯文本（专注视图 =
  「模式 做题 · 暂停 04:32 · 空闲 12s · 可暂停 2/3」），`paint()` 把它打在**最后一行**
  （紧贴提示符上面）；读输入时 `_read_line_tty()` 每秒只做
  `\x1b[1A` 上移 → `\r` + 新内容 + `\x1b[K`（清到行尾）→ `\x1b[1B` 下移，再重画提示符行。
  所以倒计时/空闲秒数在走的时候屏幕**不抖**，也不会每秒 clear 一次、更不会每秒 ping
  bridge。契约视图 `status_line()` 返回 None → 完全不 tick。
- TTY 下用 cbreak 逐键读（`read_command`），**Tab 是即时切换**、不用回车；
  **Shift+Tab（`\x1b[Z`）** 是即时切模式；管道 / `--exec` 下按行处理，一行 Tab /
  `shift-tab` 也算。
- 空闲秒数来自 daemon 写进 `temp/keep_session-idle.json` 的 `last_idle_seconds`
  （daemon 每 5s 读一次 `ioreg HIDIdleTime`）。daemon 没在跑就显示「空闲 ?」。
- 目标暂时是占位（读 `temp/keep_session-goal.txt`，没有就显示占位文案），
  以后接 Focusmate task 时只改 `Warden.current_goal()`。

暂停的规则（次数/时长/跨天）在 [`idle_watchdog.md`](./idle_watchdog.md)，
实现在 `keep_session.pause_status()` / `start_pause()` —— warden 和 daemon 共用同一份。

## 宣言闸门（The Walk of Shame）

密码系统已按用户要求**整个删除**（`password_problems` / PBKDF2 / `temp/warden.json` /
`--set-password` 全没了）。取而代之：**停 daemon 和取消 session 共用同一道宣言闸门**。

- 宣言 152 字符 = 用户原文 99 字一字未改 + 补写 53 字（「补到 ~150」时用户点头的那两句）。
- 比对规则：`strip()` 掉前导/尾随空白后**逐字比较**。少一个标点、把全角 `，` 敲成半角 `,`、
  中间多一个字，全部不通过。
- 不通过 = **鸣笛**（终端 `\a`）+ 指出第一个不一致的位置和字数差 + 继续问，**没有次数上限**。
- Ctrl+C / EOF = 放弃，什么都不做。
- 判据只有一处实现（`check_declaration()`），测试覆盖整段/少字/多字/半角/空白。

## 认罪之后：宣言是有牙齿的

`confess()` 在敲对宣言之后做三件事：

1. 把每一笔罪的 slot 写进 `temp/keep_session-verdicts.json` 的 `abandoned`
   → **daemon 从此不再重订这一场**（否则宣言只是表演）。
2. 如果它已经被 daemon 重订回来了 → 通过 bridge 再取消掉一次（「放弃」必须是真的）。
3. 罪从 `warden-sins.json` 挪进 `warden-sins.jsonl`（只增不删的账本）。

已经过去的罪只判放弃、不动 API（取消过去的场次没意义）。

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
4. **state.json 的快照含跨天数据**。旧 daemon 用 UTC 日界查，会带上本地上一天的
   23:15。退化快照必须按本地日期再滤一遍，否则表里混进昨天的场次。
   现在快照本来就是 24 小时窗口的内容，按选中那天过滤。
5. **快照里没有 id，不能说「缺」**。离线/`--status` 模式下拿 state.json 快照兜底时，
   所有计划行都显示「缺」——那是**假信息**（快照根本看不出订没订）。
   现在引入 `authoritative`（只有 bridge 拉的才算真数据），快照模式一律标 `快照`。
6. **离线/`--status` 路径忘了读 data.csv**。`paint()` 不重读契约表的话，
   表格会退化成「全是计划外」、窗口说「一条契约都没有」。现在 `paint()` 和
   `_fallback_rows()` 都先 `reload_entries()`。
7. **rich 的 `Text()` 不解析 markup**。`Text("[bold]▶[/]")` 会把方括号原样打出来。
   要么用 `console.print`（解析 markup），要么用 `Text.append(style=)`。
8. `Table.grid_from_rows` 不存在；`[[...]]` 这种转义在长行里容易渲染成 `[]`。
   少用转义，直接换措辞。
9. **state 的 key 剪枝不能用字符串比较**。旧格式的 key 是 `"20:00"`，而
   `"20:00" > "2026-10-08"` 在字典序上**成立**（`:` 排在数字后面），会把旧记录留下来。
   现在按 `date.fromisoformat(k[:10])` 判。

## 依赖

`rich` 装在 `temp/pylibs`（和 `download_calendar.py` 同一个路子，不污染系统）：

```
python3 warden.py install-deps
```

`temp/` 在 `.gitignore` 里，所以换机器要重跑一次 `install-deps`。

## 未验证 / 待办

- **`stop` 的 `keep_session.py daemon stop` 子进程路径没有在真 daemon 上跑过**
  （怕把正在跑的 daemon 停掉影响今天的场次）。`start` / 宣言闸 / 参数拼装都有测试。
- 交互式 TUI 只在**管道 stdin** 下测过；没在真 TTY 上人工敲过（沙箱里没 TTY）。
  特别是 152 字宣言在真 TTY 下的粘贴/回显行为。
- **视图的 Tab 键 / 动态行原地刷新只在管道下测过**：`read_command()` 的 cbreak 逐键读
  （真 Tab 即时切换、`status_line_escape()` 的 `\x1b[1A`/`\x1b[K`/`\x1b[1B` 原地覆盖）
  没在真 TTY 上人工敲过；管道下用「一行 Tab」走的是同一条 `SwitchView` 路径，
  但真 TTY 的键盘解码/光标重画（尤其中文输入、行尾折行）没验证。
  不好用就先用 `tab` 命令切视图。
- **`session` 的"立刻触发一轮 check"没在真账号上跑过**（那会真下单）。它调的是
  现成的 `keep_session.py check`，路径本身是通的，但没实地验证过。
- **`sync` 没在真日历上端到端跑过**（那会覆盖 data.csv）。它薄封装
  `download_calendar.py populate`；子进程路径/参数透传/失败提示都有测试，
  `populate` 本身另有 `test_download_calendar.py`。
- 表格默认不自动刷新。如果发现「数字看漂」，考虑加一个只读的轻量刷新
  （不走浏览器），但会拉长占锁时间。
- 真 TTY 下 `Console.clear()` 的表现没验证；不好用就 `--no-clear`。

## 已删除的历史设计（别再抄回来）

- **停用密码**：曾经有「≥12 字符 + 3 类字符」的密码，PBKDF2 存 `temp/warden.json`。
  用户要求删掉，改成和取消共用的宣言闸门。
- **cancel 的地狱级阻力**：曾经是「红面板 → 手打开始时间 → 6 秒冷静期 → DELETE」。
  现在按区分层：草稿区零阻力、执行区敲宣言。`CANCEL_COOLDOWN` 常量已删。
