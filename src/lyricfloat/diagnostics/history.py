"""Local play history: one JSON line per song played (no lyric text).

Source for test-list candidates (SPEC v1.6: the 200-song list is built
from play history, then curated by hand). Stays on this machine.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from lyricfloat.logging_setup import log_dir
from lyricfloat.models import LyricResult, TrackInfo


def history_path() -> Path:
    return log_dir() / "history.jsonl"


def append_play(track: TrackInfo, result: LyricResult, language: str, path: Path | None = None) -> None:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cand = result.candidate
    record = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration": round(track.duration, 1),
        "source_app": track.source_app,
        "language": language,
        "kind": result.kind.value,
        "source": cand.source if cand else None,
        "score": result.score,
        "from_cache": result.from_cache,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or history_path()
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a half-written line from a crash
        if isinstance(item, dict) and item.get("title"):
            records.append(item)
    return records
