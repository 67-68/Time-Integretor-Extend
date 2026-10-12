#!/usr/bin/env python3
"""分心看门狗回归测试：session 门控、每整分钟记一次、三次后拉低音量放歌、暂停。

    python3 test/test_idle_watchdog.py     # 全绿退出 0，有失败退出 1

不联网、不出声、不弹通知、不碰仓库里的真状态文件：data.csv / idle / pause / 日志
全部指到临时目录，notify / play_alert / hid_idle_seconds 都换成替身。
"""
import datetime
import importlib.util
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


ks = load("ks", "keep_session.py")
sch = ks.sch

# ---------------------------------------------------------------- 沙箱

TMP = tempfile.mkdtemp(prefix="idle-watchdog-test-")
CSV = os.path.join(TMP, "data.csv")
sch.DATA_CSV = CSV
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle-state.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.LOG_FILE = os.path.join(TMP, "log")
ks.IDLE_CHECK = True

NOTIFIES = []
PLAYS = []
LOGS = []
ks.notify = lambda title, message: NOTIFIES.append((title, message))
ks.play_alert = lambda path=None, volume=None: PLAYS.append((path, volume))
ks.log = lambda msg, echo=True, to_file=True: LOGS.append(str(msg))

# 固定一个「今天 10:00」当 now：与真实几点无关，测试才稳定
NOW = datetime.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
TODAY = NOW.date().isoformat()
WD = sch.weekday_of_day(NOW.date())


def write_csv(lines):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def reset():
    for p in (ks.IDLE_STATE_FILE, ks.PAUSE_FILE):
        if os.path.exists(p):
            os.remove(p)
    del NOTIFIES[:]
    del PLAYS[:]
    del LOGS[:]


def entries():
    return ks.load_entries()[0]


# ---------------------------------------------------------------- current_session

print("== current_session：只在 session 进行时命中 ==")
write_csv(["# t", "%s,09:50,50" % WD])
sess = ks.current_session(entries(), NOW)
check_true("10:00 落在 09:50+50min 里", sess is not None)
check("命中的是 09:50", ks.key_of(sess[0], sess[1]) if sess else None, "%sT09:50" % TODAY)
check("开始前（08:00）返回 None", ks.current_session(entries(), NOW.replace(hour=8)), None)
check("结束后（11:00）返回 None", ks.current_session(entries(), NOW.replace(hour=11)), None)


# ---------------------------------------------------------------- hid_idle_seconds

print("== hid_idle_seconds：解析 ioreg 的纳秒 ==")
_real_run = ks.subprocess.run


class _Done(object):
    def __init__(self, out):
        self.stdout = out


ks.subprocess.run = lambda *a, **k: _Done('  | | "HIDIdleTime" = 1234567890\n')
check("纳秒 → 秒", ks.hid_idle_seconds(), 1234567890 / 1e9)
ks.subprocess.run = lambda *a, **k: _Done("没有这个字段\n")
check("读不到 HIDIdleTime → None", ks.hid_idle_seconds(), None)
ks.subprocess.run = _real_run


# ---------------------------------------------------------------- 主流程

print("== 不在 session：看门狗不上班 ==")
write_csv(["# t", "%s,09:50,50" % WD])
reset()
check("不在 session 里 idle_tick 返回 None",
      ks.idle_tick(now=NOW.replace(hour=8), idle_seconds=999), None)
check("不发通知", NOTIFIES, [])
check("不放声音", PLAYS, [])
check("也顺手记下最近空闲秒数（给 warden 看）",
      ks.load_idle_state().get("last_idle_seconds"), 999)

print("== session 内：每整整一分钟记一次 ==")
reset()
check("空闲 30s：第 1 个整分钟还没到", ks.idle_tick(now=NOW, idle_seconds=30), 0)
check("不发通知", NOTIFIES, [])
check("空闲 61s：违规 1", ks.idle_tick(now=NOW, idle_seconds=61), 1)
check("发了 1 条通知", len(NOTIFIES), 1)
check("通知写当前次数 1/3", "1/3" in NOTIFIES[-1][1], True)
check("通知说明三次后有不好的事情", "不好的事情" in NOTIFIES[-1][1], True)
check("第 1 次不放声音", PLAYS, [])
check("同一分钟内不动：90s 不再记", ks.idle_tick(now=NOW, idle_seconds=90), 1)
check("通知还是 1 条", len(NOTIFIES), 1)
check("空闲 121s：违规 2", ks.idle_tick(now=NOW, idle_seconds=121), 2)
check("通知 2 条，写的是 2/3", (len(NOTIFIES), "2/3" in NOTIFIES[-1][1]), (2, True))
check("第 2 次仍不放声音", PLAYS, [])

print("== 第 3 次：拉低音量 + 播放赵小六嘶吼 ==")
check("空闲 181s：违规 3", ks.idle_tick(now=NOW, idle_seconds=181), 3)
check("通知 3 条", len(NOTIFIES), 3)
check("通知写 3/3", "3/3" in NOTIFIES[-1][1], True)
check("放了 1 次声音", len(PLAYS), 1)
check("放的是赵小六嘶吼", PLAYS[0][0], ks.IDLE_ALERT_AUDIO)
check("音量拉到 10%", PLAYS[0][1], ks.IDLE_ALERT_VOLUME)
check("空闲 240s：封顶 3，不再重复", ks.idle_tick(now=NOW, idle_seconds=240), 3)
check("不再放声音、不再通知", (len(PLAYS), len(NOTIFIES)), (1, 3))

print("== 人回来动一下：本段空闲清零，但本场累计违规不归零 ==")
check("idle=5s 重新开始数", ks.idle_tick(now=NOW, idle_seconds=5), 3)
check("再空闲 65s：已封顶，不再通知", ks.idle_tick(now=NOW, idle_seconds=65), 3)
check("通知没增加", (len(NOTIFIES), len(PLAYS)), (3, 1))

print("== 换一场 session：违规次数归零 ==")
write_csv(["# t", "%s,10:00,50" % WD])         # 10:00 整开始，NOW 正好在其中
check("新 session 从 0 开始", ks.idle_tick(now=NOW, idle_seconds=30), 0)
check("新 session 第 1 次违规", ks.idle_tick(now=NOW, idle_seconds=61), 1)
check("状态里的 session_key 是新的",
      ks.load_idle_state().get("session_key"), "%sT10:00" % TODAY)

print("== 看门狗刚上班时已经很闲：不追溯、不爆击 ==")
reset()
write_csv(["# t", "%s,09:50,50" % WD])
check("第一次就 idle=300：先不记", ks.idle_tick(now=NOW, idle_seconds=300), 0)
check("下一次 idle=330：还是不记（没跨新的整分钟）",
      ks.idle_tick(now=NOW, idle_seconds=330), 0)
check("跨到 361s：记第 1 次", ks.idle_tick(now=NOW, idle_seconds=361), 1)
check("只发 1 条通知", len(NOTIFIES), 1)

print("== 暂停：期间不打扰，解除后接着数 ==")
reset()
write_csv(["# t", "%s,09:50,50" % WD])
ks.idle_tick(now=NOW, idle_seconds=10)          # 先建立基线（第一次观察不追溯）
check("先升到 2 次违规", (ks.idle_tick(now=NOW, idle_seconds=61),
                          ks.idle_tick(now=NOW, idle_seconds=121)), (1, 2))
ok, st, why = ks.start_pause(NOW)
check("开始暂停成功", ok, True)
check("暂停后今天还剩 2 次", st["remaining"], 2)
check("暂停中 active", st["active"], True)
check("暂停中剩余秒数 ≈ 300", st["left_sec"], 300)
n = ks.idle_tick(now=NOW + datetime.timedelta(seconds=20), idle_seconds=500)
check("暂停中即使 idle=500 也不记", n, 2)
check("暂停中不发通知", len(NOTIFIES), 2)
ok2, _st2, why2 = ks.start_pause(NOW + datetime.timedelta(seconds=30))
check("暂停中再按 stop 会被拒", ok2, False)
check("被拒理由提到暂停中", "暂停中" in why2, True)
# 暂停到期（5 分钟后）
after = NOW + datetime.timedelta(minutes=6)
check("暂停已过期", ks.pause_status(after)["active"], False)
check("解除后接着数：跨整分钟 → 违规 3", ks.idle_tick(now=after, idle_seconds=900), 3)
check("触发时放声音", len(PLAYS), 1)
check("剩余次数还是 2", ks.pause_status(after)["remaining"], 2)
ok3, st3, _why3 = ks.start_pause(after + datetime.timedelta(seconds=1))
check("暂停过期后还能再按（剩 1 次）", (ok3, st3["remaining"]), (True, 1))

print("== 每天 3 次暂停：用完拒绝 ==")
reset()
base = datetime.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
for i in range(ks.PAUSE_MAX_PER_DAY):
    ok, st, why = ks.start_pause(base + datetime.timedelta(minutes=6 * i))
    check("第 %d 次暂停成功" % (i + 1), ok, True)
ok, st, why = ks.start_pause(base + datetime.timedelta(minutes=6 * ks.PAUSE_MAX_PER_DAY))
check("第 4 次被拒", ok, False)
check("理由提到用完", "用完" in why, True)

print("== 读不到空闲时间：不误判 ==")
reset()
write_csv(["# t", "%s,09:50,50" % WD])
_real_hid = ks.hid_idle_seconds
ks.hid_idle_seconds = lambda: None
check("拿不到空闲时间返回 None", ks.idle_tick(now=NOW), None)
check("不发通知", NOTIFIES, [])
ks.hid_idle_seconds = _real_hid

print("== dry-run：只记数、不打扰 ==")
reset()
ks.idle_tick(now=NOW, idle_seconds=10, dry_run=True)   # 先建立基线
check("dry-run 仍记第 1 次", ks.idle_tick(now=NOW, idle_seconds=61, dry_run=True), 1)
check("dry-run 不发通知", NOTIFIES, [])
check("dry-run 不放声音", PLAYS, [])
check("dry-run 也落盘计数", ks.load_idle_state().get("strikes"), 1)

print("== 总开关 ==")
ks.IDLE_CHECK = False
check("关掉后 idle_tick 不做事", ks.idle_tick(now=NOW, idle_seconds=999), None)
check("关掉后不发通知", NOTIFIES, [])
ks.IDLE_CHECK = True

print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
