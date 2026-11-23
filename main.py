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
2. 已跑过一次 focusmate_auth（需要 browser-data/ 里有登录态），否则返回 AUTH_REQUIRED。
   运行目录默认 `~/.focusmate-mcp`；本机沙箱禁写那里，所以 main.py 会自动切到
   `temp/focusmate-home`（FOCUSMATE_CONFIG_DIR），首次把旧 `~/.focusmate-mcp/browser-data`
   复制过去。Chromium profile 必须可写，否则 dashboard 会空白、所有下单失败。
3. token 仅用于 list/verify（只读 API），book 本身走浏览器 cookies，不走 token。
"""
import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import focusmate_lock as fm_lock
import focusmate_service as fm_service

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
# 登录态快照/自愈（scripts/auth_store.mjs）。登录成功后跑一次，把
# IndexedDB 里的 firebase:authUser 备份下来 —— 它被 app 删掉时能自动写回。
AUTH_STORE_JS = _os.path.join(_HERE, "scripts", "auth_store.mjs")

# 出错截图目录。focusmate-mcp 默认写到 ~/.focusmate-mcp/screenshots，但本机的
# dsh 沙箱禁止写该目录 —— 截图失败本身还会把真正的预定报错盖成 EPERM。指到
# 工作区内的 temp/（可写、且 temp/ 已在 .gitignore 里，截图含账号信息不外泄）。
# 显式设了 FOCUSMATE_SCREENSHOT_DIR 就以你的为准。
SCREENSHOT_DIR = _os.environ.get("FOCUSMATE_SCREENSHOT_DIR") or _os.path.join(
    _HERE, "temp", "focusmate-screenshots")
_os.environ["FOCUSMATE_SCREENSHOT_DIR"] = SCREENSHOT_DIR

# focusmate-mcp 的运行目录（browser-data / screenshots / config.json）。
# 本机 dsh 沙箱禁止写 ~/.focusmate-mcp/：Chromium profile 写不进去时 dashboard
# 会渲染成空白页（SPA 不挂载），这跟 slot 本身没关系，但会被当普通失败耗尽重试。
# 这里懒解析到一个工作区可写目录，并首次把旧登录态复制过去。
# 优先级：FOCUSMATE_CONFIG_DIR > FOCUSMATE_MCP_HOME > temp/focusmate-home。
DEFAULT_MCP_HOME = _os.path.join(_HERE, "temp", "focusmate-home")
LEGACY_MCP_HOME = _os.path.expanduser("~/.focusmate-mcp")


def mcp_home():
    """当前 MCP 运行目录：显式环境变量优先，否则用工作区可写目录。"""
    for key in ("FOCUSMATE_CONFIG_DIR", "FOCUSMATE_MCP_HOME"):
        val = (_os.environ.get(key) or "").strip()
        if val:
            return _os.path.abspath(_os.path.expanduser(val))
    return DEFAULT_MCP_HOME


def _ensure_writable_dir(path):
    try:
        _os.makedirs(path, exist_ok=True)
        probe = _os.path.join(path, ".write-probe-%d" % _os.getpid())
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        _os.remove(probe)
        return True
    except OSError as e:
        print("[mcp] 运行目录不可写：%s（%s）" % (path, e), file=sys.stderr)
        return False


def _seed_mcp_home(home, legacy=None):
    """第一次使用工作区目录时，把旧 ~/.focusmate-mcp 的登录态搬过来。"""
    legacy = legacy or LEGACY_MCP_HOME
    if _os.path.abspath(home) == _os.path.abspath(legacy):
        return
    src_browser = _os.path.join(legacy, "browser-data")
    dst_browser = _os.path.join(home, "browser-data")
    if _os.path.isdir(src_browser) and not _os.path.exists(dst_browser):
        tmp = dst_browser + ".tmp-%d" % _os.getpid()
        try:
            shutil.copytree(src_browser, tmp)
            _os.replace(tmp, dst_browser)
            print("[mcp] 已把登录态从 %s 迁到可写目录 %s" % (src_browser, dst_browser))
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            print("[mcp] 迁移 browser-data 失败：%s" % e, file=sys.stderr)
    for name in ("config.json", "cookies.json"):
        src = _os.path.join(legacy, name)
        dst = _os.path.join(home, name)
        if _os.path.isfile(src) and not _os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                print("[mcp] 迁移 %s 失败：%s" % (name, e), file=sys.stderr)


def ensure_mcp_home(home=None, legacy=None):
    """确保 MCP 运行目录可写，返回实际使用的路径。

    不可写时优先退回工作区默认目录，两个都不行才抛 RuntimeError。每次
    mcp_call 前调用，所以运行时改环境变量也生效（不缓存）。
    """
    env_override = any(
        (_os.environ.get(k) or "").strip()
        for k in ("FOCUSMATE_CONFIG_DIR", "FOCUSMATE_MCP_HOME"))
    if home is None:
        home = mcp_home()
    home = _os.path.abspath(home)
    if not _ensure_writable_dir(home):
        fallback = _os.path.abspath(DEFAULT_MCP_HOME)
        if fallback != home and _ensure_writable_dir(fallback):
            home = fallback
        else:
            raise RuntimeError(
                "Focusmate MCP 运行目录不可写：%s；请把 FOCUSMATE_MCP_HOME "
                "或 FOCUSMATE_CONFIG_DIR 指到一个可写目录" % home)
    if legacy is not None or not env_override:
        _seed_mcp_home(home, legacy=legacy)
    _os.environ["FOCUSMATE_CONFIG_DIR"] = home
    _os.environ["FOCUSMATE_SCREENSHOT_DIR"] = (
        _os.environ.get("FOCUSMATE_SCREENSHOT_DIR") or SCREENSHOT_DIR)
    return home


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


#: 每个工具拿 profile 锁最多等多久。bridge 让位通常 1-2 秒，60s 已经很宽松；
#: 登录虽然要人操作 5 分钟，但只需要先拿到锁，等 30 秒足够。
_LOCK_WAIT_MS = {"focusmate_auth": 30000, "book_session": 60000}


def mcp_call(tool: str, arguments: dict, timeout: int = 180, wait_ms=None):
    """最小 MCP stdio client：initialize -> tools/call，全用 stdlib。

    整个调用（起 node → 起 Chromium → 操作页面）都在 profile 锁里：同一个
    user-data-dir 绝不能被两个 Chromium 同时打开 —— 第二个会白屏，并在争抢中
    把 firebase:authUser 记录踩丢（看起来就像「登录态又过期了」）。见
    :mod:`focusmate_lock` 的头注释。
    """
    home = ensure_mcp_home()   # 确保 browser-data 可写，否则 dashboard 会空白
    # **共用浏览器**：ti_extend 的浏览器服务已经握着 profile 时，这里就不要再开一只
    # （两只 Chromium 抢同一个 profile 会白屏、还会踩坏 IndexedDB），直接用它的 CDP。
    cdp = shared_browser_cdp(tool, home)
    if cdp:
        print("[mcp] home=%s tool=%s 共用浏览器 %s（不自己起 Chromium、不抢锁）"
              % (home, tool, cdp))
        return _mcp_call_locked(tool, arguments, timeout, cdp=cdp)
    if wait_ms is None:
        wait_ms = _LOCK_WAIT_MS.get(tool, 30000)
    with fm_lock.profile_lock(home, "mcp:%s" % tool, wait_ms=wait_ms) as held:
        print("[mcp] home=%s tool=%s lock=%s"
              % (home, tool, fm_lock.describe_holder(held)))
        return _mcp_call_locked(tool, arguments, timeout)


def shared_browser_cdp(tool, home=None):
    """让浏览器服务把浏览器开起来并给出 CDP 地址；拿不到就返回 ""（退回自己开）。

    auth 例外：登录要 headed + 独占整个 profile，绝不能借用共用浏览器 ——
    调用方（:func:`do_auth`）会先让服务 hold/release。
    """
    if tool in ("focusmate_auth",):
        return ""
    try:
        svc = fm_service.Service(home=home or mcp_home())
        st = svc.ensure_browser(timeout=180) or {}
        url = str(st.get("cdpUrl") or "")
        if url:
            print("[mcp] 浏览器服务就绪（pid=%s, cdp=%s）" % (st.get("pid"), url))
        else:
            print("[mcp] 浏览器服务在，但没开 CDP（降级：自己起浏览器）")
        return url
    except Exception as e:  # noqa: BLE001 - 服务不可用不该拖垮下单路径
        print("[mcp] 共用浏览器不可用（%s）；这次自己开（要抢 profile 锁）" % e)
        return ""


def _mcp_call_locked(tool: str, arguments: dict, timeout: int = 180, cdp=""):
    """真正跑 MCP 协议；走共用浏览器时**不要**持有 profile 锁（锁在服务手上）。"""
    env = dict(_os.environ)
    if cdp:
        env["FOCUSMATE_CDP_URL"] = cdp
    else:
        env.pop("FOCUSMATE_CDP_URL", None)
    proc = subprocess.Popen(
        ["node", MCP_SERVER_JS],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,  # MCP 日志走 stderr，直接透出
        text=True,
        bufsize=1,
        env=env,
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
    try:
        result = mcp_call("book_session",
                          {"startTime": start_iso, "duration": duration})
    except fm_lock.ProfileBusy as e:
        # 这一条不算 slot 级失败：另一个浏览器占着 profile，等下一轮环境退避再来。
        print("[book] 拿不到 profile 锁：%s" % e)
        return {
            "success": False,
            "error": "另一个 Chromium 正占着 profile（%s），本轮没下单：%s"
                     % ("focusmate profile", e),
            "errorCode": "PROFILE_BUSY",
        }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # book_session 返回 content[0].text 里是 JSON: {success, session / error, errorCode}
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:
        payload = {}
    # 「白屏」这句话原本还说「profile 目录不可写」，实测最常发生的是**另一只
    # 浏览器正占着 profile**。现在有 focusmate_lock 互斥，正常不该再出现；
    # 真出现就照这句话查锁和进程。
    if payload.get("errorCode") == "AUTOMATION_FAILED":
        err = payload.get("error") or ""
        if "blank page" in err or "SPA did not mount" in err:
            payload["error"] = (
                "dashboard 白屏（SPA 没挂载）：profile 写不进去，或另一只 Chromium 正占着它。"
                "查 temp/focusmate-home/.profile-lock.json 与 "
                "`pgrep -f -- '--user-data-dir='`。")
    return payload


def snapshot_auth_state(timeout=150):
    """登录成功后立刻开一次浏览器核对 + 备份登录态。

    登录工具（focusmate-mcp）只负责把 Firebase 记录写进 IndexedDB；如果那条
    记录没落盘（或者第二天被 app 删掉），refresh token 就没了，只能人工重登。
    ``scripts/auth_store.mjs --snapshot`` 会：
      * 用同一个 profile 进 /dashboard，读 firebaseLocalStorage；
      * 读得到 → 写 ``temp/focusmate-home/auth-backup.json``（600 权限）；
      * 读不到 → 明确报「no-auth-record」，别让「登录成功」变成假象。

    这是旁路：它失败不改 do_auth 的结论。
    """
    env = dict(_os.environ)
    env["FOCUSMATE_CONFIG_DIR"] = mcp_home()
    try:
        proc = subprocess.run(
            ["node", AUTH_STORE_JS, "--snapshot"],
            cwd=_HERE, env=env, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - 备份失败不该拖垮登录流程
        print("[auth] 登录态备份没跑起来（%s）；登录本身可能还是好的。" % e)
        return None
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    payload = None
    for line in out.splitlines():
        if line.startswith("RESULT_JSON "):
            try:
                payload = json.loads(line[len("RESULT_JSON "):])
            except ValueError:
                payload = None
    if payload is None:
        print("[auth] 登录态备份输出看不懂：%s" % out[-400:])
        return None
    if payload.get("ok"):
        print("[auth] 登录态已备份：%s 条记录（%s）"
              % ((payload.get("summary") or {}).get("count"),
                 (payload.get("backup") or "").replace(_HERE + _os.sep, "")))
    else:
        print("[auth] 登录态没备份成功：%s（登录工具说成功，但 profile 里读不到记录）"
              % (payload.get("reason") or "unknown"))
    return payload


def do_auth(reset=False):
    """触发 focusmate_auth，会弹出 Chromium 让你手动登录，5 分钟内完成即可。

    ``reset=True`` 才会清空整个 profile（``clearAuthData``）。默认**保留**
    profile：以前 force=True 就把 IndexedDB / localStorage 全删了，每次重登都
    从空白开始，登录态攒不下来也查不清为什么掉。

    返回 True 表示登录成功，False 表示失败/超时。
    """
    print("[auth] %s请在弹出的窗口里登录 Focusmate（5分钟有效）..."
          % ("先清空 profile 再" if reset else ""))

    # 登录要 headed + **独占**整个 profile：先请共用浏览器服务关掉浏览器并 hold 住。
    # 不 hold 的话，服务会在 auth 期间被别的命令（daemon 下单 / TUI 刷新）唤醒，
    # 把 profile 又抢回去 —— 那正是以前「登录到一半白屏」的来源。
    holder = None
    try:
        holder = fm_service.Service(home=mcp_home())
        holder.hold(600000)
        print("[auth] 已请共用浏览器让位（hold 10 分钟）")
    except Exception as e:  # noqa: BLE001 - 服务没跑就不 hold，直接走独占登录
        print("[auth] 共用浏览器服务没在跑（%s），直接走独占登录" % e)
        holder = None

    try:
        try:
            result = mcp_call("focusmate_auth",
                              {"force": True, "reset": bool(reset)}, timeout=360)
        except fm_lock.ProfileBusy as e:
            print("[auth] 拿不到 profile 锁：%s" % e)
            print("[auth] 说明另一个 Chromium 正在用这个 profile（下单中/桥还开着）。"
                  "等它让位，或先在 warden 里停一轮再重试。")
            return False
        print(json.dumps(result, indent=2, ensure_ascii=False))
        try:
            payload = json.loads(result["content"][0]["text"])
        except Exception:
            payload = {}
        ok = bool(payload.get("success"))
        print("[auth] %s" % ("登录成功" if ok else "登录失败/超时"))
        if ok:
            snapshot_auth_state()
        return ok
    finally:
        if holder is not None:
            try:
                holder.unhold()
                print("[auth] 已解除 hold（共用浏览器恢复可用）")
            except Exception as e:  # noqa: BLE001 - 解除失败不该改写登录结论
                print("[auth] 解除 hold 失败：%s（服务可能已经退出）" % e)


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
    ap.add_argument("--reset", action="store_true",
                    help="配 --auth 用：先清空整个浏览器 profile 再登录（登录态彻底坏了才用）")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.auth:
        do_auth(reset=args.reset)
    else:
        main(args.start, args.time)
