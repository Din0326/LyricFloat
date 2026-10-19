"""FR-12: manual search, binding and unbinding. Lyrics are made-up placeholder text."""

import asyncio

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.lyrics.cache import SqliteCache
from lyricfloat.lyrics.scoring import ScoredCandidate
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo
from lyricfloat.ui.search_dialog import LYRICS_COLUMN, SearchDialog, duration_text, match_text, preview_text

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
AUTO = "[00:01.00]auto one\n[00:05.00]auto two"
PICKED = "[00:02.00]picked one\n[00:06.00]picked two"


def cand(cid, synced=AUTO, title="Paper Lanterns", duration=200.0, source="inline", plain=None):
    return Candidate(source, cid, title, "The Example Band", "Lights", duration, synced, plain)


class InlineSource(LyricSource):
    name = "inline"

    def __init__(self, found=(), fail=False):
        self.found = list(found)
        self.fail = fail
        self.searches = 0

    async def search(self, track):
        self.searches += 1
        if self.fail:
            raise SourceError("inline: down")
        return self.found


class LazySource(LyricSource):
    """Lists hits without lyrics, like NetEase."""

    name = "lazy"
    lyrics_inline = False

    def __init__(self, found=(), lyrics=None):
        self.found = list(found)
        self.lyrics = lyrics or {}
        self.fetches = 0

    async def search(self, track):
        return self.found

    async def fetch_lyrics(self, c):
        self.fetches += 1
        return Candidate(c.source, c.source_id, c.title, c.artist, c.album, c.duration, self.lyrics.get(c.source_id))


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "cache.sqlite3")
    yield c
    c.close()


# -- resolver -------------------------------------------------------------------


async def test_manual_search_lists_every_version_best_first():
    inline = InlineSource([cand("far", duration=230.0), cand("plain", synced=None, plain="a line"), cand("good")])
    lazy = LazySource([cand("n1", synced=None, source="lazy")])
    hits, errors = await LyricResolver([inline, lazy]).search_manual(TRACK, "Paper Lanterns", "The Example Band")
    assert errors == []
    assert [(s, sc.candidate.source_id) for s, sc in hits] == [("inline", "good"), ("inline", "plain"), ("lazy", "n1"), ("inline", "far")]
    assert hits[-1][1].rejected == "duration"  # listed, the user decides


async def test_manual_search_uses_the_typed_title_and_reports_failures():
    inline = InlineSource([cand("x", title="Different Name")])
    hits, errors = await LyricResolver([inline, InlineSource(fail=True)]).search_manual(TRACK, " Different Name ", "")
    assert hits[0][1].title_sim == pytest.approx(1.0)
    assert errors == ["inline: down"]


async def test_bind_replaces_the_cached_version_and_resets_offset(cache):
    inline = InlineSource([cand("auto")])
    resolver = LyricResolver([inline], cache=cache)
    await resolver.resolve(TRACK)
    await resolver.set_offset(TRACK, 0.7)
    bound = await resolver.bind(TRACK, "inline", cand("picked", synced=PICKED))
    assert bound.bound and bound.timeline_checked
    assert [line.text for line in bound.lyrics.lines] == ["picked one", "picked two"]
    again = await resolver.resolve(TRACK)
    assert again.from_cache and again.bound and again.offset == 0.0
    assert again.candidate.source_id == "picked"
    assert inline.searches == 1  # served from the binding, no new search


async def test_lazy_version_is_fetched_once_for_binding(cache):
    lazy = LazySource(lyrics={"n1": PICKED})
    resolver = LyricResolver([lazy], cache=cache)
    filled = await resolver.fill("lazy", cand("n1", synced=None, source="lazy"))
    await resolver.bind(TRACK, "lazy", filled)
    assert lazy.fetches == 1


async def test_version_without_lyrics_cannot_be_bound(cache):
    resolver = LyricResolver([LazySource()], cache=cache)
    with pytest.raises(SourceError):
        await resolver.bind(TRACK, "lazy", cand("empty", synced=None, source="lazy"))
    assert await cache.get(TRACK) is None


async def test_automatic_results_never_overwrite_a_binding(cache):
    resolver = LyricResolver([InlineSource([cand("auto")])], cache=cache)
    await resolver.bind(TRACK, "inline", cand("picked", synced=PICKED))
    auto = LyricResult(TRACK, ResultKind.SYNCED, Lyrics((LyricLine(1.0, "auto one"),), True), cand("auto"), 0.99)
    await cache.put(auto, AUTO, False, None)
    assert (await cache.get(TRACK)).result.candidate.source_id == "picked"


async def test_bound_version_skips_the_timeline_consensus(cache):
    resolver = LyricResolver([InlineSource([cand("auto"), cand("other", synced="[00:09.00]auto one\n[00:13.00]auto two")])], cache=cache)
    await resolver.bind(TRACK, "inline", cand("picked", synced=PICKED))
    cached = await resolver.resolve(TRACK)
    enriched = await resolver.enrich(cached)
    assert enriched.candidate.source_id == "picked" and "l1_switched" not in enriched.notes


async def test_unbind_goes_back_to_automatic_matching(cache):
    inline = InlineSource([cand("auto")])
    resolver = LyricResolver([inline], cache=cache)
    await resolver.bind(TRACK, "inline", cand("picked", synced=PICKED))
    await resolver.unbind(TRACK)
    result = await resolver.resolve(TRACK)
    assert not result.bound and result.candidate.source_id == "auto"


async def test_binding_a_song_not_found_earlier_clears_the_session_miss(cache):
    resolver = LyricResolver([InlineSource([])], cache=cache)
    assert (await resolver.resolve(TRACK)).kind is ResultKind.NOT_FOUND
    await resolver.bind(TRACK, "inline", cand("picked", synced=PICKED))
    assert (await resolver.resolve(TRACK)).bound


# -- service --------------------------------------------------------------------


async def test_service_shows_a_binding_for_the_current_song(cache):
    service = LyricService(LyricResolver([InlineSource([cand("auto")])], cache=cache))
    shown = []
    service.resultReady.connect(shown.append)
    service.request(TRACK)
    await asyncio.sleep(0.05)
    await service.bind(TRACK, "inline", cand("picked", synced=PICKED))
    assert shown[-1].bound and shown[-1].candidate.source_id == "picked"


async def test_service_binding_another_song_is_stored_quietly(cache):
    service = LyricService(LyricResolver([InlineSource([cand("auto")])], cache=cache))
    shown = []
    service.resultReady.connect(shown.append)
    other = TrackInfo("Another Song", "The Example Band", "", 180.0)
    service.request(other)
    await asyncio.sleep(0.05)
    shown.clear()
    await service.bind(TRACK, "inline", cand("picked", synced=PICKED))
    assert shown == []
    assert (await cache.get(TRACK)).result.bound


# -- dialog ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def scored(c, score=0.95, rejected=None):
    return ScoredCandidate(c, score, 1.0, 1.0, 1.0, rejected)


class FakeBackend:
    def __init__(self, hits, errors=()):
        self.hits = hits
        self.errors = list(errors)
        self.bound = []
        self.unbound = 0

    async def search(self, title, artist):
        return self.hits, self.errors

    async def preview(self, source, c):
        filled = Candidate(c.source, c.source_id, c.title, c.artist, c.album, c.duration, PICKED)
        lyrics = Lyrics((LyricLine(2.0, "picked one"), LyricLine(6.0, "picked two")), True)
        return filled, LyricResult(TRACK, ResultKind.SYNCED, lyrics, filled)

    async def bind(self, source, c, score):
        self.bound.append((source, c.source_id, c.synced_lrc))
        return LyricResult(TRACK, ResultKind.SYNCED, None, c, score, bound=True)

    async def unbind(self):
        self.unbound += 1


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_cell_texts():
    assert duration_text(205.0, 200.0) == "3:25（+5.0s）"
    assert duration_text(200.0, 200.0) == "3:20"
    assert duration_text(0.0, 200.0) == "—"
    assert match_text(scored(cand("x"), 0.5, "duration")) == "50%（時長不符）"
    assert match_text(scored(cand("x"), 0.5, "artist")) == "50%（歌手不符）"
    assert preview_text(None) == "這個版本沒有歌詞。"


async def test_dialog_search_preview_and_bind(qapp):
    current = LyricResult(TRACK, ResultKind.SYNCED, None, cand("auto"), 0.9)
    backend = FakeBackend([("lazy", scored(cand("n1", synced=None, source="lazy"))), ("inline", scored(cand("auto")))], ["lazy: down"])
    dialog = SearchDialog(backend, TRACK, TRACK, current)
    assert dialog.title.text() == "Paper Lanterns" and not dialog.unbind_button.isEnabled()
    dialog.start_search()
    await settle()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(1, 0).text() == "inline（目前）"
    assert "lazy: down" in dialog.status.text()
    assert dialog.table.item(0, LYRICS_COLUMN).text() == "點選後載入"

    dialog.table.selectRow(0)
    await settle()
    assert dialog.preview.toPlainText().startswith("[0:02.0] picked one")
    assert dialog.table.item(0, LYRICS_COLUMN).text() == "同步"
    dialog.start_bind()
    await settle()
    assert backend.bound == [("lazy", "n1", PICKED)]  # the fetched lyrics, not fetched again
    assert dialog.table.item(0, 0).text() == "lazy（已綁定）"
    assert dialog.unbind_button.isEnabled()

    dialog.start_unbind()
    await settle()
    assert backend.unbound == 1 and not dialog.unbind_button.isEnabled()
    dialog.close()


async def test_dialog_is_prefilled_with_the_terms_and_named_after_the_song(qapp):
    # SPEC v1.76: a video's found song goes in the fields; the dialog still names what is playing.
    terms = TrackInfo("Paper Lanterns (Remastered)", "The Example Band & Friend", "", 200.0)
    dialog = SearchDialog(FakeBackend([]), TRACK, TRACK, terms=terms)
    assert (dialog.title.text(), dialog.artist.text()) == ("Paper Lanterns (Remastered)", "The Example Band & Friend")
    assert dialog.windowTitle() == f"搜尋歌詞 — {TRACK.describe()}"
    dialog.close()


async def test_empty_title_is_not_searched(qapp):
    dialog = SearchDialog(FakeBackend([]), TRACK, TRACK)
    dialog.title.setText("  ")
    dialog.start_search()
    assert dialog.status.text() == "請輸入歌名。"
    dialog.close()
