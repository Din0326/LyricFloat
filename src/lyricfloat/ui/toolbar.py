"""Hover toolbar: player controls, offset -/+, cue, font size -/+, lock, settings, hide."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PyQt6.QtWidgets import QBoxLayout, QLabel, QToolButton, QWidget

_STYLE = """
QWidget#toolbar { background: rgba(20, 20, 20, 190); border-radius: 8px; }
QToolButton { color: white; background: transparent; border: none; padding: 2px 6px;
              font-size: 13px; font-family: "Segoe UI", "Microsoft JhengHei"; }
QToolButton:hover { background: rgba(255, 255, 255, 40); border-radius: 4px; }
QToolButton:disabled { color: rgba(255, 255, 255, 90); }
QLabel { color: rgba(255, 255, 255, 220); font-size: 12px; padding: 0 2px;
         font-family: "Segoe UI", "Microsoft JhengHei"; }
"""

_ICON = 14  # logical pixels; the media symbols render as colour emoji, so they are drawn


def _media_icon(kind: str) -> QIcon:
    icon = QIcon()
    for mode, alpha in ((QIcon.Mode.Normal, 255), (QIcon.Mode.Disabled, 90)):
        pm = QPixmap(_ICON * 2, _ICON * 2)
        pm.setDevicePixelRatio(2)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        if kind == "play":
            p.drawPolygon(QPolygonF([QPointF(3, 2), QPointF(12, 7), QPointF(3, 12)]))
        elif kind == "pause":
            p.drawRect(QRectF(3, 2, 3, 10))
            p.drawRect(QRectF(8, 2, 3, 10))
        elif kind == "next":
            p.drawPolygon(QPolygonF([QPointF(2, 2), QPointF(9, 7), QPointF(2, 12)]))
            p.drawRect(QRectF(9.5, 2, 2, 10))
        else:  # previous
            p.drawPolygon(QPolygonF([QPointF(12, 2), QPointF(5, 7), QPointF(12, 12)]))
            p.drawRect(QRectF(2.5, 2, 2, 10))
        p.end()
        icon.addPixmap(pm, mode)
    return icon


def format_offset(offset: float) -> str:
    return "±0.0s" if abs(offset) < 0.05 else f"{offset:+.1f}s"


class Toolbar(QWidget):
    mediaControl = pyqtSignal(str)  # "previous" / "play_pause" / "next" (FR-15)
    offsetStep = pyqtSignal(int)  # +1 = lyrics 0.1 s later, -1 = 0.1 s earlier
    fontStep = pyqtSignal(int)
    lockClicked = pyqtSignal()
    settingsClicked = pyqtSignal()
    revertAlignmentClicked = pyqtSignal()  # FR-22
    cueClicked = pyqtSignal()  # SPEC v1.29
    hideClicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(_STYLE)
        layout = self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)

        def button(text: str, tip: str) -> QToolButton:
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(b)
            return b

        self._icons = {k: _media_icon(k) for k in ("previous", "play", "pause", "next")}
        self.previous = button("", "上一首")
        self.play_pause = button("", "播放")
        self.next = button("", "下一首")
        for b, kind in ((self.previous, "previous"), (self.play_pause, "play"), (self.next, "next")):
            b.setIcon(self._icons[kind])
            b.setIconSize(QSize(_ICON, _ICON))
        layout.addSpacing(8)
        self.earlier = button("−", "歌詞提前 0.1 秒")
        self.offset_label = QLabel("±0.0s", self)
        self.offset_label.setToolTip("這首歌的歌詞偏移")
        layout.addWidget(self.offset_label)
        self.later = button("+", "歌詞延後 0.1 秒")
        self.cue = button("對齊開唱", "")
        self.cue.hide()
        layout.addSpacing(8)
        self.smaller = button("A−", "字級縮小")
        self.bigger = button("A+", "字級放大")
        layout.addSpacing(8)
        self.aligned = button("已自動校正", "這首歌的時間軸已依實際音訊校正。點一下還原為原始時間軸，之後這首不再自動校正")
        self.aligned.hide()
        self.lock = button("鎖定", "鎖定歌詞：滑鼠點擊會穿透到下方視窗，可從系統列解鎖")
        self.settings = button("設定", "開啟設定")
        self.close_ = button("✕", "隱藏歌詞（可從系統列叫回）")

        for held in (self.earlier, self.later):  # hold to keep stepping (SPEC v1.9)
            held.setAutoRepeat(True)
            held.setAutoRepeatDelay(400)
            held.setAutoRepeatInterval(80)
        self.previous.clicked.connect(lambda: self.mediaControl.emit("previous"))
        self.play_pause.clicked.connect(lambda: self.mediaControl.emit("play_pause"))
        self.next.clicked.connect(lambda: self.mediaControl.emit("next"))
        self.earlier.clicked.connect(lambda: self.offsetStep.emit(-1))
        self.later.clicked.connect(lambda: self.offsetStep.emit(1))
        self.smaller.clicked.connect(lambda: self.fontStep.emit(-1))
        self.bigger.clicked.connect(lambda: self.fontStep.emit(1))
        self.lock.clicked.connect(self.lockClicked)
        self.settings.clicked.connect(self.settingsClicked)
        self.aligned.clicked.connect(self.revertAlignmentClicked)
        self.cue.clicked.connect(self.cueClicked)
        self.close_.clicked.connect(self.hideClicked)
        self.set_offset(None)
        self.set_media_state(available=False, playing=False)
        self.adjustSize()

    def set_aligned(self, aligned: bool) -> None:
        """FR-22: show the 'automatically corrected' mark, which also reverts it."""
        self.aligned.setVisible(aligned)
        self.adjustSize()

    def set_cue(self, available: bool, shortcut: str = "") -> None:
        """SPEC v1.29: a version of another length (an MV) starts on the user's cue."""
        key = f"（快捷鍵 {shortcut}）" if shortcut else ""
        self.cue.setToolTip(f"這首的歌詞版本與播放的長度不同（例如 MV）。開唱時按一下，下一句就從現在開始{key}")
        self.cue.setVisible(available)
        self.adjustSize()

    def set_vertical(self, vertical: bool) -> None:
        """FR-16: a column of buttons beside the vertical lyrics."""
        direction = QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)
            self.adjustSize()

    def set_media_state(self, available: bool, playing: bool) -> None:
        """Enable the player buttons when a session is followed; show what play/pause will do."""
        for b in (self.previous, self.play_pause, self.next):
            b.setEnabled(available)
        self.play_pause.setIcon(self._icons["pause" if playing else "play"])
        self.play_pause.setToolTip("暫停" if playing else "播放")

    def set_offset(self, offset: float | None) -> None:
        """None when the current song has no synced lyrics: offset buttons are disabled."""
        enabled = offset is not None
        self.earlier.setEnabled(enabled)
        self.later.setEnabled(enabled)
        self.offset_label.setText("—" if offset is None else format_offset(offset))
