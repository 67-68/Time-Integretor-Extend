#!/usr/bin/env python3
"""re-action 的「跑一次就够」合并：失败账只留一条、重放只跑一次。

用户原话：「让 re-action 不要重复执行类似『每日检查 OmniFocus 项目（1 个）』
这样的、运行一个就行的任务；他们如果失败，算一个失败就行了，因为我只需要做
一个就可以检查完成全部。」

    python3 test/test_reaction_coalesce.py
"""
import importlib.util
import json
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


TMP = tempfile.mkdtemp(prefix="reaction-coalesce-")
STORE = os.path.join(TMP, "failed-actions.json")

# warden.py 一 import 就会读一堆 data/ 路径；全部钉到临时目录（和 test_async_ops 一样）
for env, name in (
    ("TI_FAILED_ACTIONS", "failed-actions.json"),
    ("TI_TASKS_FILE", "tasks.json"),
    ("TI_PROJECTS_FILE", "projects.json"),
    ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
    ("TI_NOSTART_FILE", "nostart.json"),
    ("TI_TASK_ARCHIVE", "archive.json"),
    ("TI_SETTINGS_FILE", "settings.json"),
    ("TI_SESSION_LAYOUTS", "layouts.json"),
    ("TI_SESSION_EXTRAS", "extras.json"),
    ("TI_SESSION_RULES", "rules.json"),
    ("TI_CALENDAR_DOWNLOAD", "download.json"),
    ("TI_LOG_DIR", "logs"),
):
    os.environ[env] = os.path.join(TMP, name)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
repl_kit = w.repl_kit

print("== 1. warden 把「一次就检完」的动作标成 coalesce ==")
sync_action = w.NetMixin.net_action(None, "omnifocus.project_sync",
                                    "每日检查 1 个 OmniFocus 项目",
                                    {"projects": [{"id": 1}]},
                                    source=w.SRC_OMNIFOCUS)
check("project_sync 是 coalesce", sync_action.coalesce, True)
one_task = w.NetMixin.net_action(None, "task.complete", "勾完成 #15",
                                 {"task_id": 15}, source=w.SRC_FOCUSMATE)
check("task.complete 不是 coalesce", one_task.coalesce, False)

print("\n== 2. 同一个 coalesce 动作失败 3 次（args 每次不同）→ 只记 1 条 ==")
calls = []


def boom(args, rep):
    calls.append(args)
    raise ValueError("读 OmniFocus 任务失败（退出码 3）")


runner = repl_kit.JobRunner(sync=True, store=STORE)
runner.register("omnifocus.project_sync", boom)
for index in range(3):
    action = repl_kit.Action("omnifocus.project_sync",
                             "每日检查 %d 个 OmniFocus 项目" % (index + 1),
                             {"projects": [{"id": index + 1}]},
                             source=w.SRC_OMNIFOCUS, coalesce=True)
    runner.submit(action)
check("失败账只有一条", len(runner.failures), 1)
check("尝试次数累加", runner.failures[0].attempts, 3)
check("保留的是最新一次的参数", runner.failures[0].action.args, {"projects": [{"id": 3}]})

print("\n== 3. re-action 只重放一次 ==")
calls.clear()
check("重放了 1 条", runner.retry_failures(), 1)
check("handler 只跑了 1 次", len(calls), 1)
check("失败后仍然只有一条账", len(runner.failures), 1)

print("\n== 4. 普通动作（非 coalesce）还是各记各的 ==")
runner2 = repl_kit.JobRunner(sync=True, store=os.path.join(TMP, "store2.json"))
runner2.register("task.complete", boom)
for task_id in (15, 16):
    runner2.submit(repl_kit.Action("task.complete", "勾完成 #%d" % task_id,
                                   {"task_id": task_id}))
check("两条不同的 task.complete 各记一条", len(runner2.failures), 2)

print("\n== 5. coalesce 动作成功一次 → 这一类旧账全清 ==")
ok_runner = repl_kit.JobRunner(sync=True, store=os.path.join(TMP, "store3.json"))
ok_runner.register("omnifocus.project_sync", boom)
ok_runner.submit(repl_kit.Action("omnifocus.project_sync", "每日检查 1 个",
                                 {"projects": [1]}, coalesce=True))
ok_runner.submit(repl_kit.Action("omnifocus.project_sync", "刷新 2 个",
                                 {"projects": [1, 2]}, coalesce=True))
check("先攒成一条", len(ok_runner.failures), 1)
ok_runner.register("omnifocus.project_sync", lambda args, rep: {"ok": True})
ok_runner.submit(repl_kit.Action("omnifocus.project_sync", "每日检查 3 个",
                                 {"projects": [1, 2, 3]}, coalesce=True))
check("成功之后账清空", len(ok_runner.failures), 0)

print("\n== 6. 旧账本（没有 coalesce 字段）启动时合并成一条 ==")
legacy = {"updated_at": 0.0, "failures": [
    {"action": {"name": "omnifocus.project_sync", "label": "每日检查 1 个",
                "args": {"projects": [{"id": i}]}, "source": "omnifocus"},
     "error": "失败 %d" % i, "at": float(i), "attempts": 1}
    for i in range(1, 5)
] + [{"action": {"name": "task.complete", "label": "勾完成 #15",
                 "args": {"task_id": 15}, "source": "focusmate"},
      "error": "浏览器卡住", "at": 9.0, "attempts": 1}]}
legacy_path = os.path.join(TMP, "legacy.json")
with open(legacy_path, "w", encoding="utf-8") as handle:
    json.dump(legacy, handle, ensure_ascii=False)

legacy_runner = repl_kit.JobRunner(sync=True, store=legacy_path)
check("旧账本先读到 5 条", len(legacy_runner.failures), 5)
check("合并掉 3 条重复", legacy_runner.prune_failures(["omnifocus.project_sync"]), 3)
check("合并后剩 2 条", len(legacy_runner.failures), 2)
kept = [item for item in legacy_runner.failures
        if item.action.name == "omnifocus.project_sync"]
check("每日检查只留最新那一条", kept[0].error, "失败 4")
with open(legacy_path, encoding="utf-8") as handle:
    on_disk = json.load(handle)["failures"]
check("合并结果落盘", len(on_disk), 2)

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    raise SystemExit(1)
print("all checks passed")
