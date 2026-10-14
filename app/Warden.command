#!/bin/bash
# 起 warden TUI 的唯一入口。Finder 双击 / .app 壳 / 终端手敲 都走这里。
#
# 为什么要有这一层：Finder 双击启动的进程 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
# 拿不到 brew 的 python3/node，也拿不到你 shell 里的环境变量。这里把常见的补回去，
# 并且永远 cd 到仓库根（warden.py 里写死相对路径 temp/、data.csv 都靠它）。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO" || exit 1

# Finder 启动时的 PATH 修补：Homebrew(arm64) / Homebrew(intel) / pip --user / 本地 bin
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$PATH"

# node 是给 scripts/bridge.mjs 用的（写操作走它），缺了 TUI 能起但 mute/title/cancel 会报错
if ! command -v node >/dev/null 2>&1; then
    echo "⚠ 没找到 node —— TUI 能起、能看，但 mute / title / cancel 会失败。" >&2
fi

# 解释器可覆盖：WARDEN_PYTHON=/opt/homebrew/bin/python3 ./app/Warden.command
PY="${WARDEN_PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "✗ 找不到 python3。装一个，或 WARDEN_PYTHON=/path/to/python3 指一个。" >&2
    exit 2
fi

exec "$PY" warden.py "$@"
