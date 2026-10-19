"""Browser title self-test (SPEC v1.50): known songs dressed up as YouTube video titles.

    python tools/title_variants.py run [--history PATH] [--per-group N] [--groups zh,jp,kr,en]
                                       [--templates NAME,...] [--only TITLE,...] [--delay SECONDS] [--out XLSX]
    python tools/title_variants.py list [--history PATH] [--per-group N]

run   resolve each song from its clean title (the answer), then every YouTube-style
      variant of it as a Chrome video, without the cache; writes the workbook
      (reports/private/title_variants.xlsx) and the raw rows (reports/raw/), prints
      the failures, exits 0 when every variant found its song, 1 otherwise
list  print the simulated titles without asking any source

Songs come from the app's play history (%LOCALAPPDATA%, or --history): those a music
app reported and the app found lyrics for. Outputs carry song titles and stay out of git.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from lyricfloat.diagnostics.history import read_history
from lyricfloat.diagnostics.title_variants import (
    GROUP_NAMES,
    GROUPS,
    NOT_FOUND,
    OK,
    OK_PLAIN,
    TEMPLATES,
    WRONG,
    MemoSource,
    evaluate,
    outcome_json,
    pick_songs,
    variants_of,
    write_report_xlsx,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_XLSX = ROOT / "reports" / "private" / "title_variants.xlsx"
RAW_DIR = ROOT / "reports" / "raw"


def _songs(args):
    history = read_history(Path(args.history)) if args.history else read_history()
    groups = [g for g in args.groups.split(",") if g in GROUPS]
    songs = pick_songs(history, args.per_group, groups)
    if args.only:
        wanted = [w.casefold() for w in args.only.split(",")]
        songs = [s for s in songs if any(w in s.title.casefold() for w in wanted)]
    return songs


def _templates(args):
    if not args.templates:
        return TEMPLATES
    names = set(args.templates.split(","))
    unknown = names - {t.name for t in TEMPLATES}
    if unknown:
        raise SystemExit(f"沒有這些標題格式：{', '.join(sorted(unknown))}")
    return tuple(t for t in TEMPLATES if t.name in names)


def _cmd_list(args) -> int:
    from lyricfloat.lyrics.chinese import ChineseConverter

    cc = ChineseConverter()
    for song in _songs(args):
        print(f"== {song.title} — {song.artist}")
        for v in variants_of(song, cc.convert, _templates(args)):
            print(f"  [{v.template}] {v.title}  ｜頻道 {v.channel}｜{v.duration:.0f} 秒")
    return 0


async def _run(args) -> int:
    from lyricfloat.lyrics.cache import NullCache
    from lyricfloat.lyrics.pipeline import OpenCCPipeline
    from lyricfloat.lyrics.service import LyricResolver
    from lyricfloat.lyrics.sources.lrclib import LrclibSource
    from lyricfloat.lyrics.sources.netease import NeteaseSource

    songs = _songs(args)
    if not songs:
        print("播放紀錄裡沒有可用的歌（需要音樂 App 播過、且找到歌詞的歌）。")
        return 2
    pipeline = OpenCCPipeline()
    sources = [MemoSource(LrclibSource()), MemoSource(NeteaseSource())]
    resolver = LyricResolver(sources, cache=NullCache(), pipeline=pipeline)
    fold = lambda text: pipeline.cc.convert("t2s", text)  # noqa: E731
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw = RAW_DIR / f"title_variants_{stamp}.jsonl"

    def progress(i, total, o, song) -> None:
        if o is None:
            print(f"  [{i:4d}/{total}] - {song.title} — {song.artist}：沒有答案，略過", flush=True)
            return
        with raw.open("a", encoding="utf-8") as f:  # every row as it comes (a long run may be cut short)
            f.write(json.dumps(outcome_json(o), ensure_ascii=False) + "\n")
        if o.verdict not in (OK, OK_PLAIN) or i % 25 == 0:
            print(f"  [{i:4d}/{total}] {o.verdict} [{o.variant.template}] {o.variant.title}｜{o.got or o.kind}", flush=True)

    print(f"{len(songs)} 首歌，每首套用適用的標題格式（每次間隔 {args.delay} 秒）…")
    try:
        with pipeline.session():
            outcomes, skipped = await evaluate(songs, resolver, pipeline.cc.convert, fold, args.delay, progress,
                                               _templates(args))
    finally:
        for source in sources:
            await source.aclose()

    out = Path(args.out) if args.out else REPORT_XLSX
    write_report_xlsx(out, outcomes, skipped)
    requests = sum(s.requests for s in sources)
    ok = sum(o.verdict in (OK, OK_PLAIN) for o in outcomes)
    print(f"\n正確 {ok}/{len(outcomes)}（找不到 {sum(o.verdict == NOT_FOUND for o in outcomes)}，"
          f"抓錯歌 {sum(o.verdict == WRONG for o in outcomes)}），沒有答案的歌 {len(skipped)} 首，請求 {requests} 次")
    by_tpl: dict[str, list] = {}
    for o in outcomes:
        by_tpl.setdefault(o.variant.template, []).append(o)
    for name, rows in sorted(by_tpl.items(), key=lambda kv: sum(o.verdict in (OK, OK_PLAIN) for o in kv[1]) / len(kv[1])):
        good = sum(o.verdict in (OK, OK_PLAIN) for o in rows)
        groups = "、".join(sorted({GROUP_NAMES[o.variant.group] for o in rows}))
        print(f"  {name:20s} {good:3d}/{len(rows):<3d} {groups}")
    print(f"報告：{out}\n原始資料：{raw}")
    return 0 if ok == len(outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "list"):
        p = sub.add_parser(name)
        p.add_argument("--history", help="播放紀錄 jsonl（預設：App 的紀錄）")
        p.add_argument("--per-group", type=int, default=0, help="每種語言最多幾首（0：全部）")
        p.add_argument("--groups", default=",".join(GROUPS), help="zh,jp,kr,en")
        p.add_argument("--templates", help="只跑這些標題格式（逗號分隔）")
        p.add_argument("--only", help="只跑歌名含這些文字的歌（逗號分隔）")
        if name == "run":
            p.add_argument("--delay", type=float, default=0.3)
            p.add_argument("--out", help="報告 xlsx 路徑")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        return _cmd_list(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
