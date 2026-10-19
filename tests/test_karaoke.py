"""FR-11 karaoke fill: interpolation timing and drawing. Placeholder text only."""

from dataclasses import replace

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.models import LyricLine, TrackInfo
from lyricfloat.sync.karaoke import BREATH, DEFAULT_PACE, MIN_FILL, PACE_RANGE, KaraokeTimeline, fill_duration, song_pace, split_point, sung_units
from lyricfloat.ui.lyric_window import LyricWindow

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)


@pytest.mark.parametrize(
    "text, units",
    [("hello there world", 3), ("這是一句", 4), ("中文 mixed 句子", 5), ("ひらがな", 4), ("", 0), ("  ", 0)],
)
def test_sung_units(text, units):
    assert sung_units(text) == units


def test_fill_ends_a_breath_before_a_close_next_line():
    assert fill_duration("one two three four five six seven eight", 10.0, 12.0) == pytest.approx(2.0 - BREATH)
    assert fill_duration("one two three", 10.0, 10.2) == pytest.approx(0.2)  # too close for a breath


def test_fill_follows_the_song_pace():
    slow = fill_duration("一二三四五六七八", 0.0, 20.0, pace=0.5)
    fast = fill_duration("一二三四五六七八", 0.0, 20.0, pace=0.2)
    assert slow == pytest.approx(8 * 0.5 + 0.2) and fast == pytest.approx(8 * 0.2 + 0.2)


def spaced(gaps, text="一二三四五六七八"):
    lines, t = [], 0.0
    for gap in gaps:
        lines.append(LyricLine(t, text))
        t += gap
    lines.append(LyricLine(t, text))
    return lines


def test_song_pace_comes_from_the_tighter_line_spacings():
    fast = song_pace(spaced([2.4, 2.4, 2.5, 2.4, 6.0, 8.0]))  # 8 units every ~2.4 s, plus pauses
    slow = song_pace(spaced([5.0, 5.2, 5.0, 5.1, 9.0]))
    assert fast < slow
    assert fast == pytest.approx(0.3 * 0.75, abs=0.02)


def test_song_pace_defaults_and_limits():
    assert song_pace(spaced([3.0, 3.0])) == DEFAULT_PACE  # too few lines to judge
    assert song_pace(spaced([0.4] * 6)) == PACE_RANGE[0]


def test_fill_does_not_crawl_over_an_interlude():
    duration = fill_duration("short line", 10.0, 40.0)
    assert MIN_FILL <= duration < 3.0


def test_last_line_uses_the_estimate():
    assert fill_duration("a b c", 10.0, None) == fill_duration("a b c", 10.0, 999.0)


def test_fraction_over_a_line():
    tl = KaraokeTimeline([LyricLine(10.0, "one two three"), LyricLine(12.0, "next")])
    assert tl.fraction(0, 9.0) == 0.0
    end = tl.end_of(0)
    assert 10.0 < end <= 12.0
    assert tl.fraction(0, (10.0 + end) / 2) == pytest.approx(0.5)
    assert tl.fraction(0, end) == 1.0
    assert tl.fraction(0, None) == 0.0
    assert tl.fraction(5, 11.0) == 0.0


def test_split_point_skips_spaces():
    assert split_point("ab cd", 0.0) == (0, 0.0)
    assert split_point("ab cd", 0.5) == (3, 0.0)  # past "ab", the space is skipped
    assert split_point("ab cd", 0.625) == (3, pytest.approx(0.5))
    assert split_point("ab cd", 1.0) == (5, 0.0)


# -- window ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = LyricWindow()
    w.resize(800, 120)
    yield w
    w.close()


LINES = (LyricLine(10.0, "WWWWWWWWWW"), LyricLine(14.0, "next line"))


def sung_pixels(window):
    """(count, rightmost x) of pixels in the sung colour #4FC3F7."""
    image = window.grab().toImage()
    count, right = 0, -1
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            c = image.pixelColor(x, y)
            if c.alpha() > 200 and c.blue() > 220 and 170 < c.green() < 220 and c.red() < 120:
                count += 1
                right = max(right, x)
    return count, right


def test_fill_follows_the_clock(window):
    clock = {"t": 9.0}
    window.set_clock(lambda: clock["t"])
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    assert window.fill_of(window._text) == 0.0
    assert sung_pixels(window)[0] == 0
    end = window._karaoke.end_of(0)
    clock["t"] = 10.0 + (end - 10.0) / 2
    half = sung_pixels(window)
    clock["t"] = end
    full = sung_pixels(window)
    assert 0 < half[0] < full[0]
    assert half[1] < full[1]


def test_next_line_and_intro_are_never_filled(window):
    window.set_clock(lambda: 100.0)
    window.set_lyrics(TRACK, LINES)
    assert window.fill_of(window._text) == 0.0  # intro: title + first line
    window.show_line(0, animate=False)
    assert window.fill_of(window._text) == 1.0


def test_unsynced_lyrics_and_switched_off_karaoke_are_not_filled(window):
    window.set_clock(lambda: 100.0)
    window.set_lyrics(TRACK, LINES, unsynced=True)
    window.show_line(0, animate=False)
    assert window.fill_of(window._text) == 0.0
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    window.apply_style(replace(window.style_, karaoke=False))
    assert window.fill_of(window._text) == 0.0


def test_timer_runs_only_while_a_synced_line_shows(window):
    window.set_clock(lambda: 11.0)
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    assert window._karaoke_timer.isActive()
    window.show_waiting()
    assert not window._karaoke_timer.isActive()


def test_lines_are_drawn_once_and_redrawn_after_a_style_change(window):
    window.set_clock(lambda: 11.0)
    window.set_lyrics(TRACK, LINES)
    window.show_line(0, animate=False)
    window.grab()
    first = dict(window._sprites)
    window.grab()
    assert window._sprites == first  # same sprites reused frame to frame
    assert any(s.sung is not None for s in first.values())
    window.apply_style(replace(window.style_, sung_color="#FF0000"))
    assert window._sprites == {}


# -- word timings from L3 (FR-21) -------------------------------------------------

from lyricfloat.models import WordTiming  # noqa: E402


def test_word_timings_fill_each_word_in_its_own_time():
    lines = [LyricLine(10.0, "ab cd"), LyricLine(20.0, "next")]
    words = [(WordTiming(0, 2, 10.0, 11.0), WordTiming(3, 5, 12.0, 13.0)), None]
    tl = KaraokeTimeline(lines, words)
    assert tl.fraction(0, 10.5) == pytest.approx(0.25)
    assert tl.fraction(0, 11.5) == pytest.approx(0.5)  # between words: the first one stays full
    assert tl.fraction(0, 12.5) == pytest.approx(0.75)
    assert tl.fraction(0, 13.0) == 1.0 and tl.end_of(0) == 13.0
    assert tl.fraction(1, 20.0) == 0.0 and tl.end_of(1) > 20.0  # no words: interpolated


def test_word_timings_for_other_lines_are_ignored():
    lines = [LyricLine(10.0, "ab cd")]
    tl = KaraokeTimeline(lines, [None, None])
    assert tl.words == (None,)


def test_window_fill_uses_word_timings(window):
    lines = (LyricLine(10.0, "ab cd"), LyricLine(20.0, "next"))
    clock = {"t": 11.5}
    window.set_clock(lambda: clock["t"])
    window.set_lyrics(TRACK, lines, words=((WordTiming(0, 2, 10.0, 11.0), WordTiming(3, 5, 12.0, 13.0)), None))
    window.show_line(0, animate=False)
    assert window.fill_of(window._text) == pytest.approx(0.5)
