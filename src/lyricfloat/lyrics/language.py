"""Song language detection (SPEC v1.7, matching rules).

Two passes: from title/artist before querying, and from the lyric text
once fetched. Kana/hangul are checked first because Japanese text also
contains Han characters.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002ffff]")
_KANA_HANGUL = re.compile(r"[぀-ヿㇰ-ㇿｦ-ﾟ가-힯ᄀ-ᇿ㄰-㆏]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

OTHER_RATIO = 0.10  # kana + hangul share that makes lyrics "other"


def has_kana_or_hangul(text: str) -> bool:
    """Japanese or Korean text: never converted between Chinese scripts (spec, text processing)."""
    return bool(_KANA_HANGUL.search(text))


class Language(Enum):
    ZH = "zh"
    EN = "en"
    OTHER = "other"


def detect_from_metadata(title: str, artist: str) -> Language:
    text = f"{title} {artist}"
    if _KANA_HANGUL.search(text):
        return Language.OTHER
    if _HAN.search(text):
        return Language.ZH
    return Language.EN


def detect_from_lyrics(lines: Iterable[str]) -> Language | None:
    """Han characters vs English words; None when there is nothing to judge."""
    text = "\n".join(lines)
    han = len(_HAN.findall(text))
    kana_hangul = len(_KANA_HANGUL.findall(text))
    words = len(_LATIN_WORD.findall(text))
    total = han + kana_hangul + words
    if total == 0:
        return None
    if kana_hangul / total >= OTHER_RATIO:
        return Language.OTHER
    return Language.ZH if han > words else Language.EN
