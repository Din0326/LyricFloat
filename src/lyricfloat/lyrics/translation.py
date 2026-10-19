"""Translation-line alignment and L1 cross-source timing comparison (SPEC v1.6).

NetEase gives a translation (tlyric) whose timestamps match its own
original lyrics (lrc). The main lyrics may come from another source, so
each main line is matched to a NetEase original line: within 1 s and
with text similarity >= 0.8. Lines that do not match get no translation.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Sequence

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from lyricfloat.lyrics.lrc import parse_lrc
from lyricfloat.models import LyricLine

TIME_WINDOW = 1.0  # seconds, translation alignment
TEXT_THRESHOLD = 0.8
SAME_SOURCE_WINDOW = 0.05  # lrc and tlyric from one NetEase entry share timestamps
PAIRING_WINDOW = 15.0  # seconds searched when estimating the offset between two timelines
L1_OFFSET_LIMIT = 1.0  # median offset above this means the two versions disagree


def _clean(text: str) -> str:
    # default_process turns punctuation into spaces; collapse them so "a, b" equals "a b".
    return " ".join(default_process(text).split())


def text_similarity(a: str, b: str) -> float:
    if not a.strip() or not b.strip():
        return 0.0
    return fuzz.ratio(a, b, processor=_clean) / 100.0


def parse_translation(tlyric: str | None) -> list[LyricLine]:
    if not tlyric:
        return []
    return [line for line in parse_lrc(tlyric).lyrics.lines if line.text.strip()]


def _translation_at(start: float, translations: Sequence[LyricLine], starts: Sequence[float]) -> str | None:
    i = bisect_left(starts, start - SAME_SOURCE_WINDOW)
    if i < len(starts) and starts[i] <= start + SAME_SOURCE_WINDOW:
        return translations[i].text
    return None


def align_translations(
    main: Sequence[LyricLine], original: Sequence[LyricLine], translations: Sequence[LyricLine]
) -> tuple[str | None, ...]:
    """Translation for each main line, or None where nothing matches."""
    tr_starts = [t.start for t in translations]
    orig = [o for o in original if o.text.strip()]
    orig_starts = [o.start for o in orig]
    out: list[str | None] = []
    for line in main:
        best: tuple[float, float, str] | None = None  # (similarity, -time gap, translation)
        lo = bisect_left(orig_starts, line.start - TIME_WINDOW)
        hi = bisect_right(orig_starts, line.start + TIME_WINDOW)
        for o in orig[lo:hi]:
            sim = text_similarity(line.text, o.text)
            if sim < TEXT_THRESHOLD:
                continue
            tr = _translation_at(o.start, translations, tr_starts)
            if tr is None:
                continue
            key = (sim, -abs(o.start - line.start), tr)
            if best is None or key[:2] > best[:2]:
                best = key
        out.append(best[2] if best else None)
    return tuple(out)


def align_same_source(original: Sequence[LyricLine], translations: Sequence[LyricLine]) -> tuple[str | None, ...]:
    """Main lyrics and translation from the same NetEase entry: timestamps alone pair them."""
    tr_starts = [t.start for t in translations]
    return tuple(_translation_at(o.start, translations, tr_starts) if o.text.strip() else None for o in original)


def median_offset(main: Sequence[LyricLine], other: Sequence[LyricLine]) -> float | None:
    """Median of (main.start - other.start) over lines whose text matches.

    Each main line is paired with its most similar line in `other` within
    15 s, so a repeated chorus elsewhere in the song is not picked up.
    Returns None with fewer than 3 pairs: too little to judge.
    """
    others = [o for o in other if o.text.strip()]
    starts = [o.start for o in others]
    gaps: list[float] = []
    for line in main:
        if not line.text.strip():
            continue
        lo = bisect_left(starts, line.start - PAIRING_WINDOW)
        hi = bisect_right(starts, line.start + PAIRING_WINDOW)
        best: tuple[float, float] | None = None  # (similarity, gap)
        for o in others[lo:hi]:
            sim = text_similarity(line.text, o.text)
            if sim >= TEXT_THRESHOLD and (best is None or sim > best[0] or (sim == best[0] and abs(line.start - o.start) < abs(best[1]))):
                best = (sim, line.start - o.start)
        if best is not None:
            gaps.append(best[1])
    if len(gaps) < 3:
        return None
    return statistics.median(gaps)
