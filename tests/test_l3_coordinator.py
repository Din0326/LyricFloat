"""FR-21/22: which plays get recorded, how outcomes are stored, the controller hooks. Fakes only."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from lyricfloat.align.coordinator import STATUS_READY, STATUS_SOURCE_MISSING, L3Coordinator
from lyricfloat.align.recorder import Recording
from lyricfloat.align.runner import AlignOutcome
from lyricfloat.lyrics.cache import L3_APPLIED, L3_FAILED, L3_REVERTED
from lyricfloat.models import Alignment, LyricLine, LyricResult, Lyrics, PlaybackAnchor, ResultKind, TrackInfo
from lyricfloat.settings import Settings
from lyricfloat.ui import settings_dialog as dialog_module
from lyricfloat.ui.settings_dialog import SettingsDialog

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
OTHER = TrackInfo("Another Song", "The Example Band", "", 180.0)
LINES = (LyricLine(5.0, "first line"), LyricLine(10.0, "second line"))
ZH_LINES = (LyricLine(5.0, "這是第一句"), LyricLine(10.0, "這是第二句"))


class FakeRecorder(QObject):
    recorded = pyqtSignal(object)
    dropped = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.track = None
        self.calls = []

    @property
    def active(self):
        return self.track is not None

    def start(self, track, app_id, anchor):
        self.track = track
        self.calls.append(("start", track.title, app_id))

    def on_anchor(self, anchor):
        pass

    def finish(self):
        self.calls.append(("finish", self.track.title))
        self.track = None

    def cancel(self):
        if self.track is not None:
            self.calls.append(("cancel", self.track.title))
        self.track = None

    def live(self):
        return None


class FakeRunner(QObject):
    finished = pyqtSignal(object)

    def __init__(self, python=Path("python.exe")):
        super().__init__()
        self.python = python
        self.worker = Path("worker.py")
        self.models_dir = Path("models")
        self.jobs = []
        self.cleared = 0

    def submit(self, job):
        self.jobs.append(job)

    def clear(self):
        self.cleared += 1

    def shutdown(self):
        pass


class FakeFinder(QObject):
    """Stands in for the start finder (SPEC v1.36): records what it was asked to do."""

    found = pyqtSignal(object, float)
    searching = pyqtSignal(object, bool)
    missed = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.python = Path("python.exe")
        self.worker = Path("worker.py")
        self.calls = []
        self.search = None

    def start(self, track, search, live):
        self.calls.append(("start", track.title))
        self.search = search
        return True

    def stop(self):
        self.calls.append(("stop",))


class FakeService:
    def __init__(self):
        self.stored = []
        self.failed = []

    async def store_alignment(self, track, lines, segments, pos0, seconds):
        self.stored.append((track, lines, pos0, seconds))

        class Plan:
            stats = {"usable": True, "sure": 2}

        return Plan()

    async def alignment_failed(self, track):
        self.failed.append(track)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def l3(qapp):
    c = L3Coordinator(FakeService(), FakeRecorder(), FakeRunner(), t2s=lambda text: text.replace("這", "这"), finder=FakeFinder())
    c.set_enabled(True)
    c.on_session("Spotify.exe")
    c.on_anchor(PlaybackAnchor(1.0, time.monotonic(), True))
    return c


def synced(track=TRACK, lines=LINES, **changes):
    return replace(LyricResult(track, ResultKind.SYNCED, Lyrics(lines, True)), **changes)


def test_a_fresh_song_is_recorded(l3):
    l3.on_result(synced())
    assert l3.recorder.calls == [("start", TRACK.title, "Spotify.exe")]


@pytest.mark.parametrize(
    "changes",
    [
        {"bound": True},  # manual pick or edited version
        {"offset": 0.3},  # the user's offset wins (SPEC)
        {"alignment": Alignment((5.1, 10.1), (None, None))},
        {"l3_state": L3_APPLIED},
        {"l3_state": L3_REVERTED},
        {"kind": ResultKind.INSTRUMENTAL},
        {"kind": ResultKind.PLAIN_ONLY, "notes": ("source_failed",)},  # not cached: nowhere to keep it
    ],
)
def test_songs_the_user_owns_or_already_aligned_are_left_alone(l3, changes):
    l3.on_result(synced(**changes))
    assert l3.recorder.calls == []


def plain(**changes):
    return replace(LyricResult(TRACK, ResultKind.PLAIN_ONLY, Lyrics(tuple(LyricLine(0.0, l.text) for l in LINES), False)), **changes)


@pytest.mark.parametrize("result", [plain(), plain(bound=True)], ids=["plain", "bound or untimed edit"])
def test_plain_lyrics_are_recorded_for_timing(l3, result):
    l3.on_result(result)
    assert l3.recorder.active


@pytest.mark.parametrize(
    "changes",
    [{}, {"offset": 39.5}, {"bound": True, "offset": 39.5}],
    ids=["waiting for the cue", "cued", "bound and cued"],
)
def test_a_version_of_another_length_is_recorded_even_when_cued_or_bound(l3, changes):
    l3.on_result(synced(version_mismatch=True, **changes))  # SPEC v1.29
    assert l3.recorder.active


def test_a_version_of_another_length_already_aligned_is_left_alone(l3):
    l3.on_result(synced(version_mismatch=True, l3_state=L3_APPLIED, alignment=Alignment((5.1, 10.1), (None, None))))
    assert l3.recorder.calls == []


def test_a_failed_song_is_tried_again(l3):
    l3.on_result(synced(l3_state=L3_FAILED))
    assert l3.recorder.active


def test_a_song_already_past_15_seconds_is_not_recorded(l3):
    l3.on_anchor(PlaybackAnchor(30.0, time.monotonic(), True))
    l3.on_result(synced())
    assert l3.recorder.calls == []


def test_off_or_without_runtime_nothing_is_recorded(qapp):
    off = L3Coordinator(FakeService(), FakeRecorder(), FakeRunner())
    off.on_session("Spotify.exe")
    off.on_anchor(PlaybackAnchor(1.0, time.monotonic(), True))
    off.on_result(synced())
    missing = L3Coordinator(FakeService(), FakeRecorder(), FakeRunner(python=None))
    missing.set_enabled(True)
    missing.on_session("Spotify.exe")
    missing.on_anchor(PlaybackAnchor(1.0, time.monotonic(), True))
    missing.on_result(synced())
    assert off.recorder.calls == [] and missing.recorder.calls == []
    assert missing.status_text() == STATUS_SOURCE_MISSING and off.status_text() == STATUS_READY


def test_next_song_finishes_the_recording(l3):
    l3.on_result(synced())
    l3.on_track(TRACK)  # same song again: nothing
    l3.on_track(OTHER)
    assert l3.recorder.calls[-1] == ("finish", TRACK.title)


def test_lyrics_changing_mid_song_drop_the_recording(l3):
    l3.on_result(synced())
    l3.on_result(synced(lines=(LyricLine(6.0, "first line"), LyricLine(11.0, "second line"))))
    assert l3.recorder.calls[-1] == ("cancel", TRACK.title)


def test_switching_player_or_turning_off_drops_everything(l3):
    l3.on_result(synced())
    l3.on_session("Chrome")
    assert l3.recorder.calls[-1] == ("cancel", TRACK.title)
    l3.set_enabled(False)
    assert l3.runner.cleared == 1


def test_recordings_are_queued_with_t2s_text_for_chinese(l3, tmp_path):
    l3.on_result(synced(lines=ZH_LINES))
    rec = Recording(TRACK, tmp_path / "rec.pcm", 0.4, 199.0)
    l3.recorder.recorded.emit(rec)
    (job,) = l3.runner.jobs
    assert job.language == "zh" and job.texts == ("这是第一句", "这是第二句") and job.lines == ZH_LINES


def test_an_english_song_under_chinese_credits_is_aligned_in_english(l3, tmp_path):
    lines = (LyricLine(0.0, "作词 : 某人"), *LINES)
    l3.on_result(synced(lines=lines))
    l3.recorder.recorded.emit(Recording(TRACK, tmp_path / "rec.pcm", 0.4, 199.0))
    (job,) = l3.runner.jobs
    assert job.language == "en" and job.texts == ("作词 : 某人", "first line", "second line")  # SPEC v1.82


async def test_outcomes_are_stored_or_counted_as_failures(l3, tmp_path):
    l3.on_result(synced())
    rec = Recording(TRACK, tmp_path / "rec.pcm", 0.4, 199.0)
    l3.recorder.recorded.emit(rec)
    job = l3.runner.jobs[0]
    l3.runner.finished.emit(AlignOutcome(job, True, [{"start": 4.9, "words": []}], {"align_s": 1.0}))
    l3.runner.finished.emit(AlignOutcome(job, False, error="boom"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert l3.service.stored[0][2:] == (0.4, 199.0) and l3.service.failed == [TRACK]


# -- settings dialog -------------------------------------------------------------


def test_turning_l3_on_asks_first(qapp, tmp_path, monkeypatch):
    answers = [dialog_module.QMessageBox.StandardButton.No, dialog_module.QMessageBox.StandardButton.Yes]
    asked = []
    monkeypatch.setattr(dialog_module.QMessageBox, "question", lambda *a: (asked.append(a[2]), answers.pop(0))[1])
    d = SettingsDialog(Settings(), tmp_path / "v.txt", l3_status=STATUS_READY)
    d.l3_enabled.setChecked(True)
    assert not d.l3_enabled.isChecked() and "只錄下播放器" in asked[0]
    d.l3_enabled.setChecked(True)
    assert d.current().l3_enabled and d.l3_status.text() == STATUS_READY
    d.close()


def test_an_enabled_l3_opens_without_asking_again(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(dialog_module.QMessageBox, "question", lambda *a: pytest.fail("asked again"))
    d = SettingsDialog(replace(Settings(), l3_enabled=True), tmp_path / "v.txt")
    assert d.current().l3_enabled
    d.close()


async def test_statistics_record_how_the_start_was_judged(l3, tmp_path, caplog):
    import json
    import logging

    l3.on_result(synced())
    rec = Recording(TRACK, tmp_path / "rec.pcm", -0.2, 199.0, first_estimate=0.4, reports=55)
    l3.recorder.recorded.emit(rec)
    with caplog.at_level(logging.INFO, logger="lyricfloat.alignment"):
        l3.runner.finished.emit(AlignOutcome(l3.runner.jobs[0], True, [{"start": 4.9, "words": []}], {}))
        for _ in range(5):
            await asyncio.sleep(0)
    record = json.loads([r.getMessage() for r in caplog.records if r.name == "lyricfloat.alignment"][-1])
    assert record["pos0_s"] == -0.2 and record["reports"] == 55 and record["start_fix_s"] == pytest.approx(-0.6)


# -- versions of another length: finding the singing start (SPEC v1.36) ----------------------

from lyricfloat.models import Candidate  # noqa: E402

MV_LINES = (LyricLine(0.0, "作詞 : 某人"), LyricLine(20.0, "first line"), LyricLine(24.0, "second line"), LyricLine(28.0, "third line"))


def mv(**changes):
    cand = Candidate("lrclib", "1", TRACK.title, TRACK.artist, "", 90.0, "[00:20.00]x")  # TRACK runs 200 s
    return synced(lines=MV_LINES, candidate=cand, version_mismatch=True, **changes)


def test_an_uncued_mv_is_listened_to_for_its_start(l3):
    l3.on_result(mv())
    assert l3.finder.calls == [("start", TRACK.title)]
    search = l3.finder.search
    assert search.gap == pytest.approx(110.0) and [l.text for l in search.lines] == ["first line", "second line", "third line"]


def test_a_cued_mv_is_recorded_but_not_listened_to(l3):
    l3.on_result(mv(offset=42.0))
    assert l3.recorder.active and l3.finder.calls == []


def test_a_new_song_or_a_cue_stops_the_search(l3):
    l3.on_result(mv())
    l3.on_cue(42.0)
    assert l3.finder.calls[-1] == ("stop",)
    l3.on_track(OTHER)
    assert l3.finder.calls[-1] == ("stop",) and l3.recorder.calls[-1] == ("finish", TRACK.title)


def test_the_start_finders_signals_reach_the_app(l3):
    got = []
    l3.startFound.connect(lambda track, offset: got.append(("found", offset)))
    l3.startSearching.connect(lambda track, on: got.append(("searching", on)))
    l3.startMissed.connect(lambda track: got.append(("missed",)))
    l3.finder.searching.emit(TRACK, True)
    l3.finder.found.emit(TRACK, 41.5)
    l3.finder.missed.emit(TRACK)
    assert got == [("searching", True), ("found", 41.5), ("missed",)]


async def test_the_whole_song_alignment_leaves_out_a_known_intro(l3, tmp_path):
    l3.on_result(mv())
    l3.on_cue(42.0)  # sung from 62 s: the intro before 57 s is left out
    rec = Recording(TRACK, tmp_path / "rec.pcm", 0.4, 199.0)
    l3.recorder.recorded.emit(rec)
    (job,) = l3.runner.jobs
    assert job.skip == pytest.approx(20.0 + 42.0 - 5.0 - 0.4)
    l3.runner.finished.emit(AlignOutcome(job, True, [{"start": 4.9, "words": []}] * 4, {}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert l3.service.stored[0][2:] == (pytest.approx(57.0), pytest.approx(199.0 - 56.6))


def own_length(**changes):
    cand = Candidate("lrclib", "1", TRACK.title, TRACK.artist, "", 199.0, "[00:20.00]x")  # TRACK runs 200 s
    return synced(lines=MV_LINES, candidate=cand, **changes)


def test_every_synced_song_is_listened_to_for_its_start(l3):
    # SPEC v1.74 (Din, over v1.24): not only versions of another length.
    l3.on_result(own_length())
    assert l3.finder.calls == [("start", TRACK.title)] and l3.finder.search.gap == pytest.approx(1.0)


def test_a_song_with_the_users_offset_is_not_listened_to(l3):
    l3.on_result(own_length(offset=1.5))  # the user's timing: not recorded, not listened to
    assert not l3.recorder.active and l3.finder.calls == []


def test_songs_of_the_right_length_are_aligned_whole(l3, tmp_path):
    l3.on_result(own_length())
    l3.on_cue(2.0)  # a start found for a song of its own length: the whole song is still timed
    l3.recorder.recorded.emit(Recording(TRACK, tmp_path / "rec.pcm", 0.4, 199.0))
    assert l3.runner.jobs[0].skip == 0.0
