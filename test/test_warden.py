#!/usr/bin/env python3
"""warden.py 回归测试：宣言闸门 + 三层摩擦（草稿/执行/罪案）+ day 切换 + session 创建。

    python3 test/test_warden.py        # 全绿退出 0，有失败退出 1

不联网、不碰浏览器、不写仓库里的任何文件：data.csv 和三个状态文件都指到临时目录。
"""
import builtins
import contextlib
import dataclasses
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


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(text):
    """去掉 rich 写出的 ANSI，方便断言屏幕上真正看到的字。"""
    return ANSI_RE.sub("", text)


w = load("warden", "warden.py")
from rich.console import Console  # noqa: E402  (warden 已经把 temp/pylibs 塞进 sys.path)

ks = w.ks
sch = w.sch
sp = w.sp

TMP = tempfile.mkdtemp(prefix="warden-test-")
# 所有落盘的东西都关进临时目录：绝不碰仓库里的真 data.csv / 状态文件
CSV = os.path.join(TMP, "data.csv")
sch.DATA_CSV = CSV
sch.BACKUP_CSV = os.path.join(TMP, "data.csv.bak")
sch.TEMP = TMP
os.environ["TI_SESSION_EXTRAS"] = os.path.join(TMP, "session-extras.json")
os.environ["TI_SESSION_RULES"] = os.path.join(TMP, "session-rules.json")
os.environ["TI_SETTINGS_FILE"] = os.path.join(TMP, "settings.json")
# 任务库也必须关进临时目录：这里以前漏了，于是用例读的是仓库里**真的**
# data/tasks.json —— 任务一多，契约表就变宽，`已匹配` 会被压成 `已匹`，用例跟着红；
# 更糟的是重复任务会在 refresh 里被 materialize，那是会**写**真库的。
os.environ["TI_TASKS_FILE"] = os.path.join(TMP, "tasks.json")
os.environ["TI_TASK_ARCHIVE"] = os.path.join(TMP, "task-archive.json")
ks.STATE_FILE = os.path.join(TMP, "state.json")
# 改排布会取 daemon 那一轮的 round lock、并写「我取消的」挂号单 —— 别落到真目录里
ks.LOCK_FILE = os.path.join(os.path.dirname(ks.STATE_FILE), "keep_session.lock")
ks.EXPECTED_FILE = os.path.join(os.path.dirname(ks.STATE_FILE),
                                "keep_session-expected.json")
ks.VERDICTS_FILE = os.path.join(TMP, "verdicts.json")
ks.SINS_FILE = os.path.join(TMP, "sins.json")
ks.SINS_ARCHIVE = os.path.join(TMP, "sins.jsonl")
ks.IDLE_STATE_FILE = os.path.join(TMP, "idle-state.json")
ks.IDLE_MODE_FILE = os.path.join(TMP, "mode.json")
ks.PAUSE_FILE = os.path.join(TMP, "pause.json")
ks.notify = lambda *a, **kw: None        # 别真弹 macOS 通知


def fake_stat_history():
    """手搭一份 stats.py 口径下的 history：累计 6 场完成、8 场订，区间 10 天。

    为什么在 warden 的测试里也塞这个：统计视图**一进就懒加载**（Tab 也会进去），
    不把 st.load 换掉的话，只要测试里切到统计视图就会真去连 api.focusmate.com。
    """
    plan = {"2026-01-01": [("09:00", True), ("10:00", True)],
            "2026-01-02": [("09:00", False)],
            "2026-01-03": [],
            "2026-01-04": [("09:00", True), ("10:00", False)],
            "2026-01-05": [("09:00", True), ("10:00", True), ("11:00", True)]}
    sessions = []
    for day, items in sorted(plan.items()):
        for hhmm, done in items:
            sessions.append({"sessionId": "%s-%s" % (day, hhmm),
                             "start": "%sT%s:00" % (day, hhmm), "durationMin": 50,
                             "completed": done, "joined": True, "title": "",
                             "activityType": "", "mine": True})
    return {"fetched_at": "2026-01-11T09:00:00", "since": "2026-01-01",
            "until": "2026-01-10", "source": "api",
            "me": {"userId": "me", "name": "测试", "memberSince": "2026-01-01T00:00:00",
                   "totalSessionCount": 6, "timeZone": "Asia/Hong_Kong"},
            "sessions": sessions}


FAKE_STAT = fake_stat_history()
STAT_PULLS = []


def fake_stat_load(force=False, **kw):
    STAT_PULLS.append((bool(force), bool(kw.get("full"))))
    return FAKE_STAT, "测试缓存"


w.st.load = fake_stat_load          # 全程离线；真正走网络的分支在 test/test_stat.py 测

NOW = datetime.datetime.now()
TODAY = NOW.date()
TODAY_WD = sch.weekday_of_day(TODAY)
# “另一个有契约的星期几”。如果下一个周五就是今天（今天周五），+7 仍是同一个
# weekday，测试里两行会撞车；所以退到明天，保证 FRI_WD != TODAY_WD。
FRI = TODAY + datetime.timedelta(days=(4 - TODAY.weekday()) % 7 or 1)
FRI_WD = sch.weekday_of_day(FRI)
ROWS = ["# test schedule", "%s,21:00,25" % TODAY_WD, "%s,09:00,50" % FRI_WD]


def write_csv(lines):
    with open(CSV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def clear_state():
    for p in (ks.VERDICTS_FILE, ks.SINS_FILE, ks.SINS_ARCHIVE, ks.STATE_FILE):
        if os.path.exists(p):
            os.remove(p)


def clear_mode():
    if os.path.exists(ks.IDLE_MODE_FILE):
        os.remove(ks.IDLE_MODE_FILE)


write_csv(ROWS)
clear_state()


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
        if cmd in self.replies:
            r = self.replies[cmd]
            return r(kw) if callable(r) else r
        if cmd == "release":
            was = self.browser
            self.browser = False
            return {"released": was}
        if cmd == "snapshot":
            return {"date": kw.get("date", TODAY.isoformat()),
                    "now": int(NOW.timestamp() * 1000), "count": 0, "sessions": []}
        return {"verified": True}

    def browser_state(self):
        return True, self.browser, ""

    def stop(self):
        pass


def fd(zone="locked", hhmm="21:00", id_="id-1", planned=True, lineno=None, raw="",
       title="", matched=False, dur=25, phase="未开始"):
    """手工造一行表行：命令分发看的是 row["zone"]，所以直接给。"""
    h, m = (int(x) for x in hhmm.split(":"))
    start = datetime.datetime.combine(TODAY, datetime.time(h, m))
    end = start + datetime.timedelta(minutes=dur)
    sm, em = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    return {
        "startMs": sm, "endMs": em,
        "startLabel": start.strftime("%H:%M"), "endLabel": end.strftime("%H:%M"),
        "durationMin": dur, "id": id_, "title": title, "quiet": False,
        "matched": matched, "phase": phase,
        "planned": planned, "lineno": lineno, "raw": raw,
        "contract": ("%s,%s,%d" % (TODAY_WD, hhmm, dur)) if planned else "",
        "zone": zone,
    }


def mkwarden(bridge=None, offline=True):
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return w.Warden(console, bridge or FakeBridge(), no_clear=True, offline=offline)


def answer(seq):
    """把 builtins.input 换成按序吐答案（用完一直吐最后一个）。

    序列里可以放异常（类或实例）来模拟 Ctrl+C / EOF —— 宣言闸门没有次数上限，
    不放个出口的话，吐错答案的 stub 会把它喂成死循环。
    """
    box = list(seq)

    def fake(prompt=""):
        got = box.pop(0) if len(box) > 1 else (box[0] if box else "")
        if isinstance(got, BaseException):
            raise got
        if isinstance(got, type) and issubclass(got, BaseException):
            raise got()
        return got

    builtins.input = fake
    return fake


def beeped(fn):
    """跑 fn，返回（结果, stdout 里有没有响铃）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn()
    return out, "\a" in buf.getvalue()


# ---------------------------------------------------------------- 宣言

print("== 宣言文本 ==")
check("宣言 152 字符", len(w.DECLARATION), 152)
check("宣言以你的原话开头", w.DECLARATION.startswith("我在此确认，我选择屈服于短期的多巴胺诱惑"), True)
check("宣言以句号收尾", w.DECLARATION.endswith("状态或心情。"), True)
check("一字不差 → 通过", w.check_declaration(w.DECLARATION), (True, ""))
check("前后空白不算（手滑带了空格）", w.check_declaration("  " + w.DECLARATION + " ")[0], True)
check("空行 → 不通过", w.check_declaration("")[0], False)
check("空行提示", w.check_declaration("   ")[1], "你敲了空行。宣言一个字都不能少。")

ok, why = w.check_declaration(w.DECLARATION[:-1])
check("少最后一个标点 → 不通过", ok, False)
check_true("少标点会指出少几个字", "少 1 个" in why)
ok, why = w.check_declaration(w.DECLARATION.replace("，", ",", 1))
check("第一个逗号敲成半角 → 不通过", ok, False)
check_true("半角逗号被点名", "第 6 个字开始不一致" in why and "，" in why and "," in why)
ok, why = w.check_declaration(w.DECLARATION.replace("懦夫", "懦夫啊"))
check("中间多一个字 → 不通过", ok, False)
check_true("多字被点名", "第 89 个字开始不一致" in why)
ok, why = w.check_declaration(w.DECLARATION + "哈")
check("结尾多一个字 → 不通过", ok, False)
check_true("多字提示", "多敲了 1 个字" in why)
check("原文 99 字那版不够（你选了补到 150）", w.check_declaration(w.DECLARATION[:99])[0], False)
check("first_mismatch 全同 → 0", w.first_mismatch("abc", "abc"), 0)
check("first_mismatch 第一个就错 → 1", w.first_mismatch("xbc", "abc"), 1)
check("first_mismatch 短了 → 落在末尾之后", w.first_mismatch("ab", "abc"), 3)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    w.beep()
check("鸣笛写的是终端 bell", "\a" in buf.getvalue(), True)

# ---------------------------------------------------------------- 区域判据

print("== 三层摩擦的判据（滚动 24 小时窗口）==")
check("已开始的 = past", sch.zone_name(NOW - datetime.timedelta(hours=1), NOW), "past")
check("窗口里 = locked", sch.zone_name(NOW + datetime.timedelta(hours=2), NOW), "locked")
check("刚进窗口边界 = locked", sch.zone_name(NOW + datetime.timedelta(hours=23, minutes=59), NOW),
      "locked")
check("超过 24h = draft", sch.zone_name(NOW + datetime.timedelta(hours=24, minutes=1), NOW), "draft")
check("远未来 = draft", sch.zone_name(NOW + datetime.timedelta(days=10), NOW), "draft")

entries, warns = sch.load_entries()
check("临时 data.csv 解析无 warning", warns, [])
win = sch.window(entries, NOW)
horizon = NOW + datetime.timedelta(hours=24)
check("执行区全部落在 (now, now+24h] 内",
      all(NOW < st <= horizon for _d, _s, st in win["due"]), True)
check("过去区全部 <= now", all(st <= NOW for _d, _s, st in win["past"]), True)
check_true("窗口总共覆盖到了契约（今天或明天）", len(win["due"]) + len(win["past"]) >= 1)

# ---------------------------------------------------------------- 表格合并

print("== 契约 × API 合并 ==")


def api_row(hhmm, dur, id_, title, quiet, matched, phase):
    h, m = (int(x) for x in hhmm.split(":"))
    start = datetime.datetime.combine(TODAY, datetime.time(h, m))
    end = start + datetime.timedelta(minutes=dur)
    return {"startMs": int(start.timestamp() * 1000), "endMs": int(end.timestamp() * 1000),
            "startLabel": hhmm, "endLabel": end.strftime("%H:%M"), "durationMin": dur,
            "id": id_, "title": title, "quiet": quiet, "matched": matched, "phase": phase}


wd = mkwarden()
wd.reload_entries()
wd.api_rows = [api_row("21:00", 25, "sess-1", "契约那场", True, True, "未开始"),
               api_row("08:00", 50, "sess-2", "计划外的", False, False, "已结束")]
wd.rows_source = "bridge"
wd.rebuild_rows()
check("按时间排序", [r["startLabel"] for r in wd.rows], ["08:00", "21:00"])
check("契约有 + API 有 → 计划内且有 id", (wd.rows[1]["planned"], wd.rows[1]["id"]), (True, "sess-1"))
check("API 有 + 契约没有 → 计划外", wd.rows[0]["planned"], False)
check("计划外的契约列是空的", wd.rows[0]["contract"], "")
check("契约列写的是 csv 原文", wd.rows[1]["contract"], "%s,21:00,25" % TODAY_WD)
check("契约行列号指向 data.csv", wd.rows[1]["lineno"], 2)
ps = wd.plan_summary()
check("契约统计：计划 1 场", ps["total"], 1)
check("契约统计：1 场已落地", ps["booked"], 1)
check("契约统计：不缺", ps["missing"], [])
check("权威数据才敢下结论", wd.authoritative, True)
wd.rows_source = "snapshot"
check("快照模式：不假装知道订没订", wd.plan_summary()["booked"], None)
check("快照模式非权威", wd.authoritative, False)

wd.rows_source = "bridge"
wd.api_rows = []
wd.rebuild_rows()
check("契约有但 API 没有 → 缺", wd.rows[0]["id"], None)
check("缺的那场被算进 missing", [r["startLabel"] for r in wd.plan_summary()["missing"]], ["21:00"])

# ---------------------------------------------------------------- cancel：三层摩擦

print("== cancel 的三种命运 ==")

# 1) 草稿区：零阻力，直接删契约行，不敲宣言
write_csv(ROWS)
clear_state()
wd = mkwarden()
wd.reload_entries()
wd.rows = [fd(zone="draft", hhmm="21:00", id_=None, lineno=2, raw="%s,21:00,25" % TODAY_WD)]
wd.bridge.calls = []
builtins.input = lambda prompt="": "绝不敲宣言"
check("草稿区取消 → 成功", wd.cmd_cancel(["1"]), True)
check("草稿区取消 → 没碰 API", [c for c in wd.bridge.calls if c[0] == "cancel"], [])
check("草稿区取消 → 契约行没了",
      [ln for ln in sch.read_text().splitlines() if "21:00" in ln], [])
check("草稿区取消 → 没记罪", ks.load_sins()["sins"], [])
check("草稿区取消 → 没有判决", ks.load_verdicts()["abandoned"], {})
check("草稿区取消 → 动了 data.csv 会留下备份", os.path.exists(sch.BACKUP_CSV), True)

# 2) 执行区：敲错就锁死，敲对才动；判决落盘
write_csv(ROWS)
clear_state()
wd = mkwarden()
wd.reload_entries()
wd.rows = [fd(zone="locked", hhmm="21:00", id_="sess-9", lineno=2, raw="%s,21:00,25" % TODAY_WD)]
wd.bridge.calls = []
answer(["胡写一句", w.DECLARATION[:-1], w.DECLARATION])
check("执行区：敲错两次后敲对 → 执行", wd.cmd_cancel(["1"]), True)
check("执行区：真调了 cancel", [c[0] for c in wd.bridge.calls if c[0] == "cancel"], ["cancel"])
check("cancel 带的是 session 身份（meetingId）",
      [c[1].get("meetingId") for c in wd.bridge.calls if c[0] == "cancel"], ["sess-9"])
ab = ks.load_verdicts()["abandoned"]
check("判决落盘：这场被标 abandoned", list(ab), ["%sT21:00" % TODAY])
check("判决记了理由", ab["%sT21:00" % TODAY]["why"], "walk-of-shame")
check("敲错的那两次被记进消息", any("不通过" in m[2] for m in wd.messages), True)

# 3) 执行区 + Ctrl+C：什么都没发生
write_csv(ROWS)
clear_state()
wd = mkwarden()
wd.reload_entries()
wd.rows = [fd(zone="locked", hhmm="21:00", id_="sess-9")]
wd.bridge.calls = []
builtins.input = lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt())
check("执行区 + Ctrl+C → 返回 False", wd.cmd_cancel(["1"]), False)
check("执行区 + Ctrl+C → 没碰 API", [c for c in wd.bridge.calls if c[0] == "cancel"], [])
check("执行区 + Ctrl+C → 没判决", ks.load_verdicts()["abandoned"], {})

# 4) 过去：直接拒绝
wd = mkwarden()
wd.rows = [fd(zone="past", hhmm="09:00", phase="已结束")]
try:
    wd.cmd_cancel(["1"])
    check("过去的场次应该被拒", False, True)
except ValueError as e:
    check_true("过去的场次被拒（不取消也不补订）", "不取消也不补订" in str(e))

# 5) 不存在的 ID
wd = mkwarden()
wd.rows = [fd()]
try:
    wd.cmd_cancel(["7"])
    check("不存在的 ID 应该被拒", False, True)
except ValueError as e:
    check_true("不存在的 ID 被拒", "没有 ID 7" in str(e))

# 6) 草稿区 + 文件被别处改过 → 拒绝（不能删错行）
write_csv(ROWS)
clear_state()
wd = mkwarden()
wd.reload_entries()
wd.rows = [fd(zone="draft", hhmm="21:00", id_=None, lineno=2, raw="%s,21:00,25" % TODAY_WD)]
write_csv(["# test schedule", "%s,20:00,50" % TODAY_WD])   # 别处改过了
try:
    wd.cmd_cancel(["1"])
    check("指纹不一致应该被拒", False, True)
except ValueError as e:
    check_true("指纹不一致被拒（免得删错行）", "指纹不一致" in str(e))

# ---------------------------------------------------------------- session 命令

print("== session：默认只加这一天（一次性），--permanent 才写进日历 ==")
write_csv(["# test schedule", "%s,21:00,25" % TODAY_WD])
clear_state()
ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "round done", ""))[1]
wd = mkwarden()
wd.reload_entries()
wd.bridge.calls = []
for pth in (sp.extras_path(), sp.rules_path()):
    if os.path.exists(pth):
        os.remove(pth)
check("session 写进去了", wd.cmd_session(["21:30", "50"]), True)
check("一次性 session 记在 extras 里",
      [r["start"] for r in sp.extras_for_date(sp.load_extras(), TODAY)], ["21:30"])
check("一次性 session 不进长期规则", sp.load_rules()["rules"], [])
ents, _ = sch.load_entries()
check("data.csv 里是**精确日期**行（不是每周重复的 weekday 行）",
      "%s,21:30,50" % TODAY.isoformat() in sch.read_text(), True)
check_true("解析成日期行",
           any(e.get("date") == TODAY.isoformat() and e["start"] == "21:30"
               and e["duration"] == "50" for e in ents))
check("没有写成按星期几的规则",
      [e for e in ents if e["start"] == "21:30" and not e.get("date")], [])
check("session 之后立刻跑了一轮 check", ran[-1], ("check",))
check("session 不自己碰 cancel/title 那类写接口",
      [c for c in wd.bridge.calls if c[0] not in ("release",)], [])

ran = []
wd.handle("session --permanent 21:30 50")
check("--permanent 写长期规则", [r["start"] for r in sp.load_rules()["rules"]], ["21:30"])
check_true("长期规则按星期几",
           sp.load_rules()["rules"][0]["weekday"] == TODAY_WD)
ran = []
wd.handle("session --permanent 21:30 50")
check("重复的永久 slot → 被拒（没再跑 check）", ran, [])
check_true("重复时给了人话提示", any("已经有" in m[2] for m in wd.messages))
for bad, note in [("session 22:00 40", "25/50/75"),
                  ("session 22:10 50", "15 分钟整点"),
                  ("session 25:99 50", "HH:MM"),
                  ("session 22:00", "用法"),
                  ("session 22:20 abc", "25/50/75")]:
    before = len(ran)
    wd.handle(bad)
    check("非法 session 被拒：%s" % bad, len(ran), before)
    check_true("非法 session 给了提示：%s" % bad,
               any(note in m[2] for m in wd.messages))

# 起点–终点：11:15-12:05 → 50min。所有等价写法都要认。
print("== session：起点–终点区间（11:15-12:05 → 50min）==")
for spec in (["11:15-12:05"], ["11:15", "-", "12:05"], ["11:15", "12:05"],
             ["11:15 - 12:05"], ["23:30-00:20"]):
    check("区间解析：%r" % (spec,), w.parse_session_spec(spec),
          ("23:30", "50") if spec == ["23:30-00:20"] else ("11:15", "50"))
check("区间时长必须是 25/50/75",
      w.parse_session_spec(["11:15", "50"]), ("11:15", "50"))
try:
    w.parse_session_spec(["11:15", "12:00"])
    check("45min 区间应该被拒", False, True)
except ValueError as e:
    check_true("45min 区间被拒", "区间不合法" in str(e))

write_csv(["# test schedule", "%s,21:00,25" % TODAY_WD])
clear_state()
ran = []
wd = mkwarden()
wd.reload_entries()
check("session 11:15-12:05 写进去了", wd.cmd_session(["11:15-12:05"]), True)
check("契约表里是规范的 HH:MM,duration（不是区间）",
      "%s,11:15,50" % TODAY.isoformat() in sch.read_text(), True)
ents, _ = sch.load_entries()
check_true("区间推出 50 分钟",
           any(e.get("date") == TODAY.isoformat() and e["start"] == "11:15"
               and e["duration"] == "50" for e in ents))

# ---------------------------------------------------------------- sync 命令

print("== sync：调 download_calendar.py populate ==")
write_csv(["# test schedule", "%s,21:00,25" % TODAY_WD])
clear_state()
calls = []
w.run_download_calendar = lambda *a, **kw: (calls.append(a), (0, "data.csv 已更新: /x", ""))[1]
wd = mkwarden()
wd.reload_entries()
check("sync 成功返回 True", wd.cmd_sync([]), True)
check("sync 调的是 download_calendar.py populate", calls[-1][0], "populate")
check("sync 不碰 bridge 的写接口",
      [c for c in wd.bridge.calls if c[0] not in ("release",)], [])
check_true("sync 给了人话反馈", any("已同步" in m[2] for m in wd.messages))

calls = []
wd = mkwarden()
check("sync 透传参数（--days 3）", wd.cmd_sync(["--days", "3"]), True)
check("sync 参数原样给了 populate",
      calls[-1], ("populate", "--from-download", "--days", "3"))

calls = []
w.run_download_calendar = lambda *a, **kw: (calls.append(a), (1, "", "缺少依赖 caldav"))[1]
wd = mkwarden()
check("sync 失败返回 False", wd.cmd_sync([]), False)
check("sync 失败标了 had_error", wd.had_error, True)
check_true("sync 失败把 stderr 说出来", any("缺少依赖" in m[2] for m in wd.messages))

ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "round done", ""))[1]
check("handle 认识 sync", wd.handle("sync") in (True, False), True)
check("sync 走的是 download_calendar，不是 keep_session",
      calls[-1][0], "download")
check("sync 不会顺手跑 keep_session check", ran, [])

# ---------------------------------------------------------------- day 命令

print("== day 切换 ==")
wd = mkwarden()
check("day 1 → 明天",
      (wd.cmd_day(["1"]), wd.day_offset, wd.sel_date), (True, 1, TODAY + datetime.timedelta(days=1)))
check("day -1 → 昨天", (wd.cmd_day(["-1"]), wd.day_offset), (True, -1))
check("day 0 → 今天", (wd.cmd_day(["0"]), wd.sel_date), (True, TODAY))
# 用相对日期：写死具体某天的话，过几天就会自己掉出 -7..+60 的窗口
_recent = TODAY - datetime.timedelta(days=3)
check("day <3 天前> → 绝对日期", (wd.cmd_day([_recent.isoformat()]), wd.day_offset),
      (True, -3))
check("day 不带参数只是报告（不退出）", wd.cmd_day([]), False)
for bad in ("明天", "2026-13-99", "abc"):
    try:
        wd.cmd_day([bad])
        check("非法 day 应该被拒：%s" % bad, False, True)
    except ValueError:
        check("非法 day 被拒：%s" % bad, True, True)
try:
    wd.cmd_day(["999"])
    check("太远的 day 应该被拒", False, True)
except ValueError as e:
    check_true("太远的 day 被拒", "只让看" in str(e))

# ---------------------------------------------------------------- 罪案区：全局锁

print("== 罪案区：不认罪就别想做别的 ==")
clear_state()
# 用「明天 09:00」当罪：不管现在几点，它都还没开始，confess 才会去动 API
SIN_KEY = "%sT09:00" % (TODAY + datetime.timedelta(days=1))
ks.record_sin(SIN_KEY, "booked-but-missing", "测试用的罪")
check("罪落盘了", len(ks.load_sins()["sins"]), 1)
wd = mkwarden()
wd.reload_entries()
wd.bridge.calls = []
check("有罪时 lockdown_reason 有话说", bool(wd.lockdown_reason()), True)
# 罪案区现在是「视图上的两个钩子」：prompt_text 交出「宣言> 」，intercept 拦住派发
check("有罪时提示符变成「宣言> 」", wd.prompt_text(), "宣言> ")
check("有罪时 shell 用的就是那个提示符", wd.shell.prompt, "宣言> ")
out, rang = beeped(lambda: wd.handle("mute 1 on"))
check("有罪时 mute 被拦（没碰 API）", [c for c in wd.bridge.calls if c[0] == "mute"], [])
check("有罪时 mute 会鸣笛", rang, True)
_buf = io.StringIO()
wd.console = Console(file=_buf, width=120, force_terminal=False)
beeped(lambda: wd.handle("mute 1 on"))
check("拦住时把宣言面板打出来", "认罪之前" in _buf.getvalue(), True)
check("有罪时 handle 不退出", out, True)
beeped(lambda: wd.handle("session 22:00 50"))
check("有罪时 session 也被拦（连契约表都不给写）",
      "%s,22:00,50" % TODAY_WD in sch.read_text(), False)
beeped(lambda: wd.handle("day 1"))
check("有罪时 day 也被拦", wd.day_offset, 0)

# 敲宣言 → 认罪：判决 + 清罪 + 归档 + 把被重订的那场再取消掉
wd.bridge.replies = {
    "snapshot": lambda kw: {"date": kw.get("date"), "now": int(NOW.timestamp() * 1000),
                            "sessions": [{"startLabel": "09:00", "startMs": 1, "id": "rebooked-1"}]},
}
wd.bridge.calls = []
check("有罪时敲宣言 → 受理", wd.handle(w.DECLARATION), True)
check("认罪后罪清空", ks.load_sins()["sins"], [])
check("认罪后判决落盘", SIN_KEY in ks.load_verdicts()["abandoned"], True)
check("认罪后归档留痕（只增不删）",
      "booked-but-missing" in open(ks.SINS_ARCHIVE, encoding="utf-8").read(), True)
check("已被 daemon 重订回来的 → 再取消掉",
      [c[0] for c in wd.bridge.calls if c[0] == "cancel"], ["cancel"])
check("取消的是被重订的那一场",
      [c[1].get("meetingId") for c in wd.bridge.calls if c[0] == "cancel"], ["rebooked-1"])
wd.bridge.calls = []
check("认罪之后命令恢复正常", wd.handle("day 1"), True)
check("认罪之后 day 真的动了", wd.day_offset, 1)

# 已经过去的罪：只判放弃，不去动 API（取消过去的场次没意义）
clear_state()
PAST_KEY = TODAY.isoformat() + "T00:01"
ks.record_sin(PAST_KEY, "booked-but-missing-past", "过去了的罪")
wd = mkwarden()
wd.bridge.calls = []
check("过去的罪也能认（判决落盘）", wd.handle(w.DECLARATION), True)
check("过去的罪：判 abandoned", PAST_KEY in ks.load_verdicts()["abandoned"], True)
check("过去的罪：不去动 API", [c for c in wd.bridge.calls if c[0] == "cancel"], [])
ks.clear_sins()

# 有罪时的第一屏必须把宣言摆出来（不然你没法抄）
ks.record_sin(SIN_KEY, "booked-but-missing", "再来一次")
buf = io.StringIO()
console = Console(file=buf, width=120, force_terminal=False)
wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
wd.run(exec_lines=["quit"])
out = buf.getvalue()
check("启动就告诉你欠着账", "欠着账" in out, True)
check("启动就把宣言摆出来（好让你抄）", "我在此确认" in out, True)

check("有罪时 quit 允许退出", wd.handle("quit"), False)
check("但罪还挂着（关窗口不解锁）", len(ks.load_sins()["sins"]), 1)
ks.clear_sins()
check("清干净了", ks.load_sins()["sins"], [])

# ---------------------------------------------------------------- stop 要宣言

print("== 停 daemon 也要敲宣言（密码系统已删）==")
clear_state()
ran = []
w.run_keep_session = lambda *a, **kw: (ran.append(a), (0, "daemon 已停止", ""))[1]
wd = mkwarden()
answer(["我不想敲", KeyboardInterrupt])   # 敲错 → 再问 → 模拟用户 Ctrl+C 放弃
check("宣言没敲对 → 停不了", wd.cmd_stop(), False)
check("宣言没敲对 → 没调 keep_session", ran, [])
wd = mkwarden()
answer([w.DECLARATION])
check("宣言敲对 → 停成功", wd.cmd_stop(), True)
check("stop 真的调了 keep_session", ran[-1], ("daemon", "stop"))
check("stop 不需要动浏览器（浏览器只在下单时才要）",
      [c for c in wd.bridge.calls if c[0] == "release"], [])
check("密码系统已经不存在了", hasattr(w, "verify_password") or hasattr(w, "save_password"), False)

# start 不要宣言，但也要先放开浏览器
ran = []
wd = mkwarden()
wd.bridge.browser = True
wd.bridge.calls = []
check("start 成功", wd.cmd_start(), True)
check("start 先 release 了浏览器", wd.bridge.calls[0][0], "release")
check("start 真的调了 keep_session", ran[-1], ("daemon", "start"))

# ---------------------------------------------------------------- 请求号撞名

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
write_csv(ROWS)
for width in (40, 80, 160):
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    wd = w.Warden(console, FakeBridge(replies={
        "snapshot": lambda kw: {"date": kw.get("date"), "now": int(NOW.timestamp() * 1000),
                                "sessions": []}
    }), no_clear=True)
    wd.refresh()
    wd.paint()
    out = buf.getvalue()
    check("width=%d 渲染出表格" % width, "The Contract Table" in out, True)
    check("width=%d 渲染出区域提示" % width, "草稿区" in out or "执行区" in out, True)
    check("width=%d 渲染无异常" % width, "Traceback" in out, False)
    check("width=%d 渲染出窗口信息" % width, "执行区" in out, True)

buf = io.StringIO()
console = Console(file=buf, width=100, force_terminal=False)
wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
wd.rows = []
wd.paint()
check("空表不炸", "既没有契约、也没有 session" in buf.getvalue(), True)

# help 里别塞会被 rich 当样式的方括号（[草] 这种会直接抛 MarkupError）
for width in (40, 120):
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
    wd.console = console
    wd.cmd_help()
    out = buf.getvalue()
    check("width=%d help 不炸" % width, "Traceback" not in out and "命令" in out, True)
    check("width=%d help 里有 session/day" % width, "session HH:MM LEN" in out and "day" in out, True)

# 已匹配标记必须真的渲染出来（Text.append 不解析 markup，踩过）
buf = io.StringIO()
console = Console(file=buf, width=120, force_terminal=False)
wd = w.Warden(console, FakeBridge(replies={
    "snapshot": lambda kw: {"date": kw.get("date"), "now": int(NOW.timestamp() * 1000),
                            "sessions": [api_row("21:00", 25, "s1", "t", True, True, "未开始")]}
}), no_clear=True)
wd.refresh()
wd.paint()
out = buf.getvalue()
check("已匹配标记渲染干净（没有裸露的 markup）", "[cyan]" in out, False)
check("已匹配标记真的出现了", "已匹配" in out, True)

buf = io.StringIO()
console = Console(file=buf, width=120, force_terminal=False)
wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True, day_offset=1)
wd.paint()
out = buf.getvalue()
check("day_offset=1 渲染出「明天」", "明天" in out, True)
wd.day_offset = -1
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
check("day_offset=-1 渲染出「昨天」", "昨天" in buf.getvalue(), True)

# 离线快照：不许假装知道订没订
ks._save_json(ks.STATE_FILE, {
    "date": TODAY.isoformat(),
    "slots": {},
    "api_snapshot": {"at": NOW.isoformat(timespec="seconds"),
                     "sessions": [datetime.datetime.combine(TODAY, datetime.time(21, 0)).isoformat()]},
})
buf = io.StringIO()
console = Console(file=buf, width=120, force_terminal=False)
wd = w.Warden(console, FakeBridge(), no_clear=True, offline=True)
wd._fallback_rows("测试")
wd.paint()
out = buf.getvalue()
check("快照模式标出「快照」而不是「缺」", "快照" in out and " 缺 " not in out, True)

# ---------------------------------------------------------------- 视图（Tab 切换）

print("== 视图：注册 + Tab 切换 ==")
check("注册了契约/专注/统计/任务/设置五个视图", [v.name for v in w.VIEWS],
      ["契约", "专注", "统计", "任务", "设置"])
wd = mkwarden()
check("默认契约视图", (wd.view.name, wd.view_index), ("契约", 0))
wd.handle("tab")
check("tab 切到专注", (wd.view.name, wd.view_index), ("专注", 1))
wd.handle("\t")
check("再一个 Tab 切到统计（Tab 是循环）", (wd.view.name, wd.view_index), ("统计", 2))
wd.handle("\t")
check("第三个 Tab 切到任务", (wd.view.name, wd.view_index), ("任务", 3))
wd.handle("\t")
check("第四个 Tab 切到设置", (wd.view.name, wd.view_index), ("设置", 4))
wd.handle("\t")
check("第五个 Tab 转回契约", (wd.view.name, wd.view_index), ("契约", 0))
wd.cycle_view()
check("cycle_view 切到专注", wd.view.name, "专注")
wd.cycle_view(-1)
check("cycle_view(-1) 切回契约", wd.view.name, "契约")

print("== 专注视图：Shift+Tab 循环专注模式 ==")
clear_mode()
mode_inprog = (NOW - datetime.timedelta(minutes=5)).strftime("%H:%M")
write_csv(["# mode test", "%s,%s,50" % (TODAY_WD, mode_inprog)])
wd = mkwarden()
wd.reload_entries()
check("契约视图里 Shift+Tab 不做事", wd.cycle_focus_mode(), None)
wd.cycle_view()        # 专注
mode_key = wd.view.current_session_key()
check("默认模式是普通", ks.get_idle_mode(mode_key), "normal")
check("Shift+Tab：普通 → 做题", wd.cycle_focus_mode(), "study")
check("模式已绑定到当前 session", ks.load_mode_state()["session_key"], mode_key)
check("状态行显示当前模式", "模式 做题" in wd.live_status(), True)
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
check("专注面板显示模式与阈值", "专注模式" in buf.getvalue() and "3 分钟" in buf.getvalue(), True)
check("Shift+Tab：做题 → 写作", wd.cycle_focus_mode(), "writing")
check("Shift+Tab：写作 → 普通", wd.cycle_focus_mode(), "normal")
check("循环回普通", ks.get_idle_mode(mode_key), "normal")
clear_mode()

print("== 专注视图：目标 / 可暂停次数 / 空闲 / 倒计时 ==")
if os.path.exists(ks.PAUSE_FILE):
    os.remove(ks.PAUSE_FILE)
ks._save_json(ks.IDLE_STATE_FILE, {"session_key": "x", "strikes": 1, "episode_marks": 0,
                                   "last_idle_seconds": 42,
                                   "last_tick_at": NOW.isoformat(timespec="seconds")})
inprog = (NOW - datetime.timedelta(minutes=10)).strftime("%H:%M")
write_csv(["# view test", "%s,%s,50" % (TODAY_WD, inprog)])
wd = mkwarden()
wd.cycle_view()        # 专注
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
out = buf.getvalue()
check("专注视图显示目标", "目标" in out, True)
check("专注视图显示可暂停次数 3/3", "可暂停次数" in out and "3 / 3" in out, True)
check("专注视图显示当前空闲 42s", "42s" in out, True)
check("专注视图显示进行中的 session", "当前 session" in out, True)
check("专注视图不画契约表", "The Contract Table" not in out, True)
check("视图指示有 Tab 切换", "Tab 切换" in out, True)
_hint = wd.view.hint_line("", [])
check("专注提示是 repl_kit.Hint（交给框架排版）",
      isinstance(_hint, w.repl_kit.Hint), True)
check("专注提示只提 stop", ("stop" in str(_hint) and "session HH:MM" not in str(_hint)), True)
check("专注视图有动态状态行", "空闲 42s" in wd.live_status(), True)
check("状态行被 paint() 打在最后一行（供原地刷新）",
      out.rstrip("\n").splitlines()[-1].strip(), wd.live_status())
check("契约视图没有动态状态行（不用定时刷新）",
      wd.views[0].status_line(), None)
# 现在提示行 / 状态行 / 提示符都由 repl_kit 的 chrome 拼，顺序是固定的：
#   <看板> <提示行> <状态行> <提示符>
_chrome = wd.shell.chrome("")
check("chrome 最后一段是提示符", _chrome[-1][1], wd.shell.prompt)
check("状态行紧贴提示符上面", "空闲 42s" in _chrome[-2][1], True)
check("提示行在状态行上面", _chrome[-3][1].startswith("命令"), True)
check("状态行用独立样式类（不跟提示行混在一起）",
      _chrome[-2][0] != _chrome[-3][0], True)
wd.console = Console(file=io.StringIO(), width=40, force_terminal=False)
check("超宽状态行在交给框架前就被截到控制台宽度",
      len(wd.live_status()) < 40, True)

print("== 提示行：用 repl_kit.Hint 排版，空闲 + 命令参数两层 ==")
wd_hint = mkwarden()
check("空闲提示是 repl_kit.Hint",
      isinstance(wd_hint.view.hint_line("", []), w.repl_kit.Hint), True)
idle_text = wd_hint.shell.hint_line("")
check("空闲提示是框架格式（命令: a  b   (note)）",
      idle_text.startswith("命令: ") and "  " in idle_text and "(" in idle_text, True)
check("空闲提示里不再堆一长串手写命令",
      "mute N on|off" not in idle_text, True)
check("day 后提示取值", wd_hint.shell.hint_line("day ").startswith("取值: "), True)
check("day 提示含日期格式", "YYYY-MM-DD" in wd_hint.shell.hint_line("day "), True)
check("mute 第二个参数提示 on/off",
      "on" in wd_hint.shell.hint_line("mute 3 ")
      and "off" in wd_hint.shell.hint_line("mute 3 "), True)
check("session 第二个参数提示时长",
      "25" in wd_hint.shell.hint_line("session 11:15 "), True)
help_labels = [label for _, rows in wd_hint.shell.sections() for label, _ in rows]
check("help 表显示 usage（带参数）", "mute <n> on|off" in help_labels, True)

print("== 看板比屏幕高：裁顶部，不裁底部（提示/状态/视图栏/输入行） ==")
wd_frame = mkwarden()
shell = wd_frame.shell
check("补全菜单浮在看板上（不占输入行下面 8 行）",
      shell.reserve_space_for_menu, 0)
shell._terminal_rows = lambda: 80
full = shell.frame_text()
check("默认看板确实比矮终端高", len(full.splitlines()) > 12, True)
shell._terminal_rows = lambda: 14
trimmed = shell.frame_text()
lines = trimmed.splitlines()
check("裁到终端预算内（14 行 - 提示 - 输入）", len(lines) <= 12, True)
check("从顶部裁：The Warden 标题滚走", "The Warden" not in plain(trimmed), True)
check("视图栏不在看板里（所以裁屏裁不掉它）", "视图 1 契约" in plain(trimmed), False)
check("视图栏改由框架的 footer 画，永远在",
      "视图 1 契约" in plain(shell.footer_text()), True)
shell._terminal_rows = lambda: 80
check("终端够高时不裁", len(shell.frame_text().splitlines()), len(full.splitlines()))

print("== 专注视图：stop = 消耗一次暂停（不是停 daemon）==")
wd.bridge.calls = []
check("专注视图 stop 受理", wd.handle("stop"), True)
ps = ks.pause_status()
check("消耗一次暂停（今天剩 2 次）", ps["remaining"], 2)
check("暂停生效中", ps["active"], True)
check("专注视图 stop 没碰 bridge", wd.bridge.calls, [])

print("== 专注视图：预约命令没用 ==")
before = open(CSV, encoding="utf-8").read()
check("session 在专注视图不执行", wd.handle("session 22:00 50"), True)
check("data.csv 没被改", open(CSV, encoding="utf-8").read(), before)
check("提示只认 stop", any("只认 stop" in m[2] for m in wd.messages), True)

print("== 切回契约视图，原来的命令还在 ==")
wd.go_to_view(w.ContractView)
check("切回契约视图", wd.view.name, "契约")
check("契约视图认 day 0", wd.handle("day 0"), True)
check("契约视图认 mute（会走 bridge，不炸就行）", wd.handle("mute 1 on") in (True, False), True)

# ---------------------------------------------------------------- 帧式重画

print("== 重画：擦除交给 prompt_toolkit，warden 只负责交出看板 ==")

# warden 不再手算「上一帧占几行」。它只提供一个 frame() —— 一个 rich
# renderable —— 由 repl_kit/prompt_toolkit 按单元格 diff 上屏。
buf = io.StringIO()
console = Console(file=buf, width=100, force_terminal=True, height=40)
wd = w.Warden(console, FakeBridge(), no_clear=False, offline=True)
wd.paint()
first = buf.getvalue()
check("画一帧 = 把看板打出来", "The Contract Table" in first, True)
check("画帧本身不发任何擦除转义",
      ("\x1b[J" in first) or ("\x1b[2J" in first) or ("\x1b[1A" in first), False)
buf.seek(0)
buf.truncate()
wd.paint()
check("两次 paint = 两份追加内容（擦除不是 warden 的事了）",
      buf.getvalue().count("The Contract Table"), 1)

# frame() 必须是可复用的 renderable：框架每帧都会调它
frame = wd.frame()
check("frame() 交出 renderable 而不是字符串", isinstance(frame, w.Group), True)

# chrome 的头一段就是那块看板
shell = wd.shell
# 钉住终端高度：这一组比的是「看板内容」，不是裁切。非 TTY 下没有 prompt_toolkit
# 的 Application，行数退回 shutil 默认值（24）；真 TTY 下由 app.output 给。
shell._terminal_rows = lambda: 80
chrome = shell.chrome("")
# 看板是 rich 渲染出来的 ANSI，prompt_toolkit 会把它拆成很多片段，所以拼起来看
chrome_text = "".join(fragment[1] for fragment in chrome)
check("chrome 里有看板", "The Contract Table" in chrome_text, True)
check("看板只出现一次（不会和提示行重复）",
      chrome_text.count("The Contract Table"), 1)
check("chrome 里看板在前、提示符在后",
      chrome_text.index("The Contract Table") < chrome_text.index(shell.prompt), True)

# 空表 / 窄终端仍然不炸
for width in (40, 80, 160):
    buf2 = io.StringIO()
    console2 = Console(file=buf2, width=width, force_terminal=False)
    wd2 = w.Warden(console2, FakeBridge(), no_clear=True, offline=True)
    wd2.paint()
    check("width=%d frame() 不炸" % width, "Traceback" in buf2.getvalue(), False)

# 非 TTY（管道/测试）不该出现任何 ANSI
buf3 = io.StringIO()
console3 = Console(file=buf3, width=100, force_terminal=False)
wd3 = w.Warden(console3, FakeBridge(), no_clear=False, offline=True)
wd3.paint()
check("非 TTY（管道/测试）不写任何 ANSI", "\x1b[" in buf3.getvalue(), False)

print("== 打字不掉帧：同一秒里不重复排版，状态一变立刻重排版 ==")
wd_fast = mkwarden()
shell_fast = wd_fast.shell
shell_fast._terminal_rows = lambda: 80
shell_fast._frame_clock = lambda: 5000          # 钉住「现在」，免得踩秒边界
_built = {"n": 0}


def _counting_render():
    """包一层 view.render：数一数到底排版了几次。"""
    _built["n"] += 1
    return wd_fast.frame()


_i = shell_fast.view_index
shell_fast._views[_i] = dataclasses.replace(shell_fast._views[_i], render=_counting_render)
first_frame = shell_fast.frame_text()
check("第一帧真的排版了一次", _built["n"], 1)
check("同一秒里再画 → 直接复用上一个字符串（同一个对象）",
      shell_fast.frame_text() is first_frame, True)
check("复用 = 一次都没重新排版", _built["n"], 1)
check("连敲 10 个键也不新增排版", [shell_fast.chrome("task s") for _ in range(10)]
      and _built["n"], 1)
wd_fast.say("状态变了", "info")
check("状态一变 → 下一次就重排版", (shell_fast.frame_text(), _built["n"])[1], 2)
check("重排版之后新消息就在画面上", "状态变了" in plain(shell_fast.frame_text()), True)
shell_fast._frame_clock = lambda: 5001
shell_fast.frame_text()
check("跨过一秒 → 自然重排版（时钟得走）", _built["n"], 3)
check("窗口大小变了也要重排版",
      (shell_fast.invalidate_frame(), shell_fast.frame_text(), _built["n"])[2], 4)
# 快路径必须和框架原版拼出一样的东西（不然就是「快但画错了」）
_fast = [(style, text) for style, text in shell_fast.chrome("task ")]
_slow = [(style, text) for style, text in w.repl_kit.ReplShell.chrome(shell_fast, "task ")]
check("快路径和框架原版逐字一致", _fast, _slow)

print("== 打字不掉帧：日志只读尾巴（daemon 日志能长到几十 MB）==")
_real_log = ks.LOG_FILE
log_path = os.path.join(TMP, "big.log")
with open(log_path, "w", encoding="utf-8") as f:
    for i in range(20000):
        f.write("第 %d 行日志\n" % i)
ks.LOG_FILE = log_path
try:
    check("尾巴是最后一行", wd_fast.log_tail(1), ["第 19999 行日志"])
    check("能要不止一行", wd_fast.log_tail(3),
          ["第 19997 行日志", "第 19998 行日志", "第 19999 行日志"])
    memo = wd_fast.log_tail(1)
    check("文件没变就直接用上次的结果（不再读盘）", wd_fast.log_tail(1) is memo, True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("新的一行\n")
    check("追加之后能读到新尾巴", wd_fast.log_tail(1), ["新的一行"])
    check("文件没了也不炸", (os.remove(log_path), wd_fast.log_tail(1))[1], [])
finally:
    ks.LOG_FILE = _real_log

# 提示符：欠着罪时换成「宣言> 」
check("没欠账时用默认提示符", wd3.prompt_text(), None)
check("欠着罪时提示符变成「宣言> 」", wd3.shell.prompt, "warden> ")

print("== 消息区：屏幕只显示最近 3 条，内部多留一点 ==")
check("屏幕显示条数常量", w.MESSAGE_SHOW, 3)
wd = mkwarden()
for i in range(15):
    wd.say("msg%d" % i)
check("内部只留 MESSAGE_KEEP 条", len(wd.messages), w.MESSAGE_KEEP)
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
out = buf.getvalue()
check("屏幕上只剩最近 3 条", [("msg%d" % i) in out for i in (14, 13, 12)], [True, True, True])
check("第 4 条旧的没上屏", "msg11" in out, False)

# ---------------------------------------------------------------- 统计视图 / stat 命令

print("== 统计视图 + stat 命令 ==")
write_csv(ROWS)
clear_state()
ks._save_json(ks.VERDICTS_FILE, {"abandoned": {
    "2026-01-09T09:00": {"at": "2026-01-09T10:30:00", "why": "walk-of-shame", "note": "测试用"}}})
STAT_PULLS[:] = []
wd = mkwarden()
check("统计视图没有动态状态行（不用每秒刷）", wd.views[2].status_line(), None)
check("一进来不是统计视图", wd.view.name, "契约")

check("任意视图敲 stat 都受理", wd.handle("stat"), True)
check("stat 切到了统计视图", wd.view.name, "统计")
check("stat 拉到了数字（完成 6 场 / 10 天）",
      (wd.stat["total_completed"], wd.stat["days"]), (6, 10))

buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
out = buf.getvalue()
for label in ("Stat · 统计", "每天平均", "空 天", "峰 值", "中位数", "p25", "最近 10 天",
              "按月", "永不重订", "完成 6"):
    check("统计面板有 %s" % label, label in out, True)
check("永不重订逐行列（一条一行）", "2026-01-09 09:00" in out, True)
check("永不重订带认罪时间", "2026-01-09 10:30" in out, True)
check("统计视图不画契约表", "The Contract Table" not in out, True)

wd.go_to_view(w.ContractView)
check("切回契约视图", wd.view.name, "契约")
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
out = buf.getvalue()
check("平常视图不逐行列永不重订名单", "2026-01-09 09:00" not in out, True)
check("那一行已经换成警告（状态框里不再提永不重订名单）",
      "永不重订名单" not in out, True)
check("契约视图最上面是「状态」框（平滑重线边框）", "┏━━ 状态" in out, True)
check("状态框里有契约执行情况", "契约" in out and "data.csv" in out, True)
check("状态框里有警告", "→" in out, True)

# 缓存语义：内存里已有就别再拉；stat refresh 强制重拉
STAT_PULLS[:] = []
wd.handle("stat")
check("第二次 stat 不重拉（内存里已有）", STAT_PULLS, [])
wd.handle("stat refresh")
check("stat refresh → 立刻增量刷新（force，但不是 full）", STAT_PULLS, [(True, False)])
wd.handle("stat refresh full")
check("stat refresh full → 全量重拉", STAT_PULLS, [(True, False), (True, True)])
wd.stat_at = datetime.datetime.now() - datetime.timedelta(seconds=w.st.CACHE_TTL + 10)
wd.handle("stat")
check("内存里那份放超过 TTL → 再敲 stat 会重拉", len(STAT_PULLS), 3)
wd.go_to_view(w.StatsView)
check("Tab 进统计视图不重拉", len(STAT_PULLS), 3)

# 拉取失败：不掀 TUI，视图给一行说明
def boom(force=False, **kw):
    raise OSError("网络炸了")


w.st.load = boom
wd = mkwarden()
check("拉取失败 stat 返回 False（不炸）", wd.handle("stat"), True)
check("失败时 had_error 置位", wd.had_error, True)
check("失败时没有数字", wd.stat, None)
buf = io.StringIO()
wd.console = Console(file=buf, width=120, force_terminal=False)
wd.paint()
check("失败也把说明打在统计视图上", "拉取失败" in buf.getvalue(), True)
w.st.load = fake_stat_load        # 复原：后面的用例还要离线拉假的

print("== 非 TTY 下：一行 Tab / Shift+Tab 也算切换 ==")

# 以前这是 read_command 的活儿（逐键读，遇到 Tab 就抛异常）。
# 现在输入归 repl_kit，warden 只在 handle() 里认这两种「命令行的切换写法」。
wd = mkwarden()
check("默认在契约视图", wd.view.name, "契约")
check("一行 Tab 切到专注视图", (wd.handle("\t"), wd.view.name), (True, "专注"))
check("一行 \\x1b[Z 认作 Shift+Tab（切专注模式）", wd.handle("\x1b[Z"), True)
check("shift-tab 文字写法也认", wd.handle("shift-tab"), True)
check("普通一行照常执行（不返回 False）", wd.handle("day 0"), True)
check("quit 仍然返回 False", wd.handle("quit"), False)

shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("%d 项失败：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全绿。")
