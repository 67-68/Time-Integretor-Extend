#!/usr/bin/env python3
"""project_store.py —— OmniFocus **项目**的本地账（项目库 ``data/projects.json``）。

任务库（``task_store.py``）里放的是「这一场要干的活」；项目库放的是**活的母体**：
一个 OmniFocus Project 连同它手底下全部的未完成任务（每条带 count）。两者不是
一回事，所以不塞进同一个文件：

```
data/tasks.json      这一场（或这一天）要干的活（一条 = 一场 session 上的活）
data/projects.json   项目母体：任务清单 + 每天分配了多少 count + 谁被唤起过
```

## 为什么项目要单独存一份任务清单

用户原话：*在项目入库的时候就应该获取他所有的 task，但不展示直到他们被 project
「唤起」*。于是导入时把清单**快照**下来（不建本地任务、不上任务面板），等到
``plan`` 给这个项目分了 count，才按清单顺序把任务「唤起」成当天的本地任务。
「快照」这个词是认真的：清单不会每次读盘都去问 OmniFocus（那要几秒、还可能没权限），
要新数据就 ``project <序号> refresh``。

## 一条项目长什么样

```json
{
  "id": 1,                                   // 项目序号（面板第几条；数据库内部主键）
  "title": "论文",
  "omnifocus": {"id": "p-abc", "url": "omnifocus:///project/p-abc", "title": "论文"},
  "status": "active",                        // active / dropped
  "imported_at": "…", "synced_at": "…",
  "tasks": [                                 // **全部**任务（含已完成的），按 OmniFocus 顺序
    {"pk": "t-1", "title": "写引言", "parent": "", "due": "…",
     "count": 3, "duration": 75, "completed": false, "blocked": false, "rank": 0}
  ],
  "plans": {
    "2026-11-04": {"count": 4, "at": "…",     // 用户分给这一天的资源（count）
                   "items": [{"pk": "t-1", "task_id": 12,
                              "count": 2, "start": "11:00", "duration": 50}]}
  }
}
```

``plans[day].items`` 是「这些 count 具体落到哪几个本地任务上」——唤起的证据。
``task_id`` 指回任务库，进度就是靠它算的（见 :func:`invested`）。

## 进度怎么算（只有一个事实来源）

| 数字 | 怎么来的 |
| --- | --- |
| 总 count | 清单里每条任务的 ``count`` 求和（``count`` 来自 OmniFocus 的 estimated minutes ÷ 25，往上取整） |
| 已投入 | 本地任务库那条链上 ``count_invested`` 求和（被 plan 唤起过的活真干了多少） |
| 已完成 | OmniFocus 那边 ``completed`` 的任务算满；其余按「已投入」封顶 |
| 剩余 | 总 − 已完成（**已投入但没做完的也会减掉**：干了一半就是干了一半） |

**没有第二本账**：项目库不记「我做掉了多少」，它只记清单和分配；进度由
``tasks.json`` 的 ``count_invested`` 现算。所以收尾里怎么记 count、这里就怎么显示，
永远不会对不上。

## 和重复任务的区别

重复任务（``repeat``）是「同一个任务每周再来一次」；项目是「一堆不同的任务，每天
按分到的 count 挑几条来干」。共同点是两者都**不直接占 session**：真正挂到 session
上的是它们生出来的当天子任务（重复）或当天唤起的任务（项目）。

这个模块不联网、不依赖 rich；warden / 测试只调这里。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile

import task_store as ts

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")

VERSION = 1

STATUS_ACTIVE = "active"
STATUS_DROPPED = "dropped"
STATUSES = (STATUS_ACTIVE, STATUS_DROPPED)

#: 唤起出来的本地任务用这几个 ``fields`` 键指回项目。字段名就是界面上显示的名字
#: （任务视图会把 fields 的每个键摊成一列），所以要能一眼看懂。
PROJECT_FIELD = "project"             # 项目名
PROJECT_ID_FIELD = "project_id"       # 项目序号
PROJECT_TASK_FIELD = "project_task"   # OmniFocus 那条任务的 primary key


def projects_path(path=None):
    return path or os.environ.get("TI_PROJECTS_FILE") or DEFAULT_PROJECTS_FILE


def project_url(pk):
    pk = str(pk or "").strip()
    return "omnifocus:///project/%s" % pk if pk else ""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _backup_before_write(path):
    """留一份上一版（``xxx.bak``）—— 项目库也是活数据，写坏了要能回去。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            shutil.copy2(path, path + ".bak")
    except OSError:
        pass


def blank():
    return {"version": VERSION, "next_id": 1, "projects": []}


def load(path=None):
    """读项目库；文件不在 / 坏 JSON 一律当空库（不炸，和任务库一个脾气）。"""
    path = projects_path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return blank()
    if not isinstance(raw, dict):
        return blank()
    return normalize(raw)


def save(store, path=None):
    path = projects_path(path)
    _backup_before_write(path)
    _atomic_write(path, store)
    return store


# ---------------------------------------------------------------- 规范化


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _one_line(text, limit=120):
    return " ".join(str(text or "").split())[:limit]


def _norm_omnifocus(raw):
    if not isinstance(raw, dict):
        return None
    pk = str(raw.get("id") or raw.get("pk") or "").strip()
    if not pk:
        return None
    return {"id": pk,
            "url": raw.get("url") or project_url(pk),
            "title": _one_line(raw.get("title") or raw.get("name"))}


def _norm_task(raw, rank=0):
    """一条项目任务。``count`` / ``duration`` 至少能推出一个来。"""
    if not isinstance(raw, dict):
        return None
    pk = str(raw.get("pk") or raw.get("id") or "").strip()
    if not pk:
        return None
    count = ts.parse_count(raw.get("count"))
    minutes = ts.parse_minutes(raw.get("duration"))
    if count is None and minutes is not None:
        count = ts.minutes_to_count(minutes)
    if minutes is None and count:
        minutes = ts.count_to_minutes(count)
    return {
        "pk": pk,
        "title": _one_line(raw.get("title") or raw.get("name")) or pk,
        "parent": _one_line(raw.get("parent")),
        "due": raw.get("due") or None,
        "count": int(count or 0),
        "duration": minutes,
        "completed": bool(raw.get("completed")),
        "blocked": bool(raw.get("blocked")),
        "rank": _as_int(raw.get("rank")) if raw.get("rank") is not None else int(rank),
    }


def _norm_plan(raw):
    if not isinstance(raw, dict):
        return None
    out = {"count": _as_int(raw.get("count")) or 0,
           "at": raw.get("at") or None,
           "items": []}
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pk") or "").strip()
        task_id = _as_int(item.get("task_id"))
        if not pk or task_id is None:
            continue
        out["items"].append({
            "pk": pk,
            "task_id": task_id,
            "count": _as_int(item.get("count")) or 0,
            "start": item.get("start") or "",
            "duration": _as_int(item.get("duration")) or 0,
        })
    return out


def _norm_project(raw):
    if not isinstance(raw, dict):
        return None
    ident = _as_int(raw.get("id"))
    if ident is None or ident <= 0:
        return None
    tasks = []
    for rank, item in enumerate(raw.get("tasks") or []):
        rec = _norm_task(item, rank=rank)
        if rec is not None:
            tasks.append(rec)
    plans = {}
    for day, plan in (raw.get("plans") or {}).items():
        rec = _norm_plan(plan)
        if rec is not None:
            plans[str(day)] = rec
    omnifocus = _norm_omnifocus(raw.get("omnifocus"))
    status = raw.get("status") if raw.get("status") in STATUSES else STATUS_ACTIVE
    return {
        "id": ident,
        "title": _one_line(raw.get("title") or (omnifocus or {}).get("title")) or ("项目 %d" % ident),
        "omnifocus": omnifocus,
        "status": status,
        "imported_at": raw.get("imported_at") or None,
        "synced_at": raw.get("synced_at") or None,
        "tasks": tasks,
        "plans": plans,
    }


def normalize(store):
    """把读回来的东西收拾干净（坏记录直接丢掉，不炸）。"""
    store = store if isinstance(store, dict) else {}
    projects = []
    for raw in store.get("projects") or []:
        rec = _norm_project(raw)
        if rec is not None:
            projects.append(rec)
    projects.sort(key=lambda p: p["id"])
    used = {p["id"] for p in projects}
    next_id = _as_int(store.get("next_id")) or 1
    while next_id in used:
        next_id += 1
    return {"version": VERSION, "next_id": max(1, next_id), "projects": projects}


# ---------------------------------------------------------------- 从 OmniFocus 造 payload


def project_payload(row, rows):
    """OmniFocus 的 ``project_root`` 行 + 全量行 → 项目 payload（纯函数）。

    返回的 dict 里 ``missing`` 是「**没有设持续时间**的未完成任务」，``tasks`` 是
    全部任务（含已完成的，界面算进度要用），``warnings`` 是归组时的疑点。

    归组按 ``project`` 字段（OmniFocus 的「所属项目名」）—— 任务可能嵌套，光看
    ``parent`` 只拿得到第一层。同名项目是唯一会串的地方，所以这里**明确报警**，
    不假装没这回事。
    """
    row = row or {}
    name = _one_line(row.get("name") or row.get("title"))
    pk = str(row.get("id") or "").strip()
    if not pk or not name:
        raise ValueError("这不是一个 OmniFocus 项目（没有 primary key 或名字）")
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    same_name = [r for r in rows
                 if r.get("kind") == "project_root" and _one_line(r.get("name")) == name]
    warnings = []
    if len(same_name) > 1:
        warnings.append("OmniFocus 里有 %d 个同名项目「%s」—— 任务清单按项目名归组，"
                        "同名的那几个会串在一起。" % (len(same_name), name))
    members, missing, done = [], [], 0
    for r in rows:
        if r.get("kind") == "project_root":
            continue
        if _one_line(r.get("project")) != name:
            continue
        if r.get("dropped"):
            continue
        title = _one_line(r.get("name")) or str(r.get("id") or "?")
        minutes = ts.parse_minutes(r.get("estimated_minutes"))
        completed = bool(r.get("completed"))
        if completed:
            done += 1
        elif not minutes:
            missing.append(title)
        members.append({
            "pk": str(r.get("id") or ""),
            "title": title,
            "parent": _one_line(r.get("parent")),
            "due": r.get("due") or None,
            "duration": minutes,
            "count": ts.minutes_to_count(minutes) if minutes else 0,
            "completed": completed,
            "blocked": bool(r.get("blocked")),
            "rank": len(members),
        })
    return {
        "kind": "project",
        "id": pk,
        "title": name,
        "url": project_url(pk),
        "tasks": members,
        "missing": missing,
        "done": done,
        "warnings": warnings,
    }


def format_missing(payload, limit=6):
    """「这些任务没设持续时间」那一句话（导入被拒时原样念给用户听）。"""
    missing = list((payload or {}).get("missing") or [])
    if not missing:
        return ""
    head = "、".join("「%s」" % t for t in missing[:limit])
    if len(missing) > limit:
        head += " 等 %d 条" % len(missing)
    return ("项目「%s」里这 %d 条未完成任务没有「持续时间」（OmniFocus 的 estimated "
            "duration）：%s。去 OmniFocus 里给它们设上时长再导入 —— 没有时长就算不出"
            "要占几个 count。" % ((payload or {}).get("title") or "?",
                                  len(missing), head))


# ---------------------------------------------------------------- 统计


def contribution(task):
    """一条本地任务替项目任务「干掉了多少 count」。

    - **完成**的：整条算数（它的 count 就是这场分给它的量，干完 = 那几块都干了）；
    - **进行中**的：只算已经记进 ``count_invested`` 的（干了一半就是一半）；
    - **放弃**的：0 —— 放掉的那部分活还在项目里等着。
    """
    cap = ts.task_count(task) or 0
    status = str(task.get("status") or ts.STATUS_ACTIVE)
    if status == ts.STATUS_COMPLETED:
        return cap or ts.task_count_invested(task)
    if status == ts.STATUS_DROPPED:
        return 0
    return ts.task_count_invested(task)


def invested(task_store, project=None):
    """``{项目任务 pk: 已投入 count}`` —— 由任务库里唤起过的任务现算。"""
    out = {}
    want_id = project.get("id") if project else None
    for task in (task_store.list() if task_store else []):
        fields = task.get("fields") or {}
        pk = str(fields.get(PROJECT_TASK_FIELD) or "").strip()
        if not pk:
            continue
        if want_id is not None:
            got_id = _as_int(fields.get(PROJECT_ID_FIELD))
            if got_id is not None and got_id != want_id:
                continue
        out[pk] = out.get(pk, 0) + contribution(task)
    return out


def invested_by_project(task_store, projects=None):
    """``{项目序号: {项目任务 pk: 已投入 count}}`` —— plan / 预览表格用的整份账。"""
    out = {}
    for task in (task_store.list() if task_store else []):
        fields = task.get("fields") or {}
        pk = str(fields.get(PROJECT_TASK_FIELD) or "").strip()
        if not pk:
            continue
        pid = _as_int(fields.get(PROJECT_ID_FIELD))
        if pid is None:
            continue
        mine = out.setdefault(pid, {})
        mine[pk] = mine.get(pk, 0) + contribution(task)
    return out


def completion_guard(task, store, task_store):
    """「这条 OmniFocus 任务整条都干完了吗？」→ ``(能不能勾完成, 一句人话)``。

    项目唤起出来的任务可能只覆盖了那条 OmniFocus 任务的**一部分** count（今天分 2、
    那条总共 4）。这时候把 OmniFocus 勾完成就等于把剩下的 2 个 count 抹掉 ——
    所以挡一下，并且说清楚还差多少。
    """
    fields = task.get("fields") or {}
    pk = str(fields.get(PROJECT_TASK_FIELD) or "").strip()
    if not pk:
        return True, ""
    pid = _as_int(fields.get(PROJECT_ID_FIELD))
    project = store.by_id(pid) if (store is not None and pid) else None
    if project is None:
        return True, ""
    total = 0
    for item in project.get("tasks") or []:
        if item.get("pk") == pk:
            total = int(item.get("count") or 0)
            break
    if total <= 0:
        return True, ""
    mine = invested_by_project(task_store).get(project["id"]) or {}
    got = min(total, int(mine.get(pk, 0) or 0))
    if got >= total:
        return True, ""
    return False, ("这是项目「%s」里的一条：OmniFocus 那条一共 %d count，本地累计才 "
                   "%d —— **先不勾 OmniFocus 完成**（剩下的 %d count 还在项目里）。"
                   % (project.get("title"), total, got, total - got))


def stats(project, task_store=None, invested_map=None):
    """一个项目的账：``total`` / ``done`` / ``remaining`` / ``progress``。

    - ``total``：清单里全部任务的 count 之和；
    - ``done``：OmniFocus 里已完成的任务算满，其余按「本地已投入」封顶；
    - ``remaining``：只数**还没完成**的任务（已投入的照样减掉）。
    """
    if invested_map is None:
        invested_map = invested(task_store, project)
    total = done = remaining = 0
    for item in project.get("tasks") or []:
        cap = int(item.get("count") or 0)
        total += cap
        if item.get("completed"):
            done += cap
            continue
        got = min(cap, int(invested_map.get(item["pk"], 0) or 0))
        done += got
        remaining += cap - got
    return {
        "total": total,
        "done": done,
        "remaining": remaining,
        "progress": (float(done) / total) if total else 0.0,
        "progress_text": "%d/%d" % (done, total) if total else "—",
    }


def remaining_tasks(project, invested_map=None):
    """还没做完、而且还有 count 可干的那些任务（按清单顺序）。"""
    invested_map = invested_map or {}
    out = []
    for item in project.get("tasks") or []:
        if item.get("completed"):
            continue
        cap = int(item.get("count") or 0)
        got = min(cap, int(invested_map.get(item["pk"], 0) or 0))
        if cap - got <= 0:
            continue
        out.append(dict(item, remaining=cap - got, invested=got))
    return out


def plan_of(project, day):
    """那一天分给这个项目的资源（``{"count": n, "items": [...]}`` 或 None）。"""
    if day is None:
        return None
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return (project.get("plans") or {}).get(key)


def planned_counts(project, day):
    """那一天**实际排下去**的 count（items 求和；「说好的 4」但只排了 2 时看这个）。"""
    plan = plan_of(project, day)
    if not plan:
        return 0
    return sum(int(item.get("count") or 0) for item in plan.get("items") or [])


# ---------------------------------------------------------------- 库


class ProjectStore(object):
    """项目库的读写口（每次操作都重新读盘：TUI 和 daemon 可能同时在写）。"""

    def __init__(self, path=None):
        self.path = projects_path(path)

    # -- 读 -----------------------------------------------------------
    def load(self):
        return load(self.path)

    def save(self, store):
        return save(store, self.path)

    def list(self, status=None):
        projects = self.load()["projects"]
        if status is None:
            return projects
        return [p for p in projects if p.get("status") == status]

    def active(self):
        return self.list(status=STATUS_ACTIVE)

    def by_id(self, project_id):
        try:
            ident = int(project_id)
        except (TypeError, ValueError):
            return None
        for project in self.load()["projects"]:
            if project["id"] == ident:
                return project
        return None

    def by_omnifocus(self, pk):
        pk = str(pk or "").strip()
        if not pk:
            return None
        for project in self.load()["projects"]:
            if ((project.get("omnifocus") or {}).get("id") or "") == pk:
                return project
        return None

    def number_of(self, project_id):
        """项目序号 = 面板（预览表格）上的第几条（1 起、连续、不含已下架的）。"""
        try:
            ident = int(project_id)
        except (TypeError, ValueError):
            return None
        for index, project in enumerate(self.active(), 1):
            if project["id"] == ident:
                return index
        return None

    def resolve(self, token):
        """手敲的那个数 → 项目记录；``id:3`` 走内部 id，越界就明说范围。"""
        raw = str(token or "").strip()
        if not raw:
            raise ValueError("要给项目序号（预览表格上的第几条）")
        projects = self.active()
        if not projects:
            raise ValueError("项目库是空的：先 `task import` 一个 OmniFocus 项目。")
        low = raw.lower()
        if low.startswith("id:"):
            hit = self.by_id(low[3:])
            if hit is None:
                raise ValueError("没有 id 为 %r 的项目" % raw[3:])
            return hit
        ident = _as_int(raw)
        if ident is not None and 1 <= ident <= len(projects):
            return projects[ident - 1]
        if ident is not None:
            hit = self.by_id(ident)
            if hit is not None:
                return hit
        raise ValueError("项目序号 %r 不在范围里：#1–#%d（`project` 看全部）"
                         % (raw, len(projects)))

    # -- 写 -----------------------------------------------------------
    def upsert(self, payload):
        """把一个项目 payload 写进库（**同一个 OmniFocus 项目只留一条**）。

        第二次导入同一个项目 = 刷新它的任务清单，返回 ``(project, created)``。
        """
        payload = payload or {}
        pk = str(payload.get("id") or "").strip()
        if not pk:
            raise ValueError("项目 payload 里没有 OmniFocus primary key")
        store = self.load()
        tasks = [rec for rec in (_norm_task(item, rank=rank)
                                 for rank, item in enumerate(payload.get("tasks") or []))
                 if rec is not None]
        stamp = _now()
        for project in store["projects"]:
            if ((project.get("omnifocus") or {}).get("id") or "") == pk:
                project["tasks"] = tasks
                project["title"] = _one_line(payload.get("title")) or project["title"]
                project["synced_at"] = stamp
                project["status"] = STATUS_ACTIVE
                self.save(store)
                return project, False
        nid = int(store.get("next_id") or 1)
        used = {p["id"] for p in store["projects"]}
        while nid in used:
            nid += 1
        project = {
            "id": nid,
            "title": _one_line(payload.get("title")) or ("项目 %s" % pk),
            "omnifocus": {"id": pk,
                          "url": payload.get("url") or project_url(pk),
                          "title": _one_line(payload.get("title"))},
            "status": STATUS_ACTIVE,
            "imported_at": stamp,
            "synced_at": stamp,
            "tasks": tasks,
            "plans": {},
        }
        store["projects"].append(project)
        store["projects"].sort(key=lambda p: p["id"])
        store["next_id"] = max(int(store.get("next_id") or 1), nid + 1)
        self.save(store)
        return project, True

    def set_tasks(self, project_id, tasks):
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            project["tasks"] = [rec for rec in
                                (_norm_task(t, rank=i) for i, t in enumerate(tasks or []))
                                if rec is not None]
            project["synced_at"] = _now()
            self.save(store)
            return project
        raise ValueError("没有项目 %s" % project_id)

    def remove(self, project_id):
        store = self.load()
        before = len(store["projects"])
        store["projects"] = [p for p in store["projects"] if p["id"] != int(project_id)]
        if len(store["projects"]) == before:
            raise ValueError("没有项目 %s" % project_id)
        self.save(store)
        return before - len(store["projects"])

    def set_allocation(self, project_id, day, count):
        """记下「这一天给这个项目多少 count」（**只记资源，不排任务**）。"""
        store = self.load()
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            plans = project.setdefault("plans", {})
            if not count:
                plans.pop(key, None)
            else:
                plan = plans.get(key) or {"items": []}
                plan["count"] = int(count)
                plan["at"] = _now()
                plans[key] = plan
            self.save(store)
            return project
        raise ValueError("没有项目 %s" % project_id)

    def record_items(self, project_id, day, items):
        """记下「这一天真的排了哪些本地任务」（唤起完成的证据）。"""
        store = self.load()
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            plans = project.setdefault("plans", {})
            plan = plans.get(key) or {"count": 0, "items": []}
            plan["items"] = [dict(item) for item in items or []]
            plan["at"] = _now()
            plans[key] = plan
            self.save(store)
            return project
        raise ValueError("没有项目 %s" % project_id)

    # -- 给界面用的行 -------------------------------------------------
    def preview(self, day=None, task_store=None):
        """项目预览表格的每一行（``task <序号> <count>`` 里的序号 = 这里的 #）。"""
        rows = []
        invested_all = invested_by_project(task_store) if task_store else {}
        for index, project in enumerate(self.active(), 1):
            mine = invested_all.get(project["id"]) or {}
            stat = stats(project, task_store, mine)
            plan = plan_of(project, day)
            rows.append({
                "number": index,
                "id": project["id"],
                "title": project["title"],
                "pk": (project.get("omnifocus") or {}).get("id") or "",
                "total": stat["total"],
                "done": stat["done"],
                "remaining": stat["remaining"],
                "progress": stat["progress"],
                "progress_text": stat["progress_text"],
                "allocated": int((plan or {}).get("count") or 0),
                "scheduled": planned_counts(project, day),
                "tasks_open": len(remaining_tasks(project, mine)),
                "project": project,
                "plan": plan,
            })
        return rows
