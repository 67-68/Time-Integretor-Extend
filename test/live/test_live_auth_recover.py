#!/usr/bin/env python3
"""真机测试：Focusmate 登录态（IndexedDB 里的 firebase:authUser）丢了，能不能自愈。

它验的是单元测试永远验不到的那一段（2026-11-22/23 真机事故）：

    app 自己把 /dashboard 踢回 /login（cookie 还在、firebase:authUser 被删）
    → openSession 发现登录页 → 从 auth-backup.json 写回 IndexedDB
    → reload → Firebase 用 refresh token 续期 → 回到 /dashboard

**只在 profile 的拷贝上做破坏性动作**（wipe、删备份），真 profile 只读一次；
跑完把拷贝删掉。所以这个测试可以反复跑，不会把真实登录态弄坏。

跑法
----
    python3 -B test/live/test_live_auth_recover.py          # 只打印计划
    python3 -B test/live/test_live_auth_recover.py --yes    # 真跑（~1-2 分钟）

前置：真实 profile 里现在得是登录状态，并且 `auth-backup.json` 已经在
（`python3 main.py --auth` 成功后会自动生成；也可以手动
`node scripts/auth_store.mjs --snapshot`）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
REAL_HOME = os.path.join(ROOT, "temp", "focusmate-home")
AUTH_STORE = os.path.join(ROOT, "scripts", "auth_store.mjs")
BACKUP_NAME = "auth-backup.json"

fails = []
ok_count = 0


def check(label, got, want):
    global ok_count
    if got == want:
        ok_count += 1
        print("ok   %s" % label)
    else:
        fails.append("%s\n      got  = %r\n      want = %r" % (label, got, want))


def run_auth(mode, home, timeout=240):
    """跑 scripts/auth_store.mjs，返回 (returncode, payload, 完整输出)。"""
    env = dict(os.environ)
    env["FOCUSMATE_CONFIG_DIR"] = home
    proc = subprocess.run(
        ["node", AUTH_STORE, mode],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    payload = None
    for line in out.splitlines():
        if line.startswith("RESULT_JSON "):
            try:
                payload = json.loads(line[len("RESULT_JSON "):])
            except ValueError:
                payload = None
    return proc.returncode, payload, out


def copy_home(dst):
    """拷一份 profile；删掉锁文件和 Chromium 的 Singleton*，否则拷出来的浏览器起不来。"""
    shutil.copytree(REAL_HOME, dst, symlinks=True, dirs_exist_ok=True)
    for name in (".profile-lock.json", ".profile-want.json"):
        try:
            os.remove(os.path.join(dst, name))
        except FileNotFoundError:
            pass
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(dst, "browser-data", name)
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真跑（默认只打印计划）")
    args = ap.parse_args()

    if not os.path.isdir(os.path.join(REAL_HOME, "browser-data", "Default")):
        print("!! 找不到真实 profile：%s" % REAL_HOME)
        return 2
    backup = os.path.join(REAL_HOME, BACKUP_NAME)
    if not os.path.exists(backup):
        print("!! 真实 profile 还没有 %s；先跑一次登录：python3 main.py --auth" % BACKUP_NAME)
        return 2

    plan = [
        "1) 拷 profile 到临时目录（不动真 profile）",
        "2) 拷贝上 --check：应能从备份/登录态读到 firebase:authUser",
        "3) 拷贝上 --wipe：删掉 IndexedDB 里的登录记录（模拟事故现场）",
        "4) 拷贝上 --check：应自动用 auth-backup.json 写回并回到 /dashboard",
        "5) --refresh：用 refresh token 主动续期，过期时间应变晚",
        "6) 再删备份 + --wipe + --check：应快速失败并提示「先 --auth」",
    ]
    print("计划：")
    for line in plan:
        print("  " + line)
    if not args.yes:
        print("\n（没带 --yes，只打印计划。真跑大约 1-2 分钟。）")
        return 0

    tmp = tempfile.mkdtemp(prefix="fmauth-live-")
    try:
        home = tmp
        print("\n[1] 拷 profile → %s" % home)
        copy_home(home)

        print("\n[2] --check（拷贝）")
        rc, p, _ = run_auth("--check", home)
        check("首次 check 成功", rc, 0)
        check("读得到 1 条登录记录", (p or {}).get("summary", {}).get("count"), 1)
        check("带 refresh token", (p or {}).get("summary", {}).get("hasRefresh"), True)

        print("\n[3] --wipe（模拟 app 把 firebase:authUser 删掉）")
        rc, p, _ = run_auth("--wipe", home)
        check("wipe 成功", rc, 0)
        check("wipe 掉 1 条", (p or {}).get("wiped"), 1)

        print("\n[4] --check（应自动自愈）")
        rc, p, out = run_auth("--check", home)
        check("自愈后 check 成功", rc, 0)
        check("自愈后记录回来了", (p or {}).get("summary", {}).get("count"), 1)
        check("输出里说明用了备份", "备份写回" in out, True)
        check("备份仍在", os.path.exists(os.path.join(home, BACKUP_NAME)), True)

        print("\n[5] --refresh：用 refresh token 主动续期")
        before_exp = ((p or {}).get("summary") or {}).get("expMs")
        rc, p2, out2 = run_auth("--refresh", home)
        check("refresh 成功", rc, 0)
        after_exp = ((p2 or {}).get("summary") or {}).get("expMs")
        check("续期后过期时间变晚", bool(after_exp and before_exp and after_exp > before_exp), True)

        print("\n[6] 备份也删掉 + --wipe → 应快速失败并让人去 --auth")
        os.remove(os.path.join(home, BACKUP_NAME))
        run_auth("--wipe", home)
        rc, p, out = run_auth("--check", home)
        check("无备份时退出码 2", rc, 2)
        check("原因是 logged-out", (p or {}).get("reason"), "logged-out")
        check("提示里点名没有备份", "没有登录态备份" in out, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if fails:
        print("FAIL (%d ok, %d failed)" % (ok_count, len(fails)))
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("PASS (%d checks)" % ok_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
