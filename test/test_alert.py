#!/usr/bin/env python3
"""alert.py 回归测试：命令怎么拼、音量怎么夹、dry-run / 非 macOS 怎么降级。

    python3 test/test_alert.py        # 全绿退出 0，有失败退出 1

不真的放声音、不真的弹通知、不改系统音量：只断言「会执行哪些命令」。
"""
import contextlib
import importlib.util
import io
import os
import sys

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


a = load("alert", "alert.py")

# ---------------------------------------------------------------- 拼命令（纯函数）

print("== build_play_cmds ==")
check("默认播放：afplay，增益 1.0",
      a.build_play_cmds("x.mp3"),
      [["/usr/bin/afplay", "-v", "1.0000", "x.mp3"]])
check("volume=10 → 增益 0.1",
      a.build_play_cmds("x.mp3", volume=10),
      [["/usr/bin/afplay", "-v", "0.1000", "x.mp3"]])
check("system_volume 先设系统音量、再播放",
      a.build_play_cmds("x.mp3", volume=10, system_volume=10),
      [["/usr/bin/osascript", "-e", "set volume output volume 10"],
       ["/usr/bin/afplay", "-v", "0.1000", "x.mp3"]])
check("system_volume=0 也算给了（不能被当成没传）",
      a.build_play_cmds("x.mp3", system_volume=0)[0],
      ["/usr/bin/osascript", "-e", "set volume output volume 0"])
check("system_volume 四舍五入成整数",
      a.build_play_cmds("x.mp3", system_volume=12.6)[0],
      ["/usr/bin/osascript", "-e", "set volume output volume 13"])

print("== build_notify_cmd / build_volume_cmd ==")
cmd = a.build_notify_cmd("标题", "消息")
check("notify 走 osascript", cmd[:2], ["/usr/bin/osascript", "-e"])
check("notify 用 display notification", "display notification" in cmd[2], True)
check("notify 中文保留原文", "标题" in cmd[2] and "消息" in cmd[2], True)
check("notify 中文不转义成 \\uXXXX", "\\u" not in cmd[2], True)
check("notify 引号被转义",
      a.build_notify_cmd("t", 'a"b')[2].count('\\"'), 1)
check("notify sound name",
      'sound name "Basso"' in a.build_notify_cmd("t", "m", "Basso")[2], True)
check("volume 命令",
      a.build_volume_cmd(10),
      ["/usr/bin/osascript", "-e", "set volume output volume 10"])

print("== 音量越界 ==")
check_raises("volume -1 抛 ValueError", lambda: a.build_volume_cmd(-1))
check_raises("volume 101 抛 ValueError", lambda: a.build_volume_cmd(101))
check_raises("volume 'abc' 抛 ValueError", lambda: a.build_volume_cmd("abc"))
check_raises("play volume 200 抛 ValueError",
             lambda: a.build_play_cmds("x.mp3", volume=200))
check_raises("play system_volume -5 抛 ValueError",
             lambda: a.build_play_cmds("x.mp3", system_volume=-5))
check_raises("play 空路径抛 ValueError", lambda: a.build_play_cmds(""))


# ---------------------------------------------------------------- 执行（假 subprocess）

class FakeProc(object):
    DEVNULL = object()

    def __init__(self):
        self.runs = []
        self.popens = []

    def run(self, cmd, **kw):
        self.runs.append(list(cmd))
        return None

    def Popen(self, cmd, **kw):
        self.popens.append(list(cmd))
        return None


fake = FakeProc()
a.subprocess = fake
a.IS_MAC = True

print("== play（等待）==")
fake.runs[:] = []
check("play 返回 0", a.play("x.mp3", volume=10, system_volume=10), 0)
check("play 执行了两条命令", len(fake.runs), 2)
check("第一条设系统音量", fake.runs[0][-1], "set volume output volume 10")
check("第二条 afplay 增益 0.1",
      fake.runs[1], ["/usr/bin/afplay", "-v", "0.1000", "x.mp3"])

print("== play（background）==")
fake.runs[:] = []
fake.popens[:] = []
check("background play 返回 0", a.play("x.mp3", wait=False), 0)
check("background 用 Popen、不等", (len(fake.runs), len(fake.popens)), (0, 1))
check("Popen 的是 afplay", fake.popens[0][0], "/usr/bin/afplay")

print("== notify ==")
fake.runs[:] = []
check("notify 返回 0", a.notify("t", "m"), 0)
check("notify 执行一条", len(fake.runs), 1)
check("notify 是 osascript", fake.runs[0][0], "/usr/bin/osascript")

fake.runs[:] = []
check("notify + system_volume 返回 0", a.notify("t", "m", system_volume=10), 0)
check("notify + system_volume 先设音量再通知", len(fake.runs), 2)
check("第一条设系统音量", fake.runs[0][-1], "set volume output volume 10")
check("第二条才是通知", "display notification" in fake.runs[1][-1], True)

print("== 非 macOS 降级 ==")
a.IS_MAC = False
fake.runs[:] = []
check("非 macOS play 返回 0", a.play("x.mp3"), 0)
check("非 macOS 不执行命令", fake.runs, [])
a.IS_MAC = True


# ---------------------------------------------------------------- CLI

def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = a.main(argv)
    return code, buf.getvalue()


print("== CLI dry-run ==")
code, out = run_cli(["play", "x.mp3", "--volume", "10", "--dry-run"])
check("CLI play dry-run 返回 0", code, 0)
check("CLI 打印 DRY-RUN", "DRY-RUN" in out, True)
check("CLI 里增益 0.1", "0.1000" in out, True)

code, out = run_cli(["play", "x.mp3", "--system-volume", "10", "--dry-run"])
check("CLI system-volume 打印 set volume",
      "set volume output volume 10" in out, True)

code, out = run_cli(["notify", "-t", "分心警告", "-m", "违规 1/3", "--dry-run"])
check("CLI notify dry-run 返回 0", code, 0)
check("CLI notify 中文不转义", "\\u" not in out and "分心警告" in out, True)

code, out = run_cli(["notify", "-m", "x", "--system-volume", "10", "--dry-run"])
check("CLI notify system-volume 先设音量", "set volume output volume 10" in out, True)

code, out = run_cli(["volume", "10", "--dry-run"])
check("CLI volume dry-run 返回 0", code, 0)
check("CLI volume 打印 set volume", "set volume output volume 10" in out, True)

print("== CLI 参数错 ==")
code, _ = run_cli(["volume", "200"])
check("CLI volume 200 返回 2", code, 2)

print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
