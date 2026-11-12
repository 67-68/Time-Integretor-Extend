#!/usr/bin/env python3
"""重复规则「并进全量读取」的回归。

背景：对账/导入以前是「先 list 一遍，再给每条活跃任务单独起一个 osascript 查
repetitionInfo」—— 0.26s × 28 条 ≈ 7 秒纯等待，用户反馈「网络拉取太慢」。
现在 `list --with-repetition` 在**同一次**遍历里把规则带回来，`repetition_info`
从「起一个进程」变成「查一个字典」。

    python3 test/test_omnifocus_repetition_batch.py
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


ot = load("omnifocus_tasks")
of = load("omnifocus")

print("== 1. 只有 --with-repetition 才把重复字段塞进 JS ==")
plain = ot.build_script("omnijs")
with_rep = ot.build_script("omnijs", with_repetition=True)
check("默认脚本不认 repInfo", "repInfo" in plain, False)
check("默认脚本没有占位符", "{{REPETITION" in plain, False)
check("with_repetition 里有 repInfo", "repInfo" in with_rep, True)
check("with_repetition 里有字段", "repetition_rule" in with_rep, True)
check("OMNIJS_SOURCE 是成品（不含占位符）", "{{REPETITION" in ot.OMNIJS_SOURCE, False)

print("\n== 2. 解析不丢掉顺手带回来的字段 ==")
envelope = {
    "tool": "omnifocus_tasks", "version": 1,
    "rows": [{
        "id": "t1", "name": "读一章", "kind": "action", "project": "读书",
        "project_id": "p1", "task_type": "", "parent": "读书", "parent_id": "p1",
        "in_inbox": False, "tags": [], "tag_ids": [], "flagged": False,
        "completed": False, "dropped": False, "effective_completed": False,
        "effective_dropped": False, "due": None, "defer": None, "planned": None,
        "completed_at": None, "created": None, "modified": None,
        "estimated_minutes": 50, "next": False, "blocked": False, "children": 0,
        "note": "",
        "repetition_rule": "FREQ=WEEKLY", "repetition_method": "Fixed",
        "repetition_schedule_type": "Regularly",
        "repetition_anchor_date_key": "DeferDate",
        "repetition_catch_up": False, "repetition_next": "2026-11-19T17:00:00",
    }],
}
text = json.dumps(envelope, ensure_ascii=False)
rows_plain = ot.parse_json_rows(text)
rows_extra = ot.parse_json_rows(text, extra=ot.REPETITION_FIELDS)
check("不带 extra 时字段被丢掉", "repetition_rule" in rows_plain[0], False)
check("带 extra 时规则在", rows_extra[0]["repetition_rule"], "FREQ=WEEKLY")
check("带 extra 时 next 在", rows_extra[0]["repetition_next"], "2026-11-19T17:00:00")
check("带 extra 时 bool 是 bool", rows_extra[0]["repetition_catch_up"], False)
check("普通字段没受影响", rows_extra[0]["estimated_minutes"], 50)

print("\n== 3. Client：缓存里有规则就不要起 osascript ==")
client = of.Client()
row = dict(rows_extra[0], id="t1")
client._cache = [row]
client._cache_at = client.clock()
client._cache_repetition = True


def _boom(*args, **kwargs):
    raise AssertionError("缓存命中时不该再起 osascript")


client._run_applescript = _boom
info = client.repetition_info("t1")
check("repeated", info["repeated"], True)
check("rule_string", info["rule_string"], "FREQ=WEEKLY")
check("next", info["next"], "2026-11-19T17:00:00")
check("duration 从行里带过来", info["estimated_minutes"], 50)
check("due 从行里带过来", info["due"], None)

print("\n== 4. 缓存没带重复字段 → 退回单条查询（不假装知道） ==")
client2 = of.Client()
client2._cache = [dict(rows_plain[0], id="t2")]
client2._cache_at = client2.clock()
client2._cache_repetition = False
called = []


def _fake_applescript(script, timeout=None):
    called.append(1)
    return json.dumps({
        "tool": "omnifocus_repetition", "ok": True, "repeated": False,
        "id": "t2", "name": "读一章",
    })


client2._run_applescript = _fake_applescript
info2 = client2.repetition_info("t2")
check("退回了单条查询", len(called), 1)
check("repeated=False", info2["repeated"], False)
# client2 是独立实例，别让它挡住上面第 3 段用的 client
check("两个实例互不影响", client._cache_repetition, True)

print()
if fails:
    print("FAILED %d" % len(fails))
    for item in fails:
        print(" - %s" % item)
    sys.exit(1)
print("all checks passed")
