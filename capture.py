from __future__ import annotations

import gc
import logging
import time

import numpy as np

log = logging.getLogger("capture")


def _open_mss():
    import mss
    factory = getattr(mss, "MSS", None) or mss.mss
    return factory()


class ContentRegion:
    def __init__(self, threshold: int = 13, switch_frames: int = 25,
                 max_inconsistent: int = 5, min_fraction: float = 0.25):
        self.threshold = threshold
        self.switch_frames = switch_frames
        self.max_inconsistent = max_inconsistent
        self.min_fraction = min_fraction
        self.box: tuple[int, int, int, int] | None = None
        self._pending: tuple[int, int, int, int] | None = None
        self._agree = 0
        self._disagree = 0
        self.unknown_frames = 0
        self.switches = 0

    def update(self, frame: np.ndarray) -> tuple[int, int, int, int]:
        h, w = frame.shape[:2]
        if self.box is None:
            self.box = (0, h, 0, w)
        found = self._detect(frame, h, w)
        if found is None:
            self.unknown_frames += 1
            return self.box
        if found == self.box:
            self._pending = None
            self._agree = self._disagree = 0
            return self.box
        if found == self._pending:
            self._agree += 1
            if self._agree >= self.switch_frames:
                self.box = found
                self.switches += 1
                self._pending = None
                self._agree = self._disagree = 0
        else:
            self._disagree += 1
            if self._pending is None or self._disagree > self.max_inconsistent:
                self._pending = found
                self._agree = 1
                self._disagree = 0
        return self.box

    def _detect(self, frame: np.ndarray, h: int,
                w: int) -> tuple[int, int, int, int] | None:
        if h < 8 or w < 8:
            return None
        cols_at = (w // 4, w // 2, 3 * w // 4)
        rows_at = (h // 4, h // 2, 3 * h // 4)
        row_max = frame[:, cols_at, :3].max(axis=(1, 2))
        col_max = frame[rows_at, :, :3].max(axis=(0, 2))
        lit_rows = np.flatnonzero(row_max >= self.threshold)
        lit_cols = np.flatnonzero(col_max >= self.threshold)
        if lit_rows.size == 0 or lit_cols.size == 0:
            return None
        t, b = int(lit_rows[0]), int(lit_rows[-1]) + 1
        l, r = int(lit_cols[0]), int(lit_cols[-1]) + 1
        if (b - t) < h * self.min_fraction or (r - l) < w * self.min_fraction:
            return None
        return t, b, l, r

    def describe(self, h: int, w: int) -> str:
        if self.box is None:
            return "весь кадр"
        t, b, l, r = self.box
        if (t, b, l, r) == (0, h, 0, w):
            return "весь кадр"
        return "поля: сверху %d, снизу %d, слева %d, справа %d" % (
            t, h - b, l, w - r)


class Capturer:
    def __init__(self, backend: str = "auto", monitor: int = 1, step: int = 8,
                 crop_bars: bool = True, stall_seconds: float = 4.0):
        self.step = max(1, step)
        self.monitor = monitor
        self.region = ContentRegion() if crop_bars else None
        self.stall_seconds = stall_seconds
        self.backend = ""
        self._cam = None
        self._sct = None
        self._mon = None
        self.width = self.height = 0
        self.unchanged = 0
        self.grabbed = 0
        self.fallbacks = 0
        self._last_frame_at = 0.0

        if backend in ("auto", "dxcam"):
            if self._try_dxcam(monitor):
                return
            if backend == "dxcam":
                raise RuntimeError("dxcam недоступен")
            log.info("dxcam не поднялся, откатываюсь на mss")
        self._start_mss(monitor)


    def _try_dxcam(self, monitor: int) -> bool:
        try:
            import dxcam
        except ImportError:
            return False
        try:
            output_idx = max(0, monitor - 1)
            cam = dxcam.create(output_idx=output_idx, output_color="BGRA")
            if cam is None:
                return False
            probe = cam.grab()
            if probe is None:
                if not getattr(cam, "width", 0):
                    return False
            self._cam = cam
            self.width, self.height = cam.width, cam.height
            self.backend = "dxcam"
            return True
        except Exception as e:
            log.info("dxcam недоступен: %s", e)
            return False

    def _start_mss(self, monitor: int) -> None:
        self._sct = _open_mss()
        self._mon = self._sct.monitors[monitor]
        self.width = self._mon["width"]
        self.height = self._mon["height"]
        self.backend = "mss"

    def _ensure_mss(self) -> None:
        if self._sct is None:
            self._sct = _open_mss()
            self._mon = self._sct.monitors[self.monitor]

    def _grab_mss_full(self) -> np.ndarray:
        self._ensure_mss()
        shot = self._sct.grab(self._mon)
        buf = np.frombuffer(shot.bgra, dtype=np.uint8)
        return buf.reshape(shot.height, shot.width, 4)

    def _reduce(self, full: np.ndarray) -> np.ndarray:
        if self.region is not None:
            t, b, l, r = self.region.update(full)
            full = full[t:b, l:r]
        return full[::self.step, ::self.step, :3]


    def grab(self) -> np.ndarray | None:
        now = time.monotonic()
        if self._cam is not None:
            frame = self._cam.grab()
            if frame is not None:
                self._last_frame_at = now
            elif now - self._last_frame_at < self.stall_seconds:
                self.unchanged += 1
                return None
            else:
                self.fallbacks += 1
                self._last_frame_at = now
                frame = self._grab_mss_full()
        else:
            frame = self._grab_mss_full()
        self.grabbed += 1
        return self._reduce(frame)

    def close(self) -> None:
        if self._cam is not None:
            cam, self._cam = self._cam, None
            try:
                cam.release()
            except Exception:
                pass
            del cam
            gc.collect()
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None

    def __enter__(self) -> "Capturer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

