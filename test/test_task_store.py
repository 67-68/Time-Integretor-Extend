#!/usr/bin/env python3
"""task_store.py 回归测试：任意字段 CRUD + session 关联 + Focusmate 完成归档。"""
import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import task_store as ts

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="task-store-test-")
TASKS = os.path.join(TMP, "tasks.json")
ARCHIVE = os.path.join(TMP, "archive.json")
store = ts.TaskStore(TASKS, ARCHIVE)

print("== CRUD：字段不校验 ==")
a = store.create({"title": "写论文", "due": "明天", "priority": 3,
                  "任意 key": {"nested": True}})
check("自动编号从 1 开始", a["id"], 1)
check("任意字段原样保存", store.get(1)["fields"]["任意 key"], {"nested": True})
b = store.create({"title": "复习"}, task_id=7)
check("可以显式指定 ID", b["id"], 7)
check("list 按 ID 排序", [t["id"] for t in store.list()], [1, 7])

store.modify(1, "due", "后天")
check("modify 改任意字段", store.get(1)["fields"]["due"], "后天")
store.modify(1, "new_field", "")
check("空值也照收（不做语义检查）", store.get(1)["fields"]["new_field"], "")

s = store.summarize(store.get(1), max_len=200)
check_true("多字段压成 1. / 2. 一行", s.startswith("1. ") and "  2. " in s)
check("摘要里没有换行", "\n" in s, False)

store.delete(7)
check("delete 真删掉", [t["id"] for t in store.list()], [1])
try:
    store.get(7)
    check("取不存在的任务应报错", False, True)
except ValueError as e:
    check_true("不存在的任务给明确错误", "没有任务 7" in str(e))

print("== session 关联 + Focusmate 完成归档 ==")
session = {"index": 2, "date": "2026-10-26", "start": "11:00", "end": "11:50",
           "duration": 50, "focusmate_session_id": "sess-1"}
store.assign(1, session)
check("session 落盘", store.get(1)["session"]["focusmate_session_id"], "sess-1")
check("Focusmate title 优先 title 字段", store.focusmate_title(store.get(1)), "写论文")
store.attach_focusmate(1, {"task_id": "ft-1", "session_id": "sess-1"}, session=session)

remote = {"tasks": [
    {"id": "ft-1", "title": "写论文", "completedAt": "2026-10-26T12:00:00Z",
     "attachments": [{"type": "session", "externalId": "sess-1"}]},
]}
n, matched = store.reconcile(remote)
check("匹配到本地 Focusmate task", matched, [1])
check("新归档 1 条", n, 1)
check("本地也记下 completed_at",
      store.get(1)["focusmate"]["completed_at"], "2026-10-26T12:00:00Z")
arch = store.load_archive()["records"]
check("归档里有完成时间", arch[0]["focusmate"]["completed_at"], "2026-10-26T12:00:00Z")
check("归档里有 session 信息", arch[0]["session"]["start"], "11:00")
check("归档里保留任务字段", arch[0]["fields"]["title"], "写论文")
check("reconcile 幂等：第二次不重复归档", store.reconcile(remote)[0], 0)
# Focusmate 那边勾了完成 → 本地这条记录也**落库**成 completed（不只是归档）。
# 以前只靠 _coerce_task 的老数据兼容规则兜：读出来是完成、写回去没状态，
# 界面上的 ✓ 时有时无（用户反馈的「任务状态同步不对」）。
check("reconcile 把状态也落成 completed", store.get(1)["status"], ts.STATUS_COMPLETED)
check("status_at 用 Focusmate 的完成时间",
      store.get(1)["status_at"], "2026-10-26T12:00:00Z")
check("原因留痕", store.get(1)["status_reason"], "focusmate-completed")
check("落盘里也真有 status（不是靠读时兜）",
      [t.get("status") for t in json.load(open(TASKS))["tasks"] if t["id"] == 1],
      [ts.STATUS_COMPLETED])
check("归档文件仍是 1 条", len(store.load_archive()["records"]), 1)

print("== 备注字段：note / notes / 备注 都收敛成 notes ==")
n1 = store.create({"title": "带备注", "note": "https://example.com/a"})
check("create 时 note= 收敛成 notes", store.get(n1["id"])["fields"]["notes"],
      "https://example.com/a")
check("notes() 读得到", store.notes(store.get(n1["id"])), "https://example.com/a")
store.modify(n1["id"], "备注", "换个网址 https://example.com/b")
check("modify 用中文别名也收敛", store.get(n1["id"])["fields"]["notes"],
      "换个网址 https://example.com/b")
check("没有备注就是空串", store.notes(store.get(1)), "")
store.set_notes(n1["id"], "先看 https://example.com/c 再说")
check("set_notes 直接写", store.notes(store.get(n1["id"])), "先看 https://example.com/c 再说")
store.set_notes(n1["id"], "")
check("空值 = 清掉", store.notes(store.get(n1["id"])), "")

print("== 一场 session 能挂多个任务 ==")
t2 = store.create({"title": "第二条"})
t3 = store.create({"title": "第三条"})
same = {"index": 2, "date": "2026-10-26", "start": "11:00", "end": "11:50",
        "duration": 50, "focusmate_session_id": "sess-1"}
store.assign(1, same)
store.assign(t2["id"], dict(same))
store.assign(t3["id"], dict(same))
check("tasks_for_session 拿到三条",
      [t["id"] for t in store.tasks_for_session("2026-10-26", "11:00")], [1, t2["id"], t3["id"]])
check("别的时段不算", [t["id"] for t in store.tasks_for_session("2026-10-26", "12:00")], [])
store.assign(t2["id"], {"index": 3, "date": "2026-10-26", "start": "12:00", "end": "12:50",
                        "duration": 50, "focusmate_session_id": "sess-2"})
check("重挂到别的场次不影响同场的其他任务",
      [t["id"] for t in store.tasks_for_session("2026-10-26", "11:00")], [1, t3["id"]])
check("新场次里只有它自己",
      [t["id"] for t in store.tasks_for_session("2026-10-26", "12:00")], [t2["id"]])

print("== 任务改挂到别的场次：旧 task 的完成时间也不丢 ==")
store2 = ts.TaskStore(os.path.join(TMP, "t2.json"), os.path.join(TMP, "a2.json"))
x = store2.create({"title": "改挂我"})
old_sess = {"index": 1, "date": "2026-10-26", "start": "11:00", "end": "11:50",
            "duration": 50, "focusmate_session_id": "sess-A"}
new_sess = {"index": 2, "date": "2026-10-27", "start": "09:00", "end": "09:50",
            "duration": 50, "focusmate_session_id": "sess-B"}
store2.assign(x["id"], old_sess)
store2.attach_focusmate(x["id"], {"task_id": "ft-A", "session_id": "sess-A"}, session=old_sess)
store2.assign(x["id"], new_sess)
store2.attach_focusmate(x["id"], {"task_id": "ft-B", "session_id": "sess-B"}, session=new_sess)
check("旧 task id 记进 previous",
      [p["task_id"] for p in store2.get(x["id"])["focusmate"]["previous"]], ["ft-A"])
check("previous 里留着旧场次",
      store2.get(x["id"])["focusmate"]["previous"][0]["session"]["start"], "11:00")
n2, _ = store2.reconcile({"tasks": [
    {"id": "ft-A", "title": "改挂我", "completedAt": "2026-10-26T11:50:00Z"},
    {"id": "ft-B", "title": "改挂我", "completedAt": "2026-10-27T09:50:00Z"},
]})
check("两条都归档", n2, 2)
recs = store2.load_archive()["records"]
check("归档里的场次各自对得上",
      sorted(r["session"]["start"] for r in recs), ["09:00", "11:00"])

print("== unassign：断绑定，但任务和完成时间都留着 ==")
u = store2.create({"title": "会被断开的"})
sess_u = {"index": 1, "date": "2026-10-26", "start": "11:00", "end": "11:50",
          "duration": 50, "focusmate_session_id": "sess-U"}
store2.assign(u["id"], sess_u)
store2.attach_focusmate(u["id"], {"task_id": "ft-U", "session_id": "sess-U",
                                  "completed_at": "2026-10-26T11:50:00Z"},
                        session=sess_u)
store2.unassign(u["id"], reason="layout-changed")
after = store2.get(u["id"])
check("任务还在", after["fields"]["title"], "会被断开的")
check("session 断了", after["session"], None)
check("Focusmate task_id 清掉", after["focusmate"].get("task_id"), None)
check("完成时间不抹（那是历史）",
      after["focusmate"].get("completed_at"), "2026-10-26T11:50:00Z")
prev = after["focusmate"]["previous"][0]
check("旧 task 记进 previous", prev["task_id"], "ft-U")
check("previous 里带着当时那场 session", prev["session"]["start"], "11:00")
check("previous 记着原因", prev["reason"], "layout-changed")
try:
    store2.unassign(999)
    check("断不存在的任务应报错", False, True)
except ValueError as e:
    check_true("不存在的任务给明确错误", "没有任务 999" in str(e))

print("== 归档记录里带备注 ==")
store.set_notes(1, "材料在这 https://example.com/set3")
store3 = ts.TaskStore(os.path.join(TMP, "t3.json"), os.path.join(TMP, "a3.json"))
y = store3.create({"title": "有备注的任务", "notes": "https://example.com/z"})
store3.assign(y["id"], {"index": 1, "date": "2026-10-26", "start": "11:00",
                        "duration": 50, "focusmate_session_id": "sess-Z"})
store3.attach_focusmate(y["id"], {"task_id": "ft-Z"})
store3.reconcile({"tasks": [{"id": "ft-Z", "title": "有备注的任务",
                             "completedAt": "2026-10-26T11:50:00Z"}]})
rec = store3.load_archive()["records"][0]
check("归档里有 notes 字段", rec["notes"], "https://example.com/z")
check("归档里 fields 也留着", rec["fields"]["notes"], "https://example.com/z")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ts.main(["--file", os.path.join(TMP, "t3.json"),
             "--archive", os.path.join(TMP, "a3.json"), "note", str(y["id"]), "改成",
             "https://example.com/new"])
check("CLI note 能改备注", "https://example.com/new" in buf.getvalue(), True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ts.main(["--file", os.path.join(TMP, "t3.json"),
             "--archive", os.path.join(TMP, "a3.json"), "archive"])
check("CLI archive 打印备注", "备注：https://example.com/z" in buf.getvalue(), True)

print("== 归档 CLI：一个 JSON 文件，一眼能看 ==")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ts.main(["--file", TASKS, "--archive", ARCHIVE, "archive"])
out = buf.getvalue()
check("archive 退出码 0", rc, 0)
check("archive 打印归档路径", ARCHIVE in out, True)
check("archive 有条数", "共 1 条" in out, True)
check("archive 按完成日期分组", "2026-10-26  1 条" in out, True)
check("archive 列出任务和 session", "#1" in out and "11:00" in out, True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ts.main(["--file", TASKS, "--archive", ARCHIVE, "archive", "--json"])
check("--json 原样打出整个文件", '"records"' in buf.getvalue(), True)

print("== 转移之后：本地把 Focusmate task 忘掉（旧 id 进 previous）==")
import tempfile as _tf
store2 = ts.TaskStore(os.path.join(_tf.mkdtemp(), "t.json"),
                      os.path.join(_tf.mkdtemp(), "a.json"))
t = store2.create({"title": "写论文"})
store2.assign(t["id"], {"date": "2026-10-27", "start": "11:00", "duration": 50})
store2.attach_focusmate(t["id"], {"task_id": "ft-9"})
check("先记着 task_id", store2.get(t["id"])["focusmate"]["task_id"], "ft-9")
store2.forget_focusmate(t["id"], reason="transfer")
fm = store2.get(t["id"])["focusmate"]
check("当前那格清空", fm.get("task_id"), None)
check("旧 id 进 previous（归档还要用）",
      [p["task_id"] for p in fm.get("previous") or []], ["ft-9"])
check("记下了为什么删", (fm.get("deleted_reason"), bool(fm.get("deleted_at"))),
      ("transfer", True))
check("任务本身还在（没被删）", store2.get(t["id"])["fields"]["title"], "写论文")

print("== 坏 JSON 不炸 ==")
with open(TASKS, "w", encoding="utf-8") as f:
    f.write("{not json")
with open(ARCHIVE, "w", encoding="utf-8") as f:
    f.write("[]broken")
check("坏文件读成空库", store.list(), [])
check("坏归档读成空归档", store.load_archive()["records"], [])

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
