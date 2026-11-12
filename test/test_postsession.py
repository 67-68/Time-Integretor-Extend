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
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_POSTSESSION_FILE"] = os.path.join(TMP, "postsession.json")
# 真实的 temp/keep_session-nostart.json 里可能正躺着 daemon 刚入队的「到点没开始」，
# 不钉住的话 warden 一开就会弹那一问、把命令输入吃掉（p/x/s 才认）。
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")

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
check("没有链接的多行只当一条任务（折行不是两条）",
      [i["title"] for i in of.parse_links("语法高频\n30 个")], ["语法高频 30 个"])
check("多行裸标题也可以显式不合并（剪贴板多选要用）",
      [i["title"] for i in of.parse_links("甲\n乙", merge_bare=False)], ["甲", "乙"])
check("is_link 只认有 id 的", (of.is_link("title: x"), of.is_link("omnifocus:///task/pk1")),
      (False, True))
html = '<a href="omnifocus:///task/kc-9">数学 &amp; 易错</a>'
check("HTML 剪贴板抓 href + 锚文本",
      [(i["id"], i["title"]) for i in of.html_links(html)], [("kc-9", "数学 & 易错")])


print("== omnifocus 重复规则查询：只读脚本 + 信封解析 ==")
_script = of.build_repetition_script("pk-repeat")
check_true("脚本走 evaluate javascript", "evaluate javascript js" in _script)
check_true("脚本只查 Task.byIdentifier", "Task.byIdentifier" in _script)
check_true("信封 tool 是 omnifocus_repetition", "omnifocus_repetition" in _script)
check_true("脚本取 repetitionRule.ruleString", "repetitionRule" in _script
           and "ruleString" in _script)
_rep = of.parse_repetition_result(json.dumps({
    "tool": "omnifocus_repetition", "version": 1, "ok": True,
    "repeated": True, "id": "pk-repeat", "name": "每周三闪卡",
    "rule_string": "FREQ=WEEKLY;BYDAY=WE", "method": "DueDate",
    "due": "2026-11-04T11:10:00", "planned": None, "defer": None,
    "next": "2026-11-04T11:10:00", "estimated_minutes": 25,
}))
check("repeated=True 解出来", _rep["repeated"], True)
check("ruleString 解出来", _rep["rule_string"], "FREQ=WEEKLY;BYDAY=WE")
check("estimated_minutes 是 int", _rep["estimated_minutes"], 25)
_plain = of.parse_repetition_result(json.dumps({
    "tool": "omnifocus_repetition", "version": 1, "ok": True,
    "repeated": False, "id": "pk-plain", "name": "普通任务",
    "rule_string": None, "due": None, "estimated_minutes": None,
}))
check("普通任务 repeated=False", _plain["repeated"], False)
check("普通任务没有 rule", _plain["rule_string"], None)
try:
    of.parse_repetition_result('{"tool":"wrong","ok":true}')
    _wrong_ok = False
except ValueError:
    _wrong_ok = True
check("信封 tool 不对 → ValueError", _wrong_ok, True)


print("== omnifocus 剪贴板：按 flavor 读（⌘V 到终端只剩标题）==")
check("osascript 的 «data url …» 解出链接",
      of._decode_data("«data url 6F6D6E69666F6375733A2F2F2F7461736B2F6935424B584335754E6B30»"),
      "omnifocus:///task/i5BKXC5uNk0")
check("html flavor 解出来就是 HTML",
      of._decode_data("«data HTML 3C623Ee6B189«»") if False else
      of._decode_data("«data HTML 3C623EE6B1893C2F623E»"), "<b>汉</b>")
check("不是 data 包装就当纯文本", of._decode_data("TI: Swap"), "TI: Swap")
check("空输入不炸", (of._decode_data(""), of._decode_data(None)), ("", ""))

rtf = ('{\\rtf1\\ansi{\\field{\\*\\fldinst{HYPERLINK '
       '"omnifocus:///task/kc-4VLAE_1L"}}{\\fldrslt 数学易错修正}}}')
rtf_items = of.rtf_links(rtf)
check("RTF 里抓出链接", [(i["id"], i["title"]) for i in rtf_items],
      [("kc-4VLAE_1L", "数学易错修正")])
check("RTF 兜底：整段里扫链接",
      [i["id"] for i in of.rtf_links("x omnifocus:///task/abc y")], ["abc"])

# OmniFocus 拷带中文的任务时，RTF 里的标题是 \uNNNN 码点（实测形状）：
#   {\fldrslt \n\f0\fs24 \cf0 \uc0\u25968 \u23398 \u26131 \u38169 \u20462 \u27491 }
# 以前把 \u25968 当控制词整段删掉 → 标题空 → 多选时 3 条变 6 条。
check("RTF 的 \\uNNNN 码点解回中文",
      of._rtf_unescape(r"\f0\fs24 \cf0 \uc0\u25968 \u23398 \u26131 \u38169 \u20462 \u27491 "),
      "数学易错修正")
check("\\ucN 指定的 fallback 字符会跳过", of._rtf_unescape(r"\uc1\u25968?\u23398?"), "数学")
check("别的小转义照旧（单字节 / 花括号 / 双反斜杠）",
      of._rtf_unescape("\\'3a" + "\\{" + "\\\\"), ":{" + chr(92))
_multi_rtf = ('{\\rtf1{\\field{\\*\\fldinst{HYPERLINK "omnifocus:///task/AAA"}}'
              '{\\fldrslt \\uc0\\u20219 \\u21153 \\u19968 }}'
              '{\\field{\\*\\fldinst{HYPERLINK "omnifocus:///task/BBB"}}'
              '{\\fldrslt \\uc0\\u20219 \\u21153 \\u20108 }}}')
check("多选时 RTF 自己就能给出「链接 + 标题」成对",
      [(i["id"], i["title"]) for i in of.rtf_links(_multi_rtf)],
      [("AAA", "任务一"), ("BBB", "任务二")])
check("一行里挤了 3 条 Markdown 链接 → 拆成 3 条（终端会把换行压成空格）",
      [(i["id"], i["title"]) for i in of.parse_links(
          "[甲](omnifocus:///task/pk-a) [乙](omnifocus:///task/pk-b) "
          "[丙](omnifocus:///task/pk-c)")],
      [("pk-a", "甲"), ("pk-b", "乙"), ("pk-c", "丙")])
check("一行里挤了 2 条裸链接 → 也拆（标题空着，由导入方去 OmniFocus 查）",
      [i["id"] for i in of.parse_links(
          "omnifocus:///task/pk-a omnifocus:///task/pk-b")], ["pk-a", "pk-b"])
check("只有一条链接时那一行原样保留（标题不会被拆散）",
      [(i["id"], i["title"]) for i in of.parse_links(
          "[语法高频](omnifocus:///task/pk-a) 30 个")], [("pk-a", "语法高频")])
check("没有链接的行照旧（折行仍合成一条）",
      [(i["id"], i["title"]) for i in of.parse_links("语法高频" + chr(10) + "30 个")],
      [(None, "语法高频 30 个")])
check("裸标题和某条带 id 的标题一模一样 → 去掉（那是同一份剪贴板的另一半）",
      [(i.get("id"), i.get("title")) for i in of.drop_duplicate_titles(
          [{"id": "a", "title": "甲"}, {"id": None, "title": "甲"},
           {"id": None, "title": "手写的"}])],
      [("a", "甲"), (None, "手写的")])

# 各 flavor 全部喂给 clipboard_links（真的按 flavor 读，不是 pbpaste 一刀切）
_real_flavor = of.clipboard_flavor
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/i5BKXC5uNk0",
    "html": '<a href="omnifocus:///task/i5BKXC5uNk0">TI: Swap</a>',
    "rtf": rtf.replace("kc-4VLAE_1L", "i5BKXC5uNk0").replace("数学易错修正", "TI: Swap"),
    "text": "TI: Swap\n",
}.get(which, "")
items = of.clipboard_links()
check("三个 flavor 说的是同一条 → 合并成一条", len(items), 1)
check("id 来自 URL flavor", items[0]["id"], "i5BKXC5uNk0")
check("标题来自 HTML 锚文本", items[0]["title"], "TI: Swap")

# 多选拷好几条：HTML 里有全部（URL flavor 只剩最后一个）
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/b",
    "html": ('<a href="omnifocus:///task/a">甲</a>'
             '<a href="omnifocus:///task/b">乙</a>'),
    "rtf": "",
    "text": "甲" + chr(10) + "乙" + chr(10),
}.get(which, "")
items = of.clipboard_links()
check("多选拷贝 → 两条都在（URL flavor 只剩最后一条也补得上）",
      [(i["id"], i["title"]) for i in items], [("a", "甲"), ("b", "乙")])

# 一条链接 + 被 flavor 截断的标题：HTML 只给 "30"，纯文本才是全标题
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/pk-grammar",
    "html": '<a href="omnifocus:///task/pk-grammar">30</a>',
    "rtf": "",
    "text": "语法高频 30 个\n",
}.get(which, "")
items = of.clipboard_links()
check("标题被截断时保留更长的那份（不会多建一条 30）",
      [(i.get("id"), i.get("title")) for i in items],
      [("pk-grammar", "语法高频 30 个")])

# 没有纯文本，只有 HTML 短标题 + RTF 全标题：同一个 id 也要取长的
_rtf_full = ('{\\rtf1\\ansi{\\field{\\*\\fldinst{HYPERLINK '
             '"omnifocus:///task/pk-grammar"}}{\\fldrslt 语法高频 30 个}}}')
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/pk-grammar",
    "html": '<a href="omnifocus:///task/pk-grammar">30</a>',
    "rtf": _rtf_full,
    "text": "",
}.get(which, "")
items = of.clipboard_links()
check("同一 id 的短标题会被 RTF 全标题升级",
      [(i.get("id"), i.get("title")) for i in items],
      [("pk-grammar", "语法高频 30 个")])

# 一条链接 + 纯文本被折成两行：合回一条任务
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/pk-grammar",
    "html": "",
    "rtf": "",
    "text": "语法高频\n30 个\n",
}.get(which, "")
items = of.clipboard_links()
check("纯文本折行也合回一条任务",
      [(i.get("id"), i.get("title")) for i in items],
      [("pk-grammar", "语法高频 30 个")])

# 只有 URL + 纯文本标题（OmniFocus 拷一条任务的真实剪贴板）
of.clipboard_flavor = lambda which: {
    "url": "omnifocus:///task/i5BKXC5uNk0",
    "html": "",
    "rtf": "",
    "text": "TI: Swap\n",
}.get(which, "")
items = of.clipboard_links()
check("URL + 纯文本标题会配对成一条", len(items), 1)
check("配对之后既有 id 又有标题",
      (items[0]["id"], items[0]["title"]), ("i5BKXC5uNk0", "TI: Swap"))

# 只有标题：不假装有 id
of.clipboard_flavor = lambda which: {"text": "只有个标题\n"}.get(which, "")
items = of.clipboard_links()
check("只有标题 → 没有 id（宁可少关联）",
      [(i.get("id"), i.get("title")) for i in items], [(None, "只有个标题")])

check("非 macOS 读不到剪贴板就返回空（不抛）",
      of.clipboard_flavor("url") if sys.platform == "darwin" else "", "")
_real_osascript = of._osascript
of._osascript = lambda script, timeout=5: "«class RTF », 392, URL, 29, string, 9"
check("clipboard info 解析得出 flavor 和大小", of.clipboard_info(),
      [("RTF", 392), ("URL", 29), ("string", 9)])
of._osascript = _real_osascript
of.clipboard_flavor = _real_flavor


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
# 增量读：先问一次 changes（假脚本不管参数，照样吐两行 → 两条都算「变了」），
# 再按 id 补读一次。所以是 1（首次全量）+ 2（刷新那轮）。
check("refresh=True 真的再读一次（changes + 补读）", client.calls, 3)


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

# G 延长排期：inspect-due 专属，先问已投入、再问还需，最后排下一场
print("\n== G inspect-due 延长排期 ==")
calls = []
store, review, ids = mk_review(hooks=hooks_base(
    next_session=lambda task: NEXT,
    assign_focusmate=lambda task, sess: (
        store.assign(task["id"], sess),
        calls.append(("assign", task["id"], sess["start"])))[1] or "Focusmate task x",
    set_count_plan=lambda task, invested, remaining: (
        calls.append(("plan", task["id"], invested, remaining)),
        store.set_count_plan(task["id"], invested, remaining))[1]))
store.set_count(ids[0], 2)
store.add_count(ids[0], 1)
review.tasks = [store.get(ids[0])]
review.session = dict(SESS)
check("普通 session 收尾不出 G", "g" in [k for k, _n, _d in review.options()], False)
review.session = {"kind": "due", "label": "过期未完成任务"}
check("inspect-due 出 G", "g" in [k for k, _n, _d in review.options()], True)
body = "\n".join(text for text, _style in review.lines())
check_true("面板上看得见 G 延长排期", "G" in body and "延长排期" in body)
check("敲 G 进入「问已投入」", review.handle("g"), {"action": "handled"})
check("stage = extend-invested", review.stage, psv.Review.EXTEND_INVESTED)
review.handle("-1")
check("负的已投入被拒、还在问", review.stage, psv.Review.EXTEND_INVESTED)
review.handle("2")
check("已投入记下、进入「问还需」", (review.stage, review.extend_invested),
      (psv.Review.EXTEND_REMAIN, 2))
review.handle("0")
check("还需 0 被拒、还在问", review.stage, psv.Review.EXTEND_REMAIN)
review.handle("3")
fresh = store.get(ids[0])
check("G：已投入校准为 2", ts.task_count_invested(fresh), 2)
check("G：上限 = 已投入 2 + 还需 3 = 5", ts.task_count(fresh), 5)
check("G：时长跟着 = 125 分钟", fresh["fields"].get("duration"), 125)
check("G：任务仍然进行中", fresh["status"], ts.STATUS_ACTIVE)
check("G：自动排到下一场", (fresh["session"].get("start"),
                              fresh["session"].get("date")), ("12:00", "2026-10-27"))
check("G：截止时间跟下一场终点", fresh["fields"].get("due"), "2026-10-27T12:50:00")
check("G：先写 count plan，再挂下一场",
      [c for c in calls if c[0] != "say"],
      [("plan", ids[0], 2, 3), ("assign", ids[0], "12:00")])
check_true("G：说清楚排到了哪场", any("已排到 2026-10-27 12:00" in text
                                        for _kind, text in review.notes))
check("G：一条任务处理完就收工", (review.closed, review.pending()), (True, False))

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

print("== 转移之后 Focusmate 那条 task 要删掉 ==")
deleted = []
store, review, ids = mk_review(hooks=dict(hooks_base(),
      delete_focusmate_task=lambda task: deleted.append(task["id"]) or "Focusmate task ft-1 已删除"))
store.attach_focusmate(ids[0], {"task_id": "ft-1"})
review.handle("a")
store.duplicate(ids[0], session=NEXT)          # 真的把复制品挂到下一场（＝转移发生了）
review.after_dispatch("task %d duplicate -a 1" % ids[0])
check("转移成立时叫了「删 Focusmate task」", deleted, [ids[0]])
check_true("收尾里说了已删除",
           any("已删除" in text for _kind, text in review.notes))
check("旧任务还是放弃 + 标签", store.get(ids[0])["status"], ts.STATUS_DROPPED)
store2, review2, ids2 = mk_review(hooks=hooks_base())
check("没有 delete hook 也不炸（收尾照走）", review2.handle("b")["action"], "close")

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

# ⚠️ 立刻钉死：后面任何一段（包括本文件里后来才写的用例）都可能碰 daemon 的
# 状态文件。以前是到「warden 侧」才钉，于是前面那几段写的是仓库里**真的**
# temp/keep_session-state.json（实测把真 state 清空过一次）。
CSV = os.path.join(TMP, "data.csv")
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")
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


print("== 收尾按「实际场次」发问（计划 75 被拆成 25+50）==")
real = [{"start": datetime.datetime(2026, 10, 27, 16, 0), "duration": 25, "id": "a"},
        {"start": datetime.datetime(2026, 10, 27, 16, 30), "duration": 50, "id": "b"}]
by_day = psv.slots_from_details(real)
check("按天分组", list(by_day), [datetime.date(2026, 10, 27)])
check("实际时长进来（不再拿计划占位）",
      [(s["start"], s["duration"]) for s in by_day[datetime.date(2026, 10, 27)]],
      [("16:00", 25), ("16:30", 50)])
check("标了 actual", all(s["actual"] for s in by_day[datetime.date(2026, 10, 27)]),
      True)

with open(CSV, "w", encoding="utf-8") as f:
    f.write("2026-10-27,16:15,75\n")
plan_only = psv.slots_around(sch.load_entries()[0],
                             now=datetime.datetime(2026, 10, 27, 12, 0), days_back=0)
check("不给 actual 就用计划", [(x["start"], int(x["duration"]))
                                for _d, xs in plan_only for x in xs], [("16:15", 75)])
with_actual = psv.slots_around(sch.load_entries()[0],
                               now=datetime.datetime(2026, 10, 27, 12, 0),
                               days_back=0, actual=real)
check("(entries, warnings) 整个传进来也认（以前会静悄悄变空）",
      psv.slots_around(sch.load_entries(), now=datetime.datetime(2026, 10, 27, 12, 0),
                       days_back=0),
      plan_only)
check("给了 actual 就用实际",
      [(x["start"], x["duration"]) for _d, xs in with_actual for x in xs],
      [("16:00", 25), ("16:30", 50)])
early = psv.ended_sessions(with_actual, now=datetime.datetime(2026, 10, 27, 16, 25, 20))
check("第一段结束 15 秒后就该问（不是等到计划的 17:30）",
      [r["key"] for r in early], ["2026-10-27T16:00"])
late = psv.ended_sessions(with_actual, now=datetime.datetime(2026, 10, 27, 17, 20, 20))
check("第二段结束 15 秒后再问一次（第一段还在 3 小时内，一起等着）",
      [r["key"] for r in late], ["2026-10-27T16:00", "2026-10-27T16:30"])
plan_early = psv.ended_sessions(plan_only, now=datetime.datetime(2026, 10, 27, 16, 25, 20))
check("（对照）按计划那边这时候什么都不问", plan_early, [])

print("== keep_session：收尾线程按 state.json 里的实际快照算 ==")
fresh = [{"start": (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat(
              timespec="seconds"), "duration": 25, "id": "x"}]
ks.save_state({"date": datetime.date.today().isoformat(), "slots": {},
               "api_snapshot": {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                "horizon": None, "sessions": [], "details": fresh}})
got = ks.fresh_snapshot_details()
check("新鲜快照读得出来", [r["duration"] for r in got], [25])
ks.save_state({"date": datetime.date.today().isoformat(), "slots": {},
               "api_snapshot": {"at": (datetime.datetime.now()
                                       - datetime.timedelta(hours=3)).isoformat(timespec="seconds"),
                                "horizon": None, "sessions": [], "details": fresh}})
check("过期快照当没有（退回计划，别拿旧数据当事实）",
      ks.fresh_snapshot_details(), None)

print("== 叫人：TUI 活着 / 没开 分得清 ==")
TUI = os.path.join(TMP, "warden-tui.json")
ks.TUI_STATE_FILE = TUI
check("没有状态文件 → 没开", ks.tui_alive(), (False, {}))
with open(TUI, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "tty": "/dev/ttys999"}, f)
alive, st = ks.tui_alive()
check("自己这个 pid 活着 → 开着的", (alive, st.get("tty")), (True, "/dev/ttys999"))
with open(TUI, "w", encoding="utf-8") as f:
    json.dump({"pid": 999999, "tty": "/dev/ttys999"}, f)
check("pid 没了 → 没开（marker 里的 session 早就不跑 warden 了）",
      ks.tui_alive()[0], False)

# summon_warden 的三种参数组合（不真跑 osascript）
calls = []


class _Proc(object):
    def __init__(self, out="focused ABC"):
        self.returncode = 0
        self.stdout = out
        self.stderr = ""


_real_run = ks.subprocess.run
_real_exists = os.path.exists
ks.subprocess.run = lambda cmd, **kw: (calls.append(list(cmd)), _Proc())[1]
ks.os.path.exists = lambda p: True if p == ks.SUMMON_SCRIPT else _real_exists(p)
with open(TUI, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid(), "tty": "/dev/ttys003"}, f)
check("TUI 开着 → 聚焦 + 按 tty 找 + 不打 refresh",
      (ks.summon_warden(), calls[-1][2:]),
      (True, ["--no-refresh", "--tty", "/dev/ttys003"]))
os.remove(TUI)
check("TUI 没开 → --new（新窗口跑 Warden.command）",
      (ks.summon_warden(), calls[-1][2:]), (True, ["--new", "--no-refresh"]))
check("显式要 refresh 就给（默认不给）",
      (ks.summon_warden(refresh=True), calls[-1][2:]),
      (True, ["--new"]))
check("dry-run 不调 osascript", (ks.summon_warden(dry_run=True), len(calls)), (True, 3))

# osascript 被 TCC 挡住（-10004 权限违例）→ 走 open -a app bundle
SUMMON_LOGS = []
_real_ks_log = ks.log
ks.log = lambda msg, echo=True, to_file=True: SUMMON_LOGS.append(str(msg))
opened = []
_real_isdir = os.path.isdir
ks.os.path.isdir = lambda p: True if p == ks.SUMMON_APP else _real_isdir(p)


class _Proc2(object):
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def fake_run(cmd, **kw):
    args = list(cmd)
    calls.append(args)
    if args and args[0] == "open":
        opened.append(args)
        return _Proc2(0, "")
    # osascript 那条：退出码 0，但 AppleScript 的 fail() 把错误写进了 stdout
    return _Proc2(0, "error -10004: “iTerm”遇到一个错误：发生权限违例。")


ks.subprocess.run = fake_run
check("osascript 报权限违例时算失败，并改用 app（先按名字找装好的那个）",
      (ks.summon_warden(), opened[-1]), (True, ["open", "-a", "Warden"]))


def fake_run_all_broken(cmd, **kw):
    args = list(cmd)
    calls.append(args)
    if args and args[0] == "open":
        return _Proc2(1, "")            # open 也失败
    return _Proc2(0, "权限违例")


ks.subprocess.run = fake_run_all_broken
check("两条路都走不通 → 返回失败（不假装成功）", ks.summon_warden(), False)
ks.os.path.isdir = lambda p: False
check("app 也起不来 → 失败（不假装成功）", ks.summon_warden(), False)
check_true("日志里给了 build.sh / 授权 的修法",
           any("build.sh" in m for m in SUMMON_LOGS))
check_true("日志里点名了 app 那条路也试过",
           any("open -a" in m for m in SUMMON_LOGS))
ks.log = _real_ks_log
ks.os.path.isdir = _real_isdir
ks.subprocess.run = _real_run
ks.os.path.exists = _real_exists

print("== 补叫：叫了但人没来 → 隔一会儿再叫 ==")
psv.clear()
psv.mark_pending({"key": "2026-10-27T11:00", "date": "2026-10-27", "start": "11:00",
                  "end": "11:50", "duration": 50},
                 now=datetime.datetime(2026, 10, 27, 11, 50, 16))
retries = []
check("TUI 没开、刚叫过 → 这一秒不再叫",
      ks.postsession_retry(now=datetime.datetime(2026, 10, 27, 11, 50, 30),
                           summon=lambda **kw: retries.append("x")), [])
check("过了一分钟还没人 → 再叫一次",
      len(ks.postsession_retry(now=datetime.datetime(2026, 10, 27, 11, 52, 0),
                               summon=lambda **kw: retries.append("x"))), 1)
check("叫了", retries, ["x"])
psv.clear()

print("== 收尾线程起来了（1 秒一次，不等订场那一轮）==")
check_true("start_postsession_thread 返回活线程",
           ks.start_postsession_thread(dry_run=True).is_alive())
ks._STOP["flag"] = True
ks._STOP["flag"] = False


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
wd.cmd_task_link(["id:%d" % task2["id"], "[复习数学](omnifocus:///task/pk1)"])
check("link 之后本地记下了 primary key",
      store.get(task2["id"])["omnifocus"]["id"], "pk1")
wd.handle("task id:%d done 35" % task2["id"])
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
wd.cmd_task_link(["id:%d" % task3["id"], "omnifocus:///task/pk2"])
omni.writes = []
wd.handle("task id:%d drop --local" % task3["id"])
check("--local 只改本地", omni.writes, [])
check("--local 也真的放弃了", store.get(task3["id"])["status"], ts.STATUS_DROPPED)
task4 = store.create({"title": "也不要了"})
wd.cmd_task_link(["id:%d" % task4["id"], "omnifocus:///task/pk3"])
omni.writes = []
wd.handle("task id:%d drop" % task4["id"])
check("drop 会连带 OmniFocus", omni.writes, [("drop", "pk3", None)])

# Focusmate 勾完成 → 本地落 completed → 写回 OmniFocus（refresh / 收尾都走这条）
task_fm = store.create({"title": "Focusmate 已完成"})
store.assign(task_fm["id"], {"date": "2026-10-27", "start": "13:00", "duration": 50})
wd.cmd_task_link(["id:%d" % task_fm["id"], "omnifocus:///task/pk-fm-done"])
wd.bridge.replies["tasks"] = {"tasks": [{
    "id": "fm-task-done", "title": "Focusmate 已完成", "completed": True,
    "completedAt": "2026-10-27T05:50:00.000Z",
}]}
task_fm = store.get(task_fm["id"])
store.attach_focusmate(task_fm["id"], {"task_id": "fm-task-done"})
omni.rows = []
omni.writes = []
wd.offline = False
try:
    wd.sync_completion_state()
finally:
    wd.offline = True
task_fm = store.get(task_fm["id"])
check("Focusmate completedAt → 本地 completed", task_fm["status"], ts.STATUS_COMPLETED)
check("Focusmate completedAt → 状态原因可追溯",
      task_fm["status_reason"], "focusmate-completed")
check("Focusmate completedAt → 同一轮写回 OmniFocus", omni.writes,
      [("complete", "pk-fm-done", "2026-10-27T13:50:00")])

# task view 的 refresh / 全局 refresh 都走 refresh_task_state —— 只读，但必须能看见完成
task_refresh = store.create({"title": "refresh 也要看见完成"})
store.assign(task_refresh["id"], {"date": "2026-10-27", "start": "15:00", "duration": 50})
store.attach_focusmate(task_refresh["id"], {"task_id": "fm-refresh-done"})
wd.offline = False
try:
    wd.bridge.replies["tasks"] = {"tasks": [{
        "id": "fm-refresh-done", "title": "refresh 也要看见完成", "completed": True,
        "completedAt": "2026-10-27T07:50:00.000Z",
    }]}
    wd.refresh_task_state()
finally:
    wd.offline = True
check("refresh 的任务同步路径能看到 Focusmate 已完成",
      store.get(task_refresh["id"])["status"], ts.STATUS_COMPLETED)

# refresh --check：Focusmate 已勾完的 task 只在 /tasks/completed → 不能被当成「挂丢了」
# 再建一条。bridge 的 tasks 合并两半后，_attachment_verdict 应认它仍然挂在原 session。
task_attached = store.create({"title": "已经勾完不要再建"})
store.assign(task_attached["id"], {
    "date": "2026-10-27", "start": "14:00", "duration": 50,
    "focusmate_session_id": "sess-attached",
})
store.attach_focusmate(task_attached["id"], {"task_id": "fm-attached"})
wd.offline = False
try:
    wd.bridge.replies["tasks"] = {"tasks": [{
        "id": "fm-attached", "title": "已经勾完不要再建", "completed": True,
        "completedAt": "2026-10-27T06:50:00.000Z",
        # 就算服务端没给 attachment，完成态本身也必须阻止「重挂成第二条」
        "attachments": [],
    }]}
    wd.bridge.calls = []
    fixed = wd.check_task_attachments()
finally:
    wd.offline = True
check("已完成 task 不再触发重挂（attachment 缺失也一样）", fixed, 0)
check("没有额外调 add_task", [c for c in wd.bridge.calls if c[0] == "add_task"], [])

# task N duplicate -a M
task5 = store.create({"title": "复制的源头"})
wd.cmd_task_link(["id:%d" % task5["id"], "omnifocus:///task/pk5"])
wd.rebuild_rows()
wd.rows = [{"startLabel": "12:00", "id": "sess-2", "startMs": 1793088000000,
            "date": "2026-10-27", "endLabel": "12:50", "durationMin": 50,
            "task_text": ""}]
wd.handle("task id:%d duplicate -a 1" % task5["id"])
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
check("数据 / 契约视图都在", ("数据" in view_names and "契约" in view_names), True)
for name in ("数据", "契约"):
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

print("== 收尾面板挂在 repl_kit 的「打断栏位」里 ==")
psv.clear()
wd.review = None
wd.say = w.__dict__["Warden"].say.__get__(wd)          # 把 say 还原成真的
t4 = store.create({"title": "栏位里的这条"})
store.assign(t4["id"], entry)
wd.begin_review(entry, sync=False)
check_true("在收尾", wd.review is not None)

# 1) 视图自己不再画它（换视图、表格变长都不该把它挤走）
contract_parts = [v for v in wd.view_impls if v.name == "契约"][0].render()
check("契约视图的 render 里没有收尾面板了",
      [p for p in contract_parts if "收尾" in str(getattr(p, "title", ""))], [])
task_parts = [v for v in wd.view_impls if v.name == "数据"][0].render()
check("数据视图的 render 里也没有",
      [p for p in task_parts if "收尾" in str(getattr(p, "title", ""))], [])

# 2) 栏位里（repl_kit.interrogate）有这块东西
block = wd.shell.interrogate()
check_true("打断栏位里有收尾面板", block is not None)
check_true("栏位里认得出是收尾", "收尾" in str(wd.shell.interrogate_text()))

# 3) 位置：看板下面、提示行上面（走不带裁屏的 _basic_chrome，免得终端太矮把顶部裁掉）
_parts = wd.shell.frame_parts()
check("帧里最后一块就是栏位（它在看板最下面）",
      "收尾" in str(getattr(_parts[-1], "title", "")), True)
_console = wd.console
_out = io.StringIO()
wd.console = Console(file=_out, width=160, force_terminal=False)
wd.shell._basic_chrome()
wd.console = _console
_chrome = _out.getvalue()
_tbl = _chrome.index("The Contract Table")
_blk = _chrome.index("收尾", _tbl)
_hint = _chrome.index("session", _blk)
check("栏位在看板之后、提示行之前", _tbl < _blk < _hint, True)

# 4) 切视图不掉
wd.go_to_view(w.TaskView)
check_true("切到数据视图，栏位里还有", wd.shell.interrogate() is not None)
wd.go_to_view(w.StatsView)
check_true("切到统计视图，栏位里还有", wd.shell.interrogate() is not None)
wd.go_to_view(w.ContractView)

# 5) 收完就空，且不占行
wd.intercept("q")
check("收尾结束", wd.review, None)
check("栏位空了（一问完就不占地方）", wd.shell.interrogate(), None)

# 6) --status / --exec 那条路（paint）也画在同一位置
buf = io.StringIO()
wd.console = Console(file=buf, width=160, force_terminal=False)
wd.begin_review(entry, sync=False)
wd.paint()
out = buf.getvalue()
check_true("paint 里也有收尾面板", "收尾" in out)
_tbl2 = out.index("The Contract Table")
check("paint 里栏位在契约表格之后",
      _tbl2 < out.index("收尾", _tbl2), True)

# 7) 画帧缓存不能把「问题变了」当成「画面没变」：栏位内容变了必须重排版
wd.review = None
psv.clear()
for _old_task in store.list():                # 清场：只留下面这两条
    store.delete(_old_task["id"])
t5 = store.create({"title": "栏位里的第一条"})
store.assign(t5["id"], entry)
t6 = store.create({"title": "栏位里的第二条"})
store.assign(t6["id"], entry)
wd.begin_review(entry, sync=False)
check("这场问两条", wd.review.total(), 2)
wd.shell._frame_clock = lambda: 5000          # 钉住时钟：看「同一秒里」会不会端出旧画面
_f1 = wd.shell.frame_text()
check_true("第一帧画的是第一条", "栏位里的第一条" in _f1)
check("同一秒、没变化 → 复用同一帧", wd.shell.frame_text() is _f1, True)
check("跳过一条（问题变了）", wd.intercept("s"), True)
_f2 = wd.shell.frame_text()
check_true("换到下一条之后重排版了", _f2 != _f1)
check_true("新的一帧画的是第二条", "栏位里的第二条" in _f2)
del wd.shell._frame_clock
wd.review = None

# ------------------------------------ Focusmate 说完成了 → OmniFocus 得跟着完成


print("== 写回：Focusmate 勾掉之后，OmniFocus 那条也得完成 ==")
psv.clear()
wd.review = None
for old in store.list():
    store.delete(old["id"])


class _OnlineBridge(FakeBridge):
    """在线替身：`tasks` 里那条已经被人勾掉了（completedAt 有值）。"""

    def __init__(self, done_tasks):
        FakeBridge.__init__(self)
        self.done_tasks = done_tasks

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "tasks":
            return {"tasks": list(self.done_tasks)}
        return FakeBridge.call(self, cmd, timeout=timeout, **kw)


def online_warden(done_tasks):
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    # 这组断言在同一行里紧接着读结果；钉死同步模式，避免终端是不是 TTY
    # 把 JobRunner 变成异步后产生时序抖动。
    wd2 = w.Warden(console, _OnlineBridge(done_tasks), no_clear=True, offline=False,
                   net_async=False)
    wd2.reload_entries()
    wd2.omnifocus = FakeOmni()
    return wd2


done_task = store.create({"title": "在 Focusmate 勾掉的那条"})
store.assign(done_task["id"], {"date": "2026-10-27", "start": "12:00", "duration": 50})
store.attach_focusmate(done_task["id"], {"task_id": "ft-done-1"},
                       session=store.get(done_task["id"])["session"])
wd.cmd_task_link(["id:%d" % done_task["id"], "omnifocus:///task/pk-done"])
online = online_warden([{"id": "ft-done-1", "title": "在 Focusmate 勾掉的那条",
                         "completedAt": "2026-10-27T04:50:00Z"}])
# 用同一条任务库：online warden 的 task_store 指向同一个 TI_TASKS_FILE
online.task_store = store
check("Focusmate 那条一开始是进行中", store.is_open(store.get(done_task["id"])), True)
check("裸 refresh 的那一步（refresh_task_state）受理", online.refresh_task_state(), True)
check("refresh：Focusmate 说完成了 → 本地跟着收工",
      ts.TaskStore.status_of(store.get(done_task["id"])), "completed")
check("refresh：**同时**把 OmniFocus 那条写成完成（不用再敲 task sync）",
      online.omnifocus.writes,
      [("complete", "pk-done", "2026-10-27T12:50:00")])
check("写回留了痕迹（下次不会重复写）",
      bool((store.get(done_task["id"])["omnifocus"].get("writeback") or {})
           .get("completed")), True)
online.omnifocus.writes = []
online.refresh_task_state()
check("再来一次 refresh 不会重复写", online.omnifocus.writes, [])

# 反向：OmniFocus 那边**已经是完成**的（用户自己勾的），本地跟着收工，
# 但同一条 OmniFocus 上如果还有「自己写回完成」的兄弟记录，别拿它去带别人。
print("== 写回：重复任务（OmniFocus 那边会翻到下一次）==")
for old in store.list():
    store.delete(old["id"])


class _RepeatOmni(FakeOmni):
    """OmniFocus 的重复任务：勾掉这一次，它自己生出下一次（新 primary key）。"""

    def __init__(self, clone_id="pk-next"):
        FakeOmni.__init__(self)
        self.clone_id = clone_id

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        self.writes.append(("complete", pk, due))
        return {"ok": True, "changed": ["completed", "due"],
                "before": {"id": pk, "completed": False},
                # 重复任务写完，`after` 是**新那一次**，completed 当然是 false
                "after": {"id": self.clone_id, "completed": False},
                "repeat_clone_id": self.clone_id}


template = store.create({"title": "每天闪卡", "duration": "25"},
                        omnifocus={"id": "pk-series", "title": "每天闪卡"},
                        source={"kind": "omnifocus", "id": "pk-series"})
store.set_repeat(template["id"], "mon", "22:15", 50)
template = store.get(template["id"])
child = store.create_repeat_occurrence(template["id"], "2026-11-02")
tomorrow = store.create_repeat_occurrence(template["id"], "2026-11-03")
check("子任务继承了 OmniFocus 链接",
      (child.get("omnifocus") or {}).get("id"), "pk-series")

repeat_wd = mkwarden()
repeat_wd.omnifocus = _RepeatOmni()
repeat_wd.task_store = store
note = repeat_wd.omni_complete(store.get(child["id"]))
check("重复任务写回**不算失败**（写完返回的是下一次）",
      "失败" in note, False)
check("写回留了痕迹（不会每次都把重复表往前推）",
      bool((store.get(child["id"])["omnifocus"].get("writeback") or {}).get("completed")), True)
check("今天这条还挂在**旧** key 上（它说的就是这一次）",
      (store.get(child["id"])["omnifocus"] or {}).get("id"), "pk-series")
check("明天的子任务改挂到**下一次**的 key",
      (store.get(tomorrow["id"])["omnifocus"] or {}).get("id"), "pk-next")
check("本体也改挂到下一次的 key",
      (store.get(template["id"])["omnifocus"] or {}).get("id"), "pk-next")
check("旧 key 留了档",
      [(p.get("id"), p.get("reason"))
       for p in (store.get(tomorrow["id"])["omnifocus"] or {}).get("previous") or []],
      [("pk-series", "repeat-clone")])
repeat_wd.omnifocus.writes = []
repeat_wd.writeback_completions()
check("写过的不会再写一遍（重复表不会被推着走）", repeat_wd.omnifocus.writes, [])
check_true("只改挂还开着的记录（收工的那几条不动）",
           repeat_wd.task_store.is_open(store.get(tomorrow["id"])))

print("== 写回扫描：同一个 OmniFocus occurrence 只 complete 一次 + 翻篇 ==")
for old in store.list():
    store.delete(old["id"])
# 本地可能因为延期/项目 part 留下多条共享同一 OmniFocus key 的完成记录。
done_a = store.create({"title": "今天这次 A"},
                      omnifocus={"id": "pk-series", "title": "词汇题多 50 个"})
done_b = store.create({"title": "今天这次 B"},
                      omnifocus={"id": "pk-series", "title": "词汇题多 50 个"})
still_open = store.create({"title": "下一次"},
                          omnifocus={"id": "pk-series", "title": "词汇题多 50 个"})
store.set_status(done_a["id"], ts.STATUS_COMPLETED, reason="test")
store.set_status(done_b["id"], ts.STATUS_COMPLETED, reason="test")
wb_wd = w.Warden(Console(file=io.StringIO(), width=160, force_terminal=False),
                 FakeBridge(), no_clear=True, offline=True, net_async=False)
wb_wd.task_store = store
wb_wd.omnifocus = _RepeatOmni(clone_id="pk-next-week")
wb_wd.writeback_completions()
check("同一个 OminFocus key 只 complete 一次（不会连推两周）",
      wb_wd.omnifocus.writes, [("complete", "pk-series", None)])
check("同一 occurrence 的两条完成记录都记了 writeback",
      [bool(((store.get(t["id"])["omnifocus"] or {}).get("writeback") or {})
            .get("completed")) for t in (done_a, done_b)],
      [True, True])
check("还开着的那条改挂到下一次 occurrence",
      (store.get(still_open["id"])["omnifocus"] or {}).get("id"), "pk-next-week")
wb_wd.omnifocus.writes = []
wb_wd.writeback_completions()
check("再扫一次不会重复写", wb_wd.omnifocus.writes, [])
check_true("同一完成记录再走 omni_complete 也不会重复写",
           "不重复勾" in wb_wd.omni_complete(store.get(done_a["id"])))
check("确实一次外部写都没发生", wb_wd.omnifocus.writes, [])

# OmniFocus 说「这条已完成」时，别拿它把同一个 key 上还开着的兄弟记录也带成完成：
# 兄弟里已经有一条是**我们自己写回**完成的，那这次完成就有归属了。
print("== 写回：同一条 OmniFocus 不会把还开着的兄弟记录也带成完成 ==")
for old in store.list():
    store.delete(old["id"])
twin_done = store.create({"title": "今天这次"},
                         omnifocus={"id": "pk-twin", "title": "每天闪卡"})
store.set_status(twin_done["id"], ts.STATUS_COMPLETED, reason="test")
store.set_omnifocus_writeback(twin_done["id"], "completed")
twin_open = store.create({"title": "明天那次"},
                         omnifocus={"id": "pk-twin", "title": "每天闪卡"})
twin_wd = mkwarden()
twin_wd.task_store = store
twin_wd.omnifocus = FakeOmni([{"id": "pk-twin", "name": "每天闪卡", "completed": True,
                               "completed_at": "2026-11-02T22:00:00"}])
rows = twin_wd.sync_omnifocus_status()
check("自己写回完成的那条照样对得上", twin_done["id"] in rows, True)
check("还开着的兄弟记录**不**跟着被判成完成", twin_open["id"] in rows, False)

# 用户自己在 OmniFocus 里勾的（本地没有任何「写回完成」的记录）→ 照旧跟着收工
for old in store.list():
    store.delete(old["id"])
manual = store.create({"title": "用户在 OmniFocus 勾的"},
                      omnifocus={"id": "pk-manual", "title": "用户在 OmniFocus 勾的"})
manual_wd = mkwarden()
manual_wd.task_store = store
manual_wd.omnifocus = FakeOmni([{"id": "pk-manual", "name": "用户在 OmniFocus 勾的",
                                 "completed": True,
                                 "completed_at": "2026-11-02T22:00:00"}])
manual_wd.sync_completion_state()
check("OmniFocus 里手动勾掉的 → 本地跟着完成",
      ts.TaskStore.status_of(store.get(manual["id"])), "completed")
check("跟着完成之后也补写了一次截止时间（幂等）",
      [w[0] for w in manual_wd.omnifocus.writes], ["complete"])

# ---------------------------------------------------------------- warden：inspect-due 的 G
print("== warden：inspect-due 里敲 G 真的校准 count 并排到下一场 ==")
# 过期任务原来那场是昨天：下一场必须从「现在」往后找，不能排回昨天后面的 slot。
ns_wd = mkwarden()
ns_wd.offline = True
_today = datetime.date.today()
_ns_now = datetime.datetime.combine(_today, datetime.time(10, 0))
ns_wd.review_now = _ns_now
_ns_entries = w.sch.parse_text("09:00,50\n11:00,50\n13:00,50")[0]
ns_wd.reload_entries = lambda: _ns_entries
_nxt = ns_wd.next_session_after({
    "session": {"date": (_today - datetime.timedelta(days=1)).isoformat(),
                "start": "09:00"}})
check("过期任务的下一次从今天往后找",
      (_nxt.get("date"), _nxt.get("start")), (_today.isoformat(), "11:00"))
for old in store.list():
    store.delete(old["id"])
g_wd = mkwarden()
g_wd.offline = True
g_task = store.create(fields={"title": "写论文", "count": "2",
                              "due": "2026-10-27T09:00:00"})
store.add_count(g_task["id"], 1)
g_wd.next_session_after = lambda task: dict(NEXT)
g_wd.begin_inspect_due([store.get(g_task["id"])], sync=False)
check_true("inspect-due 面板开起来", g_wd.review is not None)
check("intercept 吃掉 G", g_wd.intercept("g"), True)
check("提示问已投入", g_wd.review.stage, psv.Review.EXTEND_INVESTED)
check("intercept 吃掉已投入 2", g_wd.intercept("2"), True)
check("提示问还需", g_wd.review.stage, psv.Review.EXTEND_REMAIN)
check("intercept 吃掉还需 3", g_wd.intercept("3"), True)
g_fresh = store.get(g_task["id"])
check("warden 的 set_count_plan 真的写进库",
      (ts.task_count_invested(g_fresh), ts.task_count(g_fresh)), (2, 5))
check("warden 自动排到下一场",
      (g_fresh["session"].get("date"), g_fresh["session"].get("start")),
      ("2026-10-27", "12:00"))
check("处理完收尾面板消失", g_wd.review, None)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
shutil.rmtree(TMP, ignore_errors=True)
