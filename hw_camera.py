from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import urllib.request

import numpy as np

from bjled import BJLed
from color import DEFAULT_PROFILE_PATH, OutputProfile

LEVELS = (12, 24, 48, 96, 160, 255)


def lin(v):
    v = v / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


class Camera:
    def __init__(self, index: int):
        import cv2
        self.cv2, self.index = cv2, index
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        deadline = time.monotonic() + 10
        while self.frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.frame is None:
            raise RuntimeError("камера %d не отдаёт кадры" % index)

    def _loop(self):
        cap = self.cv2.VideoCapture(self.index, self.cv2.CAP_MSMF)
        cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, 720)
        while self.running:
            ok, f = cap.read()
            if ok:
                with self.lock:
                    self.frame = f.astype(np.float32)
        cap.release()

    def grab(self, n: int = 8) -> np.ndarray:
        fs = []
        for _ in range(n):
            with self.lock:
                fs.append(self.frame)
            time.sleep(0.05)
        return np.mean(fs, axis=0)

    def close(self):
        self.running = False


class DroidCam:
    DAYLIGHT, AUTO = 5, 0

    def __init__(self, host: str | None):
        self.host = host

    def wb(self, mode: int) -> bool:
        if not self.host:
            return False
        req = urllib.request.Request("http://%s:4747/v1/camera/wb_mode/%d"
                                     % (self.host, mode), method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except OSError:
            return False


class Rig:
    def __init__(self, led: BJLed, cam: Camera):
        self.led, self.cam = led, cam
        self.dark = None
        self.roi = None

    async def show(self, rgb, wait: float = 1.8) -> np.ndarray:
        await self.led.set_rgb(*rgb)
        await asyncio.sleep(wait)
        await self.led.set_rgb(*rgb)
        await asyncio.sleep(0.3)
        return self.cam.grab()

    async def setup(self) -> None:
        self.dark = await self.show((0, 0, 0), 3.0)
        on = await self.show((160, 60, 60), 2.5)
        d = np.clip(on - self.dark, 0, None).sum(axis=2)
        ys, xs = np.nonzero(d > max(15.0, d.max() * 0.3))
        if len(xs) < 200:
            raise RuntimeError("свет ленты в кадре не виден - наведите камеру")
        pad = 40
        self.roi = (slice(max(0, ys.min() - pad), ys.max() + pad),
                    slice(max(0, xs.min() - pad), xs.max() + pad))
        print("Лента в кадре: x %d-%d, y %d-%d" % (xs.min(), xs.max(), ys.min(), ys.max()))

    def measure(self, frame) -> np.ndarray:
        f = frame[self.roi]
        d = np.clip(lin(f) - lin(self.dark[self.roi]), 0, None)
        w = d.sum(axis=2)
        ok = (f.max(axis=2) < 230) & (w > 0)
        if ok.sum() < 20:
            ok = w > 0
        return d[ok & (w >= np.percentile(w[ok], 95))].mean(axis=0)[::-1]


async def calibrate(rig: Rig, profile: OutputProfile) -> list[list[int]]:
    gb0 = np.array([0.261, 0.245])
    points = []
    for red in LEVELS:
        gb = red * gb0
        best = None
        for _ in range(6):
            codes = (red, int(round(max(1, gb[0]))), int(round(max(1, gb[1]))))
            m = rig.measure(await rig.show(codes))
            ratio = m[1:] / max(1e-9, m[0])
            dev = float(np.abs(ratio - 1).max())
            if best is None or dev < best[0]:
                best = (dev, codes)
            if dev < 0.04:
                break
            gb = np.maximum(1.0, gb * np.clip((1.0 / np.maximum(ratio, 0.05)) ** 0.8, 0.5, 2.0))
        dev, codes = best
        points.append(list(codes))
        print("  красный %3d -> белый при зелёном %3d, синем %3d (отклонение %.0f%%)"
              % (codes[0], codes[1], codes[2], dev * 100))
    return points


async def check(rig: Rig, profile: OutputProfile) -> None:
    import colorsys
    print("Белый через калибровку (идеал - R=G=B):")
    for lvl in (0.05, 0.19, 0.63, 1.0):
        b = profile.apply((lvl,) * 3, 1.0, 1.0, 0.0)
        m = rig.measure(await rig.show(b))
        m = m / m.max()
        print("  уровень %.2f байты %-15s камера R%.2f G%.2f B%.2f" % (lvl, b, *m))
    from scenes import make_scene
    print("Режимы (35%): камера против расчёта, оттенок в градусах")
    for name in ("свет", "камин", "свеча", "засыпание"):
        scene = make_scene(name, seed=1)
        cam, calc = [], []
        for i in range(6):
            b = profile.apply(scene.sample(2.0 + i * 0.4), 0.35, 1.0, 0.0)
            cam.append(rig.measure(await rig.show(b, 0.8)))
            calc.append(np.array(profile.to_light(b)))
        m, c = np.mean(cam, axis=0), np.mean(calc, axis=0)
        hm = colorsys.rgb_to_hsv(*(m / m.max()))[0] * 360
        hc = colorsys.rgb_to_hsv(*(c / c.max()))[0] * 360
        print("  %-10s камера %3.0f°  расчёт %3.0f°" % (name, hm, hc))


async def amain(args) -> int:
    droid = DroidCam(args.droidcam)
    if droid.wb(DroidCam.DAYLIGHT):
        print("Баланс белого камеры зафиксирован (Дневной свет).")
        await asyncio.sleep(1.5)
    elif args.droidcam:
        print("Не удалось зафиксировать баланс DroidCam - замер будет неточным.")
    cam = Camera(args.camera)
    led = BJLed(args.address)
    try:
        for _ in range(4):
            if await led.connect():
                break
            await asyncio.sleep(2)
        if not led.is_connected:
            print("Лента не подключилась. Закройте Lamp.exe и Bluetooth на телефоне.")
            return 1
        await led.turn_on()
        rig = Rig(led, cam)
        await rig.setup()
        profile = OutputProfile.load(args.profile)
        if args.command == "calibrate":
            points = await calibrate(rig, profile)
            with open(args.profile, encoding="utf-8") as f:
                data = json.load(f)
            ref = points[-2]
            data["channel_curve"] = points
            data["white_balance"] = [1.0, round(ref[1] / ref[0], 3), round(ref[2] / ref[0], 3)]
            with open(args.profile, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("Записано в %s. Пересоберите Lamp.exe, чтобы калибровка попала в сборку."
                  % args.profile)
        else:
            await check(rig, profile)
        await led.set_rgb(0, 0, 0)
    finally:
        cam.close()
        await led.disconnect()
        if droid.wb(DroidCam.AUTO):
            print("Баланс белого камеры - снова автоматический.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Калибровка цвета ленты камерой")
    p.add_argument("address")
    p.add_argument("command", choices=("calibrate", "check"))
    p.add_argument("--camera", type=int, default=0, help="номер камеры (DroidCam - 0)")
    p.add_argument("--droidcam", default="192.168.0.2",
                   help="адрес телефона с DroidCam, чтобы зафиксировать баланс белого "
                        "('' - не трогать)")
    p.add_argument("--profile", default=DEFAULT_PROFILE_PATH)
    try:
        return asyncio.run(amain(p.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

