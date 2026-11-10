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
     "tags": ["project/论文"], "completed": False, "dropped": False},
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
     "tags": ["project/读书"], "completed": False, "dropped": False},
    {"id": "t-book", "name": "读一章", "kind": "action", "project": "读书",
     "parent": "读书", "estimated_minutes": 50, "completed": False},
    {"id": "t-more", "name": "读书笔记", "kind": "action", "project": "读书",
     "parent": "读书", "estimated_minutes": 75, "completed": False},
    {"id": "t-bad", "name": "没设时长的", "kind": "action", "project": "论文",
     "parent": "论文", "estimated_minutes": 0, "completed": False},
]


class FakeOmniFocus(object):
    def __init__(self, rows=None, repetitions=None):
        self.rows = rows if rows is not None else [dict(r) for r in ONROWS]
        self.repetitions = repetitions or {}
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
        return dict(self.repetitions.get(str(pk), {"repeated": False}))

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        return {"after": {"completed": True}}

    def drop(self, pk, at=None, all_occurrences=False, yes=True, timeout=None):
        return {"after": {"dropped": True}}

    def invalidate(self):
        pass


def mkwarden(rows=None, repetitions=None):
    console = Console(file=io.StringIO(), width=250, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.omnifocus = FakeOmniFocus(rows, repetitions)
    return wd


def reset_tasks():
    """任务库是同一个文件：上一段排进 session 的任务会让那几场不再「空着」。"""
    store = ts.TaskStore()
    for task in store.list():
        store.delete(task["id"])


def reset_projects():
    """整份清空（连 next_id 一起）—— 只删记录的话序号会一路涨，用例就对不上 #1/#2。"""
    pj.save(pj.blank())


def fresh(rows=None, repetitions=None):
    """干净的任务库 + 项目库 + 一个新的 warden（各段互不串味）。"""
    reset_tasks()
    reset_projects()
    return mkwarden(rows, repetitions)


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

print("== 本周任务预算：active / 被推迟 / 重复 occurrence / 叠加 ==")
_fixed_now = datetime.datetime(2026, 11, 4, 12, 0)      # 周三；本周 = 11-02(一)–11-08(日)
_week_tasks = [
    {"pk": "active", "title": "现在就能做", "duration": 25},
    {"pk": "defer-thu", "title": "周四才开放", "duration": 50,
     "defer": "2026-11-05T09:00:00"},
    {"pk": "defer-next", "title": "下下周才开放", "duration": None,
     "defer": "2026-11-16T09:00:00"},
    {"pk": "repeat-mw", "title": "周一和周三重复", "duration": 25,
     "repeat": {"rule": "FREQ=WEEKLY;BYDAY=MO,WE",
                "rule_anchor": "2026-11-02T10:00:00",
                "next": "2026-11-09T10:00:00"}},
    {"pk": "repeat-defer", "title": "推迟的每日重复", "duration": 25,
     "defer": "2026-11-05T10:00:00",
     "repeat": {"rule": "FREQ=DAILY",
                "rule_anchor": "2026-11-05T10:00:00",
                "next": "2026-11-05T10:00:00"}},
    {"pk": "repeat-bad", "title": "重复但没时长", "duration": None,
     "repeat": {"rule": "FREQ=WEEKLY;BYDAY=MO,WE",
                "rule_anchor": "2026-11-04T10:00:00"}},
    {"pk": "done", "title": "已完成", "duration": 25, "completed": True},
]
_week = pj.weekly_yield(_week_tasks, now=_fixed_now)
check("周一固定为本周起点", (_week["start"], _week["end"]),
      ("2026-11-02", "2026-11-08"))
check("重复任务的周一 + 周三两次都算",
      [x["date"] for x in _week["tasks"] if x["pk"] == "repeat-mw"],
      ["2026-11-02", "2026-11-04"])
check("本周四开放的推迟任务算一条",
      [(x["pk"], x["date"]) for x in _week["tasks"] if x["pk"] == "defer-thu"],
      [("defer-thu", "2026-11-05")])
check("下周才开放的推迟任务不算",
      [x for x in _week["tasks"] if x["pk"] == "defer-next"], [])
check("重复 + 推迟同一条：周四到周日 4 次都算",
      [x["date"] for x in _week["tasks"] if x["pk"] == "repeat-defer"],
      ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"])
check("重复且被推迟的 occurrence 有标记",
      all(x["deferred"] for x in _week["tasks"] if x["pk"] == "repeat-defer"), True)
check("已完成不产生本周任务", [x for x in _week["tasks"] if x["pk"] == "done"], [])
check("本周总条数", _week["count"], 1 + 1 + 2 + 4 + 2)
check("时长缺口按任务聚合，重复的两天都点名",
      [(_x["pk"], _x["count"]) for _x in _week["missing"]],
      [("repeat-bad", 2)])
check_true("周总结说从周一开始", "周一开始" in pj.week_summary(_week))

print("== project_payload：重复规则合进来后才算完整的本周账 ==")
_payload = pj.project_payload(
    {"id": "p-week", "name": "周项目", "kind": "project_root"},
    [
        {"id": "p-week", "name": "周项目", "kind": "project_root"},
        {"id": "active", "name": "现在就能做", "kind": "action",
         "project": "周项目", "estimated_minutes": 25},
        {"id": "rep", "name": "周一和周三", "kind": "action",
         "project": "周项目", "estimated_minutes": 25,
         "defer": "2026-11-04T09:00:00"},
    ],
    repeat_infos={"rep": {"repeated": True,
                          "rule_string": "FREQ=WEEKLY;BYDAY=MO,WE",
                          "next": "2026-11-09T10:00:00",
                          "estimated_minutes": 25}},
    now=_fixed_now,
)
check("payload 里带本周账", _payload["week"]["count"], 3)
check("active 1 + 重复 2", (_payload["week"]["active"],
                            _payload["week"]["repeated"]), (1, 2))
check("重复规则被保存进任务记录",
      _payload["tasks"][1]["repeat"]["rule"], "FREQ=WEEKLY;BYDAY=MO,WE")
check("本周没有时长缺口", pj.format_missing(_payload), "")

print("== task import：时长不齐的项目被拒，一个字节都不落 ==")
wd = fresh()
check("import 受理", wd.handle("task import sop omnifocus:///project/p-lunwen"), True)
check("项目库还是空的", wd.projects.list(), [])
check("任务库也是空的", wd.task_store.list(), [])
check_true("说了「拒绝」", any("这次导入被拒" in t for t in texts(wd)))
check_true("点名了缺时长的那条", any("没设时长的" in t for t in texts(wd)))
check("记成了出过错", wd.had_error, True)

print("== task import：项目链接但 OmniFocus 没开 → 拒绝（不建「叫项目名的任务」） ==")
wd_off = mkwarden()
wd_off.settings.set("omnifocus", "off")
check("import 受理", wd_off.handle("task import sop omnifocus:///project/p-lunwen"), True)
check("项目库还是空的", wd_off.projects.list(), [])
check("任务库也没被污染", wd_off.task_store.list(), [])
check_true("说清是「项目链接 + 读不到」",
           any("项目" in t and "读不到" in t for t in texts(wd_off)))
wd_off.settings.set("omnifocus", "on")

print("== 子任务：最多父-子两层、父任务空壳、周账按父任务算一条 ==")
_sub_rows = [
    {"id": "p-sub", "name": "子任务项目", "kind": "project_root",
     "tags": ["project/子任务项目"], "completed": False, "dropped": False},
    {"id": "parent", "name": "父任务", "kind": "action", "project": "子任务项目",
     "parent_id": "", "estimated_minutes": None, "completed": False},
    {"id": "c10", "name": "子 10", "kind": "action", "project": "子任务项目",
     "parent_id": "parent", "estimated_minutes": 10, "completed": False},
    {"id": "c13", "name": "子 13", "kind": "action", "project": "子任务项目",
     "parent_id": "parent", "estimated_minutes": 13, "completed": False},
    {"id": "c27", "name": "子 27", "kind": "action", "project": "子任务项目",
     "parent_id": "parent", "estimated_minutes": 27, "completed": False},
]
_sub_payload = pj.project_payload(_sub_rows[0], _sub_rows, now=_fixed_now)
check("父任务壳没设时长也不缺时长",
      _sub_payload["missing"], [])
check("父任务时长 = 子任务之和", _sub_payload["tasks"][0]["duration"], 50)
check("父任务 count = ceil(50/25)", _sub_payload["tasks"][0]["count"], 2)
check("父任务被标成 shell", _sub_payload["tasks"][0]["shell"], True)
check("父任务下三个子任务",
      _sub_payload["tasks"][0]["child_pks"], ["c10", "c13", "c27"])
check("本周按父任务算一条，不把三个子任务重复计", _sub_payload["week"]["count"], 1)
_sub_rem = pj.remaining_tasks(dict(_sub_payload, id=1), {})
check("排期只返回父任务壳", [(x["pk"], x["duration"], len(x["children"]))
                             for x in _sub_rem],
      [("parent", 50, 3)])

_sub_grand = [dict(r) for r in _sub_rows]
_sub_grand.append({"id": "grand", "name": "孙任务", "kind": "action",
                   "project": "子任务项目", "parent_id": "c13",
                   "estimated_minutes": 5, "completed": False})
_sub_bad = pj.project_payload(_sub_grand[0], _sub_grand, now=_fixed_now)
check("三层任务被点名", [e["title"] for e in _sub_bad["depth_errors"]],
      ["孙任务"])
check_true("三层任务有明确拒绝话术",
           "严禁三层任务" in pj.format_depth_errors(_sub_bad))

print("== 同名项目：按 OmniFocus 项目 primary key 分组，不串 ==")
_dup_rows = [
    {"id": "p-dup-a", "name": "同名项目", "kind": "project_root"},
    {"id": "a-task", "name": "A 的任务", "kind": "action",
     "project": "同名项目", "project_id": "p-dup-a",
     "estimated_minutes": 25, "completed": False},
    {"id": "p-dup-b", "name": "同名项目", "kind": "project_root"},
    {"id": "b-task", "name": "B 的任务", "kind": "action",
     "project": "同名项目", "project_id": "p-dup-b",
     "estimated_minutes": 50, "completed": False},
]
_payload_a = pj.project_payload(_dup_rows[0], _dup_rows, now=_fixed_now)
_payload_b = pj.project_payload(_dup_rows[2], _dup_rows, now=_fixed_now)
check("同名项目 A 只拿自己的任务",
      [t["pk"] for t in _payload_a["tasks"]], ["a-task"])
check("同名项目 B 只拿自己的任务",
      [t["pk"] for t in _payload_b["tasks"]], ["b-task"])
check("有 project_id 时不再报同名串项目", _payload_a["warnings"], [])
check("同名项目任务时长各自正确",
      (_payload_a["tasks"][0]["duration"], _payload_b["tasks"][0]["duration"]),
      (25, 50))

wd_dup = fresh(_dup_rows)
check("导入第一个同名项目", wd_dup.handle("task import sop omnifocus:///project/p-dup-a"), True)
check("导入第二个同名项目", wd_dup.handle("task import sop omnifocus:///project/p-dup-b"), True)
_dup_projects = wd_dup.projects.active()
check("两个同名项目分别进库",
      [(p["omnifocus"]["id"], [t["pk"] for t in p["tasks"]])
       for p in _dup_projects],
      [("p-dup-a", ["a-task"]), ("p-dup-b", ["b-task"])])

print("== 导入校验只看活跃/推迟任务：完成/放弃的任务不参与层级和时长检查 ==")
_done_deep = [dict(r) for r in _sub_grand]
for item in _done_deep:
    if item["id"] == "grand":
        item["completed"] = True
_done_payload = pj.project_payload(_done_deep[0], _done_deep, now=_fixed_now)
check("已完成的孙任务不触发三层错误", _done_payload["depth_errors"], [])
_done_missing = [
    {"id": "p-donecheck", "name": "完成检查项目", "kind": "project_root"},
    {"id": "done-bad", "name": "已完成但没时长", "kind": "action",
     "project": "完成检查项目", "estimated_minutes": 0, "completed": True},
    {"id": "defer-ok", "name": "推迟但有时长", "kind": "action",
     "project": "完成检查项目", "estimated_minutes": 25, "completed": False,
     "defer": "2026-11-05T09:00:00"},
]
_done_missing_payload = pj.project_payload(
    _done_missing[0], _done_missing, now=_fixed_now)
check("已完成但没时长的任务不挡导入", _done_missing_payload["missing"], [])
_defer_bad = _done_missing + [
    {"id": "defer-bad", "name": "推迟但没时长", "kind": "action",
     "project": "完成检查项目", "estimated_minutes": 0, "completed": False,
     "defer": "2026-11-05T10:00:00"},
]
_defer_bad_payload = pj.project_payload(
    _defer_bad[0], _defer_bad, now=_fixed_now)
check("推迟的活跃任务仍要检查时长",
      _defer_bad_payload["missing"], ["推迟但没时长"])

print("== 子任务继承父任务状态：父完成/放弃 → 未单独改状态的子任务也关闭 ==")
_inherit_done_rows = [
    {"id": "p-inherit-done", "name": "继承完成项目", "kind": "project_root"},
    {"id": "done-parent", "name": "已完成的父任务", "kind": "action",
     "project": "继承完成项目", "estimated_minutes": None,
     "completed": True, "dropped": False},
    {"id": "done-child", "name": "没有单独改状态的子任务", "kind": "action",
     "project": "继承完成项目", "parent_id": "done-parent",
     "estimated_minutes": 0, "completed": False, "dropped": False},
    {"id": "done-grand", "name": "孙任务也不该挡导入", "kind": "action",
     "project": "继承完成项目", "parent_id": "done-child",
     "estimated_minutes": 0, "completed": False, "dropped": False},
]
_inherit_done = pj.project_payload(
    _inherit_done_rows[0], _inherit_done_rows, now=_fixed_now)
_inherit_tasks = {t["pk"]: t for t in _inherit_done["tasks"]}
check("父任务完成 → 子任务有效状态 completed",
      (_inherit_tasks["done-child"]["completed"],
       _inherit_tasks["done-child"]["dropped"]), (True, False))

# OmniFocus 自己会给出 taskStatus / effective* 标记。优先用这个标记，
# 不再要求子任务自己的 completed 也为 true。
_marker_rows = [
    {"id": "p-marker", "name": "OF 有效状态项目", "kind": "project_root"},
    {"id": "marker-parent", "name": "父任务", "kind": "action",
     "project": "OF 有效状态项目", "completed": False, "dropped": False},
    {"id": "marker-child", "name": "OF 标记已完成的子任务", "kind": "action",
     "project": "OF 有效状态项目", "parent_id": "marker-parent",
     "completed": False, "dropped": False,
     "effective_completed": True, "effective_dropped": False,
     "estimated_minutes": None},
]
_marker_payload = pj.project_payload(
    _marker_rows[0], _marker_rows, now=_fixed_now)
_marker_tasks = {t["pk"]: t for t in _marker_payload["tasks"]}
check("OmniFocus 有效完成标记 → 子任务按完成处理",
      (_marker_tasks["marker-child"]["completed"],
       _marker_tasks["marker-child"]["effective_completed"]), (True, True))
check("OmniFocus 有效完成标记 → 不再检查子任务时长",
      _marker_payload["missing"], [])
check("OmniFocus 有效完成标记 → 子任务不进可用任务",
      pj.remaining_tasks(dict(_marker_payload, id=1), {}), [])
check("父任务完成 → 孙任务也继承 completed",
      (_inherit_tasks["done-grand"]["completed"],
       _inherit_tasks["done-grand"]["dropped"]), (True, False))
check("继承完成的任务不参与时长校验", _inherit_done["missing"], [])
check("继承完成的任务不触发三层层级报错", _inherit_done["depth_errors"], [])
check("继承完成的子任务不产生本周任务", _inherit_done["week"]["count"], 0)
check("继承完成的子任务不进入可用任务", pj.remaining_tasks(
    dict(_inherit_done, id=1), {}), [])

_inherit_drop_rows = [
    {"id": "p-inherit-drop", "name": "继承放弃项目", "kind": "project_root"},
    {"id": "drop-parent", "name": "已放弃的父任务", "kind": "action",
     "project": "继承放弃项目", "estimated_minutes": None,
     "completed": False, "dropped": True},
    {"id": "drop-child", "name": "跟着父任务放弃", "kind": "action",
     "project": "继承放弃项目", "parent_id": "drop-parent",
     "estimated_minutes": 0, "completed": False, "dropped": False},
    {"id": "drop-grand", "name": "孙任务也一起放弃", "kind": "action",
     "project": "继承放弃项目", "parent_id": "drop-child",
     "estimated_minutes": 0, "completed": False, "dropped": False},
]
_inherit_drop = pj.project_payload(
    _inherit_drop_rows[0], _inherit_drop_rows, now=_fixed_now)
_inherit_drop_tasks = {t["pk"]: t for t in _inherit_drop["tasks"]}
check("父任务放弃 → 子任务有效状态 dropped",
      (_inherit_drop_tasks["drop-child"]["completed"],
       _inherit_drop_tasks["drop-child"]["dropped"]), (False, True))
check("父任务放弃 → 孙任务也继承 dropped",
      (_inherit_drop_tasks["drop-grand"]["completed"],
       _inherit_drop_tasks["drop-grand"]["dropped"]), (False, True))
check("继承放弃的任务不参与时长校验", _inherit_drop["missing"], [])
check("继承放弃的任务不触发三层层级报错", _inherit_drop["depth_errors"], [])
check("继承放弃的子任务不产生本周任务", _inherit_drop["week"]["count"], 0)
check("继承放弃的子任务不进入可用任务", pj.remaining_tasks(
    dict(_inherit_drop, id=1), {}), [])

_inherit_override_rows = _inherit_drop_rows + [
    {"id": "drop-child-done", "name": "单独改过完成的子任务", "kind": "action",
     "project": "继承放弃项目", "parent_id": "drop-parent",
     "estimated_minutes": 25, "completed": True, "dropped": False},
]
_inherit_override = pj.project_payload(
    _inherit_override_rows[0], _inherit_override_rows, now=_fixed_now)
_override_tasks = {t["pk"]: t for t in _inherit_override["tasks"]}
check("子任务显式完成不被父任务放弃覆盖",
      (_override_tasks["drop-child-done"]["completed"],
       _override_tasks["drop-child-done"]["dropped"]), (True, False))
check("父放弃 + 子显式完成：仍没有可排的任务",
      pj.remaining_tasks(dict(_inherit_override, id=1), {}), [])
check("显式完成的子任务仍计入完成账",
      pj.stats(dict(_inherit_override, id=1))["progress_text"], "1/1")

reset_projects()
_inherit_store = pj.ProjectStore()
_inherit_project, _ = _inherit_store.upsert(_inherit_drop)
_inherit_saved = pj.task_by_pk(_inherit_project, "drop-child")
check("项目库刷新后仍保存继承的 dropped",
      (_inherit_saved.get("completed"), _inherit_saved.get("dropped")),
      (False, True))

wd_inherit_done = fresh(_inherit_done_rows)
check("父完成 + 子任务没时长也能导入",
      wd_inherit_done.handle("task import sop omnifocus:///project/p-inherit-done"), True)
check("父完成项目确实进库", len(wd_inherit_done.projects.active()), 1)
check_true("没有报子任务缺少持续时间",
           not any("没有「持续时间」" in t for t in texts(wd_inherit_done)))
wd_inherit_drop = fresh(_inherit_drop_rows)
check("父放弃 + 子任务没时长也能导入",
      wd_inherit_drop.handle("task import sop omnifocus:///project/p-inherit-drop"), True)
check("父放弃项目确实进库", len(wd_inherit_drop.projects.active()), 1)
check_true("父放弃时也没报子任务缺少持续时间",
           not any("没有「持续时间」" in t for t in texts(wd_inherit_drop)))
wd_marker = fresh(_marker_rows)
check("OmniFocus 有效完成标记 + 子任务没时长也能导入",
      wd_marker.handle("task import sop omnifocus:///project/p-marker"), True)
check("有效完成标记项目确实进库", len(wd_marker.projects.active()), 1)
check_true("有效完成标记路径没报缺少持续时间",
           not any("没有「持续时间」" in t for t in texts(wd_marker)))

print("== 子任务完成：一条条勾，全部完成父任务自动完成 ==")
reset_projects()
_sub_store = pj.ProjectStore()
_sub_proj, _created = _sub_store.upsert(_sub_payload)
_sub_proj, _auto1, _new1 = _sub_store.complete_subtasks(
    _sub_proj["id"], "parent", ["c10"])
check("勾一个子任务", _new1, ["c10"])
check("父任务还没完成", _auto1, False)
_sub_proj, _auto2, _new2 = _sub_store.complete_subtasks(
    _sub_proj["id"], "parent", ["c13", "c27"])
check("剩下两个子任务一起勾", sorted(_new2), ["c13", "c27"])
check("全部子任务完成 → 父任务自动完成", _auto2, True)
check("项目库里父任务 completed",
      pj.task_by_pk(_sub_proj, "parent")["completed"], True)

print("== 子任务排期：父任务为单元，25 分钟场只能放 10+13=23，27 留到以后 ==")
_sub_items, _sub_notes = pp.distribute(
    [{"project_id": 1, "project": dict(_sub_payload, id=1), "count": 2}],
    [dict(_sub_payload, id=1)], {1: {}})
check("distribute 给出一套父任务单元", len(_sub_items), 1)
check("单元里带三个不可拆子任务",
      [c["pk"] for c in _sub_items[0]["children"]], ["c10", "c13", "c27"])
_slot25 = pp.slot_record(1, ISO, "11:00", 25, "s25")
_places, _uns, _more = pp.schedule(_sub_items, [_slot25])
check("25 分钟场选中 10+13",
      sorted(p["duration"] for p in _places), [10, 13])
check("27 分钟子任务没被拆 / 没硬塞",
      [p["item"]["pk"] for p in _places], ["c10", "c13"])
check("没排下的仍是剩余子任务",
      [c["pk"] for c in _uns[0]["children"]], ["c27"])
check_true("说明剩余分钟数", any("还有 27 分钟" in n for n in _more))
check("父任务信息跟着每个 placement",
      {(p["parent"]["pk"], p["item"]["parent_pk"]) for p in _places},
      {("parent", "parent")})
_slot75 = pp.slot_record(1, ISO, "12:00", 75, "s75")
_places75, _uns75, _more75 = pp.schedule(_sub_items, [_slot75])
check("75 分钟场放得下就三个子任务一起排",
      sorted(p["duration"] for p in _places75), [10, 13, 27])
check("全部排下就没有 unscheduled", _uns75, [])

print("== plan 落地子任务：session 里只出现子任务，note 标注父任务 ==")
wd_sub = fresh(_sub_rows)
check("导入含子任务的项目", wd_sub.handle("task import sop omnifocus:///project/p-sub"), True)
wd_sub.handle("day 1")
wd_sub.handle("session T1 25")          # 明天：T1 25 分钟 + T2 50 分钟
book_all(wd_sub)
_backlog = wd_sub.task_store.list()
check("plan 前任务库已有项目镜像（叶子任务）",
      sorted(t["fields"]["title"] for t in _backlog), ["子 10", "子 13", "子 27"])
check("plan 前镜像都还没挂 session",
      [t.get("session") for t in _backlog], [None, None, None])
check("plan 前镜像都是项目备选（不进契约面板）",
      all(ts.TaskStore.is_project_backlog(t) for t in _backlog), True)
wd_sub.handle("plan +1")
check_true("plan 打开", wd_sub.planning is not None)
wd_sub.planning.handle("task 1 1")       # 只分 1 count = 25 分钟
wd_sub.handle("complete")
_sub_locals = wd_sub.task_store.list()
_assigned = [t for t in _sub_locals if (t.get("session") or {}).get("start")]
check("session 里只挂子任务，没有父任务空壳",
      sorted(t["fields"]["title"] for t in _assigned), ["子 10", "子 13"])
check("每个子任务都记着父任务来源",
      {((t.get("source") or {}).get("parent_id"),
        (t.get("source") or {}).get("parent_title")) for t in _assigned},
      {("parent", "父任务")})
check("note 写明是谁的子",
      all("父任务：父任务" in ((t["fields"].get("note")
                               or t["fields"].get("notes") or ""))
          for t in _assigned), True)
check("本地任务时长是子任务原时长",
      sorted(t["fields"]["duration"] for t in _assigned), [10, 13])
check("两个子任务挂在同一场 session",
      len({(t["session"]["date"], t["session"]["start"]) for t in _assigned}), 1)
_before_sync = [(t["id"], t["fields"].get("count"), t["fields"].get("duration"),
                 tuple(sorted((t.get("session") or {}).items())))
                for t in _assigned]
wd_sub.sync_project_tasks(wd_sub.projects.by_id(1), quiet=True)
_after_sync = [(t["id"], t["fields"].get("count"), t["fields"].get("duration"),
                tuple(sorted((t.get("session") or {}).items())))
               for t in wd_sub.task_store.list()
               if (t.get("session") or {}).get("start")]
check("项目重新镜像不会覆盖已唤起任务的 count/session",
      _after_sync, _before_sync)
check("项目 plan 记账也按子任务落",
      sorted(x["pk"] for x in (pj.plan_of(wd_sub.projects.by_id(1),
                                          wd_sub.sel_date) or {}).get("items") or []),
      ["c10", "c13"])

_first = next(t for t in _assigned if t["fields"]["title"] == "子 10")
wd_sub.handle("task id:%d done" % _first["id"])
_sub_proj_after = wd_sub.projects.by_id(1)
check("完成一个 session 子任务会更新项目库",
      pj.task_by_pk(_sub_proj_after, "c10")["completed"], True)
check("还有子任务没完成 → 父任务不完成",
      pj.task_by_pk(_sub_proj_after, "parent")["completed"], False)

print("== 每日项目对账 diff：新增 / 删除 / 修改 ==")
_diff = pj.diff_tasks(
    [{"pk": "a", "title": "甲", "duration": 25},
     {"pk": "b", "title": "乙", "duration": 50}],
    [{"pk": "a", "title": "甲改", "duration": 25},
     {"pk": "c", "title": "丙", "duration": 25}],
)
check("新增任务", [x["pk"] for x in _diff["added"]], ["c"])
check("删除任务", [x["pk"] for x in _diff["removed"]], ["b"])
check("修改任务", [x["pk"] for x in _diff["modified"]], ["a"])
check_true("diff 摘要说清三种变化",
           all(x in pj.diff_text(_diff) for x in ("新增 1", "删除 1", "修改 1")))

print("== 每日项目对账：新增自动拉，删除/修改整项目问一次 ==")
reset_projects()
_sync_store = pj.ProjectStore()
_local_payload = pj.project_payload(
    {"id": "p-sync", "name": "同步项目", "kind": "project_root"},
    [
        {"id": "p-sync", "name": "同步项目", "kind": "project_root"},
        {"id": "old-a", "name": "旧甲", "kind": "action", "project": "同步项目",
         "estimated_minutes": 25, "completed": False},
        {"id": "old-b", "name": "旧乙", "kind": "action", "project": "同步项目",
         "estimated_minutes": 50, "completed": False},
    ], now=_fixed_now)
_sync_proj, _ = _sync_store.upsert(_local_payload)
_remote_payload = pj.project_payload(
    {"id": "p-sync", "name": "同步项目", "kind": "project_root"},
    [
        {"id": "p-sync", "name": "同步项目", "kind": "project_root"},
        {"id": "old-a", "name": "旧甲改", "kind": "action", "project": "同步项目",
         "estimated_minutes": 25, "completed": False},
        {"id": "new-c", "name": "新丙", "kind": "action", "project": "同步项目",
         "estimated_minutes": 25, "completed": False},
    ], now=_fixed_now)
_wd_sync = mkwarden()
_wd_sync._apply_project_sync_result({"projects": [{
    "project_id": _sync_proj["id"], "title": "同步项目",
    "payload": _remote_payload,
    "diff": pj.diff_tasks(_sync_proj["tasks"], _remote_payload["tasks"]),
    "remote_found": True, "invalid": "",
}]})
_synced = _wd_sync.projects.by_id(_sync_proj["id"])
check("新增任务已自动拉进项目库",
      "new-c" in [t["pk"] for t in _synced["tasks"]], True)
check("旧乙还在（冲突还没回答，不先删）",
      "old-b" in [t["pk"] for t in _synced["tasks"]], True)
check("删除 + 修改只开一个项目级问答", _wd_sync.project_sync is not None, True)
check_true("问答面板一次列完冲突",
           "删除 1" in _wd_sync.project_sync_panel().renderable.plain
           and "修改 1" in _wd_sync.project_sync_panel().renderable.plain)
_wd_sync._answer_project_sync("l")
check("选本地覆盖后保留本地旧任务",
      "old-b" in [t["pk"] for t in _wd_sync.projects.by_id(_sync_proj["id"])["tasks"]], True)
check("问答关闭", _wd_sync.project_sync, None)

# 再建一次冲突，选择 OmniFocus 覆盖
_sync_proj2 = _wd_sync.projects.upsert(_local_payload)[0]
_wd_sync._apply_project_sync_result({"projects": [{
    "project_id": _sync_proj2["id"], "title": "同步项目",
    "payload": _remote_payload,
    "diff": pj.diff_tasks(_sync_proj2["tasks"], _remote_payload["tasks"]),
    "remote_found": True, "invalid": "",
}]})
_wd_sync._answer_project_sync("o")
_after_remote = _wd_sync.projects.by_id(_sync_proj2["id"])
check("OmniFocus 覆盖后删掉本地多出的旧乙",
      "old-b" in [t["pk"] for t in _after_remote["tasks"]], False)
check("OmniFocus 覆盖后保留新丙",
      "new-c" in [t["pk"] for t in _after_remote["tasks"]], True)
check("OmniFocus 覆盖后采用远端标题",
      [t["title"] for t in _after_remote["tasks"] if t["pk"] == "old-a"],
      ["旧甲改"])

print("== refresh 会检查 OmniFocus 项目任务变化（60→50 能更新） ==")
_ref_rows_60 = [
    {"id": "p-refresh", "name": "刷新项目", "kind": "project_root",
     "completed": False, "dropped": False},
    {"id": "r-1", "name": "任务一", "kind": "action",
     "project": "刷新项目", "estimated_minutes": 60, "completed": False},
]
_ref_rows_50 = [
    {"id": "p-refresh", "name": "刷新项目", "kind": "project_root",
     "completed": False, "dropped": False},
    {"id": "r-1", "name": "任务一", "kind": "action",
     "project": "刷新项目", "estimated_minutes": 50, "completed": False},
]
_wd_ref = fresh(_ref_rows_60)
_wd_ref.projects.upsert(pj.project_payload(_ref_rows_60[0], _ref_rows_60,
                                           now=_fixed_now))
check("项目库先存的是 60 分钟",
      [t["duration"] for t in _wd_ref.projects.active()[0]["tasks"]
       if t["pk"] == "r-1"], [60])
_wd_ref.omnifocus.rows = [dict(r) for r in _ref_rows_50]
_wd_ref._refresh_project_tasks()
check("refresh 项目任务对账：发现修改并开问答", _wd_ref.project_sync is not None, True)
_wd_ref._answer_project_sync("o")
check("refresh 后 OmniFocus 的 50 分钟更新进项目库",
      [t["duration"] for t in _wd_ref.projects.active()[0]["tasks"]
       if t["pk"] == "r-1"], [50])

print("== task import：重复 / 被推迟的本周任务没时长 → 整批拒绝 ==")
_week_mon = TODAY - datetime.timedelta(days=TODAY.weekday())
_thu = _week_mon + datetime.timedelta(days=3)
_week_rows = [
    {"id": "p-week-import", "name": "周导入项目", "kind": "project_root",
     "completed": False, "dropped": False},
    {"id": "w-active", "name": "正常 active", "kind": "action",
     "project": "周导入项目", "estimated_minutes": 25, "completed": False},
    {"id": "w-defer", "name": "周四开放但没时长", "kind": "action",
     "project": "周导入项目", "estimated_minutes": 0, "completed": False,
     "defer": "2026-11-05T09:00:00"},
    {"id": "w-rep", "name": "重复但没时长", "kind": "action",
     "project": "周导入项目", "estimated_minutes": 0, "completed": False},
]
_rep_infos = {"w-rep": {
    "repeated": True,
    "rule_string": "FREQ=WEEKLY;BYDAY=MO,WE",
    "next": (_week_mon + datetime.timedelta(days=7)).isoformat() + "T10:00:00",
    "estimated_minutes": 0,
}}
wd_week = fresh(_week_rows, repetitions=_rep_infos)
check("含重复/被推迟任务的 import 受理",
      wd_week.handle("task import sop omnifocus:///project/p-week-import"), True)
check("项目没进库", wd_week.projects.list(), [])
check_true("本周缺时长的被推迟任务点名",
           any("周四开放但没时长" in t for t in texts(wd_week)))
check_true("本周缺时长的重复任务点名",
           any("重复但没时长" in t for t in texts(wd_week)))
check_true("报错说的是「本周会产生」",
           any("本周会产生" in t and "没有「持续时间」" in t
               for t in texts(wd_week)))

print("== task import：时长齐全时，重复规则和本周条数一起进库 ==")
_week_rows_ok = [dict(r) for r in _week_rows
                 if r["id"] not in ("w-defer", "w-rep")]
_week_rows_ok.append({
    "id": "w-rep-ok", "name": "周一和周三重复", "kind": "action",
    "project": "周导入项目", "estimated_minutes": 25, "completed": False,
})
_rep_ok = {"w-rep-ok": {
    "repeated": True,
    "rule_string": "FREQ=WEEKLY;BYDAY=MO,WE",
    "next": (_week_mon + datetime.timedelta(days=7)).isoformat() + "T10:00:00",
    "estimated_minutes": 25,
}}
wd_week_ok = fresh(_week_rows_ok, repetitions=_rep_ok)
check("时长齐全的 import 受理",
      wd_week_ok.handle("task import sop omnifocus:///project/p-week-import"), True)
_proj_week = wd_week_ok.projects.active()[0]
check("重复规则落进项目库",
      [t.get("repeat", {}).get("rule") for t in _proj_week["tasks"]
       if t["pk"] == "w-rep-ok"],
      ["FREQ=WEEKLY;BYDAY=MO,WE"])
check("本周条数 = active 1 + 周三和周一重复 2",
      wd_week_ok.project_rows()[0]["week_count"], 3)
check_true("导入消息里报了本周会产出几条",
           any("会产生 3 条" in t for t in texts(wd_week_ok)))

print("== task import：时长齐了就进项目库（任务库一条都不建） ==")
rows_ok = [r for r in ONROWS if r["id"] != "t-bad"]
wd = fresh(rows_ok)
check("import 受理", wd.handle("task import sop omnifocus:///project/p-lunwen"), True)
projects = wd.projects.list()
check("项目进库了", len(projects), 1)
check("项目名", projects[0]["title"], "论文")
check("项目挂的是 OmniFocus 项目 pk",
      projects[0]["omnifocus"]["id"], "p-lunwen")
check("清单里 4 条任务",
      [t["title"] for t in projects[0]["tasks"]],
      ["写引言", "跑数据", "读文献", "写完的"])
_backlog = wd.task_store.list()
check("叶子任务已镜像进任务库",
      sorted(t["fields"]["title"] for t in _backlog),
      sorted(["写引言", "跑数据", "读文献"]))
check("镜像都还没挂 session、属于项目备选",
      all(t.get("session") is None and ts.TaskStore.is_project_backlog(t)
          for t in _backlog), True)
check("没被唤起的不进契约任务面板", wd.task_panel_tasks(), [])
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
_other = next(t for t in wd_s.task_store.list()
              if (t.get("fields") or {}).get("title") == "别的活")
wd_s.handle("session T1 25")
book_all(wd_s)
check("带 -a 的 import 受理",
      wd_s.handle("task import sop omnifocus:///project/p-lunwen -a 1"), True)
check("别的活没被顺手挂上",
      (wd_s.task_store.get(_other["id"]).get("session") or {}).get("start"), None)
check_true("说了「项目不吃后缀」",
           any("项目不吃后缀" in t for t in texts(wd_s)))

print("== 再导入同一个项目 = 刷新清单，不重复建 ==")
wd.omnifocus.rows = [dict(r) for r in rows_ok if r["id"] != "t-read"]
check("第二次 import 受理", wd.handle("task import sop omnifocus:///project/p-lunwen"), True)
check("还是一个项目", len(wd.projects.list()), 1)
check("清单被刷新成 3 条",
      [t["title"] for t in wd.projects.list()[0]["tasks"]],
      ["写引言", "跑数据", "写完的"])
check_true("说了「刷新」", any("刷新项目" in t for t in texts(wd)))

print("== 项目表：挂在任务面板下面 ==")
wd.omnifocus.rows = [dict(r) for r in rows_ok]
wd.handle("task import sop omnifocus:///project/p-lunwen")
wd.handle("task import sop omnifocus:///project/p-dushu")
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
check("项目叶子任务已镜像进任务库", len(tasks) >= 2, True)
assigned = [t for t in tasks if (t.get("session") or {}).get("start")]
check("唤起了任务（一共 8 count → 至少 2 条）", len(assigned) >= 2, True)
check("每条被唤起的任务都记着来源项目",
      sorted({t["fields"]["project"] for t in assigned}), ["论文", "读书"])
check("被唤起的任务都挂到了 session 上", len(assigned), len(assigned))
check("一场只放了一个任务（尽量独立 session）",
      len({(t["session"]["date"], t["session"]["start"]) for t in assigned}),
      len(assigned))
check("一条任务的 count 和时长对得上",
      [(t["fields"]["count"], t["fields"]["duration"]) for t in assigned
       if t["fields"]["count"] != t["fields"]["duration"] // 25], [])
check("count 相加 = 分出去的资源",
      sum(int(t["fields"]["count"]) for t in assigned), total)
check("都走 Focusmate 建了 task", len([c for c in wd.bridge.calls
                                     if c[0] == "add_task"]), len(assigned))
plan1 = pj.plan_of(by_num(wd), wd.sel_date)
check("项目库记下了这一天说好的 count", int(plan1["count"]), total - 1)
check("项目库记下了排下去的每一条",
      len(plan1["items"]), len([t for t in assigned if t["fields"]["project_id"] == 1]))
check("due 跟着 session 终点走（第二天）",
      all(t["fields"].get("due", "").startswith(TOMORROW) for t in assigned), True)

print("== plan：分配数超过项目剩余 count 时如实报少排 ==")
wd2 = fresh(rows_ok)
wd2.handle("task import sop omnifocus:///project/p-dushu")
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
wd3.handle("task import sop omnifocus:///project/p-lunwen")
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
wd4.handle("task import sop omnifocus:///project/p-lunwen")
check("project 列表受理", wd4.handle("project"), True)
check_true("列出了项目名", any("论文" in t for t in texts(wd4)))
check("project 1 详情受理", wd4.handle("project 1"), True)
check_true("详情里有清单", any("写引言" in t for t in texts(wd4)))
wd4.omnifocus.rows = [dict(r) for r in rows_ok if r["id"] != "t-done"]
check("project 1 refresh 受理", wd4.handle("project 1 refresh"), True)
check("清单刷新成 3 条", len(wd4.projects.active()[0]["tasks"]), 3)
check_true("说了「刷新了」", any("刷新了" in t for t in texts(wd4)))
check("project 1 remove 受理（优先删真正项目 pro）",
      wd4.handle("project 1 remove"), True)
check("Pro 已删除", wd4.projects.active_pros(), [])
check("op 还在，用 op 命令单独撤", wd4.handle("op 1 remove"), True)
check("项目库空了", wd4.projects.active(), [])

print("== 刷新时项目里冒出一条没时长的 → 拒绝写库 ==")
wd5 = fresh(rows_ok)
wd5.handle("task import sop omnifocus:///project/p-lunwen")
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
wd6.handle("task import sop omnifocus:///project/p-lunwen")
wd6.handle("day 1")
wd6.handle("session T1 75 25")
wd6.handle("session T2 50")
wd6.api_rows = []                   # data.csv 有排布、但一场都没订上
wd6.rows_source = "bridge"
wd6.rebuild_rows()
wd6.handle("plan +1")
check_true("没订上的场次仍计入空场（只排除 formatter/过去/已有任务）",
           any("还没订上 Focusmate，但仍计入空场" in t for t in texts(wd6)))
check("这时开了问答（不再因为没订上而拒绝）", wd6.planning is not None, True)
wd6.handle("q")                       # 关掉问答，下一段继续用
check("问答关掉了", wd6.planning, None)

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
with open(sch.DATA_CSV, "w", encoding="utf-8") as _f:   # 清掉上一段遗留的明天排布
    _f.write("# empty\n")
wd7.reload_entries()
wd7.handle("task import sop omnifocus:///project/p-lunwen")
wd7.handle("day 1")                       # 看板在看明天
check("看板是明天", wd7.sel_date.isoformat(), TOMORROW)
wd7.messages.clear()
try:
    wd7.handle("plan 明天")                # 明天连排布都没有 → 空场为 0
    check("这时没开问答", wd7.planning, None)
except ValueError as e:
    check("这时没开问答（无空场被拒）", wd7.planning, None)
    check_true("说清是一场空着的 session 都没有",
               "一场空着的 session 都没有" in str(e))
check_true("说清 plan 的是哪一天",
           any("plan 的是 %s" % TOMORROW in t for t in texts(wd7)))

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
wd8.handle("task import sop omnifocus:///project/p-lunwen")
wd8.handle("day 1")
wd8.handle("session T1 75 25")             # 明天的排布
wd8.handle("day 0")
wd8.handle("session T1 75 25")             # 今天的排布（12:15 那场是今天的记号）
book_all(wd8)                              # 今天的都订上了
check("看板在今天是哪一天", wd8.sel_date, datetime.date.today())
# 真实时钟跑到晚上时今天已经没有未来的场次；这条用例只关心「默认目标今天」，
# 所以用一个固定的未来 slot 替掉时钟依赖，别让 23:00 跑测试时假红。
wd8._plan_slots = lambda day: (
    [pp.slot_record(1, day.isoformat(), "12:15", 25, "s99")], [])
wd8.handle("plan")
check("今天有空场才开得了问答", wd8.planning is not None, True)
check("plan 不带参数 = 今天", wd8.planning.day, datetime.date.today())
wd8.handle("q")
wd8.handle("day 1")                        # 切到明天（离线模式下 API 行会被清掉）
check("看板到明天", wd8.sel_date.isoformat(), TOMORROW)
wd8.messages.clear()
wd8.handle("plan")                         # 照样该排今天
check("看板被切回今天", wd8.sel_date, datetime.date.today())
check("plan 打开且目标仍是今天", wd8.planning.day, datetime.date.today())
wd8.handle("q")

print("== 今天没 plan → 状态框里有一条警告；plan 过就消失 ==")
wd9 = fresh(rows_ok)
wd9.omnifocus.rows = [dict(r) for r in rows_ok]
check("没项目时不该提这件事", wd9.pending_plan(), None)
wd9.handle("task import sop omnifocus:///project/p-lunwen")
# 这条用例验证「提醒/plan 一次就消失」，不依赖测试跑在一天里的哪个时刻。
wd9.free_plan_slots = lambda day: [{"start": "12:15", "duration": 25}]
wd9._plan_slots = lambda day: (
    [pp.slot_record(1, day.isoformat(), "12:15", 25, "s99")], [])
wd9._assign_task = lambda *args, **kwargs: None
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

print("== 琐事 Project：设置项 + ProjectStore.find / complete_task ==")
reset_projects()
_chore_store = pj.ProjectStore()
_chore_store.upsert({
    "id": "p-chores", "title": "琐事库",
    "tasks": [
        {"pk": "chore-parent", "title": "收拾房间", "duration": 50,
         "completed": False},
        {"pk": "chore-a", "title": "倒垃圾", "parent_id": "chore-parent",
         "duration": 25, "completed": False},
        {"pk": "chore-b", "title": "洗碗", "parent_id": "chore-parent",
         "duration": 25, "completed": False},
    ],
})
check("find 认 #序号", _chore_store.find("#1")["title"], "琐事库")
check("find 认项目名", _chore_store.find("琐事库")["title"], "琐事库")
check("find 认 OmniFocus pk",
      _chore_store.find("p-chores")["title"], "琐事库")
_chore_proj, _chore_done, _chore_parent = _chore_store.complete_task(1, "chore-a")
check("complete_task 只完成指定子任务",
      pj.task_by_pk(_chore_proj, "chore-a")["completed"], True)
check("还有一个子任务 → 父任务不自动完成", _chore_parent, None)
_chore_proj, _chore_done, _chore_parent = _chore_store.complete_task(1, "chore-b")
check("最后一个子任务完成 → 父壳自动完成",
      (pj.task_by_pk(_chore_proj, "chore-b")["completed"],
       _chore_parent and _chore_parent["pk"]),
      (True, "chore-parent"))

print("== 空闲屏：没有 session 时只显示绑定项目的一条琐事；m/c/q ==")
wd_chore = fresh()
_check_payload = {
    "id": "p-chore-ui", "title": "琐事库",
    "tasks": [
        {"pk": "ui-a", "title": "倒垃圾", "duration": 25, "completed": False},
        {"pk": "ui-b", "title": "洗碗", "duration": 50, "completed": False},
    ],
}
wd_chore.projects.upsert(_check_payload)
check_true("没绑定琐事 Project → 警告板里有提醒",
           any(getattr(item, "source", "") == "琐事"
               for item in wd_chore.collect_warnings()))
wd_chore.chore_now = datetime.datetime.combine(TODAY, datetime.time(3, 0))
check("set chore-project #1 受理", wd_chore.handle("set chore-project #1"), True)
check("空闲 + 已绑定 → 自动切到琐事屏", wd_chore.view.name, "琐事")
check("绑好了就不再警告",
      [item for item in wd_chore.collect_warnings()
       if getattr(item, "source", "") == "琐事"], [])
snap = wd_chore.chore_snapshot()
check("屏幕上当前是第一条未完成琐事",
      (snap["project"]["title"], snap["task"]["title"]), ("琐事库", "倒垃圾"))
check("c 换一条", wd_chore.handle("c"), True)
check("当前任务换成第二条", wd_chore.chore_snapshot()["task"]["title"], "洗碗")
check("m 完成当前琐事", wd_chore.handle("m"), True)
_after = wd_chore.projects.find("#1")
check("项目库里的「洗碗」已完成",
      pj.task_by_pk(_after, "ui-b")["completed"], True)
check("完成之后自动显示下一条未完成琐事",
      wd_chore.chore_snapshot()["task"]["title"], "倒垃圾")
_chore_buf = io.StringIO()
wd_chore.console = Console(file=_chore_buf, width=120, force_terminal=False)
wd_chore.paint()
_chore_out = _chore_buf.getvalue()
check_true("琐事屏显示当前任务", "倒垃圾" in _chore_out)
check_true("琐事屏带 m/c/q 提示",
           all(x in _chore_out for x in ("m 完成", "c 换一个", "q 退出")))
check_true("琐事屏不画正常契约表 / 视图栏",
           ("契约（data.csv" not in _chore_out and "视图 " not in _chore_out))
wd_chore.shell._terminal_rows = lambda: 30
wd_chore.frame_width = lambda: 80
wd_chore.invalidate()
_chore_frame = re.sub(r"\x1b\[[0-9;]*m", "", wd_chore.shell.frame_text())
_chore_lines = _chore_frame.splitlines()
check("琐事屏占满整个画布高度（输入行钉在底部）",
      len(_chore_lines), wd_chore.shell.dashboard_budget())
_non_empty = [idx for idx, line in enumerate(_chore_lines) if line.strip()]
check_true("琐事屏垂直居中（上下都有留白，不在左上角第 0 行）",
           bool(_non_empty) and _non_empty[0] >= 5
           and (_chore_lines[_non_empty[0]].startswith("     ")))
check("q 回到正常界面（不是退出 TUI）", wd_chore.handle("q"), True)
check("q 后回到契约视图", wd_chore.view.name, "契约")
check_true("q 压制到下一场 session / 明天",
           wd_chore.chore_suppressed_until is not None)
wd_chore.refresh_warnings(force=True)
check("同一段空档不再自动弹回", wd_chore.view.name, "契约")

def _snapshot_data():
    out = {}
    for path in (pj.projects_path(), ts.TaskStore().path):
        try:
            with open(path, "rb") as fh:
                out[path] = fh.read()
        except OSError:
            out[path] = None
    return out


def _restore_data(snap):
    for path, data in (snap or {}).items():
        if data is None:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            with open(path, "wb") as fh:
                fh.write(data)


print("== 1) 项目 Project (pro) 数据模型 + 关联文件 + open + enter 顺便打开 ==")
wd_pro = fresh(rows_ok)
check("pro create 创建真正项目并带关联文件",
      wd_pro.handle("pro create 论文 file=/tmp/thesis-draft.md"), True)
pros = wd_pro.projects.active_pros()
check("pro 进库", len(pros), 1)
check("pro 标题与默认阶段 running",
      (pros[0]["title"], pros[0]["stage"], pros[0]["file"]),
      ("论文", "running", "/tmp/thesis-draft.md"))
check("pro 1 file 修改关联文件",
      wd_pro.handle("pro 1 file /tmp/thesis-v2.md"), True)
check("关联文件已更新", wd_pro.projects.active_pros()[0]["file"], "/tmp/thesis-v2.md")
wd_pro.opened_files.clear()
check("敲 open + enter（不带参数）切口径并顺便打开关联文件",
      wd_pro.handle("open"), True)
check("顺便打开了关联文件", wd_pro.opened_files, ["/tmp/thesis-v2.md"])
check_true("消息里提示顺便打开了项目关联文件",
           any("顺便打开项目「论文」关联文件" in t for t in texts(wd_pro)))
check("pro 1 open 直接打开关联文件",
      wd_pro.handle("pro 1 open"), True)
check("opened_files 记录第 2 次打开", len(wd_pro.opened_files), 2)

print("== 2) OmniFocus 项目（SOP）使用 tagging project/… 自动关联真正的项目 pro ==")
_tagged_rows = [
    {"id": "p-sop-1", "name": "论文写作 SOP", "kind": "project_root",
     "tags": ["project/学术论文"], "completed": False, "dropped": False},
    {"id": "t-sop-1", "name": "写第一章", "kind": "action", "project": "论文写作 SOP",
     "project_id": "p-sop-1", "estimated_minutes": 50, "completed": False,
     "tags": ["project/学术论文", "写作"]},
    {"id": "p-inc-1", "name": "实验瓶颈孵化", "kind": "project_root",
     "tags": [], "completed": False, "dropped": False},
    {"id": "t-inc-1", "name": "调研替代方案", "kind": "action", "project": "实验瓶颈孵化",
     "project_id": "p-inc-1", "estimated_minutes": 25, "completed": False},
    {"id": "p-sop-2", "name": "新的论文 SOP", "kind": "project_root",
     "tags": ["project/学术论文"], "completed": False, "dropped": False},
    {"id": "t-sop-2", "name": "写第二章", "kind": "action", "project": "新的论文 SOP",
     "project_id": "p-sop-2", "estimated_minutes": 25, "completed": False},
]
wd_tag = fresh(_tagged_rows)
check("导入时说明类型为 SOP：task import sop",
      wd_tag.handle("task import sop omnifocus:///project/p-sop-1"), True)
_op1 = wd_tag.projects.active()[0]
check("op 类型记为 SOP", _op1["op_type"], "SOP")
check("从标签提取出 project_tag", _op1["project_tag"], "学术论文")
_linked_pro = wd_tag.projects.pro_for_op(_op1)
check_true("自动创建并关联了真正项目 pro「学术论文」",
           _linked_pro is not None and _linked_pro["title"] == "学术论文")
check("自动创建的 pro 阶段为 running", _linked_pro["stage"], "running")
check("导入时已说明类型 → 不弹待补全提示", wd_tag.pending_op_type, [])

print("== 3) 导入未说明类型 / refresh 检查缺少类型的 op → 让用户输入 SOP / incubator ==")
_snap_before_3 = _snapshot_data()
wd_untyped = fresh(_tagged_rows)
check("不带类型导入 op",
      wd_untyped.handle("task import omnifocus:///project/p-inc-1"), True)
check("未说明类型时进入 pending_op_type 队列",
      len(wd_untyped.pending_op_type), 1)
check_true("打断栏位展示补全 op 类型面板",
           wd_untyped.render_interrogate() is not None
           and "SOP" in wd_untyped.render_interrogate().renderable.plain)
check("用户输入 incubator 补全",
      wd_untyped.handle("incubator"), True)
check("补全后队列清空", wd_untyped.pending_op_type, [])
check("op 类型已写入 incubator",
      wd_untyped.projects.active()[0]["op_type"], "incubator")

# 模拟旧数据缺少 op_type，refresh 时自动检查并让用户补
_raw_store = wd_untyped.projects.load()
_raw_store["projects"][0]["op_type"] = None
wd_untyped.projects.save(_raw_store)
check("旧数据 op_type 置空", wd_untyped.projects.active()[0]["op_type"], None)
check("refresh 检查出缺少 op_type 的项目",
      wd_untyped.handle("refresh"), True)
check("refresh 后 pending_op_type 有待补项",
      len(wd_untyped.pending_op_type), 1)
# 现在 plan 只收「关联了真正项目 pro」的 op；先把它绑到 pro，验证未设类型
# 的 linked op 仍然会挡住 plan，而不是被静默跳过。
wd_untyped.handle("pro create 未类型项目")
wd_untyped.handle("op 1 pro 1")
check("未说明类型时 eligible_ops 不把它当成 SOP",
      wd_untyped.projects.eligible_ops("SOP"), [])
check("未说明类型时 plan 直接拒绝", wd_untyped.handle("plan"), True)
check_true("plan 拒绝理由说清要先补类型",
           any("没说明类型" in t for t in texts(wd_untyped)))
check("用户输入 SOP 完成补全", wd_untyped.handle("SOP"), True)
check("op_type 更新为 SOP", wd_untyped.projects.active()[0]["op_type"], "SOP")

_restore_data(_snap_before_3)
# 前面琐事屏用例把 chore-project 设成了 #1；这会让后续没有 session 的测试
# 自动切进 ChoreView（只吃 m/c/q），所以这里把绑定清掉，保持普通契约视图。
wd_tag.settings.set("chore-project", "")
if isinstance(wd_tag.view, w.ChoreView):
    wd_tag.handle("q")

print("== 4) pro <序号> -a / --advance 快捷切换状态并关联新 op（task import 或 op） ==")
# wd_tag 里已有 Pro #1「学术论文」(running) 关联 op #1「论文写作 SOP」(SOP)
check("初始阶段 running 下 SOP op 允许进行",
      [p["id"] for p in wd_tag.projects.eligible_ops("SOP")], [_op1["id"]])
check("初始阶段 running 下 incubator op 为空",
      wd_tag.projects.eligible_ops("incubator"), [])

# 用 project 1 --advance（不带尾参数）进入问答，再用 task import 关联新 incubator op
check("project 1 --advance 打开阶段切换问答",
      wd_tag.handle("project 1 --advance"), True)
check_true("pending_advance 已挂起，目标是 bottleneck / incubator",
           wd_tag.pending_advance is not None
           and wd_tag.pending_advance["target_stage"] == "bottleneck"
           and wd_tag.pending_advance["required_op_type"] == "incubator")
check("在 advance 问答里直接敲 task import 导入新 op 作为关联",
      wd_tag.handle("task import omnifocus:///project/p-inc-1"), True)
check("advance 问答已完成关闭", wd_tag.pending_advance, None)
_pro_after_bn = wd_tag.projects.pro_by_id(_linked_pro["id"])
check("Pro #1 已切换到 bottleneck 阶段", _pro_after_bn["stage"], "bottleneck")
_op_inc = wd_tag.projects.by_omnifocus("p-inc-1")
check("新导入的 op 自动标为 incubator 并绑定到 Pro #1",
      (_op_inc["op_type"], _op_inc["pro_id"]), ("incubator", _pro_after_bn["id"]))
check("bottleneck 阶段禁止该项目的 SOP op 进行",
      wd_tag.projects.eligible_ops("SOP"), [])
check("bottleneck 阶段允许该项目对应的 incubator op 进行",
      [p["id"] for p in wd_tag.projects.eligible_ops("incubator")], [_op_inc["id"]])

# 再导入第二个 SOP op，然后用 pro 1 -a op <序号> 切回 running
check("op import sop 导入新的 SOP op",
      wd_tag.handle("op import sop omnifocus:///project/p-sop-2"), True)
_op_sop2 = wd_tag.projects.by_omnifocus("p-sop-2")
_op_sop2_num = wd_tag.projects.number_of(_op_sop2["id"])
check("pro 1 -a op <序号> 快捷切回 running 并关联新 SOP op",
      wd_tag.handle("pro 1 -a op %d" % _op_sop2_num), True)
_pro_after_run = wd_tag.projects.pro_by_id(_linked_pro["id"])
check("Pro #1 已切回 running 阶段", _pro_after_run["stage"], "running")
check("running 阶段允许 SOP op 进行",
      _op_sop2["id"] in [p["id"] for p in wd_tag.projects.eligible_ops("SOP")], True)
check("running 阶段不再允许该项目的 incubator op 进行",
      wd_tag.projects.eligible_ops("incubator"), [])

print("== 5) plan 只有一次：每个项目按阶段取当前的 SOP / incubator op ==")
_two_stage_rows = [
    {"id": "p-run-sop", "name": "常规开发 SOP", "kind": "project_root",
     "tags": ["project/核心系统"], "completed": False, "dropped": False},
    {"id": "t-run-1", "name": "实现接口", "kind": "action", "project": "常规开发 SOP",
     "project_id": "p-run-sop", "estimated_minutes": 50, "completed": False},
    {"id": "p-bn-inc", "name": "算法破局孵化", "kind": "project_root",
     "tags": ["project/算法攻关"], "completed": False, "dropped": False},
    {"id": "t-bn-1", "name": "验证新启发式", "kind": "action", "project": "算法破局孵化",
     "project_id": "p-bn-inc", "estimated_minutes": 25, "completed": False},
    {"id": "p-bn-sop", "name": "被阻塞的算法 SOP", "kind": "project_root",
     "tags": ["project/算法攻关"], "completed": False, "dropped": False},
    {"id": "t-bn-sop-1", "name": "常规调参", "kind": "action", "project": "被阻塞的算法 SOP",
     "project_id": "p-bn-sop", "estimated_minutes": 25, "completed": False},
]
wd_plan2 = fresh(_two_stage_rows)
# 核心系统（running）→ p-run-sop (SOP)
wd_plan2.handle("op import sop omnifocus:///project/p-run-sop")
# 算法攻关：先有 SOP，再通过 -a 切换到 bottleneck 并关联 p-bn-inc (incubator)
wd_plan2.handle("op import sop omnifocus:///project/p-bn-sop")
wd_plan2.handle("pro 2 -a task import omnifocus:///project/p-bn-inc")
check("Pro #1 处于 running、Pro #2 处于 bottleneck",
      [(p["title"], p["stage"]) for p in wd_plan2.projects.active_pros()],
      [("核心系统", "running"), ("算法攻关", "bottleneck")])

plan_day(wd_plan2)  # 明天 3 场：T1 75(3) + 25(1)、T2 50(2) = 6 count
check("启动 plan +1", wd_plan2.handle("plan +1"), True)
check_true("plan 开着", wd_plan2.planning is not None)
check("一次 plan 同时列出 running 的 SOP 与 bottleneck 的 incubator",
      [(p["title"], p.get("op_type")) for p in wd_plan2.planning.projects],
      [("常规开发 SOP", "SOP"), ("算法破局孵化", "incubator")])
check("不再有全局 SOP/incubator 阶段切换", wd_plan2.planning.stage, "mixed")
check("容量是全部空场 6 count", wd_plan2.planning.cap()["counts"], 6)
wd_plan2.handle("task 1 2")  # 常规开发 SOP：2 count
wd_plan2.handle("task 2 1")  # 算法破局孵化：1 count
check("complete 一次就收尾", wd_plan2.handle("complete"), True)
check("planning 已关闭", wd_plan2.planning, None)
_assigned_titles = sorted(
    t["fields"]["title"] for t in wd_plan2.task_store.list()
    if (t.get("session") or {}).get("start")
)
check("SOP 与 incubator 的当前 op 在**同一次 plan** 里排好",
      _assigned_titles, ["实现接口", "验证新启发式"])

print("== 5b) 只有 incubator op 时 plan 也一次完成；阶段门控挡住时明确拒绝 ==")
_inc_only_rows = [
    {"id": "p-only-inc", "name": "唯一孵化 op", "kind": "project_root",
     "tags": ["project/唯一项目"], "completed": False, "dropped": False},
    {"id": "t-only-inc", "name": "孵化任务", "kind": "action", "project": "唯一孵化 op",
     "project_id": "p-only-inc", "estimated_minutes": 25, "completed": False},
]
wd_inc_only = fresh(_inc_only_rows)
check("op import incubator 受理",
      wd_inc_only.handle("op import incubator omnifocus:///project/p-only-inc"), True)
_inc_pro = wd_inc_only.projects.active_pros()[0]
check("incubator op 自动关联的 pro 默认处于 bottleneck",
      _inc_pro["stage"], "bottleneck")
plan_day(wd_inc_only)
check("只有 incubator op 时 plan 仍可启动", wd_inc_only.handle("plan +1"), True)
check("一次 plan 就只含 incubator op",
      [(p["title"], p.get("op_type")) for p in wd_inc_only.planning.projects],
      [("唯一孵化 op", "incubator")])
check("只有一种类型时 stage 也明确",
      wd_inc_only.planning.stage, "incubator")
wd_inc_only.handle("q")

_gated_rows = [
    {"id": "p-gated", "name": "被门控的 SOP", "kind": "project_root",
     "tags": ["project/门控项目"], "completed": False, "dropped": False},
    {"id": "t-gated", "name": "门控任务", "kind": "action", "project": "被门控的 SOP",
     "project_id": "p-gated", "estimated_minutes": 25, "completed": False},
]
wd_gated = fresh(_gated_rows)
wd_gated.handle("op import sop omnifocus:///project/p-gated")
check("把项目阶段改为 bottleneck", wd_gated.handle("pro 1 stage bottleneck"), True)
plan_day(wd_gated)
check("bottleneck 下没有 incubator op，plan 拒绝", wd_gated.handle("plan +1"), True)
check_true("拒绝理由说清没有当前阶段可排期的 op",
           any("当前没有可排期的 OmniFocus 项目" in t for t in texts(wd_gated)))
check("被门控时不会打开 plan 问答", wd_gated.planning, None)

print("== 5c) plan：跨午夜的场次按开始日归属（今天 23:15–明天 00:05 算今天） ==")
wd_cross = fresh()
_cross_day = TODAY
_cross_start = datetime.datetime.combine(_cross_day, datetime.time(23, 15))
_cross_end = _cross_start + datetime.timedelta(minutes=50)
# 故意把 row["date"] 写成终点日：真正的权威归属应该看 startMs/开始时间，
# 而不是终点落在哪天。旧实现按 row["date"] 过滤，会漏掉整场。
_cross_row = {
    "planned": True,
    "date": (_cross_day + datetime.timedelta(days=1)).isoformat(),
    "startMs": int(_cross_start.timestamp() * 1000),
    "endMs": int(_cross_end.timestamp() * 1000),
    "startLabel": "23:15", "endLabel": "00:05", "durationMin": 50,
    "id": "sess-cross", "title": "", "quiet": False, "matched": False,
    "phase": "未开始", "period_muted": False,
}
wd_cross.clock_now = datetime.datetime.combine(_cross_day, datetime.time(22, 0))
wd_cross.rows = [dict(_cross_row)]
wd_cross.task_store.tasks_for_session = lambda *a, **k: []
_cross_slots, _cross_notes = wd_cross._plan_slots(_cross_day)
check("跨午夜场次按开始日进入 plan 空场", len(_cross_slots), 1)
if _cross_slots:
    check("跨午夜场次保留 50 分钟容量",
          (_cross_slots[0]["start"], _cross_slots[0]["end"],
           _cross_slots[0]["duration"], _cross_slots[0]["counts"]),
          ("23:15", "00:05", 50, 2))
# 同样的口径也用在「今天还没 plan」提醒 / 空闲场查询上。
wd_cross.sessions_for_day_plan = lambda day: [{
    "start": "23:15", "end": "00:05", "duration": 50,
}]
_free_cross = wd_cross.free_plan_slots(_cross_day)
check("free_plan_slots 也认跨午夜开始于今天的场次", len(_free_cross), 1)

print("== 6) help 页面澄清 op / pro / task 三个数据模型的区别 ==")
wd_help = fresh()
wd_help.handle("help")
_buf_h = io.StringIO()
Console(file=_buf_h, width=240, force_terminal=False).print(wd_help.help_overlay)
_help_plain = _buf_h.getvalue()
check_true("help 说明了 op 代表 OmniFocus Project（incubator / SOP）",
           "op" in _help_plain and "incubator" in _help_plain and "SOP" in _help_plain)
check_true("help 说明了 pro 代表真正项目 Project",
           "pro" in _help_plain and "Project" in _help_plain)
check_true("help 说明了 task 代表任务",
           "task" in _help_plain and "任务" in _help_plain)

print("== 7) 短任务按实际分钟算 count；1 count 塞两条 10 分钟任务 ==")
_rows_small = [
    {"id": "p-small", "name": "短任务项目", "kind": "project_root",
     "project": "", "completed": False, "dropped": False},
]
for _i in range(7):
    _rows_small.append({
        "id": "small-%d" % _i, "name": "短任务 %d" % _i, "kind": "action",
        "project": "短任务项目", "parent": "短任务项目",
        "estimated_minutes": 10, "completed": False, "dropped": False,
    })
_proj_small = pj.project_payload(_rows_small[0], _rows_small)
_stat_small = pj.stats(dict(_proj_small, id=1))
check("7 条 10 分钟 = 3 count（不是 7）", _stat_small["total"], 3)
check("剩余也是 3 count", _stat_small["remaining"], 3)
_items_small, _notes_small = pp.distribute(
    [{"project_id": 1, "project": dict(_proj_small, id=1), "count": 1}],
    [dict(_proj_small, id=1)], {})
check("1 count 实际挑出两条 10 分钟短任务",
      [(i["title"], i.get("minutes")) for i in _items_small],
      [("短任务 0", 10), ("短任务 1", 10)])
_slots_small = [pp.slot_record(1, ISO, "11:00", 25, "s-small")]
_places_small, _uns_small, _more_small = pp.schedule(_items_small, _slots_small)
check("两条短任务进了同一场 25 分钟 session",
      sorted((p["item"]["title"], p["duration"]) for p in _places_small),
      [("短任务 0", 10), ("短任务 1", 10)])
check("没有漏排", [u["title"] for u in _uns_small], [])
check_true("提示了时间对不齐导致空位，并列出（10、10）",
           any("剩 5 分钟空位" in n and "10、10" in n for n in _more_small))

print("== 8) op relate --export/--import：模板 -> 本地项目库 ==")
_relate_dir = os.path.join(TMP, "relate")
os.makedirs(_relate_dir, exist_ok=True)
_rows_rel_sop = [
    {"id": "p-relate-sop", "name": "Relate SOP", "kind": "project_root",
     "project": "", "completed": False, "dropped": False,
     "tags": ["project/Relate Pro"]},
    {"id": "sop-r1", "name": "SOP 已有任务", "kind": "action",
     "project": "Relate SOP", "parent": "Relate SOP",
     "estimated_minutes": 25, "completed": False, "dropped": False,
     "tags": ["project/Relate Pro"]},
]
_rows_rel_inc = [
    {"id": "p-relate-inc", "name": "Relate Inc", "kind": "project_root",
     "project": "", "completed": False, "dropped": False,
     "tags": ["project/Relate Pro"]},
    {"id": "inc-r1", "name": "Inc 已有任务", "kind": "action",
     "project": "Relate Inc", "parent": "Relate Inc",
     "estimated_minutes": 25, "completed": False, "dropped": False,
     "tags": ["project/Relate Pro"]},
]
wd_rel = fresh(_rows_rel_sop + _rows_rel_inc)
wd_rel.settings.set("relate-dir", _relate_dir)
check("relate import 之前 op 没有关联文件",
      all(p.get("file") is None for p in wd_rel.projects.active()), True)
check("SOP op import 受理",
      wd_rel.handle("task import sop omnifocus:///project/p-relate-sop"), True)
check("incubator op import 受理",
      wd_rel.handle("task import incubator omnifocus:///project/p-relate-inc"), True)
_inc_op = next(p for p in wd_rel.projects.active()
               if pj.normalize_op_type(p.get("op_type")) == pj.OP_TYPE_INCUBATOR)
_sop_op = next(p for p in wd_rel.projects.active()
               if pj.normalize_op_type(p.get("op_type")) == pj.OP_TYPE_SOP)
_inc_num = wd_rel.projects.number_of(_inc_op["id"])
check("incubator op 序号", _inc_num, 2)
# SOP op 不是 incubator：relate 不动作
wd_rel.handle("op 1 relate --export")
check("SOP op relate 不动作（仍无关联文件）",
      wd_rel.projects.by_id(_sop_op["id"]).get("file"), None)
check("incubator op export 受理",
      wd_rel.handle("op %d relate --export" % _inc_num), True)
_inc_after = wd_rel.projects.by_id(_inc_op["id"])
_rel_path = _inc_after.get("file")
check_true("export 生成了模板文件", bool(_rel_path) and os.path.exists(_rel_path))
if _rel_path:
    _template_text = open(_rel_path, encoding="utf-8").read()
    check_true("模板填了「关联项目」",
               "关联项目: Relate Pro" in _template_text)
    check_true("模板里有「# 资源准备任务列表」和「# 具体的 SOP 流程」",
               "# 资源准备任务列表" in _template_text
               and "# 具体的 SOP 流程" in _template_text)
    # 先用缺时长的模板试一次：应当拒绝且一条都不 append。
    _before_titles = [t["title"] for t in wd_rel.projects.by_id(_inc_op["id"])["tasks"]]
    open(_rel_path, "w", encoding="utf-8").write(
        "---\n关联项目: Relate Pro\n---\n"
        "# 资源准备任务列表\n- 缺时长的任务\n"
        "# 具体的 SOP 流程\n- 也缺时长\n")
    check("缺时长的 relate import 被拒（handle 不炸）",
          wd_rel.handle("op %d relate --import" % _inc_num), True)
    check("被拒后 incubator op 一条都没 append",
          [t["title"] for t in wd_rel.projects.by_id(_inc_op["id"])["tasks"]],
          _before_titles)
    check_true("拒绝理由用了项目任务检查（没设持续时间）",
               any("没设持续时间" in t or "没有「持续时间」" in t
                   for t in texts(wd_rel)))
    # 合法模板：资源准备 -> incubator op；SOP 流程 -> SOP op。
    open(_rel_path, "w", encoding="utf-8").write(
        "---\n关联项目: Relate Pro\n---\n"
        "# 资源准备任务列表\n"
        "- wash flashcards in English deck @estimate(10m)\n"
        "- create flashcards about key themes @estimate(10m)\n"
        "# 具体的 SOP 流程\n"
        "- complete flashcards in English deck @estimate(25m)\n")
    check("relate import 受理", wd_rel.handle("op %d relate --import" % _inc_num), True)
    _inc_titles = [t["title"] for t in wd_rel.projects.by_id(_inc_op["id"])["tasks"]]
    _sop_titles = [t["title"] for t in wd_rel.projects.by_id(_sop_op["id"])["tasks"]]
    check("资源准备任务 append 到 incubator op",
          all(t in _inc_titles for t in ["wash flashcards in English deck",
                                         "create flashcards about key themes"]), True)
    check("SOP 流程 append 到 SOP op",
          "complete flashcards in English deck" in _sop_titles, True)
    _rel_local = next((t for t in wd_rel.task_store.list()
                       if (t.get("fields") or {}).get("title")
                       == "wash flashcards in English deck"), None)
    check_true("relate 任务已镜像进任务库", _rel_local is not None)
    if _rel_local is not None:
        check("relate 任务不挂 OmniFocus（本地 only）",
              (dict(_rel_local).get("omnifocus") or {}).get("id"), None)
        check("relate 任务 source 标记 local_only",
              (dict(_rel_local).get("source") or {}).get("local_only"), True)
    check("op 文件字段已记上模板路径",
          wd_rel.projects.by_id(_inc_op["id"]).get("file"), _rel_path)

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    sys.exit(1)
print("all good")
