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
RESERVED = {"id", "fields", "created_at", "updated_at", "session", "focusmate"}


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
    task.setdefault("created_at", _now())
    task.setdefault("updated_at", task["created_at"])
    return task


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
    def create(self, fields=None, task_id=None):
        """创建任务。fields 不校验；task_id 可以显式指定（占位/导入场景）。"""
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
            "fields": dict(fields),
            "created_at": stamp,
            "updated_at": stamp,
            "session": None,
            "focusmate": {},
        }
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
        field = str(field)
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

    # -- session / Focusmate -----------------------------------------
    def assign(self, task_id, session):
        """把任务挂到一场 session 上（只写本地；Focusmate 写入由调用方做）。"""
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
        found["session"] = dict(session)
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
        fm.update(dict(focusmate_task or {}))
        if session is not None:
            found["session"] = dict(session)
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
            if not rid:
                continue
            remote = by_id.get(str(rid))
            if not remote:
                continue
            matched.append(task["id"])
            done_at = remote.get("completedAt") or remote.get("completed_at")
            if done_at:
                if fm.get("completed_at") != done_at:
                    fm["completed_at"] = done_at
                    task["focusmate"] = fm
                    task["updated_at"] = _now()
                    dirty = True
                if self.archive_completed(task, remote):
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

    p = sub.add_parser("show", help="显示一条任务")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=lambda a, st: _cli_show(st, a.task_id))

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


def _cli_show(store, task_id):
    print(json.dumps(store.get(task_id), ensure_ascii=False, indent=2))
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
