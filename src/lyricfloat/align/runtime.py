"""The L3 inference runtime for the portable build (SPEC v1.21).

The packaged app has no PyTorch, so turning L3 on downloads a separate runtime
into %LOCALAPPDATA%\\LyricFloat\\runtime: python.org's embeddable CPython and the
packages in runtime_lock.json. Every download is pinned by SHA-256 (pip runs
with --require-hashes, and the two source-only packages build without build
isolation from hash-checked setuptools/wheel). Network access is Python only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
LOCK_FILE = HERE / "runtime_lock.json"
WORKER = HERE / "worker.py"
READY = "ready.json"
USER_AGENT = "LyricFloat-runtime/1 (personal use)"
CREATE_NO_WINDOW = 0x08000000
CHUNK = 1 << 20

Progress = Callable[[str], None]


class RuntimeError_(Exception):
    """Installing the runtime failed; the message is shown to the user."""


def load_lock(path: Path = LOCK_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lock_id(lock: dict) -> str:
    return hashlib.sha256(json.dumps(lock, sort_keys=True).encode("utf-8")).hexdigest()


def python_of(root: Path) -> Path:
    return root / "python" / "python.exe"


def installed_python(root: Path, lock: dict | None = None) -> Path | None:
    """The runtime's interpreter when it is complete and matches this version's lock."""
    try:
        marker = json.loads((root / READY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lock = lock or load_lock()
    if marker.get("lock") != lock_id(lock) or not python_of(root).exists() or not (root / WORKER.name).exists():
        return None
    return python_of(root)


def requirement_lines(entries: list[dict]) -> list[str]:
    return [f"{e['name']}=={e['version']} --hash=sha256:{e['sha256']}" for e in entries]


def enable_site(python_dir: Path) -> None:
    """The embeddable Python ignores site-packages until its ._pth file says `import site`."""
    pth = next(python_dir.glob("python*._pth"))
    lines = pth.read_text(encoding="utf-8").splitlines()
    lines = ["import site" if line.strip() == "#import site" else line for line in lines]
    if "import site" not in lines:
        lines.append("import site")
    if "Lib\\site-packages" not in lines:
        lines.insert(len(lines) - 1, "Lib\\site-packages")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download(url: str, sha256: str, target: Path, progress: Progress) -> Path:
    """Stream `url` to `target`, refusing it unless its SHA-256 matches."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    got = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as f:
        total = int(response.headers.get("Content-Length") or 0)
        while chunk := response.read(CHUNK):
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if total and got % (8 * CHUNK) < CHUNK:
                progress(f"下載 {target.name}：{got // 2**20} / {total // 2**20} MB")
    if digest.hexdigest() != sha256:
        target.unlink(missing_ok=True)
        raise RuntimeError_(f"{target.name} 的檢查碼不符，已刪除（可能是下載不完整或檔案被竄改）")
    return target


class RuntimeInstaller:
    def __init__(self, root: Path, progress: Progress = lambda message: None, lock: dict | None = None,
                 fetch: Callable[[str, str, Path, Progress], Path] = download, run: Callable | None = None) -> None:
        self.root = root
        self.progress = progress
        self.lock = lock or load_lock()
        self.fetch = fetch
        self.run = run or self._run

    def install(self) -> Path:
        """Blocking (run it on a thread). Returns the runtime's python.exe."""
        root, lock = self.root, self.lock
        downloads = root / "downloads"
        python_dir = root / "python"
        (root / READY).unlink(missing_ok=True)
        if python_dir.exists():
            shutil.rmtree(python_dir)
        downloads.mkdir(parents=True, exist_ok=True)
        try:
            self.progress("下載 Python 執行環境…")
            embed = self.fetch(lock["python"]["url"], lock["python"]["sha256"], downloads / "python-embed.zip", self.progress)
            with zipfile.ZipFile(embed) as z:
                z.extractall(python_dir)
            enable_site(python_dir)
            pip_wheel = downloads / lock["pip"]["url"].rsplit("/", 1)[-1]
            self.fetch(lock["pip"]["url"], lock["pip"]["sha256"], pip_wheel, self.progress)
            build = downloads / "build.txt"
            build.write_text("\n".join(requirement_lines(lock["build"])) + "\n", encoding="utf-8")
            packages = downloads / "packages.txt"
            packages.write_text("\n".join(requirement_lines(lock["packages"])) + "\n", encoding="utf-8")
            pip = [str(python_of(root)), str(pip_wheel / "pip"), "install", "--require-hashes", "--disable-pip-version-check",
                   "--no-warn-script-location", "--progress-bar", "off"]
            self.progress("安裝建置工具…")
            self.run([*pip, "-r", str(build)])
            self.progress("下載並安裝推論套件（約 250 MB，需要幾分鐘）…")
            self.run([*pip, "--no-build-isolation", "-r", str(packages)])
            shutil.copy2(WORKER, root / WORKER.name)
            (root / READY).write_text(json.dumps({"lock": lock_id(lock)}), encoding="utf-8")
        finally:
            shutil.rmtree(downloads, ignore_errors=True)
        self.progress("執行環境已安裝。")
        log.info("L3 runtime installed in %s", root)
        return python_of(root)

    def _run(self, command: list[str]) -> None:
        """One pip run; its output (stdout and stderr merged into one pipe) is passed on line by line."""
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        tail: list[str] = []
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail = (tail + [line])[-5:]
            if line.startswith(("Collecting", "Downloading", "Building", "Installing", "Successfully")):
                self.progress(line[:120])
        if proc.wait() != 0:
            raise RuntimeError_("安裝套件失敗：" + (tail[-1] if tail else f"exit code {proc.returncode}"))


def worker_for(python: Path) -> Path:
    """The worker script that goes with an interpreter: the runtime's copy, or this package's.
    A copy from an older version is replaced, so the runtime gains new worker modes (SPEC v1.36)."""
    copy = python.parent.parent / WORKER.name
    if not copy.exists():
        return WORKER
    try:
        if copy.read_bytes() != WORKER.read_bytes():
            shutil.copy2(WORKER, copy)
            log.info("L3 worker in the runtime updated")
    except OSError as exc:
        log.warning("could not update the runtime's worker: %s", exc)
    return copy


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
