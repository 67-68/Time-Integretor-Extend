# network —— 外部操作（Focusmate / OmniFocus / 子进程）：读异步、写乐观、失败可重放

warden 里**所有不在软件内部自己完成的事**都不在提示符线程上跑：Focusmate
（bridge 子进程 + 浏览器）、OmniFocus（osascript 子进程）、macOS 剪贴板
（osascript / pbpaste）、keep_session（下单 / 对账 / 启停 daemon）、日历同步
（download_calendar.py）。这一层由 `repl_kit.jobs` 提供（单 worker 串行队列 +
进度面板 + 失败账），warden 只负责“哪件事叫什么名字、参数是什么、本地那半件事
怎么做、失败怎么退回去”。

## 为什么

- **提示符线程不能等网络**。浏览器冷启动十几秒，等它的时候 TUI 和卡死没区别：
  敲什么都没反应，你分不清是慢还是崩。
- **写操作要立刻有反应**。把任务挂到 Focusmate、静音一场、改标题 —— 这些
  “点了就该生效”的动作不该让人盯着 `console.status` 转圈。
- **失败必须留下痕迹**。断网、登录态过期、复核不一致都会失败；失败之后
  总得有人记得“这件事没做成”，否则账本里就是一条假事实。

## 一次操作 = 一个标题 + 编号步骤

一条命令如果要把两个世界都动一遍，面板上**先给操作名字，再给步骤**：

```
╭─ ⏳ import 任务（task import -a 1） 1/2 ━━━━╸────── 50% ────────╮
│ · 步骤 1：[omnifocus] 获取任务（OmniFocus）…                    │
│ ✓ 步骤 1：[omnifocus] 拿到 1 条                                 │
│ · 步骤 2：[focusmate] 挂任务 #3 到 11-03 11:00…                 │
╰────────────────────────────────────────────────────────────────╯
```

* `runner.begin(2, title="…")` 声明这一次操作叫什么、几步；
* 每一步是 `Action(..., source="omnifocus" | "focusmate" | "keep_session" | "calendar")`，
  日志行自动加上 `步骤 N：[source] ` 前缀；
* 步骤可以在**上一步的成功回调里**再排（`import` 的第 2 步就是第 1 步 apply 里
  排进去的），它会并进同一批 —— 分母变大，标题不变；
* 干完标题就没了，面板消失，结果进 Recent。

已经这样串起来的操作：`task import`（[omnifocus] 读剪贴板/获取任务 →
[focusmate] 挂任务）、`task 5 link`（读剪贴板 → 写本地关联）、
`task done`（[focusmate] 勾完成 → [omnifocus] 写回完成）、`task drop`、
`task sync`、formatter 切出来的旧场取消、`refresh`、
`sync`（[calendar] download → [calendar] populate）、`daemon start/stop`。

**取消 session 的所有入口**也走这条队列，不再在提示符线程上用
`console.status` 等浏览器：

- `cancel N` → `session.cancel`（乐观拿掉行，失败自动放回）
- 到点没开始敲 `x 不去了` → `session.missed`（状态落盘在后台，输入立刻进收尾）
- 认罪后复查 daemon 是否重订回来 → `session.confess_cancel`（查快照 + 取消，
  逐场更新进度；失败进失败账可 `re-action`）
- 改排布 / 临时挪动旧场 → `session.relocate_cancel`，
  随后的 `keep_session.py check` 也是 `keep_session.run` 后台动作

## 两种操作，两种待遇

    读（session 列表 / Focusmate task / 生涯统计 / OmniFocus 任务列表）
        不预测。拿到真数据才改表格；期间表格里显示的是上一次的数据，
        而“这份数据是什么时候来的”写在状态框里（见下）。

    写（挂任务 / 下架 / 勾完成 / 取消 / 静音 / 改标题 /
        OmniFocus 写回完成 / OmniFocus 放弃）
        乐观。本地先改（立刻可见），网络进后台；成功就落定真结果，
        失败就**精确回滚**到操作前的快照。

## 数据新鲜度

状态框里永远有一行：

    数据：session 12 秒前 · task 3 分钟前          ← 灰的：够新，不吵你
    数据（⚠ session 不是最近的）：session 8 分钟前 · task 1 分钟前
                                                   ← 红色加粗：别拿旧账做决定

阈值：session `FRESH_SESSION`（5 分钟）、task `FRESH_TASK`（15 分钟）。
从来没拉过就是“还没拉过”，同样是红色加粗。`state.json` 快照（不是真数据）
单独一句话，不许冒充。

## 乐观写操作的五个步骤

    1. 存快照      copy.deepcopy(任务) —— 回滚要用的就是它
    2. 改本地      task_store.assign / attach_focusmate(带 `pending` 标记) / 行级字段
    3. 入队        Action(名字, 人话标签, JSON 参数)
    4. 落地/回滚   on_ok → 填真 task id、撤掉 pending；
                   on_fail → 整体放回快照 + 警告 + 记账
    5. 记账        失败的 (名字, 参数, 原因, 时间) 写进 data/failed-actions.json

界面上的“在途”标记：任务的 focusmate 里挂一个 `pending`（任务面板上显示 ⏳），
落地就撤掉。`refresh --check` 对账时会**跳过**带 pending 的任务 —— 不能把
“还在后台跑”当成“没挂上”再挂一次。

## 失败账与 re-action

    re-action            重放全部失败操作（参数从盘上读，跨进程有效）
    re-action --list     先看清单
    re-action --clear    清空

重放走的是**名字**，不是当初那个闭包：`jobs.register("task.assign", 网络那半,
apply=本地那半)`。所以 warden 关掉再开、断网恢复之后，照样能把昨天那几条补上。
重放成功就自动消账；又失败就次数 +1，账继续留着。

失败账同时进警告板（`source=网络`），文案里直接给出下一步命令。

## 边界：哪些外部调用**不**进队列

一次改好几场的“多步事务”里，**本地写**仍然同步跑（要么都改、要么都别改），
但其中碰外面世界的子步骤已经拆进队列：

- 时段排布剪断：写 layout + 重算 data.csv 是本地同步事务；旧场的 Focusmate
  取消走 `session.cancel` 队列（本地先乐观拿掉，失败自动放回）
- formatter / 收尾要挂 Focusmate 时，走 `task.assign` 队列，本地先打 pending
- 收尾（postsession）那一串里的读写（它自己有一个后台线程，见下）
- 布局重写

理由：这类事务中途失败，回滚一半比不回滚更糟 —— 要的是“要么都改，要么都别改”，
而且用户是在一条命令里明确要求这件事，等一下是应该的。

两个**后台循环**也不走队列（它们本来就不在 UI 线程上，而且不是用户发起的一件事）：

- 收尾的核对线程（`_review_worker`）里的 OmniFocus 读
- 静音自动落地（`_mute_worker`）里的 bridge mute

bridge 自己有一把命令锁，所以和队列并发时协议不会错乱；只是**顺序**不由队列决定。

还有一类：`refresh --check` 这种“对账”命令里，读回来之后要连着修好几件事
（补订 session、补挂 task、拆独占冲突）。那条修复链是同步的 —— 它必须拿着对完账
的数据一路改下去，中途让位给队列只会让“修一半”。

## 落盘位置

    data/failed-actions.json     失败账（gitignore：每台机器自己一份；重放靠它）
    temp/logs/YYYY-MM-DD.jsonl   既有的运行日志，网络操作也往里记

`TI_FAILED_ACTIONS` 可以把失败账指到别处（测试必须指到临时目录）。
