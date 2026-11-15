from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed, CMD_ON, rgb_command, effect_command


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
        await led.send(CMD_ON)
        await asyncio.sleep(0.3)

        print("=" * 66)
        print("ШАГ 1 - белый с яркостью на максимуме")
        print("=" * 66)
        frame = rgb_command(255, 255, 255, 255)
        await led.send(frame)
        print("  %s" % frame.hex(" ").upper())
        print("  (последний байт FF - общая яркость на максимум)")
        await asyncio.sleep(hold)
        if ask("\n  Лента ЗАГОРЕЛАСЬ? [y/n]: ").startswith("y"):
            print("\n  НАЙДЕНО. Четвёртый байт - запоминаемая яркость, и мы")
            print("  оставили её почти в нуле. Теперь она на максимуме.")
            print("  Сейчас проверю, что обычные команды цвета снова работают.")
            for name, c in (("красный", (255, 0, 0)), ("зелёный", (0, 255, 0)),
                            ("синий", (0, 0, 255))):
                await led.send(rgb_command(*c))
                print("     %s  %s" % (rgb_command(*c).hex(" ").upper(), name))
                await asyncio.sleep(1.2)
            if ask("\n  Цвета менялись? [y/n]: ").startswith("y"):
                print("\n  Всё. Лента в рабочем состоянии, можно запускать")
                print("  ambilight.py. Я добавлю установку яркости при старте,")
                print("  чтобы это больше не повторилось.")
            else:
                print("\n  Странно: яркость вернулась, а цвет не меняется.")
            return 0

        print("\n" + "=" * 66)
        print("ШАГ 2 - встроенный эффект")
        print("=" * 66)
        print("Эффект контроллер крутит сам, наш цвет и яркость он не трогает.")
        print("Это отделяет состояние софта от железа.\n")
        eff = effect_command(0x00, 0x03)
        await led.send(eff)
        print("  %s   перелив цветов" % eff.hex(" ").upper())
        await asyncio.sleep(hold * 2)
        effect_lit = ask("\n  Лента что-нибудь показывает? [y/n]: ").startswith("y")
        await led.send(rgb_command(255, 255, 255, 255))

        if not effect_lit:
            print("\n" + "=" * 66)
            print("Не светится даже встроенный эффект.")
            print("=" * 66)
            print("Значит проблема НЕ в наших командах. Контроллер отвечает по")
            print("Bluetooth, то есть питание на него идёт, но света нет.")
            print("Проверьте по порядку:")
            print("  1. Разъём между контроллером и самой лентой - он часто")
            print("     выпадает, и контроллер при этом продолжает работать.")
            print("  2. Блок питания: 5 В на выходе, и хватает ли тока.")
            print("  3. ИК-пульт: нажмите на нём кнопку включения и яркость +.")
            print("  4. Штатное приложение MohuanLED с телефона - если и там")
            print("     темно, это точно железо.")
            print("  5. Обесточьте на 10 секунд и включите заново.")
            return 1

        print("\n" + "=" * 66)
        print("ШАГ 3 - перебор яркости")
        print("=" * 66)
        print("Эффект светится, значит лента жива. Ищем, на какой яркости")
        print("появляется цвет.\n")
        for w in (0xFF, 0xC0, 0x80, 0x40, 0x20, 0x10, 0x08, 0x00):
            f = rgb_command(0, 255, 0, w)
            await led.send(f)
            print("  %s   яркость 0x%02X" % (f.hex(" ").upper(), w))
            await asyncio.sleep(1.0)
        print("\n  Скажите, на каких значениях был свет - это и есть ответ.")
    finally:
        await led.send(rgb_command(255, 255, 255, 255))
        await led.disconnect()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Почему лента не светит")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--hold", type=float, default=3.0,
                   help="сколько держать каждый шаг, секунд")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

