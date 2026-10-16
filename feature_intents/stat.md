# stat

## 模块职责

`stats.py` —— **生涯统计的取数与算数**（只读），给 warden 的「统计」视图和 `stat` 命令供数。

它回答的是「这么久到底干成什么样了」：一共做了多少场、每天平均多少、
有多少天一场都没做、哪一天最多、每天场次的中位数 / 25th percentile 在哪。

**唯一的写操作是写自己的缓存文件**。它不碰 Focusmate 的任何写接口（内部 API 的
PUT/DELETE 那套在 `scripts/` 里，和这里无关），不改 `data.csv`，不改 daemon 的状态文件。

渲染不在这里：本模块不 import rich，只吐 dict / 纯文本。面板长什么样是
`warden.py` 里 `StatsView` 的事。

## 数据源（实测，2026-10-17）

公开只读 API，认证是 `X-API-Key`（和 `keep_session.py` 同一个 token，取自 `main.py`）。

| 调用 | 给什么 |
|---|---|
| `GET /v1/me` | `userId` / `name` / `memberSince` / `totalSessionCount` / `timeZone` |
| `GET /v1/sessions?start=&end=` | 区间内的**全部** session（含已过去的），按 UTC ISO 给边界 |

关键事实：

- **单次查询上限 365 天**：366 天直接 `400 Bad Request`（不是分页，是拒绝）。
  所以拉全量历史要**按 ≤365 天分块**（`chunk_ranges()`），块边界用 UTC 瞬间，
  拉回来再按本地日期过滤，顺带按 `sessionId` 去重（块与块之间会有重叠的一条）。
- 能拿到**全量历史**：本账号 `memberSince=2025-12-26`，一次「2026 全年」拿到 2251 条。
  没发现翻页参数。
- `/v1/me` 的 `totalSessionCount`（1933）＝ **自己 `completed=true` 的场次数**，
  比「订过多少场」少 —— 用它对账。
- 每条 session：`sessionId / sessionType / duration(ms) / startTime / title / users[]`。
  「我」是 `users[]` 里 `userId` 对得上的那条，只有它带
  `joinedAt / completed / activityType / preferences`。
- `duration` 是毫秒（50 分钟 = `3000000`）。

## 口径（改这里就改了命运）

- **完成 = 我自己那条 `users[]` 记录的 `completed == true`**。
  不是「订了」（`total_booked` 单独留着对照），也不是「进过房间」（`joinedAt`）。
  本账号两者差得不少：订了 2261 场，`completed=true` 只有 1933 场。
- **区间 = `memberSince` 的本地日 → 昨天**。今天的场次还在进行中，算进去只会污染
  平均数和分位数，所以**一律不算今天**。`memberSince` 读不到就退到「第一条 session
  的本地日期」。
- **分母 = 区间里的每一天**，包含一场都没做的空天 —— 不这么算，「有多少天一个
  session 都没做」就没有意义了。
- 所有「一天」都按**本地日期**分桶（`Asia/Hong_Kong`）。API 给的是 UTC，
  直接按 UTC 切会把 23:15 那场算到第二天去。
- 分位数用 inclusive 线性插值（和 `statistics.quantiles(method="inclusive")` 同义），
  自己实现是为了 `n<2` 时不抛异常。

## 缓存

`temp/stat-history.json`（`temp/` 在 .gitignore 里），只存瘦身后的字段。

- TTL `CACHE_TTL`（1 小时）。
- **缓存的 `until` 必须和这次要算的 `until` 一致**才复用：昨天到今天过了午夜，
  缓存里缺今天那天的数据，直接复用会把昨天算成空天。
- `force=True`（TUI 里 `stat refresh`）无视缓存重拉。
- 拉失败时**回落到旧缓存**并标注「拉取失败（…）→ 退回缓存」；连缓存都没有才抛出去
  由 TUI 兜（TUI 只记一条 error 消息，不掀屏）。

## 算出来的东西（`compute_stats`）

纯函数：不联网、不读文件、不 import rich，喂一个 history dict 就出数。给视图用的字段：

| 字段 | 含义 |
|---|---|
| `since` / `until` / `days` | 区间与分母 |
| `total_completed` / `total_booked` / `completion_rate` | 完成 / 订了 / 完成率 |
| `mean_per_day` / `mean_active_day` | 每天平均（含空天）/（只算做过的天） |
| `zero_days` / `zero_rate` / `active_days` | 一个 session 都没做的天数与占比 |
| `max_day` / `max_per_day` / `min_per_day` | 峰值是哪天、多少场 |
| `p25` / `median` / `p75` | 每天场次的分位数（含空天，所以 0 很常见） |
| `total_minutes` / `total_hours` / `avg_minutes` | 只累计**完成**的场次 |
| `streak_current` / `streak_best` | 连续有「完成」的天数 |
| `per_day` / `by_month` / `recent` | 明细：每天 / 每月 / 最近 14 天 |
| `fetched_at` / `source` | 数据是什么时候拉的、实时还是缓存 |

## 和 TUI 的接口

- `stats.load(...)` → `(history, note)`；`note` 是给人看的一行（"实时" / "缓存" /
  "拉取失败（…）→ 退回缓存"）。
- `stats.compute_stats(history)` → 上面那张表。
- `stats.format_text(stats)` → 纯文本（`python3 stats.py` 用，排查时不开 TUI）。

TUI 侧：`warden.py` 的 `StatsView`（Tab 第 3 个视图）渲染面板，**底部**逐行列出
「永不重订」名单（`temp/keep_session-verdicts.json` 的 `abandoned`）——
那份名单的**唯一展示位**就在这里，平常视图只留一句指路。

## 不做的事

- 不写 Focusmate（不 book / 不 cancel / 不改 title）。统计是照镜子，不是动手。
- 不替 daemon 做决策：`abandoned` 仍然是 `keep_session.py` 读了才生效的判决。
- 不做高频轮询：TTL 缓存 + 手动 `stat refresh`，别把只读接口当监控探针用。
