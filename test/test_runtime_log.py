#!/usr/bin/env python3
"""runtime_log.py 回归：按天分文件、JSONL、敏感字段隐藏、绝不抛异常。"""
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import runtime_log as rl

ok = 0
fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("ok   %s" % name)
    else:
        fail += 1
        print("FAIL %s\n  got : %r\n  want: %r" % (name, got, want))


with tempfile.TemporaryDirectory() as d:
    when = datetime.datetime(2026, 10, 30, 23, 59, 1, 123000)
    path = rl.log_event("focusmate", "tasks_pull_done", now=when, path=d,
                        count=2, tasks=[{"id": "a", "title": "写论文"}])
    check("按本地日期分文件", os.path.basename(path), "2026-10-30.jsonl")
    rec = json.loads(open(path, encoding="utf-8").read())
    check("area", rec["area"], "focusmate")
    check("event", rec["event"], "tasks_pull_done")
    check("普通字段原样落盘", rec["tasks"][0]["title"], "写论文")
    check("毫秒时间戳", rec["ts"], "2026-10-30T23:59:01.123")

    rl.log_event("omnifocus", "complete_result", now=when, path=d,
                 token="secret", nested={"accessToken": "secret2", "pk": "abc"})
    lines = open(path, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[-1])
    check("每天追加、不覆盖", len(lines), 2)
    check("token 字段隐藏", rec["token"], "（已隐藏）")
    check("嵌套 token 字段隐藏", rec["nested"]["accessToken"], "（已隐藏）")
    check("非敏感字段保留", rec["nested"]["pk"], "abc")

# 不可写路径只返回 None，不抛异常
check("写失败不炸", rl.log_event("x", "y", path="/dev/null/nope"), None)

print("\n%d passed, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
