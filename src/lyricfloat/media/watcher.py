"""MediaWatcher: follows the active SMTC session and reports track and position.

WinRT events arrive on thread-pool threads; they only wake the asyncio
loop, and all reads and signal emissions happen on the loop thread. A
poll runs alongside the events because some players (Spotify among them)
do not fire timeline events while playing normally.
"""

from __future__ import annotations

import asyncio
import logging
import time

from PyQt6.QtCore import QObject, pyqtSignal

from lyricfloat.media import smtc
from lyricfloat.media.timeline import (
    PauseFlapFilter,
    RawTimeline,
    RestartHold,
    anchor_from_timeline,
    choose_session,
    restrict_to_pinned,
    standby_session,
)
from lyricfloat.models import PlaybackAnchor, PlaybackStatus, TrackInfo

log = logging.getLogger(__name__)


class MediaWatcher(QObject):
    trackChanged = pyqtSignal(object)  # TrackInfo, or None when no session is left
    anchorChanged = pyqtSignal(object)  # PlaybackAnchor
    sessionChanged = pyqtSignal(str)  # source app id of the followed session ("" for none)
    standbyChanged = pyqtSignal(bool)  # nothing followed: whether a paused player can take the toolbar buttons

    def __init__(
        self,
        poll_playing: float = 0.5,
        poll_idle: float = 2.0,
        settle_delay: float = 0.25,
        metadata_refresh: float = 5.0,
        sleep_gap: float = 10.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.poll_playing = poll_playing
        self.poll_idle = poll_idle
        self.settle_delay = settle_delay
        self.metadata_refresh = metadata_refresh
        self.sleep_gap = sleep_gap

        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager = None
        self._manager_tokens: list[tuple[str, object]] = []
        self._session = None
        self._session_tokens: list[tuple[str, object]] = []
        self._standby = None  # SPEC v1.49: with nothing followed, the player the toolbar controls
        self._standby_token = None  # its playback events wake the loop, so pressing play is seen at once
        self._wake = asyncio.Event()
        self._media_dirty = True
        self._task: asyncio.Task | None = None

        self._track: TrackInfo | None = None
        self._last_signature: tuple | None = None
        self._last_raw: RawTimeline | None = None
        self._last_anchor: PlaybackAnchor | None = None
        self._flap = PauseFlapFilter()
        self._restart = RestartHold()
        self._last_metadata_read = 0.0
        self._last_refresh = 0.0
        self.pinned = ""  # FR-24: app id of the pinned player, "" follows Windows

    @property
    def track(self) -> TrackInfo | None:
        return self._track

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._manager = await smtc.request_manager()
        self._manager_tokens = [
            ("sessions_changed", self._manager.add_sessions_changed(self._on_winrt_event)),
            ("current_session_changed", self._manager.add_current_session_changed(self._on_winrt_event)),
        ]
        self._task = asyncio.ensure_future(self._run())

    async def control(self, action: str) -> bool:
        """FR-15: previous / play_pause / next on the followed session, or with none
        followed, on the paused player Windows names current (SPEC v1.49)."""
        session = self._session if self._session is not None else self._standby
        if session is None:
            return False
        try:
            ok = await smtc.send_control(session, action)
        except OSError as exc:
            log.warning("control %s failed: %s", action, exc)
            return False
        log.info("control %s -> %s", action, "ok" if ok else "refused by the player")
        if ok:
            self._mark(media=action != "play_pause")  # track or status is about to change
        return ok

    def set_pinned(self, app_id: str) -> None:
        if app_id != self.pinned:
            log.info("pinned player: %s", app_id or "(auto)")
            self.pinned = app_id
            self._mark(media=True)

    def players(self) -> list[str]:
        """App ids of the players that have a media session right now."""
        if self._manager is None:
            return []
        try:
            ids = {smtc.app_id(s) for s in self._manager.get_sessions()}
        except OSError:
            return []
        return sorted(i for i in ids if i)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._unsubscribe_session()
        self._set_standby(None)
        if self._manager is not None:
            for name, token in self._manager_tokens:
                getattr(self._manager, f"remove_{name}")(token)
        self._manager_tokens = []

    # -- WinRT thread side -------------------------------------------------

    def _on_winrt_event(self, sender, args) -> None:
        self._wake_from_thread(media=False)

    def _on_media_event(self, sender, args) -> None:
        self._wake_from_thread(media=True)

    def _wake_from_thread(self, media: bool) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._mark, media)
        except RuntimeError:
            pass  # loop shutting down

    def _mark(self, media: bool) -> None:
        if media:
            self._media_dirty = True
        self._wake.set()

    # -- loop side ---------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("media refresh failed")
            playing = self._last_signature is not None and self._last_signature[2] is PlaybackStatus.PLAYING
            timeout = self.poll_playing if playing else self.poll_idle
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass
            self._wake.clear()

    def _subscribe_session(self, session) -> None:
        self._session = session
        self._session_tokens = [
            ("media_properties_changed", session.add_media_properties_changed(self._on_media_event)),
            ("playback_info_changed", session.add_playback_info_changed(self._on_winrt_event)),
            ("timeline_properties_changed", session.add_timeline_properties_changed(self._on_winrt_event)),
        ]

    def _unsubscribe_session(self) -> None:
        if self._session is not None:
            for name, token in self._session_tokens:
                try:
                    getattr(self._session, f"remove_{name}")(token)
                except OSError:
                    pass  # session already gone
        self._session = None
        self._session_tokens = []

    def _pick_session(self):
        """(session to follow, player for the toolbar when that is None)."""
        current = self._manager.get_current_session()
        sessions = [(s, smtc.read_status(s)) for s in self._manager.get_sessions()]
        current, sessions = restrict_to_pinned(current, sessions, self.pinned, smtc.app_id)
        session = choose_session(current, sessions, self._session)
        return session, (standby_session(current, sessions) if session is None else None)

    def _set_standby(self, session) -> None:
        if session == self._standby:
            return
        if self._standby is not None:
            try:
                self._standby.remove_playback_info_changed(self._standby_token)
            except OSError:
                pass  # session already gone
        self._standby = session
        self._standby_token = None
        if session is not None:
            self._standby_token = session.add_playback_info_changed(self._on_winrt_event)
            log.info("nothing playing; the toolbar controls %s", smtc.app_id(session))

    async def _refresh(self) -> None:
        mono_now = time.monotonic()
        gap = mono_now - self._last_refresh if self._last_refresh else 0.0
        self._last_refresh = mono_now
        if gap > self.sleep_gap:
            # Likely resumed from sleep: never keep extrapolating a pre-sleep anchor.
            log.info("refresh gap %.1fs, re-reading session state", gap)
            self._forget_timeline()
            self._media_dirty = True

        session, standby = self._pick_session()
        if session is None:
            dropped = self._session is not None or self._track is not None
            if dropped:
                self._unsubscribe_session()
                self._track = None
                self._forget_timeline()
                self.sessionChanged.emit("")
                self.trackChanged.emit(None)
            had = self._standby is not None
            self._set_standby(standby)
            if dropped or had != (standby is not None):
                self.standbyChanged.emit(standby is not None)
            return
        self._set_standby(None)

        if session != self._session:
            self._unsubscribe_session()
            self._subscribe_session(session)
            self._media_dirty = True
            self._forget_timeline()
            log.info("following session %s", smtc.app_id(session))
            self.sessionChanged.emit(smtc.app_id(session))

        await self._refresh_track(session, mono_now)
        self._refresh_timeline(session)

    async def _refresh_track(self, session, mono_now: float) -> None:
        stale = mono_now - self._last_metadata_read > self.metadata_refresh
        if not (self._media_dirty or stale or self._track is None):
            return
        if self._media_dirty and self.settle_delay:
            # Players often publish title and artist in separate updates.
            await asyncio.sleep(self.settle_delay)
        self._media_dirty = False
        self._last_metadata_read = time.monotonic()
        track = await smtc.read_track(session)
        if track is None:
            return
        if self._track is None or track.key != self._track.key:
            self._track = track
            self._forget_timeline()  # force a fresh anchor for the new track
            log.info("track: %s | album=%s | %.1fs | %s", track.describe(), track.album, track.duration, track.source_app)
            self.trackChanged.emit(track)
        elif track.duration and not self._track.duration:
            self._track = track  # duration arrived late; same song, no re-query

    def _refresh_timeline(self, session) -> None:
        try:
            raw: RawTimeline = smtc.read_timeline(session)
        except OSError:
            return
        if raw.signature == self._last_signature:
            return
        now = time.monotonic()
        if not self._flap.accept(raw.status, now):
            log.debug("holding a pause right after resume (Spotify reports a brief one)")
            return
        if self._restart.check(self._predicted(now), raw.position, now):
            log.debug("holding a jump to the start: a track change may follow")
            self._media_dirty = True  # read the metadata on the next refresh
            self._wake_soon(0.3)
            return
        # A forced refresh (new track, new session, wake from sleep) clears the
        # signature; the previous raw report is then meaningless as well.
        previous = self._last_raw if self._last_signature is not None else None
        self._last_signature = raw.signature
        self._last_raw = raw
        self._last_anchor = anchor_from_timeline(raw, time.time(), now, previous)
        self.anchorChanged.emit(self._last_anchor)

    def _predicted(self, now: float) -> float | None:
        anchor = self._last_anchor
        if anchor is None:
            return None
        return anchor.position + (now - anchor.at) if anchor.playing else anchor.position

    def _forget_timeline(self) -> None:
        self._last_signature = None
        self._last_raw = None
        self._last_anchor = None
        self._flap.reset()
        self._restart.reset()

    def _wake_soon(self, delay: float) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_later(delay, self._wake.set)
