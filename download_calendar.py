#!/usr/bin/env python3
"""download_calendar.py —— 从 iCloud 日历（CalDAV）拉时间，算成 Focusmate session，写进 data.csv。

它在整套系统里的位置
--------------------
    iCloud 日历 ──CalDAV──▶ 事件 ──切分──▶ 一周 session 计划 ──▶ data.csv
                                                  └──▶ temp/calendar.md（中间产物，给人看）

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
    python3 download_calendar.py fetch                  # 只拉日历，出 temp/calendar.md
    python3 download_calendar.py populate               # 拉 → 切 → 生成未来一周 data.csv
    python3 download_calendar.py populate --dry-run     # 只打印，不写文件
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
TEMP = os.path.join(HERE, "temp")
PYLIBS = os.path.join(TEMP, "pylibs")
CONFIG_FILE = os.path.join(TEMP, "calendar.json")
DEFAULT_MD = os.path.join(TEMP, "calendar.md")
DATA_CSV = os.path.join(HERE, "data.csv")
BACKUP_CSV = os.path.join(TEMP, "data.csv.bak")

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


def fetch_caldav(cfg, horizon_start, horizon_end):
    """连 CalDAV 把窗口内的事件全拉下来。返回 (events, 所有日历名)。"""
    client = connect(cfg)
    events, names = [], []
    try:
        principal = client.principal()
        cals = list(principal.calendars())
        for cal in cals:
            name = safe_display_name(cal)
            names.append(name)
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


def plan_week(events, start_date, days, ignore, use, include_all_day, now):
    """把事件按天切成 session 计划。今天只保留还没开始的。"""
    plans = []
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
            s = max(ev.start, d0)
            e = min(ev.end, d1)
            if day == now.date():
                s = max(s, now)                       # 今天：已经过去的时段不再排
            if e <= s:
                continue
            windows.append((s, e))
        merged = merge_windows(windows)
        keep = lambda w: (w[1] - w[0]).total_seconds() >= MIN_DURATION * 60      # noqa: E731
        usable = [w for w in merged if keep(w)]
        short = [w for w in merged if not keep(w)]        # <25min，Focusmate 订不了
        sessions = [x for x in carve(usable) if x[0] > now]
        plans.append(DayPlan(date=day, windows=usable, sessions=sessions,
                             ignored=ignored, all_day=allday, short=short))
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
    """拉事件 → 排一周。返回 (plans, meta)。"""
    cfg = load_config()
    ignore = args.ignore if args.ignore is not None else (cfg.get("ignore_calendars") or DEFAULT_IGNORE)
    use = args.use if args.use is not None else (cfg.get("use_calendars") or [])
    start_date = (datetime.date.fromisoformat(args.start) if args.start
                  else datetime.datetime.now().date())
    now = datetime.datetime.now(LOCAL_TZ)
    horizon_start = datetime.datetime.combine(start_date, datetime.time.min).replace(tzinfo=LOCAL_TZ)
    horizon_end = horizon_start + datetime.timedelta(days=args.days)

    events, names = load_fn(horizon_start, horizon_end)
    plans = plan_week(events, start_date, args.days, ignore, use, args.include_all_day, now)

    if args.ics:
        source = "本地 .ics：%s" % "、".join(args.ics)
    else:
        source = "CalDAV · %s（%s）" % (cfg.get("url") or DEFAULT_URL, cfg.get("username") or "?")
    meta = {"stamp": stamp(), "source": source, "ignore": ignore, "use": use,
            "calendars": names, "days": args.days}
    return plans, meta, events


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
    return lambda a, b: fetch_caldav(cfg, a, b)


def cmd_fetch(args):
    plans, meta, events = build(make_loader(args), args)
    print_summary(plans, meta, events)
    md = render_md(plans, meta)
    out = args.md or DEFAULT_MD
    if args.dry_run:
        info("")
        info(md)
        return 0
    write_text(out, md)
    info("")
    info("md 写到 %s" % out)
    return 0


def cmd_populate(args):
    plans, meta, events = build(make_loader(args), args)
    print_summary(plans, meta, events)
    md = render_md(plans, meta)
    csv = render_csv(plans, meta)
    md_out = args.md or DEFAULT_MD

    if args.dry_run:
        info("")
        info("---- data.csv（预览，不写）----")
        info(csv)
        return 0

    if not any(p.sessions for p in plans) and not args.force:
        die("这一周没切出任何 session——多半是过滤/模型不对。\n"
            "  确认一下日历内容，或加 --force 强行写空 data.csv，"
            "或加 --include-all-day / 调 --ignore。\n"
            "（现有 data.csv 没动）")

    if os.path.exists(DATA_CSV):
        os.makedirs(TEMP, exist_ok=True)
        shutil.copy2(DATA_CSV, BACKUP_CSV)
        info("旧 data.csv 备份到 %s" % BACKUP_CSV)
    write_text(DATA_CSV, csv)
    if not args.no_md:
        write_text(md_out, md)
        info("md 写到 %s" % md_out)
    info("")
    info("data.csv 已更新：%s" % DATA_CSV)
    return 0


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
        p.add_argument("--days", type=int, default=7, help="排几天，默认 7")
        p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD，默认今天")
        p.add_argument("--ignore", nargs="*", default=None, help="忽略的分组（默认 上课 休息）")
        p.add_argument("--use", nargs="*", default=None, help="白名单分组")
        p.add_argument("--include-all-day", action="store_true", help="把全天事件当整天可用")
        p.add_argument("--ics", action="append", default=None, help="离线：读本地 .ics（可重复）")
        p.add_argument("--md", default=None, help="md 输出路径，默认 %s" % DEFAULT_MD)
        p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")

    p = sub.add_parser("fetch", help="只拉日历出 md，不碰 data.csv")
    common(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("populate", help="拉日历 → 切 session → 写 data.csv（一次性，手动触发）")
    common(p)
    p.add_argument("--force", action="store_true", help="即使一个 session 都没切出来也写")
    p.add_argument("--no-md", action="store_true", help="不写 md")
    p.set_defaults(func=cmd_populate)

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
