"""L3 song recorder: what counts as a whole-song recording (SPEC v1.22). Synthetic audio only."""

import array

import pytest
from PyQt6.QtCore import QCoreApplication

from lyricfloat.align.processes import exe_for, top_process
from lyricfloat.align.recorder import CHANNELS, RATE, SongRecorder, cleanup_temp
from lyricfloat.models import PlaybackAnchor, TrackInfo

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 10.0)
T0 = 1000.0  # monotonic time of the first recorded sample


class FakeStream:
    def __init__(self, seconds=10.0, level=2000):
        self.seconds = seconds
        self.level = level
        self.closed = False

    def run(self, sink, stop):
        frames = int(self.seconds * RATE)
        chunk_frames = RATE // 2
        sample = array.array("h", [self.level, -self.level] * chunk_frames).tobytes()
        sent = 0
        while sent < frames and not stop.is_set():
            n = min(chunk_frames, frames - sent)
            sink(sample[: n * CHANNELS * 2], T0 + sent / RATE)
            sent += n
        stop.wait(5)

    def close(self):
        self.closed = True


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def recorder(tmp_path, stream=None, error=None):
    def open_stream(app_id):
        if error:
            raise OSError(error)
        return stream or FakeStream()

    return SongRecorder(tmp_path / "tmp", open_stream)


def anchor(position, at=T0, playing=True):
    return PlaybackAnchor(position, at, playing, TRACK.duration)


def finish_after_streaming(rec, seconds):
    want = int(seconds * RATE) * CHANNELS * 2
    for _ in range(2000):
        if rec._thread.bytes >= want:
            break
        rec._thread.join(0.005)
    return rec.finish()


def test_whole_song_is_kept(app, tmp_path):
    rec = recorder(tmp_path)
    got = []
    rec.recorded.connect(got.append)
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    rec.on_anchor(anchor(4.5, at=T0 + 4.0))
    result = finish_after_streaming(rec, 10.0)
    assert result is not None and got == [result]
    assert result.pos0 == pytest.approx(0.5) and result.seconds == pytest.approx(10.0)
    assert result.path.exists() and result.path.stat().st_size == int(10 * RATE) * CHANNELS * 2


@pytest.mark.parametrize(
    "anchors",
    [
        [anchor(3.0, at=T0 + 2.5, playing=False)],  # paused
        [anchor(9.0, at=T0 + 2.0)],  # jumped ahead: a seek
    ],
)
def test_pause_or_seek_drops_the_recording(app, tmp_path, anchors):
    rec = recorder(tmp_path)
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    for a in anchors:
        rec.on_anchor(a)
    assert finish_after_streaming(rec, 10.0) is None
    assert list((tmp_path / "tmp").glob("*.pcm")) == []


def test_late_start_is_dropped(app, tmp_path):
    rec = recorder(tmp_path)
    rec.start(TrackInfo("Long", "Band", "", 100.0), "Spotify.exe", anchor(20.0))
    assert finish_after_streaming(rec, 10.0) is None


def test_incomplete_recording_is_dropped(app, tmp_path):
    rec = recorder(tmp_path, FakeStream(seconds=5.0))
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    assert finish_after_streaming(rec, 5.0) is None


def test_muted_player_is_dropped(app, tmp_path):
    rec = recorder(tmp_path, FakeStream(level=0))
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    assert finish_after_streaming(rec, 10.0) is None


def test_capture_error_is_dropped(app, tmp_path):
    rec = recorder(tmp_path, error="no process")
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    rec._thread.join(1)
    assert rec.finish() is None


def test_cancel_deletes_the_file(app, tmp_path):
    rec = recorder(tmp_path)
    rec.start(TRACK, "Spotify.exe", anchor(0.5))
    rec._thread.join(0.05)
    rec.cancel()
    assert not rec.active and list((tmp_path / "tmp").glob("*.pcm")) == []


def test_cleanup_removes_leftovers(tmp_path):
    (tmp_path / "rec-old.pcm").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("x")
    assert cleanup_temp(tmp_path) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


@pytest.mark.parametrize(
    "app_id, exe",
    [("Spotify.exe", "Spotify.exe"), ("Chrome", "chrome.exe"), ("MSEdge", "msedge.exe"), ("SpotifyAB.SpotifyMusic_x!Spotify", "Spotify.exe")],
)
def test_exe_for(app_id, exe):
    assert exe_for(app_id) == exe


def test_top_process_skips_children():
    procs = [(10, 1, "chrome.exe"), (11, 10, "chrome.exe"), (12, 10, "chrome.exe"), (20, 1, "Spotify.exe")]
    assert top_process(procs, "chrome.exe") == 10
    assert top_process(procs, "spotify.exe") == 20
    assert top_process(procs, "firefox.exe") is None


def test_start_comes_from_all_reports_not_the_first(app, tmp_path):
    """Right after a track change Spotify reports the song as playing before its audio starts:
    the first report is ~0.6 s ahead, the later ones agree with each other (SPEC v1.28)."""
    rec = recorder(tmp_path)
    rec.start(TRACK, "Spotify.exe", anchor(0.6, at=T0))  # says 0.6 s at the first sample: wrong
    for k in range(1, 6):
        rec.on_anchor(anchor(0.0 + 1.5 * k, at=T0 + 1.5 * k))  # the song was really at 0.0
    result = finish_after_streaming(rec, 10.0)
    assert result.pos0 == pytest.approx(0.0, abs=1e-6)
    assert result.first_estimate == pytest.approx(0.6) and result.reports == 6


def test_a_start_before_the_song_stays_negative(app, tmp_path):
    rec = recorder(tmp_path)
    rec.start(TRACK, "Spotify.exe", anchor(-0.3, at=T0))
    rec.on_anchor(anchor(2.7, at=T0 + 3.0))
    result = finish_after_streaming(rec, 10.0)
    assert result.pos0 == pytest.approx(-0.3)  # recording began before the audio: no clamp at 0


def test_the_recording_can_be_read_while_it_grows(app, tmp_path):
    # SPEC v1.36: the start finder reads clips from the file before the song ends.
    rec = recorder(tmp_path)
    assert rec.live() is None
    rec.start(TRACK, "Spotify.exe", anchor(0.5, at=T0))
    want = int(4.0 * RATE) * CHANNELS * 2
    for _ in range(2000):
        if rec._thread.bytes >= want:
            break
        rec._thread.join(0.005)
    live = rec.live()
    assert live.first_at == T0 and live.pos0 == pytest.approx(0.5) and live.bytes >= want
    assert live.path.stat().st_size >= live.bytes  # unbuffered: everything counted is on disk already
    rec.on_anchor(anchor(9.0, at=T0 + 3.0))  # a jump: sought
    assert rec.live() is None
    rec.cancel()
