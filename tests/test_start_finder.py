"""SPEC v1.36: finding where an MV starts singing while it plays. A fake worker and clock."""

import json
import logging
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from lyricfloat.align.anchor import CLIP, OVERLAP, StartFinder, StartSearch, plan_search
from lyricfloat.align.recorder import LiveRecording
from lyricfloat.models import LyricLine, TrackInfo

TRACK = TrackInfo("Paper Lanterns", "The Example Band", "", 200.0, "Chrome")
LINES = (LyricLine(20.0, "first line"), LyricLine(24.0, "second line"), LyricLine(28.0, "third line"))
SEARCH = StartSearch(LINES, ("first line", "second line", "third line"), "en", 110.0)
BYTES_PER_S = 44100 * 4


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def seg(start, p=0.6):
    return {"start": start, "words": [{"p": p}]} if start is not None else {"start": None, "words": []}


# -- the search ---------------------------------------------------------------------------


def test_where_the_start_can_be():
    assert SEARCH.low == -3.0 and SEARCH.high == 113.0
    assert SEARCH.wanted(8.0, 20.0) and not SEARCH.wanted(0.0, 12.0)  # before 17 s nothing can start
    # SPEC v1.44: the search goes on while the last searched line (28 s) can still come.
    assert SEARCH.wanted(138.0, 150.0) and not SEARCH.over(141.0) and SEARCH.over(141.5)


def judge(search, *clips):
    """The search's verdict on clips given as (song position of the clip, segments)."""
    return search.judge([e for pos0, segments in clips for e in search.sure(segments, pos0)])


def test_three_sure_lines_that_agree_give_the_start():
    assert judge(SEARCH, (55.0, [seg(5.0), seg(9.1), seg(13.0)])) == pytest.approx(40.0)
    assert judge(SEARCH, (55.0, [seg(5.0), seg(9.1), seg(None)])) is None  # two lines: not enough
    assert judge(SEARCH, (55.0, [seg(5.0), seg(9.1), seg(13.0, p=0.25)])) is None  # 0.25 is not sure (0.3)
    assert judge(SEARCH, (55.0, [seg(5.0), seg(10.5), seg(13.0)])) is None  # they disagree by 1.5 s
    assert judge(SEARCH, (10.0, [seg(1.0), seg(5.0), seg(9.0)])) is None  # -9 s: not a believable offset
    two = StartSearch(LINES[:2], ("first line", "second line"), "en", 110.0)
    assert judge(two, (55.0, [seg(5.0), seg(9.1)])) == pytest.approx(40.05)  # a song with two sung lines


def test_the_lines_may_come_from_different_clips_and_need_not_be_the_first():
    lines = (*LINES, LyricLine(40.0, "fourth line"), LyricLine(44.0, "fifth line"))
    five = StartSearch(lines, tuple(line.text for line in lines), "en", 110.0)
    none = [seg(None)] * 5
    # The singing starts 40 s late. A slow song: the first line in one clip, the next ones in
    # the clip after (SPEC v1.44).
    first = (52.0, [seg(8.0), *none[1:]])
    then = (62.0, [seg(None), seg(2.0), seg(6.1), seg(None), seg(None)])
    assert judge(five, first) is None and judge(five, first, then) == pytest.approx(40.0)
    # A weak first line does not sink the start the other lines agree on.
    weak = (52.0, [seg(8.0, p=0.24), *none[1:]])
    later = (72.0, [*none[:3], seg(8.0), seg(12.2)])
    assert judge(five, weak, then, later) == pytest.approx(40.05)
    # One line sure twice (overlapping clips, a chorus sung again) is still one line.
    again = (54.0, [seg(6.1), *none[1:]])
    assert judge(five, first, again, (62.0, [seg(None), seg(2.0), *none[2:]])) is None


def test_the_most_lines_that_agree():
    sure = [(0, 0.5, 21.7), (3, 0.6, 16.7), (0, 0.5, 40.7), (1, 0.3, 40.9), (3, 0.6, 40.9), (0, 0.4, 40.6)]
    assert SEARCH.agreeing(sure) == [(0, 0.5, 40.7), (1, 0.3, 40.9), (3, 0.6, 40.9)]  # the surest entry per line


def test_the_search_skips_credits_and_needs_two_sung_lines():
    lines = (LyricLine(0.0, "作詞 : 某人"), LyricLine(20.0, "這是第一句"), LyricLine(24.0, "這是第二句"))
    search = plan_search(lines, "Paper Lanterns", "The Example Band", 30.0, lambda t: t.replace("這", "这"))
    assert search.lines == lines[1:] and search.texts == ("这是第一句", "这是第二句") and search.language == "zh"
    assert plan_search(lines[:2], "", "", 30.0, lambda t: t) is None


def test_an_english_song_under_chinese_credits_is_searched_in_english():
    lines = (LyricLine(0.0, "作词 : 某人"), LyricLine(1.0, "作曲 : 某人"), LyricLine(17.0, "first line"), LyricLine(21.0, "second line"))
    search = plan_search(lines, "Paper Lanterns", "The Example Band", 24.0, lambda t: t.replace("line", "LINE"))
    assert search.language == "en" and search.texts == ("first line", "second line")  # SPEC v1.82: no t2s


# -- the finder -----------------------------------------------------------------------------


class FakeServing:
    def __init__(self, python, worker, models_dir, on_reply):
        self.on_reply = on_reply
        self.sent = []
        self.closed = False

    def send(self, request):
        self.sent.append(request)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def finder(qapp):
    clock = Clock()
    servings = []

    def launch(*args):
        servings.append(FakeServing(*args))
        return servings[-1]

    f = StartFinder(Path("models"), Path("python.exe"), Path("worker.py"), launch=launch, clock=clock)
    f.free_memory = lambda: 8 * 2**30
    f.clock, f.servings = clock, servings
    f.events = []
    f.found.connect(lambda track, offset: f.events.append(("found", offset)))
    f.searching.connect(lambda track, on: f.events.append(("searching", on)))
    f.missed.connect(lambda track: f.events.append(("missed",)))
    return f


def recording(clock, start_pos=1.0):
    """A recording that began at monotonic 1000 s, song position `start_pos`, written up to now."""
    return lambda: LiveRecording(Path("rec.pcm"), 1000.0, int((clock.now - 1000.0) * BYTES_PER_S), start_pos)


def run_until(f, upto):
    while f.clock.now < upto:
        f.clock.now += 0.5
        f.tick()


def test_clips_wait_for_the_worker_and_skip_the_part_before_any_start(finder):
    assert finder.start(TRACK, SEARCH, recording(finder.clock))
    serving = finder.servings[0]
    run_until(finder, 1015.0)
    assert serving.sent == []  # models still loading
    serving.on_reply({"ok": True, "ready": True})
    run_until(finder, 1021.0)
    # Clip 1 (song -1..11 s) cannot hold a start at 17 s or later; clip 2 (song 9..21 s) can.
    (request,) = serving.sent
    assert request["offset_bytes"] == int(8.0 * 44100) * 4 and request["length_bytes"] == int((CLIP + OVERLAP) * 44100) * 4
    assert request["lines"] == ["first line", "second line", "third line"] and request["pcm_path"] == "rec.pcm"


def test_a_sure_clip_ends_the_search_with_the_start(finder, caplog):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    serving = finder.servings[0]
    serving.on_reply({"ok": True, "ready": True})
    run_until(finder, 1021.0)
    assert len(serving.sent) == 1
    with caplog.at_level(logging.INFO, logger="lyricfloat.alignment"):
        serving.on_reply({"ok": True, "segments": [seg(None), seg(None), seg(None)]})  # nothing sure yet
        run_until(finder, 1031.0)
        assert len(serving.sent) == 2
        serving.on_reply({"ok": True, "segments": [seg(2.0), seg(6.1), seg(10.0)]})  # the clip from song 19 s
    assert finder.events == [("searching", True), ("found", pytest.approx(1.0)), ("searching", False)]
    assert serving.closed and not finder.active
    stats = json.loads([r.getMessage() for r in caplog.records if r.name == "lyricfloat.alignment"][-1])
    assert stats["event"] == "start_search" and stats["found"] and stats["clips"] == 2
    assert stats["agreeing_lines"] == [0, 1, 2]
    assert stats["confidences"] == [0.6, 0.6, 0.6] and stats["line_offsets_s"] == [1.0, 1.1, 1.0]


def answer(finder, *segments):
    """The worker's reply to the clip sent, then the clock runs to the next clip."""
    finder.servings[0].on_reply({"ok": True, "segments": list(segments)})
    run_until(finder, finder.clock.now + CLIP)


def test_sure_lines_add_up_over_the_clips(finder, caplog):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    finder.servings[0].on_reply({"ok": True, "ready": True})
    run_until(finder, 1021.0)
    # The singing starts 15 s late: the lines at song 35, 39 and 43 s.
    answer(finder, seg(None), seg(None), seg(None))  # clip at song 9 s
    answer(finder, seg(3.0), seg(None), seg(None))  # clip at song 19 s: the first line sure at +2 s, wrongly
    with caplog.at_level(logging.INFO, logger="lyricfloat.alignment"):
        answer(finder, seg(6.0), seg(10.0, p=0.25), seg(None))  # clip at song 29 s: the first line at +15 s
        assert finder.active
        finder.servings[0].on_reply({"ok": True, "segments": [seg(None), seg(0.1), seg(4.0)]})  # clip at song 39 s
    assert finder.events[-2:] == [("found", pytest.approx(15.0)), ("searching", False)]
    stats = json.loads([r.getMessage() for r in caplog.records if r.name == "lyricfloat.alignment"][-1])
    assert stats["agreeing_lines"] == [0, 1, 2] and stats["line_offsets_s"] == [15.0, 15.1, 15.0]


def test_a_miss_records_the_most_lines_that_agreed(finder, caplog):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    serving = finder.servings[0]
    serving.on_reply({"ok": True, "ready": True})
    run_until(finder, 1021.0)
    answer(finder, seg(None), seg(None), seg(None))  # clip at song 9 s
    serving.on_reply({"ok": True, "segments": [seg(2.0), seg(6.1), seg(10.0, p=0.25)]})  # two lines agree, one weak
    with caplog.at_level(logging.INFO, logger="lyricfloat.alignment"):
        for _ in range(40):
            run_until(finder, finder.clock.now + CLIP)
            if finder.active:
                serving.on_reply({"ok": True, "segments": [seg(None)] * 3})
    stats = json.loads([r.getMessage() for r in caplog.records if r.name == "lyricfloat.alignment"][-1])
    assert not stats["found"] and stats["agreeing_lines"] == [0, 1] and stats["line_offsets_s"] == [1.0, 1.1]


def test_the_search_gives_up_once_no_clip_can_hold_the_start(finder):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    serving = finder.servings[0]
    serving.on_reply({"ok": True, "ready": True})
    for _ in range(40):
        run_until(finder, finder.clock.now + CLIP)
        if finder.active and len(serving.sent) > 0:
            serving.on_reply({"ok": True, "segments": [seg(None)] * 3})
    assert finder.events[-2:] == [("missed",), ("searching", False)] and not finder.active


def test_stopping_or_losing_the_recording_ends_quietly(finder):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    finder.stop()
    assert finder.events == [("searching", True), ("searching", False)] and finder.servings[0].closed
    finder.start(TRACK, SEARCH, lambda: None)  # paused or sought: the recording no longer counts
    finder.servings[1].on_reply({"ok": True, "ready": True})
    assert not finder.active and ("missed",) not in finder.events


def test_a_worker_that_dies_counts_as_a_miss(finder):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    finder.servings[0].on_reply({"ok": False, "error": "worker exited", "gone": True})
    assert finder.events[-2] == ("missed",)


def test_models_gone_from_the_cache_end_the_search_quietly_and_are_reported(finder):
    """SPEC v1.85: the worker does not download them; nothing was searched, so it is not a miss."""
    missing = []
    finder.modelsMissing.connect(lambda: missing.append(True))
    finder.start(TRACK, SEARCH, recording(finder.clock))
    finder.servings[0].on_reply({"ok": False, "error": "ModelsMissing: Whisper base is not in models", "models_missing": True})
    assert missing == [True] and not finder.active and finder.servings[0].closed
    assert finder.events == [("searching", True), ("searching", False)]


def test_no_search_without_runtime_or_memory(finder):
    finder.free_memory = lambda: 2**30
    assert not finder.start(TRACK, SEARCH, recording(finder.clock))
    finder.free_memory, finder.python = (lambda: 8 * 2**30), None
    assert not finder.start(TRACK, SEARCH, recording(finder.clock))
    assert finder.servings == [] and finder.events == []


def test_a_worker_falling_behind_goes_to_the_newest_clip(finder):
    finder.start(TRACK, SEARCH, recording(finder.clock))
    serving = finder.servings[0]
    finder.clock.now = 1045.0  # the models took 45 s to load
    serving.on_reply({"ok": True, "ready": True})
    (request,) = serving.sent
    assert request["offset_bytes"] == int(28.0 * 44100) * 4  # the clip ending at 40 s, not the one ending at 20 s
