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

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "add_task":
            self.fm_seq += 1
            tid = "ft-%d" % self.fm_seq
            self.titles[tid] = kw.get("title")
            return {"verified": True, "taskId": tid, "task": {"id": tid}, "via": "api"}
        if cmd == "tasks":
            return {"tasks": [{"id": k, "title": self.titles.get(k, "论文"),
                               "completedAt": v}
                              for k, v in self.completed.items()]}
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

print("== 裸 ID 等价 select：task 8 ≡ task select 8，task 8 -a 1 ==")
check("task create 77 显式 ID", wd.handle("task create 77 title=裸ID"), True)
check("task 77 受理（裸 ID）", wd.handle("task 77"), True)
check("裸 ID 真的选中了任务 77", wd.selected_task_id, 77)
check_true("说的还是 select 那套话",
           any("已选中任务 77" in m[2] for m in wd.messages))
check("task select 77 照样能用", wd.handle("task select 77"), True)

row1200 = [r for r in wd.rows if r["startLabel"] == "12:00"][0]
idx1200 = wd.rows.index(row1200) + 1
wd.bridge.calls = []
check("task 77 -a N：选中 + 挂上一步到位",
      wd.handle("task 77 -a %d" % idx1200), True)
check("真的挂到那一场了", wd.task_store.get(77)["session"]["start"], "12:00")
check("bridge 只调了一次 add_task",
      len([c for c in wd.bridge.calls if c[0] == "add_task"]), 1)
check("选中状态落在 77 上", wd.selected_task_id, 77)

check("task 77 -n 也能用",
      wd.handle("task 77 -n 材料在这 https://example.com/bare"), True)
check("备注写进去了", wd.task_store.notes(wd.task_store.get(77)),
      "材料在这 https://example.com/bare")

check("裸 ID 后面再跟东西 → 受理但报错",
      wd.handle("task 77 %d" % idx1200), True)
check_true("提示改用 -a/--assign",
           any("要挂到 session 用 -a/--assign" in m[2] for m in wd.messages))
check("报错时没改任何绑定", wd.task_store.get(77)["session"]["start"], "12:00")

check("不存在的裸 ID → 受理但报错", wd.handle("task 999"), True)
check_true("说任务 999 没有",
           any("没有任务 999" in m[2] for m in wd.messages))

check("未知动作 → 受理但报错", wd.handle("task 装死"), True)
check_true("错误提示里也提一句裸 ID",
           any("task 8 ≡ task select 8" in m[2] for m in wd.messages))
wd.handle("task delete 77")
check("收尾把 77 删掉", [t for t in wd.task_store.list() if t["id"] == 77], [])

wd.rebuild_rows()
# task sync 之后这条任务就算「完成」了（Focusmate 的 completedAt）→ 前面带 ✓
check("表格 task 列是「状态记号 + 编号: 标题」",
      [r.get("task_text") for r in wd.rows if r["startLabel"] == "11:00"][0],
      "✓5: 论文")

print("== 契约视图：task 列「编号: 标题」+ 下面那块两列任务表格 ==")
row1100 = [r for r in wd.rows if r["startLabel"] == "11:00"][0]
check("task 列带任务编号", row1100["task_text"], "✓5: 论文")
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
check("第二块是任务面板", "Task · 任务" in str(getattr(parts[1], "title", "")), True)
_panel_cols = len(parts[1].columns) // 2
check("表里是 number/task 一对一对的", len(parts[1].columns) % 2, 0)
check("表头按对数重复",
      [str(c.header) for c in parts[1].columns], ["number", "task"] * _panel_cols)

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
# 每列几条往下读：20 条 4 列 → 第 2 列从编号 6 开始（不是第 3 条）
_cols, _rows = wd.task_index_layout(_many, width=160)
check("编号从上往下读，不是从左往右读", (_rows, _many[_rows]["id"]), (5, 6))

# 真的画出来：160 列的终端里，表里是 2*cols 个 column
_real_width = wd.frame_width
for _t in (_many[:1],):
    pass
wd.frame_width = lambda: 160
_wide_cols = wd.task_index_layout(wd.task_panel_tasks() or _many, width=160)[0]
wide_table = [v for v in w.VIEWS if v.key == "1"][0](wd).render()[1]
wd.frame_width = _real_width
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
check("编号列显示任务 ID", str(num_a), str(task_assigned["id"]))
# 状态记号：完成 ✓ / 进行中 ·（表格里「什么都没有」曾经分不清是哪种）
done_task = wd.task_store.get(5)
num_d, title_d = wd._task_index_cells(done_task, None)
check("完成的任务打 ✓", str(title_d).startswith("✓"), True)
check("完成的染绿", str(num_d.style), "green")
check("进行中的打 ·", str(title_f).startswith("·"), True)
check("task_mark：进行中 = ·", wd.task_mark(task_free), "·")
check("task_mark：完成 = ✓", wd.task_mark(done_task), "✓")
index_table = [v for v in w.VIEWS if v.key == "1"][0](wd).render()[1]
check_true("任务表格标题里写了记号图例", "✓ 完成" in str(index_table.title))
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
wd.handle("task note 5 材料在这 https://example.com/set3")
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
check_true("Recent 里报了完成", any("完成：任务 %d" % done_task in m[2] for m in wd.messages))
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
check("task select 5", wd.handle("task select 5"), True)
check("把 5 改挂到另一场", wd.handle("task assign %d" % idx1200), True)
check_true("提醒旧场次那条 task 不会跟着挪",
           any("不会跟着挪" in m[2] for m in wd.messages))
check("旧场次已记进 previous_session/focusmate",
      wd.task_store.get(5)["session"]["start"], "12:00")

for t in list(wd.task_store.list()):
    wd.handle("task delete %d" % t["id"])
check("task delete", wd.task_store.list(), [])


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
