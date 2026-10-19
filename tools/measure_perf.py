"""Non-functional measurements against the running app (spec: non-functional requirements).

    python tools/measure_perf.py sample --label idle [--seconds 120] [--interval 1] [--pid N]
    python tools/measure_perf.py sample --label playing [--seconds 120]
    python tools/measure_perf.py soak [--hours 72] [--interval 60] [--pid N]
    python tools/measure_perf.py report

Start LyricFloat first (Win+R: ...\\.venv\\Scripts\\pythonw.exe -m lyricfloat).
idle: nothing playing; playing: music with synced lyrics on screen.
soak writes every sample as it goes, so stopping it (Ctrl+C) or a crash
keeps what was measured. Raw samples go to reports/raw/ (numbers only),
the summary to reports/perf_summary.xlsx.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import psutil

from lyricfloat.diagnostics.perf import (
    PerfSample,
    find_app_process,
    read_samples,
    summarize,
    take_sample,
    verdict,
    write_perf_xlsx,
    write_samples,
)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "reports" / "raw"
SUMMARY = ROOT / "reports" / "perf_summary.xlsx"


def _process(pid: int | None):
    if pid:
        return psutil.Process(pid)
    proc = find_app_process(psutil)
    if proc is None:
        print("找不到執行中的 LyricFloat，請先啟動 App（或用 --pid 指定）。")
    return proc


def _sample_run(proc, label: str, seconds: float, interval: float, path: Path, append: bool) -> int:
    meta = {"label": label, "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pid": str(proc.pid)}
    cpus = psutil.cpu_count() or 1
    started = time.time()
    proc.cpu_percent(interval=None)  # prime: the first reading is always 0
    count = 0
    next_print = started + max(60.0, interval * 10)
    if append:
        write_samples(path, [], meta, append=True)
    collected: list[PerfSample] = []
    try:
        while time.time() - started < seconds:
            time.sleep(interval)
            now = time.time()
            try:
                sample = take_sample(proc, started, now, cpus)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                gone = PerfSample(round(now, 3), round(now - started, 3), 0, 0, 0, 0, alive=False)
                print(f"程序在 {(now - started) / 3600:.2f} 小時後消失（記為中途消失）。")
                if append:
                    write_samples(path, [gone], meta, append=True)
                else:
                    collected.append(gone)
                break
            count += 1
            if append:
                write_samples(path, [sample], meta, append=True)
            else:
                collected.append(sample)
            if now >= next_print:
                next_print = now + max(60.0, interval * 10)
                print(f"  {(now - started) / 60:6.1f} 分：CPU {sample.cpu_pct:.2f}%（全機）、記憶體 {sample.pws_mb:.1f} MB", flush=True)
    except KeyboardInterrupt:
        print("已停止，保留目前的樣本。")
    if not append:
        write_samples(path, collected, meta)
    print(f"樣本 {count} 筆：{path}")
    return 0


def cmd_sample(args) -> int:
    proc = _process(args.pid)
    if proc is None:
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RAW / f"perf_{args.label}_{stamp}.csv"
    print(f"量測 {args.label}：PID {proc.pid}，{args.seconds:.0f} 秒，每 {args.interval} 秒一次")
    _sample_run(proc, args.label, args.seconds, args.interval, path, append=False)
    return cmd_report(args)


def cmd_soak(args) -> int:
    proc = _process(args.pid)
    if proc is None:
        return 2
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RAW / f"perf_soak_{stamp}.csv"
    print(f"長跑：PID {proc.pid}，{args.hours} 小時，每 {args.interval} 秒一次；可隨時 Ctrl+C 停止")
    _sample_run(proc, "soak", args.hours * 3600, args.interval, path, append=True)
    return cmd_report(args)


def cmd_report(_args) -> int:
    runs = []
    for path in sorted(RAW.glob("perf_*.csv")):
        meta, samples = read_samples(path)
        if not samples:
            continue
        summary = summarize(meta.get("label", "?"), samples)
        summary["started"] = meta.get("started", "")
        runs.append(summary)
    if not runs:
        print("還沒有量測資料。")
        return 2
    lock = SUMMARY.with_name("~$" + SUMMARY.name)
    if lock.exists():
        print(f"請先關閉 Excel 中的 {SUMMARY.name}，再重跑 report。")
        return 1
    write_perf_xlsx(SUMMARY, runs)
    for r in runs:
        v = verdict(r)
        growth = f"、長跑增長 {r['growth_pct']}%" if r["growth_pct"] is not None else ""
        print(
            f"[{r['label']}] {r['started']} {r['samples']} 筆：CPU 中位數 {r['cpu_median']:.2f}%（全機）／{r['cpu_core_median']:.1f}%（單核），"
            f"記憶體（工作管理員）中位數 {r['pws_median']:.1f} MB、最大 {r['pws_max']:.1f} MB，工作集 {r['rss_median']:.1f} MB{growth}｜"
            + "、".join(f"{k} {'通過' if ok else '未通過'}" for k, ok in v.items())
        )
    print(f"摘要：{SUMMARY}")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample", help="sample CPU and memory for a while")
    s.add_argument("--label", choices=["idle", "playing"], required=True)
    s.add_argument("--seconds", type=float, default=120.0)
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("--pid", type=int)
    k = sub.add_parser("soak", help="long run (default 72 h)")
    k.add_argument("--hours", type=float, default=72.0)
    k.add_argument("--interval", type=float, default=60.0)
    k.add_argument("--pid", type=int)
    sub.add_parser("report", help="rebuild reports/perf_summary.xlsx")
    args = parser.parse_args(argv)
    return {"sample": cmd_sample, "soak": cmd_soak, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
