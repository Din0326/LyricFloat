"""Portable L3 runtime (SPEC v1.21): hash-checked downloads, the install steps, enabling it. No network."""

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from lyricfloat.align import runtime as runtime_module
from lyricfloat.align.coordinator import STATUS_DOWNLOAD, STATUS_READY, L3Coordinator
from lyricfloat.align.runtime import (
    READY,
    RuntimeError_,
    RuntimeInstaller,
    download,
    enable_site,
    installed_python,
    lock_id,
    requirement_lines,
    worker_for,
)

PTH = "python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n"
LOCK = {
    "python": {"version": "3.12.10", "url": "https://example.invalid/python-embed.zip", "sha256": "x"},
    "pip": {"version": "26.2.1", "url": "https://example.invalid/pip-26.2.1-py3-none-any.whl", "sha256": "y"},
    "build": [{"name": "setuptools", "version": "84.0.0", "file": "s.whl", "sha256": "aa"}],
    "packages": [{"name": "torch", "version": "2.14.1", "file": "t.whl", "sha256": "bb"}],
}


def test_enable_site_turns_on_site_packages(tmp_path):
    (tmp_path / "python312._pth").write_text(PTH, encoding="utf-8")
    enable_site(tmp_path)
    lines = (tmp_path / "python312._pth").read_text(encoding="utf-8").splitlines()
    assert "import site" in lines and "#import site" not in lines
    assert lines.index("Lib\\site-packages") < lines.index("import site")
    enable_site(tmp_path)  # idempotent
    assert (tmp_path / "python312._pth").read_text(encoding="utf-8").count("Lib\\site-packages") == 1


def test_requirements_pin_version_and_hash():
    assert requirement_lines(LOCK["packages"]) == ["torch==2.14.1 --hash=sha256:bb"]


class FakeResponse(io.BytesIO):
    headers = {"Content-Length": "11"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_keeps_only_matching_files(tmp_path, monkeypatch):
    body = b"hello world"
    monkeypatch.setattr(runtime_module.urllib.request, "urlopen", lambda request, timeout: FakeResponse(body))
    good = download("https://example.invalid/a", hashlib.sha256(body).hexdigest(), tmp_path / "a.bin", lambda m: None)
    assert good.read_bytes() == body
    with pytest.raises(RuntimeError_):
        download("https://example.invalid/b", "0" * 64, tmp_path / "b.bin", lambda m: None)
    assert not (tmp_path / "b.bin").exists()


def fake_fetch(url, sha256, target, progress):
    if target.suffix == ".zip":
        with zipfile.ZipFile(target, "w") as z:
            z.writestr("python312._pth", PTH)
            z.writestr("python.exe", b"stub")
    else:
        target.write_bytes(b"wheel")
    return target


def test_install_runs_hash_checked_pip_twice_and_marks_ready(tmp_path):
    commands = []
    installer = RuntimeInstaller(tmp_path, lock=LOCK, fetch=fake_fetch, run=commands.append)
    python = installer.install()
    assert python == tmp_path / "python" / "python.exe"
    build, packages = commands
    assert "--require-hashes" in build and "--no-build-isolation" not in build
    assert "--require-hashes" in packages and "--no-build-isolation" in packages
    assert str(tmp_path / "downloads" / "pip-26.2.1-py3-none-any.whl" / "pip") in build  # pip runs from its wheel
    assert not (tmp_path / "downloads").exists()  # downloads are cleaned up
    assert installed_python(tmp_path, LOCK) == python
    assert worker_for(python) == tmp_path / "worker.py"


def test_a_failed_install_is_not_marked_ready(tmp_path):
    def failing(command):
        raise RuntimeError_("pip broke")

    with pytest.raises(RuntimeError_):
        RuntimeInstaller(tmp_path, lock=LOCK, fetch=fake_fetch, run=failing).install()
    assert installed_python(tmp_path, LOCK) is None and not (tmp_path / "downloads").exists()


def test_a_runtime_for_another_lock_does_not_count(tmp_path):
    RuntimeInstaller(tmp_path, lock=LOCK, fetch=fake_fetch, run=lambda c: None).install()
    newer = {**LOCK, "packages": [{**LOCK["packages"][0], "version": "2.15.0"}]}
    assert lock_id(newer) != json.loads((tmp_path / READY).read_text())["lock"]
    assert installed_python(tmp_path, newer) is None


# -- coordinator ------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


class Runner:
    def __init__(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class Sig(QObject):
            finished = pyqtSignal(object)

        self._sig = Sig()
        self.finished = self._sig.finished
        self.python = None
        self.worker = None
        self.models_dir = None

    def clear(self):
        pass


class Recorder:
    def __init__(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class Sig(QObject):
            recorded = pyqtSignal(object)
            dropped = pyqtSignal(str)

        self._sig = Sig()
        self.recorded = self._sig.recorded
        self.dropped = self._sig.dropped
        self.track = None
        self.active = False

    def cancel(self):
        pass


def wait_for(app, condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return condition()


def test_portable_build_installs_the_runtime_when_turned_on(app, tmp_path):
    def installer(root, progress):
        return RuntimeInstaller(root, progress, lock=LOCK, fetch=fake_fetch, run=lambda c: None)

    c = L3Coordinator(None, Recorder(), Runner(), runtime_root=tmp_path, frozen=True, installer=installer)
    notices = []
    c.notice.connect(lambda title, text: notices.append(text))
    assert c.status_text() == STATUS_DOWNLOAD
    c.set_enabled(True)
    assert wait_for(app, lambda: c.runtime_ready)
    assert c.runner.worker == tmp_path / "worker.py" and c.status_text() == STATUS_READY
    assert "下載執行環境" in notices[0] and "已就緒" in notices[-1]


def test_a_failed_runtime_install_is_reported(app, tmp_path):
    def installer(root, progress):
        def broken(command):
            raise RuntimeError_("pip broke")

        return RuntimeInstaller(root, progress, lock=LOCK, fetch=fake_fetch, run=broken)

    c = L3Coordinator(None, Recorder(), Runner(), runtime_root=tmp_path, frozen=True, installer=installer)
    notices = []
    c.notice.connect(lambda title, text: notices.append(text))
    c.set_enabled(True)
    assert wait_for(app, lambda: len(notices) == 2)
    assert not c.runtime_ready and not c.installing and "pip broke" in notices[-1]


def test_running_from_source_never_downloads(app, tmp_path):
    c = L3Coordinator(None, Recorder(), Runner(), runtime_root=tmp_path, frozen=False, installer=lambda *a: pytest.fail("installed"))
    c.set_enabled(True)
    assert not c.installing
