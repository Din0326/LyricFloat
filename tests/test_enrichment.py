import asyncio
from dataclasses import replace

import pytest

from lyricfloat.lyrics.cache import SqliteCache
from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver, visibly_changed, LyricService
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, ResultKind, TrackInfo

PIPE = OpenCCPipeline(converter=ChineseConverter())
EN_TRACK = TrackInfo("Paper Boats", "The Example Band", "Harbor", 200.0, "Spotify.exe")
ZH_TRACK = TrackInfo("紙船", "範例樂團", "港口", 200.0, "Spotify.exe")

# Made-up English lines; the NetEase copy carries simplified-Chinese translations.
EN_LRC = "\n".join(
    f"[00:{s:02d}.00]{text}"
    for s, text in [(10, "we fold the paper boats"), (14, "and set them on the stream"), (18, "they drift beyond the harbor"), (22, "further than we dream")]
)
NE_LRC_SAME_TIME = EN_LRC.replace("[00:10.00]", "[00:10.30]").replace("[00:14.00]", "[00:14.20]")
NE_TLYRIC = "[00:10.30]我们折纸船\n[00:14.20]放进溪流里\n[00:18.00]它们漂过港口\n[00:22.00]比梦还远"
ZH_LRC = "[00:10.00]我們摺紙船\n[00:14.00]放進溪流裡\n[00:18.00]漂過港口\n[00:22.00]比夢還遠"


def shifted(lrc: str, seconds: int) -> str:
    out = []
    for line in lrc.splitlines():
        ss = int(line[4:6]) + seconds
        out.append(f"[00:{ss:02d}{line[6:]}")
    return "\n".join(out)


class MainSource(LyricSource):
    """LRCLIB-like: lyrics inline, not simplified."""

    name = "lrclib"

    def __init__(self, lrc=EN_LRC, duration=200.0, title="Paper Boats"):
        self.cand = Candidate("lrclib", "7", title, "The Example Band", "Harbor", duration, lrc, None)

    async def search(self, track):
        return [self.cand]


class FakeNetease(LyricSource):
    name = "netease"
    simplified = True
    lyrics_inline = False

    def __init__(self, lrc=NE_LRC_SAME_TIME, tlyric=NE_TLYRIC, duration=200.0, fail=False, found=True):
        self.lrc, self.tlyric, self.duration, self.fail, self.found = lrc, tlyric, duration, fail, found
        self.calls = 0

    async def search(self, track):
        self.calls += 1
        if self.fail:
            raise SourceError("netease down")
        if not self.found:
            return []
        return [Candidate("netease", "99", track.title, track.artist, "", self.duration)]

    async def fetch_lyrics(self, c):
        return replace(c, synced_lrc=self.lrc, translation_lrc=self.tlyric)


def resolver(*sources, cache=None):
    return LyricResolver(list(sources), cache=cache, pipeline=PIPE)


# --- query order -------------------------------------------------------------


def test_order_follows_language_table():
    netease, lrclib = FakeNetease(), MainSource()
    r = resolver(netease, lrclib)
    assert [s.name for s in r.order_sources(EN_TRACK)] == ["lrclib", "netease"]
    assert [s.name for s in r.order_sources(ZH_TRACK)] == ["lrclib", "netease"]


def test_unknown_sources_go_last():
    class Extra(MainSource):
        name = "extra"

    r = resolver(Extra(), FakeNetease(), MainSource())
    assert [s.name for s in r.order_sources(EN_TRACK)] == ["lrclib", "netease", "extra"]


# --- translation ----------------------------------------------------------------


async def test_english_song_gets_converted_translations():
    r = resolver(MainSource(), FakeNetease())
    result = await r.resolve(EN_TRACK)
    assert result.lyrics.translations is None  # first pass shows the lyrics without waiting
    enriched = await r.enrich(result)
    assert visibly_changed(result, enriched)
    assert enriched.lyrics.translations == ("我們摺紙船", "放進溪流裡", "它們漂過港口", "比夢還遠")
    assert enriched.candidate.source == "lrclib"  # timelines agree: no L1 switch


async def test_chinese_song_skips_translation_and_netease():
    netease = FakeNetease()
    r = resolver(MainSource(lrc=ZH_LRC, title="紙船"), netease)
    result = await r.resolve(ZH_TRACK)
    assert not visibly_changed(result, await r.enrich(result))
    assert netease.calls == 0


async def test_language_is_rejudged_from_lyrics():
    # Chinese-looking metadata, English lyrics: still gets the translation.
    netease = FakeNetease()
    track = TrackInfo("紙船", "The Example Band", "", 200.0)
    r = resolver(MainSource(title="紙船"), netease)
    enriched = await r.enrich(await r.resolve(track))
    assert enriched.lyrics.has_translation


async def test_main_lyrics_from_netease_pair_by_timestamp():
    netease = FakeNetease(lrc=EN_LRC, tlyric="[00:10.00]我们折纸船\n[00:18.00]它们漂过港口")
    r = resolver(netease)
    result = await r.resolve(EN_TRACK)
    assert result.candidate.source == "netease"
    enriched = await r.enrich(result)
    assert enriched.lyrics.translations == ("我們摺紙船", None, "它們漂過港口", None)
    assert netease.calls == 1  # no second lookup for the translation


async def test_no_netease_match_means_no_translation():
    r = resolver(MainSource(), FakeNetease(found=False))
    result = await r.resolve(EN_TRACK)
    assert not visibly_changed(result, await r.enrich(result))


async def test_netease_disabled_means_no_translation():
    r = resolver(MainSource())
    result = await r.resolve(EN_TRACK)
    assert not visibly_changed(result, await r.enrich(result))


async def test_translation_failure_is_retried_next_play(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    netease = FakeNetease(fail=True)
    r = resolver(MainSource(), netease, cache=cache)
    result = await r.resolve(EN_TRACK)
    assert not visibly_changed(result, await r.enrich(result))
    cached = await cache.get(EN_TRACK)
    assert cached.result.lyrics.translations is None  # still unchecked
    netease.fail = False
    again = await r.resolve(EN_TRACK)
    assert r.needs_enrichment(again)
    assert (await r.enrich(again)).lyrics.has_translation
    cache.close()


async def test_translations_are_cached(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    netease = FakeNetease()
    r = resolver(MainSource(), netease, cache=cache)
    await r.enrich(await r.resolve(EN_TRACK))
    hit = await r.resolve(EN_TRACK)
    assert hit.from_cache
    assert hit.lyrics.translations == ("我們摺紙船", "放進溪流裡", "它們漂過港口", "比夢還遠")
    assert not r.needs_enrichment(hit)
    cache.close()


async def test_checked_without_translation_is_not_checked_again(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    netease = FakeNetease()
    r = resolver(MainSource(lrc=ZH_LRC, title="紙船"), netease, cache=cache)
    await r.enrich(await r.resolve(ZH_TRACK))
    hit = await r.resolve(ZH_TRACK)
    assert hit.lyrics.translations == ()
    assert not r.needs_enrichment(hit)
    cache.close()


async def test_mode_change_reconverts_cached_translations(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    cc = ChineseConverter()
    tlyric = "[00:10.30]软件更新"
    first = LyricResolver([MainSource(), FakeNetease(tlyric=tlyric)], cache=cache, pipeline=OpenCCPipeline("s2twp", converter=cc))
    await first.enrich(await first.resolve(EN_TRACK))
    second = LyricResolver([MainSource(), FakeNetease()], cache=cache, pipeline=OpenCCPipeline("s2tw", converter=cc))
    hit = await second.resolve(EN_TRACK)
    assert hit.lyrics.translations[0] == "軟件更新"
    cache.close()


# L1 consensus tests live in test_consensus.py (SPEC v1.9 replaced the duration rule).


# --- service: first result fast, translation follows ---------------------------------


async def test_service_emits_lyrics_then_translation():
    service = LyricService(resolver(MainSource(), FakeNetease()))
    received = []
    service.resultReady.connect(received.append)
    service.request(EN_TRACK)
    await asyncio.sleep(0.2)
    assert len(received) == 2
    assert received[0].lyrics.translations is None
    assert received[1].lyrics.has_translation


async def test_service_emits_once_for_chinese_songs():
    service = LyricService(resolver(MainSource(lrc=ZH_LRC, title="紙船"), FakeNetease()))
    received = []
    service.resultReady.connect(received.append)
    service.request(ZH_TRACK)
    await asyncio.sleep(0.2)
    assert len(received) == 1
