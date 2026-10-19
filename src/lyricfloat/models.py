"""Plain data types shared across modules.

Everything here is immutable and Qt-free so it can cross the asyncio/Qt
boundary through signals and be used directly in unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


class PlaybackStatus(IntEnum):
    """Mirrors GlobalSystemMediaTransportControlsSessionPlaybackStatus."""

    CLOSED = 0
    OPENED = 1
    CHANGING = 2
    STOPPED = 3
    PLAYING = 4
    PAUSED = 5


@dataclass(frozen=True, slots=True)
class TrackInfo:
    title: str
    artist: str
    album: str = ""
    duration: float = 0.0  # seconds; 0 means the player did not report it
    source_app: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity used to tell "same song" (loop, re-emit) from a real track change.

        Duration and source app are left out on purpose: some players fill in
        the duration late, and the same song on another player is still the
        same song.
        """
        return (self.title.casefold(), self.artist.casefold(), self.album.casefold())

    def describe(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title


@dataclass(frozen=True, slots=True)
class PlaybackAnchor:
    """A playback position pinned to a monotonic clock instant.

    `position` is where playback was at `at` (time.monotonic seconds).
    """

    position: float
    at: float
    playing: bool
    duration: float = 0.0


@dataclass(frozen=True, slots=True)
class LyricLine:
    start: float  # seconds
    text: str


@dataclass(frozen=True, slots=True)
class WordTiming:
    """When the characters text[first:last] of a line are sung (L3, song time in seconds)."""

    first: int
    last: int
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Alignment:
    """L3 result for a song's lines: new start per line and word timings (None where a line has none)."""

    starts: tuple[float, ...]
    words: tuple[tuple[WordTiming, ...] | None, ...]


@dataclass(frozen=True, slots=True)
class Lyrics:
    lines: tuple[LyricLine, ...]
    synced: bool
    # Traditional Chinese translation per line (None where no line matched), or None for no translation.
    translations: tuple[str | None, ...] | None = None
    # Word timings per line from L3 (None for a line without them), or None for none at all.
    words: tuple[tuple[WordTiming, ...] | None, ...] | None = None

    @property
    def has_translation(self) -> bool:
        return bool(self.translations) and any(self.translations)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One lyric search hit from a source, before scoring."""

    source: str
    source_id: str
    title: str
    artist: str
    album: str = ""
    duration: float = 0.0
    synced_lrc: str | None = None
    plain_text: str | None = None
    instrumental: bool = False
    translation_lrc: str | None = None  # NetEase tlyric, timestamps match synced_lrc
    # Found by the title alone, the artist search having found nothing (SPEC v1.33): many
    # songs share a title, so the artist has to match on its own.
    title_only: bool = False

    @property
    def has_synced(self) -> bool:
        return bool(self.synced_lrc and self.synced_lrc.strip())

    @property
    def has_plain(self) -> bool:
        return bool(self.plain_text and self.plain_text.strip())


class ResultKind(Enum):
    SYNCED = "synced"
    PLAIN_ONLY = "plain_only"
    INSTRUMENTAL = "instrumental"
    NOT_FOUND = "not_found"
    SKIPPED_AD = "skipped_ad"
    NOT_A_SONG = "not_a_song"  # a long or artist-less browser video, not queried (SPEC v1.34, v1.43)
    SOURCES_FAILED = "sources_failed"  # every source errored or timed out


@dataclass(frozen=True, slots=True)
class LyricResult:
    track: TrackInfo
    kind: ResultKind
    lyrics: Lyrics | None = None
    candidate: Candidate | None = None
    score: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    offset: float = 0.0  # user offset in seconds for this song (FR-07); positive shows lyrics later
    from_cache: bool = False
    # Other synced versions from the same source with their scores, for the L1 consensus check.
    # None: not searched (exact lookup or cache hit); empty: searched, nothing else found.
    alternatives: tuple[tuple[Candidate, float], ...] | None = None
    timeline_checked: bool = False  # L1 consensus done (SPEC v1.9)
    bound: bool = False  # picked by the user (FR-12): never replaced by automatic checks
    # SPEC v1.29: the version's length is not the player's (e.g. the player plays the MV), so its
    # times are only relative: synced lines are moved by a cue (the user's or the start finder's),
    # and L3 times it from the audio.
    version_mismatch: bool = False
    # FR-21: applied when shown (shown_lyrics); `lyrics` keeps the source times for storing and reverting.
    alignment: Alignment | None = None
    l3_state: str | None = None  # None: not aligned yet; see lyrics.cache L3_* states

    def shown_lyrics(self) -> Lyrics | None:
        """The lyrics as displayed: L3 line times and word timings when there is an alignment."""
        lyrics, alignment = self.lyrics, self.alignment
        if lyrics is None or alignment is None or len(alignment.starts) != len(lyrics.lines):
            return lyrics
        lines = tuple(LyricLine(start, line.text) for start, line in zip(alignment.starts, lyrics.lines))
        return Lyrics(lines, True, lyrics.translations, alignment.words)  # aligned plain lyrics are synced now
