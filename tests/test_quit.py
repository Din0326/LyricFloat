"""Quit from the tray ends the process cleanly (the packaged build showed "Event loop stopped before
Future completed"). Runs the app in demo mode: no media watcher, no network."""

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import lyricfloat.__main__ as entry
import lyricfloat.app as app_mod


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tray_quit_finishes_shutdown(qapp, monkeypatch, caplog):
    monkeypatch.setattr("lyricfloat.ui.tray.QSystemTrayIcon.show", lambda self: None)
    monkeypatch.setattr(entry, "QApplication", lambda argv: qapp)  # one QApplication per process
    monkeypatch.setattr(entry, "setup_logging", lambda debug: "logs")

    class Tray(app_mod.TrayController):
        def __init__(self, window, on_quit, parent=None):
            super().__init__(window, on_quit, parent)
            QTimer.singleShot(300, on_quit)  # what the tray's Quit item runs

    monkeypatch.setattr(app_mod, "TrayController", Tray)
    with caplog.at_level("INFO"):
        assert entry.main(["--demo"]) == 0
    assert "shutting down" in caplog.text
