#!/usr/bin/env python3
"""task_hooks.py —— 任务的 trigger / formatter 扩展点。

任务库本身只存数据、不执行算法。这里把「什么时候该创建一条任务」（trigger）
和「这条任务在这天怎么占时间」（formatter）都做成**具名预设函数**：

    task 3 -t <trigger> [参数...]      ← 给任务挂一个 create trigger
    task 3 -f <formatter> [参数...]    ← 给任务挂一个排队算法；end / enlist / repeat 会当场执行

两条设计原则
------------
1. **预设函数是唯一入口**。命令里存下来的只是 ``{"name": ..., "params": ...,
   "args": ...}``，执行时再按名字查表。没有注册的函数一律报错，绝不猜测。
2. **自动排期只排明天及以后**。``end`` / ``enlist`` / ``repeat`` 这类 formatter 的算法本身只管「哪一段
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
所有 formatter 走**同一条流水线**：先在时段尾巴上找到能放下的时间块，再切一场
任务大小的 session（时长取 Focusmate 真能订的 25/50/75，起点落 15 分钟整点），
把「时长 + 5 分钟休息」标成 reserved，最后由 warden 改写 layout 并把任务挂到
这一场。formatter 之间**只差挑哪一块**：

- ``end``：当天**最后一个**还有空位的时段；
- ``enlist``：当天**最早**还有空位的时段（早到早得），排在所有其它 formatter
  之后、项目 plan 之前。

没有任何 formatter 允许「把任务塞进别人已经切好的 session」：那会造出「25 分钟
任务挂在 75 分钟场里」这种一处时间两种身份的状态，表格 / 对账 / 排期都得为它写
特判。

这一层**只管算、不写盘**：formatter 把「那个时段的前半截该长什么样」
（``cut_prefix``）和「切出来的这一场」（``session``）一起交出去，由 warden 去
剪断排布（``_cut_period_for_carve``：改写 layout + 重算 data.csv）。排布表达
不了的边角（中间隔了 5/20 分钟那种）才额外补一场一次性 session
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

import session_plan as sp
import task_store as ts

#: trigger / formatter 的注册表。值是内部的小 dict：name / params / help / func。
TRIGGERS = {}
FORMATTERS = {}

#: ``end`` / ``enlist`` 默认在任务时长之外多留的休息分钟数；
#: ``repeat`` 默认 0（它只借用已有场次，不额外切时间）。
DEFAULT_BREAK_MINUTES = 5

#: formatter 的执行优先级：数字越小越早。``enlist`` 明确排在所有普通 formatter
#: 之后（1000），但整个 formatter 阶段仍然早于项目 ``plan``。以后加新 formatter
#: 如果不写优先级，默认 100，会自然排在 enlist 前面。
FORMATTER_PRIORITY = {"enlist": 1000}

#: 任务时长的字段别名。第一个是推荐写法，其余是为了兼容手写数据。
#: **口径只有一份**：``count ↔ duration``（1 count = 25 分钟）的换算是 task_store
#: 的事，这里只是借同一份别名表，别在两边各写一份然后慢慢漂。
DURATION_KEYS = ts.DURATION_KEYS

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


def formatter_priority(name):
    """formatter 名称 → 执行优先级（越小越早）。

    用户要求 ``enlist`` 排在「其他 formatter 的最后、项目之前」。优先级写在这里
    而不是散在 warden 里：注册表和调度顺序是同一份事实。
    """
    return int(FORMATTER_PRIORITY.get(str(name or "").strip().lower(), 100))


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
    return ts.parse_minutes(value)


def task_duration(task):
    """任务的持续时间（分钟）；没写 / 写坏了就是 ``None``。"""
    return ts.task_minutes(task)


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


def _ceil_quarter(minutes):
    """往上取整到 :00/:15/:30/:45（和 session_plan.plan_period 的口径一致）。"""
    total = int(minutes)
    return total + (-total % 15)


def _cut_prefix_for(period_range, carve_start, reservations):
    """给 ``end`` 计算「切割点之前」的排布。

    以前这里只拿「整场都在切割点之前」的旧 session，再用 50→25 往上补。
    这样 22:15–00:05、切点 23:30 的时段会留下原来的 50 分钟场，补不上
    23:05–23:30 的 25 分钟缝：排布表达不了，任务只能在 23:30 额外造一场，
    原来那场 50 分钟却还在。用户看到的就是「没有切分 session 给 task」。

    现在这里把「时段起点 → 切点」当成一个小型背包：只允许 Focusmate 真卖
    的 25/50/75 分钟，按 ``plan_period`` 同样的 15 分钟取整规则接龙，找出
    能**恰好把任务前一块填到切点**的时长序列。22:15–00:05 + 切点 23:30
    → ``[75]``（22:15–23:30），再接任务那场 25 分钟；11:00–12:00 + 切点
    11:30 → ``[25]``（11:00–11:25，下一场从 11:30 开始）。

    返回 ``(durations, exact)``。``exact`` 表示接上任务后任务起点正好是
    ``carve_start``；找不到精确解时返回能填得最靠后的序列，调用方仍可退回
    「前半截 + 一次性 session」的兜底路径。

    ``reservations`` 是别的 formatter 已占的区间。前缀只能排在空位上 ——
    先排的 formatter 是硬约束，后跑的流程不能把它们盖掉。
    """
    lower = int(period_range[0])
    carve_start = int(carve_start)
    if carve_start <= lower:
        return [], True

    # ``cursor`` = 「上一场结束 + 这一场强制的休息」= 下一场最早能开始的位置；
    # 下一场从 ``ceil15(cursor)`` 开始，和 :func:`session_plan.plan_period` 完全
    # 一致。以前 cursor 直接取上一场的 end（只做 15 分钟取整），于是 14:00 的
    # 75 分钟场后面被当成能从 15:15 接着排 —— Focusmate 不允许（要留 15 分钟）。
    reachable = {lower: []}
    best_exact = None
    best_partial = (lower, [])
    for _round in range(64):                       # 保险丝；一天最多几十场
        changed = False
        for cursor, path in list(reachable.items()):
            start = _ceil_quarter(cursor)
            for duration in (75, 50, 25):
                end = start + duration
                if end > carve_start:
                    continue
                if not is_free({"start_min": start, "end_min": end}, reservations):
                    continue
                next_cursor = end + sp.break_minutes(duration)
                if next_cursor in reachable:
                    continue
                new_path = path + [duration]
                reachable[next_cursor] = new_path
                changed = True
                gap = carve_start - end
                if _ceil_quarter(next_cursor) == carve_start:
                    key = (0 if gap == 0 else 1, gap, len(new_path), tuple(new_path))
                    if best_exact is None or key < best_exact[0]:
                        best_exact = (key, new_path)
                if next_cursor > best_partial[0]:
                    best_partial = (next_cursor, new_path)
        if not changed:
            break
    if best_exact is not None:
        return list(best_exact[1]), True
    # 没有精确解：给出填得最靠后的合法前缀。调用方会把任务单独补一场，
    # 至少原时段里能用的前半截不会被整段丢掉。
    return list(best_partial[1]), False


# ---------------------------------------------------------------- formatter: end


def grid_floor(minutes, step=15):
    """往下取整到 :00/:15/:30/:45（Focusmate 只认 15 分钟整点开场）。"""
    total = int(minutes)
    step = max(1, int(step))
    return total - (total % step)


def _already_past(ctx, start_min):
    """这一块是不是**已经过去了**（只在排「今天」时才是约束）。

    formatter 是「往未来排」的工具：今天 21:00 的时候不该把任务切到 09:00 那场
    —— 那一场订不了，也会把已经过去的时间重复算成容量。``ctx["now"]`` 由
    warden 注入（测试可以钉死时钟），没有就当作「不知道现在几点」，不拦。
    """
    now = ctx.get("now")
    if now is None:
        return False
    if str(ctx.get("date") or "") != now.date().isoformat():
        return False
    return int(start_min) < now.hour * 60 + now.minute


def _anchor_min(ctx):
    """今天 `plan` 的起点（下一个 15 分钟整点）→ 当天分钟数；不是今天返回 None。

    和 ``Warden._plan_anchor`` 同一口径：已经流走的分钟不是资源。formatter 在
    「整段还没切过、但已经开始了」的时段上只能从 anchor 之后切。
    """
    now = ctx.get("now")
    if now is None:
        return None
    if str(ctx.get("date") or "") != now.date().isoformat():
        return None
    anchor = now.replace(second=0, microsecond=0)
    anchor += datetime.timedelta(minutes=(-anchor.minute) % 15)
    if anchor.date() != now.date():
        return None                        # anchor 跨到明天了，今天没得排
    return anchor.hour * 60 + anchor.minute


def _format_end_over_windows(ctx, windows, task, duration, break_got, fit,
                             reservations, task_id):
    """``end`` 的 period-first 版本：在「可用时间窗」的尾巴切一场。

    窗口 = period 减去用户手订 / 静音 / 进行中的场次（见
    ``Warden.available_windows``）。窗口覆盖整个 period 时，切出来的那一场能
    写进 layout（``cut_period_id``），TI 自己生成的旧场次按「不是约束」取消重排；
    窗口被手订场切过时，layout 表达不了空洞，只在**不重叠**的前提下补一场
    一次性 session（``cut_period_id=None``）。
    """
    date = ctx.get("date")
    sessions = [dict(s) for s in (ctx.get("sessions") or ()) if isinstance(s, dict)]
    best = None
    for window in windows or ():
        if not isinstance(window, dict):
            continue
        start = int(window.get("start_min") or 0)
        end = int(window.get("end_min") or 0)
        if end - start < int(fit) + int(break_got):
            continue
        carve_start = grid_floor(end - int(break_got) - int(fit))
        if carve_start < start:
            continue
        carve_end = carve_start + int(fit) + int(break_got)
        if carve_end > end:
            continue
        if _already_past(ctx, carve_start):
            continue
        if not is_free({"start_min": carve_start, "end_min": carve_end},
                       reservations):
            continue
        prefix, prefix_exact, cut_pid = None, False, None
        if window.get("full"):
            pref, exact = _cut_prefix_for(
                (int(window.get("period_start_min") or start),
                 int(window.get("period_end_min") or end)),
                carve_start, reservations)
            if exact and pref:
                prefix, prefix_exact, cut_pid = pref, True, window.get("period_id")
        if cut_pid is None:
            span = (carve_start, carve_end)
            if any(_range_of(sess) is not None and overlaps(span, _range_of(sess))
                   for sess in sessions):
                continue
        key = (-end, -carve_start, 0 if cut_pid else 1)
        if best is None or key < best[0]:
            best = (key, window, carve_start, carve_end, prefix, prefix_exact,
                    cut_pid)
    if best is None:
        raise HookError(
            "formatter end：%s 没有能放下 %d 分钟（含 %d 分钟休息）的空位；"
            "可用时间窗：%s"
            % (date or "这一天", duration, int(break_got),
               "、".join("%s–%s" % (w.get("start"), w.get("end"))
                         for w in windows or () if isinstance(w, dict)) or "无"))
    _key, window, carve_start, carve_end, prefix, prefix_exact, cut_pid = best
    session_duration = int(fit)
    session_end = carve_start + session_duration
    existing = None
    for sess in sessions:
        got = _range_of(sess)
        if got is None or got[0] != carve_start:
            continue
        dur = parse_minutes(sess.get("duration"))
        if dur is None:
            dur = got[1] - got[0]
        if int(dur) == session_duration:
            existing = sess
        break
    conflicts = [str(s) for s in sessions
                 if not (existing is not None and s is existing)
                 and _range_of(s) is not None
                 and overlaps((carve_start, carve_end), _range_of(s))]
    period = dict(window.get("period") or {})
    reservation = {
        "date": date,
        "period_id": window.get("period_id"),
        "start": fmt_minutes(carve_start),
        "end": fmt_minutes(carve_end),
        "start_min": carve_start,
        "end_min": carve_end,
        "session_start": fmt_minutes(carve_start),
        "session_end": fmt_minutes(session_end),
        "session_duration": session_duration,
        "duration": int(duration),
        "break_minutes": int(break_got),
        "task_id": task_id,
        "exclusive": True,
        "carved": True,
        "windowed": True,
        "existing_session": existing is not None,
        "cut_period_id": cut_pid,
        "cut_prefix": list(prefix or []),
        "cut_prefix_exact": bool(prefix_exact),
        "source_session": {"start": window.get("start"),
                           "end": window.get("end"),
                           "duration": window.get("minutes"),
                           "period_id": window.get("period_id")},
        "created_at": _now(),
    }
    session = {
        "date": date,
        "start": fmt_minutes(carve_start),
        "end": fmt_minutes(session_end),
        "duration": session_duration,
        "period_id": window.get("period_id"),
        "period_start": period.get("start"),
        "period_end": period.get("end"),
        "carved": True,
        "existing": existing is not None,
        "source_start": window.get("start"),
        "cut_period_id": cut_pid,
        "cut_prefix": list(prefix or []),
        "cut_prefix_exact": bool(prefix_exact),
    }
    return {"name": "end", "reservation": reservation, "session": session,
            "new_session": existing is None, "conflicts": conflicts}


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
    if not sessions and ctx.get("windows") is None:
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

    # 2026-11-21：当天**一场计划 session 都没有**时，直接在「可用时间窗」
    # （period 减去用户手订场次）的尾巴切 —— 旧逻辑要求先有 session，导致
    # 只有真实订场/只有 period 的那天直接报「没有已排的 session」。
    # 有 session 时仍走原来的尾巴/剪断路径（period-first 的大改另做，避免
    # 一次动到表格、对账和剪断三条已覆盖的路径）。
    windows = ctx.get("windows") if not sessions else None
    if windows is not None:
        return _format_end_over_windows(ctx, windows, task, duration, break_got,
                                        fit, reservations, task_id)

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
            # 「剪断」用的前半截：从时段起点重新排到 carve_start，找一组
            # 25/50/75 的接龙，把最后一块**精准切到任务起点**。不能沿用
            # 「旧 session 原样保留 + 后面随便补」——22:15–00:05 这种时段
            # 里旧的 50 分钟场会让 23:30 的切点永远接不上，最后只剩一场
            # 重叠的旧 session + 一场额外 session。
            #
            # 每场之后要留 Focusmate 的强制休息，所以「最晚的切点」不一定接得上：
            # 22:15–00:05 里 75+15 到 23:45 就过了 23:30，这时往回退一格
            # （23:30 → 23:15，前缀 50+10）才是真正排得下的方案。
            cut_prefix = None
            prefix_exact = False
            probe = carve_start
            for _try in range(12):
                if probe < lower:
                    break
                got_prefix, got_exact = _cut_prefix_for(
                    period_range[pid], probe, reservations)
                # 前缀为空 = 任务被压到时段起点。那不叫「切最后一块」，还不如
                # 走「清掉整段 layout + 给任务单独补一场」的兜底（任务更靠后）。
                if got_exact and got_prefix:
                    carve_start = probe
                    cut_prefix = got_prefix
                    prefix_exact = True
                    break
                if probe == carve_start:
                    # 第一个候选都接不上：先记下最靠后的合法前缀当兜底。
                    cut_prefix, prefix_exact = got_prefix, False
                probe -= 15
            if cut_prefix is None:
                cut_prefix, prefix_exact = _cut_prefix_for(
                    period_range[pid], carve_start, reservations)
        else:
            # 同一天更早的场次：占这一场的开头（绝不越过下一场）。
            if int(fit) > sess_duration:
                continue
            window_end = int(sess_end) + int(break_got)
            carve_start = int(sess_start)
        carve_end = carve_start + int(fit) + int(break_got)     # 含休息的整段占用
        if _already_past(ctx, carve_start):
            continue                       # 今天已经过去的时段不是容量
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
            "prefix_exact": prefix_exact if tail else True,
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
        "cut_prefix_exact": bool(pick.get("prefix_exact", True)) if pick["tail"] else True,
    }
    return {"name": "end", "reservation": reservation, "session": session,
            "new_session": existing is None,
            "cut_period_id": session["cut_period_id"],
            "cut_prefix": session["cut_prefix"],
            "carved_from": fmt_minutes(source_range[0]),
            "conflicts": [s.get("start") for s in conflicts],
            "created_at": _now()}



# ---------------------------------------------------------------- formatter: enlist


def _session_duration(session):
    """一场 session 的分钟数；优先 duration，坏数据就看起终点。"""
    got = _range_of(session)
    dur = parse_minutes((session or {}).get("duration"))
    if dur is not None:
        return int(dur)
    return (got[1] - got[0]) if got is not None else None


@register_formatter(
    "enlist",
    params=("break_minutes", "date"),
    help="在当天**最早**还有空位的时段尾巴上切一场任务大小的 session（25/50/75）"
         "并挂上去；和 end 只差挑块顺序：end 从晚往早，enlist 从早到晚")
def format_enlist(ctx, break_minutes=DEFAULT_BREAK_MINUTES, date=None):
    """``task <id> -f enlist`` 的算法。

    **和 ``end`` 走同一条流水线**：先算「这个时段还有没有能放下的时间块」，
    再在那个时间块上切一场任务大小的 session，最后由 warden 改写 layout 并把
    任务挂到这一场。区别只有一处 —— 挑哪一块：

    * ``end``   ：当天**最后一个**还有空位的时段（越晚越好）；
    * ``enlist``：当天**最早**还有空位的时段（早到早得）。

    以前 ``enlist`` 是「往已经存在的 session 里塞」，那是主干之外的一条旁路：
    它会产出「25 分钟任务挂在 75 分钟场里」这种一份时间两种身份的状态，
    表格、对账、排期三处都得为它单独写特判。现在没有这条旁路了 —— 一个任务
    的落点永远是**它自己切出来的那一场**。

    切块的位置只认时段**尾巴**（:func:`session_plan.tail_gap`）：layout 只存
    从时段起点接龙的 duration 列表，往中间插一场会把后面已经排/订的场次整体
    挪位（= 取消 + 重订）。尾巴追加则前面每一场的起点一分不变。
    """
    task = ctx.get("task") or {}
    duration = task_duration(task)
    if duration is None:
        raise HookError(
            "formatter enlist 需要任务的持续时间，但这%s没有 duration。先补一个，例如："
            "`task modify %s duration 25`（也认 duration_minutes / minutes / 时长）"
            % ("条任务", task.get("id", "N")))
    break_got = parse_minutes(break_minutes)
    if break_got is None or int(break_got) < 0:
        raise HookError("formatter enlist 的 break_minutes 必须是非负整数；你给的是 %r"
                        % (break_minutes,))
    break_got = int(break_got)

    sessions = [dict(s) for s in (ctx.get("sessions") or ()) if isinstance(s, dict)]
    periods = [p for p in (ctx.get("periods") or ()) if isinstance(p, dict)]
    day_label = date or ctx.get("date") or "这一天"
    if not periods:
        raise HookError("formatter enlist：%s 没有任何时段，没法切时间块。" % day_label)

    task_id = ctx.get("task_id", task.get("id"))
    reservations = []
    for rec in ctx.get("reservations") or ():
        if task_id is not None and rec.get("task_id") == task_id:
            continue                       # 自己上一次的 reservation 不算占用
        reservations.append(rec)

    # 已有场次按「落在哪个时段」分组；拿不到 period_id 的（手写 csv）只当硬约束。
    by_period = {}
    all_ranges = []
    for session in sessions:
        got = _range_of(session)
        if got is None:
            continue
        pid = session.get("period_id")
        if pid:
            by_period.setdefault(str(pid), []).append(session)
        all_ranges.append(got)

    fit = int(fit_session_duration(duration))
    span = fit + break_got
    windows = (ctx.get("windows")
               if isinstance(ctx.get("windows"), (list, tuple)) else None)
    anchor_min = _anchor_min(ctx)
    candidates = []
    for period in periods:
        pid = str(period.get("id") or "")
        period_range = _range_of(period)
        if not pid or period_range is None:
            continue
        real = by_period.get(pid) or []
        # period-first：整段还没切过 session 的时段按「可用窗口的头部」切。
        # 窗口起点可能就是时段起点；如果时段已经开始，从 anchor 之后切；
        # 手订场把时段切成洞时，洞后面的窗口也能用 —— 这几种起点 layout 都
        # 表达不了，落盘走 extras（结果里不带 cut_period_id）。
        if windows is not None and not real:
            picked = None
            start_min = None
            mine = [w for w in windows
                    if str(w.get("period_id") or "") == pid]
            for win in sorted(mine, key=lambda w: int(w.get("start_min") or 0)):
                w_start = int(win.get("start_min") or 0)
                w_end = int(win.get("end_min") or 0)
                start_min = w_start
                if _already_past(ctx, start_min) or (
                        anchor_min is not None and start_min < anchor_min):
                    if anchor_min is None:
                        continue
                    start_min = max(start_min, anchor_min)
                start_min += (-start_min) % 15     # Focusmate 只认 15 分钟整点
                if start_min + span > w_end:
                    continue
                reserve = (start_min, start_min + span)
                if not is_free({"start_min": reserve[0], "end_min": reserve[1]},
                               reservations):
                    continue
                if any(overlaps(reserve, got) for got in all_ranges):
                    continue
                picked = win
                break
            if picked is None:
                continue
            picked_start = int(picked.get("start_min") or 0)
            if start_min == picked_start and picked_start == int(period_range[0]):
                # 窗口起点就是时段起点：和尾巴追加同形，能直接写进 layout
                # （只有起点被 anchor/手订洞挪过才需要 extras）。
                try:
                    planned = sp.plan_period(period, [fit])
                except ValueError:
                    planned = []
                if (len(planned) == 1
                        and int(planned[-1].get("start_min") or 0) == start_min):
                    candidates.append({
                        "period": period, "pid": pid, "prefix": [],
                        "planned": planned, "session": dict(planned[-1]),
                        "start_min": start_min, "period_range": period_range,
                    })
                    continue
            candidates.append({
                "period": period, "pid": pid, "prefix": [], "planned": [],
                "start_min": start_min,
                "period_range": (int(picked.get("start_min") or 0),
                                 int(picked.get("end_min") or 0)),
                "window": True,
            })
            continue
        gap = sp.tail_gap(period, real)
        if not gap:
            continue                       # 序列对不上 / 没有尾巴：这一段不许追加
        prefix = [int(d) for d in (gap.get("prefix") or [])]
        try:
            planned = sp.plan_period(period, prefix + [fit])
        except ValueError:
            continue                       # 尾巴放不下这一档
        if len(planned) != len(prefix) + 1:
            continue
        new = planned[-1]
        start_min = int(new.get("start_min") or 0)
        if _already_past(ctx, start_min):
            continue                       # 今天已经过去的时段不是容量
        reserve = (start_min, start_min + span)
        if not is_free({"start_min": reserve[0], "end_min": reserve[1]},
                       reservations):
            continue                       # 别的 formatter 已经占了这一块
        if any(overlaps(reserve, got) for got in all_ranges):
            continue                       # 休息尾巴会压住别的场次
        candidates.append({"period": period, "pid": pid, "prefix": prefix,
                           "planned": planned, "session": dict(new),
                           "start_min": start_min, "period_range": period_range})

    if not candidates:
        raise HookError(
            "formatter enlist：%s 没有任何时段还放得下 %d 分钟任务（需要一场 %d 分钟"
            "的 session）。要么各时段的尾巴都排满了，要么现有场次和 layout 的接龙"
            "对不上 —— 那种时段不能追加，否则会把已经排好的场次挪位。"
            % (day_label, int(duration), fit))

    # 早到早得：切点越早越优先，同一切点按时段起点。
    candidates.sort(key=lambda c: (c["start_min"], c["period_range"][0], c["pid"]))
    pick = candidates[0]
    period = pick["period"]
    prefix = pick["prefix"]
    start_min = pick["start_min"]
    planned = pick.get("planned") or []
    windowed = bool(pick.get("window"))
    session_end = start_min + fit
    reserve_end = start_min + span
    prior = planned[:-1]
    source_start = prior[-1].get("start") if prior else period.get("start")

    reservation = {
        "date": period.get("date") or ctx.get("date"),
        "period_id": pick["pid"],
        "start": fmt_minutes(start_min),
        "end": fmt_minutes(reserve_end),
        "start_min": start_min,
        "end_min": reserve_end,
        "session_start": fmt_minutes(start_min),
        "session_end": fmt_minutes(session_end),
        "session_duration": fit,
        "duration": int(duration),
        "break_minutes": break_got,
        "task_id": task_id,
        "exclusive": True,
        "carved": True,
        "existing_session": False,
        "source_session": {
            "start": source_start,
            "end": period.get("end"),
            "duration": int(prefix[-1]) if prefix else 0,
            "period_id": pick["pid"],
        },
        "created_at": _now(),
    }
    session = {
        "date": period.get("date") or ctx.get("date"),
        "start": fmt_minutes(start_min),
        "end": fmt_minutes(session_end),
        "duration": fit,
        "period_id": pick["pid"],
        "period_start": period.get("start"),
        "period_end": period.get("end"),
        "carved": True,
        "existing": False,
        "source_start": source_start,
        # 尾巴上追加：前缀原样保留，只在后面接上这一场 —— 已排好的场次一分钟不动。
        "cut_period_id": pick["pid"],
        "cut_prefix": prefix,
        "cut_prefix_exact": True,
    }
    if windowed:
        # 窗口起点不是时段起点（整段没切过 / 洞后窗口）：layout 表达不了，
        # 由 _ensure_carved_session 写成一次性 session；不带 cut_period_id，
        # 免得 _cut_period_for_carve 去重写整段 layout。
        session.pop("cut_period_id", None)
        session.pop("cut_prefix_exact", None)
        session["windowed"] = True
    return {"name": "enlist", "reservation": reservation, "session": session,
            "new_session": True, "cut_period_id": pick["pid"],
            "cut_prefix": prefix,
            "carved_from": fmt_minutes(int(pick["period_range"][0])),
            "windowed": windowed,
            "conflicts": [], "created_at": _now()}


# ---------------------------------------------------------------- formatter: repeat


def _ordered_sessions(sessions):
    """把候选 session 去重、按起点排序，给 ``repeat`` 的数字序号选择用。"""
    out = []
    seen = set()
    for session in sessions or ():
        if not isinstance(session, dict):
            continue
        got = _range_of(session)
        if got is None:
            continue
        start = str(session.get("start") or "").strip()
        dur = _session_duration(session)
        if not start or not dur:
            continue
        key = (start, int(dur), int(got[0]))
        if key in seen:
            continue
        seen.add(key)
        rec = dict(session)
        rec.update({"start_min": int(got[0]), "end_min": int(got[1]),
                    "duration": int(dur), "start": start})
        out.append(rec)
    out.sort(key=lambda r: (int(r["start_min"]), int(r["duration"])))
    return out


def _repeat_target_session(raw, sessions, date=None):
    """``repeat`` formatter 的 session 选择器 → 那一场 session。

    支持两种写法：

    * ``10:45`` / ``10:45:00``：按**起点**选，推荐用于重复任务（每周同一场）；
    * ``#2`` / ``2``：按当天从早到晚的第 2 场选（临时用；布局变了会跟着变）。

    只返回已有 session，绝不为了这个 formatter 新造或挪动排布。
    """
    text = str(raw if raw is not None else "").strip()
    day = date or "这一天"
    ordered = _ordered_sessions(sessions)
    if not ordered:
        raise HookError("formatter repeat：%s 没有任何已排的 session。" % day)
    if not text:
        raise HookError(
            "formatter repeat 需要指定要固定挂到哪一场 session。写法："
            "`task <序号> -f repeat 10:45`（按起点，重复任务推荐）或 "
            "`-f repeat #2`（当天第 2 场）。可用场次：%s"
            % "、".join("%s–%s" % (s.get("start"), s.get("end") or "?")
                        for s in ordered[:6]))

    idx = re.match(r"^#?(\d+)$", text)
    if idx:
        number = int(idx.group(1))
        if number < 1 or number > len(ordered):
            raise HookError(
                "formatter repeat：%s 只有 %d 场 session，没有第 %d 场。可用：%s"
                % (day, len(ordered), number,
                   "、".join("%s–%s" % (s.get("start"), s.get("end") or "?")
                            for s in ordered)))
        return ordered[number - 1]
    else:
        # 允许 10:45:00 这种带秒的粘贴值；只取前两个冒号段。
        match = re.match(r"^(\d{1,2}:\d{2})(?::\d{2})?$", text)
        if not match:
            raise HookError(
                "formatter repeat：session 要写成 HH:MM（按起点）或 #N（当天第 N 场）；"
                "你给的是 %r。可用场次：%s"
                % (text, "、".join("%s–%s" % (s.get("start"), s.get("end") or "?")
                                  for s in ordered[:6])))
        start_min = parse_hhmm(match.group(1))
        if start_min is None:
            raise HookError("formatter repeat：时间超出范围：%r" % text)
        if start_min % 15:
            raise HookError(
                "formatter repeat：Focusmate 只认 15 分钟整点，%r 不是合法起点；"
                "可用场次：%s"
                % (text, "、".join("%s–%s" % (s.get("start"), s.get("end") or "?")
                                  for s in ordered)))
        want = fmt_minutes(start_min)
        for session in ordered:
            if str(session.get("start")) == want:
                return session
        raise HookError(
            "formatter repeat：%s 没有起点为 %s 的 session。可用场次：%s"
            % (day, want, "、".join("%s–%s" % (s.get("start"), s.get("end") or "?")
                                   for s in ordered)))


@register_formatter(
    "repeat",
    params=("session", "break_minutes", "date"),
    help="把任务**固定挂到指定的已有 session** 上；参数 session=HH:MM（推荐）"
         "或 #N。挂在重复任务本体上时，每周每个 occurrence 都落同一场")
def format_repeat(ctx, session=None, break_minutes=0, date=None):
    """``task <id> -f repeat <session>`` 的算法。

    ``session`` 是目标场次的**起点**（推荐 ``10:45``）或当天第几场
    （``#2``）。和 ``end`` 的区别是：这里不切 layout、不新造、不取消 session，
    只把任务绑到用户点名的那一场。任务从该场起点开始占「任务时长 + 休息」；
    目标场必须真实存在，而且必须装得下任务。

    这一点和重复任务配合时最有用：本体上的 formatter 会转嫁给每个 occurrence
    子任务，所以 ``task id:<本体> -f repeat 10:45`` 的语义就是「以后每周的
    这一次，都固定在那场 10:45 的 session 上完成」。如果那一周 10:45 那场
    不存在（排布变了 / 被取消），这一条会明确报错，而不是悄悄改到别的场。
    """
    task = ctx.get("task") or {}
    duration = task_duration(task)
    if duration is None:
        raise HookError(
            "formatter repeat 需要任务的持续时间，但这%s没有 duration。先补一个，例如："
            "`task modify %s duration 25`（也认 duration_minutes / minutes / 时长）"
            % ("条任务", task.get("id", "N")))
    # break_minutes 默认 0；parse_minutes(0) 会把 0 当成「没有」，所以这里
    # 单独认 0 / "0"，其余再交给统一的分钟解析。
    if break_minutes is None or str(break_minutes).strip() in ("", "0"):
        break_got = 0
    else:
        break_got = parse_minutes(break_minutes)
    if break_got is None:
        raise HookError("formatter repeat 的 break_minutes 必须是非负整数；你给的是 %r"
                        % (break_minutes,))
    break_got = int(break_got)

    sessions = [dict(s) for s in (ctx.get("sessions") or ()) if isinstance(s, dict)]
    day = ctx.get("date") or date
    source = _repeat_target_session(session, sessions, date=day)
    session_start = _range_of(source)[0]
    session_duration = _session_duration(source)
    if not session_duration:
        raise HookError("formatter repeat：目标 session 的时长认不出来。")
    span = int(duration) + break_got
    if span > int(session_duration):
        raise HookError(
            "formatter repeat：%s %s 那场只有 %d 分钟，装不下任务 %d 分钟"
            "%s。换一场，或先改这个时段的排布。"
            % (day, fmt_minutes(session_start), int(session_duration), int(duration),
               (" + %d 分钟休息" % break_got) if break_got else ""))

    task_id = ctx.get("task_id", task.get("id"))
    reservations = [rec for rec in (ctx.get("reservations") or ())
                    if task_id is None or rec.get("task_id") != task_id]
    block = {"start_min": session_start, "end_min": session_start + span}
    if not is_free(block, reservations):
        raise HookError(
            "formatter repeat：%s %s 那场的开头已经被别的 formatter 占了，"
            "没法再把任务固定到这里。" % (day, fmt_minutes(session_start)))

    session_end = _range_of(source)[1]
    reservation = {
        "date": source.get("date") or day,
        "period_id": source.get("period_id"),
        "start": fmt_minutes(session_start),
        "end": fmt_minutes(session_start + span),
        "start_min": session_start,
        "end_min": session_start + span,
        "session_start": fmt_minutes(session_start),
        "session_end": fmt_minutes(session_end),
        "session_duration": int(session_duration),
        "duration": int(duration),
        "break_minutes": break_got,
        "task_id": task_id,
        "exclusive": False,
        "pinned": True,
        "created_at": _now(),
    }
    chosen = {
        "date": source.get("date") or day,
        "start": fmt_minutes(session_start),
        "end": fmt_minutes(session_end),
        "duration": int(session_duration),
        "period_id": source.get("period_id"),
        "period_start": source.get("period_start"),
        "period_end": source.get("period_end"),
        "pinned": True,
        "existing": True,
        "source_start": source.get("start"),
        "cut_period_id": None,
        "cut_prefix": [],
        "cut_prefix_exact": True,
    }
    return {"name": "repeat", "reservation": reservation, "session": chosen,
            "new_session": False, "cut_period_id": None, "cut_prefix": [],
            "target": fmt_minutes(session_start), "conflicts": [],
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
