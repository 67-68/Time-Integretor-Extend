#!/usr/bin/env python3
"""derived.py 回归测试：note 标题清单 / 资格判定 / 勾选 / 合成 pk / count 范围。

    python3 test/test_derived.py        # 全绿退出 0，有失败退出 1

纯逻辑，不碰 OmniFocus、不碰网络。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))
    return ok


def check_in(label, needle, haystack):
    ok = needle in (haystack or "")
    if not ok:
        fails.append("%s\n      %r 不在 %r" % (label, needle, haystack))
    print("%s %s" % ("ok  " if ok else "FAIL", label))
    return ok


dv = load("derived", "derived.py")

NOTE = """\
做一些准备

# titles
- 2019-亚太-12
- [ ] 2019-亚太-22
- [x] 2019-亚太-32

# 日志
- 这行不该算标题
"""

print("== parse ==")
parsed = dv.parse(NOTE)
check("找到 3 条标题", len(parsed["items"]), 3)
check("1 条已勾选", parsed["open"], 2)
check("标题文本", [x["text"] for x in parsed["items"]],
      ["2019-亚太-12", "2019-亚太-22", "2019-亚太-32"])
check("勾选状态", [x["checked"] for x in parsed["items"]], [False, False, True])
check("行号是 note 里的行", [x["line"] for x in parsed["items"]], [3, 4, 5])
check("下一段标题不算", parsed["total"], 3)
check("没有 titles 段 → 空", dv.parse("# 别的\n- x\n")["items"], [])
check("大小写/空格容忍", len(dv.parse("# Titles\n  - x\n")["items"]), 1)
check("星号 bullet 也认", dv.parse("# titles\n* a\n+ b\n")["total"], 2)
check("空 bullet 不算", dv.parse("# titles\n- \n")["total"], 0)

print("== next_open / mark_checked ==")
check("下一条未勾选", dv.next_open(NOTE)["text"], "2019-亚太-12")
check("全勾完没有下一条", dv.next_open("# titles\n- [x] a\n"), None)
new_note, changed = dv.mark_checked(NOTE, 3)
check("勾上第 3 行", changed, True)
check_in("回写成了 [x]", "- [x] 2019-亚太-12", new_note)
check("重复勾选不变", dv.mark_checked(new_note, 3)[1], False)
new2, changed2 = dv.mark_checked(NOTE, 4)
check("已有 [ ] 的行只换方括号", new2.splitlines()[4], "- [x] 2019-亚太-22")
check("越界行号不动", dv.mark_checked(NOTE, 999)[1], False)
check("不是 bullet 的行不动", dv.mark_checked(NOTE, 0)[1], False)
check("缩进保留", dv.mark_checked("# titles\n   - a\n", 1)[0].splitlines()[1],
      "   - [x] a")
check("大小写 [X] 也算勾过", dv.mark_checked("# titles\n- [X] a\n", 1)[1], False)

print("== 资格判定 ==")
daily = {"rule": "FREQ=DAILY;INTERVAL=1",
         "anchor_date_key": "[object Task.AnchorDateKey: DueDate]"}
defer_daily = dict(daily, anchor_date_key="[object Task.AnchorDateKey: DeferDate]")
weekly = dict(daily, rule="FREQ=WEEKLY;BYDAY=WE")
every16 = dict(daily, rule="FREQ=DAILY;INTERVAL=16")
check("每天 + due 锚 → 合格", dv.eligible(daily, NOTE), True)
check("每天 + defer 锚 → 不合格", dv.eligible(defer_daily, NOTE), False)
check("每周 → 不合格", dv.eligible(weekly, NOTE), False)
check("16 天一次 → 不合格", dv.eligible(every16, NOTE), False)
check("没有 rule → 不合格", dv.eligible({}, NOTE), False)
check("没有 titles → 不合格", dv.eligible(daily, "just a note"), False)
check("全勾完 → 不合格", dv.eligible(daily, "# titles\n- [x] a\n"), False)
check("interval 缺省算每天", dv.is_daily({"rule": "FREQ=DAILY"}), True)
check("RRULE: 前缀也认", dv.is_daily({"rule": "RRULE:FREQ=DAILY"}), True)
check("裸 anchor 也认", dv.is_defer_anchored({"anchor_date_key": "DeferDate"}), True)
check("due 锚不 defer", dv.is_defer_anchored(daily), False)
check("没有 anchor 不 defer", dv.is_defer_anchored({}), False)

print("== 标题 / 合成 pk ==")
check("派生标题", dv.derived_title("完成历史 Papers", "2019-亚太-12"),
      "完成历史 Papers：2019-亚太-12")
check("父标题空", dv.derived_title("", "a"), "a")
check("子标题空", dv.derived_title("P", ""), "P")
pk = dv.synthetic_pk("abc123", 7)
check("合成 pk", pk, "abc123#d7")
check("解码父 pk", dv.decode_pk(pk)[0], "abc123")
check("解码行号", dv.decode_pk(pk)[1], 7)
check("真实 pk 不误判", dv.decode_pk("bXBKXt03Pgc"), (None, None))
check("带 # 但尾巴不是数字不误判", dv.decode_pk("a#db"), (None, None))

print("== count 范围 ==")
check("25 + 5 条 → 25-125", dv.count_span(25, 5), "25-125")
check("1 条 → 单列不变形", dv.count_span(3, 1), "3-3")
check("没有 derived → None", dv.count_span(25, 0), None)
check("没有 count → None", dv.count_span(0, 5), None)
check("脏输入 → None", dv.count_span("x", 5), None)

print("== 派生任务识别 ==")
task = {"source": {"kind": "derived", "line": 3, "pk": "abc123",
                   "text": "2019-亚太-12", "parent_title": "完成历史 Papers"},
        "fields": {"title": "完成历史 Papers：2019-亚太-12"}}
info = dv.derivation(task)
check("识别派生任务", dv.is_derived(task), True)
check("父 pk", info["parent_pk"], "abc123")
check("行号", info["line"], 3)
check("标题原文", info["text"], "2019-亚太-12")
check("普通任务不算", dv.is_derived({"source": {"kind": "project"}}), False)
check("普通任务 derivation 为 None", dv.derivation({"source": {}}), None
      )
fields_only = {"fields": {"derived_text": "a", "derived_line": 1,
                          "derived_parent_pk": "pk"}}
check("只有 fields 也认", dv.is_derived(fields_only), True)
check("只有 fields 能取出信息", dv.derivation(fields_only)["text"], "a")

print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
