"""FR-25 dialog: optional note, and whether to add the song to the test list."""

from __future__ import annotations

from PyQt6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout, QWidget


class ReportDialog(QDialog):
    def __init__(self, song: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("這首歌詞有誤")
        self.setMinimumWidth(420)

        heading = QLabel(f"回報：{song}")
        heading.setWordWrap(True)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("哪裡有誤？（選填，例如：整首早了一句、是別首歌的歌詞、有簡體字）")
        self.note.setFixedHeight(90)
        self.add_to_list = QCheckBox("同時加入測試歌單")
        self.add_to_list.setChecked(True)
        self.reveal = QCheckBox("匯出後開啟資料夾")
        self.reveal.setChecked(True)
        info = QLabel("會把這首歌的候選、評分與歌詞快照存成檔案，只存在這台電腦，不會上傳。")
        info.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("匯出")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        for widget in (heading, self.note, self.add_to_list, self.reveal, info, buttons):
            layout.addWidget(widget)

    def note_text(self) -> str:
        return self.note.toPlainText().strip()
