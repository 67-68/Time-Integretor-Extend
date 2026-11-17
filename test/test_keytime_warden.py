#!/usr/bin/env python3
"""Key Time 在 warden 里的那一半：整页接管 / 看板模板 / 每帧节拍 / ack / 规则命令。

    python3 test/test_keytime_warden.py

不真的置顶 iTerm、不真的发声、不真的推手机：summon / say / push 全部注入。
"""
import datetime
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="keytime-warden-test-")
os.environ["TI_LOG_DIR"] = os.path.join(TMP, "logs")   # 测试日志别写进真 temp/logs
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
    ("TI_SINS_FILE", "sins.json"),
    ("TI_FAILED_ACTIONS", "failed-actions.json"),
    ("TI_KEYTIME_JSON", "keytime.json"),
    ("TI_KEYTIME_STATE", "keytime-state.json"),
    ("TI_KEYTIME_DEVICES", "devices.json"),
    ("TI_KEYTIME_RULES", "keytime-rules.toml"),
):
    os.environ[_name] = os.path.join(TMP, _file)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
kt = w.kt
kr = kt.kr
ORIG_KT_SUMMON = kt.summon

ks = w.ks
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.LOCK_FILE = os.path.join(TMP, "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(TMP, "keep_session-expected.json")
ks.TUI_STATE_FILE = os.path.join(TMP, "warden-tui.json")

fails = []


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_in(label, needle, haystack):
    ok = needle in (haystack or "")
    if not ok:
        fails.append("%s\n      缺 %r in %r" % (label, needle, haystack))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_not_in(label, needle, haystack):
    ok = needle not in (haystack or "")
    if not ok:
        fails.append("%s\n      不该出现 %r in %r" % (label, needle, haystack))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


class FakeBridge(object):
    def stop(self):
        pass

    def browser_state(self):
        return (False, None, "")


def render(wd, obj):
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    console.print(obj)
    return console.file.getvalue()


TODAY = datetime.date.today()


def at(minutes, base=None):
    base = base or datetime.datetime.combine(TODAY, datetime.time(16, 0))
    return base + datetime.timedelta(minutes=minutes)


def write_download(rows):
    data = {"version": 1, "generated_at": datetime.datetime.now().isoformat(),
            "start_date": TODAY.isoformat(), "days": 8, "events": [], "periods": [],
            "all_calendars": ["工作", "空闲", "运动", "通勤", "休息"], "mode": "profiles",
            "time_blocks": rows}
    with open(os.environ["TI_CALENDAR_DOWNLOAD"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def kt_row(start, end, calendar="休息", date=None, title=""):
    return {"date": date or TODAY.isoformat(), "start": start, "end": end,
            "calendar": calendar, "title": title or calendar, "all_day": False}


write_download([kt_row("16:00", "17:00", "休息", title="强制断电")])


def fresh(now=None):
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.keytime_now = now
    wd.keytime_tick_enabled = True     # 非 tty 的测试进程里手动打开节拍
    wd.keytime_reload(force=True)
    return wd


print("== 规则文件自动生成 ==")
wd = fresh(at(-30))
check("规则文件被写出来了", os.path.exists(os.environ["TI_KEYTIME_RULES"]), True)
check("warden 里有规则集", [r.id for r in wd.keytime_rules.rules],
      ["exercise", "commute", "rest", "generic"])
check("规则文件没有错误", wd.keytime_rules_error, "")

print("== 到点前：只有一行淡红提示 ==")
wd = fresh(at(-30))
check("还没到点没有面板", wd.render_keytime_panel(), None)
line = wd.render_keytime_line()
check_in("淡红一行有 KEY TIME", "KEY TIME", line.plain if line else "")
check_in("提示多少分钟后", "30 分钟后", line.plain if line else "")
check_in("带时间块名字", "强制断电", line.plain if line else "")

print("== 违约中：整页接管（不再混在契约界面里）==")
wd = fresh(at(12))
panel = wd.render_keytime_panel()
check("有面板", panel is not None, True)
text = render(wd, panel)
check_in("面板标题是规则 id", "KEY TIME · rest", text)
check_in("模板标题", "KEY TIME 违约警报", text)
check_in("超时分钟数", "已超时：12 分钟", text)
check_in("应于 16:00", "应于 16:00 启动", text)
check_in("休息模板", "生理算力坍塌后果", text)
check_in("没有总目标给占位", "keytime goal", text)
check("淡红一行让位给面板", wd.render_keytime_line(), None)
check("确认前：整页只有这块暂停牌", wd.render_keytime_takeover() is not None, True)
frame_text = render(wd, wd.frame())
check_in("整页里有模板", "生理算力坍塌后果", frame_text)
check_not_in("整页里没有契约表", "The Contract Table", frame_text)
check_not_in("整页里没有任务面板", "Task · 任务", frame_text)

print("== 每帧节拍：置顶 + 播报 ==")
spoken, summoned, pushed = [], [], []


class FakeAnnouncer(object):
    def __init__(self):
        self.stopped = 0

    def speak(self, text):
        spoken.append(text)

    def stop(self):
        self.stopped += 1


wd = fresh(at(1))
wd._keytime_announcer = FakeAnnouncer()
kt.summon = lambda *a, **k: summoned.append(1)
kt.left_mac = lambda *a, **k: False
kt.push_all = lambda *a, **k: pushed.append(a)
wd._keytime_tick()
check("到点就播报一次", len(spoken), 1)
check_in("播报里有分钟数", "休息超时 1 分钟", spoken[0])
for _ in range(20):
    if summoned:
        break
    time.sleep(0.02)
check("置顶了一次", len(summoned), 1)
check("人在 Mac 前不推手机", pushed, [])

# 节拍没到：不重复播报
wd._keytime_tick()
check("同一分钟内不重复播", len(spoken), 1)

# 节拍到了：再播一次
wd.keytime_next_announce = 0
wd.keytime_next_tick = 0
wd._keytime_tick()
check("下一分钟再播", len(spoken), 2)

print("== 置顶只聚焦，不新建窗口 ==")
kt.summon = ORIG_KT_SUMMON          # 前面节拍测试把它换成了假的，这里换回来
captured = {}
real_summon = ks.summon_warden


def fake_summon(**kwargs):
    captured.update(kwargs)
    return True


ks.summon_warden = fake_summon
check("summon 成功", kt.summon(), True)
check("focus_only=True（只会聚焦已有 warden）", captured.get("focus_only"), True)
check("quiet=True（不往 TUI stdout 打日志）", captured.get("quiet"), True)
ks.summon_warden = real_summon
kt.summon = lambda *a, **k: summoned.append(1)   # 后面的节拍测试还用这个假函数

print("== 人不在 Mac 前：每分钟推手机 ==")
kt.save_devices([{"name": "iphone", "key": "k1"}])
kt.left_mac = lambda *a, **k: True
wd._keytime_announcer = FakeAnnouncer()
wd.keytime_next_announce = 0
wd.keytime_next_tick = 0
pushed.clear()
wd._keytime_tick()
for _ in range(40):
    if pushed:
        break
    time.sleep(0.02)
check("推了一次", len(pushed), 1)
check("记下「确认时人不在」", wd.keytime_acked_left_mac, True)

print("== keytime ack ==")
wd.keytime_next_tick = 0
check("确认成功", wd.keytime_ack(), True)
state = kt.load_state()
key = "%sT16:00" % TODAY.isoformat()
check("状态文件里有 ack", list(state["acked"]), [key])
check("acks 记了 left_mac", state["acked"][key]["left_mac"], True)
check("确认后停播报", wd._keytime_announcer, None)
check("确认后整页接管解除", wd.render_keytime_takeover(), None)
panel = render(wd, wd.render_keytime_panel())
check_in("面板显示已确认", "已确认", panel)
check_in("确认后不再喊超时", "计时器仍在走", panel)
frame_text = render(wd, wd.frame())
check_in("确认后回到正常看板（有契约表）", "The Contract Table", frame_text)

print("== 确认后：停止播报，置顶照旧，5 分钟后再推 ==")
spoken.clear()
wd._keytime_announcer = FakeAnnouncer()
summoned.clear()
wd.keytime_next_tick = 0
wd.keytime_next_announce = 0
wd.keytime_next_summon = 0
pushed.clear()
wd._keytime_tick()
check("确认后不再播报", spoken, [])
for _ in range(20):
    if summoned:
        break
    time.sleep(0.02)
check("确认后仍然置顶", len(summoned), 1)
for _ in range(40):
    if pushed:
        break
    time.sleep(0.02)
check("确认后 5 分钟档会再推（这里首次立即推）", len(pushed), 1)
check("再推用的是 acked 参数",
      kt.push_params(wd.keytime_events, mode="acked", seq=key)["level"], "passive")

print("== 名字匹配优先：运动模板 ==")
# 清掉上一段留下的 ack，否则这一行会显示「已确认」而不是「未来」
if os.path.exists(os.environ["TI_KEYTIME_STATE"]):
    os.remove(os.environ["TI_KEYTIME_STATE"])
write_download([kt_row("16:00", "17:00", "运动", title="运动")])
wd = fresh(at(5))
wd._keytime_todays_sessions = lambda now: [
    {"start": at(30), "duration": 50, "tasks": ["[Run] 物理 2023 P1"], "label": "16:30"}]
text = render(wd, wd.render_keytime_panel())
check_in("命中 exercise 规则", "肉体僵死警报", text)
check_in("用下一个 session 的任务", "后续的【[Run] 物理 2023 P1】", text)
check_in("超时 5 分钟", "已粘在椅子上超时：5 分钟", text)

print("== 没命中规则的时间块不报警 ==")
write_download([kt_row("16:00", "17:00", "工作", title="SAT 3H")])
wd = fresh(at(12))
check("工作块 → 没有面板", wd.render_keytime_panel(), None)
check("工作块 → 没有那行", wd.render_keytime_line(), None)
check("工作块 → 没有整页接管", wd.render_keytime_takeover(), None)

print("== 数据视图 open keytime ==")
write_download([kt_row("16:00", "17:00", "休息", title="强制断电")])
wd = fresh(at(-30))
wd._switch_data_model("keytime")
table = wd.view.render()
text = render(wd, table[0])
check_in("表头是 Key Time", "Key Time", text)
check_in("有那一行", "强制断电", text)
check_in("有未来状态", "未来", text)
check_in("有规则列", "rest", text)

print("== keytime 命令族 ==")
wd = fresh(at(12))
wd.cmd_keytime([])
check("keytime 能列出来", any("休息" in m[2] for m in wd.messages), True)
rules = wd.cmd_keytime(["rules"])
check("rules 返回 4 条", len(rules), 4)
wd.cmd_keytime(["rules", "reset"])
check("rules reset 后规则文件还在", os.path.exists(os.environ["TI_KEYTIME_RULES"]), True)
devices = wd.cmd_keytime(["device", "add", "iphone", "abc123"])
check("加设备", [d["name"] for d in devices], ["iphone"])
check("device 落盘", [d["name"] for d in kt.load_devices()], ["iphone"])
check("device list 返回设备", [d["name"] for d in wd.cmd_keytime(["device"])], ["iphone"])
pushed.clear()
kt.push_all = lambda devices, title, body, params=None, timeout=None: (
    pushed.append((devices, title)) or [("iphone", True, "ok")])
results = wd.cmd_keytime(["device", "test", "iphone"])
check("device test 真发了一发", len(pushed), 1)
check("测试推送成功", results[0][1], True)
wd.cmd_keytime(["set", "volume", "25"])
check("set 写了配置", kt.load_config()["volume"], 25)
try:
    wd.cmd_keytime(["set", "calendars", "通勤,休息"])
    check("set calendars 被拒绝", False, True)
except ValueError as e:
    check_in("set calendars 提示改用规则文件", "keytime-rules.toml", str(e))
wd.cmd_keytime(["off"])
check("off 关掉报警", kt.load_config()["enabled"], False)
wd.cmd_keytime(["on"])
check("on 打开报警", kt.load_config()["enabled"], True)

print("== 规则文件坏了也不崩 ==")
with open(os.environ["TI_KEYTIME_RULES"], "w", encoding="utf-8") as f:
    f.write("[[rule]]\nid = x\nname = \"不存在\"\ntext = \"\"\"没闭上\n")
wd = fresh(at(12))
check("坏规则 → 没有 key time 也不崩", wd.keytimes, [])
check("坏规则 → 有错误提示", bool(wd.keytime_rules_error), True)
check("坏规则 → 面板空", wd.render_keytime_panel(), None)

print("== 文件不存在时不崩 ==")
kr.save_default_rules()
os.remove(os.environ["TI_CALENDAR_DOWNLOAD"])
wd = fresh(at(12))
check("没有中间层 → 没面板", wd.render_keytime_panel(), None)
check("没有中间层 → 没那行", wd.render_keytime_line(), None)
check("没有中间层 → 没有整页", wd.render_keytime_takeover(), None)
wd._keytime_tick()
check("没有中间层 → tick 也不崩", True, True)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    import sys
    sys.exit(1)
print("全绿。")
