#!/usr/bin/env python3
"""download_calendar.py —— 从 iCloud 日历（CalDAV）拉时间，算成 Focusmate session，写进 data.csv。

它在整套系统里的位置
--------------------
    iCloud 日历（只 工作/空闲）
        ──CalDAV──▶ 事件 ──▶ temp/calendar-download.json（T1/T2 时段）
                                  ├──▶ temp/calendar.md（人读版）
                                  └──▶ temp/session-layouts.json（用户排布）
                                            └──▶ data.csv（精确日期行）

    data.csv 是 keep_session.py（每天 check）唯一的目标来源。
    本脚本**只生成计划、不下单**；下单永远是 keep_session.py 的事。

需求（来自本文件原来的注释）
----------------------------
1. 用 caldav 库把日历从云端下载下来。
2. filter 掉 < 25 分钟的时间段——Focusmate 最短 25 分钟，订不了。
3. 每个时间段从开始时间往后找最近的整点/quarter（:00/:15/:30/:45，Focusmate 只认这些）。
4. 再往后加 25/50 分钟找一个塞得下的 session；>50 分钟的时间段优先 50。
   例：14:10–15:30 → 14:15–15:05。
5. 切出来的 session 从原时间段挖掉，剩下的继续算（14:00–17:00 切掉 14:00–14:50
   后剩 14:50–17:00，继续切）。
6. 忽略「上课」「休息」分组（日历）——那些时间不排 session。
7. 结果覆盖写入 data.csv。

关键取舍（先读这里）
--------------------
- **日历事件 = 可用的空闲时间段。** 所以「忽略上课/休息」= 把这两个日历里的事件
  排除在候选之外。若你的日历反过来记的是「忙」（空闲 = 补集），这套算法不适用，
  得改 plan_week()。
- 按分组（日历显示名）过滤：`--ignore 上课 休息`（默认）排除，`--use 自习` 只留白名单。
- 全天事件默认跳过（会打日志）；想当整天可用就加 `--include-all-day`。
- 只有 `populate` 会写 data.csv，且**必须手动触发**；daemon 不会自己跑它。
- 今天只保留「还没开始」的 session；未来几天全保留。
- 只输出 25/50 分钟两种时长（keep_session 也接受 75，但本脚本不会排 75）。

用法
----
    python3 download_calendar.py install-deps           # 一次性：按 requirement.txt 装到 temp/pylibs
    python3 download_calendar.py login                  # 登录 iCloud（App 专用密码），存配置
    python3 download_calendar.py status                 # 看依赖 / 配置 / 有哪几个日历
    python3 download_calendar.py download               # 只拉 工作/空闲 → 中间层 JSON + md
    python3 download_calendar.py populate --from-download  # 中间层 + layout → data.csv
    python3 download_calendar.py layout T1 50 50        # 改一个时段的排布
    python3 download_calendar.py show                   # 看所有时段和当前 session
    python3 download_calendar.py populate --ics a.ics   # 离线：从本地 .ics 跑，不联网（可重复给）

依赖：caldav（连带 icalendar / recurring-ical-events），装在 temp/pylibs，不污染系统环境。
"""
import argparse
import dataclasses
import datetime
import getpass
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import session_plan as sp
except Exception:                                    # noqa: BLE001
    sp = None
TEMP = os.path.join(HERE, "temp")
PYLIBS = os.path.join(TEMP, "pylibs")
CONFIG_FILE = os.path.join(TEMP, "calendar.json")
DEFAULT_MD = os.path.join(TEMP, "calendar.md")
DATA_CSV = os.path.join(HERE, "data.csv")
BACKUP_CSV = os.path.join(TEMP, "data.csv.bak")
DEFAULT_DOWNLOAD_JSON = os.path.join(TEMP, "calendar-download.json")
DEFAULT_PROFILES = ["工作", "空闲"]

DEFAULT_URL = "https://caldav.icloud.com"
DEFAULT_IGNORE = ["上课", "休息"]
DEFAULT_PIP_SPEC = "caldav==2.2.6"
REQ_FILE = os.path.join(HERE, "requirement.txt")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CN_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DURATIONS = (50, 25)   # 优先 50；「>50 分钟的时间段优先订 50」
MIN_DURATION = 25      # Focusmate 最短 session
LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

# 依赖装在项目里的 temp/pylibs，不动系统环境（沙箱/公司机器上 pip --user 常常写不进去）。
if os.path.isdir(PYLIBS) and PYLIBS not in sys.path:
    sys.path.insert(0, PYLIBS)

try:
    import caldav
except Exception:                                    # noqa: BLE001
    caldav = None
try:
    import icalendar
except Exception:                                    # noqa: BLE001
    icalendar = None
try:
    import recurring_ical_events
except Exception:                                    # noqa: BLE001
    recurring_ical_events = None


def info(msg):
    print(msg)


def warn(msg):
    print("[warn] " + msg, file=sys.stderr)


def die(msg, code=1):
    print("[error] " + msg, file=sys.stderr)
    sys.exit(code)


def stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def require_deps():
    if caldav is None or icalendar is None:
        die("缺少依赖 caldav / icalendar。先跑：\n"
            "    python3 download_calendar.py install-deps\n"
            "（会装到 %s，可以再加 --spec 指定版本）" % PYLIBS)


# ================================================================ 配置 / 登录


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(TEMP, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)                 # 里面有 App 专用密码
    os.replace(tmp, CONFIG_FILE)


def mask(secret):
    if not secret:
        return "(空)"
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


def safe_display_name(cal):
    """日历显示名——也就是需求里说的「分组」。"""
    for getter in ("get_display_name",):
        try:
            n = getattr(cal, getter)()
            if n:
                return str(n).strip()
        except Exception:                            # noqa: BLE001
            pass
    try:
        return str(cal.name or "").strip()
    except Exception:                                # noqa: BLE001
        return ""


def connect(cfg):
    require_deps()
    return caldav.DAVClient(
        url=cfg.get("url") or DEFAULT_URL,
        username=cfg.get("username"),
        password=cfg.get("password"),
        timeout=30,
    )


def list_calendars(cfg):
    """连上去列出所有日历显示名（顺便验证凭据）。"""
    client = connect(cfg)
    try:
        principal = client.principal()
        return [safe_display_name(c) for c in principal.calendars()]
    finally:
        try:
            client.close()
        except Exception:                            # noqa: BLE001
            pass


def cmd_install_deps(args):
    os.makedirs(PYLIBS, exist_ok=True)
    if args.spec:
        specs, src = [args.spec], "命令行 --spec"
    elif os.path.exists(REQ_FILE):
        specs, src = ["-r", REQ_FILE], os.path.basename(REQ_FILE)
    else:
        specs, src = [DEFAULT_PIP_SPEC], "内置默认"
    cmd = [sys.executable, "-m", "pip", "install", "--target", PYLIBS] + specs
    info("$ " + " ".join(cmd))
    info("（包来源：%s）" % src)
    rc = subprocess.call(cmd)
    if rc == 0:
        info("装好了：%s" % PYLIBS)
        info("（用 python3 download_calendar.py status 复查）")
    return rc


def cmd_login(args):
    """保存 iCloud CalDAV 凭据，并当场验证一次。"""
    require_deps()
    cfg = load_config()
    url = args.url or cfg.get("url") or DEFAULT_URL
    username = args.username or cfg.get("username") or ""
    if not username:
        username = input("Apple ID（iCloud 邮箱）: ").strip()
    if not username:
        die("账号不能为空")

    password = args.password or os.environ.get("TI_CALDAV_PASSWORD") or ""
    if not password:
        password = getpass.getpass("App 专用密码（不是 Apple ID 登录密码）: ").strip()
    if not password:
        die("密码不能为空")

    ignore = args.ignore if args.ignore is not None else cfg.get("ignore_calendars", DEFAULT_IGNORE)
    use = args.use if args.use is not None else cfg.get("use_calendars", [])
    new_cfg = {"url": url, "username": username, "password": password,
               "ignore_calendars": ignore, "use_calendars": use}

    if args.no_verify:
        info("跳过验证（--no-verify），直接保存")
    else:
        info("正在连 %s 验证……" % url)
        try:
            names = list_calendars(new_cfg)
        except Exception as e:                       # noqa: BLE001
            die("连不上 / 认证失败：%s\n"
                "  · 密码必须是 App 专用密码：appleid.apple.com → 登录与安全 → App 专用密码\n"
                "  · 账号要填完整 Apple ID 邮箱；开了双重认证也走这条路\n"
                "  · 确认后重试；只是想先存下来可以用 --no-verify" % e)
        info("认证 OK，看到 %d 个日历：" % len(names))
        for n in names:
            tag = "  ← 忽略" if n in ignore else ("  ← 只留它" if use and n in use else "")
            info("   - %s%s" % (n, tag))

    save_config(new_cfg)
    info("已保存到 %s（权限 600）" % CONFIG_FILE)
    info("下一步：python3 download_calendar.py populate --dry-run")
    return 0


def cmd_logout(args):
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)
        info("已删除 %s" % CONFIG_FILE)
    else:
        info("本来就没有配置")
    return 0


def modver(mod, name, fallback="缺"):
    """模块版本——有的包（如 recurring_ical_events）不带 __version__，退回包元数据。"""
    if mod is None:
        return fallback
    v = getattr(mod, "__version__", None)
    if v:
        return v
    try:
        import importlib.metadata as _md
        return _md.version(name)
    except Exception:                                # noqa: BLE001
        return "已装"


def cmd_status(args):
    info("== 依赖 ==")
    if caldav is None:
        info("  caldav: 缺（跑 install-deps）")
    else:
        info("  caldav: %s @ %s" % (modver(caldav, "caldav"), os.path.dirname(caldav.__file__)))
    info("  icalendar: %s" % modver(icalendar, "icalendar"))
    info("  recurring-ical-events: %s" % modver(recurring_ical_events, "recurring-ical-events"))

    info("== 配置（%s）==" % CONFIG_FILE)
    cfg = load_config()
    if not cfg:
        info("  (没有，先跑 login)")
    else:
        info("  url      : %s" % cfg.get("url"))
        info("  username : %s" % cfg.get("username"))
        info("  password : %s" % mask(cfg.get("password")))
        info("  忽略分组 : %s" % ("、".join(cfg.get("ignore_calendars") or []) or "(无)"))
        info("  白名单   : %s" % ("、".join(cfg.get("use_calendars") or []) or "(无)"))

    if args.live and cfg and caldav is not None:
        info("== 实时日历列表 ==")
        try:
            for n in list_calendars(cfg):
                info("   - %s" % n)
        except Exception as e:                       # noqa: BLE001
            warn("连不上：%s" % e)

    info("== 产物 ==")
    for p in (DEFAULT_MD, DATA_CSV):
        if os.path.exists(p):
            info("  %s  (%d 字节, %s)" % (p, os.path.getsize(p),
                                          datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M")))
        else:
            info("  %s  (还没有)" % p)
    return 0


# ================================================================ 日历拉取


@dataclasses.dataclass
class CalEvent:
    start: datetime.datetime
    end: datetime.datetime
    calendar: str = ""
    title: str = ""
    all_day: bool = False

    def fmt(self):
        return "%s–%s" % (self.start.strftime("%H:%M"), self.end.strftime("%H:%M"))


def component_span(comp):
    """从一个 VEVENT 里取 (start, end, all_day)；取不出来返回 None。"""
    ds = comp.get("DTSTART")
    if ds is None:
        return None
    start = ds.dt
    all_day = not isinstance(start, datetime.datetime)
    if all_day:
        start = datetime.datetime.combine(start, datetime.time.min)
    if start.tzinfo is None:
        start = start.replace(tzinfo=LOCAL_TZ)

    de = comp.get("DTEND")
    du = comp.get("DURATION")
    if de is not None:
        end = de.dt
        if not isinstance(end, datetime.datetime):
            end = datetime.datetime.combine(end, datetime.time.min)
    elif du is not None:
        end = start + du.dt
    else:
        end = start + (datetime.timedelta(days=1) if all_day else datetime.timedelta(0))
    if end.tzinfo is None:
        end = end.replace(tzinfo=LOCAL_TZ)
    return start, end, all_day


def events_from_ical(ics_text, calendar_name, horizon_start, horizon_end):
    """把一份 .ics 文本拆成落在窗口内的事件（RRULE 会自动展开）。"""
    try:
        cal = icalendar.Calendar.from_ical(ics_text)
    except Exception as e:                           # noqa: BLE001
        warn("解析 ics 失败（%s）：%s" % (calendar_name, e))
        return []

    comps = None
    if recurring_ical_events is not None:
        try:
            comps = recurring_ical_events.of(cal).between(horizon_start, horizon_end)
        except Exception as e:                       # noqa: BLE001
            warn("展开重复事件失败（%s），退化为只取原始 VEVENT：%s" % (calendar_name, e))
    if comps is None:
        comps = list(cal.walk("VEVENT"))

    out = []
    for c in comps:
        span = component_span(c)
        if span is None:
            continue
        start, end, all_day = span
        if end <= horizon_start or start >= horizon_end:
            continue
        out.append(CalEvent(start=start, end=end, calendar=calendar_name,
                            title=str(c.get("SUMMARY") or "").strip(), all_day=all_day))
    return out


def fetch_caldav(cfg, horizon_start, horizon_end, profiles=None):
    """连 CalDAV 把窗口内的事件拉下来。返回 (events, 所有日历名)。

    ``profiles`` 给了（默认 工作/空闲）时只查询这些日历；如果一个都没匹配到，
    退回查询全部，让上层按旧规则处理 —— 这样别的日历结构也不会静默变空。
    """
    client = connect(cfg)
    events, names = [], []
    try:
        principal = client.principal()
        cals = list(principal.calendars())
        named = [(safe_display_name(c), c) for c in cals]
        names = [n for n, _ in named]
        wanted = set(profiles or [])
        selected = [c for n, c in named if n in wanted] if wanted else cals
        if wanted and not selected:
            warn("日历里没有找到目标 profile（%s）；退回下载全部分组" % "、".join(sorted(wanted)))
            selected = cals
        for cal in selected:
            name = safe_display_name(cal)
            try:
                objs = cal.search(start=horizon_start.astimezone(datetime.timezone.utc),
                                  end=horizon_end.astimezone(datetime.timezone.utc),
                                  event=True)
            except Exception as e:                   # noqa: BLE001
                warn("日历 %r 查询失败，跳过：%s" % (name, e))
                continue
            for obj in objs:
                try:
                    text = obj.data
                except Exception as e:               # noqa: BLE001
                    warn("取事件内容失败（%s）：%s" % (name, e))
                    continue
                events.extend(events_from_ical(text, name, horizon_start, horizon_end))
    finally:
        try:
            client.close()
        except Exception:                            # noqa: BLE001
            pass
    return events, names


def fetch_ics(paths, horizon_start, horizon_end):
    """离线模式：直接读本地 .ics（分组名取 X-WR-CALNAME，没有就用文件名）。"""
    events, names = [], []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            die("读不了 %s：%s" % (p, e))
        name = ""
        try:
            cal = icalendar.Calendar.from_ical(text)
            name = str(cal.get("X-WR-CALNAME") or "").strip()
        except Exception:                            # noqa: BLE001
            pass
        if not name:
            name = os.path.splitext(os.path.basename(p))[0]
        names.append(name)
        events.extend(events_from_ical(text, name, horizon_start, horizon_end))
    return events, names


# ================================================================ 切分算法


def ceil_to_quarter(dt):
    """往上取整到最近的 :00/:15/:30/:45（已经对齐则原样返回）。"""
    base = dt.replace(second=0, microsecond=0)
    rem = base.minute % 15
    if rem == 0 and dt.second == 0 and dt.microsecond == 0:
        return base
    return base + datetime.timedelta(minutes=15 - rem)


def merge_windows(windows):
    """合并重叠/相接的时间段。"""
    out = []
    for s, e in sorted(windows):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def carve(blocks, durations=DURATIONS, min_duration=MIN_DURATION):
    """把空闲时间段切成 Focusmate session。

    blocks: [(start, end)] 已合并的空闲时间段（本地 timezone-aware）。
    返回 [(start, duration_min)]，按时间排序。

    贪心：从段首往后找最近的 quarter 起点；能塞下 durations 里第一个时长就切走，
    然后把「左缺口」和「右剩余」继续当新时间段算（左缺口 ≤15min，必然 <25min 被丢弃）。
    段首都塞不下说明后面更塞不下，直接放弃这一段。
    """
    sessions = []
    work = list(blocks)
    while work:
        s, e = work.pop(0)
        if (e - s).total_seconds() < min_duration * 60:
            continue                                  # <25min，订不了
        start = ceil_to_quarter(s)
        for d in durations:
            end = start + datetime.timedelta(minutes=d)
            if end <= e:
                sessions.append((start, d))
                if (start - s).total_seconds() >= min_duration * 60:
                    work.append((s, start))
                if (e - end).total_seconds() > 0:
                    work.append((end, e))
                break
        work.sort(key=lambda b: b[0])
    sessions.sort()
    return sessions


@dataclasses.dataclass
class DayPlan:
    date: datetime.date
    windows: list = dataclasses.field(default_factory=list)
    sessions: list = dataclasses.field(default_factory=list)
    ignored: list = dataclasses.field(default_factory=list)
    all_day: list = dataclasses.field(default_factory=list)
    short: list = dataclasses.field(default_factory=list)


def grouped_out(ev, ignore, use):
    """这个事件该不该被分组过滤掉？

    白名单（--use）优先：给了白名单就只留白名单里的分组，ignore 不再参与。
    没给白名单才用黑名单（--ignore）。
    """
    name = (ev.calendar or "").strip()
    if use:
        return name not in use
    return name in (ignore or [])


def select_profile_events(events, wanted_profiles, ignore, use, include_all_day=False):
    """先做「只下载工作 + 空闲」这一层过滤。

    真实日历里同时有 工作/空闲/上课/休息/提醒 等分组。新链路只关心前两个。
    为了让老的离线测试和「没有这两个分组」的日历仍可用，如果这批事件里**一个**
    目标 profile 都没有，就退回旧的 ignore/use 过滤（并给出 warning）。
    返回 (selected_events, mode, used_profiles)。
    """
    profiles = list(dict.fromkeys(p for p in (wanted_profiles or []) if p))
    present = set((ev.calendar or "").strip() for ev in events)
    used = [p for p in profiles if p in present]
    if used:
        out = []
        for ev in events:
            if (ev.calendar or "").strip() not in profiles:
                continue
            if ev.all_day and not include_all_day:
                continue
            out.append(ev)
        return out, "profiles", used
    if profiles:
        warn("日历里没有找到目标 profile（%s）；退回旧的 ignore/use 过滤"
             % "、".join(profiles))
    out = [ev for ev in events if not grouped_out(ev, ignore, use)]
    return out, "legacy", []


def plan_week(events, start_date, days, ignore, use, include_all_day, now):
    """把事件切成 session 计划。今天只保留还没开始的。

    session 是在**整条时间轴上**一次切完的，**允许跨午夜**（23:15–00:05 这种 50min
    session 必须能切出来），再按 session 的**起始日期**归到那一天。

    为什么不能按天各自切：午夜会把一个连续的空闲段劈成「23:15–00:00（45min）」和
    「00:00–00:05（5min）」，前者只能切出 25min；而且相邻两天各切一份会重复预订。
    """
    plans = []
    horizon_start = datetime.datetime.combine(
        start_date, datetime.time.min).replace(tzinfo=LOCAL_TZ)
    horizon_end = horizon_start + datetime.timedelta(days=days)

    # 1) 每天的**报告**桶（给人看的 md：可用时间段 / 太短 / 被忽略 / 全天事件）。
    #    这里仍按天截断（报告是「这一天看到什么」），真正的 session 见下面第 2 步。
    for i in range(days):
        day = start_date + datetime.timedelta(days=i)
        d0 = datetime.datetime.combine(day, datetime.time.min).replace(tzinfo=LOCAL_TZ)
        d1 = d0 + datetime.timedelta(days=1)
        windows, ignored, allday = [], [], []
        for ev in events:
            if ev.end <= d0 or ev.start >= d1:
                continue
            if grouped_out(ev, ignore, use):
                ignored.append(ev)
                continue
            if ev.all_day and not include_all_day:
                allday.append(ev)
                continue
            if ev.start < d0 and d0 > horizon_start:
                # 跨午夜事件的后半截：前一天已经报过/切过了，别在这儿当成新窗口
                # （也不要在「太短」里留一条 00:00–00:05 的碎片）
                continue
            s = max(ev.start, d0)
            e = ev.end                            # 不按午夜截：跨午夜的窗口按真实终点显示
            if day == now.date():
                s = max(s, now)                       # 今天：已经过去的时段不再排
            if e <= s:
                continue
            windows.append((s, e))
        merged = merge_windows(windows)
        keep = lambda w: (w[1] - w[0]).total_seconds() >= MIN_DURATION * 60      # noqa: E731
        plans.append(DayPlan(date=day, windows=[w for w in merged if keep(w)],
                             ignored=ignored, all_day=allday,
                             short=[w for w in merged if not keep(w)]))

    # 2) session：整条时间轴切一次。起点按 horizon / now 截，**终点不截**（允许跨午夜），
    #    切出来的 session 按起始日期分给对应的那几天；跑出计划范围的不写。
    free = []
    for ev in events:
        if grouped_out(ev, ignore, use):
            continue
        if ev.all_day and not include_all_day:
            continue
        s = max(ev.start, horizon_start)
        if ev.end <= s:
            continue
        free.append((s, ev.end))
    carveable = []
    for s, e in merge_windows(free):
        if e <= now:
            continue
        carveable.append((max(s, now), e))          # 今天：已经过去的时段不再排
    for start, dur in carve(carveable):
        if not (horizon_start <= start < horizon_end):
            continue
        plans[(start.date() - start_date).days].sessions.append((start, dur))
    return plans


# ================================================================ 渲染 / 落盘


def render_csv(plans, meta):
    lines = [
        "# 每日目标 session（weekday,HH:MM,duration；weekday 留空或 * 表示每天）",
        "# 由 download_calendar.py populate 生成 @ %s" % meta["stamp"],
        "# 来源：%s" % meta["source"],
    ]
    for p in plans:
        wd = WEEKDAYS[p.date.weekday()]
        for s, d in p.sessions:
            lines.append("%s,%s,%d" % (wd, s.strftime("%H:%M"), d))
    return "\n".join(lines) + "\n"


def make_download(meta):
    """把本次拉到的日历事件变成中间层 JSON（含 T1/T2 编号的时段）。"""
    if sp is None:
        return None
    events = meta.get("selected_events") or []
    profiles = meta.get("used_profiles") or None
    data = sp.build_periods(events, meta["start_date"], meta["days"], profiles=profiles)
    data["generated_at"] = meta.get("stamp") or stamp()
    data["source"] = meta.get("source") or ""
    data["all_calendars"] = meta.get("calendars") or []
    data["mode"] = meta.get("download_mode") or "legacy"
    return data


def download_json_path(args=None):
    return (getattr(args, "download_json", None) if args else None) or DEFAULT_DOWNLOAD_JSON


def write_download(data, path=None):
    if data is None or sp is None:
        return None
    path = path or DEFAULT_DOWNLOAD_JSON
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def render_md(plans, meta):
    L = []
    L.append("# 日历 → Focusmate session 计划")
    L.append("")
    L.append("- 生成时间：%s（本地）" % meta["stamp"])
    L.append("- 来源：%s" % meta["source"])
    L.append("- 范围：%s ~ %s（%d 天）"
             % (plans[0].date.isoformat(), plans[-1].date.isoformat(), len(plans)))
    L.append("- 忽略分组：%s" % ("、".join(meta["ignore"]) or "（无）"))
    if meta["use"]:
        L.append("- 白名单分组：%s" % "、".join(meta["use"]))
    if meta.get("profiles"):
        L.append("- 下载 profile：%s（实际命中：%s）"
                 % ("、".join(meta["profiles"]),
                    "、".join(meta.get("used_profiles") or []) or "无，退回旧过滤"))
    L.append("- 规则：<25min 丢弃；起点对齐 :00/:15/:30/:45；优先 50min，不够退 25min")
    L.append("- 日历里看到的分组：%s" % ("、".join(meta["calendars"]) or "（无）"))
    L.append("")

    total = sum(len(p.sessions) for p in plans)
    total_min = sum(d for p in plans for _, d in p.sessions)
    L.append("## 汇总")
    L.append("")
    L.append("| 日期 | 星期 | session 数 | 总时长 |")
    L.append("|------|------|-----------|--------|")
    for p in plans:
        L.append("| %s | %s | %d | %d min |"
                 % (p.date.isoformat(), CN_WEEKDAYS[p.date.weekday()],
                    len(p.sessions), sum(d for _, d in p.sessions)))
    L.append("| **合计** |  | **%d** | **%d min** |" % (total, total_min))
    L.append("")

    # 中间层：下载下来、按天编号的时段。用户就是用这些 T1/T2 改排布的。
    periods = (meta.get("periods") or {}).get("periods") if isinstance(
        meta.get("periods"), dict) else None
    if periods:
        L.append("## 时段（下载层，T1/T2 可用来改 session 排布）")
        L.append("")
        L.append("| 日期 | 时段 ID | 时间段 | 类型 | 名称 |")
        L.append("|------|---------|--------|------|------|")
        for period in periods:
            L.append("| %s | %s | %s–%s | %s | %s |"
                     % (period.get("date"), period.get("id"), period.get("start"),
                        period.get("end"), period.get("type") or "",
                        period.get("name") or ""))
        L.append("")

    for p in plans:
        L.append("## %s %s" % (CN_WEEKDAYS[p.date.weekday()], p.date.isoformat()))
        L.append("")
        if p.windows:
            L.append("可用时间段（已过滤分组/短段）：")
            L.append("")
            L.append("| 时间段 | 时长 |")
            L.append("|--------|------|")
            for s, e in p.windows:
                L.append("| %s–%s | %d min |" % (s.strftime("%H:%M"), e.strftime("%H:%M"),
                                                 int((e - s).total_seconds() // 60)))
            L.append("")
        if p.sessions:
            L.append("切出的 session：")
            L.append("")
            L.append("| session | 时长 |")
            L.append("|---------|------|")
            for s, d in p.sessions:
                end = s + datetime.timedelta(minutes=d)
                L.append("| %s–%s | %d min |" % (s.strftime("%H:%M"), end.strftime("%H:%M"), d))
            L.append("")
        else:
            L.append("_这天没切出 session。_")
            L.append("")
        if p.short:
            L.append("<details><summary>太短被丢掉的时间段（%d，&lt;25min）</summary>" % len(p.short))
            L.append("")
            for s, e in p.short:
                L.append("- %s–%s（%d min）" % (s.strftime("%H:%M"), e.strftime("%H:%M"),
                                               int((e - s).total_seconds() // 60)))
            L.append("")
            L.append("</details>")
            L.append("")
        if p.ignored:
            L.append("<details><summary>被忽略分组挡掉的事件（%d）</summary>" % len(p.ignored))
            L.append("")
            for ev in p.ignored:
                L.append("- `%s` %s %s  %s"
                         % (ev.calendar, ev.fmt(), ev.start.strftime("%m-%d"), ev.title or "(无标题)"))
            L.append("")
            L.append("</details>")
            L.append("")
        if p.all_day:
            L.append("<details><summary>跳过的全天事件（%d）</summary>" % len(p.all_day))
            L.append("")
            for ev in p.all_day:
                L.append("- `%s` %s" % (ev.calendar, ev.title or "(无标题)"))
            L.append("")
            L.append("</details>")
            L.append("")
    return "\n".join(L) + "\n"


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ================================================================ 命令


def build(load_fn, args):
    """拉事件 → 过滤 profile → 排一周。返回 (plans, meta, selected_events)。

    ``args.profiles`` / ``temp/calendar.json`` 的 ``download_calendars`` 决定只下载
    哪几个日历（默认 工作 + 空闲）。如果没有一个目标 profile 存在，退回旧的
    ignore/use 语义，避免把一个结构不同的日历直接清空。
    """
    cfg = load_config()
    ignore = args.ignore if args.ignore is not None else (cfg.get("ignore_calendars") or DEFAULT_IGNORE)
    use = args.use if args.use is not None else (cfg.get("use_calendars") or [])
    profiles = (args.profiles if getattr(args, "profiles", None)
                else cfg.get("download_calendars") or DEFAULT_PROFILES)
    start_date = (datetime.date.fromisoformat(args.start) if args.start
                  else datetime.datetime.now().date())
    now = datetime.datetime.now(LOCAL_TZ)
    horizon_start = datetime.datetime.combine(start_date, datetime.time.min).replace(tzinfo=LOCAL_TZ)
    horizon_end = horizon_start + datetime.timedelta(days=args.days)

    events, names = load_fn(horizon_start, horizon_end)
    selected, mode, used = select_profile_events(
        events, profiles, ignore, use, args.include_all_day)
    # profile 模式：候选只有 工作/空闲，ignore/use 不参与。
    # legacy 模式：把**全部事件**交给 plan_week，让它照旧把 ignored 事件列进 md。
    plan_events = selected if mode == "profiles" else events
    plan_ignore = [] if mode == "profiles" else ignore
    plan_use = [] if mode == "profiles" else use
    plans = plan_week(plan_events, start_date, args.days, plan_ignore, plan_use,
                      args.include_all_day, now)

    if args.ics:
        source = "本地 .ics：%s" % "、".join(args.ics)
    else:
        source = "CalDAV · %s（%s）" % (cfg.get("url") or DEFAULT_URL, cfg.get("username") or "?")
    meta = {"stamp": stamp(), "source": source, "ignore": ignore, "use": use,
            "calendars": names, "days": args.days, "start_date": start_date,
            "profiles": list(profiles), "used_profiles": used,
            "download_mode": mode, "selected_events": selected}
    return plans, meta, selected


def print_summary(plans, meta, events):
    info("")
    info("拉到 %d 个事件（%d 天：%s ~ %s）"
         % (len(events), meta["days"], plans[0].date.isoformat(), plans[-1].date.isoformat()))
    info("忽略分组：%s" % ("、".join(meta["ignore"]) or "（无）"))
    info("")
    total_min = 0
    for p in plans:
        mins = sum(d for _, d in p.sessions)
        total_min += mins
        if p.sessions:
            body = "  ".join("%s(%d)" % (s.strftime("%H:%M"), d) for s, d in p.sessions)
        else:
            body = "—"
        info("  %s %s  %s" % (p.date.isoformat(), CN_WEEKDAYS[p.date.weekday()], body))
        if p.ignored:
            info("        （忽略分组挡掉 %d 个事件）" % len(p.ignored))
        if p.all_day:
            info("        （跳过 %d 个全天事件）" % len(p.all_day))
    info("")
    info("合计 %d 场 / %d 分钟" % (sum(len(p.sessions) for p in plans), total_min))


def make_loader(args):
    """挑数据源，返回 load_fn(start, end) -> (events, 日历名列表)。"""
    if args.ics:
        if icalendar is None:                    # 离线模式一样要 icalendar 解析
            die("离线模式也需要 icalendar。先跑：python3 download_calendar.py install-deps")
        return lambda a, b: fetch_ics(args.ics, a, b)
    require_deps()
    cfg = load_config()
    if not cfg:
        die("还没有配置。先跑：python3 download_calendar.py login")
    profiles = getattr(args, "profiles", None) or cfg.get("download_calendars") or DEFAULT_PROFILES
    return lambda a, b: fetch_caldav(cfg, a, b, profiles=profiles)


def cmd_fetch(args):
    """只下载日历（工作 + 空闲），写中间层 JSON 和给人看的 md，不碰 data.csv。"""
    plans, meta, events = build(make_loader(args), args)
    download = make_download(meta)
    meta["periods"] = download
    print_summary(plans, meta, events)
    md = render_md(plans, meta)
    md_out = args.md or DEFAULT_MD
    if args.dry_run:
        info("")
        info(md)
        if download is not None:
            info("---- 中间层 JSON（预览，不写：%s）----" % download_json_path(args))
            info(json.dumps(download, ensure_ascii=False, indent=2))
        return 0
    write_text(md_out, md)
    jpath = write_download(download, download_json_path(args))
    info("")
    info("md 写到 %s" % md_out)
    if jpath:
        info("日历中间层写到 %s（%d 个时段）"
             % (jpath, len((download or {}).get("periods") or [])))
    return 0


def _print_download_summary(download):
    periods = (download or {}).get("periods") or []
    info("")
    info("中间层：%d 个时段（%d 个事件，profile：%s）"
         % (len(periods), len((download or {}).get("events") or []),
            "、".join((download or {}).get("profiles") or []) or "旧过滤"))
    for day in sorted(set(p.get("date") for p in periods)):
        items = [p for p in periods if p.get("date") == day]
        info("  %s  %s" % (day, "  ".join(
            "%s %s–%s %s" % (p.get("id"), p.get("start"), p.get("end"),
                             p.get("name") or p.get("type") or "")
            for p in items)))


def _populate_from_download(args):
    """从已经下载好的中间层生成 data.csv；不再联网。"""
    if sp is None:
        die("缺少 session_plan.py，无法从下载层生成 session")
    path = args.from_download if isinstance(args.from_download, str) else None
    download = sp.load_download(path)
    if not download.get("periods"):
        die("没有可用的下载结果（%s）。先跑 download 命令。" % sp.download_path(path))
    layouts = sp.load_layouts(getattr(args, "layouts", None))
    sessions = sp.all_sessions(download, layouts)
    csv = sp.render_data_csv(download, layouts, date_specific=True)
    counts = sp.plan_counts(download, layouts)
    _print_download_summary(download)
    info("")
    info("按 layout 生成 %d 场 session（另：一次性 %d 场，长期规则 %d 条）"
         % (len(sessions), counts.get("extra", 0), counts.get("rule", 0)))

    if args.dry_run:
        info("")
        info("---- data.csv（预览，不写）----")
        info(csv)
        return 0

    if not sessions and not counts.get("extra") and not counts.get("rule") \
            and not args.force:
        die("下载层里没有生成任何 session —— 检查时段/layout。现有 data.csv 没动。")

    if os.path.exists(DATA_CSV):
        os.makedirs(TEMP, exist_ok=True)
        shutil.copy2(DATA_CSV, BACKUP_CSV)
        info("旧 data.csv 备份到 %s" % BACKUP_CSV)
    write_text(DATA_CSV, csv)
    info("")
    info("data.csv 已更新：%s（精确日期行；daemon 会按日期读取）" % DATA_CSV)
    return 0


def cmd_populate(args):
    """拉/读日历 → 生成 session → 写 data.csv。

    默认会先下载日历（写中间层），再按中间层 + 已保存的 layout 生成。
    ``--from-download`` 直接复用上一次下载，不联网。
    """
    if getattr(args, "from_download", False):
        return _populate_from_download(args)

    plans, meta, events = build(make_loader(args), args)
    download = make_download(meta)
    meta["periods"] = download
    csv = None
    if meta.get("download_mode") == "profiles" and download is not None:
        layouts = sp.load_layouts(getattr(args, "layouts", None))
        csv = sp.render_data_csv(download, layouts, date_specific=True)
        info("生成模式：日历下载层 → 时段 layout → 精确日期 data.csv")
        _print_download_summary(download)
    else:
        csv = render_csv(plans, meta)
    print_summary(plans, meta, events)
    md = render_md(plans, meta)
    md_out = args.md or DEFAULT_MD

    if args.dry_run:
        info("")
        info("---- data.csv（预览，不写）----")
        info(csv)
        return 0

    if download is not None and meta.get("download_mode") == "profiles":
        counts = sp.plan_counts(download, sp.load_layouts(getattr(args, "layouts", None)))
        has_sessions = bool(counts.get("period") or counts.get("extra")
                            or counts.get("rule"))
    else:
        has_sessions = any(p.sessions for p in plans)
    if not has_sessions and not args.force:
        die("这一周没切出任何 session——多半是过滤/模型不对。\n"
            "  确认一下日历内容，或加 --force 强行写空 data.csv，"
            "或加 --include-all-day / 调 --ignore。\n"
            "（现有 data.csv 没动）")

    if os.path.exists(DATA_CSV):
        os.makedirs(TEMP, exist_ok=True)
        shutil.copy2(DATA_CSV, BACKUP_CSV)
        info("旧 data.csv 备份到 %s" % BACKUP_CSV)
    write_text(DATA_CSV, csv)
    write_download(download, download_json_path(args))
    if not args.no_md:
        write_text(md_out, md)
        info("md 写到 %s" % md_out)
    info("")
    info("data.csv 已更新：%s" % DATA_CSV)
    return 0


def cmd_layout(args):
    """改一个时段的 session 排布：layout T1 50 50 [--date YYYY-MM-DD]。

    先完整验证（超了就报错、什么都不写），再把 layout 落盘并重新生成 data.csv。
    """
    if sp is None:
        die("缺少 session_plan.py")
    day = args.date or datetime.date.today().isoformat()
    durations = [str(x) for x in args.durations]
    period_id = args.period_id.upper()
    try:
        if args.dry_run:
            # 验证但**不写 layout**：dry-run 必须零副作用。
            periods = sp.periods_for_date(args.from_download, day)
            period = next((p for p in periods if p.get("id") == period_id), None)
            if period is None:
                raise ValueError("没有时段 %s（%s）" % (period_id, day))
            sessions = sp.plan_period(period, durations)
        else:
            sessions = sp.set_layout(day, period_id, durations,
                                     layouts_path_=args.layouts,
                                     download_path_=args.from_download)
    except ValueError as e:
        die(str(e))
    download = sp.load_download(args.from_download)
    layouts = sp.load_layouts(args.layouts)
    csv = sp.render_data_csv(download, layouts, date_specific=True)
    info("✅ %s %s → %s" % (day, period_id,
                            "  ".join("%s–%s(%d)" % (s["start"], s["end"], s["duration"])
                                      for s in sessions) or "（空）"))
    if args.dry_run:
        info("---- data.csv（预览，不写）----")
        info(csv)
        return 0
    if os.path.exists(DATA_CSV):
        os.makedirs(TEMP, exist_ok=True)
        shutil.copy2(DATA_CSV, BACKUP_CSV)
    write_text(DATA_CSV, csv)
    counts = sp.plan_counts(download, layouts)
    info("data.csv 已重算：%s（时段 %d / 一次性 %d / 长期规则 %d）"
         % (DATA_CSV, counts.get("period", 0), counts.get("extra", 0),
            counts.get("rule", 0)))
    return 0


def cmd_show(args):
    if sp is None:
        die("缺少 session_plan.py")
    return sp.cmd_show([])

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="从 iCloud 日历（CalDAV）生成 Focusmate session 计划 → data.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：\n"
               "  python3 download_calendar.py login\n"
               "  python3 download_calendar.py populate --dry-run\n"
               "  python3 download_calendar.py populate --ics my.ics --days 7\n")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("install-deps", help="把 caldav 装到 temp/pylibs")
    p.add_argument("--spec", default=None, help="pip 包规格，默认 %s" % DEFAULT_PIP_SPEC)
    p.set_defaults(func=cmd_install_deps)

    p = sub.add_parser("login", help="登录 iCloud 并保存配置（App 专用密码）")
    p.add_argument("--url", default=None, help="CalDAV 地址，默认 %s" % DEFAULT_URL)
    p.add_argument("--username", default=None, help="Apple ID 邮箱")
    p.add_argument("--password", default=None, help="App 专用密码（不给就交互输入）")
    p.add_argument("--ignore", nargs="*", default=None, help="忽略的分组（日历名），默认 上课 休息")
    p.add_argument("--use", nargs="*", default=None, help="白名单分组（只在这些日历里排 session）")
    p.add_argument("--no-verify", action="store_true", help="不联网验证，直接保存")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="删掉保存的配置")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("status", help="看依赖 / 配置 / 日历列表")
    p.add_argument("--live", action="store_true", help="真连一次，列出日历名")
    p.set_defaults(func=cmd_status)

    def common(p):
        p.add_argument("--days", type=int, default=8,
                       help="排几天，默认 8（今天 + 之后 7 天）")
        p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD，默认今天")
        p.add_argument("--profiles", nargs="*", default=None,
                       help="只下载这些日历/profile，默认 工作 空闲")
        p.add_argument("--ignore", nargs="*", default=None, help="旧模式：忽略的分组（默认 上课 休息）")
        p.add_argument("--use", nargs="*", default=None, help="旧模式：白名单分组")
        p.add_argument("--include-all-day", action="store_true", help="把全天事件当整天可用")
        p.add_argument("--ics", action="append", default=None, help="离线：读本地 .ics（可重复）")
        p.add_argument("--md", default=None, help="md 输出路径，默认 %s" % DEFAULT_MD)
        p.add_argument("--download-json", default=None,
                       help="中间层 JSON 路径，默认 %s" % DEFAULT_DOWNLOAD_JSON)
        p.add_argument("--layouts", default=None,
                       help="自定义排布 JSON，默认 temp/session-layouts.json")
        p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")

    p = sub.add_parser("fetch", aliases=["download"],
                       help="只下载 工作/空闲 日历 → 中间层 JSON + md，不碰 data.csv")
    common(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("populate", help="日历（下载层）→ 时段 layout → 精确日期 data.csv")
    common(p)
    p.add_argument("--from-download", nargs="?", const=True, default=False,
                   help="复用已有的下载层，不联网（可给 JSON 路径）")
    p.add_argument("--force", action="store_true", help="即使一个 session 都没切出来也写")
    p.add_argument("--no-md", action="store_true", help="不写 md")
    p.set_defaults(func=cmd_populate)

    p = sub.add_parser("layout", help="改一个时段的 session 排布：layout T1 50 50")
    p.add_argument("period_id", help="时段 ID，如 T1")
    p.add_argument("durations", nargs="+", help="每场时长，只认 25/50/75")
    p.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认今天")
    p.add_argument("--from-download", default=None, help="下载层 JSON 路径")
    p.add_argument("--layouts", default=None, help="自定义排布 JSON 路径")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    p.set_defaults(func=cmd_layout)

    p = sub.add_parser("show", help="显示下载层 + 每个时段的 session 排布")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return None
    return args


def main(argv=None):
    args = parse_args(argv)
    if args is None:
        return 1
    days = getattr(args, "days", None)
    if days is not None and days < 1:
        die("--days 至少 1（给了 %d）" % days)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        info("")
        warn("被 Ctrl+C 打断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
