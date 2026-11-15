from __future__ import annotations

from audio import modulate
from color import OutputProfile
from dynamics import (PerceptualSmoother, DarkGate, FlashGate, IdleDrift,
                      AutoContrast, MotionActivity, DominantHold, luminance)


class ScenePipeline:
    def __init__(self, args, n_zones: int, profile: OutputProfile):
        a = args
        self.args = a
        self.profile = profile
        self.smoothers = [PerceptualSmoother(tau_up=a.tau_up,
                                             tau_down=a.tau_down,
                                             autospeed=a.autospeed,
                                             cut_distance=a.cut_distance)
                          for _ in range(n_zones)]
        self.base_tau = (a.tau_up, a.tau_down)
        self.holds = [DominantHold(hold_distance=a.dominant_hold_distance)
                      for _ in range(n_zones)]
        self.gate = DarkGate()
        self.flash = FlashGate(ratio=a.flash_ratio, floor=a.flash_floor,
                               baseline_tau=a.flash_baseline_tau,
                               release_ratio=a.flash_release_ratio,
                               hold=a.flash_hold)
        self.idle = IdleDrift(dwell=a.idle_dwell,
                              still_distance=a.idle_still_distance,
                              period_min=a.idle_period_min,
                              period_max=a.idle_period_max,
                              hue_jitter=a.idle_hue_jitter,
                              light_jitter=a.idle_light_jitter)
        self.contrast = AutoContrast(expand=a.contrast_expand,
                                     contract=a.contrast_contract,
                                     min_span=a.contrast_min_span,
                                     strength=a.contrast_strength)
        self.activity = MotionActivity(smoothing=a.activity_smoothing,
                                       calm_diff=a.activity_calm,
                                       busy_diff=a.activity_busy,
                                       factor_min=a.tau_factor_min,
                                       factor_max=a.tau_factor_max)
        self.brightness = a.brightness
        self.gamma_out = a.gamma_out
        self.min_level = a.min_level
        self.dark_gate_on = bool(a.dark_gate)
        self.flash_gate_on = bool(a.flash_gate)
        self.idle_life_on = bool(a.idle_life)
        self.auto_contrast_on = bool(a.auto_contrast)
        self.adaptive_tau_on = bool(a.adaptive_tau)
        self.dominant_hold_on = bool(a.dominant_hold)
        self.dark = False
        self.holding = False
        self.contrast_gain = 1.0
        self.tau_factor = 1.0
        self.flashes = 0
        self.flash_ratio = 0.0

    def reset(self, targets) -> None:
        for sm, t in zip(self.smoothers, targets):
            sm.reset(t)

    @property
    def suppressed_hue_switches(self) -> int:
        return sum(h.suppressed for h in self.holds)

    def step(self, targets, now: float, dt: float, force_cut: bool = False,
             mode: str = "", frame_diff: float = 0.0,
             audio_level: float | None = None):
        targets = list(targets)
        if self.dominant_hold_on and mode == "dominant":
            targets = [h.apply(t) for h, t in zip(self.holds, targets)]
        else:
            for h in self.holds:
                h.reset()

        mean = tuple(sum(t[k] for t in targets) / len(targets)
                     for k in range(3))
        dark = self.gate.update(mean, now) if self.dark_gate_on else False
        holding = fired = False
        if self.flash_gate_on:
            base_before = self.flash.baseline
            if self.flash.update(mean, dt, now):
                fired = force_cut = True
                self.flashes += 1
                self.flash_ratio = luminance(mean) / max(1e-6, base_before or 0.0)
            held = self.flash.holding(now)
            if held is not None:
                targets = [held] * len(targets)
                holding = True
        if self.idle_life_on:
            self.idle.update(mean, now)
            if self.idle.active and not holding:
                targets = [self.idle.apply(t) for t in targets]
        gain = self.contrast.gain(mean) if self.auto_contrast_on else 1.0

        bright = self.brightness
        if audio_level is not None:
            bright = modulate(bright, audio_level, self.args.audio_depth)
        bright *= gain

        factor = (self.activity.factor(frame_diff, dt)
                  if self.adaptive_tau_on else 1.0)
        for sm in self.smoothers:
            sm.tau_up = max(0.01, self.base_tau[0] * factor)
            sm.tau_down = max(0.01, self.base_tau[1] * factor)

        codes, cuts = [], []
        for i, sm in enumerate(self.smoothers):
            lin, _dE, _a, is_cut = sm.step(
                targets[i] if i < len(targets) else targets[-1], dt, force_cut)
            codes.append((0, 0, 0) if dark else self.profile.apply(
                lin, bright, self.gamma_out, self.min_level))
            cuts.append(is_cut)

        self.dark, self.holding = dark, holding
        self.contrast_gain, self.tau_factor = gain, factor
        return codes, cuts, fired

