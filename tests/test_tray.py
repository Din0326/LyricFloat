"""Tray "follow player" submenu (FR-24) and the usage line (FR-27). The icon itself is never
shown in tests."""

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.ui.lyric_window import LyricWindow
from lyricfloat.diagnostics.usage import Usage
from lyricfloat.ui.tray import USAGE_IDLE_MS, USAGE_OPEN_MS, TrayController, player_label


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tray(qapp, monkeypatch):
    monkeypatch.setattr("lyricfloat.ui.tray.QSystemTrayIcon.show", lambda self: None)
    window = LyricWindow()
    t = TrayController(window, on_quit=lambda: None)
    yield t
    t.hide()
    window.close()


@pytest.mark.parametrize(
    "app_id, label",
    [("Spotify.exe", "Spotify"), ("Chrome", "Chrome"), ("SpotifyAB.SpotifyMusic_x!Spotify", "Spotify"), ("msedge.EXE", "msedge")],
)
def test_player_label(app_id, label):
    assert player_label(app_id) == label


def entries(tray):
    tray.player_menu.aboutToShow.emit()
    return [(a.text(), a.isChecked()) for a in tray.player_menu.actions()]


def test_menu_lists_auto_and_open_players(tray):
    tray.set_players_provider(lambda: ["Chrome", "Spotify.exe"])
    assert entries(tray) == [("自動（跟隨 Windows 目前播放）", True), ("Chrome", False), ("Spotify", False)]


def test_pinned_player_that_is_closed_stays_listed(tray):
    tray.set_players_provider(lambda: ["Chrome"])
    tray.set_pinned("Spotify.exe")
    assert entries(tray)[-1] == ("Spotify（未開啟）", True)


def test_picking_a_player_emits_the_pin(tray):
    got = []
    tray.pinChanged.connect(got.append)
    tray.set_players_provider(lambda: ["Chrome", "Spotify.exe"])
    tray.player_menu.aboutToShow.emit()
    tray.player_menu.actions()[2].trigger()
    assert got == ["Spotify.exe"]
    tray.player_menu.aboutToShow.emit()
    tray.player_menu.actions()[0].trigger()
    assert got == ["Spotify.exe", ""]


class FakeMeter:
    def __init__(self):
        self.readings = [Usage(None, 50.0), Usage(0.4, 86.0)]

    def read(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


@pytest.fixture
def metered_tray(qapp, monkeypatch):
    monkeypatch.setattr("lyricfloat.ui.tray.QSystemTrayIcon.show", lambda self: None)
    window = LyricWindow()
    t = TrayController(window, on_quit=lambda: None, meter=FakeMeter())
    yield t
    t.hide()
    window.close()


def test_usage_is_the_first_menu_item_and_read_only(metered_tray):
    first = metered_tray.menu.actions()[0]
    assert first is metered_tray.usage_action
    assert not first.isEnabled()
    assert first.text() == "CPU —・記憶體 50 MB"  # CPU needs a second reading
    metered_tray._usage_timer.timeout.emit()
    assert first.text() == "CPU 0.4%・記憶體 86 MB"


def test_usage_refreshes_every_second_only_while_the_menu_is_open(metered_tray):
    timer = metered_tray._usage_timer
    assert timer.isActive() and timer.interval() == USAGE_IDLE_MS
    metered_tray.menu.aboutToShow.emit()
    assert timer.interval() == USAGE_OPEN_MS
    metered_tray.menu.aboutToHide.emit()
    assert timer.interval() == USAGE_IDLE_MS
