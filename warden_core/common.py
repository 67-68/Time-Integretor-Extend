"""warden_core.common —— 环境引导、全局常量、配色、宣言校验、列规格与通用小工具。"""
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


# HERE 指回项目根目录（ti_extend/），而不是 warden_core/ 子目录
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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



# 顶层 warden 模块的 globals() 绑定（供测试里的 w.run_keep_session / w.BRIDGE_LOG 等猴子补丁实时生效）
WARDEN_GLOBALS = {}


def bind_warden_globals(ns):
    """绑定顶层 warden.py 的 globals() 字典。"""
    global WARDEN_GLOBALS
    WARDEN_GLOBALS = ns


def get_warden_global(name, default):
    """优先从顶层 warden.py 的 globals() 取同名符号（兼容测试 monkeypatch）。"""
    if isinstance(WARDEN_GLOBALS, dict) and name in WARDEN_GLOBALS:
        return WARDEN_GLOBALS[name]
    return default


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
    "create", "import", "delete", "modify", "note", "assign", "assign-count",
    "duplicate", "drop", "done", "link", "repeat", "sync", "swap", "archive",
})
TASK_MUTATING_FLAGS = (
    "-a", "--assign", "-n", "--note", "-m", "--mark", "-c", "--count",
    "-act", "--assign-count", "--assigned-count", "--act",
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
    # `-act` 展开成 `--assign-count`，后面跟这一场要记几个 count：
    # `task 5 -ac -act 2` = 先挂当前场，再把本场 count 覆盖成 2。
    repl_kit.ShortFlag("-act", ("--assign-count",), note="设置这一场的 count"),
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
# end / enlist / repeat（enlist 永远排在其它 formatter 之后）。
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
TASK_DYNAMIC_HIDDEN = {"due", "due_source", "count_invested", "count_total", "project_id", "project_task", "notes", "note"}

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

