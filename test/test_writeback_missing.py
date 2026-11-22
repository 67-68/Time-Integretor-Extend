#!/usr/bin/env python3
"""OmniFocus 写回 not_found：终局失败只记一次，不能每次 refresh 都重试。

现场（2026-11-22）：任务 #249「每日考试」在 11-20 就完成了，它关联的 OmniFocus
任务已经被删掉（``Task.byIdentifier`` 返回 null）。本地没有 writeback 标记，于是
每次扫描都把它重新排进 flush；JXA 报 ``not_found``，``net_omnifocus_flush`` 抛
「全部写回失败：not_found」，面板弹一句「已记进失败账，re-action 可以重放」——
但它其实 ``persist=False``，根本没进失败账。结果：

    * 一天几十条 WARNING / ERROR，永远不会停；
    * 提示语在撒谎（说可以 re-action，其实账上没有）。

修法：``not_found`` / ``project_root_refused`` 是终局 → 在本地 ``omnifocus`` 上
记 ``missing``，写回扫描从此跳过；flush 失败不再冒用「失败账」那句话。

    python3 test/test_writeback_missing.py
"""
import importlib.util
import io
import os
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


TMP = tempfile.mkdtemp(prefix="wb-missing-")
for env, name in (("TI_TASKS_FILE", "tasks.json"),
                  ("TI_PROJECTS_FILE", "projects.json"),
                  ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
                  ("TI_NOSTART_FILE", "nostart.json"),
                  ("TI_TASK_ARCHIVE", "archive.json"),
                  ("TI_SETTINGS_FILE", "settings.json"),
                  ("TI_SESSION_LAYOUTS", "layouts.json"),
                  ("TI_SESSION_EXTRAS", "extras.json"),
                  ("TI_SESSION_RULES", "rules.json"),
                  ("TI_CALENDAR_DOWNLOAD", "download.json"),
                  ("TI_FAILED_ACTIONS", "failed-actions.json"),
                  ("TI_OMNIFOCUS_OUTBOX", "outbox.json")):
    os.environ[env] = os.path.join(TMP, name)
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

ts = w.ts


class StubBridge(object):
    def call(self, *a, **k):
        raise RuntimeError("这组用例不该碰 bridge")

    def start(self):
        pass

    def stop(self):
        pass


class GoneOmni(object):
    """OmniFocus 里那条任务已经不存在：flush 一律 not_found（和现场一样）。"""

    def __init__(self):
        self.flushed = []

    def available(self):
        return True

    def why_unavailable(self):
        return ""

    def list_tasks(self, **k):
        return []

    def repetition_info(self, pk):
        return {}

    def flush(self, ops, timeout=None):
        self.flushed.append([dict(op) for op in ops])
        return {"ok": False, "results": [
            {"id": op.get("id"), "task_id": op.get("task_id"),
             "op": op.get("op") or "complete", "ok": False, "error": "not_found"}
            for op in ops]}

    def complete(self, *a, **k):
        raise AssertionError("走 flush，不该走单条 complete")


def settle(wd, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wd.jobs.wait(max(0.01, deadline - time.time()))
        wd.drain_net()
        if not wd.jobs.busy:
            return True
        time.sleep(0.01)
    return False


def mkwarden():
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    wd = w.Warden(console, StubBridge(), no_clear=True, offline=False, net_async=True)
    wd.omnifocus = GoneOmni()
    wd.settings.set("omnifocus", "on")
    wd.refresh = lambda *a, **k: False
    wd.refresh_task_state = lambda *a, **k: False
    wd.auto_refresh_after_command = False
    return wd


print("== 1. 一条 not_found 写回：只试一次，之后不再重试 ==")
wd = mkwarden()
wd.omni_outbox.clear()
task = wd.task_store.create(fields={"title": "每日考试"})
wd.task_store.link_omnifocus(task["id"], "gone-pk")
wd.task_store.set_status(task["id"], ts.STATUS_COMPLETED, reason="test")

check("第一次扫描会把它排上", [op["id"] for op in wd.pending_writebacks()], ["gone-pk"])
wd.messages = []
check_true("flush 排上了", wd.flush_omnifocus(wd.pending_writebacks()))
settle(wd)
check("只调用了一次 flush", len(wd.omnifocus.flushed), 1)
check("本地记下 not_found 终局标记",
      bool(ts.TaskStore.omnifocus_missing(wd.task_store.get(task["id"]))), True)
check("没有记 writeback（不能假装写回去了）",
      bool(((wd.task_store.get(task["id"]).get("omnifocus") or {}).get("writeback")
            or {}).get("completed")), False)
check("第二次扫描不再重试", wd.pending_writebacks(), [])
check("第二次 flush 直接空转", wd.flush_omnifocus(), False)
settle(wd)
check("flush 确实只跑过一次", len(wd.omnifocus.flushed), 1)
check_true("提示说的是「停止重试」，不是「re-action 可以重放」",
           any("停止重试" in text for _stamp, _kind, text in wd.messages))
check_true("没有冒用失败账那句话",
           not any("re-action" in text for _stamp, _kind, text in wd.messages))

print("\n== 2. 重新 link 会清掉 missing 标记（改挂另一条还能写）==")
wd.task_store.link_omnifocus(task["id"], "fresh-pk")
check("link 之后 missing 没了",
      ts.TaskStore.omnifocus_missing(wd.task_store.get(task["id"])), None)
check("也能重新排进写回", [op["id"] for op in wd.pending_writebacks()], ["fresh-pk"])

print("\n== 3. 其他失败（不是 not_found）仍然留在账上重试 ==")
wd.task_store.set_omnifocus_writeback(task["id"], "completed")  # 清掉上一步留下的 fresh-pk
wd2 = mkwarden()


class FlakyOmni(GoneOmni):
    def flush(self, ops, timeout=None):
        self.flushed.append([dict(op) for op in ops])
        return {"ok": False, "results": [
            {"id": op.get("id"), "task_id": op.get("task_id"),
             "op": op.get("op") or "complete", "ok": False,
             "error": "osascript 超时"} for op in ops]}


wd2.omnifocus = FlakyOmni()
task2 = wd2.task_store.create(fields={"title": "普通失败"})
wd2.task_store.link_omnifocus(task2["id"], "flaky-pk")
wd2.task_store.set_status(task2["id"], ts.STATUS_COMPLETED, reason="test")
wd2.flush_omnifocus(wd2.pending_writebacks())
settle(wd2)
check("普通失败不标 missing",
      ts.TaskStore.omnifocus_missing(wd2.task_store.get(task2["id"])), None)
check("下一轮还会重试", [op["id"] for op in wd2.pending_writebacks()], ["flaky-pk"])

print("\n%s" % ("全绿。" if not fails else "\n".join(fails)))
raise SystemExit(1 if fails else 0)
