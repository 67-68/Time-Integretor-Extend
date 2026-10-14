# app/ —— 把 warden TUI 封装成「可前台唤起的 macOS 应用」

同一份 `warden.py`，三种「应用感」。三者都只是**包装**，不碰 `warden.py` 的业务逻辑；
删掉 `app/` 目录对主程序没有任何影响。

| 方式 | 长什么样 | 前台唤起 | 入口 |
|---|---|---|---|
| **`Warden.app`** | Dock / ⌘-Tab / Spotlight 里的 `Warden` | ✅ 找到就聚焦 + refresh，找不到就新建 | `WardenMain.swift` |
| `Warden.command` | 开一个终端窗口跑 TUI | ❌ 每次都是新窗口（它没有 app 身份） | 双击 |
| `warden-web.sh` | 浏览器里的网页终端 | ⚠️ 借 Chrome 的窗口，且要 ttyd 常驻 | `brew install ttyd` |

日常用 `Warden.app`；`Warden.command` 留作「我就是要一个新窗口」和排障入口。

---

## 架构：两层，各管一件事

```
Warden.app (WardenMain.swift, 真 app, 常驻)
   │  只管「什么时候该唤起」：
   │    applicationDidFinishLaunching / applicationDidBecomeActive / applicationShouldHandleReopen
   │
   └─ 子进程 ──> /usr/bin/osascript app/warden-summon.applescript
                    │  只管「唤起成什么样」：
                    │    读 app/.warden-window 里的 iTerm session id
                    │    → 在 iTerm 里找到它 → activate + select window/tab/session → write text "refresh"
                    │    → 找不到 → 新建窗口跑 ./app/Warden.command，把新 id 写回去
                    └──> iTerm2
```

**为什么分两层**：窗口逻辑必须能操纵 iTerm，只有 AppleScript 干得了；但
「什么时候唤起」AppleScript applet 干不了 —— applet 只支持
`run / open / reopen / idle / quit`，**没有 `activate`**。于是「点 Dock」（发 reopen）能用、
「⌘-Tab 切过来」（只发 activate）就什么都不做，只剩一个空窗口挂最前面。
换成真 app 才有 `applicationDidBecomeActive`，两条路才终于是一条路。

顺带好处：`warden-summon.applescript` 能单独跑，调试不用经过 app：

```bash
osascript app/warden-summon.applescript --dry-run    # 只说会干什么，绝不碰 iTerm
osascript app/warden-summon.applescript --new        # 无视已有窗口，强制新建
osascript app/warden-summon.applescript --no-refresh # 只聚焦，不打 refresh
osascript app/warden-summon.applescript --log        # 直接看日志
```

## 唤起语义

```
（没开）        → 新建 iTerm2 窗口跑 ./app/Warden.command，把新 session id 写进 app/.warden-window
（已经开着）    → activate iTerm2 + select 到那个 window/tab/session
                + 往它的 stdin 打一行 refresh（TUI 会像你手敲一样执行，顺手刷新看板）
（窗口被你关了）→ 下次唤起当「没开」处理，新建一个，覆盖 id
```

> `refresh` 在 warden 的**罪案区**会被拒（那是全局锁死，除宣言外什么命令都不受理）——
> 这是 `warden.py` 的既有设计，唤起不会去绕过它。

`Warden.app` **常驻**，所以它才留在 Dock 和 ⌘-Tab 里。退出：⌘Q，或 `pkill -f Warden.app`。

**登录项注意**：如果把 `Warden.app` 加进登录项，开机就会弹出看板（因为
`applicationDidFinishLaunching` 也会唤起 —— 这是为了让「从后台 open」也能可靠工作）。
不想要的话给登录项设环境变量 `WARDEN_QUIET_LAUNCH=1`。

## 构建

```bash
./app/build-app.sh                    # 生成 app/Warden.app
./app/build-app.sh --out ~/Applications/Warden.app
./app/build-app.sh --install-profile  # 顺带把 iTerm2 的「Warden」外观装进 DynamicProfiles
```

依赖：`swiftc`（Xcode Command Line Tools 自带）+ macOS 自带的
`osascript` / `codesign` / `lsregister` / `iconutil`。
图标由 `make-icon.py` 用 PIL 画（深色渐变 + `W` + 状态绿点）。

仓库路径在 build 时烤进 Swift 和 AppleScript 两处，**所以仓库搬家后要重跑一次**；
`Warden.app` 本身可以放在任何地方（比如 `~/Applications`）。

**只支持 iTerm2**：唤起要一个稳定的窗口身份，iTerm2 的 AppleScript 有 session id，
Terminal.app 没有等价物（窗口 id 重启即失效）。

## 第一次用之前：必须手工点一次授权

macOS 不允许一个 app 悄悄控制另一个 app。第一次唤起会弹
「**「Warden」想要控制「iTerm2」**」，**点「允许」**（那个框只等 2 分钟，超时＝拒绝）。

点错/错过的话，被拒会被记住、之后不再弹框，只能显式重置：

```bash
tccutil reset AppleEvents com.ti-extend.warden
# 或 系统设置 → 隐私与安全性 → 自动化 → 勾上 Warden → iTerm2
```

以后唤起失败时会**自动帮你打开这个设置面板**，并弹一个 12 秒自消失的提示框。

---

## 排障

```bash
cat temp/warden-app.log        # Swift 侧：什么时候决定唤起、osascript 返回了什么
cat temp/warden-summon.log     # AppleScript 侧：命中哪个窗口、每一步成没成
```

`temp/` 已在 `.gitignore` 里。

---

## 踩过的坑（都实测过，别再踩）

| 坑 | 现象 | 结论 |
|---|---|---|
| **AppleScript applet 没有 `activate`** | 点 Dock 能唤起，⌘-Tab 只留一个空窗口 | applet 只支持 run/open/reopen/idle/quit。要 ⌘-Tab 就得是真 app（现在是 Swift） |
| **`set running to ...`** | `-10006 不能将"running"设置为...` | `running` 是 AppleScript 保留属性（只读），变量别叫这个 |
| **`application "iTerm" is running` 不可信** | GUI 没起却返回 `true` | iTerm 的 XPC 守护进程（`iTermServer` / `pidinfo`）活着就算数。改查 `pgrep -x iTerm2` |
| **`set current window to window N`** | `-10000 AppleEvent处理程序失败` | iTerm 3.6.11 不认。要用 `select window/tab/session` 逐层选 |
| **iTerm 半启动时查窗口** | `-1708 "count"不能继续` | 刚被拉起的 iTerm 查 `count of windows` 会抛 errAENoSuchObject；那段整体包 `try`，降级成「没命中 → 新建」 |
| **改完 Info.plist 没重签** | 授权弹框**根本不弹**，直接 `-1743`；设置的「自动化」里也找不到它 | Info.plist 是签名封印的一部分，一改签名就坏；签名一坏 TCC 拒绝记录任何决策。所有改动之后必须 `codesign` 重签（`build-app.sh` 会自动做并校验） |
| **即席签名的身份** | 每次重 build 都算「另一个 app」，授权作废 | designated requirement 显式写 `=designated => identifier "com.ti-extend.warden"` |
| **后台 `open` 不激活 app** | 起来了但什么都没发生 | 不能只挂 `applicationDidBecomeActive`，`applicationDidFinishLaunching` 也得挂 |
| **新窗口 `create` 完立刻 `write text`** | 命令偶尔丢 | 窗口的 shell 还没接上 pty，先 `delay 0.5` |
| **沙箱里 `pgrep` / `ps` 是瞎的** | 明明在跑的进程查不到，会把人带沟里 | 排查唤起问题时别用沙箱 shell 下的 `pgrep` 下结论，看日志 |
| **`$VAR` 后面紧跟中文标点** | bash 报 `APP_DIR\uFFFD: unbound variable` | 变量名的字节判断会把全角标点的首字节吃进变量名。写 `${VAR}。` 而不是 `$VAR。` |
| **拿完整路径去 `grep` 二进制判仓库搬家** | 永远匹配不上，每次重装都重建 | Swift 里烤的是「仓库根」一个字符串，`app/warden-summon.applescript` 是运行时拼的。要 grep 仓库根 |
| **`[ -w /Applications ]`** | 说可写、装到一半才炸 | 只读权限位。要真去写一个探针文件 |

---

## 生成物 / 该不该进 git

- `Warden.app/`、`Warden.iconset/`、`app/.warden-window` —— 构建 / 运行产物，已进 `app/.gitignore`。
- `Warden.icns` —— 跟着走（图标是资产，不是产物）。
- `warden-profile.json` —— iTerm2 外观草稿，`build-app.sh` 只在它不存在时生成，所以你可以直接改它。
