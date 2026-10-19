"""User settings, persisted as JSON under %APPDATA%\\LyricFloat.

No credentials live here (spec: Spotify sp_dc goes to the Windows
credential manager in v1.1). Loading is forgiving: unknown keys are
ignored, bad values fall back to defaults, so a hand-edited or older
file never stops the app from starting.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from lyricfloat import APP_NAME, shortcuts

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CONVERSION_MODES = ("s2twp", "s2tw")
ALIGNMENTS = ("center", "left")
KNOWN_SOURCES = ("lrclib", "netease")
FONT_PX_RANGE = (12, 96)


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


@dataclass
class SourceSetting:
    name: str
    enabled: bool = True


def _default_sources() -> list[SourceSetting]:
    return [SourceSetting(name) for name in KNOWN_SOURCES]


@dataclass
class Settings:
    # General
    conversion_mode: str = "s2twp"
    autostart: bool = False
    l3_enabled: bool = False  # FR-21, off until the user agrees (SPEC v1.15)
    # Appearance (spec UI defaults)
    font_latin: str = "Segoe UI"
    font_cjk: str = "Microsoft JhengHei"
    font_px: int = 32
    text_color: str = "#FFFFFF"
    outline_px: float = 2.0
    outline_color: str = "#000000"
    outline_opacity: float = 0.7
    shadow_blur: float = 6.0
    shadow_offset_y: float = 2.0
    alignment: str = "center"
    line_animation: bool = True
    show_translation: bool = True
    karaoke: bool = True  # FR-11 fill, interpolated per line in v1.1
    sung_color: str = "#4FC3F7"
    # Lyric sources, in priority order
    sources: list[SourceSetting] = field(default_factory=_default_sources)
    # Window state
    window_rect: list[int] | None = None  # x, y, width, height
    vertical_rect: list[int] | None = None  # the same in vertical mode
    locked: bool = False
    visible: bool = True
    obs_window: bool = False  # FR-18 window open
    obs_rect: list[int] | None = None
    vertical: bool = False  # FR-16
    obs_background: str = "#00FF00"  # FR-18
    pinned_player: str = ""  # FR-24: SMTC app id, "" follows the Windows current session
    hotkeys: dict[str, str] = field(default_factory=lambda: dict(shortcuts.DEFAULTS))  # FR-14, "" = none

    def enabled_sources(self) -> list[str]:
        return [s.name for s in self.sources if s.enabled]


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Return `value` if it fits the field, else `default`."""
    if name == "sources":
        return _coerce_sources(value)
    if name == "hotkeys":
        return _coerce_hotkeys(value)
    if name in ("window_rect", "vertical_rect", "obs_rect"):
        ok = isinstance(value, list) and len(value) == 4 and all(isinstance(v, int) for v in value)
        return value if ok else None
    if name == "conversion_mode":
        return value if value in CONVERSION_MODES else default
    if name == "alignment":
        return value if value in ALIGNMENTS else default
    if name == "font_px":
        if isinstance(value, int) and not isinstance(value, bool):
            return min(max(value, FONT_PX_RANGE[0]), FONT_PX_RANGE[1])
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else default
    if isinstance(default, str):
        return value if isinstance(value, str) and value.strip() else default
    return default


def _coerce_hotkeys(value: Any) -> dict[str, str]:
    result = dict(shortcuts.DEFAULTS)
    if isinstance(value, dict):
        for action in result:
            text = value.get(action)
            if isinstance(text, str) and (normal := shortcuts.normalize(text)) is not None:
                result[action] = normal
    return result


def _coerce_sources(value: Any) -> list[SourceSetting]:
    result: list[SourceSetting] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name in KNOWN_SOURCES and name not in {s.name for s in result}:
                result.append(SourceSetting(name, bool(item.get("enabled", True))))
    for name in KNOWN_SOURCES:  # sources added in a later version join at the end
        if name not in {s.name for s in result}:
            result.append(SourceSetting(name))
    return result


def settings_from_dict(data: dict[str, Any]) -> Settings:
    defaults = Settings()
    values = {}
    for f in fields(Settings):
        default = getattr(defaults, f.name)
        values[f.name] = _coerce(f.name, data[f.name], default) if f.name in data else default
    return Settings(**values)


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, **asdict(settings)}


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "settings.json"

    def load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError) as exc:
            log.warning("settings unreadable (%s), using defaults", exc)
            return Settings()
        if not isinstance(raw, dict):
            log.warning("settings file is not an object, using defaults")
            return Settings()
        return settings_from_dict(raw)

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings_to_dict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash mid-write never leaves half a file
