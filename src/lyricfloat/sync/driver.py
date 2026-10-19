"""Qt driver for SyncEngine: fires exactly at the next line boundary."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from lyricfloat.models import LyricLine, PlaybackAnchor
from lyricfloat.sync.engine import LineUpdate, SyncEngine

log = logging.getLogger(__name__)

BOUNDARY_SLACK_MS = 5  # land just after the boundary so bisect sees the new line


class SyncDriver(QObject):
    lineChanged = pyqtSignal(int, bool)  # line index (-1 = none yet), animate

    def __init__(self, engine: SyncEngine | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine or SyncEngine()
        self._starts: list[float] = []
        self.offset = 0.0  # seconds; positive shows every line later (FR-07)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_timer)

    @property
    def has_lines(self) -> bool:
        return bool(self._starts)

    def set_lines(self, lines: Sequence[LyricLine], offset: float = 0.0) -> None:
        self._starts = [line.start for line in lines]
        self.offset = offset
        self._apply()

    def set_offset(self, offset: float) -> None:
        """Shift every line; repositions immediately without the fade."""
        self.offset = offset
        if self._starts:
            self._apply()

    def _apply(self) -> None:
        self._emit(self.engine.set_lines([s + self.offset for s in self._starts]))
        self._reschedule()

    def clear_lines(self) -> None:
        self._starts = []
        self.offset = 0.0
        self.engine.clear_lines()
        self._timer.stop()

    def reset(self) -> None:
        self._starts = []
        self.offset = 0.0
        self.engine.reset()
        self._timer.stop()

    def on_anchor(self, anchor: PlaybackAnchor) -> None:
        self._emit(self.engine.update(anchor))
        self._reschedule()

    def _on_timer(self) -> None:
        self._emit(self.engine.tick())
        self._reschedule()

    def _emit(self, update: LineUpdate | None) -> None:
        if update is None:
            return
        if update.reason in ("seek", "loop"):
            log.info("%s -> line %d", update.reason, update.index)
        else:
            log.debug("line %d (%s) at pos %.3f", update.index, update.reason, self.engine.position() or 0.0)
        self.lineChanged.emit(update.index, update.animate)

    def _reschedule(self) -> None:
        delay = self.engine.seconds_to_next()
        if delay is None:
            self._timer.stop()
            return
        self._timer.start(max(1, int(delay * 1000) + BOUNDARY_SLACK_MS))
