import pytest

from lyricfloat.lyrics.language import Language, detect_from_lyrics, detect_from_metadata


@pytest.mark.parametrize(
    "title, artist, expected",
    [
        ("Paper Lanterns", "The Example Band", Language.EN),
        ("紙燈籠", "範例樂團", Language.ZH),
        ("Paper Lanterns", "範例樂團", Language.ZH),  # English title, Chinese artist name
        ("纸灯笼", "范例乐团", Language.ZH),  # simplified counts too
        ("かみの ちょうちん", "Example", Language.OTHER),
        ("紙の灯籠", "Example", Language.OTHER),  # kanji + kana is Japanese, not Chinese
        ("종이 등불", "Example", Language.OTHER),
        ("", "", Language.EN),
    ],
)
def test_metadata_detection(title, artist, expected):
    assert detect_from_metadata(title, artist) is expected


def test_chinese_lyrics_with_some_english_are_chinese():
    lines = ["我們在路口等一班車", "今天的風有一點冷", "say goodbye", "明天見"]
    assert detect_from_lyrics(lines) is Language.ZH


def test_english_lyrics_with_a_chinese_word_are_english():
    lines = ["we walk along the river at night", "and the lights are fading slowly", "再見"]
    assert detect_from_lyrics(lines) is Language.EN


def test_word_count_not_letter_count_decides():
    # 6 Han characters vs 4 English words (many letters): still Chinese.
    assert detect_from_lyrics(["一起走到天亮", "everything everybody everywhere everyday"]) is Language.ZH


def test_japanese_lyrics_with_kanji_are_other():
    lines = ["今日は雨が降っている", "傘をさして歩こう"]
    assert detect_from_lyrics(lines) is Language.OTHER


def test_korean_lyrics_are_other():
    assert detect_from_lyrics(["오늘은 비가 와요", "우산을 써요"]) is Language.OTHER


def test_a_little_kana_does_not_flip_chinese():
    lines = ["我們在路口等一班車今天的風有一點冷明天見再說吧好不好", "の"]
    assert detect_from_lyrics(lines) is Language.ZH


def test_nothing_to_judge_returns_none():
    assert detect_from_lyrics(["", "123 456", "♪ ♪"]) is None


def test_apostrophes_stay_one_word():
    assert detect_from_lyrics(["don't you know it's late"]) is Language.EN
