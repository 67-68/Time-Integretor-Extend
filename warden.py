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
按 **Tab** 在多个视图间循环切换，视图显示在命令提示上面。现有五个：

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
   `task assign` 成功后才写本地，Focusmate 侧 task id / completedAt 都会记回来。
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
   的 session 自动打开 **Focusmate quiet**（「静音」只指这个）。本地看门狗/通知/
   嘶吼一概照旧 —— daemon 里根本不读这张表。表在 `temp/session-mutes.json`，
   判定在 `session_plan.mute_window_at()`，
   见 [`feature_intents/session_mute.md`](feature_intents/session_mute.md)。
5. **设置**：`settings_store.SETTINGS` 里注册的全部设置项（id / 当前值 / 默认值 /
   解释），`set <id> <value>` 修改，不写 value = 恢复默认。现有四项：
   `edit-tomorrow`（今天可以先安排明天，过了凌晨回到 24 小时窗口）、
   `tomorrow-plan`（明日时间安排 = 一个每周重复任务）、`omnifocus`（和 OmniFocus
   联动，**只单向**）、`postsession`（结束 15 秒后把 warden 拉到前台收尾）。

**收尾（session 结束 +15s）**：daemon 到点把 warden 拉到前台，warden 逐个问
本场没做完的任务 —— A 延期 / B 放弃 / C 重议 / D 缺漏完成补充 / E 快捷推后。
流程在 `postsession.py`（纯逻辑）+ 这里的 `_maybe_start_review` / `begin_review`；
细节见 [`feature_intents/postsession.md`](feature_intents/postsession.md)。

**`inspect-due`（主动查账）**：不看「哪一场」，而是把**过期还没做完**的任务
（`inspect-due 60` = 未来 60 分钟内到期的也算，`--all` = 不按截止时间筛）
拉进同一套状态机过一遍。过期任务还会出现在状态框的警告里，提示敲这条命令。

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
       └── stats.py ──> 只读公开 API（/v1/me + /v1/sessions 分块拉全量历史）→ 统计视图

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
import stats as st  # noqa: E402  生涯统计：只读 API 拉全量历史 + 算数字 + 缓存
import settings_store as cfg  # noqa: E402  设置项注册表 + data/settings.json
import omnifocus as of  # noqa: E402  OmniFocus：链接解析 + 读写（osascript 子进程）
import postsession as psv  # noqa: E402  session 结束 15s 后的收尾（纯逻辑）

BRIDGE_JS = os.path.join(HERE, "scripts", "bridge.mjs")

# 罪案区的全局锁：有未认罪的 sin 时，除了宣言什么都不受理。
# 上限只影响一轮循环里反复敲错的次数（敲错就继续鸣笛、继续锁）。
SIN_BEEP_LIMIT = 3

# 消息区（Recent）：屏幕上只显示最近 MESSAGE_SHOW 条 —— 屏幕永远只有「一帧」，
# 留一长串历史只会让帧越来越高、钳到屏高之后顶部残帧反而删不干净。
# 内部再多留几条（MESSAGE_KEEP），好让「敲错被记账」这类过程还能被测试和排查看到。
MESSAGE_SHOW = 3
MESSAGE_KEEP = 12

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
)

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
        self.stderr_path = os.path.join(TEMP, "warden-bridge.log")
        self.last_idle_close = None
        # 命令是串行的（bridge 侧一条条跑），所以这边也上锁：
        # 收尾的后台核对线程和主线程可能同时说话，协议是「一问一答」的，
        # 两边同时写 stdin 会让响应和请求号对不上。
        self._lock = threading.Lock()

    # -- 生命周期

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = dict(os.environ)
        if self.headed:
            env["WARDEN_BRIDGE_HEADED"] = "1"
        if self.idle_ms is not None:
            env["WARDEN_BRIDGE_IDLE_MS"] = str(self.idle_ms)
        if self.profile_dir:
            env["FOCUSMATE_PROFILE_DIR"] = self.profile_dir
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
                if f["event"] == "eof":
                    raise BridgeError("bridge 的 stdout 关了（看 %s）" % self.stderr_path)
                continue
            if f.get("id") != req_id:
                # 不该发生（命令是串行的），但别把响应丢了
                continue
            if not f.get("ok"):
                raise BridgeError(f.get("error") or "未知错误")
            return f.get("result") or {}

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
    return repl_kit.Hint("参数", ("<n>", "±MM"),
                         note="15 的倍数，不能跨天")


def _hint_mute(parts):
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("on", "off"))
    return repl_kit.Hint("参数", ("<n>", "on|off"),
                         note="不写 on/off 就是取反")


def _hint_title(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("参数", ('"文本"',), note='"" = 清空')
    return repl_kit.Hint("参数", ("<n>", '"文本"'))


def _hint_cancel(parts):
    return repl_kit.Hint("参数", ("<n>",), note="执行区要敲宣言")


def _task_command(w):
    """``task`` 那一条命令：动作、后缀、紧凑短选项、补全都写在一个地方。

    紧凑短选项走 repl_kit 的 ``short`` 表（``-an`` ≡ ``-a none``）：解析、补全、
    提示三件事都由框架统一办，这里只声明一次（:data:`TASK_SHORT_FLAGS`）。
    """
    return repl_kit.Command(
        name="task",
        handler=w.cmd_task,
        summary="任务 CRUD / 挂到 session / 同步完成时间（任何视图都能敲）",
        aliases=(),
        section="通用",
        usage="task <create|import|delete|modify|note|assign|select|swap|repeat|sync|archive>"
              " ... [-a <编号|none|current>] [-s <编号>] [-n 备注]",
        options=("create", "import", "delete", "modify", "note", "assign", "select",
                 "swap", "repeat", "sync", "archive", "inspect-due",
                 "--assign", "-a", "--note", "-n", "--from", "-s", "--repeat"),
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
    if words and words[-1] in ("--assign", "-a", "-an", "-ac"):
        return [str(i) for i in range(1, len(w.rows) + 1)] + ["none", "current"]
    if words and words[-1] in ("--from", "-s"):
        return [str(i) for i in range(1, len(w.rows) + 1)]
    return []


def mute_window_for(day, hhmm):
    """``("11:15", 这一天)`` 落在哪条静音窗里 → 记录；不在就是 None。

    口径只认 ``period mute``（:mod:`session_plan` 里那张表），别处不要自己算 ——
    表格的「静音」列和自动 quiet 都问这一个函数。
    （daemon 的分心看门狗**不**问它：静音只改 Focusmate 那一场，本地该响还响。）
    """
    got = sp.parse_hhmm(hhmm)
    if got is None:
        return None
    try:
        moment = datetime.datetime.combine(day, datetime.time(got[0], got[1]))
        return sp.mute_window_at(moment, data=mute_data())
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


def _hint_task(parts):
    # repl_kit 递进来的是**整行**的词（含命令名，含正在敲的那半个词），
    # 而这里只关心 `task` 后面的参数 —— 先把这个命令名摘掉。
    words = [str(part).lower() for part in parts]
    if words and words[0] == "task":
        words = words[1:]
    if not words:
        return repl_kit.Hint("动作", TASK_ACTION_WORDS,
                             note="后缀：-a <编号|none|current> · -s <编号> 批量 · -n 备注"
                                  " · 字段随便写")
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
    if head and head[0].isdigit():
        if len(head) == 1:
            return repl_kit.Hint("动作", ("swap", "duplicate", "drop", "done", "link", "-a <编号>"),
                                 note="swap 21 = 和 21 换位置 · -a none 下架 · "
                                      "-a current 挂到当前这一场")
        head = head[1:]
    action = head[0] if head else ""
    if action in ("import", "粘贴", "导入"):
        return repl_kit.Hint("参数", ("（不写）", "[标题](omnifocus:///task/PK)", "-a 3"),
                             note="不写参数 = 读剪贴板（⌘V 到终端只剩标题，这条不会丢链接）")
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
        return repl_kit.Hint("参数", ("<任务 ID>", "周几@HH:MM|off"),
                             note="每周那一天、那一场自动挂上")
    if action in ("note", "notes", "备注"):
        return repl_kit.Hint("参数", ("<任务 ID>", "<备注文本 / 网址>"),
                             note="备注会跟着完成归档一起显示（网址可点）")
    if action in ("delete", "select"):
        return repl_kit.Hint("参数", ("<任务 ID>",))
    if action in ("swap", "调换", "换位"):
        return repl_kit.Hint("参数", ("<任务 ID>", "<任务 ID>"),
                             note="两条任务互换位置（一条赋闲也行）：task 32 swap 21")
    if action == "modify":
        return repl_kit.Hint("参数", ("<任务 ID>", "<字段>", "<新值>"), note="字段不校验")
    if action == "assign":
        return repl_kit.Hint("参数", ("<session ID>",),
                             note="默认挂当前选中的任务；也可 assign <任务ID> <sessionID>")
    return repl_kit.Hint("参数", TASK_ACTION_WORDS,
                         note="任何动作都能带 -a <编号|none|current> / -n 备注；"
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
            _cmd("help", lambda a: w.cmd_help(), "命令列表", ("?", "h"), "通用",
                 usage="help", hint=_hint_actions("help", note="当前视图的全部命令")),
            _cmd("view", lambda a: w.cycle_view(),
                 "下一个视图", ("tab", "switch"), "通用",
                 usage="view", hint=_hint_actions("view", note="等同 Shift+Tab")),
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
        ]

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
            ("session", "period", "cancel", "mute", "title", "day", "sync", "help", "quit"),
            note="Tab 切下一个视图（专注 → 统计 → 任务）",
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
        # 契约表格下面那块任务表格（Recent 由框架接在后面）
        index = self.w.render_task_index()
        if index is not None:
            out.append(index)
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
            _cmd("relocate", w.cmd_relocate, "临时把 #N 提前/推后：relocate N ±MM",
                 usage="relocate <n> ±MM",
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
            _cmd("title", w.cmd_title, '把 #N 的标题改成文本：title N "文本"', ("t",),
                 usage='title <n> "文本"', hint=_hint_title),
            _cmd("cancel", w.cmd_cancel, "取消 #N（执行区要敲宣言）",
                 usage="cancel <n>", hint=_hint_cancel),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="结束后 15 秒 daemon 会自动拉你回来收尾")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
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

    def current_session_key(self):
        """当前进行中的 session key；没有就 None（模式会设为下一场默认）。"""
        sess = ks.current_session(self.w.entries, datetime.datetime.now())
        return ks.key_of(sess[0], sess[1]) if sess else None

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
        sess = ks.current_session(w.entries, now)

        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        t.add_row("目标", Text(w.current_goal()))
        if sess:
            day, slot, start, end = sess
            t.add_row("当前 session", Text(
                "%s   %s–%s（%smin）" % (ks.key_of(day, slot), start.strftime("%H:%M"),
                                         end.strftime("%H:%M"), slot["duration"]),
                style="bold green"))
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
        task delete <id>
        task modify <id> <field> <新值>
        task note <id> <备注文本|网址>      # 备注：完成之后照着去对应地方干活
        task assign <session ID>            # 默认当前选中的任务
        task assign <任务 ID> <session ID>  # 显式指定
        task select <id>
        task <id>                           # ↑ 同义：裸 ID 就是 select
        task <id> -a <session ID>           # 选中 + 挂上去（最省事）
        task <id> -an                       # 从它那一场下架（≡ -a none）
        task <id> -ac                       # 挂到当前这一场（≡ -a current）
        task 32 swap 21                     # 两条任务互换位置（赋闲 ↔ 有场）
        task -s 5 -a 6                      # 把 #5 上的全部任务转移到 #6
        task -s 5 -a none                   # 把 #5 上的全部任务下架
        task repeat <id> <周几@HH:MM|off>   # 每周重复（只影响那一天的那一场）
        task sync                           # GET /v1/tasks → 把 completedAt 归档
        task archive [N]                    # 看归档（data/task-archive.json）

    任何子命令都能带 ``--assign N``（表格里的 session ID）：命令跑完顺手挂上去，
    例如 ``task create title=写论文 --assign 3``、``task 8 -a 1``。
    ``-a`` 还有两个特殊取值：``none``（下架：删掉 Focusmate 那条 task，本地断绑定）
    和 ``current``（当前这一场，进行中的优先）；紧凑写法 ``-an`` / ``-ac`` 由
    repl_kit 的短选项表统一展开，补全和提示都跟着走。
    ``-s/--from N`` 是批量：那一场上的**全部**任务一起办事，和 ``-a`` 搭配就是
    「全量转移」，``-a none`` 就是「全量下架」。

    任务字段不校验：想记 due / priority / notes / 任何 key 都行。
    备注（``task note`` / ``note=`` / ``备注=``，统一存成 ``notes``）会跟着完成归档
    一起显示，里面塞网址就能在完成后直接点开去干活。
    给 session 挂任务时会**自动**走 bridge 在 Focusmate 那一场建同名 task，
    成功后才把 Focusmate 的 task id 记回本地，失败就什么都不写。
    """

    key = "4"
    name = "任务"
    label = "任务 / 归档"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("task create", "task 8 -a 1", "task 8 -an", "task 8 -ac",
                     "task 32 swap 21", "task -s 5 -a 6", "inspect-due", "review"),
            note="字段不校验 · task 8 ≡ task select 8 · -an 下架 / -ac 当前这一场"
                 " · Tab 切视图",
        )

    def status_line(self):
        return None

    def unknown_hint(self):
        return ("「任务」视图认 task create/delete/modify/assign/select/duplicate/"
                "drop/done/link/import/sync/swap、review、inspect-due"
                "（也可以直接写任务 ID，如 task 8 -a 1 / task 8 -an / "
                "task 32 swap 21 / task -s 5 -a 6）；"
                "Shift+Tab 切回去再干别的。")

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", lambda a: w.cmd_task_sync(quiet=False),
                 "拉 GET /v1/tasks，把已完成任务的 completedAt 归档",
                 ("r", "reload"), usage="refresh",
                 hint=_hint_actions("refresh", note="同步 Focusmate task 完成时间")),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="A 延期 / B 放弃 / C 重议 / D 补录 / E 推后")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
        ]

    def render(self):
        w = self.w
        tasks = w.task_store.list()
        selected = w.selected_task_id
        if not tasks:
            return Panel(
                Text("还没有任务。\n"
                     "  task create title=写论文 due=明天\n"
                     "  task create 3 title=复习数学          ← 显式 ID\n"
                     "  task modify 3 due 后天\n"
                     "  task note 3 材料在这 https://example.com/x   ← 完成后会显示（可点）\n"
                     "  task assign 2                        ← 挂到表格里的 #2 session\n"
                     "  task 3 -a 2                          ← 同上（裸 ID = select）\n"
                     "  task create title=明日时间安排 --repeat mon@22:15"
                     "   ← 每周一 22:15 那场自动挂上\n"
                     "  inspect-due                          ← 主动看「过期还没做完」的任务",
                     style="dim"),
                title="[bold]Task · 任务[/]", border_style="magenta", box=box.ROUNDED)

        # due / due_source 有专门的「截止」列，别在右边再列一遍
        hidden = {"due", "due_source"}
        keys = []
        for t in tasks:
            for k in (t.get("fields") or {}):
                if k not in keys and k not in hidden:
                    keys.append(k)
        t = Table(title="Task · 本地 JSON 任务库", box=box.SIMPLE_HEAVY,
                  header_style="bold magenta", title_justify="left", expand=False)
        t.add_column("ID", justify="right", style="bold", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("重复", no_wrap=True)
        t.add_column("session", no_wrap=True)
        t.add_column("截止", no_wrap=True)
        t.add_column("Focusmate", no_wrap=True)
        t.add_column("OmniFocus", no_wrap=True)
        t.add_column("完成时间", no_wrap=True)
        t.add_column("标签", no_wrap=True, overflow="crop")
        for k in keys:
            t.add_column(str(k), no_wrap=True, overflow="crop")
        if not keys:
            t.add_column("内容", no_wrap=True, overflow="crop")
        status_style = {ts.STATUS_COMPLETED: STYLE_DONE, ts.STATUS_DROPPED: STYLE_DROPPED}
        for task in tasks:
            status = w.task_store.status_of(task)
            style = "bold reverse" if task.get("id") == selected else ""
            if status != ts.STATUS_ACTIVE:
                style = status_style.get(status, "") + (" reverse" if task.get("id") == selected else "")
            sess = task.get("session") or {}
            sess_txt = ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?"
                                  )) if sess else "—"
            due = w.omni_due_of(task)
            due_txt = due.replace("T", " ")[:16] if due else "—"
            fm = task.get("focusmate") or {}
            fm_txt = str(fm.get("task_id") or "")[:8] or "—"
            omni_id = ts.TaskStore.omnifocus_id(task)
            omni_txt = str(omni_id)[:12] if omni_id else "—"
            done = fm.get("completed_at") or task.get("status_at") or "—"
            if isinstance(done, str) and len(done) > 16:
                done = done.replace("T", " ")[:16]
            vals = []
            if keys:
                for k in keys:
                    vals.append(w.task_store._one_line((task.get("fields") or {}).get(k)))
            else:
                vals.append(w.task_store.summarize(task))
            rep = task.get("repeat") or {}
            rep_txt = ("🔁 %s@%s" % (rep.get("weekday_label"), rep.get("start"))
                       if rep else "—")
            status_txt = "%s %s" % (w.task_mark(task) or "·", status)
            tags = task.get("tags") or []
            t.add_row(str(task["id"]),
                      Text(status_txt, style=status_style.get(status, "")),
                      Text(rep_txt, style="magenta" if rep else STYLE_MUTED),
                      Text(sess_txt, style=style),
                      Text(due_txt, style=STYLE_MUTED if due else style),
                      Text(fm_txt, style=style),
                      Text(omni_txt, style="cyan" if omni_id else STYLE_MUTED),
                      Text(done, style=style),
                      Text(" · ".join(tags) if tags else "—",
                           style="yellow" if tags else STYLE_MUTED),
                      *[Text(v, style=style) for v in vals])

        # 收尾面板在 repl_kit 的打断栏位里（见 render_interrogate），不在这儿。
        out = [t]
        arch = w.task_store.load_archive().get("records") or []
        at = Table.grid(padding=(0, 1))
        at.add_column(style="dim", no_wrap=True)
        at.add_column(no_wrap=True)
        at.add_column(no_wrap=True, overflow="crop")
        at.add_column(no_wrap=True, overflow="crop")
        if not arch:
            at.add_row("", Text("（还没有完成归档；task sync 会读 GET /v1/tasks 的 "
                                "completedAt）", style="dim"))
        for rec in arch[-8:]:
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
        super().__init__(*args, **kwargs)
        self.frame_rev = 0                 # 状态变化计数：变了就必须重排版
        self.frame_key_hook = None         # (() -> tuple)：业务侧补充的帧键
        self._frame_memo = None            # (key, ansi_text)
        self._ansi_memo = None             # (ansi_text, chunks)

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
               self._terminal_rows(), bool(self.show_warnings)]
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

    def _build_frame_text(self):
        """真正排版一次（贵：rich 要测量 / 换行 / 裁切整块看板）。"""
        raw = super().frame_text()
        if not raw:
            return raw
        reserved = 1                                  # 提示行
        # 老版本 repl_kit 没有这个属性时，PromptSession 仍会预留 8 行。
        reserved += max(0, getattr(self, "reserve_space_for_menu", 8))
        try:
            has_status = bool(self.status_line())
        except Exception:                             # 状态行坏了也不该撑破布局
            has_status = False
        if has_status:
            reserved += 1                             # 状态行（专注视图）
        reserved += 1                                 # 提示符
        budget = max(1, self._terminal_rows() - reserved)
        lines = raw.splitlines()
        if len(lines) > budget:
            # 只保留最后 budget 行：顶部滚走，底部（含提示）留下。
            raw = "\n".join(lines[-budget:]) + "\n"
        return raw

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


class Warden(object):
    def __init__(self, console, bridge, no_clear=False, offline=False, day_offset=0):
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
        # 静音时段（period mute）自动落地：mutes_at 是节流时间戳，
        # mute_tries 记「这一场刚才试过、几分钟内别再试」，mute_worker 是后台线程。
        self.mutes_at = 0.0
        self.mute_tries = {}
        self.mute_worker = None
        # 「现在」是几点：None = 跟着墙上时钟走。测试/回放可以钉死它，
        # 否则「3 小时内才自动催」这条规则会让用例在钟点变了之后莫名其妙地红。
        self.review_now = None
        self._snapshot_cache = {}
        self._last_line = ""
        # 警告板：框架负责画在「状态」框里，这里只负责"有什么问题"。
        # 每次操作 / 每 5 秒重算一次（见 refresh_warnings）。
        self.warnings = repl_kit.WarningBoard()
        self.warnings_at = 0.0
        self.warnings_sig = ()
        # 重算 data.csv 时被"计划里没有"冲掉的行（例如手写的旧契约）：
        # 记下来当警告显示，别让人以为契约凭空消失了。
        self.csv_dropped = []
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
        )
        # 看板自己就是最下面那块内容；补全菜单浮在它上面，不再在输入行
        # 下面留 8 行空白（留了就会把看板/提示挤出屏幕）。
        # 老版本 repl_kit 没有这个参数就退回默认值（WardenShell 会按 8 行算）。
        # 注意要问 repl_kit.ReplShell（WardenShell 自己只是 *args/**kwargs 透传）。
        if "reserve_space_for_menu" in inspect.signature(repl_kit.ReplShell).parameters:
            shell_kwargs["reserve_space_for_menu"] = 0
        self.shell = WardenShell(**shell_kwargs)
        # 画帧缓存要认识「业务状态变没变」：把签名挂上去（见 frame_signature）。
        self.shell.frame_key_hook = self.frame_signature

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
            return (id(self.review_check), bool(self.review_check))
        return (
            id(review), review.stage, review.index, review.closed,
            len(review.notes), len(review.skipped),
            (review.pending_task or {}).get("id"),
        )

    def frame_signature(self):
        """画帧缓存要看的那些「便宜的事实」（贵的东西留在 key 之外）。

        只放**不求值就便宜**的东西：长度、身份、指纹、警告签名。任何一项变了
        就重排版。`messages` 取最后一条（带秒级时间戳），所以 Recent 不会滞后。
        """
        msgs = self.messages or ()
        return (
            len(msgs), msgs[-1] if msgs else (),
            len(self.rows), id(self.rows),
            self.rows_note or "", self.rows_source or "",
            self.bridge_note or "", bool(self.had_error),
            self.csv_fp, len(self.entries),
            self.review_signature(),
            self.sel_date.isoformat(), self.day_offset,
            id(getattr(self, "stat", None)),
        )

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
        taken = {t.get("id") for t in tasks}
        sessions = self.sessions_for_day_plan(day)
        for task in self.task_store.repeated_tasks(day.weekday()):
            if task.get("id") in taken:
                continue
            hit = self.task_store.repeat_occurrence(task, day, sessions)
            if not hit or hit.get("start") != start:
                continue
            if duration is not None and hit.get("duration") is not None \
                    and int(hit["duration"]) != int(duration):
                continue
            tasks.append(task)
        return tasks

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
        window = mute_window_for(day, row.get("startLabel"))
        row["period_muted"] = bool(window)
        if window:
            row["mute_window"] = dict(window)
        tasks = self.tasks_for_session(day, row.get("startLabel"), row.get("durationMin"))
        if tasks:
            row["task"] = tasks[0]                # 兼容：只关心一条的地方仍然能用
            row["tasks"] = tasks
            row["task_repeat"] = any(t.get("repeat") for t in tasks)
            row["task_text"] = self.task_cell_text(tasks)
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

    def task_cell_text(self, tasks):
        """契约表格 task 列的正文：``1: 任务… | ✓3: 任务…``（编号 + 裁剪过的标题）。

        标题优先取 title/name 字段（`focusmate_title`），没有才退回整条摘要 ——
        编号 + 一长串 ``1. … 2. …`` 在表格里没法看。前面的记号见 :meth:`task_mark`。
        """
        parts = []
        for task in tasks or []:
            title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
            prefix = self.task_mark(task)
            if task.get("repeat"):
                prefix += "🔁"
            parts.append("%s%d: %s" % (prefix, task.get("id"), title))
        return " | ".join(parts)

    def frame_width(self):
        """这一帧能占多少列（= 终端宽度，和 repl_kit 排「状态」框用的是同一个数）。

        抽成一个方法是为了测试能钉住它：「几列并排」「哪几行被裁」这种断言不能
        跟着「现在窗口开多大」乱跳。
        """
        width = getattr(self.console, "width", None)
        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:                             # noqa: BLE001
            cols = width if isinstance(width, int) else 80
        if not isinstance(cols, int) or cols < 24:
            cols = width if isinstance(width, int) and width >= 24 else 80
        return cols

    def task_panel_tasks(self, day=None):
        """这一天任务面板里该出现哪些任务 —— 用户定的两条规矩。

        * **今天（以及以后）**：所有没做完的 + 今天被处理过的。
        * **过去那天**：固化，只剩「那一天的 session 里被处理过的」。

        「被处理过」不挑结果：放弃 / 完成 / 补录 / 重议 / 推后 / 转移走 / 跳过
        都算一笔，痕迹记在 ``task["history"]``（``TaskStore.mark_processed``）。
        跳过也要记：那一天你问过它、你说先放着 —— 这是那一天发生过的事。
        """
        day = day or self.sel_date
        is_today_or_later = day >= datetime.date.today()
        open_tasks = []
        touched = []
        for task in self.task_store.list():
            records = self.task_store.processed_on(task, day.isoformat())
            if self.task_store.is_open(task):
                # 没做完的一律挂在「今天」的面板上；过去那天只有「问过但没收掉」才留
                if is_today_or_later or records:
                    open_tasks.append(task)
            elif records:
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

    def task_index_title(self, task):
        """任务面板里那一格的字：记号 + （🔁）+ 裁过的标题。"""
        title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
        prefix = self.task_mark(task)
        if task.get("repeat"):
            prefix += "🔁"
        return prefix + title

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

        内容按天决定（见 :meth:`task_panel_tasks`）：今天 = 没做完的 + 今天处理过的，
        过去那天 = 只有那天处理过的。**顺序**见 :meth:`task_panel_order`：先按状态
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
        scope = ("今天 = 没做完的 + 今天处理过的"
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
        return [Text(str(task.get("id")), style=style),
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

    def emit(self, renderable=None):
        """帧外打印：帮助面板 / 宣言闸门 / `>>>` 回显。

        命令输出发生在两次提示符之间，所以直接 print 就行 —— 擦除和重画
        归 prompt_toolkit 管，不用再手算「这一帧占几行」。
        """
        if renderable is None:
            self.console.print()
        else:
            self.console.print(renderable)

    # -- 看板（框架把它画在提示行上方）---------------------------------

    def frame(self):
        """当前视图的整块看板。repl_kit 每帧调它一次，按单元格 diff 上屏。"""
        # 每次画之前重读 data.csv：契约可能在别处被人手改了（这就是控制面）
        self.reload_entries()
        self.refresh_warnings()
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
        """一行「当前在第几个视图」，带切换键提示。视图多了自动跟着排。"""
        bar = Text()
        bar.append("视图 ", style="dim")
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
        """拉生涯统计：stats.load 走缓存 + 增量刷新；失败不掀 TUI。

        - 默认：TTL 内直接用缓存，不打网络；过期只拉缺口 + 最近几天。
        - force：立刻增量刷新（`stat refresh`）。
        - full：丢掉缓存全量重拉（`stat refresh full`，很少需要）。
        """
        try:
            history, note = st.load(force=force, full=full)
        except Exception as e:
            self.had_error = True
            self.stat_note = "拉取失败：%s: %s" % (type(e).__name__, e)
            self.say("生涯统计拉取失败：%s" % e, "error")
            return False
        try:
            self.stat = st.compute_stats(history)
        except Exception as e:                      # 数据变形的兜底
            self.had_error = True
            self.stat_note = "数据没法算：%s" % e
            self.say("生涯统计算不动：%s" % e, "error")
            return False
        self.stat_note = note
        self.stat_at = datetime.datetime.now()
        self.say("生涯统计：%s（完成 %d 场 / %d 天）"
                 % (note, self.stat["total_completed"], self.stat["days"]),
                 "ok" if history else "warn")
        return True

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

    def refresh(self, quiet=False, timeout=180):
        """拉一次**选中那一天**的 session，并和 data.csv 的契约合并成表格行。

        失败时**不要**拿退化快照去覆盖已有好数据 —— 快照里没有 session id，
        覆盖了后面 mute/title/cancel 全都做不了，还会让人困惑。
        """
        self.reload_entries()
        day = self.sel_date
        if self.offline:
            if not self.rows:
                self._fallback_rows("离线模式")
            return False
        try:
            with self.console.status("[yellow]问 Focusmate 要 %s 的 session…[/]" % day.isoformat(),
                                     spinner="dots"):
                res = self.bridge.call("snapshot", timeout=timeout, date=day.isoformat())
        except BridgeError as e:
            self.had_error = True
            if self.rows and self.rows_source == "bridge":
                self.say("拉列表失败（表格保持上一次的结果）：%s" % e, "error")
            else:
                self._fallback_rows("bridge 出错")
                self.say("拉列表失败：%s" % e, "error")
            return False

        now_ms = res.get("now") or int(time.time() * 1000)
        api_rows = []
        for s in res.get("sessions") or []:
            row = dict(s)
            row["phase"] = self.phase_of(s["startMs"], s["endMs"], now_ms)
            api_rows.append(row)
        self.api_rows = api_rows
        self.rows_source = "bridge"
        self.rows_note = ""
        self.rows_at = time.time()
        self.rebuild_rows(now_ms)
        if not quiet:
            self.say("拉到 %d 场（%s）" % (len(self.rows), res.get("date")))
        return True

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
        title = "The Contract Table · %s %s" % (day.isoformat(), sch.cn_weekday_of_day(day))
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

        if not self.rows:
            empty = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                          title_justify="left", expand=False)
            empty.add_column("状态")
            empty.add_row("（这天上既没有契约、也没有 session）")
            return empty
        t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        t.add_column("时段 ID", no_wrap=True)
        t.add_column("时段", no_wrap=True)
        t.add_column("时段类型", no_wrap=True)
        t.add_column("时段名称", no_wrap=True, overflow="crop")
        t.add_column("ID", justify="right", style="bold", no_wrap=True)
        t.add_column("时间", no_wrap=True)
        t.add_column("时长", justify="right", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("静音", justify="center", no_wrap=True)
        t.add_column("契约", no_wrap=True)
        t.add_column("标题", overflow="crop", no_wrap=True)
        t.add_column("task", no_wrap=True, overflow="crop")

        live_idx = None
        for i, r in enumerate(self.rows):
            if r["phase"] == "进行中":
                live_idx = i + 1
                break
        zone_tag = {"locked": ("锁", "bold red"), "draft": ("草", "bold green"),
                    "past": ("过", STYLE_MUTED)}
        for i, r in enumerate(self.rows):
            idx = i + 1
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
            task_cell = Text(task_text, style="bold green" if task_text else STYLE_MUTED)
            t.add_row(
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
            )
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
        rows.append((sch.data_csv_path(), "dim"))
        return rows

    # -- 警告（repl_kit.WarningBoard）-----------------------------------

    def sessions_for_day_plan(self, day=None):
        """那一天**排好的** session（时段 + 一次性；不含 API、不含长期规则展开）。"""
        day = day or self.sel_date
        return sp.sessions_for_day(self.download, self.layouts, day)

    def repeated_task_for(self, day, session):
        """这一场有没有落上「每周重复任务」；没有就 None。"""
        sessions = self.sessions_for_day_plan(day)
        for task in self.task_store.repeated_tasks(day.weekday()):
            hit = self.task_store.repeat_occurrence(task, day, sessions)
            if hit and hit.get("start") == session.get("start"):
                return task
        return None

    def _suggest_repeat_spec(self, day=None, sessions=None):
        """建议的 --repeat 取值：那天最后一场的时间（没有就 22:15）。"""
        day = day or datetime.date.today()
        sessions = sessions if sessions is not None else self.sessions_for_day_plan(day)
        start = sessions[-1]["start"] if sessions else "22:15"
        return "%s@%s" % (cfg.WEEKDAYS[day.weekday()], start)

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
                hint="敲 inspect-due 逐个过一遍（A 延期 / B 放弃 / C 重议 / D 补录 / E 推后）"))
        # 4) 每周重复任务：今天有没有落上
        today = datetime.date.today()
        today_sessions = self.sessions_for_day_plan(today)
        landed = []
        if today_sessions:
            for task in self.task_store.repeated_tasks(today.weekday()):
                if self.task_store.repeat_occurrence(task, today, today_sessions):
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
                "今天的 session 里没有「明日时间安排」这类每周重复任务",
                level="warn", source="重复任务",
                hint="task create title=明日时间安排 --repeat %s"
                     % self._suggest_repeat_spec(today, today_sessions)))
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
            self._maybe_apply_mutes()
        except Exception:                          # noqa: BLE001 - 静音坏了也不能没帧
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

        now = datetime.datetime.now()
        for key in keys:
            try:
                day = datetime.date.fromisoformat(key[:10])
                hhmm = key[11:]
                got = sch.parse_hhmm(hhmm)
            except ValueError:
                continue
            if not got:
                continue
            start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
            if start <= now:
                self.say("  %s 已经过去了，只判放弃，不动 API。" % key, "info")
                continue
            # 已经被 daemon 重订回来了吗？有就再取消掉
            try:
                res = self.bridge.call("snapshot", timeout=180, date=day.isoformat())
            except BridgeError as e:
                self.had_error = True
                self.say("  %s 查不到现状（%s），你自己去 Focusmate 看一眼。" % (key, e), "error")
                continue
            hit = None
            for s in res.get("sessions") or []:
                if s.get("startLabel") == hhmm:
                    hit = s
                    break
            if not hit:
                self.say("  %s 现在 API 里没有（daemon 还没重订回来）。" % key, "info")
                continue
            with self.console.status("[red]取消 %s…[/]" % key, spinner="dots"):
                out = self.bridge.call("cancel", startMs=hit["startMs"], meetingId=hit["id"])
            if out.get("verified"):
                self.say("  ✅ %s 已真的取消（API 复核：没了）" % key, "ok")
            else:
                self.had_error = True
                self.say("  ❌ %s 取消后复核：它还在" % key, "error")
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
        self.say("#%d %s–%s quiet %s…" % (idx, row["startLabel"], row.get("endLabel") or "?",
                                          verb), "cmd")
        with self.console.status("[yellow]改 quiet mode…[/]", spinner="dots"):
            res = self.bridge.call("mute", startMs=row["startMs"], meetingId=row["id"], quiet=want)
        if res.get("verified"):
            self.say("✅ #%d quiet = %s（API 复核一致）" % (idx, "ON" if res["quiet"] else "off"), "ok")
        else:
            self.had_error = True
            self.say("❌ #%d 改完复核不一致：现在是 %s" % (idx, res.get("quiet")), "error")
        self.refresh(quiet=True)
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
        self.say("#%d 改标题为 %r…" % (idx, title), "cmd")
        with self.console.status("[yellow]写标题…[/]", spinner="dots"):
            res = self.bridge.call("title", startMs=row["startMs"], meetingId=row["id"], title=title)
        if res.get("verified"):
            self.say("✅ #%d 标题已写入（API 复核一致）" % idx, "ok")
        else:
            self.had_error = True
            self.say("❌ #%d 写完复核不一致：现在是 %r" % (idx, res.get("actual")), "error")
        self.refresh(quiet=True)
        return True

    def cmd_cancel(self, args):
        """取消。摩擦层由**这一行**在哪个区决定：草稿区零阻力，执行区敲宣言。"""
        if not args:
            raise ValueError("用法：cancel N")
        idx, row = self.pick(args[0])
        zone = row.get("zone")

        if zone == "past":
            raise ValueError("#%d 已经开始（%s）—— 过去的场次不取消也不补订。"
                             % (idx, row["startLabel"]))

        if zone == "draft":
            return self._drop_contract_row(idx, row)

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

        key = sch.key_of(self.sel_date, row["startLabel"])
        ks.mark_abandoned(key, why="walk-of-shame",
                          note="TUI 里敲完宣言后放弃（#%d %s）" % (idx, row["startLabel"]))
        self.say("已判 %s 为「已放弃」：daemon 不会再重订这一场。" % key, "ok")

        if row.get("id") is None:
            self.say("它本来就没订上（契约缺的那一场），不用动 API。", "info")
            self.refresh(quiet=True)
            return True

        self.say("#%d 取消中…（%s）" % (idx, row["startLabel"]), "cmd")
        with self.console.status("[red]取消中…[/]", spinner="dots"):
            res = self.bridge.call("cancel", startMs=row["startMs"], meetingId=row["id"])
        if res.get("verified"):
            self.say("✅ #%d %s 已取消（API 复核：该时段已消失）" % (idx, row["startLabel"]), "ok")
        else:
            self.had_error = True
            self.say("❌ #%d 取消后复核：该 session 还在" % idx, "error")
        self.refresh(quiet=True)
        return True

    def cmd_relocate(self, args):
        """relocate N ±MM —— 把当前表里 #N 临时提前/推后 MM 分钟。

        提前：直接改临时计划并取消原 API session，不需要宣言（这是在往前赶）。
        推后：同一天只能推一次，且不能跨天；防止把拖延变成日常操作。
        **不写 data.csv**：只影响所选这一天的这一场，未来同星期几照旧。
        """
        if not args:
            raise ValueError("用法：relocate N ±MM（MM 是 15 的倍数，如 relocate 3 -15）")
        idx_token, delta = parse_relocate_spec(args)
        if idx_token is None:
            raise ValueError("用法：relocate N ±MM（MM 是 15 的倍数，如 relocate 3 -15）")
        idx, row = self.pick(idx_token)

        if row.get("zone") == "past" or row.get("phase") == "已结束":
            raise ValueError("#%d 已经开始（%s）—— 过去的场次不能挪。" % (idx, row["startLabel"]))
        if not row.get("planned"):
            raise ValueError("#%d 是计划外 session（data.csv 里没有对应契约），relocate 改不了它。"
                             % idx)

        day = self.sel_date
        rec = row.get("relocation") or {}
        from_hhmm = rec.get("from") or row.get("relocated_from") or row["startLabel"]
        cur_hhmm = row["startLabel"]
        dur = row.get("durationMin") or rec.get("duration")
        if not dur:
            raise ValueError("#%d 看不出时长（快照？），先 refresh 成功一次再挪。" % idx)
        dur = str(int(dur))

        got = sch.parse_hhmm(cur_hhmm)
        if got is None:
            raise ValueError("当前行时间 %r 不是合法 HH:MM" % cur_hhmm)
        cur_start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        target = cur_start + datetime.timedelta(minutes=delta)
        if target.date() != day:
            raise ValueError("不能跨天移动：%s %s 挪 %+d 分钟会跑到 %s。"
                             % (day.isoformat(), cur_hhmm, delta, target.date().isoformat()))
        target_hhmm = target.strftime("%H:%M")

        if delta > 0 and sch.get_later_move(day):
            raise ValueError("%s 已经往后挪过一次了；一天只能往后挪一次。"
                             % day.isoformat())

        # 目标时间撞车：契约里不能撞，API 里也不能撞（原 session 自己不算）。
        for slot in sch.day_slots(self.entries, day):
            if slot["start"] == cur_hhmm:
                continue
            if slot["start"] == target_hhmm:
                raise ValueError("目标时间 %s 已经有一场契约；不能撞车。" % target_hhmm)
        for r in getattr(self, "api_rows", []):
            if r.get("startLabel") == target_hhmm and r.get("id") != row.get("id"):
                raise ValueError("目标时间 %s API 里已经有一场 session；不能撞车。" % target_hhmm)

        old_offset = int(rec.get("offset") or 0)
        new_offset = old_offset + delta
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        key = sch.key_of(day, from_hhmm)

        # 先落临时挪动记录；原 session 取消失败就回滚，保证「取消+挪动」不会只做一半。
        before = sch.load_relocations()
        after = copy.deepcopy(before)
        after.setdefault("relocations", {})[key] = {
            "kind": "temporary",
            "date": day.isoformat(),
            "from": from_hhmm,
            "to": target_hhmm,
            "duration": dur,
            "offset": new_offset,
            "at": stamp,
        }
        if delta > 0:
            after.setdefault("later_moves", {})[day.isoformat()] = {
                "key": key,
                "from": from_hhmm,
                "to": target_hhmm,
                "at": stamp,
            }
        sch.save_relocations(after)

        if row.get("id") is not None:
            self.say("#%d 取消原 session %s…" % (idx, cur_hhmm), "cmd")
            try:
                with self.console.status("[red]取消原 session…[/]", spinner="dots"):
                    res = self.bridge.call("cancel", startMs=row["startMs"], meetingId=row["id"])
            except BridgeError:
                sch.save_relocations(before)
                raise
            if not res.get("verified"):
                sch.save_relocations(before)
                self.had_error = True
                self.say("❌ #%d 原 session 取消后复核还在；临时挪动已回滚。" % idx, "error")
                return False
            # 本地表也把旧 session 拿掉，离线/测试环境不用等下一次 refresh 才看不到它。
            self.api_rows = [r for r in self.api_rows
                             if not (r.get("id") == row.get("id")
                                     and r.get("startMs") == row.get("startMs"))]

        moved = "提前" if delta < 0 else "推后"
        self.say("✅ #%d %s %d 分钟：%s → %s（临时，不改 data.csv；原时间 %s）。"
                 % (idx, moved, abs(delta), cur_hhmm, target_hhmm, from_hhmm), "ok")

        # 执行区里的场次立刻走一轮 check：data.csv 原 slot 已被临时记录替换，
        # check 会去订新时间；草稿区还没进窗口，等 daemon 到点再说。
        if row.get("zone") != "draft":
            self._release_browser("daemon 下单要独占同一个 Chromium profile")
            self.say("立刻触发一轮 check（keep_session.py check）…", "cmd")
            rc, out, err = run_keep_session("check", timeout=600)
            for line in (out or "").splitlines()[-20:]:
                self.say(line)
            if rc == 0:
                self.say("✅ check 跑完。", "ok")
            else:
                self.had_error = True
                self.say("❌ check 退出码 %d%s（临时计划已落盘，daemon 后续还会补）"
                         % (rc, ("：" + err) if err else ""), "error")

        self.reload_entries()
        self.rebuild_rows()
        if not self.offline:
            self.refresh(quiet=True)
        return True

    # -- 任务系统 ------------------------------------------------------

    def cmd_task(self, args):
        """TaskView 的唯一入口：task <create|delete|modify|assign|select|repeat|sync|archive> ...

        裸任务 ID 就是 select（``task 8`` ≡ ``task select 8``），所以最短的挂法是
        ``task 8 -a 1``。

        任何子命令后面都可以跟三个后缀（都能组合）：
          ``--assign <session ID>``（``-a`` 同义）= 顺手挂到那一场
          ``--note <备注>``（``-n`` 同义）= 顺手写备注（可以塞网址）
          ``--from <session ID>``（``-s`` 同义）= 批量：那一场上的**全部**任务

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
        if source is not None:
            # `-s` 本身就是动作（批量），后面不该再有子命令：``task -s 5 -a 6``。
            if kept:
                raise ValueError("`-s/--from` 是批量后缀，后面不要再跟动作（%s）；"
                                 "写成 `task -s 5 -a 6` 这样。" % kept[0])
            return self._apply_bulk(source, assign_to)
        if not kept:
            raise ValueError("task 后面要跟动作（create/delete/modify/assign/select/repeat/sync）")
        action = kept[0].lower()
        rest = kept[1:]
        result = self._dispatch_task(action, rest)
        if note_text is not None:
            self._apply_note(result, note_text, action)
        if assign_to is not None:
            self._apply_assign(result, assign_to, action)
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
        self.say("✅ 任务 %d 备注：%s"
                 % (task["id"], self.task_store.notes(task) or "（空）"), "ok")
        return task

    def _apply_assign(self, result, session_token, action):
        """``--assign`` 的收尾：拿到任务就挂，拿不到就说一声（不改任何东西）。"""
        task_id = None
        if isinstance(result, dict) and result.get("id") is not None:
            task_id = result["id"]
        elif isinstance(result, (int, str)) and str(result).isdigit():
            task_id = int(result)
        if task_id is None:
            self.say("`--assign` 只对会产出任务的动作用得上（create/modify/select）；"
                     "%s 没有可挂的任务，忽略。" % action, "warn")
            return None
        self.selected_task_id = task_id
        return self.cmd_task_assign([str(task_id), str(session_token)])

    def _dispatch_task(self, action, rest):
        # `task 3 duplicate -a 1`：动作写在 ID 后面（需求里的语法就是这个顺序）
        if action.isdigit() and rest:
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
            return self.cmd_task_import(rest)
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
        # 裸任务 ID 就是 select：`task 8` ≡ `task select 8`，
        # 于是 `task 8 -a 1` = 「选中任务 8 再挂到表格 #1」。
        if action.isdigit():
            if rest:
                raise ValueError("`task %s` 就等于 `task select %s`，后面不用再跟东西；"
                                 "要挂到 session 用 -a/--assign（例如 task %s -a %s）。"
                                 % (action, action, action, rest[0]))
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
        self.say("✅ 任务 %d 已创建（字段：%s）"
                 % (task["id"], self.task_store.summarize(task) or "空"), "ok")
        return task

    def cmd_task_repeat(self, args, quiet=False):
        """task repeat <id> <周几@HH:MM|off> —— 设置 / 取消每周重复。

        不写时刻时用今天最后一场的时间当锚点；duration 取那场 session 的时长，
        所以它只在「同一时刻、同一时长」的 session 上落地。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task repeat <任务 ID> <周几@HH:MM|off>")
        task = self.task_store.get(toks[0])
        if len(toks) == 1:
            rep = task.get("repeat")
            if not rep:
                self.say("任务 %d 不是重复任务。" % task["id"], "info")
            else:
                self.say("任务 %d 每周 %s %s（%s min；落点 %d 次）"
                         % (task["id"], rep.get("weekday_label"), rep.get("start"),
                            rep.get("duration") or "?", len(rep.get("occurrences") or {})),
                         "info")
            return task
        spec = toks[1].lower()
        if spec in ("off", "none", "clear", "cancel"):
            task = self.task_store.clear_repeat(task["id"])
            self.say("✅ 任务 %d 的每周重复已取消。" % task["id"], "ok")
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
            self.say("✅ 任务 %d 每周 %s %s 重复（锚定 %dmin 那场）"
                     % (task["id"], parsed["weekday_label"], parsed["start"], duration), "ok")
        return task

    def cmd_task_note(self, args):
        """task note <任务 ID> <备注文本>；不写文本 = 清空。

        备注可以是网址（完成之后照着去对应地方把活干完），也可以是任何话。
        """
        if not args:
            raise ValueError("用法：task note <任务 ID> <备注文本>（不写文本 = 清空）")
        text = " ".join(str(x) for x in args[1:]).strip()
        task = self.task_store.set_notes(args[0], text)
        self.say("✅ 任务 %d 备注%s：%s"
                 % (task["id"], "已清空" if not text else "",
                    self.task_store.notes(task) or "（空）"), "ok")
        return task

    def cmd_task_delete(self, args):
        if not args:
            raise ValueError("用法：task delete <任务 ID>")
        task = self.task_store.delete(args[0])
        if self.selected_task_id == task["id"]:
            self.selected_task_id = None
        self.say("✅ 已删除任务 %d：%s" % (task["id"], self.task_store.summarize(task)), "ok")
        return True

    def cmd_task_modify(self, args):
        if len(args) < 3:
            raise ValueError("用法：task modify <任务 ID> <字段> <新值>")
        task_id, field = args[0], args[1]
        value = " ".join(str(x) for x in args[2:])
        task = self.task_store.modify(task_id, field, value)
        self.say("✅ 任务 %d.%s = %r" % (task["id"], field, value), "ok")
        return task

    def cmd_task_select(self, args):
        if not args:
            raise ValueError("用法：task select <任务 ID>")
        task = self.task_store.get(args[0])
        self.selected_task_id = task["id"]
        self.say("已选中任务 %d：%s" % (task["id"], self.task_store.summarize(task)), "cmd")
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
            raise ValueError("用法：task <任务 ID> duplicate [-a <session ID>]")
        task = self.task_store.get(toks[0])
        copy = self.task_store.duplicate(task["id"])
        self.selected_task_id = copy["id"]
        self.say("✅ 任务 %d「%s」→ 复制成 #%d（同一条 OmniFocus：%s）"
                 % (task["id"], self.task_store.focusmate_title(task, 40), copy["id"],
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
            raise ValueError("用法：task <任务 ID> drop [--local]")
        task = self.task_store.get(toks[0])
        note = "" if local_only else self.omni_drop(task)
        task = self.task_store.set_status(task["id"], ts.STATUS_DROPPED, reason="cmd-drop")
        self.say("✗ 任务 %d「%s」已放弃%s"
                 % (task["id"], self.task_store.focusmate_title(task, 40),
                    ("；" + note) if note else "（只改了本地）"), "ok")
        return task

    def cmd_task_done(self, args):
        """``task <id> done [分钟]`` —— 完成：本地完成 + Focusmate 勾上 + OmniFocus 写回。

        不给分钟数也行；给了就记成实际耗时（和收尾里「缺漏完成补充」同一套）。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：task <任务 ID> done [实际分钟数]")
        task = self.task_store.get(toks[0])
        minutes = None
        if len(toks) > 1:
            try:
                minutes = int(toks[1])
            except ValueError:
                raise ValueError("分钟数得是整数；你给的是 %r" % toks[1]) from None
        task = self.task_store.complete(task["id"], minutes=minutes, reason="cmd-done")
        self.task_store.archive_local(self.task_store.get(task["id"]))
        note_fm = self.fm_complete(task)
        note_of = self.omni_complete(task)
        self.say("✓ 任务 %d「%s」完成%s%s%s"
                 % (task["id"], self.task_store.focusmate_title(task, 40),
                    ("（%d 分钟）" % minutes) if minutes else "",
                    ("；" + note_fm) if note_fm else "",
                    ("；" + note_of) if note_of else ""), "ok")
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
        task_id = toks[0]
        rest = toks[1:]
        auto = not rest
        if auto:
            items = [item for item in of.clipboard_links() if item.get("id")]
            if not items:
                raise ValueError("剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本 "
                                 "都读了）。在 OmniFocus 里 ⌘C 那条任务再来。")
            if len(items) > 1:
                raise ValueError("剪贴板里有 %d 条 OmniFocus 链接：%s —— 只拷一条再来，"
                                 "或者把要的那条的链接直接写出来。"
                                 % (len(items), "、".join(i["id"] for i in items[:5])))
            info = items[0]
        else:
            info = of.parse_link(" ".join(rest))
        if not info or not info.get("id"):
            raise ValueError("认不出 OmniFocus 任务 primary key：%r（要 omnifocus:///task/… "
                             "或裸 PK；只有标题的那种粘贴认不出来）" % (" ".join(rest),))
        task = self.task_store.link_omnifocus(task_id, info["id"], title=info.get("title"))
        self.say("🔗 任务 %d 已关联 OmniFocus %s%s%s"
                 % (task["id"], info["id"],
                    ("「%s」" % info["title"]) if info.get("title") else "",
                    "（链接是从剪贴板读的）" if auto else ""), "ok")
        return task

    def cmd_task_import(self, args):
        """``task import [--assign N] [粘贴文本|链接]`` —— 从**剪贴板**导入任务。

        需求（第 4 条）：在 OmniFocus 里复制一条任务（剪贴板里自带 primary key 的
        链接），然后 import —— 本地任务**自动关联**到那条 OmniFocus 任务。

        **动作归子命令，后缀归分流**：

            task import                 ← 读剪贴板（默认动作，不需要 flag）
            task import -a 3            ← 同上，顺手分流/挂到表格 #3
            task import [标题](omnifocus:///task/PK)   ← 手动给链接

        为什么不靠粘贴：从 OmniFocus ⌘C 再往终端 ⌘V，粘出来的**只有标题** ——
        终端只接受纯文本那个 flavor，链接在粘贴那一刻就没了。所以 import 自己按
        flavor 去读剪贴板：`the clipboard as URL` / `«class HTML»` / `«class RTF »` /
        纯文本，四个都读、按 id 合并（见 ``omnifocus.clipboard_links``）。

        不带参数就是读剪贴板；粘进来一段**只有标题**的文字时，会去剪贴板里对一下
        标题完全一致的那条链接（那是同一次 ⌘C 的两半，不是拿标题猜身份），对不上
        就建任务但**明确说一句**没关联 —— 宁可少关联，也不拿标题猜身份。
        """
        toks = []
        assign_to = None
        raw = [str(a).strip() for a in args]
        i = 0
        while i < len(raw):
            tok, low = raw[i], str(raw[i]).lower()
            if low in ("--assign", "-a") and i + 1 < len(raw):
                assign_to = raw[i + 1]
                i += 2
                continue
            if low.startswith("--assign="):
                assign_to = tok.split("=", 1)[1]
            elif tok:
                toks.append(tok)
            i += 1

        pasted = " ".join(toks).strip()
        if not pasted:
            items = [dict(item, from_clipboard=True) for item in of.clipboard_links()]
            if not items:
                raise ValueError(
                    "剪贴板里没有 OmniFocus 链接（URL / HTML / RTF / 纯文本四个 flavor "
                    "都试过了）。在 OmniFocus 里 ⌘C 一条任务再回来敲 `task import`；"
                    "也可以手写 `task import [标题](omnifocus:///task/PK)`。")
        else:
            items = of.parse_links(pasted) or [of.parse_link(pasted)]
            if not any(item and item.get("id") for item in items):
                # 粘进来的只有标题 → 去剪贴板对同名的那条（同一次 ⌘C 的两半）
                items = self._task_import_match_clipboard(items, of.clipboard_links())

        made = []
        for item in items:
            if not item:
                continue
            title = item.get("title") or (item.get("id") or "（无标题）")
            omnifocus = None
            if item.get("id"):
                omnifocus = {"id": item["id"], "url": item.get("url"),
                             "title": item.get("title")}
            task = self.task_store.create({"title": title}, omnifocus=omnifocus)
            made.append(task)
            if item.get("id"):
                self.say("✅ 导入任务 %d「%s」→ OmniFocus %s%s"
                         % (task["id"], title, item["id"],
                            "（链接是从剪贴板读的）" if item.get("from_clipboard") else ""),
                         "ok")
            else:
                self.say("✅ 导入任务 %d「%s」（只有标题：认不出 OmniFocus primary key，"
                         "所以**没有关联**）" % (task["id"], title), "warn")
                self.say("   要连上那条 OmniFocus 任务：回 OmniFocus ⌘C 它，"
                         "再敲 `task %d link`（不带参数就是读剪贴板）。"
                         % task["id"], "info")
        if made:
            self.selected_task_id = made[-1]["id"]
        if made and assign_to:
            self.cmd_task_assign([str(made[-1]["id"]), assign_to])
        return made[-1] if made else None

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

    def current_session(self):
        """「当前这一场」→ ``(表格编号, 行)``：进行中的优先，其次最近一场还没开始的。

        需求（``task 8 -ac``）：正在干的那一场就是「当前」；一场都不在进行时，
        取**最近一场还没开始**的（下一场马上就来，先挂上去就是为了别手忙脚乱）。
        已经全结束、或者这一天一行都没有 —— 明说，不猜。
        """
        now_ms = int(time.time() * 1000)
        live = [r for r in self.rows
                if r.get("startMs") is not None
                and r["startMs"] <= now_ms < (r.get("endMs") or 0)]
        if not live:
            upcoming = [r for r in self.rows if (r.get("startMs") or 0) > now_ms]
            live = upcoming[:1]
        if not live:
            raise ValueError("现在没有正在进行的 session，也没有还没开始的一场"
                             "（%s 只有 %d 行）；用表格编号挂吧。"
                             % (self.sel_date.isoformat(), len(self.rows)))
        row = live[0]
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
            self.say("任务 %d 本来就没挂 session（也没 Focusmate task），不用下架。"
                     % task["id"], "info")
            return task
        note = self.fm_delete_task(task)
        task = self.task_store.unassign(task["id"], reason=reason)
        self.say("⏏ 任务 %d「%s」已从 %s %s 下架%s"
                 % (task["id"], self.task_store.focusmate_title(task, 40),
                    sess.get("date") or "（没有日期）", sess.get("start") or "",
                    "；%s" % note if note else ""), "ok")
        return task

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
            raise ValueError("用法：task <任务 ID> swap <任务 ID>")
        a = self.task_store.get(toks[0])
        b = self.task_store.get(toks[1])
        if a["id"] == b["id"]:
            raise ValueError("swap 要给两条不同的任务（都写成了 %d）" % a["id"])
        sa = a.get("session") or {}
        sb = b.get("session") or {}
        same = (sa and sb and (sa.get("date"), sa.get("start"))
                == (sb.get("date"), sb.get("start")))
        if not sa and not sb:
            self.say("任务 %d 和 %d 都还赋闲（没挂 session）—— 没有位置可换。"
                     % (a["id"], b["id"]), "warn")
            return [a["id"], b["id"]]
        if same:
            self.say("任务 %d 和 %d 都在 %s %s，位置本来就一样，不用换。"
                     % (a["id"], b["id"], sa.get("date") or "?", sa.get("start") or "?"),
                     "info")
            return [a["id"], b["id"]]

        def where(sess):
            return ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?")
                    if sess else "赋闲")

        self.say("互换位置：任务 %d（%s）↔ 任务 %d（%s）"
                 % (a["id"], where(sa), b["id"], where(sb)), "cmd")
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
                self.say("⚠ 任务 %d 原来的位置 %s 不在当前表格里（那天没在显示），"
                         "所以它现在是赋闲的 —— 切到那天再 `task %d -a <编号>`。"
                         % (task["id"], where(sess), task["id"]), "warn")
                continue
            try:
                self.cmd_task_assign([str(task["id"]), str(idx)], refresh=False)
                done.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡另一条
                self.had_error = True
                self.say("❌ 任务 %d 没能挂回 %s：%s" % (task["id"], where(sess), e), "error")
        self.rebuild_rows()
        idle_now = [t for t, sess in ((a, sb), (b, sa)) if not sess]
        if done:
            moved_txt = "、".join("任务 %d → %s" % (tid, where(sa if tid == b["id"] else sb))
                                  for tid in done)
            idle_txt = ("；赋闲的：" + "、".join("任务 %d" % t["id"] for t in idle_now)
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
                self.say("   %d %s%s"
                         % (task["id"], self.task_store.focusmate_title(task, 40),
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
                self.cmd_task_assign([str(task["id"]), str(tgt_idx)], refresh=False)
                moved.append(task["id"])
            except Exception as e:                      # noqa: BLE001 - 一条失败不挡其余
                self.had_error = True
                failed.append((task["id"], e))
                self.say("❌ 任务 %d 转移失败：%s" % (task["id"], e), "error")
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
            task = self.task_store.get(args[0])
            session_token = args[1]
        else:
            task = self._task_for_assign()
            session_token = args[0]
        session_idx, row = self._resolve_assign_target(session_token)
        if self._session_kind(session_token) == "current" and row is not None:
            self.say("「当前这一场」= 表格 #%d（%s %s %s）"
                     % (session_idx, row.get("date") or self.sel_date.isoformat(),
                        row.get("startLabel"), row.get("phase") or ""), "info")
        if row is None:
            # `-a none`：下架。它不是一个「场次」，所以不走下面那套建 task 的流程。
            self.selected_task_id = task["id"]
            task = self._task_detach(task, reason="cmd-assign-none")
            self.rebuild_rows()
            return task
        if row.get("id") is None:
            raise ValueError("session #%d 还没有 Focusmate session id（没订上/快照），"
                             "不能挂 task。先 refresh 成功一次。" % session_idx)
        date_iso = row.get("date") or self.sel_date.isoformat()
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
            self.say("注意：任务 %d 原来挂在 %s %s；本地会改记到这场，Focusmate 上那条旧 task "
                     "不会跟着挪（完成时间照样会归档）。"
                     % (task["id"], prev.get("date"), prev.get("start")), "warn")
        already = [t for t in self.task_store.tasks_for_session(date_iso, row.get("startLabel"))
                   if t.get("id") != task["id"]]
        self.say("把任务 %d「%s」挂到 session #%d（%s %s）%s…"
                 % (task["id"], title, session_idx, date_iso, row.get("startLabel"),
                    "（这场已有 %d 个任务，再去挂一个）" % len(already) if already else ""), "cmd")
        res = self.bridge.call("add_task", timeout=240,
                               startMs=row["startMs"], meetingId=row["id"],
                               title=title, notes=notes)
        if not res.get("verified"):
            self.had_error = True
            raise ValueError("Focusmate 里没能确认 task 已挂上；本地任务没改，避免假成功。")
        # Focusmate 成功之后才落本地。
        self.task_store.assign(task["id"], session)
        self.task_store.attach_focusmate(task["id"], {
            "task_id": res.get("taskId") or (res.get("task") or {}).get("id"),
            "session_id": row["id"],
            "attached_at": ts._now(),
            "title": title,
            "via": res.get("via"),
        }, session=session)
        self.selected_task_id = task["id"]
        session["task_id"] = res.get("taskId") or (res.get("task") or {}).get("id")
        # 表格里那一行的 task 列是 rebuild_rows 时算出来的：不重排就还是旧的
        # （用户看到的正是「assign 成功但 task 列空着」）。先本地重排，再尽量刷新。
        self.rebuild_rows()
        if refresh and not self.offline:
            try:
                self.refresh(quiet=True)
            except Exception:                       # noqa: BLE001 - 刷新失败不影响已完成的挂载
                pass
        remote_had = res.get("existingCount")
        total = (remote_had if isinstance(remote_had, int) else len(already)) + 1
        extra = ""
        if total > 1:
            extra = "（这场现在共 %d 个任务，表格里会一起列出来）" % total
        via = res.get("via")
        self.say("✅ 任务 %d 已挂到 session #%d；Focusmate task id=%s%s%s"
                 % (task["id"], session_idx, res.get("taskId") or "?",
                    "（via %s）" % via if via else "", extra), "ok")
        return True

    def materialize_repeats(self):
        """把「每周重复任务」挂到选中那天的 session 上（本地 + Focusmate）。

        只有真的落上了（同一时刻、同一时长）才挂：排布被拆开/改时长后这一周就
        没有落点 —— repeat 设置不动，下一周照旧。Focusmate 写入必须复核成功才
        落本地，失败就把错误抛给调用方，绝不假成功。
        """
        day = self.sel_date
        sessions = self.sessions_for_day_plan(day)
        iso = day.isoformat()
        done = []
        for task in self.task_store.repeated_tasks(day.weekday()):
            hit = self.task_store.repeat_occurrence(task, day, sessions)
            if not hit:
                continue
            occ = (task.get("repeat") or {}).get("occurrences") or {}
            already = (occ.get(iso) or {}).get("focusmate") or {}
            if already.get("task_id"):
                continue
            row = next((r for r in self.rows
                        if r.get("planned") and r["startLabel"] == hit["start"]), None)
            session = {
                "index": self.rows.index(row) + 1 if row in self.rows else None,
                "date": iso,
                "start": hit["start"],
                "end": hit.get("end"),
                "duration": hit.get("duration"),
                "focusmate_session_id": (row or {}).get("id"),
            }
            focusmate = {}
            if row is not None and row.get("id") and not self.offline:
                title = self.task_store.focusmate_title(task)
                notes = (task.get("fields") or {}).get("notes") or ""
                res = self.bridge.call("add_task", timeout=240,
                                       startMs=row["startMs"], meetingId=row["id"],
                                       title=title, notes=str(notes))
                if not res.get("verified"):
                    raise ValueError("重复任务 %d 没能挂到 %s %s（Focusmate 没确认）"
                                     % (task["id"], iso, hit["start"]))
                focusmate = {"task_id": res.get("taskId"),
                             "session_id": row["id"], "attached_at": ts._now(),
                             "title": title}
            self.task_store.attach_occurrence(task["id"], day, session, focusmate)
            done.append((task["id"], hit["start"]))
        return done

    def cmd_task_sync(self, quiet=False):
        """GET /v1/tasks → 读 completedAt，给已完成任务写归档。

        顺带把「每周重复」落到选中那天的 session 上（落不上就算了，那是排布变了）。
        """
        if not self.offline:
            try:
                for task_id, start in self.materialize_repeats():
                    self.say("🔁 任务 %d 已挂到 %s %s" % (task_id, self.sel_date, start), "ok")
            except Exception as e:                      # noqa: BLE001
                self.had_error = True
                self.say("重复任务挂载失败：%s" % e, "warn")
        ok = self.sync_task_completions(quiet=quiet)
        # 完成的时候把「完成 + 截止时间」写回 OmniFocus（幂等，写过的不再写）
        self.writeback_completions()
        if not quiet:
            self.report_omnifocus_links()
        return ok

    def sync_task_completions(self, quiet=True):
        """**只读**那一半：``GET /v1/tasks`` → 对齐完成状态（`refresh` 用它）。

        和 :meth:`cmd_task_sync` 的区别只有一件事：这里不往外面写 —— 不把「每周重复」
        落到焦点场次上（那会真的在 Focusmate 建 task）、也不写回 OmniFocus。
        ``refresh`` 要能随时敲，不能敲一下就在外面多出一条任务。
        """
        res = self.bridge.call("tasks", timeout=180)
        remote = res.get("tasks") if isinstance(res, dict) else res
        before = len(self.task_store.load_archive().get("records") or [])
        n, matched = self.task_store.reconcile(remote or [])
        self.task_sync_at = time.time()
        if n:
            self.announce_completions(before)
        if not quiet:
            self.say("Focusmate task 同步：匹配 %d 条，新增完成归档 %d 条。"
                     % (len(matched), n), "ok")
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
            line = "✅ 完成：任务 %s「%s」· 完成时间 %s · session %s %s" % (
                rec.get("task_id") or "?", title,
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

    def omni_complete(self, task, minutes=None, at=None):
        """OmniFocus：完成 + 把截止时间写回 session 终点。返回一句人话。"""
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        if not self.omnifocus_enabled():
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        due = self.omni_due_of(task)
        try:
            res = self.omnifocus.complete(pk, at=at, due=due or None, yes=True)
        except of.OmniFocusError as e:
            self.had_error = True
            return "OmniFocus 写回失败：%s" % e
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "completed")
        except ValueError:
            pass
        changed = ", ".join(res.get("changed") or [])
        return "OmniFocus %s 完成%s" % (pk, ("（%s）" % changed) if changed else "")

    def omni_drop(self, task, at=None):
        """OmniFocus：放弃这条任务（只给「真的不要了」用）。"""
        pk = ts.TaskStore.omnifocus_id(task)
        if not pk:
            return ""
        if not self.omnifocus_enabled():
            return "OmniFocus 关着（set omnifocus on 打开）→ 只改了本地"
        try:
            res = self.omnifocus.drop(pk, at=at, yes=True)
        except of.OmniFocusError as e:
            self.had_error = True
            return "OmniFocus 放弃失败：%s" % e
        try:
            self.task_store.set_omnifocus_writeback(task["id"], "dropped")
        except ValueError:
            pass
        changed = ", ".join(res.get("changed") or [])
        return "OmniFocus %s 已放弃%s" % (pk, ("（%s）" % changed) if changed else "")

    def fm_complete(self, task):
        """Focusmate：把这条 task 勾完成（走 bridge 的内部 API，GET 复核 completedAt）。"""
        fm = task.get("focusmate") or {}
        task_id = fm.get("task_id")
        if not task_id:
            return ""
        if self.offline:
            return "离线模式：没去勾 Focusmate"
        try:
            res = self.bridge.call("complete_task", timeout=180, taskId=task_id)
        except BridgeError as e:
            self.had_error = True
            return "Focusmate 勾完成失败：%s" % e
        if not res.get("verified"):
            return "Focusmate 没确认勾上（可以手动点一下）"
        stamp = res.get("completedAt") or ts._now()
        try:
            self.task_store.attach_focusmate(task["id"], {"completed_at": stamp})
        except ValueError:
            pass
        return "Focusmate 已勾完成"

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
        if not self.omnifocus_enabled():
            return {}
        rows = None
        try:
            rows = self.omnifocus.list_tasks(refresh=True)
        except of.OmniFocusError as e:
            self.had_error = True
            self.say("读 OmniFocus 失败：%s" % e, "warn")
            return {}
        by_id = {}
        for row in rows or []:
            if row.get("id"):
                by_id[str(row["id"])] = row
        out = {}
        for task in self.task_store.list():
            pk = ts.TaskStore.omnifocus_id(task)
            if pk and str(pk) in by_id:
                out[task["id"]] = by_id[str(pk)]
        return out

    def writeback_completions(self, tasks=None):
        """把「本地已完成、但还没写回 OmniFocus」的任务写回去（幂等，靠 writeback 标记）。

        需求（第 2 条）：任务被分配到的 session 时间＝它的截止时间；完成的时候
        把这条截止时间一起写回 OmniFocus —— 于是 OmniFocus 里记的是「这一场是什么时候结束的」。
        """
        done = []
        if not self.omnifocus_enabled():
            return done
        for task in (tasks if tasks is not None else self.task_store.list()):
            if self.task_store.status_of(task) != ts.STATUS_COMPLETED:
                continue
            if not ts.TaskStore.omnifocus_id(task):
                continue
            wb = ((task.get("omnifocus") or {}).get("writeback") or {})
            if wb.get("completed"):
                continue
            note = self.omni_complete(task)
            if note:
                done.append((task["id"], note))
                self.say("↗ 写回 OmniFocus：任务 %d %s" % (task["id"], note), "ok")
        return done

    # -- 收尾用的几个动作（收尾和命令共用同一套，不写两遍）--------------

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
        cache = getattr(self, "_snapshot_cache", None)
        if cache is None:
            cache = self._snapshot_cache = {}
        rows = cache.get(iso)
        if rows is None:
            try:
                res = self.bridge.call("snapshot", timeout=180, date=iso)
                rows = res.get("sessions") or []
            except BridgeError:
                rows = []
            cache[iso] = rows
        for row in rows:
            if row.get("startLabel") == slot.get("start") and row.get("id"):
                info["focusmate_session_id"] = row["id"]
                info["startMs"] = row.get("startMs")
                info["end"] = row.get("endLabel") or info["end"]
                break
        return info

    def assign_task_to(self, task, session):
        """把任务挂到一场 session：Focusmate 建 task（能建就建）+ 本地落账。返回一句人话。"""
        date_iso = session.get("date") or self.sel_date.isoformat()
        sid = session.get("focusmate_session_id") or session.get("id")
        start_ms = session.get("startMs")
        title = self.task_store.focusmate_title(task)
        notes = self.task_store.notes(task) or ""
        payload = {
            "index": None,
            "date": date_iso,
            "start": session.get("start"),
            "end": session.get("end"),
            "duration": session.get("duration"),
            "focusmate_session_id": sid,
        }
        note = ""
        if sid and start_ms is not None and not self.offline:
            try:
                res = self.bridge.call("add_task", timeout=240, startMs=start_ms,
                                       meetingId=sid, title=title, notes=str(notes))
            except (BridgeError, ValueError) as e:
                res = {}
                note = "Focusmate 没建上：%s" % e
            if res.get("verified"):
                self.task_store.assign(task["id"], payload)
                self.task_store.attach_focusmate(task["id"], {
                    "task_id": res.get("taskId") or (res.get("task") or {}).get("id"),
                    "session_id": sid,
                    "attached_at": ts._now(),
                    "title": title,
                    "via": res.get("via"),
                }, session=payload)
                note = "Focusmate task %s" % (res.get("taskId") or "?")
                self.rebuild_rows()
                return note
        self.task_store.assign(task["id"], payload)
        return note or "这场还没有 Focusmate session id（没订上），只记了本地"

    def next_session_after(self, task, max_days=2):
        """这条任务「下一场」放哪儿：同一场之后最近的那个 slot（跨天也找）。"""
        sess = task.get("session") or {}
        if sess.get("date") and sess.get("start"):
            day_iso = str(sess["date"])
            start = str(sess["start"])
        else:
            # 没挂过场次（`inspect-due` 里的过期任务常常这样）：从「现在」往后找
            now = datetime.datetime.now()
            day_iso = now.date().isoformat()
            start = now.strftime("%H:%M")
        try:
            day = datetime.date.fromisoformat(day_iso)
        except ValueError:
            day = self.sel_date
        entries = self.reload_entries()
        for offset in range(0, int(max_days) + 1):
            probe = day + datetime.timedelta(days=offset)
            try:
                slots = sch.day_slots(entries, probe)
            except Exception:                    # noqa: BLE001 - 排布坏了就当这天没有
                continue
            for slot in slots:
                if offset == 0 and str(slot.get("start") or "") <= start:
                    continue
                return self.session_info_for(probe, slot)
        return None

    # -- 收尾（postsession）：session 结束 15 秒后的强制对账 -----------------

    def review_hooks(self):
        """收尾流程要「碰外面世界」的那几个口子（其余全在 postsession.Review 里）。"""
        return {
            "say": lambda text, kind="info": self.say(text, kind),
            "drop_omnifocus": lambda task: self.omni_drop(task),
            "complete_focusmate": lambda task: self.fm_complete(task),
            "delete_focusmate_task": lambda task: self.fm_delete_task(task),
            "complete_omnifocus": lambda task, minutes=None: self.omni_complete(task),
            "next_session": lambda task: self.next_session_after(task),
            "assign_focusmate": lambda task, sess: self.assign_task_to(task, sess),
            "due_of": lambda task: self.omni_due_of(task),
        }

    def sync_completion_state(self, entry=None):
        """核对完成状态：Focusmate 的 completedAt + OmniFocus 的 completed。

        走 bridge / 子进程，**可能要几秒**，所以自动收尾从后台线程调它
        （见 :meth:`_review_worker`），手动 `review` 就直接调。
        返回 ``{本地任务 id: omni 行}``。
        """
        if not self.offline:
            try:
                res = self.bridge.call("tasks", timeout=180)
                remote = res.get("tasks") if isinstance(res, dict) else res
                before = len(self.task_store.load_archive().get("records") or [])
                n, matched = self.task_store.reconcile(remote or [])
                self.task_sync_at = time.time()
                if n:
                    self.announce_completions(before)
            except (BridgeError, ValueError) as e:
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
            if self.task_store.is_open(task):
                self.task_store.set_status(task_id, ts.STATUS_COMPLETED,
                                           at=row.get("completed_at"),
                                           reason="omnifocus-completed")
                self.say("✓ 任务 %d 在 OmniFocus 里已完成" % task_id, "ok")
        self.writeback_completions()
        return rows

    def begin_review(self, entry, sync=True):
        """进入一场收尾：核对完成状态 → 挑出没做完的任务 → 建 Review。"""
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
        self.rebuild_rows()
        if not opened:
            self.say("⏱ %s %s 那场没有未完成的任务（%d 条记录都已收）—— 这场翻篇。"
                     % (day_iso, start or "?", len(tasks)), "ok")
            psv.mark_done(entry, note="nothing-open")
            self.review = None
            return None
        self.review = psv.Review(self.task_store, entry, opened, hooks=self.review_hooks())
        self.say("⏱ %s 收尾：%d 条没做完的任务，一条一条来（A/B/C/D/E）。"
                 % (self.review.headline(), len(opened)), "cmd")
        return self.review

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
        self.say("🔍 %s：%d 条要过一遍（A/B/C/D/E，s 跳过，q 结束）。"
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
        return self.review_panel()

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
            if row.get("id") is not None:
                self.say("#%d 取消临时挪动目标 %s…" % (idx, row["startLabel"]), "cmd")
                with self.console.status("[red]取消中…[/]", spinner="dots"):
                    res = self.bridge.call("cancel", startMs=row["startMs"], meetingId=row["id"])
                if not res.get("verified"):
                    self.had_error = True
                    self.say("❌ #%d 取消后复核：临时目标还在；挪动没撤销。" % idx, "error")
                    return False
            sch.remove_relocation(self.sel_date, from_hhmm)
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
            self.say("清掉任务 %d「%s」的绑定（原来 %s %s，这一场排布改了）"
                     % (task.get("id"), self.task_store.summarize(task, max_len=24),
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
                res = self.bridge.call("cancel", timeout=180,
                                       startMs=row["startMs"], meetingId=row["id"])
                if not res.get("verified"):
                    self.had_error = True
                    raise ValueError("旧 session %s 取消后复核还在；排布没改，避免两套 session 并存。"
                                     % row.get("startLabel"))
                self.say("✅ 已取消旧 session %s" % row.get("startLabel"), "ok")
                self.api_rows = [r for r in self.api_rows
                                 if not (r.get("id") == row.get("id")
                                         and r.get("startMs") == row.get("startMs"))]
                cancelled.append(row)
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
           的 ``GET /v1/tasks`` 里那条 task 没有指向那场的 attachment（手动删过、
           或者上次只做了半截）→ 重新挂。
        3. 有问题的都修完之后再拉一次，表格显示的就是**修完之后**的样子。

        不带 ``--check`` 的 ``refresh`` **不动外面任何东西**，但它不是只管一行数：
        它把看板上每块组件的状态都重新对一遍账（见 :meth:`refresh_all`）——
        表格、任务完成状态、统计、收尾面板、各种缓存。
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
        列表、重算表格」，而是一条命令把看板上每一块拆开重新对账（**只读外面世界**：
        不订场、不挂任务、不改 data.csv）：

        ==================  ============================================
        组件                刷什么
        ==================  ============================================
        契约表格            重读 data.csv / 日历下载层 / 时段 layout → 重拉 session
        任务面板            任务的**完成状态**（``GET /v1/tasks``，只读同步）
        统计视图            已经加载过、且超过 TTL 的那份数字重算一次
        收尾面板            正在问的那一轮从任务库里重读一遍
        警告 / 静音         缓存作废（mute 缓存、快照缓存、日志尾巴）
        画帧缓存            作废 → 下一帧按新状态重排版
        ==================  ============================================

        失败不拦路：任何一块刷不动就记一句警告，其余照刷 —— refresh 是「再对一次账」，
        不是「全有或全无」。
        """
        self.forget_caches()
        pulled = self.refresh()
        self.refresh_task_state()
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
        """任务的完成状态对齐 Focusmate（``GET /v1/tasks``，**只读**）。

        走 :meth:`sync_task_completions` 而不是 ``cmd_task_sync``：refresh 不能
        顺手在 Focusmate 上建 task（重复任务落地）、也不写回 OmniFocus。
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
        """把「该订没订上 / 该挂没挂上」都查一遍并修掉（``refresh --check`` 的实作）。"""
        if self.offline:
            self.say("refresh --check：离线模式下没有外部世界可查，只重画了表格。", "warn")
            return False
        self.say("refresh --check：对账 —— 该订的 session、该挂的 task…", "cmd")
        fixed = 0
        fixed += self.check_bookings()
        fixed += self.check_task_attachments()
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
        rc, out, err = run_keep_session("check", timeout=600)
        for line in (out or "").splitlines()[-20:]:
            self.say(line)
        if rc != 0:
            self.had_error = True
            self.say("❌ check 退出码 %d%s（计划照旧留着，daemon 下一轮还会补）"
                     % (rc, ("：" + err) if err else ""), "error")
            return 0
        return len(missing)

    def check_task_attachments(self):
        """任务真的挂在那场 session 上了吗？挂漏/挂错的重新挂一遍。返回修了几条。

        判据只有一条，而且来自 Focusmate 自己：``GET /v1/tasks`` 里那条 task 的
        ``attachments`` 里有没有这一场的 session id。本地账本说「挂了」不算数 ——
        手动在 Task Manager 删过、或上次调 API 只做了半截，账本都会照旧留着。
        """
        remote = self._remote_tasks()
        if remote is None:
            self.say("⚠ 拉不到 GET /v1/tasks（bridge 不在？），任务挂载这一项跳过。", "warn")
            return 0
        day = self.sel_date.isoformat()
        bad = []
        for task in self.task_store.list():
            sess = task.get("session") or {}
            if (sess.get("date") or "") != day:
                continue
            if not self.task_store.is_open(task):
                continue
            verdict = self._attachment_verdict(task, remote)
            if verdict:
                bad.append((task, verdict))
        if not bad:
            return 0
        fixed = 0
        for task, verdict in bad:
            sess = task.get("session") or {}
            self.say("⚠ 任务 %d「%s」%s（本地记着挂在 %s %s）—— 重挂…"
                     % (task["id"], self.task_store.focusmate_title(task, 30), verdict,
                        sess.get("date") or "?", sess.get("start") or "?"), "warn")
            idx, row = self._row_for_session(sess)
            if row is None or row.get("id") is None:
                self.say("   那一场现在不在表格里（或还没订上），先 refresh --check 等它订上。",
                         "warn")
                continue
            try:
                self.cmd_task_assign([str(task["id"]), str(idx)], refresh=False)
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
        """
        self.say("sync：先 download（工作/空闲 → 中间层），再 populate "
                 "（中间层 + 时段 layout → data.csv）…", "cmd")
        rc, out, err = run_download_calendar("download", *args, timeout=300)
        for line in (out or "").splitlines()[-20:]:
            self.say(line)
        if rc != 0:
            self.had_error = True
            self.say("❌ download 退出码 %d%s" % (rc, ("：" + err) if err else ""), "error")
            if not (err or out):
                self.say("   （download_calendar.py 没有输出；先跑 "
                         "python3 download_calendar.py status 看配置/依赖）", "warn")
            return False
        rc, out, err = run_download_calendar("populate", "--from-download", *args,
                                             timeout=300)
        for line in (out or "").splitlines()[-25:]:
            self.say(line)
        if rc != 0:
            self.had_error = True
            self.say("❌ populate 退出码 %d%s" % (rc, ("：" + err) if err else ""), "error")
            return False
        self.say("✅ data.csv 已同步（下载层 → 时段 layout → 精确日期契约）。", "ok")
        self.reload_entries()
        self.refresh()
        return True

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
        self.say("启动 daemon…", "cmd")
        rc, out, err = run_keep_session("daemon", "start")
        for line in (out or "").splitlines():
            self.say(line)
        if rc == 0:
            self.say("✅ daemon 已启动", "ok")
        else:
            self.had_error = True
            self.say("❌ daemon 启动失败（rc=%d）%s" % (rc, err), "error")
        return rc == 0

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
        self.say("停 daemon…", "cmd")
        rc, out, err = run_keep_session("daemon", "stop")
        for line in (out or "").splitlines():
            self.say(line)
        if rc == 0:
            self.say("✅ daemon 已停止（bridge 还活着，随时能改 session）", "ok")
        else:
            self.had_error = True
            self.say("❌ daemon 停止失败（rc=%d）%s" % (rc, err), "error")
        return rc == 0

    def cmd_help(self):
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True, justify="right")
        t.add_column()
        for cmd, desc in [
            ("Tab", "切换视图：契约 → 专注 → 统计 → 任务 → 设置 → 契约（循环）"),
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
            ("task repeat <id> 周几@HH:MM|off", "给已有任务加上/取消每周重复"),
            ("task archive [N]", "看完成归档（data/task-archive.json，只增不改）"),
            ("task sync", "GET /v1/tasks，把 completedAt 连同 session 写进完成归档"),
            ("cancel N", "取消 #N：草稿区直接删契约；执行区要敲宣言"),
            ("relocate N ±MM", "临时把 #N 提前/推后：不写 data.csv；推后一天一次，且不能跨天"),
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
        self.emit(Panel(t, title="[bold]命令[/]", border_style="cyan", box=box.ROUNDED))
        self.emit(
            "[dim]ID 就是表格第一列，按时间排序、每次 refresh 重编号 —— 看着表敲，别凭记忆。\n"
            "区域标记（状态列第一格）：草=草稿区（随便改）· 锁=执行区（取消要敲宣言）"
            "· 过=已过去。\n"
            "专注视图：只认 stop（消耗暂停）和 Shift+Tab（切模式），"
            "预约那套命令在那边一律没用。\n"
            "统计视图：只读，stat 走缓存、stat refresh 只增量拉缺口 + 最近几天；"
            "它不改 data.csv、不碰 API 写接口。\n"
            "任务视图：task 命令在任何视图都能敲；任务字段不校验，JSON 存在 data/tasks.json。\n"
            "设置视图：列出所有设置项和解释；set <id> <value> 改，"
            "edit-tomorrow / tomorrow-plan 见那里的说明。\n"
            "状态框（契约视图最上面）：契约执行情况 + 警告；warn off 可以藏掉警告。\n"
            "时段 ID（T1/T2）来自下载层 temp/calendar-download.json；"
            "同段后续行留空是正常的。[/]"
        )
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
        return run

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
        try:
            self.refresh()
        except Exception as e:
            self.say("首次拉列表失败：%s" % e, "error")
            self.had_error = True

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
    usage = "用法：relocate N ±MM（MM 是 15 的倍数，如 relocate 3 -15）"
    if len(toks) == 2:
        return toks[0], parse_relocate_offset(toks[1])
    if len(toks) == 1:
        # 有人会连写成 relocate 1-15 / relocate 1+15。
        m = re.match(r"^(\d+)([+-]\d+)$", toks[0])
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
    warden = Warden(console, bridge, no_clear=no_clear, offline=args.offline,
                    day_offset=args.day)

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
