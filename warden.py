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
4. **任务**：本地 JSON 任务库（字段不校验）+ 挂到 session + Focusmate 完成归档。
   `task create/delete/modify/assign/select/repeat/sync`；`task assign` 成功后才写
   本地，Focusmate 侧 task id / completedAt 都会记回来。`--repeat 周几@HH:MM` 让任务
   每周重复：只在「同一星期几、同一时刻、同一时长」的那场 session 上落地；排布被
   改过（例如 50 拆成 25+25）时这一周就不落，但 repeat 设置本身不变。
5. **设置**：`settings_store.SETTINGS` 里注册的全部设置项（id / 当前值 / 默认值 /
   解释），`set <id> <value>` 修改，不写 value = 恢复默认。现有两项：
   `edit-tomorrow`（今天可以先安排明天，过了凌晨回到 24 小时窗口）和
   `tomorrow-plan`（明日时间安排 = 一个每周重复任务）。

**状态框**（契约视图最上面那一块 ASCII 框）：上面是「契约（data.csv 周几）」的
执行情况，下面是警告（`repl_kit.WarningBoard`）—— data.csv 解析问题、layout 坏了、
今天没有落上每周重复任务…… 警告由框架画、由 warden 每帧重算；`warn off` 只藏警告，
`warn on` 回来。

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

看板比终端高时，`WardenShell` 会从**顶部**裁，保证最下面的消息 / 视图栏 / 提示行 /
状态行 / 输入行永远在屏幕上（矮终端里顶部本来就装不下）。补全菜单浮在看板上，
不再在输入行下面预留 8 行空白。

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

import keep_session as ks  # noqa: E402  只用来读 daemon 状态 / 起停 daemon / 罪与判决
import schedule as sch  # noqa: E402  data.csv 的解析与编辑（唯一实现）
import session_plan as sp  # noqa: E402  日历下载层 / 时段 layout / 精确日期 csv
import task_store as ts  # noqa: E402  本地任务库 + Focusmate 完成归档
import stats as st  # noqa: E402  生涯统计：只读 API 拉全量历史 + 算数字 + 缓存
import settings_store as cfg  # noqa: E402  设置项注册表 + data/settings.json

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


def _hint_sync(parts):
    return repl_kit.Hint("选项", ("--days",), note="日历 → data.csv")


def _hint_stat(parts):
    if len(parts) >= 2 and parts[1] == "refresh":
        return repl_kit.Hint("动作", ("full",), note="不加 full = 增量刷新")
    return repl_kit.Hint("动作", ("refresh", "refresh full"),
                         note="默认走 1 小时缓存")


def _hint_period(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("时长", ("25", "50", "75"), note="可连续给多个；超时就报错")
    return repl_kit.Hint("参数", ("T1", "50", "50"), note="按 25/50/75 排这个时段")


def _hint_set(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("取值", ("on", "off"), note="不写 = 恢复默认")
    return repl_kit.Hint("参数", ("<设置 id>", "<value>"),
                         note="完整清单在「设置」视图；不写 value = 恢复默认")


def _hint_task(parts):
    if not parts:
        return repl_kit.Hint("动作", ("create", "delete", "modify", "note", "assign",
                                      "select", "repeat", "sync", "archive"),
                             note="字段随便写，不校验 · 备注可以塞网址")
    action = parts[0].lower() if len(parts) else ""
    if action == "create":
        return repl_kit.Hint("参数", ("[id]", "key=value...", "--repeat 周几@HH:MM",
                                      "--assign N"),
                             note="不给 id 就自动编号")
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
    if action == "modify":
        return repl_kit.Hint("参数", ("<任务 ID>", "<字段>", "<新值>"), note="字段不校验")
    if action == "assign":
        return repl_kit.Hint("参数", ("<session ID>",),
                             note="默认挂当前选中的任务；也可 assign <任务ID> <sessionID>")
    return repl_kit.Hint("参数", ("create", "delete", "modify", "note", "assign",
                                  "select", "repeat", "sync", "archive"),
                         note="任何动作都能带 --assign N")


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
        """「状态」框（ASCII 边框）里本视图自己的那几行；None = 这个视图不要框。

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
            _cmd("task", w.cmd_task,
                 "任务 CRUD / 挂到 session / 同步完成时间（任何视图都能敲）",
                 usage="task <create|delete|modify|note|assign|select|repeat|sync|archive> ... [--assign N]",
                 options=("create", "delete", "modify", "note", "assign", "select", "repeat",
                          "sync", "archive", "--assign", "--repeat"),
                 hint=_hint_task),
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
            _cmd("refresh", lambda a: w.refresh(),
                 "重新拉选中那天的 session（会重编号）", ("r", "reload"),
                 usage="refresh",
                 hint=_hint_actions("refresh", note="重拉选中那天的 session（会重编号）")),
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
                 "改一个时段的 session 排布：period T1 50 50",
                 usage="period <T1> [25|50|75 ...]", options=("25", "50", "75"),
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
        task repeat <id> <周几@HH:MM|off>   # 每周重复（只影响那一天的那一场）
        task sync                           # GET /v1/tasks → 把 completedAt 归档
        task archive [N]                    # 看归档（data/task-archive.json）

    任何子命令都能带 ``--assign N``（表格里的 session ID）：命令跑完顺手挂上去，
    例如 ``task create title=写论文 --assign 3``。

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
            "命令", ("task create", "task delete", "task modify", "task assign",
                     "task repeat", "task sync"),
            note="字段不校验 · --repeat 每周重复 · Tab 切视图",
        )

    def status_line(self):
        return None

    def unknown_hint(self):
        return ("「任务」视图认 task create/delete/modify/assign/select/sync；"
                "Shift+Tab 切回去再干别的。")

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", lambda a: w.cmd_task_sync(quiet=False),
                 "拉 GET /v1/tasks，把已完成任务的 completedAt 归档",
                 ("r", "reload"), usage="refresh",
                 hint=_hint_actions("refresh", note="同步 Focusmate task 完成时间")),
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
                     "  task create title=明日时间安排 --repeat mon@22:15"
                     "   ← 每周一 22:15 那场自动挂上",
                     style="dim"),
                title="[bold]Task · 任务[/]", border_style="magenta", box=box.ROUNDED)

        keys = []
        for t in tasks:
            for k in (t.get("fields") or {}):
                if k not in keys:
                    keys.append(k)
        t = Table(title="Task · 本地 JSON 任务库", box=box.SIMPLE_HEAVY,
                  header_style="bold magenta", title_justify="left", expand=False)
        t.add_column("ID", justify="right", style="bold", no_wrap=True)
        t.add_column("重复", no_wrap=True)
        t.add_column("session", no_wrap=True)
        t.add_column("Focusmate", no_wrap=True)
        t.add_column("完成时间", no_wrap=True)
        for k in keys:
            t.add_column(str(k), no_wrap=True, overflow="crop")
        if not keys:
            t.add_column("内容", no_wrap=True, overflow="crop")
        for task in tasks:
            style = "bold reverse" if task.get("id") == selected else ""
            sess = task.get("session") or {}
            sess_txt = ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?"
                                  )) if sess else "—"
            fm = task.get("focusmate") or {}
            fm_txt = fm.get("task_id") or "—"
            done = fm.get("completed_at") or "—"
            vals = []
            if keys:
                for k in keys:
                    vals.append(w.task_store._one_line((task.get("fields") or {}).get(k)))
            else:
                vals.append(w.task_store.summarize(task))
            rep = task.get("repeat") or {}
            rep_txt = ("🔁 %s@%s" % (rep.get("weekday_label"), rep.get("start"))
                       if rep else "—")
            t.add_row(str(task["id"]), Text(rep_txt, style="magenta" if rep else "dim"),
                      Text(sess_txt, style=style),
                      Text(fm_txt, style=style), Text(done, style=style),
                      *[Text(v, style=style) for v in vals])

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
    """

    def frame_text(self):
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
        """
        try:
            from prompt_toolkit.application.current import get_app
            return get_app().output.get_size().rows
        except Exception:
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
        )
        # 看板自己就是最下面那块内容；补全菜单浮在它上面，不再在输入行
        # 下面留 8 行空白（留了就会把看板/提示挤出屏幕）。
        # 老版本 repl_kit 没有这个参数就退回默认值（WardenShell 会按 8 行算）。
        if "reserve_space_for_menu" in inspect.signature(WardenShell).parameters:
            shell_kwargs["reserve_space_for_menu"] = 0
        self.shell = WardenShell(**shell_kwargs)

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

    def task_cell_text(self, tasks):
        """契约表格 task 列的正文：``1: 任务… | 3: 任务…``（编号 + 裁剪过的标题）。

        标题优先取 title/name 字段（`focusmate_title`），没有才退回整条摘要 ——
        编号 + 一长串 ``1. … 2. …`` 在表格里没法看。
        """
        parts = []
        for task in tasks or []:
            title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
            prefix = "🔁" if task.get("repeat") else ""
            parts.append("%s%d: %s" % (prefix, task.get("id"), title))
        return " | ".join(parts)

    def render_task_index(self):
        """契约视图里那块「任务表格」：两列（编号+任务 各两对）。

        需求：已 assign 到某个 session 的任务，编号和标题染红；其他保持深蓝。
        分成左右两栏是为了让任务一多也不会把契约表格挤没。
        """
        tasks = self.task_store.list()
        if not tasks:
            return None
        half = (len(tasks) + 1) // 2
        left = tasks[:half]
        right = tasks[half:]
        t = Table(title="Task · 任务（[red]红[/] = 已挂 session）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        t.add_column("number", justify="left", no_wrap=True)
        t.add_column("task", no_wrap=True, overflow="crop")
        t.add_column("number", justify="left", no_wrap=True)
        t.add_column("task", no_wrap=True, overflow="crop")
        selected = self.selected_task_id
        for i in range(len(left)):
            cells = []
            for col in (left, right):
                task = col[i] if i < len(col) else None
                cells.extend(self._task_index_cells(task, selected))
            t.add_row(*cells)
        return t

    def _task_index_cells(self, task, selected):
        """一行里的一对（编号, 标题）。没任务时给两个空格占位。"""
        if task is None:
            return [Text(""), Text("")]
        assigned = bool(task.get("session"))
        style = "bold red" if assigned else "dark_blue"
        if task.get("id") == selected:
            style += " reverse"
        title = self.task_store.focusmate_title(task, max_len=TASK_CELL_TITLE_MAX)
        prefix = "🔁" if task.get("repeat") else ""
        return [Text(str(task.get("id")), style=style),
                Text(prefix + title, style=style)]

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
        parts.append(self.render_view_bar())
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
        try:
            with open(ks.LOG_FILE, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            return lines[-n:] if lines else []
        except OSError:
            return []

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
        kinds = {0: ("今天", "bold"), -1: ("昨天", "dim"), 1: ("明天", "bold")}
        name, style = kinds.get(off, ("%+d 天" % off, ""))
        zone = self.day_zone()
        zone_txt = {
            "past": ("过去 · 只能看", "dim"),
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
                        style="dim green")
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
                    "past": ("过", "dim")}
        for i, r in enumerate(self.rows):
            idx = i + 1
            style = "dim" if r["phase"] == "已结束" else ""
            st = Text()
            tag, tag_style = zone_tag.get(r.get("zone"), ("", ""))
            if tag:
                st.append(tag + " ", style=tag_style)
            if not self.authoritative:
                # 快照里没有 session id —— 订没订看不出来，别假装知道
                st.append(r["phase"], style="dim")
                st.append(" 快照", style="dim")
            elif r.get("planned") and not r.get("id"):
                st.append("缺", style="bold yellow")
            elif not r.get("planned"):
                st.append("计划外", style="bold magenta")
            else:
                st.append(r["phase"], style={"进行中": "bold green", "未开始": "white",
                                             "已结束": "dim"}[r["phase"]])
            quiet = r.get("quiet")
            if quiet is None:
                qtxt = "[dim]?[/]"
            elif quiet:
                qtxt = "[bold yellow]ON[/]"
            else:
                qtxt = "[dim]·[/]"
            dur = r.get("durationMin")
            label = Text()
            if idx == live_idx:
                label.append("▶ ", style="bold green")
            label.append(r.get("title") or "", style=style)
            if r.get("matched"):
                # 注意：Text.append **不解析 markup**，所以要给 style 而不是塞 "[cyan]…[/]"
                label.append("  · 已匹配", style="cyan")
            period = r.get("period") or {}
            first = bool(r.get("period_first"))
            pid = period.get("id") if first else ""
            pspan = ("%s–%s" % (period.get("start"), period.get("end"))
                     if first and period else "")
            ptype = (period.get("type") or period.get("calendar") or "") if first else ""
            pname = (period.get("name") or period.get("title") or "") if first else ""
            task_text = r.get("task_text") or ""
            task_cell = Text(task_text, style="bold green" if task_text else "dim")
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
                Text(r.get("contract") or "—", style="dim"),
                label,
                task_cell,
            )
        return t

    def render_status_rows(self):
        """「状态」框的正文：契约执行情况 + 一次性/长期规则 + 临时挪动 + 文件位置。

        用户要求：这一行（原来这里挂的「永不重订名单（N 条）」）换成警告，
        并且「契约（data.csv 周几）」和警告一起装进同一个 ASCII「状态」框。
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
        """重算警告板（每次操作之后 / 最多 5 秒一次；不联网、不写盘）。"""
        now = time.time()
        if not force and self.warnings_at and now - self.warnings_at < max_age:
            return self.warnings
        self.warnings.set_all(self.collect_warnings())
        self.warnings_at = now
        self.warnings_sig = self.warnings.signature()
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

        任何子命令后面都可以跟 ``--assign <表格里的 session ID>``（``-a`` 同义）：
        命令跑完、拿到一条任务之后，顺手把它挂到那一场 session 上。

            task create title=写论文 --assign 3
            task create title=复习 --repeat mon@22:15 --assign 3
            task modify 1 due 后天 --assign 3

        「挂」永远是同一套：先在 Focusmate 建 task 并复核，成功后才落本地
        （见 :meth:`cmd_task_assign`），所以 ``--assign`` 不会产生假成功的半截状态。
        """
        toks = [str(a) for a in args]
        if not toks:
            raise ValueError("用法：task create/delete/modify/assign/select/repeat/sync/archive ...")
        assign_to, kept = self._split_assign(toks)
        if not kept:
            raise ValueError("task 后面要跟动作（create/delete/modify/assign/select/repeat/sync）")
        action = kept[0].lower()
        rest = kept[1:]
        result = self._dispatch_task(action, rest)
        if assign_to is not None:
            self._apply_assign(result, assign_to, action)
        return result

    @staticmethod
    def _split_assign(toks):
        """把 ``--assign N`` / ``--assign=N`` / ``-a N`` 从参数里摘出来。"""
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
        if action == "create":
            return self.cmd_task_create(rest)
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
        raise ValueError("未知 task 动作 %r（create/delete/modify/assign/select/repeat/"
                         "sync/archive）" % action)

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
        task = self.task_store.create(fields, task_id=explicit)
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

    def cmd_task_assign(self, args):
        """task assign <session ID>（默认当前任务）或 task assign <任务ID> <session ID>。

        写入顺序：先在 Focusmate 给那一场建 task 并复核，成功后才落本地；
        失败不产生半截 assignment。
        """
        if not args:
            raise ValueError("用法：task assign <session ID>（先 task select/create）")
        if len(args) >= 2:
            task = self.task_store.get(args[0])
            session_token = args[1]
        else:
            task = self._task_for_assign()
            session_token = args[0]
        session_idx, row = self.pick(session_token)
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
        if not self.offline:
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

    def _cancel_stale_period_sessions(self, period, new_sessions):
        """改 layout 前，把旧排布里已经订上的 API session 对齐掉。

        为什么必须做：data.csv 换成 22:15(25)+22:45(25) 之后，API 里原先那场
        22:15(50) 不会自己消失；daemon 只补订、不擅自杀旧场，于是 50min/25min
        会同时存在。这里在改契约之前先把「起点或时长不再匹配」的场次取消。
        执行区里的取消仍走同一道宣言闸门 —— 不能靠改 layout 绕过 Warden。
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
        self.emit(Panel(info, title="[bold red]⚠ 改排布：旧 session 需要先取消[/]",
                        border_style="red", box=box.HEAVY))

        locked = [r for r in live if r.get("zone") == "locked"]
        if locked:
            if not self.require_declaration(
                    "改 %s 的排布，并取消 %d 场执行区旧 session"
                    % (period.get("id"), len(locked))):
                self.say("没认罪，排布和 session 都没动。", "warn")
                return False

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

    def cmd_period_layout(self, args):
        """period T1 50 50 / session T1 50 50 —— 改一个时段的 session 排布。

        需求里的规则原样实现：
          - 时长只认 25/50/75（Focusmate 的限制）；
          - 第一场从时段起点开始，之后每场从上一场结束后的下一个 15 分钟整点开始；
          - 任何一场超出时段终点 → 直接报错，**layout 和 data.csv 都不动**。

        改排布时若 API 里已有旧 session（例如原来 22:15 50，现在要 22:15 25 +
        22:45 25），先取消不再匹配的旧场，再写新契约。执行区的取消仍要敲宣言。
        """
        toks = [str(a).strip() for a in args if str(a).strip()]
        if not toks:
            raise ValueError("用法：period T1 50 50（时段 ID 看表格第一列）")
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

        # 旧 API session 先对齐，否则 50min / 25min 会并存。
        if not self._cancel_stale_period_sessions(period, new_sessions):
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

        # 从下载层 + 所有 layout + 一次性 + 长期规则重新生成 data.csv。
        # 写在 layout 成功之后。
        if self.rewrite_data_csv_from_plan():
            self.say("data.csv 已按下载层重算（精确日期行）。", "info")

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
            ("refresh  (r)", "重新拉选中那天的 session（会重编号）"),
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
        """repl_kit 在派发前问一句：返回 True=我处理了，None=照常派发。"""
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
            command.handler(parts[1:])
        except SystemExit:
            return False
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

    def run(self, exec_lines=None):
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
