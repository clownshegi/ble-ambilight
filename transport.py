from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

from bjled import BJLed, FULL_BRIGHTNESS
from color import split_ww

log = logging.getLogger("transport")


class Sink:
    capacity = 1

    async def open(self) -> bool:
        raise NotImplementedError

    async def send(self, codes: list[tuple[int, int, int]]) -> None:
        raise NotImplementedError

    async def close(self, turn_off: bool = True) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> int:
        return 0

    def label(self) -> str:
        return "?"

    def stats(self) -> str:
        return ""


class _Link:
    def __init__(self, address: str, fps: float, use_ww: bool,
                 keepalive: float = 20.0):
        self.led = BJLed(address)
        self.period = 1.0 / max(0.5, fps)
        self.use_ww = use_ww
        self.keepalive = keepalive
        self.pending: tuple[int, int, int] | None = None
        self.last: tuple[int, int, int] | None = None
        self.last_at = 0.0
        self.sent = 0
        self.stalls = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def address(self) -> str:
        return self.led.address

    async def start(self) -> bool:
        ok = await self.led.connect()
        if ok:
            await self.led.turn_on()
        self._task = asyncio.ensure_future(self._pump())
        return ok

    async def _pump(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            colour, self.pending = self.pending, None
            now = time.monotonic()
            if colour is not None and colour == self.last:
                colour = None
            stale = now - self.last_at >= self.keepalive
            if colour is None and self.last is not None and stale:
                colour = self.last
            if colour is not None:
                if not self.led.is_connected:
                    self.stalls += 1
                    if await self.led.ensure_connected():
                        await self.led.turn_on()
                        self.last = None
                if self.led.is_connected:
                    args = split_ww(colour) if self.use_ww else colour
                    if await self.led.set_rgb(*args):
                        self.last = colour
                        self.last_at = now
                        self.sent += 1
            delay = self.period - (time.perf_counter() - t0)
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def stop(self, turn_off: bool) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self.led.is_connected:
            if turn_off:
                await self.led.turn_off(acked=True)
            else:
                await self.led.set_rgb(*(self.last or (255, 255, 255)),
                                       w=FULL_BRIGHTNESS)
        await self.led.disconnect()


class BLESink(Sink):
    def __init__(self, addresses: list[str], fps: float = 20.0,
                 use_ww: bool = False, keepalive: float = 20.0):
        if not addresses:
            raise ValueError("нужен хотя бы один адрес")
        self.links = [_Link(a, fps, use_ww, keepalive) for a in addresses]
        self.capacity = len(self.links)

    async def open(self) -> bool:
        results = await asyncio.gather(*(l.start() for l in self.links))
        live = sum(1 for r in results if r)
        for link, ok in zip(self.links, results):
            if not ok:
                log.warning("BLE: %s не подключилась, буду пробовать дальше",
                            link.address)
        return live > 0

    async def send(self, codes: list[tuple[int, int, int]]) -> None:
        n, m = len(codes), len(self.links)
        for i, link in enumerate(self.links):
            lo = i * n // m
            hi = max(lo + 1, (i + 1) * n // m)
            group = codes[lo:hi]
            if len(group) == 1:
                link.pending = group[0]
            else:
                link.pending = tuple(
                    sum(c[k] for c in group) // len(group) for k in range(3))

    async def close(self, turn_off: bool = True) -> None:
        await asyncio.gather(*(l.stop(turn_off) for l in self.links),
                             return_exceptions=True)

    @property
    def connected(self) -> int:
        return sum(1 for l in self.links if l.led.is_connected)

    def label(self) -> str:
        return "BLE %d/%d" % (self.connected, len(self.links))

    def stats(self) -> str:
        return " | ".join(
            "%s: отправлено %d, обрывов %d" % (l.address[-5:], l.sent, l.stalls)
            for l in self.links)


DDP_PORT = 4048
DDP_FLAGS_VER1 = 0x40
DDP_FLAGS_PUSH = 0x01
DDP_TYPE_RGB = 0x0B
DDP_DEST_DISPLAY = 1
DDP_MAX_PIXELS = 480


class DDPSink(Sink):
    def __init__(self, host: str, pixels: int | None = None,
                 port: int = DDP_PORT):
        self.host = host
        self.port = port
        self.pixels = pixels
        self.capacity = pixels or 4096
        self._sock: socket.socket | None = None
        self._seq = 0
        self.sent = 0
        self.failed = 0

    async def open(self) -> bool:
        reachable, detail = await asyncio.get_running_loop().run_in_executor(
            None, self._probe)
        if not reachable:
            log.error("WLED на %s не отвечает: %s", self.host, detail)
            log.error("UDP не подтверждает доставку, поэтому без этой проверки "
                      "всё выглядело бы работающим. Проверьте адрес и то, что "
                      "устройство включено.")
            return False
        log.info("WLED на %s: %s", self.host, detail)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        return True

    def _probe(self, timeout: float = 2.0) -> tuple[bool, str]:
        try:
            with socket.create_connection((self.host, 80), timeout) as c:
                crlf = chr(13) + chr(10)
                request = ("GET /json/info HTTP/1.1" + crlf
                           + "Host: " + self.host + crlf
                           + "Connection: close" + crlf + crlf)
                c.sendall(request.encode("ascii", "ignore"))
                chunks = []
                c.settimeout(timeout)
                while len(b"".join(chunks)) < 4096:
                    part = c.recv(2048)
                    if not part:
                        break
                    chunks.append(part)
            body = b"".join(chunks).decode("utf-8", "replace")
        except OSError as e:
            return False, "%s: %s" % (type(e).__name__, e)
        if '"brand"' not in body and "WLED" not in body:
            return False, "по адресу кто-то есть, но это не похоже на WLED"
        import json as _json
        try:
            info = _json.loads(body[body.index("{"):body.rindex("}") + 1])
            return True, "версия %s, диодов %s" % (
                info.get("ver", "?"), info.get("leds", {}).get("count", "?"))
        except (ValueError, KeyError):
            return True, "отвечает"

    def _packet(self, payload: bytes, offset: int, push: bool) -> bytes:
        self._seq = (self._seq % 15) + 1
        flags = DDP_FLAGS_VER1 | (DDP_FLAGS_PUSH if push else 0)
        return struct.pack("!BBBBIH", flags, self._seq, DDP_TYPE_RGB,
                           DDP_DEST_DISPLAY, offset, len(payload)) + payload

    async def send(self, codes: list[tuple[int, int, int]]) -> None:
        if self._sock is None:
            return
        data = bytes(v for c in codes for v in c)
        try:
            total = len(codes)
            for start in range(0, total, DDP_MAX_PIXELS):
                chunk = data[start * 3:(start + DDP_MAX_PIXELS) * 3]
                last = start + DDP_MAX_PIXELS >= total
                self._sock.sendto(self._packet(chunk, start * 3, last),
                                  (self.host, self.port))
            self.sent += 1
        except OSError as e:
            self.failed += 1
            if self.failed % 50 == 1:
                log.warning("DDP: не отправилось на %s: %s", self.host, e)

    async def close(self, turn_off: bool = True) -> None:
        if self._sock is not None:
            if turn_off and self.pixels:
                await self.send([(0, 0, 0)] * self.pixels)
            self._sock.close()
            self._sock = None

    @property
    def connected(self) -> int:
        return self.capacity if self._sock is not None else 0

    def label(self) -> str:
        return "WLED %s" % self.host

    def stats(self) -> str:
        return "DDP -> %s: кадров %d, ошибок %d" % (self.host, self.sent,
                                                    self.failed)

