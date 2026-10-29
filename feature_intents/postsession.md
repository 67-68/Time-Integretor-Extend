# postsession（session 结束后的强制对账）

## 一句话

一场 session **结束 15 秒后**，daemon 把 warden 拉到前台，然后一台一台地问：
这段没做完的活，是延期、放弃、重议、补录完成，还是推后？

## 为什么需要它

session 结束的那一刻，是唯一还清楚「刚才那 50 分钟到底干了什么」的时刻。
再往后半小时，人只记得「好像没干完」。所以这里做的是一道**强制对账**：
不让你含糊过去，「没做完」必须落成一个明确的动作。

## 流程（数据在哪、谁动的手）

```
keep_session.py（daemon，一直在跑）
   │  收尾线程里每秒醒一次 → postsession_tick()
   │  （**独立线程**，不等「订场那一轮」跑完 —— 见下面「为什么必须是线程」）
   │  判据：结束时刻 + 15s ≤ now < 结束时刻 + 15s + 3h
   │  （机器睡了俩小时醒来会补一次，但不会翻三天前的旧账）
   ├─→ temp/keep_session-postsession.json   pending{} 里加一条
   └─→ osascript app/warden-summon.applescript   把 warden 拉到前台
                        │
warden.py ──────────────┘
   │  每帧（自己节流 1 秒）看 pending 文件 → 起后台线程核对完成状态
   │     ├─ GET /v1/tasks（bridge）→ Focusmate 的 completedAt
   │     └─ omnifocus.py list → OmniFocus 的 completed
   ├─→ postsession.Review   逐个问没做完的任务（A~E）
   └─→ 收完 → 状态文件里 pending → done（daemon 就不会再催这一场）
```

- **准点在 daemon**：warden 是人开的 TUI，可能压根没在看这一场；
  daemon 才是一直在跑的那个。所以「15 秒」由 `postsession_tick()` 保证，
  warden 只负责「被叫起来之后把话说清楚」。
- **为什么必须是线程**：`postsession_tick()` 以前挂在 `run_once()` 之间的睡眠里，
  而一轮（浏览器开单 + 复核 + 退避重试）动不动 5~20 分钟 —— 于是「结束 15 秒内
  叫人」实际变成「结束后 25~50 分钟才叫人」（真日志：10:11 结束的那场 10:55 才叫）。
  收尾是时间敏感的事，不能排在订场后面，所以它自己一个线程（`postsession_loop`）。
- **叫人的两条路**：先 `osascript app/warden-summon.applescript`（能聚焦到具体
  iTerm 窗口）；这条在**孤儿 daemon** 里会被 TCC 顶回来（实测 `-10004 权限违例`），
  那就退到 `open -a Warden`（LaunchServices，不需要 Apple Events，由 app 用自己
  的身份去跑同一段脚本）。两条都失败才认输，日志里写清「怎么修」。
- **叫了没人来会补叫**：队列里还有 pending、而 `temp/warden-tui.json` 说 TUI 没开
  （你 `q` 退出过、或 iTerm 被关了）→ 每 60 秒再叫一次，最多 5 次
  （`postsession_retry`）。之后安静等着 —— warden 一打开就会自己把 pending 接走。
- **不往 TUI 里打字**：`summon` 默认 `--no-refresh`。以前它会往那个 iTerm session
  里敲一行 `refresh`；可那个 session 早就不跑 warden 了（你在 shell 里），
  于是 `refresh` 变成一条 shell 报错；收尾问答进行中时那句话还会被当成答案。
  warden 自己每帧看队列，不需要谁往它嘴里塞字。
- **状态文件是唯一交接**：`temp/keep_session-postsession.json`
  （`pending` = 该收还没收，`done` = 收完了；两边都读同一个文件）。
- **反复催**：同一场只在 `pending` 里出现一次；收完挪进 `done`，之后永不重催。

## 收尾按「实际」还是「计划」发问？—— 按实际

`postsession_tick()` 用 **API 里的实际场次**当槽位（`slots_from_details`），
拿不到 / 快照超过 20 分钟才退回 data.csv 的计划。

2026-10-28 的教训：计划 16:15 一场 75min，实际订的是 16:00 25 + 16:30 50。
按计划问，只会在 17:30 问一次（前一段干完时没人问）；按实际问，16:25 和 17:20
各问一次，任务转移才落得下来。同理，**计划外**的场次（你手动加的）也会被问到。

## 转移之后：把 Focusmate 那条 task 删掉

转移（A 延期 / E 快捷推后）的意思是「这一场不干了」：旧记录标放弃 + 标签
「未完成转移」、复制品挂到下一场。可 Focusmate 那边那条 task 还挂在**旧场次**
上 —— 它既不会完成，又一直躺在 Task Manager 里，第二天对账还会以为它在等你。
所以 `_finish_transfer()` 会调 bridge 的 `delete_task`（`DELETE /v1/tasks/{id}`，
再 `GET /v1/tasks` 复核「真的没了」），删完把本地 `focusmate.task_id` 挪进
`previous`（`task_store.forget_focusmate()`）。删失败只报告，不挡转移本身。

## 五个选项（用户原话 → 动作）

| 键 | 名字 | 本地 | Focusmate | OmniFocus |
|---|---|---|---|---|
| **A** | 延期（自己转移） | 旧记录 → 放弃 + 标签「未完成转移」 | 由用户敲的命令决定 | **一个字节都不动** |
| **B** | 放弃 | 放弃 | 不动 | **放弃** |
| **C** | 重议 | 复制一条 + 旧的放弃 | 不动 | **不动** |
| **D** | 缺漏完成补充 | 完成 + 手填实际分钟 + 归档 | 勾完成 | **完成 + 截止时间写回** |
| **E** | 快捷推后 | 复制品挂下一场 + 旧的放弃 + 标签 | 复制品挂上 | **不动** |

另外：`s` 跳过（这条留着，下一场收尾再问）、`q` 结束收尾、`?` 说明。

六个动作（含跳过）都会给任务留一笔**「这一天这一场处理过它」**的痕迹
（`TaskStore.mark_processed` → `task["history"]`，键是 day + start + action）。
契约视图下面那块任务面板按这条痕迹按天取值：今天 = 没做完的 + 今天处理过的，
过去那天固化 = 只剩当时处理过的（`Warden.task_panel_tasks()`）。

面板本身画在 `repl_kit` 的**打断栏位**里（`ReplShell(interrogate=...)`），
位置是「整块看板下面、视图栏/提示行上面」—— 换视图、表格变长、终端太矮裁屏
都冲不掉它。

### A 延期：为什么要「等你敲一条命令」

原话：「让用户自己使用命令转移任务，用户输入完成转移并 duplicate 命令之后，
把在那个 session 的未完成任务转化为放弃状态，打 tag：『未完成转移』」。

所以 A 不是「系统替你搬家」，而是**进入一个等你敲命令的状态**：

```
⏱ 收尾 · 2026-10-27 11:00–11:50 收尾
任务 3：写论文
  · session 2026-10-27 11:00（截止 2026-10-27T11:50:00）· Focusmate 42cabb17… · OmniFocus kc-4VLAE_1L
  A  延期   你自己敲命令把任务转移走
  ...
第 1/1 条 · 把转移命令敲出来（例如 `task 3 duplicate -a 5`）；敲完这条旧任务会自动
标成放弃 + 「未完成转移」。敲 `b` 直接放弃、`s` 跳过。
```

这一段里 TUI 照常派发你敲的**任何**命令（`task 3 duplicate -a 5`、
`task 3 -a 5`、`task 3 modofy …` 都行），派发完回头判一次**转移到底发生没发生**：

- 出现了 `duplicate_of == 旧任务` 的新记录，或者旧任务的 session 被挪到别处 → 转移成立
  → 旧记录放弃 + 标签「未完成转移」，收尾继续问下一条；
- 没有 → 一律不动，提示「还没看到转移」。

判据是**结果**，不是「你敲了什么」，所以敲 `task 3 modify due 明天` 不会误判成转移。

结果就是需求里那句：「这会创建两个本地的任务，对应一样的 omnifocus 任务，
但是在不同的 session，同时一个之前 session 的任务被放弃」。

### E 快捷推后：和 A 一样，但不用你动手

下一场 = `data.csv` 里这场之后最近的那个 slot（同一天往后找，找不到就跨天）。
复制品由系统建好、直接挂上去（Focusmate 上另建一条 task），旧的那条同样
放弃 + 「未完成转移」。

### D 缺漏完成补充：为什么问分钟数

活干完了但忘了勾 —— 这时候唯一值钱的信息是「大概花了多久」。填个整数就行
（`35` / `35min` / `大概 35 分钟` 都认），然后本地完成 + 归档
（`manual: true` + `actual_minutes`），Focusmate 勾上，OmniFocus 完成并把截止时间
写回这一场的终点。

## 主动查账：`inspect-due`

自动催只盯「刚结束的那一场」。**过期了却压根没挂过场次**（或者那一场早就翻篇了）
的任务，得有一条主动入口 —— 就是 `inspect-due`：

```
inspect-due          所有过了截止时间、还没做完的任务
inspect-due 60       未来 60 分钟内要到的也算（提前看一眼）
inspect-due --all    不按截止时间筛：所有没做完的（有截止的排前面，没截止的最后）
```

- 走的是**同一套状态机**（A 延期 / B 放弃 / C 重议 / D 缺漏完成 / E 快捷推后），
  所以不会有第二套语义。面板标题变成「过期未完成任务」，每条任务多一行
  `截止 2026-10-27T10:00:00，已过期 2 小时`。
- 判据（`postsession.due_candidates()`，纯逻辑）：
  - 只看 `status == active` 的；
  - 截止时间用 `postsession.effective_due()`：`fields.due` 优先，**没有再退回「所挂
    session 的终点」**。这一层是补历史数据用的：`fields.due` 只在 `task assign`
    那一刻才写进去，更早的任务（老 JSON、只从 Focusmate attach 过的）根本没有这个
    字段，只认 `fields.due` 会把它们**整批漏掉** —— 真出现过「12 条任务、几场早就
    结束，`inspect-due` 却说『账是干净的』」；
  - `fields.due` 是 ISO（`2026-10-27T10:00:00`）就按时刻比；
    **只给日期**（`2026-10-27`）按当天 23:59 算 —— 「今天到期」不该在零点就判过期；
  - 认不出的写法（`due=明天`）不算过期，只有 `--all` 才带上。
- **结束时不写 postsession 状态文件**：这里没有「那一场」可以翻篇。
  任务本身已经被改成 `completed` / `dropped`（跳过时保持 `active`），
  下次 `inspect-due` 自然不会再问一遍。
- 过期任务会出现在**状态框的警告里**（「N 条任务过了截止时间还没做完」+
  提示敲 `inspect-due`），否则这条命令没人会知道。

## 谁来判断「该收尾了」：daemon 是闹钟，不是唯一入口

daemon 结束 +15 秒把 warden 拉到前台，写的是 `temp/keep_session-postsession.json`。
但**那个文件里有没有东西，不该决定这一场收不收**：

- daemon 没在跑 / 队列文件丢了 / warden 是事后才打开的 —— 这几种情况下队列永远是
  空的，于是「一场结束了、任务还没收尾」永远不会被问到，完成状态也就永远落不了库。
- 所以 warden 每帧先看 daemon 的队列，**队列空就自己按 `data.csv` + 任务库算一遍**
  （`postsession.unreviewed_sessions()`：结束满 15 秒 … 3 小时内 + 这一场确实还有
  `active` 任务 + 不在状态文件的 `done` 里）。算出来就照常起核对 / 弹面板，
  Recent 里会说明「是本地自己算的」。
- 3 小时以外的旧账不走自动催（那会天天翻旧账），交给 `inspect-due`。

## 手动开始一场收尾

自动收尾被关掉（`set postsession off`）、或者刚才那场没催上时：

```
review              # 最近一场结束的
review 11:00        # 今天 11:00 那场
review 2026-10-27T11:00
```

走的是同一套流程（同一个 `postsession.Review`），不会出现两套语义。

收尾进行中时 `refresh` 也会把这一轮手里那份 `tasks` 重读一遍（`Review.reload()`）：
你在别处改了某条任务的状态，面板上立刻是新的，不用收尾完再看。重读**不动** `index`
—— 刷一下就把人问回第一条，那是帮倒忙。

## 边界（别在这里加东西）

- 这里**不做双向同步**：只有「warden 说的算」和「收尾时读一次 OmniFocus」。
- 收尾**不删任务**：放弃 / 转移都是改状态（`status`）+ 打标签，记录留着可查
  （「完成/放弃的任务不隐藏」是需求第 1 条）。
- 状态记号在界面上一律可见：`·` 进行中 / `✓` 完成 / `✗` 放弃 / `⤼` 转移走
  （`warden.task_mark`）。「什么都不画」等于没说 —— 分不清是「还没做」还是
  「这一格压根没状态」，所以进行中也必须打 `·`。
- 收尾**不替你取消 session、不替你订下一场**：那两件事分别归 `cancel` 和 daemon。
- `postsession.py` 是纯逻辑（不 import rich、不联网）：要碰 Focusmate/OmniFocus 的
  地方全走 `hooks`，所以测试塞假实现就能跑完五个选项。
