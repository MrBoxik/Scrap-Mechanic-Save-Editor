from __future__ import annotations

import sys
import threading
from typing import Any

from . import __version__


class StartupSplash:
    """Display immediate launch feedback while WebView2 creates its window."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="startup-splash", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def close(self, *_args: Any) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("Scrap Mechanic Save Editor - Starting")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            width, height = 470, 224
            left = (root.winfo_screenwidth() - width) // 2
            top = (root.winfo_screenheight() - height) // 2
            root.geometry(f"{width}x{height}+{left}+{top}")
            root.configure(background="#15191c")

            canvas = tk.Canvas(
                root,
                width=width,
                height=height,
                background="#15191c",
                highlightthickness=1,
                highlightbackground="#3a4147",
            )
            canvas.pack(fill="both", expand=True)
            canvas.create_rectangle(0, 0, width, 5, fill="#f6c343", outline="")
            canvas.create_rectangle(34, 43, 98, 107, fill="#f6c343", outline="")
            canvas.create_text(66, 75, text="SM", fill="#201a0d", font=("Segoe UI", 18, "bold"))
            canvas.create_text(120, 49, anchor="nw", text="SCRAP MECHANIC", fill="#f4f6f7", font=("Segoe UI", 17, "bold"))
            canvas.create_text(120, 78, anchor="nw", text="SAVE EDITOR", fill="#f6c343", font=("Segoe UI", 10, "bold"))
            canvas.create_text(width - 34, 53, anchor="ne", text=f"v{__version__}", fill="#778188", font=("Segoe UI", 9))
            canvas.create_text(34, 142, anchor="nw", text="Starting editor", fill="#e6eaec", font=("Segoe UI", 11, "bold"))
            canvas.create_text(34, 167, anchor="nw", text="Preparing the local save engine and desktop interface...", fill="#90999f", font=("Segoe UI", 9))
            canvas.create_rectangle(34, 199, width - 34, 203, fill="#2d3438", outline="")
            progress = canvas.create_rectangle(34, 199, 118, 203, fill="#f6c343", outline="")

            root.update_idletasks()
            root.update()
            self._ready.set()
            phase = 0
            track = width - 68
            bar = 84
            while not self._stop.wait(0.016):
                phase = (phase + 3) % (track + bar)
                x1 = 34 + max(0, phase - bar)
                x2 = 34 + min(track, phase)
                canvas.coords(progress, x1, 199, max(x1, x2), 203)
                root.update_idletasks()
                root.update()
            root.destroy()
        except Exception:
            self._ready.set()
