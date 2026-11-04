#!/usr/bin/env python3
"""项目（OmniFocus Project）+ plan（资源分配 → 唤起任务 → 自动排期）回归。"""
import datetime
import importlib.util
import io
import os
import re
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


TMP = tempfile.mkdtemp(prefix="project-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
# 静音表也要钉住：真实 temp/session-mutes.json 里有「周四 T1 09:30–12:30 永久静音」，
# 用到例跑在周三晚上、看的是「明天」（周四）时，T1 那两场会被当成静音 → 空场凭空少两场。
os.environ["TI_SESSION_MUTES"] = os.path.join(TMP, "mutes.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
# 收尾 / 到点没开始的状态文件也要钉住：真实的 temp/keep_session-nostart.json 里
# 可能正躺着 daemon 刚入队的提问，warden 一开就会把它顶上来抢走 `plan` 的输入。
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

sp, sch, ts, ks, of = w.sp, w.sch, w.ts, w.ks, w.of
pj, pp = w.pj, w.pp
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
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()
ISO = TODAY.isoformat()
data = {"version": 1, "generated_at": "x", "start_date": ISO, "days": 8,
        "profiles": ["工作", "空闲"], "events": [],
        "periods": [
            {"id": "T1", "date": ISO, "start": "11:00", "end": "13:00",
             "type": "工作", "name": "SAT 准备", "calendar": "工作", "title": "SAT 准备"},
            {"id": "T2", "date": ISO, "start": "15:00", "end": "16:00",
             "type": "工作", "name": "托福 准备", "calendar": "工作", "title": "托福 准备"},
            # 明天也排两个时段：`plan` 只认「还没开始」的场次，今天这几场在真实
            # 时钟下多半已经过去了（用例不能按墙上时间挑那一天）。
            {"id": "T1", "date": TOMORROW, "start": "11:00", "end": "13:00",
             "type": "工作", "name": "SAT 准备", "calendar": "工作", "title": "SAT 准备"},
            {"id": "T2", "date": TOMORROW, "start": "15:00", "end": "16:00",
             "type": "工作", "name": "托福 准备", "calendar": "工作", "title": "托福 准备"},
        ]}
sp.save_download(data)


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.fm_seq = 0
        self.titles = {}
        self.attached = {}

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
            self.attached.pop(tid, None)
            return {"verified": True, "taskId": tid, "deleted": {"id": tid}}
        if cmd == "tasks":
            return {"tasks": [{"id": tid, "title": self.titles.get(tid, ""),
                               "completedAt": None,
                               "attachments": [{"type": "session", "externalId": mid}]}
                              for tid, mid in list(self.attached.items())]}
        if cmd == "release":
            return {"released": True}
        if cmd == "snapshot":
            return {"date": kw.get("date"),
                    "now": int(datetime.datetime.now().timestamp() * 1000),
                    "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


ONROWS = [
    {"id": "p-lunwen", "name": "论文", "kind": "project_root", "project": "",
     "completed": False, "dropped": False},
    {"id": "t-intro", "name": "写引言", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 75, "completed": False,
     "due": "%sT11:50:00" % ISO},
    {"id": "t-data", "name": "跑数据", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 100, "completed": False},
    {"id": "t-read", "name": "读文献", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 50, "completed": False},
    {"id": "t-done", "name": "写完的", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 25, "completed": True},
    {"id": "p-dushu", "name": "读书", "kind": "project_root", "project": "",
     "completed": False, "dropped": False},
    {"id": "t-book", "name": "读一章", "kind": "action", "project": "读书",
     "parent": "读书", "estimated_minutes": 50, "completed": False},
    {"id": "t-more", "name": "读书笔记", "kind": "action", "project": "读书",
     "parent": "读书", "estimated_minutes": 75, "completed": False},
    {"id": "t-bad", "name": "没设时长的", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 0, "completed": False},
]


class FakeOmniFocus(object):
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [dict(r) for r in ONROWS]
        self.calls = []

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None):
        self.calls.append(("list", refresh))
        return [dict(r) for r in self.rows]

    def get(self, pk, refresh=False):
        for row in self.rows:
            if str(row.get("id")) == str(pk):
                return dict(row)
        return None

    def repetition_info(self, pk, timeout=None):
        return {"repeated": False}

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        return {"after": {"completed": True}}

    def drop(self, pk, at=None, all_occurrences=False, yes=True, timeout=None):
        return {"after": {"dropped": True}}

    def invalidate(self):
        pass


def mkwarden(rows=None):
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus(rows)
    return wd


def reset_tasks():
    """任务库是同一个文件：上一段排进 session 的任务会让那几场不再「空着」。"""
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])


def reset_projects():
    """整份清空（连 next_id 一起）—— 只删记录的话序号会一路涨，用例就对不上 #1/#2。"""
    pj.save(pj.blank())


def fresh(rows=None):
    """干净的任务库 + 项目库 + 一个新的 warden（各段互不串味）。"""
    reset_tasks()
    reset_projects()
    return mkwarden(rows)


def by_num(wd, num=1):
    return wd.projects.active()[num - 1]


def plan_day(wd):
    """切到明天再 plan（今天那几场在真实时钟下可能已经开始了）。

    排布顺手做成 T1 75 25 + T2 50 —— 空场 = 3 + 1 + 2 = 6 个 count（三场），
    正好让「一场一个任务」的排法装得下。
    """
    wd.handle("day 1")
    wd.handle("session T1 75 25")
    wd.handle("session T2 50")
    book_all(wd)
    return wd


def book_all(wd):
    """把这张表上的每场 session 都当成「已经订上」（plan 只认订上的空场）。"""
    plan = sch.day_slots(wd.entries, wd.sel_date)
    wd.api_rows = []
    for slot in plan:
        h, m = map(int, slot["start"].split(":"))
        st = datetime.datetime.combine(wd.sel_date, datetime.time(h, m))
        en = st + datetime.timedelta(minutes=int(slot["duration"]))
        wd.api_rows.append({
            "startMs": int(st.timestamp() * 1000), "endMs": int(en.timestamp() * 1000),
            "startLabel": slot["start"], "endLabel": en.strftime("%H:%M"),
            "durationMin": int(slot["duration"]), "id": "sess-" + slot["start"],
            "title": "", "quiet": False, "matched": False,
            "phase": wd.phase_of(int(st.timestamp() * 1000),
                                 int(en.timestamp() * 1000),
                                 int(datetime.datetime.now().timestamp() * 1000)),
        })
    wd.rows_source = "bridge"
    wd.rebuild_rows()


def texts(wd):
    return [m[2] for m in wd.messages]


print("== 链接：项目链接也认（omnifocus:///project/PK） ==")
item = of.parse_link("omnifocus:///project/p-lunwen")
check("project 链接的 id", item["id"], "p-lunwen")
check("project 链接的 kind", item["kind"], "project")
check("project 链接的 url", item["url"], "omnifocus:///project/p-lunwen")
check("任务链接照旧是 task", of.parse_link("[甲](omnifocus:///task/t-1)")["kind"], "task")

print("== project_payload：时长校验 / 归组 / count ==")
payload = pj.project_payload(ONROWS[0], ONROWS)
check("项目名", payload["title"], "论文")
check("缺时长的被点名", payload["missing"], ["没设时长的"])
check("成员里没有别的项目的任务",
      sorted(t["title"] for t in payload["tasks"]),
      sorted(["写引言", "跑数据", "读文献", "写完的", "没设时长的"]))
check("75 分钟 = 3 count",
      [t["count"] for t in payload["tasks"] if t["pk"] == "t-intro"], [3])
check("100 分钟 = 4 count（往上取整）",
      [t["count"] for t in payload["tasks"] if t["pk"] == "t-data"], [4])
check_true("拒绝的话里说清是哪几条",
           "「没设时长的」" in pj.format_missing(payload))

print("== task import：时长不齐的项目被拒，一个字节都不落 ==")
wd = fresh()
check("import 受理", wd.handle("task import omnifocus:///project/p-lunwen"), True)
check("项目库还是空的", wd.projects.list(), [])
check("任务库也是空的", wd.task_store.list(), [])
check_true("说了「拒绝」", any("这次导入被拒" in t for t in texts(wd)))
check_true("点名了缺时长的那条", any("没设时长的" in t for t in texts(wd)))
check("记成了出过错", wd.had_error, True)

print("== task import：项目链接但 OmniFocus 没开 → 拒绝（不建「叫项目名的任务」） ==")
wd_off = mkwarden()
wd_off.settings.set("omnifocus", "off")
check("import 受理", wd_off.handle("task import omnifocus:///project/p-lunwen"), True)
check("项目库还是空的", wd_off.projects.list(), [])
check("任务库也没被污染", wd_off.task_store.list(), [])
check_true("说清是「项目链接 + 读不到」",
           any("项目" in t and "读不到" in t for t in texts(wd_off)))
wd_off.settings.set("omnifocus", "on")

print("== task import：时长齐了就进项目库（任务库一条都不建） ==")
rows_ok = [r for r in ONROWS if r["id"] != "t-bad"]
wd = fresh(rows_ok)
check("import 受理", wd.handle("task import omnifocus:///project/p-lunwen"), True)
projects = wd.projects.list()
check("项目进库了", len(projects), 1)
check("项目名", projects[0]["title"], "论文")
check("项目挂的是 OmniFocus 项目 pk",
      projects[0]["omnifocus"]["id"], "p-lunwen")
check("清单里 4 条任务",
      [t["title"] for t in projects[0]["tasks"]],
      ["写引言", "跑数据", "读文献", "写完的"])
check("**没有**建本地任务（没被唤起就不展示）", wd.task_store.list(), [])
check("项目序号是 1", wd.projects.number_of(projects[0]["id"]), 1)
check_true("说了「先躺着不占 session」",
           any("先躺着" in t for t in texts(wd)))

stat = pj.stats(projects[0], wd.task_store)
check("总 count = 3+4+2+1", stat["total"], 10)
check("已完成的那条算满", stat["done"], 1)
check("剩余 = 9", stat["remaining"], 9)
check("进度文字", stat["progress_text"], "1/10")

print("== task import <项目> -a N：不去挂「上一次选中的任务」 ==")
wd_s = fresh(rows_ok)
wd_s.handle("task create title=别的活")
wd_s.handle("session T1 25")
book_all(wd_s)
check("带 -a 的 import 受理",
      wd_s.handle("task import omnifocus:///project/p-lunwen -a 1"), True)
check("别的活没被顺手挂上",
      (wd_s.task_store.get(1).get("session") or {}).get("start"), None)
check_true("说了「项目不吃后缀」",
           any("项目不吃后缀" in t for t in texts(wd_s)))

print("== 再导入同一个项目 = 刷新清单，不重复建 ==")
wd.omnifocus.rows = [dict(r) for r in rows_ok if r["id"] != "t-read"]
check("第二次 import 受理", wd.handle("task import omnifocus:///project/p-lunwen"), True)
check("还是一个项目", len(wd.projects.list()), 1)
check("清单被刷新成 3 条",
      [t["title"] for t in wd.projects.list()[0]["tasks"]],
      ["写引言", "跑数据", "写完的"])
check_true("说了「刷新」", any("刷新项目" in t for t in texts(wd)))

print("== 项目表：挂在任务面板下面 ==")
wd.omnifocus.rows = [dict(r) for r in rows_ok]
wd.handle("task import omnifocus:///project/p-lunwen")
wd.handle("task import omnifocus:///project/p-dushu")
check("两个项目", [p["title"] for p in wd.projects.active()], ["论文", "读书"])
rows = wd.project_rows()
check("预览表两行", [r["number"] for r in rows], [1, 2])
check("论文剩余 9 count", rows[0]["remaining"], 9)
check("读书剩余 2+3 count", rows[1]["remaining"], 5)
wd.handle("task create title=顺手一条")     # 有任务才有任务面板可以比位置
buf = io.StringIO()
wd.console = Console(file=buf, width=250, force_terminal=False)
wd.paint()
out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.getvalue())
check_true("契约视图里画出了项目表", "Project · 项目" in out)
check_true("项目表在任务面板**下面**",
           out.index("Task · 任务") < out.index("Project · 项目"))
check_true("项目表里有剩余 count 列", "剩余 count" in out)
check_true("项目表里有进度列", "进度" in out)
check_true("项目表里有今天 plan 列", "今天 plan" in out)

print("== plan：元数据 → 分资源（覆盖 / 清空 / 超预算） ==")
plan_day(wd)                       # 明天：T1 75 25 + T2 50，都订上了
check("plan +1 受理", wd.handle("plan +1"), True)
planner = wd.planning
check_true("plan 开着", planner is not None)
cap = planner.cap()
check("空场 3 场（T1 75+25、T2 50）", cap["sessions"], 3)
check("count = 时长/25 之和 = 6",
      cap["counts"], 6)
check_true("元数据里有「1 count = 25 分钟」",
           any("1 count = 25 分钟" in t for t in texts(wd)))

planner.handle("task 1 2")
check("项目 #1 分到 2 count", planner.allocation_of(1), 2)
planner.handle("task 1 4")
check("再敲一次是覆盖（4，不是 6）", planner.allocation_of(1), 4)
planner.handle("task 1 clear")
check("clear 清空", planner.allocation_of(1), 0)

total = cap["counts"]
planner.handle("task 1 %d" % (total - 1))
planner.handle("task 2 2")
check("加起来超预算 → 项目 #2 一个字都没动", planner.allocation_of(2), 0)
check("项目 #1 也没被动", planner.allocation_of(1), total - 1)
check_true("报错说「超预算」", any("超预算" in t for t in texts(wd)))
check_true("报错说了「这条命令没执行」",
           any("这条命令没执行" in t or "一个字都没动" in t for t in texts(wd)))
planner.handle("task 2 1")
check("放得下就收", planner.allocation_of(2), 1)

print("== plan：complete 之后自动排任务 ==")
wd.bridge.calls = []
check("complete 收掉这一问", wd.handle("complete"), True)
check("plan 关了", wd.planning, None)
tasks = [t for t in wd.task_store.list() if "project" in (t.get("fields") or {})]
check("没被唤起的任务原样不动",
      sorted(t["fields"]["title"] for t in wd.task_store.list()
             if "project" not in (t.get("fields") or {})),
      ["别的活", "顺手一条"])
check("唤起了任务（一共 8 count → 至少 2 条）", len(tasks) >= 2, True)
check("每条任务都记着来源项目",
      sorted({t["fields"]["project"] for t in tasks}), ["论文", "读书"])
assigned = [t for t in tasks if (t.get("session") or {}).get("start")]
check("每条任务都挂到了 session 上", len(assigned), len(tasks))
check("一场只放了一个任务（尽量独立 session）",
      len({(t["session"]["date"], t["session"]["start"]) for t in tasks}), len(tasks))
check("一条任务的 count 和时长对得上",
      [(t["fields"]["count"], t["fields"]["duration"]) for t in tasks
       if t["fields"]["count"] != t["fields"]["duration"] // 25], [])
check("count 相加 = 分出去的资源",
      sum(int(t["fields"]["count"]) for t in tasks), total)
check("都走 Focusmate 建了 task", len([c for c in wd.bridge.calls
                                     if c[0] == "add_task"]), len(tasks))
plan1 = pj.plan_of(by_num(wd), wd.sel_date)
check("项目库记下了这一天说好的 count", int(plan1["count"]), total - 1)
check("项目库记下了排下去的每一条",
      len(plan1["items"]), len([t for t in tasks if t["fields"]["project_id"] == 1]))
check("due 跟着 session 终点走（第二天）",
      all(t["fields"].get("due", "").startswith(TOMORROW) for t in tasks), True)

print("== plan：分配数超过项目剩余 count 时如实报少排 ==")
wd2 = fresh(rows_ok)
wd2.handle("task import omnifocus:///project/p-dushu")
plan_day(wd2)
wd2.handle("plan +1")
cap2 = wd2.planning.cap()
wd2.planning.handle("task 1 %d" % cap2["counts"])
wd2.bridge.calls = []
wd2.handle("complete")
check_true("说了「只剩 … count」",
           any("只剩" in t and "少排" in t for t in texts(wd2)),
           )

print("== 排期：尽量一场一个任务、尽量不拆 ==")
rows_p = [
    {"id": "p1", "name": "论文", "kind": "project_root"},
    {"id": "a", "name": "写引言", "kind": "action", "project": "论文",
     "estimated_minutes": 75},
    {"id": "b", "name": "跑数据", "kind": "action", "project": "论文",
     "estimated_minutes": 100},
]
proj = pj.project_payload(rows_p[0], rows_p)
items, notes = pp.distribute([{"project_id": 1, "project": proj, "count": 4}], [proj], {})
check("按清单顺序切 count", [(i["title"], i["count"]) for i in items],
      [("写引言", 3), ("跑数据", 1)])
slots = [pp.slot_record(1, ISO, "11:00", 75, "s1"), pp.slot_record(2, ISO, "12:15", 25, "s2")]
places, uns, more = pp.schedule(items, slots)
check("装得下就不拆", [p["split"] for p in places], [False, False])
check("一场一个任务", [p["split"] or p["stacked"] for p in places], [False, False])
check("75 分钟的活进了 75 分钟那场",
      [(p["slot"]["start"], p["item"]["title"]) for p in places],
      [("11:00", "写引言"), ("12:15", "跑数据")])
check("没有漏排", uns, [])

# 每场都装不下 → 拆开（如实说）
slots2 = [pp.slot_record(1, ISO, "11:00", 50, "s1"), pp.slot_record(2, ISO, "12:00", 50, "s2")]
places2, uns2, more2 = pp.schedule(items, slots2)
check("3 count 的活进不了 2 count 的场 → 拆开",
      sorted((p["item"]["title"], p["count"]) for p in places2
             if p["item"]["title"] == "写引言"),
      [("写引言", 1), ("写引言", 2)])
check_true("拆开要说出来", any("拆成" in n for n in more2))

print("== 部分完成不勾 OmniFocus 完成（项目里还有剩） ==")
wd3 = fresh(rows_ok)
wd3.handle("task import omnifocus:///project/p-lunwen")
project = wd3.projects.active()[0]
inv = pj.invested_by_project(wd3.task_store)
# 只分 2 count（那条 OmniFocus 任务总共 3）：唤起的任务完成 ≠ 那条任务完成
items3, _ = pp.distribute([{"project_id": project["id"], "project": project,
                            "count": 2}], [project], {project["id"]: {}})
check("只唤起 1 条、2 count", [(i["title"], i["count"]) for i in items3],
      [("写引言", 2)])
local = wd3.task_store.create(
    {"title": "写引言", "count": 2, "project": "论文",
     "project_id": project["id"], "project_task": "t-intro"},
    omnifocus={"id": "t-intro", "url": of.task_url("t-intro"), "title": "写引言"})
wd3.task_store.complete(local["id"])
allowed, why = pj.completion_guard(wd3.task_store.get(local["id"]),
                                   wd3.projects, wd3.task_store)
check("2/3 → 不许勾 OmniFocus 完成", allowed, False)
check_true("说清还差多少", "剩下的 1 count" in why)
note = wd3.omni_complete(wd3.task_store.get(local["id"]))
check_true("omni_complete 也挡住了", "先不勾 OmniFocus 完成" in note)
check("没记 writeback",
      (wd3.task_store.get(local["id"]).get("omnifocus") or {}).get("writeback"), None)
# 再把剩下 1 count 干完 → 允许
local2 = wd3.task_store.create(
    {"title": "写引言", "count": 1, "project": "论文",
     "project_id": project["id"], "project_task": "t-intro"},
    omnifocus={"id": "t-intro", "url": of.task_url("t-intro"), "title": "写引言"})
wd3.task_store.complete(local2["id"])
allowed2, _why2 = pj.completion_guard(wd3.task_store.get(local2["id"]),
                                      wd3.projects, wd3.task_store)
check("3/3 → 允许勾完成", allowed2, True)

print("== 项目进度：干过的活算进剩余 count ==")
stat2 = pj.stats(wd3.projects.by_id(project["id"]), wd3.task_store)
check("写引言 3 count 已经全投进去", stat2["done"], 1 + 3)
check("剩余 = 总 10 - 4", stat2["remaining"], 6)

print("== project 命令 ==")
wd4 = fresh(rows_ok)
wd4.handle("task import omnifocus:///project/p-lunwen")
check("project 列表受理", wd4.handle("project"), True)
check_true("列出了项目名", any("论文" in t for t in texts(wd4)))
check("project 1 详情受理", wd4.handle("project 1"), True)
check_true("详情里有清单", any("写引言" in t for t in texts(wd4)))
wd4.omnifocus.rows = [dict(r) for r in rows_ok if r["id"] != "t-done"]
check("project 1 refresh 受理", wd4.handle("project 1 refresh"), True)
check("清单刷新成 3 条", len(wd4.projects.active()[0]["tasks"]), 3)
check_true("说了「刷新了」", any("刷新了" in t for t in texts(wd4)))
check("project 1 remove 受理", wd4.handle("project 1 remove"), True)
check("项目库空了", wd4.projects.active(), [])

print("== 刷新时项目里冒出一条没时长的 → 拒绝写库 ==")
wd5 = fresh(rows_ok)
wd5.handle("task import omnifocus:///project/p-lunwen")
before = [t["title"] for t in wd5.projects.active()[0]["tasks"]]
wd5.omnifocus.rows = [dict(r) for r in ONROWS]
check("refresh 受理", wd5.handle("project 1 refresh"), True)
check("清单一个字没动", [t["title"] for t in wd5.projects.active()[0]["tasks"]], before)
check_true("说了拒绝", any("没写库" in t for t in texts(wd5)))

print("== plan：过去的日子 / 没有空场 / 没有项目都说清楚 ==")
wd6 = fresh(rows_ok)
wd6.handle("plan -1")
check_true("过去的日子不 plan", any("是过去的日子" in t for t in texts(wd6)))
wd6.handle("day 0")
wd6.handle("plan")
check_true("没项目时 plan 不开口，只说「项目库是空的」",
           any("项目库是空的" in t for t in texts(wd6)))
check("没项目时没有开问答", wd6.planning, None)
wd6.handle("task import omnifocus:///project/p-lunwen")
wd6.handle("day 1")
wd6.handle("session T1 75 25")
wd6.handle("session T2 50")
wd6.api_rows = []                   # data.csv 有排布、但一场都没订上
wd6.rows_source = "bridge"
wd6.rebuild_rows()
wd6.handle("plan +1")
check_true("没订上的场次不算空场",
           any("还没订上 Focusmate" in t for t in texts(wd6)))
check_true("并说「一场空着的都没有」",
           any("空着" in t and "session" in t for t in texts(wd6)))
check("这时也没开问答", wd6.planning, None)

print("== plan 进行中：别的问答不会抢输入 ==")
plan_day(wd6)
wd6.handle("plan +1")
check_true("plan 开着", wd6.planning is not None)
wd6._maybe_start_nostart()
check("plan 开着时不弹「到点没开始」", wd6.nostart, None)
check("plan 开着时 q 放弃", wd6.handle("q"), True)
check("plan 收掉了", wd6.planning, None)
check("放弃时没分资源", by_num(wd6)["plans"], {})

print("== plan 的日期参数：默认今天，+1 是明天 ==")
wd7 = fresh(rows_ok)
wd7.omnifocus.rows = [dict(r) for r in rows_ok]
wd7.handle("task import omnifocus:///project/p-lunwen")
wd7.handle("day 1")                       # 看板在看明天
check("看板是明天", wd7.sel_date.isoformat(), TOMORROW)
wd7.messages.clear()
wd7.handle("plan 明天")                    # 明天还没订上 → 空场为 0
check_true("说清 plan 的是哪一天",
           any("plan 的是 %s" % TOMORROW in t for t in texts(wd7)))
check_true("没订上就不算空场", any("还没订上 Focusmate" in t for t in texts(wd7)))
check("这时没开问答", wd7.planning, None)

check("plan 的日期参数：+1", wd7._plan_target(["+1"])[1], 1)
check("plan 的日期参数：明天", wd7._plan_target(["明天"])[1], 1)
check("plan 的日期参数：空 = 今天",
      wd7._plan_target([])[0], datetime.date.today())
check("plan 的日期参数：今天",
      wd7._plan_target(["今天"])[0], datetime.date.today())
check("plan 的日期参数：ISO 日期",
      wd7._plan_target([TOMORROW])[0].isoformat(), TOMORROW)
try:
    wd7._plan_target(["-1"])
    check("过去的日子应该被拒", "没拒绝", "拒绝")
except ValueError as e:
    check_true("过去的日子被拒", "过去的日子" in str(e))
try:
    wd7._plan_target(["下周吧"])
    check("认不出的参数应该被拒", "没拒绝", "拒绝")
except ValueError as e:
    check_true("认不出的参数被拒", "你给的是" in str(e))

print("== plan 默认今天：看板在看明天，也照样排今天 ==")
wd8 = fresh(rows_ok)
wd8.omnifocus.rows = [dict(r) for r in rows_ok]
wd8.handle("task import omnifocus:///project/p-lunwen")
wd8.handle("day 1")
wd8.handle("session T1 75 25")             # 明天的排布
wd8.handle("day 0")
wd8.handle("session T1 75 25")             # 今天的排布（12:15 那场是今天的记号）
book_all(wd8)                              # 今天的都订上了
check("看板在今天是哪一天", wd8.sel_date, datetime.date.today())
wd8.handle("plan")
check("plan 不带参数 = 今天", wd8.planning.day, datetime.date.today())
check("今天有空场才开得了问答", wd8.planning is not None, True)
wd8.handle("q")
wd8.handle("day 1")                        # 切到明天（离线模式下 API 行会被清掉）
check("看板到明天", wd8.sel_date.isoformat(), TOMORROW)
wd8.messages.clear()
wd8.handle("plan")                         # 照样该排今天
check("看板被切回今天", wd8.sel_date, datetime.date.today())
check_true("说的是今天的场次（12:15 只存在于今天的排布里）",
           any("12:15" in t for t in texts(wd8)))

print("== 今天没 plan → 状态框里有一条警告；plan 过就消失 ==")
wd9 = fresh(rows_ok)
wd9.omnifocus.rows = [dict(r) for r in rows_ok]
check("没项目时不该提这件事", wd9.pending_plan(), None)
wd9.handle("task import omnifocus:///project/p-lunwen")
pend = wd9.pending_plan()
check_true("有项目 + 今天有空场 → 该提醒", pend is not None)
check("提醒里带着场次数", pend["sessions"] > 0, True)
check_true("警告板里有一条「今天还没 plan」",
           any("今天还没 plan" in getattr(item, "text", "")
               for item in wd9.collect_warnings()))
check_true("提示里教怎么敲",
           any("plan +1" in (getattr(item, "hint", "") or "")
               for item in wd9.collect_warnings()))
# 真去 plan 一次（分 1 个 count 就够）→ 提醒消失
wd9.handle("session T1 75 25")
book_all(wd9)
wd9.handle("plan")
wd9.handle("task 1 1")
wd9.handle("complete")
check("plan 过之后不再提醒", wd9.pending_plan(), None)
check_true("警告板里也没了",
           not any("今天还没 plan" in getattr(item, "text", "")
                   for item in wd9.collect_warnings()))

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    sys.exit(1)
print("all good")
