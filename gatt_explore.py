from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    sys.exit("Нет bleak. Установи:  python -m pip install bleak")


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

STANDARD_SERVICES = {
    "00001800": "Generic Access",
    "00001801": "Generic Attribute",
    "0000180a": "Device Information",
    "0000180f": "Battery Service",
    "0000fe59": "Nordic DFU",
}

KNOWN_HINTS = {
    "0000fff0": "часто service у ELK-BLEDOM-подобных",
    "0000fff3": "часто write-характеристика у ELK-BLEDOM-подобных",
    "0000ffd5": "часто service у Triones / LEDBLE",
    "0000ffd9": "часто write-характеристика у Triones / LEDBLE",
    "0000ffd4": "часто notify у Triones / LEDBLE",
    "0000ffe5": "часто service у MagicBlue-подобных",
    "0000ffe9": "часто write-характеристика у MagicBlue-подобных",
    "0000ffe0": "часто service у модулей HM-10 / JDY (прозрачный UART)",
    "0000ffe1": "часто write/notify у модулей HM-10 / JDY",
    "0000ffff": "часто service у LEDnetWF",
    "0000ff01": "часто write-характеристика у LEDnetWF",
    "0000ff02": "часто notify у LEDnetWF",
}

INTERESTING_READS = {
    "00002a00": "Device Name",
    "00002a24": "Model Number",
    "00002a25": "Serial Number",
    "00002a26": "Firmware Revision",
    "00002a27": "Hardware Revision",
    "00002a28": "Software Revision",
    "00002a29": "Manufacturer Name",
    "00002a19": "Battery Level",
}


def short(uuid):
    return uuid.lower()[:8]


def annotate(uuid):
    key = short(uuid)
    bits = []
    if key in STANDARD_SERVICES:
        bits.append(STANDARD_SERVICES[key])
    if key in INTERESTING_READS:
        bits.append(INTERESTING_READS[key])
    if key in KNOWN_HINTS:
        bits.append(KNOWN_HINTS[key])
    return "  <- " + "; ".join(bits) if bits else ""


def is_standard(uuid):
    return short(uuid) in STANDARD_SERVICES


def preview(data):
    if not data:
        return "(пусто)"
    h = data.hex(" ").upper()
    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return "%s   |%s|" % (h, txt)


async def resolve_device(target, name_filter, timeout):
    if target and MAC_RE.match(target):
        want = target.upper().replace("-", ":")
        print("Ищу %s (до %.0f с)..." % (want, timeout))
        dev = await BleakScanner.find_device_by_address(want, timeout=timeout)
        if dev is None:
            print("Не нашёл %s в эфире." % want)
        return dev

    needle = (name_filter or target or "").lower()
    print("Ищу устройство с именем, содержащим %r (до %.0f с)..." % (needle, timeout))
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches = [(d, a) for d, a in found.values()
               if (a.local_name or d.name or "") and
               needle in (a.local_name or d.name or "").lower()]
    if not matches:
        print("Ничего не нашёл. Запусти сначала:  python scan_ble.py -t 25 --all")
        return None
    if len(matches) > 1:
        print("Подходит несколько, беру самый сильный сигнал:")
        for d, a in matches:
            print("   %-28s %s  %d dBm" % (a.local_name or d.name, d.address, a.rssi))
    dev, _ = max(matches, key=lambda pair: pair[1].rssi)
    return dev


async def dump(device, do_read, out_path):
    print("\nПодключаюсь к %s [%s]..." % (device.name or "(без имени)", device.address))

    result = {
        "dumped_at": datetime.now().isoformat(timespec="seconds"),
        "address": device.address,
        "name": device.name,
        "services": [],
    }
    writable = []

    async with BleakClient(device, timeout=25.0) as client:
        print("Подключено. Читаю GATT-таблицу.\n")
        print("=" * 78)

        for service in client.services:
            std = " [стандартный]" if is_standard(service.uuid) else ""
            print("\nSERVICE  %s%s%s" % (service.uuid, std, annotate(service.uuid)))
            print("         handle=%d  %s" % (service.handle, service.description))

            svc_rec = {
                "uuid": service.uuid,
                "handle": service.handle,
                "description": service.description,
                "standard": is_standard(service.uuid),
                "characteristics": [],
            }

            for char in service.characteristics:
                props = ",".join(char.properties)
                can_write = bool({"write", "write-without-response"} & set(char.properties))
                mark = "  <<< ПИСАТЬ СЮДА?" if can_write and not is_standard(service.uuid) else ""
                print("\n    CHAR   %s%s" % (char.uuid, annotate(char.uuid)))
                print("           handle=%d  props=[%s]%s" % (char.handle, props, mark))
                if char.description:
                    print("           описание: %s" % char.description)
                if can_write:
                    print("           max write w/o response: %d байт"
                          % char.max_write_without_response_size)

                char_rec = {
                    "uuid": char.uuid,
                    "handle": char.handle,
                    "properties": list(char.properties),
                    "description": char.description,
                    "max_write_without_response_size": char.max_write_without_response_size,
                    "value_hex": None,
                    "value_text": None,
                    "descriptors": [],
                }

                if do_read and "read" in char.properties:
                    try:
                        data = bytes(await client.read_gatt_char(char))
                        print("           значение: %s" % preview(data))
                        char_rec["value_hex"] = data.hex(" ").upper()
                        char_rec["value_text"] = "".join(
                            chr(b) if 32 <= b < 127 else "." for b in data)
                    except (BleakError, OSError, asyncio.TimeoutError) as e:
                        print("           значение: не прочиталось (%s)" % e)

                for desc in char.descriptors:
                    line = "           descriptor %s (handle=%d) %s" % (
                        desc.uuid, desc.handle, desc.description)
                    desc_rec = {"uuid": desc.uuid, "handle": desc.handle,
                                "description": desc.description, "value_hex": None}
                    if do_read:
                        try:
                            dd = bytes(await client.read_gatt_descriptor(desc.handle))
                            if dd:
                                line += " = %s" % dd.hex(" ").upper()
                                desc_rec["value_hex"] = dd.hex(" ").upper()
                        except (BleakError, OSError, asyncio.TimeoutError):
                            pass
                    print(line)
                    char_rec["descriptors"].append(desc_rec)

                if can_write:
                    writable.append({
                        "service_uuid": service.uuid,
                        "char_uuid": char.uuid,
                        "properties": list(char.properties),
                        "max_write": char.max_write_without_response_size,
                        "standard": is_standard(service.uuid),
                    })
                svc_rec["characteristics"].append(char_rec)

            result["services"].append(svc_rec)

    print("\n" + "=" * 78)
    custom = [w for w in writable if not w["standard"]]
    result["writable"] = writable
    if custom:
        print("\nКУДА МОЖНО ПИСАТЬ (нестандартные сервисы):")
        for w in custom:
            wtype = ("write-without-response"
                     if "write-without-response" in w["properties"] else "write")
            print("\n   service        %s%s" % (w["service_uuid"],
                                                annotate(w["service_uuid"])))
            print("   characteristic %s%s" % (w["char_uuid"],
                                              annotate(w["char_uuid"])))
            print("   тип записи     %s, до %d байт" % (wtype, w["max_write"]))
    else:
        print("\nЗаписываемых характеристик вне стандартных сервисов не нашлось.")
        print("Возможно, устройство отдаёт полную таблицу только после спаривания.")

    notify = []
    for svc in result["services"]:
        for ch in svc["characteristics"]:
            if {"notify", "indicate"} & set(ch["properties"]):
                notify.append((svc["uuid"], ch["uuid"]))
    if notify:
        print("\nХарактеристики с notify (пригодятся, если контроллер шлёт ответы):")
        for su, cu in notify:
            print("   %s / %s" % (su, cu))

    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print("\nДамп сохранён: %s" % out_path)
        except OSError as e:
            print("\nНе смог сохранить %s: %s" % (out_path, e))

    return result


def hint_on_error(e):
    msg = str(e).lower()
    print("\nНе получилось: %s" % e)
    print("\nЧто обычно помогает:")
    if "not found" in msg or "unreachable" in msg or "timeout" in msg:
        print("  - Выключи Bluetooth на телефоне: контроллер держит только одно")
        print("    соединение, и пока приложение подключено, ПК не пустят.")
        print("  - Обесточь ленту на 5 секунд и включи заново.")
        print("  - Поднеси ПК ближе, RSSI слабее -85 dBm часто не даёт подключиться.")
    if "access" in msg or "denied" in msg or "auth" in msg:
        print("  - Удали устройство в Параметрах Windows (Bluetooth и устройства)")
        print("    и попробуй снова - лишнее спаривание часто мешает.")
    print("  - Перезапусти Bluetooth-адаптер Windows (выкл/вкл в Параметрах).")


async def amain(args):
    device = await resolve_device(args.target, args.name, args.scan_time)
    if device is None:
        return 1
    try:
        await dump(device, not args.no_read, args.out)
    except (BleakError, OSError, asyncio.TimeoutError) as e:
        hint_on_error(e)
        return 1
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(
        description="Дамп GATT-таблицы BLE-устройства")
    p.add_argument("target", nargs="?", default=None,
                   help="MAC-адрес (AA:BB:CC:DD:EE:FF) или часть имени")
    p.add_argument("--name", metavar="SUBSTR", default=None,
                   help="искать по части имени, а не по MAC")
    p.add_argument("--scan-time", type=float, default=15.0,
                   help="сколько секунд искать устройство (по умолчанию 15)")
    p.add_argument("--no-read", action="store_true",
                   help="не читать значения характеристик, только структура")
    p.add_argument("-o", "--out", metavar="FILE", default="gatt_dump.json",
                   help="куда сохранить дамп (по умолчанию gatt_dump.json)")
    args = p.parse_args()

    if not args.target and not args.name:
        p.error("укажи MAC-адрес или --name ЧАСТЬ_ИМЕНИ")

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n(остановлено)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

