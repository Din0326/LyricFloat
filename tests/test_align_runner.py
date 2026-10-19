"""L3 job runner: low-priority worker process, memory gate, recording always deleted. Fake workers only."""

import json
import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from lyricfloat.align import runner as runner_module
from lyricfloat.align.anchor import Serving
from lyricfloat.align.recorder import Recording
from lyricfloat.align.runner import AlignJob, AlignRunner, run_worker, source_runtime
from lyricfloat.models import LyricLine, TrackInfo

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "Lights", 10.0)

ECHO = """
import json, sys
job = json.loads(sys.stdin.read())
sys.stderr.write("noise\\n" * 20000)  # far more than a pipe buffer
words = [{"start": i * 0.1, "end": i * 0.1 + 0.1, "text": "w", "p": 0.9} for i in range(5000)]
print(json.dumps({"ok": True, "segments": [{"start": 1.0, "end": 2.0, "words": words}], "timings": {"align_s": 0.1}, "lines": job["lines"]}))
"""
CRASH = "import sys; sys.stderr.write('boom happened\\n'); sys.exit(3)"
SLOW = "import time; time.sleep(30)"


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def script(tmp_path, body, name="worker.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def job(tmp_path, name="rec-test.pcm"):
    pcm = tmp_path / name
    pcm.write_bytes(b"\0" * 400)
    rec = Recording(TRACK, pcm, 0.5, 10.0)
    return AlignJob(rec, (LyricLine(1.0, "line"),), ("line",), "en")


def wait_for(app, condition, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return condition()


def test_large_output_does_not_block(tmp_path):
    result = run_worker(Path(sys.executable), {"lines": ["a"]}, script(tmp_path, ECHO))
    assert result["ok"] and len(result["segments"][0]["words"]) == 5000


def test_crash_is_reported_with_its_last_line(tmp_path):
    result = run_worker(Path(sys.executable), {}, script(tmp_path, CRASH))
    assert not result["ok"] and "exit code 3" in result["error"] and "boom happened" in result["error"]


def test_hung_worker_is_killed(tmp_path):
    result = run_worker(Path(sys.executable), {}, script(tmp_path, SLOW), timeout=1)
    assert not result["ok"] and "timed out" in result["error"]


def ready_runner(tmp_path, body):
    """A runner whose models were prepared."""
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, body))
    r.models_ready = True
    return r


def test_runner_aligns_and_deletes_the_recording(app, tmp_path):
    r = ready_runner(tmp_path, ECHO)
    r.free_memory = lambda: 8 * 2**30
    got = []
    r.finished.connect(got.append)
    j = job(tmp_path)
    r.submit(j)
    assert wait_for(app, lambda: got)
    assert got[0].ok and got[0].job is j and got[0].segments
    assert not j.recording.path.exists() and r.pending == 0


def test_failed_run_still_deletes_the_recording(app, tmp_path):
    r = ready_runner(tmp_path, CRASH)
    r.free_memory = lambda: 8 * 2**30
    got = []
    r.finished.connect(got.append)
    j = job(tmp_path)
    r.submit(j)
    assert wait_for(app, lambda: got)
    assert not got[0].ok and not j.recording.path.exists()


def test_short_memory_waits(app, tmp_path):
    r = ready_runner(tmp_path, ECHO)
    r.free_memory = lambda: 1 * 2**30
    j = job(tmp_path)
    r.submit(j)
    assert r.pending == 1 and r._retry.isActive() and j.recording.path.exists()
    r.clear()
    assert r.pending == 0 and not j.recording.path.exists()


def test_without_a_runtime_recordings_are_dropped(app, tmp_path):
    r = AlignRunner(tmp_path / "models", None)
    j = job(tmp_path)
    r.submit(j)
    assert r.pending == 0 and not j.recording.path.exists()


def test_source_runtime_needs_the_packages(monkeypatch):
    found = source_runtime()
    assert found is None or found == Path(sys.executable)
    monkeypatch.setattr(runner_module.importlib.util, "find_spec", lambda name: None)
    assert source_runtime() is None


# -- models: prepared once, then offline (SPEC v1.85) -------------------------------------

ENV = """
import json, os, sys
sys.stdin.readline()
keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_IMPLICIT_TOKEN")
print(json.dumps({"ok": True, "env": {k: os.environ.get(k) for k in keys}}), flush=True)
"""
PREPARING = """
import json, sys
job = json.loads(sys.stdin.read())
if job.get("prepare"):
    open(sys.argv[0] + ".job", "w").write(json.dumps(job))
    print(json.dumps({"ok": True, "downloaded": ["Demucs htdemucs.yaml"]}))
else:
    print(json.dumps({"ok": True, "segments": [], "timings": {}}))
"""
CANNOT_DOWNLOAD = """
import json, sys
sys.stdin.read()
print(json.dumps({"ok": False, "error": "LocalEntryNotFoundError: no connection"}))
"""
MODELS_GONE = """
import json, sys
sys.stdin.read()
print(json.dumps({"ok": False, "error": "ModelsMissing: Whisper base is not in models", "models_missing": True}))
"""


def serve_env(worker, models_dir):
    replies = []
    serving = Serving(Path(sys.executable), worker, models_dir, replies.append)
    deadline = time.monotonic() + 20
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    serving.close()
    return replies[0]["env"]


def test_only_the_prepare_job_may_go_online(tmp_path, monkeypatch):
    """Whole-song jobs and the start finder's worker are offline; no run sends the user's token."""
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(key, raising=False)
    worker = script(tmp_path, ENV)
    align = run_worker(Path(sys.executable), {"lines": ["a"]}, worker)["env"]
    serve = serve_env(worker, tmp_path / "models")
    prepare = run_worker(Path(sys.executable), {"prepare": True}, worker)["env"]
    assert align["HF_HUB_OFFLINE"] == "1" and serve["HF_HUB_OFFLINE"] == "1"
    assert prepare["HF_HUB_OFFLINE"] is None
    assert all(env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1" for env in (align, serve, prepare))
    # the user's own offline setting would stop the first download
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    prepare = run_worker(Path(sys.executable), {"prepare": True}, worker)["env"]
    assert prepare["HF_HUB_OFFLINE"] is None and prepare["TRANSFORMERS_OFFLINE"] is None


def test_jobs_wait_until_the_models_are_prepared(app, tmp_path):
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, PREPARING))
    r.free_memory = lambda: 8 * 2**30
    prepared, got = [], []
    r.prepared.connect(lambda *args: prepared.append(args))
    r.finished.connect(got.append)
    j = job(tmp_path)
    r.submit(j)
    assert r.pending == 1 and j.recording.path.exists()  # held: the models are not known to be there
    r.prepare()
    r.prepare()  # one at a time
    assert r.preparing
    assert wait_for(app, lambda: got)
    assert prepared == [(True, True, "")] and r.models_ready and not r.preparing
    assert got[0].ok and not j.recording.path.exists()
    sent = json.loads((tmp_path / "worker.py.job").read_text(encoding="utf-8"))
    assert sent == {"prepare": True, "model": "base", "models_dir": str(tmp_path / "models")}


def test_a_failed_prepare_drops_the_waiting_recordings(app, tmp_path):
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, CANNOT_DOWNLOAD))
    prepared = []
    r.prepared.connect(lambda *args: prepared.append(args))
    j = job(tmp_path)
    r.submit(j)
    r.prepare()
    assert wait_for(app, lambda: prepared)
    assert prepared == [(False, False, "LocalEntryNotFoundError: no connection")]
    assert not r.models_ready and not r.preparing and r.pending == 0 and not j.recording.path.exists()


def test_without_a_runtime_nothing_is_prepared(tmp_path):
    r = AlignRunner(tmp_path / "models", None)
    r.prepare()
    assert not r.preparing


def test_models_gone_mid_way_hold_the_next_jobs(app, tmp_path):
    r = ready_runner(tmp_path, MODELS_GONE)
    r.free_memory = lambda: 8 * 2**30
    got = []
    r.finished.connect(got.append)
    first, second = job(tmp_path, "rec-1.pcm"), job(tmp_path, "rec-2.pcm")
    r.submit(first)
    r.submit(second)
    assert wait_for(app, lambda: got)
    assert not got[0].ok and got[0].models_missing and "ModelsMissing" in got[0].error
    assert not r.models_ready and not first.recording.path.exists()
    assert r.pending == 1 and second.recording.path.exists()  # waits for the models to be prepared again
    r.clear()
