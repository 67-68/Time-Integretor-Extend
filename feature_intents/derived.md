# 派生重复任务（note 标题清单）

**状态**：v1 已实现（2026-11-15）。相关代码：`derived.py`（纯逻辑）、
`warden_core/derived_mixin.py`（命令 / 勾 note / 收尾）、`project_plan.py` +
`project_store.py`（plan 展开与轮转）、`omnifocus_tasks.py`（batch `note_mark`）。

## 这是什么

一条「**每天重复**、**不以推迟日期为锚**」的任务，如果 note 里写了

```markdown
# titles
- 2019-亚太-12
- 2019-亚太-22
- 2019-亚太-32
```

那它就不是一条任务，而是一份**一次做一条的清单**：每一行标题都可以变成一条
**派生任务**（本地任务，不写回 OmniFocus）；做完一条就把 note 里那行勾成
`- [x] …`，而不是完成原来的重复任务；整份清单勾完之后**自动放弃整个重复任务**
（OmniFocus 里 `drop --all-occurrences`，走 outbox）。

用户原话：「这个重复任务不是普通的重复任务，每次完成一个子任务之后更新原本的
重复任务而不是完成原本的重复任务；在勾完之后自动放弃一整个重复任务。」

## 判定（`derived.eligible`）

三个条件同时满足：

1. 重复规则是**每天**：`rule` 里 `FREQ=DAILY` 且 `INTERVAL` 缺省或 1；
2. 重复锚点**不是** defer 日期：`anchor_date_key` 不等于 `DeferDate`
   （OmniFocus 的 JS 字符串是 `[object Task.AnchorDateKey: DeferDate]`）；
3. note 里有 `# titles` 段（大小写、前后空格容忍），且至少一行**未勾选** bullet。

`- ` / `* ` / `+ ` 都认；`[x]` / `[X]` 算已勾选，`[ ]` 或没有 checkbox 算未勾选。
`# titles` 段到下一个 markdown 标题为止；段外的 bullet 一律不算。

## 展示

- **任务格子 / session 表**：父任务的 count 显示成范围 `(0/25-125)`，上界 =
  **所有剩余派生条目的加和**（每条派生继承父任务的 count；不含本体）。
  例如父任务 count=25、还剩 5 条未勾 → `25-125`。全部勾完 / 不是这种任务时
  回到单个数字。
- 派生任务自己的标题是 `父任务：这一行的标题`，在任务面板里和普通任务一样
  挂 session、记 count、进 Focusmate。

## 命令

| 命令 | 作用 |
| --- | --- |
| `task <序号> -d` | 把**下一条未勾选**标题变成派生任务；默认挂到最近的 session（进行中的场优先，其次下一场未开始的） |
| `task <序号> -d 2` | 指定第 2 条未勾选（或 note 里第 N 行） |
| `task <序号> -d -ac` | 派生后按 `-ac` 挂到当前这一场；`-act` / `-n` / `-c` 等后缀同理 |
| `task <序号> -ac` | **被拒绝**：这种任务要先 `-d`，再挂派生出来的那条 |

连续的 `-d` 会依次拿到 12、22、32：已经在做（active）的行不会重复派生。
派生任务不建 OmniFocus 关联，也不写 due；唯一的写回是父任务的 note。

## 完成 → 勾 note → 整份清单完了就放弃

1. 任何路径把派生任务标成 `completed`（`task done` / `task -m complete` / 收尾 /
   `-c` 够数）都会走 `TaskStore.set_status` 的观察者钩子
   （`task_store.after_status`）。
2. 如果这条派生任务只是 plan 拆出来的一个 part（`fields.count < count_total`），
   则等**整条派生任务**的投入 count 够了（`project_store.invested` 按合成 pk
   汇总）才勾，避免「做了第一场就把整条历史卷子记成做完」。
3. 勾 note：先改本地两份缓存（项目清单里的任务 + 本地父任务备注），再往
   `omni_outbox` 排一条 `{"op": "note_mark", "id": <父 pk>, "match": <行标题>}`。
   空闲 / `refresh` 时由 batch 一次 osascript 读→改→写；按**标题文本**匹配，
   不按行号（note 可能被改过），已经是 `[x]` 就跳过（幂等）。
4. `note_mark` 写回成功、并且 `# titles` 段里一条未勾的都不剩 → 自动
   `drop --all-occurrences`（outbox 里再排一条 `drop`），本地父任务和所有还活着
   的派生任务一起标成 dropped（`status_reason=derived-done`）。

## plan 集成

`project_store.remaining_tasks()` 会把合格的任务展开成派生项（不排本体）：

- `pk` 用**合成 pk** `<父 pk>#d<行号>`，count 投入按它记账；重新 plan 时能找回
  同一条 (`project_store.invested` 按 `fields.project_task` 聚合)。
- **parallel**：一次只展开**下一条**未勾选标题（已经投满的行跳过、顺延到再下一条）；
  整组派生项排到普通任务**最后**，组内按轮转排序 —— `rotation` 是
  `{父 pk: 最近一次完成派生任务的时间}`（`warden.derived_rotation()`），
  没做过的先上，做过越久越先回来。这就实现了「完成一个之后把它放到最后一个任务，
  其它同类都过一遍之前排除」。
- **sequential**：把**全部**未勾选标题按 note 顺序展开在原位 —— 前一条的所有
  副本做完（count 投满）才轮到下一条，符合「把 12、22、32 都做完才能开始后面的」。
- 派生项在 plan 落盘时走单独的字段（`fields.derived_*`）和 `source.kind=derived`，
  `omnifocus=None`，不会在完成时勾掉 OmniFocus 里的父任务。

## 边界 / 已知限制

- 只认**项目库 / 本地任务上已有的 note 缓存**。note 在 OmniFocus 里改了之后，
  要等 `project sync` / `refresh` 把 note 刷回来；`-d` 用的是本地缓存（写完 note
  会立刻更新缓存）。
- 父任务没有 OmniFocus 关联（没 pk）时不能派生：note 写不回去，直接报错。
- 派生任务是本地任务，**不**在 OmniFocus 里出现；它进 Focusmate / session 的
  方式和普通任务一样。
- 同一行已经有 active 派生任务时，`-d` 不会再派一条（要重来先放弃旧的）。
- 本地勾完最后一行会**立刻**把「放弃整套」排进 outbox（异步写回）。如果 OmniFocus
  里的 note 被外部改过、`note_mark` 找不到那一行，note 那几行不会勾上，但整套
  仍然会被放弃 —— 这是「勾完就收工」的代价，写回失败会在 outbox 里留着重试。
- 放弃整套是**真写 OmniFocus**（`drop --all-occurrences`）；OmniFocus 关着时
  只停本地，下次不会重试（outbox 里没有东西）。

## 测试

- `test/test_derived.py`：note 解析 / 勾选 / 资格 / 合成 pk / count 范围。
- `test/test_derived_warden.py`：`-d`、`-ac` 拒绝、完成勾 note、勾完放弃、
  parallel/sequential 展开、轮转、distribute、batch note_mark 脚本。
