from __future__ import annotations

import json
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict

import numpy as np

log = logging.getLogger("color")


_SRGB_TO_LINEAR = np.array(
    [(c / 255.0 / 12.92) if (c / 255.0) <= 0.04045
     else (((c / 255.0) + 0.055) / 1.055) ** 2.4 for c in range(256)],
    dtype=np.float32,
)


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def decode_pixels(px: np.ndarray) -> np.ndarray:
    return _SRGB_TO_LINEAR[px]


def encode_rgb(lin: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(255.0 * linear_to_srgb(min(1.0, max(0.0, c))) for c in lin)


def linear_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_ = math.copysign(abs(l) ** (1.0 / 3.0), l)
    m_ = math.copysign(abs(m) ** (1.0 / 3.0), m)
    s_ = math.copysign(abs(s) ** (1.0 / 3.0), s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_linear(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def oklab_to_oklch(L: float, a: float, b: float) -> tuple[float, float, float]:
    return L, math.hypot(a, b), math.atan2(b, a)


def oklch_to_oklab(L: float, C: float, h: float) -> tuple[float, float, float]:
    return L, C * math.cos(h), C * math.sin(h)


def lerp_oklab(c0, c1, t: float) -> tuple[float, float, float]:
    L0, a0, b0 = linear_to_oklab(*c0)
    L1, a1, b1 = linear_to_oklab(*c1)
    return oklab_to_linear(
        L0 + (L1 - L0) * t, a0 + (a1 - a0) * t, b0 + (b1 - b0) * t)


def oklab_distance(c0, c1) -> float:
    L0, a0, b0 = linear_to_oklab(*c0)
    L1, a1, b1 = linear_to_oklab(*c1)
    return math.sqrt((L1 - L0) ** 2 + (a1 - a0) ** 2 + (b1 - b0) ** 2)


def _default_profile_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                       "profile.json"))
        candidates.append(os.path.join(getattr(sys, "_MEIPASS", here),
                                       "profile.json"))
    candidates.append(os.path.join(here, "profile.json"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]


DEFAULT_PROFILE_PATH = _default_profile_path()

FULL_BRIGHTNESS = 0xFF


def _interp(x: float, pts) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-9, x1 - x0)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    return y1 + (y1 - y0) * (x - x1) / max(1e-9, x1 - x0)


def _load_curve(raw):
    if not raw:
        return None
    pts = sorted(tuple(float(v) for v in p) for p in raw)
    if len(pts) < 2 or any(len(p) != 3 for p in pts):
        raise ValueError("channel_curve: нужны точки [R, G, B], хотя бы две")
    return tuple(pts)


@dataclass
class OutputProfile:
    white_balance: tuple[float, float, float] = (1.0, 1.0, 1.0)
    led_gamma: float = 1.0
    floor: int = 3
    max_level: float = 1.0
    min_level: float = 0.0
    channel_curve: tuple | None = None


    def _curve_points(self, ch: int):
        pts = [(0.0, 0.0)] + [(float(p[0]), float(p[ch])) for p in self.channel_curve]
        return pts

    def to_bytes(self, units: list[float]) -> list[float]:
        if not self.channel_curve:
            return units
        out = [units[0]]
        for ch in (1, 2):
            out.append(_interp(units[ch], self._curve_points(ch)))
        return out

    def to_light(self, codes) -> list[float]:
        if not self.channel_curve:
            return [c / max(1e-3, w) for c, w in zip(codes, self.white_balance)]
        out = [float(codes[0])]
        for ch in (1, 2):
            pts = [(b, a) for a, b in self._curve_points(ch)]
            out.append(_interp(float(codes[ch]), pts))
        return out

    @classmethod
    def load(cls, path: str = DEFAULT_PROFILE_PATH) -> "OutputProfile":
        if not os.path.exists(path):
            log.warning("Калибровка %s не найдена - работаю без неё "
                        "(python calibrate.py <MAC>)", path)
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                white_balance=tuple(data.get("white_balance", (1.0, 1.0, 1.0))),
                led_gamma=float(data.get("led_gamma", 1.0)),
                floor=int(data.get("floor", 3)),
                max_level=float(data.get("max_level", 1.0)),
                min_level=float(data.get("min_level", 0.0)),
                channel_curve=_load_curve(data.get("channel_curve")),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            log.warning("Калибровка %s повреждена (%s) - работаю без неё",
                        path, e)
            return cls()

    def save(self, path: str = DEFAULT_PROFILE_PATH) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def apply(self, lin: tuple[float, float, float],
              brightness: float = 1.0,
              gamma_out: float | None = None,
              min_level: float | None = None) -> tuple[int, int, int]:
        gamma = self.led_gamma if gamma_out is None else gamma_out
        balance = (1.0, 1.0, 1.0) if self.channel_curve else self.white_balance
        vals = [min(1.0, max(0.0, c * g)) for c, g in zip(lin, balance)]
        level = max(vals)
        if level <= 1e-9:
            return 0, 0, 0
        curved = level ** (1.0 / max(0.05, gamma))
        if min_level is None:
            min_level = self.min_level
        if min_level > 0.0:
            curved = min_level + (1.0 - min_level) * curved
        ratio = curved / level * brightness * self.max_level
        out = [255.0 * min(1.0, v * ratio) for v in vals]
        peak = max(out)
        if peak <= 0.0:
            return 0, 0, 0
        if peak < self.floor:
            lift = self.floor / peak
            out = [min(255.0, v * lift) for v in out]
        out = self.to_bytes(out)
        return tuple(int(round(min(255.0, max(0.0, v)))) for v in out)

    def scale_codes(self, codes: tuple[int, int, int],
                    brightness: float) -> tuple[int, int, int]:
        k = max(0.0, brightness) * self.max_level
        out = [c * k for c in codes]
        peak = max(out)
        if peak <= 0.0:
            return 0, 0, 0
        if peak < self.floor:
            lift = self.floor / peak
            out = [v * lift for v in out]
        return tuple(int(round(min(255.0, v))) for v in out)


def split_ww(codes: tuple[int, int, int],
             min_w: int = 12) -> tuple[int, int, int, int]:
    level = max(codes)
    if level <= 0:
        return 0, 0, 0, FULL_BRIGHTNESS
    if level >= min_w:
        scale = 255.0 / level
        return (min(255, int(round(codes[0] * scale))),
                min(255, int(round(codes[1] * scale))),
                min(255, int(round(codes[2] * scale))),
                level)
    scale = 255.0 / min_w
    return (min(255, int(round(codes[0] * scale))),
            min(255, int(round(codes[1] * scale))),
            min(255, int(round(codes[2] * scale))),
            min_w)

