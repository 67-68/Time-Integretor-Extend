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
                      —— layout `source=manual`：用户自己切的硬约束
    T3  19:00–21:30   19:00(75) **已经订上 Focusmate**，但 layout 是 auto/plan
                      —— 上一轮 plan 自己切的，按现行语义可以被重切
    T4  22:15–23:15   整段 60min，还没切 session

四条必须成立的口径（2026-11-17 用户推翻旧口径之后的现行语义）：

1. **只排接下来剩下的时间，从下一个十五分钟整点开始**：15:58 敲 plan，
   14:00 / 15:15 这两场一个字都不许动；容量只算 16:00 之后。
2. **整天重排**：上一轮 plan 唤起的任务不再算占用（`_plan_slots(replan=True)`），
   `complete` 走 `_plan_apply(append_to_plan=False)` 整体替换旧计划。
   旧口径是「上一次安排要保留，要清得手动 plan clear」，用户 2026-11-17 明确推翻：
   原话「在 plan 开始之后，之前安排的任务仍然在占地方，应该把他们视为没有」。
3. **冻结只看 layout 来源**：manual（T2）是硬约束，plan 只能往里填任务、切分
   一个数字都不许变；auto/plan（T3）**即使已经订上也可以被重切**——19:00 那场
   会从 75 变成 50。旧口径「已经订上的场次不许重切」同一天被推翻。
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


def plan_task_ids():
    """这一天 plan items 沿用的本地任务 id（重排前后对比用）。"""
    out = []
    store = json.load(open(PATHS["projects"], encoding="utf-8"))
    for op in store.get("projects") or []:
        plan = (op.get("plans") or {}).get(DAY.isoformat()) or {}
        for item in plan.get("items") or []:
            if item.get("task_id") is not None:
                out.append(item["task_id"])
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
    """1) 容量只算「下一个十五分钟整点」之后；plan 口径把上一轮 plan 的场次放回池子。

    两个口径要分开看（2026-11-17 之后）：

    * ``replan=True`` 是 ``plan`` 真正用的：上一轮 plan 切出来的场次（T3）不算
      占用，整段回到池子里 → 3 场 10 count；
    * ``replan=False`` 是「上一轮还占地方」的对照口径（sessional plan / 表格
      尾巴占位还在用）：T3 只剩 19:00 之后那 60 分钟尾巴 → 3+2+2 = 7 count。
    """
    reset_sandbox()
    wd = new_warden()

    # ---- plan 口径：整天重排（replan=True） ------------------------------
    slots, notes = wd._plan_slots(DAY, replan=True)
    starts = [s.get("start") for s in slots]
    check("plan 口径：15:58 的空场里没有 16:00 之前的（14:00/15:15 都不算）",
          [s for s in starts if s < ANCHOR], [])
    check_true("16:15 那场算空场（还没订上也照样算）", "16:15" in starts)
    # 旧口径要求「19:00 已经挂着任务/已经订上 → 不算空场」；2026-11-17 改成
    # 整天重排：T3 是上一轮 plan 自己切的（auto/plan），它的场次回到池子里。
    check_true("19:00 整段 T3 回到池子（上一轮 plan 切的场次不算占用）",
               "19:00" in starts)
    check_true("22:15 整段算空场（60min → 2 count）", "22:15" in starts)
    # 3 场 = 16:15(3 count) + 19:00(5 count) + 22:15(2 count) = 10 count。
    # 19:00 是 5 而不是 3：T3 整段 19:00–21:30 = 150 分钟，按「每 30 分钟一个
    # block、每 block 25 分钟」算 floor(150/30)=5 count（不是 150/25=6）。
    cap = pp.capacity(slots)
    check("plan 口径：3 场空 session", cap.get("sessions"), 3)
    check("plan 口径的 count = 3 + 5 + 2 = 10", cap.get("counts"), 10)
    check("plan 口径的总分钟 = 10 × 25 = 250", cap.get("minutes"), 250)
    check_true("plan 口径的 notes 不再提「只追加」那套尾巴口径",
               not any("半截时段只追加" in n for n in notes))

    # ---- 对照口径：上一轮 plan 还占地方（replan=False） -------------------
    keep_slots, keep_notes = wd._plan_slots(DAY)
    keep_starts = [s.get("start") for s in keep_slots]
    check("对照口径：19:00 仍被（上一轮 plan 的场次）占着，不算空场",
          "19:00" not in keep_starts, True)
    # 19:00(75) 之后到 21:30 还有 60 分钟空着（2 count）：`tail_gap` 给一个
    # append slot（只追加、不重切）。这条口径 11-17 之后只剩 sessional plan
    # 和表格占位在用了。
    check_true("对照口径：T3 尾巴 20:30 是只追加的 slot（append=True）",
               any(s.get("start") == "20:30" and s.get("append")
                   for s in keep_slots))
    check("对照口径的 count = 16:15(3) + 20:30(2) + 22:15(2) = 7",
          pp.capacity(keep_slots).get("counts"), 7)
    check_true("对照口径的 notes 还写着「半截时段只追加、不重切」",
               any("半截时段只追加" in n for n in keep_notes))
    # 表格里也要看得见这一截：一行「20:30–21:30 剩余 60min（2 count）未切
    # session」。以前 T3 有行就整段不再补占位，用户看到的是「20:15 之后什么都没有」。
    gap_rows = [r for r in wd.rows if r.get("partial_gap")]
    check("表里给 T3 尾巴补了一行占位",
          [(r.get("startLabel"), r.get("endLabel"), r.get("durationMin"),
            r.get("plan_counts")) for r in gap_rows],
          [("20:30", "21:30", 60, 2)])

    # ---- 面板说的是 plan 口径的 10（不是对照口径的 7） --------------------
    run(wd, "plan 0")
    text = say(wd)
    check_true("plan 面板说「3 场空 session = 10 count 可以分」",
               "3 场空 session = 10 count 可以分" in text)
    check_true("面板把总分钟说成 250", "250 分钟" in text)
    check_true("面板说明 16:15 / 19:00 没订上也计入空场",
               "16:15、19:00 还没订上 Focusmate，但仍计入空场" in text)
    check_true("面板不再把上一轮 plan 的场次说成「已占用」",
               "1 场已经挂着任务了，不算空闲" in text
               and "2 场已经挂着任务了" not in text)


def scenario_plan_keeps_past_and_booked():
    """2) 跑完 plan：过去/进行中的不动，manual 不重切，plan 的场次**按新语义重切**。

    2026-11-17 之前这里断言的是「19:00(75) 原样保留 + 20:30 追加 50」。用户当天
    推翻了那条口径（「之前安排的任务仍然在占地方，应该把他们视为没有」），现行
    语义是：T3 属于 auto/plan，**即使 19:00 已经订上**，下一次 plan 也可以整段
    重切 —— 现在切出来的是 19:00(50)，旧的 19:00(75) 那行被替换掉。
    """
    reset_sandbox()
    wd = new_warden()
    before = csv_rows()
    # 现场本来就有任务挂着（17:30 / 23:15 的 flashcard review 是 fixture 的残留）：
    # 「plan 不碰计划外场次」的判据是**这些绑定一个都没变**，而不是「一个人都没有」。
    bound_before = dict((s, t) for s, t, _i in task_sessions())
    run(wd, "plan 0")
    # 容量 10（plan 口径），这里只分 5（3+2）：History 3 count、English 2 count。
    for idx, count in ((2, 3), (5, 2)):
        run(wd, "pro %d %d" % (idx, count))
    err = run(wd, "complete")
    check("plan complete 没报错（%s）" % (err or "ok"), err, "")

    after = csv_rows()
    # 过去 / 进行中的两场：任何语义下都不许动（anchor 只管未来）。
    check_true("14:00(75) 还在，没被动过",
               ("14:00", 75) in after and ("14:00", 75) in before)
    check_true("15:15(50) 还在，没被动过",
               ("15:15", 50) in after and ("15:15", 50) in before)

    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
    recs = layouts.get(DAY.isoformat()) or {}
    # T2 是 manual（用户自己 `session` 切的）：plan 只能往里填任务（16:15 被填了
    # History），但切分一个数字都不许变 —— 这是硬约束，11-17 之后也没变。
    t2 = recs.get("T2") or {}
    check("T2 是 manual：切分原样不动", t2.get("durations"), [75, 50, 75, 25])
    check("T2 的来源还是 manual", t2.get("source"), "manual")

    # T3 是上一轮 plan 自己切的（fixture 里 source=auto，和 plan 同类）：
    # 即使 19:00 已经订上也可以重切 —— 整段重切成 [50]，19:00 从 75 变 50。
    check("19:00 被重切成 50 分钟（旧的「已订上不许重切」已推翻）",
          ("19:00", 50) in after, True)
    check("旧的 19:00(75) 那一行已经被替换掉", ("19:00", 75) not in after, True)
    check("没有凭空冒出 19:30", [r for r in after if r[0] == "19:30"], [])
    t3 = recs.get("T3") or {}
    check("T3 整段重切成 [50]（不是旧口径的 [75, 50] 前缀+追加）",
          t3.get("durations"), [50])
    # plan 自己切出来的段打 source=plan：表格契约列显示 🗂plan，
    # 下一次 plan 认得它可以重算（手动 `period` 切的是 manual，不带这个标记）。
    check("T3 的来源是 plan（下一次 plan 还认得它可以重算）",
          t3.get("source"), "plan")
    check("旧口径的 20:30 尾巴场次不再产生（整段重切，不做追加）",
          [r for r in after if r[0] == "20:30"], [])

    wd.rebuild_rows()
    made = {r.get("startLabel"): bool(r.get("plan_made")) for r in wd.rows}
    check("19:00 那行带 plan 标记", made.get("19:00"), True)
    check("16:15 是 manual 场次，不带 plan 标记", made.get("16:15"), False)
    check("没有凭空冒出 09:00–12:00（T1 已经过去了）",
          [r for r in after if r[0] < "13:00"], [])

    sess = task_sessions()
    check("没有任何任务挂到 16:00 之前的场次",
          [s for s, _t, _i in sess if s and s < ANCHOR], [])
    check("过去/进行中的两场没有任务",
          [(s, t) for s, t, _i in sess if s in ("14:00", "15:15")], [])
    check_true("manual 的 16:15 被填进 History 那条任务",
               [t for s, t, _i in sess if s == "16:15" and t.startswith("History:")])
    check_true("plan 重切出来的 19:00 挂上 English 那条任务",
               [t for s, t, _i in sess if s == "19:00" and t.startswith("Lit:")])
    # 真实 Focusmate 上是 16:00(75)（data.csv 写的是 16:15，差 15 分钟）——
    # 这一场是「计划外」：不写契约行、不塞任务、也不该被 plan 抹掉（它只在 API 里）。
    check("plan 没有往 16:00 写契约行（真实那场不归 data.csv 管）",
          [r for r in after if r[0] == "16:00"], [])
    check("16:00 那场没有被塞任务", [t for s, t, _i in sess if s == "16:00"], [])
    # 23:15 那场已在 Focusmate 上订好（data.csv 里没有它）—— plan 同样不碰。
    check("plan 没有动 23:15（它只在 API 里，不在 data.csv）",
          [r for r in after if r[0] == "23:15"], [])
    check("23:15 那场挂着的东西和 plan 之前一模一样",
          [(s, t) for s, t, _i in sess if s == "23:15"],
          [(s, t) for s, t in bound_before.items() if s == "23:15"])
    check("17:30 那场（也是现场本来就有的）同样没被碰",
          [(s, t) for s, t, _i in sess if s == "17:30"],
          [(s, t) for s, t in bound_before.items() if s == "17:30"])
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
        run(wd, "pro %d %d" % (idx, count))
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
    """3) 再 plan 一次是**整体替换**上一轮（旧的「上一轮保留」已推翻）。

    2026-11-17 之前这里断言的是「第二轮之后第一轮的计划还在」「第一轮挂好的任务
    仍在原来那场」。用户当天明确定成整天重排（上一轮 plan 不算占用），所以现在
    正确的断言是反过来的：第一轮的 items 被换掉、任务被解绑。`plan clear` 才是
    「我不想排了」的显式清空（它只清计划 items/绑定/分配，不动 layout）。
    """
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    for idx, count in ((2, 3), (5, 2)):
        run(wd, "pro %d %d" % (idx, count))
    run(wd, "complete")
    first = {k: len(v.get("items") or []) for k, v in plan_of_day().items()}
    first_ids = plan_task_ids()
    check_true("第一轮排出了计划", bool(first))
    check_true("第一轮挂上了任务", bool(first_ids))

    # 第二轮：只给一个无关项目 1 count。整天重排 → 第一轮的计划整体换掉。
    run(wd, "plan 0")
    run(wd, "pro 6 1")
    err = run(wd, "complete")
    check("第二轮 complete 没报错（%s）" % (err or "ok"), err, "")
    text = say(wd)
    check_true("第二轮明说「先撤掉旧 plan 的 %d 条任务」" % len(first_ids),
               "先撤掉旧 plan 的 %d 条任务" % len(first_ids) in text)
    second = {k: len(v.get("items") or []) for k, v in plan_of_day().items()}
    check("第一轮的项目在第二轮之后没有旧 items（被整体替换）",
          dict((k, second.get(k, 0)) for k in first),
          dict((k, 0) for k in first))
    check("第二轮只有新项目有 items", sorted(second), [7])
    check("第一轮 plan 沿用的任务全部解绑",
          [(t.get("session") or {}).get("start")
           for t in wd.task_store.list() if t.get("id") in first_ids],
          [None] * len(first_ids))
    check_true("任务本身还在库里（解绑 ≠ 丢任务）",
               all(any(t.get("id") == tid for t in wd.task_store.list())
                   for tid in first_ids))

    # 显式清空：只撤计划，不动 layout。
    run(wd, "plan clear")
    check("plan clear 之后当天计划清空", plan_of_day(), {})
    check("plan clear 之后没有计划沿用的任务", plan_task_ids(), [])
    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
    t2 = (layouts.get(DAY.isoformat()) or {}).get("T2") or {}
    check("plan clear 不删排布（T2 的 manual 切分还在）",
          t2.get("durations"), [75, 50, 75, 25])


def scenario_long_period_used():
    """4) 需要时 T4（22:15–23:15，60min）要切出 50 分钟那场。

    为什么用 10 count 的分配、而不是旧的 7 count：7 count 在现行语义下会被
    16:15(3) + T3 重切出来的 19:00/20:00(4) 吃掉，T4 根本轮不到 —— 那样就测不到
    「整段 60 分钟能切出 50」这个意图（旧的 7 count 会落到 20:30 尾巴追加，那是
    已被推翻的口径）。这里把 plan 口径的 10 count 全分下去，逼排期走到 T4。
    """
    reset_sandbox()
    wd = new_warden()
    run(wd, "plan 0")
    for idx, count in ((2, 4), (5, 4), (1, 2)):
        run(wd, "pro %d %d" % (idx, count))
    err = run(wd, "complete")
    check("大批量 plan 没报错（%s）" % (err or "ok"), err, "")
    layouts = json.load(open(PATHS["layouts"], encoding="utf-8"))["layouts"]
    rec = (layouts.get(DAY.isoformat()) or {}).get("T4") or {}
    check_true("T4 切出了 session（不是 durations: []）", rec.get("durations"))
    # 60 分钟 / 每 30 分钟一个 block = 2 block = 2 count = 50 分钟。
    check("T4 的切分是 [50]（60min → 2 count）", rec.get("durations"), [50])
    check_true("22:15 真的有一场 50 分钟", ("22:15", 50) in csv_rows())


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
