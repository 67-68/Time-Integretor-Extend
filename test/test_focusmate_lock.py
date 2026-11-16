#!/usr/bin/env python3
"""focusmate_lock 回归：拿锁/放锁/死锁抢占/want 让位/孤儿判断。

为什么值得钉住：2026-11-16 实测「登录态天天腐烂」的真因是同一个 Chromium
profile 被两个进程同时打开（daemon 下单 + TUI 的常驻 bridge）。锁若失效，
症状不是报错，而是 dashboard 白屏 + IndexedDB 打坏 —— 非常难查。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import focusmate_lock as fl

ok = 0
fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("ok   %s" % name)
    else:
        fail += 1
        print("FAIL %s\n  got : %r\n  want: %r" % (name, got, want))


def check_true(name, got):
    check(name, bool(got), True)


DEAD_PID = 999999

with tempfile.TemporaryDirectory() as d:
    os.environ.setdefault("FOCUSMATE_PROFILE_DIR", os.path.join(d, "browser-data"))

    # --- 基本：拿锁 → 写文件 → 放锁 ---------------------------------
    info = fl.acquire(d, "mcp:book_session", wait_ms=500)
    check("拿到的锁 role", info["role"], "mcp:book_session")
    check("拿到的锁 pid 是自己", info["pid"], os.getpid())
    on_disk = fl.read_lock(d)
    check("锁文件内容与返回值一致", on_disk["pid"], os.getpid())
    check("锁文件名固定", sorted(os.listdir(d)) and
          fl.LOCK_NAME in os.listdir(d), True)

    # 同一进程再 acquire：不能自己挡自己 → 短等待后 ProfileBusy
    busy = False
    try:
        fl.acquire(d, "second", wait_ms=200)
    except fl.ProfileBusy:
        busy = True
    check("同进程重复 acquire 会等到超时（说明互斥真的生效）", busy, True)
    check("超时后会写 want 标记", fl.read_want(d) is not None, True)
    fl.clear_want(d)

    check("放锁成功", fl.release(d, info), True)
    check("放完锁文件没了", fl.read_lock(d), None)
    check("重复放锁返回 False", fl.release(d, info), False)

    # --- 死锁抢占：持有者进程已死 ------------------------------------
    with open(os.path.join(d, fl.LOCK_NAME), "w", encoding="utf-8") as f:
        json.dump({"pid": DEAD_PID, "role": "ghost", "ts": time.time(),
                   "host": os.uname().nodename}, f)
    check("死进程的锁判为 stale", fl.holder_is_stale(fl.read_lock(d)), True)
    t0 = time.time()
    info2 = fl.acquire(d, "taker", wait_ms=1500)
    check("能抢到死锁", info2["role"], "taker")
    check_true("抢锁没有白等满超时", time.time() - t0 < 1.0)
    fl.release(d, info2)

    # --- 超时抢占：ts 太老（进程还活着也不行） ------------------------
    with open(os.path.join(d, fl.LOCK_NAME), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "role": "old", "ts": time.time() - 3600,
                   "host": os.uname().nodename}, f)
    check("太老的锁判为 stale", fl.holder_is_stale(fl.read_lock(d)), True)
    fl.acquire(d, "taker2", wait_ms=500)
    check("能抢太老的锁", fl.read_lock(d)["role"], "taker2")
    fl.release(d)

    # --- want TTL ---------------------------------------------------
    fl.write_want(d, "waiter")
    check("want 刚写能读到", fl.read_want(d)["role"], "waiter")
    stale_path = os.path.join(d, fl.WANT_NAME)
    with open(stale_path, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "role": "old-want",
                   "ts": time.time() - 3600}, f)
    check("过期的 want 读不到", fl.read_want(d), None)
    fl.clear_want(d)

    # --- 跨进程：别的进程持锁时，等的人写 want、等到超时 --------------
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r); import focusmate_lock as f;"
         "f.acquire(sys.argv[1], 'child-holder', wait_ms=2000);"
         "print('held', flush=True); time.sleep(3)" % ROOT, d],
        stdout=subprocess.PIPE, text=True)
    try:
        line = holder.stdout.readline().strip()
        check("子进程拿到锁", line, "held")
        t0 = time.time()
        raced = False
        try:
            fl.acquire(d, "racer", wait_ms=600)
        except fl.ProfileBusy as e:
            raced = True
            check("ProfileBusy 里带着持有者信息", e.holder.get("role"), "child-holder")
        check("活进程持锁时抢不到", raced, True)
        check_true("等待时写了 want（让持锁方让位）", (time.time() - t0) >= 0.5)
        check("want 里的角色是等待者", fl.read_want(d)["role"], "racer")
        fl.clear_want(d)
    finally:
        holder.kill()
        holder.wait()

    # --- 孤儿浏览器探测：不存在的 profile 目录不该误杀/误报 -------------
    check("假 profile 没浏览器", fl.find_profile_browsers(os.path.join(d, "nope")), [])
    check("清孤儿返回空", fl.kill_orphan_browsers(os.path.join(d, "nope")), [])

    # --- 嵌套 contextmanager：只算最外层 -----------------------------
    with fl.profile_lock(d, "outer", wait_ms=500):
        outer = fl.read_lock(d)
        with fl.profile_lock(d, "inner", wait_ms=500):
            check("嵌套时还是外层的锁", fl.read_lock(d)["role"], "outer")
        check("内层退出后锁还在", fl.read_lock(d) is not None, True)
    check("外层退出后锁没了", fl.read_lock(d), None)
    check("外层锁 pid 是自己", outer["pid"], os.getpid())

    # --- 接线：main.mcp_call 整个调用都在锁里，book_once 认识 PROFILE_BUSY ----
    import main as fm

    home = os.path.join(d, "home")
    os.makedirs(home, exist_ok=True)
    old_env = {k: os.environ.get(k) for k in ("FOCUSMATE_CONFIG_DIR", "FOCUSMATE_PROFILE_DIR")}
    os.environ["FOCUSMATE_CONFIG_DIR"] = home
    os.environ["FOCUSMATE_PROFILE_DIR"] = os.path.join(home, "browser-data")
    real_locked = fm._mcp_call_locked
    seen = {}

    def fake_locked(tool, arguments, timeout=180):
        seen["tool"] = tool
        seen["lock_inside"] = fl.read_lock(home)
        return {"content": [{"type": "text", "text": '{"success": true}'}]}

    fm._mcp_call_locked = fake_locked
    real_waits = dict(fm._LOCK_WAIT_MS)
    fm._LOCK_WAIT_MS = {"book_session": 300}   # 别让用例白等 60s
    try:
        fm.mcp_call("book_session", {"startTime": "x", "duration": "25"}, wait_ms=500)
        check("mcp_call 真的调到了内层实现", seen.get("tool"), "book_session")
        check("内层执行时锁在自己手上", (seen.get("lock_inside") or {}).get("role"),
              "mcp:book_session")
        check("mcp_call 返回后放锁", fl.read_lock(home), None)

        # 锁被别人占着：mcp_call 抛 ProfileBusy，book_once 转成 PROFILE_BUSY
        holder = fl.acquire(home, "other", wait_ms=500)
        raised = False
        try:
            fm.mcp_call("book_session", {"startTime": "x", "duration": "25"}, wait_ms=200)
        except fl.ProfileBusy:
            raised = True
        check("被占着时 mcp_call 抛 ProfileBusy", raised, True)
        fl.release(home, holder)

        fl.acquire(home, "other2", wait_ms=500)
        payload = fm.book_once("2026-11-17T01:00:00.000Z", "25")
        check("book_once 把占锁转成 PROFILE_BUSY", payload.get("errorCode"), "PROFILE_BUSY")
        check("book_once 不把它当 slot 级失败", payload.get("success"), False)
        fl.release(home)
    finally:
        fm._mcp_call_locked = real_locked
        fm._LOCK_WAIT_MS = real_waits
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

print("\n%d passed, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
