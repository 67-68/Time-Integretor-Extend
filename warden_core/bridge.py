"""warden_core.bridge —— Focusmate 常驻浏览器服务的客户端。

架构（2026-11-23 起）
--------------------
以前这里是「起一个 `scripts/bridge.mjs` 子进程、用 stdin/stdout 的 JSON 行跟它说话」，
桥自己持着 Chromium profile 的锁；daemon 每次下单又现起一只 Chromium，两边靠
`<home>/.profile-lock.json` + want 标记互相让位。实测代价：桥在一条失败命令里
（goto 超时 30s ×3 重试）能连续占 90s，让位轮询又被 `inCommand` 挡着，daemon 下单
等到 90–630s 才失败（`temp/keep_session.log` 2026-11-23 15:18:58 就这么丢掉一场）。

现在只有一个 owner：`scripts/bridge.mjs --serve <home>/browser-service.sock`，
它才是唯一持有 profile 的常驻进程；warden、daemon、命令行都只是客户端
（见 `focusmate_service.py`）。本模块保持 **原来的对外接口**：

    Bridge(console, headed=…, idle_ms=…, profile_dir=…)
    .call(cmd, timeout=…, **kw)   -> result dict
    .browser_state()              -> (服务在?, 浏览器开着?, 错误)
    .alive() / .start() / .stop()
    .last_idle_close / .profile / .stderr_path

`.stop()` 的语义变了：**只断开连接**，不杀服务（服务是独立进程，warden 退出不该
带走别人正在用的浏览器）。真要停服务用 `.shutdown()`。
"""

import threading

import focusmate_service as fm_service

from .common import BRIDGE_LOG, get_warden_global

# ---------------------------------------------------------------- bridge 客户端


class BridgeError(RuntimeError):
    pass


#: 桥的「话」→ 人该干什么。认得出的才翻，认不出原样放过去（别猜）。
_AUTH_HINTS = ("未登录", "登录态", "Firebase token", "token 已过期", "重新登录", "AUTH_REQUIRED")


def explain_bridge_error(msg, profile=""):
    """把 bridge 回来的错翻成「下一步做什么」。

    踩过：登录态失效时前端只显示「内部调用了 process.exit(1)，已转成命令级失败」
    —— bridge 侧现在会把 `die()` 的真话拼在 `｜` 后面，这里再补上动作和
    profile（多 profile 时「读的是哪个」比错本身更关键）。
    """
    text = (msg or "").strip() or "未知错误"
    if any(k in text for k in _AUTH_HINTS):
        # 桥自己已经把「先跑 --auth」和 profile 写进话里了就别重复，只说漏的那句
        if "--auth" not in text:
            text += "　　下一步：`python3 main.py --auth` 重新登录一次"
        if profile and profile not in text:
            text += "（桥读的 profile：%s）" % profile
        return text
    if "process.exit(" in text and "｜" not in text:
        return text + "（bridge 的 die() 不走响应帧；真话在 %s）" % get_warden_global("BRIDGE_LOG", BRIDGE_LOG)
    return text


#: 进程级缓存：Focusmate 运行目录（服务、daemon、TUI 必须同一个）。
_FOCUSMATE_HOME = ""
_FOCUSMATE_HOME_TRIED = False


def focusmate_home():
    """和 main.py / daemon 用**同一个**运行目录。

    踩过：bridge 没传 FOCUSMATE_CONFIG_DIR，`scripts/_lib.mjs` 就退回
    `~/.focusmate-mcp`（9 月的老 profile、登录态早过期），而 daemon 用工作区的
    `temp/focusmate-home`。结果 daemon 一切正常、TUI 每条命令都「未登录」。
    """
    global _FOCUSMATE_HOME, _FOCUSMATE_HOME_TRIED
    if _FOCUSMATE_HOME or _FOCUSMATE_HOME_TRIED:
        return _FOCUSMATE_HOME
    _FOCUSMATE_HOME_TRIED = True  # 失败也只试一次：这函数会被每帧的表头叫到
    try:
        import main as mainmod  # 只为了它那份「运行目录」逻辑，不启动任何东西
        home = mainmod.ensure_mcp_home()
    except Exception:
        home = ""
    _FOCUSMATE_HOME = home or ""
    return _FOCUSMATE_HOME


class Bridge(object):
    """浏览器服务的客户端（保持老接口，传输层换成 unix socket）。

    一条连接串行问答；多线程共用时内部上锁。服务不在就按需拉起
    （`focusmate_service.Service.start`：daemon 优先、warden 兜底）。
    """

    def __init__(self, console, headed=False, idle_ms=None, profile_dir=None):
        self.console = console
        self.headed = headed
        self.idle_ms = idle_ms
        self.profile_dir = profile_dir
        #: 兼容老代码/测试：以前是 bridge 子进程句柄，现在服务由 focusmate_service 管。
        self.proc = None
        self.stderr_path = get_warden_global("BRIDGE_LOG", BRIDGE_LOG)
        #: 服务报上来的 profile 目录（ping/where 带回来的），报错时告诉人
        self.profile = ""
        self.last_idle_close = None
        self.fatal = None
        self._service = None
        self._lock = threading.Lock()

    # -- 生命周期

    def _svc(self):
        with self._lock:
            if self._service is None:
                home = get_warden_global("focusmate_home", focusmate_home)()
                if not home:
                    raise BridgeError(
                        "不知道 Focusmate 运行目录（FOCUSMATE_CONFIG_DIR）；"
                        "先跑 `python3 main.py --where` 看一眼")
                self._service = fm_service.Service(
                    home=home,
                    idle_ms=self.idle_ms,
                    headed=self.headed,
                    profile_dir=self.profile_dir,
                    log_path=self.stderr_path,
                )
            return self._service

    def _child_env(self):
        """服务子进程该带的环境（测试用；真正起进程是 focusmate_service 的事）。

        保留这个方法是因为 ``test_warden.py`` 用它验「运行目录必须传给子进程」
        —— 那条约定本身没变，只是子进程从 stdio 桥换成了 socket 服务。
        """
        return self._svc()._spawn_env()

    def start(self):
        """确保浏览器服务在跑（幂等）。"""
        try:
            self._svc().start()
        except fm_service.ServiceError as e:
            raise BridgeError(explain_bridge_error(str(e), self.profile or self.profile_dir or ""))

    def alive(self):
        try:
            return fm_service.is_running(self._svc().home)
        except (fm_service.ServiceError, BridgeError):
            return False

    def stop(self):
        """**只断开连接**：服务是独立进程，别的客户端可能还在用。

        想真的停服务（关浏览器、删 sock）用 `.shutdown()`。
        """
        if self._service is not None:
            self._service.close()

    def shutdown(self):
        """让服务退出（关浏览器 + 删 sock），给「我要独占 profile 跑 auth」之外的场合用。"""
        if self._service is not None:
            self._service.shutdown()
            self._service = None

    def release(self):
        """关浏览器、把 profile 让出去（服务留着）。"""
        return self.call("release", timeout=60)

    # -- 通信

    @property
    def events(self):
        return self._service.events if self._service is not None else []

    def call(self, cmd, timeout=240, **kw):
        """发一条命令、等它的响应。

        ⚠️ kw 里**不能**出现 `id`：信封的请求号就叫 id，混了会把请求号覆盖掉，
        服务照常执行、响应也发，但这边永远等不到 —— 表现为卡死，而写入其实已经生效。
        （session 身份统一用 meetingId。）这里直接拦住，不给下次踩的机会。
        """
        if "id" in kw:
            raise BridgeError(
                "内部错误：call() 的 kw 里不能有 id（会和请求号撞名）。"
                "session 身份请用 meetingId=。"
            )
        svc = self._svc()
        try:
            result = svc.call(cmd, timeout=timeout, **kw)
        except fm_service.ServiceError as e:
            raise BridgeError(
                explain_bridge_error(str(e), self.profile or self.profile_dir or ""))
        if isinstance(result, dict):
            if result.get("profileDir"):
                self.profile = result["profileDir"]
            if svc.last_idle_close:
                self.last_idle_close = svc.last_idle_close
            self.fatal = svc.fatal
        return result

    def browser_state(self):
        """(服务活着?, 浏览器开着?) —— 用 ping，但不启动浏览器。"""
        if not self.alive():
            return False, False, ""
        try:
            r = self.call("ping", timeout=15)
        except BridgeError as e:
            return False, False, str(e)
        if r.get("profileDir"):
            self.profile = r["profileDir"]
        return True, bool(r.get("browser")), ""

    def where(self):
        """服务视角的自述：读的是哪个 profile、锁在谁手上、浏览器开着没。"""
        return self.call("where", timeout=15)

    def ensure_browser(self):
        """确保浏览器开着（Phase 2 起返回里会带 cdpUrl）。"""
        return self.call("ensure", timeout=90)

    def hold(self, ms=600000):
        """登录/维护窗口：关浏览器 + 期间拒绝开浏览器。"""
        return self.call("hold", timeout=90, ms=int(ms))

    def unhold(self):
        return self.call("unhold", timeout=30)
