#!/usr/bin/env python3
"""OmniFocus 增量读取的回归（不碰真 OmniFocus：`_run` 全用假的）。

规则：
- 第一次读 = 全量；
- 距上次读不超过 INCREMENTAL_WINDOW（默认 600s）→ 先问 changes，只补读变了的行；
- 变化太多 / 有项目变化 / changes 失败 / 超过窗口 → 全量（删除只有全量能发现）；
- 增量补读必须保住快照里的重复规则字段。

    python3 test/test_omnifocus_incremental.py
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


of = load("omnifocus")

ROWS = [
    {"id": "t1", "name": "读一章", "kind": "action", "completed": False},
    {"id": "t2", "name": "写笔记", "kind": "action", "completed": False},
    {"id": "t3", "name": "项目 P", "kind": "project_root", "completed": False},
]


class FakeClock(object):
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeClient(of.Client):
    """把子进程换成记账本：只测调度/合并逻辑。"""

    def __init__(self, rows=None, changes=None, clock=None, fail_changes=False):
        super(FakeClient, self).__init__()
        self.enabled = True
        self.available = lambda: True          # noqa: E731 - 测试里不查真 osascript
        self.rows = [dict(r) for r in (rows or ROWS)]
        self.changes = list(changes or [])
        self.fail_changes = fail_changes
        self.calls = []
        self.fake_clock = clock or FakeClock()
        self.clock = self.fake_clock
        self.wall_clock = lambda: 1794000000.0 + self.fake_clock.now

    def _run(self, args, timeout=None, expect_jsonl=False):
        self.calls.append(list(args))
        if args[0] == "changes":
            if self.fail_changes:
                return 1, "", "changes boom"
            out = "".join(json.dumps({"kind": "task", "id": pk, "modified": None}) + "\n"
                          for pk in self.changes)
            return 0, out, ""
        ids = None
        with_rep = "--with-repetition" in args
        if "--ids" in args:
            ids = json.loads(args[args.index("--ids") + 1])
        picked = [r for r in self.rows if ids is None or r["id"] in ids]
        out = []
        for row in picked:
            item = dict(row)
            if with_rep:
                item["repetition_rule"] = None
            out.append(json.dumps(item, ensure_ascii=False) + "\n")
        return 0, "".join(out), ""

    def list_calls(self):
        return [c for c in self.calls if c[0] == "list"]

    def change_calls(self):
        return [c for c in self.calls if c[0] == "changes"]


print("== 1. 第一次读是全量 ==")
c = FakeClient()
rows = c.list_tasks(refresh=True)
check("行数", len(rows), 3)
check("只跑了一次 list", len(c.list_calls()), 1)
check("list 是全量（没有 --ids）", "--ids" in c.list_calls()[0], False)
check("没有问 changes", len(c.change_calls()), 0)

print("\n== 2. 窗外（600s 内）再读：增量补读变化的行 ==")
c.calls = []
c.fake_clock.advance(120)
c.rows[1] = {"id": "t2", "name": "写笔记（改过）", "kind": "action", "completed": True}
c.changes = ["t2"]
rows = c.list_tasks(refresh=True)
check("问了一次 changes", len(c.change_calls()), 1)
check("不走全量", "--ids" in c.list_calls()[0], True)
check("补读的就是 t2", json.loads(c.list_calls()[0][c.list_calls()[0].index("--ids") + 1]),
      ["t2"])
check("改过的行合并进来了", rows[1]["name"], "写笔记（改过）")
check("没变的行还在", [r["name"] for r in rows if r["id"] == "t1"], ["读一章"])

print("\n== 3. 没有变化：只问一句，不读全量 ==")
c.calls = []
c.changes = []
c.list_tasks(refresh=True)
check("questions only changes", [call[0] for call in c.calls], ["changes"])

print("\n== 4. 变化太多 → 全量 ==")
c.calls = []
c.changes = ["x%d" % i for i in range(of.INCREMENTAL_MAX + 1)]
c.list_tasks(refresh=True)
check("退化成全量", "--ids" in c.list_calls()[0], False)

print("\n== 5. 有项目变化 → 全量（项目改名没法靠补读任务行反映）==")
class ProjectClient(FakeClient):
    def _run(self, args, timeout=None, expect_jsonl=False):
        if args[0] == "changes":
            self.calls.append(list(args))
            return 0, json.dumps({"kind": "project", "id": "p1", "modified": None}) + "\n", ""
        return FakeClient._run(self, args, timeout=timeout, expect_jsonl=expect_jsonl)

pc = ProjectClient()
pc.list_tasks(refresh=True)
pc.calls = []
pc.list_tasks(refresh=True)
check("退化成全量", "--ids" in pc.list_calls()[0], False)

print("\n== 6. changes 失败 → 全量（不拿半截结果冒充没变化）==")
fc = FakeClient(fail_changes=True)
fc.list_tasks(refresh=True)
fc.calls = []
fc.list_tasks(refresh=True)
check("退化成全量", "--ids" in fc.list_calls()[0], False)

print("\n== 7. 超过增量窗口 → 全量（定期全量对账，删除只能这样发现）==")
wc = FakeClient()
wc.list_tasks(refresh=True)
wc.calls = []
wc.fake_clock.advance(of.INCREMENTAL_WINDOW + 5)
wc.list_tasks(refresh=True)
check("没去问 changes", len(wc.change_calls()), 0)
check("直接全量", "--ids" in wc.list_calls()[0], False)

print("\n== 8. invalidate 之后仍可增量（快照留着）==")
ic = FakeClient()
ic.list_tasks(refresh=True)
ic.invalidate()
ic.calls = []
ic.changes = ["t1"]
ic.list_tasks(refresh=True)
check("走增量", "--ids" in ic.list_calls()[0], True)

print("\n== 9. 快照带重复字段时，补读也必须带（否则合并后一半有一半没有）==")
rc = FakeClient()
rc.list_tasks(refresh=True, with_repetition=True)
rc.calls = []
rc.changes = ["t2"]
rows = rc.list_tasks(refresh=True, with_repetition=True)
check("补读带了 --with-repetition", "--with-repetition" in rc.list_calls()[0], True)
check("合并后的行带 repetition_rule", "repetition_rule" in rows[1], True)

print("\n== 10. 增量丢字段？把全量结果和增量结果按 id 比一遍 ==")
a = FakeClient()
full = a.list_tasks(refresh=True)
a.rows[0] = dict(a.rows[0], name="读一章（改）")
a.changes = ["t1"]
inc = a.list_tasks(refresh=True)
by_full = {r["id"]: r for r in full}
by_inc = {r["id"]: r for r in inc}
check("id 集合一致", set(by_full), set(by_inc))
check("只有 t1 变了", {k for k in by_full if by_full[k] != by_inc[k]}, {"t1"})

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    sys.exit(1)
print("all checks passed")
