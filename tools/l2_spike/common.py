"""Helpers shared by the L2 and L3 spike runners."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lyricfloat.media import smtc  # noqa: E402
from lyricfloat.models import LyricLine  # noqa: E402


def whisper_language(lines: list[LyricLine]) -> str:
    text = "".join(l.text for l in lines)
    if re.search(r"[가-힯]", text):
        return "ko"
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[㐀-鿿]", text):
        return "zh"
    return "en"


async def current(app: str):
    manager = await smtc.request_manager()
    for s in manager.get_sessions():
        if smtc.app_id(s).lower().startswith(app.lower().removesuffix(".exe")):
            track = await smtc.read_track(s)
            return s, track
    raise SystemExit(f"no media session for {app}")
