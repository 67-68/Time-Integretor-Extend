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
当前只有 ``end``：取**当天最后一个还有空位的时段**，在它的尾巴上**切一场任务
大小的 session**（时长取 Focusmate 真能订的 25/50/75，起点落 15 分钟整点），
把「时长 + 5 分钟休息」标成 reserved，后续 formatter 不会再用它。

这一层**只管算、不写盘**：切完把「那个时段的前半截该长什么样」（``cut_prefix``）
和「切出来的这一场」（``session``）一起交出去，由 warden 去剪断排布
（``_cut_period_for_carve``：取消旧的 50 分钟场 + 改写 layout + 重算 data.csv）。
排布表达不了的边角（中间隔了 5/20 分钟那种）才额外补一场一次性 session
（``warden._ensure_carved_session``）。

独占（``exclusive``）也在这里落地：:func:`collect_reservations` 会把独占任务的
占用扩展成**整场** session，所以「一场 session 只能有一条任务」不是靠各处的
if，而是靠「这段被占了」这一个事实。

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

#: Focusmate 只卖这三种长度的 session。formatter 切割出来的场次必须落在这几个值上，
#: 否则「排一场 30 分钟的 Focusmate session」就是一句空话 —— 订不出来。
FOCUSMATE_DURATIONS = (25, 50, 75)

#: ``exclusive`` 的字段别名 / 真值写法（``task 3 -e true`` 落成 ``fields.exclusive``）。
EXCLUSIVE_KEYS = ("exclusive", "独占")
TRUTHY_WORDS = ("1", "true", "yes", "y", "on", "t", "是", "开", "真", "独占")
FALSEY_WORDS = ("0", "false", "no", "n", "off", "f", "否", "关", "假", "共享")


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


def parse_flag(value):
    """``true/false/是/否/1/0`` → bool；认不出来就 ``None``（调用方负责报错）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUTHY_WORDS:
        return True
    if text in FALSEY_WORDS:
        return False
    return None


def is_exclusive(task):
    """这条任务是不是 **独占** 的（``fields.exclusive`` / 顶层 ``exclusive``）。

    独占 = 它那一场 session 只能有它一条任务。理由很实际：一场 Focusmate 50 分钟
    里塞两条任务，等于两个「专注块」被切成四段，谁都没真专注过。
    """
    task = task or {}
    for holder in (task.get("fields") or {}, task):
        for key in EXCLUSIVE_KEYS:
            if key in holder:
                got = parse_flag(holder.get(key))
                if got is not None:
                    return got
    return False


def fit_session_duration(minutes):
    """任务时长 → 能订到的 session 长度（25/50/75；超过 75 就往上取 15 的整数倍）。"""
    span = parse_minutes(minutes)
    if span is None:
        return None
    for size in FOCUSMATE_DURATIONS:
        if size >= span:
            return size
    return span + (-span % 15)


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


def exclusive_block(task):
    """独占任务真正占掉的那一段：它自己的窗口 **∪** 它挂着的那一**整场** session。

    「一场 session 不能有两条任务」落在数据上就是这条：独占任务的占用必须盖满
    **它那一场**，而不是只盖住它自己那 25 分钟 —— 否则同一场剩下的 25 分钟还会
    被下一条任务排进去（那正是「一场 Focusmate 里塞两条任务」的成因）。

    注意只跟自己那一场求并，**不**蔓延到前后相邻的 session：一场的身份是
    「哪一天 + 几点开始」，15:00 那场和 15:30 那场是两场，各放一条完全合法。
    """
    task = task or {}
    reservation = ((task.get("formatter") or {}).get("reservation")) or {}
    got = reservation_range(task, reservation)
    span = _range_of((task or {}).get("session") or {})
    if got is None:
        return span
    if span is not None and overlaps(got, span):
        return min(got[0], span[0]), max(got[1], span[1])
    return got


def collect_reservations(tasks, day_iso=None):
    """从任务库里收集 formatter 已经占掉的时间段。

    只有 **active** 任务算数：完成 / 放弃的任务不该继续占着当天的时间。
    **独占**（``fields.exclusive``）的任务额外把整场 session 都算成占用（见
    :func:`exclusive_block`）—— 这就是「一个 session 不能有两个任务」的执行点。
    """
    out = []
    for task in tasks or ():
        if (task.get("status") or "active") != "active":
            continue
        formatter = task.get("formatter") or {}
        reservation = formatter.get("reservation") or {}
        sess = task.get("session") or {}
        exclusive = is_exclusive(task)
        if not reservation and not (exclusive and sess):
            continue
        if day_iso:
            day = str(reservation.get("date") or sess.get("date") or "")
            if day != day_iso:
                continue
        got = reservation_range(task, reservation)
        if got is None:
            continue
        rec = dict(reservation)
        rec["task_id"] = task.get("id")
        rec["start_min"], rec["end_min"] = got
        rec["exclusive"] = exclusive
        if exclusive:
            block = exclusive_block(task)
            if block is not None:
                rec["start_min"], rec["end_min"] = block
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


def grid_floor(minutes, step=15):
    """往下取整到 :00/:15/:30/:45（Focusmate 只认 15 分钟整点开场）。"""
    total = int(minutes)
    step = max(1, int(step))
    return total - (total % step)


@register_formatter(
    "end",
    params=("break_minutes", "date"),
    help="当天最后一段可用时间里**切一场属于这条任务的 session**（时长取 25/50/75），"
         "并自动挂上去；切出来的这场就是「额外创建」的那一场")
def format_end(ctx, break_minutes=DEFAULT_BREAK_MINUTES, date=None):
    """``task <id> -f end`` 的算法。

    输入 ``ctx``：

    * ``task``：任务对象（必须能从 fields 里读到 duration）
    * ``sessions``：候选 session（``date/start/end/duration/period_id/...``）
    * ``periods``：时段表；**每个时段的最后一场**才拿时段终点当窗口尾
    * ``reservations``：已经被别的 formatter 占掉的区间
    * ``task_id``：当前任务 id（重排自己时忽略自己的旧 reservation）

    输出 ``{"name": "end", "reservation": {...}, "session": {...},
    "new_session": bool, "conflicts": [...]}``。

    排法（用户原话：11:00–12:00 有时间段，就该切 11:30–12:00 做一场 25 分钟的，
    **不是**塞进 11:00–11:50 那场 50 分钟的）：

    1. 取**当天最后一个还有空位的时段**（这就是 ``end``），窗口尾 = 时段终点
       （跨午夜也认，:func:`_range_of` 已经归一到绝对分钟）；
    2. 在这个窗口尾**切一场任务大小的 session**：时长取 25/50/75 里最小能装下任务
       的那个（Focusmate 只卖这三档），起点往下取整到 15 分钟整点，再留
       ``break_minutes`` 分钟休息 —— 11:00–12:00 的时段、25 分钟任务 ⇒
       切出 11:30–11:55（占用写 11:30–12:00）；
    3. 切出来的那场如果和已有 session 同起点、**同时长**就复用（``existing``，
       排布一个字都不用改），否则 ``new_session`` 为真，调用方要**剪断**那个时段：
       ``cut_prefix`` 是「整场都在切点之前」的那些场次（再用 50→25 把缝补上），
       于是原来那场 50 分钟会消失，当天只剩「11:00–11:25 + 11:30–11:55」两场。

    同一时段里**不是最后一场**的 session 仍按老口径：任务从这场起点开始占
    「时长 + 休息」，绝不越过这场去抢下一场的时间。所以同一天多条任务各有各的
    一段，而不是挤进同一场。
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

    # 时段区间必须是**绝对分钟**（相对时段起点归一），否则 23:15–00:05 会算成 5 分钟，
    # 「当天最后一段」就永远轮不到它。_range_of 已经处理了跨午夜。
    period_range = {}
    for period in ctx.get("periods") or ():
        if not isinstance(period, dict):
            continue
        got = _range_of(period)
        if got is not None:
            period_range[period.get("id")] = got

    task_id = ctx.get("task_id", task.get("id"))
    reservations = []
    for rec in ctx.get("reservations") or ():
        if task_id is not None and rec.get("task_id") == task_id:
            continue                       # 自己上一次的 reservation 不算占用
        reservations.append(rec)

    fit = fit_session_duration(duration)

    # 每个时段里**起点最靠后的那一场** = 这个时段的尾巴（end 要的就是它）。
    last_start = {}
    for session in sessions:
        pid = session.get("period_id")
        got = _range_of(session)
        if not pid or got is None:
            continue
        if pid not in last_start or got[0] > last_start[pid]:
            last_start[pid] = got[0]

    candidates = []
    for session in sessions:
        got = _range_of(session)
        if got is None:
            continue
        sess_start, sess_end = got
        pid = session.get("period_id")
        sess_duration = parse_minutes(session.get("duration"))
        if sess_duration is None:
            sess_duration = sess_end - sess_start
        tail = bool(pid) and pid in period_range and last_start.get(pid) == sess_start
        cut_prefix = None
        if tail:
            # 时段尾巴：窗口尾 = 时段终点（「切割」就从这儿往回量）。
            window_end = int(period_range[pid][1])
            lower = int(period_range[pid][0])
            carve_start = grid_floor(window_end - int(break_got) - int(fit))
            if carve_start < lower:
                # 时段太短，切不出任务大小的场次：退到这一场自己的尾巴上。
                carve_start = grid_floor(int(sess_end) - int(fit))
                if carve_start < int(sess_start):
                    continue
            # 「剪断」用的前半截：这个时段里**整场都在 carve_start 之前**的那些
            # （它们是排布的前缀，重新打包位置不变），再用默认口味（50 → 25）把
            # 缝补上 —— 这样 11:00–11:30 的空档会变成一场 11:00–11:25，而不是
            # 白白空着、也不是留着原来那场 50 分钟的。
            kept = []
            for other in sessions:
                if other.get("period_id") != pid:
                    continue
                span = _range_of(other)
                if span is None or span[1] > carve_start:
                    continue
                dur = parse_minutes(other.get("duration"))
                kept.append((span[0], int(dur) if dur else span[1] - span[0]))
            kept.sort()
            cut_prefix = [dur for _start, dur in kept]
            cursor = kept[-1][0] + kept[-1][1] if kept else lower
            for _round in range(12):                      # 保险丝：最多补 12 场
                begin = cursor + (-cursor % 15)           # 往上取整到整点 :00/:15/:30/:45
                for size in (50, 25):
                    if begin + size <= carve_start:
                        cut_prefix.append(size)
                        cursor = begin + size
                        break
                else:
                    break
        else:
            # 同一天更早的场次：占这一场的开头（绝不越过下一场）。
            if int(fit) > sess_duration:
                continue
            window_end = int(sess_end) + int(break_got)
            carve_start = int(sess_start)
        carve_end = carve_start + int(fit) + int(break_got)     # 含休息的整段占用
        if not is_free({"start_min": carve_start, "end_min": carve_end}, reservations):
            continue
        candidates.append({
            "window_end": int(window_end),
            "carve_start": int(carve_start),
            "carve_end": int(carve_end),
            "session_duration": int(fit),
            "session": session,
            "tail": tail,
            "cut_prefix": cut_prefix,
            "period_id": pid,
        })
    if not candidates:
        raise HookError(
            "formatter end：%s 没有空闲的位置能放下 %d 分钟（已有 %d 段被占）。"
            % ((date or ctx.get("date") or "这一天"), duration, len(reservations)))

    # end 的第一优先级是「当天最后一个还有空位的时段」，同一窗口里越晚越好。
    candidates.sort(key=lambda row: (-row["window_end"], -row["carve_start"],
                                     row["session_duration"]))
    pick = candidates[0]
    carved_start = int(pick["carve_start"])
    session_duration = int(pick["session_duration"])
    reserve_end = int(pick["carve_end"])
    session_end = carved_start + session_duration
    source = pick["session"]
    source_range = _range_of(source) or (carved_start, carved_start)

    # 已经有同起点、而且**时长也正好**的 session（比如时段本来就排成 25+25）就复用，
    # 不动排期；同起点但时长不一样（50 分钟的场里切 25 分钟）仍要剪断。
    existing = None
    for session in sessions:
        got = _range_of(session)
        if got is None or got[0] != carved_start:
            continue
        dur = parse_minutes(session.get("duration"))
        if dur is None:
            dur = got[1] - got[0]
        if int(dur) == int(session_duration):
            existing = session
        break

    # 切出来的这段和哪些已有 session 时间上重叠（多半是那个时段原来那场大 session）：
    # 调用方要把它说给用户听，别让「11:00–11:50」和「11:30–11:55」静悄悄叠在一起。
    conflicts = []
    for session in sessions:
        if existing is not None and session is existing:
            continue
        got = _range_of(session)
        if got is not None and overlaps((carved_start, reserve_end), got):
            conflicts.append(session)

    reservation = {
        "date": source.get("date") or ctx.get("date"),
        "period_id": source.get("period_id"),
        "start": fmt_minutes(carved_start),
        "end": fmt_minutes(reserve_end),
        "start_min": carved_start,
        "end_min": reserve_end,
        "session_start": fmt_minutes(carved_start),
        "session_end": fmt_minutes(session_end),
        "session_duration": session_duration,
        "duration": int(duration),
        "break_minutes": int(break_got),
        "task_id": task_id,
        "exclusive": True,
        "carved": True,
        "existing_session": existing is not None,
        "source_session": {
            "start": source.get("start"), "end": source.get("end"),
            "duration": source.get("duration"), "period_id": source.get("period_id"),
        },
        "created_at": _now(),
    }
    session = {
        "date": source.get("date") or ctx.get("date"),
        "start": fmt_minutes(carved_start),
        "end": fmt_minutes(session_end),
        "duration": session_duration,
        "period_id": source.get("period_id"),
        "period_start": source.get("period_start"),
        "period_end": source.get("period_end"),
        "carved": True,
        "existing": existing is not None,
        "source_start": source.get("start"),
        # 尾巴上的切割要**剪断**那个时段：前半截（整场都在 carve_start 之前的场次）
        # 保留、原来那场消失、然后接上切出来的这一场。调用方拿这两个字段去重排。
        "cut_period_id": pick["period_id"] if pick["tail"] else None,
        "cut_prefix": list(pick["cut_prefix"] or []) if pick["tail"] else [],
    }
    return {"name": "end", "reservation": reservation, "session": session,
            "new_session": existing is None,
            "cut_period_id": session["cut_period_id"],
            "cut_prefix": session["cut_prefix"],
            "carved_from": fmt_minutes(source_range[0]),
            "conflicts": [s.get("start") for s in conflicts],
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
