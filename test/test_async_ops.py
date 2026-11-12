#!/usr/bin/env python3
"""后台网络队列 + 乐观写操作 + 失败回滚 + re-action 的回归。

这文件只测「异步那一半」：其它 17 个用例都跑在同步模式（网络当场跑完），
这里故意让 bridge **卡住**，才看得到「点完先有反应、失败再退回去」。

    python3 test/test_async_ops.py
"""
import copy
import datetime
import importlib.util
import io
import json
import os
import shutil
import tempfile
import threading
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


def check_in(label, needle, haystack):
    check(label, needle in haystack, True)


TMP = tempfile.mkdtemp(prefix="async-ops-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
# 真实的 temp/keep_session-nostart.json 里可能正躺着 daemon 刚入队的「到点没开始」，
# 不钉住的话 warden 一开就会弹那一问、把命令输入吃掉（p/x/s 才认）。
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
# 失败账一定要指到临时目录：真实的 data/failed-actions.json 是给人看的账本
os.environ["TI_FAILED_ACTIONS"] = os.path.join(TMP, "failed-actions.json")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, ks = w.sp, w.sch, w.ts, w.ks
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
FAILED_FILE = os.environ["TI_FAILED_ACTIONS"]

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作"], "events": [],
                  "periods": [{"id": "T1", "date": ISO, "start": "11:00",
                               "end": "13:00", "type": "工作", "name": "SAT",
                               "calendar": "工作", "title": "SAT"}]})


class GateBridge(object):
    """桥替身：每个写操作先卡在 gate 上，测试放行之后才回话。

    这就是「网络慢」的模型 —— 乐观 UI 有没有生效，只有慢才看得出来。
    """

    def __init__(self):
        self.calls = []
        self.fail = {}                 # cmd -> 异常信息（放行后抛）
        self.gate = threading.Event()
        self.gate.set()                # 默认不卡；想卡就 .hold()
        self.sessions = []
        self.completed = {}
        self.attached = {}
        self.titles = {}
        self.fm_seq = 0
        self.last_idle_close = None
        self.threads = []

    def hold(self):
        self.gate.clear()

    def release(self):
        self.gate.set()

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        self.threads.append(threading.current_thread().name)
        if not self.gate.wait(20.0):
            raise RuntimeError("测试没放行")
        if cmd in self.fail:
            raise w.BridgeError(self.fail[cmd])
        if cmd == "add_task":
            self.fm_seq += 1
            tid = "ft-%d" % self.fm_seq
            self.titles[tid] = kw.get("title")
            self.attached[tid] = kw.get("meetingId")
            return {"verified": True, "taskId": tid, "task": {"id": tid}, "via": "api"}
        if cmd == "delete_task":
            tid = str(kw.get("taskId") or "")
            self.attached.pop(tid, None)
            return {"verified": True, "taskId": tid, "deleted": {"id": tid}}
        if cmd == "complete_task":
            tid = str(kw.get("taskId") or "")
            self.completed[tid] = "2026-10-26T12:00:00Z"
            return {"verified": True, "completedAt": self.completed[tid], "taskId": tid}
        if cmd == "mute":
            # 命令完成后的自动对账会再拉一次 snapshot；替身必须和真实 API
            # 一样，把已经写成功的 quiet 反映到这份快照里。
            for row in self.sessions:
                if row.get("id") == kw.get("meetingId"):
                    row["quiet"] = bool(kw.get("quiet"))
            return {"verified": True, "quiet": bool(kw.get("quiet"))}
        if cmd == "title":
            for row in self.sessions:
                if row.get("id") == kw.get("meetingId"):
                    row["title"] = kw.get("title")
            return {"verified": True, "title": kw.get("title")}
        if cmd == "cancel":
            self.sessions = [row for row in self.sessions
                             if row.get("id") != kw.get("meetingId")]
            return {"verified": True}
        if cmd == "tasks":
            rows = [{"id": k, "title": self.titles.get(k, "论文"), "completedAt": v,
                     "attachments": ([{"type": "session", "externalId": self.attached[k]}]
                                     if self.attached.get(k) else [])}
                    for k, v in self.completed.items()]
            for tid, meeting in self.attached.items():
                if tid not in self.completed:
                    rows.append({"id": tid, "title": self.titles.get(tid, "论文"),
                                 "completedAt": None,
                                 "attachments": [{"type": "session",
                                                  "externalId": meeting}]})
            return {"tasks": rows}
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(time.time() * 1000),
                    "sessions": [dict(row) for row in self.sessions]}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmni(object):
    """OmniFocus 替身：记调用、能按需失败、能给 repetition_info。"""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.rows = [{"id": "pk-1", "name": "写论文（OmniFocus 全名）",
                      "estimated_minutes": 25, "completed": False}]
        self.gate = threading.Event()
        self.gate.set()

    def hold(self):
        self.gate.clear()

    def release(self):
        self.gate.set()

    def available(self):
        return True

    def list_tasks(self, refresh=False):
        self.calls.append(("list", refresh))
        self.gate.wait(20)
        if "list" in self.fail:
            raise w.of.OmniFocusError(self.fail["list"])
        return [dict(row) for row in self.rows]

    def repetition_info(self, pk):
        self.calls.append(("repetition_info", pk))
        if "repetition_info" in self.fail:
            raise w.of.OmniFocusError(self.fail["repetition_info"])
        return {}

    def complete(self, pk, at=None, due=None, yes=True):
        self.calls.append(("complete", pk, at, due))
        self.gate.wait(20)
        if "complete" in self.fail:
            raise w.of.OmniFocusError(self.fail["complete"])
        return {"changed": ["completed"], "before": {"completed": False},
                "after": {"completed": True, "id": pk}}

    def drop(self, pk, at=None, all_occurrences=False, yes=True):
        self.calls.append(("drop", pk, all_occurrences))
        self.gate.wait(20)
        if "drop" in self.fail:
            raise w.of.OmniFocusError(self.fail["drop"])
        return {"changed": ["dropped"], "before": {"dropped": False},
                "after": {"dropped": True, "id": pk}}


def mkwarden(bridge=None, async_net=True):
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, bridge or GateBridge(), no_clear=True, offline=False,
                  net_async=async_net)
    # 这个文件只测一条命令自己的乐观写/异步队列；命令后的自动对账在
    # test_task_ui.py 单独钉，不往这里的 GateBridge 队列里掺额外任务。
    wd.auto_refresh_after_command = False
    wd.omnifocus = FakeOmni()
    wd.settings.set_bool("omnifocus", True) if hasattr(wd.settings, "set_bool") else None
    return wd


def seed_rows(wd):
    """铺两张有 session id 的表行（11:00 / 12:00），省掉真快照。"""
    wd.api_rows = []
    for hhmm in ("11:00", "12:00"):
        h, m = map(int, hhmm.split(":"))
        start = datetime.datetime.combine(wd.sel_date, datetime.time(h, m))
        end = start + datetime.timedelta(minutes=50)
        wd.api_rows.append({
            "startMs": int(start.timestamp() * 1000),
            "endMs": int(end.timestamp() * 1000),
            "startLabel": hhmm, "endLabel": end.strftime("%H:%M"),
            "durationMin": 50, "id": "sess-" + hhmm, "title": "", "quiet": False,
            "matched": False,
            "phase": wd.phase_of(int(start.timestamp() * 1000),
                                 int(end.timestamp() * 1000),
                                 int(time.time() * 1000)),
        })
    wd.rows_source = "bridge"
    wd.read_at["session"] = time.time()
    wd.rebuild_rows()
    if hasattr(wd.bridge, "sessions"):
        wd.bridge.sessions = [dict(row) for row in wd.api_rows]


def settle(wd, timeout=15.0):
    """等后台那批跑完 + 把结果搬回主线程（等价于连续过几帧）。

    注意**要循环**：一次 apply 里可能又排下一步（import → 挂任务），
    所以「wait 一次、drain 一次」不够 —— 真界面里是靠每帧 drain 自然收敛的。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        wd.jobs.wait(max(0.1, deadline - time.time()))
        wd.drain_net()
        if not wd.jobs.busy:
            wd.drain_net()
            if not wd.jobs.busy:
                return


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def reset_store(wd):
    for task in wd.task_store.list():
        try:
            wd.task_store.delete(task["id"])
        except Exception:                       # noqa: BLE001 - 删不掉就算了
            pass


# ---------------------------------------------------------------- 1. 队列本身
print("== 1. 队列：单 worker、异步、乐观写操作先落本地 ==")
bridge = GateBridge()
wd = mkwarden(bridge)
check("异步模式真的开了", wd.net_async, True)
check("JobRunner 不是 sync", wd.jobs.sync, False)
seed_rows(wd)
task = wd.task_store.create(fields={"title": "写论文"})
idx = wd.rows.index([r for r in wd.rows if r["startLabel"] == "11:00"][0]) + 1

bridge.hold()                                   # 让 add_task 卡住
wd.handle("task assign %d" % idx)
check("命令立刻返回（没等网络）", True, True)
check("表格里立刻记上了 session",
      (wd.task_store.get(task["id"]).get("session") or {}).get("start"), "11:00")
check("打了「在途」标记（还是乐观的）",
      bool((wd.task_store.get(task["id"]).get("focusmate") or {}).get("pending")), True)
check("还没有真 task id",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("task_id"), None)
check("队列忙（面板会画出来）", wd.jobs.busy, True)
check("进度面板有东西", wd.shell.progress_value() is not None, True)
check_true("Recent 说「后台在挂」",
           any("后台" in m[2] for m in wd.messages))
check("bridge 只在 worker 线程里被调用", bridge.threads[0] != "MainThread", True)

bridge.release()
settle(wd)
check("落地之后在途标记没了",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("pending"), None)
check("拿到了真 task id",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("task_id"), "ft-1")
check("队列空了（面板消失）", wd.jobs.busy, False)
check("面板真的消失", wd.shell.progress_value(), None)
check_true("Recent 里有成功那句话", any("已挂上" in m[2] for m in wd.messages))
check("没有进失败账", wd.jobs.failures, [])

# ---------------------------------------------------------------- 1.5 用户命令
print("\n== 1.5 task <序号> -ac / formatter 挂载都不在提示符线程等 bridge ==")
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
task = wd.task_store.create(fields={"title": "紧凑写法也要异步"})
wd.current_session = lambda: (1, wd.rows[0])
bridge.hold()
started = time.time()
wd.handle("task %d -ac" % wd.task_no(task))
elapsed = time.time() - started
check("task <序号> -ac 立刻返回", elapsed < 1.0, True)
check("乐观结果已经落到本地",
      (wd.task_store.get(task["id"]).get("session") or {}).get("start"),
      wd.rows[0].get("startLabel"))
check("add_task 真的排了", any(c[0] == "add_task" for c in bridge.calls), True)
_add_at = next(i for i, c in enumerate(bridge.calls) if c[0] == "add_task")
check("add_task 在 worker 线程里跑", bridge.threads[_add_at], "repl-kit-jobs")
bridge.release()
settle(wd)

# formatter / 收尾共用的 assign_task_to 也不能同步等 add_task。
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
task = wd.task_store.create(fields={"title": "formatter 挂载"})
row = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
session = {"date": wd.sel_date.isoformat(), "start": row["startLabel"],
           "end": row["endLabel"], "duration": row["durationMin"],
           "focusmate_session_id": row["id"], "startMs": row["startMs"],
           "index": 1}
bridge.hold()
before_calls = len(bridge.calls)
started = time.time()
wd.assign_task_to(task, session)
elapsed = time.time() - started
check("formatter / 收尾的挂载立刻返回", elapsed < 1.0, True)
check("assign_task_to 先记本地",
      (wd.task_store.get(task["id"]).get("session") or {}).get("start"), "11:00")
check("assign_task_to 的 add_task 真的排了",
      any(c[0] == "add_task" for c in bridge.calls[before_calls:]), True)
check("assign_task_to 的 bridge 也在 worker 线程",
      bridge.threads[-1], "repl-kit-jobs")
bridge.release()
settle(wd)

# 剪贴板同样是外部读：task <id> link（不给链接）不能先在主线程等 osascript。
bridge = GateBridge()
wd = mkwarden(bridge)
task = wd.task_store.create(fields={"title": "剪贴板异步"})
_clip_gate = threading.Event()
_real_clip = w.of.clipboard_links
w.of.clipboard_links = lambda: (_clip_gate.wait(20),
                                [{"id": "pk-clip", "title": "剪贴板任务"}])[1]
try:
    started = time.time()
    wd.handle("task id:%d link" % task["id"])
    elapsed = time.time() - started
    check("task link 读剪贴板也立刻返回", elapsed < 1.0, True)
    check("命令返回时还没读完（后台在排队）",
          (wd.task_store.get(task["id"]).get("omnifocus") or {}).get("id"), None)
    _clip_gate.set()
    settle(wd)
    check("剪贴板读完后关联落到本地",
          (wd.task_store.get(task["id"]).get("omnifocus") or {}).get("id"), "pk-clip")
finally:
    _clip_gate.set()
    w.of.clipboard_links = _real_clip


# ---------------------------------------------------------------- 2. 失败回滚
print("\n== 2. 失败：精确回滚 + 警告 + 记账（落盘）==")
if os.path.exists(FAILED_FILE):
    os.unlink(FAILED_FILE)
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
task = wd.task_store.create(fields={"title": "会被回滚的任务"})
before = copy.deepcopy(wd.task_store.get(task["id"]))
idx = wd.rows.index([r for r in wd.rows if r["startLabel"] == "12:00"][0]) + 1
bridge.fail["add_task"] = "桥断了（测试）"
wd.handle("task assign %d" % idx)
check("乐观：一开始真的记上了",
      (wd.task_store.get(task["id"]).get("session") or {}).get("start"), "12:00")
settle(wd)
after = wd.task_store.get(task["id"])
check("回滚：session 退回去了", after.get("session"), before.get("session"))
check("回滚：focusmate 也退回去了", after.get("focusmate"), before.get("focusmate"))
check_true("说了「回滚」", any("回滚" in m[2] for m in wd.messages))
check("记进失败账（内存）", [f.action.name for f in wd.jobs.failures], ["task.assign"])
check("失败账真的落盘了", os.path.exists(FAILED_FILE), True)
with open(FAILED_FILE, encoding="utf-8") as handle:
    saved = json.load(handle)
args0 = saved["failures"][0]["action"]["args"]
check("盘上有名字 + 参数 + 原因",
      (saved["failures"][0]["action"]["name"], args0["task_id"],
       "桥断了（测试）" in saved["failures"][0]["error"]),
      ("task.assign", task["id"], True))
check("参数里带够了重放要用的东西（startMs / meetingId / 标题）",
      bool(args0.get("startMs")) and bool(args0.get("meeting_id"))
      and args0.get("title") == "会被回滚的任务", True)
warn_text = " ".join(row[0] for row in wd.warnings.rows()) if wd.warnings else ""
wd.collect_warnings()
warn_rows = [row[0] for row in wd.warnings.rows()]
check_true("警告板里有这条失败", any("网络操作失败" in text for text in warn_rows))
check_true("警告里教了下一步", any("re-action" in text for text in warn_rows))
check("表格里那条任务回到「赋闲」",
      [r.get("task_text") or "" for r in wd.rows if r["startLabel"] == "12:00"],
      [""])

print("\n== 2.1 下架：DELETE 已生效、复核命令自己炸了 → 远端没了就不恢复绑定 ==")
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
_lost = wd.task_store.create(fields={"title": "远端已删但本地曾被回滚"})
_lost_row = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
_lost_session = {
    "date": wd.sel_date.isoformat(), "start": _lost_row["startLabel"],
    "end": _lost_row["endLabel"], "duration": _lost_row["durationMin"],
    "focusmate_session_id": _lost_row["id"],
}
wd.task_store.assign(_lost["id"], _lost_session)
wd.task_store.attach_focusmate(
    _lost["id"], {"task_id": "ft-gone"}, session=_lost_session)
bridge.attached["ft-gone"] = _lost_row["id"]

# 重现桥里的真实事故：DELETE 已经生效，但删除后的复核阶段抛
# ReferenceError（用户看到的 `sleep is not defined`），于是整条命令报错。
_lost_before = copy.deepcopy(wd.task_store.get(_lost["id"]))
wd.task_store.unassign(_lost["id"], reason="cmd-assign-none")   # 乐观下架
bridge.attached.pop("ft-gone", None)                             # 远端其实已删
wd._detach_failed(_lost["id"], _lost_before, "sleep is not defined（测试）")
_lost_after = wd.task_store.get(_lost["id"])
check("远端已经删掉：本地不复原绑定", _lost_after.get("session"), None)
check("也不把 Focusmate task id 放回来",
      (_lost_after.get("focusmate") or {}).get("task_id"), None)
check_true("说明复核发现远端已经没了",
           any("Focusmate task" in m[2] and "保持下架" in m[2] for m in wd.messages))

print("\n== 3. re-action：重放全部失败行动（参数从盘上来）==")
bridge.fail.pop("add_task", None)               # 网络好了
console_lines = []
wd.console = Console(file=io.StringIO(), width=200, force_terminal=False)
out = io.StringIO()
wd.console = Console(file=out, width=200, force_terminal=False)
wd.handle("re-action --list")
check_in("--list 念出清单", "挂任务", out.getvalue())
wd.handle("re-action")
settle(wd)
check("重放成功之后失败账清空", wd.jobs.failures, [])
check("重放真的挂上了（本地也补上了）",
      (wd.task_store.get(task["id"]).get("session") or {}).get("start"), "12:00")
check("重放之后拿到真 task id",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("task_id"), "ft-1")
with open(FAILED_FILE, encoding="utf-8") as handle:
    check("盘上的账也清了", json.load(handle)["failures"], [])

# ---------------------------------------------------------------- 4. 读操作
print("\n== 4. 读操作：异步 + 数据新鲜度（不吵人 vs 红色加粗）==")
bridge = GateBridge()
wd = mkwarden(bridge)
bridge.sessions = [{"startMs": int(time.time() * 1000) + 3600000,
                    "endMs": int(time.time() * 1000) + 3600000 + 3000000,
                    "startLabel": "11:00", "endLabel": "11:50", "durationMin": 50,
                    "id": "sess-11:00", "title": "", "quiet": False, "matched": False}]
wd.read_at = {}
rows = wd.freshness_rows()
check("还没拉过：红字 + 加粗", rows[0][1], "bold red")
check_in("还没拉过就直说", "还没拉过", rows[0][0])
bridge.hold()
wd.refresh()
check("refresh 立刻返回，队列忙", wd.jobs.busy, True)
check("读操作没有预测（表格还空着）", len(wd.rows), 0)
bridge.release()
settle(wd)
check("真数据到了表格才有", len(wd.rows), 1)
check("拉了之后记下时间", bool(wd.read_at.get("session")), True)
wd.read_at["task"] = time.time()      # task 那一份也刚拉过（不然它会单独把整行拉红）
rows = wd.freshness_rows()
check("新鲜的数据是灰的（不吵）", rows[0][1], "dim")
check_in("新鲜的数据说「几秒前」", "秒前", rows[0][0])
wd.read_at["session"] = time.time() - (w.FRESH_SESSION + 120)
wd.read_at["task"] = time.time()
rows = wd.freshness_rows()
check("过期的数据红色加粗", rows[0][1], "bold red")
check_in("过期的数据直说不是最近的", "不是最近的", rows[0][0])
check("状态框里也有这一行",
      any("数据" in (row[0] or "") for row in wd.render_status_rows()), True)

# ---------------------------------------------------------------- 5. 行级操作
print("\n== 5. 行级乐观操作：mute / title / cancel ==")
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
row_before = dict(wd.rows[0])
bridge.hold()
wd.handle("mute 1 on")
check("mute 立刻改在表格里", wd.rows[0].get("quiet"), True)
bridge.release()
settle(wd)
check("mute 落地之后还是 on", wd.rows[0].get("quiet"), True)

bridge.fail["mute"] = "复核不一致（测试）"
wd.handle("mute 1 off")
bridge.release()
settle(wd)
check("mute 失败：行回滚到 on", wd.rows[0].get("quiet"), True)
check("mute 失败进账", [f.action.name for f in wd.jobs.failures], ["session.mute"])
wd.jobs.clear_failures()
bridge.fail.pop("mute", None)

bridge.hold()
wd.handle('title 1 "SAT 冲刺"')
check("title 立刻写进表格", wd.rows[0].get("title"), "SAT 冲刺")
bridge.release()
settle(wd)
check("title 落地", wd.rows[0].get("title"), "SAT 冲刺")

# cancel：执行区要敲宣言，但敲完不能等浏览器。这里把宣言闸门换成直接放行，
# 只测「命令立刻返回 + 后台有进度条 + 取消成功之后行消失」。
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
wd.require_declaration = lambda reason: True
row = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
row["zone"] = "locked"
idx = wd.rows.index(row) + 1
bridge.hold()
started = time.time()
wd.handle("cancel %d" % idx)
elapsed = time.time() - started
check("cancel 命令不在提示符线程等 bridge", elapsed < 1.0, True)
check("取消是乐观的：行立刻从表格消失",
      [r.get("startLabel") for r in wd.rows if r.get("id") == "sess-11:00"], [])
wait_for(lambda: wd.jobs.busy)
check("取消进了后台队列", wd.jobs.busy, True)
check_true("取消有进度面板（不是 console.status 转圈）",
           wd.shell.progress_value() is not None)
check_true("进度标题说在取消", "取消" in wd.jobs.title_text())
bridge.release()
settle(wd)
check("Focusmate 真的收到 cancel",
      [c for c in bridge.calls if c[0] == "cancel"][0][1].get("meetingId"),
      "sess-11:00")
check("取消成功后队列清空", wd.jobs.busy, False)
check("取消没有进失败账", wd.jobs.failures, [])

print("\n== 5.5 OmniFocus 也走同一条管道（乐观 + 回滚 + [标签]）==")
bridge = GateBridge()
wd = mkwarden(bridge)
wd.offline = False
task = wd.task_store.create(fields={"title": "写论文"})
wd.task_store.link_omnifocus(task["id"], "pk-1")
note = wd.omni_complete(wd.task_store.get(task["id"]))
check("本地优先：只记本地账，不立刻写 OmniFocus", "已记本地账" in note, True)
check("没有当场起 osascript", wd.omnifocus.calls, [])
check("记进了 outbox", [op.get("op") for op in wd.omni_outbox], ["complete"])
check("队列没被占（面板不卡）", wd.jobs.busy, False)

# 水位到了（空闲 90s / refresh）：一次 flush 把攒的账写回去
wd.omnifocus.hold()
wd.flush_omnifocus()
check("flush 排进队列（面板画得出来）", wd.jobs.busy, True)
wd.omnifocus.release()
settle(wd)
check("flush 真的调了 complete", [c[0] for c in wd.omnifocus.calls],
      ["complete"])
check("写回标记落定",
      bool(((wd.task_store.get(task["id"]).get("omnifocus") or {}).get("writeback")
            or {}).get("completed")), True)
check("outbox 清空", len(wd.omni_outbox), 0)
check("日志里带 [omnifocus] 标签",
      any("[omnifocus]" in text for _l, text in wd.jobs.log_lines(8)), True)
check("失败账空着", wd.jobs.failures, [])

# 失败：留在 outbox 里等下一轮（本地不回滚 —— 用户看到的 ✓ 就是真的）
if os.path.exists(FAILED_FILE):
    os.unlink(FAILED_FILE)
wd.jobs.clear_failures()
task2 = wd.task_store.create(fields={"title": "会失败的写回"})
wd.task_store.link_omnifocus(task2["id"], "pk-2")
wd.omni_complete(wd.task_store.get(task2["id"]))
wd.omnifocus.fail["complete"] = "OmniFocus 自动化被拒（测试）"
wd.flush_omnifocus()
settle(wd)
check("失败了还留在 outbox（下次再写）",
      [op.get("id") for op in wd.omni_outbox], ["pk-2"])
check("本地没乱回滚",
      ((wd.task_store.get(task2["id"]).get("omnifocus") or {}).get("writeback")
       or {}).get("completed"), None)
check("说了人话（警告）",
      any("写回" in m[2] and "失败" in m[2] for m in wd.messages), True)
wd.jobs.clear_failures()
wd.omni_outbox.clear()

print("\n== 5.6 一次操作、一个标题、编号步骤（task import -a）==")
bridge = GateBridge()
wd = mkwarden(bridge)
seed_rows(wd)
wd.omnifocus.hold()
wd.omnifocus.rows = [{"id": "pk-9", "name": "数学易错修正",
                      "estimated_minutes": 25, "completed": False}]
wd.handle("task import -a 1 [数学](omnifocus:///task/pk-9)")
check("操作标题是这次操作（不是某一步）",
      (wd.jobs.batch_title or "").startswith("import 任务（task import -a 1）"),
      True)
check("标题栏里带进度条 + 步骤数",
      bool(wd.jobs.total >= 1) and "─" in wd.jobs.title_text(), True)
wd.omnifocus.release()
settle(wd)
lines = [text for _l, text in wd.jobs.log_lines(10)]
check_true("步骤 1 是 [omnifocus] 获取任务",
           any(text.startswith("步骤 1：[omnifocus]") for text in lines))
check_true("步骤 2 是 [focusmate] 挂任务",
           any(text.startswith("步骤 2：[focusmate]") for text in lines))
check("导入 + 挂载都落地了",
      [(t.get("fields") or {}).get("title")
       for t in wd.task_store.list()
       if (t.get("fields") or {}).get("title") == "数学易错修正"
       and (t.get("session") or {}).get("start") == "11:00"],
      ["数学易错修正"])
check("面板干完就消失", wd.shell.progress_value(), None)
check("标题也清掉了", wd.jobs.batch_title, None)

print("\n== 6. 命令表：re-action / jobs 每个视图都在 ==")
bridge = GateBridge()
wd = mkwarden(bridge)
names = [c.name for c in wd.view_impls[0].common_commands()]
check_true("通用命令里有 re-action", "re-action" in names)
check_true("通用命令里有 jobs", "jobs" in names)
check_true("re-action 的别名也算命令",
           wd.view_impls[0].build_registry().get("reaction") is not None)
out = io.StringIO()
wd.console = Console(file=out, width=200, force_terminal=False)
wd.handle("jobs")
check_in("jobs 念出队列状态", "网络", out.getvalue())

print("\n== 7. 同步模式（测试/--exec）仍然是当场跑完 ==")
bridge = GateBridge()
wd = mkwarden(bridge, async_net=False)
check("sync 模式", wd.jobs.sync, True)
seed_rows(wd)
task = wd.task_store.create(fields={"title": "同步挂"})
idx = wd.rows.index([r for r in wd.rows if r["startLabel"] == "11:00"][0]) + 1
wd.handle("task assign %d" % idx)
check("同步：命令返回时就已经挂好",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("task_id"), "ft-1")
check("同步：没有在途标记",
      (wd.task_store.get(task["id"]).get("focusmate") or {}).get("pending"), None)

print()
if fails:
    print("失败 %d 条：" % len(fails))
    for item in fails:
        print("  - " + item)
else:
    print("全绿。")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if fails else 0)
