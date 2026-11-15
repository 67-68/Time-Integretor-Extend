#!/usr/bin/env python3
"""keytime.py / keytime_rules.py 回归测试：规则表 / 匹配 / 模板变量 / 挤压 / 播报 / Bark。

    python3 test/test_keytime.py        # 全绿退出 0，有失败退出 1

不碰系统音量、不发声、不联网：``Announcer`` 的 say / 音量全部注入假函数；
Bark 用本机 http.server 收一条真 POST，验证 JSON 结构和失败分支。
"""
import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
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


TMP = tempfile.mkdtemp(prefix="keytime-test-")
os.environ["TI_KEYTIME_JSON"] = os.path.join(TMP, "keytime.json")
os.environ["TI_KEYTIME_STATE"] = os.path.join(TMP, "keytime-state.json")
os.environ["TI_KEYTIME_DEVICES"] = os.path.join(TMP, "devices.json")
os.environ["TI_KEYTIME_RULES"] = os.path.join(TMP, "keytime-rules.toml")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "calendar-download.json")

kt = load("keytime", "keytime.py")
kr = kt.kr

D = datetime.date(2026, 11, 14)     # 周六，和用户真实场景同一天（11-14 是周六）


def dt(hm, days=0):
    h, m = [int(x) for x in hm.split(":")]
    base = datetime.datetime(D.year, D.month, D.day, h, m)
    return base + datetime.timedelta(days=days)


def write_download(blocks=None, legacy_keytimes=None, events=None):
    data = {"version": 1, "generated_at": "2026-11-14T00:00:00",
            "start_date": D.isoformat(), "days": 8,
            "events": events or [], "periods": [],
            "all_calendars": ["工作", "空闲", "运动", "休息"],
            "mode": "profiles"}
    if blocks is not None:
        data["time_blocks"] = blocks
    if legacy_keytimes is not None:
        data["keytimes"] = legacy_keytimes
    with open(os.environ["TI_CALENDAR_DOWNLOAD"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def ev(date, start, end, calendar, title=""):
    return {"date": date, "start": start, "end": end, "calendar": calendar,
            "title": title or calendar, "all_day": False,
            "start_iso": "%sT%s:00" % (date, start), "end_iso": "%sT%s:00" % (date, end)}


# ---------------------------------------------------------------- 配置

print("== 配置 ==")
cfg = kt.load_config()
check("配置里不再有硬编码日历列表", "calendars" in cfg, False)
check("默认播报节拍 60s", cfg["announce_sec"], 60)
check("默认音量 10", cfg["volume"], 10)
check("默认推送档位 timeSensitive", cfg["push"]["level"], "timeSensitive")

kt.save_config({"calendars": ["通勤"], "volume": 30, "push": {"sound": "minute"}})
cfg = kt.load_config()
check("旧配置里的 calendars 不炸、也不生效", cfg.get("calendars"), ["通勤"])
check("缺的键补默认（announce_sec）", cfg["announce_sec"], 60)
check("push 子字典合并而不是替换", cfg["push"]["sound"], "minute")
check("push 其他键还在", cfg["push"]["level"], "timeSensitive")
kt.save_config(kt.DEFAULT_CONFIG)

# ---------------------------------------------------------------- 规则文件

print("== 规则文件（keytime_rules）==")
path = kr.rules_path()
check("规则文件路径走 TI_KEYTIME_RULES", path, os.environ["TI_KEYTIME_RULES"])
ruleset, rules_error = kr.load_rules()
check("第一次读会写出厂文件", os.path.exists(path), True)
text = open(path, encoding="utf-8").read()
check_in("出厂文件自带注释", "#  Key Time 规则表", text)
check_in("出厂文件自带变量说明", "{overdue_mins}", text)
check_in("出厂文件有 [[rule]]", '[[rule]]', text)
check_in("出厂文件带用户要的运动模板", "🚨 [肉体僵死警报] 动作：出去随便干点什么", text)
check("默认规则 4 条", [r.id for r in ruleset.rules],
      ["exercise", "commute", "rest", "generic"])
check("默认 display 不是空", bool(ruleset.display), True)
check("没有解析错误", rules_error, "")

check("名字匹配：运动", ruleset.match("运动", "运动").id, "exercise")
check("名字匹配优先于 calendar", ruleset.match("休息", "运动").id, "exercise")
check("calendar 匹配：通勤", ruleset.match("通勤", "回家").id, "commute")
check("calendar 匹配：休息", ruleset.match("休息", "强制断电").id, "rest")
check("都没命中 → 不报警", ruleset.match("工作", "SAT 3H"), None)
check("兜底规则默认停用：上课不报警", ruleset.match("上课", "数学"), None)

# 自定义规则：优先级 / 通配符 / 兜底 / 停用 / 裸值 / 注释
with open(path, "w", encoding="utf-8") as f:
    f.write('''
# 这是注释
[display]
line = "-> {block} · {when}"   # 行尾注释

[[rule]]
id = "run"
name = "跑*"
text = """{block} {overdue_mins}"""

[[rule]]
id = "sport"
calendar = "运动"
text = """日历运动 {block}"""

[[rule]]
id = "bare"
name = 散步
text = """裸值 {block}"""

[[rule]]
id = "every"
text = """兜底 {block}"""

[[rule]]
id = "off"
name = "睡觉"
enabled = false
text = """不该出现"""
''')
ruleset, rules_error = kr.load_rules()
check("自定义规则解析没有错误", rules_error, "")
check("display 行尾注释没被吃进值", ruleset.display, "-> {block} · {when}")
check("通配符 + 名字优先", ruleset.match("运动", "跑步").id, "run")
check("名字没命中退到 calendar", ruleset.match("运动", "拉伸").id, "sport")
check("裸值也认", ruleset.match("随便", "去散步").id, "bare")
check("兜底（没写 name/calendar）刚才没命中时生效", ruleset.match("工作", "SAT").id, "every")
check("停用的规则不参与", ruleset.match("随便", "睡觉").id, "every")
check("通配符按整串匹配（不是包含）", ruleset.match("工作", "准备跑步").id, "every")
ruleset.rules[0].enabled = False
check("规则停用后让位给 calendar", ruleset.match("运动", "跑步").id, "sport")
ruleset.rules[0].enabled = True

# 解析错误要说清哪一行
broken, errs = kr.parse_rules('''
[[rule]]
id = "x"
nmae = "运动"
text = """
没有收尾
''')
check("写错键名会报行", any("nmae" in e for e in errs), True)
check("多行字符串没闭上会报错", any("没有收尾" in e for e in errs), True)
check("错的地方仍然解析出规则（不至于全空）", [r.id for r in broken.rules], ["x"])

with open(path, "w", encoding="utf-8") as f:
    f.write("# 只剩注释\n")
empty, errs = kr.load_rules()
check("空规则文件 → 0 条规则", len(empty.rules), 0)
check_in("空规则文件给提示", "没有任何 [[rule]]", errs)

# 恢复出厂
kr.save_default_rules()
ruleset, rules_error = kr.load_rules()
check("reset 恢复 4 条", len(ruleset.rules), 4)
check("reset 后没有错误", rules_error, "")

# 多行字符串 / 转义 / 引号里的 #
multi, errs = kr.parse_rules(r'''
[[rule]]
id = "m"
name = "a#b"
text = """
第一行
第二行 \"引号\"\t制表
"""
''')
check("多行文本两行", multi.rules[0].text, '第一行\n第二行 "引号"\t制表')
check("引号里的 # 不算注释", multi.rules[0].name, "a#b")
check("多行文本没有报错", errs, [])

# ---------------------------------------------------------------- 模板渲染

print("== 模板与变量 ==")
check("未知变量原样保留", kt.format_vars("{nope}", {"block": "x"}), "{nope}")
check("已知变量替换", kt.format_vars("{block}!", {"block": "运动"}), "运动!")
check("{{ }} 是字面量", kt.format_vars("{{block}}", {"block": "x"}), "{block}")


def segments_text(lines):
    return kt.plain_text(lines)


def line_styles(lines):
    return [tuple(style for style, _text in line) for line in lines]


rule = ruleset.match("运动", "运动")
exercise = kt.KeyTime(D.isoformat(), "16:00", "17:00", "运动", "运动", rule=rule)
ctx = {
    "sessions": [{"start": datetime.datetime(2026, 11, 14, 16, 30), "duration": 50,
                  "tasks": ["[Run] 物理 2023 P1"]}],
    "squeeze": {"delay": 12, "total": 1, "pushed": [],
                "dropped": [{"duration": 50, "tasks": ["[Run] 物理 2023 P1"]}],
                "extra_sessions": 2, "extra_minutes": 50},
    "chore": "打印作业",
}
lines = kt.render_lines(exercise, dt("16:12"), ctx)
blob = segments_text(lines)
check_in("运动模板：标题", "🚨 [肉体僵死警报] 动作：出去随便干点什么", blob)
check_in("运动模板：超时分钟", "已粘在椅子上超时：12 分钟", blob)
check_in("运动模板：应于 16:00 弹射", "应于 16:00 弹射，现已 16:12", blob)
check_in("运动模板：下一个 session 的任务", "后续的【[Run] 物理 2023 P1】", blob)
check_in("运动模板：红线下楼", "绝对不准继续待在电脑前", blob)
check_not_in("运动模板：** 变成加粗而不是星号", "**", blob)
check("第一行红底白字", lines[0][0][0], "bold white on red")
check("! 前缀是红字", "bold red" in line_styles(lines)[-1], True)
timeout_line = [styles for styles, line in zip(line_styles(lines), lines)
                if any("⏱️" in text for _style, text in line)]
check("> 前缀是黄字", timeout_line[0], ("yellow",))
check("加粗片段真的加粗",
      any("bold" in style for line in lines for style, _t in line), True)
check_in("运动语音：超时分钟", "运动超时 12 分钟", kt.speech_text(exercise, dt("16:12"), ctx))
check("运动推送标题用 headline", kt.headline(exercise, dt("16:12"), ctx),
      "🚨 [肉体僵死警报] 动作：出去随便干点什么")

# 没有后续 session 时 next_task 有兜底词
lines = kt.render_lines(exercise, dt("16:12"), {})
check_in("没有下一场时用「后面的安排」", "后续的【后面的安排】", segments_text(lines))

# 通勤模板（calendar 规则）：挤压后果 + 加班句
commute = kt.KeyTime(D.isoformat(), "16:00", "17:00", "通勤", "回家",
                     rule=ruleset.match("通勤", "回家"))
blob = segments_text(kt.render_lines(commute, dt("23:10"), ctx))
check_in("通勤模板：被挤出的任务", "【[Run] 物理 2023 P1】", blob)
check_in("通勤模板：加班句", "额外加班 2 个 Session", blob)
check_in("通勤语音：挤出句", "已经被挤出今日排期的是：[Run] 物理 2023 P1",
         kt.speech_text(commute, dt("23:10"), ctx))
# 只是顺延、没有挤出时，加班那行自动消失
push_ctx = {"squeeze": {"delay": 12, "total": 2,
                        "pushed": [{"duration": 50, "tasks": ["数学"]}],
                        "dropped": [], "extra_sessions": 0, "extra_minutes": 0}}
blob = segments_text(kt.render_lines(commute, dt("16:12"), push_ctx))
check_in("顺延模板：整体后移", "整体后移 12 分钟", blob)
check_not_in("没挤出就不出现加班句", "额外加班", blob)

# 休息模板：有总目标 / 没总目标
rest = kt.KeyTime(D.isoformat(), "10:40", "11:00", "休息", "强制断电",
                  rule=ruleset.match("休息", "强制断电"))
blob = segments_text(kt.render_lines(rest, dt("10:48"), {"goal": "在 EOS 历史中取得好成绩"}))
check_in("休息模板：生理算力", "生理算力坍塌后果", blob)
check_in("休息模板：总目标", "在 EOS 历史中取得好成绩", blob)
check_in("休息模板：离开屏幕", "现在立刻离开屏幕 20 分钟", blob)
blob = segments_text(kt.render_lines(rest, dt("10:48"), {}))
check_in("没有总目标给占位", "keytime goal", blob)
check_in("休息语音", "休息超时 8 分钟", kt.speech_text(rest, dt("10:48"), {}))

# 确认后的画面：标题 + 已确认
acked = kt.render_lines(commute, dt("16:12"), {"acked_at": "2026-11-14T16:13"}, acked=True)
check("确认后两行", len(acked), 2)
check_in("确认后显示已确认", "已确认（16:13）", segments_text(acked))
check_in("确认后计时器还在走", "计时器仍在走", segments_text(acked))

# 直接构造的 KeyTime（没有规则）走兜底模板，不炸
plain = kt.KeyTime(D.isoformat(), "16:00", "17:00", "提醒", "买菜")
blob = segments_text(kt.render_lines(plain, dt("16:12")))
check_in("没有规则的兜底模板", "立刻处理这个 key time", blob)
check("kind 猜成 generic", plain.kind, "generic")

# 顶部那一行（[display] line 模板）
line = kt.display_line(exercise, dt("15:30"), ruleset)
check("display 模板渲染", line, "🔴 KEY TIME 11-14 运动 · 30 分钟后 · 16:00-17:00")
line = kt.display_line(exercise, dt("15:59"), ruleset)
check_in("差 1 分钟", "1 分钟后", line)
line = kt.display_line(exercise, dt("14:00"), ruleset)
check_in("小时级提示", "2 小时 0 分钟后", line)

# ---------------------------------------------------------------- 中间层解析

print("== parse_keytimes ==")
write_download(blocks=[
    ev(D.isoformat(), "16:00", "17:00", "运动", "运动"),
    ev(D.isoformat(), "10:40", "11:00", "休息", "强制断电"),
    ev(D.isoformat(), "09:00", "12:00", "工作", "SAT 3H"),
    ev((D + datetime.timedelta(days=1)).isoformat(), "08:00", "08:30", "通勤", "去学校"),
])
kts, err = kt.load_keytimes(rules=ruleset)
check("只有匹配规则的时间块成为 key time", len(kts), 3)
check("按开始时间排序", [k.start for k in kts], ["10:40", "16:00", "08:00"])
check("按规则给出 kind", [k.kind for k in kts], ["rest", "exercise", "commute"])
check("工作块没有被误报", [k.calendar for k in kts], ["休息", "运动", "通勤"])
check("没有解析错误", err, "")
check("key 形如 日期T时刻", kts[0].key(), "%sT10:40" % D.isoformat())
check("时间块名字 = 事件标题", kts[1].block(), "运动")

# 老中间层：keytimes 一节（已经按日历名筛过）
write_download(legacy_keytimes=[
    ev(D.isoformat(), "16:00", "17:00", "通勤", "回家"),
    ev(D.isoformat(), "09:00", "12:00", "工作", "SAT 3H")])
kts2, _ = kt.load_keytimes(rules=ruleset)
check("老 keytimes 一节也按规则筛", [k.calendar for k in kts2], ["通勤"])

# 更老：只有 events 一节（非全天）
write_download(events=[ev(D.isoformat(), "16:00", "17:00", "运动", "运动"),
                       ev(D.isoformat(), "09:00", "12:00", "工作", "SAT 3H"),
                       dict(ev(D.isoformat(), "00:00", "23:59", "休息", "假"), all_day=True)])
kts3, _ = kt.load_keytimes(rules=ruleset)
check("最老的 events 一节也认（全天事件跳过）", [k.calendar for k in kts3], ["运动"])

check("_hm 坏格式 → None", kt._hm("25:00"), None)
check("classify 仍能猜通勤", kt.classify("通勤", ""), "commute")

# ---------------------------------------------------------------- 报警判定

print("== alarm_target ==")


def kt_at(start, end="17:00", calendar="通勤", date=D.isoformat()):
    return kt.KeyTime(date, start, end, calendar)


kts = [kt_at("16:00", "17:00")]
check("15:59 还没到点：不报警", kt.alarm_target(dt("15:59"), kts, {}), None)
got = kt.alarm_target(dt("16:12"), kts, {})
check("16:12 报警", got.start if got else None, "16:00")
check("超时 12 分钟", got.overdue_minutes(dt("16:12")), 12)
check("确认后不再报警",
      kt.alarm_target(dt("16:12"), kts, {"acked": {"%sT16:00" % D.isoformat(): {}}}), None)
check("结束前 1 分钟仍然报",
      kt.alarm_target(dt("16:59"), [kt_at("16:00", "17:00")], {}).start, "16:00")
check("过了结束时间就不再报警（用户明确要求）",
      kt.alarm_target(dt("17:01"), [kt_at("16:00", "17:00")], {}), None)
two = [kt_at("16:00", "17:00", "通勤"), kt_at("16:05", "17:00", "休息")]
check("重叠取最早的", kt.alarm_target(dt("16:10"), two, {}).calendar, "通勤")
yesterday = [kt.KeyTime("2026-11-13", "23:30", "23:59", "休息")]
check("块内仍然报", kt.alarm_target(dt("23:40", days=-1), yesterday, {}).start, "23:30")
check("隔夜已过结束时间 → 不追账", kt.alarm_target(dt("00:10"), yesterday, {}), None)

print("== upcoming / next ==")
kts = [kt_at("16:00"), kt.KeyTime("2026-11-15", "08:00", "08:30", "通勤")]
check("upcoming 只看还没开始的（含明天）",
      [k.start for k in kt.upcoming(dt("15:00"), kts)], ["16:00", "08:00"])
check("next 含已经开始的", kt.next_keytime(dt("16:12"), kts).start, "16:00")
check("没有未来项 → None",
      kt.next_keytime(dt("20:00"), [kt_at("16:00", "17:00")]), None)
check("include_started 也能取到进行中的",
      [k.start for k in kt.upcoming(dt("16:12"), [kt_at("16:00")], include_started=True)],
      ["16:00"])

# ---------------------------------------------------------------- 挤压预测

print("== squeeze ==")
sessions = [
    {"start": dt("16:30"), "duration": 50, "tasks": ["[Run] 物理 2023 P1"]},
    {"start": dt("17:30"), "duration": 50, "tasks": ["[Run] 历史 Essay 提纲"]},
    {"start": dt("18:30"), "duration": 25, "tasks": ["数学"]},
]
sq = kt.squeeze(dt("16:12"), kt_at("16:00", "17:00"), sessions, day_end="23:30")
check("delay = 12", sq["delay"], 12)
check("三场都只是顺延，没被挤出", len(sq["pushed"]), 3)
check("总场次记下来了", sq["total"], 3)
check("第一场顺延到 16:42", sq["pushed"][0]["new_start"], dt("16:42"))
check("没有补课场次", sq["extra_sessions"], 0)

sq = kt.squeeze(dt("23:10"), kt_at("16:00", "17:00"), sessions, day_end="23:30")
check("深夜超时：全部被挤出", len(sq["dropped"]), 3)
check("被挤出 125 分钟", sq["extra_minutes"], 125)
check("补课 5 场（125/25）", sq["extra_sessions"], 5)
check_in("squeeze_text 说挤出", "挤出今日排期", kt.squeeze_text(sq))
check_in("squeeze_speech 说挤出", "已经被挤出今日排期的是", kt.squeeze_speech(sq))

sq = kt.squeeze(dt("22:45"), kt_at("16:00", "17:00"), sessions[:1], day_end="23:30")
check("50 分钟场 + 405 分钟 delay → 挤出", len(sq["dropped"]), 1)

sq = kt.squeeze(dt("16:12"), kt_at("16:00", "17:00"), [], day_end="23:30")
check("没有 session 也能算（不崩）", sq["dropped"], [])
check_in("没有后续排期也有话说", "从睡眠和明天里扣", kt.squeeze_text(sq))

sq = kt.squeeze(dt("16:00"), kt_at("16:00", "17:00"), sessions, day_end="23:30")
check_in("刚好到点：说后面还有几场", "还有 3 场排期", kt.squeeze_text(sq))

sq = kt.squeeze(dt("16:12"), kt_at("16:00", "17:00"),
                [{"start": "16:30", "duration": 50, "tasks": ["A"]}], day_end="23:30")
check("start 用字符串也认", sq["pushed"][0]["new_start"], dt("16:42"))

# ---------------------------------------------------------------- 播报线程

print("== Announcer ==")
spoken, volumes = [], []

ann = kt.Announcer(kt.load_config(),
                   speaker=lambda text, voice: spoken.append(text),
                   volume_setter=lambda pct: volumes.append(pct),
                   volume_getter=lambda: 55)
ann.speak("第一句")
for _ in range(50):
    if spoken:
        break
    time.sleep(0.02)
check("播报执行了", spoken, ["第一句"])
check("播报前设了音量 10", volumes[0], 10)
check("记下了原音量", ann.saved_volume, 55)

# 连发三句：只有最后一句会落地（正在播的不会被抢）
spoken.clear()
ann._speaker = lambda text, voice: (time.sleep(0.15), spoken.append(text))
ann.speak("A")
time.sleep(0.05)
ann.speak("B")
ann.speak("C")
for _ in range(100):
    if len(spoken) >= 2:
        break
    time.sleep(0.02)
check("前一句播完后播最新一句（B 被 C 顶掉）", spoken, ["A", "C"])

ann.stop()
check("停止后还原音量", volumes[-1], 55)

# 音量还原可以关掉
volumes.clear()
ann2 = kt.Announcer({**kt.load_config(), "restore_volume": False},
                    speaker=lambda t, v: None,
                    volume_setter=lambda pct: volumes.append(pct),
                    volume_getter=lambda: 80)
ann2.speak("x")
time.sleep(0.1)
ann2.stop()
check("restore_volume=False 不还原", volumes, [10])

# ---------------------------------------------------------------- Bark

print("== Bark 负载 ==")
device = {"name": "iphone", "key": "abc123", "server": "http://127.0.0.1:9",
          "level": "", "group": "", "sound": "", "enabled": True}
payload = kt.build_push_payload(device, "标题", "正文",
                                {"level": "timeSensitive", "group": "keytime",
                                 "sound": "alarm", "ttl": 3600})
check("device_key 进 body", payload["device_key"], "abc123")
check("level 原样", payload["level"], "timeSensitive")
check("默认 isArchive=1", payload["isArchive"], "1")
check("设备级 sound 覆盖参数", kt.build_push_payload(
    {**device, "sound": "minuet"}, "t", "b", {"sound": "alarm"})["sound"], "minuet")
check("stack 模式不带 id", "id" in kt.build_push_payload(device, "t", "b", {}), False)
cfg_stack_off = {"push": {"stack_notifications": False, "level": "timeSensitive"}}
check("不堆叠模式带 id", kt.push_params(cfg_stack_off, seq="2026-11-14T16:00")["id"],
      "keytime-2026-11-14T16:00")
check("确认后推送降级成 passive", kt.push_params(cfg_stack_off, mode="acked")["level"],
      "passive")


class Handler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):                                  # noqa: N802 - http.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        Handler.received.append((self.path, self.headers.get("Content-Type"),
                                 json.loads(raw) if raw else {}))
        code = 200 if "bad" not in self.path else 400
        body = json.dumps({"code": code, "message": "ok" if code == 200 else "nope"})
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):                       # noqa: A003 - 静音测试服务器
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = "http://127.0.0.1:%d" % server.server_address[1]
dev = {**device, "server": base}
ok, detail = kt.push_one(dev, "[KEY TIME] 通勤", "已超时 12 分钟",
                         {"level": "timeSensitive"})
check("推送成功", ok, True)
check("真的打到了 /push", Handler.received[-1][0], "/push")
check("Content-Type 是 JSON",
      Handler.received[-1][1], "application/json; charset=utf-8")
check("标题送达", Handler.received[-1][2]["title"], "[KEY TIME] 通勤")
check("中文正文没坏", Handler.received[-1][2]["body"], "已超时 12 分钟")

ok, detail = kt.push_one({**dev, "server": base + "/bad"}, "t", "b", {})
check("4xx 判失败", ok, False)
check_in("失败信息带状态码", "400", detail)
ok, detail = kt.push_one({**device, "server": "http://127.0.0.1:9"}, "t", "b", {},
                         timeout=1)
check("连不上判失败", ok, False)

results = kt.push_all([dev, {**dev, "name": "off", "enabled": False}], "t", "b", {})
check("push_all 只推启用的设备", [r[0] for r in results], ["iphone"])
server.shutdown()

# ---------------------------------------------------------------- 状态文件

print("== 状态 ==")
kt.save_state({"acked": {"2026-11-14T16:00": {"at": "2026-11-14T16:13"}}})
state = kt.load_state()
check("确认记录读得回来", list(state["acked"]), ["2026-11-14T16:00"])
check("acked() 认得", kt.acked(kt_at("16:00"), state), True)
check("没确认的不算", kt.acked(kt_at("17:00"), state), False)
check("状态文件内容合法 JSON",
      isinstance(json.load(open(os.environ["TI_KEYTIME_STATE"], encoding="utf-8")), dict),
      True)

print("== devices ==")
kt.save_devices([{"name": "iphone", "key": "k1"}, {"name": "", "key": ""}])
devs = kt.load_devices()
check("空 key 被丢弃", len(devs), 1)
check("默认服务器", devs[0]["server"], "https://api.day.app")
check("默认 enabled", devs[0]["enabled"], True)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print("失败 %d 项 ❌" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("全绿。")
