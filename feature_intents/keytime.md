# keytime —— 日历里的硬节点（通勤 / 休息）：到点强制置顶 + 播报 + 手机推送

`keytime` 管的是**不是 session 的时间块**。日历里「通勤」「休息」两个分组的每一个
事件都是一个 **Key Time**：它不排 session、不订场、不进 `data.csv`，但到点必须做
（回家 / 断电休息），晚一分钟都是从后面的时间盒里抢。

```
日历（CalDAV）──download_calendar.py──▶ temp/calendar-download.json["keytimes"]
                                              │
                                    keytime.load_keytimes()
                                              │
            ┌─────────────────────────────────┼──────────────────────────────┐
            ▼                                 ▼                              ▼
   warden 看板（模板 / 淡红一行）      音量 10 + say 播报              Bark → iPhone
```

## 为什么

用户原话：「这个功能的存在是为了提醒我在某个时候做某件事」——重点不是「知道」，
是**必须立刻动**。所以它不是一条通知，而是三层同时压上来：

1. **屏幕**：warden 每帧把 iTerm 拉到最前（不是发个通知，是抢焦点）；
2. **耳朵**：系统音量拧到 10，中文语音播报「已超时 N 分钟 + 后果」；
3. **手机**：人不在 Mac 前时，每分钟 Bark 推送一条（堆叠，不覆盖）。

确认（`keytime ack`）之后：停止音量与语音，但**置顶照旧每分钟一次** —— 用户明确
要求「确认 ≠ 可以继续赖着」。

## 语义（一份合同）

| 时刻 | 屏幕 | 声音 | 手机 |
|---|---|---|---|
| 到点前 | 契约视图顶部一行淡红字：`16:00 通勤 · 还有 23 分钟` | — | — |
| 到点（`start` 起） | 看板顶部整块红色模板（通勤 / 休息两套文案） | 立刻 + 之后每 60s | 人不在 Mac 前才推，每分钟一条（`stack_notifications`） |
| 确认后 | 模板变成「✓ 已确认」 | 停 | 一开始就离开过 Mac 的话，每 5 分钟再推一条，直到这一段结束 |
| 过了 `end` | 模板消失，回到一行提示下一个 | — | — |

- **超时口径**：`overdue = now - start`，**不是**「到点 5 分钟才提醒」。模板里写
  「应于 16:00 启动，现已 16:12」，所以 16:00 一到就是「已超时 0 分钟」。
- **人在不在 Mac 前**：`keep_session.hid_idle_seconds()`（Quartz，~0.2ms），超过
  `leave_idle_sec`（默认 120s）算离开。读不到就**当人在**（绝不因为读不到而轰炸手机）。
- **不再追账**：`lookback_min`（默认 120）之外的 key time 不报警。半夜醒来不会
  看到八小时前那条通勤还在喊。
- **重叠**（通勤和休息同时命中）：取**最早开始**的那个报警。

## 挤压预测（通勤模板里那两段「后果」）

模型故意简单、和用户心智一致：**拖延 1 分钟 = 后面每一场整体后移 1 分钟**。

```
delay = 已超时分钟数
对今天剩下还没开始的每一场：new_end = start + delay + duration
    new_end > day_end（默认 23:30）→ 这一场被挤出今日排期
extra_sessions = ceil(被挤出分钟数 / 25)   # 今晚要补几场
```

模板会点名**最早被挤出的两场**里的任务（`【[Run] 物理 2023 P1】`），并报出补课场次。
不试图「帮你重排」—— 真到那一步再 plan，那是另一条命令的事。

## 模板（两种类型）

类型由日历名/标题判定（`classify`）：含「通勤/路上」→ `commute`，含「休息/睡觉/
断电」→ `rest`，其余 → `generic`。

- **commute**：`🚨 [KEY TIME 违约警报]` / 动作 / 已超时 / `💥 物理排期坍塌后果` /
  被挤出的任务 / `你今晚将被迫额外加班 N 个 Session (25m) 擦屁股！` /
  `🔴 计时器正在跳动：每多看屏幕 1 秒，今晚就晚睡 1 秒！`
- **rest**：`🧠 生理算力坍塌后果` / 拒绝清理大脑 L1 缓存 / `你的 Project 总目标：…将
  因此失败！` / `🛑 别拿垃圾时间骗自己：现在立刻离开屏幕 N 分钟。`
- **总目标**：计划存在 **OmniFocus 任务的 note** 里（用户指定）。现在**还没有接**
  （见下面「未完成」）：模板会明确写 `用 keytime goal <文本> 设置`，不假装有。

## 命令

```
keytime                             今天与之后的 key time（状态：未来/进行中/违约中/已确认）
keytime ack                         确认（停止播报与音量；置顶继续）
keytime refresh                     立刻重读日历中间层
keytime on|off                      整条报警链路开关（写 temp/keytime.json 的 enabled）
open keytime                        数据视图第四种模型：全表 + 已超时分钟数
keytime device                      列出 Bark 设备
keytime device add iphone <key> [--server URL]
keytime device remove iphone | on|off iphone
keytime device test [iphone]        发一条测试推送（验证 key / 授权 / 网络）
keytime set <键> <值>               volume / announce_sec / calendars / day_end /
                                    leave_idle_sec / lookback_min / summon /
                                    restore_volume / push.level / push.sound
```

`keytime` 在**所有视图**都可用（挂在 `View.common_commands`）——报警随时可能响，
不管当时在契约、专注还是琐事屏，`keytime ack` 都要能敲。

## Bark（手机推送）

- iPhone 装 [Bark](https://github.com/Finb/Bark)，复制它首页那条 URL 里的 key；
  **一个 key 只能一台设备**，多设备就多建几条。
- Mac 端只发 HTTP：`POST {server}/push`，JSON body 带 `device_key`（避免把中文/
  括号塞进 URL 路径导致的 404/405）。默认服务器 `https://api.day.app`。
- 参数：`level=timeSensitive`（专注模式也能弹；`critical` 要 App 内授权「重要警告」）、
  `group=keytime`（可整组静音）、`sound=alarm`、`ttl=3600`、`isArchive=1`。
  `stack_notifications=true`（用户选的）→ 每分钟一条新通知；false → 用同一个
  `id` 原地替换成「已超时 N 分钟」。
- **失败不重试**：Bark 公共服务器的封 IP 规则是「5 分钟内 >5 次 405 或 >1000 次错误
  请求 → 封 24 小时」。`push_one` 只打一枪，失败进 Recent 的警告，等下一分钟。
- **代理坑**：macOS 的「系统设置 → 代理」会被 `urllib` 自动读进来（curl 不会），
  它的例外列表里通常只有 `localhost`、没有 `127.0.0.1`。所以 `_urlopen` 对回环地址
  **强制绕过代理**；真机推送仍走系统代理（用户可能就靠它出网）。代理没开时的报错会
  直接提示「curl 能通、python 不通就是这个原因」。

## 文件

```
temp/keytime.json           配置（日历名 / 节拍 / 音量 / 推送档位 / enabled）
temp/keytime-state.json     确认记录：{"acked": {"2026-11-14T16:00": {"at", "left_mac"}}}
data/keytime-devices.json   Bark 设备（含 key，**不进 git**）
temp/calendar-download.json keytimes 由 download_calendar.py 写入
```

全部可用环境变量指到临时目录再跑（`TI_KEYTIME_JSON` / `TI_KEYTIME_STATE` /
`TI_KEYTIME_DEVICES` / `TI_CALENDAR_DOWNLOAD`），测试就是这么做的。

## 日历侧（download_calendar.py）

- `keytime_calendars`（默认 `["通勤", "休息"]`，可写在 `temp/calendar.json` 或
  `--keytime-calendars`）里的日历，**查询时也会拉**（`fetch_caldav` 只查传进去的
  几个日历，不并进去永远拿不到「休息」的事件）。
- 它们仍然**不排 session**：`select_profile_events` 只认 工作/空闲；`DEFAULT_IGNORE`
  里本来就有「休息」。
- 提取在 `keytime_rows()`：跳过全天事件；跨午夜的块按天切成两段（前一段收在 23:59）。

## 节拍挂在哪

`WardenShell.frame_key_hook`（`Warden._frame_key`）—— repl_kit 里**唯一**保证
「每秒一定被调一次」的地方（`tick=1.0` 会 invalidate）。不挂在某个视图的
`status_box` 上：那样切到专注/统计视图就不响了。节拍自己再节流 1 秒。

置顶（`keep_session.summon_warden`，收尾提醒同一条路）和推送都在**后台线程**里跑：
画帧线程等 subprocess 会把整个 TUI 卡住。

## 未完成 / 已知

- **`keytime goal`（总目标）还没接 OmniFocus note**：现在模板给占位文案。接的时候
  要新加一条只读查询（项目 note）+ 写回命令，并且必须走后台队列（osascript 0.3s 起）。
- **语音队列语义**：说「等之前的放完再继续」；实现是「正在播的一定播完，排队中的
  只保留最新一句」（60s 节拍下几乎不会积压）。
- **`day_end` 默认 23:30**，挤压预测用它算「挤出今日排期」；改 `keytime set day_end`。
- 确认后每 5 分钟推送只在「确认那一刻人在 Mac 外」时生效（用户要求）。

## 回归

```
python3 test/test_keytime.py            # 模型 / 挤压 / 文案 / 播报 / Bark 负载（60+ 断言）
python3 test/test_keytime_warden.py     # 看板模板 / 每帧节拍 / ack / 数据模型 / 命令
python3 test/test_calendar_download_layer.py   # keytime_rows 提取（含跨午夜）
```
