#!/usr/bin/env python3
"""current_session（-a current / -ac）的取数与新鲜度回归。

2026-11-21 用户确认：表格过期 / bridge 失败时，-ac 要**当场同步拉一次**，
拉完仍然没有才报错；报错必须说清「行数 / 来源 / 上次成功时间 / bridge 错误」，
并在 JSONL 里留下判定依据（session/current_missing）。

不联网：refresh 用桩替换（只改内存里的 api_rows），Focusmate 一律不碰。
"""
import datetime
import importlib.util
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="session-current-test-")

for key, name in (("TI_LOG_DIR", "logs"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_SESSION_MUTES", "mutes.json"),
                  ("TI_SETTINGS_FILE", "settings.json"),
                  ("TI_TASKS_FILE", "tasks.json"),
                  ("TI_TASK_ARCHIVE", "archive.json"),
                  ("TI_PROJECTS_FILE", "projects.json"),
                  ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
                  ("TI_NOSTART_FILE", "nostart.json"),
                  ("TI_POSTSESSION_FILE", "postsession.json"),
                  ("TI_PLAN_PINS", "plan-pins.json"),
                  ("TI_PLAN_WATCHDOG", "plan-watchdog.json"),
                  ("TI_OMNIFOCUS_OUTBOX", "omnifocus-outbox.json"),
                  ("TI_FAILED_ACTIONS", "failed-actions.json")):
    os.environ[key] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

w.run_keep_session = lambda *a, **kw: (0, "", "")
sp, sch, ts, ks, pj = w.sp, w.sch, w.ts, w.ks, w.pj

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
fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


class FakeBridge(object):
    def call(self, cmd, timeout=240, **kw):
        return {"verified": True, "session": None}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def make_row(start_dt, minutes, sid=None, phase="进行中"):
    end_dt = start_dt + datetime.timedelta(minutes=int(minutes))
    return {
        "startMs": int(start_dt.timestamp() * 1000),
        "endMs": int(end_dt.timestamp() * 1000),
        "startLabel": start_dt.strftime("%H:%M"),
        "endLabel": end_dt.strftime("%H:%M"),
        "durationMin": int(minutes), "id": sid, "title": "",
        "quiet": False, "matched": False, "phase": phase,
        "date": start_dt.date().isoformat(),
    }


def fresh(offline=False):
    for task in ts.TaskStore().list():
        ts.TaskStore().delete(task["id"])
    pj.save(pj.blank())
    sp.save_download({"version": 1, "generated_at": "x",
                      "start_date": TODAY.isoformat(), "days": 8,
                      "profiles": ["工作"], "events": [], "periods": []})
    sp.save_layouts(sp.empty_layouts())
    sp.save_extras(sp.empty_extras())
    try:
        with open(sch.DATA_CSV, "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError:
        pass
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=offline)
    wd._suppress_book = True
    wd.reload_periods()
    wd.reload_entries()
    wd.api_rows = []
    wd.rows_source = ""
    wd.rows_at = 0
    wd.rows_error = ""
    wd.rebuild_rows()
    return wd


def set_rows(wd, rows, source="bridge", age_seconds=0.0, error=""):
    wd.api_rows = list(rows)
    wd.rows_source = source
    wd.rows_at = time.time() - float(age_seconds)
    wd.rows_error = error
    wd.rebuild_rows()


def live_start(minutes_ago=10):
    now = datetime.datetime.now()
    start = now - datetime.timedelta(minutes=minutes_ago)
    start = start.replace(second=0, microsecond=0,
                          minute=(start.minute // 15) * 15)
    return start


def stub_refresh(wd, calls, new_rows=None, error_after=""):
    """refresh 桩：记录调用；给了 new_rows 就模拟拉到了新数据。"""
    def _refresh(quiet=False, timeout=180, wait=False):
        calls.append({"quiet": quiet, "wait": wait})
        if new_rows is not None:
            set_rows(wd, new_rows, source="bridge", age_seconds=0)
        if error_after:
            wd.rows_error = error_after
        return True
    wd.refresh = _refresh
    return calls


print("== 1) 表格里就有正在进行的场次：直接返回，不重拉 ==")
wd = fresh()
set_rows(wd, [make_row(live_start(), 50, "live-1")], age_seconds=5)
calls = stub_refresh(wd, [])
idx, row = wd.current_session()
check("拿到进行中的那一行", row.get("id"), "live-1")
check("没有触发重拉", calls, [])
check("返回的表格编号对得上", idx, 1)

print("\n== 2) 表格过期 + bridge 有新数据：当场同步重拉一次 ==")
wd = fresh()
set_rows(wd, [make_row(live_start(240), 50, "old-1", phase="已结束")],
         age_seconds=3600)
calls = stub_refresh(wd, [], new_rows=[make_row(live_start(), 50, "live-1")])
idx, row = wd.current_session()
check("重拉了一次且是阻塞语义", calls, [{"quiet": True, "wait": True}])
check("拿到重拉后的进行中场", row.get("id"), "live-1")

print("\n== 3) 重拉后仍然没有：报错带来源/新鲜度/bridge 错误，并落诊断日志 ==")
wd = fresh()
set_rows(wd, [make_row(live_start(240), 50, "old-1", phase="已结束")],
         age_seconds=3600, error="Target page, context or browser has been closed")
calls = stub_refresh(wd, [], new_rows=None,
                     error_after="Target page, context or browser has been closed")
captured = []
orig_log = w.rl.log_event


def _capture(area, event, **kw):
    captured.append((area, event, kw))
    return orig_log(area, event, **kw)


w.rl.log_event = _capture
try:
    wd.current_session()
    text = None
except ValueError as e:
    text = str(e)
w.rl.log_event = orig_log
check_true("抛了 ValueError", text is not None)
check_true("文案说明当场重拉过", "已当场重拉一次" in (text or ""))
check_true("文案带来源", "来源 bridge" in (text or ""))
check_true("文案带上一次成功时间", "上次成功" in (text or ""))
check_true("文案带 bridge 错误", "Target page" in (text or ""))
check_true("文案给出可执行退路", "用表格编号挂吧" in (text or ""))
missing = [entry for entry in captured if entry[1] == "current_missing"]
check("写了 current_missing 诊断日志", len(missing), 1)
detail = (missing[0][2].get("rows_detail") if missing else None) or []
check_true("诊断日志带每行 start/id/时间",
           bool(detail) and {"start", "id", "startMs", "endMs"} <= set(detail[0]))

print("\n== 4) 离线模式：不重拉，文案说明离线 ==")
wd = fresh(offline=True)
set_rows(wd, [], source="snapshot", age_seconds=600)
calls = stub_refresh(wd, [])
try:
    wd.current_session()
    text = None
except ValueError as e:
    text = str(e)
check("离线不调用 refresh", calls, [])
check_true("文案说明离线没有重拉", "离线模式没有重拉" in (text or ""))

print("\n== 5) 快照回退（行没有 id）：只要时间覆盖现在，也能认出当前场 ==")
wd = fresh()
set_rows(wd, [make_row(live_start(), 50, None)], source="snapshot",
         age_seconds=30)
calls = stub_refresh(wd, [])
idx, row = wd.current_session()
check("快照行也能当当前场", idx, 1)
check("没有触发重拉", calls, [])

print("\n" + ("全部通过" if not fails else "失败 %d 条" % len(fails)))
if fails:
    for f in fails:
        print(" - " + f)
    sys.exit(1)
