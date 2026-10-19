import pytest

from lyricfloat.lyrics.lrc import parse_lrc, parse_plain


def starts(parsed):
    return [round(line.start, 3) for line in parsed.lyrics.lines]


def texts(parsed):
    return [line.text for line in parsed.lyrics.lines]


def test_basic_lines_are_parsed_in_order():
    parsed = parse_lrc("[00:01.00]alpha\n[00:02.50]bravo\n[00:10.00]charlie")
    assert starts(parsed) == [1.0, 2.5, 10.0]
    assert texts(parsed) == ["alpha", "bravo", "charlie"]
    assert parsed.lyrics.synced


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("[00:05]", 5.0),
        ("[00:05.5]", 5.5),
        ("[00:05.25]", 5.25),
        ("[00:05.125]", 5.125),
        ("[00:05:25]", 5.25),  # colon fraction variant
        ("[01:02.00]", 62.0),
        ("[100:00.00]", 6000.0),  # minutes past 99
    ],
)
def test_timestamp_formats(tag, expected):
    assert starts(parse_lrc(f"{tag}line")) == [expected]


def test_multiple_timestamps_on_one_line_expand_to_several_lines():
    parsed = parse_lrc("[00:30.00][00:10.00]chorus\n[00:20.00]verse")
    assert starts(parsed) == [10.0, 20.0, 30.0]
    assert texts(parsed) == ["chorus", "verse", "chorus"]


def test_out_of_order_timestamps_are_sorted():
    parsed = parse_lrc("[00:20.00]second\n[00:05.00]first\n[00:40.00]third")
    assert texts(parsed) == ["first", "second", "third"]


def test_same_timestamp_keeps_file_order_and_drops_exact_duplicates():
    parsed = parse_lrc("[00:05.00]original\n[00:05.00]translated\n[00:05.00]original")
    assert texts(parsed) == ["original", "translated"]


def test_positive_offset_makes_lyrics_earlier():
    parsed = parse_lrc("[offset:+500]\n[00:10.00]line")
    assert parsed.offset_ms == 500
    assert starts(parsed) == [9.5]


def test_negative_offset_makes_lyrics_later():
    parsed = parse_lrc("[offset: -250]\n[00:10.00]line")
    assert parsed.offset_ms == -250
    assert starts(parsed) == [10.25]


def test_offset_tag_after_lines_still_applies():
    parsed = parse_lrc("[00:10.00]line\n[offset:1000]")
    assert starts(parsed) == [9.0]


def test_offset_never_pushes_lines_below_zero():
    parsed = parse_lrc("[offset:2000]\n[00:01.00]line")
    assert starts(parsed) == [0.0]


def test_invalid_offset_is_ignored():
    parsed = parse_lrc("[offset:abc]\n[00:01.00]line")
    assert parsed.offset_ms == 0
    assert starts(parsed) == [1.0]


def test_id_tags_are_collected_not_shown():
    parsed = parse_lrc("[ar:Some Artist]\n[ti:Some Title]\n[length: 03:20]\n[00:01.00]line")
    assert parsed.tags["ar"] == "Some Artist"
    assert parsed.tags["ti"] == "Some Title"
    assert texts(parsed) == ["line"]


def test_empty_text_lines_are_kept_as_breaks():
    parsed = parse_lrc("[00:01.00]sing\n[00:05.00]\n[00:09.00]again")
    assert texts(parsed) == ["sing", "", "again"]


def test_word_level_tags_are_stripped():
    parsed = parse_lrc("[00:01.00]<00:01.00>one <00:01.40>two <00:01.90>three")
    assert texts(parsed) == ["one two three"]


def test_bom_crlf_and_untimed_noise_are_tolerated():
    parsed = parse_lrc("﻿[ti:x]\r\n[00:01.00]a\r\nnot a lyric line\r\n[00:02.00]b\r\n")
    assert texts(parsed) == ["a", "b"]


def test_empty_input_gives_no_lines():
    assert parse_lrc("").lyrics.lines == ()


def test_plain_lyrics_skip_blank_lines():
    lyrics = parse_plain("first\n\n  second  \n")
    assert [line.text for line in lyrics.lines] == ["first", "second"]
    assert not lyrics.synced


# -- the line a version of another length waits at (SPEC v1.29) ---------------------------


def test_first_sung_line_skips_credits_and_the_title_line():
    from lyricfloat.lyrics.lrc import first_sung_line
    from lyricfloat.models import LyricLine

    lines = [LyricLine(0.0, "作詞 : 某人"), LyricLine(1.0, "Composer：Someone"), LyricLine(2.0, "♪"),
             LyricLine(2.5, "母带工程师 Mastering Engineer : 某人 @ 某錄音室"),
             LyricLine(3.0, "Paper Lanterns - The Example Band"), LyricLine(20.0, "sung line"), LyricLine(24.0, "作詞 : not a credit here")]
    assert first_sung_line(lines, "Paper Lanterns", "The Example Band") == 5
    assert first_sung_line([LyricLine(20.0, "sung line")]) == 0
    assert first_sung_line([LyricLine(0.0, "作曲：某人")]) == 0  # nothing sung: start at the top
