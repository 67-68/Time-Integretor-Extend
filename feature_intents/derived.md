# 派生清单任务（`@list`）

**状态**：v2 已实现（2026-11-15，记号从 `# titles` 改成 `@list`）。
相关代码：`derived.py`（纯逻辑）、
`warden_core/derived_mixin.py`（命令 / 勾 note / 收尾）、`project_plan.py` +
`project_store.py`（plan 展开与轮转）、`omnifocus_tasks.py`（batch `note_mark`）。

## 这是什么

任何一条任务，只要**带上 `@list` 记号**——写在 note 的第一行非空行，或者写在
**任务标题最前面**（`@list 完成历史 Papers`）都行，两者等价。note 里写记号时：

```markdown
@list
- 2019-亚太-12
- 2019-亚太-22
- 2019-亚太-32
```

那它就不是一条任务，而是一份**一次做一条的清单**：每一行标题都可以变成一条
**派生任务**（本地任务，不写回 OmniFocus）；做完一条就把 note 里那行勾成
`- [x] …`，而不是完成原来的任务；整份清单勾完之后**自动收掉容器** ——
重复任务 `drop --all-occurrences`（放弃整套），普通任务标完成（都走 outbox）。

**不需要它是重复任务**——`@list` 记号就是全部条件；是不是重复任务只影响勾完之后
怎么收尾。旧写法 `# titles`（markdown 标题段）继续认，当作别名。

用户原话：「这个重复任务不是普通的重复任务，每次完成一个子任务之后更新原本的
重复任务而不是完成原本的重复任务；在勾完之后自动放弃一整个重复任务。」
「不需要是（或者强制不能是）重复任务，标题前面加上这个记号就行。」

## 判定（`derived.eligible(note, title)`）

两个条件同时满足：

1. 有清单记号（大小写不敏感，后面可以跟说明，例如 `@list 历史盲抽`）：
   * note 的**第一行非空行**是 `@list`；或
   * **任务标题**以 `@list` 开头（此时 note 里不用再写记号，整份 note 就是清单）；
   * 旧写法 `# titles` 段也算；
2. 清单里至少一行**未勾选** bullet。

`- ` / `* ` / `+ ` 都认；`[x]` / `[X]` 算已勾选，`[ ]` 或没有 checkbox 算未勾选。
清单段到下一个 markdown 标题为止（清单后面还能写别的段落）；段外的 bullet 不算。
标题带记号时，派生任务标题里的 `@list` 会被去掉（`@list 完成历史 Papers` →
`完成历史 Papers：这一行`）。

**不看重复规则**：每天重复、每周重复、根本不重复都认。

## 重复任务的 `-d`：复制一条和本体一样的任务

note 里**没有**清单、但任务本身是**重复任务**时，`-d` 走另一条路（用户要求：
「对于重复任务也加上这个 -d 的设计，但他们 derive 出来的任务和他们自己一样且
没有 derive 上限」）：

- 复制出来的任务和本体**一样**：同标题、同 count、同时长、同项目、同 tags；
- **没有数量上限**：`-d` 敲几次就复制几条（合成 pk `<父 pk>#c<序号>`，从 1 起）；
- 副本是**一次性的本地任务**：`repeat=None`、`omnifocus=None`，不写回 OmniFocus，
  不会推动重复表，也不参与父任务 note 的勾选 / 收尾；
- 分不清「清单」和「重复」时按清单优先：带 `@list` 的重复任务仍然派发下一条标题。

副本和清单派生一样会选中并尽量挂到最近的 session，所以在契约表里就是一条普通
任务，可以 `task N -ac`、记账、完成。

## 展示

- **任务格子 / session 表**：父任务的 count 显示成范围 `(0/25-125)`，上界 =
  **所有剩余派生条目的加和**（每条派生继承父任务的 count；不含本体）。
  例如父任务 count=25、还剩 5 条未勾 → `25-125`。全部勾完 / 不是这种任务时
  回到单个数字。
- **项目表（契约视图下半截的 Pro 行）**：`剩余 count` 和 `进度` 也按同一个口径
  放大 —— `stats()` 把清单容器算成「本体 .. 未勾条目 × 本体」，所以一条
  count=3、还剩 2 条未勾的清单，项目行显示 `剩余 3-6`、`进度 0/3-6`
  （表脚注的合计也一样）。`pro` / `op` / `project N` 的说明行同样用这个范围。
- 派生任务自己的标题是 `父任务：这一行的标题`，在任务面板里和普通任务一样
  挂 session、记 count、进 Focusmate。

## 命令

| 命令 | 作用 |
| --- | --- |
| `task <序号> -d` | 清单任务：把**下一条未勾选**标题变成派生任务；重复任务：复制一条和本体一样的一次性任务（无上限）。默认挂到最近的 session（进行中的场优先，其次下一场未开始的） |
| `task <序号> -d 2` | 指定第 2 条未勾选（或 note 里第 N 行） |
| `task <序号> -d -ac` | 派生后按 `-ac` 挂到当前这一场；`-act` / `-n` / `-c` 等后缀同理 |
| `task <序号> -ac` | **被拒绝**（只对 `@list` 容器）：这种任务要先 `-d`，再挂派生出来的那条 |

连续的 `-d` 会依次拿到 12、22、32：已经在做（active）的行不会重复派生。
派生任务不建 OmniFocus 关联，也不写 due；唯一的写回是父任务的 note。

## 完成 → 勾 note → 整份清单完了就收尾

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
4. `note_mark` 写回成功、并且清单里一条未勾的都不剩 → 自动收容器：
   * 容器**有重复规则** → `drop --all-occurrences`（outbox 里再排一条 `drop`），
     本地父任务和所有还活着的派生任务一起标成 dropped
     （`status_reason=derived-done`）；
   * 容器**不重复** → outbox 排一条 `complete`，本地父任务标 completed。

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
- 同一行已经有 active 派生任务时，`-d` 不会再派一条（要重来先放弃旧的）；但
  **重复任务的副本没有这个限制**（每次都是新的一条 `#c<序号>`）。
- 本地勾完最后一行会**立刻**把收尾动作排进 outbox（异步写回）。如果 OmniFocus
  里的 note 被外部改过、`note_mark` 找不到那一行，note 那几行不会勾上，但容器
  仍然会被收掉 —— 这是「勾完就收工」的代价，写回失败会在 outbox 里留着重试。
- 收尾是**真写 OmniFocus**（重复任务 `drop --all-occurrences`、普通任务
  `complete`）；OmniFocus 关着时只停本地，下次不会重试（outbox 里没有东西）。
- 清单记号只认**第一行非空行**：`@list` 前面不能有标题/说明（空白行没事）。
  旧 note 里的 `# titles` 不用改，能被当成别名读出来。

## 测试

- `test/test_derived.py`：note 解析 / 标题记号 / 勾选 / 资格 / 合成 pk / count 范围。
- `test/test_derived_warden.py`：`-d`、`-ac` 拒绝、完成勾 note、勾完放弃、
  parallel/sequential 展开、轮转、distribute、batch note_mark 脚本。
