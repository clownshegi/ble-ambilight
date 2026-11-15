from __future__ import annotations

import asyncio
import logging
import sys
import threading

log = logging.getLogger("winevents")

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

SUSPEND_BUDGET = 1.5

BLOCKING = ("suspend", "shutdown")


def translate(msg: int, wparam: int) -> str | None:
    if msg == WM_POWERBROADCAST:
        if wparam == PBT_APMSUSPEND:
            return "suspend"
        if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            return "resume"
    elif msg == WM_WTSSESSION_CHANGE:
        if wparam == WTS_SESSION_LOCK:
            return "lock"
        if wparam == WTS_SESSION_UNLOCK:
            return "unlock"
    elif msg == WM_ENDSESSION and wparam:
        return "shutdown"
    return None


class SystemEvents(threading.Thread):
    def __init__(self, callback, name: str = "LampSuiteSystemEvents"):
        super().__init__(name="winevents", daemon=True)
        self.callback = callback
        self.class_name = name
        self.hwnd = None
        self.ready = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            log.warning("Сон/блокировка ПК отслеживаться не будут: %s",
                        self.error)
        finally:
            self.ready.set()

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        u32.DefWindowProcW.restype = LRESULT
        u32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
        u32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        u32.RegisterClassW.restype = wintypes.ATOM
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        u32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT)
        u32.GetMessageW.restype = wintypes.BOOL
        u32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u32.DestroyWindow.argtypes = (wintypes.HWND,)
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        k32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_QUERYENDSESSION:
                    return 1
                if msg == WM_CLOSE:
                    u32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    u32.PostQuitMessage(0)
                    return 0
                event = translate(msg, wparam)
                if event is not None:
                    self.callback(event)
                    if msg == WM_POWERBROADCAST:
                        return 1
            except Exception as e:
                log.warning("Событие Windows не обработано: %s", e)
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.class_name
        if not u32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = u32.CreateWindowExW(0, self.class_name, self.class_name, 0,
                                   0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd
        try:
            wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
            wts.WTSRegisterSessionNotification.argtypes = (wintypes.HWND,
                                                           wintypes.DWORD)
            if not wts.WTSRegisterSessionNotification(hwnd,
                                                      NOTIFY_FOR_THIS_SESSION):
                log.warning("Блокировка ПК отслеживаться не будет")
        except OSError as e:
            log.warning("Блокировка ПК отслеживаться не будет: %s", e)
        self.ready.set()

        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


def input_idle_seconds() -> float:
    if sys.platform != "win32":
        return 0.0
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def foreground_exe() -> str:
    if sys.platform != "win32":
        return ""
    import ctypes
    import os
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(handle)


def attach(controller, loop: asyncio.AbstractEventLoop) -> SystemEvents | None:
    if sys.platform != "win32":
        return None

    def on_event(event: str) -> None:
        log.info("Windows: %s", event)
        fut = asyncio.run_coroutine_threadsafe(controller.on_system(event), loop)
        if event in BLOCKING:
            try:
                fut.result(SUSPEND_BUDGET)
            except Exception as e:
                log.warning("Перед сном лента не успела погаснуть: %r", e)

    events = SystemEvents(on_event)
    events.start()
    return events

