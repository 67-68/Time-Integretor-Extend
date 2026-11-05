#!/usr/bin/env python3
"""「到点没开始」那一问 + `relocate T1 ±MM`（整段挪动）的回归测试。

    python3 test/test_nostart.py

覆盖三件事：

1. **判定**（``postsession.nostart_sessions``）：计划 14:00 那场，14:05 之前不问；
   14:05 之后 API 里还是没这一场 → 问；已经问过的不再问。
2. **三个选项**（``postsession.NostartReview``）：``p 15`` 推迟（走 relocate 底层）、
   ``x`` 不去了（记缺失 + 任务交给收尾）、``s`` 先不管；推迟窗口（+5 分钟）过了
   再推迟 → **取消推迟、算缺失**。
3. **整段挪动**：``relocate T1 15`` 把时段里每一场一起推后，**任务绑定跟着走**；
   和别的场次重叠就拒绝。

不联网、不碰真浏览器、不写仓库里的任何文件。
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


TMP = tempfile.mkdtemp(prefix="nostart-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_FAILED_ACTIONS"] = os.path.join(TMP, "failed-actions.json")

w = load("warden", "warden.py")
from rich.console import Console  # noqa: E402

sp, sch, ks, psv, ts = w.sp, w.sch, w.ks, w.psv, w.ts
CSV = os.path.join(TMP, "data.csv")
sch.DATA_CSV = CSV
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")
ks.notify = lambda *a, **k: None
ks.debounce = lambda: None
ks.verify_slot_booked = lambda start, **kw: (True, "")
BOOKED = []
ks.fm.book_once = lambda start_iso, duration: (
    BOOKED.append((start_iso, str(duration))),
    {"success": True, "session": {"id": "stub"}})[1]
LOGS = []
ks.log = lambda msg, echo=True, to_file=True: LOGS.append(str(msg))


def _no_network(*a, **kw):
    raise RuntimeError("测试里不许真连 Focusmate API（这一轮本该被替身拦住）")


ks.fetch_session_details = _no_network
ks.fetch_sessions = _no_network

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
# 「现在」钉在当天 14:00：用例全是「到点前/后几分钟」这种相对时间，跟着墙上时钟走
# 就会在午夜前 8 分钟算出「昨天 23:52」这种落不到今天的时刻（真踩过）。
NOW = datetime.datetime.combine(TODAY, datetime.time(14, 0))


class _FrozenDatetime(datetime.datetime):
    """``datetime.datetime`` 的替身：``.now()`` 永远返回 NOW。

    daemon（`keep_session`）和 warden 都自己取 `now()`，不钉住的话「到点后 5 分钟」
    这种用例会跟着墙上时钟漂：午夜前跑一遍是 23:52 那场，午夜后同一行代码算出来的
    就成了「昨天 23:52」。
    """

    @classmethod
    def now(cls, tz=None):
        return NOW


_frozen = mock.patch.object(w.datetime, "datetime", _FrozenDatetime)
_frozen.start()



class FakeBridge(object):
    def __init__(self, verified=True):
        self.calls = []
        self.verified = verified

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "cancel":
            return {"verified": self.verified}
        if cmd == "snapshot":
            return {"date": kw.get("date", ISO),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return False, False, ""

    def stop(self):
        pass


def mkwarden(day_offset=0, bridge=None):
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return w.Warden(console, bridge or FakeBridge(), no_clear=True, offline=True,
                    day_offset=day_offset)


def write_csv(lines):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("# nostart test\n" + "\n".join(lines) + "\n")


def hhmm_of(dt):
    return dt.strftime("%H:%M")


def write_periods(day=None, periods=None):
    """写一份下载层：默认只有 T1 11:00–13:00。"""
    day = day or TODAY
    data = {"version": 1, "generated_at": "x", "start_date": day.isoformat(),
            "days": 8, "profiles": ["工作"], "events": [],
            "periods": periods if periods is not None else [
                {"id": "T1", "date": day.isoformat(), "start": "11:00", "end": "13:00",
                 "type": "工作", "name": "SAT 准备", "calendar": "工作",
                 "title": "SAT 准备"}]}
    sp.save_download(data)


def reset_all():
    for path in (sch.relocations_path(), psv.nostart_path(), psv.state_path(),
                 ks.VERDICTS_FILE, ks.SINS_FILE, ks.STATE_FILE):
        if os.path.exists(path):
            os.remove(path)
    reset_tasks()


def reset_tasks():
    with open(os.environ["TI_TASKS_FILE"], "w", encoding="utf-8") as f:
        f.write('{"version": 1, "tasks": []}')
    with open(os.environ["TI_TASK_ARCHIVE"], "w", encoding="utf-8") as f:
        f.write('{"version": 1, "records": []}')


reset_tasks()

# ================================================================ 1. 判定窗口

print("== 判定窗口：14:00 那场，14:05 之前不问，14:06 才问 ==")
D = datetime.date(2026, 11, 3)
e = psv.nostart_entry(D, "14:00", 50, now=datetime.datetime(2026, 11, 3, 14, 0))
check("预约窗口 14:05", e["grace_end"], "2026-11-03T14:05:00")
check("推迟窗口 14:10", e["deadline"], "2026-11-03T14:10:00")
check("14:00 时还没 late", e["late"], False)
prompt = psv.NostartReview(e, now=datetime.datetime(2026, 11, 3, 14, 5, 30))
check("14:05:30 不 late", prompt.late(), False)
prompt = psv.NostartReview(e, now=datetime.datetime(2026, 11, 3, 14, 10, 1))
check("14:10 之后 late（推迟作废）", prompt.late(), True)

print("== 推迟参数：p 15 / p T1 15 ==")
check("p 15", psv.parse_postpone_spec("15"), (None, 15))
check("p +30", psv.parse_postpone_spec("+30"), (None, 30))
check("p T1 15", psv.parse_postpone_spec("T1 15"), ("T1", 15))
check("p t1 -15", psv.parse_postpone_spec("t1 -15"), ("T1", -15))
for bad in ("0", "7", "x 15", "T1"):
    try:
        psv.parse_postpone_spec(bad)
        check("非法推迟应该被拒：%s" % bad, False, True)
    except ValueError:
        check("非法推迟被拒：%s" % bad, True, True)

print("== nostart_sessions：计划有、API 没有才问 ==")
entries, warns = sch.load_entries()
check("契约无 warning", warns, [])


def entries_with(start_hhmm, duration=50, day=None):
    day = day or TODAY
    return [{"date": day.isoformat(), "weekday": None, "start": start_hhmm,
             "hour": int(start_hhmm.split(":")[0]), "minute": int(start_hhmm.split(":")[1]),
             "duration": str(duration), "raw": "x", "lineno": 2}]


# 计划 14:00、现在 14:06、API 里只有别的场次 → 该问
PLAN = entries_with("14:00")
at = datetime.datetime.combine(TODAY, datetime.time(14, 6))
other = [{"start": datetime.datetime.combine(TODAY, datetime.time(15, 0)), "duration": 50}]
found = psv.nostart_sessions(PLAN, now=at, actual=other)
check("14:06 发现没开始", [(r["start"], r["date"]) for r in found], [("14:00", ISO)])
check("入队记录带 deadline", found[0]["deadline"][-8:], "14:10:00")
# 14:04 还在预约窗口里 → 不问
at2 = datetime.datetime.combine(TODAY, datetime.time(14, 4))
check("14:04 不问（预约窗口内）", psv.nostart_sessions(PLAN, now=at2, actual=other), [])
# API 里确实有这一场 → 不问（订上了只是没进场，那是另一件事）
booked = [{"start": datetime.datetime.combine(TODAY, datetime.time(14, 0)), "duration": 50}]
check("API 里有这场就不问", psv.nostart_sessions(PLAN, now=at, actual=booked), [])
# 拿不到 API 数据 → 一句都不问（不能诬告）
check("actual=None 时闭嘴", psv.nostart_sessions(PLAN, now=at, actual=None), [])
# 已经问过的不再问
check("answered 里有就不再问",
      psv.nostart_sessions(PLAN, now=at, actual=other,
                           answered=[psv.session_key(TODAY, "14:00")]), [])
# 太老的不翻旧账（3 小时前）
at3 = datetime.datetime.combine(TODAY, datetime.time(18, 0))
check("3 小时前的不翻旧账", psv.nostart_sessions(PLAN, now=at3, actual=other), [])

# ================================================================ 2. 三个选项

print("== NostartReview：x 不去了 / p 推迟 / s 先不管 ==")
reset_all()
calls = []
hooks = {
    "say": lambda text, kind="info": calls.append(("say", text)),
    "postpone": lambda entry, period, minutes: (calls.append(("postpone", period, minutes)),
                                                {"target": "14:30"})[1],
    "mark_missed": lambda entry, note=None: (calls.append(("missed", note)),
                                             "到点没开始")[1],
    "open_review": lambda entry: calls.append(("review", entry.get("key"))),
}
at = datetime.datetime.combine(TODAY, datetime.time(14, 6))
entry = psv.nostart_entry(TODAY, "14:00", 50, now=at)
review = psv.NostartReview(entry, hooks=hooks, now=at)
check("x → missed", review.handle("x")["done"], "missed")
check("记进状态文件", psv.nostart_answers()[entry["key"]]["action"], "missed")
check("开了收尾（任务交给 A-E）", ("review", entry["key"]) in calls, True)
check("问了「记缺失」", any(c[0] == "missed" for c in calls), True)

reset_all()
calls = []
entry = psv.nostart_entry(TODAY, "14:00", 50, now=at)
review = psv.NostartReview(entry, hooks=hooks, now=at)
out = review.handle("p 15")
check("p 15 → postponed", out["done"], "postponed")
check("推迟只针对这一场", calls[0], ("postpone", None, 15))
check("目标时间记下来了", psv.nostart_answers()[entry["key"]]["target"], "14:30")

reset_all()
calls = []
review = psv.NostartReview(psv.nostart_entry(TODAY, "14:00", 50, now=at), hooks=hooks, now=at)
review.handle("p T1 30")
check("p T1 30 → 整段", calls[0], ("postpone", "T1", 30))

print("== 推迟窗口过了：取消推迟、直接算缺失 ==")
reset_all()
calls = []
late_now = datetime.datetime.combine(TODAY, datetime.time(14, 20))
review = psv.NostartReview(psv.nostart_entry(TODAY, "14:00", 50, now=late_now),
                           hooks=hooks, now=late_now)
out = review.handle("p 15")
check("late 的 p → missed", out["done"], "missed")
check("推迟没被执行", [c for c in calls if c[0] == "postpone"], [])
check("缺失原因记成 late-postpone",
      psv.nostart_answers()[psv.session_key(TODAY, "14:00")]["note"], "late-postpone")
check("late 时提示只剩两种", "只能 x 不去了" in review.stage_hint(), True)

reset_all()
calls = []
review = psv.NostartReview(psv.nostart_entry(TODAY, "14:00", 50, now=at), hooks=hooks, now=at)
check("s → deferred", review.handle("s")["done"], "deferred")
check("deferred 记进状态",
      psv.nostart_answers()[psv.session_key(TODAY, "14:00")]["action"], "deferred")

reset_all()
calls = []
review = psv.NostartReview(psv.nostart_entry(TODAY, "14:00", 50, now=at), hooks=hooks, now=at)
check("认不出的输入不动作", review.handle("跑")["action"], "handled")
check("还没关", review.closed, False)
check("relocate 之类照常派发", review.handle("relocate T1 15")["action"], "dispatch")

# ================================================================ 3. daemon

print("== daemon：刚到点（窗口内）不写 missed、不记罪 ==")
reset_all()
START = (NOW - datetime.timedelta(minutes=8)).replace(second=0, microsecond=0)
HHMM = hhmm_of(START)
write_csv(["%s,%s,50" % (ISO, HHMM)])
# API 里什么都没有（快照是空的）
# 快照里给一场**离得远**的 session：nostart_tick 需要「有真数据」才判，
# 但这一场不能落在计划那一场的匹配窗口里（写死 23:45 会随用例跑的钟点撞上 3 小时
# 前那条判据）。固定「5 小时前」= 永远不会被当成这一场的替代场次。
ks.fresh_snapshot_details = lambda now=None: [
    {"start": (NOW - datetime.timedelta(hours=5)).replace(second=0, microsecond=0),
     "duration": 25}]
SUMMONED = []
ks.summon_warden = lambda **kw: (SUMMONED.append(kw), True)[1]
fired = ks.nostart_tick(now=NOW)
check("nostart_tick 入队 1 场", [r["start"] for r in fired], [HHMM])
check("叫了 warden", len(SUMMONED), 1)
check("状态文件里是 queued",
      psv.nostart_answers()[psv.session_key(TODAY, HHMM)]["action"], "queued")
check("同一场不重复入队", ks.nostart_tick(now=NOW), [])

print("== daemon 一轮：窗口内 → nostart（不记罪），过窗口 → missed ==")
reset_all()
ks.save_state({"date": ISO, "slots": {sch.key_of(TODAY, HHMM): {
    "status": "booked", "duration": "50", "attempts": 0}}})
API = []
ks.fetch_session_details = lambda now, until: [
    {"start": x, "duration": None, "id": None} if isinstance(x, datetime.datetime) else x
    for x in API]
ks.fetch_sessions = lambda now, until: [r["start"] for r in ks.fetch_session_details(now, until)]
ks.run_once()
rec = ks.load_state()["slots"].get(sch.key_of(TODAY, HHMM)) or {}
check("刚刚过点 → status=nostart", rec.get("status"), "nostart")
check("没记罪（留给 warden 问）", ks.load_sins()["sins"], [])

reset_all()
OLD = (NOW - datetime.timedelta(minutes=40)).replace(second=0, microsecond=0)
write_csv(["%s,%s,50" % (ISO, hhmm_of(OLD))])
ks.save_state({"date": ISO, "slots": {sch.key_of(TODAY, hhmm_of(OLD)): {
    "status": "booked", "duration": "50", "attempts": 0}}})
ks.run_once()
rec = ks.load_state()["slots"].get(sch.key_of(TODAY, hhmm_of(OLD))) or {}
check("过了窗口才 missed", rec.get("status"), "missed")
check("这时候才记罪", len(ks.load_sins()["sins"]), 1)

# ================================================================ 4. 整段 relocate

print("== relocate T1 +15：整段一起推后，任务绑定跟着走 ==")
reset_all()
TOMORROW = TODAY + datetime.timedelta(days=1)
TOM = TOMORROW.isoformat()
write_periods(TOMORROW)
write_csv(["%s,11:00,50" % TOM, "%s,12:00,50" % TOM])
bridge = FakeBridge()
wd = mkwarden(day_offset=1, bridge=bridge)
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
check("T1 有两行", [r["startLabel"] for r in wd.rows if r.get("period_id") == "T1"],
      ["11:00", "12:00"])

store = ts.TaskStore()
t1 = store.create({"title": "第一场"})
t2 = store.create({"title": "第二场"})
store.assign(t1["id"], {"date": TOM, "start": "11:00", "duration": 50})
store.assign(t2["id"], {"date": TOM, "start": "12:00", "duration": 50})
check("12:00 那场挂着第二场", [t["id"] for t in store.tasks_for_session(TOMORROW, "12:00")],
      [t2["id"]])

ran = []
with mock.patch.object(w, "run_keep_session",
                       side_effect=lambda *a, **kw: (ran.append(a), (0, "done", ""))[1]):
    check("relocate T1 +15 受理", wd.handle("relocate T1 +15"), True)
relocations = sch.load_relocations()["relocations"]
check("两条临时挪动", sorted(r["to"] for r in relocations.values()), ["11:15", "12:15"])
check("data.csv 没改",
      [l for l in open(CSV, encoding="utf-8").read().splitlines()
       if l and not l.startswith("#")],
      ["%s,11:00,50" % TOM, "%s,12:00,50" % TOM])
check("整段挪动占的是「每时段一次」的额度",
      bool(sch.get_later_move(TOMORROW, scope="T1")), True)
check("没吃掉整天的额度", sch.get_later_move(TOMORROW), None)
check("任务跟着挪到 11:15", [t["id"] for t in store.tasks_for_session(TOMORROW, "11:15")],
      [t1["id"]])
check("任务跟着挪到 12:15", [t["id"] for t in store.tasks_for_session(TOMORROW, "12:15")],
      [t2["id"]])
check("旧时间没有任务了", store.tasks_for_session(TOMORROW, "12:00"), [])
check("截止时间跟着走（12:15 + 50 分钟）", store.get(t2["id"])["fields"]["due"],
      "%sT13:05:00" % TOM)
# 明天在「edit-tomorrow」开着时算**草稿区**，草稿区不急着叫 daemon 下单
# （到点前那一轮自己会订）；今天/执行区的挪动才会立刻跑 check —— 下面 p 30 那条覆盖它。
check("草稿区不急着跑 check", ran, [])

print("== relocate T1 再来一次推后：每时段一天一次 ==")
# `handle` 会把 ValueError 翻成一句警告（guard），所以这里直接问底层
try:
    wd._relocate_period(TOMORROW, "T1", 15)
    check("第二次 +15 应该被拒", False, True)
except ValueError as exc:
    check_true("第二次 +15 被拒（每时段一天一次）", "每个时段一天只能往后挪一次" in str(exc))

print("== 撞车检查：和别的场次重叠就拒绝 ==")
reset_all()
# T1（11:00–13:00）挪 +15 → 最后一场 12:15–13:05，正好压住 13:00 那场（T2）
write_periods(TOMORROW, periods=[
    {"id": "T1", "date": TOM, "start": "11:00", "end": "13:00", "type": "工作",
     "name": "SAT 准备", "calendar": "工作", "title": "SAT 准备"},
    {"id": "T2", "date": TOM, "start": "13:00", "end": "14:00", "type": "工作",
     "name": "托福 准备", "calendar": "工作", "title": "托福 准备"}])
write_csv(["%s,11:00,50" % TOM, "%s,12:00,50" % TOM, "%s,13:00,25" % TOM])
wd = mkwarden(day_offset=1, bridge=FakeBridge())
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
try:
    wd._relocate_period(TOMORROW, "T1", 15)
    check("重叠应该被拒", False, True)
except ValueError as exc:
    check_true("重叠被拒（T1 推到 12:15–13:05 压住 13:00 那场）", "重叠" in str(exc))

print("== 提前整段（-15）不占额度 ==")
reset_all()
write_csv(["%s,11:00,50" % TOM, "%s,12:00,50" % TOM])
wd = mkwarden(day_offset=1, bridge=FakeBridge())
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
with mock.patch.object(w, "run_keep_session", return_value=(0, "done", "")):
    wd._relocate_period(TOMORROW, "T1", -15)
check("提前到 10:45 / 11:45",
      sorted(r["to"] for r in sch.load_relocations()["relocations"].values()),
      ["10:45", "11:45"])
check("提前不占推后额度", sch.get_later_move(TOMORROW, scope="T1"), None)

# ================================================================ 5. warden 里那一问

FIXED = NOW

print("== warden：表格里「计划有、API 没有」→ 弹那一问 ==")
reset_all()
START = (FIXED - datetime.timedelta(minutes=10)).replace(second=0, microsecond=0)
HHMM = hhmm_of(START)
write_csv(["%s,%s,50" % (ISO, HHMM)])
wd = mkwarden(day_offset=0, bridge=FakeBridge())
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
task = ts.TaskStore().create({"title": "到点没开始的那场"})
ts.TaskStore().assign(task["id"], {"date": ISO, "start": HHMM, "duration": 50})
wd._maybe_start_nostart()
check_true("弹出了那一问", wd.nostart is not None)
check("问的是这一场", wd.nostart.entry["start"], HHMM)
panel = wd.nostart_panel()
check_true("面板画得出来", panel is not None)

print("== x 不去了：算缺失 + 任务进收尾 ==")
check("x 被受理", wd.handle("x"), True)
check("那一问收掉了", wd.nostart, None)
answers = psv.nostart_answers()
check("记成 missed", answers[psv.session_key(TODAY, HHMM)]["action"], "missed")
check("daemon 那边也记 missed",
      (ks.load_state()["slots"].get(sch.key_of(TODAY, HHMM)) or {}).get("status"), "missed")
check("判了「已放弃」（不再重订）",
      sch.key_of(TODAY, HHMM) in (ks.load_verdicts()["abandoned"]), True)
check_true("任务进了收尾", wd.review is not None)
check("收尾里就是这条任务", [t["id"] for t in wd.review.tasks], [task["id"]])
check("这一场没记 count（一分钟都没干）", wd.review.counts, {})

print("== p 30：推迟这一场（走 relocate 底层，任务跟着走）==")
reset_all()
# 6 分钟前那场：deadline = start + 10 分钟，现在还在推迟窗口里
START = (FIXED - datetime.timedelta(minutes=6)).replace(second=0, microsecond=0)
HHMM = hhmm_of(START)
write_csv(["%s,%s,50" % (ISO, HHMM)])
wd = mkwarden(day_offset=0, bridge=FakeBridge())
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
task = ts.TaskStore().create({"title": "要推迟的那场"})
ts.TaskStore().assign(task["id"], {"date": ISO, "start": HHMM, "duration": 50})
wd._maybe_start_nostart()
check_true("弹出了那一问", wd.nostart is not None)
with mock.patch.object(w, "run_keep_session", return_value=(0, "done", "")):
    check("p 30 被受理", wd.handle("p 30"), True)
target = (START + datetime.timedelta(minutes=30)).strftime("%H:%M")
relocations = sch.load_relocations()["relocations"]
check("临时挪动落盘", [r["to"] for r in relocations.values()], [target])
check("记成 postponed",
      psv.nostart_answers()[psv.session_key(TODAY, HHMM)]["action"], "postponed")
check("任务跟着挪过去",
      [t["id"] for t in ts.TaskStore().tasks_for_session(TODAY, target)], [task["id"]])
check("旧时间没任务了", ts.TaskStore().tasks_for_session(TODAY, HHMM), [])

print("== 答过的不再问 ==")
wd2 = mkwarden(day_offset=0, bridge=FakeBridge())
wd2.reload_entries()
wd2.api_rows = []
wd2.rows_source = "bridge"
wd2.rebuild_rows()
wd2._maybe_start_nostart()
check("答过就不再弹", wd2.nostart, None)

_frozen.stop()
shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
