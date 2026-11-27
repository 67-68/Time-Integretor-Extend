# ui_prefs（显示偏好的落盘口径）

## 一句话

warden 里每一个**显示类**修改（close conditions / open 时段类型 / close 列）
改完就写进 `data/warden-ui.json`，下次启动第一帧照样按它画；`ui` 看记了什么，
`ui reset` 一键清空。**只有一份状态**，不再分「永久 / 本次运行」。

## 用户原话

2026-11-27：

> 添加一个功能：用户对于 GUI 的修改（eg. close conditions）真实被记录持久化，
> 之后每次的加载使用他们来修改显示状态

## 为什么改（改之前的样子）

原来每个显示动作都有**两套账**：`permanent_*`（落盘）和 `session_*`（只活这一
进程），`effective_*()` 在四个集合上做并集差集；而落盘只在命令带 `--permanent`
时发生。实际结果是：`data/warden-ui.json` 从来没被创建过 —— 用户敲的每一次
`close conditions` 都在退出时蒸发。这不是 bug，是设计（「默认不落盘，防误操作」）
选错了默认值。

## 落盘

| 项 | 位置 |
|---|---|
| 文件 | `data/warden-ui.json`（`ui_prefs_path()` = `dirname(TI_SETTINGS_FILE)/warden-ui.json`，测试跟着 `TI_SETTINGS_FILE` 隔离） |
| 结构 | `{"hidden": {mode: [列名]}, "visible": {mode: [列名]}, "folded_blocks": [块 key]}` |
| 时机 | `_set_block_folded()` / `_set_column_open()` / `_set_column_closed()` 里**立即** `_save_ui_prefs()`（临时文件 + `os.replace`） |
| 读回 | `Warden.__init__` → `_load_ui_prefs()`；块 key 先按 canonical 认，旧文件里的命令别名（`tasktable`）再折算回 `task` |
| 坏文件 | 静默回退默认，不抛异常 |

生效口径：`hidden = 自动折叠 ∪ hidden − visible`（`mode` = `working` / `detail` /
`task`，三套互不干扰）。

## 命令

- `close <表头|块名>` / `open <表头|块名>` —— 改完立刻落盘；`--permanent` 仍被
  接受但已无区别（保留了参数解析，避免旧习惯报错）。
- `ui` —— 逐行列出：块折叠、每个 mode 的 close/open 列、落盘路径。
- `ui reset` —— 清空块折叠 + 全部 mode 的列折叠，并写盘。

## 边界（故意**不**持久化）

`day_offset`（选中日期）、任务搜索词、翻页位置、看板口径（working/detail）、
当前视图、数据视图的 model/filter/sort/group。判据：**观察现场 ≠ 显示偏好**。
把「昨天看到哪儿」也记住，下次打开会停在一个你已经忘了的现场，第一眼就懵。
这几项想要长期记住时再单独立项，别顺手塞进这张表。

## 测试

- `test/test_session_modes.py`：不带 `--permanent` 的 `close` 也写盘、新 warden
  读回来、`open` 也写盘。
- `test/test_task_ui.py`：块折叠落盘 / 重载 / `ui reset` 清空 / 偏好文件里确实没
  了折叠块。
- `test/test_warden.py`：改 `hidden_columns` 后 `frame_signature()` 必须变。
- 黑盒：`blackbox_kit.snapshot_workspace(真实仓库)` → 每条命令一个全新
  `warden.py --exec` 进程 → 只查 `data/warden-ui.json` 与新进程的读数。
