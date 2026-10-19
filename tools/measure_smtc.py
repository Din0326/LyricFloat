"""M1 gate: measure how players report progress through Windows SMTC.

    python tools/measure_smtc.py measure --player spotify [--seconds 180] [--scripted]
    python tools/measure_smtc.py report [--block PLAYER="reason"] [--unblock PLAYER]

measure  samples one player's session (50 ms poll + SMTC events) and saves
         reports/raw/smtc_<player>_<time>.csv, then rebuilds the report.
         --scripted also sends pause / play / seek commands through SMTC
         and records how long the player takes to report them.
report   analyses every raw CSV into reports/smtc_summary.xlsx (summary,
         segments, method) and prints a text summary.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

from lyricfloat.diagnostics.smtc_report import (
    Sample,
    analyse,
    read_samples,
    segment_rows,
    summary_row,
    text_summary,
    write_samples,
    write_xlsx,
)

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
RAW_DIR = REPORTS / "raw"
SUMMARY = REPORTS / "smtc_summary.xlsx"
BLOCKED = REPORTS / "blocked.json"

PLAYERS = {
    "spotify": ("Spotify", "spotify"),
    "ytmusic": ("YouTube Music (Chrome)", "chrome"),
}

# (seconds after start, action) for --scripted runs
SCRIPT = [(30.0, "pause"), (36.0, "play"), (60.0, "seek+30"), (95.0, "seek-20")]


def _now_label() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ measure


async def _find_session(manager, needle: str):
    from lyricfloat.media import smtc

    matches = [s for s in manager.get_sessions() if needle in smtc.app_id(s).lower()]
    if not matches:
        return None
    playing = [s for s in matches if smtc.read_status(s) == 4]
    return (playing or matches)[0]


async def measure(player: str, seconds: float, interval: float, scripted: bool, app_id: str | None) -> Path | None:
    from lyricfloat.media import smtc

    label, default_needle = PLAYERS[player]
    needle = (app_id or default_needle).lower()
    manager = await smtc.request_manager()
    session = await _find_session(manager, needle)
    if session is None:
        ids = [smtc.app_id(s) for s in manager.get_sessions()]
        print(f"找不到 {label} 的媒體會話（比對字串 {needle!r}）。目前的會話：{ids or '無'}")
        return None

    props = await session.try_get_media_properties_async()
    controls = session.get_playback_info().controls
    meta = {
        "player": player,
        "app_id": smtc.app_id(session),
        "started": _now_label(),
        "seconds": str(seconds),
        "interval": str(interval),
        "scripted": str(scripted),
        "can_pause": str(controls.is_pause_enabled),
        "can_seek": str(controls.is_playback_position_enabled),
        "can_next": str(controls.is_next_enabled),
    }
    print(f"量測 {label}：{meta['app_id']}｜目前歌曲：{props.title} — {props.artist}｜{seconds:.0f} 秒")
    print(f"控制能力：暫停={meta['can_pause']} 拖動={meta['can_seek']} 下一首={meta['can_next']}")

    loop = asyncio.get_running_loop()
    samples: list[Sample] = []
    title = props.title or ""

    def record(kind: str, detail: str = "") -> Sample | None:
        try:
            raw = smtc.read_timeline(session)
        except OSError:
            return None
        s = Sample(time.monotonic(), time.time(), kind, int(raw.status), raw.position, raw.duration, raw.last_updated, title, detail)
        samples.append(s)
        return s

    def from_thread(kind: str):
        def handler(sender, args) -> None:
            try:
                loop.call_soon_threadsafe(record, kind)
            except RuntimeError:
                pass

        return handler

    tokens = [
        ("timeline_properties_changed", session.add_timeline_properties_changed(from_thread("event:timeline"))),
        ("playback_info_changed", session.add_playback_info_changed(from_thread("event:playback"))),
        ("media_properties_changed", session.add_media_properties_changed(from_thread("event:media"))),
    ]

    async def run_script(start: float) -> None:
        for at, action in SCRIPT:
            if at >= seconds - 10:
                break
            await asyncio.sleep(max(0.0, start + at - time.monotonic()))
            if action == "pause":
                record("action:pause")
                await session.try_pause_async()
            elif action == "play":
                record("action:play")
                await session.try_play_async()
            else:
                current = record("poll")
                if current is None:
                    continue
                delta = 30.0 if action == "seek+30" else -20.0
                target = current.extrapolated() + delta
                if current.duration > 0:
                    target = min(target, current.duration - 15.0)
                target = max(0.0, target)
                record("action:seek", f"{target:.3f}")
                await session.try_change_playback_position_async(int(target * 10_000_000))

    start = time.monotonic()
    script_task = asyncio.ensure_future(run_script(start)) if scripted else None
    next_title_check = start
    next_progress = start + 10
    try:
        while (now := time.monotonic()) - start < seconds:
            record("poll")
            if now >= next_title_check:
                next_title_check = now + 1.0
                p = await session.try_get_media_properties_async()
                if p is not None and p.title and p.title != title:
                    title = p.title
            if now >= next_progress:
                next_progress = now + 10
                print(f"  {now - start:5.0f}s／{seconds:.0f}s，樣本 {len(samples)}")
            await asyncio.sleep(interval)
    finally:
        if script_task is not None:
            script_task.cancel()
        for name, token in tokens:
            try:
                getattr(session, f"remove_{name}")(token)
            except OSError:
                pass

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RAW_DIR / f"smtc_{player}_{stamp}.csv"
    write_samples(path, samples, meta)
    print(f"原始資料：{path}")
    return path


# ------------------------------------------------------------------- report


def _load_blocked() -> dict[str, str]:
    try:
        return json.loads(BLOCKED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def report(block: list[str], unblock: list[str]) -> int:
    blocked = _load_blocked()
    for item in block:
        player, _, reason = item.partition("=")
        blocked[player.strip()] = reason.strip() or "BLOCK"
    for player in unblock:
        blocked.pop(player.strip(), None)
    if block or unblock:
        BLOCKED.parent.mkdir(parents=True, exist_ok=True)
        BLOCKED.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")

    runs = []
    for path in sorted(RAW_DIR.glob("smtc_*.csv")):
        meta, samples = read_samples(path)
        if not samples:
            continue
        player = meta.get("player") or path.stem.split("_")[1]
        runs.append(analyse(player, f"reports/raw/{path.name}", meta, samples))

    order = list(PLAYERS)
    runs.sort(key=lambda r: (order.index(r.player) if r.player in order else 99, r.started))
    rows, seg_rows = [], []
    for player in order:
        player_runs = [r for r in runs if r.player == player]
        if not player_runs:
            status = "BLOCK" if player in blocked else "未量測"
            rows.append({"player": player, "status": status, "note": blocked.get(player, "尚無量測資料")})
            continue
        for run in player_runs:
            rows.append(summary_row(run))
            seg_rows.extend(segment_rows(run))

    lock = SUMMARY.with_name("~$" + SUMMARY.name)
    if lock.exists():
        print(f"請先關閉 Excel 中的 {SUMMARY.name}（偵測到鎖定檔），再重跑 report。")
        return 1
    write_xlsx(SUMMARY, rows, seg_rows)
    print(text_summary(rows))
    print(f"摘要：{SUMMARY}")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("measure", help="sample one player's SMTC session")
    m.add_argument("--player", choices=list(PLAYERS), required=True)
    m.add_argument("--seconds", type=float, default=180.0)
    m.add_argument("--interval", type=float, default=0.05, help="poll interval in seconds")
    m.add_argument("--scripted", action="store_true", help="send pause/play/seek via SMTC during the run")
    m.add_argument("--app-id", help="override the session id substring to match")

    r = sub.add_parser("report", help="rebuild reports/smtc_summary.xlsx from raw CSVs")
    r.add_argument("--block", action="append", default=[], metavar="PLAYER=REASON")
    r.add_argument("--unblock", action="append", default=[], metavar="PLAYER")

    args = parser.parse_args(argv)
    if args.command == "measure":
        path = asyncio.run(measure(args.player, args.seconds, args.interval, args.scripted, args.app_id))
        if path is None:
            return 1
        return report([], [])
    return report(args.block, args.unblock)


if __name__ == "__main__":
    sys.exit(main())
