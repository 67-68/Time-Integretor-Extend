#!/usr/bin/env python3
"""统一流水线的契约：一个 commit_layout 事务 + 一个 book 预约出口。

这两条是 2026-11 重构的**核心不变量**，以前它们各散在 4~5 处手抄的实现里：

1. 改排布只有一个事务 `commit_layout()`：取消旧场 → 写 layout → 清绑定 →
   重算 data.csv，全程拿 daemon 的 round lock，并先写挂号单。
2. 预约只有一个出口 `book()`：任何改了 data.csv 的写入者末尾都调它；
   refresh 的对账轮（`_suppress_book`）例外 —— 只对账、不新造外部动作。

这里只钉这几条不变量本身；各条命令的细节仍由 test_period_reconcile /
test_schedule_pipeline / test_project 覆盖。
"""
import builtins
import datetime
import importlib.util
import inspect
import io
import os
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


TMP = tempfile.mkdtemp(prefix="unified-pipeline-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
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
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **k: None


def _no_network(*a, **kw):
    raise RuntimeError("测试里不许真连 Focusmate API")


ks.fetch_session_details = _no_network
ks.fetch_sessions = _no_network

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
T5 = {"id": "T5", "date": ISO, "start": "22:15", "end": "23:15",
      "type": "空闲", "name": "空闲", "calendar": "空闲", "title": "空闲"}

data = {"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
        "profiles": ["空闲"], "events": [], "periods": [dict(T5)]}
sp.save_download(data)
open(sch.DATA_CSV, "w", encoding="utf-8").write("")


class FakeBridge(object):
    def __init__(self):
        self.calls = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": int(time.time() * 1000),
                    "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden(with_old=True):
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=False)
    # 固定「现在」在 21:00：旧 session 是 22:15，必须仍属「未开始」。
    wd.clock_now = datetime.datetime.combine(TODAY, datetime.time(21, 0))
    wd.rows_source = "bridge"
    wd.rows_at = time.time()
    h, m = 22, 15
    start = datetime.datetime.combine(TODAY, datetime.time(h, m))
    end = start + datetime.timedelta(minutes=50)
    row = {
        "startMs": int(start.timestamp() * 1000),
        "endMs": int(end.timestamp() * 1000),
        "startLabel": "22:15", "endLabel": "23:05", "durationMin": 50,
        "id": "old-50", "title": "", "quiet": False, "matched": True,
        "phase": "未开始", "planned": True, "lineno": 1, "raw": "",
        "contract": "%s,22:15,50" % sch.weekday_of_day(TODAY),
        "zone": "locked", "date": ISO, "period_id": "T5",
    }
    wd.api_rows = [dict(row)] if with_old else []
    wd.rows = [dict(row)] if with_old else []
    wd.rebuild_rows = lambda *a, **kw: None
    wd.refresh = lambda *a, **kw: True
    return wd


def reset(booked=True):
    """清掉 layout / state / 挂号单，回到「一场 50 分钟已订」的起点。"""
    for path in (sp.layouts_path(), ks.EXPECTED_FILE, ks.VERDICTS_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
    ks._save_json(ks.STATE_FILE, {"date": ISO, "slots": (
        {"%sT22:15" % ISO: {"status": "booked", "duration": "50"}}
        if booked else {})})


# ---------------------------------------------------------------- 1) book 出口
print("== book()：唯一预约出口 ==")
reset()
ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "round done", ""))[1]
wd = mkwarden(True)
period = wd.periods_for_day(TODAY)[0]

check("commit_layout(book=False) 不触发 check",
      (wd.commit_layout(TODAY, period, [25, 25], book=False)["ok"],
       ran), (True, []))
ran[:] = []
check("commit_layout(book=True) 受理",
      wd.commit_layout(TODAY, period, [25, 25], book=True)["ok"], True)
check("commit_layout(book=True) 触发了一次 check", ran, [("check",)])

ran[:] = []
wd._suppress_book = True
try:
    wd.commit_layout(TODAY, period, [50], book=True)
finally:
    wd._suppress_book = False
check("_suppress_book 挡住预约（refresh 对账轮不新造外部动作）", ran, [])

ran[:] = []
wd._booking = True
try:
    wd.book(TODAY)
finally:
    wd._booking = False
check("book() 防重入（已经在订就不再嵌套）", ran, [])

# plan 是整天重排，末尾必须预约；自动重排（refresh）必须不预约。
check("_plan_apply 默认 book=True（plan 完成会预约）",
      inspect.signature(w.Warden._plan_apply).parameters["book"].default, True)

# ---------------------------------------------------------------- 2) 空排布语义
print("== commit_layout：[] 是「这一段不排了」，不是「没给、回落默认」 ==")
reset(booked=False)
wd = mkwarden(False)
wd.rows = []
wd.api_rows = []
check("写空排布受理", wd.commit_layout(TODAY, period, [], book=False)["ok"], True)
check("layout 落的是空序列", sp.get_layout(sp.load_layouts(), TODAY, "T5"), [])
check("data.csv 里这个时段一场都没有",
      [ln for ln in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
       if ln.strip() and not ln.startswith("#")], [])

# ---------------------------------------------------------------- 3) 挂号单 + 锁
print("== commit_layout：拿 round lock + 先写挂号单，再取消旧场 ==")
reset()
ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "round done", ""))[1]
wd = mkwarden(True)
period = wd.periods_for_day(TODAY)[0]
locks = []
_orig_lock = wd._acquire_round_lock


def _counting_lock(*a, **kw):
    locks.append(1)
    return _orig_lock(*a, **kw)


wd._acquire_round_lock = _counting_lock
wd.bridge.calls = []
result = wd.commit_layout(TODAY, period, [25, 25], book=False, announce=False)
check("拿了一次 round lock", len(locks), 1)
check("取消了旧 50 分钟那场",
      [c[1].get("meetingId") for c in wd.bridge.calls if c[0] == "cancel"],
      ["old-50"])
check("返回里带上了被取消的行",
      [r.get("id") for r in result["cancelled"]], ["old-50"])
check("旧 booked state 被清掉（否则 daemon 会记 sin）",
      "%sT22:15" % ISO in (ks.load_state().get("slots") or {}), False)
check_true("挂号单在取消前就写好了（daemon 不记罪）",
           "%sT22:15" % ISO in (ks.load_expected().get("cancels") or {}))
check("新 data.csv 是 25+25",
      [ln for ln in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
       if ln.strip() and not ln.startswith("#")],
      ["%s,22:15,25" % ISO, "%s,22:45,25" % ISO])

# ---------------------------------------------------------------- 4) 取消路径
print("== _cancel_stale_period_sessions：唯一一份取消实现 ==")
reset()
wd = mkwarden(True)
period = wd.periods_for_day(TODAY)[0]
cancelled, failed = wd._cancel_stale_period_sessions(
    TODAY, period, [{"start": "22:15", "duration": 25}])
check("旧 50 被取消", [r.get("id") for r in cancelled], ["old-50"])
check("没报失败", failed, False)

reset()
wd = mkwarden(False)
period = wd.periods_for_day(TODAY)[0]
wd.rows = []
wd.api_rows = []
cancelled, failed = wd._cancel_stale_period_sessions(
    TODAY, period, [{"start": "22:15", "duration": 25}])
check("没有旧场时什么都不做", (cancelled, failed), ([], False))

# ---------------------------------------------------------------- 5) period 层
print("== available_periods：period 是排期的第一等输入 ==")
reset(booked=False)
wd = mkwarden(False)
meta = wd.available_periods(TODAY)
check("认出这一天的时段", sorted(meta), ["T5"])
check("没有 layout = 自动排布", meta["T5"]["auto"], True)
check("没有 layout 记录", meta["T5"]["has_layout"], False)
sp.set_layout(TODAY, "T5", [25, 25], source="manual")
meta = wd.available_periods(TODAY)
check("手动 layout = 硬约束", meta["T5"]["auto"], False)
check("有 layout 记录", meta["T5"]["has_layout"], True)

# ---------------------------------------------------------------- 收尾
shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
