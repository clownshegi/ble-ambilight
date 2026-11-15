from __future__ import annotations

import struct
import zlib
from functools import lru_cache

import numpy as np

BG = (11, 10, 13)
WARM = (255, 112, 140)
CORE = (255, 214, 170)


def _png(rgb: np.ndarray) -> bytes:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in rgb)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack("!I", len(data)) + kind + data
                + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


@lru_cache(maxsize=8)
def icon_png(size: int) -> bytes:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    r = np.hypot((x - c) / 0.95, (y - c * 1.08) / 0.80) / (size / 2.0)
    glow = np.clip(1.0 - r / 1.10, 0.0, 1.0) ** 1.15
    core = np.clip(1.0 - r / 0.46, 0.0, 1.0) ** 1.3
    bg, warm, hot = (np.array(v, np.float32) for v in (BG, WARM, CORE))
    img = bg + (warm - bg) * glow[..., None]
    img = img + (hot - img) * core[..., None]
    return _png(np.clip(img + 0.5, 0, 255).astype(np.uint8))


def write_ico(path: str, sizes=(16, 24, 32, 48, 64, 256)) -> None:
    images = [icon_png(s) for s in sizes]
    offset = 6 + 16 * len(sizes)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    entries = b""
    for s, data in zip(sizes, images):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(head + entries + b"".join(images))


if __name__ == "__main__":
    import os
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lamp.ico")
    write_ico(out)
    print("записано:", out)

