"""session 看板两口径：working（实际为主）/ detail（计划 + 实际）。"""
import importlib.util, io, os, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []

def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))

def check_true(label, got, want=True):
    check(label, bool(got), want)
TMP = tempfile.mkdtemp(prefix="session-modes-test-")
for key, name in (("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_SETTINGS_FILE", "settings.json"),
                  ("TI_TASKS_FILE", "tasks.json"),
                  ("TI_TASK_ARCHIVE", "archive.json"),
                  ("TI_NOSTART_FILE", "nostart.json"),
                  ("TI_POSTSESSION_FILE", "postsession.json")):
    os.environ[key] = os.path.join(TMP, name)
spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console
sp, sch, ks = w.sp, w.sch, w.ks
sch.DATA_CSV = os.path.join(TMP, "data.csv")
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
class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.last_idle_close = None
    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        return {"sessions": [], "tasks": []}
    def browser_state(self):
        return False, False, "test"
    def stop(self):
        pass

def mkwarden():
    console = Console(file=io.StringIO(), width=180, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True, net_async=False)
    wd.omnifocus.available = lambda: False
    wd.omnifocus.why_unavailable = lambda: "test"
    return wd

def row(idx, phase, start_ms, *, planned=False, sid=None, adopted=False,
        start="09:00", end="09:50"):
    return {
        "startMs": start_ms, "endMs": start_ms + 50 * 60 * 1000,
        "startLabel": start, "endLabel": end, "durationMin": 50,
        "id": sid, "title": "session %d" % idx, "quiet": False,
        "matched": bool(sid), "phase": phase, "planned": planned,
        "zone": "past" if phase == "已结束" else "locked",
        "contract": ("wed,%s,50" % start) if planned else "",
        "adopted": adopted, "actual_note": "实际 09:15 50min" if adopted else "",
        "period": {}, "period_first": False, "task_text": "",
    }

rows = [
    row(1, "已结束", 1, planned=True, sid="sess-1", start="09:00", end="09:50"),
    row(2, "已结束", 2, planned=True, adopted=True, start="10:00", end="10:50"),
    row(3, "已结束", 3, planned=False, sid="sess-3", start="11:00", end="11:50"),
    row(4, "进行中", 4, planned=False, sid="sess-4", start="12:00", end="12:50"),
    row(5, "未开始", 5, planned=True, start="13:00", end="13:50"),
    row(6, "未开始", 6, planned=True, adopted=True, start="14:00", end="14:50"),
    row(7, "未开始", 7, planned=False, sid="sess-7", start="15:00", end="15:50"),
]
print("== 纯筛选：working / detail ==")
detail, collapsed = w.visible_session_rows(rows, "detail")
check("detail 保留全部行", [idx for idx, _r in detail], [1, 2, 3, 4, 5, 6, 7])
check("detail 不折叠过去", collapsed, 0)
working, collapsed = w.visible_session_rows(rows, "working")
check("working 序号保留原表位置", [idx for idx, _r in working], [3, 4, 5, 7])
check("working 收起 1 条更早的过去 session", collapsed, 1)
check("working 不出现认账伪行",
      any((r.get("adopted") and not r.get("id")) for _idx, r in working), False)
check("working 将来未订上的计划行保留",
      [idx for idx, r in working if r.get("phase") == "未开始" and r.get("planned")
       and not r.get("id")], [5])
check("working 保留最近一条过去的实际 session",
      [idx for idx, r in working if r.get("phase") == "已结束"], [3])

print("\n== 命令：open working / open detail + 指示条 ==")
wd = mkwarden()
wd.rows = [dict(r) for r in rows]
wd.rows_source = "bridge"
check("默认是原来的 detail", wd.session_ui_mode, "detail")
check("open working 受理", wd.handle("open working"), True)
check("切到 working", wd.session_ui_mode, "working")
bar = wd.render_view_bar().plain
check_true("指示条同时有 working | detail", "working | detail" in bar)
check_true("指示条下方仍是视图栏", "视图 " in bar and "契约" in bar)
check("open detail 受理", wd.handle("open detail"), True)
check("切回 detail", wd.session_ui_mode, "detail")
print("\n== 表格渲染：working 收起过去 + caption；detail 原样 ==")
wd.session_ui_mode = "detail"
buf = io.StringIO()
wd.console = Console(file=buf, width=180, force_terminal=False)
wd.console.print(wd.render_table())
detail_out = buf.getvalue()
check_true("detail 渲染原表格", "The Contract Table" in detail_out)
check_true("detail 有认账行", "认账" in detail_out)
check_true("detail 没有折叠 caption", "过去 session 已收起" in detail_out, False)

wd.session_ui_mode = "working"
buf = io.StringIO()
wd.console = Console(file=buf, width=180, force_terminal=False)
wd.console.print(wd.render_table())
working_out = buf.getvalue()
check_true("working 仍是同一张合同表", "The Contract Table" in working_out)
check_true("working 不画认账伪行", "认账" in working_out, False)
check_true("working 画出折叠数量",
           "还有 1 条过去 session 已收起（open detail 查看）" in working_out)
check_true("working 保留最近过去场次 11:00", "11:00–11:50" in working_out)
check_true("working 不画更早过去场次 09:00", "09:00–09:50" in working_out, False)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
