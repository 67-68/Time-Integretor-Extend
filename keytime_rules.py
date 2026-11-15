#!/usr/bin/env python3
"""keytime_rules.py —— Key Time 规则表：**哪些日历时间块要报警、用哪套文字**。

为什么要有这一层
----------------
以前「哪些块算 Key Time」是硬编码在 download_calendar.py 里的日历名列表
（``["通勤", "休息"]``），模板也硬编码在 keytime.py 里。现在下载层负责记录
**所有**日历时间块，报警与否完全交给这个文件里的规则：

    一条规则有两个匹配条件（可以只写一个，也可以都写）
        name     匹配**时间块名字**（事件标题）—— 优先级最高
        calendar 匹配**时间块类型**（日历名）—— 名字命中的规则永远排在前面

    匹配方式：大小写不敏感的「包含」；pattern 里带 ``*`` / ``?`` 时按通配符。
    多条同级命中：文件里写在前面的赢。
    都不命中 → 不是 Key Time（工作 / 空闲 / 上课 默认就不报警）。
    一条 name 和 calendar 都不写的规则 = 兜底规则，只在前面都没命中时生效。

文件格式
--------
用的是 **TOML 的一个小子集**（Python 3.9 没有 tomllib，所以这里自己解析）：

    [display]                 全局：还没到点时那一行淡红提示的模板
    line = "..."

    [[rule]]                  一条规则一段
    id = "exercise"
    name = "运动"             匹配名字（可省）
    calendar = "通勤"         匹配类型（可省）
    enabled = true            可省，默认 true
    headline = "..."          手机推送标题 / 面板标题（可省 → 模板第一行）
    text = \"\"\"             屏幕模板（多行，模板第一行固定红底白字）
    ...
    \"\"\"
    speech = "..."            语音播报稿（可省 → 模板第一行）

支持：``#`` 注释、三个双引号的多行字符串 / 三个单引号的原样多行、``"带转义"``、裸值（宽松：
``name = 运动`` 也认）。不认识的键会报「第 N 行」，避免 ``nmae`` 这种手滑静默失效。

这个文件是**给用户改的**，所以默认内容自带完整说明（见 :data:`DEFAULT_RULES_TEXT`）。
解析失败不会让 warden 崩：返回空规则 + 错误文本，由 warden 提示。
"""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DEFAULT_RULES_FILE = os.path.join(DATA, "keytime-rules.toml")

#: 还没到点时契约视图顶部那一行（可用变量见规则文件顶部注释）。
DEFAULT_DISPLAY = "🔴 KEY TIME {date_short} {block} · {when} · {start}-{end}"

#: 一条规则认得的键；写错键名会报行号（不然 ``nmae = 运动`` 会静默失效）。
RULE_KEYS = ("id", "name", "calendar", "enabled", "headline", "text", "speech")
DISPLAY_KEYS = ("line",)

_INT_RE = re.compile(r"^[+-]?\d+$")


# ================================================================ 模型


@dataclasses.dataclass
class Rule(object):
    """一条 Key Time 规则。``name`` / ``calendar`` 都空 = 兜底规则。"""

    id: str = ""
    name: str = ""
    calendar: str = ""
    headline: str = ""
    text: str = ""
    speech: str = ""
    enabled: bool = True

    def match_name(self, title):
        return bool(self.name) and pattern_match(self.name, title)

    def match_calendar(self, calendar):
        return bool(self.calendar) and pattern_match(self.calendar, calendar)

    @property
    def catch_all(self):
        return not self.name and not self.calendar

    def describe(self):
        bits = []
        if self.name:
            bits.append("name=%s" % self.name)
        if self.calendar:
            bits.append("calendar=%s" % self.calendar)
        if not bits:
            bits.append("兜底")
        if not self.enabled:
            bits.append("已停用")
        return "%s ← %s" % (self.id or "(没写 id)", " ".join(bits))


@dataclasses.dataclass
class RuleSet(object):
    rules: list = dataclasses.field(default_factory=list)
    display: str = ""

    def match(self, calendar, title):
        """返回命中的规则或 None。名字优先 > 类型 > 兜底，同级按文件顺序。"""
        title = str(title or "").strip()
        calendar = str(calendar or "").strip()
        name_text = title or calendar          # 事件没标题时用日历名当名字
        for rule in self.rules:                # 1) 名字命中（最高优先级）
            if rule.enabled and rule.match_name(name_text):
                return rule
        for rule in self.rules:                # 2) 类型命中
            if rule.enabled and rule.match_calendar(calendar):
                return rule
        for rule in self.rules:                # 3) 兜底（默认没有 / 默认停用）
            if rule.enabled and rule.catch_all:
                return rule
        return None

    def by_id(self, rule_id):
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None


def pattern_match(pattern, text):
    """大小写不敏感：带 ``*`` / ``?`` 按通配符，否则按「包含」。"""
    pattern = str(pattern or "").strip()
    text = str(text or "")
    if not pattern:
        return False
    low = text.casefold()
    pat = pattern.casefold()
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatchcase(low, pat)
    return pat in low


# ================================================================ 解析（TOML 子集）


def _unescape(text):
    """处理 ``\\n`` / ``\\t`` / ``\\"`` / ``\\\\``；别的转义原样保留。"""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r",
                        '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_comment(text):
    """去掉行尾 ``#`` 注释（引号里的 ``#`` 不算）。"""
    quote = None
    for index, ch in enumerate(text):
        if quote:
            if ch == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return text[:index].rstrip()
    return text.rstrip()


def _parse_value(raw, errors, where):
    """把一个标量值变成 str / int / bool。宽松：裸值当字符串。"""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'" and len(raw) >= 2 and raw[-1] == raw[0]:
        body = raw[1:-1]
        return _unescape(body) if raw[0] == '"' else body
    if raw[0] in "\"'":
        errors.append("%s：引号没配对：%s" % (where, raw))
        body = raw[1:]
        return _unescape(body) if raw[0] == '"' else body
    low = raw.casefold()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT_RE.match(raw):
        return int(raw)
    return raw                                   # 裸值（name = 运动）也认


def parse_rules(text):
    """解析规则文件文本 → ``(RuleSet, [错误/警告])``。纯函数，不碰磁盘。"""
    errors = []
    rules = []
    display = {}
    current = None
    current_kind = None
    current_line = 0
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()
        index += 1
        where = "第 %d 行" % index
        if not line or line.startswith("#"):
            continue
        # ---- 段落头 ----
        if line.startswith("[[") and line.endswith("]]"):
            name = line[2:-2].strip()
            if name != "rule":
                errors.append("%s：只支持 [[rule]]，收到 [[%s]]" % (where, name))
                current = None
                continue
            current = {}
            current_kind = "rule"
            current_line = index
            rules.append(current)
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name == "display":
                current = display
                current_kind = "display"
                continue
            errors.append("%s：只支持 [[rule]] / [display]，收到 [%s]" % (where, name))
            current = None
            continue
        # ---- key = value ----
        if "=" not in line:
            errors.append("%s：看不懂（要 key = value）：%s" % (where, raw_line))
            continue
        key, _sep, rest = line.partition("=")
        key = key.strip()
        rest = rest.strip()
        if current is None:
            errors.append("%s：%s = … 不在任何 [[rule]] / [display] 里" % (where, key))
            continue
        allowed = RULE_KEYS if current_kind == "rule" else DISPLAY_KEYS
        if key not in allowed:
            errors.append("%s：不认识的键 %r（可用：%s）"
                          % (where, key, " / ".join(allowed)))
            continue
        # ---- 多行字符串 ----
        if rest[:3] in ('"""', "'''"):
            delim = rest[:3]
            after = rest[3:]
            if delim in after:                      # text = """一行写完"""
                value = after[:after.index(delim)]
                value = _unescape(value) if delim == '"""' else value
            else:
                parts = [after] if after else []
                closed = False
                while index < len(lines):
                    nxt = lines[index]
                    index += 1
                    cut = nxt.find(delim)
                    if cut >= 0:
                        if nxt[:cut]:
                            parts.append(nxt[:cut])
                        closed = True
                        break
                    parts.append(nxt)
                if not closed:
                    errors.append("%s：%s 的多行字符串没有收尾的 %s"
                                  % (where, key, delim))
                value = "\n".join(parts)
                value = _unescape(value) if delim == '"""' else value
            current[key] = value
            continue
        current[key] = _parse_value(_strip_comment(rest), errors, where)
    # ---- 收尾：把 dict 变成 Rule ----
    out = []
    for number, item in enumerate(rules, 1):
        rule = Rule(
            id=str(item.get("id") or "rule-%d" % number).strip(),
            name=str(item.get("name") or "").strip(),
            calendar=str(item.get("calendar") or "").strip(),
            headline=str(item.get("headline") or "").strip(),
            text=str(item.get("text") or ""),
            speech=str(item.get("speech") or "").strip(),
            enabled=bool(item.get("enabled", True)),
        )
        if not rule.text.strip():
            errors.append("规则 %s 没有 text（模板），命中时会用内置兜底模板" % rule.id)
        out.append(rule)
    seen = set()
    for rule in out:
        if rule.id in seen:
            errors.append("规则 id 重复：%s（前面的那条继续生效）" % rule.id)
        seen.add(rule.id)
    return RuleSet(rules=out, display=str(display.get("line") or "").strip()), errors


# ================================================================ 默认规则文件


DEFAULT_RULES_TEXT = r'''# =============================================================================
#  Key Time 规则表 —— 哪些日历时间块要报警、用哪套文字
#  位置：data/keytime-rules.toml（本文件是自动生成的；直接改它，存盘后 5 秒内生效）
#  立刻生效：warden 里敲 `keytime refresh`；看解析结果：`keytime rules`
#  想恢复成出厂内容：`keytime rules reset`（会覆盖你现在写的所有东西）
# =============================================================================
#
# 【下载层】download_calendar.py 把**所有日历**的每个非全天时间块写进
#   temp/calendar-download.json 的 time_blocks —— 不再只挑「通勤/休息」两个日历。
# 【这一层】哪些时间块算 Key Time，完全由下面的规则决定：
#
#   一条规则有两个匹配条件，可以只写一个，也可以都写：
#     name     = 匹配**时间块名字**（日历事件标题）。优先级最高。
#     calendar = 匹配**时间块类型**（日历名）。只要有一条规则按 name 命中，
#                就轮不到 calendar 的规则（名字 > 类型）。
#   匹配写法：大小写不敏感的「包含」；带 * 或 ? 时按通配符匹配。
#   事件没有标题时，用日历名当名字去匹配。
#   多条同级命中：**文件里写在前面的赢**。
#   一条 name 和 calendar 都不写的规则 = 兜底规则：只在上面都没命中时生效
#   （默认关着 —— 打开它会让所有没命中的时间块都报警）。
#   都不命中 → 不是 Key Time，不报警（工作 / 空闲 / 上课 默认就不报）。
#
# -----------------------------------------------------------------------------
#  模板内联变量（渲染时替换；写错的变量会原样留在屏幕上，方便你发现）
# -----------------------------------------------------------------------------
#    {block}          时间块名字（没标题时用日历名）
#    {calendar}       时间块类型（日历名）
#    {kind}           命中的规则 id
#    {date}           2026-11-15
#    {key_time}       应开始的时刻 16:00                （别名 {start}）
#    {key_end}        应结束的时刻 17:00                （别名 {end}）
#    {now_time}       现在 16:12
#    {overdue_mins}   已超时分钟数（未到点时为负）
#    {duration_mins}  这个时间块本来有多长
#    {remain_mins}    离块结束还剩多少分钟（已过为负）
#    {next_task}      下一个 session 的第一个任务（没有则「后面的安排」）
#    {next_tasks}     下一个 session 的全部任务（顿号连接）
#    {next_time}      下一个 session 的开始时刻（没有则空）
#    {next_in_mins}   离下一个 session 还有多少分钟（没有则空）
#    {delay}          拖延让后面整体顺延的分钟数
#    {pushed_count}   只是顺延、没被挤出排期的场次数
#    {dropped_count}  被挤出今日排期的场次数
#    {dropped_mins}   被挤出排期的总分钟数
#    {dropped_tasks}  被挤出排期的任务名（顿号连接）
#    {extra_sessions} 今晚要额外补几个 25 分钟 session
#    {extra_minutes}  今晚要补的总分钟数
#    {squeeze_text}   一整句挤压后果（自动在「挤出 / 顺延 / 没排期」之间换句）
#    {squeeze_speech} 同上，但更短，给语音用
#    {extra_text}     一整句加班补课（没挤出时为空 —— 整行会自动删掉）
#    {goal}           Project 总目标（还没接好，通常为空）
#    {goal_line}      一整句「总目标将因此失败」（没有目标时给占位提示）
#    {goal_speech}    「总目标：X。」（没有目标时为空）
#    {chore}          现在还能顺手做掉的那件杂事的标题
#    {acked_at}       确认时刻（没确认为空）
#    {acked_line}     确认后那行绿字
#
#  模板里支持 **加粗**；行首前缀可以改颜色（前缀本身不显示）：
#    !  红字加粗      -  品红加粗（段落标题）      >  黄字      #  灰字
#  模板第一行固定「红底白字」，当报警标题和手机推送标题用。
#  某一行渲染后变成空字符串（比如 {extra_text} 为空）→ 这一行整行消失。
#
# -----------------------------------------------------------------------------
#  还没到点时，契约视图顶部那一行淡红提示：
#    {date} {date_short} {block} {calendar} {title} {kind} {start} {end} {in_mins} {when}
# -----------------------------------------------------------------------------

[display]
line = "🔴 KEY TIME {date_short} {block} · {when} · {start}-{end}"

# =============================================================================
#  规则（写在前面的优先）
# =============================================================================

[[rule]]
id = "exercise"
# 两个条件都写了：标题含「运动」**或**在「运动」日历里都算 —— 名字优先于类型。
# （同一日历里标题叫「晨跑」「拉伸」的事件也会用这一套模板。）
name = "运动"
calendar = "运动"
headline = "🚨 [肉体僵死警报] 动作：出去随便干点什么"
text = """
🚨 [肉体僵死警报] 动作：出去随便干点什么
>⏱️ 已粘在椅子上超时：{overdue_mins} 分钟（应于 {key_time} 弹射，现已 {now_time}）

-🩸 硬件报废与排期后果：
你已经在屏幕前连续静坐充血太久，前额叶正在缺氧降频！
如果你现在继续赖在电脑前，后续的【{next_task}】将直接沦为「低效死磕局」！

-🏳️ 最低降级契约 (Zero-Friction Fallback)：
觉得跑步太累？不想换衣服？没关系！
契约允许降级：**哪怕你不跑，现在立刻下楼漫无目的地游荡/散步 30 分钟，也算 100% 完成任务！**
!唯一死刑红线：绝对不准继续待在电脑前！立刻站起来，拿钥匙，滚出门！
"""
speech = "运动超时 {overdue_mins} 分钟。现在立刻站起来，拿钥匙出门 —— 哪怕只是散步三十分钟。"

[[rule]]
id = "commute"
calendar = "通勤"
headline = "🚨 [KEY TIME 违约警报] 通勤"
text = """
🚨 [KEY TIME 违约警报] 通勤
-动作：立刻动身（通勤）
>⏱️ 已超时：{overdue_mins} 分钟（应于 {key_time} 启动，现已 {now_time}）
{squeeze_text}
{extra_text}
!🔴 计时器正在跳动：每多看屏幕 1 秒，今晚就晚睡 1 秒！
"""
speech = "通勤超时 {overdue_mins} 分钟，应于 {key_time} 出发。{squeeze_speech}"

[[rule]]
id = "rest"
calendar = "休息"
headline = "🚨 [KEY TIME 违约警报] 休息"
text = """
🚨 [KEY TIME 违约警报] 休息
-动作：强制断电休息（离开屏幕）
>⏱️ 已超时：{overdue_mins} 分钟（应于 {key_time} 启动，现已 {now_time}）
-🧠 生理算力坍塌后果：
你正在拒绝清理大脑 L1 缓存！
{goal_line}
原本 1 遍能读懂的东西，你会被迫反复空转 4 遍；冲动控制力归零，下个 session 极高概率半途破防。
>🛑 别拿垃圾时间骗自己：现在立刻离开屏幕 {duration_mins} 分钟。
"""
speech = "休息超时 {overdue_mins} 分钟。立刻离开屏幕。{goal_speech}再不休息，下个 session 会脑雾死锁。"

[[rule]]
id = "generic"
# name 和 calendar 都不写 = 兜底规则。默认停用：工作/空闲/上课 不该报警。
# 想让「所有没被上面命中的时间块」都按这套文案报警，把下面这行改成 enabled = true
enabled = false
headline = "🚨 [KEY TIME 违约警报] {block}"
text = """
🚨 [KEY TIME 违约警报] {block}
-动作：立刻处理这个 key time
>⏱️ 已超时：{overdue_mins} 分钟（应于 {key_time} 启动，现已 {now_time}）
⏳ 这个 key time 正在被拖延；后面的时间盒按 1:1 顺延。
"""
speech = "key time 超时 {overdue_mins} 分钟：{block}。"
'''


_DEFAULT_CACHE = []


def default_rules():
    """出厂规则（解析一次，之后复用）。"""
    if not _DEFAULT_CACHE:
        rule_set, _errors = parse_rules(DEFAULT_RULES_TEXT)
        _DEFAULT_CACHE.append(rule_set)
    return _DEFAULT_CACHE[0]


# ================================================================ 读写磁盘


def rules_path(path=None):
    """规则文件路径。``TI_KEYTIME_RULES`` 指到临时目录（测试/多环境）。"""
    return path or os.environ.get("TI_KEYTIME_RULES") or DEFAULT_RULES_FILE


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def save_default_rules(path=None):
    """写出厂规则文件（含全部注释）→ 路径。"""
    path = rules_path(path)
    _write_atomic(path, DEFAULT_RULES_TEXT)
    return path


def load_rules(path=None):
    """读规则文件 → ``(RuleSet, 错误文本)``。

    - 文件不存在：**自动写出厂版本**（用户第一次打开就能照着改）；
    - 解析出错：仍然返回能解析出来的规则，错误文本交给 warden 提示；
    - 一条规则都没有：返回空规则集（宁可不报警，也不乱报警）。
    """
    path = rules_path(path)
    if not os.path.exists(path):
        try:
            save_default_rules(path)
        except OSError as exc:
            return default_rules(), "规则文件写不出来（%s）：%s" % (path, exc)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return default_rules(), "规则文件读不了（%s）：%s" % (path, exc)
    rule_set, errors = parse_rules(text)
    if not rule_set.rules:
        errors.append("规则文件里没有任何 [[rule]] —— 现在不会报警")
    return rule_set, "\n".join(errors)


def main(argv=None):                                   # pragma: no cover - 自检
    import argparse
    parser = argparse.ArgumentParser(prog="keytime_rules.py",
                                     description="Key Time 规则文件自检")
    parser.add_argument("--rules", default=None)
    parser.add_argument("--reset", action="store_true", help="恢复出厂规则文件")
    args = parser.parse_args(argv)
    path = rules_path(args.rules)
    if args.reset:
        print("已写出厂规则：%s" % save_default_rules(path))
        return 0
    rule_set, error = load_rules(path)
    print("规则文件：%s" % path)
    print("display  ：%s" % (rule_set.display or DEFAULT_DISPLAY))
    for rule in rule_set.rules:
        print("  - %s" % rule.describe())
    if error:
        print("\n[warn] %s" % error)
    print("\n和真实时间块试匹配：python3 keytime.py list")
    return 0


if __name__ == "__main__":                             # pragma: no cover
    import sys
    sys.exit(main())
