#!/usr/bin/env python3
"""日历下载中间层：只收 工作/空闲 profile，并生成按天编号的 T1/T2 时段。"""
import datetime
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import session_plan as sp

spec = importlib.util.spec_from_file_location(
    "download_calendar", os.path.join(ROOT, "download_calendar.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


TZ = dc.LOCAL_TZ
D = datetime.date(2026, 10, 26)


def T(h, m=0):
    return datetime.datetime(D.year, D.month, D.day, h, m, tzinfo=TZ)


events = [
    dc.CalEvent(T(11), T(13), "工作", "SAT 准备"),
    dc.CalEvent(T(15), T(16), "工作", "托福 准备"),
    dc.CalEvent(T(19), T(21), "空闲", "自由时间"),
    dc.CalEvent(T(9), T(10), "上课", "数学"),
    dc.CalEvent(T(12), T(13), "休息", "午睡"),
]

print("== 只留 工作 + 空闲 ==")
selected, mode, used = dc.select_profile_events(events, ["工作", "空闲"],
                                                ["上课", "休息"], [])
check("模式是 profiles", mode, "profiles")
check("命中两个 profile", used, ["工作", "空闲"])
check("只剩工作/空闲事件",
      sorted(e.title for e in selected), ["SAT 准备", "托福 准备", "自由时间"])

print("== 下载层：按时段编号 ==")
data = sp.build_periods(selected, D, 1, profiles=["工作", "空闲"])
check("三个时段", [(p["id"], p["start"], p["end"], p["type"], p["name"])
                   for p in data["periods"]],
      [("T1", "11:00", "13:00", "工作", "SAT 准备"),
       ("T2", "15:00", "16:00", "工作", "托福 准备"),
       ("T3", "19:00", "21:00", "空闲", "自由时间")])
check("原始事件也保留", len(data["events"]), 3)

print("== 没有目标 profile 时退回旧过滤（不静默清空）==")
legacy = [dc.CalEvent(T(9), T(10), "自习", "数学")]
selected2, mode2, used2 = dc.select_profile_events(
    legacy, ["工作", "空闲"], ["上课", "休息"], [])
check("退回 legacy", mode2, "legacy")
check("没有 target profile 被命中", used2, [])
check("旧过滤仍保留自习", [e.title for e in selected2], ["数学"])

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
