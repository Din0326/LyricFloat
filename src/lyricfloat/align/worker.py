"""L3 alignment worker: vocal separation (Demucs) and forced alignment (stable-ts).

Runs in its own low-priority process, possibly under a separately downloaded
Python runtime, so it imports nothing from lyricfloat. Reads one JSON job line on
stdin, writes one JSON result on stdout:

    job:    {"pcm_path", "rate", "channels", "lines", "language", "model",
             "threads", "models_dir", "offset_bytes"?, "length_bytes"?}
            or  {"prepare": true, "model", "models_dir"}
    result: {"ok": true, "segments": [{"start", "end", "words": [{"start", "end", "text", "p"}]}],
             "timings": {...}}   or  {"ok": false, "error": "...", "models_missing"?: true}
            prepare: {"ok": true, "downloaded": [names]}

`offset_bytes` and `length_bytes` take a part of the recording (SPEC v1.36: an MV's
intro is skipped, and clips are read from a recording still being written).

{"serve": true, "model", "threads", "models_dir"} keeps the models loaded instead:
the worker answers {"ok": true, "ready": true}, then one result line per job line
until stdin closes (SPEC v1.36, finding an MV's singing start while it plays).

Only the prepare job goes online (SPEC v1.85): it downloads the models missing from
models_dir. Every other run reads them from there alone and answers "models_missing"
when one is gone.

The PCM file is only read here; the app deletes it afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Models pinned to Hugging Face commits, like the runtime's packages (SPEC v1.85): the ones
# measured since 2026-10-07, still the latest on 2026-10-19.
WHISPER_REVISIONS = {"base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"}  # Systran/faster-whisper-<model>
DEMUCS = ("adefossez/HTDemucs", "htdemucs", "cbc8a9b1a87023b7fd74e7b3412e6321c0eab003")  # repo, bag, commit


class ModelsMissing(Exception):
    """A model is not in models_dir, and this run may not download it."""


def fetch_models(job: dict, online: bool = False) -> dict:
    """The models' files in models_dir. Offline (every run but prepare) a missing model raises
    ModelsMissing; online, what is missing is downloaded, never with the user's token."""
    import yaml
    from faster_whisper.utils import download_model
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    cache = job["models_dir"]
    downloaded: list[str] = []

    def local_or_download(name: str, get):
        try:
            return get(True)
        except LocalEntryNotFoundError as exc:
            if not online:
                raise ModelsMissing(f"{name} is not in the models folder") from exc
        downloaded.append(name)
        return get(False)

    model = job.get("model", "base")
    whisper = local_or_download(f"Whisper {model}", lambda local: download_model(
        model, cache_dir=cache, revision=WHISPER_REVISIONS.get(model), local_files_only=local, use_auth_token=False))
    repo, bag_name, revision = DEMUCS

    def demucs_file(filename: str) -> str:
        return local_or_download(f"Demucs {filename}", lambda local: hf_hub_download(
            repo, filename, revision=revision, cache_dir=cache, local_files_only=local, token=False))

    with open(demucs_file(f"{bag_name}.yaml"), encoding="utf-8") as f:
        bag = yaml.safe_load(f)
    weights = [demucs_file(f"{sig}.safetensors") for sig in bag["models"]]
    return {"whisper": whisper, "demucs_bag": bag, "demucs_weights": weights, "downloaded": downloaded}


def _load_models(job: dict):
    """From models_dir only (SPEC v1.85). Demucs is put together the way its own Hugging Face
    loader does, without that loader's unpinned lookup and its fallback download."""
    import torch
    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model
    import stable_whisper

    threads = max(1, int(job.get("threads") or 2))
    torch.set_num_threads(threads)
    files = fetch_models(job)
    bag = files["demucs_bag"]
    separator = BagOfModels([load_safetensors_model(path) for path in files["demucs_weights"]], bag.get("weights"), bag.get("segment"))
    separator.eval()
    whisper = stable_whisper.load_faster_whisper(files["whisper"], device="cpu", compute_type="int8", cpu_threads=threads)
    return separator, whisper


def read_pcm(path: str, channels: int, offset_bytes: int = 0, length_bytes: int | None = None):
    """16-bit PCM as float channels x samples, from `offset_bytes` for `length_bytes` (whole frames)."""
    import numpy as np

    block = 2 * channels
    offset = max(0, int(offset_bytes)) // block * block
    count = -1 if length_bytes is None else max(0, int(length_bytes)) // block * channels
    pcm = np.fromfile(path, dtype=np.int16, count=count, offset=offset)
    pcm = pcm[: pcm.size - pcm.size % channels]
    return pcm.astype(np.float32).reshape(-1, channels).T / 32768.0


def align(job: dict, models=None) -> dict:
    """`models`: already loaded (serve mode); loaded here otherwise."""
    import julius
    import numpy as np
    import torch
    from demucs.apply import apply_model

    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    separator, whisper = models or _load_models(job)
    timings["load_s"] = time.perf_counter() - t0

    rate, channels = int(job["rate"]), int(job["channels"])
    audio = read_pcm(job["pcm_path"], channels, job.get("offset_bytes", 0), job.get("length_bytes"))
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak <= 0:
        return {"ok": False, "error": "silent recording"}
    audio *= 0.9 / peak
    if audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)

    t1 = time.perf_counter()
    wav = torch.from_numpy(audio)
    del audio
    if separator.samplerate != rate:
        wav = julius.resample_frac(wav, rate, separator.samplerate)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        sources = apply_model(separator, ((wav - mean) / std)[None], device="cpu", shifts=0, split=True, overlap=0.25, progress=False)[0]
    vocals = sources[separator.sources.index("vocals")].mean(0) * std + mean
    del sources, wav
    vocals = julius.resample_frac(vocals, separator.samplerate, 16000).numpy().astype(np.float32)
    timings["separate_s"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    lines = job["lines"]
    text = "\n".join(line for line in lines if _chars(line))
    aligned = whisper.align(vocals, text, language=job["language"], original_split=True, verbose=None)
    timings["align_s"] = time.perf_counter() - t2
    words = [
        {"start": w.start, "end": w.end, "text": w.word, "p": w.probability}
        for s in aligned.segments
        for w in s.words
    ]
    return {"ok": True, "segments": split_by_lines(lines, words), "timings": timings}


def _chars(text: str) -> int:
    """Characters that are sung: letters, digits, CJK (not spaces, punctuation or symbols)."""
    return sum(1 for ch in text if ch.isalnum())


def split_by_lines(lines: list[str], words: list[dict]) -> list[dict]:
    """One segment per input line, whatever segments the aligner returned.

    The aligner may merge, split or drop lines (blank lines, symbol-only lines),
    so its words are handed back to the lines by counting sung characters: each
    line takes words until it has as many as its own text. A line with nothing to
    sing gets no words and no start.
    """
    out = []
    k = 0
    for line in lines:
        need = _chars(line)
        taken: list[dict] = []
        got = 0
        if need:
            while k < len(words) and got < need:
                got += _chars(words[k]["text"])
                taken.append(words[k])
                k += 1
            while k < len(words) and not _chars(words[k]["text"]):  # trailing punctuation stays with its line
                taken.append(words[k])
                k += 1
        sung = [w for w in taken if _chars(w["text"])]
        out.append(
            {
                "start": sung[0]["start"] if sung else None,
                "end": sung[-1]["end"] if sung else None,
                "words": taken,
            }
        )
    return out


def _failure(exc: Exception) -> dict:
    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(exc, ModelsMissing):
        result["models_missing"] = True  # the app prepares the models again (SPEC v1.85)
    return result


def _attempt(run, *args) -> dict:
    try:
        return run(*args)
    except Exception as exc:  # reported to the app, which logs it and moves on
        return _failure(exc)


def serve(job: dict, read_line, write_line, load=_load_models, run=align) -> None:
    """Keep the models loaded and answer one job per line until the input ends."""
    try:
        models = load(job)
    except Exception as exc:
        write_line(_failure(exc))
        return
    write_line({"ok": True, "ready": True})
    while True:
        raw = read_line()
        if not raw:
            return
        try:
            request = json.loads(raw)
        except ValueError:
            write_line({"ok": False, "error": "unreadable job"})
            continue
        write_line(_attempt(run, request, models))


def _write_line(result: dict) -> None:
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> None:
    job = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    os.environ.setdefault("TORCH_HOME", os.path.join(job["models_dir"], "torch"))
    if job.get("serve"):
        serve(job, lambda: sys.stdin.buffer.readline().decode("utf-8"), _write_line)
        return
    if job.get("prepare"):

        def prepare(j: dict) -> dict:
            return {"ok": True, "downloaded": fetch_models(j, online=True)["downloaded"]}

        result = _attempt(prepare, job)
    else:
        result = _attempt(align, job)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
