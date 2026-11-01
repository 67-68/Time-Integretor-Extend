#!/usr/bin/env python3
"""runtime_log.py —— 按天落盘的 JSONL 运行日志。

为什么单独一个模块
------------------
warden 的 TUI 消息区只留最近几条（屏幕就那么大），排障时真正需要的是
「今天到底拉了哪些 Focusmate task、哪一条被判定完成、OmniFocus 写回到底
返回了什么」。这些事实不能只存在内存里。

日志按**本地日期**分文件：

    temp/logs/YYYY-MM-DD.jsonl

每行一个 JSON object，至少包含 ``ts`` / ``area`` / ``event``。写入失败绝不
影响主流程（日志是旁路）。可用 ``TI_LOG_DIR`` 改目录，测试用它指到临时目录。

不要把 token / cookie / 密码写进来；这里只记录任务 id、标题、状态、接口
返回摘要和错误。
"""
from __future__ import annotations

import datetime
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = os.path.join(HERE, "temp", "logs")
_LOCK = threading.Lock()


def log_dir(path=None):
    return path or os.environ.get("TI_LOG_DIR") or DEFAULT_LOG_DIR


def today_path(now=None, path=None):
    day = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    return os.path.join(log_dir(path), day + ".jsonl")


def _plain(value, depth=0):
    """把任意对象收成可 JSON 序列化的形状；截断超长文本，避免日志无限膨胀。"""
    if depth > 6:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        return text if len(text) <= 4000 else text[:4000] + "…（截断）"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:200]:
            key_text = str(key)
            if any(secret in key_text.lower() for secret in ("token", "cookie", "password", "secret")):
                out[key_text] = "（已隐藏）"
            else:
                out[key_text] = _plain(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [_plain(item, depth + 1) for item in items[:200]]
    return _plain(str(value), depth + 1)


def log_event(area, event, now=None, path=None, **fields):
    """追加一行日志，返回写入路径（失败返回 None）。永不抛异常。"""
    when = now or datetime.datetime.now()
    record = {
        "ts": when.isoformat(timespec="milliseconds"),
        "area": str(area),
        "event": str(event),
    }
    record.update(fields)
    try:
        record = _plain(record)
        target = today_path(when, path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOCK:
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return target
    except Exception:  # noqa: BLE001 - 日志失败不能影响业务
        return None
