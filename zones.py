from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from extract import extract

EDGE_H = 0.08
EDGE_V = 0.05


@dataclass(frozen=True)
class Zone:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    def slice_of(self, px: np.ndarray) -> np.ndarray:
        h, w = px.shape[:2]
        a = max(0, min(h - 1, int(round(self.y0 * h))))
        b = max(a + 1, min(h, int(round(self.y1 * h))))
        c = max(0, min(w - 1, int(round(self.x0 * w))))
        d = max(c + 1, min(w, int(round(self.x1 * w))))
        return px[a:b, c:d]


def _split_horizontal(n: int) -> list[Zone]:
    out = []
    for i in range(n):
        out.append(Zone("%d" % (i + 1), i / n, 0.0, (i + 1) / n, 1.0))
    return out


def _perimeter(n_top: int, n_right: int, n_bottom: int, n_left: int) -> list[Zone]:
    zones: list[Zone] = []
    for i in range(n_top):
        zones.append(Zone("top%d" % (i + 1), i / n_top, 0.0,
                          (i + 1) / n_top, EDGE_V))
    for i in range(n_right):
        zones.append(Zone("right%d" % (i + 1), 1.0 - EDGE_H, i / n_right,
                          1.0, (i + 1) / n_right))
    for i in range(n_bottom):
        j = n_bottom - 1 - i
        zones.append(Zone("bottom%d" % (i + 1), j / n_bottom, 1.0 - EDGE_V,
                          (j + 1) / n_bottom, 1.0))
    for i in range(n_left):
        j = n_left - 1 - i
        zones.append(Zone("left%d" % (i + 1), 0.0, j / n_left,
                          EDGE_H, (j + 1) / n_left))
    return zones


LAYOUTS = {
    "single": lambda: [Zone("all", 0.0, 0.0, 1.0, 1.0)],
    "lr": lambda: _split_horizontal(2),
    "lcr": lambda: _split_horizontal(3),
    "quad": lambda: [
        Zone("tl", 0.0, 0.0, 0.5, 0.5), Zone("tr", 0.5, 0.0, 1.0, 0.5),
        Zone("bl", 0.0, 0.5, 0.5, 1.0), Zone("br", 0.5, 0.5, 1.0, 1.0),
    ],
    "edges": lambda: _perimeter(5, 3, 4, 3),
}


def make_layout(spec: str) -> list[Zone]:
    if spec in LAYOUTS:
        return LAYOUTS[spec]()
    if spec.startswith("strips:"):
        n = int(spec.split(":", 1)[1])
        if n < 1:
            raise ValueError("нужна хотя бы одна полоса")
        return _split_horizontal(n)
    if spec.startswith("perimeter:"):
        parts = [int(x) for x in spec.split(":", 1)[1].split(",")]
        if len(parts) != 4 or min(parts) < 0 or sum(parts) < 1:
            raise ValueError("нужно четыре неотрицательных числа: сверху,справа,снизу,слева")
        return _perimeter(*parts)
    raise ValueError("неизвестная раскладка: %s" % spec)


def describe(zones: list[Zone]) -> str:
    return "%d зон: %s" % (len(zones), ", ".join(z.name for z in zones))


def sample(px: np.ndarray, zones: list[Zone], mode: str = "vibrant") -> list:
    if len(zones) == 1 and zones[0].x1 - zones[0].x0 >= 1.0 \
            and zones[0].y1 - zones[0].y0 >= 1.0:
        return [extract(px, mode)]
    return [extract(z.slice_of(px), mode) for z in zones]

