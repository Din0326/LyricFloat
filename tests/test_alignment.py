"""FR-21/22: judging an L3 alignment, storing it, showing it and reverting it. Placeholder text only."""

import asyncio

import pytest

from lyricfloat.align.apply import MIN_CONFIDENCE, language_of, lines_hash, map_words, plan, plan_from_json, plan_to_json
from lyricfloat.lyrics.cache import L3_APPLIED, L3_GAVE_UP, L3_MAX_ATTEMPTS, L3_REVERTED, L3_UNUSABLE, SqliteCache
from lyricfloat.lyrics.service import LyricResolver, LyricService
from lyricfloat.lyrics.sources.base import LyricSource
from lyricfloat.models import Candidate, LyricLine, ResultKind, TrackInfo

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 60.0)
LRC = "\n".join(f"[00:{10 + 5 * i:02d}.00]line number {i}" for i in range(8))
LINES = tuple(LyricLine(10.0 + 5 * i, f"line number {i}") for i in range(8))


def seg(start, p=0.6, text=None):
    words = (text or "line number x").split()
    step = 0.3
    return {"start": start, "end": start + step * len(words), "words": [{"start": start + step * k, "end": start + step * (k + 1), "text": " " + w, "p": p} for k, w in enumerate(words)]}


def segments(shift=0.2, pos0=0.0, **overrides):
    out = [seg(l.start + shift - pos0, text=l.text) for l in LINES]
    for i, s in overrides.items():
        out[int(i[1:])] = s
    return out


# -- plan -----------------------------------------------------------------------


def test_sure_lines_take_the_aligned_times():
    p = plan(LINES, segments(0.2), 0.0, 60.0)
    assert p.usable and p.stats["sure"] == 8
    assert p.starts == pytest.approx([l.start + 0.2 for l in LINES])
    assert p.words[0][0].first == 0 and p.words[0][-1].last == len(LINES[0].text)


def test_unsure_lines_shift_by_the_median():
    p = plan(LINES, segments(0.2, l3=seg(25.0 + 5.0), l4=seg(30.2, p=MIN_CONFIDENCE / 2)), 0.0, 60.0)
    assert p.usable and p.stats["sure"] == 6
    assert p.starts[3] == pytest.approx(LINES[3].start + 0.2)  # 5 s off: not trusted, shifted
    assert p.starts[4] == pytest.approx(LINES[4].start + 0.2)  # low confidence: not trusted
    assert p.words[3] is None and p.words[4] is None


def test_too_few_sure_lines_keep_the_source_times():
    bad = {f"l{i}": seg(LINES[i].start + 4.0) for i in range(4)}
    p = plan(LINES, segments(0.2, **bad), 0.0, 60.0)
    assert not p.usable and p.starts == tuple(l.start for l in LINES)


def test_lines_outside_the_recording_are_not_judged():
    p = plan(LINES, segments(0.2, pos0=20.0), 20.0, 30.0)
    assert p.stats["judged"] < len(LINES)
    assert p.starts[0] == pytest.approx(LINES[0].start + 0.2)  # before the recording: shifted


def test_segment_count_mismatch_is_unusable():
    assert not plan(LINES, segments()[:-1], 0.0, 60.0).usable


def test_starts_stay_in_order():
    crowded = (LyricLine(10.0, "a b"), LyricLine(10.5, "c d"), LyricLine(20.0, "e f"), LyricLine(25.0, "g h"), LyricLine(30.0, "i j"))
    segs = [seg(10.6, text="a b"), seg(10.4, text="c d"), seg(20.1, text="e f"), seg(25.1, text="g h"), seg(30.1, text="i j")]
    p = plan(crowded, segs, 0.0, 60.0)
    assert list(p.starts) == sorted(p.starts)


@pytest.mark.parametrize(
    "text, words, spans",
    [
        ("這是 一句", ["这是", "一句"], [(0, 2), (3, 5)]),
        ("hello there", [" hello", " there"], [(0, 5), (6, 11)]),
        ("中文 mixed", ["中", "文", " mixed"], [(0, 1), (1, 2), (3, 8)]),
    ],
)
def test_words_map_onto_the_displayed_characters(text, words, spans):
    mapped = map_words(text, [{"start": i, "end": i + 1, "text": w} for i, w in enumerate(words)], 100.0)
    assert [(w.first, w.last) for w in mapped] == spans and mapped[0].start == 100.0


@pytest.mark.parametrize(
    "texts, language",
    [
        (("作词 : 某人", "作曲 : 某人", "first line", "second line"), "en"),  # SPEC v1.82: NetEase credits
        (("first line", "second line"), "en"),
        (("作詞 : 某人", "這是第一句", "這是第二句"), "zh"),
        (("男：這是第一句", "女：這是第二句"), "zh"),  # duet labels are not credits once the singing starts
        (("first line", "這是第二句"), "zh"),
        (("作词 : 某人", "これは一行目"), "ja"),
        (("作词 : 某人", "첫 번째 줄"), "ko"),
    ],
)
def test_language_comes_from_the_sung_lines(texts, language):
    assert language_of(tuple(LyricLine(float(i), t) for i, t in enumerate(texts))) == language


def test_words_that_do_not_add_up_are_dropped():
    assert map_words("這是一句", [{"start": 0, "end": 1, "text": "这是一句多"}], 0.0) is None
    assert map_words("這是一句", [{"start": 0, "end": 1, "text": "这是"}], 0.0) is None


def test_stored_plan_only_fits_the_same_lines():
    p = plan(LINES, segments(0.2), 0.0, 60.0)
    stored = plan_to_json(p, LINES)
    back = plan_from_json(stored, LINES)
    assert back.starts == pytest.approx(p.starts) and back.words == p.words
    moved = tuple(LyricLine(l.start + 1, l.text) for l in LINES)
    assert plan_from_json(stored, moved) is None
    assert plan_from_json("not json", LINES) is None
    assert lines_hash(LINES) != lines_hash(moved)


# -- cache, resolver, service ---------------------------------------------------


class Source(LyricSource):
    name = "inline"

    async def search(self, track):
        return [Candidate("inline", "1", TRACK.title, TRACK.artist, TRACK.album, 60.0, LRC)]


@pytest.fixture
def cache(tmp_path):
    c = SqliteCache(tmp_path / "cache.sqlite3")
    yield c
    c.close()


async def test_alignment_shows_while_the_source_times_stay(cache):
    resolver = LyricResolver([Source()], cache=cache)
    first = await resolver.resolve(TRACK)
    judged, shown = await resolver.store_alignment(TRACK, first.lyrics.lines, segments(0.2), 0.0, 60.0)
    assert judged.usable and shown.l3_state == L3_APPLIED
    again = await resolver.resolve(TRACK)
    assert [l.start for l in again.lyrics.lines] == [l.start for l in LINES]  # stored as it was
    assert [l.start for l in again.shown_lyrics().lines] == pytest.approx([l.start + 0.2 for l in LINES])
    assert again.shown_lyrics().words[0] is not None


async def test_unusable_alignment_is_remembered_but_not_shown(cache):
    resolver = LyricResolver([Source()], cache=cache)
    first = await resolver.resolve(TRACK)
    bad = {f"l{i}": seg(LINES[i].start + 4.0) for i in range(5)}
    await resolver.store_alignment(TRACK, first.lyrics.lines, segments(0.2, **bad), 0.0, 60.0)
    again = await resolver.resolve(TRACK)
    assert again.l3_state == L3_UNUSABLE and again.alignment is None


async def test_revert_restores_the_source_times_for_good(cache):
    resolver = LyricResolver([Source()], cache=cache)
    first = await resolver.resolve(TRACK)
    await resolver.store_alignment(TRACK, first.lyrics.lines, segments(0.2), 0.0, 60.0)
    assert await resolver.revert_alignment(TRACK)
    again = await resolver.resolve(TRACK)
    assert again.l3_state == L3_REVERTED and again.alignment is None


async def test_alignment_of_changed_lyrics_is_discarded(cache):
    resolver = LyricResolver([Source()], cache=cache)
    await resolver.resolve(TRACK)
    other = tuple(LyricLine(l.start + 1, l.text) for l in LINES)
    judged, shown = await resolver.store_alignment(TRACK, other, segments(0.2), 0.0, 60.0)
    assert judged is None and shown is None
    assert (await resolver.resolve(TRACK)).l3_state is None


async def test_failures_give_up_after_a_few_tries(cache):
    resolver = LyricResolver([Source()], cache=cache)
    await resolver.resolve(TRACK)
    for _ in range(L3_MAX_ATTEMPTS):
        await resolver.alignment_failed(TRACK)
    assert (await resolver.resolve(TRACK)).l3_state == L3_GAVE_UP


async def test_editing_drops_the_alignment(cache):
    resolver = LyricResolver([Source()], cache=cache)
    first = await resolver.resolve(TRACK)
    await resolver.store_alignment(TRACK, first.lyrics.lines, segments(0.2), 0.0, 60.0)
    await resolver.save_edit(TRACK, [LyricLine(1.0, "edited")])
    edited = await resolver.resolve(TRACK)
    assert edited.l3_state is None and edited.alignment is None


async def test_service_switches_a_playing_song_to_the_aligned_times(cache):
    service = LyricService(LyricResolver([Source()], cache=cache))
    shown = []
    service.resultReady.connect(shown.append)
    service.request(TRACK)
    await asyncio.sleep(0.05)
    await service.store_alignment(TRACK, shown[-1].lyrics.lines, segments(0.2), 0.0, 60.0)
    assert shown[-1].alignment is not None
    await service.revert_alignment(TRACK)
    await asyncio.sleep(0.05)
    assert shown[-1].alignment is None and shown[-1].l3_state == L3_REVERTED
