#!/usr/bin/env python3
"""focusmate_service + warden_core.bridge 的回归（纯 unix socket，无浏览器/网络）。

钉住的新架构约定：

* **一个 home 只有一个浏览器服务**：`Service.start()` 幂等，第二个 node 进程会被
  socket 单实例挡掉（退出码 3）；
* **调用方只是客户端**：`Bridge.stop()` 只断开连接，不杀服务；`shutdown()` 才真停；
* **hold 期间绝不开浏览器**（auth 要 headed 独占 profile），`ensure_browser` 必须报错；
* **服务被 kill -9 之后能自愈**：残留 sock 自动清理，下一条命令拉起新进程；
* 命令里的 `id` 是信封请求号，调用方 kw 里带 id 必须硬拦（踩过：写入生效但永远等不到响应）。

真正的「浏览器开起来 / 到点自动关」需要真 Chromium，属于 live 测试。
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ok = 0
fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("ok   %s" % name)
    else:
        fail += 1
        print("FAIL %s\n  got : %r\n  want: %r" % (name, got, want))


def check_true(name, got):
    check(name, bool(got), True)


def wait_until(fn, timeout=10.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


with tempfile.TemporaryDirectory() as home:
    os.environ["FOCUSMATE_CONFIG_DIR"] = home
    os.environ["TI_LOG_DIR"] = os.path.join(home, "logs")
    os.environ["WARDEN_BRIDGE_IDLE_MS"] = "60000"

    import focusmate_service as fs
    import warden_core.bridge as wbridge

    # Bridge 的运行目录要指到临时目录（默认会用工作区的 temp/focusmate-home）
    wbridge._FOCUSMATE_HOME = home
    wbridge._FOCUSMATE_HOME_TRIED = True

    def service_pids():
        st = fs.read_state(home) or {}
        return st.get("pid")

    # --- 1. start 幂等 -------------------------------------------------
    svc = fs.Service(home=home)
    st1 = svc.start()
    pid1 = st1.get("pid")
    check_true("服务起来了（有 pid）", pid1)
    st2 = svc.start()
    check("再 start 一次是同一个进程（幂等）", st2.get("pid"), pid1)
    check("门牌文件写在 home 下", os.path.exists(fs.state_path(home)), True)
    check_true("门牌里带着 CDP 端口（供 focusmate-mcp 复用浏览器）",
               isinstance(st1.get("cdpPort"), int) and st1["cdpPort"] > 0)
    sock_mode = stat.S_IMODE(os.stat(fs.sock_path(home)).st_mode)
    check("socket 权限 600（只有自己能连）", sock_mode, 0o600)

    # --- 2. 单实例：第二个进程必须退出 ---------------------------------
    proc = subprocess.run(
        ["node", fs.BRIDGE_JS, "--serve", fs.sock_path(home)],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=30)
    check("第二个服务进程退出码 3", proc.returncode, 3)
    check_true("它说了是单实例", "已经有一个服务在跑" in (proc.stderr or ""))

    # --- 3. 未知命令 / 超时要报错，不能假装成功 ------------------------
    bad = False
    try:
        svc.call("no_such_command", timeout=10)
    except fs.ServiceError as e:
        bad = "未知命令" in str(e)
    check("未知命令报错", bad, True)

    timed_out = False
    t0 = time.time()
    try:
        # timeout=0 是确定性的「已经超时」：不等，立刻抛。
        # （别用 0.0001 这种：macOS 上 SO_RCVTIMEO 会被抹成 0 → 阻塞到响应回来，反而成功。）
        svc.call("ping", timeout=0)
    except fs.ServiceError as e:
        timed_out = "超时" in str(e) or "timeout" in str(e).lower()
    check("超时会抛 ServiceError", timed_out, True)
    check_true("超时是立刻返回的（没卡死）", time.time() - t0 < 5)

    # --- 4. hold 期间不开浏览器 ----------------------------------------
    held = svc.hold(3000)
    check("hold 返回 holdUntil", bool(held.get("holdUntil")), True)
    refused = False
    try:
        svc.ensure_browser(timeout=10)
    except fs.ServiceError as e:
        refused = "hold" in str(e)
    check("hold 期间 ensure 被拒（而不是冷启动浏览器）", refused, True)
    check("hold 期间浏览器仍然是关着的", svc.ping().get("browser"), False)
    # 共用浏览器：auth 绝不允许走别人的浏览器；hold 期间其它工具也拿不到 CDP
    import main as m
    check("auth 不借共用浏览器", m.shared_browser_cdp("focusmate_auth", home), "")
    check("hold 期间拿不到 CDP（降级而不是硬闯）",
          m.shared_browser_cdp("book_session", home), "")
    svc.unhold()
    check("unhold 之后不再 hold", svc.ping().get("held"), False)

    # --- 5. Bridge 客户端：id 硬拦、不拉起浏览器 ----------------------
    class _Console(object):
        def print(self, *a, **k):
            pass

    br = wbridge.Bridge(_Console(), idle_ms=60000)
    stopped = False
    try:
        br.call("mute", startMs=1, id="some-uuid", quiet=True)
    except wbridge.BridgeError as e:
        stopped = "不能有 id" in str(e)
    check("kw 里带 id 被拦住", stopped, True)
    check("拦住时不启子进程（proc 仍是 None）", br.proc, None)

    alive, browser, err = br.browser_state()
    check("browser_state: 服务在", alive, True)
    check("browser_state: 浏览器关着（ping 不启动浏览器）", browser, False)

    # --- 6. stop 只断连，shutdown 才真停 ------------------------------
    br.stop()
    check_true("Bridge.stop() 之后服务还活着", fs.is_running(home))
    check("Bridge.stop() 之后 pid 没变", service_pids(), pid1)

    # --- 7. 服务被 kill -9 之后自愈 -----------------------------------
    os.kill(int(pid1), 9)
    check_true("被 kill 后 is_running 变 False", wait_until(lambda: not fs.is_running(home)))

    svc2 = fs.Service(home=home)
    st3 = svc2.start()
    pid2 = st3.get("pid")
    check_true("自动拉起新服务", bool(pid2))
    check("新进程和新 pid", pid2 != pid1, True)
    check("残留 sock 被清理后仍能连", svc2.ping().get("pong"), True)

    # --- 8. shutdown 真退出且不留残骸 ---------------------------------
    svc2.shutdown()
    check_true("shutdown 后服务没了", wait_until(lambda: not fs.is_running(home)))
    check("shutdown 删掉 sock", os.path.exists(fs.sock_path(home)), False)
    check("shutdown 删掉门牌", os.path.exists(fs.state_path(home)), False)

print("\n%d ok, %d fail" % (ok, fail))
sys.exit(1 if fail else 0)
