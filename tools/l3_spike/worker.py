"""L3 spike worker: vocal separation (Demucs) and forced alignment (stable-ts).

stdin: one JSON header line, then 16-bit PCM (44.1 kHz stereo).
stdout: JSON with per-line aligned times, word timings and the cost of each step.
Audio stays in memory. Runs in its own process, like L3 will (SPEC: subprocess).
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    header = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    pcm = sys.stdin.buffer.read()
    threads = header.get("threads") or max(1, (os.cpu_count() or 2) // 2)
    models_dir = header["models_dir"]
    os.environ.setdefault("TORCH_HOME", os.path.join(models_dir, "torch"))

    import numpy as np
    import torch

    torch.set_num_threads(threads)
    rate, channels = header["rate"], header["channels"]
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32).reshape(-1, channels).T / 32768.0
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 0:
        audio = audio * (0.9 / peak)
    if audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)
    out: dict = {"seconds": audio.shape[1] / rate, "peak": peak, "threads": threads}

    import julius

    def to_16k_mono(wav) -> np.ndarray:
        mono = torch.as_tensor(wav).mean(0)
        return julius.resample_frac(mono, rate, 16000).numpy().astype(np.float32)

    vocals = None
    if header.get("separate", True):
        t0 = time.perf_counter()
        from demucs.apply import apply_model
        from demucs.pretrained import get_model

        model = get_model("htdemucs")
        model.eval()
        out["demucs_load_s"] = time.perf_counter() - t0
        t1 = time.perf_counter()
        wav = torch.as_tensor(audio)
        if model.samplerate != rate:
            wav = julius.resample_frac(wav, rate, model.samplerate)
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            sources = apply_model(model, ((wav - mean) / std)[None], device="cpu", shifts=0, split=True, overlap=0.25, progress=False)[0]
        vocals_full = sources[model.sources.index("vocals")] * std + mean
        out["demucs_s"] = time.perf_counter() - t1
        rate_v = model.samplerate
        mono = vocals_full.mean(0)
        vocals = julius.resample_frac(mono, rate_v, 16000).numpy().astype(np.float32)

    import stable_whisper

    t2 = time.perf_counter()
    whisper = stable_whisper.load_faster_whisper(header.get("model", "base"), device="cpu", compute_type="int8", download_root=models_dir, cpu_threads=threads)
    out["whisper_load_s"] = time.perf_counter() - t2
    text = "\n".join(header["lines"])
    results = {}
    for label, clip in (("vocals", vocals), ("mix", to_16k_mono(audio) if header.get("mix", True) else None)):
        if clip is None:
            continue
        t3 = time.perf_counter()
        aligned = whisper.align(clip, text, language=header["language"], original_split=True, verbose=None)
        took = time.perf_counter() - t3
        segments = [
            {
                "start": s.start,
                "end": s.end,
                "words": [{"start": w.start, "end": w.end, "chars": len(w.word.strip()), "p": w.probability} for w in s.words],
            }
            for s in aligned.segments
        ]
        results[label] = {"align_s": took, "segments": segments}
    out["results"] = results
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
