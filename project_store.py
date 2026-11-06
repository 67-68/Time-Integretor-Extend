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
| 总 count | 清单里每条有效 active / completed 叶子任务的 ``count`` 求和（``count`` 来自 OmniFocus 的 estimated minutes ÷ 25，往上取整） |
| 已投入 | 本地任务库那条链上 ``count_invested`` 求和（被 plan 唤起过的活真干了多少） |
| 已完成 | OmniFocus 那边有效状态为 ``completed`` 的任务算满；其余按「已投入」封顶 |
| 剩余 | 总 − 已完成（**已投入但没做完的也会减掉**：干了一半就是干了一半） |

父任务完成/放弃后，没有单独改状态的子任务继承父任务的有效状态；``dropped``
的任务不计入总账，也不进入 ``remaining_tasks`` / ``weekly_yield``。

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
STATUS_COMPLETED = "completed"
STATUS_DROPPED = "dropped"
#: 项目本身的 status（只有 active / dropped）；任务的有效状态还包含 completed。
STATUSES = (STATUS_ACTIVE, STATUS_DROPPED)

#: 唤起出来的本地任务用这几个 ``fields`` 键指回项目。字段名就是界面上显示的名字
#: （任务视图会把 fields 的每个键摊成一列），所以要能一眼看懂。
PROJECT_FIELD = "project"             # 项目名
PROJECT_ID_FIELD = "project_id"       # 项目序号
PROJECT_TASK_FIELD = "project_task"   # OmniFocus 那条任务的 primary key


def projects_path(path=None):
    return path or os.environ.get("TI_PROJECTS_FILE") or DEFAULT_PROJECTS_FILE


def sync_check_path(path=None):
    """每日项目对账的日期戳文件（跟项目库同目录，测试自动隔离）。"""
    if path:
        return path
    env = os.environ.get("TI_PROJECT_SYNC_FILE")
    if env:
        return env
    return os.path.join(os.path.dirname(projects_path()) or ".", "project-sync.json")


def last_sync_day(path=None):
    """上次每日项目对账的日期（``YYYY-MM-DD``）；没有/坏了返回空串。"""
    try:
        with open(sync_check_path(path), "r", encoding="utf-8") as f:
            raw = json.load(f)
        return str((raw or {}).get("last_day") or "") if isinstance(raw, dict) else ""
    except (OSError, ValueError):
        return ""


def mark_sync_day(day=None, path=None):
    """记下今天已经做过每日项目对账。``day`` 可以是 date 或 ISO 文本。"""
    text = (day.isoformat() if hasattr(day, "isoformat") else str(day or ""))
    if not text:
        text = datetime.date.today().isoformat()
    _atomic_write(sync_check_path(path),
                  {"last_day": text, "at": _now()})
    return text


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


# ---------------------------------------------------------------- 一周任务预算
#
# ``import project`` 不只看「现在能看到的未完成任务」：OmniFocus 里还有两类
# 任务不会以普通 active 行的身份出现在项目视图中 ——
#   * 被推迟（defer 在未来）但**没有放弃**的任务；
#   * 重复任务（``repetitionRule``），本周还没发生的 occurrence 现在根本不存在。
# 项目导入前要把这些人一周内会产生的任务一起数出来，并检查每一条都有持续时间。
# 周的边界固定为**周一 00:00 到下周一 00:00**，不随导入当天滚动。


def week_bounds(now=None):
    """当前自然周 → ``(周一 date, 下周一 date)``（左闭右开）。

    需求：无论导入时是周几，都按「从周一开始」的那一周算。
    """
    if isinstance(now, datetime.datetime):
        day = now.date()
    elif isinstance(now, datetime.date):
        day = now
    else:
        day = datetime.date.today()
    start = day - datetime.timedelta(days=day.weekday())
    return start, start + datetime.timedelta(days=7)


def _local_dt(raw):
    """ISO 时间文本 / datetime / date → naive datetime；认不出返回 None。"""
    if isinstance(raw, datetime.datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, datetime.date):
        return datetime.datetime.combine(raw, datetime.time(0, 0))
    return ts.parse_iso_datetime(raw)


def _task_map(tasks):
    """``pk -> task``；同一个 pk 只认第一条，避免坏数据把层级绕成环。"""
    out = {}
    for item in tasks or ():
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pk") or item.get("id") or "").strip()
        if pk and pk not in out:
            out[pk] = item
    return out


def child_map(tasks):
    """``parent_pk -> [直接子任务]``（只认在同一个项目清单里的父任务）。"""
    index = _task_map(tasks)
    out = {}
    for item in tasks or ():
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pk") or item.get("id") or "").strip()
        parent_pk = str(item.get("parent_id") or "").strip()
        if not pk or not parent_pk or parent_pk == pk or parent_pk not in index:
            continue
        out.setdefault(parent_pk, []).append(item)
    return out


def top_level_tasks(tasks):
    """项目清单里的顶层任务（子任务不在这里重复出现）。"""
    index = _task_map(tasks)
    out = []
    for item in tasks or ():
        if not isinstance(item, dict):
            continue
        parent_pk = str(item.get("parent_id") or "").strip()
        if parent_pk and parent_pk in index:
            continue
        out.append(item)
    return out


def effective_task_statuses(tasks):
    """``pk -> active / completed / dropped``，把父任务状态传给未单独改状态的子任务。

    OmniFocus 里父任务完成或放弃后，子任务有时仍会以 ``completed=False`` /
    ``dropped=False`` 的原始行返回；它的真实状态由父任务继承。这里统一算出有效
    状态，避免导入校验、周预算和 ``plan`` 把这种子任务当成还能干的活。

    规则：任务自己的 ``completed`` / ``dropped`` 是显式状态，优先保留；自己没有
    显式关闭时，继承最近一层父任务的有效状态。父任务不在清单里或层级成环时，
    按 active 处理，交给层级校验去报错。
    """
    index = _task_map(tasks)
    memo = {}

    def resolve(pk, trail):
        if pk in memo:
            return memo[pk]
        item = index.get(pk)
        if not item or pk in trail:
            return STATUS_ACTIVE
        # 显式状态优先：子任务被单独完成/放弃后，不再被父任务反向覆盖。
        if item.get("dropped"):
            status = STATUS_DROPPED
        elif item.get("completed"):
            status = STATUS_COMPLETED
        else:
            parent_pk = str(item.get("parent_id") or "").strip()
            parent = index.get(parent_pk)
            if not parent or parent_pk == pk:
                status = STATUS_ACTIVE
            else:
                status = resolve(parent_pk, trail | {pk})
        memo[pk] = status
        return status

    for pk in index:
        resolve(pk, set())
    return memo


def task_status(item, tasks=None, statuses=None):
    """一条任务的有效状态；不传清单时只看它自己的字段。"""
    if statuses is None:
        statuses = effective_task_statuses(tasks or [item or {}])
    key = str((item or {}).get("pk") or (item or {}).get("id") or "").strip()
    if key:
        return statuses.get(key, STATUS_ACTIVE)
    if (item or {}).get("dropped"):
        return STATUS_DROPPED
    if (item or {}).get("completed"):
        return STATUS_COMPLETED
    return STATUS_ACTIVE


def hierarchy_errors(tasks, only_active=False):
    """检查「最多父-子两层」；返回 ``[{"pk", "title", "depth", "chain"}]``。

    0 = 顶层，1 = 子任务；2 就是孙任务（三层），严格禁止。
    ``only_active=True`` 时只检查未完成、未放弃的任务；已完成/已放弃的旧层级
    不参与导入校验。
    """
    index = _task_map(tasks)
    statuses = effective_task_statuses(tasks) if only_active else {}
    errors = []
    for item in tasks or ():
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pk") or item.get("id") or "").strip()
        if only_active and statuses.get(pk, STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        chain = [pk]
        cursor = item
        seen = {pk}
        depth = 0
        while True:
            parent_pk = str((cursor or {}).get("parent_id") or "").strip()
            if not parent_pk or parent_pk not in index:
                break
            if parent_pk in seen:
                errors.append({"pk": pk,
                               "title": _one_line(item.get("title")) or pk,
                               "depth": 99,
                               "chain": chain + [parent_pk],
                               "reason": "父子关系成环"})
                break
            seen.add(parent_pk)
            chain.append(parent_pk)
            depth += 1
            cursor = index[parent_pk]
        else:  # pragma: no cover - while True 由 break 收口
            depth = 0
        if depth > 1 and not (errors and errors[-1].get("pk") == pk
                              and errors[-1].get("depth") == 99):
            errors.append({"pk": pk,
                           "title": _one_line(item.get("title")) or pk,
                           "depth": depth,
                           "chain": chain,
                           "reason": "超过父-子两层"})
    return errors


def _item_minutes(item):
    """一条项目任务的分钟数；父任务壳取子任务之和。"""
    try:
        minutes = ts.parse_minutes((item or {}).get("duration"))
    except Exception:                       # noqa: BLE001 - 坏记录当没有
        minutes = None
    return int(minutes or 0)


def _parent_duration(item, children):
    if not children:
        return _item_minutes(item)
    return sum(_item_minutes(child) for child in children)


def _norm_repeat_info(info):
    """把 ``repetition_info()`` / 已存 task.repeat 收敛成可保存的重复信息。"""
    if not isinstance(info, dict):
        return None
    rule = ts.normalize_rule(info.get("rule") or info.get("rule_string"))
    if not rule:
        return None
    out = {"rule": rule}
    for key in ("rule_anchor", "anchor", "next", "due", "planned", "defer",
                "method", "schedule_type", "anchor_date_key"):
        if info.get(key):
            out[key] = info[key]
    minutes = ts.parse_minutes(info.get("estimated_minutes"))
    if minutes is not None:
        out["estimated_minutes"] = minutes
    return out


def _task_repeat(item):
    return _norm_repeat_info((item or {}).get("repeat"))


def _repeat_dates(item, week_start, week_end, now=None):
    """一条重复任务本周会落下的日期（``set[date]``，可能为空）。

    OmniFocus 的 ``repetition_info`` 只给「下一次」和 RRULE，不给历史 occurrence。
    为了把本周已经过去的那几天也数进去，先把 DTSTART 按规则间隔退到周一之前，
    再用 dateutil 展开整周。WEEKLY 的 BYDAY / INTERVAL、DAILY、MONTHLY、
    YEARLY 都按原规则走。
    """
    rep = _task_repeat(item)
    if not rep:
        return set()
    rule = ts.normalize_rule(rep.get("rule"))
    if not rule:
        return set()
    anchor = _local_dt(rep.get("rule_anchor") or rep.get("anchor")
                       or rep.get("next") or rep.get("planned")
                       or rep.get("due") or (item or {}).get("defer")
                       or (item or {}).get("due") or (item or {}).get("planned"))
    if anchor is None:
        return set()
    # 规则里 UNTIL 可能已经过期：把范围放到整周再交给 dateutil 判断。
    start_dt = datetime.datetime.combine(week_start, datetime.time(0, 0))
    end_dt = datetime.datetime.combine(week_end, datetime.time(0, 0))
    try:
        from dateutil.relativedelta import relativedelta
        parts = {}
        for piece in rule.split(";"):
            if "=" in piece:
                key, value = piece.split("=", 1)
                parts[key.strip().upper()] = value.strip().upper()
        freq = parts.get("FREQ", "")
        try:
            interval = max(1, int(parts.get("INTERVAL") or 1))
        except ValueError:
            interval = 1
        if freq == "DAILY":
            step = datetime.timedelta(days=interval)
        elif freq == "WEEKLY":
            step = datetime.timedelta(weeks=interval)
        elif freq == "MONTHLY":
            step = relativedelta(months=interval)
        elif freq == "YEARLY":
            step = relativedelta(years=interval)
        elif freq == "HOURLY":
            step = datetime.timedelta(hours=interval)
        else:
            step = None
        if step is not None:
            guard = 0
            while anchor.date() >= week_start and guard < 1000:
                anchor = anchor - step
                guard += 1
    except Exception:                       # noqa: BLE001 - 退成「不快进」也能跑
        pass
    rr = ts.rule_objects(rule, anchor)
    if rr is None:
        nxt = _local_dt(rep.get("next"))
        if nxt is not None and week_start <= nxt.date() < week_end:
            return {nxt.date()}
        return set()
    try:
        found = rr.between(start_dt, end_dt, inc=True)
    except (ValueError, OverflowError):
        return set()
    return {dt.date() for dt in found if week_start <= dt.date() < week_end}


def _week_item_date(item, now_dt, week_start, week_end):
    """非重复任务在本周算不算一条 → ``(date, deferred)``；不算返回 ``(None, False)``。"""
    defer = _local_dt((item or {}).get("defer"))
    if defer is not None:
        if defer.date() >= week_end:
            return None, False
        if defer.date() > now_dt.date():
            return defer.date(), True
    # 没有 defer / defer 已经过去：这是一条当前就 active 的任务，本周算一条。
    day = max(week_start, now_dt.date())
    if day >= week_end:
        return None, False
    return day, False


#: 每日项目对账时决定「任务数据被修改」的字段。完成状态不在这里比较：
#: 本地完成会单向写回 OmniFocus，不能用还没写回的远端状态反过来判冲突。
DIFF_FIELDS = ("title", "duration", "defer", "due", "planned", "parent_id")


def diff_tasks(local_tasks, remote_tasks):
    """本地项目清单 vs OmniFocus 清单 → ``{"added", "removed", "modified"}``。

    - ``added``：远端有、本地没有；调用方应自动拉取；
    - ``removed``：本地有、远端没有；属于冲突，问用户保哪边；
    - ``modified``：两边都有但关键字段不同；属于冲突。
    """
    def index(tasks):
        out = {}
        for task in tasks or ():
            pk = str((task or {}).get("pk") or (task or {}).get("id") or "")
            if pk:
                out[pk] = task
        return out

    local = index(local_tasks)
    remote = index(remote_tasks)
    added = [remote[pk] for pk in remote if pk not in local]
    removed = [local[pk] for pk in local if pk not in remote]
    modified = []
    for pk in sorted(set(local) & set(remote)):
        changes = []
        for field in DIFF_FIELDS:
            left = local[pk].get(field)
            right = remote[pk].get(field)
            if field == "duration":
                left = ts.parse_minutes(left)
                right = ts.parse_minutes(right)
            if left != right:
                changes.append({"field": field, "local": left, "remote": right})
        if changes:
            modified.append({"pk": pk,
                             "title": remote[pk].get("title") or local[pk].get("title"),
                             "changes": changes})
    return {"added": added, "removed": removed, "modified": modified}


def diff_text(diff, limit=5):
    """一条冲突摘要（面板/消息用）。"""
    diff = diff or {}
    bits = []
    if diff.get("added"):
        bits.append("新增 %d" % len(diff["added"]))
    if diff.get("removed"):
        names = "、".join("「%s」" % item.get("title")
                          for item in diff["removed"][:limit])
        bits.append("删除 %d%s" % (len(diff["removed"]),
                                   ("（%s）" % names) if names else ""))
    if diff.get("modified"):
        names = "、".join("「%s」" % item.get("title")
                          for item in diff["modified"][:limit])
        bits.append("修改 %d%s" % (len(diff["modified"]),
                                   ("（%s）" % names) if names else ""))
    return " · ".join(bits) or "没有变化"


def weekly_yield(tasks, now=None):
    """一组项目任务 → 本周会产生的任务清单 + 时长缺口（纯函数）。

    计数口径：

    * 当前 active（未完成、未放弃）的普通任务：1 条；
    * 未来 defer 但落在本周的普通任务：1 条；
    * 有 ``repeat.rule`` 的任务：本周每个 RRULE occurrence 各 1 条；一个 occurrence
      同时被 defer 挡在后面时仍算重复任务，``deferred=True``；
    * 已完成 / 已放弃：0 条；
    * 重复任务本体本身**不额外**再加 1 —— 它本周的 occurrence 就是它产生的任务。

    ``tasks`` 可以来自 :func:`project_payload`，也可以是项目库里已经规范化过的
    task 记录。返回的 ``tasks`` 每一条都带 ``duration`` / ``missing_duration``；
    ``missing`` 按任务聚合成人话需要的字段。
    """
    if isinstance(now, datetime.datetime):
        now_dt = now.replace(tzinfo=None) if now.tzinfo else now
    elif isinstance(now, datetime.date):
        now_dt = datetime.datetime.combine(now, datetime.time(12, 0))
    else:
        now_dt = datetime.datetime.now()
    week_start, week_end = week_bounds(now_dt)
    produced, missing_by_pk = [], {}
    seen = set()
    all_tasks = [dict(item) for item in (tasks or ()) if isinstance(item, dict)]
    index = _task_map(all_tasks)
    kids = child_map(all_tasks)
    statuses = effective_task_statuses(all_tasks)
    for raw in all_tasks:
        item = raw if isinstance(raw, dict) else {}
        pk = str(item.get("pk") or item.get("id") or "")
        if statuses.get(pk, STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        # 子任务不单独计入「本周产生多少条」；父任务壳代表这一整套。
        parent_pk = str(item.get("parent_id") or "").strip()
        if parent_pk and parent_pk in index:
            continue
        title = _one_line(item.get("title") or item.get("name")) or pk or "?"
        children = kids.get(pk) or []
        if children:
            good_children = [child for child in children
                             if statuses.get(str(child.get("pk") or child.get("id") or ""),
                                             STATUS_ACTIVE) == STATUS_ACTIVE]
            missing_children = [child for child in good_children
                                if ts.parse_minutes(child.get("duration")) is None]
            minutes = (sum(_item_minutes(child) for child in good_children)
                       if good_children and not missing_children else None)
            if not good_children:
                continue
        else:
            missing_children = []
            minutes = ts.parse_minutes(item.get("duration"))
            if minutes is None:
                minutes = ts.parse_minutes(item.get("estimated_minutes"))
        repeat = _task_repeat(item)
        # repetition_info 里的时长也可以补 row 里没写出来的情况。
        if minutes is None and repeat:
            minutes = ts.parse_minutes(repeat.get("estimated_minutes"))
        defer = _local_dt(item.get("defer") or (repeat or {}).get("defer"))
        dates = []
        if repeat:
            dates = sorted(_repeat_dates(item, week_start, week_end, now=now_dt))
            # defer 在未来时，未来的重复 occurrence 还没开放；早于 defer 那天的
            # 不算「本周会产生」。
            if defer is not None and defer > now_dt:
                dates = [d for d in dates if d >= defer.date()]
        else:
            day, deferred = _week_item_date(item, now_dt, week_start, week_end)
            if day is not None:
                dates = [day]
        for day in dates:
            key = (pk, day.isoformat())
            if key in seen:
                continue
            seen.add(key)
            is_deferred = bool(defer is not None and defer > now_dt
                               and day >= defer.date())
            produced.append({
                "pk": pk,
                "title": title,
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "duration": int(minutes or 0),
                "count": ts.minutes_to_count(minutes) if minutes else 0,
                "missing_duration": minutes is None,
                "repeat": bool(repeat),
                "deferred": is_deferred,
            })
            if minutes is None:
                rec = missing_by_pk.setdefault(pk or title, {
                    "pk": pk,
                    "title": title,
                    "dates": [],
                    "count": 0,
                    "repeat": bool(repeat),
                    "deferred": bool(is_deferred),
                    "children": [_one_line(c.get("title")) or str(c.get("pk") or "")
                                 for c in missing_children],
                })
                rec["dates"].append(day.isoformat())
                rec["count"] += 1
    produced.sort(key=lambda item: (item.get("date") or "", item.get("pk") or ""))
    repeated = [item for item in produced if item.get("repeat")]
    deferred_only = [item for item in produced
                     if item.get("deferred") and not item.get("repeat")]
    active = [item for item in produced
              if not item.get("repeat") and not item.get("deferred")]
    return {
        "start": week_start.isoformat(),
        "end": (week_end - datetime.timedelta(days=1)).isoformat(),
        "count": len(produced),
        "active": len(active),
        "deferred": len(deferred_only),
        "repeated": len(repeated),
        "repeated_deferred": len([item for item in repeated
                                  if item.get("deferred")]),
        "tasks": produced,
        "missing": list(missing_by_pk.values()),
    }


def week_summary(payload_or_week):
    """一句人话：``本周（11-02–11-08）会产生 5 条任务：…``。"""
    week = ((payload_or_week or {}).get("week")
            if isinstance(payload_or_week, dict) and "week" in payload_or_week
            else payload_or_week)
    if not isinstance(week, dict):
        return ""
    bits = ["当前 active %d" % int(week.get("active") or 0),
            "本周开放的被推迟 %d" % int(week.get("deferred") or 0),
            "重复任务 %d" % int(week.get("repeated") or 0)]
    if week.get("repeated_deferred"):
        bits.append("其中重复且被推迟 %d" % int(week["repeated_deferred"]))
    return ("本周（%s–%s，周一开始）会产生 %d 条任务：%s"
            % (week.get("start") or "?", week.get("end") or "?",
               int(week.get("count") or 0), " · ".join(bits)))


def format_depth_errors(payload, limit=6):
    """父子层级超过两层的报错句；没有错误返回空串。"""
    errors = list((payload or {}).get("depth_errors") or [])
    if not errors:
        return ""
    bits = []
    for rec in errors[:limit]:
        chain = rec.get("chain") or []
        bits.append("「%s」%s" % (rec.get("title") or rec.get("pk") or "?",
                                  ("（层级：%s）" % " → ".join(chain)) if chain else ""))
    more = (" 等 %d 条" % len(errors)) if len(errors) > limit else ""
    return ("项目「%s」里这 %d 条任务超过父-子两层（严禁三层任务）：%s%s。"
            "请回 OmniFocus 把孙任务上移或拆成独立的父任务，再重新导入。"
            % ((payload or {}).get("title") or "?", len(errors),
               "、".join(bits), more))


def format_week_missing(payload, limit=6):
    """本周任务缺时长的报错句；没有缺口返回空串。"""
    week = (payload or {}).get("week") or {}
    missing = list(week.get("missing") or [])
    if not missing:
        # 兼容老的 payload（没有 week 字段）
        names = list((payload or {}).get("missing") or [])
        if not names:
            return ""
        head = "、".join("「%s」" % t for t in names[:limit])
        if len(names) > limit:
            head += " 等 %d 条" % len(names)
        return ("项目「%s」里这 %d 条未完成任务没有「持续时间」：%s。去 OmniFocus "
                "里给它们设上时长再导入。"
                % ((payload or {}).get("title") or "?", len(names), head))
    bits = []
    for rec in missing[:limit]:
        dates = "、".join(str(d)[5:] for d in (rec.get("dates") or []))
        detail = ""
        if rec.get("children"):
            detail = "（子任务：%s）" % "、".join(rec["children"])
        bits.append("「%s」%s%s" % (rec.get("title") or rec.get("pk") or "?",
                                   ("（%s）" % dates) if dates else "", detail))
    head = "、".join(bits)
    total = sum(int(rec.get("count") or 0) for rec in missing)
    if len(missing) > limit:
        head += " 等 %d 条" % len(missing)
    return ("项目「%s」本周会产生 %d 条任务，其中 %d 条没有「持续时间」"
            "（OmniFocus 的 estimated duration）：%s。去 OmniFocus 里给它们设上"
            "时长再导入 —— 没有时长就算不出要占几个 count。"
            % ((payload or {}).get("title") or "?",
               int(week.get("count") or total), total, head))


def attach_week(payload, repeat_infos=None, now=None):
    """把重复规则合并进项目 payload，并算出 ``payload["week"]``。

    ``repeat_infos`` 是 ``{task_pk: repetition_info()}``；这也是 OmniFocus 里
    “project 直接看不到、但没放弃”的重复任务的信息来源。
    """
    payload = dict(payload or {})
    infos = repeat_infos or {}
    tasks = []
    for raw in payload.get("tasks") or ():
        item = dict(raw or {})
        rep = _norm_repeat_info(infos.get(str(item.get("pk") or "")))
        if rep:
            item["repeat"] = rep
        tasks.append(item)
    payload["tasks"] = tasks
    week = weekly_yield(tasks, now=now)
    payload["week"] = week
    payload["week_count"] = week["count"]
    payload["week_missing"] = week["missing"]
    return payload


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
    out = {
        "pk": pk,
        "title": _one_line(raw.get("title") or raw.get("name")) or pk,
        "parent": _one_line(raw.get("parent")),
        "parent_id": str(raw.get("parent_id") or "").strip(),
        "due": raw.get("due") or None,
        "defer": raw.get("defer") or None,
        "planned": raw.get("planned") or None,
        "note": str(raw.get("note") or ""),
        "count": int(count or 0),
        "duration": minutes,
        "completed": bool(raw.get("completed")),
        "dropped": bool(raw.get("dropped")),
        "blocked": bool(raw.get("blocked")),
        "rank": _as_int(raw.get("rank")) if raw.get("rank") is not None else int(rank),
    }
    repeat = _norm_repeat_info(raw.get("repeat"))
    if repeat:
        out["repeat"] = repeat
    return out


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


def project_payload(row, rows, repeat_infos=None, now=None):
    """OmniFocus 的 ``project_root`` 行 + 全量行 → 项目 payload（纯函数）。

    ``tasks`` 是全部任务（含已完成、已放弃的，界面算进度和层级继承要用）；
    ``missing`` 是**全部有效状态为 active 的任务**里没设持续时间的标题；``week``
    是本周（周一起）会产生的任务，包含当前 active、被推迟到本周开放的、以及重复
    任务本周的 occurrence。父任务完成/放弃时，没有单独改状态的子任务继承同样
    状态，不会被当成可用任务，也不参与时长/层级校验。

    ``repeat_infos`` 是 ``{task_pk: repetition_info()}``。重复规则不在
    ``omnifocus_tasks.py list`` 的字段里，所以由调用方查好传进来；纯函数本身不
    联网。``now`` 可注入测试，默认当前时间。

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
    infos = repeat_infos or {}
    same_name = [r for r in rows
                 if r.get("kind") == "project_root" and _one_line(r.get("name")) == name]
    rows_with_project_id = [r for r in rows
                            if r.get("kind") != "project_root"
                            and str(r.get("project_id") or "").strip()]
    warnings = []
    if len(same_name) > 1 and not rows_with_project_id:
        warnings.append("OmniFocus 里有 %d 个同名项目「%s」—— 这份清单没有 project_id，"
                        "只能按项目名归组，同名项目会串在一起。" % (len(same_name), name))
    project_rows = []
    for r in rows:
        if r.get("kind") == "project_root":
            continue
        remote_project_id = str(r.get("project_id") or "").strip()
        if remote_project_id:
            # 身份只认 OmniFocus 项目 primary key；同名项目不会再串。
            if remote_project_id != pk:
                continue
        elif _one_line(r.get("project")) != name:
            continue
        project_rows.append(r)

    # 先按完整清单（包含已放弃的父任务）算有效状态，再落成成员记录。
    statuses = effective_task_statuses(project_rows)
    members, done = [], 0
    for r in project_rows:
        raw_pk = str(r.get("id") or "").strip()
        status = statuses.get(raw_pk, STATUS_ACTIVE)
        completed = status == STATUS_COMPLETED
        dropped = status == STATUS_DROPPED
        if completed:
            done += 1
        title = _one_line(r.get("name")) or raw_pk or "?"
        minutes = ts.parse_minutes(r.get("estimated_minutes"))
        item = {
            "pk": raw_pk,
            "title": title,
            "parent": _one_line(r.get("parent")),
            "parent_id": str(r.get("parent_id") or "").strip(),
            "due": r.get("due") or None,
            "defer": r.get("defer") or None,
            "planned": r.get("planned") or None,
            "note": r.get("note") or "",
            "duration": minutes,
            "count": ts.minutes_to_count(minutes) if minutes else 0,
            "completed": completed,
            "dropped": dropped,
            "blocked": bool(r.get("blocked")),
            "rank": len(members),
        }
        rep = _norm_repeat_info(infos.get(item["pk"]))
        if rep:
            item["repeat"] = rep
        members.append(item)

    index = _task_map(members)
    kids = child_map(members)
    for item in members:
        children = kids.get(item["pk"]) or []
        if children:
            item["shell"] = True
            item["child_pks"] = [child["pk"] for child in children]
            item["duration"] = _parent_duration(item, children)
            item["count"] = ts.minutes_to_count(item["duration"]) if item["duration"] else 0
    # 父任务本身是空壳：有子任务时不要求它自己有 estimated_minutes；缺时长的是
    # 具体子任务。没有子任务的普通任务仍按原来的硬门槛检查。
    statuses = effective_task_statuses(members)
    missing = []
    for item in members:
        if statuses.get(item["pk"], STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        if kids.get(item["pk"]):
            continue
        if ts.parse_minutes(item.get("duration")) is None:
            missing.append(item.get("title") or item.get("pk"))
    depth_errors = hierarchy_errors(members, only_active=True)
    if depth_errors:
        warnings.append("有 %d 条任务超过父-子两层（严禁三层）：%s"
                        % (len(depth_errors),
                           "、".join("「%s」" % item.get("title")
                                    for item in depth_errors[:6])))
    # 重复任务本身是父任务时，重复规则仍在父任务上；weekly_yield 会按整套算一条。
    payload = {
        "kind": "project",
        "id": pk,
        "title": name,
        "url": project_url(pk),
        "tasks": members,
        "missing": missing,
        "depth_errors": depth_errors,
        "done": done,
        "warnings": warnings,
    }
    # 重复规则已经全部在 members 里了，这里只负责算本周账。
    return attach_week(payload, repeat_infos={}, now=now)


def format_missing(payload, limit=6):
    """「这些任务没设持续时间」那一句话（导入被拒时原样念给用户听）。"""
    week = (payload or {}).get("week")
    if isinstance(week, dict) and (week.get("missing") or []):
        return format_week_missing(payload, limit=limit)
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

    父任务壳的 ``duration/count`` 由子任务派生，不能和子任务重复计账：
    ``total/done/remaining`` 只逐条算叶子任务。父任务本身已完成的，它的子任务
    在 OmniFocus 里通常也已完成，仍按叶子算，不会把父壳再加一遍；继承后为
    dropped 的子树整棵不计账。
    """
    if invested_map is None:
        invested_map = invested(task_store, project)
    tasks = project.get("tasks") or []
    kids = child_map(tasks)
    statuses = effective_task_statuses(tasks)
    total = done = remaining = 0
    for item in tasks:
        if kids.get(item.get("pk")):
            continue
        pk = str(item.get("pk") or "")
        status = statuses.get(pk, STATUS_ACTIVE)
        if status == STATUS_DROPPED:
            continue
        minutes = _item_minutes(item)
        cap = int(item.get("count") or 0)
        if not cap and minutes:
            cap = ts.minutes_to_count(minutes)
        total += cap
        if status == STATUS_COMPLETED:
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
    """还没做完、而且还有内容可干的**顶层任务**（按清单顺序）。

    有子任务的父任务返回成一条派生单元：``duration`` 是剩余子任务分钟数之和，
    ``count`` 是它的 ceil(minutes/25)，``children`` 里是每个剩余子任务（子任务
    不可再拆）。没有子任务的普通任务行为和以前一样。
    """
    invested_map = invested_map or {}
    tasks = project.get("tasks") or []
    kids = child_map(tasks)
    statuses = effective_task_statuses(tasks)
    out = []
    for item in top_level_tasks(tasks):
        pk = str(item.get("pk") or "")
        if statuses.get(pk, STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        children = kids.get(item.get("pk")) or []
        if children:
            open_children = []
            for child in children:
                child_pk = str(child.get("pk") or "")
                if statuses.get(child_pk, STATUS_ACTIVE) != STATUS_ACTIVE:
                    continue
                minutes = _item_minutes(child)
                if minutes <= 0:
                    continue
                open_children.append(dict(child, remaining=minutes,
                                          invested=0))
            if not open_children:
                continue
            total_minutes = sum(_item_minutes(child) for child in open_children)
            cap = ts.minutes_to_count(total_minutes) or 0
            out.append(dict(item, remaining=cap, invested=0,
                            duration=total_minutes, count=cap,
                            shell=True, children=open_children,
                            composite=True))
            continue
        cap = int(item.get("count") or 0)
        got = min(cap, int(invested_map.get(item.get("pk"), 0) or 0))
        if cap - got <= 0:
            continue
        out.append(dict(item, remaining=cap - got, invested=got))
    return out


def task_by_pk(project, pk):
    """项目清单里按 primary key 找一条任务；没有返回 None。"""
    want = str(pk or "").strip()
    if not want:
        return None
    for item in (project or {}).get("tasks") or ():
        if str(item.get("pk") or "") == want:
            return item
    return None


def parent_of(project, child_pk):
    """子任务 → 直接父任务；不是子任务 / 父不在清单里返回 None。"""
    child = task_by_pk(project, child_pk)
    parent_pk = str((child or {}).get("parent_id") or "").strip()
    return task_by_pk(project, parent_pk) if parent_pk else None


def children_of(project, parent_pk):
    """项目清单里某个父任务的直接子任务（按 rank 顺序）。"""
    want = str(parent_pk or "").strip()
    if not want:
        return []
    return [item for item in (project or {}).get("tasks") or ()
            if str(item.get("parent_id") or "").strip() == want]


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

    def find(self, token):
        """把设置里的值解析成一个 active 项目；认 #序号 / id:N / OF pk / 项目名。

        和 :meth:`resolve` 的区别是它也认项目名，并且把「找不到 / 重名」说成
        一句适合显示在警告板里的话。设置 ``chore-project`` 走这里。
        """
        raw = str(token or "").strip()
        if not raw:
            raise ValueError("没填项目")
        low = raw.lower()
        if low.startswith("#"):
            raw = raw[1:].strip()
            low = raw.lower()
        if low.startswith("id:") or raw.isdigit():
            return self.resolve(raw)
        projects = self.active()
        for project in projects:
            if str((project.get("omnifocus") or {}).get("id") or "") == raw:
                return project
        exact = [p for p in projects
                 if str(p.get("title") or "").strip().casefold() == raw.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            nums = "、".join("#%d" % (self.number_of(p["id"]) or p["id"])
                             for p in exact)
            raise ValueError("项目名「%s」有 %d 个同名项目（%s）；请改用 #序号"
                             "（project 先看列表）" % (raw, len(exact), nums))
        fuzzy = [p for p in projects
                 if raw.casefold() in str(p.get("title") or "").casefold()]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise ValueError("「%s」匹配到多个项目；请改用 #序号" % raw)
        raise ValueError("项目库里找不到「%s」；先 `project` 看序号或 "
                         "`task import` 导入项目" % raw)

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

    def complete_subtasks(self, project_id, parent_pk, child_pks):
        """把这次排进 session 的子任务标完成；全部完成 → 父任务自动完成。

        返回 ``(project, parent_completed_now, newly_completed_children)``。
        父任务本身是空壳，不参与 session；这里只更新项目库里的完成状态。
        外部 OmniFocus 写回由 warden 决定（普通 ``task done`` 会立刻写，纯本地
        ``-m complete`` 不写）。
        """
        wanted = [str(pk) for pk in (child_pks or []) if str(pk).strip()]
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            direct = {str(item.get("pk")) for item in children_of(project, parent_pk)}
            newly = []
            for item in project.get("tasks") or []:
                pk = str(item.get("pk") or "")
                if pk in wanted and pk in direct and not item.get("completed"):
                    item["completed"] = True
                    item["completed_at"] = _now()
                    newly.append(pk)
            parent = task_by_pk(project, parent_pk)
            all_done = bool(direct) and all(
                bool(task_by_pk(project, pk).get("completed"))
                for pk in direct if task_by_pk(project, pk) is not None)
            auto_done = False
            if parent is not None and all_done and not parent.get("completed"):
                parent["completed"] = True
                parent["completed_at"] = _now()
                auto_done = True
            if newly or auto_done:
                project["synced_at"] = _now()
                self.save(store)
            return project, auto_done, newly
        raise ValueError("没有项目 %s" % project_id)

    def complete_task(self, project_id, task_pk):
        """把项目清单里的一条任务标完成；若是子任务，全部兄弟完成则父壳自动完成。

        返回 ``(project, task, auto_parent)``。``auto_parent`` 是这次被顺带
        完成的父任务（没有就是 None）。只改本地项目库；OmniFocus 写回由
        warden 决定（和普通任务完成同一条单向联动规则）。
        """
        want = str(task_pk or "").strip()
        if not want:
            raise ValueError("没给项目任务 pk")
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            task = task_by_pk(project, want)
            if task is None:
                raise ValueError("项目「%s」里没有任务 %s"
                                 % (project.get("title") or project_id, want))
            newly = not bool(task.get("completed"))
            if newly:
                task["completed"] = True
                task["completed_at"] = _now()
            auto_parent = None
            parent_pk = str(task.get("parent_id") or "").strip()
            if parent_pk:
                parent = task_by_pk(project, parent_pk)
                direct = children_of(project, parent_pk)
                all_done = bool(direct) and all(
                    bool(child.get("completed")) for child in direct)
                if parent is not None and all_done and not parent.get("completed"):
                    parent["completed"] = True
                    parent["completed_at"] = _now()
                    auto_parent = parent
            if newly or auto_parent is not None:
                project["synced_at"] = _now()
                self.save(store)
            return project, task, auto_parent
        raise ValueError("没有项目 %s" % project_id)

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
            week = weekly_yield(project.get("tasks") or [])
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
                "week": week,
                "week_count": int(week.get("count") or 0),
                "week_missing": week.get("missing") or [],
                "project": project,
                "plan": plan,
            })
        return rows
