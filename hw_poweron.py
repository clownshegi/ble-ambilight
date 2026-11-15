from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed, CMD_OFF, power_on_command, rgb_command

GREEN = (0, 200, 60)
ORANGE = (255, 110, 0)


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
    print("Подключено.\n")
    hold = args.hold

    try:
        print("=" * 66)
        print("ВАРИАНТ А - короткая команда включения, потом цвет")
        print("=" * 66)
        await led.send(CMD_OFF)
        await asyncio.sleep(1.0)
        print("  %s   выключаю" % CMD_OFF.hex(" ").upper())
        await asyncio.sleep(hold)
        await led.send(power_on_command())
        print("  %s   включаю (короткая)" % power_on_command().hex(" ").upper())
        await asyncio.sleep(0.3)
        await led.send(rgb_command(*GREEN))
        print("  %s   зелёный" % rgb_command(*GREEN).hex(" ").upper())
        await asyncio.sleep(hold * 2)
        a_lit = ask("\n  Лента ЗАГОРЕЛАСЬ зелёным? [y/n]: ").startswith("y")

        print("\n" + "=" * 66)
        print("ВАРИАНТ Б - включение с цветом внутри (девятибайтовое)")
        print("=" * 66)
        await led.send(CMD_OFF)
        await asyncio.sleep(1.0)
        print("  %s   выключаю" % CMD_OFF.hex(" ").upper())
        await asyncio.sleep(hold)
        combined = power_on_command(ORANGE)
        await led.send(combined)
        print("  %s   включаю сразу оранжевым" % combined.hex(" ").upper())
        await asyncio.sleep(0.3)
        await led.send(rgb_command(*ORANGE))
        print("  %s   оранжевый" % rgb_command(*ORANGE).hex(" ").upper())
        await asyncio.sleep(hold * 2)
        b_lit = ask("\n  Лента ЗАГОРЕЛАСЬ оранжевым? [y/n]: ").startswith("y")

        print("\n" + "=" * 66)
        if a_lit and not b_lit:
            print("НАЙДЕНО. Девятибайтовая форма включения не работает:")
            print("лента остаётся выключенной, и команды цвета не действуют.")
            print("Именно это и было причиной. В bjled.py она уже отключена,")
            print("ambilight.py теперь включает короткой командой.")
        elif a_lit and b_lit:
            print("Обе формы включения работают - причина не в них.")
            print("Значит дело в уровне: гоните ярче клавишами '.' и ']'.")
        elif not a_lit and not b_lit:
            print("Не горит ни от одной формы. Тогда проблема глубже:")
            print("  - проверьте питание ленты и что ИК-пультом она не выключена")
            print("  - попробуйте python hw_color.py %s" % args.address)
            print("    он гоняет по ярким цветам на полной мощности")
        else:
            print("Неожиданно: работает только девятибайтовая форма.")
            print("Тогда запускайте ambilight.py как есть и скажите мне.")
        print("=" * 66)
    finally:
        await led.send(CMD_OFF)
        await led.disconnect()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Какая команда включения работает")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--hold", type=float, default=2.0,
                   help="пауза между шагами, секунд")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

