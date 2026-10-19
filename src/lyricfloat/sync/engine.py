"""SyncEngine: estimates playback position locally and maps it to a lyric line.

Pure logic with an injectable clock. Every SMTC report re-anchors the
estimate; a jump of more than `seek_threshold` seconds against the
prediction counts as a seek (drag, loop restart) and repositions without
the line-change animation.
"""

from __future__ import annotations

import time
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lyricfloat.models import PlaybackAnchor

SEEK_THRESHOLD = 2.0  # seconds (spec: jumps over 2 s reposition)
BACKSTEP_TOLERANCE = 0.25  # small re-anchors this close to a boundary do not step back a line
LOOP_EDGE = 5.0  # seconds from either end that make a backwards jump look like a loop


@dataclass(frozen=True, slots=True)
class LineUpdate:
    index: int  # -1 before the first line, or with no lyrics
    animate: bool
    reason: str  # "tick", "anchor", "seek", "loop", "lyrics"


class SyncEngine:
    def __init__(self, clock: Callable[[], float] = time.monotonic, seek_threshold: float = SEEK_THRESHOLD) -> None:
        self.clock = clock
        self.seek_threshold = seek_threshold
        self._starts: list[float] = []
        self._anchor: PlaybackAnchor | None = None
        self._index = -1

    @property
    def index(self) -> int:
        return self._index

    @property
    def starts(self) -> tuple[float, ...]:
        """Line start times as displayed (any offset already applied)."""
        return tuple(self._starts)

    @property
    def playing(self) -> bool:
        return self._anchor is not None and self._anchor.playing

    def set_lines(self, starts: Sequence[float]) -> LineUpdate:
        self._starts = sorted(starts)
        self._index = self._locate(self.position())
        return LineUpdate(self._index, animate=False, reason="lyrics")

    def clear_lines(self) -> None:
        self._starts = []
        self._index = -1

    def reset(self) -> None:
        self.clear_lines()
        self._anchor = None

    def position(self, now: float | None = None) -> float | None:
        a = self._anchor
        if a is None:
            return None
        if not a.playing:
            return a.position
        pos = a.position + ((self.clock() if now is None else now) - a.at)
        if a.duration > 0:
            pos = min(pos, a.duration)
        return max(0.0, pos)

    def _locate(self, pos: float | None) -> int:
        if pos is None or not self._starts:
            return -1
        return bisect_right(self._starts, pos) - 1

    def update(self, anchor: PlaybackAnchor) -> LineUpdate | None:
        predicted = self.position(anchor.at)
        reason = "anchor"
        if predicted is not None and abs(anchor.position - predicted) > self.seek_threshold:
            looped = (
                anchor.position < predicted
                and anchor.position <= LOOP_EDGE
                and anchor.duration > 0
                and predicted >= anchor.duration - LOOP_EDGE
            )
            reason = "loop" if looped else "seek"
        self._anchor = anchor

        new_index = self._locate(anchor.position)
        if reason == "anchor" and new_index == self._index - 1:
            # Report jitter right after a boundary: stay on the current line.
            if self._starts[self._index] - anchor.position < BACKSTEP_TOLERANCE:
                return None
        if reason == "anchor" and new_index == self._index:
            return None
        self._index = new_index
        return LineUpdate(new_index, animate=reason == "anchor", reason=reason)

    def tick(self, now: float | None = None) -> LineUpdate | None:
        new_index = self._locate(self.position(now))
        if new_index == self._index:
            return None
        self._index = new_index
        return LineUpdate(new_index, animate=True, reason="tick")

    def seconds_to_next(self, now: float | None = None) -> float | None:
        """Time until the next line starts, or None when nothing is scheduled."""
        if not self.playing or not self._starts:
            return None
        pos = self.position(now)
        nxt = bisect_right(self._starts, pos)
        if nxt >= len(self._starts):
            return None
        return self._starts[nxt] - pos
