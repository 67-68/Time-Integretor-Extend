#!/usr/bin/env python3
"""schedule.py —— data.csv 的解析 / 序列化 / 编辑（唯一真源）。

它在整套系统里的位置
--------------------
    data.csv（控制面，人手写）
        │  只读：未来 24 小时的契约
        ├──────────────▶ keep_session.py（数据面 daemon）──▶ Focusmate API
        │                                        └──▶ state / sins / verdicts
        └──────────────▶ warden.py（TUI）── 草稿区可增删改

**只有这一份实现**：daemon 和 TUI 都 import 它，所以"哪一行算一个 slot"
"写回文件该怎么写" 不会有第二套说法。

格式
----
纯文本 CSV，`#` 开头是注释（整行忽略），空行忽略。每行三种写法：

    HH:MM,duration              —— 每天
    weekday,HH:MM,duration      —— 只在 weekday 生效（mon..sun，也认 周一/星期一）
    *,HH:MM,duration            —— 同「每天」

- duration 只能是 25/50/75（Focusmate 只认这几种）
- 行内 `#` 之后的都是注释（`thu,19:00,50  # 数学` 合法）
- 同一 (weekday, HH:MM) 重复出现：后面的忽略 + 给 warning
- 同一个日期上不同写法撞车（`fri,09:00` 和 `*,09:00`）：
  按文件顺序取第一条，并且**只订一场**（day_slots 会去重）

解析**永不抛异常**：坏行跳过、收进 warnings 列表返回。因为这是人手写的文件，
一行手滑不该让 daemon 停摆或让 TUI 崩掉。

滚动 24 小时窗口（核心概念）
----------------------------
    「执行区」= (now, now+24h]      ← daemon 只订这里面的，取消要敲宣言
    「草稿区」= now+24h 之后        ← 随便改，零阻力
    「过去」  = 已经开始且没订上的   ← 只对账（记 missed / 记 sin），不补订

所以每天傍晚，第二天早上的场次会被提前订掉；不需要 23:59 的定时任务 ——
机器合盖睡了也没关系，醒来那一轮会把窗口重新填满。见 window()。

用法（给人看的）
----------------
    python3 schedule.py show             # 今天 + 明天：契约 / 草稿 / 过去
    python3 schedule.py show 2026-10-11  # 只看某一天
    python3 schedule.py show --week      # 一周概览（哪几天有契约）

纯 stdlib，Python 3.9 可跑。
"""
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DATA_CSV = os.path.join(HERE, "data.csv")
BACKUP_CSV = os.path.join(TEMP, "data.csv.bak")

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CN_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_ALIASES = {
    "mon": "mon", "monday": "mon", "周一": "mon", "星期一": "mon", "礼拜一": "mon",
    "tue": "tue", "tues": "tue", "tuesday": "tue", "周二": "tue", "星期二": "tue", "礼拜二": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed", "周三": "wed", "星期三": "wed", "礼拜三": "wed",
    "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu",
    "周四": "thu", "星期四": "thu", "礼拜四": "thu",
    "fri": "fri", "friday": "fri", "周五": "fri", "星期五": "fri", "礼拜五": "fri",
    "sat": "sat", "saturday": "sat", "周六": "sat", "星期六": "sat", "礼拜六": "sat",
    "sun": "sun", "sunday": "sun", "周日": "sun", "周天": "sun", "星期日": "sun",
    "星期天": "sun", "礼拜日": "sun", "礼拜天": "sun",
}
EVERY_DAY = {"*", "每天", "每日", "daily", "everyday", "every-day", "all"}
DURATIONS = ("25", "50", "75")
WINDOW_HOURS = 24          # 执行区长度：滚动 24 小时


# ---------------------------------------------------------------- 基础工具


def data_csv_path(path=None):
    return path or DATA_CSV


def today_weekday(now=None):
    return WEEKDAYS[(now or datetime.datetime.now()).weekday()]


def weekday_of_day(day):
    """datetime.date → 'mon'..'sun'"""
    return WEEKDAYS[day.weekday()]


def cn_weekday_of_day(day):
    return CN_WEEKDAYS[day.weekday()]


def normalize_weekday(raw):
    """'mon' / '周一' / '星期一' / '*' / '' → 'mon'|...|'*'；认不出来返回 None。"""
    v = (raw or "").strip().lower()
    if v in EVERY_DAY:
        return "*"
    return WEEKDAY_ALIASES.get(v)


def parse_hhmm(raw):
    """严格的 HH:MM → (hour, minute)；不合法返回 None。"""
    s = (raw or "").strip()
    if len(s) not in (4, 5) or s.count(":") != 1:
        return None
    hh, mm = s.split(":")
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def fmt_hhmm(hour, minute):
    return "%02d:%02d" % (hour, minute)


def slot_datetime(slot, day):
    """slot 在 day 这一天的本地 datetime（naive 本地时间，和 now 同源）。"""
    return datetime.datetime(day.year, day.month, day.day, slot["hour"], slot["minute"])


def key_of(day, slot_or_start):
    """状态机的 key：'2026-10-09T19:00'。带日期，所以跨天不会串味。"""
    if isinstance(slot_or_start, dict):
        hhmm = slot_or_start["start"]
    elif isinstance(slot_or_start, datetime.datetime):
        hhmm = fmt_hhmm(slot_or_start.hour, slot_or_start.minute)
    else:
        hhmm = str(slot_or_start)
    return "%sT%s" % (day.isoformat(), hhmm)


# ---------------------------------------------------------------- 解析


def parse_text(text, weekday=None, filename="data.csv"):
    """解析整个文件内容。

    weekday 非 None 时只留「该星期几 + 每天」的行（daemon 老接口用得上）。
    返回 (slots, warnings)；slots 按 (weekday 顺序, 时间) 排好序。
    """
    slots = []
    warnings = []
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            day, hhmm, duration = "*", parts[0], parts[1]
        elif len(parts) == 3:
            day, hhmm, duration = parts[0], parts[1], parts[2]
        else:
            warnings.append("%s:%d 格式不对（要 [weekday,]HH:MM,duration），已跳过: %r"
                            % (filename, lineno, raw.strip()))
            continue
        norm = normalize_weekday(day)
        if norm is None:
            warnings.append("%s:%d 星期几认不出来（要 mon..sun / 周一..周日 / *），已跳过: %r"
                            % (filename, lineno, raw.strip()))
            continue
        got = parse_hhmm(hhmm)
        if got is None:
            warnings.append("%s:%d 时间格式不对，已跳过: %r" % (filename, lineno, hhmm))
            continue
        if duration not in DURATIONS:
            warnings.append("%s:%d 时长只能是 25/50/75，已跳过: %r" % (filename, lineno, duration))
            continue
        if weekday is not None and norm not in ("*", weekday):
            continue
        key = (norm, hhmm)
        if key in seen:
            warnings.append("%s:%d 重复的 %s %s，已跳过" % (filename, lineno, norm, hhmm))
            continue
        seen.add(key)
        slots.append({"weekday": norm, "start": fmt_hhmm(*got), "duration": duration,
                      "hour": got[0], "minute": got[1], "lineno": lineno,
                      "raw": raw.rstrip("\n")})
    slots.sort(key=lambda s: (WEEKDAYS.index(s["weekday"]) if s["weekday"] in WEEKDAYS else -1,
                              s["hour"], s["minute"]))
    return slots, warnings


def read_text(path=None):
    """读 data.csv；读不到返回空字符串（第一次跑就是这种状态）。"""
    try:
        with open(data_csv_path(path), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def load_entries(path=None, weekday=None):
    """读文件 → (slots, warnings)。文件不存在 = ([], [])，不是错误。"""
    path = data_csv_path(path)
    return parse_text(read_text(path), weekday=weekday,
                      filename=os.path.basename(path))


def load_slots(path=None, weekday=None):
    """只有 slots 的版本（老接口，keep_session/test 都还在用）。"""
    return load_entries(path, weekday=weekday)[0]


def day_slots(entries, day):
    """某一天实际生效的契约：该星期几 + 每天的行，按时间排序、同时间只留一条。

    同时间撞车时按文件行号取第一条（先写的赢），后面的忽略。
    """
    wd = weekday_of_day(day)
    out, used = [], set()
    for slot in entries:
        if slot["weekday"] not in ("*", wd):
            continue
        if slot["start"] in used:
            continue
        used.add(slot["start"])
        out.append(slot)
    out.sort(key=lambda s: (s["hour"], s["minute"]))
    return out


def window(entries, now, hours=WINDOW_HOURS):
    """滚动窗口切分。

    返回 {
      "horizon": now+hours,
      "due":  [(day, slot, start), ...]   执行区里还没开始的 —— daemon 要订满的
      "past": [(day, slot, start), ...]   今天已经开始却没进 API 的 —— 只对账
    }
    """
    horizon = now + datetime.timedelta(hours=hours)
    span = int(hours // 24) + 2
    today = now.date()
    due, past = [], []
    for i in range(span):
        day = today + datetime.timedelta(days=i)
        for slot in day_slots(entries, day):
            start = slot_datetime(slot, day)
            if start <= now:
                if day == today:
                    past.append((day, slot, start))
                continue
            if start <= horizon:
                due.append((day, slot, start))
    due.sort(key=lambda t: t[2])
    past.sort(key=lambda t: t[2])
    return {"horizon": horizon, "due": due, "past": past}


# ---------------------------------------------------------------- 编辑（TUI 用）


def fingerprint(path=None):
    """文件内容的指纹：TUI 改文件前用它检查"我看到的还是现在这个文件吗"。"""
    import hashlib
    return hashlib.sha1(read_text(path).encode("utf-8")).hexdigest()


def backup(path=None):
    """改文件前留一份底：temp/data.csv.bak（和 download_calendar 用同一个位置）。"""
    path = data_csv_path(path)
    if not os.path.isfile(path):
        return None
    os.makedirs(TEMP, exist_ok=True)
    shutil.copy2(path, BACKUP_CSV)
    return BACKUP_CSV


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def fmt_row(weekday, hhmm, duration):
    return "%s,%s,%s" % (weekday, hhmm, duration)


def _row_sort_key(weekday, hhmm):
    got = parse_hhmm(hhmm) or (0, 0)
    return (WEEKDAYS.index(weekday) if weekday in WEEKDAYS else -1, got[0], got[1])


def add_row(path=None, weekday="*", hhmm="", duration="50"):
    """插一行（按 weekday+时间排好序插进去，保持文件整洁）。

    返回插到了第几行（1 起）。参数不合法 / 已存在 → ValueError（给 TUI 直接显示）。
    """
    path = data_csv_path(path)
    norm = normalize_weekday(weekday)
    if norm is None:
        raise ValueError("星期几认不出来：%r（要 mon..sun / 周一..周日 / *）" % weekday)
    got = parse_hhmm(hhmm)
    if got is None:
        raise ValueError("时间格式不对：%r（要 HH:MM，如 19:00）" % hhmm)
    hhmm = fmt_hhmm(*got)
    duration = str(duration).strip()
    if duration not in DURATIONS:
        raise ValueError("时长只能是 25/50/75，给了 %r" % duration)
    h, m = got
    if m % 15 != 0:
        raise ValueError("Focusmate 只认 15 分钟整点（:00/:15/:30/:45），%s 不行" % hhmm)

    lines = read_text(path).splitlines()
    entries, _ = parse_text("\n".join(lines), filename=os.path.basename(path))
    for slot in entries:
        if slot["weekday"] == norm and slot["start"] == hhmm:
            raise ValueError("data.csv 第 %d 行已经有 %s %s 了" % (slot["lineno"], norm, hhmm))

    new = fmt_row(norm, hhmm, duration)
    want = _row_sort_key(norm, hhmm)
    insert_at = len(lines)
    for idx, raw in enumerate(lines):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        parts = [p.strip() for p in body.split(",")]
        if len(parts) == 2:
            day, hh = "*", parts[0]
        elif len(parts) == 3:
            day, hh = parts[0], parts[1]
        else:
            continue
        day = normalize_weekday(day)
        if day is None or parse_hhmm(hh) is None:
            continue
        if _row_sort_key(day, hh) > want:
            insert_at = idx
            break

    backup(path)
    lines.insert(insert_at, new)
    _write(path, "\n".join(lines) + "\n")
    return insert_at + 1


def remove_row(path=None, lineno=None, expect=None):
    """删掉第 lineno 行。

    expect 给的是"我以为那一行长什么样"；对不上说明文件被别人改过，
    宁可报错也不删错行。返回被删掉的那行内容。
    """
    path = data_csv_path(path)
    lines = read_text(path).splitlines()
    if lineno is None or lineno < 1 or lineno > len(lines):
        raise ValueError("行号 %r 不在 1..%d 里" % (lineno, len(lines)))
    now_line = lines[lineno - 1]
    if expect is not None and now_line.strip() != str(expect).strip():
        raise ValueError("data.csv 变了：第 %d 行现在是 %r（我以为它是 %r）。先 refresh 再动手。"
                         % (lineno, now_line.strip(), str(expect).strip()))
    backup(path)
    del lines[lineno - 1]
    _write(path, "\n".join(lines) + "\n")
    return now_line


# ---------------------------------------------------------------- 给人看的


def zone_name(start, now, hours=WINDOW_HOURS):
    """'past' / 'locked' / 'draft' —— 三层摩擦的判据。"""
    if start <= now:
        return "past"
    if start <= now + datetime.timedelta(hours=hours):
        return "locked"
    return "draft"


def _cmd_show(argv):
    day = None
    week = False
    for a in argv:
        if a == "--week":
            week = True
        else:
            try:
                day = datetime.date.fromisoformat(a)
            except ValueError:
                print("日期要 YYYY-MM-DD，收到 %r" % a)
                return 2
    now = datetime.datetime.now()
    entries, warnings = load_entries()
    if week:
        print("data.csv: %s（%d 条契约规则）" % (data_csv_path(), len(entries)))
        for i in range(7):
            d = now.date() + datetime.timedelta(days=i)
            slots = day_slots(entries, d)
            zones = {}
            for s in slots:
                z = zone_name(slot_datetime(s, d), now)
                zones[z] = zones.get(z, 0) + 1
            print("  %s %s  %d 场  %s" % (d.isoformat(), cn_weekday_of_day(d), len(slots),
                                          " ".join("%s=%d" % kv for kv in sorted(zones.items()))))
        return 0

    days = [day] if day else [now.date(), now.date() + datetime.timedelta(days=1)]
    for d in days:
        slots = day_slots(entries, d)
        print("== %s %s（%d 场）==" % (d.isoformat(), cn_weekday_of_day(d), len(slots)))
        if not slots:
            print("  （空的：这一天的契约一条都没有 —— 不是放假，是断档）")
        for s in slots:
            start = slot_datetime(s, d)
            print("  %s %-3smin  行%d  %-6s  %s  %s"
                  % (s["start"], s["duration"], s["lineno"], zone_name(start, now),
                     s["weekday"], s["raw"].strip()))
    for w in warnings:
        print("[warn] %s" % w)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("show", "list"):
        return _cmd_show(argv[1:])
    print(__doc__.split("用法（给人看的）\n----------------\n", 1)[-1])
    return 2


if __name__ == "__main__":
    sys.exit(main())
