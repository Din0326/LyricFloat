"""SPEC v1.26: L3 gives times to plain lyrics, including untimed edited versions. Placeholder text only."""

import asyncio

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.align.apply import MIN_CONFIDENCE, plan_plain
from lyricfloat.lyrics.cache import EDITED_SOURCE, L3_APPLIED, SqliteCache
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.lyrics.sources.base import LyricSource
from lyricfloat.models import Candidate, LyricLine, ResultKind, TrackInfo
from lyricfloat.ui import editor_dialog as editor_module
from lyricfloat.ui.editor_dialog import TIME, UNTIMED_NOTE, EditorDialog

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 60.0)
TEXTS = [f"plain line {i}" for i in range(6)]
PLAIN = tuple(LyricLine(0.0, t) for t in TEXTS)


def seg(start, p=0.6, text="plain line x"):
    if start is None:
        return {"start": None, "end": None, "words": []}
    words = text.split()
    return {"start": start, "end": start + 0.9, "words": [{"start": start + 0.3 * k, "end": start + 0.3 * (k + 1), "text": " " + w, "p": p} for k, w in enumerate(words)]}


# -- plan -----------------------------------------------------------------------


def test_sure_lines_get_their_aligned_times():
    p = plan_plain(PLAIN, [seg(5.0 + 4 * i, text=t) for i, t in enumerate(TEXTS)], 0.0, 60.0)
    assert p.usable and p.starts == pytest.approx([5.0 + 4 * i for i in range(6)])
    assert all(w is not None for w in p.words) and p.stats["plain"]


def test_unsure_lines_sit_between_their_neighbours():
    segs = [seg(5.0, text=TEXTS[0]), seg(9.0, text=TEXTS[1]), seg(30.0, p=MIN_CONFIDENCE / 2), seg(17.0, text=TEXTS[3]), seg(None), seg(25.0, text=TEXTS[5])]
    p = plan_plain(PLAIN, segs, 0.0, 60.0)
    assert p.usable and p.stats["sure"] == 4
    assert p.starts[2] == pytest.approx(13.0)  # halfway between 9 and 17, not the unsure 30
    assert p.starts[4] == pytest.approx(21.0)
    assert p.words[2] is None and p.words[4] is None


def test_lines_before_or_after_the_sure_ones_use_their_spacing():
    segs = [seg(None), seg(10.0, text=TEXTS[1]), seg(14.0, text=TEXTS[2]), seg(18.0, text=TEXTS[3]), seg(22.0, text=TEXTS[4]), seg(None)]
    p = plan_plain(PLAIN, segs, 0.0, 60.0)
    assert p.starts[0] == pytest.approx(6.0) and p.starts[5] == pytest.approx(26.0)


def test_too_few_sure_lines_leave_the_lyrics_untimed():
    segs = [seg(5.0, text=TEXTS[0])] + [seg(None)] * 5
    p = plan_plain(PLAIN, segs, 0.0, 60.0)
    assert not p.usable and p.starts == tuple(0.0 for _ in PLAIN)


def test_blank_lines_are_not_judged():
    lines = PLAIN + (LyricLine(0.0, ""),)
    p = plan_plain(lines, [seg(5.0 + 4 * i, text=t) for i, t in enumerate(TEXTS)] + [seg(None)], 0.0, 60.0)
    assert p.usable and p.stats["judged"] == 6
    assert list(p.starts) == sorted(p.starts)


# -- storing and showing ------------------------------------------------------------


class PlainSource(LyricSource):
    name = "inline"

    async def search(self, track):
        return [Candidate("inline", "p1", TRACK.title, TRACK.artist, TRACK.album, 60.0, None, "\n".join(TEXTS))]


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "cache.sqlite3")
    yield c
    c.close()


def aligned_segments():
    return [seg(5.0 + 4 * i, text=t) for i, t in enumerate(TEXTS)]


async def test_plain_lyrics_show_synced_once_aligned(cache):
    resolver = LyricResolver([PlainSource()], cache=cache)
    first = await resolver.resolve(TRACK)
    assert first.kind is ResultKind.PLAIN_ONLY and not first.shown_lyrics().synced
    judged, shown = await resolver.store_alignment(TRACK, first.lyrics.lines, aligned_segments(), 0.0, 60.0)
    assert judged.usable and shown.l3_state == L3_APPLIED
    again = await resolver.resolve(TRACK)
    assert again.kind is ResultKind.PLAIN_ONLY and [l.start for l in again.lyrics.lines] == [0.0] * 6  # source kept
    lyrics = again.shown_lyrics()
    assert lyrics.synced and [l.start for l in lyrics.lines] == pytest.approx([5.0 + 4 * i for i in range(6)])


async def test_untimed_edit_is_bound_plain_and_can_be_aligned(cache):
    resolver = LyricResolver([PlainSource()], cache=cache)
    await resolver.resolve(TRACK)
    edited = await resolver.save_edit_plain(TRACK, ["typed one", "", "typed two", "typed three"])
    assert edited.kind is ResultKind.PLAIN_ONLY and edited.bound and edited.candidate.source == EDITED_SOURCE
    again = await resolver.resolve(TRACK)
    assert [l.text for l in again.lyrics.lines] == ["typed one", "typed two", "typed three"] and again.l3_state is None
    segs = [seg(4.0, text="typed one"), seg(8.0, text="typed two"), seg(12.0, text="typed three")]
    judged, shown = await resolver.store_alignment(TRACK, again.lyrics.lines, segs, 0.0, 60.0)
    assert judged.usable and shown.shown_lyrics().synced
    assert await resolver.revert_edit(TRACK)
    assert [l.text for l in (await resolver.resolve(TRACK)).lyrics.lines] == TEXTS


async def test_service_shows_an_untimed_edit_at_once(cache):
    service = LyricService(LyricResolver([PlainSource()], cache=cache))
    shown = []
    service.resultReady.connect(shown.append)
    service.request(TRACK)
    await asyncio.sleep(0.05)
    await service.save_edit_plain(TRACK, ["typed one", "typed two"])
    assert shown[-1].kind is ResultKind.PLAIN_ONLY and shown[-1].bound


# -- editor ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Backend:
    def __init__(self):
        self.saved, self.plain = [], []

    def position(self):
        return 3.0

    async def save(self, lines, translations):
        self.saved.append(list(lines))

    async def save_plain(self, texts):
        self.plain.append(list(texts))

    async def revert(self):
        pass


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_pasted_lyrics_are_saved_untimed(qapp, monkeypatch):
    monkeypatch.setattr(editor_module.QInputDialog, "getMultiLineText", lambda *a, **k: ("first\n\n second \nthird\n", True))
    backend = Backend()
    d = EditorDialog(backend, TRACK, [LyricLine(1.0, "old")])
    d.paste_lyrics()
    assert d.untimed and d.status.text() == UNTIMED_NOTE
    assert [l.text for l in d.lines()[0]] == ["first", "second", "third"]
    assert [l.start for l in d.lines()[0]] == pytest.approx([0.0, 20.0, 40.0])  # spread over the song
    d.start_save()
    await settle()
    assert backend.plain == [["first", "second", "third"]] and backend.saved == []
    d.close()


async def test_changing_a_time_makes_it_a_timed_edit(qapp, monkeypatch):
    monkeypatch.setattr(editor_module.QInputDialog, "getMultiLineText", lambda *a, **k: ("first\nsecond", True))
    backend = Backend()
    d = EditorDialog(backend, TRACK, [])
    d.paste_lyrics()
    d.table.item(1, TIME).setText("0:12.00")
    assert not d.untimed
    d.start_save()
    await settle()
    assert backend.plain == [] and [l.start for l in backend.saved[0]] == [0.0, 12.0]
    d.close()


def test_cancelled_paste_changes_nothing(qapp, monkeypatch):
    monkeypatch.setattr(editor_module.QInputDialog, "getMultiLineText", lambda *a, **k: ("ignored", False))
    d = EditorDialog(Backend(), TRACK, [LyricLine(1.0, "kept")])
    d.paste_lyrics()
    assert [l.text for l in d.lines()[0]] == ["kept"] and not d.untimed
    d.close()
