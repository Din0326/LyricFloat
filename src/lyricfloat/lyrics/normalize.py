"""Title and artist normalization for candidate matching (SPEC v1.4, matching rules)."""

from __future__ import annotations

import re
import unicodedata

# NFKC already folds full-width () [] {} into ASCII; these two pairs survive it.
_BRACKET_PATTERNS = (
    re.compile(r"\([^()]*\)"),
    re.compile(r"\[[^\[\]]*\]"),
    re.compile(r"\{[^{}]*\}"),
    re.compile(r"【[^【】]*】"),
    re.compile(r"〔[^〔〕]*〕"),
)

# " - ", " – ", " — " only; a bare hyphen (Anti-Hero) is part of the title.
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")

_FEAT = re.compile(r"\s+(?:feat\.?|ft\.|featuring)\s+.*$")

_VERSION_SUBSTRINGS = ("remaster", "version", "edit", "mix", "版", "現場")
_VERSION_WORDS = re.compile(r"\b(?:live|mono|stereo|acoustic|demo|instrumental|unplugged)\b")
# YouTube titles: "AIYOU - Eve Music Video", "Song - Official MV" (SPEC v1.30).
# "Music Video" and "Lyric Video" go whole, so neither word is left over as if it were a title (SPEC v1.37).
_VIDEO_WORDS = re.compile(r"\b(?:(?:music|lyrics?)\s*video|video|mv|m/v|audio|visualizer)\b")

_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|/|;|×|、|\bfeat\b\.?|\bft\.|\bfeaturing\b)\s*")

_WHITESPACE = re.compile(r"\s+")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", text).strip()


def _strip_brackets(text: str) -> str:
    previous = None
    while previous != text:  # repeat to peel nested brackets from the inside out
        previous = text
        for pattern in _BRACKET_PATTERNS:
            text = pattern.sub(" ", text)
    return text


# Language versions carry other lyrics (SPEC v1.70): BIGBANG's "HaruHaru (Japanese Version_...)",
# WayV's "Love Talk (English Version)", "IF YOU -JP Ver.-", 國語版.
_LANGUAGES = (
    ("ja", r"japanese|jpn?|日文|日語|日语"),
    ("en", r"english|eng|英文|英語|英语"),
    ("zh", r"chinese|mandarin|chn|cn|中文|國語|国语|華語|华语"),
    ("yue", r"cantonese|粵語|粤语"),
    ("ko", r"korean|kor|kr|韓文|韩文|韓語|韩语"),
    ("th", r"thai|泰文|泰語|泰语"),
)
_LANGUAGE_VERSION = [
    (code, re.compile(rf"(?<![a-z])(?:{names})[\s._-]*(?:ver(?:sion)?(?![a-z])|版)", re.IGNORECASE))
    for code, names in _LANGUAGES
]


def language_version(title: str) -> str | None:
    """The language a title names as its version ("ja" for "Japanese Version", "zh" for
    國語版), or None."""
    text = unicodedata.normalize("NFKC", title)
    return next((code for code, pattern in _LANGUAGE_VERSION if pattern.search(text)), None)


def other_language(player_title: str, cand_title: str) -> bool:
    """Whether the hit is a language version the player's title does not name (SPEC v1.70)."""
    lang = language_version(cand_title)
    return lang is not None and lang != language_version(player_title)


# Live performances keep the lyrics but not the timing of the record an MV plays (SPEC v1.75):
# Din's "PPlin - 最後一班車(Official Music Video)" took NetEase's 最后一班车 (Live), its length.
_LIVE_WORDS = re.compile(r"(?<![a-z])live(?![a-z])|現場|现场|純享|纯享|演唱會|演唱会|(?<![a-z])concert(?![a-z])", re.IGNORECASE)
_VERSION_PARTS = re.compile(r"[(\[【〔]([^)\]】〕]*)[)\]】〕]|\s[-–—]\s+(.*)$")
_LIVE_TAG = re.compile(r"live版|現場版|现场版", re.IGNORECASE)


def live_version(title: str) -> bool:
    """Whether a version's title says it is a live performance, in its version notes (brackets,
    a dash part, 現場版): "最后一班车 (Live)" is, "Live Forever" is not."""
    text = unicodedata.normalize("NFKC", title)
    parts = (m.group(1) or m.group(2) or "" for m in _VERSION_PARTS.finditer(text))
    return any(_LIVE_WORDS.search(part) for part in parts) or bool(_LIVE_TAG.search(text))


def names_live(title: str) -> bool:
    """Whether a player's or video's title says the performance is live, anywhere in it:
    "Quanzo - 心疾如焚｜純享版｜EP7", "Boss Life (狂放人生) - Live"."""
    return bool(_LIVE_WORDS.search(unicodedata.normalize("NFKC", title)))


def other_performance(player_title: str, cand_title: str) -> bool:
    """Whether the hit is a live performance the player's title does not name (SPEC v1.75)."""
    return live_version(cand_title) and not names_live(player_title)


def _is_version_note(segment: str) -> bool:
    return (
        any(s in segment for s in _VERSION_SUBSTRINGS)
        or bool(_VERSION_WORDS.search(segment))
        or bool(_VIDEO_WORDS.search(segment))
    )


def _strip_dash_versions(text: str) -> str:
    parts = _DASH_SPLIT.split(text)
    if len(parts) == 1:
        return text
    kept = [parts[0]] + [p for p in parts[1:] if not _is_version_note(p)]
    return " - ".join(kept)


def normalize_title(title: str) -> str:
    folded = _fold(title)
    text = _strip_brackets(folded)
    text = _FEAT.sub("", text)
    text = _strip_dash_versions(text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text or folded


_HAN_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_LATIN_RUN = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)*")


def split_bilingual(title: str) -> tuple[str, str] | None:
    """(Chinese part, English part) of a title carrying both, e.g. "想講你知 Gotta Say It".

    Works on the normalized title, so names already reduced by the bracket
    rule are left alone. None unless both parts are present and the Latin
    part has letters (a trailing number is not an English title).
    """
    normalized = normalize_title(title)
    chinese = " ".join(_HAN_RUN.findall(normalized))
    latin = " ".join(_LATIN_RUN.findall(normalized))
    if not chinese or not latin or not any(ch.isalpha() for ch in latin):
        return None
    return chinese, latin


def title_variants(title: str) -> list[str]:
    """Normalized title, plus its Chinese and English parts for bilingual titles."""
    variants = [normalize_title(title)]
    parts = split_bilingual(title)
    if parts:
        variants.extend(parts)
    return variants


def split_artists(artist: str) -> list[str]:
    """Normalize an artist field and split collaborations into individual names."""
    folded = _fold(artist)
    stripped = _WHITESPACE.sub(" ", _strip_brackets(folded)).strip() or folded
    names: list[str] = []
    for name in _ARTIST_SPLIT.split(stripped):
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def normalize_artist(artist: str) -> str:
    return " / ".join(split_artists(artist))


_TITLE_BRACKET = re.compile(r"【([^【】]+)】|\[([^\[\]]+)\]")
_VIDEO_FILLER = re.compile(r"\bofficial\b")  # "Official MV": says nothing about the song


def _has_content(text: str, names: list[str]) -> bool:
    """Whether a title says more than the artist's name and video words."""
    rest = normalize_title(text)
    for name in names:
        rest = rest.replace(name, " ")
    return any(ch.isalnum() for ch in _VIDEO_FILLER.sub(" ", _VIDEO_WORDS.sub(" ", rest)))


# Video titles join artists with a letter x: "張瑚HU X 趙翊帆YI94 - 妳 Rosie". Only used to spot
# the artist's segment; split_artists keeps "多多 x 以捷" whole, since cache keys are built from it.
_X_SPLIT = re.compile(r"\s+x\s+")


# Video titles write the dash loosely: "王水源 Lifloo -【保持呼吸】". Space on one side is enough
# here (SPEC v1.37); "Anti-Hero", with none, stays whole.
_VIDEO_DASH = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")


def _credited(segment: str) -> list[str]:
    return [n.strip() for part in split_artists(segment) for n in _X_SPLIT.split(part) if n.strip()]


_LATIN_WORD = re.compile(r"[a-z0-9]+(?:['’.][a-z0-9]+)*")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _alias_of(text: str, name: str) -> bool:
    """`text` is `name` plus a Latin alias of at most two words: "王水源 lifloo" for 王水源 (SPEC v1.37)."""
    if name not in text:
        return False
    rest = text.replace(name, " ", 1)
    return not _HAN.search(rest) and len(_LATIN_WORD.findall(rest)) <= 2


def _credits_artist(segment: str, names: list[str]) -> bool:
    """Whether a dash segment names the artist, alone or among others, or with a Latin alias."""
    folded = normalize_title(segment)
    if folded in names or set(_credited(segment)) & set(names):
        return True
    return any(len(name) >= 2 and _alias_of(folded, name) for name in names)


def credited_artists(title: str, artist: str) -> list[str]:
    """Other artists a YouTube video title names beside the player's one (SPEC v1.33):
    "張瑚HU X 趙翊帆YI94 - 妳 Rosie" played as 張瑚HU gives ["趙翊帆yi94"]."""
    names = split_artists(artist)
    parts = _VIDEO_DASH.split(title.strip())
    if len(parts) < 2:
        return []
    others: list[str] = []
    for part in parts:
        credited = _credited(part)
        if normalize_title(part) in names or set(credited) & set(names):  # not "王水源 Lifloo": an alias, no one else
            others.extend(n for n in credited if n not in names and n not in others)
    return others


# YouTube channels named after the artist (SPEC v1.54): "ICE杨长青 - Topic" (made by YouTube for
# every artist), "TaylorSwiftVEVO", "ICE杨长青 Official", "告五人 官方頻道".
_CHANNEL_SUFFIX = re.compile(
    r"\s+-\s+topic$|(?<=\S)vevo$|\s+official(?:\s+(?:channel|youtube(?:\s+channel)?))?$|\s*官方(?:頻道|频道)$",
    re.IGNORECASE,
)


def channel_artist(channel: str) -> str:
    """The artist a YouTube channel is named after, or the channel as it is."""
    return _CHANNEL_SUFFIX.sub("", channel.strip()).strip() or channel


# "沒有妳在 Not Around ft.王ADEN": with no space after it the credit is not cut (_FEAT).
_FEAT_GLUED = re.compile(r"(?<!\S)(feat\.|ft\.)(?=\S)", re.IGNORECASE)
_LATIN_NAME = re.compile(r"[a-z0-9]+(?:[ .'’][a-z0-9]+)*")


def _names_channel(segment: str, names: list[str]) -> bool:
    """Whether a dash segment writes the channel's name, or its Chinese or Latin half:
    "莫宰羊Goater,派偉俊Patrick Brasca" from 莫宰羊 (the "Song - Artists" order)."""
    folded = _fold(segment)
    for name in names:
        if any(len(run) >= 2 and run in folded for run in _HAN_RUN.findall(name)):
            return True
        for run in _LATIN_NAME.findall(name):
            if len(run) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(run)}(?![a-z0-9])", folded):
                return True
    return False


def dash_credit(title: str, channel: str) -> tuple[str, str] | None:
    """(artist, song) of a video title "Artist - Song" uploaded by someone other than the
    artist, a label or a lyrics channel (SPEC v1.66): "FRαNKIE阿法 - 沒有妳在 Not Around
    ft.王ADEN (Dir.by @VincentWan9)" from BROYA ICEBOX. YouTube titles put the artist first;
    the other order ("CRASH - ALLDAY PROJECT") finds nothing here and is left to the video
    identification. None unless the title has exactly two dash segments, neither naming the
    channel's artist (searchable_title cuts those), with a name before the dash; or when the
    channel's name turns up after the dash, the artists' side of "瑪格莉塔 Margarita -
    莫宰羊Goater,派偉俊Patrick Brasca…" from 莫宰羊."""
    names = split_artists(channel)
    parts = [p.strip() for p in _VIDEO_DASH.split(title.strip())]
    if len(parts) != 2 or any(_credits_artist(p, names) for p in parts):
        return None
    singer, song = parts
    if _names_channel(song, names):
        return None
    if not _has_content(singer, []):  # "【MV】 - Song": nobody before the dash
        return None
    song = searchable_title(_FEAT_GLUED.sub(r"\1 ", song), singer)  # "- 【Song】Official MV": its 【】 part
    if not _has_content(song, []):
        return None
    return singer, song


# "BIGBANG - HARU HARU(하루하루) M/V": video words closing the title, outside brackets. LRCLIB
# finds nothing for "haru haru m/v", and a Japanese version of another length took its place (SPEC v1.66).
_TRAILING_VIDEO_WORDS = re.compile(
    r"(?:\s+(?:official|(?:music|lyrics?)\s*video|video|mv|m/v|audio|visualizer))+\s*$", re.IGNORECASE
)


def searchable_title(title: str, artist: str) -> str:
    """The song's title inside a YouTube video title (SPEC v1.31). A dash segment that is
    the artist goes ("TYSON YOSHI - 你不會一輩子的愛上我 Glad That We Met (Official Music
    Video)"); a title left with only the artist and video words gives way to its 【】 part
    ("BK-【不管過了多久都會等妳】audio"); video words closing it go (SPEC v1.66). Other titles
    come back unchanged."""
    names = split_artists(artist)
    parts = _VIDEO_DASH.split(title.strip())
    kept = [p for p in parts if not _credits_artist(p, names)] if len(parts) > 1 else parts
    text = " - ".join(kept) if kept else title
    if _has_content(text, names):
        if len(kept) != 1:
            # "瑪格莉塔 Margarita - 莫宰羊Goater,… M/V": the video words make the segment after the
            # dash a note that normalize_title drops (SPEC v1.30), leaving the song.
            return text
        trimmed = _TRAILING_VIDEO_WORDS.sub("", text)
        return trimmed if _has_content(trimmed, names) else text
    for match in _TITLE_BRACKET.finditer(title):
        inner = (match.group(1) or match.group(2)).strip()
        if _has_content(inner, names):
            return inner
    return title
