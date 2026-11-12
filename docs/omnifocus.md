# OmniFocus 联动（只单向：warden → OmniFocus）

> 三个文件，三层职责：
> `omnifocus_tasks.py`（osascript 小工具，unix 风格）→ `omnifocus.py`（warden 侧薄封装 + 链接解析）
> → `warden.py`（什么时候写、写什么）。**没有反向同步**，见最后一节。

## 为什么是单向

用户的原话：「目前的原则就是不做完整的双向同步，仅做单向的覆盖（除了我手动输入任务）」。

于是边界划得很死：

| 方向 | 谁说了算 |
|---|---|
| 完成 / 放弃 一个任务 | **warden**（本地账本是主）→ 写回 OmniFocus |
| 任务的截止时间 | **warden**（= 它被分配的那场 session 的终点）→ 写回 OmniFocus |
| 「活还在不在、叫什么、在哪条路径上」 | **OmniFocus**（本地从不改这些字段） |
| 用户在 OmniFocus 里手动勾完成 | 收尾核对时**读**一次，本地跟着标完成（不反向改别的东西） |

所以「同一条 OmniFocus 任务在本地可以有多条记录」是**设计**，不是重复：
一条旧场次的记录（已放弃 + 标签「未完成转移」）+ 一条新场次的记录（还在干）。
OmniFocus 那边始终只有一条。

## 1. `omnifocus_tasks.py`：osascript 小工具

Unix 风格：**stdout 只有数据、stderr 只有诊断**，退出码有意义。

```bash
# 读：默认 JSONL 全部任务（走 Omni Automation 快路径，1380 条约 1 秒）
python3 omnifocus_tasks.py
python3 omnifocus_tasks.py --format tsv --header --fields id,name,due,completed
python3 omnifocus_tasks.py | jq -r 'select(.kind != "project_root" and .flagged) | .name'

# 写：按 primary key 精确改一条，默认只预览（退出码 4），--yes 才写
python3 omnifocus_tasks.py set --id <PK> --complete
python3 omnifocus_tasks.py set --id <PK> --complete --due 2026-10-27T11:50 --yes
python3 omnifocus_tasks.py set --id <PK> --drop --yes
python3 omnifocus_tasks.py set --id <PK> --add-tag 写作 --yes
```

退出码：`0` 成功 / `1` 失败 / `2` 参数错 / `3` **自动化权限被拒（TCC）** /
`4` 预览未写 / `5` 任务不存在 / `6` 标签问题 / `7` 拒绝改 project_root。

### 自动化权限（TCC）：授权对象是「跑 osascript 的那个 App」

这条踩过两次，记清楚：

- OmniFocus 是沙盒 App，任何进程给它发 Apple 事件都要 TCC 的「自动化」授权；
- 授权对象是**责任进程**，也就是把 warden / osascript 拉起来的那个终端 App。
  warden 的 TUI 是 `warden-summon.applescript` 在 **iTerm2** 里开的，所以该勾的是
  「自动化 → iTerm2 → OmniFocus」。**勾「终端」不等于给了** —— 名字像而已；
- `Warden.app` 只需要「控制 iTerm2」那一项（它只管把窗口叫到前台），跟
  OmniFocus 权限无关；
- 被拒时的表现是**秒回**而不是卡住：`-10004`（errAEPrivilegeError）或 `-1743`，
  0.2 秒就退。秒回说明这条拒绝**已经被记住**，等不到弹框 —— 要重新弹框得先
  `tccutil reset AppleEvents <bundle id>`（iTerm2 是 `com.googlecode.iterm2`）；
- 判断是不是权限问题，别只看某一条命令：`osascript -e 'tell application "OmniFocus"
  to get name of front document'` 这种最简单的读也会一起失败，那就是整条
  Apple 事件通道被拒，跟脚本内容无关；
- **沙箱里的 -10004 不是 TCC**。seatbelt / App Sandbox 会直接拦下 Apple 事件，
  报的同样是 errAEPrivilegeError。2026-11 踩过：AI agent 的 bash 沙箱里跑
  `omnifocus_tasks.py` 得到 -10004，差点误判成「用户没授权」，其实 iTerm2 早
  就给了 —— 换一个没沙箱的终端一跑就通。所以先问「这条命令是谁拉起来的」。

### 为什么失败账里的「每日检查」永远清不掉（已修）

`_note_success` 用 `name + args` 认「同一条」，而每日检查的 args 里带的是**当时
的项目快照**，每次都不一样 → 成功一次也只会清掉参数完全相同的那条，旧账留在
面板上，看起来像「每天都在失败」。现在这类动作标成 `coalesce`：账只留一条，
成功一次就清掉整类（见 `feature_intents/network.md`）。

`run_osascript` 会把这三件事写进 stderr（**最后一行**是「下一步做什么」，
warden 的失败账只显示最后一行）：

```
macOS 拒绝了自动化权限（TCC：这个进程不许向 OmniFocus 发 Apple 事件；-10004 / -1743）。
  原始错误：…
  要授权的是「跑 warden 的那个 App」，不是「终端」这个泛称…
  还是不行就清掉旧决定再跑一次，让它重新弹框：tccutil reset AppleEvents com.googlecode.iterm2
  下一步：系统设置 → 隐私与安全性 → 自动化 → 勾上「iTerm2」里的 OmniFocus
```

终端名从 `TERM_PROGRAM` 认（iTerm.app / Apple_Terminal / vscode / Warp / WezTerm /
Ghostty / Hyper / Tabby），认不出来就只说「给跑 warden 的终端」。


- `--due` 是**修饰**，不是独占操作：`--complete --due …` 一条命令跑完
  （收尾「缺漏完成补充」就是靠这个把「完成 + 截止时间」一起写回去）。
- 标签操作和状态/截止时间操作**不能混在一条命令里**（要分两次跑）。
- `kind` 字段区分 `action` / `inbox` / `project_root`；`project_root` 是「项目自身」的
  根任务，筛任务一般要排掉。
- `completed` / `dropped` 是任务**自己的**标记；`effective_completed` /
  `effective_dropped` 是 OmniFocus 计算的**有效状态**。父任务完成或放弃后，
  子任务可能自己的 `completed=false`，但 `taskStatus` / `effective*` 已经是
  Completed / Dropped。项目导入用有效状态判断任务能不能排、要不要检查时长。

### 读得快一点：`--with-repetition`

`list` 默认不带重复规则。对账/导入要判断「这条任务会不会重复产生」时，老做法是
每条活跃任务再起一个 `osascript` 查 `repetitionInfo`（0.26s/条）。现在：

```bash
python3 omnifocus_tasks.py --with-repetition      # 同一次遍历顺手带回来
```

多出来的字段（`repetition_rule` / `_method` / `_schedule_type` /
`_anchor_date_key` / `_catch_up` / `_next`）只在这条命令里出现，TSV 的列不变。
`omnifocus.Client.repetition_info()` 先查缓存行，缓存没带才退回单条查询。
warden 的项目对账走的就是这条路：12.5s → 5.1s（28 条活跃任务实测）。

### 读取：全量 + 增量（`changes` 子命令）

OmniFocus **没有**动作流水 / change feed；但它有 `modificationDate`，而且 JXA 的
`whose({modificationDate: {_greaterThanEquals: since}})` 是**下推**给 OmniFocus
自己执行的谓词 —— 不用把 1715 条任务逐条搬进 JS。OmniJS 不暴露这个查询（只能
逐条遍历，1715 条空转就要 2.9s），所以变化查询走 JXA，补读走的还是 OmniJS。

```bash
python3 omnifocus_tasks.py changes --since "2026-11-12T08:00:00"   # 一行一条 JSONL
python3 omnifocus_tasks.py list --ids '["pk1","pk2"]' --with-repetition
```

实测（1715 条库）：

| 操作 | 耗时 |
|---|---|
| 全量 `list`（含重复规则） | 4.8–6.2s |
| JXA `whose` 1 小时窗口（2 条命中） | 0.04s |
| JXA `whose` 24 小时窗口（23 条） | 0.23s |
| 按 id 批量补读 200 条整行 | 0.69s（3.4ms/条） |
| **增量一整轮（查询 + 补读）** | **0.25s** |

调度规则（`Client._read_locked`）：

1. 没有快照 / 距上次全量超过 `INCREMENTAL_WINDOW`（默认 600s）→ **全量**；
2. 否则问 `changes`：命中的任务 id 少于 `INCREMENTAL_MAX`（默认 200）且没有项目
   变化 → 只补读这些行，合并进快照；否则全量；
3. `changes` 自己失败（权限、谓词被拒）→ 全量，**不拿半截结果冒充「没变化」**；
4. 补读时保持快照原有的 `--with-repetition` 形状，别让同一份快照一半有重复字段。

**删除只有全量能发现**（Omni Group 自己的 MCP ADR-0026 也是这么写的），所以
「超过 10 分钟没读过 → 全量」既是性能保护（`whose` 的代价随命中数线性涨，攒太久
的变化查询自己就要十几秒），也顺手把定期全量对账做到了 10 分钟级。

> 为什么不用数据库文件 mtime 做缓存键：它只在真写时变，但「你正在用软件」时
> OmniFocus 一直在被改（你自己 + 同步），缓存几乎总失效；反过来空闲时它不动，
> 可那时也没人需要读。所以走 `whose` 查询，不用文件指纹。

## 2. `omnifocus.py`：链接解析 + 子进程

### 认得出哪些粘贴形状

在 OmniFocus 里「拷贝任务」拿到的剪贴板里，同一条任务有好几种长相：

```
[数学易错修正](omnifocus:///task/kc-4VLAE_1L)   ← Obsidian / Markdown
<omnifocus:///task/kc-4VLAE_1L>                  ← 纯文本自动链接
omnifocus:///task/kc-4VLAE_1L                    ← 裸链接
title: "数学易错修正"                             ← 只有标题（**没有** primary key）
```

前三种都能拿到 **primary key**（`kc-4VLAE_1L`），也就是 OmniFocus 里的任务身份；
第四种只有标题，认不出身份 —— 那就**不假装**关联上了（`has_id == False`）。
拿标题去猜一条 OmniFocus 任务比不关联更糟：猜错就是改错东西。

### 剪贴板：为什么粘贴会丢链接，怎么才能不丢

**⌘C 一条任务 → 终端里 ⌘V，粘出来只有标题。** 终端只接受剪贴板里
`string` / `utf8` 那个 flavor，`omnifocus:///task/PK` 在**粘贴那一刻**就丢了。
所以「把粘贴的内容交给 import」这条路注定没有 primary key。

而且 `pbpaste -Prefer html` **在这台 macOS 上完全不起作用**（实测 macOS 15.5：
传 `html` / `rtf` / `url` 都原样返回纯文本）—— 老实现其实一直在读纯文本，
链接从来没读到过。要拿链接只能**按 flavor 点名**用 osascript 读，返回的是
hex 包装：

```bash
osascript -e 'the clipboard as URL'             # «data url 6F6D6E69…»  ← 链接本体
osascript -e 'the clipboard as «class HTML»'    # «data HTML 3C68746D…»  ← 多选+标题
osascript -e 'the clipboard as «class RTF »'    # «data RTF 7B5C7274…»
osascript -e 'clipboard info'                   # 有哪些 flavor（排障）
pbpaste                                          # 纯文本（常常只有标题）
```

`omnifocus.clipboard_links()` 四个 flavor 全读、按 id 合并、标题优先取 HTML 锚文本：

| flavor | 里面是什么 | 用途 |
|---|---|---|
| `URL` | `omnifocus:///task/PK` | 最可靠的 id 来源（单条任务就是它） |
| `HTML` | `<a href="omnifocus:///task/PK">标题</a>` | 多选时的全部 + 好标题 |
| `RTF` | `{\field{\*\fldinst{HYPERLINK "omnifocus:///task/PK"}}{\fldrslt 标题}}` | 同上，兜底 |
| 纯文本 | 常常只有标题 | 只有「一条有 id 没标题 + 一条只有标题」时才配对成一条 |

```bash
python3 omnifocus.py parse '[数学易错修正](omnifocus:///task/kc-4VLAE_1L)'
python3 omnifocus.py links        # 直接看剪贴板里认出了什么
python3 omnifocus.py get kc-4VLAE_1L
```

TUI 里的用法：`task import`（不给参数 = 读剪贴板导入并自动关联）、`task 5 link`
（不给链接 = 读剪贴板关联到已有任务）。规矩是「动作归子命令，后缀归分流」——
`-a N` 只是 `--assign N`（顺手挂到表格 #N），不兼职「读剪贴板」。
粘进来一段只有标题的文字时，会去剪贴板对一下**同名**链接（标题完全相同才认，
不是猜），对不上就明确说「没有关联」。

### 子进程而不是 import

写操作可能因为 TCC 弹框卡几十秒，甚至把 OmniFocus 抢到前台；放在子进程里，
超时/被杀只影响这一次调用，TUI 不会被拖进去。`Client` 顺便带 20 秒缓存
（一次收尾会连着问好几次「这条完成了没」）。

### 写回：本地优先 + 批量 flush（`temp/omnifocus-outbox.json`）

用户原话：「本地修改状态（之后 refresh 的时候更新，包括 focusmate 任务。比如说
空闲 90s 自动 refresh）」+「空闲的时候（刷新完成之后另外一次）间隔 600s」。

所以 `task done` / `task drop` **不再当场起 osascript**：

1. 只往 `temp/omnifocus-outbox.json` 记一笔意图（`{id, op, task_id, due, at}`，
   同一 `(op, pk)` 只留最新一次）；
2. 空闲 **≥90s** 时 flush 一次，之后仍空闲每 **600s** 一次；`refresh`、`task sync`、
   退出、下次启动也会 flush（崩溃不丢：outbox 在盘上）；
3. flush 把攒的账**合成一次** osascript（`python3 omnifocus_tasks.py batch`，ops 走
   stdin）：N 条 = 1 个进程，而不是 N 个；
4. 写成功 → 落 `writeback.completed/dropped` + 重复任务按 `repeat_clone_id` 改挂，
   并清掉 outbox 那几条；写失败 → **留在 outbox**（本地不回滚 —— 用户看到的 ✓ 就是
   真的），下一轮再写。

`jobs.sync`（`--exec` / 测试）没有「之后」，仍然当场写，行为不变。

读空闲用 Quartz 的 `CGEventSourceSecondsSinceLastEventType`（~0.2ms），不要在每帧
里去起 `ioreg` 子进程（40–80ms，会卡帧）。

## 3. warden：什么时候写、写什么

| 动作 | 本地 | Focusmate | OmniFocus |
|---|---|---|---|
| `task <id> done` | 完成 + 归档 | `complete_task` 勾上 | **完成 + 截止时间写回** |
| `task <id> drop` | 放弃 | 不动 | **放弃** |
| `task <id> drop --local` | 放弃 | 不动 | 不动 |
| 收尾 A 延期 | 旧记录放弃 + 标签「未完成转移」 | 用户自己敲的命令决定 | **不动** |
| 收尾 B 放弃 | 放弃 | 不动 | **放弃** |
| 收尾 C 重议 | 复制一条 + 旧的放弃 | 不动 | **不动** |
| 收尾 D 缺漏完成补充 | 完成（手填分钟） | 勾上 | **完成 + 截止时间写回** |
| 收尾 E 快捷推后 | 复制品挂下一场 + 旧的放弃 | 复制品挂上 | **不动** |

**截止时间 = 那场 session 的终点**（11:00–11:50 → `2026-10-27T11:50:00`）。
`task_store.assign()` 里写（覆盖原值，原值留在 `session.previous_due`），
`writeback_completions()` 在完成时把它一起写回 OmniFocus —— 于是 OmniFocus 里
看到的就是「这一场几点结束的」。

写回是**幂等**的：写过的记在 `task.omnifocus.writeback.completed` 上，重复同步不会重复写。

触发写回的地方（**只要知道一条任务完成了就会写**）：

1. `task done` / 收尾 D（直接完成）；
2. `task sync`（拉 Focusmate 完成状态后补写）；
3. 收尾自动核对（session 结束后）；
4. **任何一次同步完成状态的 `refresh`**（`sync_task_completions` 末尾，含裸 `refresh`
   和 `refresh --check`）。`refresh` 仍然**不在外面新造东西**（不订场、不在
   Focusmate 建 task、不改 data.csv），但「Focusmate 勾掉了」是一条已经发生的
   事实，本地打 ✓ 的同时 OmniFocus 那条也得完成 —— 否则本地一 ✓，收尾就再也
   不会问它，**没有任何命令会再想起这条任务**（真实踩过：用户只能人肉 `task sync`）。

写回扫的是「本地 `completed` + 有 primary key + `writeback.completed` 还没记」，
所以补写是幂等的；一次 sync 里没有这种任务就**一个子进程都不起**。

同一轮扫描按 **OmniFocus primary key 去重**：项目延期、拆 part 或历史转移可能在
本地留下多条共享同一个 key 的已完成记录，但一个 key 只代表一次 occurrence，只能
complete 一次。写回成功后会把这个 key 下所有已完成兄弟记录一起记上
`writeback.completed`，不会下一次同步又各写一遍。项目任务还只完成了原任务的一部分
count 时，后台写回扫描和 `omni_complete()` 一样走 `completion_guard()`，不勾
OmniFocus 完成。

### 重复任务：OmniFocus 那边会「翻到下一次」

`omnifocus_tasks.py set --complete` 打在**重复任务**上时，OmniFocus 的行为是
「这一次完成 + 立刻生出下一次」，返回的 `after` 是**新那一次**的快照
（`completed: false`），新 primary key 在 `repeat_clone_id` 里。

于是 warden 必须做两件事，否则就出两个真事：

| 不做 | 后果 |
|---|---|
| 把 `after.completed == false` 当失败 | 报「写回失败」、不记 writeback → 每次同步再写一遍 → OmniFocus 的重复表被一次次往前推 |
| 不把新 key 接过来 | 本地还开着的记录（本体 / 明天的子任务）全挂在**旧 key**（已完成的那次）上 → 下一次的活在 OmniFocus 里永远勾不掉，还会被「OmniFocus 已完成」带成假完成 |

所以**所有写回完成的路**（`omni_complete()`、Focusmate 同步后的
`writeback_completions()` 批量写回、收尾自动核对）看见 `repeat_clone_id` 都要：
`repeat_advanced = true`（日志里能看到）、照常记 writeback、并调
`task_store.advance_omnifocus(old_pk, new_pk)` 把**还开着的**本地记录改挂到新 key
（旧 key 留在 `omnifocus.previous` 里备查；已经收工/放弃的记录留在旧 key 上，
它们说的就是那一次已经结束的活）。否则同一个 key 被重复 complete 会把重复表多推
一周；这也是「本来每周一次，完成之后却下下周才有」的根因。

`task <本体> drop` 会带 `--all-occurrences`：放弃「这一套」，而不是只放弃这一次
（否则 OmniFocus 会照着重复规则继续生出下一次）。

### 反向那一次「读」的边界

「用户在 OmniFocus 里手动勾完成 → 本地跟着标完成」只在**这条完成还没有归属**时做。
同一条 OmniFocus 在本地可能挂着好几条记录（转移的旧记录、重复任务的子任务）：
其中一条要是我们自己写回完成的（`writeback.completed` 有值），就不再拿它去把
同一个 key 上**还开着**的兄弟记录也带成 ✓（否则今天的子任务写完回，明天的
子任务直接被判成完成）。

## 4. 收尾时「读」一次

> ⚠️ Focusmate 的 `GET /v1/tasks` **只返回未完成的 task**；勾完的会从列表消失，
> 只在 `GET /v1/tasks/completed` 里（可带 cursor 分页）。所以 bridge 的 `tasks`
> 必须两边都拉、按 id 合并 —— 只拉 `/tasks` 时本地永远等不到 `completedAt`，
> 也就不会触发 OmniFocus 写回。

`Warden.sync_completion_state()`（收尾入口调它）：
1. `GET /v1/tasks` + `GET /v1/tasks/completed`（bridge）→ `task_store.reconcile()`
   → Focusmate 的 `completedAt` 进归档，**并且把本地 `status` 落成 `completed`**
   （以前只归档不改状态，靠读文件时的老数据兼容规则兜，于是界面上「有完成却看不见 ✓」，
   用户反馈过）；
2. `omnifocus.py list` **一次** → 哪条 OmniFocus 任务已经 `completed`，本地跟着标完成；
3. `writeback_completions()` 把「本地完成但还没写回」的补写回去。

OmniFocus 读不到（权限没给 / 没装）只变成一句警告：收尾照常进行，
只是少了「OmniFocus 那边也勾了」这条信息。

## 5. 运行日志（按天）

Focusmate 拉 task、Focusmate 勾完成、OmniFocus 读写都会写 JSONL：

```text
temp/logs/YYYY-MM-DD.jsonl
```

一行一个事件，关键事件：

| `area` | `event` | 里面有什么 |
|---|---|---|
| `focusmate` | `tasks_pull_page` | 本次请求的端点、HTTP 状态、任务数、`hasMore`、这一页的每条 task（id/title/completed/completedAt/attachments） |
| `focusmate` | `tasks_pull_done` | `/tasks` 与 `/tasks/completed` 各多少条、合并后总数、耗时、合并后的任务快照 |
| `focusmate` | `task_complete_start/result/error` | 勾哪条 task、尝试过哪些端点、复核到的 completedAt、错误 |
| `omnifocus` | `status_pull_start/result/error` | 读 OmniFocus 全部任务的结果、本地关联数、已完成数、`claimed_pks` / `held_task_ids`（因为「我们自己写过回」而没有跟着标完成的兄弟记录） |
| `omnifocus` | `complete_start/result/error` | 本地任务 id / 标题 / pk / due、写前状态、`changed`、写后 `after.completed`、`repeat_clone_id` / `repeat_advanced` / `repeat_moved`（重复任务翻篇 + 改挂了几条）、错误 kind / exit code / detail |
| `omnifocus` | `writeback_scan_start/result` | 本轮补写扫到哪些本地已完成任务、写了几条 |

日志不会写 token / cookie。`TI_LOG_DIR=/path` 可以改目录（Node 桥和 Python
共用这个变量）。日志只是旁路：目录写不进去不会影响任务流程。

## 6. 权限（第一次用之前点一次）

macOS 不允许一个 app 悄悄控制另一个 app。第一次跑
`python3 omnifocus_tasks.py` 会弹「终端（或 iTerm）想要控制 OmniFocus」——
**点允许**。点错了会记住拒绝，之后不再弹，只能重置：

```bash
tccutil reset AppleEvents
# 或 系统设置 → 隐私与安全性 → 自动化 → 勾上你的终端 → OmniFocus
```

被拒时 `omnifocus_tasks.py` 退出码是 **3**，warden 里会显示
「自动化权限被拒」而不是一句「失败」——看到这一句就别再试了，去点开关。

## 7. 关掉它

```
set omnifocus off
```

一个字节都不碰（不读也不写），收尾照常问你 A~E，只是「本地说改了什么」那一半还在。
