#!/usr/bin/env python3
"""alert.py —— 放音乐 / 发通知 / 调音量的 macOS 小工具，音量由调用方决定。

谁用它
------
- `keep_session daemon` 的「分心看门狗」：没输入满一分钟就通知；到第 3 次就把
  系统音量拉到 10% 并播放 `data/audio/赵小六嘶吼.mp3`（见 feature_intents/idle_watchdog.md）。
- 任何 shell / agent：想响一声、想弹个通知、想把音量拧到某个数，都能直接调。

三种动作
--------
    python3 alert.py play  <file>  [--volume 0-100] [--system-volume 0-100] [--background]
    python3 alert.py notify        [--title T] [--message M] [--sound NAME] [--system-volume 0-100]
    python3 alert.py volume <0-100>

`play` 用 `afplay`，`notify` / `volume` 用 `osascript`，全是 macOS 自带、纯 stdlib。

音量语义（两个都能调，别混）
---------------------------
- `--volume N`        播放增益：`afplay -v N/100`。只影响这一次播放，不动系统音量。
- `--system-volume N` 系统输出音量：先 `osascript -e "set volume output volume N"` 再播放。
                      这就是「把音量拉到 10%」的字面实现；会改系统音量、且不还原。

非 macOS 或系统工具缺失时 **静默降级**（打一行提示、退出码 0），
告警工具不该把调用方（比如 daemon）搞挂。`--dry-run` 只打印要跑的命令。
"""
import argparse
import json
import subprocess
import sys

AFPLAY = "/usr/bin/afplay"
OSASCRIPT = "/usr/bin/osascript"
IS_MAC = sys.platform == "darwin"


# ---------------------------------------------------------------- 参数 / 拼命令


def _pct(value, what="音量"):
    """把音量参数夹到 0-100 的浮点数；非法就抛 ValueError。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是 0-100 的数字，收到 %r" % (what, value))
    if v < 0 or v > 100:
        raise ValueError("%s 必须在 0-100 之间，收到 %r" % (what, value))
    return v


def build_play_cmds(path, volume=100, system_volume=None):
    """play 动作要依次执行的命令列表。纯函数，测试直接断言它。

    system_volume 给了就先 osascript 把系统输出音量设成 N%，再用
    afplay -v volume/100 播 path。
    """
    if not path:
        raise ValueError("play 需要一个音频文件路径")
    cmds = []
    if system_volume is not None:
        cmds.append([OSASCRIPT, "-e",
                     "set volume output volume %d"
                     % int(round(_pct(system_volume, "系统音量")))])
    cmds.append([AFPLAY, "-v", "%.4f" % (_pct(volume) / 100.0), path])
    return cmds


def build_notify_cmd(title, message, sound=None):
    """notify 动作的 osascript 命令。

    转义必须用 json.dumps(..., ensure_ascii=False)：AppleScript 不认 `\\uXXXX`，
    默认的 ensure_ascii=True 会把中文变成 `\\u4f60...`，osascript 直接语法错、通知根本不弹。
    ensure_ascii=False 得到的是原样 UTF-8 + 合法的 `\\"` / `\\\\` 转义。
    """
    script = "display notification %s with title %s" % (
        json.dumps(message or "", ensure_ascii=False),
        json.dumps(title or "", ensure_ascii=False))
    if sound:
        script += " sound name %s" % json.dumps(sound, ensure_ascii=False)
    return [OSASCRIPT, "-e", script]


def build_volume_cmd(percent):
    """把系统输出音量设成 percent% 的命令。"""
    return [OSASCRIPT, "-e",
            "set volume output volume %d" % int(round(_pct(percent, "系统音量")))]


# ---------------------------------------------------------------- 执行


def _execute(cmds, dry_run=False, background=False):
    """依次执行命令。dry_run 只打印；background 时最后一条 Popen 不等待。

    永远不抛：失败打一行 stderr 并返回 1。返回 0 = 成功（或跳过）。
    """
    if dry_run:
        for c in cmds:
            print("DRY-RUN " + " ".join(c))
        return 0
    if not IS_MAC:
        print("[alert] 非 macOS，跳过（%s）" % (cmds[-1][0] if cmds else "-"), file=sys.stderr)
        return 0
    devnull = subprocess.DEVNULL
    try:
        # 先做的（如设系统音量）必须等；最后一条才是真正播放/通知
        for c in cmds[:-1]:
            subprocess.run(c, check=False, stdout=devnull, stderr=devnull)
        last = cmds[-1]
        if background:
            # 留在调用方的进程组里：daemon 按进程组 kill 时能一起收走，不留孤儿音频
            subprocess.Popen(last, stdout=devnull, stderr=devnull)
        else:
            subprocess.run(last, check=False, stdout=devnull, stderr=devnull)
        return 0
    except Exception as e:
        print("[alert] 执行失败: %s" % e, file=sys.stderr)
        return 1


def play(path, volume=100, system_volume=None, wait=True, dry_run=False):
    """播放 path：volume 是播放增益（%），system_volume 给了就先设系统音量。

    wait=False 时立即返回（Popen），适合 daemon 这类不想被音频阻塞的调用方。
    """
    return _execute(build_play_cmds(path, volume=volume, system_volume=system_volume),
                    dry_run=dry_run, background=not wait)


def notify(title="Focusmate", message="", sound=None, system_volume=None, dry_run=False):
    """发一条 macOS 系统通知。sound 是可选的通知声音名（如 "Basso"）。

    system_volume 给了就先 `osascript` 把系统音量设成 N% 再通知 ——
    通知本身没有独立音量，系统输出/提示音量就是唯一的旋钮。
    """
    cmds = []
    if system_volume is not None:
        cmds.append(build_volume_cmd(system_volume))
    cmds.append(build_notify_cmd(title, message, sound=sound))
    return _execute(cmds, dry_run=dry_run)


def set_system_volume(percent, dry_run=False):
    """把系统输出音量设成 percent%。"""
    return _execute([build_volume_cmd(percent)], dry_run=dry_run)


# ---------------------------------------------------------------- CLI


def build_parser():
    ap = argparse.ArgumentParser(
        prog="alert.py", description="放音乐 / 发通知 / 调音量（macOS）")
    sub = ap.add_subparsers(dest="cmd", metavar="{play,notify,volume}")

    p_play = sub.add_parser("play", help="用 afplay 播放音频（音量可调）")
    p_play.add_argument("file", help="音频文件路径")
    p_play.add_argument("-v", "--volume", type=float, default=100,
                        help="播放增益 0-100（默认 100，不明白就只填这个）")
    p_play.add_argument("--system-volume", type=float, default=None,
                        help="先设系统输出音量到 N%%（0-100）再播放")
    p_play.add_argument("--background", action="store_true",
                        help="不等待播放结束（daemon 用，长音频别卡住主循环）")
    p_play.add_argument("--dry-run", action="store_true", help="只打印要跑的命令")

    p_notify = sub.add_parser("notify", help="发 macOS 系统通知")
    p_notify.add_argument("-t", "--title", default="Focusmate")
    p_notify.add_argument("-m", "--message", default="")
    p_notify.add_argument("-s", "--sound", default=None,
                          help='通知声音名，如 "Basso"（默认无声）')
    p_notify.add_argument("--system-volume", type=float, default=None,
                          help="先把系统输出音量设到 N%%（0-100）再通知")
    p_notify.add_argument("--dry-run", action="store_true")

    p_vol = sub.add_parser("volume", help="设置系统输出音量")
    p_vol.add_argument("percent", type=float, help="0-100")
    p_vol.add_argument("--dry-run", action="store_true")

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if args.cmd == "play":
            return play(args.file, volume=args.volume, system_volume=args.system_volume,
                        wait=not args.background, dry_run=args.dry_run)
        if args.cmd == "notify":
            return notify(args.title, args.message, sound=args.sound,
                          system_volume=args.system_volume, dry_run=args.dry_run)
        if args.cmd == "volume":
            return set_system_volume(args.percent, dry_run=args.dry_run)
    except ValueError as e:
        print("[alert] 参数错: %s" % e, file=sys.stderr)
        return 2
    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
