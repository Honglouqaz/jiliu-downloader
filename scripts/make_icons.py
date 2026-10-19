#!/usr/bin/env python3
"""生成扩展图标：一滴墨青色的水（呼应「汲流」）。

不依赖 Pillow，直接按 PNG 规范写字节。4 倍超采样做抗锯齿。
"""
from __future__ import annotations

import pathlib
import struct
import zlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "extension" / "icons"
SIZES = (16, 48, 128)
SS = 4  # 超采样倍数

BG = (0x12, 0x16, 0x1C)          # 深色底
FG = (0x3F, 0xD0, 0xC9)          # 墨青
HI = (0x9B, 0xF2, 0xEC)          # 高光


def write_png(path: pathlib.Path, w: int, h: int, rgba: bytes) -> None:
    rows = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(typ: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + typ + data
        return out + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def inside_drop(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    """水滴：下方一个圆 + 上方一个收窄的尖。"""
    # 圆
    if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
        return True
    # 尖端（从圆心上方 r 处，向上收窄到 0）
    top = cy - r * 2.05
    if y < cy - r * 0.15 and y > top:
        t = (y - top) / ((cy - r * 0.15) - top)   # 0 在顶尖，1 在圆顶
        half = r * (t ** 0.85)
        return abs(x - cx) <= half
    return False


def render(size: int) -> bytes:
    n = size * SS
    cx, cy = n / 2.0, n / 2.0 + n * 0.015
    r = n * 0.315
    acc = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]

    for sy in range(n):
        for sx in range(n):
            x = sx + 0.5
            y = sy + 0.5
            if inside_drop(x, y, cx, cy, r):
                # 右上方向做一点高光
                d = ((x - (cx + r * 0.34)) ** 2 + (y - (cy - r * 0.30)) ** 2) ** 0.5
                t = max(0.0, 1.0 - d / (r * 0.78))
                col = (
                    int(FG[0] + (HI[0] - FG[0]) * t * 0.85),
                    int(FG[1] + (HI[1] - FG[1]) * t * 0.85),
                    int(FG[2] + (HI[2] - FG[2]) * t * 0.85),
                    255,
                )
            else:
                col = (BG[0], BG[1], BG[2], 0) if size <= 16 else (BG[0], BG[1], BG[2], 255)
            px = acc[sy // SS][sx // SS]
            for k in range(4):
                px[k] += col[k]

    buf = bytearray()
    total = SS * SS
    for row in acc:
        for px in row:
            for k in range(4):
                buf.append(min(255, px[k] // total))
    return bytes(buf)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for s in SIZES:
        p = OUT / f"icon{s}.png"
        write_png(p, s, s, render(s))
        # 校验：重新解析尺寸
        d = p.read_bytes()
        w, h = struct.unpack(">II", d[16:24])
        assert (w, h) == (s, s), f"{p} 尺寸异常 {w}x{h}"
        print(f"  {p.name:<14} {p.stat().st_size:>6} bytes  {w}x{h}")
    print("图标生成完成")


if __name__ == "__main__":
    main()
