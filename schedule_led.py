from __future__ import annotations

import argparse
import asyncio
import sys
import time

from bjled import BJLed, frame, clock_command, CMD_TIMER

DAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def day_mask(spec: str) -> int:
    if spec.lower() in ("все", "all", "*"):
        return 0xFF
    mask = 0x80
    for token in spec.split(","):
        token = token.strip().lower()
        if token not in DAY_NAMES:
            raise ValueError("не знаю день %r, ожидаю из: %s"
                             % (token, ", ".join(DAY_NAMES)))
        mask |= 1 << DAY_NAMES.index(token)
    return mask


def describe_mask(mask: int) -> str:
    days = [DAY_NAMES[i] for i in range(7) if mask & (1 << i)]
    flag = "вкл" if mask & 0x80 else "выкл"
    return "%s, дни: %s" % (flag, ", ".join(days) if days else "нет")


def timer_command(hour: int, minute: int, mask: int, slot: int = 1) -> bytes:
    return frame(CMD_TIMER, slot, mask, hour, minute, 0x00)


def parse_time(text: str) -> tuple[int, int]:
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("время задаётся как ЧЧ:ММ")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("часы 0..23, минуты 0..59")
    return h, m


async def amain(args: argparse.Namespace) -> int:
    now = int(time.time())
    plan: list[tuple[str, bytes]] = []
    if args.sync_clock or args.at:
        plan.append(("сверить часы (%s)" % time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(now)),
                     clock_command(now)))
    if args.at:
        h, m = parse_time(args.at)
        mask = day_mask(args.days)
        plan.append(("расписание на %02d:%02d, %s" % (h, m, describe_mask(mask)),
                     timer_command(h, m, mask, args.slot)))

    print("Отправлю:")
    for label, cmd in plan:
        print("  %-46s %s" % (label, cmd.hex(" ").upper()))
    if args.dry_run:
        print("\n--dry-run: ничего не отправлено.")
        return 0

    led = BJLed(args.address)
    print("\nПодключаюсь к %s..." % args.address)
    if not await led.connect():
        print("Не подключился. Выключен ли Bluetooth на телефоне?")
        return 1
    try:
        for label, cmd in plan:
            ok = await led.send(cmd)
            print("  %-46s %s" % (label, "отправлено" if ok else "НЕ ПРОШЛО"))
            await asyncio.sleep(0.3)
    finally:
        await led.disconnect()

    print("\nРаскладка таймера выведена по косвенным признакам, поэтому")
    print("проверьте наблюдением: сработает ли в назначенное время.")
    print("Если нет - скажите, и разберём дальше.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description="Часы и расписание внутри контроллера")
    p.add_argument("address", help="MAC-адрес ленты")
    p.add_argument("--sync-clock", action="store_true",
                   help="выставить контроллеру текущее время")
    p.add_argument("--at", metavar="ЧЧ:ММ", help="время срабатывания")
    p.add_argument("--days", default="все",
                   help="дни через запятую: пн,вт,... или «все»")
    p.add_argument("--slot", type=int, default=1,
                   help="номер ячейки расписания (предположительно)")
    p.add_argument("--dry-run", action="store_true",
                   help="показать кадры и не отправлять")
    args = p.parse_args()

    if not args.sync_clock and not args.at:
        p.error("нужен --sync-clock или --at ЧЧ:ММ")
    try:
        if args.at:
            parse_time(args.at)
            day_mask(args.days)
    except ValueError as e:
        p.error(str(e))

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

