"""Regenerate src/lyricfloat/align/runtime_lock.json: what the portable build downloads
to run L3 (SPEC v1.21), each file pinned by version and SHA-256.

    .venv/Scripts/python tools/lock_l3.py

Resolves the packages with pip for CPython 3.12 on 64-bit Windows (run it on such
a machine), takes pip's wheel from PyPI's JSON API and hashes python.org's
embeddable zip. All network access is Python (urllib / pip).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "src" / "lyricfloat" / "align" / "runtime_lock.json"
PYTHON_VERSION = "3.12.10"
PACKAGES = ["torch", "demucs", "stable-ts", "faster-whisper"]
BUILD = ["setuptools", "wheel"]  # stable-ts and openai-whisper ship as source only
USER_AGENT = "LyricFloat-lock/1 (personal use)"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def resolve(names: list[str]) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet", "--report", str(report), *names],
            check=True,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
    out = []
    for item in data["install"]:
        info = item["download_info"]
        hashes = info.get("archive_info", {}).get("hashes", {})
        out.append(
            {
                "name": item["metadata"]["name"],
                "version": item["metadata"]["version"],
                "file": info["url"].rsplit("/", 1)[-1],
                "sha256": hashes["sha256"],
            }
        )
    return sorted(out, key=lambda p: p["name"].lower())


def main() -> None:
    if sys.version_info[:2] != (3, 12) or sys.maxsize < 2**32:
        raise SystemExit("run this with 64-bit CPython 3.12 (the runtime's version)")
    embed_url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
    embed = fetch(embed_url)
    pip_info = json.loads(fetch("https://pypi.org/pypi/pip/json"))
    pip_wheel = next(f for f in pip_info["urls"] if f["packagetype"] == "bdist_wheel")
    build = resolve(BUILD)
    packages = [p for p in resolve(PACKAGES + BUILD) if p["name"].lower() not in {b.lower() for b in BUILD}]
    lock = {
        "python": {"version": PYTHON_VERSION, "url": embed_url, "sha256": hashlib.sha256(embed).hexdigest()},
        "pip": {"version": pip_info["info"]["version"], "url": pip_wheel["url"], "sha256": pip_wheel["digests"]["sha256"]},
        "build": build,
        "packages": packages,
    }
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"{LOCK.relative_to(ROOT)}: Python {PYTHON_VERSION}, pip {lock['pip']['version']}, {len(build)} build + {len(packages)} packages")


if __name__ == "__main__":
    main()
