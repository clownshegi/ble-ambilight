from __future__ import annotations

import math
import random
import time

from color import encode_rgb, OutputProfile


def _hash(i: int, seed: int) -> float:
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFF) / float(0x7FFFFF) - 1.0


class Noise:
    def __init__(self, seed: int):
        self.seed = seed & 0xFFFFFFFF

    def __call__(self, x: float) -> float:
        i = math.floor(x)
        f = x - i
        u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0)
        a, b = _hash(i, self.seed), _hash(i + 1, self.seed)
        return a + (b - a) * u

    def fbm(self, x: float, octaves: int = 3) -> float:
        total = norm = 0.0
        amp, freq = 1.0, 1.0
        for o in range(octaves):
            total += amp * self(x * freq + o * 17.31)
            norm += amp
            amp *= 0.5
            freq *= 2.0
        return total / norm


class Drift:
    def __init__(self, period: float, spread: float, noise: Noise,
                 wander: float = 0.04):
        self.period, self.spread, self.noise, self.wander = period, spread, noise, wander
        self.phase = 0.0
        self._t = 0.0

    def step(self, dt: float) -> float:
        self._t += dt
        rate = (1.0 + self.spread * self.noise(self._t * self.wander)) / self.period
        self.phase += dt * rate
        return self.phase


class Poisson:
    def __init__(self, rng: random.Random, mean_gap: float, first: float | None = None):
        self.rng, self.mean_gap = rng, mean_gap
        self.next_at = rng.expovariate(1.0 / mean_gap) if first is None else first

    def due(self, t: float) -> bool:
        if t < self.next_at:
            return False
        self.next_at = t + self.rng.expovariate(1.0 / self.mean_gap)
        return True


class Follower:
    def __init__(self, tau_up: float, tau_down: float, value: float):
        self.tau_up, self.tau_down, self.value = tau_up, tau_down, value

    def step(self, target: float, dt: float) -> float:
        tau = self.tau_up if target > self.value else self.tau_down
        self.value += (target - self.value) * (1.0 - math.exp(-dt / tau))
        return self.value


def smoothstep(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


def kelvin(t: float) -> tuple[float, float, float]:
    if t >= 6500.0:
        return 1.0, 1.0, 1.0
    t = max(1667.0, t)
    if t <= 4000.0:
        x = -0.2661239e9 / t ** 3 - 0.2343589e6 / t ** 2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t ** 3 + 2.1070379e6 / t ** 2 + 0.2226347e3 / t + 0.240390
    if t <= 2222.0:
        y = -1.1063814 * x ** 3 - 1.34811020 * x ** 2 + 2.18555832 * x - 0.20219683
    elif t <= 4000.0:
        y = -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x ** 3 - 5.87338670 * x ** 2 + 3.75112997 * x - 0.37001483
    X, Y, Z = x / y, 1.0, (1.0 - x - y) / y
    r = 3.2404542 * X - 1.5371385 * Y - 0.4985314 * Z
    g = -0.9692660 * X + 1.8760108 * Y + 0.0415560 * Z
    b = 0.0556434 * X - 0.2040259 * Y + 1.0572252 * Z
    rgb = [max(0.0, c) for c in (r, g, b)]
    peak = max(rgb)
    rgb = [c / peak for c in rgb]
    if t > 5000.0:
        k = (t - 5000.0) / 1500.0
        rgb = [c + (1.0 - c) * k for c in rgb]
    return tuple(rgb)


def mired_lerp(k0: float, k1: float, u: float) -> float:
    m = (1e6 / k0) + ((1e6 / k1) - (1e6 / k0)) * u
    return 1e6 / m


def scaled(rgb, level: float) -> tuple[float, float, float]:
    return tuple(c * level for c in rgb)


def encode_srgb(lin: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(int(round(c)) for c in encode_rgb(lin))


class Scene:
    key = ""
    label = ""
    hint = ""
    tone = "#8a8794"
    fade_in = 0.6
    smooth_tau: float | None = None
    deadband = 2
    calibrated = True
    duration: float | None = None

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        base = self.rng.getrandbits(32)
        self.noise = [Noise(base + 7919 * i) for i in range(4)]
        self._last_t: float | None = None
        self.setup()

    def setup(self) -> None:
        pass

    def sample(self, t: float) -> tuple[float, float, float]:
        dt = 0.0 if self._last_t is None else max(0.0, min(0.5, t - self._last_t))
        self._last_t = t
        return self.render(t, dt)

    def render(self, t: float, dt: float) -> tuple[float, float, float]:
        raise NotImplementedError

    def encode(self, lin, profile: OutputProfile, brightness: float):
        if self.calibrated:
            return profile.apply(lin, brightness, 1.0, 0.0)
        return profile.scale_codes(encode_srgb(lin), brightness)


class Fire(Scene):
    key, label, hint, tone = "камин", "Камин", "отсвет огня", "#ff7a2e"
    fade_in = 1.2
    deadband = 2

    def setup(self) -> None:
        self.flares = Poisson(self.rng, 18.0, first=self.rng.uniform(4.0, 10.0))
        self.flare: tuple[float, float, float] | None = None
        self.level = Follower(0.07, 0.30, 0.6)
        self.flare_count = 0

    def _flare(self, t: float) -> float:
        if self.flares.due(t):
            self.flare = (t, self.rng.uniform(0.20, 0.45), self.rng.uniform(1.0, 1.8))
            self.flare_count += 1
        if self.flare is None:
            return 0.0
        t0, s, decay = self.flare
        a = 0.18
        if t < t0 + a:
            return s * (t - t0) / a
        v = s * math.exp(-(t - t0 - a) / decay)
        if v < 0.005:
            self.flare = None
        return v

    def render(self, t, dt):
        raw = (0.60 + 0.17 * self.noise[0].fbm(t * 0.22, 3)
               + 0.10 * self.noise[1].fbm(t * 2.0, 2) + self._flare(t))
        lvl = min(1.0, max(0.2, self.level.step(raw, dt) if dt else raw))
        k = 1650.0 + 900.0 * (lvl - 0.2) / 0.8
        return scaled(kelvin(k), lvl)


class Candle(Scene):
    key, label, hint, tone = "свеча", "Свеча", "тёплое пламя", "#ffb45e"
    fade_in = 1.2
    deadband = 2

    def setup(self) -> None:
        self.gusts = Poisson(self.rng, 75.0, first=self.rng.uniform(15.0, 40.0))
        self.gust: tuple[float, float, float] | None = None
        self.level = Follower(0.06, 0.12, 0.8)
        self.gust_count = 0

    def _gust(self, t: float) -> float:
        if self.gusts.due(t):
            self.gust = (t, self.rng.uniform(0.25, 0.40), self.rng.uniform(0.6, 1.1))
            self.gust_count += 1
        if self.gust is None:
            return 0.0
        t0, depth, length = self.gust
        u = (t - t0) / length
        if u >= 1.0:
            self.gust = None
            return 0.0
        return depth * math.sin(math.pi * u) ** 2 * (1.0 + 0.3 * math.sin(6 * math.pi * u))

    def render(self, t, dt):
        raw = (0.80 + 0.06 * self.noise[0].fbm(t * 0.18, 2)
               + 0.06 * self.noise[1].fbm(t * 2.6, 2) - self._gust(t))
        lvl = min(1.0, max(0.3, self.level.step(raw, dt) if dt else raw))
        return scaled(kelvin(1700.0 + 350.0 * lvl), lvl)


class Storm(Scene):
    key, label, hint, tone = "гроза", "Гроза", "молнии", "#7d8cff"
    fade_in = 1.5
    BASE = (0.30, 0.40, 1.0)
    FLASH = (0.85, 0.90, 1.0)

    def setup(self) -> None:
        self.strikes = Poisson(self.rng, 22.0, first=self.rng.uniform(3.0, 8.0))
        self.distant = Poisson(self.rng, 9.0, first=self.rng.uniform(1.0, 4.0))
        self.strokes: list[tuple[float, float, float]] = []
        self.glow: tuple[float, float] | None = None
        self.strike_count = 0

    def _plan(self, t0: float, strong: bool) -> None:
        n = self.rng.randint(2, 4) if strong else 1
        t = t0
        for k in range(n):
            dur = self.rng.uniform(0.10, 0.16)
            s = (1.0 if k == 0 else self.rng.uniform(0.45, 0.85)) if strong \
                else self.rng.uniform(0.12, 0.28)
            self.strokes.append((t, dur, s))
            t += dur + self.rng.uniform(0.08, 0.22)
        last_start, last_dur, last_s = self.strokes[-1]
        self.glow = (last_start + last_dur, 0.25 * last_s)

    def flash(self, t: float) -> float:
        if self.strikes.due(t):
            self._plan(t, True)
            self.strike_count += 1
        elif self.distant.due(t):
            self._plan(t, False)
        for start, dur, s in self.strokes:
            if start <= t < start + dur:
                return s
        self.strokes = [x for x in self.strokes if x[0] + x[1] > t]
        if self.glow is not None and not self.strokes and t >= self.glow[0]:
            v = self.glow[1] * math.exp(-(t - self.glow[0]) / 0.35)
            if v < 0.004:
                self.glow = None
            return v
        return 0.0

    def render(self, t, dt):
        level = 0.10 * (1.0 + 0.25 * self.noise[0].fbm(t * 0.05, 2))
        f = min(1.0, self.flash(t))
        base = scaled(self.BASE, level)
        return tuple(b + (fl - b) * f for b, fl in zip(base, self.FLASH))


class Sleep(Scene):
    key, label, hint, tone = "засыпание", "Засыпание", "закат → выкл", "#ff9a5a"
    fade_in = 2.0
    deadband = 1
    duration = 30 * 60.0
    KEYS = ((0.00, 3000.0, 1.00), (0.30, 2500.0, 0.70), (0.62, 2000.0, 0.38),
            (0.86, 1750.0, 0.14), (1.00, 1667.0, 0.035))

    def at(self, u: float) -> tuple[float, float]:
        u = min(1.0, max(0.0, u))
        for (u0, k0, l0), (u1, k1, l1) in zip(self.KEYS, self.KEYS[1:]):
            if u <= u1:
                s = smoothstep((u - u0) / (u1 - u0))
                light = l0 ** (1 / 3) + (l1 ** (1 / 3) - l0 ** (1 / 3)) * s
                return mired_lerp(k0, k1, s), light ** 3
        return self.KEYS[-1][1], self.KEYS[-1][2]

    def render(self, t, dt):
        k, lvl = self.at(t / self.duration)
        lvl *= 1.0 + 0.015 * self.noise[0](t * 0.1)
        return scaled(kelvin(k), lvl)


class Light(Scene):
    key, label, hint, tone = "свет", "Свет", "белый, вечером тёплый", "#f1dcb8"
    fade_in = 1.0
    deadband = 1
    DAY_K, EVENING_K = 5000.0, 2700.0
    location: tuple | None = None
    warm_evening = True
    clock = staticmethod(time.localtime)

    def setup(self) -> None:
        self._checked = None
        self.k = self.DAY_K

    def render(self, t, dt):
        if not self.warm_evening:
            self.k = self.DAY_K
        elif self._checked is None or t - self._checked >= 30.0:
            self._checked = t
            lt = self.clock()
            lat, lon = self.location or (None, None)
            w = evening_warmth(lt.tm_hour + lt.tm_min / 60.0,
                               (lt.tm_gmtoff or 0) / 3600.0, lt.tm_yday, lat, lon)
            self.k = mired_lerp(self.DAY_K, self.EVENING_K, w)
        return kelvin(self.k)


SCENE_TYPES: dict[str, type[Scene]] = {
    cls.key: cls for cls in (Light, Fire, Candle, Storm, Sleep)}


def make_scene(name: str, seed: int | None = None, **overrides) -> Scene:
    if name not in SCENE_TYPES:
        raise ValueError("неизвестный режим: %s (доступны: %s)"
                         % (name, ", ".join(SCENE_TYPES)))
    scene = SCENE_TYPES[name](seed)
    for key, value in overrides.items():
        setattr(scene, key, value)
    return scene


def sun_times(lat: float, lon: float, day_of_year: int) -> tuple[float, float] | None:
    g = 2.0 * math.pi / 365.0 * (day_of_year - 1)
    eq = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                   - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_h = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
             - math.tan(phi) * math.tan(decl))
    if not -1.0 <= cos_h <= 1.0:
        return None
    h = math.degrees(math.acos(cos_h))
    sunrise = (720 - 4 * (lon + h) - eq) / 60.0
    sunset = (720 - 4 * (lon - h) - eq) / 60.0
    return sunrise % 24.0, sunset % 24.0


def evening_warmth(local_hour: float, utc_offset_h: float, day_of_year: int,
                   lat: float | None = None, lon: float | None = None) -> float:
    rise_h, set_h = 7.0, 21.0
    if lat is not None and lon is not None:
        st = sun_times(lat, lon, day_of_year)
        if st is not None:
            rise_h = (st[0] + utc_offset_h) % 24.0
            set_h = (st[1] + utc_offset_h) % 24.0
    h = local_hour
    evening = smoothstep((h - (set_h - 0.5)) / 2.0)
    morning = 1.0 - smoothstep((h - (rise_h - 1.0)) / 2.0)
    return max(evening, morning) if h < 12.0 else evening

