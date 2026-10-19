# PyInstaller spec for the portable (onedir) build. Run through tools/build.py.
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
ICON = ROOT / "build" / "LyricFloat.ico"  # written by tools/build.py

# pywinrt namespace packages are imported by name at run time; OpenCC needs its
# config/dictionary files and native library next to the module.
hiddenimports = collect_submodules("winrt") + collect_submodules("lyricfloat") + ["win32com.client"]
hiddenimports += collect_submodules("comtypes")  # L3 process loopback, imported on the recording thread
datas = collect_data_files("opencc")
# L3 (SPEC v1.21): the worker runs under the downloaded runtime, so it ships as a file,
# together with the pinned list of what that runtime installs.
datas += [
    (str(ROOT / "src" / "lyricfloat" / "align" / "worker.py"), "lyricfloat/align"),
    (str(ROOT / "src" / "lyricfloat" / "align" / "runtime_lock.json"), "lyricfloat/align"),
]
binaries = collect_dynamic_libs("opencc")

# OpenCC's wheel vendors its C++ runtime in site-packages/opencc.libs and adds only
# that folder to the DLL search path. rapidfuzz vendors a DLL with the same name, and
# PyInstaller keeps just one copy (under rapidfuzz.libs), so OpenCC failed to load.
import opencc as _opencc  # noqa: E402

_opencc_libs = Path(_opencc.__file__).resolve().parent.parent / "opencc.libs"
binaries += [(str(dll), "opencc.libs") for dll in _opencc_libs.glob("*.dll")]

# Development and measurement tools never ship; Qt modules the app does not use stay out.
excludes = [
    "tkinter",
    "pytest",
    "respx",
    "vcr",
    "yaml",
    "openpyxl",
    "psutil",
    "PyInstaller",
    "torch",  # L3 inference lives in the downloaded runtime, never in the app
    "demucs",
    "stable_whisper",
    "faster_whisper",
    "numpy",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtMultimedia",
    "PyQt6.QtBluetooth",
    "PyQt6.QtNfc",
    "PyQt6.QtSql",
    "PyQt6.QtTest",
    "PyQt6.QtPdf",
    "PyQt6.QtSvg",
    "PyQt6.QtOpenGL",
    "PyQt6.QtPositioning",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtWebSockets",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LyricFloat",
    console=False,  # tray app: no console window
    icon=str(ICON) if ICON.exists() else None,
    upx=False,  # UPX-packed executables are a common antivirus false positive
)
coll = COLLECT(exe, a.binaries, a.datas, name="LyricFloat", upx=False)
