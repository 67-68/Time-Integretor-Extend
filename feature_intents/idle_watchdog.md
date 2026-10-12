# idle_watchdog（分心看门狗）

## 模块职责

`keep_session daemon` 在**session 进行中**额外跑的一个检查：

1. **每个「整整一分钟」**没有键盘/触控板输入 → 记一次「违规」，弹通知，
   内容含**当前违规次数**，并说明「到第 3 次会有不好的事情发生」。
2. 累计到第 **3** 次 → **把系统音量拉到 10% 并播放
   `data/audio/赵小六嘶吼.mp3`**（走 [`alert.py`](./alert.md)）。

旁边还配了一个**暂停**：在 warden 的[专注视图](./warden.md)里敲 `stop` 消耗一次，
暂停 5 分钟、每天最多 3 次，暂停期间看门狗不念你。

它回答的是「我订了 Focusmate，人却不在桌前」这件事 —— daemon 守契约，
看门狗守「人在不在」。

## 在哪跑、什么时候上班

- 跑在 `cmd_run()`（daemon / `run` 前台循环）**两轮 round 之间的睡眠里**，
  每 `IDLE_POLL_SEC=5s` 一次 `idle_tick()`。
- `check`（只跑一轮）**不跑**它 —— 它不是补订逻辑的一部分。
- **只在 session 进行时上班**：`current_session()` 按 `data.csv` 的契约窗口
  （`start ≤ now < start+duration`，也看昨天以覆盖跨午夜）判断；不在任何
  session 里就什么都不做，不打扰（但仍会记录最近空闲秒数，供专注视图展示）。
- daemon 每 5 分钟才醒一次主循环，但看门狗挂在**分段睡眠**里，所以仍是 ~5s 的
  粒度；下单那几十秒不会和「session 进行中」重叠（进行中的场次不会再被订）。

## 规则（都在 `idle_tick()` 里）

空闲状态由 `temp/keep_session-idle.json` 落盘：

| 字段 | 含义 |
|---|---|
| `strikes` | 本场累计违规次数（通知次数），封顶 `IDLE_MAX_STRIKES` |
| `episode_marks` | 当前这段连续空闲已经数过几个「整分钟」 |
| `last_idle_seconds` / `last_tick_at` | 最近一次读到的空闲秒数（专注视图展示用） |

- **每整整一分钟记一次**：`marks = idle // 60`，每跨过一个分钟边界就记一次；
  同一分钟内不重复。连续空闲 3 分钟 = 3 次通知 → 第 3 次触发。
- **不追溯**：本场第一次观察、或中间有人动过键鼠/触控板之后，把
  `episode_marks` 对齐到当前 `marks` —— 不从 0 开始补算你「已经空闲了多久」。
  否则「session 一开始你已经闲了 5 分钟」或「daemon 刚起来」会瞬间连爆三次。
- **人在动 = 本段清零**：空闲时间比上一次读数还小（说明中间有输入），
  本段分钟计数归零、重新数；但**本场累计 `strikes` 不归零**。
- **读不到空闲时间就不判**：`ioreg` 出错 / 非 macOS / 输出里没有该字段 → 返回
  None，跳过本轮。绝不把「读不到」误判成「没输入」。
- **读不到时钟也不会崩**：`idle_tick` 在 `cmd_run` 里被 try/except 包住，
  出错只记日志、下轮再试。
- `dry_run` 下只记数 + 打日志，不通知、不出声（方便调试）。

## 空闲时间从哪来

```python
ioreg -c IOHIDSystem        # → "HIDIdleTime" = <纳秒>
```

`HIDIdleTime` 是「最近一次 HID 输入到现在」的纳秒数，**系统级**、覆盖键盘/鼠标/
触控板，正好就是「没有键盘或者触控板输入」。除以 `1e9` 得秒。

- 没用 `Quartz.CGEventSourceSecondsSinceLastEventType`：要 pyobjc，本仓库坚持
  「纯 stdlib、不 pip」，而 `ioreg` 是 macOS 自带。
- `ioreg` 在 `/usr/sbin`，daemon 脱离终端后 PATH 极简，所以写死
  `IOREG_BIN = /usr/sbin/ioreg`（`subprocess` 5s 超时，失败返回 None）。

## 三次的「不好的事情」

```python
alert.play(IDLE_ALERT_AUDIO, volume=100, system_volume=10)
# ≈ osascript -e "set volume output volume 10"  →  afplay -v 1.0 赵小六嘶吼.mp3
```

即字面的「把音量拉到 10% 再播放」。达到 3 次后本场不再重复触发；换到下一场
session 时违规次数归零。音频不存在 / `alert.py` 不可用时只记日志，不报错。

## 暂停（专注视图的 stop）

- 状态落盘 `temp/keep_session-pause.json`：`{date, used, pause_until}`；
  跨天自动把 `used` 归零。
- `pause_status(now)` → `{used, remaining, active, left_sec, pause_until}`；
  `start_pause(now)` → `(ok, status, reason)`，每次 `PAUSE_MINUTES=5` 分钟，
  每天上限 `PAUSE_MAX_PER_DAY=3`。暂停中再按会被拒。
- warden [专注视图](./warden.md)里 `stop` 调 `start_pause()`；专注视图每秒
  重画，所以剩余时间是**动态**的。
- daemon 的 `idle_tick()`：暂停期间**只更新 `last_idle_seconds`**、把
  `episode_marks` 对齐到当前 `marks`，不记违规、不通知 —— 暂停结束后从下一个
  整分钟接着数（不补算暂停期间的空闲）。

## 配置

| 常量 / 环境变量 | 默认 | 说明 |
|---|---|---|
| `IDLE_THRESHOLD_SEC` | 60 | 每多久没输入算一次违规 |
| `IDLE_POLL_SEC` | 5 | 看门狗多久看一次 |
| `IDLE_MAX_STRIKES` | 3 | 第几次触发「不好的事情」 |
| `IDLE_ALERT_AUDIO` | `data/audio/赵小六嘶吼.mp3` | 告警音频 |
| `IDLE_ALERT_VOLUME` | 10 | 触发时把系统音量拉到几 % |
| `PAUSE_MAX_PER_DAY` | 3 | 每天最多暂停几次 |
| `PAUSE_MINUTES` | 5 | 每次暂停多少分钟 |
| `KEEP_SESSION_IDLE_CHECK=0` | 开 | 关掉整个看门狗（restart daemon 生效） |

> 改了这些常量/音频要 `python3 keep_session.py daemon restart` ——
> daemon 是启动时把代码读进内存的，光改文件不生效。

## 测试

`python3 test/test_idle_watchdog.py` —— 固定「今天 10:00」当 now，
`notify` / `play_alert` / `hid_idle_seconds` 全换成替身，不联网、不出声：

- session 门控（不在 session 不记，但记空闲秒数）；
- 每整分钟记一次、同一分钟不重复；
- 三次后拉低音量 + 放赵小六、之后本场不重复；
- 人回来动一下：本段清零、累计不归零；
- 换 session 计数归零；
- 刚上班时已经很闲：不追溯、不爆击；
- 暂停期间不打扰、解除后接着数、每天 3 次用完拒绝；
- 读不到空闲时间不误判；
- `dry_run` 不打扰、总开关生效。

`test/test_warden.py` 覆盖专注视图渲染 + Tab 切换 + `stop` 消耗暂停。

## 已知取舍

- 判断依据是 `data.csv` 的**契约窗口**，不是 API 里「真的订上了」。也就是说这一场
  就算没订上（failed），到了计划时间看门狗照样要求你专注。这是有意的：契约写了
  就该在。
- 只覆盖键盘/触控板/鼠标这类 HID；外接设备走别的通道时 `HIDIdleTime` 可能不准。
- 达到 3 次后本场不再重复响（避免一直轰炸）；想再来就等下一场。
- 暂停只在 warden TUI 里能按（`stop`）；CLI 侧可以直接调
  `keep_session.start_pause()`，但没做成独立命令。
