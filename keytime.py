#!/usr/bin/env python3
"""keytime.py —— Key Time（日历里的「通勤 / 休息」硬节点）：模型 / 挤压预测 / 播报 / 推送。

Key Time 是什么
---------------
日历里某些**不是 session** 的时间块（默认「通勤」「休息」两个日历里的所有事件）
是硬节点：到点必须做那件事，晚一分钟都是从后面的时间盒里抢。本模块只负责
「怎么算、怎么喊、怎么推」，不碰 warden 的表格，也不写 data.csv。

    日历（CalDAV）──download_calendar.py──▶ temp/calendar-download.json["keytimes"]
                                                        │
                                              keytime.load_keytimes()
                                                        │
                       ┌────────────────────────────────┼─────────────────────────┐
                       ▼                                ▼                         ▼
               warden 看板（淡红一行 / 全屏模板）   alert.py（音量 + say）    Bark（手机推送）

三个纯函数（测试直接断言，不碰系统）
------------------------------------
- :func:`alarm_target`  现在该为哪一个 key time 报警（已确认的不算、太久远的不算）
- :func:`squeeze`       这次拖延会挤掉今天后面哪几场、几场被挤出排期、要多加班几场
- :func:`render_lines` / :func:`speech_text`  同一个事实的两种句式（屏幕 / 语音）

两条铁律
--------
1. **只读日历、只写自己的状态文件。** keytime 不改变契约、不订场、不动 data.csv；
   唯一会写的外部状态是：系统音量（播报前后）和手机推送。
2. **报警强度从状态来，不从时间硬编码。** 到点 60 秒内一次，之后每分钟一次；
   确认（ack）之后停止播报，但「每分钟把 warden 拉到最前」还会继续 —— 这是用户
   明确要的行为（见 feature_intents/keytime.md）。

文件
----
    temp/keytime.json           配置（日历名、节拍、音量、推送档位）
    temp/keytime-state.json     确认记录（按 keytime 的 key = 日期+开始时间）
    data/keytime-devices.json    Bark 设备（含 key，**不进 git**）
    temp/calendar-download.json 日历中间层（keytimes 由 download_calendar.py 写入）
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DATA = os.path.join(HERE, "data")

DEFAULT_CONFIG_FILE = os.path.join(TEMP, "keytime.json")
DEFAULT_STATE_FILE = os.path.join(TEMP, "keytime-state.json")
DEFAULT_DEVICES_FILE = os.path.join(DATA, "keytime-devices.json")
DEFAULT_DOWNLOAD_JSON = os.path.join(TEMP, "calendar-download.json")

#: 默认把哪些日历里的**全部事件**当作 Key Time。用户日历里现在就有一个「休息」。
DEFAULT_CALENDARS = ["通勤", "休息"]

#: key time 的类型 → 模板。认不出来当 "generic"。
TEMPLATES = ("commute", "rest", "generic")

DEFAULT_CONFIG = {
    "calendars": list(DEFAULT_CALENDARS),
    # 一天的时间盒到哪儿结束：被推迟到 day_end 之后的 session 算「挤出今日排期」。
    "day_end": "23:30",
    # 播报节拍（秒）。用户要求每分钟一次。
    "announce_sec": 60,
    # 播报前把系统输出音量拧到多少（0-100）；确认后还原。
    "volume": 10,
    # 语音："say" 的音色名；空 = 系统默认。中文本机有 Tingting（zh_CN）。
    "voice": "",
    # 确认后是否把系统音量还原成播报前那个值。
    "restore_volume": True,
    # 每分钟把 warden 拉到最前（activate iTerm + select 到那个 tab）。
    "summon": True,
    # 系统空闲超过这个秒数 = 人不在 Mac 前（这时才往手机推）。
    "leave_idle_sec": 120,
    # 超过这么久还没确认就不再报警（防止半夜补账式轰炸）。
    "lookback_min": 120,
    # Bark 推送。stack_notifications=True 时每分钟发一条新通知（用户选的）；
    # False 时用同一个 id 原地替换（手机上只留一条「已超时 N 分钟」）。
    "push": {
        "enabled": True,
        "level": "timeSensitive",
        "group": "keytime",
        "sound": "alarm",
        "stack_notifications": True,
        "ttl": 3600,
        "timeout": 8.0,
    },
}

#: 一分钟 60 秒；播报节拍按这个粒度对齐（不跟秒表抢）。
MINUTE = 60


# ================================================================ 小工具


def _now(now=None):
    return now or datetime.datetime.now()


def env_path(name, default):
    """测试/多环境用：``TI_*`` 指到临时目录，绝不碰用户真实状态。"""
    return os.environ.get(name) or default


def config_path(path=None):
    return path or env_path("TI_KEYTIME_JSON", DEFAULT_CONFIG_FILE)


def state_path(path=None):
    return path or env_path("TI_KEYTIME_STATE", DEFAULT_STATE_FILE)


def devices_path(path=None):
    return path or env_path("TI_KEYTIME_DEVICES", DEFAULT_DEVICES_FILE)


def download_json_path(path=None):
    """日历中间层路径。优先 ``TI_KEYTIME_DOWNLOAD``，然后跟 session_plan 共用
    ``TI_CALENDAR_DOWNLOAD``（测试把整个 temp 指到临时目录时不用再配一遍）。"""
    return (path or os.environ.get("TI_KEYTIME_DOWNLOAD")
            or os.environ.get("TI_CALENDAR_DOWNLOAD")
            or env_path("TI_CALENDAR_DOWNLOAD_JSON", DEFAULT_DOWNLOAD_JSON))


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


# ================================================================ 配置


def load_config(path=None):
    """读配置；缺的键用默认值补齐（不会把默认值写回去）。"""
    raw = _read_json(config_path(path), {}) or {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))          # deep copy
    for key, value in raw.items():
        if key == "push" and isinstance(value, dict):
            cfg["push"].update(value)
        else:
            cfg[key] = value
    cfg["calendars"] = [str(c).strip() for c in (cfg.get("calendars") or [])
                        if str(c).strip()] or list(DEFAULT_CALENDARS)
    return cfg


def save_config(cfg, path=None):
    _atomic_write(config_path(path), cfg)
    return config_path(path)


def load_devices(path=None):
    raw = _read_json(devices_path(path), {}) or {}
    items = raw.get("devices") if isinstance(raw, dict) else raw
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not str(item.get("key") or "").strip():
            continue
        out.append({
            "name": str(item.get("name") or "device").strip(),
            "key": str(item["key"]).strip(),
            "server": str(item.get("server") or "https://api.day.app").rstrip("/"),
            "level": str(item.get("level") or ""),
            "group": str(item.get("group") or ""),
            "sound": str(item.get("sound") or ""),
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def save_devices(devices, path=None):
    _atomic_write(devices_path(path), {"devices": list(devices)})
    return devices_path(path)


# ================================================================ 状态（确认记录）


def load_state(path=None):
    raw = _read_json(state_path(path), {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("acked", {})
    raw.setdefault("last_announce", {})
    return raw


def save_state(state, path=None):
    _atomic_write(state_path(path), state)
    return state_path(path)


# ================================================================ Key Time 模型


def _hm(text):
    """"HH:MM" → 从零点起的分钟数；坏格式（25:00 / 9点 / 空）返回 None。"""
    try:
        hh, mm = str(text).strip().split(":")[:2]
        hh, mm = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def _iso(dt):
    return dt.isoformat(timespec="minutes")


class KeyTime(object):
    """一个 key time 时间块（日历事件；date/start/end 都是本地时间）。"""

    __slots__ = ("date", "start", "end", "calendar", "title", "kind")

    def __init__(self, date, start, end, calendar, title="", kind=None):
        self.date = str(date)
        self.start = str(start)
        self.end = str(end)
        self.calendar = str(calendar or "")
        self.title = str(title or "")
        self.kind = kind or classify(self.calendar, self.title)

    # -- 时间 ---------------------------------------------------------

    def start_dt(self):
        return _combine(self.date, self.start)

    def end_dt(self):
        return _combine(self.date, self.end)

    def key(self):
        return "%sT%s" % (self.date, self.start)

    def label(self):
        return "%s-%s %s" % (self.start, self.end, self.calendar or self.title)

    # -- 判定 ---------------------------------------------------------

    def started(self, now):
        return self.start_dt() <= now

    def ended(self, now):
        return self.end_dt() <= now

    def overdue_minutes(self, now):
        return int((now - self.start_dt()).total_seconds() // 60)

    def to_dict(self):
        return {"date": self.date, "start": self.start, "end": self.end,
                "calendar": self.calendar, "title": self.title, "kind": self.kind}

    def __repr__(self):                                # pragma: no cover - 调试用
        return "KeyTime(%s %s-%s %s)" % (self.date, self.start, self.end, self.calendar)


def _combine(date, hm):
    """'2026-11-14' + '16:00' → naive datetime（日历/契约全用本地墙钟）。"""
    d = datetime.date.fromisoformat(str(date))
    minutes = _hm(hm)
    if minutes is None:
        raise ValueError("时间格式不对：%r（要 HH:MM）" % (hm,))
    return datetime.datetime(d.year, d.month, d.day, 0, 0) + datetime.timedelta(minutes=minutes)


def classify(calendar, title=""):
    """日历名/标题 → 模板类型。用户日历里现在叫「休息」，以后可能加「通勤」。"""
    text = "%s %s" % (calendar or "", title or "")
    if "通勤" in text or "路上" in text or " commute" in text.lower():
        return "commute"
    if "休息" in text or "睡觉" in text or "断电" in text or "rest" in text.lower():
        return "rest"
    return "generic"


def parse_keytimes(download, calendars=None):
    """从日历中间层里取 key time 事件。

    优先用 download_download.py 新写的 ``keytimes``；老文件没有这一节时，
    退回去 ``events`` 里按日历名筛（老文件只存了工作/空闲，通常筛不到）。
    """
    calendars = [str(c) for c in (calendars or DEFAULT_CALENDARS)]
    raw = (download or {}).get("keytimes")
    if raw is None:
        raw = [ev for ev in (download or {}).get("events") or []
               if str(ev.get("calendar") or "") in calendars]
    out = []
    for ev in raw or []:
        try:
            kt = KeyTime(ev.get("date") or str(ev.get("start_iso") or "")[:10],
                         ev.get("start"), ev.get("end"),
                         ev.get("calendar"), ev.get("title") or "")
        except (TypeError, ValueError):
            continue
        if not (kt.start_dt() < kt.end_dt()):
            continue
        out.append(kt)
    out.sort(key=lambda k: (k.date, k.start))
    return out


def load_keytimes(download_path=None, config=None):
    """读中间层 → (keytimes, error)。读不到不是异常：只是「今天没有 key time」。"""
    cfg = config or load_config()
    path = download_json_path(download_path)
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return [], "没有日历中间层（先跑 sync / download_calendar.py download）：%s" % path
    return parse_keytimes(data, cfg.get("calendars")), ""


def upcoming(now, keytimes, limit=3, include_started=False):
    """接下来还没结束的 key time（默认不含已经开始的）。"""
    now = _now(now)
    out = [kt for kt in keytimes
           if (kt.end_dt() > now) and (include_started or kt.start_dt() > now)]
    out.sort(key=lambda k: k.start_dt())
    return out[:limit] if limit else out


def next_keytime(now, keytimes):
    got = upcoming(now, keytimes, limit=1, include_started=True)
    return got[0] if got else None


def alarm_target(now, keytimes, state=None, config=None):
    """现在该为哪一个 key time 报警？没有 → None。

    规则（按优先级）：
    1. 已经开始的（start ≤ now）；2. 还没结束或超过 lookback 不算太久远；
    3. 没有被确认过（state.acked 里有它的 key）。
    同时命中多个（重叠的两个日历事件）时取**最早开始的**那个。
    """
    now = _now(now)
    cfg = config or load_config()
    lookback = datetime.timedelta(minutes=int(cfg.get("lookback_min") or 120))
    acked = ((state or {}).get("acked") or {})
    hits = [kt for kt in keytimes
            if kt.started(now) and kt.key() not in acked
            and (kt.end_dt() + lookback) > now]
    if not hits:
        return None
    hits.sort(key=lambda k: k.start_dt())
    return hits[0]


def acked(kt, state=None):
    return kt.key() in (((state or {}).get("acked") or {}))


# ================================================================ 挤压预测


def squeeze(now, keytime, sessions, day_end=None):
    """这次拖延会挤掉什么。**纯函数**，不碰任何外部状态。

    ``sessions``：今天剩下还没开始的场次（按开始时间升序），每项::

        {"start": datetime, "duration": 分钟, "tasks": ["任务标题", ...]}

    ``day_end``："HH:MM"；被推迟到它之后的场次算「挤出今日排期」。

    模型（故意简单，和用户的心智模型一致）：**拖延 1 分钟 = 后面每一场整体后移
    1 分钟**。所以 delay = keytime 已经超时的分钟数；逐场加 delay，超过 day_end
    的就被挤出去了。这个模型不试图重排（重排会自欺欺人：真到那一步还得再重排）。
    """
    now = _now(now)
    delay = max(0, keytime.overdue_minutes(now))
    minutes = _hm(day_end or "23:30")
    end_of_day = datetime.datetime(now.year, now.month, now.day, 0, 0) + \
        datetime.timedelta(minutes=minutes if minutes is not None else 23 * 60 + 30)
    pushed, dropped = [], []
    for row in sessions or []:
        start = row.get("start")
        if isinstance(start, str):
            start = _combine(now.date().isoformat(), start)
        if not isinstance(start, datetime.datetime):
            continue
        dur = int(row.get("duration") or 0)
        new_start = start + datetime.timedelta(minutes=delay)
        new_end = new_start + datetime.timedelta(minutes=dur)
        item = {"start": start, "duration": dur, "new_start": new_start,
                "new_end": new_end, "tasks": list(row.get("tasks") or []),
                "label": row.get("label") or start.strftime("%H:%M")}
        if new_end > end_of_day:
            item["new_start"] = new_start
            dropped.append(item)
        elif delay:
            pushed.append(item)
    dropped_minutes = sum(int(item["duration"]) for item in dropped)
    return {
        "delay": delay,
        # 整场被挤出今日排期所需的「补课场次」（25 分钟一场，向上取整）
        "extra_sessions": (dropped_minutes + 24) // 25 if dropped_minutes else 0,
        "extra_minutes": dropped_minutes,
        "pushed": pushed,
        "dropped": dropped,
        "day_end": end_of_day,
    }


# ================================================================ 文案（屏幕 / 语音）


ACTION_BY_KIND = {
    "commute": "立刻动身（通勤）",
    "rest": "强制断电休息（离开屏幕）",
    "generic": "立刻处理这个 key time",
}


def headline(keytime, now):
    over = keytime.overdue_minutes(_now(now))
    return "🚨 [KEY TIME 违约警报] %s" % keytime.calendar


def _clock(dt):
    return dt.strftime("%H:%M") if isinstance(dt, datetime.datetime) else str(dt or "--:--")


def _task_names(items, limit=2):
    names = []
    for item in items:
        for title in item.get("tasks") or []:
            title = str(title or "").strip()
            if title and title not in names:
                names.append(title)
    return names[:limit]


def render_lines(keytime, now, ctx=None, acked=False, config=None):
    """屏幕上的模板：``[(rich style, text), ...]``，一行一段，别再自己拼。"""
    now = _now(now)
    ctx = ctx or {}
    lines = []
    over = keytime.overdue_minutes(now)
    lines.append(("bold white on red", headline(keytime, now)))
    lines.append(("bold", "动作：%s" % ACTION_BY_KIND.get(keytime.kind, ACTION_BY_KIND["generic"])))
    lines.append(("yellow", "⏱️ 已超时：%d 分钟（应于 %s 启动，现已 %s）"
                  % (over, keytime.start, _clock(now))))
    if acked:
        lines.append(("bold green", "✓ 已确认（%s）—— 不再播报；计时器仍在走"
                      % (_clock(ctx.get("acked_at")) if ctx.get("acked_at") else "刚刚")))
        return lines
    sq = ctx.get("squeeze") or {}
    if keytime.kind == "rest":
        lines.append(("bold magenta", "🧠 生理算力坍塌后果："))
        lines.append(("", "你正在拒绝清理大脑 L1 缓存！"))
        goal = ctx.get("goal") or ""
        if goal:
            lines.append(("", "你的 Project 总目标：%s 将因此失败！" % goal))
        else:
            lines.append(("dim", "（这个 session 的任务没有 Project 总目标；"
                                 "用 keytime goal <文本> 设置后这里会显示）"))
        lines.append(("", "原本 1 遍能读懂的东西，你会被迫反复空转 4 遍；"
                          "冲动控制力归零，下个 session 极高概率半途破防。"))
        lines.append(("bold yellow", "🛑 别拿垃圾时间骗自己：现在立刻离开屏幕 %d 分钟。"
                      % (int((keytime.end_dt() - keytime.start_dt()).total_seconds() // 60)
                         or 10)))
        return lines
    if keytime.kind == "commute":
        lines.append(("bold magenta", "💥 物理排期坍塌后果："))
        lines.append(("", "你的拖延正在以 1:1 的速度直接吞噬后续时间盒！"))
        dropped = sq.get("dropped") or []
        pushed = sq.get("pushed") or []
        names = _task_names(dropped) or _task_names(pushed)
        if dropped:
            if names:
                lines.append(("", "再不行动，今日队列中的%s将被直接挤出今日排期。"
                              % " 与 ".join("【%s】" % n for n in names)))
            else:
                lines.append(("", "再不行动，今天后面 %d 场会被挤出排期。"
                              % len(dropped)))
        elif pushed:
            tail = ("（%s）" % "、".join("【%s】" % n for n in names)) if names else ""
            lines.append(("", "再不行动，今天后面 %d 场%s将整体后移 %d 分钟，今晚就晚 "
                              "%d 分钟收工。" % (len(pushed), tail, sq.get("delay") or 0,
                                               sq.get("delay") or 0)))
        else:
            lines.append(("", "今天后面已经没有排期了 —— 这一小时只能从睡眠和明天里扣。"))
        if sq.get("extra_sessions"):
            lines.append(("bold red", "你今晚将被迫额外加班 %d 个 Session (25m) 擦屁股！"
                          % sq["extra_sessions"]))
        lines.append(("bold red", "🔴 计时器正在跳动：每多看屏幕 1 秒，今晚就晚睡 1 秒！"))
        return lines
    lines.append(("bold magenta", "⏳ 这个 key time 正在被拖延；后面的时间盒按 1:1 顺延。"))
    return lines


def speech_text(keytime, now, ctx=None, config=None):
    """语音播报稿：短、只讲「超时几分钟 + 结果」。太长时 ``say`` 自己会排队。"""
    now = _now(now)
    ctx = ctx or {}
    sq = ctx.get("squeeze") or {}
    over = keytime.overdue_minutes(now)
    if keytime.kind == "rest":
        head = "休息超时 %d 分钟。立刻离开屏幕。" % over
        goal = (ctx.get("goal") or "").strip()
        tail = ("总目标：%s。" % goal) if goal else ""
        return head + tail + "再不休息，下个 session 会脑雾死锁。"
    if keytime.kind == "commute":
        head = "通勤超时 %d 分钟，应于 %s 出发。" % (over, keytime.start)
        dropped = sq.get("dropped") or []
        pushed = sq.get("pushed") or []
        if dropped:
            names = _task_names(dropped)
            if names:
                head += "已经被挤出今日排期的是：%s。" % "、".join(names)
            if sq.get("extra_sessions"):
                head += "今晚要额外加班 %d 个 session。" % sq["extra_sessions"]
        elif pushed:
            head += "后面 %d 场整体后移 %d 分钟。" % (len(pushed), sq.get("delay") or 0)
        return head
    return "key time 超时 %d 分钟：%s。" % (over, keytime.calendar or keytime.title)


# ================================================================ 播报（音量 + say）


class Announcer(object):
    """一个后台线程：设音量 → say（阻塞）→ 队列里没播的就作废掉旧的。

    为什么不是每句话起一个 Popen：用户明确要求「如果过长那么等之前的放完再继续」。
    所以同一时刻只有一条在播，后面来的替换掉**还没开始**的那条（避免积压十几条
    旧消息），正在播的那条一定让它播完。
    """

    def __init__(self, config=None, speaker=None, volume_setter=None,
                 volume_getter=None, log=None):
        self.config = config or load_config()
        self._speaker = speaker or self._say
        self._set_volume = volume_setter or self._osascript_volume
        self._get_volume = volume_getter
        self._log = log or (lambda msg: None)
        self._queue = queue.Queue(maxsize=1)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.saved_volume = None
        self.spoken = 0

    # -- 系统调用（测试注入） -----------------------------------------

    @staticmethod
    def _say(text, voice=""):
        cmd = ["/usr/bin/say"]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        return subprocess.run(cmd, check=False, capture_output=True,
                              timeout=180).returncode

    @staticmethod
    def _osascript_volume(percent):
        script = "set volume output volume %d" % int(percent)
        return subprocess.run(["/usr/bin/osascript", "-e", script],
                              check=False, capture_output=True,
                              timeout=10).returncode

    @staticmethod
    def _osascript_get_volume():
        proc = subprocess.run(["/usr/bin/osascript", "-e",
                               "output volume of (get volume settings)"],
                              check=False, capture_output=True, text=True, timeout=10)
        try:
            return int((proc.stdout or "").strip())
        except ValueError:
            return None

    # -- 生命周期 -----------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="keytime-announcer", daemon=True)
        self._thread.start()
        return self

    def stop(self, restore=True):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if restore and self.config.get("restore_volume") and self.saved_volume is not None:
            try:
                self._set_volume(self.saved_volume)
            except Exception:                      # noqa: BLE001 - 还原失败别崩
                pass
            self.saved_volume = None

    def speak(self, text):
        """排一句（非阻塞）。已有一句在播时，替换掉排队中的旧句。"""
        text = str(text or "").strip()
        if not text:
            return
        self.start()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(text)
        except queue.Full:                          # 理论上不会：刚清空过
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if text is None:
                break
            try:
                self._prepare_volume()
                self._speaker(text, self.config.get("voice") or "")
                self.spoken += 1
            except Exception as e:                 # noqa: BLE001 - 播报失败不致命
                self._log("播报失败：%s" % e)

    def _prepare_volume(self):
        volume = self.config.get("volume")
        if volume is None:
            return
        try:
            if self.saved_volume is None and self._get_volume is not None:
                self.saved_volume = self._get_volume()
            self._set_volume(int(volume))
        except Exception:                          # noqa: BLE001 - 音量不动也能播
            pass


# ================================================================ Bark 推送


def build_push_payload(device, title, body, params=None):
    """Bark JSON 负载（POST /push）。device_key 放 body，避免 URL 编码坑。"""
    params = params or {}
    payload = {"device_key": device.get("key") or "",
               "title": title or "", "body": body or ""}
    for key in ("level", "group", "sound", "ttl", "badge", "id",
                "isArchive", "call", "volume", "url"):
        value = params.get(key)
        if value not in (None, ""):
            payload[key] = str(value) if not isinstance(value, int) else value
    for key in ("level", "group", "sound"):        # 设备级覆盖
        value = device.get(key)
        if value:
            payload[key] = value
    payload.setdefault("isArchive", "1")
    return payload


def _urlopen(req, timeout, server):
    """localhost 一律绕过系统代理。

    macOS 的「系统设置 → 网络 → 代理」会被 urllib 自动读进来（**curl 不会**），
    而它的例外列表里通常只有 ``localhost``、没有 ``127.0.0.1`` —— 于是本机
    测试服务器会被塞进代理，得到 ``RemoteDisconnected``。真机推送 api.day.app
    仍然走系统代理（用户可能就靠它出网），只有回环地址直连。
    """
    host = (urllib.parse.urlsplit(server).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def push_one(device, title, body, params=None, timeout=None):
    """发一条推送 → (ok, detail)。**不重试**：失败交给上层退避（Bark 会封 IP）。"""
    payload = build_push_payload(device, title, body, params)
    server = str(device.get("server") or "https://api.day.app").rstrip("/")
    url = server + "/push"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with _urlopen(req, timeout or 8.0, server) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:            # 4xx/5xx：Bark 的封 IP 规则就是数它
        return False, "HTTP %s：%s" % (e.code, (e.read() or b"")[:200])
    except urllib.error.URLError as e:             # 代理没开 / DNS / 拒绝
        return False, _net_hint(e.reason, server)
    except Exception as e:                         # noqa: BLE001 - 网络问题
        return False, _net_hint(e, server)
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"raw": raw}
    if isinstance(parsed, dict) and parsed.get("code") not in (None, 200):
        return False, "Bark code %s：%s" % (parsed.get("code"), parsed.get("message"))
    return (200 <= int(code) < 300), (raw or "ok")[:200]


def _net_hint(error, server):
    """把「连不上」翻成一句能直接动手的话。"""
    text = str(error)
    hint = ""
    try:
        proxies = urllib.request.getproxies()
        proxy = proxies.get("https") or proxies.get("http") or ""
        if proxy and ("refused" in text.lower() or "disconnect" in text.lower()
                      or "timed out" in text.lower()):
            hint = ("（系统代理 %s 可能没开：curl 能通、python 不通就是这个原因；"
                    "要么开代理，要么给设备配 --server 直连地址）" % proxy)
    except Exception:                              # noqa: BLE001
        pass
    return "连不上 %s：%s%s" % (server, text, hint)


def push_all(devices, title, body, params=None, timeout=None):
    """推给所有启用的设备 → [(name, ok, detail)]。"""
    out = []
    for device in devices or []:
        if not device.get("enabled", True):
            continue
        ok, detail = push_one(device, title, body, params, timeout=timeout)
        out.append((device.get("name") or "device", ok, detail))
    return out


def push_params(config, mode="alarm", seq=None):
    """报警 / 确认后提醒 两套参数。seq 给 stack 模式区分每条通知。"""
    cfg = config or load_config()
    push = cfg.get("push") or {}
    params = {
        "level": push.get("level") or "timeSensitive",
        "group": push.get("group") or "keytime",
        "sound": push.get("sound") or "alarm",
        "ttl": push.get("ttl") or 3600,
    }
    if not push.get("stack_notifications", True):
        # 不堆叠：同一个 id 原地替换，手机上永远只有一条。
        params["id"] = "keytime-%s" % (seq or "alarm")
    if mode == "acked":
        params["sound"] = ""
        params["level"] = push.get("acked_level") or "passive"
    return params


# ================================================================ 把 warden 拉到最前


def summon(dry_run=False, log=None):
    """activate iTerm + select 到 warden 那个 tab（复用收尾提醒那条路）。"""
    log = log or (lambda msg: None)
    if dry_run:
        log("会执行 keep_session.summon_warden()")
        return True
    try:
        import keep_session as ks
    except Exception as e:                         # noqa: BLE001 - 独立运行时
        log("拿不到 keep_session，跳过置顶：%s" % e)
        return False
    try:
        return bool(ks.summon_warden())
    except Exception as e:                         # noqa: BLE001
        log("置顶失败：%s" % e)
        return False


def left_mac(threshold=None, config=None):
    """人是不是不在 Mac 前（系统空闲秒数超过阈值）。读不到 → False（不推手机）。"""
    cfg = config or load_config()
    limit = int(threshold if threshold is not None else cfg.get("leave_idle_sec") or 120)
    try:
        import keep_session as ks
        idle = ks.hid_idle_seconds()
    except Exception:                              # noqa: BLE001
        return False
    return bool(idle is not None and idle >= limit)


# ================================================================ CLI（独立自检用）


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="keytime.py", description="Key Time 自检工具")
    ap.add_argument("--config", default=None)
    ap.add_argument("--download", default=None)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出今天的 key time 和状态")
    p_p = sub.add_parser("push-test", help="给所有设备发一条测试推送")
    p_p.add_argument("message", nargs="?", default="这是一条 keytime 测试推送")
    sub.add_parser("devices", help="列出设备")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    kts, err = load_keytimes(args.download, cfg)
    now = datetime.datetime.now()
    if args.cmd == "push-test":
        results = push_all(load_devices(), "[KEY TIME 测试]",
                           args.message, push_params(cfg))
        for name, ok, detail in results:
            print("%s %s：%s" % ("✓" if ok else "✗", name, detail))
        return 0 if results and all(ok for _, ok, _ in results) else 1
    if args.cmd == "devices":
        for d in load_devices():
            print("- %s  %s…  %s  enabled=%s"
                  % (d["name"], d["key"][:6], d["server"], d["enabled"]))
        return 0
    if err:
        print("[warn] " + err, file=sys.stderr)
    for kt in kts:
        if kt.end_dt() <= now:
            continue
        print("%s  %-8s %s" % (kt.label(), kt.kind, kt.title))
    target = alarm_target(now, kts, load_state(), cfg)
    if target:
        print("\n现在该报警：%s（已超时 %d 分钟）"
              % (target.label(), target.overdue_minutes(now)))
        print("\n".join(text for _style, text in render_lines(target, now)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
