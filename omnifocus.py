#!/usr/bin/env python3
"""omnifocus.py —— warden 侧的 OmniFocus 薄封装：链接解析 + 读写调用。

两件事，分得很清楚：

1. **链接解析**（纯字符串，不碰系统）
   OmniFocus 里「拷贝任务」得到的是一段富文本/链接，粘到不同地方长得不一样：

       [数学易错修正](omnifocus:///task/kc-4VLAE_1L)      Obsidian / Markdown
       <omnifocus:///task/kc-4VLAE_1L>                    纯文本里自动链接
       omnifocus:///task/kc-4VLAE_1L                      裸链接
       title: "数学易错修正"                               只有标题的那种粘贴

   前三种都能拿到 primary key（＝ OmniFocus 的任务身份），第四种只有标题 ——
   「只做单向覆盖」的前提是**认得出是哪个 OmniFocus 任务**，所以只有标题时
   本地任务照建、但**不假装**自己关联上了（``has_id == False``）。

   macOS 剪贴板里其实好几样都有（plain / HTML / RTF / URL），:func:`clipboard_links`
   四个 flavor 全读：URL flavor 就是 ``omnifocus:///task/PK`` 本体（最可靠的 id
   来源），HTML/RTF 里有 ``<a href="omnifocus:///task/…">标题</a>``（多选时才全），
   纯文本往往只有标题 —— 所以「⌘C 任务 → 终端 ⌘V」粘出来的东西**必然**缺 id，
   那条路只能建任务、不能关联（见 :func:`clipboard_flavor` 上面的注释）。

2. **读写调用**（子进程）
   常规读写走 ``omnifocus_tasks.py``（那边的契约是 stdout 只有数据、退出码
   有意义）；这里只负责起子进程、解析 JSON、翻译退出码。另有一个只读小查询
   :meth:`Client.repetition_info`：用 Omni Automation 的 ``repetitionRule``
   查单条任务是不是重复任务，给 ``task import`` 决定要不要复制规则。

   为什么不 import 那个脚本：写操作可能因为 TCC 权限弹框卡几十秒，甚至把
   OmniFocus 的前台焦点抢走；放在子进程里，超时/被杀都只影响这一次调用，
   warden 本身不会被拖进去。而且它本来就是「unix 小工具」，子进程就是它的接口。

退出码→异常的映射（和 omnifocus_tasks.py 的契约一一对应）：

    0  成功
    1  失败 / 解析失败
    2  参数错
    3  自动化权限被拒（TCC）—— 这类要提示用户去「系统设置 → 隐私与安全性 → 自动化」
    4  预览（dry-run），什么都没写
    5  任务 primary key 不存在
    6  标签问题（找不到 / 有歧义）
    7  拒绝改 project_root

本模块不联网、不 import rich、不碰 data/ 下面任何文件。
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "omnifocus_tasks.py")

#: list 结果的默认缓存时长。warden 一次收尾会连着问好几次「这条完成了没」，
#: 每次都真跑一遍 osascript（1 秒级）没必要；但也不能太久，完成状态要新鲜。
CACHE_TTL = 20.0

#: omnifocus:///task/<primary key>；primary key 是 base64url 风格的串
#: （含 `-` `_` 和偶尔的 `=` 填充），所以字符集要放宽，别用 \w。
ID_RE = re.compile(r"omnifocus:///task/([A-Za-z0-9_\-=+%/]+)")
#: [标题](omnifocus:///task/PK) —— Markdown / Obsidian 的粘贴结果
MD_RE = re.compile(r"\[([^\]\n]*)\]\((omnifocus:///task/[^)\s]+)\)")
#: <omnifocus:///task/PK>
ANGLE_RE = re.compile(r"<(omnifocus:///task/[^>\s]+)>")
#: title: "数学易错修正" / title: 数学易错修正 / 标题：…
TITLE_RE = re.compile(r"^(?:title|标题)\s*[:：]\s*(.+)$", re.IGNORECASE)

#: 粘贴里可能带的无用前缀（Obsidian 的列表符号 / 引用符）
_LEAD = " \t-*+>·•"


class OmniFocusError(RuntimeError):
    """一次 OmniFocus 调用失败。``kind`` 决定上层怎么说话。"""

    KINDS = {
        "permission": "自动化权限被拒（系统设置 → 隐私与安全性 → 自动化）",
        "missing_script": "找不到 omnifocus_tasks.py",
        "timeout": "osascript 超时",
        "not_found": "任务不存在",
        "tag_problem": "标签问题",
        "project_root": "拒绝改 project_root",
        "dry_run": "只预览，没写",
        "bad_args": "参数错",
        "unavailable": "OmniFocus 不可用",
        "error": "失败",
    }

    def __init__(self, message, kind="error", exit_code=1, detail=None):
        super(OmniFocusError, self).__init__(message)
        self.kind = kind
        self.exit_code = int(exit_code)
        self.detail = detail

    @property
    def permission(self):
        return self.kind == "permission"

    def hint(self):
        if self.permission:
            return ("系统设置 → 隐私与安全性 → 自动化 → 允许终端/iTerm 控制 OmniFocus；"
                    "改完不用重启 warden")
        if self.kind == "timeout":
            return "OmniFocus 可能正忙（同步/重建数据库）；稍后再试，或把超时调大"
        if self.kind == "unavailable":
            return "确认 OmniFocus 装在本机、并且 omnifocus_tasks.py 能单独跑通"
        return ""

    def __str__(self):
        base = super(OmniFocusError, self).__str__()
        hint = self.hint()
        return "%s（%s）" % (base, hint) if hint else base


# ---------------------------------------------------------------- 链接解析


def _clean_id(raw):
    """把链接里抓到的 primary key 收敛一下（去掉 scheme 前缀、尾部标点）。"""
    pk = str(raw or "").strip()
    if pk.lower().startswith("omnifocus:///task/"):
        pk = pk[len("omnifocus:///task/"):]
    pk = pk.strip("<>")
    pk = pk.rstrip(".,;:!?)]}>\"'，。、；：）」』】")
    pk = pk.replace("%2F", "/").replace("%20", " ")
    return pk


def task_url(pk):
    pk = _clean_id(pk)
    return "omnifocus:///task/%s" % pk if pk else ""


def parse_link(text):
    """一段文本 → ``{"id", "url", "title"}``；没有任务身份时 ``id`` 为 None。

    ``title`` 只有在文本里同时给了标题（Markdown 锚文本 / ``title: "…"``）时才有；
    都不认识的话退化成整行去掉乱字符后的文本。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    body = raw.strip(_LEAD)
    pk = None
    title = None
    match = MD_RE.search(body)
    if match:
        title = match.group(1).strip() or None
        pk = _clean_id(match.group(2))
    else:
        match = ANGLE_RE.search(body)
        if match:
            pk = _clean_id(match.group(1))
        else:
            match = ID_RE.search(body)
            if match:
                pk = _clean_id(match.group(1))
    if pk is None:
        match = TITLE_RE.match(body)
        if match:
            title = match.group(1).strip().strip('"“”\'') or None
    if title is None and pk is not None:
        # 只有链接没有标题：把链接本身剥掉，剩下的才是标题（可能是空）
        rest = MD_RE.sub("", body)
        rest = ANGLE_RE.sub("", rest)
        rest = ID_RE.sub("", rest).strip(_LEAD + " \t<>")
        title = rest or None
    if title is None and pk is None:
        title = body
    return {
        "id": pk,
        "url": task_url(pk) if pk else None,
        "title": title,
        "has_id": bool(pk),
        "raw": raw,
    }


def split_line_links(line):
    """一行里挤了好几条链接 → 按链接边界拆开。

    单独一行只认第一条链接（:func:`parse_link` 用 ``search``）。终端会把多行
    粘贴压成一行（``[甲](pk-a) [乙](pk-b)``），于是「一次导入多条」在粘贴这条路上
    退化成「只导入第一条」。所以**这一行里明确出现两条以上** omnifocus 链接时拆开：

    * Markdown 链接 ``[标题](omnifocus:///task/PK)`` → 每条各成一个条目（标题还有）；
    * 裸链接 ``omnifocus:///task/PK`` → 每条各成一个条目（标题空着，由调用方去
      OmniFocus 查）；
    * 只有一条链接 / 一条都没有 → 原样返回这一行（标题里的字不会被拆散。
      「语法高频 30 个」这种折行靠的是 ``merge_bare`` 那条规则）。
    """
    found = [m.group(0) for m in MD_RE.finditer(line)]
    if len(found) < 2:
        found = ["omnifocus:///task/%s" % _clean_id(m.group(0))
                 for m in re.finditer(r"omnifocus:///task/[^\s\"'<>]+", line)]
        if len(found) < 2:
            return [line]
    return found


def parse_links(text, merge_bare=True):
    """把一段（可能多行的）粘贴拆成若干条链接，一次一行地认。

    一行里挤了好几条链接（终端把换行压成空格）也认，见 :func:`split_line_links`。

    ``merge_bare=True`` 时，**没有一条带 primary key** 的整段只当一条任务：
    纯文本里换行分不清「两条任务」和「一条任务被折行」（真实反馈：``语法高频
    30 个`` 被拆成 ``语法高频`` + ``30 个``）。要多条任务就得有链接，链接里
    才有身份。剪贴板那条路会传 ``False``（HTML 已经给了全部 id 时，纯文本的
    多行是**真的**多条标题）。
    """
    out = []
    for line in str(text or "").splitlines():
        for chunk in split_line_links(line):
            item = parse_link(chunk)
            if item and (item["id"] or item["title"]):
                out.append(item)
    if merge_bare and len(out) > 1 and not any(item.get("id") for item in out):
        merged = " ".join((item.get("title") or "").strip()
                          for item in out if (item.get("title") or "").strip())
        merged = re.sub(r"\s+", " ", merged).strip()
        return [parse_link(merged)] if merged else []
    return out


def is_link(text):
    """这段文本里有没有 OmniFocus 任务身份（没 id 的只有标题不算）。"""
    item = parse_link(text)
    return bool(item and item["id"])


def html_links(markup):
    """从剪贴板的 HTML 里抓 ``<a href="omnifocus:///task/PK">标题</a>``。"""
    out = []
    if not markup:
        return out
    pattern = re.compile(
        r"<a\b[^>]*href\s*=\s*[\"'](omnifocus:///task/[^\"']+)[\"'][^>]*>(.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for href, text in pattern.findall(markup):
        title = re.sub(r"<[^>]+>", "", text)
        title = html_mod.unescape(title)
        title = re.sub(r"\s+", " ", title).strip()
        pk = _clean_id(href)
        out.append({"id": pk or None, "url": task_url(pk), "title": title or None,
                    "has_id": bool(pk), "raw": href})
    return out


def rtf_links(markup):
    """从 RTF 里抓链接。

    OmniFocus 拷任务时 RTF flavor 长这样（实测）：

        {\\field{\\*\\fldinst{HYPERLINK "omnifocus:///task/PK"}}{\\fldrslt 标题}}

    解析不了也不要紧 —— 兜底是把整段里出现的 ``omnifocus:///task/…`` 全抓出来
    （标题就空了，调用方再去 OmniFocus 查）。
    """
    out = []
    if not markup:
        return out
    seen = set()
    pattern = re.compile(
        r'HYPERLINK\s+"(omnifocus:///task/[^"]+)"\s*\}*\s*\{\\fldrslt\s*([^{}]*)',
        re.IGNORECASE | re.DOTALL,
    )
    for href, text in pattern.findall(markup):
        pk = _clean_id(href)
        if not pk or pk in seen:
            continue
        seen.add(pk)
        out.append({"id": pk, "url": task_url(pk), "title": _rtf_unescape(text) or None,
                    "has_id": True, "raw": href})
    for href in re.findall(r"omnifocus:///task/[^\s\"'\\{}<>]+", markup):
        pk = _clean_id(href)
        if not pk or pk in seen:
            continue
        seen.add(pk)
        out.append({"id": pk, "url": task_url(pk), "title": None,
                    "has_id": True, "raw": href})
    return out


def _rtf_unescape(text):
    """RTF 片段 → 纯文本。

    认三种转义，别的控制词一律丢掉：

    * ``\\'3a``             单字节（cp1252）
    * ``\\\\`` / ``\\{`` / ``\\}``  字面反斜杠 / 花括号
    * ``\\u25968``           Unicode 码点（**中文标题全靠它**）

    OmniFocus 拷一条带中文的任务时，RTF 里的标题就是 ``\\uc0\\u25968 \\u23398 …``。
    以前这里用 ``re.sub(r"\\[a-zA-Z]+-?\\d*", "", raw)`` 当控制词整段删掉，
    结果「数学易错修正」变成空串 —— 多选时标题全空，于是纯文本那串标题被当成
    「认不出 primary key 的任务」又建一遍（3 条变 6 条）。

    ``\\uN`` 后面还可能跟 ``\\ucN`` 指定个数的 fallback 字符（给不认 Unicode 的
    阅读器看的），一并跳过。
    """
    raw = text if isinstance(text, str) else str(text or "")
    out = []
    i, size = 0, len(raw)
    fallback = 0                      # \ucN：\uN 之后要跳过几个 fallback 字符
    while i < size:
        ch = raw[i]
        if ch != "\\":
            out.append(" " if ch in "\r\n\t" else ch)
            i += 1
            continue
        nxt = raw[i + 1:i + 2]
        if nxt == "'":                                  # \'3a → 单字节
            try:
                out.append(bytes([int(raw[i + 2:i + 4], 16)]).decode("cp1252", "replace"))
            except ValueError:
                pass
            i += 4
            continue
        if nxt in ("\\", "{", "}"):                     # \\ \{ \} → 字面字符
            out.append(nxt)
            i += 2
            continue
        match = re.match(r"\\uc(-?\d+)\s?", raw[i:])      # \ucN
        if match:
            fallback = max(0, int(match.group(1)))
            i += match.end()
            continue
        match = re.match(r"\\u(-?\d+)", raw[i:])          # \u25968 → Unicode
        if match:
            code = int(match.group(1))
            if code < 0:
                code += 65536
            try:
                out.append(chr(code))
            except ValueError:
                pass
            i += match.end()
            if raw[i:i + 1] == " ":                     # 控制词的分隔空格
                i += 1
            skipped = 0
            while skipped < fallback and i < size and raw[i] not in "\\{}":
                i += 1
                skipped += 1
            continue
        match = re.match(r"\\[a-zA-Z]+-?\d*\s?", raw[i:])  # \f0 \fs24 \pard …
        if match:
            i += match.end()
            continue
        i += 1                                          # 落单的反斜杠
    return re.sub(r"\s+", " ", "".join(out)).strip()


# 剪贴板 flavor 读取 -------------------------------------------------------
#
# ⚠️ 别用 `pbpaste -Prefer html`：这个 macOS 上 `-Prefer` **完全不起作用**，
# 传 html / rtf / url 都原样返回纯文本（实测 2026-10-28，macOS 15.5）。
# 而「在 OmniFocus 里 ⌘C 一条任务、到终端 ⌘V」之所以只剩标题，正是因为
# 终端只吃 string/utf8 那个 flavor —— 链接在**粘贴那一刻**就丢了。
# 所以读剪贴板必须用 osascript 按 flavor 点名要，返回的是 hex 包装：
#
#     osascript -e 'the clipboard as URL'             «data url 6F6D6E69…»
#     osascript -e 'the clipboard as «class HTML»'    «data HTML 3C68746D…»
#     osascript -e 'the clipboard as «class RTF »'    «data RTF 7B5C7274…»
#     osascript -e 'clipboard info'                   有哪些 flavor（排障用）
#
# OmniFocus 拷一条任务时 URL flavor 就是 ``omnifocus:///task/PK`` 本身
# （实测：URL=omnifocus:///task/i5BKXC5uNk0，纯文本只有标题），多条一起拷时
# URL 只剩最后一个、HTML/RTF 里才是全部 —— 所以三个都读、按 id 合并，
# 标题优先取 HTML 的锚文本。

_DATA_RE = re.compile(r"«data\s+([A-Za-z0-9 ]+?)\s*([0-9A-Fa-f]+)»")


def _osascript(script, timeout=5):
    """跑一条 osascript；失败返回空串（读不到剪贴板不该抛异常）。"""
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _decode_data(raw):
    """osascript 的 ``«data HTML 3C61…»`` → 字符串；不是这形状就当纯文本返回。"""
    text = str(raw or "")
    match = _DATA_RE.search(text)
    if not match:
        return text
    try:
        blob = bytes.fromhex(match.group(2))
    except ValueError:
        return ""
    return blob.decode("utf-8", "replace")


def clipboard_flavor(which):
    """按 flavor 点名读剪贴板：``url`` / ``html`` / ``rtf`` / ``text``。

    非 macOS 一律空串 —— 剪贴板是 macOS 才有的东西，别在别的平台上假装有。
    """
    if sys.platform != "darwin":
        return ""
    key = str(which or "").lower()
    if key == "html":
        return _decode_data(_osascript("the clipboard as «class HTML»"))
    if key == "rtf":
        return _decode_data(_osascript("the clipboard as «class RTF »"))
    if key == "url":
        return _decode_data(_osascript("the clipboard as URL")).strip()
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def clipboard_info():
    """剪贴板里有哪些 flavor（``[("URL", 29), …]``）—— 排障 / 测试用。"""
    raw = _osascript("clipboard info") if sys.platform == "darwin" else ""
    out = []
    # 形状：「名字, 字节数」一条条排下来，名字里可能带空格/逗号（``«class RTF »``）：
    #   «class RTF », 392, «class ut16», 18, URL, 29, string, 9
    # 所以按「最后一个是数字」把片段粘回去，而不是硬按逗号切两半。
    pending = ""
    for part in str(raw or "").split(","):
        pending = (pending + "," + part) if pending else part
        bits = pending.split()
        if not bits or not bits[-1].isdigit():
            continue
        name = " ".join(bits[:-1]).replace("«class", "").replace("»", "").strip()
        name = name.strip(",").strip()
        if name:
            out.append((name, int(bits[-1])))
        pending = ""
    return out


def clipboard_text(prefer=None):
    """读剪贴板纯文本（读不到返回空串）。

    ``prefer="html"`` / ``"rtf"`` 时读**那个 flavor** —— 历史调用点就是这么用的，
    但以前实际上退化成 pbpaste 拿纯文本、链接全丢，现在真的读那个 flavor。
    """
    key = str(prefer or "").lower()
    if key in ("html", "rtf"):
        return clipboard_flavor(key)
    return clipboard_flavor("text")


def _norm_title(text):
    """标题归一（比大小写/空白用）：合并空白、去首尾、转小写。"""
    return " ".join(str(text if text is not None else "").split()).strip().lower()


def drop_duplicate_titles(items):
    """去掉「标题和某条有 id 的条目一模一样」的**裸标题**条目。

    多选 ⌘C 时纯文本 flavor 会把那一串标题原样再给一遍（RTF/HTML 已经给了
    「pk + 标题」）。有 pk 却没标题的条目补完标题（去 OmniFocus 查真名）之后，
    必须再跑一次这个去重：不然那串裸标题会被当成「认不出 primary key 的任务」
    又建一遍 —— 3 条变 6 条。

    只按**完全相同**的标题去重，不做模糊匹配：宁可多一条无关联的任务，也不把
    两条不同的活并成一条。
    """
    known = set()
    for item in items or ():
        if item and item.get("id"):
            key = _norm_title(item.get("title"))
            if key:
                known.add(key)
    out = []
    for item in items or ():
        if not item:
            continue
        if not item.get("id") and _norm_title(item.get("title")) in known:
            continue
        out.append(item)
    return out


def clipboard_links():
    """剪贴板 → 链接列表（按 id 合并，标题优先取 HTML）。

    顺序：HTML（多选时的全部 + 好标题）→ RTF（HTML 缺时兜底）→ URL flavor
    （单条任务的链接本体，最能保证「有 id」）→ 纯文本（只有标题的粘贴）。
    有 id 的条目按 id 去重；纯文本里「只有标题」的条目只在标题没出现过时保留
    （调用方会明说「认不出 primary key，所以没关联」）。
    """
    found = []
    by_id = {}

    def add(item):
        if not item:
            return
        pk = item.get("id")
        if not pk:
            return
        if pk in by_id:
            old = by_id[pk]
            # 同一个 id 可能被不同 flavor 给出长度不同的标题（HTML/RTF 偶尔只给
            # 数字片段）；保留更长的那个，别让先读到的短标题把全文盖掉。
            old_title = str(old.get("title") or "").strip()
            new_title = str(item.get("title") or "").strip()
            if new_title and len(new_title) > len(old_title):
                old["title"] = new_title
            return
        by_id[pk] = item
        found.append(item)

    for item in html_links(clipboard_flavor("html")):
        add(item)
    for item in rtf_links(clipboard_flavor("rtf")):
        add(item)
    url_item = parse_link(clipboard_flavor("url"))
    if url_item and url_item.get("id"):
        add(url_item)

    for item in parse_links(clipboard_flavor("text"), merge_bare=False):
        if item.get("id"):
            add(item)
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        if any((x.get("title") or "").strip() == title for x in found):
            continue
        found.append(item)

    # 纯文本把一个标题折成好几行、而剪贴板里只有**一条**链接时，合回去：
    # ``语法高频`` + ``30 个`` → ``语法高频 30 个``。有多条链接时不猜（每条
    # 标题各自对应哪条链接是未知的），只按完全同名去重。
    ids_now = [x for x in found if x.get("id")]
    bare_now = [x for x in found if not x.get("id")]
    if len(ids_now) == 1 and len(bare_now) > 1:
        merged_title = re.sub(r"\s+", " ", " ".join(
            (x.get("title") or "").strip() for x in bare_now)).strip()
        for item in bare_now:
            found.remove(item)
        if merged_title:
            found.append(parse_link(merged_title))

    # 「一条有 id + 一条只有标题」= OmniFocus 拷一条任务的典型剪贴板
    # （URL flavor 给链接、纯文本/HTML 给标题，两边其实是同一条）。这时配对是
    # **读出来的事实**，不是拿标题猜身份 —— 猜的是「标题属于这条链接」，而
    # 剪贴板里只有这一条链接。标题可能被 flavor 截断（真实反馈：``语法高频
    # 30 个`` 的 HTML/RTF 只给出 ``30``），所以「一个标题是另一个的子串」也认，
    # 并保留更长的那份。
    norm = _norm_title

    ids = [x for x in found if x.get("id")]
    bare = [x for x in found if not x.get("id")]
    if len(ids) == 1 and bare:
        id_title = norm(ids[0].get("title"))
        candidates = []
        for item in bare:
            title = norm(item.get("title"))
            if not title:
                continue
            if not id_title or id_title in title or title in id_title:
                candidates.append(item)
        chosen = None
        if len(bare) == 1:
            chosen = bare[0]
        elif len(candidates) == 1 and id_title:
            chosen = candidates[0]
        if chosen is not None:
            chosen_title = (chosen.get("title") or "").strip()
            if chosen_title and (not id_title
                                 or len(norm(chosen_title)) >= len(id_title)):
                ids[0]["title"] = chosen_title
            found.remove(chosen)
    return found


# ---------------------------------------------------------------- 重复规则（只读）

#: 导入任务时用的一个小查询：只问「这一条有没有 repetitionRule」。
#:
#: 为什么不走 ``omnifocus_tasks.py list``：它的字段表是公开契约，重复规则不在里面；
#: 而为了一个导入问句去改那边的输出会牵动所有下游。这里用同一套
#: ``evaluate javascript`` 机制单独查一条，只读、不写、不影响 list 的结果。
REPETITION_JS = r'''
(() => {
  const args = {{ARGS}};
  const reply = obj => JSON.stringify(Object.assign(
    { tool: "omnifocus_repetition", version: 1 }, obj
  ));
  const pad = n => (n < 10 ? "0" + n : "" + n);
  const iso = d => {
    if (!d) return null;
    try {
      return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
        + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    } catch (e) {
      return null;
    }
  };
  const idOf = o => {
    try { return o.id.primaryKey; } catch (e) { return null; }
  };
  try {
    if (typeof Task === "undefined" || typeof Task.byIdentifier !== "function") {
      return reply({
        ok: false,
        error: "omnijs_unavailable",
        message: "当前 OmniFocus 不支持 Task.byIdentifier"
      });
    }
    const task = Task.byIdentifier(args.id);
    if (!task) {
      return reply({ ok: false, error: "not_found", message: "找不到任务：" + args.id });
    }
    const rule = task.repetitionRule || null;
    let next = null;
    let ruleString = null;
    let method = null;
    let scheduleType = null;
    let anchorDateKey = null;
    let catchUpAutomatically = null;
    if (rule) {
      try { ruleString = rule.ruleString || null; } catch (e) {}
      try { method = rule.method ? String(rule.method) : null; } catch (e) {}
      try { scheduleType = rule.scheduleType ? String(rule.scheduleType) : null; } catch (e) {}
      try { anchorDateKey = rule.anchorDateKey ? String(rule.anchorDateKey) : null; } catch (e) {}
      try { catchUpAutomatically = !!rule.catchUpAutomatically; } catch (e) {}
      try { next = iso(rule.firstDateAfterDate(new Date())); } catch (e) {}
    }
    return reply({
      ok: true,
      repeated: !!rule,
      id: idOf(task) || args.id,
      name: task.name || "",
      rule_string: ruleString,
      method: method,
      schedule_type: scheduleType,
      anchor_date_key: anchorDateKey,
      catch_up_automatically: catchUpAutomatically,
      due: iso(task.dueDate),
      defer: iso(task.deferDate),
      planned: iso(task.plannedDate),
      next: next,
      estimated_minutes: (task.estimatedMinutes === undefined
        ? null : task.estimatedMinutes)
    });
  } catch (e) {
    return reply({ ok: false, error: "exception", message: String(e) });
  }
})()
'''


def _applescript_string(text):
    """Python 文本 → AppleScript 字符串字面量。"""
    escaped = (str(text).replace("\\", "\\\\")
               .replace('"', '\\"')
               .replace("\r", "\\r")
               .replace("\n", "\\n")
               .replace("\t", "\\t"))
    return '"' + escaped + '"'


def build_repetition_script(pk, chunk_size=600):
    """查一条 OmniFocus 任务的重复规则（只读）的完整 AppleScript。"""
    pk = _clean_id(pk)
    js = REPETITION_JS.replace(
        "{{ARGS}}",
        json.dumps({"id": pk}, ensure_ascii=False, separators=(",", ":")),
    ).strip("\n")
    chunks = [js[i:i + chunk_size] for i in range(0, len(js), chunk_size)] or [""]
    lines = ["-- 由 omnifocus.py 生成：查重复规则（只读）"]
    lines.append("set js to " + _applescript_string(chunks[0]))
    for chunk in chunks[1:]:
        lines.append("set js to js & " + _applescript_string(chunk))
    lines.append('tell application "OmniFocus"')
    lines.append("\treturn evaluate javascript js")
    lines.append("end tell")
    return "\n".join(lines) + "\n"


def parse_repetition_result(text):
    """把 ``build_repetition_script`` 的 stdout 解析成 dict。

    失败（不是 JSON / tool 不对 / ok=false）抛 :class:`ValueError`；调用方
    负责翻成 :class:`OmniFocusError`。纯函数，测试可以不碰 osascript。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("OmniFocus 没有返回重复规则（空输出）")
    try:
        data = json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError("重复规则查询返回的不是 JSON：%r" % raw[:160]) from None
    if not isinstance(data, dict):
        raise ValueError("重复规则查询应返回对象，实际是 %s" % type(data).__name__)
    if data.get("tool") != "omnifocus_repetition":
        raise ValueError("重复规则信封的 tool 不对：%r" % (data.get("tool"),))
    if not data.get("ok"):
        raise ValueError(data.get("message") or data.get("error") or "查询重复规则失败")
    out = {
        "repeated": bool(data.get("repeated")),
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
        "rule_string": data.get("rule_string") or None,
        "method": data.get("method") or None,
        "schedule_type": data.get("schedule_type") or None,
        "anchor_date_key": data.get("anchor_date_key") or None,
        "catch_up_automatically": data.get("catch_up_automatically"),
        "due": data.get("due") or None,
        "defer": data.get("defer") or None,
        "planned": data.get("planned") or None,
        "next": data.get("next") or None,
        "estimated_minutes": data.get("estimated_minutes"),
    }
    if out["estimated_minutes"] is not None:
        try:
            out["estimated_minutes"] = int(out["estimated_minutes"])
        except (TypeError, ValueError):
            out["estimated_minutes"] = None
    return out


# ---------------------------------------------------------------- 子进程调用


def python_bin():
    """用哪个 python 去跑子进程（和 warden 自己是同一个，venv 里也不会错）。"""
    return os.environ.get("OMNIFOCUS_PYTHON") or sys.executable or "python3"


class Client(object):
    """``omnifocus_tasks.py`` 的子进程封装 + 一点点缓存。"""

    def __init__(self, script=None, python=None, timeout=120.0, osascript=None,
                 enabled=None, clock=None):
        self.script = script or os.environ.get("OMNIFOCUS_SCRIPT") or SCRIPT
        self.python = python or python_bin()
        self.timeout = float(timeout)
        self.osascript = osascript or os.environ.get("OSASCRIPT") or "/usr/bin/osascript"
        self.enabled = enabled
        self.clock = clock or time.monotonic
        self._cache = None
        self._cache_at = 0.0
        self.calls = 0
        # 收尾的后台核对线程和主线程可能同时问 OmniFocus；子进程本身没问题，
        # 但缓存和「一次只起一个 osascript」得串起来。
        self._lock = threading.RLock()

    # -- 可用性 -------------------------------------------------------
    def available(self):
        """能不能真的去问 OmniFocus（平台 + 脚本 + osascript 三样都得在）。"""
        if self.enabled is False:
            return False
        if sys.platform != "darwin":
            return False
        if not os.path.exists(self.script):
            return False
        return bool(shutil.which(self.osascript) or os.path.exists(self.osascript))

    def why_unavailable(self):
        if self.enabled is False:
            return "设置里关掉了 OmniFocus 集成（set omnifocus off 打开）"
        if sys.platform != "darwin":
            return "只在 macOS 上能用（需要 osascript + OmniFocus）"
        if not os.path.exists(self.script):
            return "找不到 %s" % self.script
        return "找不到 osascript：%s" % self.osascript

    # -- 底层 ---------------------------------------------------------
    def _run(self, args, timeout=None, expect_jsonl=False):
        """跑一次子进程；返回 (退出码, stdout, stderr)。"""
        if not self.available():
            raise OmniFocusError(self.why_unavailable(), kind="unavailable", exit_code=1)
        cmd = [self.python, self.script] + [str(a) for a in args]
        if self.osascript:
            # 用哪个 osascript 由调用方定（默认 /usr/bin/osascript），原样透传
            cmd += ["--osascript", self.osascript]
        self.calls += 1
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise OmniFocusError(
                "osascript 超过 %.0f 秒没返回" % (timeout or self.timeout),
                kind="timeout", exit_code=1,
            ) from None
        except OSError as e:
            raise OmniFocusError("起不来子进程：%s" % e, kind="unavailable", exit_code=1) from None
        return proc.returncode, proc.stdout, proc.stderr

    def _run_applescript(self, script, timeout=None):
        """把一段 AppleScript 从 stdin 喂给 osascript，返回 stdout。

        ``repetition_info`` 用这条：它只查一条任务，没必要再起一个 Python
        子进程；仍然独占一次 osascript，超时/权限错误照常翻成
        :class:`OmniFocusError`。
        """
        if not self.available():
            raise OmniFocusError(self.why_unavailable(), kind="unavailable", exit_code=1)
        self.calls += 1
        try:
            proc = subprocess.run(
                [self.osascript], input=script, capture_output=True, text=True,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise OmniFocusError(
                "osascript 超过 %.0f 秒没返回" % (timeout or self.timeout),
                kind="timeout", exit_code=1,
            ) from None
        except OSError as e:
            raise OmniFocusError("起不来 osascript：%s" % e,
                                 kind="unavailable", exit_code=1) from None
        if proc.returncode != 0:
            text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
            lower = text.lower()
            if ("not authorized" in lower or "not permitted" in lower
                    or "-1743" in lower or "1743" in lower):
                kind = "permission"
            elif "application isn" in lower or "can’t be found" in lower or "can't be found" in lower:
                kind = "unavailable"
            else:
                kind = "error"
            raise OmniFocusError(
                "查 OmniFocus 重复规则失败（退出码 %d）：%s"
                % (proc.returncode, text.splitlines()[-1] if text else "(没有输出)"),
                kind=kind, exit_code=proc.returncode, detail=text,
            )
        return proc.stdout

    @staticmethod
    def _raise_for(rc, out, err, what):
        """把退出码翻译成异常；成功（0）什么也不做。"""
        text = (err or "").strip() or (out or "").strip()
        short = text.splitlines()[-1] if text else ""
        if rc == 0:
            return
        kind = {
            2: "bad_args",
            3: "permission",
            4: "dry_run",
            5: "not_found",
            6: "tag_problem",
            7: "project_root",
        }.get(rc, "error")
        raise OmniFocusError(
            "%s失败（退出码 %d）%s" % (what, rc, ("：" + short) if short else ""),
            kind=kind, exit_code=rc, detail=text,
        )

    # -- 读 -----------------------------------------------------------
    def list_tasks(self, refresh=False, fields=None, timeout=None):
        """全部任务（JSONL → list[dict]）。默认带 20 秒缓存。"""
        now = self.clock()
        with self._lock:
            if (not refresh and self._cache is not None
                    and (now - self._cache_at) < CACHE_TTL):
                rows = self._cache
            else:
                args = ["list"]
                if fields:
                    args += ["--fields", ",".join(fields)]
                rc, out, err = self._run(args, timeout=timeout)
                self._raise_for(rc, out, err, "读 OmniFocus 任务")
                rows = []
                for line in (out or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        raise OmniFocusError("读回来的不是 JSON 行：%r" % line[:120],
                                             kind="error", exit_code=1) from None
                self._cache = rows
                self._cache_at = now
        if fields:
            want = list(fields)
            return [{k: row.get(k) for k in want} for row in rows]
        return rows

    def get(self, pk, refresh=False):
        """按 primary key 找一条；找不到返回 None。"""
        pk = _clean_id(pk)
        if not pk:
            return None
        for row in self.list_tasks(refresh=refresh):
            if str(row.get("id") or "") == pk:
                return row
        return None

    def repetition_info(self, pk, timeout=None):
        """查一条任务是不是 OmniFocus 的重复任务；返回 dict。

        - ``repeated=False``：普通任务，``rule_string`` 等为 ``None``；
        - ``repeated=True``：带 ``rule_string`` / ``method`` / ``due`` / ``planned``
          / ``next`` / ``estimated_minutes`` 等字段，交给 warden 判断能不能
          映射成本地「每周重复」；
        - 查询本身失败抛 :class:`OmniFocusError`（导入方会退化成「没法核对，
          先按普通任务导入」，不拿网络故障当重复任务）。

        只读；不写 OmniFocus、不碰缓存。
        """
        pk = _clean_id(pk)
        if not pk:
            raise OmniFocusError("没有 OmniFocus primary key，查不了重复规则",
                                 kind="bad_args", exit_code=2)
        script = build_repetition_script(pk)
        with self._lock:
            out = self._run_applescript(script, timeout=timeout)
        try:
            return parse_repetition_result(out)
        except ValueError as e:
            raise OmniFocusError("查重复规则失败：%s" % e, kind="error", exit_code=1) from None

    def invalidate(self):
        self._cache = None
        self._cache_at = 0.0

    # -- 写 -----------------------------------------------------------
    def set_task(self, pk, *flags, yes=True, timeout=None, allow_project_root=False):
        """跑 ``set`` 子命令；返回解析后的结果 dict。

        ``flags`` 原样透传（``--complete`` / ``--drop`` / ``--due 2026-…`` / …）。
        不给 ``yes`` 就是预览（退出码 4 → 抛 kind="dry_run"）。
        """
        args = ["set", "--id", _clean_id(pk)] + [str(f) for f in flags]
        if allow_project_root:
            args.append("--allow-project-root")
        if yes:
            args.append("--yes")
        with self._lock:
            rc, out, err = self._run(args, timeout=timeout)
        if rc == 4 and out.strip():
            # 预览也算成功：结果 JSON 在 stdout 上，只是没写
            data = _load_json(out)
            data["_dry_run_exit"] = 4
            return data
        self._raise_for(rc, out, err, "写 OmniFocus 任务")
        self.invalidate()
        return _load_json(out)

    def complete(self, pk, at=None, due=None, yes=True, timeout=None):
        """完成一条任务；``due`` 给了就顺手把截止时间写回去（一条命令）。"""
        flags = ["--complete"]
        if at:
            flags += ["--at", at]
        if due:
            flags += ["--due", due]
        return self.set_task(pk, *flags, yes=yes, timeout=timeout)

    def drop(self, pk, at=None, all_occurrences=False, yes=True, timeout=None):
        flags = ["--drop"]
        if at:
            flags += ["--at", at]
        if all_occurrences:
            flags.append("--all-occurrences")
        return self.set_task(pk, *flags, yes=yes, timeout=timeout)

    def set_due(self, pk, due, yes=True, timeout=None):
        return self.set_task(pk, "--due", due, yes=yes, timeout=timeout)

    def add_tag(self, pk, name, yes=True, timeout=None):
        return self.set_task(pk, "--add-tag", name, yes=yes, timeout=timeout)

    def incomplete(self, pk, yes=True, timeout=None):
        return self.set_task(pk, "--incomplete", yes=yes, timeout=timeout)


def _load_json(out):
    text = (out or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        raise OmniFocusError("写操作返回的不是 JSON：%r" % text[:120],
                             kind="error", exit_code=1) from None


# ---------------------------------------------------------------- 模块级默认
#
# warden 只用一个 client；测试可以自己 new 一个（或者把 _DEFAULT 换掉）。

_DEFAULT = None


def default_client(**kw):
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Client(**kw)
    return _DEFAULT


def main(argv=None):
    """小 CLI：`omnifocus.py parse` 读 stdin 认链接；`links` 读剪贴板。

    只为了「不许开 warden 也能验证一下粘贴结果」这一个场景，不承担别的职责。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "parse"
    if action in ("parse", "-"):
        text = sys.stdin.read() if not argv[1:] else " ".join(argv[1:])
        for item in parse_links(text):
            print(json.dumps(item, ensure_ascii=False))
        return 0
    if action == "links":
        items = clipboard_links()
        for item in items:
            print(json.dumps(item, ensure_ascii=False))
        return 0 if items else 1
    if action == "get":
        if not argv[1:]:
            sys.stderr.write("用法：omnifocus.py get <primary key>\n")
            return 2
        client = default_client()
        try:
            row = client.get(argv[1], refresh=True)
        except OmniFocusError as e:
            sys.stderr.write("%s\n" % e)
            return e.exit_code
        if not row:
            sys.stderr.write("没有这条任务\n")
            return 5
        print(json.dumps(row, ensure_ascii=False))
        return 0
    sys.stderr.write("用法：omnifocus.py parse|links|get <pk>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
