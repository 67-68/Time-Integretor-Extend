#!/usr/bin/env python3
"""生成 `fixtures/edge-cases/`：**历史失败特征的全集**（用户 2026-11-16 要求）。

一个场景里同时塞进这些特征，断言按「每个失败特征一条」组织：

| # | 特征 | 对应的历史失败 |
|---|---|---|
| 1 | T1 整段已过去（09:00–12:30） | plan 把过掉的时段算成容量、给过掉的 session 排任务 |
| 2 | 14:00(75) 已结束 + 挂着一条已完成任务 | 同上 |
| 3 | 15:15(50) 正在进行、没订上 | 「进行中」被当资源；也当过去 |
| 4 | 16:15(75) 未订上 | 计划有、API 没有 → 仍应计入空场（并给提示） |
| 5 | 17:30(25) 落在 16:15 那场的 15 分钟休息里 | 「Failed to select time slot 17:30」（物理订不上） |
| 6 | 19:00(75) 已订上 + **两条独占任务**挤同一场 | 独占冲突；已订场次不许重切 |
| 7 | 22:15–23:15 整段 60min 未切 session | 容量算了但提交成 `durations: []` |
| 8 | 23:15–00:05 **跨午夜**的已订场次 | 「23:15 开始的那场算今天」 |
| 9 | 清单容器 `@list` 7 条、每条 3 count | 「English 6 count 仅排了 3」 |
| 10 | 重复任务本体 + occurrence 子任务 | 本体不占 session；副本不能勾父 note |
| 11 | 「唤起了但没挂上」的残留任务（无 session） | plan 要么复用，要么重复唤起 |
| 12 | defer 到今晚 23:00 的任务 | 未到点的任务不许排 |
| 13 | 10 分钟小任务 | <25 分钟必须叠进别人的大场 |
| 14 | 计划 16:15 / 实际 16:00（差 15 分钟） | 「计划 vs 实际」错位，plan 不许自作主张“修正” |

容量口径（15:58 敲 plan）：16:15(3) + 17:30(1) + 22:15(2) = **6 count**。
19:00 与 23:15 都挂着任务 → 不算空场；T1/14:00/15:15 已过去或进行中 → 不算。

跑法：`python3 -B test/live/make_edge_fixture.py`（写盘后由 test_live_edge_cases.py 读）。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "edge-cases")
DAY = "{{DAY}}"
CLOCK = "15:58"

PERIODS = [
    ("T1", "09:00", "12:30"),      # 1) 整段已过去
    ("T2", "14:00", "18:00"),      # 2)3)4)5) 部分过去 + 进行中 + 未订 + 休息冲突
    ("T3", "19:00", "21:30"),      # 6) 已订上 + 独占冲突
    ("T4", "22:15", "23:15"),      # 7) 整段 60min 未切
]


def write(name, obj):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def calendar():
    return {
        "version": 1,
        "generated_at": "%sT00:00:00" % DAY,
        "start_date": DAY,
        "days": 1,
        "profiles": ["空闲"],
        "events": [],
        "periods": [
            {"id": pid, "date": DAY, "start": s, "end": e, "type": "空闲",
             "name": "空闲", "calendar": "空闲", "title": "空闲",
             "start_iso": "%sT%s:00" % (DAY, s), "end_iso": "%sT%s:00" % (DAY, e)}
            for pid, s, e in PERIODS
        ],
    }


def tasks():
    """任务库 zoo：每条都对应一个历史失败特征。"""

    def task(tid, title, **kw):
        base = {"id": tid, "fields": {"title": title}, "status": "active",
                "tags": [], "repeat": None, "duplicate_of": None,
                "history": [], "created_at": "%sT00:00:00" % DAY,
                "updated_at": "%sT00:00:00" % DAY, "session": {},
                "actual_minutes": None, "file": None}
        fields = kw.pop("fields", {})
        base.update(kw)
        base["fields"].update(fields)
        if not base.get("session"):
            base["session"] = {}
        return base

    out = [
        # 6) 19:00 那场挤了两条独占任务：check_exclusive_sessions 必须拆掉
        task(1, "flashcard review & new learn",
             fields={"exclusive": True, "count": 1, "duration": 25},
             session={"index": 6, "date": DAY, "start": "19:00", "end": "20:15",
                      "duration": 75, "focusmate_session_id": "fm-1900"}),
        task(2, "flashcard review 副本",
             fields={"exclusive": True, "count": 1, "duration": 25},
             session={"index": 6, "date": DAY, "start": "19:00", "end": "20:15",
                      "duration": 75, "focusmate_session_id": "fm-1900"}),
        # 13) 10 分钟小任务：必须叠进别人的大场
        task(3, "errand 十分钟小活",
             fields={"count": 1, "duration": 10}),
        # 12) defer 到今晚 23:00：不得到点前排
        task(4, "晚上才允许动的任务",
             fields={"count": 3, "duration": 75, "defer": "%sT23:00:00" % DAY}),
        # 10) 重复任务本体（不占 session）+ 今天的 occurrence 子任务
        task(5, "每日闪卡（本体）",
             fields={"count": 1, "duration": 25},
             repeat={"rule": "FREQ=DAILY", "next": "%sT00:00:00" % DAY}),
        task(6, "每日闪卡（今天的子任务）",
             fields={"count": 1, "duration": 25}, repeat_parent=5),
        # 11) 「唤起了但没挂上」的残留任务（上轮 plan 建的，没有 session）
        task(7, "Lit: Complete 1 Scene：scene 2",
             fields={"count": 3, "duration": 75, "project_id": 1,
                     "project_task": "edge_list#d2"},
             source={"kind": "derived", "line": 2, "text": "scene 2",
                     "pk": "edge_list", "parent_title": "Lit: Complete 1 Scene"}),
        # 2) 已结束的场次上挂着一条已完成任务
        task(8, "早上已经做完的那条",
             status="completed",
             fields={"count": 1, "duration": 75},
             session={"index": 3, "date": DAY, "start": "14:00", "end": "15:15",
                      "duration": 75, "focusmate_session_id": "fm-1400"}),
        # 8) 跨午夜：23:15 开始、次日 00:05 结束（已排过的派生行也挂在这）
        task(9, "跨午夜的那场任务",
             fields={"count": 2, "duration": 50},
             session={"index": 7, "date": DAY, "start": "23:15", "end": "00:05",
                      "duration": 50, "focusmate_session_id": "fm-2315"}),
        task(10, "Lit: Complete 1 Scene：scene 1",
             fields={"count": 2, "duration": 50, "project_id": 1,
                     "project_task": "edge_list#d1"},
             source={"kind": "derived", "line": 1, "text": "scene 1",
                     "pk": "edge_list", "parent_title": "Lit: Complete 1 Scene"},
             session={"index": 7, "date": DAY, "start": "23:15", "end": "00:05",
                      "duration": 50, "focusmate_session_id": "fm-2315"}),
    ]
    return {"version": 1, "next_id": 100, "tasks": out}


def projects():
    """项目库：清单容器 / sequential / 50 分钟大任务。"""
    note = "@list 场景清单\n" + "\n".join(
        "- scene %d" % i for i in range(1, 8))   # 9) 7 条未勾

    def op(oid, title, task_type, pro_id, tasks_):
        return {"id": oid, "title": title, "op_type": "SOP", "task_type": task_type,
                "pro_id": pro_id, "project_tag": "", "tags": [], "file": None,
                "omnifocus": None, "status": "active", "status_at": None,
                "status_reason": None, "imported_at": "%sT00:00:00" % DAY,
                "synced_at": "%sT00:00:00" % DAY, "plans": [], "tasks": tasks_}

    def item(pk, title, count, minutes, rank, defer=None, note=""):
        return {"pk": pk, "title": title, "parent": None, "parent_id": None,
                "due": None, "defer": defer, "planned": None, "note": note,
                "count": count, "duration": minutes, "completed": False,
                "dropped": False, "effective_completed": False,
                "effective_dropped": False, "blocked": False,
                "local_only": False, "rank": rank}

    def pro(pid, title, op_id):
        return {"id": pid, "title": title, "stage": "running", "file": None,
                "sop_op_id": op_id, "incubator_op_id": None, "op_ids": [op_id],
                "status": "active", "status_at": None, "status_reason": None,
                "stage_locked": False, "created_at": "%sT00:00:00" % DAY,
                "updated_at": "%sT00:00:00" % DAY, "soft_interrupts": []}

    return {
        "version": 1, "next_id": 10, "next_pro_id": 10,
        "pros": [pro(1, "沙盒 Lit", 1), pro(2, "沙盒 顺序", 2), pro(3, "沙盒 大任务", 3)],
        "projects": [
            op(1, "Lit SOP", "parallel", 1, [
                item("edge_list", "Lit: Complete 1 Scene", 3, 75, 0, note=note),
                item("edge_small", "errand 十分钟小活", 1, 10, 1),
                item("edge_defer", "晚上才允许动的任务", 3, 75, 2,
                     defer="%sT23:00:00" % DAY),
            ]),
            op(2, "顺序 SOP", "sequential", 2, [
                item("edge_seq1", "顺序第一步", 1, 25, 0),
                item("edge_seq2", "顺序第二步", 1, 25, 1),
            ]),
            op(3, "大任务 SOP", "parallel", 3, [
                item("edge_big50", "大任务 50 分钟", 2, 50, 0),
            ]),
        ],
    }


def main():
    write("calendar-download.json", calendar())
    write("tasks.json", tasks())
    write("projects.json", projects())
    write("session-layouts.json", {"version": 1, "layouts": {
        DAY: {"T1": {"durations": [], "source": "auto",
                     "updated_at": "%sT10:00:00" % DAY},
              "T2": {"durations": [75, 50, 75, 25], "source": "manual",
                     "updated_at": "%sT11:44:00" % DAY},
              "T3": {"durations": [75], "source": "auto",
                     "updated_at": "%sT15:53:00" % DAY}}}})
    # 23:15 那行挂在「时段之外」：只能由 extras 提供，否则重算 data.csv 会把它冲掉
    write("session-extras.json", {"version": 1, "dates": {
        DAY: [{"start": "23:15", "duration": 50,
               "note": "跨午夜：23:15 开始、次日 00:05 结束（算今天）"}]}})
    write("session-rules.json", {"version": 1, "rules": []})
    write("session-mutes.json", {"version": 1, "mutes": []})
    with open(os.path.join(OUT, "data.csv"), "w", encoding="utf-8") as f:
        f.write("# edge-cases：历史失败特征全集（make_edge_fixture.py 生成）\n")
        f.write("# 14:00 已结束 / 15:15 进行中 / 16:15 未订 / 17:30 落在 16:15 的休息里\n")
        f.write("# 19:00 已订上（两条独占挤同一场）/ 22:15–23:15 整段未切（没有行）\n")
        f.write("# 23:15 跨午夜（23:15–次日 00:05），已订上并挂着任务\n")
        for start, dur in (("14:00", 75), ("15:15", 50), ("16:15", 75),
                           ("17:30", 25), ("19:00", 75), ("23:15", 50)):
            f.write("%s,%s,%d\n" % (DAY, start, dur))
    write("meta.json", {
        "name": "edge-cases",
        "why": "历史失败特征全集：见 make_edge_fixture.py 顶部那张表（14 条特征）。",
        "clock": CLOCK,
        "booked": [["10:00", 50], ["11:15", 50], ["14:00", 75], ["16:00", 75],
                   ["17:30", 25], ["19:00", 75], ["23:15", 50]],
        "alloc": [[1, 6], [2, 2]],
    })
    print("wrote", OUT)
    for name in sorted(os.listdir(OUT)):
        print("  ", name, os.path.getsize(os.path.join(OUT, name)))


if __name__ == "__main__":
    main()
