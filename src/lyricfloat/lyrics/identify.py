"""Identify the song behind a browser video's title (SPEC v1.34). Pure logic.

YouTube in a browser reports the video's own title and, as the artist, often the
uploading channel: "两难-加木『…lyric excerpt…』【动态歌词】#流行歌曲" from 流行音乐.
When matching by title and artist finds nothing, the title is cleaned into a few
free-text queries for the sources' own search, and a hit only counts when its
title and one of its artists both appear in the video's title (or artist field),
as two separate pieces, with a believable length.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from lyricfloat.lyrics.normalize import normalize_title, other_language, split_artists, title_variants
from lyricfloat.models import Candidate, TrackInfo

BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave")
NOT_A_SONG_LENGTH = 15 * 60.0  # seconds: a browser video this long is not a song
MAX_QUERIES = 4
MAX_QUERY_LENGTH = 60
# Seconds the video may be shorter than the song: a label's MV is often an edit (林宥嘉's 自然醒
# 4 s, TANK's 給我你的愛 10 s shorter); a teaser or a clip is far shorter (SPEC v1.73; was 3).
SHORTER_LIMIT = 15.0
LONGER_LIMIT = 180.0  # seconds it may be longer: an MV with scenes before and after
MIN_PIECE = 2  # characters: a one-character title or artist turns up anywhere

_NOISE = re.compile(
    r"official|music\s*video|lyrics?\s*video|lyrics?|\baudio\b|\bmv\b|m/v|visualizer|"
    r"\bprod(?:uced)?\.?\s+(?:by\s+)?[^\s()\[\]【】]+|動態歌詞|动态歌词|中文歌詞|中文歌词|高音質|高音质|完整版|#[^\s《》【】『』「」\[\]()]+|"
    # Picture quality (SPEC v1.40): 【1080P 60帧】, (4K)
    r"\b\d{3,4}p\b|\b\d+\s*fps\b|\d+\s*[帧幀]|\b[48]k\b|高清|超清|[藍蓝]光|"
    # Japanese "with lyrics"; TV stages (SPEC v1.52): "@인기가요 inkigayo 20241006", "EP.880 | Mnet 241010 방송";
    # THE FIRST TAKE's own name (SPEC v1.60); a show's 【纯享】 tag (SPEC v1.67)
    r"歌詞付き?|(?<!\w)@\S+|\b(?:19|20)?\d{6}\b|\bep\.?\s*\d+\b|\b(?:sbs|kbs2?|mbc|mnet|jtbc)\b|"
    r"inkigayo|인기가요|music\s*bank|뮤직뱅크|show!?\s*music\s*core|음악중심|m\s*countdown|엠카운트다운|show\s*champion|쇼챔피언|방송|the\s+first\s+take|[纯純]享版?",
    re.IGNORECASE,
)
# NFKC has already turned ［］（）｜ into [] () | by the time these run; │ (box drawing) stays.
# v1.60: " / " too, spaced ("星野源 - Comedy / THE FIRST TAKE"); AC/DC stays whole.
_BARS = re.compile(r"[|│]|\s+/\s+")
# K-pop titles: MARK 마크 'Flight to NYC'. Quotes must stand apart, unlike Don't.
_SINGLE_QUOTED = r"(?:(?<=\s)|^)'([^']+)'(?=\s|$|[|│:,.])"
_BRACKETED = re.compile(
    r"【([^【】]*)】|『([^『』]*)』|「([^「」]*)」|\[([^\[\]]*)\]|\(([^()]*)\)|〔([^〔〕]*)〕|\"([^\"]*)\"|“([^“”]*)”|‘([^‘’]*)’|"
    + _SINGLE_QUOTED
)
_QUOTED_SONG = re.compile(_SINGLE_QUOTED)
_PIECE_END = re.compile(r"[|│\]】)」』〕\"”’]")  # where the text before a quoted song begins
# Book title marks hold the song in Chinese video titles: TANK《如果我变成回忆》 (SPEC v1.40).
_BOOK = re.compile(r"《([^《》]+)》")
_BRACKET_CHARS = re.compile(r"[【】『』「」\[\]()〔〕\"“”‘’]|(?:(?<=\s)|^)'|'(?=\s|$|[|│:,.])")
_SPACES = re.compile(r"\s+")


def is_browser(app: str) -> bool:
    folded = app.casefold()
    return any(name in folded for name in BROWSERS)


def not_a_song(track: TrackInfo) -> bool:
    """A browser video not worth asking the lyric sources: a long one (a stream, a vlog), or
    one with no artist. YouTube and YouTube Music always name one (the channel); Douyin,
    Instagram and other sites' clips leave it empty (SPEC v1.43)."""
    return is_browser(track.source_app) and (track.duration > NOT_A_SONG_LENGTH or not track.artist)


# "追追追追Chasing on You" finds nothing on NetEase; "追追追追 Chasing on You" finds the song (SPEC v1.38).
_HAN_LATIN = re.compile(r"(?<=[㐀-䶿一-鿿豈-﫿])(?=[A-Za-z])|(?<=[A-Za-z])(?=[㐀-䶿一-鿿豈-﫿])")


def _tidy(text: str) -> str:
    return _SPACES.sub(" ", _HAN_LATIN.sub(" ", text)).strip(" -–—|│/:：")


_HAN_LATIN_SPACE = re.compile(r"(?<=[㐀-䶿一-鿿豈-﫿]) (?=[A-Za-z])|(?<=[A-Za-z]) (?=[㐀-䶿一-鿿豈-﫿])")


def joined(query: str) -> str:
    """The query with Chinese and Latin run together again: names are written that way
    ("ICE杨长青" finds the singer; "ICE 杨长青" finds nobody on LRCLIB and a singer called Ice
    on NetEase), while titles want the space (v1.38). Sources are asked both ways (SPEC v1.55)."""
    return _HAN_LATIN_SPACE.sub("", query)


# The Chinese title before an English one, digits kept: "戀曲2020 LOVE SONG 2020" -> 戀曲2020.
_LEADING_CHINESE = re.compile(r"^(.*?[㐀-䶿一-鿿豈-﫿][^\sA-Za-z]*)\s+[A-Za-z]")


def chinese_part(query: str) -> str | None:
    """The Chinese title a bilingual query starts with: NetEase finds nothing it wants for
    "戀曲2020 LOVE SONG 2020", and 影子計劃's 〈恋曲2020〉 for 戀曲2020 (SPEC v1.69), as the
    ordinary search asks again with the Chinese part (v1.32)."""
    m = _LEADING_CHINESE.match(query)
    part = m.group(1).strip(" -–—|│/:：") if m else ""
    return part if len(part) >= MIN_PIECE and part != query else None


def video_queries(title: str) -> list[str]:
    """Free-text queries for a video title: what 《》 holds, a quoted song with the singer
    written before it, what 【】 holds, the text outside brackets (each part between "|", "│"
    or " / " on its own), then what the other brackets hold, then everything; noise words
    dropped, at most MAX_QUERIES."""
    text = unicodedata.normalize("NFKC", title)
    # Taken before noise goes: 《#专属天使》 is a hashtag and the song (SPEC v1.41).
    books = [_tidy(_NOISE.sub(" ", m.group(1).replace("#", " "))) for m in _BOOK.finditer(text)]
    base = _NOISE.sub(" ", text)
    outside = _tidy(_BRACKET_CHARS.sub(" ", _BRACKETED.sub(" ", base)))  # and any bracket left unpaired
    # "CRASH - ALLDAY PROJECT | SBS 261004 방송": the show's part sinks the search (SPEC v1.42).
    sections = [_tidy(part) for part in _BARS.split(outside)]
    # 올데이 프로젝트 'CRASH': "CRASH" alone finds 20 other songs; with the singer it is found (SPEC v1.46).
    credited = []
    for m in _QUOTED_SONG.finditer(base):
        singer = _BRACKET_CHARS.sub(" ", _PIECE_END.split(base[: m.start()])[-1])
        if any(ch.isalnum() for ch in singer):
            credited.append(_tidy(f"{singer} {m.group(1)}"))
    inside = [_tidy(next(g for g in m.groups() if g is not None)) for m in _BRACKETED.finditer(base)]
    # 【】 holds the song in label MVs, "G.E.M.【再見 GOODBYE】Official MV [HD] 鄧紫棋": ahead of the
    # text outside, where "G.E.M." named her song 〈G.E.M.〉 (SPEC v1.67).
    lenticular = [_tidy(m.group(1)) for m in _BRACKETED.finditer(base) if m.group(1) is not None]
    whole = _tidy(_BRACKET_CHARS.sub(" ", base))
    queries: list[str] = []
    for q in (*books, *credited, *lenticular, *sections, *inside, whole):
        # A part that is only 《title》 was asked already, as what 《》 holds.
        if q and any(ch.isalnum() for ch in q) and len(q) <= MAX_QUERY_LENGTH and not {q, q.strip("《》")} & set(queries):
            queries.append(q)
    return queries[:MAX_QUERIES]


def _spans(needle: str, hay: str) -> list[tuple[int, int]]:
    out, start = [], hay.find(needle)
    while start >= 0:
        out.append((start, start + len(needle)))
        start = hay.find(needle, start + 1)
    return out


Fold = Callable[[str], str]


_HAN_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def _keyer(fold: Fold) -> Callable[[str], str]:
    def key(text: str) -> str:
        # One Han run at a time: the fold leaves a text with kana or hangul in it alone, and
        # 孫盛希「jagi」歌詞付き would keep 孫盛希 apart from NetEase's 孙盛希 (SPEC v1.51).
        text = _HAN_RUN.sub(lambda m: fold(m.group()), unicodedata.normalize("NFKC", text).casefold())
        return _SPACES.sub("", text).replace("妳", "你")

    return key


_ALIAS = re.compile(r"[(\[]([^()\[\]]+)[)\]]")  # after NFKC: （） are ()
_EDGE_MARKS = re.compile(r"^[\W_]+|[\W_]+$")
# Where Chinese meets Latin in a name, with marks or a space between or nothing: 弹壳Danko,
# NINEONE#乃万 (v1.59), "TANK 呂建忠" (v1.71).
_SCRIPT_CHANGE = re.compile(r"(?<=[㐀-䶿一-鿿豈-﫿])[^\w]*(?=[A-Za-z])|(?<=[A-Za-z])[^\w]*(?=[㐀-䶿一-鿿豈-﫿])")


def _full_names(artist: str) -> list[str]:
    """The whole names a hit is credited to, the one in brackets too: NetEase's 丽兹（LIZ） is 丽兹
    and LIZ (SPEC v1.47). A bracketed number is a catalogue's tag ("Ive (16)"), not a name."""
    names = split_artists(artist)
    for m in _ALIAS.finditer(unicodedata.normalize("NFKC", artist)):
        names += [n for n in split_artists(m.group(1)) if any(ch.isalpha() for ch in n) and n not in names]
    return names


def _artist_names(artist: str) -> list[str]:
    """The names a hit is credited to: its whole names, and their parts: a Chinese name run into
    an English one is both, 弹壳Danko is 弹壳 and Danko (SPEC v1.48); NINEONE# is NINEONE too,
    and NINEONE#乃万 is NINEONE and 乃万 (SPEC v1.59); "TANK 呂建忠" is TANK and 呂建忠 (SPEC v1.71)."""
    names = _full_names(artist)
    for name in list(names):
        names += [p for p in (part.strip() for part in _SCRIPT_CHANGE.split(name)) if p and p not in names]
    # Marks around a name are not part of it: LRCLIB credits "NINEONE#" (SPEC v1.59).
    names += [t for n in list(names) if (t := _EDGE_MARKS.sub("", n)) and t not in names]
    return names


# The parts of a video title between dashes, bars, slashes and brackets.
_SEGMENT_SPLIT = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+|[|│/【】《》「」『』\[\]()〔〕\"“”]")


def _segments(title: str, key: Callable[[str], str]) -> set[str]:
    text = _NOISE.sub(" ", unicodedata.normalize("NFKC", title))
    return {k for part in _SEGMENT_SPLIT.split(text) if (k := key(part))}


# An all-caps name of 3 to 6 letters may stand for words the video spells out (SPEC v1.83):
# LRCLIB credits FTP for "Fast-Track Pathway - "BOOM一下你就BAP" (Lyric Video)". Two letters (ET)
# turn up as the initials of any two words.
_ACRONYM = re.compile(r"(?<![A-Za-z])[A-Z]{3,6}(?![A-Za-z])")
_LATIN_WORD_RUN = re.compile(r"[A-Za-z][A-Za-z']*")
_WORD_GAP = re.compile(r"[\s\-–]*")


def _spelled_out(artist: str, text: str) -> list[str]:
    """The phrases in `text` whose words' initials spell one of the hit's all-caps names."""
    text = unicodedata.normalize("NFKC", text)
    words = list(_LATIN_WORD_RUN.finditer(text))
    out = []
    for acronym in dict.fromkeys(m.group() for m in _ACRONYM.finditer(artist)):
        n = len(acronym)
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            if "".join(w.group()[0] for w in run).upper() != acronym:
                continue
            if all(_WORD_GAP.fullmatch(text[a.end():b.start()]) for a, b in zip(run, run[1:])):
                out.append(text[run[0].start():run[-1].end()])
    return out


def _length_fits(track: TrackInfo, cand: Candidate) -> bool:
    return not (track.duration > 0 and cand.duration > 0) or -SHORTER_LIMIT <= track.duration - cand.duration <= LONGER_LIMIT


def song_key(title: str, fold: Fold = lambda s: s) -> str:
    """One song's title however a source spells it, for telling songs apart."""
    return _keyer(fold)(normalize_title(title))


def confirms(track: TrackInfo, cand: Candidate, fold: Fold = lambda s: s) -> bool:
    """Whether the video names this hit: its title and one of its artists both appear in
    the video's title or artist field, not overlapping, and the lengths fit an MV at most.
    `fold` puts text in one script (t2s), so 不會 matches NetEase's 不会."""
    if not _length_fits(track, cand) or other_language(track.title, cand.title):  # v1.70
        return False
    key = _keyer(fold)
    hay = key(f"{track.title} {track.artist}")
    # The title, or either half of a bilingual one: LRCLIB's "愛我的時候 - When You Loved Me"
    # is in the video as 《愛我的時候 When You Loved Me》 (SPEC v1.68).
    spans: list[tuple[int, int]] = []
    for name in dict.fromkeys(key(v) for v in title_variants(cand.title)):
        # A one-character title turns up anywhere: it counts only as a part of the video title
        # of its own, as in "趙翊帆 - 妳 (Lyrics)" (SPEC v1.57).
        if name and (len(name) >= MIN_PIECE or name in _segments(track.title, key)):
            spans += _spans(name, hay)
    if not spans:
        return False
    # Not the title when it is part of the credited name: "ICE" in ICE杨长青 does not name
    # 〈ICE〉 by ICE杨长青, now that 杨长青 alone counts as the artist (SPEC v1.53); nor the name in
    # brackets: "G.E.M." in a G.E.M.【再見】 video is 鄧紫棋 (G.E.M), not her 〈G.E.M.〉 (SPEC v1.67).
    credited = [f for n in _full_names(cand.artist) if len(key(n)) >= MIN_PIECE for f in _spans(key(n), hay)]
    titles = [t for t in spans if all(t[1] <= f[0] or f[1] <= t[0] for f in credited)]
    for artist in (*_artist_names(cand.artist), *_spelled_out(cand.artist, f"{track.title} {track.artist}")):
        who = key(artist)
        if len(who) < MIN_PIECE:
            continue
        for a in _spans(who, hay):
            if any(a[1] <= t[0] or t[1] <= a[0] for t in titles):
                return True
    return False


def names_artist(track: TrackInfo, cand: Candidate, fold: Fold = lambda s: s) -> bool:
    """Whether one of the hit's artists appears in the video's title or artist field (SPEC v1.58),
    or is spelled out there (FTP: Fast-Track Pathway, SPEC v1.83)."""
    key = _keyer(fold)
    hay = key(f"{track.title} {track.artist}")
    if any(len(who := key(a)) >= MIN_PIECE and who in hay for a in _artist_names(cand.artist)):
        return True
    return bool(_spelled_out(cand.artist, f"{track.title} {track.artist}"))


def credit_query(track: TrackInfo) -> str | None:
    """SPEC v1.35: the artist field plus what the title holds, for a title that may be only
    another artist's name: YouTube reported "OSN)" for "Dizzy Dizzo 蔡詩芸 ft. 高爾宣OSN【Tides】"."""
    queries = video_queries(track.title)
    if not queries or not track.artist.strip():
        return None
    return f"{track.artist.strip()} {queries[0]}"


def credits_both(track: TrackInfo, cand: Candidate, fold: Fold = lambda s: s) -> bool:
    """Whether the hit is credited to the player's artist and to the name the title holds.
    The caller still needs this to single out one song: two artists may share several."""
    queries = video_queries(track.title)
    if not queries or not _length_fits(track, cand):
        return False
    key = _keyer(fold)
    fragment, credit = key(queries[0]), key(cand.artist)
    field = [key(a) for a in split_artists(track.artist)]
    if len(fragment) < MIN_PIECE or any(fragment in f for f in field):
        return False  # the title is the artist's own name: any of their songs would do
    return fragment in credit and any(len(f) >= MIN_PIECE and f in credit for f in field)
