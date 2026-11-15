from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

try:
    from bleak import BleakScanner
    from bleak.exc import BleakError
except ImportError:
    sys.exit("Нет bleak. Установи:  python -m pip install bleak")


LED_NAME_HINTS = (
    "ELK-BLEDOM", "ELK-BLE", "ELK-BULB", "ELK",
    "LEDBLE", "LEDBLUE", "LED BLE", "LEDVALET",
    "TRIONES",
    "LEDNETWF", "LEDNET",
    "MELK",
    "BJ_LED", "BJLED", "BJ-LED", "BJ-5V", "5VRGB", "BJ",
    "MOHUANLED",
    "SP105E", "SP110E", "SP107E", "SP511E",
    "QHM-", "KS03", "LOTUS LANTERN", "IDEAL LED",
    "MAGIC", "HTZM", "JDY-", "ILED", "AP-",
    "LEDCONTROLLER", "RGB", "LED",
)

NO_NAME = "(без имени)"


def looks_like_led(name):
    if not name:
        return False
    up = name.upper()
    return any(hint in up for hint in LED_NAME_HINTS)


def hexs(b):
    if not b:
        return ""
    return b.hex(" ").upper()


@dataclass
class Seen:
    address: str
    name: str | None = None
    rssi: int = -127
    rssi_best: int = -127
    rssi_worst: int = 0
    adv_count: int = 0
    service_uuids: list = field(default_factory=list)
    manufacturer_data: dict = field(default_factory=dict)
    service_data: dict = field(default_factory=dict)
    tx_power: int | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0

    def update(self, device_name, adv, now):
        is_new = self.adv_count == 0
        name_resolved = False

        candidate = adv.local_name or device_name
        if candidate and candidate != self.name:
            name_resolved = not is_new and not self.name
            self.name = candidate

        self.rssi = adv.rssi
        self.rssi_best = max(self.rssi_best, adv.rssi)
        self.rssi_worst = min(self.rssi_worst, adv.rssi)
        self.adv_count += 1

        for u in adv.service_uuids:
            if u not in self.service_uuids:
                self.service_uuids.append(u)
        for cid, data in adv.manufacturer_data.items():
            self.manufacturer_data["0x%04X" % cid] = hexs(data)
        for uuid, data in adv.service_data.items():
            self.service_data[uuid] = hexs(data)
        if adv.tx_power is not None:
            self.tx_power = adv.tx_power

        if is_new:
            self.first_seen = now
        self.last_seen = now
        return is_new, name_resolved

    @property
    def display_name(self):
        return self.name or NO_NAME

    @property
    def is_candidate(self):
        return looks_like_led(self.name)


async def run_scan(duration, live):
    seen = {}
    t0 = time.monotonic()

    def on_adv(device, adv):
        entry = seen.get(device.address)
        if entry is None:
            entry = Seen(address=device.address)
            seen[device.address] = entry
        is_new, name_resolved = entry.update(device.name, adv, time.monotonic())
        if is_new or name_resolved:
            mark = "*" if entry.is_candidate else " "
            tag = "+" if is_new else "~"
            line = "  [%5.1fs] %s%s %4d dBm  %-28.28s  %s" % (
                time.monotonic() - t0, tag, mark,
                entry.rssi, entry.display_name, entry.address,
            )
            print(line, flush=True)

    scanner = BleakScanner(detection_callback=on_adv, scanning_mode="active")
    try:
        async with scanner:
            if live:
                print("  (режим --live: Ctrl+C чтобы остановить)\n", flush=True)
                while True:
                    await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(duration)
    except asyncio.CancelledError:
        pass
    except BleakError as e:
        print("\nОшибка BLE: %s" % e)
        print("Проверь: Bluetooth включён, адаптер поддерживает BLE 4.0+.")
    return seen


def print_table(rows, baseline, title):
    if not rows:
        return
    print("\n" + title)
    newcol = "  NEW" if baseline is not None else ""
    print("%2s%5s  %-28s  %-17s  %4s%s" % ("", "RSSI", "ИМЯ", "MAC", "ADV", newcol))
    print("-" * (62 + len(newcol)))
    for d in rows:
        mark = "* " if d.is_candidate else "  "
        flag = ""
        if baseline is not None:
            flag = "   <<<" if d.address not in baseline else "      "
        print("%s%5d  %-28.28s  %-17s  %4d%s" % (
            mark, d.rssi, d.display_name, d.address, d.adv_count, flag))


def print_details(d):
    print("\n  %s  [%s]" % (d.display_name, d.address))
    print("      RSSI: %d dBm (лучший %d, худший %d), пакетов: %d" % (
        d.rssi, d.rssi_best, d.rssi_worst, d.adv_count))
    if d.tx_power is not None:
        print("      TX power: %s" % d.tx_power)
    if d.service_uuids:
        print("      service UUIDs:")
        for u in d.service_uuids:
            print("        - %s" % u)
    else:
        print("      service UUIDs: нет в рекламе "
              "(нормально, найдём при подключении)")
    if d.manufacturer_data:
        print("      manufacturer data:")
        for cid, data in d.manufacturer_data.items():
            print("        %s: %s" % (cid, data))
    if d.service_data:
        print("      service data:")
        for u, data in d.service_data.items():
            print("        %s: %s" % (u, data))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(
        description="Этап 1: сканирование BLE и поиск LED-контроллера")
    p.add_argument("-t", "--time", type=float, default=15.0,
                   help="длительность скана в секундах (по умолчанию 15)")
    p.add_argument("--live", action="store_true",
                   help="сканировать до Ctrl+C")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="подробности (UUID, manufacturer data) по всем")
    p.add_argument("--filter", metavar="SUBSTR", default=None,
                   help="только устройства, чьё имя содержит подстроку")
    p.add_argument("--all", action="store_true",
                   help="не сворачивать список безымянных устройств")
    p.add_argument("--baseline", metavar="FILE", default=None,
                   help="сравнить с прошлым сканом, новые адреса помечаются <<<")
    p.add_argument("-o", "--out", metavar="FILE", default="scan_results.json",
                   help="куда сохранить результат (по умолчанию scan_results.json)")
    p.add_argument("--no-save", action="store_true", help="не сохранять JSON")
    args = p.parse_args()

    baseline = None
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = set(d["address"] for d in json.load(f)["devices"])
            print("Базовый скан: %s (%d устройств)" % (args.baseline, len(baseline)))
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as e:
            print("Не смог прочитать baseline %s: %s" % (args.baseline, e))
            return 1

    if args.live:
        print("Сканирую BLE...")
    else:
        print("Сканирую BLE %.0f секунд..." % args.time)
    print()

    try:
        seen = asyncio.run(run_scan(args.time, args.live))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130

    devices = list(seen.values())
    if args.filter:
        needle = args.filter.lower()
        devices = [d for d in devices if d.name and needle in d.name.lower()]

    devices.sort(key=lambda d: d.rssi, reverse=True)
    named = [d for d in devices if d.name]
    unnamed = [d for d in devices if not d.name]
    candidates = [d for d in named if d.is_candidate]

    print("\n" + "=" * 70)
    print("Найдено: %d устройств (%d с именем, %d без имени)" % (
        len(devices), len(named), len(unnamed)))
    print("=" * 70)

    print_table(named, baseline, "С ИМЕНЕМ (* = похоже на LED-контроллер):")

    if unnamed:
        if args.all or args.verbose:
            print_table(unnamed, baseline, "БЕЗ ИМЕНИ:")
        else:
            print("\nБез имени: %d шт. (покажет --all; обычно это наушники, "
                  "часы, чужие телефоны)" % len(unnamed))

    if args.verbose:
        print("\n" + "=" * 70 + "\nПОДРОБНОСТИ\n" + "=" * 70)
        for d in named + (unnamed if args.all else []):
            print_details(d)
    elif candidates:
        print("\n" + "=" * 70 + "\nКАНДИДАТЫ\n" + "=" * 70)
        for d in candidates:
            print_details(d)

    print("\n" + "=" * 70)
    if candidates:
        print("Похоже, лента - одно из этих:")
        for d in candidates:
            print("   %-28s %s   (%d dBm)" % (d.display_name, d.address, d.rssi))
        best = max(candidates, key=lambda d: d.rssi)
        print("\nСледующий шаг - вытащить GATT-таблицу:")
        print("   python gatt_explore.py %s" % best.address)
    else:
        print("Явных кандидатов нет. Что попробовать:")
        print("  1. Выключи Bluetooth на телефоне: пока приложение держит")
        print("     соединение, контроллер часто не отвечает на скан.")
        print("  2. Если лента была спарена в Параметрах Windows - удали её")
        print("     (Bluetooth и устройства -> ... -> Удалить устройство).")
        print("  3. Сканируй дольше:  python scan_ble.py -t 40 --all -v")
        print("  4. Метод разницы: скан с обесточенной лентой, потом с включённой:")
        print("        python scan_ble.py -t 20 -o off.json        (лента выкл.)")
        print("        python scan_ble.py -t 20 --baseline off.json --all")
        print("     Новые адреса будут помечены <<<.")
        print("  5. Имя может быть пустым - тогда ищи по RSSI: самый сильный")
        print("     сигнал, когда ноутбук вплотную к контроллеру.")

    if not args.no_save:
        payload = {
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "duration_s": None if args.live else args.time,
            "devices": [asdict(d) for d in devices],
        }
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print("\nРезультат сохранён: %s" % args.out)
        except OSError as e:
            print("\nНе смог сохранить %s: %s" % (args.out, e))

    return 0


if __name__ == "__main__":
    sys.exit(main())

