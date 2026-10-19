import pytest

from lyricfloat.media.timeline import RawTimeline, anchor_from_timeline, choose_session, standby_session
from lyricfloat.models import PlaybackStatus as St

WALL = 1_800_000_000.0
MONO = 5_000.0


def raw(position=10.0, duration=200.0, last_updated=WALL - 2.0, status=St.PLAYING):
    return RawTimeline(position, duration, last_updated, status)


def test_playing_position_is_extrapolated_from_last_updated():
    anchor = anchor_from_timeline(raw(), WALL, MONO)
    assert anchor.position == pytest.approx(12.0)
    assert anchor.at == MONO
    assert anchor.playing


def test_paused_position_is_not_extrapolated():
    anchor = anchor_from_timeline(raw(status=St.PAUSED), WALL, MONO)
    assert anchor.position == pytest.approx(10.0)
    assert not anchor.playing


def test_unset_last_updated_is_ignored():
    assert anchor_from_timeline(raw(last_updated=0.0), WALL, MONO).position == pytest.approx(10.0)


def test_last_updated_slightly_in_future_counts_as_now():
    assert anchor_from_timeline(raw(last_updated=WALL + 0.3), WALL, MONO).position == pytest.approx(10.0)


def test_stamp_that_would_overrun_the_track_is_not_trusted():
    anchor = anchor_from_timeline(raw(position=190.0, last_updated=WALL - 30.0), WALL, MONO)
    assert anchor.position == pytest.approx(190.0)


def test_ancient_stamp_is_not_trusted_without_duration():
    anchor = anchor_from_timeline(raw(duration=0.0, last_updated=WALL - 7200.0), WALL, MONO)
    assert anchor.position == pytest.approx(10.0)


def test_stale_pause_carries_position_forward():
    # Last playing report was 4 s ago; status flips to paused before the position updates.
    playing = raw(position=10.0, last_updated=WALL - 4.0)
    stale = raw(position=10.0, last_updated=WALL - 4.0, status=St.PAUSED)
    anchor = anchor_from_timeline(stale, WALL, MONO, previous=playing)
    assert anchor.position == pytest.approx(14.0)
    assert not anchor.playing


def test_fresh_pause_report_is_used_as_is():
    playing = raw(position=10.0, last_updated=WALL - 4.0)
    fresh = raw(position=13.95, last_updated=WALL - 0.05, status=St.PAUSED)
    assert anchor_from_timeline(fresh, WALL, MONO, previous=playing).position == pytest.approx(13.95)


def test_pause_without_previous_report_is_used_as_is():
    stale_looking = raw(position=10.0, last_updated=WALL - 4.0, status=St.PAUSED)
    assert anchor_from_timeline(stale_looking, WALL, MONO).position == pytest.approx(10.0)


def test_staying_paused_does_not_extrapolate():
    paused = raw(position=10.0, last_updated=WALL - 4.0, status=St.PAUSED)
    assert anchor_from_timeline(paused, WALL, MONO, previous=paused).position == pytest.approx(10.0)


def test_signature_ignores_sub_millisecond_noise():
    assert raw(position=10.0001).signature == raw(position=10.0004).signature
    assert raw(position=10.0).signature != raw(position=10.5).signature


# Session selection: follow Windows' current session unless it is not playing
# and another one is.

def test_current_playing_wins():
    assert choose_session("spotify", [("spotify", St.PLAYING), ("chrome", St.PLAYING)]) == "spotify"


def test_paused_current_yields_to_playing_other():
    assert choose_session("spotify", [("spotify", St.PAUSED), ("chrome", St.PLAYING)]) == "chrome"


def test_followed_session_is_sticky_among_playing_ones():
    sessions = [("spotify", St.PAUSED), ("chrome", St.PLAYING), ("other", St.PLAYING)]
    assert choose_session("spotify", sessions, followed="other") == "other"


def test_nothing_playing_follows_no_new_player():
    # SPEC v1.49: at startup a paused Spotify is not shown; the toolbar still controls it
    sessions = [("chrome", St.PAUSED), ("spotify", St.PAUSED)]
    assert choose_session("spotify", sessions) is None
    assert standby_session("spotify", sessions) == "spotify"


def test_a_paused_followed_player_is_kept_over_the_current_one():
    # a browser between two videos: Windows names the paused Spotify current
    sessions = [("chrome", St.PAUSED), ("spotify", St.PAUSED)]
    assert choose_session("spotify", sessions, followed="chrome") == "chrome"


def test_a_followed_player_gone_does_not_hand_over_to_a_paused_one():
    # the browser's session closes between two videos; Spotify sits paused
    assert choose_session("spotify", [("spotify", St.PAUSED)], followed="chrome") is None


def test_no_current_and_nothing_playing_keeps_followed():
    assert choose_session(None, [("chrome", St.PAUSED), ("spotify", St.PAUSED)], followed="spotify") == "spotify"


def test_no_sessions_gives_none():
    assert choose_session(None, []) is None


def test_stale_current_not_in_list_is_skipped():
    assert choose_session("gone", [("chrome", St.PAUSED), ("spotify", St.PLAYING)]) == "spotify"
    assert standby_session("gone", [("chrome", St.PAUSED)]) == "chrome"
    assert standby_session(None, []) is None


# --- Spotify quirks from the M1 measurement ------------------------------------------

from lyricfloat.media.timeline import PauseFlapFilter, RestartHold  # noqa: E402


def test_flap_right_after_resume_is_dropped():
    f = PauseFlapFilter()
    assert f.accept(St.PAUSED, 0.0)
    assert f.accept(St.PLAYING, 10.0)  # resume
    assert not f.accept(St.PAUSED, 10.4)  # flap: held
    assert not f.accept(St.PAUSED, 11.0)
    assert f.accept(St.PLAYING, 11.4)  # playing again: the pause never happened


def test_real_pause_after_resume_is_applied_after_the_hold():
    f = PauseFlapFilter()
    f.accept(St.PLAYING, 10.0)
    assert not f.accept(St.PAUSED, 10.5)
    assert not f.accept(St.PAUSED, 11.9)
    assert f.accept(St.PAUSED, 12.0)  # held 1.5 s: real pause


def test_pause_long_after_resume_is_immediate():
    f = PauseFlapFilter()
    f.accept(St.PLAYING, 10.0)
    assert f.accept(St.PAUSED, 15.0)


def test_other_statuses_pass_through():
    f = PauseFlapFilter()
    f.accept(St.PLAYING, 10.0)
    assert f.accept(St.STOPPED, 10.2)
    assert f.accept(St.CHANGING, 10.3)


def test_restart_is_held_then_applied():
    h = RestartHold()
    assert h.check(predicted=180.0, position=0.2, now=100.0)
    assert h.check(predicted=180.0, position=0.4, now=100.6)
    assert not h.check(predicted=180.0, position=1.1, now=101.0)  # 1 s: a loop or a seek to start


def test_normal_progress_and_small_jumps_are_not_held():
    h = RestartHold()
    assert not h.check(predicted=60.0, position=60.1, now=1.0)
    assert not h.check(predicted=4.0, position=0.5, now=2.0)  # near the start anyway
    assert not h.check(predicted=60.0, position=40.0, now=3.0)  # backward seek, not to the start
    assert not h.check(predicted=None, position=0.0, now=4.0)  # new song, nothing predicted


def test_restart_hold_resets_when_progress_resumes():
    h = RestartHold()
    assert h.check(180.0, 0.2, 100.0)
    assert not h.check(5.0, 5.1, 100.5)  # a normal report clears the pending hold
    assert h.check(180.0, 0.2, 200.0)  # a later restart is held afresh


from lyricfloat.media.timeline import restrict_to_pinned  # noqa: E402

APP = {"spotify": "Spotify.exe", "chrome-a": "Chrome", "chrome-b": "Chrome"}


def test_no_pin_keeps_everything():
    sessions = [("spotify", St.PLAYING), ("chrome-a", St.PAUSED)]
    assert restrict_to_pinned("spotify", sessions, "", APP.get) == ("spotify", sessions)


def test_pin_hides_other_players():
    sessions = [("spotify", St.PLAYING), ("chrome-a", St.PAUSED), ("chrome-b", St.PLAYING)]
    current, kept = restrict_to_pinned("spotify", sessions, "Chrome", APP.get)
    assert current is None and kept == [("chrome-a", St.PAUSED), ("chrome-b", St.PLAYING)]
    assert choose_session(current, kept) == "chrome-b"


def test_paused_pinned_player_is_kept_while_another_plays():
    current, kept = restrict_to_pinned("spotify", [("spotify", St.PLAYING), ("chrome-a", St.PAUSED)], "Chrome", APP.get)
    assert choose_session(current, kept, followed="chrome-a") == "chrome-a"
    assert choose_session(current, kept) is None  # not followed yet: waits until it plays
    assert standby_session(current, kept) == "chrome-a"


def test_pinned_player_not_open_gives_no_session():
    current, kept = restrict_to_pinned("spotify", [("spotify", St.PLAYING)], "Chrome", APP.get)
    assert choose_session(current, kept) is None



def test_a_session_without_playback_info_reads_as_closed():
    # Chrome once answered None between two videos (2026-10-08), which crashed the refresh.
    from lyricfloat.media.smtc import read_status
    from lyricfloat.models import PlaybackStatus

    class Session:
        def get_playback_info(self):
            return None

    assert read_status(Session()) is PlaybackStatus.CLOSED
