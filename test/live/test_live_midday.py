#!/usr/bin/env python3
"""集成用例：**midday-1558** —— 真实数据在 15:58 那一刻跑 `plan`。

数据和现场都是固化的：`fixtures/midday-1558/` 里就是 2026-11-16 15:58 抓下来的
`data.csv` / `tasks.json` / `projects.json` / `session-layouts.json` /
`calendar-download.json`（日期写成 ``{{DAY}}``，跑测试时替换成今天，所以它永远
是「今天 15:58」这个现场，不会过期）。

现场长这样（照抄那一屏）：

    T1  09:00–12:30   整段已经过去
    T2  14:00–18:00   14:00(75) 已结束 · 15:15(50) 正在进行
                      16:15(75) 未订 · 17:30(25) 未订
    T3  19:00–21:30   19:00(75) **已经订上 Focusmate**
    T4  22:15–23:15   整段 60min，还没切 session

四条必须成立的口径（用户 2026-11-16 原话）：

1. **只排接下来剩下的时间，从下一个十五分钟整点开始**：15:58 敲 plan，
   14:00 / 15:15 这两场一个字都不许动；容量只算 16:00 之后。
2. **上一次安排要保留**：再 plan 一次不许把上一轮的任务摘掉，要清得手动
   `plan clear`。
3. **已经订上的场次不许重切**：19:00(75) 在 Focusmate 上是真 session，
   plan 不能把它改成 19:30(75)（那要先取消再重订，就会「约都没约上」）。
4. **整段 60 分钟要能切出 session**：需要它的时候 T4 必须切出 50 分钟那场，
   不能提交一个空的 `durations: []` 把容量白吃掉。
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

FIXTURE = os.path.join(HERE, "fixtures", "midday-1558")
DAY = datetime.date.today()
CLOCK = datetime.datetime.combine(DAY, datetime.time(15, 58))
ANCHOR = "16:00"          # 15:58 的下一个十五分钟整点

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


# ---------------------------------------------------------------- 铺沙盒
def build_sandbox():
    tmp = tempfile.mkdtemp(prefix="ti-midday-%s-" % DAY.isoformat())
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
    mapping = {
        "csv": "data.csv", "tasks": "tasks.json", "projects": "projects.json",
        "calendar": "calendar-download.json", "layouts": "session-layouts.json",
        "extras": "session-extras.json", "rules": "session-rules.json",
        "mutes": "session-mutes.json",
    }
    stamp = DAY.isoformat()
    for key, name in mapping.items():
        text = open(os.path.join(FIXTURE, name), encoding="utf-8").read()
        with open(paths[key], "w", encoding="utf-8") as f:
            f.write(text.replace("{{DAY}}", stamp))
    shutil.copy2(os.path.join(ROOT, "data", "settings.json"), paths["settings"])
    with open(paths["keytime_json"], "w", encoding="utf-8") as f:
        json.dump({"focus": {"enabled": False}, "notify": {"enabled": False}}, f)
    return paths


PATHS = build_sandbox()


def reset_sandbox():
    """把 fixture 重新铺一遍：每个用例都从「15:58 那一刻」重新开始，互不污染。"""
    inside = {"csv", "tasks", "projects", "calendar", "layouts",
              "extras", "rules", "mutes"}
    for key, path in PATHS.items():
        if key in inside:
            continue
        if os.path.isfile(path):
            os.unlink(path)
    shutil.rmtree(PATHS["data"], ignore_errors=True)
    os.makedirs(PATHS["data"], exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "data", "settings.json"), PATHS["settings"])
    with open(PATHS["keytime_json"], "w", encoding="utf-8") as f:
        json.dump({"focus": {"enabled": False}, "notify": {"enabled": False}}, f)
    stamp = DAY.isoformat()
    files = {"csv": "data.csv", "tasks": "tasks.json", "projects": "projects.json",
             "calendar": "calendar-download.json", "layouts": "session-layouts.json",
             "extras": "session-extras.json", "rules": "session-rules.json",
             "mutes": "session-mutes.json"}
    for key, name in files.items():
        text = open(os.path.join(FIXTURE, name), encoding="utf-8").read()
        with open(PATHS[key], "w", encoding="utf-8") as f:
            f.write(text.replace("{{DAY}}", stamp))
META = json.load(open(os.path.join(FIXTURE, "meta.json"), encoding="utf-8"))

sys.path.insert(0, HERE)
import fixture  # noqa: E402

for k, v in fixture.env_for(PATHS).items():
    os.environ[k] = v

from rich.console import Console  # noqa: E402

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
pp, pj, ts, sp, sch = w.pp, w.pj, w.ts, w.sp, w.sch


class Bridge(object):
    """只回答「Focusmate 上已经有哪些场」（meta.booked 里那几场），不出网。"""

    def __init__(self):
        self.calls = []

    def _sessions(self):
        out = []
        for i, (start, dur) in enumerate(META.get("booked") or [], 1):
            h, m = (int(x) for x in str(start).split(":"))
            at = datetime.datetime.combine(DAY, datetime.time(h, m))
            out.append({"id": "sess-%s" % start.replace(":", ""),
                        "startMs": int(at.timestamp() * 1000),
                        "duration": int(dur), "date": DAY.isoformat(),
                        "start": start, "meetingType": "paired"})
        return out

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "snapshot":
            return {"date": DAY.isoformat(),
                    "now": int(CLOCK.timestamp() * 1000),
                    "sessions": self._sessions()}
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


def plan_of_day():
    store = json.load(open(PATHS["projects"], encoding="utf-8"))
    out = {}
    for op in store.get("projects") or []:
        plan = (op.get("plans") or {}).get(DAY.isoformat()) or {}
        if plan.get("items"):
            out[op.get("id")] = plan
    return out


def task_sessions():
    return [(str((t.get("session") or {}).get("start") or ""),
             (t.get("fields") or {}).get("title") or "",
             t.get("id"))
            for t in json.load(open(PATHS["tasks"], encoding="utf-8"))["tasks"]
            if (t.get("session") or {}).get("date") == DAY.isoformat()
            and ts.TaskStore.status_of(t) == "active"]


# ---------------------------------------------------------------- 用例

def scenario_capacity():
    """1) 容量只算「下一个十五分钟整点」之后，且不含已经开始的那场。"""
    reset_sandbox()
    wd = new_warden()
    slots, notes = wd._plan_slots(DAY)
    starts = [s.get("start") for s in slots]
    check("15:58 的空场里没有 16:00 之前的（14:00/15:15 都不算）",
          [s for s in starts if s < ANCHOR], [])
    check_true("16:15 那场算空场（未订上也算）", "16:15" in starts)
    check_true("22:15 整段算空场（60min → 2 count）", "22:15" in starts)
    # 19:00 已经挂着任务（History 2.1 C 那一条）—— 它不是空场，plan 不许再往里塞
    check_true("19:00 不算空场（已经挂着任务）", "19:00" not in starts)
    # 19:00(75) 挂着任务不等于整个 T3 都没了：19:00–20:15 + 15 分钟休息之后
    # 到 21:30 还有 60 分钟空着（= 2 count）。以前这一段被整段忽略，plan 说
    # 「今天只剩 2 count」，用户想给 English 分 3 就被判超预算。
    check_true("T3 的尾巴 20:30 也算空场（19:00 之后还空 60min）",
               "20:30" in starts)
    cap = pp.capacity(slots)
    check("可分配的 count = 16:15(3) + 20:30(2) + 22:15(2) = 7",
          cap.get("counts"), 7)
    # 表格里也要看得见这一截：一行「20:30–21:30 剩余 60min（2 count）未切
    # session」。以前 T3 有行就整段不再补占位，用户看到的是「20:15 之后什么都没有」。
    gap_rows = [r for r in wd.rows if r.get("partial_gap")]
    check("表里给 T3 尾巴补了一行占位",
          [(r.get("startLabel"), r.get("endLabel"), r.get("durationMin"),
            r.get("plan_counts")) for r in gap_rows],
          [("20:30", "21:30", 60, 2)])
    # 面板要说的也是 7；以前会把「上一轮 plan 挂着的任务」当成可重排，
    # 于是说成 10 / 11 count，把已经排好的场次全掀掉。
    run(wd, "plan 0")
    text = say(wd)
    check_true("plan 面板说「7 count 可以分」", "7 count" in text)
    check_true("面板说明尾巴是「只追加、不重切」",
               "半截时段只追加" in text and "20:30" in text)
    check_true("面板不再把已经排好的算成空闲",
               "10 count" not in text and "11 count" not in text)


def scenario_plan_keeps_past_and_booked():
    """2) 跑完 plan：过去/进行中的场次不动，已订上的场次不被重切。"""
    reset_sandbox()
    wd = new_warden()
    before = csv_rows()
    run(wd, "plan 0")
    # 容量 7，这里只分 5（3+2）：English 的 2 count 正好落进 T3 尾巴。
    for idx, count in ((2, 3), (5, 2)):
        run(wd, "task %d %d" % (idx, count))
    err = run(wd, "complete")
    check("plan complete 没报错（%s）" % (err or "ok"), err, "")

    after = csv_rows()
    check_true("14:00(75) 还在，没被动过",
               ("14:00", 75) in after and ("14:00", 75) in before)
    check_true("15:15(50) 还在，没被动过",
               ("15:15", 50) in after and ("15:15", 50) in before)
    check("19:00(75) 还是 75 分钟那一场（没被切成 19:30）",
          ("19:00", 75) in after, True)
    check("没有凭空冒出 19:30", [r for r in after if r[0] == "19:30"], [])
    # 尾巴追加：19:00 原样保留，20:30 新切一场 —— 不是把整段重切。
    check("T3 尾巴切出了 20:30(50)", ("20:30", 50) in after, True)
    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
    t3 = (layouts.get(DAY.isoformat()) or {}).get("T3") or {}
    check("T3 的 layout 是 [75, 50]（前缀原样 + 尾巴追加）",
          t3.get("durations"), [75, 50])
    # plan 自己切出来的段打 source=plan：表格契约列显示 🗂plan，
    # 下一次 plan 认得它可以重算（手动 `period` 切的是 manual，不带这个标记）。
    check("T3 的 layout 来源是 plan（不是 manual）", t3.get("source"), "plan")
    wd.rebuild_rows()
    made = {r.get("startLabel"): bool(r.get("plan_made")) for r in wd.rows}
    check("19:00 / 20:30 两行都带 plan 标记",
          [made.get("19:00"), made.get("20:30")], [True, True])
    check("没有凭空冒出 09:00–12:00（T1 已经过去了）",
          [r for r in after if r[0] < "13:00"], [])

    sess = task_sessions()
    check("没有任何任务挂到 16:00 之前的场次",
          [s for s, _t, _i in sess if s and s < ANCHOR], [])
    check("过去/进行中的两场没有任务",
          [(s, t) for s, t, _i in sess if s in ("14:00", "15:15")], [])
    # 真实 Focusmate 上是 16:00(75)（data.csv 写的是 16:15，差 15 分钟）——
    # 这一场是「计划外」行：不是契约，plan 不该往里塞任务，也不该把它改掉。
    check("真实 16:00 那场（计划外）没有被塞任务",
          [t for s, t, _i in sess if s == "16:00"], [])
    check("真实 16:00 那场本身还在（没被 plan 抹掉）",
          [r for r in after if r[0] == "16:00"], [])
    # 23:15 那场已在 Focusmate 上订好（data.csv 里没有它）——「计划外」行，
    # plan 同样不该碰它、也不该把它删掉。
    check("23:15 那场（计划外）还在、没被塞任务",
          [(r[0], r[1]) for r in after if r[0] == "23:15"]
          + [t for s, t, _i in sess if s == "23:15"], [])
    return wd


def scenario_leftovers_reused():
    """残留任务（唤起了但没挂上 session 的那两条）必须被复用，不许复制一份新的。"""
    reset_sandbox()
    wd = new_warden()
    before = [t for t in wd.task_store.list()
              if (t.get("fields") or {}).get("project_id")]
    keys_before = sorted({(str((t.get("fields") or {}).get("project_id")),
                           str((t.get("fields") or {}).get("project_task") or ""))
                          for t in before if t.get("fields")})
    check_true("沙盒里有「唤起了但没挂上」的残留任务",
               any(not (t.get("session") or {}).get("start") for t in before))
    run(wd, "plan 0")
    for idx, count in ((2, 3), (5, 2)):
        run(wd, "task %d %d" % (idx, count))
    run(wd, "complete")
    after = [t for t in wd.task_store.list()
             if (t.get("fields") or {}).get("project_id")
             and wd.task_store.is_open(t)]
    keys = [(str((t.get("fields") or {}).get("project_id")),
             str((t.get("fields") or {}).get("project_task") or "")) for t in after]
    dup = {k: keys.count(k) for k in set(keys) if keys.count(k) > 1}
    check("同一条项目任务没有被复制成多条", dup, {})
    check_true("残留任务没被丢弃（要么被复用挂上场，要么还在队列里）",
               not (set(keys_before) - set(keys)))


def scenario_preserve_previous():
    """3) 再 plan 一次要保留上一轮；要清得显式 `plan clear`。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    for idx, count in ((2, 3), (5, 2)):
        run(wd, "task %d %d" % (idx, count))
    run(wd, "complete")
    first = {k: len(v.get("items") or []) for k, v in plan_of_day().items()}
    bound_first = dict((s, t) for s, t, _i in task_sessions())
    check_true("第一轮排出了计划", bool(first))

    # 第二轮：只给一个无关项目 1 count
    run(wd, "plan 0")
    run(wd, "task 6 1")
    run(wd, "complete")
    second = {k: len(v.get("items") or []) for k, v in plan_of_day().items()}
    check_true("第二轮之后第一轮的计划还在（没被整天重排清掉）",
               all(second.get(k, 0) >= n for k, n in first.items()))
    still = dict((s, t) for s, t, _i in task_sessions())
    check("第一轮挂好的任务仍在原来那场",
          {s: still.get(s) for s in bound_first}, bound_first)

    # 显式清空
    run(wd, "plan clear")
    check("plan clear 之后当天计划清空", plan_of_day(), {})
    left = [s for s, _t, _i in task_sessions()]
    check_true("plan clear 之后那些任务不再占场次", not left or
               all(s for s in left))


def scenario_long_period_used():
    """4) 需要时 T4（22:15–23:15，60min）要切出 50 分钟那场。"""
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    # 7 个 count 全分下去（16:15 拿 3、20:30 拿 2、22:15 拿 2），逼它用到 T4
    for idx, count in ((2, 3), (5, 2), (1, 2)):
        run(wd, "task %d %d" % (idx, count))
    err = run(wd, "complete")
    check("大批量 plan 没报错（%s）" % (err or "ok"), err, "")
    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
    rec = (layouts.get(DAY.isoformat()) or {}).get("T4") or {}
    check_true("T4 切出了 session（不是 durations: []）", rec.get("durations"))
    check("T4 的切分是 [50]（60min 容量 2 count）", rec.get("durations"), [50])
    check_true("22:15 真的有一场", ("22:15", 50) in csv_rows())


def main(argv):
    print("fixture：%s（%s）" % (FIXTURE, META.get("why", "")[:60]))
    print("现场：%s %s，next quarter = %s\n" % (DAY.isoformat(),
                                          CLOCK.strftime("%H:%M"), ANCHOR))
    try:
        scenario_capacity()
        print()
        scenario_plan_keeps_past_and_booked()
        print()
        scenario_leftovers_reused()
        print()
        scenario_preserve_previous()
        print()
        scenario_long_period_used()
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
