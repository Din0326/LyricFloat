"""FR-16 vertical mode. Placeholder text only."""

from dataclasses import replace

import pytest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from lyricfloat.models import LyricLine, TrackInfo
from lyricfloat.ui.lyric_window import LyricStyle, LyricWindow
from lyricfloat.ui.text_layout import LineLayout, upright

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def font():
    f = QFont()
    f.setFamilies(["Segoe UI", "Microsoft JhengHei"])
    f.setPixelSize(32)
    return f


@pytest.mark.parametrize("ch, stands", [("中", True), ("あ", True), ("한", True), ("Ａ", True), ("A", False), ("1", False), ("（", False), ("ー", False), ("—", False)])
def test_upright(ch, stands):
    assert upright(ch) is stands


def test_vertical_layout_runs_down_one_column(app):
    layout = LineLayout("中文 mixed 字", font(), vertical=True)
    box = layout.path(0, 0, 50).boundingRect()
    assert box.width() <= 50 and box.height() > box.width()
    assert box.height() == pytest.approx(layout.length, rel=0.15)


def test_edge_moves_forward_through_the_line(app):
    text = "中文 mixed 字"
    for vertical in (False, True):
        layout = LineLayout(text, font(), vertical)
        edges = [layout.edge(i, 0.0) for i in range(len(text) + 1)]
        assert edges == sorted(edges) and edges[-1] == layout.length
        assert layout.edge(0, 0.5) == pytest.approx(layout.edge(1, 0.0) / 2)


def test_horizontal_layout_matches_the_text_width(app):
    layout = LineLayout("hello 世界", font(), vertical=False)
    assert layout.length == pytest.approx(layout.metrics.horizontalAdvance("hello 世界"))


# -- window ---------------------------------------------------------------------


@pytest.fixture
def window(app):
    w = LyricWindow(LyricStyle(vertical=True))
    w.resize(120, 600)
    yield w
    w.close()


def test_columns_put_the_current_line_on_the_left(window):
    main, second = window._line_rects()
    assert main.right() <= second.left() + 0.01
    assert main.height() > main.width()


def test_default_place_is_tall_and_toolbar_is_a_column(window):
    window._place_default()
    assert window.height() > window.width()
    assert window.toolbar.height() > window.toolbar.width()


def test_turning_switches_layout_and_size(window):
    window.apply_style(replace(window.style_, vertical=False))
    assert window.width() > window.height()
    assert window.toolbar.width() > window.toolbar.height()
    main, second = window._line_rects()
    assert second.top() >= main.bottom() - 0.01


def sung_extent(window):
    image = window.grab().toImage()
    count, bottom = 0, -1
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            c = image.pixelColor(x, y)
            if c.alpha() > 200 and c.blue() > 220 and 170 < c.green() < 220 and c.red() < 120:
                count += 1
                bottom = max(bottom, y)
    return count, bottom


def test_karaoke_fills_from_the_top(window):
    lines = (LyricLine(10.0, "一二三四五六七八"), LyricLine(14.0, "next"))
    clock = {"t": 10.0}
    window.set_clock(lambda: clock["t"])
    window.set_lyrics(TRACK, lines)
    window.show_line(0, animate=False)
    end = window._karaoke.end_of(0)
    clock["t"] = 10.0 + (end - 10.0) * 0.4
    early = sung_extent(window)
    clock["t"] = end
    full = sung_extent(window)
    assert 0 < early[0] < full[0] and early[1] < full[1]


def test_overlong_column_scrolls(window):
    window.resize(120, 260)
    window.set_lyrics(TRACK, (LyricLine(1.0, "很長很長的一句歌詞" * 3),))
    window.show_line(0, animate=False)
    assert window._overflow[0] > 0 and window._marquee.isActive()
