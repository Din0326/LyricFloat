"""L3 coordinator (FR-21): decides which plays to record, queues the alignment, stores
the outcome. The controller only forwards playback events to it.

A play is recorded when L3 is on, the runtime is there, and the song has synced
lyrics that the user has not bound, edited or offset, and no alignment yet (or
only failed ones). Plain lyrics and versions of another length (an MV) are
recorded even when bound or cued. Recording starts within the song's first 15 s.

For a version of another length that nobody has cued yet, the start finder reads
the recording while it grows to find where the singing starts (SPEC v1.36); once
the start is known, the whole-song alignment leaves out the intro before it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from lyricfloat.aio import spawn
from lyricfloat.align.anchor import StartFinder, plan_search
from lyricfloat.align.apply import language_of
from lyricfloat.align.recorder import START_LIMIT, Recording, SongRecorder
from lyricfloat.align.runner import AlignJob, AlignOutcome, AlignRunner
from lyricfloat.align.runtime import RuntimeError_, RuntimeInstaller, is_frozen, worker_for
from lyricfloat.lyrics.cache import L3_FAILED
from lyricfloat.models import LyricLine, LyricResult, PlaybackAnchor, ResultKind, TrackInfo

log = logging.getLogger(__name__)
stats_log = logging.getLogger("lyricfloat.alignment")  # numbers only, never lyrics or titles

STATUS_READY = "推論環境已就緒。"
STATUS_SOURCE_MISSING = "這個 Python 環境沒有安裝推論套件：在專案資料夾執行 pip install -e \".[l2,l3]\" 後重開 LyricFloat。"
STATUS_DOWNLOAD = "開啟後會先在背景下載執行環境（約 250 MB，安裝後約 1 GB）。"
STATUS_INSTALLING = "正在下載並安裝執行環境…"
TRIM_LEAD = 5.0  # seconds kept before an MV's known singing start when aligning the whole song (SPEC v1.36)


class L3Coordinator(QObject):
    notice = pyqtSignal(str, str)  # title, text: for the tray
    startFound = pyqtSignal(object, float)  # track, offset of the source times (SPEC v1.36)
    startSearching = pyqtSignal(object, bool)  # track, whether the start finder is listening
    startMissed = pyqtSignal(object)  # track: the start went by unseen
    _installed = pyqtSignal(object, str)  # python path or None, error message (from the install thread)

    def __init__(
        self,
        service,
        recorder: SongRecorder,
        runner: AlignRunner,
        t2s: Callable[[str], str] = lambda text: text,
        runtime_root: Path | None = None,
        frozen: bool | None = None,
        installer: Callable[..., RuntimeInstaller] = RuntimeInstaller,
        finder: StartFinder | None = None,
        parent: QObject | None = None,
    ) -> None:
        """`runtime_root`: where the portable build installs its runtime (SPEC v1.21)."""
        super().__init__(parent)
        self.runtime_root = runtime_root
        self.frozen = is_frozen() if frozen is None else frozen
        self.installer = installer
        self.installing = False
        self._installed.connect(self._on_installed)
        self.service = service
        self.recorder = recorder
        self.runner = runner
        self.t2s = t2s
        self.enabled = False
        self._app_id = ""
        self._anchor: PlaybackAnchor | None = None
        self._lines: tuple[LyricLine, ...] = ()
        # The recorded song as a version of another length: where its first sung line is in
        # the source times, and the offset that puts it right (cued or found), for trimming.
        self._first_start: float | None = None
        self._offset = 0.0
        self.finder = finder or StartFinder(runner.models_dir, runner.python, runner.worker, parent=self)
        self.finder.found.connect(self.startFound)
        self.finder.searching.connect(self.startSearching)
        self.finder.missed.connect(self.startMissed)
        recorder.recorded.connect(self.on_recorded)
        recorder.dropped.connect(self._log_dropped)
        runner.finished.connect(self.on_aligned)

    @property
    def runtime_ready(self) -> bool:
        return self.runner.python is not None

    def status_text(self) -> str:
        if self.runtime_ready:
            return STATUS_READY
        if self.installing:
            return STATUS_INSTALLING
        return STATUS_DOWNLOAD if self.frozen and self.runtime_root is not None else STATUS_SOURCE_MISSING

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.finder.stop()
            self.recorder.cancel()
            self.runner.clear()
        elif not self.runtime_ready and self.frozen and self.runtime_root is not None:
            self.install_runtime()
        log.info("L3 %s (runtime %s)", "on" if on else "off", "ready" if self.runtime_ready else "missing")

    # -- portable runtime (SPEC v1.21) ----------------------------------------------

    def install_runtime(self) -> None:
        if self.installing:
            return
        self.installing = True
        self.notice.emit("自動時間軸校正", "正在背景下載執行環境（約 250 MB），完成後會通知你。")
        threading.Thread(target=self._install, name="l3-runtime", daemon=True).start()

    def _install(self) -> None:
        try:
            python = self.installer(self.runtime_root, lambda message: log.info("runtime: %s", message)).install()
        except (RuntimeError_, OSError, ValueError) as exc:
            self._installed.emit(None, str(exc))
            return
        except Exception as exc:  # anything else must not kill the thread silently
            log.exception("runtime install crashed")
            self._installed.emit(None, f"{type(exc).__name__}: {exc}")
            return
        self._installed.emit(python, "")

    def _on_installed(self, python, error: str) -> None:
        self.installing = False
        if python is None:
            log.warning("L3 runtime install failed: %s", error)
            self.notice.emit("自動時間軸校正", f"執行環境安裝失敗：{error}\n可以在設定關掉再開啟重試。")
            return
        self.runner.python = self.finder.python = python
        self.runner.worker = self.finder.worker = worker_for(python)
        self.notice.emit("自動時間軸校正", "執行環境已就緒，之後完整聽完的歌會在背景自動校正。")

    # -- playback events ------------------------------------------------------------

    def _log_dropped(self, reason: str) -> None:
        stats_log.info(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "dropped", "reason": reason}))

    def on_session(self, app_id: str) -> None:
        if app_id != self._app_id:
            if self.recorder.active:
                self._log_dropped("player switched")
            self.recorder.cancel()  # another player: its audio is not this song's
        self._app_id = app_id

    def on_anchor(self, anchor: PlaybackAnchor) -> None:
        self._anchor = anchor
        self.recorder.on_anchor(anchor)

    def on_track(self, track: TrackInfo | None) -> None:
        current = self.recorder.track
        if self.recorder.active and (track is None or current is None or track.key != current.key):
            self.finder.stop()
            self.recorder.finish()  # the song ended (or the player stopped): keep it if it counts

    def on_result(self, result: LyricResult) -> None:
        if self.recorder.active:
            if self.recorder.track is not None and result.track.key == self.recorder.track.key:
                if result.lyrics is None or result.lyrics.lines != self._lines:
                    log.info("L3 recording dropped: the lyrics changed mid-song")
                    self._log_dropped("lyrics changed")
                    self.finder.stop()
                    self.recorder.cancel()
            return
        if not self._eligible(result):
            return
        position = self._position()
        if position is None or position > START_LIMIT:
            return
        self._lines = result.lyrics.lines
        self.recorder.start(result.track, self._app_id, self._anchor)
        self._first_start, self._offset = None, result.offset
        # SPEC v1.36: a version of another length is listened to for its start; v1.74 (Din): every
        # synced song is, on a play L3 has not timed yet, so an intro of another length is caught at once.
        if result.lyrics.synced and result.candidate is not None:
            track = result.track
            search = plan_search(self._lines, track.title, track.artist, track.duration - result.candidate.duration, self.t2s)
            if search is not None:
                if result.version_mismatch:
                    self._first_start = search.lines[0].start  # its intro is left out of the whole-song timing
                if abs(result.offset) < 1e-9:  # nobody has cued it: listen for the start
                    self.finder.start(track, search, self.recorder.live)

    def on_cue(self, offset: float) -> None:
        """The song's start is known now (the user's cue or the finder's): stop listening."""
        self.finder.stop()
        self._offset = offset

    def _eligible(self, result: LyricResult) -> bool:
        lyrics = result.lyrics
        return (
            self.enabled
            and self.runtime_ready
            and bool(self._app_id)
            and result.kind in (ResultKind.SYNCED, ResultKind.PLAIN_ONLY)
            and lyrics is not None
            and bool(lyrics.lines)
            # The user's own timing wins: bound or edited synced lyrics are left alone, while
            # plain ones (bound, or an untimed edit) still get times (SPEC v1.26). A version of
            # another length is timed from the audio alone; its cue offset is only a stopgap (SPEC v1.29).
            and (result.version_mismatch or not (result.bound and lyrics.synced))
            and (result.version_mismatch or abs(result.offset) < 1e-9)
            and result.alignment is None
            and result.l3_state in (None, L3_FAILED)
            and "source_failed" not in result.notes  # not cached: nothing to keep the alignment with
        )

    def _position(self) -> float | None:
        a = self._anchor
        if a is None or not a.playing:
            return None
        return a.position + (time.monotonic() - a.at)

    # -- alignment ------------------------------------------------------------------

    def on_recorded(self, recording: Recording) -> None:
        lines = self._lines
        language = language_of(lines)
        texts = tuple(self.t2s(line.text) if language == "zh" else line.text for line in lines)  # SPEC: align in t2s
        skip = 0.0
        if self._first_start is not None and abs(self._offset) > 1e-9:
            # An MV whose singing start is known: its intro (scenes, talk) only confuses the aligner.
            skip = max(0.0, self._first_start + self._offset - TRIM_LEAD - recording.pos0)
        self.runner.submit(AlignJob(recording, lines, texts, language, skip))

    def on_aligned(self, outcome: AlignOutcome) -> None:
        spawn(self._store(outcome))

    async def _store(self, outcome: AlignOutcome) -> None:
        job = outcome.job
        rec = job.recording
        at = time.strftime("%Y-%m-%d %H:%M:%S")
        base = {"at": at, "language": job.language, "seconds": round(rec.seconds, 1), "wall_s": round(outcome.wall_s, 1),
                "pos0_s": round(rec.pos0, 3), "reports": rec.reports}
        if job.skip:
            base["skip_s"] = round(job.skip, 1)
        if rec.first_estimate is not None:
            base["start_fix_s"] = round(rec.pos0 - rec.first_estimate, 3)  # how far the old method would have been off
        base.update({k: round(v, 2) for k, v in outcome.timings.items()})
        if not outcome.ok:
            await self.service.alignment_failed(rec.track)
            stats_log.info(json.dumps({**base, "ok": False}))
            return
        judged = await self.service.store_alignment(rec.track, job.lines, outcome.segments, rec.pos0 + job.skip, rec.seconds - job.skip)
        if judged is not None:
            stats_log.info(json.dumps({**base, "ok": True, **judged.stats}))

    def shutdown(self) -> None:
        self.finder.stop()
        self.recorder.cancel()
        self.runner.shutdown()
