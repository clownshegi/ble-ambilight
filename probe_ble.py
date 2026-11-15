from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from bjled import WRITE_UUID, rgb_command, CMD_ON

RAMP_RATES = (10, 15, 20, 25, 30, 40, 60)


def hsv_to_rgb(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i % 6]
    return round(r * 255), round(g * 255), round(b * 255)


async def measure_read_latency(client: BleakClient, n: int = 30) -> None:
    print("\n1. ЗАДЕРЖКА GATT-ЧТЕНИЯ (нижняя граница - интервал соединения)")
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            await client.read_gatt_char(WRITE_UUID)
        except (BleakError, OSError) as e:
            print("   чтение не прошло: %s" % e)
            return
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    print("   минимум %6.1f мс" % samples[0])
    print("   медиана %6.1f мс" % statistics.median(samples))
    print("   p90     %6.1f мс" % samples[int(len(samples) * 0.9)])
    print("   максимум%6.1f мс" % samples[-1])
    print("   -> интервал соединения примерно %.0f мс, то есть потолок около "
          "%.0f транзакций/с" % (samples[0], 1000.0 / max(1.0, samples[0])))


async def measure_burst(client: BleakClient, n: int = 100) -> None:
    print("\n2. ПАЧКА ЗАПИСЕЙ БЕЗ ПОДТВЕРЖДЕНИЯ (%d штук подряд)" % n)
    payload = rgb_command(40, 40, 40)
    per_write = []
    t0 = time.perf_counter()
    for _ in range(n):
        t1 = time.perf_counter()
        try:
            await client.write_gatt_char(WRITE_UUID, payload, response=False)
        except (BleakError, OSError) as e:
            print("   запись не прошла на %d-й: %s" % (len(per_write) + 1, e))
            break
        per_write.append((time.perf_counter() - t1) * 1000.0)
    total = time.perf_counter() - t0
    if not per_write:
        return
    per_write_sorted = sorted(per_write)
    print("   всего %.3f с на %d записей -> %.0f пакетов/с"
          % (total, len(per_write), len(per_write) / total))
    print("   на запись: медиана %.2f мс, p90 %.2f мс, максимум %.2f мс"
          % (statistics.median(per_write_sorted),
             per_write_sorted[int(len(per_write_sorted) * 0.9)],
             per_write_sorted[-1]))
    if statistics.median(per_write_sorted) < 1.0:
        print("   -> write_gatt_char возвращается сразу, пакеты буферизуются")
        print("      стеком Windows. Значит стек нас не притормозит, и беречь")
        print("      контроллер придётся своим ограничителем частоты.")
    else:
        print("   -> запись блокируется до отправки, стек сам держит темп.")


async def ramp(client: BleakClient, seconds: float) -> None:
    print("\n3. ЛЕСЕНКА ПО ЧАСТОТАМ - смотрите на ленту")
    print("   Плавный перебор оттенка, %.0f с на каждую частоту." % seconds)
    print("   Ищем, где перестаёт быть плавно: рывки, ступеньки, застревание.\n")
    print("   %5s  %10s  %10s  %9s  %s"
          % ("Гц", "задано", "получилось", "джиттер", "ошибок"))
    for hz in RAMP_RATES:
        period = 1.0 / hz
        count = max(1, int(seconds * hz))
        gaps = []
        failures = 0
        hue = 0.0
        t_start = time.perf_counter()
        next_at = t_start
        prev = None
        for _ in range(count):
            now = time.perf_counter()
            if prev is not None:
                gaps.append((now - prev) * 1000.0)
            prev = now
            hue = (hue + 1.0 / (hz * 4.0)) % 1.0
            try:
                await client.write_gatt_char(
                    WRITE_UUID, rgb_command(*hsv_to_rgb(hue, 1.0, 0.85)),
                    response=False)
            except (BleakError, OSError):
                failures += 1
            next_at += period
            delay = next_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        elapsed = time.perf_counter() - t_start
        jitter = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
        print("   %5d  %10.1f  %10.1f  %7.1f мс  %d"
              % (hz, hz, count / elapsed, jitter, failures))
    print("\n   Частоту, после которой стало хуже, и берём за потолок.")


async def amain(args: argparse.Namespace) -> int:
    print("Ищу %s..." % args.address)
    device = await BleakScanner.find_device_by_address(args.address, timeout=15.0)
    if device is None:
        print("Не нашёл. Выключен ли Bluetooth на телефоне? Есть ли питание?")
        return 1

    async with BleakClient(device, timeout=25.0) as client:
        print("Подключено.")
        try:
            print("MTU: %d байт (полезной нагрузки %d)"
                  % (client.mtu_size, client.mtu_size - 3))
        except Exception:
            pass

        await client.write_gatt_char(WRITE_UUID, CMD_ON, response=False)
        await asyncio.sleep(0.3)

        await measure_read_latency(client)
        await measure_burst(client, args.burst)
        await asyncio.sleep(0.5)
        if not args.skip_ramp:
            await ramp(client, args.ramp_seconds)

        await client.write_gatt_char(WRITE_UUID, rgb_command(80, 60, 40, 0xFF),
                                     response=False)
    print("\nГотово.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Замер потолка BLE-канала")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--burst", type=int, default=100,
                   help="сколько записей в пачке")
    p.add_argument("--ramp-seconds", type=float, default=4.0,
                   help="секунд на каждую частоту в лесенке")
    p.add_argument("--skip-ramp", action="store_true",
                   help="без визуальной части")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130
    except (BleakError, OSError) as e:
        print("Ошибка BLE: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

