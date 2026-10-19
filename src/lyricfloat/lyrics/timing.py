"""Timeline correction hook (spec section "自動時間軸校正").

M1 applies only the LRC [offset:] tag, which the parser already handles.
L1 (duration preference, multi-source outlier removal), L2 (speech
anchors) and L3 (forced alignment) attach here from M2 onward.
"""

from __future__ import annotations

from typing import Protocol

from lyricfloat.models import Candidate, Lyrics, TrackInfo


class TimingCorrector(Protocol):
    def correct(self, track: TrackInfo, lyrics: Lyrics, candidate: Candidate) -> Lyrics: ...


class NoCorrection:
    def correct(self, track: TrackInfo, lyrics: Lyrics, candidate: Candidate) -> Lyrics:
        return lyrics
