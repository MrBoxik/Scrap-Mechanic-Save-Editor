from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys


def game_is_running() -> bool:
    if sys.platform != "win32":
        return False

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_void_p),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError("Could not check whether Scrap Mechanic is running")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.name.casefold() == "scrapmechanic.exe":
                    return True
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        return False
    finally:
        kernel.CloseHandle(snapshot)


def require_game_closed() -> None:
    if game_is_running():
        raise RuntimeError("Close Scrap Mechanic before writing saves or profiles. Your staged changes are still in the editor.")
