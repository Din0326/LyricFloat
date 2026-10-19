import asyncio

import pytest

from lyricfloat.lyrics.cache import SqliteCache
from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.normalize import normalize_artist, normalize_title
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0, "Spotify.exe")
SYNCED = "[00:01.00]first line\n[00:05.00]second line"


def synced_result(track=TRACK, text=("first line", "second line")):
    lines = tuple(LyricLine(float(i * 4 + 1), t) for i, t in enumerate(text))
    cand = Candidate("lrclib", "42", "Paper Lanterns", "The Example Band", "Lights", 200.0, SYNCED, None)
    return LyricResult(track, ResultKind.SYNCED, Lyrics(lines, synced=True), cand, 0.98)


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "LyricFloat" / "cache.sqlite3")
    yield c
    c.close()


async def test_round_trip(cache):
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    entry = await cache.get(TRACK)
    assert entry.result.kind is ResultKind.SYNCED
    assert [l.text for l in entry.result.lyrics.lines] == ["first line", "second line"]
    assert entry.result.lyrics.synced
    assert entry.result.from_cache
    assert entry.result.candidate.source == "lrclib" and entry.result.candidate.source_id == "42"
    assert entry.raw_text == SYNCED
    assert entry.conversion_mode == "s2twp"
    assert entry.result.offset == 0.0


async def test_miss_on_unknown_song(cache):
    assert await cache.get(TRACK) is None


@pytest.mark.parametrize("duration, hit", [(201.9, True), (198.1, True), (202.5, False), (0.0, True)])
async def test_duration_tolerance(cache, duration, hit):
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    probe = TrackInfo(TRACK.title, TRACK.artist, TRACK.album, duration)
    assert (await cache.get(probe) is not None) is hit


async def test_version_notes_share_the_key(cache):
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    probe = TrackInfo("Paper Lanterns (2011 Remaster)", "The Example Band", "", 200.5, "Chrome")
    assert await cache.get(probe) is not None


async def test_two_versions_with_different_lengths_coexist(cache):
    live = TrackInfo("Paper Lanterns (Live)", "The Example Band", "Live", 260.0)
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    await cache.put(synced_result(live, ("live one", "live two")), SYNCED, False, "s2twp")
    studio = await cache.get(TRACK)
    live_hit = await cache.get(TrackInfo("Paper Lanterns", "The Example Band", "", 259.0))
    assert studio.result.lyrics.lines[0].text == "first line"
    assert live_hit.result.lyrics.lines[0].text == "live one"


async def test_simplified_and_traditional_metadata_share_the_key(tmp_path):
    pipe = OpenCCPipeline(converter=ChineseConverter())
    key = lambda t: (normalize_artist(pipe.display_text(t.artist)), normalize_title(pipe.display_text(t.title)))  # noqa: E731
    cache = SqliteCache(tmp_path / "c.sqlite3", key=key)
    simplified = TrackInfo("请让一下", "范例乐团", "", 180.0)
    traditional = TrackInfo("請讓一下", "範例樂團", "", 180.0)
    await cache.put(synced_result(simplified), SYNCED, False, "s2twp")
    assert await cache.get(traditional) is not None
    cache.close()


async def test_offset_is_saved_and_survives_refresh(cache):
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    assert await cache.set_offset(TRACK, 0.3)
    assert (await cache.get(TRACK)).result.offset == pytest.approx(0.3)
    await cache.put(synced_result(text=("new first", "new second")), SYNCED, False, "s2twp")
    entry = await cache.get(TRACK)
    assert entry.result.offset == pytest.approx(0.3)
    assert entry.result.lyrics.lines[0].text == "new first"


async def test_offset_on_uncached_song_is_rejected(cache):
    assert not await cache.set_offset(TRACK, 0.5)


async def test_instrumental_and_plain_are_cached_not_found_is_not(cache):
    inst = TrackInfo("Interlude", "The Example Band", "", 90.0)
    plain = TrackInfo("Unsynced Song", "The Example Band", "", 150.0)
    missing = TrackInfo("Nowhere", "The Example Band", "", 120.0)
    await cache.put(LyricResult(inst, ResultKind.INSTRUMENTAL), None, False, "s2twp")
    await cache.put(LyricResult(plain, ResultKind.PLAIN_ONLY, Lyrics((LyricLine(0, "a line"),), False)), "a line", False, "s2twp")
    await cache.put(LyricResult(missing, ResultKind.NOT_FOUND), None, False, "s2twp")
    assert (await cache.get(inst)).result.kind is ResultKind.INSTRUMENTAL
    assert not (await cache.get(plain)).result.lyrics.synced
    assert await cache.get(missing) is None


async def test_persists_across_reopen(tmp_path):
    path = tmp_path / "c.sqlite3"
    first = SqliteCache(path)
    await first.put(synced_result(), SYNCED, False, "s2twp")
    first.close()
    second = SqliteCache(path)
    assert await second.get(TRACK) is not None
    second.close()


async def test_corrupt_file_is_set_aside(tmp_path):
    path = tmp_path / "c.sqlite3"
    path.write_bytes(b"this is not a database at all" * 100)
    cache = SqliteCache(path)
    await cache.put(synced_result(), SYNCED, False, "s2twp")
    assert await cache.get(TRACK) is not None
    assert list(tmp_path.glob("c.sqlite3.corrupt-*"))
    cache.close()


# --- resolver with a real cache -------------------------------------------------


class CountingSource(LyricSource):
    name = "fake"

    def __init__(self, found=(), fail=False, simplified=False):
        self.found = list(found)
        self.fail = fail
        self.simplified = simplified
        self.calls = 0

    async def search(self, track):
        self.calls += 1
        if self.fail:
            raise SourceError("down")
        return self.found


def cand(lrc=SYNCED):
    return Candidate("fake", "1", "Paper Lanterns", "The Example Band", "Lights", 200.0, lrc, "first line")


async def test_second_play_is_served_from_cache(cache):
    src = CountingSource([cand()])
    resolver = LyricResolver([src], cache=cache)
    first = await resolver.resolve(TRACK)
    second = await resolver.resolve(TRACK)
    assert src.calls == 1
    assert not first.from_cache and second.from_cache
    assert [l.text for l in second.lyrics.lines] == [l.text for l in first.lyrics.lines]


async def test_cached_song_shows_offline(cache):
    await LyricResolver([CountingSource([cand()])], cache=cache).resolve(TRACK)
    offline = LyricResolver([CountingSource(fail=True)], cache=cache)
    assert (await offline.resolve(TRACK)).kind is ResultKind.SYNCED


async def test_plain_fallback_is_not_cached_when_a_source_failed(cache):
    plain = CountingSource([cand(lrc=None)])
    resolver = LyricResolver([plain, CountingSource(fail=True)], cache=cache)
    result = await resolver.resolve(TRACK)
    assert result.kind is ResultKind.PLAIN_ONLY and "source_failed" in result.notes
    assert await cache.get(TRACK) is None
    await resolver.resolve(TRACK)  # next play asks again
    assert plain.calls == 2


async def test_plain_fallback_is_cached_when_every_source_answered(cache):
    plain = CountingSource([cand(lrc=None)])
    resolver = LyricResolver([plain, CountingSource([])], cache=cache)
    assert (await resolver.resolve(TRACK)).kind is ResultKind.PLAIN_ONLY
    assert (await cache.get(TRACK)).result.kind is ResultKind.PLAIN_ONLY
    await resolver.resolve(TRACK)
    assert plain.calls == 1


async def test_not_found_is_remembered_for_the_session_only(cache):
    src = CountingSource([])
    resolver = LyricResolver([src], cache=cache)
    assert (await resolver.resolve(TRACK)).kind is ResultKind.NOT_FOUND
    again = await resolver.resolve(TRACK)
    assert again.kind is ResultKind.NOT_FOUND and "session_miss" in again.notes
    assert src.calls == 1
    fresh = LyricResolver([src], cache=cache)  # next app run
    await fresh.resolve(TRACK)
    assert src.calls == 2


async def test_source_failure_is_not_remembered(cache):
    src = CountingSource(fail=True)
    resolver = LyricResolver([src], cache=cache)
    assert (await resolver.resolve(TRACK)).kind is ResultKind.SOURCES_FAILED
    await resolver.resolve(TRACK)
    assert src.calls == 2


async def test_mode_change_rebuilds_from_source_text_without_network(cache):
    cc = ChineseConverter()
    lrc = "[00:01.00]软件更新\n[00:05.00]second line"
    src = CountingSource([cand(lrc)], simplified=True)
    await LyricResolver([src], cache=cache, pipeline=OpenCCPipeline("s2twp", converter=cc)).resolve(TRACK)
    resolver = LyricResolver([src], cache=cache, pipeline=OpenCCPipeline("s2tw", converter=cc))
    result = await resolver.resolve(TRACK)
    assert src.calls == 1
    assert result.lyrics.lines[0].text == "軟件更新"  # s2tw: characters only
    assert (await cache.get(TRACK)).conversion_mode == "s2tw"


async def test_offset_round_trip_through_resolver(cache):
    resolver = LyricResolver([CountingSource([cand()])], cache=cache)
    await resolver.resolve(TRACK)
    assert await resolver.set_offset(TRACK, -0.2)
    assert (await resolver.resolve(TRACK)).offset == pytest.approx(-0.2)
