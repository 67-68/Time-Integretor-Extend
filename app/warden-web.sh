#!/bin/bash
# 把 warden TUI 变成浏览器里的一个「应用」——最接近 PWA 的那条路。
#
#   brew install ttyd
#   ./app/warden-web.sh                 # 起服务 + 用 Chrome/Edge 的 app 模式开窗
#   ./app/warden-web.sh --port 8080
#   ./app/warden-web.sh --no-open       # 只起服务，自己开浏览器
#   ./app/warden-web.sh --lan           # 允许局域网访问（手机/平板）——默认带随机密码
#   ./app/warden-web.sh --lan --user me:secret
#
# 原理：ttyd 起一个真 pty 跑 warden.py，浏览器里是 xterm.js。
# 所以 TUI 的样子、按键（Tab / Shift+Tab）全都原样保留，代价是它仍然是「终端」外观。
#
# 想要独立窗口 + Dock 图标：Chrome/Edge 里 菜单 → 投放/保存/分享 → 安装页面为应用。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

PORT=7681
IFACE=lo
OPEN=1
USERPASS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?}"; shift 2 ;;
        --port=*) PORT="${1#*=}"; shift ;;
        --no-open) OPEN=0; shift ;;
        --lan) IFACE=0.0.0.0; shift ;;
        --user) USERPASS="${2:?}"; shift 2 ;;
        --user=*) USERPASS="${1#*=}"; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

if ! command -v ttyd >/dev/null 2>&1; then
    cat >&2 <<'EOF'
✗ 没装 ttyd。它就是把终端搬到浏览器的那一层：

    brew install ttyd

装完再跑一次 ./app/warden-web.sh
EOF
    exit 2
fi

URL="http://localhost:$PORT"

ARGS=(-p "$PORT" -i "$IFACE" -W
      -t "titleFixed=Warden"
      -t "fontSize=14"
      -t "scrollback=5000"
      -t 'fontFamily="SFMono-Regular, Menlo, JetBrains Mono, monospace"'
      -t 'theme={"background":"#0b0e14","foreground":"#d8dee9","cursor":"#4f9cf9","cursorAccent":"#0b0e14","selectionBackground":"#2b3a55"}')

if [ "$IFACE" = "0.0.0.0" ]; then
    if [ -z "$USERPASS" ]; then
        # 没有登录态的 TUI 能取消 session —— 别裸奔在局域网上，随手给个密码
        PASS="$(LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 12 || true)"
        USERPASS="warden:$PASS"
    fi
    ARGS+=(-c "$USERPASS")
    LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<本机IP>')"
    URL="http://$LAN_IP:$PORT"
    echo "⚠ 局域网模式：$URL   登录 $USERPASS"
    echo "  （用完 Ctrl-C 关掉；这东西能取消你的 session，别长期开着）"
fi

echo "→ 起 ttyd：http://localhost:$PORT"
ttyd "${ARGS[@]}" bash -lc "cd '$REPO' && exec ./app/Warden.command" &
TTYD_PID=$!
trap 'kill $TTYD_PID 2>/dev/null || true' EXIT INT TERM

sleep 1

if [ "$OPEN" = 1 ] && [ "$IFACE" = "lo" ]; then
    opened=0
    for app in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium" "Vivaldi"; do
        if [ -d "/Applications/$app.app" ]; then
            # --app= 开出来的是无地址栏的独立窗口，就是 PWA 那种「感觉」
            open -na "$app" --args --app="$URL" && opened=1 && break
        fi
    done
    if [ "$opened" = 0 ]; then
        echo "  （没找到 Chrome 系浏览器，用默认浏览器开；想要独立窗口得用 Chrome/Edge）"
        open "$URL"
    fi
fi

wait $TTYD_PID
