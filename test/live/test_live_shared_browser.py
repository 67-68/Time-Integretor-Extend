#!/usr/bin/env python3
"""真机：单一浏览器服务 + CDP 共用（服务握着 profile 时，MCP 还能不能干活）。

为什么必须真机
--------------
离线测试能钉住「socket 单实例 / 多客户端 / hold / 空闲判据」，但验不到
「两只 Playwright 客户端共用同一只 Chromium」。2026-11-23 的黑盒探针证明：
CDP 连上去以后 `page.close()` 会把主人的页面一起关掉、`browser.close()` 直接
把整只浏览器杀掉 —— 所以补丁返回的是**代理 context**。这一段只有真浏览器能验。

验什么（默认只读）
------------------
1. 服务在跑，`ping` 里 `browser: true`、`cdpUrl` 是真的（不是门牌上的死端口）；
2. `<home>/.profile-lock.json` 的 pid 是**服务**的；
3. `main.mcp_call("list_sessions")` 走 CDP 成功，跑完锁**没易主**（它没抢锁）；
4. 并发：服务 `snapshot` 和 MCP `list_sessions` 同时跑，两边都成功；
5. `--book` 时额外真订一场「十天后 15:30 的 25 分钟」→ 官方 API 复核 → 取消 →
   再复核消失（默认不做任何写操作）。

它**不动 profile 拷贝**：直接验正在跑的那个服务（常态化环境才是 daemon 下单的
真实路径）。所以要求服务已经在跑；没跑会替你拉起来（和 warden 一样，幂等）。

跑法
----
    python3 -B test/live/test_live_shared_browser.py                # 只读（~15s）
    python3 -B test/live/test_live_shared_browser.py --book         # 外加真订一场再取消
    python3 -B test/live/test_live_shared_browser.py --dry-run      # 只打印计划
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
REAL_HOME = os.path.join(ROOT, "temp", "focusmate-home")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails = []
ok_count = 0


def check(name, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % name)
    else:
        fails.append(name)
        print("FAIL %s\n  got : %r\n  want: %r" % (name, got, want))


def check_true(name, got):
    check(name, bool(got), True)


def iso_z(dt):
    """zod 的 z.string().datetime() 只认 UTC Zulu：`+08:00` 会报 Invalid datetime。"""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", action="store_true",
                    help="额外真订一场「十天后 15:30 的 25 分钟」再取消（默认只读）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = ap.parse_args(argv)

    print("目标 home：%s" % REAL_HOME)
    print("计划：验服务/浏览器/CDP → MCP 经共用浏览器读 → 并发读%s"
          % ("→ 真订十天后 15:30（25 分钟）再取消" if args.book else "（不做任何写操作）"))
    if args.dry_run:
        print("（只打印计划）")
        return 0

    os.environ["FOCUSMATE_CONFIG_DIR"] = REAL_HOME
    import focusmate_service as fsvc
    import main as m

    svc = fsvc.Service(home=REAL_HOME)
    state = fsvc.read_state(REAL_HOME) or {}
    if not fsvc.is_running(REAL_HOME):
        print("服务没在跑，先拉起来（和 warden 同一条路径）")
        state = svc.start()
    pid = state.get("pid")
    check_true("服务在跑（pid=%s）" % pid, pid)

    ping = svc.ensure_browser(timeout=180)
    check("浏览器开着", ping.get("browser"), True)
    cdp = ping.get("cdpUrl") or ""
    check_true("cdpUrl 可用（%s）" % cdp, cdp.startswith("http://127.0.0.1:"))

    lock_path = os.path.join(REAL_HOME, ".profile-lock.json")
    lock = json.load(open(lock_path, encoding="utf-8")) if os.path.exists(lock_path) else {}
    check("profile 锁在服务手上", lock.get("pid"), pid)

    now = datetime.datetime.now()
    start_iso = iso_z(now.replace(hour=0, minute=0, second=0, microsecond=0))
    end_iso = iso_z(now + datetime.timedelta(days=2))

    t0 = time.time()
    res = m.mcp_call("list_sessions", {"startDate": start_iso, "endDate": end_iso}, timeout=240)
    elapsed = time.time() - t0
    text = res["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"_raw": text[:300]}
    check_true("MCP 经共用浏览器读到 session（%.1fs）" % elapsed,
               isinstance(payload.get("sessions"), list))
    lock2 = json.load(open(lock_path, encoding="utf-8")) if os.path.exists(lock_path) else {}
    check("MCP 跑完锁没易主（它没抢锁）", lock2.get("pid"), pid)

    snap_res = {}

    def _snap():
        try:
            snap_res["r"] = svc.call("snapshot", timeout=180)
        except Exception as e:  # noqa: BLE001
            snap_res["e"] = e

    th = threading.Thread(target=_snap)
    th.start()
    res2 = m.mcp_call("list_sessions", {"startDate": start_iso, "endDate": end_iso}, timeout=240)
    th.join(180)
    check_true("并发时服务 snapshot 成功", "r" in snap_res)
    check_true("并发时 MCP 也成功", "content" in res2)

    if args.book:
        target = (now + datetime.timedelta(days=10)).replace(
            hour=15, minute=30, second=0, microsecond=0)
        start_ms, day = int(target.timestamp() * 1000), target.date().isoformat()
        before = [s for s in (svc.call("snapshot", date=day, timeout=180).get("sessions") or [])
                  if s.get("startMs") == start_ms]
        if before:
            print("跳过订场：%s 15:30 已经有一场了" % day)
        else:
            t1 = time.time()
            res_book = m.mcp_call("book_session",
                                  {"startTime": iso_z(target), "duration": "25"}, timeout=300)
            try:
                bp = json.loads(res_book["content"][0]["text"])
            except (ValueError, KeyError, IndexError):
                bp = {}
            check_true("book_session 报 success（%.1fs）" % (time.time() - t1),
                       bool(bp.get("success")))
            hit = [s for s in (svc.call("snapshot", date=day, timeout=180).get("sessions") or [])
                   if s.get("startMs") == start_ms]
            check_true("官方 API 复核：这场真的在（%s 15:30）" % day, hit)
            check("时长复核 25 分钟", (hit[0].get("durationMin") if hit else None), 25)
            lock3 = json.load(open(lock_path, encoding="utf-8")) if os.path.exists(lock_path) else {}
            check("订场全程锁也在服务手上（没抢锁 → 没等待）", lock3.get("pid"), pid)
            if hit:
                c = svc.call("cancel", startMs=start_ms,
                             meetingId=hit[0].get("id"), timeout=240)
                check("取消 verified", bool(c.get("verified")), True)
                after = [s for s in (svc.call("snapshot", date=day, timeout=180).get("sessions") or [])
                         if s.get("startMs") == start_ms]
                check("取消后复核：这场不在了", after, [])

    print("\n%d ok, %d fail" % (ok_count, len(fails)))
    for name in fails:
        print("  -", name)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
