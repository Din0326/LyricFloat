import pytest

from lyricfloat.lyrics.scoring import (
    ScoringConfig,
    artist_similarity,
    duration_score,
    pick_best,
    score_all,
    score_candidate,
)
from lyricfloat.models import Candidate, TrackInfo

TRACK = TrackInfo(title="Paper Lanterns", artist="The Example Band", album="Lights", duration=200.0)
SYNCED = "[00:01.00]first line\n[00:05.00]second line"


def cand(cid="1", title="Paper Lanterns", artist="The Example Band", duration=200.0, synced=SYNCED, plain="first line", instrumental=False):
    return Candidate(
        source="test",
        source_id=cid,
        title=title,
        artist=artist,
        duration=duration,
        synced_lrc=synced,
        plain_text=plain,
        instrumental=instrumental,
    )


def test_perfect_match_scores_one():
    sc = score_candidate(TRACK, cand())
    assert sc.score == pytest.approx(1.0)
    assert sc.accepted


def test_version_notes_do_not_hurt_the_title_score():
    sc = score_candidate(TRACK, cand(title="Paper Lanterns (2011 Remaster)"))
    assert sc.title_sim == pytest.approx(1.0)


@pytest.mark.parametrize("diff, expected", [(0, 1.0), (1.5, 0.5), (3.0, 0.0)])
def test_duration_score_is_linear_up_to_three_seconds(diff, expected):
    assert duration_score(200.0, 200.0 + diff) == pytest.approx(expected)


def test_duration_over_three_seconds_is_rejected_even_with_perfect_text():
    sc = score_candidate(TRACK, cand(duration=203.5))
    assert sc.rejected == "duration"
    assert not sc.accepted


def test_exactly_three_seconds_is_not_rejected_by_duration():
    sc = score_candidate(TRACK, cand(duration=203.0))
    assert sc.rejected is None
    assert sc.score == pytest.approx(0.8)


@pytest.mark.parametrize("track_dur, cand_dur", [(0.0, 200.0), (200.0, 0.0)])
def test_missing_duration_reweights_title_and_artist(track_dur, cand_dur):
    track = TrackInfo("Paper Lanterns", "The Example Band", duration=track_dur)
    sc = score_candidate(track, cand(duration=cand_dur))
    assert sc.duration_score is None
    assert sc.score == pytest.approx(1.0)


def test_missing_duration_weights_are_five_and_three_eighths():
    track = TrackInfo("Paper Lanterns", "Nobody At All", duration=0.0)
    sc = score_candidate(track, cand())
    expected = (1.0 * 5 + sc.artist_sim * 3) / 8
    assert sc.score == pytest.approx(expected, abs=1e-4)


def test_below_threshold_is_rejected():
    sc = score_candidate(TRACK, cand(title="Completely Different Song", artist="Another Group"))
    assert sc.rejected == "below_threshold"


def test_threshold_is_configurable():
    cfg = ScoringConfig(threshold=0.95)
    sc = score_candidate(TRACK, cand(duration=201.5), cfg)  # 0.5 + 0.3 + 0.1 = 0.9
    assert sc.rejected == "below_threshold"


def test_candidate_without_any_lyrics_is_rejected():
    sc = score_candidate(TRACK, cand(synced=None, plain=None))
    assert sc.rejected == "empty"


def test_artist_similarity_uses_best_pair():
    assert artist_similarity("Alpha & The Example Band", "The Example Band") == pytest.approx(1.0)


def test_synced_beats_plain_even_with_lower_score():
    plain_perfect = cand("plain", synced=None)
    synced_ok = cand("synced", duration=201.0)  # score ~0.933
    best = pick_best(score_all(TRACK, [plain_perfect, synced_ok]))
    assert best.candidate.source_id == "synced"


def test_plain_is_used_when_no_synced_passes():
    plain = cand("plain", synced=None)
    bad_synced = cand("bad", title="Other Song", artist="Other Band")
    best = pick_best(score_all(TRACK, [bad_synced, plain]))
    assert best.candidate.source_id == "plain"


def test_higher_score_wins_within_same_tier():
    a = cand("a", duration=202.0)
    b = cand("b", duration=200.5)
    assert pick_best(score_all(TRACK, [a, b])).candidate.source_id == "b"


def test_instrumental_ranks_below_lyrics():
    inst = cand("inst", synced=None, plain=None, instrumental=True)
    plain = cand("plain", synced=None, duration=201.0)
    assert pick_best(score_all(TRACK, [inst, plain])).candidate.source_id == "plain"
    assert pick_best(score_all(TRACK, [inst])).candidate.source_id == "inst"


def test_no_accepted_candidate_returns_none():
    assert pick_best(score_all(TRACK, [cand(duration=250.0)])) is None
    assert pick_best([]) is None


def test_score_all_puts_accepted_first():
    rejected = cand("rejected", duration=260.0)
    accepted = cand("accepted", synced=None)
    ordered = score_all(TRACK, [rejected, accepted])
    assert [s.candidate.source_id for s in ordered] == ["accepted", "rejected"]


# --- bilingual titles and script unification (SPEC v1.10) -----------------------------

from lyricfloat.lyrics.chinese import ChineseConverter  # noqa: E402
from lyricfloat.lyrics.scoring import title_similarity  # noqa: E402


def test_bilingual_title_matches_either_part():
    assert title_similarity("紙燈籠 Paper Lanterns", "紙燈籠") == pytest.approx(1.0)
    assert title_similarity("紙燈籠 Paper Lanterns", "Paper Lanterns") == pytest.approx(1.0)
    assert title_similarity("紙燈籠", "紙燈籠 Paper Lanterns") == pytest.approx(1.0)


def test_plain_titles_score_as_before():
    from rapidfuzz import fuzz
    from rapidfuzz.utils import default_process

    before = fuzz.ratio("paper lanterns", "paper lantern", processor=default_process) / 100
    assert title_similarity("Paper Lanterns", "Paper Lantern") == pytest.approx(before)


def test_script_unification_for_simplified_sources():
    cc = ChineseConverter()
    t2s = lambda s: cc.convert("t2s", s)  # noqa: E731
    query, candidate = "纸灯笼", "紙燈籠"  # same title, different script
    assert title_similarity(query, candidate) == pytest.approx(0.0)
    assert title_similarity(query, candidate, t2s) == pytest.approx(1.0)



# --- L1: duration within 2 s ranks first (SPEC v1.14) ---------------------------------


def test_close_duration_beats_a_higher_score_further_away():
    far = cand("far", duration=202.5)  # perfect title and artist, 2.5 s off
    close = Candidate("test", "close", "Paper Lantern", "Example Band", "", 202.0, SYNCED, None)  # weaker text, 2.0 s off
    far_sc, close_sc = score_candidate(TRACK, far), score_candidate(TRACK, close)
    assert far_sc.accepted and close_sc.accepted
    assert far_sc.score > close_sc.score  # by score alone the far one would win
    assert pick_best(score_all(TRACK, [far, close])).candidate.source_id == "close"


def test_two_second_boundary_is_inclusive():
    assert score_candidate(TRACK, cand(duration=202.0)).duration_preferred
    assert not score_candidate(TRACK, cand(duration=202.1)).duration_preferred


def test_synced_still_beats_plain_even_when_plain_is_closer():
    plain_close = cand("plain", synced=None, duration=200.0)
    synced_far = cand("synced", duration=202.5)
    assert pick_best(score_all(TRACK, [plain_close, synced_far])).candidate.source_id == "synced"


def test_no_player_duration_means_score_alone_decides():
    track = TrackInfo("Paper Lanterns", "The Example Band", duration=0.0)
    better = cand("better")
    worse = cand("worse", title="Paper Lantern")
    assert pick_best(score_all(track, [worse, better])).candidate.source_id == "better"


def test_candidate_without_duration_is_not_preferred():
    sc = score_candidate(TRACK, cand(duration=0.0))
    assert sc.accepted and not sc.duration_preferred
    assert pick_best(score_all(TRACK, [cand("unknown", duration=0.0), cand("known", duration=201.5)])).candidate.source_id == "known"


def test_taiwan_and_mainland_you_compare_as_one():
    # SPEC v1.32: NetEase files "妳" titles under "你", and t2s leaves 妳 alone.
    assert title_similarity("沒有妳在", "沒有你在") == pytest.approx(1.0)
    assert title_similarity("妳 Rosie", "你") == pytest.approx(1.0)


def test_a_hit_found_by_title_alone_needs_the_artist():
    # SPEC v1.33: "妳" alone brings up 16 songs; only the artist tells them apart.
    from dataclasses import replace as _replace

    from lyricfloat.lyrics.scoring import score_candidate
    from lyricfloat.models import Candidate, TrackInfo

    track = TrackInfo("妳 Rosie", "張瑚HU, 趙翊帆yi94", "", 220.2)
    ours = Candidate("lrclib", "1", "妳", "趙翊帆", "", 220.0, "[00:01.00]x", title_only=True)
    theirs = _replace(ours, source_id="2", artist="Someone Else")
    assert score_candidate(track, ours).accepted
    assert score_candidate(track, theirs).rejected == "artist"
    assert score_candidate(track, _replace(theirs, title_only=False)).rejected != "artist"  # the usual rules otherwise


def test_a_title_part_that_is_the_artist_is_not_a_title():
    # SPEC v1.37: "王水源 Lifloo" splits into 王水源 and lifloo; 王水源 is the singer, so a song
    # called 愛王水源 must not match on it.
    assert title_similarity("王水源 Lifloo", "愛王水源") > 0.8  # without the artist, the old trap
    assert title_similarity("王水源 Lifloo", "愛王水源", artist="王水源") < 0.5
    assert title_similarity("紙燈籠 Paper Lanterns", "紙燈籠", artist="The Example Band") == pytest.approx(1.0)
