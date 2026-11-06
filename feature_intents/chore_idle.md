# chore_idle（琐事 Project 空闲屏）

## 一句话

把 OmniFocus 的某个项目绑成「琐事 Project」后，**没有 session 的时间**里 TUI
只显示这个项目的一条未完成任务；`m` 完成、`c` 换一条、`q` 回到正常界面。

## 绑定：设置项 `chore-project`

```text
project                       # 先看项目库序号
set chore-project #1          # 也认 id:3 / OmniFocus 项目 pk / 唯一的项目名
```

- 值是单纯的文本设置，存在 `data/settings.json`；
- 项目库来自 `task import` 导入的 OmniFocus Project（见 `project.md`）；
- **没绑定、绑定失效、项目里没有未完成项** → 状态框的「琐事」警告会明确说
  下一步敲什么。空设置是正常状态，但会提醒用户这个功能还没启用。

## 什么时候出现

- 当前没有正在进行的 session（和专注视图同一套判定：权威数据只认有 session id
  且时间覆盖现在的场次；离线/快照才退回 `data.csv` 的计划窗口）；
- 不在收尾、到点没开始、plan、项目对账、导入问答、罪案锁死这些打断态；
- 没有 session 时无论当前在哪个视图都会接管；`q` 回到进入前的视图；
- `q` 退出后压制到**下一场 session 开始**（没有下一场就压到明天零点），同一段
  空档不再自动弹回；下一段空档会重新出现。
- 正在进行的 session 开始时，空闲屏自动退出、回到进入前的正常视图。

## 屏幕上有什么

只有：

1. 当前琐事任务标题，以及项目名 / 时长 / count / 截止时间（有才显示）；
2. 操作提示：`m 完成`、`c 换一个 task`、`q 退出（回到正常界面）`。

正常看板、Recent、状态框、视图栏、警告板都不画。任务来自项目库里
`remaining_tasks` 的未完成叶子任务；父壳若有子任务，会展开成一条条子任务来轮换。

## 完成语义

- `m` 在项目库里把当前任务标完成；若是子任务且所有兄弟都完成了，父壳自动完成；
- 和普通任务完成一样，`omnifocus` 设置开着时通过后台队列写回 OmniFocus
  （子任务完成写子任务；顺带自动完成的父壳也写）；
- `omnifocus` 关着就只改本地项目库，并在空闲屏的反馈里说明；
- 完成后当前列表自动前进到下一条，不会自动退出空闲屏。

## 代码位置

- `project_store.ProjectStore.find`：设置值 → 项目；
- `project_store.ProjectStore.complete_task`：项目任务完成 + 父壳自动完成；
- `warden.ChoreView`：整屏渲染与 m/c/q 提示；
- `warden.Warden._maybe_sync_chore_mode`：自动进出空闲屏；
- `warden.Warden._handle_chore_input`：m/c/q 输入分流（`q` 不落到通用 quit）。

## 测试

- `test/test_project.py`：`find` / `complete_task` / 自动进入空闲屏 / `c` 换任务 /
  `m` 完成 / 屏幕不含正常看板 / `q` 回契约并压制；
- `test/test_settings_ui.py`：`chore-project` 设置项存在、接受文本值、未绑定时
  状态框有「琐事 Project」警告。
