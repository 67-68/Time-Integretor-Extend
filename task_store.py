#!/usr/bin/env python3
"""task_store.py —— 极简任务库（JSON，CRUD，任意字段）。

任务系统的边界故意画得很窄：

- 一个任务就是一条 dict；除了内部用的 ``id`` / ``created_at`` / ``updated_at`` /
  ``session`` / ``focusmate`` 之外，用户写什么字段都行，**不做字段校验**。
- 存储用 JSON（默认 ``data/tasks.json``），原子写，坏文件不会让 TUI 崩掉。
- Focusmate 的 task 是外部实体：本地任务记录 ``focusmate.task_id`` 和它挂到哪场
  session 上；完成时间从 ``GET /v1/tasks`` 的 ``completedAt`` 读，不猜。
- 完成后写一份只增不改的归档 ``data/task-archive.json``。归档里同时保留：
  本地任务字段、对应 session（日期/时间/API session id）、Focusmate 的完成时间。
- 任务可以声明成**每周重复**（``repeat``）：记下「周几 + 起点 + 时长」，到那一周
  就自动挂到同一时刻、同一时长的那场 session 上。排布被改了（例如 50 拆成
  25+25）就不是同一场了 —— 这一周的重复任务自然消失，但 repeat 设置本身不变。
- 任务有**状态**（``active`` / ``completed`` / ``dropped``）、**本地标签**（``tags``）
  和**OmniFocus 关联**（``omnifocus``，只认 primary key）。完成/放弃的记录**不删**
  —— session 结束之后「那一场到底干成了什么」还要看得见。
- 挂到 session 上时，**截止时间自动 = 那场 session 的终点**（11:00–11:50 → 11:50），
  原值留档在 ``session.previous_due``；真正写回 OmniFocus 是 warden 在「完成」时做的
  （见 docs/omnifocus.md）。
- 一条 OmniFocus 任务可以对应**多条**本地记录（``duplicate_of`` 记来源）：
  旧场次那条放弃 + 标签「未完成转移」，新场次那条继续干 —— OmniFocus 那边始终一条。

这个模块不碰网络、不碰 data.csv，也不认识 rich。warden / bridge 都只调用这里。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
DEFAULT_ARCHIVE_FILE = os.path.join(DATA_DIR, "task-archive.json")

# 这些键是本地簿记，不属于用户字段。用户仍然可以随便写别的字段。
RESERVED = {"id", "fields", "created_at", "updated_at", "session", "focusmate", "repeat",
            "status", "status_at", "tags", "omnifocus", "duplicate_of", "actual_minutes",
            "history"}

#: 「这条任务在哪一天的 session 里被处理过」的痕迹（见 :meth:`TaskStore.mark_processed`）。
#: 任务是按天固化的：今天的任务面板 = 所有没做完的 + 今天处理过的；过去那天只剩
#: 「当时处理过的」。所以每次收尾动了一条任务，都要在这一栏里留一笔。
HISTORY_KEY = "history"
HISTORY_KEEP = 40

# 任务状态。**「完成」只表示本地这条记录收工了**，不等于 OmniFocus 那边也完成了：
# 两边是「单向覆盖」（warden → OmniFocus），同步动作由调用方决定、并且要留痕。
STATUS_ACTIVE = "active"        # 还没收尾（默认）
STATUS_COMPLETED = "completed"  # 完成了（Focusmate / OmniFocus / 手工补录）
STATUS_DROPPED = "dropped"      # 放弃 / 转移走了（这条记录不再算账）
STATUSES = (STATUS_ACTIVE, STATUS_COMPLETED, STATUS_DROPPED)

#: 表格里的状态标记。**完成/放弃的任务不隐藏**（用户要求：session 结束之后也要看得到
#: 那一场到底干成了什么），只是前面多一个记号。
STATUS_MARKS = {STATUS_ACTIVE: "·", STATUS_COMPLETED: "✓", STATUS_DROPPED: "✗"}

#: 「延期 / 快捷推后」给旧任务打的那个本地标签。只在本地，OmniFocus 一个字节都不动。
TRANSFER_TAG = "未完成转移"

# 「备注」：一个普通字段（不是保留字段，字段仍然不校验），但有一套固定别名，
# 因为它是**有意设计**的：可以往里面塞网址，任务完成后照着去对应地方把活干完。
NOTES_KEYS = ("notes", "note", "备注")

# 备注字段的别名统一收敛到 ``notes``，免得表格里同时出现 note / notes / 备注 三列
FIELD_ALIASES = {"note": "notes", "备注": "notes", "remarks": "notes", "remark": "notes"}


def _field_name(key):
    """字段名的规范化（目前只有备注的别名）。"""
    name = str(key or "").strip()
    return FIELD_ALIASES.get(name, name)


def _norm_fields(fields):
    out = {}
    for k, v in (fields or {}).items():
        out[_field_name(k)] = v
    return out

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def tasks_path(path=None):
    return path or os.environ.get("TI_TASKS_FILE") or DEFAULT_TASKS_FILE


def archive_path(path=None):
    return path or os.environ.get("TI_TASK_ARCHIVE") or DEFAULT_ARCHIVE_FILE


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


def _empty_store():
    return {"version": 1, "next_id": 1, "tasks": []}


def _coerce_task(raw, fallback_id=None):
    """把一条可能来自旧版本/手改的 JSON 修成可用形状。"""
    if not isinstance(raw, dict):
        return None
    task = dict(raw)
    try:
        task["id"] = int(task.get("id", fallback_id))
    except (TypeError, ValueError):
        task["id"] = int(fallback_id or 0)
    if task["id"] <= 0:
        return None
    fields = task.get("fields")
    if not isinstance(fields, dict):
        # 兼容「task 本身就是扁平字段」的写法：把未知键收进 fields。
        fields = {k: v for k, v in task.items() if k not in RESERVED}
    task["fields"] = dict(fields)
    if not isinstance(task.get("session"), dict):
        task["session"] = None
    if not isinstance(task.get("focusmate"), dict):
        task["focusmate"] = {}
    task["repeat"] = _coerce_repeat(task.get("repeat"))
    task["omnifocus"] = _coerce_omnifocus(task.get("omnifocus"))
    task["tags"] = _coerce_tags(task.get("tags"))
    # 老数据没有 status：有 Focusmate 完成时间的就是「已完成」，否则「进行中」。
    status = str(task.get("status") or "").strip().lower()
    if status not in STATUSES:
        done = (task.get("focusmate") or {}).get("completed_at")
        status = STATUS_COMPLETED if done else STATUS_ACTIVE
    task["status"] = status
    if status != STATUS_ACTIVE and not task.get("status_at"):
        task["status_at"] = ((task.get("focusmate") or {}).get("completed_at")
                             or task.get("updated_at") or _now())
    try:
        dup = task.get("duplicate_of")
        task["duplicate_of"] = int(dup) if dup is not None else None
    except (TypeError, ValueError):
        task["duplicate_of"] = None
    try:
        minutes = task.get("actual_minutes")
        task["actual_minutes"] = int(minutes) if minutes is not None else None
    except (TypeError, ValueError):
        task["actual_minutes"] = None
    history = _coerce_history(task.get(HISTORY_KEY))
    if history:
        task[HISTORY_KEY] = history
    else:
        task.pop(HISTORY_KEY, None)
    task.setdefault("created_at", _now())
    task.setdefault("updated_at", task["created_at"])
    return task


def _coerce_tags(raw):
    """本地标签：去空、去重、保序；不是列表就按一个标签处理。"""
    out = []
    if isinstance(raw, str):
        raw = [raw]
    for item in raw or []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _day_text(value):
    """``date`` / ``datetime`` / ``"2026-10-28"`` / ``"2026-10-28T11:00"`` → ``"2026-10-28"``。"""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _coerce_history(raw):
    """处理痕迹：只留形状对的记录，按发生顺序排。"""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        day = _day_text(item.get("day"))
        if not day:
            continue
        out.append({
            "day": day,
            "start": str(item.get("start") or ""),
            "action": str(item.get("action") or ""),
            "at": str(item.get("at") or ""),
        })
    out.sort(key=lambda r: (r["day"], r["at"]))
    return out


OMNI_ID_RE = re.compile(r"omnifocus:///task/([A-Za-z0-9_\-=+%/]+)")


def _parse_omni_id(ident):
    """裸 primary key / ``omnifocus:///task/PK`` / ``[标题](omnifocus:///task/PK)``
    → ``{"id", "url", "title"}``。认不出 primary key 时 ``id`` 是 None。

    这段逻辑和 ``omnifocus.py`` 的 :func:`parse_link` 是同一个契约；放一份在这里
    是为了 task_store 不依赖那个模块（它要起子进程，导入它没意义）。
    """
    raw = str(ident or "").strip()
    title = None
    match = re.search(r"\[([^\]\n]*)\]\((omnifocus:///task/[^)\s]+)\)", raw)
    if match:
        title = match.group(1).strip() or None
        raw = match.group(2)
    pk = None
    match = OMNI_ID_RE.search(raw)
    if match:
        pk = match.group(1)
    elif raw and "://" not in raw:
        pk = raw.split()[0] if raw.split() else None
    if pk:
        pk = pk.strip("<>").rstrip(".,;:!?)]}>\"'，。、；：）」』】")
    return {
        "id": pk or None,
        "url": ("omnifocus:///task/%s" % pk) if pk else None,
        "title": title,
    }


def session_due(session):
    """这场 session 的**截止时间**（本地 ISO）：日期 + 起点 + 时长 = 终点。

    需求：任务被分配到一场 session 上时，它的截止时间＝这场的终点
    （11:00–11:50 那场 → 截止 11:50，「这场结束前必须干完」）。
    算不出来（没有时长/时刻）就返回 None，不瞎编。
    """
    if not isinstance(session, dict):
        return None
    day = str(session.get("date") or "").strip()
    start = str(session.get("start") or "").strip()
    if not day or not re.match(r"^\d{1,2}:\d{2}$", start):
        return None
    hh, mm = (int(x) for x in start.split(":"))
    try:
        begin = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm)
    except ValueError:
        return None
    duration = session.get("duration")
    if duration is None:
        # 没有时长就用 session 里记的 end（老记录可能只存了 end）
        end = str(session.get("end") or "").strip()
        if re.match(r"^\d{1,2}:\d{2}$", end):
            eh, em = (int(x) for x in end.split(":"))
            end_dt = begin.replace(hour=eh, minute=em)
            if end_dt <= begin:
                end_dt += datetime.timedelta(days=1)
            return end_dt.isoformat(timespec="seconds")
        return None
    try:
        finish = begin + datetime.timedelta(minutes=int(duration))
    except (TypeError, ValueError):
        return None
    return finish.isoformat(timespec="seconds")


def _coerce_omnifocus(raw):
    """OmniFocus 关联：``{"id": primary key, "url", "title", "linked_at", "writeback"}``。

    只有 ``id``（primary key）是硬的：链接解析不出来就没有关联，
    绝不拿标题去猜一条任务（猜错比没有更糟）。
    """
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get("id") or "").strip()
    if not ident:
        return None
    out = {
        "id": ident,
        "url": str(raw.get("url") or ("omnifocus:///task/%s" % ident)),
        "title": str(raw.get("title") or "").strip() or None,
        "linked_at": str(raw.get("linked_at") or _now()),
    }
    writeback = raw.get("writeback")
    if isinstance(writeback, dict) and writeback:
        out["writeback"] = {str(k): v for k, v in writeback.items()}
    return out


def _coerce_repeat(raw):
    """把重复设置修成可用形状；坏的就当没设置。"""
    if not isinstance(raw, dict):
        return None
    wd = raw.get("weekday")
    if isinstance(wd, int) and 0 <= wd <= 6:
        label = WEEKDAYS[wd]
    else:
        label = str(wd or "").strip().lower()[:3]
        if label not in WEEKDAYS:
            return None
    start = str(raw.get("start") or "").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", start):
        return None
    try:
        duration = int(raw.get("duration"))
    except (TypeError, ValueError):
        duration = None
    out = {
        "weekday": WEEKDAYS.index(label),
        "weekday_label": label,
        "start": "%02d:%02d" % tuple(int(x) for x in start.split(":")),
        "duration": duration,
        "created_at": str(raw.get("created_at") or _now()),
        "occurrences": {},
    }
    occ = raw.get("occurrences")
    if isinstance(occ, dict):
        for day, rec in occ.items():
            if isinstance(rec, dict):
                out["occurrences"][str(day)] = dict(rec)
    return out


def _clean_store(raw):
    if isinstance(raw, list):
        raw = {"tasks": raw}
    if not isinstance(raw, dict):
        return _empty_store()
    out = _empty_store()
    seen = set()
    nxt = 1
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        # 兼容 {"1": {...}} 这种手改形状
        tasks = list(raw.values()) if all(isinstance(v, dict) for v in raw.values()) else []
    for i, item in enumerate(tasks, 1):
        task = _coerce_task(item, fallback_id=i)
        if task is None or task["id"] in seen:
            continue
        seen.add(task["id"])
        nxt = max(nxt, task["id"] + 1)
        out["tasks"].append(task)
    try:
        out["next_id"] = max(nxt, int(raw.get("next_id") or 1))
    except (TypeError, ValueError):
        out["next_id"] = nxt
    out["tasks"].sort(key=lambda t: t["id"])
    return out


class TaskStore(object):
    """一个 JSON 任务库。路径可注入，方便测试不碰真实数据。"""

    def __init__(self, path=None, archive=None):
        self.path = tasks_path(path)
        self.archive = archive_path(archive)

    # -- 读写 ---------------------------------------------------------
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return _clean_store(json.load(f))
        except (OSError, ValueError):
            return _empty_store()

    def save(self, store):
        _atomic_write(self.path, _clean_store(store))

    def list(self):
        return self.load()["tasks"]

    def get(self, task_id):
        try:
            want = int(task_id)
        except (TypeError, ValueError):
            raise ValueError("任务 ID 得是数字；你给的是 %r" % (task_id,))
        for task in self.load()["tasks"]:
            if task["id"] == want:
                return task
        raise ValueError("没有任务 %d（用 task list 看当前有哪些）" % want)

    # -- CRUD ---------------------------------------------------------
    def create(self, fields=None, task_id=None, status=None, tags=None,
               omnifocus=None):
        """创建任务。fields 不校验；task_id 可以显式指定（占位/导入场景）。

        ``tags`` 是本地标签，``omnifocus`` 是 ``{"id": primary key, ...}``
        形式的关联（见 :meth:`link_omnifocus`）。
        """
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            raise ValueError("任务字段必须是一个 JSON object/dict")
        store = self.load()
        used = {t["id"] for t in store["tasks"]}
        if task_id is None:
            nid = int(store.get("next_id") or 1)
            while nid in used:
                nid += 1
        else:
            try:
                nid = int(task_id)
            except (TypeError, ValueError):
                raise ValueError("任务 ID 得是数字；你给的是 %r" % (task_id,))
            if nid <= 0:
                raise ValueError("任务 ID 必须大于 0")
            if nid in used:
                raise ValueError("任务 %d 已经存在" % nid)
        stamp = _now()
        task = {
            "id": nid,
            "fields": _norm_fields(fields),
            "created_at": stamp,
            "updated_at": stamp,
            "session": None,
            "focusmate": {},
            "status": (status if status in STATUSES else STATUS_ACTIVE),
            "tags": _coerce_tags(tags),
            "omnifocus": _coerce_omnifocus(omnifocus),
        }
        if task["status"] != STATUS_ACTIVE:
            task["status_at"] = stamp
        store["tasks"].append(task)
        store["tasks"].sort(key=lambda t: t["id"])
        store["next_id"] = max(int(store.get("next_id") or 1), nid + 1)
        self.save(store)
        return task

    def delete(self, task_id):
        task = self.get(task_id)
        store = self.load()
        store["tasks"] = [t for t in store["tasks"] if t["id"] != task["id"]]
        self.save(store)
        return task

    def modify(self, task_id, field, value):
        """改一个字段。field 不做白名单校验（用户要任意字段）。"""
        field = _field_name(field)
        if not field:
            raise ValueError("字段名不能为空")
        if field in RESERVED:
            raise ValueError("%r 是任务库的保留字段；用对应命令改它" % field)
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        found["fields"][field] = value
        found["updated_at"] = _now()
        self.save(store)
        return found

    # -- 状态 / 标签 / OmniFocus 关联 ----------------------------------

    def _mutate(self, task_id, fn):
        """在锁内改一条任务：``fn(task)`` 随便改，落盘由这里统一负责。"""
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        fn(found)
        found["updated_at"] = _now()
        self.save(store)
        return found

    @staticmethod
    def omnifocus_id(task):
        """这条任务关联的 OmniFocus primary key（没关联就是 None）。"""
        return ((task or {}).get("omnifocus") or {}).get("id")

    def link_omnifocus(self, task_id, ident, title=None, url=None):
        """把本地任务关联到一条 OmniFocus 任务（primary key 就是身份）。

        ``ident`` 可以是裸 primary key、``omnifocus:///task/PK``、或者
        ``[标题](omnifocus:///task/PK)`` 这种整条粘贴 —— 认不出 primary key 就报错，
        绝不用标题去猜（猜错比没有更糟）。
        """
        info = _parse_omni_id(ident)
        if not info["id"]:
            raise ValueError("认不出 OmniFocus 任务 primary key：%r" % (ident,))
        if not info["title"]:
            info["title"] = str(title or "").strip() or None
        if url:
            info["url"] = str(url)

        def apply(task):
            task["omnifocus"] = _coerce_omnifocus({
                "id": info["id"], "url": info["url"], "title": info["title"],
            })
        return self._mutate(task_id, apply)

    def unlink_omnifocus(self, task_id):
        def apply(task):
            task["omnifocus"] = None
        return self._mutate(task_id, apply)

    def set_omnifocus_writeback(self, task_id, what, at=None):
        """记一笔「已经往 OmniFocus 写过什么」，避免同一件事重复写。"""
        def apply(task):
            omni = task.get("omnifocus") or {}
            wb = dict(omni.get("writeback") or {})
            wb[str(what)] = at or _now()
            omni["writeback"] = wb
            task["omnifocus"] = omni
        return self._mutate(task_id, apply)

    def add_tag(self, task_id, tag):
        """加一个本地标签；已经有了就返回 False（幂等）。"""
        tag = str(tag or "").strip()
        if not tag:
            raise ValueError("标签不能为空")
        added = {"v": False}

        def apply(task):
            tags = _coerce_tags(task.get("tags"))
            if tag not in tags:
                tags.append(tag)
                added["v"] = True
            task["tags"] = tags
        self._mutate(task_id, apply)
        return added["v"]

    def remove_tag(self, task_id, tag):
        tag = str(tag or "").strip()
        removed = {"v": False}

        def apply(task):
            tags = _coerce_tags(task.get("tags"))
            if tag in tags:
                tags.remove(tag)
                removed["v"] = True
            task["tags"] = tags
        self._mutate(task_id, apply)
        return removed["v"]

    def set_status(self, task_id, status, at=None, reason=None):
        """改状态（active / completed / dropped），顺便记下时间和原因。"""
        if status not in STATUSES:
            raise ValueError("状态只能是 %s；你给的是 %r"
                             % (" / ".join(STATUSES), status))
        stamp = at or _now()

        def apply(task):
            task["status"] = status
            if status == STATUS_ACTIVE:
                task["status_at"] = None
            else:
                task["status_at"] = stamp
            if reason:
                task["status_reason"] = str(reason)
        return self._mutate(task_id, apply)

    def drop(self, task_id, reason=None, tag=None, at=None):
        """放弃这条本地记录（可选打个本地标签）。

        **只动本地**：OmniFocus 那边要不要放弃是调用方的事（收尾流程里「放弃」
        会另外调 omnifocus 写一次；「延期 / 快捷推后」则一个字节都不动）。
        """
        self.set_status(task_id, STATUS_DROPPED, at=at, reason=reason)
        if tag:
            self.add_tag(task_id, tag)
        return self.get(task_id)

    def complete(self, task_id, at=None, minutes=None, reason=None):
        """本地标记完成（``minutes`` 是「缺漏完成补充」手填的实际耗时）。"""
        task = self.set_status(task_id, STATUS_COMPLETED, at=at, reason=reason)
        if minutes is not None:
            def apply(found):
                found["actual_minutes"] = int(minutes)
            task = self._mutate(task_id, apply)
        return task

    def set_due(self, task_id, due):
        """写 ``fields.due``（session 时间就是截止时间，见 :meth:`assign`）。"""
        return self.modify(task_id, "due", due)

    def duplicate(self, task_id, session=None, fields=None, note=None):
        """复制一条任务：同样的字段 + 同样的 OmniFocus 关联，**新的**本地 ID。

        - 不复制 ``repeat``：那是原任务的排布规则，复制品不在同一时刻同一时长上。
        - 不复制 Focusmate 记录：Focusmate 上是另建一条 task（要挂场次由调用方做）。
        - 记下 ``duplicate_of``，这样「这条是从谁来的」永远查得到。
        """
        src = self.get(task_id)
        new_fields = dict(src.get("fields") or {})
        new_fields.update(fields or {})
        if note is not None:
            new_fields["notes"] = str(note)
        copy = self.create(new_fields, tags=list(src.get("tags") or []),
                           omnifocus=dict(src["omnifocus"]) if src.get("omnifocus") else None)
        copy = self._mutate(copy["id"], lambda t: t.update({"duplicate_of": src["id"]}))
        if session:
            copy = self.assign(copy["id"], session)
        return copy

    # -- 状态查询 -----------------------------------------------------
    @staticmethod
    def status_of(task):
        return (task or {}).get("status") or STATUS_ACTIVE

    @staticmethod
    def status_mark(task):
        return STATUS_MARKS.get(TaskStore.status_of(task), "·")

    @staticmethod
    def is_open(task):
        """还挂着账的任务（未完成、未放弃）。"""
        return TaskStore.status_of(task) == STATUS_ACTIVE

    @staticmethod
    def is_done(task):
        return TaskStore.status_of(task) in (STATUS_COMPLETED, STATUS_DROPPED)

    # -- 「这一天处理过哪些任务」 ---------------------------------------

    def mark_processed(self, task_id, day, start=None, action="", at=None):
        """留一笔「这条任务在这一天的 session 里被处理过」。

        任务面板是按天固化的（今天的面板 = 没做完的 + 今天处理过的；过去那天只剩
        「当时处理过的」），所以每一次收尾动作都要打这个戳 —— 放弃 / 完成 / 补录 /
        重议 / 推后 / 转移走 / 跳过**都算**，用户的原话是「无论结果是没做完迁移、
        放弃还是什么」。

        同一个 ``(day, start)`` 只留最后一条：同一场里先按了 A 延期没成、后来按 B
        放弃，结果以最后一次为准。
        """
        day = _day_text(day)
        if not day:
            raise ValueError("处理痕迹要一个日期（YYYY-MM-DD）")
        if isinstance(at, datetime.datetime):
            at = at.isoformat(timespec="seconds")
        record = {"day": day, "start": str(start or ""), "action": str(action or ""),
                  "at": str(at or _now())}

        def apply(task):
            history = [r for r in _coerce_history(task.get(HISTORY_KEY))
                       if (r["day"], r["start"]) != (record["day"], record["start"])]
            history.append(record)
            task[HISTORY_KEY] = history[-HISTORY_KEEP:]
        return self._mutate(task_id, apply)

    @staticmethod
    def processed_on(task, day):
        """``[{"day","start","action","derived"}]``：这一天里这条任务被处理的记录。

        显式痕迹优先。老数据没有痕迹（升级前收尾过的场次），就按状态时间推一把：
        ``status_at``（或 Focusmate 的 completedAt）落在这一天 → 算这天处理的。
        不这么兜底，升级当天的「过去那几天」面板会整片空白。
        """
        day = _day_text(day)
        task = task or {}
        found = [dict(r, derived=False) for r in _coerce_history(task.get(HISTORY_KEY))
                 if r["day"] == day]
        if found:
            return found
        if TaskStore.status_of(task) == STATUS_ACTIVE:
            return []
        stamp = (task.get("status_at")
                 or (task.get("focusmate") or {}).get("completed_at") or "")
        if _day_text(stamp) != day:
            return []
        sess = task.get("session") or {}
        return [{"day": day,
                 "start": str(sess.get("start") or "") if _day_text(
                     sess.get("date")) == day else "",
                 "action": TaskStore.status_of(task), "at": str(stamp), "derived": True}]

    def by_status(self, *statuses):
        want = set(statuses or STATUSES)
        return [t for t in self.list() if self.status_of(t) in want]

    def open_tasks(self):
        return self.by_status(STATUS_ACTIVE)

    # -- session / Focusmate -----------------------------------------
    def tasks_for_session(self, day, start, include_done=True):
        """这一场（某天 + 起始时刻）上挂着的所有任务，按 ID 升序。

        一场 session 可以挂多个任务（Focusmate 侧 task ↔ session 是 n:n），
        所以这里返回**列表**，不是单条。

        ``include_done=True``（默认）连**已完成 / 已放弃 / 已转移**的一起返回：
        用户要求 session 结束之后「那一场到底干成了什么」仍然看得到，
        所以状态由调用方打标记，而不是在这里筛掉。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        out = []
        for task in self.list():
            sess = task.get("session") or {}
            if sess.get("date") == iso and sess.get("start") == start:
                if not include_done and not self.is_open(task):
                    continue
                out.append(task)
        return sorted(out, key=lambda t: t.get("id") or 0)

    def assign(self, task_id, session):
        """把任务挂到一场 session 上（只写本地；Focusmate 写入由调用方做）。

        多任务共存：同一场 session 上已经挂了别的任务也没关系 —— 每个任务各存
        各的 ``session``，这里只覆盖本条任务自己的 assignment。
        """
        if not isinstance(session, dict):
            raise ValueError("session 必须是一个 dict")
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        old = found.get("session") or {}
        if old and (old.get("date"), old.get("start")) != (session.get("date"),
                                                           session.get("start")):
            # 换场次了：先记住旧场次，等 attach_focusmate 拿到新 task id 时再归档成 previous
            found.setdefault("focusmate", {})["previous_session"] = dict(old)
        found["session"] = dict(session)
        # 截止时间 = 这场 session 的**终点**。这是自动的（用户要求「任务的截止时间
        # 等于它被分配的 session 时间」），所以覆盖原值；原值留档在 session 里，
        # 手写的 due 被顶掉了也查得到。OmniFocus 那边的写回发生在「完成」的时候
        # （见 warden 的收尾流程），不在这里。
        due = session_due(session)
        if due:
            fields = found.setdefault("fields", {})
            old_due = fields.get("due")
            if old_due and old_due != due:
                found["session"]["previous_due"] = old_due
            fields["due"] = due
            fields["due_source"] = "session"
        found["updated_at"] = _now()
        self.save(store)
        return found

    def unassign(self, task_id, reason=None):
        """断掉 session 绑定（**任务本身留着**）。

        跑排布改动时用：那一场被取消/改时长，任务就不该再挂在它身上。
        Focusmate 侧那条 task 记进 ``focusmate.previous``（连同当时的 session 和
        ``reason``），这样它以后被勾完成时 `reconcile` 还能按 completedAt 归档。
        完成时间 ``completed_at`` 不抹掉 —— 那是历史，不是绑定。
        """
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        fm = found.setdefault("focusmate", {})
        old_id = fm.get("task_id") or fm.get("id")
        if old_id:
            fm.setdefault("previous", []).append({
                "task_id": old_id,
                "session": dict(found.get("session") or {}) or None,
                "replaced_at": _now(),
                "reason": reason or "unassigned",
            })
        for key in ("task_id", "id", "session_id", "attached_at", "title", "via"):
            fm.pop(key, None)
        found["session"] = None
        found["updated_at"] = _now()
        self.save(store)
        return found

    def attach_focusmate(self, task_id, focusmate_task, session=None):
        """记下 Focusmate 侧 task id / session id；不覆盖已经归档的完成记录。"""
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        fm = found.setdefault("focusmate", {})
        new_id = (focusmate_task or {}).get("task_id") or (focusmate_task or {}).get("id")
        old_id = fm.get("task_id") or fm.get("id")
        if old_id and new_id and str(old_id) != str(new_id):
            # 改挂到别的场次了：旧场次那条 Focusmate task 不会跟着挪（API 没有「挪」，
            # 我们是又建了一条）。把它记在 previous 里，完成时间照样能归档。
            fm.setdefault("previous", []).append({
                "task_id": old_id,
                # 旧场次是 assign() 换场次前记下的（此时 found["session"] 已经是新的了）
                "session": fm.pop("previous_session", None) or dict(found.get("session") or {}) or None,
                "replaced_at": _now(),
            })
        fm.update(dict(focusmate_task or {}))
        if session is not None:
            # 不要整段覆盖 session：assign() 往里存的簿记（previous_due 这种）
            # 是「这一场的历史」，attach 只是补 Focusmate 那几个字段。
            merged = dict(session)
            old_session = found.get("session") or {}
            for keep in ("previous_due",):
                if old_session.get(keep) and not merged.get(keep):
                    merged[keep] = old_session[keep]
            found["session"] = merged
        found["updated_at"] = _now()
        self.save(store)
        return found

    def forget_focusmate(self, task_id, reason="deleted", keep_previous=True):
        """Focusmate 那条 task 已经删掉了 → 本地别再记着它。

        任务**转移**走之后，旧场次上那条 Focusmate task 会被删（bridge 的
        ``delete_task``）。删完还留着 ``focusmate.task_id`` 的话，下一次
        ``reconcile`` 会以为「这条在 Focusmate 里还在、而且没完成」，一直等它。

        旧 id 挪进 ``previous``（归档/查账还要用），当前那格清空。
        """
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        fm = found.get("focusmate") or {}
        old_id = fm.get("task_id") or fm.get("id")
        if keep_previous and old_id:
            fm.setdefault("previous", []).append({
                "task_id": old_id,
                "session": dict(found.get("session") or {}) or None,
                "deleted_at": _now(),
                "reason": reason,
            })
        for key in ("task_id", "id", "completed_at", "attached_at", "via", "title"):
            fm.pop(key, None)
        fm["deleted_at"] = _now()
        fm["deleted_reason"] = reason
        found["focusmate"] = fm
        found["updated_at"] = _now()
        self.save(store)
        return found

    # -- 每周重复 -----------------------------------------------------
    def set_repeat(self, task_id, weekday, start, duration=None):
        """把任务标成每周重复：``weekday`` 0-6（或 mon..sun），``start`` HH:MM。

        ``duration`` 是**锚定**用的：只有那一周同一时刻、同一时长的那场 session
        才算这次重复的落点。排布改了（拆分/换时长）就不再匹配 —— 这一周的重复
        任务消失，但 repeat 设置原封不动留着，下一周照旧。
        """
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        if isinstance(weekday, int):
            label = WEEKDAYS[weekday % 7]
        else:
            label = str(weekday or "").strip().lower()[:3]
        if label not in WEEKDAYS:
            raise ValueError("星期几认不出来：%r（mon..sun / 周一..周日）" % (weekday,))
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(start or "").strip())
        if not m:
            raise ValueError("时间要写成 HH:MM；你给的是 %r" % (start,))
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("时间超出范围：%r" % (start,))
        if minute % 15:
            raise ValueError("Focusmate 只认 15 分钟整点，%r 不行" % (start,))
        if duration is not None:
            try:
                duration = int(str(duration).strip())
            except (TypeError, ValueError):
                raise ValueError("时长得是数字；你给的是 %r" % (duration,))
        kept = (found.get("repeat") or {}).get("occurrences") or {}
        found["repeat"] = {
            "weekday": WEEKDAYS.index(label),
            "weekday_label": label,
            "start": "%02d:%02d" % (hour, minute),
            "duration": duration,
            "created_at": _now(),
            "occurrences": kept,
        }
        found["updated_at"] = _now()
        self.save(store)
        return found

    def clear_repeat(self, task_id):
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        found["repeat"] = None
        found["updated_at"] = _now()
        self.save(store)
        return found

    def repeated_tasks(self, weekday=None):
        """所有重复任务；``weekday`` 给了就只要那天会重复的。"""
        out = []
        for task in self.load()["tasks"]:
            rep = task.get("repeat")
            if not rep:
                continue
            if weekday is not None and rep.get("weekday") != int(weekday) % 7:
                continue
            out.append(task)
        return out

    @staticmethod
    def repeat_occurrence(task, day, sessions):
        """这个任务在 ``day`` 那天落在哪一场上；落不上返回 None。

        ``sessions`` 是那天**排好的** session（``start`` / ``duration`` 够用）。
        匹配条件是「同一星期几 + 同一时刻 + 同一时长」：时长对不上就是排布被改过
        （例如 50 拆成 25+25），这一周的重复任务就不落。
        """
        rep = task.get("repeat") if isinstance(task, dict) else None
        if not rep:
            return None
        want_wd = day.weekday() if hasattr(day, "weekday") else int(day) % 7
        if int(rep.get("weekday", -1)) != want_wd:
            return None
        for sess in sessions or []:
            if sess.get("start") != rep.get("start"):
                continue
            if rep.get("duration") is not None and int(sess.get("duration") or 0) != int(rep["duration"]):
                continue
            return sess
        return None

    def attach_occurrence(self, task_id, day, session, focusmate=None):
        """记下一次重复的落点（本地 session + Focusmate 侧 task id）。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        rep = found.get("repeat") or {}
        occ = rep.setdefault("occurrences", {})
        rec = dict(occ.get(iso) or {})
        rec["session"] = dict(session or {})
        if focusmate:
            rec["focusmate"] = dict(focusmate)
        rec["updated_at"] = _now()
        occ[iso] = rec
        found["repeat"] = rep
        found["session"] = dict(session or {})
        # 和 assign() 同一条规则：截止时间 = 这一场的终点
        due = session_due(session or {})
        if due:
            fields = found.setdefault("fields", {})
            if fields.get("due") and fields.get("due") != due:
                found["session"]["previous_due"] = fields.get("due")
            fields["due"] = due
            fields["due_source"] = "session"
        found["updated_at"] = _now()
        self.save(store)
        return found

    # -- 摘要 / 展示 ---------------------------------------------------
    @staticmethod
    def _one_line(value):
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                value = str(value)
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text

    def summarize(self, task, max_len=None):
        """把任意字段压成一行；多字段按 ``1. ...  2. ...`` 排。

        需求明确要求「使用 trim，在一行之内显示完」：这里把换行/连续空白压成空格，
        Rich 侧再用 no_wrap + crop 截断。
        """
        values = [self._one_line(v) for v in (task.get("fields") or {}).values()]
        values = [v for v in values if v]
        if not values:
            text = "（空任务 #%s）" % task.get("id")
        elif len(values) == 1:
            text = values[0]
        else:
            text = "  ".join("%d. %s" % (i + 1, v) for i, v in enumerate(values))
        if max_len and len(text) > max_len:
            return text[: max(0, max_len - 1)] + "…"
        return text

    def notes(self, task, max_len=None):
        """任务的备注（``notes`` / ``note`` / ``备注`` 都认）；没有就是空串。"""
        fields = (task or {}).get("fields") or {}
        for key in NOTES_KEYS:
            text = self._one_line(fields.get(key))
            if text:
                return text[:max_len] if max_len else text
        return ""

    def set_notes(self, task_id, text):
        """写备注（统一写进 ``notes`` 字段；空值 = 清掉）。返回更新后的任务。"""
        return self.modify(task_id, "notes", "" if text is None else str(text))

    def focusmate_title(self, task, max_len=500):
        """给 Focusmate task 用的 title：优先 title/name/摘要字段，否则用整条摘要。"""
        fields = task.get("fields") or {}
        for key in ("title", "name", "summary", "task"):
            val = self._one_line(fields.get(key))
            if val:
                return val[:max_len]
        return (self.summarize(task) or ("Task #%s" % task.get("id")))[:max_len]

    # -- 归档 ---------------------------------------------------------
    def load_archive(self):
        try:
            with open(self.archive, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {"version": 1, "records": []}
        if isinstance(raw, list):
            return {"version": 1, "records": raw}
        if not isinstance(raw, dict):
            return {"version": 1, "records": []}
        records = raw.get("records")
        if not isinstance(records, list):
            records = []
        raw["version"] = raw.get("version") or 1
        raw["records"] = records
        return raw

    def _archive_key(self, rec):
        fm = rec.get("focusmate") or {}
        return (rec.get("task_id"), fm.get("task_id"), fm.get("completed_at"))

    def archive_completed(self, task, remote_task):
        """把一条已完成任务写进归档；同一 (task_id, focusmate_task_id, completed_at) 幂等。"""
        data = self.load_archive()
        rec = {
            "archived_at": _now(),
            "task_id": task.get("id"),
            "fields": dict(task.get("fields") or {}),
            "session": dict(task.get("session") or {}) or None,
            "notes": self.notes(task) or None,
            "focusmate": {
                "task_id": remote_task.get("id") or remote_task.get("taskId"),
                "title": remote_task.get("title"),
                "description": remote_task.get("description"),
                "completed_at": (remote_task.get("completedAt")
                                 or remote_task.get("completed_at")),
                "attachments": remote_task.get("attachments") or [],
            },
        }
        key = self._archive_key(rec)
        existing = {self._archive_key(r) for r in data["records"]
                    if isinstance(r, dict)}
        if key in existing:
            return None
        data["records"].append(rec)
        _atomic_write(self.archive, data)
        return rec

    def archive_local(self, task, reason=None):
        """把「本地认为完成了」记进归档（没有 Focusmate 完成时间的场合）。

        缺漏完成补充就是这种情况：Focusmate 那边可能压根没勾（或者 API 不给勾），
        但用户在收尾里说了「我大概花了 35 分钟做完了」—— 那就要留档，
        而且要能看出**这条是手工补录的**（``manual: true`` + ``actual_minutes``）。
        幂等键 = (task_id, status_at, "manual")。
        """
        data = self.load_archive()
        rec = {
            "archived_at": _now(),
            "task_id": task.get("id"),
            "fields": dict(task.get("fields") or {}),
            "session": dict(task.get("session") or {}) or None,
            "notes": self.notes(task) or None,
            "tags": list(task.get("tags") or []),
            "manual": True,
            "reason": reason or "manual-complete",
            "actual_minutes": task.get("actual_minutes"),
            "status_at": task.get("status_at"),
            "omnifocus": dict(task.get("omnifocus") or {}) or None,
            "focusmate": {
                "task_id": (task.get("focusmate") or {}).get("task_id"),
                "title": self.focusmate_title(task),
                "description": self.notes(task) or None,
                "completed_at": (task.get("focusmate") or {}).get("completed_at"),
                "attachments": [],
            },
        }
        for existing in data["records"]:
            if not isinstance(existing, dict) or not existing.get("manual"):
                continue
            if (existing.get("task_id") == rec["task_id"]
                    and existing.get("status_at") == rec["status_at"]):
                return None
        data["records"].append(rec)
        _atomic_write(self.archive, data)
        return rec

    def reconcile(self, remote_tasks):
        """用 ``GET /v1/tasks`` 的结果补完成记录。

        Focusmate 的 task 列表里 ``completedAt`` 是完成时间；能看到就照抄，看不到就
        不伪造。返回 (新归档条数, 已匹配的 task_id 列表)。
        """
        if isinstance(remote_tasks, dict):
            remote_tasks = (remote_tasks.get("tasks")
                            or remote_tasks.get("data")
                            or [])
        if not isinstance(remote_tasks, list):
            remote_tasks = []
        by_id = {}
        for r in remote_tasks:
            if isinstance(r, dict):
                rid = r.get("id") or r.get("taskId")
                if rid:
                    by_id[str(rid)] = r
        n = 0
        matched = []
        store = self.load()
        dirty = False
        for task in store["tasks"]:
            fm = task.get("focusmate") or {}
            rid = fm.get("task_id") or fm.get("id")
            remote = by_id.get(str(rid)) if rid else None
            if remote:
                matched.append(task["id"])
                done_at = remote.get("completedAt") or remote.get("completed_at")
                if done_at:
                    if fm.get("completed_at") != done_at:
                        fm["completed_at"] = done_at
                        task["focusmate"] = fm
                        task["updated_at"] = _now()
                        dirty = True
                    # 「Focusmate 那边勾了完成」= 这条本地记录收工了。以前这里只归档、
                    # 不改 status，靠 _coerce_task 的老数据兼容规则兜着 —— 结果同一份数
                    # 据「读出来是完成、写回去没有状态」，界面上的 ✓ 时有时无
                    # （用户反馈的「任务状态同步不对」就是它）。这里显式落库。
                    if task.get("status") not in (STATUS_COMPLETED, STATUS_DROPPED):
                        task["status"] = STATUS_COMPLETED
                        task["status_at"] = done_at
                        task["status_reason"] = "focusmate-completed"
                        task["updated_at"] = _now()
                        dirty = True
                    if self.archive_completed(task, remote):
                        n += 1
            # 改挂过场次：旧场次那条 Focusmate task 还在，completedAt 照样要归档，
            # 用当时记下的 session（不能拿现在的，否则归档里日期是错的）。
            for old in (fm.get("previous") or []):
                oid = old.get("task_id")
                remote3 = by_id.get(str(oid)) if oid else None
                if not remote3:
                    continue
                if not (remote3.get("completedAt") or remote3.get("completed_at")):
                    continue
                ghost = dict(task)
                ghost["session"] = old.get("session") or task.get("session")
                if self.archive_completed(ghost, remote3):
                    n += 1
                    if task["id"] not in matched:
                        matched.append(task["id"])
            # 每周重复：每次落点的 Focusmate task 是独立实体，单独对一次
            occ = ((task.get("repeat") or {}).get("occurrences") or {})
            for day, rec in sorted(occ.items()):
                ofm = rec.get("focusmate") or {}
                orid = ofm.get("task_id") or ofm.get("id")
                if not orid:
                    continue
                remote2 = by_id.get(str(orid))
                if not remote2:
                    continue
                done_at = remote2.get("completedAt") or remote2.get("completed_at")
                if not done_at:
                    continue
                if ofm.get("completed_at") == done_at and rec.get("archived_at"):
                    continue
                ofm["completed_at"] = done_at
                rec["focusmate"] = ofm
                rec["archived_at"] = _now()
                occ[day] = rec
                task.setdefault("repeat", {})["occurrences"] = occ
                task["updated_at"] = _now()
                dirty = True
                if task["id"] not in matched:
                    matched.append(task["id"])
                if self.archive_completed(dict(task, session=rec.get("session")), remote2):
                    n += 1
        if dirty:
            self.save(store)
        return n, matched


# ---------------------------------------------------------------- 模块级便捷函数
#
# 不想每次 new 一个 TaskStore 的调用方（CLI / 测试）可以直接用这层。

_DEFAULT = None


def _store():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TaskStore()
    return _DEFAULT


def load_tasks(path=None):
    return TaskStore(path).list() if path else _store().list()


def create_task(fields=None, task_id=None, path=None):
    return TaskStore(path).create(fields=fields, task_id=task_id)


def delete_task(task_id, path=None):
    return TaskStore(path).delete(task_id)


def modify_task(task_id, field, value, path=None):
    return TaskStore(path).modify(task_id, field, value)


def summarize(task, max_len=None):
    return _store().summarize(task, max_len=max_len)


# ---------------------------------------------------------------- CLI（简单 CRUD 脚本）

def _parse_fields(tokens):
    fields = {}
    loose = []
    for tok in tokens or []:
        if "=" in tok:
            k, v = tok.split("=", 1)
            if k:
                fields[k] = v
        else:
            loose.append(tok)
    if loose:
        # `task_store.py create 写论文` 这种写法当作 title；其余空格拼回去。
        fields.setdefault("title", " ".join(loose))
    return fields


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="极简 JSON 任务库（CRUD；字段不校验）")
    ap.add_argument("--file", default=None, help="任务 JSON 路径，默认 data/tasks.json")
    ap.add_argument("--archive", default=None, help="归档 JSON 路径，默认 data/task-archive.json")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="列出任务")
    p.set_defaults(func=lambda a, st: _cli_list(st))

    p = sub.add_parser("create", help="创建：create [--id N] [field=value ...] | create 标题")
    p.add_argument("--id", type=int, default=None, help="显式指定任务 ID（不给就自动）")
    p.add_argument("fields", nargs="*", help="key=value；不含 = 的 token 会拼成 title")
    p.set_defaults(func=lambda a, st: _cli_create(st, a))

    p = sub.add_parser("delete", help="删除：delete ID")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=lambda a, st: _cli_delete(st, a.task_id))

    p = sub.add_parser("modify", help="修改：modify ID FIELD VALUE")
    p.add_argument("task_id", type=int)
    p.add_argument("field")
    p.add_argument("value", nargs="+")
    p.set_defaults(func=lambda a, st: _cli_modify(st, a))

    p = sub.add_parser("note", help="写备注：note ID 文本（可以是网址/多行）")
    p.add_argument("task_id", type=int)
    p.add_argument("text", nargs="*", help="备注内容；不写 = 清空")
    p.set_defaults(func=lambda a, st: _cli_note(st, a))

    p = sub.add_parser("show", help="显示一条任务")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=lambda a, st: _cli_show(st, a.task_id))

    p = sub.add_parser("archive", help="看完成归档（那条只增不改的 JSON）")
    p.add_argument("--limit", type=int, default=0, help="只看最近 N 条（默认全看）")
    p.add_argument("--json", action="store_true", dest="as_json", help="原样打印整个文件")
    p.set_defaults(func=lambda a, st: _cli_archive(st, a))

    p = sub.add_parser("reconcile", help="用 GET /v1/tasks 的 JSON 补归档（不联网）")
    p.add_argument("json_file", help="GET /v1/tasks 的响应 JSON 文件")
    p.set_defaults(func=lambda a, st: _cli_reconcile(st, a.json_file))

    ns = ap.parse_args(argv)
    if not getattr(ns, "func", None):
        ap.print_help()
        return 1
    store = TaskStore(ns.file, ns.archive)
    return ns.func(ns, store) or 0


def _cli_list(store):
    tasks = store.list()
    if not tasks:
        print("（没有任务）")
        return 0
    for t in tasks:
        sess = t.get("session") or {}
        where = ("%s %s" % (sess.get("date") or "?", sess.get("start") or "?")) if sess else "-"
        fm = (t.get("focusmate") or {}).get("task_id") or "-"
        print("%3d  %-22s  session=%-18s focusmate=%s  %s"
              % (t["id"], store.summarize(t, max_len=80), where, fm,
                 "已完成" if (t.get("focusmate") or {}).get("completed_at") else ""))
    return 0


def _cli_create(store, ns):
    toks = list(ns.fields or [])
    explicit = ns.id
    if explicit is None and toks and re.match(r"^\d+$", toks[0]) and "=" not in toks[0]:
        explicit = int(toks.pop(0))
    task = store.create(_parse_fields(toks), task_id=explicit)
    print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0


def _cli_delete(store, task_id):
    task = store.delete(task_id)
    print("已删除任务 %d：%s" % (task["id"], store.summarize(task)))
    return 0


def _cli_modify(store, ns):
    task = store.modify(ns.task_id, ns.field, " ".join(ns.value))
    print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0


def _cli_note(store, ns):
    text = " ".join(ns.text).strip()
    task = store.set_notes(ns.task_id, text)
    print("任务 %d 备注%s：%s" % (task["id"], "已清空" if not text else "",
                               store.notes(task) or "（空）"))
    return 0


def _cli_show(store, task_id):
    print(json.dumps(store.get(task_id), ensure_ascii=False, indent=2))
    return 0


def _cli_archive(store, ns):
    data = store.load_archive()
    records = [r for r in (data.get("records") or []) if isinstance(r, dict)]
    if ns.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("归档文件：%s" % store.archive)
    if not records:
        print("（还没有归档。任务在 Focusmate 上有了 completedAt 之后，"
              "task sync / reconcile 会补进来）")
        return 0
    print("共 %d 条（只增不改；同一 (任务, Focusmate task, 完成时间) 只记一次）\n" % len(records))
    shown = records[-ns.limit:] if ns.limit else records
    by_day = {}
    for rec in shown:
        fm = rec.get("focusmate") or {}
        done = str(fm.get("completed_at") or "")
        day = done[:10] or "（没有完成时间）"
        by_day.setdefault(day, []).append(rec)
    for day in sorted(by_day):
        print("%s  %d 条" % (day, len(by_day[day])))
        for rec in by_day[day]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            fields = rec.get("fields") or {}
            title = (fields.get("title") or fields.get("name")
                     or store._one_line(fields)[:60] or "（空任务）")
            print("  · #%s  %s  ←  session %s %s  · 完成 %s  · 归档 %s"
                  % (rec.get("task_id") or "?", title,
                     sess.get("date") or "?", sess.get("start") or "?",
                     fm.get("completed_at") or "?", rec.get("archived_at") or "?"))
            note = rec.get("notes") or store.notes({"fields": fields})
            if note:
                print("      备注：%s" % note)
    return 0


def _cli_reconcile(store, json_file):
    try:
        with open(json_file, encoding="utf-8") as f:
            remote = json.load(f)
    except (OSError, ValueError) as e:
        print("[error] 读不了 %s：%s" % (json_file, e))
        return 1
    n, matched = store.reconcile(remote)
    print("匹配到 %d 条本地 Focusmate task，新增归档 %d 条。" % (len(matched), n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
