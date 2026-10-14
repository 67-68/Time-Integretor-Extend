-- warden-summon.applescript —— 「唤起 Warden」的唯一实现。
--
-- 语义：**找到就聚焦，找不到就新建**，全程不会开出第二个窗口。
--
--   1. 读 app/.warden-window 里记的 iTerm session id（上次新建时写下的）
--   2. 在 iTerm 现有 window → tab → session 里找这个 id
--   3. 找到 → activate iTerm + 把它置为 current window + 往那个 session 的 stdin
--      打一行 refresh（TUI 会像你手敲一样执行，顺手把看板数字刷新成最新）
--   4. 找不到（第一次跑 / 你把窗口关了 / iTerm 重启过）→ 新建窗口跑
--      ./app/Warden.command，再把新 session id 写回文件
--
-- 为什么是「记 id」而不是「靠窗口标题匹配」：标题会被 TUI 自己改写，也不唯一；
-- session id 是 iTerm 发的 UUID，稳。文件丢了最坏结果只是多开一个窗口，不会出错。
--
-- 为什么要 on idle + Info.plist 的 OSAAppletStayOpen：想让 Warden 出现在 Dock /
-- Command-Tab 里，applet 就必须常驻着（光有 on idle 不够，实测过）。
--
-- 用法：
--   osascript app/warden-summon.applescript              # 唤起
--   osascript app/warden-summon.applescript --dry-run    # 只说会干什么，绝不碰 iTerm
--   osascript app/warden-summon.applescript --new        # 无视已有窗口，强制新建
--   osascript app/warden-summon.applescript --no-refresh # 只聚焦，不打 refresh
--   osascript app/warden-summon.applescript --log        # 只看日志
--   （编译成 Warden.app 后：双击 / 点 Dock / open -a Warden / tell application "Warden" to activate）
--
-- 排障：每一步都往 temp/warden-summon.log 写一行。唤起没反应就先看那个文件。
-- 目标终端是 iTerm2（本机 3.6.11）。AppleScript 名字是 "iTerm"（CFBundleName 虽然是
-- "iTerm2"，但脚本字典按 "iTerm" 注册），所以下面全部写死 "iTerm"。

property REPO_HINT : "__REPO__"    -- build-app.sh 会换成真路径；直接 osascript 跑时保持原样，走 path to me 兜底
property MARKER_NAME : ".warden-window"
property LOG_REL : "temp/warden-summon.log"


-- ---------------------------------------------------------------- 入口
-- 每个入口都包一层 try：失败要留痕（日志 + 8 秒自消失的提示框），
-- 不能让「唤起没反应」变成一件查不出原因的事。

on run argv
try
return my summon(my parseArgs(argv))
on error e number n
return my fail(e, n)
end try
end run

on reopen
try
return my summon(my optsDefault())
on error e number n
return my fail(e, n)
end try
end reopen

-- 常驻：让 applet 不退出，Dock / Command-Tab 里才留得住 Warden。
on idle
return 60
end idle

on quit
continue quit
end quit


-- ---------------------------------------------------------------- 参数

on optsDefault()
return {dryRun:false, forceNew:false, doRefresh:true, logOnly:false}
end optsDefault

on parseArgs(argv)
set dryRun to false
set forceNew to false
set doRefresh to true
set logOnly to false
repeat with a in argv
set s to (a as text)
if s is "--dry-run" then set dryRun to true
if s is "--new" then set forceNew to true
if s is "--no-refresh" then set doRefresh to false
if s is "--log" then set logOnly to true
end repeat
return {dryRun:dryRun, forceNew:forceNew, doRefresh:doRefresh, logOnly:logOnly}
end parseArgs


-- ---------------------------------------------------------------- 主流程

on summon(opts)
set dryRun to dryRun of opts
set forceNew to forceNew of opts
set wantRefresh to doRefresh of opts

set repo to my resolveRepo()
set markerPath to repo & "/app/" & MARKER_NAME
set cmd to "cd " & quoted form of repo & " && exec ./app/Warden.command"

if logOnly of opts then
try
return (do shell script "cat " & quoted form of (repo & "/" & LOG_REL) & " 2>/dev/null || true")
on error
return "(读不到日志)"
end try
end if

my logLine("")
my logLine("=== summon  dry-run=" & dryRun & " forceNew=" & forceNew & " refresh=" & wantRefresh)

set priorId to my readMarker(markerPath)
my logLine("repo=" & repo & "  marker=" & markerPath & "  priorId=" & priorId)

-- dry-run 的前提：绝不 launch iTerm。所以先问「GUI 在不在跑」，而不是直接 tell。
-- 坑：不能用 application "iTerm" is running —— iTerm 的 XPC 守护进程活着时它就报
-- true，而 GUI 根本没起（实测 3.6.11）。改成查主可执行文件名，零权限、准。
set guiUp to my isITermGuiRunning()
my logLine("iTerm GUI running = " & guiUp)

if guiUp is false then
if dryRun then
return "iTerm 没在跑 → 会新建窗口跑：" & cmd
end if
return my spawnWindow(cmd, markerPath)
end if

-- 找上次那个 session
set hitW to 0
set hitT to 0
set hitS to 0
set hitId to ""
if (not forceNew) and (priorId is not "") then
-- 整体包 try：iTerm 刚被拉起来、还没完全就绪时 count 会抛 -1708
-- （errAENoSuchObject）。那种时候降级成「没命中 → 新建」就行，
-- 不该让整个唤起硬失败。
try
tell application "iTerm"
repeat with wi from 1 to (count of windows)
set w to window wi
repeat with ti from 1 to (count of tabs of w)
set t to tab ti of w
repeat with si from 1 to (count of sessions of t)
set s to session si of t
if (id of s) is priorId then
set hitW to wi
set hitT to ti
set hitS to si
set hitId to (id of s)
end if
end repeat
end repeat
end repeat
end tell
on error e number n
my logLine("找窗口出错（当作没命中继续）" & n & ": " & e)
end try
end if
my logLine("命中 = " & hitId & "  (window " & hitW & " tab " & hitT & ")")

if dryRun then
if hitId is not "" then
set tail to "只聚焦"
if wantRefresh then set tail to "聚焦 + refresh"
return "命中 session " & hitId & "（window " & hitW & " / tab " & hitT & "）→ " & tail
end if
if forceNew then
return "--new：无视标记（原 id=" & priorId & "）→ 会新建窗口跑：" & cmd
end if
return "没命中（标记里的 id=" & priorId & "）→ 会新建窗口跑：" & cmd
end if

if hitId is not "" then
my focusSession(hitW, hitT, hitS)
my logLine("已聚焦 " & hitId)
if wantRefresh then
-- TUI 的输入循环是 cbreak 逐键读的，write text 等于把 refresh<回车>
-- 打进它的 stdin —— 和手敲完全同一条路。失败不致命（顶多数字不刷新）。
try
tell application "iTerm"
tell session hitS of tab hitT of window hitW to write text "refresh"
end tell
my logLine("已发 refresh")
on error e number n
my logLine("refresh 失败 " & n & ": " & e)
end try
end if
return "focused " & hitId
end if

return my spawnWindow(cmd, markerPath)
end summon


-- 把那个 session 顶到最前：先 activate iTerm，再把它的 window 设为 current。
-- 三步各自 try：iTerm 各版本对 select 的支持程度不一样，任何一步失败也不该
-- 让「至少把 iTerm 调到前台」这件事失败。
on focusSession(hitW, hitT, hitS)
tell application "iTerm"
activate
my logLine("  activate iTerm 完成")
-- 坑：不要用 `set current window to window N` —— iTerm 3.6.11 会回
-- -10000「AppleEvent处理程序失败」。用 select 命令，逐层选中才有效。
try
select (window hitW)
my logLine("  select window " & hitW & " 成功")
on error e number n
my logLine("  select window 失败 " & n & ": " & e)
end try
try
select (tab hitT of window hitW)
my logLine("  select tab " & hitT & " 成功")
on error e number n
my logLine("  select tab 失败 " & n & ": " & e)
end try
try
select (session hitS of tab hitT of window hitW)
my logLine("  select session " & hitS & " 成功")
on error e number n
my logLine("  select session 失败 " & n & ": " & e)
end try
end tell
end focusSession


on spawnWindow(cmd, markerPath)
my logLine("spawnWindow: " & cmd)
set newId to ""
tell application "iTerm"
activate
my logLine("  已 activate iTerm")
create window with default profile
my logLine("  已 create window")
-- 刚 create 完 shell 可能还没起来，此时 write 会打到还没接上的 pty 上，丢掉。
-- 0.5s 是拿实测的冷启动时间取的保守值，只在新建时付一次。
delay 0.5
tell current session of current window
write text cmd
end tell
set newId to (id of current session of current window)
end tell
my logLine("  新 session id = " & newId)
my writeMarker(markerPath, newId)
return "created " & newId
end spawnWindow


on fail(e, n)
my logLine("!! 失败 " & n & "：" & e)
set hint to ""
if n is -1743 then
-- 唯一需要人工点一次的地方：macOS 不许一个 app 悄悄控制另一个 app。
set hint to "
没拿到「控制 iTerm2」的自动化授权。

① 系统设置 → 隐私与安全性 → 自动化 → 允许 Warden 控制 iTerm2
② 或者：tccutil reset AppleEvents com.ti-extend.warden
   然后重新唤起，在弹出的框里点「允许」（框只等 2 分钟）"
end if
if n is -1743 then
-- 顺手把该去的设置面板打开，省得人去翻菜单
try
do shell script "open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation'"
end try
end if
try
display alert "Warden 唤起失败" message (e & "（错误号 " & n & "）" & hint) as warning giving up after 12
end try
return "error " & n & ": " & e & hint
end fail


-- ---------------------------------------------------------------- 环境探测

on isITermGuiRunning()
-- pgrep -x 按可执行文件名精确匹配，不碰 LaunchServices、不需要自动化权限。
set r to (do shell script "if pgrep -x iTerm2 >/dev/null 2>&1 || pgrep -x iTerm >/dev/null 2>&1; then echo yes; else echo no; fi")
return (r is "yes")
end isITermGuiRunning


-- ---------------------------------------------------------------- 仓库路径

on resolveRepo()
-- 优先用 build 时烤进来的绝对路径（applet 被移出仓库也不断）
if REPO_HINT starts with "/" then
try
if (do shell script "[ -d " & quoted form of REPO_HINT & " ] && echo yes") is "yes" then
return REPO_HINT
end if
end try
end if
-- 兜底：path to me 是 .../app/Warden.app 或 .../app/warden-summon.applescript，
-- 两种情况 dirname 都正好是 app/，再往上一级就是仓库根
set p to POSIX path of (path to me)
if p ends with "/" then set p to text 1 thru -2 of p
set appDir to (do shell script "dirname " & quoted form of p)
if appDir ends with "/app" then
return (do shell script "cd " & quoted form of appDir & " && cd .. && pwd")
end if
return appDir
end resolveRepo


-- ---------------------------------------------------------------- 标记文件 / 日志

on readMarker(p)
try
return (do shell script "cat " & quoted form of p & " 2>/dev/null || true")
on error
return ""
end try
end readMarker

on writeMarker(p, theId)
if theId is "" then return
try
do shell script "printf %s " & quoted form of theId & " > " & quoted form of p
on error
-- 写不进去就算了：下次唤起找不到标记 → 新建一个窗口。不致命，不打扰人。
end try
end writeMarker

on logLine(msg)
try
set p to my resolveRepo() & "/" & LOG_REL
do shell script "echo " & quoted form of msg & " >> " & quoted form of p
end try
end logLine
