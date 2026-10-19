import pytest

from lyricfloat.lyrics.normalize import normalize_artist, normalize_title, split_artists


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Paper Lanterns", "paper lanterns"),
        ("Paper Lanterns (Remastered 2011)", "paper lanterns"),
        ("Paper Lanterns [Live]", "paper lanterns"),
        ("Paper Lanterns (feat. Someone Else)", "paper lanterns"),
        ("Paper Lanterns feat. Someone Else", "paper lanterns"),
        ("Paper Lanterns ft. Someone Else", "paper lanterns"),
        ("Paper Lanterns - 2011 Remaster", "paper lanterns"),
        ("Paper Lanterns - Live at the Hall", "paper lanterns"),
        ("Paper Lanterns – Radio Edit", "paper lanterns"),
        ("Paper Lanterns — Acoustic", "paper lanterns"),
        ("Paper Lanterns - Single Version", "paper lanterns"),
        ("Paper Lanterns - Club Mix", "paper lanterns"),
        # YouTube video titles (SPEC v1.30).
        ("AIYOU - Eve Music Video", "aiyou"),
        ("Paper Lanterns - Official MV", "paper lanterns"),
        ("Paper Lanterns – Official M/V", "paper lanterns"),
        ("Paper Lanterns - Lyric Video", "paper lanterns"),
        ("Paper Lanterns - Official Audio", "paper lanterns"),
        ("Paper Lanterns - Visualizer", "paper lanterns"),
        ("Videotape", "videotape"),  # outside a dash segment: a title word
        # Not a version note: the dash segment stays.
        ("Paper Lanterns - Part Two", "paper lanterns - part two"),
        # A bare hyphen belongs to the title.
        ("Anti-Hero", "anti-hero"),
        # Version keywords outside brackets/dash segments are title words.
        ("Live Forever", "live forever"),
        ("Remix the World", "remix the world"),
    ],
)
def test_title_version_notes(raw, expected):
    assert normalize_title(raw) == expected


def test_full_width_is_folded_and_brackets_removed():
    assert normalize_title("ＰＡＰＥＲ　Ｌａｎｔｅｒｎｓ（Ｌｉｖｅ）") == "paper lanterns"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("紙燈籠 (Paper Lanterns)", "紙燈籠"),
        ("紙燈籠（現場版）", "紙燈籠"),
        ("紙燈籠【官方MV】", "紙燈籠"),
        ("紙燈籠〔重製〕", "紙燈籠"),
        ("紙燈籠 - 現場版", "紙燈籠"),
        ("紙燈籠 - 電影版", "紙燈籠"),
    ],
)
def test_chinese_titles(raw, expected):
    assert normalize_title(raw) == expected


def test_book_title_marks_and_quotes_are_kept():
    assert normalize_title("《紙燈籠》") == "《紙燈籠》"
    assert normalize_title("「紙燈籠」") == "「紙燈籠」"


def test_nested_brackets_are_peeled():
    assert normalize_title("Paper Lanterns (Live (Deluxe))") == "paper lanterns"


def test_title_that_is_only_brackets_falls_back():
    assert normalize_title("(Interlude)") == "(interlude)"


def test_whitespace_is_collapsed():
    assert normalize_title("  Paper   Lanterns  ") == "paper lanterns"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("The Example Band", ["the example band"]),
        ("Alpha, Beta", ["alpha", "beta"]),
        ("Alpha & Beta", ["alpha", "beta"]),
        ("Alpha/Alpha", ["alpha"]),
        ("Alpha; Beta", ["alpha", "beta"]),
        ("Alpha × Beta", ["alpha", "beta"]),
        ("Alpha feat. Beta", ["alpha", "beta"]),
        ("Alpha ft. Beta", ["alpha", "beta"]),
        ("Alpha featuring Beta", ["alpha", "beta"]),
        ("甲、乙", ["甲", "乙"]),
        ("Alpha (UK)", ["alpha"]),
        ("ＡＬＰＨＡ＆ＢＥＴＡ", ["alpha", "beta"]),
        # "feat" inside a name is not a separator.
        ("Featherstone", ["featherstone"]),
        ("", []),
    ],
)
def test_split_artists(raw, expected):
    assert split_artists(raw) == expected


def test_normalize_artist_joins_names():
    assert normalize_artist("Alpha & Beta") == "alpha / beta"


# --- bilingual titles (SPEC v1.10) ---------------------------------------------------

from lyricfloat.lyrics.normalize import split_bilingual, title_variants  # noqa: E402


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("紙燈籠 Paper Lanterns", ("紙燈籠", "paper lanterns")),
        ("Paper Lanterns 紙燈籠", ("紙燈籠", "paper lanterns")),
        ("紙燈籠 Paper Lanterns (Live)", ("紙燈籠", "paper lanterns")),
        ("紙燈籠", None),
        ("Paper Lanterns", None),
        ("紙燈籠 2", None),  # a number is not an English title
        ("紙燈籠 (Paper Lanterns)", None),  # the bracket rule already reduced it
    ],
)
def test_split_bilingual(raw, expected):
    assert split_bilingual(raw) == expected


def test_title_variants_only_grow_for_bilingual_titles():
    assert title_variants("紙燈籠 Paper Lanterns") == ["紙燈籠 paper lanterns", "紙燈籠", "paper lanterns"]
    assert title_variants("Paper Lanterns") == ["paper lanterns"]


# -- YouTube video titles (SPEC v1.31) -----------------------------------------------------


@pytest.mark.parametrize(
    "title, artist, expected",
    [
        ("TYSON YOSHI -  你不會一輩子的愛上我 Glad That We Met (Official Music Video)", "Tyson Yoshi",
         "你不會一輩子的愛上我 Glad That We Met (Official Music Video)"),
        ("Paper Lanterns - The Example Band", "The Example Band", "Paper Lanterns"),
        # Several artists joined by a letter x, the player naming only one (SPEC v1.32).
        ("張瑚HU X 趙翊帆YI94 - 妳 Rosie (Official Audio)", "張瑚HU", "妳 Rosie (Official Audio)"),
        ("BK-【不管過了多久都會等妳】audio （Prod. EVOMUSIC）", "BK", "不管過了多久都會等妳"),
        ("The Example Band [Paper Lanterns] Official MV", "The Example Band", "Paper Lanterns"),
        # Left alone: nothing to cut, or cutting would leave nothing.
        ("Paper Lanterns", "The Example Band", "Paper Lanterns"),
        ("Paper Lanterns - Part Two", "The Example Band", "Paper Lanterns - Part Two"),
        ("【MV】Paper Lanterns", "The Example Band", "【MV】Paper Lanterns"),
        ("The Example Band", "The Example Band", "The Example Band"),  # a song named after the band
        # SPEC v1.37: the artist with a Latin alias, a dash with space on one side, the title in 【】.
        ("王水源 Lifloo -【保持呼吸】Official Music Video", "王水源", "保持呼吸"),
        ("Madonna - Music (Official Video)", "Madonna", "Music (Official Video)"),  # "Music" alone is a title
        ("Anti-Hero", "Taylor Swift", "Anti-Hero"),  # no space by the dash: one word
        # SPEC v1.66: video words closing the title, outside brackets, go.
        ("BIGBANG - HARU HARU(하루하루) M/V", "BIGBANG", "HARU HARU(하루하루)"),
        ("The Example Band 'Paper Lanterns' Official MV", "Label", "The Example Band 'Paper Lanterns'"),
        ("Video Killed the Radio Star", "The Buggles", "Video Killed the Radio Star"),  # not at the end
        ("The Example Band - Music Video", "The Example Band", "The Example Band - Music Video"),  # nothing left
        # Two segments left: the closing video words mark the second as a note normalize_title drops.
        ("Paper Lanterns - Someone,Another M/V", "Label", "Paper Lanterns - Someone,Another M/V"),
    ],
)
def test_searchable_title(title, artist, expected):
    from lyricfloat.lyrics.normalize import searchable_title

    assert searchable_title(title, artist) == expected


@pytest.mark.parametrize(
    "title, artist, expected",
    [
        ("張瑚HU X 趙翊帆YI94 - 妳 Rosie (Official Audio)", "張瑚HU", ["趙翊帆yi94"]),
        ("TYSON YOSHI - 你不會一輩子的愛上我", "Tyson Yoshi", []),
        ("Paper Lanterns", "The Example Band", []),
        ("王水源 Lifloo -【保持呼吸】Official Music Video", "王水源", []),  # an alias is nobody else
        ("AIYOU - Eve Music Video", "Eve", []),
    ],
)
def test_credited_artists(title, artist, expected):
    from lyricfloat.lyrics.normalize import credited_artists

    assert credited_artists(title, artist) == expected


@pytest.mark.parametrize(
    "title, channel, expected",
    [
        # SPEC v1.66: a label's upload; "ft." with no space is cut by normalize_title once spaced.
        ("FRαNKIE阿法 - 沒有妳在 Not Around ft.王ADEN (Dir.by @VincentWan9)", "BROYA ICEBOX",
         ("FRαNKIE阿法", "沒有妳在 Not Around ft. 王ADEN (Dir.by @VincentWan9)")),
        ("江辰 - 鏽『給你的情詩生了鏽』【Lyrics Video】", "Boba Beats", ("江辰", "鏽『給你的情詩生了鏽』【Lyrics Video】")),
        ("高爾宣 OSN -【最後一次】Official MV", "SKRpresents 陶山音樂", ("高爾宣 OSN", "最後一次")),  # the song in its 【】
        # Left to searchable_title: a segment names the channel's artist.
        ("TYSON YOSHI - 你不會一輩子的愛上我", "Tyson Yoshi", None),
        ("張瑚HU X 趙翊帆YI94 - 妳 Rosie", "張瑚HU", None),
        # "Song - Artists": the channel's name (here glued to a Latin alias) is after the dash.
        ("瑪格莉塔 Margarita - 莫宰羊Goater,派偉俊Patrick Brasca,婁峻碩SHOU * M/V", "莫宰羊", None),
        ("Paper Lanterns - The Example Band with Friends and Strings", "The Example Band", None),
        ("Paper Lanterns - Banded Together Under One Roof", "Band",
         ("Paper Lanterns", "Banded Together Under One Roof")),  # a Latin name counts as whole words only
        # Not "Artist - Song": no dash, more than two segments, nobody before the dash, nothing after it.
        ("Paper Lanterns", "Label", None),
        ("王齐铭 - 夜郎溪 - 夜郎溪", "纯正中专代言人小渡", None),
        ("【MV】 - Paper Lanterns", "Label", None),
        ("The Example Band - Official Music Video", "Label", None),
        ("Anti-Hero", "Label", None),
    ],
)
def test_dash_credit(title, channel, expected):
    from lyricfloat.lyrics.normalize import dash_credit

    assert dash_credit(title, channel) == expected


def test_a_glued_feat_credit_is_cut_from_the_song():
    from lyricfloat.lyrics.normalize import dash_credit, normalize_title, split_bilingual

    _, song = dash_credit("FRαNKIE阿法 - 沒有妳在 Not Around ft.王ADEN", "BROYA ICEBOX")
    assert normalize_title(song) == "沒有妳在 not around"
    assert split_bilingual(song) == ("沒有妳在", "not around")
    assert normalize_title("沒有妳在 Not Around ft.王ADEN") == "沒有妳在 not around ft.王aden"  # cache keys unchanged


from lyricfloat.lyrics.normalize import language_version, other_language  # noqa: E402


@pytest.mark.parametrize(
    "title, lang",
    [
        ("HaruHaru (一天一天) (Japanese Version_BIGBANG WORLD TOUR 2015~2016 [MADE] IN JAPAN)", "ja"),
        ("Love Talk (English Version)", "en"),
        ("Love Talk (English ver.)", "en"),
        ("IF YOU -JP Ver.-", "ja"),
        ("Like Crazy (Eng Ver)", "en"),
        ("雨天 (國語版)", "zh"),
        ("富士山下 (粵語版)", "yue"),
        ("Love Talk", None),
        ("Remastered Version", None),
        ("現場版", None),
        ("Kreng Version", None),  # a word that only ends like a language
    ],
)
def test_language_version(title, lang):
    # SPEC v1.70: language versions carry other lyrics.
    assert language_version(title) == lang


def test_other_language():
    assert other_language("HARU HARU", "HaruHaru (Japanese Version)")
    assert not other_language("Like Crazy (English Version)", "Like Crazy (English ver.)")
    assert not other_language("Like Crazy (English Version)", "Like Crazy")  # the player's says more: fine


from lyricfloat.lyrics.normalize import live_version, names_live  # noqa: E402


@pytest.mark.parametrize(
    "title, live",
    [
        ("最后一班车 (Live)", True),
        ("Boss Life (狂放人生) - Live", True),
        ("Love Me Like This (Band LIVE)", True),
        ("But U-乃万NINEONE # (Live|纯享版cut)", True),
        ("現場版", True),
        ("Live Forever", False),  # the song's own name
        ("I Live", False),
        ("Delivery (Remix)", False),
    ],
)
def test_live_version(title, live):
    # SPEC v1.75: only a version note says a hit is live.
    assert live_version(title) is live


def test_a_players_title_names_live_anywhere():
    assert names_live("Quanzo - 心疾如焚｜純享版｜EP7")
    assert not names_live("PPlin - 最後一班車(Official Music Video)")
    assert not names_live("Alive (Official MV)")
