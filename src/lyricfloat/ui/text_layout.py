"""Where a lyric line's characters go, horizontally or vertically (FR-16).

Vertical lines read top to bottom: CJK, kana and hangul stand upright one
per cell, while runs of Latin letters, digits and the punctuation that
turns in vertical writing (brackets, dashes, the long-vowel mark) are
rotated 90 degrees clockwise. Both directions expose the same measures, so
drawing, shrinking, the marquee and the karaoke edge do not care which
way the line runs.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont, QFontMetricsF, QPainterPath, QTransform

# Characters that turn sideways in vertical writing even inside CJK text.
_ROTATED = set("（）()「」『』【】〈〉《》〔〕［］｛｝〜～…‥—―－ー-–~")


def upright(ch: str) -> bool:
    """Stands upright in a vertical line: CJK, kana, hangul and full-width forms."""
    if ch in _ROTATED:
        return False
    o = ord(ch)
    return (
        0x3000 <= o <= 0x30FF  # CJK symbols and punctuation, kana
        or 0x3400 <= o <= 0x9FFF  # CJK ideographs
        or 0xAC00 <= o <= 0xD7AF  # hangul
        or 0xF900 <= o <= 0xFAFF  # CJK compatibility ideographs
        or 0xFF01 <= o <= 0xFF60  # full-width forms
        or 0x1100 <= o <= 0x11FF  # hangul jamo
    )


class LineLayout:
    def __init__(self, text: str, font: QFont, vertical: bool) -> None:
        self.text = text
        self.font = font
        self.vertical = vertical
        self.metrics = QFontMetricsF(font)
        # Per character: where it starts along the reading direction, and its length.
        self._starts: list[float] = []
        self._lengths: list[float] = []
        self._runs: list[tuple[int, int, bool, float]] = []  # (start, end, upright, offset) for vertical
        if vertical:
            self._measure_vertical()
        else:
            self._measure_horizontal()
        self.length = (self._starts[-1] + self._lengths[-1]) if text else 0.0

    def _measure_horizontal(self) -> None:
        m = self.metrics
        for i in range(len(self.text)):
            self._starts.append(m.horizontalAdvance(self.text[:i]))
            self._lengths.append(m.horizontalAdvance(self.text[i]))

    def _measure_vertical(self) -> None:
        m, text = self.metrics, self.text
        offset = 0.0
        i = 0
        while i < len(text):
            if upright(text[i]):
                cell = max(m.horizontalAdvance(text[i]), m.height() * 0.85)
                self._runs.append((i, i + 1, True, offset))
                self._starts.append(offset)
                self._lengths.append(cell)
                offset += cell
                i += 1
                continue
            j = i
            while j < len(text) and not upright(text[j]):
                j += 1
            run = text[i:j]
            self._runs.append((i, j, False, offset))
            for k in range(len(run)):
                self._starts.append(offset + m.horizontalAdvance(run[:k]))
                self._lengths.append(m.horizontalAdvance(run[k]))
            offset += m.horizontalAdvance(run)
            i = j

    def edge(self, done: int, share: float) -> float:
        """Distance along the line to the karaoke fill edge (see karaoke.split_point)."""
        if done >= len(self.text):
            return self.length
        return self._starts[done] + share * self._lengths[done]

    def path(self, along: float, across: float, thickness: float) -> QPainterPath:
        """The line's outline starting at `along` on its reading axis, centred in the
        band [`across`, `across` + `thickness`] on the other axis."""
        m = self.metrics
        centring = (m.ascent() - m.descent()) / 2  # baseline offset that centres the glyph box
        path = QPainterPath()
        if not self.vertical:
            path.addText(QPointF(along, across + thickness / 2 + centring), self.font, self.text)
            return path
        cx = across + thickness / 2
        for start, end, is_upright, offset in self._runs:
            y = along + offset
            if is_upright:
                ch = self.text[start]
                cell = self._lengths[start]
                path.addText(QPointF(cx - m.horizontalAdvance(ch) / 2, y + cell / 2 + centring), self.font, ch)
            else:
                run = QPainterPath()
                run.addText(QPointF(0, 0), self.font, self.text[start:end])
                # Rotate 90 degrees clockwise: the baseline runs down the column.
                turn = QTransform().translate(cx - centring, y).rotate(90)
                path.addPath(turn.map(run))
        return path
