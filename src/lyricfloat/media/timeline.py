"""Pure helpers for SMTC timeline data and session selection (no winrt imports)."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from lyricfloat.models import PlaybackAnchor, PlaybackStatus

S = TypeVar("S", bound=Hashable)

# A LastUpdatedTime older than this while "playing" is treated as meaningless.
MAX_EXTRAPOLATION = 3600.0


@dataclass(frozen=True, slots=True)
class RawTimeline:
    position: float  # seconds, as reported
    duration: float  # end_time - start_time; 0 if unknown
    last_updated: float  # epoch seconds of LastUpdatedTime; 0 if unset
    status: PlaybackStatus

    @property
    def signature(self) -> tuple[float, float, PlaybackStatus]:
        """What has to change for the report to count as a new update."""
        return (round(self.position, 3), round(self.last_updated, 3), self.status)


def is_stale_pause(previous: RawTimeline | None, raw: RawTimeline) -> bool:
    """Playback just stopped but the timeline still carries the last playing report.

    Status and timeline are separate reads; Spotify updates the status a few
    milliseconds before the position, so a read can land in between and pair
    "paused" with a position that is seconds old.
    """
    return (
        previous is not None
        and previous.status is PlaybackStatus.PLAYING
        and raw.status is not PlaybackStatus.PLAYING
        and raw.position == previous.position
        and raw.last_updated == previous.last_updated
    )


def anchor_from_timeline(
    raw: RawTimeline, wall_now: float, mono_now: float, previous: RawTimeline | None = None
) -> PlaybackAnchor:
    """Turn an SMTC timeline report into a position pinned to the monotonic clock.

    SMTC reports Position as of LastUpdatedTime, not as of "now". While
    playing, the current position is Position + (now - LastUpdatedTime).
    If the stamp is unset, in the future by a lot, or would push playback
    past the end of the track, it is not trusted and Position is used as is.

    `previous` is the last report read; when it shows a stale pause the
    position is carried forward to the moment of pausing instead of
    jumping back to the old report.
    """
    playing = raw.status is PlaybackStatus.PLAYING
    position = raw.position
    if (playing or is_stale_pause(previous, raw)) and raw.last_updated > 0:
        elapsed = max(0.0, wall_now - raw.last_updated)
        past_end = raw.duration > 0 and position + elapsed > raw.duration + 1.0
        if elapsed <= MAX_EXTRAPOLATION and not past_end:
            position += elapsed
    return PlaybackAnchor(position=position, at=mono_now, playing=playing, duration=raw.duration)


class PauseFlapFilter:
    """Spotify reports Paused for about a second right after resuming (M1 measurement).

    A pause arriving within `window` seconds of a resume is held for `hold`
    seconds; if playback is reported again in the meantime it was a flap and
    is dropped. A real pause is applied once the hold runs out.
    """

    def __init__(self, window: float = 2.0, hold: float = 1.5) -> None:
        self.window = window
        self.hold = hold
        self._last: PlaybackStatus | None = None
        self._resumed_at: float | None = None
        self._held_since: float | None = None

    def accept(self, status: PlaybackStatus, now: float) -> bool:
        """True: pass the report on. False: hold it for now."""
        if status is PlaybackStatus.PLAYING:
            if self._last is not PlaybackStatus.PLAYING:
                self._resumed_at = now
            self._held_since = None
            self._last = status
            return True
        recently_resumed = (
            self._last is PlaybackStatus.PLAYING
            and self._resumed_at is not None
            and now - self._resumed_at < self.window
        )
        if status is PlaybackStatus.PAUSED and (recently_resumed or self._held_since is not None):
            if self._held_since is None:
                self._held_since = now
            if now - self._held_since < self.hold:
                return False
        self._held_since = None
        self._last = status
        return True

    def reset(self) -> None:
        self._last = None
        self._resumed_at = None
        self._held_since = None


class RestartHold:
    """Spotify resets the position to 0 shortly before it changes the title on a
    track change (M1 measurement), which showed the old song's intro for a moment.

    A jump back to the start is held for up to `hold` seconds so a pending
    track change can arrive first; after that it is applied (loop, or a seek
    to the beginning).
    """

    START = 3.0  # seconds: "back at the start"
    MIN_JUMP = 5.0  # seconds back from where playback was

    def __init__(self, hold: float = 1.0) -> None:
        self.hold = hold
        self._since: float | None = None

    def check(self, predicted: float | None, position: float, now: float) -> bool:
        """True: hold this report for now."""
        if predicted is None or position > self.START or predicted - position < self.MIN_JUMP:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        if now - self._since < self.hold:
            return True
        self._since = None
        return False

    def reset(self) -> None:
        self._since = None


def restrict_to_pinned(
    current: S | None,
    sessions: Sequence[tuple[S, PlaybackStatus]],
    pinned: str,
    app_id: Callable[[S], str],
) -> tuple[S | None, list[tuple[S, PlaybackStatus]]]:
    """FR-24: with a pinned player, only that player's sessions count."""
    if not pinned:
        return current, list(sessions)
    kept = [(s, st) for s, st in sessions if app_id(s) == pinned]
    return (current if current is not None and app_id(current) == pinned else None), kept


def choose_session(current: S | None, sessions: Sequence[tuple[S, PlaybackStatus]], followed: S | None = None) -> S | None:
    """Spec rule: follow the Windows current session; if it is not playing
    and another session is, follow the playing one.

    `followed` (the session shown last time) wins ties among playing sessions
    so the display does not flap between two players.

    SPEC v1.49 (Din): a player that is not playing is never switched to. With
    nothing playing, the followed session is kept while it lasts (its lyrics
    fade on their own); without one, nothing is followed, so a paused player
    does not show its song at startup or in the gap between two browser videos.
    """
    status = dict(sessions)
    if current is not None and status.get(current) is PlaybackStatus.PLAYING:
        return current
    playing = [s for s, st in sessions if st is PlaybackStatus.PLAYING]
    if followed is not None and followed in playing:
        return followed
    if playing:
        return playing[0]
    if followed is not None and followed in status:
        return followed
    return None


def standby_session(current: S | None, sessions: Sequence[tuple[S, PlaybackStatus]]) -> S | None:
    """With nothing followed, the player the toolbar buttons control: Windows' current one."""
    names = [s for s, _ in sessions]
    if current is not None and current in names:
        return current
    return names[0] if names else None
