#!/usr/bin/env python3
"""task_hooks.py —— 任务的 trigger / formatter 扩展点。

任务库本身只存数据、不执行算法。这里把「什么时候该创建一条任务」（trigger）
和「这条任务在这天怎么占时间」（formatter）都做成**具名预设函数**：

    task 3 -t <trigger> [参数...]      ← 给任务挂一个 create trigger
    task 3 -f <formatter> [参数...]    ← 给任务挂一个排队算法；end 会当场执行

两条设计原则
------------
1. **预设函数是唯一入口**。命令里存下来的只是 ``{"name": ..., "params": ...,
   "args": ...}``，执行时再按名字查表。没有注册的函数一律报错，绝不猜测。
2. **自动排期只排明天及以后**。``end`` 这类 formatter 的算法本身只管「哪一段
   时间可用」；调用方（trigger runner）负责传 ``allow_today=False``。这样
   「昨天（晚上）排好、第二天绝不临时加任务」是一条调用约定，而不是散落在
   各个函数里的 if。用户手动敲 ``task 3 -f end`` 是例外。

trigger
-------
本模块默认**一个 trigger 都没有**（``TRIGGERS == {}``）。这是有意的：先把
框架和命令留出来，等某个具体习惯真的需要了再注册，不在空壳里编一套假函数。
以后注册只需要：

    @task_hooks.register_trigger("add_after_task", params=("task_id",),
                                  help="task_id 之后的下一个 session 再建一条")
    def add_after_task(ctx, task_id):
        ...

formatter
---------
当前只有 ``end``：找**能装下任务时长的最小 session**，在当天可用时间窗的最后
截出 ``时长 + 5 分钟休息`` 这一段，把任务钉进去，并把这段标成 reserved，
后续 formatter 不会再用它。

时间口径全部用 ``HH:MM`` 字符串 + ``start_min`` / ``end_min`` 分钟数，和
:mod:`session_plan` 一致；本模块不联网、不写盘。
"""
from __future__ import annotations

import datetime
import re

#: trigger / formatter 的注册表。值是内部的小 dict：name / params / help / func。
TRIGGERS = {}
FORMATTERS = {}

#: ``end`` 默认在任务时长之外多留的休息分钟数。
DEFAULT_BREAK_MINUTES = 5

#: 任务时长的字段别名。第一个是推荐写法，其余是为了兼容手写数据。
DURATION_KEYS = ("duration", "duration_minutes", "estimated_minutes",
                 "estimated_duration", "minutes", "时长", "预计时长")


class HookError(ValueError):
    """trigger / formatter 的用法错误（参数不对、没有时长、没有可用时间……）。"""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 注册表


def register_trigger(name, params=(), help="", func=None):
    """注册一个 trigger 预设函数。

    可以当装饰器用，也可以直接传 ``func=``。``params`` 是参数名序列，用来把
    位置参数绑定成关键字参数；调用时还会传一个 ``ctx`` 字典。
    """
    return _register(TRIGGERS, "trigger", name, params=params, help=help, func=func)


def register_formatter(name, params=(), help="", func=None):
    """注册一个 formatter 预设函数（签名同 trigger）。"""
    return _register(FORMATTERS, "formatter", name, params=params, help=help, func=func)


def _register(table, kind, name, params=(), help="", func=None):
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("%s 名字不能为空" % kind)
    if not re.match(r"^[a-z][a-z0-9_]*$", key):
        raise ValueError("%s 名字只能是小写字母/数字/下划线：%r" % (kind, name))
    params = tuple(str(p) for p in (params or ()))
    rec = {"name": key, "kind": kind, "params": params, "help": str(help or ""),
           "func": func}
    if func is None:
        def decorator(fn):
            rec["func"] = fn
            table[key] = rec
            return fn
        return decorator
    table[key] = rec
    return func


def table_for(kind):
    kind = str(kind or "").strip().lower()
    if kind == "trigger":
        return TRIGGERS
    if kind == "formatter":
        return FORMATTERS
    raise HookError("未知钩子类型 %r（只认 trigger / formatter）" % kind)


def preset_names(kind):
    """当前可用的预设函数名（按注册顺序）。"""
    return tuple(table_for(kind))


def describe_presets(kind):
    """``[(name, params, help), ...]`` —— 给 ``task -t`` / ``task -f`` 列表用。"""
    return [(rec["name"], tuple(rec.get("params") or ()), rec.get("help") or "")
            for rec in table_for(kind).values()]


def build_spec(kind, tokens):
    """把 ``["end", "break=5"]`` 变成要落在任务上的 spec。

    * 第一个词必须是已注册的预设函数；
    * 后面 ``key=value`` 绑定命名参数，裸词按预设声明的 ``params`` 顺序绑定；
    * 多余 / 未知参数直接报错 —— 自动排期里「悄悄忽略一个参数」比拒绝更危险。
    """
    kind = str(kind or "").strip().lower()
    tokens = [str(t) for t in (tokens or []) if str(t).strip()]
    if not tokens:
        raise HookError("%s 后面要跟预设函数名（先敲 `task -%s` 看列表）"
                        % (kind, "t" if kind == "trigger" else "f"))
    name = tokens[0].strip().lower()
    table = table_for(kind)
    rec = table.get(name)
    if rec is None:
        known = "、".join(table) or "（空）"
        raise HookError("没有 %s 预设 %r；当前可用：%s" % (kind, name, known))
    declared = list(rec.get("params") or ())
    args = {}
    positional = []
    for token in tokens[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip().lower()
            if not key:
                raise HookError("参数名不能为空：%r" % token)
            args[key] = value
        else:
            positional.append(token)
    if len(positional) > len(declared):
        raise HookError("%s %s 只接受 %d 个位置参数（%s）；你多给了 %r"
                        % (kind, name, len(declared),
                           "、".join(declared) or "无", positional[len(declared):]))
    for key, value in zip(declared, positional):
        if key in args:
            raise HookError("参数 %s 给了两次（位置参数 + key=value）" % key)
        args[key] = value
    allowed = set(declared)
    unknown = [key for key in args if key not in allowed]
    if unknown:
        raise HookError("%s %s 不认参数 %s；可用：%s"
                        % (kind, name, "、".join(unknown),
                           "、".join(declared) or "（无）"))
    return {"name": name, "kind": kind, "params": tokens[1:], "args": args,
            "set_at": _now()}


def call_preset(kind, spec, ctx):
    """按 spec 调用预设函数。目前 trigger 没有执行器，formatter 由 warden 调。"""
    kind = str(kind or "").strip().lower()
    spec = spec or {}
    name = str(spec.get("name") or "").strip().lower()
    rec = table_for(kind).get(name)
    if rec is None:
        raise HookError("没有 %s 预设 %r" % (kind, name))
    fn = rec.get("func")
    if fn is None:
        raise HookError("%s 预设 %r 还没有函数实现" % (kind, name))
    return fn(dict(ctx or {}), **dict(spec.get("args") or {}))


# ---------------------------------------------------------------- 任务时长


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


def task_duration(task):
    """任务的持续时间（分钟）；没写 / 写坏了就是 ``None``。"""
    fields = (task or {}).get("fields") or {}
    for key in DURATION_KEYS:
        if key in fields:
            got = parse_minutes(fields.get(key))
            if got is not None:
                return got
    return None


# ---------------------------------------------------------------- 时间工具


def parse_hhmm(value):
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def fmt_minutes(total):
    total = int(total) % (24 * 60)
    return "%02d:%02d" % (total // 60, total % 60)


def _clock_minutes(raw):
    """``HH:MM`` → 分钟数，**允许 24:xx**（= 第二天的 00:xx）。

    跨午夜的时段在日历数据里有两种写法：``23:15–00:05`` 和 ``23:15–24:05``。
    :func:`parse_hhmm` 是给「用户输入一个时刻」用的，只认 00:00–23:59；这里多认
    一种 24:05，免得跨午夜那一段被读成「终点 00:05 比起点 23:15 还早」的坏数据。
    """
    got = parse_hhmm(raw)
    if got is not None:
        return got
    match = re.match(r"^24:([0-5]\d)$", str(raw if raw is not None else "").strip())
    if match:
        return 24 * 60 + int(match.group(1))
    return None


def _range_of(rec):
    """一条 reservation / session / 时段 → ``(start_min, end_min)``；坏数据 None。

    终点**早于**起点就当成跨了午夜（+24h）：23:15–00:05 读成 ``(1395, 1445)``，
    而不是 ``(1395, 5)``。formatter 的区间判断（谁占了时间、时间窗尾部在哪）全靠
    这个归一，错了就会把「当天最后一段」认成倒数第二段。
    """
    start = rec.get("start_min")
    end = rec.get("end_min")
    if start is None:
        start = _clock_minutes(rec.get("start"))
    if end is None:
        end = _clock_minutes(rec.get("end"))
    if start is None or end is None:
        return None
    start, end = int(start), int(end)
    if end <= start:
        end += 24 * 60
    return start, end


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def reservation_range(task, reservation):
    """这条任务占掉的区间 ``(start_min, end_min)``；认不出来就 None。

    正常记录里 ``reservation`` 自己就带 ``start`` / ``end``。老记录（或人手改过
    的）可能只剩 ``date`` + ``start``，这时退回这条任务自己的 session、再不行就
    用「start + 任务时长」——「已经排过的任务不占时间」会让下一条任务被排到同一
    段上，两个人在同一场 Focusmate 里撞车。
    """
    got = _range_of(reservation)
    if got is not None:
        return got
    got = _range_of((task or {}).get("session") or {})
    if got is not None:
        return got
    start = _clock_minutes((reservation or {}).get("start"))
    span = task_duration(task)
    if start is None or not span:
        return None
    return start, start + int(span)


def collect_reservations(tasks, day_iso=None):
    """从任务库里收集 formatter 已经占掉的时间段。

    只有 **active** 任务算数：完成 / 放弃的任务不该继续占着当天的时间。
    """
    out = []
    for task in tasks or ():
        if (task.get("status") or "active") != "active":
            continue
        formatter = task.get("formatter") or {}
        reservation = formatter.get("reservation") or {}
        if not reservation:
            continue
        if day_iso and reservation.get("date") != day_iso:
            continue
        got = reservation_range(task, reservation)
        if got is None:
            continue
        rec = dict(reservation)
        rec["task_id"] = task.get("id")
        rec["start_min"], rec["end_min"] = got
        out.append(rec)
    return out


def is_free(session, reservations):
    got = _range_of(session)
    if got is None:
        return False
    for reservation in reservations or ():
        other = _range_of(reservation)
        if other is not None and overlaps(got, other):
            return False
    return True


# ---------------------------------------------------------------- formatter: end


@register_formatter(
    "end",
    params=("break_minutes", "date"),
    help="当天最后一段可用时间里，找装得下任务时长的最小 session；"
         "把「任务时长 + 休息」截在时间窗尾部，并自动挂上去")
def format_end(ctx, break_minutes=DEFAULT_BREAK_MINUTES, date=None):
    """``task <id> -f end`` 的算法。

    输入 ``ctx``：

    * ``task``：任务对象（必须能从 fields 里读到 duration）
    * ``sessions``：候选 session（``date/start/end/duration/period_id/...``）
    * ``periods``：可选的时段表，用来把 reservation 的终点卡在时段结束
    * ``reservations``：已经被别的 formatter 占掉的区间
    * ``task_id``：当前任务 id（重排自己时忽略自己的旧 reservation）

    输出一个 dict：``{"name": "end", "reservation": {...}, "session": {...}}``。
    选择顺序：先取**当天最后一个还有空位的时段**（这就是 end）；同一时段里
    取能装下任务时长的最小 session；同样大小时取更靠后的那一场。

    reservation 的 ``end`` 是 ``min(时段终点, session 终点 + 休息)``，
    ``start = end - (任务时长 + 休息)``（不早于 session 起点）—— 例如
    T4 11:00–12:00 排成 25+25 时，任务会拿走 11:30–12:00
    （25 分钟 session + 5 分钟休息）。
    """
    task = ctx.get("task") or {}
    duration = task_duration(task)
    if duration is None:
        raise HookError(
            "formatter end 需要任务的持续时间，但这%s没有 duration。先补一个，例如："
            "`task modify %s duration 25`（也认 duration_minutes / minutes / 时长）"
            % ("条任务", task.get("id", "N")))
    break_got = parse_minutes(break_minutes)
    if break_got is None:
        raise HookError("formatter end 的 break_minutes 必须是正整数；你给的是 %r"
                        % (break_minutes,))

    sessions = [dict(s) for s in (ctx.get("sessions") or ()) if isinstance(s, dict)]
    if not sessions:
        raise HookError("formatter end：这一天没有任何已排的 session，没法排期。")

    # 时段终点必须是**绝对分钟**（相对时段起点归一），否则 23:15–00:05 会算成 5 分钟，
    # 「当天最后一段」就永远轮不到它。_range_of 已经处理了跨午夜。
    period_end = {}
    for period in ctx.get("periods") or ():
        if not isinstance(period, dict):
            continue
        got = _range_of(period)
        if got is not None:
            period_end[period.get("id")] = got[1]

    task_id = ctx.get("task_id", task.get("id"))
    reservations = []
    for rec in ctx.get("reservations") or ():
        if task_id is not None and rec.get("task_id") == task_id:
            continue                       # 自己上一次的 reservation 不算占用
        reservations.append(rec)

    candidates = []
    for session in sessions:
        got = _range_of(session)
        if got is None:
            continue
        sess_start, sess_end = got
        sess_duration = parse_minutes(session.get("duration"))
        if sess_duration is None:
            sess_duration = sess_end - sess_start
        if sess_duration < duration:
            continue
        if not is_free(session, reservations):
            continue
        period_end_min = period_end.get(session.get("period_id"))
        if period_end_min is None:
            period_end_min = sess_end + break_got
        # 这个 session 真正能占到的窗口尾 = min(时段终点, session 终点 + 休息)。
        window_end = min(int(period_end_min), int(sess_end) + break_got)
        if window_end <= sess_start + int(duration):
            # 时段给的终点比「起点 + 任务时长」还早（坏 layout / 跨午夜没认出来）：
            # 退回 session 终点 + 休息，绝不让 reservation 反过来（start > end）。
            window_end = int(sess_end) + break_got
        candidates.append((window_end, sess_duration, sess_start, sess_end,
                           session, period_end_min))
    if not candidates:
        raise HookError(
            "formatter end：%s 没有空闲的 session 能装下 %d 分钟（已有 %d 段被占）。"
            % ((date or ctx.get("date") or "这一天"), duration, len(reservations)))

    # end 的第一优先级是「当天最后一个还有空位的时段」；同一窗口里再取
    # 能装下任务的最小 session，同样大小时取更靠后的那一场。
    candidates.sort(key=lambda row: (-row[0], row[1], -row[3], -row[2]))
    window_end, _dur, sess_start, sess_end, chosen, period_end_min = candidates[0]
    span = int(duration) + int(break_got)
    reserve_end = int(window_end)
    reserve_start = max(int(sess_start), reserve_end - span)
    if reserve_start >= reserve_end:                 # 兜底：至少别落成空区间
        reserve_start = max(int(sess_start), reserve_end - int(duration))
    start_min, end_min = int(sess_start), int(sess_end)
    reservation = {
        "date": chosen.get("date") or ctx.get("date"),
        "period_id": chosen.get("period_id"),
        "start": fmt_minutes(reserve_start),
        "end": fmt_minutes(reserve_end),
        "start_min": int(reserve_start),
        "end_min": int(reserve_end),
        "session_start": fmt_minutes(start_min),
        "session_end": fmt_minutes(end_min),
        "session_duration": int(_dur),
        "duration": int(duration),
        "break_minutes": int(break_got),
        "task_id": task_id,
        "created_at": _now(),
    }
    session = {
        "date": chosen.get("date") or ctx.get("date"),
        "start": fmt_minutes(start_min),
        "end": fmt_minutes(end_min),
        "duration": int(_dur),
        "period_id": chosen.get("period_id"),
        "period_start": chosen.get("period_start"),
        "period_end": chosen.get("period_end"),
    }
    return {"name": "end", "reservation": reservation, "session": session,
            "created_at": _now()}


# ---------------------------------------------------------------- 目标日期


def resolve_target_day(selected_day, now=None, params=None, allow_today=True):
    """formatter 排哪一天。

    * ``date=YYYY-MM-DD`` / ``day=...`` 显式指定时永远优先；
    * 否则用当前选中的那天；
    * ``allow_today=False`` 时（自动 trigger runner 必须这样传）不会排今天：
      如果选中的是今天或过去，就推到明天。用户手动敲命令时不传这个开关，
      所以「手动加」仍然可以排今天。
    """
    params = dict(params or {})
    raw = params.get("date") or params.get("day")
    now = now or datetime.datetime.now()
    today = now.date() if hasattr(now, "date") else now
    if raw:
        text = str(raw).strip().lower()
        if text in ("today", "今天"):
            day = today
        elif text in ("tomorrow", "明天", "next"):
            day = today + datetime.timedelta(days=1)
        else:
            try:
                day = datetime.date.fromisoformat(text)
            except ValueError as exc:
                raise HookError("date 得是 YYYY-MM-DD / today / tomorrow；你给的是 %r"
                                % raw) from exc
    else:
        day = selected_day
        if hasattr(day, "date"):
            day = day.date()
        if day is None:
            day = today
    if not allow_today and day <= today:
        day = today + datetime.timedelta(days=1)
    return day
