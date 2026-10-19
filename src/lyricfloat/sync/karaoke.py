"""Karaoke fill timing (FR-11). Pure logic, no Qt.

v1.1 has no word-level timestamps (the sources that offer them only do so
through encrypted client APIs, SPEC v1.18), so every line is filled by
interpolation: the sung part grows one character at a time at an even
pace from the line's start until its estimated end. Word timings from L3
(v1.2) can replace the even pace per line without changing the drawing.

The pace is judged per song from its own line spacing: lines sung back to
back show how fast the song goes, while the time between lines also holds
breaths and pauses, so only part of it is singing. A fixed pace lagged on
fast songs and filled through every breath (SPEC v1.20).
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence

from lyricfloat.models import LyricLine, WordTiming

DEFAULT_PACE = 0.26  # seconds per sung unit (a CJK character, or a Latin word) when a song is too short to judge
PACE_RANGE = (0.15, 0.6)
SINGING_SHARE = 0.75  # of the spacing between back-to-back lines; the rest is breath
TAIL = 0.2  # the last unit is usually held a little longer
BREATH = 0.15  # finish this long before the next line starts
MIN_FILL = 0.5
MIN_SAMPLES = 4

_UNIT = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]|[^\s぀-ヿ㐀-鿿가-힯豈-﫿]+")


def sung_units(text: str) -> int:
    """Rough count of sung syllable groups: each CJK/kana/hangul character, each other word."""
    return len(_UNIT.findall(text))


def song_pace(lines: Sequence[LyricLine]) -> float:
    """Seconds per sung unit for this song, from its tighter line spacings (lower quartile)."""
    samples = []
    for line, nxt in zip(lines, lines[1:]):
        units = sung_units(line.text)
        gap = nxt.start - line.start
        if units >= 3 and 0 < gap <= 12:
            samples.append(gap / units)
    if len(samples) < MIN_SAMPLES:
        return DEFAULT_PACE
    quartile = statistics.quantiles(samples, n=4)[0]
    return min(max(quartile * SINGING_SHARE, PACE_RANGE[0]), PACE_RANGE[1])


def fill_duration(text: str, start: float, next_start: float | None, pace: float = DEFAULT_PACE) -> float:
    """How long the fill takes: the line's units at the song's pace, done before the next line."""
    estimate = max(MIN_FILL, sung_units(text) * pace + TAIL)
    if next_start is None or next_start <= start:
        return estimate
    gap = next_start - start
    return min(estimate, gap - BREATH if gap > 2 * BREATH else gap)


class KaraokeTimeline:
    def __init__(
        self, lines: Sequence[LyricLine], words: Sequence[Sequence[WordTiming] | None] | None = None
    ) -> None:
        """`words`: L3 word timings per line (FR-21); lines without them are interpolated."""
        self.lines = tuple(lines)
        self.pace = song_pace(self.lines)
        self.words = tuple(words) if words is not None and len(words) == len(self.lines) else (None,) * len(self.lines)
        self._ends = [
            self.words[i][-1].end
            if self.words[i]
            else line.start + fill_duration(line.text, line.start, self.lines[i + 1].start if i + 1 < len(self.lines) else None, self.pace)
            for i, line in enumerate(self.lines)
        ]

    def end_of(self, index: int) -> float:
        return self._ends[index]

    def fraction(self, index: int, position: float | None) -> float:
        """Share of line `index`'s visible characters sung at `position` (lyric time), 0..1."""
        if position is None or not 0 <= index < len(self.lines):
            return 0.0
        if self.words[index]:
            return _word_fraction(self.lines[index].text, self.words[index], position)
        start, end = self.lines[index].start, self._ends[index]
        if position <= start:
            return 0.0
        if position >= end:
            return 1.0
        return (position - start) / (end - start)


def _word_fraction(text: str, words: Sequence[WordTiming], position: float) -> float:
    """Each word fills its own characters evenly between its start and end."""
    total = sum(1 for ch in text if not ch.isspace())
    if not total:
        return 0.0
    sung = 0.0
    for w in words:
        chars = sum(1 for ch in text[w.first : w.last] if not ch.isspace())
        if position >= w.end:
            sung += chars
        elif position > w.start:
            sung += chars * (position - w.start) / max(w.end - w.start, 1e-3)
            break
        else:
            break
    return min(1.0, sung / total)


def split_point(text: str, fraction: float) -> tuple[int, float]:
    """Where the fill edge sits: (characters fully sung, share of the next one).

    The pace is even per visible character; spaces are passed over at once,
    so the edge never lingers in a gap between words.
    """
    visible = [i for i, ch in enumerate(text) if not ch.isspace()]
    if not visible or fraction <= 0:
        return 0, 0.0
    if fraction >= 1:
        return len(text), 0.0
    pos = fraction * len(visible)
    k = int(pos)
    return visible[k], pos - k
