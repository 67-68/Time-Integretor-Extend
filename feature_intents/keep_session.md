# keep_session

## 模块职责

保证「今天应该有的 Focusmate session」一定存在。`data.csv` 是唯一的目标来源，
模块负责在 session 开始前发现缺口并自动补订，同时把手动触发能力暴露给使用者。

## 目标来源：data.csv

每行 `HH:MM,duration`，`#` 注释与空行忽略，duration 只允许 25/50/75。
语义是**仅今天**：文件描述的是「作息表」，只对运行当天生效，不做跨天补订。
需要「只订某一天」时另行扩展格式，当前不引入日期维度。

## 调度：LaunchAgent 频率驱动

调度器只做一件事——按固定频率（15 分钟，与 Focusmate 的 15 分钟 slot 粒度对齐）
唤醒 `keep_session.py check`。调度器**不做任何时间计算**，所有判定都在 `check` 内部，
因为 launchd/cron 的休眠行为不可控，把逻辑放在脚本里才能在漏跑后自愈。

## 检查：窗口 + 幂等

- 窗口：`[T - CHECK_DDL_BEFORE_SESSION, T)`。到点即停止补救，避免订到已开始或过去的 slot。
- 幂等：每次都重新拿「当前是否有 slot 命中窗口」来判定，不依赖上一次是否跑过。
- 存在性检查走官方只读 API（`GET /v1/sessions`），不拉起浏览器，快且无副作用。
- 只有确认不存在时才走预定，预定必须经过 focusmate-mcp 的浏览器自动化
  （Focusmate 官方 API 是只读的，没有下单接口，这是物理约束，不是选择）。

## 状态与失败退避

- 状态文件只记当天：`{date, slots:{HH:MM: "booked"|"failed"}}`。日期不符即整体重置，
  这是「仅今天」语义的实现方式，不需要额外的清理任务。
- `booked` = 确认存在（自己订的，或本来就有的），后续不再动作。
- `failed` = 当天该目标已尝试且失败，**不再重试**。这是刻意的：Focusmate 对
  异常下单行为敏感，反复重试有风控风险。代价是当天可能漏一个 session，
  这个代价由「立即弹系统通知」来兜底，把决策权交还给使用者。

## 手动入口

- `check`：跑一次检查，也是 LaunchAgent 实际调用的命令。
- `check --dry-run`：只打印会做什么，不调 API 不预定，用于验证配置。
- `install` / `uninstall`：管理 LaunchAgent（写 plist + launchctl load/unload）。
  install 只加载不触发，避免装完立刻误订。
- `status`：查看 launchd 加载状态、data.csv 解析结果、今日 state、日志尾部。

## 环境约束

LaunchAgent 的运行环境极简，所有路径必须绝对化：node 在 `/opt/homebrew/bin`，
python 用系统自带 `/usr/bin/python3`（版本固定、不受 homebrew 升级影响），
PATH 通过 plist 的 EnvironmentVariables 注入。
