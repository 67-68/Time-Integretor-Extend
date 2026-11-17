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


def read_lines(path):
    return open(path, encoding="utf-8").read().splitlines()


# ---- 标准 logging：级别过滤 ---------------------------------------------
with tempfile.TemporaryDirectory() as d2:
    when = datetime.datetime(2026, 11, 17, 10, 0, 0)
    day_file = os.path.join(d2, "2026-11-17.jsonl")
    rl.setup(level="info", path=d2)

    p = rl.log_event("focusmate", "book_start", now=when, level="debug", n=1)
    check("INFO 下丢弃 DEBUG", p, None)
    check("丢弃时连文件都不建", os.path.exists(day_file), False)

    rl.set_level("debug")
    p = rl.log_event("focusmate", "book_start", now=when, level="debug", n=1)
    check("DEBUG 开关打开后落盘", os.path.basename(p or ""), "2026-11-17.jsonl")
    rec = json.loads(read_lines(day_file)[-1])
    check("level 字段名", rec["level"], "DEBUG")
    check("level 字段以外的老字段不变", (rec["area"], rec["event"]), ("focusmate", "book_start"))

    rl.log_event("focusmate", "book_done", now=when, ok=True)
    rec = json.loads(read_lines(day_file)[-1])
    check("默认级别是 INFO", rec["level"], "INFO")

    rl.set_level("warning")
    check("WARNING 下丢弃 INFO", rl.log_event("focusmate", "book_done", now=when), None)
    check("WARNING 下 ERROR 照写",
          os.path.basename(rl.log_event("focusmate", "book_error", now=when,
                                        level="error", err="x") or ""),
          "2026-11-17.jsonl")
    rec = json.loads(read_lines(day_file)[-1])
    check("ERROR 名字", rec["level"], "ERROR")

# ---- log_change：只在变化时写 -------------------------------------------
with tempfile.TemporaryDirectory() as d3:
    when = datetime.datetime(2026, 11, 17, 12, 0, 0)
    day_file = os.path.join(d3, "2026-11-17.jsonl")
    rl.setup(level="info", path=d3)
    rl.reset_change_state()

    r1 = rl.log_change("focusmate", "task_pull_result", now=when, matched=2, archived=0)
    r2 = rl.log_change("focusmate", "task_pull_result", now=when, matched=2, archived=0)
    r3 = rl.log_change("focusmate", "task_pull_result", now=when, matched=2, archived=1)
    n = len(read_lines(day_file))
    check("第一次写、第二次跳过、第三次又写", (bool(r1), r2, bool(r3)), (True, None, True))
    check("一共两行", n, 2)
    rec = json.loads(read_lines(day_file)[-1])
    check("变化行带 changed", rec["changed"], True)
    check("变化行记下中间重复了几次", rec["skipped_same"], 1)
    check("内容照原样", (rec["matched"], rec["archived"]), (2, 1))

    # 不同的 key 各自记账：同一个 event、同样的内容也不会互相吞掉
    rl.reset_change_state()
    before = len(read_lines(day_file))
    rl.log_change("focusmate", "task_pull_result", key="a", now=when, matched=1)
    rl.log_change("focusmate", "task_pull_result", key="b", now=when, matched=1)
    check("不同 key 各写一条", len(read_lines(day_file)) - before, 2)

    # 心跳：内容一直不变也会隔一段时间写一条
    rl.reset_change_state()
    t0 = datetime.datetime(2026, 11, 17, 13, 0, 0)
    before = len(read_lines(day_file))
    h1 = rl.log_change("omnifocus", "status_pull_result", now=t0, heartbeat=60, rows=5)
    h2 = rl.log_change("omnifocus", "status_pull_result",
                       now=t0 + datetime.timedelta(seconds=30), heartbeat=60, rows=5)
    h3 = rl.log_change("omnifocus", "status_pull_result",
                       now=t0 + datetime.timedelta(seconds=61), heartbeat=60, rows=5)
    check("心跳前不写", h2, None)
    check("心跳到点写一条", bool(h3), True)
    check("心跳一共两行（首次 + 心跳）", len(read_lines(day_file)) - before, 2)
    rec = json.loads(read_lines(day_file)[-1])
    check("心跳行有 heartbeat 标记", rec.get("heartbeat"), True)

    # 敏感字段在 log_change 里同样被隐藏
    rl.reset_change_state()
    rl.log_change("focusmate", "auth", now=when, token="secret", note="n")
    rec = json.loads(read_lines(day_file)[-1])
    check("log_change 也隐去 token", rec["token"], "（已隐藏）")

# ---- 保留期清理 ----------------------------------------------------------
with tempfile.TemporaryDirectory() as d4:
    for name in ("2026-10-01", "2026-11-07", "2026-11-08", "2026-11-16",
                 "2031-01-01"):
        open(os.path.join(d4, name + ".jsonl"), "w", encoding="utf-8").write("{}\n")
    open(os.path.join(d4, "notes.txt"), "w", encoding="utf-8").write("x")
    open(os.path.join(d4, "2026-13-99.jsonl"), "w", encoding="utf-8").write("x")

    when = datetime.datetime(2026, 11, 17, 12, 0)
    removed = rl.prune(days=3, path=d4, now=when)
    check("清理掉 3 天前的日文件", sorted(removed),
          ["2026-10-01.jsonl", "2026-11-07.jsonl", "2026-11-08.jsonl"])
    check("保留最近的 + 不碰非日文件 / 解析不了的文件", sorted(os.listdir(d4)),
          ["2026-11-16.jsonl", "2026-13-99.jsonl", "2031-01-01.jsonl",
           "notes.txt"])
    check("days<=0 = 不清理", rl.prune(days=0, path=d4, now=when), [])
    check("目录不存在不炸", rl.prune(path="/dev/null/nope", now=when), [])

os.environ["TI_LOG_KEEP_DAYS"] = "1"
check("env 能改保留天数", rl.keep_days(), 1)
os.environ["TI_LOG_KEEP_DAYS"] = "不是数字"
check("env 不是数字退回默认", rl.keep_days(), rl.DEFAULT_KEEP_DAYS)
os.environ.pop("TI_LOG_KEEP_DAYS", None)
check("默认保留 14 天", rl.keep_days(), 14)

print("\n%d passed, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
