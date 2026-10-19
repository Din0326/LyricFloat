"""Hit-rate regression over the test list (spec: MVP acceptance).

    python tools/hit_rate.py candidates [--history PATH] [--list PATH] [--out XLSX]
    python tools/hit_rate.py adopt [--xlsx XLSX] [--list PATH]
    python tools/hit_rate.py run [--list PATH] [--limit N] [--delay SECONDS] [--out XLSX]
    python tools/hit_rate.py calibration [--file PATH] [--out XLSX]
    python tools/hit_rate.py alignment [--logs FOLDER] [--out XLSX]

candidates  songs from the app's play history -> a workbook to pick from
adopt       append the rows marked 納入=是 to the test list
run         query every listed song (no cache), write the report and exit
            0 when the targets are met, 1 when not, 2 when the list is empty
calibration summarise calibration-mode marks (timeline error, target median
            <= 500 ms) into a workbook
alignment   summarise the automatic timing correction (L3) statistics into a
            workbook (numbers only), to tune its thresholds

Defaults read the app's own files (%LOCALAPPDATA% history, %APPDATA% test
list) and write workbooks to reports/private/, which stays out of git.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from lyricfloat.diagnostics.hit_rate import (
    LANGUAGE_NAMES,
    adopt_from_xlsx,
    build_candidates,
    evaluate,
    load_test_list,
    summarize,
    write_candidates_xlsx,
    write_report_xlsx,
)
from lyricfloat.diagnostics.alignment_report import alignment_log_files, read_records, write_alignment_xlsx
from lyricfloat.diagnostics.calibration import calibration_path, read_marks, summarize_marks, write_marks_xlsx
from lyricfloat.diagnostics.history import history_path, read_history
from lyricfloat.diagnostics.problem_report import test_list_path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "reports" / "private"
CANDIDATES_XLSX = PRIVATE / "歌單候選.xlsx"
REPORT_XLSX = PRIVATE / "命中率報告.xlsx"
CALIBRATION_XLSX = PRIVATE / "校對結果.xlsx"
ALIGNMENT_XLSX = PRIVATE / "對齊統計.xlsx"


def _locked(path: Path) -> bool:
    lock = path.with_name("~$" + path.name)
    if lock.exists():
        print(f"請先關閉 Excel 中的 {path.name}（偵測到鎖定檔），再重跑。")
        return True
    return False


def cmd_candidates(args) -> int:
    history = read_history(Path(args.history) if args.history else history_path())
    listed = load_test_list(Path(args.list) if args.list else test_list_path())
    if not history:
        print("播放紀錄是空的：先用 App 聽幾首歌，或用 --history 指定紀錄檔。")
        return 2
    rows = build_candidates(history, listed)
    out = Path(args.out) if args.out else CANDIDATES_XLSX
    if _locked(out):
        return 1
    write_candidates_xlsx(out, rows, listed)
    new = sum(1 for r in rows if r["listed"] == "否")
    print(f"候選 {len(rows)} 首（尚未在歌單 {new} 首），目前歌單 {len(listed)} 首。")
    print(f"已輸出：{out}")
    return 0


def cmd_adopt(args) -> int:
    xlsx = Path(args.xlsx) if args.xlsx else CANDIDATES_XLSX
    if not xlsx.exists():
        print(f"找不到 {xlsx}，請先執行 candidates。")
        return 2
    if _locked(xlsx):
        return 1
    target = Path(args.list) if args.list else test_list_path()
    added, skipped = adopt_from_xlsx(xlsx, target)
    print(f"加入 {added} 首，已在歌單而略過 {skipped} 首。歌單：{target}（共 {len(load_test_list(target))} 首）")
    return 0


def cmd_alignment(args) -> int:
    files = alignment_log_files(Path(args.logs) if args.logs else None)
    records = read_records(files)
    if not records:
        print("還沒有對齊統計：在設定開啟「自動時間軸校正」後，完整聽完幾首有同步歌詞的歌。")
        return 2
    out = Path(args.out) if args.out else ALIGNMENT_XLSX
    if _locked(out):
        return 1
    write_alignment_xlsx(out, records)
    ok = [r for r in records if r.get("ok")]
    used = [r for r in ok if r.get("usable")]
    print(f"對齊 {len(records)} 首：成功 {len(ok)}、採用 {len(used)}｜報告：{out}")
    return 0


def cmd_calibration(args) -> int:
    marks = read_marks(Path(args.file) if args.file else calibration_path())
    if not marks:
        print("還沒有校對紀錄：在 App 系統列開啟「校對模式」，聽歌時在開唱瞬間按快捷鍵。")
        return 2
    out = Path(args.out) if args.out else CALIBRATION_XLSX
    if _locked(out):
        return 1
    write_marks_xlsx(out, marks)
    s = summarize_marks(marks)
    print(f"標記 {s['marks']} 筆、{s['songs']} 首（規格建議 20 首 × 5 行）")
    print(f"誤差絕對值中位數 {s['median_abs_ms']:.0f} ms（目標 ≤ 500），帶正負中位數 {s['median_signed_ms']:+.0f} ms")
    print(f"判定：{'通過' if s['passed'] else '未通過'}｜報告：{out}")
    return 0 if s["passed"] else 1


async def _run(args) -> int:
    from lyricfloat.lyrics.cache import NullCache
    from lyricfloat.lyrics.pipeline import OpenCCPipeline
    from lyricfloat.lyrics.service import LyricResolver
    from lyricfloat.lyrics.sources.lrclib import LrclibSource
    from lyricfloat.lyrics.sources.netease import NeteaseSource

    songs = load_test_list(Path(args.list) if args.list else test_list_path())
    if args.limit:
        songs = songs[: args.limit]
    if not songs:
        print("測試歌單是空的：先用 candidates／adopt 建立，或在 App 用「這首歌詞有誤」加入。")
        return 2
    out = Path(args.out) if args.out else REPORT_XLSX
    if _locked(out):
        return 1

    pipeline = OpenCCPipeline()
    sources = [LrclibSource(), NeteaseSource()]
    resolver = LyricResolver(sources, cache=NullCache(), pipeline=pipeline)  # no cache: measure the sources

    def progress(i: int, total: int, o) -> None:
        mark = "✔" if o.synced else ("✖" if not o.error else "!")
        extra = f" 殘留簡體 {o.simplified} 行" if o.simplified else ""
        print(f"  [{i:3d}/{total}] {mark} {o.song.title} — {o.song.artist}｜{o.kind or o.error}{extra}", flush=True)

    print(f"評估 {len(songs)} 首（每首間隔 {args.delay} 秒）…")
    try:
        outcomes = await evaluate(songs, resolver, pipeline, delay=args.delay, progress=progress)
    finally:
        for source in sources:
            await source.aclose()

    write_report_xlsx(out, outcomes)
    s = summarize(outcomes)
    for lang, v in s["languages"].items():
        if not v["songs"]:
            continue
        rate = f"{v['rate']:.1%}"
        verdict = "" if v["passed"] is None else ("　通過" if v["passed"] else "　未通過")
        target = f"（目標 {v['target']:.0%}）" if v["target"] else ""
        print(f"{LANGUAGE_NAMES[lang]}：{v['synced']}/{v['songs']} 同步命中 {rate}{target}{verdict}")
    print(f"殘留簡體：{s['simplified']} 行　查詢出錯：{s['errors']} 首　首行歌詞中位數：{s['first_ms_median'] and round(s['first_ms_median'])} ms")
    print(f"整體：{'通過' if s['passed'] else '未通過'}｜報告：{out}")
    return 0 if s["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("candidates", help="build the pick list from play history")
    c.add_argument("--history")
    c.add_argument("--list")
    c.add_argument("--out")

    a = sub.add_parser("adopt", help="add the picked songs to the test list")
    a.add_argument("--xlsx")
    a.add_argument("--list")

    r = sub.add_parser("run", help="query the test list and report hit rates")
    r.add_argument("--list")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--delay", type=float, default=0.5)
    r.add_argument("--out")

    k = sub.add_parser("calibration", help="summarise calibration-mode marks")
    k.add_argument("--file")
    k.add_argument("--out")

    al = sub.add_parser("alignment", help="summarise the automatic timing correction statistics")
    al.add_argument("--logs")
    al.add_argument("--out")

    args = parser.parse_args(argv)
    if args.command == "calibration":
        return cmd_calibration(args)
    if args.command == "alignment":
        return cmd_alignment(args)
    if args.command == "candidates":
        return cmd_candidates(args)
    if args.command == "adopt":
        return cmd_adopt(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
