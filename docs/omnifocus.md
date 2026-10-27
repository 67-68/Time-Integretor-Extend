# OmniFocus 联动（只单向：warden → OmniFocus）

> 三个文件，三层职责：
> `omnifocus_tasks.py`（osascript 小工具，unix 风格）→ `omnifocus.py`（warden 侧薄封装 + 链接解析）
> → `warden.py`（什么时候写、写什么）。**没有反向同步**，见最后一节。

## 为什么是单向

用户的原话：「目前的原则就是不做完整的双向同步，仅做单向的覆盖（除了我手动输入任务）」。

于是边界划得很死：

| 方向 | 谁说了算 |
|---|---|
| 完成 / 放弃 一个任务 | **warden**（本地账本是主）→ 写回 OmniFocus |
| 任务的截止时间 | **warden**（= 它被分配的那场 session 的终点）→ 写回 OmniFocus |
| 「活还在不在、叫什么、在哪条路径上」 | **OmniFocus**（本地从不改这些字段） |
| 用户在 OmniFocus 里手动勾完成 | 收尾核对时**读**一次，本地跟着标完成（不反向改别的东西） |

所以「同一条 OmniFocus 任务在本地可以有多条记录」是**设计**，不是重复：
一条旧场次的记录（已放弃 + 标签「未完成转移」）+ 一条新场次的记录（还在干）。
OmniFocus 那边始终只有一条。

## 1. `omnifocus_tasks.py`：osascript 小工具

Unix 风格：**stdout 只有数据、stderr 只有诊断**，退出码有意义。

```bash
# 读：默认 JSONL 全部任务（走 Omni Automation 快路径，1380 条约 1 秒）
python3 omnifocus_tasks.py
python3 omnifocus_tasks.py --format tsv --header --fields id,name,due,completed
python3 omnifocus_tasks.py | jq -r 'select(.kind != "project_root" and .flagged) | .name'

# 写：按 primary key 精确改一条，默认只预览（退出码 4），--yes 才写
python3 omnifocus_tasks.py set --id <PK> --complete
python3 omnifocus_tasks.py set --id <PK> --complete --due 2026-10-27T11:50 --yes
python3 omnifocus_tasks.py set --id <PK> --drop --yes
python3 omnifocus_tasks.py set --id <PK> --add-tag 写作 --yes
```

退出码：`0` 成功 / `1` 失败 / `2` 参数错 / `3` **自动化权限被拒（TCC）** /
`4` 预览未写 / `5` 任务不存在 / `6` 标签问题 / `7` 拒绝改 project_root。

- `--due` 是**修饰**，不是独占操作：`--complete --due …` 一条命令跑完
  （收尾「缺漏完成补充」就是靠这个把「完成 + 截止时间」一起写回去）。
- 标签操作和状态/截止时间操作**不能混在一条命令里**（要分两次跑）。
- `kind` 字段区分 `action` / `inbox` / `project_root`；`project_root` 是「项目自身」的
  根任务，筛任务一般要排掉。

## 2. `omnifocus.py`：链接解析 + 子进程

### 认得出哪些粘贴形状

在 OmniFocus 里「拷贝任务」拿到的剪贴板里，同一条任务有好几种长相：

```
[数学易错修正](omnifocus:///task/kc-4VLAE_1L)   ← Obsidian / Markdown
<omnifocus:///task/kc-4VLAE_1L>                  ← 纯文本自动链接
omnifocus:///task/kc-4VLAE_1L                    ← 裸链接
title: "数学易错修正"                             ← 只有标题（**没有** primary key）
```

前三种都能拿到 **primary key**（`kc-4VLAE_1L`），也就是 OmniFocus 里的任务身份；
第四种只有标题，认不出身份 —— 那就**不假装**关联上了（`has_id == False`）。
拿标题去猜一条 OmniFocus 任务比不关联更糟：猜错就是改错东西。

`omnifocus.clipboard_links()` 读剪贴板时**先读 HTML**（`pbpaste -Prefer html`），
因为 OmniFocus 给的 HTML 里就有一条 `<a href="omnifocus:///task/…">标题</a>`；
HTML 抓不到才退回纯文本。

```bash
python3 omnifocus.py parse '[数学易错修正](omnifocus:///task/kc-4VLAE_1L)'
python3 omnifocus.py links        # 直接看剪贴板里认出了什么
python3 omnifocus.py get kc-4VLAE_1L
```

### 子进程而不是 import

写操作可能因为 TCC 弹框卡几十秒，甚至把 OmniFocus 抢到前台；放在子进程里，
超时/被杀只影响这一次调用，TUI 不会被拖进去。`Client` 顺便带 20 秒缓存
（一次收尾会连着问好几次「这条完成了没」）。

## 3. warden：什么时候写、写什么

| 动作 | 本地 | Focusmate | OmniFocus |
|---|---|---|---|
| `task <id> done` | 完成 + 归档 | `complete_task` 勾上 | **完成 + 截止时间写回** |
| `task <id> drop` | 放弃 | 不动 | **放弃** |
| `task <id> drop --local` | 放弃 | 不动 | 不动 |
| 收尾 A 延期 | 旧记录放弃 + 标签「未完成转移」 | 用户自己敲的命令决定 | **不动** |
| 收尾 B 放弃 | 放弃 | 不动 | **放弃** |
| 收尾 C 重议 | 复制一条 + 旧的放弃 | 不动 | **不动** |
| 收尾 D 缺漏完成补充 | 完成（手填分钟） | 勾上 | **完成 + 截止时间写回** |
| 收尾 E 快捷推后 | 复制品挂下一场 + 旧的放弃 | 复制品挂上 | **不动** |

**截止时间 = 那场 session 的终点**（11:00–11:50 → `2026-10-27T11:50:00`）。
`task_store.assign()` 里写（覆盖原值，原值留在 `session.previous_due`），
`writeback_completions()` 在完成时把它一起写回 OmniFocus —— 于是 OmniFocus 里
看到的就是「这一场几点结束的」。

写回是**幂等**的：写过的记在 `task.omnifocus.writeback.completed` 上，重复同步不会重复写。

## 4. 收尾时「读」一次

`Warden.sync_completion_state()`（收尾入口调它）：
1. `GET /v1/tasks`（bridge）→ `task_store.reconcile()` → Focusmate 的 `completedAt`
   进归档，**并且把本地 `status` 落成 `completed`**（以前只归档不改状态，靠读文件时
   的老数据兼容规则兜，于是界面上「有完成却看不见 ✓」，用户反馈过）；
2. `omnifocus.py list` **一次** → 哪条 OmniFocus 任务已经 `completed`，本地跟着标完成；
3. `writeback_completions()` 把「本地完成但还没写回」的补写回去。

OmniFocus 读不到（权限没给 / 没装）只变成一句警告：收尾照常进行，
只是少了「OmniFocus 那边也勾了」这条信息。

## 5. 权限（第一次用之前点一次）

macOS 不允许一个 app 悄悄控制另一个 app。第一次跑
`python3 omnifocus_tasks.py` 会弹「终端（或 iTerm）想要控制 OmniFocus」——
**点允许**。点错了会记住拒绝，之后不再弹，只能重置：

```bash
tccutil reset AppleEvents
# 或 系统设置 → 隐私与安全性 → 自动化 → 勾上你的终端 → OmniFocus
```

被拒时 `omnifocus_tasks.py` 退出码是 **3**，warden 里会显示
「自动化权限被拒」而不是一句「失败」——看到这一句就别再试了，去点开关。

## 6. 关掉它

```
set omnifocus off
```

一个字节都不碰（不读也不写），收尾照常问你 A~E，只是「本地说改了什么」那一半还在。
