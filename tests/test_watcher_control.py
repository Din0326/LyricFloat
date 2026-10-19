"""FR-15: MediaWatcher forwards player controls to the followed SMTC session."""

from lyricfloat.media.watcher import MediaWatcher


class FakeSession:
    def __init__(self, accept=True, fail=False):
        self.accept = accept
        self.fail = fail
        self.calls = []

    def _call(self, name):
        async def run():
            self.calls.append(name)
            if self.fail:
                raise OSError("session closed")
            return self.accept

        return run

    def __getattr__(self, name):
        if name.startswith("try_"):
            return self._call(name)
        raise AttributeError(name)


async def test_controls_map_to_session_methods():
    w = MediaWatcher()
    w._session = FakeSession()
    for action in ("previous", "play_pause", "next"):
        assert await w.control(action)
    assert w._session.calls == ["try_skip_previous_async", "try_toggle_play_pause_async", "try_skip_next_async"]


async def test_skip_marks_the_metadata_for_rereading():
    w = MediaWatcher()
    w._session = FakeSession()
    w._media_dirty = False
    await w.control("play_pause")
    assert not w._media_dirty and w._wake.is_set()
    await w.control("next")
    assert w._media_dirty


async def test_no_session_or_failure_returns_false():
    w = MediaWatcher()
    assert not await w.control("next")
    w._session = FakeSession(fail=True)
    assert not await w.control("next")
    w._session = FakeSession(accept=False)
    assert not await w.control("next")


class FakeManager:
    def __init__(self, ids):
        self.ids = ids

    def get_sessions(self):
        return [type("S", (), {"source_app_user_model_id": i})() for i in self.ids]


def test_players_lists_distinct_app_ids():
    w = MediaWatcher()
    assert w.players() == []
    w._manager = FakeManager(["Spotify.exe", "Chrome", "Chrome", ""])
    assert w.players() == ["Chrome", "Spotify.exe"]


async def test_pinning_rereads_the_sessions():
    w = MediaWatcher()
    w._media_dirty = False
    w.set_pinned("Chrome")
    assert w.pinned == "Chrome" and w._media_dirty and w._wake.is_set()


# SPEC v1.49: a player that is not playing is never switched to; with nothing followed
# the toolbar controls the paused player Windows names current.

class SmtcSession(FakeSession):
    def __init__(self, app, status):
        super().__init__()
        self.source_app_user_model_id = app
        self.status = status
        self.listeners = 0

    def get_playback_info(self):
        return type("Info", (), {"playback_status": self.status})()

    def add_playback_info_changed(self, handler):
        self.listeners += 1
        return object()

    def remove_playback_info_changed(self, token):
        self.listeners -= 1


class SmtcManager:
    def __init__(self, sessions, current):
        self.sessions = sessions
        self.current = current

    def get_sessions(self):
        return list(self.sessions)

    def get_current_session(self):
        return self.current


def watching(manager):
    w = MediaWatcher()
    w._manager = manager
    events = []
    w.trackChanged.connect(lambda t: events.append(("track", t)))
    w.sessionChanged.connect(lambda a: events.append(("session", a)))
    w.standbyChanged.connect(lambda ok: events.append(("standby", ok)))
    return w, events


async def test_a_paused_player_at_startup_is_not_followed_but_can_be_started():
    spotify = SmtcSession("Spotify.exe", 5)  # paused
    w, events = watching(SmtcManager([spotify], spotify))
    await w._refresh()
    assert w._session is None and w.track is None
    assert events == [("standby", True)]
    assert spotify.listeners == 1  # its playback events wake the watcher
    await w._refresh()
    assert events == [("standby", True)]  # unchanged: no repeat
    assert await w.control("play_pause")
    assert spotify.calls == ["try_toggle_play_pause_async"]


async def test_a_closed_browser_session_does_not_hand_over_to_a_paused_player():
    spotify = SmtcSession("Spotify.exe", 5)
    chrome = SmtcSession("Chrome", 4)
    manager = SmtcManager([spotify], spotify)
    w, events = watching(manager)
    w._session = chrome  # followed while it played; its session closed between two videos
    await w._refresh()
    assert w._session is None
    assert events == [("session", ""), ("track", None), ("standby", True)]
    manager.sessions = []
    await w._refresh()
    assert events[-1] == ("standby", False) and spotify.listeners == 0
