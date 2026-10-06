#!/usr/bin/env python3
"""download_calendar.py 回归测试：切分算法 + 端到端（离线 .ics）+ data.csv 兼容性。

    python3 test/test_download_calendar.py        # 全绿退出 0，有失败退出 1

不联网、不写仓库里的任何文件（只在临时目录里造 .ics）。
"""
import datetime
import importlib.util
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dc = ks = None
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


SELFSTUDY = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:自习
BEGIN:VEVENT
UID:s1
DTSTART;TZID=Asia/Shanghai:20261007T141000
DTEND;TZID=Asia/Shanghai:20261007T153000
SUMMARY:复习数学
END:VEVENT
BEGIN:VEVENT
UID:s2
DTSTART;TZID=Asia/Shanghai:20261007T160000
DTEND;TZID=Asia/Shanghai:20261007T162000
SUMMARY:只有20分钟
END:VEVENT
BEGIN:VEVENT
UID:s3
DTSTART;TZID=Asia/Shanghai:20261011T090000
DTEND;TZID=Asia/Shanghai:20261011T120000
SUMMARY:周日上午自习
END:VEVENT
BEGIN:VEVENT
UID:s4
DTSTART;VALUE=DATE:20261010
DTEND;VALUE=DATE:20261011
SUMMARY:全天事件
END:VEVENT
BEGIN:VEVENT
UID:s5
DTSTART;TZID=Asia/Shanghai:20261009T200000
DTEND;TZID=Asia/Shanghai:20261009T210000
RRULE:FREQ=WEEKLY;BYDAY=FR
SUMMARY:周五晚自习
END:VEVENT
END:VCALENDAR
"""
CLASS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:上课
BEGIN:VEVENT
UID:c1
DTSTART;TZID=Asia/Shanghai:20261007T090000
DTEND;TZID=Asia/Shanghai:20261007T120000
SUMMARY:高等数学
END:VEVENT
END:VCALENDAR
"""
REST = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:休息
BEGIN:VEVENT
UID:r1
DTSTART;TZID=Asia/Shanghai:20261007T120000
DTEND;TZID=Asia/Shanghai:20261007T130000
SUMMARY:午休
END:VEVENT
END:VCALENDAR
"""


def run(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "download_calendar.py"), *args],
                          cwd=ROOT, capture_output=True, text=True)


def test_carve():
    TZ = dc.LOCAL_TZ

    def T(h, m=0):
        return datetime.datetime(2026, 10, 7, h, m, tzinfo=TZ)

    def carve(blocks):
        return [(s.strftime("%H:%M"), d) for s, d in dc.carve(dc.merge_windows(blocks))]

    # 需求原文里的两个例子
    check("需求例: 14:10-15:30 → 14:15(50)", carve([(T(14, 10), T(15, 30))]), [("14:15", 50)])
    check("需求例: 14:00-17:00 → 50x3", carve([(T(14, 0), T(17, 0))]),
          [("14:00", 50), ("15:00", 50), ("16:00", 50)])
    # 边界
    check("<25min 丢弃", carve([(T(9, 0), T(9, 20))]), [])
    check("正好 25min 且对齐 → 25", carve([(T(9, 0), T(9, 25))]), [("09:00", 25)])
    check("对齐吃掉起点 → 无", carve([(T(9, 5), T(9, 30))]), [])
    check("30min → 只能 25", carve([(T(9, 0), T(9, 30))]), [("09:00", 25)])
    check("50min 整 → 50", carve([(T(9, 0), T(9, 50))]), [("09:00", 50)])
    check("51min → 50，剩 1min 丢", carve([(T(9, 0), T(9, 51))]), [("09:00", 50)])
    check("95min → 50 + 25", carve([(T(9, 0), T(10, 35))]), [("09:00", 50), ("10:00", 25)])
    check("重叠两段先合并", carve([(T(14, 0), T(15, 0)), (T(14, 30), T(17, 0))]),
          [("14:00", 50), ("15:00", 50), ("16:00", 50)])
    check("非 quarter 起点 9:03 → 9:15", carve([(T(9, 3), T(10, 5))]), [("09:15", 50)])


def test_plan_week():
    TZ = dc.LOCAL_TZ

    def T(d, h, m=0):
        return datetime.datetime(2026, 10, d, h, m, tzinfo=TZ)

    now = T(6, 21, 25)                       # 周二晚 21:25
    events = [dc.CalEvent(T(6, 20, 0), T(6, 23, 0), "自习"),
              dc.CalEvent(T(7, 9, 0), T(7, 12, 0), "上课")]
    plans = dc.plan_week(events, datetime.date(2026, 10, 6), 2, ["上课", "休息"], [], False, now)
    check("今天过去的时段被裁到 now",
          [(s.strftime("%H:%M"), d) for s, d in plans[0].sessions],
          [("21:30", 50), ("22:30", 25)])
    check("不会有 session 早于 now", all(s > now for p in plans for s, _ in p.sessions), True)
    check("上课分组被忽略", [(s.strftime("%H:%M"), d) for s, d in plans[1].sessions], [])

    # 分组过滤语义
    ev = dc.CalEvent(T(7, 9, 0), T(7, 12, 0), "上课")
    check("黑名单: 上课 被挡", dc.grouped_out(ev, ["上课"], []), True)
    check("黑名单: 自习 放过",
          dc.grouped_out(dc.CalEvent(T(7, 9, 0), T(7, 12, 0), "自习"), ["上课"], []), False)
    check("白名单优先: use=上课 放过", dc.grouped_out(ev, ["上课"], ["上课"]), False)
    check("白名单优先: use=自习 挡掉上课", dc.grouped_out(ev, [], ["自习"]), True)


def test_cli(ics_args):
    r = run("populate", *ics_args, "--start", "2026-10-07", "--days", "5", "--dry-run")
    check("populate --dry-run 退出码 0", r.returncode, 0)
    marker = "---- data.csv（预览，不写）----"
    body = [l for l in r.stdout[r.stdout.index(marker) + len(marker):].splitlines()
            if l.strip() and not l.startswith("#")]
    check("生成的 csv 正文", body,
          ["wed,14:15,50", "fri,20:00,50", "sun,09:00,50", "sun,10:00,50", "sun,11:00,50"])

    md = run("fetch", *ics_args, "--start", "2026-10-07", "--days", "1", "--dry-run").stdout
    check("md 里有需求例的时间段", "| 14:10–15:30 | 80 min |" in md, True)
    check("md 切出 14:15–15:05", "| 14:15–15:05 | 50 min |" in md, True)
    check("md 把 <25min 的段单独列进 details", "太短被丢掉的时间段（1" in md, True)
    check("md 列出被忽略的上课事件", "`上课` 09:00–12:00" in md, True)

    check("没配置 + 不联网 → 报错退出 1", run("populate", "--days", "1").returncode, 1)
    check("全被过滤掉 → 守卫拦住（退出 1）",
          run("populate", *ics_args, "--days", "1", "--ignore", "自习").returncode, 1)
    check("--days 0 被拒", run("fetch", "--days", "0").returncode, 1)


def test_csv_compat():
    def slots(text, **kw):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            return [(s["weekday"], s["start"]) for s in ks.load_slots(path, **kw)]
        finally:
            os.unlink(path)

    check("旧两列格式仍可读", slots("# old\n16:30,50\n"), [("*", "16:30")])
    new = "# new\ntue,21:30,25\nwed,16:30,50\n*,12:00,50\n",
    check("新三列 + 每天行", slots(*new), [("*", "12:00"), ("tue", "21:30"), ("wed", "16:30")])
    check("按星期过滤 tue", slots(*new, weekday="tue"), [("*", "12:00"), ("tue", "21:30")])
    check("按星期过滤 mon", slots(*new, weekday="mon"), [("*", "12:00")])
    check("中文星期别名", slots("# x\n周三,09:00,25\n"), [("wed", "09:00")])
    check("缺口时间被 warn 跳过", slots("# x\n9:60,25\n"), [])


def main():
    global dc, ks
    dc = load("dc", "download_calendar.py")
    ks = load("ks", "keep_session.py")

    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, text in (("selfstudy", SELFSTUDY), ("class", CLASS), ("rest", REST)):
            p = os.path.join(tmp, name + ".ics")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            paths[name] = p
        ics_args = ["--ics", paths["selfstudy"], "--ics", paths["class"], "--ics", paths["rest"]]

        test_carve()
        test_plan_week()
        test_cli(ics_args)
        test_csv_compat()

    print()
    if fails:
        print("失败 %d 项 ❌" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
