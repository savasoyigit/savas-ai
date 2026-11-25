"""K-3b: home-screen icons for the CEO Interface (ui/icons/*.png), stdlib only.

Draws the K-3 pixel Ares on the page background (the map lives here since
K-3d moved the office characters to vector SVG):  python3 scripts/make_ui_icons.py
"""

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARES = [
    "......AAAA......", ".....AAAAAA.....", "....OHHHHHHO....", "...OHHHHHHHHO...",
    "...OHSSSSSSHO...", "...OHSESSESHO...", "....OHSSSSHO....", ".....OSSSSO.....",
    "...OBBBBBBBBO...", "..OBBBbBBbBBBO..", "..OSBBBBBBBBSO..", "..OSBbbbbbbBSO..",
    "...OBBBBBBBBO...", "....OLLOOLLO....", "....OLLOOLLO....", "....OKKOOKKO....",
]
COLORS = {"O": "#14102a", "E": "#14102a", "L": "#2b2d42", "K": "#0b0b14", "S": "#f1c27d",
          "H": "#7b4dff", "A": "#ff3b5c", "B": "#5a2fd6", "b": "#3a1d99"}
BG = (0x0B, 0x10, 0x24)


def palette():
    return {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for k, v in COLORS.items()}


def png(size, pixels):
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def icon(size):
    grid = ARES
    colors = palette()
    scale = int(size * 0.7) // 16
    offset = (size - scale * 16) // 2
    pixels = [[BG] * size for _ in range(size)]
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch == "." or ch not in colors:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    pixels[offset + y * scale + dy][offset + x * scale + dx] = colors[ch]
    return png(size, pixels)


if __name__ == "__main__":
    for size in (180, 192, 512):
        out = ROOT / "ui" / "icons" / f"icon-{size}.png"
        out.write_bytes(icon(size))
        print(out, out.stat().st_size)
