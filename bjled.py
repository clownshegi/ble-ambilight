from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

log = logging.getLogger("bjled")

SERVICE_UUID = "0000eea0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ee01-0000-1000-8000-00805f9b34fb"
ACK_UUID = "0000ee02-0000-1000-8000-00805f9b34fb"

HEAD = b"\x69\x96"

CMD_POWER = 0x01
CMD_COLOUR = 0x02
CMD_EFFECT_A = 0x03
CMD_EFFECT_B = 0x04
CMD_TIMER = 0x05
CMD_CLOCK = 0x07

POST_CONNECT_SETTLE = 0.3


def frame(cmd: int, *payload: int, length: int | None = None) -> bytes:
    body = bytes((cmd & 0xFF, *(p & 0xFF for p in payload)))
    return HEAD + bytes((len(body) if length is None else length,)) + body


FULL_BRIGHTNESS = 0xFF


def rgb_command(r: int, g: int, b: int,
                w: int | None = FULL_BRIGHTNESS) -> bytes:
    if w is None:
        return frame(CMD_COLOUR, r, g, b, length=0x05)
    return frame(CMD_COLOUR, r, g, b, w)


def power_on_command(rgb: tuple[int, int, int] | None = None,
                     w: int = FULL_BRIGHTNESS) -> bytes:
    if rgb is None:
        return frame(CMD_POWER, 0x01)
    return frame(CMD_POWER, 0x01, rgb[0], rgb[1], rgb[2], w)


CMD_ON = power_on_command()
CMD_OFF = frame(CMD_POWER, 0x00)


def effect_command(mode: int, speed: int = 5, bank: int = 0) -> bytes:
    cmd = CMD_EFFECT_A if bank == 0 else CMD_EFFECT_B
    return frame(cmd, mode & 0xFF, max(1, min(0x0A, speed)))


def clock_command(unix_seconds: int) -> bytes:
    t = int(unix_seconds) & 0xFFFFFFFF
    return frame(CMD_CLOCK, (t >> 24) & 0xFF, (t >> 16) & 0xFF,
                 (t >> 8) & 0xFF, t & 0xFF, 0x00)


class BJLed:
    def __init__(self, address: str, *, write_uuid: str = WRITE_UUID,
                 connect_timeout: float = 20.0, max_backoff: float = 10.0):
        self.address = address
        self.write_uuid = write_uuid
        self.connect_timeout = connect_timeout
        self.max_backoff = max_backoff

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._closing = False
        self._backoff = 0.5
        self._direct_failures = 0
        self._fresh = False
        self._bg: set[asyncio.Task] = set()
        self.writes_ok = 0
        self.writes_failed = 0
        self.reconnects = 0
        self.last_payload: bytes | None = None
        self.last_rgb: tuple[int, int, int] | None = None
        self.last_write_at = 0.0


    @property
    def is_connected(self) -> bool:
        client = self._client
        return client is not None and client.is_connected

    def _on_disconnect(self, client) -> None:
        if client is not self._client:
            return
        if not self._closing:
            log.warning("BLE: соединение потеряно")
        self._client = None

    def _discard(self, client) -> None:
        if client is None:
            return
        if self._client is client:
            self._client = None

        async def close() -> None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except Exception:
                pass
        try:
            task = asyncio.get_running_loop().create_task(close())
        except RuntimeError:
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _open(self, device_or_address) -> bool:
        client = BleakClient(device_or_address,
                             disconnected_callback=self._on_disconnect,
                             timeout=self.connect_timeout)
        await client.connect()
        self._client = client
        self._fresh = True
        self.last_write_at = time.monotonic()
        await asyncio.sleep(POST_CONNECT_SETTLE)
        return True

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.is_connected:
                return True
            self._discard(self._client)
            self._closing = False

            if self._direct_failures < 2:
                try:
                    await self._open(self.address)
                    self._direct_failures = 0
                    self._backoff = 0.5
                    log.info("BLE: подключено к %s", self.address)
                    return True
                except (BleakError, OSError, asyncio.TimeoutError) as e:
                    self._direct_failures += 1
                    self._discard(self._client)
                    log.debug("BLE: прямое подключение не вышло (%s)", e)

            try:
                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=self.connect_timeout)
                if device is None:
                    log.warning("BLE: %s не виден в эфире", self.address)
                    return False
                await self._open(device)
                self._direct_failures = 0
                self._backoff = 0.5
                log.info("BLE: подключено к %s (через скан)", self.address)
                return True
            except (BleakError, OSError, asyncio.TimeoutError) as e:
                log.warning("BLE: подключиться не удалось: %s", e)
                self._discard(self._client)
                return False

    async def ensure_connected(self) -> bool:
        if self.is_connected:
            return True
        self.reconnects += 1
        ok = await self.connect()
        if not ok:
            await asyncio.sleep(self._backoff)
            self._backoff = min(self.max_backoff, self._backoff * 2)
        return ok

    async def disconnect(self) -> None:
        self._closing = True
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except (BleakError, OSError, asyncio.TimeoutError):
                pass


    def _remember(self, payload: bytes) -> None:
        self.writes_ok += 1
        self.last_payload = payload
        self.last_write_at = time.monotonic()
        if len(payload) >= 7 and payload[3] == CMD_COLOUR:
            self.last_rgb = (payload[4], payload[5], payload[6])

    async def send(self, payload: bytes) -> bool:
        async with self._lock:
            client = self._client
            if client is None or not client.is_connected:
                return False
            try:
                if self._fresh:
                    self._fresh = False
                    await client.write_gatt_char(self.write_uuid, payload,
                                                 response=False)
                    await asyncio.sleep(0.08)
                await client.write_gatt_char(self.write_uuid, payload,
                                             response=False)
            except (BleakError, OSError, asyncio.TimeoutError) as e:
                self.writes_failed += 1
                log.warning("BLE: запись не прошла: %s", e)
                self._discard(client)
                return False
            self._remember(payload)
            return True

    async def send_acked(self, payload: bytes) -> bool:
        async with self._lock:
            client = self._client
            if client is None or not client.is_connected:
                return False
            try:
                await client.write_gatt_char(ACK_UUID, payload, response=True)
            except (BleakError, OSError, asyncio.TimeoutError) as e:
                log.debug("BLE: запись с подтверждением не прошла: %s", e)
                return False
            self._remember(payload)
            return True

    async def keepalive(self) -> bool:
        if self.last_payload is not None:
            return await self.send(self.last_payload)
        async with self._lock:
            client = self._client
            if client is None or not client.is_connected:
                return False
            try:
                await client.read_gatt_char(self.write_uuid)
            except (BleakError, OSError, asyncio.TimeoutError) as e:
                log.debug("BLE: чтение для поддержания связи не прошло: %s", e)
                return False
            self.last_write_at = time.monotonic()
            return True

    async def set_rgb(self, r: int, g: int, b: int,
                      w: int | None = FULL_BRIGHTNESS) -> bool:
        return await self.send(rgb_command(r, g, b, w))

    async def turn_on(self, rgb: tuple[int, int, int] | None = None,
                      combined: bool = False) -> bool:
        if combined and rgb is not None:
            return await self.send(power_on_command(rgb))
        ok = await self.send(CMD_ON)
        await asyncio.sleep(0.08)
        ok = await self.send(CMD_ON) or ok
        if rgb is not None:
            await asyncio.sleep(0.05)
            ok = await self.set_rgb(*rgb) and ok
        return ok

    async def turn_off(self, acked: bool = False) -> bool:
        if acked and await self.send_acked(CMD_OFF):
            return True
        return await self.send(CMD_OFF)

    async def set_effect(self, mode: int, speed: int = 5, bank: int = 0) -> bool:
        return await self.send(effect_command(mode, speed, bank))


    async def __aenter__(self) -> "BJLed":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

