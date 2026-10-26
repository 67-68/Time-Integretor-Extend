#!/usr/bin/env python3
"""改时段排布时，旧 API session 必须被取消，不能 50min/25min 并存。"""
import builtins
import datetime
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="period-reconcile-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ks = w.sp, w.sch, w.ks
sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **k: None

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
data = {"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
        "profiles": ["空闲"], "events": [],
        "periods": [{"id": "T5", "date": ISO, "start": "22:15", "end": "23:15",
                     "type": "空闲", "name": "空闲", "calendar": "空闲", "title": "空闲"}]}
sp.save_download(data)
open(sch.DATA_CSV, "w", encoding="utf-8").write("")


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.last_idle_close = None

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "cancel":
            return {"verified": True}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": int(time.time() * 1000),
                    "sessions": []}
        if cmd == "release":
            return {"released": True}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden(with_old=True):
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=False)
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    h, m = 22, 15
    start = datetime.datetime.combine(TODAY, datetime.time(h, m))
    end = start + datetime.timedelta(minutes=50)
    row = {
        "startMs": int(start.timestamp() * 1000), "endMs": int(end.timestamp() * 1000),
        "startLabel": "22:15", "endLabel": "23:05", "durationMin": 50,
        "id": "old-50", "title": "", "quiet": False, "matched": True,
        "phase": "未开始", "planned": True, "lineno": 1, "raw": "",
        "contract": "%s,22:15,50" % sch.weekday_of_day(TODAY),
        "zone": "locked", "date": ISO,
    }
    wd.api_rows = [row] if with_old else []
    wd.rows = [row] if with_old else []
    wd.rebuild_rows = lambda *a, **kw: None          # 避免 refresh 把手工行覆盖掉
    wd.refresh = lambda *a, **kw: True
    return wd


print("== 执行区旧 50min：改 T5 25 25 → 先取消再写新契约 ==")
wd = mkwarden(True)
wd.require_declaration = lambda reason: True
ks._save_json(ks.STATE_FILE, {"date": ISO, "slots": {
    "%sT22:15" % ISO: {"status": "booked", "duration": "50"}}})
ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "round done", ""))[1]
check("period T5 25 25 受理", wd.cmd_period_layout(["T5", "25", "25"]), True)
check("旧 booked state 被清掉（否则 daemon 会记 sin）",
      "%sT22:15" % ISO in (ks.load_state().get("slots") or {}), False)
cancels = [c for c in wd.bridge.calls if c[0] == "cancel"]
check("先取消了旧 session", len(cancels), 1)
check("取消的是旧 session 身份", cancels[0][1]["meetingId"], "old-50")
check("取消后立刻跑了一轮 check", ran[-1][0], "check")
body = [l for l in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
        if l and not l.startswith("#")]
check("新 data.csv 只有 25+25", body,
      ["%s,22:15,25" % ISO, "%s,22:45,25" % ISO])

print("== 宣言没敲对：什么都不动 ==")
open(sch.DATA_CSV, "w", encoding="utf-8").write("ORIGINAL\n")
wd = mkwarden(True)
wd.require_declaration = lambda reason: False
before_calls = len(wd.bridge.calls)
check("拒绝后返回 False", wd.cmd_period_layout(["T5", "25", "25"]), False)
check("没有取消任何东西", [c for c in wd.bridge.calls if c[0] == "cancel"], [])
check("data.csv 完全没动", open(sch.DATA_CSV, encoding="utf-8").read(), "ORIGINAL\n")

print("== 草稿区不受宣言限制 ==")
open(sch.DATA_CSV, "w", encoding="utf-8").write("ORIGINAL\n")
wd = mkwarden(True)
wd.rows[0]["zone"] = "draft"
wd.require_declaration = lambda reason: (_ for _ in ()).throw(AssertionError("不该问宣言"))
check("草稿区直接取消+改", wd.cmd_period_layout(["T5", "25", "25"]), True)
check("草稿区也取消了旧 session",
      len([c for c in wd.bridge.calls if c[0] == "cancel"]), 1)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
