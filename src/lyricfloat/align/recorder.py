"""Record one whole song from the player for L3 (SPEC v1.22).

The audio streams to a temp file on a background thread, never into the
main process's memory. A recording counts only if it started within the
song's first 15 s, ran without a pause or seek, and reached the end; the
file of any other recording is deleted at once.
"""

from __future__ import annotations

import array
import logging
import statistics
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from lyricfloat.models import PlaybackAnchor, TrackInfo

log = logging.getLogger(__name__)

RATE, CHANNELS = 44100, 2  # what Demucs separates best
START_LIMIT = 15.0  # seconds into the song
MIN_COVERAGE = 0.85
END_SLACK = 15.0
SEEK_TOLERANCE = 2.0
MIN_PEAK = 100  # 16-bit sample peak; below this the player is (nearly) muted
TEMP_PATTERN = "rec-*.pcm"


@dataclass(frozen=True, slots=True)
class Recording:
    track: TrackInfo
    path: Path
    pos0: float  # song position of the first sample; negative when recording began before the song
    seconds: float
    rate: int = RATE
    channels: int = CHANNELS
    first_estimate: float | None = None  # pos0 from the report nearest the start alone (diagnostics)
    reports: int = 0  # playback reports the start was judged from


@dataclass(frozen=True, slots=True)
class LiveRecording:
    """A recording still being written, as far as it has got (SPEC v1.36: clips are read
    from it while the song plays). Bytes are contiguous from `first_at` on."""

    path: Path
    first_at: float  # monotonic time of the first sample
    bytes: int  # written so far
    pos0: float  # song position of the first sample, judged from the reports so far
    rate: int = RATE
    channels: int = CHANNELS


# Why a recording did not count, as shown in the statistics workbook.
DROP_REASONS = {
    "paused": "中途暫停",
    "seek": "拖動進度",
    "started late": "開頭太晚才錄到",
    "incomplete": "沒錄到結尾",
    "too quiet": "音量太低或靜音",
    "no audio": "沒有收到聲音",
    "capture unavailable": "無法擷取播放器聲音",
    "capture failed": "無法擷取播放器聲音",
    "lyrics changed": "播放中換了歌詞版本",
    "player switched": "中途換了播放器",
}


def reason_key(reason: str) -> str:
    """'started late (20.0s)' -> 'started late': the details stay in the log."""
    return reason.split(" (")[0].split(":")[0].strip()


def cleanup_temp(folder: Path) -> int:
    """Delete recordings left behind (a crash or a quit mid-song). Returns how many."""
    removed = 0
    for path in folder.glob(TEMP_PATTERN):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class _StreamThread(threading.Thread):
    def __init__(self, open_stream: Callable[[], object], path: Path) -> None:
        super().__init__(name="l3-recorder", daemon=True)
        self.open_stream = open_stream
        self.path = path
        self.stop = threading.Event()
        self.first_at: float | None = None
        self.bytes = 0
        self.peak = 0
        self.error: str | None = None

    def run(self) -> None:
        try:
            stream = self.open_stream()
        except Exception as exc:  # no such process, no audio device, old Windows
            self.error = f"capture unavailable: {exc}"
            return
        try:
            with self.path.open("wb", buffering=0) as f:  # unbuffered: clips are read while it grows

                def sink(chunk: bytes, at: float) -> None:
                    if self.first_at is None:
                        self.first_at = at
                    f.write(chunk)
                    self.bytes += len(chunk)
                    if chunk:
                        samples = array.array("h", chunk)
                        self.peak = max(self.peak, max(samples), -min(samples))

                stream.run(sink, self.stop)
        except Exception as exc:
            self.error = f"capture failed: {exc}"
        finally:
            try:
                stream.close()
            except Exception:
                pass


class SongRecorder(QObject):
    recorded = pyqtSignal(object)  # Recording: a valid whole-song recording, file kept for the aligner
    dropped = pyqtSignal(str)  # why a recording did not count (a reason key, see DROP_REASONS)

    def __init__(self, temp_dir: Path, open_stream: Callable[[str], object] | None = None, parent: QObject | None = None) -> None:
        """`open_stream(app_id)` makes the capture stream (on the recording thread)."""
        super().__init__(parent)
        self.temp_dir = temp_dir
        self._open_stream = open_stream or _open_loopback
        self._thread: _StreamThread | None = None
        self._track: TrackInfo | None = None
        self._anchors: list[PlaybackAnchor] = []
        self._invalid: str | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None

    @property
    def track(self) -> TrackInfo | None:
        return self._track

    def start(self, track: TrackInfo, app_id: str, anchor: PlaybackAnchor | None = None) -> None:
        self.cancel()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        path = self.temp_dir / f"rec-{uuid.uuid4().hex}.pcm"
        self._track = track
        self._anchors = [anchor] if anchor is not None else []
        self._invalid = None
        self._thread = _StreamThread(lambda: self._open_stream(app_id), path)
        self._thread.start()
        log.info("L3 recording %s", track.describe())

    def on_anchor(self, anchor: PlaybackAnchor) -> None:
        if self._thread is None or self._invalid:
            return
        if not anchor.playing:
            self._invalid = "paused"
        elif self._anchors and self._anchors[-1].playing:
            prev = self._anchors[-1]
            expected = prev.position + (anchor.at - prev.at)
            if abs(anchor.position - expected) > SEEK_TOLERANCE:
                self._invalid = "seek"
        self._anchors.append(anchor)

    def live(self) -> LiveRecording | None:
        """The recording so far, or None when there is none or it no longer counts (paused, sought)."""
        thread = self._thread
        if thread is None or self._invalid or thread.error or thread.first_at is None:
            return None
        pos0 = self._start_of(thread.first_at)
        if pos0 is None:
            return None
        return LiveRecording(thread.path, thread.first_at, thread.bytes, pos0)

    def _start_of(self, first_at: float) -> float | None:
        # Every report during the recording says where the song was at the first sample.
        # Their median, not the report nearest the start: right after a track change Spotify
        # reports the new song as already playing before its audio starts, which put whole
        # songs about 0.6 s late (SPEC v1.28). Later reports, every few seconds, are accurate.
        estimates = [a.position + (first_at - a.at) for a in self._anchors if a.playing]
        return statistics.median(estimates) if estimates else None

    def cancel(self) -> None:
        """Stop and throw the recording away (track gone mid-song, L3 turned off, quitting)."""
        thread = self._stop()
        if thread is not None:
            _delete(thread.path)

    def finish(self) -> Recording | None:
        """The song ended: keep the recording if it counts, else delete it."""
        thread = self._stop()
        if thread is None:
            return None
        reason = self._invalid or thread.error
        recording = None
        if reason is None:
            recording, reason = self._judge(thread)
        if recording is None:
            log.info("L3 recording of %s dropped: %s", self._track.describe(), reason)
            _delete(thread.path)
            self.dropped.emit(reason_key(reason))
            return None
        log.info(
            "L3 recording kept: %.0fs from %.2fs of %s (%d reports; the nearest alone said %.2fs)",
            recording.seconds, recording.pos0, self._track.describe(), recording.reports, recording.first_estimate,
        )
        self.recorded.emit(recording)
        return recording

    def _stop(self) -> _StreamThread | None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.stop.set()
            thread.join(timeout=3)
        return thread

    def _judge(self, thread: _StreamThread) -> tuple[Recording | None, str]:
        track = self._track
        if thread.first_at is None or not self._anchors:
            return None, "no audio"
        if thread.peak < MIN_PEAK:
            return None, "too quiet"
        pos0 = self._start_of(thread.first_at)
        if pos0 is None:
            return None, "no audio"
        reports = sum(a.playing for a in self._anchors)
        nearest = min(self._anchors, key=lambda a: abs(a.at - thread.first_at))
        first = nearest.position + (thread.first_at - nearest.at)
        seconds = thread.bytes / (RATE * CHANNELS * 2)
        if pos0 > START_LIMIT:
            return None, f"started late ({pos0:.1f}s)"
        if track.duration > 0 and (seconds < MIN_COVERAGE * track.duration or pos0 + seconds < track.duration - END_SLACK):
            return None, f"incomplete ({seconds:.0f}s of {track.duration:.0f}s)"
        # No clamp at 0: a recording that began before the song's audio has a negative start,
        # and moving it to 0 would push every aligned time late.
        return Recording(track, thread.path, pos0, seconds, first_estimate=first, reports=reports), ""


def _delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete recording %s: %s", path.name, exc)


def _open_loopback(app_id: str):
    from lyricfloat.align.capture import LoopbackStream
    from lyricfloat.align.processes import player_pid

    pid = player_pid(app_id)
    if pid is None:
        raise OSError(f"no process for {app_id}")
    return LoopbackStream(pid, RATE, CHANNELS)
