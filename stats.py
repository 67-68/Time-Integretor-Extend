#!/usr/bin/env python3
"""stats.py —— Focusmate 生涯统计（只读，给 warden 的「统计」视图供数）。

数据源：**公开只读 API**（`X-API-Key`，和 keep_session.py 用的是同一个 token）

    GET /v1/me                   → userId / name / memberSince / totalSessionCount / timeZone
    GET /v1/sessions?start=&end= → 区间内的全部 session

实测（2026-10-16，本机）：
- `start`/`end` 是 UTC ISO。**单次查询上限 365 天**（366 天直接 400 Bad Request），
  所以拉全量历史要按 ≤365 天分块 —— 见 `chunk_ranges()`。
- 返回的是**全量历史**（含已过去的），不是只看未来的窗口：账号 2025-12-26 起，
  一次「2026 全年」能拿到 2251 条。翻页参数没发现。
- 每条 session：`sessionId / sessionType / duration(ms) / startTime(UTC) / title / users[]`，
  `users[]` 里找到自己那条，才有 `joinedAt / completed / activityType / preferences`。
- `/v1/me` 的 `totalSessionCount`（本账号 1933）＝ 自己 `completed=true` 的场次数。

口径（已锁定，改这里就改了"命运"）
----------------------------------
- **完成 = 我自己那条 `users[]` 记录的 `completed == true`**（不是"订了"，也不是
  "进过房间"）。订了没完成的那部分另有 `total_booked` 可对照。
- **区间 = `memberSince` 的本地日期 → 昨天**（不含不完整的今天）。默认拿不到
  `memberSince` 就退到「第一条 session 的本地日期」。
- **分母 = 区间里的每一天**，包含一场都没做的空天 —— 所以「零 session 天数」才有意义。

缓存（增量刷新，不是每次重拉）
------------------------------
`temp/stat-history.json`（只存瘦身后的字段），TTL `CACHE_TTL` 秒。

- **新鲜缓存（TTL 内且 `until` 对得上）→ 一次网络都不打。**
- 过期 / 过了午夜 → **只拉「缓存缺口 + 最近 `TAIL_REFRESH_DAYS` 天」**，
  拼在旧缓存后面（`merge_sessions`），通常 1 个请求、几十条。
  全量重拉只在**第一次**或者显式 `full=True` 时发生。
- 缓存的 `until` 和这次要算的 `until` 不一致时**不能直接复用**，但也**不需要全量重来**
  —— 从 `cached_until + 1` 接着拉就行（`refresh_window` 保证缺口连得上）。
- 为什么尾部要回看几天：`completed` 是事后才落定的，迟到的取消/重订也只影响最近几天。
- `/v1/me` 只在没有缓存时问一次：`memberSince` 不会变。
- `force=True`（TUI 里 `stat refresh`）跳过新鲜度判断，但**仍然增量**；
  `full=True` 才是真的把两千多条重拉一遍。
- 拉失败时回落到旧缓存并标注；连缓存都没有才抛出去。

本模块**不 import rich**：拉取 / 计算 / 缓存都是纯 stdlib，渲染在 warden.py 的
`StatsView` 里。这样 `test/test_stat.py` 能完全离线（注入 `get_json` 假实现）跑。

用法：
    python3 stats.py                 # 拉一次（走缓存），纯文本打印
    python3 stats.py --force         # 无视缓存重拉
    python3 stats.py --save-only     # 只刷新缓存，不打印
"""
import argparse
import datetime
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")

API_BASE = "https://api.focusmate.com/v1"
CHUNK_DAYS = 365               # API 单次查询上限（实测 366 天 = 400）
CACHE_FILE = os.path.join(TEMP, "stat-history.json")
CACHE_TTL = 3600               # 缓存多久算新鲜（秒）
TAIL_REFRESH_DAYS = 3          # 增量刷新时，尾部回看几天（completed 是事后才落定的）
TIMEOUT = 30

# 渲染用的一些窗口
RECENT_DAYS = 14


# ---------------------------------------------------------------- 取数


def default_token():
    """复用 main.py 里那个只读 API token（懒 import：不拉 main 就不加载它）。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import main as fm  # noqa: E402
    return fm.TOKEN


def _get_json(url, token, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"X-API-Key": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _iso_utc(dt):
    return urllib.parse.quote(dt.astimezone(datetime.timezone.utc).isoformat())


def sessions_url(start, end):
    """`start` / `end` 是 aware datetime（会被转成 UTC）。"""
    return "%s/sessions?start=%s&end=%s" % (API_BASE, _iso_utc(start), _iso_utc(end))


def chunk_ranges(start, end, days=CHUNK_DAYS):
    """把 [start, end) 切成每块 ≤ days 天的区间。纯函数，单测直接喂。

    为什么要切：API 单次查询上限 365 天，超了直接 400 —— 不是分页，是拒绝。
    """
    out = []
    step = datetime.timedelta(days=max(1, int(days)))
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out


def local_date_of(value):
    """'2025-12-26T03:48:44.123000+00:00' / '...Z' → 本地 date。拿不到就 None。"""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone().date()


def local_dt_of(value):
    """同上的 datetime 版（naive 本地时间，和 schedule/daemon 同源）。"""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def fetch_me(token=None, get_json=_get_json):
    """GET /v1/me → 瘦身 dict。"""
    raw = get_json("%s/me" % API_BASE, token or default_token())
    u = raw.get("user") or {}
    return {
        "userId": u.get("userId") or "",
        "name": u.get("name") or "",
        "memberSince": u.get("memberSince") or "",
        "totalSessionCount": int(u.get("totalSessionCount") or 0),
        "timeZone": u.get("timeZone") or "",
    }


def slim_session(raw, user_id):
    """一条 API session → 只留统计要用的字段（我那条 users[] 记录才是"我"）。"""
    mine = {}
    for u in raw.get("users") or []:
        if user_id and u.get("userId") == user_id:
            mine = u
            break
    start = local_dt_of(raw.get("startTime"))
    if start is None:
        return None
    try:
        duration_min = int(round(float(raw.get("duration") or 0) / 60000.0))
    except (TypeError, ValueError):
        duration_min = 0
    return {
        "sessionId": raw.get("sessionId") or "",
        "start": start.replace(microsecond=0).isoformat(),
        "durationMin": duration_min,
        "completed": bool(mine.get("completed")),
        "joined": bool(mine.get("joinedAt")),
        "title": (mine.get("sessionTitle") or raw.get("title") or "")[:80],
        "activityType": mine.get("activityType") or "",
        "mine": bool(mine),
    }


def fetch_history(token=None, since=None, until=None, get_json=_get_json, me=None,
                  today=None):
    """拉 [since, until]（本地日期，含两端）的全部 session。

    分块 ≤365 天；返回的 sessions 是瘦身后的、按时间升序、且**按本地日期过滤过**
    （块的 UTC 边界会带上邻日的记录）。
    """
    token = token or default_token()
    if me is None:
        me = fetch_me(token=token, get_json=get_json)
    today = today or datetime.date.today()
    if until is None:
        until = today - datetime.timedelta(days=1)
    if since is None:
        since = local_date_of(me.get("memberSince")) or until

    # 本地日期边界 → UTC 瞬间（块边界也走这里，所以不会漏掉跨日的那几条）
    start_dt = datetime.datetime.combine(since, datetime.time(0, 0)).astimezone()
    end_dt = datetime.datetime.combine(until + datetime.timedelta(days=1),
                                       datetime.time(0, 0)).astimezone()

    out = []
    seen = set()
    for c_start, c_end in chunk_ranges(start_dt, end_dt):
        raw = get_json(sessions_url(c_start, c_end), token)
        for item in (raw or {}).get("sessions") or []:
            sid = item.get("sessionId")
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            slim = slim_session(item, me.get("userId"))
            if slim is None:
                continue
            day = local_date_of(slim["start"])
            if day is None or not (since <= day <= until):
                continue
            out.append(slim)
    out.sort(key=lambda s: s["start"])
    return {
        "fetched_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "me": me,
        "sessions": out,
    }


# ---------------------------------------------------------------- 缓存（增量刷新）


def load_cache(path=None):
    path = path or CACHE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_cache(history, path=None):
    path = path or CACHE_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def cache_age(cache, now=None):
    """缓存多少秒之前的；读不出时间就 +inf（当过期）。"""
    if not cache:
        return float("inf")
    stamp = local_dt_of(cache.get("fetched_at"))
    if stamp is None:
        return float("inf")
    return ((now or datetime.datetime.now()) - stamp).total_seconds()


def desired_until(today=None):
    return (today or datetime.date.today()) - datetime.timedelta(days=1)


def cache_since(cache):
    """缓存是从哪天开始攒的（本地 date）。读不出来就 None。"""
    if not cache:
        return None
    since = local_date_of((cache.get("me") or {}).get("memberSince"))
    return since or local_date_of(cache.get("since"))


def cache_until(cache):
    return local_date_of(cache.get("until")) if cache else None


def refresh_window(cache, until, tail_days=TAIL_REFRESH_DAYS):
    """增量刷新要从哪天开始重拉。纯函数，单测直接喂。

    两条原则：

    1. **缺口必须连上**：缓存只攒到 `cached_until`，那 `cached_until + 1` 之后
       那几天一条都不能少 —— 从缺口起点拉，不能只拉最近几天。
    2. **尾部回看 `tail_days` 天**：`completed` 是事后才落定的（还可能有迟到的
       取消/重订），所以尾部那个小窗口每次重拉一遍、覆盖旧记录。
       窗口大小固定，与缺口多长无关 —— 这就是"增量"的全部含义。

    返回 (fetch_from, note)。`fetch_from=None` = 没有可用缓存，只能全量。
    """
    tail_start = until - datetime.timedelta(days=max(1, int(tail_days)) - 1)
    cached_until = cache_until(cache)
    if cached_until is None:
        return None, "实时（全量）"
    gap_start = cached_until + datetime.timedelta(days=1)
    fetch_from = min(gap_start, tail_start)
    if fetch_from == gap_start and cached_until < until:
        return fetch_from, "增量（补 %d 天缺口：%s → %s）" % (
            (until - fetch_from).days + 1, fetch_from.isoformat(), until.isoformat())
    return fetch_from, "增量（回看最近 %d 天）" % tail_days


def merge_sessions(cached, fresh, fetch_from):
    """把「重拉窗口之前的旧记录」和「刚拉回来的新记录」拼起来。

    窗口内的旧记录一律丢掉（那正是要刷新/删掉的），按 sessionId 去重、按时间排序。
    `fetch_from=None` = 不留旧记录（全量）。
    """
    kept = []
    if fetch_from is not None:
        for s in cached or []:
            day = local_date_of(s.get("start"))
            if day is not None and day < fetch_from:
                kept.append(s)
    by_id = {}
    extra = []
    for s in kept + list(fresh or []):
        sid = s.get("sessionId")
        if sid:
            by_id[sid] = s            # fresh 在后 → 同 id 时覆盖成新记录
        else:
            extra.append(s)
    return sorted(list(by_id.values()) + extra, key=lambda s: s.get("start") or "")


def _trim_range(history, since, until):
    """兜底：缓存里只留 [since, until] 区间内的记录（别把未来/太早的带出去）。"""
    def in_range(s):
        day = local_date_of(s.get("start"))
        return day is not None and since <= day <= until
    history["sessions"] = [s for s in (history.get("sessions") or []) if in_range(s)]
    history["since"] = since.isoformat()
    history["until"] = until.isoformat()
    return history


def _stamp(now=None):
    return (now or datetime.datetime.now()).replace(microsecond=0).isoformat()


def load(force=False, ttl=CACHE_TTL, token=None, today=None, path=None,
         fetch=fetch_history, get_json=_get_json, now=None, full=False,
         tail_days=TAIL_REFRESH_DAYS):
    """拿一份可用的 history：新鲜缓存 → 增量刷新 → 全量重拉 → 旧缓存兜底。

    - 缓存新鲜（TTL 内且 `until` 对得上）→ **一次网络都不打**。
    - 缓存过期或过了午夜 → **只拉缺口 + 最近 `tail_days` 天**（通常 1 个请求、
      几十条），拼在旧缓存后面，而不是把两千多条重拉一遍。
    - `force=True`（TUI 的 `stat refresh`）→ 跳过新鲜度判断，但仍然走增量。
    - `full=True`（`stat refresh full`）→ 丢掉缓存全量重拉。

    返回 (history, note)：note 是给人看的一行（缓存 / 增量… / 实时（全量）/ 兜底）。
    """
    today = today or datetime.date.today()
    until = desired_until(today)
    cache = load_cache(path)

    # 1) 新鲜缓存：直接用，不打网络
    if (cache is not None and not force and not full
            and cache.get("until") == until.isoformat()
            and cache_age(cache, now=now) < ttl):
        return cache, "缓存"

    base = None if (full or cache is None) else cache
    try:
        if base is None:
            # 全量：/me + 按 ≤365 天分块把 memberSince → 昨天 全拉回来
            history = fetch(token=token, until=until, today=today, get_json=get_json)
            note = "实时（全量）"
            since = cache_since(history) or until
        else:
            since = cache_since(base) or until
            fetch_from, note = refresh_window(base, until, tail_days)
            me = base.get("me") or None          # memberSince 不会变，别每次都问 /me
            fresh = fetch(token=token, since=fetch_from, until=until, me=me,
                          today=today, get_json=get_json)
            history = {
                "fetched_at": _stamp(now),
                "since": since.isoformat(),
                "until": until.isoformat(),
                "me": fresh.get("me") or me or {},
                "sessions": merge_sessions(base.get("sessions"), fresh.get("sessions"),
                                           fetch_from),
                "source": "incremental",
            }
        _trim_range(history, since, until)
        history["source"] = history.get("source") or "api"
        save_cache(history, path)
        return history, note
    except Exception as e:
        if cache is not None:
            stale = dict(cache)
            stale["source"] = "cache"
            stale["error"] = "%s: %s" % (type(e).__name__, e)
            return stale, "拉取失败（%s）→ 退回缓存" % str(e)[:60]
        raise


# ---------------------------------------------------------------- 算数（纯函数）


def percentile(values, q):
    """线性插值分位数（inclusive）。q ∈ [0,1]。values 会被排序。"""
    vals = sorted(values)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def compute_stats(history, until=None, today=None):
    """把 history 算成一堆数字。纯函数：不联网、不读写文件、不 import rich。

    关键字段（`per_day` / 分位数 / 空天 / 峰值 / 连续）见模块 docstring 的口径。
    """
    me = (history or {}).get("me") or {}
    sessions = (history or {}).get("sessions") or []
    today = today or datetime.date.today()
    if until is None:
        until = local_date_of((history or {}).get("until")) or desired_until(today)

    since = local_date_of(me.get("memberSince"))
    if since is None and sessions:
        since = local_date_of(sessions[0].get("start"))
    if since is None:
        since = until
    if since > until:               # 账号第一天就是今天 → 还没得统计
        since = until

    days = [since + datetime.timedelta(days=i) for i in range((until - since).days + 1)]
    per_day = {d.isoformat(): {"booked": 0, "completed": 0, "minutes": 0} for d in days}
    total_minutes = 0
    for s in sessions:
        day = local_date_of(s.get("start"))
        if day is None:
            continue
        rec = per_day.get(day.isoformat())
        if rec is None:
            continue
        rec["booked"] += 1
        if s.get("completed"):
            rec["completed"] += 1
            rec["minutes"] += int(s.get("durationMin") or 0)
            total_minutes += int(s.get("durationMin") or 0)

    completed_vals = [per_day[d.isoformat()]["completed"] for d in days]
    booked_vals = [per_day[d.isoformat()]["booked"] for d in days]
    total_completed = sum(completed_vals)
    total_booked = sum(booked_vals)
    active_days = sum(1 for v in completed_vals if v > 0)
    zero_days = len(days) - active_days
    best = max(days, key=lambda d: per_day[d.isoformat()]["completed"]) if days else None

    # 连续天数（连续有"完成"的一天算一天）
    streak_best = streak_cur = 0
    for v in completed_vals:
        streak_cur = streak_cur + 1 if v > 0 else 0
        streak_best = max(streak_best, streak_cur)

    months = {}
    for d in days:
        key = d.strftime("%Y-%m")
        rec = per_day[d.isoformat()]
        m = months.setdefault(key, {"month": key, "booked": 0, "completed": 0})
        m["booked"] += rec["booked"]
        m["completed"] += rec["completed"]

    recent = [{"date": d.isoformat(), "completed": per_day[d.isoformat()]["completed"],
               "booked": per_day[d.isoformat()]["booked"]}
              for d in days[-RECENT_DAYS:]]

    return {
        "me": me,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "days": len(days),
        "total_completed": total_completed,
        "total_booked": total_booked,
        "completion_rate": (100.0 * total_completed / total_booked) if total_booked else 0.0,
        "mean_per_day": (total_completed / len(days)) if days else 0.0,
        "mean_active_day": (total_completed / active_days) if active_days else 0.0,
        "active_days": active_days,
        "zero_days": zero_days,
        "zero_rate": (100.0 * zero_days / len(days)) if days else 0.0,
        "max_day": {"date": best.isoformat() if best else None,
                    "completed": per_day[best.isoformat()]["completed"] if best else 0},
        "p25": percentile(completed_vals, 0.25),
        "median": percentile(completed_vals, 0.5),
        "p75": percentile(completed_vals, 0.75),
        "max_per_day": max(completed_vals) if completed_vals else 0,
        "min_per_day": min(completed_vals) if completed_vals else 0,
        "total_minutes": total_minutes,
        "total_hours": total_minutes / 60.0,
        "avg_minutes": (total_minutes / total_completed) if total_completed else 0.0,
        "streak_best": streak_best,
        "streak_current": streak_cur,
        "per_day": per_day,
        "by_month": [months[k] for k in sorted(months)],
        "recent": recent,
        "fetched_at": (history or {}).get("fetched_at") or "",
        "source": (history or {}).get("source") or "",
    }


# ---------------------------------------------------------------- 纯文本（CLI / 排查用）


def format_text(stats):
    """不开 TUI 时看的那份纯文本。渲染进 TUI 的在 warden.StatsView 里。"""
    out = []
    a = out.append
    a("生涯      %s → %s · %d 天" % (stats["since"], stats["until"], stats["days"]))
    a("总场次    完成 %d · 订了 %d · 完成率 %.1f%%"
      % (stats["total_completed"], stats["total_booked"], stats["completion_rate"]))
    a("每天平均  %.2f 场（含空天） / %.2f 场（只算做过的天）"
      % (stats["mean_per_day"], stats["mean_active_day"]))
    a("空天      %d 天一个 session 都没做（%.1f%%）" % (stats["zero_days"], stats["zero_rate"]))
    a("峰值      %s · %d 场" % (stats["max_day"]["date"], stats["max_day"]["completed"]))
    a("分布      p25 %g · 中位数 %g · p75 %g · 最大 %d"
      % (stats["p25"], stats["median"], stats["p75"], stats["max_per_day"]))
    a("累计      %.0f 小时 · 每场均值 %.1f 分钟" % (stats["total_hours"], stats["avg_minutes"]))
    a("连续      当前 %d 天 · 最长 %d 天" % (stats["streak_current"], stats["streak_best"]))
    a("按月      " + " · ".join("%s %d/%d" % (m["month"], m["completed"], m["booked"])
                                 for m in stats["by_month"]))
    a("最近      " + " ".join("%s:%d" % (r["date"][5:], r["completed"])
                               for r in stats["recent"]))
    if stats.get("fetched_at"):
        a("数据      %s（%s）" % (stats["fetched_at"], stats.get("source") or "?"))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Focusmate 生涯统计（只读）")
    ap.add_argument("--force", action="store_true", help="无视新鲜度，立刻增量刷新一次")
    ap.add_argument("--full", action="store_true", help="丢掉缓存，全量重拉（慢，很少需要）")
    ap.add_argument("--save-only", action="store_true", help="只刷缓存，不打印")
    ap.add_argument("--cache", default=CACHE_FILE, help="缓存文件路径")
    args = ap.parse_args(argv)
    history, note = load(force=args.force, full=args.full, path=args.cache)
    print("[stats] %s" % note)
    if args.save_only:
        return 0
    print(format_text(compute_stats(history)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
