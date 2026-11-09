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
  "tasks": [                                 // **全部**任务（含已完成/已放弃），按 OmniFocus 顺序
    {"pk": "t-1", "title": "写引言", "parent": "", "due": "…",
     "count": 3, "duration": 75, "completed": false, "dropped": false,
     "effective_completed": false, "effective_dropped": false,
     "blocked": false, "rank": 0}
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

#: OmniFocus Project (op) 的两种类型：普通 SOP 与瓶颈孵化 incubator。
OP_TYPE_SOP = "SOP"
OP_TYPE_INCUBATOR = "incubator"
OP_TYPES = (OP_TYPE_SOP, OP_TYPE_INCUBATOR)

#: 真正项目 Project (pro) 的两个阶段：running 允许 SOP op，bottleneck 允许 incubator op。
STAGE_RUNNING = "running"
STAGE_BOTTLENECK = "bottleneck"
PRO_STAGES = (STAGE_RUNNING, STAGE_BOTTLENECK)

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
    return {"version": VERSION, "next_id": 1, "projects": [],
            "next_pro_id": 1, "pros": []}


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


def normalize_op_type(raw):
    """把用户输入或已存的 op 类型归一成 ``"SOP"`` / ``"incubator"`` / ``None``。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in ("sop", "normal", "standard", "普通", "常规"):
        return OP_TYPE_SOP
    if text in ("incubator", "inc", "孵化", "孵化器"):
        return OP_TYPE_INCUBATOR
    return None


def normalize_pro_stage(raw):
    """把项目阶段归一成 ``"running"`` / ``"bottleneck"`` / ``None``。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in ("bottleneck", "bottle", "bn", "瓶颈", "瓶颈期"):
        return STAGE_BOTTLENECK
    if text in ("running", "run", "normal", "active", "进行中", "运行", "运行中", "正常"):
        return STAGE_RUNNING
    return None


def extract_project_tags(tags):
    """从标签列表提取 ``project/xxx`` 关联的真正项目名（保留顺序、去重）。"""
    out = []
    seen = set()
    for raw in tags or ():
        text = str(raw or "").strip()
        if not text:
            continue
        parts = [p.strip() for p in text.split("/") if p.strip()]
        target = ""
        for idx, part in enumerate(parts):
            if part.lower() == "project" and idx + 1 < len(parts):
                target = "/".join(parts[idx + 1:]).strip()
                break
        if not target and text.lower().startswith("project:"):
            target = text.split(":", 1)[1].strip()
        if target and target.casefold() not in seen:
            seen.add(target.casefold())
            out.append(target)
    return out


def extract_project_tag(tags):
    """提取第一个 ``project/xxx`` 标签对应的真正项目名；没有返回空串。"""
    found = extract_project_tags(tags)
    return found[0] if found else ""


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
        # OmniFocus 的 taskStatus / effective* 标记优先；旧清单没有这些字段时，
        # 再看本地 completed/dropped，最后才走父任务继承。
        if item.get("effective_dropped") or item.get("dropped"):
            status = STATUS_DROPPED
        elif item.get("effective_completed") or item.get("completed"):
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
        "effective_completed": bool(raw.get("effective_completed",
                                           raw.get("completed"))),
        "effective_dropped": bool(raw.get("effective_dropped",
                                          raw.get("dropped"))),
        "blocked": bool(raw.get("blocked")),
        "local_only": bool(raw.get("local_only")),
        "rank": _as_int(raw.get("rank")) if raw.get("rank") is not None else int(rank),
    }
    if raw.get("tags"):
        out["tags"] = [str(x).strip() for x in (raw.get("tags") or ())
                       if str(x).strip()]
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
    op_type = normalize_op_type(raw.get("op_type") or raw.get("type"))
    tags = []
    for tag in raw.get("tags") or ():
        t = str(tag).strip()
        if t and t not in tags:
            tags.append(t)
    project_tag = _one_line(raw.get("project_tag")) or extract_project_tag(tags)
    if not project_tag:
        task_tags = []
        for task in tasks:
            if not task.get("completed") and not task.get("dropped"):
                task_tags.extend(task.get("tags") or ())
        project_tag = extract_project_tag(task_tags)
    pro_id = _as_int(raw.get("pro_id"))
    file_path = str(raw.get("file") or "").strip() or None
    out = {
        "id": ident,
        "title": _one_line(raw.get("title") or (omnifocus or {}).get("title")) or ("项目 %d" % ident),
        "op_type": op_type,
        "pro_id": pro_id,
        "project_tag": project_tag or "",
        "tags": tags,
        "file": file_path,
        "omnifocus": omnifocus,
        "status": status,
        "imported_at": raw.get("imported_at") or None,
        "synced_at": raw.get("synced_at") or None,
        "tasks": tasks,
        "plans": plans,
    }
    return out


def _norm_pro(raw):
    """一条真正项目 Project（``pro``）记录。"""
    if not isinstance(raw, dict):
        return None
    ident = _as_int(raw.get("id"))
    if ident is None or ident <= 0:
        return None
    title = _one_line(raw.get("title") or raw.get("name")) or ("项目 %d" % ident)
    stage = normalize_pro_stage(raw.get("stage")) or STAGE_RUNNING
    file_path = str(raw.get("file") or "").strip() or None
    sop_op_id = _as_int(raw.get("sop_op_id"))
    incubator_op_id = _as_int(raw.get("incubator_op_id"))
    op_ids = []
    for oid in raw.get("op_ids") or ():
        val = _as_int(oid)
        if val is not None and val > 0 and val not in op_ids:
            op_ids.append(val)
    for oid in (sop_op_id, incubator_op_id):
        if oid is not None and oid > 0 and oid not in op_ids:
            op_ids.append(oid)
    status = raw.get("status") if raw.get("status") in STATUSES else STATUS_ACTIVE
    return {
        "id": ident,
        "title": title,
        "stage": stage,
        "file": file_path,
        "sop_op_id": sop_op_id,
        "incubator_op_id": incubator_op_id,
        "op_ids": op_ids,
        "status": status,
        "created_at": raw.get("created_at") or None,
        "updated_at": raw.get("updated_at") or None,
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
    pros = []
    for raw in store.get("pros") or []:
        rec = _norm_pro(raw)
        if rec is not None:
            pros.append(rec)
    pros.sort(key=lambda p: p["id"])
    used_pro = {p["id"] for p in pros}
    next_pro_id = _as_int(store.get("next_pro_id")) or 1
    while next_pro_id in used_pro:
        next_pro_id += 1
    return {
        "version": VERSION,
        "next_id": max(1, next_id),
        "projects": projects,
        "next_pro_id": max(1, next_pro_id),
        "pros": pros,
    }


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
        row_tags = [str(x).strip() for x in (r.get("tags") or ()) if str(x).strip()]
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
            "effective_completed": completed,
            "effective_dropped": dropped,
            "blocked": bool(r.get("blocked")),
            "rank": len(members),
            "tags": row_tags,
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
    all_tags = []
    for tag in row.get("tags") or ():
        t = str(tag).strip()
        if t and t not in all_tags:
            all_tags.append(t)
    for item in members:
        if statuses.get(item["pk"], STATUS_ACTIVE) == STATUS_DROPPED:
            continue
        for tag in item.get("tags") or ():
            t = str(tag).strip()
            if t and t not in all_tags:
                all_tags.append(t)
    project_tag = (_one_line(row.get("project_tag"))
                   or extract_project_tag(all_tags))
    op_type = normalize_op_type(row.get("op_type") or row.get("type"))
    # 重复任务本身是父任务时，重复规则仍在父任务上；weekly_yield 会按整套算一条。
    payload = {
        "kind": "project",
        "id": pk,
        "title": name,
        "url": project_url(pk),
        "op_type": op_type,
        "tags": all_tags,
        "project_tag": project_tag,
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

    短任务按**实际分钟汇总**再向上取整成 count：7 条 10 分钟的任务总共 70
    分钟 = 3 count，而不是 7 条各 1 count 累加成 7。相应地，剩余 count 也是
    ``ceil(剩余分钟 / 25)``，保证 ``done + remaining == total``。
    """
    if invested_map is None:
        invested_map = invested(task_store, project)
    tasks = project.get("tasks") or []
    kids = child_map(tasks)
    statuses = effective_task_statuses(tasks)
    total_minutes = 0
    remaining_minutes = 0
    for item in tasks:
        if kids.get(item.get("pk")):
            continue
        pk = str(item.get("pk") or "")
        status = statuses.get(pk, STATUS_ACTIVE)
        if status == STATUS_DROPPED:
            continue
        minutes = _item_minutes(item)
        cap = int(item.get("count") or 0)
        if not minutes and cap:
            minutes = cap * ts.MINUTES_PER_COUNT
        if not minutes:
            continue
        total_minutes += minutes
        if status == STATUS_COMPLETED:
            continue
        # 已投入的 count 来自本地任务账；把 count 折回分钟封顶（短任务投 1 count
        # 也只算它自己的 10 分钟，不会算成 25 分钟）。
        got = min(cap or 0, int(invested_map.get(item["pk"], 0) or 0))
        spent = min(minutes, got * ts.MINUTES_PER_COUNT)
        remaining_minutes += max(0, minutes - spent)
    total = ts.minutes_to_count(total_minutes) or 0
    remaining = ts.minutes_to_count(remaining_minutes) or 0
    done = max(0, total - remaining)
    return {
        "total": total,
        "done": done,
        "remaining": remaining,
        "progress": (float(done) / total) if total else 0.0,
        "progress_text": "%d/%d" % (done, total) if total else "—",
    }


def _item_deferred(item, now=None):
    """项目清单项是否还没到 defer 时间（到点前不进自动排期候选）。"""
    if not isinstance(item, dict) or not item.get("defer"):
        return False
    return ts.task_is_deferred({"fields": {"defer": item.get("defer")}}, now=now)


def remaining_tasks(project, invested_map=None, now=None, exclude_pks=None):
    """还没做完、而且还有内容可干的**顶层任务**（按清单顺序）。

    有子任务的父任务返回成一条派生单元：``duration`` 是剩余子任务分钟数之和，
    ``count`` 是它的 ceil(minutes/25)，``children`` 里是每个剩余子任务（子任务
    不可再拆）。没有子任务的普通任务行为和以前一样。

    两类任务**不进入队列**：

    * ``defer`` 还没到点的（父任务 defer 挡住整棵子树；子任务各自也检查）；
    * ``exclude_pks`` 里的任务 —— 已经挂了 formatter 的项目任务由 formatter
      自己占时间，项目 plan 不能再把它当成待分配资源。
    """
    invested_map = invested_map or {}
    exclude = {str(pk) for pk in (exclude_pks or ())}
    tasks = project.get("tasks") or []
    kids = child_map(tasks)
    statuses = effective_task_statuses(tasks)
    out = []
    for item in top_level_tasks(tasks):
        pk = str(item.get("pk") or "")
        if pk in exclude:
            continue
        if statuses.get(pk, STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        if _item_deferred(item, now=now):
            continue
        children = kids.get(item.get("pk")) or []
        if children:
            open_children = []
            for child in children:
                child_pk = str(child.get("pk") or "")
                if child_pk in exclude:
                    continue
                if statuses.get(child_pk, STATUS_ACTIVE) != STATUS_ACTIVE:
                    continue
                if _item_deferred(child, now=now):
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
        minutes = _item_minutes(item)
        if not minutes and cap:
            minutes = cap * ts.MINUTES_PER_COUNT
        spent = min(minutes, got * ts.MINUTES_PER_COUNT)
        out.append(dict(item, remaining=cap - got, invested=got,
                        minutes=minutes,
                        remaining_minutes=max(0, minutes - spent)))
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
    @staticmethod
    def _ensure_pro_in_store(store, title, default_stage=STAGE_RUNNING, file_path=None):
        """在内存 store 里按名字查找或新建真正项目 Project（``pro``）。"""
        clean = _one_line(title)
        if not clean:
            return None
        pros = store.setdefault("pros", [])
        for pro in pros:
            if (pro.get("status", STATUS_ACTIVE) == STATUS_ACTIVE
                    and str(pro.get("title") or "").strip().casefold() == clean.casefold()):
                if file_path is not None:
                    pro["file"] = str(file_path).strip() or None
                return pro
        nid = int(store.get("next_pro_id") or 1)
        used = {p["id"] for p in pros}
        while nid in used:
            nid += 1
        stamp = _now()
        pro = {
            "id": nid,
            "title": clean,
            "stage": normalize_pro_stage(default_stage) or STAGE_RUNNING,
            "file": str(file_path).strip() if file_path else None,
            "sop_op_id": None,
            "incubator_op_id": None,
            "op_ids": [],
            "status": STATUS_ACTIVE,
            "created_at": stamp,
            "updated_at": stamp,
        }
        pros.append(pro)
        pros.sort(key=lambda p: p["id"])
        store["next_pro_id"] = max(int(store.get("next_pro_id") or 1), nid + 1)
        return pro

    @staticmethod
    def _bind_op_and_pro_in_store(store, op, pro):
        """在内存 store 里把 ``op`` 与 ``pro`` 双向绑定（并清掉旧 pro 上的重复引用）。"""
        if op is None or pro is None:
            return
        old_id = _as_int(op.get("pro_id"))
        if old_id is not None and old_id != pro["id"]:
            for old_pro in store.get("pros") or ():
                if old_pro.get("id") != old_id:
                    continue
                old_pro["op_ids"] = [x for x in (old_pro.get("op_ids") or ())
                                     if x != op["id"]]
                if old_pro.get("sop_op_id") == op["id"]:
                    old_pro["sop_op_id"] = None
                if old_pro.get("incubator_op_id") == op["id"]:
                    old_pro["incubator_op_id"] = None
                old_pro["updated_at"] = _now()
        op["pro_id"] = pro["id"]
        op_ids = pro.setdefault("op_ids", [])
        if op["id"] not in op_ids:
            op_ids.append(op["id"])
        if normalize_op_type(op.get("op_type")) == OP_TYPE_INCUBATOR:
            pro["incubator_op_id"] = op["id"]
        else:
            pro["sop_op_id"] = op["id"]
        pro["updated_at"] = _now()

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
        op_type = normalize_op_type(payload.get("op_type") or payload.get("type"))
        tags = []
        for tag in payload.get("tags") or ():
            t = str(tag).strip()
            if t and t not in tags:
                tags.append(t)
        project_tag = _one_line(payload.get("project_tag")) or extract_project_tag(tags)
        if not project_tag:
            task_tags = []
            for item in tasks:
                if not item.get("completed") and not item.get("dropped"):
                    task_tags.extend(item.get("tags") or ())
            project_tag = extract_project_tag(task_tags)
        pro_id = _as_int(payload.get("pro_id"))
        stamp = _now()
        target = None
        created = True
        for project in store["projects"]:
            if ((project.get("omnifocus") or {}).get("id") or "") == pk:
                project["tasks"] = tasks
                project["title"] = _one_line(payload.get("title")) or project["title"]
                if op_type is not None:
                    project["op_type"] = op_type
                if tags:
                    project["tags"] = tags
                if project_tag:
                    project["project_tag"] = project_tag
                if pro_id is not None:
                    project["pro_id"] = pro_id
                project["synced_at"] = stamp
                project["status"] = STATUS_ACTIVE
                target = project
                created = False
                break
        if target is None:
            nid = int(store.get("next_id") or 1)
            used = {p["id"] for p in store["projects"]}
            while nid in used:
                nid += 1
            target = {
                "id": nid,
                "title": _one_line(payload.get("title")) or ("项目 %s" % pk),
                "op_type": op_type,
                "pro_id": pro_id,
                "project_tag": project_tag or "",
                "tags": tags,
                "file": str(payload.get("file") or "").strip() or None,
                "omnifocus": {"id": pk,
                              "url": payload.get("url") or project_url(pk),
                              "title": _one_line(payload.get("title"))},
                "status": STATUS_ACTIVE,
                "imported_at": stamp,
                "synced_at": stamp,
                "tasks": tasks,
                "plans": {},
            }
            store["projects"].append(target)
            store["projects"].sort(key=lambda p: p["id"])
            store["next_id"] = max(int(store.get("next_id") or 1), nid + 1)
        # 如果带了显式 pro_id 或 OmniFocus 标签 project/...，自动关联真正项目 pro。
        linked_pro = None
        if target.get("pro_id") is not None:
            for pro in store.get("pros") or ():
                if pro["id"] == int(target["pro_id"]):
                    linked_pro = pro
                    break
        if linked_pro is None and target.get("project_tag"):
            def_stage = (STAGE_BOTTLENECK
                         if normalize_op_type(target.get("op_type")) == OP_TYPE_INCUBATOR
                         else STAGE_RUNNING)
            linked_pro = self._ensure_pro_in_store(
                store, target["project_tag"], default_stage=def_stage)
        if linked_pro is not None:
            self._bind_op_and_pro_in_store(store, target, linked_pro)
        self.save(store)
        return target, created

    def set_op_type(self, project_id, op_type):
        """设置 OmniFocus Project（``op``）的类型：``SOP`` 或 ``incubator``。"""
        norm_type = normalize_op_type(op_type)
        if norm_type not in OP_TYPES:
            raise ValueError("op 类型只能是 SOP 或 incubator；你给的是 %r" % op_type)
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            project["op_type"] = norm_type
            project["synced_at"] = _now()
            linked_pro = None
            if project.get("pro_id") is not None:
                for pro in store.get("pros") or ():
                    if pro["id"] == int(project["pro_id"]):
                        linked_pro = pro
                        break
            if linked_pro is None and project.get("project_tag"):
                def_stage = (STAGE_BOTTLENECK if norm_type == OP_TYPE_INCUBATOR
                             else STAGE_RUNNING)
                linked_pro = self._ensure_pro_in_store(
                    store, project["project_tag"], default_stage=def_stage)
            if linked_pro is not None:
                self._bind_op_and_pro_in_store(store, project, linked_pro)
            self.save(store)
            return project
        raise ValueError("没有 OmniFocus 项目（op）%s" % project_id)

    def untyped_ops(self):
        """返回还没填类型（SOP / incubator）的 active op 列表。"""
        return [p for p in self.active()
                if normalize_op_type(p.get("op_type")) is None]

    def ensure_tagged_pros(self):
        """把所有带 ``project/...`` 标签但还没建/没绑 ``pro`` 的 op 自动补齐关联。"""
        store = self.load()
        changed = False
        for project in store.get("projects") or ():
            if project.get("status") != STATUS_ACTIVE:
                continue
            ptag = (_one_line(project.get("project_tag"))
                    or extract_project_tag(project.get("tags") or ()))
            if not ptag:
                continue
            if project.get("project_tag") != ptag:
                project["project_tag"] = ptag
                changed = True
            pro = None
            if project.get("pro_id") is not None:
                pro = next((p for p in (store.get("pros") or ())
                            if p["id"] == int(project["pro_id"])
                            and p.get("status") == STATUS_ACTIVE), None)
            if pro is None:
                def_stage = (STAGE_BOTTLENECK
                             if normalize_op_type(project.get("op_type")) == OP_TYPE_INCUBATOR
                             else STAGE_RUNNING)
                pro = self._ensure_pro_in_store(store, ptag, default_stage=def_stage)
                self._bind_op_and_pro_in_store(store, project, pro)
                changed = True
        if changed:
            self.save(store)
        return changed

    # -- 真正项目 Project (pro) 的读写 --------------------------------
    def pros(self, status=None):
        pros = self.load().get("pros") or []
        if status is None:
            return pros
        return [p for p in pros if p.get("status") == status]

    def active_pros(self):
        return self.pros(status=STATUS_ACTIVE)

    def pro_by_id(self, pro_id, store=None):
        ident = _as_int(pro_id)
        if ident is None:
            return None
        data = store if isinstance(store, dict) else self.load()
        for pro in data.get("pros") or ():
            if pro["id"] == ident:
                return pro
        return None

    def pro_number_of(self, pro_id):
        ident = _as_int(pro_id)
        if ident is None:
            return None
        for index, pro in enumerate(self.active_pros(), 1):
            if pro["id"] == ident:
                return index
        return None

    def resolve_pro(self, token):
        """把 #序号 / id:N / 项目名 解析成一条 active ``pro`` 记录。"""
        raw = str(token or "").strip()
        if not raw:
            raise ValueError("要给项目（pro）序号或名称")
        if raw.startswith("#"):
            raw = raw[1:].strip()
        pros = self.active_pros()
        if not pros:
            raise ValueError("还没有真正项目（pro）：可用 `pro create <名称>` 创建，"
                             "或给 OmniFocus 项目加 `project/<名称>` 标签后导入。")
        low = raw.lower()
        if low.startswith("id:"):
            hit = self.pro_by_id(low[3:])
            if hit is None:
                raise ValueError("没有 id 为 %r 的项目（pro）" % raw[3:])
            return hit
        ident = _as_int(raw)
        if ident is not None and 1 <= ident <= len(pros):
            return pros[ident - 1]
        if ident is not None:
            hit = self.pro_by_id(ident)
            if hit is not None:
                return hit
        exact = [p for p in pros
                 if str(p.get("title") or "").strip().casefold() == raw.casefold()]
        if len(exact) == 1:
            return exact[0]
        fuzzy = [p for p in pros
                 if raw.casefold() in str(p.get("title") or "").casefold()]
        if len(fuzzy) == 1:
            return fuzzy[0]
        raise ValueError("项目（pro）序号 %r 不在范围里：#1–#%d（`pro` 看全部）"
                         % (raw, len(pros)))

    def create_pro(self, title, stage=STAGE_RUNNING, file=None, op_id=None):
        """创建或更新一个真正项目 Project（``pro``），返回 ``(pro, created)``。"""
        clean = _one_line(title)
        if not clean:
            raise ValueError("项目（pro）名称不能为空")
        norm_stage = normalize_pro_stage(stage) if stage else STAGE_RUNNING
        if norm_stage is None:
            raise ValueError("项目阶段只能是 running 或 bottleneck；你给的是 %r" % stage)
        store = self.load()
        pros = store.setdefault("pros", [])
        target = None
        created = False
        for pro in pros:
            if (pro.get("status") == STATUS_ACTIVE
                    and str(pro.get("title") or "").strip().casefold() == clean.casefold()):
                target = pro
                break
        if target is None:
            target = self._ensure_pro_in_store(
                store, clean, default_stage=norm_stage, file_path=file)
            created = True
        else:
            if stage:
                target["stage"] = norm_stage
            if file is not None:
                target["file"] = str(file).strip() or None
            target["updated_at"] = _now()
        if op_id is not None:
            op = next((p for p in (store.get("projects") or ())
                       if p["id"] == int(op_id)), None)
            if op is None:
                raise ValueError("没有 id 为 %s 的 OmniFocus 项目（op）" % op_id)
            self._bind_op_and_pro_in_store(store, op, target)
        # 顺手把同名 project_tag 的 op 也绑过来
        for op in store.get("projects") or ():
            if (op.get("status") == STATUS_ACTIVE
                    and str(op.get("project_tag") or "").strip().casefold() == clean.casefold()):
                self._bind_op_and_pro_in_store(store, op, target)
        self.save(store)
        return target, created

    def set_pro_file(self, pro_id, file_path):
        """设置或清空项目（``pro``）关联的打开文件。"""
        store = self.load()
        pro = self.pro_by_id(pro_id, store=store)
        if pro is None:
            raise ValueError("没有项目（pro）%s" % pro_id)
        clean = str(file_path or "").strip()
        if clean.lower() in ("", "none", "clear", "off", "清空", "无"):
            pro["file"] = None
        else:
            pro["file"] = clean
        pro["updated_at"] = _now()
        self.save(store)
        return pro

    def set_pro_stage(self, pro_id, stage):
        """直接修改项目（``pro``）阶段：``running`` 或 ``bottleneck``。"""
        norm_stage = normalize_pro_stage(stage)
        if norm_stage not in PRO_STAGES:
            raise ValueError("项目阶段只能是 running 或 bottleneck；你给的是 %r" % stage)
        store = self.load()
        pro = self.pro_by_id(pro_id, store=store)
        if pro is None:
            raise ValueError("没有项目（pro）%s" % pro_id)
        pro["stage"] = norm_stage
        pro["updated_at"] = _now()
        self.save(store)
        return pro

    def link_op_to_pro(self, op_id, pro_id, op_type=None):
        """把 ``op`` 关联到 ``pro``（可选顺带指定 ``op_type``）。"""
        store = self.load()
        op = next((p for p in (store.get("projects") or ())
                   if p["id"] == int(op_id)), None)
        if op is None:
            raise ValueError("没有 OmniFocus 项目（op）%s" % op_id)
        if pro_id is None:
            old_pro_id = op.get("pro_id")
            op["pro_id"] = None
            if old_pro_id is not None:
                old_pro = self.pro_by_id(old_pro_id, store=store)
                if old_pro is not None:
                    old_pro["op_ids"] = [x for x in (old_pro.get("op_ids") or ())
                                         if x != op["id"]]
                    if old_pro.get("sop_op_id") == op["id"]:
                        old_pro["sop_op_id"] = None
                    if old_pro.get("incubator_op_id") == op["id"]:
                        old_pro["incubator_op_id"] = None
            self.save(store)
            return op, None
        pro = self.pro_by_id(pro_id, store=store)
        if pro is None:
            raise ValueError("没有项目（pro）%s" % pro_id)
        if op_type is not None:
            norm_type = normalize_op_type(op_type)
            if norm_type not in OP_TYPES:
                raise ValueError("op 类型只能是 SOP 或 incubator；你给的是 %r" % op_type)
            op["op_type"] = norm_type
        self._bind_op_and_pro_in_store(store, op, pro)
        self.save(store)
        return op, pro

    def advance_pro(self, pro_id, op_id, target_stage=None):
        """切换 ``pro`` 的阶段（``running`` ↔ ``bottleneck``），并关联新的 ``op``。

        - ``running`` → ``bottleneck``：新的 ``op`` 会被标成 ``incubator`` 并作为该项目的
          ``incubator_op_id``；
        - ``bottleneck`` → ``running``：新的 ``op`` 会被标成 ``SOP`` 并作为该项目的
          ``sop_op_id``。
        返回 ``(pro, op)``。
        """
        store = self.load()
        pro = self.pro_by_id(pro_id, store=store)
        if pro is None:
            raise ValueError("没有项目（pro）%s" % pro_id)
        op = next((p for p in (store.get("projects") or ())
                   if p["id"] == int(op_id)), None)
        if op is None:
            raise ValueError("没有 OmniFocus 项目（op）%s" % op_id)
        cur = normalize_pro_stage(pro.get("stage")) or STAGE_RUNNING
        nxt = (normalize_pro_stage(target_stage)
               if target_stage else
               (STAGE_BOTTLENECK if cur == STAGE_RUNNING else STAGE_RUNNING))
        req_type = OP_TYPE_INCUBATOR if nxt == STAGE_BOTTLENECK else OP_TYPE_SOP
        op["op_type"] = req_type
        pro["stage"] = nxt
        self._bind_op_and_pro_in_store(store, op, pro)
        self.save(store)
        return pro, op

    def remove_pro(self, pro_id):
        store = self.load()
        before = len(store.get("pros") or [])
        store["pros"] = [p for p in (store.get("pros") or [])
                         if p["id"] != int(pro_id)]
        if len(store["pros"]) == before:
            raise ValueError("没有项目（pro）%s" % pro_id)
        for op in store.get("projects") or ():
            if op.get("pro_id") == int(pro_id):
                op["pro_id"] = None
        self.save(store)
        return before - len(store["pros"])

    def pro_for_op(self, op_or_id, store=None):
        """查找一个 ``op`` 关联的真正项目 ``pro``；没有关联返回 ``None``。"""
        data = store if isinstance(store, dict) else self.load()
        if isinstance(op_or_id, dict):
            op = op_or_id
        else:
            op = next((p for p in (data.get("projects") or ())
                       if p["id"] == _as_int(op_or_id)), None)
        if not op:
            return None
        pid = _as_int(op.get("pro_id"))
        if pid is not None:
            for pro in data.get("pros") or ():
                if pro["id"] == pid and pro.get("status") == STATUS_ACTIVE:
                    return pro
        ptag = str(op.get("project_tag") or "").strip()
        if not ptag:
            ptag = extract_project_tag(op.get("tags") or ())
        if ptag:
            for pro in data.get("pros") or ():
                if (pro.get("status") == STATUS_ACTIVE
                        and str(pro.get("title") or "").strip().casefold() == ptag.casefold()):
                    return pro
        # 反向查 pro.op_ids / sop_op_id / incubator_op_id
        oid = _as_int(op.get("id"))
        if oid is not None:
            for pro in data.get("pros") or ():
                if pro.get("status") != STATUS_ACTIVE:
                    continue
                if (oid == pro.get("sop_op_id")
                        or oid == pro.get("incubator_op_id")
                        or oid in (pro.get("op_ids") or ())):
                    return pro
        return None

    def ops_for_pro(self, pro_or_id, store=None):
        """返回关联到某个 ``pro`` 的全部 active ``op``。"""
        data = store if isinstance(store, dict) else self.load()
        pro = (pro_or_id if isinstance(pro_or_id, dict)
               else self.pro_by_id(pro_or_id, store=data))
        if not pro:
            return []
        out = []
        for op in data.get("projects") or ():
            if op.get("status") != STATUS_ACTIVE:
                continue
            linked = self.pro_for_op(op, store=data)
            if linked is not None and linked["id"] == pro["id"]:
                out.append(op)
        return out

    def is_op_allowed_by_pro(self, op, store=None):
        """根据关联 ``pro`` 的阶段判断这个 ``op`` 当前是否允许进行。

        - ``pro`` 在 ``bottleneck`` 阶段：只允许对应的 ``incubator`` op 进行；
        - ``pro`` 在 ``running`` 阶段：只允许普通的 ``SOP`` op 进行；
        - 未关联任何 ``pro`` 的 ``op``：按自身类型（SOP / incubator）放行。
        """
        data = store if isinstance(store, dict) else self.load()
        pro = self.pro_for_op(op, store=data)
        eff_type = normalize_op_type((op or {}).get("op_type")) or OP_TYPE_SOP
        if pro is None:
            return True
        stage = normalize_pro_stage(pro.get("stage")) or STAGE_RUNNING
        if stage == STAGE_BOTTLENECK:
            if eff_type != OP_TYPE_INCUBATOR:
                return False
            inc_id = _as_int(pro.get("incubator_op_id"))
            if inc_id is not None and inc_id != op.get("id"):
                inc_op = next((p for p in (data.get("projects") or ())
                               if p["id"] == inc_id and p.get("status") == STATUS_ACTIVE),
                              None)
                if inc_op is not None:
                    return False
            return True
        # stage == STAGE_RUNNING
        return eff_type == OP_TYPE_SOP

    def eligible_ops(self, op_type=OP_TYPE_SOP):
        """返回当前阶段允许参与 ``plan`` 的指定类型（``SOP`` 或 ``incubator``）的 active op。"""
        want = normalize_op_type(op_type) or OP_TYPE_SOP
        store = self.load()
        out = []
        for op in store.get("projects") or ():
            if op.get("status") != STATUS_ACTIVE:
                continue
            raw_type = normalize_op_type(op.get("op_type"))
            # 类型必须由用户说明；未说明的 op 不进入任何 plan 阶段，
            # 等 refresh / 导入提示补全后再排。
            if raw_type is None:
                continue
            if raw_type != want:
                continue
            if not self.is_op_allowed_by_pro(op, store=store):
                continue
            out.append(op)
        return out

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

    def set_project_file(self, project_id, file_path):
        """设置或清空 OmniFocus Project（``op``）关联的文件。"""
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            clean = str(file_path or "").strip()
            if clean.lower() in ("", "none", "clear", "off", "清空", "无"):
                project["file"] = None
            else:
                project["file"] = clean
            project["synced_at"] = _now()
            self.save(store)
            return project
        raise ValueError("没有项目 %s" % project_id)

    def append_tasks(self, project_id, tasks):
        """把新任务追加到某个项目清单末尾（relate --import 用）。

        ``tasks`` 是已经按 ``project_payload`` 校验过的任务记录；这里只做
        归一化 + 追加，不做校验。返回更新后的项目。
        """
        store = self.load()
        for project in store["projects"]:
            if project["id"] != int(project_id):
                continue
            existing = project.setdefault("tasks", [])
            start_rank = len(existing)
            for offset, item in enumerate(tasks or []):
                rec = _norm_task(item, rank=start_rank + offset)
                if rec is not None:
                    existing.append(rec)
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
        for pro in store.get("pros") or ():
            pro["op_ids"] = [x for x in (pro.get("op_ids") or ())
                             if x != int(project_id)]
            if pro.get("sop_op_id") == int(project_id):
                pro["sop_op_id"] = None
            if pro.get("incubator_op_id") == int(project_id):
                pro["incubator_op_id"] = None
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
        store = self.load()
        invested_all = invested_by_project(task_store) if task_store else {}
        active_ops = [p for p in (store.get("projects") or ())
                      if p.get("status") == STATUS_ACTIVE]
        for index, project in enumerate(active_ops, 1):
            mine = invested_all.get(project["id"]) or {}
            stat = stats(project, task_store, mine)
            plan = plan_of(project, day)
            week = weekly_yield(project.get("tasks") or [])
            pro = self.pro_for_op(project, store=store)
            allowed = self.is_op_allowed_by_pro(project, store=store)
            rows.append({
                "number": index,
                "id": project["id"],
                "title": project["title"],
                "op_type": normalize_op_type(project.get("op_type")),
                "pro": pro,
                "pro_id": pro["id"] if pro else None,
                "pro_title": pro.get("title") if pro else "",
                "pro_stage": pro.get("stage") if pro else "",
                "allowed": allowed,
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

    def pro_preview(self, day=None, task_store=None):
        """真正项目 Project（``pro``）预览行。"""
        store = self.load()
        pros = [p for p in (store.get("pros") or ())
                if p.get("status") == STATUS_ACTIVE]
        ops_by_id = {p["id"]: p for p in (store.get("projects") or ())
                     if p.get("status") == STATUS_ACTIVE}
        out = []
        for index, pro in enumerate(pros, 1):
            linked_ops = self.ops_for_pro(pro, store=store)
            sop_ops = [op for op in linked_ops
                       if (normalize_op_type(op.get("op_type")) or OP_TYPE_SOP) == OP_TYPE_SOP]
            inc_ops = [op for op in linked_ops
                       if normalize_op_type(op.get("op_type")) == OP_TYPE_INCUBATOR]
            sop_op = ops_by_id.get(pro.get("sop_op_id")) or (sop_ops[-1] if sop_ops else None)
            inc_op = ops_by_id.get(pro.get("incubator_op_id")) or (inc_ops[-1] if inc_ops else None)
            out.append({
                "number": index,
                "id": pro["id"],
                "title": pro.get("title") or ("项目 %d" % pro["id"]),
                "stage": normalize_pro_stage(pro.get("stage")) or STAGE_RUNNING,
                "file": pro.get("file"),
                "sop_op": sop_op,
                "incubator_op": inc_op,
                "ops": linked_ops,
                "pro": pro,
            })
        return out
