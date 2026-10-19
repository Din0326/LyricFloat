"""Build the portable Windows package (SPEC v1.6: PyInstaller onedir, no installer).

    python tools/build.py [--dist FOLDER]

`--dist` builds somewhere else, e.g. to check a build while dist/ is in use
(a running soak test keeps dist/LyricFloat/LyricFloat.exe open).

1. renders the app icon to build/LyricFloat.ico
2. runs PyInstaller with packaging/LyricFloat.spec into dist/LyricFloat/
3. zips it to dist/LyricFloat-<version>-windows-portable.zip
4. checks the zip against the spec limit (< 80 MB); exit 1 when over

If antivirus or EDR blocks or quarantines the build, the script stops and
reports it; it never renames or moves files to get around a block.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
SPEC = ROOT / "packaging" / "LyricFloat.spec"
LIMIT_MB = 80


def folder_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1048576


def make_icon(path: Path) -> None:
    from PyQt6.QtWidgets import QApplication

    from lyricfloat.ui.tray import make_icon_pixmap

    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841 (QPixmap needs an app)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not make_icon_pixmap(size=256).save(str(path), "ICO"):
        raise RuntimeError(f"could not write {path}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    from lyricfloat import __version__

    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=DIST)
    dist = parser.parse_args().dist.resolve()

    icon = BUILD / "LyricFloat.ico"
    make_icon(icon)
    print(f"圖示：{icon}")

    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(BUILD / "pyinstaller"), str(SPEC),
    ]
    print("執行 PyInstaller…")
    result = subprocess.run(cmd, cwd=ROOT)
    app_dir = dist / "LyricFloat"
    exe = app_dir / "LyricFloat.exe"
    if result.returncode != 0 or not exe.exists():
        print("打包失敗。若是防毒或 EDR 阻擋或隔離了檔案，請記下訊息回報，不要改檔名或換路徑繞過。")
        return 1

    archive = dist / f"LyricFloat-{__version__}-windows-portable.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                zf.write(f, Path("LyricFloat") / f.relative_to(app_dir))
    zip_mb = archive.stat().st_size / 1048576
    print(f"免安裝版資料夾：{app_dir}（{folder_mb(app_dir):.1f} MB）")
    print(f"壓縮檔：{archive}（{zip_mb:.1f} MB，上限 {LIMIT_MB} MB）")
    if zip_mb >= LIMIT_MB:
        print("超過規格上限。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
