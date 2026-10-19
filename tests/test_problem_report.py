import csv
import datetime as dt
import json

from lyricfloat.diagnostics.problem_report import add_to_test_list, build_report, write_report
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo

TRACK = TrackInfo("紙船: Live?", "範例樂團", "港口", 200.0, "Spotify.exe")
SHOWN = TrackInfo("紙船: Live?", "範例樂團", "港口", 200.0, "Spotify.exe")
NOW = dt.datetime(2026, 10, 7, 13, 5, 9)


def result():
    lines = (LyricLine(10.0, "made up line one"), LyricLine(14.0, "made up line two"))
    cand = Candidate("lrclib", "42", "紙船", "範例樂團", "港口", 200.4, None, None)
    return LyricResult(
        TRACK, ResultKind.SYNCED, Lyrics(lines, True, ("自編一", None)), cand, 0.97,
        notes=("l1_switched",), offset=0.3, from_cache=True, timeline_checked=True,
    )


def test_report_has_candidates_scores_and_snapshot():
    cands = [{"source": "lrclib", "candidates": [{"id": "42", "score": 0.97}]}]
    r = build_report(TRACK, SHOWN, result(), cands, 12.3456, 0, "s2twp", note="早了一句", now=NOW)
    assert r["exported_at"] == "2026-10-07T13:05:09"
    assert r["note"] == "早了一句"
    assert r["track"]["duration"] == 200.0
    assert r["playback"] == {"position": 12.346, "line_index": 0}
    assert r["candidates"] == cands
    assert r["result"]["source_id"] == "42" and r["result"]["score"] == 0.97
    assert r["result"]["offset"] == 0.3 and r["result"]["notes"] == ["l1_switched"]
    assert r["lyrics_snapshot"] == [
        {"start": 10.0, "text": "made up line one", "translation": "自編一"},
        {"start": 14.0, "text": "made up line two", "translation": None},
    ]


def test_report_without_lyrics():
    r = build_report(TRACK, SHOWN, LyricResult(TRACK, ResultKind.NOT_FOUND), [], None, -1, None, now=NOW)
    assert r["result"]["kind"] == "not_found"
    assert r["lyrics_snapshot"] == []
    r = build_report(TRACK, SHOWN, None, [], None, -1, None, now=NOW)
    assert r["result"] is None


def test_write_report_uses_a_safe_file_name(tmp_path):
    r = build_report(TRACK, SHOWN, result(), [], 1.0, 0, "s2twp", now=NOW)
    path = write_report(r, tmp_path)
    assert path.name == "20261007T130509_紙船_ Live.json"
    assert json.loads(path.read_text(encoding="utf-8"))["track"]["title"] == "紙船: Live?"


def test_add_to_test_list_dedupes(tmp_path):
    path = tmp_path / "test_list.csv"
    assert add_to_test_list(TRACK, "zh", "早了一句", path)
    assert not add_to_test_list(TRACK, "zh", "again", path)
    other = TrackInfo("Paper Boats", "The Example Band", "", 180.0, "Chrome")
    assert add_to_test_list(other, "en", "", path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["title"], r["language"]) for r in rows] == [("紙船: Live?", "zh"), ("Paper Boats", "en")]
    assert rows[0]["note"] == "早了一句"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM for Excel


def test_reveal_command_keeps_the_select_switch_unquoted(tmp_path):
    from lyricfloat.diagnostics.problem_report import reveal_command

    path = tmp_path / "20261007T130018_Super Duper.json"
    assert reveal_command(path) == f'explorer /select,"{path}"'
