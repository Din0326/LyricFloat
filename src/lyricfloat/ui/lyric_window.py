"""LyricWindow: frameless, transparent, always-on-top two-line lyric display.

Locked: input passes through to the window below. Unlocked: the window
takes the mouse; dragging the body moves it, dragging an edge resizes it,
and 200 ms after the pointer enters a dark backdrop and the toolbar fade in.

Second line (SPEC v1.8): the translation when the song has translations
and they are switched on (a line without one stays single); otherwise the
next line.
"""

from __future__ import annotations

import functools
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PyQt6.QtCore import QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import QWidget

from lyricfloat.models import LyricLine, TrackInfo
from lyricfloat.settings import Settings
from lyricfloat.sync.karaoke import KaraokeTimeline, split_point
from lyricfloat.ui.text_layout import LineLayout
from lyricfloat.ui.toolbar import Toolbar

log = logging.getLogger(__name__)

WAITING_TEXT = "等待播放中…"
NOT_FOUND_TEXT = "找不到歌詞"
PLAIN_ONLY_TEXT = "無同步歌詞"
INSTRUMENTAL_TEXT = "♪ 純音樂"
UNSYNCED_BADGE = "未同步"
SPRITE_CACHE = 8  # rendered lines kept: current, previous (fading), next, translation
LAYOUT_CACHE = 32  # measured lines, including the shrunk sizes tried while fitting

NEXT = "next"
TRANSLATION = "translation"

# SPEC v1.81 line change: quick start, gentle landing.
_EASE = QEasingCurve(QEasingCurve.Type.OutCubic)
DRIFT = 0.35  # of the distance between the two lines: how far a crossfading pair slides up
RISE = 0.25  # of that distance: how far below its place the new next line starts


@dataclass(frozen=True, slots=True)
class LyricStyle:
    """Spec defaults (UI/UX section); built from Settings by style_from_settings."""

    families: tuple[str, ...] = ("Segoe UI", "Microsoft JhengHei")
    main_px: int = 32
    secondary_ratio: float = 0.75
    next_opacity: float = 0.6
    translation_opacity: float = 0.8
    min_shrink: float = 0.8  # shrink an overlong line down to 80% before scrolling it
    text_color: str = "#FFFFFF"
    outline_px: float = 2.0
    outline_rgba: tuple[int, int, int, int] = (0, 0, 0, round(255 * 0.7))
    shadow_blur: float = 6.0
    shadow_offset: tuple[float, float] = (0.0, 2.0)
    alignment: str = "center"
    line_change_ms: int = 250  # 0 turns the line-change animation off
    hover_backdrop_opacity: float = 0.4
    hover_delay_ms: int = 200
    waiting_hold_ms: int = 3000
    margin_from_taskbar: int = 80
    marquee_speed: float = 80.0  # px per second
    marquee_pause_ms: int = 800
    karaoke: bool = True  # FR-11 fill on the current line
    sung_color: str = "#4FC3F7"
    obs_background: str = "#00FF00"  # FR-18: solid colour behind the OBS window's lyrics
    vertical: bool = False  # FR-16: two columns read top to bottom, current line on the left


def style_from_settings(s: Settings) -> LyricStyle:
    outline = QColor(s.outline_color)
    return LyricStyle(
        families=(s.font_latin, s.font_cjk),
        main_px=s.font_px,
        text_color=s.text_color,
        outline_px=s.outline_px,
        outline_rgba=(outline.red(), outline.green(), outline.blue(), round(255 * min(1.0, s.outline_opacity))),
        shadow_blur=s.shadow_blur,
        shadow_offset=(0.0, s.shadow_offset_y),
        alignment=s.alignment,
        line_change_ms=250 if s.line_animation else 0,
        karaoke=s.karaoke,
        sung_color=s.sung_color,
        obs_background=s.obs_background,
        vertical=s.vertical,
    )


class _Sprite:
    """One line drawn once: shadow, outline and text; the sung colour on demand."""

    def __init__(self, base: QPixmap, margin: int, render_sung, layout: LineLayout) -> None:
        self.base = base
        self.margin = margin
        self.layout = layout
        self._render_sung = render_sung
        self.sung: QPixmap | None = None

    def ensure_sung(self) -> None:
        if self.sung is None:
            self.sung = self._render_sung()


def _mirrored(method):
    """Content calls on the floating window are repeated on its mirrors (the OBS window)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        for mirror in self.mirrors:
            getattr(mirror, method.__name__)(*args, **kwargs)
        return result

    return wrapper


@dataclass(frozen=True, slots=True)
class DisplayText:
    primary: str = ""
    secondary: str = ""
    secondary_kind: str = NEXT
    line: int = -1  # index of the lyric line in `primary`, for the karaoke fill


class LyricWindow(QWidget):
    geometryCommitted = pyqtSignal(QRect)  # after a move or resize settles
    mediaControlRequested = pyqtSignal(str)
    offsetStepRequested = pyqtSignal(int)
    fontStepRequested = pyqtSignal(int)
    lockRequested = pyqtSignal()
    settingsRequested = pyqtSignal()
    hideRequested = pyqtSignal()
    revertAlignmentRequested = pyqtSignal()  # FR-22
    cueRequested = pyqtSignal()  # SPEC v1.29
    closed = pyqtSignal()  # OBS window closed from its title bar

    EDGE = 6  # px from the border that resizes instead of moving
    OBS_TITLE = "LyricFloat 歌詞（OBS）"

    def __init__(self, style: LyricStyle | None = None, obs: bool = False) -> None:
        """`obs`: FR-18 window for OBS window capture: an ordinary window with a title
        bar and taskbar entry, a solid background, no toolbar and no badges."""
        if obs:
            super().__init__(None, Qt.WindowType.Window)
        else:
            super().__init__(
                None,
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
            )
        self.obs = obs
        self.mirrors: list[LyricWindow] = []
        self.style_ = style or LyricStyle()
        if not obs:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(self.OBS_TITLE if obs else "LyricFloat")
        self.setMouseTracking(not obs)
        self.setMinimumSize(240, 60)
        # No QGraphicsEffect here: on a translucent top-level window on Windows
        # it freezes the on-screen content after the first paint (update() and
        # repaint() no longer reach the screen, while widget.grab() still looks
        # right). The shadow is drawn by hand in _draw_line instead.

        self._locked = False
        self._lines: tuple[LyricLine, ...] = ()
        self._translations: tuple[str | None, ...] | None = None
        self._show_translation = True
        self._unsynced = False
        self._cue_hint: str | None = None  # SPEC v1.36: the start finder is listening
        self._status_badge: str | None = None  # e.g. calibration progress
        self._flash: str | None = None  # brief feedback for shortcuts used while the toolbar is hidden
        self._flash_timer = QTimer(self, singleShot=True)
        self._flash_timer.timeout.connect(lambda: self.flash(None))
        self._track: TrackInfo | None = None
        self._index = -1
        self._karaoke: KaraokeTimeline | None = None
        self._words: tuple | None = None
        self._clock: Callable[[], float | None] | None = None  # lyric-time position
        self._fill = -1.0
        self._sprites: dict[tuple, _Sprite] = {}
        self._layouts: dict[tuple, LineLayout] = {}
        self._karaoke_timer = QTimer(self, interval=33)
        self._karaoke_timer.timeout.connect(self._on_karaoke)

        self._text = DisplayText()
        self._prev_text = DisplayText()
        self._prev_scroll = (0.0, 0.0)  # where the outgoing lines' marquee stood
        self._change_t = 1.0  # line change progress, linear in time
        self._text_opacity = 1.0  # for the waiting fade-out
        self._backdrop = 0.0

        self._change = QVariantAnimation(self, startValue=0.0, endValue=1.0)
        self._change.valueChanged.connect(self._on_change)
        self._warm_timer = QTimer(self, singleShot=True)  # once the change is over
        self._warm_timer.timeout.connect(self._warm_next)

        self._backdrop_anim = QVariantAnimation(self, duration=150, easingCurve=QEasingCurve.Type.OutCubic)
        self._backdrop_anim.valueChanged.connect(self._on_backdrop)

        self._hover_timer = QTimer(self, singleShot=True)
        self._hover_timer.timeout.connect(self._show_controls)

        self._waiting_timer = QTimer(self, singleShot=True)
        self._waiting_timer.timeout.connect(self._fade_out_text)
        self._text_fade = QVariantAnimation(self, duration=400, startValue=1.0, endValue=0.0)
        self._text_fade.valueChanged.connect(self._on_text_fade)

        # SPEC v1.45: nothing playing for a while, the lyrics fade out (the text is kept for
        # when playback resumes; the window stays, so its toolbar can still start playback).
        self._idle = False
        self._idle_opacity = 1.0
        self._idle_fade = QVariantAnimation(self, duration=400)
        self._idle_fade.valueChanged.connect(self._on_idle_fade)

        # Marquee for lines that still do not fit at 80%: one timer drives both lines.
        self._overflow = [0.0, 0.0]
        self._scroll = [0.0, 0.0]
        self._marquee_elapsed = 0.0
        self._marquee = QTimer(self, interval=33)
        self._marquee.timeout.connect(self._on_marquee)

        self._commit_timer = QTimer(self, singleShot=True, interval=500)
        self._commit_timer.timeout.connect(lambda: self.geometryCommitted.emit(self.geometry()))

        self.toolbar = Toolbar(self)
        self.toolbar.hide()
        self.toolbar.mediaControl.connect(self.mediaControlRequested)
        self.toolbar.offsetStep.connect(self.offsetStepRequested)
        self.toolbar.fontStep.connect(self.fontStepRequested)
        self.toolbar.lockClicked.connect(self.lockRequested)
        self.toolbar.settingsClicked.connect(self.settingsRequested)
        self.toolbar.revertAlignmentClicked.connect(self.revertAlignmentRequested)
        self.toolbar.cueClicked.connect(self.cueRequested)
        self.toolbar.hideClicked.connect(self.hideRequested)

        self.apply_style(self.style_)
        self._place_default()
        screens = QGuiApplication.instance()
        screens.screenRemoved.connect(lambda _s: self.ensure_on_screen())
        screens.primaryScreenChanged.connect(lambda _s: self.ensure_on_screen())
        screens.screenAdded.connect(self._watch_screen)
        for screen in QGuiApplication.screens():
            self._watch_screen(screen)

    # -- style -------------------------------------------------------------

    @_mirrored
    def apply_style(self, style: LyricStyle) -> None:
        turned = style.vertical != self.style_.vertical
        self.style_ = style
        self._sprites.clear()
        self._layouts.clear()
        self._change.setDuration(max(1, style.line_change_ms))
        self._warm_timer.setInterval(style.line_change_ms + 50)
        self._hover_timer.setInterval(style.hover_delay_ms)
        self._waiting_timer.setInterval(style.waiting_hold_ms)
        self.toolbar.set_vertical(style.vertical)
        needed = self._content_height()
        if style.vertical:
            self.setMinimumSize(60, 240)
            if turned:
                self._place_default()
            elif self.width() < needed:
                self.resize(needed, self.height())
        else:
            self.setMinimumSize(240, 60)
            if turned:
                self._place_default()
            elif self.height() < needed:
                self.resize(self.width(), needed)
        self._place_toolbar()
        self._restart_marquee()
        self.update()

    @_mirrored
    def set_show_translation(self, show: bool) -> None:
        self._show_translation = show
        if self._lines:
            self._set_text(self._texts_for(self._index), animate=False)

    def _content_height(self) -> int:
        """Room the two lines need across the reading direction (the width when vertical)."""
        s = self.style_
        return int(s.main_px * 1.6 + s.main_px * s.secondary_ratio * 1.6 + 24)

    # -- geometry ----------------------------------------------------------

    def _default_geometry(self) -> QRect:
        avail = QGuiApplication.primaryScreen().availableGeometry()
        if self.obs:
            width, height = min(1280, int(avail.width() * 0.6)), self._content_height() + 40
            return QRect(avail.x() + (avail.width() - width) // 2, avail.y() + avail.height() // 4, width, height)
        if self.style_.vertical:
            # Near the right edge of the primary screen, vertically centred.
            width, height = self._content_height(), min(900, int(avail.height() * 0.7))
            x = avail.x() + avail.width() - self.style_.margin_from_taskbar - width
            return QRect(x, avail.y() + (avail.height() - height) // 2, width, height)
        width = min(1400, int(avail.width() * 0.7))
        height = self._content_height()
        x = avail.x() + (avail.width() - width) // 2
        y = avail.y() + avail.height() - self.style_.margin_from_taskbar - height
        return QRect(x, y, width, height)

    def _place_default(self) -> None:
        self.setGeometry(self._default_geometry())

    def restore_geometry(self, rect: Sequence[int] | None) -> None:
        if rect and len(rect) == 4:
            self.setGeometry(QRect(*rect))
            self.ensure_on_screen()
        else:
            self._place_default()

    def _watch_screen(self, screen) -> None:
        screen.availableGeometryChanged.connect(lambda _r: self.ensure_on_screen())

    def ensure_on_screen(self) -> None:
        """Move back to the default spot when the window is no longer visible on any screen."""
        frame = self.frameGeometry()
        for screen in QGuiApplication.screens():
            visible = screen.availableGeometry().intersected(frame)
            if visible.width() >= 100 and visible.height() >= frame.height() // 2:
                return
        log.info("window off-screen, moving back to default position")
        self._place_default()

    def moveEvent(self, event) -> None:
        self._commit_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._commit_timer.start()
        self._place_toolbar()
        self._restart_marquee()
        super().resizeEvent(event)

    def _place_toolbar(self) -> None:
        self.toolbar.adjustSize()
        if self.style_.vertical:
            self.toolbar.move(max(2, self.width() - self.toolbar.width() - 4), 8)
        else:
            self.toolbar.move(max(4, self.width() - self.toolbar.width() - 8), 4)

    # -- lock / mouse ------------------------------------------------------

    @property
    def locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        if locked == self._locked:
            return
        self._locked = locked
        was_visible = self.isVisible()
        geometry = self.geometry()
        self._hide_controls()
        # Changing window flags recreates the native window, which hides it.
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, locked)
        self.setGeometry(geometry)
        if was_visible:
            self.show()

    def enterEvent(self, event) -> None:
        if not self._locked and not self.obs:
            self._hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hide_controls()
        self.unsetCursor()
        super().leaveEvent(event)

    def _show_controls(self) -> None:
        self._animate_backdrop(1.0)
        self._place_toolbar()
        self.toolbar.show()
        self.toolbar.raise_()

    def _hide_controls(self) -> None:
        self._hover_timer.stop()
        self._animate_backdrop(0.0)
        self.toolbar.hide()

    def _edges_at(self, pos) -> Qt.Edge:
        edges = Qt.Edge(0)
        if pos.x() <= self.EDGE:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - self.EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= self.EDGE:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - self.EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event) -> None:
        if not self._locked and not self.obs and event.buttons() == Qt.MouseButton.NoButton:
            edges = self._edges_at(event.position().toPoint())
            horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
            vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
            if horizontal and vertical:
                diagonal = edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if diagonal else Qt.CursorShape.SizeBDiagCursor)
            elif horizontal:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif vertical:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if not self._locked and not self.obs and event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                edges = self._edges_at(event.position().toPoint())
                if edges:
                    handle.startSystemResize(edges)
                else:
                    handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    def closeEvent(self, event) -> None:
        if self.obs:
            self.closed.emit()
        super().closeEvent(event)

    # -- mirrors (FR-18) -----------------------------------------------------

    def attach_mirror(self, mirror: LyricWindow) -> None:
        """Start repeating content on `mirror`, beginning with what is on screen now."""
        if mirror in self.mirrors:
            return
        mirror.apply_style(self.style_)
        mirror.set_show_translation(self._show_translation)
        mirror.set_clock(self._clock)
        if self._lines:
            mirror.set_lyrics(self._track, self._lines, self._translations, self._unsynced, self._words)
            mirror.show_line(self._index, animate=False)
        else:
            mirror._track = self._track
            mirror._reset_lyrics()
            mirror._set_text(self._text, animate=False)
        mirror.set_idle(self._idle)
        self.mirrors.append(mirror)

    def detach_mirror(self, mirror: LyricWindow) -> None:
        if mirror in self.mirrors:
            self.mirrors.remove(mirror)

    # -- content API -------------------------------------------------------

    def _reset_lyrics(self) -> None:
        self._lines = ()
        self._translations = None
        self._unsynced = False
        self._cue_hint = None
        self._index = -1
        self._karaoke = None
        self._karaoke_timer.stop()

    @_mirrored
    def show_waiting(self) -> None:
        self._track = None
        self._reset_lyrics()
        self._set_text(DisplayText(WAITING_TEXT), animate=False)
        self._waiting_timer.start()

    @_mirrored
    def show_loading(self, track: TrackInfo) -> None:
        self._track = track
        self._reset_lyrics()
        self._set_text(DisplayText(track.describe()), animate=False)

    @_mirrored
    def show_message(self, primary: str, secondary: str = "") -> None:
        self._reset_lyrics()
        self._set_text(DisplayText(primary, secondary), animate=False)

    @_mirrored
    def clear(self) -> None:
        self._reset_lyrics()
        self._set_text(DisplayText(), animate=False)

    @_mirrored
    def set_lyrics(
        self,
        track: TrackInfo,
        lines: Sequence[LyricLine],
        translations: Sequence[str | None] | None = None,
        unsynced: bool = False,
        words: Sequence | None = None,
    ) -> None:
        """`words`: L3 word timings per line, for word-level karaoke (FR-21)."""
        self._track = track
        self._lines = tuple(lines)
        self._translations = tuple(translations) if translations is not None else None
        self._unsynced = unsynced
        self._words = tuple(words) if words is not None else None
        self._index = -1
        self._karaoke = None if unsynced else KaraokeTimeline(self._lines, self._words)
        self._set_text(self._texts_for(-1), animate=False)
        self._warm_timer.start()

    @_mirrored
    def show_line(self, index: int, animate: bool) -> None:
        if not self._lines:
            return
        self._index = index
        self._set_text(self._texts_for(index), animate=animate)
        if self._karaoke_active() and index >= 0:
            self._karaoke_timer.start()
        self._warm_timer.start()

    def _warm_next(self) -> None:
        """Draw the lines of the next change ahead of time: drawing one takes 10-50 ms,
        a stall the eye catches as the move starts."""
        if not self._lines or self._index + 1 >= len(self._lines) or not self.isVisible():
            return
        s = self.style_
        top, bottom = self._line_rects()
        upcoming = self._texts_for(self._index + 1)
        if upcoming.primary:
            self._place(upcoming.primary, s.main_px, top, 0.0, sung=self._karaoke_active())
        if upcoming.secondary:
            self._place(upcoming.secondary, s.main_px * s.secondary_ratio, bottom, 0.0)

    @_mirrored
    def set_clock(self, clock: Callable[[], float | None]) -> None:
        """Where playback is in lyric time (offset already taken out), for the karaoke fill."""
        self._clock = clock

    def _karaoke_active(self) -> bool:
        return self.style_.karaoke and self._karaoke is not None and self._clock is not None

    def fill_of(self, text: DisplayText) -> float:
        if text.line < 0 or not self._karaoke_active():
            return 0.0
        return self._karaoke.fraction(text.line, self._clock())

    def _on_karaoke(self) -> None:
        if not self._karaoke_active():
            self._karaoke_timer.stop()
            return
        if not self.isVisible():
            return
        fill = self.fill_of(self._text)
        if abs(fill - self._fill) > 1e-3:  # paused or done: nothing to redraw
            self._fill = fill
            self.update(self._line_rects()[0].toAlignedRect().adjusted(-4, -4, 4, 4 + round(self.style_.shadow_blur)))

    @_mirrored
    def set_cue_hint(self, text: str | None) -> None:
        self._cue_hint = text
        self.update()

    def set_cue_available(self, available: bool, shortcut: str = "") -> None:
        self.toolbar.set_cue(available, shortcut)
        self._place_toolbar()

    def set_media_state(self, available: bool, playing: bool) -> None:
        self.toolbar.set_media_state(available, playing)
        self._place_toolbar()

    def set_status_badge(self, text: str | None) -> None:
        self._status_badge = text
        self.update()

    def flash(self, text: str | None, ms: int = 1500) -> None:
        """Show `text` in the corner badge for `ms`, then return to the status badge."""
        self._flash = text
        if text:
            self._flash_timer.start(ms)
        self.update()

    def set_aligned(self, aligned: bool) -> None:
        self.toolbar.set_aligned(aligned)
        self._place_toolbar()

    def set_offset_label(self, offset: float | None) -> None:
        self.toolbar.set_offset(offset)
        self._place_toolbar()

    def _translating(self) -> bool:
        return self._show_translation and self._translations is not None and any(self._translations)

    def _texts_for(self, index: int) -> DisplayText:
        if index < 0:
            intro = self._track.describe() if self._track else ""
            first = self._lines[0].text if self._lines else ""
            return DisplayText(intro, first, NEXT)
        current = self._lines[index].text
        if self._translating():
            translation = self._translations[index] if index < len(self._translations) else None
            return DisplayText(current, translation or "", TRANSLATION, index)
        nxt = self._lines[index + 1].text if index + 1 < len(self._lines) else ""
        return DisplayText(current, nxt, NEXT, index)

    # -- animation plumbing ------------------------------------------------

    def _set_text(self, text: DisplayText, animate: bool) -> None:
        self._waiting_timer.stop()
        self._text_fade.stop()
        self._text_opacity = 1.0
        if text == self._text:
            self.update()
            return
        self._prev_text = self._text
        self._prev_scroll = (self._scroll[0], self._scroll[1])
        self._text = text
        self._change.stop()
        if animate and self.style_.line_change_ms > 0:
            self._change_t = 0.0
            self._change.start()
        else:
            self._change_t = 1.0
        self._restart_marquee()
        self.update()

    def _on_change(self, value) -> None:
        self._change_t = float(value)
        self.update()

    @property
    def idle(self) -> bool:
        return self._idle

    @_mirrored
    def set_idle(self, idle: bool) -> None:
        """Fade the lyrics out while nothing plays, back in when playback resumes."""
        if idle == self._idle:
            return
        self._idle = idle
        self._idle_fade.stop()
        self._idle_fade.setStartValue(self._idle_opacity)
        self._idle_fade.setEndValue(0.0 if idle else 1.0)
        self._idle_fade.start()

    def _on_idle_fade(self, value) -> None:
        self._idle_opacity = float(value)
        self.update()

    def _fade_out_text(self) -> None:
        self._text_fade.start()

    def _on_text_fade(self, value) -> None:
        self._text_opacity = float(value)
        if self._text_opacity <= 0.0:
            self._text = DisplayText()
        self.update()

    def _animate_backdrop(self, target: float) -> None:
        self._backdrop_anim.stop()
        self._backdrop_anim.setStartValue(self._backdrop)
        self._backdrop_anim.setEndValue(target)
        self._backdrop_anim.start()

    def _on_backdrop(self, value) -> None:
        self._backdrop = float(value)
        self.update()

    # -- marquee -------------------------------------------------------------

    def _line_rects(self) -> tuple[QRectF, QRectF]:
        s = self.style_
        if s.vertical:
            # Columns: the current line on the left, the second line on its right (SPEC v1.21).
            inner = QRectF(self.rect()).adjusted(8, 16, -8, -16)
            main_w = s.main_px * 1.6
            second_w = s.main_px * s.secondary_ratio * 1.6
            left = inner.x() + max(0.0, (inner.width() - main_w - second_w) / 2)
            return (
                QRectF(left, inner.y(), main_w, inner.height()),
                QRectF(left + main_w, inner.y(), second_w, inner.height()),
            )
        inner = QRectF(self.rect()).adjusted(16, 8, -16, -8)
        main_h = s.main_px * 1.6
        second_h = s.main_px * s.secondary_ratio * 1.6
        top = inner.y() + max(0.0, (inner.height() - main_h - second_h) / 2)
        return (
            QRectF(inner.x(), top, inner.width(), main_h),
            QRectF(inner.x(), top + main_h, inner.width(), second_h),
        )

    def _restart_marquee(self) -> None:
        rects = self._line_rects()
        sizes = (self.style_.main_px, self.style_.main_px * self.style_.secondary_ratio)
        texts = (self._text.primary, self._text.secondary)
        for i in range(2):
            self._overflow[i] = self._overflow_of(texts[i], sizes[i], self._room(rects[i]))
            self._scroll[i] = 0.0
        self._marquee_elapsed = 0.0
        if any(self._overflow):
            self._marquee.start()
        else:
            self._marquee.stop()

    def _room(self, rect: QRectF) -> float:
        """Length available along the reading direction."""
        return rect.height() if self.style_.vertical else rect.width()

    def _overflow_of(self, text: str, px: float, room: float) -> float:
        if not text or room <= 0:
            return 0.0
        return max(0.0, self._layout(text, self._font(px * self.style_.min_shrink)).length - room)

    def _layout(self, text: str, font: QFont) -> LineLayout:
        key = (text, font.key(), self.style_.vertical)
        layout = self._layouts.get(key)
        if layout is None:
            layout = self._layouts[key] = LineLayout(text, font, self.style_.vertical)
            while len(self._layouts) > LAYOUT_CACHE:
                self._layouts.pop(next(iter(self._layouts)))
        return layout

    def _on_marquee(self) -> None:
        self._marquee_elapsed += self._marquee.interval() / 1000.0
        moving = self._marquee_elapsed - self.style_.marquee_pause_ms / 1000.0
        if moving <= 0:
            return
        done = True
        for i in range(2):
            if self._overflow[i] > 0:
                self._scroll[i] = min(self._overflow[i], moving * self.style_.marquee_speed)
                done = done and self._scroll[i] >= self._overflow[i]
        self.update()
        if done:
            self._marquee.stop()  # stays at the end of the line until the next one

    # -- painting ----------------------------------------------------------

    def _font(self, px: float) -> QFont:
        font = QFont()
        font.setFamilies(list(self.style_.families))
        font.setPixelSize(max(1, round(px)))
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _fitted_font(self, text: str, px: float, room: float) -> QFont:
        font = self._font(px)
        natural = self._layout(text, font).length
        if natural > room > 0:
            font = self._font(px * max(self.style_.min_shrink, room / natural))
        return font

    def _place(self, text: str, px: float, rect: QRectF, scroll: float, sung: bool = False) -> tuple[_Sprite, float]:
        """The line's sprite, and where it starts along the reading direction in `rect`."""
        vertical = self.style_.vertical
        room = self._room(rect)
        thickness = rect.width() if vertical else rect.height()
        start = rect.y() if vertical else rect.x()
        font = self._fitted_font(text, px, room)

        # Stroking the shadow and outline costs ~20 ms per line, too much for the
        # karaoke and marquee frame rate: each line is drawn once into a sprite.
        sprite = self._sprite(text, font, thickness, sung)
        length = sprite.layout.length
        if length > room:
            along = start - scroll  # marquee: start at the leading edge and slide
        elif self.style_.alignment == "left":  # "left" means top in a vertical column
            along = start
        else:
            along = start + (room - length) / 2
        return sprite, along

    def _anchor(self, rect: QRectF, along: float) -> QPointF:
        """A line's leading edge, centred across its band."""
        centre = rect.center()
        return QPointF(centre.x(), along) if self.style_.vertical else QPointF(along, centre.y())

    def _draw_line(
        self,
        painter: QPainter,
        text: str,
        px: float,
        rect: QRectF,
        opacity: float,
        scroll: float,
        fill: float = 0.0,
        transform: QTransform | None = None,
    ) -> None:
        """`transform` moves and scales the whole line, clip and karaoke edge included."""
        if not text or opacity <= 0:
            return
        vertical = self.style_.vertical
        start = rect.y() if vertical else rect.x()
        sprite, along = self._place(text, px, rect, scroll, sung=fill > 0)
        m = sprite.margin
        origin = QPointF(rect.x() - m, along - m) if vertical else QPointF(along - m, rect.y() - m)
        painter.save()
        if transform is not None:
            painter.setTransform(transform, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(opacity)
        blur = self.style_.shadow_blur
        painter.setClipRect(rect.adjusted(-2, 0, 2 + blur, blur) if vertical else rect.adjusted(0, 0, 0, blur))
        painter.drawPixmap(origin, sprite.base)
        if fill > 0:
            # Karaoke: the sung-colour sprite on top, clipped at the fill edge.
            edge = along + sprite.layout.edge(*split_point(text, fill))
            lead = min(start, along) - self.style_.outline_px
            if vertical:
                band = QRectF(rect.x() - px, lead, rect.width() + 2 * px, edge - lead)
            else:
                band = QRectF(lead, rect.y() - px, edge - lead, rect.height() + 2 * px)
            painter.setClipRect(band, Qt.ClipOperation.IntersectClip)
            painter.drawPixmap(origin, sprite.sung)
        painter.restore()

    def _sprite(self, text: str, font: QFont, thickness: float, sung: bool) -> _Sprite:
        s = self.style_
        dpr = self.devicePixelRatioF()
        key = (text, font.key(), thickness, dpr, s.vertical)
        sprite = self._sprites.get(key)
        if sprite is None:
            margin = math.ceil(s.outline_px * 2 + s.shadow_blur * 2 + max(map(abs, s.shadow_offset)) + 2)
            layout = self._layout(text, font)
            path = layout.path(margin, margin, thickness)
            length, across = math.ceil(layout.length) + 2 * margin, math.ceil(thickness) + 2 * margin
            size = QSize(across, length) if s.vertical else QSize(length, across)

            def render(paint) -> QPixmap:
                pixmap = QPixmap(round(size.width() * dpr), round(size.height() * dpr))
                pixmap.setDevicePixelRatio(dpr)
                pixmap.fill(Qt.GlobalColor.transparent)
                p = QPainter(pixmap)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                paint(p)
                p.end()
                return pixmap

            def base(p: QPainter) -> None:
                self._draw_shadow(p, path)
                pen = QPen(QColor(*s.outline_rgba), s.outline_px * 2)  # half is covered by the fill
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.strokePath(path, pen)
                p.fillPath(path, QColor(s.text_color))

            sprite = _Sprite(render(base), margin, lambda: render(lambda p: p.fillPath(path, QColor(s.sung_color))), layout)
            self._sprites[key] = sprite
            while len(self._sprites) > SPRITE_CACHE:
                self._sprites.pop(next(iter(self._sprites)))
        if sung:
            sprite.ensure_sung()
        return sprite

    def _draw_shadow(self, painter: QPainter, path: QPainterPath) -> None:
        """Soft drop shadow: stacked translucent strokes approximate the blur."""
        s = self.style_
        if s.shadow_blur <= 0 and s.shadow_offset == (0.0, 0.0):
            return
        shadow = path.translated(*s.shadow_offset)
        base = s.outline_px * 2
        for extra, alpha in ((s.shadow_blur * 2, 28), (s.shadow_blur * 1.2, 36), (s.shadow_blur * 0.5, 48)):
            pen = QPen(QColor(0, 0, 0, alpha), base + extra)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.strokePath(shadow, pen)
        painter.fillPath(shadow, QColor(0, 0, 0, 90))

    def _draw_badge(self, painter: QPainter, text: str, align: Qt.AlignmentFlag) -> None:
        painter.save()
        painter.setFont(self._font(13))
        painter.setOpacity(0.75)
        painter.setPen(QColor(self.style_.text_color))
        rect = QRectF(self.rect()).adjusted(8, 4, -8, -4)
        painter.drawText(rect, align | Qt.AlignmentFlag.AlignTop, text)
        painter.restore()

    def _second_opacity(self, text: DisplayText) -> float:
        if self.obs:
            return 1.0  # text blended into the key colour turns green and keys out unevenly
        s = self.style_
        return s.translation_opacity if text.secondary_kind == TRANSLATION else s.next_opacity

    def _paint_text(
        self, painter: QPainter, text: DisplayText, opacity: float, scroll: Sequence[float], shift: QPointF = QPointF()
    ) -> None:
        s = self.style_
        top, bottom = self._line_rects()
        self._draw_line(painter, text.primary, s.main_px, top.translated(shift), opacity, scroll[0], self.fill_of(text))
        second_px = s.main_px * s.secondary_ratio
        second_opacity = opacity * self._second_opacity(text)
        self._draw_line(painter, text.secondary, second_px, bottom.translated(shift), second_opacity, scroll[1])

    @staticmethod
    def _pushes(old: DisplayText, new: DisplayText) -> bool:
        """The new current line is the next line already on screen below it."""
        return old.secondary_kind == NEXT and bool(old.secondary) and old.secondary == new.primary

    def _paint_change(self, painter: QPainter, opacity: float) -> None:
        """SPEC v1.81: the next line moves up into place, growing to full size, while the
        old line leaves upwards and the new next line comes in below. Any other change
        (a translation below, a skipped line) slides the pair up a little as it crossfades."""
        s = self.style_
        old, new = self._prev_text, self._text
        p = _EASE.valueForProgress(self._change_t)
        top, bottom = self._line_rects()
        step = top.center() - bottom.center()  # from the second line's place to the first's
        if not self._pushes(old, new):
            drift = step * DRIFT
            # The old pair is mostly gone before the new one shows, so they barely overlap.
            self._paint_text(painter, old, opacity * (1 - p) ** 3, self._prev_scroll, drift * p)
            self._paint_text(painter, new, opacity * p * p, self._scroll, drift * (p - 1))
            return

        second_px = s.main_px * s.secondary_ratio
        # The old line moves up with the line below it, so the two never overlap.
        leaving = opacity * (1 - p) ** 3
        gone = top.translated(step * p)
        self._draw_line(painter, old.primary, s.main_px, gone, leaving, self._prev_scroll[0], self.fill_of(old))

        # The line moving up is drawn at its final size and place, scaled and moved back
        # to where it stood as the next line: its leading edge travels, so a centred line
        # stays centred and a left-aligned one keeps its left edge.
        moving = new.primary
        small, along_from = self._place(moving, second_px, bottom, self._prev_scroll[1])
        full, along_to = self._place(moving, s.main_px, top, self._scroll[0])
        start, end = self._anchor(bottom, along_from), self._anchor(top, along_to)
        here = start + (end - start) * p
        size = small.layout.font.pixelSize() / full.layout.font.pixelSize()
        scale = size + (1 - size) * p
        transform = QTransform().translate(here.x(), here.y()).scale(scale, scale).translate(-end.x(), -end.y())
        was = self._second_opacity(old)
        moving_opacity = opacity * (was + (1 - was) * p)
        self._draw_line(painter, moving, s.main_px, top, moving_opacity, self._scroll[0], self.fill_of(new), transform)

        # Only a short rise: the window ends just below the second line.
        arriving = opacity * self._second_opacity(new) * p**3
        below = bottom.translated(step * (RISE * (p - 1)))
        self._draw_line(painter, new.secondary, second_px, below, arriving, self._scroll[1])

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        if self.obs:
            painter.fillRect(self.rect(), QColor(self.style_.obs_background))
        elif self._backdrop > 0:
            alpha = round(255 * self.style_.hover_backdrop_opacity * self._backdrop)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)

        k = self._text_opacity * self._idle_opacity
        if self._change_t < 1.0:
            self._paint_change(painter, k)
        else:
            self._paint_text(painter, self._text, k, self._scroll)
        if self.obs:
            pass  # stream output: lyrics only, no badges
        elif self._unsynced and self._lines and k > 0:
            self._draw_badge(painter, UNSYNCED_BADGE, Qt.AlignmentFlag.AlignLeft)
        elif self._cue_hint and self._lines and k > 0:
            self._draw_badge(painter, self._cue_hint, Qt.AlignmentFlag.AlignLeft)
        badge = None if self.obs else self._flash or self._status_badge
        if badge and not self.toolbar.isVisible():
            self._draw_badge(painter, badge, Qt.AlignmentFlag.AlignRight)
        painter.end()
