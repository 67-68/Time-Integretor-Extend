"""Focusmate 浏览器服务的 Python 客户端（scripts/bridge.mjs --serve）。

为什么要有这个模块
------------------
以前「谁要浏览器谁自己起一只 Chromium」：warden 的桥常驻一只、keep_session daemon
每次下单现起一只、task_attach / auth_store 又各起一只，全靠 `<home>/.profile-lock.json`
互相让位。同一个 profile 不能同时被两个 Chromium 打开（会白屏、会打坏 IndexedDB），
于是等待就不可避免；更糟的是桥在一条失败命令里能连续占 90s（goto 超时 30s ×3 重试），
daemon 下单实测等到过 90–630s 才失败。

现在只有一个 owner：`scripts/bridge.mjs --serve <home>/browser-service.sock`。
本模块负责把它**当服务用**：

* :meth:`Service.ensure_started` 没有就拉起来（daemon 优先、warden 兜底，谁先谁起）；
* :meth:`Service.call` 发一条命令收一条响应（JSON 行，一条连接串行问答）；
* :meth:`Service.ensure_browser` 让服务把浏览器开起来（Phase 2 会同时拿到 cdpUrl）；
* :meth:`Service.release` / :meth:`Service.hold` 把 profile 让给外部的 headed 登录。

约定
----
* 服务是**独立进程**（``start_new_session=True``）：warden 退出、daemon 重启都不该带走它，
  它自己按「还有没有客户端连着」决定何时关浏览器。
* ``.close()`` 只断开连接，不杀服务；``.stop()`` 才真的让服务退出。
* 服务不在时**不假装成功**：需要浏览器的命令一律抛 :class:`ServiceError`。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

#: 服务脚本（测试可用 TI_BRIDGE_JS 指过去）。
BRIDGE_JS = os.environ.get("TI_BRIDGE_JS") or os.path.join(_HERE, "scripts", "bridge.mjs")

SOCK_NAME = "browser-service.sock"
STATE_NAME = "browser-service.json"

#: 没有客户端连着之后，多久关浏览器。0 = 永不关（别这么用：daemon 会一直抢不到 profile）。
DEFAULT_IDLE_MS = 120000

#: 拉起服务最多等多久（node 起来 + 导入 playwright + listen）。
START_TIMEOUT = 30.0
#: 单条命令的默认上限（浏览器冷启动 15s + 一次操作）。
DEFAULT_TIMEOUT = 240.0

#: 探活/等 socket 的轮询间隔。
POLL_S = 0.15


class ServiceError(RuntimeError):
    """服务不在 / 断了 / 回了错误帧。"""


def home_dir(home=None):
    """运行目录：显式传 > `FOCUSMATE_CONFIG_DIR` > 报错（绝不猜 `~/.focusmate-mcp`）。"""
    home = (home or os.environ.get("FOCUSMATE_CONFIG_DIR") or "").strip()
    if not home:
        raise ServiceError(
            "不知道 Focusmate 运行目录：传 home= 或设 FOCUSMATE_CONFIG_DIR"
            "（猜 ~/.focusmate-mcp 会读到过期的老 profile）"
        )
    return os.path.abspath(os.path.expanduser(home))


def sock_path(home):
    return os.path.join(home_dir(home), SOCK_NAME)


def state_path(home):
    return os.path.join(home_dir(home), STATE_NAME)


def read_state(home):
    """服务自己写的门牌（pid/sock/home/idleMs…）；没有或坏了返回 None。"""
    try:
        with open(state_path(home), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        import errno as _errno

        return e.errno == _errno.EPERM
    return True


def _socket_alive(path, timeout=0.6):
    """sock 上有没有活着的服务（连一下就知道，不依赖 pid 文件）。"""
    if not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass
    return True


def is_running(home=None):
    """服务在不在：sock 能连上，而且门牌上的 pid 还活着。"""
    try:
        home = home_dir(home)
    except ServiceError:
        return False
    st = read_state(home) or {}
    if st.get("pid") and not pid_alive(st.get("pid")):
        return False
    return _socket_alive(sock_path(home))


class Service(object):
    """一条到浏览器服务的连接（串行问答，带事件帧过滤）。

    多个线程可以共用一个实例：``call`` 内部上锁，保证「一问一答」不串台。
    """

    def __init__(self, home=None, idle_ms=None, headed=False, profile_dir=None,
                 log_path=None):
        self.home = home_dir(home)
        self.idle_ms = idle_ms
        self.headed = headed
        self.profile_dir = profile_dir
        self.log_path = log_path or os.path.join(self.home, "browser-service.log")
        self.sock = sock_path(self.home)
        self.sock_obj = None
        self._buf = b""
        self._lock = threading.Lock()
        self.next_id = 1
        #: 收到的事件帧（idle-closed / welcome / fatal…），调用方可以事后查。
        self.events = []
        self.last_idle_close = None
        self.fatal = None

    # -- 生命周期

    def _spawn_env(self):
        env = dict(os.environ)
        env["FOCUSMATE_CONFIG_DIR"] = self.home
        idle = self.idle_ms
        if idle is None or int(idle) <= 0:
            idle = DEFAULT_IDLE_MS
        # 用 setdefault：外部显式设了就以外部为准（测试会设很小的值）
        env.setdefault("WARDEN_BRIDGE_IDLE_MS", str(int(idle)))
        if self.headed:
            env.setdefault("WARDEN_BRIDGE_HEADED", "1")
        if self.profile_dir:
            env.setdefault("FOCUSMATE_PROFILE_DIR", self.profile_dir)
        return env

    def start(self, timeout=START_TIMEOUT):
        """确保服务在跑（没有就拉起来），返回门牌 dict。"""
        if is_running(self.home):
            return read_state(self.home) or {}
        os.makedirs(self.home, exist_ok=True)
        log = None
        try:
            log = open(self.log_path, "a")
        except OSError:
            log = subprocess.DEVNULL
        try:
            subprocess.Popen(
                ["node", BRIDGE_JS, "--serve", self.sock],
                cwd=_HERE,
                env=self._spawn_env(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,   # 和服务解耦：warden 退出/daemon 重启都不带走它
            )
        except OSError as e:
            raise ServiceError("起不来浏览器服务（%s）：%s" % (BRIDGE_JS, e))
        finally:
            if log not in (None, subprocess.DEVNULL):
                try:
                    log.close()
                except OSError:
                    pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_running(self.home):
                return read_state(self.home) or {}
            time.sleep(POLL_S)
        raise ServiceError(
            "浏览器服务 %ss 内没起来：看 %s（sock=%s）"
            % (int(timeout), self.log_path, self.sock)
        )

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(DEFAULT_TIMEOUT)
        try:
            s.connect(self.sock)
        except OSError as e:
            try:
                s.close()
            except OSError:
                pass
            raise ServiceError("连不上浏览器服务（%s）：%s" % (self.sock, e))
        s.setblocking(True)
        self.sock_obj = s
        self._buf = b""
        return s

    def connect(self):
        with self._lock:
            if self.sock_obj is not None:
                return self
            self.start()
            self._connect()
        return self

    def close(self):
        """只断开这条连接（服务继续活着，别的客户端还能用）。"""
        with self._lock:
            if self.sock_obj is not None:
                try:
                    self.sock_obj.close()
                except OSError:
                    pass
            self.sock_obj = None
            self._buf = b""

    #: warden 那边沿用 ``bridge.stop()`` 的名字；语义＝断开连接。
    stop = close

    def shutdown(self, timeout=20):
        """真的让服务退出（关浏览器、删 sock）。"""
        try:
            self.call("shutdown", timeout=timeout)
        except ServiceError:
            pass
        finally:
            self.close()

    # -- 通信

    def _read_line(self, deadline):
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = self._buf[:idx].strip()
                self._buf = self._buf[idx + 1:]
                if not line:
                    continue
                try:
                    return json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    return {"event": "protocol-error", "raw": line[:200].decode("utf-8", "replace")}
            left = deadline - time.time()
            if left <= 0:
                raise ServiceError("浏览器服务超时没回（sock=%s）" % self.sock)
            if self.sock_obj is None:
                raise ServiceError("浏览器服务的连接已经断了（sock=%s）" % self.sock)
            try:
                self.sock_obj.settimeout(min(left, 1.0))
                chunk = self.sock_obj.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                self.close()
                raise ServiceError("浏览器服务连接出错（%s）：%s" % (self.sock, e))
            if not chunk:
                self.close()
                raise ServiceError(
                    "浏览器服务把连接关了（服务退出了？看 %s）" % self.log_path)
            self._buf += chunk

    def call(self, cmd, timeout=DEFAULT_TIMEOUT, **kw):
        """发一条命令、等它的响应，返回 ``result`` dict。

        ⚠️ kw 里不能出现 ``id``：信封的请求号叫 id，混了永远等不到响应。
        「哪一场 session」一律用 ``meetingId`` / ``startMs``。
        """
        if "id" in kw:
            raise ServiceError("内部错误：call() 的 kw 里不能有 id（会和请求号撞名）")
        with self._lock:
            if self.sock_obj is None:
                self.start()
                self._connect()
            req_id = self.next_id
            self.next_id += 1
            req = {"id": req_id, "cmd": cmd}
            req.update(kw)
            try:
                self.sock_obj.sendall((json.dumps(req) + "\n").encode("utf-8"))
            except OSError as e:
                self.close()
                raise ServiceError("发给浏览器服务失败（%s）：%s" % (self.sock, e))
            deadline = time.time() + float(timeout)
            while True:
                f = self._read_line(deadline)
                if f.get("event"):
                    self.events.append(f)
                    if f["event"] == "idle-closed":
                        self.last_idle_close = time.time()
                    if f["event"] == "fatal":
                        self.fatal = f.get("error")
                    continue
                if f.get("id") != req_id:
                    # 串行协议下不该发生；丢掉，别把别的请求的响应吞了
                    continue
                if not f.get("ok"):
                    raise ServiceError(str(f.get("error") or "未知错误"))
                return f.get("result") or {}

    # -- 语义封装

    def ping(self, timeout=15):
        return self.call("ping", timeout=timeout)

    def browser_up(self, timeout=15):
        """(服务在?, 浏览器开着?)。服务不在 **不** 拉起浏览器。"""
        try:
            r = self.ping(timeout=timeout)
        except ServiceError:
            return False, False
        return True, bool(r.get("browser"))

    def ensure_browser(self, timeout=START_TIMEOUT):
        """让服务把浏览器开起来，返回 ping 结果（Phase 2 里带 cdpUrl）。"""
        try:
            return self.call("ensure", timeout=timeout)
        except ServiceError:
            # 服务刚被 kill：重启一次再试（ensure 是幂等的）
            self.start()
            return self.call("ensure", timeout=timeout)

    def where(self, timeout=15):
        return self.call("where", timeout=timeout)

    def release(self, timeout=60):
        """关浏览器、放 profile 锁（服务留着）。"""
        return self.call("release", timeout=timeout)

    def hold(self, ms=600000, timeout=90):
        """登录/维护窗口：关浏览器并在 ms 内拒绝开浏览器。"""
        return self.call("hold", timeout=timeout, ms=int(ms))

    def unhold(self, timeout=30):
        return self.call("unhold", timeout=timeout)


def call(cmd, home=None, timeout=DEFAULT_TIMEOUT, **kw):
    """一次性调用（自己连接、自己断开），给不想管生命周期的脚本用。"""
    svc = Service(home=home)
    try:
        svc.connect()
        return svc.call(cmd, timeout=timeout, **kw)
    finally:
        svc.close()


def ensure_started(home=None, **kw):
    """确保服务在跑（幂等），返回门牌。"""
    return Service(home=home, **kw).start()


def _main(argv=None):
    """命令行：`python3 focusmate_service.py status|start|stop|call <cmd> …`"""
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "status"
    try:
        if action == "status":
            st = read_state(None)
            running = is_running(None)
            print(json.dumps({"running": running, "state": st}, indent=2, ensure_ascii=False))
            return 0 if running else 2
        if action == "start":
            print(json.dumps(Service().start(), indent=2, ensure_ascii=False))
            return 0
        if action == "stop":
            Service().shutdown()
            print("服务已退出")
            return 0
        if action == "call":
            cmd = argv[1]
            kw = {}
            for item in argv[2:]:
                if "=" in item:
                    k, v = item.split("=", 1)
                    try:
                        kw[k] = json.loads(v)
                    except ValueError:
                        kw[k] = v
            print(json.dumps(call(cmd, **kw), indent=2, ensure_ascii=False))
            return 0
        print("用法：status | start | stop | call <cmd> [k=v …]", file=sys.stderr)
        return 2
    except ServiceError as e:
        print("服务错误：%s" % e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(_main())
