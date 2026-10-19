"""Run L3 alignment jobs one at a time in a low-priority subprocess (SPEC v1.22).

The subprocess gets the lowest CPU priority and a few threads, starts only
when enough memory is free, and the recording is deleted whatever happens.
Results come back on the Qt thread as signals.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from lyricfloat.align.recorder import Recording
from lyricfloat.models import LyricLine

log = logging.getLogger(__name__)

THREADS = 4
MIN_FREE_BYTES = 3 * 2**30
RETRY_MS = 60_000  # memory was short: look again in a minute
TIMEOUT_S = 20 * 60
NEEDED_MODULES = ("torch", "demucs", "stable_whisper", "faster_whisper", "julius")
IDLE_PRIORITY_CLASS = 0x00000040
CREATE_NO_WINDOW = 0x08000000
WORKER = Path(__file__).with_name("worker.py")


@dataclass(frozen=True)
class AlignJob:
    recording: Recording
    lines: tuple[LyricLine, ...]  # the displayed lines the alignment is for
    texts: tuple[str, ...]  # what the worker aligns (t2s for Chinese, SPEC v1.22)
    language: str
    skip: float = 0.0  # seconds left out at the start: an MV's intro before its singing (SPEC v1.36)


@dataclass(frozen=True)
class AlignOutcome:
    job: AlignJob
    ok: bool
    segments: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    error: str = ""
    wall_s: float = 0.0


def source_runtime() -> Path | None:
    """This interpreter, when it has the inference packages (running from source with the l3 extra)."""
    if getattr(sys, "frozen", False):
        return None
    try:
        if all(importlib.util.find_spec(name) is not None for name in NEEDED_MODULES):
            return Path(sys.executable)
    except (ImportError, ValueError):
        pass
    return None


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def free_memory() -> int:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return status.ullAvailPhys


def run_worker(python: Path, job: dict, worker: Path = WORKER, timeout: float = TIMEOUT_S, on_start=None) -> dict:
    """One worker run; both pipes are drained by communicate(), so a large result cannot block it.
    `on_start(proc)` lets the caller kill the worker (quitting the app)."""
    proc = subprocess.Popen(
        [str(python), str(worker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=IDLE_PRIORITY_CLASS | CREATE_NO_WINDOW,
        env=worker_env(),
    )
    if on_start is not None:
        on_start(proc)
    try:
        out, err = proc.communicate(json.dumps(job, ensure_ascii=False).encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"ok": False, "error": f"timed out after {timeout:.0f}s"}
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no message"]
        return {"ok": False, "error": f"exit code {proc.returncode}: {tail[0][:300]}"}
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError:
        return {"ok": False, "error": "unreadable worker output"}


def worker_env() -> dict:
    import os

    return {**os.environ, "TQDM_DISABLE": "1", "PYTHONWARNINGS": "ignore", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}


class AlignRunner(QObject):
    finished = pyqtSignal(object)  # AlignOutcome
    _done = pyqtSignal(object)  # from the worker thread

    def __init__(self, models_dir: Path, python: Path | None = None, worker: Path = WORKER, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.models_dir = models_dir
        self.python = python
        self.worker = worker
        self._queue: deque[AlignJob] = deque()
        self._busy: AlignJob | None = None
        self._retry = QTimer(self, singleShot=True, interval=RETRY_MS)
        self._retry.timeout.connect(self._next)
        self._done.connect(self._on_done)
        self.free_memory = free_memory  # replaceable in tests
        self._proc: subprocess.Popen | None = None

    @property
    def pending(self) -> int:
        return len(self._queue) + (self._busy is not None)

    def submit(self, job: AlignJob) -> None:
        self._queue.append(job)
        self._next()

    def clear(self) -> None:
        """Drop queued jobs (L3 turned off, quitting); a running one finishes and is discarded."""
        while self._queue:
            _delete(self._queue.popleft().recording.path)
        self._retry.stop()

    def shutdown(self) -> None:
        """Quitting: drop the queue and stop a running worker (its recording is deleted by its thread)."""
        self.clear()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _next(self) -> None:
        if self._busy is not None or not self._queue:
            return
        if self.python is None:
            log.info("L3 runtime not available; %d recording(s) dropped", len(self._queue))
            self.clear()
            return
        if self.free_memory() < MIN_FREE_BYTES:
            log.info("L3 waiting: less than %d GB of memory free", MIN_FREE_BYTES // 2**30)
            self._retry.start()
            return
        job = self._busy = self._queue.popleft()
        threading.Thread(target=self._work, args=(job,), name="l3-align", daemon=True).start()

    def _work(self, job: AlignJob) -> None:
        rec = job.recording
        request = {
            "pcm_path": str(rec.path),
            "rate": rec.rate,
            "channels": rec.channels,
            "lines": list(job.texts),
            "language": job.language,
            "model": "base",
            "threads": THREADS,
            "models_dir": str(self.models_dir),
        }
        if job.skip > 0:
            request["offset_bytes"] = int(job.skip * rec.rate) * rec.channels * 2
        t0 = time.perf_counter()
        try:
            result = run_worker(self.python, request, self.worker, on_start=lambda p: setattr(self, "_proc", p))
        except OSError as exc:
            result = {"ok": False, "error": f"could not start the worker: {exc}"}
        finally:
            self._proc = None
            _delete(rec.path)  # SPEC: the recording goes as soon as alignment is done
        wall = time.perf_counter() - t0
        outcome = AlignOutcome(
            job, bool(result.get("ok")), result.get("segments") or [], result.get("timings") or {}, result.get("error", ""), wall
        )
        self._done.emit(outcome)

    def _on_done(self, outcome: AlignOutcome) -> None:
        self._busy = None
        if outcome.ok:
            log.info("L3 aligned %s in %.0fs", outcome.job.recording.track.describe(), outcome.wall_s)
        else:
            log.warning("L3 failed for %s: %s", outcome.job.recording.track.describe(), outcome.error)
        self.finished.emit(outcome)
        self._next()


def _delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete recording %s: %s", path.name, exc)
