#!/usr/bin/env python3
"""生成 Warden 的 app 图标（纯本地，用 PIL 画，不联网、不依赖设计文件）。

产出：app/Warden.icns + app/icon-1024.png

用法：
    python3 app/make-icon.py            # 默认画深色版
    python3 app/make-icon.py --accent ff5f56   # 换个强调色（十六进制）

改设计直接改下面的 draw()，再跑一次 build-app.sh 就行。
"""
import argparse
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ICONSET = os.path.join(HERE, "Warden.iconset")
BOLD_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FALLBACK_FONT = "/System/Library/Fonts/Helvetica.ttc"

# macOS 要求的 iconset：(逻辑尺寸, 该文件实际像素)
ICONSET_FILES = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(size, radius, scale=4):
    """超采样画圆角矩形做蒙版，避免锯齿。"""
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, size * scale - 1, size * scale - 1],
        radius=radius * scale, fill=255)
    return big.resize((size, size), Image.LANCZOS)


def draw(size, accent):
    """深色渐变底 + 圆角 + 一个「W」。"""
    bg_top = hex_rgb("1b2430")
    bg_bot = hex_rgb("0b0e14")
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=lerp(bg_top, bg_bot, y / max(1, size - 1)))

    # 底部一条强调色横条（视觉锚点）
    bar_h = max(2, int(size * 0.075))
    d.rectangle([0, size - bar_h, size, size], fill=accent)

    # 字体：优先 Arial Bold，找不到就退回 Helvetica
    font = None
    for path in (BOLD_FONT, FALLBACK_FONT):
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, int(size * 0.56))
                break
            except OSError:
                pass
    if font is None:
        font = ImageFont.load_default()

    text = "W"
    box = d.textbbox((0, 0), text, font=font)
    d.text(((size - (box[2] - box[0])) / 2 - box[0],
            (size - (box[3] - box[1])) / 2 - box[1] - size * 0.03),
           text, font=font, fill=(238, 242, 247))

    # 右下角一个小圆点，暗示「在线 / daemon running」
    r = max(2, int(size * 0.058))
    cx, cy = size - int(size * 0.20), int(size * 0.20)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=hex_rgb("2ecc71"))

    mask = rounded_mask(size, int(size * 0.225))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accent", default="4f9cf9", help="强调色，默认 4f9cf9（蓝）")
    args = ap.parse_args()
    accent = hex_rgb(args.accent)

    os.makedirs(ICONSET, exist_ok=True)
    base = draw(1024, accent)
    base.save(os.path.join(HERE, "icon-1024.png"))
    print("写 %d 个尺寸到 %s" % (len(ICONSET_FILES), ICONSET))
    for name, px in ICONSET_FILES:
        base.resize((px, px), Image.LANCZOS).save(os.path.join(ICONSET, name))

    icns = os.path.join(HERE, "Warden.icns")
    rc = subprocess.call(["iconutil", "-c", "icns", ICONSET, "-o", icns])
    if rc != 0:
        print("iconutil 失败（图标没生成，其余照常）", file=sys.stderr)
        return rc
    print("图标好了：%s" % icns)


if __name__ == "__main__":
    sys.exit(main())
