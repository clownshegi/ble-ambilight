from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="Генератор вспышек на экране для проверки FlashGate",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=float, default=0.1,
                   help="сколько секунд держать белый экран")
    p.add_argument("--period", type=float, default=3.0,
                   help="пауза между автоматическими вспышками")
    p.add_argument("--count", type=int, default=0,
                   help="сколько автовспышек сделать, 0 - без остановки")
    args = p.parse_args()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    label = tk.Label(root, text="пробел - вспышка вручную, q/Esc - выход",
                     fg="#444", bg="black", font=("Consolas", 14))
    label.pack(expand=True)

    state = {"flashing": False, "auto_done": 0}

    def set_bg(color: str) -> None:
        root.configure(bg=color)
        label.configure(bg=color)

    def flash(_event=None) -> None:
        if state["flashing"]:
            return
        state["flashing"] = True
        t = time.strftime("%H:%M:%S")
        print("вспышка в %s, держу %.0f мс" % (t, args.duration * 1000))
        set_bg("white")
        root.after(max(1, int(args.duration * 1000)), end_flash)

    def end_flash() -> None:
        set_bg("black")
        state["flashing"] = False

    def auto_tick() -> None:
        if args.count and state["auto_done"] >= args.count:
            return
        flash()
        state["auto_done"] += 1
        if not args.count or state["auto_done"] < args.count:
            root.after(int(args.period * 1000), auto_tick)

    root.bind("<space>", flash)
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("q", lambda e: root.destroy())

    print("Окно на весь экран. Запустите рядом:")
    print("  python ambilight.py --dry-run --flash-gate")
    print("и сверяйте время вспышек здесь со строками "
          "\"вспышка #N в ЧЧ:ММ:СС\" там.\n")

    root.after(int(args.period * 1000), auto_tick)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

