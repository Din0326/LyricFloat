"""FR-25: "這首歌詞有誤" — save the song's record and a lyric snapshot for later checking.

Reports stay on this machine (%LOCALAPPDATA%\\LyricFloat\\reports) and are
never uploaded. The test list is a CSV in %APPDATA%\\LyricFloat that the
hit-rate tool reads; it is personal listening data, so it lives outside
the repository.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lyricfloat import APP_NAME, __version__
from lyricfloat.lyrics.cache import default_cache_dir
from lyricfloat.models import LyricResult, TrackInfo
from lyricfloat.settings import config_dir

TEST_LIST_FIELDS = ["title", "artist", "album", "duration", "language", "category", "source_app", "added", "note"]
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def reports_dir() -> Path:
    return default_cache_dir() / "reports"


def test_list_path() -> Path:
    return config_dir() / "test_list.csv"


def build_report(
    track: TrackInfo,
    shown: TrackInfo,
    result: LyricResult | None,
    candidates: Sequence[dict[str, Any]],
    position: float | None,
    line_index: int,
    conversion: str | None,
    note: str = "",
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now()
    report: dict[str, Any] = {
        "app": f"{APP_NAME} {__version__}",
        "exported_at": now.isoformat(timespec="seconds"),
        "note": note,
        "track": {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration": track.duration,
            "source_app": track.source_app,
        },
        "shown_as": {"title": shown.title, "artist": shown.artist},
        "playback": {"position": None if position is None else round(position, 3), "line_index": line_index},
        "conversion": conversion,
        "candidates": list(candidates),
        "result": None,
        "lyrics_snapshot": [],
    }
    if result is not None:
        cand = result.candidate
        report["result"] = {
            "kind": result.kind.value,
            "source": cand.source if cand else None,
            "source_id": cand.source_id if cand else None,
            "candidate": {"title": cand.title, "artist": cand.artist, "duration": cand.duration} if cand else None,
            "score": result.score,
            "from_cache": result.from_cache,
            "offset": result.offset,
            "timeline_checked": result.timeline_checked,
            "bound": result.bound,
            "notes": list(result.notes),
        }
        if result.lyrics is not None:
            translations = result.lyrics.translations or ()
            report["lyrics_snapshot"] = [
                {
                    "start": round(line.start, 3),
                    "text": line.text,
                    "translation": translations[i] if i < len(translations) else None,
                }
                for i, line in enumerate(result.lyrics.lines)
            ]
            report["synced"] = result.lyrics.synced
    return report


def write_report(report: dict[str, Any], directory: Path | None = None) -> Path:
    directory = directory or reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["exported_at"].replace(":", "").replace("-", "")
    title = _UNSAFE.sub("_", report["track"]["title"]).strip(" ._")[:40] or "untitled"
    path = directory / f"{stamp}_{title}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def add_to_test_list(
    track: TrackInfo, language: str, note: str = "", path: Path | None = None, category: str = ""
) -> bool:
    """Append the song unless it is already listed (same title, artist and album). Returns True if added."""
    path = path or test_list_path()
    key = (track.title.casefold(), track.artist.casefold(), track.album.casefold())
    fieldnames = TEST_LIST_FIELDS
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or TEST_LIST_FIELDS  # keep an older file's columns
            for row in reader:
                if (row.get("title", "").casefold(), row.get("artist", "").casefold(), row.get("album", "").casefold()) == key:
                    return False
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens the Chinese titles correctly.
    with path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "duration": round(track.duration, 1),
                "language": language,
                "category": category,
                "source_app": track.source_app,
                "added": dt.date.today().isoformat(),
                "note": note,
            }
        )
    return True


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(path)  # Explorer


def reveal_command(path: Path) -> str:
    # Explorer only understands /select, when the switch itself is unquoted; passing a
    # list lets Python quote the whole argument for names with spaces, and Explorer
    # then just opens its default folder.
    return f'explorer /select,"{path}"'


def reveal(path: Path) -> None:
    """Open Explorer with the file selected."""
    subprocess.Popen(reveal_command(path))
