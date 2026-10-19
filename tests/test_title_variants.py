"""SPEC v1.50: the browser title self-test. Songs are public ones; no lyrics."""

from lyricfloat.diagnostics.title_variants import (
    NOT_FOUND,
    OK,
    OK_PLAIN,
    WRONG,
    Song,
    group_of,
    judge,
    parts_of,
    pick_songs,
    variants_of,
)
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo


def same(_config, text):
    return text


def result(title, artist, lines, kind=ResultKind.SYNCED, cid="1"):
    lyrics = Lyrics(tuple(LyricLine(float(i), text) for i, text in enumerate(lines)), synced=kind is ResultKind.SYNCED)
    return LyricResult(TrackInfo(title, artist), kind, lyrics, Candidate("lrclib", cid, title, artist, "", 200.0))


LINES = ["first line of the song", "second line here", "and a third one", "the chorus goes like this"]


def test_parts_drop_spotify_suffixes_and_keep_the_featured_artist():
    p = parts_of(Song("Boss Life (狂放人生) - Live", "彈殼Danko", 240.0, "zh"))
    assert (p.title, p.artist, p.feat, p.group) == ("Boss Life (狂放人生)", "彈殼Danko", "", "zh")
    p = parts_of(Song("EPIKISM (feat. Crush)", "Epik High", 200.0, "other"))
    assert (p.title, p.feat, p.group) == ("EPIKISM", "Crush", "kr")


def test_groups():
    assert group_of(Song("夜に駆ける", "YOASOBI", 261.0, "other")) == "jp"
    assert group_of(Song("Ditto", "NewJeans", 185.0, "other")) == "kr"
    assert group_of(Song("Style", "Taylor Swift", 231.0, "en")) == "en"


def test_variants_follow_the_song_and_its_group():
    variants = variants_of(Song("Ditto", "NewJeans", 185.0, "other"), same)
    names = {v.template for v in variants}
    assert {"mv", "topic", "kr_inkigayo"} <= names and not names & {"zh_book", "jp_mv", "feat"}
    topic = next(v for v in variants if v.template == "topic")
    assert (topic.title, topic.channel, topic.duration) == ("Ditto", "NewJeans - Topic", 185.0)
    assert all(v.track().source_app == "Chrome" for v in variants)


def test_simplified_upload_is_converted():
    convert = lambda config, text: text.replace("壞", "坏") if config == "t2s" else text  # noqa: E731
    v = next(v for v in variants_of(Song("愛情的大壞蛋", "美秀集團", 200.0, "zh"), convert) if v.template == "zh_simplified")
    assert "大坏蛋" in v.title


def test_judge():
    answer = result("Style", "Taylor Swift", LINES)
    assert judge(answer, result("Style", "Taylor Swift", LINES, cid="2")) == (OK, "歌名歌手相同")
    assert judge(answer, result("Style", "TaylorSwift", ["other words entirely"], cid="6")) == (OK, "歌名歌手相同")
    nick = result("ALL I WANT IS YOU", "NICKTHEREAL 周湯豪", LINES)
    assert judge(nick, result("ALL I WANT IS YOU", "周湯豪", ["other words entirely"], cid="7"))[0] == OK
    talk = result("Love Talk", "WayV", LINES)
    assert judge(talk, result("Love Talk (English Version)", "WayV", ["sung in english instead", "all of it"], cid="11"))[0] == WRONG  # v1.70
    cry = result("CRY FOR ME", "TWICE", LINES)
    assert judge(cry, result("CRY FOR ME (live) FULL Ver.", "TWICE", ["other words entirely"], cid="9"))[0] == OK
    assert judge(cry, result("CRY", "TWICE", ["other words entirely"], cid="10"))[0] == WRONG
    nine = result("BUT U", "NINEONE赵馨玥", LINES)
    assert judge(nine, result("But U (Live)", "NINEONE#乃万", ["other words entirely"], cid="8"))[0] == OK
    # LRCLIB entries filed under a video's title or with title and artist swapped: the lines decide.
    assert judge(answer, result("Taylor Swift - Style (Lyric Video)", "Taylor Swift", LINES, cid="3"))[0] == OK
    assert judge(answer, result("Other Song", "Someone", ["nothing alike at all", "different words"], cid="4"))[0] == WRONG
    assert judge(answer, result("Style", "Taylor Swift", LINES, kind=ResultKind.PLAIN_ONLY, cid="5"))[0] == OK_PLAIN
    assert judge(answer, LyricResult(TrackInfo("x", "y"), ResultKind.NOT_FOUND))[0] == NOT_FOUND


def test_pick_songs_takes_music_app_songs_with_lyrics_once():
    history = [
        {"title": "Ditto", "artist": "NewJeans", "duration": 185, "source_app": "Spotify.exe", "language": "other", "kind": "synced"},
        {"title": "Ditto", "artist": "NewJeans", "duration": 185, "source_app": "Spotify.exe", "language": "other", "kind": "synced"},
        {"title": "OMG", "artist": "NewJeans", "duration": 212, "source_app": "Spotify.exe", "language": "other", "kind": "synced"},
        {"title": "video", "artist": "channel", "duration": 200, "source_app": "Chrome", "language": "en", "kind": "synced"},
        {"title": "Lost", "artist": "Nobody", "duration": 200, "source_app": "Spotify.exe", "language": "en", "kind": "not_found"},
    ]
    assert [s.title for s in pick_songs(history)] == ["OMG", "Ditto"]
    assert [s.title for s in pick_songs(history, per_group=1)] == ["OMG"]
