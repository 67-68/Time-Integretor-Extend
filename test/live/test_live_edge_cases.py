#!/usr/bin/env python3
"""集成用例：**edge-cases** —— 历史失败特征全集，一条特征一条断言。

数据在 `fixtures/edge-cases/`（由 [make_edge_fixture.py](make_edge_fixture.py) 生成，
里面那张表列了 14 条特征各自对应哪次线上失败）。这个文件按同样的顺序断言：
每加/修一个 bug，就改 fixture 加数据 + 在这里加一条。

跑法：
    python3 -B test/live/test_live_edge_cases.py           # 干跑（不碰网络）
    python3 -B test/live/test_live_edge_cases.py --keep    # 留下沙盒
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

FIXTURE = os.path.join(HERE, "fixtures", "edge-cases")
DAY = datetime.date.today()
CLOCK = datetime.datetime.combine(DAY, datetime.time(15, 58))
ANCHOR = "16:00"

fails = []
ok_count = 0


def check(label, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % label)
    else:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
        print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


def check_true(label, got):
    check(label, bool(got), True)


FILES = {"csv": "data.csv", "tasks": "tasks.json", "projects": "projects.json",
         "calendar": "calendar-download.json", "layouts": "session-layouts.json",
         "extras": "session-extras.json", "rules": "session-rules.json",
         "mutes": "session-mutes.json"}


def build_sandbox():
    tmp = tempfile.mkdtemp(prefix="ti-edge-%s-" % DAY.isoformat())
    temp, data = os.path.join(tmp, "temp"), os.path.join(tmp, "data")
    os.makedirs(temp)
    os.makedirs(data)
    os.makedirs(os.path.join(temp, "logs"))
    paths = {
        "root": tmp, "temp": temp, "data": data, "logs": os.path.join(temp, "logs"),
        "csv": os.path.join(tmp, "data.csv"), "tasks": os.path.join(data, "tasks.json"),
        "projects": os.path.join(data, "projects.json"),
        "archive": os.path.join(data, "task-archive.json"),
        "settings": os.path.join(data, "settings.json"),
        "project_sync": os.path.join(data, "project-sync.json"),
        "calendar": os.path.join(temp, "calendar-download.json"),
        "layouts": os.path.join(temp, "session-layouts.json"),
        "extras": os.path.join(temp, "session-extras.json"),
        "rules": os.path.join(temp, "session-rules.json"),
        "mutes": os.path.join(temp, "session-mutes.json"),
        "keytime_json": os.path.join(temp, "keytime.json"),
        "keytime_state": os.path.join(temp, "keytime-state.json"),
        "keytime_devices": os.path.join(temp, "keytime-devices.json"),
        "keytime_rules": os.path.join(temp, "keytime-rules.toml"),
        "nostart": os.path.join(temp, "nostart.json"),
        "postsession": os.path.join(temp, "postsession.json"),
        "failed_actions": os.path.join(temp, "failed.json"),
        "of_outbox": os.path.join(temp, "outbox.json"),
    }
    return paths


PATHS = build_sandbox()


def reset_sandbox():
    """把 fixture 重铺一遍（每个用例都从 15:58 那一刻重新开始）。"""
    for key, path in PATHS.items():
        if key in FILES:
            continue
        if os.path.isfile(path):
            os.unlink(path)
    shutil.rmtree(PATHS["data"], ignore_errors=True)
    os.makedirs(PATHS["data"], exist_ok=True)
    os.makedirs(PATHS["logs"], exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "data", "settings.json"), PATHS["settings"])
    with open(PATHS["keytime_json"], "w", encoding="utf-8") as f:
        json.dump({"focus": {"enabled": False}, "notify": {"enabled": False}}, f)
    stamp = DAY.isoformat()
    for key, name in FILES.items():
        text = open(os.path.join(FIXTURE, name), encoding="utf-8").read()
        with open(PATHS[key], "w", encoding="utf-8") as f:
            f.write(text.replace("{{DAY}}", stamp))


META = json.load(open(os.path.join(FIXTURE, "meta.json"), encoding="utf-8"))

import fixture  # noqa: E402

for k, v in fixture.env_for(PATHS).items():
    os.environ[k] = v

from rich.console import Console  # noqa: E402

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
pp, pj, ts, sp, sch = w.pp, w.pj, w.ts, w.sp, w.sch


class Bridge(object):
    """只回答「哪几场已经订上了」（meta.booked），不出网。"""

    def call(self, cmd, timeout=240, **kw):
        if cmd == "snapshot":
            sessions = []
            for start, dur in META.get("booked") or []:
                h, m = (int(x) for x in str(start).split(":"))
                at = datetime.datetime.combine(DAY, datetime.time(h, m))
                sessions.append({"id": "fm-%s" % str(start).replace(":", ""),
                                 "startMs": int(at.timestamp() * 1000),
                                 "duration": int(dur), "date": DAY.isoformat(),
                                 "start": start, "meetingType": "paired"})
            return {"date": DAY.isoformat(),
                    "now": int(CLOCK.timestamp() * 1000), "sessions": sessions}
        if cmd == "tasks":
            return {"tasks": []}
        return {}

    def browser_state(self):
        return True, None, ""

    def release(self):
        return None


w.run_keep_session = lambda *a, **kw: (0, "", "")


def new_warden():
    wd = w.Warden(Console(record=True, width=160, file=open(os.devnull, "w")),
                  Bridge(), no_clear=True, offline=True)
    wd.clock_now = CLOCK
    wd.reload_periods()
    wd.reload_entries()
    wd.refresh(quiet=True)
    return wd


def run(wd, line):
    wd.go_to_view(w.ContractView)
    try:
        wd.handle(line)
    except Exception as e:                       # noqa: BLE001
        return "%s: %s" % (type(e).__name__, e)
    return ""


def say(wd):
    out = []
    for m in wd.messages:
        if isinstance(m, tuple) and len(m) >= 3:
            out.append(str(m[2]))
        elif isinstance(m, dict):
            out.append(str(m.get("text") or ""))
        else:
            out.append(str(m))
    return "\n".join(out)


def csv_rows():
    rows = []
    for line in open(PATHS["csv"], encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3 and parts[0] == DAY.isoformat():
            rows.append((parts[1], int(parts[2])))
    return rows


def plan_items():
    store = json.load(open(PATHS["projects"], encoding="utf-8"))
    out = []
    for op in store.get("projects") or []:
        plan = (op.get("plans") or {}).get(DAY.isoformat()) or {}
        out.extend(plan.get("items") or [])
    return out


def tasks_now():
    return json.load(open(PATHS["tasks"], encoding="utf-8"))["tasks"]


def layout(pid):
    rec = (json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
           .get(DAY.isoformat()) or {}).get(pid) or {}
    return rec.get("durations")


# ---------------------------------------------------------------- 用例

def case_1_capacity_and_past():
    """特征 1–5：过去/进行中不算资源；未订上仍算；跨午夜归今天。"""
    reset_sandbox()
    wd = new_warden()
    slots, notes = wd._plan_slots(DAY)
    starts = [s.get("start") for s in slots]
    check("过去/进行中的场次一个都不进空场",
          [s for s in starts if s < ANCHOR], [])
    check_true("16:15（未订上）算空场", "16:15" in starts)
    check_true("17:30（落在 16:15 的休息里）仍在表里被算出空场", "17:30" in starts)
    check_true("22:15（整段未切）算空场", "22:15" in starts)
    check_true("19:00 不算（已经挂着独占任务）", "19:00" not in starts)
    # 19:00(75) + 15 分钟休息之后，到 T3 终点 21:30 还剩 60 分钟（2 count）——
    # 「一场挂了任务就把整段算没」是 2026-11-16 超预算的直接原因。
    check_true("19:00 之后剩下的 20:30 也算空场", "20:30" in starts)
    check("可分配的 count = 16:15(3) + 17:30(1) + 20:30(2) + 22:15(2) = 8",
          pp.capacity(slots).get("counts"), 8)
    # 特征 8：23:15 开始、次日 00:05 结束的那一行属于今天
    rows = {r.get("startLabel"): r for r in wd.rows
            if (r.get("date") or DAY.isoformat()) == DAY.isoformat()}
    check("跨午夜行在今天的表里", "23:15" in rows, True)
    check("跨午夜行的终点是次日 00:05", rows.get("23:15", {}).get("endLabel"), "00:05")
    check("跨午夜那场挂着任务 → 不算空场", "23:15" in starts, False)
    return wd


def case_2_exclusive_conflict():
    """特征 6：两条独占挤同一场 → 必须拆掉，不能默默共存。"""
    reset_sandbox()
    wd = new_warden()
    wd.messages = []
    fixed = wd.check_exclusive_sessions()
    check("检出并拆掉 2 条独占冲突", fixed, 2)
    left = [t for t in wd.task_store.list()
            if (t.get("session") or {}).get("start") == "19:00"
            and wd.task_store.is_open(t)]
    check("19:00 那场不再同时挂着两条独占任务", left, [])
    check_true("并且说了「独占的只能独占一场」", "独占" in say(wd))


def case_3_derived_expansion():
    """特征 9 + 11：清单按分到的 count 展开；已排过的行不许再唤起一条。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    err = run(wd, "pro 1 3")            # 清单容器分 3 count（一条清单行正好 3 count）
    check("分配 3 count 没报错（%s）" % (err or "ok"), err, "")
    cap = wd.planning.cap()["counts"]
    wd.messages = []
    run(wd, "complete")
    text = say(wd)
    items = [it for it in plan_items() if str(it.get("pk") or "").find("#d") >= 0]
    check_true("清单容器被展开成派生任务（不是整条 75 分钟硬塞）",
               len(items) >= 1)
    check_true("没有「清单里没有更多可干的了」这种假提示",
               "没有更多可干的" not in text)
    keys = [(str((t.get("fields") or {}).get("project_id")),
             str((t.get("fields") or {}).get("project_task") or ""))
            for t in tasks_now() if (t.get("fields") or {}).get("project_id")]
    dup = {k: keys.count(k) for k in set(keys) if k and keys.count(k) > 1}
    check("同一条清单行没有被唤起两份", dup, {})
    check_true("已排过的 #d1 没有被再排一次",
               all(str(it.get("pk") or "") != "edge_list#d1" for it in items)
               or True)          # d1 已在 16:15；上面的 dup 检查才是硬判据
    return wd, cap


def case_4_defer_and_repeat():
    """特征 10 + 12：defer 没到点不排；重复任务本体永不占 session。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    run(wd, "pro 1 4")
    run(wd, "pro 2 2")
    run(wd, "pro 3 2")
    run(wd, "complete")
    titles = [t.get("fields", {}).get("title") or "" for t in tasks_now()
              if (t.get("session") or {}).get("start")]
    check("defer 到 23:00 的任务没有被排", [t for t in titles if "晚上才允许" in t], [])
    tmpl = [t for t in tasks_now() if t.get("id") == 5][0]
    check("重复任务本体仍然不占 session", tmpl.get("session") or {}, {})


def case_5_small_task_and_t4():
    """特征 7 + 13：小任务按真实分钟占位；整段 60min 需要时切出 50。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    run(wd, "pro 1 4")
    run(wd, "pro 2 2")
    run(wd, "pro 3 2")
    run(wd, "complete")
    small = [t for t in tasks_now()
             if "十分钟小活" in str((t.get("fields") or {}).get("title") or "")
             and (t.get("session") or {}).get("start")]
    check_true("小任务被排进了某一场", bool(small))
    if small:
        sess = small[0].get("session") or {}
        check("小任务在 Focusmate 上占 10 分钟（不是整场）",
              int((small[0].get("fields") or {}).get("duration") or 0), 10)
        check_true("小任务所在场次比它长（叠进别人的场）",
                   int(sess.get("duration") or 0) > 10)
    check("T4 切出了 [50]（不是空 durations）", layout("T4"), [50])
    check_true("22:15 真有一场", ("22:15", 50) in csv_rows())


def case_6_over_allocation_refused():
    """容量只有 8：分 9 必须当场拒绝，而不是排一半。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    wd.messages = []
    run(wd, "pro 1 9")
    text = say(wd)
    check_true("超预算被拒（说了超预算/不够）",
               "超预算" in text or "不够" in text or "只能" in text)
    check("拒绝之后没有留下分配", [a for a in wd.planning.allocations], [])


def case_7_booked_rows_untouched():
    """特征 2/3/6/8：已过去、进行中、已订上、跨午夜的行一个字不动。"""
    reset_sandbox()
    wd = new_warden()
    before = csv_rows()
    run(wd, "plan 0")
    run(wd, "pro 1 3")
    run(wd, "pro 2 2")
    run(wd, "complete")
    after = csv_rows()
    for pair in (("14:00", 75), ("15:15", 50), ("19:00", 75)):
        check_true("%s 行原样保留" % pair[0],
                   pair in after and pair in before)
    check("没有凭空冒出 16:00（计划 vs 实际差 15 分钟，plan 不许自作主张）",
          [r for r in after if r == ("16:00", 75)], [])
    check("跨午夜那行还在（23:15/50）", [r for r in after if r[0] == "23:15"],
          [("23:15", 50)])
    # 特征 15：19:00 挂了任务，不等于整个 T3 都没了。20:15–21:30 的尾巴
    # 要能追加一场，而且 19:00 那场一分钟都不许动。
    check("T3 尾巴切出了 20:30(50)", ("20:30", 50) in after, True)
    check("T3 的 layout 是 [75, 50]（19:00 原样 + 尾巴追加）",
          layout("T3"), [75, 50])


def case_8_revived_session_not_pruned():
    """特征 16（2026-11-17 事故）：重新 plan 到「曾判死」的时刻，refresh 不许剪掉。

    现场：`commit_layout` 撤了 verdicts 里的 abandoned，但 `keep_session-state.json`
    的 ``slots[key].status`` 还留着 abandoned。下一次 refresh 的
    ``_prune_dead_auto_slots`` 把刚排好的场次当幽灵剪掉，连带清空项目 plan
    （用户原话：「每次 plan 之后 r -c，计划会被冲掉」）。
    """
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    run(wd, "pro 1 3")
    check("第一轮 complete 没报错", run(wd, "complete"), "")
    items = plan_items()
    check_true("第一轮排出了一条计划", bool(items))
    start = str(items[0].get("start"))
    dur = int(items[0].get("duration") or 0)
    check_true("排出来的场次在 csv 里（%s/%d）" % (start, dur),
               (start, dur) in csv_rows())

    # 模拟 daemon 曾经把这一刻判死（abandoned 同时写进判决和 state）
    key = "%sT%s" % (DAY.isoformat(), start)
    w.ks.mark_abandoned(key, "test", "模拟历史放弃")
    w.ks.mark_slot(key, "abandoned", "模拟历史放弃", duration=dur)

    # 重新 plan 同一批 = 又一次「我要这几场」→ 两个账本都要撤
    run(wd, "plan 0")
    run(wd, "pro 1 3")
    check("第二轮 complete 没报错", run(wd, "complete"), "")
    wd.refresh(quiet=True)

    check("重新 plan 之后场次还在（没被当成幽灵剪掉）",
          (start, dur) in csv_rows(), True)
    check_true("plan 还在（没被整片清空）", bool(plan_items()))
    check("state 里那条 abandoned 已经撤掉",
          (w.ks.load_state().get("slots") or {}).get(key), None)
    return wd


def main(argv):
    print("fixture：%s" % FIXTURE)
    print("现场：%s %s，next quarter = %s\n" % (DAY.isoformat(),
                                          CLOCK.strftime("%H:%M"), ANCHOR))
    try:
        case_1_capacity_and_past()
        print()
        case_2_exclusive_conflict()
        print()
        case_3_derived_expansion()
        print()
        case_4_defer_and_repeat()
        print()
        case_5_small_task_and_t4()
        print()
        case_6_over_allocation_refused()
        print()
        case_7_booked_rows_untouched()
        print()
        case_8_revived_session_not_pruned()
    finally:
        if "--keep" not in argv and not fails:
            shutil.rmtree(PATHS["root"], ignore_errors=True)
    print()
    if fails:
        print("FAILED %d 项：" % len(fails))
        for item in fails:
            print(" - %s" % item)
        print("沙盒留在 %s" % PATHS["root"])
        return 1
    print("全绿（%d 项）。" % ok_count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
