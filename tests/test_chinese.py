import pytest

from lyricfloat.lyrics.chinese import (
    ChineseConverter,
    apply_vocabulary,
    ensure_vocabulary_file,
    load_vocabulary,
    parse_vocabulary,
)
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.sources.base import LyricSource
from lyricfloat.models import LyricLine, Lyrics, TrackInfo


class SimplifiedSource(LyricSource):
    name = "simplified"
    simplified = True

    async def search(self, track):
        return []


class OtherSource(LyricSource):
    name = "other"
    simplified = False

    async def search(self, track):
        return []


@pytest.fixture(scope="module")
def cc():
    return ChineseConverter()


@pytest.fixture
def pipe(cc):
    return OpenCCPipeline(converter=cc)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("软件更新", True),
        ("这里是台湾", True),
        ("请让一下", True),
        ("台灣的歌", False),  # 台 is shared, 灣 is traditional-only: whole string is traditional
        ("這裡很安靜", False),
        ("皇后", True),  # shared characters only: flagged by the tables (SPEC v1.11), converts to itself
        ("Hello world", False),
        ("", False),
        ("   ", False),
        ("台北", True),  # only shared characters: converted to the formal 臺北, accepted by the spec
    ],
)
def test_is_simplified(cc, text, expected):
    assert cc.is_simplified(text) is expected


def test_default_mode_replaces_taiwan_vocabulary(pipe):
    assert pipe.convert_line("软件更新") == "軟體更新"


def test_s2tw_mode_converts_characters_only(cc):
    pipe = OpenCCPipeline(mode="s2tw", converter=cc)
    assert pipe.convert_line("软件更新") == "軟件更新"


def test_char_only_output_uses_taiwan_glyphs(pipe):
    assert pipe.display_text("这里") == "這裡"  # not 這裏


def test_traditional_line_is_left_alone(pipe):
    assert pipe.convert_line("台灣的歌") == "台灣的歌"


def test_force_converts_even_shared_character_lines(pipe):
    assert pipe.convert_line("台北", force=True) == "臺北"


def test_blank_lines_are_untouched(pipe):
    assert pipe.convert_line("", force=True) == ""


def test_simplified_source_converts_every_line(pipe):
    lyrics = Lyrics((LyricLine(1.0, "软件更新"), LyricLine(2.0, "Hello"), LyricLine(3.0, "")), synced=True)
    out = pipe.process(lyrics, SimplifiedSource())
    assert [line.text for line in out.lines] == ["軟體更新", "Hello", ""]
    assert [line.start for line in out.lines] == [1.0, 2.0, 3.0]
    assert out.synced


def test_other_source_converts_only_simplified_lines(pipe):
    lyrics = Lyrics((LyricLine(1.0, "这里是台湾"), LyricLine(2.0, "台灣的歌"), LyricLine(3.0, "English line")), synced=True)
    out = pipe.process(lyrics, OtherSource())
    assert [line.text for line in out.lines] == ["這裡是臺灣", "台灣的歌", "English line"]


def test_query_to_simplified_source_goes_through_t2s(pipe):
    track = TrackInfo("長夜漫漫", "範例樂團", "專輯", 200.0)
    q = pipe.prepare_query(track, SimplifiedSource())
    assert (q.title, q.artist, q.album) == ("长夜漫漫", "范例乐团", "专辑")
    assert q.duration == 200.0


def test_query_to_other_source_is_unchanged(pipe):
    track = TrackInfo("長夜漫漫", "範例樂團")
    assert pipe.prepare_query(track, OtherSource()) is track


def test_display_track_converts_only_simplified_fields(pipe):
    track = TrackInfo("请让一下", "範例樂團", "", 100.0, "Spotify.exe")
    shown = pipe.display_track(track)
    assert (shown.title, shown.artist) == ("請讓一下", "範例樂團")
    assert shown.source_app == "Spotify.exe"


def test_vocabulary_parsing_skips_comments_and_sorts_longest_first():
    pairs = parse_vocabulary(["# comment", "", "甲乙=甲丙", "甲乙丁=戊", "bad line", "same=same", " 空白 = 空格 "])
    assert pairs == [("甲乙丁", "戊"), ("甲乙", "甲丙"), ("空白", "空格")]


def test_vocabulary_fixes_conversion_result(cc):
    pipe = OpenCCPipeline(vocabulary=[("軟體", "軟件")], converter=cc)
    assert pipe.convert_line("软件更新") == "軟件更新"


def test_vocabulary_is_not_applied_to_untouched_traditional_text(cc):
    pipe = OpenCCPipeline(vocabulary=[("台灣", "臺灣")], converter=cc)
    assert pipe.convert_line("台灣的歌") == "台灣的歌"


def test_apply_vocabulary_longest_first():
    assert apply_vocabulary("甲乙丁", [("甲乙丁", "戊"), ("甲乙", "X")]) == "戊"


def test_vocabulary_file_template_and_loading(tmp_path):
    path = ensure_vocabulary_file(tmp_path / "LyricFloat" / "vocabulary.txt")
    assert path.exists()
    assert load_vocabulary(path) == []  # template is comments only
    path.write_text(path.read_text(encoding="utf-8") + "甲=乙\n", encoding="utf-8")
    assert load_vocabulary(path) == [("甲", "乙")]
    ensure_vocabulary_file(path)  # never overwrites
    assert load_vocabulary(path) == [("甲", "乙")]


def test_missing_vocabulary_file_is_empty(tmp_path):
    assert load_vocabulary(tmp_path / "nope.txt") == []


@pytest.mark.parametrize("text", ["私の国へ帰ろう", "우리나라 国", "気持ちが伝わる"])
def test_japanese_and_korean_lines_are_never_converted(pipe, text):
    assert not pipe.cc.is_simplified(text)
    assert pipe.convert_line(text) == text
    assert pipe.convert_line(text, force=True) == text  # not even from a simplified source
    assert pipe.display_text(text) == text


def test_shared_character_strings_convert_to_themselves(pipe):
    # The table check flags these, but phrase-aware conversion leaves them unchanged.
    assert pipe.convert_line("皇后") == "皇后"


def test_detection_loads_no_dictionary():
    cc = ChineseConverter()
    assert cc.is_simplified("软件更新") and not cc.is_simplified("台灣的歌")
    assert cc.loads == 0 and cc.loaded() == set()


def test_a_song_loads_the_big_dictionary_once_and_releases_it():
    cc = ChineseConverter()
    pipe = OpenCCPipeline(converter=cc)
    lines = tuple(LyricLine(float(i), text) for i, text in enumerate(["软件更新", "鼠标和内存", "这里是台湾"] * 10))
    pipe.process(Lyrics(lines, synced=True), SimplifiedSource())
    assert cc.loads == 1  # thirty lines, one load
    assert cc.loaded() == set()  # released after the song


def test_one_off_conversion_is_released_and_t2s_stays():
    cc = ChineseConverter()
    pipe = OpenCCPipeline(converter=cc)
    assert pipe.display_text("请让一下") == "請讓一下"
    assert cc.loaded() == set()
    pipe.prepare_query(TrackInfo("長夜", "範例", "", 1.0), SimplifiedSource())
    assert cc.loaded() == {"t2s"}  # small and used for every NetEase query: kept
