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


def check_true(label, got):
    check(label, bool(got), True)


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
# daemon 现在读的是**带时长**的 API 结果（计划 vs 实际要对得上，见
# schedule.match_slot）；API 列表里可以塞 {"start":…, "duration":…} 或裸 datetime。
ks.fetch_session_details = lambda now, until: [
    {"start": x, "duration": None, "id": None} if isinstance(x, datetime.datetime) else x
    for x in API]
ks.fetch_sessions = lambda now, until: [r["start"] for r in ks.fetch_session_details(now, until)]
ks.verify_slot_booked = lambda start, **kw: (True, "")   # 复核永远通过


def write_csv(lines):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def clear():
    for p in (ks.STATE_FILE, ks.VERDICTS_FILE, ks.SINS_FILE, ks.SINS_ARCHIVE,
              getattr(ks, "EXPECTED_FILE", None)):
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

# ---------------------------------------------------------------- 计划 vs 实际

print("== 计划 75min，实际 25+50：认账，不重订、不记罪 ==")
clear()
write_csv(["# t", "%s,%s,75" % (WD, HHMM)])
API.extend([
    {"start": _start - datetime.timedelta(minutes=15), "duration": 25, "id": "a"},
    {"start": _start + datetime.timedelta(minutes=15), "duration": 50, "id": "b"},
])
ks.run_once()
check("认账了 → 一场都没订", BOOKED, [])
check("状态是 booked", rec().get("status"), "booked")
check("记下了它是「覆盖」认账", rec().get("match"), "covered")
check_true("actual 那句人话写进去了", "25" in (rec().get("actual") or ""))
check("没记罪（冤枉过一次，别再冤枉）", ks.load_sins().get("sins", []), [])
check_true("日志里说清是认账", any("认账" in m for m in LOGS))
check_true("日志里报出计划外的两场",
           any("计划外" in m and "16:" not in m for m in LOGS))

print("== 计划 09:30 / 实际 09:45 同样算落地（挪了半格）==")
clear()
_start2 = _start + datetime.timedelta(minutes=20)      # 避开上面那场的时刻
_start2 = _start2.replace(minute=(_start2.minute // 15) * 15, second=0, microsecond=0)
HHMM2 = _start2.strftime("%H:%M")
WD2 = sch.weekday_of_day(_start2.date())
KEY2 = sch.key_of(_start2.date(), HHMM2)
write_csv(["# t", "%s,%s,50" % (WD2, HHMM2)])
API.append({"start": _start2 + datetime.timedelta(minutes=15), "duration": 50, "id": "c"})
ks.run_once()
slots = ks.load_state()["slots"]
check("判成 shifted", (slots.get(KEY2) or {}).get("match"), "shifted")
check("一场都没订", BOOKED, [])
check("没记罪", ks.load_sins().get("sins", []), [])

print("== 真的没有：还是该订就订、改记罪就记罪 ==")
clear()
write_csv(["# t", "%s,%s,75" % (WD, HHMM)])
ks.run_once()
check("没有实际场次 → 照常下单", len(BOOKED), 1)

print("== 已经过去的场次：实际覆盖了就别记「没做」==")
clear()
API.extend([
    {"start": _start - datetime.timedelta(hours=3), "duration": 25, "id": "p1"},
    {"start": _start - datetime.timedelta(hours=3) + datetime.timedelta(minutes=30),
     "duration": 50, "id": "p2"},
])
past_start = _start - datetime.timedelta(hours=3) + datetime.timedelta(minutes=15)
past_key = sch.key_of(past_start.date(), past_start.strftime("%H:%M"))
write_csv(["# t", "%s,%s,75" % (sch.weekday_of_day(past_start.date()),
                                past_start.strftime("%H:%M"))])
_now = datetime.datetime.now()
_orig_now = None
ks.WINDOW_HOURS = 24
ks.run_once()
# 那场已经开始了 → 进 past 分支（只对账）
slots = ks.load_state()["slots"]
check("过去的场次被认账成 booked", (slots.get(past_key) or {}).get("status"), "booked")
check("没记「没做」的罪", ks.load_sins().get("sins", []), [])


# ---------------------------------------------------------------- 热重载

print("== 热重载不能把 daemon 弄死（exec 的目标 argv 必须是自己）==")
execed = []
_real_execv = getattr(ks.os, "execv", None)


def fake_execv(path, argv):
    execed.append((path, list(argv)))
    raise OSError("测试里不真的 exec")


ks.os.execv = fake_execv
_real_source_mtimes = ks._SOURCE_MTIMES
_real_stale = ks.stale_sources
ks._SOURCE_MTIMES = {"dummy.py": 0}
ks.stale_sources = lambda: ["keep_session.py"]
ks._REEXEC_ARGV = None
ks.reexec_if_stale()
check("默认（前台 run / 手动 check）沿用 sys.argv",
      execed[-1][1][1:], list(ks.sys.argv))
ks._REEXEC_ARGV = [ks.PYTHON_BIN, os.path.join(ROOT, "keep_session.py"), "_daemon_loop",
                   "--dry-run"]
ks.reexec_if_stale()
check("daemon 里 exec 回 _daemon_loop（不是父进程的 'daemon start'）",
      execed[-1][1][2:], ["_daemon_loop", "--dry-run"])
check_true("绝不会 exec 成 'daemon start'", "daemon" not in execed[-1][1])
ks._REEXEC_ARGV = None
ks.os.execv = _real_execv
ks._SOURCE_MTIMES = _real_source_mtimes
ks.stale_sources = _real_stale


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

# ---------------------------------------------------------------- 挂号单：自己取消不算罪

print("== 手动取消（没挂号）→ 记罪 ==")
clear()
seed("booked", "50")
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.run_once()
check("state 说 booked、API 里没有 → 记了一笔罪", len(ks.load_sins()["sins"]), 1)
check("罪因是 booked-but-missing",
      ks.load_sins()["sins"][0]["what"], "booked-but-missing")

print("== warden 自己改排布取消（有挂号）→ 不记罪，照样重订 ==")
clear()
seed("booked", "50")
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.expect_cancel([KEY], reason="layout-changed:T3")
check("挂号单写进去了", list(ks.load_expected()["cancels"]), [KEY])
ks.run_once()
check("不记罪", ks.load_sins()["sins"], [])
check("挂号单一码一用（用掉就没了）", ks.load_expected()["cancels"], {})
check("照旧重订了一次", len(BOOKED), 1)
check("日志里说了是 warden 干的",
      any("挂号单" in l for l in LOGS), True)

print("== 挂号单过期/不命中 → 还是记罪 ==")
clear()
seed("booked", "50")
write_csv(["# t", "%s,%s,50" % (WD, HHMM)])
ks.expect_cancel(["2020-01-01T09:00"], reason="stale")
ks.run_once()
check("别的 key 的挂号不掩护这一场", len(ks.load_sins()["sins"]), 1)

print("== 重新排布撤销「永不重订」判决 ==")
clear()
ks.mark_abandoned(KEY, why="walk-of-shame", note="误判")
check("先判成 abandoned", ks.is_abandoned(KEY), True)
check("撤销 1 条", ks.clear_abandoned([KEY]), 1)
check("撤完就不是 abandoned 了", ks.is_abandoned(KEY), False)
check("撤销不存在的 key 返回 0", ks.clear_abandoned([KEY]), 0)




print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
