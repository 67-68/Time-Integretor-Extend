"""warden_core.bridge —— Focusmate 常驻 Chromium 桥接客户端（scripts/bridge.mjs）。"""
import json
import os
import queue
import subprocess
import threading
import time

from .common import BRIDGE_JS, BRIDGE_LOG, HERE, TEMP, get_warden_global

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


#: 进程级缓存：Focusmate 运行目录（bridge 子进程和 daemon 必须同一个）。
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
    """scripts/bridge.mjs 的客户端：起进程、发命令、收响应。

    stdout 上是 JSON 行。这里用后台线程读，主线程带超时取，
    这样「浏览器冷启动 15s」不会把 UI 卡死到没救。
    """

    def __init__(self, console, headed=False, idle_ms=None, profile_dir=None):
        self.console = console
        self.headed = headed
        self.idle_ms = idle_ms
        self.profile_dir = profile_dir
        self.proc = None
        self.q = queue.Queue()
        self.reader = None
        self.next_id = 1
        self.events = []
        self.fatal = None
        self.stderr_path = get_warden_global("BRIDGE_LOG", BRIDGE_LOG)
        #: 桥自己报上来的 profile 目录（ready / ping 里带回来的），报错时告诉人
        self.profile = ""
        self.last_idle_close = None
        # 命令是串行的（bridge 侧一条条跑），所以这边也上锁：
        # 收尾的后台核对线程和主线程可能同时说话，协议是「一问一答」的，
        # 两边同时写 stdin 会让响应和请求号对不上。
        self._lock = threading.Lock()

    # -- 生命周期

    def _child_env(self):
        """给 bridge 子进程的环境。

        **必须**把运行目录传下去：不传的话 `scripts/_lib.mjs` 会按自己的优先级
        猜，而它以前猜的是老位置 `~/.focusmate-mcp` —— 那儿的登录态早过期了。
        表现是 daemon 一切正常、TUI 每条命令都「未登录」（真踩过，见
        `focusmate_home()` 的注释）。
        """
        env = dict(os.environ)
        if self.headed:
            env["WARDEN_BRIDGE_HEADED"] = "1"
        if self.idle_ms is not None:
            env["WARDEN_BRIDGE_IDLE_MS"] = str(self.idle_ms)
        home = get_warden_global("focusmate_home", focusmate_home)()
        if home:
            env["FOCUSMATE_CONFIG_DIR"] = home
        if self.profile_dir:
            env["FOCUSMATE_PROFILE_DIR"] = self.profile_dir
        return env

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        env = self._child_env()
        os.makedirs(TEMP, exist_ok=True)
        self.errf = open(self.stderr_path, "a")
        self.proc = subprocess.Popen(
            ["node", BRIDGE_JS],
            cwd=HERE,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errf,
            text=True,
            bufsize=1,
        )
        self.q = queue.Queue()
        self.reader = threading.Thread(target=self._read_loop, args=(self.proc,), daemon=True)
        self.reader.start()

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    self.q.put({"event": "protocol-error", "raw": line[:300]})
        except Exception as e:  # 进程被杀了之类
            self.q.put({"event": "reader-died", "error": str(e)})
        finally:
            self.q.put({"event": "eof"})

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                # 关 stdin 就够：bridge 会自己关浏览器再退
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            try:
                self.errf.close()
            except Exception:
                pass
            self.proc = None

    # -- 通信

    def _drain_events(self):
        """把队列里的事件帧消化掉（响应帧留在队列里给 call 取）。"""
        kept = []
        while True:
            try:
                f = self.q.get_nowait()
            except queue.Empty:
                break
            if f.get("event"):
                self.events.append(f)
                if f["event"] == "idle-closed":
                    self.last_idle_close = time.time()
                if f["event"] == "fatal":
                    self.fatal = f.get("error")
                if f.get("profileDir"):
                    self.profile = f["profileDir"]
            else:
                kept.append(f)
        for f in kept:
            self.q.put(f)

    def call(self, cmd, timeout=240, **kw):
        """发一条命令、等它的响应。

        ⚠️ kw 里**不能**出现 `id`：信封的请求号就叫 id，混了会把请求号覆盖掉，
        桥照常执行、响应也发，但这边永远等不到 —— 表现为卡死，而写入其实已经生效。
        （session 身份统一用 meetingId。）这里直接拦住，不给下次踩的机会。
        """
        if "id" in kw:
            raise BridgeError(
                "内部错误：call() 的 kw 里不能有 id（会和请求号撞名）。"
                "session 身份请用 meetingId=。"
            )
        with self._lock:
            return self._call_locked(cmd, timeout, kw)

    def _call_locked(self, cmd, timeout, kw):
        self.start()
        self._drain_events()
        req_id = self.next_id
        self.next_id += 1
        req = {"id": req_id, "cmd": cmd}
        req.update(kw)
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise BridgeError("bridge 进程没了（%s）；看 %s" % (e, self.stderr_path))

        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise BridgeError("bridge 超过 %ds 没回 %s（浏览器卡住了？看 %s）"
                                  % (timeout, cmd, self.stderr_path))
            try:
                f = self.q.get(timeout=min(left, 5))
            except queue.Empty:
                if not self.alive():
                    raise BridgeError("bridge 进程退出了（看 %s）" % self.stderr_path)
                continue
            if f.get("event"):
                self.events.append(f)
                if f["event"] == "idle-closed":
                    self.last_idle_close = time.time()
                if f["event"] == "fatal":
                    self.fatal = f.get("error")
                if f.get("profileDir"):
                    self.profile = f["profileDir"]
                if f["event"] == "eof":
                    raise BridgeError("bridge 的 stdout 关了（看 %s）" % self.stderr_path)
                continue
            if f.get("id") != req_id:
                # 不该发生（命令是串行的），但别把响应丢了
                continue
            if not f.get("ok"):
                raise BridgeError(
                    explain_bridge_error(f.get("error"), self.profile or self.profile_dir or ""))
            r = f.get("result") or {}
            if r.get("profileDir"):
                self.profile = r["profileDir"]
            return r

    def browser_state(self):
        """(bridge 活着?, 浏览器开着?) —— 用 ping，但不启动浏览器。"""
        if not self.alive():
            return False, False, ""
        try:
            r = self.call("ping", timeout=15)
        except BridgeError as e:
            return False, False, str(e)
        return True, bool(r.get("browser")), ""


