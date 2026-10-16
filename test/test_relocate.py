#!/usr/bin/env python3
"""relocate 回归测试：临时挪动计划、推后一天一次、不能跨天。

    python3 test/test_relocate.py

不联网、不碰真浏览器、不写仓库里的任何文件：data.csv 和状态文件都在临时目录。
"""
import datetime
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from unittest import mock

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


w = load("warden", "warden.py")
from rich.console import Console  # noqa: E402

sch = w.sch
ks = w.ks
TMP = tempfile.mkdtemp(prefix="relocate-test-")
CSV = os.path.join(TMP, "data.csv")
sch.DATA_CSV = CSV
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")

TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)


class FakeBridge(object):
    def __init__(self, cancel_verified=True):
        self.calls = []
        self.cancel_verified = cancel_verified

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "cancel":
            return {"verified": self.cancel_verified}
        if cmd == "snapshot":
            return {"date": kw.get("date", TODAY.isoformat()),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return False, False, ""

    def stop(self):
        pass


def mkwarden(bridge=None):
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    wd = w.Warden(console, bridge or FakeBridge(), no_clear=True, offline=True, day_offset=1)
    return wd


def write_csv(day, hhmm="21:00", dur=25):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("# relocate test\n%s,%s,%d\n" % (sch.weekday_of_day(day), hhmm, dur))


def make_row(day, hhmm="21:00", dur=25, id_="sess-1", zone="locked"):
    h, m = [int(x) for x in hhmm.split(":")]
    start = datetime.datetime(day.year, day.month, day.day, h, m)
    end = start + datetime.timedelta(minutes=dur)
    return {
        "startMs": int(start.timestamp() * 1000),
        "endMs": int(end.timestamp() * 1000),
        "startLabel": start.strftime("%H:%M"),
        "endLabel": end.strftime("%H:%M"),
        "durationMin": dur,
        "id": id_,
        "title": "",
        "quiet": False,
        "matched": False,
        "phase": "未开始",
        "planned": True,
        "lineno": 2,
        "raw": "%s,%s,%d" % (sch.weekday_of_day(day), hhmm, dur),
        "contract": "%s,%s,%d" % (sch.weekday_of_day(day), hhmm, dur),
        "zone": zone,
    }


def clear_relocations():
    p = sch.relocations_path()
    if os.path.exists(p):
        os.remove(p)


print("== relocate 参数 ==")
check("提前 15", w.parse_relocate_offset("-15"), -15)
check("推后 15", w.parse_relocate_offset("+15"), 15)
check("不带加号也认", w.parse_relocate_offset("30"), 30)
check("spec：分开写", w.parse_relocate_spec(["1", "-15"]), ("1", -15))
check("spec：连写", w.parse_relocate_spec(["1-15"]), ("1", -15))
check("spec：只有偏移", w.parse_relocate_spec(["+15"]), (None, 15))
for bad in ("0", "7", "abc", "15m"):
    try:
        w.parse_relocate_offset(bad)
        check("非法偏移应该被拒：%s" % bad, False, True)
    except ValueError:
        check("非法偏移被拒：%s" % bad, True, True)

print("== schedule 临时覆盖：只影响这一天 ==")
clear_relocations()
write_csv(TOMORROW)
entries, warns = sch.load_entries()
check("测试 data.csv 无 warning", warns, [])
base = sch.day_slots(entries, TOMORROW)
check("原始计划是 21:00", [s["start"] for s in base], ["21:00"])
sch.add_relocation(TOMORROW, "21:00", "20:45", "25", offset=-15)
shifted = sch.day_slots(entries, TOMORROW)
check("这一天被挪到 20:45", [s["start"] for s in shifted], ["20:45"])
check("带上了原时间元数据", shifted[0]["relocated_from"], "21:00")
check("未来同星期几不受影响", [s["start"] for s in sch.day_slots(entries, TOMORROW + datetime.timedelta(days=7))], ["21:00"])
sch.remove_relocation(TOMORROW, "21:00")
check("撤销后恢复 21:00", [s["start"] for s in sch.day_slots(entries, TOMORROW)], ["21:00"])

print("== relocate 提前：取消原 API + 落临时计划 + check 订新时间 ==")
clear_relocations()
write_csv(TOMORROW)
bridge = FakeBridge()
wd = mkwarden(bridge)
wd.reload_entries()
wd.rows = [make_row(TOMORROW)]
ran = []
with mock.patch.object(w, "run_keep_session", side_effect=lambda *a, **kw: (ran.append(a), (0, "done", ""))[1]):
    check("relocate -15 执行成功", wd.cmd_relocate(["1", "-15"]), True)
check("真的取消了原 session",
      [c for c in bridge.calls if c[0] == "cancel"][0][1]["meetingId"], "sess-1")
check("立刻跑了一轮 check", ran[-1], ("check",))
recs = sch.load_relocations()["relocations"]
check("临时计划 key 是原日期+原时间", list(recs), ["%sT21:00" % TOMORROW])
check("临时目标改成 20:45", recs["%sT21:00" % TOMORROW]["to"], "20:45")
check("提前不占推后额度", sch.get_later_move(TOMORROW), None)
check("data.csv 没被改", open(CSV, encoding="utf-8").read(), "# relocate test\n%s,21:00,25\n" % sch.weekday_of_day(TOMORROW))

print("== relocate 推后：一天只能一次 ==")
clear_relocations()
write_csv(TOMORROW)
bridge = FakeBridge()
wd = mkwarden(bridge)
wd.reload_entries()
wd.rows = [make_row(TOMORROW)]
with mock.patch.object(w, "run_keep_session", return_value=(0, "done", "")):
    check("第一次 +15 成功", wd.cmd_relocate(["1", "+15"]), True)
check("占用了这一天的推后额度", bool(sch.get_later_move(TOMORROW)), True)
# 第一次成功后 rows 已刷新到 21:15；刷新出计划外 API 也不影响额度检查
wd.rows = [make_row(TOMORROW, hhmm="21:15")]
try:
    wd.cmd_relocate(["1", "+15"])
    check("第二次 +15 应该被拒", False, True)
except ValueError as e:
    check_true("第二次 +15 被拒（一天只能往后挪一次）", "一天只能往后挪一次" in str(e))

print("== relocate 不能跨天 ==")
clear_relocations()
write_csv(TOMORROW, hhmm="23:45", dur=25)
wd = mkwarden()
wd.reload_entries()
wd.rows = [make_row(TOMORROW, hhmm="23:45", dur=25)]
try:
    wd.cmd_relocate(["1", "+15"])
    check("跨天 +15 应该被拒", False, True)
except ValueError as e:
    check_true("跨天 +15 被拒", "不能跨天" in str(e))

print("== 草稿区：只落临时计划，不取消/不加急 ==")
clear_relocations()
write_csv(TOMORROW)
bridge = FakeBridge()
wd = mkwarden(bridge)
wd.reload_entries()
wd.rows = [make_row(TOMORROW, id_=None, zone="draft")]
ran = []
with mock.patch.object(w, "run_keep_session", side_effect=lambda *a, **kw: (ran.append(a), (0, "done", ""))[1]):
    check("草稿区 relocate -15 成功", wd.cmd_relocate(["1", "-15"]), True)
check("草稿区没有 API 可取消", [c for c in bridge.calls if c[0] == "cancel"], [])
check("草稿区不急着 check", ran, [])
check("临时计划仍然落盘", sch.load_relocations()["relocations"]["%sT21:00" % TOMORROW]["to"], "20:45")

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
