import asyncio

import pytest

from lyricfloat.lyrics.service import LyricResolver, LyricService, is_probable_ad
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, ResultKind, TrackInfo

TRACK = TrackInfo(title="Paper Lanterns", artist="The Example Band", album="Lights", duration=200.0)
SYNCED = "[00:01.00]first line\n[00:05.00]second line"


def cand(cid, synced=SYNCED, plain="first line", title="Paper Lanterns", duration=200.0, instrumental=False):
    return Candidate("fake", cid, title, "The Example Band", "Lights", duration, synced, plain, instrumental)


class FakeSource(LyricSource):
    name = "fake"

    def __init__(self, exact=None, found=(), lookup_error=False, search_error=False, delay=0.0):
        self.exact = exact
        self.found = list(found)
        self.lookup_error = lookup_error
        self.search_error = search_error
        self.delay = delay
        self.calls: list[str] = []

    async def lookup(self, track):
        self.calls.append("lookup")
        await asyncio.sleep(self.delay)
        if self.lookup_error:
            raise SourceError("lookup down")
        return self.exact

    async def search(self, track):
        self.calls.append("search")
        await asyncio.sleep(self.delay)
        if self.search_error:
            raise SourceError("search down")
        return self.found


class CrashingSource(FakeSource):
    async def lookup(self, track):
        raise RuntimeError("bug in source")

    async def search(self, track):
        raise RuntimeError("bug in source")


async def test_synced_exact_hit_skips_search():
    src = FakeSource(exact=cand("exact"))
    result = await LyricResolver([src]).resolve(TRACK)
    assert result.kind is ResultKind.SYNCED
    assert [line.text for line in result.lyrics.lines] == ["first line", "second line"]
    assert src.calls == ["lookup"]


async def test_plain_exact_hit_still_searches_for_synced():
    src = FakeSource(exact=cand("exact", synced=None), found=[cand("found", duration=201.0)])
    result = await LyricResolver([src]).resolve(TRACK)
    assert src.calls == ["lookup", "search"]
    assert result.kind is ResultKind.SYNCED
    assert result.candidate.source_id == "found"


async def test_low_scoring_exact_hit_falls_back_to_search():
    src = FakeSource(exact=cand("exact", title="Something Else Entirely"), found=[cand("found")])
    result = await LyricResolver([src]).resolve(TRACK)
    assert result.candidate.source_id == "found"


async def test_plain_only_when_no_synced_anywhere():
    src = FakeSource(exact=None, found=[cand("plain", synced=None)])
    result = await LyricResolver([src]).resolve(TRACK)
    assert result.kind is ResultKind.PLAIN_ONLY
    assert not result.lyrics.synced


async def test_later_source_synced_beats_earlier_plain():
    first = FakeSource(found=[cand("plain", synced=None)])
    second = FakeSource(found=[cand("synced")])
    result = await LyricResolver([first, second]).resolve(TRACK)
    assert result.kind is ResultKind.SYNCED
    assert second.calls  # kept looking past the plain-only hit


async def test_instrumental():
    src = FakeSource(found=[cand("inst", synced=None, plain=None, instrumental=True)])
    result = await LyricResolver([src]).resolve(TRACK)
    assert result.kind is ResultKind.INSTRUMENTAL
    assert result.lyrics is None


async def test_not_found_when_nothing_passes():
    src = FakeSource(found=[cand("far", title="Something Else Entirely", duration=230.0)])
    assert (await LyricResolver([src]).resolve(TRACK)).kind is ResultKind.NOT_FOUND


async def test_all_sources_failing_is_reported_separately():
    src = FakeSource(lookup_error=True, search_error=True)
    assert (await LyricResolver([src]).resolve(TRACK)).kind is ResultKind.SOURCES_FAILED


async def test_search_failure_after_clean_miss_counts_as_failure():
    src = FakeSource(exact=None, search_error=True)
    assert (await LyricResolver([src]).resolve(TRACK)).kind is ResultKind.SOURCES_FAILED


async def test_not_found_with_a_failed_source_is_asked_again():
    broken = FakeSource(lookup_error=True, search_error=True)
    clean = FakeSource(found=[cand("far", title="Something Else Entirely", duration=230.0)])
    resolver = LyricResolver([clean, broken])
    first = await resolver.resolve(TRACK)
    assert first.kind is ResultKind.NOT_FOUND and "source_failed" in first.notes
    again = await resolver.resolve(TRACK)
    assert "session_miss" not in again.notes
    assert clean.calls.count("search") == 2


async def test_clean_not_found_is_still_remembered():
    clean = FakeSource(found=[cand("far", title="Something Else Entirely", duration=230.0)])
    resolver = LyricResolver([clean])
    await resolver.resolve(TRACK)
    assert "session_miss" in (await resolver.resolve(TRACK)).notes
    assert clean.calls.count("search") == 1


async def test_lookup_failure_alone_does_not_fail_the_source():
    src = FakeSource(lookup_error=True, found=[cand("found")])
    assert (await LyricResolver([src]).resolve(TRACK)).kind is ResultKind.SYNCED


async def test_one_broken_source_does_not_stop_the_next():
    broken = CrashingSource()
    good = FakeSource(found=[cand("good")])
    result = await LyricResolver([broken, good]).resolve(TRACK)
    assert result.kind is ResultKind.SYNCED


async def test_each_request_is_bounded_by_the_timeout():
    slow = FakeSource(exact=cand("slow"), delay=1.0)
    good = FakeSource(found=[cand("good")])
    result = await LyricResolver([slow, good], request_timeout=0.05).resolve(TRACK)
    assert result.candidate.source_id == "good"


async def test_malformed_synced_lyrics_fall_back_to_plain():
    src = FakeSource(found=[cand("broken", synced="no timestamps here")])
    result = await LyricResolver([src]).resolve(TRACK)
    assert result.kind is ResultKind.PLAIN_ONLY


@pytest.mark.parametrize(
    "track, expected",
    [
        (TrackInfo("Ad", "Spotify", album="", duration=30.0, source_app="Spotify.exe"), True),
        (TrackInfo("Short Song", "Band", album="Album", duration=30.0, source_app="Spotify.exe"), False),
        (TrackInfo("Unknown Length", "Band", album="", duration=0.0, source_app="Spotify.exe"), False),
        (TrackInfo("Long", "Band", album="", duration=61.0, source_app="Spotify.exe"), False),
        # YouTube Music never reports an album: short songs there are not ads.
        (TrackInfo("Short Song", "Band", album="", duration=45.0, source_app="Chrome"), False),
    ],
)
def test_probable_ad_rule(track, expected):
    assert is_probable_ad(track) is expected


async def test_probable_ad_is_not_queried():
    src = FakeSource(found=[cand("x")])
    result = await LyricResolver([src]).resolve(TrackInfo("Ad", "Brand", album="", duration=30.0, source_app="Spotify.exe"))
    assert result.kind is ResultKind.SKIPPED_AD
    assert src.calls == []


async def test_service_cancels_old_query_and_emits_only_latest():
    slow = FakeSource(found=[cand("x")], delay=0.2)
    service = LyricService(LyricResolver([slow]))
    received = []
    service.resultReady.connect(received.append)

    old = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 200.0)
    new = TrackInfo("Paper Lanterns", "The Example Band", "Lights (Deluxe)", 200.0)
    service.request(old)
    first_task = service._task
    await asyncio.sleep(0.01)
    service.request(new)
    await asyncio.sleep(0.6)

    assert first_task.cancelled()
    assert [r.track for r in received] == [new]


async def test_service_cancel_drops_inflight_result():
    service = LyricService(LyricResolver([FakeSource(found=[cand("x")], delay=0.1)]))
    received = []
    service.resultReady.connect(received.append)
    service.request(TRACK)
    await asyncio.sleep(0.01)
    service.cancel()
    await asyncio.sleep(0.3)
    assert received == []


class FlakySource(FakeSource):
    """Search fails the first `failures` times, then answers."""

    def __init__(self, failures=1, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.titles: list[str] = []

    async def search(self, track):
        self.calls.append("search")
        self.titles.append(track.title)
        if self.failures:
            self.failures -= 1
            raise SourceError("search down")
        return self.found


def watched_service(*sources, delay=0.02):
    service = LyricService(LyricResolver(list(sources)), retry_delay=delay)
    shown, checked = [], []
    service.resultReady.connect(shown.append)
    service.resultChecked.connect(checked.append)
    return service, shown, checked


async def test_service_asks_once_more_after_a_source_failure():
    service, shown, _ = watched_service(FlakySource(found=[cand("x")]))
    service.request(TRACK)
    await asyncio.sleep(0.2)
    assert [r.kind for r in shown] == [ResultKind.SOURCES_FAILED, ResultKind.SYNCED]


async def test_service_plain_fallback_gives_way_to_synced_on_the_second_try():
    plain = FakeSource(found=[cand("plain", synced=None)])
    service, shown, _ = watched_service(plain, FlakySource(found=[cand("synced")]))
    service.request(TRACK)
    await asyncio.sleep(0.2)
    assert [r.kind for r in shown] == [ResultKind.PLAIN_ONLY, ResultKind.SYNCED]
    assert "source_failed" in shown[0].notes


async def test_service_retries_only_once_and_keeps_the_screen_when_nothing_changed():
    src = FlakySource(failures=99)
    service, shown, checked = watched_service(src)
    service.request(TRACK)
    await asyncio.sleep(0.3)
    assert src.calls.count("search") == 2
    assert [r.kind for r in shown] == [ResultKind.SOURCES_FAILED]
    assert [r.kind for r in checked] == [ResultKind.SOURCES_FAILED]


async def test_service_track_change_cancels_the_retry():
    src = FlakySource(failures=99)
    service, _, _ = watched_service(src, delay=0.1)
    service.request(TRACK)
    await asyncio.sleep(0.03)
    service.request(TrackInfo("Another Song", "The Example Band", "", 180.0))
    await asyncio.sleep(0.4)
    assert src.titles.count(TRACK.title) == 1
    assert src.titles.count("Another Song") == 2


async def test_service_clean_not_found_is_not_retried():
    src = FakeSource(found=[cand("far", title="Something Else Entirely", duration=230.0)])
    service, shown, _ = watched_service(src)
    service.request(TRACK)
    await asyncio.sleep(0.2)
    assert src.calls.count("search") == 1
    assert [r.kind for r in shown] == [ResultKind.NOT_FOUND]


async def test_lookup_hit_returned_again_by_search_is_listed_once():
    # Plain-only exact hit forces the search; the search returns the same entry again.
    plain = cand("same-id", synced=None)
    src = FakeSource(exact=plain, found=[plain, cand("other", duration=201.0)])
    resolver = LyricResolver([src])
    await resolver.resolve(TRACK)
    records = resolver.candidates_for(TRACK)
    ids = [c["id"] for c in records[-1]["candidates"]]
    assert sorted(ids) == ["other", "same-id"]


# --- bilingual titles: Chinese-part retry (SPEC v1.10) -----------------------------------


class TitleIndexedSource(LyricSource):
    """Answers only for exact titles, like LRCLIB filing a song under its Chinese name."""

    name = "lrclib"

    def __init__(self, by_title, fail_on=()):
        self.by_title = by_title
        self.fail_on = set(fail_on)
        self.queries: list[str] = []

    async def lookup(self, track):
        self.queries.append(f"lookup:{track.title}")
        if track.title in self.fail_on:
            raise SourceError("down")
        hits = self.by_title.get(track.title, [])
        return hits[0] if hits else None

    async def search(self, track):
        self.queries.append(f"search:{track.title}")
        if track.title in self.fail_on:
            raise SourceError("down")
        return list(self.by_title.get(track.title, []))


BILINGUAL = TrackInfo("紙燈籠 Paper Lanterns", "The Example Band", "Lights", 200.0)


def chinese_cand():
    return Candidate("lrclib", "cn", "紙燈籠", "The Example Band", "Lights", 200.0, SYNCED, "first line")


async def test_bilingual_title_retries_with_its_chinese_part():
    src = TitleIndexedSource({"紙燈籠": [chinese_cand()]})
    resolver = LyricResolver([src])
    result = await resolver.resolve(BILINGUAL)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "cn"
    assert src.queries == ["lookup:紙燈籠 Paper Lanterns", "search:紙燈籠 Paper Lanterns", "lookup:紙燈籠"]
    assert resolver.candidates_for(BILINGUAL)[-1]["stage"] == "lookup-chinese-part"


async def test_no_retry_when_the_full_title_is_found():
    full = Candidate("lrclib", "full", "紙燈籠 Paper Lanterns", "The Example Band", "Lights", 200.0, SYNCED, "x")
    src = TitleIndexedSource({"紙燈籠 Paper Lanterns": [full], "紙燈籠": [chinese_cand()]})
    result = await LyricResolver([src]).resolve(BILINGUAL)
    assert result.candidate.source_id == "full"
    assert not any(q.endswith(":紙燈籠") for q in src.queries)


async def test_no_retry_for_single_language_titles():
    src = TitleIndexedSource({})
    await LyricResolver([src]).resolve(TrackInfo("紙燈籠", "The Example Band", "", 200.0))
    assert src.queries == ["lookup:紙燈籠", "search:紙燈籠"]


async def test_failed_retry_still_reports_not_found():
    src = TitleIndexedSource({}, fail_on={"紙燈籠"})
    assert (await LyricResolver([src]).resolve(BILINGUAL)).kind is ResultKind.NOT_FOUND


async def test_a_version_of_another_length_under_the_chinese_part_is_taken():
    # SPEC v1.66: LRCLIB files the song under its Chinese name only, 4 s shorter than the video.
    shorter = Candidate("lrclib", "cn", "紙燈籠", "The Example Band", "Lights", 196.0, SYNCED, "first line")
    src = TitleIndexedSource({"紙燈籠": [shorter]})
    result = await LyricResolver([src]).resolve(BILINGUAL)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "cn"
    assert result.version_mismatch


async def test_a_chinese_part_that_is_the_singer_brings_no_versions_of_another_length():
    # "Comedy / 星野源": the Chinese part is the singer; his "Melody" (151 s) is not the song.
    melody = Candidate("lrclib", "melody", "Melody", "星野源", "", 151.0, SYNCED, "first line")
    src = TitleIndexedSource({"星野源": [melody]})
    result = await LyricResolver([src]).resolve(TrackInfo("Comedy / 星野源", "星野源", "", 230.7))
    assert result.kind is ResultKind.NOT_FOUND


class SimplifiedTitles(LyricSource):
    """NetEase-like: simplified source whose entries keep traditional titles."""

    name = "netease"
    simplified = True

    async def search(self, track):
        return [Candidate("netease", "tw", "紙燈籠 Paper Lanterns", "范例乐团", "", 200.0, SYNCED, "x")]


async def test_simplified_source_titles_are_compared_in_one_script():
    from lyricfloat.lyrics.chinese import ChineseConverter
    from lyricfloat.lyrics.pipeline import OpenCCPipeline

    pipe = OpenCCPipeline(converter=ChineseConverter())
    resolver = LyricResolver([SimplifiedTitles()], pipeline=pipe)
    result = await resolver.resolve(TrackInfo("紙燈籠 Paper Lanterns", "範例樂團", "", 200.0))
    assert result.kind is ResultKind.SYNCED
    record = resolver.candidates_for(TrackInfo("紙燈籠 Paper Lanterns", "範例樂團", "", 200.0))[-1]
    assert record["candidates"][0]["title_sim"] == pytest.approx(1.0)


async def test_youtube_video_titles_are_searched_by_the_song_title():
    class Recording(FakeSource):
        async def search(self, track):
            self.titles = getattr(self, "titles", []) + [track.title]
            return await super().search(track)

    src = Recording(found=[cand("found")])
    video = TrackInfo("The Example Band - Paper Lanterns (Official Music Video)", "The Example Band", "", 200.0)
    result = await LyricResolver([src]).resolve(video)
    assert result.kind is ResultKind.SYNCED and result.track == video  # the screen keeps the player's title
    assert src.titles == ["Paper Lanterns (Official Music Video)"]


class LazyChineseIndexed(LyricSource):
    """NetEase-like: hits without lyrics, and only the Chinese part finds the song."""

    name = "netease"
    lyrics_inline = False

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, track):
        self.queries.append(track.title)
        if track.title != "紙燈籠":
            return []
        return [Candidate("netease", "n1", "紙燈籠", "The Example Band", "", 200.0)]

    async def fetch_lyrics(self, c):
        return Candidate(c.source, c.source_id, c.title, c.artist, c.album, c.duration, SYNCED)


async def test_lazy_sources_retry_bilingual_titles_with_the_chinese_part():
    src = LazyChineseIndexed()  # SPEC v1.32: the English part can bury the song on NetEase
    result = await LyricResolver([src]).resolve(BILINGUAL)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "n1"
    assert src.queries == ["紙燈籠 Paper Lanterns", "紙燈籠"]


async def test_artists_named_in_a_video_title_count_for_matching():
    class ByOtherArtist(FakeSource):
        async def search(self, track):
            self.artists = track.artist
            return [Candidate("fake", "b", "Paper Lanterns", "Second Singer", "", 200.0, SYNCED, None, title_only=True)]

    src = ByOtherArtist()
    video = TrackInfo("The Example Band X Second Singer - Paper Lanterns (Official Audio)", "The Example Band", "", 200.0)
    result = await LyricResolver([src]).resolve(video)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "b"
    assert src.artists == "The Example Band, second singer"  # the player's artist stays first
