#!/usr/bin/env python3
"""session_plan.py 回归测试：T1 排布 / 超时回滚 / 下载层 → 精确日期 data.csv。"""
import datetime
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import schedule as sch
import session_plan as sp

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


P = {"id": "T1", "date": "2026-10-26", "start": "11:00", "end": "13:00",
     "type": "工作", "name": "SAT 准备"}

print("== 需求例：时段内任意排布 ==")
check("T1 50 50", [(s["start"], s["end"], s["duration"])
                   for s in sp.plan_period(P, [50, 50])],
      [("11:00", "11:50", 50), ("12:00", "12:50", 50)])
check("T1 25 25 25 25", [(s["start"], s["end"], s["duration"])
                         for s in sp.plan_period(P, [25, 25, 25, 25])],
      [("11:00", "11:25", 25), ("11:30", "11:55", 25),
       ("12:00", "12:25", 25), ("12:30", "12:55", 25)])
check("默认优先 50", sp.default_durations(P), [50, 50])
try:
    sp.plan_period(P, [50, 50, 50])
    check("超时应该报错", False, True)
except ValueError as e:
    check_true("超时错误说清第几场/超过哪儿", "第 3 场" in str(e) and "13:00" in str(e))
try:
    sp.plan_period(P, [40])
    check("非法时长应该报错", False, True)
except ValueError as e:
    check_true("只认 25/50/75", "25/50/75" in str(e))

print("== 落盘：验证失败 = 零副作用 ==")
TMP = tempfile.mkdtemp(prefix="session-plan-test-")
DL = os.path.join(TMP, "download.json")
LP = os.path.join(TMP, "layouts.json")
data = sp.build_periods([], "2026-10-26", 1, profiles=["工作"])
data["periods"] = [dict(P), {"id": "T2", "date": "2026-10-26", "start": "15:00",
                            "end": "16:00", "type": "工作", "name": "托福 准备"}]
sp.save_download(data, DL)
try:
    sp.set_layout("2026-10-26", "T1", [50, 50, 50], layouts_path_=LP, download_path_=DL)
    check("越界应报错", False, True)
except ValueError:
    pass
check("越界后 layout 文件没创建", os.path.exists(LP), False)

sessions = sp.set_layout("2026-10-26", "T1", [25, 25, 25, 25],
                         layouts_path_=LP, download_path_=DL)
check("合法排布落盘", len(sessions), 4)
check("layout 文件已创建", os.path.exists(LP), True)
layouts = sp.load_layouts(LP)
check("layout 里是四个 25", [s["duration"] for s in sessions], [25, 25, 25, 25])
csv = sp.render_data_csv(sp.load_download(DL), layouts)
check("data.csv 写精确日期行",
      [l for l in csv.splitlines() if l and not l.startswith("#")],
      ["2026-10-26,11:00,25", "2026-10-26,11:30,25",
       "2026-10-26,12:00,25", "2026-10-26,12:30,25",
       "2026-10-26,15:00,50"])

print("== schedule.py 同时认日期行和旧 weekday 行 ==")
entries, warns = sch.parse_text(
    "2026-10-26,11:00,25\nmon,09:00,50\n2026-10-26,11:00,75\n")
check("同一天同一时刻重复 → 后一条被 warn 跳过", len(warns), 1)
check_true("warn 点名重复", "重复" in warns[0])
check("日期行覆盖同刻 weekday 规则",
      [(s["start"], s["duration"]) for s in
       sch.day_slots(entries, datetime.date(2026, 10, 26), {})],
      [("09:00", "50"), ("11:00", "25")])
check("下一周只保留 weekday 规则",
      [(s["start"], s["duration"]) for s in
       sch.day_slots(entries, datetime.date(2026, 11, 2), {})],
      [("09:00", "50")])

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
