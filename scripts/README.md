# scripts/ — session 元数据操作

四个独立脚本，一个脚本干一件事（对齐 `main.py` / `keep_session.py` 那种 CLI 风格）。
全部基于 **Playwright 驱动已登录的 Focusmate 页面**，复用 focusmate-mcp 的持久化 profile，
不需要重新登录。

背景与全部实测依据见 [`../docs/focusmate-ui-api-notes.md`](../docs/focusmate-ui-api-notes.md)。

| 脚本 | 干什么 | 实现方式 |
|---|---|---|
| `set_title.mjs` | 给 session 设标题 | 内部 API `PUT /v1/session/` |
| `set_quiet.mjs` | 开/关 session 的 quiet mode | 内部 API `PUT /v1/session/` |
| `add_task.mjs` | 给 session 挂一个 task | DOM 自动化（点界面） |
| `cancel.mjs` | 取消 session | 内部 API `DELETE /v1/session/{id}` |

`_lib.mjs` 是共用底座（起浏览器、取 token、调内部 API、按时间找 session、CLI 解析）。

---

## 前置

1. **已登录**：`~/.focusmate-mcp/browser-data` 里有登录态。
   没登录就先 `python3 main.py --auth`。
2. **别和 daemon / MCP 并发跑**：它们共用同一个 Chromium profile，会抢锁。
   （跑之前 `python3 keep_session.py daemon stop`，或者接受偶发失败。）
3. `playwright` 能解析到（优先用 `temp/focusmate-mcp/node_modules`，兜底全局）。

## 时间参数

所有脚本都用 `--start HH:MM` 指定目标 session，语义与 `main.py` 一致：

- 24 小时制，必须是 15 分钟整点（`:00/:15/:30/:45`）；
- 默认指**今天**，若该时刻已过则自动顺延到**明天**；
- 可用 `--date YYYY-MM-DD` 指定某天，避免自动顺延。

> Focusmate 内部用 `startMs`（epoch 毫秒）当 session key，脚本按这个精确匹配。
> 匹配不到时会把手上已有的 session 列出来给你看。

公共开关：`--dry-run`（只打印要发的请求，不开浏览器）、`--headed`（弹出窗口看过程）。

---

## 1. set_title — 设置 session 标题

```bash
node scripts/set_title.mjs --start 18:00 --title "写论文"
node scripts/set_title.mjs --start 18:00 --title ""            # 清空
node scripts/set_title.mjs --start 09:00 --title "SAT" --date 2026-10-09
```

- 标题最长 **100** 字符（和网页输入框 `maxlength=100` 一致）。
- 标题 **只有自己能看到**（页面上标着 "Only visible to you"），不影响 partner。
- 写完后会重新拉一次列表复核，返回 `RESULT_JSON {ok, verified, ...}`。

## 2. set_quiet — 开/关 quiet mode

```bash
node scripts/set_quiet.mjs --start 18:00          # 开
node scripts/set_quiet.mjs --start 18:00 --off    # 关
```

- `quietMode` / `favorites` / `activityType` 在 API 里是**一组**，PUT 会整体覆盖，
  所以脚本先读出当前的 `favorites` + `activityType` 再原样带回去，不会把它们冲掉。
- 注意：设置页里那个 **"Edit quiet mode partner preference"** 是另一回事
  （控制「要不要被匹配到 quiet-mode 的人」），本脚本不动它。

## 3. add_task — 给 session 挂 task

```bash
node scripts/add_task.mjs --start 18:00 --title "写论文"
node scripts/add_task.mjs --start 18:00 --title "写论文" --notes "先写 intro"
```

- 走 DOM：点卡片 → 右侧 Session Info 面板 → `Add task` → 填 `Task title`
  （和 `Notes · not visible to partners`）→ 点 `Create task`。
- **安全闸**：点开面板后会先确认面板上写着 `Session at <目标时间>`，
  对不上就直接中止，不做任何写操作。
- 复核走 API（`GET /v1/tasks`），确认 task 真的挂到了这一场。
- 依赖 **Task Manager（Labs）** 开着；关掉按钮就没了。

## 4. cancel — 取消 session

```bash
node scripts/cancel.mjs --start 18:00           # 只看会取消哪一场，不动
node scripts/cancel.mjs --start 18:00 --yes     # 真取消
```

- ⚠️ **破坏性操作**：不加 `--yes` 只预览（退出码 3）。
- 已匹配到 partner 的 session 网页上会多一步二次确认；API 没有这一步，直接取消。
- `--source <枚举>` 是埋点参数，默认 `nowSessionsSidebar`，可选值见 `_lib.mjs` 的 `CANCEL_SOURCES`。

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（且复核一致） |
| 1 | 失败 / 复核不一致 |
| 2 | 参数错 |
| 3 | `cancel.mjs` 没给 `--yes`（预览模式） |

每个脚本最后都会打一行 `RESULT_JSON {...}`，方便上层（agent / shell）解析。

---

## 内部 API 备忘（实测）

- base：`https://api.focusmate.com/v1/`
- 鉴权：**`Authorization: <裸 Firebase ID token>`** —— 注意**没有 `Bearer` 前缀**。
  实测裸 JWT → 200；`Bearer <jwt>` → 401 `Token invalid (code 71)`。
- token 取法：页面 IndexedDB `firebaseLocalStorageDb` → `firebaseLocalStorage`
  → `value.stsTokenManager.accessToken`。过期就 reload 让 app 自己刷。
- 用到的接口：

| 接口 | 用途 |
|---|---|
| `GET /meetings/bookings` | 列已订 session（含 `startMs` / `id` / `preferences` / `activityType`） |
| `PUT /session/` `{data:{sessionTime,title}}` | 改标题 |
| `PUT /session/` `{data:{sessionTime,preferences:{favorites:{value},quietMode:{value}},activityType}}` | 改偏好 |
| `DELETE /session/{id}?source=<枚举>` | 取消 |
| `GET /tasks` / `DELETE /tasks/{id}` | 列 / 删 task |

---

## 注意

- 这是**未文档化的内部接口 + 浏览器自动化**，Focusmate 改版就会失效。
  真失效了先回退成手动操作，别硬扛。
- 别加并发、别缩短间隔。账号安全 > 自动化便利。
- 与 MCP 共用 profile，跑完浏览器会自动关；异常退出可能留锁，
  清掉 `~/.focusmate-mcp/browser-data/SingletonLock` 再试。
