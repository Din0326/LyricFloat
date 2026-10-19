import pytest
from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtWidgets import QApplication

from lyricfloat.models import LyricLine, TrackInfo
from lyricfloat.settings import Settings
from lyricfloat.ui.lyric_window import (
    NEXT,
    NOT_FOUND_TEXT,
    TRANSLATION,
    WAITING_TEXT,
    DisplayText,
    LyricWindow,
    style_from_settings,
)

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
LINES = (LyricLine(5.0, "first line"), LyricLine(10.0, "second line"), LyricLine(15.0, "third line"))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = LyricWindow()
    yield w
    w.close()


def test_window_has_no_graphics_effect(window):
    # A QGraphicsEffect on this translucent top-level window freezes what is on
    # screen after the first paint on Windows (grab() still looks fine).
    assert window.graphicsEffect() is None


def test_waiting_is_replaced_by_the_track(window):
    window.show_waiting()
    assert window._text == DisplayText(WAITING_TEXT)
    window.show_loading(TRACK)
    assert window._text == DisplayText(TRACK.describe())
    assert not window._waiting_timer.isActive()


def test_lines_show_current_and_next(window):
    window.set_lyrics(TRACK, LINES)
    assert window._text == DisplayText(TRACK.describe(), "first line", NEXT)  # intro
    window.show_line(1, animate=False)
    assert window._text == DisplayText("second line", "third line", NEXT, 1)
    window.show_line(2, animate=False)
    assert window._text == DisplayText("third line", "", NEXT, 2)


def test_translation_replaces_next_line(window):
    window.set_lyrics(TRACK, LINES, translations=("第一行", None, "第三行"))
    window.show_line(0, animate=False)
    assert window._text == DisplayText("first line", "第一行", TRANSLATION, 0)


def test_untranslated_line_in_translated_song_stays_single(window):
    window.set_lyrics(TRACK, LINES, translations=("第一行", None, "第三行"))
    window.show_line(1, animate=False)
    assert window._text == DisplayText("second line", "", TRANSLATION, 1)


def test_song_without_any_translation_shows_next_line(window):
    window.set_lyrics(TRACK, LINES, translations=(None, None, None))
    window.show_line(0, animate=False)
    assert window._text == DisplayText("first line", "second line", NEXT, 0)
    window.set_lyrics(TRACK, LINES, translations=())
    window.show_line(0, animate=False)
    assert window._text.secondary_kind == NEXT


def test_translation_switched_off_shows_next_line(window):
    window.set_lyrics(TRACK, LINES, translations=("第一行", "第二行", "第三行"))
    window.show_line(0, animate=False)
    window.set_show_translation(False)
    assert window._text == DisplayText("first line", "second line", NEXT, 0)
    window.set_show_translation(True)
    assert window._text == DisplayText("first line", "第一行", TRANSLATION, 0)


def test_not_found_message(window):
    window.show_message(TRACK.describe(), NOT_FOUND_TEXT)
    assert window._text == DisplayText(TRACK.describe(), NOT_FOUND_TEXT)


def test_style_from_settings_follows_the_user(window):
    s = Settings(font_px=40, text_color="#FFEE00", outline_color="#112233", outline_opacity=0.5, alignment="left", line_animation=False)
    style = style_from_settings(s)
    assert style.main_px == 40
    assert style.outline_rgba == (0x11, 0x22, 0x33, 128)
    assert style.alignment == "left"
    assert style.line_change_ms == 0
    window.apply_style(style)
    assert window.height() >= int(40 * 1.6 + 30 * 1.6 + 24) - 1


def test_overlong_line_scrolls(window):
    window.resize(300, window.height())
    window.set_lyrics(TRACK, (LyricLine(1.0, "a very long line " * 12),))
    window.show_line(0, animate=False)
    assert window._overflow[0] > 0
    assert window._marquee.isActive()
    window.show_message("short")
    assert window._overflow == [0.0, 0.0]
    assert not window._marquee.isActive()


def test_restore_geometry_falls_back_when_off_screen(window):
    window.restore_geometry([-50_000, -50_000, 800, 120])
    assert window.geometry() == window._default_geometry()
    window.restore_geometry(None)
    assert window.geometry() == window._default_geometry()


def test_toolbar_offset_label(window):
    window.set_offset_label(None)
    assert not window.toolbar.later.isEnabled()
    window.set_offset_label(0.3)
    assert window.toolbar.offset_label.text() == "+0.3s"
    window.set_offset_label(-0.1)
    assert window.toolbar.offset_label.text() == "-0.1s"
    window.set_offset_label(0.0)
    assert window.toolbar.offset_label.text() == "±0.0s"


def test_toolbar_buttons_emit_requests(window):
    got = []
    window.offsetStepRequested.connect(lambda s: got.append(("offset", s)))
    window.fontStepRequested.connect(lambda s: got.append(("font", s)))
    window.hideRequested.connect(lambda: got.append(("hide",)))
    window.set_offset_label(0.0)
    window.toolbar.later.click()
    window.toolbar.earlier.click()
    window.toolbar.bigger.click()
    window.toolbar.close_.click()
    assert got == [("offset", 1), ("offset", -1), ("font", 1), ("hide",)]


def test_offset_buttons_repeat_while_held(window):
    assert window.toolbar.later.autoRepeat() and window.toolbar.earlier.autoRepeat()
    assert not window.toolbar.bigger.autoRepeat()


def test_player_buttons_are_disabled_without_a_session(window):
    tb = window.toolbar
    assert not any(b.isEnabled() for b in (tb.previous, tb.play_pause, tb.next))


def test_player_buttons_emit_controls_and_show_the_next_action(window):
    got = []
    window.mediaControlRequested.connect(got.append)
    window.set_media_state(True, playing=True)
    assert window.toolbar.play_pause.toolTip() == "暫停"
    window.toolbar.previous.click()
    window.toolbar.play_pause.click()
    window.toolbar.next.click()
    assert got == ["previous", "play_pause", "next"]
    window.set_media_state(True, playing=False)
    assert window.toolbar.play_pause.toolTip() == "播放"


# -- line change (SPEC v1.81) ------------------------------------------------


def _ink(window, region: QRectF) -> QRect | None:
    """Bounding box of the visible text inside `region` of the window."""
    image = window.grab().toImage()
    box = region.toAlignedRect().intersected(image.rect())
    xs, ys = [], []
    for y in range(box.top(), box.bottom() + 1):
        for x in range(box.left(), box.right() + 1):
            if image.pixelColor(x, y).alpha() > 60:
                xs.append(x)
                ys.append(y)
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else None


def _change_at(window, t):
    window._change.stop()
    window._change_t = t


@pytest.mark.parametrize("translations", [None, ("tr one", "tr two", "tr three")])
def test_line_change_never_blanks_the_window(window, translations):
    # SPEC v1.81: the 150 ms fade went fully transparent halfway through.
    window.resize(600, window.height())
    window.set_lyrics(TRACK, LINES, translations=translations)
    window.show_line(0, animate=False)
    window.show_line(1, animate=True)
    for t in (0.0, 0.25, 0.5, 0.75):
        _change_at(window, t)
        assert _ink(window, QRectF(window.rect())) is not None, t


def test_next_line_moves_up_into_place(window):
    assert LyricWindow._pushes(DisplayText("first", "second", NEXT, 0), DisplayText("second", "third", NEXT, 1))
    assert LyricWindow._pushes(DisplayText("intro", "first", NEXT), DisplayText("first", "second", NEXT, 0))
    assert not LyricWindow._pushes(DisplayText("first", "tr", TRANSLATION, 0), DisplayText("second", "tr2", TRANSLATION, 1))
    assert not LyricWindow._pushes(DisplayText("first", "second", NEXT, 0), DisplayText("third", "", NEXT, 2))


def test_moving_line_starts_where_the_next_line_stood(window):
    window.resize(600, window.height())
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    bottom = window._line_rects()[1]
    before = _ink(window, bottom)
    window.show_line(1, animate=True)
    _change_at(window, 0.0)
    start = _ink(window, bottom)
    assert before is not None and start is not None
    for a, b in ((before.left(), start.left()), (before.right(), start.right()), (before.center().y(), start.center().y())):
        assert abs(a - b) <= 2


def test_leaving_line_keeps_its_marquee_position(window):
    window.resize(300, window.height())
    window.set_lyrics(TRACK, (LyricLine(1.0, "a very long line " * 12), LyricLine(5.0, "short")))
    window.show_line(0, animate=False)
    window._scroll[0] = 120.0
    window.show_line(1, animate=True)
    assert window._prev_scroll[0] == 120.0
    assert window._scroll[0] == 0.0


def test_next_change_is_drawn_ahead(window):
    # Drawing a line takes 10-50 ms: done after this change, not as the next one starts.
    window.move(-5000, -5000)
    window.show()
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    window._sprites.clear()
    window._warm_next()
    assert {key[0] for key in window._sprites} == {"second line", "third line"}
