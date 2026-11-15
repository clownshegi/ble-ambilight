from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time

from color import OutputProfile, DEFAULT_PROFILE_PATH
from capture import Capturer
from dynamics import SendScheduler, CUT_DISTANCE
from extract import MODES, frame_difference
from pipeline import ScenePipeline
from transport import BLESink, DDPSink, Sink
from zones import make_layout, sample, describe

try:
    import msvcrt
except ImportError:
    msvcrt = None

log = logging.getLogger("ambilight")

CUT_THRESHOLD = 27.0
MIN_SCENE_S = 0.5

PRESETS = {
    "desk": dict(
        mode="vibrant", capture_fps=15.0, step=12, fps=10.0,
        tau_up=0.35, tau_down=0.90, autospeed=4.0, gamma_out=2.6,
        brightness=0.55, min_level=0.35, crop_bars=False, cut_detect=False,
        dark_gate=True, flash_gate=False, idle_life=False, auto_contrast=False,
        adaptive_tau=False, dominant_hold=False,
    ),
    "film": dict(
        mode="dominant", capture_fps=20.0, step=10, fps=20.0,
        tau_up=0.20, tau_down=0.50, autospeed=10.0, gamma_out=2.2,
        brightness=1.0, min_level=0.20, crop_bars=True, cut_detect=True,
        dark_gate=True, flash_gate=False, idle_life=False, auto_contrast=False,
        adaptive_tau=False, dominant_hold=False,
    ),
    "game": dict(
        mode="vibrant", capture_fps=30.0, step=12, fps=20.0,
        tau_up=0.08, tau_down=0.25, autospeed=14.0, gamma_out=2.4,
        brightness=1.0, min_level=0.25, crop_bars=False, cut_detect=True,
        dark_gate=True, flash_gate=False, idle_life=False, auto_contrast=False,
        adaptive_tau=False, dominant_hold=False,
    ),
}


def enable_vt() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class CleanLineHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            self.stream.write("\r" + " " * 120 + "\r")
        except Exception:
            pass
        super().emit(record)


def getkey() -> str | None:
    if msvcrt is None or not msvcrt.kbhit():
        return None
    ch = msvcrt.getch()
    if ch in (b"\x00", b"\xe0"):
        msvcrt.getch()
        return None
    try:
        return ch.decode("ascii", "ignore")
    except Exception:
        return None


class Sampler(threading.Thread):
    def __init__(self, args, stop: threading.Event):
        super().__init__(name="sampler", daemon=True)
        self.args = args
        self.stop = stop
        self.mode = args.mode
        self.zones = make_layout(args.layout)
        self.targets = [(0.0, 0.0, 0.0)] * len(self.zones)
        self.frames = 0
        self.errors = 0
        self.last_ms = 0.0
        self.backend = "?"
        self.cut_seq = 0
        self.last_diff = 0.0
        self.want_diff = False
        self.ready = threading.Event()
        self._prev = None
        self._last_cut = 0.0
        self._last_error_log = -1e9

    def run(self) -> None:
        a = self.args
        period = 1.0 / a.capture_fps
        try:
            with Capturer(backend=a.backend, monitor=a.monitor, step=a.step,
                          crop_bars=a.crop_bars) as cap:
                self.backend = cap.backend
                log.info("Захват: %s, экран %dx%d, шаг %d, режим %s",
                         cap.backend, cap.width, cap.height, a.step, self.mode)
                log.info("Раскладка: %s", describe(self.zones))
                while not self.stop.is_set():
                    t0 = time.perf_counter()
                    try:
                        px = cap.grab()
                        if px is not None:
                            if a.cut_detect or a.adaptive_tau or self.want_diff:
                                self._check_cut(px, time.monotonic())
                            self.targets = sample(px, self.zones, self.mode)
                            self.frames += 1
                            self.ready.set()
                            self.last_ms = (time.perf_counter() - t0) * 1000.0
                    except Exception as e:
                        self.errors += 1
                        now_e = time.monotonic()
                        if now_e - self._last_error_log >= 30.0:
                            self._last_error_log = now_e
                            log.warning("Захват не удался: %s (ошибок всего "
                                        "%d)", e, self.errors)
                        self.stop.wait(0.5)
                        continue
                    self.stop.wait(max(0.0, period - (time.perf_counter() - t0)))
        except Exception as e:
            log.error("Поток захвата умер: %s", e)
            self.stop.set()
            self.ready.set()

    def _check_cut(self, px, now: float) -> None:
        diff = frame_difference(px, self._prev)
        self._prev = px.copy()
        self.last_diff = diff
        if diff >= CUT_THRESHOLD and (now - self._last_cut) >= MIN_SCENE_S:
            self._last_cut = now
            self.cut_seq += 1


def swatches(codes, colored: bool, limit: int = 8) -> str:
    if not colored:
        return ""
    shown = codes[:limit]
    out = "".join("\x1b[48;2;%d;%d;%dm  \x1b[0m" % c for c in shown)
    if len(codes) > limit:
        out += "+%d" % (len(codes) - limit)
    return out + " "


HELP = ("  клавиши:  +/-  яркость   [ ]  гамма   , .  порог свечения"
        + chr(10) +
        "            m  режим цвета   p  пауза   q  выход")


def start_audio(timeout: float = 5.0):
    from audio import LoudnessMonitor
    mon = LoudnessMonitor()
    mon.start()
    mon.ready.wait(timeout=timeout)
    if mon.error:
        log.warning("Звук не поднялся (%s), иду без него", mon.error)
        mon.stop()
        return None
    log.info("Звук: %s, %d Гц", mon.device, mon.rate)
    return mon


def build_sink(args, n_zones: int) -> Sink | None:
    if args.dry_run:
        return None
    if args.wled:
        return DDPSink(args.wled, pixels=args.pixels or n_zones)
    return BLESink(args.address, fps=args.fps, use_ww=args.ww,
                   keepalive=args.keepalive)


async def run(args) -> int:
    profile = OutputProfile.load(args.profile)
    stop = threading.Event()
    sampler = Sampler(args, stop)
    sampler.start()
    sampler.ready.wait(timeout=5.0)
    zones = sampler.zones
    n = len(zones)

    sink = build_sink(args, n)
    if sink is not None:
        target = args.wled or ", ".join(args.address)
        print("Подключаюсь: %s" % target)
        try:
            opened = await sink.open()
        except (asyncio.CancelledError, KeyboardInterrupt):
            stop.set()
            await sink.close(turn_off=False)
            raise
        if not opened:
            if args.wled:
                print("WLED по адресу %s не найден. Это адрес для адресной "
                      "ленты с ESP32; для вашей BLE-ленты укажите MAC."
                      % args.wled)
            else:
                print("Не подключился. Выключи Bluetooth на телефоне, "
                      "проверь питание.")
            stop.set()
            await sink.close(turn_off=False)
            return 1
        print("Подключено: %s" % sink.label())
        if n > sink.capacity:
            log.warning("Зон %d, а приёмник тянет %d - соседние будут "
                        "усредняться", n, sink.capacity)
    else:
        print("Режим --dry-run: в ленту ничего не уходит.")
    print(HELP + "\n")

    audio = None

    colored = enable_vt()
    pipe = ScenePipeline(args, n, profile)
    pipe.reset(sampler.targets)
    schedulers = [SendScheduler(fps=args.fps, deadband=args.deadband,
                                keepalive=args.keepalive) for _ in range(n)]
    paused = False
    period = 1.0 / args.fps
    mode_idx = list(MODES).index(args.mode) if args.mode in MODES else 1
    seen_cut = sampler.cut_seq
    started = time.monotonic()
    last_status = last_tick = started
    prev_frames = 0
    prev_at = started
    codes = [(0, 0, 0)] * n
    cuts = 0
    audio_retry_at = 0.0

    try:
        while not stop.is_set():
            tick = time.perf_counter()
            now = time.monotonic()
            dt = min(0.5, max(1e-3, now - last_tick))
            last_tick = now

            key = getkey()
            if key:
                if key == "q":
                    break
                elif key in ("+", "="):
                    pipe.brightness = min(1.0, pipe.brightness + 0.05)
                elif key == "-":
                    pipe.brightness = max(0.05, pipe.brightness - 0.05)
                elif key == "m":
                    mode_idx = (mode_idx + 1) % len(MODES)
                    sampler.mode = MODES[mode_idx]
                elif key == "]":
                    pipe.gamma_out = min(4.0, pipe.gamma_out + 0.1)
                elif key == "[":
                    pipe.gamma_out = max(0.6, pipe.gamma_out - 0.1)
                elif key == ".":
                    pipe.min_level = min(0.9, pipe.min_level + 0.05)
                elif key == ",":
                    pipe.min_level = max(0.0, pipe.min_level - 0.05)
                elif key == "p":
                    paused = not paused

            if audio is not None and not audio.is_alive():
                log.warning("Звук: %s - перезапускаю", audio.error or "поток вышел")
                audio, audio_retry_at = None, now
            if args.audio and audio is None and now >= audio_retry_at:
                audio = start_audio(timeout=2.0)
                audio_retry_at = now + 30.0

            if not paused:
                force_cut = sampler.cut_seq != seen_cut
                if force_cut:
                    cuts += 1
                seen_cut = sampler.cut_seq
                new_codes, is_cuts, fired = pipe.step(
                    sampler.targets, now, dt, force_cut, mode=sampler.mode,
                    frame_diff=sampler.last_diff,
                    audio_level=None if audio is None else audio.level)
                if fired:
                    log.info("вспышка #%d в %s: ярк. x%.1f от базы",
                             pipe.flashes, time.strftime("%H:%M:%S"),
                             pipe.flash_ratio)
                changed = False
                for i in range(n):
                    c = new_codes[i]
                    if schedulers[i].decide(c, now, is_cuts[i]):
                        schedulers[i].mark_sent(c, now, is_cuts[i])
                        codes[i] = c
                        changed = True
                if changed and sink is not None:
                    await sink.send(codes)

            if not args.quiet and now - last_status >= 0.4:
                elapsed = max(1e-6, now - prev_at)
                cap_fps = (sampler.frames - prev_frames) / elapsed
                prev_frames, prev_at = sampler.frames, now
                total = max(1.0, now - started)
                sent = sum(s.sent for s in schedulers)
                state = ("ПАУЗА" if paused else "dry-run" if sink is None
                         else sink.label())
                snd = "" if audio is None else "звук%3d%% " % round(audio.level * 100)
                flh = "вспышек %d  " % pipe.flashes if pipe.flash_gate_on else ""
                idl = "дрейф  " if pipe.idle_life_on and pipe.idle.active else ""
                cst = ("конт x%.2f  " % pipe.contrast_gain
                       if pipe.auto_contrast_on else "")
                act = ("tau x%.2f  " % pipe.tau_factor
                       if pipe.adaptive_tau_on else "")
                sys.stdout.write(
                    "\r%s%-8s ярк%3d%% γ%.1f мин%2d%% %s захват %4.1f/с "
                    "(%4.1f мс %s)  отправок %4.1f/с  склеек %d  %s%s%s%s%s   "
                    % (swatches(codes, colored), MODES[mode_idx],
                       round(pipe.brightness * 100), pipe.gamma_out,
                       round(pipe.min_level * 100), snd, cap_fps, sampler.last_ms,
                       sampler.backend, sent / total, cuts, flh, idl, cst, act,
                       state))
                sys.stdout.flush()
                last_status = now

            await asyncio.sleep(max(0.0, period - (time.perf_counter() - tick)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        print("\n\nОстанавливаюсь...")
        print("  кадров %d, ошибок захвата %d, склеек %d%s"
              % (sampler.frames, sampler.errors, cuts,
                 ", вспышек %d" % pipe.flashes if pipe.flash_gate_on else ""))
        if pipe.dominant_hold_on:
            print("  dominant: подавлено переключений оттенка %d"
                  % pipe.suppressed_hue_switches)
        print("  зон %d, отправок %d"
              % (n, sum(s.sent for s in schedulers)))
        if audio is not None:
            audio.stop()
            print("  звук: блоков %d" % audio.blocks)
        if sink is not None:
            await sink.close(turn_off=not args.keep)
            print("  %s" % sink.stats())
        sampler.join(timeout=2.0)
    return 0


def list_monitors() -> int:
    from capture import _open_mss
    with _open_mss() as sct:
        print("Экраны (номер для --monitor):")
        for i, m in enumerate(sct.monitors):
            kind = "все вместе" if i == 0 else (
                "основной" if m.get("is_primary") else "дополнительный")
            print("  %d: %dx%d при (%d,%d)  %s"
                  % (i, m["width"], m["height"], m["left"], m["top"], kind))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ambilight: цвет экрана -> светодиодная лента",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("address", nargs="*", default=[],
                   help="MAC-адреса лент, по одному на зону")
    p.add_argument("--wled", metavar="HOST",
                   help="слать в WLED по DDP вместо BLE")
    p.add_argument("--pixels", type=int, default=None,
                   help="сколько диодов у адресной ленты")
    p.add_argument("--layout", default="single",
                   help="single, lr, lcr, quad, edges, strips:N, "
                        "perimeter:сверху,справа,снизу,слева")
    p.add_argument("--preset", choices=sorted(PRESETS), default="film")
    p.add_argument("--list-monitors", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--profile", default=DEFAULT_PROFILE_PATH)

    g = p.add_argument_group("захват")
    g.add_argument("--backend", choices=("auto", "dxcam", "mss"), default="auto")
    g.add_argument("--monitor", type=int, default=1)
    g.add_argument("--capture-fps", type=float, default=None)
    g.add_argument("--step", type=int, default=None)
    g.add_argument("--crop-bars", dest="crop_bars", action="store_true",
                   default=None)
    g.add_argument("--no-crop-bars", dest="crop_bars", action="store_false")
    g.add_argument("--cut-detect", dest="cut_detect", action="store_true",
                   default=None)
    g.add_argument("--no-cut-detect", dest="cut_detect", action="store_false")

    g = p.add_argument_group("цвет")
    g.add_argument("--mode", choices=MODES, default=None)
    g.add_argument("--dominant-hold", dest="dominant_hold",
                   action="store_true", default=None,
                   help="гасить дрожание режима dominant между "
                        "конкурирующими оттенками (порог не откалиброван)")
    g.add_argument("--no-dominant-hold", dest="dominant_hold",
                   action="store_false")
    g.add_argument("--dominant-hold-distance", type=float, default=0.06,
                   help="хорда в Oklab, ниже которой оттенок не переключается")
    g.add_argument("--brightness", type=float, default=None)
    g.add_argument("--gamma-out", type=float, default=None,
                   help="1.0 - честная яркость, выше - подсветка тёмных сцен")
    g.add_argument("--min-level", type=float, default=None,
                   help="нижняя граница свечения, доля от максимума")
    g.add_argument("--ww", action="store_true", default=False,
                   help="яркость четвёртым байтом (проверьте hw_ww.py)")
    g.add_argument("--audio", action="store_true",
                   help="подмешивать громкость системного звука в яркость")
    g.add_argument("--audio-depth", type=float, default=0.6,
                   help="насколько сильно звук управляет яркостью, 0..1")
    g.add_argument("--dark-gate", dest="dark_gate", action="store_true",
                   default=None)
    g.add_argument("--no-dark-gate", dest="dark_gate", action="store_false")

    g = p.add_argument_group("вспышки (пороги не откалиброваны)")
    g.add_argument("--flash-gate", dest="flash_gate", action="store_true",
                   default=None,
                   help="реагировать на резкие вспышки в обход сглаживания")
    g.add_argument("--no-flash-gate", dest="flash_gate", action="store_false")
    g.add_argument("--flash-ratio", type=float, default=2.2,
                   help="во сколько раз ярче своей скользящей базы - вспышка")
    g.add_argument("--flash-floor", type=float, default=0.02,
                   help="ниже этой светлоты вспышки не ищем (шум на тёмном)")
    g.add_argument("--flash-baseline-tau", type=float, default=0.6,
                   help="как быстро база забывает недавнюю яркость сцены")
    g.add_argument("--flash-release-ratio", type=float, default=1.3,
                   help="ниже этого от базы - вспышка кончилась")
    g.add_argument("--flash-hold", type=float, default=0.22,
                   help="держать пиковый цвет принудительно столько секунд")

    g = p.add_argument_group("оживление в статике (пороги не откалиброваны)")
    g.add_argument("--idle-life", dest="idle_life", action="store_true",
                   default=None,
                   help="медленный дрейф цвета на неподвижной картинке")
    g.add_argument("--no-idle-life", dest="idle_life", action="store_false")
    g.add_argument("--idle-dwell", type=float, default=6.0,
                   help="сколько секунд неподвижности до активации")
    g.add_argument("--idle-still-distance", type=float, default=0.01,
                   help="порог Oklab, ниже которого картинка - неподвижная")
    g.add_argument("--idle-period-min", type=float, default=4.0)
    g.add_argument("--idle-period-max", type=float, default=7.0)
    g.add_argument("--idle-hue-jitter", type=float, default=0.07,
                   help="радианы, амплитуда качания оттенка")
    g.add_argument("--idle-light-jitter", type=float, default=0.08,
                   help="доля от светлоты, амплитуда качания яркости")

    g = p.add_argument_group("адаптивный контраст (экспериментально)")
    g.add_argument("--auto-contrast", dest="auto_contrast",
                   action="store_true", default=None,
                   help="растягивать контраст внутри текущей сцены")
    g.add_argument("--no-auto-contrast", dest="auto_contrast",
                   action="store_false")
    g.add_argument("--contrast-strength", type=float, default=0.35,
                   help="сила эффекта, 0..1 - итоговый gain зажат в "
                        "[1-strength/2, 1+strength/2]")
    g.add_argument("--contrast-expand", type=float, default=0.30,
                   help="как быстро коридор гонится за новым экстремумом")
    g.add_argument("--contrast-contract", type=float, default=0.01,
                   help="как быстро коридор сжимается без экстремума")
    g.add_argument("--contrast-min-span", type=float, default=1.5,
                   help="минимальная ширина коридора в log2, чтобы тихая "
                        "сцена не раскачивалась от шума")

    g = p.add_argument_group("адаптивное сглаживание (пороги не откалиброваны)")
    g.add_argument("--adaptive-tau", dest="adaptive_tau", action="store_true",
                   default=None,
                   help="ускорять tau на экшн-сценах, замедлять на спокойных")
    g.add_argument("--no-adaptive-tau", dest="adaptive_tau",
                   action="store_false")
    g.add_argument("--activity-smoothing", type=float, default=1.0,
                   help="постоянная времени EMA активности сцены, секунд")
    g.add_argument("--activity-calm", type=float, default=2.0,
                   help="frame_difference ниже этого - сцена спокойная")
    g.add_argument("--activity-busy", type=float, default=15.0,
                   help="frame_difference выше этого - сцена экшн")
    g.add_argument("--tau-factor-min", type=float, default=0.6,
                   help="во сколько раз ускорять tau на пике активности")
    g.add_argument("--tau-factor-max", type=float, default=1.6,
                   help="во сколько раз замедлять tau в полном покое")

    g = p.add_argument_group("динамика")
    g.add_argument("--fps", type=float, default=None,
                   help="максимум команд в секунду на канал")
    g.add_argument("--tau-up", type=float, default=None)
    g.add_argument("--tau-down", type=float, default=None)
    g.add_argument("--autospeed", type=float, default=None)
    g.add_argument("--cut-distance", type=float, default=CUT_DISTANCE)
    g.add_argument("--deadband", type=int, default=2)
    g.add_argument("--keepalive", type=float, default=20.0)
    g.add_argument("--keep", action="store_true",
                   help="не гасить ленту при выходе")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def apply_preset(args) -> None:
    for key, value in PRESETS[args.preset].items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = build_parser()
    args = p.parse_args()
    apply_preset(args)

    handler = CleanLineHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    if args.list_monitors:
        return list_monitors()
    if not args.address and not args.wled and not args.dry_run:
        p.error("укажи MAC-адрес ленты, или --wled HOST, или --dry-run")
    if args.step < 1:
        p.error("--step должен быть >= 1")
    try:
        make_layout(args.layout)
    except ValueError as e:
        p.error(str(e))
    if args.fps > 25 and not args.wled:
        log.warning("--fps %.0f выше пропускной способности BLE (~20/с). "
                    "Пакеты будут копиться, и лента начнёт отставать.", args.fps)

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

