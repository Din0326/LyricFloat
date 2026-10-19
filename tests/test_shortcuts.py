"""FR-14 shortcut text handling and the Qt key mapping."""

import pytest
from PyQt6.QtCore import QKeyCombination, Qt

from lyricfloat import shortcuts as sc
from lyricfloat.settings import settings_from_dict
from lyricfloat.ui.hotkey import GlobalHotkey, win32_from_qt

CTRL_ALT = sc.MOD_CONTROL | sc.MOD_ALT


@pytest.mark.parametrize(
    "text, parsed",
    [
        ("Ctrl+Alt+H", (CTRL_ALT, ord("H"))),
        ("ctrl + alt + left", (CTRL_ALT, 0x25)),
        ("Win+Shift+F12", (sc.MOD_WIN | sc.MOD_SHIFT, 0x7B)),
        ("Alt+PgDown", (sc.MOD_ALT, 0x22)),
        ("H", None),  # no modifier
        ("Shift+H", None),  # Shift alone would capture typing
        ("Ctrl+Ctrl+H", None),
        ("Ctrl+Alt+Esc", None),  # unsupported key
        ("Hyper+H", None),
    ],
)
def test_parse(text, parsed):
    assert sc.parse(text) == parsed


def test_normalize_gives_canonical_text():
    assert sc.normalize("alt+ctrl+right") == "Ctrl+Alt+Right"
    assert sc.normalize("  ") == ""
    assert sc.normalize("Q") is None


def test_defaults_avoid_the_calibration_key_and_each_other():
    assert sc.problems(sc.DEFAULTS) == []
    assert all(sc.parse(text) for text in sc.DEFAULTS.values())


def test_problems_report_duplicates_and_the_calibration_key():
    found = sc.problems({"toggle_visible": "Ctrl+Alt+H", "toggle_lock": "Ctrl+Alt+H", "offset_later": sc.CALIBRATION, "offset_earlier": ""})
    assert len(found) == 2
    assert "顯示／隱藏歌詞" in found[0] and "鎖定／解鎖歌詞" in found[0]
    assert "校對模式" in found[1]


def test_label_uses_the_shared_spelling():
    assert GlobalHotkey(0x27, CTRL_ALT, 1).label() == "Ctrl+Alt+Right"
    assert GlobalHotkey().label() == sc.CALIBRATION


@pytest.mark.parametrize(
    "mods, key, parsed",
    [
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier, Qt.Key.Key_Left, (CTRL_ALT, 0x25)),
        (Qt.KeyboardModifier.MetaModifier, Qt.Key.Key_F5, (sc.MOD_WIN, 0x74)),
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, Qt.Key.Key_7, (sc.MOD_CONTROL | sc.MOD_SHIFT, ord("7"))),
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier, Qt.Key.Key_Up, (sc.MOD_CONTROL, 0x26)),
        (Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Escape, None),
    ],
)
def test_qt_key_to_win32(mods, key, parsed):
    assert win32_from_qt(QKeyCombination(mods, key)) == parsed


def test_settings_keep_valid_shortcuts_and_reset_bad_ones():
    got = settings_from_dict({"hotkeys": {"toggle_lock": "ctrl+shift+l", "toggle_visible": "H", "offset_later": "", "bogus": "Ctrl+Alt+B"}})
    assert got.hotkeys == {
        "toggle_visible": "Ctrl+Alt+H",  # invalid -> default
        "toggle_lock": "Ctrl+Shift+L",
        "offset_earlier": "Ctrl+Alt+Left",
        "offset_later": "",  # explicitly turned off
        "cue_line": "Ctrl+Alt+Down",  # added in a later version: its default
    }
    assert settings_from_dict({"hotkeys": "nope"}).hotkeys == sc.DEFAULTS
