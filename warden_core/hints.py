"""warden_core.hints —— 命令提示行（repl_kit.Hint）、Tab 补全器与 OmniFocus 重复规则解析。"""
import datetime
import time

from .common import (
    TASK_HOOK_FLAGS,
    TASK_HOOK_KINDS,
    TASK_MARK_WORDS,
    TASK_SHORT_FLAGS,
    _crop_display,
    repl_kit,
    sp,
    th,
    ts,
)

# ---------------------------------------------------------------- 视图（Tab 切换）
#
# 可扩展：一个 View = 一块主体渲染 + 一套命令 + 一行提示。
# 加新视图：继承 View、填 key/name/label、实现 idle_hint()/render()/commands()，
# 然后用 @register_view 注册即可 —— paint() / 切换 / 命令提示都自动跟上。
# 未来想加「统计」「历史」视图，照抄 FocusView 的结构就行。

def _cmd(name, handler, summary, aliases=(), section="命令", usage="",
         options=(), hint=None, completer=None, values=None):
    """一条 repl_kit 命令。

    命令的「叫什么 / 干什么 / 怎么用 / 补全什么 / 提示什么」都放在这一条记录里，
    由 repl_kit 统一排版成 help 表和提示行 —— 和 paper_tui 用的是同一套。
    """
    kwargs = {}
    if completer is not None:
        kwargs["completer"] = completer
    if values:
        kwargs["values"] = dict(values)
    return repl_kit.Command(
        name=name, handler=handler, summary=summary, aliases=tuple(aliases),
        section=section, usage=usage, options=tuple(options), hint=hint, **kwargs
    )


# -- 提示行（repl_kit.Hint）-------------------------------------------------
#
# paper_tui 的提示行长这样：
#
#     命令: session  cancel  mute   (Tab 切专注视图)
#           ^ items 之间两个空格   ^ note 三个空格 + 括号
#
# 这里不手写字符串，统一交给 repl_kit.Hint 排版；命令自己的参数提示用
# Command.hint（框架会在输入了命令名之后自动换上去）。

def _hint(prefix, *items, note=""):
    """静态提示：`<prefix>: item  item   (note)`。"""
    def hook(_parts):
        return repl_kit.Hint(prefix, items, note=note)
    return hook


def _hint_actions(*items, note=""):
    return _hint("动作", *items, note=note)


# ---------------------------------------------------------------- 命令介绍窗
#
# repl_kit 的 hint 行只回答「这个位置能填什么」。这里再给 3 行「这个命令是
# 干什么的」：
#
#     📖 task — 任务管理：新建 / 改字段 / 挂 session / 导入 OmniFocus
#     ⌨ 用法  task <序号|create|...> [-a <场>] [-m ...]
#     → 现在  任务 #3 -ac：挂到当前进行中或最近的 session
#
# 介绍行和 repl_kit 的提示行分开，是因为「参数取值」会随着每个词变，而
# 「命令用途」应该稳定地留在屏幕上方；两者混成一行时，命令一长就什么都看不清。

_CONTEXT_HEADS = {
    "task": "任务管理：建任务 / 挂 session / 改状态 / 导入 OmniFocus",
    "project": "项目管理：真正项目 pro / 阶段 / 当前 op / 项目任务",
    "plan": "排期：资源分配 → 唤起任务 → 排进 session",
    "conscription": "快速征召：按剩余时间挑一条合适任务",
    "session": "session 管理：新增 / 改排 / 永久写进日历",
    "period": "时段排布：按 T1/T2 重排当天 session",
    "help": "帮助：当前视图全部命令与用法",
    "open": "打开：切数据模型 / 看板口径 / 展开列",
    "close": "折叠：收起当前表的一列",
}

_CONTEXT_USAGES = {
    "task": "task <序号|create|import|...> [-a <场>|-ac] [-act N] [-m complete|discard]",
    "project": "pro <序号> [interrupt [--soft|--hard|--check]|-a <场>|-ac|...]",
    "plan": "plan [-c|--current] [天数]  |  plan -s <场> -p <项目>",
    "conscription": "conscription / cst [session序号|current]",
    "session": "session <HH:MM> <25|50|75> [--permanent]",
    "period": "period <T1> [25|50|75 ...] | period T1 clear",
}

_VIEW_INTROS = {
    "契约": ("契约 / 预约",
             "session 预约、取消、静音、改标题；项目与任务在下方"),
    "专注": ("专注",
             "当前 session 倒计时、暂停次数、专注模式；只认 stop 和 Shift+Tab"),
    "统计": ("统计",
             "生涯累计、每日分布、永不重订名单；stat refresh 增量刷新"),
    "数据": ("数据",
             "open task / project / op 切换三种数据模型；filter 控制可见状态"),
    "设置": ("设置",
             "列出全部设置项；set <id> <value> 修改，不写 value 恢复默认"),
    "琐事": ("琐事",
             "空闲时处理一条 bind 项目的杂事；m 完成 / c 换一条 / q 退出"),
}


def _context_width(shell):
    try:
        return max(40, int(shell.console.width) - 1)
    except Exception:                              # noqa: BLE001 - 离屏/测试兜底
        return 100


def _context_detail(command, args):
    """给当前输入片段补一句「这一步具体在做什么」。

    只覆盖最常用、也最容易记错的形状；其余命令回落到 usage/summary，
    不在这里复述一整套帮助表。
    """
    name = str(getattr(command, "name", "") or "")
    words = [str(x).lower() for x in (args or []) if str(x).strip()]

    if name == "task":
        if not words:
            return "输入序号后看这条任务能做什么；或从 create / import 等动作开始"
        # 后缀 flag 可能出现在序号前后，先按 flag 判断，再按动作词判断。
        ref = next((w for w in words if w.isdigit() or w.startswith("id:")), "")
        task_ref = (("任务 #%s" % ref) if ref.isdigit() else ref) if ref else "这条任务"
        if any(tok in words for tok in ("-act", "--assign-count", "--assigned-count", "--act")):
            pos = next((i for i, tok in enumerate(words)
                        if tok in ("-act", "--assign-count", "--assigned-count", "--act")), -1)
            value = words[pos + 1] if 0 <= pos < len(words) - 1 else "N"
            return "%s -act %s：把这一场 session 要记的 count 设为 %s（需先挂场）" \
                % (task_ref, value, value)
        if any(tok in words for tok in ("-ac", "--assign-current")) or "current" in words:
            return "%s -ac：挂到当前进行中或最近的 session" % task_ref
        if any(tok in words for tok in ("-an",)) or "none" in words:
            return "%s -an：从 session 下架" % task_ref
        if any(tok in words for tok in ("-a", "--assign")):
            return "%s -a：挂到指定的表格 #session" % task_ref
        if "-m" in words or "--mark" in words:
            mark = words[words.index("-m") + 1] if "-m" in words and words.index("-m") + 1 < len(words) else ""
            if mark == "complete":
                return "%s -m complete：只改本地状态并归档，不勾 Focusmate" % task_ref
            if mark == "discard":
                return "%s -m discard：只改本地状态，外部系统都不动" % task_ref
            return "%s：手动改本地任务状态（不碰外部）" % task_ref
        if "-f" in words or "--formatter" in words:
            fmt = words[words.index("-f") + 1] if "-f" in words and words.index("-f") + 1 < len(words) else ""
            if fmt == "end":
                return "%s -f end：按任务时长在当天最后切一场 session 并自动挂上" % task_ref
            if fmt == "repeat":
                return "%s -f repeat：固定挂到点名的那一场已有 session" % task_ref
            return "%s：挂 formatter 并按它排期" % task_ref
        if "-e" in words or "--exclusive" in words:
            return "%s -e：让这条任务独占整场 session" % task_ref
        if ref:
            return "%s：查看 / 选中这条任务" % task_ref

        action = words[0]
        details = {
            "create": "新建任务；字段不校验，--repeat 可一步建每周重复",
            "import": "读剪贴板导入 OmniFocus 任务或项目",
            "assign": "把选中的任务挂到指定 session",
            "select": "选中一条任务（裸序号也等价）",
            "delete": "删除一条任务",
            "modify": "修改任务字段（字段不校验）",
            "note": "写任务备注，适合塞材料链接",
            "repeat": "查看 / 设置每周重复规则",
            "sync": "把 Focusmate 完成时间同步进本地归档",
            "archive": "查看完成归档",
            "link": "关联 OmniFocus 任务",
            "swap": "两条任务互换面板顺序",
            "done": "完成任务，并联动 Focusmate / OmniFocus",
            "drop": "放弃任务，并联动 Focusmate / OmniFocus",
            "duplicate": "复制一条任务",
        }
        # 动作可能写在 `task -a 3 create ...` 这种后缀后面，找第一个动作词。
        for tok in words:
            if tok in details:
                return details[tok]
        return "任务管理：动作 + 后缀（挂载 / 备注 / 状态 / trigger / formatter）"

    if name in ("project", "pro"):
        if not words:
            return "真正项目 pro：阶段、当前 op、阶段推进与项目任务"
        if "-ac" in words or "--assign-current" in words:
            return "把该项目第一条可排任务挂到当前或最近的 session"
        if "-a" in words or "--assign" in words:
            return "把该项目第一条可排任务挂到指定 session"
        if "interrupt" in words:
            if "--check" in words or "-c" in words:
                return "逐条检查软中断：不处理 / 入队 / 丢弃，最后合成一条 incubator 任务"
            if "--hard" in words:
                return "硬中断：下架当前场全部任务，再建一整场新任务挂回去"
            return "软中断：保存建议并选 A/B/C，不下架当前任务"
        if "create" in words:
            return "新建真正项目 pro"
        if "file" in words:
            return "给项目关联一个文件，open + Enter 时顺便打开"
        if "refresh" in words:
            return "重新从 OmniFocus 读取该项目清单"
        return "管理真正项目 pro 的阶段、关联 op 与项目任务"

    if name == "plan":
        if "-s" in words or "--session" in words or "-p" in words or "--project" in words:
            return "sessional plan：把指定项目排进指定 session（追加，不整天重排）"
        if "-c" in words or "--current" in words:
            return "把今天正在进行的空 session 也纳入排期，只算完整的 25 分钟块"
        return "分资源 → 唤起任务 → 排进 session；默认排今天"

    if name in ("conscription", "cst"):
        return "按目标 session 剩余时间每 5 分钟向下取档，挑一条合适任务征召"

    if name == "session":
        return "新增 / 改排 session；--permanent 才写进日历长期生效"
    if name == "period":
        return "按时段 T1/T2 重排当天所有 session"
    if name == "help":
        return "当前视图全部命令；help --task / --data 看数据视图详细说明"
    if name == "open":
        return "切换数据模型 / 看板口径，或展开某一列"
    if name == "close":
        return "折叠当前表的一列；--permanent 写进偏好"
    return ""


def context_hint_lines(shell, text=""):
    """Prompt 上方的 3 行命令介绍窗。

    返回 ``[(prompt_toolkit style, line), ...]``；始终返回 3 行，保证
    ``WardenShell.dashboard_budget()`` 预留的高度和实际画出来的一致。
    """
    raw = str(text or "")
    parts = raw.strip().split()
    width = _context_width(shell)
    if not parts:
        view = getattr(shell, "view", None)
        name = getattr(view, "name", "") if view is not None else ""
        title, desc = _VIEW_INTROS.get(name, (name or "warden", "输入命令开始"))
        return [
            ("fg:ansicyan bold", _crop_display("📖  %s — %s" % (title, desc), width)),
            ("fg:ansibrightblack",
             _crop_display("⌨  输入命令后这里显示用途和用法；Tab 补全 · help 看全部命令", width)),
            ("fg:ansibrightblack",
             _crop_display("↕  PgUp / PgDn 翻长内容 · Shift+Tab 切下一个视图", width)),
        ]

    token = parts[0]
    command = shell.registry.get(token)
    matches = []
    partial = False
    if command is None:
        matches = [candidate for candidate in shell.registry.completion_names(shell.verbose)
                   if candidate.lower().startswith(token.lower())]
        if matches:
            partial = True
            command = shell.registry.get(matches[0])
    if command is None:
        return [
            ("fg:ansiyellow bold", _crop_display("❔  没认出命令「%s」" % token, width)),
            ("fg:ansibrightblack",
             _crop_display("⌨  Tab 补全 · help 看当前视图全部命令", width)),
            ("fg:ansibrightblack",
             _crop_display("→  输入已有命令的前几个字母时，这里会显示它的用途", width)),
        ]

    if partial and len(matches) > 1:
        shown = "  ".join(matches[:6])
        more = " …" if len(matches) > 6 else ""
        return [
            ("fg:ansicyan bold", _crop_display("🔎  「%s」匹配 %d 条命令" % (token, len(matches)), width)),
            ("fg:ansibrightblack", _crop_display("⌨  " + shown + more, width)),
            ("fg:ansibrightblack",
             _crop_display("→  再输入一两个字母缩小范围；Tab 直接补全", width)),
        ]

    intro = _CONTEXT_HEADS.get(command.name) or getattr(command, "summary", "") or command.name
    if partial:
        head = "📖  %s → %s ─ %s" % (token, command.name, intro)
    else:
        head = "📖  %s ─ %s" % (command.name, intro)
    usage = (_CONTEXT_USAGES.get(command.name)
             or getattr(command, "usage", "") or command.name)
    detail = _context_detail(command, parts[1:])
    if not detail:
        detail = "Tab 补全参数 · Enter 执行 · help 看当前视图全部命令"
    return [
        ("fg:ansicyan bold", _crop_display(head, width)),
        ("fg:ansibrightblack", _crop_display("⌨  用法  " + usage, width)),
        ("fg:ansiyellow", _crop_display("→  现在  " + detail, width)),
    ]


def _hint_day(parts):
    return repl_kit.Hint("取值", ("-1", "0", "1", "YYYY-MM-DD"))


def _hint_session(parts):
    if "--permanent" not in " ".join(parts) and len(parts) <= 1:
        return repl_kit.Hint("参数", ("HH:MM", "25|50|75"),
                             note="--permanent 才写进日历；不写 = 只加这一天")
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("25", "50", "75"))
    if len(parts) >= 2:
        return repl_kit.Hint("取值", ("25", "50", "75"),
                             note="也可以写 11:15-12:05")
    return repl_kit.Hint("选项", ("--permanent",), note="写进日历（按星期几重复）")


def _hint_relocate(parts):
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("-30", "-15", "+15", "+30"))
    if len(parts) == 2:
        return repl_kit.Hint("参数", ("±MM",),
                             note="15 的倍数；不能跨天；撞车就拒绝")
    return repl_kit.Hint("参数", ("<n>", "T1"),
                         note="<n> = 单场；T1 = 整段（里面每场一起挪）")


def _hint_mute(parts):
    if len(parts) >= 3:
        return repl_kit.Hint("取值", ("on", "off"))
    return repl_kit.Hint("参数", ("<n>", "on|off"),
                         note="不写 on/off 就是取反")


def _hint_mute_universal(parts):
    if len(parts) >= 2 and str(parts[1]).lower() in ("off", "unmute", "清空", "取消"):
        return repl_kit.Hint("参数", ("13:00", "-", "15:00", "Mon", "清空"),
                             note="按星期几 + 时间窗撤销；只写 off = 清空全部通用静音")
    if parts:
        return repl_kit.Hint("时间窗", ("13:00", "-", "15:00", "Mon", "Tue", "Wed"),
                             note="每个指定星期几，凡与这个窗口有重叠的 session 都静音")
    return repl_kit.Hint("参数", ("13:00 - 15:00 Mon", "off", "list"),
                         note="例：mute-universal 13:00 - 15:00 Mon；"
                              "14:30–15:30 这种只碰到尾巴的 session 也算")


def _hint_title(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("参数", ('"文本"',), note='"" = 清空')
    return repl_kit.Hint("参数", ("<n>", '"文本"'))


def _hint_cancel(parts):
    if "--permanent" not in " ".join(parts):
        return repl_kit.Hint("参数", ("<n>", "--permanent"),
                             note="默认只弃这一场；--permanent 才从日历删掉")
    return repl_kit.Hint("参数", ("<n>",), note="永久删掉日历里这个 session")


class _TaskCompletionValues(dict):
    """``task`` 的 flag→候选 表；trigger/formatter 的候选**动态**从注册表读。

    必须是 Mapping 而不是普通 dict：``-t`` 在 trigger 注册表为空时要明确返回
    「没有候选」，而不是让 shell 落到「把全部 flag 当候选」那条兜底分支 ——
    否则 `task -t <Tab>` 会莫名其妙列出一堆选项。以后注册了 trigger，Tab 立刻
    就能看到，不需要重启 Warden 或重建 Command。
    """

    def __contains__(self, key):
        if key in TASK_HOOK_FLAGS["trigger"] or key in TASK_HOOK_FLAGS["formatter"]:
            return True
        return super().__contains__(key)

    def __getitem__(self, key):
        if key in TASK_HOOK_FLAGS["trigger"]:
            return th.preset_names("trigger")
        if key in TASK_HOOK_FLAGS["formatter"]:
            return th.preset_names("formatter")
        return super().__getitem__(key)


def _task_command(w):
    """``task`` 那一条命令：动作、后缀、紧凑短选项、补全都写在一个地方。

    紧凑短选项走 repl_kit 的 ``short`` 表（``-an`` ≡ ``-a none``）：解析、补全、
    提示三件事都由框架统一办，这里只声明一次（:data:`TASK_SHORT_FLAGS`）。
    """
    return repl_kit.Command(
        name="task",
        handler=w.cmd_task,
        summary="任务管理：CRUD / 挂到 session / 手动标记状态 / trigger + formatter / "
                "同步完成时间 / 导入 OmniFocus 项目",
        aliases=(),
        section="通用",
        usage="task <create|import|delete|modify|note|assign|select|swap|repeat|sync|archive>"
              " ... [-a <编号|none|current>] [-s <编号>] [-n 备注] [-e true|false]"
              " [-m complete|discard] [-c 5|+2|-1] [-act N]"
              " [-t <trigger> [参数...]] [-f <formatter> [参数...]]",
        # 把 flag 放在 action 词前面：补全列表有 20 项上限，flag 被挤到后面
        # 就等于「有这个选项但 Tab 永远看不到」。
        # 紧凑短选项（-an/-ac/-m/-t/-f）由 task_completer 的 short 表补，不要
        # 在 options 里再写一遍 —— 否则 Tab 菜单里会出现两个一模一样的候选。
        options=("--assign", "-a", "--note", "-n", "--from", "-s", "--exclusive", "-e",
                 "--mark", "--count", "--assign-count", "--trigger",
                 "--formatter", "--repeat", "--single-task",
                 "create", "import", "delete", "modify", "note", "assign", "select",
                 "swap", "repeat", "sync", "archive", "inspect-due"),
        values=_TaskCompletionValues({"--mark": TASK_MARK_WORDS,
                                      "-m": TASK_MARK_WORDS}),
        short=TASK_SHORT_FLAGS,
        completer=lambda prior: task_completer(w, prior),
        hint=_hint_task,
    )


def task_completer(w, prior):
    """``task`` 的补全：``-a`` 后面给「编号 + none + current」，动作位置给动作词。

    ``-a`` / ``-s`` 后面补的是**表格编号**（这才是它们要的东西），另加两个特殊
    取值 —— 不给它们，``-an`` / ``-ac`` 就等于只存在于文档里。
    """
    words = [str(part).lower() for part in prior]
    if len(words) <= 1:
        # `task ` 后面最常敲的就是「第几条」—— 直接补的是面板序号（1..N），
        # 不是内部 id（那两套数不是一回事，补错了会让人敲到别的任务上）。
        try:
            return [str(i) for i in range(1, len(w.task_panel_order()) + 1)]
        except Exception:                            # noqa: BLE001 - 补不出来就算了
            return []
    if words and words[-1] in ("--assign", "-a", "-an", "-ac"):
        return [str(i) for i in range(1, len(w.rows) + 1)] + ["none", "current"]
    if words and words[-1] in ("--from", "-s"):
        return [str(i) for i in range(1, len(w.rows) + 1)]
    if words and words[-1] in ("--count", "-c"):
        # count 后面就是一个整数；把最常用的三个形状摆出来（+1 / +2 / -1）。
        return ["+1", "+2", "-1"]
    if words and words[-1] in ("--assign-count", "--assigned-count", "-act", "--act"):
        # 这一场记几个 count：给几个常用正整数，具体值仍可直接输入。
        return ["1", "2", "3"]
    if words and words[-1] in TASK_HOOK_FLAGS["trigger"]:
        # 默认没有 trigger —— 返回空列表就是「空的 function 列表」。
        return list(th.preset_names("trigger"))
    if words and words[-1] in TASK_HOOK_FLAGS["formatter"]:
        return list(th.preset_names("formatter"))
    # `task 3 -f repeat `：把当天可选的 session 起点摆出来；#N 放后面。
    if any(tok in words for tok in TASK_HOOK_FLAGS["formatter"]) and "repeat" in words:
        try:
            starts = [str(r.get("startLabel")) for r in w.rows
                      if r.get("startLabel")]
        except Exception:                            # noqa: BLE001
            starts = []
        if starts:
            return starts + ["#%d" % (i + 1) for i in range(len(starts))]
    return []


def mute_window_for(day, hhmm, end_hhmm=None):
    """这一场的起点/区间落在哪条静音窗里 → 记录；没有就是 None。

    口径只认 :mod:`session_plan` 里那张表，别处不要自己算 —— 表格的「静音」
    列和自动 quiet 都问这一个函数。

    - ``period mute``：旧语义，session **起点**在窗内就算；
    - ``mute-universal``：只要 session 与窗口有重叠就算（14:30–15:30 对
      13:00–15:00 也算）。后者必须给 ``end_hhmm``；没有终点时退化为起点判定。
    （daemon 的分心看门狗**不**问它：静音只改 Focusmate 那一场，本地该响还响。）
    """
    got = sp.parse_hhmm(hhmm)
    if got is None:
        return None
    try:
        moment = datetime.datetime.combine(day, datetime.time(got[0], got[1]))
        window = sp.mute_window_at(moment, data=mute_data())
        if window is not None:
            return window
        if end_hhmm:
            return sp.mute_overlaps_window(day, hhmm, end_hhmm,
                                           data=mute_data(), universal_only=True)
        return None
    except Exception:                       # noqa: BLE001 - 静音表坏了不该没表格
        return None


_MUTE_CACHE = {"at": 0.0, "data": None}


def mute_data(force=False):
    """静音表（2 秒缓存）。表格每一行都要问它，别每行读一次盘。"""
    now = time.time()
    if not force and _MUTE_CACHE["data"] is not None and now - _MUTE_CACHE["at"] < 2.0:
        return _MUTE_CACHE["data"]
    _MUTE_CACHE["data"] = sp.load_mutes()
    _MUTE_CACHE["at"] = now
    return _MUTE_CACHE["data"]


def forget_mute_cache():
    """改过静音表之后清一下（``period mute`` 立刻生效，不用等 2 秒）。"""
    _MUTE_CACHE["data"] = None
    _MUTE_CACHE["at"] = 0.0


def _hint_sync(parts):
    return repl_kit.Hint("选项", ("--days",), note="日历 → data.csv")


def _hint_stat(parts):
    if len(parts) >= 2 and parts[1] == "refresh":
        return repl_kit.Hint("动作", ("full",), note="不加 full = 增量刷新")
    return repl_kit.Hint("动作", ("refresh", "refresh full"),
                         note="默认走 1 小时缓存")


def _hint_period(parts):
    if parts and str(parts[0]).lower() in ("mute", "unmute", "静音", "解除静音", "取消静音"):
        return repl_kit.Hint("参数", ("T1", "--permanent", "--off"),
                             note="静音这个时段（--permanent = 以后每个这个星期几都静音）")
    if len(parts) >= 2:
        return repl_kit.Hint("时长", ("25", "50", "75", "clear"),
                             note="可连续给多个；超时就报错；clear = 清空这一段的 session")
    return repl_kit.Hint("参数", ("T1", "50", "50", "clear", "mute T1"), note="按 25/50/75 排这个时段；"
                             "period mute T1 --permanent = 这个时段以后都静音")


def _hint_set(parts):
    if len(parts) >= 2:
        return repl_kit.Hint("取值", ("on", "off"), note="不写 = 恢复默认")
    return repl_kit.Hint("参数", ("<设置 id>", "<value>"),
                         note="完整清单在「设置」视图；不写 value = 恢复默认")


def _hint_refresh(parts):
    if any(str(part).lower() in ("--check", "-c", "check") for part in parts):
        return repl_kit.Hint("参数", ("（跑起来了）",),
                             note="查「该订没订上」「该挂没挂上」，能修的当场修")
    return repl_kit.Hint("选项", ("--check", "-c"),
                         note="不光重拉：还叫 daemon 查一遍、把不对的修掉")


TASK_ACTION_WORDS = ("create", "import", "delete", "modify", "note", "assign", "select",
                     "swap", "repeat", "sync", "archive")

#: 中文「每周几」的显示名（只用于消息；内部一律用 ts.WEEKDAYS）。
CN_WEEKDAY_CHARS = "一二三四五六日"


def _parse_local_iso(raw):
    """``2026-11-04T10:45:00`` → naive datetime；认不出返回 None。"""
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


def task_repeat_plan_from_omnifocus(info, sessions_for_day=None):
    """把 OmniFocus 的重复规则原样搬成本地 ``repeat``。

    **不翻译成「每周几」再丢掉信息**：``rule`` 保留 OmniFocus 的 ICS
    ``ruleString``，日期交给 dateutil 展开；``start`` / ``duration`` 只是用来
    在当天找到对应的 Focusmate session。`DAILY`、多个 `BYDAY`、`INTERVAL`、
    `COUNT` / `UNTIL` 都按原规则走。

    ``info`` 是 :meth:`omnifocus.Client.repetition_info` 的结果。
    ``sessions_for_day(day)`` 可选：给了就去那天的排期里验证「OF 的哪个时间
    才是 Focusmate 的 session 起点」（OmniFocus 的 due 可能记的是结束时间），
    能让复制过来的 ``start`` 更靠谱。

    返回::

        {"supported": True, "rule": "FREQ=DAILY",
         "rule_anchor": "2026-11-04T11:10:00", "rule_offset_days": 0,
         "weekday": 2, "weekday_label": "wed", "start": "10:45",
         "duration": 25, "summary": "每天 10:45（25 min）",
         "source": "due-duration"}

    或 ``{"supported": False, "reason": "...", "rule_string": ...}``。
    非重复任务返回 ``None``。
    """
    if not isinstance(info, dict) or not info.get("repeated"):
        return None
    rule_string = ts.normalize_rule(info.get("rule_string"))
    if not rule_string:
        return {
            "supported": False,
            "rule_string": "",
            "reason": "OmniFocus 说是重复任务，但没拿到 repetitionRule.ruleString",
        }

    duration = info.get("estimated_minutes")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 0:
        duration = None

    # OmniFocus 的 due 更常见的是「截止 = 结束」，但 planned / defer 可能是起点。
    # 三个候选都留着，能对到那天真实 session 的那个优先。
    candidates = []
    planned = _parse_local_iso(info.get("planned"))
    due = _parse_local_iso(info.get("due"))
    deferred = _parse_local_iso(info.get("defer"))
    nxt = _parse_local_iso(info.get("next"))
    if planned:
        candidates.append(("planned", planned, planned))
    if due and duration:
        candidates.append(("due-duration", due - datetime.timedelta(minutes=duration), due))
    if due:
        candidates.append(("due", due, due))
    if deferred:
        candidates.append(("defer", deferred, deferred))
    if nxt and duration:
        candidates.append(("next-duration",
                           nxt - datetime.timedelta(minutes=duration), nxt))
    if nxt:
        candidates.append(("next", nxt, nxt))
    if not candidates:
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": "规则里没有 planned / due / defer / next 时间，推不出会被哪场 session 接住",
        }

    chosen = None
    chosen_reason = ""
    if sessions_for_day is not None:
        for reason, start_dt, anchor_dt in candidates:
            try:
                sessions = sessions_for_day(start_dt.date()) or []
            except Exception:                       # noqa: BLE001 - 排期读不到就退回静态推导
                sessions = []
            start_label = start_dt.strftime("%H:%M")
            for sess in sessions:
                if sess.get("start") != start_label:
                    continue
                if duration is not None and sess.get("duration") is not None:
                    try:
                        if int(sess.get("duration")) != duration:
                            continue
                    except (TypeError, ValueError):
                        pass
                chosen = (reason, start_dt, anchor_dt)
                chosen_reason = "排期里对上了 %s 那场 session" % start_label
                break
            if chosen:
                break

    if chosen is None:
        # 没排期可对：优先 planned（这是 OF 里最像「我打算几点做」的字段），
        # 其次 due - duration（warden 自己 assign 时就是把 due 写成 session 终点），
        # 最后才是 due 本身。
        for reason, start_dt, anchor_dt in candidates:
            if start_dt.minute % 15 == 0:
                chosen = (reason, start_dt, anchor_dt)
                chosen_reason = "按 OmniFocus 的 %s 推导（没在排期里找到可核对的 session）" % reason
                break
    if chosen is None:
        start_dt = candidates[0][1]
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": "推导出的起点 %s 不在 Focusmate 的 15 分钟网格上"
                      % start_dt.strftime("%H:%M"),
        }

    reason, start_dt, anchor_dt = chosen
    ok, why = ts.validate_rule(rule_string, anchor_dt.isoformat(timespec="seconds"))
    if not ok:
        return {
            "supported": False,
            "rule_string": rule_string,
            "reason": why or "RRULE 解析不了",
        }
    # 本地 repeat 锚的是「session 起点」，不是 OmniFocus 的到期时刻。跨午夜时
    # due 可能是周四 00:05、场次却是周三 23:15：RRULE 仍用原始 due 当 DTSTART，
    # 但把日期差记进 rule_offset_days，让本地按场次起点那天的星期去查规则。
    offset = (start_dt.date() - anchor_dt.date()).days
    weekday = start_dt.weekday()
    start = start_dt.strftime("%H:%M")
    rep = {
        "rule": rule_string,
        "rule_anchor": anchor_dt.isoformat(timespec="seconds"),
        "rule_offset_days": offset,
        "weekday": weekday,
        "weekday_label": ts.WEEKDAYS[weekday],
        "start": start,
        "duration": duration,
    }
    if offset:
        chosen_reason += ("；规则锚点是周%s，场次起点在周%s（跨午夜），"
                          "本地按场次起点记"
                          % (CN_WEEKDAY_CHARS[anchor_dt.weekday()],
                             CN_WEEKDAY_CHARS[weekday]))
    return {
        "supported": True,
        "rule_string": rule_string,
        "rule": rule_string,
        "rule_anchor": rep["rule_anchor"],
        "rule_offset_days": offset,
        "weekday": weekday,
        "weekday_label": ts.WEEKDAYS[weekday],
        "start": start,
        "duration": duration,
        "summary": ts.repeat_summary(rep),
        "source": reason,
        "anchor": anchor_dt.isoformat(timespec="seconds"),
        "note": chosen_reason,
    }


def _hint_task(parts):
    # repl_kit 递进来的是**整行**的词（含命令名，含正在敲的那半个词），
    # 而这里只关心 `task` 后面的参数 —— 先把这个命令名摘掉。
    words = [str(part).lower() for part in parts]
    if words and words[0] == "task":
        words = words[1:]
    if not words:
        return repl_kit.Hint("动作", TASK_ACTION_WORDS,
                             note="后缀：-a <编号|none|current> · -s <编号> 批量 · -n 备注"
                                  " · -e true|false 独占 · -m complete|discard · 字段随便写")
    if any(tok in words for tok in ("--mark", "-m")):
        return repl_kit.Hint("取值", TASK_MARK_WORDS,
                             note="手动改本地状态：complete = 完成并归档（不勾 Focusmate）"
                                  "· discard = 放弃（不动外部）")
    for kind in TASK_HOOK_KINDS:
        flags = TASK_HOOK_FLAGS[kind]
        if not any(tok in words for tok in flags):
            continue
        names = th.preset_names(kind)
        if words and words[-1] in flags:
            note = ("当前没有注册任何 %s 预设（框架已就位，列表为空）" % kind
                    if not names else
                    "选中预设后再跟 key=value 参数；Tab 会补可用名字")
            return repl_kit.Hint("预设", names or ("（空列表）",), note=note)
        if kind == "formatter":
            chosen = next((name for name in names if name in words), "")
            if chosen == "repeat":
                note = ("repeat = 固定挂到点名的那一场已有 session；例如 "
                        "`task 3 -f repeat 10:45`（按起点，推荐）或 `-f repeat #2`"
                        "（当天第 2 场）。挂重复任务本体上时，每周每个子任务都固定这一场；"
                        "可带 date=YYYY-MM-DD / tomorrow / today、break_minutes=0")
            elif chosen == "enlist":
                note = ("enlist = 只挂进已有空 session，不切 layout；"
                        "优先孤立且正好同长的场；可带 date=...、break_minutes=5")
            else:
                note = ("end = 在当天最后一段的尾巴上切一场任务大小的 session"
                        "（11:30–11:55 那种，时长取 25/50/75）并自动挂上；"
                        "repeat = 固定挂到指定已有 session（如 `-f repeat 10:45`）；"
                        "可带 date=YYYY-MM-DD / tomorrow / today、break_minutes=5")
            return repl_kit.Hint("参数", names or ("（空列表）",), note=note)
        return repl_kit.Hint(
            "参数", names or ("（空列表）",),
            note="trigger 预设先注册函数才会有名字；"
                 "用法：task <序号> -t <函数> [参数...]")
    if any(tok in words for tok in ("--exclusive", "-e")):
        return repl_kit.Hint("取值", ("true", "false"),
                             note="独占 = 这一场 session 只能有它一条任务"
                                  "（end formatter 排出来的任务自动独占；裸写 -e 就是 true）")
    if any(tok in words for tok in ("-act", "--assign-count", "--assigned-count", "--act")):
        return repl_kit.Hint("取值", ("<正整数>",),
                             note="只改**这一场**要记的 count，不改任务总 count / 时长；"
                                  "例：task 5 -ac -act 2（先挂当前场，再把本场设为 2）")
    if any(tok in words for tok in ("-an", "-ac")):
        return repl_kit.Hint("参数", ("（不用再跟东西）",),
                             note="-an ≡ -a none（下架）· -ac ≡ -a current（当前这一场）")
    if any(tok in words for tok in ("-s", "--from")):
        return repl_kit.Hint("参数", ("-a <编号>", "-a none"),
                             note="把这一场上的全部任务转移到那一场 / 全部下架；"
                                  "没有 -a 就只列出来")
    if any(tok in words for tok in ("--assign", "-a")):
        return repl_kit.Hint(
            "取值", ("<编号>", "none", "current"),
            note="none = 下架（删掉 Focusmate 那条 task）；current = 当前这一场；"
                 "紧凑写法 " + repl_kit.flags.describe(TASK_SHORT_FLAGS, with_words=False))
    # `task 32 swap 21`：动作写在 ID 后面。提示行拿到的是「已经敲完的词」，
    # 所以 `task 32 ` 这时候 parts 只有 ["32"] —— 提示该给「这个 ID 能干什么」。
    head = list(words)
    if head and (head[0].isdigit() or head[0].startswith("id:")):
        if len(head) == 1:
            return repl_kit.Hint("动作", ("swap", "duplicate", "drop", "done", "link",
                                          "-m complete|discard", "-f end", "-e true",
                                          "-a <编号>", "-act N"),
                                 note="数字是面板序号（swap 5 = 和第 5 条换位置）"
                                      " · -m 手动标状态 · -f end 排到当天最后一段"
                                      " · -e true 独占一整场 · -a none 下架"
                                      " · -a current 挂到当前这一场"
                                      " · -act N 改这一场记几个 count")
        head = head[1:]
    action = head[0] if head else ""
    if action in ("import", "粘贴", "导入"):
        return repl_kit.Hint("参数", ("（不写）", "[标题](omnifocus:///task/PK)", "-a 3",
                                      "-st"),
                             note="不写参数 = 读剪贴板（⌘V 到终端只剩标题，这条不会丢链接）；"
                                  "OmniFocus 里多选几条一起 ⌘C 就是一次导入多条（标题/时长"
                                  "按 pk 去 OmniFocus 查），后缀 -a/-f 对整批生效；"
                                  "OmniFocus 里是重复任务时会先问要不要复制重复设置；"
                                  "-st/--single-task = 只导入这一次，不问也不建 repeat")
    if action in ("link", "关联"):
        return repl_kit.Hint("参数", ("（不写）", "omnifocus:///task/PK", "PK"),
                             note="不给链接 = 读剪贴板")
    if action == "create":
        return repl_kit.Hint("参数", ("[id]", "key=value...", "--repeat 周几@HH:MM",
                                      "--assign N", "--note 备注"),
                             note="不给 id 就自动编号 · --note 塞网址最有用")
    if action in ("archive", "history", "归档"):
        return repl_kit.Hint("参数", ("<N>",), note="不给就列最近 5 条")
    if action in ("repeat", "recurring"):
        return repl_kit.Hint("参数", ("<序号>", "周几@HH:MM|off"),
                             note="手写只建每周规则；OmniFocus 导入的任务保留原 RRULE，"
                                  "task repeat <序号> 可直接看")
    if action in ("note", "notes", "备注"):
        return repl_kit.Hint("参数", ("<序号>", "<备注文本 / 网址>"),
                             note="备注会跟着完成归档一起显示（网址可点）")
    if action in ("delete", "select"):
        return repl_kit.Hint("参数", ("<序号>",), note="面板上第几条；面板外的用 id:N")
    if action in ("swap", "调换", "换位"):
        return repl_kit.Hint("参数", ("<序号>", "<序号>"),
                             note="两条任务互换位置（一条赋闲也行）：task 32 swap 21")
    if action == "modify":
        return repl_kit.Hint("参数", ("<任务 ID>", "<字段>", "<新值>"), note="字段不校验")
    if action == "assign":
        return repl_kit.Hint("参数", ("<session ID>",),
                             note="默认挂当前选中的任务；也可 assign <任务ID> <sessionID>")
    return repl_kit.Hint("参数", TASK_ACTION_WORDS,
                         note="任何动作都能带 -a <编号|none|current> / -n 备注"
                              " / -e true|false 独占 / -m complete|discard"
                              " / -t trigger / -f formatter；"
                              "-s <编号> 是批量（那一场的全部任务）")


