"""FR-26 theme import and export."""

import json
from dataclasses import replace

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat import theme
from lyricfloat.settings import Settings
from lyricfloat.ui import settings_dialog as dialog_module
from lyricfloat.ui.settings_dialog import SettingsDialog

FONTS = {"Segoe UI", "Microsoft JhengHei", "Noto Sans TC"}


def test_round_trip_carries_only_the_appearance(tmp_path):
    styled = replace(Settings(), font_px=44, text_color="#FFEE00", alignment="left", karaoke=False, window_rect=[1, 2, 3, 4])
    path = tmp_path / "t.json"
    theme.export_theme(styled, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == theme.FORMAT and set(data["appearance"]) == set(theme.FIELDS)
    other = replace(Settings(), window_rect=[9, 9, 9, 9], hotkeys={"toggle_visible": ""})
    got, notes = theme.import_theme(other, path, FONTS)
    assert notes == []
    assert (got.font_px, got.text_color, got.alignment, got.karaoke) == (44, "#FFEE00", "left", False)
    assert got.window_rect == [9, 9, 9, 9] and got.hotkeys == {"toggle_visible": ""}  # untouched


def test_missing_font_falls_back_with_a_note(tmp_path):
    path = tmp_path / "t.json"
    theme.export_theme(replace(Settings(), font_cjk="Some Fancy Font"), path)
    got, notes = theme.import_theme(Settings(), path, FONTS)
    assert got.font_cjk == Settings().font_cjk
    assert len(notes) == 1 and "Some Fancy Font" in notes[0]


def test_invalid_values_are_adjusted_with_notes(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"format": theme.FORMAT, "version": 1, "appearance": {
        "font_px": 999, "text_color": "red", "alignment": "diagonal", "outline_px": 2, "unknown": 1}}), encoding="utf-8")
    got, notes = theme.import_theme(replace(Settings(), alignment="left"), path, FONTS)
    assert got.text_color == Settings().text_color and got.font_px < 999 and got.outline_px == 2.0
    assert len(notes) == 3  # font size, colour, alignment; 2 -> 2.0 is not a change


@pytest.mark.parametrize("content", ["not json", json.dumps({"format": "other"}), json.dumps([1, 2])])
def test_foreign_files_are_refused(tmp_path, content):
    path = tmp_path / "t.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(theme.ThemeError):
        theme.import_theme(Settings(), path)


# -- dialog ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_import_previews_and_export_writes(app, tmp_path, monkeypatch):
    source = tmp_path / "in.json"
    theme.export_theme(replace(Settings(), font_px=50, sung_color="#FF00AA", obs_background="#0000FF"), source)
    target = tmp_path / "out.json"
    monkeypatch.setattr(dialog_module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), ""))
    monkeypatch.setattr(dialog_module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    shown = []
    monkeypatch.setattr(dialog_module.QMessageBox, "information", lambda *a: shown.append(a))
    d = SettingsDialog(Settings(), tmp_path / "v.txt")
    previews = []
    d.previewChanged.connect(previews.append)
    d._import_theme()
    assert len(previews) == 1  # one preview for the whole theme
    got = d.current()
    assert (got.font_px, got.sung_color, got.obs_background) == (50, "#FF00AA", "#0000FF")
    d._export_theme()
    assert json.loads(target.read_text(encoding="utf-8"))["appearance"]["font_px"] == 50
    d.close()


def test_dialog_reports_a_bad_file(app, tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dialog_module.QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), ""))
    warned = []
    monkeypatch.setattr(dialog_module.QMessageBox, "warning", lambda *a: warned.append(a[2]))
    d = SettingsDialog(Settings(), tmp_path / "v.txt")
    d._import_theme()
    assert warned == ["這不是 LyricFloat 的主題檔。"]
    d.close()
