#!/usr/bin/env python3
"""集成测试的**沙盒与 mock 数据**：凭空造一天的工作区，不碰用户真实数据。

为什么要有这个东西
------------------
`test/test_*.py` 全是进程内、离线、stub 掉 bridge 的单元测试；真正难出错的是
模块之间的接缝（period → 容量 → 分配 → 切 session → 写 layout → 重算 data.csv
→ 挂任务 → 预约 → 对账）。要验那条线，就必须有一份**形状正确、内容虚构**的
工作区，喂给一条真实的 `warden.py`。

沙盒里有什么
------------
    <sandbox>/data.csv                 只有注释头（一天都还没排）
    <sandbox>/data/tasks.json          空任务库
    <sandbox>/data/projects.json       3 个 pro + 各自 SOP op + 少量任务
    <sandbox>/temp/calendar-download.json   只含目标日一个时段 13:00–15:30
    <sandbox>/temp/session-layouts.json     空
    <sandbox>/temp/...                 其余运行态文件（state/verdicts/log）

为什么是 13:00–15:30
-------------------
150 分钟 → 容量 floor(150/30)=5 个 block → 5 count = 125 分钟。
5 个 count 反推出的排布是 75 + 50 两场（75+15 休息 + 50 = 150，正好填满），
所以**只会有 2 次真实预约**，是「3 个项目 + 多场次 + 叠加小任务」的最小代价。

三个项目刻意挑成不同形状，各自钉一条逻辑：
    A  parallel / 2 count / 一条 50 分钟任务
    B  sequential / 2 count / 一条 50 分钟任务
    C  parallel / 1 count / 一条 **10 分钟**任务（<25 → 必须叠进别人的大场）

用法
----
    python3 -B test/live/fixture.py            # 造一份到 /tmp 里给你看
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 目标日相对今天的偏移。取 10 天：Focusmate 上肯定还没订，日历也还没覆盖到，
#: 全是沙盒自己造的；离得远，跑完取消也不算迟到取消。
DEFAULT_OFFSET = 10

#: 目标日唯一的一个时段（start, end）。
PERIOD = ("13:00", "15:30")

#: 每个元素 = (pro 标题, op task_type, [(任务标题, count, duration 分钟)])
PROJECTS = (
    ("沙盒 A · 并行", "parallel", [("A · 写方案骨架", 2, 50)]),
    ("沙盒 B · 顺序", "sequential", [("B · 收集材料", 2, 50)]),
    ("沙盒 C · 小活", "parallel", [("C · 十分钟小活", 1, 10)]),
)

#: 分配（用户 `plan` 之后敲的那几条 `task <序号> <count>`）。
ALLOCATION = ((1, 2), (2, 2), (3, 1))

#: 任务标题里必须带的标记：远端探针靠它认出「这是测试挂上去的 task」。
TITLE_MARK = "[live-test]"


def target_date(offset=DEFAULT_OFFSET):
    return datetime.date.today() + datetime.timedelta(days=offset)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def calendar_download(day):
    """一份只含目标日那个时段的 calendar-download.json。

    形状照抄 `session_plan.build_periods()` 的产出（periods 是读的一侧，
    events 只是留个可读的底稿）——集成测试不需要真的连 CalDAV。
    """
    start, end = PERIOD
    iso = day.isoformat()
    event = {
        "date": iso, "start": start, "end": end,
        "start_iso": "%sT%s:00" % (iso, start),
        "end_iso": "%sT%s:00" % (iso, end),
        "calendar": "空闲", "title": "沙盒时段", "all_day": False,
    }
    return {
        "version": 1,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "start_date": iso,
        "days": 1,
        "profiles": ["空闲"],
        "events": [event],
        "periods": [{
            "id": "T1", "date": iso, "start": start, "end": end,
            "type": "空闲", "name": "沙盒时段", "calendar": "空闲", "title": "沙盒时段",
            "start_iso": event["start_iso"], "end_iso": event["end_iso"],
        }],
    }


def projects_store(day):
    """3 个 pro + 3 个 SOP op。字段照抄 data/projects.json 的真实形状。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pros, ops = [], []
    for idx, (title, task_type, tasks) in enumerate(PROJECTS, 1):
        op_id = idx
        pros.append({
            "id": idx, "title": title, "stage": "running",
            "file": None, "sop_op_id": op_id, "incubator_op_id": None,
            "op_ids": [op_id], "status": "active", "status_at": None,
            "status_reason": None, "stage_locked": False,
            "created_at": now, "updated_at": now, "soft_interrupts": [],
        })
        ops.append({
            "id": op_id, "title": "%s · SOP" % title, "op_type": "SOP",
            "task_type": task_type, "pro_id": idx, "project_tag": "", "tags": [],
            "file": None, "omnifocus": None, "status": "active",
            "status_at": None, "status_reason": None,
            "imported_at": now, "synced_at": now,
            "tasks": [
                {
                    "pk": "live_%d_%d" % (idx, n), "title": "%s %s" % (TITLE_MARK, name),
                    "parent": None, "parent_id": None, "due": None, "defer": None,
                    "planned": None, "note": "", "count": count, "duration": duration,
                    "completed": False, "dropped": False, "effective_completed": False,
                    "effective_dropped": False, "blocked": False, "local_only": False,
                    "rank": n,
                }
                for n, (name, count, duration) in enumerate(tasks)
            ],
            "plans": [],
        })
    return {"version": 1, "next_id": 10, "next_pro_id": 10,
            "projects": ops, "pros": pros}


def data_csv_header(day=None):
    """空的 data.csv：只有注释头。目标日的场次交给 plan 从时段容量反推。"""
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    return (
        "# live 集成测试沙盒，由 test/live/fixture.py 生成 @ %s\n"
        "# 目标日：%s（这一天一行都没有，全交给 plan 从时段容量反推）\n"
        "# 格式：YYYY-MM-DD,HH:MM,duration\n"
        % (stamp, day.isoformat() if day else "?")
    )


def make_sandbox(root=None, offset=DEFAULT_OFFSET):
    """造一份完整沙盒，返回 {root, day, csv, tasks, projects, temp, calendar, ...}。"""
    day = target_date(offset)
    root = root or tempfile.mkdtemp(prefix="ti-live-%s-" % day.isoformat())
    temp = os.path.join(root, "temp")
    data = os.path.join(root, "data")
    os.makedirs(temp, exist_ok=True)
    os.makedirs(data, exist_ok=True)

    paths = {
        "root": root, "day": day, "temp": temp, "data": data,
        "csv": os.path.join(root, "data.csv"),
        "tasks": os.path.join(data, "tasks.json"),
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
        "nostart": os.path.join(temp, "keep_session-nostart.json"),
        "postsession": os.path.join(temp, "keep_session-postsession.json"),
        "failed_actions": os.path.join(temp, "failed-actions.json"),
        "of_outbox": os.path.join(temp, "omnifocus-outbox.json"),
        "logs": os.path.join(temp, "logs"),
    }
    os.makedirs(paths["logs"], exist_ok=True)

    with open(paths["csv"], "w", encoding="utf-8") as f:
        f.write(data_csv_header(day))
    _write_json(paths["tasks"], {"version": 1, "next_id": 1, "tasks": []})
    _write_json(paths["projects"], projects_store(day))
    _write_json(paths["calendar"], calendar_download(day))
    _write_json(paths["layouts"], {"version": 1, "days": {}})
    for key in ("extras", "rules", "mutes"):
        _write_json(paths[key], {"version": 1, "rows": []})
    _write_json(paths["keytime_json"], {"focus": {"enabled": False},
                                       "notify": {"enabled": False}})
    # 设置文件照抄真实那份（阈值/显示偏好），路径类字段不参与，不用担心。
    real_settings = os.path.join(ROOT, "data", "settings.json")
    if os.path.exists(real_settings):
        shutil.copy2(real_settings, paths["settings"])
    return paths


def env_for(paths, *, live=False, window_hours=None):
    """跑 warden 时要设的环境变量：一切读写都必须落在沙盒里。

    ``FOCUSMATE_CONFIG_DIR`` 是唯一的例外 —— 它**故意**指向用户真实的浏览器
    profile（不这么指就没有登录态，订不了场）。所以 live 模式下真正会碰外面
    世界的只有 Focusmate 那两次预约。
    """
    env = dict(os.environ)
    env.update({
        "TI_TEMP": paths["temp"],
        "TI_DATA_CSV": paths["csv"],
        "TI_CALENDAR_DOWNLOAD": paths["calendar"],
        "TI_SESSION_LAYOUTS": paths["layouts"],
        "TI_SESSION_EXTRAS": paths["extras"],
        "TI_SESSION_RULES": paths["rules"],
        "TI_SESSION_MUTES": paths["mutes"],
        "TI_TASKS_FILE": paths["tasks"],
        "TI_TASK_ARCHIVE": paths["archive"],
        "TI_PROJECTS_FILE": paths["projects"],
        "TI_PROJECT_SYNC_FILE": paths["project_sync"],
        "TI_SETTINGS_FILE": paths["settings"],
        "TI_NOSTART_FILE": paths["nostart"],
        "TI_POSTSESSION_FILE": paths["postsession"],
        "TI_PLAN_PINS": paths.get("plan_pins")
                        or os.path.join(paths["temp"], "plan-pins.json"),
        "TI_PLAN_DECISIONS": paths.get("plan_decisions")
                        or os.path.join(paths["temp"], "plan-decisions.json"),
        "TI_PLAN_WATCHDOG": paths.get("plan_watchdog")
                        or os.path.join(paths["temp"], "plan-watchdog.json"),
        "TI_FAILED_ACTIONS": paths["failed_actions"],
        "TI_OMNIFOCUS_OUTBOX": paths["of_outbox"],
        "TI_LOG_DIR": paths["logs"],
        "TI_KEYTIME_JSON": paths["keytime_json"],
        "TI_KEYTIME_STATE": paths["keytime_state"],
        "TI_KEYTIME_DEVICES": paths["keytime_devices"],
        "TI_KEYTIME_RULES": paths["keytime_rules"],
        "TI_KEYTIME_DOWNLOAD": paths["calendar"],
        "KEEP_SESSION_IDLE_CHECK": "0",
        "WARDEN_BRIDGE_IDLE_MS": "3000",
        "PYTHONUNBUFFERED": "1",
    })
    env.pop("TI_BOOK_WINDOW_HOURS", None)
    if window_hours:
        env["TI_BOOK_WINDOW_HOURS"] = str(window_hours)
    if live:
        env["FOCUSMATE_CONFIG_DIR"] = os.path.join(ROOT, "temp", "focusmate-home")
    return env


if __name__ == "__main__":
    p = make_sandbox()
    print("sandbox:", p["root"])
    print("target day:", p["day"].isoformat(), "period:", PERIOD)
    print(open(p["csv"], encoding="utf-8").read())
    print("projects:", len(json.load(open(p["projects"]))["pros"]), "pros,",
          len(json.load(open(p["projects"]))["projects"]), "ops")
