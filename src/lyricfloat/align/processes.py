"""Find the process whose audio belongs to a media session (L3 recording).

SMTC gives an app id ("Spotify.exe", "Chrome", "MSEdge", or a Store app id);
process loopback needs the top process of that app, because browsers and
Spotify play audio from child processes and the loopback includes the tree.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterable
from ctypes import wintypes

# App ids that are not exe names.
KNOWN_EXES = {"chrome": "chrome.exe", "msedge": "msedge.exe", "308046b0af4a39cb": "firefox.exe", "firefox": "firefox.exe"}


def exe_for(app_id: str) -> str:
    name = app_id.rsplit("!", 1)[-1].strip()
    if name.lower().endswith(".exe"):
        return name
    return KNOWN_EXES.get(name.lower(), name + ".exe")


def top_process(processes: Iterable[tuple[int, int, str]], exe: str) -> int | None:
    """Among (pid, parent pid, exe name), the instance of `exe` whose parent is not `exe`."""
    matches = [(pid, ppid) for pid, ppid, name in processes if name.lower() == exe.lower()]
    pids = {pid for pid, _ in matches}
    roots = [pid for pid, ppid in matches if ppid not in pids]
    return roots[0] if roots else None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def list_processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) of every running process (Toolhelp snapshot)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    out = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def player_pid(app_id: str) -> int | None:
    return top_process(list_processes(), exe_for(app_id)) if app_id else None
