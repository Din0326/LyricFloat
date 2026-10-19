"""MV spike: can the singing start of a version of another length be found by listening
during the first play, and how soon? (follow-up to SPEC v1.29-v1.35)

    .venv/Scripts/python tools/l2_spike/mv_start.py --app Chrome --songs 3

For each new song whose lyrics are a version more than 10 s off the player's length
(an MV), the player is recorded without a break and cut into 10 s clips, each sent with
the last 2 s of the one before. Every clip that can hold the singing start (the first
sung line's source time plus 0 to the length gap, 3 s either side) goes to the L3 worker
with the first three sung lines. A clip anchors when two of them align with confidence
and agree within 1 s.

Press Ctrl+Alt+M as the first line starts: that is the truth the anchor is measured
against (calibration mode must be off in the app, it owns that key then). Prints and
appends numbers and song titles only, never lyrics, to reports/raw/mv_start_<time>.jsonl.
Audio stays in memory; each clip goes to a temp file that is deleted right after.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil  # noqa: E402

from common import current  # noqa: E402
from lyricfloat.align.apply import MIN_CONFIDENCE, language_of  # noqa: E402
from lyricfloat.align.capture import LoopbackStream  # noqa: E402
from lyricfloat.align.processes import player_pid  # noqa: E402
from lyricfloat.align.runner import source_runtime  # noqa: E402
from lyricfloat.align.runtime import installed_python, worker_for  # noqa: E402
from lyricfloat.lyrics.cache import default_cache_dir  # noqa: E402
from lyricfloat.lyrics.chinese import ChineseConverter  # noqa: E402
from lyricfloat.lyrics.lrc import first_sung_line  # noqa: E402
from lyricfloat.lyrics.pipeline import OpenCCPipeline  # noqa: E402
from lyricfloat.lyrics.service import LyricResolver  # noqa: E402
from lyricfloat.lyrics.sources.lrclib import LrclibSource  # noqa: E402
from lyricfloat.lyrics.sources.netease import NeteaseSource  # noqa: E402
from lyricfloat.media import smtc  # noqa: E402
from lyricfloat.media.timeline import anchor_from_timeline  # noqa: E402
from lyricfloat.models import ResultKind  # noqa: E402

RATE, CHANNELS, BLOCK = 44100, 2, 4
CLIP, OVERLAP = 10.0, 2.0
WAIT_GAP = 10.0  # the app waits for the cue beyond this (SPEC v1.30)
MARGIN = 3.0
LINES = 3  # first sung lines aligned per clip
AGREE = 1.0  # seconds: the sure lines' offsets must agree this well
AFTER = 40.0  # seconds past the last possible start before a song is given up
IDLE_PRIORITY_CLASS, CREATE_NO_WINDOW = 0x40, 0x08000000
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312


class MarkKey(threading.Thread):
    """Ctrl+Alt+<key> as a global hotkey on its own thread; records when it is pressed."""

    def __init__(self, key: str) -> None:
        super().__init__(daemon=True)
        self.vk = ord(key.upper())
        self.marks: list[float] = []
        self.ready = threading.Event()
        self.ok = False

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self.ok = bool(user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, self.vk))
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.marks.append(time.monotonic())
                print("  [mark]", flush=True)


class Tape:
    """The player's audio since `start`, kept in memory: one stream, no gaps."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.data = bytearray()
        self.start: float | None = None  # monotonic time of the first frame
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error = ""

    def _run(self) -> None:
        try:
            stream = LoopbackStream(self.pid, RATE, CHANNELS)
        except OSError as exc:
            self.error = str(exc)
            return

        def sink(chunk: bytes, at: float) -> None:
            with self.lock:
                if self.start is None:
                    self.start = at
                self.data += chunk

        try:
            stream.run(sink, self.stop)
        finally:
            stream.close()

    def begin(self) -> None:
        self.thread.start()

    def end(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.data = bytearray()

    def clip(self, t0: float, t1: float) -> bytes:
        with self.lock:
            if self.start is None:
                return b""
            a = max(0, int((t0 - self.start) * RATE)) * BLOCK
            b = max(0, int((t1 - self.start) * RATE)) * BLOCK
            return bytes(self.data[a:b])


async def now_playing(app: str):
    """(session, track) for the app, or (None, None) while it has no media session."""
    try:
        return await current(app)
    except SystemExit:
        return None, None


def position_at(session, t: float) -> float | None:
    """Player position at monotonic time `t`, from the session's timeline."""
    a = anchor_from_timeline(smtc.read_timeline(session), time.time(), time.monotonic())
    if a is None:
        return None
    return a.position + ((t - a.at) if a.playing else 0.0)


def run_worker(python: Path, job: dict) -> tuple[dict, dict]:
    """One worker run at the app's priority; also reports wall time and peak memory."""
    env = {**os.environ, "TQDM_DISABLE": "1", "PYTHONIOENCODING": "utf-8"}
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(python), str(worker_for(python))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=IDLE_PRIORITY_CLASS | CREATE_NO_WINDOW, env=env,
    )
    box: dict = {}
    talk = threading.Thread(target=lambda: box.update(zip(("out", "err"), proc.communicate(json.dumps(job, ensure_ascii=False).encode("utf-8")))))
    talk.start()
    peak, ps = 0, psutil.Process(proc.pid)
    while talk.is_alive():
        try:
            peak = max(peak, sum(p.memory_info().rss for p in (ps, *ps.children(recursive=True))))
        except psutil.Error:
            pass
        talk.join(0.2)
    try:
        result = json.loads(box.get("out", b"").decode("utf-8"))
    except ValueError:
        result = {"ok": False, "error": box.get("err", b"").decode("utf-8", "replace")[-300:]}
    return result, {"wall_s": round(time.perf_counter() - t0, 2), "peak_mb": round(peak / 2**20)}


def judge(lines, segments, pos0: float, low: float, high: float) -> tuple[float | None, list[dict]]:
    """The clip's anchor (offset of the source times) when two lines are sure and agree."""
    found = []
    for line, seg in zip(lines, segments):
        probs = [w.get("p", 0.0) for w in seg.get("words", [])]
        conf = statistics.mean(probs) if probs else 0.0
        offset = pos0 + seg["start"] - line.start if seg.get("start") is not None else None
        found.append({"conf": round(conf, 3), "offset_s": None if offset is None else round(offset, 2)})
    sure = [f["offset_s"] for f in found if f["offset_s"] is not None and f["conf"] >= MIN_CONFIDENCE and low <= f["offset_s"] <= high]
    if len(sure) >= 2 and max(sure) - min(sure) <= AGREE:
        return round(statistics.median(sure), 2), found
    return None, found


async def follow_song(args, session, track, result, python, cc, mark: MarkKey, out) -> dict:
    lines = result.lyrics.lines
    first = first_sung_line(lines, track.title, track.artist)
    sung = [l for l in lines[first:] if any(ch.isalnum() for ch in l.text)][:LINES]
    gap = track.duration - result.candidate.duration
    low, high = min(0.0, gap) - MARGIN, max(0.0, gap) + MARGIN  # plausible offsets
    start_low, start_high = sung[0].start + low, sung[0].start + high  # plausible singing starts
    language = language_of(lines)
    texts = [cc.convert("t2s", l.text) if language == "zh" else l.text for l in sung]
    summary = {"song": track.title, "artist": track.artist, "gap_s": round(gap, 1), "first_line_s": sung[0].start,
               "language": language, "clips": 0}
    tape = Tape(player_pid(track.source_app or args.app) or 0)
    tape.begin()
    mark.marks.clear()
    found_at = found_offset = persistent_at = None
    try:
        while tape.start is None and not tape.error:
            await asyncio.sleep(0.1)
        if tape.error:
            summary["error"] = tape.error
            return summary
        clip_end = tape.start + CLIP
        while True:
            await asyncio.sleep(max(0.0, clip_end - time.monotonic()))
            _, now_track = await now_playing(args.app)
            if now_track is None or now_track.key != track.key:
                summary["ended"] = "track changed"
                break
            pos_end = position_at(session, clip_end)
            if pos_end is None:
                break
            pos0 = pos_end - CLIP - OVERLAP
            if pos0 > start_high + AFTER:
                summary["ended"] = "past the possible start"
                break
            row = {"clip": summary["clips"] + 1, "pos0_s": round(pos0, 1)}
            if found_offset is None and pos_end >= start_low and pos0 <= start_high + 5:
                pcm = tape.clip(clip_end - CLIP - OVERLAP, clip_end)
                with tempfile.NamedTemporaryFile(prefix="mv-", suffix=".pcm", delete=False) as f:
                    f.write(pcm)
                    path = Path(f.name)
                try:
                    queue = time.monotonic() - clip_end
                    job = {"pcm_path": str(path), "rate": RATE, "channels": CHANNELS, "lines": texts, "language": language,
                           "model": "base", "threads": args.threads, "models_dir": str(default_cache_dir() / "models")}
                    res, cost = run_worker(python, job)
                finally:
                    path.unlink(missing_ok=True)
                summary["clips"] += 1
                timings = {k: round(v, 2) for k, v in res.get("timings", {}).items()}
                row.update(queue_s=round(queue, 2), **cost, **timings)
                if res.get("ok"):
                    anchor, per_line = judge(sung, res["segments"], pos0, low, high)
                    row.update(lines=per_line, anchor_s=anchor)
                    if anchor is not None:
                        found_offset = anchor
                        found_at = clip_end + queue + cost["wall_s"]
                        persistent_at = clip_end + timings.get("separate_s", 0) + timings.get("align_s", 0)
                else:
                    row["error"] = res.get("error", "")[:200]
            else:
                row["skipped"] = "found already" if found_offset is not None else "outside the possible start"
            row = {"song": track.title, **row}
            print(json.dumps(row, ensure_ascii=False), flush=True)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if found_offset is not None and mark.marks:
                break
            clip_end += CLIP
    finally:
        tape.end()
    if mark.marks:
        truth_pos = position_at(session, mark.marks[0])
        if truth_pos is not None:
            summary["truth_offset_s"] = round(truth_pos - sung[0].start, 2)
    summary["found_offset_s"] = found_offset
    if found_offset is not None and "truth_offset_s" in summary:
        summary["error_s"] = round(found_offset - summary["truth_offset_s"], 2)
    if found_at is not None and mark.marks:
        summary["found_after_start_s"] = round(found_at - mark.marks[0], 1)
        summary["found_after_start_if_loaded_s"] = round(persistent_at - mark.marks[0], 1)
    return summary


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="Chrome")
    ap.add_argument("--songs", type=int, default=3)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--key", default="M", help="Ctrl+Alt+<key> marks the first line")
    args = ap.parse_args()
    python = source_runtime() or installed_python(default_cache_dir() / "runtime")
    if python is None:
        raise SystemExit("no L3 runtime: turn on automatic timing in the app first, or pip install -e \".[l2,l3]\"")
    mark = MarkKey(args.key)
    mark.start()
    mark.ready.wait(2)
    if not mark.ok:
        raise SystemExit(f"Ctrl+Alt+{args.key} is taken (calibration mode on?): pick another with --key")
    cc = ChineseConverter()
    resolver = LyricResolver([LrclibSource(), NeteaseSource()], pipeline=OpenCCPipeline(converter=cc))
    out_path = ROOT / "reports" / "raw" / f"mv_start_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"waiting for MVs in {args.app}; press Ctrl+Alt+{args.key} when the first line starts. Output: {out_path}", flush=True)
    done, seen = 0, None
    with out_path.open("a", encoding="utf-8") as out:
        while done < args.songs:
            session, track = await now_playing(args.app)
            if track is None or track.key == seen:
                await asyncio.sleep(0.5)
                continue
            seen = track.key
            result = await resolver.resolve(track)
            gap = track.duration - result.candidate.duration if result.candidate and result.candidate.duration else 0.0
            if result.kind is not ResultKind.SYNCED or not result.version_mismatch or abs(gap) <= WAIT_GAP:
                print(f"skip: {track.describe()} ({result.kind.value}, gap {gap:+.0f}s) - not an MV that waits for the cue", flush=True)
                continue
            position = position_at(session, time.monotonic()) or 0.0
            print(f"MV: {track.describe()} gap {gap:+.0f}s, now at {position:.0f}s", flush=True)
            summary = await follow_song(args, session, track, result, python, cc, mark, out)
            summary = {"summary": True, **summary}
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            out.write(json.dumps(summary, ensure_ascii=False) + "\n")
            out.flush()
            done += 1
    await resolver.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
