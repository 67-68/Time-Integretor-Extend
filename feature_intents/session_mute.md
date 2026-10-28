# session_mute（静音时段）

## 一句话

`period mute T1 --permanent` = **这个时段以后都静音**；设置之后，落在那个窗口里
的 session 自动切到静音：Focusmate 的 quiet 打开，daemon 的分心看门狗不出声。
违规照记、收尾照催 —— 静音是「别用声音管我」，不是「别管我」。

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

## 判定（唯一实现）

| 函数 | 谁在用 |
|---|---|
| `sp.mute_window_at(moment)` | daemon 看门狗、warden 表格、`warden.py:mute_window_for` |
| `sp.is_muted_at(moment)` | `keep_session.idle_tick` |
| `sp.muted_period_ids(day)` | 契约表 / 状态行 |
| `sp.mutes_for_day(day)` | 上面几个的底层 |

跨午夜的窗（`23:30–00:30`）按 +24h 算，并且**会算到第二天凌晨**（查昨天那条里
跨午夜的部分）。

## 落地（意图 → 结果）

1. **Focusmate quiet**：warden 每帧（节流 5 秒）看一眼「时段要求静音但 quiet 还不是
   ON」的行 → 后台线程调 bridge `mute quiet=True`，`GET /api/session` 复核。
   同一场 5 分钟内只试一次（`mute_tries`），失败不会每帧开浏览器。
   `period mute` 命令本身也会立刻落地一次。
2. **本地不出声**：`keep_session.idle_tick` 里，静音窗内不发通知、不拉音量、
   不播放嘶吼；日志写一行「静音时段（T1 11:00–12:05）→ 该播放…不响」。
3. **表格显示**：契约表的「静音」列 —— `ON` = Focusmate 已静音；`静…` = 时段要求
   静音但还没设上；`·` = 不静音。

## 命令

```text
period mute T1               # 只静音今天/选中那天的 T1
period mute T1 --permanent   # 以后每个这个星期几的 T1 都静音
period mute T1 off           # 撤销（也认 period unmute T1）
period unmute T1 --permanent # 撤销按星期几那条
```

## 测试

- `test/test_session_plan.py`：「静音时段」整段 —— 按星期几 / 按日期、覆盖不叠加、
  跨午夜、坏记录被丢掉。
- `test/test_idle_watchdog.py`：「静音时段：违规照记，但不出声」—— 三次违规也不放歌，
  撤销之后立刻恢复出声。
- `test/test_task_ui.py`：`period mute T1 --permanent` 写表、表格标 `静`、
  `apply_mutes()` 只对「该静音且没静音」的行调 bridge、已经 ON 的不重复动。
