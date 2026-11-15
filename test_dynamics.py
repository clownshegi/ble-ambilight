from __future__ import annotations

import math
import random

from dynamics import FlashGate, IdleDrift, AutoContrast, MotionActivity, DominantHold
from color import linear_to_oklab, oklab_to_linear, oklch_to_oklab

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, detail))
        FAILURES.append(name)


def test_flash_baseline_never_freezes() -> None:
    g = FlashGate(ratio=2.2, floor=0.02, baseline_tau=0.6, release_ratio=1.3)
    g.update((0.05, 0.05, 0.05), 0.05, 0.0)
    triggers = 0
    now = 0.0
    y_new = 0.05 * 3.0
    for i in range(300):
        now += 0.05
        if g.update((y_new, y_new, y_new), 0.05, now):
            triggers += 1
    check("устойчивый скачок даёт один триггер, не поток",
          triggers == 1, "триггеров: %d" % triggers)
    check("база доехала до новой яркости сцены",
          abs(g.baseline - y_new) < 1e-3, "база=%.4f, ожидалось=%.4f"
          % (g.baseline, y_new))


def test_flash_single_spike_triggers_once() -> None:
    g = FlashGate(ratio=2.2, floor=0.02, baseline_tau=0.6, release_ratio=1.3)
    g.update((0.05, 0.05, 0.05), 0.05, 0.0)
    g.update((0.05, 0.05, 0.05), 0.05, 0.05)
    rose = g.update((0.2, 0.2, 0.2), 0.05, 0.10)
    fell_is_not_new_trigger = not g.update((0.05, 0.05, 0.05), 0.05, 0.15)
    check("спайк+возврат: ровно один фронт на спайке", rose)
    check("возврат к норме не считается новым фронтом", fell_is_not_new_trigger)


def test_flash_hysteresis_no_chatter_at_boundary() -> None:
    g = FlashGate(ratio=2.2, floor=0.02, baseline_tau=0.6, release_ratio=1.3)
    g.update((0.05, 0.05, 0.05), 0.05, 0.0)
    random.seed(1)
    triggers = 0
    now = 0.0
    for i in range(200):
        now += 0.05
        base = g.baseline
        y = base * (2.15 + random.uniform(-0.1, 0.15))
        if g.update((y, y, y), 0.05, now):
            triggers += 1
    check("дрожание на границе даёт <=2 срабатывания за 200 кадров",
          triggers <= 2, "триггеров: %d" % triggers)


def test_flash_hold_survives_real_frame_going_dark() -> None:
    g = FlashGate(ratio=2.2, floor=0.02, baseline_tau=0.6,
                  release_ratio=1.3, hold=0.22)
    now = 0.0
    g.update((0.05, 0.05, 0.05), 0.05, now)
    now += 0.05
    rose = g.update((0.3, 0.3, 0.3), 0.05, now)
    check("вспышка поймана", rose)
    now += 0.05
    g.update((0.05, 0.05, 0.05), 0.05, now)
    held = g.holding(now)
    check("пик всё ещё удерживается сразу после того, как экран погас",
          held == (0.3, 0.3, 0.3), "held=%s" % (held,))
    later = now + 0.30
    check("выдержка отпускает после --flash-hold",
          g.holding(later) is None)


def test_idle_activates_after_dwell_not_before() -> None:
    g = IdleDrift(dwell=2.0, still_distance=0.01, period_min=1.0,
                  period_max=1.5, hue_jitter=0.07, light_jitter=0.08)
    t = (0.2, 0.15, 0.1)
    now = 0.0
    activated_early = False
    for i in range(80):
        now += 0.05
        g.update(t, now)
        if g.active and now < 2.0:
            activated_early = True
    check("не активируется раньше dwell", not activated_early)
    check("активируется после dwell", g.active)


def test_idle_deactivates_instantly_on_real_change() -> None:
    g = IdleDrift(dwell=2.0, still_distance=0.01, period_min=1.0,
                  period_max=1.5)
    t = (0.2, 0.15, 0.1)
    now = 0.0
    for i in range(80):
        now += 0.05
        g.update(t, now)
    check("активировалось перед проверкой деактивации", g.active)
    g.update((0.05, 0.05, 0.6), now + 0.05)
    check("мгновенно деактивировалось на настоящем изменении", not g.active)


def test_idle_jitter_bounded_over_long_run() -> None:
    g = IdleDrift(dwell=1.0, still_distance=0.01, period_min=0.3,
                  period_max=0.5, hue_jitter=0.07, light_jitter=0.08)
    t = (0.2, 0.15, 0.1)
    L0, a0, b0 = linear_to_oklab(*t)
    h0 = math.atan2(b0, a0)
    now = 0.0
    max_dh = 0.0
    max_dl_ratio = 0.0
    for i in range(2000):
        now += 0.05
        g.update(t, now)
        out = g.apply(t)
        L1, a1, b1 = linear_to_oklab(*out)
        h1 = math.atan2(b1, a1)
        dh = abs(((h1 - h0 + math.pi) % (2 * math.pi)) - math.pi)
        max_dh = max(max_dh, dh)
        max_dl_ratio = max(max_dl_ratio, abs(L1 - L0) / max(1e-6, L0))
    check("качание оттенка не превышает hue_jitter с запасом",
          max_dh <= 0.07 + 1e-6, "max=%.4f" % max_dh)
    check("качание светлоты не превышает light_jitter с запасом",
          max_dl_ratio <= 0.08 + 1e-6, "max=%.4f" % max_dl_ratio)


def test_contrast_gain_always_bounded() -> None:
    strength = 0.35
    lo, hi = 1.0 - strength / 2, 1.0 + strength / 2
    random.seed(3)
    ok = True
    worst = None
    for scene_level in (0.01, 0.2, 0.9):
        c = AutoContrast(strength=strength)
        for i in range(500):
            y = max(1e-4, scene_level * (1.0 + random.uniform(-0.4, 0.4)))
            g = c.gain((y, y, y))
            if not (lo - 1e-9 <= g <= hi + 1e-9):
                ok = False
                worst = g
    check("gain остаётся в границах на тёмной/средней/яркой сцене",
          ok, "вышел за границы: %s" % worst)


def test_contrast_corridor_converges_after_scene_change() -> None:
    c = AutoContrast(expand=0.30, contract=0.01, min_span=1.5, strength=0.35)
    for i in range(50):
        c.gain((0.01, 0.01, 0.01))
    gains = [c.gain((0.5, 0.5, 0.5)) for i in range(200)]
    check("коридор сходится к новому уровню (не застревает у края)",
          abs(gains[-1] - 1.0) < 0.05, "gain в конце=%.3f" % gains[-1])


def test_activity_slows_down_on_calm_scene() -> None:
    m = MotionActivity(smoothing=1.0, calm_diff=2.0, busy_diff=15.0,
                       factor_min=0.6, factor_max=1.6)
    f = 1.0
    for i in range(200):
        f = m.factor(0.5, 0.05)
    check("спокойная сцена сходится к factor_max (медленнее)",
          abs(f - 1.6) < 0.01, "factor=%.3f" % f)


def test_activity_speeds_up_on_busy_scene() -> None:
    m = MotionActivity(smoothing=1.0, calm_diff=2.0, busy_diff=15.0,
                       factor_min=0.6, factor_max=1.6)
    f = 1.0
    for i in range(200):
        f = m.factor(20.0, 0.05)
    check("экшн-сцена сходится к factor_min (быстрее)",
          abs(f - 0.6) < 0.01, "factor=%.3f" % f)


def test_activity_factor_bounded_on_extremes() -> None:
    m = MotionActivity(factor_min=0.6, factor_max=1.6)
    values = [m.factor(v, 0.05) for v in ([0, 1000] * 50)]
    check("factor не выходит за границы на экстремальных скачках diff",
          all(0.6 - 1e-9 <= v <= 1.6 + 1e-9 for v in values))


def _hue_color(hue_deg: float, L: float = 0.5, C: float = 0.15):
    return oklab_to_linear(*oklch_to_oklab(L, C, math.radians(hue_deg)))


def test_dominant_hold_suppresses_small_jitter() -> None:
    h = DominantHold(hold_distance=0.06)
    outs = []
    for i in range(20):
        c = _hue_color(58 if i % 2 == 0 else 72)
        out = h.apply(c)
        L, a, b = linear_to_oklab(*out)
        outs.append(round(math.degrees(math.atan2(b, a))) % 360)
    check("мелкое дрожание у границы бина держит один оттенок",
          len(set(outs[2:])) == 1, "выходы: %s" % outs)
    check("большинство кадров подавлено", h.suppressed >= 15,
          "подавлено: %d/20" % h.suppressed)


def test_dominant_hold_passes_real_change() -> None:
    h = DominantHold(hold_distance=0.06)
    h.apply(_hue_color(60))
    out = h.apply(_hue_color(120))
    L, a, b = linear_to_oklab(*out)
    hue_out = math.degrees(math.atan2(b, a)) % 360
    check("скачок на 60 градусов проходит без задержки",
          abs(hue_out - 120) < 1.0, "вышло %.1f, ожидалось 120" % hue_out)


TESTS = [
    test_flash_baseline_never_freezes,
    test_flash_single_spike_triggers_once,
    test_flash_hysteresis_no_chatter_at_boundary,
    test_flash_hold_survives_real_frame_going_dark,
    test_idle_activates_after_dwell_not_before,
    test_idle_deactivates_instantly_on_real_change,
    test_idle_jitter_bounded_over_long_run,
    test_contrast_gain_always_bounded,
    test_contrast_corridor_converges_after_scene_change,
    test_activity_slows_down_on_calm_scene,
    test_activity_speeds_up_on_busy_scene,
    test_activity_factor_bounded_on_extremes,
    test_dominant_hold_suppresses_small_jitter,
    test_dominant_hold_passes_real_change,
]


def main() -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for t in TESTS:
        print("%s:" % t.__name__)
        t()
    print()
    if FAILURES:
        print("ПРОВАЛЕНО: %d" % len(FAILURES))
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    print("Все проверки прошли (%d)." % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

