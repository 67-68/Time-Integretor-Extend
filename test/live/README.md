# test/live —— 集成测试（会碰真实 Focusmate 的那一层）

`test/test_*.py` 是进程内的单元测试：bridge 是假的，`keep_session` 被 stub 掉，
data.csv 是临时目录。它们验不了「模块之间的接缝」，也验不了「Focusmate 上到底
有没有那一场」。

这里的测试把**整条线**真的跑一遍：

```
fixture（沙盒 + mock 数据）
  → warden --exec "plan 10; task 1 2; task 2 2; task 3 1; complete"
  → 分配 → 挑任务 → 切 session → 写 layout → 重算 data.csv
  → keep_session check → 真下单 → 真 Focusmate session
  → refresh --check → 把任务挂成 Focusmate task
  → probe_live.mjs（独立探针，只读 API）复核事实
  → cancel 清场
```

## 三个入口

| 命令 | 会不会真订 | 用途 |
|---|---|---|
| `python3 -B test/live/test_live_plan.py` | 不会（进程内，stub 掉预约出口） | 39 项断言，覆盖 plan 全链路：容量→排布→绑定→plan item 口径→对账 |
| `python3 -B test/live/test_live_edge_cases.py` | 不会（进程内） | **历史失败特征全集**（`fixtures/edge-cases/`，14 条特征 / 31 条断言）：过去/进行中/已订/未订/计划外/跨午夜/清单容器/重复任务/残留任务/独占冲突/计划-实际错位/未切段 |
| `python3 -B test/live/test_live_midday.py` | 不会（进程内） | **真实数据回放**：`fixtures/midday-1558/` 是 2026-11-16 15:58 的现场快照（T1 已过去、15:15 正在进行、19:00 已订上、22:15 整段 60min），验「只排未来 / 上一轮保留 / 已订场次不重切 / 60min 段能切」 |
| `python3 -B test/live/test_live_focusmate.py --yes --plumbing` | 不会（执行区仍是 24h） | 走**真子进程** `warden.py --exec`，验沙盒隔离 + 命令分发 + data.csv 落盘 |
| `python3 -B test/live/test_live_focusmate.py --yes` | **会真订 2 场并取消** | 端到端：Focusmate 上真有那两场、task 真挂上 |

不带 `--yes` 只打印计划，什么都不做。

## 真实数据回放（`fixtures/midday-1558/`）

这一份不是手写的：从真实工作区抓的 `data.csv` / `tasks.json` / `projects.json` /
`session-layouts.json` / `calendar-download.json`，只把日期换成 ``{{DAY}}``
（跑测试时替换成今天），时钟钉在 **15:58**。它复现的是用户 2026-11-16 那一屏：

* T1 09:00–12:30 整段已过去；
* T2 里 14:00(75) 已结束、15:15(50) 正在进行、16:15(75)/17:30(25) 未订；
* T3 19:00(75) **已经订上**、且挂着任务；
* T4 22:15–23:15 整段 60min 还没切。

四条口径（改 plan 之前它全红，改完全绿）：只从下一个十五分钟整点往后排；
上一轮安排保留（`plan clear` 才清）；已订上的场次不许重切；需要时 60min 段要
切出 50 分钟那场。

## 历史失败特征全集（`fixtures/edge-cases/`）

用户 2026-11-16 的要求：「integration test 数据包括我之前的所有失败情况的特征，
他本身就应该是一个 edge case 大全」。所以这份 fixture 是**一份数据里同时塞进
14 条历史失败特征**，测试按特征一条一条断言（`make_edge_fixture.py` 顶部那张表
就是索引）。规矩：以后每修一个 bug，就在 fixture 里加一条对应数据 + 在
`test_live_edge_cases.py` 里加一条断言，不许只补单元测试。

容量口径：15:58 敲 plan → 16:15(3) + 17:30(1) + 22:15(2) = **6 count**；
19:00 / 23:15 挂着任务不算空场；T1 / 14:00 / 15:15 已过去或进行中不算。

## 沙盒（`fixture.py`）

凭空造一天，不碰用户的真实数据：

```
<sandbox>/data.csv                       只有注释头（一天都还没排）
<sandbox>/data/tasks.json                空任务库
<sandbox>/data/projects.json             3 个 pro + 3 个 SOP op
<sandbox>/temp/calendar-download.json    只有目标日 13:00–15:30 一个时段
<sandbox>/temp/...                       其余运行态（state / verdicts / 日志）
```

时段挑 13:00–15:30（150 分钟 → 5 count）不是随便挑的：容量 5 count 反推出的
排布正好是 **75 + 50 两场**（75+15 休息+50 = 150），所以整个测试**只会有 2 次
真实预约**。三个项目刻意不同形：

| 项目 | 形状 | 钉住的逻辑 |
|---|---|---|
| A | parallel，1 条 50 分钟任务（2 count） | 一对一站位 |
| B | sequential，1 条 50 分钟任务（2 count） | sequential 排序 |
| C | parallel，1 条 **10 分钟**任务（1 count） | <25 分钟必须叠进别人的大场（`place.duration` ≠ session 时长） |

## 隔离靠什么

`fixture.env_for()` 把 `TI_TEMP` / `TI_DATA_CSV` / `TI_SESSION_*` / `TI_TASKS_FILE`
/ `TI_PROJECTS_FILE` / `TI_LOG_DIR` / `TI_KEYTIME_*` 全部指进沙盒（这几个环境变量
是 2026-11-16 为集成测试补的，默认值不变）。唯一的例外是
`FOCUSMATE_CONFIG_DIR` —— 它**故意**指向真实的浏览器 profile，否则没有登录态、
订不了场。

`TI_BOOK_WINDOW_HOURS` 用来放宽 `keep_session` 的执行区：默认 24 小时，不设就是
原行为；真跑时放宽到能盖住目标日（否则 plan 排的 +10 天场次永远不进执行区）。

## 安全闸门

* 真跑要 `--yes`；同一台机器 **3 天内只允许 1 次**（`test/live/.live-ledger.json`），
  要硬跑加 `--force`。
* 目标日固定 **+10 天**，Focusmate 上必然还没订，跑完取消也不算迟到取消。
* 清场**无条件执行**（哪怕前面红了）：`cancel 2` 再 `cancel 1`（倒序，免得行号
  被顶掉）；清完再用探针复核这一天真的 0 场。
* 探针只发 `GET`；`openSession` 走 `scripts/profile_lock.mjs`，和 daemon / TUI
  抢的是同一把锁，不会双开 Chromium 把登录态踩丢。
* daemon 在跑时只警告（有锁兜底），不会为了测试叫你停 daemon。

## 已知取舍

* Focusmate 额度：一次真跑 = 2 次预约 + 2 次取消，提前 10 天取消没有惩罚。
* 探针每次要开一次浏览器（~10-15 秒，dashboard 首屏要等），一次真跑大约 3 分钟。
* `--exec` 过去会被「琐事屏」吞掉命令（空闲屏整屏接管，只认 m/c/q）；已修：
  脚本模式下不接管，且固定从契约视图起跑。`test/test_warden.py` 里有回归。
