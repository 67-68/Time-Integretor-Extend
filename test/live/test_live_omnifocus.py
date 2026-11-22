#!/usr/bin/env python3
"""真机集成（会真的读写 OmniFocus）：侧别标签 / merge / 冻结解冻写得进去吗？

单元测试跑的是离线分支（``offline=True``）：能验算出来的 ops，验不了
「OmniFocus 那边到底有没有那个标签」。而真实故障恰好长在那里 ——

    warden 发 ``set tags.add ["incubator"]`` → OmniFocus 里没有这个标签
    → 每条 batch op 回 ``tag_problem`` → 外部一条都没写
    → 下一次 ``project refresh`` 拿 OmniFocus 的旧标签把本地补的也洗掉
    → 「pro merge 不可用，没有 tag 被添加」

所以这一份不 mock 任何 OmniFocus 行为：

    建两个临时 OmniFocus 项目（SOP 2 条任务 / incubator 1 条）
    → warden.py --exec（真子进程）op import / pro create / pro merge --yes
    → 用独立 OmniJS 探针回读任务标签与所属项目
    → pro 1 -a 切 bottleneck：SOP 侧挂 manual_defer、incubator 侧摘掉
    → pro 1 -a 切回 running：反过来
    → 删掉临时项目（不动 SOP / incubator / manual_defer 这几个侧别标签）

用法
----
    python3 -B test/live/test_live_omnifocus.py            # 只打印计划（不碰 OmniFocus）
    python3 -B test/live/test_live_omnifocus.py --yes      # 真跑（建/删两个临时项目）
    python3 -B test/live/test_live_omnifocus.py --yes --keep   # 跑完保留临时项目

前置条件：macOS + OmniFocus 正在跑，且**跑这个脚本的终端 App** 有「自动化 →
OmniFocus」权限（TCC）。没有权限时探针会秒回 -10004/-1743，脚本直接说清楚怎么授权。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OSASCRIPT = "/usr/bin/osascript"

#: 沙盒里所有落盘文件都指到临时目录（和 test_project.py 那份 TI_* 清单同一个规矩）。
SANDBOX_ENV_KEYS = (
    "TI_TEMP",
    "TI_DATA_CSV",
    "TI_TASKS_FILE",
    "TI_TASK_ARCHIVE",
    "TI_PROJECTS_FILE",
    "TI_LOG_DIR",
    "TI_SETTINGS_FILE",
    "TI_CALENDAR_DOWNLOAD",
    "TI_SESSION_LAYOUTS",
    "TI_SESSION_EXTRAS",
    "TI_SESSION_RULES",
    "TI_SESSION_MUTES",
    "TI_PLAN_PINS",
    "TI_PLAN_DECISIONS",
    "TI_PLAN_WATCHDOG",
    "TI_NOSTART_FILE",
    "TI_POSTSESSION_FILE",
)

#: 没有 node 的 PATH：bridge / focusmate-mcp 会立刻失败，不会去开 Chromium。
NO_NODE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

SOP_PROJECT = "__ti_live_merge_sop__"
INC_PROJECT = "__ti_live_merge_inc__"
FIXTURE_PREFIX = "__ti_live_"

fails = []
ok_count = 0


def check(label, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % label)
    else:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
        print("FAIL %s\n      got  = %r\n      want = %r" % (label, got, want))


def check_true(label, got):
    check(label, bool(got), True)


# ---------------------------------------------------------------- OmniJS 探针

class OmniFocusUnavailable(Exception):
    pass


def run_omnijs(js, timeout=90):
    """把一段 Omni Automation JS 交给 OmniFocus（AppleScript 的 evaluate javascript）。"""
    if sys.platform != "darwin":
        raise OmniFocusUnavailable("只在 macOS 上能跑（需要 osascript + OmniFocus）")
    if not os.path.exists(OSASCRIPT):
        raise OmniFocusUnavailable("找不到 %s" % OSASCRIPT)
    escaped = (js.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("\r", "\\r").replace("\n", "\\n"))
    script = 'set js to "%s"\ntell application "OmniFocus"\n\treturn evaluate javascript js\nend tell\n' % escaped
    try:
        proc = subprocess.run([OSASCRIPT, "-"], input=script, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OmniFocusUnavailable("osascript 超过 %ss 没返回（OmniFocus 卡住 / 权限框没点？）"
                                   % timeout) from None
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    low = text.lower()
    if "-10004" in text or "-1743" in text or "not authorized" in low or "not permitted" in low:
        raise OmniFocusUnavailable(
            "macOS 拒绝了自动化权限（TCC）：跑这个脚本的终端 App 没被允许向 OmniFocus "
            "发 Apple 事件。\n  下一步：系统设置 → 隐私与安全性 → 自动化 → 勾上当前终端"
            "里的 OmniFocus（在 iTerm2 里跑就要勾 iTerm2，勾「终端」不算）。\n  原始错误：%s"
            % text.splitlines()[0] if text else text)
    if proc.returncode != 0:
        raise OmniFocusUnavailable("osascript 退出码 %d：%s" % (proc.returncode, text[:300]))
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def list_fixture_projects():
    return run_omnijs(
        '(() => JSON.stringify(Array.from(flattenedProjects)'
        '.filter(p => p.name.indexOf("%s") === 0)'
        '.map(p => ({name: p.name, id: p.id.primaryKey}))))()' % FIXTURE_PREFIX)


def delete_projects(project_ids):
    if not project_ids:
        return []
    return run_omnijs(
        '(() => { const out = []; for (const id of %s) {'
        ' const p = Project.byIdentifier(id);'
        ' if (p) { deleteObject(p); out.push(id); } } return JSON.stringify(out); })()'
        % json.dumps([str(x) for x in project_ids]))


def create_fixture():
    """建两个临时项目 + 3 条任务；返回 ``{project: id, tasks: {pk: ...}}``。"""
    js = r'''
(() => {
  const res = {sop_project: null, inc_project: null, sop_tasks: [], inc_tasks: []};
  const mkProject = (pname, items, bucketP, bucketT) => {
    const proj = new Project(pname);
    proj.status = Project.Status.Active;
    res[bucketP] = proj.id.primaryKey;
    for (const it of items) {
      const t = new Task(it[0], proj);
      t.estimatedMinutes = it[1];
      res[bucketT].push({id: t.id.primaryKey, name: it[0]});
    }
  };
  mkProject("__ti_live_merge_sop__",
            [["live 测试 SOP 任务 A", 50], ["live 测试 SOP 任务 B", 25]],
            "sop_project", "sop_tasks");
  mkProject("__ti_live_merge_inc__",
            [["live 测试 INC 任务 A", 50]],
            "inc_project", "inc_tasks");
  return JSON.stringify(res);
})()
'''
    return run_omnijs(js)


def read_tasks(task_ids):
    """回读：每条任务的标签名（排序）+ 所属项目 primary key + 项目名。"""
    js = (
        '(() => { const ids = %s; const out = [];'
        ' for (const id of ids) { const t = Task.byIdentifier(id);'
        '  if (!t) { out.push({id: id, found: false}); continue; }'
        '  out.push({id: id, found: true,'
        '    tags: Array.from(t.tags).map(g => g.name).sort(),'
        '    project_id: t.containingProject ? t.containingProject.id.primaryKey : null,'
        '    project_name: t.containingProject ? t.containingProject.name : null});'
        ' } return JSON.stringify(out); })()'
        % json.dumps([str(x) for x in task_ids])
    )
    return {row["id"]: row for row in (run_omnijs(js) or [])}


# ---------------------------------------------------------------- 沙盒

def build_sandbox():
    sb = tempfile.mkdtemp(prefix="ti-live-omnifocus-")
    os.makedirs(os.path.join(sb, "data"))
    os.makedirs(os.path.join(sb, "temp", "logs"))
    now = datetime.datetime.now().isoformat(timespec="seconds")

    def write_json(rel, data):
        path = os.path.join(sb, rel)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    write_json("data/tasks.json", {"version": 1, "next_id": 1, "tasks": []})
    write_json("data/projects.json", {"version": 1, "next_id": 1, "projects": [],
                                      "next_pro_id": 1, "pros": []})
    write_json("data/task-archive.json", {"version": 1, "tasks": []})
    write_json("data/failed-actions.json", {"version": 1, "actions": []})
    write_json("data/settings.json", {"version": 1, "values": {}})
    write_json("temp/calendar-download.json", {"version": 1, "generated_at": now,
                                               "start_date": datetime.date.today().isoformat(),
                                               "days": 1, "profiles": [], "events": [],
                                               "periods": []})
    write_json("temp/session-layouts.json", {"version": 1, "days": {}})
    write_json("temp/session-extras.json", {"version": 1, "items": []})
    write_json("temp/session-rules.json", {"version": 1, "rules": []})
    write_json("temp/session-mutes.json", {"version": 1, "mutes": []})
    write_json("temp/plan-pins.json", {"version": 1, "pins": []})
    write_json("temp/plan-decisions.json", {"version": 1, "days": {}})
    write_json("temp/plan-watchdog.json", {"version": 1, "days": {}})
    with open(os.path.join(sb, "data.csv"), "w", encoding="utf-8") as fh:
        fh.write("# ti_extend live omnifocus test\n")
    return sb


def sandbox_env(sb):
    env = dict(os.environ)
    env.update({
        "TI_TEMP": os.path.join(sb, "temp"),
        "TI_DATA_CSV": os.path.join(sb, "data.csv"),
        "TI_TASKS_FILE": os.path.join(sb, "data", "tasks.json"),
        "TI_TASK_ARCHIVE": os.path.join(sb, "data", "task-archive.json"),
        "TI_PROJECTS_FILE": os.path.join(sb, "data", "projects.json"),
        "TI_LOG_DIR": os.path.join(sb, "temp", "logs"),
        "TI_SETTINGS_FILE": os.path.join(sb, "data", "settings.json"),
        "TI_CALENDAR_DOWNLOAD": os.path.join(sb, "temp", "calendar-download.json"),
        "TI_SESSION_LAYOUTS": os.path.join(sb, "temp", "session-layouts.json"),
        "TI_SESSION_EXTRAS": os.path.join(sb, "temp", "session-extras.json"),
        "TI_SESSION_RULES": os.path.join(sb, "temp", "session-rules.json"),
        "TI_SESSION_MUTES": os.path.join(sb, "temp", "session-mutes.json"),
        "TI_PLAN_PINS": os.path.join(sb, "temp", "plan-pins.json"),
        "TI_PLAN_DECISIONS": os.path.join(sb, "temp", "plan-decisions.json"),
        "TI_PLAN_WATCHDOG": os.path.join(sb, "temp", "plan-watchdog.json"),
        "TI_NOSTART_FILE": os.path.join(sb, "temp", "keep_session-nostart.json"),
        "TI_POSTSESSION_FILE": os.path.join(sb, "temp", "keep_session-postsession.json"),
        # 让 bridge / Focusmate 立刻失败：这一份只验 OmniFocus 那半条线。
        "PATH": NO_NODE_PATH,
        "REPL_KIT_PATH": os.environ.get(
            "REPL_KIT_PATH", os.path.join(os.path.dirname(ROOT), "paper_researcher")),
    })
    return env


def run_warden(sb, commands, timeout=240):
    """真子进程 ``warden.py --exec``（不 import warden、不 stub 任何东西）。"""
    cmd = [sys.executable, "-B", os.path.join(ROOT, "warden.py"), "--exec", commands]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=sandbox_env(sb), cwd=ROOT)
    return proc


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def merge_events(sb):
    log_dir = os.path.join(sb, "temp", "logs")
    out = []
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, name), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("event") in ("merge", "merge_partial",
                                        "merge_write_failed"):
                    out.append(row)
    return out


# ---------------------------------------------------------------- 主流程

def plan_text():
    return """计划（--yes 才执行，会真的在建/删 OmniFocus 项目）：

  1. 清掉上次残留的 %s* 临时项目（有就删）；
  2. 建两个临时项目 + 3 条任务：
       %s  任务 A(50min) / B(25min)
       %s  任务 A(50min)
  3. 沙盒（%s 临时目录）里真子进程跑：
       op import sop omnifocus:///project/<sop>
       op import incubator omnifocus:///project/<inc>
       pro create __ti_live_merge__ op=1
       pro 1 op 2
       pro 1 merge --yes
  4. 独立 OmniJS 探针回读，断言：
       · SOP 两条任务在 OmniFocus 里各挂 SOP；
       · INC 任务被 moveTasks 挪进 SOP 项目，挂 incubator + manual_defer
         （running 阶段冻结非活跃侧）；
       · 本地账：src op dropped、清单只剩一份；日志 event=merge 的 omni.failed == 0；
       · 没出现 merge_partial / merge_write_failed。
  5. pro 1 -a 切 bottleneck：SOP 两条挂 manual_defer、INC 摘掉；再切回 running 反向。
  6. 删掉两个临时项目（侧别标签 SOP / incubator / manual_defer 保留）。
""" % (FIXTURE_PREFIX, SOP_PROJECT, INC_PROJECT, "test/live 下")


def main(argv=None):
    ap = argparse.ArgumentParser(description="真机 OmniFocus 标签写回 / merge 集成测试")
    ap.add_argument("--yes", action="store_true", help="真跑（不写就是只打印计划）")
    ap.add_argument("--keep", action="store_true", help="跑完保留临时 OmniFocus 项目")
    args = ap.parse_args(argv)

    print(plan_text())
    if not args.yes:
        print("（没有 --yes：什么都没做）")
        return 0

    try:
        leftovers = list_fixture_projects() or []
    except OmniFocusUnavailable as e:
        print("\n跳过：%s" % e)
        return 1
    if leftovers:
        print("清理上次残留：%s" % ", ".join(p["name"] for p in leftovers))
        delete_projects([p["id"] for p in leftovers])

    fixture = None
    sb = build_sandbox()
    project_ids = []
    try:
        fixture = create_fixture()
        project_ids = [fixture["sop_project"], fixture["inc_project"]]
        sop_pks = [t["id"] for t in fixture["sop_tasks"]]
        inc_pks = [t["id"] for t in fixture["inc_tasks"]]
        print("\n临时项目：SOP=%s INC=%s" % tuple(project_ids))

        commands = "; ".join([
            "op import sop omnifocus:///project/%s" % fixture["sop_project"],
            "op import incubator omnifocus:///project/%s" % fixture["inc_project"],
            "pro create __ti_live_merge__ op=1",
            "pro 1 op 2",
            "pro 1 merge --yes",
        ])
        proc = run_warden(sb, commands)
        print("\n-- warden --exec 退出码 %d" % proc.returncode)
        # 退出码 1 是**环境噪声**：PATH 里故意没有 node，bridge / Focusmate 的
        # 拉取一律失败（had_error）。真正要断言的是「命令本身没炸 + 落盘事实对」。
        check("warden --exec 正常跑完（0/1，1=没有 node 的噪声）",
              proc.returncode in (0, 1), True)
        check("没有意外异常 / 写回失败",
              [k for k in ("意外错误", "Traceback", "写回失败")
               if k in (proc.stdout + proc.stderr)], [])

        after_merge = read_tasks(sop_pks + inc_pks)
        check("SOP 任务 A 在 OmniFocus 里挂 SOP",
              after_merge[sop_pks[0]]["tags"], ["SOP"])
        check("SOP 任务 B 在 OmniFocus 里挂 SOP",
              after_merge[sop_pks[1]]["tags"], ["SOP"])
        check("INC 任务被挪进 SOP 项目",
              after_merge[inc_pks[0]]["project_id"], fixture["sop_project"])
        check("INC 任务挂 incubator + manual_defer（running 阶段冻结）",
              after_merge[inc_pks[0]]["tags"], ["incubator", "manual_defer"])

        store = read_json(os.path.join(sb, "data", "projects.json"), {}) or {}
        pro = (store.get("pros") or [{}])[0]
        ops = [op for op in (store.get("projects") or [])
               if op.get("status") == "active"]
        check("本地只剩一个 active op", len(ops), 1)
        check("本地 pro.op_id 指向唯一 op", pro.get("op_id"), ops[0]["id"] if ops else None)
        check("本地合并后的清单 = 3 条（INC 在前 + 两条 SOP）",
              [t.get("pk") for t in (ops[0].get("tasks") if ops else [])],
              [inc_pks[0], sop_pks[0], sop_pks[1]])
        dropped = [op for op in (store.get("projects") or [])
                   if op.get("status") == "dropped"]
        check("本地旧 incubator op 标了 dropped", len(dropped), 1)

        events = merge_events(sb)
        merge_rows = [r for r in events if r.get("event") == "merge"]
        check("日志里有且只有一条 merge", len(merge_rows), 1)
        if merge_rows:
            check("merge 的 omni.failed == 0（外部全写成功）",
                  (merge_rows[0].get("omni") or {}).get("failed"), 0)
            check("merge 冻结了 1 条（INC 侧）", merge_rows[0].get("frozen"), 1)
        check("没有 merge_partial / merge_write_failed",
              [r for r in events if r.get("event") != "merge"], [])

        # 切阶段：bottleneck → SOP 冻结、INC 解冻；再切回 running 反向。
        proc = run_warden(sb, "pro 1 -a")
        check("切 bottleneck 的 warden 没报意外错误",
              [k for k in ("意外错误", "Traceback") if k in (proc.stdout + proc.stderr)], [])
        pushed = read_tasks(sop_pks + inc_pks)
        check("bottleneck：SOP 任务 A 被冻结（manual_defer）",
              pushed[sop_pks[0]]["tags"], ["SOP", "manual_defer"])
        check("bottleneck：SOP 任务 B 被冻结（manual_defer）",
              pushed[sop_pks[1]]["tags"], ["SOP", "manual_defer"])
        check("bottleneck：INC 任务解冻（摘掉 manual_defer）",
              pushed[inc_pks[0]]["tags"], ["incubator"])

        proc = run_warden(sb, "pro 1 -a")
        check("切回 running 的 warden 没报意外错误",
              [k for k in ("意外错误", "Traceback") if k in (proc.stdout + proc.stderr)], [])
        popped = read_tasks(sop_pks + inc_pks)
        check("running：SOP 任务 A 解冻",
              popped[sop_pks[0]]["tags"], ["SOP"])
        check("running：INC 任务重新冻结",
              popped[inc_pks[0]]["tags"], ["incubator", "manual_defer"])

        store = read_json(os.path.join(sb, "data", "projects.json"), {}) or {}
        pro = (store.get("pros") or [{}])[0]
        check("切回 running 后 pro.stage", pro.get("stage"), "running")

    finally:
        if project_ids and not args.keep:
            deleted = delete_projects(project_ids)
            print("\n清理：删掉临时项目 %s" % (deleted,))
        elif project_ids:
            print("\n保留临时项目：%s（--keep）" % (project_ids,))
        if not args.keep:
            shutil.rmtree(sb, ignore_errors=True)
        else:
            print("沙盒保留：%s" % sb)

    print()
    if fails:
        print("%d 项失败：" % len(fails))
        for item in fails:
            print(" - " + item)
        return 1
    print("全绿（%d 项）。" % ok_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
