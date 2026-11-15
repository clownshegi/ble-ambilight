from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed, FULL_BRIGHTNESS, rgb_command
from color import split_ww

PAIRS = [
    ("тёмная сцена", (47, 26, 8)),
    ("тусклый бирюзовый", (18, 44, 38)),
    ("тусклый сиреневый", (38, 12, 50)),
    ("средняя яркость", (128, 60, 0)),
    ("почти белый", (128, 128, 84)),
]


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


async def amain(args: argparse.Namespace) -> int:
    led = BJLed(args.address)
    print("Подключаюсь к %s..." % args.address)
    if not await led.connect():
        print("Не подключился. Выключен ли Bluetooth на телефоне?")
        return 1
    await led.turn_on()
    print("Подключено.\n")

    try:
        print("=" * 68)
        print("Каждый цвет показывается двумя способами по очереди.")
        print("=" * 68)
        print("  A - как сейчас: затемнение масштабированием самих каналов")
        print("  B - через четвёртый байт: каналы на полной шкале, яркость в W")
        print()
        print("Если контроллер просто перемножает, это ОДИН И ТОТ ЖЕ цвет и")
        print("разницы быть не должно. Смотрите на ленту.\n")

        for name, codes in PAIRS:
            r, g, b, w = split_ww(codes)
            print("  %s" % name)
            print("     A  %s" % rgb_command(*codes).hex(" ").upper())
            print("     B  %s" % rgb_command(r, g, b, w).hex(" ").upper())
            for _ in range(args.cycles):
                await led.set_rgb(*codes)
                await asyncio.sleep(args.hold)
                await led.set_rgb(r, g, b, w)
                await asyncio.sleep(args.hold)
            print()

        print("=" * 68)
        if ask("Мигало или менялся цвет при переключении A <-> B? [y/n]: ").startswith("y"):
            print("\n  Модель НЕ подтверждена: контроллер считает как-то иначе.")
            print("  Режим --ww включать нельзя, оставляем как есть.")
        else:
            print("\n  Модель подтверждена. Можно запускать с полным")
            print("  разрешением оттенка:")
            print("     python ambilight.py %s --preset film --ww" % args.address)
            print("\n  На тёмных сценах пропорция каналов пойдёт числами вроде")
            print("  255:141:43 вместо 47:26:8 - разница в два с половиной бита.")
        print("=" * 68)
    finally:
        await led.set_rgb(255, 255, 255, FULL_BRIGHTNESS)
        await asyncio.sleep(0.2)
        await led.turn_off()
        await led.disconnect()
        print("\nЯркость возвращена на максимум.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Проверка режима --ww")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--hold", type=float, default=1.6,
                   help="сколько держать каждый вариант, секунд")
    p.add_argument("--cycles", type=int, default=2,
                   help="сколько раз переключить туда-обратно")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

