"""Start with Windows via a shortcut in the user's Startup folder (SPEC v1.6: no registry).

Only touched when the user flips the setting. Running from source, the
shortcut launches pythonw.exe (no console window) with "-m lyricfloat";
a packaged build launches its own executable.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from lyricfloat import APP_NAME

log = logging.getLogger(__name__)

SHORTCUT_NAME = f"{APP_NAME}.lnk"


def startup_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launch_command() -> tuple[str, str, str]:
    """(target, arguments, working directory) for the shortcut."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)
    interpreter = Path(sys.executable)
    windowed = interpreter.with_name("pythonw.exe")
    return str(windowed if windowed.exists() else interpreter), "-m lyricfloat", str(Path.home())


def shortcut_path(directory: Path | None = None) -> Path:
    return (directory or startup_dir()) / SHORTCUT_NAME


def is_enabled(directory: Path | None = None) -> bool:
    return shortcut_path(directory).exists()


def set_enabled(enabled: bool, directory: Path | None = None) -> None:
    path = shortcut_path(directory)
    if not enabled:
        path.unlink(missing_ok=True)
        log.info("autostart off: removed %s", path)
        return
    import win32com.client  # pywin32; imported only when the user turns this on

    target, arguments, workdir = launch_command()
    path.parent.mkdir(parents=True, exist_ok=True)
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path))
    shortcut.TargetPath = target
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = workdir
    shortcut.Description = f"{APP_NAME} 桌面懸浮歌詞"
    shortcut.Save()
    log.info("autostart on: %s -> %s %s", path, target, arguments)
