#!/usr/bin/env python3
"""settings_store.py —— TUI 的设置项（每个都有 id / 默认值 / 一句人话解释）。

设置是**数据**，不是散落在 warden 里的 if：每一项都在 :data:`SETTINGS` 里注册，
界面上把全部设置（哪怕用户从没改过）连同解释一起列出来，改的时候写
``set <id> <value>``。存到 ``data/settings.json``，只存**改过的**项 ——
默认值改了以后，没动过的项自然跟着新默认走。

现有设置
--------
``edit-tomorrow``
    今天可以先安排明天（以及之后）的 session。开着的时候，选中「明天」这一天时
    它按草稿区处理 —— 晚上 11 点想排明天早上 10 点，不用等滑进 24 小时窗口，
    也不用敲宣言。过了凌晨，明天变成今天，就照常走 24 小时窗口那套规则。

``tomorrow-plan``
    「明日时间安排」：一个**每周重复任务**（``mon@22:15`` 这种形状），到点了
    自动挂到那一周的对应 session 上。空 = 没设置（于是状态栏会提醒你设置）。

``omnifocus``
    和 OmniFocus 联动。**只有单向**（warden → OmniFocus）：完成/放弃写回去、
    收尾时读完成状态；OmniFocus 里的改动不会回流。off = 一个字节都不碰。

``postsession``
    session 结束 15 秒后把 warden 拉到前台收尾（见 ``postsession.py``）。

这个模块不联网、不依赖 rich；warden 和测试都只调这里。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

#: 每周重复用的星期几写法（和 data.csv 一样）
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

TRUTHY = {"on", "true", "1", "yes", "y", "开", "是"}
FALSY = {"off", "false", "0", "no", "n", "关", "否"}

#: 设置项注册表。``values`` 给补全和校验用；``type`` 决定怎么校验。
SETTINGS = [
    {
        "id": "edit-tomorrow",
        "type": "bool",
        "default": "on",
        "values": ("on", "off"),
        "summary": "今天可以先安排明天的 session",
        "desc": "开着时，选中「明天」按草稿区处理（晚上 11 点也能随便排明天早上 10 点）；"
                "过了凌晨就照常走 24 小时窗口。",
    },
    {
        "id": "omnifocus",
        "type": "bool",
        "default": "on",
        "values": ("on", "off"),
        "summary": "和 OmniFocus 联动（只单向：warden → OmniFocus）",
        "desc": "开着 = 任务完成/放弃会写回 OmniFocus，收尾时读 OmniFocus 的完成状态；"
                "off = 完全不碰 OmniFocus（收尾照常问你，只是不写不读）。",
    },
    {
        "id": "postsession",
        "type": "bool",
        "default": "on",
        "values": ("on", "off"),
        "summary": "session 结束 15 秒后拉 warden 到前台收尾",
        "desc": "开着 = 每场结束后 15 秒把 warden 拉到前台，逐个问没做完的任务"
                "（A 延期 / B 放弃 / C 重议 / D 缺漏完成 / E 快捷推后）；"
                "off = 不拉、不催（`review` 仍然可以手动开始）。",
    },
    {
        "id": "tomorrow-plan",
        "type": "repeat",
        "default": "",
        "values": (),
        "summary": "明日时间安排（每周重复任务）",
        "desc": "形如 mon@22:15 = 每周一 22:15 那场 session 自动挂上「明日时间安排」。"
                "空 = 没设置。改完用 task create title=明日时间安排 --repeat mon@22:15 建档。",
    },
]

BY_ID = {item["id"]: item for item in SETTINGS}


def settings_path(path=None):
    return path or os.environ.get("TI_SETTINGS_FILE") or DEFAULT_SETTINGS_FILE


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- 取值校验


def parse_weekday(raw):
    """'mon' / '周一' / 'monday' / 0..6 → 0..6；认不出返回 None。"""
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return n if 0 <= n <= 6 else None
    for i, name in enumerate(WEEKDAYS):
        if s == name or s.startswith(name):
            return i
    for i, name in enumerate(WEEKDAY_CN):
        if s == name:
            return i
    return None


def parse_repeat_spec(raw):
    """``mon@22:15`` → ``{"weekday": 0, "weekday_label": "mon", "start": "22:15"}``。

    也接受 ``22:15`` 和 ``周一 22:15`` / ``mon 22:15``。空串返回 None。
    """
    s = " ".join(str(raw or "").split())
    if not s:
        return None
    if "@" in s:
        left, right = s.split("@", 1)
    else:
        bits = s.split()
        if len(bits) != 2:
            raise ValueError("重复任务要写成 周几@HH:MM（例如 mon@22:15）；你给的是 %r" % raw)
        left, right = bits
    weekday = parse_weekday(left)
    if weekday is None:
        raise ValueError("星期几认不出来：%r（mon..sun / 周一..周日）" % left)
    m = re.match(r"^(\d{1,2}):(\d{2})$", right.strip())
    if not m:
        raise ValueError("时间要写成 HH:MM（例如 22:15）；你给的是 %r" % right)
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间超出范围：%r" % right)
    if minute % 15:
        raise ValueError("Focusmate 只认 15 分钟整点（:00/:15/:30/:45），%r 不行" % right)
    return {"weekday": weekday, "weekday_label": WEEKDAYS[weekday],
            "start": "%02d:%02d" % (hour, minute)}


def coerce_value(setting_id, raw):
    """把用户敲的 value 变成这个设置项认的字符串；不合法抛 ValueError。"""
    item = BY_ID.get(str(setting_id))
    if item is None:
        raise ValueError("没有设置项 %r（现有：%s）"
                         % (setting_id, "、".join(i["id"] for i in SETTINGS)))
    text = " ".join(str(raw or "").split())
    if item["type"] == "bool":
        low = text.lower()
        if low in TRUTHY:
            return "on"
        if low in FALSY:
            return "off"
        raise ValueError("%s 只认 on/off；你给的是 %r" % (item["id"], raw))
    if item["type"] == "repeat":
        if not text or text.lower() in ("off", "none", "clear", "空"):
            return ""
        spec = parse_repeat_spec(text)
        return "%s@%s" % (spec["weekday_label"], spec["start"])
    return text


def describe(setting_id):
    item = BY_ID[setting_id]
    return item.get("desc") or item.get("summary") or ""


# ---------------------------------------------------------------- 存储


class SettingStore(object):
    """改过的设置存 JSON；没改过的（以及其他所有项）读 default。"""

    def __init__(self, path=None):
        self.path = settings_path(path)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
        return {str(k): str(v) for k, v in (values or {}).items() if str(k) in BY_ID}

    def save(self, values):
        _atomic_write(self.path, {"version": 1, "values": dict(values)})

    # -- 读 -----------------------------------------------------------
    def get(self, setting_id, default=None):
        if setting_id not in BY_ID:
            raise ValueError("没有设置项 %r" % (setting_id,))
        values = self.load()
        if setting_id in values:
            return values[setting_id]
        return default if default is not None else BY_ID[setting_id]["default"]

    def get_bool(self, setting_id):
        return str(self.get(setting_id)).strip().lower() in TRUTHY

    def is_set(self, setting_id):
        """用户有没有主动改过（界面用它标 "改过的"）。"""
        return setting_id in self.load()

    def rows(self):
        """给界面用：每一项的 id / 当前值 / 默认值 / 是否改过 / 解释。"""
        values = self.load()
        out = []
        for item in SETTINGS:
            current = values.get(item["id"], item["default"])
            out.append({
                "id": item["id"],
                "value": current,
                "value_display": current or "（未设置）",
                "default": item["default"] or "（空）",
                "changed": item["id"] in values,
                "type": item["type"],
                "summary": item.get("summary") or "",
                "desc": item.get("desc") or "",
                "values": item.get("values") or (),
            })
        return out

    # -- 写 -----------------------------------------------------------
    def set(self, setting_id, raw):
        value = coerce_value(setting_id, raw)
        values = self.load()
        if value == BY_ID[setting_id]["default"]:
            # 回到默认值就等于没改过：以后默认变了能跟着走
            values.pop(setting_id, None)
        else:
            values[setting_id] = value
        self.save(values)
        return value

    def reset(self, setting_id):
        if setting_id not in BY_ID:
            raise ValueError("没有设置项 %r" % (setting_id,))
        values = self.load()
        values.pop(setting_id, None)
        self.save(values)
        return BY_ID[setting_id]["default"]


_DEFAULT = None


def _store():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SettingStore()
    return _DEFAULT


def get(setting_id, default=None):
    return _store().get(setting_id, default)


def get_bool(setting_id):
    return _store().get_bool(setting_id)


def cmd_show(argv=None):
    """CLI：列出全部设置（和界面里看到的一样）。"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    store = SettingStore()
    if argv and argv[0] == "set":
        if len(argv) < 3:
            print("用法：settings.py set <id> <value>")
            return 2
        try:
            value = store.set(argv[1], " ".join(argv[2:]))
        except ValueError as e:
            print("❌ %s" % e)
            return 1
        print("✅ %s = %s" % (argv[1], value or "（未设置）"))
        return 0
    for row in store.rows():
        mark = "*" if row["changed"] else " "
        print("%s %-14s = %-14s (默认 %s)" % (mark, row["id"], row["value_display"],
                                              row["default"]))
        print("    %s" % row["desc"])
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_show())
