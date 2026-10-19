"""L3 job runner: low-priority worker process, memory gate, recording always deleted. Fake workers only."""

import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from lyricfloat.align import runner as runner_module
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


def job(tmp_path):
    pcm = tmp_path / "rec-test.pcm"
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


def test_runner_aligns_and_deletes_the_recording(app, tmp_path):
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, ECHO))
    r.free_memory = lambda: 8 * 2**30
    got = []
    r.finished.connect(got.append)
    j = job(tmp_path)
    r.submit(j)
    assert wait_for(app, lambda: got)
    assert got[0].ok and got[0].job is j and got[0].segments
    assert not j.recording.path.exists() and r.pending == 0


def test_failed_run_still_deletes_the_recording(app, tmp_path):
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, CRASH))
    r.free_memory = lambda: 8 * 2**30
    got = []
    r.finished.connect(got.append)
    j = job(tmp_path)
    r.submit(j)
    assert wait_for(app, lambda: got)
    assert not got[0].ok and not j.recording.path.exists()


def test_short_memory_waits(app, tmp_path):
    r = AlignRunner(tmp_path / "models", Path(sys.executable), script(tmp_path, ECHO))
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
