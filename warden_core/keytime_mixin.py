"""warden_core.keytime_mixin —— Key Time 的看板展示、每分钟节拍与命令。

分工
----
``keytime.py`` 里是**纯逻辑**（模型 / 挤压预测 / 文案 / 播报 / Bark 推送），
``keytime_rules.py`` 管规则文件（哪些时间块报警、模板文字），这里只做三件事：
把结果接到画面上、按节拍驱动它、把命令翻译成状态改动。

"哪些时间块算 Key Time" 不再硬编码：``data/keytime-rules.toml`` 里按
「名字 > 类型（日历名）」匹配；用户改完文件 5 秒内自动生效。

节拍（用户定的）
----------------
    到点后               立刻：强制置顶 + 音量 10 + 语音播报 + 屏幕模板
    之后每 60 秒         置顶 + 播报（一句没播完就等它播完，见 Announcer）
    人在 Mac 前          不往手机推（避免「人就在电脑前，手机还一直响」）
    人不在 Mac 前        每分钟推一条 Bark（用户选了「堆叠」，不原地替换）
    确认（keytime ack）  停止音量/播报；置顶照旧每分钟一次
    确认时人不在 Mac     之后每 5 分钟再推一次，直到这个 key time 结束

为什么所有外部动作都从这里出发：warden 的每一帧都会调 :meth:`_keytime_tick`
（挂在 ``refresh_warnings`` 那条每帧钩子上），所以「到点」不需要另起 scheduler。
置顶和推送都扔到后台线程，绝不在画帧的线程里等 subprocess。
"""
import datetime
import os
import threading
import time

import sys

import keytime_rules as kr

from .common import (
    Group,
    Panel,
    Text,
    kt,
    repl_kit,
)

#: 每帧最多 1 秒驱动一次（tick=1.0 本来就是这个量级，多一层保护）。
TICK_MIN_SEC = 0.9


class KeyTimeMixin(object):
    # -- 初始化 ---------------------------------------------------------

    def _keytime_init(self):
        self.keytimes = []
        self.keytime_error = ""
        self.keytime_loaded_at = 0.0
        self.keytime_next_tick = 0.0
        self.keytime_next_announce = 0.0
        self.keytime_next_summon = 0.0
        self.keytime_next_push = 0.0
        self.keytime_last_key = ""
        self.keytime_acked_left_mac = False
        self.keytime_now = None            # 测试钉死「现在」
        self._keytime_announcer = None
        self._keytime_push_busy = False
        self._keytime_summon_busy = False
        self._keytime_state = kt.load_state()
        self.keytime_events = kt.load_config()
        #: 规则表：哪些时间块报警 + 模板文字（data/keytime-rules.toml）
        self.keytime_rules, self.keytime_rules_error = kt.load_rules()
        #: 报警只该在**真人坐在 TUI 前**时响：`--exec` / `--status` / 测试
        #: 都是非交互进程，抢焦点、拧音量、推手机全是副作用。测试可以手动
        #: 打开（``wd.keytime_tick_enabled = True``）。
        self.keytime_tick_enabled = bool(
            repl_kit.PROMPT_TOOLKIT and sys.stdin.isatty())
        # 立刻读一次：契约视图第一帧就要有那行淡红字（tick 只在交互模式下跑，
        # --status / --exec 永远等不到它）。
        self.keytime_reload(force=True)

    # -- 「现在」和读写 -------------------------------------------------

    def keytime_now_dt(self):
        return self.keytime_now or datetime.datetime.now()

    def keytime_reload(self, force=False):
        """每 5 秒最多读一次日历中间层 + 规则文件；force=True 立刻读。

        规则文件很小，5 秒读一次足够「改完存盘就生效」；中间层是同步产物。
        """
        now = time.time()
        if not force and now - self.keytime_loaded_at < 5:
            return self.keytimes
        self.keytime_loaded_at = now
        self.keytime_events = kt.load_config()
        self.keytime_rules, self.keytime_rules_error = kt.load_rules()
        self.keytimes, self.keytime_error = kt.load_keytimes(rules=self.keytime_rules)
        self._keytime_state = kt.load_state()
        self.invalidate()
        return self.keytimes

    def _keytime_save_state(self):
        try:
            kt.save_state(self._keytime_state)
        except OSError as e:
            self.say("keytime 状态没存下来：%s" % e, "warn")

    # -- 今天要报警的那个 key time + 挤压上下文 --------------------------

    def keytime_target(self, now=None):
        """现在**该报警**的那个（已经确认过的不算）。"""
        now = now or self.keytime_now_dt()
        return kt.alarm_target(now, self.keytimes, self._keytime_state,
                               self.keytime_events)

    def keytime_current(self, now=None):
        """现在正处在哪一个 key time 里（**不论确认与否**）。

        确认之后报警要停、但看板那块「已确认」的面板和「每分钟置顶」还得继续，
        所以这里和 :meth:`keytime_target` 分开：那个只回答「要不要喊」。
        """
        now = now or self.keytime_now_dt()
        hits = [item for item in self.keytimes
                if item.started(now) and item.end_dt() > now]
        hits.sort(key=lambda item: item.start_dt())
        return hits[-1] if hits else None

    def _keytime_todays_sessions(self, now):
        """今天剩下还没开始的场次（给挤压预测用）：[{start, duration, tasks}]。"""
        rows = []
        try:
            slots = self.plan_rows_for(now.date())
        except Exception:                          # noqa: BLE001 - 画看板不能崩
            return rows
        for slot in slots:
            start = datetime.datetime.combine(
                now.date(), datetime.time(slot.get("hour", 0), slot.get("minute", 0)))
            if start <= now:
                continue
            tasks = []
            try:
                tasks = [t.get("title") or "" for t in
                         self.task_store.tasks_for_session(now.date(), slot.get("start"))]
            except Exception:                      # noqa: BLE001
                tasks = []
            rows.append({"start": start, "duration": int(slot.get("duration") or 0),
                         "tasks": tasks, "label": slot.get("start")})
        rows.sort(key=lambda r: r["start"])
        return rows

    def _keytime_chore_line(self):
        """key time 屏下面那行灰色小字：现在还能顺手做掉哪件杂事。"""
        title = self._keytime_chore_title()
        return ("或者，如果你想做这件杂事：%s。" % title) if title else ""

    def _keytime_chore_title(self):
        try:
            project, _error = self.chore_binding()
            if not project:
                return ""
            tasks = self.chore_project_tasks(project)
            if not tasks:
                return ""
            index = int(getattr(self, "chore_index", 0) or 0) % len(tasks)
            return tasks[index].get("title") or tasks[index].get("pk") or ""
        except Exception:                          # noqa: BLE001 - 只是行提示
            return ""

    def keytime_ctx(self, target, now=None):
        now = now or self.keytime_now_dt()
        sessions = self._keytime_todays_sessions(now)
        return {
            "squeeze": kt.squeeze(now, target, sessions,
                                  self.keytime_events.get("day_end")),
            # 模板里的 {next_task} / {next_tasks} / {next_time}
            "sessions": sessions,
            "goal": self.keytime_goal_for(target, now),
            "chore": self._keytime_chore_title(),
            "acked_at": None,
        }

    def keytime_goal_for(self, target, now=None):
        """这个 key time 之后要做的那件事的「总目标」。还没有 → 空串。

        现在只实现占位（P4 再把它接到 OmniFocus 的 note 上）：报警模板里
        会明确说「用 keytime goal <文本> 设置」，而不是假装有。
        """
        return ""

    # -- 每帧节拍 -------------------------------------------------------

    def _keytime_tick(self):
        """每帧调一次（自己节流 1 秒）。驱动置顶 / 播报 / 推送。"""
        if not self.keytime_tick_enabled:
            return
        now = self.keytime_now_dt()
        if time.time() < self.keytime_next_tick:
            return
        self.keytime_next_tick = time.time() + TICK_MIN_SEC
        if not self.keytime_events.get("enabled", True):
            self._keytime_stop_announcer()
            return
        if not self.keytimes or time.time() - self.keytime_loaded_at > 30:
            self.keytime_reload()
        target = self.keytime_target(now) or self.keytime_current(now)
        if target is None:
            self.keytime_last_key = ""
            self._keytime_stop_announcer()
            return
        if target.key() != self.keytime_last_key:
            # 换了一个 key time：节拍从头开始，第一秒就要喊。
            self.keytime_last_key = target.key()
            self.keytime_next_announce = 0.0
            self.keytime_next_summon = 0.0
            self.keytime_next_push = 0.0
            self.keytime_acked_left_mac = bool(
                (self._keytime_state.get("acked") or {})
                .get(target.key(), {}).get("left_mac"))
            self.invalidate()
        acked = kt.acked(target, self._keytime_state)
        announce_sec = int(self.keytime_events.get("announce_sec") or 60)
        mono = time.time()
        if not acked:
            if mono >= self.keytime_next_announce:
                self.keytime_next_announce = mono + announce_sec
                self._keytime_announce(target, now)
            return
        # 已确认：不再播报，但置顶照旧每分钟一次；人一开始就不在 Mac 前的话，
        # 每 5 分钟再推一次手机（用户明确要的「别以为确认了就没事了」）。
        if mono >= self.keytime_next_summon and target.end_dt() > now:
            self.keytime_next_summon = mono + announce_sec
            self._keytime_summon()
        if (self.keytime_acked_left_mac and mono >= self.keytime_next_push
                and target.end_dt() > now):
            self.keytime_next_push = mono + 300
            self._keytime_push(target, now, mode="acked")

    def _keytime_announce(self, target, now):
        """一次完整播报：置顶 + 语音 + （人不在时）推送。"""
        self._keytime_summon()
        ctx = self.keytime_ctx(target, now)
        text = kt.speech_text(target, now, ctx, self.keytime_events)
        self._keytime_speaker().speak(text)
        self.invalidate()
        if kt.left_mac(config=self.keytime_events):
            self.keytime_acked_left_mac = True
            self._keytime_push(target, now, mode="alarm")

    def _keytime_summon(self):
        if not self.keytime_events.get("summon", True):
            return
        if self._keytime_summon_busy:
            return
        self._keytime_summon_busy = True

        def run():
            try:
                kt.summon()
            finally:
                self._keytime_summon_busy = False

        threading.Thread(target=run, name="keytime-summon", daemon=True).start()

    def _keytime_push(self, target, now, mode="alarm"):
        """Bark 推送（后台线程，失败只记一句，不重试 —— Bark 会封 IP）。"""
        if self._keytime_push_busy:
            return
        devices = kt.load_devices()
        if not devices:
            return
        self._keytime_push_busy = True
        title = kt.headline(target, now, config=self.keytime_events)
        body = kt.plain_text(kt.render_lines(target, now,
                                             self.keytime_ctx(target, now),
                                             acked=(mode == "acked"),
                                             config=self.keytime_events))
        params = kt.push_params(self.keytime_events, mode=mode, seq=target.key())
        timeout = (self.keytime_events.get("push") or {}).get("timeout")

        def run():
            try:
                results = kt.push_all(devices, title, body, params,
                                      timeout=timeout) or []
                bad = [r for r in results if not r[1]]
                if bad:
                    self.say("keytime 推送失败：%s" % "；".join(
                        "%s %s" % (name, detail) for name, _ok, detail in bad), "warn")
            finally:
                self._keytime_push_busy = False

        threading.Thread(target=run, name="keytime-push", daemon=True).start()

    def _keytime_speaker(self):
        if self._keytime_announcer is None:
            self._keytime_announcer = kt.Announcer(
                self.keytime_events, log=lambda msg: self.say("[keytime] " + msg, "warn"))
        else:
            self._keytime_announcer.config = self.keytime_events
        return self._keytime_announcer

    def _keytime_stop_announcer(self):
        if self._keytime_announcer is not None:
            try:
                self._keytime_announcer.stop()
            except Exception:                      # noqa: BLE001 - 停不下来也别崩
                pass
            self._keytime_announcer = None

    # -- 画面 -----------------------------------------------------------

    def render_keytime_takeover(self):
        """违约中：**整页**只画这块暂停牌（用户要求：强制暂停不该混在看板里）。

        返回 None = 没在违约，正常看板照常画。``keytime ack`` 之后（或这一段
        结束）自动恢复正常看板 —— 确认只是停播报，不解除「每分钟置顶」。

        只在**真交互式 TUI**里整页接管（和置顶/播报同一个开关）：
        ``--status`` / ``--exec`` / 测试进程不该被一块暂停牌顶掉整页。
        """
        if not self.keytime_tick_enabled:
            return None
        target = self.keytime_target()
        if target is None:
            return None
        panel = self.render_keytime_panel()
        if panel is None:                      # target 一定是 current，兜底而已
            return None
        return Group(panel,
                     Text("keytime ack 确认（停止播报，置顶继续）· 这一段结束后"
                          "自动回到看板 · 现在整页只留这块暂停牌", style="dim"))

    def render_keytime_panel(self):
        """正在违约的 key time：整块模板（规则文字 + 挤压预测）。"""
        target = self.keytime_target() or self.keytime_current()
        if target is None:
            return None
        now = self.keytime_now_dt()
        acked = kt.acked(target, self._keytime_state)
        ctx = self.keytime_ctx(target, now)
        if acked:
            ctx["acked_at"] = (self._keytime_state.get("acked") or {}).get(
                target.key(), {}).get("at")
        lines = kt.render_lines(target, now, ctx, acked=acked,
                                config=self.keytime_events)
        body = Text()
        for index, segments in enumerate(lines):
            if index:
                body.append("\n")
            for style, text in segments:
                body.append(text, style=style)
        chore = self._keytime_chore_line()
        if chore and not acked:
            body.append("\n")
            body.append(chore, style="grey50")
        return Panel(body, title="KEY TIME · %s" % (target.kind or "keytime"),
                     border_style="red", expand=True)

    def render_keytime_line(self):
        """契约视图顶部那一行淡红字：下一个 key time 还有多久。

        整行文字是模板（规则文件 ``[display] line``），变量见 ``keytime_rules``。
        """
        if self.keytime_target() is not None:
            return None                            # 已经在报警了，面板会接管
        now = self.keytime_now_dt()
        nxt = kt.next_keytime(now, self.keytimes)
        if nxt is None:
            return None
        if int((nxt.start_dt() - now).total_seconds() // 60) < 0:
            return None
        return Text(kt.display_line(nxt, now, self.keytime_rules, self.keytime_events),
                    style="bold #ff8a80")

    def keytime_upcoming_rows(self, now=None, limit=40):
        now = now or self.keytime_now_dt()
        rows = []
        for item in self.keytimes:
            if item.end_dt() <= now and item.key() not in self._keytime_state["acked"]:
                continue
            rows.append(item)
        return rows[:limit]

    # -- 命令 -----------------------------------------------------------

    def cmd_keytime(self, args=None):
        """``keytime`` 命令族：看 / 确认 / 设备 / 规则 / 开关 / 刷新。

            keytime                      今天与之后的 key time + 状态
            keytime ack                  确认（停止播报，置顶继续）
            keytime refresh              立刻重读日历中间层 + 规则文件
            keytime rules                列出规则（哪些时间块报警、模板在哪）
            keytime rules reset          规则文件恢复出厂内容（会覆盖你的修改）
            keytime on|off               开/关整条报警链路
            keytime device               列出 Bark 设备
            keytime device add <名字> <key> [--server URL]
            keytime device remove <名字>
            keytime device test [<名字>]
            keytime set <键> <值>        改配置（volume / announce_sec / day_end …）

        「哪些时间块算 Key Time」由 ``data/keytime-rules.toml`` 决定，用户直接
        改那个文件（自带注释和变量表），不是用命令配。这里只给看/重置/刷新。
        """
        toks = [str(a).strip() for a in (args or []) if str(a).strip()]
        cfg = self.keytime_events = kt.load_config()
        if not toks:
            return self._keytime_show()
        head = toks[0].lower()
        if head in ("ack", "确认", "ok", "确定"):
            return self.keytime_ack()
        if head in ("refresh", "reload", "刷新"):
            self.keytime_reload(force=True)
            return self._keytime_show()
        if head in ("on", "off"):
            cfg["enabled"] = head == "on"
            kt.save_config(cfg)
            self.keytime_events = kt.load_config()
            if head == "off":
                self._keytime_stop_announcer()
            self.say("keytime 报警已%s" % ("打开" if head == "on" else "关闭（不再置顶/播报/推送）"),
                     "cmd")
            self.invalidate()
            return cfg["enabled"]
        if head in ("device", "devices", "设备"):
            return self._keytime_device(toks[1:])
        if head in ("rules", "规则"):
            return self._keytime_rules(toks[1:])
        if head in ("set", "设置"):
            return self._keytime_set(toks[1:])
        raise ValueError("keytime 只认 ack / refresh / rules / on / off / device / set；"
                         "你给的是 %r" % toks[0])

    def _keytime_rules(self, toks):
        """看规则文件 / 恢复出厂。规则本身在 data/keytime-rules.toml 里改。"""
        sub = (toks[0].lower() if toks else "")
        path = kr.rules_path()
        if sub in ("reset", "重置", "恢复"):
            kr.save_default_rules()
            self.keytime_reload(force=True)
            self.say("已把 %s 恢复成出厂内容（你之前的模板被覆盖了）" % path, "cmd")
            return path
        self.keytime_reload(force=True)
        rules = self.keytime_rules.rules if self.keytime_rules else []
        self.say("Key Time 规则文件：%s（%d 条规则；直接改它，存盘后 5 秒内生效）"
                 % (path, len(rules)), "cmd")
        for rule in rules:
            self.say("  - %s" % rule.describe(), "cmd")
        if self.keytime_rules_error:
            self.say("⚠ 规则文件有问题：%s" % self.keytime_rules_error, "warn")
        self.say("还没到的 key time 那一行文字在 [display] line；模板变量见文件顶部注释。",
                 "cmd")
        return rules

    def _keytime_show(self):
        now = self.keytime_now_dt()
        self.keytime_reload(force=True)
        if self.keytime_error:
            self.say(self.keytime_error, "warn")
        if self.keytime_rules_error:
            self.say("⚠ 规则文件：%s（keytime rules 看详情）" % self.keytime_rules_error,
                     "warn")
        if not self.keytimes:
            self.say("没有匹配规则的 key time（规则 %d 条：%s）。"
                     "时间块来自日历中间层 —— 先 sync / download_calendar.py download；"
                     "哪些块报警看 data/keytime-rules.toml"
                     % (len((self.keytime_rules or kr.RuleSet()).rules),
                        kr.rules_path()), "cmd")
            return []
        lines = []
        target = self.keytime_target(now)
        for item in self.keytime_upcoming_rows(now):
            acked = kt.acked(item, self._keytime_state)
            if item is target and not acked:
                mark = "🚨 违约中"
            elif acked:
                mark = "✓ 已确认"
            elif item.started(now):
                mark = "· 进行中"
            else:
                mark = "· 未来"
            lines.append("%s %s-%s %s [%s] %s%s"
                         % (item.date[5:], item.start, item.end, item.calendar,
                            item.kind, mark,
                            ("  " + item.title) if item.title and item.title != item.calendar
                            else ""))
        if lines:
            self.say("Key Time（规则 %d 条）：%s"
                     % (len((self.keytime_rules or kr.RuleSet()).rules),
                        "；".join(lines[:6])), "cmd")
        else:
            self.say("没有接下来的 key time", "cmd")
        if len(lines) > 6:
            self.say("（还有 %d 条，`open keytime` 在数据视图里看全表）" % (len(lines) - 6),
                     "cmd")
        return lines

    def keytime_ack(self, now=None):
        now = now or self.keytime_now_dt()
        target = self.keytime_target(now)
        if target is None:
            self.say("现在没有在违约的 key time，不用确认。", "cmd")
            return False
        key = target.key()
        self._keytime_state.setdefault("acked", {})[key] = {
            "at": now.isoformat(timespec="minutes"),
            "left_mac": bool(self.keytime_acked_left_mac),
        }
        self._keytime_save_state()
        self._keytime_stop_announcer()
        self.say("✓ 已确认 key time %s（%s）—— 停止播报与音量，置顶继续每分钟一次"
                 % (key, target.calendar), "cmd")
        self.keytime_next_summon = 0.0
        self.keytime_next_push = 0.0
        self.invalidate()
        return True

    def _keytime_device(self, toks):
        devices = kt.load_devices()
        if not toks:
            if not devices:
                self.say("还没有 Bark 设备：先在 iPhone 装 Bark，复制它首页那条 URL 里的 key，"
                         "然后 keytime device add iphone <key>", "cmd")
                return []
            for d in devices:
                self.say("- %s  key=%s…  %s  %s"
                         % (d["name"], d["key"][:6], d["server"],
                            "启用" if d["enabled"] else "已停用"), "cmd")
            return devices
        sub = toks[0].lower()
        if sub in ("test", "测试"):
            targets = [d for d in devices
                       if d["enabled"] and (len(toks) < 2 or d["name"] == toks[1])]
            if not targets:
                raise ValueError("没有可测试的设备；先 keytime device add <名字> <key>")
            results = kt.push_all(targets, "[KEY TIME 测试]",
                                  "如果你看到这条，说明 Bark 链路是通的。",
                                  kt.push_params(self.keytime_events))
            for name, ok, detail in results:
                self.say("%s %s：%s" % ("✓" if ok else "✗", name, detail),
                         "info" if ok else "error")
            return results
        if sub in ("add", "添加"):
            if len(toks) < 3:
                raise ValueError("用法：keytime device add <名字> <key> [--server URL]")
            name, key = toks[1], toks[2]
            server = None
            if "--server" in toks:
                index = toks.index("--server")
                if index + 1 < len(toks):
                    server = toks[index + 1]
            elif any(t.startswith("http") for t in toks[3:]):
                server = [t for t in toks[3:] if t.startswith("http")][0]
            devices = [d for d in devices if d["name"] != name]
            devices.append({"name": name, "key": key,
                            "server": server or "https://api.day.app",
                            "level": "", "group": "", "sound": "", "enabled": True})
            kt.save_devices(devices)
            self.say("✓ 已添加 Bark 设备 %s（key %s…）。用 `keytime device test %s` 验一条。"
                     % (name, key[:6], name), "cmd")
            return devices
        if sub in ("remove", "rm", "删除"):
            if len(toks) < 2:
                raise ValueError("用法：keytime device remove <名字>")
            left = [d for d in devices if d["name"] != toks[1]]
            kt.save_devices(left)
            self.say("已删除设备 %s（还剩 %d 台）" % (toks[1], len(left)), "cmd")
            return left
        if sub in ("on", "off"):
            if len(toks) < 3:
                raise ValueError("用法：keytime device on|off <名字>")
            found = False
            for d in devices:
                if d["name"] == toks[2]:
                    d["enabled"] = sub == "on"
                    found = True
            if not found:
                raise ValueError("没有名叫 %r 的设备" % toks[2])
            kt.save_devices(devices)
            self.say("设备 %s 已%s" % (toks[2], "启用" if sub == "on" else "停用"), "cmd")
            return devices
        raise ValueError("keytime device 只认 list / add / remove / test / on / off")

    def _keytime_set(self, toks):
        if len(toks) < 2:
            raise ValueError("用法：keytime set <键> <值>（volume / announce_sec / "
                             "day_end / leave_idle_sec / summon / restore_volume / "
                             "push.level）")
        key, raw = toks[0], " ".join(toks[1:])
        cfg = kt.load_config()
        if key in ("volume", "announce_sec", "lookback_min", "leave_idle_sec"):
            cfg[key] = int(raw)
        elif key == "day_end":
            if kt._hm(raw) is None:
                raise ValueError("day_end 要 HH:MM，收到 %r" % raw)
            cfg[key] = raw
        elif key in ("summon", "enabled", "restore_volume"):
            cfg[key] = raw.lower() in ("1", "on", "true", "yes", "开")
        elif key.startswith("push."):
            cfg.setdefault("push", {})[key.split(".", 1)[1]] = raw
        elif key == "calendars":
            raise ValueError("calendars 已经不用了：哪些时间块报警由 data/keytime-rules.toml "
                             "的规则决定（keytime rules 看当前规则）")
        else:
            raise ValueError("不认识的配置项 %r" % key)
        kt.save_config(cfg)
        self.keytime_events = kt.load_config()
        self.keytime_reload(force=True)
        self.say("keytime 配置 %s = %s" % (key, cfg.get(key, (cfg.get("push") or {}).get(
            key.split(".", 1)[1] if "." in key else ""))), "cmd")
        self.invalidate()
        return cfg
