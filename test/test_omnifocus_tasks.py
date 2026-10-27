#!/usr/bin/env python3
"""omnifocus_tasks.py 回归测试：读/写两条路径的脚本生成、解析、参数校验、退出码。

    python3 test/test_omnifocus_tasks.py     # 全绿退出 0，有失败退出 1

不碰真的 OmniFocus：要跑 osascript 的地方用假的可执行文件顶替；
OmniJS 逻辑用 Node + mock 对象跑；只有纯文本 handler 会真的调 osascript。
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import omnifocus_tasks as m  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_raises(label, fn, exc=ValueError):
    try:
        fn()
    except exc:
        check(label, True, True)
        return
    except Exception as e:  # noqa: BLE001
        check("%s（却抛了别的 %r）" % (label, e), True, False)
        return
    check(label, True, False)


def wire_escape(text):
    """AppleScript esc 的 Python 版，用来造测试输入。"""
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace(m.TAG_SEP, "\\x1d")
        .replace(m.RECORD_SEP, "\\x1e")
        .replace(m.FIELD_SEP, "\\x1f")
    )


def make_wire(rows):
    """把 dict 列表编成 osascript 风格的分隔文本。"""
    records = []
    for row in rows:
        cells = []
        for name, kind, _ in m.FIELD_SPECS:
            value = row.get(name)
            if value is None:
                cell = ""
            elif kind == "tags":
                cell = m.TAG_SEP.join(wire_escape(str(v)) for v in value)
            elif kind == "bool":
                cell = wire_escape("true" if value else "false")
            else:
                cell = wire_escape(str(value))
            cells.append(cell)
        records.append(m.FIELD_SEP.join(cells))
    return m.RECORD_SEP.join(records) + "\n"


def write_exe(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------- 字段 / 脚本

print("== parse_field_list ==")
check("all → 全部字段", m.parse_field_list("all"), list(m.FIELDS))
check("* → 全部字段", m.parse_field_list("*"), list(m.FIELDS))
check("逗号子集去重、保序", m.parse_field_list("name,due,name"), ["name", "due"])
check_raises("未知字段报 ValueError", lambda: m.parse_field_list("name,bogus"))

print("== build_script（两个引擎）==")
ascript = m.build_script("applescript")
check("applescript：读 flattened tasks", "flattened tasks" in ascript, True)
check(
    "applescript：只读：没有 mark/delete/add",
    any(w in ascript for w in ("mark complete", "delete ", "add ")),
    False,
)
check("applescript：字段分隔符数量 = 字段数-1", ascript.count(" & fs & "), len(m.FIELDS) - 1)
check("applescript：会标出 project_root", 'set vKind to "project_root"' in ascript, True)
check("applescript：会标出 inbox", 'set vKind to "inbox"' in ascript, True)
for name, kind, expr in m.FIELD_SPECS:
    label = "applescript：%s 的表达式出现" % name
    check(label, expr in ascript, True)

ojs = m.build_script("omnijs")
check("omnijs：调 evaluate javascript", "evaluate javascript js" in ojs, True)
check("omnijs：用 flattenedTasks", "flattenedTasks" in ojs, True)
check("omnijs：JSON.stringify + 信封", 'JSON.stringify({ tool: "omnifocus_tasks"' in m.OMNIJS_SOURCE, True)
check("omnijs：标出 project_root", '"project_root"' in m.OMNIJS_SOURCE, True)
check("omnijs：标出 inbox", '"inbox"' in m.OMNIJS_SOURCE, True)
check("omnijs：含全部字段名", all((name + ":") in m.OMNIJS_SOURCE for name in m.FIELDS), True)
check_raises("未知引擎报错", lambda: m.build_script("bogus"))

if sys.platform == "darwin" and shutil.which("osacompile"):
    with tempfile.TemporaryDirectory() as tmp:
        for engine, source in (("applescript", ascript), ("omnijs", ojs)):
            src = os.path.join(tmp, "%s.applescript" % engine)
            dst = os.path.join(tmp, "%s.scpt" % engine)
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(source)
            proc = subprocess.run(
                ["osacompile", "-o", dst, src], capture_output=True, text=True
            )
            check("osacompile 能编译 %s 脚本" % engine, (proc.returncode, proc.stderr.strip()), (0, ""))
else:
    print("skip osacompile（不是 macOS 或没装 osacompile）")

print("== OmniJS 源码完整性 ==")
check("JS 是 IIFE", m.OMNIJS_SOURCE.strip().startswith("(()") and m.OMNIJS_SOURCE.strip().endswith("()"), True)
check("JS 里 taskStatus 对比有 try 兜底", "Task.Status.Blocked" in m.OMNIJS_SOURCE, True)
check("JS 里 next 用 Project.nextTask", "nextTask" in m.OMNIJS_SOURCE, True)
check("JS 里没有中文硬编码字段名", "项目" not in m.OMNIJS_SOURCE, True)

if sys.platform == "darwin" and os.path.exists("/usr/bin/osascript"):
    # 把 tell OmniFocus 换成 return js，验证 chunk 拼接后的 JS 与源码逐字节一致
    probe = ojs.replace(
        'tell application "OmniFocus"\n\treturn evaluate javascript js\nend tell',
        "return js",
    )
    proc = subprocess.run(
        ["/usr/bin/osascript"], input=probe.encode("utf-8"), capture_output=True
    )
    check("omnijs 包装 osascript 退出 0", proc.returncode, 0)
    check(
        "omnijs 包装还原出的 JS 与源码一致",
        proc.stdout.decode("utf-8").rstrip("\n"),
        m.OMNIJS_SOURCE.strip("\n"),
    )
else:
    print("skip omnijs 包装探针（不是 macOS）")

if shutil.which("node"):
    node_mock = r'''
const Task = { Status: { Blocked: "blocked" } };
const mk = o => Object.assign({
  id: { primaryKey: "x" }, name: "x", project: null, containingProject: null,
  parent: null, inInbox: false, tags: [], flagged: false, completed: false,
  dropDate: null, dueDate: null, deferDate: null, plannedDate: null,
  completionDate: null, added: null, modified: null, estimatedMinutes: null,
  taskStatus: "available", children: []
}, o);
const proj = { name: "P", nextTask: null };
const root = mk({ id: { primaryKey: "root" }, name: "P", project: proj, containingProject: proj });
const inboxParent = mk({ id: { primaryKey: "ib" }, name: "IB", inInbox: true });
const child = mk({
  id: { primaryKey: "c1" }, name: "child", parent: root, containingProject: proj,
  tags: [{ id: { primaryKey: "ta" }, name: "a" }, { id: { primaryKey: "tb" }, name: "b" }], flagged: true,
  dueDate: new Date(2026, 9, 27, 9, 0, 0), estimatedMinutes: 50,
  taskStatus: "blocked", children: [{}]
});
const inboxChild = mk({ id: { primaryKey: "c2" }, name: "ib child", parent: inboxParent });
proj.nextTask = child;
const flattenedTasks = [root, inboxParent, child, inboxChild];
const database = { flattenedTasks };
const result = %s;
console.log(result);
''' % m.OMNIJS_SOURCE.strip()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "omnijs_mock.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(node_mock)
        proc = subprocess.run(["node", path], capture_output=True, text=True)
        check("Node mock 跑通 OmniJS", proc.returncode, 0)
        payload = json.loads(proc.stdout) if proc.returncode == 0 else {}
        data = payload.get("rows", [])
        check("Node mock：信封 tool 正确", payload.get("tool"), "omnifocus_tasks")
        check(
            "Node mock：kind 分类",
            [row["kind"] for row in data],
            ["project_root", "inbox", "action", "inbox"],
        )
        check("Node mock：inbox 子任务也算 inbox", data[3]["in_inbox"], True)
        check("Node mock：next 用 project.nextTask", data[2]["next"], True)
        check("Node mock：blocked 用 taskStatus", data[2]["blocked"], True)
        check("Node mock：tags", data[2]["tags"], ["a", "b"])
        check("Node mock：tag_ids", data[2]["tag_ids"], ["ta", "tb"])
        check("Node mock：本地 ISO 日期", data[2]["due"], "2026-10-27T09:00:00")
else:
    print("skip Node mock（没装 node）")

print("== AppleScript esc handler（真跑 osascript，纯文本不需要权限）==")
if sys.platform == "darwin" and os.path.exists("/usr/bin/osascript"):
    sample = (
        "a\tb\nc\rd\\e 中文 \"q\" 'x' "
        + m.RECORD_SEP + m.FIELD_SEP + m.TAG_SEP + " tail"
    )
    literal = (
        sample.replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\t", "\" & tab & \"")
        .replace("\n", "\" & linefeed & \"")
        .replace("\r", "\" & return & \"")
    )
    probe = (
        "on run\n"
        "\tset sample to \"%s\"\n"
        "\treturn my esc(sample)\n"
        "end run\n" % literal
    ) + m.APPLESCRIPT_HANDLERS
    proc = subprocess.run(
        ["/usr/bin/osascript"], input=probe.encode("utf-8"), capture_output=True
    )
    check("esc 探针 osascript 退出 0", proc.returncode, 0)
    raw = proc.stdout.decode("utf-8").rstrip("\n")
    check("esc → unescape 往返一致", m.unescape(raw), sample)
    check("分隔符都被转义了", m.RECORD_SEP in raw or m.FIELD_SEP in raw or m.TAG_SEP in raw, False)
else:
    print("skip esc handler（不是 macOS）")

print("== unescape ==")
check("简单转义", m.unescape("a\\tb\\nc\\rd\\\\e"), "a\tb\nc\rd\\e")
check("控制字符转义", m.unescape("a\\x1db\\x1ec\\x1fd"), "a" + m.TAG_SEP + "b" + m.RECORD_SEP + "c" + m.FIELD_SEP + "d")
check("未知转义原样保留", m.unescape("\\q"), "\\q")
check("末尾孤立反斜杠", m.unescape("abc\\"), "abc\\")

# ---------------------------------------------------------------- 解析

print("== parse_rows ==")
sample_rows = [
    {
        "id": "task-1",
        "name": "写论文\t初稿",
        "kind": "action",
        "project": "博士",
        "parent": "",
        "in_inbox": False,
        "tags": ["deep", "写作"],
        "tag_ids": ["tag-deep", "tag-write"],
        "flagged": True,
        "completed": False,
        "dropped": False,
        "due": "2026-10-27T09:00:00",
        "defer": None,
        "planned": None,
        "completed_at": None,
        "created": "2026-10-01T08:00:00",
        "modified": "2026-10-26T20:00:00",
        "estimated_minutes": 50,
        "next": True,
        "blocked": False,
        "children": 2,
        "note": "第一行\n第二行",
    },
    {
        "id": "task-2",
        "name": "收件箱里的",
        "kind": "inbox",
        "project": "",
        "parent": "",
        "in_inbox": True,
        "tags": [],
        "tag_ids": [],
        "flagged": False,
        "completed": True,
        "dropped": False,
        "due": None,
        "defer": "2026-10-28T10:30:00",
        "planned": None,
        "completed_at": "2026-10-26T18:00:00",
        "created": "2026-10-02T08:00:00",
        "modified": "2026-10-26T18:00:00",
        "estimated_minutes": None,
        "next": False,
        "blocked": False,
        "children": 0,
        "note": "",
    },
]
parsed = m.parse_rows(make_wire(sample_rows))
check("两条都解析出来", len(parsed), 2)
check("字段顺序与 FIELD_SPECS 一致", list(parsed[0].keys()), list(m.FIELDS))
check("kind 解析", (parsed[0]["kind"], parsed[1]["kind"]), ("action", "inbox"))
check("name 里的 tab 原样保留", parsed[0]["name"], "写论文\t初稿")
check("note 多行原样保留", parsed[0]["note"], "第一行\n第二行")
check("tags 变 list", parsed[0]["tags"], ["deep", "写作"])
check("tag_ids 变 list", parsed[0]["tag_ids"], ["tag-deep", "tag-write"])
check("空 tags 变 []", parsed[1]["tags"], [])
weird_tags = dict(sample_rows[0])
weird_tags["tags"] = ["a" + m.TAG_SEP + "b", "c"]
check(
    "标签名里的 TAG_SEP 不会被拆成两个",
    m.parse_rows(make_wire([weird_tags]))[0]["tags"],
    ["a" + m.TAG_SEP + "b", "c"],
)
check("bool", (parsed[0]["flagged"], parsed[1]["completed"]), (True, True))
check("int", parsed[0]["estimated_minutes"], 50)
check("缺失 int 变 None", parsed[1]["estimated_minutes"], None)
check("缺失 date 变 None", parsed[0]["defer"], None)
check("空 note 留空串", parsed[1]["note"], "")
check("空输出 → []", m.parse_rows("\n"), [])
check_raises("非法 bool 报错", lambda: m.coerce("bool", "maybe"))
check_raises("非法 int 报错", lambda: m.coerce("int", "3.5"))

print("== parse_json_rows ==")


def envelope(rows):
    return json.dumps({"tool": "omnifocus_tasks", "version": 1, "rows": rows})


json_rows = m.parse_json_rows(envelope(sample_rows))
check("JSON 两条", len(json_rows), 2)
check("JSON 字段顺序", list(json_rows[0].keys()), list(m.FIELDS))
check("JSON 类型：bool/int/list", (json_rows[0]["flagged"], json_rows[0]["estimated_minutes"], json_rows[0]["tags"]), (True, 50, ["deep", "写作"]))
check_raises("空返回要报错", lambda: m.parse_json_rows(""))
check_raises("缺 rows 要报错", lambda: m.parse_json_rows('{"a":1}'))
check_raises("tool 不对要报错", lambda: m.parse_json_rows('{"tool":"x","rows":[]}'))
check_raises("JSON kind 非法要报错", lambda: m.parse_json_rows(envelope([dict(sample_rows[0], kind="bogus")])))
check_raises(
    "字段数不对要报错",
    lambda: m.parse_rows("a" + m.FIELD_SEP + "b\n"),
)

# ---------------------------------------------------------------- 输出 / CLI

NODE_SET_MOCKS = r'''
const Task = { Status: { Blocked: "blocked" }, byIdentifier: id => DB[id] || null };
const work = { id: { primaryKey: "tag-work" }, name: "Work", parent: null };
const workDeep = { id: { primaryKey: "tag-work-deep" }, name: "Deep", parent: work };
const otherDeep = { id: { primaryKey: "tag-other-deep" }, name: "Deep", parent: null };
const writing = { id: { primaryKey: "tag-writing" }, name: "写作", parent: null };
const flattenedTags = [work, workDeep, otherDeep, writing];
const database = { flattenedTags };
const mk = o => Object.assign({
  id: { primaryKey: "x" }, name: "x", project: null, containingProject: { name: "P" },
  parent: null, inInbox: false, tags: [], flagged: false, completed: false,
  dropDate: null, dueDate: null, deferDate: null, plannedDate: null, completionDate: null,
  added: null, modified: null, estimatedMinutes: null, taskStatus: "available", children: [],
  markComplete(d) { this.completed = true; this.completionDate = d || new Date(2026, 9, 27, 12, 0, 0); return this; },
  markIncomplete() { this.completed = false; this.completionDate = null; },
  drop(all, d) { this.dropDate = d || new Date(2026, 9, 27, 12, 0, 0); return this; },
  addTags(list) { for (const g of list) { if (!this.tags.some(x => x.id.primaryKey === g.id.primaryKey)) this.tags.push(g); } },
  removeTags(list) { const ids = list.map(g => g.id.primaryKey); this.tags = this.tags.filter(x => !ids.includes(x.id.primaryKey)); },
  clearTags() { this.tags = []; }
}, o);
const t1 = mk({ id: { primaryKey: "t1" }, name: "写论文", tags: [writing] });
const t3 = mk({ id: { primaryKey: "t3" }, name: "带标签", tags: [writing, workDeep] });
const t2 = mk({ id: { primaryKey: "t2" }, name: "项目", project: { name: "P" }, containingProject: { name: "P" } });
const DB = { t1: t1, t2: t2, t3: t3 };
'''


def set_payload(**overrides):
    payload = {
        "id": "t1",
        "complete": False,
        "incomplete": False,
        "drop": False,
        "dropAll": False,
        "undrop": False,
        "at": None,
        "tags": {"add": [], "remove": [], "set": None, "clear": False},
        "dryRun": False,
        "allowProjectRoot": False,
        "ops": [],
    }
    for key, value in overrides.items():
        if key == "tags":
            payload["tags"].update(value)
        else:
            payload[key] = value
    return payload


def run_set_js(payload):
    js = m.SET_OMNIJS_TEMPLATE.replace(
        "{{ARGS}}",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    source = NODE_SET_MOCKS + "\nconst result = " + js.strip() + ";\nconsole.log(result);\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(source)
        path = fh.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


print("== set 的 OmniJS 逻辑（Node mock）==")
if shutil.which("node"):
    r = run_set_js(set_payload(complete=True, ops=["complete"]))
    check(
        "set JS：complete",
        (r["ok"], r["after"]["completed"], r["changed"]),
        (True, True, ["completed", "completion_date"]),
    )

    r = run_set_js(set_payload(complete=True, dryRun=True, ops=["complete"]))
    check(
        "set JS：dry-run 不写",
        (r["dry_run"], r["after"]["completed"], r["changed"]),
        (True, False, []),
    )

    r = run_set_js(
        set_payload(complete=True, at="2026-10-27T09:30:00", ops=["complete"])
    )
    check("set JS：--at 生效", r["after"]["completion_date"], "2026-10-27T09:30:00")

    r = run_set_js(set_payload(drop=True, ops=["drop"]))
    check("set JS：drop", (r["ok"], r["after"]["dropped"]), (True, True))

    r = run_set_js(set_payload(id="t3", tags={"set": ["写作"]}, ops=["set-tags:写作"]))
    check("set JS：set-tags 替换", r["after"]["tags"], ["写作"])

    r = run_set_js(set_payload(tags={"add": ["Work/Deep"]}, ops=["add-tag:Work/Deep"]))
    check("set JS：按完整路径加标签", r["after"]["tags"], ["写作", "Deep"])
    check("set JS：resolved path", r["resolved_tags"]["add"][0]["path"], "Work/Deep")

    r = run_set_js(set_payload(tags={"add": ["tag-work-deep"]}, ops=["add-tag:tag-work-deep"]))
    check("set JS：按标签 primary key 加", r["after"]["tags"], ["写作", "Deep"])

    r = run_set_js(set_payload(id="t3", tags={"remove": ["写作"]}, ops=["remove-tag:写作"]))
    check("set JS：remove-tag", r["after"]["tags"], ["Deep"])

    r = run_set_js(set_payload(id="t3", tags={"clear": True}, ops=["clear-tags"]))
    check("set JS：clear-tags", r["after"]["tags"], [])

    r = run_set_js(set_payload(tags={"add": ["Deep"]}, ops=["add-tag:Deep"]))
    check("set JS：叶子名歧义要报错", (r["ok"], r["error"]), (False, "tag_problem"))
    check("set JS：歧义带候选路径", len(r["problems"][0]["paths"]), 2)

    r = run_set_js(set_payload(tags={"add": ["不存在"]}, ops=["add-tag:不存在"]))
    check("set JS：标签不存在要报错", (r["ok"], r["error"]), (False, "tag_problem"))

    r = run_set_js(set_payload(id="nope", complete=True, ops=["complete"]))
    check("set JS：任务不存在", (r["ok"], r["error"]), (False, "not_found"))

    r = run_set_js(set_payload(id="t2", complete=True, ops=["complete"]))
    check("set JS：project_root 默认拒绝", (r["ok"], r["error"]), (False, "project_root_refused"))

    r = run_set_js(
        set_payload(id="t2", complete=True, allowProjectRoot=True, ops=["complete"])
    )
    check("set JS：--allow-project-root 放行", (r["ok"], r["after"]["kind"]), (True, "project_root"))
else:
    print("skip set Node mock（没装 node）")

print("== _tsv_cell ==")
check("None → 空串", m._tsv_cell(None), "")
check("True → true", m._tsv_cell(True), "true")
check("list → 逗号连接", m._tsv_cell(["a", "b"]), "a,b")
check("tab/newline/反斜杠转义", m._tsv_cell("a\tb\nc\\d"), "a\\tb\\nc\\\\d")

print("== CLI（假 osascript）==")
with tempfile.TemporaryDirectory() as tmp:
    wire_payload = make_wire(sample_rows)
    json_payload = json.dumps(
        {"tool": "omnifocus_tasks", "version": 1, "rows": sample_rows},
        ensure_ascii=False,
    )

    ok_json_shim = os.path.join(tmp, "fake-json")
    write_exe(
        ok_json_shim,
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write(%r)\n" % json_payload,
    )
    ok_wire_shim = os.path.join(tmp, "fake-wire")
    write_exe(
        ok_wire_shim,
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write(%r)\n" % wire_payload,
    )
    fallback_shim = os.path.join(tmp, "fake-fallback")
    write_exe(
        fallback_shim,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "source = sys.stdin.read()\n"
        "if 'evaluate javascript' in source:\n"
        "    sys.stderr.write('execution error: OmniFocus got an error: -1708\\n')\n"
        "    sys.exit(1)\n"
        "sys.stdout.write(%r)\n" % wire_payload,
    )
    bad_shim = os.path.join(tmp, "fake-bad")
    write_exe(
        bad_shim,
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('execution error: (-10004)\\n')\nsys.exit(1)\n",
    )
    missing_shim = os.path.join(tmp, "fake-missing")
    write_exe(
        missing_shim,
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write(\"execution error: Can't get application \\\"OmniFocus\\\". (-1728)\\n\")\nsys.exit(1)\n",
    )

    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = m.main(argv)
            except SystemExit as e:  # argparse 的参数错
                code = e.code
        return code, out.getvalue(), err.getvalue()

    code, out, err = run_main(
        [
            "--osascript",
            ok_json_shim,
            "--fields",
            "id,name,tags,flagged,due,estimated_minutes",
        ]
    )
    lines = out.strip().split("\n")
    check("omnijs CLI 退出 0", code, 0)
    check("omnijs CLI 两条", len(lines), 2)
    check(
        "JSONL 第一条只含所选字段且类型正确",
        json.loads(lines[0]),
        {
            "id": "task-1",
            "name": "写论文\t初稿",
            "tags": ["deep", "写作"],
            "flagged": True,
            "due": "2026-10-27T09:00:00",
            "estimated_minutes": 50,
        },
    )
    check("JSONL 中文不转义", "写作" in out, True)

    code, out, err = run_main(
        [
            "--engine",
            "applescript",
            "--osascript",
            ok_wire_shim,
            "--format",
            "tsv",
            "--header",
            "--fields",
            "name,flagged",
        ]
    )
    check("applescript CLI 退出 0", code, 0)
    check(
        "TSV 表头 + 转义 + 布尔小写",
        out,
        "name\tflagged\n写论文\\t初稿\ttrue\n收件箱里的\tfalse\n",
    )

    code, out, err = run_main(["--osascript", fallback_shim])
    check("omnijs 失败时自动回退成功", code, 0)
    check("回退打了 stderr 提示", "退回 applescript" in err, True)
    check("回退后仍有数据", len(out.strip().split("\n")), 2)

    code, out, err = run_main(["--osascript", bad_shim])
    check("权限错误退出码 3", code, 3)
    check("权限错误带系统设置提示", "隐私与安全性" in err, True)
    check("权限错误不回退", "退回 applescript" in err, False)

    code, out, err = run_main(["--osascript", missing_shim])
    check("找不到 App 退出码 1", code, 1)
    check("找不到 App 有提示", "没装" in err, True)

    code, out, err = run_main(["--script"])
    check("--script 退出 0", code, 0)
    check("--script 默认打 omnijs 包", "evaluate javascript js" in out, True)
    check("--script 里有 tell OmniFocus", "tell application \"OmniFocus\"" in out, True)

    code, out, err = run_main(["--script", "--engine", "applescript"])
    check("--script applescript 退出 0", code, 0)
    check("--script applescript 是逐属性版", "flattened tasks" in out, True)

    code, out, err = run_main(["--fields", "bogus"])
    check("坏字段退出码 2", code, 2)

    code, out, err = run_main(["--timeout", "0"])
    check("非正 timeout 退出码 2", code, 2)

    code, out, err = run_main(["--engine", "bogus"])
    check("坏引擎退出码 2", code, 2)

print("== set CLI（假 osascript）==")
SET_OK_RESULT = {
    "tool": "omnifocus_tasks_set",
    "version": 1,
    "ok": True,
    "ops": ["complete"],
    "id": "t1",
    "before": {
        "id": "t1",
        "name": "写论文",
        "kind": "action",
        "completed": False,
        "dropped": False,
        "completion_date": None,
        "drop_date": None,
        "tags": [],
        "tag_paths": [],
    },
    "after": {
        "id": "t1",
        "name": "写论文",
        "kind": "action",
        "completed": True,
        "dropped": False,
        "completion_date": "2026-10-27T12:00:00",
        "drop_date": None,
        "tags": [],
        "tag_paths": [],
    },
    "changed": ["completed", "completion_date"],
    "repeat_clone_id": None,
    "resolved_tags": {"add": [], "remove": [], "set": []},
}
with tempfile.TemporaryDirectory() as tmp:
    def success_shim(name, dry_run):
        result = dict(SET_OK_RESULT)
        result["dry_run"] = dry_run
        if dry_run:
            result["changed"] = []
            result["after"] = result["before"]
        path = os.path.join(tmp, name)
        write_exe(
            path,
            "#!/usr/bin/env python3\nimport sys, json\nsys.stdout.write(%r)\n"
            % json.dumps(result, ensure_ascii=False),
        )
        return path

    set_shim = success_shim("fake-set", False)
    set_dry_shim = success_shim("fake-set-dry", True)

    def error_shim(name, error, problems=None):
        payload = {
            "tool": "omnifocus_tasks_set",
            "version": 1,
            "ok": False,
            "error": error,
            "message": "test " + error,
        }
        if problems:
            payload["problems"] = problems
        path = os.path.join(tmp, name)
        write_exe(
            path,
            "#!/usr/bin/env python3\nimport sys, json\nsys.stdout.write(%r)\n"
            % json.dumps(payload, ensure_ascii=False),
        )
        return path

    not_found_shim = error_shim("fake-set-notfound", "not_found")
    tag_shim = error_shim(
        "fake-set-tag", "tag_problem", [{"kind": "not_found", "name": "x"}]
    )
    root_shim = error_shim("fake-set-root", "project_root_refused")

    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = m.main(argv)
            except SystemExit as e:  # argparse 的参数错
                code = e.code
        return code, out.getvalue(), err.getvalue()

    code, out, err = run_main(
        ["set", "--id", "t1", "--complete", "--osascript", set_dry_shim]
    )
    check("set 预览退出码 4", code, 4)
    check("set 预览 stdout 是 JSON", json.loads(out)["dry_run"], True)
    check("set 预览 stderr 提示", "[dry-run]" in err, True)

    code, out, err = run_main(
        ["set", "--id", "t1", "--complete", "--yes", "--osascript", set_shim]
    )
    check("set --yes 退出码 0", code, 0)
    check("set --yes 真写", json.loads(out)["dry_run"], False)
    check("set --yes stderr 提示", "已改" in err, True)

    code, out, err = run_main(
        ["set", "--id", "t1", "--complete", "--osascript", not_found_shim]
    )
    check("set 任务不存在退出码 5", code, 5)
    code, out, err = run_main(
        ["set", "--id", "t1", "--add-tag", "x", "--osascript", tag_shim]
    )
    check("set 标签问题退出码 6", code, 6)
    check("set 标签问题打印 details", "not_found" in err, True)
    code, out, err = run_main(
        ["set", "--id", "t1", "--complete", "--osascript", root_shim]
    )
    check("set project_root 退出码 7", code, 7)

    code, out, err = run_main(["set", "--id", "t1", "--complete", "--script"])
    check("set --script 退出 0", code, 0)
    check("set --script 是写脚本", "omnifocus_tasks_set" in out, True)
    check("set --script 走 evaluate javascript", "evaluate javascript js" in out, True)

    for argv, label in (
        (["set", "--id", "t1"], "没有操作"),
        (["set", "--id", "t1", "--complete", "--add-tag", "x"], "状态+标签混用"),
        (["set", "--id", "t1", "--incomplete", "--at", "2026-10-27T09:00:00"], "--at 用错"),
        (["set", "--id", "t1", "--set-tags", ""], "空 set-tags"),
        (["set", "--id", "t1", "--add-tag", "x", "--remove-tag", "x"], "同标签又加又删"),
        (["set", "--id", "t1", "--complete", "--all-occurrences"], "--all-occurrences 用错"),
    ):
        code, out, err = run_main(argv)
        check("set 参数错：%s → 2" % label, code, 2)

print("== set payload ==")
set_parser = m.build_set_parser()
args = set_parser.parse_args(["--id", "t1", "--complete", "--at", "2026-10-27"])
check("date-only --at 补本地零点", m.build_set_payload(args)["at"], "2026-10-27T00:00:00")
args = set_parser.parse_args(["--id", "t1", "--complete"])
check("默认 dryRun", m.build_set_payload(args)["dryRun"], True)
args = set_parser.parse_args(["--id", "t1", "--complete", "--yes"])
check("--yes 后 dryRun=False", m.build_set_payload(args)["dryRun"], False)
args = set_parser.parse_args(["--id", "t1", "--add-tag", "a,b", "--yes"])
check("--add-tag 不拆逗号", m.build_set_payload(args)["tags"]["add"], ["a,b"])
args = set_parser.parse_args(["--id", "t1", "--set-tags", "A,B", "--set-tags", "C", "--yes"])
check("--set-tags 拆逗号并去重", m.build_set_payload(args)["tags"]["set"], ["A", "B", "C"])
args = set_parser.parse_args(["--id", "t1", "--drop", "--all-occurrences", "--yes"])
drop_all_payload = m.build_set_payload(args)
check("--drop --all-occurrences", (drop_all_payload["dropAll"], drop_all_payload["ops"]), (True, ["drop-all"]))
check_raises(
    "坏 --at 报 ValueError",
    lambda: m.build_set_payload(
        set_parser.parse_args(["--id", "t1", "--complete", "--at", "garbage"])
    ),
)

print("== set 脚本包装 ==")
set_payload_demo = m.build_set_payload(
    set_parser.parse_args(["--id", "t1", "--add-tag", '写作 "q" \\ b', "--yes"])
)
set_script = m.build_set_script(set_payload_demo)
if sys.platform == "darwin" and shutil.which("osacompile"):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "set.applescript")
        dst = os.path.join(tmp, "set.scpt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(set_script)
        proc = subprocess.run(["osacompile", "-o", dst, src], capture_output=True, text=True)
        check("set osacompile 编译", (proc.returncode, proc.stderr.strip()), (0, ""))
else:
    print("skip set osacompile（不是 macOS）")

if sys.platform == "darwin" and os.path.exists("/usr/bin/osascript"):
    probe = set_script.replace(
        'tell application "OmniFocus"\n\treturn evaluate javascript js\nend tell',
        "return js",
    )
    proc = subprocess.run(
        ["/usr/bin/osascript"], input=probe.encode("utf-8"), capture_output=True
    )
    expected_js = m.SET_OMNIJS_TEMPLATE.replace(
        "{{ARGS}}",
        json.dumps(set_payload_demo, ensure_ascii=False, separators=(",", ":")),
    ).strip("\n")
    check("set 包装 osascript 退出 0", proc.returncode, 0)
    check("set 包装还原的 JS 一致", proc.stdout.decode("utf-8").rstrip("\n"), expected_js)
else:
    print("skip set 包装探针（不是 macOS）")

print()
if fails:
    print("FAILED %d:" % len(fails))
    for item in fails:
        print("  - " + item)
    sys.exit(1)
print("all good")
