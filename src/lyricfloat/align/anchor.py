"""Find where a version of another length (an MV) starts singing while it plays (SPEC v1.36).

The song's L3 recording is read while it is being written, in 10 s clips, each with the
last 2 s of the one before. Only clips that can hold the searched lines are aligned: their
source times plus 0 to the length gap, 3 s either side. A worker that keeps its models
loaded aligns the first five sung lines to each clip; the start counts when three
different lines are sure (confidence 0.3) and agree within 1 s, from one clip or several
(SPEC v1.44). Two agreeing lines were not enough: wrong lyrics
(another song's) once "found" a start that way, and a chorus line sung again is sure at
the wrong place. Requiring the first three lines sure in one clip (v1.38) missed three
MVs in a row: a slow song's first three lines span more than a clip, and one weak line
(0.22-0.26) sank starts the other lines agreed on within 0.4 s.
"""

from __future__ import annotations

import json
import logging
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from lyricfloat.align.apply import language_of
from lyricfloat.align.recorder import LiveRecording
from lyricfloat.align.runner import MODEL, free_memory, worker_env
from lyricfloat.lyrics.lrc import first_sung_line
from lyricfloat.models import LyricLine, TrackInfo

log = logging.getLogger(__name__)
stats_log = logging.getLogger("lyricfloat.alignment")  # numbers only, never lyrics or titles

CLIP = 10.0  # seconds of new audio per clip
OVERLAP = 2.0  # seconds of the clip before, so a line across the cut is whole in one clip
LINES = 5  # first sung lines aligned per clip
NEED = 3  # different lines that must be sure and agree (both, for a song with two sung lines)
AGREE = 1.0  # seconds: the lines must agree this well
SURE = 0.3  # mean word probability a line needs; the spike's true starts had 0.42 or more
MARGIN = 3.0  # seconds either side of where the singing can start
SETTLE = 0.5  # seconds after a clip's end before reading it: the last writes reach the file
THREADS = 4
MIN_FREE_BYTES = 2 * 2**30
TICK_MS = 500
IDLE_PRIORITY_CLASS = 0x00000040
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class StartSearch:
    """What to look for: the first sung lines and how far the version's length is off."""

    lines: tuple[LyricLine, ...]  # first sung lines, as displayed
    texts: tuple[str, ...]  # what the worker aligns (t2s for Chinese)
    language: str
    gap: float  # player's length minus the version's

    @property
    def low(self) -> float:
        """Smallest believable offset of the source times."""
        return min(0.0, self.gap) - MARGIN

    @property
    def high(self) -> float:
        return max(0.0, self.gap) + MARGIN

    @property
    def need(self) -> int:
        return min(NEED, len(self.lines))

    def wanted(self, pos0: float, pos_end: float) -> bool:
        """Whether a clip from pos0 to pos_end can hold a searched line at a believable offset."""
        return pos_end >= self.lines[0].start + self.low and pos0 <= self.lines[-1].start + self.high

    def over(self, pos0: float) -> bool:
        """A clip starting here, and every later one, comes after every searched line."""
        return pos0 > self.lines[-1].start + self.high

    def sure(self, segments: Sequence[dict], pos0: float) -> list[tuple[int, float, float]]:
        """(line index, confidence, offset of the source times) for each searched line a clip
        holds surely at a believable offset."""
        if len(segments) < len(self.lines):
            return []
        out = []
        for i, (line, seg) in enumerate(zip(self.lines, segments)):
            probs = [w.get("p", 0.0) for w in seg.get("words", [])]
            confidence = statistics.mean(probs) if probs else 0.0
            if seg.get("start") is None or confidence < SURE:
                continue
            offset = pos0 + seg["start"] - line.start
            if self.low <= offset <= self.high:
                out.append((i, confidence, offset))
        return out

    @staticmethod
    def agreeing(sure: Sequence[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
        """The most different lines whose offsets agree within AGREE, one entry per line
        (the surest), in line order."""
        ordered = sorted(sure, key=lambda e: e[2])
        best: dict[int, tuple[int, float, float]] = {}
        for k, first in enumerate(ordered):
            group: dict[int, tuple[int, float, float]] = {}
            for e in ordered[k:]:
                if e[2] - first[2] > AGREE:
                    break
                if e[0] not in group or e[1] > group[e[0]][1]:
                    group[e[0]] = e
            if len(group) > len(best):
                best = group
        return [best[i] for i in sorted(best)]

    def judge(self, sure: Sequence[tuple[int, float, float]]) -> float | None:
        """The offset of the source times when enough different lines agree on it, else None."""
        group = self.agreeing(sure)
        if len(group) >= self.need:
            return round(statistics.median(o for _, _, o in group), 3)
        return None


def plan_search(lines: Sequence[LyricLine], title: str, artist: str, gap: float, t2s: Callable[[str], str]) -> StartSearch | None:
    """A search for these lyrics, or None when fewer than two lines are sung."""
    first = first_sung_line(lines, title, artist)
    sung = tuple(line for line in lines[first:] if any(ch.isalnum() for ch in line.text))[:LINES]
    if len(sung) < 2:
        return None
    language = language_of(lines)
    texts = tuple(t2s(line.text) if language == "zh" else line.text for line in sung)
    return StartSearch(sung, texts, language, gap)


class Serving:
    """A worker in serve mode: requests go in on stdin, replies come back on a reader thread.
    It reads the models from models_dir only (SPEC v1.85)."""

    def __init__(self, python: Path, worker: Path, models_dir: Path, on_reply: Callable[[dict], None]) -> None:
        self.on_reply = on_reply
        self.proc = subprocess.Popen(
            [str(python), str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=IDLE_PRIORITY_CLASS | CREATE_NO_WINDOW,
            env=worker_env(online=False),
        )
        self.send({"serve": True, "model": MODEL, "threads": THREADS, "models_dir": str(models_dir)})
        threading.Thread(target=self._read, name="l3-start-finder", daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                self.on_reply(json.loads(raw.decode("utf-8")))
            except ValueError:
                self.on_reply({"ok": False, "error": "unreadable worker reply"})
        self.on_reply({"ok": False, "error": "worker exited", "gone": True})

    def send(self, request: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except OSError:
            pass  # the reader thread reports the worker as gone

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


class StartFinder(QObject):
    """Runs one search at a time on the Qt thread; the worker answers on its own thread."""

    found = pyqtSignal(object, float)  # track, offset of the source times
    searching = pyqtSignal(object, bool)  # track, whether a search is running (for the corner hint)
    missed = pyqtSignal(object)  # track: the believable start went by without a sure clip
    modelsMissing = pyqtSignal()  # the worker found the models gone from models_dir (SPEC v1.85)
    _reply = pyqtSignal(object)

    def __init__(
        self,
        models_dir: Path,
        python: Path | None,
        worker: Path,
        launch: Callable[..., object] = Serving,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.models_dir = models_dir
        self.python = python
        self.worker = worker
        self._launch = launch
        self._clock = clock
        self.free_memory = free_memory  # replaceable in tests
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._reply.connect(self._on_reply)
        self._serving = None
        self._track: TrackInfo | None = None
        self._search: StartSearch | None = None
        self._live: Callable[[], LiveRecording | None] | None = None

    @property
    def active(self) -> bool:
        return self._track is not None

    def start(self, track: TrackInfo, search: StartSearch, live: Callable[[], LiveRecording | None]) -> bool:
        self.stop()
        if self.python is None or self.free_memory() < MIN_FREE_BYTES:
            log.info("start search skipped: %s", "no runtime" if self.python is None else "memory short")
            return False
        try:
            self._serving = self._launch(self.python, self.worker, self.models_dir, self._reply.emit)
        except OSError as exc:
            log.warning("start search: worker did not start: %s", exc)
            return False
        self._track, self._search, self._live = track, search, live
        self._ready = self._busy = False
        self._next_end: float | None = None
        self._clip_pos0 = 0.0
        self._clips = 0
        self._sure: list[tuple[int, float, float]] = []  # (line, confidence, offset) from every clip so far
        self._evidence: list[tuple[int, float, float]] = []  # the most lines that agreed, for the statistics
        self._began = self._clock()
        self._timer.start()
        self.searching.emit(track, True)
        log.info("start search for %s: gap %+.0fs, first line at %.1fs", track.describe(), search.gap, search.lines[0].start)
        return True

    def stop(self) -> None:
        """End the search quietly (new song, the user cued, L3 off)."""
        self._finish(None)

    def tick(self) -> None:
        if self._search is None or not self._ready or self._busy:
            return
        live = self._live()
        if live is None:
            self._finish(None)  # paused, sought or the song ended: nothing left to read
            return
        block = live.channels * 2
        if self._next_end is None:
            self._next_end = live.first_at + CLIP
        now = self._clock()
        if now - SETTLE < self._next_end:
            return
        behind = int((now - SETTLE - self._next_end) // CLIP)
        if behind >= 2:  # the worker fell behind: go to the newest clip
            self._next_end += behind * CLIP
        clip_start = self._next_end - CLIP - OVERLAP
        offset = max(0, int((clip_start - live.first_at) * live.rate)) * block
        length = int((CLIP + OVERLAP) * live.rate) * block
        if live.bytes < offset + length:
            return  # not written yet
        pos0 = live.pos0 + (clip_start - live.first_at)
        self._next_end += CLIP
        if self._search.over(pos0):
            self._finish(False)
            return
        if not self._search.wanted(pos0, pos0 + CLIP + OVERLAP):
            return
        self._busy, self._clip_pos0 = True, pos0
        self._serving.send({
            "pcm_path": str(live.path), "rate": live.rate, "channels": live.channels, "lines": list(self._search.texts),
            "language": self._search.language, "offset_bytes": offset, "length_bytes": length,
        })

    def _on_reply(self, reply: dict) -> None:
        if self._search is None:
            return
        if reply.get("ready"):
            self._ready = True
            self.tick()
            return
        if reply.get("models_missing"):
            log.warning("start search: %s", reply.get("error", ""))
            self._finish(None)  # nothing was searched: not a miss
            self.modelsMissing.emit()
            return
        if reply.get("gone") or (not reply.get("ok") and not self._ready):
            log.warning("start search: worker failed: %s", reply.get("error", ""))
            self._finish(False)
            return
        self._busy = False
        self._clips += 1
        if not reply.get("ok"):
            log.info("start search: clip failed: %s", reply.get("error", ""))
            return
        # Sure lines add up over the whole search: a slow song's lines span several clips, and
        # lines sure at the wrong place (a chorus sung again) do not agree with each other.
        self._sure += self._search.sure(reply.get("segments") or [], self._clip_pos0)
        group = self._search.agreeing(self._sure)
        if len(group) > len(self._evidence):
            self._evidence = group
        offset = self._search.judge(self._sure)
        if offset is not None:
            self._evidence = group
            self._finish(offset)
        else:
            self.tick()

    def _finish(self, outcome: float | bool | None) -> None:
        """outcome: the offset found, False when the start went by unseen, None to stop quietly."""
        track, search = self._track, self._search
        self._timer.stop()
        if self._serving is not None:
            self._serving.close()
            self._serving = None
        self._track = self._search = self._live = None
        if track is None:
            return
        if outcome is None:
            self.searching.emit(track, False)
            return
        found = outcome is not False
        stats_log.info(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "start_search", "found": found,
            "offset_s": outcome if found else None, "gap_s": round(search.gap, 1), "clips": self._clips,
            "language": search.language, "wall_s": round(self._clock() - self._began, 1),
            # The lines that agreed (on a miss, the most that did), so near misses can be tuned.
            "agreeing_lines": [i for i, _, _ in self._evidence],
            "confidences": [round(c, 3) for _, c, _ in self._evidence],
            "line_offsets_s": [round(o, 2) for _, _, o in self._evidence],
        }))
        # The outcome goes first, so the screen never sees the search end without its news.
        if found:
            log.info("start found for %s: source times %+.2fs", track.describe(), outcome)
            self.found.emit(track, float(outcome))
        else:
            log.info("start not found for %s after %d clips", track.describe(), self._clips)
            self.missed.emit(track)
        self.searching.emit(track, False)
