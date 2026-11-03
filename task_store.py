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
- 任务可以声明成**重复**（``repeat``）：日期用 OmniFocus 的 ICS RRULE
  （``rule`` + ``rule_anchor``）展开，``start`` / ``duration`` 决定落到当天哪一场
  session；老式手写记录仍可按「周几 + 起点 + 时长」匹配。排布被改了（例如 50
  拆成 25+25）就不是同一场了 —— 这一次的重复任务自然消失，但 repeat 设置不变。
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
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
DEFAULT_ARCHIVE_FILE = os.path.join(DATA_DIR, "task-archive.json")

# 这些键是本地簿记，不属于用户字段。用户仍然可以随便写别的字段。
# ``count_credits`` 是「哪一场 session 已经给这条记过 count」的台账（幂等用），
# 它跟 ``history`` 一样是界面/账本用的痕迹，不许当字段改。
RESERVED = {"id", "fields", "created_at", "updated_at", "session", "focusmate", "repeat",
            "status", "status_at", "tags", "omnifocus", "duplicate_of", "actual_minutes",
            "history", "trigger", "formatter", "source", "repeat_parent", "repeat_date",
            "count_credits"}

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

# ---------------------------------------------------------------- 时长 ↔ count
#
# 一个任务「要多久」有两副面孔，**必须永远是同一个事实**：
#
#   count            要花几个 25 分钟的块（最高进度）
#   fields.duration  分钟数
#
# 换算只有一个方向是精确的：count × 25 = 分钟；反过来的 分钟 → count **往上取整**
# （30 分钟的任务 = 2 个 count，不是 1.2 个）。所以「改 count = 改时长、改时长 =
# 改 count」这件事只在 :func:`link_count_fields` 里写一遍，别在调用方各写一份。
#
# ``count_invested`` 是「这条已经用掉几个 count」，只由两种事情推进：
#   * 用户手敲 ``task 3 -c +2``（见 warden 的 ``_apply_count``）
#   * 一场 session 收尾时**自动记账**（session 时长 / 25，见 warden.begin_review）
# 记账是**幂等**的：``count_credits`` 里按 session key 记一笔，同一场收尾重开
# 多少次都只加一次 —— 否则「收尾到一半退出、再进一次」就会白送 count。
MINUTES_PER_COUNT = 25
COUNT_KEY = "count"
COUNT_INVESTED_KEY = "count_invested"
COUNT_CREDITS_KEY = "count_credits"

#: 任务持续时间的字段别名。第一个是推荐写法，其余是为了兼容手写数据。
#: ``task_hooks`` 直接 import 这个名字，别再复制一份。
DURATION_KEYS = ("duration", "duration_minutes", "estimated_minutes",
                 "estimated_duration", "minutes", "时长", "预计时长")

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


# -- 时长 / count 换算（本模块是唯一口径，task_hooks 也从这儿拿） -----------

def parse_minutes(value):
    """从 ``25`` / ``"25"`` / ``"25min"`` / ``"25 分钟"`` 里取分钟数。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        out = int(value)
        return out if out > 0 else None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    out = int(match.group(0))
    return out if out > 0 else None


def parse_count(value):
    """``5`` / ``"5"`` / ``"+5"`` / ``"2 个"`` → 整数；认不出来 → ``None``。

    负数**也认**（``-c -1`` 是「退一格进度」），语义由调用方决定。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"[+-]?\d+", str(value))
    if not match:
        return None
    return int(match.group(0))


def minutes_to_count(minutes, per=MINUTES_PER_COUNT):
    """分钟 → count，**往上取整**（30 分钟 = 2 个 count）；认不出/<=0 → ``None``。"""
    got = parse_minutes(minutes)
    if got is None:
        return None
    per = int(per or MINUTES_PER_COUNT)
    return -(-got // per)


def count_to_minutes(count, per=MINUTES_PER_COUNT):
    """count → 分钟（``5`` → ``125``）；认不出/<=0 → ``None``。"""
    got = parse_count(count)
    if got is None or got <= 0:
        return None
    return got * int(per or MINUTES_PER_COUNT)


def task_minutes(task):
    """任务的持续时间（分钟）；没写 / 写坏了就是 ``None``。"""
    fields = (task or {}).get("fields") or {}
    for key in DURATION_KEYS:
        if key in fields:
            got = parse_minutes(fields.get(key))
            if got is not None:
                return got
    return None


def task_count(task):
    """任务的**最高** count；没设就是 ``None``（不是 0 —— 「没设上限」不是「上限 0」）。"""
    fields = (task or {}).get("fields") or {}
    got = parse_count(fields.get(COUNT_KEY))
    if got is None or got <= 0:
        return None
    return got


def task_count_invested(task):
    """已经用掉多少 count（没记过就是 0）。"""
    fields = (task or {}).get("fields") or {}
    got = parse_count(fields.get(COUNT_INVESTED_KEY))
    return got if got is not None and got > 0 else 0


def count_text(task, delta=0):
    """界面上的 ``(1+2/5)``：已投入 + 这一场会涨的 / 最高；没设 count 就是 ``""``。

    ``delta`` 只在「挂在某一场 session 上」时有意义 = 那一场时长 / 25（往上取整）
    —— 它是**预报**，不是事实：真正记账发生在收尾的时候（见 warden.begin_review）。
    """
    cap = task_count(task)
    invested = task_count_invested(task)
    try:
        extra = int(delta or 0)
    except (TypeError, ValueError):
        extra = 0
    if cap is None:
        if not invested:
            return ""
        return "(%d)" % invested
    if not invested and not extra and str((task or {}).get("status") or STATUS_ACTIVE) != STATUS_ACTIVE:
        # 收工/放弃的任务、一格都没记过：`(0/1)` 只会让人以为「你 1 个都没干」。
        # 有进度的照样显示（那才是「这条当时干了多少块」）。
        return ""
    if extra > 0:
        return "(%d+%d/%d)" % (invested, extra, cap)
    return "(%d/%d)" % (invested, cap)


def link_count_fields(fields, changed=None):
    """让 ``count`` 和 ``duration`` 永远说的是同一件事（原地改 ``fields``）。

    ``changed`` 指**用户这次动的是哪一边** —— 两边的关系不是对称的：

        "count"     用户设了 count → duration = 25 × count（改 count 就是改时长）
        "duration"  用户改了时长 → count = ceil(时长/25)，**时长原样留着**
                    （「45 分钟」是用户说的实话，不能悄悄改成 50）
        "both"      新建任务 → 两条都写了以 count 为准，只写一条就推另一条
        None        读盘 → 只补缺的那一边（两边都有说明是用户/旧版本写的，不动）

    两边都没写就什么都不做 —— 不是每条任务都得有 count，只有估得出几块的才有。
    """
    fields = fields if isinstance(fields, dict) else {}
    has_count = COUNT_KEY in fields
    has_duration = any(key in fields for key in DURATION_KEYS)
    takes_count = (changed == COUNT_KEY
                   or (changed == "both" and has_count))
    if takes_count:
        cap = parse_count(fields.get(COUNT_KEY))
        if cap is None or cap <= 0:
            fields.pop(COUNT_KEY, None)
            return fields
        fields[COUNT_KEY] = cap
        fields[DURATION_KEYS[0]] = count_to_minutes(cap)
        return fields
    wants_count = (changed in DURATION_KEYS
                   or changed == "both"
                   or (changed is None and has_duration and not has_count))
    if wants_count:
        minutes = task_minutes({"fields": fields})
        if minutes is None:
            return fields
        fields[COUNT_KEY] = minutes_to_count(minutes)
        return fields
    if changed is None and has_count and not has_duration:
        # 只有 count、没有时长：补 duration（唯一能补的方向）
        cap = parse_count(fields.get(COUNT_KEY))
        if cap is not None and cap > 0:
            fields[DURATION_KEYS[0]] = count_to_minutes(cap)
    return fields


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


def _backup_before_write(path):
    """留一份**上一版**（``xxx.bak``）：任务库是活数据，写坏了就再也回不去。

    真踩过：一次测试不小心把 ``data/tasks.json`` 当成临时库清空重写，
    旧内容连一个字节都找不回来。备份只留一代（每次写覆盖上一次），
    60KB 的文件不值当为它做轮转。
    """
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            shutil.copyfile(path, path + ".bak")
    except OSError:
        pass


def _empty_store():
    return {"version": 1, "next_id": 1, "tasks": []}


#: formatter / trigger 里有几个键是**某一次执行**的账，不属于规则本身：
#: ``reservation`` / ``result`` / ``applied_at``。重复任务**本体**只保存规则模板，
#: 这些运行时字段必须留在子任务上；否则本体看起来像「已经被排到某一天」。
HOOK_RUNTIME_KEYS = ("result", "reservation", "applied_at")


def _hook_template(spec):
    """把 formatter/trigger 的运行时账摘掉，只留规则模板（本体用）。"""
    if not isinstance(spec, dict):
        return None
    out = {k: v for k, v in spec.items() if k not in HOOK_RUNTIME_KEYS}
    return out or None


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
    # trigger / formatter 是「具名 preset + 参数」的小 dict；坏数据当没挂。
    for hook in ("trigger", "formatter"):
        if hook in task and not isinstance(task.get(hook), dict):
            task[hook] = None
    task["repeat"] = _coerce_repeat(task.get("repeat"))
    task["omnifocus"] = _coerce_omnifocus(task.get("omnifocus"))
    task["tags"] = _coerce_tags(task.get("tags"))
    source = _coerce_source(task.get("source"))
    if source is None and task["omnifocus"]:
        # 老任务只有 omnifocus、没有 source：补一个指针，界面和「本体来源」
        # 不用再分支判断两套字段。
        source = _source_for_omnifocus(task["omnifocus"])
    if source:
        task["source"] = source
    else:
        task.pop("source", None)
    try:
        parent = task.get("repeat_parent")
        if parent is None:
            task.pop("repeat_parent", None)
        else:
            task["repeat_parent"] = int(parent)
    except (TypeError, ValueError):
        task.pop("repeat_parent", None)
    repeat_date = str(task.get("repeat_date") or "").strip()
    if repeat_date:
        task["repeat_date"] = repeat_date
    else:
        task.pop("repeat_date", None)
    # 重复任务**本体**只是模板：formatter 记规则，不记「排到哪一天」。旧数据里
    # 本体曾经被当成普通任务排过（带 reservation / result），读进来就先把运行时
    # 账摘掉，避免界面把本体画成一个已排期的任务。
    if task["repeat"] and not task.get("repeat_parent"):
        template = _hook_template(task.get("formatter"))
        if template:
            task["formatter"] = template
        else:
            task.pop("formatter", None)
        # 本体不挂 session，所以旧的 assign() 留下的 due / due_source 是过期账：
        # 只有 due_source == "session" 才清（用户自己写的 due 保留）。
        if not task.get("session") and task["fields"].get("due_source") == "session":
            task["fields"].pop("due", None)
            task["fields"].pop("due_source", None)
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
    credits = _coerce_credits(task.get(COUNT_CREDITS_KEY))
    if credits:
        task[COUNT_CREDITS_KEY] = credits
    else:
        task.pop(COUNT_CREDITS_KEY, None)
    # 时长 ↔ count 的账：手改过 JSON 的人可能只写了一个。count 是硬上限，
    # 所以「有 count 以 count 为准」，两边都没写就什么都不做。
    link_count_fields(task["fields"], changed=None)
    invested = parse_count(task["fields"].get(COUNT_INVESTED_KEY))
    cap = task_count(task)
    if invested is None or invested <= 0:
        task["fields"].pop(COUNT_INVESTED_KEY, None)
    else:
        task["fields"][COUNT_INVESTED_KEY] = min(invested, cap) if cap else invested
    task.setdefault("created_at", _now())
    task.setdefault("updated_at", task["created_at"])
    return task


def _coerce_credits(raw):
    """``count_credits``：``{session_key: count}``，坏数据当没有。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        got = parse_count(value)
        text = str(key or "").strip()
        if text and got is not None:
            out[text] = got
    return out


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
    """OmniFocus 关联：``{"id": primary key, "url", "title", "linked_at", "writeback", "previous"}``。

    只有 ``id``（primary key）是硬的：链接解析不出来就没有关联，
    绝不拿标题去猜一条任务（猜错比没有更糟）。

    ``previous`` 是「这条本地任务**以前**挂在哪些 primary key 上」——OmniFocus 的
    重复任务每完成一次就会生出新一次（新 key），还开着的本地记录得跟着走
    （见 :meth:`TaskStore.advance_omnifocus`），换下来的旧 key 留档备查。
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
    previous = []
    for item in (raw.get("previous") or []):
        if not isinstance(item, dict):
            continue
        old = str(item.get("id") or "").strip()
        if not old:
            continue
        previous.append({"id": old,
                         "replaced_at": item.get("replaced_at"),
                         "reason": item.get("reason")})
    if previous:
        out["previous"] = previous
    return out


#: 中文「每周几」；只用于 repeat_summary，内部仍用 WEEKDAYS。
CN_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
#: ICS BYDAY 的两字母码 ↔ Python weekday。
RRULE_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

#: rrulestr 构造出来的 RRULE 对象。键 = (rule, rule_anchor iso)：
#: 每天画表 / 每次 sync 都会问「这一天是不是重复日」，不缓存会反复解析。
_RRULE_CACHE = {}


def normalize_rule(raw):
    """把 OmniFocus / ICS 的规则串收敛成 ``FREQ=...`` 形式。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.upper().startswith("RRULE:"):
        text = text[6:].strip()
    return text or None


def parse_iso_datetime(raw):
    """``2026-11-04T11:10:00`` → naive datetime；认不出返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def rule_objects(rule, anchor):
    """``(rule, dtstart)`` → dateutil RRULE；解析不了返回 None。

    这里用 python-dateutil（``icalendar`` 的依赖，项目里本来就有）做真正的
    ICS RRULE 展开，而不是自己拿 ``FREQ=WEEKLY`` 硬编码：OmniFocus 的
    ``repetitionRule.ruleString`` 是标准 ICS 规则，日常规则、多 BYDAY、
    INTERVAL、COUNT / UNTIL 都要能原样照做。
    """
    rule = normalize_rule(rule)
    anchor = (anchor if isinstance(anchor, datetime.datetime)
              else parse_iso_datetime(anchor))
    if not rule or anchor is None:
        return None
    key = (rule, anchor.isoformat(timespec="seconds"))
    if key in _RRULE_CACHE:
        return _RRULE_CACHE[key]
    try:
        from dateutil.rrule import rrulestr
    except ImportError:                                # pragma: no cover - 依赖已随 icalendar 装
        return None
    try:
        rr = rrulestr(rule, dtstart=anchor)
    except (ValueError, TypeError):
        # OmniFocus / ICS 常把 UNTIL 写成 UTC（``...Z``），而我们的 dtstart 是
        # 本地 naive datetime；dateutil 会因此报「naive 和 aware 不能比较」。
        # 去掉 UNTIL 的 Z 再试（本地任务不需要时区级精确，日期一致就够）。
        fallback = re.sub(r"(UNTIL=\d{8}T\d{6})Z", r"\1", rule, flags=re.I)
        if fallback == rule:
            return None
        try:
            rr = rrulestr(fallback, dtstart=anchor)
        except (ValueError, TypeError):
            return None
    _RRULE_CACHE[key] = rr
    return rr


def rule_matches_day(rep, day):
    """这条 ``repeat`` 的 RRULE 是否要求在 ``day`` 起一场。

    返回 ``True`` / ``False``；没有 rule 信息时返回 ``None``（交给老逻辑按
    ``weekday`` 判断）。``rule_offset_days`` 记录「session 起点 - 规则锚点」的
    日期差：跨午夜的规则（due 周四 00:05、场次周三 23:15）要拿周四去问 RRULE。
    """
    if not isinstance(rep, dict):
        return None
    rule = normalize_rule(rep.get("rule"))
    if not rule:
        return None
    anchor = parse_iso_datetime(rep.get("rule_anchor") or rep.get("anchor"))
    rr = rule_objects(rule, anchor)
    if rr is None:
        return False
    try:
        offset = int(rep.get("rule_offset_days") or 0)
    except (TypeError, ValueError):
        offset = 0
    rule_day = day - datetime.timedelta(days=offset)
    start = datetime.datetime.combine(rule_day, datetime.time(0, 0))
    end = start + datetime.timedelta(days=1)
    try:
        return bool(rr.between(start, end, inc=True))
    except (ValueError, OverflowError):
        return False


def _rule_summary(rep):
    """把 ICS 规则翻译成一句人话（不认识就保留原文）。"""
    rule = normalize_rule(rep.get("rule"))
    if not rule:
        return ""
    parts = {}
    for piece in rule.split(";"):
        if "=" in piece:
            key, value = piece.split("=", 1)
            parts[key.strip().upper()] = value.strip().upper()
    freq = parts.get("FREQ", "")
    every = parts.get("INTERVAL", "1")
    try:
        every_n = int(every)
    except (TypeError, ValueError):
        every_n = 1
    if freq == "DAILY":
        return ("每 %d 天" % every_n) if every_n > 1 else "每天"
    if freq == "WEEKLY":
        try:
            offset = int(rep.get("rule_offset_days") or 0)
        except (TypeError, ValueError):
            offset = 0
        days = [x for x in parts.get("BYDAY", "").split(",") if x]
        names = []
        for token in days:
            code = re.sub(r"^[+-]?\d+", "", token).strip().upper()
            if code in RRULE_WEEKDAYS:
                # 跨午夜：due 周四 00:05、场次周三 23:15 → 显示周三，而不是
                # 照抄 RRULE 的 TH 让用户以为复制错了。
                names.append(CN_WEEKDAYS[(RRULE_WEEKDAYS[code] + offset) % 7])
        head = ("每 %d 周" % every_n) if every_n > 1 else "每周"
        if names:
            return head + "、".join(names)
        label = str(rep.get("weekday_label") or "").lower()[:3]
        if label in WEEKDAYS:
            return head + CN_WEEKDAYS[WEEKDAYS.index(label)]
        return head
    if freq == "MONTHLY":
        head = ("每 %d 个月" % every_n) if every_n > 1 else "每月"
        day = parts.get("BYMONTHDAY")
        return (head + " %s 日" % day) if day else head
    if freq == "YEARLY":
        return ("每 %d 年" % every_n) if every_n > 1 else "每年"
    return rule


def repeat_summary(rep):
    """``repeat`` → 给人看的一句话（``每天 10:45（25 min）``）。

    OmniFocus 导入的规则用原 RRULE；老的手动 ``--repeat mon@22:15`` 走
    ``weekday_label``。两种都在表格 / 消息里显示同一套话。
    """
    if not isinstance(rep, dict):
        return ""
    explicit = str(rep.get("summary") or "").strip()
    if explicit:
        return explicit
    start = str(rep.get("start") or "").strip()
    rule_label = _rule_summary(rep)
    if not rule_label:
        label = str(rep.get("weekday_label") or "").strip().lower()[:3]
        rule_label = ("每周" + CN_WEEKDAYS[WEEKDAYS.index(label)]
                      if label in WEEKDAYS else "重复")
    duration = rep.get("duration")
    tail = ("（%s min）" % duration) if duration else ""
    return ("%s %s%s" % (rule_label, start, tail)).strip()


def validate_rule(rule, anchor):
    """导入 / 手改规则前先验一遍；返回 ``(ok, error)``。"""
    rule = normalize_rule(rule)
    if not rule:
        return True, None
    if parse_iso_datetime(anchor) is None:
        return False, "规则缺少可用的锚点日期（rule_anchor）"
    if rule_objects(rule, anchor) is None:
        return False, "RRULE 解析不了：%s" % rule
    return True, None


def _coerce_repeat(raw):
    """把重复设置修成可用形状；坏的就当没设置。"""
    if not isinstance(raw, dict):
        return None
    rule = normalize_rule(raw.get("rule"))
    wd = raw.get("weekday")
    label = None
    weekday = None
    if isinstance(wd, int) and 0 <= wd <= 6:
        weekday = wd
        label = WEEKDAYS[wd]
    else:
        label = str(wd or "").strip().lower()[:3]
        if label in WEEKDAYS:
            weekday = WEEKDAYS.index(label)
        else:
            label = None
    start = str(raw.get("start") or "").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", start):
        return None
    start = "%02d:%02d" % tuple(int(x) for x in start.split(":"))
    if weekday is None:
        # 规则任务可以没有 weekday：RRULE 决定日期，weekday 只剩「首日」
        # 显示用。锚点也缺失时才真的没法用。
        if not rule:
            return None
        anchor_dt = parse_iso_datetime(raw.get("rule_anchor") or raw.get("anchor"))
        if anchor_dt is None:
            return None
        try:
            offset = int(raw.get("rule_offset_days") or 0)
        except (TypeError, ValueError):
            offset = 0
        weekday = (anchor_dt.date() + datetime.timedelta(days=offset)).weekday()
        label = WEEKDAYS[weekday]
    try:
        duration = int(raw.get("duration"))
    except (TypeError, ValueError):
        duration = None
    out = {
        "weekday": weekday,
        "weekday_label": label,
        "start": start,
        "duration": duration,
        "created_at": str(raw.get("created_at") or _now()),
        "occurrences": {},
    }
    if rule:
        out["rule"] = rule
        out["rule_anchor"] = str(raw.get("rule_anchor") or raw.get("anchor") or "")
        try:
            out["rule_offset_days"] = int(raw.get("rule_offset_days") or 0)
        except (TypeError, ValueError):
            out["rule_offset_days"] = 0
        if raw.get("source"):
            out["source"] = str(raw.get("source"))
        if raw.get("summary"):
            out["summary"] = str(raw.get("summary"))
        if isinstance(raw.get("omnifocus"), dict):
            out["omnifocus"] = {str(k): v for k, v in raw["omnifocus"].items()}
    occ = raw.get("occurrences")
    if isinstance(occ, dict):
        for day, rec in occ.items():
            if isinstance(rec, dict):
                out["occurrences"][str(day)] = dict(rec)
    return out


def _coerce_source(raw):
    """任务来源：``{"kind": "omnifocus"|"repeat", ...}``，坏形状当没有。"""
    if isinstance(raw, dict):
        out = {str(k): v for k, v in raw.items() if v is not None}
        return out or None
    text = str(raw or "").strip()
    return {"kind": text} if text else None


def _source_for_omnifocus(omnifocus):
    """OmniFocus 关联 → 统一的 source 指针。"""
    om = omnifocus if isinstance(omnifocus, dict) else {}
    ident = str(om.get("id") or "").strip()
    if not ident:
        return None
    return {"kind": "omnifocus", "id": ident,
            "url": om.get("url") or ("omnifocus:///task/%s" % ident),
            "title": om.get("title")}


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
        _backup_before_write(self.path)
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
               omnifocus=None, source=None, repeat_parent=None, repeat_date=None):
        """创建任务。fields 不校验；task_id 可以显式指定（占位/导入场景）。

        ``tags`` 是本地标签，``omnifocus`` 是 ``{"id": primary key, ...}``
        形式的关联（见 :meth:`link_omnifocus`）。``source`` 是来源指针
        （``omnifocus`` 或 ``repeat``）；``repeat_parent`` / ``repeat_date``
        只出现在重复任务生成的子任务上。
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
            # 建的时候就对齐 count ↔ duration：`task create ... duration=50` 顺手得到
            # count=2，`count=3` 顺手得到 duration=75（见 link_count_fields）。
            "fields": link_count_fields(_norm_fields(fields), changed="both"),
            "created_at": stamp,
            "updated_at": stamp,
            "session": None,
            "focusmate": {},
            "status": (status if status in STATUSES else STATUS_ACTIVE),
            "tags": _coerce_tags(tags),
            "omnifocus": _coerce_omnifocus(omnifocus),
        }
        src = _coerce_source(source)
        if src is None and task["omnifocus"]:
            src = _source_for_omnifocus(task["omnifocus"])
        if src:
            task["source"] = src
        if repeat_parent is not None:
            task["repeat_parent"] = int(repeat_parent)
        if repeat_date:
            task["repeat_date"] = str(repeat_date)
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
        """改一个字段。field 不做白名单校验（用户要任意字段）。

        两个字段是**同一件事的两副面孔**，写完立刻对齐（见 :func:`link_count_fields`）：

            task modify 3 duration 50    → count 也跟着变成 2
            task modify 3 count 5        → duration 也跟着变成 125
        """
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
        if field == COUNT_KEY or field in DURATION_KEYS:
            link_count_fields(found["fields"], changed=field)
        found["updated_at"] = _now()
        self.save(store)
        return found

    # -- count（最高进度 / 已投入）-------------------------------------

    def set_count(self, task_id, count):
        """设定最高 count（duration 跟着变成 25 × count）；``None``/0 = 取消上限。

        返回 ``(task, 旧值, 新值)``。
        """
        cap = parse_count(count)
        before = None

        def apply(task):
            nonlocal before
            before = task_count(task)
            fields = task.setdefault("fields", {})
            if cap is None or cap <= 0:
                fields.pop(COUNT_KEY, None)
                return
            fields[COUNT_KEY] = cap
            fields[DURATION_KEYS[0]] = count_to_minutes(cap)
            # 上限降到了已投入以下：进度也跟着压到上限（进度不许超过最高进度）
            invested = task_count_invested(task)
            if invested > cap:
                fields[COUNT_INVESTED_KEY] = cap

        task = self._mutate(task_id, apply)
        return task, before, task_count(task)

    def add_count(self, task_id, delta):
        """推进（或退回）已投入的 count —— ``-c +2`` / ``-c -1`` 就是它。

        规则（用户原话）：加完不能超过最高进度；**加完正好/超过上限 = 达标**，
        达标由调用方去走「完成任务」那条路（这里只记账，不碰状态）。

        返回 ``{"before", "after", "cap", "maxed", "capped", "task"}``。
        """
        step = parse_count(delta) or 0
        out = {"before": 0, "after": 0, "cap": None, "maxed": False,
               "capped": False, "task": None}

        def apply(task):
            fields = task.setdefault("fields", {})
            cap = task_count(task)
            invested = task_count_invested(task)
            after = invested + step
            if after < 0:
                after = 0
            capped = cap is not None and after > cap
            if capped:
                after = cap                     # 进度不许超过最高进度
            fields[COUNT_INVESTED_KEY] = after
            if after <= 0:
                fields.pop(COUNT_INVESTED_KEY, None)
            out.update(before=invested, after=after, cap=cap, capped=capped,
                       maxed=bool(cap is not None and after >= cap))

        task = self._mutate(task_id, apply)
        out["task"] = task
        return out

    def credit_count(self, task_id, key, delta):
        """一场 session 收尾时给任务记 count（**幂等**：同一个 key 只记一次）。

        ``key`` 是那一场的标识（``postsession.session_key``）；``delta`` 是
        session 时长 / 25。记过就返回 ``None``（调用方据此知道「这次不算新账」）。
        进度同样不会超过最高 count。
        """
        key = str(key or "").strip()
        if not key:
            raise ValueError("记 count 必须带一个场次 key（否则没法去重）")
        step = parse_count(delta) or 0
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        credits = found.setdefault(COUNT_CREDITS_KEY, {})
        if key in credits:
            # 这一场已经记过了（收尾重开第 N 次）：什么都不写，连 updated_at 都不动。
            return None
        fields = found.setdefault("fields", {})
        cap = task_count(found)
        invested = task_count_invested(found)
        after = invested + step
        if after < 0:
            after = 0
        capped = cap is not None and after > cap
        if capped:
            after = cap
        if after > 0:
            fields[COUNT_INVESTED_KEY] = after
        else:
            fields.pop(COUNT_INVESTED_KEY, None)
        credits[key] = step
        found["updated_at"] = _now()
        self.save(store)
        return {"before": invested, "after": after, "cap": cap, "capped": capped,
                "maxed": bool(cap is not None and after >= cap), "task": found}

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

    def settle_focusmate(self, task_id, pending_key="pending"):
        """撤掉「在途」标记 —— 乐观写操作落地之后由调用方调一次。

        乐观操作会先往 ``focusmate`` 里塞一个 ``pending`` 标记（界面上看得出来
        「这条还在后台跑」）；真结果回来之后这个标记就该消失，不然它会一直挂在
        账本里，下次读的人会以为还有件事没落地。
        """
        return self._mutate(
            task_id,
            lambda task: (task.get("focusmate") or {}).pop(pending_key, None))

    def restore(self, task_id, snapshot):
        """把一条任务**整体**放回一份快照 —— 乐观操作的失败回滚用。

        和 :meth:`modify` 的区别：这里是「当这件事没发生过」，所以字段
        整体覆盖（不是合并），多余的新字段也被抹掉。传进来的 ``snapshot``
        一般是操作之前 ``copy.deepcopy(task)`` 拿到的那份。
        """
        import copy as _copy

        if not isinstance(snapshot, dict) or not snapshot:
            raise ValueError("restore 需要一份任务快照（dict）")

        def put_back(task):
            task.clear()
            task.update(_copy.deepcopy(snapshot))

        return self._mutate(task_id, put_back)

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
            # 重复子任务的 source 指向本体；别的任务被关联后 source 指向 OF。
            if not task.get("repeat_parent"):
                task["source"] = _source_for_omnifocus(task["omnifocus"])
        return self._mutate(task_id, apply)

    def unlink_omnifocus(self, task_id):
        def apply(task):
            task["omnifocus"] = None
            if (task.get("source") or {}).get("kind") == "omnifocus":
                task.pop("source", None)
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

    def advance_omnifocus(self, old_pk, new_pk, exclude_task_id=None,
                          reason="repeat-clone"):
        """OmniFocus 那条是**重复任务**：勾掉这一次，它自己生成了「下一次」（新 key）。

        于是「一条 OmniFocus 任务 = 这一次」这件事必须跟着走：**还开着的**本地
        记录（重复任务本体、还没到的子任务、拆出来的复制品）一律改挂新 key。

        不改挂会出两个真事（都踩过）：

        * 下一次写回打在一条**已经完成**的旧任务上，OmniFocus 那边永远勾不掉；
        * 旧 key 在 OmniFocus 里是 completed，而「本地跟着 OmniFocus 标完成」那条
          规则会把**明天**的子任务也带成 ✓ —— 明天的活还没干就没了。

        已经收工/放弃的记录留在旧 key 上：它们说的就是那一次已经结束的活。
        返回改挂过的本地任务 id（空 list = 没人跟着走，比如子任务都是收工的）。
        """
        old_pk = str(old_pk or "").strip()
        new_pk = str(new_pk or "").strip()
        if not old_pk or not new_pk or old_pk == new_pk:
            return []
        store = self.load()
        stamp = _now()
        moved = []
        for task in store["tasks"]:
            if exclude_task_id is not None and task["id"] == int(exclude_task_id):
                continue
            omni = task.get("omnifocus") or {}
            if str(omni.get("id") or "") != old_pk:
                continue
            if not TaskStore.is_open(task):
                continue
            previous = list(omni.get("previous") or [])
            previous.append({"id": old_pk, "replaced_at": stamp, "reason": reason})
            task["omnifocus"] = {
                "id": new_pk,
                "url": "omnifocus:///task/%s" % new_pk,
                "title": omni.get("title"),
                "linked_at": omni.get("linked_at") or stamp,
                "previous": previous,
            }
            source = task.get("source")
            if isinstance(source, dict) and str(source.get("kind")) == "omnifocus":
                task["source"] = dict(source, id=new_pk,
                                      url="omnifocus:///task/%s" % new_pk)
            task["updated_at"] = stamp
            moved.append(task["id"])
        if moved:
            self.save(store)
        return moved

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

    def set_hook(self, task_id, kind, spec):
        """挂 / 清一个 trigger 或 formatter 预设。

        ``spec`` 的形状由 :mod:`task_hooks` 的 ``build_spec`` 决定：
        ``{"name": "end", "params": [...], "args": {...}, ...}``。
        传 ``None`` = 摘掉这个钩子。**只改本地任务库**，不碰 Focusmate。
        """
        kind = str(kind or "").strip().lower()
        if kind not in ("trigger", "formatter"):
            raise ValueError("钩子类型只能是 trigger / formatter；你给的是 %r" % kind)
        if spec is not None and not isinstance(spec, dict):
            raise ValueError("%s 必须是一个 dict（用 task_hooks.build_spec 生成）" % kind)

        def apply(task):
            if spec is None:
                task.pop(kind, None)
            else:
                task[kind] = dict(spec)
        return self._mutate(task_id, apply)

    def clear_hook(self, task_id, kind):
        """摘掉 trigger / formatter。"""
        return self.set_hook(task_id, kind, None)

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

        重复任务的**本体**（模板）永远不算这一场的任务：哪怕旧数据给它留了
        session，也在这里过滤掉；契约表 / 收尾只认 ``repeat_parent`` 子任务。
        """
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        out = []
        for task in self.list():
            if self.is_repeat_template(task):
                continue
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
    def set_repeat(self, task_id, weekday, start, duration=None, rule=None,
                   rule_anchor=None, rule_offset_days=0, source=None, summary=None,
                   meta=None):
        """把任务标成重复：``weekday`` 0-6（或 mon..sun），``start`` HH:MM。

        ``duration`` 是**锚定**用的：只有那一周同一时刻、同一时长的那场 session
        才算这次重复的落点。排布改了（拆分/换时长）就不再匹配 —— 这一周的重复
        任务消失，但 repeat 设置原封不动留着，下一周照旧。

        ``rule`` 是 OmniFocus / ICS 的 ``FREQ=...`` 原文时，日期由 RRULE 展开决定
        （``DAILY`` / 多个 ``BYDAY`` / ``INTERVAL`` / ``COUNT`` / ``UNTIL`` 都照做）；
        ``weekday_label`` 退化成「首个场次是周几」的显示字段。``rule_anchor`` 是
        DTSTART，``rule_offset_days`` 处理跨午夜（due 周四、场次周三）。
        """
        store = self.load()
        found = None
        for task in store["tasks"]:
            if task["id"] == int(task_id):
                found = task
                break
        if found is None:
            raise ValueError("没有任务 %s" % task_id)
        rule = normalize_rule(rule)
        rule_anchor = str(rule_anchor or "").strip()
        if rule:
            ok, why = validate_rule(rule, rule_anchor)
            if not ok:
                raise ValueError(why)
        if isinstance(weekday, int):
            label = WEEKDAYS[weekday % 7]
        else:
            label = str(weekday or "").strip().lower()[:3]
        if label not in WEEKDAYS:
            if not rule:
                raise ValueError("星期几认不出来：%r（mon..sun / 周一..周日）" % (weekday,))
            anchor_dt = parse_iso_datetime(rule_anchor)
            label = WEEKDAYS[anchor_dt.weekday()]
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
        out = {
            "weekday": WEEKDAYS.index(label),
            "weekday_label": label,
            "start": "%02d:%02d" % (hour, minute),
            "duration": duration,
            "created_at": _now(),
            "occurrences": kept,
        }
        if rule:
            try:
                offset = int(rule_offset_days or 0)
            except (TypeError, ValueError):
                offset = 0
            out.update({
                "rule": rule,
                "rule_anchor": rule_anchor,
                "rule_offset_days": offset,
                "source": source or "rule",
            })
            if summary:
                out["summary"] = str(summary)
            if isinstance(meta, dict) and meta:
                out["omnifocus"] = {str(k): v for k, v in meta.items() if v is not None}
        found["repeat"] = out
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
        """所有重复任务；``weekday`` 给了就做一次粗筛。

        老式「每周 mon@22:15」能精确按 weekday 过滤；带 ``rule`` 的任务不能只看
        weekday（DAILY、INTERVAL、多种 BYDAY 都可能，甚至跨午夜），所以一律留下，
        交给 :meth:`repeat_occurrence` 用 RRULE 对具体日期判定。
        """
        out = []
        for task in self.load()["tasks"]:
            rep = task.get("repeat")
            if not rep:
                continue
            if weekday is not None and not rep.get("rule"):
                if rep.get("weekday") != int(weekday) % 7:
                    continue
            out.append(task)
        return out

    @staticmethod
    def is_repeat_template(task):
        """本体：带 ``repeat``、且自己不是某次 occurrence 生成出来的子任务。"""
        return bool((task or {}).get("repeat")) and not (task or {}).get("repeat_parent")

    @staticmethod
    def is_repeat_occurrence(task):
        """某一天由重复本体生成的子任务。"""
        return bool((task or {}).get("repeat_parent"))

    @staticmethod
    def hook_template(spec):
        """formatter / trigger 的规则模板（去掉 reservation / result / applied_at）。"""
        return _hook_template(spec)

    @staticmethod
    def origin_marks(task):
        """契约表格 / 面板用的来源记号：🔁 重复子任务，🔗 OmniFocus 关联。"""
        marks = ""
        if (task or {}).get("repeat_parent"):
            marks += "🔁"
        if TaskStore.omnifocus_id(task):
            marks += "🔗"
        return marks

    @staticmethod
    def repeat_due_on(task, day):
        """只问日期：``day`` 这一天该不该有这条重复任务的 occurrence。

        不看 session、不看起点时长 —— 「这一天具体什么时候」是生成之后挂哪一场
        的事。带 rule 用 RRULE，老记录退回 weekday。
        """
        rep = task.get("repeat") if isinstance(task, dict) else None
        if not rep:
            return False
        matched = rule_matches_day(rep, day)
        if matched is None:
            want_wd = day.weekday() if hasattr(day, "weekday") else int(day) % 7
            return int(rep.get("weekday", -1)) == want_wd
        return bool(matched)

    @staticmethod
    def repeat_occurrence(task, day, sessions):
        """这个任务在 ``day`` 那天落在哪一场上；落不上返回 None。

        ``sessions`` 是那天**排好的** session（``start`` / ``duration`` 够用）。
        日期判定优先走 ``repeat.rule``（原生 ICS RRULE，和 OmniFocus 一样）；
        老式记录没有 rule 时退回「同一星期几 + 同一时刻 + 同一时长」。
        最后都要在真实 session 里找到同起点、同时长的场次才算落上。
        """
        rep = task.get("repeat") if isinstance(task, dict) else None
        if not rep or not TaskStore.repeat_due_on(task, day):
            return None
        for sess in sessions or []:
            if sess.get("start") != rep.get("start"):
                continue
            if rep.get("duration") is not None and int(sess.get("duration") or 0) != int(rep["duration"]):
                continue
            return sess
        return None

    def occurrence_for(self, parent_id, day):
        """本体 ``parent_id`` 在 ``day`` 已生成的那条子任务；没有返回 None。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        try:
            want = int(parent_id)
        except (TypeError, ValueError):
            return None
        for task in self.list():
            if task.get("repeat_parent") == want and task.get("repeat_date") == iso:
                return task
        return None

    def create_repeat_occurrence(self, parent_id, day, session=None):
        """由重复本体生成当天的子任务（本地；Focusmate 由调用方另做）。

        - 子任务复制本体的 fields / tags / OmniFocus 关联；
        - ``repeat_parent`` 指向本体，``repeat_date`` 是这一天；
        - ``source`` 写成 ``{"kind": "repeat", "parent": ...}``；
        - **继承 formatter 规则模板**（不带本体上的 reservation / result）；
        - 先不复制本体的 ``repeat``：子任务本身不再是一套规则。
        """
        parent = self.get(parent_id)
        if not self.is_repeat_template(parent):
            raise ValueError("任务 %s 不是重复任务本体" % parent_id)
        exists = self.occurrence_for(parent["id"], day)
        if exists:
            return exists
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        child_fields = dict(parent.get("fields") or {})
        # due / due_source 属于之前那一次；子任务挂上 session 时由 assign() 重写。
        child_fields.pop("due", None)
        child_fields.pop("due_source", None)
        # 新的一次是**新的账**：本体的 writeback 痕迹（写回过了）和 previous
        # （以前挂过哪些 primary key）不该跟着抄 —— 抄了的话这条子任务会被判成
        # 「已经写回过了」，OmniFocus 那边就永远不会被勾掉。
        child_omni = None
        if parent.get("omnifocus"):
            child_omni = {k: v for k, v in parent["omnifocus"].items()
                          if k not in ("writeback", "previous")}
        child = self.create(
            child_fields,
            tags=list(parent.get("tags") or []),
            omnifocus=child_omni,
            source={"kind": "repeat", "parent": parent["id"], "date": iso},
            repeat_parent=parent["id"], repeat_date=iso,
        )
        template = self.hook_template(parent.get("formatter"))
        if template:
            child = self._mutate(child["id"], lambda t: t.update({"formatter": dict(template)}))
        if session:
            child = self.assign(child["id"], session)
        return child

    def record_occurrence(self, task_id, day, child_id=None, session=None,
                          focusmate=None, note=None):
        """只记本体 ``repeat.occurrences[日期]``，**不动本体自己的 session**。"""
        iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
        def apply(task):
            rep = task.get("repeat") or {}
            occ = rep.setdefault("occurrences", {})
            rec = dict(occ.get(iso) or {})
            if child_id is not None:
                rec["child_id"] = int(child_id)
            if session is not None:
                rec["session"] = dict(session or {})
            if focusmate is not None:
                rec["focusmate"] = dict(focusmate or {})
            if note:
                rec["note"] = str(note)
            rec["updated_at"] = _now()
            occ[iso] = rec
            rep["occurrences"] = occ
            task["repeat"] = rep
        return self._mutate(task_id, apply)

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
            # 重复任务：每次落点的 Focusmate task 是独立实体，单独对一次。
            # 新模型里 occurrence 只是本体的账本，真正的任务条是 child_id
            # 指向的子任务 —— 它已经在上面的主循环里归档过了，别再拿本体
            # 的 occurrence 重复归档一份。
            occ = ((task.get("repeat") or {}).get("occurrences") or {})
            for day, rec in sorted(occ.items()):
                if rec.get("child_id"):
                    continue
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
