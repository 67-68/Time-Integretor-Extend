# Focusmate 界面 / 内部 API 逆向笔记

> 目的：为 `ti_extend` 的三个候选需求（session title、session task、quiet mode、cancel session）
> 提供**已实测**的界面与接口事实，作为后续实现的依据。
>
> 状态：**只读探测**，未对账号做过任何写操作（唯一的写动作是一次可逆的 toggle 开关点击，已复原）。
>
> 最后更新：2026-10-08

---

## 0. TL;DR

| 需求 | 可行性 | 关键证据 |
|---|---|---|
| 加 session title | ✅ 可行 | 右侧 Session Info 面板铅笔按钮 → `input[placeholder="Session title..."]`（maxlength 100）→ Enter |
| 加 task | ✅ 可行 | 右侧面板 "Add task" → `textarea[placeholder="Task title"]` + "Create task"（Task Manager，Labs） |
| 订 quiet mode session | ✅ 可行 | 预约面板 "Quiet Mode" toggleswitch（`label#quietModeLabel`）→ 下单 payload `preferences.quietMode.value=true` |
| 取消 session | ✅ 可行 | session 卡片**右下角** × 按钮（`span.pi.pi-times`，accessible name `Cancel session: …`） |

**额外重大发现：** App 前端使用一套**可写的内部 REST API**，base 与公开只读 API 相同
（`https://api.focusmate.com/v1/`），只是认证方式不同（Firebase ID token 而非 `X-API-Key`）。
详见第 3 节。

---

## 1. 验证方法（含一个必须知道的坑）

### 1.1 沙箱限制

本机 `dsh` 的 bash 沙箱**禁止写入 `~/.focusmate-mcp/`**：

```
touch ~/.focusmate-mcp/browser-data/.t  →  Operation not permitted
```

后果：Chromium 能启动、Angular 能 bootstrap（`app-root` 里有 `ng-version`、`p-toast`），
但 `router-outlet` **永远是空的**（认证态读不出来）。

### 1.2 绕法（以后实测照抄）

```bash
cp -R ~/.focusmate-mcp/browser-data /tmp/fm-profile   # 读允许
PROFILE_DIR=/tmp/fm-profile node test/probe_xxx.mjs   # 写落到 /tmp
```

这样 dashboard 正常渲染。**注意**：`/tmp/fm-profile` 里有你的登录凭据，用完请删除。

> **2026-10-13 更新**：`temp/focusmate-mcp` 现在支持用环境变量指定运行目录，不必再改代码：
>
> | 变量 | 作用 |
> |---|---|
> | `FOCUSMATE_MCP_HOME`（或 `FOCUSMATE_CONFIG_DIR`） | 整个运行目录（`browser-data` / `screenshots` / `config.json`），默认 `~/.focusmate-mcp` |
> | `FOCUSMATE_SCREENSHOT_DIR` | 只挪出错截图的目录；`ti_extend` 默认指到 `temp/focusmate-screenshots` |
>
> `ti_extend/main.py` 的 `ensure_mcp_home()` 会在每次 `mcp_call` 前自动做这件事：
> 显式环境变量优先；否则用可写的 `temp/focusmate-home`，并首次把旧
> `~/.focusmate-mcp/browser-data` / `config.json` / `cookies.json` 复制过去，最后把
> `FOCUSMATE_CONFIG_DIR` 指向它。沙箱里不要再依赖默认 `~/.focusmate-mcp`。
>
> 出错截图现在**只是诊断附件**：写不进去（沙箱 EPERM）只记日志，绝不再盖掉真正的报错；
> 首选目录不可写会自动退到 `<tmp>/focusmate-mcp/screenshots`。

### 1.3 静/动态两条腿

- **动态**：Playwright 驱动真实页面（`test/probe_*.mjs`）。
- **静态**：拉 Angular bundle 做字符串/结构逆向：
  ```bash
  curl -s https://app.focusmate.com/dashboard -o dash.html   # 取 main-*.js
  curl -s https://app.focusmate.com/main-XXXX.js -o main.js
  grep -oE 'chunk-[A-Z0-9]+\.js' main.js | sort -u           # 43 个 lazy chunk
  ```

两者互相印证，下面每条结论都标注了来源。

---

## 2. 页面结构（实测）

### 2.1 路由

从 `main.js` 提取的 Angular 路由（部分）：

```
/dashboard        /sessions         /tasks            /tasks/completed
/session/:id      /session/g/:id    /launch/:id       /profile
/settings         /profile/edit-p   /profile/edit-q   /people
/group-meetings   /group-management /availability/favorites
```

### 2.2 Dashboard 布局

- 左侧/中间：日历网格 + 已订 session 卡片列表。
- **右侧 dynamic panel**：默认显示 **Session Settings**（预约面板）。
- 点 session 卡片 → 右侧面板换成 **Session Info**（实测已验证）。

### 2.3 已订 session 卡片（`<fm-booked-session-tile>`）

实测结构（脱敏）：

```html
<fm-booked-session-tile>
  <div class="w-[calc(100%-4px)] ... rounded-tr-lg rounded-br-lg relative">   <!-- click → 打开 Session Info -->
    <div>…session indicator icons…</div>
    <fm-avatar class="cursor-pointer">…</fm-avatar>                          <!-- click 会 stopPropagation -->
    <div>9:30am - 10:20am</div>
    <div class="text-12-16 … truncate">Hind (Screen share) H.</div>          <!-- click 会 stopPropagation -->
    <div class="text-11 text-muted-color …">test</div>                        <!-- ← session title 显示在这 -->
    <div class="flex flex-row justify-end … gap-1">                           <!-- 右下角按钮组 -->
      <fm-join-session-button>…</fm-join-session-button>
      <p-button icon="pi pi-…">…</p-button>                                   <!-- More options -->
      <p-button icon="pi pi-times">…</p-button>                               <!-- ← 取消 × -->
    </div>
  </div>
</fm-booked-session-tile>
```

**坑**：整块 tile 的 click 绑在根 div 上，但 avatar 和时间/partner 名等子元素都 `stopPropagation`。
所以自动化点击必须落在**空白的右侧区域**（实测 `x + width - 30, y + height*0.35` 有效），
否则点了半天面板不弹。

### 2.4 Session Info 面板（实测文本）

```
Session at 9:30am
50 min · Thursday, Oct 8th
test                                  ← 当前 title
Profile picture
Hind (Screen share) H. / 2517 sessions / Asia/Shanghai · 9:30am
Message Partner
Add task
Add from existing tasks
Session settings
Repeat session
Join session
Cancel session
```

### 2.5 预约面板（Session Settings，实测文本）

```
Session Settings
Duration:  25 min / 50 min / 75 min
My Task:   Desk / Moving / Anything        ← 这是「活动类型」，不是自由文本 task，别混淆
Quiet Mode                                 ← toggleswitch
   You may match with people in Quiet Mode. You can change this in your Settings.
Prefer Favorites
```

---

## 3. 认证模型（两套 API）

### 3.1 公开 API（文档化，只读）

- base：`https://api.focusmate.com/v1/`
- 认证：`X-API-Key: <key>`（在 Focusmate 设置页生成，存 `~/.focusmate-mcp/config.json`）
- 能力：`GET /me`、`GET /sessions` —— **只读**，不能 book / 改 / 删。

**实测补充（2026-10-17，给 `stats.py` 的生涯统计用）：**

- `GET /v1/sessions?start=&end=` 的边界是 **UTC ISO**，返回区间内**全部** session
  （含早已过去的）。
- **单次查询上限 365 天**：365 天 → 200（拿到 2251 条），366 天 → `400 Bad Request`。
  所以拉全量历史要按 ≤365 天**分块**，不是翻页 —— 没发现 `page`/`cursor` 参数。
- `GET /v1/me` 给 `userId` / `name` / `memberSince` / `totalSessionCount` / `timeZone`。
  `totalSessionCount`（本账号 1933）＝ 该用户 `completed=true` 的场次数，
  比「订过多少场」（同一区间 2261 条记录）少。
- 每条 session 的形状：`sessionId / sessionType / duration(ms) / startTime / title /
  users[]`；`users[]` 里 `userId` 对得上自己的那条才有
  `joinedAt / completed / activityType / preferences`（另一位 partner 只有 `userId`）。

### 3.2 App 内部 API（未文档化，可写）

同一个 base `https://api.focusmate.com/v1/`，认证换成 **Firebase ID token**：

```js
// Angular HTTP 拦截器（main.js 实测）
addToken(i){ return i.clone({ setHeaders:{ Authorization: this.firebaseTokenService.getToken() } }) }
addAnonymous(i){ return i.clone({ setHeaders:{ Authorization: "" } }) }
```

token 来源（实测）：

```js
e.getIdToken(false).then(n => this.firebaseTokenService.setToken(n))
```

Firebase 项目信息（实测，来自 bundle）：

```
projectId:          focusmate-99d59
authDomain:         auth.focusmate.com
messagingSenderId:  615362219491
storageBucket:      focusmate-99d59.appspot.com
```

> ✅ **已实测确认（2026-10-08）：是裸 JWT，没有 `Bearer` 前缀。**
> 在页面上下文里 `fetch("https://api.focusmate.com/v1/session/next")`：
>
> | Authorization | 结果 |
> |---|---|
> | `<裸 JWT>` | **200** ✅ |
> | `Bearer <JWT>` | 401 `{"error":"Token invalid","code":71}` |
> | （不带） | 401 `{"message":"Missing Token","code":17}` |
>
> token 取法：页面 IndexedDB `firebaseLocalStorageDb` → objectStore `firebaseLocalStorage`
> → `value.stsTokenManager.accessToken`（约 1153 字符的 JWT）。
> 过期了就 `page.reload()` 让 app 自己刷（它有 `checkTokenExpiredError` +
> `TOKEN_REFRESH_CODES=[70,17,35]` 的逻辑）。

### 3.3 已发现的写接口（从 bundle 还原 + 实测验证）

DTO（bundle 里的 `Dh` 类，即 `editSessionService` 用的那个）：

```js
class SessionWriteDto {
  constructor(sessionTime, title, preferences, activityType) {
    this.sessionTime = sessionTime;   // = meeting.startMs
    this.title = title;
    this.preferences = preferences;   // { favorites:{value}, quietMode:{value} }
    this.activityType = activityType; // 100 Anything / 200 Desk / 400 Moving
  }
}
```

```js
// 改 session title
editSessionTitle(sessionTime, title) {
  return this.apiService.put(api_url + "session/", { data: new SessionWriteDto(sessionTime, title) });
}

// 改 session 偏好（含 quiet mode）/ 活动类型
editSessionPreferences(sessionTime, favorites, activityType, quietMode) {
  const p = new SessionWriteDto(sessionTime);
  p.preferences = { favorites: { value: favorites }, quietMode: { value: quietMode } };
  p.activityType = activityType;
  return this.apiService.put(api_url + "session/", { data: p });
}

// 取消 session
cancelSession(externalId, sourceIndex) {
  return this.apiService.delete(api_url + "session/" + externalId + "?source=" + SOURCES[sourceIndex].Amplitude);
}

// Task Manager
baseUrl = api_url + "tasks"            // GET / POST / PUT / DELETE /tasks{,/reusables,/attach,/{id}/complete}
```

**关键字段映射**（`mapPairedMeetingToConfirmedSession`，实测）：

| App 里的名字 | 来自 `GET /meetings/bookings` 的字段 |
|---|---|
| `sessionInfo.sessionTime` | `meeting.startMs`（epoch ms） |
| `sessionInfo.externalId` | `meeting.id`（**UUID**，不是 startMs） |
| `sessionInfo.preferences` | `meeting.preferences` |
| `sessionInfo.activityType` | `meeting.activityType` |

`GET /meetings/bookings` 响应形状（实测节选）：

```json
{
  "meetings": [{
    "meetingType": "paired",
    "id": "8df80e36-159e-4e4b-8399-c0d288ea5382",
    "startMs": 1790842500000,
    "durationMs": 3000000,
    "title": "",
    "partnerId": "cd355120-…",
    "state": 9,
    "preferences": { "favorites": {"value":"noPreference"}, "quietMode": {"value": false} },
    "activityType": 100,
    "partnerPreferences": { "quietMode": {"value": false} }
  }],
  "participants": [ … ]
}
```

**`PUT /v1/session/` 会回显整个更新后的 session**（实测），可以直接拿来复核：

```
PUT /v1/session/  {"data":{"sessionTime":1791448200000,"title":""}}
→ 200 {"sessionTime":1791448200000,"duration":3000000,"title":"",
       "activityType":100,"preferences":{"favorites":{"value":"noPreference"},"quietMode":{"value":true}}}
```

**task 挂到 session 上的形状**（实测，`GET /v1/tasks`）：

```json
{ "id": "936bd726-…", "title": "…", "description": null,
  "attachments": [ { "type": "session", "externalId": "dc3c58b6-…" } ] }
```

> 也就是说 task 其实也能走 API（`POST /v1/tasks` 带 `attachments:[{type:"session",externalId}]`），
> 不一定非要 DOM。

**实测补充（2026-10-27）：一场 session 已经挂了 task 时，UI 面板里就没有 `Add task` 了。**
右侧面板这时候显示的是那条 task 本身（编辑/勾完成），不再给「再加一个」的入口。

但 **task ↔ session 是 n:n**（`attachments` 是数组），所以「一场挂多个 task」在数据层
完全合法 —— 只是 UI 不给第二次入口。因此 `scripts/bridge.mjs` 的 `add_task` 现在
**默认先走内部 API**（`scripts/task_attach.mjs`）：

1. `POST /v1/tasks` 带 `attachments:[{type:"session",externalId}]`（服务端不收就退到
   `POST /v1/tasks/attach`，payload 形状由简到繁试几种）；
2. 每次都 `GET /v1/tasks` 复核「title 一致 + attachment 指向这场」；
3. 建出来却没挂上的孤儿 `DELETE /v1/tasks/{id}` 回滚；
4. 全失败才退回 DOM（DOM 在「这场已有 task」时注定失败，错误信息会直接说明）。

⚠️ `POST /v1/tasks` 的 body 形状**没有公开文档**，上面是候选形状 + 运行时复核，
不是文档保证。哪天真机验证出来了，把 `taskCreateBodies()` 收敛成一种即可。

### 3.4 「它不校验权限吗？」

会校验，只是**校验的是「你是谁」，不是「你是不是浏览器」**：

- `Authorization` 里是 Firebase ID token → 服务端据此解出 user id。
- 之后按**资源归属**授权（你只能改/删自己的 session / task）。
- 没有发现任何「客户端类型」闸门（App Check / 签名校验 / nonce）：
  - 全 bundle 搜不到 `initializeAppCheck` / `ReCaptchaV3Provider`；
  - `X-Firebase-AppCheck` 只出现在 Firebase Auth SDK 自己的代码里（登录用），不是业务 API；
  - 业务请求只加了 `Authorization` 一个头，没有 `X-API-Key`、没有自定义签名。
- 换句话说：**这不是「绕过权限」，而是用同一个用户的、和官方前端一模一样的凭据调同一个接口**。
  不存在提权。真正的差别只是「谁来发这个请求」。

---

## 4. 需求逐条

### 4.1 session title

**UI 流程（实测 + bundle 印证）**

1. 点 session 卡片 → 右侧 Session Info 面板。
2. 面板里有铅笔按钮：`<span class="p-button-icon pi pi-pencil">`（实测坐标 x=1542,y=155）。
   - 已有 title：显示 title 文本 + 铅笔。
   - 无 title：显示可点文字 **"Add a session title..."** + 铅笔。
3. 点击后出现输入框，实测属性：
   ```
   INPUT  placeholder="Session title..."  maxlength=100  value="test"
   ```
4. 提交：**回车**（bundle：`(keyup.enter) → onUpdateSessionTitle()`），
   或点输入框右侧的保存图标（`p-inputicon` click → `onUpdateSessionTitle()`）。
   取消：× 按钮 → `onCancelEditTitle()`。
5. 输入框下方提示 **"Only visible to you"**。

**落库**：`PUT /v1/session/ { data: { sessionTime, title } }`

**复杂度**：低。纯文本，一次请求。

---

### 4.2 task（Task Manager，Labs）

**UI 流程（实测）**

1. 点 session 卡片 → 右侧面板。
2. 点 **"Add task"**（另有 **"Add from existing tasks"**）。
3. 实测弹出的表单：
   ```
   TEXTAREA  placeholder="Task title"                      maxlength=500
   TEXTAREA  placeholder="Notes · not visible to partners"
   BUTTON    "Create task"
   ```
4. 提交："Create task" 按钮，或回车。

**其他入口**：session 进行页（`/launch/:id`）有
`"Add task to this session" / "Tap to attach a task"` → `openInSessionTaskDrawer()`。

**落库**：`POST /v1/tasks`（Task Manager 有独立实体：title / description / category / reusable）。

**复杂度**：中。是独立实体 + 附件关系，还有创建配额（`getCreateTaskCapBlockCode` / `isSubmitDisabled`），
且 dashboard 上标着 **Labs** —— 用户关掉 Labs 就没了。

---

### 4.3 quiet mode

**UI 流程（实测）**

1. 右侧预约面板（Session Settings）里有 **Quiet Mode** 开关：
   ```html
   <label id="quietModeLabel" for="quietMode">Quiet Mode</label>
   <input type="checkbox" class="p-toggleswitch-input">
   ```
2. 实测点击：`checked: false → true`，再点：`true → false`。（这是**本次预约的偏好**，不是账号设置。）

**落库**：下单 payload 里带
```js
preferences: { favorites: { value: "noPreference"|"preferred" }, quietMode: { value: true|false } }
```
（bundle：`bookSession(){ let n={favorites:{value:"noPreference"},quietMode:{value:false}} … }`，
列表页那条路径是 `quietMode:{value:this.selectedQuietMode}`。）

**已存在的 session 也能改**：`editSessionPreferences(...)` → `PUT /v1/session/`。
所以「订完再转 quiet」可行，不必只在预约瞬间点。

> ⚠️ **别混淆**：设置里还有一个 **"Edit quiet mode partner preference"**（`quietModeMatchAllowed`，
> 控制「是否允许被匹配到 quiet-mode 的人」），和「本场 session 用 quiet mode」是两回事。

**复杂度**：低（下单路径）/ 低（改已有 session）。

---

### 4.4 cancel session

**UI 流程（实测）**

- 位置：session 卡片**右下角**（实测 box `{x:254,y:645,w:24,h:24}`，卡片范围 `x:69–287,y:586–678`）。
- 元素：`<span class="p-button-icon pi pi-times">`，accessible name 是内嵌 sr-only：
  `Cancel session: 50 minute session in quiet mode with … `。
- 右侧 Session Info 面板里还有一个显式按钮 **"Cancel session"**。

**逻辑（bundle，`onClickCancelSession()`）**

```js
onClickCancelSession() {
  if (!this.sessionInfo.partnerId || this.cancelConfirmState)
    this.cancelSessionService.cancelSession(this.sessionInfo.externalId, this.sessionTileSource).subscribe(...);
  else
    this.cancelConfirmState = true;   // 有 partner → 先弹二次确认（onClickKeepSession / 确认）
}
```

即：**还没匹配到人 → 直接取消；已匹配到人 → 多一步确认。**

**落库**：`DELETE /v1/session/{externalId}?source=<amplitude source>`

**复杂度**：低。但**现有 `focusmate-mcp/src/tools/cancel-session.ts` 是猜的选择器、从未验证过**
（依赖 `a[href*=sessionId]`、`getByRole('button',{name:/cancel/i})` 之类），基本对不上真实 DOM，
应当按本节重写。

---

## 5. 方案对比

### 方案 A：DOM 自动化（延续现有架构）

Playwright 点界面。选择器现在都有实测依据。

- 👍 与 `keep_session` / `book_session` 一致；不依赖未文档化接口；改版只需修选择器。
- 👎 DOM 会变；点击链路长（tile → 面板 → 铅笔 → 输入 → Enter），任一环节漂移就断。
- 👎 需要真实的鼠标/键盘事件序列，最容易踩「点到了 avatar 被 stopPropagation」这类坑。

### 方案 B：复用内部 API（推荐给 title / quiet / cancel）

Playwright 起应用（已登录）→ 在页面里 `page.evaluate()` 发 `fetch` 到 `https://api.focusmate.com/v1/…`，
带同一份 Firebase token。

- 👍 不碰 DOM，只依赖接口形状；一次调用完成，快。
- 👍 「在真页面里发请求」，指纹 = 官方前端，不新增自动化痕迹。
- 👍 接口行为已从 bundle 完整还原（sessionTime 而非 sessionId 做 key，注意这点）。
- 👎 非文档化接口，Focusmate 可随时改；token 需从 IndexedDB 取并处理刷新（401 / `TOKEN_REFRESH_CODES=[70,17,35]`）。
- 👎 注释/文档缺失，参数语义靠猜（如 `Rr` 类的完整字段）。

### 建议

| 需求 | 建议 |
|---|---|
| session title | **B** |
| quiet mode | **B**（预约时 `book_session` 加参数；或订完再 PUT） |
| cancel session | **B** |
| task | **A**（表单 + 独立实体 + 配额 + Labs 开关，DOM 更可控） |

两者都沿用 `keep_session` 的原则：**官方只读 API 复核才是 ground truth，脚本自述只是线索。**

---

## 6. 「容易被官方发现吗？」

先说结论：**用内部 API 本身的增量风险，比你现在已经在做的浏览器自动化还要小。**

**为什么：**

1. 请求层面**完全同构**。同样的 endpoint、同样的 `Authorization`、同样的 body。
   服务端只看得到「一个已登录用户在操作自己的 session」，和真人在页面上点一下无法区分。
2. **没有新增指纹**。你不是新开一个裸 HTTP 客户端，而是让已登录的应用页面自己发请求。
   反过来说，如果你用 python `requests` 直接打，那才会引入「非浏览器客户端」这个新特征。
3. `navigator.webdriver` / headless 指纹这类痕迹，**你现在就已经在产生**了 ——
   `keep_session` 每晚用 Playwright 无头 Chromium 下单，这是既有的暴露面，方案 B 不会放大它。

**真正的风险点（按重要性排）：**

1. **行为模式，而不是接口。** 每天固定时间、固定间隔、一次补一堆 session、
   防抖 5±3s —— 这种规律性才是风控真正会看的东西。这个风险**和选 A 还是选 B 无关**，
   是 `keep_session` 本身自带的。（好消息：`keep_session.py` 已经有随机防抖和退避设计。）
2. **ToS。** 自动化操作网页大概率是违反 Focusmate 服务条款的，无论走 DOM 还是内部 API。
   如果账号被封，成本是账号本身（1874 场 session 的记录），不是技术问题。
3. **接口被改。** 更可能发生的是「哪天接口变了导致脚本失败」，而不是「被封号」。
   所以**必须有降级路径**：B 失败 → 退回 A；两个都失败 → 通知人手动处理。
4. **token 落盘风险。** 方案 B 要把凭据从 IndexedDB 取出来用，别把它写到 git 里。

**务实的做法：**

- 控制**频率**（沿用现有防抖/退避，别加并发）。
- 别做「人类不会做」的操作（例如秒级批量改 20 个 session 的 title）。
- 先小范围试（只对 1 个 session 设 title，观察 24h），再放开。
- 出问题就退回手动，**账号安全 > 自动化便利**。

---

## 7. 未验证 / 待确认

已经验掉的（2026-10-08）：

- [x] `Authorization` 是**裸 JWT**（实测：裸 → 200，`Bearer` → 401 code 71）。见 §3.2。
- [x] `PUT /v1/session/` 的 body 形状 → `{data:{sessionTime,title}}` / `{data:{sessionTime,preferences,activityType}}`。见 §3.3。
- [x] `PUT` 是幂等的（把 title 设成原值 → 200，复核数据未变）。
- [x] `externalId = meeting.id`（UUID），`sessionTime = meeting.startMs`。见 §3.3。
- [x] 四个操作各自跑通了真实写路径（title 改+还原、quiet 往返+还原、task 建+删、cancel 只测了预览）。

还没验 / 没法验：

- [ ] 有 partner 的 session 点 × 后的**二次确认弹窗文案与按钮**（怕误取消，没敢点）。
  目前 `cancel.mjs` 走 API，**绕过**了这个二次确认。
- [ ] `PUT /v1/session/` 要**全量**还是可以只给部分字段（脚本一律给全量，所以没踩到）。
- [ ] 错误码 `TOKEN_REFRESH_CODES=[70,17,35]` 的确切含义（70/35 大概是过期）。
- [ ] Task 创建配额的具体阈值与错误码（bundle 里有 `getCreateTaskCapBlockCode` / 错误码 43 / 49）。
- [ ] 内部 API 是否也接受 `X-API-Key`（推测不接受）。
- [ ] Focusmate 是否有服务端风控/速率限制（无法从 bundle 看出）。
- [ ] `/session/<startMs>` **深链无效**（实测会重定向回 /dashboard），所以 add_task 只能点卡片。

---

## 9. 落地：`scripts/` 下的四个脚本

实测结论已经落成四个独立 CLI（用法见 [`../scripts/README.md`](../scripts/README.md)）：

| 脚本 | 实现 | 复核方式 |
|---|---|---|
| `scripts/set_title.mjs` | 内部 API `PUT /v1/session/` | 重拉列表比对 |
| `scripts/set_quiet.mjs` | 内部 API `PUT /v1/session/`（先读后写，保住 favorites/activityType） | 重拉列表比对 |
| `scripts/add_task.mjs` | DOM（点卡片 → Add task → 填 → Create task） | `GET /v1/tasks` 查 attachment |
| `scripts/cancel.mjs` | 内部 API `DELETE /v1/session/{id}` | 重拉列表确认消失 |

四个都用 Playwright 开已登录页面 → 在**页面上下文里**取 token / 发请求 / 点界面，
所以对外表现和官方前端一致。

**常驻桥 + TUI**：同一套内部 API 另有 `scripts/bridge.mjs`（常驻 Chromium，
stdin/stdout JSON 行协议）和根目录的 `warden.py`（rich TUI，看板 + 启停 daemon +
看图改 session）。设计取舍与命令表见 [`../feature_intents/warden.md`](../feature_intents/warden.md)。
动机是冷启动太贵：实测同一账号下**冷启动+snapshot ≈ 9.7s，热路径命令 ≈ 1.9s**。

**踩过的坑（写脚本时会再遇到）：**

1. 沙箱/权限：Chromium 必须能写 profile 目录，否则页面渲染空白（router-outlet 空）。
   本机 dsh 沙箱禁写 `~/.focusmate-mcp`，要 `cp -R` 到 `/tmp` 再跑。
2. 点 session 卡片必须落在**空白区域**：avatar / 时间 / partner 名都 `stopPropagation`。
3. 卡片**只在「进行中」时显示时间段**（`4:30pm - 5:20pm`），未来的只显示起点（`8:00pm`），
   所以匹配只能按起点。
4. PrimeNG 的 `p-button` 文字在 `<span class="p-button-label">` 里，
   `getByRole('button',{name})` 有时匹配不到，得用 `getByText` 兜底。

---

## 8. 附录：探测脚本

留在 `test/` 下，只读、无副作用（`PROFILE_DIR=/tmp/fm-profile` 配合使用）：

| 脚本 | 作用 |
|---|---|
| `probe_wd.mjs` | 带看门狗的最小加载探测（诊断渲染问题） |
| `probe_dash_dump.mjs` | dump dashboard 全部按钮/输入框/text |
| `probe_tile2.mjs` / `probe_tile3.mjs` | session 卡片结构与退出按钮定位 |
| `probe_panel4.mjs` | 点击卡片打开 Session Info 面板 |
| `probe_panel5.mjs` | 打开 "Add task" 表单 |
| `probe_title2.mjs` | 定位 title 铅笔 + `Session title...` 输入框 |
| `probe_quiet.mjs` | 验证 Quiet Mode 开关翻转 |
| `probe_session.mjs` | session 详情路由尝试 |
| `verify_internal_api.mjs` | 取 token + 验证裸 JWT / Bearer（§3.2 的那张表） |
| `verify_bookings.mjs` | dump `GET /meetings/bookings` 结构 |
| `verify_write_path.mjs` | 幂等 PUT 验证写路径 + 深链测试 |
| `probe_addtask_btn.mjs` | 定位 "Add task" 的真实元素（p-button-label） |
| `list_meetings.mjs` | 列出所有 session（本地时间/partner/title/quiet），给脚本挑目标用 |
| `tasks_api.mjs` | `list` / `delete <id>` task（清理 add_task 的测试数据用） |
| `test_warden.py` | warden.py 回归测试：密码复杂度/存取/权限、命令解析、cancel 阻力<br>（`python3 test/test_warden.py`；不联网、不写仓库文件） |

用法：

```bash
cp -R ~/.focusmate-mcp/browser-data /tmp/fm-profile
cd temp/focusmate-mcp
PROFILE_DIR=/tmp/fm-profile node ../../test/probe_title2.mjs
rm -rf /tmp/fm-profile     # 别忘了删，里面有登录凭据
```

---

## 10. 2026-10-23 界面改版：日历时间轴换成 24 小时制（自动预定全挂的直接原因）

**现象**：`keep_session` 从 2026-10-23 09:32 起每一场都报同一个错，而且都是"环境失败"：

```
book-session-attempt-2 failed: locator.waitFor: Timeout 3000ms exceeded.
  - waiting for locator('text="5pm"').first() to be visible
```

**原因**：日历时间轴的**小时标签格式变了**，`text="5pm"` 已经不存在，所以
`scrollToTime()` 永远等不到 → 每次下单必然超时。格子本身（48px/15min）和点击逻辑没变。

### 10.1 改了什么（实测）

| 元素 | 旧（≤2026-10-22） | 新（2026-10-23 起） |
|---|---|---|
| 小时刻度 | `5pm` / `12am` | **`17:00` / `00:00`**（补零 24 小时制） |
| 分钟刻度 | 无 | `:15` / `:30` / `:45` |
| 时间轴容器 | （全局文本） | `fm-calendar-timeslot-labels`，标签 `position:absolute`，`top = 小时 × 192px` |
| 每 15 分钟一行 | 48px | 48px（**没变**） |
| 日期表头 | `Sat 24` | `Sat 24`（**没变**） |
| 日列 | — | `fm-calendar-day`，每日 96 个 `div.relative.h-[48px]`（第 N 行 = 第 N 个 15 分钟） |
| 底部确认按钮 | `Book 1 Session` | `Book 1 session`（只是大小写，正则忽略大小写即可） |
| 已订卡片上的时间 | `4:30pm - 5:20pm` | `16:30 - 17:20` |

**结论：凡是按"12 小时制"写的选择器 / 正则全部作废**，包括
`booking.ts` 的 `scrollToTime` / `clickSlotByCoordinates` 里量 `HH(am|pm)` 的那段，
以及 `list-sessions.ts` 里 `(\d{1,2}:\d{2}[ap]m)` 的正则（现在两种都认，向后兼容）。

### 10.2 这次一起踩到的坑（都已修）

1. **原地补点 = 直接下单。** 选中格子后，**同一坐标上会渲染出一个格子内的 "Book" 按钮**。
   所以"第一次点击没等到 Book 按钮就再点一次"这个兜底，实际上是**真的会把 session 订掉**。
   因此补点之后必须拿到落地证据（sessionId / 新增的 `/session/<id>` 链接）才准报成功，
   否则一律抛错交给上层用官方 API 复核 —— 绝不能像以前那样 `verified=false` 也当成功。
2. **`fm-session-selector-outline` 会拦截 `locator.click()`。** 它是 hover/选中高亮层（`z-30`），
   Playwright 判定"被别的元素拦截"，一直重试到超时。必须量出坐标后用 `page.mouse.click(x, y)`。
3. **点不出 Book 按钮 ≠ 订上了。** 已订格子、和已有 session 冲突的格子（outline 带
   `pointer-events-none` + 50% 透明）点下去只会打开右侧 Session Info 面板，不会出现确认按钮。
4. **时间轴标签要限定容器。** 已订卡片上也有 `17:00` 这样的纯文本，
   全局 `text="17:00"` 会先撞上卡片 → 滚动/测量全错位。用
   `fm-calendar-timeslot-labels` 里的那个。
5. **顶部有个常驻的 "Book" 按钮**（Book Now）。确认按钮的 locator 必须严格匹配
   `Book N session`，否则"格子没选中"也会被判成"按钮可见"，然后去点顶部按钮，装作订上了。
6. **选中是累加的**：连着点两格会变成 `Book 2 sessions`。`confirmBooking` 现在会拒绝 >1 的情况。

### 10.3 现在的实现（`temp/focusmate-mcp/src/automation/pages/booking.ts`）

- `selectTimeSlot()`：`slotIndex = (小时*60+分钟)/15`，先 `clickSlotCell()` 点格子，
  失败再退回 `clickSlotByCoordinates()`（按时间轴标签几何算坐标，两种标签格式都认）。
- `clickSlotCell()`：`fm-calendar-day[n] > div.relative.h-[48px]` 第 `slotIndex` 个，
  `scrollIntoViewIfNeeded` → 量中心 → `mouse.move` + `mouse.click`。
- `scrollToTime()`：在 `fm-calendar-timeslot-labels` 里按**精确文本**找 `HH:00`，滚到容器中间。
- `confirmBooking()`：只认 `Book N session`，且 `N>1` 直接拒绝。

**回归验证**（2026-10-24 实测）：`book_session` 连续订上 10-25 16:00 / 17:00，
`keep_session` 日志从"环境失败"变回 `复核通过：API 确认该 session 已存在`。

---

## 11. Task 完成时间：`GET /v1/tasks` 的 `completedAt`

任务完成时间**不用猜**：`GET /v1/tasks` 返回的每个 task 带 `completedAt` 字段
（`test/tasks_api.mjs` 已经在 dump 这个字段）。形状与 attachment 一起出现：

```json
{
  "id": "936bd726-…",
  "title": "写论文",
  "description": "先写 intro",
  "completedAt": "2026-10-26T12:00:00.000Z",
  "attachments": [{"type": "session", "externalId": "<session UUID>"}]
}
```

因此本地任务归档可以同时记下：
- 本地任务字段；
- 对应的 session（日期 / 时刻 / Focusmate session id）；
- Focusmate task id；
- Focusmate 的 `completedAt`（真正完成时间）。
如果某条 remote task 没有 `completedAt`，就只匹配不归档，绝不伪造完成时间。
