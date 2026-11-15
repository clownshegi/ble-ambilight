from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

from aiohttp import web

import winevents
from icon import icon_png
from webapp import (EVENT_DEFS, GATE_DEFS, MODE_DEFS, PRESET_DEFS, SYNC_FLAGS,
                    build_app, lan_ip, load_config, make_controller,
                    setup_logging)

log = logging.getLogger("desktop")

WINDOW_TITLE = "Лента"

BG = "#0b0a0d"
SURFACE = "#151419"
SURFACE_HOVER = "#1c1a21"
LINE = "#24222a"
FG = "#f3f1f4"
DIM = "#9d9aa5"
FAINT = "#5d5a64"
WARN = "#e6b566"
IDLE = "#8a8794"
FONT = "Segoe UI"

_MUTEX = None


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def mix(a, b, t: float) -> str:
    a = hex_rgb(a) if isinstance(a, str) else a
    b = hex_rgb(b) if isinstance(b, str) else b
    return rgb_hex(tuple(x + (y - x) * t for x, y in zip(a, b)))


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def claim_single_instance(name: str = "Local\\LampSuite.Lamp") -> bool:
    global _MUTEX
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:
        k32.CloseHandle(handle)
        return False
    if _MUTEX is None:
        _MUTEX = handle
    return True


def activate_existing_window() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.FindWindowW.restype = wintypes.HWND
    u32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    hwnd = u32.FindWindowW("TkTopLevel", WINDOW_TITLE)
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, 9)
    u32.SetForegroundWindow(hwnd)
    return True


class AsyncBridge:
    def __init__(self, config: dict):
        self.controller = make_controller(config["address"], config)
        self.port = config["port"]
        self.loop = asyncio.new_event_loop()
        self.web_error: str | None = None
        self._runner: web.AppRunner | None = None
        self._closing = False
        self._events = None
        self._thread = threading.Thread(target=self._run, name="asyncio",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._events = winevents.attach(self.controller, self.loop)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start_server())
        except OSError as e:
            self.web_error = ("страница для телефона не запустилась: порт %d "
                              "занят" % self.port)
            log.error("Веб-сервер не запустился: %s", e)
            self.loop.run_until_complete(self.controller.warm_up())
        except Exception as e:
            self.web_error = "страница для телефона не запустилась: %s" % e
            log.exception("Веб-сервер не запустился")
            self.loop.run_until_complete(self.controller.warm_up())
        while True:
            try:
                self.loop.run_forever()
            except BaseException as e:
                log.error("Фоновый цикл: %r - продолжаю", e)
                if not self._closing:
                    continue
            break

    async def _start_server(self) -> None:
        app = build_app(self.controller)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()

    def call(self, make_coro, root: tk.Tk, on_done, on_error,
             timeout: float = 20.0) -> None:
        future = asyncio.run_coroutine_threadsafe(make_coro(), self.loop)
        settled = [False]

        def settle(fn, value) -> None:
            if not settled[0]:
                settled[0] = True
                fn(value)

        def to_tk(fn, value) -> None:
            try:
                root.after(0, lambda: settle(fn, value))
            except (RuntimeError, tk.TclError):
                pass

        def done(f) -> None:
            try:
                result = f.result()
            except BaseException as e:
                to_tk(on_error, e)
                return
            to_tk(on_done, result)

        future.add_done_callback(done)

        def watchdog() -> None:
            if not future.done():
                settle(on_error, TimeoutError(
                    "фон не ответил за %.0f с - подробности в журнале" % timeout))
        root.after(int(timeout * 1000), watchdog)

    def post(self, fn, *args) -> None:
        def run() -> None:
            try:
                fn(*args)
            except Exception as e:
                log.warning("Команда из окна не применилась: %s", e)
        self.loop.call_soon_threadsafe(run)

    def shutdown(self, timeout: float = 8.0) -> None:
        self._closing = True
        if self._events is not None:
            self._events.close()

        async def stop() -> None:
            try:
                await self.controller.shutdown()
            finally:
                if self._runner is not None:
                    try:
                        await self._runner.cleanup()
                    except Exception:
                        pass
        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout)
        except Exception as e:
            log.warning("Остановка при выходе: %s", e)
        self.loop.call_soon_threadsafe(self.loop.stop)


class Tile:
    def __init__(self, app: "App", parent, title: str, hint: str, command,
                 tone: str | None = None):
        self.app, self.command, self.tone = app, command, tone
        self.base = mix(SURFACE, tone, 0.22) if tone else SURFACE
        self.frame = tk.Frame(parent, bg=self.base, highlightthickness=1,
                              highlightbackground=LINE, cursor="hand2")
        self.title = tk.Label(self.frame, text=title, bg=self.base, fg=FG,
                              font=(FONT, 11, "bold"), anchor="w")
        self.title.pack(fill="x", padx=app.px(12), pady=(app.px(10), 0))
        self.hint = tk.Label(self.frame, text=hint or " ", bg=self.base,
                             fg=DIM, font=(FONT, 8), anchor="w")
        self.hint.pack(fill="x", padx=app.px(12), pady=(0, app.px(9)))
        self.active = False
        self.enabled = True
        for w in (self.frame, self.title, self.hint):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", lambda _e: self._paint(hover=True))
            w.bind("<Leave>", lambda _e: self._paint())

    def _click(self, _event=None) -> None:
        if self.enabled:
            self.command()

    def set(self, active: bool, accent: str, enabled: bool) -> None:
        self.active, self.enabled = active, enabled
        self._accent = accent
        self._paint()

    def _paint(self, hover: bool = False) -> None:
        accent = getattr(self, "_accent", IDLE)
        if self.active:
            border = self.tone or accent
            bg = self.base if self.tone else mix(SURFACE, accent, 0.16)
        else:
            border = LINE
            bg = mix(self.base, "#ffffff", 0.04) if hover and self.enabled \
                else self.base
        fg = FG if self.enabled else FAINT
        self.frame.configure(bg=bg, highlightbackground=border,
                             highlightcolor=border)
        self.title.configure(bg=bg, fg=fg)
        self.hint.configure(bg=bg)


class App:
    POLL_MS = 1000
    LIGHT_MS = 50
    TOUCH_GRACE = 3.0
    GLOW_RINGS = 26

    def __init__(self, root: tk.Tk, bridge: AsyncBridge, log_path):
        self.root = root
        self.bridge = bridge
        self.controller = bridge.controller
        self.log_path = log_path
        self.busy = False
        self._touched: dict[str, float] = {}
        self._dragging = False
        self._bright_pending = None
        self._last_ip_check = -1e9
        self._status: dict = {}
        self._shown_rgb = hex_rgb(BG)
        self._shown_level = 0.0
        self._accent = IDLE
        self.s = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))

        root.title(WINDOW_TITLE)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._report_exception
        try:
            self._icon = tk.PhotoImage(data=base64.b64encode(icon_png(64)))
            root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

        self._build_ui()
        self._poll()
        self._tick_light()
        self.fit()

    def px(self, n: float) -> int:
        return int(round(n * self.s))


    def _section(self, title: str) -> tk.Frame:
        box = tk.Frame(self.root, bg=BG)
        box.pack(fill="x", padx=self.px(16), pady=(0, self.px(20)))
        if title:
            tk.Label(box, text=title, bg=BG, fg=DIM,
                     font=(FONT, 9, "bold")).pack(anchor="w",
                                                  pady=(0, self.px(8)))
        return box

    def _build_ui(self) -> None:
        px = self.px
        self.hero = tk.Canvas(self.root, bg=BG, highlightthickness=0,
                              width=px(380), height=px(150))
        self.hero.pack(fill="x")
        self._rings = [self.hero.create_oval(0, 0, 0, 0, fill=BG, outline="")
                       for _ in range(self.GLOW_RINGS)]
        self._title = self.hero.create_text(
            px(16), px(30), text=WINDOW_TITLE, anchor="w", fill=FG,
            font=(FONT, 20, "bold"))
        self._status_item = self.hero.create_text(
            px(16), px(70), text="…", anchor="nw", fill=FG,
            font=(FONT, 11, "bold"))
        self._note_item = self.hero.create_text(
            px(16), px(96), text="", anchor="nw", fill=WARN, font=(FONT, 9))
        self._power_ring = self.hero.create_oval(0, 0, 0, 0, outline=FAINT,
                                                 width=max(1, px(1.5)))
        self._power_arc = self.hero.create_arc(
            0, 0, 0, 0, start=120, extent=300, style="arc",
            outline=DIM, width=max(2, px(2.2)))
        self._power_bar = self.hero.create_line(0, 0, 0, 0, fill=DIM,
                                                width=max(2, px(2.2)),
                                                capstyle="round")
        for item in (self._power_ring, self._power_arc, self._power_bar):
            self.hero.tag_bind(item, "<Button-1>", lambda _e: self.do_power())
            self.hero.tag_bind(item, "<Enter>",
                               lambda _e: self.hero.configure(cursor="hand2"))
            self.hero.tag_bind(item, "<Leave>",
                               lambda _e: self.hero.configure(cursor=""))
        self.hero.bind("<Configure>", lambda _e: self._layout_hero())

        box = self._section("Яркость")
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")
        self.bright_var = tk.IntVar(value=100)
        self.bright_label = tk.Label(row, text="100%", bg=BG, fg=DIM,
                                     font=(FONT, 10), width=5, anchor="e")
        self.bright_label.pack(side="right")
        self.scale = tk.Scale(row, from_=5, to=100, orient="horizontal",
                              variable=self.bright_var, showvalue=False,
                              bg="#77737f", troughcolor="#1d1b22", fg=FG,
                              highlightthickness=0, bd=0, relief="flat",
                              sliderrelief="flat", width=px(10),
                              sliderlength=px(22), activebackground=IDLE,
                              command=self._on_bright_move)
        self.scale.pack(side="left", fill="x", expand=True)
        self.scale.bind("<ButtonPress-1>", self._on_bright_press)
        self.scale.bind("<ButtonRelease-1>", self._on_bright_commit)
        self.scale.bind("<KeyRelease>", self._on_bright_commit)

        box = self._section("Экран")
        grid = tk.Frame(box, bg=BG)
        grid.pack(fill="x")
        self.preset_tiles: dict[str, Tile] = {}
        n = len(PRESET_DEFS)
        for i, (key, label, hint) in enumerate(PRESET_DEFS):
            grid.columnconfigure(i, weight=1, uniform="p")
            t = Tile(self, grid, label, hint, lambda k=key: self.do_sync(k))
            t.frame.grid(row=0, column=i, sticky="nsew",
                         padx=(0 if i == 0 else px(3), 0 if i == n - 1 else px(3)))
            self.preset_tiles[key] = t

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=(px(12), 0))
        tk.Label(row, text="Извлечение цвета", bg=BG, fg=FG,
                 font=(FONT, 10)).pack(side="left")
        self.mode_var = tk.StringVar(value=MODE_DEFS[0][1])
        self.mode_button = tk.Menubutton(
            row, text=MODE_DEFS[0][1] + "  ▾", bg=SURFACE, fg=FG,
            activebackground=SURFACE_HOVER, activeforeground=FG,
            highlightthickness=1, highlightbackground=LINE, relief="flat",
            bd=0, font=(FONT, 9), cursor="hand2", padx=px(10), pady=px(4))
        menu = tk.Menu(self.mode_button, tearoff=False, bg=SURFACE, fg=FG,
                       activebackground="#2a2831", activeforeground=FG,
                       font=(FONT, 9), bd=0)
        for _key, label in MODE_DEFS:
            menu.add_radiobutton(label=label, value=label,
                                 variable=self.mode_var,
                                 command=self._on_mode_change)
        self.mode_button["menu"] = menu
        self.mode_var.trace_add("write", lambda *_a: self.mode_button.configure(
            text=self.mode_var.get() + "  ▾"))
        self.mode_button.pack(side="right")

        self.more_open = False
        self.more_label = tk.Label(box, text="Дополнительно (экспериментальное) ▸",
                                   bg=BG, fg=DIM, font=(FONT, 9),
                                   cursor="hand2")
        self.more_label.pack(anchor="w", pady=(px(10), 0))
        self.more_label.bind("<Button-1>", lambda _e: self._toggle_more())
        self.more = tk.Frame(box, bg=BG)
        self.gate_vars: dict[str, tk.BooleanVar] = {}
        for key, label, _flag in GATE_DEFS:
            var = tk.BooleanVar()
            self.gate_vars[key] = var
            tk.Checkbutton(self.more, text=label, variable=var, bg=BG, fg=FG,
                           selectcolor=SURFACE, activebackground=BG,
                           activeforeground=FG, font=(FONT, 9), anchor="w",
                           bd=0, highlightthickness=0,
                           command=lambda k=key: self._on_gate_change(k)).pack(
                fill="x", pady=px(1))

        box = self._section("Атмосфера")
        grid = tk.Frame(box, bg=BG)
        grid.pack(fill="x")
        self.event_tiles: dict[str, Tile] = {}
        cols = 3
        for c in range(cols):
            grid.columnconfigure(c, weight=1, uniform="e")
        for i, (key, label, hint, tone) in enumerate(EVENT_DEFS):
            t = Tile(self, grid, label, hint, lambda k=key: self.do_event(k),
                     tone=tone)
            if i == 0:
                t.frame.grid(row=0, column=0, columnspan=cols, sticky="nsew")
            else:
                row, col = divmod(i - 1, cols)
                t.frame.grid(row=row + 1, column=col, sticky="nsew",
                             padx=(0 if col == 0 else px(3),
                                   0 if col == cols - 1 else px(3)),
                             pady=(px(6), 0))
            self.event_tiles[key] = t

        self.stop_link = tk.Label(self.root,
                                  text="Стоп - замереть на текущем цвете",
                                  bg=BG, fg=DIM, font=(FONT, 9, "underline"),
                                  cursor="hand2")
        self.stop_link.pack(anchor="w", padx=px(16))
        self.stop_link.bind("<Button-1>", lambda _e: self.do_stop())

        self.msg_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.msg_var, bg=BG, fg=DIM,
                 font=(FONT, 9), justify="left", anchor="w",
                 wraplength=px(340)).pack(fill="x", padx=px(16),
                                          pady=(px(6), 0))

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=px(16), pady=(px(10), px(14)))
        self.phone_text = ""
        self.foot_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.foot_var, bg=BG, fg=FAINT,
                 font=(FONT, 8), justify="left", anchor="w",
                 wraplength=px(340)).pack(fill="x")
        if self.log_path is not None:
            link = tk.Label(foot, text="журнал работы", bg=BG, fg=FAINT,
                            font=(FONT, 8, "underline"), cursor="hand2")
            link.pack(anchor="w", pady=(px(2), 0))
            link.bind("<Button-1>", self._open_log)

    def _layout_hero(self) -> None:
        px, c = self.px, self.hero
        w = max(c.winfo_width(), px(300))
        r = px(26)
        cx, cy = w - px(16) - r, px(34)
        c.coords(self._power_ring, cx - r, cy - r, cx + r, cy + r)
        g = px(10)
        c.coords(self._power_arc, cx - g, cy - g + px(2), cx + g, cy + g + px(2))
        c.coords(self._power_bar, cx, cy - px(12), cx, cy - px(2))
        c.itemconfigure(self._title, width=0)
        wrap = w - px(32)
        c.itemconfigure(self._status_item, width=wrap)
        c.itemconfigure(self._note_item, width=wrap)
        top = px(70)
        c.coords(self._status_item, px(16), top)
        bbox = c.bbox(self._status_item)
        note_top = (bbox[3] if bbox else top) + px(6)
        c.coords(self._note_item, px(16), note_top)
        nb = c.bbox(self._note_item) if c.itemcget(self._note_item, "text") \
            else None
        height = (nb[3] if nb else note_top) + px(18)
        if abs(int(c.cget("height")) - height) > 1:
            c.configure(height=height)
            self.root.after_idle(self.fit)
        n = len(self._rings)
        ex, ey = w / 2.0, -px(30)
        reach = height - ey
        for i, item in enumerate(self._rings):
            k = 1.0 - i / n
            rx, ry = w * 0.8 * k + px(12), reach * k
            c.coords(item, ex - rx, ey - ry, ex + rx, ey + ry)
        c.tag_raise(self._title)
        c.tag_raise(self._status_item)
        c.tag_raise(self._note_item)
        for item in (self._power_ring, self._power_arc, self._power_bar):
            c.tag_raise(item)
        self._paint_glow()

    def _paint_glow(self) -> None:
        n = len(self._rings)
        peak = 0.18 + 0.42 * self._shown_level if self._shown_level > 0.01 else 0
        for i, item in enumerate(self._rings):
            t = peak * (i / n) ** 1.7
            self.hero.itemconfigure(item, fill=mix(BG, self._shown_rgb, t))

    def fit(self) -> None:
        self.root.update_idletasks()
        w = max(self.px(380), self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        self.root.minsize(w, h)
        cur_w = max(w, self.root.winfo_width())
        self.root.geometry("%dx%d" % (cur_w, h))

    def _toggle_more(self) -> None:
        self.more_open = not self.more_open
        if self.more_open:
            self.more.pack(fill="x", pady=(self.px(4), 0))
        else:
            self.more.pack_forget()
        self.more_label.configure(text="Дополнительно (экспериментальное) "
                                  + ("▾" if self.more_open else "▸"))
        self.fit()


    def _touch(self, key: str) -> None:
        self._touched[key] = time.monotonic()

    def _fresh(self, key: str) -> bool:
        return time.monotonic() - self._touched.get(key, -1e9) > self.TOUCH_GRACE

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        self._paint_controls()
        self.stop_link.configure(fg=FAINT if busy else DIM)
        if text or not busy:
            self.msg_var.set(text)

    def _poll(self) -> None:
        try:
            self.refresh_status()
            now = time.monotonic()
            if now - self._last_ip_check >= 10.0:
                self._last_ip_check = now
                if self.bridge.web_error:
                    self.phone_text = self.bridge.web_error
                else:
                    self.phone_text = ("С телефона: http://%s:%d"
                                       % (lan_ip(), self.bridge.port))
                self._paint_footer()
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def _tick_light(self) -> None:
        try:
            light = self.controller.light()
            if light is None:
                target_rgb, target_level = self._shown_rgb, 0.0
            else:
                target_rgb, target_level = tuple(light["rgb"]), light["level"]
            k = 0.18
            rgb = tuple(a + (b - a) * k for a, b in
                        zip(self._shown_rgb, target_rgb))
            level = self._shown_level + (target_level - self._shown_level) * k
            changed = (max(abs(a - b) for a, b in zip(rgb, self._shown_rgb)) > 0.4
                       or abs(level - self._shown_level) > 0.003)
            self._shown_rgb, self._shown_level = rgb, level
            accent = rgb_hex(target_rgb) if light is not None else IDLE
            if accent != self._accent:
                self._accent = accent
                self._paint_controls()
            if changed:
                self._paint_glow()
        finally:
            self.root.after(self.LIGHT_MS, self._tick_light)

    def _paint_controls(self) -> None:
        st = self._status
        kind, name = st.get("kind"), st.get("name")
        enabled = not self.busy
        for key, t in self.preset_tiles.items():
            t.set(kind == "sync" and name == key, self._accent, enabled)
        for key, t in self.event_tiles.items():
            t.set(kind == "event" and name == key, self._accent, enabled)
        lit = st.get("power") is True
        c = self.hero
        if lit:
            c.itemconfigure(self._power_ring, fill=self._accent,
                            outline=self._accent)
            glyph = "#111014"
        else:
            c.itemconfigure(self._power_ring, fill=BG if enabled else BG,
                            outline=FAINT)
            glyph = DIM if enabled else FAINT
        c.itemconfigure(self._power_arc, outline=glyph)
        c.itemconfigure(self._power_bar, fill=glyph)
        self.scale.configure(activebackground=self._accent)

    def refresh_status(self) -> None:
        st = self.controller.status()
        self._status = st
        c = self.hero
        old = (c.itemcget(self._status_item, "text"),
               c.itemcget(self._note_item, "text"))
        new = (st["label"][:1].upper() + st["label"][1:], st.get("note") or "")
        if old != new:
            c.itemconfigure(self._status_item, text=new[0])
            c.itemconfigure(self._note_item, text=new[1])
            self._layout_hero()
        self._paint_controls()
        self._paint_footer()
        if not self._dragging and self._fresh("brightness"):
            value = int(round(st["brightness"] * 100))
            if value != self.bright_var.get():
                self.bright_var.set(value)
                self.bright_label.configure(text="%d%%" % value)
        if "mode" in st and self._fresh("mode"):
            labels = dict(MODE_DEFS)
            label = labels.get(st["mode"], MODE_DEFS[0][1])
            if self.mode_var.get() != label:
                self.mode_var.set(label)
        for key, var in self.gate_vars.items():
            if key in st and self._fresh(key) and var.get() != bool(st[key]):
                var.set(bool(st[key]))

    def _paint_footer(self) -> None:
        diag = self._status.get("diag") or ""
        text = self.phone_text + ("\n" + diag if diag else "")
        if text != self.foot_var.get():
            self.foot_var.set(text)
            self.root.after_idle(self.fit)

    def _report_exception(self, exc, value, tb) -> None:
        log.error("Ошибка в окне", exc_info=(exc, value, tb))

    def _open_log(self, _event=None) -> None:
        try:
            os.startfile(self.log_path)
        except OSError as e:
            self.msg_var.set("Журнал не открылся: %s" % e)


    def current_brightness(self) -> float:
        return self.bright_var.get() / 100.0

    def _selected_mode(self) -> str:
        label = self.mode_var.get()
        for key, text in MODE_DEFS:
            if text == label:
                return key
        return ""

    def _on_bright_move(self, value: str) -> None:
        self.bright_label.configure(text="%s%%" % value)
        if self._dragging and self._bright_pending is None:
            self._bright_pending = self.root.after(90, self._send_brightness)

    def _send_brightness(self) -> None:
        self._bright_pending = None
        self._touch("brightness")
        self.bridge.post(self.controller.set_brightness,
                         self.current_brightness())

    def _on_bright_press(self, _event=None) -> None:
        self._dragging = True
        self._touch("brightness")

    def _on_bright_commit(self, _event=None) -> None:
        self._dragging = False
        if self._bright_pending is not None:
            self.root.after_cancel(self._bright_pending)
            self._bright_pending = None
        self._send_brightness()

    def _on_mode_change(self) -> None:
        self._touch("mode")
        self.bridge.post(self.controller.set_mode, self._selected_mode())

    def _on_gate_change(self, key: str) -> None:
        self._touch(key)
        self.bridge.post(self.controller.set_gate, key,
                         self.gate_vars[key].get())

    def _start(self, make_coro, waiting: str) -> None:
        if self.busy:
            return
        self.set_busy(True, waiting)
        self.bridge.call(make_coro, self.root, self._after_ok, self._after_err)

    def do_sync(self, preset: str) -> None:
        flags = [flag for key, flag in SYNC_FLAGS.items()
                 if self.gate_vars[key].get()]
        brightness, mode = self.current_brightness(), self._selected_mode()
        self._start(lambda: self.controller.start_sync(
            preset, brightness=brightness, mode=mode or None, flags=flags),
            "включаю…")

    def do_event(self, name: str) -> None:
        brightness = self.current_brightness()
        self._start(lambda: self.controller.start_event(
            name, brightness=brightness), "переключаю…")

    def do_power(self) -> None:
        if self.busy:
            return
        if self.controller.status().get("power") is True:
            self._start(self.controller.turn_off, "выключаю…")
        else:
            brightness = self.current_brightness()
            self._start(lambda: self.controller.turn_on(brightness=brightness),
                        "включаю…")

    def do_stop(self) -> None:
        self._start(self.controller.stop, "останавливаю…")

    def do_off(self) -> None:
        self._start(self.controller.turn_off, "выключаю…")

    def _after_ok(self, _result) -> None:
        self.set_busy(False)
        self.refresh_status()

    def _after_err(self, error: BaseException) -> None:
        self.set_busy(False)
        self.msg_var.set("Ошибка: %s" % error)
        self.refresh_status()

    def on_close(self) -> None:
        self.msg_var.set("останавливаю…")
        self.root.update_idletasks()
        self.bridge.shutdown()
        self.root.destroy()


def main() -> int:
    if getattr(sys, "frozen", False):
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    enable_dpi_awareness()
    if not claim_single_instance():
        if not activate_existing_window():
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showinfo(WINDOW_TITLE, "Лента уже запущена - её окно "
                                "в панели задач.")
            hidden.destroy()
        return 0

    log_path = setup_logging()
    config = load_config()
    bridge = AsyncBridge(config)
    bridge.start()

    root = tk.Tk()
    App(root, bridge, log_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

