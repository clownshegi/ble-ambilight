from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

SERVICE_UUID = "0000eea0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ee01-0000-1000-8000-00805f9b34fb"
ALT_UUID = "0000ee02-0000-1000-8000-00805f9b34fb"

HEAD = b"\x69\x96"


def frame(cmd: int, *payload: int, length: int | None = None) -> bytes:
    body = bytes((cmd & 0xFF, *(p & 0xFF for p in payload)))
    return HEAD + bytes((len(body) if length is None else length,)) + body


def colour(r: int, g: int, b: int, w: int | None = None) -> bytes:
    if w is None:
        return frame(0x02, r, g, b, length=0x05)
    return frame(0x02, r, g, b, w)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


class Probe:
    def __init__(self, client: BleakClient, delay: float):
        self.client = client
        self.delay = delay
        self.notifications: list[bytes] = []

    async def write(self, payload: bytes, uuid: str = WRITE_UUID) -> bool:
        try:
            await self.client.write_gatt_char(uuid, payload, response=False)
            return True
        except (BleakError, OSError) as e:
            print("      запись не прошла: %s" % e)
            return False

    async def show(self, payload: bytes, label: str = "") -> None:
        ok = await self.write(payload)
        print("   %-28s %s%s" % (payload.hex(" "), label,
                                 "" if ok else "  [ОШИБКА]"))
        await asyncio.sleep(self.delay)

    def on_notify(self, _sender, data: bytearray) -> None:
        self.notifications.append(bytes(data))
        print("   !!! УВЕДОМЛЕНИЕ: %s" % bytes(data).hex(" "))


async def stage_read(p: Probe) -> None:
    print("\n[read] Чтение характеристик и попытка подписки на уведомления")
    print("  Обе характеристики объявляют READ, ee01 объявляет NOTIFY.")
    print("  В дампах штатное приложение не подписывалось ни разу.\n")

    for name, uuid in (("ee01 (write+notify)", WRITE_UUID),
                       ("ee02 (write+write_rsp)", ALT_UUID)):
        try:
            val = await p.client.read_gatt_char(uuid)
            print("   %-24s -> %s" % (name, val.hex(" ") if val else "(пусто)"))
        except (BleakError, OSError) as e:
            print("   %-24s -> чтение не поддержано: %s" % (name, e))

    print("\n   Подписываюсь на ee01 и дёргаю состояние...")
    try:
        await p.client.start_notify(WRITE_UUID, p.on_notify)
    except (BleakError, OSError) as e:
        print("   подписка не удалась: %s" % e)
        return

    for f in (frame(0x01, 0x01), colour(0xFF, 0x00, 0x00),
              frame(0x01, 0x00), frame(0x01, 0x01)):
        await p.show(f, "(ждём ответ)")
    for cmd in (0x00, 0x06, 0x08, 0x09, 0x0A):
        await p.show(frame(cmd, 0x01), "запрос состояния? cmd=0x%02x" % cmd)

    await asyncio.sleep(2.0)
    try:
        await p.client.stop_notify(WRITE_UUID)
    except (BleakError, OSError):
        pass

    if p.notifications:
        print("\n   ИТОГ: устройство отвечает! %d пакет(ов)." % len(p.notifications))
    else:
        print("\n   ИТОГ: ни одного уведомления. Совпадает с наблюдением автора")
        print("         протокола: notify объявлен, но прошивка молчит.")


async def stage_white(p: Probe) -> None:
    print("\n[white] Четвёртый байт команды цвета 69 96 05 02 RR GG BB WW")
    print("  Приложение всегда шлёт WW = V из HSV, то есть яркость ползунка.")
    print("  Если WW - настоящая яркость, ряд ниже будет гаснуть при том же")
    print("  красном. Если это белый канал, на RGB-ленте ничего не изменится.\n")

    print("  Красный RR=ff, меняется только WW:")
    for ww in (0xFF, 0xC0, 0x80, 0x40, 0x10, 0x00):
        await p.show(colour(0xFF, 0x00, 0x00, ww), "WW=0x%02x" % ww)

    print("\n  Контроль: тот же ряд, но масштабируем сам красный (так делаем сейчас):")
    for rr in (0xFF, 0xC0, 0x80, 0x40, 0x10, 0x00):
        await p.show(colour(rr, 0x00, 0x00), "RR=0x%02x" % rr)

    ans = ask("\n  Менялась ли яркость в ПЕРВОМ ряду? [y/n]: ")
    if ans.startswith("y"):
        print("  -> WW работает как яркость. Это и есть недостающая команда:")
        print("     можно держать RGB на полной насыщенности и гасить через WW,")
        print("     что убирает потерю цветности на тёмных сценах.")
    else:
        print("  -> WW ни на что не влияет, как и предполагал автор протокола.")
        print("     Яркость остаётся масштабированием RGB.")


async def stage_bankb(p: Probe) -> None:
    print("\n[bankb] Второй банк эффектов - команда 0x04, режимы 0x00..0x09")
    print("  В дампах приложение шлёт и 69 96 03 03 MM SS, и 69 96 03 04 MM SS.")
    print("  Все открытые реализации знают только первый банк.")
    print("  Смотрите на ленту и записывайте, что видите.\n")

    for mode in range(0x00, 0x0A):
        await p.show(frame(0x04, mode, 0x05), "банк B, режим 0x%02x" % mode)
        await asyncio.sleep(2.5)

    print("\n  Теперь проверим, есть ли у первого банка режимы выше 0x15:")
    for mode in (0x15, 0x16, 0x17, 0x18, 0x1A, 0x1F):
        await p.show(frame(0x03, mode, 0x05), "банк A, режим 0x%02x" % mode)
        await asyncio.sleep(2.0)

    print("\n  И скорость за пределами 0x0a (автор писал 'strange things happen'):")
    for sp in (0x01, 0x0A, 0x14, 0x40, 0xFF):
        await p.show(frame(0x03, 0x0B, sp), "скорость 0x%02x" % sp)
        await asyncio.sleep(2.0)


async def stage_sweep(p: Probe) -> None:
    print("\n[sweep] Перебор идентификаторов команд")
    print("  Заняты: 01 питание, 02 цвет, 03/04 эффекты, 05 таймер, 07 часы.")
    print("  Пробуем остальные. Между пробами возвращаем известный красный,")
    print("  чтобы было видно, изменила ли неизвестная команда состояние.\n")

    known = {0x01, 0x02, 0x03, 0x04, 0x05, 0x07}
    marker = colour(0x80, 0x00, 0x00)

    for cmd in range(0x00, 0x20):
        if cmd in known:
            continue
        print("  -- cmd 0x%02x --" % cmd)
        await p.write(marker)
        await asyncio.sleep(0.4)
        for args in ((0x01,), (0x01, 0x01), (0x01, 0xFF, 0xFF, 0xFF),
                     (0x00, 0x00, 0x00, 0x00, 0x00)):
            await p.show(frame(cmd, *args), "")
        ans = ask("     что-нибудь изменилось? [y/n/q]: ")
        if ans.startswith("q"):
            return
        if ans.startswith("y"):
            print("     *** ЗАПИШИТЕ: команда 0x%02x что-то делает ***" % cmd)


STAGES = {
    "read": stage_read,
    "white": stage_white,
    "bankb": stage_bankb,
    "sweep": stage_sweep,
}


async def amain(args: argparse.Namespace) -> int:
    print("Ищу %s..." % args.address)
    device = await BleakScanner.find_device_by_address(args.address, timeout=15.0)
    if device is None:
        print("Не нашёл. Лента под питанием? Телефон не держит соединение?")
        return 1

    async with BleakClient(device, timeout=25.0) as client:
        print("Подключено.")
        for s in client.services:
            if s.uuid.lower().startswith("0000eea0"):
                for c in s.characteristics:
                    print("   %s  props=%s" % (c.uuid, ",".join(c.properties)))

        p = Probe(client, args.delay)
        await p.write(frame(0x01, 0x01))
        await asyncio.sleep(0.5)

        stages = list(STAGES) if args.stage == "all" else [args.stage]
        for name in stages:
            await STAGES[name](p)

        await p.write(colour(0x50, 0x3C, 0x28))
    print("\nГотово.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Разведка протокола BJ_LED")
    ap.add_argument("address", help="MAC-адрес ленты")
    ap.add_argument("--stage", default="all",
                    choices=list(STAGES) + ["all"],
                    help="какой этап выполнить")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="пауза между командами, с")
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130
    except (BleakError, OSError) as e:
        print("Ошибка BLE: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

