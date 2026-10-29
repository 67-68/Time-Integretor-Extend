# warden

## 模块职责

一个 rich TUI（`warden.py`），把散落的东西收进一个界面：daemon 状态、**任意一天**的
契约表（data.csv 计划 × API 上真实存在的 session）、以及对单场的写操作（静音 / 标题 / 创建 / 取消）。

定位是**看板 + 审讯台**，不是自动化中枢：它不替代 daemon，也不做定时，**也不重写业务逻辑**
—— 下单和启停走 `keep_session.py` 子进程（薄封装），CSV 解析/编辑走 `schedule.py`。
daemon 负责「保证订上」，warden 负责「随时看得见、随时能改、想取消得付出代价」，
另外还负责「回头看这几个月到底干成什么样」（统计视图，取数在 `stats.py`）。

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
     ├──4) JSON 行（stdin/stdout）──> scripts/bridge.mjs
     │                                  └── Playwright ──> 常驻 Chromium
     │                                      └── 内部 API（PUT/DELETE，裸 Firebase token）
     │
     └──5) stats.py（只读，给统计视图供数）
            ├── GET /v1/me                → memberSince / totalSessionCount
            ├── GET /v1/sessions 按 ≤365 天分块 → 全量历史
            └── temp/stat-history.json    → 1 小时缓存（唯一写的东西）
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
| 执行区 | `now < start ≤ now+24h` | `cancel` 必须**一字不差敲完 152 字宣言**（改排布 `period`/`session T1 …` 不用） |
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
period mute T1      这个时段（今天）的 session 自动开 Focusmate quiet（静音只指这个）
period mute T1 --permanent   以后**每个这个星期几**的 T1 都这样
period unmute T1 [--permanent] / period mute T1 off   撤销
task import         读剪贴板导入任务并自动关联 OmniFocus（⌘V 会丢链接，所以自己读）
task 5 link         不给链接 = 读剪贴板里那条 omnifocus:///task/PK
title 2 "Math Past Paper"
title 2 ""          清空标题
sync [--days N]     跑 download_calendar.py populate：日历 → 覆盖 data.csv
force-check         忽略重试上限/退避：把所有没订上的 session 各强制重订一次
refresh / r         全量刷新看板（session + 任务完成状态 + 统计 + 收尾面板 + 各种缓存）
refresh --check/-c  同上，**再对账一遍**：该订没订上 / 该挂没挂上 → 能修的当场修
task 8 -an          从它那一场下架（≡ -a none，删掉 Focusmate 那条 task）
task 8 -ac          挂到当前这一场（≡ -a current）
task 32 swap 21     两条任务互换位置（一条赋闲也行）
task -s 5 -a 6      把 session #5 上的全部任务转移到 #6（-a none = 全部下架）
start               启动 daemon（先让 bridge 放开浏览器）
stop                停用 daemon（**要敲宣言**）
stat [refresh]      切到「统计」视图：生涯累计 / 每天分布 / 按月 / 永不重订名单
                    （1 小时缓存；refresh = 立刻增量刷新，只拉缺口 + 最近几天）
stat refresh full   统计：丢掉缓存、全量重拉（两千多条；只在怀疑缓存坏了时用）
status / s          重画看板
help / quit
review [HH:MM|日期T时刻]   手动开始一场 session 的收尾（逐个问没做完的任务）
inspect-due [分钟] [--all] 主动 inspect 过期没做完的任务（同一套 A~E）
```

`sync` 不重写日历逻辑：薄封装 `download_calendar.py populate`（`--days` 等参数原样透传），
写完重读 data.csv。它**会覆盖 data.csv**（download_calendar 自己备份到 `temp/data.csv.bak`，
且一个 session 都没切出来时会拒绝覆盖）。和 cancel/stop 不同，sync 不需要敲宣言 ——
它是「重新拉目标」，不是「放弃目标」。

`refresh`（`r`）是**纯读**：只调 bridge 的 `snapshot` 拉选中那天的 session，再和
data.csv 的契约合并重排表格（所以 ID 会重编号），**不会订任何东西**。
"缺"那几行刷一万遍也还是"缺"。订场只有一条路径 —— `keep_session.py` 的 daemon
（`book_session` + API 复核），warden 里没有任何预订入口，也不该有：
TUI 里唯一会「让 daemon 去订」的动作是改完排布/加单场之后触发一轮 `check`
（`session` / `period` / `relocate` / `force-check`）。同理，任务视图里的
`refresh` 只跑 `GET /v1/tasks` 同步完成时间，也不订场。

### `refresh`：看板上每块组件都重新对一次账

`refresh` 不只是「重拉 session 列表」——它是**刷新这界面**，所以一条命令把每块组件都
拆开对一遍（`refresh_all()`），而且**只读外面世界**：不订场、不挂任务、不改 data.csv、
不落「每周重复」（那会在 Focusmate 上真建 task，走 `task sync`）、不写回 OmniFocus
（完成状态只读 `GET /v1/tasks`，见 `sync_task_completions()`）：

| 组件 | 刷什么 |
| --- | --- |
| 契约表格 | 重读 `data.csv` / 日历下载层 / 时段 layout → 重拉 session |
| 任务面板 | 任务的**完成状态**：`GET /v1/tasks` 只读同步（不用再手敲 `task sync`） |
| 重复任务 | **不碰** —— 落地会真的在 Focusmate 建 task，那是 `task sync` 的事 |
| 统计视图 | 已经加载过、且超过 TTL 的那份数字重算一次（没看过就不替用户拉） |
| 收尾面板 | 正在问的那一轮从任务库重读（`Review.reload()`，不把人问回去） |
| 警告 / 静音 / 日志 | mute 缓存、快照缓存、日志尾巴缓存一律作废 |
| 画帧缓存 | `invalidate()` → 下一帧按新状态重排版，不会停在旧画面上 |

任何一块刷不动就记一句警告、其余照刷 —— refresh 是「再对一次账」，不是「全有或全无」。

### `refresh --check`：对账并修正

`refresh` 本身仍然是**纯读**（不碰外面任何东西）。加 `--check`（`-c`）才多走一步
`check_and_fix()` —— 它只做两件事，而且两件都是「先看外面的世界，再掰回来」：

1. **该订没订上的 session**（`check_bookings()`）：执行区里「契约有、API 没有」的行
   → 让 daemon 补一轮 `keep_session.py check`。用普通 `check`（不是 force-check），
   所以 daemon 自己的重试上限/退避照样生效 —— TUI 不越权替它决定「再试一次」。
   草稿区和过去的时间**不算缺**（前者还没轮到，后者已经晚了）。
2. **该挂没挂上的 task**（`check_task_attachments()`）：本地账本说「任务 8 挂在 11:00
   那场」，但 `GET /v1/tasks` 里那条 task 的 `attachments` 里没有这一场的 session id
   → 重新挂（走 `cmd_task_assign` 同一条路）。
   判据只有 Focusmate 自己给的那一份，本地账本说「挂了」不算数：手动在 Task Manager
   删过、或上次调 API 只做了半截，账本都会照旧留着（`_attachment_verdict()`）。

修完再拉一次表格，所以屏幕上看到的是**修之后**的样子。修了几处会打总账
（`✅ 对账完成：修了 N 处` / `没有发现要修的`）。离线模式下这一步直接跳过并明说
——没有外部世界可查。

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

现有五个视图：

| # | 视图 | 主体 | 命令 |
|---|---|---|---|
| 1 | 契约（默认） | **状态框（平滑边框）+ The Contract Table + 任务面板** | 原来那一整套（`session` / `period` / `cancel` / `mute` / `title` / `day` / `sync` / `start` / `stop`…） |
| 2 | 专注 | 目标 + 当前 session + 专注模式 + 可暂停次数 + 一行实时状态 | **`stop`**（消耗一次暂停）+ **`Shift+Tab`**（循环普通 / 做题 / 写作模式）；预约那套命令在这个视图里**一律没用** |
| 3 | 统计 | 生涯累计 + 每天分布 + 按月 + 最近 14 天 + 底部逐行列「永不重订」 | `stat`（走缓存）/ `stat refresh`（增量刷新）/ `stat refresh full`（全量重拉）/ `r` |
| 4 | 任务 | 任务表（含 `状态` / `截止` / `OmniFocus` / `标签` 列）+ 完成归档 | `task create/delete/modify/assign/select/repeat/duplicate/drop/done/link/import/sync` + `review` + `inspect-due` |
| 5 | 设置 | 全部设置项（id / 当前值 / 默认值 / 解释） | `settings`（重画）/ `set <id> <value>`（任何视图都能敲） |

- **统计视图的数据链在 `stats.py`**（只读公开 API，按 ≤365 天分块拉全量历史，
  口径与缓存见 [`stat.md`](./stat.md)）。它不写任何东西：不改 `data.csv`、
  不碰内部写接口、也不动 daemon 的状态文件。
- **进统计视图会懒加载一次**（Tab 进去或敲 `stat` 都算），之后走内存/磁盘缓存。
  只有**第一次**要拉全量（几秒，TUI 会卡那一下），之后是增量：只拉缺口 + 最近 3 天，
  通常 1 个请求、几百 ms；TTL 内的第二次连请求都不发（见 [`stat.md`](./stat.md)）。
- **「永不重订」名单（verdicts 的 `abandoned`）只在统计视图底部逐行列**（一条一行）。
  契约视图里原来那句指路已经**换成警告**（见下面的「状态框」），平常视图不铺开这些记录。
- 通用命令（三个视图都认）：`Tab`（切视图）/ `stat` / `status` / `help` / `quit`。
  注意：**罪案区锁死时 `stat` 也进不去**（和别的命令一样，除了敲宣言什么都不受理）——
  这是既有的锁语义，不是统计视图的特例。
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
### 整帧重画（删掉上一帧，而不是 clear 整屏）

屏幕**永远只有一帧**：`paint()` 先把上一帧从终端上删掉，再画新的。

- 记账：`_frame_rows` = 上一帧占了几行，`_tail_rows` = 帧之后又打了几行
  （提示符那一行、帮助面板、宣言闸门、`>>>` 回显…）。删的时候是
  `\x1b[<n>A` 上移 + `\r\x1b[J` 清到屏幕末尾，**不是** `Console.clear()`。
  为什么不用 `clear()`：它只把屏幕推到滚动历史里，翻上去是一摞残帧。
- **帧外的输出必须走 `emit()`**（它渲染到 `capture` 里数行数再写回 `console.file`）。
  直接 `console.print` 的后果不是「少删一行」而是**光标落进旧内容中间**：
  下一次上移的 n 算少了，屏幕上留半张旧面板。
- 上移行数**钳在 `console.height - 1`**：帧比屏幕高时（比如统计视图很长），
  顶上去的部分在滚动历史里，终端本来也擦不到；不钳的话光标会乱跳。
- 帧比屏幕高是终端的极限，不是 bug：内容多就多滚几行，`--no-clear` 那种"残留"
  才会真正消失。想彻底不管屏幕、只留滚动历史就用 `WARDEN_CLEAR=0`（等于 `--no-clear`）。
- 非 TTY（管道 / 测试的 `Console(file=StringIO)`）一个 ANSI 都不写，退化成纯文本追加
  —— 老测试和 `--exec` 的输出格式没变。
- 消息区（Recent）：**屏幕上只显示最近 `MESSAGE_SHOW`=3 条**（帧越高顶部越容易被顶掉），
  内部留 `MESSAGE_KEEP`=12 条好让「敲错被记账」这类过程还能被测试和排查看到。
- 空闲秒数来自 daemon 写进 `temp/keep_session-idle.json` 的 `last_idle_seconds`
  （daemon 每 5s 读一次 `ioreg HIDIdleTime`）。daemon 没在跑就显示「空闲 ?」。
- 目标暂时是占位（读 `temp/keep_session-goal.txt`，没有就显示占位文案），
  以后接 Focusmate task 时只改 `Warden.current_goal()`。

暂停的规则（次数/时长/跨天）在 [`idle_watchdog.md`](./idle_watchdog.md)，
实现在 `keep_session.pause_status()` / `start_pause()` —— warden 和 daemon 共用同一份。

### 画一帧要多久（「打字掉帧」是怎么修的）

prompt_toolkit 每敲**一个**字就重画一次 chrome（`on_text_changed` → `invalidate()`），
而 chrome = 把整块看板用 rich 重新排版成 ANSI（表格 + 任务对照表 + Recent + 状态框）
再解析回 `FormattedText`。实测（10 行合约表 + 12 条任务）：**一次 27ms**。按 10 字/秒算，
每秒有 270ms 花在画一模一样的东西上 —— 这就是输入时肉眼可见的掉帧。

三处抠回来的（都有回归测试，见 `test/test_warden.py` 的「打字不掉帧」）：

1. **日志只读尾巴**：`log_tail()` 以前是 `f.read().splitlines()`，每帧读整份 daemon 日志。
   日志一天长几 MB，这一项就从 0.1ms 涨到几十 ms（越用越卡）。现在从末尾 seek
   4 KiB 起找够 n 行，再按 `(mtime_ns, size)` 记住结果。66 MB 的日志 → 0.004ms，
   **日志多大都不再影响打字**。
2. **画帧缓存**：`WardenShell.frame_text()` / `chrome()` 按「帧键」复用上一帧的 ANSI
   和 fragments。键 = `frame_rev`（状态变化计数）+ 视图号 + **整秒** + 终端宽高 +
   警告签名 + `Warden.frame_signature()`（消息条数/最后一条、行数、`id(rows)`、
   `csv_fp`、review/stat 身份…）。`Warden.invalidate()` 在 `say()` / `intercept()` /
   `handle()` / `rebuild_rows()` / 警告变化时拍一下。
   - 键里带整秒 = 看板上最细的时间粒度就是秒（时钟、「多久以前」），所以同一秒里
     **敲多少个键都是同一帧**；漏拍 invalidate 也最多慢一秒，和 `tick=1.0` 同量级。
   - 存的是**画完之后**的键：排版这趟自己会改状态（重读 csv、重算警告），拿画之前
     的键去存会让第一帧画两遍。
   - 快路径必须和框架原版拼出**逐字一致**的 fragments —— 有一条测试直接拿
     `repl_kit.ReplShell.chrome()` 对比，改坏立刻红。
3. **`_terminal_rows()` 别用 `get_app()`**：没有正在跑的 Application 时它会现造一个
   `DummyApplication`（5ms，还要 load 一遍按键表）。改用 `get_app_or_none()` +
   退回 `shutil.get_terminal_size()`。

结果：**每次敲键 27ms → 0.1ms**，只剩每秒一次 ~20ms 的整帧重画（`tick=1.0` 本来就要画）。
注意：别往看板上放**秒以下**的时间（倒计时毫秒、spinner），那会让帧键每秒都对不上、
缓存等于白做 —— 真需要就放进 `status_line()`（它不在缓存里，逐帧现算）。

## 静音时段（`period mute`）

「这个时段以后都静音」是**排班意图**，不是单场开关，所以它落在
`temp/session-mutes.json`（按星期几 / 按日期重复），由 `session_plan` 负责判定。
warden 这边只做两件事：

1. `period mute T1 [--permanent]` 写表，然后**立刻**把今天这个窗口里还没静音的场次
   设成 quiet（`apply_mutes()`，走 bridge，API 复核）；
2. 每帧（节流 5 秒）后台补一次（`_maybe_apply_mutes()`）—— 新订上的场次会自己安静下来，
   同一场 5 分钟内只试一次，bridge 挂了也不会每帧开浏览器。

表格「静音」列：`ON` = 已静音；`静…` = 时段要求静音但还没设上；`·` = 不静音。

**范围只有 Focusmate 那一场**：daemon 的通知 / 拉音量 / 嘶吼一概照旧
（用户原话：「daemon 应该在静音时段发通知，拉音量和放嘶吼，静音只是指 focusmate
session」）。同一个表、同一个判定，见 [`session_mute.md`](./session_mute.md)。

## 计划 vs 实际（表格里为什么会出现「认账」）

daemon 会把「计划与实际不一样、但实际确实做了」的场次记成认账（拆分 / 挪了半格，
见 [`keep_session.md`](./keep_session.md)）。契约表读 `state.json` 的 `slots`，
把这种行的状态从「缺」改成「**认账**」，并在标题后标出实际场次
（`· 实际 16:00(25min) + 16:30(50min)（计划 16:15 75min）`）。
「缺」会让人去重订一场和现有场次打架的 session，所以这两个字必须分清楚。

收尾也按**实际**场次走（`actual_slots()` → `postsession.unreviewed_sessions(actual=…)`），
手动 `review` 一样能问到计划外的场次。

## 在场证明（`temp/warden-tui.json`）

TUI 启动时写 `{pid, tty, started_at}`，退出时只擦自己那条。daemon 的「叫人」用它判断
「warden 到底开着没」：没开就新窗口跑 `Warden.command`，开着就按 **tty** 聚焦
（marker 里那个 iTerm session id 在 iTerm 重启 / 拖窗口之后就死了，tty 不会）。
没有这份文件的时候只能靠 marker 猜，于是会出现「日志写着 focused、其实谁都没来」。

## 收尾（session 结束 +15s）

收尾（和 `inspect-due` 查账）共用一块黄色面板。它**不是某个视图的内部件**，而是
`repl_kit` 的**打断栏位**（`ReplShell(interrogate=...)`，框架文档在隔壁仓库
`paper_researcher/repl_kit/README.md` 的「The interrogate slot」）。
它压在**整块看板下面、状态栏上面**（顺序：看板 → 收尾面板 → 视图栏 → 提示行 → 状态行
→ 输入行）。所以换视图它不会消失、表格再长也挤不掉它、终端太矮裁屏也是从顶部裁。
一问完栏位立刻消失，不占一行。

```
⏱ 收尾 · 2026-10-27 11:00–11:50
任务 3：写论文
  · session 2026-10-27 11:00（截止 2026-10-27T11:50:00）· Focusmate 42cabb17… · OmniFocus kc-4VLAE_1L
  A  延期   你自己敲命令把任务转移走（复制品 + 旧场次放弃 + 本地标签「未完成转移」）
  B  放弃   这条真不要了：本地放弃 + OmniFocus 也放弃
  C  重议   复制一条（指向同一条 OmniFocus 任务），旧的放弃；OmniFocus 不动
  D  缺漏完成 手动填实际分钟数 → 本地完成 + Focusmate 勾上 + OmniFocus 完成
  E  快捷推后 和延期一样，但复制品由系统建好、直接塞进下一场
第 1/2 条 · A 延期 · B 放弃 · C 重议 · D 缺漏完成 · E 快捷推后 · s 跳过 · q 结束收尾
```

- **谁来催**：daemon（`keep_session.postsession_tick()`，结束 +15 秒）写
  `temp/keep_session-postsession.json`，再 `osascript app/warden-summon.applescript`
  把 warden 拉到前台。warden 每帧（自己节流 1 秒）看这个文件，有就接过来。
- **核对完成状态要联网/起进程**（bridge 的 `GET /v1/tasks` + `omnifocus.py list`），
  所以这一段在**后台线程**里跑，面板先显示「正在核对…」，核对完才建
  `postsession.Review` —— 画帧的线程永远不会被卡住。
- **A 延期阶段是「等你敲命令」**：这一行输入照常派发（例如 `task 3 duplicate -a 5`），
  派发完回头判「转移到底发生没发生」。这个回头钩子挂在 `guard()` 里，
  因为 TUI 的交互路径走 repl_kit 的 dispatch，**不经过** `Warden.handle`。
- 收完之后这一场从 `pending` 挪到 `done`，永不重催。
- 细节见 [`postsession.md`](./postsession.md)。

**`inspect-due`：主动查账**。自动催只盯「刚结束的那一场」；过期了却压根没挂过场次、
或者那场早就翻篇的任务，用这条命令拉出来过一遍（同一套 A~E）：

```
inspect-due          已经过了截止时间的
inspect-due 60       未来 60 分钟内要到的也算
inspect-due --all    不按截止时间筛（没做完的都看）
```

面板标题是「过期未完成任务」，每条多一行「截止 …，已过期 2 小时」。
结束时不写 postsession 状态文件（没有那一场可以翻篇）。
过期任务同时会出现在状态框的警告里，提示你敲这条命令。

## OmniFocus 联动（只单向）

- 「完成 / 放弃 / 截止时间」写回 OmniFocus；「活还在不在、叫什么」以 OmniFocus 为准。
- 任务被分配到一场 session 上时，**截止时间自动 = 那场的终点**
  （11:00–11:50 → `2026-10-27T11:50:00`，原值留档在 `session.previous_due`）。
- 完成的时候用一条命令把「完成 + 截止时间」写回去
  （`set --id <PK> --complete --due … --yes`），写回痕迹记在 `omnifocus.writeback`。
- OmniFocus 读不到（没装 / 权限没给）只变成一句警告：收尾照常进行。
- 「延期 / 快捷推后 / 重议」**不碰** OmniFocus —— 活还在，只是本地换了账本。
- 关掉：`set omnifocus off`。细节见 [`docs/omnifocus.md`](../docs/omnifocus.md)。

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

- **`handle()` 不展开紧凑短选项**：`-an` 的展开原本只挂在 repl_kit 的 `dispatch`
  上，于是交互式里能敲、`--exec 'task 8 -an'` 和测试走的 `handle()` 却不认
  （报「后面不用再跟东西」）。现在两条路都走 `Command.expand()` ——
  框架里的一份实现，谁也别自己再写一遍。

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
10. **帧外打印不记账 = 屏幕留残影**。整帧重画靠「上一帧占几行」来决定上移多少，
   帧外的 `console.print`（帮助面板 / 宣言闸门 / `>>>`）不经过 `emit()` 就没记进
   `_tail_rows` → 少上移几行 → 光标落在旧面板中间重画。全部改走 `emit()`。
11. **上移行数要用 `console.height`，不是 `_term_height()`**。测试里造的是
   `Console(file=StringIO, height=40)` 这种"矮终端"，用 `shutil.get_terminal_size()`
   会拿到 fallback（24 行）→ 钳位钳错，断言就对不上。真 TTY 上两者一致。

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
- **整帧重画的真 TTY 手感没人工验证过**（沙箱里没 TTY）：转义序列
  （`\x1b[<n>A\r\x1b[J`）、Tab 切视图时"提示符那一行"的行数差、中文输入折行，
  都只在假终端（`force_terminal=True` 的 StringIO）上断言过。不好用就
  `WARDEN_CLEAR=0` 退回旧的追加行为（逃生通道一直留着）。
- **统计视图第一次进会卡几秒**（拉全量历史，按年分块）；之后都是增量
  （缺口 + 回看 3 天，1 个请求）。没做后台线程 —— 真嫌卡就把 `stats.CACHE_TTL` 调大，
  或用 `stat refresh full` 之外的路径。
- **统计视图的高度**：内容比屏幕高时顶部会滚进滚动历史（终端的极限）。面板里
  「最近 14 天」+「按月」+「永不重订」都想要，所以行数不少；嫌高就先砍按月。

## 已删除的历史设计（别再抄回来）

- **停用密码**：曾经有「≥12 字符 + 3 类字符」的密码，PBKDF2 存 `temp/warden.json`。
  用户要求删掉，改成和取消共用的宣言闸门。
- **cancel 的地狱级阻力**：曾经是「红面板 → 手打开始时间 → 6 秒冷静期 → DELETE」。
  现在按区分层：草稿区零阻力、执行区敲宣言。`CANCEL_COOLDOWN` 常量已删。


## 状态框（警告） + 设置视图

### 状态框

契约视图最上面那块是**状态框**：一个平滑重线边框的块（`━━ ┃ ┏┓┗┛`，和表格那套
字形同一套；挑字体/老终端可以整体换成 `+ - |`——`StatusBox(border=ASCII_BORDER)`），
标题「状态」：

```
┏━━ 状态 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 契约（data.csv 周一）：计划 3 场 · 已落地 3 场 ✅ 一场不缺        ┃
┃ 来源：时段排布 8 · 一次性 1 · 长期规则 2   （session = …）        ┃
┃ /Users/.../data.csv                                             ┃
┃ ! 今天的 session 里没有「明日时间安排」这类每周重复任务            ┃
┃     → task create title=明日时间安排 --repeat mon@22:15           ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

- 上半是 `ContractView.status_box()` 给的行（契约执行情况 / 来源 / 临时挪动 / 文件路径）；
- 下半是**警告**：`repl_kit.WarningBoard` 里当前的条目。warden 只负责"有什么问题"
  （`collect_warnings()`），画框、限宽、CJK 对齐、按视图显示都由框架做。
- 警告来源：`data.csv` 解析问题、时段 layout 坏了、重算 `data.csv` 冲掉的旧行、
  今天没有任何「每周重复任务」落点（附 `--repeat` 的写法）。
- 默认显示、默认只在**契约**视图显示（`show_warnings=True` + `warnings_view=("契约",)`）；
  `warn on|off` 可以随时开关（只藏警告，契约那几行还在）。
- 每帧画框之前会 `refresh_warnings()` 重算（5 秒节流，纯本地、不联网、不写盘）；
  每条命令跑完也会强制重算一次 —— 框里永远是"现在"的事实。

### 设置视图

设置是**数据**（`settings_store.SETTINGS`），不是散落在 warden 里的 if：

| id | 默认 | 作用 |
|---|---|---|
| `edit-tomorrow` | `on` | 今天可以先安排明天：选中「明天」时按**草稿区**处理（晚上 11 点想排明天早上 10 点不用敲宣言）。过了凌晨明天变今天，自动回到 24 小时窗口那套规则。行上仍保留原始判据 `zone_raw`，方便追溯 |
| `tomorrow-plan` | 空 | 「明日时间安排」：一个每周重复任务的规格（`mon@22:15`）。填了会提示用 `task create ... --repeat` 建出来 |
| `omnifocus` | `on` | 和 OmniFocus 联动（**只单向**：warden → OmniFocus）。`off` = 一个字节都不碰 |
| `postsession` | `on` | session 结束 15 秒后把 warden 拉到前台收尾。`off` = 不催（`review` 仍可手动开） |
| `omnifocus` | `on` | 和 OmniFocus 联动（**只单向**：warden → OmniFocus）。`off` = 一个字节都不碰 |
| `postsession` | `on` | session 结束 15 秒后把 warden 拉到前台收尾。`off` = 不催（`review` 仍可手动开） |

- 界面列出**全部**设置项（哪怕从没改过）连同解释、当前值和默认值；改过的标 `*`。
- `set <id> <value>` 改；`set <id>`（不写 value）= 恢复默认。任何视图都能敲。
- 只把**改过的**值写进 `data/settings.json`：改回默认值 = 从文件里删掉，
  以后默认值变了能跟着走。

## 2026-10 追加：时段列 + 任务视图

- 契约表增加 `时段 ID | 时段 | 类型 | 名称` 四列；同一时段只有第一行显示，
  后续行留空（避免把同属 T1 的 session 误看成 T2）。
- 来源是 `temp/calendar-download.json`；没有下载层时这四列留空，旧流程照常。
- 第四个视图 `任务 / 归档`：`task create/delete/modify/assign/select/sync`。
  任务挂 session 时自动在 Focusmate 建 task；完成时间用 `GET /v1/tasks` 的
  `completedAt`，归档到 `data/task-archive.json`。
- `session T1 50 50` / `period T1 50 50` / 裸 `T1 50 50` 改时段排布；
  任何一场超出时段终点 → 报错，layout 和 data.csv 都不动。
- 改排布时若该时段已有 API session 且起点/时长不再匹配，先取消旧场
  （**改排布不要宣言**，用户明确要求：计划随你改，障碍只在 `cancel N`），
  再写新 layout + data.csv，最后立刻 `check` 补订新场次。
- 取消旧场之前会把面板摊开：要取消哪几场、哪些任务绑定会被清掉，一目了然。
- 任务绑定按「(起点, 时长) 有没有动」保留：没动的场次绑定留着，动过的清掉
  （详见 `session_plan.md` 的「任务绑定」一节）。
- **改排布不能把 daemon 惹毛**：`period` 的那一段（取消旧场 → 清 state → 写新排布）
  包在 `keep_session.round_lock()` 里和 daemon 的一轮互斥，并且在真正取消**之前**
  把会消失的 state key 写进「挂号单」（`ks.expect_cancel`），daemon 见到
  `booked-but-missing` 先查挂号单、命中就不记罪。另外重新排布会
  `ks.clear_abandoned(新排布 key)`，把「永不重订」判决撤掉 —— 否则一次误判就让
  那几场永远补不回来。详见 `session_plan.md`。
- 第五个视图 `设置 / 规则` + `set <id> <value>`；`edit-tomorrow` 让「明天」临时按
  草稿区处理（`Warden.effective_zone`），`tomorrow-plan` 是「明日时间安排」的规格。
- 契约表格下面那块任务面板**按位置连号排**（还是原来那一张表，不拆）：`未挂 session`
  的排最前，之后一场 session 一段（按开始时间）、段内按任务 ID 升序，同类的挨在一起
  （详见 `task.md`）。
- `task` 入口长出四个位置动作：`-an`（下架）/ `-ac`（当前这一场）/
  `32 swap 21`（互换位置）/ `-s 5 -a 6`（整场转移）；紧凑写法由 repl_kit 的
  `ShortFlag` 统一展开，补全和提示都跟着走。
- `refresh --check`（`-c`）：重拉 + 对账（该订没订上 → 叫 daemon 补；该挂没挂上 →
  重挂），所以「本地账本说挂上了、Focusmate 上其实没有」这种半截状态能被自己发现。
  不带 `--check` 的 `refresh` 也已经是**全量**刷新（见上面那节），只是不动外面东西。
- `session` 默认**只加这一天**（`temp/session-extras.json`），`session --permanent`
  才写成按星期几重复的长期规则（`temp/session-rules.json`）；两者都在重算
  `data.csv` 时并进去，sync 不会冲掉。
- **契约视图的 task 列改成「编号: 标题」**：`2: 写论文 | 5: 复习错题`，每条标题
  单独裁到 `TASK_CELL_TITLE_MAX`（18）个字再拼，别让第一条把第二条挤出屏幕；
  标题优先取 `title`/`name` 字段（`focusmate_title`），不是整条 `1. … 2. …` 摘要。
- **契约表格下面多了一块「任务面板」**（`Warden.render_task_index()`）：
  编号 + 标题成对并排，**并排对数跟着屏幕宽度走**（2 对是底线，宽屏自动摊成 3 / 4 对，
  `TASK_INDEX_MAX_COLS`；**一行一行填**，一行里从左读到右就是排序里的连号）。
  顺序见 `task_panel_order()`：先按状态分段（进行中 → 完成 → 放弃），段内未挂 session
  最前、之后按场次时间、段内按编号（详见 `task.md`）。配色分工：已挂 session = 亮红、
  没挂 = 亮蓝、完成 = 绿、收工（放弃/转移）= 亮灰。
  布局顺序 = 契约表格 → 任务面板 → Recent（Recent 仍由 `frame()` 接在后面）。
- **任务面板按天取值**（`Warden.task_panel_tasks()`）：今天（以及以后）= **所有没做完的**
  \+ **今天被处理过的**；**过去那天固化** = 只剩「那一天的 session 里被处理过的」。
  「被处理过」不挑结果：放弃 / 完成 / 补录 / 重议 / 推后 / 转移走 / 跳过都算一笔，
  痕迹落在任务的 `history`（`TaskStore.mark_processed`，键是 day + start + action）。
  老数据没有痕迹，按 `status_at`／Focusmate 的 `completedAt` 兜底（`processed_on`）。
- `task create ... --repeat 周几@HH:MM` / `task repeat <id> ...`：每周重复任务，
  只落在「同一星期几 + 同一时刻 + 同一时长」的那场 session 上（拆分后那一周不落，
  设置不变）；`task sync` 负责真的挂到 Focusmate。
