#!/usr/bin/env python3
"""本地优先写回：outbox（攒账）+ 空闲触发 + 一次批量 flush。

用户原话：「本地修改状态（之后 refresh 的时候更新，包括 focusmate 任务。比如说
空闲 90s 自动 refresh）」+「空闲的时候（刷新完成之后另外一次）间隔 600s」。

    python3 test/test_omnifocus_outbox.py
"""
import importlib.util
import os
import shutil
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="omni-outbox-")
outbox_path = os.path.join(TMP, "outbox.json")

os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_PROJECTS_FILE"] = os.path.join(TMP, "projects.json")
os.environ["TI_PROJECT_SYNC_FILE"] = os.path.join(TMP, "project-sync.json")
os.environ["TI_NOSTART_FILE"] = os.path.join(TMP, "nostart.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["TI_FAILED_ACTIONS"] = os.path.join(TMP, "failed-actions.json")
os.environ["TI_OMNIFOCUS_OUTBOX"] = outbox_path

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

ts = w.ts


class StubBridge(object):
    last_idle_close = None

    def call(self, *a, **k):
        raise RuntimeError("这组用例不该碰 bridge")

    def start(self):
        pass

    def stop(self):
        pass


class FakeOmni(object):
    """记下 flush 被调了几次、每次几条。"""

    def __init__(self):
        self.flushed = []

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, refresh=False, fields=None, timeout=None,
                   with_repetition=False):
        return []

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        return {"after": {"completed": True}, "changed": ["completed"]}

    def drop(self, pk, at=None, all_occurrences=False, yes=True, timeout=None):
        return {"after": {"dropped": True}, "changed": ["dropped"]}

    def flush(self, ops, timeout=None):
        self.flushed.append([dict(op) for op in ops])
        results = []
        for op in ops:
            results.append({"id": op.get("id"), "task_id": op.get("task_id"),
                            "op": op.get("op") or "complete", "ok": True,
                            "after": {"completed": op.get("op") != "drop",
                                      "dropped": op.get("op") == "drop"},
                            "changed": ["completed"]})
        return {"ok": True, "results": results}

    def invalidate(self):
        pass


def mkwarden():
    console = Console(file=__import__("io").StringIO(), width=160, force_terminal=False)
    wd = w.Warden(console, StubBridge(), no_clear=True, offline=False, net_async=True)
    wd.omnifocus = FakeOmni()
    wd.settings.set("omnifocus", "on")
    # 空闲触发会顺带 refresh；这组用例只关心写回，屏蔽掉网络那半。
    wd.refresh = lambda *a, **k: False
    wd.refresh_task_state = lambda *a, **k: False
    wd.auto_refresh_after_command = False
    return wd


def settle(wd, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wd.jobs.wait(max(0.05, deadline - time.time()))
        wd.drain_net()
        if not wd.jobs.busy:
            return True
        time.sleep(0.01)
    return False


print("== 1. outbox：同一条只留最后一次意图，落盘能读回来 ==")
outbox = w.OmniOutbox(outbox_path)
outbox.clear()
outbox.put({"id": "pk1", "op": "complete", "task_id": 1, "due": None})
outbox.put({"id": "pk1", "op": "complete", "task_id": 1, "due": "2026-11-12T11:50:00"})
check("同一 (op, pk) 只留一条", len(outbox), 1)
check("留的是最新那次（带 due）", outbox.ops[0]["due"], "2026-11-12T11:50:00")
outbox.put({"id": "pk1", "op": "drop", "task_id": 1})
check("不同 op 可以共存", len(outbox), 2)
again = w.OmniOutbox(outbox_path)
check("落盘读回来", len(again), 2)
check("remove 按 key 删", again.remove(["complete:pk1"]), 1)
check("删完只剩 drop", [op["op"] for op in again], ["drop"])
again.clear()

print("\n== 2. pending_writebacks：outbox + 扫描合并、按 (op, pk) 去重 ==")
wd = mkwarden()
wd.omni_outbox.clear()
task = wd.task_store.create(fields={"title": "写论文"})
wd.task_store.link_omnifocus(task["id"], "pk-1")
wd.pending = None
wd.omni_outbox.put({"id": "pk-1", "op": "complete", "task_id": task["id"]})
ops = wd.pending_writebacks()
check("扫出来的和 outbox 里的是同一条，只留一个 op", len(ops), 1)
check("op 是 complete", ops[0]["op"], "complete")
check("带 due 字段（没有就是 None）", ops[0].get("due"), None)

# 本地已完成（没进 outbox）也要被扫出来
task2 = wd.task_store.create(fields={"title": "复习"})
wd.task_store.link_omnifocus(task2["id"], "pk-2")
wd.task_store.set_status(task2["id"], ts.STATUS_COMPLETED, reason="test")
ops = wd.pending_writebacks()
check("扫描补上第二条", sorted(op["id"] for op in ops), ["pk-1", "pk-2"])

# 已写回过的不会被再扫一次
wd.task_store.set_omnifocus_writeback(task2["id"], "completed")
ops = wd.pending_writebacks()
check("写回过的扫描跳过", [op["id"] for op in ops], ["pk-1"])
wd.omni_outbox.clear()

print("\n== 3. flush：N 条合成一次调用 ==")
wd = mkwarden()
wd.omni_outbox.clear()
for i in range(3):
    task = wd.task_store.create(fields={"title": "任务%d" % i})
    wd.task_store.link_omnifocus(task["id"], "pk-%d" % i)
    wd.omni_outbox.put({"id": "pk-%d" % i, "op": "complete", "task_id": task["id"]})
check("攒了 3 条", len(wd.omni_outbox), 3)
check_true("flush 排上了", wd.flush_omnifocus())
settle(wd)
check("只调用了一次 flush", len(wd.omnifocus.flushed), 1)
check("一次带走 3 条", len(wd.omnifocus.flushed[0]), 3)
check("outbox 清空", len(wd.omni_outbox), 0)
check("三条都记了 writeback",
      [bool(((wd.task_store.get(t["id"]).get("omnifocus") or {}).get("writeback")
             or {}).get("completed"))
       for t in wd.task_store.list() if str(t.get("fields", {}).get("title", "")).startswith("任务")],
      [True, True, True])

print("\n== 4. 空闲触发：90s 一次，之后每 600s 一次，有人动就重置 ==")
wd = mkwarden()
wd.omni_outbox.clear()
task = wd.task_store.create(fields={"title": "空闲写回"})
wd.task_store.link_omnifocus(task["id"], "pk-idle")
wd.omni_outbox.put({"id": "pk-idle", "op": "complete", "task_id": task["id"]})

idle = {"sec": 30.0}
real_idle = w.ks.hid_idle_seconds
w.ks.hid_idle_seconds = lambda: idle["sec"]

wd._omni_idle_checked = 0.0
wd._omni_idle_flush_at = 0.0
check("没到 90s 不写", wd._maybe_flush_omnifocus(now=1000.0), False)
check("outbox 还在", len(wd.omni_outbox), 1)

idle["sec"] = 100.0
check("到 90s 写一次", wd._maybe_flush_omnifocus(now=1010.0), True)
settle(wd)
check("写完了", len(wd.omni_outbox), 0)

# 又攒一条，仍在同一段空闲里
task2 = wd.task_store.create(fields={"title": "第二笔"})
wd.task_store.link_omnifocus(task2["id"], "pk-idle2")
wd.omni_outbox.put({"id": "pk-idle2", "op": "complete", "task_id": task2["id"]})
wd._omni_idle_checked = 0.0
check("600s 内不再写", wd._maybe_flush_omnifocus(now=1100.0), False)
wd._omni_idle_checked = 0.0
check("过了 600s 再写一次", wd._maybe_flush_omnifocus(now=1700.0), True)
settle(wd)

idle["sec"] = 5.0
wd._omni_idle_flush_at = 1700.0
wd._omni_idle_checked = 0.0
wd._maybe_flush_omnifocus(now=1800.0)
check("有人动 → 计时重置", wd._omni_idle_flush_at, 0.0)

w.ks.hid_idle_seconds = real_idle

print("\n== 5. 同步模式（--exec / 测试）仍然当场写 ==")
wd = mkwarden()
wd.net_async = False
wd.jobs.sync = True
wd.omni_outbox.clear()
task = wd.task_store.create(fields={"title": "同步写"})
wd.task_store.link_omnifocus(task["id"], "pk-sync")
note = wd.omni_complete(wd.task_store.get(task["id"]))
check("同步模式直接写（提示语不是「已记本地账」）", "已记本地账" in note, False)
check("outbox 没留东西", len(wd.omni_outbox), 0)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    raise SystemExit(1)
print("all checks passed")
