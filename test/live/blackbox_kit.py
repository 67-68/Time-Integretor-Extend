#!/usr/bin/env python3
"""纯黑盒工具箱：一条真实的 ``warden.py`` 子进程 + 用户命令 + 只读数据文件。

和 ``test_live_plan.py`` / ``test_live_edge_cases.py`` 的区别（很重要）：

    它们      import warden.py，在同一个进程里直接调 ``wd.handle("plan")``，
              只 stub 掉预约出口。能测逻辑，但测不到「进程边界」——比如
              「上一轮 plan 的场次在**下一次进程**里还算不算资源」。
    这一份    只做三件事：造沙盒数据、``subprocess.run(warden.py --exec ...)``、
              读落盘文件。**不 import warden 的任何模块、不 stub 任何函数。**

所以这里的断言只有两种来源：

* 子进程 stdout（用户能看见的话）；
* 沙盒里的数据文件（``data.csv`` / ``data/*.json`` / ``temp/*.json`` / ``temp/logs/*.jsonl``）。

隔离（不订场、不碰浏览器）
--------------------------
``--offline`` 让 TUI 不主动拉 Focusmate；PATH 里**故意不给 node**，于是
bridge / focusmate-mcp 一律瞬间失败（不是假装成功，是外部工具不存在）——
所以 ``rc`` 可能是 1，``temp/keep_session.log`` 里会有拉取错误，这些都是
「测试环境没有外部世界」的正常噪声。断言只看逻辑产物。

目标日默认 +10 天：Focusmate / 日历都够不到，``check`` 的执行区永远是空的。

用法
----
    python3 -B test/live/blackbox_kit.py                     # 造一份给你看
    python3 -B test/live/blackbox_kit.py --cmd "plan 10; complete"
    python3 -B test/live/blackbox_kit.py --from ~/bug-copy \\
        --cmd "day 0; plan 0; complete; refresh --check"      # 复刻 bug
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 目标日在几天后。+10 天：Focusmate/日历都还没覆盖，plan 出来的场次不会被执行区盯上。
DEFAULT_OFFSET = 10

#: 沙盒里唯一的一个时段（和 test/live/fixture.py 同一形状：150min → 5 count → 75+50）。
PERIOD = ("13:00", "15:30")

#: 没有 node 的 PATH：bridge / focusmate-mcp 会立刻失败，不会去开 Chromium。
NO_NODE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: 沙盒里造的三个项目：(标题, task_type, [(任务标题, count, duration)])
PROJECTS = (
    ("黑盒 A · 并行", "parallel", [("A · 写方案骨架", 2, 50)]),
    ("黑盒 B · 顺序", "sequential", [("B · 收集材料", 2, 50)]),
    ("黑盒 C · 小活", "parallel", [("C · 十分钟小活", 1, 10)]),
)

#: 第一轮分配：把 5 个 count 分给三个项目（C 的 10 分钟会叠进大场）。
ALLOC_ALL = ((1, 2), (2, 2), (3, 1))


def target_date(offset=DEFAULT_OFFSET):
    return datetime.date.today() + datetime.timedelta(days=offset)


# ---------------------------------------------------------------- 沙盒数据

def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def calendar_download(day):
    """一份只含目标日那个时段的 ``calendar-download.json``（形状照抄 session_plan）。"""
    start, end = PERIOD
    iso = day.isoformat()
    event = {
        "date": iso, "start": start, "end": end,
        "start_iso": "%sT%s:00" % (iso, start),
        "end_iso": "%sT%s:00" % (iso, end),
        "calendar": "空闲", "title": "黑盒时段", "all_day": False,
    }
    return {
        "version": 1,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "start_date": iso, "days": 1, "profiles": ["空闲"],
        "events": [event],
        "periods": [{
            "id": "T1", "date": iso, "start": start, "end": end,
            "type": "空闲", "name": "黑盒时段", "calendar": "空闲", "title": "黑盒时段",
            "start_iso": event["start_iso"], "end_iso": event["end_iso"],
        }],
    }


def projects_store(day):
    """3 个 pro + 3 个 SOP op，字段照抄真实 ``data/projects.json``。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pros, ops = [], []
    for idx, (title, task_type, tasks) in enumerate(PROJECTS, 1):
        pros.append({
            "id": idx, "title": title, "stage": "running",
            "file": None, "sop_op_id": idx, "incubator_op_id": None,
            "op_ids": [idx], "status": "active", "status_at": None,
            "status_reason": None, "stage_locked": False,
            "created_at": now, "updated_at": now, "soft_interrupts": [],
        })
        ops.append({
            "id": idx, "title": "%s · SOP" % title, "op_type": "SOP",
            "task_type": task_type, "pro_id": idx, "project_tag": "", "tags": [],
            "file": None, "omnifocus": None, "status": "active",
            "status_at": None, "status_reason": None,
            "imported_at": now, "synced_at": now,
            "tasks": [
                {
                    "pk": "bb_%d_%d" % (idx, n), "title": name,
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


PATHS_KEYS = ("root", "day", "temp", "data", "csv", "tasks", "projects", "archive",
              "settings", "project_sync", "calendar", "layouts", "extras", "rules",
              "mutes", "keytime_json", "keytime_state", "keytime_devices",
              "keytime_rules", "nostart", "postsession", "failed_actions",
              "of_outbox", "plan_pins", "plan_watchdog", "logs")


def _paths_for(root, day):
    temp = os.path.join(root, "temp")
    data = os.path.join(root, "data")
    return {
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
        "plan_pins": os.path.join(temp, "plan-pins.json"),
        "plan_watchdog": os.path.join(temp, "plan-watchdog.json"),
        "logs": os.path.join(temp, "logs"),
    }


def make_sandbox(root=None, offset=DEFAULT_OFFSET):
    """凭空造一份最小工作区（只有数据文件），返回 paths。"""
    day = target_date(offset)
    root = root or tempfile.mkdtemp(prefix="ti-blackbox-%s-" % day.isoformat())
    paths = _paths_for(root, day)
    os.makedirs(paths["temp"], exist_ok=True)
    os.makedirs(paths["data"], exist_ok=True)
    os.makedirs(paths["logs"], exist_ok=True)

    with open(paths["csv"], "w", encoding="utf-8") as f:
        f.write("# 黑盒沙盒 @ %s（目标日 %s，一行都没有，全交给 plan 从容量反推）\n"
                "# 格式：YYYY-MM-DD,HH:MM,duration\n"
                % (datetime.datetime.now().isoformat(timespec="seconds"),
                   day.isoformat()))
    _write_json(paths["tasks"], {"version": 1, "next_id": 1, "tasks": []})
    _write_json(paths["projects"], projects_store(day))
    _write_json(paths["calendar"], calendar_download(day))
    _write_json(paths["layouts"], {"version": 1, "days": {}})
    for key in ("extras", "rules", "mutes"):
        _write_json(paths[key], {"version": 1, "rows": []})
    _write_json(paths["keytime_json"],
                {"focus": {"enabled": False}, "notify": {"enabled": False}})
    _write_json(paths["project_sync"], {"version": 1, "projects": {}})
    _write_json(paths["archive"], {"version": 1, "records": []})
    settings = os.path.join(ROOT, "data", "settings.json")
    if os.path.exists(settings):
        shutil.copy2(settings, paths["settings"])   # 只读复制：阈值/显示偏好
    return paths


SNAPSHOT_FILES = (
    "data.csv",
    "data/tasks.json", "data/projects.json", "data/task-archive.json",
    "data/project-sync.json", "data/settings.json",
    "temp/calendar-download.json", "temp/session-layouts.json",
    "temp/session-extras.json", "temp/session-rules.json", "temp/session-mutes.json",
    "temp/keep_session-state.json", "temp/keep_session-verdicts.json",
    "temp/keep_session-expected.json", "temp/keep_session-nostart.json",
    "temp/keep_session-postsession.json", "temp/keep_session-relocations.json",
    "temp/plan-pins.json", "temp/plan-watchdog.json",
)


def snapshot_workspace(source, root=None):
    """把一份**真实工作区快照**复制成黑盒沙盒（bug 复现用）。

    ``source`` 可以是真的仓库目录，也可以是 ``cp -R`` 出来的副本；只读其中
    上面列出的数据文件，一个都不改。复现 bug 的标准动作：

        1. bug 发生时把整个工作区拷一份（或只拷上面这些文件）；
        2. ``python3 -B test/live/blackbox_kit.py --from <副本> \\
              --cmd "<出 bug 的那串命令>"``
        3. 能在黑盒里复现 → 再开始 debug；复现不了 → 说明 bug 依赖外部世界
           （真 API / 真桥），先补日志把那一层说清楚。
    """
    source = os.path.abspath(os.path.expanduser(source))
    if not os.path.isdir(source):
        raise SystemExit("--from 需要一个目录：%s" % source)
    day = _snapshot_day(source)
    root = root or tempfile.mkdtemp(prefix="ti-blackbox-repro-")
    paths = _paths_for(root, day)
    os.makedirs(paths["temp"], exist_ok=True)
    os.makedirs(paths["data"], exist_ok=True)
    os.makedirs(paths["logs"], exist_ok=True)
    for rel in SNAPSHOT_FILES:
        src = os.path.join(source, rel)
        dst = os.path.join(root, rel)
        if not os.path.exists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            continue
        shutil.copy2(src, dst)
    defaults = {
        "tasks": {"version": 1, "next_id": 1, "tasks": []},
        "projects": {"version": 1, "next_id": 10, "next_pro_id": 10,
                     "projects": [], "pros": []},
        "archive": {"version": 1, "records": []},
        "layouts": {"version": 1, "days": {}},
    }
    for key, data in defaults.items():
        if not os.path.exists(paths[key]):
            _write_json(paths[key], data)
    if not os.path.exists(paths["csv"]):
        open(paths["csv"], "w", encoding="utf-8").close()
    return paths


def _snapshot_day(source):
    """从 data.csv 里猜快照是哪一天：取最后一行能解析的日期；猜不到就今天。"""
    try:
        for line in open(os.path.join(source, "data.csv"), encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                return datetime.date.fromisoformat(line.split(",")[0])
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    return datetime.date.today()


# ---------------------------------------------------------------- 跑 warden

def env_for(paths, offset=DEFAULT_OFFSET, with_node=False, extra_env=None):
    """子进程的环境：所有读写都在沙盒里，且默认**没有外部世界**。"""
    env = {(k): v for k, v in os.environ.items()
           if not k.startswith(("TI_", "KEEP_SESSION_", "WARDEN_", "FOCUSMATE_"))}
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
        "TI_FAILED_ACTIONS": paths["failed_actions"],
        "TI_OMNIFOCUS_OUTBOX": paths["of_outbox"],
        "TI_PLAN_PINS": paths["plan_pins"],
        "TI_PLAN_WATCHDOG": paths["plan_watchdog"],
        "TI_LOG_DIR": paths["logs"],
        "TI_LOG_LEVEL": "debug",
        "TI_KEYTIME_JSON": paths["keytime_json"],
        "TI_KEYTIME_STATE": paths["keytime_state"],
        "TI_KEYTIME_DEVICES": paths["keytime_devices"],
        "TI_KEYTIME_RULES": paths["keytime_rules"],
        "TI_KEYTIME_DOWNLOAD": paths["calendar"],
        "TI_BOOK_WINDOW_HOURS": "24",      # 只订未来 24h；沙盒目标日在 +10 天 → 一场都不会订
        "KEEP_SESSION_IDLE_CHECK": "0",
        "FOCUSMATE_CONFIG_DIR": os.path.join(paths["temp"], "no-browser"),
        "PYTHONUNBUFFERED": "1",
    })
    if not with_node:
        env["PATH"] = NO_NODE_PATH
    env.update(extra_env or {})
    return env


def run(paths, cmds, timeout=180, with_node=False, extra_args=(), extra_env=None):
    """跑一条 ``warden.py --exec``；返回 ``{rc, out, secs, argv}``。

    每条命令一个**全新进程**——这正是黑盒要测的东西：状态只能靠落盘文件传递。
    """
    argv = [sys.executable, "-B", os.path.join(ROOT, "warden.py"),
            "--exec", "; ".join(cmds)] + list(extra_args)
    if not with_node:
        argv.append("--offline")
    t0 = time.time()
    proc = subprocess.run(argv, env=env_for(paths, with_node=with_node,
                                            extra_env=extra_env),
                          cwd=ROOT, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)
    return {"rc": proc.returncode, "out": proc.stdout or "",
            "secs": time.time() - t0, "argv": argv}


# ---------------------------------------------------------------- 读落盘结果

def csv_rows(paths, day=None):
    """``data.csv`` 里某一天的场次：``[(HH:MM, duration)]``，按行序。"""
    day = day or paths["day"]
    out = []
    try:
        for line in open(paths["csv"], encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3 or parts[0] != day.isoformat():
                continue
            try:
                out.append((parts[1], int(parts[2])))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def layouts(paths, day=None):
    """某一天的 layout：``{period_id: {"durations": [...], "source": ...}}``。"""
    day = day or paths["day"]
    store = _read_json(paths["layouts"], {}) or {}
    return ((store.get("layouts") or {}).get(day.isoformat())
            or (store.get("days") or {}).get(day.isoformat()) or {})


def plan_items(paths, day=None):
    """这一天所有 op 的 plan items：``[(op标题, pk, start, duration, task_id)]``。"""
    day = day or paths["day"]
    out = []
    store = _read_json(paths["projects"], {}) or {}
    for op in store.get("projects") or []:
        plan = (op.get("plans") or {}).get(day.isoformat()) or {}
        for item in plan.get("items") or []:
            out.append((op.get("title"), item.get("pk"), item.get("start"),
                        item.get("duration"), item.get("task_id")))
    return out


def plan_pins(paths, day=None):
    """任务级固定项台账（``temp/plan-pins.json``）里某一天的条目。"""
    day = day or paths["day"]
    store = _read_json(paths["plan_pins"], {}) or {}
    return list(store.get(day.isoformat()) or [])


def tasks(paths):
    store = _read_json(paths["tasks"], {}) or {}
    return list(store.get("tasks") or [])


def task_title(task):
    return str((task.get("fields") or {}).get("title") or task.get("title") or "")


def bound_tasks(paths, day=None):
    """挂在某一天的场次上的本地任务：``[(标题, start, duration)]``。"""
    day = day or paths["day"]
    out = []
    for task in tasks(paths):
        sess = task.get("session") or {}
        if sess.get("date") != day.isoformat():
            continue
        out.append((task_title(task), sess.get("start"), sess.get("duration")))
    return out


def log_events(paths, area=None, event=None):
    """沙盒 JSONL 日志里的事件（按时间序）。"""
    rows = []
    logdir = paths["logs"]
    try:
        names = sorted(os.listdir(logdir))
    except OSError:
        return rows
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        try:
            fh = open(os.path.join(logdir, name), encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if area and rec.get("area") != area:
                    continue
                if event and rec.get("event") != event:
                    continue
                rows.append(rec)
    return rows


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="纯黑盒跑 warden 子进程（不订场）")
    ap.add_argument("--from", dest="source", default=None,
                    help="从一份真实工作区快照复制数据（bug 复现）")
    ap.add_argument("--cmd", dest="cmds", default="",
                    help='用户命令，`;` 分隔，如 "plan 10; complete"')
    ap.add_argument("--offset", type=int, default=DEFAULT_OFFSET,
                    help="空造沙盒时目标日在几天后（默认 10）")
    ap.add_argument("--keep", action="store_true", help="跑完保留沙盒")
    ap.add_argument("--show", action="store_true", help="打印 csv / layout / plan items")
    ap.add_argument("--with-node", action="store_true",
                    help="保留真 PATH（会开 bridge，慢；仅在必须复现网络相关 bug 时用）")
    args = ap.parse_args(argv)

    paths = (snapshot_workspace(args.source) if args.source
             else make_sandbox(offset=args.offset))
    print("沙盒：%s\n目标日：%s" % (paths["root"], paths["day"].isoformat()))
    try:
        if args.cmds:
            res = run(paths, [s.strip() for s in args.cmds.split(";") if s.strip()],
                      with_node=args.with_node)
            print("\n── rc=%d %.1fs" % (res["rc"], res["secs"]))
            print(res["out"][-6000:])
        if args.show or not args.cmds:
            print("\ndata.csv：")
            for row in csv_rows(paths):
                print("  ", row)
            print("layouts：", json.dumps(layouts(paths), ensure_ascii=False))
            print("plan items：")
            for item in plan_items(paths):
                print("  ", item)
    finally:
        if not args.keep:
            shutil.rmtree(paths["root"], ignore_errors=True)
        else:
            print("\n（沙盒保留：%s）" % paths["root"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
