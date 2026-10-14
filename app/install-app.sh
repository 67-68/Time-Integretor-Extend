#!/bin/bash
# 把 Warden.app 构建好、装到应用程序目录、唤起一次。
#
#   ./app/install-app.sh                  # 按新鲜度构建 + 装到 /Applications + 唤起
#   ./app/install-app.sh --check          # 只报告状态，不写任何东西
#   ./app/install-app.sh --force          # 强制重建（默认源文件不新就跳过）
#   ./app/install-app.sh --no-launch      # 装完不自动唤起
#   ./app/install-app.sh --app-dest DIR   # 换安装位置（默认 /Applications，不可写则 ~/Applications）
#   ./app/install-app.sh --quiet          # 只说结论
#
# 这一层只管「装到哪、怎么替换、要不要唤起」；「怎么构建」是 app/build-app.sh 的事，
# 「先装哪些依赖」是 install.py 的事。通常不用直接跑它 —— 跑 install.py 或 install.sh 就行。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$HERE/Warden.app"
APP_NAME="Warden.app"
BUNDLE_ID="com.ti-extend.warden"
SUMMON="$HERE/warden-summon.applescript"

CHECK=0
FORCE=0
LAUNCH=1
QUIET=0
APP_DEST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check)      CHECK=1; shift ;;
        --force)      FORCE=1; shift ;;
        --no-launch)  LAUNCH=0; shift ;;
        --quiet)      QUIET=1; shift ;;
        --app-dest)   APP_DEST="${2:?--app-dest 后面要给目录}"; shift 2 ;;
        --app-dest=*) APP_DEST="${1#*=}"; shift ;;
        -h|--help)    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

say() { [ "$QUIET" = 1 ] || echo "$@"; }

# ---------------------------------------------------------------- 目标位置

# 真去写一个文件探一下。不能只看 [ -w ]：那只读权限位，遇到沙箱 / ACL /
# 只读挂载会误判成「可写」，然后装到一半才炸。
probe_writable() {
    local d="$1" p
    [ -d "$d" ] || return 1
    p="$d/.warden-write-probe.$$"
    if ( : > "$p" ) 2>/dev/null; then
        rm -f "$p" 2>/dev/null || true
        return 0
    fi
    return 1
}

pick_app_dir() {
    if [ -n "$APP_DEST" ]; then
        printf '%s\n' "${APP_DEST%/}"
        return
    fi
    if probe_writable /Applications; then
        printf '/Applications\n'
    else
        mkdir -p "$HOME/Applications" 2>/dev/null || true
        printf '%s\n' "$HOME/Applications"
    fi
}

# ---------------------------------------------------------------- 状态判断

# 源文件里比已装二进制新的（空 = 不需要重建）。Warden.icns 是生成物，可能还不存在。
sources_newer_than() {
    local target="$1" f
    local srcs=("$HERE/WardenMain.swift" "$SUMMON" "$HERE/build-app.sh" "$HERE/Warden.icns")
    for f in "${srcs[@]}"; do
        [ -f "$f" ] || continue
        [ "$f" -nt "$target" ] && printf '%s\n' "$f" && return 0
    done
    return 0
}

# 0 = 已装且最新，1 = 需要装/重建，2 = 环境缺东西
app_state() {
    command -v swiftc >/dev/null 2>&1 || return 2
    local bin="$APP_DEST_PATH/Contents/MacOS/Warden" repo
    [ -x "$bin" ] || return 1
    # 仓库搬过没有：Swift 里烤的是「仓库根」这一个字符串（运行时才拼上
    # app/warden-summon.applescript），所以要拿仓库根去比，不是完整路径。
    repo="$(cd "$HERE/.." && pwd)"
    grep -aqF "$repo" "$bin" 2>/dev/null || return 1
    [ -z "$(sources_newer_than "$bin")" ] || return 1
    return 0
}

status_text() {
    case "$1" in
        0) echo "OK（已装且是最新）" ;;
        1) if [ -x "$APP_DEST_PATH/Contents/MacOS/Warden" ]; then
               echo "已装但过期（源文件更新过，或仓库搬过），需重装"
           else
               echo "未安装"
           fi ;;
        2) echo "装不了 —— 缺少 swiftc（xcode-select --install）" ;;
    esac
}

APP_DIR="$(pick_app_dir)"
APP_DEST_PATH="$APP_DIR/$APP_NAME"

if [ "$CHECK" = 1 ]; then
    st=0; app_state || st=$?
    echo "检测 Warden.app"
    echo "  安装位置 : $APP_DEST_PATH"
    echo "  状态     : $(status_text "$st")"
    exit "$st"
fi

# ---------------------------------------------------------------- 构建

if ! command -v swiftc >/dev/null 2>&1; then
    echo "✗ 没有 swiftc，装不了 app。先跑：xcode-select --install" >&2
    exit 2
fi

need_build=1
if [ "$FORCE" != 1 ] && app_state; then
    need_build=0
fi

if [ "$need_build" != 1 ]; then
    say "→ Warden.app 已是最新，跳过构建（--force 可强制重建）"
elif [ "$QUIET" = 1 ]; then
    "$HERE/build-app.sh" >/dev/null
else
    "$HERE/build-app.sh"
fi

# ---------------------------------------------------------------- 装

# 正在跑的实例要先退掉：往一个正在被使用的 bundle 上 ditto 会留半个 app。
# 用通配路径匹配，所以 app/ 里那份、和 /Applications 那份都会被退掉 ——
# 免得两个实例同时抢着唤起。
if pkill -f "/${APP_NAME}/Contents/MacOS/Warden" 2>/dev/null; then
    say "→ 已退出正在运行的 Warden"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f "/${APP_NAME}/Contents/MacOS/Warden" >/dev/null 2>&1 || break
        sleep 0.3
    done
fi

# 先 ditto 到同级临时目录，再原子替换。用 ditto 而不是 cp -R：它能正确保住
# bundle 的签名和扩展属性。
install_app_to() {
    local dir="$1" stage
    mkdir -p "$dir" 2>/dev/null || return 1
    stage="$dir/.${APP_NAME}.stage.$$"
    rm -rf "$stage" 2>/dev/null || true
    ditto "$APP_SRC" "$stage" || return 1
    if [ -e "$dir/$APP_NAME" ]; then
        rm -rf "$dir/$APP_NAME.old" 2>/dev/null || true
        mv "$dir/$APP_NAME" "$dir/$APP_NAME.old" || return 1
    fi
    mv "$stage" "$dir/$APP_NAME" || return 1
    rm -rf "$dir/$APP_NAME.old" 2>/dev/null || true
    return 0
}

if ! install_app_to "$APP_DIR"; then
    if [ -n "$APP_DEST" ] || [ "$APP_DIR" = "$HOME/Applications" ]; then
        echo "✗ 装不进 ${APP_DIR}。换位置：./app/install-app.sh --app-dest ~/somewhere" >&2
        exit 2
    fi
    echo "⚠ $APP_DIR 写不进去（权限 / 沙箱），改装到 $HOME/Applications"
    APP_DIR="$HOME/Applications"
    APP_DEST_PATH="$APP_DIR/$APP_NAME"
    install_app_to "$APP_DIR" || { echo "✗ 装不进 $APP_DIR" >&2; exit 2; }
fi

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP_DEST_PATH" >/dev/null 2>&1 || true

echo "✓ Warden.app 已安装：$APP_DEST_PATH"

# ---------------------------------------------------------------- 唤起

say
say "用法："
say "  open -a \"$APP_DEST_PATH\"                    # 唤起（已开着就聚焦 + refresh）"
say "  Dock 点图标 / Command-Tab 切过来 / Spotlight 搜 Warden"
say
say "第一次唤起会弹「Warden 想要控制 iTerm2」—— 点允许（框只等 2 分钟）。"
say "点错/错过的话：tccutil reset AppleEvents $BUNDLE_ID 再唤起一次。"

if [ "$LAUNCH" = 1 ]; then
    say
    say "→ 唤起一次…"
    open -a "$APP_DEST_PATH"
fi
