# keytime —— 日历里的硬节点：到点锁死桌面 + 整页接管 + 播报 + 横幅 + 手机推送

`keytime` 管的是**不是 session 的时间块**。日历里任意一个时间块是不是 Key Time，
**不再由「日历叫通勤/休息」这种硬编码决定**：`download_calendar.py` 把**所有日历**
的非全天事件都记进中间层，`data/keytime-rules.toml` 里的规则按
「时间块名字 > 时间块类型（日历名）」动态匹配。命中规则的块就是硬节点：它不排
session、不订场、不进 `data.csv`，但到点必须做（回家 / 断电休息 / 出门运动），
晚一分钟都是从后面的时间盒里抢。

```
日历（CalDAV，全部日历）──download_calendar.py──▶ temp/calendar-download.json["time_blocks"]
                                                            │
                                              keytime_rules.match()
                                                            │
                                                  keytime.load_keytimes()
                                                            │
        ┌───────────────┬───────────────┬───────────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼                   ▼               ▼              ▼
 warden 整页接管   置顶 iTerm    Focus.app 锁死桌面    音量 10 + say    mac 横幅    Bark → iPhone
（规则模板）                 （除 iTerm 全杀）
```

## 为什么

用户原话：「这个功能的存在是为了提醒我在某个时候做某件事」——重点不是「知道」，
是**必须立刻动**。所以它不是一条通知，而是五层同时压上来：

1. **桌面**：所有 App 被 Focus.app 收走，只留 iTerm（用户原话：「使用 focus 2
   锁死所有，仅留 iterm」「同时浮现，置顶 iterm 无法使用」）；
2. **屏幕**：warden 违约中**整页只画这块暂停牌**（不是混在契约界面里的一张卡片；
   用户原话：「强制暂停系统难道会混到一大堆信息里面？」）；
3. **耳朵**：系统音量拧到 10，中文语音播报「已超时 N 分钟 + 后果」；
4. **横幅**：mac 通知中心每分钟一条（带声音，堆叠不覆盖；用户原话「在 mac 上发通知」）；
5. **手机**：人不在 Mac 前时，每分钟 Bark 推送一条（堆叠，不覆盖）。

确认（`keytime ack`）之后：停止音量/语音/横幅，**整页接管解除**、回到正常看板，但
**置顶与桌面锁照旧每分钟一次** —— 用户明确要求「确认 ≠ 可以继续赖着」。

## 语义（一份合同）

| 时刻 | 桌面 | 屏幕 | 声音 | 手机 |
|---|---|---|---|---|
| 到点前 | — | 契约视图顶部一行淡红字：`🔴 KEY TIME 11-15 运动 · 23 分钟后 · 16:00-17:00` | — | — |
| 到点（`start ≤ now < end`） | **Focus.app 切到 `keytime` profile：除 iTerm 外的 App 被收走** | **整页**只画规则模板（标题红底白字 + 后果 + 降级契约） | 立刻 + 之后每 60s | 人不在 Mac 前才推，每分钟一条（`stack_notifications`） |
| 确认后 | 照旧锁着，直到这块结束 | 回到正常看板；契约视图顶部留一块「✓ 已确认」 | 停（横幅也停） | 一开始就离开过 Mac 的话，每 5 分钟再推一条，直到这一段结束 |
| `now ≥ end` | 解锁（只解我们自己上的锁） | 全部消失，回到一行提示下一个 | — | — |

- **超时口径**：`overdue = now - start`，**不是**「到点 5 分钟才提醒」。模板里写
  「应于 16:00 启动，现已 16:12」，所以 16:00 一到就是「已超时 0 分钟」。
- **过了 `end` 立刻停**：用户实测「休息 19:30 就结束了，20:03 还在播『休息超时
  126 分钟』」—— 旧实现是 `end + lookback(120min)` 才停，等于块结束后还要轰炸
  两小时。现在报警窗口就是 `[start, end)`，`lookback_min` 已废弃。
- **人在不在 Mac 前**：`keep_session.hid_idle_seconds()`（Quartz，~0.2ms），超过
  `leave_idle_sec`（默认 120s）算离开。读不到就**当人在**（绝不因为读不到而轰炸手机）。
- **重叠**（两个块同时命中）：取**最早开始**的那个报警。
- **整页接管只在真交互式 TUI**（`keytime_tick_enabled`，和置顶/播报同一个开关）：
  `--status` / `--exec` / 测试进程不会被一块暂停牌顶掉整页。

## 规则表（`data/keytime-rules.toml`）

第一次运行自动生成，自带完整注释和变量表。改完存盘，warden 5 秒内生效
（`keytime refresh` 立刻生效；`keytime rules` 看解析结果）。

```
[display]
line = "🔴 KEY TIME {date_short} {block} · {when} · {start}-{end}"

[[rule]]
id = "exercise"
name = "运动"              # 匹配时间块名字（事件标题），优先级最高
calendar = "运动"          # 也匹配类型（日历名），name 命中时不会看它
headline = "..."           # 手机推送标题（可省 → 模板第一行）
text = """
...
"""
speech = "..."             # 语音稿（可省 → 模板第一行）
```

匹配规则：

- `name` 匹配**时间块名字**（事件标题；没标题时用日历名当名字），优先级最高；
- `calendar` 匹配**时间块类型**（日历名）；只要有一条按 name 命中，就轮不到它；
- 大小写不敏感「包含」；pattern 带 `*` / `?` 时按通配符（整串匹配）；
- 多条同级命中：**文件里写在前面的赢**；
- `name` / `calendar` 都不写 = 兜底规则（默认 `enabled = false`，打开会让所有
  没命中的块都报警）；
- 都不命中 → **不是 Key Time**，不报警（工作 / 空闲 / 上课 默认不报）。

模板变量（完整表在规则文件顶部注释里）：

| 变量 | 含义 |
|---|---|
| `{block}` `{calendar}` `{kind}` `{date}` `{date_short}` | 时间块名字 / 日历名 / 规则 id / 日期 |
| `{key_time}` `{key_end}` `{now_time}` `{overdue_mins}` `{duration_mins}` `{remain_mins}` | 计划开始/结束、现在、已超时、块长、剩余 |
| `{next_task}` `{next_tasks}` `{next_time}` `{next_in_mins}` | 下一个 session 的任务 / 开始时刻 / 还有多久 |
| `{delay}` `{pushed_count}` `{dropped_count}` `{dropped_mins}` `{dropped_tasks}` `{extra_sessions}` `{extra_minutes}` | 挤压预测的细粒度数字 |
| `{squeeze_text}` `{squeeze_speech}` `{extra_text}` | 一整句后果（自动换句）/ 语音短句 / 加班句（没挤出时为空，整行消失） |
| `{goal}` `{goal_line}` `{goal_speech}` | Project 总目标（还没接，通常为空） |
| `{chore}` `{acked_at}` `{acked_line}` | 顺手杂事 / 确认时刻 / 确认那行绿字 |

排版：模板第一行固定「红底白字」；行首前缀 `!` 红字加粗、`-` 品红加粗（段落标题）、
`>` 黄字、`#` 灰字（前缀不显示）；行内 `**重点**` 会渲染成加粗。某行渲染成空串
（比如 `{extra_text}` 为空）→ 整行自动删掉。

默认四套规则：`exercise`（运动，用户给的那套「肉体僵死警报 / 最低降级契约」）、
`commute`（通勤，挤压后续 session 并点名被挤出的任务）、`rest`（休息，生理算力
坍塌 + 总目标占位）、`generic`（兜底，默认停用）。

## 挤压预测（commute / 模板里那两段「后果」）

模型故意简单、和用户心智一致：**拖延 1 分钟 = 后面每一场整体后移 1 分钟**。

```
delay = 已超时分钟数
对今天剩下还没开始的每一场：new_end = start + delay + duration
    new_end > day_end（默认 23:30）→ 这一场被挤出今日排期
extra_sessions = ceil(被挤出分钟数 / 25)   # 今晚要补几场
```

`{squeeze_text}` 会在「点名被挤出的任务 / 整体后移 N 分钟 / 没有后续排期」之间
自动换句；`{extra_text}` 只在真有场次被挤出时出现。不试图帮你重排 —— 真到那一步
再 plan，那是另一条命令的事。

## 命令

```
keytime                             今天与之后的 key time（状态：未来/进行中/违约中/已确认）
keytime ack                         确认（停止播报/音量/横幅、解除整页接管；置顶与桌面锁继续）
keytime refresh                     立刻重读日历中间层 + 规则文件
keytime rules                       列出规则文件里的规则和解析错误
keytime rules reset                 规则文件恢复出厂内容（会覆盖你的修改）
keytime on|off                      整条报警链路开关（写 temp/keytime.json 的 enabled；off 会解锁）
keytime focus                       看 Focus.app 状态 / 当前 profile / 锁死用的 profile
keytime focus setup                 新建/刷新「除 iTerm 全杀」的 profile（会重启 Focus.app）
keytime focus on|off                手动锁死 / 解锁桌面（验证用）
keytime notify [文字]               发一条 mac 横幅验证通知链路
open keytime                        数据视图第四种模型：全表 + 规则 + 已超时分钟数
keytime device                      列出 Bark 设备
keytime device add iphone <key> [--server URL]
keytime device remove iphone | on|off iphone
keytime device test [iphone]        发一条测试推送（验证 key / 授权 / 网络）
keytime set <键> <值>               volume / announce_sec / day_end / leave_idle_sec /
                                    summon / restore_volume / push.level / push.sound /
                                    focus.enabled / focus.profile / focus.release_on_ack /
                                    focus.keep / notify.enabled / notify.sound
```

`keytime` 在**所有视图**都可用（挂在 `View.common_commands`）——报警随时可能响，
不管当时在契约、专注还是琐事屏，`keytime ack` 都要能敲。

## 置顶（`summon`）

- **只聚焦，绝不新建窗口**：报警发生在 warden 进程里，只需要把已有窗口拉到最前。
  以前 AppleScript 的语义是「找不到就新建」，而新起来的 warden 又会再 summon 一次
  —— 每隔几秒多开一个窗口（看起来像 malware）。现在 `focus_only=True`：先按
  `temp/warden-tui.json` 里的 tty 找，找不到就**什么都不做**（不 `--new`、也不走
  `open -a` 兜底）；TUI 没开直接放弃。
- **不往 TUI 的 stdout 打日志**：`quiet=True`（照旧写日志文件）。在 TUI 进程里
  `print` 会把界面打乱（曾经看到 `[postsession] 已唤起 warden…` 直接糊在屏上）。
- 只聚焦 iTerm2（AppleScript 假设）。如果你在别的终端里跑 warden，置顶会静默失败，
  但不会影响播报/推送/整页接管。

## 锁死桌面（Focus.app）

用户的 Focus.app（Brad Jasper / heyfocus）里有一条 profile `keytime`：黑名单 =
「扫到的全部 `.app` − 保留名单」。到点用 URL scheme 打开，这块结束 / `keytime off`
时关掉。

- **URL scheme**：`open -g "focus://focus?profile=keytime"` /
  `open -g "focus://unfocus?profile=keytime"`（`-g` 让 Focus.app 在后台接事件，
  不抢 iTerm 前台）。文档：<https://heyfocus.com/docs/features/scripting/>
- **profile 怎么建**：`keytime focus setup`。它会：
  1. 以现有 `night purge` profile 为模板（保留它的脚本/引语/计时器字段）；
  2. 把 `blockedApplications` 换成「模板里已有的 + 扫到的全部 App − 保留名单」；
  3. **先退出 Focus.app 再写盘**（`~/Library/Application Support/Focus/default.cfg`
     是它的配置文件；它跑着的时候会用内存配置盖掉磁盘），写完再打开；写前留
     `default.cfg.keytime.bak`。正在跑别的 profile 时**拒绝重启**（不打断用户自己的 session）。
- **保留名单**：`com.googlecode.iterm2`（唯一终端）、`BradJasper.focus`（Focus 自己，
  杀了没人解锁）、`com.apple.ScriptEditor2`（`osascript` 横幅的宿主）。还要留别的
  （Parallels 之类）→ `keytime set focus.keep com.parallels.desktop.appstore,md.obsidian`，
  然后重新 `keytime focus setup`。
- **只解自己的锁**：`focus://unfocus` 只在「这一段是我们锁的」时发
  （`keytime_focus_on`）；用户在期间自己开的 Focus session 不接管、也不会被我们关掉。
  warden 退出时在 `finally` 里**同步**发一次 unfocus（daemon 线程会被进程退出砍掉）。
- **手动解锁会被锁回去**：每分钟 tick 先读 `default.cfg` 的 `activeSessions`；没在锁就
  重新 `focus://focus` —— 所以「偷偷关掉 Focus」最多管用一分钟。
- **状态怎么读**：只读 `default.cfg` 的 `activeSessions` / `activeProfileID`，不走
  AppleScript（sdef 只有查询命令，iTerm → Focus 的自动化授权还要用户点一次；沙箱里也
  一律 `-10004`）。

## mac 横幅（`notify`）

- `osascript` 的 `display notification`，文字从 **argv** 传（不拼 AppleScript 源码，
  中文/引号不会翻车）；标题 = 规则第一行（`headline`），正文 = 模板渲染后压成一行、
  截 240 字。
- 每分钟一条，**堆叠不覆盖**（通知中心里能看到「已超时 1 分钟 / 2 分钟 / …」）。
- 声音默认 `Sosumi`（`keytime set notify.sound Funk` 换；设成空字符串 = 静音横幅）。
- 宿主显示成「脚本编辑器」；Focus.app 的 `disableDoNotDisturbMode` 在用户配置里是开着
  的（它不强制 DND），所以锁死期间横幅照样弹。`keytime notify 测试` 单独验证链路。

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
  **强制绕过代理**；真机推送仍走系统代理（用户可能就靠它出网）。

## 文件

```
temp/keytime.json           配置（节拍 / 音量 / 推送档位 / focus / notify / enabled；**不含日历名**）
data/keytime-rules.toml     规则表：哪些时间块报警 + 模板文字（用户自己改，不进 git）
temp/keytime-state.json     确认记录：{"acked": {"2026-11-14T16:00": {"at", "left_mac"}}}
data/keytime-devices.json   Bark 设备（含 key，**不进 git**）
temp/calendar-download.json time_blocks 由 download_calendar.py 写入
~/Library/Application Support/Focus/default.cfg
                            Focus.app 的配置（**它的**，不是我们的）：profile / activeSessions
                            / activeProfileID。我们只读 activeSessions，只在 `focus setup` 时写
                            （写前备份 default.cfg.keytime.bak，写前先退出 Focus.app）。
```

全部可用环境变量指到临时目录再跑（`TI_KEYTIME_RULES` / `TI_KEYTIME_JSON` /
`TI_KEYTIME_STATE` / `TI_KEYTIME_DEVICES` / `TI_KEYTIME_FOCUS_CFG` /
`TI_CALENDAR_DOWNLOAD`），测试就是这么做的。**测试绝不去 kill 真 Focus.app**：
`focus_app_running` 也被注入成 False。

## 日历侧（download_calendar.py）

- **全部日历都要拉**（`fetch_caldav(..., all_calendars=True)`）：时间块候选来自所有
  日历，不并进去就永远拿不到「休息」「运动」这些分组的事件。
- session 排期仍然只认 工作/空闲（`select_profile_events`）；`time_blocks` 是另一份
  数据，不参与切 session。
- 提取在 `block_rows()`：跳过全天事件；跨夜的块按天切成两段（前一段收在 23:59）。
  下载层**不判断**哪些算 Key Time —— 那是规则表的事。

## 节拍挂在哪

`WardenShell.frame_key_hook`（`Warden._frame_key`）—— repl_kit 里**唯一**保证
「每秒一定被调一次」的地方（`tick=1.0` 会 invalidate）。不挂在某个视图的
`status_box` 上：那样切到专注/统计视图就不响了。节拍自己再节流 1 秒。

置顶、桌面锁、mac 横幅、手机推送都在**后台线程**里跑：画帧线程等 subprocess 会把
整个 TUI 卡住。唯一的例外是 warden 退出时的解锁（`blocking=True`）—— daemon 线程
会被进程退出砍掉，那时必须同步把 `focus://unfocus` 发出去。

## 未完成 / 已知

- **`keytime goal`（总目标）还没接 OmniFocus note**：现在模板给占位文案。接的时候
  要新加一条只读查询（项目 note）+ 写回命令，并且必须走后台队列（osascript 0.3s 起）。
- **语音队列语义**：说「等之前的放完再继续」；实现是「正在播的一定播完，排队中的
  只保留最新一句」（60s 节拍下几乎不会积压）。
- **`day_end` 默认 23:30**，挤压预测用它算「挤出今日排期」；改 `keytime set day_end`。
- 确认后每 5 分钟推送只在「确认那一刻人在 Mac 外」时生效（用户要求）。
- 规则文件用的是 TOML 的小子集（Python 3.9 没有 tomllib）：`[[rule]]` / `[display]`、
  `"""多行"""`、`#` 注释、裸值。写错键名会报行号；解析失败不会让 warden 崩。

## 回归

```
python3 test/test_keytime.py            # 规则解析 / 匹配优先级 / 模板变量 / 挤压 / 播报 / Bark（90+ 断言）
python3 test/test_keytime_warden.py     # 整页接管 / 面板 / 每帧节拍 / ack / 规则命令 / summon focus-only
python3 test/test_keytime_focus.py      # Focus.app profile 生成 / 读状态 / URL scheme / 到点锁·结束解锁 / 横幅
python3 test/test_calendar_download_layer.py   # 全部日历 → time_blocks（含跨午夜）
python3 test/test_keep_session.py       # summon(focus_only=True) 绝不新建窗口
```
