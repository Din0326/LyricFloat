import pytest

from lyricfloat.lyrics.translation import (
    align_same_source,
    align_translations,
    median_offset,
    parse_translation,
    text_similarity,
)
from lyricfloat.models import LyricLine as L

# Made-up lines; the "NetEase" side mirrors them with its own timestamps.
MAIN = [
    L(10.0, "we walk along the river"),
    L(14.0, "the lamps are burning low"),
    L(18.0, "and nobody is waiting"),
    L(22.0, "for the paper boat to go"),
]
ORIGINAL = [
    L(10.3, "We walk along the river"),
    L(14.2, "The lamps are burning low"),
    L(18.4, "And nobody is waiting"),
    L(22.1, "For the paper boat to go"),
]
TRANSLATIONS = [L(10.3, "我們沿著河走"), L(14.2, "燈火漸暗"), L(18.4, "沒有人在等待"), L(22.1, "等紙船出發")]


def test_text_similarity_ignores_case_and_punctuation():
    assert text_similarity("We walk, along!", "we walk along") == pytest.approx(1.0)
    assert text_similarity("", "anything") == 0.0


def test_aligned_within_one_second_and_matching_text():
    assert align_translations(MAIN, ORIGINAL, TRANSLATIONS) == ("我們沿著河走", "燈火漸暗", "沒有人在等待", "等紙船出發")


def test_lines_more_than_one_second_apart_get_nothing():
    shifted = [L(o.start + 1.5, o.text) for o in ORIGINAL]
    trans = [L(t.start + 1.5, t.text) for t in TRANSLATIONS]
    assert align_translations(MAIN, shifted, trans) == (None, None, None, None)


def test_different_text_gets_nothing_even_when_time_matches():
    other_words = [L(o.start, "completely different words here") for o in ORIGINAL]
    assert align_translations(MAIN, other_words, TRANSLATIONS) == (None, None, None, None)


def test_missing_translation_lines_leave_gaps():
    partial = [TRANSLATIONS[0], TRANSLATIONS[2]]
    assert align_translations(MAIN, ORIGINAL, partial) == ("我們沿著河走", None, "沒有人在等待", None)


def test_closest_in_time_wins_between_equal_matches():
    main = [L(30.0, "la la la")]
    original = [L(29.2, "la la la"), L(30.4, "la la la")]
    trans = [L(29.2, "早的"), L(30.4, "近的")]
    assert align_translations(main, original, trans) == ("近的",)


def test_blank_original_lines_are_skipped():
    original = [L(10.0, ""), *ORIGINAL]
    trans = [L(10.0, "不該配到"), *TRANSLATIONS]
    assert align_translations(MAIN[:1], original, trans) == ("我們沿著河走",)


def test_same_source_pairs_by_timestamp():
    assert align_same_source(ORIGINAL, TRANSLATIONS) == ("我們沿著河走", "燈火漸暗", "沒有人在等待", "等紙船出發")
    assert align_same_source([L(1.0, "")], [L(1.0, "x")]) == (None,)


def test_parse_translation_drops_blank_lines():
    lines = parse_translation("[00:01.00]一\n[00:02.00]\n[00:03.00]三")
    assert [(l.start, l.text) for l in lines] == [(1.0, "一"), (3.0, "三")]
    assert parse_translation(None) == []


def test_median_offset_between_two_versions():
    later = [L(o.start + 2.5, o.text) for o in ORIGINAL]
    # main - other: MAIN is about 2.5-2.9 s earlier than `later`
    assert median_offset(MAIN, later) == pytest.approx(-2.75, abs=0.2)


def test_median_offset_needs_three_pairs():
    assert median_offset(MAIN[:2], ORIGINAL[:2]) is None


def test_repeated_chorus_far_away_is_not_paired():
    main = [L(10.0, "la la la"), L(14.0, "the lamps are burning low"), L(18.0, "and nobody is waiting"), L(22.0, "for the paper boat to go")]
    other = [L(90.0, "la la la"), *ORIGINAL[1:]]
    assert median_offset(main, other) == pytest.approx(-0.2, abs=0.11)
