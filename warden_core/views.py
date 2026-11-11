"""warden_core.views —— 六个 Tab 视图（契约 / 专注 / 统计 / 数据 / 设置 / 琐事）。"""
import dataclasses
import datetime

from .common import (
    Panel,
    SESSION_TABLE_COLUMNS,
    STYLE_DONE,
    STYLE_DROPPED,
    STYLE_MUTED,
    TASK_CONTENT_COLUMN,
    TASK_PROJECT_MAX_LEN,
    TASK_TABLE_COLUMNS,
    Table,
    Text,
    _crop_display,
    box,
    cfg,
    fmt_mmss,
    format_compact_timestamp,
    ks,
    note_renderable,
    repl_kit,
    task_dynamic_keys,
    task_table_headers,
    th,
    ts,
)
from .hints import (
    _cmd,
    _hint_actions,
    _hint_cancel,
    _hint_day,
    _hint_mute,
    _hint_mute_universal,
    _hint_period,
    _hint_refresh,
    _hint_relocate,
    _hint_session,
    _hint_set,
    _hint_stat,
    _hint_sync,
    _hint_title,
    _task_command,
)

class View(object):
    """一个视图 = 一套命令 + 一块看板 + 一行状态。

    业务留在子类里（render / status_line / cmd_* / idle_hint），
    对外统一经过 to_view() 变成 repl_kit 的 View：命令表、提示行、
    看板、状态行、进视图时的钩子、提示符，全由框架接管。
    """

    key = "?"
    name = "view"
    label = "视图"

    def __init__(self, w):
        self.w = w

    def render(self):
        """本视图的主体内容：rich renderable，或它们的列表。返回 None = 不渲染主体。"""
        return None

    def status_line(self):
        """提示符上方那行「随时在变的数字」，纯文本（无 markup）。None = 不需要定时刷新。"""
        return None

    def status_box(self):
        """「状态」框里本视图自己的那几行；None = 这个视图不要框。

        警告由框架追加（repl_kit 的 WarningBoard），这里只管业务状态。
        """
        return None

    def idle_hint(self):
        """没在输入命令时的那行提示：返回 repl_kit.Hint（或 None = 用框架默认）。"""
        return None

    def commands(self):
        """本视图认识的命令：[(repl_kit.Command, ...)]。"""
        return []

    def unknown_hint(self):
        return "「%s」视图不认这个命令（Shift+Tab 切视图；help 看当前视图的命令）。" % self.name

    # -- 所有视图都认的命令 --------------------------------------------

    def common_commands(self):
        w = self.w
        return [
            _cmd("quit", lambda a: w.exit_tui(),
                 "退出（bridge 和浏览器一起收掉）", ("exit", "q"), "通用",
                 usage="quit", hint=_hint_actions("quit", note="bridge 和浏览器一起收掉")),
            _cmd("help", w.cmd_help, "命令列表（help --task / --data = 数据视图详细说明）",
                 ("?", "h"), "通用", usage="help [--task|--data]",
                 hint=_hint_actions("help", "--task", "--data",
                                    note="当前视图的全部命令")),
            _cmd("view", lambda a: w.cycle_view(),
                 "下一个视图", ("tab", "switch"), "通用",
                 usage="view", hint=_hint_actions("view", note="等同 Shift+Tab")),
            _cmd("open", w.cmd_open,
                 "open task|project|op 切换数据模型；open working|detail 切口径；"
                 "open <表头> [--permanent] 打开当前表的一列",
                 ("看板",), "通用",
                 usage="open task|project|op | open working|detail | open <列名> [--permanent]",
                 options=tuple(dict.fromkeys(
                     ("task", "project", "op", "working", "detail")
                     + tuple(h for h, _kw in SESSION_TABLE_COLUMNS)
                     + tuple(h for h, _kw in TASK_TABLE_COLUMNS)
                     + (TASK_CONTENT_COLUMN,))),
                 hint=_hint_actions("task", "project", "op", "working", "detail",
                                    "时段类型", "id", "状态",
                                    note="open task/project/op = 看三个数据模型；"
                                         "open 表头名 = 展开列")),
            _cmd("close", w.cmd_close,
                 "close <表头> [--permanent]：折叠当前表的一列（--permanent = 永久折叠）",
                 ("关闭",), "通用", usage="close <列名> [--permanent]",
                 options=tuple(dict.fromkeys(
                     tuple(h for h, _kw in SESSION_TABLE_COLUMNS)
                     + tuple(h for h, _kw in TASK_TABLE_COLUMNS)
                     + (TASK_CONTENT_COLUMN, "--permanent"))),
                 hint=_hint_actions("时段类型", "id", "状态",
                                    note="close 表头名 = 折叠列；数据视图操作任务表")),
            _cmd("stat", w.cmd_stat_from_args, "生涯统计（stat refresh / full）",
                 ("stats",), "通用", usage="stat [refresh [full]]",
                 options=("refresh", "full"), hint=_hint_stat),
            _cmd("status", lambda a: None, "只重画看板", ("s",), "通用",
                 usage="status", hint=_hint_actions("status", note="只重画看板")),
            _cmd("set", w.cmd_set,
                 "改一个设置：set <id> <value>（不写 value = 恢复默认；任何视图都能敲）",
                 usage="set <设置 id> <value>",
                 completer=lambda prior: [row["id"] for row in w.settings.rows()],
                 hint=_hint_set),
            _cmd("warn", w.cmd_warn,
                 "状态框里的警告：warn on|off（不写 = 取反）",
                 ("warnings",), "通用", usage="warn [on|off]",
                 options=("on", "off"),
                 hint=_hint_actions("on", "off", note="不写就是取反")),
            _cmd("conscription", w.cmd_conscription,
                 "快速征召：把一条合适时长的任务塞进当前/下一场 session",
                 ("cst", "征召", "抓任务"),
                 usage="conscription / cst [session序号|current]",
                 options=("current",),
                 hint=_hint_actions("cst", "current",
                                    note="进行中的场只有一条任务时优先用它的项目；"
                                         "按剩余时间每 5 分钟向下取档；"
                                         "不足 10 分钟先找 errand 项目")),
            _task_command(w),
        # say 用 lambda 晚绑定：换 console（测试、--exec）时输出跟着走
        ] + repl_kit.job_commands(w.jobs, say=lambda text: w.shell.print(text))

    def build_registry(self):
        w = self.w
        guarded = [
            dataclasses.replace(command, handler=w.guard(command.handler))
            for command in self.commands() + self.common_commands()
        ]
        return repl_kit.Registry(guarded)

    def hint_line(self, text, parts):
        """repl_kit 的提示行回调。

        空行（没在输入）→ 本视图的 idle_hint；已经开始输入 → None，
        让框架去问 `Command.hint`（参数 / 取值 / 选项），没有就回落到
        框架自动生成的 `命令: ...`。这和 paper_tui 的钩子完全同构。
        """
        if parts:
            return None
        return self.idle_hint()

    def to_view(self):
        """变成框架认识的那个 View。"""
        w = self.w
        return repl_kit.View(
            name=self.name,
            registry=self.build_registry(),
            hint=self.hint_line,
            status=self.status_line,
            status_box=self.status_box,
            render=w.frame,
            on_enter=w.enter_view,
            prompt=w.prompt_text,
        )


VIEWS = []


def register_view(cls):
    VIEWS.append(cls)
    return cls


@register_view
class ContractView(View):
    """契约 / 预约视图：就是原来那套表格 + 命令。"""

    key = "1"
    name = "契约"
    label = "契约 / 预约"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令",
            ("session", "period", "cancel", "mute", "mute-universal",
             "title", "day", "sync", "open working", "open detail",
             "close 时段类型", "help", "quit"),
            note="Tab 切下一个视图；open/close + 表头名折叠列；open working|detail 切口径",
        )

    def status_box(self):
        """「状态」框里属于本视图的那几行（警告由框架追加在后面）。"""
        return self.w.render_status_rows()

    def render(self):
        # 收尾面板不在这儿画：它是 repl_kit 的「打断栏位」（见 render_interrogate），
        # 换视图 / 表格变长都不该把它挤走。
        out = [self.w.render_table()]
        if self.w.rows_note:
            out.append(Text("  " + self.w.rows_note, style="dim"))
        # 看未来日期时，额外给一张「这一天每个 session 挂什么任务、属于哪个项目」
        # 的编排表；今天已经有契约表 + 任务面板，不重复占高度。
        binder = self.w.render_day_binder()
        if binder is not None:
            out.append(binder)
        # 契约表格下面那块任务表格（Recent 由框架接在后面）
        index = self.w.render_task_index()
        if index is not None:
            out.append(index)
        # 任务面板**下面**那块项目表（剩余 count / 进度 / 今天排了多少）
        projects = self.w.render_projects_table()
        if projects is not None:
            out.append(projects)
        return out

    def unknown_hint(self):
        return "「契约」视图不认这个命令（Shift+Tab 切视图；help 看当前视图的命令）。"

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", w.cmd_refresh,
                 "全量刷新看板（session/任务完成状态/统计/收尾面板）；"
                 "--check 再对账并修正", ("r", "reload"),
                 usage="refresh [--check]",
                 options=("--check", "-c"),
                 hint=_hint_refresh),
            _cmd("day", w.cmd_day, "切换看哪一天（-1/0/1 或 YYYY-MM-DD）",
                 usage="day <-1|0|1|YYYY-MM-DD>",
                 options=("-1", "0", "1"),
                 hint=_hint_day),
            _cmd("session", w.cmd_session,
                 "加一场（默认只加这一天；--permanent 才写进日历）",
                 usage="session [--permanent] <HH:MM> <25|50|75> | session <T1> [25|50|75 ...]",
                 options=("--permanent", "25", "50", "75"),
                 hint=_hint_session),
            _cmd("period", w.cmd_period_layout,
                 "改一个时段的排布（period T1 50 50）或静音它（period mute T1）",
                 usage="period <T1> [25|50|75 ...] | period mute|unmute <T1> [--permanent]",
                 options=("25", "50", "75", "mute", "unmute", "--permanent"),
                 hint=_hint_period),
            _cmd("relocate", w.cmd_relocate,
                 "临时挪一场（#N）或整个时段（T1）：relocate N ±MM / relocate T1 ±MM",
                 usage="relocate <n>|T1 ±MM",
                 options=("-30", "-15", "+15", "+30"),
                 hint=_hint_relocate),
            _cmd("force-check", lambda a: w.cmd_force_check(),
                 "忽略重试上限，把所有没订上的各强制重订一次", ("forcecheck",),
                 usage="force-check",
                 hint=_hint_actions("force-check", note="忽略重试上限，全部重订一次")),
            _cmd("sync", w.cmd_sync, "跑 download_calendar.py populate：日历 → data.csv",
                 usage="sync [--days N]", options=("--days",),
                 hint=_hint_sync),
            _cmd("mute", w.cmd_mute, "把 #N 静音：mute N on|off",
                 usage="mute <n> on|off", options=("on", "off"),
                 hint=_hint_mute),
            _cmd("mute-universal", w.cmd_mute_universal,
                 "每周固定时间窗静音：所有与窗口有重叠的 session（不绑 T 时段）",
                 ("unmute-universal", "universal-mute"),
                 usage="mute-universal 13:00 - 15:00 Mon | mute-universal off ... | list",
                 options=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
                          "off", "list"),
                 hint=_hint_mute_universal),
            _cmd("title", w.cmd_title, '把 #N 的标题改成文本：title N "文本"', ("t",),
                 usage='title <n> "文本"', hint=_hint_title),
            _cmd("cancel", w.cmd_cancel, "取消 #N（默认只弃这一场；--permanent 删日历）",
                 usage="cancel <n> [--permanent]", hint=_hint_cancel),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="结束后 15 秒 daemon 会自动拉你回来收尾")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E/G）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
            _cmd("plan", w.cmd_plan,
                 "一次分配：按每个项目的阶段取其当前 SOP/incubator op，再排任务",
                 ("排期", "分配"),
                 usage="plan [+1|今天|明天|YYYY-MM-DD] → "
                       "task <序号> <count> | task <序号> clear | complete；"
                       "或 plan -s <session> -p <project> 单场直排",
                 options=("+1", "+2", "today", "明天", "complete", "task 1 4",
                          "-s 1 -p 1"),
                 hint=_hint_actions("+1", "明天", "complete", "task 1 4", "task 1 clear",
                                    "-s 1 -p 1",
                                    note="不带参数 = 今天；plan +1 = 明天；"
                                         "plan -s <session> -p <project> = 把这个项目的"
                                         "内容按该项目 sequential/parallel 规则排进这一场；"
                                         "parallel 用 OmniFocus tag P1/P2 分优先级并按 session "
                                         "从早到晚排；普通 plan 重排会先撤掉当天旧计划；"
                                         "每个项目只出现当前阶段对应的 op；超预算整条命令不执行")),
            _cmd("project", w.cmd_project,
                 "管理真正项目 Project (pro)：pro | pro 1 | pro 1 -m complete|discard | "
                 "pro 1 -a | pro 1 interrupt | pro create",
                 ("pro", "pr", "项目"),
                 usage="pro [<序号> [-a <session>|-ac|--advance [op <N>|task import] | "
                       "-m complete|discard | interrupt [--soft|--hard] [--check|--list] | "
                       "file <路径> | open | refresh | remove] | create <名称>]",
                 options=("--advance", "-a", "-ac", "-m", "--mark", "complete", "discard",
                          "active", "interrupt", "--soft", "-s", "--hard", "--check",
                          "--list", "create", "file", "open", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -a 3", "1 -ac", "1 -m complete",
                                    "1 -m discard", "1 -m active",
                                    "1 interrupt", "1 interrupt --check",
                                    "1 interrupt --hard", "create",
                                    note="pro = 真正项目（running/bottleneck）；"
                                         "-a <session> / -ac = 直接把该项目第一条任务挂到"
                                         "指定场 / 当前场；interrupt 默认 soft（记录建议，"
                                         "A/B/C 分类）；--check 逐条检查；-m complete|discard "
                                         "只动本地状态，默认 disposable filter 会隐藏")),
            _cmd("op", w.cmd_op,
                 "管理 OmniFocus Project (op)：op | op 1 -m complete|discard | "
                 "op 1 sop|incubator | op 1 relate --export|--import | "
                 "op import [sop|incubator]",
                 ("ofp", "omnifocus-project"),
                 usage="op [<序号> [-m complete|discard | sop|incubator | pro <N> | "
                       "relate --export|--import [路径] | refresh|remove] | "
                       "import [sop|incubator]]",
                 options=("-m", "--mark", "complete", "discard", "active", "sop",
                          "incubator", "import", "relate", "--export", "--import",
                          "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -m complete", "1 -m discard", "1 -m active",
                                    "1 sop", "1 incubator", "1 relate --export",
                                    "import sop", "refresh",
                                    note="op = OmniFocus Project（SOP / incubator）；"
                                         "-m complete|discard 只动本地状态")),
            _cmd("start", lambda a: w.cmd_start(), "启动 daemon",
                 usage="start", hint=_hint_actions("start", note="启动 daemon")),
            _cmd("stop", lambda a: w.cmd_stop(), "停用 daemon（要敲宣言）",
                 usage="stop", hint=_hint_actions("stop", note="停用 daemon（要敲宣言）")),
        ]


@register_view
class FocusView(View):
    """专注视图：当前目标 + 当前 session + 专注模式 + 可暂停次数 + 当前空闲时间。

    这个视图里：`stop` = 消耗一次暂停（暂停 PAUSE_MINUTES 分钟），Shift+Tab =
    循环切换普通 / 做题 / 写作模式。之前那套预约命令在这里**一条都不认** ——
    你要改预约就按 Tab 切回去。
    """
    key = "2"
    name = "专注"
    label = "专注 / 暂停"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("stop",),
            note="消耗一次暂停 · Shift+Tab 切模式 · Tab 切回契约",
        )

    def current_row(self):
        """当前真正在进行的 session 行；没有就 None。

        API 数据权威时只认**有 session id 且时间窗口覆盖现在**的行：
        `data.csv` 里的计划窗口只是预约意图，用户取消后另约了别的时间时，
        不能拿旧的计划窗口继续冒充「我正在这一场」。
        离线/快照模式没有 id，才退回按契约时间判断。
        """
        w = self.w
        if w.authoritative:
            live = w.live_session()
            return live[1] if live else None
        sess = ks.current_session(w.entries, datetime.datetime.now())
        if not sess:
            return None
        day, slot, start, end = sess
        return {"date": day.isoformat(), "startLabel": slot["start"],
                "endLabel": end.strftime("%H:%M"), "durationMin": slot["duration"],
                "id": None, "phase": "进行中"}

    def current_session_key(self):
        """当前进行中的 session key；没有就 None（模式会设为下一场默认）。"""
        row = self.current_row()
        if not row:
            return None
        day = row.get("date") or self.w.sel_date
        if isinstance(day, str):
            try:
                day = datetime.date.fromisoformat(day)
            except ValueError:
                day = self.w.sel_date
        return ks.key_of(day, row.get("startLabel"))

    def mode_key(self):
        return ks.get_idle_mode(self.current_session_key())

    def mode_text(self):
        cfg = ks.idle_mode_config(self.mode_key())
        return "%s（%s）" % (cfg["name"], cfg["desc"])

    def cycle_mode(self):
        """Shift+Tab：在模式集合里循环。绑定到当前 session（没有就设为下一场默认）。"""
        key = self.current_session_key()
        cur = ks.get_idle_mode(key)
        order = ks.IDLE_MODE_ORDER
        idx = order.index(cur) if cur in order else 0
        nxt = order[(idx + 1) % len(order)]
        cfg = ks.set_idle_mode(nxt, key)
        cur_name = ks.idle_mode_config(cur)["name"]
        where = "已绑定本场 session" if key else "已设为下一场 session 的默认"
        self.w.say("专注模式：%s → %s（%s；%s）"
                   % (cur_name, cfg["name"], cfg["desc"], where), "ok")
        return nxt

    def status_line(self):
        """一行实时状态（提示符上面那行）。读输入时每秒原地覆盖，不重画整屏。"""
        now = datetime.datetime.now()
        ps = ks.pause_status(now)
        if ps["active"]:
            pause = "暂停 %s（剩 %d 次）" % (fmt_mmss(ps["left_sec"]), ps["remaining"])
        else:
            pause = "未暂停（剩 %d 次）" % ps["remaining"]
        idle = ks.load_idle_state().get("last_idle_seconds")
        idle_txt = "空闲 ?" if idle is None else "空闲 %ds" % int(idle)
        mode_txt = "模式 %s" % ks.idle_mode_config(self.mode_key())["name"]
        return "%s   ·   %s   ·   %s   ·   可暂停 %d/%d" % (
            mode_txt, pause, idle_txt, ps["remaining"], ks.PAUSE_MAX_PER_DAY)

    def render(self):
        w = self.w
        now = datetime.datetime.now()        # naive 本地时间，和 schedule/daemon 同源
        ps = ks.pause_status(now)
        row = self.current_row()

        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        t.add_row("目标", Text(w.current_goal()))
        if row:
            day = row.get("date") or w.sel_date
            if isinstance(day, str):
                try:
                    day = datetime.date.fromisoformat(day)
                except ValueError:
                    day = w.sel_date
            key = ks.key_of(day, row.get("startLabel"))
            detail = "%s   %s–%s（%smin）" % (
                key, row.get("startLabel"), row.get("endLabel") or "?",
                row.get("durationMin") or "?")
            if row.get("id"):
                detail += "  id=%s" % row["id"]
            t.add_row("当前 session", Text(detail, style="bold green"))
        else:
            t.add_row("当前 session", Text("（没有进行中的 session —— 看门狗也不上班）", style="dim"))

        cfg = ks.idle_mode_config(self.mode_key())
        mode = Text()
        mode.append("%s" % cfg["name"], style="bold magenta")
        mode.append("（Shift+Tab 切换）", style="dim")
        t.add_row("专注模式", mode)
        t.add_row("", Text("阈值：%s 无输入 = 违规；%s"
                           % (ks.fmt_idle_seconds(cfg["threshold_sec"]),
                              "违规前 %s 预警，预警不计次数"
                              % ks.fmt_idle_seconds(cfg.get("warn_before_sec", 0))
                              if cfg.get("warn") else "无预警，直接记违规"),
                           style="dim"))

        quota = Text()
        quota.append("%d / %d" % (ps["remaining"], ks.PAUSE_MAX_PER_DAY), style="bold")
        quota.append("   每次 %d 分钟" % ks.PAUSE_MINUTES, style="dim")
        t.add_row("可暂停次数", quota)

        # 暂停剩余 / 当前空闲是**每秒在变**的，放到下方那一行实时覆盖（见 status_line），
        # 免得为了几个跳动的数字每秒把整屏 clear + 重画（那样会抖）。
        t.add_row("实时", Text("模式 / 暂停倒计时 / 空闲秒数在下方状态行原地刷新", style="dim"))

        return Panel(t, title="[bold]Focus · 专注[/]", border_style="yellow", box=box.ROUNDED)

    def commands(self):
        # 预约那套命令在这个视图里一条都不注册 —— 框架层面就不存在
        return [
            _cmd("stop", lambda a: self.w.cmd_pause(),
                 "消耗一次暂停（暂停 %d 分钟）" % ks.PAUSE_MINUTES,
                 usage="stop",
                 hint=_hint_actions("stop", note="消耗一次暂停（%d 分钟）"
                                    % ks.PAUSE_MINUTES)),
        ]

    def unknown_hint(self):
        return ("「专注」视图只认 stop（消耗一次暂停）；Ctrl+O 切模式；"
                "Shift+Tab 切回契约视图再干别的。")


@register_view
class StatsView(View):
    """统计视图：生涯累计 + 每天分布 + 永不重订名单。

    数据链在 `stats.py`（只读公开 API，按 365 天分块拉全量历史，缓存 1 小时）；
    这个视图只管渲染。口径见 stats.py 的 docstring（完成 = completed=true，
    区间 = memberSince → 昨天，分母含空天）。

    「永不重订」（daemon 永不重订的场次 = 你敲完宣言认过罪的地方）**只在这里**出现，
    一条一行列在最底下 —— 平常视图一个字都不提。
    """

    key = "3"
    name = "统计"
    label = "统计 / 历史"

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("stat", "stat refresh", "refresh"),
            note="Tab 切视图 · 数据：Focusmate 只读 API",
        )

    def status_line(self):
        return None                     # 静态面板，不需要每秒刷新

    def unknown_hint(self):
        return "「统计」视图只认 stat / stat refresh [/full] / refresh；Shift+Tab 切回去再敲别的。"

    def commands(self):
        w = self.w
        # `stat` 由通用命令集提供（任何视图都能敲），这里只补这个视图的 refresh
        return [
            _cmd("refresh", lambda a: w.cmd_stat(),
                 "重新拉一次统计", ("r", "reload"),
                 usage="refresh",
                 hint=_hint_actions("refresh", note="重新拉一次统计（走缓存）")),
        ]

    # -- 渲染

    @staticmethod
    def _bar(value, vmax, width=8):
        """把一天的量画成一小段条（0 就留空）。"""
        if value <= 0:
            return ""
        if vmax <= 0:
            return "▇"
        return "▇" * max(1, int(round(value * width / float(vmax))))

    def _recent_panel(self, s):
        vmax = max([r["completed"] for r in s["recent"]] or [0])
        cells = []
        for r in s["recent"]:
            n = r["completed"]
            bar = self._bar(n, vmax, width=8)
            txt = Text()
            txt.append(r["date"][5:] + " ", style="dim")
            txt.append((bar or "·").ljust(8), style="green" if n else "dim")
            txt.append(" %2d" % n, style="bold" if n else "dim")
            cells.append(txt)
        cols = 7
        t = Table.grid(padding=(0, 1))
        for _ in range(cols):
            t.add_column(no_wrap=True)
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols]
            while len(row) < cols:
                row.append(Text(""))
            t.add_row(*row)
        return Panel(t, title="[bold]最近 %d 天[/]" % len(cells), border_style="dim",
                     box=box.ROUNDED)

    def _month_panel(self, s):
        t = Table.grid(padding=(0, 1))
        for _ in range(4):
            t.add_column(no_wrap=True)
        cells = []
        for m in s["by_month"]:
            txt = Text()
            txt.append(m["month"][2:] + " ", style="dim")
            txt.append("%d" % m["completed"], style="bold green")
            txt.append("/%d" % m["booked"], style="dim")
            cells.append(txt)
        for i in range(0, len(cells), 4):
            row = cells[i:i + 4]
            while len(row) < 4:
                row.append(Text(""))
            t.add_row(*row)
        return Panel(t, title="[bold]按月（完成 / 订了）[/]", border_style="dim",
                     box=box.ROUNDED)

    def _never_panel(self):
        """永不重订名单：一行一条，像无序列表。平常视图里不出现。"""
        abandoned = ks.load_verdicts()["abandoned"]
        t = Table.grid(padding=(0, 1))
        t.add_column(style="red", no_wrap=True)
        t.add_column(no_wrap=True)
        t.add_column(style="dim")
        if not abandoned:
            t.add_row("", Text("（还没有认过罪的场次 —— daemon 会一直重订）", style="dim"))
        for key in sorted(abandoned):
            rec = abandoned[key] or {}
            when = (rec.get("at") or "").replace("T", " ")
            t.add_row("·  " + key.replace("T", " "),
                      "认罪于 " + (when or "?"),
                      (rec.get("note") or rec.get("why") or "")[:48])
        return Panel(t, title="[bold red]永不重订 · 你认过罪的地方[/]",
                     border_style="red", box=box.ROUNDED)

    def render(self):
        w = self.w
        if not w.stat:
            return Panel(
                Text(w.stat_note or "还没拉过。敲 stat 拉一次（Focusmate 只读 API）。",
                     style="dim"),
                title="[bold]📊 Stat · 统计[/]", border_style="green", box=box.ROUNDED)
        s = w.stat
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column()

        t.add_row("生涯", Text("%s → %s · %d 天" % (s["since"], s["until"], s["days"])))
        total = Text()
        total.append("完成 %d" % s["total_completed"], style="bold green")
        total.append("  ·  订了 %d  ·  完成率 %.1f%%" % (s["total_booked"],
                                                        s["completion_rate"]), style="dim")
        t.add_row("总场次", total)
        mean = Text("%.2f 场/天" % s["mean_per_day"], style="bold")
        mean.append("（含空天） · %.2f 场/天（只算做过的 %d 天）"
                    % (s["mean_active_day"], s["active_days"]), style="dim")
        t.add_row("每天平均", mean)
        zero = Text()
        zero.append("%d 天" % s["zero_days"], style="bold red" if s["zero_days"] else "bold green")
        zero.append(" 一个 session 都没做（%.1f%%）" % s["zero_rate"], style="dim")
        t.add_row("空 天", zero)
        t.add_row("峰 值", Text("%s · %d 场" % (s["max_day"]["date"], s["max_day"]["completed"]),
                              style="bold yellow"))
        t.add_row("分 布", Text("p25 %g · 中位数 %g · p75 %g · 最大 %d"
                              % (s["p25"], s["median"], s["p75"], s["max_per_day"])))
        t.add_row("累 计", Text("%.0f 小时 · 每场均值 %.1f 分钟"
                              % (s["total_hours"], s["avg_minutes"])))
        t.add_row("连 续", Text("当前 %d 天 · 最长 %d 天"
                              % (s["streak_current"], s["streak_best"])))
        src = Text((s.get("fetched_at") or "?").replace("T", " "), style="dim")
        src.append(" · %s" % (w.stat_note or "?"), style="dim")
        t.add_row("数 据", src)

        out = [Panel(t, title="[bold]📊 Stat · 统计（Focusmate 只读 API）[/]",
                     border_style="green", box=box.ROUNDED)]
        if s["recent"]:
            out.append(self._recent_panel(s))
        if s["by_month"]:
            out.append(self._month_panel(s))
        out.append(self._never_panel())
        return out



@register_view
class DataView(View):
    """数据视图：同一个界面查看 task / project / op 三种本地数据模型。

    ``open task`` / ``open project`` / ``open op`` 切换当前展示的数据模型：

    * task：本地任务库（data/tasks.json）+ 挂 session + Focusmate 归档；
    * project：真正项目 Project（``pro``，data/projects.json 的 ``pros``）；
    * op：OmniFocus Project（``op``，data/projects.json 的 ``projects``）。

    task 相关的 create/assign/modify 等命令仍然是这个视图的主操作；project / op
    的管理命令也保留，切到对应数据只是把“看什么”切换过去。

    命令统一以 ``task`` 开头（需求里的语法）：::

        task create [id] [field=value ...] [--repeat 周几@HH:MM] [--assign N]
        task delete <序号>
        task modify <序号> <field> <新值>
        task note <序号> <备注文本|网址>    # 备注：完成之后照着去对应地方干活
        task assign <session ID>            # 默认当前选中的任务
        task assign <序号> <session ID>     # 显式指定
        task select <序号>
        task <序号>                         # ↑ 同义：裸序号就是 select
        task <id> -a <session ID>           # 选中 + 挂上去（最省事）
        task <id> -an                       # 从它那一场下架（≡ -a none）
        task <id> -ac                       # 挂到当前这一场（≡ -a current）
        task 32 swap 21                     # 两条任务互换位置（赋闲 ↔ 有场）
        task -s 5 -a 6                      # 把 #5 上的全部任务转移到 #6
        task -s 5 -a none                   # 把 #5 上的全部任务下架
        task repeat <id> <周几@HH:MM|off>   # 每周重复（只影响那一天的那一场）
        task -t                             # 列 trigger 预设（默认空列表）
        task 3 -f end                       # formatter：排到当天最后一段并自动挂上
        task 3 -c 5                         # count = 5（时长跟着变成 125 分钟）
        task 3 -c +2                        # 进度 +2；够数就当完成（勾 Focusmate + 写回）
        task 3 -c -1                        # 进度 -1（最低 0）
        task sync                           # GET /v1/tasks → 把 completedAt 归档
        task archive [N]                    # 看归档（data/task-archive.json）
        sort <字段> [asc|desc] / sort off   # 按字段排序
        group <字段> / group off            # 按字段分组（前置分组列，同组后续留空）
        search <关键词> [--group <字段>]    # 搜索过滤（可顺带分组）
        open 1 / 2 / 3                      # 直接翻到第 N 页

    **编号有两套，别混**：
      * **序号** = 契约视图那块任务面板上的第几条（1 起、连续、refresh/收工后重排）。
        命令里直接写它：``task 3 -an`` 就是「面板第 3 条下架」。
      * **内部 id** = 任务库里那个不变的 id（`task list` 里那一列）。面板上没有的任务
        （别的天处理过的、归档里的）用 ``id:15`` 显式指定。
    """

    key = "4"
    name = "数据"
    label = "数据 / task · project · op"

    #: 当前数据模型：task / project / op。真正的状态放在 Warden 上，
    #: 这样从任何视图敲 `open project` 都能切过来并保持选择。
    @property
    def model(self):
        return getattr(self.w, "data_view_model", "task")

    #: 数据视图的过滤器。disposable 是默认值：只留 active 记录，
    #: 被 mark complete / discard 的 task、pro、op 都从这里消失。
    FILTER_ALIASES = {
        "disposable": "disposable", "active": "disposable", "only": "disposable",
        "可支配": "disposable", "活跃": "disposable",
        "all": "all", "off": "all", "全部": "all", "所有": "all",
        "completed": "completed", "complete": "completed", "done": "completed",
        "完成": "completed",
        "dropped": "dropped", "discard": "dropped", "discarded": "dropped",
        "drop": "dropped", "放弃": "dropped",
    }

    @property
    def data_filter(self):
        raw = str(getattr(self.w, "data_filter", "disposable") or "disposable").lower()
        return self.FILTER_ALIASES.get(raw, "disposable")

    def _status_in_filter(self, status):
        """当前 filter 是否接受一个 active / completed / dropped 状态。"""
        mode = self.data_filter
        status = str(status or ts.STATUS_ACTIVE).lower()
        if mode == "disposable":
            return status == ts.STATUS_ACTIVE
        if mode in ("completed", "dropped"):
            return status == mode
        return True

    def _filter_caption(self):
        mode = self.data_filter
        if mode == "disposable":
            return "filter: disposable（只看 active；隐藏 completed / dropped）"
        return "filter: %s" % mode

    def cmd_data_filter(self, args=None):
        """切换数据视图默认过滤器；disposable 隐藏 completed / dropped。"""
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        if not toks:
            self.w.say("当前数据过滤器：%s。可用：disposable / all / completed / "
                       "dropped（filter off = all）" % self.data_filter, "cmd")
            self.w.invalidate()
            return self.data_filter
        raw = toks[0].lower()
        if raw not in self.FILTER_ALIASES:
            raise ValueError("filter 只认 disposable / active / all / completed / "
                             "dropped / off；你给的是 %r" % toks[0])
        mode = self.FILTER_ALIASES[raw]
        self.w.data_filter = mode
        self.w.task_view_page = None
        self.w.say("🗂 数据视图 filter → %s%s"
                   % (mode, "（隐藏 completed / dropped）" if mode == "disposable"
                      else "（显示全部状态）" if mode == "all" else ""), "cmd")
        self.w.invalidate()
        return mode

    def idle_hint(self):
        return repl_kit.Hint(
            "命令", ("open task", "open project", "open op", "filter",
                     "task create", "task 3 -a 1", "sort", "group project",
                     "search", "inspect-due", "review"),
            note="默认 filter=disposable 只看 active · "
                 "open task/project/op = 切换数据模型（task / 真正项目 pro / "
                 "OmniFocus op）· task 模式 open 1/2/3 翻页 · open/close <表头> 折叠列"
                 " · sort/group/search 操作任务数据 · Tab 切视图",
        )

    def status_line(self):
        return None

    def unknown_hint(self):
        return ("「数据」视图认 open task/project/op 切换数据模型，以及 task "
                "create/delete/modify/assign/select/duplicate/drop/done/link/import/sync/swap、"
                "project、op、sort、group、search、open <页码|表头>、close <表头>、"
                "review、inspect-due（也可以直接写序号，如 task 3 -a 1 / task 3 -an / "
                "task 3 -c +2；序号 = 面板上的第几条，面板外的任务用 id:N）；"
                "Shift+Tab 切回去再干别的。")

    def commands(self):
        w = self.w
        return [
            _cmd("refresh", lambda a: w.cmd_task_sync(quiet=False),
                 "拉 GET /v1/tasks，把已完成任务的 completedAt 归档",
                 ("r", "reload"), usage="refresh",
                 hint=_hint_actions("refresh", note="同步 Focusmate task 完成时间")),
            _cmd("filter", self.cmd_data_filter,
                 "数据视图过滤器（默认 disposable=只看 active）："
                 "filter disposable|all|completed|dropped",
                 ("筛选",), usage="filter [disposable|active|all|completed|dropped|off]",
                 options=("disposable", "active", "all", "completed", "dropped", "off"),
                 hint=_hint_actions("disposable", "all", "completed", "dropped",
                                    note="disposable 隐藏 completed / dropped；"
                                         "all 显示全部")),
            _cmd("sort", w.cmd_task_sort,
                 "任务表排序：sort <字段> [asc|desc] | sort off",
                 ("排序",), usage="sort <字段> [asc|desc] | sort off",
                 options=("id", "序号", "title", "project", "状态", "session", "截止",
                          "duration", "count", "asc", "desc", "off"),
                 hint=_hint_actions("project", "截止", "session", "状态", "duration", "off",
                                    note="默认升序；加 desc 降序；sort off 恢复默认")),
            _cmd("group", w.cmd_task_group,
                 "任务表按字段分组（前置分组列，同组任务聚拢）：group <字段> | group off",
                 ("grouping", "分组"), usage="group <字段> | group off",
                 options=("project", "状态", "session", "截止", "标签", "off"),
                 hint=_hint_actions("project", "状态", "session", "off",
                                    note="前置分组列，同组首行显示组名，后续行留空")),
            _cmd("search", w.cmd_task_search,
                 "搜索任务（支持按字段分组显示）：search <关键词> [--group <字段>] | search off",
                 ("find", "搜索"),
                 usage="search [<关键词>] [-g|--group <字段>] | search off",
                 options=("--group", "-g", "project", "off"),
                 hint=_hint_actions("project", "--group project", "off",
                                    note="搜 title/project/备注/标签/状态；search off 清空过滤")),
            _cmd("review", w.cmd_review,
                 "手动开始一场 session 的收尾（逐个问没做完的任务）", ("收尾",),
                 usage="review [HH:MM | 日期T时刻]",
                 hint=_hint_actions("review", note="A 延期 / B 放弃 / C 重议 / D 补录 / E 推后"
                                                   "（inspect-due 另有 G 延长排期）")),
            _cmd("inspect-due", w.cmd_inspect_due,
                 "主动 inspect 过期没做完的任务（同一套 A/B/C/D/E/G）",
                 ("inspect_due", "due", "过期", "查账"),
                 usage="inspect-due [分钟] [--all]",
                 options=("--all", "60"),
                 hint=_hint_actions("--all", "60", "120",
                                    note="不给参数 = 看已经过期的；给分钟数 = 提前看")),
            _cmd("plan", w.cmd_plan,
                 "一次分配：按每个项目的阶段取其当前 SOP/incubator op，再排任务",
                 ("排期", "分配"),
                 usage="plan [+1|今天|明天|YYYY-MM-DD] → "
                       "task <序号> <count> | task <序号> clear | complete；"
                       "或 plan -s <session> -p <project> 单场直排",
                 options=("+1", "+2", "today", "明天", "complete", "task 1 4",
                          "-s 1 -p 1"),
                 hint=_hint_actions("+1", "明天", "complete", "task 1 4", "task 1 clear",
                                    "-s 1 -p 1",
                                    note="不带参数 = 今天；plan +1 = 明天；"
                                         "plan -s <session> -p <project> = 把这个项目的"
                                         "内容按该项目 sequential/parallel 规则排进这一场；"
                                         "parallel 用 OmniFocus tag P1/P2 分优先级并按 session "
                                         "从早到晚排；普通 plan 重排会先撤掉当天旧计划；"
                                         "每个项目只出现当前阶段对应的 op；超预算整条命令不执行")),
            _cmd("project", w.cmd_project,
                 "管理真正项目 Project (pro)：pro | pro 1 | pro 1 -m complete|discard | "
                 "pro 1 -a | pro 1 interrupt | pro create",
                 ("pro", "pr", "项目"),
                 usage="pro [<序号> [-a <session>|-ac|--advance [op <N>|task import] | "
                       "-m complete|discard | interrupt [--soft|--hard] [--check|--list] | "
                       "file <路径> | open | refresh | remove] | create <名称>]",
                 options=("--advance", "-a", "-ac", "-m", "--mark", "complete", "discard",
                          "active", "interrupt", "--soft", "-s", "--hard", "--check",
                          "--list", "create", "file", "open", "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -a 3", "1 -ac", "1 -m complete",
                                    "1 -m discard", "1 -m active",
                                    "1 interrupt", "1 interrupt --check",
                                    "1 interrupt --hard", "create",
                                    note="pro = 真正项目（running/bottleneck）；"
                                         "-a <session> / -ac = 直接把该项目第一条任务挂到"
                                         "指定场 / 当前场；interrupt 默认 soft（记录建议，"
                                         "A/B/C 分类）；--check 逐条检查；-m complete|discard "
                                         "只动本地状态，默认 disposable filter 会隐藏")),
            _cmd("op", w.cmd_op,
                 "管理 OmniFocus Project (op)：op | op 1 -m complete|discard | "
                 "op 1 sop|incubator | op 1 relate --export|--import | "
                 "op import [sop|incubator]",
                 ("ofp", "omnifocus-project"),
                 usage="op [<序号> [-m complete|discard | sop|incubator | pro <N> | "
                       "relate --export|--import [路径] | refresh|remove] | "
                       "import [sop|incubator]]",
                 options=("-m", "--mark", "complete", "discard", "active", "sop",
                          "incubator", "import", "relate", "--export", "--import",
                          "refresh", "remove", "check"),
                 hint=_hint_actions("1", "1 -m complete", "1 -m discard", "1 -m active",
                                    "1 sop", "1 incubator", "1 relate --export",
                                    "import sop", "refresh",
                                    note="op = OmniFocus Project（SOP / incubator）；"
                                         "-m complete|discard 只动本地状态")),
        ]

    def _row_budget(self):
        """任务表每页画几行 —— 用掉终端真正剩下的高度。

        以前这里硬钳 ``min(20, terminal_rows - 22)``：120 行的终端也只显示
        20 条，下面明明还有一大片空白，用户要找的任务被迫翻好几页。现在按
        看板预算和当前宽度下固定的 chrome 高度算；表头/图例/归档/Recent
        全部算进去，仍然不会把底部提示挤出屏幕。
        """
        try:
            budget = int(self.w.shell.dashboard_budget())
        except Exception:                             # noqa: BLE001 - 测试/离屏兜底
            try:
                budget = max(4, int(self.w.shell._terminal_rows()) - 3)
            except Exception:                         # noqa: BLE001
                budget = 27
        try:
            width = int(self.w.frame_width())
        except Exception:                             # noqa: BLE001
            width = 120
        # 实测的固定开销：头部 8 行 + 表题/表头/分隔线 + 图例/命令 + 归档框；
        # 窄屏时图例和 caption 会多折几行。这里取略保守值，宁可少画一条，
        # 也不让页面高度超过一屏（超高时框架会从顶部裁，反而丢内容）。
        if width < 90:
            overhead = 27
        elif width < 120:
            overhead = 25
        elif width < 170:
            overhead = 24
        else:
            overhead = 23
        # 至少留 4 条：极小终端也别把表变成只有表头；超出的一点点由
        # WardenShell 的帧内滚动兜住（PgUp/PgDn）。
        return max(4, min(200, budget - overhead))

    def _task_window(self, tasks):
        """返回 ``(当前页任务, 起始下标, 总数, 当前页码, 总页数)``。

        支持 ``open 1 / 2 / 3`` 显式翻页；未指定页码时跟随 ``selected_task_id``
        自动落到它所在的那一页（默认第 1 页）。
        """
        total = len(tasks)
        limit = self._row_budget()
        total_pages = max(1, (total + limit - 1) // limit)
        page = getattr(self.w, "task_view_page", None)
        if page is None:
            selected = self.w.selected_task_id
            page = 1
            if selected is not None:
                for index, task in enumerate(tasks):
                    if task.get("id") == selected:
                        page = (index // limit) + 1
                        break
        page = max(1, min(int(page), total_pages))
        if total <= limit:
            return tasks, 0, total, 1, 1
        start = (page - 1) * limit
        return tasks[start:start + limit], start, total, page, total_pages

    def _guidance_mode(self):
        """说明文字的三档密度：tiny（矮屏）/ compact（窄屏）/ full。

        矮屏优先保证表头留在屏幕上，所以 tiny 会把词压到最短；信息仍然齐全。
        """
        try:
            rows = int(self.w.shell._terminal_rows())
        except Exception:                             # noqa: BLE001
            rows = 30
        try:
            width = self.w.frame_width()
        except Exception:                             # noqa: BLE001
            width = 80
        if rows < 40 or width < 100:
            return "tiny"
        if rows < 50 or width < 120:
            return "compact"
        return "full"

    def _legend_text(self):
        """表下灰色小字：所有标记与备注缩写的含义。"""
        mode = self._guidance_mode()
        if mode == "tiny":
            return Text(
                "标记：🔁重复子 🔁*模板 📁项目 🔗OF 🎯FM ⏳挂载 ⏱fmt ⚡trig 🔒独占 · "
                "状态：·进行 ✓完成 ✗放弃 ⤼转移 · 备注：dur分 cnt进度 excl独占 "
                "tag标签 rep重复 fmt格式 trig触发 note备注",
                style="dim")
        if mode == "compact":
            return Text(
                "标记：🔁=重复子任务 🔁*=模板 📁=项目 🔗=OmniFocus 🎯=Focusmate "
                "⏳=挂载中 ⏱=formatter ⚡=trigger 🔒=独占 · 状态：·=进行中 ✓=完成 "
                "✗=放弃 ⤼=转移走 · 备注：dur=分钟 cnt=进度 excl=独占 tag=标签 "
                "rep=重复 fmt=formatter trig=trigger note=备注",
                style="dim")
        text = Text(style="dim")
        text.append("标记：")
        text.append("🔁=重复子任务 · 🔁*=重复模板 · 📁=项目任务 · 🔗=OmniFocus · "
                    "🎯=Focusmate · ⏳=挂载中 · ⏱=formatter · ⚡=trigger · 🔒=独占\n")
        text.append("状态：·=进行中 · ✓=完成 · ✗=放弃 · ⤼=转移走　")
        text.append("备注缩写：dur=分钟 · cnt=进度 · excl=独占 · tag=标签 · "
                    "rep=重复 · fmt=formatter · trig=trigger · note=备注")
        return text

    def _command_guide_text(self):
        """表下一行灰色命令指引：每条 ≤7 字，参数细节交给 help --task。"""
        entries = (
            ("open task", "任务数据"), ("open project", "项目数据"),
            ("open op", "op 数据"), ("filter", "过滤器"), ("refresh", "同步"),
            ("review", "收尾"), ("inspect-due", "查过期"), ("plan", "排期"),
            ("project", "项目"),
            ("sort", "排序"), ("group", "分组"), ("search", "搜索"),
            ("open", "翻页/展开"), ("close", "折叠"), ("task", "任务操作"),
            ("help --task", "详情"),
        )
        mode = self._guidance_mode()
        text = Text(style="dim")
        text.append("命令：")
        for index, (cmd, desc) in enumerate(entries):
            if index:
                text.append("," if mode == "tiny" else " · ")
            text.append(cmd)
            text.append((" " if mode != "tiny" else "") + desc)
        return text

    def render(self):
        """按当前数据模型分派：task / project / op。"""
        model = self.model
        if model == "project":
            return self._render_project_data()
        if model == "op":
            return self._render_op_data()
        return self._render_task_data()

    def _dispatch_hint(self):
        return Text("open task · open project · open op    ← 切换数据模型（当前：%s）"
                    "    |    %s" % (self.model, self._filter_caption()), style="dim")

    def _render_project_data(self):
        """project 模型：真正项目 Project（``pro``）的本地数据。"""
        w = self.w
        mode = self.data_filter
        # disposable 为保持管理序号连续，默认只看 active；其他筛选需要把
        # completed / dropped 的 pro 也一起读出来再过滤。
        rows = w.projects.pro_preview(
            w.sel_date, w.task_store,
            include_inactive=(mode != "disposable"))
        rows = [row for row in rows
                if self._status_in_filter(row.get("status"))]
        t = Table(title="Project · 真正项目 pro 数据（open task / open project / open op 切换）",
                  box=box.SIMPLE_HEAVY, header_style="bold blue",
                  title_justify="left", expand=True)
        t.add_column("#", justify="right", style="bold", no_wrap=True)
        t.add_column("id", justify="right", style=STYLE_MUTED, no_wrap=True)
        t.add_column("title", no_wrap=True, overflow="crop")
        t.add_column("阶段", no_wrap=True)
        t.add_column("当前 op", overflow="crop")
        t.add_column("SOP op", overflow="crop", style="cyan")
        t.add_column("incubator op", overflow="crop", style="magenta")
        t.add_column("文件", overflow="crop", style=STYLE_MUTED)
        t.add_column("状态", no_wrap=True)
        t.add_column("关联 op", justify="right", no_wrap=True)
        for row in rows:
            current = row.get("active_op") or {}
            sop = row.get("sop_op") or {}
            inc = row.get("incubator_op") or {}
            raw_pro = row.get("pro") or {}

            def op_label(op):
                if not op:
                    return "—"
                num = w.projects.number_of(op.get("id")) or op.get("id")
                return "#%s %s" % (num, op.get("title") or "?")

            t.add_row(
                str(row.get("number") or "—"),
                str(row.get("id") or "—"),
                Text(w.pro_display_title(raw_pro or row, fallback=row.get("title"))
                     or "—", style="bold"),
                Text((row.get("effective_stage") or row.get("stage") or "—")
                     + (" ⚠阶段按实际 op 推导" if row.get("stage_mismatch") else ""),
                     style="magenta"
                     if (row.get("effective_stage") or row.get("stage")) == "bottleneck"
                     else "cyan"),
                Text(op_label(current), style="green"),
                Text(op_label(sop)),
                Text(op_label(inc)),
                Text(str(row.get("file") or "—")),
                Text(str(row.get("status") or raw_pro.get("status") or "—"),
                     style=STYLE_DONE if row.get("status") == ts.STATUS_COMPLETED
                     else STYLE_DROPPED if row.get("status") == ts.STATUS_DROPPED
                     else "green"),
                Text(str(len(row.get("ops") or ()))),
            )
        if not rows:
            row = [Text("") for _ in range(10)]
            row[0] = Text("（当前 filter=%s 下没有真正项目 pro；`filter all` 显示全部，"
                          "或 `project create <名称>` 新建）"
                          % self.data_filter, style="dim")
            t.add_row(*row)
        t.caption = ("%s · %d 条真正项目 Project（pro）· 存储：%s · 每个 pro 的当前 "
                     "op 由 stage 决定（running=SOP / bottleneck=incubator）"
                     % (self._filter_caption(), len(rows), w.projects.path))
        return [t, self._dispatch_hint()]

    def _render_op_data(self):
        """op 模型：OmniFocus Project（``op``）的本地数据，含未关联 pro 的孤立项。"""
        w = self.w
        mode = self.data_filter
        rows = w.projects.preview(
            w.sel_date, w.task_store,
            include_inactive=(mode != "disposable"))
        rows = [row for row in rows
                if self._status_in_filter(row.get("status"))]
        try:
            plan_ids = {op.get("id") for op in w.projects.plan_ops()}
        except Exception:                          # noqa: BLE001 - 展示失败别挡表
            plan_ids = set()
        t = Table(title="Op · OmniFocus Project 数据（SOP / incubator；含未关联 pro 的 op）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", expand=True)
        t.add_column("#", justify="right", style="bold", no_wrap=True)
        t.add_column("id", justify="right", style=STYLE_MUTED, no_wrap=True)
        t.add_column("title", no_wrap=True, overflow="crop")
        t.add_column("类型", no_wrap=True)
        t.add_column("状态", no_wrap=True)
        t.add_column("关联 pro", overflow="crop")
        t.add_column("阶段", no_wrap=True)
        t.add_column("plan", no_wrap=True)
        t.add_column("剩余", justify="right", no_wrap=True)
        t.add_column("进度", justify="right", no_wrap=True)
        t.add_column("未完成", justify="right", no_wrap=True)
        t.add_column("本周", justify="right", no_wrap=True)
        t.add_column("OmniFocus", no_wrap=True, style=STYLE_MUTED)
        for row in rows:
            kind = row.get("op_type") or "未设类型"
            kind_style = ("magenta" if kind == "incubator"
                          else "cyan" if kind == "SOP" else "yellow")
            pro_obj = row.get("pro") or {}
            pro_txt = (w.pro_display_title(pro_obj, fallback=row.get("pro_title"))
                       if pro_obj or row.get("pro_title") else "—")
            if row.get("pro_title") and row.get("pro_stage"):
                pro_txt += " (%s)" % row.get("pro_stage")
            plan_txt = Text("✓", style="bold green") if row.get("id") in plan_ids else Text("—", style=STYLE_MUTED)
            if not row.get("allowed", True):
                plan_txt = Text("⏸", style=STYLE_MUTED)
            t.add_row(
                str(row.get("number") or "—"),
                str(row.get("id") or "—"),
                Text(row.get("title") or "—", style="bold"),
                Text(kind, style=kind_style),
                Text(str(row.get("status") or ts.STATUS_ACTIVE),
                     style=STYLE_DONE if row.get("status") == ts.STATUS_COMPLETED
                     else STYLE_DROPPED if row.get("status") == ts.STATUS_DROPPED
                     else "green"),
                Text(pro_txt, style="cyan" if row.get("pro_title") else STYLE_MUTED),
                Text(row.get("pro_stage") or "—", style=STYLE_MUTED),
                plan_txt,
                Text(str(row.get("remaining") or 0), style="bold cyan"),
                Text(row.get("progress_text") or "—", style=STYLE_MUTED),
                Text(str(row.get("tasks_open") or 0), style=STYLE_MUTED),
                Text(str(row.get("week_count") or 0), style=STYLE_MUTED),
                Text(str(row.get("pk") or "")[:12] or "—"),
            )
        if not rows:
            row = [Text("") for _ in range(13)]
            row[0] = Text("（当前 filter=%s 下没有 OmniFocus Project op；"
                          "`filter all` 显示全部，或 `op import sop|incubator` 导入）"
                          % self.data_filter, style="dim")
            t.add_row(*row)
        t.caption = ("%s · %d 条 OmniFocus Project（op）· plan 只收有 pro 关联且"
                     "阶段匹配的 active op；孤立 op 仍在这里显示"
                     % (self._filter_caption(), len(rows)))
        return [t, self._dispatch_hint()]

    def _render_task_data(self):
        w = self.w
        raw_tasks = w.task_store.list()
        visible_tasks = [task for task in raw_tasks
                         if self._status_in_filter(w.task_store.status_of(task))]
        filtered_tasks = w.filter_and_sort_tasks(visible_tasks)
        tasks, start, total, page, total_pages = self._task_window(filtered_tasks)
        selected = w.selected_task_id
        keys = task_dynamic_keys(raw_tasks)
        headers = task_table_headers(raw_tasks)
        hidden = w.effective_task_hidden_columns(raw_tasks)
        visible_headers = [h for h in headers if h not in hidden]
        # close 已经保证至少留一列；这里只是防御坏偏好文件。
        if not visible_headers:
            visible_headers = [headers[0]]

        group_by = getattr(w, "task_group_by", None)
        group_col_header = None
        if group_by:
            group_col_header = "grouping(%s)" % group_by
            # 分组列前置在最前面，原本同名列就不必重复占地方
            visible_headers = [h for h in visible_headers if h != group_by]

        width = w.frame_width()
        proj_max = 7 if width < 95 else TASK_PROJECT_MAX_LEN
        # title + 备注分掉所有剩下的横向空间。以前 remark 硬钳 48，宽屏
        # 明明右边还有十几列空白，备注却已经以「…」结束（例如重复任务的
        # `fmt=enlist` 整段被吞掉）。硬钳只该防窄屏，不该在宽屏浪费。
        avail = max(18, width - (52 + proj_max))
        title_max = max(8, min(48, avail * 2 // 5))
        remark_max = max(10, avail - title_max)
        group_max = 10 if width < 95 else 16

        t = Table(title="Data · Task · 任务表（open task/project/op 切换模型 · "
                        "open/close 折叠列 · open 1/2/3 翻页 · 序号 = 契约面板上的第几条）",
                  box=box.SIMPLE_HEAVY, header_style="bold magenta",
                  title_justify="left", show_header=True,
                  width=max(20, width - 1), expand=True)
        if group_col_header:
            t.add_column(group_col_header, style="bold cyan", no_wrap=True,
                         overflow="crop", max_width=group_max)
        specs = dict(TASK_TABLE_COLUMNS)
        # 显式给每列 max_width / min_width：Rich 在总宽不够时会悄悄把前几列
        # 压成空白；不钉住「序号/标记/状态」的话，表头看起来就像少了好几列。
        max_widths = {
            "序号": 7, "标记": 8, "title": title_max, "project": proj_max,
            "状态": 4, "session": 11, "截止": 11, "备注": remark_max,
            "id": 6, "重复": 18, "trigger": 12, "formatter": 14,
            "Focusmate": 10, "OmniFocus": 12, "完成时间": 11, "标签": 14,
            "duration": 7, "count": 10, "exclusive": 7,
        }
        # Rich 的 width/max_width/min_width 都算 padding。给关键列钉一个
        # 恰好能装下内容的 min_width，表格又会按终端宽度展开，于是宽屏上
        # 多出来的空间会真的分给备注，而不是在右边留白、同时把备注裁掉。
        min_widths = {
            "序号": 8, "标记": 10, "title": min(20, max(8, title_max)),
            "project": min(14, max(8, proj_max + 2)),
            "状态": 6, "session": 13, "截止": 13,
            "备注": min(remark_max, 30 if width < 100 else 60),
        }
        if width < 100:
            min_widths.update({"序号": 6, "标记": 8, "session": 10,
                               "截止": 10, "备注": 12})
        for header in visible_headers:
            if header in specs:
                kwargs = dict(specs[header])
                if header in max_widths:
                    kwargs["max_width"] = max_widths[header]
                if header in min_widths:
                    kwargs["min_width"] = min_widths[header]
                t.add_column(header, **kwargs)
            else:
                t.add_column(str(header), no_wrap=True, overflow="crop",
                             max_width=remark_max)

        status_style = {ts.STATUS_COMPLETED: STYLE_DONE, ts.STATUS_DROPPED: STYLE_DROPPED}
        total_cols = (1 if group_col_header else 0) + len(visible_headers)
        if not tasks:
            row = [Text("") for _ in range(total_cols)]
            msg = ("（没有匹配「%s」的任务；`search off` 清除搜索）"
                   % w.task_search_query
                   if getattr(w, "task_search_query", "")
                   else "（当前 filter=%s 下没有任务；`filter all` 显示全部，"
                        "task create title=写论文 开始）" % self.data_filter)
            row[0] = Text(msg, style="dim")
            t.add_row(*row)
        prev_group_val = object()
        for task in tasks:
            status = w.task_store.status_of(task)
            style = "bold reverse" if task.get("id") == selected else ""
            if status != ts.STATUS_ACTIVE:
                style = status_style.get(status, "") + (
                    " reverse" if task.get("id") == selected else "")
            sess = task.get("session") or {}
            if sess:
                sess_raw = "%s %s" % (sess.get("date") or "", sess.get("start") or "")
                sess_txt = format_compact_timestamp(sess_raw.strip(), w.sel_date)
            else:
                sess_txt = "—"
            due = w.omni_due_of(task)
            due_txt = format_compact_timestamp(due, w.sel_date) if due else "—"
            fm = task.get("focusmate") or {}
            fm_txt = str(fm.get("task_id") or "")[:8] or "—"
            omni_id = ts.TaskStore.omnifocus_id(task)
            omni_txt = str(omni_id)[:12] if omni_id else "—"
            done_raw = fm.get("completed_at") or task.get("status_at") or ""
            done = format_compact_timestamp(done_raw, w.sel_date) if done_raw else "—"
            rep = task.get("repeat") or {}
            rep_txt = ("🔁 " + ts.repeat_summary(rep)) if rep else "—"
            trig = task.get("trigger") or {}
            fmt = task.get("formatter") or {}
            trig_txt = str(trig.get("name") or "—")
            fmt_txt = str(fmt.get("name") or "—")
            if fmt.get("reservation"):
                fmt_txt += "@" + str(fmt["reservation"].get("start") or "?")
            elif fmt and ts.TaskStore.is_repeat_template(task):
                fmt_txt += "（模板）"
            mark_sym = w.task_mark(task) or "·"
            tags = task.get("tags") or []
            panel_no = w.task_no(task)
            if panel_no:
                num_txt = str(panel_no)
            elif (ts.TaskStore.is_repeat_template(task)
                  or ts.TaskStore.is_project_backlog(task)):
                # 不在契约面板上；任务视图里直接把可敲的写法摆出来。
                num_txt = "id:%s" % task["id"]
            else:
                num_txt = "—"
            fields = task.get("fields") or {}
            flags_txt = w.task_flags_text(task)
            title_raw = w.task_store._one_line(
                fields.get("title") or fields.get("name") or "")
            title_txt = _crop_display(title_raw, title_max)
            proj_raw = w.task_project_name(task)
            proj_txt = _crop_display(proj_raw, proj_max) if proj_raw else "—"
            remark_raw = w.task_remark_text(task)
            remark_txt = _crop_display(remark_raw, remark_max) if remark_raw else "—"
            dur_val = ts.task_minutes(task)
            cap_val, _invested_val = w.task_count_scope(task)
            cnt_val = w.task_count_label(task, w.task_count_delta(task)).strip()
            if not cnt_val and cap_val is not None:
                cnt_val = str(cap_val)
            excl_val = "true" if th.is_exclusive(task) else "—"
            cells = {
                "序号": Text(num_txt, style=style),
                "标记": Text(flags_txt, style=style if flags_txt != "·" else STYLE_MUTED),
                "title": Text(title_txt, style=style),
                "project": Text(proj_txt, style="cyan" if proj_raw else STYLE_MUTED),
                "状态": Text(mark_sym, style=status_style.get(status, "") or style),
                "session": Text(sess_txt, style=style if sess else STYLE_MUTED),
                "截止": Text(due_txt, style=STYLE_MUTED if due else style),
                "备注": Text(remark_txt, style=style if remark_raw else STYLE_MUTED),
                "id": Text(str(task["id"]), style=style),
                "重复": Text(rep_txt, style="magenta" if rep else STYLE_MUTED),
                "trigger": Text(trig_txt, style="cyan" if trig else STYLE_MUTED),
                "formatter": Text(fmt_txt, style="green" if fmt else STYLE_MUTED),
                "Focusmate": Text(fm_txt, style=style),
                "OmniFocus": Text(omni_txt, style="cyan" if omni_id else STYLE_MUTED),
                "完成时间": Text(done, style=style),
                "标签": Text(" · ".join(tags) if tags else "—",
                            style="yellow" if tags else STYLE_MUTED),
                "duration": Text(str(dur_val) if dur_val is not None else "—",
                                 style=style if dur_val is not None else STYLE_MUTED),
                "count": Text(cnt_val or "—", style=style if cnt_val else STYLE_MUTED),
                "exclusive": Text(excl_val,
                                  style="yellow" if excl_val == "true" else STYLE_MUTED),
                TASK_CONTENT_COLUMN: Text(
                    _crop_display(w.task_store.summarize(task), title_max), style=style),
            }
            for key in keys:
                if key not in cells:
                    cells[key] = Text(
                        _crop_display(w.task_store._one_line(fields.get(key)), remark_max),
                        style=style)
            row_cells = [cells.get(header, Text("")) for header in visible_headers]
            if group_col_header:
                gval = w.task_group_label(task, group_by)
                show_gval = _crop_display(gval, group_max) if gval != prev_group_val else ""
                prev_group_val = gval
                row_cells = [Text(show_gval, style="bold cyan")] + row_cells
            t.add_row(*row_cells)

        caption_bits = []
        page_examples = " / ".join(str(i) for i in range(1, min(total_pages, 3) + 1))
        if total_pages > 3:
            page_examples += " / …"
        caption_bits.append(
            "第 %d/%d 页，共 %d 条（open %s 直接翻页）"
            % (page, total_pages, total, page_examples or "1"))
        caption_bits.append(self._filter_caption())
        active_filters = w.task_filter_summary()
        if active_filters:
            caption_bits.append(active_filters)
        if hidden:
            # 被折叠的表头名字放在表下面，方便随时知道还有什么列 / 怎么打开。
            caption_bits.append("已折叠列：%s（open <表头名> 打开）" % "、".join(
                header for header in headers if header in hidden))
        if caption_bits:
            t.caption = " · ".join(caption_bits)

        # 收尾面板在 repl_kit 的打断栏位里（见 render_interrogate），不在这儿。
        out = [t, self._legend_text(), self._command_guide_text()]
        try:
            terminal_rows = int(w.shell._terminal_rows())
        except Exception:                             # noqa: BLE001
            terminal_rows = 30
        arch = w.task_store.load_archive().get("records") or []
        archive_limit = 2 if terminal_rows < 50 else 5
        at = Table.grid(padding=(0, 1))
        at.add_column(style="dim", no_wrap=True)
        at.add_column(no_wrap=True)
        at.add_column(no_wrap=True, overflow="crop")
        at.add_column(no_wrap=True, overflow="crop")
        if not arch:
            at.add_row("", Text("（还没有完成归档；task sync 会读 GET /v1/tasks 的 "
                                "completedAt）", style="dim"))
        for rec in arch[-archive_limit:]:
            fm = rec.get("focusmate") or {}
            sess = rec.get("session") or {}
            note = rec.get("notes") or w.task_store.notes({"fields": rec.get("fields")})
            at.add_row(
                "· " + str(rec.get("task_id") or "?"),
                "%s ← %s %s" % (str(fm.get("completed_at") or "?"),
                                sess.get("date") or "?", sess.get("start") or "?"),
                (rec.get("fields") or {}).get("title") or
                w.task_store._one_line(rec.get("fields"))[:60],
                note_renderable(note))
        out.append(Panel(at, title="[bold]Task Archive · 完成归档[/]",
                         border_style="dim", box=box.ROUNDED))
        out.append(Text("open task · open project · open op    ← 切换数据模型（当前：task）"
                        "    |    " + self._filter_caption(), style="dim"))
        return out


# 兼容旧代码/测试：类对象仍是原来那个，只是界面名字改成了「数据」。
TaskView = DataView


@register_view
class SettingsView(View):
    """设置视图：把所有设置项（哪怕从没改过）连同解释列出来，``set`` 改。

    需求：展示**所有**设置和他们的介绍，用 ``set {设置的 id} {value}`` 改内容。
    设置本身是数据（``settings_store.SETTINGS``），这里不硬编码任何一条。
    """

    key = "5"
    name = "设置"
    label = "设置 / 规则"

    def idle_hint(self):
        return repl_kit.Hint("命令", ("set", "settings", "help", "quit"),
                             note="Tab 切视图 · 不写 value = 恢复默认")

    def unknown_hint(self):
        return "「设置」视图只认 set / settings（Shift+Tab 切回去干别的）。"

    def commands(self):
        w = self.w
        return [
            _cmd("settings", lambda a: w.cmd_settings(show=True),
                 "重新列出全部设置（也是只重画看板）", ("list",),
                 usage="settings", hint=_hint_actions("settings", note="只重画看板")),
        ]

    def render(self):
        w = self.w
        rows = w.settings.rows()
        t = Table(title="Settings · 设置（改过的标 *；set <id> <value> 修改）",
                  box=box.SIMPLE_HEAVY, header_style="bold cyan",
                  title_justify="left", expand=False)
        t.add_column("设置 ID", no_wrap=True, style="bold")
        t.add_column("当前值", no_wrap=True, style="green")
        t.add_column("默认", no_wrap=True, style="dim")
        t.add_column("说明", no_wrap=False, overflow="fold")
        for row in rows:
            ident = Text(("* " if row["changed"] else "  ") + row["id"],
                         style="bold yellow" if row["changed"] else "bold")
            t.add_row(ident, Text(row["value_display"], style="green"),
                      Text(str(row["default"])), Text(row["desc"], style="dim"))
        hint = Text("set 的取值：", style="dim")
        hint.append("  ".join("set %s %s" % (r["id"],
                                             "|".join(r["values"]) if r["values"] else "…")
                              for r in rows), style="cyan")
        return [t, hint]


@register_view
class ChoreView(View):
    """空闲屏：没有 session 时只显示绑定项目里的一条琐事。

    这是**自动**视图：warden 每帧发现当前不在 session、且 ``chore-project``
    已绑定，就切进来。屏幕上不画正常看板、状态框、消息和视图栏，只有一条任务
    和 m/c/q 三行提示。``q`` 回到进入前的正常视图，并压制到下一场 session 开始。
    """

    key = "6"
    name = "琐事"
    label = "琐事 / 空闲"

    def idle_hint(self):
        return repl_kit.Hint("琐事", ("m 完成", "c 换一个 task", "q 退出"),
                             note="q = 回到正常界面；这段空档里不再自动弹回来")

    def unknown_hint(self):
        return "琐事空闲屏只认 m / c / q。"

    def commands(self):
        w = self.w
        # m / c / q 由 Warden.intercept 在命令派发之前先吃掉；q 不能落到
        # 通用 quit（那会退出整个 TUI，而这里只退出空闲屏）。
        # refresh 必须保留：app 从 Dock 唤起时会往 stdin 打一行 refresh，
        # 如果琐事屏不认它，恰好在你空闲时唤起就会只得到一条「不认命令」。
        return [
            _cmd("refresh", w.cmd_refresh,
                 "全量刷新看板（琐事屏里也能用；不会退出当前屏）", ("r", "reload"),
                 usage="refresh [--check]", options=("--check", "-c",),
                 hint=_hint_refresh),
        ]

    def render(self):
        return self.w.render_chore_idle()


