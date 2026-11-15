from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from bjled import BJLed
from color import lerp_oklab, OutputProfile, DEFAULT_PROFILE_PATH
from dynamics import PerceptualSmoother
from scenes import SCENE_TYPES, Scene, make_scene, smoothstep as _smoothstep

log = logging.getLogger("events")

PALETTES = SCENE_TYPES


def mix_codes(a: tuple[int, int, int], b: tuple[int, int, int],
              t: float, profile: OutputProfile | None = None) -> tuple[int, int, int]:
    if profile is None:
        to_light, to_bytes = (lambda c: list(c)), (lambda u: u)
    else:
        to_light, to_bytes = profile.to_light, profile.to_bytes
    la = tuple(v / 255.0 for v in to_light(a))
    lb = tuple(v / 255.0 for v in to_light(b))
    m = [255.0 * min(1.0, max(0.0, c)) for c in lerp_oklab(la, lb, t)]
    return tuple(int(round(min(255.0, max(0.0, v)))) for v in to_bytes(m))


class ScenePlayer:
    def __init__(self, scene: Scene, profile: OutputProfile):
        self.scene = scene
        self.profile = profile
        self.smoother = PerceptualSmoother(autospeed=0.0, cut_distance=10.0)
        if scene.smooth_tau:
            self.smoother.tau_up = self.smoother.tau_down = scene.smooth_tau
        self.smoother.reset(scene.sample(0.0))
        self.last_sent: tuple[int, int, int] | None = None

    def frame(self, t: float, dt: float, brightness: float):
        lin = self.scene.sample(t)
        if self.scene.smooth_tau:
            lin = self.smoother.step(lin, dt)[0]
        codes = self.scene.encode(lin, self.profile, brightness)
        if self.last_sent is not None and max(
                abs(c - l) for c, l in zip(codes, self.last_sent)) < self.scene.deadband:
            return None
        return codes


async def run_event(address: str, name: str, stop: asyncio.Event,
                    fps: float = 15.0, led: BJLed | None = None,
                    on_frame=None, brightness: float = 1.0) -> None:
    player = ScenePlayer(make_scene(name), OutputProfile.load(DEFAULT_PROFILE_PATH))
    period = 1.0 / max(1.0, fps)
    owns_connection = led is None
    if owns_connection:
        led = BJLed(address)
        if not await led.connect():
            raise ConnectionError("не подключился к %s" % address)
        await led.turn_on()
    try:
        started = last = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            dt, last = max(1e-3, now - last), now
            t = now - started
            codes = player.frame(t, dt, brightness)
            if codes is not None and await led.set_rgb(*codes):
                player.last_sent = codes
            if on_frame is not None:
                on_frame(t, player.last_sent)
            if player.scene.duration and t >= player.scene.duration:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass
    finally:
        if owns_connection:
            await led.disconnect()


class EventPlayer:
    FADE_S = 0.4
    FADE_OUT_S = 0.3
    FADE_OUT_FLOOR = 12

    def __init__(self, address: str, fps: float = 15.0, keepalive: float = 20.0,
                 connect_timeout: float = 45.0, min_retry: float = 2.0,
                 max_retry: float = 30.0):
        self.address = address
        self.fps = fps
        self.keepalive = keepalive
        self.connect_timeout = connect_timeout
        self.min_retry = min_retry
        self.max_retry = max_retry
        self.led = BJLed(address)
        self.name: str | None = None
        self.brightness = 1.0
        self.power: bool | None = None
        self.epoch = 0
        self.link_up = False
        self._task: asyncio.Task | None = None
        self._started = 0.0
        self.scene_player: ScenePlayer | None = None
        self.sleep_minutes = 30.0
        self.location: tuple | None = None
        self.warm_evening = True
        self.on_scene_end = None
        self._scene_done = False
        self._retry_at = 0.0
        self._retry_delay = min_retry
        self._last_keepalive_try = 0.0
        self._was_connected = False
        self.reconnects = 0
        self.profile = OutputProfile.load(DEFAULT_PROFILE_PATH)
        self._fade: tuple[tuple[int, int, int], float, float] | None = None
        self._wanted: tuple[int, int, int] | None = None

    @property
    def connected(self) -> bool:
        return self.led.is_connected

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    def kick(self) -> None:
        if not self.led.is_connected:
            self._retry_at = 0.0
            self._retry_delay = self.min_retry

    def begin_fade(self, duration: float | None = None) -> None:
        frm = self.led.last_rgb
        self._fade = None if frm is None else (
            frm, time.monotonic(), self.FADE_S if duration is None else duration)
        self._wanted = None

    async def put_rgb(self, codes: tuple[int, int, int]) -> bool:
        if self.power is False:
            return False
        self._wanted = codes
        if self._fade is not None:
            return self.led.is_connected
        return await self.led.set_rgb(*codes)

    async def power_on(self) -> None:
        self.power = True
        self.kick()
        if self.led.is_connected:
            await self.led.turn_on()
        self.begin_fade()

    def switch(self, name: str) -> None:
        overrides = {}
        if name == "засыпание":
            overrides["duration"] = self.sleep_minutes * 60.0
        elif name == "свет":
            overrides["location"] = self.location
            overrides["warm_evening"] = self.warm_evening
        scene = make_scene(name, **overrides)
        self.name = name
        self._started = time.monotonic()
        self.scene_player = ScenePlayer(scene, self.profile)
        self._scene_done = False
        self.kick()
        self.begin_fade(scene.fade_in)

    @property
    def scene(self) -> Scene | None:
        return self.scene_player.scene if self.scene_player else None

    def scene_elapsed(self) -> float:
        return time.monotonic() - self._started if self.name else 0.0

    def pause(self) -> None:
        self.name = None
        self.scene_player = None
        self._fade = None

    async def off(self, fade: bool = True, duration: float | None = None) -> bool:
        self.name = None
        self.scene_player = None
        self._fade = None
        was_on = self.power is not False
        self.power = False
        self.kick()
        if not self.led.is_connected:
            return False
        rgb = self.led.last_rgb
        if fade and was_on and rgb is not None and max(rgb) > self.FADE_OUT_FLOOR:
            length = self.FADE_OUT_S if duration is None else duration
            steps = max(2, int(length * self.fps))
            end = self.FADE_OUT_FLOOR / max(rgb)
            for i in range(1, steps + 1):
                k = 1.0 - _smoothstep(i / steps) * (1.0 - end)
                await self.led.set_rgb(*(int(round(c * k)) for c in rgb))
                await asyncio.sleep(length / steps)
        return await self.led.turn_off()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.name = None
        self.link_up = False
        await self.led.disconnect()


    async def _loop(self) -> None:
        period = 1.0 / max(1.0, self.fps)
        last = time.monotonic()
        while True:
            now = time.monotonic()
            dt, last = min(0.5, max(1e-3, now - last)), now
            try:
                self.link_up = self.led.is_connected
                if not self.link_up:
                    if self._was_connected:
                        self._was_connected = False
                        log.warning("BLE: связь с лентой пропала - "
                                    "переподключаюсь в фоне")
                    if now >= self._retry_at:
                        await self._reconnect()
                        last = time.monotonic()
                else:
                    if self.name is not None:
                        await self._frame(now, dt)
                    await self._keepalive(now)
            except Exception as e:
                log.warning("Связь с лентой: итерация не удалась (%s: %s)",
                            type(e).__name__, e)
            try:
                if self._fade is not None and self.led.is_connected:
                    await self._fade_step(time.monotonic())
            except Exception as e:
                self._fade = None
                log.warning("Переход цвета не удался (%s: %s)",
                            type(e).__name__, e)
            await asyncio.sleep(period)

    async def _reconnect(self) -> None:
        try:
            ok = await asyncio.wait_for(self.led.connect(),
                                        timeout=self.connect_timeout)
        except asyncio.TimeoutError:
            log.warning("BLE: подключение не уложилось в %.0f с - "
                        "попробую ещё", self.connect_timeout)
            ok = False
        if not ok:
            self._retry_at = time.monotonic() + self._retry_delay
            self._retry_delay = min(self.max_retry, self._retry_delay * 2)
            return
        self._retry_delay = self.min_retry
        if self.epoch:
            self.reconnects += 1
        self._was_connected = True
        log.info("BLE: связь с лентой есть")
        await self._restore()
        self.link_up = self.led.is_connected

    async def _restore(self) -> None:
        if self.power is True:
            await self.led.turn_on()
            rgb = self.led.last_rgb
            if rgb is not None and self.name is None:
                await asyncio.sleep(0.05)
                await self.led.set_rgb(*rgb)
        elif self.power is False:
            await self.led.turn_off()
        self._fade = None
        if self.scene_player is not None:
            self.scene_player.last_sent = None
        self.epoch += 1

    async def _fade_step(self, now: float) -> None:
        frm, t0, duration = self._fade
        wanted = self._wanted
        if wanted is None:
            if now - t0 > 2 * duration + 2.0:
                self._fade = None
            return
        k = min(1.0, (now - t0) / max(1e-3, duration))
        if k >= 1.0:
            self._fade = None
            await self.led.set_rgb(*wanted)
            return
        await self.led.set_rgb(*mix_codes(frm, wanted, _smoothstep(k), self.profile))

    async def _keepalive(self, now: float) -> None:
        idle = now - max(self.led.last_write_at, self._last_keepalive_try)
        if idle >= self.keepalive:
            self._last_keepalive_try = now
            await self.led.keepalive()

    async def _frame(self, now: float, dt: float) -> None:
        sp = self.scene_player
        if sp is None:
            return
        t = now - self._started
        codes = sp.frame(t, dt, self.brightness)
        if codes is not None:
            send_started = time.monotonic()
            if await self.put_rgb(codes):
                sp.last_sent = codes
            send_ms = (time.monotonic() - send_started) * 1000.0
            if send_ms > 15.0:
                log.warning("Режим %s: запись заняла %.0f мс (обычно <1 мс) - "
                            "стек BLE копит пакеты", self.name, send_ms)
        duration = sp.scene.duration
        if duration and t >= duration and not self._scene_done:
            self._scene_done = True
            if self.on_scene_end is not None:
                self.on_scene_end(self.name)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    p = argparse.ArgumentParser(description="Режимы атмосферы на ленте")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--mode", choices=sorted(PALETTES), required=True)
    p.add_argument("--fps", type=float, default=15.0)
    p.add_argument("--off", action="store_true",
                   help="погасить ленту при выходе (по умолчанию остаётся "
                        "на последнем кадре)")
    args = p.parse_args()

    async def amain() -> int:
        print("Подключаюсь к %s, режим «%s»... Ctrl+C - выход." % (
            args.address, args.mode))
        led = BJLed(args.address)
        if not await led.connect():
            print("Не подключился.")
            return 1
        await led.turn_on()
        stop = asyncio.Event()

        def show(t, rgb):
            sys.stdout.write("\r  t=%6.1fс  RGB %s   " % (t, rgb))
            sys.stdout.flush()

        try:
            await run_event(args.address, args.mode, stop,
                            fps=args.fps, led=led, on_frame=show)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print()
            if args.off:
                await led.turn_off()
            await led.disconnect()
        return 0

    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

