#!/usr/bin/env python3
"""warden.py —— Focusmate 狱卒看板（rich TUI）。

四个部分，对应四个概念
----------------------
1. The Warden Status   当前 daemon 状态（Running/Stopped）+ 启停。
                       **停用要输密码**（首次停用会让你设一个）。
2. The Contract Table  今天的 session 表：ID | 时间 | 状态 | 静音 | 标题 | 伙伴。
                       底下再补一行「契约执行情况」（data.csv 计划 vs 实际）。
3. The Interrogation   死循环 input，极简命令：
                        mute 3 / mute 3 off / title 2 "Math Past Paper" / cancel 1
4. Panic Action        —— 已按你的要求删掉。没有这个功能。

架构（重要）
------------
    warden.py ──JSON 行──> scripts/bridge.mjs ──Playwright──> 常驻 Chromium
       ▲                                                          │
       └── 只读文件 ──> temp/keep_session-state.json / pid / log <──┘

- 写操作（mute / title / cancel）全部走 bridge（内部 API，见 docs/…-notes.md）。
- daemon 的启停走 `keep_session.py daemon start|stop` 子进程（不 fork 在自己身上）。
- **daemon 和 bridge 抢同一个 Chromium profile**：所以 `start` 之前会先让 bridge
  释放浏览器；bridge 自己也会在空闲 WARDEN_BRIDGE_IDLE_MS 后自动放开。

依赖
----
    python3 warden.py install-deps     # 把 rich 装到 temp/pylibs（不污染系统）
    python3 warden.py                  # 起 TUI

用法
----
    python3 warden.py                          # 交互 TUI
    python3 warden.py --no-clear               # 别清屏（留滚动历史）
    python3 warden.py --exec "mute 3 on"       # 跑完命令就退，不进交互（可 `;` 分隔多条）
    python3 warden.py --set-password           # 只设/改停用密码
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
import getpass
import hashlib
import hmac
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
import keep_session as ks  # noqa: E402  只用来读 daemon 状态 / 起停 daemon

BRIDGE_JS = os.path.join(HERE, "scripts", "bridge.mjs")
WARDEN_JSON = os.path.join(TEMP, "warden.json")

# cancel 的冷静期（秒）。地狱级阻力的最后一道闸。
CANCEL_COOLDOWN = 6
# 密码最多试几次
PASSWORD_ATTEMPTS = 3

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


# ---------------------------------------------------------------- 停用密码


def password_problems(pw):
    """返回一串「哪里不够复杂」；空列表 = 通过。

    规则（你定的）：至少 12 字符，且 小写/大写/数字/符号 里至少占 3 类。
    """
    problems = []
    if len(pw) < 12:
        problems.append("至少 12 个字符（现在是 %d）" % len(pw))
    classes = [
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any((not c.isalnum()) for c in pw),
    ]
    n = sum(1 for c in classes if c)
    if n < 3:
        problems.append("小写/大写/数字/符号 至少占 3 类（现在 %d 类）" % n)
    return problems


def _hash_password(pw, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)


def load_password_record():
    try:
        with open(WARDEN_JSON, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or "hash" not in rec or "salt" not in rec:
        return None
    return rec


def save_password(pw):
    salt = os.urandom(16)
    iterations = 260000
    rec = {
        "version": 1,
        "algo": "pbkdf2_hmac_sha256",
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": _hash_password(pw, salt, iterations).hex(),
        "created": now_local().isoformat(timespec="seconds"),
    }
    os.makedirs(TEMP, exist_ok=True)
    tmp = WARDEN_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, WARDEN_JSON)
    return rec


def verify_password(pw):
    rec = load_password_record()
    if not rec:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
        want = bytes.fromhex(rec["hash"])
        iterations = int(rec.get("iterations", 260000))
    except (ValueError, TypeError):
        return False
    got = _hash_password(pw, salt, iterations)
    return hmac.compare_digest(got, want)


def read_secret(prompt):
    """单独抽出来是为了测试能替换掉它（getpass 在管道里读不了）。"""
    return getpass.getpass(prompt)


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
    def __init__(self, console, bridge, no_clear=False, offline=False):
        self.console = console
        self.bridge = bridge
        self.no_clear = no_clear
        self.offline = offline
        self.messages = []
        self.rows = []
        self.rows_source = ""
        self.rows_note = ""
        self.rows_at = 0
        self.had_error = False
        self.bridge_note = ""

    # -- 输出

    def say(self, text, kind="info"):
        stamp = now_local().strftime("%H:%M:%S")
        self.messages.append((stamp, kind, text))
        del self.messages[:-12]

    def paint(self):
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
            "[bold]cancel[/] N · [bold]refresh[/] · [bold]start[/] · [bold]stop[/] · "
            "[bold]help[/] · [bold]quit[/]"
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
        """拉一次今天的 session 列表。

        失败时**不要**拿退化快照去覆盖已有好数据 —— 快照里没有 session id，
        覆盖了后面 mute/title/cancel 全都做不了，还会让人困惑。
        """
        if self.offline:
            if not self.rows:
                self._fallback_rows("离线模式")
            return False
        try:
            with self.console.status("[yellow]问 Focusmate 要今天的 session…[/]", spinner="dots"):
                res = self.bridge.call("snapshot", timeout=timeout)
        except BridgeError as e:
            self.had_error = True
            if self.rows and self.rows_source == "bridge":
                self.say("拉列表失败（表格保持上一次的结果）：%s" % e, "error")
            else:
                self._fallback_rows("bridge 出错")
                self.say("拉列表失败：%s" % e, "error")
            return False

        now_ms = res.get("now") or int(time.time() * 1000)
        rows = []
        for s in res.get("sessions") or []:
            row = dict(s)
            row["phase"] = self.phase_of(s["startMs"], s["endMs"], now_ms)
            rows.append(row)
        self.rows = rows
        self.rows_source = "bridge"
        self.rows_note = ""
        self.rows_at = time.time()
        if not quiet:
            self.say("拉到 %d 场（%s）" % (len(rows), res.get("date")))
        return True

    def _fallback_rows(self, why):
        """拿不到 bridge 时，用 daemon 写下的 state.json 快照（只有时间，没有标题/静音）。"""
        st = ks.load_state()
        snap = st.get("api_snapshot") or {}
        iso = snap.get("sessions") or []
        now_ms = int(time.time() * 1000)
        today = datetime.date.today().isoformat()
        rows = []
        for t in iso:
            try:
                dt = datetime.datetime.fromisoformat(t)
            except ValueError:
                continue
            # daemon 的快照是用 UTC 日界捞的，跨天时会带上本地上一天的 23:15，滤掉
            if dt.date().isoformat() != today:
                continue
            s_ms = int(dt.timestamp() * 1000)
            # 快照里没有 duration，先用 50min 占位；这个值只影响显示
            e_ms = s_ms + 50 * 60 * 1000
            end = dt + datetime.timedelta(minutes=50)
            rows.append({
                "startMs": s_ms,
                "endMs": e_ms,
                "startLabel": dt.strftime("%H:%M"),
                "endLabel": end.strftime("%H:%M"),
                "durationMin": None,
                "id": None,
                "title": "",
                "quiet": None,
                "matched": None,
                "phase": self.phase_of(s_ms, e_ms, now_ms),
            })
        rows.sort(key=lambda r: r["startMs"])
        self.rows = rows
        self.rows_source = "snapshot"
        self.rows_note = "来自 keep_session 的 state.json 快照（%s）——只有时间，没有标题/静音/伙伴" % why
        self.rows_at = time.time()

    @staticmethod
    def phase_of(start_ms, end_ms, now_ms):
        if now_ms < start_ms:
            return "未开始"
        if now_ms < end_ms:
            return "进行中"
        return "已结束"

    def plan_summary(self):
        """data.csv（契约）vs 实际有订的场次。"""
        try:
            slots = quiet_call(ks.load_slots, None, ks.today_weekday())[0]
        except Exception:
            return None
        if not slots:
            return None
        booked = set()
        for r in self.rows:
            booked.add(r["startLabel"])
        missing = [s for s in slots if s["start"] not in booked]
        return {"total": len(slots), "booked": len(slots) - len(missing), "missing": missing}

    # -- 渲染

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
        t.add_row("今天", Text(now_local().strftime("%Y-%m-%d %H:%M:%S"), style="bold"))
        return Panel(t, title="[bold]The Warden[/]", border_style="cyan", box=box.ROUNDED)

    def render_table(self):
        title = "The Contract Table · 今天"
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
        t.add_column("伙伴", no_wrap=True)
        t.add_column("标题", overflow="fold")

        if not self.rows:
            t.add_row("-", "-", "-", "-", "-", "-", "[dim]（没有）[/]")
            return t

        live_idx = None
        for i, r in enumerate(self.rows):
            if r["phase"] == "进行中":
                live_idx = i + 1
                break
        for i, r in enumerate(self.rows):
            idx = i + 1
            style = "dim" if r["phase"] == "已结束" else ""
            phase_style = {"进行中": "bold green", "未开始": "white", "已结束": "dim"}[r["phase"]]
            quiet = r.get("quiet")
            if quiet is None:
                qtxt = "[dim]?[/]"
            elif quiet:
                qtxt = "[bold yellow]ON[/]"
            else:
                qtxt = "[dim]·[/]"
            matched = r.get("matched")
            if matched is None:
                ptxt = "[dim]?[/]"
            elif matched:
                ptxt = "[cyan]已匹配[/]"
            else:
                ptxt = "[dim]待匹配[/]"
            dur = r.get("durationMin")
            label = Text()
            if idx == live_idx:
                label.append("▶ ", style="bold green")
            label.append(r.get("title") or "", style=style)
            t.add_row(
                str(idx),
                "%s–%s" % (r["startLabel"], r.get("endLabel") or "?"),
                ("%dmin" % dur) if dur else "?",
                Text(r["phase"], style=phase_style),
                qtxt,
                ptxt,
                label,
            )
        return t

    def render_contract(self):
        plan = self.plan_summary()
        if not plan:
            return Text("")
        txt = Text("契约（data.csv 今天）：", style="bold")
        txt.append("计划 %d 场 · 已落地 %d 场" % (plan["total"], plan["booked"]))
        if not plan["missing"]:
            txt.append("  ✅ 一场不缺", style="bold green")
        else:
            txt.append("  ⚠ 缺 %d 场：" % len(plan["missing"]), style="bold red")
            txt.append("、".join("%s(%smin)" % (s["start"], s["duration"]) for s in plan["missing"]),
                       style="red")
            txt.append("   —— daemon 会在下一轮补", style="dim")
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
        """地狱级阻力：看清目标 → 手打时间 → 冷静期 → 才真取消。"""
        if not args:
            raise ValueError("用法：cancel N")
        idx, row = self.pick(args[0])
        if row.get("id") is None:
            raise ValueError("这场是从 state.json 快照来的，没有 session id，取消不了。先 refresh 成功一次。")
        if row["phase"] == "已结束":
            raise ValueError("#%d 已经结束了，没法取消（Focusmate 也不让你取消过去的场次）" % idx)

        matched = row.get("matched")
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True)
        info.add_column()
        info.add_row("目标", "#%d  %s–%s（%s）" % (idx, row["startLabel"],
                                                row.get("endLabel") or "?", row["phase"]))
        info.add_row("标题", row.get("title") or "[dim]（无）[/]")
        info.add_row("伙伴", "[cyan]已匹配[/]" if matched else "[dim]还没匹配[/]")
        info.add_row("session", (row["id"] or "")[:8] + "…")

        self.console.print(Panel(
            info,
            title="[bold red]⚠  取消 session —— 不可撤销[/]",
            border_style="bold red",
            box=box.HEAVY,
        ))
        warn = Text()
        if matched:
            warn.append("这场已经匹配到人了。取消 = 对方被放鸽子，会收到通知，"
                        "而且你的 attRate（出席率）会掉。\n", style="bold red")
        warn.append("Focusmate 的内部接口没有网页那层二次确认，说取消就取消了，脚本也救不回来。",
                    style="red")
        self.console.print(warn)

        try:
            typed = input("\n  手打这个开始时间以确认（%s），其它任何输入都会放弃： " % row["startLabel"])
        except (EOFError, KeyboardInterrupt):
            self.console.print("\n  放弃了。")
            return False
        if typed.strip() != row["startLabel"]:
            self.say("输入不匹配（%r ≠ %r），已放弃，什么都没动。" % (typed.strip(), row["startLabel"]),
                     "warn")
            return False

        self.console.print("\n  冷静期 %d 秒 —— Ctrl+C 还来得及。" % CANCEL_COOLDOWN)
        try:
            for left in range(CANCEL_COOLDOWN, 0, -1):
                self.console.print("    [bold red]%d[/]" % left, end=" ")
                time.sleep(1)
            self.console.print()
        except KeyboardInterrupt:
            self.console.print("\n  中断了，什么都没动。")
            self.say("冷静期内被 Ctrl+C，已放弃取消。", "warn")
            return False

        self.say("#%d 取消中…（%s）" % (idx, row["startLabel"]), "cmd")
        with self.console.status("[red]取消中…[/]", spinner="dots"):
            res = self.bridge.call("cancel", startMs=row["startMs"], meetingId=row["id"])
        if res.get("verified"):
            self.say("✅ #%d %s 已取消（API 复核：该时段已消失）" % (idx, row["startLabel"]), "ok")
        else:
            self.had_error = True
            self.say("❌ #%d 取消后复核：该 session 还在", "error")
        self.refresh(quiet=True)
        return True

    def cmd_start(self):
        up, browser, _ = self.bridge.browser_state()
        if browser:
            self.say("先让 bridge 把浏览器放开（daemon 下单要独占同一个 Chromium profile）…", "info")
            try:
                self.bridge.call("release", timeout=60)
            except BridgeError as e:
                self.say("释放浏览器失败（不影响启动，但可能撞锁）：%s" % e, "warn")
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
        """停用 daemon —— 必须过密码这一关。"""
        rec = load_password_record()
        if not rec:
            self.console.print(Panel(
                "还没设过停用密码。\n\n"
                "daemon 是你今天 session 的保命绳，随手停掉 = 剩下的场次没人补。\n"
                "所以第一次停用要先设一个够复杂的密码（至少 12 字符，"
                "小写/大写/数字/符号里至少占 3 类）。\n"
                "密码只存本地哈希：[bold]temp/warden.json[/]（temp/ 已在 .gitignore，不进 git）。",
                title="[bold yellow]首次停用 · 先设密码[/]",
                border_style="yellow", box=box.ROUNDED,
            ))
            try:
                pw1 = read_secret("  新密码: ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n  放弃了。")
                return False
            problems = password_problems(pw1)
            if problems:
                self.had_error = True
                for p in problems:
                    self.say("密码不合格：%s" % p, "error")
                return False
            try:
                pw2 = read_secret("  再输一次: ")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n  放弃了。")
                return False
            if pw1 != pw2:
                self.had_error = True
                self.say("两次输入不一样，没设成。", "error")
                return False
            save_password(pw1)
            self.say("✅ 停用密码已设好（temp/warden.json）。这次就按你刚设的密码放行。", "ok")
        else:
            for attempt in range(1, PASSWORD_ATTEMPTS + 1):
                try:
                    pw = read_secret("  停用密码: ")
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\n  放弃了。")
                    return False
                if verify_password(pw):
                    break
                self.had_error = True
                left = PASSWORD_ATTEMPTS - attempt
                self.say("密码不对。%s" % ("还能试 %d 次" % left if left else "不给试了。"), "error")
                if not left:
                    return False
                time.sleep(2)  # 手速攻击也别太舒服

        self.say("停 daemon…", "cmd")
        rc, out, err = run_keep_session("daemon", "stop")
        for line in (out or "").splitlines():
            self.say(line)
        if rc == 0:
            self.say("✅ daemon 已停止（桥还活着，随时能改 session）", "ok")
        else:
            self.had_error = True
            self.say("❌ daemon 停止失败（rc=%d）%s" % (rc, err), "error")
        return rc == 0

    def cmd_help(self):
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True, justify="right")
        t.add_column()
        for cmd, desc in [
            ("mute N on|off", "把 #N 静音；不写 on/off 就是取反"),
            ('title N "文本"', '把 #N 的标题改成文本；title N "" 清空'),
            ("cancel N", "取消 #N（要手打时间 + 冷静期）"),
            ("refresh  (r)", "重新拉今天的 session（会重编号）"),
            ("start", "启动 daemon（会先让 bridge 放开浏览器）"),
            ("stop", "停用 daemon（要密码）"),
            ("status  (s)", "只重画看板"),
            ("set-password", "改停用密码"),
            ("quit  (q)", "退出（bridge 和浏览器一起收掉）"),
        ]:
            t.add_row(cmd, desc)
        self.console.print(Panel(t, title="[bold]命令[/]", border_style="cyan", box=box.ROUNDED))
        self.console.print(
            "[dim]ID 就是表格第一列，按时间排序、每次 refresh 重编号——看着表敲，别凭记忆。[/]"
        )
        return False

    def cmd_set_password(self):
        if not load_password_record():
            self.say("还没设过密码，直接设就行。", "info")
        else:
            try:
                old = read_secret("  当前密码: ")
            except (EOFError, KeyboardInterrupt):
                return False
            if not verify_password(old):
                self.had_error = True
                self.say("当前密码不对。", "error")
                return False
        try:
            pw1 = read_secret("  新密码: ")
            pw2 = read_secret("  再输一次: ")
        except (EOFError, KeyboardInterrupt):
            return False
        problems = password_problems(pw1)
        if problems:
            self.had_error = True
            for p in problems:
                self.say("密码不合格：%s" % p, "error")
            return False
        if pw1 != pw2:
            self.had_error = True
            self.say("两次输入不一样。", "error")
            return False
        save_password(pw1)
        self.say("✅ 停用密码已更新。", "ok")
        return True

    # -- 循环

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
        try:
            if cmd in ("quit", "exit", "q"):
                return False
            if cmd in ("help", "?", "h"):
                self.cmd_help()
            elif cmd in ("refresh", "r", "reload"):
                self.refresh()
            elif cmd in ("status", "s"):
                pass
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
            elif cmd == "set-password":
                self.cmd_set_password()
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
        self.console.print("[dim]起 bridge、拉今天的 session…[/]")
        try:
            self.refresh()
        except Exception as e:
            self.say("首次拉列表失败：%s" % e, "error")
            self.had_error = True

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
            try:
                line = input("warden> ")
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


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Focusmate 狱卒看板（rich TUI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----\n", 1)[-1] if "用法\n----\n" in __doc__ else None,
    )
    ap.add_argument("--exec", dest="exec_", metavar="CMDS",
                    help='跑完这些命令就退（`;` 分隔），如 --exec "mute 3 on;refresh"')
    ap.add_argument("--status", action="store_true", help="只画一次看板然后退出（不拉浏览器列表）")
    ap.add_argument("--set-password", action="store_true", help="只设/改停用密码")
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

    if args.set_password:
        w = Warden(console, Bridge(console), no_clear=True, offline=True)
        w.cmd_set_password()
        return 0 if not w.had_error else 1

    bridge = Bridge(console, headed=args.headed, idle_ms=args.idle_ms,
                    profile_dir=args.profile_dir)
    warden = Warden(console, bridge, no_clear=no_clear, offline=args.offline)

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
