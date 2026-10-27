#!/usr/bin/env python3
"""omnifocus_tasks.py —— 用 osascript 读写 OmniFocus 任务（默认只读导出）。

默认只做一件事：**读**出全部任务，写到 stdout，筛选交给下游。
写操作走 `set` 子命令，按 primary key 精确改一条任务；默认只预览，`--yes` 才写。

用法
----
    python3 omnifocus_tasks.py                     # JSONL（默认，走 Omni Automation 快路径）
    python3 omnifocus_tasks.py --fields id,name,due
    python3 omnifocus_tasks.py --format tsv --header --fields name,project,due
    python3 omnifocus_tasks.py --engine applescript  # 强制走逐属性 AppleScript（慢，兼容用）
    python3 omnifocus_tasks.py --script            # 只打印要交给 osascript 的脚本
    python3 omnifocus_tasks.py --verbose           # 在 stderr 打进度（卡住时先看这个）

    # 例：没完成且 flagged 的任务名（排掉项目自身那条根任务）
    python3 omnifocus_tasks.py |
        jq -r 'select(.kind != "project_root" and .flagged and (.completed|not)) | .name'

    # 例：只看今天到期的，TSV 给 awk
    python3 omnifocus_tasks.py --format tsv --fields due,name |
        awk -F'\t' '$1 ~ /^2026-10-27/'

    # 写操作：先预览，加 --yes 才真写
    python3 omnifocus_tasks.py set --id <PK> --complete
    python3 omnifocus_tasks.py set --id <PK> --complete --yes
    python3 omnifocus_tasks.py set --id <PK> --drop --yes
    python3 omnifocus_tasks.py set --id <PK> --drop --all-occurrences --yes
    python3 omnifocus_tasks.py set --id <PK> --incomplete --yes
    python3 omnifocus_tasks.py set --id <PK> --add-tag 写作 --yes
    python3 omnifocus_tasks.py set --id <PK> --set-tags 工作,Deep --yes
    python3 omnifocus_tasks.py set --id <PK> --clear-tags --yes
    # 截止时间是「修饰」：可以和 --complete 一条命令一起给（warden 收尾就用这个）
    python3 omnifocus_tasks.py set --id <PK> --complete --due 2026-10-27T11:50 --yes
    python3 omnifocus_tasks.py set --id <PK> --due 2026-10-28 --yes
    python3 omnifocus_tasks.py set --id <PK> --clear-due --yes

    # 例：脚本本身也是纯文本，可以直接喂给 osascript
    python3 omnifocus_tasks.py --script | osascript

两种引擎
--------
- ``omnijs``（默认）：让 OmniFocus 在进程内跑一段 Omni Automation JS，一次
  AppleEvent 把全部任务变成 JSON。1380 条任务通常 1 秒级。
- ``applescript``：逐 task、逐属性地问 OmniFocus。兼容性最好，但几千条任务会
  很慢（几分钟），只在 omnijs 不可用时兜底。
- ``auto``（默认）：先试 omnijs，失败且不是权限问题就自动退回 applescript。
  退回时会在 stderr 明确说一声，不会悄悄变慢。

输出
----
- 默认 JSONL：一行一个 task，UTF-8，中文不转义，字段顺序见下表。
- `--format tsv`：制表符分隔，`--header` 打表头；值里的 \\ \t \n \r 用反斜杠转义，
  所以一行永远是一条记录。
- **stdout 只有数据**，诊断信息一律走 stderr。

字段（`--fields` 可选任意子集，顺序就是输出顺序）
--------------------------------------------------
    id                 text  任务 UUID（稳定，适合和别的东西 join）
    name               text  任务名
    kind               text  action / inbox / project_root
                             project_root 是 OmniFocus 里代表「项目自身」的根任务，
                             筛任务时一般要排掉：jq 'select(.kind != "project_root")'
    project            text  所属项目名；inbox 任务为空
    parent             text  父任务名；顶层任务为空
    in_inbox           bool  是否在收件箱
    tags               list  标签名列表（TSV 里用逗号连接）
    tag_ids            list  标签 primary key 列表，和 tags 一一对应
    flagged            bool  是否 flagged
    completed          bool  是否已完成
    dropped            bool  是否已丢弃
    due                date  截止时间
    defer              date  推迟到（开始）时间
    planned            date  计划时间
    completed_at       date  完成时间
    created            date  创建时间
    modified           date  最后修改时间
    estimated_minutes  int   预估分钟
    next               bool  是否是所在项目里的「下一个任务」
    blocked            bool  是否被前置任务阻塞
    children           int   直接子任务数
    note               text  备注（可能多行）

date 一律是**本地时间**的 ISO 8601（``YYYY-MM-DDTHH:MM:SS``，不带时区后缀）；
没设就是 ``null``（TSV 里是空串）。

OmniFocus 的 ``flattened tasks`` 里，每个项目还有一条代表项目自身的根任务。
本脚本**不偷偷丢掉**它，而是用 ``kind: "project_root"`` 标出来；只想看真任务就
``jq 'select(.kind != "project_root")'``。

退出码
------
    0  成功
    1  osascript / OmniFocus / 输出解析失败（含超时、没装 App）
    2  参数错
    3  macOS 自动化权限被拒（去「系统设置 → 隐私与安全性 → 自动化」勾 OmniFocus）
    4  set 预览完成，但没有 --yes，所以没写
    5  set 找不到这个 primary key
    6  set 的标签找不到 / 有歧义
    7  set 拒绝改 project_root（要加 --allow-project-root）

写操作的安全边界
----------------
- 只按 primary key 定位，不按名字模糊匹配；一次只改一条。
- 默认只预览；`--yes` 才动数据。预览也会去 OmniFocus 里核对任务和标签。
- 状态操作（完成/未完成/放弃/取消放弃）和标签操作分开跑，不混在一条命令里。
- 标签找不到就报错，不自动创建；标签可写叶子名（唯一时）、完整路径（Work/Deep）或标签 primary key。
- 默认拒绝改 project_root 任务（那等于改整个项目），要显式 `--allow-project-root`。
- 重复任务默认只放弃当前一次；要连整个系列一起放弃加 `--drop --all-occurrences`。
- 写操作只走 omnijs，不回退到慢速 AppleScript（避免行为不一致）；改完回读验证。

大小 / 权限备注
---------------
- 第一次跑会拉起 OmniFocus（AppleScript 的正常行为），这是**只读**访问。
- macOS 第一次会弹「允许控制 OmniFocus」的自动化权限框；在弹框上点允许之前，
  命令看起来就是「卡住」。用 `--verbose` 可以看到它停在等权限。
- 默认超时 120 秒；超时会明确报错而不是无限挂着。库特别大/回退到
  applescript 时可以 `--timeout 600`。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time

OSASCRIPT = os.environ.get("OSASCRIPT", "/usr/bin/osascript")
DEFAULT_TIMEOUT = 120.0

# AppleScript 传回来的分隔符（跟 find -print0 一个思路：用控制字符，普通文本里没有）
RECORD_SEP = "\x1e"  # RS：两条 task 之间
FIELD_SEP = "\x1f"   # US：一条 task 的字段之间
TAG_SEP = "\x1d"     # GS：一个 task 的多个标签之间

# 字段顺序 = AppleScript 里的拼装顺序 = 输出顺序。三处靠这张表对齐，别只改一边。
# (字段名, 类型, AppleScript 取值表达式)；t 是当前 task 的引用。
FIELD_SPECS = (
    ("id", "text", "id of t"),
    ("name", "text", "name of t"),
    ("kind", "text", "vKind"),
    ("project", "text", "vProject"),
    ("parent", "text", "vParent"),
    ("in_inbox", "bool", "in inbox of t"),
    ("tags", "tags", "vTags"),
    ("tag_ids", "tags", "vTagIds"),
    ("flagged", "bool", "flagged of t"),
    ("completed", "bool", "completed of t"),
    ("dropped", "bool", "dropped of t"),
    ("due", "date", "due date of t"),
    ("defer", "date", "defer date of t"),
    ("planned", "date", "planned date of t"),
    ("completed_at", "date", "completion date of t"),
    ("created", "date", "creation date of t"),
    ("modified", "date", "modification date of t"),
    ("estimated_minutes", "int", "estimated minutes of t"),
    ("next", "bool", "next of t"),
    ("blocked", "bool", "blocked of t"),
    ("children", "int", "number of tasks of t"),
    ("note", "text", "note of t"),
)
FIELDS = tuple(name for name, _, _ in FIELD_SPECS)
FIELD_KIND = {name: kind for name, kind, _ in FIELD_SPECS}

# 纯文本处理的 AppleScript handler，不含 OmniFocus 调用，可以单独跑测试。
APPLESCRIPT_HANDLERS = r'''
on esc(v)
	if v is missing value then return ""
	try
		set s to v as text
	on error
		return ""
	end try
	set s to my repl(s, "\\", "\\\\")
	set s to my repl(s, tab, "\\t")
	set s to my repl(s, linefeed, "\\n")
	set s to my repl(s, return, "\\r")
	set s to my repl(s, character id 29, "\\x1d")
	set s to my repl(s, character id 30, "\\x1e")
	set s to my repl(s, character id 31, "\\x1f")
	return s
end esc

on escList(lst, sep)
	if lst is {} then return ""
	set parts to {}
	repeat with v in lst
		set end of parts to my esc(contents of v)
	end repeat
	set {oldDelims, text item delimiters} to {text item delimiters, sep}
	set joined to parts as text
	set text item delimiters to oldDelims
	return joined
end escList

on boolText(b)
	if b is missing value then return ""
	if b then return "true"
	return "false"
end boolText

on intText(n)
	if n is missing value then return ""
	try
		return (n as integer) as text
	on error
		return ""
	end try
end intText

on iso(d)
	if d is missing value then return ""
	try
		set y to year of d
		set mo to month of d as integer
		set da to day of d
		set hh to hours of d
		set mi to minutes of d
		set ss to seconds of d
	on error
		return ""
	end try
	return (y as text) & "-" & my pad2(mo) & "-" & my pad2(da) & "T" & my pad2(hh) & ":" & my pad2(mi) & ":" & my pad2(ss)
end iso

on pad2(n)
	set s to (n as integer) as text
	if (length of s) is 1 then return "0" & s
	return s
end pad2

on repl(s, needle, replacement)
	if s does not contain needle then return s
	set {oldDelims, text item delimiters} to {text item delimiters, needle}
	set parts to text items of s
	set text item delimiters to replacement
	set s to parts as text
	set text item delimiters to oldDelims
	return s
end repl
'''

APPLESCRIPT_TEMPLATE = r'''-- 由 omnifocus_tasks.py 生成：只读导出 OmniFocus 全部任务
-- 记录分隔 character id 30 / 字段分隔 character id 31 / 标签分隔 character id 29
on run
	set fs to character id 31
	set tagSep to character id 29
	set recSep to character id 30
	tell application "OmniFocus"
		tell default document
			set taskList to flattened tasks
			set rows to {}
			repeat with t in taskList
				set vInbox to in inbox of t
				set vProjectObj to containing project of t
				set vProject to ""
				if vProjectObj is not missing value then
					set vProject to name of vProjectObj
				end if
				set vParentObj to parent task of t
				set vParent to ""
				if vParentObj is not missing value then
					set vParent to name of vParentObj
				end if
				set vKind to "action"
				if vInbox then
					set vKind to "inbox"
				else if (vParentObj is missing value) and (vProjectObj is not missing value) then
					set vKind to "project_root"
				end if
				set vTags to name of every tag of t
				set vTagIds to id of every tag of t
				set end of rows to ({{ROW}})
			end repeat
			set text item delimiters to recSep
			set outText to rows as text
			set text item delimiters to ""
			return outText
		end tell
	end tell
end run
{{HANDLERS}}
'''


# ---------------------------------------------------------------- 生成脚本


OMNIJS_SOURCE = r'''
(() => {
  const all = (typeof flattenedTasks !== "undefined")
    ? flattenedTasks
    : database.flattenedTasks;
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
  const idOf = t => {
    try { return t.id.primaryKey; } catch (e) { return null; }
  };
  const inInboxTree = t => {
    let x = t;
    while (x) {
      if (x.inInbox) return true;
      x = x.parent;
    }
    return false;
  };
  const isNext = t => {
    try {
      const p = t.containingProject;
      if (!p) return false;
      const n = p.nextTask;
      return !!n && idOf(n) === idOf(t);
    } catch (e) {
      return null;
    }
  };
  const isBlocked = t => {
    try {
      const s = t.taskStatus;
      try {
        const text = String(s).toLowerCase();
        if (text === "blocked" || text.indexOf("blocked") >= 0) return true;
      } catch (e) {}
      if (typeof Task !== "undefined") {
        if (s === Task.Status.Blocked) return true;
        if (s && typeof s.contains === "function" && s.contains(Task.Status.Blocked)) return true;
      }
      return false;
    } catch (e) {
      return null;
    }
  };
  const rows = [];
  for (const t of all) {
    const inbox = inInboxTree(t);
    const project = t.containingProject;
    const parent = t.parent;
    rows.push({
      id: idOf(t),
      name: t.name || "",
      kind: inbox ? "inbox" : (t.project ? "project_root" : "action"),
      project: project ? project.name : "",
      parent: parent ? parent.name : "",
      in_inbox: inbox,
      tags: Array.from(t.tags).map(g => g.name),
      tag_ids: Array.from(t.tags).map(g => idOf(g)),
      flagged: !!t.flagged,
      completed: !!t.completed,
      dropped: !!t.dropDate,
      due: iso(t.dueDate),
      defer: iso(t.deferDate),
      planned: iso(t.plannedDate),
      completed_at: iso(t.completionDate),
      created: iso(t.added),
      modified: iso(t.modified),
      estimated_minutes: (t.estimatedMinutes === undefined ? null : t.estimatedMinutes),
      next: isNext(t),
      blocked: isBlocked(t),
      children: t.children.length,
      note: t.note || ""
    });
  }
  return JSON.stringify({ tool: "omnifocus_tasks", version: 1, rows: rows });
})()
'''


SET_OMNIJS_TEMPLATE = r'''
(() => {
  const args = {{ARGS}};
  const reply = obj => JSON.stringify(Object.assign({ tool: "omnifocus_tasks_set", version: 1 }, obj));
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
  const tagPath = g => {
    const names = [];
    let x = g;
    while (x) {
      names.unshift(x.name);
      x = x.parent;
    }
    return names.join("/");
  };
  const kindOf = t => {
    let x = t;
    let inbox = false;
    while (x) {
      if (x.inInbox) { inbox = true; break; }
      x = x.parent;
    }
    if (inbox) return "inbox";
    try { if (t.project) return "project_root"; } catch (e) {}
    return "action";
  };
  const snapshot = t => ({
    id: idOf(t),
    name: t.name || "",
    kind: kindOf(t),
    completed: !!t.completed,
    dropped: !!t.dropDate,
    completion_date: iso(t.completionDate),
    drop_date: iso(t.dropDate),
    due: iso(t.dueDate),
    defer: iso(t.deferDate),
    tags: Array.from(t.tags).map(g => g.name),
    tag_paths: Array.from(t.tags).map(tagPath)
  });
  const tagInfo = list => list.map(g => ({ id: idOf(g), name: g.name, path: tagPath(g) }));
  try {
    if (typeof Task === "undefined" || typeof Task.byIdentifier !== "function") {
      return reply({ ok: false, error: "omnijs_unavailable", message: "当前 OmniFocus 不支持 Task.byIdentifier" });
    }
    const task = Task.byIdentifier(args.id);
    if (!task) {
      return reply({ ok: false, error: "not_found", message: "找不到任务：" + args.id });
    }
    const kind = kindOf(task);
    if (kind === "project_root" && !args.allowProjectRoot) {
      return reply({
        ok: false,
        error: "project_root_refused",
        message: "这是 project_root 任务；确认要改整个项目请加 --allow-project-root"
      });
    }
    const hasState = !!(args.complete || args.incomplete || args.drop || args.undrop);
    const hasTags = !!(
      (args.tags.set && args.tags.set.length) ||
      args.tags.clear ||
      (args.tags.add && args.tags.add.length) ||
      (args.tags.remove && args.tags.remove.length)
    );
    const hasDue = !!(args.due || args.clearDue);
    if (hasState && hasTags) {
      return reply({ ok: false, error: "bad_request", message: "状态操作和标签操作不要混在一条命令里" });
    }
    if (hasTags && hasDue) {
      return reply({ ok: false, error: "bad_request", message: "标签操作和截止时间操作不要混在一条命令里" });
    }
    const before = snapshot(task);
    const allTags = (typeof flattenedTags !== "undefined") ? flattenedTags : database.flattenedTags;
    const resolveList = names => {
      const tags = [];
      const problems = [];
      const seenNames = [];
      for (const name of names) {
        if (seenNames.indexOf(name) >= 0) continue;
        seenNames.push(name);
        const matches = [];
        for (const g of allTags) {
          if (g.name === name || tagPath(g) === name || idOf(g) === name) matches.push(g);
        }
        const seen = {};
        const unique = [];
        for (const g of matches) {
          const key = idOf(g) || tagPath(g);
          if (!seen[key]) { seen[key] = 1; unique.push(g); }
        }
        if (unique.length === 0) problems.push({ kind: "not_found", name: name });
        else if (unique.length > 1) problems.push({ kind: "ambiguous", name: name, paths: unique.map(tagPath) });
        else tags.push(unique[0]);
      }
      return { tags: tags, problems: problems };
    };
    const addRes = resolveList(args.tags.add || []);
    const removeRes = resolveList(args.tags.remove || []);
    const setRes = resolveList(args.tags.set || []);
    const problems = [].concat(addRes.problems, removeRes.problems, setRes.problems);
    if (problems.length) {
      return reply({ ok: false, error: "tag_problem", message: "标签找不到或有歧义", problems: problems });
    }
    let afterTask = task;
    if (!args.dryRun) {
      if (args.complete) {
        afterTask = (args.at ? task.markComplete(new Date(args.at)) : task.markComplete()) || task;
      } else if (args.incomplete) {
        task.markIncomplete();
      } else if (args.drop) {
        afterTask = (args.at ? task.drop(!!args.dropAll, new Date(args.at)) : task.drop(!!args.dropAll)) || task;
      } else if (args.undrop) {
        task.dropDate = null;
      } else if (args.tags.set) {
        task.clearTags();
        if (setRes.tags.length) task.addTags(setRes.tags);
      } else if (args.tags.clear) {
        task.clearTags();
      } else if (hasTags) {
        if (removeRes.tags.length) task.removeTags(removeRes.tags);
        if (addRes.tags.length) task.addTags(addRes.tags);
      }
      // 截止时间是「修饰」，不独占一次命令：--complete --due … 一条就跑完。
      if (hasDue) {
        task.dueDate = args.due ? new Date(args.due) : null;
        afterTask = task;
      }
    }
    const repeatCloneId = (idOf(afterTask) !== idOf(task)) ? idOf(afterTask) : null;
    const after = args.dryRun ? before : snapshot(afterTask);
    const changed = [];
    for (const key of ["completed", "dropped", "completion_date", "drop_date", "due", "tags"]) {
      if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) changed.push(key);
    }
    return reply({
      ok: true,
      dry_run: !!args.dryRun,
      ops: args.ops || [],
      id: args.id,
      before: before,
      after: after,
      changed: changed,
      repeat_clone_id: repeatCloneId,
      resolved_tags: {
        add: tagInfo(addRes.tags),
        remove: tagInfo(removeRes.tags),
        set: tagInfo(setRes.tags)
      }
    });
  } catch (e) {
    return reply({ ok: false, error: "exception", message: String(e) });
  }
})()
'''


def _applescript_string(text):
    """把一个 Python 字符串变成 AppleScript 字符串字面量。"""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


def build_omnijs_script(js_source=None, chunk_size=600, header=None):
    """Omni Automation 快路径：把 JS 通过 evaluate javascript 交给 OmniFocus。"""
    js = (OMNIJS_SOURCE if js_source is None else js_source).strip("\n")
    chunks = [js[i : i + chunk_size] for i in range(0, len(js), chunk_size)] or [""]
    lines = [header or "-- 由 omnifocus_tasks.py 生成：Omni Automation 快路径（只读）"]
    lines.append("set js to " + _applescript_string(chunks[0]))
    for chunk in chunks[1:]:
        lines.append("set js to js & " + _applescript_string(chunk))
    lines.append('tell application "OmniFocus"')
    lines.append("\treturn evaluate javascript js")
    lines.append("end tell")
    return "\n".join(lines) + "\n"


def build_set_script(payload, chunk_size=600):
    """写操作脚本：把参数 JSON 嵌进 Omni Automation JS。"""
    js = SET_OMNIJS_TEMPLATE.replace(
        "{{ARGS}}",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    header = "-- 由 omnifocus_tasks.py 生成：Omni Automation 写操作（%s）" % (
        "dry-run" if payload.get("dryRun") else "apply"
    )
    return build_omnijs_script(js, chunk_size=chunk_size, header=header)


def _row_expression():
    """按 FIELD_SPECS 拼出一行：字段之间用 fs 连接，每个值都经过对应 handler。"""
    parts = []
    for _name, kind, expr in FIELD_SPECS:
        if kind == "text":
            parts.append("my esc(%s)" % expr)
        elif kind == "bool":
            parts.append("my boolText(%s)" % expr)
        elif kind == "int":
            parts.append("my intText(%s)" % expr)
        elif kind == "date":
            parts.append("my iso(%s)" % expr)
        elif kind == "tags":
            parts.append("my escList(%s, tagSep)" % expr)
        else:  # pragma: no cover - FIELD_SPECS 是写死的
            raise AssertionError("未知字段类型：%r" % (kind,))
    return " & fs & ".join(parts)


def build_applescript_script():
    """逐属性 AppleScript 兜底路径，返回要交给 osascript 的完整源码。"""
    script = APPLESCRIPT_TEMPLATE.replace("{{ROW}}", _row_expression())
    script = script.replace("{{HANDLERS}}", APPLESCRIPT_HANDLERS.strip("\n"))
    return script if script.endswith("\n") else script + "\n"


ENGINES = ("auto", "omnijs", "applescript")


def build_script(engine="omnijs"):
    """返回指定引擎的 osascript 源码。纯函数，方便 --script / 测试。"""
    if engine == "applescript":
        return build_applescript_script()
    if engine == "omnijs":
        return build_omnijs_script()
    raise ValueError("未知引擎：%r" % (engine,))


# ---------------------------------------------------------------- 跑 osascript


class OsascriptError(RuntimeError):
    """osascript 失败；code 是给进程用的退出码，kind 给自动回退逻辑判断。"""

    def __init__(self, message, code=1, kind="error"):
        super().__init__(message)
        self.code = code
        self.kind = kind


_PERMISSION_HINTS = (
    "not allowed to send apple events",
    "not authorized to send apple events",
    "operation not permitted",
    "权限违例",
    "-1743",
    "-10004",
)

_MISSING_APP_HINTS = (
    "can't get application",
    "application isn't running",
    "application can't be found",
    "-1728",
    "-10814",
    "-600",
)


def _looks_like_permission_denied(stderr):
    lowered = (stderr or "").lower()
    return any(hint in lowered for hint in _PERMISSION_HINTS)


def _looks_like_missing_app(stderr):
    lowered = (stderr or "").lower()
    return any(hint in lowered for hint in _MISSING_APP_HINTS)


def run_osascript(script, timeout=DEFAULT_TIMEOUT, osascript=OSASCRIPT):
    """把脚本从 stdin 喂给 osascript，返回 stdout 文本（UTF-8 解码）。"""
    executable = osascript if os.path.isfile(osascript) else shutil.which(osascript)
    if not executable:
        raise OsascriptError("找不到 osascript：%s" % osascript, 1, "osascript")
    try:
        proc = subprocess.run(
            [executable],
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise OsascriptError(
            "osascript %.0f 秒没返回（OmniFocus 卡住 / 库太大 / 弹出的权限框没点？），"
            "可以用 --timeout 加长，或先 --verbose 看卡在哪" % timeout,
            1,
            "timeout",
        )
    except OSError as e:
        raise OsascriptError("起不来 osascript：%s" % e, 1, "osascript")

    stderr = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        if _looks_like_permission_denied(stderr):
            raise OsascriptError(
                "macOS 拒绝了自动化权限。\n"
                "  系统设置 → 隐私与安全性 → 自动化 → 勾上（终端 / iTerm / 宿主程序）里的 OmniFocus，然后重跑。\n"
                "  原始错误：%s" % (stderr or "(没有 stderr)"),
                3,
                "permission",
            )
        if _looks_like_missing_app(stderr):
            raise OsascriptError(
                "找不到 / 起不来 OmniFocus（没装？改名了？）。\n"
                "  原始错误：%s" % (stderr or "(没有 stderr)"),
                1,
                "missing_app",
            )
        raise OsascriptError(
            "osascript 退出码 %d：%s" % (proc.returncode, stderr or "(没有 stderr)"),
            1,
            "osascript",
        )
    if stderr:
        # osascript 有时会往 stderr 打警告；转发出去，别污染 stdout 的数据
        print("omnifocus_tasks: [osascript] " + stderr, file=sys.stderr)
    return proc.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- 解析 / 转类型


_UNESCAPE_SIMPLE = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def unescape(text):
    """还原 AppleScript ``esc`` 做的转义（包括 \\x1d / \\x1e / \\x1f）。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _UNESCAPE_SIMPLE:
                out.append(_UNESCAPE_SIMPLE[nxt])
                i += 2
                continue
            if text.startswith("\\x1d", i):
                out.append(TAG_SEP)
                i += 4
                continue
            if text.startswith("\\x1e", i):
                out.append(RECORD_SEP)
                i += 4
                continue
            if text.startswith("\\x1f", i):
                out.append(FIELD_SEP)
                i += 4
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def coerce(kind, raw):
    """把 AppleScript 的文本值转成 Python 值；空串表示「没有值」。

    只接受自己生成的 AppleScript 该产出的形状；对不上就抛 ValueError，
    免得把脏数据悄悄当成 False / 0 吐给下游。
    """
    if kind == "bool":
        if raw == "":
            return None
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise ValueError("非法布尔值 %r" % raw)
    if kind == "int":
        if raw == "":
            return None
        return int(raw)
    if kind == "date":
        return raw or None
    if kind == "tags":
        # 调用方给的是「未 unescape 的原始 cell」：先按 TAG_SEP 切开再逐段 unescape，
        # 否则标签名里原本的 TAG_SEP 会在 unescape 之后变成假的分隔符。
        return [unescape(part) for part in raw.split(TAG_SEP)] if raw else []
    return raw


def decode_cell(kind, cell):
    """一个 wire cell → Python 值。tags 特殊：分隔符在转义之前切。"""
    if kind == "tags":
        return coerce(kind, cell)
    return coerce(kind, unescape(cell))


def parse_rows(text):
    """把 osascript 的原始输出解析成 list[dict]，字段顺序与 FIELD_SPECS 一致。"""
    text = text.rstrip("\r\n")
    if not text:
        return []
    rows = []
    for index, record in enumerate(text.split(RECORD_SEP), 1):
        if record == "":
            continue
        cells = record.split(FIELD_SEP)
        if len(cells) != len(FIELD_SPECS):
            raise ValueError(
                "第 %d 条记录字段数 %d，应该是 %d —— AppleScript 输出被污染了？"
                % (index, len(cells), len(FIELD_SPECS))
            )
        row = {}
        for (name, kind, _), cell in zip(FIELD_SPECS, cells):
            try:
                row[name] = decode_cell(kind, cell)
            except ValueError as e:
                raise ValueError(
                    "第 %d 条记录的 %s 字段有问题：%s" % (index, name, e)
                ) from None
        rows.append(row)
    return rows


def _coerce_json(kind, value):
    """Omni Automation JSON 值 → Python 值；形状不对就报错，别静默改类型。"""
    if kind == "bool":
        if value is None or isinstance(value, bool):
            return value
        raise ValueError("期望 bool/null，收到 %r" % (value,))
    if kind == "int":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("期望 int/null，收到 %r" % (value,))
        return value
    if kind == "date":
        if value is None or isinstance(value, str):
            return value
        raise ValueError("期望日期字符串/null，收到 %r" % (value,))
    if kind == "tags":
        if value is None:
            return []
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        raise ValueError("期望字符串数组，收到 %r" % (value,))
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise ValueError("期望字符串，收到 %r" % (value,))


def parse_json_rows(text):
    """解析 Omni Automation 快路径返回的 JSON 信封。

    要求是 ``{"tool": "omnifocus_tasks", "version": 1, "rows": [...]}``：
    这样 ``evaluate javascript`` 万一只返回了空值/别的东西，我们能立刻发现，
    而不是把「零任务」当成成功。
    """
    text = text.strip()
    if not text:
        raise ValueError("Omni Automation 返回了空值（evaluate javascript 没拿到返回值？）")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("Omni Automation 返回的不是 JSON：%s" % e) from None
    if not isinstance(data, dict):
        raise ValueError("Omni Automation 应返回对象信封，实际是 %s" % type(data).__name__)
    if data.get("tool") != "omnifocus_tasks":
        raise ValueError("Omni Automation 信封的 tool 不对：%r" % (data.get("tool"),))
    items = data.get("rows")
    if not isinstance(items, list):
        raise ValueError("Omni Automation 信封里没有 rows 数组")
    rows = []
    for index, obj in enumerate(items, 1):
        if not isinstance(obj, dict):
            raise ValueError("第 %d 条不是对象：%r" % (index, obj))
        row = {}
        for name, kind, _ in FIELD_SPECS:
            try:
                row[name] = _coerce_json(kind, obj.get(name))
            except ValueError as e:
                raise ValueError("第 %d 条记录的 %s 字段有问题：%s" % (index, name, e)) from None
        if row["kind"] not in ("action", "inbox", "project_root"):
            raise ValueError("第 %d 条记录的 kind 非法：%r" % (index, row["kind"]))
        rows.append(row)
    return rows


def parse_set_result(text):
    """解析写操作返回的 JSON 信封。"""
    text = text.strip()
    if not text:
        raise ValueError("Omni Automation 返回了空值（evaluate javascript 没拿到返回值？）")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("Omni Automation 返回的不是 JSON：%s" % e) from None
    if not isinstance(data, dict):
        raise ValueError("写操作应返回对象信封，实际是 %s" % type(data).__name__)
    if data.get("tool") != "omnifocus_tasks_set":
        raise ValueError("写操作信封的 tool 不对：%r" % (data.get("tool"),))
    if not isinstance(data.get("ok"), bool):
        raise ValueError("写操作信封缺少 ok 布尔字段")
    return data


# ---------------------------------------------------------------- 输出


def parse_field_list(spec):
    """解析 --fields；支持 all / * / 逗号分隔子集，重复的自动去掉。"""
    if not spec or spec.strip() in ("", "*") or spec.strip().lower() == "all":
        return list(FIELDS)
    names = [part.strip() for part in spec.split(",") if part.strip()]
    unknown = [name for name in names if name not in FIELD_KIND]
    if unknown:
        raise ValueError(
            "未知字段：%s\n可选：%s" % (", ".join(unknown), ", ".join(FIELDS))
        )
    seen = set()
    picked = []
    for name in names:
        if name not in seen:
            seen.add(name)
            picked.append(name)
    return picked


def _tsv_cell(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        value = ",".join(str(item) for item in value)
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def write_rows(rows, fields, fmt="jsonl", header=False):
    """把行写进 stdout；只做序列化，不做任何筛选。"""
    out = sys.stdout
    if fmt == "tsv":
        if header:
            out.write("\t".join(fields) + "\n")
        for row in rows:
            out.write("\t".join(_tsv_cell(row.get(name)) for name in fields) + "\n")
        return
    for row in rows:
        obj = {name: row.get(name) for name in fields}
        out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------- CLI


def build_parser():
    parser = argparse.ArgumentParser(
        prog="omnifocus_tasks.py",
        description="用 osascript 只读导出 OmniFocus 全部任务（一行一条），筛选交给下游。",
        epilog="字段：%s\n\n写操作：omnifocus_tasks.py set --help\n"
        "退出码：0 成功 / 1 失败 / 2 参数错 / 3 自动化权限被拒。"
        % ", ".join(FIELDS),
    )
    parser.add_argument(
        "--format",
        choices=("jsonl", "tsv"),
        default="jsonl",
        help="输出格式（默认 jsonl）",
    )
    parser.add_argument(
        "--fields",
        default="all",
        metavar="LIST",
        help="逗号分隔的字段名，或 all（默认）。顺序即输出顺序。",
    )
    parser.add_argument(
        "--header",
        action="store_true",
        help="TSV 模式先打一行字段名（JSONL 忽略）",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="auto",
        help="读取引擎：auto（默认，omnijs 失败自动回退）/ omnijs（快）/ applescript（慢但兼容）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="osascript 超时秒数（默认 %.0f）" % DEFAULT_TIMEOUT,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="在 stderr 打进度/回退信息（stderr 是终端时默认就会打）",
    )
    parser.add_argument(
        "--script",
        action="store_true",
        help="只把所选引擎的 osascript 源码打到 stdout，不执行（可接 | osascript）",
    )
    parser.add_argument(
        "--osascript",
        default=OSASCRIPT,
        metavar="PATH",
        help="osascript 路径（默认 %(default)s）",
    )
    return parser


def _note(args, message):
    """进度只走 stderr；stderr 是终端时默认打，避免看起来像卡死。"""
    if args.verbose or sys.stderr.isatty():
        print("omnifocus_tasks: " + message, file=sys.stderr, flush=True)


def main_list(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        fields = parse_field_list(args.fields)
    except ValueError as e:
        parser.error(str(e))
    if args.timeout <= 0:
        parser.error("--timeout 必须是正数")

    engines = ["omnijs", "applescript"] if args.engine == "auto" else [args.engine]

    if args.script:
        sys.stdout.write(build_script(engines[0]))
        return 0

    if sys.platform != "darwin":
        print("omnifocus_tasks: 只能在 macOS 上跑（需要 osascript + OmniFocus）", file=sys.stderr)
        return 1

    started = time.monotonic()
    for index, engine in enumerate(engines):
        script = build_script(engine)
        _note(args, "正在用 %s 读取 OmniFocus（首次会弹自动化权限框，等一下）…" % engine)
        try:
            text = run_osascript(script, timeout=args.timeout, osascript=args.osascript)
        except OsascriptError as e:
            if engine == "omnijs" and index + 1 < len(engines) and e.kind == "osascript":
                print(
                    "omnifocus_tasks: omnijs 不可用（%s），退回 applescript；这条路会慢很多"
                    % e,
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print("omnifocus_tasks: %s" % e, file=sys.stderr)
            return e.code

        try:
            rows = parse_json_rows(text) if engine == "omnijs" else parse_rows(text)
        except ValueError as e:
            if engine == "omnijs" and index + 1 < len(engines):
                print(
                    "omnifocus_tasks: omnijs 返回的数据无法解析（%s），退回 applescript"
                    % e,
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print("omnifocus_tasks: %s" % e, file=sys.stderr)
            return 1

        elapsed = time.monotonic() - started
        _note(args, "读到 %d 条任务，用时 %.1fs（%s）" % (len(rows), elapsed, engine))
        write_rows(rows, fields, fmt=args.format, header=args.header)
        return 0

    print("omnifocus_tasks: 所有引擎都失败了", file=sys.stderr)
    return 1


def build_set_parser():
    parser = argparse.ArgumentParser(
        prog="omnifocus_tasks.py set",
        description="按 primary key 修改一个 OmniFocus 任务。默认只预览，加 --yes 才真写。",
        epilog=(
            "状态操作和标签操作分两次跑，不要混在一条命令里；"
            "--due 是修饰，可以跟 --complete/--drop 一起给。\n"
            "标签名可以写叶子名（唯一时）、完整路径（如 Work/Deep）或标签 primary key。\n"
            "退出码：0 成功 / 1 失败 / 2 参数错 / 3 权限被拒 / 4 预览未写 / "
            "5 任务不存在 / 6 标签问题 / 7 拒绝改 project_root。"
        ),
    )
    parser.add_argument(
        "--id",
        required=True,
        metavar="PK",
        help="任务 primary key（list 输出里的 id 字段）",
    )
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--complete", action="store_true", help="标记完成")
    state.add_argument("--incomplete", action="store_true", help="标记未完成")
    state.add_argument("--drop", action="store_true", help="放弃任务")
    state.add_argument("--undrop", action="store_true", help="取消放弃")
    parser.add_argument(
        "--all-occurrences",
        action="store_true",
        help="配合 --drop：连重复任务的整个系列一起放弃",
    )
    parser.add_argument(
        "--at",
        metavar="ISO",
        help="完成/放弃时间（ISO 8601，例如 2026-10-27T09:00:00；默认现在）",
    )
    due = parser.add_mutually_exclusive_group()
    due.add_argument(
        "--due",
        metavar="ISO",
        help="改截止时间（ISO 8601 或 YYYY-MM-DD；可以和 --complete 一起给）",
    )
    due.add_argument(
        "--clear-due",
        action="store_true",
        help="清掉截止时间",
    )
    parser.add_argument(
        "--add-tag",
        action="append",
        default=[],
        metavar="NAME",
        help="加一个标签，可重复",
    )
    parser.add_argument(
        "--remove-tag",
        action="append",
        default=[],
        metavar="NAME",
        help="删一个标签，可重复",
    )
    parser.add_argument(
        "--set-tags",
        action="append",
        default=[],
        metavar="A,B",
        help="用这些标签替换全部标签；可重复，也可逗号分隔",
    )
    parser.add_argument(
        "--clear-tags",
        action="store_true",
        help="清空全部标签",
    )
    parser.add_argument(
        "--allow-project-root",
        action="store_true",
        help="允许改 project_root 任务（等于改整个项目，危险）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="真的写；不加只预览并以退出码 4 结束",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="osascript 超时秒数（默认 %.0f）" % DEFAULT_TIMEOUT,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="在 stderr 打进度",
    )
    parser.add_argument(
        "--script",
        action="store_true",
        help="只把 osascript 源码打到 stdout，不执行",
    )
    parser.add_argument(
        "--osascript",
        default=OSASCRIPT,
        metavar="PATH",
        help="osascript 路径（默认 %(default)s）",
    )
    return parser


def _normalize_at(value):
    """把 --at 规整成 JS new Date() 能按本地时间解析的 ISO 字符串。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("--at 不能为空")
    if len(text) == 10:  # YYYY-MM-DD → 本地零点
        text = text + "T00:00:00"
    probe = text[:-1] + "+00:00" if text.upper().endswith("Z") else text
    try:
        datetime.datetime.fromisoformat(probe)
    except ValueError:
        raise ValueError(
            "--at 要 ISO 8601，例如 2026-10-27T09:00:00 或 2026-10-27T09:00:00+08:00"
        ) from None
    return text


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def build_set_payload(args):
    """把 set 命令的参数校验并变成 JS 侧的 args 对象。"""
    state_ops = [
        name
        for name, flag in (
            ("complete", args.complete),
            ("incomplete", args.incomplete),
            ("drop", args.drop),
            ("undrop", args.undrop),
        )
        if flag
    ]
    if len(state_ops) > 1:
        raise ValueError("状态操作只能选一个")

    add_tags = _dedupe([t.strip() for t in args.add_tag if t.strip()])
    remove_tags = _dedupe([t.strip() for t in args.remove_tag if t.strip()])
    set_tags = []
    for raw in args.set_tags:
        set_tags.extend(part.strip() for part in raw.split(",") if part.strip())
    set_tags = _dedupe(set_tags)

    if args.set_tags and not set_tags:
        raise ValueError("--set-tags 是空的；想清空标签请用 --clear-tags")
    overlap = set(add_tags) & set(remove_tags)
    if overlap:
        raise ValueError("同一个标签不能既加又删：%s" % ", ".join(sorted(overlap)))

    tag_modes = sum(
        [
            bool(set_tags),
            bool(args.clear_tags),
            bool(add_tags or remove_tags),
        ]
    )
    if tag_modes > 1:
        raise ValueError("--set-tags / --clear-tags / --add-tag|--remove-tag 三选一")

    if state_ops and tag_modes:
        raise ValueError("状态操作和标签操作不要混在一条命令里，分两次跑")
    if tag_modes and (args.due or args.clear_due):
        raise ValueError("标签操作和截止时间操作不要混在一条命令里，分两次跑")
    if not state_ops and tag_modes == 0 and not (args.due or args.clear_due):
        raise ValueError(
            "至少给一个操作：--complete / --incomplete / --drop / --undrop，"
            "或 --add-tag / --remove-tag / --set-tags / --clear-tags，"
            "或 --due / --clear-due"
        )

    if args.all_occurrences and not args.drop:
        raise ValueError("--all-occurrences 只能配合 --drop")

    at = None
    if args.at:
        if not (args.complete or args.drop):
            raise ValueError("--at 只能配合 --complete 或 --drop")
        at = _normalize_at(args.at)

    due = _normalize_at(args.due) if args.due else None

    ops = []
    if state_ops:
        op = state_ops[0]
        if op == "drop" and args.all_occurrences:
            op = "drop-all"
        ops.append(op + (("@" + at) if at else ""))
    elif set_tags:
        ops.append("set-tags:" + ",".join(set_tags))
    elif args.clear_tags:
        ops.append("clear-tags")
    elif add_tags or remove_tags:
        ops.extend("remove-tag:" + tag for tag in remove_tags)
        ops.extend("add-tag:" + tag for tag in add_tags)
    if due:
        ops.append("due:" + due)
    elif args.clear_due:
        ops.append("clear-due")

    return {
        "id": args.id,
        "complete": bool(args.complete),
        "incomplete": bool(args.incomplete),
        "drop": bool(args.drop),
        "undrop": bool(args.undrop),
        "dropAll": bool(args.all_occurrences),
        "at": at,
        "due": due,
        "clearDue": bool(args.clear_due),
        "tags": {
            "add": add_tags,
            "remove": remove_tags,
            "set": set_tags if set_tags else None,
            "clear": bool(args.clear_tags),
        },
        "dryRun": not args.yes,
        "allowProjectRoot": bool(args.allow_project_root),
        "ops": ops,
    }


def main_set(argv=None):
    parser = build_set_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须是正数")
    try:
        payload = build_set_payload(args)
    except ValueError as e:
        parser.error(str(e))

    script = build_set_script(payload)
    if args.script:
        sys.stdout.write(script)
        return 0

    if sys.platform != "darwin":
        print("omnifocus_tasks: 只能在 macOS 上跑（需要 osascript + OmniFocus）", file=sys.stderr)
        return 1

    _note(
        args,
        "正在用 omnijs %s任务 %s…"
        % ("预览" if payload["dryRun"] else "修改", args.id),
    )
    try:
        text = run_osascript(script, timeout=args.timeout, osascript=args.osascript)
    except OsascriptError as e:
        print("omnifocus_tasks: %s" % e, file=sys.stderr)
        return e.code

    try:
        result = parse_set_result(text)
    except ValueError as e:
        print("omnifocus_tasks: %s" % e, file=sys.stderr)
        return 1

    if not result.get("ok"):
        error = result.get("error")
        message = result.get("message") or error or "写操作失败"
        print("omnifocus_tasks: %s" % message, file=sys.stderr)
        problems = result.get("problems")
        if problems:
            print(json.dumps(problems, ensure_ascii=False), file=sys.stderr)
        return {
            "not_found": 5,
            "tag_problem": 6,
            "project_root_refused": 7,
        }.get(error, 1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    name = (result.get("before") or {}).get("name") or args.id
    if result.get("dry_run"):
        print(
            "omnifocus_tasks: [dry-run] 将对「%s」执行：%s；加 --yes 才写"
            % (name, ", ".join(payload["ops"])),
            file=sys.stderr,
        )
        return 4
    changed = result.get("changed") or []
    print(
        "omnifocus_tasks: 已改「%s」：%s" % (name, ", ".join(changed) if changed else "(无变化)"),
        file=sys.stderr,
    )
    return 0


def main(argv=None):
    """默认 list；`set` 子命令走写操作。"""
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "set":
        return main_set(argv[1:])
    if argv and argv[0] == "list":
        return main_list(argv[1:])
    return main_list(argv)


def _default_sigpipe():
    """像 cat / grep 一样对待 `| head`：下游关管就安静地死掉，别打 traceback。"""
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


if __name__ == "__main__":
    _default_sigpipe()
    sys.exit(main())
