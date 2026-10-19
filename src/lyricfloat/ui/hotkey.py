"""A process-wide hotkey via Win32 RegisterHotKey (calibration mode, FR-14 shortcuts).

Registered for the GUI thread (hwnd NULL) rather than a window, because
locking the lyric window recreates its native handle. Only held while the
feature is on; nothing persistent (no registry, no system setting).
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QKeyCombination, QObject, Qt, pyqtSignal

from lyricfloat.shortcuts import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, format_shortcut

log = logging.getLogger(__name__)

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, owner: "GlobalHotkey") -> None:
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.owner.hotkey_id:
                self.owner.activated.emit()
                return True, 0
        return False, 0


class GlobalHotkey(QObject):
    activated = pyqtSignal()

    def __init__(
        self,
        vk: int = ord("M"),
        modifiers: int = MOD_CONTROL | MOD_ALT,
        hotkey_id: int = 0xB10C,
        repeat: bool = False,  # keep firing while held (offset steps)
    ) -> None:
        super().__init__()
        self.vk = vk
        self.modifiers = modifiers
        self.hotkey_id = hotkey_id
        self.repeat = repeat
        self._filter: _Filter | None = None
        self.registered = False

    def label(self) -> str:
        return format_shortcut(self.modifiers, self.vk)

    def register(self) -> bool:
        if self.registered:
            return True
        flags = self.modifiers if self.repeat else self.modifiers | MOD_NOREPEAT
        ok = bool(ctypes.windll.user32.RegisterHotKey(None, self.hotkey_id, flags, self.vk))
        if not ok:
            log.warning("hotkey %s unavailable (used by another program?)", self.label())
            return False
        self._filter = _Filter(self)
        QCoreApplication.instance().installNativeEventFilter(self._filter)
        self.registered = True
        log.info("hotkey %s registered", self.label())
        return True

    def unregister(self) -> None:
        if not self.registered:
            return
        ctypes.windll.user32.UnregisterHotKey(None, self.hotkey_id)
        if self._filter is not None:
            QCoreApplication.instance().removeNativeEventFilter(self._filter)
            self._filter = None
        self.registered = False
        log.info("hotkey %s released", self.label())


_QT_MODIFIERS = (
    (Qt.KeyboardModifier.ControlModifier, MOD_CONTROL),
    (Qt.KeyboardModifier.AltModifier, MOD_ALT),
    (Qt.KeyboardModifier.ShiftModifier, MOD_SHIFT),
    (Qt.KeyboardModifier.MetaModifier, MOD_WIN),
)
_QT_KEYS = {
    Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_End: 0x23,
    Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27,
    Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_Insert: 0x2D,
    Qt.Key.Key_Delete: 0x2E,
}


def win32_from_qt(combo: QKeyCombination) -> tuple[int, int] | None:
    """A key pressed in a QKeySequenceEdit -> (modifiers, virtual key), None if unsupported."""
    key = combo.key()
    if Qt.Key.Key_0.value <= key.value <= Qt.Key.Key_9.value or Qt.Key.Key_A.value <= key.value <= Qt.Key.Key_Z.value:
        vk = key.value  # Qt and Win32 share ASCII codes for digits and letters
    elif Qt.Key.Key_F1.value <= key.value <= Qt.Key.Key_F24.value:
        vk = 0x70 + key.value - Qt.Key.Key_F1.value
    else:
        vk = _QT_KEYS.get(key)
    if vk is None:
        return None
    mods = combo.keyboardModifiers()
    return sum(bit for flag, bit in _QT_MODIFIERS if mods & flag), vk
