"""L2 spike: can a 15 s process-loopback clip plus Whisper find the lyric offset?

Usage (a song with synced lyrics playing in the player):
    .venv/Scripts/python tools/l2_spike/run.py --app Spotify.exe --windows 3 --model base

For each window it captures the player's audio, recognizes it in a separate
process, anchors lyric lines inside +-15 s of the expected position (SPEC L2)
and reports the median offset. The same recognition is then scored against the
lyrics shifted by +-3 s (FR-20 acceptance). Prints numbers only: no lyric or
recognized text is shown or stored, and the audio never leaves memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture import Capture, root_pid  # noqa: E402
from common import current, whisper_language  # noqa: E402

from lyricfloat.app import SOURCE_FACTORIES  # noqa: E402
from lyricfloat.lyrics.chinese import ChineseConverter  # noqa: E402
from lyricfloat.lyrics.pipeline import OpenCCPipeline  # noqa: E402
from lyricfloat.lyrics.service import LyricResolver  # noqa: E402
from lyricfloat.media import smtc  # noqa: E402
from lyricfloat.media.timeline import anchor_from_timeline  # noqa: E402
from lyricfloat.models import LyricLine, ResultKind  # noqa: E402

WINDOW = 15.0  # seconds of audio per attempt (SPEC L2)
SEARCH = 15.0  # anchor only lines within +-15 s of the expected position
MIN_SCORE = 0.7  # SPEC: anchors below 0.7 are not applied
MIN_CHARS = 4  # lines this short match anything
_KEEP = re.compile(r"[\w぀-ヿ㐀-鿿가-힯]+")
CC = ChineseConverter()


def norm(text: str) -> str:
    return "".join(_KEEP.findall(CC.convert("t2s", text).casefold())).replace("_", "")


@dataclass
class Anchor:
    line: int
    score: float
    heard_at: float  # song position where the line's first matched character was heard
    offset: float  # heard_at - line.start: positive means the lyrics run early


def char_stream(segments: list[dict], pos0: float) -> tuple[str, list[float]]:
    """Recognized text as one normalized string with the song time of each character."""
    chars: list[str] = []
    times: list[float] = []
    for seg in segments:
        for w in seg["words"]:
            t = norm(w["text"])
            chars.append(t)
            times.extend([pos0 + w["start"]] * len(t))
    return "".join(chars), times


def anchors_for(
    lines: list[LyricLine], stream: str, times: list[float], pos0: float, expected_shift: float = 0.0, search: float = SEARCH
) -> list[Anchor]:
    found = []
    lo, hi = pos0 - search, pos0 + WINDOW + search
    for i, line in enumerate(lines):
        start = line.start + expected_shift
        if not lo <= start <= hi:
            continue
        target = norm(line.text)
        if len(target) < MIN_CHARS or not stream:
            continue
        al = fuzz.partial_ratio_alignment(target, stream)
        if al is None or al.score / 100 < MIN_SCORE:
            continue
        heard = times[al.dest_start]
        found.append(Anchor(i, al.score / 100, heard, heard - line.start))
    return found


def estimate(anchors: list[Anchor]) -> tuple[float | None, float | None]:
    """Median offset and median absolute deviation."""
    if not anchors:
        return None, None
    offs = [a.offset for a in anchors]
    med = statistics.median(offs)
    return med, statistics.median(abs(o - med) for o in offs)


def recognize(pcm: bytes, model: str, models_dir: Path, language: str, prompt: str = "") -> tuple[dict, dict]:
    worker = Path(__file__).resolve().parent / "recognize.py"
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(worker), model, str(models_dir), language, prompt],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ps = psutil.Process(proc.pid)
    proc.stdin.write(pcm)
    proc.stdin.close()
    peak_rss = 0
    cpu = 0.0
    while proc.poll() is None:
        try:
            # The venv python.exe is a launcher: the real interpreter is its child.
            family = [ps, *ps.children(recursive=True)]
            peak_rss = max(peak_rss, sum(p.memory_info().rss for p in family))
            cpu = max(cpu, sum(p.cpu_times().user + p.cpu_times().system for p in family))
        except psutil.Error:
            pass
        time.sleep(0.05)
    out = proc.stdout.read()
    err = proc.stderr.read().decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed ({proc.returncode}): {err[-800:]}")
    return json.loads(out.decode("utf-8")), {"wall_s": time.perf_counter() - t0, "cpu_s": cpu, "peak_rss_mb": peak_rss / 2**20}


def position_now(session) -> float:
    raw = smtc.read_timeline(session)
    anchor = anchor_from_timeline(raw, time.time(), time.monotonic())
    return anchor.position + ((time.monotonic() - anchor.at) if anchor.playing else 0.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="Spotify.exe")
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--model", default="base")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--models-dir", default=str(Path.home() / "AppData" / "Local" / "LyricFloat" / "models"))
    args = ap.parse_args()

    pipeline = OpenCCPipeline()
    resolver = LyricResolver([SOURCE_FACTORIES[n]() for n in ("lrclib", "netease")], pipeline=pipeline)
    songs: dict = {}

    async def lyrics_for(track):
        if track.key not in songs:
            result = await resolver.resolve(track)
            ok = result.kind is ResultKind.SYNCED
            songs[track.key] = (len(songs) + 1, list(result.lyrics.lines) if ok else None, result.candidate.source if ok else None)
            print(f"song {songs[track.key][0]}: {'%d synced lines from %s' % (len(songs[track.key][1]), songs[track.key][2]) if ok else 'no synced lyrics'}, duration {track.duration:.0f}s")
        return songs[track.key]

    pid = root_pid(args.app)
    cap = Capture(pid)
    rows = []
    for n in range(args.windows):
        session, before = await current(args.app)
        pcm, first_at = cap.record(WINDOW)
        session, after = await current(args.app)
        if before is None or after is None or before.key != after.key:
            print(json.dumps({"window": n + 1, "skipped": "track changed during capture"}))
            continue
        song_no, lines, _source = await lyrics_for(after)
        if lines is None:
            print(json.dumps({"window": n + 1, "song": song_no, "skipped": "no synced lyrics"}))
            continue
        pos_at_capture_end = position_now(session)
        pos0 = pos_at_capture_end - (time.monotonic() - first_at)
        language = whisper_language(lines) if args.language == "auto" else args.language
        data, cost = recognize(pcm, args.model, Path(args.models_dir), language)
        stream, times = char_stream(data["segments"], pos0)
        base = anchors_for(lines, stream, times, pos0)
        med, mad = estimate(base)
        nearby = [l.text for l in lines if pos0 - 5 <= l.start <= pos0 + WINDOW + 5]
        pdata, _pcost = recognize(pcm, args.model, Path(args.models_dir), language, " ".join(nearby)[:600])
        pstream, ptimes = char_stream(pdata["segments"], pos0)
        prompted = anchors_for(lines, pstream, ptimes, pos0)
        pmed, pmad = estimate(prompted)
        narrow = anchors_for(lines, pstream, ptimes, pos0, search=5.0)
        nmed, nmad = estimate(narrow)
        shifted = {}
        for shift in (3.0, -3.0):
            moved = [LyricLine(l.start + shift, l.text) for l in lines]
            # Expected position is not corrected first here (worst case for the +-15 s window).
            a = anchors_for(moved, stream, times, pos0)
            m, _ = estimate(a)
            shifted[shift] = (len(a), m)
        row = {
            "window": n + 1,
            "song": song_no,
            "pos0": round(pos0, 2),
            "language": data["language"],
            "peak": round(data["peak"], 3),
            "recognized_chars": len(stream),
            "anchors": len(base),
            "median_offset_s": None if med is None else round(med, 3),
            "mad_s": None if mad is None else round(mad, 3),
            "scores": [round(a.score, 2) for a in base],
            "prompted_chars": len(pstream),
            "prompted_anchors": len(prompted),
            "prompted_offset_s": None if pmed is None else round(pmed, 3),
            "prompted_mad_s": None if pmad is None else round(pmad, 3),
            "narrow_anchors": len(narrow),
            "narrow_offset_s": None if nmed is None else round(nmed, 3),
            "narrow_mad_s": None if nmad is None else round(nmad, 3),
            "shift+3": shifted[3.0],
            "shift-3": shifted[-3.0],
            **{k: round(v, 2) for k, v in cost.items()},
            "model_load_s": round(data["load_s"], 2),
            "infer_s": round(data["run_s"], 2),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    cap.close()
    await resolver.aclose()

    for key in ("prompted_offset_s", "narrow_offset_s"):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if vals:
            print(f"summary {key}: {len(vals)}/{len(rows)} windows; median {statistics.median(vals):+.3f}s; values {sorted(vals)}")
    offs = [r["median_offset_s"] for r in rows if r["median_offset_s"] is not None]
    if offs:
        print(f"summary: {len(offs)}/{len(rows)} windows anchored; offset median {statistics.median(offs):+.3f}s, spread {max(offs) - min(offs):.3f}s")
        for shift in (3.0, -3.0):
            key = "shift+3" if shift > 0 else "shift-3"
            errs = [abs((r[key][1] - r["median_offset_s"]) + shift) for r in rows if r[key][1] is not None and r["median_offset_s"] is not None]
            if errs:
                print(f"  lyrics shifted {shift:+.0f}s: correction error vs unshifted estimate max {max(errs) * 1000:.0f} ms")
    else:
        print("summary: no window anchored")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
