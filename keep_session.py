#!/usr/bin/env python3
"""keep_session.py —— 后台常驻，确保 data.csv 里列的今天的 Focusmate session 都存在。

架构（改动前先读这里）
----------------------
    ┌─ keep_session daemon（后台常驻，脱离终端）──────────────┐
    │  每 LOOP_INTERVAL=300s 醒一次：                          │
    │    读 data.csv → 一次 API 列出今天所有 session →          │
    │    补齐缺失的（带防抖 + 指数退避）→ 写 state.json + 日志   │
    └──────────────────────────────────────────────────────────┘
                        ▲ 读 state.json / 日志（未来 TUI）
                        │
        keep_session status / log    ← 前台只读，随时开随时关

两种前台入口（都不常驻）：
- `run`   前台常驻循环，stdout 直接看日志，Ctrl+C 干净退出（调试用）
- `check` 只跑一轮就退出（launchd / 手动补跑都能用）

决策（已锁定）
--------------
- 调度：进程内 sleep 循环，每 300s 一轮。不用 LaunchAgent（daemon 自己常驻）。
- 目标语义：仅今天。data.csv 只对运行当天生效，state 里带 date，跨天自动重置。
- 「提前订满」：遍历今天**所有还没开始**的 slot（now < T），没有 session 就订。
  不做「只在前 5 分钟才订」——那是旧版逻辑，已废弃。
- 检查走官方只读 API（GET /v1/sessions），一次调用拿到当天全部 session，
  所以「列一遍看谁没订」很便宜，可以每轮都做。
- 预定必须走 focusmate-mcp 浏览器自动化（Focusmate 官方 API 只读，无下单接口）。
- 失败退避：最多 3 次尝试，指数退避（5min → 15min），第 3 次失败转 failed + 通知。
- 防抖：每次真正下单前随机 sleep DEBOUNCE_BASE ± DEBOUNCE_JITTER 秒（5±3），
  避免规律性的机器行为。
- 手动取消自动重订：每轮以 API 为准——API 里没有、且 state 以为是 booked，
  说明你在 Focusmate 手动取消了，回退为待订并**重置重试计数**（这不是脚本的失败）。
- 状态全部落盘（state.json），前台 TUI 只需读文件，不依赖 daemon 生命周期。

用法：
    python3 keep_session.py daemon start      # 后台常驻（脱离终端，关终端不影响）
    python3 keep_session.py daemon stop
    python3 keep_session.py daemon restart
    python3 keep_session.py daemon status
    python3 keep_session.py run               # 前台常驻循环（Ctrl+C 退出，调试用）
    python3 keep_session.py check             # 只跑一轮
    python3 keep_session.py check --dry-run   # 只打印计划，不调 API 不预定
    python3 keep_session.py status            # 看 daemon 是否活着 + 今日 state + 日志尾

纯 stdlib，Python 3.9 可跑。
"""
import argparse
import datetime
import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main as fm  # noqa: E402  复用 resolve_target / mcp_call / book_once / TOKEN

LOOP_INTERVAL = 300            # daemon 每轮间隔（秒）
DEBOUNCE_BASE = 5              # 下单前基础等待（秒）
DEBOUNCE_JITTER = 3            # 随机抖动 ±（秒）
MAX_ATTEMPTS = 3               # 单个 slot 当天最多尝试预定次数
BACKOFF_MINUTES = [5, 15]      # 第 1、2 次失败后的等待（分钟）；第 3 次失败即放弃

STATE_FILE = os.path.join(HERE, "temp", "keep_session-state.json")
PID_FILE = os.path.join(HERE, "temp", "keep_session.pid")
LOG_FILE = os.path.join(HERE, "temp", "keep_session.log")
STOP_FILE = os.path.join(HERE, "temp", "keep_session.stop")  # 放这个文件 = 优雅退出
LOCK_FILE = os.path.join(HERE, "temp", "keep_session.lock")  # 防 daemon 与手动 check 同时下单

NODE_BIN_DIR = "/opt/homebrew/bin"
# daemon 子进程用的解释器：优先系统 /usr/bin/python3（稳定），否则当前解释器
PYTHON_BIN = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

# daemon 子进程需要的环境（脱离终端后 PATH 极简，显式补上 node）
DAEMON_ENV = dict(os.environ)
DAEMON_ENV["PATH"] = "%s:%s" % (NODE_BIN_DIR, DAEMON_ENV.get("PATH", "/usr/bin:/bin"))
DAEMON_ENV["FOCUSMATE_MCP_JS"] = fm.MCP_SERVER_JS


# ---------------------------------------------------------------- 日志 / 状态


class round_lock(object):
    """文件锁：同一时刻只允许一个进程跑 run_once（daemon 与手动 check 互斥）。

    拿不到锁就返回 False，调用方直接跳过本轮——比阻塞等待更合适，
    因为下单本身要几十秒，等锁只会让手动 check 卡住。
    """

    def __init__(self):
        self.fd = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
            self.fd = open(LOCK_FILE, "w")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.write(str(os.getpid()))
            self.fd.flush()
            return True
        except (OSError, BlockingIOError):
            if self.fd:
                try:
                    self.fd.close()
                except OSError:
                    pass
                self.fd = None
            return False

    def __exit__(self, *exc):
        if self.fd:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
            except OSError:
                pass
            self.fd = None
        return False


def log(msg, echo=True, to_file=True):
    """写日志。to_file=False 时只打到 stdout（daemon 父进程的确认信息用它，
    避免和已经在跑第一轮的孙进程抢同一个日志文件、导致行序错乱）。"""
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if echo:
        print(line, flush=True)
    if not to_file:
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, message):
    """macOS 系统通知。非 macOS 或失败时静默降级。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s' % (json.dumps(message), json.dumps(title))],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def today_str():
    return datetime.date.today().isoformat()


def fmt_local_utc(dt):
    """把本地 datetime 显示成 `MM-DD HH:MM 本地 (HH:MMZ)`，省得心算 -8h。"""
    u = dt.astimezone(datetime.timezone.utc)
    return "%s 本地 (%sZ)" % (dt.strftime("%m-%d %H:%M"), u.strftime("%H:%M"))


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def load_state():
    """返回 {date, slots:{HH:MM: {status, attempts, next_retry_at, last_error, duration}}}。

    日期不是今天就整体重置（「仅今天」语义，无需清理任务）。
    """
    fresh = {"date": today_str(), "slots": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return fresh
    if not isinstance(st, dict) or st.get("date") != today_str():
        return fresh
    st.setdefault("slots", {})
    if not isinstance(st["slots"], dict):
        st["slots"] = {}
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- data.csv


def load_slots(path=None):
    """解析 data.csv：每行 `HH:MM,duration`，# 注释与空行忽略。

    返回 [{"start":"16:30","duration":"50","hour":16,"minute":30}, ...]
    """
    path = path or os.path.join(HERE, fm.SESSION_REGISTRATION_PATH)
    slots = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                log("  [warn] data.csv:%d 格式不对（要 HH:MM,duration），已跳过: %r" % (lineno, raw.strip()))
                continue
            hhmm, duration = parts[0], parts[1]
            try:
                t = datetime.datetime.strptime(hhmm, "%H:%M")
            except ValueError:
                log("  [warn] data.csv:%d 时间格式不对，已跳过: %r" % (lineno, hhmm))
                continue
            if duration not in ("25", "50", "75"):
                log("  [warn] data.csv:%d 时长只能是 25/50/75，已跳过: %r" % (lineno, duration))
                continue
            slots.append({"start": hhmm, "duration": duration,
                          "hour": t.hour, "minute": t.minute})
    return slots


def slot_dt(slot, now):
    """slot 的今天的本地 datetime（用 now 的时区）。"""
    return now.replace(hour=slot["hour"], minute=slot["minute"], second=0, microsecond=0)


# ---------------------------------------------------------------- 只读 API


def fetch_today_sessions(now):
    """一次 API 调用，返回今天所有 session 的 start time（本地时区的 datetime 集合）。

    出错就抛异常，由调用方决定这一轮怎么办。
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(datetime.timezone.utc)
    url = "https://api.focusmate.com/v1/sessions?start=%s&end=%s" % (
        urllib.parse.quote(day_start.isoformat()),
        urllib.parse.quote(day_end.isoformat()),
    )
    req = urllib.request.Request(url, headers={"X-API-Key": fm.TOKEN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.load(resp).get("sessions", [])
    out = []
    for s in raw:
        t = s.get("startTime")
        if not t:
            continue
        try:
            got = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(got.astimezone(now.tzinfo).replace(second=0, microsecond=0))
    return out


def has_session_at(sessions, start_dt):
    """sessions 里有没有和 start_dt 同一分钟的 session。"""
    return any(abs((s - start_dt).total_seconds()) < 60 for s in sessions)


def debounce():
    """下单前随机等 5±3 秒。"""
    secs = DEBOUNCE_BASE + random.uniform(-DEBOUNCE_JITTER, DEBOUNCE_JITTER)
    secs = max(0.5, secs)
    log("    防抖：等 %.1fs 再下单" % secs)
    time.sleep(secs)


def verify_slot_booked(slot, now, attempts=3, wait=4):
    """订完后**以 API 为准**复核这场 session 是否真的存在。

    为什么要复核：focusmate-mcp 的 book_session 抓不到真实 sessionId 时会
    伪造 `temp-<毫秒时间戳>` 并照样返回 success:true（见其 booking.ts）。
    也就是说 payload.success 不可信 —— API 才是 ground truth。

    返回 (ok, detail)。ok=True 表示 API 里确实有这个 slot。
    """
    start = slot_dt(slot, now)
    last = ""
    for i in range(attempts):
        time.sleep(wait)  # booking 落库有延迟，给几秒
        try:
            sessions = fetch_today_sessions(datetime.datetime.now().astimezone())
        except Exception as e:
            last = "复核时 API 出错: %s" % e
            continue
        if has_session_at(sessions, start):
            return True, ""
        last = "API 复核 %d 次仍未出现该 session" % (i + 1)
        log("    [verify] 第 %d 次复核：API 里还没出现，%ds 后再试" % (i + 1, wait))
    return False, last


# ---------------------------------------------------------------- 核心一轮


def run_once(dry_run=False):
    """跑一轮完整检查+补订（带文件锁）。daemon 与手动 check 互斥。"""
    with round_lock() as got:
        if not got:
            log("  [lock] 另一轮正在跑（daemon 或手动 check），本轮跳过")
            return 0
        return _run_once_locked(dry_run=dry_run)


def _run_once_locked(dry_run=False):
    """真正干活的一轮。必须在持有 round_lock 时调用。"""
    now = datetime.datetime.now().astimezone()
    log("=== round start (dry_run=%s) %s ===" % (dry_run, now.strftime("%Y-%m-%d %H:%M:%S")))

    try:
        slots = load_slots()
    except OSError as e:
        log("  data.csv 读不到: %s" % e)
        return 1
    if not slots:
        log("  data.csv 里没有有效目标")
        return 0

    state = load_state()
    changed = False

    # 今天还没开始的 slot（now < T）才需要补订；已经过去的只做展示
    upcoming = []
    past = []
    for slot in slots:
        start = slot_dt(slot, now)
        (upcoming if start > now else past).append((slot, start))

    # 一次 API 拿到今天全部 session（即使没有 upcoming 也要拿，供 status 展示）
    sessions = None
    if not dry_run:
        try:
            sessions = fetch_today_sessions(now)
            log("  今天已有 %d 个 session" % len(sessions))
        except Exception as e:
            log("  API 列 session 失败: %s（本轮跳过，下轮再试）" % e)
            return 1
        # 记录快照，供 status 展示"今天实际有哪些 session"
        state["api_snapshot"] = {
            "at": now.isoformat(),
            "sessions": [s.isoformat() for s in sessions],
        }
        changed = True

    # 已过的 slot：不做任何补订，只把"今天到底有没有"记进 state，
    # 让 status 能显示（否则 status 静默不显示，容易误以为一切正常）。
    for slot, start in past:
        key = slot["start"]
        rec = state["slots"].get(key) or {}
        if dry_run or sessions is None:
            continue
        if has_session_at(sessions, start):
            if rec.get("status") != "booked":
                state["slots"][key] = {"status": "booked", "duration": slot["duration"],
                                       "attempts": rec.get("attempts", 0), "past": True}
                changed = True
        else:
            if rec.get("status") != "missed":
                log("  [已过] %s (%smin) 今天没有这场 session，已无法补订" % (key, slot["duration"]))
                state["slots"][key] = {"status": "missed", "duration": slot["duration"],
                                       "attempts": rec.get("attempts", 0), "past": True}
                changed = True

    if not upcoming:
        log("  今天没有还没开始的 slot（全部已过）")

    for slot, start in upcoming:
        key = slot["start"]
        rec = state["slots"].get(key) or {}
        status = rec.get("status")

        if dry_run:
            present = "?" if sessions is None else ("有" if has_session_at(sessions or [], start) else "无")
            log("  [dry-run] %s (%smin) 在 API 中: %s；state=%s" % (key, slot["duration"], present, status or "无记录"))
            continue

        present = has_session_at(sessions, start)

        if present:
            # API 有 → 确认 booked；若是从 failed/取消 恢复，也一并纠正
            if status != "booked":
                log("  %s 已存在（API 确认），标记 booked" % key)
                state["slots"][key] = {"status": "booked", "duration": slot["duration"],
                                       "attempts": rec.get("attempts", 0)}
                changed = True
            continue

        # API 没有该 session
        if status == "booked":
            # 我们以为是 booked，但 API 说没有 → 你手动取消了 → 重新订，重置尝试次数
            log("  %s 原以为已订，但 API 查不到（疑似手动取消），重新预定" % key)
            rec = {"duration": slot["duration"], "attempts": 0, "status": None}
            state["slots"][key] = rec
            changed = True
        elif status == "failed":
            log("  %s 已达重试上限，今天不再尝试" % key)
            continue

        # 该订了吗？（退避等待）
        nra = parse_iso(rec.get("next_retry_at"))
        if nra and now < nra:
            log("  %s 在退避中，等到 %s 再试" % (key, nra.strftime("%H:%M:%S")))
            continue

        attempts = int(rec.get("attempts", 0))
        if attempts >= MAX_ATTEMPTS:
            log("  %s 已尝试 %d 次仍失败，转 failed + 通知" % (key, attempts))
            state["slots"][key] = {"status": "failed", "duration": slot["duration"],
                                   "attempts": attempts, "last_error": rec.get("last_error")}
            notify("Focusmate 预定失败",
                   "%s (%smin) 尝试 %d 次仍失败，今天不再重试。原因：%s"
                   % (key, slot["duration"], attempts, rec.get("last_error") or "未知"))
            changed = True
            continue

        # 真的去订
        attempts += 1
        log("  %s 缺失，开始第 %d/%d 次预定...（目标时间 %s，%smin）"
            % (key, attempts, MAX_ATTEMPTS, fmt_local_utc(start), slot["duration"]))
        debounce()
        try:
            _target, start_iso = fm.resolve_target(key)
            payload = fm.book_once(start_iso, slot["duration"])
        except Exception as e:
            payload = {"success": False, "error": str(e), "errorCode": "EXCEPTION"}

        if payload.get("success"):
            sess = payload.get("session") or {}
            log("  %s book_session 报成功 (id=%s)，开始 API 复核..."
                % (key, sess.get("id")))
            ok, detail = verify_slot_booked(slot, now)
            if ok:
                log("  %s 复核通过：API 确认该 session 已存在" % key)
                state["slots"][key] = {"status": "booked", "duration": slot["duration"],
                                       "attempts": attempts}
                changed = True
                continue
            # 假成功：MCP 说订上了，API 查不到 → 按失败处理，走退避
            reason = detail or "book_session 报成功但 API 查不到（疑似假成功）"
            log("  %s 复核失败：%s" % (key, reason))
        else:
            reason = payload.get("error") or payload.get("errorCode") or "未知错误"
        next_retry = None
        if attempts < MAX_ATTEMPTS:
            wait = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
            next_retry = now + datetime.timedelta(minutes=wait)
            log("  %s 第 %d 次失败: %s（%d 分钟后重试）" % (key, attempts, reason, wait))
        else:
            log("  %s 第 %d 次失败: %s（已达上限，今天不再试）" % (key, attempts, reason))
            notify("Focusmate 预定失败",
                   "%s (%smin) 尝试 %d 次仍失败：%s。今天不再重试。"
                   % (key, slot["duration"], attempts, reason))
        state["slots"][key] = {
            "status": None if next_retry else "failed",
            "duration": slot["duration"],
            "attempts": attempts,
            "next_retry_at": next_retry.isoformat() if next_retry else None,
            "last_error": reason,
        }
        changed = True

    if changed:
        save_state(state)
    log("=== round done ===")
    return 0


# ---------------------------------------------------------------- 前台循环


_STOP = {"flag": False}


def _handle_stop(signum, frame):
    _STOP["flag"] = True
    log("收到信号 %d，准备退出..." % signum)


def cmd_run(dry_run=False):
    """前台常驻循环。Ctrl+C 干净退出。调试用（正式用 daemon）。"""
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    log("run 启动：每 %ds 一轮，Ctrl+C 退出" % LOOP_INTERVAL)
    while not _STOP["flag"]:
        run_once(dry_run=dry_run)
        # 可被 STOP_FILE 或信号打断的分段睡眠
        slept = 0
        while slept < LOOP_INTERVAL and not _STOP["flag"]:
            if os.path.exists(STOP_FILE):
                log("发现 STOP_FILE，退出")
                _STOP["flag"] = True
                break
            time.sleep(min(1, LOOP_INTERVAL - slept))
            slept += 1
    log("run 已退出")
    return 0


# ---------------------------------------------------------------- daemon


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def cmd_daemon_start(dry_run=False):
    old = read_pid()
    if old and pid_alive(old):
        log("daemon 已在运行 (pid=%d)，不重复启动" % old, to_file=False)
        return 1
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    args = [PYTHON_BIN, os.path.join(HERE, "keep_session.py"), "_daemon_loop"]
    if dry_run:
        args.append("--dry-run")
    # 双重 fork + setsid，脱离控制终端（关终端不影响）
    pid = os.fork()
    if pid > 0:
        # 父进程等子进程把 PID 写好
        for _ in range(50):
            time.sleep(0.1)
            p = read_pid()
            if p and pid_alive(p):
                log("daemon 已启动 (pid=%d)" % p, to_file=False)
                return 0
        log("daemon 启动后没读到 PID，请查日志", to_file=False)
        return 1
    # 第一层子进程
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # 孙进程：真正的 daemon
    try:
        os.setpgid(0, 0)  # 自建进程组（pgid=pid），方便整组杀，避免 node/Chromium 子进程残留
    except OSError:
        pass
    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    out = open(LOG_FILE + ".daemon.out", "a")
    os.dup2(out.fileno(), 1)
    os.dup2(out.fileno(), 2)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    os.chdir(HERE)
    os.environ.update(DAEMON_ENV)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        cmd_run(dry_run=dry_run)
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    os._exit(0)


def cmd_daemon_stop():
    pid = read_pid()
    if not pid or not pid_alive(pid):
        log("daemon 没在运行")
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return 0
    log("给 daemon(pid=%d) 发 SIGTERM..." % pid)
    try:
        # 先杀进程组（覆盖可能的 node/Chromium 子进程），失败再退回到单进程
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            log("杀进程失败: %s" % e)
            return 1
    for _ in range(50):
        if not pid_alive(pid):
            log("daemon 已停止")
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return 0
        time.sleep(0.1)
    log("daemon 没在 5s 内退出，发 SIGKILL")
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    if pid_alive(pid):
        log("SIGKILL 后仍存活，请手动处理 pid=%d" % pid)
        return 1
    log("daemon 已强制停止")
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def cmd_daemon_status():
    pid = read_pid()
    alive = bool(pid and pid_alive(pid))
    print("== daemon ==")
    print("  running: %s%s" % ("是" if alive else "否", " (pid=%d)" % pid if pid else ""))
    if STOP_FILE and os.path.exists(STOP_FILE):
        print("  stop flag: 存在（下轮会退出）")
    return 0


def cmd_daemon(action, dry_run=False):
    if action == "start":
        return cmd_daemon_start(dry_run=dry_run)
    if action == "stop":
        return cmd_daemon_stop()
    if action == "restart":
        cmd_daemon_stop()
        time.sleep(0.3)
        return cmd_daemon_start(dry_run=dry_run)
    if action == "status":
        return cmd_daemon_status()
    print("未知 daemon 动作: %s" % action)
    return 2


# ---------------------------------------------------------------- status 总览


def cmd_status():
    cmd_daemon_status()
    print("== data.csv ==")
    try:
        for s in load_slots():
            print("  %s  %smin" % (s["start"], s["duration"]))
    except OSError as e:
        print("  读取失败: %s" % e)

    print("== today state ==")
    st = load_state()
    snap = st.get("api_snapshot") or {}
    snap_at = snap.get("at")
    if snap_at:
        print("  (API 快照 @%s，今天共 %d 场)"
              % (snap_at[11:19], len(snap.get("sessions", []))))
    if st["slots"]:
        for k in sorted(st["slots"]):
            r = st["slots"][k]
            extra = ""
            if r.get("next_retry_at"):
                extra = " retry@%s" % r["next_retry_at"][11:19]
            if r.get("last_error"):
                extra += " err=%s" % r["last_error"]
            tag = "[已过] " if r.get("past") else ""
            print("  %s%s: %s attempts=%s%s" % (tag, k, r.get("status") or "pending",
                                                r.get("attempts", 0), extra))
    else:
        print("  (空)")

    print("== log tail ==")
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f.read().splitlines()[-20:]:
                print("  " + line)
    except OSError:
        print("  (无日志)")
    return 0


# ---------------------------------------------------------------- CLI


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="确保今天的 Focusmate session 都存在（后台常驻）")
    sub = ap.add_subparsers(dest="cmd", metavar="{run,check,daemon,status}")

    p_run = sub.add_parser("run", help="前台常驻循环（Ctrl+C 退出，调试用）")
    p_run.add_argument("--dry-run", action="store_true")

    p_check = sub.add_parser("check", help="只跑一轮")
    p_check.add_argument("--dry-run", action="store_true")

    p_daemon = sub.add_parser("daemon", help="后台常驻管理")
    p_daemon.add_argument("action", choices=["start", "stop", "restart", "status"])
    p_daemon.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="总览：daemon + data.csv + state + 日志尾")

    # 内部入口：daemon 孙进程真正跑的就是它。不进 help（argparse 没有官方方式
    # 隐藏子命令名，用 subparsers 的 metavar + 过滤方式最省事，这里只置空 help）。
    p_loop = sub.add_parser("_daemon_loop", help=argparse.SUPPRESS)
    p_loop.add_argument("--dry-run", action="store_true")
    # 从 help 列表中彻底移除内部子命令（argparse 没有官方 hide，用它内部结构）
    sub._choices_actions = [a for a in sub._choices_actions if a.dest != "_daemon_loop"]

    ap.set_defaults(cmd="status")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.cmd == "run":
        return cmd_run(dry_run=args.dry_run)
    if args.cmd == "_daemon_loop":
        return cmd_run(dry_run=args.dry_run)
    if args.cmd == "check":
        return run_once(dry_run=args.dry_run)
    if args.cmd == "daemon":
        return cmd_daemon(args.action, dry_run=getattr(args, "dry_run", False))
    if args.cmd == "status":
        return cmd_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
