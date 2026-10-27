#!/usr/bin/env python3
"""warden 新界面回归：时段列 / T1 排布 / 任务视图 / 自动挂 Focusmate task / 归档。"""
import datetime
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="task-ui-test-")
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

sp, sch, ts, ks = w.sp, w.sch, w.ts, w.ks
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
        "profiles": ["工作", "空闲"], "events": [],
        "periods": [
            {"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
             "type": "工作", "name": "SAT 准备", "calendar": "工作", "title": "SAT 准备"},
            {"id": "T2", "date": ISO, "start": "15:00", "end": "16:00",
             "type": "工作", "name": "托福 准备", "calendar": "工作", "title": "托福 准备"},
        ]}
sp.save_download(data)


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.last_idle_close = None

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "add_task":
            return {"verified": True, "taskId": "ft-1", "task": {"id": "ft-1"}}
        if cmd == "tasks":
            return {"tasks": [{"id": "ft-1", "title": "论文",
                               "completedAt": "2026-10-26T12:00:00Z"}]}
        if cmd == "release":
            return {"released": True}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden():
    console = Console(file=io.StringIO(), width=240, force_terminal=False)
    return w.Warden(console, FakeBridge(), no_clear=True, offline=True)


print("== 时段排布：T1 50 50；后续行时段列留空 ==")
wd = mkwarden()
check("session T1 50 50 受理", wd.handle("session T1 50 50"), True)
check("data.csv 是精确日期行",
      [l for l in open(sch.DATA_CSV, encoding="utf-8").read().splitlines()
       if l and not l.startswith("#")],
      ["%s,11:00,50" % ISO, "%s,12:00,50" % ISO, "%s,15:00,50" % ISO])
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
check("T1 有两行", [r.get("period_id") for r in wd.rows].count("T1"), 2)
check("T1 只在第一行显示 ID",
      [r.get("period_first") for r in wd.rows if r.get("period_id") == "T1"],
      [True, False])
buf = io.StringIO()
wd.console = Console(file=buf, width=240, force_terminal=False)
wd.paint()
out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.getvalue())
check_true("表头有时段 ID", "时段 ID" in out)
check_true("表头有 task", "task" in out)
check_true("时段名称在表里", "SAT 准备" in out)

before = open(sch.DATA_CSV, encoding="utf-8").read()
check("越界命令被受理（错误进消息区）", wd.handle("T1 50 50 50"), True)
check("越界后 data.csv 一字未动",
      open(sch.DATA_CSV, encoding="utf-8").read(), before)
check_true("越界错误说清超过终点",
           any("超过时段终点" in m[2] for m in wd.messages))

print("== 任务 CRUD + 自动挂 Focusmate + 归档 ==")
check("task create 显式 ID", wd.handle("task create 5 title=论文 notes=先写intro"), True)
check("任务 5 存在", wd.task_store.get(5)["fields"]["title"], "论文")
check("task modify 不改字段校验", wd.handle("task modify 5 due 明天"), True)
check("任意字段被写入", wd.task_store.get(5)["fields"]["due"], "明天")

# 造一带 session id 的权威行，供 task assign 用
import datetime as dt
plan = sch.day_slots(wd.entries, wd.sel_date)
wd.api_rows = []
for slot in plan:
    h, m = map(int, slot["start"].split(":"))
    st = dt.datetime.combine(wd.sel_date, dt.time(h, m))
    en = st + dt.timedelta(minutes=int(slot["duration"]))
    wd.api_rows.append({
        "startMs": int(st.timestamp() * 1000), "endMs": int(en.timestamp() * 1000),
        "startLabel": slot["start"], "endLabel": en.strftime("%H:%M"),
        "durationMin": int(slot["duration"]), "id": "sess-" + slot["start"],
        "title": "", "quiet": False, "matched": False,
        "phase": wd.phase_of(int(st.timestamp() * 1000), int(en.timestamp() * 1000),
                             int(dt.datetime.now().timestamp() * 1000)),
    })
wd.rows_source = "bridge"
wd.rebuild_rows()

wi = [i for i, r in enumerate(wd.rows, 1) if r["startLabel"] == "11:00"][0]
check("task select 5", wd.handle("task select 5"), True)
check("task assign session #%d" % wi, wd.handle("task assign %d" % wi), True)
add = [c for c in wd.bridge.calls if c[0] == "add_task"]
check("自动调了 bridge add_task", len(add), 1)
check("add_task 带的是 session 身份", add[0][1]["meetingId"], "sess-11:00")
check("本地 task 关联成功", wd.task_store.get(5)["session"]["start"], "11:00")
check("Focusmate task id 记回本地",
      wd.task_store.get(5)["focusmate"]["task_id"], "ft-1")

# 回归：assign 成功之后**不用手动 rebuild**，表格里的 task 列就该有了
# （用户报的正是「assign 到 #3 成功了，但表格 task 列空着」）
row1100 = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
check("assign 之后表格 task 列立刻有内容（不用手动 refresh）",
      row1100.get("task_text") != "", True)
check_true("assign 之后 task 列显示的就是那条任务", "论文" in row1100.get("task_text", ""))

print("== task ... --assign N：任何子命令都能顺手挂 ==")
row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
idx1200 = wd.rows.index(row1200) + 1
wd.bridge.calls = []
check("task create ... --assign N 受理",
      wd.handle("task create title=写论文 --assign %d" % idx1200), True)
new_id = max(t["id"] for t in wd.task_store.list())
check("任务建出来了", wd.task_store.get(new_id)["fields"]["title"], "写论文")
check("顺手挂到了那一场",
      wd.task_store.get(new_id)["session"]["start"], "12:00")
check("Focusmate 也只调了一次 add_task",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)
row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
check("表格那一行的 task 列也更新了", "写论文" in row1200.get("task_text", ""), True)

wd.bridge.calls = []
check("--assign=N 这种写法也认",
      wd.handle("task modify %d due 后天 --assign %d" % (new_id, idx1200)), True)
check("modify 的字段生效", wd.task_store.get(new_id)["fields"]["due"], "后天")
check("又挂了一次（同一场重挂是会走 bridge 的）",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)

check("--assign 后面没给 ID → 报错不改东西", wd.handle("task create title=空挂 --assign"), True)
check_true("给了人话提示", "--assign 后面要跟" in wd.messages[-1][2])
check("没建出这条任务",
      [t for t in wd.task_store.list() if (t.get("fields") or {}).get("title") == "空挂"], [])

wd.bridge.calls = []
wd.handle("task delete %d --assign 1" % new_id)
check("delete 带 --assign：不动 bridge", [c for c in wd.bridge.calls if c[0] == "add_task"], [])
check_true("并且说清 --assign 用错地方了",
           any("没有可挂的任务" in m[2] for m in wd.messages))
wd.handle("task create 5 title=论文 notes=先写intro")

check("task sync 受理", wd.handle("task sync"), True)
check("本地记 completedAt", wd.task_store.get(5)["focusmate"]["completed_at"],
      "2026-10-26T12:00:00Z")
arch = wd.task_store.load_archive()["records"]
check("归档 1 条", len(arch), 1)
check("归档里有 session", arch[0]["session"]["focusmate_session_id"], "sess-11:00")
check("归档里有 Focusmate 完成时间", arch[0]["focusmate"]["completed_at"],
      "2026-10-26T12:00:00Z")

wd.rebuild_rows()
check("表格 task 列用 trim 后的一行",
      [r.get("task_text") for r in wd.rows if r["startLabel"] == "11:00"][0],
      "1. 论文  2. 先写intro  3. 明天")

print("== 同一场 session 可以挂多个 task（重点回归：已有 task 时再加一个）==")
row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
idx1200 = wd.rows.index(row1200) + 1
wd.bridge.calls = []
check("先挂第一个", wd.handle("task create title=写论文 --assign %d" % idx1200), True)
check("这场先有一个任务",
      [t["fields"].get("title") for t in
       wd.task_store.tasks_for_session(ISO, "12:00")], ["写论文"])
wd.bridge.calls = []
check("已经有 task 的场次再挂一个：受理",
      wd.handle("task create title=复盘 --assign %d" % idx1200), True)
check("bridge 照样被叫了一次（桥内部先 API 后 DOM）",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)
check("同一场现在挂了两条",
      [t["fields"].get("title") for t in
       wd.task_store.tasks_for_session(ISO, "12:00")], ["写论文", "复盘"])
row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
check("表格 task 列两条都显示",
      ("写论文" in row1200["task_text"], "复盘" in row1200["task_text"]), (True, True))
check("row['tasks'] 也是两条", len(row1200["tasks"]), 2)
check("先挂的那条没被覆盖（各自都有 session）",
      [t["session"]["start"] for t in wd.task_store.tasks_for_session(ISO, "12:00")],
      ["12:00", "12:00"])
check_true("消息里说了这场已有几个任务",
           any("这场已有 1 个任务" in m[2] for m in wd.messages))

print("== 任务改挂到别的场次：会提醒，旧 task 的完成时间也不丢 ==")
wd.messages = []
check("task select 5", wd.handle("task select 5"), True)
check("把 5 改挂到另一场", wd.handle("task assign %d" % idx1200), True)
check_true("提醒旧场次那条 task 不会跟着挪",
           any("不会跟着挪" in m[2] for m in wd.messages))
check("旧场次已记进 previous_session/focusmate",
      wd.task_store.get(5)["session"]["start"], "12:00")

for t in list(wd.task_store.list()):
    wd.handle("task delete %d" % t["id"])
check("task delete", wd.task_store.list(), [])

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
