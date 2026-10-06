"""TI_Extended 测试程序：book 一个 Focusmate session（默认今天 16:45，50 分钟）.

用法：
  python3 main.py                        # 今天 16:45，50 分钟
  python3 main.py --start 14:50          # 今天 14:50，50 分钟
  python3 main.py --start 14:50 --time 25# 今天 14:50，25 分钟
  python3 main.py --auth                 # 只登录（弹浏览器），不下单

说明：--start 是 24 小时制 HH:MM，默认指今天；若该时刻已过则自动顺延到明天。
原理：
- Focusmate 官方 API (X-API-Key) 是只读的，只能 list，不能 book。
- book 必须走 focusmate-mcp 的 browser automation (playwright, persistent context)。
- 所以本脚本用 stdlib 以 MCP Client 身份通过 stdio 调起 focusmate-mcp 的
  `book_session` tool，不依赖 `mcp` pip 包，Python 3.9 可直接跑。

前置：
1. focusmate-mcp 已装好 (./install.sh 装到 temp/，可用 FOCUSMATE_MCP_JS 覆盖路径)
2. 已跑过一次 focusmate_auth（需要 ~/.focusmate-mcp/browser-data/ 里有登录态），
   否则会返回 AUTH_REQUIRED。
3. token 仅用于 list/verify（只读 API），book 本身走浏览器 cookies，不走 token。
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request

SESSION_REGISTRATION_PATH = "data.csv"
TOKEN = "8e595973b02a43a58c7e3dbbba477777"
# MCP server 入口：默认读 install.py 装到 temp/ 的副本，可用环境变量覆盖。
# 旧的 /tmp/focusmate-mcp 只是系统临时目录（重启会丢），不要再用。
import os as _os

_HERE = _os.path.dirname(_os.path.abspath(__file__))
MCP_SERVER_JS = _os.environ.get(
    "FOCUSMATE_MCP_JS",
    _os.path.join(_HERE, "temp", "focusmate-mcp", "build", "index.js"),
)

DEFAULT_START = "16:45"


def resolve_target(start):
    """把 --start 解析成 (本地 target, 传给 MCP 的 UTC Zulu 字符串）。

    start 格式：24 小时制 "HH:MM"，如 "9:00" / "14:50" / "23:15"，默认指今天；
    若该时刻已过则自动顺延到明天，避免订到过去时间。
    必须是 15 分钟整点（:00/:15/:30/:45），否则 Focusmate 不认。
    """
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", start.strip())
    if not m:
        raise ValueError('时间格式不对，要 24 小时制 "HH:MM"，如 --start 14:50，你传的是 %r' % start)
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute % 15 != 0:
        raise ValueError("Focusmate 只认 15 分钟整点（:00/:15/:30/:45），%r 不行" % start)
    # 本地时区 (Asia/Hong_Kong = UTC+8，机器当前就是 CST/UTC+8)
    now = datetime.datetime.now().astimezone()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + datetime.timedelta(days=1)
    # zod z.string().datetime() 默认只认 UTC Zulu 格式，+08:00 会报 Invalid datetime，
    # 所以转成 2026-09-29T08:45:00.000Z 这种形式（16:45+08:00 == 08:45Z）
    start_iso = target.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return target, start_iso


def verify_token(target, start_iso):
    """用只读 API 验证 token 有效，顺便打印当天已有 sessions 防止冲突。"""
    print(f"[1/2] verify token, target={start_iso} (本地 {target.strftime('%m-%d %H:%M')})")
    req = urllib.request.Request(
        "https://api.focusmate.com/v1/me",
        headers={"X-API-Key": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        me = json.load(resp)
    print("  me:", me["user"].get("name"), me["user"].get("timeZone"))

    day_start = target.replace(hour=0, minute=0, second=0).astimezone(datetime.timezone.utc).isoformat()
    day_end = target.replace(hour=23, minute=59, second=59).astimezone(datetime.timezone.utc).isoformat()
    url = (
        "https://api.focusmate.com/v1/sessions"
        f"?start={urllib.parse.quote(day_start)}&end={urllib.parse.quote(day_end)}"
    )
    req = urllib.request.Request(url, headers={"X-API-Key": TOKEN})
    with urllib.request.urlopen(req, timeout=20) as resp:
        sessions = json.load(resp).get("sessions", [])
    print(f"  sessions that day: {len(sessions)}")
    for s in sessions:
        print("   -", s.get("startTime"), s.get("sessionId"))


def mcp_call(tool: str, arguments: dict, timeout: int = 180):
    """最小 MCP stdio client：initialize -> tools/call，全用 stdlib。"""
    proc = subprocess.Popen(
        ["node", MCP_SERVER_JS],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,  # MCP 日志走 stderr，直接透出
        text=True,
        bufsize=1,
    )
    seq = 0

    def rpc(method=None, params=None, is_notif=False, req_id=None):
        nonlocal seq
        msg = {"jsonrpc": "2.0"}
        if method:
            msg["method"] = method
        if params is not None:
            msg["params"] = params
        if not is_notif:
            msg["id"] = req_id if req_id is not None else seq
            seq += 1
        line = json.dumps(msg)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        return msg.get("id")

    def read_one(req_id):
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == req_id:
                return msg
            # server 发来的 request/notification（如 ping/roots），暂忽略
            continue

    try:
        rid = rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ti-extend-test", "version": "0.1"},
        })
        res = read_one(rid)
        if "error" in res:
            raise RuntimeError(f"initialize failed: {res['error']}")
        rpc("notifications/initialized", {}, is_notif=True)
        rid2 = rpc("tools/call", {"name": tool, "arguments": arguments})
        # tools/call 可能很慢（要起浏览器），逐行等直到拿到对应 id
        import time
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"tools/call {tool} timeout {timeout}s")
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout during tools/call")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == rid2:
                if "error" in msg:
                    raise RuntimeError(f"tools/call error: {msg['error']}")
                return msg.get("result", {})
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def book_once(start_iso, duration):
    """调一次 book_session，返回解析后的 payload。"""
    print(f"[book] book_session startTime={start_iso} duration={duration}")
    result = mcp_call("book_session", {"startTime": start_iso, "duration": duration})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # book_session 返回 content[0].text 里是 JSON: {success, session / error, errorCode}
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:
        payload = {}
    return payload


def do_auth():
    """触发 focusmate_auth，会弹出 Chromium 让你手动登录，5 分钟内完成即可。

    返回 True 表示登录成功，False 表示失败/超时。
    """
    print("[auth] 检测到未登录，自动拉起浏览器，请在弹出的窗口里登录 Focusmate（5分钟有效）...")
    result = mcp_call("focusmate_auth", {"force": True}, timeout=360)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:
        payload = {}
    ok = bool(payload.get("success"))
    print("[auth] %s" % ("登录成功" if ok else "登录失败/超时"))
    return ok


def main(start, duration):
    try:
        target, start_iso = resolve_target(start)
    except ValueError as e:
        print("参数错误：%s" % e)
        sys.exit(2)
    verify_token(target, start_iso)
    payload = book_once(start_iso, duration)
    if payload.get("success"):
        print("BOOK OK:", payload["session"])
        return
    # 没登录就自动跑一次 auth，然后重试一次 book，不用手动加 --auth
    if payload.get("errorCode") == "AUTH_REQUIRED":
        print("BOOK FAILED: 未登录，自动跑 auth 后重试一次...")
        if not do_auth():
            print(">> auth 没成功，手动跑 python3 main.py --auth 再试")
            sys.exit(1)
        payload = book_once(start_iso, duration)
        if payload.get("success"):
            print("BOOK OK:", payload["session"])
            return
    print("BOOK FAILED:", payload.get("error"), payload.get("errorCode"))
    sys.exit(1)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="book 一个 Focusmate session")
    ap.add_argument("--start", default=DEFAULT_START,
                    help='开始时间，24 小时制 "HH:MM"，默认指今天（已过则顺延明天）。如 --start 14:50')
    ap.add_argument("--time", default="50", choices=["25", "50", "75"],
                    help="时长（分钟），默认 50")
    ap.add_argument("--auth", action="store_true", help="只登录（弹浏览器），不下单")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.auth:
        do_auth()
    else:
        main(args.start, args.time)
