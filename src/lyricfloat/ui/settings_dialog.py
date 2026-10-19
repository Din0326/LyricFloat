"""Settings dialog: general, appearance, lyric sources, shortcuts.

Appearance changes preview live on the lyric window; Cancel restores the
settings the dialog opened with. Changes with side effects (conversion
mode, sources, autostart, shortcuts) take effect on OK only.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lyricfloat.lyrics.chinese import ensure_vocabulary_file
from lyricfloat import shortcuts, theme
from lyricfloat.settings import FONT_PX_RANGE, Settings, SourceSetting
from lyricfloat.ui.hotkey import win32_from_qt

THEME_FILTER = "LyricFloat 主題 (*.json)"
L3_CONSENT = (
    "開啟後：\n\n"
    "・播放有同步歌詞的歌時，只錄下播放器這個程式的聲音（不含其他程式與麥克風），"
    "暫存在本機，校正完成或放棄時立即刪除，不會上傳。\n"
    "・歌曲播完後在背景以最低優先權處理，每首約 1 分鐘；處理時會暫時使用約 1.5–2 GB 記憶體，"
    "可用記憶體不足時自動延後。\n"
    "・第一次處理前會下載人聲分離與辨識模型（約 220 MB）；免安裝版另外會下載執行環境"
    "（約 250 MB，安裝後約 1 GB），全部只從官方來源下載並核對檢查碼。\n"
    "・中途暫停、拖動進度或沒聽完的歌不會校正，下次完整播放時再試。\n\n"
    "要開啟嗎？"
)
SOURCE_LABELS = {"lrclib": "LRCLIB（開放歌詞資料庫）", "netease": "網易雲音樂（華語備援、英文歌翻譯）"}


class ColorButton(QPushButton):
    changed = pyqtSignal(str)

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(80)
        self.clicked.connect(self._pick)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.color = QColor(color).name().upper()
        self.setText(self.color)
        text = "#000000" if QColor(self.color).lightness() > 128 else "#FFFFFF"
        self.setStyleSheet(f"background: {self.color}; color: {text};")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.color), self, "選擇顏色")
        if chosen.isValid():
            self.set_color(chosen.name())
            self.changed.emit(self.color)


class HotkeyEdit(QKeySequenceEdit):
    """One shortcut: click, press the combination; the clear button removes it."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumSequenceLength(1)
        self.setClearButtonEnabled(True)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        qt_text = "+".join("Meta" if part == "Win" else part for part in text.split("+")) if text else ""
        self.setKeySequence(QKeySequence.fromString(qt_text, QKeySequence.SequenceFormat.PortableText))

    def value(self) -> str | None:
        """The shortcut as stored in settings; "" for none, None if it cannot be used."""
        seq = self.keySequence()
        if seq.isEmpty():
            return ""
        parsed = win32_from_qt(seq[0])
        return shortcuts.normalize(shortcuts.format_shortcut(*parsed)) if parsed else None


class SettingsDialog(QDialog):
    previewChanged = pyqtSignal(object)  # Settings, for live preview of appearance

    def __init__(self, settings: Settings, vocabulary_path: Path, parent: QWidget | None = None, l3_status: str = "") -> None:
        """`l3_status`: whether the alignment runtime is ready, shown under the L3 switch."""
        super().__init__(parent)
        self.l3_status_text = l3_status
        self.setWindowTitle("LyricFloat 設定")
        self.setMinimumWidth(460)
        self.original = settings
        self.vocabulary_path = vocabulary_path
        self._building = True

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(settings), "一般")
        tabs.addTab(self._appearance_tab(settings), "外觀")
        tabs.addTab(self._sources_tab(settings), "歌詞來源")
        tabs.addTab(self._hotkeys_tab(settings), "快捷鍵")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("確定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self._building = False
        self._check_hotkeys()

    # -- tabs ----------------------------------------------------------------

    def _general_tab(self, s: Settings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.autostart = QCheckBox("Windows 登入時自動啟動")
        self.autostart.setChecked(s.autostart)
        form.addRow(self.autostart)

        self.mode_twp = QRadioButton("轉成台灣正體並替換慣用詞（例：软件 → 軟體）")
        self.mode_tw = QRadioButton("只轉字、不換詞（例：软件 → 軟件）")
        (self.mode_tw if s.conversion_mode == "s2tw" else self.mode_twp).setChecked(True)
        form.addRow(QLabel("簡體歌詞轉換方式"))
        form.addRow(self.mode_twp)
        form.addRow(self.mode_tw)

        vocab = QPushButton("編輯自訂詞彙表…")
        vocab.setToolTip("修正轉換錯誤，例如歌手名或專有名詞；按確定後生效")
        vocab.clicked.connect(self._open_vocabulary)
        form.addRow(vocab)

        form.addRow(QLabel("自動時間軸校正"))
        self.l3_enabled = QCheckBox("歌曲播完後依實際音訊校正時間軸，並產生逐字卡拉 OK")
        self.l3_enabled.setChecked(s.l3_enabled)
        self.l3_enabled.toggled.connect(self._confirm_l3)
        form.addRow(self.l3_enabled)
        self.l3_status = QLabel(self.l3_status_text)
        self.l3_status.setWordWrap(True)
        form.addRow(self.l3_status)
        return page

    def _confirm_l3(self, on: bool) -> None:
        """Turning L3 on records the player's audio: say what happens and ask first (SPEC v1.15)."""
        if not on or self._building:
            return
        answer = QMessageBox.question(self, "自動時間軸校正", L3_CONSENT)
        if answer != QMessageBox.StandardButton.Yes:
            self.l3_enabled.setChecked(False)

    def _appearance_tab(self, s: Settings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.font_latin = QFontComboBox()
        self.font_latin.setCurrentFont(QFont(s.font_latin))
        self.font_cjk = QFontComboBox()
        self.font_cjk.setWritingSystem(QFontDatabase.WritingSystem.TraditionalChinese)
        self.font_cjk.setCurrentFont(QFont(s.font_cjk))
        self.font_px = QSpinBox()
        self.font_px.setRange(*FONT_PX_RANGE)
        self.font_px.setSuffix(" px")
        self.font_px.setValue(s.font_px)
        form.addRow("西文字型", self.font_latin)
        form.addRow("中文字型", self.font_cjk)
        form.addRow("字級", self.font_px)

        self.text_color = ColorButton(s.text_color)
        form.addRow("文字顏色", self.text_color)

        self.outline_px = QDoubleSpinBox()
        self.outline_px.setRange(0.0, 8.0)
        self.outline_px.setSingleStep(0.5)
        self.outline_px.setSuffix(" px")
        self.outline_px.setValue(s.outline_px)
        self.outline_color = ColorButton(s.outline_color)
        self.outline_opacity = QSpinBox()
        self.outline_opacity.setRange(0, 100)
        self.outline_opacity.setSuffix(" %")
        self.outline_opacity.setValue(round(s.outline_opacity * 100))
        form.addRow("描邊粗細", self.outline_px)
        form.addRow("描邊顏色", self.outline_color)
        form.addRow("描邊不透明度", self.outline_opacity)

        self.shadow_blur = QDoubleSpinBox()
        self.shadow_blur.setRange(0.0, 20.0)
        self.shadow_blur.setSuffix(" px")
        self.shadow_blur.setValue(s.shadow_blur)
        self.shadow_offset = QDoubleSpinBox()
        self.shadow_offset.setRange(0.0, 10.0)
        self.shadow_offset.setSuffix(" px")
        self.shadow_offset.setValue(s.shadow_offset_y)
        form.addRow("陰影模糊", self.shadow_blur)
        form.addRow("陰影向下偏移", self.shadow_offset)

        self.alignment = QComboBox()
        self.alignment.addItem("置中", "center")
        self.alignment.addItem("靠左（直向時靠上）", "left")
        self.alignment.setCurrentIndex(max(0, self.alignment.findData(s.alignment)))
        form.addRow("對齊方式", self.alignment)

        self.orientation = QComboBox()
        self.orientation.addItem("橫向（兩行）", False)
        self.orientation.addItem("直向（兩欄，目前句在左）", True)
        self.orientation.setCurrentIndex(1 if s.vertical else 0)
        form.addRow("排列方向", self.orientation)
        self.orientation.currentIndexChanged.connect(self._preview)

        self.line_animation = QCheckBox("換句動畫")
        self.line_animation.setChecked(s.line_animation)
        self.show_translation = QCheckBox("英文歌顯示繁中翻譯")
        self.show_translation.setChecked(s.show_translation)
        form.addRow(self.line_animation)
        form.addRow(self.show_translation)

        self.karaoke = QCheckBox("卡拉 OK 填色（依每句時長推估進度）")
        self.karaoke.setChecked(s.karaoke)
        self.sung_color = ColorButton(s.sung_color)
        form.addRow(self.karaoke)
        form.addRow("已唱顏色", self.sung_color)
        self.karaoke.toggled.connect(self._preview)
        self.sung_color.changed.connect(self._preview)

        self.obs_background = ColorButton(s.obs_background)
        self.obs_background.setToolTip("系統列開啟「OBS 視窗」時的背景色，在 OBS 用色度去背去掉")
        form.addRow("OBS 視窗背景", self.obs_background)
        self.obs_background.changed.connect(self._preview)

        for widget in (self.font_latin, self.font_cjk):
            widget.currentFontChanged.connect(self._preview)
        for widget in (self.font_px, self.outline_opacity):
            widget.valueChanged.connect(self._preview)
        for widget in (self.outline_px, self.shadow_blur, self.shadow_offset):
            widget.valueChanged.connect(self._preview)
        for widget in (self.text_color, self.outline_color):
            widget.changed.connect(self._preview)
        self.alignment.currentIndexChanged.connect(self._preview)
        self.line_animation.toggled.connect(self._preview)
        self.show_translation.toggled.connect(self._preview)

        row = QHBoxLayout()
        import_button = QPushButton("匯入主題…")
        import_button.setToolTip("從主題檔套用外觀設定；按確定後才儲存，按取消還原")
        import_button.clicked.connect(self._import_theme)
        export_button = QPushButton("匯出主題…")
        export_button.setToolTip("把這一頁的外觀設定存成主題檔（不含視窗位置、快捷鍵等其他設定）")
        export_button.clicked.connect(self._export_theme)
        row.addWidget(import_button)
        row.addWidget(export_button)
        row.addStretch(1)
        form.addRow(row)
        return page

    def _set_appearance(self, s: Settings) -> None:
        """Show `s`'s appearance in the tab (one preview at the end, not one per field)."""
        self._building = True
        try:
            self.font_latin.setCurrentFont(QFont(s.font_latin))
            self.font_cjk.setCurrentFont(QFont(s.font_cjk))
            self.font_px.setValue(s.font_px)
            self.text_color.set_color(s.text_color)
            self.outline_px.setValue(s.outline_px)
            self.outline_color.set_color(s.outline_color)
            self.outline_opacity.setValue(round(s.outline_opacity * 100))
            self.shadow_blur.setValue(s.shadow_blur)
            self.shadow_offset.setValue(s.shadow_offset_y)
            self.alignment.setCurrentIndex(max(0, self.alignment.findData(s.alignment)))
            self.orientation.setCurrentIndex(1 if s.vertical else 0)
            self.line_animation.setChecked(s.line_animation)
            self.show_translation.setChecked(s.show_translation)
            self.karaoke.setChecked(s.karaoke)
            self.sung_color.set_color(s.sung_color)
            self.obs_background.set_color(s.obs_background)
        finally:
            self._building = False
        self._preview()

    def _import_theme(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入主題", str(Path.home()), THEME_FILTER)
        if not path:
            return
        try:
            imported, notes = theme.import_theme(self.current(), Path(path), QFontDatabase.families())
        except theme.ThemeError as exc:
            QMessageBox.warning(self, "匯入主題", str(exc))
            return
        self._set_appearance(imported)
        if notes:
            QMessageBox.information(self, "匯入主題", "已套用，但有幾項調整：\n" + "\n".join(notes))

    def _export_theme(self) -> None:
        suggested = Path.home() / "Documents" / "LyricFloat 主題.json"
        path, _ = QFileDialog.getSaveFileName(self, "匯出主題", str(suggested), THEME_FILTER)
        if not path:
            return
        try:
            theme.export_theme(self.current(), Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "匯出主題", f"無法寫入：{exc}")

    def _sources_tab(self, s: Settings) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.source_boxes: dict[str, QCheckBox] = {}
        for source in s.sources:
            box = QCheckBox(SOURCE_LABELS.get(source.name, source.name))
            box.setChecked(source.enabled)
            self.source_boxes[source.name] = box
            layout.addWidget(box)
        note = QLabel("查詢順序依歌曲語言自動決定。停用網易雲後，英文歌將沒有翻譯。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _hotkeys_tab(self, s: Settings) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.hotkey_edits: dict[str, HotkeyEdit] = {}
        for action, label in shortcuts.ACTIONS.items():
            edit = HotkeyEdit(s.hotkeys.get(action, ""))
            edit.keySequenceChanged.connect(self._check_hotkeys)
            self.hotkey_edits[action] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        row = QHBoxLayout()
        defaults = QPushButton("還原預設")
        defaults.clicked.connect(self._default_hotkeys)
        row.addWidget(defaults)
        row.addStretch(1)
        layout.addLayout(row)
        self.hotkey_problems = QLabel()
        self.hotkey_problems.setWordWrap(True)
        self.hotkey_problems.setStyleSheet("color: #C62828;")
        layout.addWidget(self.hotkey_problems)
        note = QLabel(
            "點欄位後直接按下組合鍵，需包含 Ctrl、Alt 或 Win；按欄位右側的清除鈕表示不使用。"
            f"校對模式固定使用 {shortcuts.CALIBRATION}。按確定後生效，被其他程式占用時會在系統列提示。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _default_hotkeys(self) -> None:
        for action, edit in self.hotkey_edits.items():
            edit.set_text(shortcuts.DEFAULTS[action])

    def _check_hotkeys(self, *_args) -> None:
        found = []
        values = {}
        for action, edit in self.hotkey_edits.items():
            value = edit.value()
            if value is None:
                found.append(f"「{shortcuts.ACTIONS[action]}」需包含 Ctrl、Alt 或 Win，按鍵限英數字、F1–F24、方向鍵與編輯鍵")
            else:
                values[action] = value
        found += shortcuts.problems(values)
        self.hotkey_problems.setText("\n".join(found))
        self.hotkey_problems.setVisible(bool(found))
        self.ok_button.setEnabled(not found)

    # -- values ----------------------------------------------------------------

    def current(self) -> Settings:
        """The settings as currently shown in the dialog."""
        sources = [SourceSetting(src.name, self.source_boxes[src.name].isChecked()) for src in self.original.sources]
        return replace(
            self.original,
            autostart=self.autostart.isChecked(),
            l3_enabled=self.l3_enabled.isChecked(),
            conversion_mode="s2tw" if self.mode_tw.isChecked() else "s2twp",
            font_latin=self.font_latin.currentFont().family(),
            font_cjk=self.font_cjk.currentFont().family(),
            font_px=self.font_px.value(),
            text_color=self.text_color.color,
            outline_px=self.outline_px.value(),
            outline_color=self.outline_color.color,
            outline_opacity=self.outline_opacity.value() / 100.0,
            shadow_blur=self.shadow_blur.value(),
            shadow_offset_y=self.shadow_offset.value(),
            alignment=self.alignment.currentData(),
            vertical=bool(self.orientation.currentData()),
            line_animation=self.line_animation.isChecked(),
            show_translation=self.show_translation.isChecked(),
            karaoke=self.karaoke.isChecked(),
            sung_color=self.sung_color.color,
            obs_background=self.obs_background.color,
            sources=sources,
            hotkeys={
                action: value if (value := edit.value()) is not None else self.original.hotkeys.get(action, "")
                for action, edit in self.hotkey_edits.items()
            },
        )

    def _preview(self, *_args) -> None:
        if not self._building:
            self.previewChanged.emit(self.current())

    def _open_vocabulary(self) -> None:
        path = ensure_vocabulary_file(self.vocabulary_path)
        os.startfile(path)  # the user's default text editor
