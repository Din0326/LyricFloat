"""Thin adapter over pywinrt's Windows.Media.Control. All winrt imports live here."""

from __future__ import annotations

import datetime as dt

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as Session,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)

from lyricfloat.media.timeline import RawTimeline
from lyricfloat.models import PlaybackStatus, TrackInfo

__all__ = [
    "Session",
    "SessionManager",
    "request_manager",
    "app_id",
    "read_status",
    "read_timeline",
    "read_track",
    "send_control",
    "CONTROLS",
]

# FR-15 playback control: action name -> session method
CONTROLS = {
    "previous": "try_skip_previous_async",
    "play_pause": "try_toggle_play_pause_async",
    "next": "try_skip_next_async",
}

_EPOCH_FLOOR = dt.datetime(1971, 1, 1, tzinfo=dt.timezone.utc)


async def request_manager() -> SessionManager:
    return await SessionManager.request_async()


def app_id(session: Session) -> str:
    try:
        return session.source_app_user_model_id or ""
    except OSError:
        return ""


def read_status(session: Session) -> PlaybackStatus:
    try:
        info = session.get_playback_info()
        if info is None:  # Chrome, between two videos, once answered nothing here (2026-10-08)
            return PlaybackStatus.CLOSED
        return PlaybackStatus(int(info.playback_status))
    except (OSError, ValueError):
        return PlaybackStatus.CLOSED


def _epoch(value: dt.datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.timestamp() if value >= _EPOCH_FLOOR else 0.0


def read_timeline(session: Session) -> RawTimeline:
    tl = session.get_timeline_properties()
    start = tl.start_time.total_seconds()
    end = tl.end_time.total_seconds()
    return RawTimeline(
        position=tl.position.total_seconds() - start,
        duration=max(0.0, end - start),
        last_updated=_epoch(tl.last_updated_time),
        status=read_status(session),
    )


async def send_control(session: Session, action: str) -> bool:
    """Ask the player to go to the previous/next track or toggle play/pause."""
    method = getattr(session, CONTROLS[action])
    return bool(await method())


async def read_track(session: Session) -> TrackInfo | None:
    props = await session.try_get_media_properties_async()
    if props is None:
        return None
    title = (props.title or "").strip()
    if not title:
        return None  # players blank the title while switching; wait for the real one
    try:
        duration = read_timeline(session).duration
    except OSError:
        duration = 0.0
    return TrackInfo(
        title=title,
        artist=(props.artist or props.album_artist or "").strip(),
        album=(props.album_title or "").strip(),
        duration=duration,
        source_app=app_id(session),
    )
