from __future__ import annotations

import math
import random

from color import lerp_oklab, oklab_distance, linear_to_oklab
from extract import clip_chroma_to_gamut

CUT_DISTANCE = 0.30

DEADBAND_CODES = 2


def luminance(lin) -> float:
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


class PerceptualSmoother:
    def __init__(self, tau_up: float = 0.20, tau_down: float = 0.50,
                 autospeed: float = 10.0, cut_distance: float = CUT_DISTANCE):
        self.tau_up = max(0.01, tau_up)
        self.tau_down = max(0.01, tau_down)
        self.autospeed = autospeed
        self.cut_distance = cut_distance
        self.current: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.cuts = 0

    def reset(self, lin: tuple[float, float, float]) -> None:
        self.current = lin

    def step(self, target: tuple[float, float, float], dt: float,
             force_cut: bool = False):
        d_perc = oklab_distance(self.current, target)
        is_cut = force_cut or d_perc >= self.cut_distance
        if is_cut:
            self.cuts += 1
            self.current = target
            return self.current, d_perc, 1.0, True

        tau = self.tau_up if luminance(target) > luminance(self.current) else self.tau_down
        alpha = 1.0 - math.exp(-max(1e-4, dt) / tau)
        d_lin = sum(abs(t - c) for t, c in zip(target, self.current)) / 3.0
        snap = min(1.0, d_lin * self.autospeed / 10.0) if d_lin > 0.001 else 0.0
        a = min(1.0, alpha + (1.0 - alpha) * snap)

        self.current = target if a >= 0.999 else lerp_oklab(self.current, target, a)
        return self.current, d_perc, a, False


class DarkGate:
    def __init__(self, y_off: float = 0.0015, y_on: float = 0.0045,
                 dwell: float = 0.5):
        self.y_off = y_off
        self.y_on = y_on
        self.dwell = dwell
        self.dark = False
        self._since: float | None = None

    def update(self, lin, now: float) -> bool:
        y = luminance(lin)
        want_dark = y < self.y_off if not self.dark else y < self.y_on
        if want_dark == self.dark:
            self._since = None
            return self.dark
        if not want_dark:
            self.dark = False
            self._since = None
            return False
        if self._since is None:
            self._since = now
        elif now - self._since >= self.dwell:
            self.dark = True
            self._since = None
        return self.dark


class FlashGate:
    def __init__(self, ratio: float = 2.2, floor: float = 0.02,
                 baseline_tau: float = 0.6, release_ratio: float = 1.3,
                 hold: float = 0.22):
        self.ratio = ratio
        self.floor = floor
        self.baseline_tau = max(0.01, baseline_tau)
        self.release_ratio = release_ratio
        self.hold = hold
        self.baseline: float | None = None
        self.elevated = False
        self.peak: tuple[float, float, float] | None = None
        self._hold_until = 0.0

    def update(self, lin, dt: float, now: float) -> bool:
        y = luminance(lin)
        if self.baseline is None:
            self.baseline = y
            return False
        ratio_now = y / max(self.floor, self.baseline)
        was_elevated = self.elevated
        if self.elevated:
            self.elevated = ratio_now > self.release_ratio
        else:
            self.elevated = y > self.floor and ratio_now > self.ratio
        alpha = 1.0 - math.exp(-max(1e-4, dt) / self.baseline_tau)
        self.baseline += (y - self.baseline) * alpha
        rising = self.elevated and not was_elevated
        if rising:
            self.peak = lin
            self._hold_until = now + self.hold
        return rising

    def holding(self, now: float):
        if now < self._hold_until:
            return self.peak
        return None


class IdleDrift:
    def __init__(self, dwell: float = 6.0, still_distance: float = 0.01,
                 period_min: float = 4.0, period_max: float = 7.0,
                 hue_jitter: float = 0.07, light_jitter: float = 0.08):
        self.dwell = dwell
        self.still_distance = still_distance
        self.period_min = period_min
        self.period_max = max(period_min, period_max)
        self.hue_jitter = hue_jitter
        self.light_jitter = light_jitter
        self.anchor: tuple[float, float, float] | None = None
        self.active = False
        self._still_since: float | None = None
        self._next_tick = 0.0
        self._dl = 0.0
        self._dh = 0.0

    def update(self, target, now: float) -> None:
        if self.anchor is None:
            self.anchor = target
            self._still_since = now
            return
        if oklab_distance(target, self.anchor) > self.still_distance:
            self.anchor = target
            self._still_since = now
            self.active = False
            return
        self.anchor = target
        if self._still_since is None:
            self._still_since = now
        elif not self.active and now - self._still_since >= self.dwell:
            self.active = True
            self._next_tick = now
        if self.active and now >= self._next_tick:
            self._dh = random.uniform(-self.hue_jitter, self.hue_jitter)
            self._dl = random.uniform(-self.light_jitter, self.light_jitter)
            self._next_tick = now + random.uniform(self.period_min, self.period_max)

    def apply(self, target):
        if not self.active:
            return target
        L, a, b = linear_to_oklab(*target)
        C = math.hypot(a, b)
        h = math.atan2(b, a)
        return clip_chroma_to_gamut(max(0.0, L * (1.0 + self._dl)), C,
                                    h + self._dh)


class AutoContrast:
    def __init__(self, expand: float = 0.30, contract: float = 0.01,
                 min_span: float = 1.5, strength: float = 0.35):
        self.expand = expand
        self.contract = contract
        self.min_span = min_span
        self.strength = strength
        self._ceil: float | None = None
        self._floor: float | None = None

    def gain(self, lin) -> float:
        y = max(1e-4, luminance(lin))
        db = math.log2(y)
        if self._ceil is None:
            self._ceil = self._floor = db
        if db > self._ceil:
            self._ceil += (db - self._ceil) * self.expand
        else:
            self._ceil += (db - self._ceil) * self.contract
        if db < self._floor:
            self._floor += (db - self._floor) * self.expand
        else:
            self._floor += (db - self._floor) * self.contract
        span = max(self.min_span, self._ceil - self._floor)
        pos = min(1.0, max(0.0, (db - self._floor) / span))
        return 1.0 + self.strength * (0.5 - pos)


class MotionActivity:
    def __init__(self, smoothing: float = 1.0, calm_diff: float = 2.0,
                 busy_diff: float = 15.0, factor_min: float = 0.6,
                 factor_max: float = 1.6):
        self.smoothing = max(0.01, smoothing)
        self.calm_diff = calm_diff
        self.busy_diff = max(calm_diff + 1e-6, busy_diff)
        self.factor_min = factor_min
        self.factor_max = factor_max
        self.level = 0.0

    def factor(self, diff: float, dt: float) -> float:
        alpha = 1.0 - math.exp(-max(1e-4, dt) / self.smoothing)
        self.level += (diff - self.level) * alpha
        pos = (self.level - self.calm_diff) / (self.busy_diff - self.calm_diff)
        pos = min(1.0, max(0.0, pos))
        return self.factor_max + (self.factor_min - self.factor_max) * pos


class DominantHold:
    def __init__(self, hold_distance: float = 0.06):
        self.hold_distance = hold_distance
        self.hue: float | None = None
        self.suppressed = 0

    def apply(self, lin):
        L, a, b = linear_to_oklab(*lin)
        C = math.hypot(a, b)
        if C < 1e-6:
            self.hue = None
            return lin
        h = math.atan2(b, a)
        if self.hue is not None:
            dh = ((h - self.hue + math.pi) % (2.0 * math.pi)) - math.pi
            chord = 2.0 * C * math.sin(abs(dh) / 2.0)
            if chord < self.hold_distance:
                self.suppressed += 1
                h = self.hue
        self.hue = h
        return clip_chroma_to_gamut(L, C, h)

    def reset(self) -> None:
        self.hue = None


class SendScheduler:
    def __init__(self, fps: float = 20.0, deadband: int = DEADBAND_CODES,
                 keepalive: float = 20.0, cut_min_gap: float = 0.04):
        self.min_period = 1.0 / max(0.5, fps)
        self.deadband = deadband
        self.keepalive = keepalive
        self.cut_min_gap = cut_min_gap
        self.last_sent: tuple[int, int, int] | None = None
        self.last_at = -1e9
        self.sent = 0
        self.skipped_deadband = 0
        self.skipped_rate = 0
        self.keepalives = 0
        self.cut_sends = 0

    def decide(self, codes: tuple[int, int, int], now: float,
               is_cut: bool = False) -> bool:
        if self.last_sent is None:
            return True
        gap = now - self.last_at
        if is_cut:
            if gap >= self.cut_min_gap:
                return True
            self.skipped_rate += 1
            return False
        if gap < self.min_period:
            self.skipped_rate += 1
            return False
        if gap >= self.keepalive:
            return True
        delta = max(abs(c - l) for c, l in zip(codes, self.last_sent))
        if delta >= self.deadband:
            return True
        self.skipped_deadband += 1
        return False

    def mark_sent(self, codes: tuple[int, int, int], now: float,
                  is_cut: bool = False) -> None:
        if self.last_sent is not None and now - self.last_at >= self.keepalive:
            self.keepalives += 1
        if is_cut:
            self.cut_sends += 1
        self.last_sent = codes
        self.last_at = now
        self.sent += 1

    def stats(self) -> str:
        return ("отправлено %d (склеек %d, подтверждений %d), "
                "придержано: мёртвая зона %d, частота %d"
                % (self.sent, self.cut_sends, self.keepalives,
                   self.skipped_deadband, self.skipped_rate))

