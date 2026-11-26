#!/usr/bin/env python3
"""refresh 不许把「真实发生过 / daemon 说在」的场次当成幽灵剪掉（2026-11-26 事故）。

现场：T1 09:30–11:15 的 layout 是 plan 切的 ``[75]``，计划 09:30，Focusmate 实际把
它落在 **09:45**（`keep_session-state.json`：``status=booked, match=shifted,
actual=实际 09:45 75min（计划 09:30）``），人上完了、11:00 还进了收尾队列。

但 10:03 TUI 里给它记了一条 ``abandoned / nostart`` 判决（「到点没开始，我选了
不去了」）。16:08 一次 refresh 的 ``_prune_dead_auto_slots`` 先发现「09:30 不在
API 快照里」（真实那场在 09:45），再拿那条过期判决当证据 → 整段 ``mode=clear``，
T1 变成「整段 105min 未切 session」，data.csv 里那行没了，daemon 之后每轮都在报
「计划外 1 场：09:45」。

判据（这次定死）：
  * **已经过去的场次一律不剪** —— 它是已经发生的事实，不是「永远订不上的幽灵」；
  * ``state.status == booked`` 或带 ``match``（被挪过时间、daemon 已认账）→ 不剪；
  * 只有「未来 + 没订上 + 有 abandoned/missed 证据」才是幽灵，才剪。
并给剪枝补了 ``plan.prune_dead`` 日志事件（以前只有一句 commit_done，看不出原因）。
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="prune-keeps-real-test-")

for key, name in (("TI_LOG_DIR", "logs"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_SESSION_MUTES", "mutes.json"),
                  ("TI_SETTINGS_FILE", "settings.json"),
                  ("TI_PLAN_PINS", "plan-pins.json"),
                  ("TI_PLAN_DECISIONS", "plan-decisions.json"),
                  ("TI_PLAN_WATCHDOG", "plan-watchdog.json"),
                  ("TI_TASKS_FILE", "tasks.json"),
                  ("TI_TASK_ARCHIVE", "archive.json"),
                  ("TI_PROJECTS_FILE", "projects.json"),
                  ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
                  ("TI_NOSTART_FILE", "nostart.json"),
                  ("TI_POSTSESSION_FILE", "postsession.json"),
                  ("TI_OMNIFOCUS_OUTBOX", "omnifocus-outbox.json"),
                  ("TI_FAILED_ACTIONS", "failed-actions.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

w.run_keep_session = lambda *a, **kw: (0, "", "")
sp, sch, ts, ks, of, pj, pp = w.sp, w.sch, w.ts, w.ks, w.of, w.pj, w.pp

sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
for _name, _file in (("STATE_FILE", "state.json"),
                     ("LOCK_FILE", "keep_session.lock"),
                     ("EXPECTED_FILE", "keep_session-expected.json"),
                     ("VERDICTS_FILE", "verdicts.json"),
                     ("SINS_FILE", "sins.json"),
                     ("LOG_FILE", "keep_session.log"),
                     ("TUI_STATE_FILE", "warden-tui.json"),
                     ("SINS_ARCHIVE", "sins.jsonl"),
                     ("IDLE_STATE_FILE", "idle.json"),
                     ("IDLE_MODE_FILE", "mode.json"),
                     ("PAUSE_FILE", "pause.json")):
    setattr(ks, _name, os.path.join(TMP, _file))
ks.notify = lambda *a, **k: None
ks.fetch_session_details = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("测试里不许真连 Focusmate API"))
ks.fetch_sessions = ks.fetch_session_details

TODAY = datetime.date.today()
DAY_ISO = TODAY.isoformat()

fails = []


def check(label, got, want):
    if got == want:
        print("ok   %s" % label)
        return
    fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": [{"startLabel": s, "durationMin": d,
                                  "id": "sess-" + s,
                                  "startMs": 0, "endMs": 0, "title": "",
                                  "quiet": None, "matched": None}
                                 for s, d in getattr(self, "sessions", ())]}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmniFocus(object):
    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        return []

    def get(self, pk, refresh=False):
        return None

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def invalidate(self):
        pass


def fresh(periods, layouts, csv_rows_, clock):
    """沙盒：给定时段 + layout + data.csv 行 + 钉死的时钟。"""
    sp.save_extras(sp.empty_extras())
    sp.save_rules(sp.empty_rules())
    lay = sp.empty_layouts()
    for pid, rec in layouts.items():
        lay.setdefault("layouts", {}).setdefault(DAY_ISO, {})[pid] = rec
    sp.save_layouts(lay)
    sp.save_download({"version": 1, "generated_at": "x", "start_date": DAY_ISO,
                      "days": 8, "profiles": ["工作"], "events": [],
                      "periods": periods})
    with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
        fh.write("# 空\n")
        for start, duration in csv_rows_:
            fh.write("%s,%s,%d\n" % (DAY_ISO, start, duration))
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])
    pj.save(pj.blank())
    with open(ks.STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"date": DAY_ISO, "slots": {}}, fh)
    with open(ks.VERDICTS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"abandoned": {}}, fh)
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus()
    wd.clock_now = datetime.datetime.combine(TODAY, clock)
    # 只把账读进来，**不跑 refresh 流水线**：那会顺手触发资源回收，
    # 把这一轮要验的 layout 先收掉（本文件测的是剪枝那一步）。
    wd.reload_periods()
    wd.reload_entries()
    wd.rebuild_rows()
    return wd


def set_state(wd, iso, slots):
    with open(ks.STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"date": iso, "slots": slots}, fh)
    wd.forget_caches()          # daemon_slots() 有 2 秒缓存：写完必须作废


def set_abandoned(wd, iso, starts):
    with open(ks.VERDICTS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"abandoned": {
            "%sT%s" % (iso, s): {"at": iso + "T10:03:58", "why": "nostart",
                                 "note": "到点没开始，我选了「不去了」"}
            for s in starts}}, fh)
    wd.forget_caches()


def inject_api(wd, rows):
    wd.api_rows = []
    for start, duration in rows:
        hour, minute = (int(x) for x in start.split(":"))
        begin = datetime.datetime.combine(TODAY, datetime.time(hour, minute))
        end = begin + datetime.timedelta(minutes=duration)
        wd.api_rows.append({
            "startMs": int(begin.timestamp() * 1000),
            "endMs": int(end.timestamp() * 1000),
            "startLabel": start, "endLabel": end.strftime("%H:%M"),
            "durationMin": duration, "id": "sess-" + start, "title": "",
            "quiet": None, "matched": True, "phase": "已结束",
        })
    wd.rows_source = "bridge"
    wd.rebuild_rows()


T1 = {"id": "T1", "date": DAY_ISO, "start": "09:30", "end": "11:15",
      "type": "工作", "name": "SAT 3H", "calendar": "工作", "title": "SAT 3H"}
T3 = {"id": "T3", "date": DAY_ISO, "start": "19:00", "end": "21:30",
      "type": "空闲", "name": "其他项目", "calendar": "空闲", "title": "其他项目"}


def t1_layout():
    return (sp.get_layout_record(sp.load_layouts(), TODAY, "T1") or {})


def t3_layout():
    return (sp.get_layout_record(sp.load_layouts(), TODAY, "T3") or {})


def csv_today():
    out = []
    for line in open(sch.DATA_CSV, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[0] == DAY_ISO:
            out.append((parts[1], int(parts[2])))
    return out


print("== 1) 现场复刻：09:30 计划场实际落在 09:45（已上完）+ 一条过期 abandoned 判决 ==")
wd = fresh([T1, T3], {"T1": {"durations": [75], "source": "plan"}},
           [("09:30", 75)], datetime.time(16, 0))
set_state(wd, DAY_ISO, {
    "2026-11-26T09:30": {"status": "booked", "duration": "75", "past": True,
                         "match": "shifted",
                         "actual": "实际 09:45 75min（计划 09:30）"},
})
set_abandoned(wd, DAY_ISO, ["09:30"])
inject_api(wd, [("09:45", 75)])          # API 里真实那场在 09:45
check("剪枝剪掉 0 段", wd._prune_dead_auto_slots(TODAY, quiet=True), 0)
check("T1 的 75 还在", t1_layout().get("durations"), [75])
check("data.csv 那行也还在", csv_today(), [("09:30", 75)])

print("\n== 2) 过去 + 没有 booked、只有 abandoned 的幽灵：也不剪（不回头改历史）==")
wd = fresh([T1, T3], {"T1": {"durations": [75], "source": "plan"}},
           [("09:30", 75)], datetime.time(16, 0))
set_abandoned(wd, DAY_ISO, ["09:30"])
inject_api(wd, [])
check("剪枝剪掉 0 段", wd._prune_dead_auto_slots(TODAY, quiet=True), 0)
check("T1 的 75 还在", t1_layout().get("durations"), [75])

print("\n== 3) 未来 + daemon 说 booked（只是还没进 API 快照）：不剪 ==")
wd = fresh([T1, T3], {"T3": {"durations": [25], "source": "plan"}},
           [("19:00", 25)], datetime.time(16, 0))
set_state(wd, DAY_ISO, {"2026-11-26T19:00": {"status": "booked", "duration": "25"}})
set_abandoned(wd, DAY_ISO, ["19:00"])
inject_api(wd, [])
check("剪枝剪掉 0 段", wd._prune_dead_auto_slots(TODAY, quiet=True), 0)
check("T3 的 25 还在", t3_layout().get("durations"), [25])

print("\n== 4) 未来 + 真死（missed）：照旧剪掉（老行为不许丢）==")
wd = fresh([T1, T3], {"T3": {"durations": [25, 50], "source": "plan"}},
           [("19:00", 25), ("19:30", 50)], datetime.time(16, 0))
set_state(wd, DAY_ISO, {"2026-11-26T19:30": {"status": "missed", "duration": "50"}})
set_abandoned(wd, DAY_ISO, ["19:30"])
inject_api(wd, [("19:00", 25)])
check("剪枝剪掉 1 段", wd._prune_dead_auto_slots(TODAY, quiet=True), 1)
check("只剩前面那场", t3_layout().get("durations"), [25])
check("data.csv 也只剩前面那场", csv_today(), [("19:00", 25)])

print("\n== 5) 剪枝要留日志证据（plan.prune_dead）==")
events = [r for r in w.rl.read_events() if r.get("event") == "prune_dead"] \
    if hasattr(w.rl, "read_events") else []
log_path = os.path.join(TMP, "logs", DAY_ISO + ".jsonl")
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if '"prune_dead"' in line]
check("日志里有 prune_dead", len(events) >= 1, True)
if events:
    check("记了原因和起点", (events[-1].get("why"), events[-1].get("starts")),
          ("status=missed", ["19:30"]))

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for item in fails:
        print(" -", item)
    sys.exit(1)
print("全绿。")
