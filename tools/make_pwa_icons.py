"""Generate the Jarvis PWA icons (stdlib only: zlib + struct) into jarvis/static/.

Run:  python tools/make_pwa_icons.py
Output: icon-192.png, icon-512.png, icon-maskable-512.png (safe-zone artwork, full-bleed background),
apple-touch-icon.png (180, opaque). Design: dark page background, a glowing blue-to-green ring, a light "J".
"""
import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "jarvis" / "static"
BG = (0x0E, 0x11, 0x16)
BLUE, GREEN, TEXT = (0x6C, 0xB6, 0xFF), (0x7E, 0xE0, 0xA0), (0xD8, 0xDE, 0xE9)


def _smooth(edge: float, d: float, aa: float) -> float:
    """Coverage 1 inside (d < edge), 0 outside, antialiased over `aa`."""
    return min(1.0, max(0.0, (edge - d) / aa + 0.5))


def _seg(px, py, ax, ay, bx, by):
    vx, vy, wx, wy = bx - ax, by - ay, px - ax, py - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _j_dist(x, y):
    """Distance to the J's centre line (unit coordinates, origin at the icon centre)."""
    cx, cy, r = -0.03, 0.07, 0.09
    d = _seg(x, y, cx + r, -0.19, cx + r, cy)  # stem
    if y >= cy:
        d = min(d, abs(math.hypot(x - cx, y - cy) - r))  # hook (lower half circle)
    else:
        d = min(d, math.hypot(x - (cx - r), y - cy))  # round end of the hook
    return d


def render(size: int, scale: float, round_bg: bool = False) -> bytes:
    aa = 1.5 / size
    rows = []
    for j in range(size):
        y = ((j + 0.5) / size - 0.5) / scale
        row = bytearray([0])
        for i in range(size):
            x = ((i + 0.5) / size - 0.5) / scale
            rad = math.hypot(x, y)
            ring_d = abs(rad - 0.36)
            t = min(1.0, max(0.0, (y / 0.36 + 1) / 2))  # top blue to bottom green (no seam)
            col = tuple(BLUE[k] + (GREEN[k] - BLUE[k]) * t for k in range(3))
            r, g, b = BG
            glow = 0.55 * math.exp(-ring_d / 0.045)
            r, g, b = (c + (cc - c) * glow for c, cc in zip((r, g, b), col))
            a = _smooth(0.022, ring_d, aa / scale)
            r, g, b = (c + (cc - c) * a for c, cc in zip((r, g, b), col))
            a = _smooth(0.042, _j_dist(x, y + 0.01), aa / scale)
            r, g, b = (c + (cc - c) * a for c, cc in zip((r, g, b), TEXT))
            row += bytes((int(min(255, max(0, v))) for v in (r, g, b)))
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int, raw: bytes) -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, scale in (("icon-192.png", 192, 1.0), ("icon-512.png", 512, 1.0),
                              ("icon-maskable-512.png", 512, 0.72), ("apple-touch-icon.png", 180, 0.9)):
        (OUT / name).write_bytes(png(size, render(size, scale)))
        print("wrote", OUT / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
