# download_calendar

## 模块职责

把 iCloud 日历（CalDAV）拉下来，切成 Focusmate 能预约的时间段，写进 `data.csv`。
**只生成计划，不下单**——下单永远是 `keep_session.py` 的事。

## 数据流

```
iCloud 日历 ──CalDAV──▶ 事件 ──过滤/切分──▶ 一周 session 计划 ──▶ data.csv
                                              └──▶ temp/calendar.md（给人看的中间产物）
```

`data.csv` 是 `keep_session.py`（每天 check）唯一的目标来源。两者解耦：
日历变了就手动重跑 `populate`，daemon 不关心日历，只读 csv。

## 切分规则

需求原文 → 实现：

1. `< 25min` 的时间段**丢弃**——Focusmate 最短 25 分钟，订不了。
2. 起点**往后**对齐到最近的整点/quarter（`:00/:15/:30/:45`，Focusmate 只认这些）。
3. `> 50min` 的时间段**优先订 50**，塞不下才退 25。
4. 切出来的 session 从原时间段**挖掉**，剩下的继续算（左缺口 ≤15min 必然 <25min 被丢，
   右剩余继续迭代）。
5. 忽略「上课」「休息」分组。
6. 结果**覆盖**写入 `data.csv`。

### 例子（都进了单测）

| 输入时间段 | 输出 session |
|---|---|
| 14:10–15:30 | 14:15–15:05 (50) |
| 14:00–17:00 | 14:00–14:50、15:00–15:50、16:00–16:50 |
| 09:00–09:20 | 无（< 25min） |
| 09:05–09:30 | 无（对齐到 09:15 后只剩 15min） |
| 09:00–09:30 | 09:00–09:25 (25) |
| 09:00–10:20 | 09:00–09:50（剩 30min，但从 10:00 起加 25 会到 10:25，超了） |
| 重叠两段 | 先并成一段再切 |

## 关键取舍（先读这里）

- **日历事件 = 可用的空闲时间段**，不是「忙」。所以「忽略上课/休息」= 把这两个
  日历里的事件排除在候选之外。若你的日历反过来记的是「忙」（空闲 = 补集），
  `plan_week()` 要改成求补集。
- 分组过滤按日历**显示名**精确匹配：
  - `--ignore 上课 休息`（默认）：黑名单。
  - `--use 自习`：白名单，**优先**，此时 ignore 不参与（想在白名单里再排除就用不带）。
- 全天事件默认**跳过**（会单独列出来），要当整天可用就加 `--include-all-day`。
- 今天只保留**还没开始**的 session；未来几天全保留。
- 时区：按本地时区（`Asia/Shanghai`）切，事件本身的 TZID 会被正确换算。

## 凭据

`temp/calendar.json`（权限 0600，在 gitignore 的 `temp/` 里）：
`url` / `username` / `password` / `ignore_calendars` / `use_calendars`。

iCloud 的密码必须是 **App 专用密码**（appleid.apple.com → 登录与安全 → App 专用密码），
**不是** Apple ID 登录密码。开了双重认证也走这条路。
（默认 `https://caldav.icloud.com`；caldav 库会自动做 RFC6764 发现，跳到实际的
`pXX-caldav.icloud.com`。）

## 依赖

`caldav`（连带 `icalendar` / `recurring-ical-events`）装在 **`temp/pylibs`**，
用 `python3 download_calendar.py install-deps` 一次性装好，脚本启动时把
`temp/pylibs` 插进 `sys.path`。

不用 `pip install --user`：`~/Library/Python` 在很多环境下不可写（沙箱/公司机器），
装到项目内既省事又不污染系统。

## 命令

| 命令 | 作用 |
|---|---|
| `install-deps` | 把 caldav 装到 `temp/pylibs` |
| `login` / `logout` | 存/删凭据（login 会当场连一次验证并列出日历） |
| `status [--live]` | 看依赖版本 / 配置（密码打码）/ 产物；`--live` 真连一次列日历 |
| `fetch` | 只拉日历出 `temp/calendar.md`，不碰 `data.csv` |
| `populate` | 拉 → 切 → 写 `data.csv`（**一次性，只手动触发**） |

通用选项：`--days 7`、`--start YYYY-MM-DD`、`--ignore`、`--use`、
`--include-all-day`、`--md PATH`、`--dry-run`；`populate` 另有 `--force` / `--no-md`。

**离线模式**：`--ics a.ics`（可重复给多个，用来看多个分组）直接从本地 `.ics`
读，不联网。分组名取 `X-WR-CALNAME`，没有就用文件名。测试和排查都靠它。

## 安全阀

- 一个 session 都没切出来时 `populate` **直接报错退出**，不覆盖 `data.csv`
  （除非 `--force`）——防止模型/过滤配错时把计划清空。
- 覆盖前把旧 `data.csv` 备份到 `temp/data.csv.bak`。
- `--dry-run` 只打印不写任何文件。

## 已知限制

- 只排 25/50 两种时长（`keep_session.py` 也接受 75，但本脚本不会排 75）。
- 依赖 RRULE 展开：CalDAV 路径交给 `recurring-ical-events` 对服务器返回的对象展开，
  离线路径同样。展开失败会退化为「只取原始 VEVENT」并打 warn。
- 「事件 = 空闲」这个模型如果不符，需要改 `plan_week()`（见上）。
