"""LRC lyric parser.

Handles the cases called out in the spec: the [offset:] tag, several
timestamps on one line, and timestamps that are out of order or repeated.
Word-level (enhanced LRC) tags are stripped down to plain line text for now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lyricfloat.models import LyricLine, Lyrics

# [mm:ss], [mm:ss.f], [mm:ss.ff], [mm:ss.fff], and the [mm:ss:ff] variant.
_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LEADING_TIME_TAGS = re.compile(r"^(?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+")
_ID_TAG = re.compile(r"^\s*\[([A-Za-z#]+)\s*:(.*)\]\s*$")
# Enhanced LRC word timing: <mm:ss.xx>
_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


@dataclass(frozen=True, slots=True)
class ParsedLrc:
    lyrics: Lyrics
    offset_ms: int = 0
    tags: dict[str, str] = field(default_factory=dict)


def _to_seconds(minutes: str, seconds: str, fraction: str | None) -> float:
    value = int(minutes) * 60 + int(seconds)
    if fraction:
        value += int(fraction) / (10 ** len(fraction))
    return value


def _parse_offset(raw: str) -> int:
    try:
        return int(raw.strip().replace("+", "", 1))
    except ValueError:
        return 0


def parse_lrc(text: str) -> ParsedLrc:
    """Parse LRC text into time-sorted lines with [offset:] already applied.

    Per the LRC convention a positive offset makes lyrics appear sooner, so
    each timestamp is shifted by -offset. Lines that would land before zero
    are clamped to zero.
    """
    tags: dict[str, str] = {}
    entries: list[tuple[float, int, str]] = []
    seen: set[tuple[float, str]] = set()
    order = 0

    for raw_line in text.lstrip("﻿").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        leading = _LEADING_TIME_TAGS.match(line)
        if leading:
            body = _WORD_TAG.sub("", line[leading.end():]).strip()
            for m in _TIME_TAG.finditer(leading.group(0)):
                t = _to_seconds(*m.groups())
                key = (t, body)
                if key in seen:
                    continue  # exact duplicate line
                seen.add(key)
                entries.append((t, order, body))
                order += 1
            continue

        tag = _ID_TAG.match(line)
        if tag:
            tags[tag.group(1).lower()] = tag.group(2).strip()

    offset_ms = _parse_offset(tags.get("offset", "0"))
    shift = offset_ms / 1000.0

    # Stable sort: same timestamp keeps file order (e.g. original + translation).
    entries.sort(key=lambda e: (e[0], e[1]))
    lines = tuple(LyricLine(start=max(0.0, t - shift), text=body) for t, _, body in entries)
    return ParsedLrc(lyrics=Lyrics(lines=lines, synced=True), offset_ms=offset_ms, tags=tags)


def parse_plain(text: str) -> Lyrics:
    """Wrap unsynced lyrics; every line gets start=0."""
    lines = tuple(
        LyricLine(start=0.0, text=line.strip())
        for line in text.lstrip("﻿").splitlines()
        if line.strip()
    )
    return Lyrics(lines=lines, synced=False)


# Credits before the singing, e.g. NetEase's "作词 : 某人" and "作曲 : 某人" at 0:00 and 0:01.
# Labels can be bilingual and long: "母带工程师 Mastering Engineer : 某人 @ 某錄音室".
_CREDIT = re.compile(r"^[^:：]{1,40}[:：]")


def first_sung_line(lines, title: str = "", artist: str = "") -> int:
    """Index of the first line that is sung (SPEC v1.29: the line the user cues). Leading
    credits, a "title - artist" line and lines without letters or digits are skipped."""
    title, artist = title.casefold().strip(), artist.casefold().strip()
    for i, line in enumerate(lines):
        text = line.text.strip()
        folded = text.casefold()
        if not any(ch.isalnum() for ch in text) or _CREDIT.match(text):
            continue
        if title and artist and title in folded and artist in folded:
            continue
        return i
    return 0


_CLOCK =re.compile(r"^\s*(?:(\d{1,3}):)?(\d{1,2}(?:\.\d{1,3})?)\s*$")


def format_time(seconds: float) -> str:
    """0:05.30 style, as written in LRC tags and shown in the lyric editor."""
    centis = max(0, round(seconds * 100))
    minutes, centis = divmod(centis, 6000)
    return f"{minutes}:{centis // 100:02d}.{centis % 100:02d}"


def parse_time(text: str) -> float | None:
    """"1:05.3", "65.3" or "1:05" -> seconds; None when it is not a time."""
    m = _CLOCK.match(text)
    if m is None:
        return None
    minutes, seconds = m.groups()
    value = float(seconds) + 60 * int(minutes or 0)
    return value if minutes is None or float(seconds) < 60 else None


def format_lrc(lines) -> str:
    """Synced lines back to LRC text ([mm:ss.xx]text), in time order."""
    out = []
    for line in sorted(lines, key=lambda l: l.start):
        minutes, rest = divmod(max(0, round(line.start * 100)), 6000)
        out.append(f"[{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}]{line.text}")
    return "\n".join(out)
