#!/usr/bin/env python3
"""runtime_log.py —— 标准库 logging + 按天落盘的 JSONL。

为什么单独一个模块
------------------
warden 的 TUI 消息区只留最近几条（屏幕就那么大），排障时真正需要的是
「今天到底拉了哪些 Focusmate task、哪一条被判定完成、OmniFocus 写回到底
返回了什么」。这些事实不能只存在内存里。

为什么用标准库 logging
----------------------
以前这里只有 ``log_event()`` 一个函数 + 自己拼 JSON，没有等级、没有过滤、
没有办法区分「每 5 分钟都要说的同一句话」和「第一次出现的失败」。现在：

* logger 树 ``ti`` / ``ti.<area>``，用 logging 原生等级
  （DEBUG / INFO / WARNING / ERROR / CRITICAL）；
* 一条自定义 Handler 把记录写成 **JSONL，按本地日期分文件**
  ``temp/logs/YYYY-MM-DD.jsonl``（可用 ``TI_LOG_DIR`` 改目录）；
* 级别由 ``TI_LOG_LEVEL`` 控制（默认 INFO：DEBUG 的「拉取开始/结束」这类
  纯流程事件默认不进文件，排障时把它调成 DEBUG 再复现一次）；
* ``log_change()``：内容没变就**不写**，变了才写一条，并带上
  「这中间同样的结果出现了 N 次」——高频轮询的成功快照不该刷屏；
* ``log_event()`` 保留原来的签名（``now=`` / ``path=``），29 个老调用点
  不用批量改，直接原地加 ``level=`` 即可。

每行一个 JSON object，字段：

    {"ts": "...", "level": "INFO", "area": "focusmate", "event": "...", ...}

写入失败绝不影响主流程（日志是旁路）。不要往这里写 token / cookie / 密码：
``_plain()`` 会把名字里带 token/cookie/password/secret 的字段值隐掉，超长
文本也会截断；但这只是兜底，不是许可证。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = os.path.join(HERE, "temp", "logs")

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_TRUNCATE = 4000

KEEP_DAYS_ENV = "TI_LOG_KEEP_DAYS"      # 保留几天；<=0 表示不清理
DEFAULT_KEEP_DAYS = 14
_ROOT_NAME = "ti"
_setup_lock = threading.Lock()
_setup_done = False
_active_base = None         # setup() 定的日志目录（handler 用同一个）
_active_level = None        # setup() 定下的等级；之后再 setup() 不带 level 就沿用它
_handler = None            # 当前那个天天切文件的 JSONL handler（测试要换目录）


# --------------------------------------------------------------------------
# 公共小工具
# --------------------------------------------------------------------------

def log_dir(path=None):
    return path or os.environ.get("TI_LOG_DIR") or DEFAULT_LOG_DIR


def today_path(now=None, path=None):
    day = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    return os.path.join(log_dir(path), day + ".jsonl")


def level_of(value, default=logging.INFO):
    """``"warning"`` / ``"WARN"`` / ``logging.WARNING`` / ``30`` → 等级数字。"""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    return LEVELS.get(text, default)


def keep_days():
    """读 ``TI_LOG_KEEP_DAYS``；没设 / 不是数字就用默认 14 天。"""
    raw = os.environ.get(KEEP_DAYS_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_KEEP_DAYS
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS


def prune(days=None, path=None, now=None):
    """删掉 ``days`` 天以前的 ``YYYY-MM-DD.jsonl``，返回被删的文件名列表。

    只认 ``YYYY-MM-DD.jsonl`` 这种**能解析成日期**的名字：目录里要是混进了别的
    东西（`notes.txt`、手写的 `2026-13-99.jsonl`）一律不碰。删不掉（权限/被占用）
    就跳过，绝不抛异常 —— 日志清理同样只是旁路。
    """
    try:
        keep = keep_days() if days is None else int(days)
    except (TypeError, ValueError):
        return []
    if keep <= 0:
        return []
    base = log_dir(path)
    cutoff = (now or datetime.datetime.now()) - datetime.timedelta(days=keep)
    cutoff_day = cutoff.date()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    removed = []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext != ".jsonl":
            continue
        try:
            day = datetime.date.fromisoformat(stem)
        except ValueError:
            continue
        if day >= cutoff_day:
            continue
        try:
            os.remove(os.path.join(base, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def _plain(value, depth=0):
    """把任意对象收成可 JSON 序列化的形状；截断超长文本，避免日志无限膨胀。"""
    if depth > 6:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        return text if len(text) <= _TRUNCATE else text[:_TRUNCATE] + "…（截断）"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:200]:
            key_text = str(key)
            if any(secret in key_text.lower()
                   for secret in ("token", "cookie", "password", "secret")):
                out[key_text] = "（已隐藏）"
            else:
                out[key_text] = _plain(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [_plain(item, depth + 1) for item in items[:200]]
    return _plain(str(value), depth + 1)


# --------------------------------------------------------------------------
# JSONL handler / formatter
# --------------------------------------------------------------------------

class JsonlFormatter(logging.Formatter):
    """把一条 LogRecord 拍成一行 JSON：ts / level / area / event + 业务字段。"""

    def format(self, record):
        payload = {
            "ts": getattr(record, "ti_ts", None),
            "level": record.levelname,
            "area": getattr(record, "ti_area", ""),
            "event": getattr(record, "ti_event", ""),
        }
        payload.update(getattr(record, "ti_fields", None) or {})
        payload = _plain(payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class DailyJsonlHandler(logging.Handler):
    """按 ``record.ti_ts`` 的本地日期切文件；写失败只吞掉异常，绝不抛给业务。"""

    def __init__(self, path=None, fsync=None):
        super().__init__()
        self.base = path
        if fsync is None:
            fsync = os.environ.get("TI_LOG_FSYNC", "1") not in ("0", "false", "no")
        self.fsync = bool(fsync)
        self._handle = None
        self._current = None

    def emit(self, record):
        try:
            line = self.format(record)
            day = str(getattr(record, "ti_ts", "") or "")[:10]
            target = os.path.join(log_dir(self.base),
                                  (day or datetime.date.today().isoformat()) + ".jsonl")
            if target != self._current:
                self._close()
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                self._handle = open(target, "a", encoding="utf-8")
                self._current = target
            self._handle.write(line + "\n")
            self._handle.flush()
            if self.fsync:
                os.fsync(self._handle.fileno())
        except Exception:  # noqa: BLE001 - 日志失败不能影响业务
            self._close()

    def _close(self):
        handle, self._handle, self._current = self._handle, None, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        self._close()
        super().close()


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------

def setup(level=None, path=None, stderr=False):
    """装配 ``ti`` logger（幂等）。返回实际生效的等级。

    ``level`` 优先，其次环境变量 ``TI_LOG_LEVEL``，最后 INFO。
    ``path`` 只给测试用（默认 ``TI_LOG_DIR`` / ``temp/logs``）。
    ``stderr=True`` 时额外把 WARNING 以上吐到 stderr（短跑脚本排障用；
    TUI 里别开，会把界面弄花）。
    """
    global _setup_done, _active_base, _active_level, _handler
    with _setup_lock:
        root = logging.getLogger(_ROOT_NAME)
        root.propagate = False
        if level is None:
            level = _active_level
        if level is None:
            level = os.environ.get("TI_LOG_LEVEL") or logging.INFO
        lvl = level_of(level)
        _active_level = lvl
        root.setLevel(lvl)          # 子 logger 不设 level，effective level 继承这里
        _active_base = path
        if _setup_done and path is not None and _handler is not None and _handler.base != path:
            # 测试/短脚本换目录：复用同一个 handler，只改落点，别叠第二个。
            _handler._close()
            _handler.base = path
        if not _setup_done:
            handler = DailyJsonlHandler(path)
            _handler = handler
            handler.setFormatter(JsonlFormatter())
            root.addHandler(handler)
            if stderr:
                err = logging.StreamHandler()
                err.setLevel(logging.WARNING)
                err.setFormatter(logging.Formatter("[ti:%(levelname)s] %(message)s"))
                root.addHandler(err)
            logging.raiseExceptions = False
            _setup_done = True
            # 保留期清理：每个进程启动时做一次（写失败/删失败都不影响业务）。
            prune(path)
        return lvl


def get_logger(area=""):
    """取 ``ti`` 树下的 logger（area 作后缀，便于按模块过滤）。"""
    setup()
    name = _ROOT_NAME + ("." + str(area) if area else "")
    return logging.getLogger(name)


def set_level(level):
    """运行期改级别（TUI 里开「调试日志」用）。"""
    lvl = level_of(level)
    setup(level=lvl)
    return lvl


# 兼容旧名：外面有代码写 ``rl.log_event(...)``，也有写 ``rl.setup`` 的。
def configure(level=None, path=None, stderr=False):
    return setup(level=level, path=path, stderr=stderr)


# --------------------------------------------------------------------------
# 写日志
# --------------------------------------------------------------------------

def _record(area, event, level, fields, when):
    return {
        "ts": when.isoformat(timespec="milliseconds"),
        "level": logging.getLevelName(level) if isinstance(level, int) else str(level),
        "area": str(area),
        "event": str(event),
        **fields,
    }


def _append_direct(area, event, level, fields, when, path):
    """``path=`` 给测试/短脚本用：直接拼一行写进去，不过 logger、不过级别过滤。"""
    record = _record(area, event, level, fields, when)
    try:
        record = _plain(record)
        target = today_path(when, path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return target
    except Exception:  # noqa: BLE001 - 日志失败不能影响业务
        return None


def log_event(area, event, level=logging.INFO, now=None, path=None, **fields):
    """追加一行日志，返回写入路径（失败返回 None）。永不抛异常。

    ``level`` 接受 logging 常量或 ``"debug"/"info"/"warning"/"error"/"critical"``。
    低于 ``TI_LOG_LEVEL`` 的记录直接丢弃（不拼 JSON、不碰磁盘）。
    """
    when = now or datetime.datetime.now()
    lvl = level_of(level)
    if path is not None:
        return _append_direct(area, event, lvl, fields, when, path)
    try:
        logger = get_logger(area)
        if not logger.isEnabledFor(lvl):
            return None
        logger.log(lvl, "", extra={
            "ti_ts": when.isoformat(timespec="milliseconds"),
            "ti_area": str(area),
            "ti_event": str(event),
            "ti_fields": fields,
        })
        return os.path.join(log_dir(_active_base),
                            when.strftime("%Y-%m-%d") + ".jsonl")
    except Exception:  # noqa: BLE001 - 日志失败不能影响业务
        return None


# --------------------------------------------------------------------------
# 只在变化时记录（高频轮询用）
# --------------------------------------------------------------------------

_change_lock = threading.Lock()
_change_state = {}          # (area, event, key) -> {"sig": str, "skipped": int}


def reset_change_state(key=None):
    """忘掉「上次记了什么」。测试用；运行期一般不需要。"""
    with _change_lock:
        if key is None:
            _change_state.clear()
        else:
            _change_state.pop(key, None)


def log_change(area, event, level=logging.INFO, key=None, now=None, path=None,
               heartbeat=0, volatile=(), **fields):
    """``log_event`` 的降噪版：字段内容和上次**完全一样**就不写。

    * ``key`` 区分同一 event 的多个对象（比如按 task id、按 source）；
      不传就是「这个 event 只有一路」。
    * 内容变了 → 立刻写，并带上 ``changed=true``；
    * 内容没变 → 累计 ``skipped`` 次数，等下次变化时一起写出来
      （``skipped_same=N`` 表示「这 N 轮结果一模一样」）；
    * ``volatile``：不参与「变没变」比较、但照样写进行的字段（``elapsed_ms``
      这类每次都不同的耗时数字，放进签名会让降噪彻底失效）。
    * ``heartbeat``（秒）>0 时，就算一直没变也每隔这么久写一条心跳
      （``heartbeat=true``），这样「daemon 还活着但没活了」能看出来。
      默认 0 = 纯变化触发。

    返回写入路径（没写返回 None）。
    """
    when = now or datetime.datetime.now()
    lvl = level_of(level)
    if path is None and not get_logger(area).isEnabledFor(lvl):
        return None                     # 级别关着就别算签名了（高频路径）
    ck = (str(area), str(event), None if key is None else str(key))
    volatile = tuple(volatile or ())
    stable = {k: v for k, v in fields.items() if k not in volatile}
    signature = json.dumps(_plain(stable), sort_keys=True, ensure_ascii=False,
                           default=str)
    with _change_lock:
        last = _change_state.get(ck)
        if last is not None and last["sig"] == signature:
            last["skipped"] += 1
            if not heartbeat:
                return None
            elapsed = (when - last["at"]).total_seconds()
            if elapsed < heartbeat:
                return None
            heartbeat_hit = True
        else:
            heartbeat_hit = False
        skipped = last["skipped"] if last else 0
        _change_state[ck] = {"sig": signature, "skipped": 0, "at": when}
    extra = dict(fields)
    extra["changed"] = True
    if heartbeat_hit:
        extra["heartbeat"] = True
    if skipped:
        extra["skipped_same"] = skipped
    return log_event(area, event, level=lvl, now=when, path=path, **extra)
