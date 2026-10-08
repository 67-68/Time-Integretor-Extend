#!/usr/bin/env python3
"""warden.py 回归测试：停用密码 + 命令解析 + cancel 的「地狱级阻力」拦不拦得住。

    python3 test/test_warden.py        # 全绿退出 0，有失败退出 1

不联网、不碰浏览器、不写仓库里的任何文件（warden.json 指到临时目录）。
"""
import builtins
import datetime
import importlib.util
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(label, got, want):
    ok = got == want
    if not ok:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))
    print("%s %s" % ("ok  " if ok else "FAIL", label))


def check_true(label, got):
    check(label, bool(got), True)


w = load("warden", "warden.py")
from rich.console import Console  # noqa: E402  (warden 已经把 temp/pylibs 塞进 sys.path)

TMP = tempfile.mkdtemp(prefix="warden-test-")
w.WARDEN_JSON = os.path.join(TMP, "warden.json")


# ---------------------------------------------------------------- 假 bridge

class FakeBridge(object):
    """记下所有调用，按需返回预设结果；不碰任何真东西。"""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.last_idle_close = None
        self.browser = False

    def call(self, cmd, timeout=240, **kw):
        self.calls.append((cmd, kw))
        if cmd == "ping":
            return {"pong": True, "browser": self.browser}
        if cmd == "release":
            was = self.browser
            self.browser = False
            return {"released": was}
        if cmd in self.replies:
            r = self.replies[cmd]
            return r(kw) if callable(r) else r
        if cmd == "snapshot":
            return {"date": "2026-10-08", "now": NOW_MS, "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, self.browser, ""

    def stop(self):
        pass


def snapshot_of(rows):
    return {"date": "2026-10-08", "now": NOW_MS, "count": len(rows), "sessions": rows}


def mkrow(idx_hour, quiet=False, title="", partner=True, dur=50):
    s = datetime.datetime(2026, 10, 8, idx_hour, 0)
    e = s + datetime.timedelta(minutes=dur)
    sm = int(s.timestamp() * 1000)
    em = int(e.timestamp() * 1000)
    return {
        "startMs": sm, "endMs": em,
        "startLabel": s.strftime("%H:%M"), "endLabel": e.strftime("%H:%M"),
        "durationMin": dur, "id": "id-%02d" % idx_hour,
        "title": title, "quiet": quiet,
        "matched": partner, "partnerId": "p" if partner else None,
        "phase": w.Warden.phase_of(sm, em, NOW_MS),
    }


NOW_MS = int(datetime.datetime(2026, 10, 8, 12, 0).timestamp() * 1000)


def mkwarden(bridge=None, offline=False):
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    wd = w.Warden(console, bridge or FakeBridge(), no_clear=True, offline=offline)
    return wd


# ---------------------------------------------------------------- 密码规则

print("== 密码复杂度 ==")
check("空密码不合格", bool(w.password_problems("")), True)
check("11 位不合格", bool(w.password_problems("Abcdefg1234")), True)
check("12 位但只有 2 类不合格", bool(w.password_problems("abcdefghijkl")), True)
check("12 位只有小写+数字 = 2 类", bool(w.password_problems("abcdefgh1234")), True)
check("12 位 小写+大写+数字 = 3 类合格", w.password_problems("Abcdefgh1234"), [])
check("12 位 小写+数字+符号 = 3 类合格", w.password_problems("abcdefgh123!"), [])
check("长但单一类不合格", bool(w.password_problems("aaaaaaaaaaaaaaaaaaaa")), True)
check("刚好 12 位 4 类合格", w.password_problems("Abcdefgh12!x"), [])

print("== 密码存取 ==")
check("没设过时 load 是 None", w.load_password_record(), None)
check("没设过时 verify 是 False", w.verify_password("whatever-not-set"), False)
PW = "Corr3ct-Horse-Batt!ery"
w.save_password(PW)
rec = w.load_password_record()
check_true("存下来有 hash", rec and "hash" in rec and "salt" in rec)
check("明文没落盘", PW in open(w.WARDEN_JSON, encoding="utf-8").read(), False)
check("盐不是空的", len(rec["salt"]) >= 32, True)
check_true("迭代次数够高", int(rec["iterations"]) >= 100000)
check("正确密码通过", w.verify_password(PW), True)
check("错密码拒绝", w.verify_password(PW + "x"), False)
check("空密码拒绝", w.verify_password(""), False)
check("大小写敏感", w.verify_password(PW.lower()), False)
check("文件权限 600", oct(os.stat(w.WARDEN_JSON).st_mode & 0o777), "0o600")

print("== 改密码要用旧密码 ==")
wd = mkwarden(offline=True)
wd.bridge.calls = []
w.read_secret = lambda prompt="": "wrong-old-password"
check("旧密码错 → 不改", wd.cmd_set_password(), False)
check("旧密码错 → 密码没变", w.verify_password(PW), True)
seen = []


def fake_secret(prompt=""):
    seen.append(prompt)
    if "当前" in prompt:
        return PW
    return "N3w-Password!xyz"


w.read_secret = fake_secret
check("旧密码对 → 改成功", wd.cmd_set_password(), True)
check("新密码生效", w.verify_password("N3w-Password!xyz"), True)
check("旧密码失效", w.verify_password(PW), False)

print("== 弱密码拒绝 ==")
wd = mkwarden(offline=True)
w.read_secret = lambda prompt="": "weak"
check("弱新密码 → 拒绝", wd.cmd_set_password(), False)
w.read_secret = lambda prompt="": PW
check("两次不一致 → 拒绝", wd.cmd_set_password(), False)

# 恢复一个已知密码，供后面的 stop 测试用
w.save_password("Warden-Test-Pass!42")


# ---------------------------------------------------------------- 命令解析

print("== 命令解析 ==")
wd = mkwarden(offline=True)
wd.rows = [mkrow(9), mkrow(15, quiet=True, title="hi"), mkrow(20, partner=False)]
wd.rows_source = "bridge"
wd.rows_at = 0

try:
    wd.pick("99")
    check("pick 99 应该报错", False, True)
except ValueError as e:
    check("pick 99 报错内容", "没有 ID 99" in str(e), True)
try:
    wd.pick("abc")
    check("pick abc 应该报错", False, True)
except ValueError as e:
    check("pick abc 报错内容", "得是数字" in str(e), True)
check("pick 1 命中第一行", wd.pick("1")[1]["startLabel"], "09:00")

check("title 少参数报错", wd.handle('title 2'), True)
check("title 少参数不进 bridge", len(wd.bridge.calls), 0)
check("未知命令不崩", wd.handle("frobnicate"), True)
check("quit 返回 False", wd.handle("quit"), False)
check("q 返回 False", wd.handle("q"), False)
check("空行返回 True", wd.handle("   "), True)
check("引号没配对不崩", wd.handle('title 2 "unclosed'), True)

# 带引号的标题要整段传过去
wd.bridge.calls = []
wd.handle('title 2 "Math Past Paper"')
title_calls = [c for c in wd.bridge.calls if c[0] == "title"]
check("title 调用了一次", len(title_calls), 1)
check("title 文本正确", title_calls[0][1].get("title"), "Math Past Paper")
check("title 带上了 meetingId", title_calls[0][1].get("meetingId"), "id-15")

# 不带引号也要能拼
wd.bridge.calls = []
wd.handle("title 2 Math Past Paper")
title_calls = [c for c in wd.bridge.calls if c[0] == "title"]
check("无引号也能拼", title_calls[0][1].get("title"), "Math Past Paper")

print("== mute ==")
wd.bridge.calls = []
wd.handle("mute 1")          # 原本 quiet=False → 取反成 True
mute_calls = [c for c in wd.bridge.calls if c[0] == "mute"]
check("mute 调用了一次", len(mute_calls), 1)
check("mute 取反 = True", mute_calls[0][1].get("quiet"), True)
wd.bridge.calls = []
wd.handle("mute 2")          # 原本 quiet=True → 取反成 False
check("mute 取反 = False", [c for c in wd.bridge.calls if c[0] == "mute"][0][1].get("quiet"), False)
wd.bridge.calls = []
wd.handle("mute 2 off")
check("mute 显式 off", [c for c in wd.bridge.calls if c[0] == "mute"][0][1].get("quiet"), False)
wd.bridge.calls = []
wd.handle("mute 1 on")
check("mute 显式 on", [c for c in wd.bridge.calls if c[0] == "mute"][0][1].get("quiet"), True)
wd.bridge.calls = []
wd.handle("mute 1 banana")
check("mute 乱参数不调 bridge", len([c for c in wd.bridge.calls if c[0] == "mute"]), 0)
check("title 超 100 字被拒", wd.handle('title 1 "%s"' % ("x" * 101)), True)
check("超长 title 不调 bridge", len([c for c in wd.bridge.calls if c[0] == "title"]), 0)

print("== 快照行改不了 ==")
wd2 = mkwarden(offline=True)
wd2._fallback_rows("测试")
if wd2.rows:
    wd2.bridge.calls = []
    import json as _json
    # 手搓一行没有 id 的（模拟 state.json 快照）
    wd2.rows = [dict(wd2.rows[0])]
    wd2.rows[0]["id"] = None
    wd2.handle("mute 1")
    check("没有 session id 就不发请求", len([c for c in wd2.bridge.calls if c[0] == "mute"]), 0)
    wd2.handle("cancel 1")
    check("没有 session id 就不取消", len([c for c in wd2.bridge.calls if c[0] == "cancel"]), 0)


# ---------------------------------------------------------------- cancel 阻力

print("== cancel 地狱级阻力 ==")


def cancel_setup():
    wd = mkwarden(offline=True)
    wd.rows = [mkrow(20), mkrow(21, partner=False)]
    wd.rows_source = "bridge"
    wd.rows_at = 0
    return wd


# 1) 打错时间 → 必须放弃
wd = cancel_setup()
wd.bridge.calls = []
_orig_input = builtins.input
builtins.input = lambda prompt="": "20:01"
wd.handle("cancel 1")
check("时间打错 → 不 cancel", len([c for c in wd.bridge.calls if c[0] == "cancel"]), 0)

# 2) 直接 EOF / Ctrl+C → 必须放弃
builtins.input = lambda prompt="": (_ for _ in ()).throw(EOFError())
wd.bridge.calls = []
wd.handle("cancel 1")
check("Ctrl-D → 不 cancel", len([c for c in wd.bridge.calls if c[0] == "cancel"]), 0)

builtins.input = lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt())
wd.bridge.calls = []
wd.handle("cancel 1")
check("Ctrl-C → 不 cancel", len([c for c in wd.bridge.calls if c[0] == "cancel"]), 0)

# 3) 时间打对 → 过冷静期 → 真 cancel
builtins.input = lambda prompt="": "20:00"
w.CANCEL_COOLDOWN = 0            # 测试别真等 6 秒
wd.bridge.calls = []
wd.handle("cancel 1")
cancel_calls = [c for c in wd.bridge.calls if c[0] == "cancel"]
check("时间打对 → cancel 一次", len(cancel_calls), 1)
check("cancel 带对了 startMs", cancel_calls[0][1].get("startMs"), wd.rows[0]["startMs"])
check("cancel 带上了 meetingId", cancel_calls[0][1].get("meetingId"), "id-20")

# 4) 已结束的场次不给取消
w.CANCEL_COOLDOWN = 0
wd = cancel_setup()
wd.rows = [mkrow(9)]      # 09:00，NOW 是 12:00，已结束
wd.bridge.calls = []
builtins.input = lambda prompt="": "09:00"
wd.handle("cancel 1")
check("已结束 → 不 cancel", len([c for c in wd.bridge.calls if c[0] == "cancel"]), 0)

builtins.input = _orig_input

# ---------------------------------------------------------------- daemon 启停

print("== daemon 启停 ==")
ran = []


def fake_run_ks(*args, **kw):
    ran.append(args)
    return 0, "daemon 已启动 (pid=123)", ""


w.run_keep_session = fake_run_ks

# start 之前必须让 bridge 放开浏览器
wd = mkwarden(offline=True)
wd.bridge.browser = True
wd.bridge.calls = []
check("start 成功", wd.cmd_start(), True)
check("start 先 release 了浏览器", wd.bridge.calls[0][0], "release")
check("start 真的调了 keep_session", ran[-1], ("daemon", "start"))

# stop 要密码：密码不对 → 绝不真停
ran = []
wd = mkwarden(offline=True)
seq = ["wrong-password-here", "Warden-Test-Pass!42"]
w.read_secret = lambda prompt="": seq.pop(0) if seq else "x"
check("第一次密码错但第二次对 → 能停", wd.cmd_stop(), True)
check("stop 调了 keep_session", ran[-1], ("daemon", "stop"))

ran = []
wd = mkwarden(offline=True)
w.read_secret = lambda prompt="": "definitely-wrong"
check("三次全错 → 停不了", wd.cmd_stop(), False)
check("密码全错 → 没调 keep_session", ran, [])

ran = []
wd = mkwarden(offline=True)
seq = ["nope1", "nope2", "nope3"]
w.read_secret = lambda prompt="": seq.pop(0)
check("三次全错返回值 False", wd.cmd_stop(), False)
check("三次全错 → 没调 keep_session", ran, [])

# 还没设过密码 → 先设再说（弱密码不给过）
ran = []
os.remove(w.WARDEN_JSON)
wd = mkwarden(offline=True)
w.read_secret = lambda prompt="": "short"
check("首次停用 + 弱密码 → 拒绝", wd.cmd_stop(), False)
check("弱密码没落盘", w.load_password_record(), None)
check("弱密码没调 keep_session", ran, [])

seq = ["Str0ng-Enough-Pass!", "Str0ng-Enough-Pass!"]
w.read_secret = lambda prompt="": seq.pop(0) if seq else "x"
check("首次停用 + 强密码 → 设完就能停", wd.cmd_stop(), True)
check("强密码落盘了", bool(w.load_password_record()), True)
check("首次停用确实调了 stop", ran[-1], ("daemon", "stop"))

# 恢复一个干净状态给真人用
os.remove(w.WARDEN_JSON)

print("== 请求号不能和 session 身份撞名（踩过的坑）==")
# 实测：call() 的 kw 里塞 id，会把请求号覆盖成 UUID → 桥执行了、响应也发了，
# 但这边永远等不到自己的请求号 → 卡死，而写入其实已经生效。必须硬拦。
_console = Console(file=io.StringIO(), width=100, force_terminal=False)
_shadow = w.Bridge(_console)
try:
    _shadow.call("mute", startMs=1, id="some-uuid", quiet=True)
    check("kw 里带 id 应该被拦住", False, True)
except w.BridgeError as e:
    check("kw 里带 id 被拦住", "不能有 id" in str(e), True)
check("拦住时不启子进程", _shadow.proc, None)

# ---------------------------------------------------------------- 渲染

print("== 渲染不炸 ==")
import shutil  # noqa: E402

for width in (40, 80, 160):
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    wd = w.Warden(console, FakeBridge(replies={
        "snapshot": lambda kw: snapshot_of([mkrow(9), mkrow(12, quiet=True, title="中" * 40), mkrow(23)])
    }), no_clear=True)
    wd.refresh()
    wd.paint()
    out = buf.getvalue()
    check("width=%d 渲染出表格" % width, "The Contract Table" in out, True)
    check("width=%d 渲染无异常" % width, "Traceback" in out, False)

buf = io.StringIO()
console = Console(file=buf, width=100, force_terminal=False)
wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
wd.rows = []
wd.paint()
check("空表不炸", "没有" in buf.getvalue(), True)

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
