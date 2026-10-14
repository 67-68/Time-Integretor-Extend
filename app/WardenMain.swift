// WardenMain.swift —— Warden.app 的本体：一个「前台唤起器」。
//
// 为什么不用 osacompile 的 AppleScript applet：
//   applet 只支持 run / open / reopen / idle / quit 这几个钩子，
//   **没有 activate**。于是「点 Dock 图标」（发 reopen）能用，而
//   「⌘-Tab 切过来」（只发 activate）什么都不做，只剩一个空窗口挂在最前面。
//   换成真 app 才有 applicationDidBecomeActive，两条路才终于是一条路。
//
// 本进程**不实现任何窗口逻辑**：找到/新建/聚焦全在 warden-summon.applescript 里，
// 那是唯一真相（也能单独 `osascript` 跑）。这里只负责「什么时候该唤起」。
import Cocoa

// build-app.sh 会把 __REPO__ 换成真实仓库路径
let REPO = "__REPO__"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let queue = DispatchQueue(label: "warden.summon")
    private let lock = NSLock()
    private var busy = false
    private var lastAt = Date.distantPast

    // ⌘-Tab 切过来 / 从别的 app 切回来 —— applet 拿不到的那个事件
    func applicationDidBecomeActive(_ notification: Notification) {
        summon(reason: "becomeActive")
    }

    // 点 Dock 图标 / open -a / Finder 双击
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        summon(reason: "reopen")
        return false
    }

    // 没有任何窗口，别因为「窗口都关了」就退出
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    // 启动就要唤起。**不能只靠 applicationDidBecomeActive**：实测从后台
    // （终端 / 脚本 / 自动化工具）`open` 一个 app 时，它不一定被激活，
    // 那条路就会「起来了但什么都没发生」。两个都挂上，靠防抖去重。
    //
    // 副作用：把 Warden.app 放进「登录项」会导致开机就弹出看板。
    // 需要登录项又不想弹的话，看 README 里的 WARDEN_QUIET_LAUNCH。
    func applicationDidFinishLaunching(_ notification: Notification) {
        if ProcessInfo.processInfo.environment["WARDEN_QUIET_LAUNCH"] == "1" {
            log("启动完成（WARDEN_QUIET_LAUNCH=1，不唤起）")
            return
        }
        log("启动完成")
        summon(reason: "launch")
    }

    private func summon(reason: String) {
        lock.lock()
        let now = Date()
        if busy {
            lock.unlock()
            log("跳过 \(reason)：上一次还没跑完")
            return
        }
        // 防抖。两件事都要靠它：
        //   1) didFinishLaunching → didBecomeActive 会在同一瞬间连着来
        //   2) 我们 activate iTerm 之后，系统可能把 active 又交还给 Warden
        //      （实测会晚 1 秒左右再 becomeActive 一次），那一下不该再 push 一次 refresh
        if now.timeIntervalSince(lastAt) < 1.5 {
            lock.unlock()
            log("跳过 \(reason)：距上次不足 0.5s")
            return
        }
        busy = true
        lastAt = now
        lock.unlock()

        queue.async { [weak self] in
            guard let self = self else { return }
            self.runSummon(reason: reason)
            self.lock.lock()
            self.busy = false
            self.lock.unlock()
        }
    }

    private func runSummon(reason: String) {
        let script = REPO + "/app/warden-summon.applescript"
        guard FileManager.default.fileExists(atPath: script) else {
            log("找不到 \(script)（仓库挪过？重跑 ./app/build-app.sh）")
            return
        }
        log("唤起（\(reason)）→ \(script)")

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = [script]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        do {
            try proc.run()
        } catch {
            log("osascript 起不来：\(error)")
            return
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        let out = (String(data: data, encoding: .utf8) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        log("osascript 退出 \(proc.terminationStatus)：\(out)")
    }

    private func log(_ msg: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "\(stamp) [app] \(msg)\n"
        let path = REPO + "/temp/warden-app.log"
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile()
            h.write(Data(line.utf8))
            try? h.close()
        } else {
            try? line.write(toFile: path, atomically: true, encoding: .utf8)
        }
    }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// .regular = 出现在 Dock 和 ⌘-Tab 里（这正是我们要的）
app.setActivationPolicy(.regular)

// 最小菜单栏：没有窗口的 app 也得能 ⌘Q 退出
let mainMenu = NSMenu()
let appMenuItem = NSMenuItem()
mainMenu.addItem(appMenuItem)
let appMenu = NSMenu()
appMenu.addItem(withTitle: "关于 Warden",
                action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                keyEquivalent: "")
appMenu.addItem(NSMenuItem.separator())
appMenu.addItem(withTitle: "退出 Warden",
                action: #selector(NSApplication.terminate(_:)),
                keyEquivalent: "q")
appMenuItem.submenu = appMenu
app.mainMenu = mainMenu

app.run()
