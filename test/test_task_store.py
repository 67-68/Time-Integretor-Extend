#!/usr/bin/env python3
"""task_store.py 回归测试：任意字段 CRUD + session 关联 + Focusmate 完成归档。"""
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
check("归档文件仍是 1 条", len(store.load_archive()["records"]), 1)

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
