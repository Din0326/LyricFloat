import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.settings import Settings, SourceSetting
from lyricfloat.ui.settings_dialog import SettingsDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app, tmp_path):
    d = SettingsDialog(Settings(), tmp_path / "vocabulary.txt")
    yield d
    d.close()


def test_dialog_starts_from_the_given_settings(app, tmp_path):
    s = Settings(font_px=40, conversion_mode="s2tw", alignment="left", show_translation=False, autostart=True)
    s.sources = [SourceSetting("lrclib"), SourceSetting("netease", enabled=False)]
    d = SettingsDialog(s, tmp_path / "v.txt")
    got = d.current()
    assert got.font_px == 40
    assert got.conversion_mode == "s2tw"
    assert got.alignment == "left"
    assert not got.show_translation
    assert got.autostart
    assert got.enabled_sources() == ["lrclib"]
    d.close()


def test_appearance_changes_are_previewed_live(dialog):
    seen = []
    dialog.previewChanged.connect(seen.append)
    dialog.font_px.setValue(44)
    dialog.alignment.setCurrentIndex(dialog.alignment.findData("left"))
    dialog.outline_opacity.setValue(50)
    assert seen[-1].font_px == 44
    assert seen[-1].alignment == "left"
    assert seen[-1].outline_opacity == pytest.approx(0.5)


def test_general_and_sources_are_read_on_ok(dialog):
    dialog.mode_tw.setChecked(True)
    dialog.source_boxes["netease"].setChecked(False)
    dialog.autostart.setChecked(True)
    got = dialog.current()
    assert got.conversion_mode == "s2tw"
    assert got.enabled_sources() == ["lrclib"]
    assert got.autostart


def test_source_order_is_kept(dialog):
    assert [s.name for s in dialog.current().sources] == ["lrclib", "netease"]


def test_color_buttons_normalise_values(dialog):
    dialog.text_color.set_color("#ffee00")
    assert dialog.current().text_color == "#FFEE00"


def test_shortcuts_tab_shows_the_defaults(dialog):
    assert dialog.current().hotkeys == Settings().hotkeys
    assert dialog.ok_button.isEnabled() and dialog.hotkey_problems.isHidden()


def test_duplicate_shortcut_blocks_ok(dialog):
    dialog.hotkey_edits["toggle_lock"].set_text("Ctrl+Alt+H")
    assert not dialog.ok_button.isEnabled()
    assert "Ctrl+Alt+H" in dialog.hotkey_problems.text()
    dialog.hotkey_edits["toggle_lock"].set_text("Ctrl+Shift+L")
    assert dialog.ok_button.isEnabled()
    assert dialog.current().hotkeys["toggle_lock"] == "Ctrl+Shift+L"


def test_shortcut_without_modifier_blocks_ok(dialog):
    dialog.hotkey_edits["toggle_visible"].set_text("H")
    assert dialog.hotkey_edits["toggle_visible"].value() is None
    assert not dialog.ok_button.isEnabled()


def test_cleared_shortcut_is_off_and_defaults_come_back(dialog):
    dialog.hotkey_edits["offset_later"].clear()
    dialog.hotkey_edits["toggle_visible"].set_text("Win+F9")
    got = dialog.current().hotkeys
    assert got["offset_later"] == "" and got["toggle_visible"] == "Win+F9"
    dialog._default_hotkeys()
    assert dialog.current().hotkeys == Settings().hotkeys


def test_orientation_choice(dialog):
    seen = []
    dialog.previewChanged.connect(seen.append)
    dialog.orientation.setCurrentIndex(1)
    assert dialog.current().vertical and seen[-1].vertical
