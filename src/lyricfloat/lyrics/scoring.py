"""Candidate scoring (SPEC v1.4, matching rules).

Weights and threshold are parameters so they can be recalibrated once the
200-song test list exists; every scored candidate is logged for that purpose.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from lyricfloat.lyrics.normalize import other_language, split_artists, title_variants
from lyricfloat.models import Candidate, TrackInfo


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    title_weight: float = 0.5
    artist_weight: float = 0.3
    duration_weight: float = 0.2
    threshold: float = 0.7
    max_duration_diff: float = 3.0  # seconds; beyond this a candidate is rejected
    preferred_duration_diff: float = 2.0  # L1: within this, a candidate ranks ahead (SPEC v1.14)
    title_only_artist_min: float = 0.5  # a hit found by title alone needs this artist match (SPEC v1.33)
    # A version of another length, and any hit for a browser video, needs the same artist: this
    # much match, or one name holding the other, or (videos) its artist named in the video.
    # SPEC v1.58 had 0.5, too loose for short names: PG One and Pi-re are 0.55 (SPEC v1.61).
    artist_min: float = 0.8
    # SPEC v1.63: a match with a title this unlike yields to a version of another length by the
    # same artist whose title is this alike (BIGBANG's MAKE LOVE, 0.56, for This Love).
    weak_title: float = 0.8
    same_title: float = 0.95


DEFAULT_CONFIG = ScoringConfig()


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    title_sim: float
    artist_sim: float
    duration_score: float | None  # None when either side has no duration
    rejected: str | None = None
    # L1 (SPEC v1.14): duration within 2 s of the player's. True when the player gave no
    # duration (nothing to rank on); False when only the candidate's duration is missing.
    duration_preferred: bool = True
    artist_named: bool = False  # a browser video names one of its artists (SPEC v1.58)
    artist_match: bool = False  # the same artist (SPEC v1.61): see ScoringConfig.artist_min

    @property
    def accepted(self) -> bool:
        return self.rejected is None


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.ratio(a, b, processor=default_process) / 100.0


TitleTransform = Callable[[str], str]


# Taiwan writes 妳 for "you" said to a woman; mainland titles use 你, and t2s keeps 妳 (SPEC v1.32).
# Compared only: cache keys keep the title as it is.
_SAME_PRONOUN = str.maketrans({"妳": "你"})


def title_similarity(a: str, b: str, transform: TitleTransform | None = None, artist: str = "") -> float:
    """Best match between the titles' variants: the full name and, for bilingual
    titles, its Chinese and English parts (SPEC v1.10). `transform` puts both
    sides in the same script first (t2s for simplified sources). A part of `a`
    that is just the artist's name is not a title (SPEC v1.37: "王水源 Lifloo"
    split into 王水源 matched a song called 愛王水源)."""
    if transform is not None:
        a, b, artist = transform(a), transform(b), transform(artist)
    a, b = a.translate(_SAME_PRONOUN), b.translate(_SAME_PRONOUN)
    names = set(split_artists(artist)) if artist else set()
    ours = [v for v in title_variants(a) if v not in names] or title_variants(a)
    return max(_similarity(x, y) for x in ours for y in title_variants(b))


_SPACE = re.compile(r"\s+")


def same_artist(a: str, b: str, sim: float, cfg: ScoringConfig = DEFAULT_CONFIG,
                transform: TitleTransform | None = None) -> bool:
    """SPEC v1.61: close enough (cfg.artist_min), or one name holds the other: 鄧紫棋 is
    "G.E.M. 鄧紫棋" (0.46 by similarity), while PG One is not Pi-re (0.55)."""
    if sim >= cfg.artist_min - 1e-9:
        return True
    if transform is not None:
        a, b = transform(a), transform(b)
    left = [_SPACE.sub("", n) for n in split_artists(a)]
    right = [_SPACE.sub("", n) for n in split_artists(b)]
    return any(len(x) >= 2 and len(y) >= 2 and (x in y or y in x) for x in left for y in right)


def artist_similarity(a: str, b: str) -> float:
    left, right = split_artists(a), split_artists(b)
    return max((_similarity(x, y) for x in left for y in right), default=0.0)


def duration_score(query: float, candidate: float, cfg: ScoringConfig = DEFAULT_CONFIG) -> float | None:
    if query <= 0 or candidate <= 0:
        return None
    return max(0.0, 1.0 - abs(query - candidate) / cfg.max_duration_diff)


def score_candidate(
    track: TrackInfo,
    cand: Candidate,
    cfg: ScoringConfig = DEFAULT_CONFIG,
    require_lyrics: bool = True,
    title_transform: TitleTransform | None = None,
) -> ScoredCandidate:
    t_sim = title_similarity(track.title, cand.title, title_transform, track.artist)
    a_sim = artist_similarity(track.artist, cand.artist)
    d_score = duration_score(track.duration, cand.duration, cfg)

    if d_score is None:
        # Player gave no duration: renormalize the remaining weights (5/8, 3/8 by default).
        rest = cfg.title_weight + cfg.artist_weight
        score = (t_sim * cfg.title_weight + a_sim * cfg.artist_weight) / rest
    else:
        score = t_sim * cfg.title_weight + a_sim * cfg.artist_weight + d_score * cfg.duration_weight

    rejected = None
    if cand.title_only and a_sim < cfg.title_only_artist_min - 1e-9:
        rejected = "artist"  # a song of the same name by someone else (SPEC v1.33)
    elif other_language(track.title, cand.title):
        rejected = "language"  # its lyrics are in another language (SPEC v1.70)
    elif d_score is not None and abs(track.duration - cand.duration) > cfg.max_duration_diff:
        rejected = "duration"
    elif score < cfg.threshold - 1e-9:
        rejected = "below_threshold"
    elif require_lyrics and not (cand.has_synced or cand.has_plain or cand.instrumental):
        rejected = "empty"
    if track.duration <= 0:
        preferred = True
    elif cand.duration <= 0:
        preferred = False
    else:
        preferred = abs(track.duration - cand.duration) <= cfg.preferred_duration_diff + 1e-9
    match = same_artist(track.artist, cand.artist, a_sim, cfg, title_transform)
    return ScoredCandidate(cand, round(score, 4), round(t_sim, 4), round(a_sim, 4), d_score, rejected, preferred,
                           artist_match=match)


def metadata_score(sc: ScoredCandidate, cfg: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Title and artist alone, weighted as when the player gives no duration (5/8, 3/8)."""
    return (sc.title_sim * cfg.title_weight + sc.artist_sim * cfg.artist_weight) / (cfg.title_weight + cfg.artist_weight)


def other_length_usable(sc: ScoredCandidate, cfg: ScoringConfig = DEFAULT_CONFIG) -> bool:
    """SPEC v1.29: turned away for its length alone, with title and artist good enough on
    their own. The player may be playing the MV, whose intro and outro make it longer."""
    return (
        sc.rejected == "duration"
        and metadata_score(sc, cfg) >= cfg.threshold - 1e-9
        # SPEC v1.58: with the title alone 0.2 of artist match was enough (语音备忘录 by Pi-re for PG One's)
        and (sc.artist_match or sc.artist_named)
    )


def _rank(sc: ScoredCandidate) -> tuple[int, int, float]:
    """Lyrics type first (synced > plain > instrumental), then L1's duration
    preference (within 2 s), then score."""
    c = sc.candidate
    tier = 2 if c.has_synced else 1 if c.has_plain else 0
    return (tier, int(sc.duration_preferred), sc.score)


def score_all(
    track: TrackInfo,
    candidates: list[Candidate],
    cfg: ScoringConfig = DEFAULT_CONFIG,
    require_lyrics: bool = True,
    title_transform: TitleTransform | None = None,
) -> list[ScoredCandidate]:
    """Score every candidate; accepted ones first, best first.

    `require_lyrics=False` scores on metadata only, for sources whose hits
    carry no lyrics until fetched.
    """
    return ranked([score_candidate(track, c, cfg, require_lyrics, title_transform) for c in candidates])


def ranked(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Accepted ones first, best first."""
    return sorted(scored, key=lambda s: (s.accepted, *_rank(s)), reverse=True)


def pick_best(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    """Synced beats plain-only whenever both pass the threshold, regardless of score."""
    accepted = [s for s in scored if s.accepted]
    return max(accepted, key=_rank) if accepted else None
