#!/usr/bin/env python3
"""keytime.py 回归测试：日历模型 / 报警判定 / 挤压预测 / 播报 / Bark 负载。

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


TMP = tempfile.mkdtemp(prefix="keytime-test-")
os.environ["TI_KEYTIME_JSON"] = os.path.join(TMP, "keytime.json")
os.environ["TI_KEYTIME_STATE"] = os.path.join(TMP, "keytime-state.json")
os.environ["TI_KEYTIME_DEVICES"] = os.path.join(TMP, "devices.json")
os.environ["TI_CALENDAR_DOWNLOAD"] = os.path.join(TMP, "calendar-download.json")

kt = load("keytime", "keytime.py")

D = datetime.date(2026, 11, 14)     # 周六，和用户真实场景同一天（11-14 是周六）


def dt(hm, days=0):
    h, m = [int(x) for x in hm.split(":")]
    base = datetime.datetime(D.year, D.month, D.day, h, m)
    return base + datetime.timedelta(days=days)


def write_download(keytimes=None, events=None):
    data = {"version": 1, "generated_at": "2026-11-14T00:00:00",
            "start_date": D.isoformat(), "days": 8,
            "events": events or [], "periods": [], "all_calendars": ["工作", "空闲", "休息"],
            "mode": "profiles"}
    if keytimes is not None:
        data["keytimes"] = keytimes
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
check("默认日历是 通勤 / 休息", cfg["calendars"], ["通勤", "休息"])
check("默认播报节拍 60s", cfg["announce_sec"], 60)
check("默认音量 10", cfg["volume"], 10)
check("默认推送档位 timeSensitive", cfg["push"]["level"], "timeSensitive")

kt.save_config({"calendars": ["通勤"], "volume": 30, "push": {"sound": "minute"}})
cfg = kt.load_config()
check("save_config 后日历只剩通勤", cfg["calendars"], ["通勤"])
check("缺的键补默认（announce_sec）", cfg["announce_sec"], 60)
check("push 子字典合并而不是替换", cfg["push"]["sound"], "minute")
check("push 其他键还在", cfg["push"]["level"], "timeSensitive")
kt.save_config(kt.DEFAULT_CONFIG)

# ---------------------------------------------------------------- 解析

print("== parse_keytimes ==")
write_download(keytimes=[ev(D.isoformat(), "16:00", "17:00", "通勤", "回家"),
                         ev(D.isoformat(), "10:40", "11:00", "休息", "强制断电"),
                         ev((D + datetime.timedelta(days=1)).isoformat(),
                            "08:00", "08:30", "通勤", "去学校")])
kts, err = kt.load_keytimes()
check("读到 3 条", len(kts), 3)
check("按开始时间排序", [k.start for k in kts], ["10:40", "16:00", "08:00"])
check("kind：通勤 → commute", kts[0].kind, "rest")
check("kind：休息 → rest", kts[1].kind, "commute")
check("没有解析错误", err, "")
check("key 形如 日期T时刻", kts[0].key(), "%sT10:40" % D.isoformat())

# 老中间层没有 keytimes 时退回 events 里按日历名筛
write_download(events=[ev(D.isoformat(), "16:00", "17:00", "通勤", "回家"),
                       ev(D.isoformat(), "09:00", "12:00", "工作", "SAT 3H")])
kts2, _ = kt.load_keytimes()
check("老中间层按日历名筛出 1 条", [k.calendar for k in kts2], ["通勤"])

check("classify 认标题里的通勤", kt.classify("", "通勤（去学校）"), "commute")
check("classify 认 rest", kt.classify("Rest", ""), "rest")
check("classify 认不出来 → generic", kt.classify("提醒", "买菜"), "generic")
check("_hm 坏格式 → None", kt._hm("25:00"), None)


def kt_at(start, end="17:00", calendar="通勤", date=D.isoformat()):
    return kt.KeyTime(date, start, end, calendar)


# ---------------------------------------------------------------- 报警判定

print("== alarm_target ==")
kts = [kt_at("16:00", "17:00")]
check("15:59 还没到点：不报警", kt.alarm_target(dt("15:59"), kts, {}), None)
got = kt.alarm_target(dt("16:12"), kts, {})
check("16:12 报警", got.start if got else None, "16:00")
check("超时 12 分钟", got.overdue_minutes(dt("16:12")), 12)
check("确认后不再报警",
      kt.alarm_target(dt("16:12"), kts, {"acked": {"%sT16:00" % D.isoformat(): {}}}), None)
# lookback：默认 120 分钟，超过就不追账
late = dt("19:30")
check("超过 lookback 不再报警", kt.alarm_target(late, [kt_at("16:00", "17:00")], {}), None)
check("放宽 lookback 又能报",
      kt.alarm_target(late, [kt_at("16:00", "17:00")], {},
                      {"lookback_min": 300}).start, "16:00")
# 重叠时取最早开始的
two = [kt_at("16:00", "17:00", "通勤"), kt_at("16:05", "17:00", "休息")]
check("重叠取最早的", kt.alarm_target(dt("16:10"), two, {}).calendar, "通勤")
# 昨晚的 keytime：还没确认、又在 lookback 内 → 继续报（这是「延迟睡」的正确语义）；
# 过了 lookback 才不再追账。
yesterday = [kt.KeyTime("2026-11-13", "23:30", "23:59", "休息")]
check("隔夜但在 lookback 内仍然报",
      kt.alarm_target(dt("00:10"), yesterday, {}).start, "23:30")
check("隔夜又过 lookback → 不报", kt.alarm_target(dt("03:00"), yesterday, {}), None)

print("== upcoming / next ==")
kts = [kt_at("16:00"), kt.KeyTime("2026-11-15", "08:00", "08:30", "通勤")]
check("upcoming 只看还没开始的（含明天）",
      [k.start for k in kt.upcoming(dt("15:00"), kts)], ["16:00", "08:00"])
check("next 含已经开始的",
      kt.next_keytime(dt("16:12"), kts).start, "16:00")
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
check("第一场顺延到 16:42", sq["pushed"][0]["new_start"], dt("16:42"))
check("没有补课场次", sq["extra_sessions"], 0)

sq = kt.squeeze(dt("23:10"), kt_at("16:00", "17:00"), sessions, day_end="23:30")
check("深夜超时：全部被挤出", len(sq["dropped"]), 3)
check("被挤出 125 分钟", sq["extra_minutes"], 125)
check("补课 5 场（125/25）", sq["extra_sessions"], 5)

sq = kt.squeeze(dt("22:45"), kt_at("16:00", "17:00"), sessions[:1], day_end="23:30")
check("50 分钟场 + 405 分钟 delay → 挤出", len(sq["dropped"]), 1)

sq = kt.squeeze(dt("16:12"), kt_at("16:00", "17:00"), [], day_end="23:30")
check("没有 session 也能算（不崩）", sq["dropped"], [])

# 只是顺延、还没被挤出排期时，模板也要说清楚后果（别只剩标题）
target2 = kt_at("16:00", "17:00", "通勤")
sq2 = kt.squeeze(dt("16:12"), target2, sessions, day_end="23:30")
blob2 = "\n".join(text for _style, text in kt.render_lines(target2, dt("16:12"),
                                                            {"squeeze": sq2}))
check_in("顺延模板：整体后移", "整体后移 12 分钟", blob2)
check_in("顺延模板：晚收工", "晚 12 分钟收工", blob2)
check_in("顺延语音", "整体后移 12 分钟", kt.speech_text(target2, dt("16:12"),
                                                       {"squeeze": sq2}))
blob3 = "\n".join(text for _style, text in kt.render_lines(target2, dt("16:12"),
                                                            {"squeeze": {"dropped": [],
                                                                         "pushed": []}}))
check_in("没有后续排期也有话说", "从睡眠和明天里扣", blob3)

sq = kt.squeeze(dt("16:12"), kt_at("16:00", "17:00"),
                [{"start": "16:30", "duration": 50, "tasks": ["A"]}], day_end="23:30")
check("start 用字符串也认", sq["pushed"][0]["new_start"], dt("16:42"))

# ---------------------------------------------------------------- 文案

print("== 文案 ==")
target = kt_at("16:00", "17:00", "通勤")
sq = kt.squeeze(dt("23:10"), target, sessions, day_end="23:30")
lines = kt.render_lines(target, dt("23:10"), {"squeeze": sq})
blob = "\n".join(text for _style, text in lines)
check_in("标题带 KEY TIME", "KEY TIME 违约警报", blob)
check_in("已超时 430 分钟", "已超时：430 分钟", blob)
check_in("应于 16:00 启动", "应于 16:00 启动", blob)
check_in("点名被挤出的任务", "【[Run] 物理 2023 P1】", blob)
check_in("点名第二个任务", "【[Run] 历史 Essay 提纲】", blob)
check_in("算出要加班 5 场", "额外加班 5 个 Session", blob)
sp = kt.speech_text(target, dt("23:10"), {"squeeze": sq})
check_in("语音：超时分钟数", "通勤超时 430 分钟", sp)
check_in("语音：应于 16:00", "应于 16:00 出发", sp)
check_in("语音：加班场次", "5 个 session", sp)

rest = kt_at("10:40", "11:00", "休息")
lines = kt.render_lines(rest, dt("10:48"), {"goal": "在 EOS 历史中获取好成绩"})
blob = "\n".join(text for _style, text in lines)
check_in("休息模板：生理算力", "生理算力坍塌后果", blob)
check_in("休息模板：总目标", "在 EOS 历史中获取好成绩", blob)
check_in("休息模板：离开屏幕", "现在立刻离开屏幕", blob)
lines = kt.render_lines(rest, dt("10:48"), {})
blob = "\n".join(text for _style, text in lines)
check_in("没设总目标给占位提示", "keytime goal", blob)
check_in("休息语音", "休息超时 8 分钟", kt.speech_text(rest, dt("10:48"), {}))
acked_lines = kt.render_lines(target, dt("16:12"), {}, acked=True)
check_in("确认后显示已确认", "已确认", acked_lines[0][1] + acked_lines[-1][1])

# ---------------------------------------------------------------- 播报线程

print("== Announcer ==")
spoken, volumes = [], []


class FakeAnnouncer(kt.Announcer):
    pass


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
