#!/usr/bin/env python3
"""keytime.py —— Key Time（日历里的硬节点）：模型 / 挤压预测 / 播报 / 推送。

Key Time 是什么
---------------
日历里某些**不是 session** 的时间块是硬节点：到点必须做那件事，晚一分钟都是
从后面的时间盒里抢。哪些块算 Key Time、模板写什么，全在规则文件里
（``keytime_rules.py`` → ``data/keytime-rules.toml``）：规则按
「时间块名字 > 时间块类型（日历名）」匹配，用户自己改，不需要碰代码。
本模块只负责「怎么算、怎么喊、怎么推」，不碰 warden 的表格，也不写 data.csv。

    日历（CalDAV）──download_calendar.py──▶ temp/calendar-download.json["time_blocks"]
                                                        │
                                          keytime_rules.match()
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
- :func:`render_lines` / :func:`speech_text`  规则模板 + 内联变量（屏幕 / 语音）

两条铁律
--------
1. **只读日历、只写自己的状态文件。** keytime 不改变契约、不订场、不动 data.csv；
   唯一会写的外部状态是：系统音量（播报前后）和手机推送。
2. **报警强度从状态来，不从时间硬编码。** 到点 60 秒内一次，之后每分钟一次；
   确认（ack）之后停止播报，但「每分钟把 warden 拉到最前」还会继续 —— 这是用户
   明确要的行为（见 feature_intents/keytime.md）。

文件
----
    temp/keytime.json           配置（节拍、音量、推送档位；**不含日历名**）
    data/keytime-rules.toml     规则表：哪些时间块报警 + 模板文字（用户自己改）
    temp/keytime-state.json     确认记录（按 keytime 的 key = 日期+开始时间）
    data/keytime-devices.json    Bark 设备（含 key，**不进 git**）
    temp/calendar-download.json 日历中间层（time_blocks 由 download_calendar.py 写入）
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import keytime_rules as kr

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DATA = os.path.join(HERE, "data")

DEFAULT_CONFIG_FILE = os.path.join(TEMP, "keytime.json")
DEFAULT_STATE_FILE = os.path.join(TEMP, "keytime-state.json")
DEFAULT_DEVICES_FILE = os.path.join(DATA, "keytime-devices.json")
DEFAULT_DOWNLOAD_JSON = os.path.join(TEMP, "calendar-download.json")

DEFAULT_CONFIG = {
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
    """读配置；缺的键用默认值补齐（不会把默认值写回去）。

    老版本配置里的 ``calendars``（硬编码的日历名列表）现在**不再参与匹配**：
    哪些时间块报警由 ``data/keytime-rules.toml`` 决定。这里把旧键原样留着，
    不报错也不生效（用户从旧配置升级上来不会炸）。
    """
    raw = _read_json(config_path(path), {}) or {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))          # deep copy
    for key, value in raw.items():
        if key == "push" and isinstance(value, dict):
            cfg["push"].update(value)
        else:
            cfg[key] = value
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
    """一个 key time 时间块（日历事件；date/start/end 都是本地时间）。

    ``rule`` 是命中它的那条规则（``keytime_rules.Rule``）；模板文字从规则来。
    """

    __slots__ = ("date", "start", "end", "calendar", "title", "kind", "rule")

    def __init__(self, date, start, end, calendar, title="", kind=None, rule=None):
        self.date = str(date)
        self.start = str(start)
        self.end = str(end)
        self.calendar = str(calendar or "")
        self.title = str(title or "")
        self.rule = rule
        #: 命中的规则 id（没有规则时按日历名/标题猜一个，只为展示）
        self.kind = kind or (rule.id if rule is not None else
                             classify(self.calendar, self.title))

    # -- 时间 ---------------------------------------------------------

    def start_dt(self):
        return _combine(self.date, self.start)

    def end_dt(self):
        return _combine(self.date, self.end)

    def key(self):
        return "%sT%s" % (self.date, self.start)

    def block(self):
        """时间块名字：事件标题，没标题就用日历名。"""
        return self.title or self.calendar

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
    """日历名/标题 → 一个「像什么」的猜测。

    只给**没有规则**的 ``KeyTime``（测试里直接构造 / 旧数据）兜底用；真正
    决定报警与文案的是 ``data/keytime-rules.toml`` 里的规则。
    """
    text = "%s %s" % (calendar or "", title or "")
    if "通勤" in text or "路上" in text or " commute" in text.lower():
        return "commute"
    if "休息" in text or "睡觉" in text or "断电" in text or "rest" in text.lower():
        return "rest"
    return "generic"


def _as_ruleset(rules):
    """容忍传 RuleSet / list / None（None → 出厂规则）。"""
    if isinstance(rules, kr.RuleSet):
        return rules
    if isinstance(rules, (list, tuple)):
        return kr.RuleSet(rules=list(rules))
    return kr.default_rules()


def parse_keytimes(download, rules=None):
    """从日历中间层里取时间块，按规则筛出 key time。

    中间层新版写 ``time_blocks``（**全部日历**）；老文件写 ``keytimes``
    （已经按日历名筛过）；再老的文件只有 ``events``。三种都认。

    没有规则命中的时间块**不是** key time（工作 / 空闲 / 上课 默认不报警）。
    """
    rule_set = _as_ruleset(rules)
    raw = (download or {}).get("time_blocks")
    if raw is None:
        raw = (download or {}).get("keytimes")
    if raw is None:                                    # 最老的文件
        raw = [ev for ev in (download or {}).get("events") or []
               if not ev.get("all_day")]
    out = []
    for ev in raw or []:
        title = ev.get("title") or ""
        calendar = ev.get("calendar") or ""
        rule = rule_set.match(calendar, title)
        if rule is None:
            continue
        try:
            kt = KeyTime(ev.get("date") or str(ev.get("start_iso") or "")[:10],
                         ev.get("start"), ev.get("end"),
                         calendar, title, rule=rule)
        except (TypeError, ValueError):
            continue
        if not (kt.start_dt() < kt.end_dt()):
            continue
        out.append(kt)
    out.sort(key=lambda k: (k.date, k.start))
    return out


def load_rules(path=None):
    """读规则文件（同 :func:`keytime_rules.load_rules`，这里再导出一次方便调用）。"""
    return kr.load_rules(path)


def load_keytimes(download_path=None, rules=None):
    """读中间层 → (keytimes, error)。读不到不是异常：只是「今天没有 key time」。"""
    path = download_json_path(download_path)
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return [], "没有日历中间层（先跑 sync / download_calendar.py download）：%s" % path
    return parse_keytimes(data, rules), ""


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
    1. 已经开始的（start ≤ now）；
    2. **还在这个时间块里**（now < end）—— 过了 end 就立刻停，不再追账。
       以前是 end + lookback（默认 120 分钟）才停，结果「休息」块 19:30 结束
       后到 21:30 还在播「休息超时 126 分钟」，用户明确说不行；
    3. 没有被确认过（state.acked 里有它的 key）。

    同时命中多个（重叠的两个日历事件）时取**最早开始的**那个。
    ``config`` 只是为了兼容旧调用（lookback_min 已经不用了）。
    """
    now = _now(now)
    acked = ((state or {}).get("acked") or {})
    hits = [kt for kt in keytimes
            if kt.started(now) and not kt.ended(now) and kt.key() not in acked]
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
        "total": len(sessions or []),
        "day_end": end_of_day,
    }


# ================================================================ 文案（规则模板 + 内联变量）


#: 规则文件被删光 / KeyTime 没有规则（测试直接构造）时的兜底模板。
FALLBACK_RULE = kr.Rule(
    id="generic",
    headline="🚨 [KEY TIME 违约警报] {block}",
    text="""🚨 [KEY TIME 违约警报] {block}
-动作：立刻处理这个 key time
>⏱️ 已超时：{overdue_mins} 分钟（应于 {key_time} 启动，现已 {now_time}）
⏳ 这个 key time 正在被拖延；后面的时间盒按 1:1 顺延。
""",
    speech="key time 超时 {overdue_mins} 分钟：{block}。",
)

#: 行首前缀 → rich 样式（前缀本身不显示）。
LINE_PREFIXES = (("!", "bold red"), ("-", "bold magenta"),
                 (">", "yellow"), ("#", "dim"))

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _task_names(items, limit=2):
    names = []
    for item in items:
        for title in item.get("tasks") or []:
            title = str(title or "").strip()
            if title and title not in names:
                names.append(title)
    return names[:limit]


# -- 一整句的合成文案（模板里想省事就用这些，想自己写就用下面的细粒度变量） ----


def squeeze_text(sq):
    """这次拖延对后面排期的后果 —— 一整句（自动在挤出 / 顺延 / 没排期之间换句）。"""
    sq = sq or {}
    dropped = sq.get("dropped") or []
    pushed = sq.get("pushed") or []
    names = _task_names(dropped) or _task_names(pushed)
    if dropped:
        if names:
            return ("再不行动，今日队列中的%s将被直接挤出今日排期。"
                    % " 与 ".join("【%s】" % name for name in names))
        return "再不行动，今天后面 %d 场会被挤出排期。" % len(dropped)
    if pushed:
        tail = ("（%s）" % "、".join("【%s】" % name for name in names)) if names else ""
        return ("再不行动，今天后面 %d 场%s将整体后移 %d 分钟，今晚就晚 %d 分钟收工。"
                % (len(pushed), tail, sq.get("delay") or 0, sq.get("delay") or 0))
    if int(sq.get("total") or 0):
        return ("今天后面还有 %d 场排期 —— 你拖的每一分钟，都从它们身上扣。"
                % int(sq["total"]))
    return "今天后面已经没有排期了 —— 这一小时只能从睡眠和明天里扣。"


def squeeze_speech(sq):
    """同 :func:`squeeze_text`，但更短，给语音播报用（没后果时为空）。"""
    sq = sq or {}
    dropped = sq.get("dropped") or []
    pushed = sq.get("pushed") or []
    if dropped:
        names = _task_names(dropped)
        head = ("已经被挤出今日排期的是：%s。" % "、".join(names)) if names else ""
        if sq.get("extra_sessions"):
            head += "今晚要额外加班 %d 个 session。" % sq["extra_sessions"]
        return head
    if pushed:
        return "后面 %d 场整体后移 %d 分钟。" % (len(pushed), sq.get("delay") or 0)
    return ""


def extra_text(sq):
    """今晚要额外补几场（没有挤出时为空 —— 模板里那一行会自动消失）。"""
    count = int((sq or {}).get("extra_sessions") or 0)
    return ("你今晚将被迫额外加班 %d 个 Session (25m) 擦屁股！" % count) if count else ""


def goal_line(goal):
    """「总目标将因此失败」一整句；还没设总目标时给占位提示。"""
    goal = str(goal or "").strip()
    if goal:
        return "你的 Project 总目标：%s 将因此失败！" % goal
    return ("（这个 session 的任务没有 Project 总目标；"
            "用 keytime goal <文本> 设置后这里会显示）")


def goal_speech(goal):
    goal = str(goal or "").strip()
    return ("总目标：%s。" % goal) if goal else ""


# -- 变量表 ------------------------------------------------------------------


def render_values(keytime, now, ctx=None, config=None, acked=False):
    """把「现在的事实」摊成模板变量表（键名见 keytime-rules.toml 顶部注释）。"""
    now = _now(now)
    ctx = ctx or {}
    cfg = config if isinstance(config, dict) else load_config(config)
    sq = ctx.get("squeeze") or {}
    over = keytime.overdue_minutes(now)
    duration = int((keytime.end_dt() - keytime.start_dt()).total_seconds() // 60)
    remain = int((keytime.end_dt() - now).total_seconds() // 60)
    next_row = None
    for row in ctx.get("sessions") or []:
        start = row.get("start")
        if isinstance(start, str):
            try:
                start = _combine(keytime.date, start)
            except (TypeError, ValueError):
                continue
        if isinstance(start, datetime.datetime) and start > now:
            next_row = dict(row, start=start)
            break
    titles = [str(t).strip() for t in (next_row or {}).get("tasks") or []
              if str(t).strip()]
    goal = str(ctx.get("goal") or "").strip()
    acked_at = ctx.get("acked_at")
    if isinstance(acked_at, datetime.datetime):
        acked_at = acked_at.strftime("%H:%M")
    else:
        acked_at = str(acked_at or "")[11:16] if acked_at else ""
    dropped = sq.get("dropped") or []
    pushed = sq.get("pushed") or []
    return {
        "block": keytime.block(),
        "calendar": keytime.calendar,
        "kind": keytime.kind,
        "date": keytime.date,
        "date_short": keytime.date[5:],
        "key_time": keytime.start,
        "start": keytime.start,
        "key_end": keytime.end,
        "end": keytime.end,
        "now_time": now.strftime("%H:%M"),
        "overdue_mins": over,
        "duration_mins": duration,
        "remain_mins": remain,
        "next_task": titles[0] if titles else "后面的安排",
        "next_tasks": "、".join(titles[:3]) if titles else "后面的安排",
        "next_time": next_row["start"].strftime("%H:%M") if next_row else "",
        "next_in_mins": (int((next_row["start"] - now).total_seconds() // 60)
                         if next_row else ""),
        "delay": int(sq.get("delay") or 0),
        "pushed_count": len(pushed),
        "dropped_count": len(dropped),
        "dropped_mins": int(sq.get("extra_minutes") or 0),
        "dropped_tasks": "、".join(_task_names(dropped, limit=99)),
        "extra_sessions": int(sq.get("extra_sessions") or 0),
        "extra_minutes": int(sq.get("extra_minutes") or 0),
        "squeeze_text": squeeze_text(sq),
        "squeeze_speech": squeeze_speech(sq),
        "extra_text": extra_text(sq),
        "goal": goal,
        "goal_line": goal_line(goal),
        "goal_speech": goal_speech(goal),
        "chore": str(ctx.get("chore") or "").strip(),
        "acked_at": acked_at,
        "acked_line": "✓ 已确认（%s）—— 不再播报；计时器仍在走" % (acked_at or "刚刚"),
        "day_end": str(cfg.get("day_end") or "23:30"),
        "acked": "1" if acked else "",
    }


def format_vars(text, values):
    """把 ``{name}`` 换成变量值；不认识的变量**原样留着**（方便发现写错）。

    ``{{`` / ``}}`` 是字面量花括号。
    """
    text = str(text or "").replace("{{", "\x00").replace("}}", "\x01")

    def replace(match):
        name = match.group(1)
        if name in values:
            return str(values[name])
        return match.group(0)

    return _VAR_RE.sub(replace, text).replace("\x00", "{").replace("\x01", "}")


def _line_style(line):
    """行首前缀 → (style, 去掉前缀的正文)。"""
    for prefix, style in LINE_PREFIXES:
        if line.startswith(prefix):
            return style, line[len(prefix):]
    return "", line


def _bold_segments(body, style):
    """把 ``**重点**`` 切成 rich 片段；没有 ** 时就是一段。"""
    out, pos = [], 0
    for match in _BOLD_RE.finditer(body):
        if match.start() > pos:
            out.append((style, body[pos:match.start()]))
        out.append((("bold " + style).strip(), match.group(1)))
        pos = match.end()
    if pos < len(body):
        out.append((style, body[pos:]))
    return out or [(style, "")]


def render_text(text, values):
    """按模板文本渲染 → ``[[(style, text), ...], ...]``（每行一串片段）。

    空行保留（分段用）；某行模板非空但渲染成空串（比如 ``{extra_text}`` 为空）
    → 整行删掉。第一行固定红底白字。
    """
    lines = []
    for raw in str(text or "").splitlines():
        filled = format_vars(raw, values)
        if raw.strip() and not filled.strip():
            continue
        style, body = _line_style(filled)
        lines.append(_bold_segments(body, style))
    if not lines:                                      # 模板被删空 → 至少有一行
        lines = [[("bold white on red", "KEY TIME %s" % values.get("block", ""))]]
    first = "".join(text for _style, text in lines[0])
    lines[0] = [("bold white on red", first)]
    return lines


def plain_text(lines):
    """``render_lines`` 的结果 → 纯文本（推送正文 / 日志 / 测试断言用）。"""
    return "\n".join("".join(text for _style, text in line) for line in lines)


def _rule_of(keytime):
    return keytime.rule if keytime.rule is not None else FALLBACK_RULE


def headline(keytime, now, ctx=None, config=None):
    """报警标题（面板第一行 / 手机推送标题）。"""
    rule = _rule_of(keytime)
    values = render_values(keytime, now, ctx, config)
    if rule.headline:
        return format_vars(rule.headline, values).strip()
    for raw in (rule.text or FALLBACK_RULE.text).splitlines():
        if raw.strip():
            _style, body = _line_style(format_vars(raw, values))
            return body.strip()
    return "🚨 KEY TIME %s" % keytime.block()


def render_lines(keytime, now, ctx=None, acked=False, config=None):
    """屏幕模板 → ``[[(style, text), ...], ...]``（一行一串片段，别再自己拼）。

    确认之后只留「标题 + 已确认」两行：停止播报、但计时器还在走。
    """
    now = _now(now)
    ctx = ctx or {}
    rule = _rule_of(keytime)
    values = render_values(keytime, now, ctx, config, acked=acked)
    if acked:
        return [[("bold white on red", headline(keytime, now, ctx, config))],
                [("bold green", values["acked_line"])]]
    return render_text(rule.text or FALLBACK_RULE.text, values)


def speech_text(keytime, now, ctx=None, config=None):
    """语音播报稿：规则里的 ``speech``（没写就用标题）。太长时 ``say`` 自己排队。"""
    now = _now(now)
    ctx = ctx or {}
    rule = _rule_of(keytime)
    values = render_values(keytime, now, ctx, config)
    text = rule.speech or headline(keytime, now, ctx, config)
    text = format_vars(text, values)
    text = text.replace("**", "").replace("\n", " ").strip()
    return re.sub(r"\s{2,}", " ", text)


def display_line(keytime, now, ruleset=None, config=None):
    """还没到点时，契约视图顶部那一行（模板在规则的 ``[display] line``）。"""
    now = _now(now)
    values = render_values(keytime, now, {}, config)
    delta = int((keytime.start_dt() - now).total_seconds() // 60)
    if delta < 1:
        when = "现在"
    elif delta < 60:
        when = "%d 分钟后" % delta
    else:
        when = "%d 小时 %d 分钟后" % (delta // 60, delta % 60)
    values["in_mins"] = max(0, delta)
    values["when"] = when
    values["title"] = keytime.title
    template = (ruleset.display if isinstance(ruleset, kr.RuleSet) and ruleset.display
                else kr.DEFAULT_DISPLAY)
    return format_vars(template, values).replace("**", "").strip()


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
    """activate iTerm + select 到 warden 那个 tab（复用收尾提醒那条路）。

    **focus-only**：报警本来就发生在 warden 进程里，只需要把已有窗口拉到最前。
    绝不能让它新建窗口 —— 以前「找不到就新建」，而新起来的 warden 又会
    再 summon 一次，每隔几秒就多开一个窗口（看起来像 malware）。``quiet``
    则保证 keep_session 的 log 不 print 到 TUI 的 stdout（会把界面打乱）。
    """
    log = log or (lambda msg: None)
    if dry_run:
        log("会执行 keep_session.summon_warden(focus_only=True, quiet=True)")
        return True
    try:
        import keep_session as ks
    except Exception as e:                         # noqa: BLE001 - 独立运行时
        log("拿不到 keep_session，跳过置顶：%s" % e)
        return False
    try:
        return bool(ks.summon_warden(focus_only=True, quiet=True))
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
    ap.add_argument("--rules", default=None, help="规则文件路径（默认 data/keytime-rules.toml）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出接下来的 key time 和状态")
    p_p = sub.add_parser("push-test", help="给所有设备发一条测试推送")
    p_p.add_argument("message", nargs="?", default="这是一条 keytime 测试推送")
    sub.add_parser("devices", help="列出设备")
    sub.add_parser("rules", help="列出规则文件里的规则")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    ruleset, rules_error = kr.load_rules(args.rules)
    if args.cmd == "rules" or rules_error:
        print("规则文件：%s" % kr.rules_path(args.rules))
        for rule in ruleset.rules:
            print("  - %s" % rule.describe())
        if rules_error:
            print("[warn] %s" % rules_error, file=sys.stderr)
        if args.cmd == "rules":
            return 0
    kts, err = load_keytimes(args.download, ruleset)
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
    for key_time in kts:
        if key_time.end_dt() <= now:
            continue
        print("%s  %-10s %s" % (key_time.label(), key_time.kind, key_time.title))
    target = alarm_target(now, kts, load_state(), cfg)
    if target:
        print("\n现在该报警：%s（已超时 %d 分钟）"
              % (target.label(), target.overdue_minutes(now)))
        print(plain_text(render_lines(target, now, config=cfg)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
