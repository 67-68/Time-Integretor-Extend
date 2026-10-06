#!/usr/bin/env python3
"""ti_extend 依赖安装器（幂等）：focusmate-mcp 装到 ti_extend/temp，装过就跳过。

设计：
- 安装位置：ti_extend/temp/focusmate-mcp（工作区内，可用 FOCUSMATE_MCP_HOME 覆盖）。
  不再用 /tmp（重启会丢）和 ~/.local/share（散落在外）。
- 状态文件：ti_extend/temp/.install-state.json，记录 commit + package-lock 哈希，
  下次运行自动对比，命中则跳过 npm ci / build。
- Chromium：按 browsers.json 要的 revision 精确检测；下载走 curl + 系统 unzip
  手动装（`npx playwright install` 自带解压器在这台机器上会卡死，只当回退）。
  尊重 PLAYWRIGHT_DOWNLOAD_HOST 镜像环境变量。
- 纯 stdlib，Python 3.9 可跑，无需 pip 装任何东西。

用法：
    python3 install.py                # 缺啥装啥
    python3 install.py --check        # 只检测不安装（exit 0=齐了，1=缺东西）
    python3 install.py --force        # 全量重装（删 temp 重来）
    python3 install.py --skip-browser # 跳过 Chromium 下载
    python3 install.py --force-browser# 重下 Chromium
    ./install.sh ...                  # 薄封装，转调本脚本，参数透传
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
DEFAULT_DEST = os.path.join(TEMP, "focusmate-mcp")
STATE_FILE = os.path.join(TEMP, ".install-state.json")
REPO = "https://github.com/67-68/focusmate-mcp.git"
BRANCH = "main"


def dest_dir():
    return os.environ.get("FOCUSMATE_MCP_HOME", DEFAULT_DEST)


def sh(cmd, cwd=None):
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError("命令失败(%d)：%s" % (r.returncode, " ".join(cmd)))


def out(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(TEMP, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def git_head(dest):
    code, s = out(["git", "-C", dest, "rev-parse", "HEAD"])
    return s if code == 0 else None


def browsers_base():
    """playwright 浏览器缓存根目录。"""
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")


def expected_revisions(dest):
    """vendored playwright 要求的浏览器 revision，如 {'chromium': '1208', ...}。

    读 dest/node_modules/playwright-core/browsers.json，只取 MCP 会用到的
    chromium（headful 登录）+ chromium-headless-shell（headless 下单）。
    node_modules 还没装时返回 {}。
    """
    wanted = ("chromium", "chromium-headless-shell")
    meta = os.path.join(dest, "node_modules", "playwright-core", "browsers.json")
    try:
        with open(meta, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    browsers = data.get("browsers", [])
    if isinstance(browsers, dict):
        browsers = browsers.values()
    out = {}
    for b in browsers:
        if isinstance(b, dict) and b.get("name") in wanted and b.get("revision"):
            out[b["name"]] = str(b["revision"])
    return out


def expected_chromium_revision(dest):
    return expected_revisions(dest).get("chromium")


def chromium_ok(dest):
    """需要的那个 revision 是否装好（只认版本号，不认“随便有个 chromium 目录”）。

    返回 (ok, expected_revision, actual_path_or_None)。
    """
    revs = expected_revisions(dest)
    rev = revs.get("chromium")
    base = browsers_base()
    if not rev or not base or not os.path.isdir(base):
        return False, rev, None
    path = os.path.join(base, "chromium-%s" % rev)
    if not (os.path.isdir(path) and os.listdir(path)):
        return False, rev, None
    # 目录在还不够（解压中断/层级错位会留下残目录），必须按 CFT_LAYOUT 的
    # 精确路径验到可执行文件；headless 下单还要 headless-shell，一起查
    plat = cft_platform()
    full = CFT_LAYOUT.get(("chrome", plat))
    shell = CFT_LAYOUT.get(("shell", plat))
    if not full or not shell:
        return False, rev, None
    if sys.platform == "darwin":
        app = os.path.join(path, full[1], "Google Chrome for Testing.app", "Contents")
        exe = os.path.join(app, "MacOS", "Google Chrome for Testing")
        fw = os.path.join(app, "Frameworks")
        if not (os.path.isfile(exe) and os.path.isdir(fw) and os.listdir(fw)):
            return False, rev, None
    elif sys.platform == "win32":
        if not os.path.isfile(os.path.join(path, full[1], "chrome.exe")):
            return False, rev, None
    else:
        if not os.path.isfile(os.path.join(path, full[1], "chrome")):
            return False, rev, None
    shell_rev = revs.get("chromium-headless-shell")
    if not shell_rev:
        return True, rev, path
    shell_exe = ("chrome-headless-shell.exe" if sys.platform == "win32"
                 else "chrome-headless-shell")
    if os.path.isfile(os.path.join(
            base, "chromium_headless_shell-%s" % shell_rev, shell[1], shell_exe)):
        return True, rev, path
    return False, rev, None


def chromium_dirs():
    """缓存里现有的 chromium-* 目录（仅用于展示/诊断）。"""
    base = browsers_base()
    found = []
    if not base or not os.path.isdir(base):
        return found
    for name in os.listdir(base):
        if name.startswith("chromium-") and os.path.isdir(os.path.join(base, name)):
            found.append(os.path.join(base, name))
    return found


def src_newer_than_build(dest):
    """src 下是否有文件比 build/index.js 新（没有 build 视为需要构建）。"""
    entry = os.path.join(dest, "build", "index.js")
    if not os.path.isfile(entry):
        return True
    try:
        build_mtime = os.path.getmtime(entry)
    except OSError:
        return True
    src = os.path.join(dest, "src")
    if not os.path.isdir(src):
        return True
    for root, _, files in os.walk(src):
        for name in files:
            if not name.endswith((".ts", ".js", ".json")):
                continue
            try:
                if os.path.getmtime(os.path.join(root, name)) > build_mtime:
                    return True
            except OSError:
                return True
    # package.json 变化也视为需要重新 build
    try:
        if os.path.getmtime(os.path.join(dest, "package.json")) > build_mtime:
            return True
    except OSError:
        return True
    return False


def status(dest):
    """返回各项检测结果字典，不做任何写操作。"""
    lock = os.path.join(dest, "package-lock.json")
    st = load_state()
    cloned = os.path.isdir(os.path.join(dest, ".git"))
    entry_ok = os.path.isfile(os.path.join(dest, "build", "index.js"))
    nm_ok = os.path.isdir(os.path.join(dest, "node_modules"))
    lock_hash = sha256_file(lock) if os.path.isfile(lock) else None
    lock_match = bool(lock_hash and st.get("package_lock_sha256") == lock_hash)
    head = git_head(dest) if cloned else None
    head_match = bool(head and st.get("commit") == head)
    need_build = (not entry_ok) or src_newer_than_build(dest)
    browser_ok, expected_rev, browser_path = chromium_ok(dest)
    chromiums = chromium_dirs()
    installed = cloned and entry_ok and nm_ok and lock_match and not need_build and browser_ok
    return {
        "dest": dest,
        "cloned": cloned,
        "head": head,
        "head_recorded": st.get("commit"),
        "entry_ok": entry_ok,
        "node_modules_ok": nm_ok,
        "lock_match": lock_match,
        "need_build": need_build,
        "browser_ok": browser_ok,
        "expected_rev": expected_rev,
        "browser_path": browser_path,
        "chromiums": chromiums,
        "installed": installed,
    }


def print_status(s):
    print("检测 %s" % s["dest"])
    print("  源码 clone   : %s" % ("OK" if s["cloned"] else "缺失"))
    print("  build/index.js: %s" % ("OK" if s["entry_ok"] else "缺失"))
    print("  node_modules : %s" % ("OK" if s["node_modules_ok"] else "缺失"))
    print("  lock 一致     : %s" % ("OK（跳过 npm ci）" if s["lock_match"] else "不一致/无记录"))
    print("  需要 build    : %s" % ("是" if s["need_build"] else "否（跳过 build）"))
    if s["browser_ok"]:
        print("  chromium-%s : OK（%s，跳过下载）" % (s["expected_rev"], s["browser_path"]))
    elif s["expected_rev"]:
        print("  chromium-%s : 缺失（缓存里有 %s，但版本不对，需下载）"
              % (s["expected_rev"],
                 ", ".join(os.path.basename(c) for c in s["chromiums"]) or "空"))
    else:
        print("  chromium     : 未知（node_modules 还没装，先装依赖再看）")
    print("  总体          : %s" % ("已安装齐" if s["installed"] else "缺东西，需安装"))


def cmd_check(args):
    s = status(dest_dir())
    print_status(s)
    if args.verbose_chromium is False:
        pass
    return 0 if s["installed"] else 1


def ensure_source(dest, force):
    if force and os.path.isdir(dest):
        print("==> --force：清空 %s" % dest)
        shutil.rmtree(dest)
    if os.path.isdir(os.path.join(dest, ".git")):
        print("==> 源码已存在，检查更新")
        code, _ = out(["git", "-C", dest, "fetch", "origin", BRANCH])
        if code != 0:
            print("  离线或 fetch 失败，沿用本地源码（跳过 pull）")
            return
        _, local = out(["git", "-C", dest, "rev-parse", "HEAD"])
        _, remote = out(["git", "-C", dest, "rev-parse", "origin/%s" % BRANCH])
        if local and remote and local == remote:
            print("  已是最新（%s），跳过 pull" % local[:12])
        elif local and remote:
            # 本地有自己 commit（领先 upstream）时，pull --ff-only 会失败，
            # 这里保持本地 commit 不动，只提示。
            code, _ = out(["git", "-C", dest, "merge-base", "--is-ancestor",
                           "origin/%s" % BRANCH, "HEAD"])
            if code == 0:
                print("  本地领先 upstream（有本地 commit），跳过 pull")
            else:
                print("  有更新，pull --ff-only")
                sh(["git", "-C", dest, "pull", "--ff-only"])
    else:
        print("==> 克隆源码")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        sh(["git", "clone", "--branch", BRANCH, REPO, dest])


def ensure_patches(dest):
    """把 patches/*.patch 应用到源码（幂等）。--force 重装后也会自动补上。"""
    pdir = os.path.join(HERE, "patches")
    if not os.path.isdir(pdir):
        return
    patches = sorted(p for p in os.listdir(pdir) if p.endswith(".patch"))
    for name in patches:
        pfile = os.path.join(pdir, name)
        # 已经打过：反向检查通过说明补丁已存在
        code_rev, _ = out(["git", "-C", dest, "apply", "--check", "--reverse", pfile])
        if code_rev == 0:
            print("==> patch 已应用，跳过：%s" % name)
            continue
        code_fwd, _ = out(["git", "-C", dest, "apply", "--check", pfile])
        if code_fwd != 0:
            print(">> patch 无法应用（可能与上游冲突）：%s，跳过" % name)
            continue
        sh(["git", "-C", dest, "apply", pfile])
        print("==> 已应用 patch：%s" % name)


def ensure_npm(dest, force, state):
    lock = os.path.join(dest, "package-lock.json")
    nm = os.path.join(dest, "node_modules")
    lock_hash = sha256_file(lock) if os.path.isfile(lock) else None
    if (
        not force
        and os.path.isdir(nm)
        and lock_hash
        and state.get("package_lock_sha256") == lock_hash
        and state.get("npm_ci_ok")
    ):
        print("==> node_modules 已就绪且 lock 未变，跳过 npm ci")
        return lock_hash
    print("==> npm ci")
    sh(["npm", "ci"], cwd=dest)
    state["npm_ci_ok"] = True
    return lock_hash


def ensure_build(dest, force):
    if not force and not src_newer_than_build(dest):
        print("==> build/index.js 已是最新，跳过 npm run build")
        return
    print("==> npm run build")
    sh(["npm", "run", "build"], cwd=dest)


# Chrome-for-Testing 归档名 + 包内顶层目录名：(chrome|shell, 平台) -> (zip, wrapper)。
# 平台命名跟 CfT 官网一致；wrapper 是 playwright 缓存布局要求的顶层目录。
CFT_LAYOUT = {
    ("chrome", "mac-arm64"): ("chrome-mac-arm64.zip", "chrome-mac-arm64"),
    ("chrome", "mac-x64"): ("chrome-mac-x64.zip", "chrome-mac-x64"),
    ("chrome", "linux64"): ("chrome-linux64.zip", "chrome-linux"),
    ("chrome", "win64"): ("chrome-win64.zip", "chrome-win"),
    ("shell", "mac-arm64"): ("chrome-headless-shell-mac-arm64.zip", "chrome-headless-shell-mac-arm64"),
    ("shell", "mac-x64"): ("chrome-headless-shell-mac-x64.zip", "chrome-headless-shell-mac-x64"),
    ("shell", "linux64"): ("chrome-headless-shell-linux64.zip", "chrome-headless-shell-linux64"),
    ("shell", "win64"): ("chrome-headless-shell-win64.zip", "chrome-headless-shell-win64"),
}

# browsers.json 包名 -> 缓存目录名前缀（跟 playwright 的缓存布局一致）
CACHE_PREFIX = {
    "chromium": "chromium",
    "chromium-headless-shell": "chromium_headless_shell",
}


def cft_platform():
    m = sys.platform
    arch = platform.machine().lower()
    if m == "darwin":
        return "mac-arm64" if arch in ("arm64", "aarch64") else "mac-x64"
    if m == "win32":
        return "win64"
    return "linux64"


def expected_browsers(dest):
    """{包名: {'revision': .., 'version': ..}}，只要 MCP 用到的两个包。"""
    wanted = ("chromium", "chromium-headless-shell")
    meta = os.path.join(dest, "node_modules", "playwright-core", "browsers.json")
    try:
        with open(meta, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    browsers = data.get("browsers", [])
    if isinstance(browsers, dict):
        browsers = browsers.values()
    out = {}
    for b in browsers:
        if (isinstance(b, dict) and b.get("name") in wanted
                and b.get("revision") and b.get("browserVersion")):
            out[b["name"]] = {"revision": str(b["revision"]),
                              "version": str(b["browserVersion"])}
    return out


def download_file(url, dst):
    """curl 优先（快、有进度），没有 curl 就用 stdlib urllib。"""
    if shutil.which("curl"):
        sh(["curl", "-fL", "--retry", "3", "--connect-timeout", "20",
            "-o", dst, url])
        return
    print("  (无 curl，用 urllib 下载，无进度条)")
    try:
        urllib.request.urlretrieve(url, dst)
    except Exception as e:
        raise RuntimeError("下载失败 %s：%s" % (url, e))


def manual_install_one(name, info, plat):
    """手动装一个浏览器包：curl 下载 -> 系统 unzip 解压 -> xattr 清隔离属性。

    不经过 `npx playwright install`（它自带的 JS 解压器在这台机器上会卡死）。
    成功返回缓存目录路径。
    """
    kind = "shell" if "headless" in name else "chrome"
    key = (kind, plat)
    if key not in CFT_LAYOUT:
        raise RuntimeError("不支持的平台组合：%s，请改用 npx playwright install" % (key,))
    zipname, wrapper = CFT_LAYOUT[key]
    host = os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST", "https://cdn.playwright.dev")
    url = "%s/builds/cft/%s/%s/%s" % (host.rstrip("/"), info["version"], plat, zipname)
    target = os.path.join(browsers_base(), "%s-%s" % (CACHE_PREFIX[name], info["revision"]))
    dl_dir = os.path.join(TEMP, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    zippath = os.path.join(dl_dir, "%s-%s-%s.zip" % (name, info["revision"], plat))
    stage = os.path.join(dl_dir, "stage-%s" % name)
    print("==> 下载 %s %s (%s)" % (name, info["version"], url))
    download_file(url, zippath)
    print("==> 解压到 %s" % target)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)
    sh(["unzip", "-q", zippath, "-d", stage])
    # 归一化布局：目标必须是 target/<wrapper>/...，不管包里是单顶层目录还是平铺
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    entries = [e for e in os.listdir(stage) if e != "__MACOSX"]
    if len(entries) == 1 and os.path.isdir(os.path.join(stage, entries[0])):
        inner = os.path.join(stage, entries[0])
        if entries[0] == wrapper:
            shutil.move(inner, os.path.join(target, wrapper))
        else:
            # 顶层名不对（如平铺的残包），统一收进 wrapper
            dest_inner = os.path.join(target, wrapper)
            os.makedirs(dest_inner)
            for e in os.listdir(inner):
                shutil.move(os.path.join(inner, e), dest_inner)
    else:
        dest_inner = os.path.join(target, wrapper)
        os.makedirs(dest_inner)
        for e in entries:
            shutil.move(os.path.join(stage, e), dest_inner)
    shutil.rmtree(stage, ignore_errors=True)
    try:
        os.remove(zippath)
    except OSError:
        pass
    if sys.platform == "darwin" and shutil.which("xattr"):
        subprocess.run(["xattr", "-cr", target])
    return target


def ensure_browser(dest, skip, force_browser):
    if skip:
        print("==> 跳过 Chromium（--skip-browser）")
        return
    ok, rev, path = chromium_ok(dest)
    if not force_browser and ok:
        print("==> Chromium 已安装（chromium-%s + headless-shell），跳过下载" % rev)
        return
    plat = cft_platform()
    infos = expected_browsers(dest)
    if not infos:
        raise RuntimeError("读不到 browsers.json（先确认 npm ci 成功），装不了浏览器")
    err = None
    if shutil.which("unzip"):
        try:
            for name in ("chromium", "chromium-headless-shell"):
                if name in infos:
                    manual_install_one(name, infos[name], plat)
            ok2, _, _ = chromium_ok(dest)
            if ok2:
                print("==> 浏览器手动安装完成并校验通过")
                return
            err = "手动解压后校验没过"
        except (RuntimeError, OSError) as e:
            err = str(e)
        print(">> 手动安装失败（%s），回退到 npx playwright install" % err)
    else:
        print(">> 无系统 unzip，直接用 npx playwright install")
    sh(["npx", "playwright", "install", "chromium"], cwd=dest)
    ok3, rev3, _ = chromium_ok(dest)
    if not ok3:
        raise RuntimeError("Chromium 下载后仍找不到 chromium-%s，请检查磁盘/网络后重试" % (rev3 or "?"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="ti_extend 依赖安装器（幂等，装到 temp/）")
    ap.add_argument("--check", action="store_true", help="只检测不安装")
    ap.add_argument("--force", action="store_true", help="全量重装（清空 temp 重来）")
    ap.add_argument("--skip-browser", action="store_true", help="跳过 Chromium 下载")
    ap.add_argument("--force-browser", action="store_true", help="重下 Chromium")
    ap.add_argument("--verbose-chromium", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if shutil.which("node") is None:
        print("缺少 node，请先装 Node.js >= 20", file=sys.stderr)
        return 2
    if shutil.which("npm") is None:
        print("缺少 npm", file=sys.stderr)
        return 2
    if shutil.which("git") is None:
        print("缺少 git", file=sys.stderr)
        return 2

    dest = dest_dir()
    print("ti_extend install")
    print("  temp     %s" % TEMP)
    print("  dest     %s" % dest)

    if args.check:
        return cmd_check(args)

    state = load_state()
    ensure_source(dest, args.force)
    ensure_patches(dest)
    lock_hash = ensure_npm(dest, args.force, state)
    ensure_build(dest, args.force)
    ensure_browser(dest, args.skip_browser, args.force_browser)

    state["commit"] = git_head(dest)
    if lock_hash:
        state["package_lock_sha256"] = lock_hash
    save_state(state)

    print("")
    print("完成。server 入口：%s" % os.path.join(dest, "build", "index.js"))
    print("下一步：")
    print("  python3 \"%s\" --auth    # 首次登录（会弹浏览器）" % os.path.join(HERE, "main.py"))
    print("  python3 \"%s\"           # 订 16:45 / 50min 测试" % os.path.join(HERE, "main.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
