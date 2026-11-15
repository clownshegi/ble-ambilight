from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aiohttp
from aiohttp import web
from bleak.exc import BleakError

import ambilight
import bjled
import sync_engine
import webapp
from bjled import (CMD_OFF, CMD_ON, clock_command, effect_command,
                   power_on_command, rgb_command)
from color import DEFAULT_PROFILE_PATH, OutputProfile
from scenes import make_scene

ADDR = "BE:68:E4:00:09:25"
STEP_LIMIT = 60 * max(1.0, OutputProfile.load(DEFAULT_PROFILE_PATH).max_level / 0.5)
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, detail))
        FAILURES.append(name)


class Lamp:
    def __init__(self):
        self.power: bool | None = None
        self.color: tuple[int, int, int] | None = None

    def apply(self, p: bytes) -> None:
        if p == CMD_ON:
            self.power = True
        elif p == CMD_OFF:
            self.power = False
        elif len(p) >= 7 and p[3] == 0x02:
            self.color = (p[4], p[5], p[6])

    @property
    def lit(self) -> bool:
        return self.power is True


class Radio:
    reachable = True
    lamp = Lamp()
    writes: list[tuple[float, bytes]] = []
    reads = 0
    clients: list["FakeClient"] = []
    fail_writes = False

    @classmethod
    def reset(cls, reachable: bool = True) -> None:
        cls.reachable = reachable
        cls.lamp = Lamp()
        cls.writes = []
        cls.reads = 0
        cls.clients = []
        cls.fail_writes = False

    @classmethod
    def current(cls) -> "FakeClient | None":
        live = [c for c in cls.clients if c.is_connected]
        return live[-1] if live else None


class FakeClient:
    def __init__(self, device, disconnected_callback=None, timeout=20.0):
        self._cb = disconnected_callback
        self.is_connected = False
        Radio.clients.append(self)

    async def connect(self):
        await asyncio.sleep(0.01)
        if not Radio.reachable:
            raise BleakError("устройство не найдено")
        self.is_connected = True

    async def disconnect(self):
        was, self.is_connected = self.is_connected, False
        if was and self._cb is not None:
            self._cb(self)

    async def write_gatt_char(self, uuid, payload, response=False):
        if not self.is_connected:
            raise BleakError("нет соединения")
        if Radio.fail_writes:
            raise BleakError("ошибка записи")
        payload = bytes(payload)
        Radio.lamp.apply(payload)
        Radio.writes.append((time.monotonic(), payload))

    async def read_gatt_char(self, uuid, **_kw):
        if not self.is_connected:
            raise BleakError("нет соединения")
        Radio.reads += 1
        return bytearray(b"\x00")

    def drop(self):
        if self.is_connected:
            self.is_connected = False
            if self._cb is not None:
                self._cb(self)


class FakeScanner:
    @staticmethod
    async def find_device_by_address(address, timeout=10.0):
        await asyncio.sleep(0.01)
        return object() if Radio.reachable else None


class FakeSampler(threading.Thread):
    target = (0.02, 0.05, 0.40)
    producing = True
    hang_first = False
    instances: list["FakeSampler"] = []

    def __init__(self, args, stop):
        super().__init__(daemon=True)
        self.args, self.stop, self.mode = args, stop, args.mode
        self.targets = [(0.0, 0.0, 0.0)]
        self.frames = 0
        self.cut_seq = 0
        self.last_diff = 0.0
        self.backend = "dxcam"
        self.ready = threading.Event()
        FakeSampler.instances.append(self)

    def run(self):
        while not self.stop.is_set():
            hung = FakeSampler.hang_first and self is FakeSampler.instances[0]
            if FakeSampler.producing and not hung:
                self.targets = [FakeSampler.target]
                self.frames += 1
                self.ready.set()
            self.stop.wait(0.03)


bjled.BleakClient = FakeClient
bjled.BleakScanner = FakeScanner
bjled.POST_CONNECT_SETTLE = 0.01
ambilight.Sampler = FakeSampler


def fast_controller() -> webapp.Controller:
    c = webapp.Controller(ADDR)
    p = c._player
    p.min_retry = p._retry_delay = 0.1
    p.max_retry = 0.3
    p.connect_timeout = 2.0
    return c


async def wait_for(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def writes_since(t: float) -> list[bytes]:
    return [p for ts, p in Radio.writes if ts >= t]


def reset_world(reachable: bool = True) -> None:
    Radio.reset(reachable)
    FakeSampler.target = (0.02, 0.05, 0.40)
    FakeSampler.producing = True
    FakeSampler.hang_first = False
    FakeSampler.instances = []


async def t_sync_not_killed_by_keepalive():
    reset_world()
    c = fast_controller()
    c._player.keepalive = 0.3
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_sync("game")
    t = time.monotonic()
    await asyncio.sleep(1.5)
    sent = writes_since(t)
    check("синхронизация: за 5 интервалов keepalive ни одного ВЫКЛ",
          CMD_OFF not in sent, "команды: %s" % [p.hex() for p in sent][:8])
    check("синхронизация: лента горит", Radio.lamp.lit)
    check("keepalive повторяет цвет, а не шлёт что-то своё",
          sum(1 for p in sent if p[3:4] == b"\x02") >= 3,
          "цветовых команд %d" % sum(1 for p in sent if p[3:4] == b"\x02"))
    await c.shutdown()


async def t_modes_after_off():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.turn_off()
    check("«Выключить» гасит", Radio.lamp.power is False)
    await c.start_event("камин")
    await asyncio.sleep(0.3)
    check("«Выключить» -> «Камин»: лента горит", Radio.lamp.lit)
    await c.turn_off()
    await c.start_sync("film")
    await asyncio.sleep(0.3)
    check("«Выключить» -> «Кино»: лента горит", Radio.lamp.lit)
    await c.shutdown()


async def t_reconnect_keeps_off():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.turn_off()
    n_clients = len(Radio.clients)
    Radio.current().drop()
    await wait_for(lambda: len(Radio.clients) > n_clients and c._player.link_up)
    await asyncio.sleep(0.2)
    check("переподключение произошло", len(Radio.clients) > n_clients)
    check("после переподключения выключенная лента осталась выключенной",
          Radio.lamp.power is False)
    await c.shutdown()


async def t_startup_does_not_touch_lamp():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await asyncio.sleep(0.3)
    check("запуск программы не шлёт ленте ни ВКЛ, ни ВЫКЛ",
          CMD_ON not in [p for _, p in Radio.writes]
          and CMD_OFF not in [p for _, p in Radio.writes],
          "команды: %s" % [p.hex() for _, p in Radio.writes])
    await c.shutdown()


async def t_keepalive_without_commands_reads():
    reset_world()
    c = fast_controller()
    c._player.keepalive = 0.2
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await asyncio.sleep(0.8)
    check("простой без команд: связь держится GATT-чтением, записей нет",
          Radio.reads >= 2 and not Radio.writes,
          "чтений %d, записей %d" % (Radio.reads, len(Radio.writes)))
    await c.shutdown()


async def t_unreachable_at_start():
    reset_world(reachable=False)
    c = fast_controller()
    t0 = time.monotonic()
    await c.warm_up()
    check("старт программы не ждёт ленту", time.monotonic() - t0 < 0.2)
    await asyncio.sleep(0.3)
    st = c.status()
    check("статус честно говорит, что связи нет",
          st["connected"] is False and "нет связи" in st["note"], st["note"])
    t0 = time.monotonic()
    await c.start_event("камин")
    check("нажатие без ленты не висит", time.monotonic() - t0 < 0.5,
          "%.2f с" % (time.monotonic() - t0))
    Radio.reachable = True
    ok = await wait_for(lambda: c._player.link_up and Radio.lamp.lit, 3.0)
    check("лента появилась - программа подключилась сама и включила режим", ok)
    await asyncio.sleep(0.3)
    col = Radio.lamp.color or (0, 0, 0)
    check("и это именно «камин» (тёплый: красный > синего)",
          col[0] > col[2], "цвет %s" % (col,))
    await c.shutdown()


async def t_off_while_unreachable():
    reset_world(reachable=False)
    c = fast_controller()
    await c.warm_up()
    await c.turn_off()
    st = c.status()
    check("«Выключить» без связи: статус «выключено» + пометка про связь",
          st["kind"] == "off" and "нет связи" in st["note"], str(st))
    Radio.reachable = True
    await wait_for(lambda: c._player.link_up, 3.0)
    await asyncio.sleep(0.2)
    check("связь появилась - выключение дошло", Radio.lamp.power is False)
    await c.shutdown()


async def t_sync_resends_after_reconnect():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_sync("game")
    await asyncio.sleep(0.5)
    blue = Radio.lamp.color
    Radio.reachable = False
    Radio.current().drop()
    FakeSampler.target = (0.60, 0.05, 0.02)
    await asyncio.sleep(0.5)
    Radio.reachable = True
    await wait_for(lambda: c._player.link_up, 3.0)
    await asyncio.sleep(0.4)
    col = Radio.lamp.color or (0, 0, 0)
    check("после восстановления связи сразу актуальный цвет экрана",
          col != blue and col[0] > col[2], "было %s, стало %s" % (blue, col))
    check("и лента включена", Radio.lamp.lit)
    await c.shutdown()


async def t_switch_starts_from_current_color():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_event("камин")
    await asyncio.sleep(0.8)
    await c.start_sync("game")
    await asyncio.sleep(0.8)
    blue = Radio.lamp.color
    t = time.monotonic()
    await c.start_event("свеча")
    await asyncio.sleep(0.3)
    frames = [(p[4], p[5], p[6]) for p in writes_since(t) if p[3:4] == b"\x02"]
    first = frames[0] if frames else (0, 0, 0)
    dist = lambda a, b: max(abs(x - y) for x, y in zip(a, b))
    check("первый кадр «свечи» ближе к тому, что горело (синему), чем к "
          "красному", dist(first, blue) < dist(first, (220, 0, 10)),
          "синий %s, первый кадр %s" % (blue, first))
    check("переход плавный: соседние кадры отличаются не больше чем на 60",
          all(dist(a, b) <= STEP_LIMIT for a, b in zip([blue] + frames, frames)),
          "кадры %s" % frames[:6])
    await c.shutdown()


async def t_stop_and_labels():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    check("до любых команд - «ожидание», а не «выключено»",
          c.status()["label"].startswith("ожидание"), c.status()["label"])
    await c.start_event("камин")
    await c.stop()
    st = c.status()
    check("«Стоп» после режима: честное «держит последний цвет»",
          st["kind"] == "stopped" and "держит" in st["label"], st["label"])
    check("«Стоп» не гасит ленту", Radio.lamp.lit)
    await c.turn_off()
    await c.stop()
    check("«Стоп» после «Выключить» остаётся «выключено»",
          c.status()["kind"] == "off")
    await c.shutdown()


async def t_brightness_single_source():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await c.start_sync("game", brightness=0.4)
    check("яркость из запуска пресета видна в статусе",
          abs(c.status()["brightness"] - 0.4) < 1e-9, str(c.status()["brightness"]))
    c.set_brightness(0.7)
    check("живая яркость доходит до синхронизации",
          abs(c._sync.brightness - 0.7) < 1e-9)
    c.set_mode("dominant")
    check("режим цвета меняется на лету", c._sync.mode == "dominant")
    c.set_mode("")
    check("«по пресету» возвращает режим пресета на лету",
          c._sync.mode == ambilight.PRESETS["game"]["mode"]
          and c.status()["mode"] == "")
    c.set_gate("audio", False)
    check("звук переключается на лету", c._sync.audio_on is False)
    await c.shutdown()


async def t_screen_lock_recovers():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await c.start_sync("game")
    s = c._sync
    s.stall_seconds, s.retry_max, s.ready_timeout = 0.3, 0.3, 0.1
    FakeSampler.producing = False
    await asyncio.sleep(3.0)
    during = c.status()
    FakeSampler.producing = True
    ok = await wait_for(lambda: c._sync is s and s.problem is None, 3.0)
    alive = s._task is not None and not s._task.done()
    check("во время блокировки статус объясняет, что происходит",
          "захват экрана" in during["note"], during["note"])
    check("после разблокировки синхронизация вернулась сама",
          ok and alive and c.status()["kind"] == "sync",
          "problem=%s alive=%s kind=%s" % (s.problem, alive, c.status()["kind"]))
    check("перезапусков было много - ограничения «5 за сессию» больше нет",
          len(FakeSampler.instances) > 6, "потоков %d" % len(FakeSampler.instances))
    await c.shutdown()


async def t_stuck_sampler_told_to_stop():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await c.start_sync("game")
    c._sync.stall_seconds = 0.3
    FakeSampler.hang_first = True
    await wait_for(lambda: len(FakeSampler.instances) >= 2, 3.0)
    first = FakeSampler.instances[0]
    check("зависшему потоку при перезапуске велено остановиться",
          first.stop.is_set())
    FakeSampler.hang_first = False
    await asyncio.sleep(0.3)
    check("«отвиснув», он вышел, а не крутится параллельно новому",
          not first.is_alive())
    await c.shutdown()


async def t_stop_does_not_block_loop():
    reset_world()

    class SlowExit(FakeSampler):
        def run(self):
            super().run()
            time.sleep(1.0)

    ambilight.Sampler = SlowExit
    try:
        c = fast_controller()
        await c.warm_up()
        await c.start_sync("game")
        gaps = []

        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        t = asyncio.ensure_future(ticker())
        await asyncio.sleep(0.05)
        await c.stop()
        await asyncio.sleep(0.05)
        t.cancel()
        check("остановка синхронизации не замораживает фон",
              max(gaps) < 0.3, "фон стоял %.2f с" % max(gaps))
        await c.shutdown()
    finally:
        ambilight.Sampler = FakeSampler


async def t_bjled_details():
    reset_world()
    led = bjled.BJLed(ADDR)
    await led.connect()
    client = led._client
    led._on_disconnect(FakeClient(None))
    check("запоздалый колбэк чужого клиента не обнуляет живое соединение",
          led._client is client and led.is_connected)
    await led.set_rgb(10, 20, 30)
    await led.keepalive()
    check("keepalive повторяет последнюю команду",
          Radio.writes[-1][1] == rgb_command(10, 20, 30))
    await led.turn_off()
    await led.keepalive()
    check("после выключения keepalive повторяет ВЫКЛ, а не включает",
          Radio.writes[-1][1] == CMD_OFF and Radio.lamp.power is False)
    Radio.fail_writes = True
    ok = await led.send(CMD_ON)
    await asyncio.sleep(0.05)
    check("ошибка записи: клиент не просто забыт, а закрыт",
          not ok and not client.is_connected and led._client is None)
    Radio.fail_writes = False

    reset_world()
    led = bjled.BJLed(ADDR)
    await asyncio.gather(led.connect(), led.connect(), led.connect())
    check("три одновременных подключения - один клиент",
          len(Radio.clients) == 1, "клиентов %d" % len(Radio.clients))
    await led.set_rgb(1, 2, 3)
    check("первая команда после подключения уходит дважды (прошивка глотает)",
          [p for _, p in Radio.writes].count(rgb_command(1, 2, 3)) == 2)
    await led.disconnect()


def t_protocol_frames():
    import schedule_led
    cases = [
        ("включить", CMD_ON, "6996020101"),
        ("выключить", CMD_OFF, "6996020100"),
        ("цвет", rgb_command(0x12, 0x34, 0x56), "69960502123456ff"),
        ("цвет, короткая форма", rgb_command(1, 2, 3, None), "69960502010203"),
        ("включить цветом", power_on_command((1, 2, 3)), "699606010101020 3ff"),
        ("эффект A", effect_command(0x0B, 5), "699603030b05"),
        ("эффект B", effect_command(2, 5, bank=1), "699603040205"),
        ("часы", clock_command(0x6554A5BD), "699606076554a5bd00"),
        ("таймер", schedule_led.timer_command(23, 30, 0xFF), "6996060501ff171e00"),
    ]
    for name, got, want in cases:
        want = bytes.fromhex(want.replace(" ", ""))
        check("кадр: %s" % name, got == want, "%s != %s" % (got.hex(), want.hex()))


def t_profile_found_from_any_folder():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            p = OutputProfile.load(DEFAULT_PROFILE_PATH)
        finally:
            os.chdir(cwd)
    ref = OutputProfile.load(os.path.join(HERE, "profile.json"))
    check("калибровка находится из любой рабочей папки (как у Lamp.exe)",
          os.path.isabs(DEFAULT_PROFILE_PATH) and p == ref,
          "%s: %s" % (DEFAULT_PROFILE_PATH, p))


def t_build_args_bad_mode():
    try:
        sync_engine.build_args("game", mode="bogus")
    except ValueError:
        check("неверный режим цвета - ValueError, а не SystemExit", True)
    except SystemExit:
        check("неверный режим цвета - ValueError, а не SystemExit", False)
    else:
        check("неверный режим цвета отклонён", False)


def t_single_instance():
    import desktop
    name = "Local\\LampSuite.Test.%d" % os.getpid()
    first = desktop.claim_single_instance(name)
    second = desktop.claim_single_instance(name)
    check("второй экземпляр программы распознаётся", first and not second)


async def t_http_api():
    reset_world()
    c = fast_controller()
    app = webapp.build_app(c)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = "http://127.0.0.1:%d" % port
    own = {"Content-Type": "application/json", "Origin": base}
    async with aiohttp.ClientSession() as s:
        async def post(path, body, headers=own):
            data = body if isinstance(body, str) else json.dumps(body)
            async with s.post(base + path, data=data, headers=headers) as r:
                return r.status

        r = await s.get(base + "/api/status")
        st = await r.json()
        check("GET /api/status отдаёт подпись статуса", "label" in st)
        check("неверный режим цвета -> 400 и фон жив",
              await post("/api/sync", {"preset": "game", "mode": "bogus"}) == 400
              and (await s.get(base + "/api/status")).status == 200)
        check("запрос с чужого сайта (Origin) -> 403",
              await post("/api/off", {}, {"Content-Type": "application/json",
                                          "Origin": "http://evil.example"}) == 403)
        check("text/plain (обход CORS) -> 415",
              await post("/api/off", "{}", {"Content-Type": "text/plain"}) == 415)
        check("чужое имя хоста (DNS rebinding) -> 403",
              await post("/api/off", {}, {"Content-Type": "application/json",
                                          "Host": "evil.example:%d" % port}) == 403)
        check("тело-массив -> 400", await post("/api/sync", "[1, 2]") == 400)
        check("яркость строкой -> 400",
              await post("/api/brightness", {"value": "abc"}) == 400)
        check("яркость NaN -> 400",
              await post("/api/brightness", '{"value": NaN}') == 400)
        check("неизвестный переключатель -> 400",
              await post("/api/gate", {"name": "x", "on": True}) == 400)
        check("своя страница: «Камин» -> 200",
              await post("/api/event", {"name": "камин", "brightness": 0.5}) == 200)
        check("своя страница без Origin (curl) -> 200",
              await post("/api/stop", {}, {"Content-Type": "application/json"}) == 200)
    await runner.cleanup()


def colours_since(t: float) -> list[tuple[int, int, int]]:
    return [(p[4], p[5], p[6]) for p in writes_since(t) if p[3:4] == b"\x02"]


def cdist(a, b) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


async def t_power_button_resumes_last_mode():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.turn_on()
    check("«Включить» без прошлого режима: горит, «включено - режим не выбран»",
          Radio.lamp.lit and c.status()["kind"] == "on", c.status()["label"])
    await c.start_event("камин")
    await asyncio.sleep(0.3)
    await c.turn_off()
    check("«Выключить» гасит", Radio.lamp.power is False)
    await c.turn_on()
    await asyncio.sleep(0.3)
    st = c.status()
    check("«Включить» продолжает «Камин»",
          Radio.lamp.lit and st["kind"] == "event" and st["name"] == "камин",
          str(st["label"]))
    await c.start_sync("film", flags=["--flash-gate"])
    c.set_gate("idle_life", True)
    await c.turn_off()
    await c.turn_on()
    st = c.status()
    check("«Включить» продолжает «Кино» с галочками, включёнными на лету",
          st["kind"] == "sync" and st["name"] == "film"
          and st.get("flash_gate") and st.get("idle_life"), str(st))
    await c.shutdown()


async def t_off_fades_out():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_event("камин")
    await asyncio.sleep(0.8)
    t = time.monotonic()
    await c.turn_off()
    sent = writes_since(t)
    cols = [(p[4], p[5], p[6]) for p in sent if p[3:4] == b"\x02"]
    peaks = [max(x) for x in cols]
    check("«Выключить» гаснет плавно: несколько шагов вниз, потом ВЫКЛ",
          len(cols) >= 3 and peaks == sorted(peaks, reverse=True)
          and sent and sent[-1] == CMD_OFF, "шаги %s" % peaks)
    check("гаснет не в ноль (ВКЛ с пульта не зажжёт ленту чёрным)",
          cols and 0 < max(cols[-1]) <= 13, "последний цвет %s" % (cols[-1:],))
    await c.shutdown()


async def t_crossfade_event_to_screen():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_event("камин")
    await asyncio.sleep(0.8)
    red = Radio.lamp.color
    t = time.monotonic()
    await c.start_sync("game")
    await asyncio.sleep(0.8)
    cols = colours_since(t)
    blue = Radio.lamp.color
    steps = [cdist(a, b) for a, b in zip([red] + cols, cols)]
    check("праздник -> экран: первый цвет ближе к красному, чем к синему",
          cols and cdist(cols[0], red) < cdist(cols[0], blue),
          "красный %s, первый %s, синий %s" % (red, cols[:1], blue))
    check("праздник -> экран: без скачков (шаг не больше 60)",
          steps and max(steps) <= STEP_LIMIT, "шаги %s" % steps[:10])
    check("и приходит к цвету экрана", blue[2] > blue[0], str(blue))
    await c.shutdown()


async def t_festive_brightness_scale():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    prof = c._player.profile
    top = round(255 * prof.max_level)
    await c.start_event("свет", brightness=1.0)
    await asyncio.sleep(3.0)
    full = max(Radio.lamp.color or (0, 0, 0))
    check("праздник на 100%% не ярче откалиброванного максимума (%d)" % top,
          0.8 * top <= full <= top + 1, "пик %d" % full)
    c.set_brightness(0.05)
    await asyncio.sleep(1.5)
    low = max(Radio.lamp.color or (999, 0, 0))
    check("праздник на 5%%: около порога ШИМ (%d), а не четверть яркости"
          % prof.floor, low <= prof.floor + 1, "пик %d" % low)
    await c.shutdown()


async def t_click_reconnects_now():
    reset_world(reachable=False)
    c = fast_controller()
    p = c._player
    p.max_retry = 30.0
    await c.warm_up()
    await asyncio.sleep(0.3)
    p._retry_at = time.monotonic() + 30.0
    p._retry_delay = 30.0
    Radio.reachable = True
    t0 = time.monotonic()
    await c.start_event("свеча")
    ok = await wait_for(lambda: p.link_up and Radio.lamp.lit, 2.0)
    check("нажатие ищет ленту сразу, а не через накопленные 30 с",
          ok, "%.1f с" % (time.monotonic() - t0))
    await c.shutdown()


async def t_sleep_and_lock():
    reset_world()
    c = fast_controller()
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)

    await c.start_sync("film")
    await asyncio.sleep(0.3)
    await c.on_system("lock")
    st = c.status()
    check("блокировка во время экрана: лента погашена, статус объясняет",
          Radio.lamp.power is False and st["kind"] == "paused"
          and "заблокирован" in st["label"], st["label"])
    await c.on_system("unlock")
    await asyncio.sleep(0.3)
    check("вход: подсветка экрана вернулась сама",
          Radio.lamp.lit and c.status()["kind"] == "sync"
          and c.status()["name"] == "film", c.status()["label"])

    await c.start_event("камин")
    await c.on_system("lock")
    check("блокировка праздничный режим не трогает",
          Radio.lamp.lit and c.status()["kind"] == "event")
    await c.on_system("unlock")
    await c.on_system("suspend")
    check("сон ПК: лента погашена", Radio.lamp.power is False
          and "спит" in c.status()["label"], c.status()["label"])
    Radio.current().drop()
    await c.on_system("resume")
    ok = await wait_for(lambda: Radio.lamp.lit and c.status()["kind"] == "event",
                        3.0)
    check("пробуждение: праздник вернулся сам (и связь тоже)", ok,
          c.status()["label"])

    await c.start_sync("game")
    await c.on_system("lock")
    await c.on_system("suspend")
    await c.on_system("resume")
    check("проснулись, но экран ещё заблокирован - ждём входа",
          Radio.lamp.power is False and "заблокирован" in c.status()["label"],
          c.status()["label"])
    await c.on_system("unlock")
    await asyncio.sleep(0.3)
    check("а после входа - экран", Radio.lamp.lit
          and c.status()["kind"] == "sync", c.status()["label"])

    await c.on_system("lock")
    await c.start_event("свеча")
    await c.on_system("unlock")
    check("команда во время блокировки главнее возврата после неё",
          c.status()["kind"] == "event" and c.status()["name"] == "свеча",
          c.status()["label"])

    await c.turn_off()
    await c.on_system("suspend")
    await c.on_system("resume")
    await asyncio.sleep(0.2)
    check("выключенную ленту пробуждение не зажигает",
          Radio.lamp.power is False and c.status()["kind"] == "off")

    c.off_on_sleep = False
    await c.start_event("камин")
    await c.on_system("suspend")
    check("off_when_pc_sleeps=false: сон ленту не трогает", Radio.lamp.lit)
    await c.shutdown()


def t_winevents_plumbing():
    import ctypes
    import winevents
    got: list[str] = []
    ev = winevents.SystemEvents(got.append, name="LampSuiteTest%d" % os.getpid())
    ev.start()
    ev.ready.wait(3.0)
    check("скрытое окно для событий Windows создалось",
          ev.hwnd and ev.error is None, str(ev.error))
    send = ctypes.windll.user32.SendMessageW
    send.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                     ctypes.c_ssize_t)
    for msg, wp in ((winevents.WM_WTSSESSION_CHANGE, winevents.WTS_SESSION_LOCK),
                    (winevents.WM_WTSSESSION_CHANGE, winevents.WTS_SESSION_UNLOCK),
                    (winevents.WM_POWERBROADCAST, winevents.PBT_APMSUSPEND),
                    (winevents.WM_POWERBROADCAST, winevents.PBT_APMRESUMEAUTOMATIC),
                    (winevents.WM_ENDSESSION, 1)):
        send(ev.hwnd, msg, wp, 0)
    check("сообщения Windows превращаются в события",
          got == ["lock", "unlock", "suspend", "resume", "shutdown"], str(got))
    ev.close()
    ev.join(2.0)
    check("окно событий закрывается", not ev.is_alive())


async def t_sync_failure_restarts_itself():
    reset_world()
    c = fast_controller()
    c.restart_base = 0.1
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    real_run = sync_engine.ScreenSync._run
    calls = {"n": 0}

    async def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.1)
            raise RuntimeError("проверка")
        await real_run(self)

    sync_engine.ScreenSync._run = flaky
    try:
        await c.start_sync("game")
        first = c._sync
        first_sampler = first.sampler
        await asyncio.sleep(0.15)
        check("упавшая синхронизация останавливает свой поток захвата",
              first_sampler.stop.is_set())
        ok = await wait_for(lambda: c._sync is not first and c._sync is not None
                            and c.status()["kind"] == "sync", 3.0)
        check("и перезапускается сама", ok, c.status()["label"])
    finally:
        sync_engine.ScreenSync._run = real_run
    await c.shutdown()


async def t_audio_restarts_on_device_change():
    reset_world()

    class DyingMonitor(threading.Thread):
        made = 0

        def __init__(self):
            super().__init__(daemon=True)
            DyingMonitor.made += 1
            self.ready, self.level, self.error = threading.Event(), 0.5, None
            self.device, self.rate = "тест", 48000

        def run(self):
            self.ready.set()
            time.sleep(0.2)
            self.error = "сменилось устройство вывода"

        def stop(self):
            pass

    real = sync_engine.LoudnessMonitor
    sync_engine.LoudnessMonitor = DyingMonitor
    try:
        c = fast_controller()
        await c.warm_up()
        await c.start_sync("film", flags=["--audio"])
        ok = await wait_for(lambda: DyingMonitor.made >= 2, 3.0)
        check("монитор звука поднимается заново после смены устройства", ok,
              "мониторов %d" % DyingMonitor.made)
        await c.shutdown()
    finally:
        sync_engine.LoudnessMonitor = real


def t_audio_device_watch_real():
    import audio
    real = audio.default_output_id
    ids = iter(["A"] + ["B"] * 100)
    audio.default_output_id = lambda: (lambda: next(ids))
    old_check = audio.DEVICE_CHECK_S
    audio.DEVICE_CHECK_S = 0.1
    try:
        m = audio.LoudnessMonitor()
        m.start()
        m.ready.wait(5.0)
        if m.error:
            check("монитор звука (пропуск: нет устройства - %s)" % m.error, True)
            m.stop()
            return
        m.join(3.0)
        check("смена устройства вывода: монитор выходит с понятной причиной",
              not m.is_alive() and m.error == "сменилось устройство вывода",
              str(m.error))
    finally:
        audio.default_output_id = real
        audio.DEVICE_CHECK_S = old_check


def t_pipeline_layers():
    from color import OutputProfile
    from pipeline import ScenePipeline
    args = sync_engine.build_args("game", 1.0, None,
                                  ["--flash-gate", "--dark-gate"])
    pipe = ScenePipeline(args, 1, OutputProfile())
    dim, bright, black = (0.02, 0.02, 0.02), (0.9, 0.9, 0.9), (0, 0, 0)
    now = 0.0
    for _ in range(20):
        now += 0.05
        pipe.step([dim], now, 0.05)
    codes, _cuts, fired = pipe.step([bright], now + 0.05, 0.05)
    held, _c, _f = pipe.step([dim], now + 0.10, 0.05)
    check("вспышка: срабатывает и держит пик, хотя экран уже погас",
          fired and held[0] == codes[0], "%s %s" % (codes, held))
    now += 1.0
    for _ in range(30):
        now += 0.05
        out, _c, _f = pipe.step([black], now, 0.05)
    check("тёмный порог гасит ленту", out[0] == (0, 0, 0), str(out))
    pipe.dark_gate_on = False
    out, _c, _f = pipe.step([dim], now + 0.05, 0.05)
    check("гейты выключаются на лету", out[0] != (0, 0, 0))
    quiet = pipe.step([dim], now + 0.1, 0.05, audio_level=0.0)[0][0]
    loud = pipe.step([dim], now + 0.15, 0.05, audio_level=1.0)[0][0]
    check("звук множит яркость", max(loud) > max(quiet), "%s %s" % (quiet, loud))


async def t_ambilight_cli_smoke():
    reset_world()

    class CliSampler(FakeSampler):
        def __init__(self, args, stop):
            super().__init__(args, stop)
            self.zones = ambilight.make_layout(args.layout)
            self.targets = [(0.0, 0.0, 0.0)] * len(self.zones)
            self.errors, self.last_ms = 0, 1.0

        def run(self):
            n = 0
            while not self.stop.is_set():
                self.targets = [FakeSampler.target] * len(self.zones)
                self.frames += 1
                self.last_diff = 20.0 if n % 10 == 0 else 1.0
                self.ready.set()
                n += 1
                if n > 40:
                    self.stop.set()
                self.stop.wait(0.02)

    ambilight.Sampler = CliSampler
    try:
        p = ambilight.build_parser()
        args = p.parse_args(["--dry-run", "--quiet", "--layout", "lr",
                             "--preset", "film", "--flash-gate", "--idle-life",
                             "--auto-contrast", "--adaptive-tau",
                             "--dominant-hold", "--mode", "dominant"])
        ambilight.apply_preset(args)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = await asyncio.wait_for(ambilight.run(args), 10.0)
        check("ambilight.py --dry-run со всеми слоями отрабатывает", rc == 0)
    finally:
        ambilight.Sampler = FakeSampler


def t_window_fits_content():
    import tkinter as tk
    import desktop
    cfg = dict(webapp.DEFAULT_CONFIG, problem=None, port=1)
    bridge = desktop.AsyncBridge(cfg)
    root = tk.Tk()
    try:
        app = desktop.App(root, bridge, "lamp.log")
        root.update()
        ok1 = root.winfo_height() >= root.winfo_reqheight()
        app._toggle_more()
        root.update()
        ok2 = root.winfo_height() >= root.winfo_reqheight()
        bridge.controller.config_problem = "очень длинная пометка " * 12
        app.refresh_status()
        root.update()
        ok3 = root.winfo_height() >= root.winfo_reqheight()
        check("окно вмещает всё содержимое", ok1,
              "%d < %d" % (root.winfo_height(), root.winfo_reqheight()))
        check("и после «Дополнительно»", ok2)
        check("и при длинной пометке в статусе", ok3)
    finally:
        root.destroy()
        bridge.loop.close()


def t_host_and_config():
    me = __import__("socket").gethostname().lower()
    check("имя ПК в домашней сети (.local) пускается",
          webapp._host_allowed(me + ".local:8099"))
    check("«<имя-ПК>.чужой-домен» не пускается (DNS rebinding)",
          not webapp._host_allowed(me + ".evil.example:8099"))
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        path = Path(tmp) / "config.json"
        path.write_text('{"address": "BE:68:E4:00:09", "port": 8099}',
                        encoding="utf-8")
        cfg = webapp.load_config(path)
        check("неверный MAC: берётся рабочий, и это видно в статусе",
              cfg["address"] == webapp.DEFAULT_CONFIG["address"]
              and "MAC" in (cfg["problem"] or ""), str(cfg))
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("новые настройки дописаны в файл, правка человека сохранена",
              "off_when_pc_sleeps" in saved
              and saved["address"] == "BE:68:E4:00:09", str(saved))
        path.write_text("[1, 2]", encoding="utf-8")
        webapp.load_config(path)
        check("повреждённый config.json не перезаписывается",
              path.read_text(encoding="utf-8") == "[1, 2]")


async def t_http_power_and_page():
    reset_world()
    c = fast_controller()
    app = webapp.build_app(c)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = "http://127.0.0.1:%d" % port
    hdr = {"Content-Type": "application/json", "Origin": base}
    async with aiohttp.ClientSession() as s:
        page = await (await s.get(base + "/")).text()
        check("страница собрана из общих списков (флаги, «Свет», сцены)",
              "Адаптивная плавность" in page and "Камин" in page
              and "Свет" in page and "/*META*/" not in page)
        r = await s.get(base + "/icon-192.png")
        body = await r.read()
        check("иконка для домашнего экрана телефона",
              r.status == 200 and body[:4] == b"\x89PNG")
        r = await s.get(base + "/manifest.webmanifest")
        check("манифест приложения", r.status == 200)
        await wait_for(lambda: c._player.link_up)
        r = await s.post(base + "/api/on", data="{}", headers=hdr)
        st = await r.json()
        check("POST /api/on включает", r.status == 200 and st["power"] is True)
        await s.post(base + "/api/event", data='{"name": "камин"}', headers=hdr)
        await asyncio.sleep(0.8)
        st = await (await s.get(base + "/api/status")).json()
        check("в статусе есть свет ленты для интерфейса",
              st["light"] is not None and len(st["light"]["rgb"]) == 3, str(st))
    await runner.cleanup()


def _run_scene(name: str, seconds: float, seed: int = 1, fps: float = 15.0):
    from events import ScenePlayer
    prof = OutputProfile.load(DEFAULT_PROFILE_PATH)
    sp = ScenePlayer(make_scene(name, seed=seed), prof)
    dt, frames, sent = 1.0 / fps, [], []
    for i in range(int(seconds * fps)):
        c = sp.frame(i * dt, dt, 1.0)
        if c is not None:
            sp.last_sent = c
            sent.append(c)
        frames.append(sp.last_sent)
    return sp.scene, frames, sent


def t_scenes_alive():
    fire, frames, _sent = _run_scene("камин", 120.0)
    lit = [f for f in frames[30:] if f and max(f) > 0]
    bright = [f for f in lit if max(f) >= sorted(max(x) for x in lit)[len(lit) * 3 // 4]]
    dim = [f for f in lit if max(f) <= sorted(max(x) for x in lit)[len(lit) // 4]]
    prof = OutputProfile.load(DEFAULT_PROFILE_PATH)

    def warm(fs):
        light = [prof.to_light(f) for f in fs]
        return sum(g / max(1e-6, r) for r, g, _b in light) / len(light)
    check("камин: тусклее значит краснее (как пламя)", warm(dim) < warm(bright),
          "g/r тусклых %.3f, ярких %.3f" % (warm(dim), warm(bright)))
    check("камин: всполохи случаются", fire.flare_count >= 2,
          "всполохов %d за 2 мин" % fire.flare_count)

    for name in ("камин", "свеча", "гроза"):
        _s, _f, sent = _run_scene(name, 120.0)
        check("%s: канал не перегружен (<= 12 команд/с в среднем)" % name,
              len(sent) / 120.0 <= 12.0, "%.1f/с" % (len(sent) / 120.0))

    storm = make_scene("гроза", seed=5)
    t, lit_since, strokes = 0.0, None, []
    while t < 200.0:
        f = storm.flash(t)
        if f > 0.1 and lit_since is None:
            lit_since = t
        elif f <= 0.1 and lit_since is not None:
            strokes.append(t - lit_since)
            lit_since = None
        t += 0.005
    check("гроза: молнии есть, и каждый удар не короче 100 мс (иначе "
          "пропадёт в задержке BLE)", strokes and min(strokes) >= 0.095,
          "ударов %d, кратчайший %.3f с" % (len(strokes), min(strokes or [0])))

    sleep = make_scene("засыпание", seed=1)
    pts = [sleep.at(u / 100.0) for u in range(101)]
    check("засыпание: только теплеет и темнеет",
          all(b[0] <= a[0] + 1e-6 and b[1] <= a[1] + 1e-9
              for a, b in zip(pts, pts[1:])))


def t_kelvin_and_sun():
    from scenes import evening_warmth, kelvin, sun_times
    check("6500 K - нейтральный белый", kelvin(6500) == (1.0, 1.0, 1.0))
    r, g, b = kelvin(2000)
    check("2000 K - тёплый: красный > зелёный > синий", r > g > b, str((r, g, b)))
    rise, set_ = sun_times(55.7558, 37.6173, 172)
    check("закат в Москве 21 июня ~21:17 по Москве (18:17 UTC)",
          abs(set_ - 18.29) < 0.2, "%.2f ч UTC" % set_)
    check("днём не теплеет, поздно вечером - полностью",
          evening_warmth(13.0, 3, 172) == 0.0
          and evening_warmth(23.5, 3, 172) == 1.0)


def t_light_scene():
    import types
    from scenes import Light
    prof = OutputProfile.load(DEFAULT_PROFILE_PATH)

    def at(hour: int, warm: bool = True):
        scene = make_scene("свет", seed=1, warm_evening=warm)
        scene.clock = lambda: types.SimpleNamespace(
            tm_hour=hour, tm_min=0, tm_gmtoff=3 * 3600, tm_yday=267)
        frames = [scene.encode(scene.sample(t * 0.5), prof, 1.0)
                  for t in range(40)]
        return frames

    day, night = at(13), at(23)
    check("«Свет» не мерцает: 20 с один и тот же цвет",
          len(set(day)) == 1 and len(set(night)) == 1, str(set(day)))
    g_day, g_night = day[0][1] / day[0][0], night[0][1] / night[0][0]
    check("днём белый, поздно вечером тёплый",
          g_night < g_day - 0.1, "%s -> %s" % (day[0], night[0]))
    check("warm_evening=false: вечером тоже дневной белый",
          at(23, warm=False)[0] == day[0])
    check("«Свет» - первая сцена (широкая плитка над эффектами)",
          list(webapp.UI_META["events"])[0][0] == "свет" and Light.fade_in <= 1.0)


async def t_sleep_scene_turns_off():
    reset_world()
    c = fast_controller()
    c._player.sleep_minutes = 0.03
    await c.warm_up()
    await wait_for(lambda: c._player.link_up)
    await c.start_event("камин")
    await c.start_event("засыпание")
    await asyncio.sleep(0.3)
    check("засыпание: в статусе сколько осталось",
          "ещё" in c.status()["label"], c.status()["label"])
    ok = await wait_for(lambda: c.status()["kind"] == "off", 5.0)
    check("засыпание кончилось - лента выключилась сама",
          ok and Radio.lamp.power is False, c.status()["label"])
    await c.turn_on()
    await asyncio.sleep(0.2)
    check("«Включить» утром - прошлый режим, а не засыпание заново",
          c.status()["name"] == "камин", str(c.status()["name"]))
    await c.shutdown()


async def t_idle_dim():
    reset_world()
    idle = {"s": 0.0}
    real = sync_engine.input_idle_seconds
    sync_engine.input_idle_seconds = lambda: idle["s"]
    try:
        c = webapp.Controller(ADDR, dim_when_idle_min=0.01)
        await c.warm_up()
        await wait_for(lambda: c._player.link_up)
        await c.start_sync("game")
        await asyncio.sleep(1.0)
        full = max(Radio.lamp.color)
        idle["s"] = 9999.0
        ok = await wait_for(lambda: "никого нет" in c.status()["note"], 4.0)
        await asyncio.sleep(6.0)
        dimmed = max(Radio.lamp.color)
        check("никого нет (ни ввода, ни движения на экране) - приглушает",
              ok and dimmed < 0.6 * full, "%d -> %d, %s"
              % (full, dimmed, c.status()["note"]))
        idle["s"] = 0.0
        await asyncio.sleep(1.5)
        back = max(Radio.lamp.color)
        check("вернулся - яркость сразу обратно", back >= full - 3
              and "никого нет" not in c.status()["note"], "%d" % back)
        await c.shutdown()
    finally:
        sync_engine.input_idle_seconds = real


async def t_auto_preset_by_app():
    reset_world()
    app = {"exe": "explorer.exe"}
    real = webapp.foreground_exe
    webapp.foreground_exe = lambda: app["exe"]
    try:
        c = webapp.Controller(ADDR, auto_presets={"film": ["vlc.exe"]})
        await c.warm_up()
        await wait_for(lambda: c._player.link_up)
        await c.start_sync("desk")
        app["exe"] = "vlc.exe"
        ok = await wait_for(lambda: c.status()["name"] == "film", 8.0)
        check("программа из списка - её пресет, и видно почему",
              ok and "vlc.exe" in c.status()["label"], c.status()["label"])
        app["exe"] = "explorer.exe"
        ok = await wait_for(lambda: c.status()["name"] == "desk", 8.0)
        check("ушли из программы - обратно выбранный человеком пресет", ok,
              c.status()["label"])
        await c.shutdown()
    finally:
        webapp.foreground_exe = real


def t_config_new_keys():
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({
            "address": "BE:68:E4:00:09:25", "latitude": 999,
            "auto_presets": {"film": ["VLC.exe", 5], "bogus": ["x.exe"]},
            "sleep_minutes": "долго"}), encoding="utf-8")
        cfg = webapp.load_config(path)
        check("config: неверные широта и время засыпания - понятная пометка",
              cfg["latitude"] is None and cfg["sleep_minutes"] == 30
              and "latitude" in cfg["problem"], str(cfg["problem"]))
        check("config: auto_presets почищен (регистр, мусор, чужие пресеты)",
              cfg["auto_presets"] == {"film": ["vlc.exe"]},
              str(cfg["auto_presets"]))


ASYNC_TESTS = [
    t_sync_not_killed_by_keepalive, t_modes_after_off, t_reconnect_keeps_off,
    t_startup_does_not_touch_lamp, t_keepalive_without_commands_reads,
    t_unreachable_at_start, t_off_while_unreachable,
    t_sync_resends_after_reconnect, t_switch_starts_from_current_color,
    t_stop_and_labels, t_brightness_single_source, t_screen_lock_recovers,
    t_stuck_sampler_told_to_stop, t_stop_does_not_block_loop,
    t_bjled_details, t_http_api,
    t_power_button_resumes_last_mode, t_off_fades_out,
    t_crossfade_event_to_screen, t_festive_brightness_scale,
    t_click_reconnects_now, t_sleep_and_lock, t_sync_failure_restarts_itself,
    t_audio_restarts_on_device_change, t_ambilight_cli_smoke,
    t_http_power_and_page, t_sleep_scene_turns_off, t_idle_dim,
    t_auto_preset_by_app,
]
SYNC_TESTS = [
    t_protocol_frames, t_profile_found_from_any_folder,
    t_build_args_bad_mode, t_single_instance, t_winevents_plumbing,
    t_pipeline_layers, t_window_fits_content, t_host_and_config,
    t_audio_device_watch_real, t_scenes_alive, t_kelvin_and_sun,
    t_light_scene, t_config_new_keys,
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.CRITICAL if "-v" not in sys.argv
                        else logging.INFO)
    for t in SYNC_TESTS:
        print("%s:" % t.__name__)
        t()
    for t in ASYNC_TESTS:
        print("%s:" % t.__name__)
        asyncio.run(t())
    print()
    total = len(SYNC_TESTS) + len(ASYNC_TESTS)
    if FAILURES:
        print("ПРОВАЛЕНО: %d" % len(FAILURES))
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    print("Все проверки прошли (групп: %d)." % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

