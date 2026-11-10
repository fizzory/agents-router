#!/usr/bin/env python3
"""Generate original routing-mark PNG/ICNS assets using only the standard library.

The tray mark follows the review's two outgoing routes. Badge: an isolated dot
with a transparent cut-out halo at top right. Slash: across the entire mark.
All tray pixels are black plus alpha, suitable for icon_as_template(true).
"""
from pathlib import Path
import math
import struct
import zlib

OUT = Path(__file__).resolve().parents[1] / "macos/src-tauri/icons"


def png(width, height, rgba):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def render(size, variant="normal", app=False):
    aa = 4 if not app else (2 if size <= 128 else 1)
    width = size * aa
    scale = width / 20
    pixels = bytearray(width * width * 4)

    def circle(cx, cy, radius, color):
        x0, x1 = max(0, int((cx - radius) * scale)), min(width, math.ceil((cx + radius) * scale))
        y0, y1 = max(0, int((cy - radius) * scale)), min(width, math.ceil((cy + radius) * scale))
        for y in range(y0, y1):
            for x in range(x0, x1):
                if ((x + .5) / scale - cx) ** 2 + ((y + .5) / scale - cy) ** 2 <= radius ** 2:
                    at = (y * width + x) * 4
                    pixels[at:at + 4] = bytes(color)

    def line(a, b, radius, color):
        dx, dy = b[0] - a[0], b[1] - a[1]
        count = max(1, math.ceil(math.hypot(dx, dy) * scale * 1.5))
        for n in range(count + 1):
            circle(a[0] + dx * n / count, a[1] + dy * n / count, radius, color)

    def curve(a, b, c, d):
        points = []
        for n in range(33):
            t = n / 32
            points.append(tuple((1 - t) ** 3 * a[k] + 3 * (1 - t) ** 2 * t * b[k]
                                + 3 * (1 - t) * t * t * c[k] + t ** 3 * d[k] for k in (0, 1)))
        return points

    ink = (0, 0, 0, 255)
    if app:
        # Original app artwork, not an SF Symbol or a downloaded binary.
        for y in range(width):
            for x in range(width):
                px, py = (x + .5) / scale, (y + .5) / scale
                qx, qy = max(3.8 - px, 0, px - 16.2), max(3.8 - py, 0, py - 16.2)
                if qx * qx + qy * qy < 3.2 ** 2 and .6 <= px <= 19.4 and .6 <= py <= 19.4:
                    at = (y * width + x) * 4
                    pixels[at:at + 4] = bytes((32, 39, 48, 255))
        ink = (230, 241, 247, 255)

    paths = [[(3, 10), (8, 10)] + curve((8, 10), (11, 10), (11, 4), (14, 4))[1:] + [(17, 4)],
             [(14, 1), (17, 4), (14, 7)],
             curve((8, 10), (11, 10), (11, 16), (14, 16)) + [(17, 16)],
             [(14, 13), (17, 16), (14, 19)]]
    if app:
        paths = [[(2.5 + x * .75, 2.5 + y * .75) for x, y in points] for points in paths]
    for points in paths:
        for a, b in zip(points, points[1:]):
            line(a, b, .85, ink)
    if variant == "badge":
        circle(16.7, 3.3, 3.1, (0, 0, 0, 0))
        circle(16.7, 3.3, 1.75, ink)
    elif variant == "slash":
        line((2.5, 18), (18, 2), 1.65, (0, 0, 0, 0))
        line((2.5, 18), (18, 2), .8, ink)

    if aa > 1:
        down = bytearray(size * size * 4)
        for y in range(size):
            for x in range(size):
                for channel in range(4):
                    down[(y * size + x) * 4 + channel] = round(sum(
                        pixels[((y * aa + dy) * width + x * aa + dx) * 4 + channel]
                        for dy in range(aa) for dx in range(aa)) / (aa * aa))
        pixels = down
    return png(size, size, pixels)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for variant in ("normal", "badge", "slash"):
        for suffix, size in (("", 20), ("@2x", 40)):
            (OUT / f"tray-{variant}{suffix}.png").write_bytes(render(size, variant))
    (OUT / "icon.png").write_bytes(render(128, app=True))
    entries = []
    for tag, size in ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
                      (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024)):
        data = render(size, app=True)
        entries.append(tag + struct.pack(">I", len(data) + 8) + data)
    data = b"".join(entries)
    (OUT / "icon.icns").write_bytes(b"icns" + struct.pack(">I", len(data) + 8) + data)
    print(f"Generated app and six template icons in {OUT}")


if __name__ == "__main__":
    main()
