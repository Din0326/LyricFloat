"""The app's own CPU and memory for the tray menu (FR-27, SPEC v1.72), read the way Task Manager
shows them: CPU as a share of the whole machine, memory as the private working set (SPEC v1.11).
Calls the Windows API directly: psutil is for the measurement tools and does not ship."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass


class _MemoryCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX2
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        *[
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
                "PrivateUsage",
                "PrivateWorkingSetSize",
            )
        ],
        ("SharedCommitUsage", ctypes.c_ulonglong),
    ]


# PROCESS_MEMORY_COUNTERS_EX: what Windows before 10 1809 fills (no private working set)
_EX_SIZE = _MemoryCounters.PrivateWorkingSetSize.offset


def private_working_set() -> int | None:
    """Bytes in Task Manager's "Memory" column for this process; private bytes on a Windows too
    old to report it (the same stand-in perf.py uses); None when it cannot be read."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):  # not Windows
        return None
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    counters = _MemoryCounters()
    process = kernel32.GetCurrentProcess()
    for size in (ctypes.sizeof(_MemoryCounters), _EX_SIZE):
        counters.cb = size
        if kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), size):
            return counters.PrivateWorkingSetSize if size > _EX_SIZE else counters.PrivateUsage
    return None


@dataclass(frozen=True)
class Usage:
    cpu_pct: float | None  # share of the whole machine since the previous reading
    memory_mb: float | None

    def label(self) -> str:
        cpu = "—" if self.cpu_pct is None else f"{self.cpu_pct:.1f}%"
        memory = "—" if self.memory_mb is None else f"{self.memory_mb:.0f} MB"
        return f"CPU {cpu}・記憶體 {memory}"


class UsageMeter:
    """CPU is averaged over the time between two readings, so the first reading has none."""

    def __init__(
        self,
        cpu_time: Callable[[], float] = time.process_time,  # all threads of this process
        clock: Callable[[], float] = time.monotonic,
        cpus: int | None = None,
        memory: Callable[[], int | None] = private_working_set,
    ) -> None:
        self.cpu_time = cpu_time
        self.clock = clock
        self.cpus = max(1, cpus or os.cpu_count() or 1)
        self.memory = memory
        self._last: tuple[float, float] | None = None

    def read(self) -> Usage:
        now = (self.cpu_time(), self.clock())
        cpu = None
        if self._last is not None and now[1] > self._last[1]:
            cpu = max(0.0, (now[0] - self._last[0]) / (now[1] - self._last[1]) / self.cpus * 100)
        self._last = now
        memory = self.memory()
        return Usage(cpu, None if memory is None else memory / 1048576)
