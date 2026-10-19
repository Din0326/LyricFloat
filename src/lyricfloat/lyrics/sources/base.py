"""Pluggable lyric source interface.

A source answers two questions: an optional exact lookup (one best guess
from the source's own matching) and a fuzzy search (a candidate list).
LyricService owns the flow between them, scoring, timeouts and error
isolation, so new sources (NetEase, Kugou, Spotify in M2+) only implement
the network calls and the mapping to Candidate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from lyricfloat.models import Candidate, TrackInfo


class SourceError(Exception):
    """The source could not answer (network, HTTP status, bad payload, timeout)."""


class LyricSource(ABC):
    name: ClassVar[str]
    # Simplified-Chinese sources get t2s on queries and full conversion on results (TextPipeline).
    simplified: ClassVar[bool] = False
    # False when search hits carry only ids: lyrics come from fetch_lyrics on the best few.
    lyrics_inline: ClassVar[bool] = True

    async def lookup(self, track: TrackInfo) -> Candidate | None:
        """Exact match by full metadata. Returns None when not found.

        Sources without an exact endpoint keep this default.
        """
        return None

    @abstractmethod
    async def search(self, track: TrackInfo) -> list[Candidate]:
        """Fuzzy search. Returns [] when nothing is found; raises SourceError on failure."""

    async def search_text(self, text: str) -> list[Candidate]:
        """Free-text search with the whole query in one string, for identifying the song
        behind a video title (SPEC v1.34). Sources without one find nothing."""
        return []

    async def fetch_lyrics(self, candidate: Candidate) -> Candidate:
        """Fill in lyrics for sources whose search results carry only ids (e.g. NetEase).

        LRCLIB returns lyrics inline, so the default is a no-op.
        """
        return candidate

    async def aclose(self) -> None:
        """Release network resources owned by the source."""
