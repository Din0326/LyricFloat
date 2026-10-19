"""Manual lyric search and binding (FR-12).

Searches the enabled sources with a typed title and artist, previews the
selected version and binds it to the song the dialog was opened for. The
dialog only talks to a SearchBackend; network work runs on the asyncio loop.
"""

from __future__ import annotations

from typing import Protocol

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lyricfloat.aio import spawn
from lyricfloat.lyrics.scoring import ScoredCandidate
from lyricfloat.models import Candidate, LyricResult, ResultKind, TrackInfo

SOURCE_NAMES = {"lrclib": "LRCLIB", "netease": "網易雲"}
COLUMNS = ("來源", "歌名", "歌手", "專輯", "時長", "歌詞", "符合度")
LYRICS_COLUMN = COLUMNS.index("歌詞")
PREVIEW_LINES = 12


class SearchBackend(Protocol):
    async def search(self, title: str, artist: str) -> tuple[list[tuple[str, ScoredCandidate]], list[str]]: ...

    async def preview(self, source: str, cand: Candidate) -> tuple[Candidate, LyricResult | None]: ...

    async def bind(self, source: str, cand: Candidate, score: float | None) -> LyricResult: ...

    async def unbind(self) -> None: ...


def _clock(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def duration_text(cand: float, player: float) -> str:
    if cand <= 0:
        return "—"
    if player <= 0:
        return _clock(cand)
    diff = cand - player
    return _clock(cand) if abs(diff) < 0.05 else f"{_clock(cand)}（{diff:+.1f}s）"


def lyrics_text(cand: Candidate) -> str:
    if cand.has_synced:
        return "同步"
    if cand.has_plain:
        return "純文字"
    if cand.instrumental:
        return "純音樂"
    return "點選後載入"


def match_text(sc: ScoredCandidate) -> str:
    text = f"{sc.score:.0%}"
    if sc.rejected == "duration":
        return text + "（時長不符）"
    if sc.rejected == "artist":
        return text + "（歌手不符）"
    if sc.rejected == "language":
        return text + "（其他語言版本）"
    if sc.rejected:
        return text + "（偏低）"
    return text


def preview_text(result: LyricResult | None) -> str:
    if result is None or result.kind is ResultKind.NOT_FOUND:
        return "這個版本沒有歌詞。"
    if result.kind is ResultKind.INSTRUMENTAL:
        return "♪ 純音樂"
    lines = result.lyrics.lines[:PREVIEW_LINES]
    if result.lyrics.synced:
        body = [f"[{_clock(line.start)}.{int(line.start * 10) % 10}] {line.text}" for line in lines]
    else:
        body = [line.text for line in lines]
    more = len(result.lyrics.lines) - len(lines)
    if more > 0:
        body.append(f"…（另有 {more} 行）")
    return "\n".join(body)


class SearchDialog(QDialog):
    def __init__(
        self,
        backend: SearchBackend,
        track: TrackInfo,
        shown: TrackInfo,
        current: LyricResult | None = None,
        parent: QWidget | None = None,
        terms: TrackInfo | None = None,
    ) -> None:
        """`track` is the player's song (durations compare against it); `shown` is
        the same song as displayed (converted script) and names the dialog. `terms`
        pre-fill the fields (SPEC v1.76: a video's found song), `shown` by default."""
        super().__init__(parent)
        self.backend = backend
        self.track = track
        self.setWindowTitle(f"搜尋歌詞 — {shown.describe()}")
        self.resize(820, 540)
        self.hits: list[tuple[str, ScoredCandidate]] = []
        self._current = (current.candidate.source, current.candidate.source_id) if current and current.candidate else None
        self._bound = bool(current and current.bound)
        self._search_id = 0
        self._preview_id = 0
        self._busy = False

        terms = terms or shown
        self.title = QLineEdit(terms.title)
        self.artist = QLineEdit(terms.artist)
        # SPEC v1.77: no artist, the title box is searched as free text.
        self.artist.setPlaceholderText("留空：歌名欄整段搜尋，歌名歌手不分先後")
        self.search_button = QPushButton("搜尋")
        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.start_search)
        self.title.returnPressed.connect(self.start_search)
        self.artist.returnPressed.connect(self.start_search)
        fields = QHBoxLayout()
        fields.addWidget(QLabel("歌名"))
        fields.addWidget(self.title, 3)
        fields.addWidget(QLabel("歌手"))
        fields.addWidget(self.artist, 2)
        fields.addWidget(self.search_button)

        self.status = QLabel("輸入歌名與歌手後按搜尋。")
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_select)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("選一個版本預覽前幾行")
        self.preview.setMaximumHeight(170)

        self.bind_button = QPushButton("套用並綁定")
        self.bind_button.setToolTip("這首歌之後都使用這個版本，自動比對不會再換掉它；偏移會歸零")
        self.bind_button.clicked.connect(self.start_bind)
        self.unbind_button = QPushButton("解除綁定")
        self.unbind_button.setToolTip("改回自動挑選版本")
        self.unbind_button.clicked.connect(self.start_unbind)
        close = QPushButton("關閉")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.bind_button)
        buttons.addWidget(self.unbind_button)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addLayout(fields)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.preview)
        layout.addLayout(buttons)
        self._refresh_buttons()

    # -- state -----------------------------------------------------------------

    def selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _refresh_buttons(self) -> None:
        row = self.selected_row()
        usable = row is not None and lyrics_text(self.hits[row][1].candidate) != "純音樂"
        self.bind_button.setEnabled(usable and not self._busy)
        self.unbind_button.setEnabled(self._bound and not self._busy)
        self.search_button.setEnabled(not self._busy)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_buttons()

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.hits))
        for row, (source, sc) in enumerate(self.hits):
            self._fill_row(row, source, sc)
        self.table.clearSelection()

    def _fill_row(self, row: int, source: str, sc: ScoredCandidate) -> None:
        c = sc.candidate
        current = self._current == (source, c.source_id)
        label = SOURCE_NAMES.get(source, source)
        if current:
            label += "（已綁定）" if self._bound else "（目前）"
        values = (label, c.title, c.artist, c.album, duration_text(c.duration, self.track.duration), lyrics_text(c), match_text(sc))
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if current:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
            self.table.setItem(row, col, item)

    # -- actions -----------------------------------------------------------------

    def start_search(self) -> None:
        title, artist = self.title.text().strip(), self.artist.text().strip()
        if not title:
            self.status.setText("請輸入歌名。")
            return
        self._search_id += 1
        self.status.setText("搜尋中…")
        self._set_busy(True)
        spawn(self._search(self._search_id, title, artist))

    async def _search(self, search_id: int, title: str, artist: str) -> None:
        try:
            hits, errors = await self.backend.search(title, artist)
        except Exception as exc:  # shown in the dialog; the app keeps running
            hits, errors = [], [str(exc)]
        if search_id != self._search_id:
            return  # a newer search is on its way
        self.hits = hits
        self.preview.clear()
        self._fill_table()
        parts = [f"找到 {len(hits)} 個版本。" if hits else "找不到符合的版本，試試只填歌名或改用原文歌名。"]
        if errors:
            parts.append("部分來源無法連線：" + "；".join(errors))
        self.status.setText(" ".join(parts))
        self._set_busy(False)

    def _on_select(self) -> None:
        self._refresh_buttons()
        row = self.selected_row()
        if row is None:
            return
        self._preview_id += 1
        self.preview.setPlainText("載入中…")
        source, sc = self.hits[row]
        spawn(self._preview(self._preview_id, row, source, sc))

    async def _preview(self, preview_id: int, row: int, source: str, sc: ScoredCandidate) -> None:
        try:
            filled, result = await self.backend.preview(source, sc.candidate)
        except Exception as exc:
            if preview_id == self._preview_id:
                self.preview.setPlainText(f"無法載入：{exc}")
            return
        if row < len(self.hits) and self.hits[row][1] is sc:
            # Keep the lyrics so binding does not fetch them again.
            self.hits[row] = (source, ScoredCandidate(filled, sc.score, sc.title_sim, sc.artist_sim, sc.duration_score, sc.rejected, sc.duration_preferred))
            self.table.item(row, LYRICS_COLUMN).setText(lyrics_text(filled))
        if preview_id == self._preview_id:
            self.preview.setPlainText(preview_text(result))
            self._refresh_buttons()

    def start_bind(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        source, sc = self.hits[row]
        self.status.setText("套用中…")
        self._set_busy(True)
        spawn(self._bind(source, sc))

    async def _bind(self, source: str, sc: ScoredCandidate) -> None:
        try:
            result = await self.backend.bind(source, sc.candidate, sc.score)
        except Exception as exc:
            self.status.setText(f"無法套用：{exc}")
            self._set_busy(False)
            return
        self._current = (source, result.candidate.source_id if result.candidate else sc.candidate.source_id)
        self._bound = True
        self._fill_table()
        name = SOURCE_NAMES.get(source, source)
        self.status.setText(f"已綁定 {name} 的「{sc.candidate.title}」，之後播放這首歌都使用這個版本。")
        self._set_busy(False)

    def start_unbind(self) -> None:
        self.status.setText("解除中…")
        self._set_busy(True)
        spawn(self._unbind())

    async def _unbind(self) -> None:
        try:
            await self.backend.unbind()
        except Exception as exc:
            self.status.setText(f"無法解除：{exc}")
            self._set_busy(False)
            return
        self._bound = False
        self._current = None
        self._fill_table()
        self.status.setText("已解除綁定，改回自動挑選版本（正在重新查詢）。")
        self._set_busy(False)
