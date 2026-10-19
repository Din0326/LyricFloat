"""L2 spike, second try (SPEC v1.22): find anchors by force-aligning the known lyrics
to a 15 s clip instead of recognising the clip.

    .venv/Scripts/python tools/l2_spike/align_run.py --app Spotify.exe --windows 10

Each window records 15 s from the player only, writes it to a temp file that is
deleted right after, and runs the L3 worker (vocal separation + alignment) on the
lines expected inside the clip. Reports per-window offsets against the LRC, the
same measure as the first L2 spike. Prints numbers only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture import Capture, root_pid  # noqa: E402
from common import current  # noqa: E402

from lyricfloat.align.apply import MIN_CONFIDENCE, language_of  # noqa: E402
from lyricfloat.align.runner import run_worker, source_runtime  # noqa: E402
from lyricfloat.app import SOURCE_FACTORIES  # noqa: E402
from lyricfloat.lyrics.chinese import ChineseConverter  # noqa: E402
from lyricfloat.lyrics.pipeline import OpenCCPipeline  # noqa: E402
from lyricfloat.lyrics.service import LyricResolver  # noqa: E402
from lyricfloat.media import smtc  # noqa: E402
from lyricfloat.media.timeline import anchor_from_timeline  # noqa: E402
from lyricfloat.models import ResultKind  # noqa: E402

WINDOW = 15.0
RATE, CHANNELS = 44100, 2
INSIDE = (1.0, 14.0)  # lines expected this far into the clip are aligned


def position_now(session) -> float:
    raw = smtc.read_timeline(session)
    a = anchor_from_timeline(raw, time.time(), time.monotonic())
    return a.position + ((time.monotonic() - a.at) if a.playing else 0.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="Spotify.exe")
    ap.add_argument("--windows", type=int, default=10)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--models-dir", default=str(Path.home() / "AppData" / "Local" / "LyricFloat" / "models"))
    args = ap.parse_args()
    python = source_runtime()
    if python is None:
        raise SystemExit('install the l3 extra first: pip install -e ".[l2,l3]"')

    resolver = LyricResolver([SOURCE_FACTORIES[n]() for n in ("lrclib", "netease")], pipeline=OpenCCPipeline())
    cc = ChineseConverter()
    songs: dict = {}
    cap = Capture(root_pid(args.app), RATE, CHANNELS)
    rows = []
    for n in range(1, args.windows + 1):
        session, before = await current(args.app)
        pcm, first_at = cap.record(WINDOW)
        session, after = await current(args.app)
        if before is None or after is None or before.key != after.key:
            print(json.dumps({"window": n, "skipped": "track changed"}), flush=True)
            continue
        pos0 = position_now(session) - (time.monotonic() - first_at)
        if after.key not in songs:
            result = await resolver.resolve(after)
            songs[after.key] = (len(songs) + 1, list(result.lyrics.lines) if result.kind is ResultKind.SYNCED else None)
        song, lines = songs[after.key]
        if not lines:
            print(json.dumps({"window": n, "song": song, "skipped": "no synced lyrics"}), flush=True)
            continue
        inside = [l for l in lines if l.text.strip() and pos0 + INSIDE[0] <= l.start <= pos0 + INSIDE[1]]
        if not inside:
            print(json.dumps({"window": n, "song": song, "skipped": "no lyric line in the clip"}), flush=True)
            continue
        language = language_of(lines)
        texts = [cc.convert("t2s", l.text) if language == "zh" else l.text for l in inside]
        with tempfile.NamedTemporaryFile(prefix="rec-", suffix=".pcm", delete=False) as f:
            f.write(pcm)
            path = Path(f.name)
        try:
            job = {"pcm_path": str(path), "rate": RATE, "channels": CHANNELS, "lines": texts, "language": language,
                   "model": "base", "threads": args.threads, "models_dir": args.models_dir}
            t0 = time.perf_counter()
            result = run_worker(python, job)
            wall = time.perf_counter() - t0
        finally:
            path.unlink(missing_ok=True)
        if not result.get("ok"):
            print(json.dumps({"window": n, "song": song, "error": result.get("error", "")[:200]}), flush=True)
            continue
        offsets = []
        for line, seg in zip(inside, result["segments"]):
            probs = [w["p"] for w in seg["words"]]
            if probs and statistics.mean(probs) >= MIN_CONFIDENCE:
                offsets.append(pos0 + seg["start"] - line.start)
        row = {
            "window": n,
            "song": song,
            "language": language,
            "lines": len(inside),
            "sure": len(offsets),
            "offset_s": round(statistics.median(offsets), 3) if offsets else None,
            "spread_s": round(max(offsets) - min(offsets), 3) if len(offsets) > 1 else None,
            "wall_s": round(wall, 1),
            "separate_s": round(result["timings"].get("separate_s", 0), 1),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
    await resolver.aclose()
    vals = [r["offset_s"] for r in rows if r["offset_s"] is not None]
    if vals:
        med = statistics.median(vals)
        close = sum(abs(v - med) <= 0.3 for v in vals)
        print(f"summary: {len(vals)}/{len(rows)} windows anchored; median {med:+.3f}s; within 0.3 s of the median {close}/{len(vals)}; values {sorted(vals)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
