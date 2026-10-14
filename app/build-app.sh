#!/bin/bash
# 把 app/WardenMain.swift 编译成 Warden.app —— 一个可被 Dock / Command-Tab /
# osascript / open -a 在前台唤起的 macOS 应用。
#
#   ./app/build-app.sh                          # 生成 app/Warden.app
#   ./app/build-app.sh --out ~/Applications/Warden.app
#   ./app/build-app.sh --install-profile        # 顺带装 iTerm2 的「Warden」外观
#   ./app/build-app.sh --uninstall-profile      # 把上面那份外观删掉
#
# 依赖：swiftc（随 Xcode Command Line Tools 自带，xcode-select --install）
#       + macOS 自带 osascript / codesign / lsregister / iconutil。
# 图标：app/Warden.icns 不存在就自动跑 make-icon.py 生成（需要 PIL）。
#
# 为什么不是 osacompile 的 AppleScript applet：applet 没有 activate 钩子，
# 于是「点 Dock」能用、「Command-Tab」只剩一个空窗口。见 README「踩过的坑」。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SRC="$HERE/WardenMain.swift"
OUT="$HERE/Warden.app"
ICNS="$HERE/Warden.icns"
PROFILE="$HERE/warden-profile.json"
PROFILE_DEST="$HOME/Library/Application Support/iTerm2/DynamicProfiles/warden.json"
BUNDLE_ID="com.ti-extend.warden"

ACTION_INSTALL_PROFILE=0
ACTION_UNINSTALL_PROFILE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="${2:?--out 后面要给路径}"; shift 2 ;;
        --out=*) OUT="${1#*=}"; shift ;;
        --install-profile) ACTION_INSTALL_PROFILE=1; shift ;;
        --uninstall-profile) ACTION_UNINSTALL_PROFILE=1; shift ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

if [ "$ACTION_UNINSTALL_PROFILE" = 1 ]; then
    rm -f "$PROFILE_DEST"
    echo "已删掉 ${PROFILE_DEST}（iTerm2 会热加载，不用重启）"
    exit 0
fi

# ---- 前置检查 ----
[ -f "$SRC" ] || { echo "✗ 找不到 $SRC" >&2; exit 2; }
if ! command -v swiftc >/dev/null 2>&1; then
    echo "✗ 没有 swiftc。装 Command Line Tools：xcode-select --install" >&2
    exit 2
fi
# 仓库路径要塞进 Swift 字符串字面量，先挡掉反斜杠和引号
case "$REPO" in
    *'"'*|*'\\'*) echo "✗ 仓库路径里有 \" 或 \\，塞不进 Swift 字面量：$REPO" >&2; exit 2 ;;
esac

if [ ! -f "$ICNS" ]; then
    echo "→ 没有 Warden.icns，先生成…"
    python3 "$HERE/make-icon.py"
fi

echo "→ 仓库：$REPO"
echo "→ 输出：$OUT"

# ---- 组装 bundle ----
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
<key>CFBundleName</key><string>Warden</string>
<key>CFBundleDisplayName</key><string>Warden</string>
<key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
<key>CFBundleExecutable</key><string>Warden</string>
<key>CFBundleIconFile</key><string>Warden</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
<!-- regular：出现在 Dock 和 Command-Tab 里。这正是被要求的效果。 -->
<key>LSUIElement</key><false/>
<!-- 首次唤起 iTerm 时系统弹的那个授权框，用这句话解释自己（默认是英文样板） -->
<key>NSAppleEventsUsageDescription</key>
<string>Warden 需要控制 iTerm2，才能把已有的看板窗口调到前台。</string>
</dict>
</plist>
PLIST

printf 'APPL????' > "$OUT/Contents/PkgInfo"
[ -f "$ICNS" ] && cp "$ICNS" "$OUT/Contents/Resources/Warden.icns"

# ---- 编译 ----
# SWIFTC_FLAGS 是逃生口：在受限环境（比如沙箱）里跑 build 时，可以把 clang 的
# 模块缓存挪到可写目录。正常使用不需要设。
TMP_SWIFT="$(mktemp -t WardenMain).swift"
trap 'rm -f "$TMP_SWIFT"' EXIT
sed "s|__REPO__|$REPO|g" "$SRC" > "$TMP_SWIFT"
if ! grep -q "let REPO = \"$REPO\"" "$TMP_SWIFT"; then
    echo "✗ REPO 没替换成功，检查 $SRC 里的 __REPO__ 占位符" >&2
    exit 2
fi

# shellcheck disable=SC2086
swiftc ${SWIFTC_FLAGS:-} -O -target arm64-apple-macos13.0 \
    -o "$OUT/Contents/MacOS/Warden" -framework Cocoa "$TMP_SWIFT"
[ -x "$OUT/Contents/MacOS/Warden" ] || { echo "✗ 编译没产出可执行文件" >&2; exit 2; }

# ---- 签名（关键，别删）----
# 无签名的 app 会让 TCC 拒绝记录任何授权决策 —— 表现是**连授权弹框都不弹**，
# 直接 -1743「未获得授权将Apple事件发送给iTerm」，设置里的「自动化」也找不到它。
# designated requirement 显式只认 identifier：即席签名默认拿 cdhash 当身份，
# 那样每次重新 build 都会被当成「另一个 app」，授权全部作废。
codesign --force --sign - \
    --identifier "$BUNDLE_ID" \
    --requirements "=designated => identifier \"$BUNDLE_ID\"" \
    "$OUT" 2>/dev/null || { echo "✗ codesign 失败（签名坏了会直接导致授权弹框不出现）" >&2; exit 2; }
if ! codesign --verify --verbose=2 "$OUT" >/dev/null 2>&1; then
    echo "✗ 签名校验没过，TCC 会拒绝记录授权。停在这里比装上去强。" >&2
    exit 2
fi

touch "$OUT"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$OUT" >/dev/null 2>&1 || true

# ---- iTerm2 外观（可选，默认不碰你的 iTerm 配置）----
# 只做两件事：给窗口一个固定标题和一套深色配色，让「Warden 窗口」一眼认得出。
# 不写 Custom Command —— 跑什么命令由 warden-summon.applescript 决定，写两处会跑两遍。
if [ ! -f "$PROFILE" ]; then
    cat > "$PROFILE" <<'JSON'
[
  {
    "Name": "Warden",
    "Guid": "warden-tui-profile-1",
    "Dynamic Profile Parent Name": "Default",
    "Use Custom Window Title": true,
    "Custom Window Title": "Warden",
    "Scrollback Lines": 2000,
    "Terminal Type": "xterm-256color",
    "Background Color": { "Red Component": 0.043, "Green Component": 0.055, "Blue Component": 0.078, "Alpha Component": 1, "Color Space": "sRGB" },
    "Foreground Color": { "Red Component": 0.847, "Green Component": 0.871, "Blue Component": 0.914, "Alpha Component": 1, "Color Space": "sRGB" },
    "Bold Color": { "Red Component": 0.949, "Green Component": 0.961, "Blue Component": 0.980, "Alpha Component": 1, "Color Space": "sRGB" },
    "Cursor Color": { "Red Component": 0.310, "Green Component": 0.612, "Blue Component": 0.976, "Alpha Component": 1, "Color Space": "sRGB" },
    "Cursor Text Color": { "Red Component": 0.043, "Green Component": 0.055, "Blue Component": 0.078, "Alpha Component": 1, "Color Space": "sRGB" },
    "Selection Color": { "Red Component": 0.169, "Green Component": 0.227, "Blue Component": 0.333, "Alpha Component": 1, "Color Space": "sRGB" }
  }
]
JSON
    echo "→ 生成了 iTerm2 外观草稿：$PROFILE"
fi

if [ "$ACTION_INSTALL_PROFILE" = 1 ]; then
    mkdir -p "$(dirname "$PROFILE_DEST")"
    cp "$PROFILE" "$PROFILE_DEST"
    echo "✓ 已装 iTerm2 外观：${PROFILE_DEST}（iTerm2 热加载，不用重启）"
else
    echo "→ iTerm2 外观没装（不想没验证过就动你的 iTerm 配置）。想装：./app/build-app.sh --install-profile"
fi

echo
echo "✓ 好了：$OUT"
echo
echo "  唤起：  open -a \"$OUT\"            （已经开着就聚焦，没开就新建）"
echo "          Dock 点图标 / Command-Tab 切过来 / tell application \"Warden\" to activate"
echo "          osascript \"$HERE/warden-summon.applescript\"        # 不走 app 也行"
echo "  试算：  osascript \"$HERE/warden-summon.applescript\" --dry-run"
echo "  排障：  cat temp/warden-app.log temp/warden-summon.log"
echo "  固定：  Finder 里把 Warden.app 拖进 Dock，或 ln -s 到 ~/Applications/"
