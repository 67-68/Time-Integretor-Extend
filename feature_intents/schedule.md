# schedule

## 模块职责

`data.csv` 的**唯一**解析 / 序列化 / 编辑实现（`schedule.py`）。

data.csv 是这套系统的**控制面**（人手写）也是唯一真源。两个消费者都 import 本模块：

```
      data.csv（控制面，人手写）
          │
          ├──(只读)──▶ keep_session.py（数据面 daemon）──▶ Focusmate API
          │                                    └──▶ state / verdicts / sins
          └──(读写)──▶ warden.py（TUI）── 草稿区随便增删
```

**为什么要独立成模块**：daemon 和 TUI 必须对「哪一行算一个 slot」「写回文件该怎么写」
有完全一致的理解。两份实现 = 两份真源 = 必然的分歧（旧 `keep_session.load_slots`
已经是一份，TUI 再抄一份就完了）。

## 文件格式

纯文本 CSV，`#` 之后是注释（整行忽略 / 行尾忽略），空行忽略。每行三种写法：

```
HH:MM,duration              —— 每天
weekday,HH:MM,duration      —— 只在 weekday 生效（mon..sun，也认「周一」「星期一」）
*,HH:MM,duration            —— 同「每天」
```

- `duration` 只能是 **25 / 50 / 75**（Focusmate 只认这几种）。
- 时间必须是 15 分钟整点（`:00/:15/:30/:45`），但**解析**不因此拒绝（daemon 的预定
  接口会拒）；`add_row()` 会拦。
- 同一 `(weekday, HH:MM)` 重复：后面的忽略 + 给一条 warning。
- 同一个日期上不同写法撞车（`fri,09:00` 和 `*,09:00`）：`day_slots()` 按文件顺序
  取第一条，**只订一场**。
- **解析永不抛异常**：坏行跳过、收进 warnings 列表返回。这是人手写的文件，
  一行手滑不该让 daemon 停摆或让 TUI 崩掉。

## 滚动 24 小时窗口（核心概念）

```
   ────────┬────────────────┬─────────────────────────▶ 时间
          now            now+24h
    「过去」  │   「执行区」     │      「草稿区」
   只对账     │  daemon 订满     │    零阻力随便改
   不补订     │  取消要敲宣言     │
```

- 「执行区」= `(now, now+24h]`，daemon 只订这里面的；TUI 里取消它要敲 152 字宣言。
- 「草稿区」= `now+24h` 之后，还没被吃进 API，随便改。
- 「过去」= 今天已经开始的，只对账（记 booked / missed / sin），不补订。

**滚动**是关键：每天傍晚，第二天早上的场次就自动进了执行区（= 用户要的「T+1 锁入」），
而**不需要 23:59 的定时任务** —— 机器合盖睡了也没关系，醒来那一轮会把窗口重新填满。

## 对外接口

| 函数 | 用途 |
|---|---|
| `parse_text(text, weekday=None)` | 解析内容 → `(slots, warnings)`，永不抛异常 |
| `load_entries(path=None, weekday=None)` | 读文件 → `(slots, warnings)`；文件不存在 = `([], [])` |
| `load_slots(path=None, weekday=None)` | 只要 slots 的老接口（`test_download_calendar.py` 还在用） |
| `day_slots(entries, day)` | 某一天实际生效的契约（该星期几 + 每天），按时间去重排序 |
| `window(entries, now, hours=24)` | 切出 `{horizon, due, past}` —— daemon 每轮就吃这个 |
| `zone_name(start, now, hours=24)` | `past` / `locked` / `draft` —— 三层摩擦的判据 |
| `key_of(day, slot)` | 状态机 key：`2026-10-09T19:00`（带日期，跨天不串味） |
| `add_row(...)` / `remove_row(...)` | TUI 的增删（合法性校验 + 排序插入 + 先备份） |
| `fingerprint()` | 内容 sha1：改文件前比对，防「改错行」 |

`python3 schedule.py show [YYYY-MM-DD]` / `show --week` 是给人看的只读视图。

## 编辑的安全保证

TUI 直接改人手写的文件，所以三条：

1. **先备份**：每次写之前 `data.csv` 复制到 `temp/data.csv.bak`
   （和 `download_calendar.py` 用同一个位置，纯 stdlib，不进 git）。
2. **原子写**：写 `data.csv.tmp` 再 `os.replace()`，不留下半截文件。
3. **指纹校验**：TUI 每行记住自己是从哪个版本的表里看到的（`fingerprint()`），
   `remove_row(expect=...)` 既比对行号又比对内容 —— 文件被别人改过就报错，
   宁可让你重来，也不删错行。

`add_row()` 按 `(weekday 顺序, 时间)` **有序插入**，所以文件始终和
`download_calendar.py populate` 的输出同构，diff 干净。

## 已知限制（都是格式本身带来的，不是 bug）

- **按星期几重复** = 删一行会影响**所有**同星期几的周（不是只删「明天那一场」）。
  TUI 删完会明确提示这一点。
- **表达不了「某个周三不上」这种例外**：要么删整条规则，要么那天手动取消（敲宣言）。
  真需要按日期覆盖的话，得扩展成「周日几规则 + 显式日期行」两套语义 —— 目前**不做**，
  复杂度不值（奥卡姆）。
- `populate` 会**整表覆盖** `data.csv`，所以 TUI 的增删在下次 `populate` 时会被冲掉。
  日历是上游、csv 是计划、TUI 是临时手改：三者关系就是这样，别把 TUI 当持久层。
