#!/usr/bin/env python3
"""count（25 分钟一个块）+ count_invested 的回归。

需求原话（用户）：

    count 字段和任务的持续时间字段关联，改 count = 改持续时间，改持续时间
    → count 自动 = 时间/25 往上取整；1 count = 25 分钟；count_invested 表示
    已经用了多少 count。``-c 5`` 设定、``-c +2`` 加进度（够数 = 完成、不许超）、
    ``-c -1`` 减；契约表格和任务面板显示 ``(1+2/5)``；session 结束之后 count
    **不**直接算进任务并完成 —— 够数的那条要在收尾里问「额外 count 还是直接完成」。

    python3 test/test_task_count.py     # 全绿退出 0，有失败退出 1

不联网、不起 bridge、不碰仓库里的真文件（任务库 / 归档 / 收尾状态全在临时目录）。
"""
import datetime
import importlib.util
import io
import os
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


def check_in(label, needle, haystack):
    check(label, needle in haystack, True)


TMP = tempfile.mkdtemp(prefix="task-count-test-")
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
os.environ["TI_FAILED_ACTIONS"] = os.path.join(TMP, "failed-actions.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, ks, psv = w.sp, w.sch, w.ts, w.ks, w.psv
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

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
sp.save_download({"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
                  "profiles": ["工作"], "events": [],
                  "periods": [{"id": "T1", "date": ISO, "start": "11:00",
                               "end": "13:00", "type": "工作", "name": "SAT",
                               "calendar": "工作", "title": "SAT"}]})


class FakeBridge(object):
    """桥替身：只回答「成功」，记下调用（count 的测试不关心网络细节）。"""

    def __init__(self):
        self.calls = []
        self.last_idle_close = None
        self.completed = {}

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "tasks":
            return {"tasks": []}
        if cmd == "complete_task":
            tid = str(kw.get("taskId") or "")
            self.completed[tid] = "2026-11-03T12:00:00Z"
            return {"verified": True, "completedAt": self.completed[tid], "taskId": tid}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "sessions": []}
        return {"verified": True, "taskId": "fm-1", "via": "api"}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmni(object):
    """OmniFocus 替身：记下每次写。"""

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def list_tasks(self, refresh=False):
        return []

    def repetition_info(self, pk):
        return {}

    def complete(self, pk, at=None, due=None, yes=True):
        self.calls.append(("complete", pk, due))
        return {"changed": ["completed"], "before": {"completed": False},
                "after": {"completed": True, "id": pk}}

    def drop(self, pk, at=None, all_occurrences=False, yes=True):
        self.calls.append(("drop", pk, all_occurrences))
        return {"changed": ["dropped"], "before": {"dropped": False},
                "after": {"dropped": True, "id": pk}}


def mkwarden(offline=False):
    """sync 模式的 warden（网络当场跑完）+ 假桥 + 假 OmniFocus。

    ``offline=False`` 是有意的：``-c`` 够数要验证「和 task done 走同一条路」——
    离线模式会跳过 Focusmate 那一勾，那就什么都验不出来了。
    """
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=offline,
                  net_async=False)
    wd.omnifocus = FakeOmni()
    for old in wd.task_store.list():
        wd.task_store.delete(old["id"])
    return wd


def seed_rows(wd):
    """铺一行 11:00（50 分钟）的 session，省掉真快照。"""
    wd.api_rows = []
    start = datetime.datetime.combine(wd.sel_date, datetime.time(11, 0))
    end = start + datetime.timedelta(minutes=50)
    wd.api_rows.append({
        "startMs": int(start.timestamp() * 1000),
        "endMs": int(end.timestamp() * 1000),
        "startLabel": "11:00", "endLabel": "11:50",
        "durationMin": 50, "id": "sess-11", "title": "", "quiet": False,
        "matched": False,
    })
    wd.rows_source = "bridge"
    wd.rebuild_rows()
    return wd.rows


# ---------------------------------------------------------------- 1. 换算
print("== 1. count ↔ duration 的换算（1 count = 25 分钟，往上取整）==")
check("25 分钟 = 1 count", ts.minutes_to_count(25), 1)
check("30 分钟 = 2 count（往上取整）", ts.minutes_to_count(30), 2)
check("50 分钟 = 2 count", ts.minutes_to_count(50), 2)
check("125 分钟 = 5 count", ts.minutes_to_count(125), 5)
check("5 count = 125 分钟", ts.count_to_minutes(5), 125)
check("认不出就是 None", ts.minutes_to_count(""), None)
check("count_text 没设上限 = 空", ts.count_text({"fields": {}}), "")
check("count_text 只有已投入", ts.count_text({"fields": {"count_invested": 2}}), "(2)")
check("收工/放弃 + 一格没记过 → 不显示 (0/1) 那种噪音",
      ts.count_text({"fields": {"count": 1}, "status": ts.STATUS_COMPLETED}), "")
check("收工但有进度的照样显示", 
      ts.count_text({"fields": {"count": 3, "count_invested": 2},
                     "status": ts.STATUS_COMPLETED}), "(2/3)")
check("count_text 已投入/上限", ts.count_text({"fields": {"count": 5, "count_invested": 1}}),
      "(1/5)")
check("count_text 带这一场的涨幅",
      ts.count_text({"fields": {"count": 5, "count_invested": 1}}, delta=2), "(1+2/5)")
check("默认 1 count、零进度 → 不显示噪音",
      ts.count_text({"fields": {"count": 1}}, delta=1), "")
check("默认 1 count、零进度但这场报 +2 → 也不显示",
      ts.count_text({"fields": {"count": 1}}, delta=2), "")
check("多个 count、零进度 → 保留（0+2/5 是有用的目标信息）",
      ts.count_text({"fields": {"count": 5}}, delta=2), "(0+2/5)")
check("有进度就显示",
      ts.count_text({"fields": {"count": 1, "count_invested": 1}}, delta=0), "(1/1)")

wd = mkwarden()
store = wd.task_store

# ---------------------------------------------------------------- 2. 设定
print("\n== 2. 改 count = 改时长；改时长 = 改 count ==")
a = store.create(fields={"title": "写论文", "duration": "30"})
check("建的时候写了 30 分钟 → count = 2", a["fields"].get("count"), 2)
check("时长原样留着（30 不是 50）", a["fields"].get("duration"), "30")
b = store.create(fields={"title": "闪卡", "count": "3"})
check("建的时候写了 count=3 → duration = 75", b["fields"].get("duration"), 75)

a = store.modify(a["id"], "duration", "100")
check("task modify duration 100 → count = 4", a["fields"].get("count"), 4)
check("时长就是用户说的 100", ts.parse_minutes(a["fields"].get("duration")), 100)
a = store.modify(a["id"], "count", "5")
check("task modify count 5 → duration = 125", a["fields"].get("duration"), 125)
check("count 就是 5", ts.task_count(store.get(a["id"])), 5)

# ---------------------------------------------------------------- 3. -c
print("\n== 3. `-c` 后缀：5 设定 / +2 加 / -1 减 ==")
wd.handle("task %s -c 2" % wd.task_no(store.get(a["id"])))
check("-c 2 设定最高 count", ts.task_count(store.get(a["id"])), 2)
check("-c 2 顺手把时长改成 50", store.get(a["id"])["fields"].get("duration"), 50)
wd.handle("task %s -c +1" % wd.task_no(store.get(a["id"])))
check("-c +1 进度 = 1", ts.task_count_invested(store.get(a["id"])), 1)
check("加完还没到上限：状态仍是进行中",
      store.get(a["id"])["status"], ts.STATUS_ACTIVE)
wd.handle("task %s -c -1" % wd.task_no(store.get(a["id"])))
check("-c -1 退回 0", ts.task_count(store.get(a["id"])) and
      ts.task_count_invested(store.get(a["id"])), 0)

print("\n-- 加过头的会被顶在上限、并且等同于完成 --")
c = store.create(fields={"title": "数学易错修正", "count": "2"})
store.attach_focusmate(c["id"], {"task_id": "ft-count-1"})
wd.handle("task %s -c +5" % wd.task_no(store.get(c["id"])))
c = store.get(c["id"])
check("进度被顶在最高 count（不会到 5）", ts.task_count_invested(c), 2)
check("-c +N 够数 = 完成任务", c["status"], ts.STATUS_COMPLETED)
check_true("走的是 done 那条路：Focusmate 勾了",
           any(cmd == "complete_task" for cmd, _ in wd.bridge.calls))
check_true("完成也归档了",
           any(r.get("task_id") == c["id"]
               for r in store.load_archive().get("records") or []))

print("\n-- 设定 count 之后进度不许超过新的上限 --")
d = store.create(fields={"title": "查文献", "count": "5"})
store.add_count(d["id"], 4)
d, _before, after = store.set_count(d["id"], 2)
check("上限降到 2", after, 2)
check("进度被压到 2", ts.task_count_invested(store.get(d["id"])), 2)

print("\n-- set_count_plan：一次写清「已投入 + 还需」，旧上限不会夹住进度 --")
plan = store.create(fields={"title": "延长排期", "count": "1"})
store.add_count(plan["id"], 1)
plan = store.set_count_plan(plan["id"], 4, 3)
check("上限 = 已投入 4 + 还需 3 = 7", ts.task_count(plan), 7)
check("已投入校准为 4（没被旧上限 1 夹住）", ts.task_count_invested(plan), 4)
check("时长跟着 count = 175 分钟", plan["fields"].get("duration"), 175)
plan = store.set_count_plan(plan["id"], 0, 2)
check("已投入可以报 0", ts.task_count_invested(plan), 0)
check("只剩还需 2", ts.task_count(plan), 2)
try:
    store.set_count_plan(plan["id"], -1, 3)
    check("负的已投入要报错", False, True)
except ValueError:
    check("负的已投入要报错", True, True)
try:
    store.set_count_plan(plan["id"], 1, 0)
    check("还需 0 个要报错", False, True)
except ValueError:
    check("还需 0 个要报错", True, True)

# ---------------------------------------------------------------- 4. 表格
print("\n== 4. 契约表格 / 任务面板里的 (1+2/5) ==")
for old in store.list():
    store.delete(old["id"])
rows = seed_rows(wd)
e = store.create(fields={"title": "数学易错修正", "count": "5"})
store.assign(e["id"], {"date": ISO, "start": "11:00", "duration": 50})
store.add_count(e["id"], 1)
wd.rebuild_rows()
row = [r for r in wd.rows if r.get("startLabel") == "11:00"][0]
check_in("session 表格里是 (1+2/5)（1 已投入 + 2 = 50min/25）",
         "(1+2/5)", row.get("task_text") or "")
check_in("任务面板里也一样", "(1+2/5)",
         wd.task_index_title(store.get(e["id"])))
check("挂在场上的任务：这一场会涨 2 个 count", wd.task_count_delta(store.get(e["id"])), 2)
loose = store.create(fields={"title": "没挂 session 的", "count": "2"})
check("没挂 session 的任务不预报涨幅", wd.task_count_delta(store.get(loose["id"])), 0)
nolo = store.create(fields={"title": "没有 count"})
check("没设 count 的任务不显示这一格", wd.task_count_label(store.get(nolo["id"])), "")

print("\n-- 项目任务拆成多个 session part：显示原任务总 count + 跨 part 累计 --")
part1 = store.create(fields={"title": "assignmentbook chap 5", "count": "1",
                             "count_total": 3, "project_id": 1,
                             "project_task": "pk-assignmentbook"})
store.assign(part1["id"], {"date": ISO, "start": "11:00", "duration": 25})
check_in("第一段：原任务 3 count，这一场 +1 → (0+1/3)",
         "(0+1/3)", wd.task_cell_text([store.get(part1["id"])], 25))
part1 = store.set_status(part1["id"], ts.STATUS_COMPLETED, reason="test-part")
check_in("第一段完成后，历史 session 行仍是「开始前 0 + 这一场 1」",
         "(0+1/3)", wd.task_cell_text([store.get(part1["id"])], 25))
part2 = store.create(fields={"title": "assignmentbook chap 5", "count": "2",
                             "count_total": 3, "project_id": 1,
                             "project_task": "pk-assignmentbook"})
store.assign(part2["id"], {"date": ISO, "start": "12:00", "duration": 50})
check_in("第一段完成后，第二段：已投入 1 + 这一场 2 / 总 3",
         "(1+2/3)", wd.task_cell_text([store.get(part2["id"])], 50))
check_in("任务面板同样显示跨 part 累计",
         "(1+2/3)", wd.task_index_title(store.get(part2["id"])))
check("count_text 允许显式覆盖总上限和已投入",
      ts.count_text(store.get(part2["id"]), 2, cap=3, invested=1), "(1+2/3)")

# 模拟第二场收尾也记了 2 个 count：两条历史 session 行仍保持各自的
# 「这一场开始前 + 这一场」快照，不拿现在总共 3/3 去回填第一行。
store.credit_count(part2["id"], psv.session_key(ISO, "12:00"), 2)
part2 = store.set_status(part2["id"], ts.STATUS_COMPLETED, reason="test-part2")
check_in("第二段完成后，第一段历史行仍是 (0+1/3)",
         "(0+1/3)", wd.task_cell_text([store.get(part1["id"])], 25))
check_in("第二段历史行是 (1+2/3)",
         "(1+2/3)", wd.task_cell_text([store.get(part2["id"])], 50))
check_in("任务面板显示项目任务当前总进度 3/3",
         "(3/3)", wd.task_index_title(store.get(part2["id"])))
store.delete(part1["id"])
store.delete(part2["id"])

print("\n-- 默认 (0+1/1) 不在 session 表格 / 下方任务栏出现 --")
plain = store.create(fields={"title": "flashcard review", "duration": "25"})
store.assign(plain["id"], {"date": ISO, "start": "11:00", "duration": 50})
plain = store.get(plain["id"])
check("session 表格里没有 (0+2/1)", "(0+2/1)" in wd.task_cell_text([plain], 50), False)
check("任务栏里没有 (0+2/1)", "(0+2/1)" in wd.task_index_title(plain), False)
check("这两处的 count 标记都为空",
      (wd.task_count_label(plain), wd.task_count_label(plain, 0)), ("", ""))
store.delete(plain["id"])

# ---------------------------------------------------------------- 5. 收尾
print("\n== 5. session 结束：count 记进账，但够数的那条要问过人才完成 ==")
e = store.get(e["id"])
entry = {"key": psv.session_key(datetime.date.fromisoformat(ISO), "11:00"),
         "date": ISO, "start": "11:00", "end": "11:50", "duration": 50}
review = wd.begin_review(entry, sync=False)
check_true("收尾开起来了", review is not None)
e = store.get(e["id"])
check("这一场给 +2 → 进度 3", ts.task_count_invested(e), 3)
check("**没有**自动完成（还在进行中）", e["status"], ts.STATUS_ACTIVE)
check("收尾面板上认得它够数了吗… 3/5 还没够", review.at_cap(e), False)
body = "\n".join(text for text, _style in review.lines())
check_in("收尾面板上看得见 count", "count", body)

# 再来一场，正好够数
entry2 = {"key": psv.session_key(datetime.date.fromisoformat(ISO), "12:00"),
          "date": ISO, "start": "11:00", "end": "11:50", "duration": 50}
wd.close_review(note="test")
review = wd.begin_review(entry2, sync=False)
e = store.get(e["id"])
check("第二场之后 5/5", (ts.task_count_invested(e), ts.task_count(e)), (5, 5))
check("够数了但**仍然**没自动完成", e["status"], ts.STATUS_ACTIVE)
check_true("收尾认得它够数", review.at_cap(store.get(e["id"])))
body = "\n".join(text for text, _style in review.lines())
check_in("面板上直接给出 f 直接完成", "f 直接完成", body)

print("\n-- 收尾里敲 f：直接完成（勾 Focusmate + 写回 OmniFocus）--")
verdict = review.handle("f")
check("f 推进到「收掉了」", (verdict or {}).get("action"), "close")
e = store.get(e["id"])
check("f 之后任务完成", e["status"], ts.STATUS_COMPLETED)
check_true("f 勾了 Focusmate",
           any(cmd == "complete_task" for cmd, _ in wd.bridge.calls))

print("\n-- 「额外 count」= 抬高上限，任务留着继续干 --")
for old in store.list():
    store.delete(old["id"])
f = store.create(fields={"title": "读论文", "count": "1"})
store.assign(f["id"], {"date": ISO, "start": "11:00", "duration": 50})
wd.begin_review(dict(entry), sync=False)
f = store.get(f["id"])
check("1 + 2 → 顶在 1/1", (ts.task_count_invested(f), ts.task_count(f)), (1, 1))
check("够数也不自动完成", f["status"], ts.STATUS_ACTIVE)
review = wd.review
verdict = review.handle("f 2")
check("额外 count 之后还在进行中", store.get(f["id"])["status"], ts.STATUS_ACTIVE)
f = store.get(f["id"])
check("上限抬到「已投入 + 2」= 3", ts.task_count(f), 3)
check("已投入还是 1", ts.task_count_invested(f), 1)
check("时长跟着上限走 = 75", f["fields"].get("duration"), 75)

check("记过账的那一场不再预报涨幅", wd.task_count_delta(store.get(f["id"])), 0)
check("面板上也就不再写 (1+2/3)：只有 1/3",
      ts.count_text(store.get(f["id"]), wd.task_count_delta(store.get(f["id"]))), "(1/3)")

print("\n-- 同一场收尾重开：count 不会记第二次 --")
wd.close_review(note="test2")
again = wd.begin_review(dict(entry), sync=False)
g = store.get(f["id"])
check("重开收尾不加账", ts.task_count_invested(g), 1)
check_true("重开之后表格里还是 1/3", (again is not None))

print("\n-- 幂等也认「直接重放 credit」这一层 --")
out = store.credit_count(f["id"], entry["key"], 2)
check("同一个 key 再记一次返回 None", out, None)
check("进度没变", ts.task_count_invested(store.get(f["id"])), 1)

# ---------------------------------------------------------------- 6. 命令面
print("\n== 6. 命令表 / 缩略选项都认 -c ==")
check_true("-c 在短选项表里（-c ≡ --count）",
           any(flag.token == "-c" and "--count" in flag.expands
               for flag in w.TASK_SHORT_FLAGS))
check_true("补全能补 -c 的取值",
           "+2" in w.task_completer(wd, ["task", "1", "-c"]))
spec_tokens, kept = w.Warden._split_count(["-c", "-1", "1"])
check("-c -1 里的 -1 是值不是 flag", (spec_tokens, kept), ("-1", ["1"]))
try:
    w.Warden._split_count(["-c"])
    check("裸 -c 要报错", "没报错", "ValueError")
except ValueError:
    check("裸 -c 要报错", True, True)

# ---------------------------------------------------------------- 收尾
if fails:
    print("\n%d 条失败：" % len(fails))
    for one in fails:
        print("  - %s" % one)
    raise SystemExit(1)
print("\n全部通过")
