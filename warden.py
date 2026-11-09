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
   任务大小的 session、把那个时段剪断（原来那场 50 分钟消失）并自动 assign，
   见 `task_hooks.format_end` / `_cut_period_for_carve`）；
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
契约视图的任务面板下面多一张项目表（剩余 count / 进度 / 今天排了多少）。`plan`
先把这一天剩下的空场切成 count（1 count = 25 分钟），逐个项目问「分多少」
（`task 1 4` / `task 1 clear` / `complete`），再按分到的 count 唤起任务、排进 session
（一场一个任务、尽量不拆）。见 [`feature_intents/project.md`](feature_intents/project.md)。

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
TEMP = os.path.join(HERE, "temp")
PYLIBS = os.path.join(TEMP, "pylibs")

# 依赖装在项目里的 temp/pylibs，不动系统环境（和 download_calendar.py 一个路子）
if os.path.isdir(PYLIBS) and PYLIBS not in sys.path:
    sys.path.insert(0, PYLIBS)

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.style import Style
except ImportError:
    sys.stderr.write(
        "缺少 rich。装一下（只装到项目的 temp/pylibs，不动系统环境）：\n"
        "    python3 %s install-deps\n" % os.path.join(HERE, "warden.py")
    )
    sys.exit(2)

sys.path.insert(0, HERE)

# TUI 的「提示行 / 补全 / 视图切换 / 状态行 / 帮助表」全在 repl_kit 里，
# 这里只写业务命令。
#
# repl_kit 是隔壁仓库里的独立包，按三种方式找，前面的赢：
#   1. 已经装好的（pip install -e /path/to/paper_researcher）—— 正常情况
#   2. REPL_KIT_PATH 指到 paper_researcher —— 换机器/换解释器又不想动环境
#   3. 同级的 ../paper_researcher —— 两个仓库一起 checkout 时的默认位置
#
# 第 3 条主要是给 .app 用的：Finder 双击起的进程拿不到你 shell 里的
# PYTHONPATH，"少跑过一次 pip" 不该表现成『点什么都没反应』。
REPL_KIT_CANDIDATES = [
    os.environ.get("REPL_KIT_PATH") or "",
    os.path.join(os.path.dirname(HERE), "paper_researcher"),
]
try:
    import repl_kit  # noqa: F401
except ImportError:
    for _cand in REPL_KIT_CANDIDATES:
        if not _cand or not os.path.isdir(os.path.join(_cand, "repl_kit")):
            continue
        sys.path.insert(0, _cand)
        try:
            import repl_kit  # noqa: F401
            break
        except ImportError:
            sys.path.remove(_cand)
    else:
        sys.stderr.write(
            "缺少 repl_kit（TUI 框架）。三种解法选一个：\n"
            "  1. pip install -e %s\n"
            "  2. REPL_KIT_PATH=/path/to/paper_researcher python3 warden.py\n"
            "  3. 把 ti_extend 和 paper_researcher 放在同一个目录下\n"
            % REPL_KIT_CANDIDATES[1]
        )
        raise SystemExit(2)

try:  # prompt_toolkit 只是个锦上添花：没有它 repl_kit 会退化成 input() 循环
    from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
except ImportError:  # pragma: no cover
    ANSI = None
    FormattedText = None
    to_formatted_text = None

import keep_session as ks  # noqa: E402  只用来读 daemon 状态 / 起停 daemon / 罪与判决
import schedule as sch  # noqa: E402  data.csv 的解析与编辑（唯一实现）
import session_plan as sp  # noqa: E402  日历下载层 / 时段 layout / 精确日期 csv
import task_store as ts  # noqa: E402  本地任务库 + Focusmate 完成归档
import task_hooks as th  # noqa: E402  任务的 trigger / formatter 预设函数框架
import stats as st  # noqa: E402  生涯统计：只读 API 拉全量历史 + 算数字 + 缓存
import settings_store as cfg  # noqa: E402  设置项注册表 + data/settings.json
import omnifocus as of  # noqa: E402  OmniFocus：链接解析 + 读写（osascript 子进程）
import postsession as psv  # noqa: E402  session 结束 15s 后的收尾（纯逻辑）
import project_store as pj  # noqa: E402  项目库：OmniFocus Project 的存在与进度
import project_plan as pp  # noqa: E402  plan：空闲容量 → 分资源 → 排任务（纯逻辑）
import runtime_log as rl  # noqa: E402  按天落盘的 JSONL 运行日志（Focusmate 拉取 / OmniFocus 写回）

# -- relate：关联文件模板的 TaskPaper-ish 解析（op <N> relate --export/--import）-----
_RELATE_HEADING_RE = re.compile(r"^#\s+(.*)$")
_RELATE_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_RELATE_ESTIMATE_RE = re.compile(r"@estimate\(([^)]*)\)", re.I)


def parse_relate_markdown(text):
    """把 relate 模板拆成 ``{章节名: [bullet, ...]}``。

    每个 bullet 形如 ``{"title": str, "notes": [str]}``；``@estimate(10m)``
    留在 title 里，由 :func:`parse_relate_task` 再剥。
    """
    sections = {}
    current = None
    for line in (text or "").splitlines():
        m = _RELATE_HEADING_RE.match(line.rstrip())
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        if not line.strip():
            continue
        sections.setdefault(current, []).append(line)
    out = {}
    for name, lines in sections.items():
        tasks = []
        cur = None
        for line in lines:
            m = _RELATE_BULLET_RE.match(line.rstrip())
            if m:
                if cur is not None:
                    tasks.append(cur)
                cur = {"title": m.group(1).strip(), "notes": []}
            else:
                note = line.strip()
                if note and cur is not None:
                    cur["notes"].append(note)
        if cur is not None:
            tasks.append(cur)
        out[name] = tasks
    return out


def parse_relate_task(task, fallback_pk):
    """一条 markdown bullet → ``{"pk", "title", "note", "minutes"}``。"""
    title = str((task or {}).get("title") or "")
    minutes = None
    m = _RELATE_ESTIMATE_RE.search(title)
    if m:
        minutes = ts.parse_minutes(m.group(1))
        title = _RELATE_ESTIMATE_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()
    note = "\n".join(str(x).strip() for x in ((task or {}).get("notes") or ())
                     if str(x).strip()) or ""
    return {"pk": fallback_pk, "title": title, "note": note, "minutes": minutes}


BRIDGE_JS = os.path.join(HERE, "scripts", "bridge.mjs")
#: bridge 的 stderr（`_lib` 的 die()/log 都落这儿）—— 报错信息里直接指过去。
BRIDGE_LOG = os.path.join(TEMP, "warden-bridge.log")

# 罪案区的全局锁：有未认罪的 sin 时，除了宣言什么都不受理。
# 上限只影响一轮循环里反复敲错的次数（敲错就继续鸣笛、继续锁）。
SIN_BEEP_LIMIT = 3

# 消息区（Recent）：屏幕上只显示最近 MESSAGE_SHOW 条 —— 屏幕永远只有「一帧」，
# 留一长串历史只会让帧越来越高、钳到屏高之后顶部残帧反而删不干净。
# 内部再多留几条（MESSAGE_KEEP），好让「敲错被记账」这类过程还能被测试和排查看到。
MESSAGE_SHOW = 3
MESSAGE_KEEP = 12

# 网络操作全走后台队列（repl_kit.jobs）。失败的行动记在这儿 ——
# 一条 = 名字 + 参数 + 为什么 + 什么时候；`re-action` 原样重放一遍。
# 落盘是为了跨进程：warden 关掉再开，昨天断网那几条还等着补。
FAILED_ACTIONS_FILE = (os.environ.get("TI_FAILED_ACTIONS")
                       or os.path.join(HERE, "data", "failed-actions.json"))

# 读操作的「新鲜度」：状态框里那行「这份数据是什么时候来的」。
# 新鲜 = 灰的、不吵你；过期 = 红色加粗 —— 表格里的 session 是几分钟前的，
# 和「现在几点了」是两件事，别让你拿旧账做决定。
FRESH_SESSION = 5 * 60
FRESH_TASK = 15 * 60

# 两条「命令跑完后自动对账」策略：
#   * 两次输入隔太久（TUI 可能开着过夜/去吃饭了）→ 下一条命令跑完就 refresh；
#   * 命令会改到看板（session / task / plan...）→ 先按本地账本立刻重算，
#     再把远端拉取排进后台队列，用云端结果覆盖乐观视图。
COMMAND_REFRESH_GAP = 30 * 60

# 这些命令会改变契约表 / 任务面板 / 项目表的内容。命令跑完后先 rebuild_rows()
# 让本地修改立刻可见，再把真正的远端快照排进 JobRunner（不阻塞输入）。
BOARD_MUTATING_COMMANDS = frozenset({
    "session", "period", "relocate", "cancel", "mute", "title", "day", "sync",
    "force-check", "plan", "project", "task",
})

# task 里只有这些子命令/后缀会改账本；`task 3` / `task select 3` 只是换选中项。
TASK_MUTATING_ACTIONS = frozenset({
    "create", "import", "delete", "modify", "note", "assign", "duplicate",
    "drop", "done", "link", "repeat", "sync", "swap", "archive",
})
TASK_MUTATING_FLAGS = (
    "-a", "--assign", "-n", "--note", "-m", "--mark", "-c", "--count",
    "-e", "--exclusive", "-t", "--trigger", "-f", "--formatter",
    "-s", "--from",
)

#: focusmate 里「乐观预测、还没落地」的标记（任务库里的一个字段）
FM_PENDING = "pending"

#: 「这件事已经排进后台队列了」——同步写法里本该返回结果的地方返回它，
#: `cmd_task` / `handle` 见到就知道后缀已经交给后台那半件事了。
NET_PENDING = "__net_pending__"

#: 面板上 `[…]` 里的那几个标签：谁在外面干活。
SRC_FOCUSMATE = "focusmate"
SRC_OMNIFOCUS = "omnifocus"
SRC_KEEP = "keep_session"
SRC_CALENDAR = "calendar"

# 契约表格 task 列里的每条任务：「1: 任务… | 3: 任务…」。
# 每条标题单独裁到 TASK_CELL_TITLE_MAX 个字（后面的内容 trim 掉），
# 别让第一条把第二条挤出屏幕（列的 overflow="crop" 只能整体截断）。
TASK_CELL_TITLE_MAX = 18

# 契约表格下面那块任务面板：2 列是底线，屏幕够宽就摊成 3 列、4 列。
# 填法是**一行一行**（左→右、上→下），所以一行里读过去编号是递增的。
TASK_INDEX_MAX_COLS = 4

# 任务面板里的「状态段」：待办的先出现，收工的往后放。
# 用户反馈过「放弃的任务和待办任务放在一起，没有 grouping」—— 所以状态是第一维，
# 段内再按位置（未挂 session 最前 → 场次时间 → 编号）。
TASK_PANEL_BANDS = (ts.STATUS_ACTIVE, ts.STATUS_COMPLETED, ts.STATUS_DROPPED)

# `-a` 的两个特殊取值：不进表格编号，直接按语义办。
#   none    = 下架（删掉 Focusmate 那条 task，本地断绑定）→ `task 8 -an`
#   current = 当前这一场（进行中的优先，其次最近一场还没开始的）→ `task 8 -ac`
# 两个都能写成 `-a none` / `-a current`，也各自有一个紧凑写法（见 SHORT_FLAGS）。
TASK_SESSION_NONE = ("none", "off", "no", "-", "无", "空", "下架", "卸下")
TASK_SESSION_CURRENT = ("current", "cur", "now", "当前", "现在", "这一场", "这节")

# 紧凑短选项（repl_kit 的 short 表）：`-an` ≡ `-a none`、`-ac` ≡ `-a current`。
# 定义成数据而不是散在解析里：补全要能补到它们，提示行也要能念出来。
TASK_SHORT_FLAGS = (
    repl_kit.ShortFlag("-an", ("--assign", "none"), note="从 session 下架"),
    repl_kit.ShortFlag("-ac", ("--assign", "current"), note="挂到当前这一场"),
    # `-m` 展开成 `--mark`，后面那个词（complete / discard）照常跟着：
    # `task 3 -m complete` ≡ `task 3 --mark complete`。
    repl_kit.ShortFlag("-m", ("--mark",), note="手动标记 complete/discard"),
    # trigger / formatter 预设：`-t end` ≡ `--trigger end`、`-f end` ≡
    # `--formatter end`，后面的参数（key=value）照常跟着。
    repl_kit.ShortFlag("-t", ("--trigger",), note="trigger 预设函数"),
    repl_kit.ShortFlag("-f", ("--formatter",), note="formatter 预设函数"),
    # `task import -st` ≡ `--single-task`：只导入这一条，就算 OmniFocus 里是
    # 重复任务也不问「复制重复设置」、不建本地 repeat。
    repl_kit.ShortFlag("-st", ("--single-task",), note="只导入一次，不建重复任务"),
    # `task 3 -e true` ≡ `--exclusive true`：这条独占一整场 session。
    repl_kit.ShortFlag("-e", ("--exclusive",), note="独占：这一场只能有这一条任务"),
    # `task 3 -c +2` ≡ `--count +2`：count 进度（1 count = 25 分钟）。
    # 纯数字 = 设定最高 count（duration 跟着变），`+n` / `-n` = 加减已投入。
    repl_kit.ShortFlag("-c", ("--count",), note="count 进度（5=设定 / +2=加 / -1=减）"),
)

#: `-e/--exclusive` 的真值写法（``true`` 也可以省略：裸写 ``-e`` 就是 true）。
TASK_TRUE_WORDS = ("true", "1", "yes", "y", "on", "t", "是", "开", "真", "独占")
TASK_FALSE_WORDS = ("false", "0", "no", "n", "off", "f", "否", "关", "假", "共享")


def _task_bool_word(word):
    """``true / 是 / 1`` → True，``false / 否 / 0`` → False，其它 → None。"""
    text = str(word or "").strip().lower()
    if text in TASK_TRUE_WORDS:
        return True
    if text in TASK_FALSE_WORDS:
        return False
    return None

#: `cmd_task_import` 挂起等待 y/n/q 时返回的哨兵：`cmd_task` 看见它就别再把
#: 后缀（-a / -n / -m / -t / -f）套到「上一次选中的任务」上，而是存进
#: ``pending_import["suffix"]``，等用户答完再落到真正导入的任务上。
IMPORT_PENDING = object()


class ImportBatch(list):
    """一次导入建出来的**全部**任务（顺序 = 剪贴板/粘贴顺序；单条时不用它）。

    为什么不是「返回最后一条」：`task import -a 3` / `task import -f end` 这类
    后缀要落到**每一条**上。多选 5 条 OmniFocus 任务再 `-f end`，用户要的是五条
    各自排进当天最后一段，不是只有最后一条进了面板 —— 所以 `cmd_task` 看见
    `ImportBatch` 就把后缀按条套一遍，最后返回最后一条当选中项。
    """

    @property
    def last(self):
        return self[-1] if self else None

    def task_ids(self):
        return [t.get("id") for t in self if isinstance(t, dict) and t.get("id")]


# 手动状态标记：`task 3 -m complete` / `task 3 --mark discard`。
# 这是**账本动作**，不是「完成/放弃」那套外部联动：
#   complete = 本地 completed + 归档（不勾 Focusmate、不当场写 OmniFocus）
#   discard  = 本地 dropped（完全不动 Focusmate / OmniFocus）
# 之后的 `task sync` / `refresh --check` 仍会按单向契约，把本地 completed 写回
# OmniFocus；discard 永远没有自动写回。
TASK_MARK_WORDS = ("complete", "discard")
TASK_MARK_STATES = {
    "complete": ts.STATUS_COMPLETED,
    "completed": ts.STATUS_COMPLETED,
    "done": ts.STATUS_COMPLETED,
    "完成": ts.STATUS_COMPLETED,
    "discard": ts.STATUS_DROPPED,
    "discarded": ts.STATUS_DROPPED,
    "drop": ts.STATUS_DROPPED,
    "dropped": ts.STATUS_DROPPED,
    "放弃": ts.STATUS_DROPPED,
}

# trigger / formatter：两个后缀共用一套「具名 preset + 参数」的解析与提示。
# trigger 默认是空的（框架先留出来，等真的需要再注册）；formatter 现在有
# end / enlist（enlist 永远排在其它 formatter 之后）。
TASK_HOOK_KINDS = ("trigger", "formatter")
TASK_HOOK_FLAGS = {
    "trigger": ("--trigger", "-t"),
    "formatter": ("--formatter", "-f"),
}
TASK_HOOK_LABELS = {"trigger": "trigger（创建触发）", "formatter": "formatter（排期算法）"}

# ---------------------------------------------------------------- 配色
# 终端背景**一定是深色**的（用户反馈："有些字根本看不见"）。所以这里只有三条规矩：
#   1. 不用暗色系：dark_blue / blue 在深色背景上等于隐形。
#   2. **数据不用 dim**：dim 是给说明、图例、单位这些「看漏了也不耽误事」的东西的；
#      任务标题、时间、状态这些要读的字一律给实色（灰也得是 grey62 这种亮灰）。
#   3. 颜色要有分工，同一个颜色别兼两份意思：
#      红 = 今天真要做的（已挂 session）· 蓝 = 还没挂 · 绿 = 完成 · 黄 = 警告 · 灰 = 收工的。
STYLE_MUTED = "grey62"            # 亮灰：次要但**要读得清**（收工的任务、占位的破折号）
STYLE_ASSIGNED = "bold bright_red"     # 已挂 session：今天得干的那条
STYLE_ACTIVE = "bold bright_blue"      # 还没挂 session
STYLE_DONE = "green"
STYLE_DROPPED = STYLE_MUTED

# 帧式重画：把上一帧从终端上抹掉，而不是 Console.clear() 整屏清。
# 上移 n 行（`\x1b[<n>A`）+ 回行首 + 清到屏幕末尾（`\r\x1b[J`）。
ERASE_FRAME = "\x1b[%dA\r\x1b[J"

# ---------------------------------------------------------------- 小工具

URL_RE = re.compile(r"https?://[^\s」』）)\]】,，；;]+")


def note_renderable(note, max_len=60):
    """备注 → 一行 rich 渲染件。

    需求背景：备注里可以塞一个网址，任务完成后照着去对应地方把活干完。
    所以看到网址就用 **OSC 8 超链接**（`Style(link=...)`）渲染 —— iTerm2 /
    Terminal.app / kitty 之类直接能点开；不支持的终端当普通文本看也不亏。
    """
    text = " ".join(str(note or "").split())
    if not text:
        return Text("")
    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        # 备注里除了网址还有别的话时，先显示那句话，再显示可点的网址
        head = text[:m.start()].strip(" ：:-—")
        out = Text()
        if head:
            out.append(head[:max_len] + "  ", style="dim")
        out.append("🔗 " + url[:max_len],
                   style=Style(color="cyan", underline=True, link=url))
        return out
    return Text("📝 " + text[:max_len], style="dim")


def now_local():
    return datetime.datetime.now().astimezone()


def quiet_call(fn, *a, **kw):
    """调 keep_session 里会 print 的函数，把它的输出吞掉（别弄脏 TUI）。"""
    box = []
    orig = ks.log

    def cap(msg, echo=True, to_file=True):
        box.append(str(msg))

    ks.log = cap
    try:
        rc = fn(*a, **kw)
    finally:
        ks.log = orig
    return rc, box


def fmt_age(seconds):
    if seconds < 90:
        return "%d 秒前" % seconds
    if seconds < 5400:
        return "%d 分钟前" % (seconds // 60)
    return "%d 小时前" % (seconds // 3600)


def fmt_mmss(seconds):
    """秒 → MM:SS（暂停倒计时用）。"""
    seconds = max(0, int(seconds))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


# ---------------------------------------------------------------- 宣言（The Walk of Shame）


# 一字不差敲完的羞辱宣言（152 字符）。少一个标点 = 继续锁死 + 鸣笛。
# 这是**唯一**的取消通道：没有密码、没有代币、没有特权。
# （你原文 99 字一字未改，后面 53 字是补到 150 你点头的那两句。）
DECLARATION = (
    "我在此确认，我选择屈服于短期的多巴胺诱惑，取消了本该执行的 Focusmate Session。"
    "我知道这会导致我今天的计划全部延误，但我依然选择成为一个被短视频和小说支配的懦夫。"
    "我愿意承担所有后果。"
    "我承认自己没有资格抱怨时间不够，也没有资格羡慕那些比我自律的人，"
    "更没有资格把这一切归咎于环境、状态或心情。"
)


def beep():
    """鸣笛。用终端 bell（\\a），不走 rich —— 免得被 rich 转义掉。"""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def first_mismatch(typed, want):
    """第一个不一样的字是第几个（1 起）。完全一致返回 0。"""
    n = min(len(typed), len(want))
    for i in range(n):
        if typed[i] != want[i]:
            return i + 1
    if len(typed) != len(want):
        return n + 1
    return 0


def diff_note(typed, want):
    """给一句人话的报错：哪里开始不对、少敲/多敲了多少。"""
    pos = first_mismatch(typed, want)
    if not pos:
        return ""
    if len(typed) < len(want) and want.startswith(typed):
        return ("你敲到第 %d 个字就没了（应该继续到「%s」），一共少 %d 个。"
                % (pos, want[pos - 1], len(want) - len(typed)))
    if len(typed) > len(want) and typed.startswith(want):
        return "你多敲了 %d 个字（宣言到「%s」就该结束）。" % (len(typed) - len(want), want[-1])
    got = typed[pos - 1] if pos - 1 < len(typed) else "（没了）"
    return ("第 %d 个字开始不一致：应该敲「%s」，你敲的是「%s」。"
            "（你 %d 个字，宣言 %d 个字）" % (pos, want[pos - 1], got, len(typed), len(want)))


def check_declaration(typed):
    """比对宣言。返回 (是否通过, 说明)。前导/尾随空白不算内容，中间一个字都不能错。"""
    got = (typed or "").strip()
    if got == DECLARATION:
        return True, ""
    if not got:
        return False, "你敲了空行。宣言一个字都不能少。"
    return False, diff_note(got, DECLARATION)


# ---------------------------------------------------------------- bridge 客户端


class BridgeError(RuntimeError):
    pass


#: 桥的「话」→ 人该干什么。认得出的才翻，认不出原样放过去（别猜）。
_AUTH_HINTS = ("未登录", "登录态", "Firebase token", "token 已过期", "重新登录", "AUTH_REQUIRED")


def explain_bridge_error(msg, profile=""):
    """把 bridge 回来的错翻成「下一步做什么」。

    踩过：登录态失效时前端只显示「内部调用了 process.exit(1)，已转成命令级失败」
    —— bridge 侧现在会把 `die()` 的真话拼在 `｜` 后面，这里再补上动作和
    profile（多 profile 时「读的是哪个」比错本身更关键）。
    """
    text = (msg or "").strip() or "未知错误"
    if any(k in text for k in _AUTH_HINTS):
        # 桥自己已经把「先跑 --auth」和 profile 写进话里了就别重复，只说漏的那句
        if "--auth" not in text:
            text += "　　下一步：`python3 main.py --auth` 重新登录一次"
        if profile and profile not in text:
            text += "（桥读的 profile：%s）" % profile
        return text
    if "process.exit(" in text and "｜" not in text:
        return text + "（bridge 的 die() 不走响应帧；真话在 %s）" % BRIDGE_LOG
    return text


#: 进程级缓存：Focusmate 运行目录（bridge 子进程和 daemon 必须同一个）。
_FOCUSMATE_HOME = ""
_FOCUSMATE_HOME_TRIED = False


def focusmate_home():
    """和 main.py / daemon 用**同一个**运行目录。

    踩过：bridge 没传 FOCUSMATE_CONFIG_DIR，`scripts/_lib.mjs` 就退回
    `~/.focusmate-mcp`（9 月的老 profile、登录态早过期），而 daemon 用工作区的
    `temp/focusmate-home`。结果 daemon 一切正常、TUI 每条命令都「未登录」。
    """
    global _FOCUSMATE_HOME, _FOCUSMATE_HOME_TRIED
    if _FOCUSMATE_HOME or _FOCUSMATE_HOME_TRIED:
        return _FOCUSMATE_HOME
    _FOCUSMATE_HOME_TRIED = True  # 失败也只试一次：这函数会被每帧的表头叫到
    try:
        import main as mainmod  # 只为了它那份「运行目录」逻辑，不启动任何东西
        home = mainmod.ensure_mcp_home()
    except Exception:
        home = ""
    _FOCUSMATE_HOME = home or ""
    return _FOCUSMATE_HOME


class Bridge(object):
    """scripts/bridge.mjs 的客户端：起进程、发命令、收响应。

    stdout 上是 JSON 行。这里用后台线程读，主线程带超时取，
    这样「浏览器冷启动 15s」不会把 UI 卡死到没救。
    """

    def __init__(self, console, headed=False, idle_ms=None, profile_dir=None):
        self.console = console
        self.headed = headed
        self.idle_ms = idle_ms
        self.profile_dir = profile_dir
        self.proc = None
        self.q = queue.Queue()
        self.reader = None
        self.next_id = 1
        self.events = []
        self.fatal = None
        self.stderr_path = BRIDGE_LOG
        #: 桥自己报上来的 profile 目录（ready / ping 里带回来的），报错时告诉人
        self.profile = ""
        self.last_idle_close = None
        # 命令是串行的（bridge 侧一条条跑），所以这边也上锁：
        # 收尾的后台核对线程和主线程可能同时说话，协议是「一问一答」的，
        # 两边同时写 stdin 会让响应和请求号对不上。
        self._lock = threading.Lock()

    # -- 生命周期

    def _child_env(self):
        """给 bridge 子进程的环境。

        **必须**把运行目录传下去：不传的话 `scripts/_lib.mjs` 会按自己的优先级
        猜，而它以前猜的是老位置 `~/.focusmate-mcp` —— 那儿的登录态早过期了。
        表现是 daemon 一切正常、TUI 每条命令都「未登录」（真踩过，见
        `focusmate_home()` 的注释）。
        """
        env = dict(os.environ)
        if self.headed:
            env["WARDEN_BRIDGE_HEADED"] = "1"
        if self.idle_ms is not None:
            env["WARDEN_BRIDGE_IDLE_MS"] = str(self.idle_ms)
        home = focusmate_home()
        if home:
            env["FOCUSMATE_CONFIG_DIR"] = home
        if self.profile_dir:
            env["FOCUSMATE_PROFILE_DIR"] = self.profile_dir
        return env

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = self._child_env()
        os.makedirs(TEMP, exist_ok=True)
        self.errf = open(self.stderr_path, "a")
        self.proc = subprocess.Popen(
            ["node", BRIDGE_JS],
            cwd=HERE,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errf,
            text=True,
            bufsize=1,
        )
        self.q = queue.Queue()
        self.reader = threading.Thread(target=self._read_loop, args=(self.proc,), daemon=True)
        self.reader.start()

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    self.q.put({"event": "protocol-error", "raw": line[:300]})
        except Exception as e:  # 进程被杀了之类
            self.q.put({"event": "reader-died", "error": str(e)})
        finally:
            self.q.put({"event": "eof"})

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                # 关 stdin 就够：bridge 会自己关浏览器再退
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            try:
                self.errf.close()
            except Exception:
                pass
            self.proc = None

    # -- 通信

    def _drain_events(self):
        """把队列里的事件帧消化掉（响应帧留在队列里给 call 取）。"""
        kept = []
        while True:
            try:
                f = self.q.get_nowait()
            except queue.Empty:
                break
            if f.get("event"):
                self.events.append(f)
                if f["event"] == "idle-closed":
                    self.last_idle_close = time.time()
                if f["event"] == "fatal":
                    self.fatal = f.get("error")
                if f.get("profileDir"):
                    self.profile = f["profileDir"]
            else:
                kept.append(f)
        for f in kept:
            self.q.put(f)

    def call(self, cmd, timeout=240, **kw):
        """发一条命令、等它的响应。

        ⚠️ kw 里**不能**出现 `id`：信封的请求号就叫 id，混了会把请求号覆盖掉，
        桥照常执行、响应也发，但这边永远等不到 —— 表现为卡死，而写入其实已经生效。
        （session 身份统一用 meetingId。）这里直接拦住，不给下次踩的机会。
        """
        if "id" in kw:
            raise BridgeError(
                "内部错误：call() 的 kw 里不能有 id（会和请求号撞名）。"
                "session 身份请用 meetingId=。"
            )
        with self._lock:
            return self._call_locked(cmd, timeout, kw)

    def _call_locked(self, cmd, timeout, kw):
        self.start()
        self._drain_events()
        req_id = self.next_id
        self.next_id += 1
        req = {"id": req_id, "cmd": cmd}
        req.update(kw)
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise BridgeError("bridge 进程没了（%s）；看 %s" % (e, self.stderr_path))

        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise BridgeError("bridge 超过 %ds 没回 %s（浏览器卡住了？看 %s）"
                                  % (timeout, cmd, self.stderr_path))
            try:
                f = self.q.get(timeout=min(left, 5))
            except queue.Empty:
                if not self.alive():
                    raise BridgeError("bridge 进程退出了（看 %s）" % self.stderr_path)
                continue
            if f.get("event"):
                self.events.append(f)
                if f["event"] == "idle-closed":
                    self.last_idle_close = time.time()
                if f["event"] == "fatal":
                    self.fatal = f.get("error")
                if f.get("profileDir"):
                    self.profile = f["profileDir"]
                if f["event"] == "eof":
                    raise BridgeError("bridge 的 stdout 关了（看 %s）" % self.stderr_path)
                continue
            if f.get("id") != req_id:
                # 不该发生（命令是串行的），但别把响应丢了
                continue
            if not f.get("ok"):
                raise BridgeError(
                    explain_bridge_error(f.get("error"), self.profile or self.profile_dir or ""))
            r = f.get("result") or {}
            if r.get("profileDir"):
                self.profile = r["profileDir"]
            return r

    def browser_state(self):
        """(bridge 活着?, 浏览器开着?) —— 用 ping，但不启动浏览器。"""
        if not self.alive():
            return False, False, ""
        try:
            r = self.call("ping", timeout=15)
        except BridgeError as e:
            return False, False, str(e)
        return True, bool(r.get("browser")), ""


# ---------------------------------------------------------------- 视图（Tab 切换）
#
# 可扩展：一个 View = 一块主体渲染 + 一套命令 + 一行提示。
# 加新视图：继承 View、填 key/name/label、实现 idle_hint()/render()/commands()，
# 然后用 @register_view 注册即可 —— paint() / 切换 / 命令提示都自动跟上。
# 未来想加「统计」「历史」视图，照抄 FocusView 的结构就行。

def _cmd(name, handler, summary, aliases=(), section="命令", usage="",
         options=(), hint=None, completer=None, values=None):
    """一条 repl_kit 命令。

    命令的「叫什么 / 干什么 / 怎么用 / 补全什么 / 提示什么」都放在这一条记录里，
    由 repl_kit 统一排版成 help 表和提示行 —— 和 paper_tui 用的是同一套。
    """
    kwargs = {}
    if completer is not None:
        kwargs["completer"] = completer
    if values:
        kwargs["values"] = dict(values)
    return repl_kit.Command(
        name=name, handler=handler, summary=summary, aliases=tuple(aliases),
        section=section, usage=usage, options=tuple(options), hint=hint, **kwargs
    )


# -- 提示行（repl_kit.Hint）-------------------------------------------------
#
# paper_tui 的提示行长这样：
#
#     命令: session  cancel  mute   (Tab 切专注视图)
#           ^ items 之间两个空格   ^ note 三个空格 + 括号
#
# 这里不手写字符串，统一交给 repl_kit.Hint 排版；命令自己的参数提示用
# Command.hint（框架会在输入了命令名之后自动换上去）。

def _hint(prefix, *items, note=""):
    """静态提示：`<prefix>: item  item   (note)`。"""
    def hook(_parts):
        return repl_kit.Hint(prefix, items, note=note)
    return hook


def _hint_actions(*items, note=""):
    return _hint("动作", *items, note=note)


def _hint_day(parts):
    return repl_kit.Hint("取值", ("-1", "0", "1", "YYYY-MM-DD"))


def _hint_session(parts):
    if "--permanent" not in " ".join(parts) and len(parts) <= 1:
        return repl_kit.Hint("参数", ("HH:MM", "25|50|75"),
                             note="--permanent 才写进日历；不写 = 只加这一天")
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("25", "50", "75"))
    if len(parts) >= 2:
        return repl_kit.Hint("取值", ("25", "50", "75"),
                             note="也可以写 11:15-12:05")
    return repl_kit.Hint("选项", ("--permanent",), note="写进日历（按星期几重复）")


def _hint_relocate(parts):
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("-30", "-15", "+15", "+30"))
    if len(parts) == 2:
        return repl_kit.Hint("参数", ("±MM",),
                             note="15 的倍数；不能跨天；撞车就拒绝")
    return repl_kit.Hint("参数", ("<n>", "T1"),
                         note="<n> = 单场；T1 = 整段（里面每场一起挪）")


def _hint_mute(parts):
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("on", "off"))
    return repl_kit.Hint("参数", ("<n>", "on|off"),
                         note="不写 on/off 就是取反")


def _hint_mute_universal(parts):
    if len(parts) >= 2 and str(parts[1]).lower() in ("off", "unmute", "清空", "取消"):
        return repl_kit.Hint("参数", ("13:00", "-", "15:00", "Mon", "清空"),
                             note="按星期几 + 时间窗撤销；只写 off = 清空全部通用静音")
    if parts:
        return repl_kit.Hint("时间窗", ("13:00", "-", "15:00", "Mon", "Tue", "Wed"),
                             note="每个指定星期几，凡与这个窗口有重叠的 session 都静音")
    return repl_kit.Hint("参数", ("13:00 - 15:00 Mon", "off", "list"),
                         note="例：mute-universal 13:00 - 15:00 Mon；"
                              "14:30–15:30 这种只碰到尾巴的 session 也算")


def _hint_title(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("参数", ('"文本"',), note='"" = 清空')
    return repl_kit.Hint("参数", ("<n>", '"文本"'))


def _hint_cancel(parts):
    if "--permanent" not in " ".join(parts):
        return repl_kit.Hint("参数", ("<n>", "--permanent"),
                             note="默认只弃这一场；--permanent 才从日历删掉")
    return repl_kit.Hint("参数", ("<n>",), note="永久删掉日历里这个 session")


class _TaskCompletionValues(dict):
    """``task`` 的 flag→候选 表；trigger/formatter 的候选**动态**从注册表读。

    必须是 Mapping 而不是普通 dict：``-t`` 在 trigger 注册表为空时要明确返回
    「没有候选」，而不是让 shell 落到「把全部 flag 当候选」那条兜底分支 ——
    否则 `task -t <Tab>` 会莫名其妙列出一堆选项。以后注册了 trigger，Tab 立刻
    就能看到，不需要重启 Warden 或重建 Command。
    """

    def __contains__(self, key):
        if key in TASK_HOOK_FLAGS["trigger"] or key in TASK_HOOK_FLAGS["formatter"]:
            return True
        return super().__contains__(key)

    def __getitem__(self, key):
        if key in TASK_HOOK_FLAGS["trigger"]:
            return th.preset_names("trigger")
        if key in TASK_HOOK_FLAGS["formatter"]:
            return th.preset_names("formatter")
        return super().__getitem__(key)


def _task_command(w):
    """``task`` 那一条命令：动作、后缀、紧凑短选项、补全都写在一个地方。

    紧凑短选项走 repl_kit 的 ``short`` 表（``-an`` ≡ ``-a none``）：解析、补全、
    提示三件事都由框架统一办，这里只声明一次（:data:`TASK_SHORT_FLAGS`）。
    """
    return repl_kit.Command(
        name="task",
        handler=w.cmd_task,
        summary="任务 CRUD / 挂到 session / 手动标记状态 / trigger + formatter / "
                "同步完成时间 / 导入 OmniFocus 项目",
        aliases=(),
        section="通用",
        usage="task <create|import|delete|modify|note|assign|select|swap|repeat|sync|archive>"
              " ... [-a <编号|none|current>] [-s <编号>] [-n 备注] [-e true|false]"
              " [-m complete|discard] [-c 5|+2|-1]"
              " [-t <trigger> [参数...]] [-f <formatter> [参数...]]",
        # 把 flag 放在 action 词前面：补全列表有 20 项上限，flag 被挤到后面
        # 就等于「有这个选项但 Tab 永远看不到」。
        # 紧凑短选项（-an/-ac/-m/-t/-f）由 task_completer 的 short 表补，不要
        # 在 options 里再写一遍 —— 否则 Tab 菜单里会出现两个一模一样的候选。
        options=("--assign", "-a", "--note", "-n", "--from", "-s", "--exclusive", "-e",
                 "--mark", "--count", "--trigger",
                 "--formatter", "--repeat", "--single-task",
                 "create", "import", "delete", "modify", "note", "assign", "select",
                 "swap", "repeat", "sync", "archive", "inspect-due"),
        values=_TaskCompletionValues({"--mark": TASK_MARK_WORDS,
                                      "-m": TASK_MARK_WORDS}),
        short=TASK_SHORT_FLAGS,
        completer=lambda prior: task_completer(w, prior),
        hint=_hint_task,
    )


def task_completer(w, prior):
    """``task`` 的补全：``-a`` 后面给「编号 + none + current」，动作位置给动作词。

    ``-a`` / ``-s`` 后面补的是**表格编号**（这才是它们要的东西），另加两个特殊
    取值 —— 不给它们，``-an`` / ``-ac`` 就等于只存在于文档里。
    """
    words = [str(part).lower() for part in prior]
    if len(words) <= 1:
        # `task ` 后面最常敲的就是「第几条」—— 直接补的是面板序号（1..N），
        # 不是内部 id（那两套数不是一回事，补错了会让人敲到别的任务上）。
        try:
            return [str(i) for i in range(1, len(w.task_panel_order()) + 1)]
        except Exception:                            # noqa: BLE001 - 补不出来就算了
            return []
    if words and words[-1] in ("--assign", "-a", "-an", "-ac"):
        return [str(i) for i in range(1, len(w.rows) + 1)] + ["none", "current"]
    if words and words[-1] in ("--from", "-s"):
        return [str(i) for i in range(1, len(w.rows) + 1)]
    if words and words[-1] in ("--count", "-c"):
        # count 后面就是一个整数；把最常用的三个形状摆出来（+1 / +2 / -1）。
        return ["+1", "+2", "-1"]
    if words and words[-1] in TASK_HOOK_FLAGS["trigger"]:
        # 默认没有 trigger —— 返回空列表就是「空的 function 列表」。
        return list(th.preset_names("trigger"))
    if words and words[-1] in TASK_HOOK_FLAGS["formatter"]:
        return list(th.preset_names("formatter"))
    return []


def mute_window_for(day, hhmm, end_hhmm=None):
    """这一场的起点/区间落在哪条静音窗里 → 记录；没有就是 None。

    口径只认 :mod:`session_plan` 里那张表，别处不要自己算 —— 表格的「静音」
    列和自动 quiet 都问这一个函数。

    - ``period mute``：旧语义，session **起点**在窗内就算；
    - ``mute-universal``：只要 session 与窗口有重叠就算（14:30–15:30 对
      13:00–15:00 也算）。后者必须给 ``end_hhmm``；没有终点时退化为起点判定。
    （daemon 的分心看门狗**不**问它：静音只改 Focusmate 那一场，本地该响还响。）
    """
    got = sp.parse_hhmm(hhmm)
    if got is None:
        return None
    try:
        moment = datetime.datetime.combine(day, datetime.time(got[0], got[1]))
        window = sp.mute_window_at(moment, data=mute_data())
        if window is not None:
            return window
        if end_hhmm:
            return sp.mute_overlaps_window(day, hhmm, end_hhmm,
                                           data=mute_data(), universal_only=True)
        return None
    except Exception:                       # noqa: BLE001 - 静音表坏了不该没表格
        return None


_MUTE_CACHE = {"at": 0.0, "data": None}


def mute_data(force=False):
    """静音表（2 秒缓存）。表格每一行都要问它，别每行读一次盘。"""
    now = time.time()
    if not force and _MUTE_CACHE["data"] is not None and now - _MUTE_CACHE["at"] < 2.0:
        return _MUTE_CACHE["data"]
    _MUTE_CACHE["data"] = sp.load_mutes()
    _MUTE_CACHE["at"] = now
    return _MUTE_CACHE["data"]


def forget_mute_cache():
    """改过静音表之后清一下（``period mute`` 立刻生效，不用等 2 秒）。"""
    _MUTE_CACHE["data"] = None
    _MUTE_CACHE["at"] = 0.0


def _hint_sync(parts):
    return repl_kit.Hint("选项", ("--days",), note="日历 → data.csv")


def _hint_stat(parts):
    if len(parts) >= 2 and parts[1] == "refresh":
        return repl_kit.Hint("动作", ("full",), note="不加 full = 增量刷新")
    return repl_kit.Hint("动作", ("refresh", "refresh full"),
                         note="默认走 1 小时缓存")


def _hint_period(parts):
    if parts and str(parts[0]).lower() in ("mute", "unmute", "静音", "解除静音", "取消静音"):
        return repl_kit.Hint("参数", ("T1", "--permanent", "--off"),
                             note="静音这个时段（--permanent = 以后每个这个星期几都静音）")
    if len(parts) >= 2:
        return repl_kit.Hint("时长", ("25", "50", "75"), note="可连续给多个；超时就报错")
    return repl_kit.Hint("参数", ("T1", "50", "50", "mute T1"), note="按 25/50/75 排这个时段；"
                             "period mute T1 --permanent = 这个时段以后都静音")


def _hint_set(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("取值", ("on", "off"), note="不写 = 恢复默认")
    return repl_kit.Hint("参数", ("<设置 id>", "<value>"),
                         note="完整清单在「设置」视图；不写 value = 恢复默认")


def _hint_refresh(parts):
    if any(str(part).lower() in ("--check", "-c", "check") for part in parts):
        return repl_kit.Hint("参数", ("（跑起来了）",),
                             note="查「该订没订上」「该挂没挂上」，能修的当场修")
    return repl_kit.Hint("选项", ("--check", "-c"),
                         note="不光重拉：还叫 daemon 查一遍、把不对的修掉")


TASK_ACTION_WORDS = ("create", "import", "delete", "modify", "note", "assign", "select",
                     "swap", "repeat", "sync", "archive")

#: 中文「每周几」的显示名（只用于消息；内部一律用 ts.WEEKDAYS）。
CN_WEEKDAY_CHARS = "一二三四五六日"


def _parse_local_iso(raw):
    """``2026-11-04T10:45:00`` → naive datetime；认不出返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def task_repeat_plan_from_omnifocus(info, sessions_for_day=None):
    """把 OmniFocus 的重复规则原样搬成本地 ``repeat``。

    **不翻译成「每周几」再丢掉信息**：``rule`` 保留 OmniFocus 的 ICS
    ``ruleString``，日期交给 dateutil 展开；``start`` / ``duration`` 只是用来
    在当天找到对应的 Focusmate session。`DAILY`、多个 `BYDAY`、`INTERVAL`、
    `COUNT` / `UNTIL` 都按原规则走。

    ``info`` 是 :meth:`omnifocus.Client.repetition_info` 的结果。
    ``sessions_for_day(day)`` 可选：给了就去那天的排期里验证「OF 的哪个时间
    才是 Focusmate 的 session 起点」（OmniFocus 的 due 可能记的是结束时间），
    能让复制过来的 ``start`` 更靠谱。

    返回::

        {"supported": True, "rule": "FREQ=DAILY",
         "rule_anchor": "2026-11-04T11:10:00", "rule_offset_days": 0,
         "weekday": 2, "weekday_label": "wed", "start": "10:45",
         "duration": 25, "summary": "每天 10:45（25 min）",
         "source": "due-duration"}

    或 ``{"supported": False, "reason": "...", "rule_string": ...}``。
    非重复任务返回 ``None``。
    """
    if not isinstance(info, dict) or not info.get("repeated"):
        return None
    rule_string = ts.normalize_rule(info.get("rule_string"))
    if not rule_string:
        return {
            "supported": False,
            "rule_string": "",
            "reason": "OmniFocus 说是重复任务，但没拿到 repetitionRule.ruleString",
        }

    duration = info.get("estimated_minutes")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 0:
        duration = None

    # OmniFocus 的 due 更常见的是「截止 = 结束」，但 planned / defer 可能是起点。
    # 三个候选都留着，能对到那天真实 session 的那个优先。
    candidates = []
    planned = _parse_local_iso(info.get("planned"))
    due = _parse_local_iso(info.get("due"))
    deferred = _parse_local_iso(info.get("defer"))
    nxt = _parse_local_iso(info.get("next"))
    if planned:
        candidates.append(("planned", planned, planned))
    if due and duration:
        candidates.append(("due-duration", due - datetime.timedelta(minutes=duration), due))
    if due:
        candidates.append(("due", due, due))
    if deferred:
        candidates.append(("defer", deferred, deferred))
    if nxt and duration:
        candidates.append(("next-duration",
                           nxt - datetime.timedelta(minutes=duration), nxt))
    if nxt:
        candidates.append(("next", nxt, nxt))
    if not candidates:
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": "规则里没有 planned / due / defer / next 时间，推不出会被哪场 session 接住",
        }

    chosen = None
    chosen_reason = ""
    if sessions_for_day is not None:
        for reason, start_dt, anchor_dt in candidates:
            try:
                sessions = sessions_for_day(start_dt.date()) or []
            except Exception:                       # noqa: BLE001 - 排期读不到就退回静态推导
                sessions = []
            start_label = start_dt.strftime("%H:%M")
            for sess in sessions:
                if sess.get("start") != start_label:
                    continue
                if duration is not None and sess.get("duration") is not None:
                    try:
                        if int(sess.get("duration")) != duration:
                            continue
                    except (TypeError, ValueError):
                        pass
                chosen = (reason, start_dt, anchor_dt)
                chosen_reason = "排期里对上了 %s 那场 session" % start_label
                break
            if chosen:
                break

    if chosen is None:
        # 没排期可对：优先 planned（这是 OF 里最像「我打算几点做」的字段），
        # 其次 due - duration（warden 自己 assign 时就是把 due 写成 session 终点），
        # 最后才是 due 本身。
        for reason, start_dt, anchor_dt in candidates:
            if start_dt.minute % 15 == 0:
                chosen = (reason, start_dt, anchor_dt)
                chosen_reason = "按 OmniFocus 的 %s 推导（没在排期里找到可核对的 session）" % reason
                break
    if chosen is None:
        start_dt = candidates[0][1]
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": "推导出的起点 %s 不在 Focusmate 的 15 分钟网格上"
                      % start_dt.strftime("%H:%M"),
        }

    reason, start_dt, anchor_dt = chosen
    ok, why = ts.validate_rule(rule_string, anchor_dt.isoformat(timespec="seconds"))
    if not ok:
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": why or "RRULE 解析不了",
        }
    # 本地 repeat 锚的是「session 起点」，不是 OmniFocus 的到期时刻。跨午夜时
    # due 可能是周四 00:05、场次却是周三 23:15：RRULE 仍用原始 due 当 DTSTART，
    # 但把日期差记进 rule_offset_days，让本地按场次起点那天的星期去查规则。
    offset = (start_dt.date() - anchor_dt.date()).days
    weekday = start_dt.weekday()
    start = start_dt.strftime("%H:%M")
    rep = {
        "rule": rule_string,
        "rule_anchor": anchor_dt.isoformat(timespec="seconds"),
        "rule_offset_days": offset,
        "weekday": weekday,
        "weekday_label": ts.WEEKDAYS[weekday],
        "start": start,
        "duration": duration,
    }
    if offset:
        chosen_reason += ("；规则锚点是周%s，场次起点在周%s（跨午夜），"
                          "本地按场次起点记"
                          % (CN_WEEKDAY_CHARS[anchor_dt.weekday()],
                             CN_WEEKDAY_CHARS[weekday]))
    return {
        "supported": True,
        "rule_string": rule_string,
        "rule": rule_string,
        "rule_anchor": rep["rule_anchor"],
        "rule_offset_days": offset,
        "weekday": weekday,
        "weekday_label": ts.WEEKDAYS[weekday],
        "start": start,
        "duration": duration,
        "summary": ts.repeat_summary(rep),
        "source": reason,
        "anchor": anchor_dt.isoformat(timespec="seconds"),
        "note": chosen_reason,
    }


def _hint_task(parts):
    # repl_kit 递进来的是**整行**的词（含命令名，含正在敲的那半个词），
    # 而这里只关心 `task` 后面的参数 —— 先把这个命令名摘掉。
    words = [str(part).lower() for part in parts]
    if words and words[0] == "task":
        words = words[1:]
    if not words:
        return repl_kit.Hint("动作", TASK_ACTION_WORDS,
                             note="后缀：-a <编号|none|current> · -s <编号> 批量 · -n 备注"
                                  " · -e true|false 独占 · -m complete|discard · 字段随便写")
    if any(tok in words for tok in ("--mark", "-m")):
        return repl_kit.Hint("取值", TASK_MARK_WORDS,
                             note="手动改本地状态：complete = 完成并归档（不勾 Focusmate）"
                                  "· discard = 放弃（不动外部）")
    for kind in TASK_HOOK_KINDS:
        flags = TASK_HOOK_FLAGS[kind]
        if not any(tok in words for tok in flags):
            continue
        names = th.preset_names(kind)
        if words and words[-1] in flags:
            note = ("当前没有注册任何 %s 预设（框架已就位，列表为空）" % kind
                    if not names else
                    "选中预设后再跟 key=value 参数；Tab 会补可用名字")
            return repl_kit.Hint("预设", names or ("（空列表）",), note=note)
        if kind == "formatter":
            return repl_kit.Hint(
                "参数", names or ("（空列表）",),
                note="end = 在当天最后一段的尾巴上切一场任务大小的 session"
                     "（11:30–11:55 那种，时长取 25/50/75）并自动挂上；"
                     "可带 date=YYYY-MM-DD / tomorrow / today、break_minutes=5")
        return repl_kit.Hint(
            "参数", names or ("（空列表）",),
            note="trigger 预设先注册函数才会有名字；"
                 "用法：task <序号> -t <函数> [参数...]")
    if any(tok in words for tok in ("--exclusive", "-e")):
        return repl_kit.Hint("取值", ("true", "false"),
                             note="独占 = 这一场 session 只能有它一条任务"
                                  "（end formatter 排出来的任务自动独占；裸写 -e 就是 true）")
    if any(tok in words for tok in ("-an", "-ac")):
        return repl_kit.Hint("参数", ("（不用再跟东西）",),
                             note="-an ≡ -a none（下架）· -ac ≡ -a current（当前这一场）")
    if any(tok in words for tok in ("-s", "--from")):
        return repl_kit.Hint("参数", ("-a <编号>", "-a none"),
                             note="把这一场上的全部任务转移到那一场 / 全部下架；"
                                  "没有 -a 就只列出来")
    if any(tok in words for tok in ("--assign", "-a")):
        return repl_kit.Hint(
            "取值", ("<编号>", "none", "current"),
            note="none = 下架（删掉 Focusmate 那条 task）；current = 当前这一场；"
                 "紧凑写法 " + repl_kit.flags.describe(TASK_SHORT_FLAGS, with_words=False))
    # `task 32 swap 21`：动作写在 ID 后面。提示行拿到的是「已经敲完的词」，
    # 所以 `task 32 ` 这时候 parts 只有 ["32"] —— 提示该给「这个 ID 能干什么」。
    head = list(words)
    if head and (head[0].isdigit() or head[0].startswith("id:")):
        if len(head) == 1:
            return repl_kit.Hint("动作", ("swap", "duplicate", "drop", "done", "link",
                                          "-m complete|discard", "-f end", "-e true",
                                          "-a <编号>"),
                                 note="数字是面板序号（swap 5 = 和第 5 条换位置）"
                                      " · -m 手动标状态 · -f end 排到当天最后一段"
                                      " · -e true 独占一整场 · -a none 下架"
                                      " · -a current 挂到当前这一场")
        head = head[1:]
    action = head[0] if head else ""
    if action in ("import", "粘贴", "导入"):
        return repl_kit.Hint("参数", ("（不写）", "[标题](omnifocus:///task/PK)", "-a 3",
                                      "-st"),
                             note="不写参数 = 读剪贴板（⌘V 到终端只剩标题，这条不会丢链接）；"
                                  "OmniFocus 里多选几条一起 ⌘C 就是一次导入多条（标题/时长"
                                  "按 pk 去 OmniFocus 查），后缀 -a/-f 对整批生效；"
                                  "OmniFocus 里是重复任务时会先问要不要复制重复设置；"
                                  "-st/--single-task = 只导入这一次，不问也不建 repeat")
    if action in ("link", "关联"):
        return repl_kit.Hint("参数", ("（不写）", "omnifocus:///task/PK", "PK"),
                             note="不给链接 = 读剪贴板")
    if action == "create":
        return repl_kit.Hint("参数", ("[id]", "key=value...", "--repeat 周几@HH:MM",
                                      "--assign N", "--note 备注"),
                             note="不给 id 就自动编号 · --note 塞网址最有用")
    if action in ("archive", "history", "归档"):
        return repl_kit.Hint("参数", ("<N>",), note="不给就列最近 5 条")
    if action in ("repeat", "recurring"):
        return repl_kit.Hint("参数", ("<序号>", "周几@HH:MM|off"),
                             note="手写只建每周规则；OmniFocus 导入的任务保留原 RRULE，"
                                  "task repeat <序号> 可直接看")
    if action in ("note", "notes", "备注"):
        return repl_kit.Hint("参数", ("<序号>", "<备注文本 / 网址>"),
                             note="备注会跟着完成归档一起显示（网址可点）")
    if action in ("delete", "select"):
        return repl_kit.Hint("参数", ("<序号>",), note="面板上第几条；面板外的用 id:N")
    if action in ("swap", "调换", "换位"):
        return repl_kit.Hint("参数", ("<序号>", "<序号>"),
                             note="两条任务互换位置（一条赋闲也行）：task 32 swap 21")
    if action == "modify":
        return repl_kit.Hint("参数", ("<任务 ID>", "<字段>", "<新值>"), note="字段不校验")
    if action == "assign":
        return repl_kit.Hint("参数", ("<session ID>",),
                             note="默认挂当前选中的任务；也可 assign <任务ID> <sessionID>")
    return repl_kit.Hint("参数", TASK_ACTION_WORDS,
                         note="任何动作都能带 -a <编号|none|current> / -n 备注"
                              " / -e true|false 独占 / -m complete|discard"
                              " / -t trigger / -f formatter；"
                              "-s <编号> 是批量（那一场的全部任务）")


class View(object):
    """一个视图 = 一套命令 + 一块看板 + 一行状态。

    业务留在子类里（render / status_line / cmd_* / idle_hint），
    对外统一经过 to_view() 变成 repl_kit 的 View：命令表、提示行、
    看板、状态行、进视图时的钩子、提示符，全由框架接管。
    """

    key = "?"
    name = "view"
    label = "视图"

    def __init__(self, w):
        self.w = w

    def render(self):
        """本视图的主体内容：rich renderable，或它们的列表。返回 None = 不渲染主体。"""
        return None

    def status_line(self):
        """提示符上方那行「随时在变的数字」，纯文本（无 markup）。None = 不需要定时刷新。"""
        return None

    def status_box(self):
        """「状态」框里本视图自己的那几行；None = 这个视图不要框。

        警告由框架追加（repl_kit 的 WarningBoard），这里只管业务状态。
        """
        return None

    def idle_hint(self):
        """没在输入命令时的那行提示：返回 repl_kit.Hint（或 None = 用框架默认）。"""
        return None

    def commands(self):
        """本视图认识的命令：[(repl_kit.Command, ...)]。"""
        return []

    def unknown_hint(self):
        return "「%s」视图不认这个命令（Shift+Tab 切视图；help 看当前视图的命令）。" % self.name

    # -- 所有视图都认的命令 --------------------------------------------

    def common_commands(self):
        w = self.w
        return [
            _cmd("quit", lambda a: w.exit_tui(),
                 "退出（bridge 和浏览器一起收掉）", ("exit", "q"), "通用",
                 usage="quit", hint=_hint_actions("quit", note="bridge 和浏览器一起收掉")),
            _cmd("help", w.cmd_help, "命令列表（help --task = 任务视图详细说明）", ("?", "h"), "通用",
                 usage="help [--task]", hint=_hint_actions("help", "--task", note="当前视图的全部命令")),
            _cmd("view", lambda a: w.cycle_view(),
                 "下一个视图", ("tab", "switch"), "通用",
                 usage="view", hint=_hint_actions("view", note="等同 Shift+Tab")),
            _cmd("open", w.cmd_open,
                 "open working|detail 切口径；open <表头> [--permanent] 打开当前表的一列",
                 ("看板",), "通用", usage="open working|detail | open <列名> [--permanent]",
                 options=tuple(dict.fromkeys(
                     ("working", "detail")
                     + tuple(h for h, _kw in SESSION_TABLE_COLUMNS)
                     + tuple(h for h, _kw in TASK_TABLE_COLUMNS)
                     + (TASK_CONTENT_COLUMN,))),
                 hint=_hint_actions("working", "detail", "时段类型", "id", "状态",
                                    note="open 表头名 = 展开列；任务视图操作任务表")),
            _cmd("close", w.cmd_close,
                 "close <表头> [--permanent]：折叠当前表的一列（--permanent = 永久折叠）",
                 ("关闭",), "通用", usage="close <列名> [--permanent]",
                 options=tuple(dict.fromkeys(
                     tuple(h for h, _kw in SESSION_TABLE_COLUMNS)
                     + tuple(h for h, _kw in TASK_TABLE_COLUMNS)
                     + (TASK_CONTENT_COLUMN, "--permanent"))),
                 hint=_hint_actions("时段类型", "id", "状态",
                                    note="close 表头名 = 折叠列；任务视图操作任务表")),
            _cmd("stat", w.cmd_stat_from_args, "生涯统计（stat refresh / full）",
                 ("stats",), "通用", usage="stat [refresh [full]]",
                 options=("refresh", "full"), hint=_hint_stat),
            _cmd("status", lambda a: None, "只重画看板", ("s",), "通用",
                 usage="status", hint=_hint_actions("status", note="只重画看板")),
            _cmd("set", w.cmd_set,
                 "改一个设置：set <id> <value>（不写 value = 恢复默认；任何视图都能敲）",
                 usage="set <设置 id> <value>",
                 completer=lambda prior: [row["id"] for row in w.settings.rows()],
                 hint=_hint_set),
            _cmd("warn", w.cmd_warn,
                 "状态框里的警告：warn on|off（不写 = 取反）",
                 ("warnings",), "通用", usage="warn [on|off]",
                 options=("on", "off"),
                 hint=_hint_actions("on", "off", note="不写就是取反")),
            _task_command(w),
        # say 用 lambda 晚绑定：换 console（测试、--exec）时输出跟着走
        ] + repl_kit.job_commands(w.jobs, say=lambda text: w.shell.print(text))

    def build_registry(self):
        w = self.w
        guarded = [
            dataclasses.replace(command, handler=w.guard(command.handler))
            for command in self.commands() + self.common_commands()
        ]
        return repl_kit.Registry(guarded)

    def hint_line(self, text, parts):
        """repl_kit 的提示行回调。

        空行（没在输入）→ 本视图的 idle_hint；已经开始输入 → None，
        让框架去问 `Command.hint`（参数 / 取值 / 选项），没有就回落到
        框架自动生成的 `命令: ...`。这和 paper_tui 的钩子完全同构。
        """
        if parts:
            return None
        return self.idle_hint()

    def to_view(self):
        """变成框架认识的那个 View。"""
        w = self.w
        return repl_kit.View(
            name=self.name,
            registry=self.build_registry(),
            hint=self.hint_line,
            status=self.status_line,
            status_box=self.status_box,
            render=w.frame,
            on_enter=w.enter_view,
            prompt=w.prompt_text,
        )


VIEWS = []


def register_view(cls):
    VIEWS.append(cls)
    return cls


@register_view
class ContractView(View):
    """契约 / 预约视图：就是原来那套表格 + 命令。"""

    key = "1"
    name = "契约"
    label = "契约 / 预约"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令",
            ("session", "period", "cancel", "mute", "mute-universal",
             "title", "day", "sync", "open working", "open detail",
             "close 时段类型", "help", "quit"),
            note="Tab 切下一个视图；open/close + 表头名折叠列；open working|detail 切口径",
        )

    def status_box(self):
        """「状态」框里属于本视图的那几行（警告由框架追加在后面）。"""
        return self.w.render_status_rows()

    def render(self):
        # 收尾面板不在这儿画：它是 repl_kit 的「打断栏位」（见 render_interrogate），
        # 换视图 / 表格变长都不该把它挤走。
        out = [self.w.render_table()]
        if self.w.rows_note:
            out.append(Text("  " + self.w.rows_note, style="dim"))
        # 看未来日期时，额外给一张「这一天每个 session 挂什么任务、属于哪个项目」
        # 的编排表；今天已经有契约表 + 任务面板，不重复占高度。
        binder = self.w.render_day_binder()
        if binder is not None:
            out.append(binder)
        # 契约表格下面那块任务表格（Recent 由框架接在后面）
        index = self.w.render_task_index()
        if index is not None:
            out.append(index)
        # 任务面板**下面**那块项目表（剩余 count / 进度 / 今天排了多少）
        projects = self.w.render_projects_table()
        if projects is not None:
            out.append(projects)
        return out

    def unknown_hint(self):
        return "「契约」视图不认这个命令（Shift+Tab 切视图；help 看当前视图的命令）。"

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", w.cmd_refresh,
                 "全量刷新看板（session/任务完成状态/统计/收尾面板）；"
                 "--check 再对账并修正", ("r", "reload"),
                 usage="refresh [--check]",
                 options=("--check", "-c"),
                 hint=_hint_refresh),
            _cmd("day", w.cmd_day, "切换看哪一天（-1/0/1 或 YYYY-MM-DD）",
                 usage="day <-1|0|1|YYYY-MM-DD>",
                 options=("-1", "0", "1"),
                 hint=_hint_day),
            _cmd("session", w.cmd_session,
                 "加一场（默认只加这一天；--permanent 才写进日历）",
                 usage="session [--permanent] <HH:MM> <25|50|75> | session <T1> [25|50|75 ...]",
                 options=("--permanent", "25", "50", "75"),
                 hint=_hint_session),
            _cmd("period", w.cmd_period_layout,
                 "改一个时段的排布（period T1 50 50）或静音它（period mute T1）",
                 usage="period <T1> [25|50|75 ...] | period mute|unmute <T1> [--permanent]",
                 options=("25", "50", "75", "mute", "unmute", "--permanent"),
                 hint=_hint_period),
            _cmd("relocate", w.cmd_relocate,
                 "临时挪一场（#N）或整个时段（T1）：relocate N ±MM / relocate T1 ±MM",
                 usage="relocate <n>|T1 ±MM",
                 options=("-30", "-15", "+15", "+30"),
                 hint=_hint_relocate),
            _cmd("force-check", lambda a: w.cmd_force_check(),
                 "忽略重试上限，把所有没订上的各强制重订一次", ("forcecheck",),
                 usage="force-check",
                 hint=_hint_actions("force-check", note="忽略重试上限，全部重订一次")),
            _cmd("sync", w.cmd_sync, "跑 download_calendar.py populate：日历 → data.csv",
                 usage="sync [--days N]", options=("--days",),
                 hint=_hint_sync),
            _cmd("mute", w.cmd_mute, "把 #N 静音：mute N on|off",
                 usage="mute <n> on|off", options=("on", "off"),
                 hint=_hint_mute),
            _cmd("mute-universal", w.cmd_mute_universal,
                 "每周固定时间窗静音：所有与窗口有重叠的 session（不绑 T 时段）",
                 ("unmute-universal", "universal-mute"),
                 usage="mute-universal 13:00 - 15:00 Mon | mute-universal off ... | list",
                 options=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
                          "off", "list"),
                 hint=_hint_mute_universal),
            _cmd("title", w.cmd_title, '把 #N 的标题改成文本：title N "文本"', ("t",),
                 usage='title <n> "文本"', hint=_hint_title),
            _cmd("cancel", w.cmd_cancel, "取消 #N（默认只弃这一场；--permanent 删日历）",
                 usage="cancel <n> [--permanent]", hint=_hint_cancel),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="结束后 15 秒 daemon 会自动拉你回来收尾")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E/G）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
            _cmd("plan", w.cmd_plan,
                 "把这一天的空闲切成 count 分给项目，再自动排任务进 session",
                 ("排期", "分配"),
                 usage="plan [+1|今天|明天|YYYY-MM-DD] → "
                       "task <序号> <count> | task <序号> clear | complete",
                 options=("+1", "+2", "today", "明天", "complete", "task 1 4"),
                 hint=_hint_actions("+1", "明天", "complete", "task 1 4", "task 1 clear",
                                    note="不带参数 = 今天；plan +1 = 明天；"
                                         "序号 = 项目表上的 #；超预算整条命令不执行")),
            _cmd("project", w.cmd_project,
                 "管理真正项目 Project (pro)：pro | pro 1 | pro 1 -a | pro 1 file <路径> | pro create <名称>",
                 ("pro", "pr", "项目"),
                 usage="pro [<序号> [-a|--advance [op <N>|task import] | file <路径> | open | refresh | remove] | create <名称>]",
                 options=("--advance", "-a", "create", "file", "open", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -a", "1 file", "1 open", "create", "1 refresh", "1 remove",
                                    note="pro = 真正项目（stage: running/bottleneck）；-a 快捷切换阶段并关联新 op")),
            _cmd("op", w.cmd_op,
                 "管理 OmniFocus Project (op)：op | op 1 | op 1 sop|incubator | op 1 relate --export|--import | op import [sop|incubator]",
                 ("ofp", "omnifocus-project"),
                 usage="op [<序号> [sop|incubator|pro <N>|relate --export|--import [路径]|refresh|remove] | import [sop|incubator]]",
                 options=("sop", "incubator", "import", "relate", "--export", "--import", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 sop", "1 incubator", "1 relate --export", "1 relate --import", "import sop", "import incubator", "refresh",
                                    note="op = OmniFocus Project（SOP 或 incubator 两种类型）")),
            _cmd("start", lambda a: w.cmd_start(), "启动 daemon",
                 usage="start", hint=_hint_actions("start", note="启动 daemon")),
            _cmd("stop", lambda a: w.cmd_stop(), "停用 daemon（要敲宣言）",
                 usage="stop", hint=_hint_actions("stop", note="停用 daemon（要敲宣言）")),
        ]


@register_view
class FocusView(View):
    """专注视图：当前目标 + 当前 session + 专注模式 + 可暂停次数 + 当前空闲时间。

    这个视图里：`stop` = 消耗一次暂停（暂停 PAUSE_MINUTES 分钟），Shift+Tab =
    循环切换普通 / 做题 / 写作模式。之前那套预约命令在这里**一条都不认** ——
    你要改预约就按 Tab 切回去。
    """
    key = "2"
    name = "专注"
    label = "专注 / 暂停"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("stop",),
            note="消耗一次暂停 · Shift+Tab 切模式 · Tab 切回契约",
        )

    def current_row(self):
        """当前真正在进行的 session 行；没有就 None。

        API 数据权威时只认**有 session id 且时间窗口覆盖现在**的行：
        `data.csv` 里的计划窗口只是预约意图，用户取消后另约了别的时间时，
        不能拿旧的计划窗口继续冒充「我正在这一场」。
        离线/快照模式没有 id，才退回按契约时间判断。
        """
        w = self.w
        if w.authoritative:
            live = w.live_session()
            return live[1] if live else None
        sess = ks.current_session(w.entries, datetime.datetime.now())
        if not sess:
            return None
        day, slot, start, end = sess
        return {"date": day.isoformat(), "startLabel": slot["start"],
                "endLabel": end.strftime("%H:%M"), "durationMin": slot["duration"],
                "id": None, "phase": "进行中"}

    def current_session_key(self):
        """当前进行中的 session key；没有就 None（模式会设为下一场默认）。"""
        row = self.current_row()
        if not row:
            return None
        day = row.get("date") or self.w.sel_date
        if isinstance(day, str):
            try:
                day = datetime.date.fromisoformat(day)
            except ValueError:
                day = self.w.sel_date
        return ks.key_of(day, row.get("startLabel"))

    def mode_key(self):
        return ks.get_idle_mode(self.current_session_key())

    def mode_text(self):
        cfg = ks.idle_mode_config(self.mode_key())
        return "%s（%s）" % (cfg["name"], cfg["desc"])

    def cycle_mode(self):
        """Shift+Tab：在模式集合里循环。绑定到当前 session（没有就设为下一场默认）。"""
        key = self.current_session_key()
        cur = ks.get_idle_mode(key)
        order = ks.IDLE_MODE_ORDER
        idx = order.index(cur) if cur in order else 0
        nxt = order[(idx + 1) % len(order)]
        cfg = ks.set_idle_mode(nxt, key)
        cur_name = ks.idle_mode_config(cur)["name"]
        where = "已绑定本场 session" if key else "已设为下一场 session 的默认"
        self.w.say("专注模式：%s → %s（%s；%s）"
                   % (cur_name, cfg["name"], cfg["desc"], where), "ok")
        return nxt

    def status_line(self):
        """一行实时状态（提示符上面那行）。读输入时每秒原地覆盖，不重画整屏。"""
        now = datetime.datetime.now()
        ps = ks.pause_status(now)
        if ps["active"]:
            pause = "暂停 %s（剩 %d 次）" % (fmt_mmss(ps["left_sec"]), ps["remaining"])
        else:
            pause = "未暂停（剩 %d 次）" % ps["remaining"]
        idle = ks.load_idle_state().get("last_idle_seconds")
        idle_txt = "空闲 ?" if idle is None else "空闲 %ds" % int(idle)
        mode_txt = "模式 %s" % ks.idle_mode_config(self.mode_key())["name"]
        return "%s   ·   %s   ·   %s   ·   可暂停 %d/%d" % (
            mode_txt, pause, idle_txt, ps["remaining"], ks.PAUSE_MAX_PER_DAY)

    def render(self):
        w = self.w
        now = datetime.datetime.now()        # naive 本地时间，和 schedule/daemon 同源
        ps = ks.pause_status(now)
        row = self.current_row()

        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        t.add_row("目标", Text(w.current_goal()))
        if row:
            day = row.get("date") or w.sel_date
            if isinstance(day, str):
                try:
                    day = datetime.date.fromisoformat(day)
                except ValueError:
                    day = w.sel_date
            key = ks.key_of(day, row.get("startLabel"))
            detail = "%s   %s–%s（%smin）" % (
                key, row.get("startLabel"), row.get("endLabel") or "?",
                row.get("durationMin") or "?")
            if row.get("id"):
                detail += "  id=%s" % row["id"]
            t.add_row("当前 session", Text(detail, style="bold green"))
        else:
            t.add_row("当前 session", Text("（没有进行中的 session —— 看门狗也不上班）", style="dim"))

        cfg = ks.idle_mode_config(self.mode_key())
        mode = Text()
        mode.append("%s" % cfg["name"], style="bold magenta")
        mode.append("（Shift+Tab 切换）", style="dim")
        t.add_row("专注模式", mode)
        t.add_row("", Text("阈值：%s 无输入 = 违规；%s"
                           % (ks.fmt_idle_seconds(cfg["threshold_sec"]),
                              "违规前 %s 预警，预警不计次数"
                              % ks.fmt_idle_seconds(cfg.get("warn_before_sec", 0))
                              if cfg.get("warn") else "无预警，直接记违规"),
                           style="dim"))

        quota = Text()
        quota.append("%d / %d" % (ps["remaining"], ks.PAUSE_MAX_PER_DAY), style="bold")
        quota.append("   每次 %d 分钟" % ks.PAUSE_MINUTES, style="dim")
        t.add_row("可暂停次数", quota)

        # 暂停剩余 / 当前空闲是**每秒在变**的，放到下方那一行实时覆盖（见 status_line），
        # 免得为了几个跳动的数字每秒把整屏 clear + 重画（那样会抖）。
        t.add_row("实时", Text("模式 / 暂停倒计时 / 空闲秒数在下方状态行原地刷新", style="dim"))

        return Panel(t, title="[bold]Focus · 专注[/]", border_style="yellow", box=box.ROUNDED)

    def commands(self):
        # 预约那套命令在这个视图里一条都不注册 —— 框架层面就不存在
        return [
            _cmd("stop", lambda a: self.w.cmd_pause(),
                 "消耗一次暂停（暂停 %d 分钟）" % ks.PAUSE_MINUTES,
                 usage="stop",
                 hint=_hint_actions("stop", note="消耗一次暂停（%d 分钟）"
                                    % ks.PAUSE_MINUTES)),
        ]

    def unknown_hint(self):
        return ("「专注」视图只认 stop（消耗一次暂停）；Ctrl+O 切模式；"
                "Shift+Tab 切回契约视图再干别的。")


@register_view
class StatsView(View):
    """统计视图：生涯累计 + 每天分布 + 永不重订名单。

    数据链在 `stats.py`（只读公开 API，按 365 天分块拉全量历史，缓存 1 小时）；
    这个视图只管渲染。口径见 stats.py 的 docstring（完成 = completed=true，
    区间 = memberSince → 昨天，分母含空天）。

    「永不重订」（daemon 永不重订的场次 = 你敲完宣言认过罪的地方）**只在这里**出现，
    一条一行列在最底下 —— 平常视图一个字都不提。
    """

    key = "3"
    name = "统计"
    label = "统计 / 历史"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("stat", "stat refresh", "refresh"),
            note="Tab 切视图 · 数据：Focusmate 只读 API",
        )

    def status_line(self):
        return None                     # 静态面板，不需要每秒刷新

    def unknown_hint(self):
        return "「统计」视图只认 stat / stat refresh [/full] / refresh；Shift+Tab 切回去再敲别的。"

    def commands(self):
        w = self.w
        # `stat` 由通用命令集提供（任何视图都能敲），这里只补这个视图的 refresh
        return [
            _cmd("refresh", lambda a: w.cmd_stat(),
                 "重新拉一次统计", ("r", "reload"),
                 usage="refresh",
                 hint=_hint_actions("refresh", note="重新拉一次统计（走缓存）")),
        ]

    # -- 渲染

    @staticmethod
    def _bar(value, vmax, width=8):
        """把一天的量画成一小段条（0 就留空）。"""
        if value <= 0:
            return ""
        if vmax <= 0:
            return "▇"
        return "▇" * max(1, int(round(value * width / float(vmax))))

    def _recent_panel(self, s):
        vmax = max([r["completed"] for r in s["recent"]] or [0])
        cells = []
        for r in s["recent"]:
            n = r["completed"]
            bar = self._bar(n, vmax, width=8)
            txt = Text()
            txt.append(r["date"][5:] + " ", style="dim")
            txt.append((bar or "·").ljust(8), style="green" if n else "dim")
            txt.append(" %2d" % n, style="bold" if n else "dim")
            cells.append(txt)
        cols = 7
        t = Table.grid(padding=(0, 1))
        for _ in range(cols):
            t.add_column(no_wrap=True)
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols]
            while len(row) < cols:
                row.append(Text(""))
            t.add_row(*row)
        return Panel(t, title="[bold]最近 %d 天[/]" % len(cells), border_style="dim",
                     box=box.ROUNDED)

    def _month_panel(self, s):
        t = Table.grid(padding=(0, 1))
        for _ in range(4):
            t.add_column(no_wrap=True)
        cells = []
        for m in s["by_month"]:
            txt = Text()
            txt.append(m["month"][2:] + " ", style="dim")
            txt.append("%d" % m["completed"], style="bold green")
            txt.append("/%d" % m["booked"], style="dim")
            cells.append(txt)
        for i in range(0, len(cells), 4):
            row = cells[i:i + 4]
            while len(row) < 4:
                row.append(Text(""))
            t.add_row(*row)
        return Panel(t, title="[bold]按月（完成 / 订了）[/]", border_style="dim",
                     box=box.ROUNDED)

    def _never_panel(self):
        """永不重订名单：一行一条，像无序列表。平常视图里不出现。"""
        abandoned = ks.load_verdicts()["abandoned"]
        t = Table.grid(padding=(0, 1))
        t.add_column(style="red", no_wrap=True)
        t.add_column(no_wrap=True)
        t.add_column(style="dim")
        if not abandoned:
            t.add_row("", Text("（还没有认过罪的场次 —— daemon 会一直重订）", style="dim"))
        for key in sorted(abandoned):
            rec = abandoned[key] or {}
            when = (rec.get("at") or "").replace("T", " ")
            t.add_row("·  " + key.replace("T", " "),
                      "认罪于 " + (when or "?"),
                      (rec.get("note") or rec.get("why") or "")[:48])
        return Panel(t, title="[bold red]永不重订 · 你认过罪的地方[/]",
                     border_style="red", box=box.ROUNDED)

    def render(self):
        w = self.w
        if not w.stat:
            return Panel(
                Text(w.stat_note or "还没拉过。敲 stat 拉一次（Focusmate 只读 API）。",
                     style="dim"),
                title="[bold]📊 Stat · 统计[/]", border_style="green", box=box.ROUNDED)
        s = w.stat
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        t.add_row("生涯", Text("%s → %s · %d 天" % (s["since"], s["until"], s["days"])))
        total = Text()
        total.append("完成 %d" % s["total_completed"], style="bold green")
        total.append("  ·  订了 %d  ·  完成率 %.1f%%" % (s["total_booked"],
                                                        s["completion_rate"]), style="dim")
        t.add_row("总场次", total)
        mean = Text("%.2f 场/天" % s["mean_per_day"], style="bold")
        mean.append("（含空天） · %.2f 场/天（只算做过的 %d 天）"
                    % (s["mean_active_day"], s["active_days"]), style="dim")
        t.add_row("每天平均", mean)
        zero = Text()
        zero.append("%d 天" % s["zero_days"], style="bold red" if s["zero_days"] else "bold green")
        zero.append(" 一个 session 都没做（%.1f%%）" % s["zero_rate"], style="dim")
        t.add_row("空 天", zero)
        t.add_row("峰 值", Text("%s · %d 场" % (s["max_day"]["date"], s["max_day"]["completed"]),
                              style="bold yellow"))
        t.add_row("分 布", Text("p25 %g · 中位数 %g · p75 %g · 最大 %d"
                              % (s["p25"], s["median"], s["p75"], s["max_per_day"])))
        t.add_row("累 计", Text("%.0f 小时 · 每场均值 %.1f 分钟"
                              % (s["total_hours"], s["avg_minutes"])))
        t.add_row("连 续", Text("当前 %d 天 · 最长 %d 天"
                              % (s["streak_current"], s["streak_best"])))
        src = Text((s.get("fetched_at") or "?").replace("T", " "), style="dim")
        src.append(" · %s" % (w.stat_note or "?"), style="dim")
        t.add_row("数 据", src)

        out = [Panel(t, title="[bold]📊 Stat · 统计（Focusmate 只读 API）[/]",
                     border_style="green", box=box.ROUNDED)]
        if s["recent"]:
            out.append(self._recent_panel(s))
        if s["by_month"]:
            out.append(self._month_panel(s))
        out.append(self._never_panel())
        return out



@register_view
class TaskView(View):
    """任务视图：本地 JSON 任务库 + 挂到 session + Focusmate 完成归档。

    命令统一以 ``task`` 开头（需求里的语法）：::

        task create [id] [field=value ...] [--repeat 周几@HH:MM] [--assign N]
        task delete <序号>
        task modify <序号> <field> <新值>
        task note <序号> <备注文本|网址>    # 备注：完成之后照着去对应地方干活
        task assign <session ID>            # 默认当前选中的任务
        task assign <序号> <session ID>     # 显式指定
        task select <序号>
        task <序号>                         # ↑ 同义：裸序号就是 select
        task <id> -a <session ID>           # 选中 + 挂上去（最省事）
        task <id> -an                       # 从它那一场下架（≡ -a none）
        task <id> -ac                       # 挂到当前这一场（≡ -a current）
        task 32 swap 21                     # 两条任务互换位置（赋闲 ↔ 有场）
        task -s 5 -a 6                      # 把 #5 上的全部任务转移到 #6
        task -s 5 -a none                   # 把 #5 上的全部任务下架
        task repeat <id> <周几@HH:MM|off>   # 每周重复（只影响那一天的那一场）
        task -t                             # 列 trigger 预设（默认空列表）
        task 3 -f end                       # formatter：排到当天最后一段并自动挂上
        task 3 -c 5                         # count = 5（时长跟着变成 125 分钟）
        task 3 -c +2                        # 进度 +2；够数就当完成（勾 Focusmate + 写回）
        task 3 -c -1                        # 进度 -1（最低 0）
        task sync                           # GET /v1/tasks → 把 completedAt 归档
        task archive [N]                    # 看归档（data/task-archive.json）
        sort <字段> [asc|desc] / sort off   # 按字段排序
        group <字段> / group off            # 按字段分组（前置分组列，同组后续留空）
        search <关键词> [--group <字段>]    # 搜索过滤（可顺带分组）
        open 1 / 2 / 3                      # 直接翻到第 N 页

    **编号有两套，别混**：
      * **序号** = 契约视图那块任务面板上的第几条（1 起、连续、refresh/收工后重排）。
        命令里直接写它：``task 3 -an`` 就是「面板第 3 条下架」。
      * **内部 id** = 任务库里那个不变的 id（`task list` 里那一列）。面板上没有的任务
        （别的天处理过的、归档里的）用 ``id:15`` 显式指定。
    """

    key = "4"
    name = "任务"
    label = "任务 / 归档"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("task create", "task 3 -a 1", "task 3 -an", "task 3 -ac",
                     "task 3 -c +2", "open 1", "sort", "group project", "search",
                     "inspect-due", "review"),
            note="数字 = 面板上的序号 · open 1/2/3 翻页 · open/close <表头> 折叠列"
                 " · sort <字段> 排序 · group <字段> 分组 · search <词> 搜索 · "
                 "PgUp/PgDn 翻看长看板 · Tab 切视图",
        )

    def status_line(self):
        return None

    def unknown_hint(self):
        return ("「任务」视图认 task create/delete/modify/assign/select/duplicate/"
                "drop/done/link/import/sync/swap、sort、group、search、open <页码|表头>、"
                "close <表头>、review、inspect-due"
                "（也可以直接写序号，如 task 3 -a 1 / task 3 -an / task 3 -c +2；"
                "序号 = 面板上的第几条，面板外的任务用 id:N）；Shift+Tab 切回去再干别的。")

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", lambda a: w.cmd_task_sync(quiet=False),
                 "拉 GET /v1/tasks，把已完成任务的 completedAt 归档",
                 ("r", "reload"), usage="refresh",
                 hint=_hint_actions("refresh", note="同步 Focusmate task 完成时间")),
            _cmd("sort", w.cmd_task_sort,
                 "任务表排序：sort <字段> [asc|desc] | sort off",
                 ("排序",), usage="sort <字段> [asc|desc] | sort off",
                 options=("id", "序号", "title", "project", "状态", "session", "截止",
                          "duration", "count", "asc", "desc", "off"),
                 hint=_hint_actions("project", "截止", "session", "状态", "duration", "off",
                                    note="默认升序；加 desc 降序；sort off 恢复默认")),
            _cmd("group", w.cmd_task_group,
                 "任务表按字段分组（前置分组列，同组任务聚拢）：group <字段> | group off",
                 ("grouping", "分组"), usage="group <字段> | group off",
                 options=("project", "状态", "session", "截止", "标签", "off"),
                 hint=_hint_actions("project", "状态", "session", "off",
                                    note="前置分组列，同组首行显示组名，后续行留空")),
            _cmd("search", w.cmd_task_search,
                 "搜索任务（支持按字段分组显示）：search <关键词> [--group <字段>] | search off",
                 ("find", "filter", "搜索"),
                 usage="search [<关键词>] [-g|--group <字段>] | search off",
                 options=("--group", "-g", "project", "off"),
                 hint=_hint_actions("project", "--group project", "off",
                                    note="搜 title/project/备注/标签/状态；search off 清空过滤")),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="A 延期 / B 放弃 / C 重议 / D 补录 / E 推后"
                                                   "（inspect-due 另有 G 延长排期）")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E/G）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
            _cmd("plan", w.cmd_plan,
                 "把这一天的空闲切成 count 分给项目，再自动排任务进 session",
                 ("排期", "分配"),
                 usage="plan [+1|今天|明天|YYYY-MM-DD] → "
                       "task <序号> <count> | task <序号> clear | complete",
                 options=("+1", "+2", "today", "明天", "complete", "task 1 4"),
                 hint=_hint_actions("+1", "明天", "complete", "task 1 4", "task 1 clear",
                                    note="不带参数 = 今天；plan +1 = 明天；"
                                         "序号 = 项目表上的 #；超预算整条命令不执行")),
            _cmd("project", w.cmd_project,
                 "管理真正项目 Project (pro)：pro | pro 1 | pro 1 -a | pro 1 file <路径> | pro create <名称>",
                 ("pro", "pr", "项目"),
                 usage="pro [<序号> [-a|--advance [op <N>|task import] | file <路径> | open | refresh | remove] | create <名称>]",
                 options=("--advance", "-a", "create", "file", "open", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -a", "1 file", "1 open", "create", "1 refresh", "1 remove",
                                    note="pro = 真正项目（stage: running/bottleneck）；-a 快捷切换阶段并关联新 op")),
            _cmd("op", w.cmd_op,
                 "管理 OmniFocus Project (op)：op | op 1 | op 1 sop|incubator | op 1 relate --export|--import | op import [sop|incubator]",
                 ("ofp", "omnifocus-project"),
                 usage="op [<序号> [sop|incubator|pro <N>|relate --export|--import [路径]|refresh|remove] | import [sop|incubator]]",
                 options=("sop", "incubator", "import", "relate", "--export", "--import", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 sop", "1 incubator", "1 relate --export", "1 relate --import", "import sop", "import incubator", "refresh",
                                    note="op = OmniFocus Project（SOP 或 incubator 两种类型）")),
        ]

    def _row_budget(self):
        """任务表每页画几行 —— 用掉终端真正剩下的高度。

        以前这里硬钳 ``min(20, terminal_rows - 22)``：120 行的终端也只显示
        20 条，下面明明还有一大片空白，用户要找的任务被迫翻好几页。现在按
        看板预算和当前宽度下固定的 chrome 高度算；表头/图例/归档/Recent
        全部算进去，仍然不会把底部提示挤出屏幕。
        """
        try:
            budget = int(self.w.shell.dashboard_budget())
        except Exception:                             # noqa: BLE001 - 测试/离屏兜底
            try:
                budget = max(4, int(self.w.shell._terminal_rows()) - 3)
            except Exception:                         # noqa: BLE001
                budget = 27
        try:
            width = int(self.w.frame_width())
        except Exception:                             # noqa: BLE001
            width = 120
        # 实测的固定开销：头部 8 行 + 表题/表头/分隔线 + 图例/命令 + 归档框；
        # 窄屏时图例和 caption 会多折几行。这里取略保守值，宁可少画一条，
        # 也不让页面高度超过一屏（超高时框架会从顶部裁，反而丢内容）。
        if width < 90:
            overhead = 27
        elif width < 120:
            overhead = 25
        elif width < 170:
            overhead = 24
        else:
            overhead = 23
        # 至少留 4 条：极小终端也别把表变成只有表头；超出的一点点由
        # WardenShell 的帧内滚动兜住（PgUp/PgDn）。
        return max(4, min(200, budget - overhead))

    def _task_window(self, tasks):
        """返回 ``(当前页任务, 起始下标, 总数, 当前页码, 总页数)``。

        支持 ``open 1 / 2 / 3`` 显式翻页；未指定页码时跟随 ``selected_task_id``
        自动落到它所在的那一页（默认第 1 页）。
        """
        total = len(tasks)
        limit = self._row_budget()
        total_pages = max(1, (total + limit - 1) // limit)
        page = getattr(self.w, "task_view_page", None)
        if page is None:
            selected = self.w.selected_task_id
            page = 1
            if selected is not None:
                for index, task in enumerate(tasks):
                    if task.get("id") == selected:
                        page = (index // limit) + 1
                        break
        page = max(1, min(int(page), total_pages))
        if total <= limit:
            return tasks, 0, total, 1, 1
        start = (page - 1) * limit
        return tasks[start:start + limit], start, total, page, total_pages

    def _guidance_mode(self):
        """说明文字的三档密度：tiny（矮屏）/ compact（窄屏）/ full。

        矮屏优先保证表头留在屏幕上，所以 tiny 会把词压到最短；信息仍然齐全。
        """
        try:
            rows = int(self.w.shell._terminal_rows())
        except Exception:                             # noqa: BLE001
            rows = 30
        try:
            width = self.w.frame_width()
        except Exception:                             # noqa: BLE001
            width = 80
        if rows < 40 or width < 100:
            return "tiny"
        if rows < 50 or width < 120:
            return "compact"
        return "full"

    def _legend_text(self):
        """表下灰色小字：所有标记与备注缩写的含义。"""
        mode = self._guidance_mode()
        if mode == "tiny":
            return Text(
                "标记：🔁重复子 🔁*模板 📁项目 🔗OF 🎯FM ⏳挂载 ⏱fmt ⚡trig 🔒独占 · "
                "状态：·进行 ✓完成 ✗放弃 ⤼转移 · 备注：dur分 cnt进度 excl独占 "
                "tag标签 rep重复 fmt格式 trig触发 note备注",
                style="dim")
        if mode == "compact":
            return Text(
                "标记：🔁=重复子任务 🔁*=模板 📁=项目 🔗=OmniFocus 🎯=Focusmate "
                "⏳=挂载中 ⏱=formatter ⚡=trigger 🔒=独占 · 状态：·=进行中 ✓=完成 "
                "✗=放弃 ⤼=转移走 · 备注：dur=分钟 cnt=进度 excl=独占 tag=标签 "
                "rep=重复 fmt=formatter trig=trigger note=备注",
                style="dim")
        text = Text(style="dim")
        text.append("标记：")
        text.append("🔁=重复子任务 · 🔁*=重复模板 · 📁=项目任务 · 🔗=OmniFocus · "
                    "🎯=Focusmate · ⏳=挂载中 · ⏱=formatter · ⚡=trigger · 🔒=独占\n")
        text.append("状态：·=进行中 · ✓=完成 · ✗=放弃 · ⤼=转移走　")
        text.append("备注缩写：dur=分钟 · cnt=进度 · excl=独占 · tag=标签 · "
                    "rep=重复 · fmt=formatter · trig=trigger · note=备注")
        return text

    def _command_guide_text(self):
        """表下一行灰色命令指引：每条 ≤7 字，参数细节交给 help --task。"""
        entries = (
            ("refresh", "同步"), ("review", "收尾"), ("inspect-due", "查过期"),
            ("plan", "排期"), ("project", "项目"), ("sort", "排序"),
            ("group", "分组"), ("search", "搜索"), ("open", "翻页/展开"),
            ("close", "折叠"), ("task", "任务操作"), ("help --task", "详情"),
        )
        mode = self._guidance_mode()
        text = Text(style="dim")
        text.append("命令：")
        for index, (cmd, desc) in enumerate(entries):
            if index:
                text.append("," if mode == "tiny" else " · ")
            text.append(cmd)
            text.append((" " if mode != "tiny" else "") + desc)
        return text

    def render(self):
        w = self.w
        raw_tasks = w.task_store.list()
        filtered_tasks = w.filter_and_sort_tasks(raw_tasks)
        tasks, start, total, page, total_pages = self._task_window(filtered_tasks)
        selected = w.selected_task_id
        keys = task_dynamic_keys(raw_tasks)
        headers = task_table_headers(raw_tasks)
        hidden = w.effective_task_hidden_columns(raw_tasks)
        visible_headers = [h for h in headers if h not in hidden]
        # close 已经保证至少留一列；这里只是防御坏偏好文件。
        if not visible_headers:
            visible_headers = [headers[0]]

        group_by = getattr(w, "task_group_by", None)
        group_col_header = None
        if group_by:
            group_col_header = "grouping(%s)" % group_by
            # 分组列前置在最前面，原本同名列就不必重复占地方
            visible_headers = [h for h in visible_headers if h != group_by]

        width = w.frame_width()
        proj_max = 7 if width < 95 else TASK_PROJECT_MAX_LEN
        # title + 备注分掉所有剩下的横向空间。以前 remark 硬钳 48，宽屏
        # 明明右边还有十几列空白，备注却已经以「…」结束（例如重复任务的
        # `fmt=enlist` 整段被吞掉）。硬钳只该防窄屏，不该在宽屏浪费。
        avail = max(18, width - (52 + proj_max))
        title_max = max(8, min(48, avail * 2 // 5))
        remark_max = max(10, avail - title_max)
        group_max = 10 if width < 95 else 16

        t = Table(title="Task · 任务表（open/close 折叠列 · open 1/2/3 翻页 · 序号 = 契约面板上的第几条）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", show_header=True,
                  width=max(20, width - 1), expand=True)
        if group_col_header:
            t.add_column(group_col_header, style="bold cyan", no_wrap=True,
                         overflow="crop", max_width=group_max)
        specs = dict(TASK_TABLE_COLUMNS)
        # 显式给每列 max_width / min_width：Rich 在总宽不够时会悄悄把前几列
        # 压成空白；不钉住「序号/标记/状态」的话，表头看起来就像少了好几列。
        max_widths = {
            "序号": 7, "标记": 8, "title": title_max, "project": proj_max,
            "状态": 4, "session": 11, "截止": 11, "备注": remark_max,
            "id": 6, "重复": 18, "trigger": 12, "formatter": 14,
            "Focusmate": 10, "OmniFocus": 12, "完成时间": 11, "标签": 14,
            "duration": 7, "count": 10, "exclusive": 7,
        }
        # Rich 的 width/max_width/min_width 都算 padding。给关键列钉一个
        # 恰好能装下内容的 min_width，表格又会按终端宽度展开，于是宽屏上
        # 多出来的空间会真的分给备注，而不是在右边留白、同时把备注裁掉。
        min_widths = {
            "序号": 8, "标记": 10, "title": min(20, max(8, title_max)),
            "project": min(14, max(8, proj_max + 2)),
            "状态": 6, "session": 13, "截止": 13,
            "备注": min(remark_max, 30 if width < 100 else 60),
        }
        if width < 100:
            min_widths.update({"序号": 6, "标记": 8, "session": 10,
                               "截止": 10, "备注": 12})
        for header in visible_headers:
            if header in specs:
                kwargs = dict(specs[header])
                if header in max_widths:
                    kwargs["max_width"] = max_widths[header]
                if header in min_widths:
                    kwargs["min_width"] = min_widths[header]
                t.add_column(header, **kwargs)
            else:
                t.add_column(str(header), no_wrap=True, overflow="crop",
                             max_width=remark_max)

        status_style = {ts.STATUS_COMPLETED: STYLE_DONE, ts.STATUS_DROPPED: STYLE_DROPPED}
        total_cols = (1 if group_col_header else 0) + len(visible_headers)
        if not tasks:
            row = [Text("") for _ in range(total_cols)]
            msg = ("（没有匹配「%s」的任务；`search off` 清除过滤）" % w.task_search_query
                   if getattr(w, "task_search_query", "")
                   else "（还没有任务；task create title=写论文 开始）")
            row[0] = Text(msg, style="dim")
            t.add_row(*row)
        prev_group_val = object()
        for task in tasks:
            status = w.task_store.status_of(task)
            style = "bold reverse" if task.get("id") == selected else ""
            if status != ts.STATUS_ACTIVE:
                style = status_style.get(status, "") + (
                    " reverse" if task.get("id") == selected else "")
            sess = task.get("session") or {}
            if sess:
                sess_raw = "%s %s" % (sess.get("date") or "", sess.get("start") or "")
                sess_txt = format_compact_timestamp(sess_raw.strip(), w.sel_date)
            else:
                sess_txt = "—"
            due = w.omni_due_of(task)
            due_txt = format_compact_timestamp(due, w.sel_date) if due else "—"
            fm = task.get("focusmate") or {}
            fm_txt = str(fm.get("task_id") or "")[:8] or "—"
            omni_id = ts.TaskStore.omnifocus_id(task)
            omni_txt = str(omni_id)[:12] if omni_id else "—"
            done_raw = fm.get("completed_at") or task.get("status_at") or ""
            done = format_compact_timestamp(done_raw, w.sel_date) if done_raw else "—"
            rep = task.get("repeat") or {}
            rep_txt = ("🔁 " + ts.repeat_summary(rep)) if rep else "—"
            trig = task.get("trigger") or {}
            fmt = task.get("formatter") or {}
            trig_txt = str(trig.get("name") or "—")
            fmt_txt = str(fmt.get("name") or "—")
            if fmt.get("reservation"):
                fmt_txt += "@" + str(fmt["reservation"].get("start") or "?")
            elif fmt and ts.TaskStore.is_repeat_template(task):
                fmt_txt += "（模板）"
            mark_sym = w.task_mark(task) or "·"
            tags = task.get("tags") or []
            panel_no = w.task_no(task)
            if panel_no:
                num_txt = str(panel_no)
            elif (ts.TaskStore.is_repeat_template(task)
                  or ts.TaskStore.is_project_backlog(task)):
                # 不在契约面板上；任务视图里直接把可敲的写法摆出来。
                num_txt = "id:%s" % task["id"]
            else:
                num_txt = "—"
            fields = task.get("fields") or {}
            flags_txt = w.task_flags_text(task)
            title_raw = w.task_store._one_line(
                fields.get("title") or fields.get("name") or "")
            title_txt = _crop_display(title_raw, title_max)
            proj_raw = w.task_project_name(task)
            proj_txt = _crop_display(proj_raw, proj_max) if proj_raw else "—"
            remark_raw = w.task_remark_text(task)
            remark_txt = _crop_display(remark_raw, remark_max) if remark_raw else "—"
            dur_val = ts.task_minutes(task)
            cnt_val = ts.count_text(task, w.task_count_delta(task))
            if not cnt_val and ts.task_count(task) is not None:
                cnt_val = str(ts.task_count(task))
            excl_val = "true" if th.is_exclusive(task) else "—"
            cells = {
                "序号": Text(num_txt, style=style),
                "标记": Text(flags_txt, style=style if flags_txt != "·" else STYLE_MUTED),
                "title": Text(title_txt, style=style),
                "project": Text(proj_txt, style="cyan" if proj_raw else STYLE_MUTED),
                "状态": Text(mark_sym, style=status_style.get(status, "") or style),
                "session": Text(sess_txt, style=style if sess else STYLE_MUTED),
                "截止": Text(due_txt, style=STYLE_MUTED if due else style),
                "备注": Text(remark_txt, style=style if remark_raw else STYLE_MUTED),
                "id": Text(str(task["id"]), style=style),
                "重复": Text(rep_txt, style="magenta" if rep else STYLE_MUTED),
                "trigger": Text(trig_txt, style="cyan" if trig else STYLE_MUTED),
                "formatter": Text(fmt_txt, style="green" if fmt else STYLE_MUTED),
                "Focusmate": Text(fm_txt, style=style),
                "OmniFocus": Text(omni_txt, style="cyan" if omni_id else STYLE_MUTED),
                "完成时间": Text(done, style=style),
                "标签": Text(" · ".join(tags) if tags else "—",
                            style="yellow" if tags else STYLE_MUTED),
                "duration": Text(str(dur_val) if dur_val is not None else "—",
                                 style=style if dur_val is not None else STYLE_MUTED),
                "count": Text(cnt_val or "—", style=style if cnt_val else STYLE_MUTED),
                "exclusive": Text(excl_val,
                                  style="yellow" if excl_val == "true" else STYLE_MUTED),
                TASK_CONTENT_COLUMN: Text(
                    _crop_display(w.task_store.summarize(task), title_max), style=style),
            }
            for key in keys:
                if key not in cells:
                    cells[key] = Text(
                        _crop_display(w.task_store._one_line(fields.get(key)), remark_max),
                        style=style)
            row_cells = [cells.get(header, Text("")) for header in visible_headers]
            if group_col_header:
                gval = w.task_group_label(task, group_by)
                show_gval = _crop_display(gval, group_max) if gval != prev_group_val else ""
                prev_group_val = gval
                row_cells = [Text(show_gval, style="bold cyan")] + row_cells
            t.add_row(*row_cells)

        caption_bits = []
        page_examples = " / ".join(str(i) for i in range(1, min(total_pages, 3) + 1))
        if total_pages > 3:
            page_examples += " / …"
        caption_bits.append(
            "第 %d/%d 页，共 %d 条（open %s 直接翻页）"
            % (page, total_pages, total, page_examples or "1"))
        active_filters = w.task_filter_summary()
        if active_filters:
            caption_bits.append(active_filters)
        if hidden:
            # 被折叠的表头名字放在表下面，方便随时知道还有什么列 / 怎么打开。
            caption_bits.append("已折叠列：%s（open <表头名> 打开）" % "、".join(
                header for header in headers if header in hidden))
        if caption_bits:
            t.caption = " · ".join(caption_bits)

        # 收尾面板在 repl_kit 的打断栏位里（见 render_interrogate），不在这儿。
        out = [t, self._legend_text(), self._command_guide_text()]
        try:
            terminal_rows = int(w.shell._terminal_rows())
        except Exception:                             # noqa: BLE001
            terminal_rows = 30
        arch = w.task_store.load_archive().get("records") or []
        archive_limit = 2 if terminal_rows < 50 else 5
        at = Table.grid(padding=(0, 1))
        at.add_column(style="dim", no_wrap=True)
        at.add_column(no_wrap=True)
        at.add_column(no_wrap=True, overflow="crop")
        at.add_column(no_wrap=True, overflow="crop")
        if not arch:
            at.add_row("", Text("（还没有完成归档；task sync 会读 GET /v1/tasks 的 "
                                "completedAt）", style="dim"))
        for rec in arch[-archive_limit:]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            note = rec.get("notes") or w.task_store.notes({"fields": rec.get("fields")})
            at.add_row(
                "· " + str(rec.get("task_id") or "?"),
                "%s ← %s %s" % (str(fm.get("completed_at") or "?"),
                                sess.get("date") or "?", sess.get("start") or "?"),
                (rec.get("fields") or {}).get("title") or
                w.task_store._one_line(rec.get("fields"))[:60],
                note_renderable(note))
        out.append(Panel(at, title="[bold]Task Archive · 完成归档[/]",
                         border_style="dim", box=box.ROUNDED))
        return out


@register_view
class SettingsView(View):
    """设置视图：把所有设置项（哪怕从没改过）连同解释列出来，``set`` 改。

    需求：展示**所有**设置和他们的介绍，用 ``set {设置的 id} {value}`` 改内容。
    设置本身是数据（``settings_store.SETTINGS``），这里不硬编码任何一条。
    """

    key = "5"
    name = "设置"
    label = "设置 / 规则"

    def idle_hint(self):
        return repl_kit.Hint("命令", ("set", "settings", "help", "quit"),
                             note="Tab 切视图 · 不写 value = 恢复默认")

    def unknown_hint(self):
        return "「设置」视图只认 set / settings（Shift+Tab 切回去干别的）。"

    def commands(self):
        w = self.w
        return [
            _cmd("settings", lambda a: w.cmd_settings(show=True),
                 "重新列出全部设置（也是只重画看板）", ("list",),
                 usage="settings", hint=_hint_actions("settings", note="只重画看板")),
        ]

    def render(self):
        w = self.w
        rows = w.settings.rows()
        t = Table(title="Settings · 设置（改过的标 *；set <id> <value> 修改）",
                  box=box.SIMPLE_HEAVY, header_style="bold cyan",
                  title_justify="left", expand=False)
        t.add_column("设置 ID", no_wrap=True, style="bold")
        t.add_column("当前值", no_wrap=True, style="green")
        t.add_column("默认", no_wrap=True, style="dim")
        t.add_column("说明", no_wrap=False, overflow="fold")
        for row in rows:
            ident = Text(("* " if row["changed"] else "  ") + row["id"],
                         style="bold yellow" if row["changed"] else "bold")
            t.add_row(ident, Text(row["value_display"], style="green"),
                      Text(str(row["default"])), Text(row["desc"], style="dim"))
        hint = Text("set 的取值：", style="dim")
        hint.append("  ".join("set %s %s" % (r["id"],
                                             "|".join(r["values"]) if r["values"] else "…")
                              for r in rows), style="cyan")
        return [t, hint]


@register_view
class ChoreView(View):
    """空闲屏：没有 session 时只显示绑定项目里的一条琐事。

    这是**自动**视图：warden 每帧发现当前不在 session、且 ``chore-project``
    已绑定，就切进来。屏幕上不画正常看板、状态框、消息和视图栏，只有一条任务
    和 m/c/q 三行提示。``q`` 回到进入前的正常视图，并压制到下一场 session 开始。
    """

    key = "6"
    name = "琐事"
    label = "琐事 / 空闲"

    def idle_hint(self):
        return repl_kit.Hint("琐事", ("m 完成", "c 换一个 task", "q 退出"),
                             note="q = 回到正常界面；这段空档里不再自动弹回来")

    def unknown_hint(self):
        return "琐事空闲屏只认 m / c / q。"

    def commands(self):
        w = self.w
        # m / c / q 由 Warden.intercept 在命令派发之前先吃掉；q 不能落到
        # 通用 quit（那会退出整个 TUI，而这里只退出空闲屏）。
        # refresh 必须保留：app 从 Dock 唤起时会往 stdin 打一行 refresh，
        # 如果琐事屏不认它，恰好在你空闲时唤起就会只得到一条「不认命令」。
        return [
            _cmd("refresh", w.cmd_refresh,
                 "全量刷新看板（琐事屏里也能用；不会退出当前屏）", ("r", "reload"),
                 usage="refresh [--check]", options=("--check", "-c",),
                 hint=_hint_refresh),
        ]

    def render(self):
        return self.w.render_chore_idle()


# ---------------------------------------------------------------- 输入（Tab 切视图 / 原地刷新）


class WardenShell(repl_kit.ReplShell):
    """repl_kit 的 shell，加一条只属于看板型 TUI 的让步。

    prompt_toolkit 把 `frame()` 当成**多行 prompt**：内容比终端高时，它先
    保住最下面的输入行，然后从看板**底部**裁掉几行。被裁的恰好是 Recent、
    视图栏和命令提示 —— 这就是「warden 最下面几行被吞掉」的原因。

    这里在交给 prompt_toolkit 之前，先按终端高度把看板裁到「底部预算」内：
    要裁就从**顶部**裁，把最下面的东西（消息 / 视图栏 / 提示行 / 状态行 /
    输入行）永远留在屏幕上。矮终端里看板顶部本来就装不下 —— 与其让它把底部
    挤掉，不如让顶部让位。

    补全菜单的空间由 repl_kit 的 reserve_space_for_menu 决定；warden 把它
    设成 0（菜单改成浮在看板上面），所以这里只需要给它留 0 行。

    还有一件：**画帧要缓存**。prompt_toolkit 每敲一个字就重画一次 chrome
    （`on_text_changed` → `invalidate()`），而 chrome 会把整块看板从 rich 重新
    排版成 ANSI（表格 + 任务对照表 + Recent + 状态框，20ms 上下），再解析回
    FormattedText（又是几毫秒）。按 10 字/秒算，每秒有 250ms 花在「画一模一样的
    东西」上 —— 这就是输入时肉眼可见的掉帧。

    所以这里按「帧键」缓存：键一样就直接复用上次的 ANSI 文本和 fragments。
    看板里唯一自己会动的东西是时钟和「多久以前」，全是秒级，所以同一秒里
    复用一帧看不出差别；状态一变（`invalidate_frame()`）立刻作废。
    """

    def __init__(self, *args, **kwargs):
        pin_bottom = kwargs.pop("pin_bottom", True)
        super().__init__(*args, **kwargs)
        self.pin_bottom = bool(pin_bottom) # 把输入行永远钉在屏幕最底部
        self.frame_rev = 0                 # 状态变化计数：变了就必须重排版
        self.frame_key_hook = None         # (() -> tuple)：业务侧补充的帧键
        self._frame_memo = None            # (key, ansi_text)
        self._ansi_memo = None             # (ansi_text, chunks)
        # 内部滚动：0 = 贴底（最新内容）。向上翻时离开底部，最多翻到整块
        # 看板的第一行；不会把内容漏到终端 scrollback 里。因为深色终端的
        # Alt-Screen 没有「往上滚」的原生操作，否则超高一截就只能丢顶部。
        self.scroll_offset = 0
        self._max_scroll_offset = 0
        self._scroll_view_index = self.view_index

    def switch_view(self, delta=1):
        """切换视图时回到看板底部：新视图不该继承旧视图的滚动位置。"""
        self.scroll_offset = 0
        self._max_scroll_offset = 0
        self._scroll_view_index = getattr(self, "view_index", 0)
        return super().switch_view(delta)

    def scroll_by(self, pages=1):
        """页面内滚动看板。

        ``pages > 0`` = 往上看旧内容，``< 0`` = 往下回到底部。上限由
        :meth:`_build_frame_text` 按实际看板行数算出，不会滚出空白。
        """
        try:
            step = max(1, int(self.dashboard_budget()) - 3)
        except Exception:                             # noqa: BLE001
            step = 10
        old = self.scroll_offset
        target = max(0, int(self.scroll_offset) + int(pages) * step)
        # _max_scroll_offset 是上一帧实际算出的上限；有了它就不让一次
        # PageUp 把 offset 留在一个下一帧必须回钳的荒谬值里。
        if self._max_scroll_offset:
            target = min(target, self._max_scroll_offset)
        self.scroll_offset = target
        self.invalidate_frame()
        return self.scroll_offset != old

    def scroll_to_bottom(self):
        old = self.scroll_offset
        self.scroll_offset = 0
        if old:
            self.invalidate_frame()
        return bool(old)

    def scroll_hint(self):
        """给 footer 的一小段提示；没滚过就不占地方。"""
        if not self.scroll_offset:
            return ""
        total = max(0, self._max_scroll_offset)
        if not total:
            return ""
        return "↕ 已上翻 %d/%d 行 · PgDn 回底部" % (
            min(self.scroll_offset, total), total)

    def key_bindings(self):
        """框架键 + PageUp/PageDown：长看板在**帧内**有限滚动。"""
        bindings = super().key_bindings()

        @bindings.add("pageup", eager=True)
        def _(event):  # noqa: F811 - one binding per shell
            self.scroll_by(1)
            event.current_buffer.cancel_completion()
            event.app.invalidate()

        @bindings.add("pagedown", eager=True)
        def _(event):  # noqa: F811
            self.scroll_by(-1)
            event.current_buffer.cancel_completion()
            event.app.invalidate()

        return bindings

    def invalidate_frame(self):
        """状态变了：下一次 chrome() 必须重新排版（不重排版就是旧画面）。"""
        self.frame_rev += 1
        self._frame_memo = None

    def _frame_clock(self):
        """帧键里的时间位（整秒）。看板上最细的时间粒度就是秒。

        单独抽成一个方法是为了测试能钉住它 —— 不然「同一秒里复用」这种
        断言会随机踩在秒的边界上。
        """
        return int(time.time())

    def frame_key(self):
        """这一帧的「身份」：键相等 = 画出来的东西相等。

        只用**微秒级**的东西算键：`_terminal_rows()` 走 `get_app_or_none()`，
        宽度走 `shutil.get_terminal_size()`。窗口大小一变键就变（prompt_toolkit
        本来也会因为 resize 重画）。
        """
        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except Exception:                             # noqa: BLE001
            width = 80
        key = [self.frame_rev, self.view_index, self._frame_clock(), width,
               self._terminal_rows(), bool(self.show_warnings), bool(self.pin_bottom),
               int(self.scroll_offset)]
        try:
            key.append(self.warnings.signature())
        except Exception:                             # noqa: BLE001
            key.append(object())
        if self.frame_key_hook is not None:
            try:
                key.extend(self.frame_key_hook())
            except Exception:                         # noqa: BLE001 - 键算不出来就别用缓存
                key.append(object())
        return tuple(key)

    def frame_text(self):
        key = self.frame_key()
        memo = self._frame_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        raw = self._build_frame_text()
        # 记的是**画完之后**的键：排版这一趟本身可能改了状态（重读 data.csv、
        # 重算警告、收尾面板刚起步），拿画之前的键去存，下一次就对不上、
        # 白重画一遍（第一帧永远是「画了->又画一遍」）。
        self._frame_memo = (self.frame_key(), raw)
        return raw

    def chrome(self, text=""):
        """父类的 chrome + 「看板没变就别重新解析 ANSI」的缓存。

        父类每次都把整块看板的 ANSI 交给 prompt_toolkit 的解析器（几十行，
        几毫秒）。看板没变时那几毫秒纯属白烧，所以把 fragments 也存下来。
        拼装顺序和父类一致：看板 → 提示行 → 状态行 → 提示符。
        """
        dashboard = self.frame_text()
        chunks = None
        if dashboard:
            memo = self._ansi_memo
            if memo is not None and memo[0] is dashboard:
                chunks = list(memo[1])
            else:
                chunks = self._dashboard_chunks(dashboard)
                self._ansi_memo = (dashboard, tuple(chunks))
        else:
            chunks = []
        # 底部状态栏（视图栏）：框架把它排在打断栏位下面、提示行上面 —— 这里照抄。
        footer = self.footer_text()
        scroll_hint = self.scroll_hint()
        if scroll_hint:
            # 只在上翻后追加，平常不占额外宽度。footer 本来就是一行。
            footer = ((footer.rstrip("\n") + "    " + scroll_hint + "\n")
                      if footer else (scroll_hint + "\n"))
        if footer:
            chunks.extend(self._dashboard_chunks(footer))
        hint = self.hint_line(text)
        if hint:
            chunks.append((self.pt_hint_class, hint + "\n"))
        status = self.status_line()
        if status:
            chunks.append((self.pt_status_class, status + "\n"))
        chunks.append((self.pt_prompt_class, self.prompt))
        return FormattedText(chunks) if FormattedText is not None else chunks

    @staticmethod
    def _dashboard_chunks(dashboard):
        """ANSI 文本 → prompt_toolkit 的 fragments（和父类同一套规则）。"""
        if "\x1b" in dashboard and ANSI is not None:
            return list(to_formatted_text(ANSI(dashboard)))
        return [("", dashboard)]

    def dashboard_budget(self):
        """看板画布的可用高度（终端总行数 - 底部视图栏/提示行/状态行/输入行）。"""
        reserved = 1                                  # 提示行
        # 老版本 repl_kit 没有这个属性时，PromptSession 仍会预留 8 行。
        reserved += max(0, getattr(self, "reserve_space_for_menu", 8))
        try:
            has_status = bool(self.status_line())
        except Exception:                             # 状态行坏了也不该撑破布局
            has_status = False
        if has_status:
            reserved += 1                             # 状态行（专注视图）
        try:
            has_footer = bool(self.footer_text())
        except Exception:                             # 视图栏坏了也别撑破布局
            has_footer = False
        if has_footer:
            reserved += 1                             # 底部视图栏
        reserved += 1                                 # 提示符
        return max(1, self._terminal_rows() - reserved)

    def _build_frame_text(self):
        """真正排版一次，并把看板裁进「一屏固定高度」的视口。

        不再让 prompt_toolkit 自己处理超高内容：那会从底部裁、还会把内容
        推进终端 scrollback。这里从多行看板里取一个窗口：

        * ``scroll_offset == 0``：永远显示最下面一屏（输入/消息/表格底部）；
        * 按 PageUp：窗口整体上移，最多到第一行；PageDown 回到底部；
        * 内容不足一屏：补空行把输入框钉在底部。
        """
        self._in_frame_text = True
        try:
            raw = super().frame_text()
        finally:
            self._in_frame_text = False
        if not raw:
            return raw
        budget = max(1, self.dashboard_budget())
        lines = raw.splitlines()
        # 视图切换可能发生在框架内部（set_view 走 switch_view，已重置）；
        # 这里再兜一层，防止别的调用直接改 view_index。
        if self.view_index != self._scroll_view_index:
            self.scroll_offset = 0
            self._scroll_view_index = self.view_index
        max_offset = max(0, len(lines) - budget)
        self._max_scroll_offset = max_offset
        if self.scroll_offset > max_offset:
            self.scroll_offset = max_offset
        if self.scroll_offset < 0:
            self.scroll_offset = 0
        if len(lines) > budget:
            end = max(0, len(lines) - self.scroll_offset)
            start = max(0, end - budget)
            visible = lines[start:end]
            if self.scroll_offset > 0 and budget >= 3:
                # 顶部用一行说明当前视口位置；其余仍显示原内容。这个提示
                # 本身也算在预算内，所以总高度绝不会超过终端。
                shown = min(self.scroll_offset, max_offset)
                indicator = "↕ 上翻 %d/%d 行 · PgUp 更早 · PgDn 回底部" % (
                    shown, max_offset)
                keep = budget - 1
                start = max(0, len(lines) - budget - self.scroll_offset)
                visible = [indicator] + lines[start:start + keep]
            raw = "\n".join(visible) + "\n"
        elif self.pin_bottom and len(lines) < budget:
            # 内容不足一屏时在底部补齐空行，让视图栏/提示行/输入行永远钉在屏幕最底部。
            pad = budget - len(lines)
            raw = raw.rstrip("\n") + ("\n" * (pad + 1))
        return raw

    def print(self, *objects, **kwargs):
        """全屏 App 内的标准输出改走帧内消息，不写终端历史。

        ``ReplShell.dispatch`` 的 unknown / parse-error / exception 分支会直接
        调 ``print``；在常驻 full-screen Application 里那会把文字写在渲染器
        背后（就是用户看到的「拉一下终端就多出一截历史」）。Warden 把这些
        输出接成 Recent 消息；--exec / 测试 / 普通模式仍按原样输出。
        """
        hook = getattr(self, "output_hook", None)
        if getattr(self, "_app_running", False) and hook is not None:
            try:
                hook(*objects, **kwargs)
            except Exception:                         # noqa: BLE001 - 别因为报错再报错
                pass
            # 全屏时**绝不能**回落到 print：那正是写穿渲染器、留下
            # scrollback 的路径。接不住宁可丢一条消息。
            return
        return super().print(*objects, **kwargs)

    @staticmethod
    def _terminal_rows():
        """这次渲染实际可用的行数。

        优先问 prompt_toolkit 的 output（离屏渲染/测试时它才是真的）；
        拿不到再退回终端尺寸。两者在真 TTY 下是同一个数。

        这里**不能**用 `get_app()`：没有正在跑的 Application 时它会现造一个
        DummyApplication（好几毫秒，还要 load 一遍按键表），而这个函数每画一帧
        都要调一次 —— 以前打字掉帧有一部分就是它。
        """
        try:
            from prompt_toolkit.application.current import get_app_or_none
        except ImportError:                           # pragma: no cover - 老版本
            get_app_or_none = None
        if get_app_or_none is not None:
            try:
                app = get_app_or_none()
                if app is not None:
                    return app.output.get_size().rows
            except Exception:                         # noqa: BLE001
                pass
        return shutil.get_terminal_size((80, 24)).lines


#: session 看板的两种口径。``detail`` 是原来的完整视图；``working`` 只看
#: 「现在/将来要干的 + 真的订上的」，并把过去的实际 session 折成最近一条。
SESSION_UI_MODES = ("working", "detail")

#: 契约表的列（顺序 = 渲染顺序；表头名称也用作 open/close 的列名）。
SESSION_TABLE_COLUMNS = (
    ("时段 ID", {"no_wrap": True}),
    ("时段", {"no_wrap": True}),
    ("时段类型", {"no_wrap": True}),
    ("时段名称", {"no_wrap": True, "overflow": "crop"}),
    ("ID", {"justify": "right", "style": "bold", "no_wrap": True}),
    ("时间", {"no_wrap": True}),
    ("时长", {"justify": "right", "no_wrap": True}),
    ("状态", {"no_wrap": True}),
    ("静音", {"justify": "center", "no_wrap": True}),
    ("契约", {"no_wrap": True}),
    ("标题", {"overflow": "crop", "no_wrap": True}),
    ("task", {"no_wrap": True, "overflow": "crop"}),
)

#: working 口径下默认折叠的列（用户明确要求：类型 / 名称 / 契约）。
WORKING_AUTO_HIDDEN_COLUMNS = ("时段类型", "时段名称", "契约")

#: 任务视图（Tab 第 4 个）的固定列表头。
#: ``标记`` 是 boolean flags 专列，``title`` 紧随其后排第二，``project`` 默认常驻，
#: ``备注`` 用紧凑记号（dur=… / cnt=… / tag=…）汇总被默认折叠的各列。
TASK_TABLE_COLUMNS = (
    ("序号", {"justify": "right", "style": "bold", "no_wrap": True}),
    ("标记", {"no_wrap": True}),
    ("title", {"no_wrap": True, "overflow": "crop"}),
    ("project", {"no_wrap": True, "overflow": "crop"}),
    ("状态", {"no_wrap": True}),
    ("session", {"no_wrap": True}),
    ("截止", {"no_wrap": True}),
    ("备注", {"no_wrap": True, "overflow": "crop"}),
    ("id", {"justify": "right", "style": STYLE_MUTED, "no_wrap": True}),
    ("重复", {"no_wrap": True}),
    ("trigger", {"no_wrap": True}),
    ("formatter", {"no_wrap": True}),
    ("Focusmate", {"no_wrap": True}),
    ("OmniFocus", {"no_wrap": True}),
    ("完成时间", {"no_wrap": True}),
    ("标签", {"no_wrap": True, "overflow": "crop"}),
    ("duration", {"no_wrap": True}),
    ("count", {"no_wrap": True}),
    ("exclusive", {"no_wrap": True}),
)
TASK_CONTENT_COLUMN = "内容"
TASK_DYNAMIC_HIDDEN = {"due", "due_source", "count_invested", "project_id", "project_task", "notes", "note"}

#: 任务视图下默认折叠的列（汇总进「标记」和「备注」，仍可用 open <表头名> 展开）。
TASK_AUTO_HIDDEN_COLUMNS = (
    "id", "重复", "trigger", "formatter",
    "Focusmate", "OmniFocus", "完成时间", "标签",
    "duration", "count", "exclusive",
    # 动态字段默认也收进「备注」：它们一列列自动铺开会把固定列挤到只剩
    # 一个字，导致「后面还有空间，备注却先被裁掉」。需要单独看时
    # `open defer` / `open <字段>` 展开即可。
    "defer",
)

#: project 列默认裁剪长度（窄屏还会进一步按窗口宽度收紧）。
TASK_PROJECT_MAX_LEN = 12

#: 任务表 open/close 认的表头别名（去掉空白、转小写之后比较）。
TASK_COLUMN_ALIASES = {
    "序号": "序号", "number": "序号", "num": "序号", "no": "序号",
    "标记": "标记", "flag": "标记", "flags": "标记", "标识": "标记",
    "title": "title", "标题": "title", "name": "title",
    "project": "project", "项目": "project", "proj": "project",
    "状态": "状态", "status": "状态",
    "session": "session", "场次": "session",
    "截止": "截止", "due": "截止", "截止时间": "截止",
    "备注": "备注", "remark": "备注", "remarks": "备注", "note": "备注", "notes": "备注",
    "id": "id", "内部id": "id", "taskid": "id",
    "重复": "重复", "repeat": "重复", "重复任务": "重复",
    "trigger": "trigger", "触发": "trigger", "触发器": "trigger",
    "formatter": "formatter", "格式": "formatter", "格式化": "formatter",
    "focusmate": "Focusmate", "fm": "Focusmate",
    "omnifocus": "OmniFocus", "of": "OmniFocus",
    "完成时间": "完成时间", "done": "完成时间", "completedat": "完成时间",
    "标签": "标签", "tags": "标签", "tag": "标签",
    "duration": "duration", "dur": "duration", "时长": "duration",
    "count": "count", "cnt": "count",
    "exclusive": "exclusive", "excl": "exclusive", "独占": "exclusive",
    "内容": TASK_CONTENT_COLUMN, "content": TASK_CONTENT_COLUMN,
}

#: 任务视图的列折叠状态也落同一个 warden-ui.json，mode 用这个名字。
TASK_UI_MODE = "task"
UI_PREF_MODES = SESSION_UI_MODES + (TASK_UI_MODE,)

#: `open/close` 认的表头别名（去掉空白、转小写之后比较）。
SESSION_COLUMN_ALIASES = {
    "时段id": "时段 ID", "periodid": "时段 ID", "pid": "时段 ID",
    "时段": "时段", "时间窗": "时段", "period": "时段",
    "时段类型": "时段类型", "类型": "时段类型", "periodtype": "时段类型",
    "时段名称": "时段名称", "名称": "时段名称", "periodname": "时段名称",
    "id": "ID", "编号": "ID", "序号": "ID",
    "时间": "时间", "time": "时间",
    "时长": "时长", "duration": "时长",
    "状态": "状态", "status": "状态",
    "静音": "静音", "quiet": "静音", "mute": "静音",
    "契约": "契约", "contract": "契约",
    "标题": "标题", "title": "标题",
    "task": "task", "任务": "task", "task列": "task",
}


def normalize_session_column(text):
    """用户敲的表头名称 → 规范列名；认不出返回 None。"""
    compact = re.sub(r"\s+", "", str(text or "")).strip().lower()
    if not compact:
        return None
    return SESSION_COLUMN_ALIASES.get(compact)


def _crop_display(text, max_width):
    """按终端显示宽度裁剪文本，超长末尾换 ``…``。"""
    raw = str(text or "")
    if not max_width or max_width <= 0:
        return raw
    if repl_kit.display_width(raw) <= max_width:
        return raw
    out = []
    width = 0
    for ch in raw:
        cw = repl_kit.display_width(ch)
        if width + cw > max_width:
            if max_width > 1 and out:
                out[-1] = "…"
            break
        out.append(ch)
        width += cw
    return "".join(out)


def format_compact_timestamp(raw, base_date=None):
    """方案 A 紧凑时间戳：同天 ``HH:MM`` / 明昨 ``+1/-1 HH:MM`` / 同年 ``MM/DD HH:MM``。"""
    text = str(raw or "").strip()
    if not text or text == "—":
        return "—"
    if isinstance(base_date, datetime.datetime):
        base_date = base_date.date()
    elif not isinstance(base_date, datetime.date):
        base_date = datetime.date.today()
    dt = ts.parse_iso_datetime(text)
    if dt is not None:
        day_diff = (dt.date() - base_date).days
        hm = dt.strftime("%H:%M")
        if day_diff == 0:
            return hm
        if day_diff in (-1, 1):
            return "%+d %s" % (day_diff, hm)
        if dt.date().year == base_date.year:
            return "%02d/%02d %s" % (dt.date().month, dt.date().day, hm)
        return "%02d/%02d/%02d %s" % (dt.date().year % 100, dt.date().month,
                                      dt.date().day, hm)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s]+(\d{1,2}:\d{2}))?", text)
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            hm = m.group(4) or ""
            if hm and len(hm) == 4:
                hm = "0" + hm
            day_diff = (d - base_date).days
            if day_diff == 0 and hm:
                return hm
            if day_diff in (-1, 1):
                return ("%+d %s" % (day_diff, hm)).strip()
            if d.year == base_date.year:
                return ("%02d/%02d %s" % (d.month, d.day, hm)).strip()
            return ("%02d/%02d/%02d %s" % (d.year % 100, d.month, d.day, hm)).strip()
        except ValueError:
            pass
    return text.replace("T", " ")[:16]


def task_dynamic_keys(tasks):
    """任务里出现的自定义字段列（due / due_source 等内部字段不重复展开）。"""
    out = []
    for task in tasks or ():
        for key in ((task or {}).get("fields") or {}):
            if key not in out and key not in TASK_DYNAMIC_HIDDEN:
                out.append(str(key))
    return out


def task_table_headers(tasks):
    """任务表当前可显示的完整表头（固定列 + 自定义字段列）。"""
    fixed = [header for header, _kw in TASK_TABLE_COLUMNS]
    keys = [key for key in task_dynamic_keys(tasks) if key not in fixed]
    if not tasks:
        return fixed + [TASK_CONTENT_COLUMN]
    return fixed + keys


def normalize_task_column(text, tasks=None):
    """用户敲的任务表表头 → 规范列名；认不出返回 None。"""
    raw = str(text or "").strip()
    compact = re.sub(r"\s+", "", raw).strip().lower()
    if not compact:
        return None
    col = TASK_COLUMN_ALIASES.get(compact)
    if col:
        return col
    for key in task_dynamic_keys(tasks):
        if key == raw or re.sub(r"\s+", "", key).lower() == compact:
            return key
    return None


def visible_session_rows(rows, mode="detail"):
    """按看板口径筛 session 行 → ``(items, collapsed_past)``。

    ``items`` 是 ``[(原表格序号, row), ...]``。序号故意保留**原 self.rows 里的
    位置**，因为 `task -a N` / `cancel N` / `mute N` 这些命令一直按这个数找行；
    working 模式只是少画几条，不能把命令的编号也改掉。

    working 口径：
    * 当前/将来：保留计划行与实际行；``认账`` 那种「计划被别的场次覆盖」的
      伪行不显示（真正的实际场次会自己出现在 API 行里）。
    * 过去：只留最近一条**实际存在**的 session；更早的折成 caption 计数。
    * 过去但根本没订上的计划行不显示（它不是实际内容）。
    """
    items = list(enumerate(rows or [], 1))
    if mode != "working":
        return items, 0
    current_future, past_actual = [], []
    for idx, row in items:
        planned_only = bool(row.get("planned")) and not row.get("id")
        if planned_only and row.get("adopted"):
            # 认账行：它是契约被实际场次覆盖后的解释，不是一个 session。
            continue
        if row.get("phase") == "已结束":
            if planned_only:
                continue
            past_actual.append((idx, row))
        elif planned_only and row.get("phase") == "进行中":
            # 契约时间到了，但 API 里没有这一场：它不是一个真正在进行的
            # session（常见于取消后另约了 15 分钟后的场）。working 只画事实，
            # 不能拿「计划窗口」冒充「正在发生」。
            continue
        else:
            current_future.append((idx, row))
    past_actual.sort(key=lambda pair: (pair[1].get("startMs") or 0, pair[0]))
    kept_past = past_actual[-1:]
    collapsed = max(0, len(past_actual) - 1)
    out = current_future + kept_past
    out.sort(key=lambda pair: (pair[1].get("startMs") or 0, pair[0]))
    return out, collapsed


class Warden(object):
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

    # -- 网络：读也异步、写乐观（repl_kit.jobs）-------------------------
    #
    #     主线程                          worker 线程
    #     ────────                        ───────────
    #     乐观改本地（立刻看见）──submit──> bridge.call(...)   ← 真网络
    #     drain_net() ←──── outcome ─────  成功 / 失败
    #       ├ 成功：填上真 id / 真结果
    #       └ 失败：restore() 回滚 + 警告 + 记进失败账（re-action 重放）
    #
    # 三条规矩：
    #   1. **worker 只碰 bridge 和 rep**，任务库 / data.csv / 画面一律在主线程改；
    #   2. 每个动作都有 `名字 + 参数 + 怎么干 + 本地那半件事(apply)`，
    #      所以失败之后换个进程也能重放（re-action）；
    #   3. **多步事务不拆**：时段剪断、收尾、布局重写这些一次改好几场的内部写
    #      仍然同步 —— 中途失败回滚一半比不回滚更糟。

    def _net_touched(self):
        """worker 线程只说一句「画面脏了」。业务状态一律留给主线程。"""
        self._net_dirty = True

    def net_action(self, name, label, args=None, source=SRC_FOCUSMATE):
        """一条可重放的外部动作。``source`` 决定面板上的 ``[focusmate]`` 这种标签。"""
        return repl_kit.Action(name, label, dict(args or {}), source=source)

    def op_keep_session(self, argv, label, timeout=600, wait=False, on_ok=None):
        """跑一次 keep_session.py（下单/对账/启停 daemon）—— 走后台队列。

        ``wait=True`` 是给「对账」这种必须先有结果的流程用的（会等这一批落地）。
        """
        action = self.net_action("keep_session.run", label,
                                 {"argv": [str(a) for a in argv],
                                  "timeout": int(timeout)}, source=SRC_KEEP)
        job = self.jobs.submit(action, on_ok=on_ok)
        if wait:
            self.wait_net(timeout + 60)
        return job

    def op_calendar(self, argv, label, timeout=300, on_ok=None):
        """跑一次 download_calendar.py（日历 → data.csv）—— 走后台队列。"""
        action = self.net_action("calendar.sync", label,
                                 {"argv": [str(a) for a in argv],
                                  "timeout": int(timeout)}, source=SRC_CALENDAR)
        return self.jobs.submit(action, on_ok=on_ok)

    def drain_net(self):
        """每帧一次：把后台跑完的网络操作搬回主线程（成功/失败回调都在这里跑）。"""
        outcomes = self.jobs.drain()
        if outcomes or self._net_dirty:
            self._net_dirty = False
            self.invalidate()
        return outcomes

    def _register_net_actions(self):
        """给队列登记「名字 → 怎么干 / 本地怎么落」（re-action 靠这两半重放）。"""
        self.jobs.register("session.snapshot", self.net_snapshot,
                           apply=self._apply_snapshot)
        self.jobs.register("task.pull", self.net_task_pull,
                           apply=self._apply_task_pull)
        self.jobs.register("stat.history", self.net_stat_history,
                           apply=self._apply_stat_history)
        self.jobs.register("task.assign", self.net_task_assign,
                           apply=self._apply_task_assign)
        self.jobs.register("task.detach", self.net_task_detach,
                           apply=self._apply_task_detach)
        self.jobs.register("task.complete", self.net_task_complete,
                           apply=self._apply_task_complete)
        self.jobs.register("session.cancel", self.net_session_cancel,
                           apply=self._apply_session_cancel)
        # 取消/认罪/「不去了」这些动作以前有几条路会在提示符线程上直接等
        # 浏览器（console.status 转圈不算进度条，输入也一起冻住）。现在它们
        # 全走同一个后台队列：面板里有标题、步骤和进度，失败照样进失败账。
        self.jobs.register("session.missed", self.net_session_missed)
        self.jobs.register("session.confess_cancel", self.net_session_confess_cancel)
        self.jobs.register("session.relocate_cancel", self.net_session_relocate_cancel)
        self.jobs.register("session.mute", self.net_session_mute,
                           apply=self._apply_session_mute)
        self.jobs.register("session.title", self.net_session_title,
                           apply=self._apply_session_title)
        # ── OmniFocus（osascript 子进程：同样不属于「软件内部自己完成」）──
        self.jobs.register("omnifocus.list", self.net_omnifocus_list,
                           apply=self._apply_omnifocus_list)
        self.jobs.register("omnifocus.project_sync", self.net_omnifocus_project_sync)
        self.jobs.register("omnifocus.clipboard", self.net_omnifocus_clipboard)
        self.jobs.register("omnifocus.import", self.net_omnifocus_import,
                           apply=self._apply_omnifocus_import)
        self.jobs.register("omnifocus.complete", self.net_omnifocus_complete,
                           apply=self._apply_omnifocus_complete)
        self.jobs.register("omnifocus.drop", self.net_omnifocus_drop,
                           apply=self._apply_omnifocus_drop)
        self.jobs.register("omnifocus.writeback", self.net_omnifocus_writeback,
                           apply=self._apply_omnifocus_writeback)
        # ── 别的进程：keep_session（下单/对账/daemon）与日历同步 ──
        self.jobs.register("keep_session.run", self.net_keep_session,
                           apply=self._apply_keep_session)
        self.jobs.register("calendar.sync", self.net_calendar_sync,
                           apply=self._apply_calendar_sync)
        # 重放的时候没有当初那个闭包了 —— 失败仍然要有人说话
        self.jobs.on_failure = self._net_action_failed

    # -- worker 侧：只有 bridge 调用 + rep（日志/进度）------------------

    def net_snapshot(self, args, rep):
        """拉某一天的 session 列表。**读**操作：只回数据，不预测。"""
        day_iso = str(args.get("date") or self.sel_date.isoformat())
        rep.label("拉 session %s" % day_iso[5:])
        rep.progress(0.05)
        res = self.bridge.call("snapshot", timeout=int(args.get("timeout") or 180),
                               date=day_iso)
        rep.progress(1.0)
        rep.log("%s 拉到 %d 场" % (day_iso[5:], len(res.get("sessions") or [])), "ok")
        return {"date": day_iso, "res": res}

    def net_task_pull(self, args, rep):
        """拉 Focusmate task（完成状态）。**读**操作。"""
        rep.label("拉 Focusmate task")
        rep.progress(0.05)
        res = self.bridge.call("tasks", timeout=180)
        remote = res.get("tasks") if isinstance(res, dict) else res
        rep.progress(1.0)
        rep.log("Focusmate task %d 条" % len(remote or []), "ok")
        return {"remote": remote or []}

    def net_stat_history(self, args, rep):
        """拉生涯统计历史（只读公开 API，分块）。**读**操作。"""
        rep.label("拉生涯统计")
        rep.progress(0.05)
        history, note = st.load(force=bool(args.get("force")), full=bool(args.get("full")))
        rep.progress(1.0)
        try:
            count = len((history or {}).get("sessions") or [])
        except AttributeError:
            count = 0
        rep.log("生涯统计：%d 场（%s）" % (count, note), "ok")
        return {"history": history, "note": note}

    def net_task_assign(self, args, rep):
        """把任务挂到 Focusmate 那一场（**写**操作，乐观之后在后台跑的就是它）。"""
        rep.label("挂任务 #%s 到 %s" % (args.get("task_id"), args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("add_task", timeout=240, startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"),
                               title=args.get("title") or "",
                               notes=args.get("notes") or "")
        if not res.get("verified"):
            raise ValueError("Focusmate 里没能确认 task 已挂上（本地已回滚）")
        fm_id = res.get("taskId") or (res.get("task") or {}).get("id")
        if not fm_id:
            raise ValueError("Focusmate 说挂上了，但没给 task id")
        rep.progress(1.0)
        rep.log("任务 #%s 挂上了（Focusmate task %s）"
                % (args.get("task_id"), str(fm_id)[:8]), "ok")
        return {"task_id": fm_id, "via": res.get("via"),
                "existingCount": res.get("existingCount")}

    def net_task_detach(self, args, rep):
        """删掉 Focusmate 上那条 task（下架 / 转移）。**写**操作。"""
        fm_id = args.get("focusmate_task_id")
        if not fm_id:
            raise ValueError("这条任务没有 Focusmate task id，删不了")
        rep.label("删 Focusmate task %s" % str(fm_id)[:8])
        rep.progress(0.05)
        res = self.bridge.call("delete_task", timeout=180, taskId=fm_id)
        if not res.get("verified"):
            still = (res.get("stillThere") or {}).get("title") or ""
            raise ValueError("Focusmate 说删了，但复核还在%s"
                             % (("：" + still) if still else ""))
        rep.progress(1.0)
        rep.log("Focusmate task %s 已删除" % str(fm_id)[:8], "ok")
        return {"deleted": fm_id}

    def net_task_complete(self, args, rep):
        """把 Focusmate 那条 task 勾完成。**写**操作。"""
        fm_id = args.get("focusmate_task_id")
        if not fm_id:
            raise ValueError("这条任务没有 Focusmate task id，勾不了")
        rep.label("勾完成 #%s" % args.get("task_id"))
        rep.progress(0.05)
        res = self.bridge.call("complete_task", timeout=180, taskId=fm_id)
        if not res.get("verified"):
            raise ValueError("Focusmate 没确认勾上")
        rep.progress(1.0)
        rep.log("Focusmate task %s 已勾完成" % str(fm_id)[:8], "ok")
        return {"completed_at": res.get("completedAt") or ts._now(),
                "already": bool(res.get("already"))}

    def net_session_cancel(self, args, rep):
        """取消一场 session。**写**操作（浏览器可能冷启动几十秒，全在 worker 上）。"""
        rep.label("取消 %s %s" % (str(args.get("date") or "")[5:], args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("cancel", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"))
        rep.progress(0.9)
        if not res.get("verified"):
            raise ValueError("取消后复核：该 session 还在")
        rep.progress(1.0)
        rep.log("%s 那场已取消" % (args.get("start") or "?"), "ok")
        return {"cancelled": args.get("meeting_id")}

    def net_session_missed(self, args, rep):
        """把一场「到点没开始」记成 missed + abandoned（纯本地落盘，也放后台）。

        这个动作本身很快；放队列里是为了两件事：一是输入线程永远不等文件锁/磁盘，
        二是如果哪天 state 文件在慢盘/被杀进程卡住，面板至少明确显示在做哪一步。
        """
        key = str(args.get("key") or "")
        day_iso = str(args.get("date") or "")
        start = str(args.get("start") or "?")
        why = str(args.get("why") or
                  ("到点没开始，我选了「不去了」（%s %s）" % (day_iso, start)))
        rep.label("记录缺失 %s %s" % (day_iso[5:], start))
        rep.progress(0.1)
        try:
            ks.mark_abandoned(key, why="nostart", note=why)
        except Exception as e:                  # noqa: BLE001 - 记不上也别挡住下一步
            rep.log("记「已放弃」失败：%s" % e, "warn")
        rep.progress(0.55)
        try:
            ks.mark_slot(key, "missed", note="nostart")
        except Exception as e:                  # noqa: BLE001
            rep.log("记 missed 失败：%s" % e, "warn")
        rep.progress(1.0)
        rep.log("%s 已记为缺失（daemon 不再重订）" % start, "ok")
        return {"key": key, "why": why}

    def net_session_confess_cancel(self, args, rep):
        """认罪后把 daemon 已经重订回来的场次再取消掉。

        以前这一段在 ``confess()`` 里直接 ``bridge.call``，还要在一个
        ``console.status`` 里等浏览器 —— 宣言刚敲完画面像死了。现在拆成
        一个可重放的后台动作：逐场查快照、命中就取消、每步更新进度。
        """
        keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
        now = datetime.datetime.now()
        checked = cancelled = past = missing = 0
        failed = []
        total = max(1, len(keys))
        for index, key in enumerate(keys, 1):
            rep.progress((index - 1) / total)
            try:
                day = datetime.date.fromisoformat(key[:10])
                hhmm = key[11:]
                got = sch.parse_hhmm(hhmm)
            except (ValueError, TypeError):
                rep.log("%s 不是合法场次 key，跳过" % key, "warn")
                continue
            if not got:
                rep.log("%s 不是合法场次 key，跳过" % key, "warn")
                continue
            start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
            if start <= now:
                past += 1
                rep.log("%s 已经过去：只判放弃，不动 API" % key, "info")
                continue
            rep.label("查 %s 是否被重订" % key)
            try:
                res = self.bridge.call("snapshot", timeout=180, date=day.isoformat())
            except BridgeError as e:
                failed.append("%s 查不到现状：%s" % (key, e))
                rep.log("%s 查不到现状：%s" % (key, e), "warn")
                continue
            checked += 1
            hit = None
            for sess in res.get("sessions") or []:
                if sess.get("startLabel") == hhmm:
                    hit = sess
                    break
            if not hit:
                missing += 1
                rep.log("%s 现在 API 里没有（还没重订回来）" % key, "info")
                continue
            rep.label("取消重订的 %s" % key)
            try:
                out = self.bridge.call("cancel", startMs=hit.get("startMs"),
                                       meetingId=hit.get("id"))
            except BridgeError as e:
                failed.append("%s 取消失败：%s" % (key, e))
                rep.log("%s 取消失败：%s" % (key, e), "warn")
                continue
            if not out.get("verified"):
                failed.append("%s 取消后复核还在" % key)
                rep.log("%s 取消后复核还在" % key, "warn")
                continue
            cancelled += 1
            rep.log("%s 已真的取消（API 复核：没了）" % key, "ok")
            rep.progress(index / total)
        if failed:
            raise ValueError("；".join(failed))
        rep.progress(1.0)
        return {"keys": keys, "checked": checked, "cancelled": cancelled,
                "past": past, "missing": missing}

    def net_session_relocate_cancel(self, args, rep):
        """挪场次时批量取消旧 API session（一个动作 = 一条进度条）。"""
        moves = list(args.get("moves") or [])
        total = max(1, len(moves))
        done = []
        for index, move in enumerate(moves, 1):
            label = str(move.get("start") or "?")
            rep.label("取消旧 session %s" % label)
            rep.progress((index - 1) / total)
            res = self.bridge.call("cancel", timeout=180,
                                   startMs=move.get("startMs"),
                                   meetingId=move.get("meeting_id"))
            if not res.get("verified"):
                raise ValueError("旧 session %s 取消后复核还在" % label)
            done.append(move.get("meeting_id"))
            rep.log("旧 session %s 已取消" % label, "ok")
            rep.progress(index / total)
        return {"cancelled": done}

    def net_session_mute(self, args, rep):
        """改 quiet mode。**写**操作。"""
        want = bool(args.get("quiet"))
        rep.label("%s quiet %s" % (args.get("start") or "?", "ON" if want else "off"))
        rep.progress(0.05)
        res = self.bridge.call("mute", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"), quiet=want)
        if not res.get("verified"):
            raise ValueError("改完复核不一致：现在是 %s" % (res.get("quiet"),))
        rep.progress(1.0)
        rep.log("%s quiet = %s" % (args.get("start") or "?", "ON" if want else "off"), "ok")
        return {"quiet": bool(res.get("quiet"))}

    def net_session_title(self, args, rep):
        """写 session 标题。**写**操作。"""
        rep.label("写标题 %s" % (args.get("start") or "?"))
        rep.progress(0.05)
        res = self.bridge.call("title", startMs=args.get("startMs"),
                               meetingId=args.get("meeting_id"),
                               title=args.get("title") or "")
        if not res.get("verified"):
            raise ValueError("写完复核不一致：现在是 %r" % (res.get("actual"),))
        rep.progress(1.0)
        rep.log("%s 标题已写入" % (args.get("start") or "?"), "ok")
        return {"title": args.get("title") or ""}

    # -- worker 侧：OmniFocus（osascript）-----------------------------

    def net_omnifocus_list(self, args, rep):
        """读一遍 OmniFocus 任务列表。**读**操作。"""
        rep.label("读 OmniFocus")
        rep.progress(0.1)
        rows = self.omnifocus.list_tasks(refresh=bool(args.get("refresh")))
        rep.progress(1.0)
        rep.log("OmniFocus %d 条" % len(rows or []), "ok")
        return {"rows": rows or []}

    def net_omnifocus_clipboard(self, args, rep):
        """读 macOS 剪贴板里的 OmniFocus 链接（**外部读**，不能在提示符线程跑）。"""
        rep.label("读剪贴板（OmniFocus）")
        rep.progress(0.1)
        items = [dict(item) for item in of.clipboard_links()]
        rep.progress(1.0)
        rep.log("剪贴板里拿到 %d 条 OmniFocus 链接" % len(items), "ok")
        return {"items": items}

    def net_omnifocus_import(self, args, rep):
        """``task import`` 的第 1 步：读 OmniFocus 补齐标题/时长 + 核对重复设置。

        **读**操作（一次 list + 每条一次 repetition_info）。真正的建任务、
        去重、问「复制重复设置吗」都在主线程那半件事里（见
        :meth:`_apply_omnifocus_import`）。
        """
        items = [dict(item) for item in (args.get("items") or []) if item]
        single_task = bool(args.get("single_task"))
        rep.label("获取任务（OmniFocus）")
        rep.progress(0.05)
        # 剪贴板也是外部世界（osascript / pbpaste）：在 worker 里读，别卡输入。
        if args.get("clipboard"):
            items = [dict(item, from_clipboard=True) for item in of.clipboard_links()]
            if not items:
                raise ValueError(
                    "剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本四个 flavor "
                    "都试过了）。在 OmniFocus 里 ⌘C 一条任务再回来敲 `task import`；"
                    "也可以手写 `task import [标题](omnifocus:///task/PK)`。")
        elif args.get("match_clipboard"):
            items = self._task_import_match_clipboard(items, of.clipboard_links())
        rep.progress(0.1)
        items = self._task_import_resolve(items, warn=lambda text, kind="warn":
                                          rep.log(text, kind))
        # 剪贴板里可能是**项目**（OmniFocus 的 project_root）：项目不进任务库，
        # 进项目库；时长不齐的项目直接拒绝（一条都不建），见 _task_import_projects。
        items, projects, refused = self._task_import_projects(
            items, warn=lambda text, kind="warn": rep.log(text, kind))
        if refused:
            rep.log("拒绝导入 %d 个项目（本周任务检查不通过）" % len(refused), "error")
            return {"items": [], "projects": [], "refused": refused,
                    "repeats": {}, "warnings": []}
        for payload in projects:
            week = payload.get("week") or {}
            rep.log("项目「%s」：%d 条任务（其中 %d 条已完成）· 本周会产出 %d 条"
                    % (payload.get("title"), len(payload.get("tasks") or []),
                       int(payload.get("done") or 0), int(week.get("count") or 0)),
                    "ok")
        rep.progress(0.5)
        repeats, warnings = {}, []
        checker = None if single_task else getattr(self.omnifocus, "repetition_info", None)
        for index, item in enumerate(items):
            if not item or not item.get("id") or checker is None:
                continue
            try:
                info = checker(item["id"])
            except Exception as e:                  # noqa: BLE001 - 核对不了就按普通任务导
                warnings.append("没法核对「%s」是不是重复任务：%s（先按普通任务导入）"
                                % (item.get("title") or item.get("id"), e))
                continue
            plan = task_repeat_plan_from_omnifocus(
                info, sessions_for_day=self.sessions_for_day_plan)
            if plan is not None:
                repeats[index] = {"info": info, "plan": plan}
            rep.progress(0.5 + 0.5 * (index + 1) / max(1, len(items)))
        rep.log("拿到 %d 条任务%s%s"
                % (len(items),
                   ("，其中 %d 条是重复任务" % len(repeats)) if repeats else "",
                   ("；另有 %d 个项目" % len(projects)) if projects else ""),
                "ok")
        return {"items": items, "projects": projects, "refused": [],
                "repeats": repeats, "warnings": warnings}

    def net_omnifocus_project_sync(self, args, rep):
        """每日项目对账的**读**侧：读一份 OmniFocus 清单，和本地项目快照做 diff。

        在 worker 里逐项目查重复任务规则，避免主线程被 osascript 卡住。返回的
        ``payload`` 已经带 ``week`` / 重复信息，主线程决定自动拉新增还是发起冲突问答。
        """
        local_projects = list(args.get("projects") or [])
        rep.label("每日检查 OmniFocus 项目（%d 个）" % len(local_projects))
        rep.progress(0.05)
        rows = self.omnifocus.list_tasks(refresh=True)
        by_pk = {str(row.get("id") or ""): row for row in rows or ()}
        out = []
        for index, local in enumerate(local_projects, 1):
            pk = str(((local or {}).get("omnifocus") or {}).get("id") or "")
            title = (local or {}).get("title") or pk
            rep.progress(0.05 + 0.9 * index / max(1, len(local_projects)))
            root = by_pk.get(pk)
            if root is None:
                payload = {
                    "kind": "project", "id": pk, "title": title,
                    "url": pj.project_url(pk), "tasks": [], "missing": [],
                    "depth_errors": [], "done": 0, "warnings": [],
                }
                out.append({"project_id": local.get("id"), "title": title,
                            "payload": payload,
                            "diff": pj.diff_tasks(local.get("tasks") or [], []),
                            "remote_found": False, "invalid": ""})
                continue
            try:
                payload = pj.project_payload(root, rows)
                payload = pj.attach_week(
                    payload, self._project_repeat_infos(
                        payload, lambda text, kind="warn": rep.log(text, kind)))
            except Exception as e:                  # noqa: BLE001 - 单个项目坏了不挡其余
                out.append({"project_id": local.get("id"), "title": title,
                            "payload": None, "diff": {},
                            "remote_found": True,
                            "invalid": "读项目失败：%s" % e})
                continue
            invalid = ""
            if payload.get("depth_errors"):
                invalid = pj.format_depth_errors(payload)
            elif payload.get("missing") or payload.get("week_missing"):
                invalid = pj.format_missing(payload)
            out.append({"project_id": local.get("id"), "title": title,
                        "payload": payload,
                        "diff": pj.diff_tasks(local.get("tasks") or [],
                                              payload.get("tasks") or []),
                        "remote_found": True, "invalid": invalid})
        rep.progress(1.0)
        rep.log("项目对账读完：%d 个" % len(out), "ok")
        return {"projects": out}

    def net_omnifocus_complete(self, args, rep):
        """写回完成 + 截止时间。**写**操作（乐观：本地先记 writeback）。"""
        pk = str(args.get("pk") or "")
        rep.label("OmniFocus 完成 %s" % pk[:8])
        rep.progress(0.1)
        res = self.omnifocus.complete(pk, at=args.get("at"), due=args.get("due"),
                                      yes=True)
        after = res.get("after") if isinstance(res, dict) else None
        advanced = bool(isinstance(res, dict) and res.get("repeat_clone_id")
                        and str(res.get("repeat_clone_id")) != pk)
        if isinstance(after, dict) and not after.get("completed") and not advanced:
            raise ValueError("写完复核 completed 仍为 false（pk %s）" % pk)
        rep.progress(1.0)
        rep.log("OmniFocus %s 完成写回%s" % (pk[:8], "（重复任务已翻篇）" if advanced else ""),
                "ok")
        return {"pk": pk, "res": res or {}, "advanced": advanced}

    def net_omnifocus_drop(self, args, rep):
        """写回放弃。**写**操作。"""
        pk = str(args.get("pk") or "")
        rep.label("OmniFocus 放弃 %s" % pk[:8])
        rep.progress(0.1)
        res = self.omnifocus.drop(pk, at=args.get("at"),
                                  all_occurrences=bool(args.get("all_occurrences")),
                                  yes=True)
        after = res.get("after") if isinstance(res, dict) else None
        advanced = bool(isinstance(res, dict) and res.get("repeat_clone_id")
                        and str(res.get("repeat_clone_id")) != pk)
        if isinstance(after, dict) and not after.get("dropped") and not advanced:
            raise ValueError("写完复核 dropped 仍为 false（pk %s）" % pk)
        rep.progress(1.0)
        rep.log("OmniFocus %s 已放弃" % pk[:8], "ok")
        return {"pk": pk, "res": res or {}, "advanced": advanced}

    def net_omnifocus_writeback(self, args, rep):
        """把「Focusmate 说完成了」的那几条一次性写回 OmniFocus。**写**操作。

        一条一次子进程，所以进度是「第几条 / 共几条」；单条失败不挡其余。
        """
        task_ids = [int(t) for t in (args.get("task_ids") or [])]
        done, failed = [], []
        rep.label("写回 OmniFocus（%d 条）" % len(task_ids))
        for index, task_id in enumerate(task_ids, 1):
            try:
                task = self.task_store.get(task_id)
            except (ValueError, TypeError):
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            rep.progress(index / max(1, len(task_ids)))
            try:
                res = self.omnifocus.complete(pk, at=None,
                                              due=self.omni_due_of(task) or None, yes=True)
            except of.OmniFocusError as exc:
                failed.append((task_id, str(exc)))
                rep.log("任务 %s 写回失败：%s" % (task_id, exc), "warn")
                continue
            done.append({"task_id": task_id, "pk": pk, "res": res or {}})
            rep.log("任务 %s → OmniFocus %s" % (task_id, pk[:8]), "ok")
        if not done and failed:
            raise ValueError("全部写回失败：%s" % failed[0][1])
        return {"done": done, "failed": failed}

    # -- worker 侧：别的进程（keep_session / 日历）---------------------

    def net_keep_session(self, args, rep):
        """跑一次 ``keep_session.py``（下单/对账/启停 daemon）。**外部**操作。

        ``release_browser``：在跑子进程前先让 bridge 放开 Chromium profile。
        这一步以前在主线程用 ``_release_browser()`` 做，浏览器卡住时输入也卡住；
        现在和真正的 check 一起在 worker 上，进度条会依次显示两步。
        """
        argv = [str(a) for a in (args.get("argv") or [])]
        if args.get("release_browser"):
            rep.label("释放浏览器给 daemon")
            rep.progress(0.05)
            try:
                up, browser, _ = self.bridge.browser_state()
                if up and browser:
                    self.bridge.call("release", timeout=60)
                    rep.log("浏览器已释放", "ok")
            except BridgeError as e:
                rep.log("释放浏览器失败（继续跑，可能撞锁）：%s" % e, "warn")
        rep.label("keep_session %s" % (" ".join(argv) or "check"))
        rep.progress(0.1)
        rc, out, err = run_keep_session(*argv, timeout=int(args.get("timeout") or 600))
        rep.progress(1.0)
        if rc != 0:
            raise ValueError("keep_session %s 退出码 %d：%s"
                             % (" ".join(argv), rc, (err or out or "").strip()[:200]))
        rep.log("keep_session %s 完成" % (" ".join(argv) or "check"), "ok")
        return {"rc": rc, "out": out, "err": err, "argv": argv}

    def net_calendar_sync(self, args, rep):
        """跑 ``download_calendar.py``（日历 → data.csv）。**外部**操作。"""
        argv = [str(a) for a in (args.get("argv") or ["download"])]
        rep.label("日历 %s" % " ".join(argv))
        rep.progress(0.1)
        rc, out, err = run_download_calendar(*argv,
                                            timeout=int(args.get("timeout") or 300))
        rep.progress(1.0)
        if rc != 0:
            raise ValueError("download_calendar %s 退出码 %d：%s"
                             % (" ".join(argv), rc, (err or out or "").strip()[:200]))
        rep.log("日历 %s 完成" % " ".join(argv), "ok")
        return {"rc": rc, "out": out, "err": err, "argv": argv}

    # -- 主线程侧：成功之后把本地落定（apply）--------------------------

    def _apply_snapshot(self, payload, args):
        """session 列表到了：换成真数据（读操作，没有回滚这回事）。"""
        payload = payload or {}
        res = payload.get("res") or {}
        now_ms = res.get("now") or int(time.time() * 1000)
        api_rows = []
        for sess in res.get("sessions") or []:
            row = dict(sess)
            row["phase"] = self.phase_of(sess["startMs"], sess["endMs"], now_ms)
            api_rows.append(row)
        self.api_rows = api_rows
        self.rows_source = "bridge"
        self.rows_note = ""
        self.rows_at = time.time()
        self.read_at["session"] = self.rows_at
        self.rebuild_rows(now_ms)
        self.invalidate()
        return len(self.rows)

    def _apply_task_pull(self, payload, args):
        """Focusmate task 到了：对账完成状态（本地写 + 写回 OmniFocus）。"""
        remote = (payload or {}).get("remote") or []
        started = time.monotonic()
        before = len(self.task_store.load_archive().get("records") or [])
        try:
            archived, matched = self.task_store.reconcile(remote)
        except Exception as e:                     # noqa: BLE001 - 对账坏了别掀 TUI
            self.had_error = True
            self.say("Focusmate task 对账失败：%s" % e, "error")
            return 0
        self.task_sync_at = time.time()
        self.read_at["task"] = self.task_sync_at
        summary = self._focusmate_task_summary(remote)
        summary.update({
            "source": "net.task_pull",
            "matched": matched,
            "archived": archived,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        })
        rl.log_event("focusmate", "task_pull_result", **summary)
        if archived:
            self.announce_completions(before)
        self.writeback_completions()
        # reconcile() 改了任务状态；契约表 task 列里的 ✓/✗ 是 rebuild_rows()
        # 算出来的缓存，不重算就会一直显示同步前的旧状态（用户报的
        # 「任务都删了/勾了，session 上还显示旧任务」就是这一层缓存没拍掉）。
        self.rebuild_rows()
        self.say("Focusmate task 同步：匹配 %d 条，新增完成归档 %d 条。"
                 % (len(matched), archived), "ok")
        self.invalidate()
        return archived

    def _apply_stat_history(self, payload, args):
        """统计历史到了：算数字（纯本地 CPU，放主线程没关系）。"""
        payload = payload or {}
        history, note = payload.get("history"), payload.get("note") or ""
        try:
            self.stat = st.compute_stats(history)
        except Exception as e:                      # noqa: BLE001 - 数据变形的兜底
            self.had_error = True
            self.stat_note = "数据没法算：%s" % e
            self.say("生涯统计算不动：%s" % e, "error")
            return 0
        self.stat_note = note
        self.stat_at = datetime.datetime.now()
        self.read_at["stat"] = time.time()
        self.say("生涯统计：%s（完成 %d 场 / %d 天）"
                 % (note, self.stat["total_completed"], self.stat["days"]),
                 "ok" if history else "warn")
        self.invalidate()
        return 1

    def _apply_task_assign(self, payload, args):
        """挂上了：把乐观的「在途」换成真 task id（重放时同样走这里）。"""
        payload = payload or {}
        task_id = args.get("task_id")
        session = dict(args.get("session") or {})
        fm_id = payload.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        session["task_id"] = fm_id
        current = task.get("session") or {}
        same = (current.get("date") == session.get("date")
                and current.get("start") == session.get("start"))
        if current and not same:
            # 排进队列之后这条任务又被挪到别处了：这次落地不许把它拽回来
            self.jobs._log_line("任务 %s 已经挂到 %s %s，这次落地跳过"
                                % (self.task_label(task), current.get("date"),
                                   current.get("start")), "warn")
            return task["id"]
        # 已经是同一场就别再 assign 一次：assign 会整体覆盖 session 字典，
        # 把 previous_due / previous_session 这些留档冲掉。
        if not same:
            self.task_store.assign(task["id"], session)
        self.task_store.attach_focusmate(task["id"], {
            "task_id": fm_id,
            "session_id": args.get("meeting_id"),
            "attached_at": ts._now(),
            "title": args.get("title"),
            "via": payload.get("via"),
        }, session=None)
        self.task_store.settle_focusmate(task["id"], FM_PENDING)   # 落地了：撤掉在途标记
        # 重复任务的子任务落地之后，把本体 occurrence 里的 Focusmate 信息也换
        # 成真 id（materialize_repeats 现在是乐观队列，不再当场等复核）。
        if task.get("repeat_parent") and task.get("repeat_date"):
            try:
                current = self.task_store.get(task["id"])
                self.task_store.record_occurrence(
                    task["repeat_parent"], task["repeat_date"],
                    child_id=task["id"],
                    session=current.get("session") or session,
                    focusmate=current.get("focusmate") or None)
            except (ValueError, TypeError):
                pass
        self.selected_task_id = task["id"]
        self.rebuild_rows()
        remote_had = payload.get("existingCount")
        total = (remote_had if isinstance(remote_had, int) else 0) + 1
        extra = ("（这场现在共 %d 个任务，表格里会一起列出来）" % total) if total > 1 else ""
        via = payload.get("via")
        self.say("✅ 任务 %s 已挂上；Focusmate task id=%s%s%s"
                 % (self.task_label(task), fm_id,
                    "（via %s）" % via if via else "", extra), "ok")
        self.invalidate()
        return task["id"]

    def _apply_task_detach(self, payload, args):
        """删掉了：本地断绑定（重放时同样走这里）。"""
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        current = task.get("session") or {}
        if current and (current.get("date") != args.get("date")
                        or current.get("start") != args.get("start")):
            # 排进队列之后它又挂到别处去了：这次「下架」不该动新位置
            self.jobs._log_line("任务 %s 已经挂到 %s %s，这次下架跳过"
                                % (self.task_label(task), current.get("date"),
                                   current.get("start")), "warn")
            return task_id
        try:
            task = self.task_store.unassign(task_id, reason=args.get("reason") or "net-detach")
        except (ValueError, TypeError):
            return None
        self.rebuild_rows()
        self.say("⏏ 任务 %s 已从 %s %s 下架"
                 % (self.task_label(task), (args.get("date") or "?"),
                    args.get("start") or "?"), "ok")
        self.invalidate()
        return task["id"]

    def _apply_task_complete(self, payload, args):
        """勾上了：本地记 completed_at。"""
        payload = payload or {}
        try:
            task = self.task_store.get(args.get("task_id"))
        except (ValueError, TypeError):
            return None
        self.task_store.attach_focusmate(task["id"], {
            "completed_at": payload.get("completed_at") or ts._now()})
        self.task_store.settle_focusmate(task["id"], FM_PENDING)
        self.invalidate()
        return task["id"]

    def _apply_session_cancel(self, payload, args):
        """取消了：那一行从表格里消失（乐观时已经先拿掉；重放时在这儿拿掉）。"""
        self._forget_api_row(args.get("meeting_id"))
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_session_mute(self, payload, args):
        self._patch_api_row(args.get("meeting_id"), quiet=bool((payload or {})
                                                              .get("quiet")))
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_session_title(self, payload, args):
        self._patch_api_row(args.get("meeting_id"),
                            title=(payload or {}).get("title") or "")
        self.rebuild_rows()
        self.invalidate()
        return True

    def _apply_omnifocus_import(self, payload, args):
        """第 1 步回来了：去重 → （可能要问重复设置）→ 建本地任务 → 套后缀。

        「套后缀」就是 ``task import -a 3`` 里的 ``-a 3`` —— 它会**再排一步**
        （``[focusmate] 挂任务``），所以一次 import 在面板上就是两步。
        """
        payload = payload or {}
        items = payload.get("items") or []
        repeats = {int(k): v for k, v in (payload.get("repeats") or {}).items()}
        warnings = payload.get("warnings") or []
        suffix = dict(args.get("suffix") or {})
        skipped = []
        # 项目：要么「时长不齐 → 整批拒绝」，要么写进项目库（不进任务库）。
        refused = payload.get("refused") or []
        if refused:
            self.had_error = True
            for payload_item in refused:
                self.say("✗ " + (pj.format_missing(payload_item)
                                 or pj.format_depth_errors(payload_item)
                                 or payload_item.get("reason")
                                 or "没法导入这个项目。"), "error")
            if any(payload_item.get("depth_errors") for payload_item in refused):
                self.say("🚫 这次导入被拒：一条任务、一个项目都没建。先去 OmniFocus "
                         "把三层任务改成最多父-子两层，再 ⌘C 重新 `task import`。",
                         "error")
            else:
                self.say("🚫 这次导入被拒：一条任务、一个项目都没建。先去 OmniFocus "
                         "给上面那几条设「持续时间」（预计时长），再 ⌘C 重新 "
                         "`task import`。", "error")
            self._last_import_result = None
            self.jobs.end()
            return None
        imported = self._apply_project_imports(
            payload.get("projects") or [],
            suffix=suffix,
            op_type=args.get("op_type"),
            advance_pro_id=args.get("advance_pro_id"),
        )
        # 「已经导入过」只在**批量**里跳过：一次选十条里混着上个月的旧条目才是误伤；
        # 单条导入是「我就要这条」的明确动作（再来一条同名关联是合法用法）。
        if len(items) > 1:
            items, skipped = self._task_import_skip_known(items)
            if skipped and items:
                self.say("📥 跳过已经导入过的 %d 条：%s"
                         % (len(skipped), "、".join("%s→任务 %s" % (pk, tid)
                                                   for pk, tid in skipped)), "info")
        if not items:
            if imported:
                # 只有项目、没有普通任务：项目已经进库了，没有「空导入」这回事。
                self.jobs.end()
                return imported[0]
            detail = "、".join("%s → 任务 %s" % (pk, tid) for pk, tid in skipped)
            self.say("📥 这一批都已经导入过了（%s）；没有新建任何任务。" % detail
                     if detail else "📥 这一批没有可导入的条目。", "warn")
            self.jobs.end()
            return None
        if repeats:
            self._last_import_result = IMPORT_PENDING
            self.pending_import = {"items": items, "repeats": repeats,
                                   "warnings": [], "suffix": suffix}
            titles = "、".join((items[i] or {}).get("title") or (items[i] or {}).get("id")
                               or "?" for i in sorted(repeats))
            self.say("📥 发现 OmniFocus 重复任务：%s —— 先确认要不要复制重复设置，"
                     "还没建本地任务。" % titles, "cmd")
            self.invalidate()
            self.jobs.end()          # 等用户答 y/n，这一次操作到此为止
            return IMPORT_PENDING
        task = self._finish_task_import(items, copy_repeat=False, repeats={},
                                        warnings=warnings)
        if suffix:
            try:
                task = self._apply_import_suffix(task, suffix) or task
            except Exception as e:                  # noqa: BLE001 - 建好了但没套上后缀
                self.had_error = True
                self.say("⚠ 任务导入建好了，但后缀（%s）没套上：%s"
                         % (self._suffix_summary(suffix).strip() or "?", e), "error")
        # 同步模式（--exec / 测试）里 cmd_task_import 要把「建出来的任务」还给
        # 调用方；异步模式没人等这个返回值（后缀已经在上面套完了）。
        self._last_import_result = task
        return task

    def _project_local_task(self, project_id, project_task_pk):
        """按 ``(项目序号, 项目任务 pk)`` 找本地镜像任务；没有返回 None。"""
        want_pk = str(project_task_pk or "").strip()
        if not want_pk:
            return None
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if pk == want_pk and str(pid) == str(project_id):
                return task
        return None

    def sync_project_tasks(self, project, quiet=True):
        """把项目库里的**叶子任务**镜像进本地任务库（幂等）。

        这样任务视图的搜索 / ``group project`` / 排序里能直接找到项目任务，而
        契约视图的任务面板不会因此被几十条备选任务淹掉：这些镜像任务带
        ``source.kind == "project"``、``fields.project_task``、且没有 session，
        会被 :meth:`task_panel_tasks` 当成「项目备选」跳过；等 ``plan`` 真正
        唤起它们时，就复用同一条本地任务并挂上 session。
        """
        project = project or {}
        try:
            pid = int(project.get("id"))
        except (TypeError, ValueError):
            return []
        title = project.get("title") or ""
        tasks = project.get("tasks") or []
        kids = pj.child_map(tasks)
        statuses = pj.effective_task_statuses(tasks)
        made = []
        for item in tasks:
            pk = str(item.get("pk") or "").strip()
            if not pk or kids.get(pk):
                continue
            status = statuses.get(pk, pj.STATUS_ACTIVE)
            local = self._project_local_task(pid, pk)
            if status == pj.STATUS_ACTIVE:
                task, created = self.task_store.ensure_project_task(pid, title, item)
                made.append((task, created))
                continue
            # OmniFocus 那边已经完成/放弃了：本地还开着的镜像跟着收口，
            # 但已经挂过 session 的任务不动（那是当天的真实账）。
            if local is not None and ts.TaskStore.is_open(local) and not local.get("session"):
                target = (ts.STATUS_COMPLETED if status == pj.STATUS_COMPLETED
                          else ts.STATUS_DROPPED)
                self.task_store.set_status(local["id"], target, reason="project-sync")
        if made and not quiet:
            created = sum(1 for _task, is_new in made if is_new)
            self.say("📥 项目「%s」任务已入库：%d 条叶子任务%s（任务视图可 "
                     "`group project` / `search 项目名` 找到）"
                     % (title, len(made),
                        "（新建 %d）" % created if created else ""), "ok")
        return made

    def sync_all_project_tasks(self, quiet=True):
        """所有 active 项目都镜像一遍（TUI 启动时补历史项目）。"""
        made = []
        try:
            projects = self.projects.active()
        except Exception:                             # noqa: BLE001 - 坏项目库不挡启动
            return []
        for project in projects:
            made.extend(self.sync_project_tasks(project, quiet=quiet))
        return made

    def _apply_project_imports(self, payloads, suffix=None, op_type=None,
                               advance_pro_id=None):
        """把项目 payload 写进**项目库**，并把叶子任务镜像进任务库。

        项目库仍是「母体」；任务库里的镜像只用于任务视图的搜索 / 分组 / 排序，
        没有 session 时不会进契约视图的任务面板。等 `plan` 给项目分了 count、
        真正唤起任务时，复用同一条镜像并挂上 session。
        第二次导入同一个项目 = 刷新它的任务清单。
        """
        if advance_pro_id is None and self.pending_advance is not None:
            advance_pro_id = self.pending_advance.get("pro_id")
            if not op_type:
                op_type = self.pending_advance.get("required_op_type")
        norm_type = pj.normalize_op_type(op_type)
        made = []
        for payload in payloads or []:
            if not payload:
                continue
            item_payload = dict(payload)
            if norm_type:
                item_payload["op_type"] = norm_type
            if advance_pro_id is not None:
                item_payload["pro_id"] = advance_pro_id
            try:
                project, created = self.projects.upsert(item_payload)
            except ValueError as e:
                self.had_error = True
                self.say("⚠ 项目没写进库：%s" % e, "error")
                continue
            made.append(project)
            num = self.projects.number_of(project["id"]) or project["id"]
            tasks = project.get("tasks") or []
            open_tasks = [t for t in tasks
                          if not t.get("completed") and not t.get("dropped")]
            done_tasks = [t for t in tasks if t.get("completed")]
            dropped_tasks = [t for t in tasks if t.get("dropped")]
            stat = pj.stats(project, self.task_store)
            head = "📁 导入项目" if created else "🔄 刷新项目"
            type_label = pj.normalize_op_type(project.get("op_type")) or "未设类型"
            pro = self.projects.pro_for_op(project)
            pro_label = (" · 关联项目「%s」(%s)" % (pro.get("title"), pro.get("stage"))
                         if pro else "")
            self.say("%s #%d「%s」[%s%s] → OmniFocus 项目 %s"
                     % (head, num, project.get("title"), type_label, pro_label,
                        (project.get("omnifocus") or {}).get("id") or "?"), "ok")
            self.say("   %d 条任务（%d 条未完成、%d 条已完成、%d 条已放弃）"
                     " · 共 %d count = %d 分钟 · 剩余 %d count（进度 %s）"
                     % (len(tasks), len(open_tasks), len(done_tasks),
                        len(dropped_tasks), stat["total"],
                        stat["total"] * pp.MINUTES_PER_COUNT,
                        stat["remaining"], stat["progress_text"]), "info")
            week = pj.weekly_yield(tasks)
            if week.get("count"):
                self.say("   " + pj.week_summary(week), "info")
            if week.get("missing"):
                self.say("   ⚠ 本周任务里还有 %d 条没设持续时间（导入时本应被挡下，"
                         "`project %d refresh` 再看一次）。"
                         % (len(week["missing"]), num), "warn")
            self.sync_project_tasks(project, quiet=False)
            self.say("   任务清单已镜像进任务库（任务视图可按项目分组/搜索）；"
                     "没挂 session 前先躺着、不占契约面板，`plan` 分给这个项目 count "
                     "时才会挑出来挂 session。", "info")
        if made and (suffix or {}):
            # 项目自己不挂 session（母体永远是母体），-a / -f 这些后缀对它没有意义。
            self.say("· 项目不吃后缀（%s）：它自己不占 session，等 `plan` 唤起任务时再"
                     "挂。" % self._suffix_summary(suffix).strip(), "info")
        if made and advance_pro_id is not None:
            self._finish_pro_advance(advance_pro_id, made[-1]["id"])
        elif made and any(pj.normalize_op_type(p.get("op_type")) is None for p in made):
            self.check_untyped_ops(announce_import=True)
        if made:
            self.invalidate()
        return made

    def _apply_omnifocus_list(self, payload, args):
        """读到了：把行塞进 client 的缓存，调用方自己取（import / 对账用）。"""
        rows = (payload or {}).get("rows") or []
        try:
            self.omnifocus.remember_rows(rows)
        except Exception:                       # noqa: BLE001 - 老版本/替身没有这个方法
            pass
        self.invalidate()
        return rows

    def _apply_omnifocus_complete(self, payload, args):
        payload = payload or {}
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        advanced, moved = self._omni_repeat_advance(task, payload.get("pk") or "",
                                                   payload.get("res") or {})
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "completed")
        except ValueError:
            pass
        self.say("✅ OmniFocus %s 完成写回%s"
                 % (str(payload.get("pk") or "")[:8],
                    self._omni_repeat_note(advanced, (payload.get("res") or {})
                                          .get("repeat_clone_id"), moved)), "ok")
        self.invalidate()
        return task["id"]

    def _apply_omnifocus_drop(self, payload, args):
        payload = payload or {}
        task_id = args.get("task_id")
        try:
            task = self.task_store.get(task_id)
        except (ValueError, TypeError):
            return None
        advanced, moved = self._omni_repeat_advance(task, payload.get("pk") or "",
                                                   payload.get("res") or {})
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "dropped")
        except ValueError:
            pass
        self.say("✅ OmniFocus %s 已放弃%s"
                 % (str(payload.get("pk") or "")[:8],
                    self._omni_repeat_note(advanced, (payload.get("res") or {})
                                          .get("repeat_clone_id"), moved)), "ok")
        self.invalidate()
        return task["id"]

    def _apply_omnifocus_writeback(self, payload, args):
        payload = payload or {}
        for item in payload.get("done") or []:
            try:
                self.task_store.set_omnifocus_writeback(int(item["task_id"]),
                                                        "completed")
            except (ValueError, TypeError):
                pass
        count = len(payload.get("done") or [])
        failed = payload.get("failed") or []
        if count:
            self.say("✅ OmniFocus 写回 %d 条完成%s"
                     % (count, ("；%d 条失败" % len(failed)) if failed else ""),
                     "ok" if not failed else "warn")
        self.invalidate()
        return count

    def _apply_keep_session(self, payload, args):
        payload = payload or {}
        out = (payload.get("out") or "").strip()
        for line in out.splitlines()[-6:]:
            if line.strip():
                self.say(line.strip()[:160], "info")
        self.forget_caches()
        self.invalidate()
        return payload.get("rc")

    def _apply_calendar_sync(self, payload, args):
        payload = payload or {}
        argv = payload.get("argv") or []
        verb = str(argv[0]).lower() if argv else "download"
        out = (payload.get("out") or "").strip()
        for line in out.splitlines()[-6:]:
            if line.strip():
                self.say(line.strip()[:160], "info")
        if verb == "populate":
            self.say("✅ data.csv 已同步（下载层 → 时段 layout → 精确日期契约）。", "ok")
        else:
            self.say("✅ 日历已下载到中间层（工作/空闲）。", "ok")
        self.reload_entries()
        self.refresh(quiet=True)
        self.invalidate()
        return payload.get("rc")

    def _net_action_failed(self, action, error):
        """没有专属失败钩子的失败（重放时的就是这种）：说话 + 记账就够了。"""
        self.had_error = True
        self.say("❌ %s 失败：%s（已记进失败账，re-action 可以重放）"
                 % (action.label, error), "error")
        self.invalidate()

    # -- 小工具：乐观写操作的「本地那半件事」和它的反面 ----------------

    def _patch_api_row(self, meeting_id, **fields):
        """改表格里那一行的本地字段（不动 data.csv，等 refresh 覆盖）。"""
        if not meeting_id:
            return None
        for row in self.api_rows:
            if row.get("id") == meeting_id:
                row.update(fields)
                return row
        return None

    def _forget_api_row(self, meeting_id):
        """把一行从本地表格里拿掉（取消的乐观预测）。返回被拿掉的那行和位置。"""
        if not meeting_id:
            return None
        for index, row in enumerate(self.api_rows):
            if row.get("id") == meeting_id:
                return index, self.api_rows.pop(index)
        return None

    def _restore_api_row(self, before_row, index=None):
        """把一行（或它原来的位置）放回本地表格 —— 乐观行操作的失败回滚。"""
        if not before_row:
            return None
        meeting_id = before_row.get("id")
        existing = self._patch_api_row(meeting_id, **dict(before_row)) if meeting_id else None
        if existing is None:
            if index is None or index > len(self.api_rows):
                self.api_rows.append(dict(before_row))
            else:
                self.api_rows.insert(index, dict(before_row))
        self.rebuild_rows()
        self.invalidate()
        return before_row

    def _row_op_failed(self, what, before_row, error, index=None):
        """行级乐观操作失败：把那一行放回原样，说清楚，记账照旧。"""
        self._restore_api_row(before_row, index=index)
        self.had_error = True
        self.say("↩ 回滚：%s 没成功（%s）；表格已经退回操作前。" % (what, error), "warn")

    @staticmethod
    def _fm_pending(task):
        """这条任务的 Focusmate 挂载是不是「乐观预测、还没落地」。"""
        return ((task or {}).get("focusmate") or {}).get(FM_PENDING) or None

    def _revert_task(self, task_id, snapshot, why):
        """把一条任务整体放回操作前的快照，并且说实话。"""
        try:
            self.task_store.restore(task_id, snapshot)
        except (ValueError, TypeError):
            pass
        self.rebuild_rows()
        self.invalidate()
        self.had_error = True
        self.say("↩ 回滚：%s（本地已经退回操作前）" % why, "warn")

    # -- 新鲜度：这份数据是什么时候来的 --------------------------------

    def _age_text(self, stamp, limit_ok=True):
        """epoch → 「12 秒前」/「6 分钟前」/「还没拉过」。"""
        if not stamp:
            return "还没拉过"
        seconds = max(0, int(time.time() - stamp))
        if seconds < 90:
            return "%d 秒前" % seconds
        if seconds < 5400:
            return "%d 分钟前" % int(seconds / 60)
        return "%d 小时前" % int(seconds / 3600)

    def freshness_rows(self):
        """状态框里的「数据是什么时候来的」。

        用户要求：**最近的就不吵**（灰的），**不是最近的红色 + 加粗** ——
        「这份 session 列表是 8 分钟前的」必须一眼看得出来，不然你会拿旧账
        去做「现在几点了」的决定。
        """
        now = time.time()
        pieces = []
        stale = []
        for label, key, limit in (("session", "session", FRESH_SESSION),
                                  ("task", "task", FRESH_TASK)):
            stamp = self.read_at.get(key) or 0
            text = self._age_text(stamp)
            pieces.append("%s %s" % (label, text))
            if not stamp or (now - stamp) > limit:
                stale.append(label)
        if "session" not in self.read_at and self.rows_source == "snapshot":
            return [("数据：session 列表来自 state.json 快照（不是真数据）→ refresh",
                     "bold red")]
        if stale:
            return [("数据（⚠ %s 不是最近的）：%s" % ("、".join(stale), " · ".join(pieces)),
                     "bold red")]
        return [("数据：%s" % " · ".join(pieces), "dim")]

    def frame_signature(self):
        """画帧缓存要看的那些「便宜的事实」（贵的东西留在 key 之外）。

        只放**不求值就便宜**的东西：长度、身份、指纹、警告签名。任何一项变了
        就重排版。`messages` 取最后一条（带秒级时间戳），所以 Recent 不会滞后。
        """
        msgs = self.messages or ()
        pending_import = self.pending_import
        pending_sig = None
        if pending_import:
            pending_sig = (
                tuple(str((item or {}).get("id") or (item or {}).get("title") or "")
                      for item in pending_import.get("items") or ()),
                tuple(sorted((pending_import.get("repeats") or {}).keys())),
            )
        return (
            len(msgs), msgs[-1] if msgs else (),
            len(self.rows), id(self.rows),
            self.rows_note or "", self.rows_source or "",
            self.bridge_note or "", bool(self.had_error),
            self.csv_fp, len(self.entries),
            self.review_signature(), pending_sig,
            self.sel_date.isoformat(), self.day_offset, self.session_ui_mode,
            tuple(sorted(self.effective_hidden_columns())),
            tuple(sorted(self.effective_task_hidden_columns(self.task_store.list()))),
            self.task_view_page, self.task_sort_key, self.task_sort_desc,
            self.task_group_by, self.task_search_query,
            isinstance(self.view, ChoreView), self.chore_index,
            self.chore_last_note,
            id(getattr(self, "stat", None)),
            id(getattr(self, "help_overlay", None)),
            (id(self.planning), getattr(self.planning, "stage", None),
             tuple((a.get("project_id"), a.get("count"))
                   for a in (getattr(self.planning, "allocations", None) or ())),
             getattr(self.planning, "closed", None)) if self.planning else None,
            tuple(self.pending_op_type or ()),
            tuple(sorted((self.pending_advance or {}).items())) if self.pending_advance else None,
            # 队列一动（进度条、日志、失败账）画面就得重排版
            self.jobs.signature(),
        )

    # -- 表头折叠 -------------------------------------------------------

    def ui_prefs_path(self):
        """列折叠偏好落在设置文件同目录（测试跟着 TI_SETTINGS_FILE 隔离）。"""
        return os.path.join(os.path.dirname(os.path.abspath(cfg.settings_path())),
                            "warden-ui.json")

    def _load_ui_prefs(self):
        try:
            with open(self.ui_prefs_path(), encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (OSError, ValueError, TypeError):
            return
        for section, target in (("hidden", self.permanent_hidden_columns),
                                ("visible", self.permanent_visible_columns)):
            data = raw.get(section) or {}
            if not isinstance(data, dict):
                continue
            for mode in UI_PREF_MODES:
                names = data.get(mode) or []
                if not isinstance(names, list):
                    continue
                for name in names:
                    if mode == TASK_UI_MODE:
                        col = normalize_task_column(name) or str(name).strip()
                    else:
                        col = normalize_session_column(name)
                    if col:
                        target[mode].add(col)

    def _save_ui_prefs(self):
        path = self.ui_prefs_path()
        data = {
            "hidden": {m: sorted(self.permanent_hidden_columns[m])
                       for m in UI_PREF_MODES},
            "visible": {m: sorted(self.permanent_visible_columns[m])
                        for m in UI_PREF_MODES},
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except OSError as e:
            self.say("列折叠偏好没存下来：%s" % e, "warn")

    def auto_hidden_columns(self, mode=None):
        mode = mode or self.session_ui_mode
        if mode == "working":
            return set(WORKING_AUTO_HIDDEN_COLUMNS)
        return set()

    def effective_hidden_columns(self, mode=None):
        """当前口径实际不展示的列（自动 + 永久 + 本次 close - 手动 open）。"""
        mode = mode or self.session_ui_mode
        hidden = set(self.auto_hidden_columns(mode))
        hidden |= self.permanent_hidden_columns.get(mode, set())
        hidden |= self.session_hidden_columns.get(mode, set())
        hidden -= self.permanent_visible_columns.get(mode, set())
        hidden -= self.session_visible_columns.get(mode, set())
        return {col for col in hidden
                if any(col == header for header, _kw in SESSION_TABLE_COLUMNS)}

    def _set_column_open(self, col, permanent=False, mode=None):
        mode = mode or self.session_ui_mode
        self.session_hidden_columns[mode].discard(col)
        self.session_visible_columns[mode].add(col)
        if permanent:
            self.permanent_hidden_columns[mode].discard(col)
            self.permanent_visible_columns[mode].add(col)
            self._save_ui_prefs()

    def _set_column_closed(self, col, permanent=False, mode=None):
        mode = mode or self.session_ui_mode
        self.session_visible_columns[mode].discard(col)
        self.session_hidden_columns[mode].add(col)
        if permanent:
            self.permanent_visible_columns[mode].discard(col)
            self.permanent_hidden_columns[mode].add(col)
            self._save_ui_prefs()

    def effective_task_hidden_columns(self, tasks=None):
        """任务表当前不展示的任务表头（默认收敛 + 永久 + 本次 close - 手动 open）。

        ``tasks=None`` 时不读任务库，只返回偏好里记的列名（frame signature 用）；
        渲染和 close 校验会传入当前任务，于是还能过滤掉已经不存在的动态字段。
        """
        headers = set(task_table_headers(tasks)) if tasks is not None else None
        hidden = set(TASK_AUTO_HIDDEN_COLUMNS)
        if tasks is not None:
            # 自定义字段默认也收进「备注」：它们不是每张任务都有的固定列，
            # 自动铺成列会挤掉 title/session/截止，宽屏上反而更早出现省略号。
            # 需要单独看时仍可 `open <字段>`。
            fixed = {header for header, _kw in TASK_TABLE_COLUMNS}
            hidden |= {key for key in task_dynamic_keys(tasks) if key not in fixed}
        hidden |= self.permanent_hidden_columns.get(TASK_UI_MODE, set())
        hidden |= self.session_hidden_columns.get(TASK_UI_MODE, set())
        hidden -= self.permanent_visible_columns.get(TASK_UI_MODE, set())
        hidden -= self.session_visible_columns.get(TASK_UI_MODE, set())
        return {col for col in hidden if headers is None or col in headers}

    def task_flags_text(self, task):
        """任务表 boolean flags 专列（``标记`` 列）：沿用契约界面记号并补齐。

        🔁 = 重复任务子任务（🔁* = 重复任务模板本体）
        🔗 = 关联 OmniFocus
        🎯 = 关联 Focusmate task（⏳ = 异步挂载在途）
        ⏱ = 挂了 formatter
        ⚡ = 挂了 trigger
        🔒 = 独占场次（exclusive）
        """
        marks = ""
        if ts.TaskStore.is_repeat_template(task):
            marks += "🔁*"
        elif (task or {}).get("repeat_parent"):
            marks += "🔁"
        if self.task_project_name(task):
            marks += "📁"
        if ts.TaskStore.omnifocus_id(task):
            marks += "🔗"
        if self._fm_pending(task):
            marks += "⏳"
        elif ((task or {}).get("focusmate") or {}).get("task_id"):
            marks += "🎯"
        if (task or {}).get("formatter"):
            marks += "⏱"
        if (task or {}).get("trigger"):
            marks += "⚡"
        if th.is_exclusive(task):
            marks += "🔒"
        return marks or "·"

    def task_project_name(self, task):
        """任务所属项目名称（优先 ``fields.project``，其次项目库匹配）。"""
        fields = (task or {}).get("fields") or {}
        proj = str(fields.get(pj.PROJECT_FIELD) or fields.get("项目") or "").strip()
        if proj:
            return proj
        source = (task or {}).get("source") or {}
        if str(source.get("kind") or "") == "project":
            src_title = str(source.get("project_title") or "").strip()
            if src_title:
                return src_title
        try:
            projects = self.projects.list()
        except Exception:                             # noqa: BLE001
            projects = []
        pid = fields.get(pj.PROJECT_ID_FIELD)
        if pid is not None:
            for p in projects:
                if str(p.get("id")) == str(pid):
                    return str(p.get("title") or "").strip()
        pk = ts.TaskStore.omnifocus_id(task)
        if pk:
            for p in projects:
                for pt in (p.get("tasks") or ()):
                    if str(pt.get("pk") or "") == str(pk):
                        return str(p.get("title") or "").strip()
        return ""

    def task_remark_text(self, task):
        """任务表「备注」列：一行 ``k=v`` 紧凑汇总被折叠字段与备注。"""
        fields = (task or {}).get("fields") or {}
        parts = []
        dur = ts.task_minutes(task)
        if dur is not None:
            parts.append("dur=%dm" % dur)
        cap = ts.task_count(task)
        inv = ts.task_count_invested(task)
        delta = self.task_count_delta(task)
        if cap is not None or inv > 0:
            if cap is not None:
                if delta > 0:
                    parts.append("cnt=%d+%d/%d" % (inv, delta, cap))
                elif inv > 0:
                    parts.append("cnt=%d/%d" % (inv, cap))
                else:
                    parts.append("cnt=%d" % cap)
            else:
                parts.append("cnt=%d" % inv)
        if th.is_exclusive(task):
            parts.append("excl=1")
        tags = (task or {}).get("tags") or []
        if tags:
            parts.append("tag=" + "/".join(str(t) for t in tags))
        rep = (task or {}).get("repeat") or {}
        if rep:
            parts.append("rep=🔁 " + ts.repeat_summary(rep))
        fmt = (task or {}).get("formatter") or {}
        if fmt.get("name"):
            f_txt = str(fmt.get("name"))
            if fmt.get("reservation"):
                f_txt += "@" + str(fmt["reservation"].get("start") or "?")
            parts.append("fmt=" + f_txt)
        trig = (task or {}).get("trigger") or {}
        if trig.get("name"):
            parts.append("trig=" + str(trig.get("name")))
        note = self.task_store.notes(task)
        if note:
            parts.append("note=" + self.task_store._one_line(note))
        skip_keys = (set(ts.DURATION_KEYS) | set(ts.NOTES_KEYS) | set(th.EXCLUSIVE_KEYS)
                     | {"title", "name", pj.PROJECT_FIELD, "项目",
                        pj.PROJECT_ID_FIELD, pj.PROJECT_TASK_FIELD,
                        ts.COUNT_KEY, ts.COUNT_INVESTED_KEY, "due", "due_source"})
        for k, v in fields.items():
            if k in skip_keys:
                continue
            val = self.task_store._one_line(v)
            if val:
                parts.append("%s=%s" % (k, val))
        # 用紧凑分隔符（不带空格）：备注列是固定宽度里信息密度最高的一格，
        # 三个逗号省下的 3 列正好能让 `fmt=enlist` 这类尾项完整显示，而不是
        # 在宽屏上仍被 Rich 裁掉最后一个字符。
        return ",".join(parts)

    def task_group_label(self, task, group_by):
        """分组列显示的标签值。"""
        col = normalize_task_column(group_by, [task]) or str(group_by or "").strip()
        if col == "project":
            return self.task_project_name(task) or "（无项目）"
        if col == "状态":
            return "%s %s" % (self.task_mark(task) or "·", self.task_store.status_of(task))
        if col == "session":
            sess = (task or {}).get("session") or {}
            if not sess:
                return "（未排场次）"
            raw = "%s %s" % (sess.get("date") or "", sess.get("start") or "")
            return format_compact_timestamp(raw.strip(), self.sel_date)
        if col == "截止":
            due = self.omni_due_of(task)
            return format_compact_timestamp(due, self.sel_date) if due else "（无截止）"
        if col == "标签":
            tags = (task or {}).get("tags") or []
            return "·".join(tags) if tags else "（无标签）"
        if col == "标记":
            return self.task_flags_text(task)
        if col == "duration":
            dur = ts.task_minutes(task)
            return ("%dm" % dur) if dur is not None else "（无时长）"
        if col == "count":
            cap = ts.task_count(task)
            return ("cnt=%d" % cap) if cap is not None else "（无 count）"
        fields = (task or {}).get("fields") or {}
        val = self.task_store._one_line(fields.get(col) or fields.get(group_by))
        return val or ("（无 %s）" % col)

    def _task_sort_tuple(self, task, key):
        """返回可比较的排序键元组 ``(是否空值, 排序值, id)``。"""
        col = normalize_task_column(key, [task]) or str(key or "").strip()
        tid = int((task or {}).get("id") or 0)
        if col == "id":
            return (0, tid, tid)
        if col == "序号":
            no = self.task_no(task)
            return (0 if no else 1, no or 999999, tid)
        if col == "状态":
            rank = {ts.STATUS_ACTIVE: 0, ts.STATUS_COMPLETED: 1, ts.STATUS_DROPPED: 2}
            st = self.task_store.status_of(task)
            return (0, rank.get(st, 9), tid)
        if col == "title":
            fields = (task or {}).get("fields") or {}
            t = str(fields.get("title") or fields.get("name") or "").strip().lower()
            return (0 if t else 1, t, tid)
        if col == "project":
            p = self.task_project_name(task).lower()
            return (0 if p else 1, p, tid)
        if col == "session":
            sess = (task or {}).get("session") or {}
            s = ("%s %s" % (sess.get("date") or "", sess.get("start") or "")).strip()
            return (0 if s else 1, s, tid)
        if col == "截止":
            due = self.omni_due_of(task) or ""
            return (0 if due else 1, due, tid)
        if col == "duration":
            dur = ts.task_minutes(task)
            return (0 if dur is not None else 1, dur or 0, tid)
        if col == "count":
            cap = ts.task_count(task)
            return (0 if cap is not None else 1, cap or 0, tid)
        if col == "完成时间":
            fm = (task or {}).get("focusmate") or {}
            done = str(fm.get("completed_at") or (task or {}).get("status_at") or "")
            return (0 if done else 1, done, tid)
        fields = (task or {}).get("fields") or {}
        raw = self.task_store._one_line(fields.get(col) or fields.get(key)).lower()
        return (0 if raw else 1, raw, tid)

    def _task_matches_query(self, task, query):
        """任务是否命中搜索词（支持 ``field=val`` / ``field:val`` 或全文子串匹配）。"""
        tokens = [t.strip() for t in str(query or "").split() if t.strip()]
        if not tokens:
            return True
        fields = (task or {}).get("fields") or {}
        haystack_parts = [
            str((task or {}).get("id") or ""),
            str(self.task_no(task) or ""),
            str(fields.get("title") or fields.get("name") or ""),
            self.task_project_name(task),
            self.task_store.status_of(task),
            self.task_mark(task) or "",
            self.task_flags_text(task),
            self.task_remark_text(task),
            " ".join(str(t) for t in ((task or {}).get("tags") or [])),
            str(ts.TaskStore.omnifocus_id(task) or ""),
        ]
        for k, v in fields.items():
            haystack_parts.append("%s=%s" % (k, self.task_store._one_line(v)))
        haystack = " ".join(haystack_parts).lower()
        for tok in tokens:
            m = re.match(r"^([A-Za-z0-9_一-鿿]+)[:=](.+)$", tok)
            if m:
                k_raw, v_want = m.group(1), m.group(2).strip().lower()
                col = normalize_task_column(k_raw, [task]) or k_raw
                if col == "project":
                    got = self.task_project_name(task).lower()
                elif col == "状态":
                    got = (self.task_store.status_of(task) + " " + (self.task_mark(task) or "")).lower()
                elif col == "标签":
                    got = " ".join((task or {}).get("tags") or []).lower()
                elif col == "id":
                    got = str((task or {}).get("id") or "").lower()
                else:
                    got = self.task_store._one_line(fields.get(col) or fields.get(k_raw)).lower()
                if v_want not in got:
                    return False
            else:
                if tok.lower() not in haystack:
                    return False
        return True

    def filter_and_sort_tasks(self, tasks):
        """按当前搜索、排序和分组规则处理任务列表。"""
        out = list(tasks or [])
        query = getattr(self, "task_search_query", "")
        if query:
            out = [t for t in out if self._task_matches_query(t, query)]
        sort_key = getattr(self, "task_sort_key", None)
        sort_desc = bool(getattr(self, "task_sort_desc", False))
        if sort_key:
            non_empty = [t for t in out if self._task_sort_tuple(t, sort_key)[0] == 0]
            empty = [t for t in out if self._task_sort_tuple(t, sort_key)[0] != 0]
            non_empty.sort(key=lambda t: self._task_sort_tuple(t, sort_key)[1:],
                           reverse=sort_desc)
            out = non_empty + empty
        group_by = getattr(self, "task_group_by", None)
        if group_by:
            # 同一分组聚拢在一起，保留首次出现的分组顺序（或排完序后的顺序），无值的排在末尾
            groups = {}
            order = []
            tail_key = None
            for t in out:
                g = self.task_group_label(t, group_by)
                if g not in groups:
                    groups[g] = []
                    if g.startswith("（无"):
                        tail_key = g
                    else:
                        order.append(g)
                groups[g].append(t)
            if tail_key is not None:
                order.append(tail_key)
            grouped = []
            for g in order:
                grouped.extend(groups[g])
            out = grouped
        return out

    def task_filter_summary(self):
        """表尾 caption 里的当前排序 / 分组 / 搜索提示。"""
        bits = []
        if getattr(self, "task_search_query", ""):
            bits.append("搜索：%s" % self.task_search_query)
        if getattr(self, "task_group_by", None):
            bits.append("分组：grouping(%s)" % self.task_group_by)
        if getattr(self, "task_sort_key", None):
            bits.append("排序：%s %s" % (
                self.task_sort_key, "↓" if self.task_sort_desc else "↑"))
        return " · ".join(bits)

    # -- 选中的那一天

    @property
    def sel_date(self):
        return datetime.date.today() + datetime.timedelta(days=self.day_offset)

    def reload_entries(self):
        """重读 data.csv（每一次操作前都重读：人手可能在别处改了它）。"""
        self.entries, self.entry_warnings = sch.load_entries()
        self.csv_fp = sch.fingerprint()
        return self.entries

    def reload_periods(self):
        """重读日历下载层 + 时段 layout（T1/T2 和每段的 session 起点）。"""
        self.download = sp.load_download()
        self.layouts = sp.load_layouts()
        return self.download

    def periods_for_day(self, day=None):
        return sp.periods_for_date(self.download, day or self.sel_date)

    def period_first_start(self, period):
        """这个时段第一场 session 的 HH:MM；没排出来就是 None。"""
        try:
            sessions = sp.sessions_for_period(period, self.layouts)
        except ValueError:
            return None
        return sessions[0]["start"] if sessions else None

    def tasks_for_session(self, day, start, duration=None):
        """这一场挂着的**所有**任务：本地 assignment 优先，其次是「每周重复」落点。

        一场 session 可以挂多个任务（用户会在已有 task 的场次上再挂一个），
        所以表格里那一格要能显示多条，不能只认第一条。
        """
        tasks = self.task_store.tasks_for_session(day, start)
        # 重复任务的**本体**只当模板，不出现在契约表 task 列；表里只显示
        # 到那一天生成出来的子任务（子任务带 repeat_parent）。
        return [t for t in tasks if not ts.TaskStore.is_repeat_template(t)]

    def task_for_session(self, day, start, duration=None):
        """这一场挂的第一条任务（老接口，保留给只关心一条的调用方）。"""
        tasks = self.tasks_for_session(day, start, duration=duration)
        return tasks[0] if tasks else None

    def attach_period_info(self, row, day=None):
        """给一行表格补上「时段 ID / 时段 / 类型 / 名称 / task」。"""
        day = day or self.sel_date
        period = sp.period_for_session(self.periods_for_day(day), row.get("startLabel"))
        if period:
            row["period"] = dict(period)
            row["period_id"] = period.get("id")
            first = self.period_first_start(period)
            # 需求：同一时段的第一行显示 T1/时段/类型/名称，后续行留空。
            row["period_first"] = bool(first is None or first == row.get("startLabel"))
        else:
            row["period"] = None
            row["period_id"] = None
            row["period_first"] = False
        # 「这一行在不在静音时段里」——口径只有一个：`period mute T1 [--permanent]`
        # 写下的时间窗。quiet 是**结果**（Focusmate 那边设上了没），period_muted
        # 是**意图**；两个都摆出来，才知道是「本来就该静音」还是「该静音没设上」。
        window = mute_window_for(day, row.get("startLabel"), row.get("endLabel"))
        row["period_muted"] = bool(window)
        if window:
            row["mute_window"] = dict(window)
        tasks = self.tasks_for_session(day, row.get("startLabel"), row.get("durationMin"))
        if tasks:
            row["task"] = tasks[0]                # 兼容：只关心一条的地方仍然能用
            row["tasks"] = tasks
            row["task_repeat"] = any(t.get("repeat_parent") for t in tasks)
            row["task_text"] = self.task_cell_text(tasks, row.get("durationMin"))
        else:
            row["task"] = None
            row["tasks"] = []
            row["task_repeat"] = False
            row["task_text"] = ""
        return row

    @staticmethod
    def task_mark(task):
        """任务的状态标记：``·`` 进行中 / ``✓`` 完成 / ``✗`` 放弃 / ``⤼`` 转移走。

        用户要求 session 结束之后「那一场到底干成了什么」仍然看得见，所以
        完成/放弃的任务**不隐藏、不删**，只是前面多一个记号。

        进行中也必须打 ``·``：以前这里返回空串，表格里「没有记号」既可能是
        「还没做」也可能是「这一格压根没画状态」，用户看不出差别（真实反馈：
        「契约界面的 task 部分没有显示 task 状态」）。
        """
        status = ts.TaskStore.status_of(task)
        if status == ts.STATUS_COMPLETED:
            return "✓"
        if status == ts.STATUS_DROPPED:
            return "⤼" if ts.TRANSFER_TAG in (task.get("tags") or []) else "✗"
        return ts.STATUS_MARKS.get(ts.STATUS_ACTIVE) or "·"

    def task_cell_text(self, tasks, session_minutes=None):
        """契约表格 task 列的正文：``1: (1+2/5) 任务… | ✓3: 任务…``。

        标题优先取 title/name 字段（`focusmate_title`），没有才退回整条摘要 ——
        编号 + 一长串 ``1. … 2. …`` 在表格里没法看。前面的记号见 :meth:`task_mark`。

        ``(1+2/5)`` 是 count：已投入 + **这一场会涨的** / 最高。1 是这条任务之前
        已经用掉的 count，2 = 这一场时长 / 25（往上取整）—— 那是预报，真正记账
        发生在收尾（见 :meth:`begin_review`）。没设 count 的任务不显示这一格。
        """
        parts = []
        delta = ts.minutes_to_count(session_minutes) or 0
        for task in tasks or []:
            if ts.TaskStore.is_repeat_template(task):
                continue
            title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
            prefix = self.task_mark(task) + ts.TaskStore.origin_marks(task)
            # 本体的 formatter 只是模板，不能被画成「已经排过」（⏱ 属于子任务）。
            if task.get("formatter") and not ts.TaskStore.is_repeat_template(task):
                prefix += "⏱"
            no = self.task_no(task)
            count = self.task_count_label(task, delta)
            parts.append("%s%d: %s%s" % (prefix, no, count, title) if no else
                         "%s%s%s" % (prefix, count, title))
        return " | ".join(parts)

    @staticmethod
    def task_count_delta(task):
        """这条任务「完成这一场之后会自动涨多少 count」= 那一场时长 / 25。

        只对**还开着**、并且挂在某一场 session 上的任务有意义：收工/放弃的任务
        再预报一次涨幅就是在骗人。**已经记过账的那一场也不再预报**（否则收尾记完
        账之后表格里还写着 ``(3+2/5)``，看起来像下一帧还要再涨 2 个）。
        """
        if not task or not ts.TaskStore.is_open(task):
            return 0
        sess = task.get("session") or {}
        delta = ts.minutes_to_count(sess.get("duration")) or 0
        if not delta:
            return 0
        day = str(sess.get("date") or "").strip()
        if not day:
            return delta
        key = psv.session_key(day, sess.get("start"))
        if key in (task.get("count_credits") or {}):
            return 0
        return delta

    def task_count_label(self, task, delta=None):
        """任务格子里那截 ``(1+2/5) ``（带尾随空格）；没设 count 就是空串。"""
        if delta is None:
            delta = self.task_count_delta(task)
        text = ts.count_text(task, delta)
        return (text + " ") if text else ""

    def frame_width(self):
        """这一帧能占多少列（= 终端宽度，和 repl_kit 排「状态」框用的是同一个数）。

        抽成一个方法是为了测试能钉住它：「几列并排」「哪几行被裁」这种断言不能
        跟着「现在窗口开多大」乱跳。
        """
        width = getattr(self.console, "width", None)
        if getattr(getattr(self, "shell", None), "_in_frame_text", False):
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:                         # noqa: BLE001
                cols = width if isinstance(width, int) else 80
        else:
            try:
                cols = os.get_terminal_size().columns
            except Exception:                         # noqa: BLE001
                cols = width if isinstance(width, int) else 80
        if not isinstance(cols, int) or cols < 24:
            cols = width if isinstance(width, int) and width >= 24 else 80
        return cols

    def task_panel_tasks(self, day=None):
        """这一天任务面板里该出现哪些任务 —— 用户定的两条规矩。

        * **今天（以及以后）**：没挂 session 的、以及**挂在这一天**的没做完任务；
          再加上今天被处理过的。
        * **过去那天**：固化，只剩「那一天的 session 里被处理过的」。

        关键两条：
          * **重复任务本体只是模板**，不进【契约】的任务面板（也不进 task 列）；
            它只在「任务」视图里用 `id:N` 管理。
          * **已挂到别的日期的任务属于那一天，不属于今天**。明天 22:15 的任务
            今天不出现在面板里（切到 `day +1` 才出现）—— 否则今天的序号里混着
            明天的活，按面板序号敲命令就会打到错的账上。

        「被处理过」不挑结果：放弃 / 完成 / 补录 / 重议 / 推后 / 转移走 / 跳过
        都算一笔，痕迹记在 ``task["history"]``（``TaskStore.mark_processed``）。
        跳过也要记：那一天你问过它、你说先放着 —— 这是那一天发生过的事。

        还有一条兜底：**挂在这一天某场 session 上的任务一律进面板**。契约表格那一格
        写着它，面板上却没有它的序号 —— 那两处就对不上了（用户按面板序号敲命令，
        看到的却是「这一条没号」）。
        """
        day = day or self.sel_date
        iso = day.isoformat()
        is_today_or_later = day >= datetime.date.today()
        open_tasks = []
        touched = []
        for task in self.task_store.list():
            # 重复任务**本体**只是模板：它不出现在【契约】的任务面板 / task 列，
            # 只在「任务」视图里用 id:N 管理（选它没有意义：它不是一次活）。
            if ts.TaskStore.is_repeat_template(task):
                continue
            records = self.task_store.processed_on(task, iso)
            sess = task.get("session") or {}
            sess_day = sess.get("date") or ""
            attached_today = sess_day == iso
            # 项目库镜像进来的「备选叶子任务」（还没被 plan 唤起）：只给任务视图
            # 搜索/分组用，不占契约面板；一旦挂了 session 或当天处理过就走正常路径。
            if ts.TaskStore.is_project_backlog(task) and not records and not attached_today:
                continue
            if self.task_store.is_open(task):
                # 已经排到某一天的任务只在那一天的面板里出现（除非那天处理过它，
                # 那是历史账）。没挂 session 的才是「随时可干」的待办。
                if sess_day and not attached_today and not records:
                    continue
                if is_today_or_later or records or attached_today:
                    open_tasks.append(task)
            elif records or attached_today:
                touched.append(task)
        return open_tasks + touched

    def task_panel_order(self, day=None):
        """任务面板的**排序** —— 一张表，但同类的连号排在一起。

        需求两条：
          * 「每次 refresh 之后重排序，不然看着一大堆 32、15 的 task 混杂」
          * 「放弃的任务和待办任务还是放在一起，没有 grouping」

        所以排序有**两个维度**，先状态、后位置：

          1. **状态段**：进行中 → 完成 → 放弃（``TASK_PANEL_BANDS``）。
             待办归待办、收工归收工，不再 15(待办) / 33(放弃) / 38(放弃) 混着。
          2. **位置**（段内）：未挂 session 的最前，之后按场次时间聚成段，
             同一场的那几条挨在一起。
          3. 段内最后按任务 ID 升序：同一场上的几条是「一起干」的，编号顺序最好认，
             也最稳定（不会因为一次 refresh 就换位置）。

        这是**排序**不是**拆表**：面板还是那一张表，只是同类连号。
        """
        rank = {status: i for i, status in enumerate(TASK_PANEL_BANDS)}

        def key(task):
            band = rank.get(ts.TaskStore.status_of(task), len(rank))
            sess = task.get("session") or {}
            if not sess:
                return (band, 0, "", "", task.get("id") or 0)
            return (band, 1, sess.get("date") or "", sess.get("start") or "",
                    task.get("id") or 0)

        return sorted(self.task_panel_tasks(day), key=key)

    # -- 任务序号（面板上的第几条）--------------------------------------

    def task_number_index(self, day=None):
        """当前面板的「序号 → 任务」+ 反查表 ``{内部 id: 序号}``。

        需求：「任务重置他们的序号，根据当前任务的数量从 1 到最多」。所以序号**不是**
        内部 id，而是这条任务在面板上是**第几条**（1 起、连续、不跳号）。口径就是
        :meth:`task_panel_order`（先状态分段、段内按位置），所以「序号」和「你看到的
        那块面板」永远是同一份东西 —— 见 1 就是那一行里的第 1 条。

        每读一次库就重算一趟太浪费（画一帧要问好几次），所以按「库文件 mtime + 哪一天」
        存一份：库一写（增删、状态变了）、天一切，缓存自己就作废。
        """
        day = day or self.sel_date
        try:
            info = os.stat(self.task_store.path)
            stamp = (day.isoformat(), info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = (day.isoformat(), 0, 0)
        memo = getattr(self, "_number_memo", None)
        if memo is not None and memo[0] == stamp:
            return memo[1], memo[2]
        tasks = self.task_panel_order(day)
        by_no = {}
        no_of = {}
        for i, task in enumerate(tasks, 1):
            by_no[i] = task
            no_of[task.get("id")] = i
        self._number_memo = (stamp, by_no, no_of)
        return by_no, no_of

    def task_no(self, task, day=None):
        """这条任务在**当前面板**上是第几条（不在面板上 → None）。"""
        if not isinstance(task, dict):
            return None
        return self.task_number_index(day)[1].get(task.get("id"))

    def task_label(self, task, day=None):
        """给用户看的编号：面板上是 ``#3``；不在面板上就老实写内部 id。

        面板上**没有**这条（别的天的收工记录、归档里的）就别编个号给它 —— 编号一
        编，用户敲下去要么打偏要么报错，不如直接写 ``id 15``。
        """
        no = self.task_no(task, day)
        if no:
            return "#%d" % no
        ident = task.get("id") if isinstance(task, dict) else task
        return "id %s" % ident

    def resolve_task(self, token, day=None):
        """把用户敲的那个数解析成任务：**面板上的第几条**。

        两种写法：
          * ``3``     —— 面板上的第 3 条（正常用法）
          * ``id:15`` —— 明说「按内部 id」，给 `task list` / 归档里那些**不在面板上**
                         的任务留的口子（切不了天又必须处理它时用）

        面板上找不到、而这个数又正好是某条任务的内部 id 时也照收，并明说一句是按 id
        认的。两头都不沾边就报清楚：面板现在只有 #1…#几。
        """
        text = str(token).strip()
        if text.lower().startswith("id:"):
            return self.task_store.get(text[3:])
        try:
            want = int(text)
        except (TypeError, ValueError):
            raise ValueError("任务序号得是数字；你给的是 %r" % (token,)) from None
        day = day or self.sel_date
        by_no, _no_of = self.task_number_index(day)
        task = by_no.get(want)
        if task is not None:
            return task
        for candidate in self.task_store.list():
            if candidate.get("id") == want:
                self.say("（%d 超过面板的 #1–#%d，按内部 id 认：%s）"
                         % (want, len(by_no),
                            self.task_store.focusmate_title(candidate, 20)), "warn")
                return candidate
        raise ValueError("面板上现在只有 #1–#%d，没有 #%d（切天看别的日子，"
                         "或 `task list` 看全部）" % (len(by_no), want))

    def task_index_title(self, task):
        """任务面板里那一格的字：状态 + 来源记号 + count 进度 + 裁过的标题。"""
        title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
        prefix = self.task_mark(task) + ts.TaskStore.origin_marks(task)
        # 本体的 formatter 是给子任务的模板；⏱ 只画在真正排过的子任务上。
        if task.get("formatter") and not ts.TaskStore.is_repeat_template(task):
            prefix += "⏱"
        if th.is_exclusive(task) and not ts.TaskStore.is_repeat_template(task):
            prefix += "🔒"
        if self._fm_pending(task):
            # 乐观挂载还在后台跑：⏳ 就是「这条还没落地，别当它是事实」
            prefix += "⏳"
        # count 进度和 session 表格里的写法**一模一样**：(1+2/5)
        return prefix + self.task_count_label(task) + title

    def task_index_layout(self, tasks, width=None):
        """``(几对并排, 几行)``：屏幕够宽就摊成 3 对、4 对，装不下回来 2 对。

        需求：不该永远只有两列 —— 宽屏上白白浪费半屏，任务一多还得翻。
        一「对」的宽度 = 编号列 + 1 空格 + 标题列，两对之间 rich 再给 2 列 padding。

        填法是**一行一行**（左→右、上→下），不是一列一列。以前按列填，四列的
        宽屏上一行读过去是 15 / 39 / 42 / 28，看着就是「序号很乱」；按行填之后
        一行里的编号是递增的，同一段的连号也在同一行上并排。
        """
        if not tasks:
            return 1, 0
        width = width or self.frame_width()
        num_w = max(len(str(t.get("id"))) for t in tasks)
        title_w = max(repl_kit.display_width(self.task_index_title(t)) for t in tasks)
        pair_w = num_w + 1 + title_w
        cols = 1
        for n in range(min(TASK_INDEX_MAX_COLS, len(tasks)), 1, -1):
            if n * pair_w + (n - 1) * 2 <= width - 2:
                cols = n
                break
        rows = (len(tasks) + cols - 1) // cols
        cols = (len(tasks) + rows - 1) // rows        # 有余数时别留空列
        return cols, rows

    def render_task_index(self):
        """契约视图里那块「任务面板」：编号+任务 并排，列数看屏幕宽度。

        内容按天决定（见 :meth:`task_panel_tasks`）：今天 = 没挂 session 的 +
        挂今天 session 的 + 今天处理过的（排到明天的今天不出现），过去那天 =
        只有那天处理过的。**顺序**见 :meth:`task_panel_order`：先按状态
        分段（进行中 → 完成 → 放弃），段内未挂 session 最前、之后一场一段、段内按编号。
        还是这一张表，只是同类的连号挨在一起；格子是**一行一行**填的，所以一行
        从左读到右编号是递增的。配色见文件头那段「配色」。
        """
        tasks = self.task_panel_order()
        if not tasks:
            return None
        cols, rows = self.task_index_layout(tasks)
        t = Table(title="Task · 任务（· 进行中 / ✓ 完成 / ✗ 放弃 / ⤼ 转移走；"
                        "[bright_red]红[/] = 已挂 session）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        for _ in range(cols):
            t.add_column("number", justify="left", no_wrap=True)
            t.add_column("task", no_wrap=True, overflow="crop")
        selected = self.selected_task_id
        for i in range(rows):
            chunk = tasks[i * cols:(i + 1) * cols]      # 一行就是连续的几对
            cells = []
            for task in chunk + [None] * (cols - len(chunk)):
                cells.extend(self._task_index_cells(task, selected))
            t.add_row(*cells)
        # 表尾一行账：这张面板里到底几条收工了 —— 光看记号容易被标题淹掉。
        counts = {ts.STATUS_ACTIVE: 0, ts.STATUS_COMPLETED: 0, ts.STATUS_DROPPED: 0}
        for task in tasks:
            counts[ts.TaskStore.status_of(task)] = counts.get(
                ts.TaskStore.status_of(task), 0) + 1
        scope = ("今天 = 没挂 session 的 + 挂今天的 + 今天处理过的"
                 if self.sel_date >= datetime.date.today()
                 else "%s = 那天处理过的（过去就固化）" % self.sel_date.isoformat())
        t.caption = ("%d 条：%d 进行中 · %d 完成 · %d 放弃 "
                     "（收工的不删，只打记号 · %s）"
                     % (len(tasks), counts[ts.STATUS_ACTIVE],
                        counts[ts.STATUS_COMPLETED], counts[ts.STATUS_DROPPED], scope))
        return t

    def _task_index_cells(self, task, selected):
        """一行里的一对（编号, 标题）。没任务时给两个空格占位。"""
        if task is None:
            return [Text(""), Text("")]
        status = ts.TaskStore.status_of(task)
        style = STYLE_ASSIGNED if task.get("session") else STYLE_ACTIVE
        if status == ts.STATUS_DROPPED:
            style = STYLE_DROPPED
        if status == ts.STATUS_COMPLETED:
            style = STYLE_DONE
        if task.get("id") == selected:
            style += " reverse"
        return [Text(str(self.task_no(task) or "?"), style=style),
                Text(self.task_index_title(task), style=style)]

    def plan_rows_for(self, day):
        return sch.day_slots(self.entries, day)

    def pending_sins(self):
        """还没认罪的罪（daemon 在你手动取消时记下的）。有它 = 全局锁死。"""
        return ks.load_sins()["sins"]

    @property
    def authoritative(self):
        """表格里的数据是不是真数据（bridge 拉的）。快照/离线 = 不敢下"订没订"的结论。"""
        return self.rows_source == "bridge"

    # -- 输出

    def say(self, text, kind="info"):
        stamp = now_local().strftime("%H:%M:%S")
        self.messages.append((stamp, kind, text))
        del self.messages[:-MESSAGE_KEEP]
        self.invalidate()

    def _is_fullscreen_tty(self):
        """当前是否处在交互式全屏单页模式（Alt-Screen，不留历史）。"""
        return bool(not self.no_clear
                    and getattr(self.shell, "alt_screen", False)
                    and sys.stdin.isatty() and sys.stdout.isatty())

    def _inline_shell_output(self, *objects, **kwargs):
        """把框架直接 print 的输出搬进 Recent（全屏模式下不能写 stdout）。"""
        parts = []
        for obj in objects:
            if isinstance(obj, Text):
                text = obj.plain
            elif isinstance(obj, str):
                try:
                    text = Text.from_markup(obj).plain
                except Exception:                     # noqa: BLE001 - 不是 markup 就原样
                    text = obj
            else:
                try:
                    text = Text.from_markup(str(obj)).plain
                except Exception:                     # noqa: BLE001
                    text = str(obj)
            if text:
                parts.append(text)
        message = " ".join(parts).strip()
        if message:
            self.say(message, "warn")
        self.invalidate()

    def emit(self, renderable=None):
        """帧外打印：帮助面板 / 宣言闸门 / `>>>` 回显。

        在 `--exec` / 测试 / `--no-clear` 下直接往 console.print 打字；
        在全屏单页模式下不往 stdout 留滚动历史（面板改由帧内栏位承接）。
        """
        if self._is_fullscreen_tty():
            return
        if renderable is None:
            self.console.print()
        else:
            self.console.print(renderable)

    # -- 看板（框架把它画在提示行上方）---------------------------------

    def frame(self):
        """当前视图的整块看板。repl_kit 每帧调它一次，按单元格 diff 上屏。"""
        # 后台跑完的网络操作在这儿搬回主线程（成功落定 / 失败回滚）
        self.drain_net()
        # 每次画之前重读 data.csv：契约可能在别处被人手改了（这就是控制面）
        self.reload_entries()
        self.maybe_generate_repeat_occurrences()
        self.refresh_warnings()
        # 琐事屏是整屏内容：正常看板、Recent、状态框、视图栏都让位。
        if isinstance(self.view, ChoreView):
            return self.view.render()
        parts = [self.render_header()]
        body = self.view.render()
        if body is not None:
            parts.extend(body if isinstance(body, (list, tuple)) else [body])
        if self.messages:
            parts.append(self.render_messages())
        # 视图栏**不**放这儿：它是 repl_kit 的 footer（见 shell_kwargs）。
        # 看板是会被裁掉顶部的那一块，而「你在第几个视图」任何时候都得看得见。
        return Group(*parts)

    def paint(self, prompt_row=True):
        """画一整帧到 console：--status / --exec / 测试用。

        交互模式下不走这里，走 repl_kit 的 chrome（框架会把「状态」框和看板
        一起拼出来）。这里手动补上同一块框，保证 --status 看到的和交互时一样。
        """
        self.drain_net()
        try:
            self._maybe_sync_chore_mode()
        except Exception:                          # noqa: BLE001
            pass
        box = self.shell.status_box()
        if box is not None:
            self.console.print(box.renderable())
        self.console.print(self.frame())
        # 打断栏位（收尾/查账那句问话）：和交互模式同一位置 —— 看板下面、状态行上面
        block = self.shell.interrogate()
        if block is not None:
            self.console.print(block.renderable())
        # 底部状态栏（视图栏）：栏位下面、状态行上面 —— 和交互模式同一顺序
        self.console.print(self.render_view_bar())
        st = self.live_status()
        if st is not None:
            self.console.print(Text(st))

    def live_status(self):
        """当前视图的动态状态行（截到控制台宽度，纯文本）；没有就 None。"""
        st = self.view.status_line()
        if not st:
            return None
        return st[: max(10, self.console.width - 1)]

    def prompt_text(self):
        """欠着罪的时候提示符换成「宣言> 」—— 那时候只受理宣言。"""
        return "宣言> " if self.pending_sins() else None

    def render_view_bar(self):
        """看板口径 + 「当前在第几个视图」，当前项都染色。琐事屏不显示。"""
        if isinstance(self.view, ChoreView):
            return None
        bar = Text()
        for mode in SESSION_UI_MODES:
            if mode != SESSION_UI_MODES[0]:
                bar.append(" | ", style="dim")
            style = "bold reverse cyan" if self.session_ui_mode == mode else "dim"
            bar.append(mode, style=style)
        bar.append("    视图 ", style="dim")
        for i, v in enumerate(self.view_impls):
            if i:
                bar.append("  ", style="dim")
            style = "bold reverse cyan" if i == self.shell.view_index else "dim"
            bar.append("%d %s" % (i + 1, v.name), style=style)
        bar.append("    （Shift+Tab 切换）", style="dim")
        return bar

    # -- 视图（业务侧的对象；框架侧在 self.shell.views）------------------

    @property
    def views(self):
        return self.view_impls

    @property
    def view(self):
        return self.view_impls[self.shell.view_index]

    @property
    def view_index(self):
        return self.shell.view_index

    def cycle_view(self, step=1):
        """切到下一个视图。返回新视图。"""
        if not self.view_impls:
            return None
        self.shell.switch_view(step)
        self.say("切到「%s」视图" % self.view.name, "cmd")
        self.enter_view()
        return self.view

    def go_to_view(self, cls):
        """直接跳到某个视图（stat 命令用）。已经是它就原地不动。返回该视图。"""
        for i, v in enumerate(self.view_impls):
            if isinstance(v, cls):
                self.shell.set_view(i)
                self.enter_view()
                return v
        return None

    def enter_view(self):
        """进视图时的懒加载：统计拉数；任务视图第一次进时同步完成时间。"""
        if isinstance(self.view, StatsView) and self.stat is None:
            self.cmd_stat()
        elif isinstance(self.view, TaskView) and not self.offline:
            has_fm = any((t.get("focusmate") or {}).get("task_id")
                         for t in self.task_store.list())
            if has_fm and time.time() - self.task_sync_at > 60:
                try:
                    self.cmd_task_sync(quiet=True)
                except Exception as e:                    # noqa: BLE001
                    self.say("任务完成时间同步失败（不影响本地任务）：%s" % e, "warn")

    def cmd_stat(self, force=False, full=False):
        """拉生涯统计（**读**操作，后台跑）：stats.load 走缓存 + 增量刷新。

        - 默认：TTL 内直接用缓存，不打网络；过期只拉缺口 + 最近几天。
        - force：立刻增量刷新（``stat refresh``）。
        - full：丢掉缓存全量重拉（``stat refresh full``，很少需要）。

        拿到历史之后算数字在主线程做（纯 CPU，见 :meth:`_apply_stat_history`）。
        """
        action = self.net_action("stat.history", "拉生涯统计",
                                 {"force": bool(force), "full": bool(full)})
        job = self.jobs.submit(action, persist=False,
                               on_fail=lambda error: self._stat_failed(error))
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        if self.stat is None:
            self.stat_note = "正在后台拉（进度看面板）…"
            self.say("生涯统计在后台拉（进度看面板）；拉完数字会自己出来。", "cmd")
        return True

    def _stat_failed(self, error):
        self.had_error = True
        self.stat_note = "拉取失败：%s" % error
        self.say("生涯统计拉取失败：%s" % error, "error")

    def cycle_focus_mode(self):
        """Shift+Tab：只在专注视图生效，循环模式。返回新模式 key 或 None。"""
        if not isinstance(self.view, FocusView):
            self.say("Shift+Tab 切专注模式只在「专注」视图里有效；先按 Tab 切过去。", "warn")
            return None
        return self.view.cycle_mode()

    def current_goal(self):
        """当前 session 的目标。

        暂时读 temp/keep_session-goal.txt（有就用），没有就占位 —— 以后接
        Focusmate task / 手写目标时，只要换这里的取法，专注视图不用改。
        """
        path = os.path.join(TEMP, "keep_session-goal.txt")
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except OSError:
            pass
        return "（占位）还没接目标 —— 以后从 Focusmate task / temp/keep_session-goal.txt 读"

    def cmd_pause(self):
        """专注视图的 stop：消耗一次暂停（每次 PAUSE_MINUTES 分钟，每天 3 次）。"""
        ok, st, why = ks.start_pause()
        if not ok:
            self.say("暂停没生效：%s" % why, "warn")
            return
        self.say("暂停 %d 分钟（今天还剩 %d 次）—— 这几分钟里 daemon 不念你。"
                 % (ks.PAUSE_MINUTES, st["remaining"]), "ok")

    # -- 数据

    def daemon_state(self):
        pid = ks.read_pid()
        alive = bool(pid and ks.pid_alive(pid))
        st = ks.load_state()
        snap = st.get("api_snapshot") or {}
        return {"pid": pid, "alive": alive, "state": st, "snapshot": snap}

    def log_tail(self, n=1):
        """daemon 日志的最后 n 行 —— **只读文件尾部**。

        以前是 ``f.read().splitlines()``：每画一帧（也就是每敲一个字）都要把整份
        日志读进来再切分。日志一天长几 MB，这一项就从 0.1ms 涨到几十毫秒，
        而且越用越卡。现在从末尾 seek 一小块，并且按 (mtime, size) 记住结果。
        """
        try:
            st = os.stat(ks.LOG_FILE)
        except OSError:
            return []
        sig = (st.st_mtime_ns, st.st_size, n)
        memo = getattr(self, "_log_tail_memo", None)
        if memo is not None and memo[0] == sig:
            return memo[1]
        lines = []
        try:
            with open(ks.LOG_FILE, "rb") as f:
                want = 4096
                while True:
                    start = max(0, st.st_size - want)
                    f.seek(start)
                    lines = f.read().decode("utf-8", "replace").splitlines()
                    if start == 0:
                        break
                    if len(lines) > n:            # 尾部够用了
                        lines = lines[1:]         # 首行可能被截断在半条上
                        break
                    if want >= st.st_size:
                        break
                    want *= 4
        except OSError:
            return []
        tail = [line for line in lines if line.strip()][-n:]
        self._log_tail_memo = (sig, tail)
        return tail

    def refresh(self, quiet=False, timeout=180, wait=False):
        """拉一次**选中那一天**的 session，并和 data.csv 的契约合并成表格行。

        **读**操作：不预测，拿到真数据才换表格（失败时不要拿退化快照去覆盖
        已有的好数据 —— 快照里没有 session id，覆盖了后面 mute/title/cancel
        全都做不了）。默认**不阻塞**：网络丢进后台队列，进度和日志在面板里；
        ``wait=True`` 是给「必须先有真数据」的命令用的（``refresh --check``）。
        """
        self.reload_entries()
        day = self.sel_date
        if self.offline:
            if not self.rows:
                self._fallback_rows("离线模式")
            return False
        action = self.net_action("session.snapshot",
                                 "拉 session %s" % day.strftime("%m-%d"),
                                 {"date": day.isoformat(), "timeout": int(timeout)})
        job = self.jobs.submit(
            action, persist=False,
            on_ok=lambda payload: self._snapshot_arrived(payload, quiet=quiet),
            on_fail=lambda error: self._snapshot_failed(error, quiet=quiet))
        if wait:
            self.wait_net(timeout + 30)
        if self.jobs.sync or wait:
            return bool(job.outcome is not None and job.outcome.ok)
        if not quiet:
            self.say("正在拉 %s 的 session（后台，进度看面板）" % day.isoformat(), "cmd")
        return True

    def _snapshot_arrived(self, payload, quiet=False):
        count = self._apply_snapshot(payload, {})
        if not quiet:
            self.say("拉到 %d 场（%s）" % (count, (payload or {}).get("date") or ""), "info")

    def _snapshot_failed(self, error, quiet=False):
        self.had_error = True
        if self.rows and self.rows_source == "bridge":
            self.say("拉列表失败（表格保持上一次的结果）：%s" % error, "error")
        else:
            self._fallback_rows("bridge 出错")
            self.say("拉列表失败：%s" % error, "error")

    def wait_net(self, timeout=240.0):
        """等后台那一批跑完再往下走（`refresh --check` 这种要真数据的命令用）。

        这是**故意**阻塞的：一条「对账」命令必须拿着刚拉的账去对，
        不然它会对着一份旧表说「一场不缺」。
        """
        if self.jobs.sync:
            self.drain_net()
            return True
        done = self.jobs.wait(timeout)
        self.drain_net()
        return done

    # -- 表格行：契约（data.csv）和 API 上真实存在的 session 合并

    def rebuild_rows(self, now_ms=None):
        """把「契约」和「真实存在」按时间合并成一张表。

        契约有、API 有   → 已订（有 id，能 mute / title / cancel）
        契约有、API 没有 → 缺（草稿区里 = 还没吃进去；执行区里 = 该订却没订上）
        API 有、契约没有 → 计划外（你手动订的，或者你把 csv 那行删掉了）

        zone 是**每一行**的判据，不是整天的：start ≤ now+24h 才是执行区。
        """
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        now = datetime.datetime.now()
        day = self.sel_date
        self.reload_periods()
        plan = self.plan_rows_for(day)
        plan_labels = set(s["start"] for s in plan)
        api_by_label = {}
        for r in self.api_rows:
            api_by_label.setdefault(r["startLabel"], r)

        rows = []
        for slot in plan:
            start = sch.slot_datetime(slot, day)
            dur = int(slot["duration"])
            end = start + datetime.timedelta(minutes=dur)
            api = api_by_label.pop(slot["start"], None)
            if api:
                row = dict(api)
            else:
                s_ms = int(start.timestamp() * 1000)
                e_ms = int(end.timestamp() * 1000)
                row = {
                    "startMs": s_ms, "endMs": e_ms,
                    "startLabel": slot["start"], "endLabel": end.strftime("%H:%M"),
                    "durationMin": dur, "id": None, "title": "", "quiet": None,
                    "matched": None, "phase": self.phase_of(s_ms, e_ms, now_ms),
                }
            row["planned"] = True
            row["lineno"] = slot["lineno"]
            row["raw"] = slot["raw"]
            relocation = slot.get("relocation")
            if relocation:
                row["relocation"] = dict(relocation)
                row["relocated_from"] = slot.get("relocated_from")
                row["contract"] = "临时 %s→%s" % (slot.get("relocated_from") or "?",
                                                   slot["start"])
            else:
                row["contract"] = "%s,%s,%s" % (slot["weekday"], slot["start"], slot["duration"])
            row["zone"] = self.effective_zone(sch.zone_name(start, now), day)
            row["zone_raw"] = sch.zone_name(start, now)
            row["date"] = day.isoformat()
            rows.append(row)

        for r in self.api_rows:
            if r["startLabel"] in plan_labels:
                continue
            row = dict(r)
            start = datetime.datetime.fromtimestamp(r["startMs"] / 1000.0)
            row["planned"] = False
            row["lineno"] = None
            row["raw"] = ""
            row["contract"] = ""
            row["zone"] = sch.zone_name(start, now)
            row["date"] = start.date().isoformat()
            rows.append(row)

        rows.sort(key=lambda r: r["startMs"])
        # daemon 的「认账」结论（state.json 里 slots[key] 的 match / actual）：
        # 计划 16:15 没有同一分钟的 session，但实际被 16:00 25 + 16:30 50 覆盖了。
        # 这是**已经付过钱**的事实，表格必须照着它显示，不能再报「缺」。
        state_slots = self.daemon_slots()
        for row in rows:
            rec = state_slots.get("%sT%s" % (row.get("date"), row.get("startLabel"))) or {}
            if rec.get("match") and rec.get("match") != "exact":
                row["adopted"] = True
                row["actual_note"] = rec.get("actual") or rec.get("match")
            elif rec.get("match") == "exact" and rec.get("actual"):
                row["actual_note"] = rec.get("actual")
        for row in rows:
            try:
                self.attach_period_info(row, datetime.date.fromisoformat(row["date"]))
            except (KeyError, ValueError):
                row.setdefault("period", None)
                row.setdefault("period_id", None)
                row.setdefault("period_first", False)
                row.setdefault("task", None)
                row.setdefault("tasks", [])
                row.setdefault("task_text", "")
        self.rows = rows
        self.invalidate()          # 表格换了内容 → 画帧缓存作废

    def _fallback_rows(self, why):
        """拿不到 bridge 时，用 daemon 写下的 state.json 快照（只有时间，没有标题/静音）。"""
        self.reload_entries()
        st = ks.load_state()
        snap = st.get("api_snapshot") or {}
        iso = snap.get("sessions") or []
        now_ms = int(time.time() * 1000)
        want = self.sel_date.isoformat()
        api_rows = []
        for t in iso:
            try:
                dt = datetime.datetime.fromisoformat(t)
            except ValueError:
                continue
            # 快照是窗口（最多跨两天）的内容，按选中的那天过滤
            if dt.date().isoformat() != want:
                continue
            s_ms = int(dt.timestamp() * 1000)
            # 快照里没有 duration，先用 50min 占位；这个值只影响显示
            e_ms = s_ms + 50 * 60 * 1000
            api_rows.append({
                "startMs": s_ms,
                "endMs": e_ms,
                "startLabel": dt.strftime("%H:%M"),
                "endLabel": (dt + datetime.timedelta(minutes=50)).strftime("%H:%M"),
                "durationMin": None,
                "id": None,
                "title": "",
                "quiet": None,
                "matched": None,
                "phase": self.phase_of(s_ms, e_ms, now_ms),
            })
        api_rows.sort(key=lambda r: r["startMs"])
        self.api_rows = api_rows
        self.rows_source = "snapshot"
        self.rows_note = ("来自 keep_session 的 state.json 快照（%s）——只有时间，"
                          "没有标题/静音/伙伴/契约行" % why)
        self.rows_at = time.time()
        self.rebuild_rows(now_ms)

    def daemon_slots(self):
        """daemon 写下的 ``state.json`` 里的 ``slots``（带 2 秒缓存，画表格会问它）。"""
        now = time.time()
        cached = self._snapshot_cache.get("__daemon_slots__")
        if cached and now - cached[0] < 2.0:
            return cached[1]
        try:
            slots = (ks.load_state() or {}).get("slots") or {}
        except Exception:                       # noqa: BLE001 - 读不到就当没有
            slots = {}
        self._snapshot_cache["__daemon_slots__"] = (now, slots)
        return slots

    def actual_slots(self):
        """这一天的**实际** session（bridge 真数据）→ 收尾/对账要的形状。

        返回 ``[{"start": datetime, "duration": 分钟}]`` 或 None（离线 / 快照）。
        收尾按它发问，才不会把「计划 75min」当成一场去问。
        """
        if not self.authoritative:
            return None
        out = []
        for row in self.rows:
            if row.get("id") is None:
                continue
            try:
                start = datetime.datetime.fromtimestamp(row["startMs"] / 1000.0)
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"start": start, "duration": row.get("durationMin")})
        return out or None

    def effective_zone(self, zone, day):
        """``edit-tomorrow`` 设置：开着时，明天（以及之后）按草稿区算。

        需求：晚上 11 点想排明天早上 10 点（本来已经进 24 小时窗口 = 执行区，
        取消要敲宣言）—— 打开这个设置就是零阻力。过了凌晨，明天变成今天，
        又回到正常的 24 小时窗口规则（那时日期已经变了，不需要额外判断）。
        """
        if zone == "locked" and self.settings.get_bool("edit-tomorrow"):
            try:
                if day > datetime.date.today():
                    return "draft"
            except TypeError:
                return zone
        return zone

    @staticmethod
    def phase_of(start_ms, end_ms, now_ms):
        if now_ms < start_ms:
            return "未开始"
        if now_ms < end_ms:
            return "进行中"
        return "已结束"

    def plan_summary(self):
        """选中那天的契约执行情况：几场契约、几场真订上了、缺的是哪些。

        只有在 bridge 拉到真数据时才敢说"缺" —— state.json 快照里没有 session id，
        根本看不出订没订，那种情况一律返回 booked=None（渲染成"未知"）。
        草稿区里的"缺"也是正常的（还没到时间被吃进去）。
        """
        plan = [r for r in self.rows if r.get("planned")]
        if not plan:
            return None
        if self.rows_source != "bridge":
            return {"total": len(plan), "booked": None, "missing": None}
        missing = [r for r in plan if not r.get("id")]
        return {"total": len(plan), "booked": len(plan) - len(missing), "missing": missing}

    # -- 渲染

    def day_zone(self):
        """选中那天的区域（按天给个大致判断；每行还有更精确的 zone）。"""
        now = datetime.datetime.now()
        if self.sel_date < now.date():
            return "past"
        seen = set(r["zone"] for r in self.rows if r.get("planned"))
        if "locked" in seen:
            return "locked" if "draft" not in seen else "mixed"
        if seen:
            return "draft"
        if self.sel_date > now.date() and self.settings.get_bool("edit-tomorrow"):
            return "draft"
        return "locked" if self.sel_date == now.date() else "draft"

    def _day_cell(self):
        day = self.sel_date
        off = self.day_offset
        kinds = {0: ("今天", "bold"), -1: ("昨天", STYLE_MUTED), 1: ("明天", "bold")}
        name, style = kinds.get(off, ("%+d 天" % off, ""))
        zone = self.day_zone()
        zone_txt = {
            "past": ("过去 · 只能看", STYLE_MUTED),
            "locked": ("执行区 · 已吃进 API，取消要敲宣言", "bold red"),
            "draft": ("草稿区 · 100% 自由，随便改", "bold green"),
            "mixed": ("执行区 + 草稿区混合（按行看）」", "bold yellow"),
        }.get(zone, (zone, ""))
        cell = Text()
        cell.append("%s  %s %s" % (day.isoformat(), sch.cn_weekday_of_day(day), name), style="bold")
        cell.append("\n")
        cell.append(zone_txt[0], style=zone_txt[1])
        if off > 0 and self.settings.get_bool("edit-tomorrow"):
            cell.append("\n（edit-tomorrow 开着：未来的日子先按草稿区，凌晨后回到 24h 规则）",
                        style="green")
        return cell

    def _window_cell(self):
        now = datetime.datetime.now()
        win = sch.window(self.entries, now)
        cell = Text()
        cell.append("now → %s" % win["horizon"].strftime("%m-%d %H:%M"), style="bold")
        in_win = len(win["due"])
        cell.append("  执行区 %d 场" % in_win)
        if not in_win and not win["past"]:
            cell.append("  ⚠ 未来 24 小时一条契约都没有（不是放假就是断档）", style="bold red")
        if self.day_offset != 0:
            cell.append("   （窗口只跟今天/明天有关）", style="dim")
        return cell

    def render_header(self):
        d = self.daemon_state()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        if d["alive"]:
            daemon_cell = Text("● RUNNING", style="bold green")
            daemon_cell.append("  pid %s" % d["pid"], style="dim")
        else:
            daemon_cell = Text("○ STOPPED", style="bold red")
            if d["pid"]:
                daemon_cell.append("  残留 pid 文件 %s（进程已不在）" % d["pid"], style="dim")
        tail = self.log_tail(1)
        if tail:
            daemon_cell.append("\n" + tail[0], style="dim")

        up, browser, err = self.bridge.browser_state()
        if up and browser:
            bridge_cell = Text("● READY", style="bold green")
            extra = "  浏览器开着（占着 Chromium profile）"
            if self.bridge.last_idle_close:
                extra += "，上次释放 %s" % fmt_age(int(time.time() - self.bridge.last_idle_close))
            bridge_cell.append(extra, style="dim")
        elif up:
            bridge_cell = Text("○ IDLE", style="yellow")
            bridge_cell.append("  进程在，浏览器已释放（第一次命令要冷启动 ~15s）", style="dim")
        else:
            bridge_cell = Text("○ DOWN", style="bold red")
            bridge_cell.append("  " + (err or "还没起（第一次命令会自动起）"), style="dim")
        # 桥读的 profile 和 daemon 的不是一个 → 一律红字点名。这条静默地错过一次：
        # 登录态只在工作区那个 profile 里，桥去读老的 ~/.focusmate-mcp，每条命令
        # 都「未登录」，而表头还写着 READY/IDLE 看着一切正常。
        seen = getattr(self.bridge, "profile", "") or ""
        want = focusmate_home()
        if seen and want:
            want_profile = os.path.join(want, "browser-data")
            if os.path.abspath(seen) != os.path.abspath(want_profile):
                bridge_cell.append("\n⚠ profile 不是 daemon 那个：%s" % seen,
                                   style="bold red")
        if self.bridge_note:
            bridge_cell.append("\n" + self.bridge_note, style="yellow")

        t.add_row("Daemon", daemon_cell)
        t.add_row("Bridge", bridge_cell)
        t.add_row("选中", self._day_cell())
        t.add_row("窗口", self._window_cell())

        sins = self.pending_sins()
        if sins:
            cell = Text()
            cell.append("⛔ 锁死：%d 笔未认罪" % len(sins), style="bold red")
            for rec in sins[:3]:
                cell.append("\n%s  %s（%s）" % (rec.get("at", "")[11:19], rec.get("key"),
                                              rec.get("what") or ""), style="red")
            if len(sins) > 3:
                cell.append("\n…还有 %d 笔" % (len(sins) - 3), style="dim")
            cell.append("\n除了敲完宣言，TUI 不执行任何命令。", style="bold red")
            t.add_row("罪案", cell)

        t.add_row("时钟", Text(now_local().strftime("%Y-%m-%d %H:%M:%S"), style="bold"))
        return Panel(t, title="[bold]The Warden[/]", border_style="cyan", box=box.ROUNDED)

    def render_table(self):
        day = self.sel_date
        title = "The Contract Table · %s %s" % (day.isoformat(),
                                                sch.cn_weekday_of_day(day))
        if self.day_offset == 0:
            title += "（今天）"
        elif self.day_offset == 1:
            title += "（明天）"
        elif self.day_offset == -1:
            title += "（昨天）"
        else:
            title += "（day %+d）" % self.day_offset
        title += "  [dim]day %+d[/]" % self.day_offset
        if self.rows_at:
            if self.rows_source == "bridge":
                title += "  [dim](%s)[/]" % fmt_age(int(time.time() - self.rows_at))
            else:
                title += "  [yellow](快照)[/]"

        items, collapsed = visible_session_rows(self.rows, self.session_ui_mode)
        if not self.rows:
            empty = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                          title_justify="left", expand=False)
            empty.add_column("状态")
            empty.add_row("（这天上既没有契约、也没有 session）")
            return empty
        t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        hidden_cols = self.effective_hidden_columns()
        visible_cols = [(i, header, kw) for i, (header, kw)
                        in enumerate(SESSION_TABLE_COLUMNS) if header not in hidden_cols]
        for _pos, header, kw in visible_cols:
            t.add_column(header, **kw)
        visible_positions = [i for i, _header, _kw in visible_cols]

        live_idx = None
        for idx, r in items:
            # ▶ 只标真正在进行的场次：权威模式下计划窗口到了、但 API 没有 id
            # 的行不是「正在发生」，不能抢当前标记。
            if r.get("phase") != "进行中":
                continue
            if self.authoritative and not r.get("id"):
                continue
            live_idx = idx
            break
        zone_tag = {"locked": ("锁", "bold red"), "draft": ("草", "bold green"),
                    "past": ("过", STYLE_MUTED)}
        for idx, r in items:
            # 数据（标题/时间/状态）不用 dim：dim 是给说明和单位的，压在数据上就看不清了
            style = STYLE_MUTED if r["phase"] == "已结束" else ""
            st = Text()
            tag, tag_style = zone_tag.get(r.get("zone"), ("", ""))
            if tag:
                st.append(tag + " ", style=tag_style)
            if not self.authoritative:
                # 快照里没有 session id —— 订没订看不出来，别假装知道
                st.append(r["phase"], style=STYLE_MUTED)
                st.append(" 快照", style=STYLE_MUTED)
            elif r.get("planned") and not r.get("id") and r.get("adopted"):
                # 计划这一行在 API 里没有「同一分钟」的场次，但 daemon 认账了
                # （实际是别的场次覆盖了它：拆分 / 挪了半格）。别再说「缺」——
                # 「缺」会让你去重订一场和现有场次打架的 session。
                st.append("认账", style="bold green")
            elif not r.get("planned"):
                st.append("计划外", style="bold magenta")
            else:
                st.append(r["phase"], style={"进行中": "bold green", "未开始": "white",
                                             "已结束": STYLE_MUTED}[r["phase"]])
            quiet = r.get("quiet")
            if quiet is None:
                qtxt = "[%s]?[/]" % STYLE_MUTED
            elif quiet:
                qtxt = "[bold yellow]ON[/]"
            elif r.get("period_muted"):
                # 时段要求静音、但 Focusmate 那边还没设上（正在补 / 补失败）——
                # 不能显示成「·」，那等于假装这一行不在静音时段里。
                qtxt = "[yellow]静…[/]"
            else:
                qtxt = "[%s]·[/]" % STYLE_MUTED
            dur = r.get("durationMin")
            label = Text()
            if idx == live_idx:
                label.append("▶ ", style="bold green")
            label.append(r.get("title") or "", style=style)
            if r.get("matched"):
                # 注意：Text.append **不解析 markup**，所以要给 style 而不是塞 "[cyan]…[/]"
                label.append("  · 已匹配", style="cyan")
            if r.get("actual_note"):
                label.append("  · %s" % r["actual_note"], style="yellow")
            period = r.get("period") or {}
            first = bool(r.get("period_first"))
            pid = period.get("id") if first else ""
            pspan = ("%s–%s" % (period.get("start"), period.get("end"))
                     if first and period else "")
            ptype = (period.get("type") or period.get("calendar") or "") if first else ""
            pname = (period.get("name") or period.get("title") or "") if first else ""
            task_text = r.get("task_text") or ""
            if task_text:
                task_cell = Text(task_text, style="bold green")
            else:
                # 每一行都是一场要执行的 session（计划或实际）；没有任务就明确报警，
                # 不要留空让用户以为只是「这一格没画东西」。
                task_cell = Text("没有设定任务🔞", style="bold red")
            cells = [
                Text(pid or "", style="bold magenta"),
                Text(pspan, style="cyan"),
                Text(ptype, style="cyan"),
                Text(pname, style="cyan"),
                str(idx),
                "%s–%s" % (r["startLabel"], r.get("endLabel") or "?"),
                ("%dmin" % dur) if dur else "?",
                st,
                qtxt,
                Text(r.get("contract") or "—", style=STYLE_MUTED),
                label,
                task_cell,
            ]
            t.add_row(*(cells[i] for i in visible_positions))
        captions = []
        if collapsed:
            captions.append("… 还有 %d 条过去 session 已收起（open detail 查看）"
                            % collapsed)
        if hidden_cols:
            captions.append("已折叠列：%s（open <表头名> 打开）"
                            % "、".join(h for h, _kw in SESSION_TABLE_COLUMNS
                                        if h in hidden_cols))
        if not items:
            captions.append("working 模式没有可展示的实际 session（open detail 查看全部）")
        if captions:
            t.caption = Text("\n".join(captions), style="dim")
            t.caption_justify = "left"
        return t

    def render_status_rows(self):
        """「状态」框的正文：契约执行情况 + 一次性/长期规则 + 临时挪动 + 文件位置。

        用户要求：这一行（原来这里挂的「永不重订名单（N 条）」）换成警告，
        并且「契约（data.csv 周几）」和警告一起装进同一个「状态」框（平滑重线边框）。
        永不重订名单本体只在「统计」视图里逐行列。
        """
        rows = []
        plan = self.plan_summary()
        day = self.sel_date
        wd_cn = sch.cn_weekday_of_day(day)
        if plan is None:
            rows.append(("契约：data.csv 里没有 %s 的规则（这天不排 session）" % wd_cn,
                         "dim"))
        elif plan["booked"] is None:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 订没订看不出来"
                         "（state.json 快照，不是真数据）" % (wd_cn, plan["total"]),
                         "yellow"))
        elif not plan["missing"]:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 已落地 %d 场 ✅ 一场不缺"
                         % (wd_cn, plan["total"], plan["booked"]), "bold green"))
        else:
            rows.append(("契约（data.csv %s）：计划 %d 场 · 已落地 %d 场 · ⚠ 缺 %d 场：%s"
                         % (wd_cn, plan["total"], plan["booked"], len(plan["missing"]),
                            "、".join("%s(%smin)" % (r["startLabel"],
                                                     r.get("durationMin") or "?")
                                      for r in plan["missing"])),
                         "red"))
            if self.day_zone() in ("draft", "past"):
                rows.append(("  （草稿区/过去：还没被吃进去是正常的，daemon 下一轮会补）",
                             "dim"))

        # 契约的三个来源：时段排布 / 一次性 session / 长期规则
        try:
            counts = sp.plan_counts(self.download, self.layouts)
        except Exception:                       # noqa: BLE001 - 坏文件不该炸框
            counts = {}
        if counts:
            pieces = ["时段排布 %d" % counts.get("period", 0)]
            if counts.get("extra"):
                pieces.append("一次性 %d" % counts["extra"])
            if counts.get("rule"):
                pieces.append("长期规则 %d" % counts["rule"])
            rows.append(("来源：" + " · ".join(pieces)
                         + "   （session = 只加这一天；session --permanent = 写进日历）",
                         "dim"))

        relocated = [r for r in self.rows if r.get("planned") and r.get("relocation")]
        if relocated:
            rows.append(("临时挪动（只影响这一天，不改 data.csv）：" +
                         "、".join("%s→%s" % (r.get("relocated_from") or "?",
                                              r["startLabel"]) for r in relocated),
                         "magenta"))
        rows.extend(self.freshness_rows())
        rows.append((sch.data_csv_path(), "dim"))
        return rows

    # -- 警告（repl_kit.WarningBoard）-----------------------------------

    def sessions_for_day_plan(self, day=None):
        """那一天**排好的** session（时段 + 一次性；不含 API、不含长期规则展开）。"""
        day = day or self.sel_date
        return sp.sessions_for_day(self.download, self.layouts, day)

    def repeated_task_for(self, day, session):
        """这一场有没有本次重复生成的子任务；没有就 None。"""
        for task in self.task_store.tasks_for_session(day, session.get("start")):
            if task.get("repeat_parent"):
                return task
        return None

    def _suggest_repeat_spec(self, day=None, sessions=None):
        """建议的 --repeat 取值：那天最后一场的时间（没有就 22:15）。"""
        day = day or datetime.date.today()
        sessions = sessions if sessions is not None else self.sessions_for_day_plan(day)
        start = sessions[-1]["start"] if sessions else "22:15"
        return "%s@%s" % (cfg.WEEKDAYS[day.weekday()], start)

    # -- 琐事 Project 空闲屏 -------------------------------------------------

    def _chore_now_dt(self):
        return self.chore_now or datetime.datetime.now()

    def _chore_active_session(self, now=None):
        """现在是不是正处在 session 里；是就返回那一行/时段，否则 None。

        权威模式（bridge 拉到了真数据）只认真的在进行、且有 session id 的场次；
        离线/快照才退回 ``data.csv`` 的计划窗口 —— 和专注视图用同一套口径。
        """
        now = now or self._chore_now_dt()
        if self.authoritative and self.sel_date == now.date():
            now_ms = int(now.timestamp() * 1000)
            try:
                live = [row for row in self._actual_rows(self.rows)
                        if row.get("startMs") is not None
                        and row["startMs"] <= now_ms < (row.get("endMs") or 0)]
            except Exception:                  # noqa: BLE001 - 画空闲屏不该被行数据弄崩
                live = []
            if live:
                live.sort(key=lambda row: (row.get("startMs") or 0,
                                           self.rows.index(row)))
                return live[-1]
            return None
        try:
            got = ks.current_session(self.entries, now)
        except Exception:                      # noqa: BLE001
            got = None
        if not got:
            return None
        day, slot, start, end = got
        return {"date": day.isoformat(), "startLabel": slot.get("start"),
                "endLabel": end.strftime("%H:%M")}

    def _next_session_start(self, now=None):
        """下一场 session 的开始时间（用来决定 ``q`` 退出后压到什么时候）。

        先看 data.csv 的计划窗口；当前选中的那一天若有 bridge 真数据，也把
        只存在于 Focusmate 的场次算进去。找不到就压到明天零点。
        """
        now = now or self._chore_now_dt()
        starts = []
        for delta in range(0, 8):
            day = now.date() + datetime.timedelta(days=delta)
            try:
                slots = self.plan_rows_for(day)
            except Exception:                  # noqa: BLE001
                slots = []
            for slot in slots:
                try:
                    start = sch.slot_datetime(slot, day)
                except Exception:              # noqa: BLE001
                    continue
                if start > now:
                    starts.append(start)
        if self.authoritative and self.sel_date == now.date():
            now_ms = int(now.timestamp() * 1000)
            for row in self.rows:
                try:
                    if row.get("id") and int(row.get("startMs") or 0) > now_ms:
                        starts.append(datetime.datetime.fromtimestamp(
                            row["startMs"] / 1000.0))
                except (TypeError, ValueError, KeyError, OSError):
                    continue
        if starts:
            return min(starts)
        return datetime.datetime.combine(now.date() + datetime.timedelta(days=1),
                                         datetime.time(0, 0))

    def _suppress_chore_idle(self, now=None):
        """``q`` 退出空闲屏：压到下一场 session 开始（没有就明天）。"""
        now = now or self._chore_now_dt()
        self.chore_suppressed_until = self._next_session_start(now)
        self.chore_last_note = ""
        self.invalidate()

    def _on_view_change(self, view):
        """记下用户什么时候手动离开琐事屏 —— 同一段空档别再自动弹回来。"""
        try:
            previous_index = self._chore_last_view_index
            previous = (self.view_impls[previous_index]
                        if 0 <= previous_index < len(self.view_impls) else None)
            current_index = self.shell.view_index
            if isinstance(view, ChoreView) and not isinstance(previous, ChoreView):
                # 进入琐事屏时记住来路；q / 自动退出都回到这里。
                self._chore_return_index = previous_index
            if (isinstance(previous, ChoreView) and not isinstance(view, ChoreView)
                    and not self._chore_leaving):
                self._suppress_chore_idle()
            self._chore_last_view_index = current_index
        except Exception:                      # noqa: BLE001 - 切视图不能因为这里坏掉
            pass

    def chore_binding(self):
        """设置里的琐事项目 → ``(project, error)``。"""
        raw = str(self.settings.get("chore-project") or "").strip()
        if not raw:
            return None, ("还没绑定「琐事 Project」：先 `project` 看项目序号，"
                          "再 `set chore-project #1`（或项目名 / OmniFocus pk）")
        try:
            return self.projects.find(raw), ""
        except ValueError as e:
            return None, str(e)

    def chore_project_tasks(self, project=None):
        """绑定项目里可做的一条条任务；父壳展开成其未完成子任务。"""
        if project is None:
            project, _err = self.chore_binding()
        if not project:
            return []
        try:
            invested = pj.invested(self.task_store, project)
            items = pj.remaining_tasks(project, invested)
        except Exception:                      # noqa: BLE001 - 坏项目不该炸空闲屏
            return []
        out = []
        for item in items:
            children = item.get("children") or []
            if children:
                for child in children:
                    rec = dict(child)
                    rec["_parent_title"] = item.get("title")
                    out.append(rec)
            else:
                out.append(item)
        return out

    def chore_snapshot(self):
        """空闲屏当前该显示什么：项目 / 任务列表 / 当前任务 / 错误说明。"""
        project, error = self.chore_binding()
        tasks = self.chore_project_tasks(project) if project else []
        if project and not tasks:
            error = ("琐事项目「%s」里没有未完成任务了 —— 去 OmniFocus 加一条，"
                     "或换一个 `chore-project`" % (project.get("title") or project.get("id")))
        if not tasks:
            return {"project": project, "tasks": [], "task": None, "index": 0,
                    "error": error}
        index = self.chore_index % len(tasks)
        return {"project": project, "tasks": tasks, "task": tasks[index],
                "index": index, "error": error}

    def _chore_center(self, body):
        """把琐事内容放在整个终端画布的正中央（水平 + 垂直居中）。"""
        body.justify = "center"
        try:
            height = int(self.shell.dashboard_budget())
        except Exception:                          # noqa: BLE001
            height = 20
        try:
            width = int(self.frame_width())
        except Exception:                          # noqa: BLE001
            width = 80
        return Align.center(body, vertical="middle",
                            height=max(6, height), width=max(24, width))

    def render_chore_idle(self):
        """琐事屏的全部内容：一条任务 + m/c/q 提示，在屏幕正中央居中展示。"""
        snap = self.chore_snapshot()
        body = Text(justify="center")
        if self.chore_last_note:
            body.append(self.chore_last_note + "\n\n", style="bold green")
        if not snap.get("task"):
            body.append("琐事 Project\n\n", style="bold")
            body.append((snap.get("error") or "现在没有可显示的琐事。") + "\n\n",
                        style="yellow")
            body.append("q 退出（回到正常界面）", style="cyan")
            return self._chore_center(body)
        task = snap["task"]
        project = snap.get("project") or {}
        body.append("琐事\n", style="dim")
        body.append(str(task.get("title") or task.get("pk") or "（无标题）"),
                    style="bold")
        body.append("\n\n", style="")
        body.append("项目：%s" % (project.get("title") or "?"), style="dim")
        bits = []
        if task.get("duration"):
            bits.append("%d 分钟" % int(task["duration"]))
        if task.get("count"):
            bits.append("%d count" % int(task["count"]))
        if task.get("due"):
            bits.append("截止 %s" % str(task["due"]).replace("T", " ")[:16])
        if task.get("_parent_title"):
            bits.append("父任务：%s" % task["_parent_title"])
        if bits:
            body.append("\n" + " · ".join(bits), style="dim")
        body.append("\n\n", style="")
        body.append("m 完成    c 换一个 task    q 退出（回到正常界面）", style="cyan")
        return self._chore_center(body)

    def _enter_chore_view(self):
        for index, view in enumerate(self.view_impls):
            if isinstance(view, ChoreView):
                self.chore_last_note = ""
                self.invalidate()
                self.shell.set_view(index)
                return True
        return False

    def _leave_chore_view(self, suppress=False):
        if not isinstance(self.view, ChoreView):
            return False
        if suppress:
            self._suppress_chore_idle()
        target = self._chore_return_index
        if (target < 0 or target >= len(self.view_impls)
                or isinstance(self.view_impls[target], ChoreView)):
            target = 0
        self._chore_leaving = True
        try:
            self.shell.set_view(target)
        finally:
            self._chore_leaving = False
        self.chore_last_note = ""
        self.invalidate()
        return True

    def _maybe_sync_chore_mode(self):
        """每帧看一眼：进/出「没有 session」的琐事屏。

        没有 session 时整屏接管（从哪个视图来都会接管，q 回原视图）；但收尾、
        plan 问答、项目对账、导入问答、罪案锁死这些打断态优先，不抢输入。
        """
        now = self._chore_now_dt()
        if isinstance(self.view, ChoreView):
            if self._chore_active_session(now) is not None:
                self._leave_chore_view(suppress=False)
            return False
        if (self.review_active() or self.nostart is not None
                or self.planning is not None or self.pending_import is not None
                or self.pending_advance is not None or self.pending_op_type
                or self.project_sync is not None or self.pending_sins()):
            return False
        if (self.chore_suppressed_until is not None
                and now < self.chore_suppressed_until):
            return False
        if self._chore_active_session(now) is not None:
            return False
        project, _error = self.chore_binding()
        if not project or not self.chore_project_tasks(project):
            return False
        return self._enter_chore_view()

    def _chore_finish_writeback(self, project, task, kind="完成"):
        """项目任务完成后写回 OmniFocus（单向联动；off 就只改本地）。"""
        pk = str((task or {}).get("pk") or "").strip()
        if not pk:
            return ""
        if not self.omnifocus_enabled():
            return "OmniFocus 关着 → 只改了本地"
        action = self.net_action(
            "omnifocus.complete",
            "OmniFocus %s琐事 %s" % (kind, pk[:8]),
            {"pk": pk, "at": None, "due": task.get("due") or None},
            source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self.say(
            "琐事「%s」写回 OmniFocus 失败：%s"
            % (task.get("title") or pk, error), "warn"))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 写回失败：%s" % (
                    outcome and outcome.error or "没跑")
            return "OmniFocus 已写回"
        return "OmniFocus 写回丢后台了"

    def _chore_complete(self):
        snap = self.chore_snapshot()
        task, project = snap.get("task"), snap.get("project")
        if not task or not project:
            self.chore_last_note = "现在没有可完成的琐事。"
            self.invalidate()
            return False
        try:
            updated, done_task, auto_parent = self.projects.complete_task(
                project["id"], task.get("pk"))
        except (ValueError, TypeError) as e:
            self.chore_last_note = "完成失败：%s" % e
            self.invalidate()
            return False
        notes = ["✅ 已完成：%s" % (done_task.get("title") or done_task.get("pk"))]
        note = self._chore_finish_writeback(updated, done_task)
        if note:
            notes.append(note)
        if auto_parent is not None:
            notes.append("父任务「%s」的子任务全部完成 → 父任务自动完成"
                         % (auto_parent.get("title") or auto_parent.get("pk")))
            note = self._chore_finish_writeback(updated, auto_parent, kind="父任务")
            if note:
                notes.append(note)
        self.chore_last_note = "；".join(notes)
        self.invalidate()
        return True

    def _chore_next(self):
        snap = self.chore_snapshot()
        count = len(snap.get("tasks") or [])
        if count <= 1:
            self.chore_last_note = "只有这一条琐事，没有别的可换。"
        else:
            self.chore_index = (self.chore_index + 1) % count
            self.chore_last_note = ""
        self.invalidate()
        return True

    def _handle_chore_input(self, line):
        """琐事屏只吃 m / c / q；返回 True = 已处理。"""
        if not isinstance(self.view, ChoreView):
            return False
        cmd = str(line or "").strip().lower()
        if cmd in ("q", "quit", "exit"):
            self._leave_chore_view(suppress=True)
        elif cmd in ("m", "done", "完成"):
            self._chore_complete()
        elif cmd in ("c", "next", "换", "换一个"):
            self._chore_next()
        elif not cmd:
            return True
        else:
            self.chore_last_note = "琐事屏只认 m（完成）/ c（换一个）/ q（退出）。"
            self.invalidate()
        return True

    def collect_warnings(self):
        """现在有哪些「问题」要显示在状态框里。纯本地计算，不联网、不写盘。"""
        out = []
        # 1) data.csv 解析问题（schedule.py 给的 warning）。最多列 3 条：
        #    状态框是给人一眼看"哪里不对"的，不是日志。
        entry_warnings = [str(t) for t in (self.entry_warnings or []) if str(t).strip()]
        for text in entry_warnings[:3]:
            if "重复" in text:
                hint = ("同一天同一时刻只能留一行：删掉多余的，"
                        "或改成 session --permanent 只留一条长期规则")
            else:
                hint = "格式：mon,HH:MM,50 或 YYYY-MM-DD,HH:MM,50（时长 25/50/75）"
            out.append(repl_kit.Warning(text, level="error", source="data.csv", hint=hint))
        if len(entry_warnings) > 3:
            out.append(repl_kit.Warning(
                "data.csv 还有 %d 条解析问题没列出来" % (len(entry_warnings) - 3),
                level="error", source="data.csv",
                hint="python3 schedule.py show 看全部"))
        # 2) 时段 layout 坏了（排不出来）
        for period in self.periods_for_day(self.sel_date):
            try:
                sp.sessions_for_period(period, self.layouts)
            except ValueError as e:
                out.append(repl_kit.Warning(
                    "%s %s 的排布坏了：%s" % (period.get("date"), period.get("id"), e),
                    level="error", source="layout",
                    hint="period %s 25 50 重新排，或删掉 temp/session-layouts.json 里那条"
                         % period.get("id")))
        # 3) 重算 data.csv 时被冲掉的旧行（备份仍在 temp/data.csv.bak）
        if self.csv_dropped:
            out.append(repl_kit.Warning(
                "重算 data.csv 冲掉了 %d 行旧契约（%s%s）"
                % (len(self.csv_dropped), "、".join(self.csv_dropped[:2]),
                   "…" if len(self.csv_dropped) > 2 else ""),
                level="warn", source="data.csv",
                hint="用 session --permanent 加回长期规则，或把这些行放回日历/一次性 session"))
        # 3.5) 过期还没做完的任务：给 `inspect-due` 一个入口（不然这条命令没人知道）
        try:
            overdue = psv.due_candidates(self.task_store)
        except Exception:                      # noqa: BLE001 - 警告板不能因为这里坏掉
            overdue = []
        if overdue:
            first = overdue[0]
            title = self.task_store.focusmate_title(first, max_len=18)
            out.append(repl_kit.Warning(
                "%d 条任务过了截止时间还没做完（最早：%s「%s」）"
                % (len(overdue), first.get("id"), title),
                level="warn", source="task",
                hint="敲 inspect-due 逐个过一遍"
                     "（A 延期 / B 放弃 / C 重议 / D 补录 / E 推后 / G 延长排期）"))
        # 3.6) 网络操作失败账：有它就告诉你，而且告诉你下一步敲什么
        failures = self.jobs.failures
        if failures:
            first = failures[0]
            more = (" 等 %d 条" % len(failures)) if len(failures) > 1 else ""
            out.append(repl_kit.Warning(
                "%d 条网络操作失败（%s：%s%s）" % (len(failures), first.action.label,
                                                first.error, more),
                level="error", source="网络",
                hint="re-action 重放全部（re-action --list 先看清单，--clear 清空）"))
        # 3.7) 今天还没 plan：项目在那儿、空闲也在那儿，但今天一个项目都没分到 count
        try:
            pending = self.pending_plan(datetime.date.today())
        except Exception:                      # noqa: BLE001 - 警告板不能因为这里坏掉
            pending = None
        if pending:
            out.append(repl_kit.Warning(
                "今天还没 plan：还有 %d 场空闲（%d count）没分给项目（%s）"
                % (pending["sessions"], pending["counts"],
                   "、".join(str(s) for s in pending["starts"][:3])
                   + ("…" if len(pending["starts"]) > 3 else "")),
                level="warn", source="项目",
                hint="敲 plan 分今天的资源（plan +1 = 明天；task 1 4 = 项目 #1 分 4 个 count）"))
        # 4) 重复任务：今天该不该有 occurrence（日期对上就算；挂不挂得上 session
        # 是另一回事，不再用「同起点同时长」否定整条规则）
        today = datetime.date.today()
        today_sessions = self.sessions_for_day_plan(today)
        landed = []
        for task in self.task_store.repeated_tasks():
            if self.task_store.repeat_due_on(task, today):
                landed.append(task)
        if self.settings.get("tomorrow-plan") and not self.task_store.repeated_tasks():
            out.append(repl_kit.Warning(
                "设置里填了「明日时间安排」（%s），但没有对应的重复任务"
                % self.settings.get("tomorrow-plan"),
                level="warn", source="重复任务",
                hint="task create title=明日时间安排 --repeat %s"
                     % self.settings.get("tomorrow-plan")))
        elif not landed:
            out.append(repl_kit.Warning(
                "今天没有「明日时间安排」这类重复任务要生成",
                level="warn", source="重复任务",
                hint="task create title=明日时间安排 --repeat %s"
                     % self._suggest_repeat_spec(today, today_sessions)))
        # 5) 琐事 Project：没绑定 / 绑定失效 / 项目里没活了都要说一声。
        project, chore_error = self.chore_binding()
        if chore_error:
            out.append(repl_kit.Warning(
                "琐事 Project 不可用：%s" % chore_error,
                level="warn", source="琐事",
                hint="project 看项目列表；set chore-project #1 绑定（空 = 恢复未绑定）"))
        elif project and not self.chore_project_tasks(project):
            out.append(repl_kit.Warning(
                "琐事项目「%s」没有未完成任务了" % (project.get("title") or project.get("id")),
                level="warn", source="琐事",
                hint="去 OmniFocus 加一条任务并 project refresh；或 set chore-project 换项目"))
        return out

    def refresh_warnings(self, force=False, max_age=5.0):
        """重算警告板（每次操作之后 / 最多 5 秒一次；不联网、不写盘）。

        顺便（每帧，自己节流 1 秒）看一眼「有没有刚结束的 session 该收尾」：
        daemon 在结束 +15 秒把 warden 拉到前台并把这场写进
        ``temp/keep_session-postsession.json``，这里把它接过来。
        """
        try:
            self._maybe_start_review()
        except Exception:                          # noqa: BLE001 - 收尾坏了也不能没帧
            pass
        try:
            self._maybe_start_nostart()
        except Exception:                          # noqa: BLE001 - 这一问坏了也不能没帧
            pass
        try:
            self._maybe_apply_mutes()
        except Exception:                          # noqa: BLE001 - 静音坏了也不能没帧
            pass
        try:
            self._maybe_daily_project_sync()
            self._maybe_start_project_sync_conflict()
        except Exception:                          # noqa: BLE001 - 项目对账坏了也不能没帧
            pass
        try:
            self._maybe_sync_chore_mode()
        except Exception:                          # noqa: BLE001 - 空闲屏坏了也不能没帧
            pass
        now = time.time()
        if not force and self.warnings_at and now - self.warnings_at < max_age:
            return self.warnings
        before = self.warnings.signature()
        self.warnings.set_all(self.collect_warnings())
        self.warnings_at = now
        self.warnings_sig = self.warnings.signature()
        if self.warnings_sig != before:            # 警告变了 → 画面得重排版
            self.invalidate()
        return self.warnings

    def render_messages(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=False)
        colors = {"info": "white", "ok": "green", "warn": "yellow", "error": "bold red",
                  "cmd": "bold cyan"}
        for stamp, kind, text in self.messages[-MESSAGE_SHOW:]:
            t.add_row(stamp, Text(text, style=colors.get(kind, "white")))
        return Panel(t, title="[bold]Recent[/]", border_style="dim", box=box.ROUNDED)

    # -- 找行

    def pick(self, token):
        try:
            idx = int(token)
        except ValueError:
            raise ValueError("ID 得是数字，比如 3；你给的是 %r" % token)
        if idx < 1 or idx > len(self.rows):
            if not self.rows:
                raise ValueError("表格还是空的，先跑 refresh")
            raise ValueError("没有 ID %d（当前 1..%d）" % (idx, len(self.rows)))
        return idx, self.rows[idx - 1]

    # -- 宣言闸门（The Walk of Shame）

    def require_declaration(self, reason):
        """一字不差敲完宣言才放行。敲错就鸣笛、继续问（不设次数上限）。"""
        body = Text()
        body.append("要执行「%s」，你得先把下面这段话一字不差地敲一遍。\n\n" % reason,
                    style="bold red")
        body.append(DECLARATION + "\n\n", style="bold")
        body.append("共 %d 字符（含标点）。少一个标点、错一个引号，都继续锁死并鸣笛。\n"
                    "Ctrl+C 可以放弃 —— 那就什么都不做。"
                    % len(DECLARATION), style="dim")
        self.emit(Panel(body, title="[bold red]⛓  The Walk of Shame[/]",
                                 border_style="bold red", box=box.HEAVY))
        tries = 0
        while True:
            try:
                typed = input("  宣言> ")
            except (EOFError, KeyboardInterrupt):
                self.emit("\n  [dim]放弃了。[/]")
                return False
            ok, why = check_declaration(typed)
            if ok:
                self.say("✅ 认罪完成（第 %d 次敲对）。" % (tries + 1), "ok")
                return True
            tries += 1
            beep()
            self.emit("[bold red]✗ 不通过：%s[/]" % why)
            self.say("宣言不通过（第 %d 次）：%s" % (tries, why), "error")

    def confess(self, sins):
        """认罪收尾：把每一笔罪的 slot 判成「已放弃」，并且让它真的消失。

        1. 标 abandoned —— daemon 从此不再重订这一场（宣言的牙齿）
        2. 如果它已经被 daemon 重订回来了 → 再取消掉（否则"放弃"是假的）
        3. 罪从待认罪挪进 archive（.jsonl 只增不删）
        """
        keys = [rec.get("key") for rec in sins if rec.get("key")]
        for key in keys:
            ks.mark_abandoned(key, why="walk-of-shame", note="TUI 里敲完宣言后放弃")
        self.say("已判 %d 场为「已放弃」：daemon 不会再重订。" % len(keys), "ok")

        # 已经被 daemon 重订回来吗？查快照 + 取消都在后台队列里做：
        # 浏览器冷启动时输入线程不能跟着一起冻住，进度条会写清楚在做哪一场。
        action = self.net_action(
            "session.confess_cancel",
            "认罪后取消已重订的场次（%d 场）" % len(keys),
            {"keys": keys}, source=SRC_FOCUSMATE)

        def done(payload):
            payload = payload or {}
            if payload.get("cancelled"):
                self.say("  ✅ 后台复查：%d 场被重订回来的 session 已真的取消。"
                         % payload["cancelled"], "ok")
            if payload.get("missing"):
                self.say("  %d 场 API 里还没有（daemon 还没重订回来）。"
                         % payload["missing"], "info")
            if payload.get("past"):
                self.say("  %d 场已经过去：只判放弃，没动 API。" % payload["past"], "info")

        def failed(error):
            self.had_error = True
            self.say("  ❌ 复查/取消失败：%s（判决已落，daemon 不会重订；"
                     "失败账可用 re-action 重放）" % error, "error")

        job = self.jobs.submit(action, on_ok=done, on_fail=failed)
        if self.jobs.sync:
            # 同步模式（测试 / --exec）：submit 已经跑完，让错误照旧表现为失败。
            if job.outcome is None or not job.outcome.ok:
                self.had_error = True
        else:
            self.say("  正在后台复查并取消已被重订的场次（进度看面板）。", "cmd")
        n = ks.clear_sins(keys)
        self.say("罪已归档 %d 笔（temp/warden-sins.jsonl，只增不删）。" % n, "ok")

    # -- 命令实现

    def cmd_mute(self, args):
        if not args:
            raise ValueError("用法：mute N [on|off]")
        idx, row = self.pick(args[0])
        if len(args) > 1:
            want = args[1].lower()
            if want in ("on", "true", "1", "开"):
                want = True
            elif want in ("off", "false", "0", "关"):
                want = False
            else:
                raise ValueError("第三个词只能是 on 或 off；你给的是 %r" % args[1])
        else:
            want = not bool(row.get("quiet"))
        if row.get("id") is None:
            raise ValueError("这场是从 state.json 快照来的，没有 session id，改不了。先 refresh 成功一次。")
        verb = "开" if want else "关"
        self.say("#%d %s–%s quiet %s（乐观：表格先改，Focusmate 在后台）…"
                 % (idx, row["startLabel"], row.get("endLabel") or "?", verb), "cmd")
        before_row = dict(row)
        self._patch_api_row(row["id"], quiet=want)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.mute",
                                 "quiet %s %s" % (row["startLabel"], "ON" if want else "off"),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"], "quiet": bool(want)})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d quiet %s" % (idx, verb), before_row, error))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d quiet = %s（API 复核一致）"
                         % (idx, "ON" if want else "off"), "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d quiet = %s（已记上；Focusmate 复核回来我再说话）"
                 % (idx, "ON" if want else "off"), "ok")
        return True

    def cmd_title(self, args):
        if len(args) < 2:
            raise ValueError('用法：title N "文本"（清空用 title N ""）')
        idx, row = self.pick(args[0])
        title = " ".join(args[1:]).strip()
        if row.get("id") is None:
            raise ValueError("这场是从 state.json 快照来的，没有 session id，改不了。先 refresh 成功一次。")
        if len(title) > 100:
            raise ValueError("标题最长 100 字符，现在 %d" % len(title))
        self.say("#%d 改标题为 %r（乐观：表格先改，Focusmate 在后台）…" % (idx, title), "cmd")
        before_row = dict(row)
        self._patch_api_row(row["id"], title=title)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.title",
                                 "写标题 %s %r" % (row["startLabel"], title[:20]),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"], "title": title})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d 写标题" % idx, before_row, error))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d 标题已写入（API 复核一致）" % idx, "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d 标题已记上（Focusmate 复核回来我再说话）" % idx, "ok")
        return True

    def cmd_cancel(self, args):
        """取消。默认只取消**这一个时间点的这一场**（判 abandoned，daemon 不再重订），
        日历里的规则不动；加 ``--permanent`` 才把日历里这个 session 永久删掉。
        摩擦层由**这一行**在哪个区决定：草稿区零阻力，执行区敲宣言。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            raise ValueError("用法：cancel N [--permanent]")
        permanent = False
        kept = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "永久"):
                permanent = True
            else:
                kept.append(tok)
        if not kept:
            raise ValueError("用法：cancel N [--permanent]")
        idx, row = self.pick(kept[0])
        zone = row.get("zone")

        if zone == "past":
            raise ValueError("#%d 已经开始（%s）—— 过去的场次不取消也不补订。"
                             % (idx, row["startLabel"]))

        key = sch.key_of(self.sel_date, row["startLabel"])
        if permanent:
            return self._cancel_permanent(idx, row, key)
        return self._cancel_single(idx, row, key)

    def _cancel_declaration_gate(self, idx, row):
        """执行区取消前要敲宣言；草稿区不用。返回是否放行。"""
        if row.get("zone") == "draft":
            return True
        matched = row.get("matched")
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True)
        info.add_column()
        info.add_row("目标", "#%d  %s–%s（%s）" % (idx, row["startLabel"],
                                                row.get("endLabel") or "?", row["phase"]))
        info.add_row("契约", row.get("contract") or "—（计划外，手动订的）")
        info.add_row("标题", row.get("title") or "[dim]（无）[/]")
        info.add_row("伙伴", "[cyan]已匹配 —— 对方会被放鸽子[/]" if matched else "[dim]还没匹配[/]")
        info.add_row("session", (row.get("id") or "—")[:8] + ("…" if row.get("id") else ""))
        self.emit(Panel(info, title="[bold red]⚠  执行区：取消要过宣言闸门[/]",
                                 border_style="bold red", box=box.HEAVY))
        if not self.require_declaration("取消 #%d（%s %s）"
                                        % (idx, self.sel_date.isoformat(), row["startLabel"])):
            self.say("没认罪，什么都没动。", "warn")
            return False
        return True

    def _cancel_single(self, idx, row, key):
        """默认 cancel：只弃这一个时间点的 session，日历规则保留。"""
        if not self._cancel_declaration_gate(idx, row):
            return False
        ks.mark_abandoned(key, why="walk-of-shame",
                          note="TUI 里放弃这一场（#%d %s）" % (idx, row["startLabel"]))
        if row.get("planned"):
            self.say("已判 %s 为「已放弃」：daemon 不会再重订这个时间点的这一场"
                     "（日历规则还在；`cancel %d --permanent` 才从日历删掉）。"
                     % (key, idx), "ok")
        else:
            self.say("已判 %s 为「已放弃」：这一场不是日历规则（计划外），"
                     "daemon 本来也不会重订。" % key, "ok")
        if row.get("id") is None:
            self.say("它本来就没订上（契约缺的那一场），不用动 API。", "info")
            self.refresh(quiet=True)
            return True
        return self._cancel_booked_session(idx, row)

    def _cancel_permanent(self, idx, row, key):
        """``cancel N --permanent``：日历里这个 session 永久不要 + 取消今天这场。"""
        if not self._cancel_declaration_gate(idx, row):
            return False
        ks.mark_abandoned(key, why="permanent",
                          note="TUI 里永久取消这一场（#%d %s）" % (idx, row["startLabel"]))
        self.say("已判 %s 为「永久不要」：daemon 不会再重订这一场。" % key, "ok")
        self._remove_contract_row_for_cancel(idx, row)
        if row.get("id") is None:
            self.say("它本来就没订上，不用动 API。", "info")
            self.refresh(quiet=True)
            return True
        return self._cancel_booked_session(idx, row)

    def _remove_contract_row_for_cancel(self, idx, row):
        """把日历里这一行（data.csv）删掉；临时挪动的行会连挪动记录一起清。"""
        relocation = row.get("relocation")
        from_hhmm = (row.get("relocated_from")
                     or (relocation or {}).get("from")
                     or row.get("startLabel"))
        if row.get("lineno"):
            if sch.fingerprint() != self.csv_fp:
                raise ValueError("data.csv 刚被别处改过（指纹不一致）。先 refresh 再动手，免得删错行。")
            raw = sch.remove_row(None, row["lineno"], expect=row.get("raw"))
            self.say("✅ 已从 data.csv 删掉第 %d 行：%s" % (row["lineno"], raw.strip()), "ok")
            self.say("   日历里的这个 session 永久不要了（以后同星期几也不会再有）。", "warn")
        elif not relocation:
            self.say("· 这一场不是日历里的规则（计划外 session），只取消这一场本身。", "info")
        if relocation:
            try:
                sch.remove_relocation(self.sel_date, from_hhmm)
                self.say("✅ 已撤销 #%d 的临时挪动（原时间 %s）。" % (idx, from_hhmm), "ok")
            except Exception as e:                     # noqa: BLE001 - 挪动记录坏了也别挡取消
                self.say("⚠ 清理临时挪动记录失败：%s" % e, "warn")

    def _cancel_booked_session(self, idx, row):
        """取消一场已经订上的 session（乐观先拿掉，网络丢后台）。"""
        self.say("#%d 取消中…（%s；乐观：表格先拿掉，Focusmate 在后台）"
                 % (idx, row["startLabel"]), "cmd")
        before_row = dict(row)
        removed = self._forget_api_row(row["id"])
        removed_at = removed[0] if removed else None
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("session.cancel",
                                 "取消 %s %s" % (self.sel_date.isoformat(), row["startLabel"]),
                                 {"date": self.sel_date.isoformat(),
                                  "start": row["startLabel"],
                                  "startMs": row.get("startMs"),
                                  "meeting_id": row["id"]})
        job = self.jobs.submit(action, on_fail=lambda error: self._row_op_failed(
            "#%d 取消 %s" % (idx, row["startLabel"]), before_row, error,
            index=removed_at))
        if self.jobs.sync:
            if job.outcome is not None and job.outcome.ok:
                self.say("✅ #%d %s 已取消（API 复核：该时段已消失）"
                         % (idx, row["startLabel"]), "ok")
                self.refresh(quiet=True)
            return True
        self.say("✅ #%d %s 已取消（Focusmate 复核回来我再说话）"
                 % (idx, row["startLabel"]), "ok")
        self.refresh(quiet=True)
        return True

    # -- 临时挪动（单场 / 整段）----------------------------------------

    def _period_rows(self, day, period_id):
        """这一天属于某个时段的场次（形状和表格里的行一样，能直接拿去挪）。

        以**契约**为准（``schedule.day_slots``，含已经落过的临时挪动），再从表格 /
        API 快照里借出那一半（id / startMs / zone）—— 这样 `relocate T1 15` 既不会
        漏掉刚改过的场次，也不会漏掉「订上了但表格还没重建」的那些。
        """
        by_start = {}
        for row in getattr(self, "rows", []) or []:
            if row.get("date") == day.isoformat():
                by_start.setdefault(row.get("startLabel"), row)
        api_by_start = {}
        for r in getattr(self, "api_rows", []) or []:
            api_by_start.setdefault(r.get("startLabel"), r)
        periods = self.periods_for_day(day)
        now = datetime.datetime.now()
        out = []
        for slot in sch.day_slots(self.entries, day):
            period = sp.period_for_session(periods, slot["start"])
            if not period or period.get("id") != period_id:
                continue
            row = by_start.get(slot["start"])
            if row is None:
                start_dt = sch.slot_datetime(slot, day)
                dur = int(slot["duration"])
                row = {
                    "startMs": int(start_dt.timestamp() * 1000),
                    "endMs": int((start_dt + datetime.timedelta(minutes=dur)
                                  ).timestamp() * 1000),
                    "startLabel": slot["start"],
                    "durationMin": dur,
                    "id": (api_by_start.get(slot["start"]) or {}).get("id"),
                    "zone": self.effective_zone(sch.zone_name(start_dt, now), day),
                    "date": day.isoformat(),
                    "planned": True,
                }
            row = dict(row)
            if slot.get("relocation"):
                row["relocation"] = dict(slot["relocation"])
                row["relocated_from"] = slot.get("relocated_from")
            row["period_id"] = period_id
            out.append(row)
        out.sort(key=lambda r: r.get("startMs") or 0)
        return out

    def _relocate_moves(self, day, rows, delta, allow_past=False):
        """**纯计算**：这批行挪 delta 分钟之后是什么样 + 撞车检查（不改任何文件）。

        返回 ``[{"row","from","cur","to","duration","offset"}]``；有任何问题抛
        ValueError，调用方此时还没有副作用。
        """
        if not rows:
            raise ValueError("没有要挪的场次。")
        moves = []
        for row in rows:
            if not row.get("planned"):
                raise ValueError("计划外 session（data.csv 里没有对应契约），relocate 改不了它。")
            if not allow_past and (row.get("zone") == "past"
                                   or row.get("phase") == "已结束"):
                raise ValueError("%s 那场已经开始/结束了 —— 过去的场次不能挪。"
                                 "（到点还没开始的那场走收尾里的 `p` 推迟。）"
                                 % row.get("startLabel"))
            rec = row.get("relocation") or {}
            from_hhmm = rec.get("from") or row.get("relocated_from") or row["startLabel"]
            cur_hhmm = row["startLabel"]
            dur = row.get("durationMin") or rec.get("duration")
            if not dur:
                raise ValueError("%s 看不出时长（快照？），先 refresh 成功一次再挪。"
                                 % cur_hhmm)
            got = sch.parse_hhmm(cur_hhmm)
            if got is None:
                raise ValueError("当前行时间 %r 不是合法 HH:MM" % cur_hhmm)
            cur_start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
            target = cur_start + datetime.timedelta(minutes=delta)
            if target.date() != day:
                raise ValueError("不能跨天移动：%s %s 挪 %+d 分钟会跑到 %s。"
                                 % (day.isoformat(), cur_hhmm, delta,
                                    target.date().isoformat()))
            moves.append({
                "row": row,
                "from": from_hhmm,
                "cur": cur_hhmm,
                "to": target.strftime("%H:%M"),
                "duration": str(int(dur)),
                "offset": int(rec.get("offset") or 0) + delta,
            })
        self._check_relocate_conflicts(day, moves)
        return moves

    def _check_relocate_conflicts(self, day, moves):
        """撞车检查：挪过去之后不能和**别的**场次重叠。

        单场挪动以前只查「同一时刻」，整段挪动必须查「时间重叠」——
        T1 整段往后 15 分钟时，11:15–12:05 和 12:00 那场就是真撞车（以前只看
        起点不同就放行了）。改成重叠判据之后，单场也一起受益。
        """
        moving_now = set(m["cur"] for m in moves)
        moving_ids = set(m["row"].get("id") for m in moves if m["row"].get("id"))
        targets = {}
        for m in moves:
            if m["to"] in targets:
                raise ValueError("挪完之后 %s 撞车：这一批里有两场被挪到了同一时刻。"
                                 % m["to"])
            targets[m["to"]] = m

        def _span(hhmm, dur):
            got = sch.parse_hhmm(hhmm)
            if got is None:
                return None
            begin = got[0] * 60 + got[1]
            try:
                dur = int(dur or 0)
            except (TypeError, ValueError):
                dur = 0
            return begin, begin + max(1, dur)

        occupied = []
        for slot in sch.day_slots(self.entries, day):
            if slot["start"] in moving_now:
                continue
            span = _span(slot["start"], slot.get("duration"))
            if span:
                occupied.append((span[0], span[1], "%s 那场契约" % slot["start"]))
        for r in getattr(self, "api_rows", []) or []:
            if r.get("id") in moving_ids or r.get("startLabel") in moving_now:
                continue
            span = _span(r.get("startLabel"), r.get("durationMin"))
            if span:
                occupied.append((span[0], span[1], "%s 那场 API session" % r.get("startLabel")))

        for m in moves:
            span = _span(m["to"], m["duration"])
            if span is None:
                raise ValueError("目标时间 %r 不是合法 HH:MM" % m["to"])
            for begin, end, label in occupied:
                if span[0] < end and begin < span[1]:
                    raise ValueError("目标时间 %s 和 %s 重叠；不能撞车。" % (m["to"], label))

    def _migrate_task_bindings(self, day, moves):
        """把挂在这些场次上的任务**跟着挪过去**（绑定 + 截止时间一起动）。

        为什么必须做：任务是用「哪一天 + 哪一刻」认场次的（``tasks_for_session``）。
        session 挪了而绑定留在原地 —— 表格会说「这场一条任务都没有」，收尾也不会
        再问这些任务，它们会静默地丢掉自己那一场（真实踩过的坑）。
        """
        moved = []
        for m in moves:
            try:
                tasks = self.task_store.tasks_for_session(day, m["cur"])
            except Exception:                      # noqa: BLE001 - 绑定挪不动不该挡住挪场次
                tasks = []
            session = {"date": day.isoformat(), "start": m["to"],
                       "duration": int(m["duration"]),
                       "focusmate_session_id": None,
                       "relocated_from": m["from"]}
            for task in tasks:
                if ts.TaskStore.is_repeat_template(task):
                    continue
                try:
                    self.task_store.assign(task["id"], session)
                except (ValueError, TypeError) as e:
                    self.say("⚠ 任务 %s 的绑定没跟着挪：%s"
                             % (self.task_label(task), e), "warn")
                    continue
                moved.append(task)
        if moved:
            self.say("↔ %d 条任务跟着挪到新时间：%s"
                     % (len(moved), "、".join(self.task_label(t) for t in moved)), "ok")
        return moved

    def _apply_relocate(self, day, moves, delta, later_scope=None, reason="relocate",
                        on_done=None):
        """落临时挪动，取消旧 API session；成功后挪任务绑定、再执行 ``on_done``。

        取消失败时把 relocation 文件整批回滚。取消请求现在走后台队列
        （``session.relocate_cancel``），提示符线程不会等浏览器；面板里能看到
        「取消旧 session X」的进度。同步模式（测试 / --exec）仍是当场完成。
        """
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        before = sch.load_relocations()
        after = copy.deepcopy(before)
        recs = after.setdefault("relocations", {})
        for m in moves:
            rec = {
                "kind": "temporary",
                "date": day.isoformat(),
                "from": m["from"],
                "to": m["to"],
                "duration": m["duration"],
                "offset": m["offset"],
                "at": stamp,
            }
            if m.get("scope"):
                rec["scope"] = m["scope"]
            recs[sch.key_of(day, m["from"])] = rec
        if delta > 0:
            last = moves[-1]
            after.setdefault("later_moves", {})[sch.later_move_key(day, later_scope)] = {
                "key": sch.key_of(day, last["from"]),
                "from": last["from"],
                "to": last["to"],
                "scope": later_scope or "",
                "count": len(moves),
                "reason": reason,
                "at": stamp,
            }
        sch.save_relocations(after)

        live = [m for m in moves if m["row"].get("id") is not None]

        def finish_local():
            # 本地表也把旧 session 拿掉：成功取消之后，表格立刻只显示新时间。
            for m in live:
                row = m["row"]
                self.api_rows = [r for r in self.api_rows
                                 if not (r.get("id") == row.get("id")
                                         and r.get("startMs") == row.get("startMs"))]
            # 旧 booked state 清掉：不清的话，等你哪天把挪动撤回去，daemon 会看到
            # 「state 说 booked 但 API 没有」→ 记一笔罪（其实是 warden 自己干的活）。
            self._forget_layout_state(
                [{"date": day.isoformat(), "startLabel": m["cur"]} for m in moves])
            # 任务绑定跟着挪（绑定 + 截止时间）。
            self._migrate_task_bindings(day, moves)
            self.reload_entries()
            self.rebuild_rows()
            self.invalidate()
            if callable(on_done):
                on_done()

        def failed(error):
            try:
                sch.save_relocations(before)
            except Exception as exc:               # noqa: BLE001 - 回滚失败也要说实话
                self.say("⚠ 回滚临时挪动失败：%s" % exc, "warn")
            self.reload_entries()
            self.rebuild_rows()
            self.invalidate()
            self.had_error = True
            self.say("↩ 取消原 session 失败：%s；临时挪动已整批回滚（任务绑定没动）。"
                     % error, "warn")

        if not live:
            finish_local()
            return len(moves)

        action = self.net_action(
            "session.relocate_cancel",
            "取消旧 session 以改排布（%d 场）" % len(live),
            {"moves": [{"start": m["cur"], "startMs": m["row"].get("startMs"),
                        "meeting_id": m["row"].get("id")} for m in live]},
            source=SRC_FOCUSMATE)
        job = self.jobs.submit(action, on_ok=lambda payload: finish_local(),
                               on_fail=failed)
        if self.jobs.sync:
            if job.outcome is None or not job.outcome.ok:
                return False
            return len(moves)
        self.say("已落临时计划；旧 session 的取消在后台跑（进度看面板）。", "cmd")
        return len(moves)

    def _relocate_say(self, day, moves, delta, period_id=None, label=""):
        """挪完之后那一句人话（单场和整段共用）。"""
        moved = "提前" if delta < 0 else "推后"
        what = ("%s 整段 " % period_id) if period_id else (label or "")
        pairs = "、".join("%s→%s" % (m["cur"], m["to"]) for m in moves)
        self.say("✅ %s%s %d 分钟：%s（临时，不改 data.csv；原时间 %s）。"
                 % (what, moved, abs(delta), pairs,
                    "、".join(sorted(set(m["from"] for m in moves)))), "ok")

    def _relocate_period(self, day, period_id, delta, allow_past=False, reason="relocate"):
        """``relocate T1 ±MM`` —— 把一个时段里的**每一场**一起挪（相对位置不变）。"""
        rows = self._period_rows(day, period_id)
        if not rows:
            raise ValueError("%s 在 %s 没有排出来的 session（先 `period %s 50 50` 排一下）。"
                             % (period_id, day.isoformat(), period_id))
        if delta > 0 and sch.get_later_move(day, scope=period_id):
            raise ValueError("%s %s 已经往后挪过一次了；每个时段一天只能往后挪一次。"
                             % (day.isoformat(), period_id))
        moves = self._relocate_moves(day, rows, delta, allow_past=allow_past)
        for m in moves:
            m["scope"] = period_id

        def done():
            self._relocate_say(day, moves, delta, period_id=period_id)
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=period_id,
                                  reason=reason, on_done=done)
        return ok is not False

    def _relocate_after(self, day, moves):
        """挪完之后收尾：重读契约、重画表格，并把 check 放进后台队列。

        check 会开 keep_session 子进程、可能还要浏览器；以前它在提示符线程上
        ``run_keep_session``，一挪场次整个 TUI 就冻住。现在它是一条有进度条的
        后台动作（同时让 bridge 先放开 Chromium profile）。
        """
        self.reload_entries()
        self.rebuild_rows()
        live = [m for m in moves if m["row"].get("zone") != "draft"]
        if not live:
            return True

        def done(payload):
            out = (payload or {}).get("out") or ""
            for line in str(out).splitlines()[-20:]:
                self.say(line)
            self.say("✅ 重排后的 check 跑完。", "ok")
            self.reload_entries()
            self.rebuild_rows()

        def failed(error):
            self.had_error = True
            self.say("❌ 重排后的 check 失败：%s（临时计划已落盘，daemon 后续还会补）"
                     % error, "error")

        action = self.net_action(
            "keep_session.run", "重排后 check（补订/对账）",
            {"argv": ["check"], "timeout": 600, "release_browser": True},
            source=SRC_KEEP)
        job = self.jobs.submit(action, on_ok=done, on_fail=failed)
        if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
            self.reload_entries()
            self.rebuild_rows()
            return False
        self.say("重排后的 check 已排进后台（进度看面板）。", "cmd")
        if not self.offline:
            self.refresh(quiet=True)
        return True

    def cmd_relocate(self, args):
        """relocate N ±MM | relocate T1 ±MM —— 临时挪一场 / 挪一整个时段。

        ``relocate 3 -15``  只有 #3 这一场动。
        ``relocate T1 15``  时段 T1 里**每一场**一起推后 15 分钟：11:00、12:00 两场
                            变成 11:15、12:15，场次之间的相对位置一点不变。

        两者都不写 data.csv：只影响所选这一天的这几场，未来同星期几照旧。
        挪动的**任务绑定跟着走**（``tasks_for_session`` 认的是「哪一天 + 哪一刻」，
        绑定留在原地等于把这些任务从这一场上摘掉）。
        提前随时可以；推后单场是「一天一次」、整段是「每个时段一天一次」，
        而且撞车（重叠）就拒绝 —— 撞车判据对单场也生效了。
        """
        if not args:
            raise ValueError("用法：relocate N ±MM（单场）/ relocate T1 ±MM（整段）"
                             "，MM 是 15 的倍数")
        idx_token, delta = parse_relocate_spec(args)
        if idx_token is None:
            raise ValueError("用法：relocate N ±MM（单场）/ relocate T1 ±MM（整段）")
        day = self.sel_date
        if re.match(r"^[Tt]\d+$", idx_token):
            return self._relocate_period(day, idx_token.upper(), delta)

        idx, row = self.pick(idx_token)
        if delta > 0 and sch.get_later_move(day):
            raise ValueError("%s 已经往后挪过一次了；一天只能往后挪一次。"
                             % day.isoformat())
        moves = self._relocate_moves(day, [row], delta)

        def done():
            self._relocate_say(day, moves, delta, label="#%d" % idx)
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=None,
                                  on_done=done)
        return ok is not False

    # -- 任务系统 ------------------------------------------------------

    def cmd_task(self, args):
        """TaskView 的唯一入口：task <create|delete|modify|assign|select|repeat|sync|archive> ...

        裸任务 ID 就是 select（``task 8`` ≡ ``task select 8``），所以最短的挂法是
        ``task 8 -a 1``。

        任何子命令后面都可以跟这些后缀（都能组合）：
          ``--assign <session ID>``（``-a`` 同义）= 顺手挂到那一场
          ``--note <备注>``（``-n`` 同义）= 顺手写备注（可以塞网址）
          ``--mark complete|discard``（``-m`` 同义）= 手动改本地状态
          ``--from <session ID>``（``-s`` 同义）= 批量：那一场上的**全部**任务
          ``--trigger <预设> [参数...]``（``-t`` 同义）= 挂创建触发（框架；默认空）
          ``--formatter <预设> [参数...]``（``-f`` 同义）= 挂排期算法并当场执行

        ``-m/--mark`` 是**账本动作**，不代替 ``done`` / ``drop`` 的外部联动：
          - ``complete`` = 本地 completed + 归档；不勾 Focusmate，不当场写 OmniFocus
          - ``discard``  = 本地 dropped；完全不动 Focusmate / OmniFocus
        之后的 ``task sync`` / ``refresh --check`` 仍会按单向契约把本地 completed
        写回 OmniFocus；discard 没有自动写回。

        ``-a`` 有两个特殊取值，替代「表格编号」：
          ``none``    = 下架（删掉 Focusmate 那条 task，本地断绑定）→ ``task 8 -an``
          ``current`` = 当前这一场（进行中的优先，其次最近那一场）→ ``task 8 -ac``

            task 8 -a 1
            task create title=写论文 --assign 3
            task create title=写论文 -n "材料在这 https://example.com/x" --assign 3
            task modify 1 due 后天 --note=下午再弄
            task select 1 -n "https://notion.so/xxx"
            task 8 -an                  ← 从它那一场下架（≡ task 8 -a none）
            task 8 -ac                  ← 挂到当前这一场（≡ task 8 -a current）
            task 3 -m complete          ← 只把本地 #3 标成完成（并归档）
            task -m discard 3           ← 只把本地 #3 标成放弃（外头都不动）
            task -t                    ← 列 trigger 预设（默认是空列表）
            task -f                    ← 列 formatter 预设（现在有 end / enlist）
            task 3 -f end              ← 按 duration 排到当天最后一段并自动 assign
            task 3 -e true             ← 这条独占一整场（同一场不能再放第二条任务）
            task create title=写论文 duration=25 -e true -f end
            task create title=闪卡 duration=25 -f end
            task 32 swap 21             ← 两条任务互换位置
            task -s 5 -a 6              ← 把 #5 上的全部任务转移到 #6
            task -s 5 -a none           ← 把 #5 上的全部任务下架
            task -s 5                   ← 只列 #5 上现在挂着谁

        「挂」永远是同一套：先在 Focusmate 建 task 并复核，成功后才落本地
        （见 :meth:`cmd_task_assign`），所以 ``--assign`` 不会产生假成功的半截状态。
        """
        toks = [str(a) for a in args]
        if not toks:
            raise ValueError("用法：task create/delete/modify/note/assign/select/repeat/sync/archive ...")
        # 分工固定成一条规矩：**动作归子命令，后缀归分流**。
        #   task import          → 动作：读剪贴板（不需要任何 flag）
        #   task import -a 3     → 后缀：顺手分流/挂到表格 #3
        #   task 5 link          → 动作：读剪贴板里的链接
        # 所以 ``-a`` 永远只表示 ``--assign``（分流），不兼职别的意思 ——
        # 同一个字母两副面孔，是这个入口最容易踩的坑。
        assign_to, kept = self._split_assign(toks)
        source, kept = self._split_source(kept)
        note_text, kept = self._split_note(kept)
        exclusive, kept = self._split_exclusive(kept)
        mark_status, kept = self._split_mark(kept)
        count_spec, kept = self._split_count(kept)
        trigger_tokens, kept = self._split_hook(kept, "trigger")
        formatter_tokens, kept = self._split_hook(kept, "formatter")

        # `task -t` / `task -f` 后面什么都没跟 = 列预设，不是「挂一个空函数」。
        bare_hooks = [kind for kind, tokens in
                      (("trigger", trigger_tokens), ("formatter", formatter_tokens))
                      if tokens is not None and not tokens]
        if bare_hooks:
            self._list_hooks(bare_hooks)
            return True

        trigger_spec = (th.build_spec("trigger", trigger_tokens)
                        if trigger_tokens else None)
        formatter_spec = (th.build_spec("formatter", formatter_tokens)
                          if formatter_tokens else None)

        if source is not None:
            if mark_status is not None:
                raise ValueError("`-m/--mark` 是单条任务的状态标记，不能和 `-s/--from` 批量一起用；"
                                 "先切到那天再逐条标。")
            if count_spec is not None:
                raise ValueError("`-c/--count` 是单条任务的进度，不能和 `-s/--from` 批量一起用；"
                                 "先切到那天再逐条加。")
            if trigger_spec is not None or formatter_spec is not None:
                raise ValueError("`-t/--trigger`、`-f/--formatter` 都是单条任务的钩子，"
                                 "不能和 `-s/--from` 批量一起用；先切到那天再逐条挂。")
            # `-s` 本身就是动作（批量），后面不该再有子命令：``task -s 5 -a 6``。
            if kept:
                raise ValueError("`-s/--from` 是批量后缀，后面不要再跟动作（%s）；"
                                 "写成 `task -s 5 -a 6` 这样。" % kept[0])
            return self._apply_bulk(source, assign_to)
        if not kept and mark_status is None and trigger_spec is None \
                and formatter_spec is None and exclusive is None \
                and count_spec is None:
            raise ValueError("task 后面要跟动作（create/delete/modify/assign/select/repeat/sync）"
                             "或后缀 -e/--exclusive / -m/--mark / -c/--count"
                             " / -t/--trigger / -f/--formatter")
        # 异步 action（import 这类要先读外面世界的）拿走**同一份**后缀，
        # 后台那半件事落地之后自己套 —— 和同步路径完全一样的那套 _apply_suffix_chain。
        self._import_suffix_doc = {
            "assign_to": assign_to,
            "note_text": note_text,
            "exclusive": exclusive,
            "mark_status": mark_status,
            "count_spec": count_spec,
            "trigger_spec": trigger_spec,
            "formatter_spec": formatter_spec,
        }
        if kept:
            action = kept[0].lower()
            rest = kept[1:]
            try:
                result = self._dispatch_task(action, rest)
            finally:
                # 这份后缀只借给**这一次**分派用：直接调 cmd_task_import 的地方
                # （测试、内部调用）不该被上一次命令的后缀污染。
                self._import_suffix_doc = None
        else:
            # `task -m complete` / `task -f end`：作用于当前选中的任务。
            action = "hook" if (trigger_spec or formatter_spec) and mark_status is None else "mark"
            result = None
        if result is NET_PENDING:
            # 已经排进后台队列了：后缀跟着 Action 的参数走，不在这儿套
            return None
        if result is IMPORT_PENDING:
            # 重复任务导入在等 y/n/q，**还没有**建出任务。这时 -a / -n / -m /
            # -t / -f 绝不能落到「上一次选中的任务」上（用户反馈：task import
            # -ac 的 -ac 不生效，反而把旧任务挂到了当前这一场）。先存起来，
            # 等 _answer_pending_import() 真正建完再按同样顺序套上去。
            pending = self.pending_import or {}
            pending["suffix"] = {
                "assign_to": assign_to,
                "note_text": note_text,
                "exclusive": exclusive,
                "mark_status": mark_status,
                "count_spec": count_spec,
                "trigger_spec": trigger_spec,
                "formatter_spec": formatter_spec,
            }
            self.pending_import = pending
            return None
        if isinstance(result, ImportBatch):
            # 一次导入多条：后缀按条套一遍（`-a 3` = 整批都挂到 #3，
            # `-f end` = 五条各自排进当天最后一段），最后那个当选中项。
            for one in result:
                self._apply_suffix_chain(
                    one, action, note_text=note_text, assign_to=assign_to,
                    exclusive=exclusive, mark_status=mark_status,
                    count_spec=count_spec, trigger_spec=trigger_spec,
                    formatter_spec=formatter_spec)
            return result.last
        if note_text is not None:
            self._apply_note(result, note_text, action)
        if count_spec is not None:
            result = self._apply_count(result, count_spec, action) or result
        if exclusive is not None:
            result = self._apply_exclusive(result, exclusive, action) or result
        if assign_to is not None:
            self._apply_assign(result, assign_to, action)
        if mark_status is not None:
            result = self._apply_mark(result, mark_status, action)
        if trigger_spec is not None:
            result = self._apply_trigger(result, trigger_spec, action)
        if formatter_spec is not None:
            result = self._apply_formatter(result, formatter_spec, action)
        return result

    @staticmethod
    def _split_assign(toks):
        """把 ``--assign N`` / ``--assign=N`` / ``-a N`` 从参数里摘出来（分流后缀）。"""
        assign_to = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--assign", "-a"):
                if i + 1 >= len(toks):
                    raise ValueError("--assign 后面要跟表格里的 session ID（例如 --assign 3）")
                assign_to = toks[i + 1]
                i += 2
                continue
            if low.startswith("--assign="):
                assign_to = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return assign_to, kept

    @staticmethod
    def _split_source(toks):
        """把 ``--from N`` / ``--from=N`` / ``-s N`` 从参数里摘出来（批量后缀）。

        ``-s`` 说的是**源 session**：它上面挂着的全部任务一起办事。和 ``-a``
        一起用就是「把这一场上的任务搬到那一场」：

            task -s 5 -a 6      把 #5 的全部任务转到 #6
            task -s 5 -a none   把 #5 的全部任务下架
            task -s 5           只列 #5 上现在挂着谁（什么都不动）
        """
        source = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = str(tok).lower()
            if low in ("--from", "-s"):
                if i + 1 >= len(toks):
                    raise ValueError("--from 后面要跟表格里的 session ID（例如 -s 5）")
                source = toks[i + 1]
                i += 2
                continue
            if low.startswith("--from="):
                source = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return source, kept

    @staticmethod
    def _split_note(toks):
        """把 ``--note 文本`` / ``-n 文本`` / ``--note=文本`` 从参数里摘出来。

        和 ``--assign`` 一样是「任何 task 子命令都能带」的后缀：
        命令跑完拿到任务之后顺手写备注。
        备注是自由文本，**一路吃到下一个 ``--flag`` 为止**，所以不用加引号：

            task create title=写论文 --note 材料在这 https://example.com/x --assign 3

        ``--note=文本`` / ``--note=``（等号后为空 = 清空）也行；
        备注本身以 ``-`` 开头时用等号写法。
        """
        note = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--note", "-n"):
                # 备注是自由文本：一路吃到下一个 --flag 为止，所以
                # `--note 材料在这 https://x` 不用加引号。
                j = i + 1
                parts = []
                while j < len(toks) and not str(toks[j]).startswith("-"):
                    parts.append(str(toks[j]))
                    j += 1
                if not parts:
                    raise ValueError("--note 后面要跟备注文本（例如 --note https://…）")
                note = " ".join(parts)
                i = j
                continue
            if low.startswith("--note="):
                note = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        return note, kept

    @staticmethod
    def _split_exclusive(toks):
        """把 ``--exclusive true`` / ``-e true`` / ``-e=false`` 摘出来。

        独占 = **这一场 session 只能有它一条任务**。和 ``-a`` / ``-n`` 一样是后缀，
        位置随便：

            task 3 -e true              # 只改这一条的账本字段
            task create title=写论文 duration=25 -e true
            task 3 -f end               # formatter end 排出来的任务**自动**独占
            task 3 -e false             # 取消独占（这一场又可以放第二条了）

        裸写 ``-e``（后面不是 true/false）就是 true：``task 3 -e`` ≡ ``task 3 -e true``。
        """
        value = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--exclusive", "-e"):
                nxt = str(toks[i + 1]).strip().lower() if i + 1 < len(toks) else ""
                got = _task_bool_word(nxt)
                if got is None:
                    value = True                 # 裸写 -e：就是「独占」
                    i += 1
                else:
                    value = got
                    i += 2
                continue
            if low.startswith("--exclusive=") or low.startswith("-e="):
                word = tok.split("=", 1)[1].strip().lower()
                got = _task_bool_word(word)
                if got is None:
                    raise ValueError("`-e/--exclusive` 只认 true / false；你给的是 %r"
                                     % tok.split("=", 1)[1])
                value = got
                i += 1
                continue
            kept.append(tok)
            i += 1
        return value, kept

    @staticmethod
    def _split_mark(toks):
        """把 ``--mark complete`` / ``--mark=complete`` / ``-m discard`` 摘出来。

        和 ``-a`` / ``-n`` 一样是后缀，位置随便：``task 3 -m complete`` 和
        ``task -m complete 3`` 等价。``-m`` 只认两个值：

          ``complete`` = 本地 completed + 归档（不勾 Focusmate、不写 OmniFocus）
          ``discard``  = 本地 dropped（完全不动外部）
        """
        status = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--mark", "-m"):
                if i + 1 >= len(toks):
                    raise ValueError("--mark 后面要跟 complete 或 discard"
                                     "（例如 task 3 -m complete）")
                value = str(toks[i + 1])
                i += 2
            elif low.startswith("--mark=") or low.startswith("-m="):
                value = tok.split("=", 1)[1]
                i += 1
            else:
                kept.append(tok)
                i += 1
                continue
            want = TASK_MARK_STATES.get(value.strip().lower())
            if want is None:
                raise ValueError("`-m/--mark` 只认 complete 或 discard；你给的是 %r" % value)
            if status is not None and status != want:
                raise ValueError("`-m/--mark` 一次只能给一个状态（complete / discard）。")
            status = want
        return status, kept

    @staticmethod
    def _split_count(toks):
        """把 ``--count +2`` / ``-c 5`` / ``-c=+2`` 摘出来（count 后缀）。

        三种写法（用户需求原话）：

            task 3 -c 5     设定最高 count = 5（duration 跟着 = 125 分钟）
            task 3 -c +2    进度 +2；加完够数 = 直接完成任务（不会超过最高 count）
            task 3 -c -1    进度 -1（最低 0）

        ``+``/``-`` 写在数值里，所以 ``-c -1`` 里的 ``-1`` 是**值**不是 flag ——
        取值时按位置拿下一个 token，不看它长什么样。
        """
        spec = None
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in ("--count", "-c"):
                if i + 1 >= len(toks):
                    raise ValueError("`-c/--count` 后面要跟一个数（5 / +2 / -1）；"
                                     "例如 task 3 -c +2")
                value = str(toks[i + 1]).strip()
                i += 2
            elif low.startswith("--count=") or low.startswith("-c="):
                value = tok.split("=", 1)[1].strip()
                i += 1
            else:
                kept.append(tok)
                i += 1
                continue
            if spec is not None:
                raise ValueError("`-c/--count` 一次只能给一个（要么设定、要么加减）。")
            if not re.match(r"^[+-]?\d+$", value):
                raise ValueError("`-c/--count` 只认整数；`+2` 是加、`-1` 是减、`5` 是设定。"
                                 "你给的是 %r" % value)
            spec = value
        return spec, kept

    def _apply_count(self, task, spec, action="count"):
        """把 ``-c`` 落到任务上：设定最高 count，或者推进/退回已投入的 count。

        「加完正好/超过最高进度 = 等同于完成任务」（用户需求），所以够数的时候
        走的是**和 ``task done`` 完全一样**的那条路：本地完成 + 归档 + 勾 Focusmate
        + 写回 OmniFocus。不在这里另开一套「悄悄标完成」的岔路。
        """
        if spec is None or not isinstance(task, dict) or task.get("id") is None:
            return task
        text = str(spec).strip()
        if text.startswith("+") or text.startswith("-"):
            step = int(text)
            out = self.task_store.add_count(task["id"], step)
            task = out["task"]
            now_text = self._count_progress_text(out)
            if step == 0:
                self.say("任务 %s 的 count 没动（%s）：加 0 什么都不会发生。"
                         % (self.task_label(task), now_text), "info")
                return task
            if out.get("maxed"):
                self.say("🎯 任务 %s 的 count 到位了（%s）—— 等同完成任务，走 done 那一套。"
                         % (self.task_label(task), now_text), "ok")
                return self._finish_task_with_count(task)
            verb = "加" if step > 0 else "减"
            self.say("✅ 任务 %s %s了 %d 个 count：%s%s"
                     % (self.task_label(task), verb, abs(step), now_text,
                        "（已经顶到最高 count，不会超过）" if out.get("capped") else ""),
                     "ok")
            return task
        # 纯数字 = 设定最高 count（= 同时设定时长）
        task, before, after = self.task_store.set_count(task["id"], text)
        if after is None:
            self.say("✅ 任务 %s 的最高 count 已取消（duration 没动）。"
                     % self.task_label(task), "ok")
            return task
        self.say("✅ 任务 %s 的最高 count = %d（= %d 分钟）%s"
                 % (self.task_label(task), after,
                    ts.count_to_minutes(after) or 0,
                    ("，原来是 %d" % before) if before else ""), "ok")
        return task

    @staticmethod
    def _count_progress_text(out):
        """``(1+2/5)`` 那种进度的纯文字版：``2/5（+2）``。"""
        cap = out.get("cap")
        before, after = out.get("before") or 0, out.get("after") or 0
        if cap is None:
            return "已投入 %d 个 count（没有上限）" % after
        return "%d/%d（%s%d）" % (after, cap, "+" if after >= before else "",
                                 after - before)

    def _finish_task_with_count(self, task):
        """count 够数 = 完成任务：和 ``task done`` 走同一条路（含外部联动）。"""
        return self._complete_task(task, why="count-done")

    @staticmethod
    def _split_hook(toks, kind):
        """把 ``--formatter end [参数...]`` / ``-f end`` 从参数里摘出来。

        返回 ``(tokens, kept)``：

        * 没出现这个 flag → ``(None, kept)``；
        * 出现了但后面没有词 → ``([], kept)`` —— 调用方据此**列预设**；
        * 出现了且后面有词 → ``(["end", ...], kept)``。

        参数一路吃到下一个 ``-`` 开头的 flag 为止，所以
        ``task 3 -f end -a 2`` 里 ``-a 2`` 仍然归 ``--assign``。
        预设名字/参数不能以 ``-`` 开头（key=value 的参数值可以）。
        """
        flags = TASK_HOOK_FLAGS.get(kind)
        if not flags:
            raise ValueError("未知钩子类型 %r" % (kind,))
        found = False
        parts = []
        kept = []
        i = 0
        while i < len(toks):
            tok = str(toks[i])
            low = tok.lower()
            if low in flags or low.startswith(flags[0] + "=") or low.startswith(flags[1] + "="):
                if found:
                    raise ValueError("`%s` 一次只能给一个（%s）；不能同时挂两个 %s。"
                                     % (flags[0], " / ".join(flags), kind))
                found = True
                if "=" in tok:
                    # --formatter=end / -f=end：第一个词在等号右边。
                    first = tok.split("=", 1)[1]
                    if first:
                        parts.append(first)
                    i += 1
                else:
                    i += 1
                while i < len(toks) and not str(toks[i]).startswith("-"):
                    parts.append(str(toks[i]))
                    i += 1
                continue
            kept.append(tok)
            i += 1
        return (parts if found else None), kept

    def _list_hooks(self, kinds):
        """``task -t`` / ``task -f``：把预设函数列出来（trigger 默认是空列表）。"""
        for kind in kinds:
            rows = th.describe_presets(kind)
            label = TASK_HOOK_LABELS.get(kind, kind)
            if not rows:
                self.say("%s：0 个（空列表 —— 框架已就位，还没有注册任何函数）" % label,
                         "info")
                continue
            self.say("%s：%d 个" % (label, len(rows)), "info")
            for name, params, help_text in rows:
                tail = ("（参数：%s）" % "、".join(params)) if params else "（无参数）"
                self.say("   %s %s%s" % (name, tail, (" —— " + help_text) if help_text else ""),
                         "info")

    def _hook_task(self, result, kind, action):
        """给 ``-t`` / ``-f`` 找任务：刚产出的优先，其次当前选中的。"""
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-%s/--%s` 要跟一条任务：`task 3 -%s ...`，"
                             "或先 `task select 3`。"
                             % (kind[0], kind, kind[0]))
        return task

    def _apply_trigger(self, result, spec, action):
        """``-t/--trigger`` 的收尾：把 preset 记在任务上（**不执行**）。

        trigger 的执行属于未来的 runner；这里只负责「挂上去、看得见、可校验」。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-t/--trigger` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = self._hook_task(result, "trigger", action)
        task = self.task_store.set_hook(task["id"], "trigger", spec)
        params = " ".join(spec.get("params") or ())
        self.say("✅ 任务 %s「%s」已挂 trigger %s%s（只记本地；runner 还没实现）"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    spec.get("name"), (" " + params) if params else ""), "ok")
        self.selected_task_id = task["id"]
        return task

    def _formatter_session_has_actual(self, day, session):
        """这一天这一场在 API 里有没有真实 id（权威模式下才算数）。"""
        if not self.authoritative:
            return True
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        start = str((session or {}).get("start") or "").strip()
        if not start:
            return False
        try:
            for row in self._snapshot_rows(iso):
                if (row.get("id")
                        and str(row.get("startLabel") or "") == start):
                    return True
        except Exception:                     # noqa: BLE001 - 行坏了就按没有处理
            return False
        return False

    def _formatter_bound_session_missing(self, task, day):
        """formatter 任务绑的那场**已经开始或过去**，但 API 里根本没有它。

        未来还没订上的场次允许先挂本地；但已经开始/结束的场次如果没有真实
        session id，就说明它被取消了 —— 任务不能继续假装还挂在那里。
        """
        if not self.authoritative:
            return False
        sess = (task or {}).get("session") or {}
        if not sess:
            return False
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if str(sess.get("date") or "") != iso:
            return False
        start = self._session_start_datetime(sess)
        if start is None or start > datetime.datetime.now():
            return False
        sid = str(sess.get("focusmate_session_id") or "").strip()
        if sid:
            try:
                if any(str(r.get("id") or "") == sid for r in self.rows):
                    return False
            except Exception:                 # noqa: BLE001
                pass
        return not self._formatter_session_has_actual(day, sess)

    def _formatter_sessions(self, day, strict_actual=False):
        """formatter 的候选 session：先走日历下载层 + layout，再回退到 data.csv。

        ``strict_actual=True`` 是 refresh 流水线：权威模式下，**今天已经开始/
        过去、但 API 里没有实际 id 的本地计划场**不再是候选。否则一场被取消的
        21:00 session 永远躺在 data.csv 里，formatter 每轮都把它当目标重新挂
        任务，任务就永远“还挂在取消的场上”。手动 `task -f` 仍允许排，
        由调用方决定是否接受本地未订上的未来/过去窗。
        """
        sessions = []
        try:
            sessions = [dict(s) for s in self.sessions_for_day_plan(day)]
        except Exception:                     # noqa: BLE001 - 下载层坏了不该挡住 csv
            sessions = []
        if sessions:
            if strict_actual and self.authoritative and day <= datetime.date.today():
                now = datetime.datetime.now()
                sessions = [
                    sess for sess in sessions
                    if not (self._session_start_datetime(sess) is not None
                            and self._session_start_datetime(sess) <= now
                            and not self._formatter_session_has_actual(day, sess))
                ]
            return sessions
        try:
            self.reload_entries()
            for slot in sch.day_slots(self.entries, day):
                start = slot["start"]
                duration = int(slot["duration"])
                hour, minute = map(int, start.split(":"))
                total = hour * 60 + minute + duration
                sessions.append({
                    "date": day.isoformat(), "start": start,
                    "end": "%02d:%02d" % ((total // 60) % 24, total % 60),
                    "start_min": hour * 60 + minute, "end_min": total,
                    "duration": duration, "period_id": None,
                })
        except Exception:                     # noqa: BLE001 - csv 也坏了就没有候选
            sessions = []
        return sessions

    def _exclusive_blockers(self, date_iso, start, task=None):
        """这一场里「不许再塞人」的任务；没有就是空列表。

        规则只有一条：**一场 session 里只要有独占任务，就别再往里放第二条** ——
        不管是把独占任务放进已经有人的场，还是往有独占任务的场里再塞一条。
        """
        if not date_iso or not start:
            return []
        others = [t for t in self.task_store.tasks_for_session(date_iso, start)
                  if task is None or t.get("id") != task.get("id")]
        others = [t for t in others if self.task_store.is_open(t)]
        if not others:
            return []
        exclusive_others = [t for t in others if th.is_exclusive(t)]
        if exclusive_others:
            return exclusive_others
        return others if th.is_exclusive(task or {}) else []

    def _exclusive_guard(self, task, date_iso, start):
        """挂之前先看这一场：有独占任务就别再塞第二条（抛 ValueError 给人话）。

        ``_exclusive_blockers`` 只说「谁挡着」，这里负责把它翻译成用户能照着做的
        一句话 —— 换一场，或者先 ``task <序号> -e false``。
        """
        blockers = self._exclusive_blockers(date_iso, start, task)
        if not blockers:
            return
        names = "、".join(
            "%s「%s」" % (self.task_label(t),
                         self.task_store.focusmate_title(t, 20) or "?")
            for t in blockers)
        if th.is_exclusive(task or {}):
            raise ValueError(
                "任务 %s 是 🔒 独占的，而 %s %s 这一场已经有 %s 了 —— 一场只能有一条任务。"
                "换一场，或先 `task %s -e false` 放开独占。"
                % (self.task_label(task), date_iso, start, names,
                   self.task_label(task)))
        raise ValueError(
            "%s %s 这一场已经有 🔒 独占任务 %s —— 一场只能有一条任务。"
            "换一场，或先 `task <序号> -e false` 放开那条的独占。"
            % (date_iso, start, names))

    def _ensure_carved_session(self, day, chosen, task=None):
        """把 formatter **切出来的那一场**作为一次性 session 写进当天排期。

        这是**兜底**路径：正常的切割走 :meth:`_cut_period_for_carve`（直接改写那个
        时段的 layout，连旧场一起取消掉）。只有排布表达不了的情况才落到这里 ——
        切点和「前半截打包完的终点」之间隔着 5/20 分钟（Focusmate 只认 15 分钟
        整点，填不上），例如 16:45–18:00 的时段要切 17:30 那场。

        这一场必须真的落进 ``temp/session-extras.json`` + data.csv：否则任务挂不上
        （没有 session id）、daemon 也不会去订它，那段时间就白白空着。

        返回 None（没造/不需要）或一句话（造了什么）。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        start = str(chosen.get("start") or "").strip()
        duration = int(chosen.get("duration") or 0)
        if not start or duration <= 0:
            return None
        already = [s for s in self.sessions_for_day_plan(day) if s.get("start") == start]
        if already:
            return None                        # 同起点那场已经有了，复用，不重复造
        note = "formatter end 切的（任务 %s）" % (
            self.task_label(task) if task is not None else "?")
        try:
            sp.add_extra(day, start, duration, note=note)
        except ValueError as exc:
            self.say("⚠ 没能把切出来的 %s %dmin 写进当天排期：%s（任务照样挂，"
                     "只是这一场没有计划）" % (start, duration, exc), "warn")
            return None
        if self.rewrite_data_csv_from_plan():
            self.say("➕ 当天额外排了一场：%s %s %dmin（切出来的，已写进 data.csv）"
                     % (day.isoformat(), start, duration), "ok")
        else:
            self.reload_entries()
        return "切出来的 %s %dmin 这一场已经写进当天排期" % (start, duration)

    def _now_dt(self):
        """当前时间；测试可把 ``clock_now`` 钉死，生产环境跟墙上时钟。"""
        return self.clock_now or datetime.datetime.now()

    @staticmethod
    def _session_start_datetime(row):
        """从一行 session（API / 本地绑定 / 快照）里取**实际开始时间**。

        优先用 API 的 ``startMs`` —— 这是 Focusmate 真实约定的开始时刻；
        计划里的 ``startLabel`` 可能和它差几分钟（用户说的「时间偏差」）。
        只有没有 ``startMs`` 时才退回 ``date + startLabel/start``。
        无法解析返回 None（调用方按「已开始」保守处理）。
        """
        if not isinstance(row, dict):
            return None
        raw_ms = row.get("startMs")
        if raw_ms not in (None, ""):
            try:
                ms = float(raw_ms)
                # 1 / 1234 这类占位值不是真实 epoch 毫秒（1970 年），退回
                # date + startLabel；真实 Focusmate API 的 startMs 一定远大于此。
                if ms >= 946684800000:            # 2000-01-01T00:00:00Z
                    return datetime.datetime.fromtimestamp(ms / 1000.0)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        raw_day = row.get("date") or row.get("period_date")
        raw_hhmm = row.get("startLabel") or row.get("start")
        if raw_day in (None, "") or raw_hhmm in (None, ""):
            return None
        try:
            if hasattr(raw_day, "year"):
                day = datetime.date(raw_day.year, raw_day.month, raw_day.day)
            else:
                day = datetime.date.fromisoformat(str(raw_day)[:10])
            got = sch.parse_hhmm(str(raw_hhmm))
            if not got:
                return None
            return datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        except (TypeError, ValueError):
            return None

    @classmethod
    def _session_already_started(cls, row, now=None):
        """这一场是否已经开始（含刚好到点）；解析不了也按已开始保护。"""
        now = now or datetime.datetime.now()
        start = cls._session_start_datetime(row)
        return start is None or start <= now

    def _split_started_sessions(self, rows):
        """把待处理场次拆成 (未开始可取消, 已开始必须保护)。

        **refresh / formatter / 改排布都不得取消已经开始或过去的 session。**
        判定按 API 实际 ``startMs``，不是按表格的 zone / 计划 startLabel；
        这样即使当前 session 因时间偏差被显示成 locked/draft，也不会误杀。
        """
        now = self._now_dt()
        protected, safe = [], []
        for row in rows or []:
            (protected if self._session_already_started(row, now) else safe).append(row)
        if protected:
            labels = "、".join(
                str(row.get("startLabel") or row.get("start") or "?")
                for row in protected[:5])
            if len(protected) > 5:
                labels += " 等"
            self.say("⚠ %d 场 session 已经开始或已过去（%s）；refresh / 自动排布"
                     "不会取消它们（按 API 实际 startMs 判定，不看计划时间偏差）。"
                     % (len(protected), labels), "warn")
        return safe, protected

    def _cancel_stale_sessions_for(self, day, period, new_sessions,
                                  reason="formatter-cut"):
        """计划里不再包含的**已订上**场次 → 真的去 Focusmate 取消。返回取消掉的行。

        formatter 切出来的场次多半落在未来（草稿区，还没订上），但也有今天就切这种
        已经订好的 —— 那就得取消，否则 50 分钟的旧场会跟新的 25 分钟并存（用户原话：
        「实际上就是取消 + 约定一个 25 分钟的」）。

        和 `cmd_period_layout` 那条路同一套顺序：**先**给 daemon 挂 ``expect_cancel``
        单，再取消，再清 state —— 否则 daemon 下一轮会把 warden 自己干的活记成
        「用户手动取消」的罪，全局锁死 TUI。
        """
        if self.offline:
            return []
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        keep = set()
        for sess in new_sessions or []:
            try:
                keep.add((str(sess.get("start")), int(sess.get("duration"))))
            except (TypeError, ValueError):
                continue
        stale = []
        for row in self._snapshot_rows(iso):
            start = row.get("startLabel")
            if not start or not row.get("id"):
                continue
            if sp.period_for_session([period], start) is None:
                continue
            try:
                dur = int(row.get("durationMin"))
            except (TypeError, ValueError):
                dur = None
            if (str(start), dur) in keep:
                continue
            stale.append(row)
        stale, _protected = self._split_started_sessions(stale)
        if not stale:
            return []
        try:
            keys = [sch.key_of(day, row.get("startLabel")) for row in stale]
        except (ValueError, TypeError):
            keys = []
        if keys:
            ks.expect_cancel(sorted(keys), reason="%s:%s %s"
                             % (reason, period.get("id"), iso))
        cancelled = []
        try:
            for row in stale:
                self.say("取消已经订上的旧场 %s–%s（%smin）：切割之后计划里不再包含它…"
                         % (row.get("startLabel"), row.get("endLabel") or "?",
                            row.get("durationMin") or "?"), "cmd")
                # **乐观 + 后台**：先把它从本地表格拿掉，取消请求丢给单 worker。
                # 这里以前直接 bridge.call("cancel")，formatter 切场时会在提示符
                # 线程上等浏览器几十秒 —— 和「外部操作全异步」的约定不符。
                before_index, _ = self._forget_api_row(row.get("id")) or (None, None)
                action = self.net_action(
                    "session.cancel",
                    "取消旧场 %s %s" % (iso, row.get("startLabel") or "?"),
                    {"startMs": row.get("startMs"), "meeting_id": row.get("id"),
                     "date": iso, "start": row.get("startLabel")})
                job = self.jobs.submit(
                    action,
                    on_fail=lambda error, _row=dict(row), _idx=before_index:
                    self._row_op_failed("旧场 %s 取消" % _row.get("startLabel"),
                                        _row, error, index=_idx))
                if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
                    # 同步模式回调已经回滚；这一轮别再把它算成取消成功。
                    self.had_error = True
                    continue
                cancelled.append(row)
                self.say("✅ %s 旧场 %s %s"
                         % ("已取消" if self.jobs.sync else "已排进后台取消",
                            row.get("startLabel"), "" if self.jobs.sync else "（失败会自动放回）"),
                         "ok")
            if cancelled and not self.jobs.sync:
                self.rebuild_rows()
        finally:
            self._forget_layout_state(cancelled)
            if cancelled and iso == self.sel_date.isoformat():
                gone = {r.get("id") for r in cancelled}
                self.api_rows = [r for r in self.api_rows
                                 if r.get("id") not in gone]
        return cancelled

    def _apply_cut_layout(self, day, period, durations):
        """按 formatter 算出来的时长序列重排这个时段（可能不是选中的那天）。

        顺序照抄 `cmd_period_layout`：先取消计划里不再包含的旧 API session，再写
        layout，再清掉动过的场次上的任务绑定，最后重算 data.csv —— 反过来的话
        daemon 会在缝隙里把旧的 50 分钟场订回来。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        period_id = period.get("id")
        new_sessions = sp.plan_period(period, durations or sp.default_durations(period))
        cancelled = self._cancel_stale_sessions_for(day, period, new_sessions)
        if durations:
            sp.set_layout(day, period_id, durations)
        else:
            sp.remove_layout(day, period_id)
        dropped = self.clear_task_bindings_for_layout(period, new_sessions)
        self.reload_periods()
        self.rewrite_data_csv_from_plan()
        self.reload_entries()
        body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                         for s in new_sessions) or "（这个时段当天不排了）"
        self.say("✂ %s %s 的排布剪断成：%s —— 原来那场不再存在"
                 % (iso, period_id, body), "ok")
        if cancelled:
            self.say("   %s %d 场旧 session（计划里不再包含它们）。"
                     % ("已在 Focusmate 取消" if self.jobs.sync
                        else "已排进后台取消",
                        len(cancelled)), "info")
        if dropped:
            self.say("   有 %d 条任务因为那几场没了而下架（任务本身还在，"
                     "`task <序号> -f end` 可以重新排）。" % len(dropped), "warn")
        return body

    def _cut_period_for_carve(self, day, chosen, task=None):
        """把 `end` 切出来的那一场**钉进当天排期**：原来那场剪断，只留新的一两场。

        用户原话：「切出来的 11:30 那场应该导致原来那场 11:00–11:50 的剪断（实际上
        就是取消 + 约定一个 25 分钟的）…… 原来的 11:00–11:50 不显示，仅显示新的两场。」

        所以这里不玩「两场并存 + 警告」，直接把那个时段的**当天排布**改成
        「前半截 + 切出来的这一场」：

            11:00–12:00 的时段、原来一场 50 分钟、任务 25 分钟
            → 11:00–11:25（空着）+ 11:30–11:55（这一场）

        排布表达不了时（例如前半截打完包，最后一场落在 11:25，而切割点是 11:30
        —— Focusmate 只认 15 分钟整点，中间那 5 分钟没法用一场 session 填），就
        只把前半截写进 layout，切出来的那场单独作为**一次性 session** 补上。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        period_id = chosen.get("cut_period_id")
        start = str(chosen.get("start") or "").strip()
        fit = int(chosen.get("duration") or 0)
        prefix = [int(d) for d in (chosen.get("cut_prefix") or [])]
        period = None
        for probe in self.periods_for_day(day):
            if probe.get("id") == period_id:
                period = probe
                break
        if period is None or not start or fit <= 0:
            # 没有时段信息（比如 data.csv 手写模式）：退回「只补一场」的老路。
            return self._ensure_carved_session(day, chosen, task=task)

        try:
            planned = sp.plan_period(period, prefix + [fit])
        except ValueError:
            planned = []
        exact = bool(planned) and str(planned[-1]["start"]) == start
        durations = (prefix + [fit]) if exact else prefix

        current = sp.get_layout(self.layouts, day, period_id)
        if current is None:
            current = sp.default_durations(period)
        current = [int(d) for d in (current or [])]
        if [int(d) for d in durations] != current:
            self._apply_cut_layout(day, period, durations)
        if exact:
            # 以前“补一场”的一次性 session 现在已经被正式 layout 覆盖，
            # 必须删掉；否则同一时刻会同时存在 layout 场和 extra 场。
            removed = 0
            for sess in planned:
                try:
                    if sp.remove_extra(day, sess.get("start")):
                        removed += 1
                except Exception:              # noqa: BLE001 - 坏 extra 不该挡住切割
                    pass
            if removed:
                self.rewrite_data_csv_from_plan()
                self.reload_periods()
                self.reload_entries()
            body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                             for s in planned)
            return "时段 %s 已经剪断成 %s" % (period_id, body)
        return (self._ensure_carved_session(day, chosen, task=task)
                or "时段 %s 的前半截留在 %s" % (period_id, iso))

    # -- refresh 排期流水线：formatter 先占位，项目 plan 后校验 -------------

    @staticmethod
    def _formatter_dates_of(task):
        """一条 formatter 任务所有可能的落点日期（reservation/session/repeat_date）。"""
        fmt = (task or {}).get("formatter") or {}
        reservation = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        out = set()
        for raw in (reservation.get("date"), sess.get("date"),
                    (task or {}).get("repeat_date")):
            text = str(raw or "").strip()
            if text:
                out.add(text)
        return out

    @staticmethod
    def _formatter_date_of(task):
        """兼容旧接口：返回第一个已知落点日期；没有就 None。"""
        dates = WardenShell._formatter_dates_of(task)
        return sorted(dates)[0] if dates else None

    def formatter_tasks_for_day(self, day=None):
        """``day`` 上需要跑 formatter 的 active 任务（稳定按 id 排序）。"""
        day = day or self.sel_date
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        out = []
        now = datetime.datetime.now()
        for task in self.task_store.list():
            if not ts.TaskStore.is_open(task):
                continue
            if ts.TaskStore.is_repeat_template(task):
                continue
            spec = task.get("formatter") or {}
            if not spec.get("name"):
                continue
            if ts.task_is_deferred(task, now=now):
                # defer 还没到：不进这一轮排期队列，也不动它旧的位置。
                continue
            # occurrence 子任务只属于它自己的 repeat_date，绝不能被 formatter
            # 的旧 reservation / 全局「今天」拽到别的日子重排。否则前几天的
            # 未收尾子任务会在每次 refresh 时全部涌到今天、互相抢 session。
            if ts.TaskStore.is_repeat_occurrence(task):
                own_days = set()
                raw_repeat_day = str(task.get("repeat_date") or "").strip()
                if raw_repeat_day:
                    own_days.add(raw_repeat_day)
            else:
                own_days = self._formatter_dates_of(task)
            # 只有「还没有任何落点日期」的新 formatter 任务，才允许按当前
            # 选中的那天试排。已经有 reservation / session 的任务只看自己的
            # 落点日期；不再无条件把 sel_date 塞进 own_days。
            if own_days and iso not in own_days:
                continue
            if not own_days:
                explicit_day = str((spec.get("args") or {}).get("date")
                                   or (spec.get("args") or {}).get("day") or "").strip()
                if explicit_day:
                    try:
                        if th.resolve_target_day(
                                self.sel_date, now, spec.get("args") or {},
                                allow_today=True).isoformat() != iso:
                            continue
                    except Exception:          # noqa: BLE001 - 坏 args 不挡流水线
                        pass
            out.append(task)
        # 用户要求 enlist 排在所有其它 formatter 的最后、项目之前。
        # 同一优先级内按 id 稳定排序。
        out.sort(key=lambda t: (th.formatter_priority(
            (t.get("formatter") or {}).get("name")), t.get("id") or 0))
        return out

    def _formatter_apply_valid(self, task, day):
        """这条 formatter 的账和当前 layout 是否已经一致（一致就绝不再切一次）。

        只看「任务还挂在同一起点」不够：同一起点但时段 layout 已经不是
        ``cut_prefix + 任务场``（例如用户/别的流程把 75 分钟改回了 50），
        必须判为失效并重切。反过来，正常 refresh 里多条 formatter 任务共用
        一个时段时，第一条不能因为自己重跑就把后面的任务清掉 —— 所以这份
        快速校验必须在调用 formatter 算法之前做。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        fmt = (task or {}).get("formatter") or {}
        name = str(fmt.get("name") or "").strip().lower()
        if not name:
            return False
        # 本地 layout 说“还在”不算数：已经开始/过去的目标场次必须在 API 里
        # 真有 id；否则就是被取消的幽灵场，必须重跑 formatter 重新找位置。
        if self._formatter_bound_session_missing(task, day):
            return False
        res = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        if name == "enlist":
            return self._enlist_apply_valid(task, day)
        if str(res.get("date") or "") != iso:
            return False
        if not str(res.get("session_start") or "").strip():
            return False
        try:
            if int(res.get("duration") or 0) != int(ts.task_minutes(task) or 0):
                return False
            if (str(sess.get("start")) != str(res.get("session_start"))
                    or int(sess.get("duration") or 0)
                    != int(res.get("session_duration") or 0)):
                return False
        except (TypeError, ValueError):
            return False
        period_id = str(res.get("period_id") or "").strip()
        result = fmt.get("result") or {}
        result_session = result.get("session") or {}
        prefix = list(result_session.get("cut_prefix")
                      or result.get("cut_prefix") or [])
        exact = bool(result_session.get("cut_prefix_exact",
                                        result.get("cut_prefix_exact", True)))
        if not period_id:
            # 没有时段（手写 csv / 一次性 session 模式）：只检查那一场还在。
            return any(str(s.get("start")) == str(sess.get("start"))
                       and int(s.get("duration") or 0) == int(sess.get("duration") or 0)
                       for s in self.sessions_for_day_plan(day))
        period = next((p for p in self.periods_for_day(day)
                       if str(p.get("id")) == period_id), None)
        if period is None:
            return False
        # 非尾巴 placement（end 把任务塞进更早一场，或 enlist/兜底一次性场）：
        # formatter 没有改写整段 layout，不能拿「prefix + 任务场」当整段预期，
        # 否则同一时段里第二条 formatter 永远被判失效、每轮 refresh 都被搬走。
        if not str(result_session.get("cut_period_id") or "").strip():
            try:
                return any(
                    str(s.get("start")) == str(sess.get("start"))
                    for s in self.sessions_for_day_plan(day))
            except Exception:                  # noqa: BLE001 - 坏排布按失效处理
                return False
        try:
            planned = sp.plan_period(period, prefix + [int(sess.get("duration") or 0)])
        except (TypeError, ValueError):
            return False
        if not planned or str(planned[-1].get("start")) != str(sess.get("start")):
            return False
        try:
            actual = sp.sessions_for_period(period, self.layouts)
        except (TypeError, ValueError):
            return False
        expected = [(str(s.get("start")), int(s.get("duration") or 0)) for s in planned]
        current = [(str(s.get("start")), int(s.get("duration") or 0)) for s in actual]
        if exact:
            if expected != current:
                return False
            # 精确排布下不应该再有同一时刻的 extra 兜底场。
            try:
                extras = {str(s.get("start")) for s in
                          sp.extras_for_date(sp.load_extras(), day)}
            except Exception:                  # noqa: BLE001
                extras = set()
            if extras.intersection(start for start, _dur in expected):
                return False
            return True
        # 兜底路径：layout 只保留前缀，任务那一场在 extras 里。
        if current != expected[:len(prefix)] and current != expected[:-1]:
            return False
        try:
            return any(str(s.get("start")) == str(sess.get("start"))
                       and int(s.get("duration") or 0)
                       == int(sess.get("duration") or 0)
                       for s in sp.extras_for_date(sp.load_extras(), day))
        except Exception:                      # noqa: BLE001
            return False

    def _enlist_apply_valid(self, task, day):
        """``enlist`` 的幂等校验：目标 session 还在、空着，占位也没漂。

        ``enlist`` 不切 layout，所以不能套 ``end`` 的「cut_prefix + 任务场」
        校验。这里检查的是：任务绑定的整场 session、formatter 记下的任务块、
        当前 layout 三者是否还对得上；以及这个块有没有被别的 formatter 抢走。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        iso = day.isoformat()
        fmt = (task or {}).get("formatter") or {}
        res = fmt.get("reservation") or {}
        sess = (task or {}).get("session") or {}
        if str(res.get("date") or "") != iso:
            return False
        try:
            if int(res.get("duration") or 0) != int(ts.task_minutes(task) or 0):
                return False
            target_start = str(res.get("session_start") or "")
            target_duration = int(res.get("session_duration") or 0)
            if (str(sess.get("date")) != iso
                    or str(sess.get("start")) != target_start
                    or int(sess.get("duration") or 0) != target_duration):
                return False
        except (TypeError, ValueError):
            return False
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            return False
        target = None
        for candidate in sessions:
            try:
                if (str(candidate.get("start")) == target_start
                        and int(candidate.get("duration") or 0) == target_duration):
                    target = candidate
                    break
            except (TypeError, ValueError):
                continue
        if target is None:
            return False
        target_range = th._range_of(target)
        block = th._range_of(res)
        if target_range is None or block is None:
            return False
        task_minutes = int(res.get("duration") or 0)
        if block[0] < target_range[0]:
            return False
        if block[0] + task_minutes > target_range[1]:
            return False
        # 目标场次里除了自己不能再有别的 open 任务；enlist 的语义就是「挂进
        # 一场空 session」，而且这样项目 plan 也不会看到假空场。
        try:
            others = [t for t in self.task_store.tasks_for_session(
                iso, target_start) if t.get("id") != task.get("id")]
        except Exception:                          # noqa: BLE001
            others = []
        if any(ts.TaskStore.is_open(t) for t in others):
            return False
        # 别的 formatter 后来占用了同一块 → 失效重排。
        try:
            reservations = [r for r in th.collect_reservations(
                self.task_store.list(), iso) if r.get("task_id") != task.get("id")]
        except Exception:                          # noqa: BLE001
            reservations = []
        if not th.is_free({"start_min": block[0], "end_min": block[1]},
                          reservations):
            return False
        # 休息尾巴不能压住下一场 session。
        for other in sessions:
            if other is target:
                continue
            got = th._range_of(other)
            if got is not None and th.overlaps(block, got):
                return False
        return True

    def _project_plan_invalid_reasons(self, project, day):
        """项目今天的 plan 为什么和当前资源对不上；空列表 = 对得上。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        plan = pj.plan_of(project, day)
        if not plan:
            return []
        items = list(plan.get("items") or [])
        if not items:
            # 只有“说好的 count”、没有任何落地场次 = 没有真正的安排；
            # 不在这里制造假失败，留给用户重新 plan。
            return []
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            sessions = []
        by_key = {}
        for sess in sessions:
            try:
                by_key[(str(sess.get("start")), int(sess.get("duration") or 0))] = sess
            except (TypeError, ValueError):
                continue
        rows_by_start = {}
        for row in self.rows or ():
            if row.get("date") != iso or not row.get("planned"):
                continue
            rows_by_start.setdefault(str(row.get("startLabel") or ""), row)
        try:
            reservations = th.collect_reservations(self.task_store.list(), iso)
        except Exception:                          # noqa: BLE001
            reservations = []
        reasons = []
        if not self.projects.is_op_allowed_by_pro(project):
            pro = self.projects.pro_for_op(project)
            op_kind = pj.normalize_op_type(project.get("op_type")) or pj.OP_TYPE_SOP
            reasons.append("所属项目「%s」当前处于 %s 阶段，不允许 %s op 进行"
                           % ((pro or {}).get("title") or "?",
                              (pro or {}).get("stage") or "?", op_kind))
        for item in items:
            start = str(item.get("start") or "")
            try:
                duration = int(item.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            title = item.get("pk") or "?"
            task = None
            try:
                task = self.task_store.get(item.get("task_id"))
            except (ValueError, TypeError):
                pass
            if task is None or not ts.TaskStore.is_open(task):
                reasons.append("任务 %s 已不存在/已收工" % title)
                continue
            if task.get("formatter"):
                reasons.append("任务 %s 已改由 formatter 独立占时间" % title)
                continue
            if ts.task_is_deferred(task):
                reasons.append("任务 %s 的 defer 还没到" % title)
                continue
            sess = task.get("session") or {}
            if (sess.get("date") != iso or str(sess.get("start")) != start
                    or int(sess.get("duration") or 0) != duration):
                reasons.append("任务 %s 的绑定和 plan 不一致" % title)
                continue
            if not start or (start, duration) not in by_key:
                reasons.append("%s %s 这场已不在当前排布里" % (start or "?", duration))
                continue
            row = rows_by_start.get(start)
            if row is None:
                reasons.append("%s 这场没有对应的契约行" % start)
                continue
            if self.rows_source == "bridge" and not row.get("id"):
                reasons.append("%s 这场没有订上/已被取消" % start)
                continue
            start_min = th._clock_minutes(start)
            end_min = start_min + duration if start_min is not None else None
            if (start_min is not None and end_min is not None
                    and not th.is_free({"start_min": start_min, "end_min": end_min},
                                       reservations)):
                reasons.append("%s 这场和 formatter 的硬占位重叠" % start)
        return reasons

    def _invalidate_project_plan(self, project, day, reasons):
        """清空项目当天的 plan，并把相关非 formatter 任务从失效场次下架。

        返回被清掉的计划条数。formatter 任务绝不在这里被下架/改写 —— 它是
        流水线第一阶段占好的资源，后续阶段只能绕开。
        """
        day = day if hasattr(day, "isoformat") else self.sel_date
        plan = pj.plan_of(project, day) or {}
        items = list(plan.get("items") or [])
        self.projects.set_allocation(project.get("id"), day, 0)
        dropped = 0
        for item in items:
            try:
                task = self.task_store.get(item.get("task_id"))
            except (ValueError, TypeError):
                continue
            if task.get("formatter"):
                continue
            sess = task.get("session") or {}
            if (sess.get("date") == day.isoformat()
                    and str(sess.get("start")) == str(item.get("start"))):
                try:
                    self.task_store.unassign(task["id"], reason="project-plan-invalid")
                    dropped += 1
                except ValueError:
                    pass
        self.say("⚠ 项目「%s」%s 的 plan 和当前时间资源对不上：%s。已清空，"
                 "请重新 `plan` 安排。"
                 % (project.get("title") or project.get("id"),
                    day.isoformat(), "；".join(reasons[:3])
                    + (" 等" if len(reasons) > 3 else "")), "warn")
        return len(items), dropped

    def reconcile_schedule_pipeline(self, day=None, quiet=False):
        """refresh 的排期流水线：**formatter 先、项目 plan 后**。

        1. formatter 阶段：每条 active formatter 任务都按当前可用场次重新核对/
           重跑。它们占掉的区间是硬约束，后面的项目 plan 只能看到剩余空场，
           绝不会改这些场次。
        2. 项目阶段：只检查**今天已经 plan 过**的项目。plan 的每个 item 必须还
           对得上当前场次、本地绑定和未取消的 API session；对不上就清空 plan，
           让用户重新 `plan`。没有 plan 的项目直接跳过。
        3. defer 还没到点的任务不进入任何排期队列；本地任务 / 项目清单两边都查。

        返回 ``{"formatters": n, "invalidated": n, "detached": n, "errors": n}``。
        """
        day = day or self.sel_date
        if hasattr(day, "datetime"):
            day = day.date()
        result = {"formatters": 0, "invalidated": 0, "detached": 0, "errors": 0}
        # 先清掉过期且从未处理的重复 occurrence：它们绝不能再进入今天/明天的
        # formatter 队列（否则同名任务会越积越多，互相抢同一场 session）。
        try:
            result["expired"] = len(self.expire_stale_repeat_occurrences())
        except Exception as e:                     # noqa: BLE001 - 清理失败不挡刷新
            result["expired"] = 0
            self.say("重复 occurrence 清理失败（继续刷新）：%s" % e, "warn")
        try:
            self.reload_periods()
            self.reload_entries()
            self.rebuild_rows()
        except Exception as e:                     # noqa: BLE001 - 先用上一份账
            self.say("排期流水线：重读本地排布失败（先用上一份）：%s" % e, "warn")
        for task in self.formatter_tasks_for_day(day):
            spec = dict(task.get("formatter") or {})
            try:
                if self._formatter_apply_valid(task, day):
                    # 已经正确：只算“检查过”，绝不重写场次，避免第一条
                    # formatter 重跑时把后面 formatter 的绑定清掉。
                    result["formatters"] += 1
                    continue
                self._apply_formatter_to_task(task, spec, action="hook",
                                              day=day, quiet=True,
                                              strict_actual=True)
                result["formatters"] += 1
            except Exception as e:                 # noqa: BLE001 - 一条坏任务不挡别的
                result["errors"] += 1
                self.had_error = True
                self.say("⚠ formatter 任务 %s「%s」没能重排：%s"
                         % (self.task_label(task),
                            self.task_store.focusmate_title(task, 28), e), "warn")
        try:
            self.rebuild_rows()
        except Exception:                          # noqa: BLE001
            pass
        try:
            projects = self.projects.active()
        except Exception as e:                     # noqa: BLE001 - 项目库坏了不挡 formatter
            projects = []
            self.had_error = True
            self.say("排期流水线：项目库读不了，跳过项目 plan 校验：%s" % e, "warn")
        for project in projects:
            reasons = self._project_plan_invalid_reasons(project, day)
            if not reasons:
                continue
            cleared, detached = self._invalidate_project_plan(project, day, reasons)
            result["invalidated"] += cleared
            result["detached"] += detached
        if result["formatters"] or result["invalidated"]:
            try:
                self.rebuild_rows()
            except Exception:                      # noqa: BLE001
                pass
            self.invalidate()
        if not quiet and (result["formatters"] or result["invalidated"]):
            self.say("🧭 排期流水线：formatter %d 条、失效项目计划 %d 条%s"
                     % (result["formatters"], result["invalidated"],
                        ("、下架 %d 条旧绑定" % result["detached"])
                        if result["detached"] else ""), "info")
        return result

    def _apply_formatter(self, result, spec, action):
        """``-f/--formatter`` 的收尾：记下 preset，并**当场调用它**。

        普通任务当场排；**重复任务本体**不排自己，而是把 formatter 记成模板，
        再转嫁给它的 occurrence 子任务（今天 + 明天 / 下一个规则日）。本体永远
        不挂 session —— 用户在契约表里看到、能完成的是子任务。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-f/--formatter` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = self._hook_task(result, "formatter", action)
        if ts.TaskStore.is_repeat_template(task):
            return self._apply_formatter_to_repeat_template(task, spec)
        return self._apply_formatter_to_task(task, spec, action=action)

    def _apply_formatter_to_repeat_template(self, task, spec):
        """把 formatter 记在重复任务**本体**上，再落到子任务上。

        本体只保存规则模板（``ts.TaskStore.hook_template`` 会摘掉 reservation /
        result / applied_at）。随后为「今天 / 明天 / 下一个规则日」生成 occurrence
        子任务，并在每个子任务上真的跑一遍 formatter —— 这样 ``task <本体> -f
        end`` 的产出是子任务，本体自己仍然没有 session。
        """
        day = th.resolve_target_day(self.sel_date, datetime.datetime.now(),
                                    spec.get("args") or {}, allow_today=True)
        today = datetime.date.today()
        if day < today:
            raise ValueError("formatter 不能排到过去（%s）。先切到那一天或指定 date=。"
                             % day.isoformat())
        template = ts.TaskStore.hook_template(spec)
        task = self.task_store.set_hook(task["id"], "formatter", template)
        # 旧数据可能把本体当普通任务排过（带 session / focusmate）；能落到规则日
        # 就把那一场**转给**子任务，本体下架。不能落就留着让用户手动处理，
        # 至少不再把 formatter 写在本体的 reservation 上。
        task = self._adopt_template_session(task)
        days = self._repeat_prepare_days(task, day, limit=2, horizon=7)
        if not days:
            self.say("🔁 任务 %s 的 formatter %s 已记在本体上；从 %s 起 7 天里"
                     "没有它的规则日，所以还没有子任务可排。"
                     % (self.task_label(task), (template or {}).get("name") or "?",
                        day.isoformat()), "warn")
            self.rebuild_rows()
            return task
        made = []
        for probe in days:
            self.ensure_repeat_occurrences(probe, force_formatters=True)
            child = self.task_store.occurrence_for(task["id"], probe)
            if child is not None:
                made.append(child)
        if made:
            self.selected_task_id = made[-1]["id"]
        names = "、".join("%s %s" % ((c.get("repeat_date") or "?"),
                                    (c.get("session") or {}).get("start") or "没排上")
                         for c in made)
        self.say("🔁 本体 %s 的 formatter %s 已转嫁给 %d 条子任务：%s"
                 % (self.task_label(task), (template or {}).get("name") or "?",
                    len(made), names or "（没生成）"), "ok")
        self.rebuild_rows()
        return task

    @staticmethod
    def _repeat_prepare_days(parent, start, limit=2, horizon=7):
        """从 ``start`` 起找这个重复任务接下来要准备的规则日（最多 ``limit`` 个）。"""
        days = []
        probe = start
        for _ in range(max(1, int(horizon))):
            if ts.TaskStore.repeat_due_on(parent, probe):
                days.append(probe)
                if len(days) >= max(1, int(limit)):
                    break
            probe += datetime.timedelta(days=1)
        return days

    def _adopt_template_session(self, parent):
        """把旧数据里重复任务**本体**身上的 session 转给当天的子任务。

        新模型里本体永远不挂 session；以前 `-f end` 会把本体排进某一场。这里
        只在「那天确实是规则日」时搬过去：子任务接过 session / Focusmate task，
        本体下架。搬不了就原样留着（宁可不猜），但界面上仍然不会把本体当任务列。
        """
        if not ts.TaskStore.is_repeat_template(parent):
            return parent
        sess = parent.get("session") or {}
        iso = str(sess.get("date") or "").strip()
        if not iso:
            return parent
        try:
            day = datetime.date.fromisoformat(iso)
        except ValueError:
            return parent
        if not ts.TaskStore.repeat_due_on(parent, day):
            return parent
        child = self.task_store.occurrence_for(parent["id"], day)
        if child is None:
            child = self.task_store.create_repeat_occurrence(
                parent["id"], day, session=sess)
        elif not (child.get("session") or {}):
            child = self.task_store.assign(child["id"], sess)
        fm = parent.get("focusmate") or {}
        if fm.get("task_id") or fm.get("id"):
            self.task_store.attach_focusmate(
                child["id"],
                {k: fm.get(k) for k in ("task_id", "id", "session_id", "attached_at",
                                        "title", "via") if fm.get(k) is not None},
                session=sess)
        parent = self.task_store.unassign(parent["id"], reason="repeat-template-adopt")
        self.say("🔁 重复任务本体 %s 原来挂在 %s %s，已转给子任务 %s（本体不占 session）"
                 % (self.task_label(parent), iso, sess.get("start") or "?",
                    self.task_label(child)), "warn")
        return parent

    def _apply_formatter_to_task(self, task, spec, action="hook", day=None,
                                 quiet=False, strict_actual=False):
        """在**一条具体任务**（通常是 occurrence 子任务）上执行 formatter。

        ``day`` 给了就排那一天（重复任务转嫁时按子任务的 repeat_date 排）；
        没给就按 ``-f`` 的 date= / 当前选中那天解析。
        """
        if day is None:
            day = th.resolve_target_day(self.sel_date, datetime.datetime.now(),
                                        spec.get("args") or {}, allow_today=True)
        today = datetime.date.today()
        if day < today:
            raise ValueError("formatter 不能排到过去（%s）。先切到那一天或指定 date=。"
                             % day.isoformat())
        # 目标场次已经被取消：先把任务从旧 session 上摘下来，再拿真实候选重排。
        # 这一步必须发生在 formatter 算法之前，否则它还会从 data.csv 里看见那场
        # 幽灵 session，重新把任务挂回去。
        if self._formatter_bound_session_missing(task, day):
            old_sess = dict(task.get("session") or {})
            self.say("⚠ formatter 任务 %s 原来挂的 %s %s 已经在 API 里不存在，"
                     "先下架再重新找位置。"
                     % (self.task_label(task), old_sess.get("date") or "?",
                        old_sess.get("start") or "?"), "warn")
            task = self._task_detach(task, reason="formatter-target-missing")
        sessions = self._formatter_sessions(day, strict_actual=strict_actual)
        ctx = {
            "task": task,
            "task_id": task["id"],
            "date": day.isoformat(),
            "sessions": sessions,
            "periods": self.periods_for_day(day),
            "reservations": th.collect_reservations(self.task_store.list(),
                                                    day.isoformat()),
            # enlist 要判断哪些 session 已经有任务；把 open 任务一起交给算法，
            # 但算法仍只读、不写盘。
            "tasks": self.task_store.list(),
        }
        try:
            out = th.call_preset("formatter", spec, ctx)
        except th.HookError as exc:
            # 算法层不知道「面板序号」这套编号；把报错里的内部 id 换成用户
            # 眼前那个序号（例如 `task modify 3 duration 25` -> `task modify 2 ...`）。
            text = str(exc)
            panel_no = self.task_no(task)
            if panel_no:
                text = text.replace("task modify %s duration" % task.get("id"),
                                    "task modify %s duration" % panel_no)
            raise th.HookError(text) from None
        chosen = out.get("session") or {}
        reservation = out.get("reservation") or {}
        if not chosen.get("start"):
            raise th.HookError("formatter %s 没有返回 session" % spec.get("name"))

        # 先看这一场是不是已经有 🔒 独占任务：撞上了就什么都别写（不然会留下
        # 一场没人用的排期 + 一条排了但挂不上的任务）。
        blocked = self._exclusive_blockers(day.isoformat(), chosen.get("start"), task)
        if blocked:
            names = "、".join(self.task_label(t) for t in blocked)
            raise th.HookError(
                "formatter %s 没排：%s %s 那场已经有 🔒 独占任务 %s —— 一场只能有一条任务。"
                "换一天 / 换一场，或先 `task <序号> -e false` 放开那条的独占。"
                % (spec.get("name"), day.isoformat(), chosen.get("start"), names))

        # `end` 会把「当天最后一段」的尾巴**切一场任务大小的 session** 出来
        # （11:00–12:00 的时段 + 25 分钟任务 → 11:30–11:55），并**剪断**那个时段：
        # 原来那场 50 分钟会真的被取消/从计划里拿掉，只剩「前半截 + 这一场」。
        carved_note = None
        # 即使 `existing=True` 也要检查切割排布：例如旧排布是 [50] + 23:30
        # 一次性补场，任务虽然“在 23:30”，但那个时段并没有被真正切成
        # [75, 25]；refresh 必须把它掰回来，而不是看见同起点就跳过。
        if chosen.get("carved"):
            if chosen.get("cut_period_id"):
                carved_note = self._cut_period_for_carve(day, chosen, task=task)
            elif not chosen.get("existing"):
                carved_note = self._ensure_carved_session(day, chosen, task=task)
            if carved_note:
                sessions = self._formatter_sessions(day)
            try:
                # 剪断可能把我们这条任务（或它那一场）的绑定清掉了，重读一遍再判断
                # 「原来挂在哪」—— 否则会对着一个已经不存在的绑定再下架一次。
                task = self.task_store.get(task["id"])
            except (ValueError, TypeError):
                pass

        # 选中 session 的 Focusmate 身份：未来那天可能已经订好了，也可能还没有。
        hour, minute = map(int, chosen["start"].split(":"))
        slot = {"start": chosen["start"], "duration": int(chosen.get("duration") or 0),
                "hour": hour, "minute": minute}
        session_info = self.session_info_for(day, slot)
        previous = task.get("session") or {}
        # 场次身份不只看起点：同一时刻时长被 formatter 改成另一档，也必须重挂。
        # 只看 date/start 会把「25 分钟任务还挂在旧的 50 分钟场上」当成已正确。
        moved = bool(previous) and (
            (previous.get("date"), previous.get("start"),
             int(previous.get("duration") or 0))
            != (session_info.get("date"), session_info.get("start"),
                int(session_info.get("duration") or 0)))
        if moved:
            self.say("任务 %s 原来在 %s %s；formatter 要先下架旧位置再挂新位置。"
                     % (self.task_label(task), previous.get("date"), previous.get("start")),
                     "warn")
            task = self._task_detach(task, reason="formatter-move")
        if moved or not previous:
            note = self.assign_task_to(task, session_info)
            task = self.task_store.get(task["id"])
            assigned = task.get("session") or {}
            if (not assigned
                    or str(assigned.get("start") or "") != str(session_info.get("start") or "")):
                raise th.HookError(
                    "formatter 没能把任务挂到 %s %s：%s"
                    % (day.isoformat(), session_info.get("start") or "?",
                       note or "这场不可用（可能已被取消或被独占任务占用）"))
        else:
            note = "已经在这场，不重复建 Focusmate task"

        spec = dict(spec)
        spec["result"] = out
        spec["reservation"] = reservation
        spec["applied_at"] = ts._now()
        task = self.task_store.set_hook(task["id"], "formatter", spec)
        # `end` 排出来的任务**自动独占**：一场 Focusmate 里塞两条任务等于两个专注块
        # 被切成四段。想放开就 `task <序号> -e false`。
        if spec.get("name") == "end" and not th.is_exclusive(task):
            task = self.task_store.modify(task["id"], "exclusive", True)
        if not quiet:
            notes = ""
            if carved_note:
                notes += "；" + carved_note
            conflicts = [] if chosen.get("cut_period_id") else \
                [str(s) for s in (out.get("conflicts") or []) if s]
            if conflicts:
                notes += ("；⚠ 你原来排的 %s 那场还在，跟这一场时间上重叠了 —— "
                          "不想留它：`period %s 25` 重排这个时段，"
                          "或 `cancel <表格行号>` 取消已订的那场"
                          % ("、".join(conflicts),
                             (reservation.get("period_id") or "T?")))
            lock_txt = "；🔒 独占" if th.is_exclusive(task) else ""
            self.say("✅ 任务 %s「%s」已由 formatter %s 排到 %s %s–%s"
                     "（session %s 分钟，任务 %s 分钟 + %s 分钟休息；占用 %s–%s%s）%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        spec.get("name"), reservation.get("date"), reservation.get("start"),
                        reservation.get("end"), reservation.get("session_duration"),
                        reservation.get("duration"), reservation.get("break_minutes"),
                        reservation.get("start"), reservation.get("end"), lock_txt,
                        notes + (("；" + note) if note else "")), "ok")
            self.selected_task_id = task["id"]
        self.rebuild_rows()
        return task

    NO_TASK_ACTIONS = ("delete", "sync", "refresh", "reconcile", "archive", "history",
                       "归档", "list", "ls", "show", "done", "complete", "完成",
                       "drop", "放弃")

    def _apply_note(self, result, text, action):
        """``--note`` 的收尾：给刚被创建/修改/选中的那条任务写备注。"""
        if action in self.NO_TASK_ACTIONS:
            self.say("`--note` 对 %s 没意义（它不产出任务，没有能写备注的对象），忽略。"
                     % action, "warn")
            return None
        task_id = None
        if isinstance(result, dict) and result.get("id") is not None:
            task_id = result["id"]
        elif self.selected_task_id is not None:
            task_id = self.selected_task_id
        if task_id is None:
            self.say("`--note` 没找到要写备注的任务：先 task create / task select。", "warn")
            return None
        try:
            task = self.task_store.set_notes(task_id, text)
        except ValueError:
            self.say("`--note`：任务 %s 已经不在了（%s 之后没得写），忽略。"
                     % (task_id, action), "warn")
            return None
        self.say("✅ 任务 %s 备注：%s"
                 % (self.task_label(task), self.task_store.notes(task) or "（空）"), "ok")
        return task

    def _apply_exclusive(self, result, value, action):
        """``-e/--exclusive`` 的收尾：把「这一场只能有它一条任务」记进账本。

        独占只落在 ``fields.exclusive``（任务库不校验字段），真正起作用的是两处：

        * **formatter 排期**：独占任务的占用扩展成**整场** session，同一场剩下的
          时间不会再被下一条任务排进去（见 :func:`task_hooks.collect_reservations`）；
        * **refresh --check**：发现一场里挤了两条（其中有独占的）→ 全部下架 + 警告。

        ``end`` 排出来的任务**自动**独占，所以这里多半是手动给导入 / 手工任务补上。
        """
        if action in self.NO_TASK_ACTIONS:
            self.say("`-e/--exclusive` 对 %s 没意义（它不产出任务），忽略。" % action, "warn")
            return None
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-e/--exclusive` 要跟一条任务：`task 3 -e true` / "
                             "`task create title=写论文 -e true`")
        want = bool(value)
        task = self.task_store.modify(task["id"], "exclusive", want)
        if want:
            self.say("🔒 任务 %s 设为独占：这一场 session 只能有它一条任务。"
                     % self.task_label(task), "ok")
        else:
            self.say("🔓 任务 %s 取消独占：这一场又能并别的任务了。"
                     % self.task_label(task), "ok")
        return task

    def _apply_assign(self, result, session_token, action):
        """``--assign`` 的收尾：拿到任务就挂，拿不到就说一声（不改任何东西）。

        ``result`` 是**任务对象**（create/duplicate/select 的返回值），所以这里直接
        走内部的 :meth:`_assign_task` —— 绝不能再把它扔回 ``cmd_task_assign`` 让
        「序号解析」重新认一遍（内部 id 和面板序号是两套数，绕回去就会挂错任务）。
        """
        # 只认**本地任务**（有 fields 的那种）。项目记录也带 id，但它不是任务：
        # `task import <项目链接> -a 1` 不该顺手把「上一次选中的任务」挂上去。
        task = (result if isinstance(result, dict)
                and result.get("id") is not None
                and isinstance(result.get("fields"), dict) else None)
        if task is None and action != "import" and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            self.say("`--assign` 只对会产出任务的动作用得上（create/modify/select）；"
                     "%s 没有可挂的任务，忽略。" % action, "warn")
            return None
        self.selected_task_id = task["id"]
        return self._assign_task(task, str(session_token))

    def _apply_mark(self, result, status, action):
        """``-m/--mark`` 的收尾：只改本地账本，不碰 Focusmate / OmniFocus。

        和 ``done`` / ``drop`` 的分工：
          * ``done`` = 本地完成 + 归档 + 勾 Focusmate + 写回 OmniFocus
          * ``drop`` = 本地放弃（默认还让 OmniFocus 放弃）
          * ``-m complete`` / ``-m discard`` = **只动账本**。

        本地 completed 之后，下一次 ``task sync`` / ``refresh --check`` 仍会按
        单向契约把完成写回 OmniFocus；discard 则没有任何自动外部动作。
        """
        task = result if isinstance(result, dict) and result.get("id") is not None else None
        if task is None and self.selected_task_id is not None:
            try:
                task = self.task_store.get(self.selected_task_id)
            except ValueError:
                task = None
        if task is None:
            raise ValueError("`-m/--mark` 要跟一条任务：`task 3 -m complete` / "
                             "`task -m discard 3`（没有选中的任务可标）")
        task_id = task["id"]
        if status == ts.STATUS_COMPLETED and ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能标记完成；完成的是它生成的"
                             "子任务。要结束这套重复：`task repeat %s off` 或 "
                             "`task %s drop`（手动放弃）。"
                             % (self.task_label(task), self.task_label(task),
                                self.task_label(task)))
        current = self.task_store.status_of(task)
        if current == status:
            self.say("任务 %s 已经是「%s」，没有改。"
                     % (self.task_label(task),
                        "完成" if status == ts.STATUS_COMPLETED else "放弃"), "info")
            return task
        if status == ts.STATUS_COMPLETED:
            task = self.task_store.complete(task_id, reason="cmd-mark-complete")
            self.task_store.archive_local(self.task_store.get(task_id),
                                          reason="cmd-mark-complete")
            note_sub = self._project_child_completed(task, writeback=False)
            self.say("✓ 任务 %s「%s」已标记完成（只改本地账本：没勾 Focusmate、"
                     "没当场写 OmniFocus；之后同步仍会按单向契约写回）%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        ("；" + note_sub) if note_sub else ""),
                     "ok")
        else:
            task = self.task_store.drop(task_id, reason="cmd-mark-discard")
            self.say("✗ 任务 %s「%s」已标记放弃（只改本地账本：Focusmate / OmniFocus 都没动）"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40)),
                     "ok")
        self.selected_task_id = task_id
        return task

    def _dispatch_task(self, action, rest):
        # `task 3 duplicate -a 1`：动作写在序号后面（需求里的语法就是这个顺序）。
        # `id:15` 也走这条：它是「明说按内部 id」的写法（面板上没有的任务）。
        numeric = action.isdigit() or action.startswith("id:")
        if numeric and rest:
            verb = str(rest[0]).lower()
            if verb in ("duplicate", "dup", "copy", "复制"):
                return self.cmd_task_duplicate([action] + rest[1:])
            if verb in ("drop", "放弃"):
                return self.cmd_task_drop([action] + rest[1:])
            if verb in ("done", "complete", "完成"):
                return self.cmd_task_done([action] + rest[1:])
            if verb in ("link", "关联"):
                return self.cmd_task_link([action] + rest[1:])
            if verb in ("swap", "调换", "换位"):
                return self.cmd_task_swap([action] + rest[1:])
        if action == "duplicate" or action in ("dup", "copy", "复制"):
            return self.cmd_task_duplicate(rest)
        if action == "import" or action in ("粘贴", "导入"):
            # 后缀（-a / -n / -m / -t / -f）由 cmd_task 在这之后统一套，而且会
            # **逐条**套到整批上；这里再套一遍就是双份（`-a` 会重复挂一场）。
            return self.cmd_task_import(rest, apply_suffix=False)
        if action == "create":
            return self.cmd_task_create(rest)
        if action in ("drop", "放弃"):
            return self.cmd_task_drop(rest)
        if action in ("done", "complete", "完成"):
            return self.cmd_task_done(rest)
        if action in ("link", "关联"):
            return self.cmd_task_link(rest)
        if action in ("swap", "调换", "换位"):
            return self.cmd_task_swap(rest)
        if action == "delete":
            return self.cmd_task_delete(rest)
        if action == "modify":
            return self.cmd_task_modify(rest)
        if action in ("note", "notes", "备注"):
            return self.cmd_task_note(rest)
        if action == "assign":
            return self.cmd_task_assign(rest)
        if action in ("archive", "history", "归档"):
            return self.cmd_task_archive(rest)
        if action in ("repeat", "recurring"):
            return self.cmd_task_repeat(rest)
        if action == "select":
            return self.cmd_task_select(rest)
        if action in ("sync", "refresh", "reconcile"):
            return self.cmd_task_sync(quiet=False)
        if action in ("list", "ls", "show"):
            self.say("任务 %d 条；看本视图表格即可。" % len(self.task_store.list()), "info")
            return False
        if action in ("sort", "排序"):
            return self.cmd_task_sort(rest)
        if action in ("group", "grouping", "分组"):
            return self.cmd_task_group(rest)
        if action in ("search", "find", "filter", "搜索"):
            return self.cmd_task_search(rest)
        if action in ("page", "翻页"):
            return self.cmd_task_page(rest)
        # 裸序号就是 select：`task 3` ≡ `task select 3`，
        # 于是 `task 3 -a 1` = 「选中第 3 条再挂到表格 #1」。
        if numeric:
            if rest:
                hint = ("`task <项目序号> <count>` 只在 `plan` 的分配问答里才有意义 —— "
                        "先敲 `plan`。" if not self.planning else
                        "allocate 的写法是 `task <项目序号> <count>`：序号 = 项目表上的 #。")
                raise ValueError("`task %s` 就等于 `task select %s`，后面不用再跟东西；"
                                 "要挂到 session 用 -a/--assign（例如 task %s -a %s）。%s"
                                 % (action, action, action, rest[0], hint))
            return self.cmd_task_select([action])
        raise ValueError("未知 task 动作 %r（create/delete/modify/assign/select/repeat/"
                         "duplicate/drop/done/link/import/sync/archive；"
                         "也可以直接写任务 ID，如 task 8 ≡ task select 8）" % action)

    def cmd_task_archive(self, args, limit=5):
        """task archive [N] —— 看完成归档（data/task-archive.json，只增不改）。"""
        records = [r for r in (self.task_store.load_archive().get("records") or [])
                   if isinstance(r, dict)]
        try:
            limit = int(args[0]) if args else limit
        except (TypeError, ValueError):
            raise ValueError("task archive [N]：N 是看最近几条")
        self.say("归档 %s：%d 条（只增不改；同一任务+task+完成时间只记一次）"
                 % (self.task_store.archive, len(records)), "info")
        if not records:
            self.say("   还没有归档：任务在 Focusmate 上有 completedAt 之后，task sync 会补进来。",
                     "info")
            return records
        for rec in records[-limit:]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            fields = rec.get("fields") or {}
            title = (fields.get("title") or fields.get("name")
                     or self.task_store._one_line(fields)[:60] or "（空任务）")
            self.say("   #%s %s ← session %s %s · 完成 %s"
                     % (rec.get("task_id") or "?", title,
                        sess.get("date") or "?", sess.get("start") or "?",
                        fm.get("completed_at") or "?"), "ok")
            note = rec.get("notes") or self.task_store.notes({"fields": fields})
            if note:
                self.say("      备注：%s" % note, "info")
        return records

    def cmd_task_create(self, args):
        """task create [id] [key=value ...] [--repeat 周几@HH:MM]

        字段不校验；给了 id 就用它。``--repeat`` 把任务标成**每周重复**：到那一周
        就自动挂到同一时刻、同一时长的那场 session 上（拆分/改时长后这一周就不挂，
        但 repeat 设置不变）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        explicit = None
        repeat_spec = None
        kept = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--repeat", "-r"):
                if i + 1 >= len(toks):
                    raise ValueError("--repeat 后面要跟 周几@HH:MM（例如 mon@22:15）")
                repeat_spec = toks[i + 1]
                i += 2
                continue
            if low.startswith("--repeat="):
                repeat_spec = tok.split("=", 1)[1]
                i += 1
                continue
            kept.append(tok)
            i += 1
        toks = kept
        if toks and re.match(r"^\d+$", toks[0]) and "=" not in toks[0]:
            explicit = int(toks[0])
            toks = toks[1:]
        fields, loose = {}, []
        for tok in toks:
            if "=" in tok:
                key, value = tok.split("=", 1)
                if key:
                    fields[key] = value
            else:
                loose.append(tok)
        if loose:
            fields.setdefault("title", " ".join(loose))
        # 直接把 OmniFocus 里的「拷贝任务」粘进 title 也认：
        #   task create [数学易错修正](omnifocus:///task/kc-4VLAE_1L)
        # 认得出 primary key 就顺手关联上（只有标题的那种粘贴就只当标题）。
        link = of.parse_link(fields.get("title") or "")
        omnifocus = None
        if link and link.get("id"):
            omnifocus = {"id": link["id"], "url": link.get("url"), "title": link.get("title")}
            if link.get("title"):
                fields["title"] = link["title"]
        task = self.task_store.create(fields, task_id=explicit, omnifocus=omnifocus)
        if omnifocus:
            self.say("🔗 顺手关联到 OmniFocus %s" % omnifocus["id"], "ok")
        if repeat_spec:
            task = self.cmd_task_repeat([str(task["id"]), repeat_spec], quiet=True)
        self.selected_task_id = task["id"]
        self.say("✅ 任务 %s 已创建（字段：%s）"
                 % (self.task_label(task), self.task_store.summarize(task) or "空"), "ok")
        return task

    def cmd_task_repeat(self, args, quiet=False):
        """task repeat <id> <周几@HH:MM|off> —— 设置 / 取消每周重复。

        不写时刻时用今天最后一场的时间当锚点；duration 取那场 session 的时长，
        所以它只在「同一时刻、同一时长」的 session 上落地。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task repeat <任务序号> <周几@HH:MM|off>")
        task = self.resolve_task(toks[0])
        if len(toks) == 1:
            rep = task.get("repeat")
            if not rep:
                self.say("任务 %s 不是重复任务。" % self.task_label(task), "info")
            else:
                self.say("任务 %s 重复：%s；落点 %d 次"
                         % (self.task_label(task), ts.repeat_summary(rep),
                            len(rep.get("occurrences") or {})), "info")
                if rep.get("rule"):
                    self.say("   RRULE：%s（锚点 %s）"
                             % (rep.get("rule"), rep.get("rule_anchor") or "?"), "info")
            return task
        spec = toks[1].lower()
        if spec in ("off", "none", "clear", "cancel"):
            task = self.task_store.clear_repeat(task["id"])
            self.say("✅ 任务 %s 的每周重复已取消。" % self.task_label(task), "ok")
            return task
        parsed = cfg.parse_repeat_spec(toks[1])
        # 锚点：同一天里同一时刻那场 session 的时长（找不到就用 50）
        day = datetime.date.today()
        day += datetime.timedelta(days=(parsed["weekday"] - day.weekday()) % 7)
        anchored = None
        for sess in self.sessions_for_day_plan(day):
            if sess.get("start") == parsed["start"]:
                anchored = sess
                break
        duration = int(anchored["duration"]) if anchored else 50
        task = self.task_store.set_repeat(task["id"], parsed["weekday_label"],
                                         parsed["start"], duration)
        if not quiet:
            self.say("✅ 任务 %s 每周 %s %s 重复（锚定 %dmin 那场）"
                     % (self.task_label(task), parsed["weekday_label"],
                        parsed["start"], duration), "ok")
        return task

    def cmd_task_note(self, args):
        """task note <任务 ID> <备注文本>；不写文本 = 清空。

        备注可以是网址（完成之后照着去对应地方把活干完），也可以是任何话。
        """
        if not args:
            raise ValueError("用法：task note <任务 ID> <备注文本>（不写文本 = 清空）")
        text = " ".join(str(x) for x in args[1:]).strip()
        task = self.task_store.set_notes(self.resolve_task(args[0])["id"], text)
        self.say("✅ 任务 %s 备注%s：%s"
                 % (self.task_label(task), "已清空" if not text else "",
                    self.task_store.notes(task) or "（空）"), "ok")
        return task

    def cmd_task_delete(self, args):
        if not args:
            raise ValueError("用法：task delete <任务序号>")
        task = self.task_store.delete(self.resolve_task(args[0])["id"])
        if self.selected_task_id == task["id"]:
            self.selected_task_id = None
        self.say("✅ 已删除任务 %s：%s"
                 % (self.task_label(task), self.task_store.summarize(task)), "ok")
        return True

    def cmd_task_modify(self, args):
        if len(args) < 3:
            raise ValueError("用法：task modify <任务序号> <字段> <新值>")
        task_id, field = self.resolve_task(args[0])["id"], args[1]
        value = " ".join(str(x) for x in args[2:])
        task = self.task_store.modify(task_id, field, value)
        self.say("✅ 任务 %s.%s = %r" % (self.task_label(task), field, value), "ok")
        return task

    def cmd_task_select(self, args):
        if not args:
            raise ValueError("用法：task select <任务序号>")
        task = self.resolve_task(args[0])
        self.selected_task_id = task["id"]
        self.task_view_page = None
        self.say("已选中任务 %s：%s"
                 % (self.task_label(task), self.task_store.summarize(task)), "cmd")
        return task

    def _task_for_assign(self):
        if self.selected_task_id is not None:
            try:
                return self.task_store.get(self.selected_task_id)
            except ValueError:
                self.selected_task_id = None
        tasks = self.task_store.list()
        if not tasks:
            raise ValueError("还没有任务：先 task create ...")
        # 没选中时默认最近创建的那条。想精确指定就 task select / assign <任务ID> <sessionID>。
        task = tasks[-1]
        self.selected_task_id = task["id"]
        return task

    def cmd_task_duplicate(self, args):
        """``task <id> duplicate [-a N]`` —— 复制一条本地任务，指向同一条 OmniFocus 任务。

        需求（第 4 条）的原话就是 ``task {index} duplicate``（可以接 ``-a`` 之类）。
        复制品是一条**新的本地记录**：字段、本地标签、OmniFocus primary key 全一样，
        但 ID 是新的、session 是空的（除非 ``-a``）。于是「一个 OmniFocus 任务 ↔
        多条本地记录」成为可能 —— 这正是「单向覆盖」的前提：OmniFocus 只管有活没活，
        哪一场在干、干没干由本地账本决定。

        不带 ``-a`` 时复制品不挂场次（先放着，之后 ``task N -a M``）。
        原来的那条**不动**：要让它「变成放弃 + 未完成转移」是收尾里 A 那一步的事。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务序号> duplicate [-a <session ID>]")
        task = self.resolve_task(toks[0])
        copy = self.task_store.duplicate(task["id"])
        self.selected_task_id = copy["id"]
        self.say("✅ 任务 %s「%s」→ 复制成 %s（同一条 OmniFocus：%s）"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    self.task_label(copy),
                    (task.get("omnifocus") or {}).get("id") or "未关联"), "ok")
        return copy

    def cmd_task_drop(self, args):
        """``task <id> drop [--local]`` —— 放弃：本地放弃，默认**OmniFocus 也放弃**。

        ``--local`` 只在本地放弃（「延期 / 转移动了账本」那种语义），OmniFocus 不动。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        local_only = "--local" in toks
        toks = [t for t in toks if t != "--local"]
        if not toks:
            raise ValueError("用法：task <任务序号> drop [--local]")
        task = self.resolve_task(toks[0])
        template = ts.TaskStore.is_repeat_template(task)
        if not local_only and ts.TaskStore.omnifocus_id(task):
            self.jobs.begin(1, title="放弃任务 %s（task %s drop）"
                                   % (self.task_label(task), self.task_label(task)))
        note = "" if local_only else self.omni_drop(task, all_occurrences=template)
        task = self.task_store.set_status(task["id"], ts.STATUS_DROPPED, reason="cmd-drop")
        self.say("✗ 任务 %s「%s」已放弃%s%s"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    ("；" + note) if note else "（只改了本地）",
                    "；这是重复任务本体，之后不再生成子任务" if template else ""), "ok")
        return task

    def cmd_task_done(self, args):
        """``task <id> done [分钟]`` —— 完成：本地完成 + Focusmate 勾上 + OmniFocus 写回。

        不给分钟数也行；给了就记成实际耗时（和收尾里「缺漏完成补充」同一套）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务序号> done [实际分钟数]")
        task = self.resolve_task(toks[0])
        if ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能当成一次 session 的活来完成；"
                             "它能生成的子任务才是能完成的。要结束这套重复："
                             "`task repeat %s off`（保留任务）或 `task %s drop`（手动放弃）。"
                             % (self.task_label(task), self.task_label(task),
                                self.task_label(task)))
        minutes = None
        if len(toks) > 1:
            try:
                minutes = int(toks[1])
            except ValueError:
                raise ValueError("分钟数得是整数；你给的是 %r" % toks[1]) from None
        return self._complete_task(task, minutes=minutes, why="cmd-done")

    def _project_child_completed(self, task, writeback=True):
        """本地子任务完成后，更新项目库；全部子任务完成 → 父任务自动完成。

        ``writeback=True`` 时把父任务立刻写回 OmniFocus（普通 ``task done`` /
        收尾完成走这条）；``-m complete`` 这种「只动本地账本」的路径传 False。
        """
        fields = (task or {}).get("fields") or {}
        source = (task or {}).get("source") or {}
        parent_pk = str(source.get("parent_id") or "").strip()
        child_pk = str(source.get("id") or fields.get(pj.PROJECT_TASK_FIELD)
                       or "").strip()
        if not parent_pk or not child_pk:
            return ""
        try:
            project_id = int(fields.get(pj.PROJECT_ID_FIELD))
        except (TypeError, ValueError):
            return ""
        try:
            project, auto_done, newly = self.projects.complete_subtasks(
                project_id, parent_pk, [child_pk])
        except (ValueError, TypeError) as e:
            self.say("⚠ 更新项目子任务完成状态失败：%s" % e, "warn")
            return ""
        parent = pj.task_by_pk(project, parent_pk) or {}
        parent_title = parent.get("title") or parent_pk
        notes = []
        if newly:
            notes.append("项目「%s」子任务「%s」已记完成"
                         % (project.get("title") or project_id,
                            fields.get("title") or child_pk))
        if auto_done:
            notes.append("父任务「%s」的子任务全部完成 → 父任务自动完成"
                         % parent_title)
            if writeback:
                if self.omnifocus_enabled():
                    action = self.net_action(
                        "omnifocus.complete",
                        "OmniFocus 完成父任务 %s" % parent_pk[:8],
                        {"pk": parent_pk, "at": None, "due": None},
                        source=SRC_OMNIFOCUS)
                    job = self.jobs.submit(
                        action,
                        on_fail=lambda error: self.say(
                            "父任务「%s」自动完成了本地的，但 OmniFocus 写回失败：%s"
                            % (parent_title, error), "warn"))
                    if self.jobs.sync:
                        outcome = job.outcome
                        if outcome is None or not outcome.ok:
                            notes.append("OmniFocus 父任务写回失败：%s"
                                         % (outcome and outcome.error or "没跑"))
                        else:
                            notes.append("OmniFocus 父任务已完成")
                    else:
                        notes.append("OmniFocus 父任务完成写回丢后台了")
                else:
                    notes.append("OmniFocus 关着 → 父任务只完成了本地")
        if notes:
            self.rebuild_rows()
            self.invalidate()
        return "；".join(notes)

    def _complete_task(self, task, minutes=None, why="cmd-done"):
        """完成任务的那一段（``task done`` / ``-c`` 够数 / 收尾 F 共用）。

        一次操作、最多两步：先勾 Focusmate（那才是「这一场干完了」的事实），
        再写回 OmniFocus。面板标题是这次操作本身。
        """
        if ts.TaskStore.is_repeat_template(task):
            raise ValueError("任务 %s 是重复任务**本体**，不能当成一次 session 的活来完成。"
                             % self.task_label(task))
        has_fm = bool((task.get("focusmate") or {}).get("task_id"))
        has_omni = bool(ts.TaskStore.omnifocus_id(task))
        self.jobs.begin(2 if (has_fm and has_omni) else 1,
                        title="完成任务 %s（task %s done）"
                              % (self.task_label(task), self.task_label(task)))
        task = self.task_store.complete(task["id"], minutes=minutes, reason=why)
        self.task_store.archive_local(self.task_store.get(task["id"]))
        note_fm = self.fm_complete(task)
        note_of = self.omni_complete(task)
        note_sub = self._project_child_completed(task, writeback=True)
        self.say("✓ 任务 %s「%s」完成%s%s%s%s"
                 % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                    ("（%d 分钟）" % minutes) if minutes else "",
                    ("；" + note_fm) if note_fm else "",
                    ("；" + note_of) if note_of else "",
                    ("；" + note_sub) if note_sub else ""), "ok")
        return task

    def cmd_task_link(self, args):
        """``task <id> link [链接|PK]`` —— 把本地任务关联到 OmniFocus。

        **不给链接就是读剪贴板**（和 `task import` 同一条规矩：动作归子命令）。
        OmniFocus 里 ⌘C 那条任务，回来敲 `task 5 link` 就完事。

            task 5 link                 ← 读剪贴板（只认里面唯一的那条链接）
            task 5 link omnifocus:///task/kc-4VLAE_1L
            task 5 link "[数学易错修正](omnifocus:///task/kc-4VLAE_1L)"
            task 5 link kc-4VLAE_1L

        只有标题（``title: "数学易错修正"``）那种粘贴**认不出 primary key**，
        会明确拒绝 —— 拿标题去猜一条任务，猜错比没有更糟。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务 ID> link [omnifocus:///task/PK | PK]")
        task_id = self.resolve_task(toks[0])["id"]
        rest = toks[1:]
        auto = not rest
        if auto:
            # 读剪贴板走 worker（osascript / pbpaste 也是外部操作）。
            # 这里不能先读：用户敲完 link 应该立刻回到提示符。
            self.jobs.begin(1, title="关联 OmniFocus（task %s link）"
                                   % self.task_label(self.task_store.get(task_id)))
            holder = {}

            def apply_links(payload):
                items = [item for item in ((payload or {}).get("items") or [])
                         if item.get("id")]
                if not items:
                    self.had_error = True
                    self.say("剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本 "
                             "都读了）。在 OmniFocus 里 ⌘C 那条任务再来。", "error")
                    self.invalidate()
                    return
                if len(items) > 1:
                    self.had_error = True
                    self.say("剪贴板里有 %d 条 OmniFocus 链接：%s —— 只拷一条再来，"
                             "或者把要的那条的链接直接写出来。"
                             % (len(items), "、".join(i["id"] for i in items[:5])),
                             "error")
                    self.invalidate()
                    return
                info = items[0]
                task = self.task_store.link_omnifocus(
                    task_id, info["id"], title=info.get("title"))
                holder["task"] = task
                self.say("🔗 任务 %s 已关联 OmniFocus %s%s（链接是从剪贴板读的）"
                         % (self.task_label(task), info["id"],
                            ("「%s」" % info["title"]) if info.get("title") else ""), "ok")

            action = self.net_action(
                "omnifocus.clipboard", "读剪贴板里的 OmniFocus 链接", {},
                source=SRC_OMNIFOCUS)
            self.jobs.submit(
                action, persist=False, on_ok=apply_links,
                on_fail=lambda error: (setattr(self, "had_error", True),
                                       self.say("读剪贴板失败：%s" % error, "error")))
            if self.jobs.sync:
                return holder.get("task")
            self.say("正在后台读剪贴板里的 OmniFocus 链接…", "cmd")
            return NET_PENDING
        info = of.parse_link(" ".join(rest))
        if not info or not info.get("id"):
            raise ValueError("认不出 OmniFocus 任务 primary key：%r（要 omnifocus:///task/… "
                             "或裸 PK；只有标题的那种粘贴认不出来）" % (" ".join(rest),))
        task = self.task_store.link_omnifocus(task_id, info["id"], title=info.get("title"))
        self.say("🔗 任务 %s 已关联 OmniFocus %s%s"
                 % (self.task_label(task), info["id"],
                    ("「%s」" % info["title"]) if info.get("title") else ""), "ok")
        return task

    def _answer_pending_import(self, line):
        """用户在「复制重复设置吗？」后敲的 y / n / q。"""
        text = " ".join(str(line or "").strip().lower().split())
        yes = {"y", "yes", "是", "要", "复制", "copy", "ok"}
        no = {"n", "no", "不", "否", "不要", "只导入", "skip"}
        cancel = {"q", "quit", "cancel", "取消", "abort", "esc"}
        if text in cancel:
            self.pending_import = None
            self.say("已取消这次导入；没有建本地任务。", "warn")
            self.invalidate()
            return True
        if text not in yes and text not in no:
            self.say("导入还在等回答：[y] 复制重复设置并导入 / [n] 只导入这一次 / [q] 取消",
                     "warn")
            return True

        pending, self.pending_import = self.pending_import, None
        try:
            task = self._finish_task_import(
                pending.get("items") or [],
                copy_repeat=(text in yes),
                repeats=pending.get("repeats") or {},
                warnings=pending.get("warnings") or [],
            )
            self._apply_import_suffix(task, pending.get("suffix"))
        except ValueError as e:
            self.had_error = True
            self.say(str(e), "warn")
        except Exception as e:                          # noqa: BLE001 - 别让问句把 TUI 掀了
            self.had_error = True
            self.say("导入失败：%s: %s" % (type(e).__name__, e), "error")
        try:
            self.refresh_warnings(force=True)
        except Exception:                               # noqa: BLE001 - 警告板坏了也不能丢导入结果
            pass
        self.invalidate()
        return True

    def _apply_suffix_chain(self, task, action, note_text=None, assign_to=None,
                            exclusive=None, mark_status=None, count_spec=None,
                            trigger_spec=None, formatter_spec=None):
        """按固定顺序把一条任务的后缀跑完：
        note → count → exclusive → assign → mark → trigger → formatter。

        count 排在 formatter 前面是有意的：``task create ... -c 3 -f end`` 里
        ``-f end`` 要按「刚刚设好的时长」去切 session（count 3 = 75 分钟）。

        这是 `cmd_task` 和 `task import` 共用的那一段；一次导入多条时由
        :meth:`_apply_import_suffix` 对**每一条**各跑一遍。
        """
        if note_text is not None:
            self._apply_note(task, note_text, action)
        if count_spec is not None:
            task = self._apply_count(task, count_spec, action) or task
        if exclusive is not None:
            task = self._apply_exclusive(task, exclusive, action) or task
        if assign_to is not None:
            task = self._apply_assign(task, assign_to, action) or task
        if mark_status is not None:
            task = self._apply_mark(task, mark_status, action) or task
        if trigger_spec is not None:
            task = self._apply_trigger(task, trigger_spec, action) or task
        if formatter_spec is not None:
            task = self._apply_formatter(task, formatter_spec, action) or task
        return task

    def _apply_import_suffix(self, task, suffix):
        """把 `task import` 后面挂的 -n / -a / -m / -t / -f 落到**真正导入的任务**上。

        ``task`` 可以是单条任务，也可以是 :class:`ImportBatch`（一次导入多条）——
        后一种情况后缀逐条套，不是只套最后一条。重复任务本体会在这些动作里被导向
        当天的 occurrence 子任务（见 :meth:`_assign_task` /
        :meth:`_apply_formatter`），所以 ``task import -ac`` 不会把模板本身挂到
        session 上。
        """
        suffix = suffix if isinstance(suffix, dict) else {}
        if isinstance(task, ImportBatch):
            targets = [one for one in task
                       if isinstance(one, dict) and one.get("id") is not None]
            last = None
            for one in targets:
                last = self._apply_suffix_chain(
                    one, "import", note_text=suffix.get("note_text"),
                    assign_to=suffix.get("assign_to"),
                    exclusive=suffix.get("exclusive"),
                    mark_status=suffix.get("mark_status"),
                    count_spec=suffix.get("count_spec"),
                    trigger_spec=suffix.get("trigger_spec"),
                    formatter_spec=suffix.get("formatter_spec"))
            return last if last is not None else task.last
        if not isinstance(task, dict) or task.get("id") is None:
            return task
        return self._apply_suffix_chain(
            task, "import", note_text=suffix.get("note_text"),
            assign_to=suffix.get("assign_to"),
            exclusive=suffix.get("exclusive"),
            mark_status=suffix.get("mark_status"),
            count_spec=suffix.get("count_spec"),
            trigger_spec=suffix.get("trigger_spec"),
            formatter_spec=suffix.get("formatter_spec"))

    def _finish_task_import(self, items, copy_repeat=False, repeats=None,
                            warnings=None):
        """把（已经问过重复设置的）候选真正写进本地任务库。"""
        repeats = repeats or {}
        for warning in warnings or []:
            self.say(str(warning), "warn")
        made = []
        for index, item in enumerate(items or []):
            if not item:
                continue
            title = item.get("title") or (item.get("id") or "（无标题）")
            rec = repeats.get(index) or {}
            plan = rec.get("plan") or {}
            fields = {"title": title}
            # OmniFocus 的 estimatedMinutes 就是「这条活要多久」；导入时落到
            # fields.duration，formatter end 才有东西可排（不像 repeat.duration
            # 只是找 session 的锚点）。用户已经手写过就不覆盖。
            # 来源顺序：重复任务 plan 里的时长 → OmniFocus 查回来的 estimated_minutes。
            minutes = None
            for raw_minutes in (plan.get("duration"), item.get("estimated_minutes")):
                try:
                    minutes = int(raw_minutes)
                except (TypeError, ValueError):
                    minutes = None
                if minutes and minutes > 0:
                    break
            if minutes and minutes > 0:
                fields["duration"] = str(minutes)
            if item.get("project") and not fields.get(pj.PROJECT_FIELD):
                fields[pj.PROJECT_FIELD] = str(item["project"]).strip()
            omnifocus = None
            if item.get("id"):
                omnifocus = {"id": item["id"], "url": item.get("url"),
                             "title": item.get("title")}
            source = ({"kind": "omnifocus", "id": item["id"],
                       "url": item.get("url") or "omnifocus:///task/%s" % item["id"],
                       "title": item.get("title")}
                      if item.get("id") else None)
            task = self.task_store.create(fields, omnifocus=omnifocus,
                                          source=source)
            made.append(task)
            if item.get("id"):
                notes = []
                if item.get("title_from_omnifocus"):
                    notes.append("标题是去 OmniFocus 查的")
                if item.get("from_clipboard"):
                    notes.append("链接是从剪贴板读的")
                self.say("✅ 导入任务 %s「%s」→ OmniFocus %s%s"
                         % (self.task_label(task), title, item["id"],
                            "（%s）" % "；".join(notes) if notes else ""), "ok")
            else:
                self.say("✅ 导入任务 %s「%s」（只有标题：认不出 OmniFocus primary key，"
                         "所以**没有关联**）" % (self.task_label(task), title), "warn")
                self.say("   要连上那条 OmniFocus 任务：回 OmniFocus ⌘C 它，"
                         "再敲 `task %s link`（不带参数就是读剪贴板）。"
                         % self.task_no(task), "info")

            if not copy_repeat or not rec:
                continue
            if not plan.get("supported"):
                self.say("⚠ 任务 %s 是重复任务，但这条规则复制不了：%s"
                         % (self.task_label(task), plan.get("reason") or "规则无法映射"),
                         "warn")
                continue
            try:
                self.task_store.set_repeat(
                    task["id"], plan["weekday_label"], plan["start"],
                    plan.get("duration"),
                    rule=plan.get("rule"),
                    rule_anchor=plan.get("rule_anchor"),
                    rule_offset_days=plan.get("rule_offset_days") or 0,
                    source="omnifocus" if plan.get("rule") else None,
                    summary=plan.get("summary"),
                    meta={k: (rec.get("info") or {}).get(k)
                          for k in ("method", "schedule_type", "anchor_date_key",
                                    "catch_up_automatically", "next")},
                )
            except ValueError as e:
                self.say("⚠ 复制重复设置失败：%s（任务已按普通任务导入）" % e, "warn")
                continue
            task = self.task_store.get(task["id"])
            self.say("🔁 已复制重复设置：任务 %s %s"
                     % (self.task_label(task), plan.get("summary") or ""), "ok")
            if plan.get("note"):
                self.say("   %s" % plan["note"], "info")
        if made:
            self.selected_task_id = made[-1]["id"]
        if len(made) > 1:
            linked = [t for t in made if ts.TaskStore.omnifocus_id(t)]
            copied = [t for t in made if t.get("repeat")]
            self.say("📥 这一批导入 %d 条：关联 OmniFocus %d 条、无关联 %d 条%s"
                     % (len(made), len(linked), len(made) - len(linked),
                        "；复制了重复设置 %d 条" % len(copied) if copied else ""), "ok")
            rl.log_event("omnifocus", "import_batch", count=len(made),
                         linked=len(linked), repeats=len(copied),
                         task_ids=[t.get("id") for t in made],
                         omnifocus_ids=[ts.TaskStore.omnifocus_id(t) for t in made])
            return ImportBatch(made)
        return made[-1] if made else None

    def cmd_task_import(self, args, apply_suffix=True):
        """``task import [--assign N] [粘贴文本|链接]`` —— 从**剪贴板**导入任务。

        需求（第 4 条）：在 OmniFocus 里复制一条任务（剪贴板里自带 primary key 的
        链接），然后 import —— 本地任务**自动关联**到那条 OmniFocus 任务。

        **动作归子命令，后缀归分流**：

            task import                 ← 读剪贴板（默认动作，不需要 flag）
            task import -a 3            ← 同上，顺手分流/挂到表格 #3
            task import -st             ← 只导入这一条，不问重复设置、不建 repeat
            task import [标题](omnifocus:///task/PK)   ← 手动给链接

        为什么不靠粘贴：从 OmniFocus ⌘C 再往终端 ⌘V，粘出来的**只有标题** ——
        终端只接受纯文本那个 flavor，链接在粘贴那一刻就没了。所以 import 自己按
        flavor 去读剪贴板：`the clipboard as URL` / `«class HTML»` / `«class RTF »` /
        纯文本，四个都读、按 id 合并（见 ``omnifocus.clipboard_links``）。

        不带参数就是读剪贴板；粘进来一段**只有标题**的文字时，会去剪贴板里对一下
        标题完全一致的那条链接（那是同一次 ⌘C 的两半，不是拿标题猜身份），对不上
        就建任务但**明确说一句**没关联 —— 宁可少关联，也不拿标题猜身份。

        如果剪贴板那条在 OmniFocus 里带 repetitionRule（每周三之类），**先不建**：
        把候选挂到 ``pending_import``，由打断栏位问「复制重复设置吗？」；用户答
        ``y`` 才把可表达的规则写成本地 ``repeat``，答 ``n`` 只导入这一次。

        **一次导入多条**：在 OmniFocus 里多选几条 ⌘C，剪贴板里就是多条链接。
        这里对每一条都做「关联 + 标题 + 时长」，返回 :class:`ImportBatch`
        （单条时还是那一条任务），``cmd_task`` 的 ``-a`` / ``-f`` 等后缀因此会落到
        整批上。剪贴板给的 flavor 可能只给链接不给标题（多选时 RTF 与纯文本是两份
        互不相干的东西），所以有 pk 没标题的条目会去 OmniFocus 按 pk 查真名
        （顺便带上 ``estimated_minutes`` → ``fields.duration``），补完再按标题去重。
        """
        toks = []
        assign_to = None
        single_task = False
        op_type = None
        advance_pro_id = None
        if self.pending_advance is not None:
            advance_pro_id = self.pending_advance.get("pro_id")
            op_type = self.pending_advance.get("required_op_type")
        raw = [str(a).strip() for a in args]
        i = 0
        while i < len(raw):
            tok, low = raw[i], str(raw[i]).lower()
            if low in ("--single-task", "-st"):
                single_task = True
            elif low in ("--assign", "-a") and i + 1 < len(raw):
                assign_to = raw[i + 1]
                i += 2
                continue
            elif low.startswith("--assign="):
                assign_to = tok.split("=", 1)[1]
            elif low in ("--type", "--kind") and i + 1 < len(raw):
                parsed_t = pj.normalize_op_type(raw[i + 1])
                if parsed_t is None:
                    raise ValueError("OmniFocus 项目（op）类型只能是 SOP 或 incubator；你给的是 %r" % raw[i + 1])
                op_type = parsed_t
                i += 2
                continue
            elif low.startswith("--type=") or low.startswith("--kind="):
                val = tok.split("=", 1)[1]
                parsed_t = pj.normalize_op_type(val)
                if parsed_t is None:
                    raise ValueError("OmniFocus 项目（op）类型只能是 SOP 或 incubator；你给的是 %r" % val)
                op_type = parsed_t
            elif low in ("--sop", "sop"):
                op_type = pj.OP_TYPE_SOP
            elif low in ("--incubator", "incubator", "inc"):
                op_type = pj.OP_TYPE_INCUBATOR
            elif tok:
                toks.append(tok)
            i += 1

        pasted = " ".join(toks).strip()
        read_clipboard = False
        match_clipboard = False
        if not pasted:
            # 空的 items + clipboard 标记：真正读剪贴板放到 worker 里做。
            items = []
            read_clipboard = True
        else:
            items = of.parse_links(pasted) or [of.parse_link(pasted)]
            if not any(item and item.get("id") for item in items):
                # 粘进来的只有标题 → 到 worker 里对剪贴板同名的那条
                # （同一次 ⌘C 的两半）。
                match_clipboard = True

        if single_task:
            self.say("📥 按 -st/--single-task 只导入这一条；不去核对重复设置，"
                     "也不会建本地 repeat。", "cmd")

        # ── 一次操作，两步 ────────────────────────────────────────────
        #   1) [omnifocus] 获取任务   ← 补标题/时长 + 核对重复设置（读）
        #   2) [focusmate] 挂任务     ← 只有 -a 才有（写，乐观）
        # 面板标题是**这个操作**（import 任务（task import -ac）），不是某一步。
        suffix = dict(getattr(self, "_import_suffix_doc", None) or {})
        if assign_to is not None and not suffix:
            suffix = {"assign_to": assign_to}
        title = "import 任务（task import%s）" % self._suffix_summary(suffix)
        self.jobs.begin(2 if suffix.get("assign_to") is not None else 1, title=title)
        action = self.net_action("omnifocus.import", "获取任务",
                                 {"items": items, "single_task": single_task,
                                  "suffix": suffix,
                                  "op_type": op_type,
                                  "advance_pro_id": advance_pro_id,
                                  "clipboard": read_clipboard,
                                  "match_clipboard": match_clipboard,
                                  # 同步模式由 cmd_task 统一套后缀（这条路径原来就这样）
                                  "apply_suffix": False}, source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action)
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return None
            return getattr(self, "_last_import_result", None)
        return NET_PENDING

    @staticmethod
    def _suffix_summary(suffix):
        """后缀 → 面板标题里那一小段（``-a 3 -f end``），只为好看。"""
        suffix = suffix or {}
        bits = []
        if suffix.get("assign_to") is not None:
            bits.append("-a %s" % suffix["assign_to"])
        if suffix.get("applied") is True:          # 内部占位，永不显示
            pass
        if suffix.get("exclusive"):
            bits.append("-e true")
        if suffix.get("note_text"):
            bits.append("-n …")
        formatter = suffix.get("formatter_spec") or {}
        if formatter.get("name"):
            bits.append("-f %s" % formatter["name"])
        mark = suffix.get("mark_status")
        if mark:
            bits.append("-m %s" % mark)
        trigger = suffix.get("trigger_spec") or {}
        if trigger.get("name"):
            bits.append("-t %s" % trigger["name"])
        return (" " + " ".join(bits)) if bits else ""

    def _task_import_omnifocus_rows(self, items, warn=None):
        """这一批 pk → OmniFocus 的行（一次 list，client 里 20 秒缓存）；读不到 None。

        只在 import 里用。读不到不是错误：标题退回剪贴板给的（可能为空），时长就
        不补 —— 导入本身照常进行。
        """
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        pks = {str(item.get("id")) for item in items or () if item.get("id")}
        if not pks:
            return {}
        try:
            enabled = self.omnifocus_enabled()
        except Exception:                          # noqa: BLE001 - 替身/坏设置都不该挡住导入
            enabled = False
        if not enabled:
            if any(item.get("id") and not str(item.get("title") or "").strip()
                   for item in items or ()):
                say("没在读 OmniFocus：剪贴板没给标题的那几条先拿 primary key "
                    "当标题，时长也补不上。", "info")
            return None
        try:
            rows = self.omnifocus.list_tasks()
        except Exception as e:                      # noqa: BLE001 - 读失败不该挡住导入
            say("读 OmniFocus 失败（%s）：标题按剪贴板给的用，时长不补。" % e, "warn")
            return None
        out = {}
        for row in rows or ():
            pk = str(row.get("id") or "")
            if pk and pk in pks:
                out[pk] = row
        return out

    def _task_import_resolve(self, items, warn=None):
        """补齐标题/时长，并把「另一份 flavor 抄了一遍」的裸标题去掉。

        多选 ⌘C 时剪贴板里其实是两份东西：RTF/HTML 给「链接 + 标题」，纯文本把
        那一串标题**再抄一遍**。所以：

        1. 有 pk 但没标题（RTF 只给链接、或 HTML flavor 缺）→ 去 OmniFocus 按 pk
           查真名（``name``），顺手把 ``estimated_minutes`` 带上（导入时落成
           ``fields.duration``，``-f end`` 才有东西可排）；
        2. 标题比 OmniFocus 里的短（flavor 截断，真实反馈过「语法高频 30 个」只给
           「30」）→ 用长的那个；一样长就保留剪贴板的（flavor 优先）；
        3. 补完标题再按「完全相同」去重：纯文本那份裸标题会被清掉，不会 3 条变 6 条。
        """
        items = [item for item in (items or []) if item]
        if not items:
            return []
        rows = self._task_import_omnifocus_rows(items, warn=warn)
        if rows:
            for item in items:
                pk = item.get("id")
                if not pk:
                    continue
                row = rows.get(str(pk))
                if not row:
                    continue
                name = str(row.get("name") or "").strip()
                old = str(item.get("title") or "").strip()
                if name and len(name) > len(old):
                    item["title"] = name
                    item["title_from_omnifocus"] = True
                minutes = row.get("estimated_minutes")
                if minutes not in (None, "", 0, "0"):
                    item["estimated_minutes"] = minutes
                proj_name = str(row.get("project") or "").strip()
                if proj_name and not item.get("project"):
                    item["project"] = proj_name
        return of.drop_duplicate_titles(items)

    def _task_import_skip_known(self, items):
        """去掉「这个 pk 已经和本地某条任务关联过」的条目。

        多选很容易把上个月导入过的那几条再选一遍；重复建任务（同一个 pk 两条本地
        记录）会让两边的写回互相打脸。所以跳过并明说，不静默。返回
        ``(要导入的, [(pk, 已有任务 id), ...])``。

        **只在批量导入里用**（``len(items) > 1``）：单条导入是用户明确要的东西。
        """
        known = {}
        for task in self.task_store.list():
            pk = ts.TaskStore.omnifocus_id(task)
            if pk:
                known.setdefault(str(pk), task)
        kept, skipped = [], []
        for item in items or ():
            pk = str(item.get("id") or "")
            hit = known.get(pk) if pk else None
            if hit is not None:
                skipped.append((pk, hit.get("id")))
                continue
            kept.append(item)
        if skipped:
            self.say("⏭ 跳过 %d 条已经导入过的：%s（想再来一条就 `task <序号> duplicate`）"
                     % (len(skipped),
                        "、".join("OmniFocus %s → 任务 %s" % pair for pair in skipped)),
                     "warn")
        return kept, skipped

    @staticmethod
    def _task_import_match_clipboard(items, clip_items):
        """粘贴的「只有标题」→ 去剪贴板里找同名的那条，认下它的 id。

        只在**标题完全一致**（去掉首尾空白/合并空格后）时才认：这是同一次 ⌘C
        的两半，不是拿标题猜身份。认不出来就原样返回，由调用方明说「没关联」。
        """
        def norm(text):
            return " ".join(str(text or "").split()).strip().lower()

        wanted = [norm((it or {}).get("title")) for it in items if it]
        by_title = {}
        for cand in clip_items or []:
            key = norm(cand.get("title"))
            if key and cand.get("id"):
                by_title.setdefault(key, cand)
        out = []
        for item, key in zip([it for it in items if it], wanted):
            hit = by_title.get(key)
            if hit and not item.get("id"):
                merged = dict(item)
                merged["id"] = hit["id"]
                merged["url"] = hit.get("url")
                merged["title"] = item.get("title") or hit.get("title")
                merged["from_clipboard"] = True
                out.append(merged)
            else:
                out.append(item)
        return out

    # -- 导入：剪贴板里可能是一个 **项目** ---------------------------------

    def _project_repeat_infos(self, payload, warn=None):
        """查项目清单里每条任务的 OmniFocus 重复规则 → ``{pk: info}``。

        重复任务的未来 occurrence 现在还不存在，``list_tasks`` 看不到；必须逐条
        问 ``repetitionInfo``，才能在导入时把「本周会产生几条」算进去。查询失败
        只警告、按普通任务算（和单条导入的既有约定一致），不拿网络故障冒充重复。
        """
        checker = getattr(self.omnifocus, "repetition_info", None)
        if checker is None:
            return {}
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        infos = {}
        for item in (payload or {}).get("tasks") or ():
            pk = str(item.get("pk") or "")
            if not pk or item.get("completed") or item.get("dropped"):
                continue
            try:
                info = checker(pk)
            except Exception as e:              # noqa: BLE001 - 核对不了就按普通任务算
                say("没法核对「%s」是不是重复任务：%s（先按普通任务算）"
                    % (item.get("title") or pk, e), "warn")
                continue
            if isinstance(info, dict) and info.get("repeated"):
                infos[pk] = info
        return infos

    def _task_import_projects(self, items, warn=None):
        """把「剪贴板里其实是 OmniFocus 项目」的条目认出来。

        返回 ``(剩下的普通条目, 项目 payload 列表, 被拒的项目 payload 列表)``。

        判定只靠一件事：这个 primary key 在 OmniFocus 里是一条
        ``kind == "project_root"`` 的行（项目自身的根任务）。**不拿标题猜**——
        猜错了就不是「导入项目」而是「导入了一个同名任务」，两种东西的行为差得远。

        项目里**每条未完成任务都必须有「持续时间」**（OmniFocus 的 estimated
        duration）：没有时长就算不出要占几个 count，而这种项目一旦被唤起就是满地
        0 count 的任务。所以那一条项目整个**拒绝导入**（用户要求：报错，不建任何
        东西），并列出是哪几条要去补。
        """
        say = warn or (lambda text, kind="warn": self.say(text, kind))
        pks = [str(item.get("id")) for item in items or () if item.get("id")]
        if not pks:
            return list(items or ()), [], []
        try:
            enabled = self.omnifocus_enabled()
        except Exception:                          # noqa: BLE001 - 替身/坏设置都不该挡住导入
            enabled = False
        # 链接本身已经说了「这是项目」（omnifocus:///project/PK）：这时候**读不到**
        # OmniFocus 就不能装作没看见 —— 拿不到清单 = 不知道里面有哪几条任务要几个
        # count，硬导进去只会得到一条「叫项目名的任务」。
        looks_project = [item for item in items or ()
                         if str(item.get("kind") or "") == "project"]
        if not enabled:
            if looks_project:
                return list(items or ()), [], [
                    {"title": item.get("title") or item.get("id"),
                     "missing": [],
                     "reason": "「%s」是 OmniFocus **项目**链接，但 OmniFocus 集成关着"
                               "（`set omnifocus on` 打开）—— 读不到它的任务清单，"
                               "没法算 count。" % (item.get("title") or item.get("id"))}
                    for item in looks_project]
            return list(items or ()), [], []
        try:
            rows = self.omnifocus.list_tasks()
        except Exception as e:                      # noqa: BLE001 - 读不到就按普通任务导
            if looks_project:
                return list(items or ()), [], [
                    {"title": item.get("title") or item.get("id"), "missing": [],
                     "reason": "「%s」是 OmniFocus **项目**链接，但读不到 OmniFocus"
                               "（%s）—— 没法核对它的任务清单。" %
                               (item.get("title") or item.get("id"), e)}
                    for item in looks_project]
            say("读 OmniFocus 失败（%s）：没法核对这里面有没有项目，先按普通任务导入。"
                % e, "info")
            return list(items or ()), [], []
        by_pk = {}
        for row in rows or ():
            pk = str(row.get("id") or "")
            if pk:
                by_pk[pk] = row
        kept, projects, refused = [], [], []
        for item in items or ():
            row = by_pk.get(str(item.get("id") or ""))
            if not row or row.get("kind") != "project_root":
                kept.append(item)
                continue
            try:
                payload = pj.project_payload(row, rows)
            except ValueError as e:
                say("认不出这个项目（%s）：先按普通任务导入。" % e, "warn")
                kept.append(item)
                continue
            # 项目当前 active 的任务已经在清单里；重复任务 / 被推迟到本周开放的
            # 任务要靠 repetitionInfo + defer 才能看到，统一算进本周账。
            payload = pj.attach_week(payload,
                                     self._project_repeat_infos(payload, say))
            for note in payload.get("warnings") or ():
                say("⚠ %s" % note, "warn")
            if (payload.get("missing") or payload.get("week_missing")
                    or payload.get("depth_errors")):
                refused.append(payload)
                continue
            projects.append(payload)
        return kept, projects, refused

    # -- 位置：挂上 / 下架 / 调换 / 全量转移 ---------------------------

    @staticmethod
    def _session_kind(token):
        """``-a`` 的取值是不是特殊词：``"none"`` / ``"current"`` / ``""``。"""
        low = str(token or "").strip().lower()
        if low in TASK_SESSION_NONE:
            return "none"
        if low in TASK_SESSION_CURRENT:
            return "current"
        return ""

    def _actual_rows(self, rows):
        """权威数据里只留**真的订上了**的行；快照/离线模式没法看 id，全留。"""
        rows = list(rows or [])
        if not self.authoritative:
            return rows
        return [r for r in rows if r.get("id")]

    def live_session(self):
        """当前**真正在进行**的 session → ``(表格编号, 行)``；没有就 None。

        判定必须同时满足：
        * 权威模式下有 Focusmate session id（不是只有 data.csv 计划窗口）；
        * now 落在这一行自己的 ``startMs`` / ``endMs`` 内。
        取消后另约一场时，旧的 10:30 计划行没有 id，不会被当成当前 session；
        新订的 10:45 行有 id 且时间覆盖现在，才是当前这一场。
        """
        now_ms = int(time.time() * 1000)
        live = [r for r in self._actual_rows(self.rows)
                if r.get("startMs") is not None
                and r["startMs"] <= now_ms < (r.get("endMs") or 0)]
        if not live:
            return None
        # 若真的有两场重叠，取开始更晚的那一场（更接近「刚换到的新约」）。
        live.sort(key=lambda r: (r.get("startMs") or 0, self.rows.index(r)))
        row = live[-1]
        return self.rows.index(row) + 1, row

    def current_session(self):
        """「当前这一场」→ ``(表格编号, 行)``：进行中的优先，其次最近一场还没开始的。

        需求（``task 8 -ac``）：正在干的那一场就是「当前」；一场都不在进行时，
        取**最近一场还没开始、而且真的订上了**的（下一场马上就来，先挂上去就是
        为了别手忙脚乱）。已结束/取消掉、只剩计划窗口的幽灵场不参与判定。
        """
        live = self.live_session()
        if live:
            return live
        now_ms = int(time.time() * 1000)
        upcoming = [r for r in self._actual_rows(self.rows)
                    if (r.get("startMs") or 0) > now_ms]
        upcoming.sort(key=lambda r: (r.get("startMs") or 0, self.rows.index(r)))
        if not upcoming:
            raise ValueError("现在没有正在进行的 session，也没有还没开始的已订场次"
                             "（%s 只有 %d 行）；用表格编号挂吧。"
                             % (self.sel_date.isoformat(), len(self.rows)))
        row = upcoming[0]
        return self.rows.index(row) + 1, row

    def _resolve_assign_target(self, token):
        """``-a`` 的取值 → ``(编号, 行)``；``none`` 返回 ``(None, None)``（下架）。"""
        kind = self._session_kind(token)
        if kind == "none":
            return None, None
        if kind == "current":
            return self.current_session()
        return self.pick(token)

    def _row_for_session(self, session):
        """``task["session"]`` 那条记录 → 当前表格里的行（不在这一天就 None）。"""
        want = ((session or {}).get("date"), (session or {}).get("start"))
        for i, row in enumerate(self.rows, 1):
            if (row.get("date"), row.get("startLabel")) == want:
                return i, row
        return None, None

    def _task_detach(self, task, reason="cmd-down"):
        """把任务从它那一场**下架**：先删 Focusmate 那条 task，再断本地绑定。

        需求（``task 8 -an``）：「手动把它下下来」。顺序不能反 —— 本地先断了、
        Focusmate 那边删失败的话，那条 task 就成了没人认领的孤儿（第二天对账
        还以为它在等你）。所以先删、删成功（或明确报了删不掉）再断本地。
        """
        sess = task.get("session") or {}
        fm = task.get("focusmate") or {}
        if not sess and not (fm.get("task_id") or fm.get("id")):
            self.say("任务 %s 本来就没挂 session（也没 Focusmate task），不用下架。"
                     % self.task_label(task), "info")
            return task
        if self.offline or not (fm.get("task_id") or fm.get("id")):
            # 离线 / 本来就没有 Focusmate task：只改本地，不进队列
            note = "" if self.offline else "（没有 Focusmate task，只断了本地）"
            task = self.task_store.unassign(task["id"], reason=reason)
            self.say("⏏ 任务 %s「%s」已从 %s %s 下架%s"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 40),
                        sess.get("date") or "（没有日期）", sess.get("start") or "",
                        "；" + note if note else ""), "ok")
            return task
        return self.op_task_detach(task, reason=reason)

    def cmd_task_swap(self, args):
        """``task <id> swap <id>`` —— 两条任务**互换位置**（位置 = 那一场 session）。

        需求里的例子：一条挂在 session 1、一条赋闲 → ``task 32 swap 21`` 之后，
        赋闲那条进 session 1，原来那条下来赋闲。两条各在一场时就是场次对调。

        Focusmate 没有「把 task 挪到另一场」这个动作（只能建一条新的），所以做法是：
        两边各自**先下架**（删掉旧场次上那条 task），再挂到对方原来的位置。
        先下架再加挂，顺序反了会在「加挂失败」时留下两条都挂在同一场。

            task 32 swap 21     ← 动作写在 ID 后面（需求里的顺序）
            task swap 32 21     ← 一样的
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if len(toks) < 2:
            raise ValueError("用法：task <任务序号> swap <任务序号>")
        a = self.resolve_task(toks[0])
        b = self.resolve_task(toks[1])
        if a["id"] == b["id"]:
            raise ValueError("swap 要给两条不同的任务（都写成了 %s）"
                             % self.task_label(a))
        sa = a.get("session") or {}
        sb = b.get("session") or {}
        same = (sa and sb and (sa.get("date"), sa.get("start"))
                == (sb.get("date"), sb.get("start")))
        if not sa and not sb:
            self.say("任务 %s 和 %s 都还赋闲（没挂 session）—— 没有位置可换。"
                     % (self.task_label(a), self.task_label(b)), "warn")
            return [a["id"], b["id"]]
        if same:
            self.say("任务 %s 和 %s 都在 %s %s，位置本来就一样，不用换。"
                     % (self.task_label(a), self.task_label(b),
                        sa.get("date") or "?", sa.get("start") or "?"), "info")
            return [a["id"], b["id"]]

        def where(sess):
            return ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?")
                    if sess else "赋闲")

        self.say("互换位置：任务 %s（%s）↔ 任务 %s（%s）"
                 % (self.task_label(a), where(sa), self.task_label(b), where(sb)), "cmd")
        # 1) 两边都先下架：旧场次上那条 Focusmate task 一起删掉，账才干净
        if sa:
            self._task_detach(a, reason="swap")
        if sb:
            self._task_detach(b, reason="swap")
        # 2) 再各自挂到**对方原来的位置**（对方赋闲的那条就留在这里不动）
        done = []
        for task, sess in ((b, sa), (a, sb)):
            if not sess:
                continue
            idx, row = self._row_for_session(sess)
            if row is None:
                self.say("⚠ 任务 %s 原来的位置 %s 不在当前表格里（那天没在显示），"
                         "所以它现在是赋闲的 —— 切到那天再 `task %s -a <编号>`。"
                         % (self.task_label(task), where(sess),
                            self.task_label(task)), "warn")
                continue
            try:
                self._assign_task(task, str(idx), refresh=False)
                done.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡另一条
                self.had_error = True
                self.say("❌ 任务 %s 没能挂回 %s：%s"
                         % (self.task_label(task), where(sess), e), "error")
        self.rebuild_rows()
        idle_now = [t for t, sess in ((a, sb), (b, sa)) if not sess]
        if done:
            moved_txt = "、".join(
                "任务 %s → %s" % (self.task_label(b if tid == b["id"] else a),
                                  where(sa if tid == b["id"] else sb))
                for tid in done)
            idle_txt = ("；赋闲的：" + "、".join(self.task_label(t) for t in idle_now)
                        if idle_now else "")
            self.say("✅ 位置已互换：%s%s" % (moved_txt, idle_txt), "ok")
        return [a["id"], b["id"]]

    def _apply_bulk(self, source_token, assign_to):
        """``task -s <源> [-a <目标>]`` —— 对一场 session 上的**全部**任务办事。

        需求（第 4 条）：``task -s 5 -a 6`` = 把 session 5 的全部任务转移到 6。
        没有 ``-a`` 就只列一下现在挂着谁（只看不动，手滑不会掉东西）。

        每个任务走的是和单条完全一样的路：先下架（删 Focusmate 旧 task）再挂到
        目标场次 —— 所以「转移」之后旧场次上不会留下一条永远不会完成的幽灵 task。
        一条失败不挡其余的，最后报总账。
        """
        src_idx, src_row = self.pick(source_token)
        src_day = src_row.get("date") or self.sel_date.isoformat()
        src_start = src_row.get("startLabel")
        tasks = self.task_store.tasks_for_session(src_day, src_start)
        where = "%s %s" % (src_day, src_start)
        if not tasks:
            self.say("session #%d（%s）上没有挂任务，没什么可办的。" % (src_idx, where), "info")
            return False
        if assign_to is None:
            self.say("session #%d（%s）上挂着 %d 条（没给 -a，只看不动）："
                     % (src_idx, where, len(tasks)), "info")
            for task in tasks:
                self.say("   %s %s%s"
                         % (self.task_label(task),
                            self.task_store.focusmate_title(task, 40),
                            "" if self.task_store.is_open(task) else "（已收工）"))
            self.say("   全量转移用 `task -s %d -a <目标编号>`；全部下架用 "
                     "`task -s %d -a none`。" % (src_idx, src_idx), "info")
            return True

        kind = self._session_kind(assign_to)
        tgt_idx, tgt_row = self._resolve_assign_target(assign_to)
        if tgt_row is not None and ((tgt_row.get("date") or self.sel_date.isoformat(),
                                     tgt_row.get("startLabel")) == (src_day, src_start)):
            self.say("源和目标都是 session #%d（%s），什么也没动。" % (src_idx, where), "info")
            return False
        target_where = "「下架」" if kind == "none" else "%s %s" % (
            tgt_row.get("date") or self.sel_date.isoformat(), tgt_row.get("startLabel"))
        self.say("批量：session #%d（%s）的 %d 条任务 → %s"
                 % (src_idx, where, len(tasks), target_where), "cmd")

        moved, failed = [], []
        for task in tasks:
            try:
                self._task_detach(task, reason="bulk-move")
                if kind == "none":
                    moved.append(task["id"])
                    continue
                self._assign_task(task, str(tgt_idx), refresh=False)
                moved.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                failed.append((task["id"], e))
                self.say("❌ 任务 %s 转移失败：%s"
                         % (self.task_label(task), e), "error")
        self.rebuild_rows()
        if not self.offline:
            try:
                self.refresh(quiet=True)
            except Exception:                          # noqa: BLE001 - 刷新失败不影响已做的
                pass
        self.say("✅ 批量完成：%d 条 → %s%s"
                 % (len(moved), target_where,
                    ("；%d 条没成（见上面）" % len(failed)) if failed else ""),
                 "ok" if not failed else "warn")
        return not failed

    def cmd_task_assign(self, args, refresh=True):
        """task assign <session ID>（默认当前任务）或 task assign <任务ID> <session ID>。

        ``session ID`` 还可以是 ``none``（下架）或 ``current``（当前这一场）：

            task assign none            ← 把选中的任务从它那一场下架
            task 8 assign current       ← 把任务 8 挂到当前这一场
            task 8 -an / task 8 -ac     ← 上面两条的紧凑写法

        写入顺序：先在 Focusmate 给那一场建 task 并复核，成功后才落本地；
        失败不产生半截 assignment。
        """
        if not args:
            raise ValueError("用法：task assign <session ID|none|current>（先 task select/create）")
        if len(args) >= 2:
            task = self.resolve_task(args[0])
            session_token = args[1]
        else:
            task = self._task_for_assign()
            session_token = args[0]
        return self._assign_task(task, session_token, refresh=refresh)

    def _assign_task(self, task, session_token, refresh=True, allow_unbooked=False):
        """把**已经拿到手**的任务挂到某一场（或下架）。内部路径全走这儿。

        ``cmd_task_assign`` 负责「用户敲的那个数 → 任务」，这里负责「任务 → 挂」。
        分开的原因很实际：内部调用方（swap / 批量转移 / refresh --check 重挂）
        手里是任务对象，绕回按序号解析会挂错人。

        ``allow_unbooked`` 只给项目 plan 用：用户拍板把没订上的场次也算进
        空场之后，任务可以**先本地挂上**，等 daemon 订上再由 ``refresh --check``
        补挂 Focusmate；其它命令路径仍然不允许往空气里挂任务。
        """
        # 手上那份可能是「刚创建 / 刚改完备注之前」的快照，挂之前重读一遍：
        # Focusmate 那条 task 的标题和 description 要的是**现在**的字段。
        try:
            task = self.task_store.get(task.get("id"))
        except (ValueError, TypeError):
            pass
        session_idx, row = self._resolve_assign_target(session_token)
        if self._session_kind(session_token) == "current" and row is not None:
            self.say("「当前这一场」= 表格 #%d（%s %s %s）"
                     % (session_idx, row.get("date") or self.sel_date.isoformat(),
                        row.get("startLabel"), row.get("phase") or ""), "info")
        if row is None:
            # `-a none`：下架。它不是一个「场次」，所以不走下面那套建 task 的流程。
            if ts.TaskStore.is_repeat_template(task):
                self.say("🔁 任务 %s 是重复任务本体，本来就不占 session；要结束这套重复："
                         "`task repeat %s off`（保留任务）或 `task %s drop`（手动放弃）。"
                         % (self.task_label(task), self.task_label(task),
                            self.task_label(task)), "info")
                return task
            self.selected_task_id = task["id"]
            task = self._task_detach(task, reason="cmd-assign-none")
            self.rebuild_rows()
            return task
        date_iso = row.get("date") or self.sel_date.isoformat()
        if row.get("id") is None:
            if not allow_unbooked:
                raise ValueError("session #%d 还没有 Focusmate session id（没订上/快照），"
                                 "不能挂 task。先 refresh 成功一次。" % session_idx)
            # 项目 plan 专用：这场还没订上，但用户已经把它算进空场了 —— 任务先
            # 本地记到这场（Focusmate 上没有真实 session，不发起网络挂载），
            # 等订上后 `refresh --check` 会补挂。
            self._exclusive_guard(task, date_iso, row.get("startLabel"))
            self.task_store.assign(task["id"], {
                "index": session_idx,
                "date": date_iso,
                "start": row.get("startLabel"),
                "end": row.get("endLabel"),
                "duration": row.get("durationMin"),
                "focusmate_session_id": None,
            })
            self.say("🗓 任务 %s「%s」先本地挂到 session #%d（%s %s；这一场还没订上，"
                     "等 Focusmate 订上后 refresh --check 会自动补挂）。"
                     % (self.task_label(task),
                        (task.get("fields") or {}).get("title") or self.task_label(task),
                        session_idx, date_iso, row.get("startLabel")), "cmd")
            self.rebuild_rows()
            self.invalidate()
            return task
        if ts.TaskStore.is_repeat_template(task):
            # 本体不能进 session；-a/-ac 指到哪一天，就挂/生成那一天的子任务。
            probe = datetime.date.fromisoformat(date_iso)
            if not ts.TaskStore.repeat_due_on(task, probe):
                raise ValueError("任务 %s 是重复任务本体，而这个 session 在 %s，"
                                 "不是它的规则日；本体不占 session。切到它的规则日再挂，"
                                 "或者直接 `task %s -f end` 让 formatter 排子任务。"
                                 % (self.task_label(task), date_iso,
                                    self.task_label(task)))
            self.ensure_repeat_occurrences(probe, apply_formatters=True)
            child = self.task_store.occurrence_for(task["id"], probe)
            if child is None:
                raise ValueError("任务 %s 的 %s 子任务没生成出来（先看 task sync 的错误）。"
                                 % (self.task_label(task), date_iso))
            self.say("🔁 本体 %s 不占 session；这次挂的是它 %s 的子任务 %s。"
                     % (self.task_label(task), date_iso, self.task_label(child)), "info")
            task = child
        self._exclusive_guard(task, date_iso, row.get("startLabel"))
        session = {
            "index": session_idx,
            "date": date_iso,
            "start": row.get("startLabel"),
            "end": row.get("endLabel"),
            "duration": row.get("durationMin"),
            "focusmate_session_id": row.get("id"),
        }
        fields = task.get("fields") or {}
        title = self.task_store.focusmate_title(task)
        notes = self.task_store.notes(task) or fields.get("description") or ""
        if not isinstance(notes, str):
            notes = str(notes)
        # 这场可能已经挂着别的任务了 —— Focusmate 侧是 n:n，可以再挂一个，
        # 只是 UI 面板里不再出现 "Add task"，所以桥那边默认走内部 API（见 bridge.mjs）。
        prev = task.get("session") or {}
        moved = bool(prev) and (prev.get("date") != date_iso
                               or prev.get("start") != row.get("startLabel"))
        if moved:
            self.say("注意：任务 %s 原来挂在 %s %s；本地会改记到这场，Focusmate 上那条旧 task "
                     "不会跟着挪（完成时间照样会归档）。"
                     % (self.task_label(task), prev.get("date"), prev.get("start")), "warn")
        already = [t for t in self.task_store.tasks_for_session(date_iso, row.get("startLabel"))
                   if t.get("id") != task["id"]]
        self.say("把任务 %s「%s」挂到 session #%d（%s %s）%s…"
                 % (self.task_label(task), title, session_idx, date_iso,
                    row.get("startLabel"),
                    "（这场已有 %d 个任务，再去挂一个）" % len(already) if already else ""), "cmd")
        return self._op_task_assign(task, session, session_idx, date_iso, title, notes,
                                    refresh=refresh, already=len(already),
                                    start_ms=row.get("startMs"))

    def _op_task_assign(self, task, session, session_idx, date_iso, title, notes,
                        refresh=True, already=0, start_ms=None):
        """**乐观**挂任务：本地立刻记上（打 pending 标记），网络丢进后台。

        用户要的就是这个：点完就有反应，别等浏览器。成功了把 pending 换成真
        task id；失败了把整条任务放回操作前的快照，并且记进失败账（re-action）。
        """
        before = copy.deepcopy(task)
        task_id = task["id"]
        self.task_store.assign(task_id, session)
        self.task_store.attach_focusmate(task_id, {
            FM_PENDING: {"at": ts._now(), "via": "optimistic",
                         "op": "task.assign"},
            "session_id": session.get("focusmate_session_id"),
            "title": title,
        }, session=session)
        self.selected_task_id = task_id
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("task.assign",
                                 "挂任务 %s 到 %s %s" % (self.task_label(task), date_iso,
                                                        session.get("start") or "?"),
                                 {"task_id": task_id, "date": date_iso,
                                  "start": session.get("start"),
                                  "duration": session.get("duration"),
                                  "meeting_id": session.get("focusmate_session_id"),
                                  # startMs 不在 session 字典里（那是界面用的行字段），
                                  # 必须作为参数存下来：重放的时候只剩这份参数了
                                  "startMs": start_ms,
                                  "title": title, "notes": notes,
                                  "session": dict(session)})
        job = self.jobs.submit(action, on_fail=lambda error: self._assign_failed(
            task_id, before, title, error))
        if refresh and not self.offline and self.jobs.sync:
            # 只有同步模式才顺手对一次账（老行为）。异步模式表格已经是乐观结果，
            # 再排一次 refresh 只会变成这次操作多出来的一步，还可能把刚铺好的行刷掉。
            try:
                self.refresh(quiet=True)
            except Exception:                       # noqa: BLE001 - 刷新失败不影响已挂上的
                pass
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                raise ValueError("Focusmate 里没能确认 task 已挂上；本地已经回滚（%s）"
                                 % job.outcome.error)
            return True
        self.say("⏳ 任务 %s「%s」已经记到 session #%d 上（Focusmate 那边在后台挂，"
                 "进度看面板）；挂了我会说，没挂上会回滚并记账。"
                 % (self.task_label(task), title, session_idx), "cmd")
        return True

    def _assign_failed(self, task_id, before, title, error):
        """挂失败：回滚到操作前，并且说实话（下一步敲什么也一起说）。"""
        self._revert_task(task_id, before,
                          "任务「%s」没挂到 Focusmate：%s" % (title, error))
        self.say("  下一步：re-action 重放，或者 task %s -a <session> 再挂一次。"
                 % self.task_label(before), "warn")

    def op_task_detach(self, task, reason="net-detach"):
        """**乐观**下架：本地先断绑定，Focusmate 那条 task 在后台删。

        顺序仍然按老规矩办事（先删 Focusmate、再断本地），只是「删」变成了
        后台的一件事：本地先断，失败就整体放回快照（＝那条 task 其实还在）。
        """
        sess = task.get("session") or {}
        fm = task.get("focusmate") or {}
        fm_id = fm.get("task_id") or fm.get("id")
        before = copy.deepcopy(task)
        task_id = task["id"]
        self.task_store.unassign(task_id, reason=reason)
        self.rebuild_rows()
        self.invalidate()
        action = self.net_action("task.detach",
                                 "下架任务 %s" % self.task_label(task),
                                 {"task_id": task_id, "focusmate_task_id": fm_id,
                                  "date": sess.get("date"), "start": sess.get("start"),
                                  "reason": reason})
        job = self.jobs.submit(action, on_fail=lambda error: self._detach_failed(
            task_id, before, error))
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                return self.task_store.get(task_id)
            return self.task_store.get(task_id)
        self.say("⏏ 任务 %s 已经从 %s %s 下架（Focusmate 那边在后台删这条 task）"
                 % (self.task_label(task), sess.get("date") or "?",
                    sess.get("start") or "?"), "ok")
        return self.task_store.get(task_id)

    def _detach_failed(self, task_id, before, error):
        """删失败：把绑定放回去（那条 task 还在 Focusmate 上，账不能先断）。"""
        self._revert_task(task_id, before, "Focusmate task 没删掉：%s" % error)

    @staticmethod
    def _hhmm_distance(a, b):
        """两个 HH:MM 差多少分钟（认不出就当无穷远）。"""
        pa, pb = sp.parse_hhmm(a), sp.parse_hhmm(b)
        if pa is None or pb is None:
            return 10 ** 6
        return abs((pa[0] * 60 + pa[1]) - (pb[0] * 60 + pb[1]))

    def _repeat_session_for(self, parent, day, sessions=None):
        """本体在 ``day`` 优先挂哪一场 → ``(session, 原因)``。

        先找同起点、同时长的精确落点；找不到就选当天离 ``repeat.start`` 最近的
        一场 —— 用户明确说过「这一天具体什么时候不重要」，所以生成任务不因为
        排布被拆开就消失。当天完全没有 session 时返回 ``(None, ...)``，
        occurrence 子任务仍然会生成，只是暂时不挂场。
        """
        sessions = list(sessions if sessions is not None
                        else self.sessions_for_day_plan(day))
        # 今天已经开始的场次，只有 API 里真有 id 才能拿来自动挂重复任务。
        # 否则一次日终/次日重开 TUI 会把今天的新 occurrence 硬塞回已经过去的
        # 19:00 幽灵场（data.csv 有、Focusmate 早已没有），看起来就像任务又
        # “留在取消的 session 上”。
        if day <= datetime.date.today():
            now = self._now_dt()
            usable = []
            for sess in sessions:
                start = self._session_start_datetime(sess)
                if start is None or start > now:
                    usable.append(sess)
                    continue
                if self.authoritative and self._formatter_session_has_actual(day, sess):
                    usable.append(sess)
            sessions = usable
        hit = self.task_store.repeat_occurrence(parent, day, sessions)
        if hit:
            return hit, "exact"
        rep = parent.get("repeat") or {}
        want = rep.get("start")
        best = None
        best_key = None
        for sess in sessions or []:
            key = (self._hhmm_distance(want, sess.get("start")), sess.get("start") or "")
            if best_key is None or key < best_key:
                best, best_key = sess, key
        if best:
            return best, "nearest"
        return None, "no-session"

    def _repeat_formatter_done(self, child, day):
        """子任务在 ``day`` 上是不是已经跑过 formatter（有当天的 reservation）。"""
        fmt = (child or {}).get("formatter") or {}
        reservation = fmt.get("reservation") or {}
        return reservation.get("date") == day.isoformat()

    def _format_repeat_child(self, parent, child, day, quiet=True):
        """把本体上的 formatter 模板落到一条子任务上；失败只警告、不掀 TUI。"""
        template = ts.TaskStore.hook_template(parent.get("formatter"))
        if not template:
            return child
        current = ts.TaskStore.hook_template(child.get("formatter"))
        if current != template:
            child = self.task_store.set_hook(child["id"], "formatter", template)
        try:
            return self._apply_formatter_to_task(
                child, template, action="repeat", day=day, quiet=quiet)
        except Exception as e:                          # noqa: BLE001 - 一条排不上不该挡别的
            self.had_error = True
            self.say("⚠ 重复任务子任务 %s 的 formatter %s 没排上：%s"
                     % (self.task_label(child), template.get("name") or "?", e), "warn")
            return self.task_store.get(child["id"])

    def ensure_repeat_occurrences(self, day=None, apply_formatters=True,
                                  force_formatters=False, only_formatted=False):
        """生成 ``day`` 这一天该有的重复子任务（本地；formatter 可能碰 Focusmate）。

        本体（带 ``repeat``、没有 ``repeat_parent``）永远不挂 session；每到一个
        规则日就生成一条子任务（``repeat_parent`` + ``repeat_date``），并把
        ``repeat.occurrences[日期]`` 记到本体上。子任务能挂到当天 session 就挂，
        挂不到也照建 —— 晚点用户手动 assign 或 task sync 再补。

        本体挂了 formatter 时，子任务创建后自动继承并**当场排一遍**（``days``
        只看今天/以后）。``force_formatters=True`` 用于用户刚改了本体的 formatter，
        要求连已经排过的子任务也重排。``only_formatted=True`` 只处理挂了 formatter
        的本体（TUI 每天会顺手把明天那条也准备好，让用户今天就能看到）。
        """
        day = day or self.sel_date
        iso = day.isoformat()
        sessions = self.sessions_for_day_plan(day)
        # 旧数据里的本体可能带着 session：先把那一场转给当天的子任务。
        for candidate in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(candidate):
                continue
            if ((candidate.get("session") or {}).get("date") or "") == iso:
                self._adopt_template_session(candidate)
        changed = []
        today = datetime.date.today()
        for parent in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(parent):
                continue
            if not self.task_store.is_open(parent):
                # 本体被手动放弃/完成（结束这套重复）后就不再生成子任务。
                continue
            if only_formatted and not parent.get("formatter"):
                continue
            if not ts.TaskStore.repeat_due_on(parent, day):
                continue
            session, why = self._repeat_session_for(parent, day, sessions)
            child = self.task_store.occurrence_for(parent["id"], day)
            if child is None:
                child = self.task_store.create_repeat_occurrence(
                    parent["id"], day, session=session)
                changed.append(child)
            elif session and not (child.get("session") or {}):
                # 之前生成时没有 session，现在排出来了 → 补挂（只补空的，
                # 不覆盖用户手动挪到别的场次）。
                child = self.task_store.assign(child["id"], session)
                changed.append(child)
            # formatter 属于**子任务**：本体只留模板。今天及以后自动排；
            # 过去那天不补排（formatter 不允许排到过去）。
            if (apply_formatters and day >= today and parent.get("formatter")
                    and (force_formatters or not self._repeat_formatter_done(child, day))):
                before = child.get("formatter") or {}
                child = self._format_repeat_child(parent, child, day, quiet=True)
                if (child.get("formatter") or {}) != before:
                    changed.append(child)
            occ = ((parent.get("repeat") or {}).get("occurrences") or {}).get(iso) or {}
            target = child.get("session") or session
            fm = child.get("focusmate") or {}
            fm_id = fm.get("task_id") or fm.get("id")
            occ_fm = (occ.get("focusmate") or {}).get("task_id")
            if (occ.get("child_id") != child["id"]
                    or (target and occ.get("session") != target)
                    or (fm_id and occ_fm != fm_id)):
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=target,
                    focusmate=fm or None, note=why)
        return changed

    def expire_stale_repeat_occurrences(self, today=None):
        """把**已经过期、从未处理过**的重复 occurrence 收掉，避免它们天天复活。

        例如 FREQ=DAILY 的模板在 11-04 / 11-05 各生成过一条子任务，但用户那两天
        没做也没收尾；它们会一直是 active。以前 refresh 会把这些旧子任务也当成
        「今天要排的 formatter 任务」，于是一次 refresh 就出现好几条同名任务抢同一场
        session。正确口径是：**一条 occurrence 只属于它的 repeat_date**；日期过了、
        又没有处理痕迹，就记一笔 missed 并放弃（模板本身照常生成今天的新 occurrence）。

        只处理有 active 重复模板的旧子任务；模板已经删除/结束的孤儿记录不动。
        返回被收掉的 ``[(old_task, parent_id), ...]``。
        """
        today = today or datetime.date.today()
        expired = []
        for task in list(self.task_store.list()):
            if not ts.TaskStore.is_repeat_occurrence(task):
                continue
            if not self.task_store.is_open(task):
                continue
            raw_day = str(task.get("repeat_date") or "").strip()
            if not raw_day:
                continue
            try:
                repeat_day = datetime.date.fromisoformat(raw_day)
            except ValueError:
                continue
            if repeat_day >= today:
                continue
            parent_id = task.get("repeat_parent")
            try:
                parent = self.task_store.get(parent_id)
            except (TypeError, ValueError):
                continue
            if not (ts.TaskStore.is_repeat_template(parent)
                    and self.task_store.is_open(parent)):
                continue
            # 用户在那一天明确处理过（收尾里选了 skip / 延期等）就尊重历史，
            # 不在这里二次改账；这里只收「完全没人碰过」的僵尸 occurrence。
            if self.task_store.processed_on(task, raw_day):
                continue
            stamp = datetime.datetime.combine(
                repeat_day, datetime.time(23, 59, 59)).isoformat(timespec="seconds")
            try:
                self.task_store.mark_processed(
                    task["id"], repeat_day, action="missed", at=stamp)
                # 旧 bug 可能已经把这场旧 occurrence 拖到今天并挂在某场幽灵
                # session 上。收掉时一并断绑；否则它还会以 ✗ 的身份留在今天
                # 的契约行里，看起来像“任务没被移走”。
                try:
                    task = self.task_store.unassign(
                        task["id"], reason="repeat-missed")
                except (ValueError, TypeError):
                    pass
                task = self.task_store.drop(
                    task["id"], reason="repeat-missed", at=stamp)
            except (ValueError, TypeError):
                continue
            expired.append((task, parent_id))
        if expired:
            names = "、".join(
                "id:%s@%s" % (task.get("id"), task.get("repeat_date"))
                for task, _parent in expired[:5])
            self.say("🔁 收掉 %d 条过期未处理的重复 occurrence（%s）；"
                     "模板会照常生成今天的新一条。" % (len(expired), names), "warn")
            self.invalidate()
        return expired

    def maybe_generate_repeat_occurrences(self):
        """TUI 每帧问一次；每天只自动扫一遍（外加 60 秒节流）。"""
        today = datetime.date.today()
        now = time.time()
        if (self._repeat_gen_day == today
                and now - self._repeat_gen_at < 60.0):
            return []
        self._repeat_gen_day = today
        self._repeat_gen_at = now
        try:
            expired = self.expire_stale_repeat_occurrences(today)
            created = self.ensure_repeat_occurrences(today)
            if expired:
                created = [t for t, _parent in expired] + list(created or [])
            # 挂了 formatter 的本体：顺手把明天那条也生成/排好，用户今天就能在
            # `day +1` 里看到「明天会被 format 成什么样」。没挂 formatter 的
            # 本体不提前生成（仍然等到那天）。
            has_formatter = any(
                ts.TaskStore.is_repeat_template(t) and t.get("formatter")
                and self.task_store.is_open(t)
                for t in self.task_store.repeated_tasks())
            if has_formatter:
                created += self.ensure_repeat_occurrences(
                    today + datetime.timedelta(days=1), only_formatted=True)
        except Exception as e:                          # noqa: BLE001 - 生成失败不该拖死看板
            self.had_error = True
            self.say("重复任务生成失败：%s" % e, "warn")
            return []
        if created:
            self.rebuild_rows()
            self.invalidate()
        return created

    def materialize_repeats(self):
        """把选中那天的重复子任务挂上 Focusmate（本地 occurrence 先由 ensure 生成）。

        日期只看 RRULE / weekday；具体挂哪一场由 :meth:`_repeat_session_for`
        决定（精确匹配优先，其次当天最近一场）。Focusmate 写入必须复核成功才
        落本地，失败就把错误抛给调用方，绝不假成功。
        """
        day = self.sel_date
        iso = day.isoformat()
        self.ensure_repeat_occurrences(day)
        self.rebuild_rows()
        done = []
        for parent in self.task_store.repeated_tasks():
            if not ts.TaskStore.is_repeat_template(parent):
                continue
            if not ts.TaskStore.repeat_due_on(parent, day):
                continue
            child = self.task_store.occurrence_for(parent["id"], day)
            if child is None:
                continue
            base = child.get("session") or {}
            if not base:
                continue
            row = next((r for r in self.rows
                        if r.get("planned") and r["startLabel"] == base.get("start")), None)
            session = dict(base)
            session.update({
                "index": self.rows.index(row) + 1 if row in self.rows else base.get("index"),
                "date": iso,
                "start": base.get("start"),
                "end": base.get("end"),
                "duration": base.get("duration"),
                "focusmate_session_id": (row or {}).get("id") or base.get("focusmate_session_id"),
            })
            occ = ((parent.get("repeat") or {}).get("occurrences") or {}).get(iso) or {}
            already = occ.get("focusmate") or {}
            if already.get("task_id"):
                continue
            if row is not None and row.get("id") and not self.offline:
                title = self.task_store.focusmate_title(child)
                notes = (child.get("fields") or {}).get("notes") or ""
                # 和 task <序号> -ac 同一条异步路径：本地先挂 pending，Focusmate
                # 建 task 丢后台。sync 模式（测试 / --exec）仍当场复核完。
                try:
                    self._op_task_assign(
                        child, session, self.rows.index(row) + 1, iso, title, notes,
                        refresh=False, start_ms=row.get("startMs"))
                except ValueError:
                    # 同步模式下没复核成功：把错误往上传，cmd_task_sync 会报出来。
                    raise
                current = self.task_store.get(child["id"])
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=session,
                    focusmate=current.get("focusmate") or None)
            else:
                self.task_store.assign(child["id"], session)
                self.task_store.record_occurrence(
                    parent["id"], day, child_id=child["id"], session=session,
                    focusmate=None)
            done.append((child["id"], session["start"]))
        return done

    def cmd_task_sync(self, quiet=False):
        """GET /v1/tasks → 读 completedAt，给已完成任务写归档。

        顺带确保选中那天的重复子任务已经生成；子任务有 session 的再挂 Focusmate。

        **一次操作、至少两步**：`[focusmate] 拉 Focusmate task` →
        `[omnifocus] 写回 OmniFocus`（有要写回的才排这一步）。
        """
        self.jobs.begin(1, title="同步 task（task %s）"
                               % ("sync" if not quiet else "sync · 自动"))
        if not self.offline:
            try:
                for task_id, start in self.materialize_repeats():
                    self.say("🔁 任务 %s 已挂到 %s %s"
                             % (self.task_label(self.task_store.get(task_id)),
                                self.sel_date, start), "ok")
            except Exception as e:                      # noqa: BLE001
                self.had_error = True
                self.say("重复任务挂载失败：%s" % e, "warn")
        ok = self.sync_task_completions(quiet=quiet)
        # 完成写回 OmniFocus 已经在那里面做了（sync_task_completions 末尾），
        # 这里只补一句「到底关联上了几条」。
        if not quiet:
            self.report_omnifocus_links()
        return ok

    @staticmethod
    def _focusmate_task_summary(remote):
        """日志用：Focusmate task 拉取结果摘要（不记 token，只记任务事实）。"""
        rows = remote if isinstance(remote, list) else []
        done = [r for r in rows if isinstance(r, dict)
                and (r.get("completedAt") or r.get("completed_at") or r.get("completed"))]
        return {
            "count": len(rows),
            "completed_count": len(done),
            "completed": [{
                "id": r.get("id") or r.get("taskId"),
                "title": r.get("title"),
                "completed": bool(r.get("completed")),
                "completedAt": r.get("completedAt") or r.get("completed_at"),
                "attachments": r.get("attachments") or [],
            } for r in done],
        }

    def sync_task_completions(self, quiet=True):
        """``GET /v1/tasks`` → 对齐完成状态（**读**操作，后台跑）。

        ``refresh`` / ``task sync`` / 收尾都走它。和 :meth:`cmd_task_sync` 的区别
        只有一件事：这里不往 Focusmate 写 —— 不把「每周重复」落到焦点场次上
        （那会真的在 Focusmate 建 task）。``refresh`` 要能随时敲，不能敲一下
        就在外面多出一条任务。

        **但 OmniFocus 的完成写回会做**（在 :meth:`_apply_task_pull` 里，主线程）。
        「在 Focusmate 勾掉了」是一条已经发生的事实，不是 warden 的新决定：
        本地跟着打 ✓ 的同时，OmniFocus 那条也得完成（幂等，写过的记在 writeback）。
        """
        # 注：这里**不**看 self.offline —— 「离线」只挡 refresh 那种自己发起的拉取；
        # 用户显式敲 `task sync` 就是要求去对一次账（测试也靠这条路径）。
        action = self.net_action("task.pull", "拉 Focusmate task", {})
        job = self.jobs.submit(action, persist=False)
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True

    def report_omnifocus_links(self):
        """说清楚「OmniFocus 那边到底能不能核对」—— 一条都没关联就别让人瞎猜。"""
        tasks = self.task_store.list()
        linked = [t for t in tasks if ts.TaskStore.omnifocus_id(t)]
        if linked:
            done = sum(1 for t in linked if not self.task_store.is_open(t))
            self.say("OmniFocus 关联：%d/%d 条任务挂了 primary key（其中 %d 条已收工）。"
                     % (len(linked), len(tasks), done), "info")
        else:
            self.say("OmniFocus 关联：0/%d 条 —— 一条都没挂 primary key，所以核对不到"
                     " OmniFocus 的完成状态。用 `task import` 粘 omnifocus:///task/… 链接，"
                     "或 `task <id> link <pk>`。" % len(tasks), "warn")
        if not self.omnifocus_enabled():
            self.say("（OmniFocus 现在是关着/不可用：设置 omnifocus=off 或没给自动化权限，"
                     "这一侧一律跳过。）", "warn")
        return len(linked)

    def announce_completions(self, since, limit=3):
        """刚归档的那几条任务，把信息（含备注）说一遍。

        需求背景：备注里可以塞网址，完成之后照着去对应地方把活干完 ——
        所以完成的第一时间就把备注亮出来，而不是等用户自己去翻归档。
        """
        records = self.task_store.load_archive().get("records") or []
        fresh = [r for r in records[since:] if isinstance(r, dict)]
        for rec in fresh[:limit]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            fields = rec.get("fields") or {}
            title = (fields.get("title") or fields.get("name")
                     or self.task_store._one_line(fields)[:60] or "（空任务）")
            try:
                live = self.task_store.get(rec.get("task_id"))
            except (ValueError, TypeError):
                live = None
            line = "✅ 完成：任务 %s「%s」· 完成时间 %s · session %s %s" % (
                self.task_label(live) if live else "id %s" % (rec.get("task_id") or "?"),
                title,
                fm.get("completed_at") or "?", sess.get("date") or "?",
                sess.get("start") or "?")
            note = rec.get("notes") or self.task_store.notes({"fields": fields})
            if note:
                line += "\n     备注：%s" % note
            self.say(line, "ok")
        if len(fresh) > limit:
            self.say("…还有 %d 条完成归档（「任务」视图 / task archive 看全部）"
                     % (len(fresh) - limit), "info")
        return fresh

    # -- OmniFocus 联动（**单向**：warden → OmniFocus）------------------
    #
    # 契约（见 docs/omnifocus.md）：
    #   * 任务被挂到一场 session 上时，截止时间＝那场的终点（task_store.assign 写的）；
    #   * 「完成」的时候把完成 + 这条截止时间写回 OmniFocus（一条命令，见 set --due）；
    #   * 「放弃」的时候把 OmniFocus 也放弃；
    #   * 「延期 / 快捷推后 / 重议」**不动** OmniFocus（活还在，只是本地换了个账本）；
    #   * 反向没有：OmniFocus 里的改动不会回流成 warden 的命令，只在收尾时被**读**一次。
    #
    # 所有写操作都是子进程（omnifocus.py → omnifocus_tasks.py → osascript），
    # 失败只变成一句话，绝不把 TUI 掀了。

    def omnifocus_enabled(self):
        return self.settings.get_bool("omnifocus") and self.omnifocus.available()

    def omni_due_of(self, task):
        """这条任务该写回 OmniFocus 的截止时间：``fields.due``，否则由 session 推出来。"""
        fields = task.get("fields") or {}
        due = str(fields.get("due") or "").strip()
        if due:
            return due
        return ts.session_due(task.get("session") or {}) or ""

    @staticmethod
    def _omni_log_fields(task, pk=None):
        fields = task.get("fields") or {}
        omni = task.get("omnifocus") or {}
        return {
            "task_id": task.get("id"),
            "task_status": task.get("status"),
            "task_title": fields.get("title") or fields.get("name") or "",
            "pk": pk or omni.get("id"),
            "due": fields.get("due") or "",
            "session": task.get("session") or None,
            "writeback_before": dict(omni.get("writeback") or {}),
        }

    def _omni_repeat_advance(self, task, pk, res):
        """写完之后「OmniFocus 那条是重复任务，已经翻到下一次」这种情况。

        ``omnifocus_tasks.py set --complete`` 在重复任务上返回的 ``after`` 是**新
        那一次**的快照（``completed`` 当然是 false），同时把新 primary key 放在
        ``repeat_clone_id`` 里。以前这里只把它写进日志、当没看见 —— 后果是：

        * 复核 `after.completed` 判成「写回失败」→ 不记 writeback → 下一次同步
          再写一遍 → OmniFocus 的重复表被一次次往前推；
        * 本地还开着的记录全挂在**旧 key**（已经完成的那一次）上，下一次的活
          在 OmniFocus 里永远勾不掉 —— 用户看到的正是「本地 ✓ 了、OmniFocus
          那条还没完成」。

        返回 ``(advanced, moved)``：真的翻篇了没有、跟着改挂的本地任务 id。
        """
        if not isinstance(res, dict):
            return False, []
        clone = str(res.get("repeat_clone_id") or "").strip()
        if not clone or clone == str(pk):
            return False, []
        try:
            moved = self.task_store.advance_omnifocus(pk, clone,
                                                      exclude_task_id=task.get("id"))
        except (ValueError, TypeError):
            moved = []
        return True, moved

    @staticmethod
    def _omni_repeat_note(advanced, clone, moved):
        if not advanced:
            return ""
        return "；OmniFocus 那是重复任务，已经翻到下一次 %s，%d 条还没收工的本地记录改挂过去了" % (
            clone, len(moved))

    def omni_complete(self, task, minutes=None, at=None):
        """OmniFocus：完成 + 把截止时间写回 session 终点。返回一句人话。

        **写**操作，走后台队列：本地先记「已经写回过完成」（乐观），
        osascript 那一趟在 worker 里跑；失败就回滚这个标记 + 记账（re-action）。
        """
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        # 项目唤起出来的任务可能只覆盖那条 OmniFocus 任务的一部分 count：
        # 这时候勾完成等于把剩下的 count 抹掉，所以挡一下（见 project_store）。
        try:
            allowed, why = pj.completion_guard(task, self.projects, self.task_store)
        except Exception:                       # noqa: BLE001 - 账算不出来不该挡住完成
            allowed, why = True, ""
        if not allowed:
            rl.log_event("omnifocus", "complete_blocked", reason="project-partial",
                         pk=pk, task_id=task.get("id"))
            return why
        enabled = self.omnifocus_enabled()
        fields = self._omni_log_fields(task, pk)
        due = self.omni_due_of(task)
        rl.log_event("omnifocus", "complete_start", minutes=minutes, at=at,
                     enabled=enabled, api_due=due or None, **fields)
        if not enabled:
            rl.log_event("omnifocus", "complete_skipped", reason="disabled_or_unavailable",
                         **fields)
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        before = copy.deepcopy(task)
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "completed")
        except ValueError:
            pass
        action = self.net_action("omnifocus.complete",
                                 "OmniFocus 完成 %s" % pk[:8],
                                 {"task_id": task["id"], "pk": pk, "at": at,
                                  "due": due or None}, source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self._omni_failed(
            task["id"], before, "OmniFocus 写回完成（pk %s）" % pk[:8], error))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 写回失败：%s" % (outcome and outcome.error or "没跑")
            res = (outcome.result or {}).get("res") or {}
            changed = ", ".join(res.get("changed") or [])
            return "OmniFocus %s 完成%s" % (pk, ("（%s）" % changed) if changed else "")
        return "OmniFocus 完成写回丢后台了（%s）" % pk[:8]

    def _omni_failed(self, task_id, before, what, error):
        """OmniFocus 写失败：把本地标记放回去 + 说清楚（账已经记下了）。"""
        self._revert_task(task_id, before, "%s 没写成：%s" % (what, error))

    def omni_drop(self, task, at=None, all_occurrences=False):
        """OmniFocus：放弃这条任务（只给「真的不要了」用）。

        ``all_occurrences`` 给重复任务本体用：放弃「这一套」而不是「这一次」，
        否则 OmniFocus 会照着重复规则继续生出下一次。

        **写**操作，走后台队列（乐观 → 失败回滚 + 记账）。
        """
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        enabled = self.omnifocus_enabled()
        fields = self._omni_log_fields(task, pk)
        rl.log_event("omnifocus", "drop_start", at=at, enabled=enabled,
                     all_occurrences=all_occurrences, **fields)
        if not enabled:
            rl.log_event("omnifocus", "drop_skipped", reason="disabled_or_unavailable",
                         **fields)
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        before = copy.deepcopy(task)
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "dropped")
        except ValueError:
            pass
        action = self.net_action("omnifocus.drop",
                                 "OmniFocus 放弃 %s" % pk[:8],
                                 {"task_id": task["id"], "pk": pk, "at": at,
                                  "all_occurrences": bool(all_occurrences)},
                                 source=SRC_OMNIFOCUS)
        job = self.jobs.submit(action, on_fail=lambda error: self._omni_failed(
            task["id"], before, "OmniFocus 放弃（pk %s）" % pk[:8], error))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is None or not outcome.ok:
                return "OmniFocus 放弃失败：%s" % (outcome and outcome.error or "没跑")
            res = (outcome.result or {}).get("res") or {}
            changed = ", ".join(res.get("changed") or [])
            return "OmniFocus %s 已放弃%s" % (pk, ("（%s）" % changed) if changed else "")
        return "OmniFocus 放弃丢后台了（%s）" % pk[:8]

    def fm_complete(self, task):
        """Focusmate：把这条 task 勾完成（走 bridge 的内部 API，GET 复核 completedAt）。"""
        fm = task.get("focusmate") or {}
        fm_task_id = fm.get("task_id")
        if not fm_task_id:
            return ""
        if self.offline:
            rl.log_event("focusmate", "complete_skipped", task_id=task["id"],
                         focusmate_task_id=fm_task_id, reason="offline")
            return "离线模式：没去勾 Focusmate"
        # **乐观**：本地立刻记「已勾」（打 pending 标记），网络丢进后台。
        # 勾失败就把这个字段放回去，并且记账（re-action 重放）。
        before = copy.deepcopy(task)
        local_id = task["id"]
        try:
            self.task_store.attach_focusmate(local_id, {
                FM_PENDING: {"at": ts._now(), "via": "optimistic", "op": "task.complete"},
                "completed_at": ts._now(),
            })
        except ValueError:
            pass
        action = self.net_action("task.complete",
                                 "勾完成 %s" % self.task_label(task),
                                 {"task_id": local_id, "focusmate_task_id": fm_task_id})
        job = self.jobs.submit(action, on_fail=lambda error: self._complete_failed(
            local_id, before, error))
        if self.jobs.sync:
            if job.outcome is not None and not job.outcome.ok:
                return "Focusmate 勾完成失败：%s" % job.outcome.error
            return "Focusmate 已勾完成"
        return "Focusmate 勾完成丢后台了（进度看面板）"

    def _complete_failed(self, task_id, before, error):
        """勾失败：把 completed_at 放回去（本地别先替 Focusmate 宣布完成）。"""
        self._revert_task(task_id, before, "Focusmate 没勾上：%s" % error)

    def fm_delete_task(self, task):
        """Focusmate：删掉这条任务在 Task Manager 里那条 task（收尾「转移」时用）。

        需求：「任务转移之后 focusmate 任务删除」。转移的语义是**这一场不干了**
        —— 旧任务标放弃、复制品挂到下一场。可 Focusmate 那边那条 task 还挂在
        旧场次上：既不会完成，又一直躺在 Task Manager 里，第二天对账还会以为
        它在等你。删掉它，账才干净（OmniFocus 那条照旧一个字节都不动）。

        返回一句人话（收尾面板会显示），删不掉也**不挡**转移本身。
        """
        fm = task.get("focusmate") or {}
        task_id = fm.get("task_id") or fm.get("id")
        if not task_id:
            return "（这条没有 Focusmate task，不用删）"
        if self.offline:
            return "离线模式：没去删 Focusmate task %s" % str(task_id)[:8]
        try:
            res = self.bridge.call("delete_task", timeout=180, taskId=task_id)
        except BridgeError as e:
            self.had_error = True
            return "Focusmate task 删除失败：%s" % e
        if not res.get("verified"):
            self.had_error = True
            still = (res.get("stillThere") or {}).get("title") or ""
            return "Focusmate 说删了，但复核还在%s（去 Task Manager 手动删一下）" % (
                ("：" + still) if still else "")
        try:
            self.task_store.forget_focusmate(task["id"], reason="transfer")
        except ValueError:
            pass
        return "Focusmate task %s 已删除" % str(task_id)[:8]

    def sync_omnifocus_status(self):
        """读一遍 OmniFocus 的完成状态，返回 ``{本地任务 id: OmniFocus 行}``。

        只在收尾的核对里调用（一次 list 拿到全部，缓存在 client 里）。
        读不到就返回空 dict —— 收尾照常进行，只是少了「OmniFocus 那边也勾了」这条信息。
        """
        started = time.monotonic()
        enabled = self.omnifocus_enabled()
        rl.log_event("omnifocus", "status_pull_start", enabled=enabled)
        if not enabled:
            rl.log_event("omnifocus", "status_pull_skipped", reason="disabled_or_unavailable")
            return {}
        rows = None
        try:
            rows = self.omnifocus.list_tasks(refresh=True)
        except of.OmniFocusError as e:
            self.had_error = True
            rl.log_event("omnifocus", "status_pull_error", kind=e.kind,
                         exit_code=e.exit_code, detail=e.detail, message=str(e),
                         elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            self.say("读 OmniFocus 失败：%s" % e, "warn")
            return {}
        by_id = {}
        for row in rows or []:
            if row.get("id"):
                by_id[str(row["id"])] = row
        tasks = self.task_store.list()
        # 同一条 OmniFocus 任务在本地可能挂着好几条记录（转移留下的旧记录、重复任务
        # 的子任务）。其中一条要是「**我们自己写回**完成的」，那这条 OmniFocus 的
        # completed 就已经有归属了 —— 别再拿它去把**还开着**的兄弟记录也带成 ✓：
        # 重复任务今天这条写完回，明天那条会被判成「已完成」，活还没干就没了。
        claimed = set()
        for task in tasks:
            if ts.TaskStore.is_repeat_template(task):
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk:
                continue
            wb = (task.get("omnifocus") or {}).get("writeback") or {}
            if (self.task_store.status_of(task) == ts.STATUS_COMPLETED
                    and wb.get("completed")):
                claimed.add(str(pk))
        out = {}
        held = []
        for task in tasks:
            if ts.TaskStore.is_repeat_template(task):
                # 本体不是一次可完成的活；同一条 OmniFocus 的完成状态只对齐到子任务。
                continue
            pk = ts.TaskStore.omnifocus_id(task)
            if not pk or str(pk) not in by_id:
                continue
            if str(pk) in claimed and self.task_store.is_open(task):
                held.append(task["id"])
                continue
            out[task["id"]] = by_id[str(pk)]
        done = [{
            "task_id": task_id,
            "pk": row.get("id"),
            "name": row.get("name"),
            "completed": bool(row.get("completed")),
            "completed_at": row.get("completed_at"),
        } for task_id, row in out.items()]
        rl.log_event(
            "omnifocus", "status_pull_result", row_count=len(rows or []),
            linked_count=len(out), completed_count=sum(1 for x in done if x["completed"]),
            claimed_pks=sorted(claimed), held_task_ids=held,
            linked=done, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return out

    def writeback_completions(self, tasks=None):
        """把「本地已完成、但还没写回 OmniFocus」的任务写回去（幂等，靠 writeback 标记）。

        需求（第 2 条）：任务被分配到的 session 时间＝它的截止时间；完成的时候
        把这条截止时间一起写回 OmniFocus —— 于是 OmniFocus 里记的是「这一场是什么时候结束的」。

        **写**操作，走后台队列：一次子进程一条任务，全部塞进**同一个** job 里跑
        （进度是「第几条/共几条」），本地 writeback 标记在 apply 里落定。
        这一批会**并进当前那一步操作**（refresh / task sync），不会另起一个标题。
        """
        if not self.omnifocus_enabled():
            rl.log_event("omnifocus", "writeback_scan_skipped",
                         reason="disabled_or_unavailable")
            return []
        todo = []
        for task in (tasks if tasks is not None else self.task_store.list()):
            if ts.TaskStore.is_repeat_template(task):
                # 本体不可完成；它的子任务共享同一条 OmniFocus 链接，写回由子任务做。
                continue
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            if not ts.TaskStore.omnifocus_id(task):
                continue
            wb = ((task.get("omnifocus") or {}).get("writeback") or {})
            if wb.get("completed"):
                continue
            todo.append(task["id"])
        if not todo:
            rl.log_event("omnifocus", "writeback_scan_result", written=[])
            return []
        rl.log_event("omnifocus", "writeback_scan_start", pending=len(todo))
        action = self.net_action("omnifocus.writeback",
                                 "写回 OmniFocus（%d 条）" % len(todo),
                                 {"task_ids": todo}, source=SRC_OMNIFOCUS)
        self.jobs.submit(action, persist=False)
        return [(task_id, "丢后台了") for task_id in todo]

    # -- 收尾用的几个动作（收尾和命令共用同一套，不写两遍）--------------

    def _snapshot_rows(self, iso):
        """某一天的 Focusmate 场次快照（带缓存；读不到就是空列表，不抛）。

        **绝不在提示符线程上等 bridge。** 这个方法会被 formatter / 剪断这类
        用户命令间接调用；以前这里 ``bridge.call("snapshot")`` 是同步的，浏览器
        冷启动时一条 ``task 5 -ac`` 能整整数十秒没反应。现在分三档：

        * 当前选中的那一天：直接用已经拉回来的 ``self.rows``（有 session id）；
        * 别的天：只认 ``_snapshot_cache`` 里已有的快照；
        * 只有后台线程（例如收尾核对）或 sync 模式（测试 / ``--exec``）才真正去拉。

        交互式别的天没缓存时返回空列表。formatter 会退化成「先只记本地绑定」，
        等那一场真的订上、refresh/check 再补 Focusmate task —— 这比卡死 UI 好。
        """
        cache = getattr(self, "_snapshot_cache", None)
        if cache is None:
            cache = self._snapshot_cache = {}
        rows = cache.get(iso)
        if rows is not None:
            return rows
        # 当前表格就是最新的一份当天快照，不必再问一次 bridge。
        try:
            if iso == self.sel_date.isoformat() and self.rows_source == "bridge":
                rows = [dict(row) for row in self.rows
                        if row.get("date") == iso]
                if not self.jobs.sync:
                    cache[iso] = rows
                    return rows
                # sync 模式（测试 / --exec）可能只关心「某一列还没在本地表格
                # 里拿到 id」；按老语义再问一次 bridge，脚本才能拿到真 session。
        except Exception:                          # noqa: BLE001 - 日期/行坏了就走缓存
            pass
        # worker 线程可以拉；交互式主线程只读缓存，绝不让一次外部读卡住输入。
        # sync 模式（测试 / --exec）本来就是「等结果」的语义，仍按老行为同步拉，
        # 保证脚本里的 formatter 能立刻拿到未来那天的 session id。
        if (self.jobs.sync
                or threading.current_thread() is not threading.main_thread()):
            try:
                res = self.bridge.call("snapshot", timeout=180, date=iso)
                rows = res.get("sessions") or []
            except BridgeError:
                rows = []
            cache[iso] = rows
            return rows
        return []

    def session_info_for(self, day, slot):
        """`(date, slot)` → 一场 session 的完整信息（含 Focusmate id，尽力而为）。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        info = {
            "date": iso,
            "start": slot.get("start"),
            "duration": slot.get("duration"),
            "end": None,
            "focusmate_session_id": None,
            "startMs": None,
        }
        try:
            begin = datetime.datetime.strptime(iso, "%Y-%m-%d").replace(
                hour=int(slot["hour"]), minute=int(slot["minute"]))
            finish = begin + datetime.timedelta(minutes=int(slot.get("duration") or 0))
            info["end"] = finish.strftime("%H:%M")
        except (KeyError, ValueError):
            pass
        if self.offline:
            return info
        for row in self._snapshot_rows(iso):
            if row.get("startLabel") == slot.get("start") and row.get("id"):
                info["focusmate_session_id"] = row["id"]
                info["startMs"] = row.get("startMs")
                info["end"] = row.get("endLabel") or info["end"]
                break
        return info

    def assign_task_to(self, task, session):
        """把任务挂到一场 session：Focusmate 建 task（能建就建）+ 本地落账。返回一句人话。

        这一场已经有 🔒 独占任务（或这条自己独占、而场里已经有人）时**不挂**，
        只返回一句警告 —— 「一场 session 只能有一条任务」这条规矩在这里也必须成立，
        不能因为调用方是 formatter / 收尾流程就绕过去。
        """
        date_iso = session.get("date") or self.sel_date.isoformat()
        blockers = self._exclusive_blockers(date_iso, session.get("start"), task)
        if blockers:
            names = "、".join(self.task_label(t) for t in blockers)
            return ("⚠ %s %s 那场已经有 🔒 独占任务 %s，没挂上去（换一场或先放开独占）"
                    % (date_iso, session.get("start") or "?", names))
        sid = session.get("focusmate_session_id") or session.get("id")
        start_ms = session.get("startMs")
        title = self.task_store.focusmate_title(task)
        notes = self.task_store.notes(task) or ""
        payload = {
            "index": session.get("index"),
            "date": date_iso,
            "start": session.get("start"),
            "end": session.get("end"),
            "duration": session.get("duration"),
            "focusmate_session_id": sid,
        }
        if sid and start_ms is not None and not self.offline:
            # formatter / 收尾也会走到这里：同样走「本地先挂、Focusmate 丢后台」，
            # 不在提示符线程上等 add_task。同步模式（测试 / --exec）仍然当场跑完。
            try:
                self._op_task_assign(
                    task, payload, session.get("index") or 0, date_iso,
                    title, notes, refresh=False, start_ms=start_ms)
                return ("Focusmate task 后台挂（%s %s）"
                        % (date_iso, session.get("start") or "?"))
            except (BridgeError, ValueError) as e:
                return "Focusmate 没建上：%s（只记了本地）" % e
        if payload.get("focusmate_session_id"):
            # session id 有但缺少 startMs（只可能是缓存/旧数据）：先记本地，
            # 等 refresh/check 从 Focusmate 真数据里补挂。
            payload["focusmate_session_id"] = None
        self.task_store.assign(task["id"], payload)
        return "这场还没有 Focusmate session id（没订上），只记了本地"

    def next_session_after(self, task, max_days=2):
        """这条任务「下一场」放哪儿：不早于现在、且在原场次之后最近的那个 slot。

        收尾里的 E 和 ``inspect-due`` 的 G 都用它。以前只从任务原场次的起点往后
        找，一条几天前过期、但当天后面还有 slot 的任务会被排回**已经过去**的
        那一天；``inspect-due`` 专门处理过期任务，所以下界必须取「原场次」和
        「现在」的较大者。
        """
        sess = task.get("session") or {}
        now = self.review_now or datetime.datetime.now()
        day = now.date()
        start = now.strftime("%H:%M")
        if sess.get("date") and sess.get("start"):
            try:
                sess_day = datetime.date.fromisoformat(str(sess["date"]))
                sess_start = str(sess["start"])
                sess_dt = datetime.datetime.combine(
                    sess_day, datetime.time.fromisoformat(sess_start))
                if sess_dt > now:
                    day, start = sess_day, sess_start
            except (ValueError, TypeError):
                pass
        entries = self.reload_entries()
        for offset in range(0, int(max_days) + 1):
            probe = day + datetime.timedelta(days=offset)
            try:
                slots = sch.day_slots(entries, probe)
            except Exception:                    # noqa: BLE001 - 排布坏了就当这天没有
                continue
            for slot in slots:
                if probe == day and str(slot.get("start") or "") <= start:
                    continue
                return self.session_info_for(probe, slot)
        return None

    # -- 收尾（postsession）：session 结束 15 秒后的强制对账 -----------------

    def review_hooks(self):
        """收尾流程要「碰外面世界」的那几个口子（其余全在 postsession.Review 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            # 收尾面板上写的编号必须是**面板序号**（和用户正看着的那块表格一个数）
            "label": lambda task: self.task_label(task),
            "drop_omnifocus": lambda task: self.omni_drop(task),
            "complete_focusmate": lambda task: self.fm_complete(task),
            "delete_focusmate_task": lambda task: self.fm_delete_task(task),
            "complete_omnifocus": lambda task, minutes=None: (
                "；".join(x for x in (
                    self.omni_complete(task, minutes=minutes),
                    self._project_child_completed(task, writeback=True)) if x)),
            "next_session": lambda task: self.next_session_after(task),
            "assign_focusmate": lambda task, sess: self.assign_task_to(task, sess),
            "due_of": lambda task: self.omni_due_of(task),
            "set_count": lambda task, cap: self.task_store.set_count(task["id"], cap),
            "set_count_plan": lambda task, invested, remaining: (
                self.task_store.set_count_plan(task["id"], invested, remaining)),
        }

    def sync_completion_state(self, entry=None):
        """核对完成状态：Focusmate 的 completedAt + OmniFocus 的 completed。

        走 bridge / 子进程，**可能要几秒**，所以自动收尾从后台线程调它
        （见 :meth:`_review_worker`），手动 `review` 就直接调。
        返回 ``{本地任务 id: omni 行}``。
        """
        if not self.offline:
            started = time.monotonic()
            rl.log_event("focusmate", "task_pull_start", source="sync_completion_state")
            try:
                res = self.bridge.call("tasks", timeout=180)
                remote = res.get("tasks") if isinstance(res, dict) else res
                before = len(self.task_store.load_archive().get("records") or [])
                n, matched = self.task_store.reconcile(remote or [])
                self.task_sync_at = time.time()
                summary = self._focusmate_task_summary(remote or [])
                summary.update({
                    "source": "sync_completion_state",
                    "matched": matched,
                    "archived": n,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                })
                rl.log_event("focusmate", "task_pull_result", **summary)
                if n:
                    self.announce_completions(before)
            except (BridgeError, ValueError) as e:
                rl.log_event("focusmate", "task_pull_error", source="sync_completion_state",
                             elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                             error="%s: %s" % (type(e).__name__, e))
                self.say("核对 Focusmate 完成状态失败：%s" % e, "warn")
        rows = self.sync_omnifocus_status()
        # OmniFocus 那边已经勾了完成的，本地也标完成（然后走一遍写回，把截止时间对齐）
        for task_id, row in rows.items():
            if not row.get("completed"):
                continue
            try:
                task = self.task_store.get(task_id)
            except ValueError:
                continue
            if ts.TaskStore.is_repeat_template(task):
                continue
            if self.task_store.is_open(task):
                self.task_store.set_status(task_id, ts.STATUS_COMPLETED,
                                           at=row.get("completed_at"),
                                           reason="omnifocus-completed")
                note_sub = self._project_child_completed(task, writeback=False)
                self.say("✓ 任务 %s 在 OmniFocus 里已完成%s"
                         % (self.task_label(task),
                            ("；" + note_sub) if note_sub else ""), "ok")
        self.writeback_completions()
        return rows

    def begin_review(self, entry, sync=True, credit=True):
        """进入一场收尾：核对完成状态 → 挑出没做完的任务 → 建 Review。

        ``credit=False``：**不记这一场的 count**（「到点没开始」那场一分钟都没干，
        不能往任务上记 count —— 见 :meth:`credit_session_counts`）。
        """
        if self.nostart is not None:
            # 一次只问一件事：「没开始」那一问还开着就先收掉（记成 deferred），
            # 否则两套提示抢同一行输入。
            self.close_nostart(note="review-started")
        entry = dict(entry or {})
        day_iso = entry.get("date") or self.sel_date.isoformat()
        start = entry.get("start") or ""
        key = entry.get("key") or psv.session_key(day_iso, start)
        entry["key"] = key
        try:
            self.focus_day(day_iso)
        except Exception:                        # noqa: BLE001 - 切不过去也照样收尾
            pass
        if sync:
            self.sync_completion_state(entry)
        try:
            day = datetime.date.fromisoformat(day_iso)
        except ValueError:
            day = self.sel_date
        tasks = self.task_store.tasks_for_session(day, start)
        opened = [t for t in tasks if self.task_store.is_open(t)]
        # 先把这一场该给的 count 记进账（幂等），**但不完成** —— 够数的那条留给
        # 收尾去问「再要 count 还是直接完成」（见 postsession.COUNT_OPTION）。
        counts = self.credit_session_counts(entry, opened) if credit else {}
        self.rebuild_rows()
        if not opened:
            self.say("⏱ %s %s 那场没有未完成的任务（%d 条记录都已收）—— 这场翻篇。"
                     % (day_iso, start or "?", len(tasks)), "ok")
            psv.mark_done(entry, note="nothing-open")
            self.review = None
            return None
        self.review = psv.Review(self.task_store, entry, opened,
                                 hooks=self.review_hooks(), counts=counts)
        self.say("⏱ %s 收尾：%d 条没做完的任务，一条一条来（A/B/C/D/E）。"
                 % (self.review.headline(), len(opened)), "cmd")
        return self.review

    def credit_session_counts(self, entry, tasks):
        """这一场该给的 count 记进每条任务（**幂等**），返回 ``{task_id: 状态}``。

        用户的需求：``count`` 是「这件事要几个 25 分钟」，一场 session 干完就该往
        前推一格（时长 / 25，往上取整）。但**推完不自动完成** —— 够数只是一次
        「到点了」的预报，收尾里问过人之后才落成「完成」或「再加 count」。

        记账的 key 是 ``postsession.session_key``（``2026-11-03T11:00``），写进任务的
        ``count_credits`` 里：同一场收尾重开多少次都只加一次。
        """
        delta = ts.minutes_to_count(psv.session_minutes(entry)) or 0
        key = str((entry or {}).get("key") or "?")
        out = {}
        for task in tasks or []:
            if ts.TaskStore.is_repeat_template(task):
                continue
            cap = ts.task_count(task)
            if cap is None:
                continue                     # 没设 count 的任务：这一场不记账
            credited = None
            try:
                credited = self.task_store.credit_count(task["id"], key, delta)
            except (ValueError, TypeError) as e:      # noqa: BLE001 - 记账失败不该挡住收尾
                self.say("⚠ 给任务 %s 记 count 失败：%s"
                         % (self.task_label(task), e), "warn")
            fresh = self.task_store.get(task["id"])
            invested = ts.task_count_invested(fresh)
            state = {"cap": cap, "invested": invested, "delta": delta,
                     "credited": credited is not None,
                     "maxed": invested >= cap}
            out[fresh["id"]] = state
            if credited is not None:
                self.say("🧮 任务 %s 记了 %d 个 count：%d/%d%s"
                         % (self.task_label(fresh), delta, invested, cap,
                            "（够数了，收尾时问你：再要 count 还是直接完成）"
                            if state["maxed"] else ""), "ok")
        return out

    def begin_inspect_due(self, tasks, label=None, sync=True, within_minutes=0):
        """``inspect-due``：把「过期还没做完」的任务拉进同一套收尾状态机。

        和 session 收尾唯一的区别：**没有那一场 session 可以「翻篇」**，
        所以结束时不写 postsession 状态文件（见 :meth:`close_review`）。
        """
        entry = {"kind": "due", "key": "due",
                 "label": label or "过期未完成任务",
                 "within_minutes": int(within_minutes or 0),
                 "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        if sync:
            self.sync_completion_state(entry)
        opened = []
        for task in tasks or []:
            try:
                current = self.task_store.get(task["id"])
            except ValueError:
                continue
            if self.task_store.is_open(current):
                opened.append(current)
        self.rebuild_rows()
        if not opened:
            self.say("🔍 没有到期还没做完的任务 —— 账是干净的。", "ok")
            self.review = None
            return None
        self.review = psv.Review(self.task_store, entry, opened, hooks=self.review_hooks())
        self.say("🔍 %s：%d 条要过一遍（A/B/C/D/E，G 延长排期，s 跳过，q 结束）。"
                 % (self.review.headline(), len(opened)), "cmd")
        return self.review

    def cmd_inspect_due(self, args=None, now=None):
        """``inspect-due [分钟] [--all]`` —— 主动 inspect 没做完、已经到期的任务。

        和「session 结束自动催」共用同一套状态机（A 延期 / B 放弃 / C 重议 /
        D 缺漏完成 / E 快捷推后），区别只在**谁触发**：

            inspect-due           所有已经过了截止时间、还没做完的任务
            inspect-due 60        未来 60 分钟内要到的也算（提前看一眼）
            inspect-due --all     连没有截止时间的一起（「主动看没完成的任务」）
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        include_no_due = False
        within = 0
        for tok in toks:
            low = tok.lower()
            if low in ("--all", "all", "全部"):
                include_no_due = True
                continue
            if low in ("--no-due", "nodue"):
                include_no_due = True
                continue
            try:
                within = max(0, int(tok))
            except ValueError:
                raise ValueError("inspect-due 的参数要么是分钟数，要么是 --all；"
                                 "你给的是 %r" % tok) from None
        candidates = psv.due_candidates(self.task_store, now=now, within_minutes=within,
                                        include_no_due=include_no_due)
        label = "过期未完成任务"
        if include_no_due:
            label = "未完成任务（含没设截止时间的）"
        elif within:
            label = "截止时间在未来 %d 分钟内的未完成任务" % within
        if not candidates:
            self.say("🔍 没有%s —— 账是干净的。" % label, "ok")
            return None
        return self.begin_inspect_due(candidates, label=label, within_minutes=within)

    # -- 项目 / plan ----------------------------------------------------
    #
    # 项目是「活的母体」：导入时把 OmniFocus 项目下的任务清单快照进项目库，但
    # **不建本地任务、不上任务面板**；等到 `plan` 给它分了 count，才把当天的任务
    # 「唤起」成本地任务、排进 session（用户原话：*在项目入库的时候就应该获取他
    # 所有的 task，但不展示直到他们被 project 唤起*）。

    def project_rows(self, day=None):
        """项目预览表格的行（序号 = `task <序号> <count>` 里的那个数）。"""
        return self.projects.preview(day or self.sel_date, self.task_store)

    def _task_project_label(self, task):
        """任务 → 一句项目来源（项目名 / OmniFocus pk / count）。"""
        fields = (task or {}).get("fields") or {}
        source = (task or {}).get("source") or {}
        pid = fields.get(pj.PROJECT_ID_FIELD)
        title = str(fields.get(pj.PROJECT_FIELD) or "").strip()
        if not title and pid is not None:
            try:
                project = self.projects.by_id(pid)
                title = (project or {}).get("title") or ""
            except Exception:                  # noqa: BLE001 - 显示失败别挡面板
                title = ""
        if not title and str(source.get("kind") or "") == "project":
            title = str(source.get("title") or "").strip()
        pk = str(fields.get(pj.PROJECT_TASK_FIELD) or "").strip()
        count = fields.get("count")
        if not title and not pk:
            return ""
        bits = []
        if title:
            bits.append("项目:%s" % title)
        if pk:
            bits.append(str(pk)[:12])
        if count:
            try:
                bits.append("%d count" % int(count))
            except (TypeError, ValueError):
                pass
        return " · ".join(bits)

    def render_day_binder(self):
        """未来某一天「session ← 任务 ← formatter / 项目」的编排总览。

        用户看明天的 session 时，不只想看空白场次：还想知道哪一场已经被
        ``end`` / ``enlist`` 占了、挂的是哪条任务、这条任务属于哪个项目。
        这张表把这些信息放在**同一行**，方便决定剩下的空场怎么分给项目。

        只读、只画；不改任何排期。今天不画（契约表本身已经是当天执行面）。
        """
        day = self.sel_date
        if day <= datetime.date.today():
            return None
        iso = day.isoformat()
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001
            sessions = []
        try:
            tasks = self.task_store.list()
        except Exception:                          # noqa: BLE001
            tasks = []

        entries = {}

        def entry(start, end="", duration=None):
            key = str(start or "")
            rec = entries.get(key)
            if rec is None:
                rec = {"start": key, "end": str(end or ""), "duration": duration,
                       "tasks": [], "plans": []}
                entries[key] = rec
            elif not rec.get("duration") and duration:
                rec["duration"] = duration
            return rec

        for sess in sessions:
            entry(sess.get("start"), sess.get("end"),
                  int(sess.get("duration") or 0) or None)

        # 本地任务：正常绑定在 session 上的，以及 formatter reservation 落在这一天的。
        for task in tasks:
            sess = task.get("session") or {}
            fmt = task.get("formatter") or {}
            res = fmt.get("reservation") or {}
            if str(sess.get("date") or "") == iso and sess.get("start"):
                start = str(sess.get("start"))
            elif str(res.get("date") or "") == iso and res.get("start"):
                start = str(res.get("session_start") or res.get("start"))
            else:
                continue
            rec = entry(start, sess.get("end") or res.get("end"),
                        int(sess.get("duration") or 0) or None)
            if not any(t.get("id") == task.get("id") for t in rec["tasks"]):
                rec["tasks"].append(task)

        # 项目 plan items：即使本地 task 已丢失，也把「项目原本怎么排的」露出来。
        try:
            projects = self.projects.active()
        except Exception:                          # noqa: BLE001
            projects = []
        for project in projects:
            plan = pj.plan_of(project, day) or {}
            for item in plan.get("items") or []:
                rec = entry(item.get("start"), duration=item.get("duration"))
                rec["plans"].append({"project": project, "item": item})

        if not entries:
            return None

        t = Table(title="Day Plan · %s %s 编排（session ← 任务 / formatter / 项目）"
                        % (iso, "明天" if day == datetime.date.today()
                           + datetime.timedelta(days=1) else sch.cn_weekday_of_day(day)),
                  box=box.SIMPLE_HEAVY, header_style="bold cyan",
                  title_justify="left", expand=False)
        t.add_column("时间", no_wrap=True)
        t.add_column("时长", justify="right", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("任务 / formatter", overflow="fold")
        t.add_column("项目安排", overflow="fold")

        # 当天 API 行：只看 start，订没订上以 id 为准。
        rows_by_start = {}
        for row in self.rows or ():
            if row.get("date") != iso or not row.get("planned"):
                continue
            rows_by_start.setdefault(str(row.get("startLabel") or ""), row)

        def sort_key(key):
            got = th._clock_minutes(key)
            return (got if got is not None else 24 * 60 + 1, key)

        for start in sorted(entries, key=sort_key):
            rec = entries[start]
            row = rows_by_start.get(start) or {}
            if row.get("id"):
                status = Text("已订", style="bold green")
            elif rows_by_start:
                status = Text("未订", style="yellow")
            else:
                status = Text("仅本地", style="dim")
            task_lines = []
            for task in rec["tasks"]:
                title = self.task_store.focusmate_title(task, max_len=48) or "（无标题）"
                bits = [self.task_mark(task), title]
                fmt = task.get("formatter") or {}
                if fmt.get("name"):
                    label = "⏱%s" % fmt.get("name")
                    placement = (fmt.get("reservation") or {}).get("placement")
                    if placement:
                        label += "(%s)" % placement
                    bits.append(label)
                if th.is_exclusive(task):
                    bits.append("🔒")
                task_lines.append(" ".join(bits))
            if not task_lines:
                task_lines = [Text("空闲", style=STYLE_MUTED)]
            project_lines = []
            for task in rec["tasks"]:
                label = self._task_project_label(task)
                if label:
                    project_lines.append(label)
            for plan in rec["plans"]:
                item = plan.get("item") or {}
                project = plan.get("project") or {}
                label = "plan:%s" % (project.get("title") or project.get("id") or "?")
                if item.get("pk"):
                    label += " · %s" % str(item.get("pk"))[:12]
                if item.get("count"):
                    label += " · %s count" % item.get("count")
                project_lines.append(label)
            # 任务/项目行可能重复（本地任务本身带项目来源），去重但保持顺序。
            seen = set()
            project_lines = [x for x in project_lines
                             if not (x in seen or seen.add(x))]
            dur = rec.get("duration")
            t.add_row(Text("%s%s" % (start, ("–" + rec["end"]) if rec.get("end") else ""),
                           style="bold"),
                      Text(str(dur) if dur else "?"),
                      status,
                      Text("\n".join(str(x) if not isinstance(x, Text) else x.plain
                                     for x in task_lines)),
                      Text("\n".join(project_lines) if project_lines else "—",
                           style=STYLE_MUTED))

        # 项目当天的资源账：说好多少 / 已经落地多少。放 caption，避免再占一张表。
        alloc_bits = []
        for project in projects:
            plan = pj.plan_of(project, day) or {}
            if not plan:
                continue
            planned = sum(int(item.get("count") or 0)
                          for item in plan.get("items") or [])
            alloc_bits.append("「%s」说好 %s count · 已排 %s"
                              % (project.get("title") or project.get("id"),
                                 int(plan.get("count") or 0), planned))
        if alloc_bits:
            t.caption = Text("项目安排：" + "；".join(alloc_bits), style="dim")
        return t

    def render_projects_table(self):
        """契约视图里**任务面板下面**那块项目表：剩余 count / 进度 / 今天 plan 了多少。"""
        rows = self.project_rows()
        pro_rows = self.projects.pro_preview(self.sel_date, self.task_store)
        if not rows and not pro_rows:
            return None
        t = Table(title="Project · 项目（op = OmniFocus Project [SOP/incubator] · "
                        "pro = 真正项目 [running/bottleneck]）",
                  box=box.SIMPLE_HEAVY, header_style="bold blue",
                  title_justify="left", expand=False)
        t.add_column("#", justify="right", style="bold", no_wrap=True)
        t.add_column("项目", no_wrap=True, overflow="crop")
        t.add_column("类型/阶段", no_wrap=True)
        t.add_column("剩余 count", justify="right", no_wrap=True)
        t.add_column("进度", justify="right", no_wrap=True)
        t.add_column("今天 plan", no_wrap=True)
        t.add_column("未完成", justify="right", no_wrap=True)
        t.add_column("本周", justify="right", no_wrap=True)
        t.add_column("OmniFocus", no_wrap=True, style=STYLE_MUTED)
        for row in rows:
            plan_txt = "—"
            if row["allocated"]:
                plan_txt = "说好 %d count" % row["allocated"]
                if row["scheduled"]:
                    plan_txt += " · 已排 %d" % row["scheduled"]
            elif row["scheduled"]:
                plan_txt = "已排 %d count" % row["scheduled"]
            op_kind = row.get("op_type") or "未设"
            if row.get("pro_title"):
                type_txt = "%s · pro:%s(%s)" % (
                    op_kind, row["pro_title"], row.get("pro_stage") or "running")
            else:
                type_txt = op_kind
            type_style = ("bold yellow" if not row.get("op_type")
                          else ("magenta" if op_kind == pj.OP_TYPE_INCUBATOR else "cyan"))
            if not row.get("allowed", True):
                type_txt += " ⏸"
                type_style = STYLE_MUTED
            t.add_row(str(row["number"]),
                      row["title"],
                      Text(type_txt, style=type_style),
                      Text(str(row["remaining"]), style="bold cyan"),
                      Text(row["progress_text"], style=STYLE_MUTED),
                      Text(plan_txt, style="green" if row["allocated"] else STYLE_MUTED),
                      Text(str(row["tasks_open"]), style=STYLE_MUTED),
                      Text(str(row["week_count"]),
                           style="bold yellow" if row.get("week_missing")
                           else STYLE_MUTED),
                      Text(str(row["pk"])[:10] or "—"))
        total_remaining = sum(r["remaining"] for r in rows)
        pro_summary = ""
        if pro_rows:
            bits = []
            for pr in pro_rows:
                f_bit = (" · 文件:%s" % os.path.basename(pr["file"])) if pr.get("file") else ""
                bits.append("Pro#%d「%s」[%s%s]" % (
                    pr["number"], pr["title"], pr["stage"], f_bit))
            pro_summary = "真正项目：" + "、".join(bits) + " · "
        t.caption = ("%s%d 个项目 · 剩余 %d count（%d 分钟）· 本周 = 周一起这一周会产生的"
                     "任务（含被推迟到本周开放的、重复任务的每个 occurrence）· 剩余 "
                     "count = 未完成任务还差几块 25 分钟；进度 = 已完成 / 总共（按 count "
                     "算）· `plan` 分配今天的资源"
                     % (pro_summary, len(rows), total_remaining,
                        total_remaining * pp.MINUTES_PER_COUNT))
        return t

    @staticmethod
    def _is_advance_flag(token):
        low = str(token or "").strip().lower()
        return low in ("-a", "--advance", "—advance", "advance", "切换", "推进")

    def _ensure_pro_for_token(self, token):
        """解析 ``pro <序号|名称>``；若还没有创建对应的 ``pro`` 但存在同序号/同名 ``op``，
        自动用该 ``op`` 初始化一条 ``pro``，让 ``pro 1 -a`` / ``pro 1 file ...`` 直接可用。"""
        self.projects.ensure_tagged_pros()
        try:
            return self.projects.resolve_pro(token)
        except ValueError:
            pass
        # 退回看看是不是指着某个 op 的序号或名字
        try:
            op = self.projects.find(token)
        except ValueError:
            # 既不是已有 pro 也不是已有 op：直接抛出 resolve_pro 的错误
            return self.projects.resolve_pro(token)
        existing = self.projects.pro_for_op(op)
        if existing is not None:
            return existing
        title = op.get("project_tag") or op.get("title") or ("项目 %s" % op["id"])
        stage = (pj.STAGE_BOTTLENECK
                 if pj.normalize_op_type(op.get("op_type")) == pj.OP_TYPE_INCUBATOR
                 else pj.STAGE_RUNNING)
        pro, _created = self.projects.create_pro(
            title, stage=stage, op_id=op["id"])
        return pro

    def _finish_pro_advance(self, pro_id, op_id, target_stage=None):
        """完成 ``pro`` 的阶段切换并绑定新的 ``op``。"""
        pro, op = self.projects.advance_pro(pro_id, op_id, target_stage=target_stage)
        self.pending_advance = None
        if self.pending_op_type:
            self.pending_op_type = [oid for oid in self.pending_op_type
                                    if oid != op["id"]]
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        op_num = self.projects.number_of(op["id"]) or op["id"]
        op_type = pj.normalize_op_type(op.get("op_type")) or pj.OP_TYPE_SOP
        self.say("🔀 项目 #%s「%s」阶段已切换为 %s，并关联 %s op #%s「%s」。"
                 % (pro_num, pro.get("title"), pro.get("stage"),
                    op_type, op_num, op.get("title")), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _start_pro_advance(self, pro, tail_tokens=None):
        """执行 ``pro <序号> -a / --advance [op <序号> | task import ...]``。"""
        tail = [str(t).strip() for t in (tail_tokens or ()) if str(t).strip()]
        cur_stage = pj.normalize_pro_stage(pro.get("stage")) or pj.STAGE_RUNNING
        target_stage = (pj.STAGE_BOTTLENECK if cur_stage == pj.STAGE_RUNNING
                        else pj.STAGE_RUNNING)
        req_type = (pj.OP_TYPE_INCUBATOR if target_stage == pj.STAGE_BOTTLENECK
                    else pj.OP_TYPE_SOP)
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        if not tail:
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            self.say("🔀 项目 #%s「%s」准备从 %s 切换到 %s：需要提供新的 %s OmniFocus Project (op) 作为关联。"
                     % (pro_num, pro.get("title"), cur_stage, target_stage, req_type), "cmd")
            self.say("   请直接输入 `task import [链接]`（导入新 op）或 `op <序号>`（关联已有 op），`q` 取消。",
                     "info")
            self.invalidate()
            return pro
        low0 = tail[0].lower()
        if low0 == "task" and len(tail) >= 2 and tail[1].lower() in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[2:])
        if low0 in ("op", "ofp") and len(tail) >= 2 and tail[1].lower() in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[2:])
        if low0 in ("import", "导入", "粘贴"):
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail[1:])
        joined = " ".join(tail)
        if of.link_kind(joined) == "project":
            self.pending_advance = {
                "pro_id": pro["id"],
                "pro_title": pro.get("title"),
                "from_stage": cur_stage,
                "target_stage": target_stage,
                "required_op_type": req_type,
            }
            return self.cmd_task_import(tail)
        if low0 in ("op", "ofp", "omnifocus-project"):
            if len(tail) < 2:
                raise ValueError("`op` 后面要跟 op 序号（如 `pro %s -a op 2`）或 `task import`" % pro_num)
            op_tok = " ".join(tail[1:])
        else:
            op_tok = joined
        op = self.projects.find(op_tok)
        return self._finish_pro_advance(pro["id"], op["id"], target_stage=target_stage)

    def _cmd_pro_create(self, toks):
        """``pro create <名称> [file=<路径>] [stage=running|bottleneck] [op=<op序号>]``。"""
        title_parts = []
        file_path = None
        stage = pj.STAGE_RUNNING
        op_token = None
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            if low in ("--file", "-f", "file") and i + 1 < len(toks):
                file_path = toks[i + 1]
                i += 2
                continue
            if low.startswith("file=") or low.startswith("--file="):
                file_path = tok.split("=", 1)[1]
                i += 1
                continue
            if low in ("--stage", "-s", "stage") and i + 1 < len(toks):
                stage = toks[i + 1]
                i += 2
                continue
            if low.startswith("stage=") or low.startswith("--stage="):
                stage = tok.split("=", 1)[1]
                i += 1
                continue
            if low in ("--op", "op") and i + 1 < len(toks):
                op_token = toks[i + 1]
                i += 2
                continue
            if low.startswith("op=") or low.startswith("--op="):
                op_token = tok.split("=", 1)[1]
                i += 1
                continue
            if low.startswith("title=") or low.startswith("name="):
                title_parts.append(tok.split("=", 1)[1])
                i += 1
                continue
            title_parts.append(tok)
            i += 1
        title = " ".join(title_parts).strip()
        if not title:
            raise ValueError("用法：pro create <项目名称> [file=<文件路径>] [stage=running|bottleneck] [op=<op序号>]")
        op_id = None
        if op_token is not None:
            op = self.projects.find(op_token)
            op_id = op["id"]
        pro, created = self.projects.create_pro(
            title, stage=stage, file=file_path, op_id=op_id)
        self.selected_pro_id = pro["id"]
        num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        file_note = (" · 关联文件：%s" % pro["file"]) if pro.get("file") else ""
        self.say("%s Pro #%s「%s」阶段：%s%s"
                 % ("📂 新建项目" if created else "📂 更新项目",
                    num, pro.get("title"), pro.get("stage"), file_note), "ok")
        self.rebuild_rows()
        self.invalidate()
        return pro

    def _pro_detail(self, pro):
        """展示真正项目 ``pro`` 的详情 + 关联 ``op`` 的任务清单。"""
        self.selected_pro_id = pro["id"]
        pro_num = self.projects.pro_number_of(pro["id"]) or pro["id"]
        linked_ops = self.projects.ops_for_pro(pro)
        sop_op = self.projects.by_id(pro.get("sop_op_id")) if pro.get("sop_op_id") else None
        inc_op = self.projects.by_id(pro.get("incubator_op_id")) if pro.get("incubator_op_id") else None
        file_txt = pro.get("file") or "（未关联，用 `pro %s file <路径>` 绑定）" % pro_num
        self.say("📂 Pro #%s「%s」· 阶段：%s · 关联文件：%s"
                 % (pro_num, pro.get("title"), pro.get("stage"), file_txt), "cmd")
        self.say("   SOP op：%s · incubator op：%s"
                 % (("#%s「%s」" % (self.projects.number_of(sop_op["id"]) or sop_op["id"],
                                    sop_op.get("title"))) if sop_op else "—",
                    ("#%s「%s」" % (self.projects.number_of(inc_op["id"]) or inc_op["id"],
                                    inc_op.get("title"))) if inc_op else "—"), "info")
        for op in linked_ops:
            self._project_detail(op)
        return pro

    def cmd_project(self, args=None):
        """``project`` / ``pro`` —— 管理真正项目 Project (pro) 与兼容项目库操作。

            pro / project                       列出真正项目 pro 与 OmniFocus 项目 op
            pro create <名称> [file=<路径>]     创建真正项目 pro
            pro <序号> -a / --advance [...]     快捷切换阶段（running ↔ bottleneck）并关联新 op
            pro <序号> file <路径|clear>        关联/清空项目的打开文件（open + enter 顺便打开）
            pro <序号> open                     打开该项目关联的文件
            pro <序号> stage <running|bottleneck>  直接设置阶段
            pro <序号> op <op序号>              关联已有 op
            project 1                           看 #1 的详情与任务清单
            project 1 refresh                   刷新 OmniFocus 清单
            project 1 remove                    撤掉项目
            project refresh / check             全量刷新 / 每日对账
        """
        self.projects.ensure_tagged_pros()
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            pro_rows = self.projects.pro_preview(self.sel_date, self.task_store)
            rows = self.project_rows()
            if not pro_rows and not rows:
                self.say("📁 项目库是空的：在 OmniFocus 里 ⌘C 一个项目，再敲 `task import`，"
                         "或 `pro create <名称>`。", "warn")
                return []
            for pr in pro_rows:
                sop_txt = ("#%s「%s」" % (self.projects.number_of(pr["sop_op"]["id"]) or pr["sop_op"]["id"],
                                          pr["sop_op"].get("title"))
                           if pr.get("sop_op") else "—")
                inc_txt = ("#%s「%s」" % (self.projects.number_of(pr["incubator_op"]["id"]) or pr["incubator_op"]["id"],
                                          pr["incubator_op"].get("title"))
                           if pr.get("incubator_op") else "—")
                file_txt = (" · 文件：%s" % pr["file"]) if pr.get("file") else ""
                self.say("📂 Pro #%d %s [阶段:%s] · SOP:%s · incubator:%s%s"
                         % (pr["number"], pr["title"], pr["stage"],
                            sop_txt, inc_txt, file_txt), "info")
            for row in rows:
                op_kind = row.get("op_type") or "未设类型"
                pro_bit = (" · pro:%s(%s)" % (row["pro_title"], row.get("pro_stage") or "running")
                           if row.get("pro_title") else "")
                self.say("📁 #%d %s [%s%s]：剩余 %d count · 进度 %s · %d 条未完成 · 本周 %d 条%s"
                         % (row["number"], row["title"], op_kind, pro_bit,
                            row["remaining"], row["progress_text"], row["tasks_open"],
                            int(row.get("week_count") or 0),
                            ("· 今天说好 %d count（已排 %d）"
                             % (row["allocated"], row["scheduled"]))
                            if row["allocated"] else ""), "info")
            return rows or pro_rows
        action = toks[0].lower()
        if action in ("create", "new", "add", "新建", "创建"):
            return self._cmd_pro_create(toks[1:])
        if action in ("check", "对账", "daily", "每日检查"):
            self._project_sync_day = None
            started = self._start_project_sync_now()
            return started
        if action in ("refresh", "sync", "reload", "更新"):
            made = [self._project_refresh(project) for project in self.projects.active()]
            return [p for p in made if p]
        if self._is_advance_flag(action):
            if len(toks) < 2:
                raise ValueError("用法：pro <序号> -a / --advance [op <序号> | task import]")
            pro = self._ensure_pro_for_token(toks[1])
            return self._start_pro_advance(pro, toks[2:])

        rest = toks[1:]
        rest_low = [t.lower() for t in rest]

        # 检查是否带 -a / --advance / —advance
        if rest and self._is_advance_flag(rest[0]):
            pro = self._ensure_pro_for_token(toks[0])
            return self._start_pro_advance(pro, rest[1:])

        # 检查是否是 file / --file / -f / file=...
        if rest and (rest_low[0] in ("file", "--file", "-f", "文件")
                     or rest_low[0].startswith("file=")
                     or rest_low[0].startswith("--file=")):
            pro = self._ensure_pro_for_token(toks[0])
            if "=" in rest[0]:
                fpath = rest[0].split("=", 1)[1]
                if len(rest) > 1:
                    fpath = (fpath + " " + " ".join(rest[1:])).strip()
            else:
                fpath = " ".join(rest[1:]).strip()
            updated = self.projects.set_pro_file(pro["id"], fpath)
            self.selected_pro_id = updated["id"]
            pnum = self.projects.pro_number_of(updated["id"]) or updated["id"]
            if updated.get("file"):
                self.say("📂 项目 #%s「%s」已关联文件：%s（敲 `open` + 回车时顺便打开）。"
                         % (pnum, updated.get("title"), updated["file"]), "ok")
            else:
                self.say("📂 项目 #%s「%s」已清空关联文件。"
                         % (pnum, updated.get("title")), "ok")
            self.invalidate()
            return updated

        # 检查是否是 open（打开项目关联文件）
        if rest and rest_low[0] in ("open", "打开"):
            pro = self._ensure_pro_for_token(toks[0])
            self.selected_pro_id = pro["id"]
            if not pro.get("file"):
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                raise ValueError("项目 #%s「%s」还没有关联文件；先用 `pro %s file <路径>` 关联。"
                                 % (pnum, pro.get("title"), pnum))
            self._open_pro_file(pro)
            return pro

        # 检查是否是 stage <running|bottleneck>
        if rest and (rest_low[0] in ("stage", "--stage", "阶段")
                     or rest_low[0].startswith("stage=")
                     or pj.normalize_pro_stage(rest[0]) is not None):
            pro = self._ensure_pro_for_token(toks[0])
            if rest_low[0].startswith("stage="):
                st_val = rest[0].split("=", 1)[1]
            elif rest_low[0] in ("stage", "--stage", "阶段"):
                if len(rest) < 2:
                    raise ValueError("用法：pro <序号> stage <running|bottleneck>")
                st_val = rest[1]
            else:
                st_val = rest[0]
            updated = self.projects.set_pro_stage(pro["id"], st_val)
            self.selected_pro_id = updated["id"]
            pnum = self.projects.pro_number_of(updated["id"]) or updated["id"]
            self.say("📂 项目 #%s「%s」阶段已设为 %s。"
                     % (pnum, updated.get("title"), updated.get("stage")), "ok")
            self.rebuild_rows()
            self.invalidate()
            return updated

        # 检查是否是关联已有 op：pro <序号> op <op序号>
        if rest and rest_low[0] in ("op", "link", "关联"):
            if len(rest) < 2:
                raise ValueError("用法：pro <序号> op <op序号>")
            pro = self._ensure_pro_for_token(toks[0])
            op = self.projects.find(" ".join(rest[1:]))
            op, pro = self.projects.link_op_to_pro(op["id"], pro["id"])
            self.selected_pro_id = pro["id"]
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            onum = self.projects.number_of(op["id"]) or op["id"]
            self.say("🔗 项目 #%s「%s」已关联 op #%s「%s」。"
                     % (pnum, pro.get("title"), onum, op.get("title")), "ok")
            self.invalidate()
            return pro

        # refresh / remove / 详情：兼容 op 与 pro
        if rest and rest_low[0] in ("refresh", "sync", "reload", "更新"):
            try:
                project = self.projects.resolve(toks[0])
                return self._project_refresh(project)
            except ValueError:
                pro = self.projects.resolve_pro(toks[0])
                ops = self.projects.ops_for_pro(pro)
                made = [self._project_refresh(op) for op in ops]
                return [p for p in made if p]
        if rest and rest_low[0] in ("remove", "rm", "delete", "撤掉", "删除"):
            try:
                project = self.projects.resolve(toks[0])
                num = self.projects.number_of(project["id"]) or project["id"]
                removed = self.task_store.remove_project_backlog(project["id"])
                self.projects.remove(project["id"])
                self.rebuild_rows()
                self.invalidate()
                self.say("🗑 项目 #%d「%s」从项目库撤掉了（OmniFocus 那边没动；已经排进 "
                         "session 的任务照旧%s）。"
                         % (num, project.get("title"),
                            "；清掉 %d 条未唤起的镜像任务" % removed if removed else ""), "ok")
                return project
            except ValueError:
                pro = self.projects.resolve_pro(toks[0])
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                self.projects.remove_pro(pro["id"])
                self.invalidate()
                self.say("🗑 项目（pro）#%s「%s」已删除。" % (pnum, pro.get("title")), "ok")
                return pro
        # 详情：如果既有 pro 也有 op，优先看 op 详情并附带 pro 信息；若只有 pro 则看 pro 详情
        try:
            project = self.projects.resolve(toks[0])
            pro = self.projects.pro_for_op(project)
            if pro is not None:
                self.selected_pro_id = pro["id"]
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                self.say("📂 所属项目 Pro #%s「%s」· 阶段：%s%s"
                         % (pnum, pro.get("title"), pro.get("stage"),
                            (" · 文件：%s" % pro["file"]) if pro.get("file") else ""),
                         "info")
            return self._project_detail(project)
        except ValueError:
            pro = self.projects.resolve_pro(toks[0])
            return self._pro_detail(pro)

    def cmd_op(self, args=None):
        """``op`` —— 管理 OmniFocus Project（类型：``SOP`` 或 ``incubator``）。

            op                              列出全部 OmniFocus Project（op）
            op import [sop|incubator] [...] 从剪贴板/链接导入 op 并指定类型
            op <序号>                       查看 op 详情与任务清单
            op <序号> sop | incubator       设置 op 类型为 SOP 或 incubator
            op <序号> pro <pro序号|名称|none> 关联/解绑真正项目 pro
            op <序号> refresh | remove      刷新或移除该 op
            op refresh | check              全量刷新或每日对账
        """
        self.projects.ensure_tagged_pros()
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            rows = self.project_rows()
            if not rows:
                self.say("📁 OmniFocus 项目库（op）是空的：在 OmniFocus 里 ⌘C 一个项目，"
                         "再敲 `op import sop` 或 `task import sop`。", "warn")
                return []
            for row in rows:
                op_kind = row.get("op_type") or "⚠未设类型"
                pro_bit = (" · pro:%s(%s)" % (row["pro_title"], row.get("pro_stage") or "running")
                           if row.get("pro_title") else "")
                allow_bit = "" if row.get("allowed", True) else " · ⏸受项目阶段限制"
                self.say("📁 op #%d %s [%s%s%s]：剩余 %d count · 进度 %s · %d 条未完成 · 本周 %d 条"
                         % (row["number"], row["title"], op_kind, pro_bit, allow_bit,
                            row["remaining"], row["progress_text"], row["tasks_open"],
                            int(row.get("week_count") or 0)), "info")
            return rows
        first_low = toks[0].lower()
        if first_low in ("import", "导入", "粘贴"):
            return self.cmd_task_import(toks[1:])
        if pj.normalize_op_type(first_low) in pj.OP_TYPES and len(toks) >= 2 and toks[1].lower() in ("import", "导入", "粘贴"):
            return self.cmd_task_import([toks[0]] + toks[2:])
        if first_low in ("check", "对账", "daily", "每日检查"):
            self._project_sync_day = None
            return self._start_project_sync_now()
        if first_low in ("refresh", "sync", "reload", "更新"):
            made = [self._project_refresh(project) for project in self.projects.active()]
            return [p for p in made if p]
        op = self.projects.find(toks[0])
        rest = toks[1:]
        if not rest:
            return self._project_detail(op)
        rel = repl_kit.relation.parse(toks, "op")
        if rel is not None:
            return self._op_relate(op, rel)
        r0 = rest[0].lower()
        if r0 in ("refresh", "sync", "reload", "更新"):
            return self._project_refresh(op)
        if r0 in ("remove", "rm", "delete", "撤掉", "删除"):
            num = self.projects.number_of(op["id"]) or op["id"]
            removed = self.task_store.remove_project_backlog(op["id"])
            self.projects.remove(op["id"])
            self.rebuild_rows()
            self.invalidate()
            self.say("🗑 op #%d「%s」已从项目库撤掉%s。"
                     % (num, op.get("title"),
                        "（清掉 %d 条未唤起镜像任务）" % removed if removed else ""), "ok")
            return op
        if r0 in ("type", "--type", "-t", "类型") or pj.normalize_op_type(r0) in pj.OP_TYPES:
            val = rest[1] if r0 in ("type", "--type", "-t", "类型") and len(rest) > 1 else rest[0]
            norm_t = pj.normalize_op_type(val)
            if norm_t not in pj.OP_TYPES:
                raise ValueError("op 类型只能是 SOP 或 incubator；你给的是 %r" % val)
            updated = self.projects.set_op_type(op["id"], norm_t)
            if self.pending_op_type:
                self.pending_op_type = [oid for oid in self.pending_op_type
                                        if oid != updated["id"]]
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pro = self.projects.pro_for_op(updated)
            pro_note = (" · 关联项目「%s」(%s)" % (pro.get("title"), pro.get("stage"))
                        if pro else "")
            self.say("✅ op #%s「%s」类型已设为 %s%s。"
                     % (num, updated.get("title"), norm_t, pro_note), "ok")
            self.rebuild_rows()
            self.invalidate()
            return updated
        if r0 in ("pro", "project", "link", "关联"):
            if len(rest) < 2:
                raise ValueError("用法：op <序号> pro <pro序号|名称|none>")
            target_tok = " ".join(rest[1:]).strip()
            if target_tok.lower() in ("none", "clear", "off", "无", "清空"):
                updated, _ = self.projects.link_op_to_pro(op["id"], None)
                num = self.projects.number_of(updated["id"]) or updated["id"]
                self.say("⏏ op #%s「%s」已解除项目（pro）关联。" % (num, updated.get("title")), "ok")
                self.invalidate()
                return updated
            pro = self._ensure_pro_for_token(target_tok)
            updated, pro = self.projects.link_op_to_pro(op["id"], pro["id"])
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            self.say("🔗 op #%s「%s」已关联到项目 Pro #%s「%s」（阶段：%s）。"
                     % (num, updated.get("title"), pnum, pro.get("title"), pro.get("stage")), "ok")
            self.invalidate()
            return updated
        raise ValueError("op 子命令只认 sop / incubator / pro <N> / refresh / remove；你给的是 %r"
                         % rest[0])

    # -- relate：关联文件（模板只给 incubator op 用）--------------------

    def _relate_dir(self):
        """``relate-dir`` 设置里指定的目录；相对路径按 warden 所在目录算。"""
        raw = str(self.settings.get("relate-dir") or "").strip() or "data/relate"
        if os.path.isabs(raw):
            return raw
        return os.path.join(HERE, raw)

    def _op_relate(self, op, rel):
        """``op <序号> relate ...`` 的分流。解析逻辑在 repl_kit.relation。"""
        if rel.has_flag("--export", "-e", "export"):
            return self._relate_export(op)
        if rel.has_flag("--import", "-i", "import"):
            return self._relate_import(op, rel)
        raise ValueError("relate 只认 --export / -e（生成模板）或 --import / -i（解析并 append）；"
                         "你给的是 %r" % (" ".join(rel.args) or "（空）"))

    def _relate_export(self, op):
        """在 ``relate-dir`` 生成 incubator SOP 模板，并把路径记到 op 关联文件。"""
        num = self.projects.number_of(op["id"]) or op["id"]
        if pj.normalize_op_type(op.get("op_type")) != pj.OP_TYPE_INCUBATOR:
            self.say("⏭ op #%s「%s」不是 incubator 类型；relate 模板只给 incubator op 用，"
                     "这条命令什么也没做。先用 `op %s incubator` 再试。"
                     % (num, op.get("title"), num), "warn")
            return None
        pro = self.projects.pro_for_op(op)
        if pro is None:
            raise ValueError("op #%s「%s」没有关联真正项目 pro；先 `op %s pro <pro序号>` 再 relate。"
                             % (num, op.get("title"), num))
        pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        pro_title = pro.get("title") or ("pro #%s" % pnum)
        body = ("---\n"
                "created: %s\n"
                "updated: %s\n"
                "关联项目: %s\n"
                "---\n"
                "**用途**: 被incubator 使用，用来准备一个 SOP"
                "（可以是无规律的，也就是说是 research + brainstorm 产生的 SOP）\n"
                "# 资源准备任务列表\n"
                "- example\n"
                "\n"
                "# 具体的 SOP 流程\n"
                "**仔细思考，代入其中。作为一个实际上执行 SOP 的人，"
                "你会需要提前准备什么还没有列出来的东西？**\n"
                "- example\n") % (now, now, pro_title)
        directory = self._relate_dir()
        os.makedirs(directory, exist_ok=True)
        slug = re.sub(r"[^\w\-]+", "-", str(op.get("title") or "op")).strip("-")[:40] or "op"
        path = os.path.join(directory, "op-%d-%s.md" % (op["id"], slug))
        if os.path.exists(path):
            path = os.path.join(directory, "op-%d-%s-%d.md"
                                % (op["id"], slug, int(time.time())))
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        updated = self.projects.set_project_file(op["id"], path)
        self.say("📄 已生成 relate 模板：%s" % path, "ok")
        self.say("   关联项目：%s（pro #%s）· 已记到 op #%s「%s」的关联文件。"
                 % (pro_title, pnum, num, updated.get("title")), "info")
        self.rebuild_rows()
        self.invalidate()
        return updated

    def _relate_import(self, op, rel):
        """``op <序号> relate --import [绝对路径]``：解析模板并 append 到项目库。"""
        num = self.projects.number_of(op["id"]) or op["id"]
        if pj.normalize_op_type(op.get("op_type")) != pj.OP_TYPE_INCUBATOR:
            self.say("⏭ op #%s「%s」不是 incubator 类型；relate 模板只给 incubator op 用，"
                     "这条命令什么也没做。先用 `op %s incubator` 再试。"
                     % (num, op.get("title"), num), "warn")
            return None
        pro = self.projects.pro_for_op(op)
        if pro is None:
            raise ValueError("op #%s「%s」没有关联真正项目 pro；先 `op %s pro <pro序号>` 再 relate。"
                             % (num, op.get("title"), num))
        sop_op = self._relate_sop_op(pro, exclude_op=op)
        if sop_op is None:
            pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
            raise ValueError("项目 Pro #%s「%s」没有对应的 SOP op；"
                             "relate 模板的「具体的 SOP 流程」没地方放，"
                             "先 `pro %s op <SOP op序号>` 关联好再做任何导入。"
                             % (pnum, pro.get("title"), pnum))
        path = rel.flag_value("--import", "-i")
        if not path:
            for _arg in rel.args:
                if _arg.lower().startswith("--import="):
                    path = _arg.split("=", 1)[1]
                    break
                if _arg.lower().startswith("-i="):
                    path = _arg.split("=", 1)[1]
                    break
        if not path:
            path = op.get("file")
        if path and not os.path.exists(path):
            raise ValueError("关联文件不存在：%s（先 `op %s relate --export` 重新生成，"
                             "或给一个存在的绝对路径）。" % (path, num))
        if not path:
            if getattr(self.shell, "_app_running", False):
                self.pending_relate_path = {"op_id": op["id"]}
                self.say("📄 op #%s「%s」还没有关联文件；请输入要 import 的绝对路径（q 取消）："
                         % (num, op.get("title")), "cmd")
                self.invalidate()
                return None
            raise ValueError("op #%s「%s」还没有关联文件；用法：op %s relate --import <绝对路径>。"
                             % (num, op.get("title"), num))
        return self._relate_import_from_path(op, pro, sop_op, path)

    def _relate_sop_op(self, pro, exclude_op=None):
        """找到 ``pro`` 对应的 SOP op；没有返回 None。"""
        store = self.projects.load()
        sop_ops = [op for op in store.get("projects") or ()
                   if op.get("status") == pj.STATUS_ACTIVE
                   and pj.normalize_op_type(op.get("op_type")) == pj.OP_TYPE_SOP]
        by_id = {op["id"]: op for op in sop_ops}
        if pro.get("sop_op_id"):
            hit = by_id.get(pro["sop_op_id"])
            if hit is not None and (exclude_op is None or hit["id"] != exclude_op["id"]):
                return hit
        pro_id = pro.get("id")
        for op in sop_ops:
            if exclude_op is not None and op["id"] == exclude_op["id"]:
                continue
            linked = self.projects.pro_for_op(op, store=store)
            if linked is not None and linked["id"] == pro_id:
                return op
        return None

    def _relate_import_from_path(self, op, pro, sop_op, path):
        """读模板文件 → 解析两节 → 校验 → append 到两个 op 的清单。"""
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise ValueError("读不了 %s：%s" % (path, e))
        sections = parse_relate_markdown(text)
        resource_tasks = sections.get("资源准备任务列表") or []
        sop_tasks = sections.get("具体的 SOP 流程") or []
        if not resource_tasks and not sop_tasks:
            raise ValueError("模板里没有找到「# 资源准备任务列表」或「# 具体的 SOP 流程」"
                             "下面的无序列表。")
        res_members = self._relate_validate_section(op, resource_tasks, "res")
        sop_members = self._relate_validate_section(sop_op, sop_tasks, "sop")
        # 两节都合法才动手，避免只 append 一半。
        if res_members:
            updated = self.projects.append_tasks(op["id"], res_members)
            self.sync_project_tasks(self.projects.by_id(op["id"]), quiet=True)
        else:
            updated = self.projects.by_id(op["id"])
        if sop_members:
            self.projects.append_tasks(sop_op["id"], sop_members)
            self.sync_project_tasks(self.projects.by_id(sop_op["id"]), quiet=True)
        if updated is not None and str(updated.get("file") or "") != str(path):
            updated = self.projects.set_project_file(op["id"], path)
        self.rebuild_rows()
        self.invalidate()
        num = self.projects.number_of(op["id"]) or op["id"]
        snum = self.projects.number_of(sop_op["id"]) or sop_op["id"]
        self.say("📥 relate 导入完成：%s" % path, "ok")
        self.say("   资源准备任务列表 → incubator op #%s「%s」append %d 条。"
                 % (num, updated.get("title") if updated else op.get("title"),
                    len(res_members)), "info")
        self.say("   具体的 SOP 流程 → SOP op #%s「%s」append %d 条。"
                 % (snum, sop_op.get("title"), len(sop_members)), "info")
        self.say("   这两批任务都只落在本地项目库（OmniFocus 没动）；"
                 "等 `plan` 分到 count 就会按项目任务正常唤起。", "info")
        return updated

    def _relate_validate_section(self, target_op, tasks, section):
        """把一节里的 bullets 变成项目任务记录；没时长的整节报错（复用项目检查）。"""
        stamp = int(time.time() * 1000)
        root = {"id": "__relate_root__", "name": target_op.get("title") or "relate",
                "kind": "project_root", "project": "",
                "estimated_minutes": None}
        rows = [root]
        for index, task in enumerate(tasks or []):
            pk = "relate-%d-%s-%d-%d" % (target_op["id"], section, index + 1, stamp)
            rec = parse_relate_task(task, pk)
            rows.append({
                "id": rec["pk"],
                "name": rec["title"] or ("relate 任务 %d" % (index + 1)),
                "kind": "action",
                "project": target_op.get("title") or "",
                "estimated_minutes": rec["minutes"],
                "note": rec["note"],
                "completed": False,
                "dropped": False,
            })
        try:
            payload = pj.project_payload(root, rows)
        except ValueError as e:
            raise ValueError("「%s」这节解析不了：%s" % (section, e))
        if payload.get("missing"):
            raise ValueError(pj.format_missing(payload))
        if payload.get("depth_errors"):
            raise ValueError("「%s」这节有超过父-子两层的任务，先改成平铺的无序列表再 import。"
                             % section)
        members = list(payload.get("tasks") or [])
        for member in members:
            member["local_only"] = True
        return members

    def pending_relate_path_panel(self):
        """``op <序号> relate --import`` 等用户输入绝对路径的面板。"""
        pending = self.pending_relate_path or {}
        op = self.projects.by_id(pending.get("op_id"))
        num = self.projects.number_of(op["id"]) if op else None
        title = (op or {}).get("title") or "?"
        body = Text()
        body.append("op #%s「%s」还没有关联文件，请直接输入要 import 的绝对路径：\n"
                    % (num or pending.get("op_id"), title), style="bold")
        body.append("（`op <序号> relate --export` 会先建好模板并自动关联；q 取消）\n",
                    style="dim")
        return Panel(body, title="[bold cyan]📄 relate --import[/]",
                     border_style="cyan", box=box.HEAVY)

    def _answer_pending_relate_path(self, line):
        """处理 ``pending_relate_path`` 下的输入：路径或 q。"""
        pending = self.pending_relate_path
        if pending is None:
            return None
        raw = " ".join(str(line or "").strip().split())
        if not raw:
            return True
        if raw.lower() in ("q", "quit", "cancel", "取消", "放弃"):
            self.pending_relate_path = None
            self.say("⏹ 已取消 relate --import。", "info")
            self.invalidate()
            return True
        self.pending_relate_path = None
        try:
            op = self.projects.by_id(pending.get("op_id"))
            if op is None:
                raise ValueError("op %s 不见了" % pending.get("op_id"))
            if not os.path.isabs(raw):
                raise ValueError("要绝对路径：%r" % raw)
            if not os.path.exists(raw):
                raise ValueError("文件不存在：%s" % raw)
            rel = repl_kit.relation.parse(
                [str(pending.get("op_id")), "relate", "--import", raw], "op")
            if rel is None:
                raise ValueError("内部解析失败：%r" % raw)
            self._relate_import(op, rel)
        except Exception as e:                      # noqa: BLE001
            self.had_error = True
            self.say(str(e), "warn")
        self.invalidate()
        return True

    def _project_detail(self, project):
        """一个项目的清单 + 今天的分工（`project 1`）。"""
        invested = pj.invested_by_project(self.task_store).get(project["id"]) or {}
        stat = pj.stats(project, self.task_store, invested)
        num = self.projects.number_of(project["id"]) or project["id"]
        self.say("📁 #%d「%s」OmniFocus %s · 剩余 %d count · 进度 %s（%d/%d）"
                 % (num, project.get("title"),
                    ((project.get("omnifocus") or {}).get("id") or "?"),
                    stat["remaining"], stat["progress_text"],
                    stat["done"], stat["total"]), "cmd")
        if project.get("file"):
            self.say("   📄 关联文件：%s" % project.get("file"), "info")
        week = pj.weekly_yield(project.get("tasks") or [])
        if week.get("count"):
            self.say("   " + pj.week_summary(week), "info")
            if week.get("missing"):
                self.say("   ⚠ 本周有 %d 条任务没设持续时间：%s"
                         % (len(week["missing"]),
                            "、".join("「%s」" % rec.get("title")
                                     for rec in week["missing"][:6])), "warn")
        items = pj.remaining_tasks(project, invested)
        for item in items[:12]:
            bits = ["%d count" % int(item.get("count") or 0),
                    "还剩 %d" % int(item.get("remaining") or 0)]
            if item.get("due"):
                bits.append("截止 %s" % str(item["due"]).replace("T", " ")[:16])
            if item.get("blocked"):
                bits.append("被前置阻塞")
            self.say("   · %s（%s）" % (item.get("title"), " · ".join(bits)), "info")
        if len(items) > 12:
            self.say("   …还有 %d 条" % (len(items) - 12), "info")
        plan = pj.plan_of(project, self.sel_date)
        if plan and (plan.get("count") or plan.get("items")):
            self.say("   今天（%s）说好 %d count，已经排了 %d 条："
                     % (self.sel_date.isoformat(), int(plan.get("count") or 0),
                        len(plan.get("items") or [])), "info")
            for rec in plan.get("items") or []:
                task = None
                try:
                    task = self.task_store.get(rec["task_id"])
                except (ValueError, TypeError):
                    task = None
                self.say("     %s(%d) ← 任务 %s「%s」%s"
                         % (rec.get("start") or "?", int(rec.get("duration") or 0),
                            self.task_no(task) or rec["task_id"],
                            (task or {}).get("fields", {}).get("title") or "?",
                            "" if task else "（本地任务已经不在了）"), "info")
        else:
            self.say("   今天还没给它分资源（`plan` 分 count 才会唤起任务）。", "info")
        return project

    def _project_refresh(self, project):
        """重新读 OmniFocus 里这个项目的清单（写回项目库）。"""
        project = project or {}
        pk = (project.get("omnifocus") or {}).get("id")
        num = self.projects.number_of(project.get("id")) or project.get("id")
        if not pk:
            self.say("⚠ 项目 #%s 没有 OmniFocus 关联，刷不了。" % num, "warn")
            return None
        try:
            if not self.omnifocus_enabled():
                raise ValueError("OmniFocus 关着（`set omnifocus on` 打开）")
            rows = self.omnifocus.list_tasks(refresh=True)
        except Exception as e:                      # noqa: BLE001 - 读不到就说清楚
            self.had_error = True
            self.say("⚠ 刷新项目 #%s 失败：%s" % (num, e), "error")
            return None
        row = next((r for r in rows or () if str(r.get("id") or "") == str(pk)), None)
        if row is None:
            self.say("⚠ OmniFocus 里找不到项目 %s（被删了？）—— 项目库先留着旧的清单。"
                     % pk, "warn")
            return None
        payload = pj.project_payload(row, rows)
        payload = pj.attach_week(
            payload, self._project_repeat_infos(
                payload, lambda text, kind="warn": self.say(text, kind)))
        if (payload.get("missing") or payload.get("week_missing")
                or payload.get("depth_errors")):
            self.had_error = True
            self.say("✗ " + (pj.format_missing(payload)
                             or pj.format_depth_errors(payload)), "error")
            self.say("🚫 这次刷新没写库（清单先按旧的算）。", "error")
            return None
        updated, _created = self.projects.upsert(payload)
        self.sync_project_tasks(updated, quiet=False)
        try:
            self.check_untyped_ops(announce_import=False)
        except Exception:                      # noqa: BLE001 - 检查失败不挡刷新
            pass
        stat = pj.stats(updated, self.task_store)
        week = pj.weekly_yield(updated.get("tasks") or [])
        self.say("🔄 项目 #%s「%s」刷新了：%d 条任务、剩余 %d count（进度 %s）"
                 % (num, updated.get("title"), len(updated.get("tasks") or []),
                    stat["remaining"], stat["progress_text"]), "ok")
        if week.get("count"):
            self.say(pj.week_summary(week), "info")
        self.rebuild_rows()
        self.invalidate()
        return updated

    def _project_tasks_refreshable(self):
        """``refresh`` 能不能顺带刷新 OmniFocus 项目任务清单。"""
        if self.offline or not self.net_async:
            return False
        try:
            if not self.omnifocus_enabled():
                return False
        except Exception:                           # noqa: BLE001 - 检查失败不挡刷新
            return False
        try:
            return bool(self.projects.active())
        except Exception:                           # noqa: BLE001
            return False

    def _refresh_project_tasks(self):
        """``refresh`` 时把 OmniFocus 项目任务清单也对一遍（60→50 这种改动要能更新）。"""
        projects = self.projects.active()
        if not projects:
            return False
        today = datetime.date.today()
        self._project_sync_day = today
        try:
            pj.mark_sync_day(today)
        except Exception:                           # noqa: BLE001 - 标记失败不挡对账
            pass
        action = self.net_action(
            "omnifocus.project_sync",
            "刷新 %d 个 OmniFocus 项目任务" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say(
                "OmniFocus 项目任务刷新失败（可用 `project check` 再试）：%s" % error,
                "warn"))
        if not self.jobs.sync:
            self.say("正在后台刷新 OmniFocus 项目任务…", "cmd")
        return True

    # -- 每日项目对账：新增自动拉，删除/修改整包问 -------------------------

    def _prompt_busy(self):
        return bool(self.pending_import is not None or self.nostart is not None
                    or self.planning is not None or self.review_active()
                    or self.pending_advance is not None
                    or self.project_sync is not None)

    def _maybe_daily_project_sync(self):
        """每天第一次 TUI/refresh 时后台检查一次 OmniFocus 项目。"""
        today = datetime.date.today()
        if (not self.auto_project_sync or self.offline or not self.net_async
                or self._project_sync_day == today
                or pj.last_sync_day() == today.isoformat()
                or self._prompt_busy()):
            return False
        try:
            if not self.omnifocus_enabled():
                return False
        except Exception:                           # noqa: BLE001 - 检查失败不挡 TUI
            return False
        projects = self.projects.active()
        if not projects:
            return False
        self._project_sync_day = today
        pj.mark_sync_day(today)
        action = self.net_action(
            "omnifocus.project_sync",
            "每日检查 %d 个 OmniFocus 项目" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say(
                "每日项目检查失败（明天再试，或 `project 1 refresh`）：%s" % error,
                "warn"))
        return True

    def _start_project_sync_now(self):
        """手动触发一次项目对账（``project check``；也方便测试）。"""
        projects = self.projects.active()
        if not projects:
            raise ValueError("项目库是空的：先 `task import` 一个 OmniFocus 项目。")
        action = self.net_action(
            "omnifocus.project_sync",
            "检查 %d 个 OmniFocus 项目" % len(projects),
            {"projects": projects}, source=SRC_OMNIFOCUS)
        self.jobs.submit(
            action,
            on_ok=lambda payload: self._apply_project_sync_result(payload),
            on_fail=lambda error: self.say("项目对账失败：%s" % error, "warn"))
        if not self.jobs.sync:
            self.say("正在后台检查 OmniFocus 项目…", "cmd")
        return True

    def _merge_project_additions(self, project_id, added):
        """把 OmniFocus 新增任务并进本地清单；已有任务一个字不动。"""
        project = self.projects.by_id(project_id)
        if project is None:
            return 0
        tasks = [dict(item) for item in project.get("tasks") or []]
        have = {str(item.get("pk") or "") for item in tasks}
        n = 0
        for item in added or ():
            pk = str(item.get("pk") or "")
            if not pk or pk in have:
                continue
            tasks.append(dict(item))
            have.add(pk)
            n += 1
        if n:
            self.projects.set_tasks(project_id, tasks)
        return n

    def _apply_project_sync_result(self, payload):
        """主线程：自动拉新增；删除 / 修改按项目排队问用户。"""
        records = (payload or {}).get("projects") or []
        for rec in records:
            title = rec.get("title") or rec.get("project_id") or "?"
            if rec.get("invalid"):
                self.say("⚠ 每日项目检查跳过「%s」：%s" % (title, rec["invalid"]),
                         "warn")
                continue
            diff = rec.get("diff") or {}
            added = diff.get("added") or []
            if added:
                n = self._merge_project_additions(rec.get("project_id"), added)
                if n:
                    self.say("📥 项目「%s」自动拉取 %d 条新增任务。" % (title, n), "ok")
            if diff.get("removed") or diff.get("modified"):
                self._project_sync_queue.append(rec)
            project = self.projects.by_id(rec.get("project_id"))
            if project is not None:
                self.sync_project_tasks(project, quiet=True)
        try:
            self.check_untyped_ops(announce_import=False)
            self.projects.ensure_tagged_pros()
        except Exception as e:                     # noqa: BLE001 - 检查失败不挡对账
            self.say("检查 op 类型/项目关联失败：%s" % e, "warn")
        if self._project_sync_queue:
            self._maybe_start_project_sync_conflict()
        self.rebuild_rows()
        self.invalidate()
        return payload

    def _maybe_start_project_sync_conflict(self):
        """没有别的问答在占地时，把下一个项目冲突顶上来。"""
        if self.project_sync is not None or not self._project_sync_queue:
            return False
        if self._prompt_busy():
            return False
        self.project_sync = self._project_sync_queue.pop(0)
        self.invalidate()
        return True

    def _answer_project_sync(self, line):
        """项目对账冲突：l=本地覆盖 / o=OmniFocus 覆盖 / s=先不处理。"""
        text = " ".join(str(line or "").strip().lower().split())
        local = {"l", "local", "本地", "本地覆盖", "1"}
        remote = {"o", "of", "omnifocus", "remote", "远端", "omnifocus覆盖", "2"}
        skip = {"s", "skip", "稍后", "先不处理", "不处理", "q", "3"}
        rec, self.project_sync = self.project_sync, None
        title = (rec or {}).get("title") or "?"
        if text in remote:
            payload = (rec or {}).get("payload")
            try:
                if payload is None:
                    raise ValueError("没有拿到 OmniFocus 清单")
                updated, _created = self.projects.upsert(payload)
                self.sync_project_tasks(updated, quiet=False)
                self.say("📁 项目「%s」已按 OmniFocus 覆盖本地清单。" % title, "ok")
            except Exception as e:                  # noqa: BLE001 - 对账坏了别掀 TUI
                self.had_error = True
                self.say("项目「%s」没能用 OmniFocus 覆盖：%s" % (title, e), "error")
        elif text in local:
            self.say("📁 项目「%s」保留本地清单（OmniFocus 的删除/修改先不应用）。"
                     % title, "ok")
        elif text in skip:
            self.say("📁 项目「%s」这次对账先不处理。" % title, "info")
        else:
            self.project_sync = rec
            self.say("项目对账还在等回答：\n"
                     "  [l] 保留本地覆盖  [o] 用 OmniFocus 覆盖  [s] 先不处理", "warn")
            return True
        self.rebuild_rows()
        self.invalidate()
        self._maybe_start_project_sync_conflict()
        return True

    def project_sync_panel(self):
        """项目对账冲突的打断面板。"""
        rec = self.project_sync or {}
        diff = rec.get("diff") or {}
        body = Text()
        body.append("项目「%s」和 OmniFocus 不一致：\n"
                    % (rec.get("title") or rec.get("project_id") or "?"),
                    style="bold")
        body.append("  " + pj.diff_text(diff) + "\n", style="cyan")
        for item in (diff.get("removed") or [])[:5]:
            body.append("  - 本地有、OmniFocus 没有：%s\n" % (item.get("title") or item.get("pk")),
                        style="yellow")
        for item in (diff.get("modified") or [])[:5]:
            fields = "、".join(change.get("field") or "?"
                              for change in item.get("changes") or [])
            body.append("  ~ 两边不同：%s（%s）\n"
                        % (item.get("title") or item.get("pk"), fields),
                        style="yellow")
        body.append("\n[l] 本地覆盖    [o] OmniFocus 覆盖    [s] 先不处理",
                    style="bold")
        return Panel(body, title="[bold cyan]📁 项目对账[/]",
                     border_style="cyan", box=box.HEAVY)

    def _plan_slots(self, day):
        """这一天的**可用空场** → ``(slots, notes)``。

        只排除三类：已经过去的（今天就按「现在」筛）、已经挂任务的、
        被 formatter 占掉的。**没订上 Focusmate 的场次照常计入**（notes 里点名），
        任务先本地挂到那场，等 daemon 订上之后 ``refresh --check`` 再补挂。
        """
        slots, notes = [], []
        now = self._now_dt()
        now_ms = int(now.timestamp() * 1000)
        is_today = (day == datetime.date.today())
        unbooked, busy, past, reserved = [], 0, 0, 0
        try:
            reservations = th.collect_reservations(
                self.task_store.list(), day.isoformat())
        except Exception:                          # noqa: BLE001 - 坏任务不该挡 plan
            reservations = []
        for index, row in enumerate(self.rows, 1):
            if not row.get("planned"):
                continue
            # 场次的归属看**开始时间**：今天 23:15 开始、明天 00:05 结束
            # 仍然是今天的一场。以前这里还要求 row["date"] 完全相等，
            # 一旦行来自 API、日期字段被写成了终点日，跨午夜场次就整场漏算。
            start_dt = self._row_start_datetime(row, fallback_day=day)
            if start_dt is None or start_dt.date() != day:
                continue
            if row.get("period_muted"):
                continue
            if is_today and start_dt <= now:
                past += 1
                continue
            if self.task_store.tasks_for_session(start_dt.date().isoformat(),
                                                 row.get("startLabel")):
                busy += 1
                continue
            # formatter 已经占掉的时间是**硬约束**：项目 plan 只能看剩下的空场，
            # 绝不能把这条场次再分给项目、更不能回头改 formatter 的切割。
            start_min = th._clock_minutes(row.get("startLabel"))
            end_min = th._clock_minutes(row.get("endLabel"))
            if start_min is None:
                continue
            if end_min is None:
                end_min = start_min + int(row.get("durationMin") or 0)
            if not th.is_free({"start_min": start_min, "end_min": end_min},
                              reservations):
                reserved += 1
                continue
            slots.append(pp.slot_record(index, start_dt.date().isoformat(),
                                        row.get("startLabel"),
                                        row.get("durationMin"), row.get("id"),
                                        row.get("endLabel")))
            if row.get("id") is None:
                unbooked.append(row.get("startLabel") or "?")
        if past:
            notes.append("今天已经过去的 %d 场不算空闲。" % past)
        if busy:
            notes.append("%d 场已经挂着任务了，不算空闲。" % busy)
        if reserved:
            notes.append("%d 场已经被 formatter 占掉，项目 plan 不会碰。" % reserved)
        if unbooked:
            notes.append("%s 还没订上 Focusmate，但仍计入空场（plan 排进去后，"
                         "等订上会自动挂任务）。" % "、".join(unbooked))
        return slots, notes

    def cmd_plan(self, args=None):
        """``plan`` —— 把这一天剩下的空闲切成 count，分给项目，再自动排任务。

        用户要的整条链路：先看元数据（今天还剩多少空闲时间 / 几个 count），再逐个
        项目问「分多少」，敲 ``complete`` 之后按每个项目分到的 count 唤起任务、排进
        session（尽量一场一个任务、尽量不拆开）。

            plan                    今天（**默认就是今天**，和当前看的是哪天无关）
            plan +1                 明天（plan 1 / plan 明天 / plan 2026-11-07 同义）
            task 1 4                项目 #1 分 4 个 count（= 100 分钟）
            task 1 2                再敲一次是**覆盖**（变成 2，不是 6）
            task 1 clear            清空 #1
            complete                分配完了，开始排任务

        分配**超预算**（加起来超过今天剩下的 count）时整条命令都不执行。

        为什么不跟着 `day` 走：`plan` 是「给哪一天分资源」的**动作**，不是看板。
        晚上正在看明天（`day 1`）时顺手敲 `plan`，想排的多半还是明天，所以有
        `plan +1` 这种明确写法；不给参数就是今天 —— 免得「我以为在排今天、
        其实排了后天」。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if self.planning is not None:
            raise ValueError("已经有一次 plan 在等你的分配（看下面那块面板）："
                             "`task <序号> <count>` 分资源、`complete` 开始排、`q` 放弃。")
        if self.pending_import is not None or self.nostart is not None or self.review_active():
            raise ValueError("先把手头那一问答完（收尾 / 到点没开始 / 导入确认）再来 plan。")
        day, offset = self._plan_target(toks)
        self.projects.ensure_tagged_pros()
        projects = self.projects.active()
        if not projects:
            raise ValueError("项目库是空的：在 OmniFocus 里 ⌘C 一个**项目**，再敲 "
                             "`task import sop` 或 `task import incubator`（项目里每条未完成任务都要有「持续时间」）。")
        untyped = self.projects.untyped_ops()
        if untyped:
            self.check_untyped_ops(announce_import=False)
            raise ValueError("还有 %d 个 OmniFocus 项目（op）没说明类型（SOP / incubator）；"
                             "请先按提示补全类型，再 `plan`。"
                             % len(untyped))
        sop_ops = self.projects.eligible_ops(pj.OP_TYPE_SOP)
        inc_ops = self.projects.eligible_ops(pj.OP_TYPE_INCUBATOR)
        if not sop_ops and not inc_ops:
            raise ValueError("当前没有符合项目阶段的可排期 op（running 阶段允许 SOP op，"
                             "bottleneck 阶段允许 incubator op；可用 `pro <序号> -a` 切换阶段"
                             "或用 `op <序号> sop|incubator` 检查类型）。")
        if offset != self.day_offset:
            # 空场、`-a <编号>` 都按**看板那一天**的表格算，所以先把看板搬过去。
            # 搬过去之后必须**等到**那天的真数据：空场判据要看「订没订上」，
            # 拿别的天的旧行去算会把整场排进空气里（wait=True 就是为了这一条）。
            self.day_offset = offset
            try:
                self.refresh(quiet=True, wait=not self.offline)
            except Exception:                      # noqa: BLE001 - 拉不到就照旧
                pass
            try:
                self.rebuild_rows()
            except Exception:                      # noqa: BLE001 - 快照模式也能 plan
                pass
            self.say("· 看板切到 %s（plan 的就是这一天）" % day.isoformat(), "info")
        elif day != datetime.date.today():
            self.say("· plan 的是 %s（不是今天；不带参数就是今天）" % day.isoformat(),
                     "info")
        try:
            self.rebuild_rows()
        except Exception:                          # noqa: BLE001 - 快照模式也能 plan
            pass
        slots, notes = self._plan_slots(day)
        for note in notes:
            self.say("· %s" % note, "info")
        if not slots:
            raise ValueError("%s 一场空着的 session 都没有 —— 换个日子，或者先 "
                             "`session <HH:MM> <25|50|75>` 把排布加上。" % day.isoformat())
        cap = pp.capacity(slots)
        pro_map = {op["id"]: self.projects.pro_for_op(op) for op in projects}
        if sop_ops:
            self.planning = pp.Allocator(
                day, sop_ops, slots, task_store=self.task_store,
                hooks=self.plan_hooks(), stage=pj.OP_TYPE_SOP,
                incubator_projects=inc_ops, pro_map=pro_map)
            self.say("🗂 %s 的 SOP 资源分配：%d 个 SOP op、%d 场空 session = %d count 可以分"
                     "（%d 分钟；1 count = %d 分钟）%s"
                     % (day.isoformat(), len(sop_ops), cap["sessions"], cap["counts"],
                        cap["minutes"], pp.MINUTES_PER_COUNT,
                        ("；完成后将进入 %d 个 incubator op 的时间安排阶段" % len(inc_ops))
                        if inc_ops else ""), "cmd")
        else:
            self.planning = pp.Allocator(
                day, inc_ops, slots, task_store=self.task_store,
                hooks=self.plan_hooks(), stage=pj.OP_TYPE_INCUBATOR,
                incubator_projects=[], pro_map=pro_map)
            self.say("🌱 %s 的 incubator 资源分配（当前无 running 阶段的 SOP op）："
                     "%d 个 incubator op、%d 场空 session = %d count 可以分"
                     "（%d 分钟；1 count = %d 分钟）"
                     % (day.isoformat(), len(inc_ops), cap["sessions"], cap["counts"],
                        cap["minutes"], pp.MINUTES_PER_COUNT), "cmd")
        self.say("   `task <序号> <count>` 分给项目（序号 = 面板上的 #）、"
                 "`task <序号> clear` 清空、`complete` 开始排任务、`q` 放弃。", "info")
        self.invalidate()
        return self.planning

    #: `plan` 的日期参数里认的那几个人话
    PLAN_DAY_WORDS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1,
                      "后天": 2, "大后天": 3, "昨天": -1, "yesterday": -1}

    def _plan_target(self, toks):
        """``plan`` 的参数 → ``(day, offset)``。不给参数 = **今天**（用户要求）。"""
        today = datetime.date.today()
        if not toks:
            return today, 0
        if len(toks) > 1:
            raise ValueError("plan 只认一个日期：plan / plan +1 / plan 明天 / plan 2026-11-07")
        tok = toks[0]
        low = tok.lower()
        if low in self.PLAN_DAY_WORDS:
            offset = self.PLAN_DAY_WORDS[low]
        else:
            try:
                offset = int(tok)
            except ValueError:
                try:
                    day = datetime.date.fromisoformat(tok)
                except ValueError:
                    raise ValueError(
                        "plan 的参数要么是偏移量（+1 / 0 / -1）、今天/明天/后天，"
                        "要么是 YYYY-MM-DD；你给的是 %r" % tok) from None
                offset = (day - today).days
        if offset < 0:
            raise ValueError("%s 是过去的日子 —— 那是已经发生的事，不用再 plan"
                             "（`plan` 不带参数就是今天，`plan +1` 是明天）。"
                             % (today + datetime.timedelta(days=offset)).isoformat())
        if offset > 60:
            raise ValueError("最多 plan 到 +60 天（你给的是 %+d）" % offset)
        return today + datetime.timedelta(days=offset), offset

    @staticmethod
    def _row_start_datetime(row, fallback_day=None):
        """一行 session 的本地开始时间。

        ``startMs`` 是权威值；老快照/手工造的行没有它时，退回 ``date`` +
        ``startLabel``。plan 的「哪一天」一律按这个**开始时间**判，不按终点：
        23:15–00:05 这种跨午夜的场次属于 23:15 那天。
        """
        raw_ms = (row or {}).get("startMs")
        try:
            if raw_ms is not None and int(raw_ms) > 946684800000:
                return datetime.datetime.fromtimestamp(int(raw_ms) / 1000.0)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        raw_day = (row or {}).get("date")
        if fallback_day is not None and not raw_day:
            raw_day = fallback_day
        if hasattr(raw_day, "isoformat"):
            raw_day = raw_day.isoformat()
        label = str((row or {}).get("startLabel")
                    or (row or {}).get("start") or "").strip()
        try:
            day = datetime.date.fromisoformat(str(raw_day))
            hour, minute = (int(x) for x in label.split(":")[:2])
            return datetime.datetime.combine(day, datetime.time(hour, minute))
        except (TypeError, ValueError):
            return None

    def free_plan_slots(self, day):
        """那一天「还没开始、没静音、没挂任务」的排好的场次（只看 data.csv + 本地绑定）。

        和 :meth:`_plan_slots` 的区别只有一个：**不问 API**（所以不知道「订上没」）。
        警告板每 5 秒算一次，不能为了提醒去拉一次网络。
        """
        day = day or datetime.date.today()
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        now = self._now_dt()
        out = []
        try:
            sessions = self.sessions_for_day_plan(day)
        except Exception:                          # noqa: BLE001 - 下载层坏了不该没帧
            return out
        try:
            reservations = th.collect_reservations(self.task_store.list(), iso)
        except Exception:                          # noqa: BLE001
            reservations = []
        for sess in sessions:
            label = sess.get("start")
            try:
                hour, minute = (int(x) for x in str(label).split(":")[:2])
                start = datetime.datetime.combine(day, datetime.time(hour, minute))
            except (TypeError, ValueError):         # noqa: BLE001 - 坏行跳过
                continue
            # 只按**开始日期**归属这一天；开始后即使终点跨到明天，也已经
            # 不再是一场空 session。
            if start.date() != day or start <= now:
                continue
            if self.task_store.tasks_for_session(iso, label):
                continue
            start_min = th._clock_minutes(label)
            end_min = th._clock_minutes(sess.get("end"))
            if start_min is None:
                continue
            if end_min is None:
                end_min = start_min + int(sess.get("duration") or 0)
            if not th.is_free({"start_min": start_min, "end_min": end_min},
                              reservations):
                continue
            try:
                if mute_window_for(day, label):
                    continue
            except Exception:                      # noqa: BLE001
                pass
            out.append(sess)
        return out

    def pending_plan(self, day=None):
        """今天还没 plan 吗？→ ``{"sessions", "counts", "starts"}`` 或 ``None``。

        三条同时成立才算「该提醒」：项目库里有 active 项目、这一天**一个项目都没分过
        count**、而且这一天还有没开始、没挂任务、没静音的场次。已经 plan 过（哪怕只
        排了一部分）就不再吵 —— 提醒是用来「别忘了」，不是用来记账的。
        """
        day = day or datetime.date.today()
        try:
            projects = self.projects.active()
        except Exception:                          # noqa: BLE001 - 项目库坏了别挡住警告板
            return None
        if not projects:
            return None
        allocated = 0
        for project in projects:
            plan = pj.plan_of(project, day)
            allocated += int((plan or {}).get("count") or 0)
        if allocated:
            return None
        slots = self.free_plan_slots(day)
        if not slots:
            return None
        counts = sum(int(s.get("duration") or 0) // pp.MINUTES_PER_COUNT for s in slots)
        return {"sessions": len(slots), "counts": counts,
                "starts": [s.get("start") for s in slots]}

    def plan_hooks(self):
        """`plan` 要碰外面世界的那个口子（其余全在 project_plan.Allocator 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            "apply": lambda allocator, allocs: self._plan_apply(allocator, allocs),
            "remaining_slots": lambda allocator, summary: self._plan_remaining_slots(
                allocator, summary),
        }

    def _plan_remaining_slots(self, allocator, summary):
        """SOP 阶段排完之后，给 incubator 阶段留下的空场列表。"""
        used_idx = set((summary or {}).get("used_slot_indices") or ())
        out = []
        for slot in allocator.slots or ():
            if slot.get("index") in used_idx:
                continue
            if self.task_store.tasks_for_session(slot.get("date"), slot.get("start")):
                continue
            out.append(dict(slot))
        return out

    def _plan_apply(self, allocator, allocs):
        """分配完了：先记资源，再挑任务、排进 session。

        「尽量一场一个任务、尽量不拆开」在 :func:`project_plan.schedule` 里：先跑
        二分图匹配求「一对一站位」，配不上才拆、才往同一场里叠。
        """
        day = allocator.day
        invested = pj.invested_by_project(self.task_store)
        # formatter 任务 / 尚未到 defer 点的本地任务都不该被项目 plan 再算一遍。
        # 前者已经有自己的时间占位，后者现在根本还没进入可安排队列。
        excluded = {}
        for task in self.task_store.list():
            pid, _title, pk = ts.TaskStore.project_task_fields(task)
            if not pk or pid is None or not ts.TaskStore.is_open(task):
                continue
            if task.get("formatter") or ts.task_is_deferred(task):
                excluded.setdefault(pid, set()).add(str(pk))
        items, notes = pp.distribute(
            allocs, [a["project"] for a in allocs], invested,
            now=datetime.datetime.now(), exclude_pks=excluded)
        placements, unscheduled, more = pp.schedule(items, allocator.slots)
        notes = list(notes) + list(more)
        for alloc in allocs:
            self.projects.set_allocation(alloc["project_id"], day, alloc["count"])
        made, failed = {}, 0
        for place in placements:
            item = place["item"]
            slot = place["slot"]
            project = item["project"]
            url = of.task_url(item["pk"])
            parent_pk = item.get("parent_pk") or place.get("parent", {}).get("pk")
            parent_title = (item.get("parent_title")
                            or place.get("parent", {}).get("title"))
            fields = {"title": item.get("title") or item["pk"],
                      pj.PROJECT_FIELD: project.get("title") or "",
                      pj.PROJECT_ID_FIELD: item["project_id"],
                      pj.PROJECT_TASK_FIELD: item["pk"]}
            if item.get("note"):
                fields["note"] = item["note"]
            if parent_pk:
                # session 里只放子任务；note 明确写它是谁的子。
                parent_note = "父任务：%s" % (parent_title or parent_pk)
                fields["note"] = ((fields.get("note") or "") + "\n"
                                  + parent_note).strip()
                fields["duration"] = int(place.get("duration")
                                         or item.get("duration") or 0)
            elif place.get("by_minutes") or item.get("small"):
                # 短任务按实际分钟排；duration 写真实分钟（10 分钟就是 10 分钟），
                # count 交给 task_store.create 的 link_count_fields 去 ceil。
                fields["duration"] = int(place.get("duration")
                                         or item.get("minutes") or 0)
            else:
                fields["count"] = int(place["count"])
            source = {"kind": "project", "id": item["pk"], "url": url,
                      "title": item.get("title")}
            if item.get("local_only"):
                # relate --import 从模板文件 append 的任务在 OmniFocus 里没有
                # 真 primary key：不能挂 omnifocus，否则完成时会拿假 pk 写回。
                source["local_only"] = True
                source["url"] = ""
                url = ""
            if parent_pk:
                source.update({"parent_id": parent_pk,
                               "parent_title": parent_title})
            existing = self._project_local_task(item["project_id"], item["pk"])
            try:
                if (existing is not None and ts.TaskStore.is_open(existing)
                        and not existing.get("session")):
                    # 项目导入时已经把叶子任务镜像进任务库了：plan 唤起时复用
                    # 同一条，并把这次分到的 count / duration / 父任务来源写上去。
                    task = self.task_store.apply_project_plan(
                        existing["id"], fields, source=source)
                else:
                    task = self.task_store.create(
                        fields,
                        omnifocus=None if item.get("local_only") else {
                            "id": item["pk"], "url": url,
                            "title": item.get("title")},
                        source=source)
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」没建成本地任务：%s" % (item.get("title"), e), "error")
                continue
            made.setdefault(item["project_id"], []).append({
                "pk": item["pk"], "task_id": task["id"],
                "count": int(place["count"]), "start": slot.get("start") or "",
                "duration": int(place.get("duration")
                                or int(place["count"]) * pp.MINUTES_PER_COUNT),
                "parent_pk": parent_pk or ""})
            try:
                self._assign_task(task, str(slot["index"]), refresh=False,
                                  allow_unbooked=True)
            except ValueError as e:
                failed += 1
                self.say("⚠ 「%s」建好了但没挂到 session #%s：%s"
                         % (item.get("title"), slot["index"], e), "warn")
        for pid, records in made.items():
            self.projects.record_items(pid, day, records)
        self.rebuild_rows()
        total_counts = sum(int(p["count"]) for p in placements)
        self.say("🗂 %s 排好了：%d 条任务 → %d 场 session（共 %d count = %d 分钟）"
                 % (day.isoformat(), len(placements),
                    len({p["slot"]["index"] for p in placements}), total_counts,
                    total_counts * pp.MINUTES_PER_COUNT), "ok")
        for place in placements:
            item = place["item"]
            dur = place.get("duration")
            tail = ("，%d 分钟" % int(dur)) if dur else ""
            parent = place.get("parent") or {}
            if parent:
                tail += "，父任务「%s」" % (parent.get("title") or parent.get("pk"))
            self.say("   %s(%d) ← %s / %s（%d count%s%s%s）"
                     % (place["slot"].get("start"), int(place["slot"].get("duration") or 0),
                        item["project"].get("title") or "?",
                        item.get("title") or item["pk"], int(place["count"]), tail,
                        "，拆开的" if place.get("split") else "",
                        "，和别的任务同一场" if place.get("stacked") else ""), "info")
        for note in notes:
            self.say("⚠ %s" % note, "warn")
        for item in unscheduled:
            self.say("⚠ 「%s」（%d count）没排进去：空场次不够。"
                     % (item.get("title"), int(item.get("count") or 0)), "warn")
        if failed:
            self.had_error = True
        self.invalidate()
        return {"placements": len(placements), "counts": total_counts,
                "unscheduled": len(unscheduled), "notes": notes, "failed": failed,
                "used_slot_indices": {p["slot"]["index"] for p in placements}}

    def plan_panel(self):
        """`plan` 的分资源问答面板（挂在 repl_kit 的打断栏位里）。"""
        prompt = self.planning
        if prompt is None:
            return None
        body = Text()
        for text, style in prompt.lines():
            body.append(text + "\n", style=style or None)
        return Panel(body, title="[bold cyan]🗂 plan · 资源分配[/]",
                     border_style="cyan", box=box.HEAVY)

    def close_plan(self, note=None):
        """把 `plan` 那一问收掉（`complete` / `q` 之后）。"""
        prompt, self.planning = self.planning, None
        if prompt is None:
            return None
        if not prompt.closed:
            prompt.closed = True
        self.rebuild_rows()
        self.invalidate()
        return prompt

    def focus_day(self, day_iso):
        """把看板切到某一天（收尾时用：那一场在哪天就看哪天）。"""
        try:
            target = datetime.date.fromisoformat(str(day_iso))
        except ValueError:
            return False
        offset = (target - datetime.date.today()).days
        if offset == self.day_offset:
            return True
        self.day_offset = max(-3650, min(3650, offset))
        try:
            self.refresh(quiet=True)
        except Exception:                        # noqa: BLE001
            pass
        return True

    def cmd_review(self, args=None):
        """``review [HH:MM | 2026-10-27T11:00]`` —— 手动开始一场收尾。

        自动收尾靠 daemon（结束 + 15 秒）拉起；这条命令是给
        「刚才那场没催上 / 想补收一场」用的，走的是同一套流程。
        """
        wanted = str(args[0]).strip() if args else ""
        pending = psv.pending_sessions()
        entry = psv.pick(pending, wanted) if wanted else (pending[-1] if pending else None)
        if entry is None:
            # 手动收尾往回看一整天（自动催只看 3 小时，见 postsession.LOOKBACK_SEC）：
            # 「上午那场我忘了收」正是这条命令存在的理由。
            sessions = psv.ended_sessions(
                psv.slots_around(self.reload_entries(), days_back=1,
                                 actual=self.actual_slots()),
                lookback=36 * 3600)
            entry = psv.pick(sessions, wanted)
        if entry is None:
            self.say("没有可以收尾的场次%s（要看的是「已经结束」的那一场）。"
                     % (("：%r" % wanted) if wanted else ""), "warn")
            return None
        return self.begin_review(entry)

    def _review_worker(self, entry):
        """后台核对完成状态（bridge / osascript 都可能几秒，别卡着画帧）。"""
        try:
            self.sync_completion_state(entry)
        except Exception as e:                   # noqa: BLE001 - 收尾不能因为核对失败就不做
            self.say("核对完成状态出错（照常收尾）：%s: %s" % (type(e).__name__, e), "warn")
        finally:
            if self.review_check is not None:
                self.review_check["ready"] = True

    def _maybe_start_review(self):
        """每帧看一眼：daemon 说哪一场该收尾了，就接过来（**只在主线程动手**）。"""
        if self.review is not None:
            return
        if self.nostart is not None:
            return                              # 「没开始」那一问还开着：先答它
        if self.planning is not None or self.project_sync is not None:
            return                              # plan / 项目对账在等回答：别抢输入
        now = time.time()
        if now - self.review_at < 1.0:
            return
        self.review_at = now
        if self.review_check is not None:
            if self.review_check.get("ready"):
                entry = self.review_check.get("entry") or {}
                self.review_check = None
                self.begin_review(entry, sync=False)
            return
        if not self.settings.get_bool("postsession"):
            return
        if self.pending_sins():
            # 罪案区锁死：除了宣言什么都不受理，收尾也排在后面（别把面板顶在锁上面）
            return
        entry, derived = self._next_review_entry()
        if entry is None:
            return
        self.review_check = {"entry": entry, "ready": False}
        threading.Thread(target=self._review_worker, args=(entry,), daemon=True).start()
        if derived:
            self.say("⏱ %s %s 那场已经结束、还没收尾（daemon 队列里没有，是本地自己算的）"
                     " —— 正在核对完成状态…"
                     % (entry.get("date"), entry.get("start")), "cmd")
        else:
            self.say("⏱ %s %s 那场结束了 —— 正在核对完成状态…"
                     % (entry.get("date"), entry.get("start")), "cmd")

    # -- 「这一场到点没开始」那一问 -------------------------------------
    #
    # 事实只有一个来源：**表格**。planned 有、id 没有 = 契约有、API 里没这一场
    # （用户原话：「refresh 能发现这个 session 没了就行」）。所以这里不需要任何
    # 新接口 —— 到点 5 分钟之后 API 还是空的，就问；
    # 问的时候不判「没完成」，而是给三个选择（见 postsession.NostartReview）。

    def _maybe_start_nostart(self):
        """每帧看一眼：有没有「早该开始、API 里却没有」的场次该问人。"""
        if self.nostart is not None:
            return
        now_ts = time.time()
        if now_ts - self.nostart_at < 1.0:      # 节流：别每帧都遍历
            return
        self.nostart_at = now_ts
        if not self.settings.get_bool("nostart"):
            return
        if self.review is not None or self.review_check is not None:
            return                              # 一次只问一件事（收尾优先）
        if self.planning is not None or self.project_sync is not None:
            return                              # plan / 项目对账在等回答：别抢输入
        if self.pending_sins():
            return                              # 罪案区锁死：除了宣言什么都不受理
        entry = self._next_nostart_entry()
        if entry is None:
            return
        self.nostart = psv.NostartReview(entry, hooks=self.nostart_hooks())
        self.say("🚪 %s 那场到点没开始（API 里没有这一场）—— "
                 "p 推迟 / x 不去了 / s 先不管。" % self.nostart.solo(), "cmd")
        self.invalidate()

    def _next_nostart_entry(self):
        """该问哪一场：daemon 入队的优先（它会专门叫我一声），其次自己按表格算。"""
        try:
            queued = psv.nostart_pending()
        except Exception:                       # noqa: BLE001 - 状态文件坏了不该没帧
            queued = []
        if queued:
            entry = dict(queued[0])
            if not entry.get("period_id"):
                try:
                    day = datetime.date.fromisoformat(str(entry.get("date")))
                    period = sp.period_for_session(self.periods_for_day(day),
                                                   entry.get("start"))
                    if period:
                        entry["period_id"] = period.get("id") or ""
                except (TypeError, ValueError):
                    pass
            return entry
        if not self.authoritative:
            # 拿不到真数据（离线 / 快照）就别判：「API 里没有这一场」这句话必须有依据
            return None
        now = datetime.datetime.now()
        try:
            answered = psv.nostart_answered()
        except Exception:                       # noqa: BLE001
            answered = set()
        for row in self.rows:
            entry = self._nostart_entry_for_row(row, now, answered)
            if entry:
                return entry
        return None

    def _nostart_entry_for_row(self, row, now, answered):
        """表格里这一行该不该问？→ 该问就返回 entry，不该问返回 None。"""
        if not row.get("planned") or row.get("id") is not None:
            return None
        try:
            start = datetime.datetime.fromtimestamp(row["startMs"] / 1000.0)
        except (KeyError, TypeError, ValueError):
            return None
        if now < start + datetime.timedelta(seconds=psv.NOSTART_GRACE_SEC):
            return None                          # 预约窗口里还不问（刚过几分钟很正常）
        if (now - start).total_seconds() > psv.NOSTART_LOOKBACK_SEC:
            return None                          # 太老了：别翻旧账
        key = psv.session_key(row.get("date"), row.get("startLabel"))
        if key in answered:
            return None                          # 问过/答过了
        try:
            day = datetime.date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError):
            return None
        return psv.nostart_entry(day, row.get("startLabel"), row.get("durationMin"),
                                 now=now, period_id=row.get("period_id"))

    def nostart_hooks(self):
        """「没开始」那一问要碰外部世界的几个口子（其余全在 postsession 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            "postpone": lambda entry, period_id, minutes: self._nostart_postpone(
                entry, period_id, minutes),
            "mark_missed": lambda entry, note=None: self._nostart_mark_missed(entry, note),
            # 不去了 → 挂在这场的任务交给收尾（A 延期 / B 放弃 / C 重议 / E 推后）。
            # credit=False：这一场一分钟都没干，不记 count。
            "open_review": lambda entry: self.begin_review(
                {"kind": "session", "key": entry.get("key"), "date": entry.get("date"),
                 "start": entry.get("start"), "end": entry.get("end"),
                 "duration": entry.get("duration"),
                 "nostart": True},
                sync=False, credit=False),
        }

    def _nostart_postpone(self, entry, period_id, minutes):
        """推迟：单场 / 整段都走 **relocate 那一套底层**（取消 → 落盘 → 任务跟着挪）。"""
        try:
            day = datetime.date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            raise ValueError("这一场没有合法日期（%r），推迟不了。" % entry.get("date"))
        delta = int(minutes)
        if period_id:
            period_id = str(period_id).upper()
            self._relocate_period(day, period_id, delta, allow_past=True,
                                  reason="nostart-postpone")
            return {"target": "整段 %+d 分钟" % delta}
        row = None
        for cand in self.rows:
            if (cand.get("date") == entry.get("date")
                    and cand.get("startLabel") == entry.get("start")):
                row = cand
                break
        if row is None:
            row = self._synth_planned_row(day, entry)
        moves = self._relocate_moves(day, [row], delta, allow_past=True)

        def done():
            self._relocate_say(day, moves, delta, label="这一场")
            self._relocate_after(day, moves)

        ok = self._apply_relocate(day, moves, delta, later_scope=None,
                                  reason="nostart-postpone", on_done=done)
        if ok is False:
            raise ValueError("原 session 取消失败；这一场的推迟已经回滚。")
        return {"target": moves[0]["to"]}

    def _nostart_mark_missed(self, entry, note=None):
        """这一场算缺失：state 记 missed + 判「已放弃」（daemon 不重订、也不记罪）。

        **不在输入线程上写盘**：x 这一下要立刻把面板收掉、马上进入收尾；
        两个状态文件交给后台队列，进度条会显示「记录缺失」。
        """
        key = str(entry.get("key")
                  or psv.session_key(entry.get("date"), entry.get("start")))
        why = "到点没开始，我选了「不去了」（%s %s）" % (entry.get("date"),
                                                        entry.get("start"))
        action = self.net_action(
            "session.missed",
            "记录缺失 %s %s" % (entry.get("date") or "?", entry.get("start") or "?"),
            {"key": key, "date": entry.get("date"), "start": entry.get("start"),
             "why": why},
            source=SRC_KEEP)
        job = self.jobs.submit(action, on_fail=lambda error: self.say(
            "⚠ 后台记「缺失」失败：%s（这次收尾照常进行）" % error, "warn"))
        if self.jobs.sync:
            outcome = job.outcome
            if outcome is not None and not outcome.ok:
                return "后台记录失败"
            return why
        return "正在后台记录（进度看面板）"

    def close_nostart(self, note=None, action="deferred"):
        """把「没开始」那一问收掉（没收过的记成 deferred，免得下次又弹）。"""
        prompt, self.nostart = getattr(self, "nostart", None), None
        if prompt is None:
            return None
        if not prompt.closed:
            try:
                psv.mark_nostart(prompt.entry.get("key"), action, entry=prompt.entry,
                                 note=note or "closed")
            except OSError:
                pass
        self.rebuild_rows()
        self.invalidate()
        return prompt

    def nostart_panel(self):
        """「到点没开始」那一问的正文（挂在 repl_kit 的打断栏位里）。"""
        prompt = self.nostart
        if prompt is None:
            return None
        body = Text()
        for text, style in prompt.lines():
            body.append(text + "\n", style=style or None)
        return Panel(body, title="[bold yellow]🚪 到点没开始[/]",
                     border_style="yellow", box=box.HEAVY)

    def _next_review_entry(self):
        """该收尾的是哪一场 → ``(entry, 是不是本地自己算出来的)``。

        先看 daemon 的队列（`temp/keep_session-postsession.json`）；队列里没有就
        **自己按 data.csv + 任务库算一遍**（:func:`postsession.unreviewed_sessions`）。

        为什么必须自己算：daemon 只是「闹钟」。它没在跑、队列文件丢了、或者 warden
        是事后才打开的 —— 这些情况下队列永远是空的，于是「一场结束了、任务还没
        收尾」这件事**永远不会被问到**，完成状态也就永远落不了库（真实踩过）。
        收过的场次记在 postsession 状态文件的 done 里，不会重复催。
        """
        pending = psv.pending_sessions()
        if pending:
            return pending[-1], False
        if self.offline:
            return None, False
        try:
            entries = self.reload_entries()
        except Exception:                          # noqa: BLE001 - data.csv 坏了不该没帧
            return None, False
        try:
            done_keys = set(psv.load_state()["done"])
        except Exception:                          # noqa: BLE001
            done_keys = set()
        try:
            rows = psv.unreviewed_sessions(self.task_store, entries,
                                           done_keys=done_keys, now=self.review_now,
                                           actual=self.actual_slots())
        except Exception:                          # noqa: BLE001
            return None, False
        if not rows:
            return None, False
        return rows[-1], True

    def render_interrogate(self):
        """repl_kit 的「打断栏位」里画什么 —— 收尾/查账那句问话。

        以前这块面板是塞在契约视图/任务视图自己的 render 里的：视图一换就没了、
        表格一长就被挤出屏幕，而它恰恰是屏幕上最该看见的东西。现在它归框架的那个
        栏位管（压在整块看板下面、提示行上面），切视图、滚表格都不影响。
        """
        if self.pending_sins():
            return self.render_lockdown_panel()
        if self.help_overlay is not None:
            return self.help_overlay
        if self.pending_import is not None:
            return self.pending_import_panel()
        if self.pending_advance is not None:
            return self.pending_advance_panel()
        if self.project_sync is not None:
            return self.project_sync_panel()
        if self.pending_relate_path is not None:
            return self.pending_relate_path_panel()
        if self.nostart is not None:
            return self.nostart_panel()
        if self.planning is not None:
            return self.plan_panel()
        rev = self.review_panel()
        if rev is not None:
            return rev
        if self.pending_op_type:
            return self.pending_op_type_panel()
        return None

    def check_untyped_ops(self, announce_import=False):
        """检查是否有未设置类型（``SOP`` / ``incubator``）的 op，并排队让用户补全。"""
        try:
            self.projects.ensure_tagged_pros()
            untyped = self.projects.untyped_ops()
        except Exception:                          # noqa: BLE001
            return []
        self.pending_op_type = [p["id"] for p in untyped]
        if untyped:
            first = untyped[0]
            num = self.projects.number_of(first["id"]) or first["id"]
            if announce_import:
                self.say("⚠ 导入 OmniFocus 项目（op）需说明类型：请为 #%s「%s」输入 "
                         "`SOP` 或 `incubator`（下次可直接 `task import sop` / `op import incubator`）。"
                         % (num, first.get("title")), "warn")
            else:
                self.say("⚠ 发现 %d 个 OmniFocus 项目（op）尚未设置类型（SOP / incubator）："
                         "请为 #%s「%s」输入 `SOP` 或 `incubator`。"
                         % (len(untyped), num, first.get("title")), "warn")
            self.invalidate()
        return untyped

    def pending_op_type_panel(self):
        """未设置类型的 op 提示面板（挂在打断栏位）。"""
        valid_ids = []
        for oid in self.pending_op_type or ():
            op = self.projects.by_id(oid)
            if (op is not None and op.get("status") == pj.STATUS_ACTIVE
                    and pj.normalize_op_type(op.get("op_type")) is None):
                valid_ids.append(oid)
        self.pending_op_type = valid_ids
        if not valid_ids:
            return None
        cur = self.projects.by_id(valid_ids[0])
        num = self.projects.number_of(cur["id"]) or cur["id"]
        pk = (cur.get("omnifocus") or {}).get("id") or "?"
        body = Text()
        body.append("OmniFocus 项目（op）#%s「%s」（%s）尚未指定类型（共 %d 个待补全）：\n"
                    % (num, cur.get("title") or "?", pk, len(valid_ids)),
                    style="bold")
        body.append("  [SOP]       普通标准项目流程（项目处于 running 阶段时进行，plan 第一阶段安排）\n",
                    style="cyan")
        body.append("  [incubator] 瓶颈孵化器流程（项目处于 bottleneck 阶段时进行，plan 第二阶段安排）\n",
                    style="magenta")
        body.append("\n请直接输入 SOP 或 incubator（输入 skip 暂不处理）", style="bold")
        return Panel(body, title="[bold yellow]📁 补全 op 类型（SOP / incubator）[/]",
                     border_style="yellow", box=box.HEAVY)

    def _answer_pending_op_type(self, line):
        """处理用户在 ``pending_op_type`` 面板下的输入；若敲的是常规命令则放行派发。"""
        valid_ids = []
        for oid in self.pending_op_type or ():
            op = self.projects.by_id(oid)
            if (op is not None and op.get("status") == pj.STATUS_ACTIVE
                    and pj.normalize_op_type(op.get("op_type")) is None):
                valid_ids.append(oid)
        self.pending_op_type = valid_ids
        if not valid_ids:
            return None
        text = " ".join(str(line or "").strip().split())
        if not text:
            return True
        low = text.lower()
        if low in ("skip", "稍后", "先不处理"):
            self.pending_op_type = []
            self.say("⏭ 暂不补全 op 类型（下次 refresh 会再次检查；可随时用 `op <序号> sop|incubator` 设置）。",
                     "info")
            self.invalidate()
            return True
        norm_type = pj.normalize_op_type(text)
        if norm_type in pj.OP_TYPES:
            op_id = self.pending_op_type.pop(0)
            updated = self.projects.set_op_type(op_id, norm_type)
            num = self.projects.number_of(updated["id"]) or updated["id"]
            pro = self.projects.pro_for_op(updated)
            pro_note = (" · 关联项目「%s」(%s)" % (pro.get("title"), pro.get("stage"))
                        if pro else "")
            self.say("✅ op #%s「%s」已设为 %s%s。"
                     % (num, updated.get("title"), norm_type, pro_note), "ok")
            if self.pending_op_type:
                nxt = self.projects.by_id(self.pending_op_type[0])
                if nxt is not None:
                    nnum = self.projects.number_of(nxt["id"]) or nxt["id"]
                    self.say("👉 下一个待补全：op #%s「%s」，请输入 SOP 或 incubator："
                             % (nnum, nxt.get("title")), "cmd")
            self.rebuild_rows()
            self.invalidate()
            return True
        # 如果用户输入的是合法 TUI 命令（如 day / plan / task / pro / op / refresh 等），放行派发
        parts = self._split(line)
        if parts:
            cmd_head = parts[0].lower()
            if (self.shell.registry.get(cmd_head) is not None
                    or (isinstance(self.view, ContractView)
                        and re.match(r"^[Tt]\d+$", parts[0]))):
                return None
        cur = self.projects.by_id(self.pending_op_type[0])
        cnum = (self.projects.number_of(cur["id"]) if cur else None) or self.pending_op_type[0]
        self.say("op #%s「%s」还在等类型：请输入 `SOP` 或 `incubator`（或 `skip` 稍后处理）。"
                 % (cnum, (cur or {}).get("title") or "?"), "warn")
        self.invalidate()
        return True

    def pending_advance_panel(self):
        """``pro <序号> -a / --advance`` 等待提供新 ``op`` 的打断面板。"""
        adv = self.pending_advance or {}
        body = Text()
        body.append("项目「%s」切换阶段：%s → %s\n"
                    % (adv.get("pro_title") or "?",
                       adv.get("from_stage") or "?",
                       adv.get("target_stage") or "?"),
                    style="bold")
        body.append("  需要关联新的 %s OmniFocus Project (op)：\n"
                    % (adv.get("required_op_type") or "op"), style="cyan")
        body.append("  · 输入 `task import [链接]`（或 `op import`）直接从剪贴板/链接导入并关联\n",
                    style="green")
        body.append("  · 输入 `op <序号>`（或直接输入 op 序号）关联已有的 OmniFocus Project\n",
                    style="green")
        body.append("  · 输入 `q` 取消切换\n", style="dim")
        return Panel(body, title="[bold cyan]🔀 pro --advance · 关联新 op[/]",
                     border_style="cyan", box=box.HEAVY)

    def _answer_pending_advance(self, line):
        """处理 ``pro <序号> -a / --advance`` 问答下的输入。"""
        adv = self.pending_advance
        if adv is None:
            return None
        raw = str(line or "").strip()
        if not raw:
            self.say("请提供要关联的 %s op：`task import [链接]` 或 `op <序号>`（`q` 取消）。"
                     % (adv.get("required_op_type") or "op"), "warn")
            return True
        low = raw.lower()
        if low in ("q", "quit", "cancel", "abort", "取消", "放弃"):
            self.pending_advance = None
            self.say("⏹ 已取消项目「%s」的阶段切换（仍为 %s）。"
                     % (adv.get("pro_title") or "?", adv.get("from_stage") or "?"), "info")
            self.invalidate()
            return True
        parts = raw.split()
        head = parts[0].lower()
        try:
            if head == "task" and len(parts) >= 2 and parts[1].lower() in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[2:])
                return True
            if head in ("op", "ofp") and len(parts) >= 2 and parts[1].lower() in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[2:])
                return True
            if head in ("import", "导入", "粘贴"):
                self.cmd_task_import(parts[1:])
                return True
            if of.link_kind(raw) == "project":
                self.cmd_task_import(parts)
                return True
            op_tok = " ".join(parts[1:]).strip() if head in ("op", "ofp", "omnifocus-project") else raw
            op = self.projects.find(op_tok)
            self._finish_pro_advance(adv["pro_id"], op["id"],
                                     target_stage=adv.get("target_stage"))
        except Exception as e:                      # noqa: BLE001
            self.had_error = True
            self.say(str(e), "warn")
        self.invalidate()
        return True

    def pending_import_panel(self):
        """``task import`` 发现重复任务时的询问面板（打断栏位）。"""
        pending = self.pending_import or {}
        items = pending.get("items") or []
        repeats = pending.get("repeats") or {}
        body = Text()
        if len(items) == 1:
            item = items[0] or {}
            title = item.get("title") or item.get("id") or "（无标题）"
            body.append("OmniFocus 任务「%s」是重复任务。\n" % title, style="bold")
        else:
            body.append("这批 %d 条里有 %d 条是 OmniFocus 重复任务：\n"
                        % (len(items), len(repeats)), style="bold")
        any_supported = False
        for index in sorted(repeats):
            rec = repeats[index] or {}
            info = rec.get("info") or {}
            plan = rec.get("plan") or {}
            item = items[index] if index < len(items) else {}
            title = (item or {}).get("title") or (info or {}).get("id") or "（无标题）"
            rule = (info or {}).get("rule_string") or "（重复规则）"
            body.append("· %s  %s\n" % (title, rule), style="cyan")
            if plan.get("supported"):
                any_supported = True
                body.append("  复制到本地 → %s\n" % plan.get("summary"), style="green")
            else:
                body.append("  不能复制：%s\n" % (plan.get("reason") or "规则无法映射"),
                            style="yellow")
            note = plan.get("note")
            if note:
                body.append("  %s\n" % note, style="dim")
        if any_supported:
            body.append("\n[y] 复制重复设置并导入    [n] 只导入这一次    [q] 取消",
                        style="bold")
        else:
            body.append("\n[n] 只导入这一次    [q] 取消（这些规则本地 repeat 表达不了）",
                        style="bold")
        return Panel(body, title="[bold cyan]📥 导入重复任务[/]",
                     border_style="cyan", box=box.HEAVY)

    def review_panel(self):
        """收尾 / 查账面板的正文（由 :meth:`render_interrogate` 挂进栏位）。"""
        if self.review is not None:
            body = Text()
            for text, style in self.review.lines():
                body.append(text + "\n", style=style or None)
            if self.review.due_mode():
                head = "🔍 inspect-due · %s" % self.review.headline()
            else:
                head = "⏱ 收尾 · %s" % self.review.headline()
            return Panel(body, title="[bold yellow]%s[/]" % head,
                         border_style="yellow", box=box.HEAVY)
        if self.review_check is not None:
            entry = self.review_check.get("entry") or {}
            return Panel(
                Text("正在核对 %s %s 那场的完成状态（Focusmate + OmniFocus）…"
                     % (entry.get("date"), entry.get("start")), style="dim"),
                title="[bold yellow]⏱ 收尾[/]", border_style="dim", box=box.ROUNDED)
        return None

    def after_dispatch_review(self, line):
        """派发完一条命令之后回头问收尾一句：刚才那条算不算把任务转移走了？"""
        if self.review is None:
            return
        self.review.after_dispatch(line)
        if self.review is not None and self.review.closed:
            self.close_review()

    def close_review(self, note=None):
        """收尾结束（全部处理完 / 用户敲 q）：告诉 daemon 这场翻篇了。"""
        review, self.review = self.review, None
        if review is None:
            return None
        if review.due_mode():
            # 过期检查没有「那一场」可以翻篇：不写 postsession 状态文件。
            # 任务本身已经被改成 completed / dropped（或跳过时保持 active），
            # 所以下次 inspect-due 自然不会再问一遍。
            pass
        else:
            try:
                psv.mark_done(review.session, note=note or "reviewed")
            except OSError:
                pass
        self.rebuild_rows()
        if review.skipped:
            self.say("收尾结束；跳过了 %d 条（它们还是「进行中」）。" % len(review.skipped), "warn")
        return review

    def review_active(self):
        return self.review is not None and self.review.pending()

    # -- 设置 / 警告开关 -------------------------------------------------

    def cmd_settings(self, args=None, show=False):
        """settings —— 只把设置视图的内容说一遍（界面本身在 SettingsView 里）。"""
        rows = self.settings.rows()
        self.say("设置 %d 项：%s" % (len(rows), "、".join(
            "%s=%s" % (r["id"], r["value_display"]) for r in rows)), "info")
        return rows

    def cmd_set(self, args):
        """set <id> <value> —— 改一个设置项；不写 value = 恢复默认值。"""
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：set <设置 id> <value>；现有：%s"
                             % "、".join(r["id"] for r in self.settings.rows()))
        setting_id = toks[0]
        if len(toks) == 1:
            value = self.settings.reset(setting_id)
            self.say("✅ %s 已恢复默认值（%s）" % (setting_id, value or "（空）"), "ok")
        else:
            value = self.settings.set(setting_id, " ".join(toks[1:]))
            self.say("✅ %s = %s" % (setting_id, value or "（未设置）"), "ok")
        # 设置可能立即改变行为（比如 edit-tomorrow 影响草稿/执行区判定）
        if setting_id == "tomorrow-plan" and value:
            self.say("   想让它真的挂到 session 上，再建一条同名重复任务："
                     "task create title=明日时间安排 --repeat %s" % value, "info")
        self.refresh_warnings(force=True)
        return value

    def cmd_warn(self, args):
        """warn on|off —— 状态框里的警告要不要显示（不写 = 取反）。"""
        token = str(args[0]).strip().lower() if args else ""
        if token in ("on", "true", "1"):
            on = self.shell.set_warnings(True)
        elif token in ("off", "false", "0"):
            on = self.shell.set_warnings(False)
        elif not token:
            on = self.shell.set_warnings()
        else:
            raise ValueError("用法：warn on|off（不写就是取反）")
        self.say("状态框警告：%s" % ("显示" if on else "隐藏"), "info")
        return on

    def _drop_contract_row(self, idx, row):
        """草稿区：把 data.csv 里那行删掉。零阻力、不问任何问题 —— 这就是「自由」。

        如果这一行是临时挪动出来的：撤销挪动（恢复 data.csv 原时间），
        **不删** data.csv 的按星期几规则；已订上的目标 session 顺手取消。
        """
        if row.get("relocation"):
            rec = row.get("relocation") or {}
            from_hhmm = rec.get("from") or row.get("relocated_from") or row["startLabel"]
            # 先乐观撤销本地挪动，再把「取消已订上的临时目标」丢后台。
            # 失败时把 relocation 文件和表格行一起放回去 —— 用户不用等浏览器。
            before_relocations = sch.load_relocations()
            before_row = dict(row)
            removed = self._forget_api_row(row.get("id")) if row.get("id") else None
            removed_at = removed[0] if removed else None
            sch.remove_relocation(self.sel_date, from_hhmm)
            self.rebuild_rows()
            self.invalidate()
            if row.get("id") is not None:
                action = self.net_action(
                    "session.cancel",
                    "取消临时目标 %s %s" % (self.sel_date.isoformat(),
                                           row["startLabel"]),
                    {"date": self.sel_date.isoformat(),
                     "start": row.get("startLabel"),
                     "startMs": row.get("startMs"),
                     "meeting_id": row.get("id")})
                def failed(error, _before=before_relocations, _row=before_row,
                           _idx=removed_at):
                    try:
                        sch.save_relocations(_before)
                    except Exception as e:         # noqa: BLE001 - 回滚失败也要说话
                        self.say("⚠ 回滚临时挪动失败：%s" % e, "warn")
                    self._row_op_failed("#%d 取消临时目标 %s"
                                        % (idx, _row.get("startLabel")),
                                        _row, error, index=_idx)
                    self.refresh(quiet=True)
                job = self.jobs.submit(action, on_fail=failed)
                if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
                    return False
                self.say("✅ 已撤销 #%d 的临时挪动：恢复原时间 %s；%s"
                         % (idx, from_hhmm,
                            "临时目标已取消" if self.jobs.sync
                            else "取消临时目标已排进后台（失败会自动放回）"), "ok")
            else:
                self.say("✅ 已撤销 #%d 的临时挪动：恢复 data.csv 原时间 %s。"
                         % (idx, from_hhmm), "ok")
            self.refresh(quiet=True)
            return True

        if not row.get("lineno"):
            raise ValueError("#%d 在 data.csv 里没有对应行（计划外的场次），删不了" % idx)
        if sch.fingerprint() != self.csv_fp:
            raise ValueError("data.csv 刚被别处改过（指纹不一致）。先 refresh 再动手，免得删错行。")
        raw = sch.remove_row(None, row["lineno"], expect=row.get("raw"))
        self.say("✅ 已从 data.csv 删掉第 %d 行：%s" % (row["lineno"], raw.strip()), "ok")
        self.say("   注意：这是**按星期几**的规则 → 以后每个%s都不会再有这一场。"
                 % sch.cn_weekday_of_day(self.sel_date), "warn")
        self.refresh(quiet=True)
        return True

    def cmd_day(self, args):
        """day / day -1 / day 0 / day 1 / day 2026-10-11 —— 切换看哪一天。"""
        if not args:
            self.say("现在看的是 day %+d（%s %s）"
                     % (self.day_offset, self.sel_date.isoformat(),
                        sch.cn_weekday_of_day(self.sel_date)), "info")
            return False
        token = args[0].strip()
        today = datetime.date.today()
        try:
            offset = int(token)
        except ValueError:
            try:
                offset = (datetime.date.fromisoformat(token) - today).days
            except ValueError:
                raise ValueError("day 要么是偏移量（-1 / 0 / 1），要么是 YYYY-MM-DD；"
                                 "你给的是 %r" % token)
        if offset < -7 or offset > 60:
            raise ValueError("只让看 -7 .. +60 天（再远没意义），你给的是 %+d" % offset)
        self.day_offset = offset
        self.say("切到 %s %s（day %+d）" % (self.sel_date.isoformat(),
                                          sch.cn_weekday_of_day(self.sel_date), offset), "cmd")
        if self.offline:
            self._fallback_rows("离线模式")
        else:
            self.refresh()
        return True

    def _column_command_args(self, args):
        """解析 open/close 的列名 + ``--permanent``。

        列名允许带空格（``open 时段 ID`` / ``open "时段 ID"`` 都行），
        所以不能只拿 args[0]。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        permanent = False
        kept = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "永久"):
                permanent = True
            else:
                kept.append(tok)
        return " ".join(kept).strip(), permanent, toks

    def _switch_session_mode(self, mode):
        self.session_ui_mode = mode
        if not isinstance(self.view, ContractView):
            self.go_to_view(ContractView)
        self.say("看板切到 %s：%s"
                 % (mode, "实际 session（过去只留最近一场）"
                    if mode == "working" else "计划 + 实际全部内容"), "cmd")
        self.invalidate()
        return mode

    def _resolve_open_close_column(self, text, task_mode):
        """当前视图优先：任务视图找任务表头，其他视图找契约表头。

        返回 ``(col, mode, headers, table_name)``；找不到返回 ``(None, ...)``。
        这样两个表里都有「状态」时不会串，同时从别的视图敲任务表头也能切过去。
        """
        tasks = self.task_store.list()
        task_headers = task_table_headers(tasks)
        session_headers = [h for h, _kw in SESSION_TABLE_COLUMNS]
        if task_mode:
            col = normalize_task_column(text, tasks)
            if col:
                return col, TASK_UI_MODE, task_headers, "任务表"
            col = normalize_session_column(text)
            if col:
                return col, self.session_ui_mode, session_headers, "契约表"
        else:
            col = normalize_session_column(text)
            if col:
                return col, self.session_ui_mode, session_headers, "契约表"
            col = normalize_task_column(text, tasks)
            if col:
                return col, TASK_UI_MODE, task_headers, "任务表"
        return None, None, task_headers if task_mode else session_headers, (
            "任务表" if task_mode else "契约表")

    def _open_pro_file(self, pro):
        """打开项目 ``pro`` 关联的文件（``open`` + 回车顺便打开）。"""
        raw_file = str((pro or {}).get("file") or "").strip()
        if not raw_file:
            return None
        expanded = os.path.expanduser(raw_file)
        self.opened_files.append(expanded)
        opener = getattr(self, "file_opener", None)
        if callable(opener):
            opener(expanded, pro)
        elif not self.offline and sys.platform == "darwin":
            try:
                subprocess.Popen(["open", expanded],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                self.say("⚠ 打开项目关联文件失败（%s）：%s" % (raw_file, e), "warn")
                return None
        self.say("📂 已顺便打开项目「%s」关联文件：%s"
                 % (pro.get("title") or pro.get("id"), raw_file), "ok")
        return expanded

    def _pro_for_task(self, task):
        """从一条本地任务找到它所属的真正项目 ``pro``（通过 ``project_id`` 或 ``project`` 字段）。"""
        if not isinstance(task, dict):
            return None
        pid, ptitle, _pk = ts.TaskStore.project_task_fields(task)
        if pid is not None:
            pro = self.projects.pro_for_op(pid)
            if pro is not None:
                return pro
        if ptitle:
            for pro in self.projects.active_pros():
                if str(pro.get("title") or "").strip().casefold() == str(ptitle).strip().casefold():
                    return pro
        return None

    def _open_associated_project_files(self):
        """在 ``open`` + 回车时顺便打开当前相关项目（``pro``）关联的文件。"""
        try:
            self.projects.ensure_tagged_pros()
        except Exception:                          # noqa: BLE001
            pass
        candidates = []
        seen_ids = set()

        def add_pro(pro):
            if pro and pro.get("file") and pro.get("id") not in seen_ids:
                seen_ids.add(pro["id"])
                candidates.append(pro)

        # 1) 当前进行中或即将开始 session 上的任务所属项目
        try:
            cur = self.live_session()
            if cur is not None:
                _idx, row = cur
                for t in self.task_store.tasks_for_session(row.get("date"), row.get("startLabel")):
                    add_pro(self._pro_for_task(t))
        except Exception:                          # noqa: BLE001
            pass
        # 2) 当前选中任务所属项目
        if not candidates and self.selected_task_id is not None:
            try:
                sel_task = self.task_store.get(self.selected_task_id)
                add_pro(self._pro_for_task(sel_task))
            except Exception:                      # noqa: BLE001
                pass
        # 3) 最近选中的 pro
        if not candidates and self.selected_pro_id is not None:
            add_pro(self.projects.pro_by_id(self.selected_pro_id))
        # 4) 所有配置了关联文件的 active pro
        if not candidates:
            for pro in self.projects.active_pros():
                add_pro(pro)
        opened = []
        for pro in candidates:
            got = self._open_pro_file(pro)
            if got:
                opened.append(got)
        return opened

    def cmd_open(self, args=None):
        """``open working|detail`` 切口径；``open <表头> [--permanent]`` 展开列；
        直接敲 ``open`` + 回车时顺便打开项目（``pro``）关联的文件。

        working 默认自动折叠「时段类型 / 时段名称 / 契约」；手动 open 只是
        本次运行的覆盖，加 ``--permanent`` 才会写进偏好、以后也不再自动折叠。
        在任务视图里，``open/close`` 作用于任务表的表头。
        """
        text, permanent, toks = self._column_command_args(args)
        if not text:
            if permanent:
                raise ValueError("open --permanent 后面要跟一个表头名")
            mode = self._switch_session_mode(
                "detail" if self.session_ui_mode == "working" else "working")
            self._open_associated_project_files()
            return mode
        if toks and toks[0].lower() in ("pro", "project", "项目") and len(toks) >= 2:
            pro = self._ensure_pro_for_token(" ".join(toks[1:]))
            self.selected_pro_id = pro["id"]
            if not pro.get("file"):
                pnum = self.projects.pro_number_of(pro["id"]) or pro["id"]
                raise ValueError("项目 #%s「%s」还没有关联文件；先用 `pro %s file <路径>` 绑定。"
                                 % (pnum, pro.get("title"), pnum))
            return self._open_pro_file(pro)
        aliases = {"w": "working", "work": "working", "d": "detail", "all": "detail"}
        mode = aliases.get(text.lower(), text.lower())
        if mode in SESSION_UI_MODES:
            if permanent:
                raise ValueError("open --permanent 只用于表头列；切口径不带 --permanent")
            return self._switch_session_mode(mode)
        task_mode = isinstance(self.view, TaskView)
        if task_mode and re.match(r"^(?:page\s*|p)?(\d+)$", text.lower()):
            if permanent:
                raise ValueError("open <页码> 翻页不带 --permanent")
            page_no = int(re.match(r"^(?:page\s*|p)?(\d+)$", text.lower()).group(1))
            return self.cmd_task_page([str(page_no)])
        col, target_mode, headers, table_name = self._resolve_open_close_column(
            text, task_mode=task_mode)
        if not col:
            # 退回看看是不是要直接打开某个关联了文件的项目 pro
            try:
                pro = self.projects.resolve_pro(text)
            except ValueError:
                pro = None
            if pro is not None and pro.get("file"):
                self.selected_pro_id = pro["id"]
                return self._open_pro_file(pro)
            raise ValueError(
                "open 只认 working / detail、表头名（当前表：%s）或已关联文件的项目 pro；你给的是 %r"
                % ("、".join(headers), text))
        self._set_column_open(col, permanent=permanent, mode=target_mode)
        if target_mode == TASK_UI_MODE:
            if not isinstance(self.view, TaskView):
                self.go_to_view(TaskView)
            detail = "（任务表）"
        else:
            if not isinstance(self.view, ContractView):
                self.go_to_view(ContractView)
            detail = "（%s 口径）" % self.session_ui_mode
        self.say("已打开列「%s」%s%s"
                 % (col, "并写入偏好" if permanent else "（本次运行）", detail), "cmd")
        self.invalidate()
        return col

    def cmd_close(self, args=None):
        """``close <表头> [--permanent]``：折叠一列；永久偏好写 warden-ui.json。

        在任务视图里折叠任务表列；其他视图折叠契约表列。
        """
        text, permanent, _toks = self._column_command_args(args)
        if not text:
            raise ValueError("用法：close <表头名> [--permanent]；当前表头名：%s"
                             % "、".join(task_table_headers(self.task_store.list())
                                         if isinstance(self.view, TaskView)
                                         else [h for h, _kw in SESSION_TABLE_COLUMNS]))
        task_mode = isinstance(self.view, TaskView)
        col, target_mode, headers, table_name = self._resolve_open_close_column(
            text, task_mode=task_mode)
        if not col:
            raise ValueError(
                "close 只认表头名（当前表：%s）；你给的是 %r"
                % ("、".join(headers), text))
        # 至少留一列，否则 rich 会画一张没有列的表。
        if target_mode == TASK_UI_MODE:
            hidden = set(self.effective_task_hidden_columns(self.task_store.list()))
        else:
            hidden = set(self.effective_hidden_columns())
        hidden.add(col)
        if len(hidden) >= len(headers):
            raise ValueError("不能把所有列都折叠；至少留一列看。")
        self._set_column_closed(col, permanent=permanent, mode=target_mode)
        if target_mode == TASK_UI_MODE:
            if not isinstance(self.view, TaskView):
                self.go_to_view(TaskView)
            detail = "（任务表）"
        else:
            if not isinstance(self.view, ContractView):
                self.go_to_view(ContractView)
            detail = "（%s 口径）" % self.session_ui_mode
        self.say("已折叠列「%s」%s%s"
                 % (col, "并写入偏好" if permanent else "（本次运行）", detail), "cmd")
        self.invalidate()
        return col

    def cmd_task_page(self, args=None):
        """``open <页码>`` / ``task page <页码>``：直接翻到任务表第 N 页。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks or not re.match(r"^\d+$", toks[0]):
            raise ValueError("用法：open <页码>（例如 open 1 / open 2）")
        want = int(toks[0])
        if want < 1:
            raise ValueError("页码从 1 开始；你给的是 %d" % want)
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        filtered = self.filter_and_sort_tasks(self.task_store.list())
        limit = self.view._row_budget() if isinstance(self.view, TaskView) else 10
        total_pages = max(1, (len(filtered) + limit - 1) // limit)
        if want > total_pages:
            raise ValueError("当前只有 %d 页（共 %d 条任务），没有第 %d 页"
                             % (total_pages, len(filtered), want))
        self.task_view_page = want
        self.say("已翻到第 %d/%d 页（共 %d 条）" % (want, total_pages, len(filtered)), "cmd")
        self.invalidate()
        return want

    def cmd_task_sort(self, args=None):
        """``sort <字段> [asc|desc]`` / ``sort off``：任务表排序。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_sort_key:
                self.say("当前排序：%s %s（`sort off` 取消）"
                         % (self.task_sort_key, "降序" if self.task_sort_desc else "升序"),
                         "info")
            else:
                self.say("当前未排序（默认按内部 id 顺序；用法：sort <字段> [asc|desc]）", "info")
            return self.task_sort_key
        first = toks[0].lower()
        if first in ("off", "none", "clear", "reset", "取消", "默认"):
            self.task_sort_key = None
            self.task_sort_desc = False
            self.task_view_page = 1
            self.say("已恢复默认排序", "cmd")
            self.invalidate()
            return None
        desc = False
        key_tok = toks[0]
        if key_tok.startswith("-"):
            desc = True
            key_tok = key_tok[1:]
        elif key_tok.startswith("+"):
            key_tok = key_tok[1:]
        if len(toks) > 1:
            dir_tok = toks[1].lower()
            if dir_tok in ("desc", "down", "reverse", "d", "降序", "倒序"):
                desc = True
            elif dir_tok in ("asc", "up", "a", "升序", "正序"):
                desc = False
            else:
                raise ValueError("排序方向只认 asc / desc；你给的是 %r" % toks[1])
        tasks = self.task_store.list()
        col = normalize_task_column(key_tok, tasks)
        if not col:
            headers = task_table_headers(tasks)
            raise ValueError("sort 只认任务字段或表头名（%s）；你给的是 %r"
                             % ("、".join(headers), key_tok))
        self.task_sort_key = col
        self.task_sort_desc = desc
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        self.say("任务表按「%s」%s排列（`sort off` 恢复默认）"
                 % (col, "降序" if desc else "升序"), "cmd")
        self.invalidate()
        return col

    def cmd_task_group(self, args=None):
        """``group <字段>`` / ``group off``：任务表按字段前置分组显示。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_group_by:
                self.say("当前分组：grouping(%s)（`group off` 取消）" % self.task_group_by, "info")
            else:
                self.say("当前未分组（用法：group <字段>，如 `group project`）", "info")
            return self.task_group_by
        raw = " ".join(toks).strip()
        m = re.match(r"^grouping\((.+)\)$", raw, flags=re.I)
        if m:
            raw = m.group(1).strip()
        if raw.lower() in ("off", "none", "clear", "reset", "取消"):
            self.task_group_by = None
            self.task_view_page = 1
            self.say("已取消任务表分组", "cmd")
            self.invalidate()
            return None
        tasks = self.task_store.list()
        col = normalize_task_column(raw, tasks)
        if not col:
            headers = task_table_headers(tasks)
            raise ValueError("group 只认任务字段或表头名（%s）；你给的是 %r"
                             % ("、".join(headers), raw))
        self.task_group_by = col
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        self.say("任务表已按「grouping(%s)」分组显示（同组首行显示，后续留空；`group off` 取消）"
                 % col, "cmd")
        self.invalidate()
        return col

    def cmd_task_search(self, args=None):
        """``search <关键词> [--group <字段>]`` / ``search off``：搜索并可选分组。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            if self.task_search_query or self.task_group_by:
                self.say("当前搜索：%r · 分组：%s（`search off` 清空）"
                         % (self.task_search_query or "（无）",
                            ("grouping(%s)" % self.task_group_by) if self.task_group_by else "（无）"),
                         "info")
            else:
                self.say("用法：search <关键词> [--group <字段>] | search grouping(project) [关键词] | search off",
                         "info")
            return self.task_search_query
        if len(toks) == 1 and toks[0].lower() in ("off", "none", "clear", "reset", "取消"):
            self.task_search_query = ""
            self.task_group_by = None
            self.task_view_page = 1
            self.say("已清除任务搜索与分组过滤", "cmd")
            self.invalidate()
            return ""
        group_arg = None
        query_parts = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            low = tok.lower()
            m_grp = re.match(r"^(?:grouping|group)\((.+)\)$", tok, flags=re.I)
            if m_grp:
                group_arg = m_grp.group(1).strip()
                i += 1
                continue
            if low in ("--group", "-g", "by", "--by"):
                if i + 1 >= len(toks):
                    raise ValueError("%s 后面要跟分组字段名（例如 --group project）" % tok)
                group_arg = toks[i + 1]
                i += 2
                continue
            if low.startswith("--group=") or low.startswith("-g="):
                group_arg = tok.split("=", 1)[1].strip()
                i += 1
                continue
            query_parts.append(tok)
            i += 1
        tasks = self.task_store.list()
        # 如果只敲了 `search project` 这类恰好是字段名的单token，而且没带别的词，
        # 既当搜索也可当按该字段分组？按用户说的：「比如说搜索的时候，在最前面引入一个 field，
        # 然后一个这个 field 对应很多 task，比如说 grouping(project)」。
        # 如果第一个词是已知可分组字段名（如 project）且用户写了 `search grouping(project)`
        # 或 `search -g project` 已经识别；若用户写 `search by project` 也能识别。
        if group_arg is not None:
            if group_arg.lower() in ("off", "none", "clear"):
                self.task_group_by = None
            else:
                col = normalize_task_column(group_arg, tasks)
                if not col:
                    raise ValueError("未知分组字段 %r" % group_arg)
                self.task_group_by = col
        self.task_search_query = " ".join(query_parts).strip()
        self.task_view_page = 1
        if not isinstance(self.view, TaskView):
            self.go_to_view(TaskView)
        matched = self.filter_and_sort_tasks(tasks)
        msg_bits = []
        if self.task_search_query:
            msg_bits.append("搜索「%s」" % self.task_search_query)
        if self.task_group_by:
            msg_bits.append("分组 grouping(%s)" % self.task_group_by)
        self.say("%s：命中 %d/%d 条任务（`search off` 清除）"
                 % (" · ".join(msg_bits) or "全部任务", len(matched), len(tasks)), "cmd")
        self.invalidate()
        return matched

    def _period_row_is_stale(self, row, new_by_start):
        """这一行 API session 还符合新排布吗？（按起点 + 时长判断）"""
        start = row.get("startLabel")
        if start not in new_by_start:
            return True
        dur = row.get("durationMin")
        try:
            return dur is not None and int(dur) != int(new_by_start[start])
        except (TypeError, ValueError):
            return True

    def _acquire_round_lock(self, timeout=30.0, poll=0.5, on_wait=None):
        """拿 daemon 那一轮的同一把文件锁（`keep_session.round_lock`）。

        为什么：改排布是「取消旧场 → 清 state → 写新排布」三步。daemon 要是刚好
        在中间跑一轮，就会看到「state 说 booked、API 里没了」，把 **warden 自己
        干的活** 记成「你手动取消」的罪，然后 TUI 锁死 —— 用户就是这么被咬的。
        等不到（daemon 那一轮在下单，可能几十秒）就返回 None，调用方用挂号单兜底。
        """
        lock = ks.round_lock()
        if lock.__enter__():
            return lock
        if on_wait:
            on_wait()
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            time.sleep(poll)
            lock = ks.round_lock()
            if lock.__enter__():
                return lock
        return None

    def _period_cancel_keys(self, period, new_sessions):
        """这一时段里「改完排布之后就不该再存在」的 state key（给 daemon 挂号用）。"""
        kept = set()
        for sess in new_sessions or []:
            try:
                kept.add((str(sess.get("start")), int(sess.get("duration"))))
            except (TypeError, ValueError):
                continue
        date = period.get("date") or self.sel_date.isoformat()
        keys = set()
        # 1) 表格里有 id、但新排布里没有同起点同时长的场次
        for row in self.rows or []:
            if not row.get("id") or row.get("date") != date:
                continue
            start = row.get("startLabel")
            if sp.period_for_session([period], start) is None:
                continue
            try:
                dur = int(row.get("durationMin"))
            except (TypeError, ValueError):
                dur = None
            if (str(start), dur) not in kept:
                # 已经开始/过去的场次不进入挂号单：它们根本不会被自动取消，
                # 不能给 daemon 留一张可能误掩护后续手动取消的 `expect_cancel`。
                if self._session_already_started(row, now=self._now_dt()):
                    continue
                try:
                    keys.add(sch.key_of(datetime.date.fromisoformat(date), start))
                except ValueError:
                    pass
        # 2) state 里记着（哪怕表格没显示出来）的槽位，新排布里也不该再有
        try:
            slots = (ks.load_state().get("slots") or {})
        except Exception:                              # noqa: BLE001
            slots = {}
        for key, rec in slots.items():
            if not str(key).startswith(date + "T"):
                continue
            start = str(key).split("T", 1)[1]
            if sp.period_for_session([period], start) is None:
                continue
            dur = (rec or {}).get("duration")
            try:
                dur = int(dur)
            except (TypeError, ValueError):
                dur = None
            if (start, dur) not in kept:
                if self._session_already_started({"date": date, "start": start},
                                                 now=self._now_dt()):
                    continue
                keys.add(str(key))
        return keys

    def _task_bindings_dropped(self, period, new_sessions):
        """排布变了之后，哪些任务绑定会被清掉（纯计算，不改任何东西）。

        判据：**(起点, 时长) 完全一致才算「这场没动」**。
        50 25 25 → 50 50 时第一场还是 11:00(50)，它挂的任务留着；
        50 25 → 25 50 时没有一场对得上，两场的绑定全清。
        """
        return [task for task, _key in self._task_bindings_split(period, new_sessions)[1]]

    def _task_bindings_split(self, period, new_sessions):
        """把这一时段里挂着任务分两堆：(留着的, 要清掉的)，元素是 (task, key)。"""
        kept_keys = set()
        for sess in new_sessions or []:
            try:
                kept_keys.add((str(sess.get("start")), int(sess.get("duration"))))
            except (TypeError, ValueError):
                continue
        date = period.get("date") or self.sel_date.isoformat()
        keep, drop = [], []
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if sess.get("date") != date:
                continue
            if sp.period_for_session([period], sess.get("start")) is None:
                continue
            try:
                key = (str(sess.get("start")), int(sess.get("duration")))
            except (TypeError, ValueError):
                key = (str(sess.get("start")), None)
            (keep if key in kept_keys else drop).append((task, key))
        return keep, drop

    def clear_task_bindings_for_layout(self, period, new_sessions):
        """排布改了：留下来的场次保留任务绑定，动过的场次清掉绑定。

        任务本身**不删**（还是任务库里的），只是不再挂在那一场上；
        Focusmate 侧那条 task 记进 ``focusmate.previous``，
        以后它要是被勾了完成，`task sync` 照样能按 completedAt 归档。
        """
        _keep, drop = self._task_bindings_split(period, new_sessions)
        if not drop:
            return []
        cleared = []
        for task, key in drop:
            try:
                self.task_store.unassign(task.get("id"), reason="layout-changed")
            except ValueError:
                continue
            cleared.append(task)
            self.say("清掉任务 %s「%s」的绑定（原来 %s %s，这一场排布改了）"
                     % (self.task_label(task),
                        self.task_store.summarize(task, max_len=24),
                        (task.get("session") or {}).get("date") or period.get("date"),
                        key[0]), "warn")
        return cleared

    def _forget_layout_state(self, rows):
        """清掉被 warden 主动取消的旧 state 记录，避免 daemon 误记 sin。"""
        if not rows:
            return
        try:
            state = ks.load_state()
            slots = state.setdefault("slots", {})
            removed = 0
            for row in rows:
                try:
                    day = datetime.date.fromisoformat(row.get("date"))
                except (TypeError, ValueError):
                    day = self.sel_date
                key = sch.key_of(day, row.get("startLabel"))
                if key in slots:
                    slots.pop(key, None)
                    removed += 1
            if removed:
                ks.save_state(state)
                self.say("已清掉 %d 条旧 booked state（不会误记 sin）。" % removed, "info")
        except Exception as e:                        # noqa: BLE001
            self.say("清理旧 state 失败（daemon 可能多记一笔 sin）：%s" % e, "warn")

    def _cancel_stale_period_sessions(self, period, new_sessions, dropped_tasks=()):
        """改 layout 前，把旧排布里已经订上的 API session 对齐掉。

        为什么必须做：data.csv 换成 22:15(25)+22:45(25) 之后，API 里原先那场
        22:15(50) 不会自己消失；daemon 只补订、不擅自杀旧场，于是 50min/25min
        会同时存在。这里在改契约之前先把「起点或时长不再匹配」的场次取消。

        **改排布不要宣言**（用户明确要求）：排布是「计划」，点 UI 就能调；
        有阻力的是**取消单场**（`cancel N` 那条路仍然要敲宣言，一点没放松）。
        为了不让人稀里糊涂丢东西，取消前照样把面板列出来，并把会被清掉的任务
        绑定一起写清楚。
        """
        if self.offline or not self.authoritative:
            return True
        new_by_start = {s["start"]: int(s["duration"]) for s in new_sessions}
        stale = []
        for row in self.rows:
            if not row.get("id"):
                continue
            if row.get("date") != period.get("date"):
                continue
            if sp.period_for_session([period], row.get("startLabel")) is None:
                continue
            if self._period_row_is_stale(row, new_by_start):
                stale.append(row)
        stale, _protected = self._split_started_sessions(stale)
        if not stale:
            return True

        past = [r for r in stale if r.get("zone") == "past"]
        live = [r for r in stale if r.get("zone") != "past"]
        if past:
            self.say("有 %d 场旧 session 已经过去；过去的场次不取消、不补订。"
                     % len(past), "warn")
        if not live:
            return True

        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True)
        info.add_column()
        info.add_row("时段", "%s  %s–%s  %s"
                     % (period.get("id"), period.get("start"), period.get("end"),
                        period.get("name") or period.get("type") or ""))
        for row in live:
            info.add_row("要取消", "#%s  %s–%s（%smin）"
                         % (self.rows.index(row) + 1, row.get("startLabel"),
                            row.get("endLabel") or "?", row.get("durationMin") or "?"))
        info.add_row("新排布", "  ".join("%s–%s(%d)"
                                       % (s["start"], s["end"], s["duration"])
                                       for s in new_sessions) or "（空）")
        for task in dropped_tasks:
            sess = task.get("session") or {}
            info.add_row("清任务绑定", "#%s %s @%s"
                         % (task.get("id"),
                            self.task_store.summarize(task, max_len=24),
                            sess.get("start") or "?"))
        self.emit(Panel(info, title="[bold yellow]⚠ 改排布：旧 session 会被取消[/]",
                        border_style="yellow", box=box.HEAVY))

        cancelled = []
        try:
            for row in live:
                self.say("取消旧 session %s–%s（%smin）…"
                         % (row.get("startLabel"), row.get("endLabel") or "?",
                            row.get("durationMin") or "?"), "cmd")
                # 乐观 + 后台：旧行立刻从表格消失，取消请求交给单 worker。
                # 这里以前直接 bridge.call("cancel")，改排布会在提示符线程上
                # 等浏览器几十秒 —— 和「外部操作全异步」的约定不符。
                before_row = dict(row)
                before_index, _ = self._forget_api_row(row.get("id")) or (None, None)
                action = self.net_action(
                    "session.cancel",
                    "取消旧 session %s %s" % (row.get("date"),
                                              row.get("startLabel") or "?"),
                    {"date": row.get("date"), "start": row.get("startLabel"),
                     "startMs": row.get("startMs"), "meeting_id": row.get("id")})
                job = self.jobs.submit(
                    action,
                    on_fail=lambda error, _row=before_row, _idx=before_index:
                    self._row_op_failed("旧 session %s 取消"
                                        % _row.get("startLabel"),
                                        _row, error, index=_idx))
                if self.jobs.sync and (job.outcome is None or not job.outcome.ok):
                    self.had_error = True
                    continue
                cancelled.append(row)
                self.say("✅ %s旧 session %s"
                         % ("已取消" if self.jobs.sync else "已排进后台取消",
                            row.get("startLabel")), "ok")
            if cancelled and not self.jobs.sync:
                self.rebuild_rows()
        finally:
            # 关键：旧 session 是 warden 主动取消的，不是用户手动搞丢的。
            # 清掉 state 里的旧 booked 记录，否则 daemon 下一轮会记一笔 sin 并锁死 TUI。
            # 同一个起点如果新排布还要用，它会以新 duration 重新进入待订队列。
            self._forget_layout_state(cancelled)
        return True

    # -- 静音时段（period mute） --------------------------------------

    def mute_targets(self, rows=None):
        """「该静音、但 Focusmate 那边 quiet 还不是 ON」的行。

        判据只有一个：这行的起点落在 ``period mute`` 写下的时间窗里
        （``row["period_muted"]``，见 :func:`mute_window_for`）。
        没有 id 的行动不了（快照 / 还没订上），跳过。
        """
        out = []
        for row in (self.rows if rows is None else rows):
            if not row.get("period_muted"):
                continue
            if row.get("quiet"):
                continue
            if row.get("id") is None:
                continue
            out.append(row)
        return out

    def apply_mutes(self, rows=None, quiet_spinner=True):
        """把「时段静音」落到 Focusmate（quiet on）。返回 ``[(标签, ok, 说明)]``。

        幂等：已经是 ON 的行不在 :meth:`mute_targets` 里，所以重复调用只是查一次。
        这是**意图 → 结果**的那一步：`period mute` 只写本地那张表，真正让这一场
        安静下来的是这里（Focusmate 的 quiet 是单场开关）。
        """
        if self.offline:
            return []
        targets = self.mute_targets() if rows is None else list(rows)
        out = []
        for row in targets:
            label = "%s–%s" % (row.get("startLabel"), row.get("endLabel") or "?")
            status = self.console.status("[yellow]把 %s 设成静音…[/]" % label, spinner="dots")
            try:
                if quiet_spinner:
                    status.__enter__()
                try:
                    res = self.bridge.call("mute", startMs=row["startMs"],
                                           meetingId=row["id"], quiet=True)
                finally:
                    if quiet_spinner:
                        status.__exit__(None, None, None)
            except (BridgeError, KeyError) as e:
                self.had_error = True
                out.append((label, False, str(e)))
                self.say("❌ %s 静音失败：%s" % (label, e), "error")
                continue
            ok = bool(res.get("verified"))
            if ok:
                out.append((label, True, ""))
                self.say("🔇 %s 已静音（时段要求，Focusmate quiet = ON）" % label, "ok")
            else:
                self.had_error = True
                out.append((label, False, "复核不一致：%s" % (res.get("quiet"),)))
                self.say("❌ %s 静音后复核不一致：quiet = %s"
                         % (label, res.get("quiet")), "error")
        if out:
            self.invalidate()
        return out

    def _maybe_apply_mutes(self):
        """每帧看一眼：有「时段要求静音但还没设上」的场次 → 后台补上。

        节流两层：整体 5 秒一次（`mutes_at`），同一场 5 分钟内只试一次
        （`mute_tries`，避免 bridge 连着失败时每帧都去开浏览器）。
        """
        if self.offline or self.review is not None:
            return
        now = time.time()
        if now - self.mutes_at < 5.0:
            return
        self.mutes_at = now
        cooldown = 300.0
        targets = []
        for row in self.mute_targets():
            key = (row.get("date"), row.get("startLabel"))
            if now - self.mute_tries.get(key, 0.0) < cooldown:
                continue
            self.mute_tries[key] = now
            targets.append(row)
        if not targets:
            return
        if self.mute_worker is not None and self.mute_worker.is_alive():
            return
        self.mute_worker = threading.Thread(target=self._mute_worker, args=(targets,),
                                           daemon=True)
        self.mute_worker.start()

    def _mute_worker(self, targets):
        try:
            self.apply_mutes(rows=targets, quiet_spinner=False)
        except Exception as e:                   # noqa: BLE001
            self.say("自动静音出错（下次再试）：%s: %s" % (type(e).__name__, e), "warn")

    def cmd_period_mute(self, args):
        """``period mute T1 [--permanent]`` / ``period unmute T1 [--permanent]``。

        需求原话：「我希望可以设置静音时段，比如说 ``period mute t1 --permanent``
        就是设置之后这里都静音。在设置完成之后，这个时段内部的 session 自动切换为
        静音模式」。

        **「静音」= Focusmate session 的 quiet mode，仅此而已**（用户明确过：
        daemon 在静音时段照旧发通知、拉音量、放嘶吼）。所以这条命令只写**意图**
        （``temp/session-mutes.json``：这个时段按星期几重复，或者只今天），
        然后把它落到 Focusmate 那一场上：

        1. 命令本身立刻设一遍（今天的、还在这个窗口里的场次）；
        2. 之后每帧后台补（:meth:`_maybe_apply_mutes`）—— 新订上的场次自己安静下来，
           已经是 ON 的不会反复动。

        本地行为一概不变：分心看门狗照旧通知/拉音量/放嘶吼（``keep_session`` 里
        根本没有读静音表的地方）。

        ``--permanent`` 写星期几（以后每个这个星期几都静音）；不写就只静音这一天。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        want_on = True
        permanent = False
        period_id = None
        if toks and toks[0].lower() in ("unmute", "解除静音", "取消静音", "off", "关"):
            want_on = False
            toks = toks[1:]
        if toks and toks[0].lower() in ("mute", "静音", "on", "开"):
            toks = toks[1:]
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "--perm", "permanent"):
                permanent = True
            elif low in ("off", "unmute", "关", "解除静音", "取消静音"):
                want_on = False
            elif low in ("on", "mute", "开", "静音"):
                want_on = True
            elif re.match(r"^[Tt]\d+$", tok):
                period_id = tok.upper()
            else:
                raise ValueError("认不出参数 %r。用法：period mute T1 [--permanent] / "
                                 "period unmute T1 [--permanent]" % (tok,))
        day = self.sel_date
        periods = self.periods_for_day(day)
        if period_id is None:
            ids = "、".join(p.get("id", "?") for p in periods) or "无"
            raise ValueError("要哪个时段？%s 的时段：%s（例：period mute T1 --permanent）"
                             % (day.isoformat(), ids))
        period = next((p for p in periods if p.get("id") == period_id), None)
        if period is None:
            ids = "、".join(p.get("id", "?") for p in periods) or "无"
            raise ValueError("没有时段 %s（%s 的时段：%s）"
                             % (period_id, day.isoformat(), ids))

        scope = (("每个%s的 " % sch.cn_weekday_of_day(day)) if permanent
                 else ("%s 的 " % day.isoformat()))
        if not want_on:
            gone = sp.unmute_period(day, period_id, permanent=permanent)
            if not gone:
                self.say("ℹ️ %s%s 本来就没设静音（%s）"
                         % (scope, period_id, "按星期几" if permanent else "按日期"), "info")
            else:
                self.say("🔊 已解除静音：%s%s（%s–%s）"
                         % (scope, period_id, gone[0].get("start"), gone[0].get("end")), "ok")
            forget_mute_cache()
            self.rebuild_rows()
            return True

        rec = sp.mute_period(day, period, permanent=permanent)
        forget_mute_cache()
        self.say("🔇 %s%s 静音了：%s–%s（%s）"
                 % (scope, period_id, rec["start"], rec["end"],
                    "写进按星期几重复的表" if permanent else "只这一天"), "ok")
        self.say("   这段钟点里的 session 会自动打开 Focusmate quiet（那才是「静音」"
                 "的本体）；本地看门狗/通知照旧 —— 静音只作用于 Focusmate 那一场。"
                 "撤销：period unmute %s%s"
                 % (period_id, " --permanent" if permanent else ""), "info")
        self.rebuild_rows()
        applied = self.apply_mutes()
        if applied:
            self.refresh(quiet=True)
        return True

    def cmd_mute_universal(self, args):
        """``mute-universal 13:00 - 15:00 Mon`` —— 每周固定时间窗静音。

        与 ``period mute`` 的区别：不绑定日历时段 T1/T2，也不要求 session 的
        **起点**落在窗内。用户要求「只要涉及了就算」：14:30–15:30 与
        13:00–15:00 有重叠，同样静音。可以一次给多个星期几（Mon,Wed / 周一 周三）。
        """
        raw = " ".join(str(a).strip() for a in (args or []) if str(a).strip())
        low = raw.lower()
        if not raw or low in ("list", "ls", "show", "列表", "查看"):
            recs = sp.universal_mutes()
            if not recs:
                self.say("ℹ️ 还没有通用静音窗。例：mute-universal 13:00 - 15:00 Mon",
                         "info")
                return []
            for rec in recs:
                wd = cfg.parse_weekday(rec.get("weekday"))
                label = ("每个" + cfg.WEEKDAY_CN[wd]) if wd is not None else "?"
                self.say("🔇 %s %s–%s（与窗口有重叠的 session 都静音）"
                         % (label, rec.get("start"), rec.get("end")), "info")
            return recs

        want_off = any(tok in low for tok in
                       ("off", "unmute", "remove", "delete", "取消", "清空", "解除"))
        # 先找时间窗；星期几可以在前面或后面。也接受 13:00~15:00 / 13:00–15:00。
        match = re.search(r"(?<!\d)(\d{1,2}:\d{2})\s*[-–—~至]\s*(\d{1,2}:\d{2})(?!\d)",
                          raw)
        if not match:
            if want_off:
                gone = sp.unmute_universal()
                forget_mute_cache()
                self.rebuild_rows()
                self.say("🔊 清空了 %d 条通用静音窗。" % len(gone), "ok")
                return gone
            raise ValueError("用法：mute-universal 13:00 - 15:00 Mon"
                             "（撤销：mute-universal off 13:00 - 15:00 Mon；"
                             "看列表：mute-universal list）")
        start, end = match.group(1), match.group(2)
        if sp.parse_hhmm(start) is None or sp.parse_hhmm(end) is None:
            raise ValueError("时间要写成 HH:MM（00:00–23:59）；你给的是 %s–%s"
                             % (start, end))
        rest = (raw[:match.start()] + " " + raw[match.end():])
        tokens = [tok.strip(" ,，/、") for tok in rest.split()]
        weekdays = []
        for tok in tokens:
            part = tok.strip()
            if not part:
                continue
            if part.lower() in ("off", "unmute", "remove", "delete", "取消",
                                "清空", "解除", "mute", "on", "静音"):
                continue
            # Mon-Fri / 周一-周五 这种区间也顺手支持。
            bits = [b for b in re.split(r"[-–—~至]", part) if b.strip()]
            if len(bits) == 2:
                lo, hi = cfg.parse_weekday(bits[0]), cfg.parse_weekday(bits[1])
                if lo is None or hi is None:
                    raise ValueError("认不出星期几区间：%r" % part)
                if lo <= hi:
                    weekdays.extend(cfg.WEEKDAYS[lo:hi + 1])
                else:
                    weekdays.extend(cfg.WEEKDAYS[lo:] + cfg.WEEKDAYS[:hi + 1])
                continue
            wd = cfg.parse_weekday(part)
            if wd is None:
                raise ValueError("认不出星期几：%r（可写 Mon / 周一 / Mon,Wed / Mon-Fri）"
                                 % part)
            weekdays.append(cfg.WEEKDAYS[wd])
        # 去重，保留用户给的顺序。
        weekdays = list(dict.fromkeys(weekdays))
        if not weekdays:
            raise ValueError("要指定星期几，例如 mute-universal 13:00 - 15:00 Mon")
        cn = "、".join(cfg.WEEKDAY_CN[cfg.parse_weekday(wd)] for wd in weekdays)

        if want_off:
            gone = sp.unmute_universal(start, end, weekdays)
            forget_mute_cache()
            self.rebuild_rows()
            if gone:
                self.say("🔊 已撤销 %s %s–%s 的通用静音。" % (cn, start, end), "ok")
            else:
                self.say("ℹ️ %s %s–%s 本来就没有通用静音。" % (cn, start, end), "info")
            return gone

        made = sp.mute_universal(start, end, weekdays)
        forget_mute_cache()
        self.rebuild_rows()
        self.say("🔇 已设通用静音：每个%s %s–%s —— 所有与这个窗口**有重叠**的 "
                 "session 都会打开 Focusmate quiet。" % (cn, start, end), "ok")
        self.say("   看全部：mute-universal list；撤销：mute-universal off %s - %s %s"
                 % (start, end, weekdays[0]), "info")
        applied = self.apply_mutes()
        if applied:
            self.refresh(quiet=True)
        return made

    def cmd_period_layout(self, args):
        """period T1 50 50 / session T1 50 50 —— 改一个时段的 session 排布。

        需求里的规则原样实现：
          - 时长只认 25/50/75（Focusmate 的限制）；
          - 第一场从时段起点开始，之后每场从上一场结束后的下一个 15 分钟整点开始；
          - 任何一场超出时段终点 → 直接报错，**layout 和 data.csv 都不动**。

        改排布时若 API 里已有旧 session（例如原来 22:15 50，现在要 22:15 25 +
        22:45 25），先取消不再匹配的旧场，再写新契约。

        **改排布不需要宣言**（用户明确要求）：计划怎么摆是你的事；
        真正有阻力的是取消单场（`cancel N`）。
        任务绑定按「(起点, 时长) 有没有动」处理：没动的场次绑定留着
        （50 25 25 → 50 50，第一场还是 11:00(50)，任务照旧），
        动过的场次绑定清掉（50 25 → 25 50，两场都变 → 两个任务的绑定都清）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：period T1 50 50（时段 ID 看表格第一列）"
                             " / period mute T1 [--permanent]")
        # `period mute T1 --permanent` 走静音那条路（同一个命令名词，两个动作）
        if toks[0].lower() in ("mute", "unmute", "静音", "解除静音", "取消静音"):
            return self.cmd_period_mute(toks)
        period_id = toks[0]
        if not re.match(r"^[Tt]\d+$", period_id):
            raise ValueError("时段 ID 要像 T1 / T2；你给的是 %r" % period_id)
        period_id = period_id.upper()
        durations = toks[1:]

        # 先刷新一次，拿到当前 API 里的真实 session（id / duration）。
        # 拉不到就别改 —— 否则没法判断旧场次要不要取消，会留下 50/25 并存。
        if not self.offline and time.time() - self.rows_at > 20:
            if not self.refresh(quiet=True):
                raise ValueError("拉不到当前 API session；先 refresh 成功再改排布，"
                                 "避免旧场次和新场次并存。")

        periods = self.periods_for_day(self.sel_date)
        period = next((p for p in periods if p.get("id") == period_id), None)
        if period is None:
            raise ValueError("没有时段 %s（%s 的时段：%s）"
                             % (period_id, self.sel_date.isoformat(),
                                "、".join(p.get("id", "?") for p in periods) or "无"))
        # 纯计算先验证；这里失败 = 零副作用。
        effective = durations if durations else sp.default_durations(period)
        new_sessions = sp.plan_period(period, effective)

        # 排布一变，动过的场次上的任务绑定就该清掉；先算出来，好写进取消前的面板。
        dropped = self._task_bindings_dropped(period, new_sessions)

        # 重新排布 = 你又想要这几场：先撤掉「永不重订」的判决，否则认过罪的场次
        # 永远补不回来（daemon 在 due 之前就把 abandoned 的 key 滤掉了）。
        revived = ks.clear_abandoned(
            [sch.key_of(self.sel_date, s["start"]) for s in new_sessions])
        if revived:
            self.say("撤销 %d 条「永不重订」判决：这几场重新进 daemon 的下单队列。"
                     % revived, "ok")

        # 取消旧场 → 清 state → 写新排布 这三步必须和 daemon 的一轮互斥（同一把锁），
        # 否则它会在缝隙里把 warden 自己干的活记成「你手动取消」的罪。
        lock = self._acquire_round_lock(
            on_wait=lambda: self.say("daemon 正在跑一轮，等它跑完再改排布（最多等 30 秒）…",
                                     "cmd"))
        if lock is None:
            self.say("daemon 正在跑一轮（等不到 round lock），改成挂号单兜底："
                     "会让它别把这次改动记成罪。", "warn")
        try:
            # 挂号单要写在真正取消**之前**：daemon 任何一轮看到「booked 但 API 没了」
            # 都会先查它，命中就不记罪（一码一用，不掩护以后真的手动取消）。
            keys = self._period_cancel_keys(period, new_sessions)
            if keys:
                ks.expect_cancel(sorted(keys),
                                 reason="layout-changed:%s %s" % (period_id, self.sel_date))

            # 旧 API session 先对齐，否则 50min / 25min 会并存。
            if not self._cancel_stale_period_sessions(period, new_sessions, dropped):
                return False

            if not durations:
                removed = sp.remove_layout(self.sel_date, period_id)
                if removed is None:
                    raise ValueError("%s 没有自定义排布（本来就是默认）" % period_id)
                self.say("已撤销 %s 的自定义排布，恢复默认 50/25。" % period_id, "ok")
            else:
                sp.set_layout(self.sel_date, period_id, durations)
                body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                                 for s in new_sessions)
                self.say("✅ %s %s → %s" % (self.sel_date.isoformat(), period_id, body), "ok")

            # 排布落盘之后才动任务绑定：没写进去就什么都不清。
            self.clear_task_bindings_for_layout(period, new_sessions)

            # 从下载层 + 所有 layout + 一次性 + 长期规则重新生成 data.csv。
            # 写在 layout 成功之后。
            if self.rewrite_data_csv_from_plan():
                self.say("data.csv 已按下载层重算（精确日期行）。", "info")
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

        if not self.offline:
            # 新排布里执行区的场次需要立刻补订；先让 bridge 放开 profile。
            self._release_browser("daemon 下单要独占同一个 Chromium profile")
            self.say("立刻触发一轮 check（keep_session.py check）…", "cmd")
            rc, out, err = run_keep_session("check", timeout=600)
            for line in (out or "").splitlines()[-20:]:
                self.say(line)
            if rc != 0:
                self.had_error = True
                self.say("❌ check 退出码 %d%s（新契约已落盘，daemon 后续还会补）"
                         % (rc, ("：" + err) if err else ""), "error")
            self.rebuild_rows()
            self.refresh(quiet=True)
        return True

    def cmd_session(self, args):
        """session [--permanent] HH:MM LEN | HH:MM-HH:MM | T1 50 50

        - 默认：**只加在这一天**（一次性；存 temp/session-extras.json，不写进日历）。
          起点–终点的写法（`session 11:15-12:05`）会推出时长（这里是 50）。
        - `--permanent`：写进日历（temp/session-rules.json 里一条**按星期几重复**的
          长期规则）→ 以后每个这个星期几都会有这一场。
        - 第一个 token 是 T1 这种时段 ID 时，等同于 period T1 50 50（改排布）。

        创建零阻力 —— 有阻力的是**取消**。下单本身不在这里实现：薄封装
        keep_session.py check，业务逻辑一份就够。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        permanent = False
        rest = []
        for tok in toks:
            low = tok.lower()
            if low in ("--permanent", "-p", "--perm", "permanent"):
                permanent = True
            else:
                rest.append(tok)
        if rest and re.match(r"^[Tt]\d+$", rest[0]):
            return self.cmd_period_layout(rest)

        hhmm, dur = parse_session_spec(rest)     # 不合法会抛 ValueError（给人话提示）
        day = self.sel_date
        weekday = sch.weekday_of_day(day)
        if permanent:
            rec = sp.add_rule(weekday, hhmm, dur)
            self.say("✅ 已写进日历（按星期几重复）：每个%s %s 一场 %dmin"
                     % (sch.cn_weekday_of_day(day), rec["start"], rec["duration"]), "ok")
            self.say("   长期规则存在 %s（sync/populate 之后仍然在）。"
                     % sp.rules_path(), "info")
            row_token = weekday
        else:
            iso, rec = sp.add_extra(day, hhmm, dur)
            self.say("✅ 只加在 %s 这一天：%s %dmin（一次性，不进日历）"
                     % (iso, rec["start"], rec["duration"]), "ok")
            self.say("   想让它每周都出现，用 session --permanent %s %d。"
                     % (rec["start"], rec["duration"]), "info")
            row_token = iso

        # 手动加一场 = 我就要这一场：撤掉这一场上的「永不重订」判决，
        # 否则认过罪的时刻永远补不回来（daemon 在 due 之前就把它滤掉了）。
        if ks.clear_abandoned([sch.key_of(day, hhmm)]):
            self.say("撤销这条时刻的「永不重订」判决：重新进 daemon 的下单队列。", "ok")

        if self.rewrite_data_csv_from_plan():
            self.say("data.csv 已按下载层重算（含一次性 / 长期规则）。", "info")
        else:
            # 没有下载层（旧的手写 data.csv 模式）：只能直接加一行
            lineno = sch.add_row(None, row_token, hhmm, dur)
            self.reload_entries()
            self.say("✅ 契约表已写：data.csv 第 %d 行 %s,%s,%s"
                     % (lineno, row_token, hhmm, dur), "ok")

        got = sch.parse_hhmm(hhmm)
        start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        zone = self.effective_zone(sch.zone_name(start, datetime.datetime.now()), day)
        if zone == "draft":
            self.say("   它在草稿区（>24h 或 edit-tomorrow 放行）：这一轮不会订它，"
                     "等它滑进执行区会自动被吃。", "info")
        elif zone == "past":
            self.say("   ⚠ 这个时间已经过了：这一轮不会订（对下一个%s才生效）。"
                     % sch.cn_weekday_of_day(day), "warn")

        self._release_browser("daemon 下单要独占同一个 Chromium profile")
        self.say("立刻触发一轮 check（keep_session.py check）…", "cmd")
        rc, out, err = run_keep_session("check", timeout=600)
        for line in (out or "").splitlines()[-20:]:
            self.say(line)
        if rc == 0:
            self.say("✅ check 跑完。", "ok")
        else:
            self.had_error = True
            self.say("❌ check 退出码 %d%s" % (rc, ("：" + err) if err else ""), "error")
        self.refresh()
        return True

    def cmd_refresh(self, args):
        """``refresh [--check|-c]`` —— 重拉选中那天的 session；带 ``--check`` 就顺手对账。

        ``--check`` 做的是**只读外面世界、再把不对的掰回来**（需求第 6 条）：

        1. **该订没订上的 session**：执行区里契约有、API 没有的 → 让 daemon 补一轮
           （``keep_session.py check``，仍走 daemon 自己的重试上限/退避，不越权）。
        2. **该挂没挂上的 task**：本地记着「任务 8 挂在 11:00 那场」，但 Focusmate
           的 task 列表里那条 task 没有指向那场的 attachment（手动删过、
           或者上次只做了半截）→ 重新挂。**已完成的 task 不重挂**。
        3. **已完成但还没写回 OmniFocus 的 task**：补写「完成 + 截止时间」。
        4. **一场挤了两条任务、其中有 🔒 独占的**：那一场的任务**全部下架**并警告
           （见 :meth:`check_exclusive_sessions`；它排在最前面，先拆干净再谈重挂）。
        5. **那场当时还没订上、现在订上了的任务**：补挂（formatter 切出来的场次
           就落在这一条上，不然任务永远不会出现在 Focusmate 的 Task Manager 里）。
        6. 有问题的都修完之后再拉一次，表格显示的就是**修完之后**的样子。

        不带 ``--check`` 的 ``refresh`` **不在外面新造东西**（不订场、不在 Focusmate
        建 task、不改 data.csv），但它不是只管一行数：它把看板上每块组件的状态都
        重新对一遍账（见 :meth:`refresh_all`）——表格、任务完成状态、统计、收尾面板、
        各种缓存。其中「Focusmate 已经说完成了」的 task 会**顺手写回 OmniFocus**
        （幂等；这不是新决定，是同一条事实的第二个账本）。
        """
        toks = [str(tok).lower() for tok in (args or [])]
        want_check = any(tok in ("--check", "-c", "check", "查", "对账", "核对")
                         for tok in toks)
        pulled = self.refresh_all()
        if not want_check:
            return pulled
        checked = self.check_and_fix()
        return checked and pulled is not False

    def refresh_all(self):
        """``refresh`` 实作的**全量**那一半：把屏幕上**每块组件**的状态都对一遍。

        需求：「refresh 需要刷新这个界面每个组件的状态」。所以不再只是「重拉 session
        列表、重算表格」，而是一条命令把看板上每一块拆开重新对账（**不新造东西**：
        不订场、不挂任务、不改 data.csv）：

        ==================  ============================================
        组件                刷什么
        ==================  ============================================
        契约表格            重读 data.csv / 日历下载层 / 时段 layout → 重拉 session
        任务面板            任务的**完成状态**（``GET /v1/tasks`` 拉，Focusmate 说完成的
                            顺手把 OmniFocus 也补成完成 —— 幂等）
        统计视图            已经加载过、且超过 TTL 的那份数字重算一次
        收尾面板            正在问的那一轮从任务库里重读一遍
        警告 / 静音         缓存作废（mute 缓存、快照缓存、日志尾巴）
        画帧缓存            作废 → 下一帧按新状态重排版
        ==================  ============================================

        失败不拦路：任何一块刷不动就记一句警告，其余照刷 —— refresh 是「再对一次账」，
        不是「全有或全无」。
        """
        project_sync = self._project_tasks_refreshable()
        self.jobs.begin(2 + (1 if project_sync else 0), title="刷新（refresh）")
        self.forget_caches()
        pulled = self.refresh()
        # 「再对一次账」必须拿着刚拉回来的账：后台模式下这里等它落地
        self.wait_net()
        self.refresh_task_state()
        self.wait_net()
        if project_sync:
            self._refresh_project_tasks()
            self.wait_net()
        # refresh 的排期流水线：formatter 先占时间，项目 plan 后校验。
        # 必须在刚拉回的 session 账上跑，否则会把已取消的场次当成资源。
        try:
            self.reconcile_schedule_pipeline(day=self.sel_date, quiet=True)
        except Exception as e:                     # noqa: BLE001 - 一块坏不挡整次刷新
            self.had_error = True
            self.say("排期流水线失败（界面其余部分照常刷新）：%s" % e, "warn")
        try:
            self.check_untyped_ops(announce_import=False)
        except Exception as e:                     # noqa: BLE001 - 检查失败不挡刷新
            self.say("检查 op 类型失败：%s" % e, "warn")
        self.refresh_stat_state()
        self.refresh_review_state()
        self.forget_caches()
        self.invalidate()
        return pulled

    def forget_caches(self):
        """把所有「上次算出来就先存着」的东西作废（refresh 的前后各来一次）。"""
        forget_mute_cache()
        try:
            self._snapshot_cache.clear()
        except Exception:                              # noqa: BLE001 - 没有就算了
            pass
        self._log_tail_memo = None
        try:
            self.reload_periods()
        except Exception as e:                         # noqa: BLE001 - 文件坏了也别崩
            self.say("日历下载层/时段 layout 读不了（表格照旧用上次的）：%s" % e, "warn")

    def refresh_task_state(self):
        """任务的完成状态对齐 Focusmate（``GET /v1/tasks``）。

        走 :meth:`sync_task_completions` 而不是 ``cmd_task_sync``：refresh 不能
        顺手在 Focusmate 上建 task（重复任务落地）。**完成写回 OmniFocus 照做** ——
        「Focusmate 勾掉了」是已经发生的事实，本地打 ✓ 的同时 OmniFocus 也得完成
        （幂等；没有要写的就一次子进程都不起），见 :meth:`sync_task_completions`。
        """
        if self.offline:
            return False
        try:
            return self.sync_task_completions(quiet=True)
        except Exception as e:                         # noqa: BLE001 - 刷不动就不刷
            self.had_error = True
            self.say("任务完成状态没能刷新：%s: %s" % (type(e).__name__, e), "warn")
            return False

    def refresh_stat_state(self):
        """统计视图那份数字：**加载过**且过期才重算（没看过就别替用户拉）。"""
        if self.offline or self.stat is None:
            return False
        if self.stat_at is not None:
            age = (datetime.datetime.now() - self.stat_at).total_seconds()
            if age < st.CACHE_TTL:
                return False
        return self.cmd_stat()

    def refresh_review_state(self):
        """收尾面板：正在问的那一轮从任务库重读（用户可能刚在外面改了状态）。"""
        review = self.review
        if review is None:
            return False
        try:
            review.reload()
        except Exception as e:                         # noqa: BLE001 - 收尾比刷新重要
            self.say("收尾面板没能重读任务（照旧继续问）：%s" % e, "warn")
            return False
        return True

    def check_and_fix(self):
        """把「该订没订上 / 该挂没挂上 / 该写回没写回」查一遍并修掉。

        这是 ``refresh --check`` 的实作。完成写回 OmniFocus 已经由 ``refresh_all``
        → :meth:`refresh_task_state` → :meth:`sync_task_completions` 顺手做掉了
        （幂等），这里只管 session 和 task 挂载这两件事。
        """
        if self.offline:
            self.say("refresh --check：离线模式下没有外部世界可查，只重画了表格。", "warn")
            return False
        self.say("refresh --check：对账 —— 该订的 session、该挂的 task、该写回的完成…", "cmd")
        fixed = 0
        # 独占先查：一场挤了两条任务时**先**拆掉，否则后面的「重挂」会把它又挂回去。
        fixed += self.check_exclusive_sessions()
        fixed += self.check_bookings()
        fixed += self.check_task_attachments()
        # 保险起见再扫一遍「本地已完成但还没写回」的：上面的重挂可能刚把一条任务
        # 的 Focusmate 链接修好，而 writeback 是幂等的（写过的记在 writeback 上）。
        fixed += len(self.writeback_completions())
        # 修完再拉一次：表格要显示的是**修完**的样子，不是修之前的
        # （走全量：刚刚重挂过 task，任务面板/画帧缓存也得跟着换）
        if fixed:
            try:
                self.refresh_all()
            except Exception:                       # noqa: BLE001 - 拉不到就算了
                pass
        if fixed:
            self.say("✅ 对账完成：处理了 %d 处（详细见上面几条）。" % fixed, "ok")
            # 说完「修了」还得看一眼**修完**还剩什么 —— 补订是 daemon 的事，
            # 它这一轮可能没补上（上限/退避/还没到点），不能报喜不报忧。
            still = [r for r in self.rows if r.get("planned") and r.get("id") is None
                     and r.get("zone") == "execute"]
            if still:
                self.say("   还有 %d 场没订上：%s —— daemon 下一轮还会试（或 force-check）。"
                         % (len(still), "、".join(str(r.get("startLabel")) for r in still[:5])),
                         "warn")
        else:
            self.say("✅ 对账完成：没有发现要修的（session 都订上了、任务都挂对了）。", "ok")
        return True

    def check_exclusive_sessions(self):
        """``refresh --check`` 的独占对账：一场挤了两条任务就**全部下架** + 警告。

        规则（用户原话）：「如果不对就直接那个 session 的任务全部下下来 + 警告用户」。
        判据只有一条 —— **同一场**里有 ≥2 条**未收工**的任务，而且其中有 🔒 独占的。
        「同一场」优先按 ``session.focusmate_session_id`` 认（那才是 Focusmate 眼里
        的一场：两条任务挂在同一个 meetingId 上就是挤在一起，哪怕本地记的起点不同，
        切割出来的 11:30 和原来那场 11:00 就可能是同一个 meetingId）；没订上（没有
        id）才退回「同一天 + 同一时刻」。下架走 :meth:`_task_detach`（先删 Focusmate
        那条 task 再断本地），所以 Focusmate 上也不会留下两条打架的 task。

        返回处理了几条任务。
        """
        day = self.sel_date.isoformat()
        groups = {}
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if (sess.get("date") or "") != day:
                continue
            if not self.task_store.is_open(task):
                continue
            # 「一场 session」的身份：有 Focusmate session id 就按它认（那才是最硬的
            # 身份 —— 两条任务挂在同一个 meetingId 上就是挤在同一场，哪怕本地记的
            # 起点不一样）；没订上就退回「日期 + 起点」。
            key = str(sess.get("focusmate_session_id") or "") or \
                "start:%s" % (sess.get("start") or "")
            groups.setdefault(key, []).append(task)
        fixed = 0
        for key in sorted(groups):
            tasks = groups[key]
            if len(tasks) < 2:
                continue
            exclusive = [t for t in tasks if th.is_exclusive(t)]
            if not exclusive:
                continue
            where = "、".join(sorted({str((t.get("session") or {}).get("start") or "?")
                                     for t in tasks}))
            names = "、".join("%s「%s」" % (self.task_label(t),
                                          self.task_store.focusmate_title(t, 20) or "?")
                              for t in tasks)
            self.say("⚠ %s %s 那场挤了 %d 条任务，其中 🔒 独占的只能独占一场 → 全部下架：%s"
                     % (day, where, len(tasks), names), "warn")
            for task in tasks:
                try:
                    self._task_detach(task, reason="exclusive-conflict")
                    fixed += 1
                except Exception as e:              # noqa: BLE001 - 一条失败不挡其余
                    self.had_error = True
                    self.say("   ❌ 下架 %s 失败：%s" % (self.task_label(task), e), "error")
        if fixed:
            self.say("   下架后它们还在任务表里（未挂 session，🟦）。重新排："
                     "`task <序号> -f end`，或 `task <序号> -a <session>` 自己挑一场。",
                     "warn")
        return fixed

    def check_bookings(self):
        """该订没订上的 session → 让 daemon 补一轮。返回修了几场。"""
        missing = [r for r in self.rows
                   if r.get("planned") and r.get("id") is None
                   and r.get("zone") == "execute"]
        if not missing:
            return 0
        names = "、".join("#%d(%s)" % (self.rows.index(r) + 1, r.get("startLabel"))
                         for r in missing[:5])
        self.say("⚠ %d 场该订却没订上：%s —— 叫 daemon 补一轮（check）…"
                 % (len(missing), names), "warn")
        self._release_browser("daemon 下单要独占同一个 Chromium profile")
        job = self.op_keep_session(["check"], "对账（keep_session check）", wait=True)
        outcome = job.outcome
        if outcome is None or not outcome.ok:
            self.had_error = True
            self.say("❌ check 没跑成%s（计划照旧留着，daemon 下一轮还会补）"
                     % (("：" + outcome.error) if outcome else ""), "error")
            return 0
        return len(missing)

    def check_task_attachments(self):
        """任务真的挂在那场 session 上了吗？挂漏/挂错的重新挂一遍。返回修了几条。

        判据只有一条，而且来自 Focusmate 自己：``GET /v1/tasks`` 里那条 task 的
        ``attachments`` 里有没有这一场的 session id。本地账本说「挂了」不算数 ——
        手动在 Task Manager 删过、或上次调 API 只做了半截，账本都会照旧留着。

        两类都要修：

        1. **本地说挂着、Focusmate 说没挂上** → 重挂；
        2. **本地挂着「那场当时还没订上」**（formatter 切出来的场次就是这样：
           ``assign_task_to`` 只会记本地）→ 现在场次订上了就**补挂**，否则那条
           任务永远不会出现在 Focusmate 的 Task Manager 里。
        """
        remote = self._remote_tasks()
        if remote is None:
            self.say("⚠ 拉不到 GET /v1/tasks（bridge 不在？），任务挂载这一项跳过。", "warn")
            return 0
        day = self.sel_date.isoformat()
        bad, late = [], []
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if (sess.get("date") or "") != day:
                continue
            if not self.task_store.is_open(task):
                continue
            if self._fm_pending(task):
                continue                       # 乐观挂载还在后台跑，别重复挂
            verdict = self._attachment_verdict(task, remote)
            if verdict:
                bad.append((task, verdict))
                continue
            if sess.get("focusmate_session_id"):
                continue
            fm = task.get("focusmate") or {}
            if fm.get("task_id") or fm.get("id"):
                continue                       # 已经有 Focusmate task，别再造一条
            idx, row = self._row_for_session(sess)
            if row is None or row.get("id") is None:
                continue
            late.append((task, idx))
        fixed = 0
        for task, idx in late:
            sess = task.get("session") or {}
            self.say("⚠ 任务 %s「%s」挂的 %s %s 那场已经订上了，但 Focusmate 里还没有"
                     "这条 task —— 补挂…"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 30),
                        sess.get("date") or "?", sess.get("start") or "?"), "warn")
            try:
                self._assign_task(task, str(idx), refresh=False)
                fixed += 1
            except Exception as e:                  # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                self.say("   ❌ 补挂失败：%s" % e, "error")
        for task, verdict in bad:
            sess = task.get("session") or {}
            self.say("⚠ 任务 %s「%s」%s（本地记着挂在 %s %s）—— 重挂…"
                     % (self.task_label(task), self.task_store.focusmate_title(task, 30),
                        verdict,
                        sess.get("date") or "?", sess.get("start") or "?"), "warn")
            idx, row = self._row_for_session(sess)
            if row is None or row.get("id") is None:
                self.say("   那一场现在不在表格里（或还没订上），先 refresh --check 等它订上。",
                         "warn")
                continue
            try:
                self._assign_task(task, str(idx), refresh=False)
                fixed += 1
            except Exception as e:                  # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                self.say("   ❌ 重挂失败：%s" % e, "error")
        self.rebuild_rows()
        return fixed

    @staticmethod
    def _attachment_verdict(task, remote):
        """本地说挂着、Focusmate 说不算 → 一句人话；没问题返回 ``""``。"""
        sess = task.get("session") or {}
        want = str(sess.get("focusmate_session_id") or "")
        if not want:
            # 那一场还没有 Focusmate session id（没订上/快照）—— 无从查起
            return ""
        fm = task.get("focusmate") or {}
        tid = str(fm.get("task_id") or fm.get("id") or "")
        if not tid:
            return "没有 Focusmate task id"
        row = remote.get(tid)
        if row is None:
            return "Focusmate 那边这条 task 已经不在了"
        # 已完成/已放弃的 task 不该触发「重挂」：任务已经收工，哪怕 attachment
        # 对不上（例如 Focusmate 后续改了挂载信息），再建一条也只会多一个幽灵。
        if row.get("completed") or row.get("completedAt") or row.get("completed_at"):
            return ""
        ids = [str(a.get("externalId") or a.get("id") or "")
               for a in (row.get("attachments") or [])]
        if want not in ids:
            return "Focusmate 那条 task 没挂在这一场上"
        return ""

    def _remote_tasks(self):
        """``GET /v1/tasks`` → ``{task_id: 行}``；拉不到返回 None（不是空 dict）。"""
        try:
            res = self.bridge.call("tasks", timeout=180)
        except BridgeError as e:
            self.say("拉 GET /v1/tasks 失败：%s" % e, "warn")
            return None
        rows = res.get("tasks") if isinstance(res, dict) else res
        return {str(r.get("id")): r for r in (rows or [])
                if isinstance(r, dict) and r.get("id") is not None}

    def rewrite_data_csv_from_plan(self):
        """下载层 + layout + 一次性 + 长期规则 → 重算 data.csv。

        没有下载层（说明这台机器还在用"手写 data.csv"那套）就返回 False，
        调用方退回 schedule.add_row —— 绝不拿空计划把人家手写的契约冲掉。
        """
        download = sp.load_download()
        if not download.get("periods"):
            return False
        layouts = sp.load_layouts()
        csv = sp.render_data_csv(download, layouts, date_specific=True)
        before = [ln.strip() for ln in sch.read_text().splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
        after = set(ln.strip() for ln in csv.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#"))
        sch.backup()
        sch._write(sch.DATA_CSV, csv)
        # 下载层/排布/一次性/长期规则之外的旧行会被冲掉 —— 记下来，下次画框报警。
        self.csv_dropped = [ln for ln in before if ln not in after]
        self.reload_entries()
        self.reload_periods()
        if self.csv_dropped:
            self.say("⚠ 重算 data.csv 冲掉了 %d 行（下载层/layout/一次性/长期规则里没有的）："
                     "%s%s；旧文件备份在 %s"
                     % (len(self.csv_dropped), "、".join(self.csv_dropped[:3]),
                        "…" if len(self.csv_dropped) > 3 else "", sch.BACKUP_CSV), "warn")
        return True

    def cmd_force_check(self):
        """force-check —— 忽略重试上限/退避，强制对执行区里所有还没订上的 session
        各试一次重订。

        普通 `check` 只补订没到上限、且不在退避中的场次；有的 slot 失败 3 次后被标成
        failed 就再也不碰了。force-check 是手动扳轨：已经 failed、正在退避、attempts
        已爆表的，全都再试一次。已放弃（判决 abandoned）和已订上的不碰。
        下单逻辑不在这里重写：薄封装 `keep_session.py check --force`。
        """
        self.say("force-check：忽略重试上限/退避，对执行区所有没订上的 session 各试一次…", "cmd")
        self._release_browser("daemon 下单要独占同一个 Chromium profile")
        rc, out, err = run_keep_session("check", "--force", timeout=600)
        for line in (out or "").splitlines()[-40:]:
            self.say(line)
        if rc == 0:
            self.say("✅ force-check 跑完。", "ok")
        else:
            self.had_error = True
            self.say("❌ force-check 退出码 %d%s" % (rc, ("：" + err) if err else ""), "error")
        self.refresh()
        return True

    def cmd_sync(self, args):
        """sync [download_calendar 的参数] —— 调 download_calendar.py populate，把日历
        重新切成 data.csv（会覆盖 data.csv；旧文件备份到 temp/data.csv.bak）。

        这里**不重写**日历逻辑：薄封装 download_calendar.py，和 schedule.py 一样是唯一真源。
        `sync --days 3` 之类的参数原样透传给 populate。

        **两步、一个标题**：`sync（同步日历）` 下面先是 `[calendar] 日历 download`，
        再是 `[calendar] 日历 populate` —— 第二步挂在第一步的成功回调里，
        因为「下载成功才谈得上切 csv」。
        """
        argv = [str(a) for a in args]
        self.jobs.begin(2, title="同步日历（sync）")
        self.say("sync：先 download（工作/空闲 → 中间层），再 populate "
                 "（中间层 + 时段 layout → data.csv）…", "cmd")
        first = self.op_calendar(["download"] + argv, "日历 download",
                                 on_ok=lambda payload: self._sync_populate(argv))
        if self.jobs.sync:
            outcome = first.outcome
            return bool(outcome is not None and outcome.ok)
        return True

    def _sync_populate(self, argv):
        """sync 的第二步：download 成功了才切 csv（同一批里的第 2 步）。"""
        self.op_calendar(["populate", "--from-download"] + list(argv), "日历 populate")

    def _release_browser(self, why):
        """让 bridge 放开浏览器（daemon 下单用同一个 profile，会撞锁）。"""
        try:
            up, browser, _ = self.bridge.browser_state()
        except BridgeError:
            return False
        if not up or not browser:
            return False
        self.say("先让 bridge 放开浏览器（%s）…" % why, "info")
        try:
            self.bridge.call("release", timeout=60)
            return True
        except BridgeError as e:
            self.say("释放浏览器失败（不影响，但可能撞锁）：%s" % e, "warn")
            return False

    def cmd_start(self):
        self._release_browser("daemon 下单要独占同一个 Chromium profile")
        self.jobs.begin(1, title="启动 daemon（start）")
        self.say("启动 daemon…（在后台起，结果进 Recent）", "cmd")
        job = self.op_keep_session(["daemon", "start"], "起 daemon")
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True

    def cmd_stop(self):
        """停用 daemon —— 和取消 session 共用同一道宣言闸门（密码系统已删掉）。

        daemon 是你今天 session 的保命绳：停掉之后，执行区里没人补订、
        罪也没人记。所以停用要付一样的代价。
        """
        self.emit(Panel(
            "停掉 daemon = 执行区里剩下的场次没人补订、也没人记账。\n"
            "要停就得和取消 session 一样，过 The Walk of Shame。",
            title="[bold yellow]停用 daemon[/]",
            border_style="yellow", box=box.ROUNDED,
        ))
        if not self.require_declaration("停用 daemon"):
            self.say("没认罪，daemon 还活着。", "warn")
            return False
        self.jobs.begin(1, title="停用 daemon（stop）")
        self.say("停 daemon…（在后台停，结果进 Recent）", "cmd")
        job = self.op_keep_session(["daemon", "stop"], "停 daemon")
        if self.jobs.sync:
            return bool(job.outcome is not None and job.outcome.ok)
        return True

    def cmd_help(self, args=None):
        toks = [str(a).strip().lower() for a in (args or [])]
        if any(tok in ("--task", "-t", "task", "任务") for tok in toks):
            return self.cmd_help_task()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True, justify="right")
        t.add_column()
        for cmd, desc in [
            ("Tab", "切换视图：契约 → 专注 → 统计 → 任务 → 设置 → 契约（循环）"),
            ("PgUp / PgDn", "长看板在**当前界面内**往上/往下翻；只在一屏范围内滚动，"
                            "不会把旧帧写进终端 scrollback（全屏模式）"),
            ("open working|detail", "切换 session 看板口径：working = 实际为主、"
                                     "过去只留最近一场；detail = 计划 + 实际全部"),
            ("open/close <表头>", "展开/折叠契约表的一列；加 --permanent 写进偏好，"
                                  "working 默认折叠 时段类型 / 时段名称 / 契约"),
            ("stat [refresh]", "切到「统计」视图：生涯累计 / 每天分布 / 永不重订名单（只读 API）"),
            ("stat refresh full", "统计：丢掉缓存、全量重拉（默认只增量拉缺口 + 最近几天）"),
            ("Shift+Tab（专注视图）", "循环切换专注模式：普通 / 做题 / 写作"),
            ("day -1|0|1|YYYY-MM-DD", "切换看哪一天（草稿区随便看）"),
            ("session HH:MM LEN", "只加这一天的 session（一次性），再立刻触发一轮 check"),
            ("session --permanent HH:MM LEN", "写进日历：按星期几重复的长期规则"),
            ("session HH:MM-HH:MM", "同上，但用起点–终点（如 11:15-12:05 → 50min）"),
            ("session T1 50 50", "按时段改排布：T1 的第一场从时段起点开始，超时就报错"),
            ("period T1 25 25 25 25", "同 session T1 ...；只给 T1 = 撤销自定义，恢复默认"),
            ("set <id> <value>（设置视图）", "改设置；不写 value = 恢复默认。settings 重新列出全部"),
            ("warn on|off", "状态框里的警告显示开关（不写 = 取反）"),
            ("task create ...", "在「任务」视图建任务：task create [id] [field=value ...]"),
            ("task delete/modify ...", "task delete <id> / task modify <id> <field> <值>"),
            ("task assign <session ID>", "把选中的任务挂到表格 #N 的 session，并自动写 Focusmate task"),
            ("task <任务ID> -a <session ID>", "选中并挂上：task 8 -a 1（裸 ID 就等于 task select 8）"),
            ("task ... --assign N", "任何 task 子命令都能带：跑完顺手挂到表格 #N，"
                                    "如 task create title=写论文 --assign 3"),
            ("task create ... --repeat mon@22:15",
             "建每周重复任务：每周一 22:15 那场自动挂上（拆分后那一周就不挂）"),
            ("task repeat <id> 周几@HH:MM|off", "给已有任务加上/取消每周重复；"
                                                  "OmniFocus 导入的规则会保留原 RRULE，"
                                                  "task repeat <id> 直接看原规则"),
            ("重复任务本体", "带 repeat 的任务（source 指向 OmniFocus）。它只当模板，"
                              "不在【契约】的任务面板 / task 列显示、永远不挂 session，也"
                              "**不能**被 done / -m complete；任务视图里显示成 id:N，"
                              "要结束这套重复用 task repeat off 或 task <id> drop（手动）"),
            ("重复任务子任务", "到了规则日自动生成一条：repeat_parent 指向本体，"
                                "复制标题/字段/O-F 关联，真正挂 session、显示在契约表里，"
                                "带 🔁；只有它能被完成"),
            ("重复任务日期", "本体 repeat.rule 是 OmniFocus 原 RRULE（DAILY / 多个 BYDAY / "
                              "INTERVAL / COUNT / UNTIL …），日期由 dateutil 展开；"
                              "start/duration 只决定优先挂当天哪一场"),
            ("契约表来源记号", "🔁 = 重复任务子任务 · 🔗 = 有 OmniFocus 关联 · "
                                "🔁🔗 = 两者都有 · ⏱ = 挂了 formatter"),
            ("task import", "读剪贴板导入 OmniFocus 任务；如果是重复任务（例如每周三），"
                            "会先问要不要把重复设置也复制到本地；-st / --single-task "
                            "= 只导入这一条，不问也不建 repeat"),
            ("三个数据模型区别", "pro = 真正项目 Project（有阶段 running/bottleneck、可关联文件） · "
                                 "op = OmniFocus Project（可以是 incubator 或普通的 SOP 两种类型） · "
                                 "task = 具体任务 Task（挂到 session 执行的单条任务）"),
            ("pro / project", "管理真正项目 Project (pro)：阶段为 bottleneck 时只允许对应的 "
                              "incubator op 进行；阶段为 running 时只允许普通的 SOP op 进行。"
                              "SOP op 通过 OmniFocus 标签 `project/…` 自动关联真正的项目 pro"),
            ("pro <序号> -a / --advance", "快捷切换 pro 状态（running ↔ bottleneck），"
                                           "需提供新的 op 作为关联（可直接跟 `task import` 或 `op <序号>`，"
                                           "也可回车后按提示输入）"),
            ("pro <序号> file <路径>", "为项目 pro 关联一个文件，作为敲 `open` + Enter 之后顺便打开的文件"
                                       "（也可 `pro create <名> file=<路径>` 或 `pro <序号> open`）"),
            ("op [序号] [sop|incubator]", "管理 OmniFocus Project (op)：每个 op 必须是 SOP 或 incubator "
                                          "两种类型之一，导入时必须说明；每次 refresh 也会自动检查并让用户补全"),
            ("task import [sop|incubator]（项目）",
             "剪贴板里是 **OmniFocus 项目**（omnifocus:///project/PK）→ 进 op 库（也可 `op import sop|incubator`）："
             "需说明 SOP 或 incubator 类型；带 `project/…` 标签自动关联 pro；时长不齐整批拒绝"),
            ("project [序号] [refresh|remove|check]", "看 pro 与 op 库 / 看 #1 详情与清单 / 重新去 "
                                                       "OmniFocus 读一遍 / 撤掉 / check = 每日对账"),
            ("plan [天数]", "两阶段排期：第一阶段处理类型为 SOP 的 op（仅限 running 阶段的 pro），"
                            "敲 complete 完成 SOP 时间安排后，自动进入第二阶段 incubator 时间安排"
                            "（仅展示并分配类型为 incubator 且处于 bottleneck 阶段的 op）。"
                            "每个阶段都是分配 count + 自动挑任务挂 session。**默认今天**，plan +1 = 明天"),
            ("task -t  /  --trigger", "列出 trigger（创建触发）预设；现在是空列表 —— "
                                        "框架已就位，注册函数后才有名字"),
            ("task <id> -t <preset> [参数...]", "挂创建触发：只有在预设条件成立时才建任务；"
                                                  "当前只记本地，runner 还没实现"),
            ("task -f  /  --formatter",
             "列出 formatter（排期算法）预设；现在有 end / enlist"),
            ("task <id> -f end", "按任务 duration 找当天最后一段可用时间，截出尾部并自动挂 session；"
                                  "例如 25min 任务占 11:30–12:00（25 + 5min 休息）。"
                                  "挂在重复任务**本体**上时，formatter 会转嫁给今天/明天的"
                                  "子任务，本体自己不排"),
            ("task archive [N]", "看完成归档（data/task-archive.json，只增不改）"),
            ("task sync", "GET /v1/tasks，把 completedAt 连同 session 写进完成归档"),
            ("cancel N", "取消 #N：草稿区直接删契约；执行区要敲宣言"),
            ("relocate N ±MM", "临时把 #N 提前/推后：不写 data.csv；推后一天一次，且不能跨天"),
            ("relocate T1 ±MM", "把整个时段里的每场一起挪：相对位置不变、任务跟着走；"
                                "推后每时段一天一次，和别的场次重叠就拒绝"),
            ("plan 里那几句", "task 1 4 = 项目 #1 分 4 count（100 分钟）· task 1 clear = 清空 · "
                              "再敲一次是覆盖（不是相加）· 超预算时整条命令都不执行 · "
                              "q 放弃这次 plan（不建任何任务）"),
            ("🚪 到点没开始（p/x/s）",
             "计划 14:00 那场到 14:05 在 API 里还是没有 → 问一次：p 15 推迟这一场 / "
             "p T1 15 推迟整个时段 / x 不去了（算缺失，任务交给收尾）/ s 先不管。"
             "推迟到 14:10 之后作废，直接算缺失"),
            ("mute N on|off", "把 #N 静音；不写 on/off 就是取反"),
            ('title N "文本"', '把 #N 的标题改成文本；title N "" 清空'),
            ("force-check", "忽略重试上限/退避：把所有没订上的 session 各强制重订一次"),
            ("sync [--days N]", "跑 download_calendar.py populate：日历 → 覆盖 data.csv"),
            ("refresh  (r)", "全量刷新：session（会重编号）+ 任务完成状态 + 统计 + 收尾面板；"
                             "refresh --check 再对账并修正"),
            ("start", "启动 daemon（会先让 bridge 放开浏览器）"),
            ("stop（契约视图）", "停用 daemon（和取消一样要敲宣言）"),
            ("stop（专注视图）", "消耗一次暂停：暂停 5 分钟，每天最多 3 次，daemon 不念你"),
            ("status  (s)", "只重画看板"),
            ("quit  (q)", "退出（bridge 和浏览器一起收掉）"),
        ]:
            t.add_row(cmd, desc)
        panel_main = Panel(t, title="[bold]命令（回车或敲任意命令关闭）[/]",
                           border_style="cyan", box=box.ROUNDED)
        self.help_overlay = panel_main
        self.invalidate()
        self.emit(panel_main)
        self.emit(
            "[dim]ID 就是表格第一列，按时间排序、每次 refresh 重编号 —— 看着表敲，别凭记忆。\n"
            "区域标记（状态列第一格）：草=草稿区（随便改）· 锁=执行区（取消要敲宣言）"
            "· 过=已过去。\n"
            "专注视图：只认 stop（消耗暂停）和 Shift+Tab（切模式），"
            "预约那套命令在那边一律没用。\n"
            "统计视图：只读，stat 走缓存、stat refresh 只增量拉缺口 + 最近几天；"
            "它不改 data.csv、不碰 API 写接口。\n"
            "重复任务机制：本体（repeat + source=omnifocus）只当模板，永远不挂 session，"
            "所以不出现在【契约】表的 task 列。到了规则日（RRULE 判定，不看这一刻哪一场）"
            "自动生成一条 occurrence 子任务：repeat_parent 指向本体、继承字段和 OmniFocus 关联；"
            "子任务才去挂当天 session（同起点优先，否则取最近一场；当天没 session 也先生成，"
            "之后手动 assign 或 task sync 再补）。子任务的 session 终点仍会写成 due。\n"
            "本体的 formatter 只是**模板**：task <本体> -f end 会先为今天/明天/下一个规则日"
            "生成子任务，再在每条子任务上跑 formatter（子任务带 ⏱ 和它自己的 reservation）；"
            "本体自己永远没有 reservation、也不占 session。切换 day +1 就能看到明天那条已经"
            "排好的子任务。只有子任务能被完成；本体要结束只能手动 task repeat off 或 drop。\n"
            "契约表 task 列来源记号：🔁=重复任务子任务，🔗=有 OmniFocus 关联，"
            "两个都有就是 🔁🔗；⏱=挂了 formatter（不是「重复」的意思）。"
            "本体不进契约面板，只在 task 视图里显示为 id:N、来源记号只有 🔗。\n"
            "OmniFocus 写回：**只要知道一条任务完成了就一定写**（task done / 收尾 D / "
            "task sync / 收尾自动核对 / 任何一次 refresh 同步完成状态），幂等；"
            "放弃则会放弃 OmniFocus 那条（收尾 A/C/E 那些「换账本」的动作用 --local）。"
            "OmniFocus 那条自己要是重复任务，写完会在那边翻到下一次（新 primary key），"
            "本地还开着的记录（本体 + 后面的子任务）自动改挂过去 —— 不然下一次的活"
            "永远勾不掉；放弃本体时带 --all-occurrences，连那一套一起放弃。\n"
            "任务视图：task 命令在任何视图都能敲；任务字段不校验，JSON 存在 data/tasks.json。\n"
            "设置视图：列出所有设置项和解释；set <id> <value> 改，"
            "edit-tomorrow / tomorrow-plan 见那里的说明。\n"
            "状态框（契约视图最上面）：契约执行情况 + 警告；warn off 可以藏掉警告。\n"
            "时段 ID（T1/T2）来自下载层 temp/calendar-download.json；"
            "同段后续行留空是正常的。[/]"
        )
        return False

    def cmd_help_task(self):
        """``help --task``：任务视图的详细说明（列、标记、缩写、全部命令）。"""
        from rich.table import Table as _Table
        t = _Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True, justify="right")
        t.add_column()
        t.add_row("默认列", "序号 · 标记 · title · project · 状态 · session · 截止 · 备注")
        t.add_row("默认折叠", "id · 重复 · trigger · formatter · Focusmate · OmniFocus · "
                              "完成时间 · 标签 · duration · count · exclusive · defer"
                              "（自定义字段默认也收进备注；用 open <表头> 展开）")
        t.add_row("标记", "🔁=重复子任务  🔁*=重复模板  📁=项目任务  🔗=OmniFocus  🎯=Focusmate  "
                          "⏳=挂载中  ⏱=formatter  ⚡=trigger  🔒=独占")
        t.add_row("状态", "·=进行中  ✓=完成  ✗=放弃  ⤼=转移走")
        t.add_row("备注缩写", "dur=分钟  cnt=已投/上限  excl=独占  tag=标签  "
                              "rep=重复  fmt=formatter  trig=trigger  note=备注")
        t.add_row("时间戳", "同天 HH:MM · 明天/昨天 +1/-1 HH:MM · 同年 MM/DD HH:MM")
        t.add_row("分页", "open 1 / open 2 / open 3 直接翻页；表下会显示 第 X/Y 页。"
                          "每页条数按终端高度自适应；长看板可用 PgUp/PgDn 帧内翻看")
        t.add_row("排序", "sort <字段> [asc|desc] · sort off；字段可用 id/title/project/"
                          "状态/session/截止/duration/count")
        t.add_row("分组", "group <字段> 或 group grouping(project) · group off；"
                          "分组列在最前面，同组首行显示组名、后续留空")
        t.add_row("搜索", "search <词> [--group <字段>] · search grouping(project) <词> · "
                          "search off；支持 field=value")
        t.add_row("三模型区别", "pro = 真正项目 Project（running/bottleneck + 关联文件） · "
                                "op = OmniFocus Project（SOP 或 incubator） · task = 单条任务")
        t.add_row("关联项目", "op（SOP）用标签 project/… 关联 pro；导入/刷新把叶子任务镜像进任务库"
                              "（fields.project），任务视图可搜索/group project；plan 分 SOP → incubator 两阶段唤起。")
        t.add_row("任务库", self.task_store.path)
        p_detail = Panel(t, title="[bold]Task · 任务视图详细说明[/]",
                         border_style="cyan", box=box.ROUNDED)
        self.emit(p_detail)

        c = _Table.grid(padding=(0, 2))
        c.add_column(style="bold", no_wrap=True, justify="right")
        c.add_column()
        for cmd, desc in [
            ("task create [id] [字段=值 ...]", "新建任务；字段不校验"),
            ("task delete <序号|id:N>", "删除任务"),
            ("task modify <序号|id:N> <字段> <值>", "改任意字段"),
            ("task select <序号|id:N>", "选中任务"),
            ("task <序号>", "等价 task select"),
            ("task assign [序号] <session编号>", "挂到 session（自动建 Focusmate task）"),
            ("task <序号> -a <session|none|current>", "挂上/下架/挂当前场"),
            ("task <序号> -n <备注>", "顺手写备注"),
            ("task <序号> -c 5|+2|-1", "设 count / 加进度 / 减进度"),
            ("task <序号> -e true|false", "独占 / 取消独占"),
            ("task <序号> -m complete|discard", "只改本地状态（不碰外部系统）"),
            ("task <序号> done [分钟]", "完成（勾 Focusmate + 写回 OmniFocus）"),
            ("task <序号> drop [--local]", "放弃（默认也放弃 OmniFocus）"),
            ("task <序号> duplicate [-a N]", "复制一条任务"),
            ("task <序号> link [链接|PK]", "关联 OmniFocus"),
            ("task import [-st]", "读剪贴板导入 OmniFocus 任务/项目"),
            ("task <序号> -f end", "formatter：排到当天最后一段并自动挂"),
            ("task <序号> -t <preset> [参数]", "挂 trigger 预设"),
            ("task sync / archive [N]", "同步完成时间 / 看归档"),
            ("sort / group / search", "任务表排序 / 分组 / 搜索"),
            ("open <页码|表头> / close <表头>", "翻页 / 展开列 / 折叠列"),
            ("help --task", "本页"),
        ]:
            c.add_row(cmd, desc)
        p_cmd = Panel(c, title="[bold]Task · 命令（回车或敲任意命令关闭）[/]",
                      border_style="cyan", box=box.ROUNDED)
        self.help_overlay = Group(p_detail, p_cmd)
        self.invalidate()
        self.emit(p_cmd)
        self.emit("[dim]更多细节见 feature_intents/task.md；任务库字段不校验，"
                  "内部簿记见 store 文件。[/]")
        return False

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


# ---------------------------------------------------------------- daemon 子进程


def run_keep_session(*args, **kw):
    """用子进程跑 keep_session.py（不在自己进程里 fork，省得和 TUI 抢终端）。"""
    cmd = [sys.executable, os.path.join(HERE, "keep_session.py")] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           timeout=kw.get("timeout", 90))
    except subprocess.TimeoutExpired:
        return 1, "", "超时"
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def run_download_calendar(*args, **kw):
    """用子进程跑 download_calendar.py（拉日历 → data.csv）。"""
    cmd = [sys.executable, os.path.join(HERE, "download_calendar.py")] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           timeout=kw.get("timeout", 300))
    except subprocess.TimeoutExpired:
        return 1, "", "超时"
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


# ---------------------------------------------------------------- session 参数解析


def parse_session_spec(args):
    """把 `session` 命令的参数解析成 (起点 'HH:MM', 时长 '25|50|75')。

    同一场 11:15–12:05 的几种写法都认（时长由区间推出）：

        session 11:15 50            ← 原写法：起点 + 时长
        session 11:15-12:05         ← 起点–终点
        session 11:15 - 12:05       ← 中间的短横线是单独一个 token
        session 11:15 12:05         ← 空格分隔的起点/终点
        session "11:15 - 12:05"     ← 整个区间当一个 token
    """
    toks = [str(a).strip() for a in args if str(a).strip()]
    usage = ("用法：session HH:MM LEN（LEN ∈ 25/50/75），"
             "或 session HH:MM-HH:MM（如 session 11:15-12:05）")
    if not toks:
        raise ValueError(usage)

    # 候选区间串：把各种写法归一成 "起点-终点" 再交给 schedule.parse_hhmm_range
    candidates = [" ".join(toks)]
    if len(toks) == 2 and sch.parse_hhmm(toks[1]) is not None:
        candidates.append("%s-%s" % (toks[0], toks[1]))
    if len(toks) == 3 and toks[1] in sch.RANGE_SEPARATORS:
        candidates.append("%s-%s" % (toks[0], toks[2]))
    range_like = False
    range_re = r"\d{1,2}:\d{2}\s*[-\u2013\u2014~\uff5e\u81f3\u5230]\s*\d{1,2}:\d{2}"
    for spec in candidates:
        rng = sch.parse_hhmm_range(spec)
        if rng:
            return rng
        if re.search(range_re, spec):
            range_like = True

    # 长得像区间但时长不合法（不是 25/50/75）→ 给明确提示，别让人猜
    if range_like:
        raise ValueError("区间不合法：%r（起点/终点要 HH:MM，时长只能是 25/50/75 分钟）"
                         % " ".join(toks))

    # 回退：原写法 HH:MM LEN
    if len(toks) == 2 and toks[1] in sch.DURATIONS:
        got = sch.parse_hhmm(toks[0])
        if got is None:
            raise ValueError("时间格式不对：%r（要 24 小时制 HH:MM，如 11:15）" % toks[0])
        return sch.fmt_hhmm(*got), toks[1]

    if len(toks) == 1:
        raise ValueError(usage + "；只给了 %r，没说时长" % toks[0])
    raise ValueError("时长只能是 25/50/75（LEN 位置给了 %r）；%s" % (toks[1], usage))


# ---------------------------------------------------------------- CLI


def parse_relocate_offset(token):
    """解析 relocate 的第二个参数：±MM，必须是 15 的倍数、不能是 0。"""
    s = str(token).strip()
    if not re.match(r"^[+-]?\d+$", s or ""):
        raise ValueError("挪动分钟数要像 -15 / +15；你给的是 %r" % token)
    minutes = int(s)
    if minutes == 0:
        raise ValueError("挪 0 分钟等于没挪；-15 是提前，+15 是推后。")
    if minutes % 15 != 0:
        raise ValueError("Focusmate 只认 15 分钟整点；挪动分钟数要是 15 的倍数，你给的是 %r"
                         % token)
    return minutes


def parse_relocate_spec(args):
    """把 relocate 参数解析成 (ID token, 偏移分钟)。

    兼容 `["1", "-15"]`、连写 `["1-15"]`，也兼容只传偏移量 `["-15"]`
    （这种调用方自己已经有 ID）。
    """
    toks = [str(a).strip() for a in args if str(a).strip()]
    usage = ("用法：relocate N ±MM（单场）或 relocate T1 ±MM（整段）；"
             "MM 是 15 的倍数，如 relocate 3 -15 / relocate T1 +15")
    if len(toks) == 2:
        return toks[0], parse_relocate_offset(toks[1])
    if len(toks) == 1:
        # 有人会连写成 relocate 1-15 / relocate 1+15 / relocate T1+15。
        m = re.match(r"^([Tt]?\d+)([+-]\d+)$", toks[0])
        if m:
            return m.group(1), parse_relocate_offset(m.group(2))
        return None, parse_relocate_offset(toks[0])
    raise ValueError(usage)


def cmd_install_deps():
    print("把 rich 装到 %s（不污染系统环境）…" % PYLIBS)
    os.makedirs(PYLIBS, exist_ok=True)
    rc = subprocess.call([sys.executable, "-m", "pip", "install",
                          "--disable-pip-version-check", "--target", PYLIBS, "rich"])
    if rc == 0:
        print("装好了。用 `python3 warden.py` 起 TUI。")
    return rc


def parse_day_offset(token):
    """CLI 的 --day：偏移量（-1/0/1）或 YYYY-MM-DD 都认。"""
    token = str(token).strip()
    try:
        return int(token)
    except ValueError:
        try:
            return (datetime.date.fromisoformat(token) - datetime.date.today()).days
        except ValueError:
            raise argparse.ArgumentTypeError("要 -1/0/1 这种偏移量，或 2026-10-11 这种日期")


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
