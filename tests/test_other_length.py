"""SPEC v1.29: versions of another length, e.g. the player plays the MV. Placeholder text only."""

import sqlite3

import pytest

from lyricfloat.lyrics.cache import L3_APPLIED, L3_UNUSABLE, SCHEMA_VERSION, SqliteCache
from lyricfloat.lyrics.scoring import metadata_score, other_length_usable, score_candidate
from lyricfloat.lyrics.service import MAX_FETCH, LyricResolver
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, ResultKind, TrackInfo

MV = TrackInfo("Paper Lanterns", "The Example Band", "", 290.0)  # the MV: 110 s more than the album cut
ALBUM = 180.0
SYNCED = "\n".join(f"[00:{20 + 4 * i:02d}.00]sung line {i}" for i in range(6))
PLAIN = "\n".join(f"sung line {i}" for i in range(6))


def cand(cid, synced=SYNCED, plain=PLAIN, title="Paper Lanterns", duration=ALBUM, source="inline"):
    return Candidate(source, cid, title, "The Example Band", "", duration, synced, plain)


class InlineSource(LyricSource):
    name = "inline"

    def __init__(self, found=(), fail=False):
        self.found = list(found)
        self.fail = fail

    async def search(self, track):
        if self.fail:
            raise SourceError("inline: down")
        return self.found


class LazySource(LyricSource):
    """Lists hits without lyrics, like NetEase."""

    name = "lazy"
    lyrics_inline = False

    def __init__(self, found=(), lyrics=None, broken=()):
        self.found = list(found)
        self.lyrics = lyrics or {}
        self.broken = set(broken)
        self.fetched: list[str] = []

    async def search(self, track):
        return self.found

    async def fetch_lyrics(self, c):
        self.fetched.append(c.source_id)
        if c.source_id in self.broken:
            raise SourceError("lazy: fetch down")
        synced, plain = self.lyrics.get(c.source_id, (None, None))
        return Candidate(c.source, c.source_id, c.title, c.artist, c.album, c.duration, synced, plain)


def listed(cid, duration=ALBUM):
    return Candidate("lazy", cid, "Paper Lanterns", "The Example Band", "", duration)


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "cache.sqlite3")
    yield c
    c.close()


# -- scoring ----------------------------------------------------------------------


def test_only_a_length_rejection_with_good_metadata_is_usable():
    assert other_length_usable(score_candidate(MV, cand("mv")))
    assert not other_length_usable(score_candidate(MV, cand("other", title="Something Else Entirely")))
    assert not other_length_usable(score_candidate(MV, cand("fits", duration=289.0)))  # accepted, not rejected
    assert metadata_score(score_candidate(MV, cand("mv"))) == pytest.approx(1.0)


def test_a_version_of_another_length_needs_its_artist():
    # SPEC v1.58: the same title by someone else is another song (语音备忘录 by Pi-re for PG One's).
    other = Candidate("inline", "x", "Paper Lanterns", "Pi-re", "", ALBUM, SYNCED)
    assert not other_length_usable(score_candidate(MV, other))


# -- resolver ---------------------------------------------------------------------


async def test_a_synced_version_of_another_length_is_used_when_nothing_fits(cache):
    resolver = LyricResolver([InlineSource([cand("album")])], cache=cache)
    result = await resolver.resolve(MV)
    assert result.kind is ResultKind.SYNCED and result.version_mismatch
    assert result.timeline_checked and result.alternatives == ()  # no L1 against versions that all differ
    again = await resolver.resolve(MV)
    assert again.from_cache and again.version_mismatch


async def test_a_version_that_fits_wins_from_a_later_source(cache):
    first = InlineSource([cand("album")])
    second = InlineSource([cand("mv-cut", duration=289.0)])
    result = await LyricResolver([first, second], cache=cache).resolve(MV)
    assert result.candidate.source_id == "mv-cut" and not result.version_mismatch


async def test_plain_lyrics_that_fit_still_come_before_another_length(cache):
    fits = InlineSource([cand("plain", synced=None, duration=289.0)])
    other = InlineSource([cand("album")])
    result = await LyricResolver([other, fits], cache=cache).resolve(MV)
    assert result.kind is ResultKind.PLAIN_ONLY and not result.version_mismatch


async def test_synced_comes_before_plain_among_other_lengths(cache):
    first = InlineSource([cand("plain-album", synced=None)])
    second = InlineSource([cand("synced-album")])
    result = await LyricResolver([first, second], cache=cache).resolve(MV)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "synced-album"


async def test_plain_of_another_length_is_used_and_cached(cache):
    resolver = LyricResolver([InlineSource([cand("plain-album", synced=None)])], cache=cache)
    result = await resolver.resolve(MV)
    assert result.kind is ResultKind.PLAIN_ONLY and result.version_mismatch
    assert (await cache.get(MV)).result.version_mismatch


async def test_another_song_of_another_length_is_still_not_found(cache):
    src = InlineSource([cand("other", title="Something Else Entirely")])
    assert (await LyricResolver([src], cache=cache).resolve(MV)).kind is ResultKind.NOT_FOUND


async def test_lazy_versions_of_another_length_are_fetched_until_a_synced_one(cache):
    found = [listed(f"v{i}") for i in range(MAX_FETCH + 2)]
    src = LazySource(found, {"v0": (None, PLAIN), "v1": (SYNCED, PLAIN)})
    result = await LyricResolver([src], cache=cache).resolve(MV)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "v1"
    assert src.fetched == ["v0", "v1"]


async def test_lazy_fetches_stop_at_the_limit(cache):
    src = LazySource([listed(f"v{i}") for i in range(MAX_FETCH + 2)])
    assert (await LyricResolver([src], cache=cache).resolve(MV)).kind is ResultKind.NOT_FOUND
    assert len(src.fetched) == MAX_FETCH


async def test_a_broken_fetch_is_not_remembered_as_a_clean_miss(cache):
    src = LazySource([listed("v0")], broken={"v0"})
    resolver = LyricResolver([InlineSource([]), src], cache=cache)
    first = await resolver.resolve(MV)
    assert first.kind is ResultKind.NOT_FOUND and "source_failed" in first.notes
    await resolver.resolve(MV)
    assert src.fetched == ["v0", "v0"]  # asked again


async def test_another_length_with_a_failed_source_is_shown_but_not_cached(cache):
    resolver = LyricResolver([InlineSource([cand("album")]), InlineSource(fail=True)], cache=cache)
    result = await resolver.resolve(MV)
    assert result.version_mismatch and "source_failed" in result.notes
    assert await cache.get(MV) is None


# -- binding, L3 and the cache ----------------------------------------------------------


async def test_binding_marks_a_version_of_another_length(cache):
    resolver = LyricResolver([InlineSource([])], cache=cache)
    bound = await resolver.bind(MV, "inline", cand("album"))
    assert bound.bound and bound.version_mismatch
    assert (await cache.get(MV)).result.version_mismatch
    fits = await resolver.bind(MV, "inline", cand("mv-cut", duration=289.0))
    assert not fits.version_mismatch


def segments(start=60.0, p=0.6):
    return [{"start": start + 4 * i, "end": start + 4 * i + 2, "words": [{"start": start + 4 * i, "end": start + 4 * i + 2, "text": f"sung line {i}", "p": p}]} for i in range(6)]


async def test_l3_times_another_length_from_the_audio_and_drops_the_cue_offset(cache):
    resolver = LyricResolver([InlineSource([cand("album")])], cache=cache)
    first = await resolver.resolve(MV)
    await resolver.set_offset(MV, 39.5)  # the user's cue: the MV starts singing 40 s later
    judged, shown = await resolver.store_alignment(MV, first.lyrics.lines, segments(), 0.0, 290.0)
    assert judged.usable and judged.stats["plain"]  # timed without the source times, which are 40 s off
    assert shown.l3_state == L3_APPLIED and shown.offset == 0.0
    assert [l.start for l in shown.shown_lyrics().lines] == pytest.approx([60.0 + 4 * i for i in range(6)])


async def test_an_unusable_alignment_keeps_the_cue_offset(cache):
    resolver = LyricResolver([InlineSource([cand("album")])], cache=cache)
    first = await resolver.resolve(MV)
    await resolver.set_offset(MV, 39.5)
    judged, shown = await resolver.store_alignment(MV, first.lyrics.lines, segments(p=0.05), 0.0, 290.0)
    assert not judged.usable and shown.l3_state == L3_UNUSABLE and shown.offset == pytest.approx(39.5)


def test_older_cache_files_gain_the_column(tmp_path):
    path = tmp_path / "cache.sqlite3"
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE lyrics DROP COLUMN version_mismatch")
    db.execute("PRAGMA user_version=7")
    db.commit()
    db.close()
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    assert "version_mismatch" in {row[1] for row in db.execute("PRAGMA table_info(lyrics)")}
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


@pytest.mark.parametrize(
    "ours, theirs, same",
    [
        ("PG One", "Pi-re", False),  # 0.55 by similarity: SPEC v1.58's 0.5 let it through
        ("ILLIT", "Chillvibe", False),
        ("鄧紫棋", "G.E.M. 鄧紫棋", True),  # 0.46 by similarity, one name holds the other
        ("Karencici", "Karen Ci", True),  # 0.82
        ("The Example Band", "The Example Band", True),
    ],
)
def test_the_same_artist_for_another_length(ours, theirs, same):
    # SPEC v1.61: similarity >= 0.8, or one name holding the other.
    track = TrackInfo("Paper Lanterns", ours, "", 290.0)
    other = Candidate("inline", "x", "Paper Lanterns", theirs, "", ALBUM, SYNCED)
    assert other_length_usable(score_candidate(track, other)) is same


async def test_a_video_takes_a_shorter_synced_version_over_plain_text_of_its_length():
    # SPEC v1.62: the MV runs 8 s over the synced version; a plain entry happens to fit its length.
    plain_fit = Candidate("inline", "plain", "Paper Lanterns", "The Example Band", "", 206.0, None, PLAIN)
    synced = Candidate("inline", "synced", "Paper Lanterns", "The Example Band", "", 197.0, SYNCED)
    video = TrackInfo("The Example Band - Paper Lanterns [Official MV]", "The Example Band", "", 206.0, "Chrome")
    result = await LyricResolver([InlineSource([plain_fit, synced])]).resolve(video)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "synced" and result.version_mismatch
    # Other players keep the plain text of their length: their other versions may be sped up.
    spotify = TrackInfo("Paper Lanterns", "The Example Band", "Album", 206.0, "Spotify.exe")
    assert (await LyricResolver([InlineSource([plain_fit, synced])]).resolve(spotify)).kind is ResultKind.PLAIN_ONLY
    # A version longer than the video is no MV's song.
    longer = Candidate("inline", "longer", "Paper Lanterns", "The Example Band", "", 215.0, SYNCED)
    assert (await LyricResolver([InlineSource([plain_fit, longer])]).resolve(video)).kind is ResultKind.PLAIN_ONLY


class Named(InlineSource):
    def __init__(self, name, found):
        super().__init__(found)
        self.name = name


@pytest.mark.parametrize("this_love_first", [True, False])
async def test_a_title_unlike_the_players_yields_to_the_same_title_of_another_length(this_love_first):
    # SPEC v1.63: BIGBANG's MAKE LOVE fits the length (0.56 title) while This Love is 8 s shorter.
    this_love = Candidate("a", "this", "This Love", "BIGBANG", "", 210.0, SYNCED)
    make_love = Candidate("b", "make", "MAKE LOVE", "BIGBANG", "", 217.5, SYNCED)
    sources = [Named("a", [this_love]), Named("b", [make_love])]
    track = TrackInfo("This Love (G-Dragon Solo) (Live)", "BIGBANG", "Live", 218.0, "Spotify.exe")
    result = await LyricResolver(sources if this_love_first else sources[::-1]).resolve(track)
    assert result.candidate.source_id == "this" and result.version_mismatch


async def test_without_the_same_title_the_unlike_one_stays():
    make_love = Candidate("b", "make", "MAKE LOVE", "BIGBANG", "", 217.5, SYNCED)
    track = TrackInfo("This Love (G-Dragon Solo) (Live)", "BIGBANG", "Live", 218.0, "Spotify.exe")
    result = await LyricResolver([Named("b", [make_love])]).resolve(track)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "make"


async def test_among_equal_versions_the_nearest_length_is_taken():
    # SPEC v1.64: LRCLIB lists This Love at 292 s and at 210 s; the video is 218 s.
    long = Candidate("inline", "292", "This Love", "BIGBANG", "", 292.0, SYNCED)
    near = Candidate("inline", "210", "This Love", "BIGBANG", "", 210.0, SYNCED)
    track = TrackInfo("This Love", "BIGBANG", "Live", 230.0, "Spotify.exe")
    result = await LyricResolver([InlineSource([long, near])]).resolve(track)
    assert result.candidate.source_id == "210" and result.version_mismatch


# -- live versions (SPEC v1.75) -------------------------------------------------------------

PPLIN = TrackInfo("PPlin - 最後一班車(Official Music Video)", "PPlin 林芃逸", "", 249.1, "Chrome")


def pplin(cid, title, duration):
    return Candidate("lazy", cid, title, "PPlin 林芃逸", "", duration)


async def test_a_live_version_the_title_does_not_name_yields_to_the_studio_one():
    # NetEase lists the live one at the MV's length and the studio one 35 s shorter; both are fetched lazily.
    src = LazySource([pplin("live", "最後一班車 (Live)", 249.0), pplin("studio", "最後一班車", 213.7)],
                     {"live": (SYNCED, None), "studio": (SYNCED, None)})
    result = await LyricResolver([src]).resolve(PPLIN)
    assert result.candidate.source_id == "studio" and result.version_mismatch


async def test_a_studio_version_of_the_right_length_is_taken_at_once():
    src = InlineSource([cand("live", title="Paper Lanterns (Live)", duration=180.0), cand("studio", duration=181.5)])
    track = TrackInfo("Paper Lanterns", "The Example Band", "Album", 180.0, "Spotify.exe")
    result = await LyricResolver([src]).resolve(track)
    assert result.candidate.source_id == "studio" and not result.version_mismatch


async def test_a_live_version_stays_when_it_is_named_or_alone():
    live = cand("live", title="Paper Lanterns (Live)", duration=180.0)
    named = TrackInfo("Paper Lanterns - Live", "The Example Band", "Live at Home", 180.0, "Spotify.exe")
    assert (await LyricResolver([InlineSource([live, cand("studio", duration=150.0)])]).resolve(named)).candidate.source_id == "live"
    plain = TrackInfo("Paper Lanterns", "The Example Band", "Album", 180.0, "Spotify.exe")
    assert (await LyricResolver([InlineSource([live])]).resolve(plain)).candidate.source_id == "live"
