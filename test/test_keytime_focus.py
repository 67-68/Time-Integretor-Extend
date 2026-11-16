#!/usr/bin/env python3
"""Key Time 的「锁死桌面（Focus.app）+ mac 横幅」那一半。

    python3 test/test_keytime_focus.py

不真的开关 Focus.app、不真的弹通知：焦点状态用一个临时 JSON 冒充，
focus_start / focus_stop / notify 全部注入假函数。
"""
import datetime
import importlib.util
import io
import json
import os
import plistlib
import tempfile
import time

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="keytime-focus-test-")
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
    ("TI_KEYTIME_FOCUS_CFG", "focus-default.cfg"),
):
    os.environ[_name] = os.path.join(TMP, _file)

spec = importlib.util.spec_from_file_location("warden", os.path.join(ROOT, "warden.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
kt = w.kt

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


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 假 Focus.app


def fake_profile(name, uid, apps):
    return {"name": name, "uniqueID": uid, "defaultBreaks": 0,
            "defaultIsLocked": False, "defaultSessionMinutes": 0,
            "blockedWebsites": [], "blockedApplications": apps}


def app_entry(path, bundle):
    return {"path": path, "bundleId": bundle,
            "shouldReopenWhenUnfocusing": True,
            "shouldHideInsteadOfQuit": True,
            "shouldAlwaysForceQuit": False,
            "performOnlySoftKillThenLeaveOpen": False}


def write_focus_cfg(path=None, active=None, sessions=None):
    cfg = {
        "activeProfileID": active or "ID-FOCUS",
        "activeSessions": sessions or [],
        "profiles": [
            fake_profile("Focus", "ID-FOCUS",
                         [app_entry("/Applications/Steam.app", "com.valvesoftware.steam")]),
            fake_profile("night purge", "ID-NIGHT", [
                app_entry("/Applications/iTerm.app", "com.googlecode.iterm2"),
                app_entry("/Applications/Steam.app", "com.valvesoftware.steam"),
            ]),
        ],
    }
    # 模板里带着一条半夜自动 session（真机上 night purge 就有一条 23:50→07:00）：
    # 新建 profile 绝不能继承它，否则半夜会自己锁死桌面。
    cfg["profiles"][1]["schedules"] = [{"isEnabled": True,
                                        "fromDate": {"hour": 23, "minute": 50},
                                        "toDate": {"hour": 7}}]
    with open(kt.focus_cfg_path(path), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return cfg


#: 假的 /Applications：两个 App，其中一个是要保留的 iTerm。
APPS_DIR = os.path.join(TMP, "Apps")
for _app, _bundle in (("iTerm.app", "com.googlecode.iterm2"),
                      ("Noise.app", "com.example.noise")):
    _plist = os.path.join(APPS_DIR, _app, "Contents", "Info.plist")
    os.makedirs(os.path.dirname(_plist), exist_ok=True)
    with open(_plist, "wb") as _f:
        plistlib.dump({"CFBundleIdentifier": _bundle}, _f)

print("== URL scheme ==")
check("普通 profile 名", kt.focus_url("focus", "keytime"),
      "focus://focus?profile=keytime")
check("中文 profile 名要转义", kt.focus_url("focus", "锁死 桌面"),
      "focus://focus?profile=%E9%94%81%E6%AD%BB%20%E6%A1%8C%E9%9D%A2")
check("不写 profile 就不带参数", kt.focus_url("unfocus", "  "), "focus://unfocus")

print("== 读 Focus.app 状态（只读配置，不用 AppleScript）==")
write_focus_cfg()
state = kt.focus_state()
check("默认没在 focus", state["focusing"], False)
check("默认读不到当前 profile", state["profile"], "")
check("profile 名字读出来了", kt.focus_profiles(), ["Focus", "night purge"])
write_focus_cfg(sessions=[{"profileName": "keytime", "startDate": "2026-11-15T21:00"}])
state = kt.focus_state()
check("有 activeSession = 正在锁", state["focusing"], True)
check("当前 profile 按 activeProfileID 找", state["profile"], "Focus")
check("读到开始时间", state["since"], "2026-11-15T21:00")

print("== 造一条「除 iTerm 全杀」的 profile ==")
write_focus_cfg()
profile = kt.build_focus_profile("keytime", "night purge", dirs=[APPS_DIR])
bundles = [a["bundleId"] for a in profile["blockedApplications"]]
check("名字对", profile["name"], "keytime")
check("uniqueID 是新造的（不跟模板撞）", profile["uniqueID"] != "ID-NIGHT", True)
check("保留模板里的已有条目", "com.valvesoftware.steam" in bundles, True)
check("扫到的新 App 也进黑名单", "com.example.noise" in bundles, True)
check("iTerm 不在黑名单里", "com.googlecode.iterm2" in bundles, False)
check("模板字段照抄（defaultBreaks 还在）", profile.get("defaultBreaks"), 0)
check("不继承模板的定时规则（否则半夜自己锁死）", profile.get("schedules"), [])

print("== 写进 Focus.app 配置（restart=False，只动文件）==")
write_focus_cfg()
ok, detail = kt.install_focus_profile("keytime", "night purge", restart=False,
                                      dirs=[APPS_DIR])
check("写成功", ok, True)
check_in("说明里有拦截条数", "拦", detail)
saved = kt.load_focus_cfg()
names = [p["name"] for p in saved["profiles"]]
check("老 profile 没被删", names, ["Focus", "night purge", "keytime"])
check("读回来条数一致", kt.verify_focus_profile("keytime"),
      len(profile["blockedApplications"]))
check("备份文件留了一份", os.path.exists(kt.focus_cfg_path() + ".keytime.bak"), True)
# 再写一次：不要叠加出两条同名 profile
ok, _ = kt.install_focus_profile("keytime", "night purge", restart=False, dirs=[APPS_DIR])
check("重复 setup 不会多出 profile",
      [p["name"] for p in kt.load_focus_cfg()["profiles"]], names)

print("== mac 横幅正文 ==")
check("多行压成一行", kt.notification_body("第一行\n\n第二行  "), "第一行 · 第二行")
long_text = "拖" * 400
body = kt.notification_body(long_text, limit=40)
check("超长截断到 limit", len(body), 40)
check("截断有省略号", body.endswith("…"), True)
check("空文本不炸", kt.notification_body(""), "")

print("== 发通知：dry-run 只说不做 ==")
lines = []
check("dry-run 返回成功", kt.notify("标题", "正文", "Sosumi", dry_run=True,
                                    log=lines.append), True)
check_in("说清了要做什么", "display notification", " ".join(lines))


# ---------------------------------------------------------------- warden 那一半

def write_download(rows):
    data = {"version": 1, "generated_at": datetime.datetime.now().isoformat(),
            "start_date": datetime.date.today().isoformat(), "days": 8,
            "events": [], "periods": [], "all_modes": [],
            "all_calendars": ["休息"], "mode": "profiles", "time_blocks": rows}
    with open(os.environ["TI_CALENDAR_DOWNLOAD"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


TODAY = datetime.date.today()
write_download([{"date": TODAY.isoformat(), "start": "16:00", "end": "17:00",
                 "calendar": "休息", "title": "强制断电", "all_day": False}])


class FakeBridge(object):
    def stop(self):
        pass

    def browser_state(self):
        return (False, None, "")


class FakeAnnouncer(object):
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        pass


def at(minutes):
    base = datetime.datetime.combine(TODAY, datetime.time(16, 0))
    return base + datetime.timedelta(minutes=minutes)


def fresh(now):
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.keytime_now = now
    wd.keytime_tick_enabled = True
    wd.keytime_reload(force=True)
    wd._keytime_announcer = FakeAnnouncer()
    return wd


# 假装：Focus.app 装好了，状态/开关都在测试里记录。
fake_state = {"focusing": False, "profile": ""}
calls = {"start": [], "stop": [], "notify": []}
kt.focus_available = lambda: True
kt.focus_app_running = lambda: False        # 测试绝不去 kill 用户真的 Focus.app
kt.focus_state = lambda path=None: dict(fake_state)
kt.focus_start = lambda config=None, profile=None, dry_run=False, log=None: (
    calls["start"].append(profile) or True)
kt.focus_stop = lambda config=None, profile=None, dry_run=False, log=None: (
    calls["stop"].append(profile) or True)
kt.summon = lambda *a, **k: True
kt.left_mac = lambda *a, **k: False
kt.notify = lambda title, body, sound=None, dry_run=False, log=None: (
    calls["notify"].append((title, body, sound)) or True)
kt.save_devices([])

print("== 到点：自动上锁 + 发 mac 横幅 ==")
wd = fresh(at(1))
wd._keytime_tick()
check("开了一次 Focus 锁", calls["start"], ["keytime"])
check("warden 记着自己上的锁", wd.keytime_focus_on, True)
check("mac 横幅也发了", len(calls["notify"]) >= 1, True)
check("横幅带声音", calls["notify"][0][2], "Sosumi")
check_in("横幅标题来自规则", "KEY TIME", calls["notify"][0][0])

print("== 用户手动解锁：下一分钟又被锁回去 ==")
wd.keytime_next_tick = 0
fake_state.update({"focusing": True, "profile": "keytime"})
wd._keytime_tick()
check("已经在锁就不重复开", len(calls["start"]), 1)
fake_state.update({"focusing": False, "profile": ""})
wd.keytime_next_tick = 0
wd.keytime_next_focus_check = 0.0
wd._keytime_tick()
check("被手动关掉后重新锁", calls["start"], ["keytime", "keytime"])

print("== 用户自己的 Focus session：不接管 ==")
fake_state.update({"focusing": True, "profile": "night purge"})
calls["start"].clear()
wd.keytime_next_tick = 0
wd.keytime_next_focus_check = 0.0
wd._keytime_tick()
check("别人的锁不抢", calls["start"], [])
check("也不认为锁是我们上的", wd.keytime_focus_on, False)

print("== ack：默认锁着不放 ==")
if os.path.exists(os.environ["TI_KEYTIME_STATE"]):
    os.remove(os.environ["TI_KEYTIME_STATE"])          # 清掉前面留下的 ack
fake_state.update({"focusing": True, "profile": "keytime"})
calls["stop"].clear()
wd = fresh(at(1))
wd.keytime_focus_on = True
wd.keytime_ack()
check("确认后不解锁（release_on_ack 默认 False）", calls["stop"], [])
check("确认后仍记着锁", wd.keytime_focus_on, True)

print("== release_on_ack=True：确认就解锁 ==")
if os.path.exists(os.environ["TI_KEYTIME_STATE"]):
    os.remove(os.environ["TI_KEYTIME_STATE"])
cfg = kt.load_config()
cfg["focus"]["release_on_ack"] = True
kt.save_config(cfg)
calls["stop"].clear()
wd = fresh(at(1))
fake_state.update({"focusing": True, "profile": "keytime"})
wd.keytime_focus_on = True
wd.keytime_ack()
check("配置要求确认即解锁", wait_for(lambda: calls["stop"]), True)
check("解锁的是我们的 profile", calls["stop"][-1], "keytime")
cfg = kt.load_config()
cfg["focus"]["release_on_ack"] = False
kt.save_config(cfg)

print("== 这一块结束：自动解锁 ==")
calls["stop"].clear()
fake_state.update({"focusing": True, "profile": "keytime"})
wd = fresh(at(1))
wd._keytime_tick()
check("先锁上", wd.keytime_focus_on, True)
wd.keytime_now = at(70)                    # 17:10，块已经结束
wd.keytime_next_tick = 0
fake_state.update({"focusing": True, "profile": "keytime"})
wd._keytime_tick()
check("结束后解锁", wait_for(lambda: calls["stop"]), True)
check("解完就不记账了", wd.keytime_focus_on, False)

print("== 关掉 focus / notify 就不动手 ==")
cfg = kt.load_config()
cfg["focus"]["enabled"] = False
cfg["notify"]["enabled"] = False
kt.save_config(cfg)
calls["start"].clear()
calls["notify"].clear()
fake_state.update({"focusing": False, "profile": ""})
wd = fresh(at(1))
wd._keytime_tick()
check("focus 关掉就不锁", wd.keytime_focus_on, False)
check("focus 关掉就不开 Focus.app", calls["start"], [])
check("notify 关掉就不弹横幅", calls["notify"], [])
cfg = kt.load_config()
cfg["focus"]["enabled"] = True
cfg["notify"]["enabled"] = True
kt.save_config(cfg)

print("== 命令族 ==")
wd = fresh(at(1))
state = wd.cmd_keytime(["focus"])
check("focus 返回状态字典", isinstance(state, dict), True)
check("focus setup 能建 profile",
      wd.cmd_keytime(["focus", "setup"]), True)
check("focus on 手动锁", wd.cmd_keytime(["focus", "on"]), True)
check("focus off 手动解锁", wd.cmd_keytime(["focus", "off"]), True)
check("notify 能发测试横幅", wd.cmd_keytime(["notify", "测试"]), True)
wd.cmd_keytime(["set", "focus.profile", "keytime2"])
check("set focus.profile 落盘", kt.load_config()["focus"]["profile"], "keytime2")
wd.cmd_keytime(["set", "notify.enabled", "off"])
check("set notify.enabled 落盘", kt.load_config()["notify"]["enabled"], False)
try:
    wd.cmd_keytime(["focus", "瞎写"])
    check("focus 瞎写被拒绝", False, True)
except ValueError as e:
    check_in("提示只认 setup/on/off", "setup", str(e))

print()
if fails:
    print("失败 %d 项：\n" % len(fails))
    for item in fails:
        print("  - %s" % item)
    raise SystemExit(1)
print("全绿。")
