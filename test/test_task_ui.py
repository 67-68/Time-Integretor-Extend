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
        self.fm_seq = 0
        self.titles = {}
        # Focusmate 侧「已经完成」的 task：id → completedAt
        self.completed = {"ft-1": "2026-10-26T12:00:00Z"}
        # Focusmate 侧「这条 task 挂在哪一场」：task id → meeting id
        # （refresh --check 就是拿它跟本地账本对的，所以替身必须有这一份）
        self.attached = {"ft-1": "sess-11:00"}
        self.deleted = []
        # snapshot 返回的场次（refresh 从这里拿「这一天到底有哪些 session」）
        self.sessions = []

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "add_task":
            self.fm_seq += 1
            tid = "ft-%d" % self.fm_seq
            self.titles[tid] = kw.get("title")
            self.attached[tid] = kw.get("meetingId")
            return {"verified": True, "taskId": tid, "task": {"id": tid}, "via": "api"}
        if cmd == "delete_task":
            tid = str(kw.get("taskId") or "")
            self.deleted.append(tid)
            gone = self.attached.pop(tid, None) is not None
            self.completed.pop(tid, None)
            self.titles.pop(tid, None)
            return {"verified": True, "taskId": tid, "deleted": {"id": tid} if gone else None}
        if cmd == "tasks":
            rows = [{"id": k, "title": self.titles.get(k, "论文"),
                     "completedAt": v,
                     "attachments": ([{"type": "session", "externalId": self.attached[k]}]
                                     if self.attached.get(k) else [])}
                    for k, v in self.completed.items()]
            # 挂上但还没完成的 task 也要出现在 GET /tasks 里
            for tid, meeting in self.attached.items():
                if tid not in self.completed:
                    rows.append({"id": tid, "title": self.titles.get(tid, "论文"),
                                 "completedAt": None,
                                 "attachments": [{"type": "session", "externalId": meeting}]})
            return {"tasks": rows}
        if cmd == "release":
            return {"released": True}
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": [dict(row) for row in self.sessions]}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden():
    console = Console(file=io.StringIO(), width=240, force_terminal=False)
    return w.Warden(console, FakeBridge(), no_clear=True, offline=True)


def tno(wd_, task):
    """这条任务在**面板上**是第几条 —— 命令里要敲的就是这个数（不是内部 id）。"""
    no = wd_.task_no(task)
    assert no, "任务 %r 不在 %s 的面板上" % ((task or {}).get("id"), wd_.sel_date)
    return no


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
# 需求：任务被分配到一场 session 上，截止时间**自动**变成那场的终点
# （12:00 + 50min = 12:50）。手写的那条不丢，留在 session.previous_due 里。
check("assign 之后截止时间 = 这场 session 的终点",
      wd.task_store.get(new_id)["fields"]["due"], "%sT12:50:00" % ISO)
check("原来手写的 due 留档在 session.previous_due",
      wd.task_store.get(new_id)["session"].get("previous_due"), "后天")
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

print("== 裸序号等价 select：task 3 ≡ task select 3，task 3 -a 1 ==")
check("task create 77 title=裸号", wd.handle("task create title=裸号"), True)
bare = [t for t in wd.task_store.list()
        if (t.get("fields") or {}).get("title") == "裸号"][0]
bare_no = tno(wd, bare)
check("task <序号> 受理（裸序号 = select）", wd.handle("task %d" % bare_no), True)
check("裸序号真的选中了那条", wd.selected_task_id, bare["id"])
check_true("说的还是 select 那套话",
           any("已选中任务 #%d" % bare_no in m[2] for m in wd.messages))
check("task select <序号> 照样能用",
      wd.handle("task select %d" % bare_no), True)

row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
idx1200 = wd.rows.index(row1200) + 1
wd.bridge.calls = []
check("task <序号> -a N：选中 + 挂上一步到位",
      wd.handle("task %d -a %d" % (bare_no, idx1200)), True)
check("真的挂到那一场了",
      wd.task_store.get(bare["id"])["session"]["start"], "12:00")
check("bridge 只调了一次 add_task",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)
check("选中状态落在它身上", wd.selected_task_id, bare["id"])

check("task <序号> -n 也能用",
      wd.handle("task %d -n 材料在这 https://example.com/bare" % bare_no), True)
check("备注写进去了", wd.task_store.notes(wd.task_store.get(bare["id"])),
      "材料在这 https://example.com/bare")

check("裸序号后面再跟东西 → 受理但报错",
      wd.handle("task %d %d" % (bare_no, idx1200)), True)
check_true("提示改用 -a/--assign",
           any("要挂到 session 用 -a/--assign" in m[2] for m in wd.messages))
check("报错时没改任何绑定",
      wd.task_store.get(bare["id"])["session"]["start"], "12:00")

check("不存在的序号 → 受理但报错", wd.handle("task 999"), True)
check_true("说清面板上只有 1..N，没有 999",
           any("没有 #999" in m[2] and "面板上现在只有" in m[2] for m in wd.messages))

check("未知动作 → 受理但报错", wd.handle("task 装死"), True)
check_true("错误提示里也提一句裸序号",
           any("task select" in m[2] for m in wd.messages))
wd.handle("task delete %d" % tno(wd, wd.task_store.get(bare["id"])))
check("收尾把那条删掉",
      [t for t in wd.task_store.list() if t["id"] == bare["id"]], [])

wd.rebuild_rows()
# task sync 之后这条任务就算「完成」了（Focusmate 的 completedAt）→ 前面带 ✓
_paper_no = tno(wd, wd.task_store.get(5))
check("表格 task 列是「状态记号 + 编号: 标题」",
      [r.get("task_text") for r in wd.rows if r["startLabel"] == "11:00"][0],
      "✓%d: 论文" % _paper_no)

print("== 契约视图：task 列「编号: 标题」+ 下面那块两列任务表格 ==")
row1100 = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
check("task 列带任务编号", row1100["task_text"], "✓%d: 论文" % _paper_no)
# 同一场两个任务 → 用 | 分隔；新加的那条还没收工 → 记号是 ·
wd.handle("task create title=第二条 --assign %d" % (wd.rows.index(row1100) + 1))
row1100 = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
check_true("两条任务用 | 分开且都带状态记号",
           re.match(r"^✓\d+: .+ \| ·\d+: .+$", row1100["task_text"]) is not None)
check_true("标题长的那条被 trim 了（带 …）或本来就不长",
           "…" in row1100["task_text"] or len(row1100["task_text"]) < 40)

contract = [v for v in w.VIEWS if v.key == "1"][0](wd)
parts = contract.render()
check("契约视图先给契约表格、再给任务面板（收尾面板在打断栏位里）", len(parts), 2)
check_true("第二块就是那张任务面板（一张表，不拆）", hasattr(parts[1], "columns"))
check_true("任务面板标题里写了记号图例", "Task · 任务" in str(parts[1].title))
check("面板里是 number/task 一对一对的", len(parts[1].columns) % 2, 0)

print("== 任务面板：屏幕够宽就摊成 3 / 4 列（不再固定两列）==")
# 列数只跟「终端有多宽」和「标题最长有多长」有关，所以把宽度显式喂进去。
# 造 20 条同形任务：id 两位数，标题 12 个汉字（显示宽 24 列 → 一对 28 列）。
_many = [{"id": i, "fields": {"title": "任务标题任务标题任务标题"}} for i in range(1, 21)]
check("窄屏 60 列：只能 2 列", wd.task_index_layout(_many, width=60), (2, 10))
check("80 列还是 2 列", wd.task_index_layout(_many, width=80)[0], 2)
check("110 列：3 列", wd.task_index_layout(_many, width=110)[0], 3)
check("110 列每列 7 条（20 / 3）", wd.task_index_layout(_many, width=110), (3, 7))
check("160 列：4 列", wd.task_index_layout(_many, width=160)[0], 4)
check("再宽也不会超过 4 列", wd.task_index_layout(_many, width=1000)[0], 4)
check("宽屏每列 5 条（20 / 4）", wd.task_index_layout(_many, width=160), (4, 5))
check("任务少的时候不硬凑（3 条 → 3 列 1 行，没有空列）",
      wd.task_index_layout(_many[:3], width=1000), (3, 1))
check("1 条任务 = 1 列", wd.task_index_layout(_many[:1], width=1000), (1, 1))
check("22 条 / 4 列 = 6 行，最后一列不满也不多开一列",
      wd.task_index_layout(_many + [{"id": 21, "fields": {}}, {"id": 22, "fields": {}}],
                           width=160), (4, 6))
# 20 条并排 4 对 → 5 行（填法是「一行一行」，见下面「填法」那一节）
_cols, _rows = wd.task_index_layout(_many, width=160)
check("20 条并排 4 对 = 5 行", (_cols, _rows), (4, 5))

# 真的画出来：160 列的终端里，表里是 2*cols 个 column
_real_width = wd.frame_width
for _t in (_many[:1],):
    pass
wd.frame_width = lambda: 160
_wide_cols = wd.task_index_layout(wd.task_panel_tasks() or _many, width=160)[0]
_wide_panel = [v for v in w.VIEWS if v.key == "1"][0](wd).render()[1]
wd.frame_width = _real_width
wide_table = _wide_panel
check("160 列 → 表里的 column 数 = 2 * 列数", len(wide_table.columns), 2 * _wide_cols)
check("列数还是偶数（number/task 一对一对的）", len(wide_table.columns) % 2, 0)

# 染色：已挂到 session 的 = 红，没挂的 = 深蓝
# （不能用任务 5 了 —— 它已经被 task sync 标成完成，那一行是绿的）
wd.handle("task create title=染红用 --assign %d" % (wd.rows.index(row1100) + 1))
task_assigned = [t for t in wd.task_store.list()
                 if (t.get("fields") or {}).get("title") == "染红用"][0]
wd.handle("task create title=还没挂的任务")
task_free = [t for t in wd.task_store.list() if not t.get("session")][0]
num_a, title_a = wd._task_index_cells(task_assigned, None)
num_f, title_f = wd._task_index_cells(task_free, None)
check("已 assign 的编号+标题染红",
      (str(num_a.style), str(title_a.style)), (w.STYLE_ASSIGNED, w.STYLE_ASSIGNED))
check("没 assign 的是亮蓝（深色背景上 dark_blue 等于隐形，用户反馈过）",
      (str(num_f.style), str(title_f.style)), (w.STYLE_ACTIVE, w.STYLE_ACTIVE))
check("已完成染绿", str(wd._task_index_cells(wd.task_store.get(5), None)[1].style),
      w.STYLE_DONE)
check("编号列显示的是**面板序号**（不是内部 id）",
      str(num_a), str(tno(wd, task_assigned)))
# 状态记号：完成 ✓ / 进行中 ·（表格里「什么都没有」曾经分不清是哪种）
done_task = wd.task_store.get(5)
num_d, title_d = wd._task_index_cells(done_task, None)
check("完成的任务打 ✓", str(title_d).startswith("✓"), True)
check("完成的染绿", str(num_d.style), "green")
check("进行中的打 ·", str(title_f).startswith("·"), True)
check("task_mark：进行中 = ·", wd.task_mark(task_free), "·")
check("task_mark：完成 = ✓", wd.task_mark(done_task), "✓")
index_table = [v for v in w.VIEWS if v.key == "1"][0](wd).render()[1]
check_true("任务面板标题里写了记号图例", "✓ 完成" in str(index_table.title))
check_true("表尾有一行账（几条进行中/完成/放弃）",
           "%d 条：" % len(wd.task_panel_tasks()) in str(index_table.caption))
check_true("表尾写清这块面板的口径（今天 = 没做完的 + 今天处理过的）",
           "今天处理过的" in str(index_table.caption))

print("== 任务面板按天：今天 = 没做完的 + 今天处理过的；过去那天固化 ==")
today_iso = TODAY.isoformat()
yday_iso = (TODAY - datetime.timedelta(days=1)).isoformat()
before_iso = (TODAY - datetime.timedelta(days=2)).isoformat()


def _new_task(title):
    wd.handle("task create title=%s" % title)
    return max(t["id"] for t in wd.task_store.list())


# A：昨天那场收尾里放弃了（有痕迹 + 状态时间都在昨天）
a_id = _new_task("昨天放弃的那条")
wd.task_store.mark_processed(a_id, yday_iso, "09:30", "drop")
wd.task_store.drop(a_id, reason="review-drop", at="%sT09:40:00" % yday_iso)
# B：现在还开着，谁也没碰过
b_id = _new_task("还开着的")
# C：前天完成的老账，**没有**处理痕迹（模拟升级前就收工的数据）
c_id = _new_task("前天完成的老账")
wd.task_store.set_status(c_id, ts.STATUS_COMPLETED, at="%sT10:00:00" % before_iso)
# D：昨天被「跳过」的任务，现在还开着
d_id = _new_task("昨天被跳过的")
wd.task_store.mark_processed(d_id, yday_iso, "15:00", "skip")

wd.day_offset = 0
today_ids = [t["id"] for t in wd.task_panel_tasks()]
check("今天面板里有没做完的", b_id in today_ids, True)
check("今天面板里有「昨天问过、还开着」的（它还没做完）", d_id in today_ids, True)
check("今天面板里没有昨天放弃的那条（今天没它的事）", a_id in today_ids, False)
check("今天面板里没有更早完成的老账", c_id in today_ids, False)

wd.day_offset = -1
yday_ids = [t["id"] for t in wd.task_panel_tasks()]
check("昨天面板固化：只剩那天处理过的", a_id in yday_ids, True)
check("跳过的也算那天处理过（问过它、你说先放着）", d_id in yday_ids, True)
check("昨天面板上今天还开着、昨天没碰过的不出现", b_id in yday_ids, False)
_past = [v for v in w.VIEWS if v.key == "1"][0](wd).render()[1]
check_true("过去那天的表尾写清「固化」口径", "过去就固化" in str(_past.caption))
check_true("过去那天的表尾带日期", yday_iso in str(_past.caption))

wd.day_offset = -2
before_ids = [t["id"] for t in wd.task_panel_tasks()]
check("前天面板里有前天完成的那条（老数据靠 status_at 兜底）", c_id in before_ids, True)
check("前天面板里没有昨天的事", a_id in before_ids, False)

recs = wd.task_store.processed_on(wd.task_store.get(c_id), before_iso)
check("兜底痕迹标了 derived", [r["derived"] for r in recs], [True])
recs = wd.task_store.processed_on(wd.task_store.get(a_id), yday_iso)
check("显式痕迹没有 derived 标记", [r["derived"] for r in recs], [False])
check("同一天同一场只留一条痕迹（后一次覆盖前一次）",
      len(wd.task_store.mark_processed(a_id, yday_iso, "09:30", "drop")["history"]),
      len([r for r in wd.task_store.get(a_id)["history"] if r["day"] == yday_iso]))

wd.day_offset = 0

print("== 备注：task note / note= 收敛成 notes ==")
wd.handle("task note %d 材料在这 https://example.com/set3"
          % tno(wd, wd.task_store.get(5)))
check("task note 写进 notes", wd.task_store.get(5)["fields"]["notes"],
      "材料在这 https://example.com/set3")
wd.handle("task create title=带备注 note=https://example.com/b")
note_id = max(t["id"] for t in wd.task_store.list())
check("create note= 也收敛成 notes",
      wd.task_store.get(note_id)["fields"].get("notes"), "https://example.com/b")
wd.handle("task note %d" % note_id)
check("task note 不带文本 = 清空", wd.task_store.get(note_id)["fields"]["notes"], "")

print("== --note / -n：任何 task 子命令都能带 ==")
wd.handle("task create title=带备注的 -n https://example.com/a")
nid = max(t["id"] for t in wd.task_store.list())
check("-n 在 create 上生效", wd.task_store.get(nid)["fields"]["notes"],
      "https://example.com/a")
wd.handle("task modify %d due 明天 --note=https://example.com/b" % nid)
check("--note= 也认", wd.task_store.get(nid)["fields"]["notes"], "https://example.com/b")
check("原来的 modify 照样生效", wd.task_store.get(nid)["fields"]["due"], "明天")
wd.handle("task select %d -n 材料在这 https://example.com/c" % nid)
check("select 上也能带，且备注带空格不用加引号",
      wd.task_store.get(nid)["fields"]["notes"], "材料在这 https://example.com/c")
wd.handle("task note %d" % nid)
wd.handle("task create title=清空备注 --note=x")
cid = max(t["id"] for t in wd.task_store.list())
wd.handle("task modify %d any 1 --note=" % cid)
check("--note= 空 = 清空", wd.task_store.get(cid)["fields"]["notes"], "")
wd.messages = []
wd.handle("task delete %d --note 想写也写不上" % cid)
check_true("对 delete 带 --note：只警告不炸",
           any("没意义" in m[2] for m in wd.messages))
check("任务确实删掉了", [t["id"] for t in wd.task_store.list() if t["id"] == cid], [])
wd.messages = []
wd.handle("task create title=空备注 --note")
check_true("--note 后面没给文本 → 人话提示",
           any("--note 后面要跟备注文本" in m[2] for m in wd.messages))
check("没建出这条任务",
      [t for t in wd.task_store.list() if (t.get("fields") or {}).get("title") == "空备注"], [])

print("== --note 和 --assign 能一起用 ==")
row1500b = [r for r in wd.rows if r["startLabel"] == "15:00"][0]
wd.bridge.calls = []
wd.handle("task create title=组合 --note 材料在这 https://example.com/combo --assign %d"
          % (wd.rows.index(row1500b) + 1))
combo = max(t["id"] for t in wd.task_store.list())
check("备注写上了（带空格也吃全）",
      wd.task_store.get(combo)["fields"]["notes"], "材料在这 https://example.com/combo")
check("也挂到那一场了", wd.task_store.get(combo)["session"]["start"], "15:00")
check("bridge 只被叫了一次",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)
check("Focusmate 收到的 description 就是备注",
      [c for c in wd.bridge.calls if c[0] == "add_task"][0][1]["notes"],
      "材料在这 https://example.com/combo")

print("== 完成之后：消息和归档面板都要有备注（含可点网址）==")
wd.bridge.calls = []
row1500 = [r for r in wd.rows if r["startLabel"] == "15:00"][0]
wd.handle("task create title=完成看备注 note=https://example.com/done --assign %d"
          % (wd.rows.index(row1500) + 1))
done_task = max(t["id"] for t in wd.task_store.list())
ft_id = wd.task_store.get(done_task)["focusmate"]["task_id"]
wd.bridge.completed[ft_id] = "2026-10-27T12:50:00Z"
wd.messages = []
wd.cmd_task_sync(quiet=True)
check_true("Recent 里报了完成（编号是面板序号）",
           any("完成：任务 #%d" % tno(wd, wd.task_store.get(done_task)) in m[2]
               for m in wd.messages))
check_true("Recent 里带备注", any("https://example.com/done" in m[2] for m in wd.messages))
arch = wd.task_store.load_archive()["records"]
check("归档记录里有 notes", [r["notes"] for r in arch if r["task_id"] == done_task],
      ["https://example.com/done"])
taskview = [v for v in w.VIEWS if v.key == "4"][0](wd)
tv = taskview.render()
panel = [p for p in tv if "完成归档" in str(getattr(p, "title", ""))]
check("任务视图里有归档面板", len(panel), 1)
r = note_render = None
out = io.StringIO()
Console(file=out, width=200, force_terminal=False).print(panel[0])
check_true("归档面板里看得到备注", "https://example.com/done" in out.getvalue())
rend = w.note_renderable("材料在这 https://example.com/set3")
check("网址被渲染成可点的超链接（OSC 8）",
      [sp.style.link for sp in rend.spans if getattr(sp.style, "link", None)],
      ["https://example.com/set3"])
check_true("备注正文也留着", "材料在这" in str(rend))
check("note_renderable 空备注给空文本", str(w.note_renderable("")), "")
check_true("没有网址就只显示文本", "📝" in str(w.note_renderable("下午三点前弄完")))

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
check("task select <序号>", wd.handle("task select %d" % tno(wd, wd.task_store.get(5))), True)
check("把它改挂到另一场", wd.handle("task assign %d" % idx1200), True)
check_true("提醒旧场次那条 task 不会跟着挪",
           any("不会跟着挪" in m[2] for m in wd.messages))
check("旧场次已记进 previous_session/focusmate",
      wd.task_store.get(5)["session"]["start"], "12:00")

# 不在今天面板上的（昨天/前天处理过的）走 `id:N` 显式按内部 id 删
for t in list(wd.task_store.list()):
    wd.handle("task delete id:%d" % t["id"])
check("task delete（id:N 显式按内部 id 也能删）", wd.task_store.list(), [])
wd.handle("task create title=甲")
wd.handle("task create title=乙")
wd.handle("task delete 1")
check("task delete 按面板序号删（删完序号重排，剩下的变成 #1）",
      [(t["fields"]["title"], tno(wd, t)) for t in wd.task_store.list()], [("乙", 1)])
for t in list(wd.task_store.list()):
    wd.handle("task delete id:%d" % t["id"])
check("清干净", wd.task_store.list(), [])


# ---------------------------------------------------------------- 剪贴板导入

print("== task import：读剪贴板是**动作**，-a 只是分流后缀（⌘V 到终端会丢链接）==")
CLIP = [{"id": "i5BKXC5uNk0", "url": "omnifocus:///task/i5BKXC5uNk0",
         "title": "TI: Swap", "has_id": True, "raw": "omnifocus:///task/i5BKXC5uNk0"}]
_real_clip = w.of.clipboard_links
w.of.clipboard_links = lambda: [dict(x) for x in CLIP]
wd.messages = []
check("task import（不带参数）就受理", wd.handle("task import"), True)
made = wd.task_store.get(wd.selected_task_id)
check("从剪贴板读到了 OmniFocus primary key",
      (made.get("omnifocus") or {}).get("id"), "i5BKXC5uNk0")
check("标题也来自剪贴板", made["fields"]["title"], "TI: Swap")
check_true("消息里说明链接是从剪贴板读的",
           any("链接是从剪贴板读的" in m[2] for m in wd.messages))

wd.messages = []
check("task import -a 3：动作 + 分流一起写（-a 是 --assign）",
      wd.handle("task import -a 1"), True)
check("两条都关联上了",
      len([t for t in wd.task_store.list()
           if (t.get("omnifocus") or {}).get("id") == "i5BKXC5uNk0"]), 2)
check("新导入的那条顺手分流到了 #1（表格第一场）",
      (wd.task_store.get(wd.selected_task_id).get("session") or {}).get("start"),
      wd.rows[0]["startLabel"])

# 粘贴「只有标题」：去剪贴板里对同名的那条，对上了就关联（不是拿标题猜）
w.of.clipboard_links = lambda: [dict(CLIP[0])]
wd.messages = []
check("粘贴只有标题也受理", wd.handle("task import TI: Swap"), True)
linked = wd.task_store.get(wd.selected_task_id)
check("同名 → 认下剪贴板里的链接", (linked.get("omnifocus") or {}).get("id"),
      "i5BKXC5uNk0")
# 对不上就明说没关联
w.of.clipboard_links = lambda: [{"id": "other", "title": "别的任务", "has_id": True}]
wd.messages = []
check("标题对不上也照样建", wd.handle("task import 谁也不是"), True)
loose = wd.task_store.get(wd.selected_task_id)
check("对不上 → 不关联", loose.get("omnifocus"), None)
check_true("并且明说没关联", any("没有关联" in m[2] for m in wd.messages))
check_true("给出补救办法（link 不带参数 = 读剪贴板）",
           any("link`（不带参数" in m[2] or "`task" in m[2] and "link" in m[2]
               for m in wd.messages))

wd.messages = []
w.of.clipboard_links = lambda: []
check("剪贴板空 → 报错但 TUI 不死", wd.handle("task import"), True)
check_true("错误说清剪贴板里没有链接",
           any("剪贴板里没有 OmniFocus 链接" in m[2] for m in wd.messages))

print("== task <id> link（不给链接 = 读剪贴板）==")
w.of.clipboard_links = lambda: [dict(CLIP[0])]
tgt = wd.task_store.create({"title": "先建后关联"})
check("link 不带链接也受理", wd.handle("task %d link" % tgt["id"]), True)
check("关联上了", (wd.task_store.get(tgt["id"]).get("omnifocus") or {}).get("id"),
      "i5BKXC5uNk0")
w.of.clipboard_links = _real_clip
for t in list(wd.task_store.list()):
    wd.handle("task delete %d" % t["id"])


# ---------------------------------------------------------------- 静音时段

print("== period mute T1 --permanent：写意图 … ==")
os.environ["TI_SESSION_MUTES"] = os.path.join(TMP, "mutes.json")
MUTES = os.environ["TI_SESSION_MUTES"]
if os.path.exists(MUTES):
    os.remove(MUTES)
wd.messages = []
check("period mute T1 受理", wd.handle("period mute T1"), True)
check("写到「只这一天」的表里", [(m.get("period"), m.get("date"))
                               for m in sp.load_mutes()["mutes"]],
      [("T1", ISO)])
check("静音窗是那个时段的时间窗",
      [(m["start"], m["end"]) for m in sp.load_mutes()["mutes"]], [("11:00", "13:00")])
check_true("消息说清「静音」到底指什么（Focusmate quiet，不是本地闭嘴）",
           any("Focusmate quiet" in m[2] and "本地看门狗" in m[2] for m in wd.messages))

wd.messages = []
check("period mute T1 --permanent 受理", wd.handle("period mute T1 --permanent"), True)
check("写进按星期几重复的表",
      [m.get("weekday") for m in sp.load_mutes()["mutes"] if m.get("weekday")],
      [sch.weekday_of_day(TODAY)])
check_true("消息说「每个周X」", any("每个" in m[2] and "静音了" in m[2] for m in wd.messages))
check("今天这个时段的 id 被认成静音",
      "T1" in sp.muted_period_ids(TODAY), True)
check("别的时段没事", "T2" in sp.muted_period_ids(TODAY), False)

print("== …然后这一场自动 quiet on ==")
wd.api_rows = []
plan_rows = [r for r in wd.rows if r.get("planned")] or []
for slot in sch.day_slots(wd.entries, wd.sel_date):
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
t1_rows = [r for r in wd.rows if r.get("period_id") == "T1"]
check("T1 的行都标了「时段静音」", [r.get("period_muted") for r in t1_rows],
      [True, True])
check("T2 的行没标", [r.get("period_muted") for r in wd.rows
                      if r.get("period_id") == "T2"], [False])
targets = wd.mute_targets()
check("要静音的就是这两场", [r["startLabel"] for r in targets], ["11:00", "12:00"])
wd.offline = False
wd.bridge.calls[:] = []
wd.messages = []
applied = wd.apply_mutes()
check("bridge 收到两次 mute quiet=True",
      [(c[0], c[1].get("quiet"), c[1].get("meetingId")) for c in wd.bridge.calls
       if c[0] == "mute"],
      [("mute", True, "sess-11:00"), ("mute", True, "sess-12:00")])
check("两场都确认了", [ok for _l, ok, _d in applied], [True, True])
check_true("消息说「已静音」", any("已静音" in m[2] for m in wd.messages))
# 已经是 ON 的不再重复动
for r in wd.rows:
    if r.get("period_id") == "T1":
        r["quiet"] = True
wd.bridge.calls[:] = []
check("已经 ON 的不会再动一次", wd.apply_mutes(), [])
wd.offline = True

buf = io.StringIO()
wd.console = Console(file=buf, width=240, force_terminal=False)
wd.paint()
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.getvalue())
check_true("表格里能看出「时段静音」", "静" in plain)

print("== period unmute T1 ==")
wd.messages = []
check("period unmute T1 --permanent 受理",
      wd.handle("period unmute T1 --permanent"), True)
check("按星期几那条被删掉",
      [m for m in sp.load_mutes()["mutes"] if m.get("weekday")], [])
check_true("消息说「已解除静音」", any("已解除静音" in m[2] for m in wd.messages))

print("== 分心看门狗：静音窗里不出声（daemon 侧，同一个表）==")
win = sp.mute_window_at(dt.datetime.combine(TODAY, dt.time(11, 30)))
check("11:30 在 T1 的静音窗里", win.get("period"), "T1")
check("13:30 不在", sp.mute_window_at(dt.datetime.combine(TODAY, dt.time(13, 30))), None)


# ---------------------------------------------------------------- 计划 vs 实际

print("== 计划 16:15 75min，实际 16:00 25 + 16:30 50 ==")
start = dt.datetime.combine(TODAY, dt.time(16, 15))
actual = [{"start": dt.datetime.combine(TODAY, dt.time(16, 0)), "duration": 25},
          {"start": dt.datetime.combine(TODAY, dt.time(16, 30)), "duration": 50}]
m = sch.match_slot(actual, start, 75)
check("判成 covered", m["kind"], "covered")
check_true("note 里写出实际两场", "16:00" in m["note"] and "16:30" in m["note"])
check("实际分钟数 = 75", m["actual_minutes"], 75)
check("同样两场对 50min 的计划也认账（16:30 那场就是它，只是挪了 15 分钟）",
      sch.match_slot(actual, dt.datetime.combine(TODAY, dt.time(16, 15)), 50)["kind"],
      "shifted")
check("挪了半格（09:30→09:45，同为 50min）",
      sch.match_slot([{"start": dt.datetime.combine(TODAY, dt.time(9, 45)),
                       "duration": 50}],
                     dt.datetime.combine(TODAY, dt.time(9, 30)), 50)["kind"], "shifted")
check("彻底没有 → none", sch.match_slot([], start, 75)["kind"], "none")
check("计划外场次能列出来",
      [r["start"].strftime("%H:%M") for r in sch.unmatched_actual(
          actual, [{"start": "16:15"}, {"start": "19:00"}])], ["16:00", "16:30"])

print("== daemon 认账之后，表格说「认账」不说「缺」==")
st = ks.load_state()
st["slots"] = dict(st.get("slots") or {})
st["slots"]["%sT16:15" % ISO] = {"status": "booked", "duration": "75", "attempts": 0,
                                "match": "covered",
                                "actual": "实际 16:00(25min) + 16:30(50min)（计划 16:15 75min）"}
ks.save_state(st)
wd._snapshot_cache.pop("__daemon_slots__", None)
wd.api_rows = []
wd.rows_source = "bridge"
wd.rebuild_rows()
row = [r for r in wd.rows if r["startLabel"] == "16:15"]
if not row:                       # 计划里没有 16:15 → 手动补一行来验显示逻辑
    pass
check("daemon_slots 读得到认账记录",
      (wd.daemon_slots().get("%sT16:15" % ISO) or {}).get("match"), "covered")

# ---------------------------------------------------------------- 位置：下架 / 当前 / 互换 / 全量
print("== task <id> -an / -ac：手动下架 + 挂到当前这一场 ==")


def plan_sessions(wd, day=None, drop=()):
    """把 data.csv 的契约造成 bridge.snapshot 的形状（权威数据，每行都有 id）。

    ``drop`` 里的开始时刻**故意不给 id** —— 模拟「契约有、API 没有」＝该订却没订上。
    """
    day = day or wd.sel_date
    out = []
    for slot in sch.day_slots(wd.entries, day):
        h, m = map(int, slot["start"].split(":"))
        st = dt.datetime.combine(day, dt.time(h, m))
        en = st + dt.timedelta(minutes=int(slot["duration"]))
        out.append({
            "startMs": int(st.timestamp() * 1000), "endMs": int(en.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": en.strftime("%H:%M"),
            "durationMin": int(slot["duration"]),
            "id": None if slot["start"] in drop else "sess-" + slot["start"],
            "title": "", "quiet": False, "matched": True,
        })
    return out


def fresh_warden():
    """干净的 warden：任务库清空、bridge 里没有历史 attachment。"""
    wd_ = mkwarden()
    wd_.offline = False                     # 要有「真」数据（session id 从 bridge 来）
    for old in wd_.task_store.list():
        wd_.task_store.delete(old["id"])
    wd_.bridge.completed = {}
    wd_.bridge.attached = {}
    wd_.bridge.titles = {}
    wd_.bridge.deleted = []
    wd_.reload_entries()                    # data.csv 得先读进来，plan_sessions 才有东西造
    wd_.bridge.sessions = plan_sessions(wd_)
    wd_.refresh()
    assert wd_.bridge.sessions, "替身里没有场次：data.csv 没读进来？"
    return wd_


print("== 补全 + 提示行：-an / -ac 能被 Tab 到，提示行认得每个动作 ==")
wd4 = fresh_warden()
_shell = wd4.shell
check("task 后面能 Tab 到紧凑写法",
      [c for c in _shell.complete("task ") if c.startswith("-")],
      ["-an", "-ac", "--assign", "-a", "--note", "-n", "--from", "-s"])
check("task 后面 Tab 补的是**面板序号**（1..N 连续，不是内部 id）",
      [c for c in _shell.complete("task ") if c.isdigit()],
      [str(i) for i in range(1, len(wd4.task_panel_order()) + 1)])
check("敲了 -a 之后 Tab 补的是紧凑写法",
      [c for c in _shell.complete("task -a") if c.startswith("-") and c != "-a"],
      ["-an", "-ac"])
check("-a 后面补「编号 + none + current」（不再混进一堆动作词）",
      _shell.complete("task -a "), ["1", "2", "3", "none", "current"])
check("-s 后面只补编号", _shell.complete("task -s "), ["1", "2", "3"])
check("-an / -ac 打完了：提示说不用再跟东西",
      "-an ≡ -a none" in _shell.hint_line("task -an"), True)
check("提示行认得「动作写在 ID 后面」",
      "互换位置" in _shell.hint_line("task 32 swap"), True)
check("裸 ID 后面提示它能干什么",
      "swap" in _shell.hint_line("task 32 ") and "下架" in _shell.hint_line("task 32 "), True)
# 回归：repl_kit 递给 hint 的是**整行**的词（含命令名），以前 _hint_task 没摘掉
# 「task」，于是每个动作的提示都是一句不相关的通用话（等于一直没生效）。
check("task import 的提示说的是剪贴板", "剪贴板" in _shell.hint_line("task import"), True)
check("task create 的提示说的是字段", "不给 id 就自动编号" in _shell.hint_line("task create"), True)
check("task note 的提示说的是备注", "备注" in _shell.hint_line("task note"), True)
check("task -s 的提示说的是批量", "全部任务" in _shell.hint_line("task -s 5 "), True)
check("refresh 的提示说的是对账", "--check" in _shell.hint_line("refresh "), True)

wd3 = fresh_warden()
idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
idx12 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "12:00"][0]
check("干净 warden 拉到了带 id 的场次",
      [r["id"] for r in wd3.rows if r["startLabel"] == "11:00"], ["sess-11:00"])

wd3.handle("task create title=要下架的 --assign %d" % idx11)
detached = [t for t in wd3.task_store.list() if t["fields"]["title"] == "要下架的"][0]
fm_id = detached["focusmate"]["task_id"]
wd3.bridge.calls = []
check("task <id> -an 受理", wd3.handle("task %d -an" % detached["id"]), True)
check("本地 session 断了", wd3.task_store.get(detached["id"])["session"], None)
check("Focusmate 那条 task 被删掉（不是只改本地）", wd3.bridge.deleted[-1], fm_id)
check("删的是它自己那条", [c for c in wd3.bridge.calls if c[0] == "delete_task"][0][1]["taskId"],
      fm_id)
check_true("说的是「下架」", any("下架" in m[2] for m in wd3.messages))
check("下架之后本地不再记着 Focusmate task id",
      (wd3.task_store.get(detached["id"]).get("focusmate") or {}).get("task_id"), None)

wd3.bridge.calls = []
check("task <id> -a none 是同一件事（长写法）", wd3.handle("task %d -a none" % detached["id"]), True)
check("本来就没挂：不再去删 Focusmate", [c for c in wd3.bridge.calls if c[0] == "delete_task"], [])
check_true("明说一句没挂", any("本来就没挂" in m[2] for m in wd3.messages))
check("task assign none 也认（显式动作写法）",
      wd3.handle("task assign %d none" % detached["id"]), True)

# --- 当前这一场
_saved_rows = wd3.rows
_now_ms = int(dt.datetime.now().timestamp() * 1000)


def _row(label, off_start, off_end):
    return {"startMs": _now_ms + off_start, "endMs": _now_ms + off_end,
            "startLabel": label, "date": ISO}


wd3.rows = [_row("X", -600000, -100000), _row("Y", -60000, 600000),
            _row("Z", 3600000, 7200000)]
check("current_session 优先「进行中」", wd3.current_session()[1]["startLabel"], "Y")
wd3.rows = [_row("X", -600000, -100000), _row("Z", 3600000, 7200000)]
check("没有进行中就取最近一场还没开始的", wd3.current_session()[1]["startLabel"], "Z")
wd3.rows = [_row("X", -600000, -100000)]
try:
    wd3.current_session()
except ValueError as e:
    check_true("一场都没有就说清楚", "没有正在进行的 session" in str(e))
else:
    check("一场都没有要抛错", False, True)
wd3.rows = _saved_rows

# 端到端：造一场「刚开始 5 分钟」的 session，`-ac` 应该挂到它上面
_live_start = dt.datetime.now() - dt.timedelta(minutes=5)
_live = {
    "startMs": int(_live_start.timestamp() * 1000),
    "endMs": int((_live_start + dt.timedelta(minutes=50)).timestamp() * 1000),
    "startLabel": _live_start.strftime("%H:%M"),
    "endLabel": (_live_start + dt.timedelta(minutes=50)).strftime("%H:%M"),
    "durationMin": 50, "id": "sess-live", "title": "", "quiet": False, "matched": True,
}
wd3.bridge.sessions = plan_sessions(wd3) + [_live]
wd3.refresh()
wd3.bridge.calls = []
check("task <id> -ac 受理", wd3.handle("task %d -ac" % detached["id"]), True)
check("挂到了正在进行的这一场",
      wd3.task_store.get(detached["id"])["session"]["start"],
      _live_start.strftime("%H:%M"))
check("bridge 收到的是这一场的 id",
      [c for c in wd3.bridge.calls if c[0] == "add_task"][0][1]["meetingId"], "sess-live")
check_true("说的是「当前这一场」", any("当前" in m[2] for m in wd3.messages))

wd3.bridge.sessions = plan_sessions(wd3)
wd3.refresh()

print("== task 32 swap 21：两条任务互换位置 ==")
wd3 = fresh_warden()
idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
idx12 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "12:00"][0]


def mk(title, idx=None):
    wd3.handle("task create title=%s%s" % (title, (" --assign %d" % idx) if idx else ""))
    return [t for t in wd3.task_store.list() if t["fields"]["title"] == title][0]


a = mk("甲", idx11)
b = mk("乙")
check("先是一条挂在 11:00、一条赋闲",
      (wd3.task_store.get(a["id"])["session"]["start"],
       wd3.task_store.get(b["id"])["session"]), ("11:00", None))
wd3.bridge.calls = []
check("task <id> swap <id> 受理", wd3.handle("task %d swap %d" % (a["id"], b["id"])), True)
check("原来那条下来了（赋闲）", wd3.task_store.get(a["id"])["session"], None)
check("赋闲那条进了 11:00",
      wd3.task_store.get(b["id"])["session"]["start"], "11:00")
check("旧场次上那条 Focusmate task 被删掉",
      wd3.bridge.deleted[-1], a["focusmate"]["task_id"])
check("新的一条挂到同一场",
      [c for c in wd3.bridge.calls if c[0] == "add_task"][-1][1]["meetingId"], "sess-11:00")
check_true("说明里点出「赋闲」", any("赋闲" in m[2] for m in wd3.messages))

# 两条都赋闲 → 没有位置可换（handle() 只回报「受理」，所以要直接看返回值）
c = mk("丙")
wd3.bridge.calls = []
check("两条都赋闲 → 返回两条 ID", wd3.cmd_task(["%d" % a["id"], "swap", "%d" % c["id"]]),
      [a["id"], c["id"]])
check("真的没动 bridge", wd3.bridge.calls, [])
check_true("明说「没有位置可换」",
           any("没有位置可换" in m[2] for m in wd3.messages))

# 同一场里的两条 = 没得换
d = mk("丁", idx11)
wd3.bridge.calls = []
check("同一场里的两条 → 受理", wd3.handle("task %d swap %d" % (b["id"], d["id"])), True)
check("同一场不用换（不动 bridge）", wd3.bridge.calls, [])
check_true("明说位置本来就一样", any("位置本来就一样" in m[2] for m in wd3.messages))

# 两条各在一场 → 场次对调
e = mk("戊", idx12)
fm_b = wd3.task_store.get(b["id"])["focusmate"]["task_id"]
fm_e = e["focusmate"]["task_id"]
wd3.bridge.calls = []
check("两条各在一场 → 受理", wd3.handle("task %d swap %d" % (b["id"], e["id"])), True)
check("对调之后乙在 12:00", wd3.task_store.get(b["id"])["session"]["start"], "12:00")
check("对调之后戊在 11:00", wd3.task_store.get(e["id"])["session"]["start"], "11:00")
check("两边旧 task 都删了", sorted(wd3.bridge.deleted[-2:]), sorted([fm_b, fm_e]))
check("两边都重新挂了一遍",
      sorted(c[1]["meetingId"] for c in wd3.bridge.calls if c[0] == "add_task"),
      ["sess-11:00", "sess-12:00"])
check("两边都换了新的 Focusmate task id",
      (wd3.task_store.get(b["id"])["focusmate"]["task_id"] != fm_b,
       wd3.task_store.get(e["id"])["focusmate"]["task_id"] != fm_e), (True, True))
check("swap 自己 → 受理但报错",
      wd3.handle("task %d swap %d" % (b["id"], b["id"])), True)
check_true("并且说清要两条不同的任务",
           any("两条不同的任务" in m[2] for m in wd3.messages))

print("== task -s 5 -a 6：把一场上的任务全量转移 ==")
wd3 = fresh_warden()
idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
idx12 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "12:00"][0]
one = mk("批量一", idx11)
two = mk("批量二", idx11)
three = mk("批量三", idx12)
check("11:00 上有两条、12:00 上有一条",
      ([t["id"] for t in wd3.task_store.tasks_for_session(ISO, "11:00")],
       [t["id"] for t in wd3.task_store.tasks_for_session(ISO, "12:00")]),
      ([one["id"], two["id"]], [three["id"]]))

wd3.bridge.calls = []
check("task -s N（没有 -a）：只看不动", wd3.cmd_task(["-s", "%d" % idx11]), True)
check("一条 bridge 都没调", wd3.bridge.calls, [])
check_true("列出了这一场上的两条",
           any("批量一" in m[2] for m in wd3.messages)
           and any("批量二" in m[2] for m in wd3.messages))
check_true("并提示怎么转移", any("全量转移用" in m[2] for m in wd3.messages))

wd3.bridge.calls = []
check("task -s 5 -a 6 受理", wd3.cmd_task(["-s", "%d" % idx11, "-a", "%d" % idx12]), True)
check("两条都到了 12:00",
      sorted(t["id"] for t in wd3.task_store.tasks_for_session(ISO, "12:00")),
      sorted([one["id"], two["id"], three["id"]]))
check("11:00 空了", wd3.task_store.tasks_for_session(ISO, "11:00"), [])
check("旧场次那两条 Focusmate task 都删了",
      sorted(wd3.bridge.deleted[-2:]), sorted([one["focusmate"]["task_id"],
                                               two["focusmate"]["task_id"]]))
check("新挂的都在目标场次",
      [c[1]["meetingId"] for c in wd3.bridge.calls if c[0] == "add_task"],
      ["sess-12:00", "sess-12:00"])

wd3.bridge.calls = []
check("task -s 同一场 -a 同一场 → 什么也不动",
      wd3.cmd_task(["-s", "%d" % idx12, "-a", "%d" % idx12]), False)
check("真的没动", wd3.bridge.calls, [])
check_true("明说源和目标是一场的", any("源和目标都是" in m[2] for m in wd3.messages))

check("task -s N -a none：全部下架",
      wd3.cmd_task(["-s", "%d" % idx12, "-a", "none"]), True)
check("12:00 上也空了", wd3.task_store.tasks_for_session(ISO, "12:00"), [])
check("三条都赋闲了",
      [t["session"] for t in wd3.task_store.list() if t["id"] in (one["id"], two["id"], three["id"])],
      [None, None, None])

empty = mk("空场", idx12)
wd3.handle("task %d -an" % empty["id"])          # 挂上又下架 → 这一场空了
wd3.bridge.calls = []
check("空的场次 → 明说没什么可办的",
      wd3.cmd_task(["-s", "%d" % idx12, "-a", "%d" % idx11]), False)
check_true("说的是没有任务", any("没有挂任务" in m[2] for m in wd3.messages))

check("task -s 后面再跟动作 → 受理但报错",
      wd3.handle("task -s %d -a %d done" % (idx11, idx12)), True)
check_true("提示 -s 是批量后缀", any("批量后缀" in m[2] for m in wd3.messages))
check("报错时没动任何东西", wd3.bridge.calls, [])

print("== 序号：面板上的第几条，从 1 到 N 连续（不是内部 id）==")
wd6 = fresh_warden()
for _ in range(5):
    wd6.handle("task create title=序号用")
nums = sorted(wd6.task_no(t) for t in wd6.task_store.list())
check("五条任务的序号正好是 1..5（内部 id 早就跳到几十了，也没关系）", nums, [1, 2, 3, 4, 5])
check("序号跟「面板顺序」是同一份口径",
      [wd6.task_no(t) for t in wd6.task_panel_order()], [1, 2, 3, 4, 5])
check("内部 id 确实已经不是 1..5 了（证明这两套数不是一回事）",
      [t["id"] for t in wd6.task_panel_order()] != [1, 2, 3, 4, 5], True)

# 删掉中间那条 → 后面的往前顶，还是 1..N 连续
victim = wd6.task_panel_order()[1]
wd6.handle("task delete %d" % wd6.task_no(victim))
check("删掉 #2 之后，剩下的还是 1..4 连续",
      sorted(wd6.task_no(t) for t in wd6.task_store.list()), [1, 2, 3, 4])
check("被删的那条真的没了",
      [t for t in wd6.task_store.list() if t["id"] == victim["id"]], [])

# 序号直接进命令：`task 2 drop`
target = wd6.task_panel_order()[1]
wd6.handle("task 2 drop")
check("task 2 drop 动的是「面板第 2 条」",
      ts.TaskStore.status_of(wd6.task_store.get(target["id"])), "dropped")
check("放弃之后它挪到「放弃」那一段（序号还是重排过的 1..N）",
      wd6.task_no(wd6.task_store.get(target["id"])),
      len(wd6.task_panel_order()))

# 面板外的任务：数字超过 N 时按内部 id 认，并明说一句
off = wd6.task_store.create({"title": "面板外的"})
# 很久以前就放弃掉了 → 今天面板上不该有它（status_at 也得挪到那天，别用「现在」）
wd6.task_store.mark_processed(off["id"], "2020-01-01", "09:00", "drop")
wd6.task_store.set_status(off["id"], ts.STATUS_DROPPED, at="2020-01-01T09:10:00")
check("它不在今天面板上", wd6.task_no(wd6.task_store.get(off["id"])), None)
wd6.messages = []
check("内部 id 当序号敲 → 受理（并说明是按 id 认的）",
      wd6.handle("task note %d 面板外的备注" % off["id"]), True)
check_true("说清是按内部 id 认的",
           any("按内部 id 认" in m[2] for m in wd6.messages))
check("备注也确实写到了那条上",
      wd6.task_store.get(off["id"])["fields"]["notes"], "面板外的备注")

# 显式写法：id:N 永远指内部 id（面板上有同号也照 id 走）
first = wd6.task_panel_order()[0]
wd6.messages = []
check("id:N 写法受理", wd6.handle("task note id:%d 显式备注" % first["id"]), True)
check("id:N 也认动作写在后面（task id:N done）",
      wd6.handle("task id:%d done" % wd6.task_store.create({"title": "临时"})["id"]), True)
check("id:N 写到了这一条上",
      wd6.task_store.get(first["id"])["fields"]["notes"], "显式备注")
check_true("id:N 不会再提示「按内部 id 认」（本来就是明说的）",
           not any("按内部 id 认" in m[2] for m in wd6.messages))

# 任务视图（视图 4）里两列都在：序号 + 内部 id
_view4 = [v for v in w.VIEWS if v.key == "4"][0](wd6).render()[0]
check("任务视图有「序号」列", [str(c.header) for c in _view4.columns][:2],
      ["序号", "id"])

print("== 任务面板排序：先状态分段（进行中/完成/放弃），段内按位置连号 ==")
wd3 = fresh_warden()
idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
idx12 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "12:00"][0]
late = mk("晚场任务", idx12)
early_b = mk("早场乙", idx11)
early_a = mk("早场甲", idx11)
idle_x = mk("赋闲甲")
idle_y = mk("赋闲乙")
order = wd3.task_panel_order()
check("五条都还在进行中（先看「待办」这一段）",
      [ts.TaskStore.status_of(t) for t in order], ["active"] * 5)
check("未挂 session 的排在最前面",
      [t["id"] for t in order[:2]], [idle_x["id"], idle_y["id"]])
check("接着是 11:00 那场的两条（按时间先后，不是按创建顺序）",
      [t["id"] for t in order[2:4]], sorted([early_a["id"], early_b["id"]]))
check("最后是 12:00 那场", [t["id"] for t in order[4:]], [late["id"]])
check("同一场的那几条挨在一起（连号，不是 32、15 混着）",
      [(t.get("session") or {}).get("start") for t in order],
      [None, None, "11:00", "11:00", "12:00"])
check("排序不改变「面板里有哪几条」这份口径",
      sorted(t["id"] for t in order), sorted(t["id"] for t in wd3.task_panel_tasks()))

# 状态是第一维：收工的一律挪到后面，不再跟待办混在一行里
wd3.task_store.set_status(early_a["id"], ts.STATUS_COMPLETED,
                          at=datetime.datetime.now().isoformat(timespec="seconds"))
wd3.task_store.drop(early_b["id"], reason="test")
order = wd3.task_panel_order()
check("三段顺序：进行中 → 完成 → 放弃",
      [ts.TaskStore.status_of(t) for t in order],
      ["active", "active", "active", "completed", "dropped"])
check("进行中那一段的前两条仍是未挂 session 的", 
      [t["id"] for t in order[:2]], [idle_x["id"], idle_y["id"]])
check("完成的单独一段（不再跟待办混着）", [t["id"] for t in order[3:4]],
      [early_a["id"]])
check("放弃的排在最后", [t["id"] for t in order[4:]], [early_b["id"]])
check("放弃的那条仍记得自己挂在哪一场（位置没丢）",
      (order[4].get("session") or {}).get("start"), "11:00")

# 渲染里真的按这个顺序摆 —— 把列数钉成 1 列，读出来就该等于 task_panel_order()
_orig_layout = wd3.task_index_layout
wd3.task_index_layout = lambda tasks, width=None: (1, len(tasks))
_panel = [v for v in w.VIEWS if v.key == "1"][0](wd3).render()[1]
_buf = io.StringIO()
w.Console(file=_buf, width=80, highlight=False, color_system=None).print(_panel)
wd3.task_index_layout = _orig_layout
check_true("面板还是那一张表（不是拆成好几张）", hasattr(_panel, "columns"))
check("还是 number/task 一对一对的", len(_panel.columns) % 2, 0)
check_true("标题里仍是图例（没被组名顶掉）", "Task · 任务" in str(_panel.title))
check_true("表尾仍是账目", "%d 条：" % len(wd3.task_panel_order()) in str(_panel.caption))
_text = _buf.getvalue()
_titles = [t["fields"]["title"] for t in wd3.task_panel_order()]
check("单列渲染时，表里的顺序就是 task_panel_order() 的顺序（排序真的进了渲染）",
      sorted(range(len(_titles)), key=lambda i: _text.index(_titles[i])),
      list(range(len(_titles))))

print("== 任务面板填法：一行一行填，一行里读过去编号是递增的 ==")


def panel_numbers(renderable):
    """把画出来的面板按「行」读：每行里那些「编号 + 记号标题」的编号。

    行的顺序 = 屏幕上的顺序。用来钉「一行一行填」这件事：按列填的话，
    一行读过去会是 15 / 39 / 42 / 28（用户反馈的「序号很乱」）。
    """
    buf = io.StringIO()
    w.Console(file=buf, width=200, highlight=False, color_system=None).print(renderable)
    out = []
    for line in buf.getvalue().splitlines():
        nums = [int(n) for n in re.findall(r"(?:^|\s)(\d+)\s+(?=[·✓✗⤼])", line)]
        if nums:
            out.append(nums)
    return out


_real_width = wd3.frame_width
wd3.frame_width = lambda: 160
_panel = [v for v in w.VIEWS if v.key == "1"][0](wd3).render()[1]
_cols, _rows = wd3.task_index_layout(wd3.task_panel_order())
wd3.frame_width = _real_width
_lines = panel_numbers(_panel)
_want = list(range(1, len(wd3.task_panel_order()) + 1))   # 面板序号就是 1..N
check("一行里有好几对（160 列摊开了）", max(len(x) for x in _lines) > 1, True)
check("第一行 = 序号 1..%d（按列填的话这里会是第 1、%d、%d 条，正是「序号很乱」）"
      % (_cols, _cols + 1, _cols * 2 + 1),
      _lines[0], _want[:_cols])
check("把行连起来读 = 1..N（一行一行填，不是一列一列填）",
      [n for line in _lines for n in line], _want)
check("序号是连续的 1..N，没有跳号",
      sorted(n for line in _lines for n in line), _want)
check("行数 = ceil(条数 / 并排的对数)", len(_lines), _rows)

print("== refresh：把屏幕上每块组件的状态都重新对一遍（只读外面世界）==")
wd3 = fresh_warden()
idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
kept = mk("refresh用", idx11)
fm_kept = kept["focusmate"]["task_id"]
wd3.bridge.calls = []
_spy = []
wd3.materialize_repeats = lambda: (_spy.append("repeats") or [])
wd3.writeback_completions = lambda: _spy.append("writeback")

# (1) 任务的完成状态：外面（Focusmate）说完成了 → refresh 之后本地跟着收工
wd3.bridge.completed[fm_kept] = datetime.datetime.now().isoformat(timespec="seconds")
check("refresh 受理", wd3.handle("refresh"), True)
check("refresh 去拉了 GET /v1/tasks（任务完成状态靠它，不用再手敲 task sync）",
      [c[0] for c in wd3.bridge.calls].count("tasks"), 1)
check("refresh 只在读：不落重复任务、不写回 OmniFocus（那是 task sync 的事）",
      _spy, [])
check("refresh 不在 Focusmate 上建/删 task",
      [c[0] for c in wd3.bridge.calls if c[0] in ("add_task", "delete_task")], [])
check("Focusmate 说完成了 → 本地任务跟着收工",
      ts.TaskStore.status_of(wd3.task_store.get(kept["id"])), "completed")
check_true("刚收工的那条进了归档",
           kept["id"] in [r.get("task_id")
                          for r in wd3.task_store.load_archive().get("records") or []])
check("画帧缓存被作废（不会停在旧画面上）", wd3.shell.frame_rev > 0, True)

# (2) 统计：已经加载过、且超过 TTL 才重算（没看过 / 没过期都别白拉网）
_stat_calls = []
wd3.cmd_stat = lambda *a, **k: (_stat_calls.append(1) or True)
wd3.stat = {"total_completed": 0, "days": 0}
wd3.stat_at = datetime.datetime.now()
wd3.handle("refresh")
check("统计没过期 → refresh 不重算", _stat_calls, [])
wd3.stat_at = datetime.datetime.now() - datetime.timedelta(
    seconds=w.st.CACHE_TTL + 60)
wd3.handle("refresh")
check("统计过期了 → refresh 顺手重算", len(_stat_calls), 1)
wd3.stat = None
wd3.handle("refresh")
check("统计从没加载过 → refresh 不替用户拉", len(_stat_calls), 1)

# (3) 各种「上次算出来先存着」的缓存都作废
wd3._snapshot_cache["随便"] = 1
wd3._log_tail_memo = ("sig", ["a"])
wd3.handle("refresh")
check("快照缓存被清掉", wd3._snapshot_cache, {})
check("日志尾巴缓存被清掉", wd3._log_tail_memo, None)

# (4) 收尾面板：正在问的那一轮，refresh 之后手里是最新的任务状态
#     （收尾进行中时命令行被收尾状态机接管，所以这里直接敲 cmd_refresh）
still = mk("收尾里的那条", idx11)
wd3.begin_review({"date": ISO, "start": "11:00"})
check_true("进了收尾（面板上有它）", wd3.review is not None)
check("收尾面板上现在问的就是那条",
      [t["id"] for t in wd3.review.tasks], [still["id"]])
wd3.task_store.drop(still["id"], reason="在外面处理掉了")
check("refresh 受理（收尾进行中，走实作那一层）", wd3.cmd_refresh([]), True)
check("refresh 之后收尾面板上的任务是最新的",
      ts.TaskStore.status_of(wd3.review.tasks[0]), "dropped")
check("重读不会把人问回去（当前问第几条不变）", wd3.review.index, 0)
wd3.review = None

# (5) 离线：不去碰外面（也不报错），界面照样重画
off = mkwarden()
off.bridge.calls = []
check("离线 refresh 照样受理", off.handle("refresh"), True)
check("离线不去拉 Focusmate 任务",
      [c for c in off.bridge.calls if c[0] == "tasks"], [])
check_true("离线 read 完照样有表格（退化快照）", off.rows or off.offline is True)

print("== refresh --check：对账（该订的 session / 该挂的 task）==")
wd3 = fresh_warden()
ran = []
_orig_run_ks = w.run_keep_session
w.run_keep_session = lambda *a, **k: (ran.append(list(a)) or (0, "check 跑完", ""))
try:
    idx11 = [i for i, r in enumerate(wd3.rows, 1) if r["startLabel"] == "11:00"][0]
    attached_task = mk("挂着的那条", idx11)
    fm = attached_task["focusmate"]["task_id"]
    check("先确认替身里它挂在 11:00 那场", wd3.bridge.attached.get(fm), "sess-11:00")

    # (1) 一切正常 → 什么都不该修
    wd3.bridge.calls = []
    check("没有问题时 --check 也受理", wd3.handle("refresh --check"), True)
    check("没有问题时不去调 check 下单", [r for r in ran], [])
    check("没有问题时不去重挂",
          [c for c in wd3.bridge.calls if c[0] == "add_task"], [])
    check_true("明确报「没有发现要修的」",
               any("没有发现要修的" in m[2] for m in wd3.messages))

    # (2) 本地说挂着，Focusmate 说挂到别处了 → 重挂
    wd3.bridge.attached[fm] = "sess-12:00"
    wd3.bridge.calls = []
    check("挂错场次时 --check 受理", wd3.handle("refresh --check"), True)
    check("认出来了：Focusmate 说没挂在这一场",
          any("没挂在这一场上" in m[2] for m in wd3.messages), True)
    check("重挂到正确的那一场",
          [c[1]["meetingId"] for c in wd3.bridge.calls if c[0] == "add_task"], ["sess-11:00"])
    check("本地记的 Focusmate task id 换成新的",
          wd3.task_store.get(attached_task["id"])["focusmate"]["task_id"] != fm, True)

    # (3) Focusmate 那边那条 task 已经被人删了 → 重挂
    fm2 = wd3.task_store.get(attached_task["id"])["focusmate"]["task_id"]
    wd3.bridge.attached.pop(fm2, None)
    wd3.bridge.calls = []
    check("task 不见了时 --check 受理", wd3.handle("refresh --check"), True)
    check_true("认出来了：那条 task 已经不在了",
               any("已经不在了" in m[2] for m in wd3.messages))
    check("也同样重挂了一遍",
          [c[1]["meetingId"] for c in wd3.bridge.calls if c[0] == "add_task"], ["sess-11:00"])

    # (4) 执行区里有契约、API 里没有 → 叫 daemon 补一轮
    #     只有执行区里的「缺」才值得补（草稿区本来就还没轮它），所以把区域判据
    #     钉成 execute：否则这条用例会随当前钟点变红。
    ran[:] = []
    wd3.bridge.sessions = plan_sessions(wd3, drop=("15:00",))
    wd3.bridge.calls = []
    _orig_zone = wd3.effective_zone
    wd3.effective_zone = lambda zone, day: "execute"
    try:
        check("该订没订上时 --check 受理", wd3.handle("refresh --check"), True)
    finally:
        wd3.effective_zone = _orig_zone
    check("叫 daemon 跑了一轮 check", [r[:1] for r in ran], [["check"]])
    check_true("先说清楚哪一场没订上",
               any("该订却没订上" in m[2] for m in wd3.messages))
    check_true("还报了场次编号", any("#" in m[2] and "15:00" in m[2] for m in wd3.messages))

    # (5) 离线：没外部世界可查，只重画
    wd3.bridge.sessions = plan_sessions(wd3)
    wd3.offline = True
    check("离线时 --check 只重画（返回 False）", wd3.cmd_refresh(["--check"]), False)
    check_true("离线时明说没有外部世界可查",
               any("离线模式" in m[2] and "外部世界" in m[2] for m in wd3.messages))
    wd3.offline = False
    ran[:] = []
    check("不带 --check 的 refresh 照样受理", wd3.handle("refresh"), True)
    check("refresh 自己不叫 check（只拉数）", ran, [])
finally:
    w.run_keep_session = _orig_run_ks
    wd3.bridge.sessions = plan_sessions(wd3)
    wd3.refresh(quiet=True)

print("== TUI 在场证明（daemon 靠它判断「人叫得回来吗」）==")
info = wd.write_tui_state()
check("写了 pid", info["pid"], os.getpid())
check_true("有 tty 字段（跑在管道时可能为空串）", "tty" in info)
import json as _json
on_disk = _json.load(open(ks.TUI_STATE_FILE, encoding="utf-8"))
check("文件里也是这个 pid", on_disk["pid"], os.getpid())
alive, seen = ks.tui_alive()
check("daemon 侧认得出 TUI 活着", alive, True)
wd.clear_tui_state()
check("退场时擦掉", os.path.exists(ks.TUI_STATE_FILE), False)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
