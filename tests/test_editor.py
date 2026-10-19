"""FR-17 lyric editor: LRC writing, edited versions in the cache, revert, dialog. Placeholder text only."""

import asyncio

import pytest
from PyQt6.QtWidgets import QApplication, QTableWidgetSelectionRange

from lyricfloat.lyrics.cache import EDITED_SOURCE, SqliteCache
from lyricfloat.lyrics.lrc import format_lrc, format_time, parse_lrc, parse_time
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.lyrics.sources.base import LyricSource
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo
from lyricfloat.ui import editor_dialog as editor_module
from lyricfloat.ui.editor_dialog import TEXT, TIME, EditorDialog

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
AUTO = "[00:01.00]auto one\n[00:05.00]auto two"


class Source(LyricSource):
    name = "inline"

    def __init__(self, found=()):
        self.found = list(found)

    async def search(self, track):
        return self.found


def cand(cid="auto", lrc=AUTO):
    return Candidate("inline", cid, "Paper Lanterns", "The Example Band", "Lights", 200.0, lrc)


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "cache.sqlite3")
    yield c
    c.close()


# -- LRC ------------------------------------------------------------------------


@pytest.mark.parametrize("seconds, text", [(0, "0:00.00"), (5.3, "0:05.30"), (65.25, "1:05.25"), (600.004, "10:00.00")])
def test_format_time(seconds, text):
    assert format_time(seconds) == text


@pytest.mark.parametrize("text, seconds", [("1:05.3", 65.3), ("65.3", 65.3), ("1:05", 65.0), (" 0:07.25 ", 7.25), ("1:75", None), ("abc", None), ("", None)])
def test_parse_time(text, seconds):
    assert parse_time(text) == (pytest.approx(seconds) if seconds is not None else None)


def test_format_lrc_round_trips_in_time_order():
    lines = [LyricLine(65.25, "second"), LyricLine(1.5, "first")]
    text = format_lrc(lines)
    assert text.splitlines()[0] == "[00:01.50]first"
    parsed = parse_lrc(text).lyrics.lines
    assert [(l.start, l.text) for l in parsed] == [(1.5, "first"), (65.25, "second")]


# -- cache and resolver -----------------------------------------------------------


async def test_edit_is_bound_sorted_and_survives_automatic_results(cache):
    resolver = LyricResolver([Source([cand()])], cache=cache)
    await resolver.resolve(TRACK)
    await resolver.set_offset(TRACK, 0.4)
    edited = await resolver.save_edit(TRACK, [LyricLine(6.0, "edited two"), LyricLine(1.2, "edited one")], ["二", None])
    assert [l.text for l in edited.lyrics.lines] == ["edited one", "edited two"]
    assert edited.lyrics.translations == (None, "二")  # translations follow their lines
    again = await resolver.resolve(TRACK)
    assert again.bound and again.candidate.source == EDITED_SOURCE and again.offset == 0.0
    assert [l.start for l in again.lyrics.lines] == [1.2, 6.0]


async def test_revert_returns_to_the_version_before_the_first_edit(cache):
    resolver = LyricResolver([Source([cand()])], cache=cache)
    await resolver.resolve(TRACK)
    await resolver.set_offset(TRACK, 0.4)
    await resolver.save_edit(TRACK, [LyricLine(1.0, "first edit")])
    await resolver.save_edit(TRACK, [LyricLine(1.0, "second edit")])
    assert await resolver.revert_edit(TRACK)
    back = await resolver.resolve(TRACK)
    assert [l.text for l in back.lyrics.lines] == ["auto one", "auto two"]
    assert back.candidate.source == "inline" and back.offset == pytest.approx(0.4) and not back.bound
    assert not await resolver.revert_edit(TRACK)  # nothing edited any more


async def test_revert_of_an_edit_with_nothing_before_removes_it(cache):
    source = Source([cand()])
    resolver = LyricResolver([source], cache=cache)
    await resolver.save_edit(TRACK, [LyricLine(1.0, "typed by hand")])
    assert await resolver.revert_edit(TRACK)
    assert (await resolver.resolve(TRACK)).candidate.source == "inline"  # searched again


async def test_edit_does_not_inherit_old_translations(cache):
    resolver = LyricResolver([Source([cand()])], cache=cache)
    old = LyricResult(TRACK, ResultKind.SYNCED, Lyrics((LyricLine(1.0, "a"), LyricLine(5.0, "b")), True, ("甲", "乙")), cand())
    await cache.put(old, AUTO, False, None, ("甲", "乙"))
    await resolver.save_edit(TRACK, [LyricLine(1.0, "x"), LyricLine(5.0, "y")])
    assert (await cache.get(TRACK)).result.lyrics.translations is None


async def test_service_shows_an_edit_of_the_current_song(cache):
    service = LyricService(LyricResolver([Source([cand()])], cache=cache))
    shown = []
    service.resultReady.connect(shown.append)
    service.request(TRACK)
    await asyncio.sleep(0.05)
    await service.save_edit(TRACK, [LyricLine(2.0, "edited")])
    assert shown[-1].candidate.source == EDITED_SOURCE


# -- dialog ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBackend:
    def __init__(self, position=None):
        self.now = position
        self.saved = []
        self.reverted = 0

    def position(self):
        return self.now

    async def save(self, lines, translations):
        self.saved.append((list(lines), list(translations)))

    async def revert(self):
        self.reverted += 1


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


LINES = [LyricLine(1.0, "one"), LyricLine(5.0, "two"), LyricLine(9.0, "three")]


@pytest.fixture
def no_prompts(monkeypatch):
    answers = []
    monkeypatch.setattr(editor_module.QMessageBox, "question", lambda *a: answers.pop(0) if answers else editor_module.QMessageBox.StandardButton.Yes)
    return answers


def make(backend, lines=LINES, translations=("一", "二", "三"), edited=False):
    return EditorDialog(backend, TRACK, lines, translations, edited)


def test_typed_time_is_parsed_and_bad_input_is_refused(qapp, no_prompts):
    d = make(FakeBackend())
    d.table.item(1, TIME).setText("0:06.5")
    assert d.time_of(1) == pytest.approx(6.5) and d.table.item(1, TIME).text() == "0:06.50"
    d.table.item(1, TIME).setText("soon")
    assert d.time_of(1) == pytest.approx(6.5) and "不是時間" in d.status.text()
    d.close()


def test_shift_and_set_to_now(qapp, no_prompts):
    backend = FakeBackend(position=4.25)
    d = make(backend)
    d.table.selectRow(1)
    d.shift(0.1)
    assert d.time_of(1) == pytest.approx(5.1)
    d.set_to_now()
    assert d.time_of(1) == pytest.approx(4.25)
    assert d.selected_rows() == [2]  # moved on, ready to time the next line
    d.close()


def times(d):
    return [round(d.time_of(r), 3) for r in range(d.table.rowCount())]


def test_a_new_time_moves_every_line_by_as_much(qapp, no_prompts):
    # SPEC v1.78: ticked when the dialog opens; every way of changing a time follows it.
    d = make(FakeBackend(position=6.0))
    assert d.move_all.isChecked()
    d.table.item(1, TIME).setText("0:06.50")
    assert times(d) == [2.5, 6.5, 10.5]
    for row in (0, 2):
        d.table.setRangeSelected(QTableWidgetSelectionRange(row, 0, row, 1), True)
    assert d.selected_rows() == [0, 2]
    d.shift(0.1)
    assert times(d) == [2.6, 6.6, 10.6]  # once, however many lines are selected
    d.table.clearSelection()
    d.table.selectRow(1)
    d.set_to_now()
    assert times(d) == [2.0, 6.0, 10.0]
    d.table.selectRow(0)
    d.shift(-0.1)
    assert times(d) == [1.9, 5.9, 9.9]
    d.table.item(1, TIME).setText("0:03.00")  # 2.9 s earlier: the first line stops at 0
    assert times(d) == [0.0, 3.0, 7.0]
    d.close()


def test_unticked_only_that_line_moves(qapp, no_prompts):
    d = make(FakeBackend(position=4.25))
    d.move_all.setChecked(False)
    d.table.item(1, TIME).setText("0:06.50")
    assert times(d) == [1.0, 6.5, 9.0]
    d.table.selectRow(1)
    d.shift(0.1)
    d.set_to_now()
    assert times(d) == [1.0, 4.25, 9.0]
    d.close()


def test_add_and_delete_lines(qapp, no_prompts):
    d = make(FakeBackend())
    d.table.selectRow(0)
    d.add_line()
    assert d.table.rowCount() == 4 and d.time_of(1) == pytest.approx(2.0)
    d.table.item(1, TEXT).setText("inserted")
    d.table.selectRow(3)
    d.delete_lines()
    lines, translations = d.lines()
    assert [l.text for l in lines] == ["one", "inserted", "two"]
    assert translations == ["一", None, "二"]
    d.close()


async def test_save_hands_the_lines_over_and_enables_revert(qapp, no_prompts):
    backend = FakeBackend()
    d = make(backend)
    assert not d.revert_button.isEnabled()
    d.table.item(0, TEXT).setText("ONE")
    d.start_save()
    await settle()
    (lines, translations), = backend.saved
    assert lines[0].text == "ONE" and translations == ["一", "二", "三"]
    assert d.revert_button.isEnabled() and not d._dirty
    d.close()


async def test_revert_asks_first(qapp, no_prompts):
    backend = FakeBackend()
    d = make(backend, edited=True)
    no_prompts.append(editor_module.QMessageBox.StandardButton.No)
    d.start_revert()
    await settle()
    assert backend.reverted == 0
    d.start_revert()
    await settle()
    assert backend.reverted == 1


def test_closing_with_unsaved_changes_asks(qapp, no_prompts):
    d = make(FakeBackend())
    d.show()
    d.table.item(0, TEXT).setText("changed")
    no_prompts.append(editor_module.QMessageBox.StandardButton.No)
    d.close()
    assert d.isVisible()
    d.close()  # second answer: Yes, discard
    assert not d.isVisible()


def test_the_playing_line_is_tinted(qapp, no_prompts):
    backend = FakeBackend(position=6.0)
    d = make(backend)
    d._mark_playing()
    assert d._playing_row == 1
    backend.now = None
    d._mark_playing()
    assert d._playing_row == -1
    d.close()
