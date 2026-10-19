"""Non-functional measurements (spec: idle CPU < 1%, playing CPU < 3%,
memory < 120 MB, 72 h without crash and memory growth < 10%).

CPU is reported two ways: share of the whole machine (what Task Manager
shows, used for the verdict) and share of one core. Memory is judged on
the private working set, Task Manager's default "Memory" column (SPEC
v1.11), at its median: the brief peak while simplified lyrics are
converted is listed but not judged. Working set and private bytes are
listed too. Samples hold numbers only.
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

TARGETS = {
    "idle_cpu_pct": 1.0,
    "playing_cpu_pct": 3.0,
    "memory_mb": 120.0,
    "soak_growth_pct": 10.0,
}


@dataclass
class PerfSample:
    t_wall: float
    elapsed: float  # seconds since the run started
    cpu_pct: float  # share of the whole machine
    cpu_core_pct: float  # share of one core
    rss_mb: float  # working set
    private_mb: float
    alive: bool = True
    pws_mb: float = 0.0  # private working set (Task Manager "Memory")


SAMPLE_FIELDS = [f.name for f in fields(PerfSample)]


def find_app_process(psutil_module) -> Any | None:
    """The running LyricFloat interpreter. A venv python.exe is a small launcher
    with the real interpreter as its child; the largest matching process is the app."""
    matches = []
    for proc in psutil_module.process_iter(["name", "cmdline", "memory_info"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            name = (proc.info.get("name") or "").lower()
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
        if "-m lyricfloat" in cmd or name == "lyricfloat.exe":
            matches.append(proc)
    if not matches:
        return None
    return max(matches, key=lambda p: p.info["memory_info"].rss if p.info.get("memory_info") else 0)


def take_sample(proc, started: float, now_wall: float, cpu_count: int) -> PerfSample:
    core = proc.cpu_percent(interval=None)
    mem = proc.memory_info()
    private = getattr(mem, "private", 0) or 0
    try:
        pws = proc.memory_full_info().uss
    except Exception:  # not readable for this process: private bytes are the closest figure
        pws = private
    return PerfSample(
        t_wall=round(now_wall, 3),
        elapsed=round(now_wall - started, 3),
        cpu_pct=round(core / max(1, cpu_count), 3),
        cpu_core_pct=round(core, 3),
        rss_mb=round(mem.rss / 1048576, 2),
        private_mb=round(private / 1048576, 2),
        pws_mb=round(pws / 1048576, 2),
    )


def write_samples(path: Path, samples: Iterable[PerfSample], meta: dict[str, str], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or not append
    with path.open("a" if append else "w", newline="", encoding="utf-8") as fh:
        if new:
            for key, value in meta.items():
                fh.write(f"# {key}={value}\n")
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_FIELDS)
        if new:
            writer.writeheader()
        for s in samples:
            writer.writerow(asdict(s))


def read_samples(path: Path) -> tuple[dict[str, str], list[PerfSample]]:
    meta: dict[str, str] = {}
    rows: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and "=" in line:
            key, value = line[2:].split("=", 1)
            meta[key] = value
        else:
            rows.append(line)
    samples = []
    for row in csv.DictReader(rows):
        samples.append(
            PerfSample(
                t_wall=float(row["t_wall"]),
                elapsed=float(row["elapsed"]),
                cpu_pct=float(row["cpu_pct"]),
                cpu_core_pct=float(row["cpu_core_pct"]),
                rss_mb=float(row["rss_mb"]),
                private_mb=float(row["private_mb"]),
                alive=row.get("alive", "True") == "True",
                pws_mb=float(row.get("pws_mb") or row["private_mb"]),
            )
        )
    return meta, samples


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(label: str, samples: Sequence[PerfSample]) -> dict[str, Any]:
    alive = [s for s in samples if s.alive]
    cpu = [s.cpu_pct for s in alive]
    rss = [s.rss_mb for s in alive]
    summary: dict[str, Any] = {
        "label": label,
        "samples": len(alive),
        "hours": round(alive[-1].elapsed / 3600, 2) if alive else 0.0,
        "cpu_median": _pct(cpu, 0.5),
        "cpu_p95": _pct(cpu, 0.95),
        "cpu_core_median": _pct([s.cpu_core_pct for s in alive], 0.5),
        "rss_median": _pct(rss, 0.5),
        "rss_max": max(rss) if rss else None,
        "private_median": _pct([s.private_mb for s in alive], 0.5),
        "pws_median": _pct([s.pws_mb for s in alive], 0.5),
        "pws_max": max((s.pws_mb for s in alive), default=None),
        "crashed": any(not s.alive for s in samples),
        "growth_pct": None,
    }
    pws = [s.pws_mb for s in alive]
    if label == "soak" and len(pws) >= 10:
        window = max(1, len(pws) // 10)
        first, last = statistics.median(pws[:window]), statistics.median(pws[-window:])
        summary["growth_pct"] = round((last - first) / first * 100, 2) if first else None
    return summary


def verdict(summary: dict[str, Any]) -> dict[str, bool | None]:
    """Pass/fail per target that this run can judge."""
    out: dict[str, bool | None] = {}
    if summary.get("pws_median") is not None:
        out["memory"] = summary["pws_median"] < TARGETS["memory_mb"]
    if summary["label"] == "idle" and summary["cpu_median"] is not None:
        out["cpu"] = summary["cpu_median"] < TARGETS["idle_cpu_pct"]
    if summary["label"] == "playing" and summary["cpu_median"] is not None:
        out["cpu"] = summary["cpu_median"] < TARGETS["playing_cpu_pct"]
    if summary["label"] == "soak":
        out["no_crash"] = not summary["crashed"]
        if summary["growth_pct"] is not None:
            out["growth"] = summary["growth_pct"] < TARGETS["soak_growth_pct"]
    return out


PERF_HELP = [
    "# 非功能需求量測（效能與穩定性）",
    "目標（規格：非功能需求）：閒置 CPU < 1%、播放中 CPU < 3%（MVP 沒有卡拉 OK，以一般播放中量測）、記憶體 < 120 MB、連續執行 72 小時無崩潰且記憶體增長 < 10%。",
    "# 指標",
    "CPU（全機）：占整台電腦 CPU 的比例，與工作管理員相同，用於判定。CPU（單核）：占一個核心的比例，僅供參考；規格未寫明採用哪一種。",
    "記憶體（工作管理員）：私人工作集，即工作管理員預設「記憶體」欄的數字，以中位數判定（規格 v1.11）。轉換簡體歌詞的當下會短暫升到約 150 MB，列在「記憶體最大」供參考，不列入判定。",
    "工作集（含與其他程式共用的字型、顯示驅動）與私有位元組僅供參考。",
    "長跑增長：最後 10% 時段與最初 10% 時段的記憶體（工作管理員）中位數相比。程序在長跑中消失記為崩潰。",
    "# 注意",
    "從原始碼執行時，venv 的 python.exe 會先啟動一個小的啟動器，再由它啟動真正的直譯器；量測對象是記憶體最大的那一個（App 本體）。打包後只有單一程序。",
]

SUMMARY_KEYS = [
    ("label", "量測", 10),
    ("started", "開始時間", 19),
    ("samples", "樣本數", 8),
    ("hours", "時數", 7),
    ("cpu_median", "CPU 中位數（全機 %）", 14),
    ("cpu_p95", "CPU p95（全機 %）", 13),
    ("cpu_core_median", "CPU 中位數（單核 %）", 14),
    ("pws_median", "記憶體中位數 (MB，工作管理員)", 16),
    ("pws_max", "記憶體最大 (MB，工作管理員)", 16),
    ("rss_median", "工作集中位數 (MB)", 13),
    ("rss_max", "工作集最大 (MB)", 13),
    ("private_median", "私有位元組中位數 (MB)", 15),
    ("growth_pct", "長跑增長 (%)", 11),
    ("crashed", "中途消失", 9),
    ("verdict", "判定", 28),
]


def write_perf_xlsx(path: Path, runs: Sequence[dict[str, Any]]) -> None:
    from openpyxl import Workbook

    from lyricfloat.diagnostics.xlsx import Column, write_table, write_text_sheet

    columns = [Column(key, title, width, wrap=key == "verdict") for key, title, width in SUMMARY_KEYS]
    names = {"memory": "記憶體", "cpu": "CPU", "no_crash": "無崩潰", "growth": "記憶體增長"}
    rows = []
    for run in runs:
        v = verdict(run)
        row = {k: run.get(k) for k, _t, _w in SUMMARY_KEYS}
        row["crashed"] = "是" if run.get("crashed") else "否"
        row["verdict"] = "、".join(f"{names[k]}{'通過' if ok else '未通過'}" for k, ok in v.items()) or "—"
        for key in ("cpu_median", "cpu_p95", "cpu_core_median", "pws_median", "pws_max", "rss_median", "rss_max", "private_median"):
            if row[key] is not None:
                row[key] = round(row[key], 2)
        rows.append(row)
    wb = Workbook()
    ws = wb.active
    ws.title = "摘要"
    write_table(ws, columns, rows, freeze="B2")
    write_text_sheet(wb.create_sheet("說明"), PERF_HELP)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
