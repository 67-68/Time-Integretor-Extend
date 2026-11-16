"""Focusmate Chromium profile 的跨进程互斥锁（stdlib，无依赖）。

为什么需要（2026-11-16 真机实测）
--------------------------------
同一个 user-data-dir 被两个 Chromium 同时打开时，第二个的 dashboard 会白屏
（`SPA did not mount`，bodyLen=0），IndexedDB 报
`Internal error opening backing store`。`chrome-headless-shell` **不写
SingletonLock**，Chromium 根本不拦第二个实例 —— 于是：

- `keep_session.py daemon` 每 5 分钟自己起一个 Chromium 下单；
- TUI 的 `scripts/bridge.mjs` 每条命令后把浏览器留 120s（`WARDEN_BRIDGE_IDLE_MS`）；
- 只有 warden 自己触发 `book()` / `start` 时才让 bridge 让位，daemon 的定时
  轮次不让位。

两者天天撞，撞一次白屏一次，LevelDB 在争抢下回放日志，`firebase:authUser`
记录被抹掉 → 页面跳 /login → 用户看到「登录态又过期了」。

约定
----
- **谁要开浏览器，谁先拿锁**：`<home>/.profile-lock.json`
  （`pid` / `role` / `ts` / `host`）。
- 拿不到锁就写 `<home>/.profile-want.json` 并等：持锁方（bridge）看到 want
  会立刻关浏览器让位，所以正常等待是 1～2 秒，不用等满 120 秒空闲。
- 持锁进程死了，或超过 `stale_ms` 没动静 → 视为死锁，直接抢。
- 抢到锁以后，如果还有陌生 Chromium 开着这个 profile（上一轮泄漏的孤儿），
  直接杀掉：锁在我手上，任何别的浏览器都属于孤儿。

node 侧有一份行为完全一致的实现：`scripts/profile_lock.mjs`（文件名、判据、
TTL 都对齐；改这里必须同步改那边）。
"""
import contextlib
import errno
import json
import os
import signal
import socket
import subprocess
import time

LOCK_NAME = ".profile-lock.json"
WANT_NAME = ".profile-want.json"

DEFAULT_WAIT_MS = 60_000        # 拿不到锁最多等这么久（bridge 让位通常 <2s）
DEFAULT_STALE_MS = 900_000      # 15 分钟没动静 = 死锁（auth 最长 5 分钟，留足）
WANT_TTL_MS = 20_000            # want 标记 20 秒后自动失效，避免别人一直等
POLL_MS = 250

#: home -> [info, depth]，让同一进程里嵌套 acquire 不会自己把自己挡死。
_HELD = {}


class ProfileBusy(RuntimeError):
    """等不到 profile 锁（有活进程占着）。"""

    def __init__(self, holder, waited_ms):
        self.holder = holder or {}
        self.waited_ms = waited_ms
        super(ProfileBusy, self).__init__(
            "%s（等了 %.1fs）" % (describe_holder(holder), waited_ms / 1000.0))


def profile_dir(home):
    """profile 目录：`FOCUSMATE_PROFILE_DIR` 优先，否则 `<home>/browser-data`。"""
    override = (os.environ.get("FOCUSMATE_PROFILE_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.abspath(home), "browser-data")


def _path(home, name):
    return os.path.join(os.path.abspath(home), name)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_lock(home):
    """当前持锁信息（dict）或 None。过期的也照样返回，由调用方判断。"""
    return _read_json(_path(home, LOCK_NAME))


def read_want(home):
    """还没过期的 want 标记，或 None。"""
    info = _read_json(_path(home, WANT_NAME))
    if not info:
        return None
    try:
        age_ms = (time.time() - float(info.get("ts") or 0)) * 1000
    except (TypeError, ValueError):
        return None
    return info if age_ms <= WANT_TTL_MS else None


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
        return e.errno == errno.EPERM      # 活着，只是不是我的
    return True


def holder_is_stale(info, stale_ms=DEFAULT_STALE_MS):
    if not info:
        return True
    try:
        ts = float(info.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    age_ms = (time.time() - ts) * 1000
    if age_ms > stale_ms:
        return True
    host = info.get("host")
    if host and host != socket.gethostname():
        return False                      # 别的机器？不判死，只按超时算
    return not pid_alive(info.get("pid"))


def describe_holder(info):
    if not info:
        return "没有进程持锁"
    try:
        held = max(0.0, time.time() - float(info.get("ts") or 0))
    except (TypeError, ValueError):
        held = 0.0
    return "pid %s 的 %s（已持锁 %.0fs）" % (
        info.get("pid"), info.get("role") or "?", held)


def write_want(home, role, wait_ms=DEFAULT_WAIT_MS):
    """告诉持锁方「有人要这个 profile，请让位」。失败不影响等待。"""
    info = {"pid": os.getpid(), "role": role, "ts": time.time(),
            "host": socket.gethostname(), "wait_ms": wait_ms}
    tmp = _path(home, WANT_NAME) + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, _path(home, WANT_NAME))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def clear_want(home):
    try:
        os.remove(_path(home, WANT_NAME))
    except OSError:
        pass


def find_profile_browsers(profile_dir_path):
    """开着这个 profile 的 Chromium **主进程** pid 列表（子进程带 --type=，跳过）。"""
    target = "--user-data-dir=%s" % os.path.abspath(profile_dir_path)
    try:
        out = subprocess.run(
            ["pgrep", "-f", "--", target],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for tok in out.split():
        if not tok.isdigit():
            continue
        try:
            cmd = subprocess.run(
                ["ps", "-o", "command=", "-p", tok],
                capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "--type=" in cmd:
            continue
        pids.append(int(tok))
    return pids


def kill_pids(pids, grace=3.0):
    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            continue
    deadline = time.time() + grace
    while time.time() < deadline and any(pid_alive(p) for p in killed):
        time.sleep(0.1)
    for pid in killed:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return killed


def kill_orphan_browsers(profile_dir_path, quiet=False):
    """杀掉占着 profile 的孤儿 Chromium。**只在拿到锁以后调用。**"""
    pids = find_profile_browsers(profile_dir_path)
    if not pids:
        return []
    killed = kill_pids(pids)
    if killed and not quiet:
        print("[lock] 清掉占着 profile 的孤儿浏览器：%s" % " ".join(str(p) for p in killed))
    return killed


def acquire(home, role, wait_ms=DEFAULT_WAIT_MS, stale_ms=DEFAULT_STALE_MS,
            poll_ms=POLL_MS, quiet=False, kill_orphans=True):
    """拿到锁，返回持锁信息。拿不到抛 :class:`ProfileBusy`。"""
    home = os.path.abspath(home)
    os.makedirs(home, exist_ok=True)
    path = _path(home, LOCK_NAME)
    started = time.time()
    deadline = started + wait_ms / 1000.0
    me = {"pid": os.getpid(), "role": role, "ts": time.time(),
          "host": socket.gethostname()}
    poked = False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(me, f)
            clear_want(home)
            if kill_orphans:
                kill_orphan_browsers(profile_dir(home), quiet=quiet)
            if poked and not quiet:
                print("[lock] 拿到 profile 锁（等了 %.1fs）"
                      % (time.time() - started))
            return me

        holder = read_lock(home)
        if holder_is_stale(holder, stale_ms):
            # 死锁：先删再抢；真抢不到就继续循环
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if not poked:
            write_want(home, role, wait_ms)
            poked = True
            if not quiet:
                print("[lock] profile 被 %s 占着，请它让位…" % describe_holder(holder))
        if time.time() >= deadline:
            raise ProfileBusy(holder, (time.time() - started) * 1000)
        time.sleep(poll_ms / 1000.0)


def release(home, info=None, quiet=True):
    """放锁；只删自己的锁（pid 对得上）。"""
    path = _path(home, LOCK_NAME)
    current = read_lock(home)
    if not current:
        return False
    me = info or {}
    if me.get("pid") and current.get("pid") != me.get("pid"):
        return False
    if current.get("pid") != os.getpid():
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    if not quiet:
        print("[lock] 放掉 profile 锁")
    return True


@contextlib.contextmanager
def profile_lock(home, role, wait_ms=DEFAULT_WAIT_MS, stale_ms=DEFAULT_STALE_MS,
                 quiet=False):
    """`with profile_lock(home, "mcp:book_session"): ...`

    同一进程嵌套调用只算最外层（depth），不会自己挡自己。
    """
    home = os.path.abspath(home)
    slot = _HELD.get(home)
    if slot is not None:
        slot[1] += 1
        try:
            yield slot[0]
        finally:
            slot[1] -= 1
            if slot[1] <= 0:
                _HELD.pop(home, None)
                release(home, slot[0], quiet=True)
        return

    info = acquire(home, role, wait_ms=wait_ms, stale_ms=stale_ms, quiet=quiet)
    _HELD[home] = [info, 1]
    try:
        yield info
    finally:
        held = _HELD.pop(home, None)
        if held is not None:
            release(home, held[0], quiet=True)
