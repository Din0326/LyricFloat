"""AppController wiring. A fake tray avoids putting real icons in the taskbar, and
autostart is always monkeypatched: tests never touch the real Startup folder."""

import asyncio
from dataclasses import replace

import pytest
from PyQt6.QtCore import QObject, QRect, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication

from lyricfloat import app as app_module
from lyricfloat.app import AppController, spread_evenly
from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, PlaybackAnchor, ResultKind, TrackInfo
from lyricfloat.settings import Settings, SettingsStore, SourceSetting
from lyricfloat.sync.driver import SyncDriver
from lyricfloat.ui.lyric_window import NEXT, LyricWindow

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
LINES = (LyricLine(5.0, "first line"), LyricLine(10.0, "second line"), LyricLine(15.0, "third line"))


class FakeTray(QObject):
    settingsRequested = pyqtSignal()
    searchRequested = pyqtSignal()
    editRequested = pyqtSignal()
    reportRequested = pyqtSignal()
    reportsFolderRequested = pyqtSignal()
    calibrationToggled = pyqtSignal(bool)
    visibilityChanged = pyqtSignal(bool)
    lockChanged = pyqtSignal(bool)
    obsToggled = pyqtSignal(bool)
    pinChanged = pyqtSignal(str)

    def __init__(self, window):
        super().__init__()
        self.show_action = QAction("show", self, checkable=True, checked=True)
        self.lock_action = QAction("lock", self, checkable=True)
        self.calibration_action = QAction("calibration", self, checkable=True)
        self.obs_action = QAction("obs", self, checkable=True)
        self.obs_action.toggled.connect(self.obsToggled)
        self.calibration_action.toggled.connect(self.calibrationToggled)
        self.show_action.toggled.connect(lambda v: (window.setVisible(v), self.visibilityChanged.emit(v)))
        self.lock_action.toggled.connect(lambda v: (window.set_locked(v), self.lockChanged.emit(v)))
        self.warning = False
        self.messages = []

    def notify(self, title, text):
        self.messages.append((title, text))

    def set_track(self, track):
        pass

    def set_session(self, app_id):
        pass

    def set_warning(self, warning):
        self.warning = warning

    def set_pinned(self, app_id):
        self.pinned = app_id

    def set_players_provider(self, provider):
        self.players = provider


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def autostart_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.autostart, "set_enabled", lambda enabled: calls.append(enabled))
    return calls


@pytest.fixture
def ctl(qapp, tmp_path, autostart_calls):
    window = LyricWindow()
    tray = FakeTray(window)
    service = LyricService(LyricResolver([]))
    store = SettingsStore(tmp_path / "settings.json")
    pipeline = OpenCCPipeline(converter=ChineseConverter())
    controller = AppController(service, SyncDriver(), window, tray, pipeline, store, Settings(), tmp_path / "v.txt")
    controller.current = TRACK
    controller.reports_dir = tmp_path / "reports"
    controller.test_list = tmp_path / "test_list.csv"
    controller.history_path = tmp_path / "history.jsonl"
    controller.calibration_path = tmp_path / "calibration.jsonl"
    yield controller
    window.close()


def synced(offset=0.0, translations=None):
    return LyricResult(TRACK, ResultKind.SYNCED, Lyrics(LINES, True, translations), offset=offset)


def test_spread_evenly():
    lines = spread_evenly([LyricLine(0, "a"), LyricLine(0, "b"), LyricLine(0, "c"), LyricLine(0, "d")], 200.0)
    assert [l.start for l in lines] == [0.0, 50.0, 100.0, 150.0]
    assert [l.text for l in lines] == ["a", "b", "c", "d"]


def test_synced_result_shows_offset_and_lines(ctl):
    ctl.on_result(synced(offset=0.3))
    assert ctl.driver.has_lines and ctl.driver.offset == pytest.approx(0.3)
    assert ctl.window.toolbar.offset_label.text() == "+0.3s"


def test_offset_steps_update_driver_and_label(ctl):
    ctl.on_result(synced())
    ctl.window.offsetStepRequested.emit(1)
    ctl.window.offsetStepRequested.emit(1)
    ctl.window.offsetStepRequested.emit(-1)
    assert ctl.driver.offset == pytest.approx(0.1)
    assert ctl.window.toolbar.offset_label.text() == "+0.1s"


def test_plain_lyrics_scroll_evenly_and_ignore_offsets(ctl):
    plain = LyricResult(TRACK, ResultKind.PLAIN_ONLY, Lyrics(LINES, False))
    ctl.on_result(plain)
    assert ctl.window._unsynced
    assert ctl.driver.has_lines
    assert ctl.adjust_offset(1) is None
    assert not ctl.window.toolbar.later.isEnabled()


def test_plain_lyrics_without_duration_show_a_message(ctl):
    ctl.current = replace(TRACK, duration=0.0)
    ctl.on_result(LyricResult(ctl.current, ResultKind.PLAIN_ONLY, Lyrics(LINES, False)))
    assert not ctl.driver.has_lines
    assert ctl.window._text.secondary == "無同步歌詞"


def test_translation_update_reaches_the_window(ctl):
    ctl.on_result(synced())
    ctl.on_result(synced(translations=("一", None, "三")))
    ctl.window.show_line(0, animate=False)
    assert ctl.window._text.secondary == "一"


def test_sources_failed_raises_the_tray_warning(ctl):
    ctl.on_result(LyricResult(TRACK, ResultKind.SOURCES_FAILED))
    assert ctl.tray.warning


def test_font_step_saves_the_new_size(ctl):
    ctl.window.fontStepRequested.emit(1)
    assert ctl.settings.font_px == 34
    assert ctl.store.load().font_px == 34


def test_font_size_stays_in_range(ctl):
    ctl.settings = replace(ctl.settings, font_px=96)
    ctl.on_font_step(1)
    assert ctl.settings.font_px == 96


def test_live_preview_does_not_save(ctl):
    ctl.apply_settings(replace(ctl.settings, font_px=50), live_only=True)
    assert ctl.window.style_.main_px == 50
    assert ctl.settings.font_px == 32
    assert not ctl.store.path.exists()


def test_conversion_mode_change_reaches_the_pipeline(ctl):
    ctl.apply_settings(replace(ctl.settings, conversion_mode="s2tw"))
    assert ctl.pipeline.mode == "s2tw"
    assert ctl.store.load().conversion_mode == "s2tw"


def test_source_change_rebuilds_the_resolver(ctl):
    s = replace(ctl.settings, sources=[SourceSetting("lrclib"), SourceSetting("netease", enabled=False)])
    ctl.apply_settings(s)
    assert [src.name for src in ctl.service.resolver.sources] == ["lrclib"]
    ctl.apply_settings(replace(s, sources=[SourceSetting("lrclib"), SourceSetting("netease")]))
    assert [src.name for src in ctl.service.resolver.sources] == ["lrclib", "netease"]


def test_autostart_is_toggled_only_on_change(ctl, autostart_calls):
    ctl.apply_settings(replace(ctl.settings, font_px=30))
    assert autostart_calls == []
    ctl.apply_settings(replace(ctl.settings, autostart=True))
    assert autostart_calls == [True]


def test_failed_autostart_change_is_rolled_back(ctl, monkeypatch):
    def boom(enabled):
        raise OSError("blocked")

    monkeypatch.setattr(app_module.autostart, "set_enabled", boom)
    ctl.apply_settings(replace(ctl.settings, autostart=True))
    assert ctl.settings.autostart is False


def test_lock_from_toolbar_goes_through_the_tray_and_is_saved(ctl):
    ctl.window.lockRequested.emit()
    assert ctl.window.locked
    assert ctl.store.load().locked


def test_hide_from_toolbar_is_saved(ctl):
    ctl.window.hideRequested.emit()
    assert not ctl.window.isVisible()
    assert ctl.store.load().visible is False


def test_geometry_is_saved(ctl):
    ctl.on_geometry(QRect(10, 20, 800, 120))
    assert ctl.store.load().window_rect == [10, 20, 800, 120]


def test_restore_state_applies_saved_settings(ctl):
    ctl.settings = Settings(font_px=40, show_translation=False, window_rect=None, locked=True, visible=True)
    ctl.restore_state()
    assert ctl.window.style_.main_px == 40
    assert ctl.window.locked
    ctl.window.set_lyrics(TRACK, LINES, translations=("一", "二", "三"))
    ctl.window.show_line(0, animate=False)
    assert ctl.window._text.secondary_kind == NEXT  # translation switched off


# --- problem report (FR-25) ------------------------------------------------------


def test_export_report_writes_file_and_test_list(ctl):
    import json

    ctl.on_result(synced(offset=0.3))
    path = ctl.export_report(TRACK, note="早了一句", add_to_list=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.parent == ctl.reports_dir
    assert data["note"] == "早了一句"
    assert data["result"]["offset"] == 0.3
    assert len(data["lyrics_snapshot"]) == 3
    assert ctl.test_list.exists()
    assert "加入測試歌單" in ctl.tray.messages[-1][1]
    ctl.export_report(TRACK, add_to_list=True)
    assert "已有這首" in ctl.tray.messages[-1][1]


def test_export_without_lyrics_still_works(ctl):
    ctl.on_result(LyricResult(TRACK, ResultKind.NOT_FOUND))
    path = ctl.export_report(TRACK, add_to_list=False)
    assert path.exists()
    assert not ctl.test_list.exists()


def test_report_with_nothing_playing_just_tells_the_user(ctl):
    ctl.current = None
    ctl.report_problem()
    assert ctl.tray.messages[-1][1] == "目前沒有播放中的歌曲。"


def test_report_dialog_opens_once(ctl):
    ctl.report_problem()
    first = ctl._report_dialog
    ctl.report_problem()
    assert ctl._report_dialog is first
    first.reject()
    assert ctl._report_dialog is None


def test_search_for_a_found_video_starts_from_its_song_on_screen(ctl):
    # SPEC v1.76: the NetEase hit's simplified names, shown as on screen; the dialog names the video.
    video = TrackInfo("〈Know My Name〉合作演出：真傷樂團(官方MV)", "英雄聯盟 League of Legends", "", 180.0, "Chrome")
    hit = Candidate("netease", "1", "Know My Name", "True Damage / 英雄联盟", "", 180.0)
    ctl.current = video
    ctl.last_result = LyricResult(video, ResultKind.SYNCED, Lyrics(LINES, True), hit, 0.9)
    ctl.open_search()
    dialog = ctl._search_dialog
    assert (dialog.title.text(), dialog.artist.text()) == ("Know My Name", "True Damage / 英雄聯盟")
    assert "真傷樂團" in dialog.windowTitle()
    dialog.close()
    # SPEC v1.77: nothing found: the whole video title, no artist.
    ctl.last_result = LyricResult(video, ResultKind.NOT_FOUND)
    ctl.open_search()
    assert (ctl._search_dialog.title.text(), ctl._search_dialog.artist.text()) == (video.title, "")
    ctl._search_dialog.close()
    # Spotify's own title stays, whatever was picked.
    ctl.current = TRACK
    ctl.last_result = LyricResult(TRACK, ResultKind.SYNCED, Lyrics(LINES, True), replace(hit, title="Paper Lanterns (Live)"), 0.9)
    ctl.open_search()
    assert ctl._search_dialog.title.text() == "Paper Lanterns"
    ctl._search_dialog.close()


def test_accepting_the_report_dialog_reveals_the_file(ctl, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    revealed = []
    monkeypatch.setattr(app_module, "reveal", revealed.append)
    ctl.on_result(synced())
    ctl.report_problem()
    dialog = ctl._report_dialog
    dialog.reveal.setChecked(True)
    dialog.done(QDialog.DialogCode.Accepted)
    assert len(revealed) == 1 and revealed[0].suffix == ".json"


def test_report_without_reveal_does_not_open_explorer(ctl, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    revealed = []
    monkeypatch.setattr(app_module, "reveal", revealed.append)
    ctl.report_problem()
    dialog = ctl._report_dialog
    dialog.reveal.setChecked(False)
    dialog.done(QDialog.DialogCode.Accepted)
    assert revealed == []


def test_background_check_state_reaches_reports(ctl):
    first = synced()
    ctl.on_result(first)
    checked = replace(first, timeline_checked=True)
    ctl.service.resultChecked.emit(checked)
    assert ctl.last_result.timeline_checked


def test_play_history_gets_one_line_per_song(ctl):
    from lyricfloat.diagnostics.history import read_history

    ctl.on_result(synced())
    ctl.on_result(synced(translations=("一", None, "三")))  # background update, same song
    records = read_history(ctl.history_path)
    assert len(records) == 1
    assert records[0]["title"] == "Paper Lanterns" and records[0]["language"] == "en"
    assert records[0]["kind"] == "synced"
    ctl.on_track(TRACK)  # played again
    ctl.on_result(synced())
    assert len(read_history(ctl.history_path)) == 2


# --- calibration mode -------------------------------------------------------------


class FakeHotkey(QObject):
    activated = pyqtSignal()

    def __init__(self, ok=True):
        super().__init__()
        self.ok = ok
        self.registered = False

    def label(self):
        return "Ctrl+Alt+M"

    def register(self):
        self.registered = self.ok
        return self.ok

    def unregister(self):
        self.registered = False


def _calibrating(ctl, ok=True):
    from PyQt6.QtCore import QObject  # noqa: F401

    ctl.hotkey = FakeHotkey(ok)
    ctl.hotkey.activated.connect(ctl.on_calibration_mark)
    ctl.tray.calibration_action.setChecked(True)
    return ctl.hotkey


def _playing_at(ctl, position):
    from lyricfloat.models import PlaybackAnchor

    ctl.driver.on_anchor(PlaybackAnchor(position, 0.0, playing=False, duration=200.0))


def test_marks_record_the_gap_to_the_nearest_line(ctl):
    from lyricfloat.diagnostics.calibration import read_marks

    ctl.on_result(synced())
    hotkey = _calibrating(ctl)
    _playing_at(ctl, 10.4)  # line 2 is shown from 10.0
    hotkey.activated.emit()
    (mark,) = read_marks(ctl.calibration_path)
    assert mark["line_index"] == 1 and mark["delta_ms"] == pytest.approx(400)
    assert ctl.window._status_badge == "校對 1/5"


def test_marks_use_the_displayed_time_including_offset(ctl):
    from lyricfloat.diagnostics.calibration import read_marks

    ctl.on_result(synced(offset=0.5))  # line 2 now shown from 10.5
    hotkey = _calibrating(ctl)
    _playing_at(ctl, 10.4)
    hotkey.activated.emit()
    (mark,) = read_marks(ctl.calibration_path)
    assert mark["delta_ms"] == pytest.approx(-100) and mark["offset"] == 0.5


def test_five_marks_per_song_then_a_notice(ctl):
    ctl.on_result(synced())
    hotkey = _calibrating(ctl)
    _playing_at(ctl, 10.2)
    for _ in range(6):
        hotkey.activated.emit()
    assert ctl.calibration.count("Paper Lanterns", "The Example Band") == 5
    assert "標滿" in ctl.tray.messages[-1][1]


def test_marks_without_synced_lyrics_are_ignored(ctl):
    hotkey = _calibrating(ctl)
    ctl.on_result(LyricResult(TRACK, ResultKind.NOT_FOUND))
    hotkey.activated.emit()
    assert not ctl.calibration_path.exists()


def test_hotkey_in_use_turns_the_mode_back_off(ctl):
    _calibrating(ctl, ok=False)
    assert ctl.calibration is None
    assert not ctl.tray.calibration_action.isChecked()
    assert "已被其他程式使用" in ctl.tray.messages[-1][1]


def test_turning_calibration_off_releases_the_hotkey(ctl):
    hotkey = _calibrating(ctl)
    ctl.tray.calibration_action.setChecked(False)
    assert not hotkey.registered and ctl.calibration is None
    assert ctl.window._status_badge is None


class FakeWatcher(QObject):
    trackChanged = pyqtSignal(object)
    anchorChanged = pyqtSignal(object)
    sessionChanged = pyqtSignal(str)
    standbyChanged = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.sent = []
        self.pinned = ""

    def set_pinned(self, app_id):
        self.pinned = app_id

    def players(self):
        return ["Spotify.exe", "Chrome"]

    async def control(self, action):
        self.sent.append(action)
        return True


async def test_player_buttons_reach_the_watcher(ctl):
    watcher = FakeWatcher()
    ctl.attach_watcher(watcher)
    ctl.window.mediaControlRequested.emit("next")
    await asyncio.sleep(0)
    assert watcher.sent == ["next"]


def test_play_state_follows_the_anchor_and_session(ctl):
    watcher = FakeWatcher()
    ctl.attach_watcher(watcher)
    tb = ctl.window.toolbar
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, True))
    assert tb.play_pause.isEnabled() and tb.play_pause.toolTip() == "暫停"
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))
    assert tb.play_pause.toolTip() == "播放"
    ctl.on_track(None)
    assert not tb.next.isEnabled()
    watcher.standbyChanged.emit(True)  # SPEC v1.49: a paused player nobody follows can be started
    assert tb.play_pause.isEnabled() and tb.play_pause.toolTip() == "播放"
    watcher.standbyChanged.emit(False)
    assert not tb.play_pause.isEnabled()


def fire(timer):
    """A single-shot timer running out: it stops, then times out."""
    timer.stop()
    timer.timeout.emit()


def test_lyrics_fade_out_when_nothing_plays_and_come_back_with_playback(ctl):
    watcher = FakeWatcher()
    ctl.attach_watcher(watcher)
    ctl.on_result(synced())
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, True))
    assert not ctl.window.idle and not ctl._idle_timer.isActive()
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))  # paused: the wait begins
    assert ctl._idle_timer.isActive() and not ctl.window.idle
    remaining = ctl._idle_timer.remainingTime()
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))  # still paused, reported again
    assert ctl._idle_timer.remainingTime() <= remaining  # the wait is not restarted
    fire(ctl._idle_timer)
    assert ctl.window.idle and ctl.driver.has_lines  # faded, the lyrics kept
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))
    assert not ctl._idle_timer.isActive()  # already faded: nothing to wait for
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, True))
    assert not ctl.window.idle
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, True))  # resumed within the wait
    assert not ctl._idle_timer.isActive() and not ctl.window.idle


def test_a_closed_player_keeps_its_waiting_text(ctl):
    watcher = FakeWatcher()
    ctl.attach_watcher(watcher)
    watcher.anchorChanged.emit(PlaybackAnchor(1.0, 0.0, False))
    fire(ctl._idle_timer)
    ctl.on_track(None)  # "等待播放中…" shows, then fades as before (SPEC v1.45 leaves it alone)
    assert not ctl.window.idle and not ctl._idle_timer.isActive()
    assert ctl.window._text.primary == "等待播放中…"


def test_the_obs_window_fades_with_the_lyrics(ctl):
    obs = LyricWindow(obs=True)
    ctl.window.set_idle(True)
    ctl.window.attach_mirror(obs)
    assert obs.idle
    ctl.window.set_idle(False)
    assert not obs.idle
    obs.close()


def test_pin_is_restored_and_saved(ctl):
    ctl.settings = replace(ctl.settings, pinned_player="Chrome")
    watcher = FakeWatcher()
    ctl.attach_watcher(watcher)
    assert watcher.pinned == "Chrome" and ctl.tray.pinned == "Chrome"
    assert ctl.tray.players() == ["Spotify.exe", "Chrome"]
    ctl.tray.pinChanged.emit("Spotify.exe")
    assert watcher.pinned == "Spotify.exe"
    assert ctl.store.load().pinned_player == "Spotify.exe"
    ctl.tray.pinChanged.emit("")
    assert watcher.pinned == "" and ctl.store.load().pinned_player == ""


@pytest.fixture
def fake_hotkeys(monkeypatch):
    """No real RegisterHotKey in tests; `taken` lists shortcuts another program holds."""
    state = {"registered": [], "taken": set()}

    def register(self):
        if self.label() in state["taken"]:
            return False
        state["registered"].append((self.label(), self.hotkey_id, self.repeat))
        self.registered = True
        return True

    def unregister(self):
        if self.registered:
            state["registered"] = [r for r in state["registered"] if r[1] != self.hotkey_id]
            self.registered = False

    monkeypatch.setattr(app_module.GlobalHotkey, "register", register)
    monkeypatch.setattr(app_module.GlobalHotkey, "unregister", unregister)
    return state


def test_shortcuts_register_with_distinct_ids(ctl, fake_hotkeys):
    ctl.apply_shortcuts()
    got = fake_hotkeys["registered"]
    assert [label for label, _, _ in got] == ["Ctrl+Alt+H", "Ctrl+Alt+L", "Ctrl+Alt+Left", "Ctrl+Alt+Right", "Ctrl+Alt+Down"]
    assert len({hid for _, hid, _ in got}) == 5
    assert [repeat for _, _, repeat in got] == [False, False, True, True, False]  # offsets repeat while held
    ctl.apply_shortcuts()  # re-applying replaces, never stacks
    assert len(fake_hotkeys["registered"]) == 5


def test_taken_shortcut_is_reported_and_others_still_work(ctl, fake_hotkeys):
    fake_hotkeys["taken"].add("Ctrl+Alt+L")
    ctl.apply_shortcuts()
    assert set(ctl.shortcuts) == {"toggle_visible", "offset_earlier", "offset_later", "cue_line"}
    title, text = ctl.tray.messages[-1]
    assert "Ctrl+Alt+L" in text and "鎖定／解鎖歌詞" in text


def test_turned_off_shortcut_is_not_registered(ctl, fake_hotkeys):
    ctl.settings = replace(ctl.settings, hotkeys={**ctl.settings.hotkeys, "toggle_lock": ""})
    ctl.apply_shortcuts()
    assert "toggle_lock" not in ctl.shortcuts


def test_shortcut_actions(ctl):
    ctl.on_shortcut("toggle_lock")
    assert ctl.tray.lock_action.isChecked() and ctl.window._flash == "已鎖定"
    ctl.on_shortcut("toggle_visible")
    assert not ctl.tray.show_action.isChecked()
    ctl.on_shortcut("offset_later")
    assert ctl.window._flash == "沒有同步歌詞，無法調整偏移"
    ctl.on_result(synced())
    ctl.on_shortcut("offset_later")
    ctl.on_shortcut("offset_later")
    ctl.on_shortcut("offset_earlier")
    assert ctl.driver.offset == pytest.approx(0.1)
    assert ctl.window._flash == "偏移 +0.1s"


def test_settings_dialog_suspends_shortcuts(ctl, fake_hotkeys):
    ctl.apply_shortcuts()
    ctl.open_settings()
    assert fake_hotkeys["registered"] == []  # the shortcuts tab can record them
    ctl._dialog.reject()
    assert len(fake_hotkeys["registered"]) == 5


def test_karaoke_clock_takes_the_offset_out(ctl):
    ctl.on_result(synced(offset=0.5))
    ctl.driver.on_anchor(PlaybackAnchor(12.0, __import__("time").monotonic(), False))
    assert ctl.lyric_position() == pytest.approx(11.5)


def test_karaoke_settings_reach_the_window(ctl):
    ctl.apply_settings(replace(ctl.settings, karaoke=False, sung_color="#FF0000"))
    assert not ctl.window.style_.karaoke and ctl.window.style_.sung_color == "#FF0000"


def test_obs_window_opens_mirrors_and_is_remembered(ctl):
    ctl.on_result(synced())
    ctl.tray.obs_action.setChecked(True)
    assert ctl.obs is not None and ctl.obs.isVisible() and ctl.obs in ctl.window.mirrors
    assert ctl.obs._lines == LINES
    assert ctl.store.load().obs_window
    ctl.obs.close()  # title-bar close unchecks the tray entry
    assert not ctl.tray.obs_action.isChecked()
    assert ctl.obs not in ctl.window.mirrors and not ctl.store.load().obs_window


def test_obs_window_is_restored_on_start(ctl):
    ctl.settings = replace(ctl.settings, obs_window=True, obs_rect=[100, 120, 800, 200])
    ctl.restore_state()
    assert ctl.obs is not None and ctl.obs.geometry().width() == 800
    ctl.tray.obs_action.setChecked(False)


def test_obs_background_comes_from_the_settings(ctl):
    ctl.tray.obs_action.setChecked(True)
    ctl.apply_settings(replace(ctl.settings, obs_background="#0000FF"))
    assert ctl.obs.style_.obs_background == "#0000FF"
    ctl.tray.obs_action.setChecked(False)


def test_each_direction_keeps_its_own_window_place(ctl):
    ctl.window.restore_geometry([100, 700, 1000, 120])
    ctl.on_geometry(ctl.window.geometry())
    ctl.apply_settings(replace(ctl.settings, vertical=True, vertical_rect=[1500, 100, 110, 700]))
    assert ctl.window.geometry().height() == 700
    ctl.on_geometry(ctl.window.geometry())
    assert ctl.store.load().vertical_rect == [1500, 100, 110, 700]
    assert ctl.store.load().window_rect == [100, 700, 1000, 120]
    ctl.apply_settings(replace(ctl.settings, vertical=False))
    assert ctl.window.geometry().width() == 1000


def test_previewing_a_turn_never_saves_into_the_other_direction(ctl):
    ctl.settings = replace(ctl.settings, window_rect=[100, 700, 1000, 120])
    ctl.apply_settings(replace(ctl.settings, vertical=True), live_only=True)
    ctl.on_geometry(ctl.window.geometry())
    assert ctl.store.load().window_rect != [ctl.window.x(), ctl.window.y(), ctl.window.width(), ctl.window.height()]
    ctl.apply_settings(ctl.settings, live_only=True)  # Cancel
    assert ctl.window.geometry().width() == 1000


def test_editor_starts_from_where_lines_show(ctl):
    ctl.on_result(synced(offset=0.5, translations=("一", None, "三")))
    ctl.open_editor()
    lines, translations = ctl._editor.lines()
    assert [l.start for l in lines] == [5.5, 10.5, 15.5]  # offset folded into the times
    assert translations == ["一", None, "三"] and not ctl._editor.revert_button.isEnabled()
    ctl._editor._dirty = False
    ctl._editor.close()


def test_editor_spreads_plain_lyrics_over_the_song(ctl):
    ctl.on_result(LyricResult(TRACK, ResultKind.PLAIN_ONLY, Lyrics(LINES, False)))
    ctl.open_editor()
    lines, _ = ctl._editor.lines()
    assert [round(l.start, 1) for l in lines] == [0.0, 66.7, 133.3]
    ctl._editor.close()


def test_editor_needs_a_song(ctl):
    ctl.current = None
    ctl.open_editor()
    assert ctl._editor is None and ctl.tray.messages[-1][0] == "編輯歌詞"


def test_aligned_result_shows_l3_times_and_words(ctl):
    from lyricfloat.models import Alignment, WordTiming

    words = ((WordTiming(0, 5, 5.2, 6.0), WordTiming(6, 10, 6.2, 7.0)), None, None)
    result = replace(synced(), alignment=Alignment((5.2, 10.1, 15.3), words))
    ctl.on_result(result)
    assert [l.start for l in ctl.window._lines] == [5.2, 10.1, 15.3]
    assert ctl.window._words == words
    assert ctl.driver._starts == [5.2, 10.1, 15.3]
    ctl.open_editor()
    assert [l.start for l in ctl._editor.lines()[0]] == [5.2, 10.1, 15.3]  # editing starts from what shows
    ctl._editor.close()


class FakeL3:
    def __init__(self):
        self.calls = []

    def on_track(self, track):
        self.calls.append(("track", track.title if track else None))

    def on_result(self, result):
        self.calls.append(("result", result.kind.value))

    def set_enabled(self, on):
        self.calls.append(("enabled", on))

    def on_cue(self, offset):
        self.calls.append(("cue", offset))

    def status_text(self):
        return "ready"


def test_aligned_song_shows_the_mark_and_reverts(ctl):
    from lyricfloat.models import Alignment

    ctl.on_result(replace(synced(), alignment=Alignment((5.2, 10.1, 15.3), (None, None, None))))
    assert not ctl.window.toolbar.aligned.isHidden()
    reverted = []

    async def revert(track):
        reverted.append(track)

    ctl.service.revert_alignment = revert
    ctl.window.toolbar.aligned.click()
    assert ctl.window._flash == "已還原原始時間軸"
    ctl.on_track(TRACK)
    assert ctl.window.toolbar.aligned.isHidden()


def test_l3_hears_tracks_results_and_the_switch(ctl):
    fake = ctl.l3 = FakeL3()
    ctl.on_track(TRACK)
    ctl.on_result(synced())
    ctl.apply_settings(replace(ctl.settings, l3_enabled=True))
    assert fake.calls == [("track", TRACK.title), ("result", "synced"), ("enabled", True)]


def test_aligned_plain_lyrics_show_synced_without_the_unsynced_mark(ctl):
    from lyricfloat.models import Alignment

    plain_lines = tuple(LyricLine(0.0, l.text) for l in LINES)
    result = LyricResult(TRACK, ResultKind.PLAIN_ONLY, Lyrics(plain_lines, False), alignment=Alignment((4.0, 9.0, 14.0), (None, None, None)))
    ctl.on_result(result)
    assert not ctl.window._unsynced and [l.start for l in ctl.window._lines] == [4.0, 9.0, 14.0]
    assert ctl.driver._starts == [4.0, 9.0, 14.0] and ctl.adjust_offset(1) is not None  # synced now: offsets work


def test_editor_opens_plain_lyrics_untimed(ctl):
    ctl.on_result(LyricResult(TRACK, ResultKind.PLAIN_ONLY, Lyrics(LINES, False)))
    ctl.open_editor()
    assert ctl._editor.untimed
    ctl._editor.close()
    ctl.on_result(synced())
    ctl.open_editor()
    assert not ctl._editor.untimed
    ctl._editor._dirty = False
    ctl._editor.close()


# -- versions of another length: shown at once, cued by hand or by the start finder -------------

CREDITED = (LyricLine(0.0, "作詞 : 某人"),) + LINES  # NetEase-style credit before the singing


def other_length(lines=CREDITED, length=150.0, **changes):
    """A version `length` s long for the 200 s TRACK (an MV 50 s longer by default)."""
    cand = Candidate("lrclib", "1", TRACK.title, TRACK.artist, "", length, "[00:05.00]x")
    return replace(LyricResult(TRACK, ResultKind.SYNCED, Lyrics(lines, True), cand, version_mismatch=True), **changes)


def at(ctl, position):
    ctl.driver.on_anchor(PlaybackAnchor(position, 0.0, playing=False, duration=200.0))


@pytest.fixture
def saved(ctl, monkeypatch):
    offsets = []
    monkeypatch.setattr(ctl.service, "save_offset", lambda track, offset: offsets.append(offset))
    return offsets


def test_another_length_shows_its_own_times_at_once(ctl):
    ctl.on_result(other_length())  # SPEC v1.36: no waiting at the first line any more
    assert ctl.driver.has_lines and ctl.driver.offset == 0.0
    assert ctl.window.toolbar.cue.isVisibleTo(ctl.window.toolbar) and ctl.window._cue_hint is None
    assert ctl.window.toolbar.offset_label.text() == "±0.0s"


def test_the_cue_starts_the_first_sung_line_now(ctl, saved):
    ctl.on_result(other_length())
    at(ctl, 47.0)  # the MV sings its first line at 47 s, the source at 5 s
    ctl.cue_line()
    assert ctl.driver.offset == pytest.approx(42.0) and ctl.window._index == 1
    assert saved == [pytest.approx(42.0)] and ctl.last_result.offset == pytest.approx(42.0)
    assert ctl.window.toolbar.offset_label.text() == "+42.0s" and ctl.window._flash == "已對齊開唱"
    assert ctl.adjust_offset(1) == pytest.approx(42.1)  # past the usual 30 s limit


def test_a_later_cue_catches_up_a_line_and_a_double_press_is_ignored(ctl, saved):
    ctl.on_result(other_length())
    at(ctl, 47.0)
    ctl.cue_line()
    at(ctl, 51.0)
    ctl.cue_line()  # within a second of the first: a double press
    assert saved == [pytest.approx(42.0)]
    ctl._last_cue -= 2.0
    at(ctl, 51.5)  # the screen still shows the first line, but the second (10 s in the source) is sung now
    assert ctl.window._index == 1
    ctl.cue_line()
    assert ctl.driver.offset == pytest.approx(41.5) and ctl.window._index == 2


def test_a_cued_song_starts_synced_on_the_next_play(ctl):
    ctl.on_result(other_length(offset=42.0))
    assert ctl.driver.has_lines and ctl.driver.offset == pytest.approx(42.0)
    assert ctl.window.toolbar.cue.isVisibleTo(ctl.window.toolbar)


def test_an_aligned_version_of_another_length_shows_the_aligned_times(ctl):
    from lyricfloat.models import Alignment

    starts = (40.0, 45.0, 50.0, 55.0)
    ctl.on_result(other_length(alignment=Alignment(starts, (None,) * 4)))
    assert ctl.driver.has_lines and ctl.driver._starts == list(starts)


def test_songs_of_the_right_length_have_no_cue(ctl, saved):
    ctl.on_result(synced())
    assert not ctl.window.toolbar.cue.isVisibleTo(ctl.window.toolbar)
    ctl.cue_line()
    assert saved == [] and ctl.window._flash == "這首不需要對齊開唱"
    ctl.driver.set_offset(29.95)
    assert ctl.adjust_offset(1) == pytest.approx(30.0)  # the usual limit


def test_offset_keys_work_at_once_on_another_length(ctl):
    ctl.on_result(other_length())
    ctl.on_shortcut("offset_later")
    assert ctl.window._flash == "偏移 +0.1s"


def test_the_cue_shortcut_cues(ctl, fake_hotkeys, saved):
    ctl.apply_shortcuts()
    ctl.on_result(other_length())
    at(ctl, 47.0)
    ctl.on_shortcut("cue_line")
    assert saved == [pytest.approx(42.0)]


def test_a_version_a_few_seconds_off_can_be_cued_at_its_first_line(ctl, saved):
    ctl.on_result(other_length(length=195.6))
    at(ctl, 9.4)  # the player's version sings the first line 4.4 s late
    ctl.cue_line()
    assert ctl.driver.offset == pytest.approx(4.4) and saved == [pytest.approx(4.4)]


def test_a_cue_far_from_the_start_catches_up_the_next_line(ctl, saved):
    ctl.on_result(other_length(length=195.6))
    at(ctl, 14.0)  # past where the first line can start (5 s + up to 4.4 s + 3 s): the third line is sung now
    ctl.cue_line()
    assert ctl.driver.offset == pytest.approx(-1.0) and saved == [pytest.approx(-1.0)]


def test_a_new_song_drops_the_cue(ctl):
    ctl.on_result(other_length())
    ctl.on_track(replace(TRACK, title="Another Song"))
    assert ctl._cue_lines is None and not ctl.window.toolbar.cue.isVisibleTo(ctl.window.toolbar)


# -- the start finder's news (SPEC v1.36) ---------------------------------------------------


def test_a_found_start_is_applied_like_a_cue(ctl, saved):
    fake = ctl.l3 = FakeL3()
    ctl.on_result(other_length())
    ctl.on_start_found(TRACK, 41.5)
    assert ctl.driver.offset == pytest.approx(41.5) and saved == [pytest.approx(41.5)]
    assert ctl.window._flash == "已自動對齊開唱" and ("cue", 41.5) in fake.calls
    at(ctl, 51.0)  # a cue after it catches up a line instead of moving the first one again
    ctl.cue_line()
    assert ctl.driver.offset == pytest.approx(41.0)


def test_the_users_cue_beats_a_late_found_start(ctl, saved):
    ctl.on_result(other_length())
    at(ctl, 47.0)
    ctl.cue_line()
    ctl.on_start_found(TRACK, 39.0)
    assert ctl.driver.offset == pytest.approx(42.0) and saved == [pytest.approx(42.0)]


def test_a_found_start_for_another_song_is_ignored(ctl, saved):
    ctl.on_result(other_length())
    ctl.on_start_found(replace(TRACK, title="Another Song"), 39.0)
    assert ctl.driver.offset == 0.0 and saved == []


def test_the_corner_says_while_the_finder_listens_and_when_it_missed(ctl):
    ctl.on_result(other_length())
    ctl.on_start_searching(TRACK, True)
    assert ctl.window._cue_hint == "自動對齊開唱中…"
    ctl.on_result(other_length())  # the lyrics shown again (translations, say): the hint stays
    assert ctl.window._cue_hint == "自動對齊開唱中…"
    ctl.on_start_searching(TRACK, False)
    ctl.on_start_missed(TRACK)
    assert ctl.window._cue_hint is None and ctl.window._flash.startswith("沒聽到開唱點")


def test_the_l3_signals_reach_the_controller(qapp, tmp_path, autostart_calls):
    class SignalL3(QObject):
        startFound = pyqtSignal(object, float)
        startSearching = pyqtSignal(object, bool)
        startMissed = pyqtSignal(object)

        def on_track(self, track):
            pass

        def on_result(self, result):
            pass

        def on_cue(self, offset):
            pass

    l3 = SignalL3()
    window = LyricWindow()
    controller = AppController(LyricService(LyricResolver([])), SyncDriver(), window, FakeTray(window), l3=l3)
    controller.history_path = tmp_path / "history.jsonl"  # never the real play history
    controller.current = TRACK
    controller.on_result(other_length())
    l3.startSearching.emit(TRACK, True)
    assert window._cue_hint == "自動對齊開唱中…"
    l3.startFound.emit(TRACK, 41.5)
    assert controller.driver.offset == pytest.approx(41.5)
    window.close()



# -- listened to: the source times at once, the start found in the background (SPEC v1.79) --------


def listened(ctl, **changes):
    """The finder starts listening as the result arrives, as L3 does before the screen updates."""
    ctl.on_start_searching(TRACK, True)
    ctl.on_result(other_length(**changes))


@pytest.mark.parametrize("length", [150.0, 195.6])  # 50 s off, 4.4 s off
def test_a_listened_version_shows_its_source_times_at_once(ctl, length):
    listened(ctl, length=length)  # SPEC v1.79 (Din): even far off, it no longer waits at the first line
    assert ctl.driver.has_lines and ctl.driver.offset == 0.0
    assert ctl.window._cue_hint == "自動對齊開唱中…" and ctl.window.toolbar.offset_label.text() == "±0.0s"
    assert ctl.window._flash is None  # the finder is on it: no key to press yet
    ctl.on_shortcut("offset_later")
    assert ctl.window._flash == "偏移 +0.1s"


def test_the_found_start_moves_the_shown_lyrics(ctl, saved):
    listened(ctl)
    at(ctl, 47.0)
    ctl.on_start_found(TRACK, 42.0)
    ctl.on_start_searching(TRACK, False)  # the finder's search ends after its news
    assert ctl.driver.has_lines and ctl.driver.offset == pytest.approx(42.0)
    assert ctl.window._cue_hint is None and ctl.window._flash == "已自動對齊開唱" and saved == [pytest.approx(42.0)]


@pytest.mark.parametrize("ending", ["missed", "stopped"])
def test_a_search_ending_without_a_start_keeps_the_source_times(ctl, saved, ending):
    listened(ctl)
    if ending == "missed":
        ctl.on_start_missed(TRACK)
    ctl.on_start_searching(TRACK, False)  # missed, or stopped by a pause or a seek
    assert ctl.driver.has_lines and ctl.driver.offset == 0.0 and ctl.window._cue_hint is None and saved == []
    if ending == "missed":
        assert ctl.window._flash.startswith("沒聽到開唱點")


def test_a_far_off_version_nobody_listens_to_shows_at_once_with_the_key(ctl):
    ctl.on_result(other_length())  # L3 off, started late, another player before: no finder
    assert ctl.driver.has_lines and ctl.window._flash == "開唱時可按工具列「對齊開唱」對齊"


def test_lyrics_shown_again_after_a_cue_keep_it(ctl, saved):
    listened(ctl)
    ctl.on_start_found(TRACK, 42.0)
    ctl.on_start_searching(TRACK, False)
    ctl.on_result(other_length())  # translations arrive with the result made before the cue
    assert ctl.driver.has_lines and ctl.driver.offset == pytest.approx(42.0)


def test_a_new_song_does_not_hear_the_old_search_end(ctl):
    listened(ctl)
    ctl.on_track(replace(TRACK, title="Another Song"))
    ctl.on_start_searching(TRACK, False)
    assert ctl.window._flash is None


# -- a found start on a song of its own length (SPEC v1.74) ---------------------------------


def own_length(**changes):
    cand = Candidate("lrclib", "1", TRACK.title, TRACK.artist, "", 199.0, "[00:05.00]x")
    return replace(LyricResult(TRACK, ResultKind.SYNCED, Lyrics(CREDITED, True), cand), **changes)


def test_a_found_start_shifts_a_song_of_its_own_length_for_this_play(ctl, saved):
    fake = ctl.l3 = FakeL3()
    ctl.on_result(own_length())
    ctl.on_start_searching(TRACK, True)
    assert ctl.window._cue_hint is None  # listened to quietly
    ctl.on_start_found(TRACK, 2.6)
    assert ctl.driver.offset == pytest.approx(2.6) and ctl.window._flash == "已自動對齊開唱"
    assert saved == [] and ("cue", 2.6) in fake.calls  # not the user's offset: L3 still times it next time
    ctl.on_result(own_length())  # shown again (translations): the shift stays
    assert ctl.driver.offset == pytest.approx(2.6)
    ctl.on_track(replace(TRACK, title="Another Song"))
    assert ctl._shift_for is None


@pytest.mark.parametrize("found", [0.3, -1.6, 2.0])
@pytest.mark.parametrize("result", [own_length, other_length])
def test_a_found_start_within_2_s_of_the_source_times_changes_nothing(ctl, saved, result, found):
    fake = ctl.l3 = FakeL3()
    ctl.on_result(result())
    ctl.on_start_found(TRACK, found)  # SPEC v1.80 (Din): left for the whole-song L3 after the play
    assert ctl.driver.offset == 0.0 and ctl.window._flash != "已自動對齊開唱" and saved == []
    assert ctl.window.toolbar.offset_label.text() == "±0.0s" and not ctl._cued
    # An MV's whole-song timing still leaves out the intro before the start heard.
    assert (("cue", found) in fake.calls) == (result is other_length)


def test_a_found_start_within_2_s_does_not_move_the_users_cue(ctl, saved):
    fake = ctl.l3 = FakeL3()
    ctl.on_result(other_length())
    at(ctl, 47.0)
    ctl.cue_line()
    ctl.on_start_found(TRACK, 1.0)
    assert ctl.driver.offset == pytest.approx(42.0) and fake.calls.count(("cue", 1.0)) == 0


def test_a_found_start_leaves_the_users_own_shift_alone(ctl, saved):
    ctl.on_result(own_length())
    ctl.adjust_offset(5)
    ctl.on_start_found(TRACK, 3.0)
    assert ctl.driver.offset == pytest.approx(0.5)


def test_a_missed_start_on_a_song_of_its_own_length_says_nothing(ctl):
    ctl.on_result(own_length())
    ctl.on_start_missed(TRACK)
    assert ctl.window._flash is None or "開唱" not in ctl.window._flash
