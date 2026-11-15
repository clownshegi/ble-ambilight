from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import asyncio
import math
import re
import socket
import sys
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

import sync_engine
from events import EventPlayer
from extract import MODES
from color import linear_to_srgb
from icon import icon_png
from scenes import SCENE_TYPES
from sync_engine import ScreenSync
from winevents import foreground_exe

log = logging.getLogger("webapp")


PRESET_DEFS = (("desk", "Стол", "спокойно"),
               ("film", "Кино", "плавно"),
               ("game", "Игра", "быстро"))
MODE_DEFS = (("", "по пресету"), ("average", "среднее"),
             ("vibrant", "яркое"), ("dominant", "доминирующее"))
GATE_DEFS = (("audio", "Звук управляет яркостью", "--audio"),
             ("flash_gate", "Реакция на вспышки", "--flash-gate"),
             ("idle_life", "Оживление в статике", "--idle-life"),
             ("auto_contrast", "Адаптивный контраст", "--auto-contrast"),
             ("adaptive_tau", "Адаптивная плавность", "--adaptive-tau"),
             ("dominant_hold", "Удержание оттенка (для «доминирующего»)",
              "--dominant-hold"))
EVENT_DEFS = tuple((k, c.label, c.hint, c.tone) for k, c in SCENE_TYPES.items())

PRESETS = tuple(k for k, _l, _h in PRESET_DEFS)
PRESET_NAMES = {k: label.lower() for k, label, _h in PRESET_DEFS}
SYNC_FLAGS = {k: flag for k, _l, flag in GATE_DEFS}
GATES = {**{k: k + "_on" for k, _l, _f in GATE_DEFS}, "dark_gate": "dark_gate_on"}

UI_META = {"presets": PRESET_DEFS, "modes": MODE_DEFS,
           "gates": tuple((k, label) for k, label, _f in GATE_DEFS),
           "events": EVENT_DEFS}

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "lamp.log"
DEFAULT_CONFIG = {"address": "BE:68:E4:00:09:25", "port": 8099,
                  "off_when_pc_sleeps": True, "off_when_pc_locked": True,
                  "sleep_minutes": 30,
                  "dim_when_idle_min": 10,
                  "warm_evening": True, "latitude": None, "longitude": None,
                  "auto_presets": {}}
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

PAUSE_SLEEP = "ПК спит - лента погашена, вернётся сама"
PAUSE_LOCK = ("ПК заблокирован - подсветка экрана погашена, вернётся "
              "при входе")


def setup_logging() -> Path | None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    path: Path | None = LOG_PATH
    try:
        fh = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                 encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            "%Y-%m-%d %H:%M:%S"))
        handlers.append(fh)
    except OSError:
        path = None
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                        handlers=handlers)
    return path


def load_config(path: Path | None = None) -> dict:
    path = CONFIG_PATH if path is None else path
    config = dict(DEFAULT_CONFIG)
    on_disk: dict = {}
    problems: list[str] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("не JSON-объект")
            on_disk = data
            config.update(data)
        except (OSError, ValueError):
            log.warning("config.json повреждён, использую значения по умолчанию")
            problems.append("config.json повреждён - взяты значения по умолчанию")
            on_disk = None
    if on_disk is not None and any(k not in on_disk for k in DEFAULT_CONFIG):
        try:
            path.write_text(json.dumps({**DEFAULT_CONFIG, **on_disk},
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:
            pass
    try:
        config["port"] = int(config["port"])
    except (TypeError, ValueError):
        log.warning("config.json: порт %r не число, беру %d",
                    config["port"], DEFAULT_CONFIG["port"])
        config["port"] = DEFAULT_CONFIG["port"]
    address = config["address"]
    if not isinstance(address, str) or not MAC_RE.match(address.strip()):
        log.warning("config.json: MAC %r неверный, беру %s",
                    address, DEFAULT_CONFIG["address"])
        problems.append("в config.json неверный MAC %r - использую %s"
                        % (address, DEFAULT_CONFIG["address"]))
        config["address"] = DEFAULT_CONFIG["address"]
    else:
        config["address"] = address.strip().upper()
    for key in ("off_when_pc_sleeps", "off_when_pc_locked", "warm_evening"):
        config[key] = config[key] is not False
    for key, lo, hi in (("sleep_minutes", 1, 240), ("dim_when_idle_min", 0, 240)):
        try:
            config[key] = min(hi, max(lo, float(config[key])))
        except (TypeError, ValueError):
            problems.append("config.json: %s должно быть числом" % key)
            config[key] = DEFAULT_CONFIG[key]
    for key, lim in (("latitude", 90.0), ("longitude", 180.0)):
        v = config[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                              or not -lim <= v <= lim):
            problems.append("config.json: %s неверная - вечер по часам" % key)
            config[key] = None
    auto = config["auto_presets"]
    clean: dict[str, list[str]] = {}
    if isinstance(auto, dict):
        for preset, exes in auto.items():
            if preset in PRESET_NAMES and isinstance(exes, list):
                clean[preset] = [e.lower() for e in exes if isinstance(e, str)]
    elif auto:
        problems.append("config.json: auto_presets - ожидаю объект")
    config["auto_presets"] = clean
    config["problem"] = "; ".join(problems) or None
    return config


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _clamp_brightness(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ValueError("яркость должна быть числом от 0.05 до 1")
    return max(0.05, min(1.0, float(value)))


class Controller:
    def __init__(self, address: str, *, off_on_sleep: bool = True,
                 off_on_lock: bool = True, config_problem: str | None = None,
                 sleep_minutes: float = 30.0, dim_when_idle_min: float = 10.0,
                 location: tuple | None = None, warm_evening: bool = True,
                 auto_presets: dict | None = None):
        self.address = address
        self.kind: str | None = None
        self.name: str | None = None
        self.off_on_sleep = off_on_sleep
        self.off_on_lock = off_on_lock
        self.config_problem = config_problem
        self._player = EventPlayer(address)
        self._player.sleep_minutes = sleep_minutes
        self._player.location = location
        self._player.warm_evening = warm_evening
        self._player.on_scene_end = self._on_scene_end
        self.sync_extra = {"dim_when_idle": dim_when_idle_min}
        self.auto_presets = auto_presets or {}
        self._chosen_preset: str | None = None
        self.auto_reason: str | None = None
        self._auto_task: asyncio.Task | None = None
        self._sync: ScreenSync | None = None
        self._lock = asyncio.Lock()
        self._last: tuple | None = None
        self._pause: dict | None = None
        self._locked = False
        self._sync_failures = 0
        self.restart_base = 3.0


    def status(self) -> dict:
        player, sync = self._player, self._sync
        out = {"kind": self.kind, "name": self.name,
               "label": self._label(), "note": self._note(sync),
               "brightness": player.brightness,
               "connected": player.link_up, "power": player.power,
               "resumable": self._last is not None,
               "light": self.light(), "diag": self._diag()}
        if sync is not None:
            out.update(mode=sync.mode_choice, mode_actual=sync.mode)
            for key, attr in GATES.items():
                out[key] = bool(getattr(sync, attr))
        return out

    def light(self) -> dict | None:
        p = self._player
        rgb = p.led.last_rgb
        if p.power is not True or not p.link_up or rgb is None:
            return None
        peak = max(rgb)
        if peak <= 0:
            return None
        lin = [v / 255.0 for v in p.profile.to_light(rgb)]
        top = max(lin)
        shown = [int(round(255.0 * linear_to_srgb(v / top))) for v in lin]
        full = 255.0 * max(0.05, p.profile.max_level)
        return {"rgb": shown, "level": round(min(1.0, peak / full), 3)}

    def _label(self) -> str:
        kind, name = self.kind, self.name
        if kind == "sync":
            label = "экран · %s" % PRESET_NAMES.get(name, name)
            if self.auto_reason:
                label += " (по %s)" % self.auto_reason
            return label
        if kind == "event":
            scene = self._player.scene
            if scene is not None and scene.duration:
                left = max(0.0, scene.duration - self._player.scene_elapsed())
                return "%s · ещё %d мин" % (name, math.ceil(left / 60.0))
            return "режим · %s" % name
        if kind == "stopped":
            return "стоп - лента держит последний цвет"
        if kind == "on":
            return "включено - режим не выбран"
        if kind == "off":
            return "выключено"
        if kind == "paused":
            return (self._pause or {}).get("label", PAUSE_SLEEP)
        if kind == "error":
            return ("синхронизация с экраном не запускается - подробности "
                    "в журнале")
        return "ожидание - режим не выбран"

    def _note(self, sync: ScreenSync | None) -> str:
        notes = []
        if self.config_problem:
            notes.append(self.config_problem)
        if not self._player.link_up:
            notes.append("нет связи с лентой - ищу её, выбранное "
                         "применится само")
        if sync is not None and sync.problem:
            notes.append(sync.problem)
        if sync is not None and sync.notice:
            notes.append(sync.notice)
        return "; ".join(notes)

    def _diag(self) -> str:
        n = self._player.reconnects
        return "обрывов связи за сеанс: %d" % n if n else ""


    async def warm_up(self) -> None:
        self._player.start()
        if self.auto_presets and self._auto_task is None:
            self._auto_task = asyncio.ensure_future(self._auto_preset_loop())

    async def shutdown(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        async with self._lock:
            await self._stop_sync_engine()
            await self._player.stop()
            self.kind = self.name = None

    async def _stop_sync_engine(self) -> None:
        sync, self._sync = self._sync, None
        if sync is not None:
            await sync.stop()

    def _on_sync_failed(self, sync: ScreenSync) -> None:
        if self._sync is not sync or self.kind != "sync":
            return
        delay = min(30.0, self.restart_base * 2 ** self._sync_failures)
        self._sync_failures += 1
        sync.problem = "синхронизация упала - перезапуск через %.0f с" % delay
        loop = asyncio.get_running_loop()
        loop.call_later(delay, lambda: asyncio.ensure_future(
            self._restart_failed(sync)))

    async def _restart_failed(self, sync: ScreenSync) -> None:
        async with self._lock:
            if self._sync is not sync or self.kind != "sync" or not self._last:
                return
            try:
                await self._resume(self._last)
            except Exception as e:
                log.error("Перезапуск синхронизации не удался: %s", e)
                await self._stop_sync_engine()
                self.kind, self.name = "error", None


    def _live_flags(self, sync: ScreenSync) -> list[str]:
        return [flag for key, flag in SYNC_FLAGS.items()
                if getattr(sync, key + "_on")]

    def _remember_current(self) -> None:
        sync = self._sync
        if self.kind == "sync" and sync is not None:
            self._last = ("sync", self.name, sync.mode_choice or None,
                          self._live_flags(sync))

    async def _start_sync(self, preset: str, brightness: float | None,
                          mode: str | None, flags: list[str] | None) -> None:
        b = _clamp_brightness(brightness)
        args = sync_engine.build_args(
            preset, b if b is not None else self._player.brightness,
            mode, flags, extra=self.sync_extra)
        self._player.pause()
        await self._stop_sync_engine()
        if b is not None:
            self._player.brightness = b
        await self._player.power_on()
        sync = ScreenSync(self._player, args)
        sync.on_failed = lambda: self._on_sync_failed(sync)
        sync.mode_choice = mode or ""
        try:
            await sync.start()
        except Exception:
            await sync.stop()
            self.kind = self.name = None
            raise
        self._sync = sync
        self.kind, self.name = "sync", preset
        self._last = ("sync", preset, mode, list(flags or []))
        log.info("Синхронизация с экраном: пресет %s %s",
                 preset, " ".join(flags or []))

    async def _start_event(self, name: str, brightness: float | None) -> None:
        b = _clamp_brightness(brightness)
        await self._stop_sync_engine()
        if b is not None:
            self._player.brightness = b
        await self._player.power_on()
        self._player.switch(name)
        self.kind, self.name = "event", name
        self.auto_reason = None
        if SCENE_TYPES[name].duration is None:
            self._last = ("event", name)
        log.info("Режим атмосферы: %s", name)

    def _on_scene_end(self, name: str) -> None:
        asyncio.ensure_future(self._finish_scene(name))

    async def _finish_scene(self, name: str) -> None:
        async with self._lock:
            if self.kind != "event" or self.name != name:
                return
            await self._player.off(duration=2.0)
            self.kind, self.name = "off", None
            log.info("%s: сценарий закончился - лента выключена", name)

    async def _auto_preset_loop(self) -> None:
        pending, since = None, 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                if self.kind != "sync" or not self._chosen_preset:
                    pending = None
                    continue
                exe = foreground_exe()
                target = next((preset for preset, exes in self.auto_presets.items()
                               if exe and exe in exes), self._chosen_preset)
                if target == self.name:
                    pending = None
                    continue
                now = asyncio.get_running_loop().time()
                if target != pending:
                    pending, since = target, now
                    continue
                if now - since < 3.0:
                    continue
                async with self._lock:
                    sync = self._sync
                    if self.kind != "sync" or sync is None or self.name == target:
                        continue
                    mode, flags = sync.mode_choice or None, self._live_flags(sync)
                    await self._start_sync(target, None, mode, flags)
                    self.auto_reason = exe if target != self._chosen_preset else None
                    self._last = ("sync", self._chosen_preset, mode, flags)
                log.info("Пресет по программе: %s -> %s", exe or "?", target)
                pending = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Пресет по программе: %s", e)

    async def _resume(self, last: tuple) -> None:
        if last[0] == "sync":
            self._chosen_preset = last[1]
            self.auto_reason = None
            await self._start_sync(last[1], None, last[2], last[3])
        else:
            await self._start_event(last[1], None)

    def _user_intent(self) -> None:
        self._pause = None


    async def start_sync(self, preset: str, brightness: float | None = None,
                         mode: str | None = None,
                         flags: list[str] | None = None) -> None:
        if preset not in PRESETS:
            raise ValueError("неизвестный пресет: %s" % preset)
        _clamp_brightness(brightness)
        async with self._lock:
            self._user_intent()
            self._sync_failures = 0
            self._chosen_preset = preset
            self.auto_reason = None
            await self._start_sync(preset, brightness, mode, flags)

    async def start_event(self, name: str,
                          brightness: float | None = None) -> None:
        if name not in SCENE_TYPES:
            raise ValueError("неизвестный режим: %s" % name)
        _clamp_brightness(brightness)
        async with self._lock:
            self._user_intent()
            await self._start_event(name, brightness)

    async def stop(self) -> None:
        async with self._lock:
            self._user_intent()
            self._remember_current()
            await self._stop_sync_engine()
            self._player.pause()
            power = self._player.power
            self.kind = "off" if power is False else (
                "stopped" if power else None)
            self.name = None

    async def turn_off(self) -> None:
        async with self._lock:
            self._user_intent()
            self._remember_current()
            await self._player.off()
            await self._stop_sync_engine()
            self.kind, self.name = "off", None

    async def turn_on(self, brightness: float | None = None) -> None:
        b = _clamp_brightness(brightness)
        async with self._lock:
            self._user_intent()
            if self._player.power is True and self.kind in ("sync", "event"):
                return
            if b is not None:
                self._player.brightness = b
            if self._last is not None:
                self._sync_failures = 0
                await self._resume(self._last)
                return
            await self._player.power_on()
            self.kind, self.name = "on", None


    async def on_system(self, event: str) -> None:
        if event == "lock":
            self._locked = True
            if self.off_on_lock and self.kind == "sync":
                await self._system_pause(PAUSE_LOCK, fade=True)
        elif event == "unlock":
            self._locked = False
            await self._system_resume()
        elif event in ("suspend", "shutdown"):
            if self.off_on_sleep:
                await self._system_pause(PAUSE_SLEEP, fade=False)
        elif event == "resume":
            await self._system_resume()

    async def _system_pause(self, label: str, fade: bool) -> None:
        async with self._lock:
            if self._pause is not None:
                self._pause["label"] = label
                self.kind = "paused"
                return
            if self._player.power is not True:
                return
            self._remember_current()
            prev = self.kind
            rgb = self._player.led.last_rgb
            await self._player.off(fade=fade)
            await self._stop_sync_engine()
            self._pause = {"label": label, "kind": prev, "rgb": rgb,
                           "resume": self._last if prev in ("sync", "event")
                           else None}
            self.kind, self.name = "paused", None
            log.info("Лента погашена: %s", label)

    async def _system_resume(self) -> None:
        async with self._lock:
            pause = self._pause
            if pause is None:
                return
            target = pause["resume"]
            if (self._locked and self.off_on_lock and target is not None
                    and target[0] == "sync"):
                pause["label"] = PAUSE_LOCK
                self.kind = "paused"
                return
            self._pause = None
            log.info("Возврат после %s", "сна" if pause["label"] == PAUSE_SLEEP
                     else "блокировки")
            if target is not None:
                self._sync_failures = 0
                try:
                    await self._resume(target)
                except Exception as e:
                    log.error("Возврат режима не удался: %s", e)
                    self.kind, self.name = "error", None
                return
            await self._player.power_on()
            rgb = pause["rgb"]
            if rgb is not None:
                if self._player.led.is_connected:
                    await self._player.put_rgb(rgb)
                else:
                    self._player.led.last_rgb = rgb
            self.kind = pause["kind"] if pause["kind"] in ("stopped", "on") \
                else "stopped"


    def set_brightness(self, brightness) -> None:
        b = _clamp_brightness(brightness)
        if b is None:
            raise ValueError("нужна яркость")
        self._player.brightness = b
        if self._sync is not None:
            self._sync.brightness = b

    def set_mode(self, mode: str) -> None:
        if mode and mode not in MODES:
            raise ValueError("неизвестный режим цвета: %s" % mode)
        sync = self._sync
        if sync is not None:
            sync.mode_choice = mode
            sync.mode = mode or sync.preset_mode

    def set_gate(self, name: str, on: bool) -> None:
        attr = GATES.get(name)
        if attr is None:
            raise ValueError("неизвестный переключатель: %s" % name)
        if self._sync is not None:
            setattr(self._sync, attr, bool(on))


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0a0d" id="themeColor">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>Лента</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0a0d; --fg: #f3f1f4; --dim: #9d9aa5; --faint: #5d5a64;
    --line: #24222a; --surface: #151419;
    --idle: #8a8794; --accent: var(--idle); --glow: #000; --glow-o: 0;
    --warn: #e6b566;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { background: var(--bg); overflow-x: hidden; }
  body {
    margin: 0 auto; max-width: 520px; overflow-x: hidden;
    padding: 0 16px calc(32px + env(safe-area-inset-bottom));
    background: var(--bg); color: var(--fg);
    font: 16px/1.4 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .hero { position: relative; padding: calc(22px + env(safe-area-inset-top)) 0 30px; isolation: isolate; }
  .glow {
    position: absolute; z-index: -1; left: 50%; top: -70px;
    width: 150%; height: 250px; transform: translateX(-50%);
    border-radius: 50%; background-color: var(--glow); opacity: var(--glow-o);
    filter: blur(64px); pointer-events: none;
    transition: background-color .9s ease, opacity .9s ease;
  }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  h1 { font-size: 28px; font-weight: 700; letter-spacing: -.01em; margin: 0; }
  .power {
    width: 64px; height: 64px; flex: none; border-radius: 50%; cursor: pointer;
    display: grid; place-items: center; background: transparent;
    border: 1.5px solid var(--faint); color: var(--dim);
    transition: background-color .4s, border-color .4s, color .4s, box-shadow .4s, transform .08s;
  }
  .power svg { width: 26px; height: 26px; }
  .power.on { background: var(--accent); border-color: var(--accent); color: #111014;
              box-shadow: 0 0 36px -6px var(--accent); }
  .power:active:not(:disabled) { transform: scale(.94); }
  .power:disabled { opacity: .5; }
  #statusText { margin: 14px 0 0; font-size: 17px; font-weight: 600; }
  #statusText::first-letter { text-transform: uppercase; }
  #statusText.stale { color: var(--dim); }
  #note { margin: 6px 0 0; color: var(--warn); font-size: 14px; display: none; }
  section { margin: 0 0 26px; }
  h2 { font-size: 13px; font-weight: 600; color: var(--dim); margin: 0 0 10px; }
  .bright { display: flex; align-items: center; gap: 14px; }
  .bright output { min-width: 3.2em; text-align: right; color: var(--dim);
                   font-variant-numeric: tabular-nums; }
  input[type=range] { flex: 1; height: 28px; accent-color: var(--accent); }
  .row3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .scenes { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
  .scenes .mode:first-child { grid-column: 1 / -1; }
  .mode {
    appearance: none; font: inherit; color: var(--fg); text-align: left; cursor: pointer;
    background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
    padding: 14px 12px 12px; position: relative; overflow: hidden;
    transition: background-color .3s, border-color .3s, transform .08s;
  }
  .mode b { display: block; font-size: 16px; font-weight: 650; position: relative; }
  .mode span { display: block; font-size: 12px; color: var(--dim); margin-top: 2px; position: relative; }
  .mode:active:not(:disabled) { transform: scale(.97); }
  .mode:disabled { opacity: .5; }
  .mode.active { border-color: var(--accent);
                 background: color-mix(in srgb, var(--accent) 16%, var(--surface)); }
  .event::before {
    content: ""; position: absolute; left: -20%; right: -20%; bottom: -70%; height: 130%;
    background: radial-gradient(closest-side, var(--tone), transparent);
    opacity: .32; pointer-events: none; transition: opacity .3s;
  }
  .event.active { border-color: var(--tone); background: var(--surface); }
  .event.active::before { opacity: .75; }
  .line { display: flex; align-items: center; justify-content: space-between; gap: 12px;
          padding: 14px 0 0; font-size: 15px; }
  select { background: var(--surface); color: var(--fg); border: 1px solid var(--line);
           border-radius: 10px; padding: 8px 10px; font: inherit; font-size: 14px; }
  details { margin-top: 12px; }
  summary { cursor: pointer; color: var(--dim); font-size: 14px; padding: 6px 0; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: " ▸"; }
  details[open] summary::after { content: " ▾"; }
  label.opt { display: flex; align-items: center; gap: 10px; padding: 8px 0; font-size: 15px; }
  input[type=checkbox] { width: 20px; height: 20px; accent-color: var(--accent); }
  .quiet { background: none; border: none; color: var(--dim); font: inherit; font-size: 14px;
           padding: 6px 0; cursor: pointer; text-decoration: underline;
           text-underline-offset: 3px; text-decoration-color: var(--faint); }
  .quiet:disabled { opacity: .5; }
  #msg { min-height: 20px; color: var(--dim); font-size: 14px; margin-top: 6px; }
  footer { color: var(--faint); font-size: 12px; margin-top: 18px; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
  <header class="hero">
    <div class="glow" aria-hidden="true"></div>
    <div class="top">
      <h1>Лента</h1>
      <button id="power" class="power" aria-label="Включить">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
             stroke-linecap="round" aria-hidden="true"><path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>
      </button>
    </div>
    <p id="statusText">…</p>
    <p id="note"></p>
  </header>

  <section>
    <h2>Яркость</h2>
    <div class="bright">
      <input type="range" id="brightness" min="5" max="100" value="100" aria-label="Яркость">
      <output id="brightVal">100%</output>
    </div>
  </section>

  <section>
    <h2>Экран</h2>
    <div class="row3" id="presets"></div>
    <label class="line"><span>Извлечение цвета</span><select id="modeSelect"></select></label>
    <details>
      <summary>Дополнительно (экспериментальное)</summary>
      <div id="opts"></div>
    </details>
  </section>

  <section>
    <h2>Атмосфера</h2>
    <div class="scenes" id="events"></div>
  </section>

  <button id="stopBtn" class="quiet">Стоп - замереть на текущем цвете</button>
  <div id="msg" role="status"></div>
  <footer id="diag"></footer>

<script>
const META = /*META*/null;
const $ = id => document.getElementById(id);
const root = document.documentElement.style;
const msg = $("msg"), brightness = $("brightness"), brightVal = $("brightVal");
const BG = [11, 10, 13];
const touched = {};
let busy = false, st = null, bTimer = null;

function el(tag, cls, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (parent) parent.appendChild(e);
  return e;
}
function tile(parent, cls, title, hint) {
  const b = el("button", cls, parent);
  el("b", "", b).textContent = title;
  el("span", "", b).textContent = hint;
  return b;
}
META.presets.forEach(([key, label, hint]) => {
  const b = tile($("presets"), "mode", label, hint);
  b.dataset.sync = key;
});
META.events.forEach(([key, label, hint, tone]) => {
  const b = tile($("events"), "mode event", label, hint);
  b.dataset.event = key;
  b.style.setProperty("--tone", tone);
});
META.modes.forEach(([key, label]) => {
  const o = el("option", "", $("modeSelect"));
  o.value = key; o.textContent = label;
});
META.gates.forEach(([key, label]) => {
  const l = el("label", "opt", $("opts"));
  const c = el("input", "", l);
  c.type = "checkbox"; c.id = "opt_" + key; c.dataset.gate = key;
  el("span", "", l).textContent = label;
});

function touch(id) { touched[id] = Date.now(); }
function untouched(id) { return Date.now() - (touched[id] || 0) > 3000; }
function setBusy(on) {
  busy = on;
  document.querySelectorAll("button").forEach(b => b.disabled = on);
}
function currentBrightness() { return Number(brightness.value) / 100; }
function hex(rgb) { return "#" + rgb.map(v => v.toString(16).padStart(2, "0")).join(""); }

function setLight(light) {
  if (light) {
    const c = "rgb(" + light.rgb.join(" ") + ")";
    root.setProperty("--accent", c);
    root.setProperty("--glow", c);
    root.setProperty("--glow-o", (0.16 + 0.34 * light.level).toFixed(2));
    $("themeColor").content = hex(light.rgb.map((v, i) => Math.round(BG[i] + (v - BG[i]) * 0.22)));
  } else {
    root.setProperty("--accent", "var(--idle)");
    root.setProperty("--glow-o", "0");
    $("themeColor").content = "#0b0a0d";
  }
}

async function request(url, body, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, body === undefined ? {cache: "no-store", signal: ctl.signal} : {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body || {}), signal: ctl.signal,
    });
    let data = null;
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
    return data;
  } catch (e) {
    if (e.name === "TypeError" || e.name === "AbortError")
      throw new Error("нет связи с ПК - он включён и в той же Wi-Fi?");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function action(url, body, waiting) {
  setBusy(true);
  msg.textContent = waiting;
  const started = performance.now();
  try {
    render(await request(url, body, 25000));
    const ms = Math.round(performance.now() - started);
    msg.textContent = ms > 800 ? "готово (" + (ms / 1000).toFixed(1) + " с)" : "";
  } catch (e) {
    msg.textContent = "Ошибка: " + e.message;
  } finally {
    setBusy(false);
  }
}

async function tweak(url, body) {
  try { render(await request(url, body, 8000)); msg.textContent = ""; }
  catch (e) { msg.textContent = "Ошибка: " + e.message; }
}

function render(s) {
  if (!s) return;
  st = s;
  $("statusText").textContent = s.label;
  $("statusText").classList.remove("stale");
  $("note").textContent = s.note || "";
  $("note").style.display = s.note ? "block" : "none";
  setLight(s.light);
  const lit = s.power === true;
  $("power").classList.toggle("on", lit);
  $("power").setAttribute("aria-label", lit ? "Выключить" : "Включить");
  document.querySelectorAll("[data-sync], [data-event]").forEach(b => {
    const on = (b.dataset.sync && s.kind === "sync" && b.dataset.sync === s.name)
            || (b.dataset.event && s.kind === "event" && b.dataset.event === s.name);
    b.classList.toggle("active", !!on);
  });
  if (untouched("brightness")) {
    brightness.value = Math.round(s.brightness * 100);
    brightVal.textContent = brightness.value + "%";
  }
  if (s.mode !== undefined && untouched("modeSelect")) $("modeSelect").value = s.mode;
  document.querySelectorAll("[data-gate]").forEach(c => {
    const key = c.dataset.gate;
    if (s[key] !== undefined && untouched(key)) c.checked = s[key];
  });
  $("diag").textContent = s.diag || "";
}

async function poll() {
  if (busy || document.visibilityState !== "visible") return;
  try {
    render(await request("/api/status", undefined, 3000));
  } catch (e) {
    $("statusText").classList.add("stale");
    $("note").textContent = "нет связи с ПК - показано последнее известное состояние";
    $("note").style.display = "block";
    setLight(null);
  }
}

document.querySelectorAll("[data-sync]").forEach(b => b.addEventListener("click", () => {
  const body = {preset: b.dataset.sync, brightness: currentBrightness(),
                mode: $("modeSelect").value};
  document.querySelectorAll("[data-gate]").forEach(c => { body[c.dataset.gate] = c.checked; });
  action("/api/sync", body, "включаю…");
}));
document.querySelectorAll("[data-event]").forEach(b => b.addEventListener("click", () =>
  action("/api/event", {name: b.dataset.event, brightness: currentBrightness()}, "переключаю…")));
$("power").addEventListener("click", () => {
  if (st && st.power === true) action("/api/off", {}, "выключаю…");
  else action("/api/on", {brightness: currentBrightness()}, "включаю…");
});
$("stopBtn").addEventListener("click", () => action("/api/stop", {}, "останавливаю…"));

brightness.addEventListener("input", () => {
  touch("brightness"); brightVal.textContent = brightness.value + "%";
  if (!bTimer) bTimer = setTimeout(() => {
    bTimer = null; tweak("/api/brightness", {value: currentBrightness()});
  }, 90);
});
brightness.addEventListener("change", () => {
  touch("brightness"); tweak("/api/brightness", {value: currentBrightness()});
});
$("modeSelect").addEventListener("change", e => {
  touch("modeSelect"); tweak("/api/mode", {value: e.target.value});
});
document.querySelectorAll("[data-gate]").forEach(c => c.addEventListener("change", e => {
  touch(c.dataset.gate); tweak("/api/gate", {name: c.dataset.gate, on: e.target.checked});
}));

document.addEventListener("visibilitychange", poll);
setInterval(poll, 1000);
poll();
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Лента", "short_name": "Лента", "lang": "ru",
    "start_url": "/", "display": "standalone",
    "background_color": "#0b0a0d", "theme_color": "#0b0a0d",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}
ICON_SIZES = {"icon-180.png": 180, "icon-192.png": 192, "icon-512.png": 512}
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".localdomain", ".home.arpa",
                  ".internal")


def render_page() -> str:
    meta = json.dumps(UI_META, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("/*META*/null", meta)


def _host_allowed(host: str) -> bool:
    name = (urlsplit("//" + (host or "")).hostname or "").lower()
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    if name in ("localhost", me):
        return True
    return any(name == me + suffix for suffix in LOCAL_SUFFIXES)


@web.middleware
async def _guard(request: web.Request, handler):
    if not _host_allowed(request.host):
        return web.json_response(
            {"error": "неизвестный адрес: %s" % request.host}, status=403)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin is not None and \
                urlsplit(origin).netloc.lower() != request.host.lower():
            return web.json_response({"error": "запрос с чужой страницы"},
                                     status=403)
        if request.content_type != "application/json":
            return web.json_response(
                {"error": "нужен Content-Type: application/json"}, status=415)
    return await handler(request)


async def _read_json(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("ожидаю JSON-объект")
    return data


def _opt_str(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError("%s: ожидаю строку" % key)
    return value


def build_app(controller: Controller) -> web.Application:
    routes = web.RouteTableDef()
    page = render_page()

    @routes.get("/")
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @routes.get("/manifest.webmanifest")
    async def manifest(_request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(MANIFEST, ensure_ascii=False),
                            content_type="application/manifest+json")

    async def icon(request: web.Request) -> web.Response:
        size = ICON_SIZES[request.match_info["name"]]
        return web.Response(body=icon_png(size), content_type="image/png",
                            headers={"Cache-Control": "max-age=86400"})
    routes.get("/{name:icon-(?:180|192|512)\\.png}")(icon)

    @routes.get("/api/status")
    async def status(_request: web.Request) -> web.Response:
        return web.json_response(controller.status(),
                                 headers={"Cache-Control": "no-store"})

    def endpoint(path: str, action) -> None:
        async def handler(request: web.Request) -> web.Response:
            try:
                await action(await _read_json(request))
            except (ValueError, TypeError) as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(controller.status())
        routes.post(path)(handler)

    async def do_sync(d: dict) -> None:
        flags = [flag for key, flag in SYNC_FLAGS.items() if d.get(key) is True]
        await controller.start_sync(_opt_str(d, "preset") or "",
                                    brightness=d.get("brightness"),
                                    mode=_opt_str(d, "mode") or None,
                                    flags=flags)

    async def do_event(d: dict) -> None:
        await controller.start_event(_opt_str(d, "name") or "",
                                     brightness=d.get("brightness"))

    async def do_brightness(d: dict) -> None:
        controller.set_brightness(d.get("value"))

    async def do_mode(d: dict) -> None:
        controller.set_mode(_opt_str(d, "value") or "")

    async def do_gate(d: dict) -> None:
        controller.set_gate(_opt_str(d, "name") or "", d.get("on") is True)

    async def do_stop(_d: dict) -> None:
        await controller.stop()

    async def do_off(_d: dict) -> None:
        await controller.turn_off()

    async def do_on(d: dict) -> None:
        await controller.turn_on(brightness=d.get("brightness"))

    endpoint("/api/sync", do_sync)
    endpoint("/api/event", do_event)
    endpoint("/api/brightness", do_brightness)
    endpoint("/api/mode", do_mode)
    endpoint("/api/gate", do_gate)
    endpoint("/api/stop", do_stop)
    endpoint("/api/off", do_off)
    endpoint("/api/on", do_on)

    app = web.Application(middlewares=[_guard])
    app.add_routes(routes)

    async def on_startup(_app: web.Application) -> None:
        await controller.warm_up()

    async def on_cleanup(_app: web.Application) -> None:
        await controller.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def make_controller(address: str, config: dict) -> Controller:
    config = {**load_defaults(), **config}
    location = None
    if config["latitude"] is not None and config["longitude"] is not None:
        location = (config["latitude"], config["longitude"])
    return Controller(address, off_on_sleep=config["off_when_pc_sleeps"],
                      off_on_lock=config["off_when_pc_locked"],
                      config_problem=config.get("problem"),
                      sleep_minutes=config["sleep_minutes"],
                      dim_when_idle_min=config["dim_when_idle_min"],
                      location=location, warm_evening=config["warm_evening"],
                      auto_presets=config["auto_presets"])


def load_defaults() -> dict:
    return {**DEFAULT_CONFIG, "problem": None}


async def serve(controller: Controller, port: int, open_browser: bool) -> None:
    app = build_app(controller)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        import winevents
        winevents.attach(controller, asyncio.get_running_loop())
        if open_browser:
            webbrowser.open("http://127.0.0.1:%d" % port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> int:
    setup_logging()
    config = load_config()
    p = argparse.ArgumentParser(description="Веб-панель управления лентой")
    p.add_argument("address", nargs="?", default=config["address"],
                   help="MAC-адрес ленты (по умолчанию - из config.json)")
    p.add_argument("--port", type=int, default=config["port"])
    p.add_argument("--no-browser", action="store_true",
                   help="не открывать страницу в браузере при старте")
    args = p.parse_args()

    ip = lan_ip()
    print("Панель управления:")
    print("  на этом ПК:   http://127.0.0.1:%d" % args.port)
    print("  с телефона:   http://%s:%d   (тот же Wi-Fi)" % (ip, args.port))
    print("Ctrl+C - остановить.\n")

    try:
        asyncio.run(serve(make_controller(args.address, config), args.port,
                          open_browser=not args.no_browser))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print("Веб-сервер не запустился на порту %d: %s\nСкорее всего, панель "
              "уже запущена (Lamp.exe или webapp.py)." % (args.port, e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

