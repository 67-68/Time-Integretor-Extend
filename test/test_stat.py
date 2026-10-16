#!/usr/bin/env python3
"""stats.py 回归测试：分块拉取 / 缓存 / 口径（分位数、空天、峰值、连续）。

    python3 test/test_stat.py        # 全绿退出 0，有失败退出 1

**完全不联网**：所有 API 访问都用注入的假 get_json；缓存写到临时目录，
不碰仓库里的 temp/stat-history.json。
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


st = load("stats", "stats.py")
TMP = tempfile.mkdtemp(prefix="stat-test-")
CACHE = os.path.join(TMP, "stat-history.json")

TODAY = datetime.date(2026, 1, 11)
UNTIL = datetime.date(2026, 1, 10)          # 「昨天」


# ---------------------------------------------------------------- 小工具 / 分位数

print("== 工具函数 ==")
check("chunk_ranges 不够一块 → 一块",
      st.chunk_ranges(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 3, 1)),
      [(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 3, 1))])
check("chunk_ranges 恰好 365 天 → 一块",
      len(st.chunk_ranges(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 1) + datetime.timedelta(days=365))),
      1)
check("chunk_ranges 366 天 → 两块（API 上限是 365）",
      len(st.chunk_ranges(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 1) + datetime.timedelta(days=366))),
      2)
chunks = st.chunk_ranges(datetime.datetime(2026, 1, 1),
                         datetime.datetime(2026, 1, 1) + datetime.timedelta(days=800))
check("块与块首尾相接、不重不漏",
      all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))
      and chunks[0][0] == datetime.datetime(2026, 1, 1)
      and chunks[-1][1] == datetime.datetime(2026, 1, 1) + datetime.timedelta(days=800),
      True)
check("每块都 ≤ 365 天",
      all((b - a).days <= 365 for a, b in chunks), True)

check("percentile 空列表 → 0", st.percentile([], 0.25), 0.0)
check("percentile 单值", st.percentile([7], 0.25), 7.0)
check("percentile p25（inclusive 线性插值）", st.percentile([0, 1, 2, 3, 4], 0.25), 1.0)
check("percentile p50", st.percentile([0, 1, 2, 3, 4], 0.5), 2.0)
check("percentile p75", st.percentile([0, 1, 2, 3, 4], 0.75), 3.0)

check("local_date_of 认 Z 结尾",
      st.local_date_of("2026-01-09T03:00:00Z"), datetime.date(2026, 1, 9))
check("local_date_of 认 naive", st.local_date_of("2026-01-09T03:00:00"), datetime.date(2026, 1, 9))
check("local_date_of 垃圾 → None", st.local_date_of("nope"), None)
check("local_date_of 空 → None", st.local_date_of(""), None)


# ---------------------------------------------------------------- 瘦身 / 拉取

print("== slim_session ==")
raw = {"sessionId": "s1", "duration": 3000000, "startTime": "2026-01-09T01:00:00Z",
       "title": "", "users": [
           {"userId": "other", "completed": True},
           {"userId": "me", "completed": False, "joinedAt": "2026-01-09T01:00:05Z",
            "sessionTitle": "我的标题", "activityType": "desk"}]}
slim = st.slim_session(raw, "me")
check("抓的是我那条 users 记录（别人的 completed 不算我的）", slim["completed"], False)
check("joinedAt 非空 → joined", slim["joined"], True)
check("duration 毫秒 → 分钟", slim["durationMin"], 50)
check("标题取自己的 sessionTitle", slim["title"], "我的标题")
check("activityType 取自己的", slim["activityType"], "desk")
check("没有 startTime → None", st.slim_session({"users": []}, "me"), None)
check("users 里没有我 → mine=False 且不算完成",
      (st.slim_session({"startTime": "2026-01-09T01:00:00Z", "users": []}, "me")["completed"],),
      (False,))

print("== fetch_history：分块 + 过滤 + 去重 ==")
calls = []


def fake_api(url, token, timeout=None):
    """假 API：记下请求区间，按区间返回两条数据（含跨界的邻日记录）。"""
    if "/me" in url:
        return {"user": {"userId": "me", "name": "测试", "memberSince": "2026-01-01T00:00:00Z",
                         "totalSessionCount": 3, "timeZone": "Asia/Hong_Kong"}}
    q = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in q.split("&"))
    import urllib.parse
    start = datetime.datetime.fromisoformat(urllib.parse.unquote(params["start"]))
    end = datetime.datetime.fromisoformat(urllib.parse.unquote(params["end"]))
    calls.append((start, end))
    return {"sessions": [
        # 区间内的一天
        {"sessionId": "in-%s" % start.date(), "duration": 3000000,
         "startTime": (start + datetime.timedelta(days=1)).isoformat(),
         "users": [{"userId": "me", "completed": True, "joinedAt": "x"}]},
        # 区间最后一天之后一天（跨块边界会重复出现）→ 必须被去重 + 区间过滤
        {"sessionId": "edge", "duration": 3000000,
         "startTime": (end - datetime.timedelta(hours=1)).isoformat(),
         "users": [{"userId": "me", "completed": True, "joinedAt": "x"}]},
    ]}


h = st.fetch_history(token="t", since=datetime.date(2026, 1, 1), until=UNTIL,
                     get_json=fake_api, today=TODAY)
check("拉取分块数（1/1 → 1/11 = 11 天 → 1 块）", len(calls), 1)
check("每块 ≤ 365 天", all((b - a).days <= 365 for a, b in calls), True)
check("区间边界 → UTC 瞬间（本地 1/1 00:00 起）",
      calls[0][0].astimezone().replace(tzinfo=None), datetime.datetime(2026, 1, 1, 0, 0))
check("end 是「until 的次日 00:00」（含 until 整天）",
      calls[0][1].astimezone().replace(tzinfo=None), datetime.datetime(2026, 1, 11, 0, 0))
check("结果按时间升序", h["sessions"] == sorted(h["sessions"], key=lambda s: s["start"]), True)
check("sessionId 去重", len(set(s["sessionId"] for s in h["sessions"])), len(h["sessions"]))
for s in h["sessions"]:
    check_true("每条都落在区间内", datetime.date.fromisoformat(s["start"][:10]) <= UNTIL)
check("me 带上了", h["me"]["userId"], "me")

calls[:] = []
long_h = st.fetch_history(token="t", since=datetime.date(2025, 1, 1),
                          until=datetime.date(2026, 1, 10), get_json=fake_api, today=TODAY)
check("跨年 → 分两块（375 天 > 365）", len(calls), 2)
check("第一块 365 天整", (calls[0][1] - calls[0][0]).days, 365)
check("长区间也不重复计数（跨块那条被去重）",
      len(set(s["sessionId"] for s in long_h["sessions"])), len(long_h["sessions"]))


# ---------------------------------------------------------------- 缓存

print("== 缓存 ==")
hist = {"fetched_at": "2026-01-11T09:00:00", "since": "2026-01-01", "until": UNTIL.isoformat(),
        "me": {"userId": "me", "memberSince": "2026-01-01T00:00:00", "totalSessionCount": 3},
        "sessions": []}

st.save_cache(hist, CACHE)
check("缓存写进去了", st.load_cache(CACHE)["since"], "2026-01-01")
check("缓存 age 读得出来（1 小时前）",
      st.cache_age(st.load_cache(CACHE), now=datetime.datetime(2026, 1, 11, 10, 0)), 3600.0)
check("坏缓存（不是 JSON）→ None",
      (open(CACHE + ".bad", "w", encoding="utf-8").write("{oops"),
       st.load_cache(CACHE + ".bad"))[1], None)

pulls = []


def fake_fetch(**kw):
    pulls.append(kw)
    out = dict(hist)
    out["fetched_at"] = "2026-01-11T10:00:00"
    out["until"] = kw.get("until", UNTIL).isoformat()
    return out


st.save_cache(hist, CACHE)
h2, note = st.load(ttl=3600, path=CACHE, fetch=fake_fetch, today=TODAY,
                   now=datetime.datetime(2026, 1, 11, 9, 30))
check("新鲜缓存 → 不重拉", len(pulls), 0)
check("新鲜缓存的 note", note, "缓存")
h2, note = st.load(ttl=3600, path=CACHE, fetch=fake_fetch, today=TODAY,
                   now=datetime.datetime(2026, 1, 11, 12, 0))
check("过期缓存 → 重拉", len(pulls), 1)
check("重拉的 note", note, "实时")

pulls[:] = []
st.save_cache(dict(hist, until="2026-01-09"), CACHE)      # until 对不上（昨天变了）
st.load(ttl=3600, path=CACHE, fetch=fake_fetch, today=TODAY,
        now=datetime.datetime(2026, 1, 11, 9, 30))
check("缓存里的 until 和这次要算的不一致 → 重拉（否则今天会被算成空天）", len(pulls), 1)

pulls[:] = []
st.save_cache(hist, CACHE)
st.load(force=True, path=CACHE, fetch=fake_fetch, today=TODAY,
        now=datetime.datetime(2026, 1, 11, 9, 30))
check("force=True 无视新鲜缓存", len(pulls), 1)


def boom(**kw):
    raise OSError("网络炸了")


st.save_cache(hist, CACHE)
h3, note = st.load(path=CACHE, fetch=boom, today=TODAY)
check("拉取失败 → 退回旧缓存", len(h3["sessions"]), 0)
check_true("note 里说明了失败", "失败" in note)
try:
    st.load(path=os.path.join(TMP, "nope.json"), fetch=boom, today=TODAY)
    check("没有缓存 + 拉取失败 → 抛出去（由 TUI 兜）", False, True)
except OSError:
    check("没有缓存 + 拉取失败 → 抛出去（由 TUI 兜）", True, True)


# ---------------------------------------------------------------- 口径（compute_stats）

print("== compute_stats 口径 ==")
PLAN = {
    "2026-01-01": [("09:00", True), ("10:00", True)],     # 完成 2
    "2026-01-02": [("09:00", False)],                     # 订了没完成
    "2026-01-03": [],                                     # 空天
    "2026-01-04": [("09:00", True), ("10:00", False)],    # 完成 1 / 订 2
    "2026-01-05": [("09:00", True), ("10:00", True), ("11:00", True)],   # 完成 3
}
sessions = []
for day, items in PLAN.items():
    for hhmm, done in items:
        sessions.append({"sessionId": "%s-%s" % (day, hhmm), "start": "%sT%s:00" % (day, hhmm),
                         "durationMin": 50, "completed": done, "joined": True, "title": "",
                         "activityType": "", "mine": True})
history = {"fetched_at": "2026-01-11T09:00:00", "source": "api",
           "me": {"userId": "me", "name": "测试", "memberSince": "2026-01-01T00:00:00",
                  "totalSessionCount": 7, "timeZone": "Asia/Hong_Kong"},
           "sessions": sessions}
s = st.compute_stats(history, until=UNTIL, today=TODAY)
# 区间 = 1/1 → 1/10（10 天）；完成 = 2+0+0+1+3 = 6；订了 = 2+1+0+2+3 = 8
check("区间起点取 memberSince（本地日）", s["since"], "2026-01-01")
check("区间终点是昨天", s["until"], "2026-01-10")
check("分母 = 区间每一天（含空天）", s["days"], 10)
check("总完成数", s["total_completed"], 6)
check("总订数", s["total_booked"], 8)
check("完成率", round(s["completion_rate"], 1), 75.0)
check("零 session 天数（完成口径：1/2、1/3 和 1/6~1/10）", s["zero_days"], 7)
check("有做的天数（1/1、1/4、1/5）", s["active_days"], 3)
check("每天平均（含空天，完成口径）", round(s["mean_per_day"], 2), 0.6)
check("每天平均（只算做过的天）", round(s["mean_active_day"], 2), 2.0)
check("最多的一天", s["max_day"], {"date": "2026-01-05", "completed": 3})
check("峰值 = max_per_day", s["max_per_day"], 3)
# 每天完成数：[2,0,0,1,3,0,0,0,0,0] → 排序后 [0,0,0,0,0,0,1,2,3,3]
check("中位数", s["median"], 0.0)
check("p25", s["p25"], 0.0)
check("p75", s["p75"], 0.75)
check("累计分钟（只算完成的场次）", s["total_minutes"], 6 * 50)
check("每场均值分钟", round(s["avg_minutes"], 1), 50.0)
check("累计小时", round(s["total_hours"], 1), 5.0)
check("最长连续（1/1 有 → 1/2、1/3 断；1/4、1/5 连 2）", s["streak_best"], 2)
check("当前连续（末尾 1/6 起全空 → 0）", s["streak_current"], 0)
check("按月聚合", [(m["month"], m["completed"], m["booked"]) for m in s["by_month"]],
      [("2026-01", 6, 8)])
check("最近 14 天（区间只有 10 天 → 全给）", len(s["recent"]), 10)
check("最近那天的明细", s["recent"][0], {"date": "2026-01-01", "completed": 2, "booked": 2})
check("per_day 覆盖每一天", len(s["per_day"]), 10)

print("== compute_stats 边界 ==")
s0 = st.compute_stats({}, until=UNTIL, today=TODAY)
check("空 history 不炸", s0["days"], 1)
check("空 history 完成数 0", (s0["total_completed"], s0["total_booked"]), (0, 0))
check("空 history 没有峰值日", s0["max_day"]["date"], UNTIL.isoformat())
check("空 history 分位数都是 0", (s0["median"], s0["p25"], s0["p75"]), (0.0, 0.0, 0.0))

one = {"me": {"memberSince": UNTIL.isoformat()}, "sessions": sessions}
s1 = st.compute_stats(one, until=UNTIL, today=TODAY)
check("memberSince 就是昨天 → 1 天（不是负数区间）", s1["days"], 1)

# 区间外的记录（今天/未来的）不进统计
future = {"me": {"memberSince": "2026-01-01T00:00:00"},
          "sessions": sessions + [
              {"sessionId": "later", "start": "2026-01-11T09:00:00", "durationMin": 50,
               "completed": True, "joined": True, "title": "", "activityType": "", "mine": True}]}
s2 = st.compute_stats(future, until=UNTIL, today=TODAY)
check("今天的记录不算进昨天为止的统计", s2["total_completed"], 6)

# 没有 memberSince 时退到第一条 session 的日期
s3 = st.compute_stats({"me": {}, "sessions": sessions}, until=UNTIL, today=TODAY)
check("没有 memberSince → 用第一条 session 的本地日", s3["since"], "2026-01-01")

print("== format_text ==")
txt = st.format_text(s)
for key in ("生涯", "总场次", "每天平均", "空天", "峰值", "分布", "累计", "连续", "按月"):
    check("纯文本里有 %s" % key, key in txt, True)
check("纯文本不炸（数字对得上）", "完成 6" in txt, True)


os.unlink(CACHE + ".bad") if os.path.exists(CACHE + ".bad") else None
import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
