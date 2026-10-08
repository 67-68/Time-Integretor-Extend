#!/usr/bin/env python3
"""warden.py —— Focusmate 狱卒看板（rich TUI）。

四个部分
--------
1. The Warden Status   当前 daemon 状态（Running/Stopped）+ 启停。
                       **停用 = 敲宣言**（和取消 session 共用同一道闸门；密码系统已删除）。
2. The Contract Table  某一天的表：ID | 时间 | 状态 | 静音与否 | 标题 | 契约。
                       用 `day -1 / day 0 / day 1` 切换看哪天。
3. The Interrogation   死循环 input，极简命令：
                       mute 3 / title 2 "文本" / session 09:00 50 / cancel 1 / day 1
4. Panic Action        —— 已按你的要求删掉。没有这个功能。

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
       └── 子进程 ──> keep_session.py check | daemon start | stop（薄封装，不重写业务）

- 写操作（mute / title / cancel）全部走 bridge（内部 API，见 docs/…-notes.md）。
- daemon 的启停、以及 `session` 之后的立刻下单，都走 keep_session.py 子进程
  （不 fork 在自己身上，也不在 TUI 里重写"怎么下单"）。
- **daemon 和 bridge 抢同一个 Chromium profile**：所以 `start` / `session` 之前会先让
  bridge 释放浏览器；bridge 自己也会在空闲 WARDEN_BRIDGE_IDLE_MS 后自动放开。

依赖
----
    python3 warden.py install-deps     # 把 rich 装到 temp/pylibs（不污染系统）
    python3 warden.py                  # 起 TUI

用法
----
    python3 warden.py                          # 交互 TUI
    python3 warden.py --no-clear               # 别清屏（留滚动历史）
    python3 warden.py --exec "day 1; session 09:00 50"   # 跑完命令就退（`;` 分隔）
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
import datetime
import json
import os
import queue
import shlex
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
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.stderr.write(
        "缺少 rich。装一下（只装到项目的 temp/pylibs，不动系统环境）：\n"
        "    python3 %s install-deps\n" % os.path.join(HERE, "warden.py")
    )
    sys.exit(2)

sys.path.insert(0, HERE)
import keep_session as ks  # noqa: E402  只用来读 daemon 状态 / 起停 daemon / 罪与判决
import schedule as sch  # noqa: E402  data.csv 的解析与编辑（唯一实现）

BRIDGE_JS = os.path.join(HERE, "scripts", "bridge.mjs")

# 罪案区的全局锁：有未认罪的 sin 时，除了宣言什么都不受理。
# 上限只影响一轮循环里反复敲错的次数（敲错就继续鸣笛、继续锁）。
SIN_BEEP_LIMIT = 3

# ---------------------------------------------------------------- 小工具


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


# ---------------------------------------------------------------- 看板


class Warden(object):
    def __init__(self, console, bridge, no_clear=False, offline=False, day_offset=0):
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

    # -- 选中的那一天

    @property
    def sel_date(self):
        return datetime.date.today() + datetime.timedelta(days=self.day_offset)

    def reload_entries(self):
        """重读 data.csv（每一次操作前都重读：人手可能在别处改了它）。"""
        self.entries, self.entry_warnings = sch.load_entries()
        self.csv_fp = sch.fingerprint()
        return self.entries

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
        del self.messages[:-12]

    def paint(self):
        # 每次画之前重读 data.csv：契约可能在别处被人手改了（这就是控制面）
        self.reload_entries()
        if not self.no_clear:
            self.console.clear()
        self.console.print(self.render_header())
        self.console.print(self.render_table())
        if self.rows_note:
            self.console.print("[dim]  %s[/]" % self.rows_note)
        self.console.print(self.render_contract())
        if self.messages:
            self.console.print(self.render_messages())
        self.console.print(
            "[dim]命令：[/][bold]mute[/] N on|off · [bold]title[/] N \"文本\" · "
            "[bold]session[/] HH:MM LEN · [bold]cancel[/] N · [bold]day[/] -1/0/1 · "
            "[bold]refresh[/] · [bold]start[/] · [bold]stop[/] · [bold]help[/] · [bold]quit[/]"
        )

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
            row["contract"] = "%s,%s,%s" % (slot["weekday"], slot["start"], slot["duration"])
            row["zone"] = sch.zone_name(start, now)
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
            rows.append(row)

        rows.sort(key=lambda r: r["startMs"])
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

        t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=False)
        t.add_column("ID", justify="right", style="bold", no_wrap=True)
        t.add_column("时间", no_wrap=True)
        t.add_column("时长", justify="right", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("静音", justify="center", no_wrap=True)
        t.add_column("契约", no_wrap=True)
        t.add_column("标题", overflow="fold")

        if not self.rows:
            t.add_row("-", "-", "-", "（这天上既没有契约、也没有 session）", "-", "-", "")
            return t

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
            t.add_row(
                str(idx),
                "%s–%s" % (r["startLabel"], r.get("endLabel") or "?"),
                ("%dmin" % dur) if dur else "?",
                st,
                qtxt,
                Text(r.get("contract") or "—", style="dim"),
                label,
            )
        return t

    def render_contract(self):
        plan = self.plan_summary()
        day = self.sel_date
        txt = Text()
        if plan is None:
            txt.append("契约：", style="bold")
            txt.append("data.csv 里没有 %s 的规则（这天不排 session）"
                       % sch.cn_weekday_of_day(day), style="dim")
        else:
            txt.append("契约（data.csv %s）：" % sch.cn_weekday_of_day(day), style="bold")
            txt.append("计划 %d 场" % plan["total"])
            if plan["booked"] is None:
                txt.append("  · 订没订看不出来（这是 state.json 快照，不是真数据）",
                           style="yellow")
            else:
                txt.append(" · 已落地 %d 场" % plan["booked"])
                missing = plan["missing"]
                if not missing:
                    txt.append("  ✅ 一场不缺", style="bold green")
                elif self.day_zone() in ("draft", "past"):
                    txt.append("  · %d 场还没被吃进去（草稿区/过去，正常）" % len(missing),
                               style="green")
                else:
                    txt.append("  ⚠ 缺 %d 场：" % len(missing), style="bold red")
                    txt.append("、".join("%s(%smin)" % (r["startLabel"], r.get("durationMin") or "?")
                                         for r in missing), style="red")
                    txt.append("   —— daemon 会在下一轮补", style="dim")
        ab = ks.load_verdicts()["abandoned"]
        if ab:
            txt.append("\n已放弃（daemon 永不重订）：", style="bold")
            txt.append("、".join(sorted(ab)[:6]) + ("…" if len(ab) > 6 else ""), style="red")
        txt.append("   [%s]" % sch.data_csv_path(), style="dim")
        return txt

    def render_messages(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=False)
        colors = {"info": "white", "ok": "green", "warn": "yellow", "error": "bold red",
                  "cmd": "bold cyan"}
        for stamp, kind, text in self.messages:
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
        self.console.print(Panel(body, title="[bold red]⛓  The Walk of Shame[/]",
                                 border_style="bold red", box=box.HEAVY))
        tries = 0
        while True:
            try:
                typed = input("  宣言> ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n  [dim]放弃了。[/]")
                return False
            ok, why = check_declaration(typed)
            if ok:
                self.say("✅ 认罪完成（第 %d 次敲对）。" % (tries + 1), "ok")
                return True
            tries += 1
            beep()
            self.console.print("[bold red]✗ 不通过：%s[/]" % why)
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
        self.console.print(Panel(info, title="[bold red]⚠  执行区：取消要过宣言闸门[/]",
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

    def _drop_contract_row(self, idx, row):
        """草稿区：把 data.csv 里那行删掉。零阻力、不问任何问题 —— 这就是「自由」。"""
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

    def cmd_session(self, args):
        """session HH:MM LEN —— 创建一场：先写进契约表（data.csv），再立刻触发一轮 check。

        写出去的是**星期几**规则（data.csv 的格式），所以会影响所有同星期几的周。
        创建零阻力 —— 有阻力的是**取消**。
        下单本身不在这里实现：薄封装 keep_session.py check，业务逻辑一份就够。
        """
        if len(args) < 2:
            raise ValueError("用法：session HH:MM LEN（如 session 09:00 50；LEN 只能 25/50/75）")
        hhmm, dur = args[0], args[1]
        day = self.sel_date
        weekday = sch.weekday_of_day(day)
        lineno = sch.add_row(None, weekday, hhmm, dur)     # 不合法会抛 ValueError
        self.reload_entries()
        self.say("✅ 契约表已写：data.csv 第 %d 行 %s,%s,%s" % (lineno, weekday, hhmm, dur), "ok")
        self.say("   按星期几重复 → 以后每个%s都会有这一场。"
                 % sch.cn_weekday_of_day(day), "warn")

        got = sch.parse_hhmm(hhmm)
        start = datetime.datetime(day.year, day.month, day.day, got[0], got[1])
        zone = sch.zone_name(start, datetime.datetime.now())
        if zone == "draft":
            self.say("   它在草稿区（>24h）：这一轮不会订它，等它滑进执行区会自动被吃。", "info")
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
        self.console.print(Panel(
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
            ("day -1|0|1|YYYY-MM-DD", "切换看哪一天（草稿区随便看）"),
            ("session HH:MM LEN", "创建一场：先写 data.csv，再立刻触发一轮 check"),
            ("cancel N", "取消 #N：草稿区直接删契约；执行区要敲宣言"),
            ("mute N on|off", "把 #N 静音；不写 on/off 就是取反"),
            ('title N "文本"', '把 #N 的标题改成文本；title N "" 清空'),
            ("refresh  (r)", "重新拉选中那天的 session（会重编号）"),
            ("start", "启动 daemon（会先让 bridge 放开浏览器）"),
            ("stop", "停用 daemon（和取消一样要敲宣言）"),
            ("status  (s)", "只重画看板"),
            ("quit  (q)", "退出（bridge 和浏览器一起收掉）"),
        ]:
            t.add_row(cmd, desc)
        self.console.print(Panel(t, title="[bold]命令[/]", border_style="cyan", box=box.ROUNDED))
        self.console.print(
            "[dim]ID 就是表格第一列，按时间排序、每次 refresh 重编号 —— 看着表敲，别凭记忆。\n"
            "区域标记（状态列第一格）：草=草稿区（随便改）· 锁=执行区（取消要敲宣言）"
            "· 过=已过去。[/]"
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

    def handle(self, line):
        """执行一条命令；返回 False 表示该退出了。"""
        try:
            parts = shlex.split(line)
        except ValueError as e:
            self.say("引号没配对：%s" % e, "error")
            return True
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]

        # 罪案区的全局锁：除了敲宣言，什么都不受理。
        sins = self.pending_sins()
        if sins:
            if cmd in ("quit", "exit", "q"):
                self.say("你要走可以，但罪挂在那儿、锁不会解 —— 下次开 TUI 还得敲。", "warn")
                return False
            ok, why = check_declaration(line)
            if ok:
                self.confess(sins)
                self.refresh(quiet=True)
                return True
            beep()
            self.had_error = True
            self.say("锁死中：%s" % (why or "把宣言一字不差地敲完才能做别的事。"), "error")
            self.console.print(Panel(
                Text.assemble((self.lockdown_reason() + "\n\n", "bold red"),
                              ("把下面这段一字不差地敲一遍（少一个标点就继续锁死 + 鸣笛）：\n\n",
                               "bold red"),
                              (DECLARATION, "bold")),
                title="[bold red]⛔  认罪之前，什么都不许做[/]",
                border_style="bold red", box=box.HEAVY,
            ))
            return True

        try:
            if cmd in ("quit", "exit", "q"):
                return False
            if cmd in ("help", "?", "h"):
                self.cmd_help()
            elif cmd in ("refresh", "r", "reload"):
                self.refresh()
            elif cmd in ("status", "s"):
                pass
            elif cmd == "day":
                self.cmd_day(args)
            elif cmd == "session":
                self.cmd_session(args)
            elif cmd == "mute":
                self.cmd_mute(args)
            elif cmd in ("title", "t"):
                self.cmd_title(args)
            elif cmd == "cancel":
                self.cmd_cancel(args)
            elif cmd == "start":
                self.cmd_start()
            elif cmd == "stop":
                self.cmd_stop()
            else:
                self.say("不认识的命令：%s（敲 help 看有哪些）" % cmd, "warn")
        except BridgeError as e:
            self.had_error = True
            self.say("bridge 出错：%s" % e, "error")
        except ValueError as e:
            self.say(str(e), "warn")
        except Exception as e:  # 兜底：别让一条命令把 TUI 掀了
            self.had_error = True
            self.say("意外错误：%s: %s" % (type(e).__name__, e), "error")
        return True

    def run(self, exec_lines=None):
        self.console.print("[dim]起 bridge、拉 session…[/]")
        try:
            self.refresh()
        except Exception as e:
            self.say("首次拉列表失败：%s" % e, "error")
            self.had_error = True

        # 一进来就有未认罪的罪 → 先把话说明白
        reason = self.lockdown_reason()
        if reason:
            self.console.print(Panel(
                Text.assemble(("⛔  这个 TUI 现在是锁死的。\n\n", "bold red"),
                              (reason + "\n\n", "red"),
                              ("唯一能做的事：把下面这段一字不差地敲一遍"
                               "（%d 字符，少一个标点就继续锁死 + 鸣笛）。\n\n"
                               % len(DECLARATION), "bold red"),
                              (DECLARATION, "bold")),
                title="[bold red]The Walk of Shame · 你欠着账[/]",
                border_style="bold red", box=box.HEAVY,
            ))

        if exec_lines:
            self.paint()
            for line in exec_lines:
                if line.strip():
                    self.console.print("[bold cyan]>>>[/] %s" % line)
                    if not self.handle(line):
                        break
                    self.paint()
            return 0 if not self.had_error else 1

        while True:
            self.paint()
            prompt = "宣言> " if self.pending_sins() else "warden> "
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]收工。[/]")
                return 0 if not self.had_error else 1
            if not self.handle(line):
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


# ---------------------------------------------------------------- CLI


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
    ap.add_argument("--no-clear", action="store_true", help="别清屏（留滚动历史）")
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
    console = Console()

    bridge = Bridge(console, headed=args.headed, idle_ms=args.idle_ms,
                    profile_dir=args.profile_dir)
    warden = Warden(console, bridge, no_clear=no_clear, offline=args.offline,
                    day_offset=args.day)

    if args.status:
        # 只画一次：不连浏览器，用 state.json 的快照/本地文件
        warden.offline = True
        warden._fallback_rows("--status 不连浏览器")
        warden.paint()
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
