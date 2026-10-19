"""FR-18 OBS window: an ordinary window mirroring the floating one. Placeholder text only."""

from dataclasses import replace

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QEnterEvent
from PyQt6.QtWidgets import QApplication

from lyricfloat.models import LyricLine, TrackInfo
from lyricfloat.ui.lyric_window import WAITING_TEXT, LyricWindow

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
LINES = (LyricLine(5.0, "first line"), LyricLine(10.0, "second line"), LyricLine(15.0, "third line"))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pair(app):
    main, obs = LyricWindow(), LyricWindow(obs=True)
    yield main, obs
    obs.close()
    main.close()


def test_obs_window_is_an_ordinary_capturable_window(pair):
    main, obs = pair
    flags = obs.windowFlags()
    assert obs.windowType() == Qt.WindowType.Window  # not a Tool window: it gets a taskbar entry
    assert not flags & Qt.WindowType.FramelessWindowHint
    assert not flags & Qt.WindowType.WindowStaysOnTopHint
    assert not obs.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert obs.windowTitle() == LyricWindow.OBS_TITLE
    assert main.windowType() == Qt.WindowType.Tool  # the floating window is unchanged


def test_obs_background_is_the_solid_colour(pair):
    _main, obs = pair
    obs.resize(600, 160)
    corner = obs.grab().toImage().pixelColor(3, 3)
    assert (corner.red(), corner.green(), corner.blue(), corner.alpha()) == (0, 255, 0, 255)
    obs.apply_style(replace(obs.style_, obs_background="#112233"))
    corner = obs.grab().toImage().pixelColor(3, 3)
    assert (corner.red(), corner.green(), corner.blue()) == (0x11, 0x22, 0x33)


def test_attached_mirror_starts_from_what_is_shown(pair):
    main, obs = pair
    main.set_lyrics(TRACK, LINES, ("一", None, "三"))
    main.show_line(1, animate=False)
    main.attach_mirror(obs)
    assert obs._text == main._text and obs._lines == LINES


def test_content_calls_are_repeated_on_the_mirror(pair):
    main, obs = pair
    main.attach_mirror(obs)
    main.set_lyrics(TRACK, LINES)
    main.show_line(2, animate=False)
    assert obs._text.primary == "third line"
    main.set_show_translation(False)
    main.apply_style(replace(main.style_, main_px=40))
    assert not obs._show_translation and obs.style_.main_px == 40
    main.show_waiting()
    assert obs._text.primary == WAITING_TEXT


def test_detached_mirror_stops_following(pair):
    main, obs = pair
    main.attach_mirror(obs)
    main.attach_mirror(obs)  # attaching twice keeps one copy
    assert main.mirrors == [obs]
    main.detach_mirror(obs)
    main.show_message("only on the floating window")
    assert obs._text.primary != "only on the floating window"


def test_obs_window_ignores_hover(pair):
    _main, obs = pair
    point = QPointF(5, 5)
    obs.enterEvent(QEnterEvent(point, point, point))
    assert not obs._hover_timer.isActive()


def test_closing_the_obs_window_reports_it(pair):
    _main, obs = pair
    got = []
    obs.closed.connect(lambda: got.append(True))
    obs.show()
    obs.close()
    assert got == [True]


def test_obs_second_line_is_opaque_for_clean_keying(pair):
    main, obs = pair
    for w in (main, obs):
        w.resize(700, 160)
        w.set_lyrics(TRACK, (LyricLine(5.0, "WWWWWW"), LyricLine(10.0, "WWWWWW")))
        w.show_line(0, animate=False)

    def brightest_in_second_line(w):
        image = w.grab().toImage()
        rect = w._line_rects()[1].toAlignedRect()
        best = 0
        for y in range(rect.top(), rect.bottom(), 2):
            for x in range(rect.left(), rect.right(), 2):
                c = image.pixelColor(x, y)
                best = max(best, min(c.red(), c.green(), c.blue()))  # white text, not green background
        return best

    assert brightest_in_second_line(obs) > 240
