"""Rolling local logs (UTF-8). No lyric text is ever logged, only metadata and scores."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from lyricfloat import APP_NAME


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "logs"


def setup_logging(debug: bool = False) -> Path:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    # Library internals drown out our own debug output.
    for noisy in ("httpcore", "qasync", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.INFO if debug else logging.WARNING)

    main = RotatingFileHandler(directory / "lyricfloat.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    main.setFormatter(fmt)
    root.addHandler(main)

    # Candidate scores as JSON lines, kept apart for weight calibration.
    match = logging.getLogger("lyricfloat.match")
    match.propagate = False
    match.setLevel(logging.INFO)
    for handler in list(match.handlers):
        match.removeHandler(handler)
    match_handler = RotatingFileHandler(directory / "match.jsonl", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    match_handler.setFormatter(logging.Formatter("%(message)s"))
    match.addHandler(match_handler)

    # L3 statistics, numbers only (SPEC v1.22): used to tune the confidence threshold.
    alignment = logging.getLogger("lyricfloat.alignment")
    alignment.propagate = False
    alignment.setLevel(logging.INFO)
    for handler in list(alignment.handlers):
        alignment.removeHandler(handler)
    alignment_handler = RotatingFileHandler(directory / "alignment.jsonl", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    alignment_handler.setFormatter(logging.Formatter("%(message)s"))
    alignment.addHandler(alignment_handler)

    if debug and sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    return directory
