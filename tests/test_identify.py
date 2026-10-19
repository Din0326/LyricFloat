"""SPEC v1.34: the song behind a browser video's title. Titles are real video titles; no lyrics."""

import pytest

from lyricfloat.lyrics.cache import EDITED_SOURCE
from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.identify import chinese_part, confirms, is_browser, not_a_song, video_queries
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.sources.netease import NeteaseSource
from lyricfloat.lyrics.service import LyricResolver
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.models import Candidate, LyricResult, ResultKind, TrackInfo

SYNCED = "[00:12.00]sung line one\n[00:16.00]sung line two"
CC = ChineseConverter()


def t2s(text):
    return CC.convert("t2s", text)


def video(title, channel, duration, app="Chrome"):
    return TrackInfo(title, channel, "", duration, app)


def hit(title, artist, duration, synced=SYNCED, source="lrclib", cid="1"):
    return Candidate(source, cid, title, artist, "", duration, synced)


# -- queries --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("两难-加木『你到底在为谁等待』【动态歌词】#流行歌曲", ["两难-加木", "你到底在为谁等待", "两难-加木 你到底在为谁等待"]),
        ('1 AM - Thomeboydontkill "有時會控制不住的想你"［中文歌詞 - lyrics video］', ["1 AM - Thomeboydontkill", "有時會控制不住的想你", "1 AM - Thomeboydontkill 有時會控制不住的想你"]),
        # SPEC v1.67: 【】 before the text outside it.
        ("BK-【不管過了多久都會等妳】audio （Prod. EVOMUSIC）", ["不管過了多久都會等妳", "BK", "BK- 不管過了多久都會等妳"]),
        ("MARK 마크 'Flight to NYC' | The Firstfruit Playlist", ["MARK 마크 Flight to NYC", "MARK 마크", "The Firstfruit Playlist", "Flight to NYC"]),
        ("Don't Stop Me Now - Queen", ["Don't Stop Me Now - Queen"]),  # an apostrophe is not a quote
        # SPEC v1.38: a space where Chinese meets Latin, or NetEase finds nothing.
        ("Bu$Y & Ye!!ow - 【追追追追Chasing on You】 (Official Music Video)",
         ["追追追追 Chasing on You", "Bu$Y & Ye!!ow", "Bu$Y & Ye!!ow - 追追追追 Chasing on You"]),
        # SPEC v1.40: 《》 holds the song and goes first; picture quality is noise.
        ("【1080P 60帧】谁懂这个视角!心跳直拍视角TANK《如果我变成回忆》【2025B站跨年晚会单品】",
         ["如果我变成回忆", "2025B 站跨年晚会单品", "谁懂这个视角!心跳直拍视角 TANK《如果我变成回忆》",
          "谁懂这个视角!心跳直拍视角 TANK《如果我变成回忆》 2025B 站跨年晚会单品"]),
        # SPEC v1.41: a hashtag inside 《》 is the song; hashtags elsewhere go, brackets stay whole.
        ("【纯享】终极回忆杀!#Ella #TANK《#专属天使》梦回台湾偶像剧白月光 |《时光音乐会•老友记》",
         ["专属天使", "时光音乐会•老友记", "终极回忆杀! 《 》梦回台湾偶像剧白月光",
          "终极回忆杀! 《 》梦回台湾偶像剧白月光 |《时光音乐会•老友记》"]),  # v1.67: 纯享 is a show's tag
        # SPEC v1.42: each part between "|" on its own; a TV show's name and air date sink the search.
        # SPEC v1.52: the channel, air date and 방송 are noise as well.
        ("CRASH - ALLDAY PROJECT | SBS 261004 방송", ["CRASH - ALLDAY PROJECT"]),
        # SPEC v1.46: "│" splits too; a quoted song goes with the singer written before it.
        ("[단독샷캠4K] 올데이 프로젝트 'CRASH' 단독샷 별도녹화│ALLDAY PROJECT ONE TAKE STAGE│@SBS Inkigayo 261004",
         ["올데이 프로젝트 CRASH", "올데이 프로젝트 단독샷 별도녹화", "ALLDAY PROJECT ONE TAKE STAGE", "단독샷캠4K"]),
        # SPEC v1.52: TV stages: @mentions, show names, episodes and air dates go.
        ("NewJeans - OMG @인기가요 inkigayo 20241006", ["NewJeans - OMG"]),
        ("NewJeans - OMG #엠카운트다운 EP.880 | Mnet 241010 방송", ["NewJeans - OMG"]),
        ("Love@First Sight - Someone", ["Love@First Sight - Someone"]),  # no mention inside a word
        ("孫盛希「jagi」歌詞付き Lyrics", ["孫盛希", "jagi", "孫盛希 jagi"]),
        ("G.E.M.【再見 GOODBYE】Official MV [HD] 鄧紫棋", ["再見 GOODBYE", "G.E.M. 鄧紫棋", "HD", "G.E.M. 再見 GOODBYE HD 鄧紫棋"]),
        # SPEC v1.60: a spaced slash splits like "|"; THE FIRST TAKE is the channel's name, not the song's.
        ("星野源 - Comedy / THE FIRST TAKE", ["星野源 - Comedy"]),
        ("AC/DC - Thunderstruck", ["AC/DC - Thunderstruck"]),
    ],
)
def test_video_queries(title, expected):
    assert video_queries(title) == expected


# -- confirmation ---------------------------------------------------------------------


def test_the_title_has_to_name_both_the_song_and_an_artist():
    track = video("两难-加木『你到底在为谁等待』【动态歌词】#流行歌曲", "流行音乐", 170.0)
    assert confirms(track, hit("两难", "加木", 170.0))
    assert not confirms(track, hit("两难", "梁靜茹", 170.0))  # same title, someone else
    assert not confirms(track, hit("你與我 他和她", "加木", 171.0))  # the artist, another song


def test_title_and_artist_must_be_separate_pieces():
    track = video('1 AM - Thomeboydontkill "有時會控制不住的想你"', "Magic Fox", 187.0)
    assert not confirms(track, hit("thomeboydontkill", "THOME", 187.0))  # THOME lies inside the title
    assert confirms(track, hit("1 AM", "thomeboydontkill", 187.0))


def test_scripts_and_the_channel_field_count():
    track = video("你不會一輩子的愛上我 Glad That We Met (Official Music Video)", "Tyson Yoshi", 219.0)
    assert confirms(track, hit("你不会一辈子的爱上我", "Tyson Yoshi", 197.0), fold=t2s)  # NetEase's script, an MV
    assert not confirms(track, hit("你不会一辈子的爱上我", "Tyson Yoshi", 197.0))  # without folding, no match


def test_a_name_in_brackets_counts_as_the_artist():
    # SPEC v1.47: NetEase credits K-pop singers as 中文名（English）.
    track = video("IVE 아이브 LIZ 리즈 'Unreal' Special Clip", "IVE", 164.0)
    assert confirms(track, hit("Unreal (LIZ Solo)", "丽兹（LIZ）", 162.0, source="netease"))
    # A catalogue's number in brackets is no name: "16" in a title does not confirm Ive (16).
    assert not confirms(video("Unreal 2016 stage", "fan cam", 164.0), hit("Unreal", "Ive (16)", 162.0))


def test_marks_around_a_name_are_not_part_of_it():
    # SPEC v1.59: LRCLIB credits NINEONE赵馨玥's song to "NINEONE#".
    track = video("NINEONE赵馨玥 - BUT U (Lyrics)", "7clouds", 225.0)
    assert confirms(track, hit("BUT U", "NINEONE#", 225.0))
    assert confirms(track, hit("But U", "NINEONE#乃万", 225.0, source="netease"))  # marks between the scripts
    assert not confirms(video("Feeling Good - Someone", "x", 200.0), hit("Feeling Good", "A-Lin", 200.0))  # no "Lin" alone
    # SPEC v1.73: 林宥嘉's MV is 4 s shorter than the song.
    assert confirms(video("林宥嘉 Yoga Lin [自然醒 Wake Up] Official MV", "華研國際", 240.8), hit("自然醒", "Yoga Lin", 245.0))
    # SPEC v1.71: a space between the scripts too: LRCLIB credits "TANK 呂建忠"; the label's video names TANK.
    tank = video("TANK [給我你的愛 Give me your love] Official Music Video", "華研國際", 275.1)
    assert confirms(tank, hit("給我你的愛", "TANK 呂建忠", 276.0))


def test_a_chinese_name_run_into_an_english_one_counts_either_way():
    # SPEC v1.48: NetEase credits 弹壳Danko; the show's video names only 弹壳.
    track = video(
        "【😎纯享】弹壳《Boss Life(狂放人生)》真Boss驾到 一张嘴气场两米八 | EP11 说唱巅峰对决2026 The Rap of China 2026 | iQIYI潮综艺",
        "iQIYI 潮综艺 - Get the iQIYI APP",
        241.8,
    )
    assert confirms(track, hit("Boss Life (狂放人生) (Live)", "弹壳Danko", 240.0, source="netease"))
    assert confirms(video("Danko - Boss Life (Live)", "rap fan", 241.8), hit("Boss Life", "弹壳Danko", 240.0))
    assert not confirms(track, hit("Boss Life (狂放人生)", "风聿", 228.0, source="netease"))  # a cover still does not
    # SPEC v1.53: the title may not be the other half of the name: "ICE" in ICE杨长青 is not 〈ICE〉.
    assert not confirms(video("ICE杨长青【Want U Back】Official MV", "滾石唱片 ROCK RECORDS", 162.0),
                        hit("ICE (Live)", "ICE杨长青", 74.0, source="netease"))
    assert confirms(video("ICE杨长青 - ICE (Live)", "rap fan", 74.0), hit("ICE (Live)", "ICE杨长青", 74.0))
    # SPEC v1.67: nor a name in the artist's brackets: the video's "G.E.M." is 鄧紫棋 (G.E.M).
    gem = video("G.E.M.【再見 GOODBYE】Official MV [HD] 鄧紫棋", "GEMblog", 215.7)
    assert not confirms(gem, hit("G.E.M. (GET EVERYBODY MOVING)", "鄧紫棋 (G.E.M)", 210.0), t2s)
    assert confirms(gem, hit("再见", "G.E.M.邓紫棋", 213.0, source="netease"), t2s)
    # SPEC v1.68: either half of a bilingual title: LRCLIB writes a dash the video does not.
    duet = video("周興哲Eric Chou × 單依純《愛我的時候 When You Loved Me》Official Music Video", "周興哲 Eric Chou", 222.6)
    assert confirms(duet, hit("愛我的時候 - When You Loved Me", "Eric Chou", 225.0))


def test_kana_in_the_title_does_not_keep_chinese_from_folding():
    # SPEC v1.51: the app's fold leaves text with kana or hangul alone; Han runs are folded one by one.
    fold = lambda text: OpenCCPipeline(converter=CC).comparison_text(text, NeteaseSource)  # noqa: E731
    track = video("孫盛希「jagi」歌詞付き Lyrics", "Lyrics JP", 220.0)
    assert confirms(track, hit("jagi (feat. KIRE)", "孙盛希 / KIRE", 220.0, source="netease"), fold)
    assert confirms(video("孫盛希 손성희 'jagi' LIVE", "KBS Kpop", 220.0), hit("jagi", "孙盛希", 220.0), fold)  # hangul too


# SPEC v1.73: the video may be 15 s shorter (an MV edit), no more; 3 min longer.
@pytest.mark.parametrize("length, ok", [(170.0, True), (173.5, True), (185.0, True), (185.5, False), (60.0, True), (167.5, True), (400.0, False)])
def test_the_video_may_be_an_mv_but_not_much_shorter(length, ok):
    track = video("两难-加木", "流行音乐", 170.0)
    assert confirms(track, hit("两难", "加木", length)) is ok


def test_one_character_pieces_are_too_weak():
    # SPEC v1.57 (Din, overriding v1.34's "at least 2 characters"): only as a part of its own.
    assert not confirms(video("妳是我的唯一 - 某人 Live", "頻道", 200.0), hit("妳", "某人", 200.0))


def test_long_browser_videos_are_not_songs():
    assert not_a_song(video("準備兩百萬參加台灣最大撲克賽事", "Gshan吉祥", 4473.3))
    assert not not_a_song(video("A long song", "Band", 1200.0, app="Spotify.exe"))  # players play long songs
    assert is_browser("Chrome") and is_browser("MSEdge") and not is_browser("Spotify.exe")


def test_browser_videos_without_an_artist_are_not_songs():
    # SPEC v1.43: Douyin and Instagram name no artist; YouTube always names the channel.
    assert not_a_song(video("抖音-记录美好生活", "", 298.2))
    assert not_a_song(video("Instagram", "", 81.4, app="MSEdge"))
    assert not not_a_song(video("慢走不送", "J.Sheon", 267.0))
    assert not not_a_song(video("A Song", "", 200.0, app="Spotify.exe"))  # players are left alone


# -- resolver ---------------------------------------------------------------------------


class TextSearchable(LyricSource):
    """Finds nothing by title and artist; free text finds what `by_text` says."""

    name = "lrclib"

    def __init__(self, by_text, fail=False):
        self.by_text = by_text
        self.fail = fail
        self.texts: list[str] = []

    async def search(self, track):
        return []

    async def search_text(self, text):
        self.texts.append(text)
        if self.fail:
            raise SourceError("down")
        return list(self.by_text.get(text, []))


LIANGNAN = video("两难-加木『你到底在为谁等待』【动态歌词】#流行歌曲", "流行音乐", 170.0)


async def test_a_browser_video_is_identified_and_its_lyrics_used():
    src = TextSearchable({"两难-加木": [hit("两难", "梁靜茹", 283.0, cid="x"), hit("两难", "加木", 170.0, cid="ok")]})
    result = await LyricResolver([src]).resolve(LIANGNAN)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"
    assert "identified" in result.notes and not result.version_mismatch
    assert result.track == LIANGNAN  # the screen keeps the video's title


async def test_an_identified_mv_is_a_version_of_another_length():
    src = TextSearchable({"两难-加木": [hit("两难", "加木", 140.0)]})
    result = await LyricResolver([src]).resolve(LIANGNAN)
    assert result.version_mismatch and result.timeline_checked


async def test_players_other_than_browsers_are_not_identified():
    src = TextSearchable({"两难-加木": [hit("两难", "加木", 170.0)]})
    track = TrackInfo(LIANGNAN.title, LIANGNAN.artist, "", 170.0, "Spotify.exe")
    assert (await LyricResolver([src]).resolve(track)).kind is ResultKind.NOT_FOUND
    assert src.texts == []


async def test_nothing_confirmed_is_still_not_found_and_remembered():
    src = TextSearchable({"两难-加木": [hit("两难", "梁靜茹", 283.0)]})
    resolver = LyricResolver([src])
    assert (await resolver.resolve(LIANGNAN)).kind is ResultKind.NOT_FOUND
    assert "session_miss" in (await resolver.resolve(LIANGNAN)).notes


async def test_a_broken_identifying_search_is_asked_again():
    src = TextSearchable({}, fail=True)
    resolver = LyricResolver([src])
    first = await resolver.resolve(LIANGNAN)
    assert first.kind is ResultKind.NOT_FOUND and "source_failed" in first.notes


async def test_long_videos_are_not_queried():
    src = TextSearchable({})
    result = await LyricResolver([src]).resolve(video("準備兩百萬參加台灣最大撲克賽事", "Gshan吉祥", 4473.3))
    assert result.kind is ResultKind.NOT_A_SONG and src.texts == []


async def test_videos_without_an_artist_are_not_queried():
    src = TextSearchable({})
    result = await LyricResolver([src]).resolve(video("抖音-记录美好生活", "", 298.2))
    assert result.kind is ResultKind.NOT_A_SONG and src.texts == []


# -- a title holding only another artist's name (SPEC v1.35) ------------------------------

OSN = video("OSN)", "Dizzy Dizzo 蔡詩芸", 191.2)  # YouTube's report for "Dizzy Dizzo 蔡詩芸 ft. 高爾宣OSN【Tides】"


def test_a_hit_credited_to_both_artists_counts():
    from lyricfloat.lyrics.identify import credit_query, credits_both

    assert credit_query(OSN) == "Dizzy Dizzo 蔡詩芸 OSN"
    assert credits_both(OSN, hit("Tides", "Dizzy Dizzo 蔡詩芸 feat. 高爾宣 OSN", 180.0))
    assert not credits_both(OSN, hit("紫外線", "Dizzy Dizzo 蔡詩芸", 196.0))  # OSN not credited
    assert not credits_both(OSN, hit("Hold On", "高爾宣 OSN", 180.0))  # Dizzy Dizzo not credited
    assert not credits_both(video("Dizzy Dizzo", "Dizzy Dizzo 蔡詩芸", 200.0), hit("紫外線", "Dizzy Dizzo 蔡詩芸", 196.0))


async def test_the_only_song_by_both_artists_is_taken():
    src = TextSearchable({"Dizzy Dizzo 蔡詩芸 OSN": [hit("Tides", "Dizzy Dizzo 蔡詩芸 feat. 高爾宣 OSN", 180.0), hit("紫外線", "Dizzy Dizzo 蔡詩芸", 196.0, cid="2")]})
    result = await LyricResolver([src]).resolve(OSN)
    assert result.kind is ResultKind.SYNCED and result.candidate.title == "Tides" and result.version_mismatch


async def test_two_songs_by_both_artists_are_too_many():
    src = TextSearchable({"Dizzy Dizzo 蔡詩芸 OSN": [
        hit("Tides", "Dizzy Dizzo 蔡詩芸 feat. 高爾宣 OSN", 180.0),
        hit("Another Duet", "Dizzy Dizzo 蔡詩芸, 高爾宣 OSN", 190.0, cid="2"),
    ]})
    assert (await LyricResolver([src]).resolve(OSN)).kind is ResultKind.NOT_FOUND



async def test_a_video_titled_with_the_artists_alias_finds_its_song_not_one_named_after_the_artist():
    from lyricfloat.lyrics.sources.base import LyricSource as _Source

    class ByTitle(_Source):
        name = "netease"
        lyrics_inline = True

        def __init__(self):
            self.titles = []

        async def search(self, track):
            self.titles.append(track.title)
            return [hit("愛王水源", "王水源", 181.7, cid="wrong"), hit("保持呼吸", "王水源", 175.7, cid="right")]

    src = ByTitle()
    track = video("王水源 Lifloo -【保持呼吸】Official Music Video", "王水源", 207.3)
    result = await LyricResolver([src]).resolve(track)
    assert src.titles[0] == "保持呼吸"  # SPEC v1.37
    assert result.candidate.source_id == "right" and result.version_mismatch



async def test_a_simplified_video_title_is_also_asked_in_traditional_script():
    from lyricfloat.lyrics.pipeline import OpenCCPipeline

    class Traditional(LyricSource):
        """LRCLIB-like: lists the song under its traditional title only."""

        name = "lrclib"

        def __init__(self):
            self.texts = []

        async def search(self, track):
            return []

        async def search_text(self, text):
            self.texts.append(text)
            return [hit("如果我變成回憶", "Tank", 315.0)] if text == "如果我變成回憶" else []

    src = Traditional()
    track = video("【1080P 60帧】谁懂这个视角!心跳直拍视角TANK《如果我变成回忆》", "最美的夜", 377.5)
    result = await LyricResolver([src], pipeline=OpenCCPipeline(converter=CC)).resolve(track)
    assert src.texts[:2] == ["如果我变成回忆", "如果我變成回憶"]  # SPEC v1.40
    assert result.kind is ResultKind.SYNCED and result.version_mismatch and "identified" in result.notes


async def test_chinese_and_latin_are_asked_joined_as_well():
    # SPEC v1.55: "ICE 杨长青" (spaced, v1.38) finds nobody; the name is written ICE杨长青.
    src = TextSearchable({"ICE杨长青 - Want U Back": [hit("Want U Back", "ICE杨长青", 152.0, cid="ok")]})
    result = await LyricResolver([src]).resolve(video("ICE杨长青 - Want U Back (Lyrics)", "7clouds", 152.0))
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"
    assert src.texts[:2] == ["ICE 杨长青 - Want U Back", "ICE杨长青 - Want U Back"]


def test_a_channel_named_after_the_artist_is_the_artist():
    # SPEC v1.54: YouTube's "Artist - Topic" channels, "Artist Official", "ArtistVEVO".
    topic = LyricResolver.searchable(video("Want U Back", "ICE杨长青 - Topic", 152.0))
    assert (topic.title, topic.artist) == ("Want U Back", "ICE杨长青")
    mv = LyricResolver.searchable(video("ICE杨长青 - Want U Back [Official MV]", "ICE杨长青 Official", 160.0))
    assert (mv.title, mv.artist) == ("Want U Back [Official MV]", "ICE杨长青")
    # Players other than browsers keep their artist: Spotify has an artist called Topic.
    spotify = LyricResolver.searchable(TrackInfo("Breaking Me", "Topic - Topic", "", 166.0, "Spotify.exe"))
    assert spotify.artist == "Topic - Topic"


def test_the_manual_search_starts_from_the_song_a_video_was_found_under():
    # SPEC v1.76: the video's title and a label's channel find nothing typed back in.
    track = video("田馥甄 Hebe Tien [寂寞寂寞就好 Leave Me Alone] Official MV", "華研國際", 240.0)
    found = LyricResult(track, ResultKind.SYNCED, None, hit("寂寞寂寞就好", "Hebe Tien 田馥甄", 238.0), 0.9)
    terms = LyricResolver.search_terms(track, found)
    assert (terms.title, terms.artist) == ("寂寞寂寞就好", "Hebe Tien 田馥甄")
    # SPEC v1.77: nothing found, or the user's own edit: the whole title, no artist (free text).
    edited = LyricResult(track, ResultKind.SYNCED, None, Candidate(EDITED_SOURCE, "edited", track.title, track.artist))
    for result in (None, LyricResult(track, ResultKind.NOT_FOUND), edited):
        terms = LyricResolver.search_terms(track, result)
        assert (terms.title, terms.artist) == (track.title, "")
    # A music app's title and artist are the song's own, even when the pick was wrong.
    app = TrackInfo("Paper Lanterns", "The Example Band", "", 200.0, "Spotify.exe")
    picked = LyricResult(app, ResultKind.SYNCED, None, hit("Paper Lanterns (Live)", "The Example Band", 200.0), 0.9)
    assert LyricResolver.search_terms(app, picked) == app


class AnyText(LyricSource):
    """Free text finds every hit whose title the text holds; title and artist find nothing."""

    name = "lrclib"

    def __init__(self, hits):
        self.hits = hits
        self.texts: list[str] = []

    async def search(self, track):
        return []

    async def search_text(self, text):
        self.texts.append(text)
        return [h for h in self.hits if h.title.casefold() in text.casefold()]


CRASH_MV = video("CRASH - ALLDAY PROJECT | SBS 261004 방송", "SBSKPOP X INKIGAYO", 170.0)


@pytest.mark.parametrize("typed", [CRASH_MV.title, "ALLDAY PROJECT CRASH", "CRASH ALLDAY PROJECT"])
async def test_manual_search_without_an_artist_finds_the_song_in_either_order(typed):
    # SPEC v1.77: song before singer, as in this stage video, or after: no part is taken for the singer.
    song = hit("CRASH", "ALLDAY PROJECT", 150.0, cid="song")
    same_name = hit("Crash", "Usher", 150.0, cid="other")
    hits, errors = await LyricResolver([AnyText([same_name, song])]).search_manual(CRASH_MV, typed, "")
    assert errors == []
    assert [sc.candidate.source_id for _, sc in hits] == ["song", "other"]
    # Named, so scored as that song; the stage is 20 s longer, which the list still says.
    assert hits[0][1].title_sim == 1.0 and hits[0][1].rejected == "duration"


async def test_manual_search_with_an_artist_does_not_search_free_text():
    source = AnyText([hit("CRASH", "ALLDAY PROJECT", 150.0)])
    await LyricResolver([source]).search_manual(CRASH_MV, "CRASH", "ALLDAY PROJECT")
    assert source.texts == []


async def test_manual_free_text_failures_are_reported_once():
    hits, errors = await LyricResolver([TextSearchable({}, fail=True)]).search_manual(CRASH_MV, CRASH_MV.title, "")
    assert hits == [] and errors == ["down"]


def test_a_labels_upload_is_asked_with_the_artist_before_the_dash():
    # SPEC v1.66: BROYA ICEBOX is a label; the artist is written before the dash.
    track = video("FRαNKIE阿法 - 沒有妳在 Not Around ft.王ADEN (Dir.by @VincentWan9)", "BROYA ICEBOX", 210.9)
    searched = LyricResolver.searchable(track)
    assert searched.title == "沒有妳在 Not Around ft. 王ADEN (Dir.by @VincentWan9)"
    assert searched.artist == "FRαNKIE阿法, BROYA ICEBOX"
    # Other players keep the title whole: a music app's dash is part of the song's name.
    app = LyricResolver.searchable(TrackInfo("Paper Lanterns - Part Two", "The Example Band", "", 200.0, "Spotify.exe"))
    assert (app.title, app.artist) == ("Paper Lanterns - Part Two", "The Example Band")


async def test_a_labels_upload_finds_the_song_by_the_artist_before_the_dash():
    found = hit("沒有妳在 (feat. 王ADEN)", "FRαNKIE阿法", 209.0, cid="ok")

    class ByArtist(LyricSource):
        name = "lrclib"

        async def search(self, track):
            return [found] if track.artist.startswith("FRαNKIE阿法") else []

    result = await LyricResolver([ByArtist()]).resolve(
        video("FRαNKIE阿法 - 沒有妳在 ft.王ADEN (Dir.by @VincentWan9)", "BROYA ICEBOX", 210.9)
    )
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"



async def test_a_traditional_query_goes_in_simplified_too():
    # SPEC v1.56: LRCLIB lists the song as ICE杨长青; the video wrote 楊長青.
    src = TextSearchable({"Want U Back - ICE杨长青": [hit("Want U Back", "ICE杨长青", 152.0, cid="ok")]})
    pipeline = OpenCCPipeline(converter=CC)
    result = await LyricResolver([src], pipeline=pipeline).resolve(video("Want U Back - ICE楊長青【動態歌詞】", "華語音樂台", 152.0))
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"


@pytest.mark.parametrize(
    "title, ok",
    [
        ("趙翊帆 - 妳 (Lyrics)", True),
        ("妳 - 趙翊帆「我們都在等一場雨停」【動態歌詞】", True),
        ("趙翊帆《妳》Official Music Video", True),
        ("趙翊帆 - 妳是我的唯一 (Lyrics)", False),  # 妳 inside another title
        ("趙翊帆「我們都在等妳」", False),  # inside a lyric line
    ],
)
def test_a_one_character_title_counts_as_a_part_of_its_own(title, ok):
    # SPEC v1.57: one character turns up anywhere; on its own between dashes or brackets it is the title.
    assert confirms(video(title, "7clouds", 220.0), hit("妳", "趙翊帆", 220.0)) is ok


class Listing(LyricSource):
    """Answers the ordinary search with fixed hits."""

    name = "lrclib"

    def __init__(self, found):
        self.found = list(found)

    async def search(self, track):
        return list(self.found)


async def test_a_browser_video_needs_the_artist_of_a_hit():
    # SPEC v1.58: on a lyrics channel the title and length alone took a cover by others.
    cover = hit("Love Me Like This", "CONE翻唱团 / 狐椒", 189.0, cid="cover")
    original = hit("Love Me Like This", "NMIXX", 189.0, cid="nmixx")
    track = video("Love Me Like This [NMIXX]", "Lyric Vibes", 189.0)
    assert (await LyricResolver([Listing([cover])]).resolve(track)).kind is ResultKind.NOT_FOUND
    result = await LyricResolver([Listing([cover, original])]).resolve(track)
    assert result.candidate.source_id == "nmixx"  # NMIXX is named in the title
    # A channel named like the artist is evidence enough, as for other players.
    assert (await LyricResolver([Listing([original])]).resolve(video("Love Me Like This", "NMIXX", 189.0))).kind is ResultKind.SYNCED


async def test_other_players_keep_title_and_length_matches():
    cover = hit("Love Me Like This", "CONE翻唱团", 189.0, cid="cover")
    track = TrackInfo("Love Me Like This", "Lyric Vibes", "", 189.0, "Spotify.exe")
    assert (await LyricResolver([Listing([cover])]).resolve(track)).kind is ResultKind.SYNCED


@pytest.mark.parametrize(
    "query, part",
    [
        ("戀曲2020 LOVE SONG 2020", "戀曲2020"),
        ("愛我的時候 When You Loved Me", "愛我的時候"),
        ("追追追追 Chasing on You", "追追追追"),
        ("Bu$Y & Ye!!ow - Chapter 1", None),  # no Chinese
        ("愛我的時候", None),  # nothing but Chinese
        ("ICE 杨长青 - Want U Back", None),  # Chinese is not where it starts
    ],
)
def test_the_chinese_part_of_a_bilingual_query(query, part):
    assert chinese_part(query) == part


async def test_a_bilingual_query_is_asked_with_its_chinese_title_too():
    # SPEC v1.69: NetEase lists 影子計劃's 恋曲2020 for 戀曲2020, not for the bilingual title.
    src = TextSearchable({"戀曲2020": [hit("恋曲2020", "影子计划 Shadow Project / Tc", 299.0, cid="ok")]})
    track = video("Bu$Y & Ye!!ow , Paper Jim - 【戀曲2020 LOVE SONG 2020】Chapter 1 (Official Music Video)",
                  "影子計劃Shadow Project", 306.5)
    result = await LyricResolver([src], pipeline=OpenCCPipeline(converter=CC)).resolve(track)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"


async def test_another_language_version_is_not_the_song():
    # SPEC v1.70: HARU HARU's M/V took NetEase's Japanese live version as a version of another length.
    japanese = hit("HaruHaru (一天一天) (Japanese Version_BIGBANG WORLD TOUR 2015~2016 [MADE] IN JAPAN)", "BIGBANG", 309.0, cid="ja")
    korean = hit("HARU HARU", "Bigbang", 324.0, cid="ko")
    track = video("BIGBANG - HARU HARU(하루하루) M/V", "BIGBANG", 324.0)
    assert not confirms(track, japanese)
    assert (await LyricResolver([Listing([japanese])]).resolve(track)).kind is ResultKind.NOT_FOUND
    assert (await LyricResolver([Listing([japanese, korean])]).resolve(track)).candidate.source_id == "ko"
    # A player playing the Japanese version gets it.
    spotify = TrackInfo("HaruHaru (Japanese Version)", "BIGBANG", "MADE", 309.0, "Spotify.exe")
    assert (await LyricResolver([Listing([japanese])]).resolve(spotify)).candidate.source_id == "ja"


def test_an_all_caps_name_may_be_spelled_out_in_the_video():
    # SPEC v1.83: LRCLIB credits FTP; the video names Fast-Track Pathway.
    track = video('Fast-Track Pathway - "BOOM一下你就BAP" (Lyric Video)', "PG One", 216.6)
    assert confirms(track, hit("Boom一下你就Bap", "FTP", 217.0))
    assert confirms(track, hit("Boom一下你就Bap", "FTP / 杀手耗 / Dracooo / OB03", 216.7, source="netease"))
    # Two letters are too few: any two words have initials.
    assert not confirms(video("Every Time - 我的歌 (Lyrics)", "7clouds", 200.0), hit("我的歌", "ET", 200.0))
    # The words must stand together.
    assert not confirms(video("Fast love - 我的歌 | Track Pathway", "x", 200.0), hit("我的歌", "FTP", 200.0))


async def test_a_spelled_out_name_is_artist_enough_for_a_videos_ordinary_search():
    found = hit("Boom一下你就Bap", "FTP", 217.0, cid="ok")
    src = Listing([found])
    track = video('Fast-Track Pathway - "BOOM一下你就BAP" (Lyric Video)', "PG One", 216.6)
    result = await LyricResolver([src]).resolve(track)
    assert result.kind is ResultKind.SYNCED and result.candidate.source_id == "ok"
