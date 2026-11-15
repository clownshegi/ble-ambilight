from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed

WARM = (1.0, 0.415, 0.065)


async def ramp(led: BJLed, top: int, bottom: int, step: int, seconds: float) -> None:
    levels = list(range(top, bottom - 1, -step))
    pause = seconds / max(1, len(levels))
    for level in levels:
        rgb = tuple(int(round(level * c)) for c in WARM)
        await led.set_rgb(*rgb)
        sys.stdout.write("\r  байт %3d   RGB %-15s " % (level, rgb))
        sys.stdout.flush()
        await asyncio.sleep(pause)
    print()


async def amain(args) -> int:
    led = BJLed(args.address)
    print("Подключаюсь к %s..." % args.address)
    if not await led.connect():
        print("Не подключился. Выключите Bluetooth на телефоне и закройте Lamp.exe.")
        return 1
    await led.turn_on()
    try:
        for step in (1, 2):
            print("\nСпуск 64 -> %d шагом %d байт за %.0f с. Смотрите и на "
                  "сами диоды, и на потолок." % (args.floor, step, args.seconds))
            await led.set_rgb(*(int(round(64 * c)) for c in WARM))
            await asyncio.sleep(2.0)
            await ramp(led, 64, args.floor, step, args.seconds)
            await asyncio.sleep(1.0)
        print("\nЗапишите: с какого байта спуск стал заметно ступенчатым - "
              "для шага 1 и для шага 2.")
    finally:
        await led.set_rgb(*(int(round(128 * c)) for c in WARM))
        await led.disconnect()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Видимость ступенек яркости")
    p.add_argument("address")
    p.add_argument("--seconds", type=float, default=30.0,
                   help="длительность одного спуска")
    p.add_argument("--floor", type=int, default=12,
                   help="до какого байта спускаться (порог из калибровки)")
    try:
        return asyncio.run(amain(p.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

