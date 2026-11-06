# session_mute（静音时段）

## 一句话

`period mute T1 --permanent` = **这个时段以后都静音**；设置之后，落在那个窗口里
的 session 自动切到**静音模式（Focusmate 的 quiet mode）**。

⚠️ 「静音」的范围**只有 Focusmate 那一场**。用户明确过：*「daemon 应该在静音时段
发通知，拉音量和放嘶吼，静音只是指 focusmate session」*。所以本地一概不变 ——
分心看门狗照旧通知、照旧拉音量到 10%、照旧播放 `赵小六嘶吼.mp3`；
`keep_session.py` 里**没有任何一行**读这张静音表（有测试钉着：`hasattr(ks, "muted_window") == False`）。

## 为什么不是复用 Focusmate 的 quiet

Focusmate 的 quiet 是**单场**开关：得一场一场点，而且明天新订的那场又是响的。
用户要的是**排班意图**（「这个时段我就是要安静」），它和 `session --permanent`
是一类东西 —— 所以它和长期规则一样，按星期几 / 按日期重复地落一张表。

## 数据

`temp/session-mutes.json`（`session_plan.load_mutes` / `save_mutes`）：

```json
{"version": 1, "mutes": [
  {"period": "T1", "weekday": "tue", "start": "11:00", "end": "12:05",
   "permanent": true, "created_at": "2026-10-28T16:18:08"},
  {"period": "T2", "date": "2026-10-28", "start": "15:00", "end": "16:00",
   "permanent": false, "created_at": "2026-10-28T16:20:01"}
]}
```

- `--permanent` 写 `weekday`（每个这个星期几都静音）；不写就写 `date`（只这一天）。
- 记录里存**时间窗**而不只是时段 ID：静音要能在没有日历下载层、没有 data.csv
  的地方判 —— daemon 的分心看门狗只想知道「现在这个钟点该不该出声」。
- 同一目标重复设置 = 覆盖时间窗（排班可能改过），不报错。

## 通用静音：`mute-universal`（不绑 T 时段）

`period mute` 绑的是日历下载层里的某个时段。用户还需要一种不依赖下载层、
只看「星期几 + 时间窗」的静音规则：

```text
mute-universal 13:00 - 15:00 Mon
```

意思是：**每个周一，凡是与 13:00–15:00 有重叠的 session 全部静音**。和
`period mute` 的「起点落在窗内」不同，这里按用户原话「只要涉及了就算」：
14:30–15:30 碰到窗口尾巴也算；12:30–13:15 碰到窗口开头也算；刚好 13:00
开始 / 15:00 结束的边界按半开区间处理。

- 可一次给多个星期几：`Mon,Wed` / `周一 周三` / `Mon-Fri`；
- 同一个星期几 + 同一个起点再设一次 = 覆盖终点，不叠加；
- `mute-universal list` 看全部；`mute-universal off 13:00 - 15:00 Mon` 撤销；
  `mute-universal off` 清空全部通用静音；
- 仍然只改 Focusmate 的 quiet，本地通知 / 音量 / 嘶吼一概照旧。

数据也落在 `temp/session-mutes.json`，通用记录带 `"universal": true` 且
`"period": ""`。这样 `period mute` 的旧语义完全不变，两套规则可以并存。

## 判定（唯一实现）

| 函数 | 谁在用 |
|---|---|
| `sp.mute_window_at(moment)` | warden 表格（`warden.py:mute_window_for`）、自动 quiet |
| `sp.is_muted_at(moment)` | 排障 / 测试用（daemon **不**用它） |
| `sp.muted_period_ids(day)` | 契约表 / 状态行 |
| `sp.mutes_for_day(day)` | 上面几个的底层 |
| `sp.mute_overlaps_window(day, start, end)` | `mute-universal` 的重叠判定（只认通用记录） |

跨午夜的窗（`23:30–00:30`）按 +24h 算，并且**会算到第二天凌晨**（查昨天那条里
跨午夜的部分）。

## 落地（意图 → 结果）

1. **Focusmate quiet**：warden 每帧（节流 5 秒）看一眼「时段要求静音但 quiet 还不是
   ON」的行 → 后台线程调 bridge `mute quiet=True`，`GET /api/session` 复核。
   同一场 5 分钟内只试一次（`mute_tries`），失败不会每帧开浏览器。
   `period mute` 命令本身也会立刻落地一次。
2. **表格显示**：契约表的「静音」列 —— `ON` = Focusmate 已静音；`静…` = 时段要求
   静音但还没设上；`·` = 不静音。
3. **本地什么都不改**：通知、音量、嘶吼、收尾催办全和平时一样。
   （曾经在 `idle_tick` 里做过「静音时段闭嘴」，用户明确否掉了 —— 那不是他要的意思。）

## 命令

```text
period mute T1               # 只静音今天/选中那天的 T1
period mute T1 --permanent   # 以后每个这个星期几的 T1 都静音
period mute T1 off           # 撤销（也认 period unmute T1）
period unmute T1 --permanent # 撤销按星期几那条
mute-universal 13:00 - 15:00 Mon          # 每个周一 13:00–15:00 有重叠就静音
mute-universal list                      # 看通用静音窗
mute-universal off 13:00 - 15:00 Mon     # 撤销这一条
mute-universal off                       # 清空全部通用静音
```

## 测试

- `test/test_session_plan.py`：「静音时段」整段 —— 按星期几 / 按日期、覆盖不叠加、
  跨午夜、坏记录被丢掉。
- `test/test_idle_watchdog.py`：「静音时段（period mute）**不**管这里」—— 静音窗里
  第 1 次违规照样发通知、第 3 次照样拉音量放嘶吼，并且钉住「看门狗里没有静音分支」。
- `test/test_task_ui.py`：`period mute T1 --permanent` 写表、表格标 `静`、
  `apply_mutes()` 只对「该静音且没静音」的行调 bridge、已经 ON 的不重复动；
  以及 `mute-universal` 只碰尾巴也算、`off` 撤销、旧 period 规则不受影响。
- `test/test_session_plan.py`：通用静音与窗口的重叠边界（含起止点）、按星期几、
  多星期几、覆盖不叠加、撤销。
