"""System tray icon: show/hide, lock, quit; warning dot when every source failed; the app's own
CPU and memory at the top of the menu (FR-27)."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from lyricfloat import APP_NAME
from lyricfloat.diagnostics.usage import UsageMeter
from lyricfloat.models import TrackInfo
from lyricfloat.ui.lyric_window import LyricWindow


def make_icon_pixmap(warning: bool = False, size: int = 64) -> QPixmap:
    """The ♪ app icon; also rendered at 256 px for the packaged executable."""
    k = size / 64  # drawn on a 64 px grid, scaled to the requested size
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#4FC3F7"))
    p.drawRoundedRect(QRectF(4 * k, 4 * k, size - 8 * k, size - 8 * k), 14 * k, 14 * k)
    font = QFont("Segoe UI Symbol")
    font.setPixelSize(round(40 * k))
    p.setFont(font)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(QRectF(0, 0, size, size - 2 * k), Qt.AlignmentFlag.AlignCenter, "♪")
    if warning:
        p.setPen(QColor("#FFFFFF"))
        p.setBrush(QColor("#E53935"))
        p.drawEllipse(QRectF(size - 26 * k, 2 * k, 24 * k, 24 * k))
    p.end()
    return pixmap


def player_label(app_id: str) -> str:
    """Readable player name from an SMTC app id ("Spotify.exe", "...!Spotify")."""
    name = app_id.rsplit("!", 1)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


USAGE_IDLE_MS = 5000  # menu closed: keep a recent reading to show the moment it opens
USAGE_OPEN_MS = 1000  # menu open: refresh every second


def _make_icon(warning: bool) -> QIcon:
    return QIcon(make_icon_pixmap(warning))


class TrayController(QObject):
    settingsRequested = pyqtSignal()
    searchRequested = pyqtSignal()
    editRequested = pyqtSignal()  # FR-17
    reportRequested = pyqtSignal()
    reportsFolderRequested = pyqtSignal()
    calibrationToggled = pyqtSignal(bool)
    visibilityChanged = pyqtSignal(bool)
    lockChanged = pyqtSignal(bool)
    obsToggled = pyqtSignal(bool)  # FR-18
    pinChanged = pyqtSignal(str)  # FR-24: app id, "" = follow Windows

    def __init__(
        self,
        window: LyricWindow,
        on_quit: Callable[[], None],
        parent: QObject | None = None,
        meter: UsageMeter | None = None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self._icons = {False: _make_icon(False), True: _make_icon(True)}
        self._warning = False
        self._session = ""
        self._track: TrackInfo | None = None
        self._pinned = ""
        self._players: Callable[[], list[str]] = list

        self.menu = QMenu()
        self.meter = meter or UsageMeter()
        self.usage = self.meter.read()  # baseline: CPU needs two readings
        self.usage_action = QAction(self.usage.label(), self.menu)
        self.usage_action.setEnabled(False)  # information only
        self._usage_timer = QTimer(self, interval=USAGE_IDLE_MS)
        self._usage_timer.timeout.connect(self._read_usage)
        self._usage_timer.start()
        self.menu.aboutToShow.connect(lambda: self._usage_timer.setInterval(USAGE_OPEN_MS))
        self.menu.aboutToHide.connect(lambda: self._usage_timer.setInterval(USAGE_IDLE_MS))
        self.show_action = QAction("顯示歌詞", self.menu, checkable=True, checked=True)
        self.show_action.toggled.connect(self._set_visible)
        self.lock_action = QAction("鎖定歌詞（滑鼠穿透）", self.menu, checkable=True, checked=window.locked)
        self.lock_action.toggled.connect(self._set_locked)
        search_action = QAction("搜尋歌詞／換版本…", self.menu)
        search_action.triggered.connect(self.searchRequested)
        edit_action = QAction("編輯這首歌詞…", self.menu)
        edit_action.triggered.connect(self.editRequested)
        report_action = QAction("這首歌詞有誤…", self.menu)
        report_action.triggered.connect(self.reportRequested)
        reports_folder_action = QAction("開啟診斷資料夾", self.menu)
        reports_folder_action.triggered.connect(self.reportsFolderRequested)
        self.calibration_action = QAction("校對模式（量時間軸誤差）", self.menu, checkable=True)
        self.calibration_action.toggled.connect(self.calibrationToggled)
        settings_action = QAction("設定…", self.menu)
        settings_action.triggered.connect(self.settingsRequested)
        quit_action = QAction("結束", self.menu)
        quit_action.triggered.connect(on_quit)
        self.menu.addAction(self.usage_action)
        self.menu.addSeparator()
        self.menu.addAction(self.show_action)
        self.menu.addAction(self.lock_action)
        self.obs_action = QAction("OBS 視窗（純色背景）", self.menu, checkable=True)
        self.obs_action.setToolTip("另開一個可被 OBS 視窗擷取的歌詞視窗")
        self.obs_action.toggled.connect(self.obsToggled)
        self.menu.addAction(self.obs_action)
        self.player_menu = self.menu.addMenu("跟隨播放器")
        self.player_menu.aboutToShow.connect(self._fill_player_menu)
        self.menu.addSeparator()
        self.menu.addAction(search_action)
        self.menu.addAction(edit_action)
        self.menu.addAction(report_action)
        self.menu.addAction(reports_folder_action)
        self.menu.addAction(self.calibration_action)
        self.menu.addAction(settings_action)
        self.menu.addSeparator()
        self.menu.addAction(quit_action)

        self.tray = QSystemTrayIcon(self._icons[False], self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self._refresh_tooltip()
        self.tray.show()

    def _set_visible(self, visible: bool) -> None:
        self.window.setVisible(visible)
        self.visibilityChanged.emit(visible)

    def _set_locked(self, locked: bool) -> None:
        self.window.set_locked(locked)
        self.lockChanged.emit(locked)

    def _read_usage(self) -> None:
        self.usage = self.meter.read()
        self.usage_action.setText(self.usage.label())

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_action.toggle()

    def notify(self, title: str, text: str) -> None:
        """Balloon from the tray icon: feedback without a pop-up window."""
        self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 5000)

    def set_track(self, track: TrackInfo | None) -> None:
        self._track = track
        self._refresh_tooltip()

    def set_session(self, app_id: str) -> None:
        self._session = app_id
        self._refresh_tooltip()

    def set_players_provider(self, provider: Callable[[], list[str]]) -> None:
        self._players = provider

    def set_pinned(self, app_id: str) -> None:
        self._pinned = app_id

    def _fill_player_menu(self) -> None:
        """Rebuilt on every open: players come and go."""
        self.player_menu.clear()
        group = QActionGroup(self.player_menu)
        players = self._players()
        if self._pinned and self._pinned not in players:
            players = [*players, self._pinned]
        choices = [("", "自動（跟隨 Windows 目前播放）")]
        for app_id in players:
            label = player_label(app_id)
            if app_id == self._pinned and app_id not in self._players():
                label += "（未開啟）"
            choices.append((app_id, label))
        for app_id, label in choices:
            action = QAction(label, group, checkable=True, checked=app_id == self._pinned)
            action.triggered.connect(lambda _=False, a=app_id: self._pick(a))
            self.player_menu.addAction(action)

    def _pick(self, app_id: str) -> None:
        if app_id != self._pinned:
            self._pinned = app_id
            self.pinChanged.emit(app_id)

    def set_warning(self, warning: bool) -> None:
        if warning != self._warning:
            self._warning = warning
            self.tray.setIcon(self._icons[warning])
            self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        parts = [APP_NAME]
        if self._track:
            parts.append(self._track.describe())
        if self._session:
            parts.append(f"來源：{self._session}")
        if self._warning:
            parts.append("⚠ 歌詞來源全部無法連線")
        self.tray.setToolTip("\n".join(parts)[:127])  # Windows tooltip limit

    def hide(self) -> None:
        self.tray.hide()
