from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed, CMD_ON, CMD_OFF, rgb_command

STEPS = [
    ("красный", (255, 0, 0)),
    ("зелёный", (0, 255, 0)),
    ("синий", (0, 0, 255)),
    ("жёлтый", (255, 200, 0)),
    ("голубой", (0, 180, 255)),
    ("белый", (255, 255, 255)),
    ("тусклый белый (яркость 10%)", (25, 25, 25)),
]


def show(label: str, payload: bytes, ok: bool) -> None:
    status = "ok" if ok else "НЕ ПРОШЛО"
    print("  %-30s %s   %s" % (label, payload.hex(" ").upper(), status))


async def amain(args: argparse.Namespace) -> int:
    led = BJLed(args.address)
    print("Подключаюсь к %s..." % args.address)
    if not await led.connect():
        print("\nНе подключился. Проверь:")
        print("  - выключен ли Bluetooth на телефоне (лента держит одно соединение)")
        print("  - подана ли на ленту питание")
        print("  - не спарена ли она в Параметрах Windows (лучше удалить оттуда)")
        return 1
    print("Подключено.\n")

    try:
        if args.off:
            show("выключение", CMD_OFF, await led.turn_off())
            return 0

        show("включение", CMD_ON, await led.turn_on())
        await asyncio.sleep(args.hold)

        for label, (r, g, b) in STEPS:
            show(label, rgb_command(r, g, b), await led.set_rgb(r, g, b))
            await asyncio.sleep(args.hold)

        print("\nПлавный переход синий -> оранжевый (проверка частоты 10 к/с):")
        start, end, frames = (0, 0, 255), (255, 120, 0), 40
        for i in range(frames + 1):
            t = i / frames
            rgb = tuple(round(s + (e - s) * t) for s, e in zip(start, end))
            await led.set_rgb(*rgb)
            await asyncio.sleep(0.1)
        print("  отправлено %d команд, ошибок %d" % (led.writes_ok, led.writes_failed))

        if not args.keep:
            show("\n  выключение", CMD_OFF, await led.turn_off())
    finally:
        await led.disconnect()

    print("\nЕсли лента отработала все цвета - протокол верный, можно к этапу 3.")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Проверка протокола BJ_LED")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--hold", type=float, default=1.2,
                   help="сколько держать каждый цвет, секунд")
    p.add_argument("--off", action="store_true", help="только выключить и выйти")
    p.add_argument("--keep", action="store_true",
                   help="не выключать ленту в конце")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

