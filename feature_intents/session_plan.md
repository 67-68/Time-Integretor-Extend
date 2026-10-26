# session_plan

## 模块职责

日历下载与 `data.csv` 之间的中间层（`session_plan.py`）。把「下载了什么」
和「用户想怎么排 session」分开：

```
iCloud 日历（只 工作 / 空闲，今天 + 7 天）
  → temp/calendar-download.json   原始事件 + 按时段编号的 T1/T2/...
  → temp/session-layouts.json     用户对每个时段的自定义时长序列
  → data.csv                      精确日期行（YYYY-MM-DD,HH:MM,duration）
```

`temp/calendar.md` 是同一份下载层的人读版（时段表 + 默认切分预览）。

## 时段语义

- 一个日历事件 = 一个时段。按天按开始时间编号 `T1`, `T2`, ...
- `类型` = 日历显示名（工作 / 空闲）；`名称` = 事件 SUMMARY。
- 用户只给时长序列，起点由程序算：第一场从时段起点开始，之后每场从上一场
  结束后的下一个 `:00/:15/:30/:45` 开始。
- 例：`T1 11:00–13:00`
  - `T1 50 50` → 11:00–11:50, 12:00–12:50
  - `T1 25 25 25 25` → 11:00–11:25, 11:30–11:55, 12:00–12:25, 12:30–12:55
  - `T1 50 50 50` → **报错，零副作用**（第三场会越过 13:00）

## 落盘安全

- `plan_period()` 纯计算、不写文件。
- `set_layout()` 先完整验证，再写 `session-layouts.json`；验证失败不留半截 layout。
- `data.csv` 只在 layout 成功后重算；daily schedule 的精确日期行优先于旧的
  weekday 循环规则（同一时刻日期行覆盖）。
- `schedule.py` 同时支持 `YYYY-MM-DD,HH:MM,duration` 与旧格式，daemon 不需要改接口。

## 命令

| 命令 | 作用 |
|---|---|
| `download_calendar.py download/fetch` | 只下载工作+空闲 → 中间层 JSON + md |
| `download_calendar.py populate --from-download` | 中间层 + layout → data.csv |
| `download_calendar.py layout T1 50 50 [--date ...]` | 验证并保存一个时段的排布 |
| `download_calendar.py show` | 打印所有时段及其当前 session |
| TUI `session T1 50 50` / `period T1 50 50` / 裸 `T1 50 50` | 同上 |

## 边界

- 只认 25/50/75（Focusmate 约束）；超时/非法时长直接 ValueError。
- 不支持在时段里手工指定绝对起点；需要更自由的排布就改日历事件本身。
- 没有目标 profile 时退回旧的 ignore/use 过滤，避免把结构不同的日历静默清空。
