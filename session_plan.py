#!/usr/bin/env python3
"""session_plan.py —— 日历下载与 session 排布之间的中间层。

数据流（这是本模块存在的全部理由）::

    iCloud 日历
        │  只下载「工作 / 空闲」两个日历，今天 + 之后 7 天
        ▼
    temp/calendar-download.json   ← 原始事件 + 由事件得到的时段 T1/T2/...
        │  （temp/calendar.md 是同一份东西的人读版）
        │  用户按「时段」改排布：T1 50 50 / T1 25 25 25 25
        ▼
    temp/session-layouts.json     ← 每个日期、每个时段的自定义时长序列
        │
        ▼
    data.csv                      ← daemon 唯一会读的目标来源（精确日期行）

时间段的语义
------------
- 一个下载下来的日历事件就是一个**时段**（period）。一天内按开始时间编号
  T1/T2/...。时段类型 = 日历显示名（工作 / 空闲），时段名称 = 事件 SUMMARY。
- 用户只描述「这个时段里放几场、每场多久」，起点由程序算：第一场从时段起点开始，
  之后每一场从上一场结束后的下一个 15 分钟整点开始。这正好满足需求里的例子：
      11:00–13:00, T1 50 50       → 11:00–11:50, 12:00–12:50
      11:00–13:00, T1 25 25 25 25 → 11:00–11:25, 11:30–11:55, 12:00–12:25, 12:30–12:55
- **先完整验证，再落盘**：任何一场超出时段终点（例如 T1 50 50 50）就抛 ValueError，
  layout 和 data.csv 一个字都不改。
- 默认排布（用户没改过的时段）沿用原来的贪心规则：优先 50，塞不下退 25。

本模块不联网、不依赖 rich；download_calendar.py 和 warden.py 共用。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DOWNLOAD_FILE = os.path.join(TEMP, "calendar-download.json")
LAYOUTS_FILE = os.path.join(TEMP, "session-layouts.json")
#: 一次性 session（只属于某一天；session 命令默认写这里）
EXTRAS_FILE = os.path.join(TEMP, "session-extras.json")
#: 长期规则（按星期几重复；session --permanent 写这里）
RULES_FILE = os.path.join(TEMP, "session-rules.json")
DATA_CSV = os.path.join(HERE, "data.csv")
DURATIONS = (25, 50, 75)


def download_path(path=None):
    return path or os.environ.get("TI_CALENDAR_DOWNLOAD") or DOWNLOAD_FILE


def layouts_path(path=None):
    return path or os.environ.get("TI_SESSION_LAYOUTS") or LAYOUTS_FILE


def extras_path(path=None):
    return path or os.environ.get("TI_SESSION_EXTRAS") or EXTRAS_FILE


def rules_path(path=None):
    return path or os.environ.get("TI_SESSION_RULES") or RULES_FILE


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- 时间工具


def parse_hhmm(raw):
    s = str(raw or "").strip()
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def fmt_hhmm(h, m):
    return "%02d:%02d" % (h, m)


def ceil_quarter(h, m):
    """往上取整到 :00/:15/:30/:45。已有秒不存在，这里只处理 HH:MM。"""
    total = h * 60 + m
    rem = total % 15
    if rem:
        total += 15 - rem
    return total // 60, total % 60, total


def _minutes(h, m):
    return h * 60 + m


def _norm_durations(durations):
    out = []
    for d in durations:
        try:
            n = int(str(d).strip())
        except (TypeError, ValueError):
            raise ValueError("时长只能是 %s 分钟；你给的是 %r"
                             % ("/".join(str(x) for x in DURATIONS), d))
        if n not in DURATIONS:
            raise ValueError("时长只能是 %s 分钟；你给的是 %r" % ("/".join(map(str, DURATIONS)), d))
        out.append(n)
    return out


# ---------------------------------------------------------------- 下载结果


def empty_download():
    return {"version": 1, "generated_at": None, "start_date": None, "days": 0,
            "profiles": [], "source": "", "events": [], "periods": []}


def load_download(path=None):
    try:
        with open(download_path(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty_download()
    if not isinstance(data, dict):
        return empty_download()
    data.setdefault("version", 1)
    data.setdefault("events", [])
    data.setdefault("periods", [])
    if not isinstance(data["events"], list):
        data["events"] = []
    if not isinstance(data["periods"], list):
        data["periods"] = []
    return data


def save_download(data, path=None):
    _atomic_json(download_path(path), data)
    return download_path(path)


def periods_for_date(download_or_path, day):
    data = (load_download(download_or_path)
            if isinstance(download_or_path, (str, type(None))) else download_or_path)
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return [p for p in data.get("periods") or []
            if isinstance(p, dict) and p.get("date") == iso]


def build_periods(events, start_date, days, profiles=None):
    """把下载到的 CalEvent/dict 变成按天编号的时段。

    ``events`` 里每项可以是 download_calendar.CalEvent（有 start/end/calendar/title），
    也可以是已经规整过的 dict。跨午夜的时段归到开始那天。
    """
    days = int(days)
    start_date = (datetime.date.fromisoformat(start_date)
                  if isinstance(start_date, str) else start_date)
    end_date = start_date + datetime.timedelta(days=days)
    per_day = {}
    raw_events = []
    for ev in events or []:
        if isinstance(ev, dict):
            start = ev.get("start_iso") or ev.get("start")
            end = ev.get("end_iso") or ev.get("end")
            calendar = ev.get("calendar") or ev.get("kind") or ""
            title = ev.get("title") or ev.get("name") or ""
            all_day = bool(ev.get("all_day"))
            date = ev.get("date")
            if isinstance(start, str):
                try:
                    start = datetime.datetime.fromisoformat(start)
                except ValueError:
                    # 也接受 {"date": "...", "start": "11:00", "end": "13:00"}
                    got_s = parse_hhmm(start)
                    got_e = parse_hhmm(end) if isinstance(end, str) else None
                    if got_s and got_e and date:
                        try:
                            day = datetime.date.fromisoformat(str(date))
                        except ValueError:
                            day = None
                        if day:
                            start = datetime.datetime.combine(
                                day, datetime.time(got_s[0], got_s[1]))
                            end = datetime.datetime.combine(
                                day, datetime.time(got_e[0], got_e[1]))
                            if end <= start:
                                end += datetime.timedelta(days=1)
                        else:
                            continue
                    else:
                        continue
            if isinstance(end, str):
                try:
                    end = datetime.datetime.fromisoformat(end)
                except ValueError:
                    continue
        else:
            start, end = ev.start, ev.end
            calendar, title, all_day = ev.calendar, ev.title, ev.all_day
            date = None
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            continue
        if all_day or end <= start:
            continue
        if start.date() < start_date or start.date() >= end_date:
            continue
        if profiles and calendar not in profiles:
            continue
        day_iso = date or start.date().isoformat()
        raw_events.append({
            "date": day_iso,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "start_iso": start.isoformat(timespec="seconds"),
            "end_iso": end.isoformat(timespec="seconds"),
            "calendar": calendar,
            "title": title,
            "all_day": all_day,
        })
        per_day.setdefault(day_iso, []).append(raw_events[-1])
    periods = []
    for day_iso in sorted(per_day):
        items = sorted(per_day[day_iso], key=lambda e: (e["start"], e["end"], e["title"]))
        for i, ev in enumerate(items, 1):
            periods.append({
                "id": "T%d" % i,
                "date": day_iso,
                "start": ev["start"],
                "end": ev["end"],
                "type": ev["calendar"],
                "name": ev["title"],
                "calendar": ev["calendar"],
                "title": ev["title"],
                "start_iso": ev["start_iso"],
                "end_iso": ev["end_iso"],
            })
    return {"version": 1, "generated_at": _now(),
            "start_date": start_date.isoformat(), "days": days,
            "profiles": list(profiles or []),
            "events": raw_events, "periods": periods}


# ---------------------------------------------------------------- 排布计算


def _subject(period):
    pid = period.get("id") or "?"
    return "%s（%s %s–%s）" % (pid, period.get("date") or "?",
                              period.get("start") or "?", period.get("end") or "?")


def plan_period(period, durations):
    """按 duration 序列算出 session 列表；完全不合法就抛 ValueError（无副作用）。

    返回 [{start,end,duration,period_id,date,period_start,period_end,...}, ...]。
    """
    start_got = parse_hhmm(period.get("start"))
    end_got = parse_hhmm(period.get("end"))
    if start_got is None or end_got is None:
        raise ValueError("%s 的时间不是合法 HH:MM" % _subject(period))
    start_min = _minutes(*start_got)
    end_min = _minutes(*end_got)
    if end_min <= start_min:
        end_min += 24 * 60          # 跨午夜
    durations = _norm_durations(durations)

    cursor = start_min
    out = []
    for n, dur in enumerate(durations, 1):
        hh, mm, total = ceil_quarter(cursor // 60, cursor % 60)
        s_min = total
        e_min = s_min + dur
        if e_min > end_min:
            raise ValueError(
                "%s 放不下：第 %d 场 %dmin 会到 %s，超过时段终点 %s。什么都没改。"
                % (_subject(period), n, dur, _fmt_min(e_min), period.get("end")))
        base = None
        try:
            base = datetime.date.fromisoformat(str(period.get("date")))
        except ValueError:
            pass
        session_date = ((base + datetime.timedelta(days=s_min // (24 * 60))).isoformat()
                        if base else period.get("date"))
        out.append({
            "date": session_date,
            "period_id": period.get("id"),
            "period_date": period.get("date"),
            "period_start": period.get("start"),
            "period_end": period.get("end"),
            "period_type": period.get("type") or period.get("calendar") or "",
            "period_name": period.get("name") or period.get("title") or "",
            "start": _fmt_min(s_min),
            "end": _fmt_min(e_min),
            "start_min": s_min,
            "end_min": e_min,
            "duration": dur,
            "index": n,
        })
        cursor = e_min
    return out


def _fmt_min(total):
    total %= 24 * 60
    return "%02d:%02d" % (total // 60, total % 60)


def default_durations(period):
    """没自定义时的默认排布：优先 50，塞不下退 25，和旧 carve() 同一口味。"""
    start_got = parse_hhmm(period.get("start"))
    end_got = parse_hhmm(period.get("end"))
    if start_got is None or end_got is None:
        return []
    end_min = _minutes(*end_got)
    cur = _minutes(*start_got)
    if end_min <= cur:
        end_min += 24 * 60
    out = []
    while True:
        _, _, total = ceil_quarter(cur // 60, cur % 60)
        if total >= end_min:
            break
        for d in (50, 25):
            if total + d <= end_min:
                out.append(d)
                cur = total + d
                break
        else:
            break
    return out


# ---------------------------------------------------------------- 自定义 layout


def empty_layouts():
    return {"version": 1, "layouts": {}}


def load_layouts(path=None):
    try:
        with open(layouts_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_layouts()
    if not isinstance(raw, dict):
        return empty_layouts()
    raw.setdefault("version", 1)
    if not isinstance(raw.get("layouts"), dict):
        raw["layouts"] = {}
    return raw


def save_layouts(data, path=None):
    _atomic_json(layouts_path(path), data)
    return layouts_path(path)


def get_layout(layouts, day, period_id):
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    rec = (layouts.get("layouts") or {}).get(iso, {}).get(period_id)
    if isinstance(rec, dict):
        ds = rec.get("durations")
    elif isinstance(rec, list):
        ds = rec
    else:
        ds = None
    return _norm_durations(ds) if ds is not None else None


def set_layout(day, period_id, durations, layouts_path_=None, download_path_=None):
    """验证并保存一个时段的排布。成功返回 sessions；失败抛 ValueError且不落盘。"""
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    periods = periods_for_date(download_path_, day_iso)
    period = next((p for p in periods if p.get("id") == period_id), None)
    if period is None:
        raise ValueError("没有时段 %s（%s 的时段：%s）"
                         % (period_id, day_iso,
                            "、".join(p.get("id", "?") for p in periods) or "无"))
    sessions = plan_period(period, durations)       # 先验证；下面才写盘
    data = load_layouts(layouts_path_)
    data.setdefault("layouts", {}).setdefault(day_iso, {})[period_id] = {
        "durations": [int(d) for d in durations],
        "updated_at": _now(),
    }
    save_layouts(data, layouts_path_)
    return sessions


def remove_layout(day, period_id, layouts_path_=None):
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    data = load_layouts(layouts_path_)
    removed = (data.get("layouts") or {}).get(day_iso, {}).pop(period_id, None)
    if removed is not None:
        save_layouts(data, layouts_path_)
    return removed


def sessions_for_period(period, layouts):
    ds = get_layout(layouts, period.get("date"), period.get("id"))
    if ds is None:
        ds = default_durations(period)
    return plan_period(period, ds)


# ---------------------------------------------------------------- 一次性 session
#
# 数据流里 data.csv 是**生成物**（下载层 + layout → 精确日期行）。所以「今天临时加
# 一场」不能直接手写 data.csv —— 下一次 populate 会把它冲掉。一次性 session 单独
# 存一份，渲染 data.csv 时并进去；长期规则（按星期几重复）同理。


def empty_extras():
    return {"version": 1, "dates": {}}


def load_extras(path=None):
    try:
        with open(extras_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_extras()
    if not isinstance(raw, dict):
        return empty_extras()
    raw.setdefault("version", 1)
    dates = raw.get("dates")
    if not isinstance(dates, dict):
        dates = {}
    clean = {}
    for day, items in dates.items():
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)):
            continue
        rows = []
        for item in (items if isinstance(items, list) else []):
            try:
                start = "%02d:%02d" % parse_hhmm(item.get("start"))
            except (TypeError, ValueError):
                continue
            try:
                duration = int(item.get("duration"))
            except (TypeError, ValueError):
                continue
            if duration not in DURATIONS:
                continue
            rec = {"start": start, "duration": duration}
            if item.get("note"):
                rec["note"] = str(item["note"])
            if item.get("created_at"):
                rec["created_at"] = str(item["created_at"])
            rows.append(rec)
        if rows:
            clean[str(day)] = rows
    raw["dates"] = clean
    return raw


def save_extras(data, path=None):
    _atomic_json(extras_path(path), data)
    return extras_path(path)


def _drop_old_extras(data, today=None):
    """一次性 session 过期就忘掉：只保留今天及以后。"""
    today = (today or datetime.date.today()).isoformat()
    dates = data.get("dates") or {}
    data["dates"] = {d: v for d, v in dates.items() if d >= today}
    return data


def add_extra(day, hhmm, duration, note=None, path=None):
    """往某一天加一场一次性 session（重复就报错）。返回 (dates_key, rec)。"""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    got = parse_hhmm(hhmm)
    if got is None:
        raise ValueError("时间格式不对：%r（要 HH:MM）" % (hhmm,))
    start = fmt_hhmm(*got)
    try:
        duration = int(str(duration).strip())
    except (TypeError, ValueError):
        raise ValueError("时长只能是 %s 分钟；你给的是 %r"
                         % ("/".join(str(x) for x in DURATIONS), duration))
    if duration not in DURATIONS:
        raise ValueError("时长只能是 %s 分钟；你给的是 %r"
                         % ("/".join(str(x) for x in DURATIONS), duration))
    if got[1] % 15:
        raise ValueError("Focusmate 只认 15 分钟整点（:00/:15/:30/:45），%s 不行" % start)
    data = load_extras(path)
    rows = data["dates"].setdefault(iso, [])
    if any(r.get("start") == start for r in rows):
        raise ValueError("%s 已经有一场一次性 session 在 %s 了" % (iso, start))
    rec = {"start": start, "duration": duration, "created_at": _now()}
    if note:
        rec["note"] = str(note)
    rows.append(rec)
    rows.sort(key=lambda r: r["start"])
    save_extras(data, path)
    return iso, rec


def remove_extra(day, hhmm, path=None):
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    got = parse_hhmm(hhmm)
    start = fmt_hhmm(*got) if got else str(hhmm)
    data = load_extras(path)
    rows = data["dates"].get(iso) or []
    kept = [r for r in rows if r.get("start") != start]
    removed = [r for r in rows if r.get("start") == start]
    if removed:
        if kept:
            data["dates"][iso] = kept
        else:
            data["dates"].pop(iso, None)
        save_extras(data, path)
    return removed[0] if removed else None


def extras_for_date(data, day):
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return list((data.get("dates") or {}).get(iso) or [])


# ---------------------------------------------------------------- 长期规则


def empty_rules():
    return {"version": 1, "rules": []}


def load_rules(path=None):
    try:
        with open(rules_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_rules()
    if isinstance(raw, list):
        raw = {"version": 1, "rules": raw}
    if not isinstance(raw, dict):
        return empty_rules()
    raw.setdefault("version", 1)
    out = []
    for item in (raw.get("rules") if isinstance(raw.get("rules"), list) else []):
        if not isinstance(item, dict):
            continue
        wd = item.get("weekday")
        if isinstance(wd, int) and 0 <= wd <= 6:
            label = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][wd]
        else:
            label = str(wd or "").strip().lower()[:3]
            if label not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
                continue
        got = parse_hhmm(item.get("start"))
        try:
            duration = int(item.get("duration"))
        except (TypeError, ValueError):
            duration = None
        if got is None or duration not in DURATIONS:
            continue
        rec = {"weekday": label, "start": fmt_hhmm(*got), "duration": duration}
        if item.get("created_at"):
            rec["created_at"] = str(item["created_at"])
        out.append(rec)
    raw["rules"] = out
    return raw


def save_rules(data, path=None):
    _atomic_json(rules_path(path), data)
    return rules_path(path)


def add_rule(weekday, hhmm, duration, path=None):
    """加一条长期规则（按星期几重复）。重复就报错。"""
    label = str(weekday or "").strip().lower()[:3]
    if label not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        raise ValueError("星期几认不出来：%r（mon..sun / 周一..周日）" % (weekday,))
    got = parse_hhmm(hhmm)
    if got is None:
        raise ValueError("时间格式不对：%r（要 HH:MM）" % (hhmm,))
    start = fmt_hhmm(*got)
    try:
        duration = int(str(duration).strip())
    except (TypeError, ValueError):
        raise ValueError("时长只能是 %s 分钟；你给的是 %r"
                         % ("/".join(str(x) for x in DURATIONS), duration))
    if duration not in DURATIONS:
        raise ValueError("时长只能是 %s 分钟；你给的是 %r"
                         % ("/".join(str(x) for x in DURATIONS), duration))
    if got[1] % 15:
        raise ValueError("Focusmate 只认 15 分钟整点（:00/:15/:30/:45），%s 不行" % start)
    data = load_rules(path)
    if any(r["weekday"] == label and r["start"] == start for r in data["rules"]):
        raise ValueError("%s %s 已经有长期规则了" % (label, start))
    rec = {"weekday": label, "start": start, "duration": duration, "created_at": _now()}
    data["rules"].append(rec)
    data["rules"].sort(key=lambda r: (r["weekday"], r["start"]))
    save_rules(data, path)
    return rec


def remove_rule(weekday, hhmm, path=None):
    label = str(weekday or "").strip().lower()[:3]
    got = parse_hhmm(hhmm)
    start = fmt_hhmm(*got) if got else str(hhmm)
    data = load_rules(path)
    removed = [r for r in data["rules"] if r["weekday"] == label and r["start"] == start]
    if removed:
        data["rules"] = [r for r in data["rules"]
                         if not (r["weekday"] == label and r["start"] == start)]
        save_rules(data, path)
    return removed[0] if removed else None


def all_sessions(download, layouts):
    """下载结果 + 自定义 layout → 所有日期的 session（不写任何文件）。"""
    out = []
    for period in download.get("periods") or []:
        try:
            out.extend(sessions_for_period(period, layouts))
        except ValueError:
            # 坏 layout 不该让整个 TUI 崩：跳过这个时段，调用方可通过 layout 命令修。
            continue
    out.sort(key=lambda s: (s.get("date") or "", s["start_min"]))
    return out


def plan_rows(download, layouts, extras=None, rules=None):
    """一天一天排好的全部 session + 一次性 + 长期规则，统一成一种形状。

    形状：``{"date"|None, "weekday", "start", "duration", "source", ...}``。
    ``source`` 是 ``period`` / ``extra`` / ``rule``，界面和警告用得上。
    """
    rows = []
    for s in all_sessions(download, layouts):
        rec = dict(s)
        rec["source"] = "period"
        rec["weekday"] = None
        rows.append(rec)
    ex = extras if extras is not None else load_extras()
    for day, items in sorted((ex.get("dates") or {}).items()):
        for item in items:
            rows.append({"date": day, "weekday": None, "start": item["start"],
                         "duration": int(item["duration"]), "source": "extra",
                         "note": item.get("note") or ""})
    rl = rules if rules is not None else load_rules()
    for item in rl.get("rules") or []:
        rows.append({"date": None, "weekday": item["weekday"], "start": item["start"],
                     "duration": int(item["duration"]), "source": "rule"})
    return rows


def sessions_for_day(download, layouts, day, extras=None):
    """某一天**排好的** session（不含 API，不含长期规则展开）：时段 + 一次性。"""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    out = []
    for s in all_sessions(download, layouts):
        if s.get("date") != iso:
            continue
        rec = dict(s)
        rec.setdefault("period_id", rec.get("period_id") or "")
        rec["source"] = "period"
        out.append(rec)
    ex = extras if extras is not None else load_extras()
    for item in extras_for_date(ex, iso):
        out.append({"date": iso, "start": item["start"], "duration": int(item["duration"]),
                    "start_min": _minutes(*parse_hhmm(item["start"])),
                    "end": _fmt_min(_minutes(*parse_hhmm(item["start"]))
                                    + int(item["duration"])),
                    "period_id": "", "source": "extra"})
    out.sort(key=lambda s: s.get("start_min") if s.get("start_min") is not None
             else _minutes(*parse_hhmm(s["start"])))
    return out


# ---------------------------------------------------------------- data.csv


def render_data_csv(download, layouts, date_specific=True, extras=None, rules=None):
    """生成 daemon 读取的 data.csv。

    新链路默认写**精确日期**行（``YYYY-MM-DD,HH:MM,50``），因为下载窗口是
    「今天 + 之后 7 天」，用星期几会把一次性的日历安排错误地重复到未来每一周。
    schedule.py 已同时支持日期行和旧的 weekday 行。
    """
    lines = [
        "# 由 download_calendar.py / session_plan.py 生成 @ %s" % _now(),
        "# 来源：temp/calendar-download.json + temp/session-layouts.json"
        " + temp/session-extras.json + temp/session-rules.json",
        "# 格式：YYYY-MM-DD,HH:MM,duration（也兼容旧的 weekday,HH:MM,duration）",
    ]
    seen = set()
    for s in plan_rows(download, layouts, extras=extras, rules=rules):
        if s.get("source") == "rule" or not date_specific:
            if s.get("source") == "rule":
                key = (s["weekday"], s["start"])
                token = s["weekday"]
            else:
                # 兼容旧测试/旧调用方：按 weekday 写
                try:
                    day = datetime.date.fromisoformat(s["date"])
                except (TypeError, ValueError):
                    continue
                token = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]
                key = (token, s["start"])
        else:
            token = s.get("date")
            if not token:
                continue
            key = (token, s["start"])
        if key in seen:
            # 一次性 session 和时段排出来的撞了：以时段/规则为准，不重复写
            continue
        seen.add(key)
        lines.append("%s,%s,%d" % (token, s["start"], int(s["duration"])))
    return "\n".join(lines) + "\n"


def write_data_csv(download, layouts, path=None, extras=None, rules=None):
    path = path or os.environ.get("TI_DATA_CSV") or DATA_CSV
    text = render_data_csv(download, layouts, date_specific=True,
                           extras=extras, rules=rules)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return path


def plan_counts(download, layouts, extras=None, rules=None):
    """``{"period": n, "extra": n, "rule": n}`` —— 界面/警告用的一眼统计。"""
    out = {"period": 0, "extra": 0, "rule": 0}
    for row in plan_rows(download, layouts, extras=extras, rules=rules):
        out[row.get("source", "period")] = out.get(row.get("source", "period"), 0) + 1
    return out


def period_for_session(periods, start):
    """在一天 period 列表里找到包含某个 'HH:MM' 的时段（用于 UI 合并列）。"""
    got = parse_hhmm(start)
    if got is None:
        return None
    minute = _minutes(*got)
    for p in periods:
        a, b = parse_hhmm(p.get("start")), parse_hhmm(p.get("end"))
        if a is None or b is None:
            continue
        lo, hi = _minutes(*a), _minutes(*b)
        if hi <= lo:
            hi += 24 * 60
        # 跨午夜的情况下，session 起点在午夜后要按 +24h 比
        cand = minute
        if cand < lo:
            cand += 24 * 60
        if lo <= cand < hi:
            return p
    return None


def period_first_starts(download):
    """{date: {period_id: 第一场的 HH:MM}} —— UI 里给 T1 的后续行留空用。"""
    layouts = load_layouts()
    out = {}
    for p in download.get("periods") or []:
        date = p.get("date")
        try:
            sessions = sessions_for_period(p, layouts)
        except ValueError:
            sessions = []
        out.setdefault(date, {})[p.get("id")] = sessions[0]["start"] if sessions else None
    return out


# ---------------------------------------------------------------- 静音时段
#
# 「`period mute T1 --permanent` = 以后每个这个星期几的 T1 都静音」。
#
# 为什么不复用 Focusmate 的 quiet：那是**单场**开关，得一场一场点；而「这个时段
# 我就是要静音」是**排班意图**，和 `session --permanent` 是一类东西 —— 所以它
# 和长期规则一样，落在一张按星期几 / 按日期重复的表里（temp/session-mutes.json）。
#
# 记录里存**时间窗**（start/end）而不只是时段 ID：静音要能在没有日历下载层、
# 没有 data.csv 的地方判 —— daemon 的分心看门狗就属于这种，它只想知道
# 「现在这个钟点该不该出声」。
#
#   {"period": "T1", "weekday": "tue", "start": "11:00", "end": "12:05",
#    "permanent": true, "created_at": "..."}
#   {"period": "T1", "date": "2026-10-28", "start": "11:00", ...}   ← 只这一天

MUTES_FILE = os.path.join(TEMP, "session-mutes.json")


def mutes_path(path=None):
    return path or os.environ.get("TI_SESSION_MUTES") or MUTES_FILE


def empty_mutes():
    return {"version": 1, "mutes": []}


def _weekday_label(value):
    """0-6 / 'mon' / '周一' → 'mon'；认不出返回 None。"""
    if isinstance(value, int) and 0 <= value <= 6:
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][value]
    text = str(value or "").strip().lower()
    if text[:3] in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        return text[:3]
    cn = {"一": "mon", "二": "tue", "三": "wed", "四": "thu",
          "五": "fri", "六": "sat", "日": "sun", "天": "sun"}
    for key, label in cn.items():
        if key in text:
            return label
    return None


def _day_iso(day):
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def load_mutes(path=None):
    """读静音表（坏了就当空表 —— 静音这种辅助设置不值得让谁崩掉）。"""
    try:
        with open(mutes_path(path), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_mutes()
    if isinstance(raw, list):
        raw = {"version": 1, "mutes": raw}
    if not isinstance(raw, dict):
        return empty_mutes()
    out = []
    for item in (raw.get("mutes") if isinstance(raw.get("mutes"), list) else []):
        if not isinstance(item, dict):
            continue
        start = parse_hhmm(item.get("start"))
        end = parse_hhmm(item.get("end"))
        if start is None or end is None:
            continue
        rec = {
            "period": str(item.get("period") or "").strip().upper(),
            "start": fmt_hhmm(*start),
            "end": fmt_hhmm(*end),
            "permanent": bool(item.get("permanent")),
        }
        weekday = _weekday_label(item.get("weekday"))
        date = str(item.get("date") or "").strip()
        if weekday:
            rec["weekday"] = weekday
        if date:
            rec["date"] = date
        if not weekday and not date:
            continue
        if item.get("created_at"):
            rec["created_at"] = str(item["created_at"])
        out.append(rec)
    raw = dict(raw)
    raw["version"] = 1
    raw["mutes"] = out
    return raw


def save_mutes(data, path=None):
    _atomic_json(mutes_path(path), data)
    return mutes_path(path)


def mute_period(day, period, permanent=False, path=None):
    """把某一天的某个时段设成静音（``permanent`` = 以后每个这个星期几都静音）。

    ``period`` 是 :func:`periods_for_date` 的元素（要 ``id`` / ``start`` / ``end``）。
    同一目标重复设置就覆盖时间窗（排班可能被改过），不报错 —— 「我要它静音」
    这个意图没变。
    """
    if not isinstance(period, dict):
        raise ValueError("要传时段 dict（periods_for_date 的元素）")
    pid = str(period.get("id") or "").strip().upper()
    if not pid:
        raise ValueError("这个时段没有 id，没法设静音")
    start, end = parse_hhmm(period.get("start")), parse_hhmm(period.get("end"))
    if start is None or end is None:
        raise ValueError("时段 %s 的时间窗不完整（%r–%r）"
                         % (pid, period.get("start"), period.get("end")))
    rec = {
        "period": pid,
        "start": fmt_hhmm(*start),
        "end": fmt_hhmm(*end),
        "permanent": bool(permanent),
        "created_at": _now(),
    }
    if permanent:
        weekday = _weekday_label(day.weekday() if hasattr(day, "weekday") else day)
        if weekday is None:
            raise ValueError("认不出 %r 是星期几" % (day,))
        rec["weekday"] = weekday
    else:
        rec["date"] = _day_iso(day)

    data = load_mutes(path)
    keep = []
    for old in data["mutes"]:
        if permanent:
            same = old.get("period") == pid and old.get("weekday") == rec.get("weekday")
        else:
            same = old.get("period") == pid and old.get("date") == rec.get("date")
        if not same:
            keep.append(old)
    keep.append(rec)
    keep.sort(key=lambda r: (r.get("weekday") or "", r.get("date") or "", r.get("start") or ""))
    data["mutes"] = keep
    save_mutes(data, path)
    return rec


def unmute_period(day, period_id, permanent=False, path=None):
    """撤销静音。返回被删掉的记录列表。"""
    pid = str(period_id or "").strip().upper()
    data = load_mutes(path)
    if permanent:
        weekday = _weekday_label(day.weekday() if hasattr(day, "weekday") else day)
        gone = [r for r in data["mutes"]
                if r.get("period") == pid and r.get("weekday") == weekday]
    else:
        iso = _day_iso(day)
        gone = [r for r in data["mutes"]
                if r.get("period") == pid and r.get("date") == iso]
    if gone:
        data["mutes"] = [r for r in data["mutes"] if r not in gone]
        save_mutes(data, path)
    return gone


def mutes_for_day(day, data=None):
    """某一天生效的静音记录（按星期几重复的 + 只这一天的）。"""
    data = data if isinstance(data, dict) else load_mutes(data)
    iso = _day_iso(day)
    weekday = _weekday_label(day.weekday() if hasattr(day, "weekday") else day)
    out = []
    for rec in data.get("mutes") or []:
        if rec.get("date") == iso or (rec.get("weekday") and rec.get("weekday") == weekday):
            out.append(rec)
    return out


def muted_period_ids(day, data=None):
    """某一天要静音的时段 ID 集合（契约表格子用）。"""
    return {r["period"] for r in mutes_for_day(day, data) if r.get("period")}


def _crosses_midnight(rec):
    """这条静音记录的窗是不是跨午夜（``23:30–00:30``）。"""
    a, b = parse_hhmm(rec.get("start")), parse_hhmm(rec.get("end"))
    if a is None or b is None:
        return False
    return _minutes(*b) <= _minutes(*a)


def mute_window_at(moment, data=None):
    """``moment`` 落在哪个静音窗里 → 那条记录；没有就是 None。

    daemon 的分心看门狗用它：静音时段里就**不出声**（但照旧记日志）。
    只用到点/分；跨午夜（end < start）按 +24h 算。
    """
    if moment is None:
        return None
    data = data if isinstance(data, dict) else load_mutes(data)
    minute = moment.hour * 60 + moment.minute
    # 今天生效的窗 + **昨天**那条跨午夜的窗（23:30–00:30 这种）
    yesterday = moment.date() - datetime.timedelta(days=1)
    windows = [(rec, False) for rec in mutes_for_day(moment.date(), data)]
    # 昨天那条只算**跨午夜**的（23:30–00:30）；不跨的昨天就是昨天的事
    windows += [(rec, True) for rec in mutes_for_day(yesterday, data)
                if _crosses_midnight(rec)]
    for rec, from_yesterday in windows:
        a, b = parse_hhmm(rec.get("start")), parse_hhmm(rec.get("end"))
        if a is None or b is None:
            continue
        lo, hi = _minutes(*a), _minutes(*b)
        if hi <= lo:
            hi += 24 * 60
        cand = minute
        if cand < lo:
            cand += 24 * 60
        if from_yesterday and cand < 24 * 60:
            continue                  # 昨天那条只覆盖「今天 00:00 之后」那一段
        if lo <= cand < hi:
            return rec
    return None


def is_muted_at(moment, data=None):
    """``moment`` 是不是在静音时段里（daemon / 收尾 / 提醒都问这一个）。"""
    return mute_window_at(moment, data) is not None


# ---------------------------------------------------------------- CLI（给人看 / 调试）
def cmd_show(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    data = load_download()
    layouts = load_layouts()
    if not data.get("periods"):
        print("还没有下载结果：%s" % download_path())
        print("先跑：python3 download_calendar.py download")
        return 1
    for p in data["periods"]:
        ds = get_layout(layouts, p["date"], p["id"])
        try:
            sessions = sessions_for_period(p, layouts)
            body = "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                             for s in sessions)
        except ValueError as e:
            body = "❌ " + str(e)
        print("%s %s %-4s %s–%s  %s  %s"
              % (p["date"], p["id"], p.get("type") or "",
                 p.get("start"), p.get("end"), p.get("name") or "", body))

    extras = load_extras()
    if extras.get("dates"):
        print("\n一次性 session（只属于那一天）：")
        for day, items in sorted(extras["dates"].items()):
            print("  %s  %s" % (day, "  ".join("%s(%d)" % (r["start"], r["duration"])
                                               for r in items)))
    rules = load_rules()
    if rules.get("rules"):
        print("\n长期规则（按星期几重复）：")
        for r in rules["rules"]:
            print("  %s  %s(%d)" % (r["weekday"], r["start"], r["duration"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_show())
