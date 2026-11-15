from __future__ import annotations

import logging
import math
import sys
import threading
import time

import numpy as np

log = logging.getLogger("audio")

CHUNK = 512
WINDOW = 1024
MIN_RANGE_DB = 12.0
DEVICE_CHECK_S = 3.0


def default_output_id():
    try:
        import ctypes
        from ctypes import POINTER, c_int, c_void_p
        from ctypes.wintypes import DWORD
        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    except ImportError:
        return None

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                      (["in"], DWORD, "ctx"), (["in"], c_void_p, "params"),
                      (["out"], POINTER(c_void_p), "iface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "mode"),
                      (["out"], POINTER(c_void_p), "store")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "id")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "flow"),
                      (["in"], DWORD, "mask"), (["out"], POINTER(c_void_p), "devs")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], c_int, "flow"), (["in"], c_int, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "dev")),
        ]

    clsid = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = (c_void_p,)

    def current():
        try:
            enum = comtypes.CoCreateInstance(clsid, IMMDeviceEnumerator,
                                             comtypes.CLSCTX_ALL)
            dev = enum.GetDefaultAudioEndpoint(0, 0)
            ptr = dev.GetId()
            try:
                return ctypes.wstring_at(ptr)
            finally:
                free(ptr)
        except Exception:
            return None
    return current


def a_weights(freqs: np.ndarray) -> np.ndarray:
    f2 = np.maximum(freqs, 1e-6) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2)
        * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2))
    return (ra * (10.0 ** (2.0 / 20.0))).astype(np.float32)


class LoudnessMonitor(threading.Thread):
    def __init__(self, attack: float = 0.25, release: float = 0.06,
                 keepalive: bool = True):
        super().__init__(name="audio", daemon=True)
        self.attack = attack
        self.release = release
        self.keepalive = keepalive
        self.level = 0.0
        self.db = -120.0
        self.blocks = 0
        self.rate = 0
        self.device = ""
        self.error: str | None = None
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._last_block = 0.0
        self._floor = -70.0
        self._ceil = -30.0

    def stop(self) -> None:
        self._stop.set()


    def _update_level(self, db: float) -> None:
        if db > self._ceil:
            self._ceil += (db - self._ceil) * 0.30
        else:
            self._ceil += (db - self._ceil) * 0.0015
        if db < self._floor:
            self._floor += (db - self._floor) * 0.30
        else:
            self._floor += (db - self._floor) * 0.0015
        span = max(MIN_RANGE_DB, self._ceil - self._floor)
        target = min(1.0, max(0.0, (db - self._floor) / span))
        k = self.attack if target > self.level else self.release
        self.level += (target - self.level) * k

    def run(self) -> None:
        try:
            import pyaudiowpatch as pa
        except ImportError:
            self.error = "нет pyaudiowpatch: python -m pip install pyaudiowpatch"
            self.ready.set()
            return

        audio = pa.PyAudio()
        stream = silence = None
        com = device_id = None
        try:
            import comtypes
            comtypes.CoInitialize()
            com = comtypes
            device_id = default_output_id()
        except ImportError:
            pass
        started_on = device_id() if device_id is not None else None
        next_check = time.monotonic() + DEVICE_CHECK_S
        try:
            info = audio.get_default_wasapi_loopback()
            self.device = str(info.get("name", "?"))
            rate = int(info["defaultSampleRate"])
            channels = max(1, int(info["maxInputChannels"]))
            self.rate = rate

            freqs = np.fft.rfftfreq(WINDOW, 1.0 / rate)
            weights = a_weights(freqs)
            hann = np.hanning(WINDOW).astype(np.float32)
            state = {"ring": np.zeros(WINDOW, dtype=np.float32)}

            def on_block(in_data, frame_count, time_info, status):
                block = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
                if channels > 1:
                    block = block.reshape(-1, channels).mean(axis=1)
                block /= 32768.0
                ring = np.concatenate((state["ring"][len(block):], block))[-WINDOW:]
                state["ring"] = ring
                spec = np.abs(np.fft.rfft(ring * hann)) * weights
                power = float(np.dot(spec, spec)) / WINDOW
                self.db = 10.0 * math.log10(power + 1e-12)
                self._update_level(self.db)
                self.blocks += 1
                self._last_block = time.monotonic()
                return (None, pa.paContinue)

            stream = audio.open(format=pa.paInt16, channels=channels,
                                rate=rate, input=True,
                                input_device_index=int(info["index"]),
                                frames_per_buffer=CHUNK,
                                stream_callback=on_block)
            if self.keepalive:
                silence = self._open_silence(audio, pa)
            self.ready.set()

            while not self._stop.is_set():
                self._stop.wait(0.05)
                if started_on is not None and time.monotonic() >= next_check:
                    next_check = time.monotonic() + DEVICE_CHECK_S
                    now_on = device_id()
                    if now_on is not None and now_on != started_on:
                        self.error = "сменилось устройство вывода"
                        log.info("Звук: сменилось устройство вывода - "
                                 "монитор перезапустится")
                        break
                quiet = time.monotonic() - self._last_block
                if quiet > 0.3:
                    self.level += (0.0 - self.level) * self.release
                    self.db = -120.0
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            log.warning("Звук: %s", self.error)
            self.ready.set()
        finally:
            for st in (stream, silence):
                try:
                    if st is not None:
                        st.stop_stream()
                        st.close()
                except Exception:
                    pass
            try:
                audio.terminate()
            except Exception:
                pass
            if com is not None:
                try:
                    com.CoUninitialize()
                except Exception:
                    pass

    def _open_silence(self, audio, pa):
        try:
            out = audio.get_device_info_by_index(
                audio.get_host_api_info_by_type(pa.paWASAPI)["defaultOutputDevice"])
            ch = max(1, int(out["maxOutputChannels"]))

            def feed(in_data, frame_count, time_info, status):
                return (b"\x00" * (frame_count * 2 * ch), pa.paContinue)

            return audio.open(format=pa.paInt16, channels=ch,
                              rate=int(out["defaultSampleRate"]), output=True,
                              output_device_index=int(out["index"]),
                              frames_per_buffer=CHUNK, stream_callback=feed)
        except Exception as e:
            log.info("Тишина-пустышка не открылась (%s), при молчании звука "
                     "может не быть блоков", e)
            return None


def modulate(brightness: float, level: float, depth: float,
             floor: float = 0.35) -> float:
    if depth <= 0.0:
        return brightness
    shaped = floor + (1.0 - floor) * level
    return brightness * (1.0 - depth + depth * shaped)


def _demo() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    mon = LoudnessMonitor()
    mon.start()
    mon.ready.wait(timeout=5.0)
    if mon.error:
        print("Не вышло:", mon.error)
        return 1
    print("Устройство: %s, %d Гц" % (mon.device, mon.rate))
    print("Включите музыку или видео. Ctrl+C - выход.\n")
    try:
        while True:
            filled = int(round(mon.level * 40))
            print("\r  [%s%s] %.2f   %6.1f дБ   коридор %.0f..%.0f   блоков %d  "
                  % ("#" * filled, "-" * (40 - filled), mon.level, mon.db,
                     mon._floor, mon._ceil, mon.blocks), end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n")
    finally:
        mon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_demo())

