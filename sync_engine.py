from __future__ import annotations

import asyncio
import copy
import logging
import math
import threading
import time

import ambilight as ambilight_mod
from audio import LoudnessMonitor
from color import OutputProfile, DEFAULT_PROFILE_PATH, oklab_distance
from dynamics import SendScheduler
from winevents import input_idle_seconds
from extract import MODES
from pipeline import ScenePipeline

log = logging.getLogger("sync_engine")


def build_args(preset: str, brightness: float | None = None,
               mode: str | None = None, flags: list[str] | None = None,
               extra: dict | None = None):
    if preset not in ambilight_mod.PRESETS:
        raise ValueError("неизвестный пресет: %s" % preset)
    if mode and mode not in MODES:
        raise ValueError("неизвестный режим цвета: %s (доступны: %s)"
                         % (mode, ", ".join(MODES)))
    argv = ["--preset", preset]
    if brightness is not None:
        argv += ["--brightness", str(max(0.05, min(1.0, float(brightness))))]
    if mode:
        argv += ["--mode", mode]
    argv += (flags or [])
    p = ambilight_mod.build_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit:
        raise ValueError("неверные параметры синхронизации: %s"
                         % " ".join(argv)) from None
    ambilight_mod.apply_preset(args)
    for key, value in (extra or {}).items():
        setattr(args, key, value)
    return args


class ScreenSync:
    def __init__(self, link, args, on_failed=None, *,
                 stall_seconds: float = 8.0, retry_max: float = 30.0,
                 healthy_reset: float = 60.0, upgrade_after: float = 120.0,
                 ready_timeout: float = 5.0):
        self.link = link
        self.args = args
        self.on_failed = on_failed
        self.ready_timeout = ready_timeout
        self.stall_seconds = stall_seconds
        self.retry_max = retry_max
        self.healthy_reset = healthy_reset
        self.upgrade_after = upgrade_after
        self._stop = threading.Event()
        self.sampler: ambilight_mod.Sampler | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._zombies: list = []
        self.problem: str | None = None
        self.brightness = args.brightness
        self.mode_choice = ""
        self.dark_gate_on = args.dark_gate
        self.flash_gate_on = args.flash_gate
        self.idle_life_on = args.idle_life
        self.auto_contrast_on = args.auto_contrast
        self.adaptive_tau_on = bool(args.adaptive_tau)
        self.dominant_hold_on = bool(args.dominant_hold)
        self.audio_on = bool(args.audio)
        self.dim_after = 60.0 * float(getattr(args, "dim_when_idle", 0) or 0)
        self.notice: str | None = None

    @property
    def preset_mode(self) -> str:
        return ambilight_mod.PRESETS[self.args.preset]["mode"]

    @property
    def mode(self) -> str:
        s = self.sampler
        return s.mode if s is not None else self.args.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self.args.mode = value
        s = self.sampler
        if s is not None:
            s.mode = value

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        await self._spawn_sampler()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._stop.set()
        s, self.sampler = self.sampler, None
        if s is not None:
            await asyncio.get_running_loop().run_in_executor(None, s.join, 2.0)

    async def _spawn_sampler(self) -> None:
        loop = asyncio.get_running_loop()
        old = self.sampler
        if old is not None:
            self._stop.set()
            await loop.run_in_executor(None, old.join, 3.0)
            if old.is_alive():
                self._zombies.append(old)
        self._zombies = [z for z in self._zombies if z.is_alive()]
        args = self.args
        if self._zombies and args.backend != "mss":
            args = copy.copy(args)
            args.backend = "mss"
            args.capture_fps = min(args.capture_fps, 15.0)
            log.warning("Прежний поток захвата не вышел - новый через mss, "
                        "пока тот не отпустит dxcam")
        self._stop = threading.Event()
        self.sampler = ambilight_mod.Sampler(args, self._stop)
        self.sampler.start()
        await loop.run_in_executor(None, self.sampler.ready.wait,
                                   self.ready_timeout)

    async def _start_audio(self):
        mon = LoudnessMonitor()
        mon.start()
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, mon.ready.wait, 5.0)
        except asyncio.CancelledError:
            mon.stop()
            raise
        if mon.error:
            log.warning("Звук не поднялся (%s), иду без него", mon.error)
            mon.stop()
            return None
        log.info("Звук: %s, %d Гц", mon.device, mon.rate)
        return mon

    async def _loop(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Синхронизация с экраном упала: %s: %s",
                      type(e).__name__, e)
            self.problem = "синхронизация упала: %s" % e
            self._stop.set()
            if self.on_failed is not None:
                self.on_failed()

    AWAY_LEVEL = 0.35

    def _sync_live(self, pipe: ScenePipeline, dim: float = 1.0) -> None:
        pipe.brightness = self.brightness * dim
        pipe.dark_gate_on = self.dark_gate_on
        pipe.flash_gate_on = self.flash_gate_on
        pipe.idle_life_on = self.idle_life_on
        pipe.auto_contrast_on = self.auto_contrast_on
        pipe.adaptive_tau_on = self.adaptive_tau_on
        pipe.dominant_hold_on = self.dominant_hold_on

    async def _run(self) -> None:
        a = self.args
        pipe = ScenePipeline(a, 1, OutputProfile.load(DEFAULT_PROFILE_PATH))
        scheduler = SendScheduler(fps=a.fps, deadband=a.deadband,
                                  keepalive=a.keepalive)
        audio_mon = None
        audio_retry_at = 0.0

        s = self.sampler
        last_target = s.targets[0] if s.frames > 0 else (0.0, 0.0, 0.0)
        pipe.reset([last_target])
        seen_cut = s.cut_seq
        seen_epoch = self.link.epoch
        period = 1.0 / max(0.5, a.fps)
        started = time.monotonic()
        last_tick = started
        last_frames = s.frames
        last_frame_at = started
        spawned_at = started
        healthy_since: float | None = started
        retry_delay = 0.5
        next_restart_at = 0.0
        restarts = 0
        errors_in_row = 0
        dim = 1.0
        still_anchor, still_since = last_target, started
        idle_s, next_idle_check = 0.0, 0.0
        try:
            while not self._stopping:
                tick = time.perf_counter()
                now = time.monotonic()
                try:
                    s = self.sampler
                    if s.frames != last_frames:
                        last_frames, last_frame_at = s.frames, now
                        if self.problem is not None:
                            log.info("Захват экрана снова работает")
                            self.problem = None
                    dead = not s.is_alive()
                    stuck = not dead and now - last_frame_at > self.stall_seconds
                    respawn = False
                    if dead or stuck:
                        healthy_since = None
                        if now >= next_restart_at:
                            restarts += 1
                            self.problem = ("захват экрана %s - перезапускаю "
                                            "(попытка %d)"
                                            % ("завис" if stuck else "не работает",
                                               restarts))
                            log.warning("Поток захвата %s - перезапуск %d, "
                                        "следующий не раньше чем через %.1f с",
                                        "завис" if stuck else "умер",
                                        restarts, retry_delay)
                            next_restart_at = now + retry_delay
                            retry_delay = min(self.retry_max, retry_delay * 2)
                            respawn = True
                    else:
                        if healthy_since is None:
                            healthy_since = now
                        elif now - healthy_since >= self.healthy_reset:
                            retry_delay = 0.5
                        if (s.backend == "mss" and a.backend == "auto"
                                and not any(z.is_alive() for z in self._zombies)
                                and now - spawned_at >= self.upgrade_after):
                            log.info("Захват идёт через mss - пробую вернуться "
                                     "на dxcam")
                            respawn = True
                    if respawn:
                        await self._spawn_sampler()
                        s = self.sampler
                        spawned_at = time.monotonic()
                        last_frames, last_frame_at = s.frames, spawned_at
                        seen_cut = s.cut_seq

                    if audio_mon is not None and not audio_mon.is_alive():
                        log.warning("Звук: %s - перезапускаю",
                                    audio_mon.error or "поток вышел")
                        audio_mon, audio_retry_at = None, 0.0
                    if self.audio_on and audio_mon is None and now >= audio_retry_at:
                        audio_mon = await self._start_audio()
                        if audio_mon is None:
                            audio_retry_at = now + 60.0
                    elif not self.audio_on and audio_mon is not None:
                        audio_mon.stop()
                        audio_mon = None

                    dt = min(0.5, max(1e-3, now - last_tick))
                    last_tick = now
                    force_cut = s.cut_seq != seen_cut
                    seen_cut = s.cut_seq
                    if s.frames > 0:
                        last_target = s.targets[0]
                    s.want_diff = self.adaptive_tau_on

                    away = False
                    if self.dim_after > 0:
                        if oklab_distance(last_target, still_anchor) > 0.02:
                            still_anchor, still_since = last_target, now
                        if now >= next_idle_check:
                            next_idle_check = now + 1.0
                            idle_s = input_idle_seconds()
                        away = (idle_s >= self.dim_after
                                and now - still_since >= self.dim_after)
                    want = self.AWAY_LEVEL if away else 1.0
                    tau = 4.0 if want < dim else 0.25
                    dim += (want - dim) * (1.0 - math.exp(-dt / tau))
                    self.notice = ("никого нет %d мин - приглушено"
                                   % (idle_s // 60)) if away else None
                    self._sync_live(pipe, dim)
                    new_codes, cuts, _fired = pipe.step(
                        [last_target], now, dt, force_cut, mode=self.mode,
                        frame_diff=s.last_diff,
                        audio_level=None if audio_mon is None else audio_mon.level)
                    codes, is_cut = new_codes[0], cuts[0]
                    if self.link.epoch != seen_epoch:
                        seen_epoch = self.link.epoch
                        scheduler.last_sent = None
                    if scheduler.decide(codes, now, is_cut):
                        if await self.link.put_rgb(codes):
                            scheduler.mark_sent(codes, now, is_cut)
                    errors_in_row = 0
                except Exception as e:
                    errors_in_row += 1
                    if errors_in_row in (1, 10) or errors_in_row % 100 == 0:
                        log.warning("Синхронизация: кадр не обработан "
                                    "(%s: %s), подряд %d",
                                    type(e).__name__, e, errors_in_row)
                    if errors_in_row >= 200:
                        raise
                await asyncio.sleep(max(0.0, period - (time.perf_counter() - tick)))
        finally:
            if audio_mon is not None:
                audio_mon.stop()

