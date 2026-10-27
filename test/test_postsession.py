#!/usr/bin/env python3
"""postsession.py + omnifocus.py + warden 收尾/OmniFocus 联动的回归测试。

    python3 test/test_postsession.py     # 全绿退出 0，有失败退出 1

覆盖四块（对应需求 1~4）：

1. 完成/放弃的任务**不隐藏**，只是打状态标记（✓ / ✗ / ⤼）；
2. 任务被分配到 session 上，截止时间＝那场的终点，完成时写回 OmniFocus；
3. session 结束 +15 秒：daemon 入队 + 拉 warden，warden 逐个任务问 A~E；
4. 粘贴 `[标题](omnifocus:///task/PK)` 就能把本地任务关联到 OmniFocus。

不联网、不起 bridge、不碰仓库里的真文件：tasks.json / 归档 / 设置 / 收尾状态
全部指到临时目录，OmniFocus 那侧用假的 Client。
"""
import datetime
import importlib.util
import io
import json
import os
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


def check_raises(label, fn):
    try:
        fn()
    except Exception:                              # noqa: BLE001 - 只要它抛
        print("ok   %s" % label)
        return
    fails.append("%s\n      没有抛异常" % label)
    print("FAIL %s" % label)


TMP = tempfile.mkdtemp(prefix="postsession-test-")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")

sys.path.insert(0, ROOT)
import postsession as psv  # noqa: E402
import task_store as ts  # noqa: E402
import omnifocus as of  # noqa: E402


def fresh_store():
    path = os.path.join(TMP, "tasks-%d.json" % len(os.listdir(TMP)))
    return ts.TaskStore(path, os.path.join(TMP, "archive-%d.json" % len(os.listdir(TMP))))


SESS = {"key": "2026-10-27T11:00", "date": "2026-10-27", "start": "11:00",
        "end": "11:50", "duration": 50}
NEXT = {"date": "2026-10-27", "start": "12:00", "end": "12:50", "duration": 50,
        "focusmate_session_id": "sess-2", "startMs": 1793088000000}


# ---------------------------------------------------------------- 链接解析

print("== omnifocus.parse_link：四种粘贴形状 ==")
item = of.parse_link("[数学易错修正](omnifocus:///task/kc-4VLAE_1L)")
check("Markdown 链接 → primary key", (item["id"], item["title"]), ("kc-4VLAE_1L", "数学易错修正"))
check("Markdown 链接 → url", item["url"], "omnifocus:///task/kc-4VLAE_1L")
item = of.parse_link("<omnifocus:///task/abc_1=>")
check("尖括号链接", item["id"], "abc_1=")
item = of.parse_link("omnifocus:///task/kc-4VLAE_1L")
check("裸链接没有标题", (item["id"], item["title"]), ("kc-4VLAE_1L", None))
item = of.parse_link('title: "数学易错修正"')
check("只有标题的粘贴：认得出标题", item["title"], "数学易错修正")
check("只有标题的粘贴：**没有** primary key", (item["id"], item["has_id"]), (None, False))
check("空字符串 → None", of.parse_link("   "), None)
check("多行粘贴拆成多条", len(of.parse_links(
    "[甲](omnifocus:///task/pk1)\n[乙](omnifocus:///task/pk2)")), 2)
check("is_link 只认有 id 的", (of.is_link("title: x"), of.is_link("omnifocus:///task/pk1")),
      (False, True))
html = '<a href="omnifocus:///task/kc-9">数学 &amp; 易错</a>'
check("HTML 剪贴板抓 href + 锚文本",
      [(i["id"], i["title"]) for i in of.html_links(html)], [("kc-9", "数学 & 易错")])


# ---------------------------------------------------------------- omni 客户端

print("== omnifocus.Client：退出码 → 异常 ==")


def fake_script(body):
    path = os.path.join(TMP, "fake_omnifocus_%d.py" % len(os.listdir(TMP)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("import sys, json\n" + body)
    return path


ROWS = [
    {"id": "pk1", "name": "写论文", "due": "2026-10-27T11:50:00", "completed": False},
    {"id": "pk2", "name": "复习", "due": None, "completed": True},
]
ok_script = fake_script(
    "for row in %r:\n    print(json.dumps(row, ensure_ascii=False))\n" % (ROWS,))
client = of.Client(script=ok_script)
check("list_tasks 读到两行", len(client.list_tasks()), 2)
check("get 按 primary key 找", client.get("pk2")["name"], "复习")
check("get 找不到 → None", client.get("nope"), None)
check("第二次读走缓存（不重复起子进程）", client.calls, 1)
client.list_tasks(refresh=True)
check("refresh=True 真的再读一次", client.calls, 2)


def client_with_rc(rc, out="", err="boom"):
    script = fake_script('sys.stderr.write(%r)\nsys.exit(%d)\n' % (err, rc))
    return of.Client(script=script)


try:
    client_with_rc(3).list_tasks()
    check("退出码 3 → permission", False, True)
except of.OmniFocusError as e:
    check("退出码 3 → permission", (e.kind, e.permission), ("permission", True))
    check_true("permission 会提示去系统设置", "自动化" in str(e))
for rc, kind in ((4, "dry_run"), (5, "not_found"), (6, "tag_problem"), (7, "project_root")):
    try:
        client_with_rc(rc).set_task("pk1", "--complete", yes=True)
        check("退出码 %d → %s" % (rc, kind), False, True)
    except of.OmniFocusError as e:
        check("退出码 %d → %s" % (rc, kind), e.kind, kind)
check("没有脚本 → unavailable",
      of.Client(script=os.path.join(TMP, "nope.py")).available(), False)
check_true("说清楚为什么不可用",
           "找不到" in of.Client(script=os.path.join(TMP, "nope.py")).why_unavailable())

write_script = fake_script(
    "print(json.dumps({'tool':'omnifocus_tasks_set','ok':True,'changed':['completed','due']}))\n")
res = of.Client(script=write_script).complete("pk1", due="2026-10-27T11:50:00")
check("complete 返回解析后的结果", res.get("changed"), ["completed", "due"])


# ---------------------------------------------------------------- 状态文件

print("== 收尾状态文件：pending / done ==")
psv.clear()
check("一开始没有待收尾的场次", psv.pending_sessions(), [])
psv.mark_pending(dict(SESS))
check("入队后就能查到", [r["key"] for r in psv.pending_sessions()], ["2026-10-27T11:00"])
check("is_pending 认得", psv.is_pending("2026-10-27T11:00"), True)
psv.mark_done(SESS, note="reviewed")
check("收完之后 pending 空了", psv.pending_sessions(), [])
check("收完之后记在 done 里", list(psv.load_state()["done"]), ["2026-10-27T11:00"])
check("done 里带 reviewed_at", bool(psv.load_state()["done"]["2026-10-27T11:00"]["reviewed_at"]), True)
psv.clear()


# ---------------------------------------------------------------- 谁该收尾

print("== ended_sessions：结束 +15 秒才催 ==")
day = datetime.date(2026, 10, 27)
slots = [{"start": "11:00", "hour": 11, "minute": 0, "duration": 50},
         {"start": "12:00", "hour": 12, "minute": 0, "duration": 25}]
early = datetime.datetime(2026, 10, 27, 11, 50, 14)
check("结束 14 秒 → 还没到点", psv.ended_sessions([(day, slots)], now=early), [])
on_time = datetime.datetime(2026, 10, 27, 11, 50, 15)
check("结束 15 秒 → 准点催",
      [r["key"] for r in psv.ended_sessions([(day, slots)], now=on_time)],
      ["2026-10-27T11:00"])
late = datetime.datetime(2026, 10, 27, 13, 30)
check("12:00 那场也在窗口里（25min → 12:25 结束）",
      [r["key"] for r in psv.ended_sessions([(day, slots)], now=late)],
      ["2026-10-27T11:00", "2026-10-27T12:00"])
stale = datetime.datetime(2026, 10, 27, 20, 0)
check("太老的不翻旧账（LOOKBACK 3h）", psv.ended_sessions([(day, slots)], now=stale), [])
check("slot 窗口算对", psv._slot_window(slots[0], day)[1].strftime("%H:%M"), "11:50")


# ---------------------------------------------------------------- Review 状态机

print("== Review：五个选项 ==")


def mk_review(titles=("写论文",), hooks=None, store=None):
    store = store or fresh_store()
    ids = []
    for title in titles:
        task = store.create({"title": title})
        store.assign(task["id"], SESS)
        ids.append(task["id"])
    review = psv.Review(store, SESS, store.tasks_for_session("2026-10-27", "11:00"),
                        hooks=hooks or {})
    return store, review, ids


# B 放弃：本地 + OmniFocus 都放弃
calls = []


def hooks_base(**extra):
    base = {"say": lambda text, kind="info": calls.append(("say", text))}
    base.update(extra)
    return base


store, review, ids = mk_review(hooks=hooks_base(
    drop_omnifocus=lambda task: calls.append(("omni-drop", task["id"])) or "OmniFocus 已放弃"))
store.link_omnifocus(ids[0], "omnifocus:///task/pk1")
review.tasks = store.tasks_for_session("2026-10-27", "11:00")
check("B 放弃：最后一条处理完 = 直接收工", review.handle("b"), {"action": "close"})
check("B：本地变 dropped", store.get(ids[0])["status"], ts.STATUS_DROPPED)
check("B：OmniFocus 也被叫去放弃", ("omni-drop", ids[0]) in calls, True)
check("B：收尾结束（没有更多任务）", (review.closed, review.pending()), (True, False))

# C 重议：复制一条，旧的放弃，OmniFocus 不动
calls = []
store, review, ids = mk_review(hooks=hooks_base(
    drop_omnifocus=lambda task: calls.append(("omni-drop", task["id"]))))
store.link_omnifocus(ids[0], "omnifocus:///task/pk1")
review.tasks = store.tasks_for_session("2026-10-27", "11:00")
review.handle("c")
copy = [t for t in store.list() if t.get("duplicate_of") == ids[0]][0]
check("C：复制品指向同一条 OmniFocus",
      copy["omnifocus"]["id"], store.get(ids[0])["omnifocus"]["id"])
check("C：复制品是新 ID", copy["id"] != ids[0], True)
check("C：旧的放弃", store.get(ids[0])["status"], ts.STATUS_DROPPED)
check("C：OmniFocus 没被动（重议不动它）", ("omni-drop", ids[0]) in calls, False)

# D 缺漏完成补充
calls = []
store, review, ids = mk_review(hooks=hooks_base(
    complete_focusmate=lambda task: calls.append(("fm-done", task["id"])) or "Focusmate ✓",
    complete_omnifocus=lambda task, minutes=None: calls.append(("omni-done", task["id"])) or "OmniFocus ✓",
    due_of=lambda task: task["fields"].get("due")))
review.handle("d")
check("D：先问分钟数", review.stage, psv.Review.MINUTES)
review.handle("大概 35 分钟")
check("D：本地完成", store.get(ids[0])["status"], ts.STATUS_COMPLETED)
check("D：记下实际分钟", store.get(ids[0])["actual_minutes"], 35)
check("D：Focusmate / OmniFocus 都被叫了",
      [c[0] for c in calls if c[0] != "say"], ["fm-done", "omni-done"])
arch = store.load_archive()["records"]
check("D：归档里这条是手工补录的", (arch[-1]["manual"], arch[-1]["actual_minutes"]), (True, 35))
check("D：分钟数认不出会重问", review.handle("x"), {"action": "handled"}) if False else None

# E 快捷推后：系统建复制品 + 塞进下一场 + 旧的放弃 + 标签
calls = []
store, review, ids = mk_review(hooks=hooks_base(
    next_session=lambda task: NEXT,
    assign_focusmate=lambda task, sess: (
        store.assign(task["id"], sess),
        calls.append(("assign", task["id"], sess["start"])))[1] or "Focusmate task x",
    drop_omnifocus=lambda task: calls.append(("omni-drop", task["id"]))))
review.handle("e")
copy = [t for t in store.list() if t.get("duplicate_of") == ids[0]][0]
check("E：复制品挂到下一场", copy["session"]["start"], "12:00")
check("E：复制品的截止时间＝下一场终点", copy["fields"]["due"], "2026-10-27T12:50:00")
check("E：旧任务放弃", store.get(ids[0])["status"], ts.STATUS_DROPPED)
check("E：旧任务打上「未完成转移」", store.get(ids[0])["tags"], [ts.TRANSFER_TAG])
check("E：OmniFocus 一个字节都没动", ("omni-drop", ids[0]) in calls, False)
check("E：只叫了一次挂 Focusmate",
      [c[0] for c in calls if c[0] != "say"], ["assign"])
check("E：拒绝把旧记录删掉（还在库里）", store.get(ids[0])["id"], ids[0])

# A 延期：等用户自己敲命令
store, review, ids = mk_review(hooks=hooks_base())
store.link_omnifocus(ids[0], "omnifocus:///task/pk1")
review.tasks = store.tasks_for_session("2026-10-27", "11:00")
check("A：进入等你敲命令的状态", review.handle("a"), {"action": "handled"})
check("A：stage = transfer", review.stage, psv.Review.TRANSFER)
verdict = review.handle("task %d modify due 明天" % ids[0])
check("A：别的命令照常派发出去", verdict["action"], "dispatch")
review.after_dispatch("task %d modify due 明天" % ids[0])
check("A：没转移 → 不放弃", store.get(ids[0])["status"], ts.STATUS_ACTIVE)
copy = store.duplicate(ids[0], session=NEXT)
review.after_dispatch("task %d duplicate -a 1" % ids[0])
check("A：转移后旧记录放弃", store.get(ids[0])["status"], ts.STATUS_DROPPED)
check("A：打上「未完成转移」", store.get(ids[0])["tags"], [ts.TRANSFER_TAG])
check("A：OmniFocus 还是那一条（没被放弃）",
      store.get(ids[0])["omnifocus"]["id"], "pk1")
check("A：复制品也在库里、指向同一条",
      store.get(copy["id"])["omnifocus"]["id"], "pk1")
check("A：收尾结束", review.closed, True)

# 跳过 / 退出 / 认不出的选项
store, review, ids = mk_review(titles=("写论文", "复习"), hooks=hooks_base())
check("认不出的选项不推进", review.handle("zzz"), {"action": "handled"})
check("认不出时还在同一条", review.current()["id"], ids[0])
review.handle("s")
check("s 跳过 → 下一条", review.current()["id"], ids[1])
check("跳过会被记下来", review.skipped, [ids[0]])
check("跳过的任务还是 active", store.get(ids[0])["status"], ts.STATUS_ACTIVE)
review.handle("q")
check("q → 收尾结束", review.closed, True)
check("空行只是提示", review.handle("x"), {"action": "handled"})


# ---------------------------------------------------------------- daemon 侧

print("== keep_session.postsession_tick：入队 + 拉 warden ==")
import keep_session as ks  # noqa: E402

CSV = os.path.join(TMP, "data.csv")
with open(CSV, "w", encoding="utf-8") as f:
    f.write("2026-10-27,11:00,50\n")
import schedule as sch  # noqa: E402

sch.DATA_CSV = CSV
ks.load_entries = sch.load_entries
psv.clear()
summons = []
early = datetime.datetime(2026, 10, 27, 11, 50, 10)
check("还没到 15 秒 → 不催",
      ks.postsession_tick(now=early, summon=lambda **kw: summons.append("x")), [])
check("也没拉人", summons, [])
on_time = datetime.datetime(2026, 10, 27, 11, 50, 16)
fired = ks.postsession_tick(now=on_time, summon=lambda **kw: summons.append("x"))
check("到点 → 入队一场", [r["key"] for r in fired], ["2026-10-27T11:00"])
check("到点 → 拉了一次 warden", summons, ["x"])
check("落进 pending 文件", [r["key"] for r in psv.pending_sessions()], ["2026-10-27T11:00"])
check("同一场不会重复催",
      ks.postsession_tick(now=on_time, summon=lambda **kw: summons.append("y")), [])
check("也就不会重复拉人", summons, ["x"])
psv.mark_done(SESS)
check("收完之后也不会再催",
      ks.postsession_tick(now=on_time, summon=lambda **kw: summons.append("z")), [])
psv.clear()


# ---------------------------------------------------------------- warden 侧

print("== warden：状态标记 / 截止时间 / 写回 ==")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
from rich.console import Console  # noqa: E402

spec = importlib.util.spec_from_file_location("warden_ps", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

sch.DATA_CSV = CSV
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **kw: None
w.st.load = lambda force=False, **kw: ({"sessions": [], "me": {}, "fetched_at": "",
                                        "since": "", "until": "", "source": "test"},
                                       "测试")


class FakeBridge(object):
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.last_idle_close = None

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd in self.replies:
            r = self.replies[cmd]
            return r(kw) if callable(r) else r
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "count": 1,
                    "sessions": [{"startMs": 1793088000000, "startLabel": "12:00",
                                  "endLabel": "12:50", "durationMin": 50,
                                  "id": "sess-2", "title": "", "quiet": False}]}
        if cmd == "tasks":
            return {"tasks": []}
        return {"verified": True, "taskId": "fm-1", "via": "api"}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


class FakeOmni(object):
    """假的 OmniFocus 客户端：记下每次写，用来断言「什么时候写了什么」。"""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.writes = []
        self.lists = 0

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None):
        self.lists += 1
        rows = self.rows
        if fields:
            return [{k: r.get(k) for k in fields} for r in rows]
        return rows

    def get(self, pk, refresh=False):
        for row in self.rows:
            if row.get("id") == pk:
                return row
        return None

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        self.writes.append(("complete", pk, due))
        return {"ok": True, "changed": ["completed", "due"]}

    def drop(self, pk, at=None, all_occurrences=False, yes=True, timeout=None):
        self.writes.append(("drop", pk, None))
        return {"ok": True, "changed": ["dropped"]}

    def set_task(self, pk, *flags, **kw):
        self.writes.append(("set", pk, flags))
        return {"ok": True, "changed": list(flags)}

    def invalidate(self):
        pass


def mkwarden(bridge=None):
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    return w.Warden(console, bridge or FakeBridge(), no_clear=True, offline=True)


wd = mkwarden()
omni = FakeOmni()
wd.omnifocus = omni
store = wd.task_store
for old in store.list():
    store.delete(old["id"])

# assign 自动写截止时间
task = store.create({"title": "写论文"})
store.assign(task["id"], {"date": "2026-10-27", "start": "11:00", "duration": 50})
check("assign 之后 due = session 终点", store.get(task["id"])["fields"]["due"],
      "2026-10-27T11:50:00")

# 状态标记：完成/放弃的任务不隐藏
store.complete(task["id"])
check("完成的任务还在库里", store.get(task["id"])["status"], ts.STATUS_COMPLETED)
check("契约表 task 列打 ✓", wd.task_cell_text([store.get(task["id"])]).startswith("✓"), True)
dropped = store.create({"title": "放弃的"})
store.drop(dropped["id"], tag=ts.TRANSFER_TAG)
check("转移走的是 ⤼", wd.task_mark(store.get(dropped["id"])), "⤼")
check("对照表里也带标记", "⤼" in wd.task_cell_text([store.get(dropped["id"])]), True)
plain = store.create({"title": "进行中"})
check("进行中打 ·（不能是空串，否则表格里分不清）",
      wd.task_mark(store.get(plain["id"])), "·")

# 完成 + 关联 OmniFocus → 写回（完成 + 截止时间）
task2 = store.create({"title": "复习数学"})
store.assign(task2["id"], {"date": "2026-10-27", "start": "12:00", "duration": 50})
wd.cmd_task_link([str(task2["id"]), "[复习数学](omnifocus:///task/pk1)"])
check("link 之后本地记下了 primary key",
      store.get(task2["id"])["omnifocus"]["id"], "pk1")
wd.handle("task %d done 35" % task2["id"])
check("done：本地完成", store.get(task2["id"])["status"], ts.STATUS_COMPLETED)
check("done：OmniFocus 写回（complete + due）", omni.writes,
      [("complete", "pk1", "2026-10-27T12:50:00")])
check("写回留了痕迹", bool((store.get(task2["id"])["omnifocus"].get("writeback") or {})
                          .get("completed")), True)
omni.writes = []
wd.writeback_completions()
check("已经写回过的不会重复写", omni.writes, [])

# drop 会把 OmniFocus 也放弃；--local 不会
task3 = store.create({"title": "不要了"})
wd.cmd_task_link([str(task3["id"]), "omnifocus:///task/pk2"])
omni.writes = []
wd.handle("task %d drop --local" % task3["id"])
check("--local 只改本地", omni.writes, [])
check("--local 也真的放弃了", store.get(task3["id"])["status"], ts.STATUS_DROPPED)
task4 = store.create({"title": "也不要了"})
wd.cmd_task_link([str(task4["id"]), "omnifocus:///task/pk3"])
omni.writes = []
wd.handle("task %d drop" % task4["id"])
check("drop 会连带 OmniFocus", omni.writes, [("drop", "pk3", None)])

# task N duplicate -a M
task5 = store.create({"title": "复制的源头"})
wd.cmd_task_link([str(task5["id"]), "omnifocus:///task/pk5"])
wd.rebuild_rows()
wd.rows = [{"startLabel": "12:00", "id": "sess-2", "startMs": 1793088000000,
            "date": "2026-10-27", "endLabel": "12:50", "durationMin": 50,
            "task_text": ""}]
wd.handle("task %d duplicate -a 1" % task5["id"])
copy = [t for t in store.list() if t.get("duplicate_of") == task5["id"]][0]
check("duplicate：新任务挂到了那一场", copy["session"]["start"], "12:00")
check("duplicate：OmniFocus 还是同一条", copy["omnifocus"]["id"], "pk5")
check("duplicate：旧的没被放弃", store.get(task5["id"])["status"], ts.STATUS_ACTIVE)
check("duplicate：日期跟 session 走", copy["session"]["date"], "2026-10-27")

# task import：粘贴 Markdown 链接
made = wd.cmd_task_import(["[数学易错修正](omnifocus:///task/kc-4VLAE_1L)"])
check("import：建出任务", made["fields"]["title"], "数学易错修正")
check("import：关联到 OmniFocus", made["omnifocus"]["id"], "kc-4VLAE_1L")
made2 = wd.cmd_task_import(['title: "只有标题"'])
check("import：只有标题也照建", made2["fields"]["title"], "只有标题")
check("import：但没有关联", made2["omnifocus"], None)
# 直接把链接粘进 create 也认（「粘到文本框里」那条路）
made3 = wd.cmd_task_create(["[数学](omnifocus:///task/pk9)"])
check("create 粘链接：标题剥出来了", made3["fields"]["title"], "数学")
check("create 粘链接：顺手关联上", made3["omnifocus"]["id"], "pk9")

# psv.pick：手动 review 时能只给时刻 / 只给日期
pick_rows = [{"key": "2026-10-27T11:00", "date": "2026-10-27", "start": "11:00"},
             {"key": "2026-10-27T16:45", "date": "2026-10-27", "start": "16:45"}]
check("pick 不给参数 = 最近一场", psv.pick(pick_rows)["start"], "16:45")
check("pick 只给时刻", psv.pick(pick_rows, "11:00")["start"], "11:00")
check("pick 只给日期 = 那天最后一场", psv.pick(pick_rows, "2026-10-27")["start"], "16:45")
check("pick 完整 key", psv.pick(pick_rows, "2026-10-27T11:00")["start"], "11:00")
check("pick 对不上 → None", psv.pick(pick_rows, "09:00"), None)


# ---------------------------------------------------------------- inspect-due

print("== inspect-due：主动看过期没做完的任务 ==")
for old in store.list():
    store.delete(old["id"])
NOW = datetime.datetime(2026, 10, 27, 12, 0)
made = {}
for title, due in (("过期 2 小时", "2026-10-27T10:00:00"),
                   ("过期 1 天", "2026-10-26T09:00:00"),
                   ("半小时后", "2026-10-27T12:30:00"),
                   ("没截止", None),
                   ("今天到期（只给日期）", "2026-10-27")):
    fields = {"title": title}
    if due:
        fields["due"] = due
    made[title] = store.create(fields)["id"]
check("只挑已过期的（按截止时间从早到晚）",
      [t["fields"]["title"] for t in psv.due_candidates(store, now=NOW)],
      ["过期 1 天", "过期 2 小时"])
check("within_minutes 把快到期的也算上",
      [t["fields"]["title"] for t in psv.due_candidates(store, now=NOW, within_minutes=60)],
      ["过期 1 天", "过期 2 小时", "半小时后"])
check("--all 不按截止时间筛（有截止的排前面，没截止的最后）",
      [t["fields"]["title"] for t in psv.due_candidates(store, now=NOW, include_no_due=True)],
      ["过期 1 天", "过期 2 小时", "半小时后", "今天到期（只给日期）", "没截止"])
check("完成/放弃的不催", (store.complete(made["过期 1 天"]),
                        [t["id"] for t in psv.due_candidates(store, now=NOW)]),
      (store.get(made["过期 1 天"]), [made["过期 2 小时"]]))
check("只给日期的 due 当天 23:59 才算过期",
      psv.parse_due("2026-10-27"), datetime.datetime(2026, 10, 27, 23, 59))
check("认不出的 due 不算过期", psv.parse_due("明天"), None)
check("过期时长白话", psv._fmt_age(2 * 3600 + 12 * 60), "2 小时 12 分")

wd.review = None
wd.begin_inspect_due(psv.due_candidates(store, now=NOW), label="过期未完成任务", sync=False)
check_true("inspect-due 能进收尾", wd.review is not None)
check("标题是「过期未完成任务」", wd.review.headline(), "过期未完成任务")
check("只问过期的", [t["id"] for t in wd.review.tasks], [made["过期 2 小时"]])
check_true("面板画得出来", wd.review_panel() is not None)
check("面板里能看到「截止」和「已过期」",
      "已过期" in "".join(t for t, _s in wd.review.lines()), True)
before_state = psv.load_state()
check("选 b 放弃", wd.intercept("b"), True)
check("放弃之后这条真的放弃了", store.get(made["过期 2 小时"])["status"], ts.STATUS_DROPPED)
check("inspect-due 收完不留 postsession 记录",
      psv.load_state()["pending"], before_state["pending"])
check("也不写 done（没有那一场可以翻篇）",
      psv.load_state()["done"], before_state["done"])
check("inspect-due 之后账干净了", psv.due_candidates(store, now=NOW), [])
wd.review = None
check("inspect-due 没东西可看时直接返回", wd.cmd_inspect_due([], now=NOW), None)
check("inspect-due --all 会连没截止的一起看",
      wd.cmd_inspect_due(["--all"], now=NOW) is not None, True)
wd.review = None
# 状态框里要能看见「该查账了」，否则没人知道有这条命令
warns = [str(item.message if hasattr(item, "message") else item)
         for item in wd.collect_warnings()]
check("过期任务会在状态框里提醒",
      any("过了截止时间" in text for text in warns), True)
check_true("提醒里直接给出命令",
           any("inspect-due" in str(getattr(item, "hint", "")) for item in wd.collect_warnings()))
# 两个视图都注册了这条命令
view_names = [v.name for v in wd.view_impls]
check("任务 / 契约视图都在", ("任务" in view_names and "契约" in view_names), True)
for name in ("任务", "契约"):
    impl = [v for v in wd.view_impls if v.name == name][0]
    registry = impl.to_view().registry
    check("%s视图认 inspect-due" % name, registry.get("inspect-due") is not None, True)
    check("%s视图认别名 due" % name, registry.get("due") is not None, True)
wd.review = None
for old_task in store.list():
    store.delete(old_task["id"])

# ------------------------------------------------- 老任务没有 fields.due 也不能漏

print("== 截止时间：fields.due 没有就退回 session 终点 ==")
psv.clear()
wd.review = None
for old in store.list():
    store.delete(old["id"])
LEGACY_SESS = {"date": "2026-10-27", "start": "11:00", "duration": 50,
               "focusmate_session_id": "s-legacy"}
legacy = store.create({"title": "老数据（没有 due）"})
store.assign(legacy["id"], LEGACY_SESS)
# 真实的老数据长这样：有 session，但 fields 里没有 due（那个字段是后加的）
raw = store.load()
for item in raw["tasks"]:
    (item.get("fields") or {}).pop("due", None)
    (item.get("fields") or {}).pop("due_source", None)
store.save(raw)
check("这条确实没有 fields.due", (store.get(legacy["id"])["fields"] or {}).get("due"), None)
check("截止时间退回 session 终点",
      psv.effective_due(store.get(legacy["id"])),
      datetime.datetime(2026, 10, 27, 11, 50))
check("inspect-due 不会把这类任务整批漏掉",
      [t["id"] for t in psv.due_candidates(store, now=NOW)], [legacy["id"]])
check("面板上也写清截止 + 已过期",
      "已过期" in "".join(t for t, _s in
                         psv.Review(store, {"kind": "due"}, [store.get(legacy["id"])],
                                    now=NOW).lines()), True)
check("真有 fields.due 时以它为准",
      psv.effective_due(store.create({"title": "有 due", "due": "2026-10-27T09:00:00"})),
      datetime.datetime(2026, 10, 27, 9, 0))
for old in store.list():
    store.delete(old["id"])


# ------------------------------------------- 场次结束了、但 daemon 队列是空的

print("== 队列空也算得出「这场结束了还没收」 ==")
psv.clear()
wd.review = None
wd.review_check = None
ENDED = {"date": "2026-10-27", "start": "16:45", "end": "17:35", "duration": 50,
         "key": "2026-10-27T16:45"}
late_task = store.create({"title": "那场没做完"})
store.assign(late_task["id"], ENDED)
done_task2 = store.create({"title": "那场做完了"})
store.assign(done_task2["id"], ENDED)
store.complete(done_task2["id"])
DAY = datetime.date(2026, 10, 27)
DAY_ENTRIES = [{"weekday": "tue", "start": "16:45", "hour": 16, "minute": 45,
                "duration": 50, "type": "工作", "name": "SAT 1H"}]
AT = datetime.datetime(2026, 10, 27, 18, 15)
rows = psv.unreviewed_sessions(store, DAY_ENTRIES, now=AT)
check("算得出那一场", [r["key"] for r in rows], ["2026-10-27T16:45"])
check("只点名还没做完的那条", rows[0]["open_ids"], [late_task["id"]])
check("整场都收完了就不催",
      (store.complete(late_task["id"]),
       psv.unreviewed_sessions(store, DAY_ENTRIES, now=AT)), (store.get(late_task["id"]), []))
store.set_status(late_task["id"], ts.STATUS_ACTIVE)
check("done 里记过的场次不再催",
      psv.unreviewed_sessions(store, DAY_ENTRIES, now=AT,
                              done_keys=["2026-10-27T16:45"]), [])

# warden 那一层：队列里没有，也照样把收尾面板拉起来（daemon 只是闹钟）
_real_reload = wd.reload_entries
wd.reload_entries = lambda: DAY_ENTRIES
wd.offline = False
wd.review = None
wd.review_check = None
wd.review_at = 0.0
# 「现在」钉在这场的 40 分钟后。自动催只看 3 小时内，用墙上时钟的话这组用例
# 就只在 17:35 之后那三个钟头里是绿的，过点就无缘无故地红。
wd.review_now = AT
wd._maybe_start_review()
check_true("队列空也会自己起核对", wd.review_check is not None)
for _ in range(200):
    if wd.review_check and wd.review_check.get("ready"):
        break
    import time as _t
    _t.sleep(0.02)
wd.review_at = 0.0
wd._maybe_start_review()
check_true("自己算出来的那一场也进收尾", wd.review is not None)
check("只问没做完的那一条", [t["id"] for t in wd.review.tasks], [late_task["id"]])
check("面板标题是那一场", wd.review.headline(), "2026-10-27 16:45–17:35 收尾")
check("面板里写了截止 + 已过期",
      "已过期" in "".join(t for t, _s in wd.review.lines()), True)
check("q 结束收尾", (wd.intercept("q"), wd.review), (True, None))
check("收完记进 done（下次开机不再催）",
      list(psv.load_state()["done"]), ["2026-10-27T16:45"])
wd.review_at = 0.0
wd._maybe_start_review()
check("再问一遍也不会重复催", wd.review, None)
# 3 小时以外的旧账不自动催（翻旧账是 inspect-due 的事）
wd.review_now = datetime.datetime(2026, 10, 28, 9, 0)
wd.review_at = 0.0
wd._maybe_start_review()
check("3 小时以外的旧账不自动翻", wd.review_check, None)
wd.review_now = None
wd.reload_entries = _real_reload
wd.review = None
for old in store.list():
    store.delete(old["id"])


# ---------------------------------------------------------------- 收尾：自动 + 手动

print("== warden：收尾流程 ==")
psv.clear()
for old in store.list():
    store.delete(old["id"])
SESS_ROW = {"date": "2026-10-27", "start": "11:00", "end": "11:50", "duration": 50,
            "key": "2026-10-27T11:00"}
t1 = store.create({"title": "写完"})
store.assign(t1["id"], SESS_ROW)
store.complete(t1["id"])
t2 = store.create({"title": "没写完"})
store.assign(t2["id"], SESS_ROW)
wd.review = None
wd.rebuild_rows()

psv.mark_pending(dict(SESS_ROW))
wd.review_at = 0.0
wd.review_check = None
wd._maybe_start_review()
check_true("看到 pending 就起后台核对", wd.review_check is not None)
check("后台核对跑完之前不建 Review", wd.review, None)
# 等后台线程把 ready 立起来
for _ in range(200):
    if wd.review_check and wd.review_check.get("ready"):
        break
    import time as _t
    _t.sleep(0.02)
wd.review_at = 0.0
wd._maybe_start_review()
check_true("核对完就进收尾", wd.review is not None)
check("只问没做完的那一条", [t["id"] for t in wd.review.tasks], [t2["id"]])
check_true("面板画得出来", wd.review_panel() is not None)
check_true("提示里有 A/B/C/D/E", "A 延期" in "".join(t for t, _s in wd.review.lines()))

# intercept 把这一行交给收尾
before = len(wd.messages)
check("intercept 吃掉选项键", wd.intercept("b"), True)
check("选 b 之后这条变放弃", store.get(t2["id"])["status"], ts.STATUS_DROPPED)
check("收尾自动收工（没有更多任务）", wd.review, None)
check("状态文件里这场挪到 done", psv.pending_sessions(), [])

# 手动 review：没有待收尾的场次时给一句人话
wd.say = lambda text, kind="info": None
check("没有场次时 review 不炸", wd.cmd_review([]), None)

# 手动 review 一场（12:00 那场，今天没到；用 11:00 那场）
psv.clear()
wd.review = None
wd.offline = True
entry = {"key": "2026-10-27T11:00", "date": "2026-10-27", "start": "11:00",
         "end": "11:50", "duration": 50}
t3 = store.create({"title": "又要收尾"})
store.assign(t3["id"], entry)
wd.begin_review(entry, sync=False)
check_true("begin_review 直接进收尾", wd.review is not None)
check("空行只是提示，不推进", wd.intercept(""), True)
check("q 结束收尾", (wd.intercept("q"), wd.review), (True, None))

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
shutil.rmtree(TMP, ignore_errors=True)
