from __future__ import annotations

import sys
import time
from typing import Any


WINDOW_TITLES = (
    "Scrap Mechanic Save Editor",
    "Scrap Mechanic Save Editor - Starting",
)


class SingleInstance:
    """Own a per-user Windows mutex and foreground the existing editor."""

    def __init__(self, name: str) -> None:
        self._handle: Any = None
        self.acquired = True
        if sys.platform != "win32":
            return

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            self.acquired = False
            return
        self._handle = handle

    @staticmethod
    def activate_existing_window(timeout: float = 5.0) -> bool:
        if sys.platform != "win32":
            return False

        import ctypes

        user32 = ctypes.windll.user32
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            for title in WINDOW_TITLES:
                handle = user32.FindWindowW(None, title)
                if handle:
                    user32.ShowWindow(handle, 9)  # SW_RESTORE
                    user32.BringWindowToTop(handle)
                    user32.SetForegroundWindow(handle)
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        if self._handle is None or sys.platform != "win32":
            return
        import ctypes

        ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None
