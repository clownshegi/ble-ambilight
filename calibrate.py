from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed
from color import OutputProfile, DEFAULT_PROFILE_PATH

try:
    import msvcrt
except ImportError:
    msvcrt = None


def getkey() -> str | None:
    if msvcrt is None or not msvcrt.kbhit():
        return None
    ch = msvcrt.getch()
    if ch in (b"\x00", b"\xe0"):
        msvcrt.getch()
        return None
    try:
        return ch.decode("ascii", "ignore")
    except Exception:
        return None


def bar(value: float, width: int = 24) -> str:
    filled = int(round(value * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class Step:
    def __init__(self, led: BJLed, profile: OutputProfile):
        self.led = led
        self.profile = profile
        self.dirty = True

    async def hold(self, rgb: tuple[int, int, int]) -> None:
        await self.led.set_rgb(*rgb)

    async def loop(self, render, handle) -> bool:
        tick = 0
        while True:
            if self.dirty:
                sys.stdout.write("\r" + render() + "   ")
                sys.stdout.flush()
                self.dirty = False
            key = getkey()
            if key is not None:
                result = handle(key)
                if result == "ok":
                    print()
                    return True
                if result == "quit":
                    print("\nПрервано.")
                    return False
                self.dirty = True
            await asyncio.sleep(0.04)
            tick += 1
            if tick % 50 == 0:
                await self.resend()

    async def resend(self) -> None:
        pass


async def step_white_balance(led: BJLed, profile: OutputProfile) -> bool:
    print("\n" + "=" * 68)
    print("ШАГ 1 из 3 - БАЛАНС БЕЛОГО")
    print("=" * 68)
    print("Лента сейчас пытается светить белым.")
    print("Смотрите на неё и убирайте лишний оттенок.")
    print()
    print("  r / R   меньше / больше красного")
    print("  g / G   меньше / больше зелёного")
    print("  b / B   меньше / больше синего")
    print("  0       сбросить")
    print("  Enter   принять и дальше")
    print("  q       выйти без сохранения")
    print()
    print("Ориентир: сравните с листом белой бумаги под обычной лампой,")
    print("или с белым окном на мониторе. Добивайтесь, чтобы не было")
    print("ни синевы, ни зелени, ни розовизны.")
    print()

    gains = list(profile.white_balance)
    step = Step(led, profile)

    async def push():
        rgb = profile.__class__(
            white_balance=tuple(gains), led_gamma=profile.led_gamma,
            floor=0, max_level=1.0).apply((1.0, 1.0, 1.0))
        await step.hold(rgb)
        return rgb

    current = await push()
    pending = {"rgb": current}

    def render():
        r, g, b = pending["rgb"]
        return ("  К %.2f  З %.2f  С %.2f   ->  в ленту (%3d,%3d,%3d)"
                % (gains[0], gains[1], gains[2], r, g, b))

    def handle(key):
        d = 0.02
        if key == "r":
            gains[0] = max(0.2, gains[0] - d)
        elif key == "R":
            gains[0] = min(1.0, gains[0] + d)
        elif key == "g":
            gains[1] = max(0.2, gains[1] - d)
        elif key == "G":
            gains[1] = min(1.0, gains[1] + d)
        elif key == "b":
            gains[2] = max(0.2, gains[2] - d)
        elif key == "B":
            gains[2] = min(1.0, gains[2] + d)
        elif key == "0":
            gains[:] = [1.0, 1.0, 1.0]
        elif key in ("\r", "\n"):
            return "ok"
        elif key == "q":
            return "quit"
        else:
            return None
        return "changed"

    async def pump():
        while True:
            pending["rgb"] = await push()
            await asyncio.sleep(0.08)

    task = asyncio.ensure_future(pump())
    try:
        ok = await step.loop(render, handle)
    finally:
        task.cancel()
    if ok:
        top = max(gains)
        if top > 0:
            gains[:] = [g / top for g in gains]
        profile.white_balance = tuple(round(g, 3) for g in gains)
        print("  Принято: К %.2f  З %.2f  С %.2f" % profile.white_balance)
    return ok


async def step_floor(led: BJLed, profile: OutputProfile) -> bool:
    print("\n" + "=" * 68)
    print("ШАГ 2 из 3 - НИЖНИЙ ПОРОГ")
    print("=" * 68)
    print("Лента светит очень тускло белым. Опускайте уровень, пока свечение")
    print("не станет рваным: замерцает, задёргается или пропадёт скачком.")
    print("Примите значение на одну ступень ВЫШЕ того, где стало плохо.")
    print()
    print("  - / +   ниже / выше")
    print("  Enter   принять")
    print("  q       выйти без сохранения")
    print()

    level = [max(1, profile.floor if profile.floor > 1 else 12)]
    step = Step(led, profile)

    def render():
        return "  уровень %3d из 255  %s" % (level[0], bar(level[0] / 40.0))

    def handle(key):
        if key == "-":
            level[0] = max(1, level[0] - 1)
        elif key in ("+", "="):
            level[0] = min(60, level[0] + 1)
        elif key in ("\r", "\n"):
            return "ok"
        elif key == "q":
            return "quit"
        else:
            return None
        return "changed"

    async def pump():
        while True:
            v = level[0]
            await led.set_rgb(v, v, v)
            await asyncio.sleep(0.1)

    task = asyncio.ensure_future(pump())
    try:
        ok = await step.loop(render, handle)
    finally:
        task.cancel()
    if ok:
        profile.floor = level[0]
        print("  Принято: порог %d" % profile.floor)
    return ok


async def step_max(led: BJLed, profile: OutputProfile) -> bool:
    print("\n" + "=" * 68)
    print("ШАГ 3 из 3 - КОМФОРТНЫЙ МАКСИМУМ")
    print("=" * 68)
    print("Лента светит белым на выбранной доле мощности. Поставьте столько,")
    print("сколько приятно в этой комнате при обычном освещении - на ярких")
    print("сценах лента будет выходить именно на этот уровень.")
    print()
    print("  - / +   тусклее / ярче")
    print("  Enter   принять")
    print("  q       выйти без сохранения")
    print()

    level = [profile.max_level]
    step = Step(led, profile)

    def render():
        return "  максимум %3d%%  %s" % (round(level[0] * 100), bar(level[0]))

    def handle(key):
        if key == "-":
            level[0] = max(0.05, level[0] - 0.05)
        elif key in ("+", "="):
            level[0] = min(1.0, level[0] + 0.05)
        elif key in ("\r", "\n"):
            return "ok"
        elif key == "q":
            return "quit"
        else:
            return None
        return "changed"

    async def pump():
        while True:
            p = OutputProfile(white_balance=profile.white_balance,
                              led_gamma=profile.led_gamma, floor=profile.floor,
                              max_level=level[0])
            await led.set_rgb(*p.apply((1.0, 1.0, 1.0)))
            await asyncio.sleep(0.1)

    task = asyncio.ensure_future(pump())
    try:
        ok = await step.loop(render, handle)
    finally:
        task.cancel()
    if ok:
        profile.max_level = round(level[0], 3)
        print("  Принято: максимум %d%%" % round(profile.max_level * 100))
    return ok


async def amain(args: argparse.Namespace) -> int:
    if msvcrt is None:
        print("Калибровка рассчитана на Windows: нужен msvcrt для чтения клавиш.")
        return 1

    profile = OutputProfile.load(args.profile)
    led = BJLed(args.address)
    print("Подключаюсь к %s..." % args.address)
    if not await led.connect():
        print("Не подключился. Выключен ли Bluetooth на телефоне?")
        return 1
    await led.turn_on()
    print("Подключено.")

    try:
        for step in (step_white_balance, step_floor, step_max):
            if not await step(led, profile):
                return 1
    finally:
        await led.turn_off()
        await led.disconnect()

    profile.save(args.profile)
    print("\n" + "=" * 68)
    print("Профиль сохранён в %s" % args.profile)
    print("  баланс белого : К %.2f  З %.2f  С %.2f" % profile.white_balance)
    print("  нижний порог  : %d" % profile.floor)
    print("  максимум      : %d%%" % round(profile.max_level * 100))
    print("=" * 68)
    print("\nambilight.py подхватит его автоматически.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Калибровка ленты по глазу")
    p.add_argument("address", nargs="?", help="MAC-адрес ленты")
    p.add_argument("--profile", default=DEFAULT_PROFILE_PATH,
                   help="файл профиля")
    p.add_argument("--show", action="store_true", help="показать профиль")
    p.add_argument("--reset", action="store_true", help="сбросить профиль")
    args = p.parse_args()

    if args.show:
        pr = OutputProfile.load(args.profile)
        print("Профиль %s:" % args.profile)
        print("  баланс белого : К %.2f  З %.2f  С %.2f" % pr.white_balance)
        print("  гамма ленты   : %.2f" % pr.led_gamma)
        print("  нижний порог  : %d" % pr.floor)
        print("  максимум      : %d%%" % round(pr.max_level * 100))
        return 0
    if args.reset:
        OutputProfile().save(args.profile)
        print("Профиль сброшен: %s" % args.profile)
        return 0
    if not args.address:
        p.error("укажи MAC-адрес ленты")

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

