"""TextPipeline: script conversion between sources and display (SPEC v1.7).

- Queries to simplified sources (NetEase, Kugou) go through t2s.
- Results from simplified sources are converted line by line, all lines.
- Results from other sources (LRCLIB) are checked line by line and only
  the lines judged simplified are converted.
- Player metadata shown on screen is converted character-only (s2tw),
  and only when judged simplified.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import Protocol

from lyricfloat.lyrics.chinese import CHAR_ONLY, ChineseConverter, apply_vocabulary
from lyricfloat.lyrics.language import has_kana_or_hangul
from lyricfloat.lyrics.sources.base import LyricSource
from lyricfloat.models import LyricLine, Lyrics, TrackInfo


class TextPipeline(Protocol):
    def prepare_query(self, track: TrackInfo, source: LyricSource) -> TrackInfo:
        """Adapt the query for a source (e.g. t2s for simplified sources)."""
        ...

    def process(self, lyrics: Lyrics, source: LyricSource) -> Lyrics:
        """Convert source text to what is cached and displayed."""
        ...

    def display_text(self, text: str) -> str:
        """Convert player metadata for display and cache keys."""
        ...

    def comparison_text(self, text: str, source: LyricSource) -> str:
        """A title as compared with this source's candidates (SPEC v1.10)."""
        ...

    def session(self) -> AbstractContextManager:
        """Keep conversion dictionaries loaded for a batch (SPEC v1.11)."""
        ...


class PassthroughPipeline:
    def prepare_query(self, track: TrackInfo, source: LyricSource) -> TrackInfo:
        return track

    def process(self, lyrics: Lyrics, source: LyricSource) -> Lyrics:
        return lyrics

    def display_text(self, text: str) -> str:
        return text

    def comparison_text(self, text: str, source: LyricSource) -> str:
        return text

    def session(self) -> AbstractContextManager:
        return nullcontext()


class OpenCCPipeline:
    def __init__(
        self,
        mode: str = "s2twp",
        vocabulary: Sequence[tuple[str, str]] = (),
        converter: ChineseConverter | None = None,
    ) -> None:
        self.mode = mode
        self.vocabulary = list(vocabulary)
        self.cc = converter or ChineseConverter()

    @property
    def cache_key(self) -> str:
        """Identifies the conversion: mode plus vocabulary, so cached text is redone when either changes."""
        if not self.vocabulary:
            return self.mode
        digest = hashlib.sha1(repr(self.vocabulary).encode("utf-8")).hexdigest()[:8]
        return f"{self.mode}+{digest}"

    def prepare_query(self, track: TrackInfo, source: LyricSource) -> TrackInfo:
        if not source.simplified:
            return track
        t2s = lambda s: self.cc.convert("t2s", s)  # noqa: E731
        return replace(track, title=t2s(track.title), artist=t2s(track.artist), album=t2s(track.album))

    def convert_line(self, text: str, force: bool = False) -> str:
        """Convert one lyric/translation line; `force` skips detection (simplified sources)."""
        if not text.strip() or has_kana_or_hangul(text):
            return text  # Japanese/Korean lines stay as they are, even from simplified sources
        if force or self.cc.is_simplified(text):
            return apply_vocabulary(self.cc.convert(self.mode, text), self.vocabulary)
        return text

    def session(self) -> AbstractContextManager:
        return self.cc.session()

    def process(self, lyrics: Lyrics, source: LyricSource) -> Lyrics:
        force = source.simplified
        with self.cc.session():  # one dictionary load for the whole song
            lines = tuple(LyricLine(line.start, self.convert_line(line.text, force)) for line in lyrics.lines)
        return replace(lyrics, lines=lines)

    def display_text(self, text: str) -> str:
        if not self.cc.is_simplified(text):
            return text
        return apply_vocabulary(self.cc.convert(CHAR_ONLY, text), self.vocabulary)

    def comparison_text(self, text: str, source: LyricSource) -> str:
        """Simplified sources store titles in either script: compare both sides in simplified."""
        if not source.simplified or has_kana_or_hangul(text):
            return text
        return self.cc.convert("t2s", text)

    def display_track(self, track: TrackInfo) -> TrackInfo:
        with self.cc.session():
            return replace(
                track,
                title=self.display_text(track.title),
                artist=self.display_text(track.artist),
                album=self.display_text(track.album),
            )
