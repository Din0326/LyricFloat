"""Recognition worker for the L2 spike: 16 kHz mono int16 PCM on stdin, JSON on stdout.

Runs in its own process (SPEC: L2 stays out of the main process's CPU and
memory budget). Audio is only held in memory.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np


def main() -> None:
    model_name = sys.argv[1] if len(sys.argv) > 1 else "base"
    models_dir = sys.argv[2] if len(sys.argv) > 2 else None
    language = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "auto" else None
    prompt = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None  # expected lyrics, to bias decoding
    pcm = sys.stdin.buffer.read()
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 0:
        audio = audio * (0.9 / peak)  # players often run quiet; whisper prefers full scale

    t0 = time.perf_counter()
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=models_dir, cpu_threads=max(1, (os.cpu_count() or 2) // 2))
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=False,  # singing over music trips the speech VAD
        condition_on_previous_text=False,
        beam_size=1,
        initial_prompt=prompt,
    )
    out = []
    for seg in segments:
        out.append(
            {
                "start": seg.start,
                "end": seg.end,
                "words": [{"start": w.start, "end": w.end, "text": w.word, "p": w.probability} for w in (seg.words or [])],
            }
        )
    t_run = time.perf_counter() - t1
    json.dump(
        {"language": info.language, "language_p": info.language_probability, "peak": peak, "load_s": t_load, "run_s": t_run, "segments": out},
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
