import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.models import LyricLine, PlaybackAnchor
from lyricfloat.sync.driver import SyncDriver
from lyricfloat.sync.engine import SyncEngine

LINES = [LyricLine(5.0, "a"), LyricLine(10.0, "b"), LyricLine(15.0, "c")]


class FakeClock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def driver(app):
    clock = FakeClock()
    d = SyncDriver(SyncEngine(clock=clock))
    d.clock = clock
    d.emitted = []
    d.lineChanged.connect(lambda i, animate: d.emitted.append((i, animate)))
    d.on_anchor(PlaybackAnchor(position=9.0, at=clock.t, playing=False, duration=30.0))
    return d


def test_lines_without_offset(driver):
    driver.set_lines(LINES)
    assert driver.engine.index == 0
    assert driver.has_lines


def test_positive_offset_shows_lines_later(driver):
    driver.set_lines(LINES, offset=1.5)  # line b now starts at 11.5
    driver.on_anchor(PlaybackAnchor(10.5, driver.clock.t, False, 30.0))
    assert driver.engine.index == 0


def test_negative_offset_shows_lines_earlier(driver):
    driver.set_lines(LINES, offset=-1.5)  # line b now starts at 8.5
    assert driver.engine.index == 1


def test_changing_offset_repositions_without_animation(driver):
    driver.set_lines(LINES)
    driver.emitted.clear()
    driver.set_offset(-1.5)
    assert driver.emitted == [(1, False)]
    assert driver.offset == -1.5


def test_reset_clears_offset_and_lines(driver):
    driver.set_lines(LINES, offset=0.7)
    driver.reset()
    assert driver.offset == 0.0
    assert not driver.has_lines


def test_offset_without_lines_is_just_stored(driver):
    driver.reset()
    driver.emitted.clear()
    driver.set_offset(0.3)
    assert driver.emitted == []
