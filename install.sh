#!/bin/bash
# ti_extend 一键安装 —— 薄封装，真正的逻辑在 install.py（依赖 + Warden.app）。
#
# 留着它只是因为手习惯敲 ./install.sh。参数原样透传：
#
#   ./install.sh              == python3 install.py
#   ./install.sh --check      == python3 install.py --check
#   ./install.sh --skip-app   == python3 install.py --skip-app
#
# Warden.app 那一半的细节：
#   app/build-app.sh    怎么把 bundle 构建出来（swiftc / Info.plist / codesign / 图标）
#   app/install-app.sh  装到哪、怎么替换、要不要唤起
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/install.py" "$@"
