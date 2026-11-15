from __future__ import annotations

import numpy as np

from color import decode_pixels, linear_to_oklab, oklab_to_linear, oklch_to_oklab

MODES = ("average", "vibrant", "dominant")

LUMA = np.float32([0.2126, 0.7152, 0.0722])

CHROMA_EPS = 0.02

_SQRT3_2 = 0.8660254037844386


def to_linear_rgb(px: np.ndarray) -> np.ndarray:
    lin = decode_pixels(px)
    return lin[..., ::-1].reshape(-1, 3)


def opponent(rgb: np.ndarray):
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    oa = r - 0.5 * (g + b)
    ob = (g - b) * _SQRT3_2
    return oa, ob, np.hypot(oa, ob)


def _in_gamut(lin) -> bool:
    return all(-1e-4 <= c <= 1.0 + 1e-4 for c in lin)


def clip_chroma_to_gamut(L: float, C: float, h: float):
    lin = oklab_to_linear(*oklch_to_oklab(L, C, h))
    if _in_gamut(lin):
        return lin
    lo, hi = 0.0, C
    for _ in range(12):
        mid = 0.5 * (lo + hi)
        probe = oklab_to_linear(*oklch_to_oklab(L, mid, h))
        if _in_gamut(probe):
            lo = mid
        else:
            hi = mid
    return oklab_to_linear(*oklch_to_oklab(L, lo, h))


def boost_chroma(mean_lin, mean_a: float, mean_b: float,
                 mean_pixel_chroma: float, gain: float):
    if gain <= 1.0 or mean_pixel_chroma < 1e-9:
        return mean_lin
    coherence = min(1.0, float(np.hypot(mean_a, mean_b)) / mean_pixel_chroma)
    effective = 1.0 + (gain - 1.0) * (coherence ** 1.5)
    if effective <= 1.001:
        return mean_lin
    L, a, b = linear_to_oklab(*mean_lin)
    C = float(np.hypot(a, b))
    if C < 1e-6:
        return mean_lin
    return clip_chroma_to_gamut(L, C * effective, float(np.arctan2(b, a)))


def extract_average(rgb: np.ndarray):
    m = rgb.mean(axis=0)
    return float(m[0]), float(m[1]), float(m[2])


def with_scene_lightness(tinted, plain_mean):
    L_plain = linear_to_oklab(*plain_mean)[0]
    L_w, a_w, b_w = linear_to_oklab(*tinted)
    C_w = float(np.hypot(a_w, b_w))
    if L_w <= 1e-6 or C_w < 1e-6:
        return plain_mean
    C_out = C_w * min(1.0, L_plain / L_w)
    return clip_chroma_to_gamut(L_plain, C_out, float(np.arctan2(b_w, a_w)))


def extract_vibrant(rgb: np.ndarray, sat_gain: float = 1.15,
                    lock_lightness: bool = True):
    oa, ob, chroma = opponent(rgb)
    y = rgb @ LUMA
    w = (chroma + CHROMA_EPS) * y
    total = float(w.sum())
    plain = rgb.mean(axis=0)
    plain_t = (float(plain[0]), float(plain[1]), float(plain[2]))
    if total < 1e-9:
        return plain_t
    m = (rgb * w[:, None]).sum(axis=0) / total
    mean_chroma = float((chroma * w).sum() / total)
    mean_a = float((oa * w).sum() / total)
    mean_b = float((ob * w).sum() / total)
    tinted = boost_chroma((float(m[0]), float(m[1]), float(m[2])),
                          mean_a, mean_b, mean_chroma, sat_gain)
    return with_scene_lightness(tinted, plain_t) if lock_lightness else tinted


def extract_dominant(rgb: np.ndarray, bins: int = 12, min_light: float = 0.004,
                     min_chroma: float = 0.03, sat_gain: float = 1.10,
                     min_share: float = 0.55, lock_lightness: bool = True):
    oa, ob, chroma = opponent(rgb)
    y = rgb @ LUMA
    mask = (y > min_light) & (chroma > min_chroma)
    if int(mask.sum()) < 24:
        return extract_vibrant(rgb, sat_gain=1.10, lock_lightness=lock_lightness)
    hue = np.arctan2(ob[mask], oa[mask])
    weight = chroma[mask] * np.sqrt(np.maximum(y[mask], 0.0))
    idx = ((hue + np.pi) / (2.0 * np.pi) * bins).astype(np.int32) % bins
    totals = np.bincount(idx, weights=weight, minlength=bins)
    best = int(totals.argmax())
    neighbours = [best, (best - 1) % bins, (best + 1) % bins]
    grand = float(totals.sum())
    share = float(totals[neighbours].sum())
    if grand > 0 and share / grand < min_share:
        return extract_vibrant(rgb, sat_gain=1.15, lock_lightness=lock_lightness)
    keep = np.isin(idx, neighbours)
    sel = rgb[mask][keep]
    wk = weight[keep]
    tot = float(wk.sum())
    if tot < 1e-9:
        return extract_vibrant(rgb)
    m = (sel * wk[:, None]).sum(axis=0) / tot
    mean_chroma = float((chroma[mask][keep] * wk).sum() / tot)
    mean_a = float((oa[mask][keep] * wk).sum() / tot)
    mean_b = float((ob[mask][keep] * wk).sum() / tot)
    tinted = boost_chroma((float(m[0]), float(m[1]), float(m[2])),
                          mean_a, mean_b, mean_chroma, sat_gain)
    if not lock_lightness:
        return tinted
    plain = rgb.mean(axis=0)
    return with_scene_lightness(
        tinted, (float(plain[0]), float(plain[1]), float(plain[2])))


def frame_difference(cur: np.ndarray, prev: np.ndarray) -> float:
    if prev is None or cur.shape != prev.shape:
        return 0.0
    return float(np.abs(cur.astype(np.int16) - prev.astype(np.int16)).mean())


def extract(px: np.ndarray, mode: str = "vibrant", **kw):
    rgb = to_linear_rgb(px)
    if mode == "average":
        return extract_average(rgb)
    if mode == "dominant":
        return extract_dominant(rgb, **kw)
    return extract_vibrant(rgb, **kw)

