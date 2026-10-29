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
# 改排布会取 daemon 那一轮的 round lock、并写「我取消的」挂号单 —— 别落到真目录里
ks.LOCK_FILE = os.path.join(os.path.dirname(ks.STATE_FILE), "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(os.path.dirname(ks.STATE_FILE),
                                "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")

# 兜底：本文件里任何一条**意外**走到「真·一轮」的路径都要立刻炸，而不是
# 去连 api.focusmate.com（真下过单的东西，测试绝不该碰）。日志也一样要关进
# 临时目录 —— 以前漏了，用例的日志混进真 temp/keep_session.log 里，看日志
# 排障时会以为 daemon 半夜自己干了什么。
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")


def _no_network(*a, **kw):
    raise RuntimeError("测试里不许真连 Focusmate API（这一轮本该被替身拦住）")


ks.fetch_session_details = _no_network
ks.fetch_sessions = _no_network

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

print("== 改排布不要宣言（执行区也一样）==")
open(sch.DATA_CSV, "w", encoding="utf-8").write("ORIGINAL\n")
wd = mkwarden(True)
# 谁问宣言谁就炸：改排布这条路上不该出现宣言闸门
wd.require_declaration = lambda reason: (_ for _ in ()).throw(
    AssertionError("改排布不该问宣言，问的是 %r" % reason))
check("执行区改排布：不敲宣言直接生效", wd.cmd_period_layout(["T5", "25", "25"]), True)
check("照样先取消了旧 session",
      len([c for c in wd.bridge.calls if c[0] == "cancel"]), 1)
check("data.csv 换成了新排布",
      [l for l in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
       if l and not l.startswith("#")],
      ["%s,22:15,25" % ISO, "%s,22:45,25" % ISO])

print("== 草稿区不受宣言限制 ==")
open(sch.DATA_CSV, "w", encoding="utf-8").write("ORIGINAL\n")
wd = mkwarden(True)
wd.rows[0]["zone"] = "draft"
wd.require_declaration = lambda reason: (_ for _ in ()).throw(AssertionError("不该问宣言"))
check("草稿区直接取消+改", wd.cmd_period_layout(["T5", "25", "25"]), True)
check("草稿区也取消了旧 session",
      len([c for c in wd.bridge.calls if c[0] == "cancel"]), 1)

print("== 改排布前给 daemon 挂号：别把 warden 自己干的活记成罪 ==")
open(sch.DATA_CSV, "w", encoding="utf-8").write("")
wd = mkwarden(True)
w.run_keep_session = lambda *a, **kw: (0, "round done", "")
wd.require_declaration = lambda reason: (_ for _ in ()).throw(
    AssertionError("改排布不该问宣言"))
# 旧 state：22:15 是 booked（旧 50min）；新排布 25+25 里 22:15 变成 25min
ks._save_json(ks.STATE_FILE, {"date": ISO, "slots": {
    "%sT22:15" % ISO: {"status": "booked", "duration": "50"}}})
check("T5 25 25 受理", wd.cmd_period_layout(["T5", "25", "25"]), True)
expected = ks.load_expected()["cancels"]
check_true("22:15 挂了号（旧 50min 那场要被换掉）", "%sT22:15" % ISO in expected)
check("挂号单写了原因",
      str(expected["%sT22:15" % ISO].get("reason")).startswith("layout-changed:T5"), True)
# 模拟 daemon 看到 state=booked 但 API 里没了：有挂号 → 不该记罪
check("daemon 取挂号成功", ks.take_expected("%sT22:15" % ISO) is not None, True)
check("取完就没了（一码一用）", "%sT22:15" % ISO in ks.load_expected()["cancels"], False)

print("== 重新排布会撤销「永不重订」判决（不然那几场永远补不回来）==")
ks.mark_abandoned("%sT22:15" % ISO, why="walk-of-shame", note="误判")
check("先判成 abandoned", ks.is_abandoned("%sT22:15" % ISO), True)
wd = mkwarden(True)
w.run_keep_session = lambda *a, **kw: (0, "round done", "")
check("再排一次 T5 25 25", wd.cmd_period_layout(["T5", "25", "25"]), True)
check("判决被撤销（daemon 会重新订）", ks.is_abandoned("%sT22:15" % ISO), False)
check_true("消息里说了撤销判决",
           any("永不重订" in m[2] for m in wd.messages))

print("== 改排布：没动的场次保留任务绑定，动过的清掉 ==")
# 再加一个长时段 T6（11:00–13:00），好摆 50 25 25 / 50 50
data["periods"].append({"id": "T6", "date": ISO, "start": "11:00", "end": "13:00",
                        "type": "工作", "name": "SAT", "calendar": "工作", "title": "SAT"})
sp.save_download(data)
sp.set_layout(ISO, "T6", [50, 25, 25])
wd = mkwarden(False)
w.run_keep_session = lambda *a, **kw: (0, "round done", "")
wd.require_declaration = lambda reason: (_ for _ in ()).throw(
    AssertionError("改排布不该问宣言"))
print("   旧排布：", [("%s(%d)" % (x["start"], x["duration"]))
                     for x in sp.sessions_for_period(
                         [p for p in wd.periods_for_day(TODAY) if p["id"] == "T6"][0],
                         wd.layouts)])
t1 = wd.task_store.create({"title": "第一场 50min"})
t2 = wd.task_store.create({"title": "第二场 25min"})
t3 = wd.task_store.create({"title": "第三场 25min"})
for task, start, dur in ((t1, "11:00", 50), (t2, "12:00", 25), (t3, "12:30", 25)):
    wd.task_store.assign(task["id"], {"index": 1, "date": ISO, "start": start,
                                      "duration": dur, "focusmate_session_id": "s-" + start})
    wd.task_store.attach_focusmate(task["id"], {"task_id": "fmt-" + start},
                                   session={"date": ISO, "start": start, "duration": dur})

check("T6 50 50 受理", wd.cmd_period_layout(["T6", "50", "50"]), True)
check("11:00 那场没动 → 任务绑定留着",
      wd.task_store.get(t1["id"])["session"]["start"], "11:00")
check("12:00 那场被拆掉了 → 绑定清掉", wd.task_store.get(t2["id"])["session"], None)
check("12:30 那场也没了 → 绑定清掉", wd.task_store.get(t3["id"])["session"], None)
check("任务本身还在（只是不挂了）",
      [t["id"] for t in wd.task_store.list()], [t1["id"], t2["id"], t3["id"]])
prev = wd.task_store.get(t2["id"])["focusmate"]
check("旧 Focusmate task 记进 previous（完成时间还能归档）",
      [x["task_id"] for x in prev.get("previous") or []], ["fmt-12:00"])
check("previous 里记着为什么清", prev["previous"][0]["reason"], "layout-changed")
check_true("消息里说了清掉哪条绑定",
           any("清掉任务" in m[2] and wd.task_store.focusmate_title(t2, 20) in m[2]
               for m in wd.messages))

print("== 改排布：连保留的那场也动了 → 全清 ==")
wd.messages = []
check("T6 25 50 受理", wd.cmd_period_layout(["T6", "25", "50"]), True)
check("11:00 那场从 50min 变 25min → 绑定也清掉",
      wd.task_store.get(t1["id"])["session"], None)
check("这一时段里没有任务还挂着",
      [t["id"] for t in wd.task_store.list() if t.get("session")], [])

print("== 改排布：时长一样、起点也一样 → 一个都不清 ==")
wd.task_store.assign(t1["id"], {"index": 1, "date": ISO, "start": "11:00",
                                "duration": 25, "focusmate_session_id": "s-11:00"})
wd.messages = []
check("T6 25 50 再来一次（排布没变）", wd.cmd_period_layout(["T6", "25", "50"]), True)
check("没动过的场次绑定还在",
      wd.task_store.get(t1["id"])["session"]["start"], "11:00")
check_true("没有清绑定的消息",
           not any("清掉任务" in m[2] for m in wd.messages))

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
