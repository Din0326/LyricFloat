"""Theme import and export (FR-26): the appearance settings as a JSON file.

Only the appearance tab travels: no window position, shortcuts, sources or
anything else from the settings file. Values are checked the same way the
settings file is, so a hand-edited or foreign theme cannot break the app.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path

from lyricfloat.settings import Settings, settings_from_dict

FORMAT = "lyricfloat-theme"
VERSION = 1
FIELDS = (
    "font_latin",
    "font_cjk",
    "font_px",
    "text_color",
    "outline_px",
    "outline_color",
    "outline_opacity",
    "shadow_blur",
    "shadow_offset_y",
    "alignment",
    "line_animation",
    "show_translation",
    "karaoke",
    "sung_color",
    "obs_background",
    "vertical",
)
FONT_FIELDS = ("font_latin", "font_cjk")
COLOR_FIELDS = ("text_color", "outline_color", "sung_color", "obs_background")
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
LABELS = {
    "font_latin": "西文字型",
    "font_cjk": "中文字型",
    "font_px": "字級",
    "text_color": "文字顏色",
    "outline_px": "描邊粗細",
    "outline_color": "描邊顏色",
    "outline_opacity": "描邊不透明度",
    "shadow_blur": "陰影模糊",
    "shadow_offset_y": "陰影向下偏移",
    "alignment": "對齊方式",
    "line_animation": "換句動畫",
    "show_translation": "顯示翻譯",
    "karaoke": "卡拉 OK 填色",
    "sung_color": "已唱顏色",
    "obs_background": "OBS 視窗背景",
    "vertical": "排列方向",
}


class ThemeError(ValueError):
    """The file is not a LyricFloat theme."""


def export_theme(settings: Settings, path: Path) -> None:
    data = {"format": FORMAT, "version": VERSION, "appearance": {f: getattr(settings, f) for f in FIELDS}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_theme(settings: Settings, path: Path, installed_fonts: Collection[str] | None = None) -> tuple[Settings, list[str]]:
    """`settings` with the theme's appearance applied, plus notes for the user.

    Unknown or invalid values keep the current setting; fonts missing on this
    computer fall back to the defaults (when `installed_fonts` is given).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ThemeError(f"無法讀取主題檔：{exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("appearance"), dict):
        raise ThemeError("這不是 LyricFloat 的主題檔。")
    given = {k: v for k, v in data["appearance"].items() if k in FIELDS}
    for k in COLOR_FIELDS:
        if k in given and not (isinstance(given[k], str) and _COLOR.fullmatch(given[k])):
            given[k] = None  # coerced back to the default below, with a note
    checked = settings_from_dict({**{f: getattr(settings, f) for f in FIELDS}, **given})
    notes = [
        f"「{LABELS[k]}」的值無效或超出範圍，已改用 {getattr(checked, k)}"
        for k in given
        if getattr(checked, k) != given[k] and not _same(given[k], getattr(checked, k))
    ]
    values = {f: getattr(checked, f) for f in FIELDS}
    if installed_fonts is not None:
        defaults = Settings()
        folded = {name.casefold() for name in installed_fonts}
        for f in FONT_FIELDS:
            if values[f].casefold() not in folded:
                notes.append(f"這台電腦沒有安裝字型「{values[f]}」，已改用預設的「{getattr(defaults, f)}」")
                values[f] = getattr(defaults, f)
    return replace(settings, **values), notes


def _same(a, b) -> bool:
    """Ints in the file for float settings (2 vs 2.0) are the same value."""
    return isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, float) and float(a) == b
