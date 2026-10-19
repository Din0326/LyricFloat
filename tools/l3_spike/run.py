"""L3 spike: how long do vocal separation and forced alignment take on this CPU, and how
well do the aligned line starts agree with the LRC?

Usage (synced-lyrics songs playing in the player):
    .venv/Scripts/python tools/l3_spike/run.py --app Spotify.exe

Waits for the next song to start, records it whole from the player's process only
(memory only, never written to disk), then runs the worker in a subprocess on the
separated vocals and, for comparison, on the plain mix. Prints numbers only: no
lyric text, no recognized text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "l2_spike"))

from capture import Capture, root_pid  # noqa: E402
from common import current, whisper_language  # noqa: E402  (shared with the L2 spike)

from lyricfloat.app import SOURCE_FACTORIES  # noqa: E402
from lyricfloat.lyrics.chinese import ChineseConverter  # noqa: E402
from lyricfloat.lyrics.pipeline import OpenCCPipeline  # noqa: E402
from lyricfloat.lyrics.service import LyricResolver  # noqa: E402
from lyricfloat.media import smtc  # noqa: E402
from lyricfloat.media.timeline import anchor_from_timeline  # noqa: E402
from lyricfloat.models import ResultKind  # noqa: E402

RATE, CHANNELS = 44100, 2


def position(session) -> tuple[float, float]:
    """(position now, duration) from the player's timeline."""
    raw = smtc.read_timeline(session)
    anchor = anchor_from_timeline(raw, time.time(), time.monotonic())
    return anchor.position + ((time.monotonic() - anchor.at) if anchor.playing else 0.0), raw.duration


def run_worker(header: dict, pcm: bytes) -> tuple[dict, dict]:
    worker = Path(__file__).resolve().parent / "worker.py"
    t0 = time.perf_counter()
    env = {**os.environ, "TQDM_DISABLE": "1"}  # no progress bars on stderr
    proc = subprocess.Popen(
        [sys.executable, str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    ps = psutil.Process(proc.pid)

    def feed():
        proc.stdin.write((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.write(pcm)
        proc.stdin.close()

    # Both output pipes are drained while the worker runs: a full pipe would block it forever.
    outputs: dict[str, bytes] = {}

    def drain(name, stream):
        outputs[name] = stream.read()

    readers = [threading.Thread(target=drain, args=(n, s), daemon=True) for n, s in (("out", proc.stdout), ("err", proc.stderr))]
    for thread in (threading.Thread(target=feed, daemon=True), *readers):
        thread.start()
    peak, cpu = 0, 0.0
    while proc.poll() is None:
        try:
            family = [ps, *ps.children(recursive=True)]
            peak = max(peak, sum(p.memory_info().rss for p in family))
            cpu = max(cpu, sum(p.cpu_times().user + p.cpu_times().system for p in family))
        except psutil.Error:
            pass
        time.sleep(0.2)
    for thread in readers:
        thread.join()
    out, err = outputs.get("out", b""), outputs.get("err", b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed ({proc.returncode}): {err[-1500:]}")
    return json.loads(out.decode("utf-8")), {"wall_s": round(time.perf_counter() - t0, 1), "cpu_s": round(cpu, 1), "peak_rss_mb": round(peak / 2**20)}


def compare(lines, segments, pos0: float, captured: float) -> dict:
    """Aligned line starts against the LRC, for lines inside the recording."""
    if len(segments) != len(lines):
        return {"error": f"{len(segments)} aligned segments for {len(lines)} lines"}
    diffs, words, probs = [], 0, []
    for line, seg in zip(lines, segments):
        if not pos0 + 1 <= line.start <= pos0 + captured - 1:
            continue
        diffs.append(pos0 + seg["start"] - line.start)
        words += len(seg["words"])
        probs += [w["p"] for w in seg["words"]]
    if not diffs:
        return {"error": "no lyric line inside the recording"}
    med = statistics.median(diffs)
    within = lambda limit: round(sum(abs(d) <= limit for d in diffs) / len(diffs), 2)  # noqa: E731
    centred = [d - med for d in diffs]
    return {
        "lines": len(diffs),
        "median_diff_s": round(med, 3),
        "median_abs_diff_s": round(statistics.median(abs(d) for d in diffs), 3),
        "within_0.3s": within(0.3),
        "within_0.5s": within(0.5),
        "within_1s": within(1.0),
        "spread_after_median_shift_p80_s": round(sorted(abs(c) for c in centred)[int(len(centred) * 0.8) - 1], 3),
        "words": words,
        "mean_word_p": round(statistics.mean(probs), 2) if probs else None,
    }


def record_song(session, app: str, limit: float) -> tuple[bytes, float, float]:
    """Record until the song ends (position jumps back or reaches the end).
    Returns (pcm, seconds recorded, song position of the first sample)."""
    cap = Capture(root_pid(app), RATE, CHANNELS)
    last = {"pos": -1.0, "first": None}

    def keep_going() -> bool:
        pos, duration = position(session)
        if last["first"] is None:
            last["first"] = (pos, time.monotonic())  # pins the recording to the song's timeline
        ended = pos < last["pos"] - 5 or (duration > 0 and pos >= duration - 0.3)
        last["pos"] = max(last["pos"], pos)
        return not ended

    print("recording…", flush=True)
    pcm, first_at = cap.record(limit, keep_going)
    cap.close()
    if last["first"] is None:
        raise SystemExit("recording too short")
    first_pos, first_mono = last["first"]
    return pcm, len(pcm) / (2 * CHANNELS * RATE), first_pos - (first_mono - first_at)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="Spotify.exe")
    ap.add_argument("--model", default="base")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--max", type=float, default=420.0)
    ap.add_argument("--now", action="store_true", help="record from now instead of waiting for the next song")
    ap.add_argument("--songs", type=int, default=1, help="record this many songs back to back, then align them")
    ap.add_argument("--models-dir", default=str(Path.home() / "AppData" / "Local" / "LyricFloat" / "models"))
    args = ap.parse_args()

    session, track = await current(args.app)
    if not args.now:
        print("waiting for the next song to start…", flush=True)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            session, now = await current(args.app)
            if now is not None and track is not None and now.key != track.key:
                track = now
                break
        else:
            raise SystemExit("no song change within 15 minutes")

    recordings = []
    for n in range(args.songs):
        if n:
            await asyncio.sleep(1.0)  # let the player publish the next song's title
            session, track = await current(args.app)
        recordings.append((track, *record_song(session, args.app, args.max)))
        print(f"recorded song {n + 1}: {recordings[-1][2]:.0f}s", flush=True)

    resolver = LyricResolver([SOURCE_FACTORIES[n]() for n in ("lrclib", "netease")], pipeline=OpenCCPipeline())
    cc = ChineseConverter()
    for n, (track, pcm, captured, pos0) in enumerate(recordings, 1):
        result = await resolver.resolve(track)
        if result.kind is not ResultKind.SYNCED:
            print(json.dumps({"song": n, "skipped": f"no synced lyrics ({result.kind.value})"}))
            continue
        lines = [l for l in result.lyrics.lines if l.text.strip()]
        language = whisper_language(lines)
        texts = [cc.convert("t2s", l.text) if language == "zh" else l.text for l in lines]  # SPEC: align in simplified
        print(f"song {n}: {captured:.0f}s of a {track.duration:.0f}s song from {pos0:.1f}s; {len(lines)} lines from {result.candidate.source}; language {language}", flush=True)
        header = {"rate": RATE, "channels": CHANNELS, "lines": texts, "language": language, "model": args.model,
                  "threads": args.threads or None, "models_dir": args.models_dir}
        try:
            data, cost = run_worker(header, pcm)
        except RuntimeError as exc:
            print(json.dumps({"song": n, "error": str(exc)[-800:]}, ensure_ascii=False), flush=True)
            continue
        summary = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in data.items() if k != "results"}
        print(json.dumps({"song": n, **summary, **cost}))
        for label, res in data["results"].items():
            print(json.dumps({"song": n, "variant": label, "align_s": round(res["align_s"], 1), **compare(lines, res["segments"], pos0, captured)}))
    recordings.clear()  # audio goes as soon as it is aligned
    await resolver.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
