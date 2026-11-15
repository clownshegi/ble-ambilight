# Lamp — Ambilight and ambient lighting for a BJ_LED BLE strip

*[Русская версия](README.ru.md)*

A Windows app that drives a cheap BJ_LED RGB strip (BJ-5VRGB controller, stock app MohuanLED)
over Bluetooth Low Energy: it syncs the strip with what is on screen, plays "living" light
scenes and can be controlled from the PC or from a phone via a web panel.

The controller protocol is closed — it was reverse-engineered from btsnoop dumps of the stock app.

## Features

- **Screen sync** — capture via Desktop Duplication (dxcam) with fallback to mss, black-bar cropping,
  scene-cut detection, presets "Film", "Game", "Desk".
- **Ambient scenes** — regular light (5000 K by day, 2700 K in the evening, sunset computed from
  coordinates), fireplace, candle, thunderstorm, gentle fall-asleep with auto power-off.
- **Two UIs over one state** — native window (tkinter) and a phone web panel (aiohttp, installable as a PWA).
  Both go through a shared `Controller` and a single BLE connection.
- **Windows integration** — reacts to sleep, wake and lock; dims when nobody is at the PC; picks a preset by
  the active application; system audio volume (WASAPI loopback) modulates brightness.
- **Camera colour calibration** — per-channel curve measured with a phone camera at locked white balance.
- **Zones and other transports** — layouts for several strips, or an addressable WLED strip over DDP.

## Technical decisions

**Protocol reverse engineering.** The frame grammar `69 96 LL CC <payload>` was reconstructed from dumps and
reproduces every observed frame byte for byte. Undocumented behaviour found: the fourth byte of the colour
command is a *persisted* global brightness that affects all following commands. The device sends no
notifications, so its state cannot be read — the app keeps its own state model.

| CC | command | frame |
|---|---|---|
| 01 | power | `69 96 02 01 01` / `69 96 02 01 00` |
| 02 | colour | `69 96 05 02 RR GG BB [WW]` |
| 03, 04 | built-in effects | `69 96 03 0X MM SS` |
| 05 | timer | `69 96 06 05 01 MASK HH MM 00` |
| 07 | clock | `69 96 06 07 TT TT TT TT 00` |

**Colour in linear light.** Frames are averaged in linear light, not over sRGB bytes (on dark scenes the
byte-average error reaches 5×). Lightness comes from a plain mean, hue from a saturation-weighted mean.
Transitions are interpolated in Oklab so they never pass through grey. Output goes through a profile:
white balance, PWM floor and a piecewise-linear channel curve.

**A narrow BLE channel.** The ceiling is about 20 commands per second (~45 ms connection interval).
Write-without-response reports nothing about success, so sending goes through a scheduler with a dead zone on
the final bytes relative to the last *sent* value: slow drift is not lost and the link is never flooded.

**Dynamics.** Exponential smoothing corrected for the real frame step, asymmetric fade-in / fade-out, flash
handling, dark threshold, adaptive contrast and smoothness. Scenes avoid strict periods: smooth noise,
"floating" periods and Poisson events give a non-repeating pattern; frequencies are capped (≤ 3 Hz) in the
generator itself.

**Reliability.**
- Every state change happens under an `asyncio.Lock`, so simultaneous commands from PC and phone never conflict.
- Background BLE reconnect with exponential back-off that any user command resets; keep-alive never changes the colour.
- Screen capture and sync restart on failure, falling back from dxcam to mss.
- Single app instance, watchdog for the window.
- The web API accepts only JSON from its own page and its own host — protection against cross-site requests.

## Architecture

```
desktop.py ─┐                    ┌─ sync_engine.py ─ capture → extract → dynamics → pipeline
            ├─ webapp.Controller ┤
webapp.py  ─┘                    └─ events.EventPlayer ─ scenes.py
                                          │
                                     transport.py ─ bjled.py (BLE) / WLED (DDP)
```

| module | purpose |
|---|---|
| `desktop.py` | native window, `Lamp.exe` entry point; starts the web panel in the same process |
| `webapp.py` | `Controller` (shared state and status), HTTP API and the phone page |
| `events.py` | `EventPlayer` — owns the strip connection, plays scenes, smooth transitions |
| `sync_engine.py` | screen sync over the already open connection |
| `scenes.py` | scenes: noise, floating periods, colour temperature, sunset by coordinates |
| `capture.py` | dxcam / mss screen capture, black-bar cropping |
| `extract.py` | frame colour (`average`, `vibrant`, `dominant`), scene-cut detector |
| `dynamics.py` | smoothing, flashes, drift, contrast, send scheduler |
| `pipeline.py` | shared layer pipeline on top of the screen colour |
| `color.py` | linear light, Oklab, output profile |
| `zones.py`, `transport.py` | per-zone frame layout, output to BLE or WLED |
| `bjled.py` | controller client: protocol frames, reconnect |
| `audio.py` | system audio volume via WASAPI loopback |
| `winevents.py` | Windows sleep, wake and lock |
| `ambilight.py` | standalone CLI with hotkeys |
| `icon.py` | the icon, drawn in code |

Hardware utilities: `scan_ble.py`, `gatt_explore.py`, `probe_ble.py`, `probe_protocol.py` (device and protocol
research), `calibrate.py`, `hw_camera.py` (calibration), `effects.py`, `schedule_led.py` (built-in effects and
controller timer), `rescue.py` and `hw_*.py` (diagnostics and manual checks on the strip).

## Install and run

Requires Windows 10/11 and Python 3.14.

```bash
python -m pip install -r requirements.txt
python desktop.py
```

The strip's MAC address and settings are stored in `config.json` next to the app.
The web panel is reachable from a phone on the same network at the address shown in the window.

CLI variant:

```bash
python ambilight.py <MAC> --preset film
```

Build `Lamp.exe` and `Ambilight.exe`:

```bash
python -m PyInstaller lamp.spec --noconfirm
```

## Tests

```bash
python test_app.py
python test_dynamics.py
```

`test_app.py` exercises the whole app against a software model of the strip and the BLE link, no hardware
needed: protocol, power and transitions, scenes, PC sleep and lock, reconnect, capture, web API.
`test_dynamics.py` covers the colour-processing algorithms.

## Limitations

- The strip is not addressable: the whole segment shows one colour.
- BLE latency starts at ~100 ms.
- Games in exclusive fullscreen do not expose frames via Desktop Duplication; borderless windowed mode helps.

## Credits

[8none1/bj_led](https://github.com/8none1/bj_led),
[Tech-Morph/MohuanLED_HA](https://github.com/Tech-Morph/MohuanLED_HA),
[hyperion.ng](https://github.com/hyperion-project/hyperion.ng),
[PySceneDetect](https://github.com/Breakthrough/PySceneDetect),
[Oklab — Björn Ottosson](https://bottosson.github.io/posts/oklab/).
