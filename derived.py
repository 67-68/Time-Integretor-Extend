#!/usr/bin/env python3
"""derived.py —— note 里带「标题清单」的任务：把它展开成可执行的派生任务。

需求（用户原话，2026-11-15）：

    如果一个任务带着 ``@list`` 记号（note 第一行，或者任务标题最前面），note 里写了

        @list
        - a
        - b
        - c
        - d

    那么 warden 会把这个任务当作四个这个任务，每个 title 对应一行 note；
    完成一个派生出来的任务之后，这一行会被 mark 成完成（``- [x] a``）。

    **不需要**它是重复任务（重复 / 只做一次都行）：``@list`` 记号就是全部条件。

    在被要求的时候（``task 3 -d`` / plan 自动展开）在本地任务库产生派生任务，
    不同步回 OmniFocus —— **除了**它们关联的那条任务的 note。

    每次完成一个子任务之后更新原本的任务，而不是完成原本的任务；
    勾完之后自动收掉整个容器（重复任务 → 放弃整套 occurrence，普通任务 → 完成）。

旧写法 ``# titles``（markdown 标题段）继续认，当作 ``@list`` 的别名。

这个模块只做纯逻辑：解析 note、判断资格、标题/合成 pk、count 范围、勾选后的
note 文本。所有 IO（本地任务库 / OmniFocus / plan）在 warden 那边。
"""
from __future__ import annotations

import re

#: ``source.kind``：清单条目派生出来的任务。**没有** OmniFocus 关联（写回只写 note）。
DERIVED_KIND = "derived"
#: ``source.kind``：重复任务的「再来一条」副本：标题 / count / 时长和本体一样，
#: 可以无限派生，同样不写回 OmniFocus。
COPY_KIND = "derived-copy"

#: 本地字段名（都藏在 ``fields`` 里，这样任务表/筛选照常能用）。
F_DERIVED_OF = "derived_of"            # 父任务本地 id（可能没有：plan 直接派生）
F_DERIVED_LINE = "derived_line"        # note 里那一行的行号（0 起）
F_DERIVED_TEXT = "derived_text"        # 那一行的标题原文（回写时按它匹配）
F_DERIVED_PARENT_PK = "derived_parent_pk"   # 父任务的 OmniFocus primary key
F_DERIVED_PARENT_TITLE = "derived_parent_title"
F_DERIVED_COPY = "derived_copy"        # 第几条副本（1 起，同一父任务递增）

DERIVED_FIELDS = (F_DERIVED_OF, F_DERIVED_LINE, F_DERIVED_TEXT,
                  F_DERIVED_PARENT_PK, F_DERIVED_PARENT_TITLE)

#: 清单记号：``@list``（大小写不敏感，后面可以跟说明，例 ``@list 历史盲抽``）。
#: 写在两个地方都认，等价：
#:   * note 的**第一行非空行**；
#:   * 任务**标题**的最前面（``@list 完成历史 Papers``）。
MARKER = "@list"
_MARKER_RE = re.compile(r"^\s*@list\b", re.I)
#: 标题开头的记号（读法同上）。
_TITLE_RE = re.compile(r"^\s*@list\b", re.I)
#: 旧写法 ``# titles`` 段头（``# Titles`` / ``#titles`` / 前后空格都认）。
#: 保留是为了不把用户已经写好的 note 弄坏；新 note 建议直接用 ``@list``。
_HEADER_RE = re.compile(r"^\s*#+\s*titles\s*$", re.I)
#: markdown 标题（用来确定清单段的结束：清单后面还能写别的段落）。
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
#: bullet：``- a`` / ``* a`` / ``+ a``（缩进不敏感）。
_BULLET_RE = re.compile(r"^(\s*[-*+]\s+)(.*\S)\s*$")
#: 行首 checkbox：``[ ]`` / ``[x]`` / ``[X]``。
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s*(.*)$")


def _checkbox(body):
    """bullet 正文 → ``(checked, 纯标题)``。"""
    match = _CHECKBOX_RE.match(body.strip())
    if not match:
        return False, body.strip()
    return match.group(1).lower() == "x", match.group(2).strip()


def marker_line(note):
    """``@list`` 记号的**行号**（0 起）：note 第一行非空行必须是 ``@list…``。

    不是清单 note → ``None``。只认第一行，是因为这个记号就是给整份 note
    定性的（不需要它藏在一堆说明后面才出现）。
    """
    text = str(note or "")
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        return index if _MARKER_RE.match(line) else None
    return None


def title_marked(title):
    """任务标题是不是以 ``@list`` 开头。"""
    return bool(_TITLE_RE.match(str(title or "")))


def has_marker(note="", title=""):
    """有没有清单记号：``@list``（note 第一行 / 任务标题最前），或旧的 ``# titles`` 段。"""
    if marker_line(note) is not None or title_marked(title):
        return True
    return any(_HEADER_RE.match(line) for line in str(note or "").splitlines())


def strip_marker(title):
    """去掉标题前面的 ``@list``（派生标题、展示时用）。"""
    text = str(title or "")
    if not title_marked(text):
        return text.strip()
    return _TITLE_RE.sub("", text, count=1).lstrip()


def _list_start(note, by_title=False):
    """清单段从哪一行之后开始；没有记号 → ``None``。

    ``by_title`` = 任务标题带记号（note 里不用再写），整份 note 从第一行开始算，
    到下一个 markdown 标题为止。
    """
    first = marker_line(note)
    if first is not None:
        return first + 1
    if by_title:
        return 0
    for index, line in enumerate(str(note or "").splitlines()):
        if _HEADER_RE.match(line):
            return index + 1
    return None


def parse(note="", title=""):
    """note 文本 → ``{"items": [...], "open": n, "total": n, "found": bool}``。

    清单的三种写法（``@list`` 优先）：

        @list                 ← note 第一行非空行；后面到下一个 markdown 标题为止
        - a
        - [x] b

        # titles              ← 旧写法：任意位置的 markdown 标题
        - a

    ``title`` 以 ``@list`` 开头时，note 里不用再写记号，整份 note 就是清单。

    ``items`` 只包含清单段里的 bullet：

        {"line": 3, "text": "a", "checked": False, "prefix": "- "}

    ``prefix`` 是原来的 bullet 前缀（含缩进），回写时按它拼回去，不改用户排版。
    没有清单记号 → ``{"items": [], "open": 0, "total": 0, "found": False}``。
    """
    text = str(note or "")
    lines = text.splitlines()
    start = _list_start(text, by_title=title_marked(title))
    found = start is not None
    items = []
    if start is not None:
        for index in range(start, len(lines)):
            line = lines[index]
            if _HEADING_RE.match(line):
                break
            match = _BULLET_RE.match(line)
            if not match:
                continue
            checked, body = _checkbox(match.group(2))
            if not body:
                continue
            items.append({"line": index, "text": body, "checked": checked,
                          "prefix": match.group(1)})
    return {"items": items,
            "open": sum(1 for item in items if not item["checked"]),
            "total": len(items),
            "found": found}


def open_items(parsed):
    """未勾选的条目（保持 note 顺序）。"""
    return [item for item in (parsed or {}).get("items") or () if not item["checked"]]


def next_open(note="", title=""):
    """下一条未勾选 → ``{"line", "text", ...}``；没有 → ``None``。"""
    items = open_items(parse(note, title))
    return items[0] if items else None


def mark_checked(note, line):
    """把第 ``line`` 行（必须是 ``# titles`` 段里的 bullet）勾上。

    返回 ``(新 note, 是否改动)``；行号越界 / 不是 bullet → ``(原 note, False)``。
    """
    text = str(note or "")
    lines = text.split("\n")
    if not isinstance(line, int) or line < 0 or line >= len(lines):
        return text, False
    raw = lines[line]
    if not _BULLET_RE.match(raw):
        return text, False
    if _CHECKBOX_RE.match(_BULLET_RE.match(raw).group(2).strip()):
        if re.match(r"^\s*[-*+]\s+\[[xX]\]", raw):
            return text, False
        new = re.sub(r"^(\s*[-*+]\s+)\[[ ]\]\s*", r"\1[x] ", raw, count=1)
    else:
        new = re.sub(r"^(\s*[-*+]\s+)", r"\1[x] ", raw, count=1)
    if new == raw:
        return text, False
    lines[line] = new
    return "\n".join(lines), True


# ---------------------------------------------------------------- 资格判定
#
# 资格只看 note：``@list``（或旧的 ``# titles``）+ 至少一条没勾的 bullet。
# **不看重复规则** —— 用户原话：「不需要是（或者强制不能是）重复任务，
# 标题前面加上这个记号就行」。是不是重复任务只影响勾完之后怎么收容器
# （见 :func:`has_repeat`）。


def has_repeat(repeat):
    """这条任务有没有重复规则（有 = 一整套 occurrence，收尾要放弃整套）。"""
    rep = repeat if isinstance(repeat, dict) else {}
    return bool(str(rep.get("rule") or "").strip())


def eligible(note="", title=""):
    """能不能按标题清单展开：有 ``@list`` 记号 + 还有没勾的标题。

    记号写在 note 第一行或任务标题最前面都行 —— 用户原话：「不需要是（或者
    强制不能是）重复任务，标题前面加上这个记号就行」。
    """
    return bool(open_items(parse(note, title)))


def title_of(item):
    """派生任务的标题：``父任务：这一行``。"""
    return "%s：%s" % (item.get("parent_title") or "", item.get("text") or "")


def derived_title(parent_title, text):
    """``完成历史 Papers`` + ``2019-亚太-12`` → ``完成历史 Papers：2019-亚太-12``。"""
    parent = strip_marker(parent_title)
    body = str(text or "").strip()
    if not parent:
        return body
    if not body:
        return parent
    return "%s：%s" % (parent, body)


_SEP = "#d"
#: 重复任务副本的合成 pk 分隔符（``<父 pk>#c<序号>``）。
_COPY_SEP = "#c"


def copy_pk(parent_pk, seq):
    """重复任务副本的合成 pk：``<父 pk>#c<序号>``（序号从 1 起）。"""
    return "%s%s%d" % (str(parent_pk or "").strip(), _COPY_SEP, int(seq))


def is_copy(task):
    """这条本地任务是不是「重复任务再来一条」的副本。"""
    source = (task or {}).get("source") or {}
    return str(source.get("kind") or "") == COPY_KIND


def synthetic_pk(parent_pk, line):
    """派生任务在项目账本里的合成 pk：``<父 pk>#d<行号>``。

    真实 OmniFocus pk 里没有 ``#``，所以不会撞；同一个父任务的每一行都是
    独立的账目（count 投入按它算），重新 plan 时能找回同一条。
    """
    return "%s%s%d" % (str(parent_pk or "").strip(), _SEP, int(line))


def decode_pk(pk):
    """合成 pk → ``(父 pk, 行号)``；不是合成 pk → ``(None, None)``。"""
    text = str(pk or "")
    if _SEP not in text:
        return None, None
    parent, _, tail = text.rpartition(_SEP)
    try:
        return (parent or None), int(tail)
    except (TypeError, ValueError):
        return None, None


def count_span(base, count):
    """``25`` + 5 条 derived → ``"25-125"``（上界 = 所有 derived 的加和，不含本体）。

    没有 count / 没有 derived → ``None``（界面保持原来的单个数字）。
    """
    try:
        base = int(base or 0)
        count = int(count or 0)
    except (TypeError, ValueError):
        return None
    if base <= 0 or count <= 0:
        return None
    return "%d-%d" % (base, base * count)


def is_derived(task):
    """这条本地任务是不是派生任务。"""
    source = (task or {}).get("source") or {}
    fields = (task or {}).get("fields") or {}
    return (str(source.get("kind") or "") == DERIVED_KIND
            or fields.get(F_DERIVED_TEXT) is not None)


def derivation(task):
    """派生任务 → ``{"parent_pk", "line", "text", "parent_title"}``；不是 → ``None``。"""
    if not is_derived(task):
        return None
    fields = (task or {}).get("fields") or {}
    source = (task or {}).get("source") or {}
    line = fields.get(F_DERIVED_LINE)
    if line is None:
        line = source.get("line")
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        line = None
    return {
        "parent_pk": str(fields.get(F_DERIVED_PARENT_PK)
                         or source.get("pk") or "").strip() or None,
        "line": line,
        "text": str(fields.get(F_DERIVED_TEXT) or source.get("text") or "").strip(),
        "parent_title": str(fields.get(F_DERIVED_PARENT_TITLE)
                            or source.get("parent_title") or "").strip(),
    }
