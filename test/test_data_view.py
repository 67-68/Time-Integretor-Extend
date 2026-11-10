#!/usr/bin/env python3
"""第四视图「数据」：open task / project / op 三种模型切换的回归。"""
import importlib.util
import io
import os
import shutil
import tempfile

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="data-view-test-")
for _name, _file in (
    ("TI_CALENDAR_DOWNLOAD", "download.json"),
    ("TI_SESSION_LAYOUTS", "layouts.json"),
    ("TI_SESSION_EXTRAS", "extras.json"),
    ("TI_SESSION_RULES", "rules.json"),
    ("TI_SETTINGS_FILE", "settings.json"),
    ("TI_TASKS_FILE", "tasks.json"),
    ("TI_PROJECTS_FILE", "projects.json"),
    ("TI_PROJECT_SYNC_FILE", "project-sync.json"),
    ("TI_NOSTART_FILE", "nostart.json"),
    ("TI_TASK_ARCHIVE", "archive.json"),
):
    os.environ[_name] = os.path.join(TMP, _file)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


class FakeBridge(object):
    def stop(self):
        pass


def fresh():
    console = Console(file=io.StringIO(), width=180, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    return wd


print("== 第四视图改名「数据」，默认展示 task 模型 ==")
wd = fresh()
check("视图名", [v.name for v in w.VIEWS],
      ["契约", "专注", "统计", "数据", "设置", "琐事"])
check("DataView 可从顶层导入", w.DataView is w.TaskView, True)
wd.go_to_view(w.DataView)
check("当前视图", wd.view.name, "数据")
check("默认模型", wd.data_view_model, "task")
check_true("默认还是任务表", "Task · 任务表" in str(wd.view.render()[0].title))

print("== open project 展示真正项目 pro 数据 ==")
pro, _ = wd.projects.create_pro("演示项目", stage="running")
wd.task_store.create({"title": "普通任务", "project": "演示项目"})
check("open project 受理", wd.handle("open project"), True)
check("模型切到 project", wd.data_view_model, "project")
check("仍在数据视图", isinstance(wd.view, w.DataView), True)
parts = wd.view.render()
check_true("项目表标题", "Project · 真正项目 pro 数据" in str(parts[0].title))
buf = io.StringIO()
Console(file=buf, width=180, force_terminal=False).print(parts[0])
check_true("项目行可见", "演示项目" in buf.getvalue())

print("== open op 展示 OmniFocus Project op 数据 ==")
op, _created = wd.projects.upsert({
    "id": "of-demo", "title": "OmniFocus 演示 op", "op_type": "SOP",
    "tasks": [], "pro_id": pro["id"],
})
check("open op 受理", wd.handle("open op"), True)
check("模型切到 op", wd.data_view_model, "op")
parts = wd.view.render()
check_true("op 表标题", "Op · OmniFocus Project 数据" in str(parts[0].title))
buf = io.StringIO()
Console(file=buf, width=180, force_terminal=False).print(parts[0])
out = buf.getvalue()
check_true("op 行可见", "OmniFocus 演示 op" in out)
check_true("op 类型可见", "SOP" in out)

print("== pro / op 状态机制 + data view 默认 disposable filter ==")
check("默认 filter", wd.data_filter, "disposable")
wd.handle("open project")
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("默认 project 表有演示项目", "演示项目" in _buf.getvalue(), True)

# op 用 -m discard；默认 disposable 下应消失
op_no = wd.projects.number_of(op["id"])
check("op -m discard 受理", wd.handle("op %d -m discard" % op_no), True)
check("op 状态落盘", wd.projects.by_id(op["id"])["status"], "dropped")
wd.handle("open op")
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("disposable 下 dropped op 不显示", "OmniFocus 演示 op" in _buf.getvalue(), False)
check("plan 不再收 dropped op",
      [x["id"] for x in wd.projects.plan_ops() if x["id"] == op["id"]], [])
check("filter all 受理", wd.handle("filter all"), True)
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("filter all 能看到 dropped op", "OmniFocus 演示 op" in _buf.getvalue(), True)
check("filter dropped 受理", wd.handle("filter dropped"), True)
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("filter dropped 只显示 dropped op", "OmniFocus 演示 op" in _buf.getvalue(), True)
check("op -m active 序号恢复受理", wd.handle("op -m active 1"), True)
check("op 恢复 active", wd.projects.by_id(op["id"])["status"], "active")
wd.handle("filter disposable")

# pro 用直接简写 mark complete；默认 disposable 下应消失
pro_no = wd.projects.pro_number_of(pro["id"])
check("project complete 简写受理", wd.handle("project %d complete" % pro_no), True)
check("pro 状态落盘", wd.projects.pro_by_id(pro["id"])["status"], "completed")
# 自动补关联不能因为 pro 不在 active 列表里就重建一个同名 active pro
wd.handle("project")
check("mark 后自动补关联没有重建同名 pro",
      [(x["id"], x["status"]) for x in wd.projects.pros()],
      [(pro["id"], "completed")])
wd.handle("open project")
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("disposable 下 completed pro 不显示", "演示项目" in _buf.getvalue(), False)
check("filter all 受理", wd.handle("filter all"), True)
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("filter all 显示 completed pro", "演示项目" in _buf.getvalue(), True)
# -m 放前面也可以；同名下只有这一个 pro 时序号仍可恢复
check("project -m active 前置写法受理", wd.handle("project -m active 1"), True)
check("pro 恢复 active", wd.projects.pro_by_id(pro["id"])["status"], "active")
check("filter active 恢复默认", wd.handle("filter active"), True)
wd.handle("open project")
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("恢复后的 pro 重新出现", "演示项目" in _buf.getvalue(), True)

# task 模型同样受 disposable 过滤
wd.handle("open task")
active_task = wd.task_store.create({"title": "活跃任务数据"})
done_task = wd.task_store.create({"title": "已完成任务数据"})
wd.task_store.set_status(done_task["id"], w.ts.STATUS_COMPLETED, reason="test")
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("disposable 隐藏 completed task", "已完成任务数据" in _buf.getvalue(), False)
check("disposable 仍显示 active task", "活跃任务数据" in _buf.getvalue(), True)
check("filter completed 受理", wd.handle("filter completed"), True)
_buf = io.StringIO()
Console(file=_buf, width=180, force_terminal=False).print(wd.view.render()[0])
check("filter completed 只显示 completed task",
      ("已完成任务数据" in _buf.getvalue(), "活跃任务数据" in _buf.getvalue()),
      (True, False))
check("filter off = all", wd.handle("filter off"), True)
check("filter off 后状态", wd.data_filter, "all")
wd.handle("filter disposable")

print("== open task 切回任务数据，不影响 project 表列 ==")
check("open task 受理", wd.handle("open task"), True)
check("模型切回 task", wd.data_view_model, "task")
check_true("任务表回来了", "Task · 任务表" in str(wd.view.render()[0].title))
# 在数据视图里 open project 已经是模型切换；任务表 project 列仍可用中文别名展开。
check("open 项目 仍能展开任务表 project 列", wd.handle("open 项目"), True)
check("project 列出现", "project" in [str(c.header) for c in wd.view.render()[0].columns], True)

print("== 从别的视图也能直接 open project ==")
wd.go_to_view(w.ContractView)
check("从契约视图 open project", wd.handle("open project"), True)
check("自动切到数据视图", wd.view.name, "数据")
check("模型 project", wd.data_view_model, "project")

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("%d 项失败：" % len(fails))
    for item in fails:
        print(" - " + item)
    raise SystemExit(1)
print("全绿。")
