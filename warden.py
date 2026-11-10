#!/usr/bin/env python3
"""warden.py —— Focusmate 狱卒看板（rich TUI）。

四个部分
--------
1. The Warden Status   当前 daemon 状态（Running/Stopped）+ 启停。
                       **停用 = 敲宣言**（和取消 session 共用同一道闸门；密码系统已删除）。
2. The Contract Table  某一天的表：ID | 时间 | 状态 | 静音与否 | 标题 | 契约。
                       用 `day -1 / day 0 / day 1` 切换看哪天。
3. The Interrogation   死循环 input，极简命令：
                       mute 3 / title 2 "文本" / session 09:00 50 / session 11:15-12:05
                       cancel 1 / relocate 1 -15 / day 1 / sync / force-check / stat
4. Panic Action        —— 已按你的要求删掉。没有这个功能。

视图（Tab 切换）
----------------
按 **Tab** 在多个视图间循环切换，视图显示在命令提示上面。现有六个：

1. **契约**（默认）：时段 / session / 任务表 + 命令。前面四列是日历下载层的
   时段 ID / 时段 / 类型 / 名称；同一时段只有第一行显示，后续行留空。
2. **专注**：目标 + 当前 session + 专注模式 + 可暂停次数 + 一行实时状态；
   `stop` = 消耗一次暂停，**Shift+Tab** = 循环普通 / 做题 / 写作模式。
3. **统计**：生涯累计（总场次 / 每天平均 / 空天 / 峰值 / 中位数与 p25）/ 按月 /
   最近 14 天，**底部逐行列「永不重订」名单**（verdicts 的 `abandoned`，
   平常视图不铺开）。取数与口径在 `stats.py`（只读公开 API，1 小时缓存）；
   任意视图敲 `stat` 都能切过去；`stat refresh` 立刻增量刷新（只拉缺口 + 最近几天，
   不是把两千多条重拉一遍），`stat refresh full` 才是全量重拉；缓存新鲜时一次网络都不打。
4. **任务**：本地 JSON 任务库（字段不校验）+ 挂到 session + Focusmate 完成归档
   + OmniFocus 单向联动。
   `task create/delete/modify/assign/select/repeat/duplicate/drop/done/link/import/sync`；
   `task <序号> -m complete|discard` 是**只改本地账本**的手动状态标记（不碰外部）；
   `task <序号> -t <trigger>` / `-f <formatter>` 是具名预设函数框架
   （trigger 默认为空，formatter 有 `end`：在当天最后一段的尾巴上**切一场**
   任务大小的 session、把那个时段剪断（原来那场 50 分钟消失）并自动 assign；
   `enlist` 只挂已有空场；`repeat 10:45` 把任务固定挂到点名的那一场已有
   session，挂重复任务本体时每周每个子任务都用同一场；见 `task_hooks`）；
   `task assign` 成功后才写本地，Focusmate 侧 task id / completedAt 都会记回来。
   `task <序号> -e true` 是**独占**：这一场 session 只能有它一条任务
   （`end` 排出来的自动独占；`refresh --check` 发现一场挤了两条就全部下架）。
   `--repeat 周几@HH:MM` 让任务每周重复：只在「同一星期几、同一时刻、同一时长」的
   那场 session 上落地；排布被改过（例如 50 拆成 25+25）时这一周就不落，但 repeat
   设置本身不变。
   - **状态**（`active` / `completed` / `dropped`）只影响要不要继续催，
     完成/放弃的任务**不隐藏**：表格里前面多一个 `✓` / `✗` / `⤼` 记号。
   - **截止时间**：任务挂到一场 session 上时自动 = 那场的终点；完成的时候
     把「完成 + 截止时间」一条命令写回 OmniFocus（见 docs/omnifocus.md）。
   - `task <id> duplicate [-a N]` 复制一条（同一条 OmniFocus 任务、新的本地 ID），
     `task import`（不给参数）直接**按 flavor 读剪贴板**认 `omnifocus:///task/PK`
     （⌘C 任务再 ⌘V 到终端只剩标题 —— 终端只吃纯文本 flavor，链接在粘贴那一刻
     就丢了；import 自己绕开粘贴这一层，`the clipboard as URL` 里就是链接本体）。
     分工固定：**动作归子命令**（import/link 自己读剪贴板），**后缀归分流**
     （`-a N` = `--assign N`，顺手挂到表格 #N）。
6. **静音时段**：`period mute T1 [--permanent]` = 这个时段（或以后每个这个星期几）
   的 session 自动打开 **Focusmate quiet**（「静音」只指这个）。`mute-universal
   13:00 - 15:00 Mon` 则不看 T 时段，只按星期几 + 时间窗判定，任何与窗口有重叠
   的 session 都静音。本地看门狗/通知/嘶吼一概照旧 —— daemon 里根本不读这张表。
   表在 `temp/session-mutes.json`，判定在 `session_plan.mute_window_at()` /
   `mute_overlaps_window()`，见
   [`feature_intents/session_mute.md`](feature_intents/session_mute.md)。
5. **设置**：`settings_store.SETTINGS` 里注册的全部设置项（id / 当前值 / 默认值 /
   解释），`set <id> <value>` 修改，不写 value = 恢复默认。现有六项：
   `edit-tomorrow`（今天可以先安排明天，过了凌晨回到 24 小时窗口）、
   `tomorrow-plan`（明日时间安排 = 一个每周重复任务）、`omnifocus`（和 OmniFocus
   联动，**只单向**）、`postsession`（结束 15 秒后把 warden 拉到前台收尾）、
   `nostart`（到点没开始就问一句）、`chore-project`（没有 session 时显示哪个
   OmniFocus 项目里的琐事）。
6. **琐事**（自动视图）：没有 session 时整屏只显示绑定项目的一条未完成任务；
   `m` 完成 / `c` 换一条 / `q` 回正常界面。见
   [`feature_intents/chore_idle.md`](feature_intents/chore_idle.md)。

**收尾（session 结束 +15s）**：daemon 到点把 warden 拉到前台，warden 逐个问
本场没做完的任务 —— A 延期 / B 放弃 / C 重议 / D 缺漏完成补充 / E 快捷推后。
流程在 `postsession.py`（纯逻辑）+ 这里的 `_maybe_start_review` / `begin_review`；
细节见 [`feature_intents/postsession.md`](feature_intents/postsession.md)。

**`inspect-due`（主动查账）**：不看「哪一场」，而是把**过期还没做完**的任务
（`inspect-due 60` = 未来 60 分钟内到期的也算，`--all` = 不按截止时间筛）
拉进同一套状态机过一遍。过期任务还会出现在状态框的警告里，提示敲这条命令。

**项目 + `plan`（资源 → 任务 → session）**：`task import` 认出剪贴板里是 OmniFocus
**项目**时，把它的任务清单存进项目库（`data/projects.json`），**不建本地任务**；
契约视图的任务面板下面多一张项目表（剩余 count / 进度 / 今天排了多少）。
每个真正项目只显示当前阶段对应的那一个 op（running → SOP / bottleneck →
incubator）。`plan` 只把这些关联了 pro 的当前 op 放进候选，一次问完每个项目
「分多少」（`task 1 4` / `task 1 clear` / `complete`），再按分到的 count 唤起任务、
排进 session（一场一个任务、尽量不拆）。`project <序号> interrupt --hard` 还能
硬中断当前场：全部任务下架 → 输入 title → 新建整场任务挂回去。
见 [`feature_intents/project.md`](feature_intents/project.md)。

**收尾面板长在哪儿**：不是某个视图的内部件，而是 `repl_kit` 的**打断栏位**
（`ReplShell(interrogate=...)`）—— 压在整块看板下面、状态栏上面。换视图、表格变长、
终端太矮被裁屏，都冲不掉它；一问完栏位立刻消失，不占一行。

**状态框**（契约视图最上面那一块）：上面是「契约（data.csv 周几）」的
执行情况，下面是警告（`repl_kit.WarningBoard`）—— data.csv 解析问题、layout 坏了、
今天没有落上每周重复任务…… 警告由框架画、由 warden 每帧重算；`warn off` 只藏警告，
`warn on` 回来。边框是平滑重线（`┏━━ 状态 ━━┓ ┃ ┗━━┛`），和表格里那套线是同一套
字形；老终端想退回 `+ - |`，把 `repl_kit.DEFAULT_BORDER` 换成 `ASCII_BORDER` 即可。

视图可扩展：继承 `View` + `@register_view` 即可；每个视图用 `commands()` 声明自己
认哪些命令、用 `idle_hint()` 声明空闲提示、用 `status_box()` 声明「状态」框里自己那几行，
`to_view()` 把它交给 `repl_kit`。

提示行和 paper_tui 是同一套：空闲时由 `repl_kit.Hint` 排成
`命令: session  cancel  mute   (Tab 切专注视图)`；输入命令后自动换成
`Command.hint` 给的参数 / 取值 / 选项提示（`mute 3 ` → `取值: on  off`）。

界面那一层**不在这个文件里**。提示行、Tab 补全、Shift+Tab 切视图、Ctrl+O 切专注模式、
状态行的每秒刷新、看板的擦除与重画，全部由 `repl_kit`（prompt_toolkit）负责 ——
它按单元格 diff，只把变化的那几格写出去，不整屏重画，所以倒计时跳动时屏幕不抖。
这里只提供三样东西：`frame()`（当前视图的看板）、`View.status_line()`（那行数字）、
`intercept()`（派发前的一道闸，罪案区就挂在这儿）。消息区屏幕上只显示最近 3 条。

看板比终端高时，`WardenShell` 会从**顶部**裁，保证最下面的消息 / 提示行 /
状态行 / 输入行永远在屏幕上（矮终端里顶部本来就装不下）。视图栏和收尾面板
**不在**被裁的那一块里：前者是框架的 `footer`，后者是 `interrogate` 栏位。
补全菜单浮在看板上，不再在输入行下面预留 8 行空白。

界面的三条「永远看得见」的东西，从下往上是：提示行（命令/参数）→ 状态行（秒级数字）
→ 视图栏（你在第几个视图）→ 收尾/查账面板。它们的顺序由 `repl_kit` 的 chrome 统一拼，
warden 只提供内容。

命令输出（帮助面板 / 宣言闸门 / `>>>` 回显）直接 print 就行：它们发生在两次提示符
之间，擦除和重画归 prompt_toolkit。

`--no-clear` / `WARDEN_CLEAR=0` 现在没有效果了（追加/擦除不再由 warden 决定），
仍然接受，只为不打断已有的命令行习惯。

三层摩擦（这套系统真正的规则）
------------------------------
    草稿区  start > now+24h        → 零阻力：session 加、cancel 删，随便改 data.csv
    执行区  now < start ≤ now+24h  → cancel 必须一字不差敲完 152 字宣言
    罪案区  state 说 booked、API 里没有（＝你手动取消了）
                                   → **全局锁死**：除了敲宣言，什么命令都不受理；
                                     罪落盘（temp/warden-sins.json），关掉重开照样锁。
                                     敲完 = 正式放弃：标 abandoned（daemon 永不重订），
                                     若已被 daemon 重订回来，TUI 会再取消掉它。

架构（重要）
------------
    warden.py ──JSON 行──> scripts/bridge.mjs ──Playwright──> 常驻 Chromium
       │  ▲
       │  └── 只读文件 ──> temp/keep_session-state.json / warden-sins.json / pid / log
       ├── 读写 data.csv ──> schedule.py（解析/编辑的**唯一**实现，daemon 也用它）
       ├── 子进程 ──> keep_session.py check | daemon start | stop（薄封装，不重写业务）
       ├── 子进程 ──> omnifocus.py → omnifocus_tasks.py → osascript → OmniFocus
       │              （读状态 / 写回完成与截止时间；**单向**，见 docs/omnifocus.md）
       ├── stats.py ──> 只读公开 API（/v1/me + /v1/sessions 分块拉全量历史）→ 统计视图
       └── 外部操作队列 repl_kit.jobs（单 worker 串行）：**只要不是软件内部自己
           完成的事**都走这儿 —— Focusmate（bridge）、OmniFocus（osascript）、
           keep_session 子进程、日历同步。读异步、写乐观。
           面板挂在看板（Recent）**下面**：标题是这次操作（`import 任务（task import -a 1）`）、
           进度条在标题里、步骤编号 + [omnifocus]/[focusmate] 标签、日志只留三行，
           干完消失；失败 → 回滚本地 + 警告 + 记进 data/failed-actions.json，
           `re-action` 重放。

- 写操作（mute / title / cancel）全部走 bridge（内部 API，见 docs/…-notes.md）。
- daemon 的启停、`session` 之后的立刻下单，都走 keep_session.py 子进程
  （不 fork 在自己身上，也不在 TUI 里重写"怎么下单"）。
- `sync` 走 download_calendar.py 子进程（日历 → data.csv），TUI 不重写日历逻辑。
- **daemon 和 bridge 抢同一个 Chromium profile**：所以 `start` / `session` 之前会先让
  bridge 释放浏览器；bridge 自己也会在空闲 WARDEN_BRIDGE_IDLE_MS 后自动放开。

依赖
----
    python3 warden.py install-deps     # 把 rich 装到 temp/pylibs（不污染系统）
    python3 warden.py                  # 起 TUI

    TUI 框架 repl_kit 在隔壁仓库 paper_researcher 里，按顺序找：已装好的 →
    REPL_KIT_PATH → 同级的 ../paper_researcher。所以两个仓库并排放着时，
    不装也能跑（.app 就走这条路：Finder 起的进程没有你的 PYTHONPATH）。
    想正经装：pip install -e ../paper_researcher

用法
----
    python3 warden.py                          # 交互 TUI
    python3 warden.py --no-clear               # 退化成追加（不删帧、留滚动历史）
    python3 warden.py --exec "stat"            # 拉一次生涯统计（切到统计视图）
    python3 warden.py --exec "day 1; session 09:00 50"   # 跑完命令就退（`;` 分隔）
    python3 warden.py --exec "sync"            # 拉日历重生成 data.csv 后退出
    python3 warden.py --day 1                  # 一进来就看明天
    python3 warden.py --status                 # 只打一次看板然后退出

环境变量：
    FOCUSMATE_PROFILE_DIR     浏览器 profile 目录（默认 ~/.focusmate-mcp/browser-data）
    WARDEN_BRIDGE_IDLE_MS     bridge 空闲关浏览器的毫秒数（默认 120000）
    WARDEN_BRIDGE_HEADED=1    有头模式（看它怎么点）
    WARDEN_CLEAR=0            等价 --no-clear

退出码：0 正常 / 1 出过错 / 2 用法或依赖问题
"""
import argparse
import copy
import dataclasses
import datetime
import inspect
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from warden_core import (  # noqa: E402,F401
    ANSI,
    BOARD_MUTATING_COMMANDS,
    BRIDGE_JS,
    BRIDGE_LOG,
    Bridge,
    BridgeError,
    BoardMixin,
    CN_WEEKDAY_CHARS,
    COMMAND_REFRESH_GAP,
    ChoreMixin,
    ChoreView,
    Console,
    ContractView,
    DECLARATION,
    ERASE_FRAME,
    FAILED_ACTIONS_FILE,
    FM_PENDING,
    FRESH_SESSION,
    FRESH_TASK,
    FocusView,
    FormattedText,
    Group,
    HERE,
    IMPORT_PENDING,
    ImportBatch,
    MESSAGE_KEEP,
    MESSAGE_SHOW,
    NET_PENDING,
    NetMixin,
    PYLIBS,
    Panel,
    ProjectMixin,
    REPL_KIT_CANDIDATES,
    ReviewMixin,
    SESSION_COLUMN_ALIASES,
    SESSION_TABLE_COLUMNS,
    SESSION_UI_MODES,
    SIN_BEEP_LIMIT,
    SRC_CALENDAR,
    SRC_FOCUSMATE,
    SRC_KEEP,
    SRC_OMNIFOCUS,
    STYLE_ACTIVE,
    STYLE_ASSIGNED,
    STYLE_DONE,
    STYLE_DROPPED,
    STYLE_MUTED,
    SessionMixin,
    SettingsView,
    StatsView,
    Style,
    TASK_ACTION_WORDS,
    TASK_AUTO_HIDDEN_COLUMNS,
    TASK_CELL_TITLE_MAX,
    TASK_COLUMN_ALIASES,
    TASK_CONTENT_COLUMN,
    TASK_DYNAMIC_HIDDEN,
    TASK_FALSE_WORDS,
    TASK_HOOK_FLAGS,
    TASK_HOOK_KINDS,
    TASK_HOOK_LABELS,
    TASK_INDEX_MAX_COLS,
    TASK_MARK_STATES,
    TASK_MARK_WORDS,
    TASK_MUTATING_ACTIONS,
    TASK_MUTATING_FLAGS,
    TASK_PANEL_BANDS,
    TASK_PROJECT_MAX_LEN,
    TASK_SESSION_CURRENT,
    TASK_SESSION_NONE,
    TASK_SHORT_FLAGS,
    TASK_TABLE_COLUMNS,
    TASK_TRUE_WORDS,
    TASK_UI_MODE,
    TEMP,
    Table,
    DataView,
    TaskCmdMixin,
    TaskPipelineMixin,
    TaskSyncMixin,
    TaskView,
    Text,
    UI_PREF_MODES,
    URL_RE,
    VIEWS,
    View,
    WORKING_AUTO_HIDDEN_COLUMNS,
    WardenShell,
    _AUTH_HINTS,
    _RELATE_BULLET_RE,
    _RELATE_ESTIMATE_RE,
    _RELATE_HEADING_RE,
    _TaskCompletionValues,
    _cmd,
    _crop_display,
    _hint,
    _hint_actions,
    _hint_cancel,
    _hint_day,
    _hint_mute,
    _hint_mute_universal,
    _hint_period,
    _hint_refresh,
    _hint_relocate,
    _hint_session,
    _hint_set,
    _hint_stat,
    _hint_sync,
    _hint_task,
    _hint_title,
    _parse_local_iso,
    _task_bool_word,
    _task_command,
    Align,
    beep,
    bind_warden_globals,
    box,
    cfg,
    check_declaration,
    cmd_install_deps,
    diff_note,
    explain_bridge_error,
    first_mismatch,
    fmt_age,
    fmt_mmss,
    focusmate_home,
    forget_mute_cache,
    format_compact_timestamp,
    ks,
    mute_data,
    mute_window_for,
    normalize_session_column,
    normalize_task_column,
    note_renderable,
    now_local,
    of,
    parse_day_offset,
    parse_relate_markdown,
    parse_relate_task,
    parse_relocate_offset,
    parse_relocate_spec,
    parse_session_spec,
    pj,
    pp,
    psv,
    quiet_call,
    register_view,
    repl_kit,
    rl,
    run_download_calendar,
    run_keep_session,
    sch,
    sp,
    st,
    task_completer,
    task_dynamic_keys,
    task_repeat_plan_from_omnifocus,
    task_table_headers,
    th,
    to_formatted_text,
    ts,
    visible_session_rows,
)

# 绑定顶层 warden.py 的 globals()，保证测试对 w.run_keep_session / w.BRIDGE_LOG 等的打桩在子模块中同步生效
bind_warden_globals(globals())


class Warden(
    NetMixin,
    BoardMixin,
    ChoreMixin,
    SessionMixin,
    TaskCmdMixin,
    TaskPipelineMixin,
    TaskSyncMixin,
    ReviewMixin,
    ProjectMixin,
):
    def __init__(self, console, bridge, no_clear=False, offline=False, day_offset=0,
                 net_async=None):
        # no_clear 只是个历史参数：擦除/重画已经归 repl_kit，这里不再使用。
        self.console = console
        self.bridge = bridge
        self.no_clear = no_clear
        self.offline = offline
        self.messages = []
        self.rows = []              # 合并后的表行（契约 + API），见 rebuild_rows
        self.api_rows = []          # 选中那天的 API 原始行
        self.entries = []           # data.csv 的全部规则
        self.entry_warnings = []
        self.csv_fp = ""            # data.csv 指纹：改文件前比对，防止改错行
        self.rows_source = ""
        self.rows_note = ""
        self.rows_at = 0
        self.had_error = False
        self.bridge_note = ""
        self.day_offset = int(day_offset)
        # 看板口径：detail = 计划 + 实际全量（默认，保持原行为）；
        # working = 实际为主并收起旧场次。
        self.session_ui_mode = "detail"
        # 列折叠：每个口径单独记；--permanent 落盘到 settings.json 同目录的
        # warden-ui.json。session_* 是本次运行里的临时 open/close 覆盖。
        self.permanent_hidden_columns = {m: set() for m in UI_PREF_MODES}
        self.permanent_visible_columns = {m: set() for m in UI_PREF_MODES}
        self.session_hidden_columns = {m: set() for m in UI_PREF_MODES}
        self.session_visible_columns = {m: set() for m in UI_PREF_MODES}
        self._load_ui_prefs()
        # 视图（Shift+Tab 切换）：默认「契约」视图，第二个是「专注」视图。
        self.view_impls = [cls(self) for cls in VIEWS] or [ContractView(self)]
        # 生涯统计（统计视图 / stat 命令）：self.stat 是 stats.compute_stats 的结果，
        # stat_note 是「实时拉取 / 走缓存 / 拉取失败」那行说明。
        self.stat = None
        self.stat_note = ""
        self.stat_at = None         # 内存里这份统计是什么时候拿的（超 TTL 就重拉）
        # 任务系统：本地 JSON + 第四视图。selected_task_id 是 task assign 的默认目标。
        self.task_store = ts.TaskStore()
        self.selected_task_id = None
        self.task_sync_at = 0.0
        self.task_view_page = None
        # 第四视图「数据」当前展示的数据模型：task / project / op。
        # 用 open task / open project / open op 切换；放在 Warden 上，
        # 从任何视图切过来都能保持上一次选择。
        self.data_view_model = "task"
        # 数据视图的默认过滤器：disposable = 只看未完成/未放弃的记录。
        # 可切到 all / completed / dropped 查看被 mark 掉的 pro/op/task。
        self.data_filter = "disposable"
        self.task_sort_key = None
        self.task_sort_desc = False
        self.task_group_by = None
        self.task_search_query = ""
        # task import 发现 OmniFocus 重复任务时，先不建本地任务：把候选放进这里，
        # 由打断栏位问「复制重复设置吗？」；用户答 y/n/q 后才真正导入。
        self.pending_import = None
        # 重复任务「到日子就生成子任务」：每天最多自动扫一次（frame 里做），
        # 生成后 rebuild_rows 让契约表的 task 列立刻看见 🔁。
        self._repeat_gen_day = None
        self._repeat_gen_at = 0.0
        # 日历下载层 / 时段 layout：每次 frame 重新读（人手可能刚改过）
        self.download = sp.load_download()
        self.layouts = sp.load_layouts()
        # 设置项（edit-tomorrow / tomorrow-plan ...）：界面里全部列出来，
        # 改的时候写 data/settings.json；没改过的项跟着默认值走。
        self.settings = cfg.SettingStore()
        # OmniFocus 集成：只在一个方向上写（warden → OmniFocus）。默认不碰子进程，
        # 真要用的时候才起；`set omnifocus off` 之后连问都不问。
        self.omnifocus = of.default_client()
        # 收尾（postsession）：一结束就在这儿对账。三个变量：
        #   review       当前这场收尾的 post_session.Review（None = 没有在收尾）
        #   review_check 后台核对完成状态的那点进度（dict），到点由 _maybe_start_review 收
        #   review_at    上次检查状态文件的时间（节流，别每帧都读盘）
        self.review = None
        self.review_check = None
        self.review_at = 0.0
        # 「到点没开始」那一问（postsession.NostartReview）：和收尾互斥 ——
        # 一次只问一件事，否则 p/x/s 和 A/B/C/D/E 会抢同一行输入。
        self.nostart = None
        self.nostart_at = 0.0
        # 项目库（OmniFocus Project）：导入的是「活的母体」，任务清单躺着不动，
        # 等 `plan` 给它分了 count 才唤起当天的任务（见 project_store.py）。
        self.projects = pj.ProjectStore()
        try:
            self.projects.ensure_tagged_pros()
        except Exception:                          # noqa: BLE001
            pass
        # 项目库里的叶子任务镜像进任务库（老用户升级后也能直接在任务视图搜到）。
        self.sync_all_project_tasks(quiet=True)
        # `plan` 的分资源问答（project_plan.Allocator）：和收尾 / 到点没开始一样
        # 是「一次只问一件事」—— 它开着的时候那两个都不会弹。
        self.planning = None
        # 未设置类型（SOP / incubator）的 op 待补全队列（id 列表）
        self.pending_op_type = []
        # `pro <序号> -a/--advance` 等待用户提供新 op 的问答状态
        self.pending_advance = None
        # `op <序号> relate --import` 没有关联文件时，等用户输入绝对路径
        self.pending_relate_path = None
        # `project <序号> interrupt --hard` 清完当前场后，等用户输入新任务 title
        self.pending_interrupt = None
        # 最近一次选中的真正项目 pro id + open 顺便打开过的文件记录
        self.selected_pro_id = None
        self.opened_files = []
        # 帧内帮助面板（全屏单页模式下 `help` 挂在页面里展示，任意下一条输入收起）
        self.help_overlay = None
        # 每日项目对账：每天第一次进 TUI / refresh 时后台读一次 OmniFocus。
        # 新增任务自动拉；删除/修改按项目整包问「本地 / OmniFocus / 先不处理」。
        self.project_sync = None
        self._project_sync_queue = []
        self._project_sync_day = None
        self.auto_project_sync = True
        # 静音时段（period mute）自动落地：mutes_at 是节流时间戳，
        # mute_tries 记「这一场刚才试过、几分钟内别再试」，mute_worker 是后台线程。
        self.mutes_at = 0.0
        self.mute_tries = {}
        self.mute_worker = None
        # 琐事 Project 空闲屏：没有 session 时自动切到 ChoreView；q 退出后
        # 一直压到下一场 session 开始（或明天），下一段空档再自动出现。
        # chore_now 可在测试里钉死；None = 跟墙上时钟。
        self.chore_now = None
        self.chore_index = 0
        self.chore_last_note = ""
        self.chore_suppressed_until = None
        self._chore_return_index = 0
        self._chore_last_view_index = 0
        self._chore_leaving = False
        # 「现在」是几点：None = 跟着墙上时钟走。测试/回放可以钉死它，
        # 否则「3 小时内才自动催」这条规则会让用例在钟点变了之后莫名其妙地红。
        self.review_now = None
        # 可注入的“现在”（测试固定时间用；None = 跟墙上时钟）
        self.clock_now = None
        self._snapshot_cache = {}
        self._last_line = ""
        # 命令输入节流：记录「上一条命令开始输入」的墙钟时刻。间隔太长，
        # 下一条命令完成时要主动对一次账（用户可能在外面改了任务/session）。
        self._last_command_input_at = None
        self._auto_refresh_pending = False
        # 交互式 TUI 默认开；专门测「单条异步链路」的用例可以关掉，避免自动
        # 对账把额外的 snapshot/task.pull 混进它正在观察的队列。
        self.auto_refresh_after_command = True
        # 警告板：框架负责画在「状态」框里，这里只负责"有什么问题"。
        # 每次操作 / 每 5 秒重算一次（见 refresh_warnings）。
        self.warnings = repl_kit.WarningBoard()
        self.warnings_at = 0.0
        self.warnings_sig = ()
        # 重算 data.csv 时被"计划里没有"冲掉的行（例如手写的旧契约）：
        # 记下来当警告显示，别让人以为契约凭空消失了。
        self.csv_dropped = []
        # ── 网络队列 ──────────────────────────────────────────────────
        # 读也异步、写乐观：所有碰网络的东西都交给 repl_kit.jobs 的单 worker
        # 串行队列，主线程只在 drain_net() 里应用结果（回滚也在那儿做）。
        # 测试 / --exec / --status 用同步模式：同一条代码路径，没有时序。
        self.net_async = (bool(net_async) if net_async is not None else
                          bool(repl_kit.PROMPT_TOOLKIT and not offline
                               and sys.stdin.isatty()))
        self.jobs = repl_kit.JobRunner(
            sync=not self.net_async,
            store=FAILED_ACTIONS_FILE,
            title="网络",
            on_change=self._net_touched,
        )
        self._net_dirty = False
        #: 每份数据是什么时候拿到的：{"session": epoch, "task": epoch, "stat": epoch}
        self.read_at = {}
        self._register_net_actions()
        # 提示行 / 补全 / 视图切换 / 状态行的定时刷新 / 帧的擦除重画，
        # 全部交给 repl_kit（prompt_toolkit 按单元格 diff，只写变化的那几格）。
        shell_kwargs = dict(
            name="warden",
            views=[impl.to_view() for impl in self.view_impls],
            console=lambda: self.console,
            tick=1.0,
            view_key="s-tab",          # Shift+Tab：切视图
            focus_key="c-o",           # Ctrl+O：循环专注模式
            on_focus=self.cycle_focus_mode,
            on_view_change=self._on_view_change,
            intercept=self.intercept,
            # 注：警告默认显示、且只在「契约」视图显示（用户要求）。
            warnings=self.warnings,
            warnings_view=("契约",),
            show_warnings=True,
            status_title="状态",
            # 每帧画框之前先重算警告：框和看板永远是同一时刻的事实
            warnings_refresh=self.refresh_warnings,
            # 打断栏位（repl_kit 的 interrogate）：收尾/查账那句问话挂在这里 ——
            # 它压在整块看板**下面**、提示行上面，翻页/裁屏都冲不掉它。
            interrogate=self.render_interrogate,
            # 底部状态栏（repl_kit 的 footer）：视图栏在打断栏位**下面**、提示行上面。
            footer=self.render_view_bar,
            # 进度栏位（repl_kit 的 progress）：后台在干活时，看板（Recent 之后）
            # 下面多一块「在做什么」；干完它自己消失，一次一行都不占。
            progress=self.jobs,
        )
        # 看板自己就是最下面那块内容；补全菜单浮在它上面，不再在输入行
        # 下面留 8 行空白（留了就会把看板/提示挤出屏幕）。
        # 老版本 repl_kit 没有这个参数就退回默认值（WardenShell 会按 8 行算）。
        # 注意要问 repl_kit.ReplShell（WardenShell 自己只是 *args/**kwargs 透传）。
        sig_params = inspect.signature(repl_kit.ReplShell).parameters
        if "reserve_space_for_menu" in sig_params:
            shell_kwargs["reserve_space_for_menu"] = 0
        if "erase_when_done" in sig_params:
            shell_kwargs["erase_when_done"] = not bool(no_clear)
        if "alt_screen" in sig_params:
            shell_kwargs["alt_screen"] = not bool(no_clear)
        shell_kwargs["pin_bottom"] = True
        self.shell = WardenShell(**shell_kwargs)
        # 画帧缓存要认识「业务状态变没变」：把签名挂上去（见 frame_signature）。
        self.shell.frame_key_hook = self.frame_signature
        # 全屏 Application 运行期间，框架的 print（unknown command / exception /
        # `jobs` 输出）必须接进 Recent，不能绕过渲染器写终端历史。
        self.shell.output_hook = self._inline_shell_output

    # -- 画帧缓存 -------------------------------------------------------

    def invalidate(self):
        """告诉画帧缓存「刚才那个画面不算数了」。

        看板不会每帧重排版了（见 `WardenShell.frame_text` 的缓存），所以凡是
        能改变画面的动作都得在这儿拍一下。漏拍也不会画错超过一秒 —— 帧键里
        带着 `int(time.time())`，最坏是慢一秒，和 `tick=1.0` 是一个量级。
        """
        shell = getattr(self, "shell", None)
        hook = getattr(shell, "invalidate_frame", None)
        if hook is not None:
            try:
                hook()
            except Exception:                          # noqa: BLE001 - 画面旧一秒也别崩
                pass

    def review_signature(self):
        """打断栏位里那块的「一句话身份」：内容一变，画帧缓存就得作废。

        `id(self.review)` 只认得「换了一场」，认不出「问到下一条了」——
        所以还要 stage / index / pending / 已经说了几句话。全部 O(1)：
        这个函数每画一帧都要调一次，**不许**在里面读盘或遍历任务。
        """
        review = self.review
        if review is None:
            prompt = getattr(self, "nostart", None)
            if prompt is not None:
                return ("nostart", id(prompt), prompt.closed, len(prompt.notes))
            return (id(self.review_check), bool(self.review_check))
        return (
            id(review), review.stage, review.index, review.closed,
            len(review.notes), len(review.skipped),
            (review.pending_task or {}).get("id"),
        )
    # -- 循环

    def lockdown_reason(self):
        """有未认罪的罪时，返回一句人话；没有就返回 None。"""
        sins = self.pending_sins()
        if not sins:
            return None
        head = "、".join(sorted(set(r.get("key", "?") for r in sins))[:3])
        return ("有 %d 笔未认罪的取消记录（%s%s）：daemon 说这些场次订过，"
                "但 API 里查不到 —— 也就是你手动取消了。" %
                (len(sins), head, "…" if len(sins) > 3 else ""))

    # -- 派发（框架的 intercept 钩子 + 一个给测试/--exec 用的瘦包装）------

    def guard(self, fn):
        """把一条命令的异常翻译成人话 —— 交互路径和测试路径用的是同一套。

        跑完（成功或失败）都重算一次警告板：状态框里永远是"现在"的问题，
        不用谁记得在每条命令末尾补一句 refresh_warnings。

        另外：收尾正在「延期」阶段等你敲转移命令时，**每条派发出去的命令跑完
        都要回头问收尾一句**（见 :meth:`after_dispatch_review`）。
        挂在 guard 里而不是 handle 里，是因为 TUI 的交互路径走 repl_kit 的
        dispatch，根本不经过 Warden.handle。
        """
        def run(args):
            try:
                fn(args)
            except SystemExit:
                raise
            except BridgeError as e:
                self.had_error = True
                self.say("bridge 出错：%s" % e, "error")
            except ValueError as e:
                self.say(str(e), "warn")
            except Exception as e:  # 兜底：别让一条命令把 TUI 掀了
                self.had_error = True
                self.say("意外错误：%s: %s" % (type(e).__name__, e), "error")
            finally:
                if self.review is not None:
                    self.after_dispatch_review(self._last_line or "")
                self.refresh_warnings(force=True)
                self._after_command_finished()
        return run

    def _command_line_parts(self, line):
        """把 ``_last_line`` 拆成 (规范命令名, 参数 tokens)；拆不了就返回空。"""
        try:
            parts = shlex.split(line or "")
        except ValueError:
            parts = (line or "").split()
        if not parts:
            return "", []
        command = None
        registry = getattr(getattr(self, "shell", None), "registry", None)
        if registry is not None:
            try:
                command = registry.get(parts[0].lower())
            except Exception:                          # noqa: BLE001 - 只是刷新判定
                command = None
        name = (getattr(command, "name", "") or parts[0]).lower()
        return name, parts[1:]

    @staticmethod
    def _task_command_mutates(tokens):
        """``task`` 是否真的改账本（select / 列表这类只看的不算）。"""
        for token in tokens:
            word = str(token).strip().lower()
            if word in TASK_MUTATING_ACTIONS:
                return True
            if any(word == flag or word.startswith(flag + "=")
                   for flag in TASK_MUTATING_FLAGS):
                return True
        return False

    def _command_touches_board(self, name, tokens):
        if name == "task":
            return self._task_command_mutates(tokens)
        return name in BOARD_MUTATING_COMMANDS

    def _record_command_input(self, line, now=None):
        """记下命令输入时刻，并判断距离上一条是否已超过自动 refresh 的阈值。"""
        if not (line or "").strip():
            return False
        # 用墙钟而不是 monotonic：机器睡眠时 monotonic 可能不走，
        # 而「半小时没碰 TUI」应该包含睡眠过去的时间。
        now = time.time() if now is None else float(now)
        previous = self._last_command_input_at
        stale = (previous is not None
                 and now - previous >= COMMAND_REFRESH_GAP)
        if stale:
            self._auto_refresh_pending = True
        self._last_command_input_at = now
        return stale

    def _after_command_finished(self):
        """命令完成后的统一收尾：本地立刻重算 + 需要时后台对远端账。

        本地重算必须发生在任何网络往返之前：task delete/assign、session 改动
        这类动作已经写进本地账本，表格应马上反映；随后排进 JobRunner 的
        ``session.snapshot`` / ``task.pull`` 再拿云端事实覆盖它。
        """
        name, tokens = self._command_line_parts(self._last_line)
        touches = self._command_touches_board(name, tokens)
        stale = self._auto_refresh_pending
        self._auto_refresh_pending = False
        if touches:
            try:
                self.reload_entries()
            except Exception as e:                     # noqa: BLE001 - 本地重算失败别掀 TUI
                self.say("本地表格重算失败（先用上一份）：%s" % e, "warn")
            try:
                self.rebuild_rows()
            except Exception as e:                     # noqa: BLE001
                self.say("本地表格重算失败（先用上一份）：%s" % e, "warn")
            self.invalidate()
        if not (touches or stale):
            return False
        if not self.auto_refresh_after_command or not self.net_async:
            # --exec / 测试 / 离线走同步语义：本地重算已经完成，不额外发网络。
            return False
        if name == "refresh" and not stale:
            # refresh 自己就是全量对账，别再排一遍。
            return False
        self.refresh(quiet=True)
        self.refresh_task_state()
        self.refresh_stat_state()
        self.refresh_review_state()
        self.forget_caches()
        self.invalidate()
        return True

    def exit_tui(self):
        raise SystemExit(0)

    def cmd_stat_from_args(self, args):
        """`stat` 在任何视图都能敲：先切到统计视图，再按 TTL 决定要不要重拉。"""
        force = any(a in ("refresh", "force", "--force") for a in args)
        full = any(a in ("full", "all", "--full") for a in args)
        # 先切视图：第一次进统计视图时 enter_view() 自己会拉一次，别拉两遍。
        self.go_to_view(StatsView)
        # 内存里那份放超过 TTL 也算过期：TUI 开着过夜再敲 stat 不该看到旧数字。
        stale = (self.stat is None or self.stat_at is None
                 or (datetime.datetime.now() - self.stat_at).total_seconds()
                 >= st.CACHE_TTL)
        if stale or force or full:
            self.cmd_stat(force=force, full=full)

    def _split(self, line):
        """shlex 拆一行；引号不配对就说一句、返回 None。"""
        try:
            return shlex.split(line)
        except ValueError as e:
            self.say("引号没配对：%s" % e, "error")
            return None

    def intercept(self, line):
        """repl_kit 在派发前问一句：返回 True=我处理了，None=照常派发。

        顺手把这一行记在 ``_last_line`` 上：命令跑完（``guard``）要拿它问收尾一句
        「你刚才那条算不算把任务转移走了」。
        """
        self._last_line = line
        self._record_command_input(line)
        # 如果上一帧开着 help 面板，敲回车或下一条命令时先收起它。
        if self.help_overlay is not None:
            self.help_overlay = None
            if not (line or "").strip():
                self.invalidate()
                return True
        # 这条命令可能改画面（表格/任务/消息/警告）：先作废上一次的画帧缓存。
        self.invalidate()
        # 罪案区的全局锁：除了敲宣言，什么都不受理。
        # （Shift+Tab / Ctrl+O 是框架的键绑定，不经过这里 —— 和以前一样能按。）
        sins = self.pending_sins()
        if sins:
            if line.strip().lower() in ("quit", "exit", "q"):
                self.say("你要走可以，但罪挂在那儿、锁不会解 —— 下次开 TUI 还得敲。", "warn")
                raise SystemExit(0)
            ok, why = check_declaration(line)
            if ok:
                self.confess(sins)
                self.refresh(quiet=True)
                return True
            beep()
            self.had_error = True
            self.say("锁死中：%s" % (why or "把宣言一字不差地敲完才能做别的事。"), "error")
            self.emit(self.render_lockdown_panel())
            return True

        # task import 问「复制重复设置吗？」时，下一行先当作回答吃；不回答
        # y/n/q 就不派发任何命令，避免 prompt 被别的操作悄悄丢掉。
        if self.pending_import is not None:
            return self._answer_pending_import(line)
        if self.pending_advance is not None:
            return self._answer_pending_advance(line)
        if self.project_sync is not None:
            return self._answer_project_sync(line)
        if self.pending_relate_path is not None:
            return self._answer_pending_relate_path(line)
        if self.pending_interrupt is not None:
            return self._answer_pending_interrupt(line)

        # 「到点没开始」那一问开着：p / x / s 先给它吃（和收尾互斥，见 _maybe_start_*）。
        prompt = getattr(self, "nostart", None)
        if prompt is not None:
            verdict = prompt.handle(line)
            action = (verdict or {}).get("action")
            if action == "dispatch":
                return None
            if action == "close" or prompt.closed:
                self.close_nostart(note=(verdict or {}).get("done") or "answered")
            self.invalidate()
            return True

        # `plan` 的分资源问答开着：这一行先给它吃（task 1 4 / complete / q）。
        # 它和「到点没开始」「收尾」互斥（一次只问一件事，见各自的开场判断）。
        if self.planning is not None:
            verdict = self.planning.handle(line)
            action = (verdict or {}).get("action")
            if action == "dispatch":
                return None
            if action == "close" or self.planning.closed:
                self.close_plan(note=(verdict or {}).get("done"))
            self.invalidate()
            return True

        # 收尾进行中：这一行先给收尾吃。延期阶段要用户自己敲转移命令，
        # 所以「dispatch」= 照常派发（派发完 handle() 会回头问收尾一句）。
        if self.review_active():
            verdict = self.review.handle(line)
            action = (verdict or {}).get("action")
            if action == "dispatch":
                return None
            if action == "close" or self.review.closed:
                self.close_review()
            return True
        if self.pending_op_type:
            ans = self._answer_pending_op_type(line)
            if ans is not None:
                return ans
        # 琐事空闲屏：m / c / q 是它的全部命令。q 走「退出空闲屏」，
        # 不能落到通用 quit（那会退出整个 TUI）。打断问答优先于琐事屏。
        if self._handle_chore_input(line):
            return True
        parts = self._split(line)
        if parts is None:
            return True
        if not parts:
            return True
        # 直接敲 `T1 50 50`（契约视图）等价于 `period T1 50 50`。
        if (isinstance(self.view, ContractView)
                and re.match(r"^[Tt]\d+$", parts[0])):
            self.guard(self.cmd_period_layout)(parts)
            return True
        if self.shell.registry.get(parts[0].lower()) is None:
            self.say(self.view.unknown_hint(), "warn")
            return True
        return None

    def handle(self, line):
        """执行一条命令；返回 False 表示该退出了（--exec 和测试走这条路）。"""
        if "\t" in line and line.replace("\t", "").strip() == "":
            self.cycle_view()
            return True
        if line.strip() == "\x1b[Z" or line.strip().lower() in (
                "shift-tab", "shift+tab", "s-tab"):
            self.cycle_focus_mode()
            return True
        try:
            if self.intercept(line) is True:
                return True
        except SystemExit:
            return False
        parts = self._split(line)
        if not parts:
            return True
        command = self.shell.registry.get(parts[0].lower())
        if command is None:
            self.say(self.view.unknown_hint(), "warn")
            return True
        try:
            # 紧凑短选项（`-an` ≡ `-a none`）走 repl_kit 那一套展开：
            # --exec / 测试用的 handle() 和交互式 dispatch 必须走同一条路，
            # 否则「界面上能敲、脚本里不认」这种裂痕迟早会出现。
            command.handler(command.expand(parts[1:]))
        except SystemExit:
            return False
        # 收尾在「延期」阶段会等你敲一条转移命令 —— 敲完得回头看一眼成没成
        self.after_dispatch_review(line)
        self.invalidate()          # 这条命令改了什么，下一帧就得画出来
        return True

    def render_lockdown_panel(self):
        return Panel(
            Text.assemble((self.lockdown_reason() + "\n\n", "bold red"),
                          ("把下面这段一字不差地敲一遍（少一个标点就继续锁死 + 鸣笛）：\n\n",
                           "bold red"),
                          (DECLARATION, "bold")),
            title="[bold red]⛔  认罪之前，什么都不许做[/]",
            border_style="bold red", box=box.HEAVY,
        )

    def write_tui_state(self):
        """把「我在这里」写给 daemon（``temp/warden-tui.json``）。

        内容只有三样：**pid**（活着没活着）、**tty**（在哪个终端上找得到这个
        TUI）、**started_at**。daemon 结束一场 session 之后要「把人叫回来」，
        它必须能回答一个问题：*warden 现在到底开着没*。

        以前它不回答，于是 summon 只会「聚焦那个 iTerm session 并打一行 refresh」
        —— 你 `q` 退出过 warden 之后，那个 session 就是个 shell，refresh 打进去
        等于没叫（日志里还写着 focused，看着像成功）。
        """
        path = ks.TUI_STATE_FILE
        info = {"pid": os.getpid(),
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "cwd": HERE}
        try:
            info["tty"] = os.ttyname(0)
        except (OSError, ValueError, AttributeError):
            info["tty"] = ""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass                     # 写不了就退化成老行为，别挡住 TUI
        self.tui_state = info
        return info

    def clear_tui_state(self):
        """退出时把「我在这里」擦掉（只擦自己那条，孤儿进程别误删别人的）。"""
        try:
            with open(ks.TUI_STATE_FILE, encoding="utf-8") as f:
                if int(json.load(f).get("pid") or 0) != os.getpid():
                    return
        except (OSError, ValueError, TypeError):
            return
        try:
            os.unlink(ks.TUI_STATE_FILE)
        except OSError:
            pass

    def run(self, exec_lines=None):
        if not exec_lines:
            self.write_tui_state()
        self.emit("[dim]起 bridge、拉 session…[/]")
        # 启动这一批：session 列表 + Focusmate task 完成状态。
        # 后台模式下 TUI 立刻出来，这两件事在面板里跑（干完面板自己消失，
        # 结果进 Recent）；同步模式（--exec / 测试）当场跑完。
        self.jobs.begin(1 if self.offline else 2)
        try:
            self.refresh()
            self.sync_task_completions(quiet=True)
        except Exception as e:
            self.say("首次拉列表失败：%s" % e, "error")
            self.had_error = True
        if exec_lines:
            self.wait_net()

        # 一进来就有未认罪的罪 → 先把话说明白
        if self.lockdown_reason():
            self.emit(Panel(
                Text.assemble(("⛔  这个 TUI 现在是锁死的。\n\n", "bold red"),
                              (self.lockdown_reason() + "\n\n", "red"),
                              ("唯一能做的事：把下面这段一字不差地敲一遍"
                               "（%d 字符，少一个标点就继续锁死 + 鸣笛）。\n\n"
                               % len(DECLARATION), "bold red"),
                              (DECLARATION, "bold")),
                title="[bold red]The Walk of Shame · 你欠着账[/]",
                border_style="bold red", box=box.HEAVY,
            ))

        if exec_lines:
            # --exec 没有提示符行，帧外只有 `>>>` 回显
            self.paint(prompt_row=False)
            for line in exec_lines:
                if line.strip() or line.strip("\t") != line:
                    self.emit("[bold cyan]>>>[/] %s" % line)
                    if not self.handle(line):
                        break
                    self.paint(prompt_row=False)
            return 0 if not self.had_error else 1

        # 交互模式：提示行、补全、Shift+Tab 切视图、Ctrl+O 切模式、
        # 状态行每秒原地刷新、帧的擦除重画 —— 全在 repl_kit 里。
        # 提示符（宣言> / warden>）和罪案区闸门由 prompt_text / intercept 提供。
        try:
            self.shell.run()
        finally:
            # 后台线程是 daemon：退出前给它一次机会把手上那件事做完，
            # 别让「刚点了取消」这种操作停在半路（超时就照旧退）。
            self.jobs.wait(5.0)
            self.clear_tui_state()
            self.emit("[dim]收工。[/]")
        return 0 if not self.had_error else 1


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Focusmate 狱卒看板（rich TUI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----\n", 1)[-1] if "用法\n----\n" in __doc__ else None,
    )
    ap.add_argument("--exec", dest="exec_", metavar="CMDS",
                    help='跑完这些命令就退（`;` 分隔），如 --exec "day 1; session 09:00 50"')
    ap.add_argument("--day", dest="day", type=parse_day_offset, default=0,
                    help="一进来就看哪一天：-1=昨天 0=今天 1=明天（也可给 YYYY-MM-DD）")
    ap.add_argument("--status", action="store_true", help="只画一次看板然后退出（不拉浏览器列表）")
    ap.add_argument("--no-clear", action="store_true",
                    help="【已废弃，无效果】以前是退化成追加；现在擦除重画由 repl_kit 管")
    ap.add_argument("--offline", action="store_true", help="不碰浏览器，只用 state.json 里的快照")
    ap.add_argument("--headed", action="store_true", help="浏览器有头模式（看它怎么操作）")
    ap.add_argument("--idle-ms", type=int, default=None,
                    help="bridge 空闲多少毫秒后放开浏览器（默认 120000；0=不放开）")
    ap.add_argument("--profile-dir", default=None, help="浏览器 profile 目录（默认用 _lib 的默认值）")
    ap.add_argument("--install-deps", action="store_true", help="把 rich 装到 temp/pylibs")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.install_deps:
        return cmd_install_deps()

    no_clear = args.no_clear or os.environ.get("WARDEN_CLEAR") == "0"
    if no_clear:
        sys.stderr.write("注意：--no-clear / WARDEN_CLEAR=0 已经不起作用了"
                         "（擦除重画交给 repl_kit / prompt_toolkit）。\n")
    console = Console()

    bridge = Bridge(console, headed=args.headed, idle_ms=args.idle_ms,
                    profile_dir=args.profile_dir)
    # --status / --exec / --offline 一律同步跑（脚本要的是确定的结果，
    # 不是「后台还在拉」）。交互模式才异步 + 乐观。
    warden = Warden(console, bridge, no_clear=no_clear, offline=args.offline,
                    day_offset=args.day,
                    net_async=False if (args.status or args.exec_ or args.offline) else None)

    if args.status:
        # 只画一次：不连浏览器，用 state.json 的快照/本地文件
        warden.offline = True
        warden._fallback_rows("--status 不连浏览器")
        warden.paint(prompt_row=False)
        return 0

    exec_lines = None
    if args.exec_:
        exec_lines = [s for s in args.exec_.split(";") if s.strip()]

    try:
        return warden.run(exec_lines=exec_lines)
    finally:
        bridge.stop()


if __name__ == "__main__":
    sys.exit(main())
