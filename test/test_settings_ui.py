#!/usr/bin/env python3
"""设置视图 / 设置项 / 警告开关 / 状态框 的回归测试。

    python3 test/test_settings_ui.py

覆盖用户提的这几点：
  1. 契约界面那行「永不重订名单（N 条）」换成警告，且和「契约（data.csv 周几）」
     一起装进同一个 ASCII「状态」框；
  2. 框架侧：警告默认显示、只在指定视图显示、可以关掉（warn on/off）；
  3. 设置界面列出**全部**设置项（哪怕没改过）和解释，`set <id> <value>` 修改；
  4. edit-tomorrow 设置：今晚可以把明天当草稿区（不用敲宣言），过了凌晨照旧。

不联网、不碰浏览器、不写仓库里的任何文件。
"""
import datetime
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


TMP = tempfile.mkdtemp(prefix="settings-ui-test-")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "download.json")
os.environ["TI_SESSION_LAYOUTS"] = os.path.join(TMP, "layouts.json")
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "archive.json")

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
from rich.console import Console  # noqa: E402

cfgs, sp, sch, ks = w.cfg, w.sp, w.sch, w.ks
sch.DATA_CSV = os.path.join(TMP, "data.csv")
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
ks.STATE_FILE = os.path.join(TMP, "state.json")
# 改排布会取 daemon 那一轮的 round lock、并写「我取消的」挂号单 —— 别落到真目录里
ks.LOCK_FILE = os.path.join(os.path.dirname(ks.STATE_FILE), "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(os.path.dirname(ks.STATE_FILE),
                                "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")

# 兜底：本文件里任何一条**意外**走到「真·一轮」的路径都要立刻炸，而不是
# 去连 api.focusmate.com（真下过单的东西，测试绝不该碰）。日志也一样要关进
# 临时目录 —— 以前漏了，用例的日志混进真 temp/keep_session.log 里，看日志
# 排障时会以为 daemon 半夜自己干了什么。
ks.LOG_FILE = os.path.join(TMP, "keep_session.log")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")


def _no_network(*a, **kw):
    raise RuntimeError("测试里不许真连 Focusmate API（这一轮本该被替身拦住）")


ks.fetch_session_details = _no_network
ks.fetch_sessions = _no_network

ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **k: None
w.run_keep_session = lambda *a, **kw: (0, "round done", "")
w.run_download_calendar = lambda *a, **kw: (0, "ok", "")

TODAY = datetime.date.today()
ISO = TODAY.isoformat()
WD = sch.weekday_of_day(TODAY)


class FakeBridge(object):
    def __init__(self):
        self.calls = []
        self.last_idle_close = None

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "release":
            return {"released": False}
        if cmd == "snapshot":
            return {"date": kw.get("date"), "now": 0, "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, False, ""

    def stop(self):
        pass


def mkwarden():
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return w.Warden(console, FakeBridge(), no_clear=True, offline=True)


def write_csv(lines):
    with open(sch.DATA_CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def render(wd, width=120):
    buf = io.StringIO()
    wd.console = Console(file=buf, width=width, force_terminal=False)
    wd.paint()
    return buf.getvalue()


# ---------------------------------------------------------------- 设置项本身

print("== settings_store：注册表 / 取值 / 落盘 ==")
rows = cfgs.SettingStore().rows()
check("设置项齐全（含 OmniFocus / 收尾那两项）",
      [r["id"] for r in rows],
      ["edit-tomorrow", "omnifocus", "postsession", "tomorrow-plan"])
check("每项都有解释", all(r["desc"] for r in rows), True)
check("没改过也列出来（并标出默认值）",
      [(r["value"], r["changed"]) for r in rows],
      [("on", False), ("on", False), ("on", False), ("", False)])
store = cfgs.SettingStore()
check("默认 edit-tomorrow = on", store.get_bool("edit-tomorrow"), True)
check("set off", store.set("edit-tomorrow", "off"), "off")
check("落盘了", store.get("edit-tomorrow"), "off")
check("界面里标成「改过的」",
      [r["changed"] for r in store.rows()], [True, False, False, False])
check("改回默认值 = 不算改过（跟着以后的默认走）",
      (store.set("edit-tomorrow", "on"), store.is_set("edit-tomorrow")), ("on", False))
check("bool 只认 on/off",
      cfgs.coerce_value("edit-tomorrow", "yes"), "on")
try:
    cfgs.coerce_value("edit-tomorrow", "maybe")
    check("乱写被拒", False, True)
except ValueError as e:
    check_true("乱写给了人话提示", "on/off" in str(e))
try:
    cfgs.coerce_value("nope", "1")
    check("未知设置项被拒", False, True)
except ValueError as e:
    check_true("未知设置项点名现有项", "edit-tomorrow" in str(e))
check("重复规格：mon@22:15", cfgs.coerce_value("tomorrow-plan", "周一@22:15"), "mon@22:15")
check("重复规格：空 = 清掉", cfgs.coerce_value("tomorrow-plan", "off"), "")
check("reset 回到默认", store.set("tomorrow-plan", "mon@22:15") and
      store.reset("tomorrow-plan"), "")

# ---------------------------------------------------------------- 设置视图

print("== 设置视图：列出全部设置 + set 命令 ==")
wd = mkwarden()
check("设置是第五个视图", wd.views[4].name, "设置")
wd.go_to_view(w.SettingsView)
check("切到设置视图", wd.view.name, "设置")
out = render(wd, width=160)
check("表格里有当前值列", "当前值" in out, True)
check("表格里有默认值列", "默认" in out, True)
check("两个设置项都列出来",
      ("edit-tomorrow" in out and "tomorrow-plan" in out), True)
check("每项都带解释", "晚上 11 点也能随便排明天早上 10 点" in out, True)
check("给了 set 的写法示例", "set edit-tomorrow" in out, True)
check("set 改值", wd.handle("set tomorrow-plan mon@22:15"), True)
check("值落盘", wd.settings.get("tomorrow-plan"), "mon@22:15")
check_true("提示里教怎么建重复任务",
           any("--repeat mon@22:15" in m[2] for m in wd.messages))
check("set 不写 value = 恢复默认", wd.handle("set tomorrow-plan"), True)
check("回到默认（空）", wd.settings.get("tomorrow-plan"), "")
out = render(wd, width=160)
check("改过的项标 *", "* " in out, False)
wd.handle("set edit-tomorrow off")
out = render(wd, width=160)
check("改过的项标 *", "* edit-tomorrow" in out, True)
wd.handle("set edit-tomorrow on")

# ---------------------------------------------------------------- 状态框

print("== 状态框：契约（data.csv 周几）+ 警告，一个平滑边框的框 ==")
write_csv(["# test schedule", "%s,21:00,25" % WD])
wd = mkwarden()
wd.reload_entries()
wd.refresh_warnings(force=True)
box = wd.shell.status_box()
text = box.text()
check_true("有平滑边框（重线 ━ + 竖线 ┃）", text.splitlines()[0].startswith("┏━━ 状态"))
check_true("横竖和四个角都是 box-drawing 重线",
           set("".join(text.splitlines())) >= set("━┃┏┓┗┛"))
check("框线那行没有 ASCII 的 + - | 残留",
      [ch for ch in "+-|" if ch in text.splitlines()[0]], [])
check_true("框里有契约那行", "契约" in text and "data.csv" in text)
check_true("框里有警告", "!" in text or "x" in text)
check_true("框里有怎么修的提示", "→" in text)
check_true("状态框只在「契约」视图显示", wd.shell.warnings_allowed())
wd.go_to_view(w.StatsView)
check("统计视图不显示警告", wd.shell.warnings_allowed(), False)
wd.go_to_view(w.ContractView)
check("回契约视图又显示", wd.shell.warnings_allowed(), True)
check("warn off 关掉", wd.handle("warn off"), True)
check("关掉后警告不在框里", "!" not in wd.shell.status_box_text(), True)
check("框还在（契约那行是视图自己的）",
      "契约" in wd.shell.status_box_text(), True)
check("warn on 打开", wd.handle("warn on"), True)
check("打开后警告回来", "!" in wd.shell.status_box_text(), True)

# 永不重订名单那一行已经被警告取代（名单本体只在统计视图逐行列）
ks._save_json(ks.VERDICTS_FILE, {"abandoned": {
    "2026-01-09T09:00": {"at": "2026-01-09T10:30:00", "why": "walk-of-shame"}}})
wd = mkwarden()
wd.reload_entries()
out = render(wd)
check("契约视图不再出现「永不重订名单（N 条）」", "永不重订名单" not in out, True)
check("但状态框还在", "┏━━ 状态" in out, True)

# ---------------------------------------------------------------- edit-tomorrow

print("== edit-tomorrow：今天可以先安排明天 ==")
wd = mkwarden()
tomorrow = TODAY + datetime.timedelta(days=1)
now = datetime.datetime.now()
# 造一场「明天但现在就在 24 小时窗口里」的 session：明天 00:15
start = datetime.datetime.combine(tomorrow, datetime.time(0, 15))
check("明天 00:15 本来在执行区（24h 窗口内）",
      sch.zone_name(start, now), "locked")
check("edit-tomorrow 开着 → 明天按草稿区算",
      wd.effective_zone(sch.zone_name(start, now), tomorrow), "draft")
wd.handle("set edit-tomorrow off")
check("关掉后回到执行区",
      wd.effective_zone(sch.zone_name(start, now), tomorrow), "locked")
wd.handle("set edit-tomorrow on")
check("今天不受影响", wd.effective_zone("locked", TODAY), "locked")
check("过去的日子不受影响",
      wd.effective_zone("locked", TODAY - datetime.timedelta(days=1)), "locked")

# 真实行：明天 00:15 的契约，草稿区 → cancel 不用敲宣言
write_csv(["# test", "%s,00:15,25" % tomorrow.isoformat()])
wd = mkwarden()
wd.reload_entries()
wd.api_rows = []
wd.rows_source = "bridge"
wd.day_offset = 1
wd.rebuild_rows()
check("这行被标成草稿区", wd.rows[0]["zone"], "draft")
check_true("原始判据仍是 locked（可追溯）", wd.rows[0]["zone_raw"] == "locked")
builtins_input = __import__("builtins").input
__import__("builtins").input = lambda prompt="": "绝不敲宣言"
try:
    ok = wd.cmd_cancel(["1"])
finally:
    __import__("builtins").input = builtins_input
check("草稿区取消：直接成功、不用宣言", ok, True)
check("data.csv 那行没了", "00:15" not in sch.read_text(), True)

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
