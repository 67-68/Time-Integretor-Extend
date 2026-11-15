#!/usr/bin/env python3
"""derived.py —— note 里带「标题清单」的每日重复任务：把它展开成可执行的派生任务。

需求（用户原话，2026-11-15）：

    如果一个重复任务，每天重复 + 非以推迟日期为重复内容，在 note 中写了

        # titles
        - a
        - b
        - c
        - d

    那么 warden 会把这个重复任务当作四个这个重复任务，每个 title 对应一行 note；
    完成一个派生出来的任务之后，这一行会被 mark 成完成（``- [x] a``）。

    在被要求的时候（``task 3 -d`` / plan 自动展开）在本地任务库产生派生任务，
    不同步回 OmniFocus —— **除了**它们关联的那条重复任务的 note。

    每次完成一个子任务之后更新原本的重复任务，而不是完成原本的重复任务；
    勾完之后自动放弃一整个重复任务。

这个模块只做纯逻辑：解析 note、判断资格、标题/合成 pk、count 范围、勾选后的
note 文本。所有 IO（本地任务库 / OmniFocus / plan）在 warden 那边。
"""
from __future__ import annotations

import re

#: ``source.kind``：派生任务。派生任务**没有** OmniFocus 关联（写回只写 note）。
DERIVED_KIND = "derived"

#: 本地字段名（都藏在 ``fields`` 里，这样任务表/筛选照常能用）。
F_DERIVED_OF = "derived_of"            # 父任务本地 id（可能没有：plan 直接派生）
F_DERIVED_LINE = "derived_line"        # note 里那一行的行号（0 起）
F_DERIVED_TEXT = "derived_text"        # 那一行的标题原文（回写时按它匹配）
F_DERIVED_PARENT_PK = "derived_parent_pk"   # 父任务的 OmniFocus primary key
F_DERIVED_PARENT_TITLE = "derived_parent_title"

DERIVED_FIELDS = (F_DERIVED_OF, F_DERIVED_LINE, F_DERIVED_TEXT,
                  F_DERIVED_PARENT_PK, F_DERIVED_PARENT_TITLE)

#: ``# titles`` 段头：允许 ``# Titles`` / ``#titles`` / 前后空格。
_HEADER_RE = re.compile(r"^\s*#+\s*titles\s*$", re.I)
#: markdown 标题（用来确定 ``# titles`` 段的结束）。
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


def parse(note):
    """note 文本 → ``{"items": [...], "open": n, "total": n}``。

    ``items`` 只包含 ``# titles`` 段里的 bullet：

        {"line": 3, "text": "a", "checked": False, "prefix": "- "}

    ``prefix`` 是原来的 bullet 前缀（含缩进），回写时按它拼回去，不改用户排版。
    没有 ``# titles`` 段 → ``{"items": [], "open": 0, "total": 0, "found": False}``。
    """
    text = str(note or "")
    lines = text.splitlines()
    items = []
    found = False
    in_section = False
    for index, line in enumerate(lines):
        if _HEADER_RE.match(line):
            found = True
            in_section = True
            continue
        if not in_section:
            continue
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


def next_open(note):
    """下一条未勾选 → ``{"line", "text", ...}``；没有 → ``None``。"""
    items = open_items(parse(note))
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
# 「每天重复 + 非以推迟日期为重复内容」→
#   * rule 是 ``FREQ=DAILY``（INTERVAL 缺省或 1）；
#   * 重复锚点不是 defer 日期。OmniFocus 的 ``anchorDateKey`` 在 JS 里是
#     ``[object Task.AnchorDateKey: DueDate]`` / ``... DeferDate]`` 这种字符串，
#     所以两种写法都认。

_FREQ_RE = re.compile(r"(?:^|;)FREQ=([A-Z]+)", re.I)
_INTERVAL_RE = re.compile(r"(?:^|;)INTERVAL=(\d+)", re.I)
_ANCHOR_RE = re.compile(r":\s*([A-Za-z]+)\s*\]")


def rule_freq(rule):
    """RRULE → (FREQ, INTERVAL)；解析不出来 → ``(None, None)``。"""
    text = str(rule or "").strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    freq = _FREQ_RE.search(text)
    interval = _INTERVAL_RE.search(text)
    if not freq:
        return None, None
    try:
        every = int(interval.group(1)) if interval else 1
    except (TypeError, ValueError):
        every = 1
    return freq.group(1).upper(), every


def anchor_key(repeat):
    """重复锚点 → ``"DueDate"`` / ``"DeferDate"`` / ``""``。"""
    rep = repeat if isinstance(repeat, dict) else {}
    raw = str(rep.get("anchor_date_key") or "").strip()
    if not raw:
        return ""
    match = _ANCHOR_RE.search(raw)
    if match:
        return match.group(1)
    return raw.rsplit(":", 1)[-1].strip(" ]") if ":" in raw else raw


def is_daily(repeat):
    """这条重复规则是不是「每天」（``FREQ=DAILY`` 且 INTERVAL=1）。"""
    rep = repeat if isinstance(repeat, dict) else {}
    freq, every = rule_freq(rep.get("rule"))
    return freq == "DAILY" and (every or 1) == 1


def is_defer_anchored(repeat):
    """重复是不是以「推迟日期」为锚（这种**不**展开成派生任务）。"""
    return "defer" in anchor_key(repeat).lower()


def eligible(repeat, note):
    """能不能按标题清单展开：每天重复 + 非 defer 锚 + note 里还有没勾的标题。"""
    if not is_daily(repeat) or is_defer_anchored(repeat):
        return False
    return bool(open_items(parse(note)))


def title_of(item):
    """派生任务的标题：``父任务：这一行``。"""
    return "%s：%s" % (item.get("parent_title") or "", item.get("text") or "")


def derived_title(parent_title, text):
    """``完成历史 Papers`` + ``2019-亚太-12`` → ``完成历史 Papers：2019-亚太-12``。"""
    parent = str(parent_title or "").strip()
    body = str(text or "").strip()
    if not parent:
        return body
    if not body:
        return parent
    return "%s：%s" % (parent, body)


_SEP = "#d"


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
