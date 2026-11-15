from __future__ import annotations

import argparse
import asyncio
import sys

from bjled import BJLed, FULL_BRIGHTNESS, effect_command

BANK_A = {
    0x00: "перелив всех цветов",
    0x01: "пульсация красным",
    0x02: "пульсация зелёным",
    0x03: "пульсация синим",
    0x04: "пульсация жёлтым",
    0x05: "пульсация голубым",
    0x06: "пульсация пурпурным",
    0x07: "пульсация белым",
    0x08: "перетекание красный-зелёный",
    0x09: "перетекание красный-синий",
    0x0A: "перетекание зелёный-синий",
    0x0B: "радуга",
    0x0C: "строб красным",
    0x0D: "строб зелёным",
    0x0E: "строб синим",
    0x0F: "строб жёлтым",
    0x10: "строб голубым",
    0x11: "строб пурпурным",
    0x12: "строб белым",
    0x13: "строб всеми",
    0x14: "скачки по цветам",
    0x15: "скачки RGB",
}

BANK_B = {m: "неизвестно" for m in range(0x00, 0x0A)}


def bank_of(name: str):
    return (0, BANK_A) if name.lower() == "a" else (1, BANK_B)


def show_list() -> int:
    print("БАНК A - команда 0x03, известен по открытым реализациям")
    for mode, label in BANK_A.items():
        print("  %2d  0x%02X  %s" % (mode, mode, label))
    print()
    print("БАНК B - команда 0x04, не реализован нигде")
    for mode in BANK_B:
        print("  %2d  0x%02X  %s" % (mode, mode, BANK_B[mode]))
    print()
    print("Посмотреть банк B глазами:  python effects.py <MAC> --walk b")
    return 0


async def amain(args: argparse.Namespace) -> int:
    led = BJLed(args.address)
    print("Подключаюсь к %s..." % args.address)
    if not await led.connect():
        print("Не подключился. Выключен ли Bluetooth на телефоне?")
        return 1
    await led.turn_on()
    await led.set_rgb(255, 255, 255, FULL_BRIGHTNESS)
    print("Подключено.\n")

    try:
        bank_idx, names = bank_of(args.bank)
        if args.walk:
            bank_idx, names = bank_of(args.walk)
            letter = args.walk.upper()
            print("Прохожу банк %s, по %.0f с на режим. Смотрите и записывайте."
                  % (letter, args.hold))
            print("Ctrl+C - прервать.\n")
            for mode in sorted(names):
                cmd = effect_command(mode, args.speed, bank=bank_idx)
                await led.send(cmd)
                print("  режим %2d (0x%02X)  %s   %s"
                      % (mode, mode, cmd.hex(" ").upper(), names[mode]))
                await asyncio.sleep(args.hold)
            print("\nЕсли в банке B нашлось что-то стоящее - скажите номер,")
            print("и я впишу название в effects.py.")
        else:
            mode = args.set
            if mode not in names:
                print("В банке %s нет режима %d. Список: --list"
                      % (args.bank.upper(), mode))
                return 1
            cmd = effect_command(mode, args.speed, bank=bank_idx)
            await led.send(cmd)
            print("  %s   режим %d (0x%02X), скорость %d  %s"
                  % (cmd.hex(" ").upper(), mode, mode, args.speed, names[mode]))
            print("\nЭффект крутится в самом контроллере - можно закрывать.")
    finally:
        if args.off:
            await led.turn_off()
        await led.disconnect()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Встроенные эффекты контроллера")
    p.add_argument("address", nargs="?", help="MAC-адрес ленты")
    p.add_argument("--list", action="store_true", help="показать режимы и выйти")
    p.add_argument("--set", type=int, metavar="N", help="включить режим N")
    p.add_argument("--walk", metavar="БАНК", choices=("a", "b", "A", "B"),
                   help="пройти весь банк по очереди")
    p.add_argument("--bank", default="a", choices=("a", "b", "A", "B"),
                   help="какой банк для --set")
    p.add_argument("--speed", type=int, default=5,
                   help="1 быстро, 10 медленно (значение инвертировано)")
    p.add_argument("--hold", type=float, default=6.0,
                   help="сколько держать режим при --walk")
    p.add_argument("--off", action="store_true", help="погасить в конце")
    args = p.parse_args()

    if args.list:
        return show_list()
    if not args.address:
        p.error("укажи MAC-адрес ленты (или --list)")
    if args.set is None and not args.walk:
        p.error("нужен --set N или --walk БАНК")
    if not 1 <= args.speed <= 10:
        p.error("--speed от 1 до 10")

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

