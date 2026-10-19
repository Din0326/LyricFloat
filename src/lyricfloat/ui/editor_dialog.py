"""Lyric editor (FR-17): change each line's time and text, add or remove lines.

Times are where a line shows during playback, the song's offset already
included; saving stores a "local edited version" with offset 0, bound to
the song like a manual pick. The version it replaced is kept and can be
restored. The dialog only talks to an EditorBackend.

Lyrics without real times (pasted whole, or plain lyrics opened as they are)
stay "untimed" until the user changes a time: saved like that, they become a
plain edited version that L3 may time (SPEC v1.26).

A line's new time moves every line by as much while "all lines" is ticked, as
it is when the dialog opens; unticked, only that line moves (SPEC v1.78).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lyricfloat.aio import spawn
from lyricfloat.lyrics.lrc import format_time, parse_time
from lyricfloat.models import LyricLine, TrackInfo

TIME, TEXT = 0, 1
STEP = 0.1
PASTE_SPACING = 3.0  # seconds between pasted lines when the song length is unknown
TIMED_NOTE = "時間可直接點兩下修改（例：1:05.30）。儲存後這首歌改用編輯版，自動校正不會覆蓋。"
UNTIMED_NOTE = (
    "這份歌詞還沒有時間軸（表上的時間只是平均分配）。直接儲存會存成純文字編輯版，開啟自動時間軸校正時，"
    "完整播完一次就會自動對時；調整任何一行的時間，就改成照你的時間儲存。"
)
TRANSLATION_ROLE = Qt.ItemDataRole.UserRole  # a row's translation rides on its text cell
PLAYING = QColor(79, 195, 247, 60)  # the sung colour, faint: the line playing now


class EditorBackend(Protocol):
    def position(self) -> float | None:
        """Playback position in player time (where lines show), or None."""

    async def save(self, lines: Sequence[LyricLine], translations: Sequence[str | None]) -> None: ...

    async def save_plain(self, texts: Sequence[str]) -> None: ...

    async def revert(self) -> None: ...


class EditorDialog(QDialog):
    def __init__(
        self,
        backend: EditorBackend,
        shown: TrackInfo,
        lines: Sequence[LyricLine],
        translations: Sequence[str | None] | None = None,
        edited: bool = False,
        parent: QWidget | None = None,
        untimed: bool = False,
    ) -> None:
        """`untimed`: the lines' times are placeholders (plain lyrics spread over the song)."""
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle(f"編輯歌詞 — {shown.describe()}")
        self.resize(620, 560)
        self._duration = shown.duration
        self._untimed = untimed
        self._edited = edited
        self._dirty = False
        self._busy = False
        self._loading = False

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["時間", "歌詞"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(TEXT, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)

        def button(text: str, tip: str, slot) -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self.now_button = button("設為目前播放位置", "選取行的時間改成現在播到的位置；邊聽邊按可以逐行對時", self.set_to_now)
        self.earlier_button = button("−0.1 秒", "選取的行提前 0.1 秒", lambda: self.shift(-STEP))
        self.later_button = button("+0.1 秒", "選取的行延後 0.1 秒", lambda: self.shift(STEP))
        self.paste_button = button("貼上整首歌詞…", "一次貼上整首歌詞，取代表格內容；還沒對時的歌詞可交給自動時間軸校正", self.paste_lyrics)
        self.add_button = button("新增行", "在選取行下方加一行（沒有選取時加在最後）", self.add_line)
        self.delete_button = button("刪除行", "刪除選取的行", self.delete_lines)
        tools = QHBoxLayout()
        for b in (self.now_button, self.earlier_button, self.later_button, self.paste_button, self.add_button, self.delete_button):
            tools.addWidget(b)
        tools.addStretch(1)
        self.move_all = QCheckBox("同時調整所有歌詞的時間")
        self.move_all.setToolTip(
            "勾選時，改任何一行的時間（點兩下修改、±0.1 秒、設為目前播放位置），其他行都加減同樣的秒數；取消勾選只改那一行"
        )
        self.move_all.setChecked(True)

        self.status = QLabel(UNTIMED_NOTE if untimed else TIMED_NOTE)
        self.status.setWordWrap(True)

        self.revert_button = button("還原為原始版本", "放棄所有編輯，改回編輯前的版本", self.start_revert)
        self.save_button = button("儲存", "存成這首歌的本地編輯版", self.start_save)
        self.save_button.setDefault(True)
        close = button("關閉", "", self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self.revert_button)
        bottom.addStretch(1)
        bottom.addWidget(self.save_button)
        bottom.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(tools)
        layout.addWidget(self.move_all)
        layout.addWidget(self.status)
        layout.addLayout(bottom)

        self.load(lines, translations)
        self._playing_row = -1
        self._follow = QTimer(self, interval=250)
        self._follow.timeout.connect(self._mark_playing)
        self._follow.start()

    # -- table -------------------------------------------------------------

    def load(self, lines: Sequence[LyricLine], translations: Sequence[str | None] | None = None) -> None:
        self._loading = True
        self.table.setRowCount(0)
        for i, line in enumerate(lines):
            translation = translations[i] if translations is not None and i < len(translations) else None
            self._insert(self.table.rowCount(), line.start, line.text, translation)
        self._loading = False
        self._dirty = False
        self._refresh_buttons()

    def _insert(self, row: int, start: float, text: str, translation: str | None = None) -> None:
        self.table.insertRow(row)
        time_item = QTableWidgetItem(format_time(start))
        time_item.setData(Qt.ItemDataRole.UserRole, start)
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        text_item = QTableWidgetItem(text)
        text_item.setData(TRANSLATION_ROLE, translation)
        self.table.setItem(row, TIME, time_item)
        self.table.setItem(row, TEXT, text_item)

    def time_of(self, row: int) -> float:
        return float(self.table.item(row, TIME).data(Qt.ItemDataRole.UserRole))

    def _set_time(self, row: int, seconds: float) -> None:
        """A time the user set: from now on the lyrics are timed by hand. With "all lines"
        ticked, the other lines move by as much as this one did (SPEC v1.78)."""
        if self._untimed:
            self._untimed = False
            self.status.setText(TIMED_NOTE)
        seconds = max(0.0, seconds)
        delta = seconds - self.time_of(row)  # the cell still holds the old time
        rows = range(self.table.rowCount()) if self.move_all.isChecked() else (row,)
        self._loading = True
        for r in rows:
            start = seconds if r == row else max(0.0, round(self.time_of(r) + delta, 3))
            item = self.table.item(r, TIME)
            item.setData(Qt.ItemDataRole.UserRole, start)
            item.setText(format_time(start))
        self._loading = False
        self._changed()

    def lines(self) -> tuple[list[LyricLine], list[str | None]]:
        """The table as lines plus each line's translation (unsorted; saving sorts)."""
        lines, translations = [], []
        for row in range(self.table.rowCount()):
            lines.append(LyricLine(self.time_of(row), self.table.item(row, TEXT).text().strip()))
            translations.append(self.table.item(row, TEXT).data(TRANSLATION_ROLE))
        return lines, translations

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        if item.column() == TIME:
            seconds = parse_time(item.text())
            if seconds is None:
                self.status.setText(f"「{item.text()}」不是時間，請用 分:秒.百分秒（例：1:05.30）。")
                seconds = float(item.data(Qt.ItemDataRole.UserRole))
            self._set_time(item.row(), seconds)
            return
        self._changed()

    def _changed(self) -> None:
        self._dirty = True
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        has_rows = bool(self.selected_rows())
        for b in (self.now_button, self.earlier_button, self.later_button, self.delete_button):
            b.setEnabled(has_rows and not self._busy)
        self.now_button.setEnabled(has_rows and not self._busy and self.backend.position() is not None)
        self.add_button.setEnabled(not self._busy)
        self.paste_button.setEnabled(not self._busy)
        self.save_button.setEnabled(self.table.rowCount() > 0 and not self._busy)
        self.revert_button.setEnabled(self._edited and not self._busy)

    def _mark_playing(self) -> None:
        """Tint the line playing now, so lines can be timed while listening."""
        position = self.backend.position()
        row = -1
        if position is not None:
            for r in range(self.table.rowCount()):
                if self.time_of(r) <= position and (row < 0 or self.time_of(r) >= self.time_of(row)):
                    row = r
        if row == self._playing_row:
            return
        self._loading = True
        for r, brush in ((self._playing_row, QBrush()), (row, QBrush(PLAYING))):
            if 0 <= r < self.table.rowCount():
                for c in (TIME, TEXT):
                    self.table.item(r, c).setBackground(brush)
        self._loading = False
        self._playing_row = row

    # -- actions -------------------------------------------------------------

    def set_to_now(self) -> None:
        position = self.backend.position()
        rows = self.selected_rows()
        if position is None or not rows:
            return
        self._set_time(rows[0], position)
        if rows[0] + 1 < self.table.rowCount() and len(rows) == 1:
            self.table.selectRow(rows[0] + 1)  # ready for the next line

    def shift(self, seconds: float) -> None:
        rows = self.selected_rows()
        if self.move_all.isChecked():
            rows = rows[:1]  # every line moves once, however many are selected
        for row in rows:
            self._set_time(row, round(self.time_of(row) + seconds, 2))

    @property
    def untimed(self) -> bool:
        return self._untimed

    def paste_lyrics(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "貼上整首歌詞", "一行一句，空白行會略過。貼上後取代表格裡的所有歌詞，時間先平均分配："
        )
        texts = [line.strip() for line in text.splitlines() if line.strip()] if ok else []
        if not texts:
            return
        span = self._duration if self._duration > 0 else PASTE_SPACING * len(texts)
        self.load([LyricLine(span * i / len(texts), t) for i, t in enumerate(texts)])
        self._untimed = True
        self._playing_row = -1
        self.status.setText(UNTIMED_NOTE)
        self._changed()

    def add_line(self) -> None:
        rows = self.selected_rows()
        row = rows[-1] + 1 if rows else self.table.rowCount()
        start = self.time_of(row - 1) + 1.0 if row > 0 else 0.0
        self._insert(row, start, "")
        self.table.selectRow(row)
        self.table.editItem(self.table.item(row, TEXT))
        self._changed()

    def delete_lines(self) -> None:
        for row in reversed(self.selected_rows()):
            self.table.removeRow(row)
        self._playing_row = -1
        self._changed()

    def start_save(self) -> None:
        lines, translations = self.lines()
        if not any(line.text for line in lines):
            self.status.setText("至少要有一行歌詞。")
            return
        self._set_busy(True, "儲存中…")
        if self._untimed:
            spawn(self._save_plain([line.text for line in lines]))  # table order is the lyric order
        else:
            spawn(self._save(lines, translations))

    async def _save_plain(self, texts) -> None:
        try:
            await self.backend.save_plain(texts)
        except Exception as exc:
            self._set_busy(False, f"無法儲存：{exc}")
            return
        self._edited = True
        self._dirty = False
        self._set_busy(False, "已存成純文字編輯版。開啟自動時間軸校正時，完整播完一次就會自動對時。")

    async def _save(self, lines, translations) -> None:
        try:
            await self.backend.save(lines, translations)
        except Exception as exc:  # shown in the dialog; the app keeps running
            self._set_busy(False, f"無法儲存：{exc}")
            return
        self._edited = True
        self._dirty = False
        order = sorted(range(len(lines)), key=lambda i: lines[i].start)
        self.load([lines[i] for i in order], [translations[i] for i in order])
        self._set_busy(False, "已儲存，這首歌之後都使用編輯版。")

    def start_revert(self) -> None:
        answer = QMessageBox.question(self, "還原為原始版本", "放棄所有編輯，改回編輯前的版本？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, "還原中…")
        spawn(self._revert())

    async def _revert(self) -> None:
        try:
            await self.backend.revert()
        except Exception as exc:
            self._set_busy(False, f"無法還原：{exc}")
            return
        self._edited = False
        self._dirty = False
        self._busy = False
        self.status.setText("已還原為原始版本。")
        self.close()

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.status.setText(message)
        self._refresh_buttons()

    def closeEvent(self, event) -> None:
        if self._dirty and not self._busy:
            answer = QMessageBox.question(self, "編輯歌詞", "有未儲存的修改，要放棄嗎？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._follow.stop()
        super().closeEvent(event)
