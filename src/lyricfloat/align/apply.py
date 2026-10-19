"""Turn an L3 alignment into line times and word timings (SPEC v1.22). Pure logic.

Only lines the aligner is sure about move to their aligned start: within
3 s of the source time and with enough recognition confidence. If fewer
than 60% of the lines inside the recording qualify, the alignment is not
used at all. Other lines shift by the median difference of the sure ones.
Word timings map onto the displayed text by character position (the
aligner saw the t2s form of the same text, character for character).
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from lyricfloat.lyrics.lrc import first_sung_line
from lyricfloat.models import Alignment, LyricLine, WordTiming

MAX_SHIFT = 3.0
MIN_CONFIDENCE = 0.2  # mean word probability of the line; tuned from the per-song statistics
MIN_SURE_SHARE = 0.6
EDGE_MARGIN = 1.0  # a line this close to either end of the recording is not judged
ORDER_GAP = 0.05


@dataclass(frozen=True)
class Plan:
    usable: bool
    starts: tuple[float, ...]  # new start of every line
    words: tuple[tuple[WordTiming, ...] | None, ...]
    stats: dict  # numbers only, for the alignment log


def language_of(lines: Sequence[LyricLine]) -> str:
    """Whisper language code for the lyrics. Leading credits do not count: NetEase heads
    English songs with "作词 : 某人" (SPEC v1.82)."""
    return script_language("".join(line.text for line in lines[first_sung_line(lines):]))


def script_language(text: str) -> str:
    """Whisper language code by script: hangul, kana, han, else English."""
    if any("\uac00" <= ch <= "\ud7af" for ch in text):
        return "ko"
    if any("\u3040" <= ch <= "\u30ff" for ch in text):
        return "ja"
    if any("\u3400" <= ch <= "\u9fff" for ch in text):
        return "zh"
    return "en"


def lines_hash(lines: Sequence[LyricLine]) -> str:
    """Identifies the exact lines an alignment belongs to (a version switch or edit invalidates it)."""
    data = json.dumps([[round(line.start, 3), line.text] for line in lines], ensure_ascii=False)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def map_words(text: str, words: Sequence[dict], pos0: float) -> tuple[WordTiming, ...] | None:
    """Word timings in the displayed text's character positions, or None when the characters do not add up."""
    visible = [i for i, ch in enumerate(text) if not ch.isspace()]
    out = []
    k = 0
    for w in words:
        n = len("".join(w.get("text", "").split()))
        if n == 0:
            continue
        if k + n > len(visible):
            return None
        out.append(WordTiming(visible[k], visible[k + n - 1] + 1, pos0 + w["start"], pos0 + w["end"]))
        k += n
    return tuple(out) if k == len(visible) and out else None


def plan(lines: Sequence[LyricLine], segments: Sequence[dict], pos0: float, seconds: float) -> Plan:
    if len(segments) != len(lines):
        return Plan(False, tuple(l.start for l in lines), (None,) * len(lines), {"error": "segment count", "lines": len(lines)})
    judged, sure = 0, []
    aligned: list[tuple[float, float, bool]] = []  # (aligned start, confidence, sure)
    for line, seg in zip(lines, segments):
        found = seg.get("start") is not None  # None: nothing sung on this line, or no words reached it
        start = pos0 + seg["start"] if found else line.start
        probs = [w.get("p", 0.0) for w in seg.get("words", [])]
        confidence = statistics.mean(probs) if probs else 0.0
        sung = any(ch.isalnum() for ch in line.text)
        inside = sung and pos0 + EDGE_MARGIN <= line.start <= pos0 + seconds - EDGE_MARGIN
        ok = inside and found and abs(start - line.start) <= MAX_SHIFT and confidence >= MIN_CONFIDENCE
        judged += inside
        if ok:
            sure.append(start - line.start)
        aligned.append((start, confidence, ok, inside and found))

    share = len(sure) / judged if judged else 0.0
    usable = judged > 0 and share >= MIN_SURE_SHARE
    shift = statistics.median(sure) if sure else 0.0
    starts, words = [], []
    for line, seg, (start, _conf, ok, _measured) in zip(lines, segments, aligned):
        if usable and ok:
            starts.append(start)
            words.append(map_words(line.text, seg.get("words", []), pos0))
        else:
            starts.append(line.start + shift if usable else line.start)
            words.append(None)
    for i in range(1, len(starts)):  # keep the lines in order for the sync engine
        starts[i] = max(starts[i], starts[i - 1] + ORDER_GAP)
    diffs = [a[0] - l.start for l, a in zip(lines, aligned) if a[3]]  # inside the recording and aligned
    stats = {
        "lines": len(lines),
        "judged": judged,
        "sure": len(sure),
        "sure_share": round(share, 3),
        "usable": usable,
        "median_shift_s": round(shift, 3),
        "median_abs_diff_s": round(statistics.median(abs(d) for d in diffs), 3) if diffs else None,
        "within_0.3s": round(sum(abs(d) <= 0.3 for d in diffs) / len(diffs), 3) if diffs else None,
        "mean_confidence": round(statistics.mean(a[1] for a in aligned), 3) if aligned else None,
        "word_lines": sum(w is not None for w in words),
    }
    return Plan(usable, tuple(starts), tuple(words), stats)


PLAIN_SPACING = 3.0  # seconds per line when a plain song has too few sure lines to tell


def plan_plain(lines: Sequence[LyricLine], segments: Sequence[dict], pos0: float, seconds: float) -> Plan:
    """Times for lyrics that had none (SPEC v1.26): there is no source time to compare
    with, so a line counts when the aligner found it with enough confidence, and the
    song is timed when 60% of its sung lines count. The others sit between their sure
    neighbours, in proportion to their place."""
    if len(segments) != len(lines):
        return Plan(False, tuple(l.start for l in lines), (None,) * len(lines), {"error": "segment count", "lines": len(lines), "plain": True})
    sung = [any(ch.isalnum() for ch in line.text) for line in lines]
    found: list[float | None] = []
    confidences = []
    for seg in segments:
        probs = [w.get("p", 0.0) for w in seg.get("words", [])]
        confidences.append(statistics.mean(probs) if probs else 0.0)
        found.append(pos0 + seg["start"] if seg.get("start") is not None else None)
    ok = [s and f is not None and c >= MIN_CONFIDENCE for s, f, c in zip(sung, found, confidences)]
    judged, sure = sum(sung), sum(ok)
    share = sure / judged if judged else 0.0
    usable = judged > 0 and share >= MIN_SURE_SHARE
    if not usable:
        starts = [l.start for l in lines]
        words: list = [None] * len(lines)
    else:
        known = [(i, found[i]) for i in range(len(lines)) if ok[i]]
        gaps = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(known, known[1:]) if b[1] > a[1]]
        spacing = statistics.median(gaps) if gaps else PLAIN_SPACING
        starts = []
        for i in range(len(lines)):
            if ok[i]:
                starts.append(found[i])
                continue
            before = next(((j, s) for j, s in reversed(known) if j < i), None)
            after = next(((j, s) for j, s in known if j > i), None)
            if before and after:
                starts.append(before[1] + (after[1] - before[1]) * (i - before[0]) / (after[0] - before[0]))
            elif before:
                starts.append(before[1] + spacing * (i - before[0]))
            else:
                starts.append(max(0.0, after[1] - spacing * (after[0] - i)))
        words = [map_words(line.text, seg.get("words", []), pos0) if good else None for line, seg, good in zip(lines, segments, ok)]
        for i in range(1, len(starts)):
            starts[i] = max(starts[i], starts[i - 1] + ORDER_GAP)
    stats = {
        "lines": len(lines),
        "judged": judged,
        "sure": sure,
        "sure_share": round(share, 3),
        "usable": usable,
        "mean_confidence": round(statistics.mean(confidences), 3) if confidences else None,
        "word_lines": sum(w is not None for w in words),
        "plain": True,
    }
    return Plan(usable, tuple(starts), tuple(words), stats)


def plan_to_json(p: Plan, lines: Sequence[LyricLine]) -> str:
    return json.dumps(
        {
            "version": 1,
            "lines_hash": lines_hash(lines),
            "starts": list(p.starts),
            "words": [None if w is None else [[t.first, t.last, t.start, t.end] for t in w] for w in p.words],
        }
    )


def plan_from_json(text: str, lines: Sequence[LyricLine]) -> Alignment | None:
    """The stored alignment, or None when it belongs to other lines (or cannot be read)."""
    try:
        data = json.loads(text)
        if data.get("version") != 1 or data.get("lines_hash") != lines_hash(lines) or len(data["starts"]) != len(lines):
            return None
        words = tuple(None if w is None else tuple(WordTiming(int(a), int(b), float(s), float(e)) for a, b, s, e in w) for w in data["words"])
        return Alignment(tuple(float(s) for s in data["starts"]), words)
    except (ValueError, KeyError, TypeError):
        return None
