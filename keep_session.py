#!/usr/bin/env python3
"""keep_session.py —— 后台常驻，把「未来 24 小时」的契约死死锁进 Focusmate。

架构（改动前先读这里）
----------------------
    ┌─ keep_session daemon（后台常驻，脱离终端）────────────────────┐
    │  每 LOOP_INTERVAL=300s 醒一次：                               │
    │    data.csv → 切出 (now, now+24h] 的执行区 → 一次 API 列出窗口 │
    │    内的 session → 补齐缺失的（防抖 + 指数退避）→ 写 state+日志 │
    │                                                               │
    │  两轮之间的睡眠里每 IDLE_POLL_SEC=5s 跑一次「分心看门狗」：     │
    │    session 进行中按当前模式（普通 60s / 做题 180s / 写作 600s）  │
    │    没输入 → 记一次违规 + 通知；做题/写作在违规前 30s 先预警。   │
    │    普通第 3 次、做题/写作第 1 次：音量拉到 10% 播放告警音频。    │
    │    暂停期间不念；模式由 warden 专注视图的 Shift+Tab 切换。       │
    └───────────────────────────────────────────────────────────────┘
        ▲ 读 state / verdicts / sins / idle / pause / 日志（warden TUI 只读文件）
        │
        keep_session status / check    ← 前台，随时开随时关

窗口语义（核心）
----------------
    「执行区」= (now, now+24h]    ← 只订这里面的；取消要过 warden 的宣言闸门
    「草稿区」= now+24h 之后      ← 还是草稿，人手随便改 data.csv
    「过去」  = 今天已开始的       ← 只对账：记 booked / missed / sin，不补订

滚动 24 小时意味着**每天傍晚就把第二天早上的场次订掉了**（＝「T+1 锁入」），
而且不需要 23:59 的定时任务：机器合盖睡了也没事，醒来那一轮会把窗口重新填满。

两种前台入口（都不常驻）：
- `run`   前台常驻循环，stdout 直接看日志，Ctrl+C 干净退出（调试用）
- `check` 只跑一轮就退出（launchd / 手动补跑都能用）

决策（已锁定）
--------------
- 调度：进程内 sleep 循环，每 300s 一轮。不用 LaunchAgent（daemon 自己常驻）。
- 目标语义：滚动 24 小时窗口（见上）。data.csv 的解析/编辑全在 schedule.py，
  本模块不重复实现一遍 —— daemon 和 TUI 必须共用同一份语义。
- 检查走官方只读 API（GET /v1/sessions），一次调用拿到窗口内全部 session，
  所以「列一遍看谁没订」很便宜，可以每轮都做。
- 预定必须走 focusmate-mcp 浏览器自动化（Focusmate 官方 API 只读，无下单接口）。
- 失败退避：最多 3 次尝试，指数退避（5min → 15min），第 3 次失败转 failed + 通知。
- 防抖：每次真正下单前随机 sleep DEBOUNCE_BASE ± DEBOUNCE_JITTER 秒（5±3），
  避免规律性的机器行为。
- 手动取消：自动重订（守擂），**并且记一笔 sin**——API 里没有、而 state 以为是
  booked，说明你在 Focusmate 手动取消了。机器读不了你的心，所以它继续重订；
  罪记录进 warden-sins.json，warden TUI 会因此**锁死**，直到你敲完 152 字宣言。
- 认罪后的判决（keep_session-verdicts.json 的 abandoned）：TUI 认罪时把这个
  slot 判成「已放弃」，daemon 从此不再重订它 —— 这才是宣言真正的牙齿。
- 状态全部落盘（state / verdicts / sins），前台 TUI 只读文件，不依赖 daemon 生命周期。
- 分心看门狗：session 进行中每个「整整一分钟」没输入就记一次违规 + 通知，到第 3 次
  把音量拉到 10% 并放音频。人在动就把本段空闲清零重数（累计违规不归零）；暂停期间
  不打扰。读不到空闲时间就不判（不误伤）。见 feature_intents/idle_watchdog.md。

用法：
    python3 keep_session.py daemon start      # 后台常驻（脱离终端，关终端不影响）
    python3 keep_session.py daemon stop
    python3 keep_session.py daemon restart
    python3 keep_session.py daemon status
    python3 keep_session.py run               # 前台常驻循环（Ctrl+C 退出，调试用）
    python3 keep_session.py check             # 只跑一轮
    python3 keep_session.py check --dry-run   # 只打印计划，不调 API 不预定
    python3 keep_session.py check --force     # 忽略重试上限/退避，把所有没订上的都试一次
    python3 keep_session.py status            # 看 daemon 是否活着 + 今日 state + 日志尾

纯 stdlib，Python 3.9 可跑。
"""
import argparse
import datetime
import fcntl
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main as fm  # noqa: E402  复用 resolve_target / mcp_call / book_once / TOKEN
import schedule as sch  # noqa: E402  data.csv 的解析/编辑：唯一实现（TUI 也 import 它）

LOOP_INTERVAL = 300            # daemon 每轮间隔（秒）
DEBOUNCE_BASE = 5              # 下单前基础等待（秒）
DEBOUNCE_JITTER = 3            # 随机抖动 ±（秒）
MAX_ATTEMPTS = 3               # 单个 slot 当天最多尝试预定次数
BACKOFF_MINUTES = [5, 15]      # 第 1、2 次失败后的等待（分钟）；第 3 次失败即放弃
WINDOW_HOURS = sch.WINDOW_HOURS  # 执行区长度：滚动 24 小时（改这里就改了命运）

STATE_FILE = os.path.join(HERE, "temp", "keep_session-state.json")
PID_FILE = os.path.join(HERE, "temp", "keep_session.pid")
LOG_FILE = os.path.join(HERE, "temp", "keep_session.log")
STOP_FILE = os.path.join(HERE, "temp", "keep_session.stop")  # 放这个文件 = 优雅退出
LOCK_FILE = os.path.join(HERE, "temp", "keep_session.lock")  # 防 daemon 与手动 check 同时下单
VERDICTS_FILE = os.path.join(HERE, "temp", "keep_session-verdicts.json")  # 认罪判决
SINS_FILE = os.path.join(HERE, "temp", "warden-sins.json")        # 待认罪的罪
SINS_ARCHIVE = os.path.join(HERE, "temp", "warden-sins.jsonl")    # 认过的罪（只增不删）

# ---- 分心看门狗（daemon 在「session 进行中」额外跑的一个检查）----------------
# 每个模式有一条「连续无输入多久算一次违规」的阈值，以及自己的 max_strikes；
# 到 max_strikes 次时把系统音量拉到 IDLE_ALERT_VOLUME% 并播放告警音频。做题/写作
# 模式在违规之前还有一次不计入违规次数的预警。用 KEEP_SESSION_IDLE_CHECK=0 关掉。
IDLE_CHECK = os.environ.get("KEEP_SESSION_IDLE_CHECK", "1").strip().lower() not in (
    "0", "false", "no", "off", "")
IDLE_POLL_SEC = 5                # daemon 隔多久看一眼空闲时间（秒）
IDLE_MAX_STRIKES = 3             # 兜底值；真正惩罚次数用模式里的 max_strikes
IDLE_ALERT_AUDIO = os.path.join(HERE, "data", "audio", "赵小六嘶吼.mp3")
IDLE_ALERT_VOLUME = 10           # 「把音量拉到 10%」

# 专注模式的数据集。字段说明：
#   threshold_sec   连续无输入多少秒 = 一次违规
#   warn            违规前是否先发一次预警通知（预警不算违规次数）
#   warn_before_sec 若有预警，在违规前多少秒发；每个违规周期都可以刷新一次
#   max_strikes     累计到第几次违规触发惩罚（普通 3 次；做题/写作 1 次）
#   desc            给 TUI / status 用的人话说明
# 新增模式只需在这里加一条，并把它挂进 IDLE_MODE_ORDER；daemon 每 tick 都会
# 从文件里重新解析当前模式，所以改完不用 restart daemon。
IDLE_MODES = {
    "normal": {
        "key": "normal",
        "name": "普通",
        "threshold_sec": 60,
        "warn": False,
        "warn_before_sec": 0,
        "max_strikes": 3,
        "desc": "1 分钟没输入 = 违规；3 次后惩罚",
    },
    "study": {
        "key": "study",
        "name": "做题",
        "threshold_sec": 180,
        "warn": True,
        "warn_before_sec": 30,
        "max_strikes": 1,
        "desc": "3 分钟没输入 = 违规；违规前 30s 预警；1 次就直接惩罚",
    },
    "writing": {
        "key": "writing",
        "name": "写作",
        "threshold_sec": 600,
        "warn": True,
        "warn_before_sec": 30,
        "max_strikes": 1,
        "desc": "10 分钟没输入 = 违规；违规前 30s 预警；1 次就直接惩罚",
    },
}
IDLE_MODE_ORDER = ["normal", "study", "writing"]
DEFAULT_IDLE_MODE = "normal"
# 旧常量保留为普通模式阈值，外部/测试还在引用；真正判定用模式里的 threshold_sec。
IDLE_THRESHOLD_SEC = IDLE_MODES["normal"]["threshold_sec"]

IDLE_STATE_FILE = os.path.join(HERE, "temp", "keep_session-idle.json")  # 违规计数（按 session）
IDLE_MODE_FILE = os.path.join(HERE, "temp", "keep_session-mode.json")    # 当前专注模式（TUI/daemon 共用）
# 暂停：专注视图里用 `stop` 消耗一次，暂停 PAUSE_MINUTES 分钟、全天最多 3 次。
# daemon 在暂停期间不念你；次数/到期时间落盘，warden 和 daemon 共用。
PAUSE_FILE = os.path.join(HERE, "temp", "keep_session-pause.json")
PAUSE_MAX_PER_DAY = 3
PAUSE_MINUTES = 5
# ioreg 在 /usr/sbin；daemon 脱离终端后 PATH 极简，写死绝对路径
IOREG_BIN = "/usr/sbin/ioreg" if os.path.exists("/usr/sbin/ioreg") else "ioreg"

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
    """macOS 系统通知。走 alert.py（非 macOS / 失败时静默降级）。

    alert.py 是「出声 / 通知」的唯一实现，这里保留一个 osascript 兜底，
    免得 alert.py 万一不可用就连通知都没了。

    注意 json.dumps 必须 ensure_ascii=False：AppleScript 不认 `\\uXXXX` 转义，
    默认的 ensure_ascii=True 会把中文变成 `\\u4f60...`，osascript 语法错、通知弹不出来。
    """
    if sys.platform != "darwin":
        return
    try:
        import alert  # 同目录，纯 stdlib
        if alert.notify(title, message) == 0:
            return
    except Exception:
        pass
    # 兜底：alert.py 不可用时自己调 osascript
    try:
        script = "display notification %s with title %s" % (
            json.dumps(message, ensure_ascii=False),
            json.dumps(title, ensure_ascii=False))
        subprocess.run(
            ["osascript", "-e", script],
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
    """返回 {date, slots:{"2026-10-09T09:00": {status, attempts, duration, ...}}}。

    key 带日期：滚动窗口同时管今天和明天，所以不能「不是今天就整体重置」。
    改成**剪枝** —— 丢掉今天以前的记录，今天/明天的留着。跨天自动瘦身，无需清理任务。
    """
    fresh = {"date": today_str(), "slots": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return fresh
    if not isinstance(st, dict):
        return fresh
    st.setdefault("slots", {})
    if not isinstance(st["slots"], dict):
        st["slots"] = {}
    # 只留"今天及以后"的记录。**别用字符串比较**：旧格式的 key 是 "20:00"，
    # 而 "20:00" > "2026-10-08" 在字典序上成立（':' 排在数字后面），会漏进来。
    today = datetime.date.today()
    keep = {}
    for k, v in st["slots"].items():
        if not isinstance(k, str) or len(k) < 16 or k[4] != "-" or k[10] != "T":
            continue
        try:
            kd = datetime.date.fromisoformat(k[:10])
        except ValueError:
            continue
        if kd >= today:
            keep[k] = v
    st["slots"] = keep
    st["date"] = today_str()
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- 判决 / 罪


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return default
    return got if isinstance(got, dict) else default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_verdicts():
    """认罪判决：{"abandoned": {"2026-10-09T09:00": {...}}}。

    abandoned = 你在 TUI 里敲完宣言、正式放弃的那一场。daemon 读它并且**永不重订**
    —— 否则宣言就只是一段表演。只有 warden 认罪时才会写它，daemon 自己绝不判。
    """
    v = _load_json(VERDICTS_FILE, {})
    v.setdefault("abandoned", {})
    if not isinstance(v["abandoned"], dict):
        v["abandoned"] = {}
    return v


def save_verdicts(v):
    _save_json(VERDICTS_FILE, v)


def is_abandoned(key, verdicts=None):
    return bool((verdicts if verdicts is not None else load_verdicts())["abandoned"].get(key))


def mark_abandoned(key, why="walk-of-shame", note=""):
    v = load_verdicts()
    if key in v["abandoned"]:
        return v["abandoned"][key]
    rec = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
           "why": why, "note": note}
    v["abandoned"][key] = rec
    save_verdicts(v)
    return rec


def load_sins():
    """待认罪的罪：{"sins": [{key, what, at, note}]}。"""
    s = _load_json(SINS_FILE, {})
    s.setdefault("sins", [])
    if not isinstance(s["sins"], list):
        s["sins"] = []
    return s


def record_sin(key, what, note=""):
    """记一笔罪。幂等：同一 (key, what) 只记一次。

    触发点只有一个 —— state 以为是 booked，但 API 里查不到，也就是
    **你在 Focusmate 手动取消了**。daemon 照旧重订（守擂，机器读不了你的心），
    同时把罪落盘，等 TUI 锁死你来收。
    """
    s = load_sins()
    for old in s["sins"]:
        if old.get("key") == key and old.get("what") == what:
            return None
    rec = {"key": key, "what": what, "note": note,
           "at": datetime.datetime.now().isoformat(timespec="seconds")}
    s["sins"].append(rec)
    _save_json(SINS_FILE, s)
    log("  ⚠ 记下一笔罪：%s（%s）—— 等 warden 来收" % (key, what))
    notify("Focusmate session 被手动取消",
           "%s 在 API 里不见了（%s）。daemon 会重订，TUI 会锁死直到你敲完宣言。"
           % (key, what))
    return rec


def clear_sins(keys=None):
    """认罪：把罪从「待认罪」挪进 archive（.jsonl，只增不删 = 历史即罪证）。

    keys=None 表示清掉全部。返回清掉几条。
    """
    s = load_sins()
    keep, gone = [], []
    for rec in s["sins"]:
        (gone if keys is None or rec.get("key") in keys else keep).append(rec)
    if not gone:
        return 0
    s["sins"] = keep
    _save_json(SINS_FILE, s)
    os.makedirs(os.path.dirname(SINS_ARCHIVE), exist_ok=True)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(SINS_ARCHIVE, "a", encoding="utf-8") as f:
        for rec in gone:
            f.write(json.dumps(dict(rec, confessed=stamp), ensure_ascii=False) + "\n")
    return len(gone)


# ---------------------------------------------------------------- data.csv
# 解析/编辑全在 schedule.py —— daemon 和 TUI 共用**同一份实现**，这里只做转发。
# 谁也别在本文件里再写一套"哪一行算一个 slot"，那是双份真源的开始。

WEEKDAYS = sch.WEEKDAYS                      # 老名字，其它模块还在 import
WEEKDAY_ALIASES = sch.WEEKDAY_ALIASES
EVERY_DAY = sch.EVERY_DAY
normalize_weekday = sch.normalize_weekday
today_weekday = sch.today_weekday
load_slots = sch.load_slots                  # 兼容老调用（test_download_calendar.py 用）
load_entries = sch.load_entries
day_slots = sch.day_slots
slot_datetime = sch.slot_datetime
key_of = sch.key_of


# ---------------------------------------------------------------- 分心看门狗
#
# daemon 在「session 进行中」额外跑的一个检查：每整整一分钟没键盘/触控板输入就
# 记一次违规 + 通知（含当前次数和「到第 3 次会有不好的事情」）；到第 3 次就把
# 系统音量拉到 10% 并播放 data/audio/赵小六嘶吼.mp3。
#
# 规则细节：
# - 每一「整分钟」各记一次（连续空闲 3 分钟 = 3 次通知 → 触发）；同一分钟内不重复。
# - 一旦有人动键鼠/触控板，本段空闲的分钟数清零、从 0 重新数（但本场累计违规不归零）。
# - 暂停（专注视图的 `stop`）期间不打扰：把基线推到当前空闲，暂停结束后重新从 0 数。
# - 不在 session 里时看门狗不上班；读不到空闲时间就不判（不误伤）。
# - 每次 tick 都把「当前空闲秒数」落盘，供 warden 专注视图展示。
# 见 feature_intents/idle_watchdog.md。

_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def load_idle_state():
    """看门狗落盘状态：

    {session_key, mode, strikes, episode_marks, episode_warns,
     last_idle_seconds, last_tick_at, last_strike_at}

    - strikes            本场累计违规次数（通知次数），封顶当前模式的 max_strikes
    - episode_marks      当前这段连续空闲已经数过几个违规阈值（不追溯）
    - episode_warns      当前这段连续空闲已经发过几次预警（不追溯）
    - mode               这段状态是用哪个模式算的（切模式时本段重新开始）
    - last_idle_seconds / last_tick_at  最近一次读到的空闲秒数（warden 展示用）
    """
    st = _load_json(IDLE_STATE_FILE, {})
    if not isinstance(st.get("strikes"), int):
        st["strikes"] = 0
    if not isinstance(st.get("episode_marks"), int):
        st["episode_marks"] = 0
    if not isinstance(st.get("episode_warns"), int):
        st["episode_warns"] = 0
    if st.get("mode") not in IDLE_MODES:
        st["mode"] = DEFAULT_IDLE_MODE
    return st


def save_idle_state(st):
    _save_json(IDLE_STATE_FILE, st)


# ---------------------------------------------------------------- 专注模式

def load_mode_state():
    """当前专注模式落盘：{session_key, mode, set_at}。

    session_key 为空 / None 时表示这是「下一个 session 的默认模式」；绑定到具体
    session_key 后只对该场生效。TUI 的 Shift+Tab 写这里，daemon 每 tick 读这里。
    """
    st = _load_json(IDLE_MODE_FILE, {})
    mode = st.get("mode")
    if mode not in IDLE_MODES:
        mode = DEFAULT_IDLE_MODE
    key = st.get("session_key")
    if key is not None and not isinstance(key, str):
        key = None
    return {"session_key": key or None, "mode": mode, "set_at": st.get("set_at")}


def save_mode_state(st):
    _save_json(IDLE_MODE_FILE, st)


def idle_mode_config(mode=None):
    """取某个模式配置；不认识就落回普通模式。"""
    if isinstance(mode, dict):
        return mode
    return IDLE_MODES.get(mode) or IDLE_MODES[DEFAULT_IDLE_MODE]


def get_idle_mode(session_key=None):
    """当前生效模式 key。

    规则：
    - 模式绑定在正在进行的 session 上 → 用它；
    - 模式是「下一个 session 默认」（session_key 为空）→ 所有还没绑定的场都用它；
    - 绑定的是别的（已结束的）session → 回普通模式，下一场重新选。
    """
    st = load_mode_state()
    if not st["session_key"]:
        return st["mode"]
    if session_key and st["session_key"] == session_key:
        return st["mode"]
    return DEFAULT_IDLE_MODE


def set_idle_mode(mode, session_key=None, now=None):
    """设置专注模式；返回配置。mode 不认识时抛 ValueError。

    session_key 传当前 session 的 key = 只对这场改；传 None = 设为下一场默认。
    """
    if mode not in IDLE_MODES:
        raise ValueError("未知专注模式：%s（可选：%s）"
                         % (mode, " / ".join(IDLE_MODE_ORDER)))
    now = now or datetime.datetime.now()
    st = {"session_key": session_key or None, "mode": mode,
          "set_at": now.isoformat(timespec="seconds")}
    save_mode_state(st)
    return idle_mode_config(mode)


def fmt_idle_seconds(sec):
    """把秒数写成人话：30 秒 / 1 分钟 / 2 分 30 秒。"""
    try:
        sec = int(round(float(sec)))
    except (TypeError, ValueError):
        return "?"
    if sec < 60:
        return "%d 秒" % sec
    m, s = divmod(sec, 60)
    if s:
        return "%d 分 %d 秒" % (m, s)
    return "%d 分钟" % m


def idle_targets(cfg, idle_seconds):
    """当前连续空闲已跨过几个「违规阈值」和几个「预警阈值」。

    marks = floor(idle / T)；warn_before=T 之前 W 秒，所以第 n 个预警发生在
    n*T-W。warns 也取 floor((idle+W)/T)，并且每个违规周期最多一个预警（预警本身
    不增加违规次数，只是在下一次违规前刷新一次提醒）。
    """
    threshold = max(1, int(cfg.get("threshold_sec") or IDLE_THRESHOLD_SEC))
    marks = int(idle_seconds // threshold)
    warns = 0
    if cfg.get("warn"):
        lead = int(cfg.get("warn_before_sec") or 0)
        lead = max(0, min(lead, threshold - 1))
        warns = int((idle_seconds + lead) // threshold)
    return marks, warns


# ---------------------------------------------------------------- 暂停（专注视图）

def load_pause_state(day=None):
    """暂停状态：{date, used, pause_until}。跨天自动把 used 归零（每天 3 次）。"""
    day = day or datetime.date.today()
    st = _load_json(PAUSE_FILE, {})
    if st.get("date") != day.isoformat():
        return {"date": day.isoformat(), "used": 0, "pause_until": None}
    if not isinstance(st.get("used"), int):
        st["used"] = 0
    return st


def save_pause_state(st):
    _save_json(PAUSE_FILE, st)


def pause_status(now=None):
    """当前暂停状态：used/remaining（今天还剩几次）、active、left_sec（还剩多少秒）。

    warden 专注视图和 daemon 都读它。remaining 是「今天还能按几次 stop」。
    """
    now = now or datetime.datetime.now()
    st = load_pause_state(now.date())
    until = parse_iso(st.get("pause_until"))
    left = int((until - now).total_seconds()) if until else 0
    active = bool(until and left > 0)
    used = int(st.get("used", 0))
    return {"date": st["date"], "used": used,
            "remaining": max(0, PAUSE_MAX_PER_DAY - used),
            "pause_until": until, "active": active,
            "left_sec": left if active else 0}


def start_pause(now=None):
    """消耗一次暂停，暂停 PAUSE_MINUTES 分钟。

    返回 (ok, status, reason)。已经在暂停里 / 今天次数用完 → ok=False。
    """
    now = now or datetime.datetime.now()
    st = load_pause_state(now.date())
    st_now = pause_status(now)
    if st_now["active"]:
        return False, st_now, "已经在暂停中（还剩 %ds），等它走完" % st_now["left_sec"]
    if st_now["remaining"] <= 0:
        return False, st_now, "今天的 %d 次暂停已经用完了" % PAUSE_MAX_PER_DAY
    st["used"] = int(st.get("used", 0)) + 1
    st["pause_until"] = (now + datetime.timedelta(minutes=PAUSE_MINUTES)
                         ).isoformat(timespec="seconds")
    save_pause_state(st)
    return True, pause_status(now), ""


def hid_idle_seconds():
    """最近一次键盘/鼠标/触控板输入到现在的秒数（ioreg HIDIdleTime，纳秒 / 1e9）。

    非 macOS、ioreg 出错、输出里没有该字段 → None。调用方据此跳过本轮，
    **绝不把「读不到」误判成「没输入」**。
    """
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run([IOREG_BIN, "-c", "IOHIDSystem"],
                             check=False, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    m = _HID_IDLE_RE.search(out or "")
    if not m:
        return None
    try:
        return int(m.group(1)) / 1e9
    except ValueError:
        return None


def current_session(entries, now):
    """正在进行的契约 session → (day, slot, start, end)；没有就 None。

    也看昨天，覆盖 23:xx 起、跨过午夜的那点边角（duration 最长 75min）。
    """
    for day in (now.date(), now.date() - datetime.timedelta(days=1)):
        for slot in day_slots(entries, day):
            start = slot_datetime(slot, day)
            end = start + datetime.timedelta(minutes=int(slot["duration"]))
            if start <= now < end:
                return (day, slot, start, end)
    return None


def play_alert(path=None, volume=IDLE_ALERT_VOLUME):
    """「不好的事情」：把系统音量拉到 volume% 并播放告警音频（默认赵小六嘶吼）。

    单独成函数是为了测试能把它换掉，不真的出声。返回是否成功。
    """
    path = path or IDLE_ALERT_AUDIO
    if not os.path.exists(path):
        log("  [idle] 告警音频不存在：%s" % path)
        return False
    log("  [idle] 把音量拉到 %d%% 并播放 %s" % (volume, os.path.basename(path)))
    try:
        import alert  # 同目录，纯 stdlib（osascript 设音量 + afplay 播放）
    except Exception as e:
        log("  [idle] 载入 alert.py 失败：%s" % e)
        return False
    return alert.play(path, volume=100, system_volume=volume, wait=False) == 0


def idle_tick(dry_run=False, now=None, idle_seconds=None, mode=None):
    """session 进行中的一次分心检查（daemon 每 IDLE_POLL_SEC 调一次）。

    返回本场累计违规次数；看门狗没上班（不在 session / 读不到空闲时间）返回 None，
    暂停中返回当前累计值。idle_seconds / mode 可注入 —— 测试不用真去问 ioreg 或
    改文件。
    """
    if not IDLE_CHECK:
        return None
    now = now or datetime.datetime.now()
    if idle_seconds is None:
        idle_seconds = hid_idle_seconds()
    if idle_seconds is None:
        return None                      # 读不到空闲时间：不误判

    st = load_idle_state()
    prev_idle = st.get("last_idle_seconds")
    st["last_idle_seconds"] = idle_seconds
    st["last_tick_at"] = now.isoformat(timespec="seconds")

    entries, _warnings = load_entries()
    sess = current_session(entries, now)
    if sess is None:
        save_idle_state(st)              # 顺手更新「最近空闲」，看门狗不上班
        return None
    day, slot, _start, _end = sess
    key = key_of(day, slot)
    if st.get("session_key") != key:     # 换了一场：违规次数归零
        prev_idle = None
        st["session_key"] = key
        st["strikes"] = 0
        st["episode_marks"] = 0
        st["episode_warns"] = 0

    # 模式每次 tick 都从文件/参数重新解析：TUI 中途改了立刻生效，不用重启 daemon。
    # 同一场 session 里切模式时，旧阈值下攒的整分钟数作废，从当前空闲重新对齐。
    chosen = mode if mode in IDLE_MODES else get_idle_mode(key)
    if chosen not in IDLE_MODES:
        chosen = DEFAULT_IDLE_MODE
    # TUI 在没有 session 时设的模式是「下一场默认」；新 session 一开始就让 daemon
    # 把它绑到本场，这样这场结束后的下一场会回到普通模式，不会一直粘着。
    if mode is None:
        mst = load_mode_state()
        if not mst["session_key"] and mst["mode"] != DEFAULT_IDLE_MODE:
            set_idle_mode(chosen, key, now)
    if st.get("mode") != chosen:
        prev_idle = None
        st["episode_marks"] = 0
        st["episode_warns"] = 0
    st["mode"] = chosen
    cfg = idle_mode_config(chosen)
    threshold = max(1, int(cfg["threshold_sec"]))
    max_strikes = max(1, int(cfg.get("max_strikes") or IDLE_MAX_STRIKES))
    marks, warns = idle_targets(cfg, idle_seconds)
    strikes = int(st.get("strikes", 0))

    # 暂停中（专注视图 `stop` 换来的）：不打扰；把「已数过的阈值」推到当前，
    # 暂停结束后从下一个阈值接着数 —— 免得一解除就按攒下的空闲连爆三次。
    if pause_status(now)["active"]:
        st["episode_marks"] = marks
        st["episode_warns"] = warns
        save_idle_state(st)
        return strikes

    if idle_seconds < threshold:
        st["episode_marks"] = 0          # 有人在动 / 还没攒够阈值
        st["episode_warns"] = 0
    elif prev_idle is None or idle_seconds < float(prev_idle):
        # 本场第一次观察，或中间有人动过（空闲时间比上次还小）：
        # 不追溯已经空闲掉的那几个阈值，从当前阈值边界往后数。
        st["episode_marks"] = marks
        st["episode_warns"] = warns

    counted = int(st.get("episode_marks", 0))
    warned = int(st.get("episode_warns", 0))

    # 预警：每个违规周期都可以刷新一次，但不算违规次数。只有在对应违规还没
    # 记上时才补发；已经数过的旧预警不追溯。
    if warned < counted:
        warned = counted
        st["episode_warns"] = warned
    while cfg.get("warn") and warned < warns and strikes < max_strikes:
        nxt = warned + 1
        if nxt > counted + 1:
            break                         # 一次 tick 最多补一个预警，避免追债式轰炸
        if nxt > max_strikes:
            break
        warned = nxt
        st["episode_warns"] = warned
        st["last_warn_at"] = now.isoformat(timespec="seconds")
        save_idle_state(st)
        lead = fmt_idle_seconds(cfg.get("warn_before_sec", 0))
        next_no = min(strikes + 1, max_strikes)
        log("  [idle] %s（%s模式）连续 %s 无输入 → 预警：再 %s 就记第 %d/%d 次违规"
            % (key, cfg["name"], fmt_idle_seconds(idle_seconds), lead,
               next_no, max_strikes))
        if dry_run:
            log("  [idle][dry-run] 会发预警，但不记违规")
        else:
            notify("Focusmate 分心预警",
                   "（%s模式）你已经连续 %s 没碰键盘/触控板了；再 %s 就会记"
                   "一次违规。预警不算违规次数，到下一个违规周期前会再提醒。"
                   % (cfg["name"], fmt_idle_seconds(idle_seconds), lead))

    while counted < marks and strikes < max_strikes:
        counted += 1
        strikes += 1
        st["strikes"] = strikes
        st["episode_marks"] = counted
        st["last_strike_at"] = now.isoformat(timespec="seconds")
        save_idle_state(st)
        log("  [idle] %s（%s模式）连续 %s 无键盘/触控板输入 → 违规 %d/%d"
            % (key, cfg["name"], fmt_idle_seconds(idle_seconds), strikes,
               max_strikes))
        if dry_run:
            log("  [idle][dry-run] 会通知用户；到第 %d 次会拉低音量 + 播放 %s"
                % (max_strikes, os.path.basename(IDLE_ALERT_AUDIO)))
            continue
        if strikes < max_strikes:
            notify("Focusmate 分心警告",
                   "（%s模式）连续 %s 没碰键盘/触控板 = 违规。当前违规次数：%d/%d —— "
                   "到第 %d 次会有不好的事情发生。"
                   % (cfg["name"], fmt_idle_seconds(idle_seconds), strikes,
                      max_strikes, max_strikes))
        else:
            notify("Focusmate 分心警告",
                   "（%s模式）连续 %s 没碰键盘/触控板。违规次数：%d/%d —— "
                   "说好的不好的事情，现在就来。"
                   % (cfg["name"], fmt_idle_seconds(idle_seconds), strikes,
                      max_strikes))
            play_alert(IDLE_ALERT_AUDIO, IDLE_ALERT_VOLUME)
    save_idle_state(st)
    return strikes


# ---------------------------------------------------------------- 只读 API


def fetch_sessions(now, until):
    """一次 API 调用，返回 [now 当天 00:00, until] 之间所有 session（**本地 naive** 时间）。

    窗口跨天（今天+明天），所以不能再"只捞今天"——一次捞够，反正只读接口很便宜。
    出错就抛异常，由调用方决定这一轮怎么办。
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    url = "https://api.focusmate.com/v1/sessions?start=%s&end=%s" % (
        urllib.parse.quote(day_start.astimezone(datetime.timezone.utc).isoformat()),
        urllib.parse.quote(until.astimezone(datetime.timezone.utc).isoformat()),
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
        out.append(got.astimezone().replace(tzinfo=None, second=0, microsecond=0))
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


def verify_slot_booked(start, attempts=3, wait=4):
    """订完后**以 API 为准**复核这场 session 是否真的存在。

    为什么要复核：focusmate-mcp 的 book_session 抓不到真实 sessionId 时会
    照样返回 success:true（见其 booking.ts）。也就是说 payload.success 不可信
    —— API 才是 ground truth。

    返回 (ok, detail)。ok=True 表示 API 里确实有这个 slot。
    """
    last = ""
    for i in range(attempts):
        time.sleep(wait)  # booking 落库有延迟，给几秒
        try:
            now = datetime.datetime.now()
            sessions = fetch_sessions(now, now + datetime.timedelta(hours=WINDOW_HOURS))
        except Exception as e:
            last = "复核时 API 出错: %s" % e
            continue
        if has_session_at(sessions, start):
            return True, ""
        last = "API 复核 %d 次仍未出现该 session" % (i + 1)
        log("    [verify] 第 %d 次复核：API 里还没出现，%ds 后再试" % (i + 1, wait))
    return False, last


# ---------------------------------------------------------------- 核心一轮


def run_once(dry_run=False, force=False):
    """跑一轮完整检查+补订（带文件锁）。daemon 与手动 check 互斥。

    force=True 时忽略重试上限/退避：对窗口内所有还没订上的 session 都试一次
    （`keep_session.py check --force`，TUI 里就是 `force-check`）。
    """
    with round_lock() as got:
        if not got:
            log("  [lock] 另一轮正在跑（daemon 或手动 check），本轮跳过")
            return 0
        return _run_once_locked(dry_run=dry_run, force=force)


def _run_once_locked(dry_run=False, force=False):
    """真正干活的一轮。必须在持有 round_lock 时调用。

    只管 (now, now+24h] 的执行区：订满它、对账已经过去的、记罪、认判决。
    force=True 会无视 attempts 上限和退避时间，对每场没订上的都发起一次预定。
    """
    now = datetime.datetime.now()        # 本地 naive —— schedule.py 的窗口语义就用这个
    log("=== round start (dry_run=%s, force=%s) %s ==="
        % (dry_run, force, now.strftime("%Y-%m-%d %H:%M:%S")))

    entries, warnings = load_entries()
    for wmsg in warnings:
        log("  [warn] %s" % wmsg)

    win = sch.window(entries, now, hours=WINDOW_HOURS)
    horizon = win["horizon"]
    due, past = win["due"], win["past"]
    log("  窗口 %s → %s：执行区 %d 场、已开始 %d 场"
        % (now.strftime("%m-%d %H:%M"), horizon.strftime("%m-%d %H:%M"), len(due), len(past)))
    if not due and not past:
        log("  [warn] 未来 %d 小时里**一条契约都没有** —— 不是放假就是 data.csv 断了档，"
            "去 warden 敲 `day 0` / `day 1` 看一眼" % WINDOW_HOURS)

    state = load_state()
    verdicts = load_verdicts()
    changed = False

    # 认过罪的：永不重订（这是宣言真正的牙齿）。daemon 自己绝不判，只读判决。
    abandoned = [(key_of(day, slot), (day, slot, start)) for day, slot, start in due
                 if is_abandoned(key_of(day, slot), verdicts)]
    if abandoned:
        drop = set(k for k, _ in abandoned)
        due = [(day, slot, start) for day, slot, start in due
               if key_of(day, slot) not in drop]
        for key, (_day, slot, _start) in abandoned:
            rec = state["slots"].get(key) or {}
            if rec.get("status") != "abandoned":
                log("  %s 被 warden 判为「已放弃」（你认过罪），不再重订" % key)
                state["slots"][key] = {"status": "abandoned", "duration": slot["duration"],
                                       "attempts": rec.get("attempts", 0)}
                changed = True

    # 一次 API 拿到窗口内全部 session（即使 due 为空也要拿，供 status/TUI 展示）
    sessions = None
    if not dry_run:
        try:
            sessions = fetch_sessions(now, horizon)
            log("  窗口内 API 里已有 %d 场 session" % len(sessions))
        except Exception as e:
            log("  API 列 session 失败: %s（本轮跳过，下轮再试）" % e)
            return 1
        state["api_snapshot"] = {
            "at": now.isoformat(timespec="seconds"),
            "horizon": horizon.isoformat(timespec="seconds"),
            "sessions": [s.isoformat() for s in sessions],
        }
        changed = True

    # 已经开始的：不补订，只把"到底有没有"记进 state（让 status / TUI 能看出来）
    for day, slot, start in past:
        key = key_of(day, slot)
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
                log("  [已过] %s (%smin) 没有这场 session，已经来不及补订了" % (key, slot["duration"]))
                if rec.get("status") == "booked":
                    # 订过、然后消失了、而且已经开过了：这是硬罪（你没去 / 你事后取消）
                    record_sin(key, "booked-but-missing-past",
                               "已过时间才发现 API 里没有（原记录：booked）")
                state["slots"][key] = {"status": "missed", "duration": slot["duration"],
                                       "attempts": rec.get("attempts", 0), "past": True}
                changed = True

    for day, slot, start in due:
        key = key_of(day, slot)
        rec = state["slots"].get(key) or {}
        status = rec.get("status")

        if dry_run:
            present = "?" if sessions is None else ("有" if has_session_at(sessions or [], start) else "无")
            log("  [dry-run] %s (%smin) 在 API 中: %s；state=%s"
                % (key, slot["duration"], present, status or "无记录"))
            continue

        # 计划时长变了（sync 重生成 / 手改 data.csv）＝ 这是**另一个目标**，老记录作废重来。
        # 否则改了计划 daemon 也不动：25min 试失败后改成 50min，会永远卡在 failed 上。
        # 只作废还没订上的（failed / 退避中 / 待订）；booked 不动 —— 已订那场改不了时长。
        if (rec.get("duration") not in (None, str(slot["duration"]))
                and status in (None, "failed")):
            log("  %s 计划时长 %s → %s，老状态作废，重新评估"
                % (key, rec.get("duration"), slot["duration"]))
            state["slots"].pop(key, None)
            rec, status = {}, None
            changed = True

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
            # 我们以为是 booked，但 API 说没有 → 你手动取消了。
            # 守擂：照旧重订（机器读不了你的心）；但记一笔罪，TUI 会锁死你来收。
            log("  %s 原以为已订，但 API 查不到（疑似你手动取消）→ 记罪 + 重新预定" % key)
            record_sin(key, "booked-but-missing",
                       "daemon 在 %s 的这轮检查里发现它消失了" % now.strftime("%m-%d %H:%M"))
            rec = {"duration": slot["duration"], "attempts": 0, "status": None}
            state["slots"][key] = rec
            changed = True
        elif status == "failed":
            if not force:
                log("  %s 已达重试上限，不再尝试" % key)
                continue
            log("  %s 已达重试上限，force-check 忽略上限，继续尝试" % key)

        # 该订了吗？（退避等待）
        nra = parse_iso(rec.get("next_retry_at"))
        if nra and now < nra:
            if not force:
                log("  %s 在退避中，等到 %s 再试" % (key, nra.strftime("%H:%M:%S")))
                continue
            log("  %s 在退避中（到 %s），force-check 忽略退避" % (key, nra.strftime("%H:%M:%S")))

        attempts = int(rec.get("attempts", 0))
        if attempts >= MAX_ATTEMPTS:
            if not force:
                log("  %s 已尝试 %d 次仍失败，转 failed + 通知" % (key, attempts))
                state["slots"][key] = {"status": "failed", "duration": slot["duration"],
                                       "attempts": attempts, "last_error": rec.get("last_error")}
                notify("Focusmate 预定失败",
                       "%s (%smin) 尝试 %d 次仍失败，不再重试。原因：%s"
                       % (key, slot["duration"], attempts, rec.get("last_error") or "未知"))
                changed = True
                continue
            log("  %s 已尝试 %d 次；force-check 忽略上限，继续尝试" % (key, attempts))

        # 真的去订
        attempts += 1
        if force:
            log("  %s 缺失，force-check 第 %d 次预定...（目标时间 %s，%smin）"
                % (key, attempts, fmt_local_utc(start), slot["duration"]))
        else:
            log("  %s 缺失，开始第 %d/%d 次预定...（目标时间 %s，%smin）"
                % (key, attempts, MAX_ATTEMPTS, fmt_local_utc(start), slot["duration"]))
        debounce()
        # 直接用算好的 start（带日期），不走 fm.resolve_target 的"过了就顺延到明天"猜谜：
        # 窗口里同时有今天和明天的场次时，那种猜法会挑错日子。
        start_iso = start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            payload = fm.book_once(start_iso, slot["duration"])
        except Exception as e:
            payload = {"success": False, "error": str(e), "errorCode": "EXCEPTION"}

        if payload.get("success"):
            sess = payload.get("session") or {}
            log("  %s book_session 报成功 (id=%s)，开始 API 复核..." % (key, sess.get("id")))
            ok, detail = verify_slot_booked(start)
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
        elif force:
            log("  %s force-check 第 %d 次失败: %s（已达上限，但不发通知；下次 force-check 仍可再试）"
                % (key, attempts, reason))
        else:
            log("  %s 第 %d 次失败: %s（已达上限，不再试）" % (key, attempts, reason))
            notify("Focusmate 预定失败",
                   "%s (%smin) 尝试 %d 次仍失败：%s。不再重试。"
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
    if IDLE_CHECK:
        mode_txt = " / ".join(
            "%s %s·第%d次罚" % (IDLE_MODES[m]["name"],
                                fmt_idle_seconds(IDLE_MODES[m]["threshold_sec"]),
                                IDLE_MODES[m]["max_strikes"])
            for m in IDLE_MODE_ORDER)
        log("  分心看门狗已武装：模式 %s；惩罚时把音量拉到 %d%% 并播放 %s"
            % (mode_txt, IDLE_ALERT_VOLUME,
               os.path.basename(IDLE_ALERT_AUDIO)))
    while not _STOP["flag"]:
        run_once(dry_run=dry_run)
        # 可被 STOP_FILE 或信号打断的分段睡眠；顺便每 IDLE_POLL_SEC 看一眼分心情况
        slept = 0
        while slept < LOOP_INTERVAL and not _STOP["flag"]:
            if os.path.exists(STOP_FILE):
                log("发现 STOP_FILE，退出")
                _STOP["flag"] = True
                break
            if IDLE_CHECK and slept % IDLE_POLL_SEC == 0:
                try:
                    idle_tick(dry_run=dry_run)
                except Exception as e:
                    log("  [idle] 检查出错（忽略，下轮再试）：%s" % e)
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
            # 只删「自己那条」记录。孤儿 daemon（比如 pid 文件被删过、restart 杀不到的那个）
            # 退出时如果无脑 os.remove，会把**别人**（当前正在跑的 daemon）的 pid 记录抹掉，
            # 于是 status 说没在跑、daemon start 又启一个。实测踩过。
            if read_pid() == os.getpid():
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
    now = datetime.datetime.now()
    print("== data.csv（契约表）==")
    entries, warnings = load_entries()
    print("  %s：%d 条规则" % (sch.data_csv_path(), len(entries)))
    for wmsg in warnings:
        print("  [warn] %s" % wmsg)
    win = sch.window(entries, now, hours=WINDOW_HOURS)
    print("  执行区 (now, now+%dh]：%s → %s"
          % (WINDOW_HOURS, now.strftime("%m-%d %H:%M"), win["horizon"].strftime("%m-%d %H:%M")))
    if not win["due"] and not win["past"]:
        print("    ⚠ 窗口里**一条契约都没有** —— 不是放假就是断了档")
    for day, slot, start in win["due"]:
        print("    [待订/已订] %s %smin" % (key_of(day, slot), slot["duration"]))
    if win["past"]:
        print("    [已开始] %d 场（只对账，不补订）" % len(win["past"]))
    draft = 0
    for i in range(1, 8):
        day = now.date() + datetime.timedelta(days=i)
        for slot in day_slots(entries, day):
            if sch.slot_datetime(slot, day) > win["horizon"]:
                draft += 1
    print("  草稿区（>%dh）：未来 7 天还有 %d 场没进执行区" % (WINDOW_HOURS, draft))

    print("== 罪 / 判决 ==")
    sins = load_sins()["sins"]
    if sins:
        print("  ⚠ 待认罪 %d 笔（warden 会锁死）：" % len(sins))
        for rec in sins:
            print("    %s  %s  %s" % (rec.get("at", "")[11:19], rec.get("key"), rec.get("what")))
    else:
        print("  待认罪：无")
    ab = load_verdicts()["abandoned"]
    if ab:
        print("  已放弃 %d 场（daemon 永不重订）：" % len(ab))
        for k in sorted(ab):
            print("    %s  %s" % (k, ab[k].get("at", "")[:16]))
    else:
        print("  已放弃：无")

    print("== 分心看门狗 ==")
    if not IDLE_CHECK:
        print("  已关闭（KEEP_SESSION_IDLE_CHECK=0）")
    else:
        sess = current_session(entries, now)
        cur = key_of(sess[0], sess[1]) if sess else None
        cur_mode = get_idle_mode(cur)
        cur_cfg = idle_mode_config(cur_mode)
        print("  当前 session：%s" % (cur + "（进行中）" if cur else "无（只在 session 进行时上班）"))
        print("  当前模式：%s（%s）" % (cur_cfg["name"], cur_cfg["desc"]))
        idle_st = load_idle_state()
        if idle_st.get("session_key"):
            mark = "  ← 当前这场" if idle_st.get("session_key") == cur else ""
            st_cfg = idle_mode_config(idle_st.get("mode"))
            st_max = int(st_cfg.get("max_strikes") or IDLE_MAX_STRIKES)
            print("  最近计数：%s 模式=%s 违规 %d/%d 预警 %d%s"
                  % (idle_st.get("session_key"), idle_st.get("mode", DEFAULT_IDLE_MODE),
                     idle_st.get("strikes", 0), st_max,
                     idle_st.get("episode_warns", 0), mark))
        else:
            print("  最近计数：无")
        if idle_st.get("last_idle_seconds") is not None:
            print("  最近空闲：%ds（%s）"
                  % (int(idle_st["last_idle_seconds"]), idle_st.get("last_tick_at", "?")[11:19]))
        ps = pause_status(now)
        tail = ""
        if ps["active"]:
            tail = "，暂停中 还剩 %ds" % ps["left_sec"]
        print("  今日暂停：已用 %d/%d（剩 %d 次，每次 %d 分钟）%s"
              % (ps["used"], PAUSE_MAX_PER_DAY, ps["remaining"], PAUSE_MINUTES, tail))

    print("== 窗口内 state ==")
    st = load_state()
    snap = st.get("api_snapshot") or {}
    snap_at = snap.get("at")
    if snap_at:
        print("  (API 快照 @%s，窗口内 %d 场)"
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
    p_check.add_argument("--force", action="store_true",
                         help="忽略重试上限/退避：对所有还没订上的 session 都试一次")

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
        return run_once(dry_run=args.dry_run, force=args.force)
    if args.cmd == "daemon":
        return cmd_daemon(args.action, dry_run=getattr(args, "dry_run", False))
    if args.cmd == "status":
        return cmd_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
