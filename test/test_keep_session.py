#!/usr/bin/env python3
"""keep_session.py 回归测试：计划（data.csv）变了以后，daemon 会不会跟着变。

    python3 test/test_keep_session.py        # 全绿退出 0，有失败退出 1

重点是**同一个时刻、时长被改掉**的场景：25min 试失败 → 计划改成 50min，
daemon 必须把老记录作废重试，否则改计划等于没改（sync 出来的新计划永远吃不上）。

不联网、不下单：fetch_sessions / book_once / verify 全部换成替身，
state / 日志 / data.csv 都指到临时目录。
"""
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import time

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


# ---------------------------------------------------------------- 沙箱

ks = load("ks", "keep_session.py")
sch = ks.sch

TMP = tempfile.mkdtemp(prefix="keep-session-test-")
CSV = os.path.join(TMP, "data.csv")
ks.STATE_FILE = os.path.join(TMP, "state.json")
ks.LOCK_FILE = os.path.join(TMP, "lock")
ks.PID_FILE = os.path.join(TMP, "pid")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.LOG_FILE = os.path.join(TMP, "log")
sch.DATA_CSV = CSV
NOTIFIES = []
ks.notify = lambda title=None, msg=None, **kw: NOTIFIES.append((title, msg))
ks.debounce = lambda: None                    # 别真 sleep 5±3s

LOGS = []
ks.log = lambda msg, echo=True, to_file=True: LOGS.append(str(msg))

BOOKED = []
ks.fm.book_once = lambda start_iso, duration: (BOOKED.append((start_iso, str(duration))),
                                               {"success": True, "session": {"id": "stub"}})[1]
API = []                                      # API 里默认什么都没有
ks.fetch_sessions = lambda now, until: list(API)
ks.verify_slot_booked = lambda start, **kw: (True, "")   # 复核永远通过


def write_csv(lines):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def clear():
    for p in (ks.STATE_FILE, ks.VERDICTS_FILE, ks.SINS_FILE, ks.SINS_ARCHIVE):
        if os.path.exists(p):
            os.remove(p)
    del BOOKED[:]
    del LOGS[:]
    del API[:]
    del NOTIFIES[:]


# 挑一个「未来 24h 内」的 slot（在窗口里，daemon 才会管它）
_start = datetime.datetime.now() + datetime.timedelta(hours=2)
_start = _start.replace(minute=(_start.minute // 15) * 15, second=0, microsecond=0)
WD = sch.weekday_of_day(_start.date())
HHMM = _start.strftime("%H:%M")
KEY = sch.key_of(_start.date(), HHMM)
print("== 用 %s %s（key=%s）当 test slot ==" % (WD, HHMM, KEY))


def seed(status, duration, attempts=3):
    ks.save_state({"date": datetime.date.today().isoformat(),
                   "slots": {KEY: {"status": status, "duration": duration,
                                   "attempts": attempts, "next_retry_at": None,
                                   "last_error": "stub"}}})


def rec():
    return ks.load_state()["slots"].get(KEY) or {}


# ---------------------------------------------------------------- failed + 时长不变

print("== 时长没变：failed 就该一直 failed（别乱重试）==")
clear()
seed("failed", "25")
write_csv(["# t", "%s,%s,25" % (WD, HHMM)])
is_retry = ks.run_once()
check("run_once 没报错", is_retry, 0)
check("时长没变 → 不重订", BOOKED, [])
check("状态还是 failed", rec().get("status"), "failed")

# ---------------------------------------------------------------- failed + 时长改了

print("== 计划 25min → 50min：failed 作废，按新时长重订 ==")
clear()
seed("failed", "25")
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.run_once()
check("时长改了 → 真的下单了", len(BOOKED), 1)
check("下单用的是新时长 50", BOOKED[-1][1], "50")
check("状态回到 booked", rec().get("status"), "booked")
check("记录里是新时长", rec().get("duration"), "50")
check("日志说明原因", any("计划时长 25 → 50" in m for m in LOGS), True)

# ---------------------------------------------------------------- 退避中 + 时长改了

print("== 退避中（status=None）也要作废重来，别等旧退避 ==")
clear()
seed(None, "25", attempts=1)
want_retry = ks.load_state()
want_retry["slots"][KEY]["next_retry_at"] = (datetime.datetime.now()
                                             + datetime.timedelta(minutes=30)).isoformat()
ks.save_state(want_retry)
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.run_once()
check("退避被时长变化打破 → 下单了", len(BOOKED), 1)
check("下单用的是新时长 50", BOOKED[-1][1], "50")

# ---------------------------------------------------------------- booked + 时长改了

print("== 已经订上的：不动（已订那场改不了时长）==")
clear()
seed("booked", "25", attempts=1)
API.append(_start)                       # API 里确实有这一场（不是手动取消）
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.run_once()
check("booked 不因为时长变了就重下单", BOOKED, [])
check("booked 状态保持", rec().get("status"), "booked")

# ---------------------------------------------------------------- 对照：正常补订

print("== 对照：没有旧记录 → 照常按计划订 ==")
clear()
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.run_once()
check("新 slot 会订", len(BOOKED), 1)
check("订的是 50", BOOKED[-1][1], "50")

# ---------------------------------------------------------------- 环境类失败

print("== 环境失败分类 ==")
blank = ("Dashboard https://app.focusmate.com/dashboard rendered a blank page "
         "(SPA did not mount). This usually means the browser profile directory is "
         "not writable — set FOCUSMATE_MCP_HOME to a writable directory.")
check("空白页算环境失败", ks.is_infra_failure(blank, "AUTOMATION_FAILED"), True)
check("EPERM 算环境失败", ks.is_infra_failure("EPERM: operation not permitted", ""), True)
check("SLOT_UNAVAILABLE 不算环境失败",
      ks.is_infra_failure("slot not available", "SLOT_UNAVAILABLE"), False)

print("== 环境失败不占 3 次上限，也不永久 failed ==")
clear()
write_csv(["# t", "%s,%s,25" % (WD, HHMM)])
orig_book = ks.fm.book_once
ks.fm.book_once = lambda start_iso, duration: {
    "success": False, "error": blank, "errorCode": "AUTOMATION_FAILED"}
for _ in range(5):
    ks.run_once(force=True)          # force 忽略退避，方便连续观察
r = rec()
check("环境失败后不是 failed", r.get("status"), None)
check("环境失败不消耗 attempts", r.get("attempts"), 0)
check("环境失败单独计数", r.get("infra_attempts"), 5)
check("环境失败有退避时间", bool(r.get("next_retry_at")), True)
check("环境失败日志写了环境", any("次环境失败" in m for m in LOGS), True)
check("环境失败发了通知", any("环境问题" in str(n[0]) for n in NOTIFIES), True)

print("== 旧 state 里环境类 failed 自动放回待订 ==")
clear()
seed("failed", "25")
st = ks.load_state()
st["slots"][KEY]["last_error"] = blank
st["slots"][KEY]["last_error_code"] = "AUTOMATION_FAILED"
ks.save_state(st)
ks.fm.book_once = orig_book
ks.run_once()
check("环境类 failed 被放回并订上", rec().get("status"), "booked")
check("环境类 failed 真的重下单", len(BOOKED), 1)
check("日志说放回待订", any("放回待订队列" in m for m in LOGS), True)

print("== slot 级失败仍走 3 次上限 ==")
clear()
write_csv(["# t", "%s,%s,25" % (WD, HHMM)])
ks.fm.book_once = lambda start_iso, duration: {
    "success": False, "error": "The requested time slot is not available.",
    "errorCode": "SLOT_UNAVAILABLE"}
for _ in range(3):
    ks.run_once(force=True)
r = rec()
check("slot 级失败 3 次后转 failed", r.get("status"), "failed")
check("slot 级失败 attempts 记 3", r.get("attempts"), 3)
check("slot 级失败不发环境通知", any("环境问题" in str(n[0]) for n in NOTIFIES), False)
ks.fm.book_once = orig_book

# ---------------------------------------------------------------- MCP 运行目录迁移

print("== MCP 运行目录：不可写时切到工作区，首次迁移登录态 ==")
tmp_home = tempfile.mkdtemp(prefix="mcp-home-test-")
legacy = os.path.join(tmp_home, "legacy")
home = os.path.join(tmp_home, "home")
os.makedirs(os.path.join(legacy, "browser-data", "Default"))
with open(os.path.join(legacy, "browser-data", "Default", "cookies"), "w") as f:
    f.write("x")
with open(os.path.join(legacy, "config.json"), "w") as f:
    f.write("{}")
saved_env = {k: os.environ.get(k) for k in
             ("FOCUSMATE_CONFIG_DIR", "FOCUSMATE_MCP_HOME")}
for k in saved_env:
    os.environ.pop(k, None)
got = ks.fm.ensure_mcp_home(home=home, legacy=legacy)
check("返回可写目录", got, os.path.abspath(home))
check("browser-data 迁移到可写目录",
      os.path.exists(os.path.join(home, "browser-data", "Default", "cookies")), True)
check("config.json 也迁移", os.path.exists(os.path.join(home, "config.json")), True)
check("环境变量指向可写目录",
      os.environ.get("FOCUSMATE_CONFIG_DIR"), os.path.abspath(home))
shutil.rmtree(tmp_home, ignore_errors=True)
for k, v in saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

# ---------------------------------------------------------------- daemon 热重载

print("== daemon 热重载：源码更新才重启，语法错误不重启 ==")
reload_tmp = tempfile.mkdtemp(prefix="reload-test-")
tmp_src = os.path.join(reload_tmp, "fake_keep_session.py")
with open(tmp_src, "w", encoding="utf-8") as f:
    f.write("x = 1\n")
saved_mtimes = dict(ks._SOURCE_MTIMES)
saved_bad = dict(ks._BAD_SOURCE_MTIMES)
ks._SOURCE_MTIMES = {tmp_src: 0}
ks._BAD_SOURCE_MTIMES = {}
check("新源码 → 需要重启", ks.stale_sources(), ["fake_keep_session.py"])
with open(tmp_src, "w", encoding="utf-8") as f:
    f.write("def broken(:\n")
check("语法错误的源码 → 不重启", ks.stale_sources(), [])
ks._SOURCE_MTIMES = saved_mtimes
ks._BAD_SOURCE_MTIMES = saved_bad
shutil.rmtree(reload_tmp, ignore_errors=True)




print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
