"""L1 timeline consensus (SPEC v1.9). All lyric lines are made up."""

from dataclasses import replace

import pytest

from lyricfloat.lyrics.cache import SqliteCache
from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver, visibly_changed
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, TrackInfo

PIPE = OpenCCPipeline(converter=ChineseConverter())
TRACK = TrackInfo("紙船", "範例樂團", "港口", 200.0, "Spotify.exe")
TEXTS = ["我們摺紙船", "放進溪流裡", "漂過港口", "比夢還遠", "天色漸暗", "風吹起來"]


def lrc(shift: float = 0.0, texts=TEXTS) -> str:
    lines = []
    for i, text in enumerate(texts):
        t = 10 + i * 4 + shift
        lines.append(f"[{int(t // 60):02d}:{t % 60:05.2f}]{text}")
    return "\n".join(lines)


def version(vid: str, shift: float, artist: str = "範例樂團", title: str = "紙船") -> Candidate:
    return Candidate("lrclib", vid, title, artist, "港口", 200.0, lrc(shift), None)


class Lrclib(LyricSource):
    name = "lrclib"

    def __init__(self, versions, exact=None, search_fails=False):
        self.versions = list(versions)
        self.exact = exact
        self.search_fails = search_fails
        self.searches = 0

    async def lookup(self, track):
        return self.exact

    async def search(self, track):
        self.searches += 1
        if self.search_fails:
            raise SourceError("search down")
        return self.versions


class Netease(LyricSource):
    name = "netease"
    simplified = True
    lyrics_inline = False

    def __init__(self, shift=0.0, fail=False):
        self.shift = shift
        self.fail = fail
        self.calls = 0

    async def search(self, track):
        self.calls += 1
        if self.fail:
            raise SourceError("netease down")
        return [Candidate("netease", "99", track.title, track.artist, "", 200.0)]

    async def fetch_lyrics(self, c):
        return replace(c, synced_lrc=lrc(self.shift))


def resolver(*sources, cache=None):
    return LyricResolver(list(sources), cache=cache, pipeline=PIPE)


# Two versions tie on metadata; the one listed first (picked first) runs 3 s early.
EARLY_BEST = version("early", -3.0)
RIGHT_ALT = version("right", 0.0, title="紙船 (Remastered)")


async def test_majority_with_netease_switches_versions():
    netease = Netease(shift=0.2)  # agrees with the right version within 1 s
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), netease)
    first = await r.resolve(TRACK)
    assert first.candidate.source_id == "early"  # metadata alone picks the wrong one
    assert [c.source_id for c, _ in first.alternatives] == ["right"]
    fixed = await r.enrich(first)
    assert visibly_changed(first, fixed)
    assert fixed.candidate.source_id == "right"
    assert "l1_switched" in fixed.notes
    assert fixed.lyrics.lines[0].start == pytest.approx(10.0)
    assert fixed.timeline_checked
    assert netease.calls == 1


async def test_netease_wins_when_it_scores_higher_than_the_agreeing_version():
    weak_alt = version("right", 0.0, artist="範例樂團 Example Band")  # lower artist score
    r = resolver(Lrclib([EARLY_BEST, weak_alt]), Netease(shift=0.2))
    fixed = await r.enrich(await r.resolve(TRACK))
    assert fixed.candidate.source == "netease"
    assert fixed.lyrics.lines[0].text == "我們摺紙船"  # converted to traditional


async def test_netease_agreeing_with_the_current_version_keeps_it():
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), Netease(shift=-3.0))
    first = await r.resolve(TRACK)
    assert not visibly_changed(first, await r.enrich(first))


async def test_tie_without_netease_keeps_the_best_scoring_version():
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]))  # NetEase disabled: one vote each
    first = await r.resolve(TRACK)
    assert not visibly_changed(first, await r.enrich(first))


async def test_netease_failure_leaves_the_check_for_next_time(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    netease = Netease(shift=0.0, fail=True)
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), netease, cache=cache)
    first = await r.resolve(TRACK)
    assert not visibly_changed(first, await r.enrich(first))
    assert not (await cache.get(TRACK)).result.timeline_checked
    netease.fail = False
    again = await r.resolve(TRACK)
    assert r.needs_enrichment(again)
    assert (await r.enrich(again)).candidate.source_id == "right"
    cache.close()


async def test_agreeing_versions_never_ask_netease():
    netease = Netease()
    r = resolver(Lrclib([version("a", 0.0), version("b", 0.3, artist="範例樂團 Example Band")]), netease)
    first = await r.resolve(TRACK)
    checked = await r.enrich(first)
    assert not visibly_changed(first, checked)
    assert netease.calls == 0


async def test_a_single_version_has_nothing_to_compare():
    netease = Netease(shift=-3.0)
    r = resolver(Lrclib([version("only", 0.0)]), netease)
    first = await r.resolve(TRACK)
    assert first.alternatives == ()
    assert not visibly_changed(first, await r.enrich(first))
    assert netease.calls == 0


async def test_exact_hit_searches_for_versions_in_the_background():
    lrclib = Lrclib([EARLY_BEST, RIGHT_ALT], exact=EARLY_BEST)
    r = resolver(lrclib, Netease(shift=0.0))
    first = await r.resolve(TRACK)
    assert first.alternatives is None and lrclib.searches == 0  # first line not delayed
    fixed = await r.enrich(first)
    assert lrclib.searches == 1
    assert fixed.candidate.source_id == "right"


async def test_version_search_failure_is_retried():
    lrclib = Lrclib([EARLY_BEST, RIGHT_ALT], exact=EARLY_BEST, search_fails=True)
    r = resolver(lrclib, Netease())
    first = await r.resolve(TRACK)
    assert not visibly_changed(first, await r.enrich(first))
    assert r.needs_enrichment(first)


async def test_manual_offset_takes_priority():
    netease = Netease(shift=0.0)
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), netease)
    first = replace(await r.resolve(TRACK), offset=3.0)
    assert not visibly_changed(first, await r.enrich(first))
    assert netease.calls == 0


async def test_cached_entry_from_before_the_check_is_fixed_on_next_play(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    # Simulate an entry stored before SPEC v1.9: wrong version, translations already checked.
    old = resolver(Lrclib([EARLY_BEST]), cache=cache)
    stored = await old.resolve(TRACK)
    await cache.put(replace(stored, lyrics=replace(stored.lyrics, translations=())), stored.candidate.synced_lrc, False, PIPE.cache_key)
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), Netease(shift=0.0), cache=cache)
    hit = await r.resolve(TRACK)
    assert hit.from_cache and not hit.timeline_checked
    assert r.needs_enrichment(hit)
    fixed = await r.enrich(hit)
    assert fixed.candidate.source_id == "right"
    later = await r.resolve(TRACK)
    assert later.timeline_checked and later.lyrics.lines[0].start == pytest.approx(10.0)
    assert not r.needs_enrichment(later)
    cache.close()


async def test_offset_survives_a_switch(tmp_path):
    cache = SqliteCache(tmp_path / "c.sqlite3")
    r = resolver(Lrclib([EARLY_BEST, RIGHT_ALT]), Netease(shift=0.0), cache=cache)
    first = await r.resolve(TRACK)
    fixed = await r.enrich(first)
    assert fixed.offset == 0.0
    assert (await cache.get(TRACK)).result.candidate.source_id == "right"
    cache.close()
