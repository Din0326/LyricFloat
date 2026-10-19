"""The worker hands aligned words back to the lyric lines (no torch needed for this part)."""

import json
import sqlite3

from lyricfloat.align.recorder import reason_key
from lyricfloat.align.worker import split_by_lines
from lyricfloat.lyrics.cache import L3_APPLIED, SCHEMA_VERSION, SqliteCache


def w(text, start, p=0.6):
    return {"text": text, "start": start, "end": start + 0.3, "p": p}


def test_words_go_back_to_their_lines_even_when_segments_merge():
    lines = ["这是第一句", "", "♪", "second line here", "最后，一句。"]
    words = [w("这是", 1), w("第一句", 1.5), w(" second", 5), w(" line", 5.4), w(" here", 5.8), w("最后", 8), w("，", 8.2), w("一句", 8.4), w("。", 8.6)]
    segs = split_by_lines(lines, words)
    assert len(segs) == len(lines)
    assert segs[0]["start"] == 1 and [x["text"] for x in segs[0]["words"]] == ["这是", "第一句"]
    assert segs[1] == {"start": None, "end": None, "words": []}  # blank line
    assert segs[2]["start"] is None  # nothing sung
    assert segs[3]["start"] == 5
    assert [x["text"] for x in segs[4]["words"]] == ["最后", "，", "一句", "。"]  # punctuation stays with its line


def test_missing_words_leave_later_lines_without_a_start():
    segs = split_by_lines(["一二", "三四"], [w("一二", 1)])
    assert segs[0]["start"] == 1 and segs[1]["start"] is None


def test_reason_keys_drop_the_details():
    assert reason_key("started late (20.0s)") == "started late"
    assert reason_key("capture unavailable: no process for Chrome") == "capture unavailable"
    assert reason_key("paused") == "paused"


def test_upgrade_from_v5_retries_unusable_alignments(tmp_path):
    path = tmp_path / "cache.sqlite3"
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO lyrics (artist_key, title_key, duration, kind, synced, created_at, updated_at, l3_state, l3_json) "
        "VALUES ('a', 'b', 100, 'synced', 1, 0, 0, 'unusable', '{}'), ('c', 'd', 100, 'synced', 1, 0, 0, ?, '{}')",
        (L3_APPLIED,),
    )
    db.execute("PRAGMA user_version=5")
    db.commit()
    db.close()
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    states = [r[0] for r in db.execute("SELECT l3_state FROM lyrics ORDER BY id")]
    assert states == [None, None]  # v5 -> v7: unusable retried (v6) and applied redone (v7)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_upgrade_to_v7_redoes_applied_alignments_only(tmp_path):
    from lyricfloat.lyrics.cache import L3_REVERTED, L3_UNUSABLE

    path = tmp_path / "cache.sqlite3"
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    for i, state in enumerate((L3_APPLIED, L3_REVERTED, L3_UNUSABLE, "failed")):
        db.execute(
            "INSERT INTO lyrics (artist_key, title_key, duration, kind, synced, created_at, updated_at, l3_state, l3_json, l3_attempts) "
            "VALUES (?, 't', 100, 'synced', 1, 0, 0, ?, '{}', 1)",
            (f"a{i}", state),
        )
    db.execute("PRAGMA user_version=6")
    db.commit()
    db.close()
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    rows = list(db.execute("SELECT l3_state, l3_json FROM lyrics ORDER BY id"))
    db.close()
    assert rows == [(None, None), (L3_REVERTED, "{}"), (L3_UNUSABLE, "{}"), ("failed", "{}")]


def test_upgrade_to_v9_redoes_songs_aligned_in_the_wrong_language(tmp_path):
    from lyricfloat.lyrics.cache import L3_GAVE_UP, L3_REVERTED, L3_UNUSABLE

    credited = json.dumps([[0.0, "作词 : 某人"], [17.0, "first line"], [21.0, "second line"]], ensure_ascii=False)
    chinese = json.dumps([[0.0, "作詞 : 某人"], [17.0, "這是第一句"]], ensure_ascii=False)
    english = json.dumps([[17.0, "first line"]])
    rows = [  # SPEC v1.82: only the English songs under Chinese credits, and not when reverted
        (credited, L3_UNUSABLE), (credited, L3_GAVE_UP), (credited, L3_APPLIED), (credited, L3_REVERTED),
        (chinese, L3_UNUSABLE), (english, L3_UNUSABLE),
    ]
    path = tmp_path / "cache.sqlite3"
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    for i, (lines, state) in enumerate(rows):
        db.execute(
            "INSERT INTO lyrics (artist_key, title_key, duration, kind, synced, lines_json, created_at, updated_at, l3_state, l3_json, l3_attempts) "
            "VALUES (?, 't', 100, 'synced', 1, ?, 0, 0, ?, '{}', 3)",
            (f"a{i}", lines, state),
        )
    db.execute("PRAGMA user_version=8")
    db.commit()
    db.close()
    SqliteCache(path).close()
    db = sqlite3.connect(path)
    got = list(db.execute("SELECT l3_state, l3_json, l3_attempts FROM lyrics ORDER BY id"))
    db.close()
    assert got == [(None, None, None)] * 3 + [(L3_REVERTED, "{}", 3), (L3_UNUSABLE, "{}", 3), (L3_UNUSABLE, "{}", 3)]


# -- SPEC v1.36: parts of a recording, and a worker that stays loaded -------------------------


def test_part_of_a_recording_is_read_in_whole_frames(tmp_path):
    import numpy as np

    from lyricfloat.align.worker import read_pcm

    path = tmp_path / "rec.pcm"
    np.arange(20, dtype=np.int16).tofile(path)  # 10 stereo frames: (0,1) (2,3) ...
    audio = read_pcm(str(path), 2, offset_bytes=9, length_bytes=13)  # rounded to frames 2..4
    assert audio.shape == (2, 3)
    assert (audio[0] * 32768).round().tolist() == [4, 6, 8]
    assert read_pcm(str(path), 2).shape == (2, 10)


def test_serve_answers_each_job_with_models_loaded_once():
    import json

    from lyricfloat.align.worker import serve

    loads, written = [], []
    requests = iter([json.dumps({"n": 1}), "not json", json.dumps({"n": 2, "boom": True}), ""])

    def run(request, models):
        if request.get("boom"):
            raise RuntimeError("bad clip")
        return {"ok": True, "n": request["n"], "models": models}

    serve({"serve": True}, lambda: next(requests), written.append, load=lambda job: loads.append(job) or "M", run=run)
    assert len(loads) == 1
    assert written == [
        {"ok": True, "ready": True},
        {"ok": True, "n": 1, "models": "M"},
        {"ok": False, "error": "unreadable job"},
        {"ok": False, "error": "RuntimeError: bad clip"},
    ]


def test_serve_reports_models_that_will_not_load():
    from lyricfloat.align.worker import serve

    written = []

    def load(job):
        raise OSError("no model")

    serve({"serve": True}, lambda: "", written.append, load=load)
    assert written == [{"ok": False, "error": "OSError: no model"}]


def test_an_old_worker_copy_in_the_runtime_is_replaced(tmp_path):
    from lyricfloat.align.runtime import WORKER, worker_for

    python = tmp_path / "python" / "python.exe"
    python.parent.mkdir()
    copy = tmp_path / WORKER.name
    copy.write_text("# an older worker", encoding="utf-8")
    assert worker_for(python) == copy
    assert copy.read_bytes() == WORKER.read_bytes()
    (tmp_path / WORKER.name).unlink()
    assert worker_for(python) == WORKER  # no runtime copy: the package's own
