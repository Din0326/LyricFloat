"""Application wiring: MediaWatcher → LyricService → SyncDriver → LyricWindow.

Modules talk only through Qt signals (spec: system architecture). The
controller also owns the user settings: it applies them to the window,
pipeline and sources, and saves what the user changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QObject, QRect, QTimer
from PyQt6.QtWidgets import QApplication, QDialog

from lyricfloat import autostart, shortcuts
from lyricfloat.aio import spawn
from lyricfloat.diagnostics.calibration import MARKS_PER_SONG, CalibrationRecorder
from lyricfloat.diagnostics.history import append_play
from lyricfloat.diagnostics.problem_report import (
    add_to_test_list,
    build_report,
    open_folder,
    reports_dir,
    reveal,
    write_report,
)
from lyricfloat.lyrics.cache import EDITED_SOURCE, SqliteCache, default_cache_dir
from lyricfloat.lyrics.chinese import load_vocabulary
from lyricfloat.lyrics.language import detect_from_lyrics, detect_from_metadata
from lyricfloat.lyrics.lrc import first_sung_line
from lyricfloat.lyrics.normalize import normalize_artist, normalize_title
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.lyrics.sources.lrclib import LrclibSource
from lyricfloat.lyrics.sources.netease import NeteaseSource
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, PlaybackAnchor, ResultKind, TrackInfo
from lyricfloat.settings import FONT_PX_RANGE, Settings, SettingsStore, config_dir
from lyricfloat.sync.driver import SyncDriver
from lyricfloat.ui.hotkey import GlobalHotkey
from lyricfloat.ui.toolbar import format_offset
from lyricfloat.ui.report_dialog import ReportDialog
from lyricfloat.align.coordinator import L3Coordinator
from lyricfloat.align.recorder import SongRecorder, cleanup_temp
from lyricfloat.align.runner import WORKER, AlignRunner, source_runtime
from lyricfloat.align.runtime import installed_python, worker_for
from lyricfloat.ui.editor_dialog import EditorDialog
from lyricfloat.ui.search_dialog import SearchDialog
from lyricfloat.ui.lyric_window import (
    INSTRUMENTAL_TEXT,
    NOT_FOUND_TEXT,
    PLAIN_ONLY_TEXT,
    LyricWindow,
    style_from_settings,
)
from lyricfloat.ui.settings_dialog import SettingsDialog
from lyricfloat.ui.tray import TrayController

log = logging.getLogger(__name__)

OFFSET_STEP = 0.1  # seconds per click (FR-07)
OFFSET_LIMIT = 30.0
CUE_GUARD = 1.0  # seconds: a second cue this soon is a double press, not a correction (SPEC v1.29)
CUE_HINT_GAP = 10.0  # seconds: a version further off nobody listens to shows the cue key (SPEC v1.39)
CUE_MARGIN = 3.0  # seconds either side of where the first line can start, given the length gap (SPEC v1.30)
MIN_SHIFT = 2.0  # seconds: a found start this close to the source times leaves them for L3 (SPEC v1.80; v1.74: 0.5)
IDLE_HIDE_MS = 5000  # nothing playing this long, the lyrics fade out (SPEC v1.45)
SHORTCUT_ID_BASE = 0xB100  # RegisterHotKey ids for FR-14; calibration uses 0xB10C
FONT_STEP = 2  # px per click
SOURCE_FACTORIES = {"lrclib": LrclibSource, "netease": NeteaseSource}


def spread_evenly(lines: Sequence[LyricLine], duration: float) -> tuple[LyricLine, ...]:
    """Plain lyrics' static scroll (SPEC v1.6): lines evenly spaced over the song."""
    count = len(lines)
    return tuple(LyricLine(duration * i / count, line.text) for i, line in enumerate(lines))


class AppController(QObject):
    def __init__(
        self,
        service: LyricService,
        driver: SyncDriver,
        window: LyricWindow,
        tray: TrayController,
        pipeline: OpenCCPipeline | None = None,
        store: SettingsStore | None = None,
        settings: Settings | None = None,
        vocabulary_path: Path | None = None,
        l3: L3Coordinator | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.l3 = l3  # FR-21; None in tests and the demo
        if l3 is not None:  # SPEC v1.36: the start finder's news
            l3.startFound.connect(self.on_start_found)
            l3.startSearching.connect(self.on_start_searching)
            l3.startMissed.connect(self.on_start_missed)
        self.driver = driver
        self.window = window
        self.tray = tray
        self.pipeline = pipeline
        self.store = store
        self.settings = settings or Settings()
        self.vocabulary_path = vocabulary_path or config_dir() / "vocabulary.txt"
        self.current: TrackInfo | None = None
        self.watcher = None
        self.obs: LyricWindow | None = None  # FR-18
        self._synced = False  # offsets apply to synced lyrics only, not to evenly spread plain lyrics
        # SPEC v1.29: lines of a version of another length (an MV), which the user cues; None otherwise.
        self._cue_lines: tuple[LyricLine, ...] | None = None
        self._first_sung = 0
        self._cue_gap = 0.0  # player length minus the version's: how far the first line can move
        self._cued = False  # cued during this play
        self._cue_for: tuple[tuple[str, str, str], float] | None = None  # (song, offset) of this play's cue
        self._shift_for: tuple[tuple[str, str, str], float] | None = None  # SPEC v1.74: a found start, this play only
        self._listening = False  # the start finder listens to the current song (SPEC v1.36)
        self._last_cue = float("-inf")
        self.last_result: LyricResult | None = None  # latest result for the current song (FR-25)
        self._dialog: SettingsDialog | None = None
        self._report_dialog: ReportDialog | None = None
        self._search_dialog: SearchDialog | None = None
        self._editor: EditorDialog | None = None
        self.reports_dir: Path | None = None  # None: the default folder; tests point it elsewhere
        self.test_list: Path | None = None
        self.history_path: Path | None = None
        self._history_written = False
        self.calibration_path: Path | None = None
        self.calibration: CalibrationRecorder | None = None  # set while calibration mode is on
        self.hotkey: GlobalHotkey | None = None
        self.shortcuts: dict[str, GlobalHotkey] = {}  # FR-14, registered ones only
        self._idle_timer = QTimer(self, singleShot=True, interval=IDLE_HIDE_MS)
        self._idle_timer.timeout.connect(lambda: self.window.set_idle(True))

        service.resultReady.connect(self.on_result)
        service.resultChecked.connect(self.on_checked)
        driver.lineChanged.connect(window.show_line)
        window.set_clock(self.lyric_position)
        window.mediaControlRequested.connect(self.on_media_control)
        window.offsetStepRequested.connect(self.on_offset_step)
        window.fontStepRequested.connect(self.on_font_step)
        window.lockRequested.connect(lambda: tray.lock_action.setChecked(True))
        window.hideRequested.connect(lambda: tray.show_action.setChecked(False))
        window.settingsRequested.connect(self.open_settings)
        window.geometryCommitted.connect(self.on_geometry)
        window.revertAlignmentRequested.connect(self.revert_alignment)
        window.cueRequested.connect(self.cue_line)
        tray.settingsRequested.connect(self.open_settings)
        tray.searchRequested.connect(self.open_search)
        tray.editRequested.connect(self.open_editor)
        tray.obsToggled.connect(self.set_obs)
        tray.reportRequested.connect(self.report_problem)
        tray.calibrationToggled.connect(self.set_calibration)
        tray.reportsFolderRequested.connect(lambda: open_folder(self.reports_dir or reports_dir()))
        tray.lockChanged.connect(lambda locked: self._update(locked=locked))
        tray.visibilityChanged.connect(lambda visible: self._update(visible=visible))

    def display(self, track: TrackInfo) -> TrackInfo:
        """Metadata as shown on screen (simplified converted); queries keep the raw values."""
        return self.pipeline.display_track(track) if self.pipeline else track

    def attach_watcher(self, watcher) -> None:
        self.watcher = watcher
        watcher.trackChanged.connect(self.on_track)
        watcher.anchorChanged.connect(self.driver.on_anchor)
        watcher.anchorChanged.connect(lambda anchor: self.window.set_media_state(True, anchor.playing))
        watcher.anchorChanged.connect(lambda anchor: self.on_playing(anchor.playing))
        watcher.sessionChanged.connect(self.tray.set_session)
        # SPEC v1.49: nothing followed, but the toolbar can still start a paused player
        watcher.standbyChanged.connect(lambda available: self.window.set_media_state(available, False))
        if self.l3 is not None:
            watcher.anchorChanged.connect(self.l3.on_anchor)
            watcher.sessionChanged.connect(self.l3.on_session)
        watcher.set_pinned(self.settings.pinned_player)
        self.tray.set_pinned(self.settings.pinned_player)
        self.tray.set_players_provider(watcher.players)
        self.tray.pinChanged.connect(self.on_pin)

    def on_pin(self, app_id: str) -> None:
        """FR-24: follow only this player ("" = back to the Windows current session)."""
        self._update(pinned_player=app_id)
        if self.watcher is not None:
            self.watcher.set_pinned(app_id)

    # -- startup -------------------------------------------------------------

    def restore_state(self) -> None:
        s = self.settings
        self.window.apply_style(style_from_settings(s))
        self.window.set_show_translation(s.show_translation)
        self.window.restore_geometry(s.vertical_rect if s.vertical else s.window_rect)
        self.tray.lock_action.setChecked(s.locked)
        self.tray.show_action.setChecked(s.visible)
        self.window.setVisible(s.visible)
        self.tray.obs_action.setChecked(s.obs_window)

    # -- OBS window (FR-18) ----------------------------------------------------

    def set_obs(self, on: bool) -> None:
        if on:
            if self.obs is None:
                self.obs = LyricWindow(style_from_settings(self.settings), obs=True)
                self.obs.restore_geometry(self.settings.obs_rect)
                self.obs.closed.connect(lambda: self.tray.obs_action.setChecked(False))
                self.obs.geometryCommitted.connect(
                    lambda r: self._update(obs_rect=[r.x(), r.y(), r.width(), r.height()])
                )
            self.window.attach_mirror(self.obs)
            self.obs.show()
        elif self.obs is not None:
            self.window.detach_mirror(self.obs)
            self.obs.hide()
        if on != self.settings.obs_window:
            self._update(obs_window=on)

    # -- playback ------------------------------------------------------------

    def on_track(self, track: TrackInfo | None) -> None:
        self._listening = False  # the old song's search ending is no news for the new one
        if track is None or self.current is None or track.key != self.current.key:
            self._cue_for = self._shift_for = None
        if self.l3 is not None:
            self.l3.on_track(track)  # first: the previous song's recording ends here
        self.tray.set_track(self.display(track) if track else None)
        self.window.set_aligned(False)
        self.driver.reset()  # the old song's anchor must not read as a seek on the new one
        self._synced = False
        self._cue_lines = None
        self.window.set_cue_available(False)
        self.last_result = None
        self._history_written = False
        self.window.set_offset_label(None)
        if track is None:
            self.current = None
            self.service.cancel()
            self.on_playing(None)  # the waiting text shows, then fades on its own
            self.window.show_waiting()
            self.window.set_media_state(False, False)
            return
        self.current = track
        self.window.show_loading(self.display(track))
        self.service.request(track)
        self._update_calibration_badge()

    def on_playing(self, playing: bool | None) -> None:
        """SPEC v1.45: after IDLE_HIDE_MS without playback (paused, stopped) the lyrics fade out,
        and they come back as soon as it plays again. None: the player is gone, which has its
        own waiting text."""
        if playing is False:
            if not self._idle_timer.isActive() and not self.window.idle:
                self._idle_timer.start()  # a paused player reporting again does not restart the wait
            return
        self._idle_timer.stop()
        self.window.set_idle(False)

    def on_result(self, result: LyricResult) -> None:
        if self.current is None or result.track.key != self.current.key:
            return  # stale; LyricService already guards this, keep a second fence here
        track = self.display(self.current)
        self.last_result = result
        self._record_play(result)
        self.tray.set_warning(result.kind is ResultKind.SOURCES_FAILED)
        kind, lyrics = result.kind, result.shown_lyrics()  # L3 times and words when aligned
        self.window.set_aligned(result.alignment is not None)
        if self.l3 is not None:
            self.l3.on_result(result)
        # Plain lyrics with an L3 alignment show as synced (SPEC v1.26).
        self._synced = kind in (ResultKind.SYNCED, ResultKind.PLAIN_ONLY) and lyrics is not None and lyrics.synced
        cueable = self._synced and kind is ResultKind.SYNCED and result.version_mismatch
        self._cue_lines = lyrics.lines if cueable else None
        self._first_sung = first_sung_line(lyrics.lines, track.title, track.artist) if cueable else 0
        self._cue_gap = result.track.duration - result.candidate.duration if cueable and result.candidate else 0.0
        self._cued = False
        if cueable and self._cue_for is not None and self._cue_for[0] == result.track.key and abs(result.offset) < 1e-9:
            # Shown again (translations, say) from a result made before this play's cue: keep the cue.
            self._cued = True
            result = replace(result, offset=self._cue_for[1])
        self.window.set_cue_available(cueable, self._cue_key())
        far = cueable and abs(self._cue_gap) > CUE_HINT_GAP and result.alignment is None and abs(result.offset) < 1e-9
        if far and not self._listening:
            self._flash_cue_hint("開唱時可按 {key} 對齊")  # nobody listens: the source times are a guess
        # SPEC v1.36, v1.79 (Din): a version of another length shows its own times at once, however
        # far off, while the start finder listens in the background.
        if self._synced:
            offset = result.offset
            shift = self._shift_for
            if shift is not None and shift[0] == result.track.key and abs(offset) < 1e-9 and result.alignment is None:
                offset = shift[1]  # shown again (translations, say): keep this play's found start
            self.window.set_lyrics(track, lyrics.lines, lyrics.translations, words=lyrics.words)
            self.driver.set_lines(lyrics.lines, offset)
            self.window.set_offset_label(offset)
            return
        self.driver.clear_lines()
        self.window.set_offset_label(None)
        if kind is ResultKind.PLAIN_ONLY and lyrics and lyrics.lines and self.current.duration > 0:
            lines = spread_evenly(lyrics.lines, self.current.duration)
            self.window.set_lyrics(track, lines, unsynced=True)
            self.driver.set_lines(lines)
        elif kind is ResultKind.PLAIN_ONLY:
            self.window.show_message(track.describe(), PLAIN_ONLY_TEXT)  # no duration to spread over
        elif kind is ResultKind.INSTRUMENTAL:
            self.window.show_message(INSTRUMENTAL_TEXT)
        elif kind in (ResultKind.SKIPPED_AD, ResultKind.NOT_A_SONG):
            self.window.clear()
        else:  # NOT_FOUND, SOURCES_FAILED
            self.window.show_message(track.describe(), NOT_FOUND_TEXT)

    def on_checked(self, result: LyricResult) -> None:
        """Background pass finished with nothing to redraw: keep its state for reports."""
        if self.current is not None and result.track.key == self.current.key:
            self.last_result = result

    def _language_of(self, result: LyricResult | None, track: TrackInfo) -> str:
        lines = result.lyrics.lines if result and result.lyrics else ()
        return (detect_from_lyrics(line.text for line in lines) or detect_from_metadata(track.title, track.artist)).value

    def _record_play(self, result: LyricResult) -> None:
        if self._history_written or self.current is None:
            return
        self._history_written = True
        try:
            append_play(self.current, result, self._language_of(result, self.current), self.history_path)
        except OSError:
            log.exception("play history write failed")

    def adjust_offset(self, steps: int) -> float | None:
        """Nudge the current song's offset by `steps` x 0.1 s and remember it. Returns the new offset."""
        if self.current is None or not self._synced:
            return None  # offsets only make sense for synced lyrics
        offset = round(self.driver.offset + steps * OFFSET_STEP, 3)  # a cue's offset is not a whole step
        # A cued MV may start singing minutes after the source's first line (SPEC v1.29).
        limit = max(OFFSET_LIMIT, self.current.duration) if self._cue_lines is not None else OFFSET_LIMIT
        offset = max(-limit, min(limit, offset))
        self.driver.set_offset(offset)
        self.service.save_offset(self.current, offset)
        log.info("offset %+.1fs for %s", offset, self.display(self.current).describe())
        return offset

    def _cue_key(self) -> str:
        """The cue shortcut as registered, or "" when there is none."""
        return self.settings.hotkeys.get("cue_line", "") if "cue_line" in self.shortcuts else ""

    def on_start_found(self, track: TrackInfo, offset: float) -> None:
        """SPEC v1.36: the start finder heard the singing start; as if the user had cued it.
        SPEC v1.80 (Din): a start this close to the source times moves nothing, and the
        whole-song L3 after the play times the song."""
        if self.current is None or track.key != self.current.key:
            return
        if abs(offset) <= MIN_SHIFT:
            log.info("start found: %+.2fs off, left for L3", offset)
            if self._cue_lines is not None and not self._cued and self.l3 is not None:
                self.l3.on_cue(offset)  # an MV's whole-song timing still leaves out the intro before it
            return
        if self._cue_lines is None:
            self._shift(offset)
            return
        if self._cued:
            return
        self._apply_cue(offset)
        self.window.flash("已自動對齊開唱")
        log.info("start found: offset %+.2fs for %s", offset, self.display(self.current).describe())

    def _shift(self, offset: float) -> None:
        """SPEC v1.74: a song of its own length heard starting off its source times. This play
        only: a saved offset is the user's, and would keep L3 from timing the song next time."""
        if not self._synced or self._shift_for is not None or abs(self.driver.offset) > 1e-9:
            return  # not shown in time, already shifted, or the user moved it meanwhile
        self._shift_for = (self.current.key, offset)
        self.driver.set_offset(offset)
        self.window.set_offset_label(offset)
        self.window.flash("已自動對齊開唱")
        log.info("start found: shifted %+.2fs for this play of %s", offset, self.display(self.current).describe())
        if self.l3 is not None:
            self.l3.on_cue(offset)

    def on_start_searching(self, track: TrackInfo, on: bool) -> None:
        if self.current is None or track.key != self.current.key:
            return
        self._listening = on
        shown = self.last_result
        if shown is not None and shown.track.key == track.key and not shown.version_mismatch:
            return  # SPEC v1.74: a song of its own length is listened to quietly
        self.window.set_cue_hint("自動對齊開唱中…" if on else None)

    def on_start_missed(self, track: TrackInfo) -> None:
        if self.current is not None and track.key == self.current.key and not self._cued and self._cue_lines is not None:
            self._flash_cue_hint("沒聽到開唱點，可按 {key} 對齊")

    def _flash_cue_hint(self, text: str) -> None:
        key = self._cue_key()
        self.window.flash(text.format(key=key) if key else text.replace("按 {key} ", "按工具列「對齊開唱」"), 5000)

    def _apply_cue(self, offset: float) -> None:
        self._cued = True
        self._cue_for = (self.current.key, offset)
        self.driver.set_offset(offset)
        self.window.set_offset_label(offset)
        self.service.save_offset(self.current, offset)
        if self.last_result is not None:
            self.last_result = replace(self.last_result, offset=offset)
        if self.l3 is not None:
            self.l3.on_cue(offset)

    def cue_line(self) -> None:
        """SPEC v1.29: the line coming up starts now, for a version of another length (an MV).
        Lyrics showing the source times may run ahead (SPEC v1.30, v1.36), so a play's first
        cue made where the first line can start cues that line; later ones catch up a line."""
        if self.current is None or self._cue_lines is None:
            self.window.flash("這首不需要對齊開唱")
            return
        position = self.driver.engine.position()
        if position is None:
            self.window.flash("還沒收到播放進度")
            return
        now = time.monotonic()
        if now - self._last_cue < CUE_GUARD:
            return
        self._last_cue = now
        lines = self._cue_lines
        lag = position - lines[self._first_sung].start  # the offset if this cue is the first line's
        near_start = min(0.0, self._cue_gap) - CUE_MARGIN <= lag <= max(0.0, self._cue_gap) + CUE_MARGIN
        if not self._cued and near_start:
            target = self._first_sung
        else:
            target = max(self.driver.engine.index + 1, self._first_sung)
        if target >= len(lines):
            self.window.flash("已經是最後一句")
            return
        offset = round(position - lines[target].start, 3)
        self._apply_cue(offset)
        self.window.flash("已對齊開唱" if target == self._first_sung else "已對齊這一句")
        log.info("cued line %d at %.2fs: offset %+.2fs for %s", target, position, offset, self.display(self.current).describe())

    def lyric_position(self) -> float | None:
        """Playback position in the lyrics' own time (the user offset taken out)."""
        position = self.driver.engine.position()
        return None if position is None else position - self.driver.offset

    def revert_alignment(self) -> None:
        """FR-22: the toolbar's 'automatically corrected' mark puts the source times back."""
        if self.current is None or self.last_result is None or self.last_result.alignment is None:
            return
        spawn(self.service.revert_alignment(self.current))
        self.window.flash("已還原原始時間軸")

    def on_media_control(self, action: str) -> None:
        """FR-15: toolbar previous / play-pause / next go to the followed player."""
        if self.watcher is not None:
            spawn(self.watcher.control(action))

    def on_offset_step(self, steps: int) -> None:
        offset = self.adjust_offset(steps)
        if offset is not None:
            self.window.set_offset_label(offset)

    # -- global shortcuts (FR-14) ---------------------------------------------

    def apply_shortcuts(self) -> None:
        """(Re)register the configured shortcuts; tell the user about any already taken."""
        self.release_shortcuts()
        taken = []
        for i, (action, text) in enumerate(self.settings.hotkeys.items()):
            parsed = shortcuts.parse(text) if text and action in shortcuts.ACTIONS else None
            if parsed is None:
                continue
            modifiers, vk = parsed
            hotkey = GlobalHotkey(vk, modifiers, SHORTCUT_ID_BASE + i, repeat=action.startswith("offset_"))
            hotkey.activated.connect(lambda a=action: self.on_shortcut(a))
            if hotkey.register():
                self.shortcuts[action] = hotkey
            else:
                taken.append(f"{text}（{shortcuts.ACTIONS[action]}）")
        if taken:
            self.tray.notify("快捷鍵已被占用", "已被其他程式使用，請到設定的「快捷鍵」分頁改用其他組合：\n" + "\n".join(taken))

    def release_shortcuts(self) -> None:
        for hotkey in self.shortcuts.values():
            hotkey.unregister()
        self.shortcuts = {}

    def on_shortcut(self, action: str) -> None:
        if action == "toggle_visible":
            self.tray.show_action.toggle()
        elif action == "toggle_lock":
            self.tray.lock_action.toggle()
            self.window.flash("已鎖定" if self.tray.lock_action.isChecked() else "已解鎖")
        elif action == "cue_line":
            self.cue_line()
        elif action in ("offset_earlier", "offset_later"):
            offset = self.adjust_offset(-1 if action == "offset_earlier" else 1)
            if offset is None:
                self.window.flash("沒有同步歌詞，無法調整偏移")
            else:
                self.window.set_offset_label(offset)
                self.window.flash(f"偏移 {format_offset(offset)}")

    # -- calibration mode (timeline error, SPEC v1.6) ----------------------------

    def set_calibration(self, on: bool) -> None:
        if not on:
            if self.hotkey is not None:
                self.hotkey.unregister()
            self.calibration = None
            self.window.set_status_badge(None)
            return
        if self.hotkey is None:
            self.hotkey = GlobalHotkey()
            self.hotkey.activated.connect(self.on_calibration_mark)
        if not self.hotkey.register():
            self.tray.notify("校對模式", f"快捷鍵 {self.hotkey.label()} 已被其他程式使用，無法開啟校對模式。")
            self.tray.calibration_action.setChecked(False)
            return
        self.calibration = CalibrationRecorder(self.calibration_path)
        self.tray.notify(
            "校對模式已開啟",
            f"聽到每句實際開唱的瞬間按 {self.hotkey.label()}，每首記 {MARKS_PER_SONG} 行。",
        )
        self._update_calibration_badge()

    def _update_calibration_badge(self) -> None:
        if self.calibration is None:
            return
        if self.current is None:
            self.window.set_status_badge("校對中")
            return
        shown = self.display(self.current)
        self.window.set_status_badge(f"校對 {self.calibration.count(shown.title, shown.artist)}/{MARKS_PER_SONG}")

    def on_calibration_mark(self) -> None:
        if self.calibration is None or self.current is None or not self._synced or self.last_result is None:
            log.info("calibration mark ignored: no synced lyrics on screen")
            return
        position = self.driver.engine.position()
        if position is None:
            return
        shown = self.display(self.current)
        cand = self.last_result.candidate
        mark = self.calibration.record(
            shown.title,
            shown.artist,
            cand.source if cand else "",
            cand.source_id if cand else "",
            self.driver.engine.starts,
            self.driver.offset,
            position,
        )
        if mark is None:
            if self.calibration.count(shown.title, shown.artist) >= MARKS_PER_SONG:
                self.tray.notify("校對模式", f"這首已標滿 {MARKS_PER_SONG} 行，請換下一首。")
            else:
                log.info("calibration mark ignored: no line within 3 s of %.2fs", position)
        else:
            log.info("calibration mark: line %d, %+.0f ms", mark.line_index, mark.delta_ms)
        self._update_calibration_badge()

    # -- problem report (FR-25) -------------------------------------------------

    def report_problem(self) -> None:
        if self.current is None:
            self.tray.notify("這首歌詞有誤", "目前沒有播放中的歌曲。")
            return
        if self._report_dialog is not None:
            self._report_dialog.raise_()
            self._report_dialog.activateWindow()
            return
        track = self.current
        dialog = ReportDialog(self.display(track).describe())

        def finished(code: int) -> None:
            self._report_dialog = None
            if code == QDialog.DialogCode.Accepted:
                path = self.export_report(track, dialog.note_text(), dialog.add_to_list.isChecked())
                if path is not None and dialog.reveal.isChecked():
                    reveal(path)  # tray balloons are easy to miss (Windows may suppress them)

        dialog.finished.connect(finished)
        self._report_dialog = dialog
        dialog.show()

    # -- manual search and binding (FR-12) ----------------------------------------

    def open_search(self) -> None:
        if self._search_dialog is not None:
            self._search_dialog.raise_()
            self._search_dialog.activateWindow()
            return
        track = self.current
        if track is None:
            self.tray.notify("搜尋歌詞", "目前沒有播放中的歌曲。")
            return
        result = self.last_result if self.last_result and self.last_result.track.key == track.key else None
        # A video's pre-filled with the song its lyrics were found under (SPEC v1.76), else with
        # its whole title and no artist, searched as free text (SPEC v1.77).
        shown = self.display(LyricResolver.searchable(track))
        terms = self.display(LyricResolver.search_terms(track, result))
        dialog = SearchDialog(ServiceSearchBackend(self.service, track), track, shown, result, terms=terms)
        dialog.finished.connect(lambda _code: setattr(self, "_search_dialog", None))
        self._search_dialog = dialog
        dialog.show()

    # -- lyric editor (FR-17) -------------------------------------------------------

    def open_editor(self) -> None:
        if self._editor is not None:
            self._editor.raise_()
            self._editor.activateWindow()
            return
        track = self.current
        if track is None:
            self.tray.notify("編輯歌詞", "目前沒有播放中的歌曲。")
            return
        result = self.last_result if self.last_result and self.last_result.track.key == track.key else None
        lines: list[LyricLine] = []
        translations = None
        shown = result.shown_lyrics() if result is not None else None
        untimed = False
        if shown is not None and shown.lines:
            if shown.synced:
                # Edit where lines show now: the offset (and any L3 timing) goes into the times.
                lines = [LyricLine(line.start + result.offset, line.text) for line in shown.lines]
                translations = shown.translations
            elif track.duration > 0:
                lines, untimed = list(spread_evenly(result.lyrics.lines, track.duration)), True
            else:
                lines, untimed = list(result.lyrics.lines), True
        edited = bool(result and result.candidate and result.candidate.source == EDITED_SOURCE)
        backend = ServiceEditorBackend(self.service, self.driver, track, translations is not None)
        dialog = EditorDialog(backend, self.display(track), lines, translations, edited, untimed=untimed)
        dialog.finished.connect(lambda _code: setattr(self, "_editor", None))
        self._editor = dialog
        dialog.show()

    def export_report(self, track: TrackInfo, note: str = "", add_to_list: bool = True) -> Path | None:
        result = self.last_result if self.last_result and self.last_result.track.key == track.key else None
        engine = self.driver.engine
        report = build_report(
            track,
            self.display(track),
            result,
            self.service.resolver.candidates_for(track),
            engine.position(),
            engine.index,
            getattr(self.pipeline, "cache_key", None),
            note,
        )
        try:
            path = write_report(report, self.reports_dir)
        except OSError as exc:
            log.exception("problem report failed")
            self.tray.notify("這首歌詞有誤", f"匯出失敗：{exc}")
            return None
        added = False
        if add_to_list:
            try:
                added = add_to_test_list(track, self._language_of(result, track), note, self.test_list)
            except OSError:
                log.exception("adding to the test list failed")
        log.info("problem report saved: %s (test list: %s)", path, added)
        suffix = "，並已加入測試歌單" if added else ("（測試歌單已有這首）" if add_to_list else "")
        self.tray.notify("已匯出診斷資料", f"{path.name}{suffix}\n位置：{path.parent}")
        return path

    # -- settings ------------------------------------------------------------

    def _save(self) -> None:
        if self.store is not None:
            self.store.save(self.settings)

    def _update(self, **changes) -> None:
        """Small state changes (window position, lock, visibility): save, nothing to re-apply."""
        self.settings = replace(self.settings, **changes)
        self._save()

    def on_geometry(self, rect: QRect) -> None:
        # The window's own direction, not self.settings: a previewed turn is not saved yet.
        key = "vertical_rect" if self.window.style_.vertical else "window_rect"
        self._update(**{key: [rect.x(), rect.y(), rect.width(), rect.height()]})

    def on_font_step(self, steps: int) -> None:
        px = min(max(self.settings.font_px + steps * FONT_STEP, FONT_PX_RANGE[0]), FONT_PX_RANGE[1])
        if px != self.settings.font_px:
            self.apply_settings(replace(self.settings, font_px=px))

    def apply_settings(self, new: Settings, live_only: bool = False) -> None:
        """Apply settings. `live_only` previews appearance without saving or side effects."""
        turned = new.vertical != self.window.style_.vertical
        self.window.apply_style(style_from_settings(new))
        if turned:  # each direction keeps its own window position and size
            self.window.restore_geometry(new.vertical_rect if new.vertical else new.window_rect)
        self.window.set_show_translation(new.show_translation)
        if live_only:
            return
        old, self.settings = self.settings, new
        requery = False
        if self.pipeline is not None and new.conversion_mode != self.pipeline.mode:
            self.pipeline.mode = new.conversion_mode
            requery = True  # the cache rebuilds the current song from its source text
        if new.enabled_sources() != old.enabled_sources():
            self._rebuild_sources(new.enabled_sources())
            requery = True
        if new.l3_enabled != old.l3_enabled and self.l3 is not None:
            self.l3.set_enabled(new.l3_enabled)
        if new.autostart != old.autostart:
            try:
                autostart.set_enabled(new.autostart)
            except Exception:
                log.exception("autostart change failed")
                self.settings = replace(self.settings, autostart=old.autostart)
        self._save()
        if requery:
            self.requery()

    def reload_vocabulary(self) -> None:
        if self.pipeline is None:
            return
        vocabulary = load_vocabulary(self.vocabulary_path)
        if vocabulary != self.pipeline.vocabulary:
            self.pipeline.vocabulary = vocabulary
            log.info("vocabulary reloaded: %d entries", len(vocabulary))
            self.requery()

    def requery(self) -> None:
        if self.current is not None:
            self.service.request(self.current)

    def _rebuild_sources(self, names: list[str]) -> None:
        resolver = self.service.resolver
        existing = {s.name: s for s in resolver.sources}
        sources = [existing.pop(n) if n in existing else SOURCE_FACTORIES[n]() for n in names if n in SOURCE_FACTORIES]
        for removed in existing.values():
            spawn(removed.aclose())
        resolver.sources = sources
        resolver.forget_misses()
        log.info("sources now: %s", [s.name for s in sources])

    def open_settings(self) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        original = self.settings
        self.release_shortcuts()  # let the shortcuts tab record keys that are in use
        dialog = SettingsDialog(original, self.vocabulary_path, l3_status=self.l3.status_text() if self.l3 else "")
        dialog.previewChanged.connect(lambda s: self.apply_settings(s, live_only=True))

        def finished(code: int) -> None:
            if code == QDialog.DialogCode.Accepted:
                self.apply_settings(dialog.current())
                self.reload_vocabulary()
            else:
                self.apply_settings(original, live_only=True)  # undo the preview
            self.apply_shortcuts()
            self._dialog = None

        dialog.finished.connect(finished)
        self._dialog = dialog
        dialog.show()


class ServiceEditorBackend:
    """EditorDialog's view of the lyric service and the playback clock, fixed to one song."""

    def __init__(self, service: LyricService, driver: SyncDriver, track: TrackInfo, translations_known: bool) -> None:
        self.service = service
        self.driver = driver
        self.track = track
        self.translations_known = translations_known  # unknown: let the enrichment pass look them up

    def position(self) -> float | None:
        return self.driver.engine.position()

    async def save(self, lines, translations) -> None:
        await self.service.save_edit(self.track, lines, translations if self.translations_known else None)

    async def save_plain(self, texts) -> None:
        await self.service.save_edit_plain(self.track, texts)

    async def revert(self) -> None:
        await self.service.revert_edit(self.track)


class ServiceSearchBackend:
    """SearchDialog's view of the lyric service, fixed to one song."""

    def __init__(self, service: LyricService, track: TrackInfo) -> None:
        self.service = service
        self.track = track

    async def search(self, title: str, artist: str):
        return await self.service.resolver.search_manual(self.track, title, artist)

    async def preview(self, source: str, cand: Candidate):
        resolver = self.service.resolver
        filled = await resolver.fill(source, cand)
        return filled, resolver.preview(self.track, source, filled)

    async def bind(self, source: str, cand: Candidate, score: float | None) -> LyricResult:
        return await self.service.bind(self.track, source, cand, score)

    async def unbind(self) -> None:
        await self.service.unbind(self.track)


# Placeholder text only; never real lyrics.
DEMO_TRACK = TrackInfo(title="展示歌曲 Demo Song", artist="LyricFloat", album="Demo", duration=40.0, source_app="demo")
DEMO_LINES = tuple(
    LyricLine(start, text)
    for start, text in [
        (3.0, "這是展示用的第一句歌詞"),
        (6.0, "Demo line two, mixed 中英 text"),
        (9.0, "第三句：換句時下一句會往上推"),
        (12.0, "Line four is intentionally a bit longer to exercise the shrink-to-fit rule on narrow windows"),
        (16.0, ""),
        (18.0, "空白行之後的第五句"),
        (22.0, "最後一句，之後會循環播放"),
    ]
)


async def run_demo(controller: AppController, stop: asyncio.Event) -> None:
    """Feed fabricated lyrics on a simulated timeline that loops, to check the window."""
    controller.current = DEMO_TRACK  # bypass LyricService: the demo must not touch the network
    controller.tray.set_track(DEMO_TRACK)
    controller.on_result(LyricResult(DEMO_TRACK, ResultKind.SYNCED, lyrics=Lyrics(DEMO_LINES, synced=True)))
    while not stop.is_set():
        start = time.monotonic()
        controller.driver.on_anchor(PlaybackAnchor(0.0, start, True, DEMO_TRACK.duration))
        try:
            await asyncio.wait_for(stop.wait(), DEMO_TRACK.duration - 12)
        except TimeoutError:
            pass


async def run(app: QApplication, demo: bool = False) -> None:
    stop = asyncio.Event()
    # Quit from the tray only sets `stop`: the shutdown below awaits, so the event loop must still be
    # running; qasync ends it once run() returns. app.quit() would end Qt's loop first, and qasync
    # raised "Event loop stopped before Future completed". aboutToQuit stays as a best effort for
    # Windows ending the session, which kills the process right after.
    app.aboutToQuit.connect(stop.set)

    store = SettingsStore()
    settings = store.load()
    vocabulary_path = config_dir() / "vocabulary.txt"
    pipeline = OpenCCPipeline(settings.conversion_mode, load_vocabulary(vocabulary_path))

    window = LyricWindow(style_from_settings(settings))
    tray = TrayController(window, on_quit=stop.set)
    cache = SqliteCache(
        key=lambda t: (normalize_artist(pipeline.display_text(t.artist)), normalize_title(pipeline.display_text(t.title)))
    )
    sources = [SOURCE_FACTORIES[name]() for name in settings.enabled_sources() if name in SOURCE_FACTORIES]
    resolver = LyricResolver(sources, cache=cache, pipeline=pipeline)
    service = LyricService(resolver)
    driver = SyncDriver()
    l3 = None
    if not demo:
        temp_dir = default_cache_dir() / "tmp"
        removed = cleanup_temp(temp_dir) if temp_dir.exists() else 0
        if removed:
            log.info("deleted %d leftover recording(s)", removed)
        runtime_root = default_cache_dir() / "runtime"
        python = source_runtime() or installed_python(runtime_root)
        runner = AlignRunner(default_cache_dir() / "models", python, worker_for(python) if python else WORKER)
        l3 = L3Coordinator(service, SongRecorder(temp_dir), runner, lambda text: pipeline.cc.convert("t2s", text), runtime_root)
        l3.notice.connect(tray.notify)
        l3.set_enabled(settings.l3_enabled)
    controller = AppController(service, driver, window, tray, pipeline, store, settings, vocabulary_path, l3)
    controller.restore_state()
    controller.apply_shortcuts()

    watcher = None
    demo_task = None
    try:
        if demo:
            demo_task = asyncio.ensure_future(run_demo(controller, stop))
        else:
            from lyricfloat.media.watcher import MediaWatcher  # winrt import only when needed

            watcher = MediaWatcher()
            controller.attach_watcher(watcher)
            window.show_waiting()
            await watcher.start()
        await stop.wait()
    finally:
        log.info("shutting down")
        if demo_task is not None:
            demo_task.cancel()
        if watcher is not None:
            await watcher.stop()
        controller.release_shortcuts()
        if l3 is not None:
            l3.shutdown()
        await service.aclose()
        cache.close()
        tray.hide()
