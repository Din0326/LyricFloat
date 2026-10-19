"""OpenCC wrapper: simplified detection and conversion to Taiwan traditional.

Rules (SPEC v1.11):
- a string is simplified when it holds no traditional-only character and at
  least one simplified character; both character sets are tables generated
  from OpenCC data (tools/gen_char_tables.py), so detection loads nothing
- kana/hangul text is never treated as Chinese
- full lyric conversion uses the user's mode (s2twp by default, or s2tw)
- character-only conversion (metadata, word-timed lyrics) uses s2tw
- the custom vocabulary fixes OpenCC mistakes after conversion

Memory: the s2tw/s2twp dictionaries take about 85 MB, so they load only
inside a conversion session and are released when it ends (reloading takes
about 70 ms). t2s, about 4 MB and used for every NetEase query and title
comparison, stays resident.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import opencc

from lyricfloat.lyrics.char_tables import SIMPLIFIED, TRADITIONAL_ONLY
from lyricfloat.lyrics.language import has_kana_or_hangul

log = logging.getLogger(__name__)

CHAR_ONLY = "s2tw"
RESIDENT = frozenset({"t2s"})

VOCABULARY_TEMPLATE = """# LyricFloat 自訂詞彙表
# 每行一筆「轉換後的錯字=正確寫法」，在 OpenCC 轉換之後套用，用來修正誤轉（例如歌手名、專有名詞）。
# 以 # 開頭的行是註解。範例：
# 範列=範例
"""


class ChineseConverter:
    """OpenCC converters loaded only while needed."""

    def __init__(self) -> None:
        self._converters: dict[str, opencc.OpenCC] = {}
        self._depth = 0
        self.loads = 0  # dictionary loads so far

    @contextmanager
    def session(self) -> Iterator["ChineseConverter"]:
        """Keep loaded dictionaries for a batch (a song's lines, translations, title)."""
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._release()

    def _release(self) -> None:
        for name in [n for n in self._converters if n not in RESIDENT]:
            del self._converters[name]

    def loaded(self) -> set[str]:
        return set(self._converters)

    def convert(self, config: str, text: str) -> str:
        converter = self._converters.get(config)
        if converter is None:
            converter = opencc.OpenCC(config)
            self.loads += 1
            if config in RESIDENT or self._depth > 0:
                self._converters[config] = converter
            # otherwise: a one-off conversion, freed as soon as it returns
        return converter.convert(text)

    def is_simplified(self, text: str) -> bool:
        # Japanese shares characters such as 国 with simplified Chinese; kana/hangul text is left alone.
        if not text.strip() or has_kana_or_hangul(text):
            return False
        return not any(ch in TRADITIONAL_ONLY for ch in text) and any(ch in SIMPLIFIED for ch in text)


def parse_vocabulary(lines: Iterable[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        wrong, right = (part.strip() for part in line.split("=", 1))
        if wrong and wrong != right:
            pairs.append((wrong, right))
    # Longest first so a longer phrase wins over a fragment of it.
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def load_vocabulary(path: Path) -> list[tuple[str, str]]:
    try:
        return parse_vocabulary(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("vocabulary unreadable (%s), ignoring it", exc)
        return []


def ensure_vocabulary_file(path: Path) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VOCABULARY_TEMPLATE, encoding="utf-8")
    return path


def apply_vocabulary(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    for wrong, right in pairs:
        text = text.replace(wrong, right)
    return text
