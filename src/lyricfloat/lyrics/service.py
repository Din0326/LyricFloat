"""LyricService: cache first, then sources in priority order.

LyricResolver is plain asyncio (unit-testable). LyricService wraps it for
the Qt side: one in-flight query at a time, the old one is cancelled on a
track change, and a generation counter keeps a late result from ever
overwriting the current song.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace
from typing import TypeVar

from PyQt6.QtCore import QObject, pyqtSignal

from lyricfloat.aio import spawn
from lyricfloat.align.apply import Plan, lines_hash, plan, plan_from_json, plan_plain, plan_to_json
from lyricfloat.lyrics.cache import (
    EDITED_SOURCE,
    L3_APPLIED,
    L3_FAILED,
    L3_REVERTED,
    L3_UNUSABLE,
    CachedEntry,
    LyricCache,
    NullCache,
)
from lyricfloat.lyrics.identify import (
    confirms,
    credit_query,
    credits_both,
    chinese_part,
    is_browser,
    joined,
    names_artist,
    not_a_song,
    song_key,
    video_queries,
)
from lyricfloat.lyrics.language import Language, detect_from_lyrics, detect_from_metadata
from lyricfloat.lyrics.lrc import format_lrc, parse_lrc, parse_plain
from lyricfloat.lyrics.normalize import (
    channel_artist,
    credited_artists,
    other_performance,
    dash_credit,
    searchable_title,
    split_bilingual,
)
from lyricfloat.lyrics.pipeline import PassthroughPipeline, TextPipeline
from lyricfloat.lyrics.scoring import (
    DEFAULT_CONFIG,
    ScoredCandidate,
    ScoringConfig,
    metadata_score,
    other_length_usable,
    pick_best,
    ranked,
    score_all,
    score_candidate,
)
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.lyrics.sources.lrclib import REQUEST_TIMEOUT
from lyricfloat.lyrics.timing import NoCorrection, TimingCorrector
from lyricfloat.lyrics.translation import (
    L1_OFFSET_LIMIT,
    align_same_source,
    align_translations,
    median_offset,
    parse_translation,
)
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo

log = logging.getLogger(__name__)
match_log = logging.getLogger("lyricfloat.match")  # JSON lines, kept for weight calibration

T = TypeVar("T")

AD_MAX_DURATION = 60.0
MAX_FETCH = 3  # lyric fetches per lazy source (NetEase): each one is a request to a non-public endpoint
MANUAL_PARALLEL = 3  # free-text requests at a time per source in a manual search (SPEC v1.77)
MAX_VERSIONS = 4  # other versions kept for the L1 consensus check
MAX_RECORDED_SONGS = 50  # candidate records kept in memory for problem reports (FR-25)
RETRY_AFTER_FAILURE = 10.0  # seconds; one more try in the background when a source failed (SPEC v1.23)
_FAILED = object()  # a lookup that broke, as opposed to one that found nothing
TRANSLATOR = "netease"  # the only source with translations (SPEC: English songs get NetEase translation lines)

# Query order by song language (SPEC, lyric sources). Sources not built yet are simply absent.
SOURCE_ORDER: dict[Language, tuple[str, ...]] = {
    Language.EN: ("lrclib", "spotify", "netease"),
    Language.ZH: ("spotify", "lrclib", "netease", "kugou"),
}


class _CachedSource(LyricSource):
    """Stands in for the original source when rebuilding a cached entry offline."""

    name = "cache"

    def __init__(self, simplified: bool) -> None:
        self.simplified = simplified  # instance attribute shadows the class default

    async def search(self, track: TrackInfo) -> list[Candidate]:
        return []


_SIMPLIFIED = _CachedSource(True)  # puts text in simplified script for comparisons


@dataclass
class _Identifying:
    """What one identification has done so far (SPEC v1.34)."""

    broke: bool = False
    seen: set[tuple[str, str]] = field(default_factory=set)
    fetches: dict[str, int] = field(default_factory=dict)


def visibly_changed(old: LyricResult, new: LyricResult) -> bool:
    """Whether the screen has to change: other lines (version switch) or new translations."""
    if old.lyrics is None or new.lyrics is None:
        return old.lyrics is not new.lyrics
    if new.lyrics.lines != old.lyrics.lines:
        return True
    return new.lyrics.has_translation and new.lyrics.translations != old.lyrics.translations


def _manual_rank(sc: ScoredCandidate) -> tuple:
    """Manual search order: usable before rejected, synced before plain, then score.

    Hits from sources that list songs without lyrics count as plain until fetched.
    """
    c = sc.candidate
    tier = 2 if c.has_synced else 0 if c.instrumental else 1
    return (sc.accepted, tier, sc.duration_preferred, sc.score)


def is_probable_ad(track: TrackInfo) -> bool:
    """Spotify free-tier ads: no album and shorter than 60 s.

    Spotify sessions only: YouTube Music in Chrome never reports an album,
    so the rule would skip every short song there (SPEC v1.5).
    """
    if "spotify" not in track.source_app.casefold():
        return False
    return not track.album.strip() and 0 < track.duration < AD_MAX_DURATION


class LyricResolver:
    def __init__(
        self,
        sources: Sequence[LyricSource],
        cache: LyricCache | None = None,
        pipeline: TextPipeline | None = None,
        corrector: TimingCorrector | None = None,
        scoring: ScoringConfig = DEFAULT_CONFIG,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.sources = list(sources)
        self.cache = cache or NullCache()
        self.pipeline = pipeline or PassthroughPipeline()
        self.corrector = corrector or NoCorrection()
        self.scoring = scoring
        self.request_timeout = request_timeout
        self._misses: set[tuple[str, str, str]] = set()  # NOT_FOUND this session only (SPEC v1.6)
        self._records: dict[tuple[str, str, str], list[dict]] = {}  # scored candidates per song, this session
        self._recording: tuple[str, str, str] | None = None

    @staticmethod
    def searchable(track: TrackInfo) -> TrackInfo:
        """The track as sources are asked and candidates scored: a YouTube video title cut
        down to the song's own title (SPEC v1.31), and a channel named after the artist cut down
        to the artist ("ICE杨长青 - Topic", SPEC v1.54). Cache and screen keep the player's text."""
        browser = is_browser(track.source_app)
        channel = channel_artist(track.artist) if browser else track.artist
        title = searchable_title(track.title, channel)
        # Other artists the title names count when comparing; the player's stays first,
        # the one sources are asked with (SPEC v1.33).
        artists = [channel, *credited_artists(track.title, channel)]
        credit = dash_credit(track.title, channel) if browser else None
        if credit is not None:
            # SPEC v1.66: a label's or lyrics channel's "Artist - Song": ask with the artist
            # before the dash; the channel still counts when comparing.
            singer, title = credit
            artists = [singer, channel]
        artist = ", ".join(artists)
        return track if (title, artist) == (track.title, track.artist) else replace(track, title=title, artist=artist)

    @staticmethod
    def search_terms(track: TrackInfo, result: LyricResult | None) -> TrackInfo:
        """What the manual search starts from (FR-12). A browser video whose lyrics were found
        starts from the song they were found under: its title is still the video's, and the
        channel is often a label's (SPEC v1.76). Any other video starts from its whole title
        with no artist, searched as free text: which part is the singer is not guessed (SPEC
        v1.77). A music app's own title and artist are the song's."""
        if not is_browser(track.source_app):
            return LyricResolver.searchable(track)
        cand = result.candidate if result is not None else None
        if cand is not None and cand.title and cand.source != EDITED_SOURCE:
            return replace(track, title=cand.title, artist=cand.artist, album="")
        return replace(track, artist="", album="")

    def order_sources(self, track: TrackInfo) -> list[LyricSource]:
        """Enabled sources in the spec's order for this song's language.

        The spec only defines English and Chinese orders; other languages use
        the English one. Sources missing from the table keep their place at the end.
        """
        language = detect_from_metadata(track.title, track.artist)
        order = SOURCE_ORDER.get(language, SOURCE_ORDER[Language.EN])
        rank = {name: i for i, name in enumerate(order)}
        return sorted(self.sources, key=lambda s: rank.get(s.name, len(order)))

    def _source_named(self, name: str) -> LyricSource | None:
        return next((s for s in self.sources if s.name == name), None)

    async def _call(self, source: LyricSource, op: str, awaitable: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(self.request_timeout):
                return await awaitable
        except TimeoutError as exc:
            raise SourceError(f"{source.name} {op}: timed out after {self.request_timeout}s") from exc
        except SourceError:
            raise
        except Exception as exc:  # a buggy source must not take the others down
            log.exception("%s %s crashed", source.name, op)
            raise SourceError(f"{source.name} {op}: {type(exc).__name__}") from exc

    def _title_transform(self, source: LyricSource):
        """Simplified sources: compare titles in simplified on both sides (SPEC v1.10)."""
        if not source.simplified:
            return None
        return lambda text: self.pipeline.comparison_text(text, source)

    def _score(self, source: LyricSource, query: TrackInfo, candidates, require_lyrics: bool = True):
        scored = score_all(query, list(candidates), self.scoring, require_lyrics, self._title_transform(source))
        return self._artist_evident(query, scored)

    def _artist_evident(self, query: TrackInfo, scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
        """SPEC v1.58: a browser video's artist field is a channel, often a lyrics channel's, so
        title and length alone took songs of the same name by others (covers, remixes). A hit
        for a video needs the field's artist (scoring.same_artist, SPEC v1.61) or one named in
        the video's title."""
        if not is_browser(query.source_app):
            return scored
        fold = lambda text: self.pipeline.comparison_text(text, _SIMPLIFIED)  # noqa: E731
        out = []
        for sc in scored:
            named = names_artist(query, sc.candidate, fold)
            if sc.rejected in (None, "duration") and not named and not sc.artist_match:
                sc = replace(sc, rejected="artist")
            out.append(replace(sc, artist_named=named))
        return ranked(out)

    async def _query_source(
        self, source: LyricSource, query: TrackInfo
    ) -> tuple[ScoredCandidate | None, list[ScoredCandidate] | None]:
        """Best candidate plus everything scored; the list is None when the exact lookup
        answered alone (other versions unknown).

        Bilingual titles ("想講你知 Gotta Say It"): when the source has nothing above
        the threshold for the full title, ask again with the Chinese part only
        (LRCLIB files such songs under the Chinese name; on NetEase the English
        part can bury the song under an artist of that name, SPEC v1.32).
        Candidates are still scored against the full title.
        """
        best, scored = await self._query_once(source, query, query, "search")
        if best is not None:
            return best, scored
        parts = split_bilingual(query.title)
        if not parts:
            return best, scored
        try:
            retry_best, retry_scored = await self._query_once(source, replace(query, title=parts[0]), query, "search-chinese-part")
        except SourceError as exc:
            log.warning("chinese-part retry failed: %s", exc)
            return best, scored
        if retry_best is not None:
            log.info("found %s under its Chinese title %r", query.describe(), parts[0])
            return retry_best, retry_scored
        # Versions of another length found under the Chinese part still count (SPEC v1.66): LRCLIB
        # files 沒有妳在 (206 s) under that name only, and the video "沒有妳在 Not Around" is 211 s.
        # Only those named that part: in "Comedy / 星野源" it is the singer, and his Melody is no Comedy.
        if scored is None or retry_scored is None:
            return best, scored
        part = replace(query, title=parts[0])
        named = {
            sc.candidate.source_id
            for sc in self._score(source, part, [s.candidate for s in retry_scored], require_lyrics=False)
            if sc.title_sim >= self.scoring.same_title - 1e-9
        }
        seen = {sc.candidate.source_id for sc in scored}
        extra = [sc for sc in retry_scored if sc.candidate.source_id in named and sc.candidate.source_id not in seen]
        return best, [*scored, *extra]

    async def _query_once(
        self, source: LyricSource, fetch: TrackInfo, query: TrackInfo, stage: str
    ) -> tuple[ScoredCandidate | None, list[ScoredCandidate] | None]:
        """One exact lookup plus search with `fetch`; candidates scored against `query`."""
        candidates: list[Candidate] = []
        errors: list[str] = []

        try:
            exact = await self._call(source, "lookup", source.lookup(fetch))
        except SourceError as exc:
            errors.append(str(exc))
            exact = None
        if exact is not None:
            candidates.append(exact)
            scored = self._score(source, query, candidates)
            best = pick_best(scored)
            if best and best.candidate.has_synced:
                self._log_match(source, query, stage.replace("search", "lookup"), scored, best)
                return best, None

        try:
            found = await self._call(source, "search", source.search(fetch))
            # The exact lookup's entry often comes back in the search too: list it once.
            seen = {c.source_id for c in candidates}
            candidates.extend(c for c in found if c.source_id not in seen)
        except SourceError as exc:
            errors.append(str(exc))
            if pick_best(self._score(source, query, candidates)) is None:
                # Search broke and lookup gave nothing usable: we cannot tell "not found".
                raise SourceError("; ".join(errors)) from exc

        lazy = not source.lyrics_inline
        scored = self._score(source, query, candidates, require_lyrics=not lazy)
        if lazy:
            scored = await self._fetch_top(source, query, scored)
        best = pick_best(scored)
        self._log_match(source, query, stage, scored, best, errors)
        return best, scored

    def _studio_version(self, track: TrackInfo, source: LyricSource, scored: list[ScoredCandidate] | None) -> LyricResult | None:
        """A synced version this source accepted that is not a live one the title does not name (v1.75)."""
        for sc in scored or ():
            if sc.accepted and sc.candidate.has_synced and not other_performance(track.title, sc.candidate.title):
                result = self._build(track, source, sc)
                if result is not None and result.kind is ResultKind.SYNCED:
                    return replace(result, alternatives=self._alternatives(sc, scored))
        return None

    @staticmethod
    def _alternatives(best: ScoredCandidate, scored: list[ScoredCandidate] | None) -> tuple[tuple[Candidate, float], ...] | None:
        if scored is None:
            return None
        others = [
            (s.candidate, s.score)
            for s in scored
            if s.accepted and s.candidate.has_synced and s.candidate.source_id != best.candidate.source_id
        ]
        return tuple(others[:MAX_VERSIONS])

    async def _search_versions(self, source: LyricSource, query: TrackInfo) -> list[ScoredCandidate]:
        """Synced versions from a plain search (no exact lookup): for the L1 check after an exact hit."""
        found = await self._call(source, "search", source.search(query))
        return [s for s in self._score(source, query, found) if s.accepted and s.candidate.has_synced]

    async def _fetch_top(self, source: LyricSource, query: TrackInfo, scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
        """For sources whose hits carry no lyrics: fetch the best few metadata matches,
        stopping at the first synced one, and score them again with their lyrics."""
        top = [s for s in scored if s.accepted][:MAX_FETCH]
        if not top:
            return scored
        fetched: list[ScoredCandidate] = []
        errors: list[str] = []
        for sc in top:
            try:
                filled = await self._call(source, "fetch", source.fetch_lyrics(sc.candidate))
            except SourceError as exc:
                errors.append(str(exc))
                continue
            fetched.append(score_candidate(query, filled, self.scoring, True, self._title_transform(source)))
            if filled.has_synced:
                break
        if not fetched and errors:
            raise SourceError("; ".join(errors))
        # The ones turned away stay listed for the rules on versions of another length: NetEase's
        # studio 最后一班车 (214 s) was lost once its live one (249 s) was fetched (SPEC v1.75).
        return ranked(self._artist_evident(query, fetched) + [sc for sc in scored if not sc.accepted])

    def candidates_for(self, track: TrackInfo) -> list[dict]:
        """Scored candidates seen for this song during this session (FR-25 report)."""
        return list(self._records.get(track.key, []))

    def _record_for(self, track: TrackInfo) -> None:
        self._recording = track.key
        if track.key not in self._records:
            self._records[track.key] = []
            while len(self._records) > MAX_RECORDED_SONGS:
                self._records.pop(next(iter(self._records)))

    def _log_match(self, source, query, stage, scored, best, errors=()) -> None:
        record = {
            "source": source.name,
            "stage": stage,
            "query": {"title": query.title, "artist": query.artist, "album": query.album, "duration": query.duration},
            "chosen": best.candidate.source_id if best else None,
            "errors": list(errors),
            "candidates": [
                {
                    "id": s.candidate.source_id,
                    "title": s.candidate.title,
                    "artist": s.candidate.artist,
                    "duration": s.candidate.duration,
                    "synced": s.candidate.has_synced,
                    "score": s.score,
                    "title_sim": s.title_sim,
                    "artist_sim": s.artist_sim,
                    "duration_score": s.duration_score,
                    "rejected": s.rejected,
                }
                for s in scored[:10]
            ],
        }
        if self._recording is not None and self._recording in self._records:
            self._records[self._recording].append(record)
        if match_log.isEnabledFor(logging.INFO):
            match_log.info(json.dumps(record, ensure_ascii=False))

    def _build(self, track: TrackInfo, source: LyricSource, best: ScoredCandidate) -> LyricResult | None:
        cand = best.candidate
        if cand.has_synced:
            lyrics = parse_lrc(cand.synced_lrc or "").lyrics
            if lyrics.lines:
                lyrics = self.corrector.correct(track, self.pipeline.process(lyrics, source), cand)
                return LyricResult(track, ResultKind.SYNCED, lyrics, cand, best.score)
        if cand.has_plain:
            lyrics = self.pipeline.process(parse_plain(cand.plain_text or ""), source)
            return LyricResult(track, ResultKind.PLAIN_ONLY, lyrics, cand, best.score)
        if cand.instrumental:
            return LyricResult(track, ResultKind.INSTRUMENTAL, None, cand, best.score)
        return None

    async def _other_length_version(
        self, track: TrackInfo, other_length: list[tuple[LyricSource, list[ScoredCandidate]]], synced_only: bool = False
    ) -> tuple[LyricResult | None, LyricSource | None, bool]:
        """SPEC v1.29: nothing fits the player's length (it may be playing the MV), so take a
        version of another length. Synced first, in source order, so the user can cue its
        first line; plain otherwise, unless `synced_only` (v1.62). Returns (result, its source,
        whether a fetch broke)."""
        plain: tuple[LyricResult, LyricSource] | None = None
        broke = False
        for source, scored in other_length:
            fetches = 0
            # Equal metadata: the length nearest the player's, which v1.30 shows at once (SPEC v1.64).
            # Studio before live unless the title is live (SPEC v1.75); equal metadata: the nearest length (v1.64).
            by_match = lambda s: (other_performance(track.title, s.candidate.title), -metadata_score(s, self.scoring),  # noqa: E731
                                  abs(s.candidate.duration - track.duration))
            for sc in sorted(scored, key=by_match):
                cand = sc.candidate
                if not (cand.has_synced or cand.has_plain or source.lyrics_inline):
                    if fetches >= MAX_FETCH:
                        break
                    fetches += 1
                    try:
                        cand = await self._call(source, "fetch", source.fetch_lyrics(cand))
                    except SourceError as exc:
                        log.warning("fetching a version of another length failed: %s", exc)
                        broke = True
                        continue
                result = self._build(track, source, replace(sc, candidate=cand))
                if result is None or result.kind is ResultKind.INSTRUMENTAL:
                    continue
                result = replace(result, version_mismatch=True, timeline_checked=True, alternatives=())
                if result.kind is ResultKind.SYNCED:
                    self._log_other_length(track, source, cand)
                    return result, source, broke
                if plain is None:
                    plain = result, source
        if plain is not None and not synced_only:
            self._log_other_length(track, plain[1], plain[0].candidate)
            return plain[0], plain[1], broke
        return None, None, broke

    async def _identify(self, track: TrackInfo) -> tuple[LyricResult | None, LyricSource | None, bool]:
        """SPEC v1.34: name the song behind a browser video's title with the sources' free-text
        search; a hit counts only when the title names it (identify.confirms). Synced first,
        plain otherwise; a version of another length is marked as one. SPEC v1.35: a title
        naming no song but another artist ("OSN)") is searched with the artist field, and a
        hit credited to both counts when it is the only such song. Returns (result, its
        source, whether a request broke)."""
        fold = lambda text: self.pipeline.comparison_text(text, _SIMPLIFIED)  # noqa: E731
        state = _Identifying()
        plain: tuple[LyricResult, LyricSource] | None = None
        for text in video_queries(track.title):
            async for source, hits in self._text_searches(track, text, state):
                for cand in hits:
                    if not confirms(track, cand, fold):
                        continue
                    result = await self._take_identified(track, source, cand, text, state)
                    if result is not None and result.kind is ResultKind.SYNCED:
                        return result, source, state.broke
                    if result is not None and plain is None:
                        plain = result, source
        if plain is None and (query := credit_query(track)):
            credited = [(source, c) async for source, hits in self._text_searches(track, query, state) for c in hits
                        if credits_both(track, c, fold)]
            songs = {song_key(c.title, fold) for _, c in credited}
            if len(songs) > 1:
                log.info("%r names %d songs by %s, none taken", track.title, len(songs), track.artist)
                credited = []
            for source, cand in sorted(credited, key=lambda h: not h[1].has_synced):
                result = await self._take_identified(track, source, cand, query, state)
                if result is not None and result.kind is ResultKind.SYNCED:
                    return result, source, state.broke
                if result is not None and plain is None:
                    plain = result, source
        if plain is not None:
            return plain[0], plain[1], state.broke
        return None, None, state.broke

    async def _text_searches(self, track: TrackInfo, text: str, state: "_Identifying"):
        """Each source's free-text hits for `text`, in source order, one source at a time.
        A simplified query goes to sources that are not simplified in traditional script as
        well: LRCLIB lists TANK's 如果我變成回憶, and nothing for 如果我变成回忆 (SPEC v1.40).
        A query with a space where Chinese meets Latin goes without it too (SPEC v1.55). A
        traditional query goes in simplified as well: LRCLIB lists ICE杨长青, and nothing for
        ICE楊長青 (SPEC v1.56). A bilingual query goes with its Chinese title alone too (SPEC v1.69)."""
        for source in self.order_sources(track):
            for q in self._text_queries(source, text):
                try:
                    hits = await self._call(source, "search-text", source.search_text(q))
                except SourceError as exc:
                    log.warning("identifying search failed: %s", exc)
                    state.broke = True
                    continue
                yield source, hits

    def _text_queries(self, source: LyricSource, text: str) -> list[str]:
        """What a source is asked for free text `text`: see _text_searches."""
        query = self.pipeline.prepare_query(TrackInfo(text, ""), source).title
        queries = list(dict.fromkeys(q for q in (query, joined(query), chinese_part(query)) if q))
        if not source.simplified:
            scripts = (self.pipeline.display_text, lambda q: self.pipeline.comparison_text(q, _SIMPLIFIED))
            queries += list(dict.fromkeys(c for q in queries for to in scripts if (c := to(q)) not in queries))
        return queries

    async def _take_identified(
        self, track: TrackInfo, source: LyricSource, cand: Candidate, text: str, state: "_Identifying"
    ) -> LyricResult | None:
        """An identified hit as the song's result, its lyrics fetched first where needed."""
        if (source.name, cand.source_id) in state.seen:
            return None
        state.seen.add((source.name, cand.source_id))
        if not (cand.has_synced or cand.has_plain or cand.instrumental or source.lyrics_inline):
            if state.fetches.get(source.name, 0) >= MAX_FETCH:
                return None
            state.fetches[source.name] = state.fetches.get(source.name, 0) + 1
            try:
                cand = await self._call(source, "fetch", source.fetch_lyrics(cand))
            except SourceError as exc:
                log.warning("fetching an identified song failed: %s", exc)
                state.broke = True
                return None
        named = replace(track, title=cand.title, artist=cand.artist)
        sc = score_candidate(named, cand, self.scoring, True, self._title_transform(source))
        self._log_match(source, TrackInfo(text, "", "", track.duration), "identify", [sc], sc)
        result = self._build(track, source, sc)
        if result is None or result.kind is ResultKind.INSTRUMENTAL:
            return None
        log.info(
            "identified %r as %s — %s (%s %s, %s)", track.title, cand.title, cand.artist, source.name,
            cand.source_id, result.kind.value,
        )
        return replace(
            result,
            version_mismatch=sc.rejected == "duration",
            timeline_checked=True,
            alternatives=(),
            notes=(*result.notes, "identified"),
        )

    @staticmethod
    def _log_other_length(track: TrackInfo, source: LyricSource, cand: Candidate) -> None:
        log.info(
            "no version of %s is %.0fs long: using %s %s (%.0fs, %s)",
            track.describe(), track.duration, source.name, cand.source_id, cand.duration,
            "synced" if cand.has_synced else "plain",
        )

    async def resolve(self, track: TrackInfo) -> LyricResult:
        self._record_for(track)
        if is_probable_ad(track):
            log.info("probable ad, not querying: %s (%.1fs)", track.describe(), track.duration)
            return LyricResult(track, ResultKind.SKIPPED_AD)
        if not_a_song(track):
            log.info("not a song, not querying: %s (%.0fs in %s)", track.describe(), track.duration, track.source_app)
            return LyricResult(track, ResultKind.NOT_A_SONG)

        entry = await self.cache.get(track)
        if entry is not None:
            return await self._from_cache(track, entry)
        if track.key in self._misses:
            return LyricResult(track, ResultKind.NOT_FOUND, notes=("session_miss",))

        fallback: LyricResult | None = None  # plain-only or instrumental, kept until no synced turns up
        fallback_source: LyricSource | None = None
        weak: tuple[LyricResult, LyricSource] | None = None  # synced, title unlike the player's (v1.63)
        other_length: list[tuple[LyricSource, list[ScoredCandidate]]] = []  # SPEC v1.29, per source in order
        attempted = failed = 0
        searched = self.searchable(track)
        if searched is not track:
            log.info("searching %r as %r", track.title, searched.title)
        for source in self.order_sources(track):
            attempted += 1
            query = self.pipeline.prepare_query(searched, source)
            try:
                best, scored = await self._query_source(source, query)
            except SourceError as exc:
                failed += 1
                log.warning("source failed: %s", exc)
                continue
            # Versions of another length: when nothing fits, for a video over plain text (v1.62),
            # and over a title unlike the player's (v1.63).
            usable = [s for s in scored or () if other_length_usable(s, self.scoring)]
            if usable:
                other_length.append((source, usable))
            if best is None:
                continue
            result = self._build(track, source, best)
            if result is None:
                continue
            if result.kind is ResultKind.SYNCED:
                result = replace(result, alternatives=self._alternatives(best, scored))
                if other_performance(track.title, best.candidate.title):
                    # SPEC v1.75: a live performance the title does not name: its timing is not the
                    # record's. A studio version this source accepted too comes first; else ask the rest.
                    studio = self._studio_version(track, source, scored)
                    if studio is not None:
                        await self._store(studio, source.simplified)
                        return studio
                    log.info("%r is a live version: asking the other sources", best.candidate.title)
                    weak = weak or (result, source)
                    continue
                if best.title_sim < self.scoring.weak_title - 1e-9:
                    # SPEC v1.63: another song by the same artist, of the same length? Ask the rest first.
                    log.info("title %r is unlike %r (%.2f): asking the other sources", best.candidate.title,
                             searched.title, best.title_sim)
                    weak = weak or (result, source)
                    continue
                await self._store(result, source.simplified)
                return result
            if fallback is None or (fallback.kind is ResultKind.INSTRUMENTAL and result.kind is ResultKind.PLAIN_ONLY):
                fallback, fallback_source = result, source

        if weak is not None:
            # The same title by the same artist, of another length; not live unless the title is (v1.75).
            same = [(src, [sc for sc in scs if sc.title_sim >= self.scoring.same_title - 1e-9
                           and not other_performance(track.title, sc.candidate.title)]) for src, scs in other_length]
            same = [(src, scs) for src, scs in same if scs]
            found = None
            if same:
                found, found_source, _ = await self._other_length_version(track, same, synced_only=True)
            if found is None:
                found, found_source = weak
            await self._store(found, found_source.simplified)
            return found
        if fallback is not None and fallback.kind is ResultKind.PLAIN_ONLY and is_browser(track.source_app):
            # SPEC v1.62: an MV outlasts the song by its scenes, so a synced version by the same
            # artist that the video is longer than beats unsynced text that happens to fit its length.
            shorter = [(src, [sc for sc in scs if 0 < sc.candidate.duration < track.duration
                              and not other_performance(track.title, sc.candidate.title)]) for src, scs in other_length]
            shorter = [(src, scs) for src, scs in shorter if scs]
            if shorter:
                found, found_source, _ = await self._other_length_version(track, shorter, synced_only=True)
                if found is not None:
                    if failed:
                        return replace(found, notes=(*found.notes, "source_failed"))
                    await self._store(found, found_source.simplified)
                    return found
        if fallback is not None:
            if failed:
                # A source never answered and may have synced lyrics: show this, but keep nothing.
                return replace(fallback, notes=(*fallback.notes, "source_failed"))
            await self._store(fallback, fallback_source.simplified)
            return fallback
        broke = False  # a later request broke: the song may be there, but the sources did answer
        if other_length:
            found, found_source, broke = await self._other_length_version(track, other_length)
            if found is not None:
                if failed:
                    return replace(found, notes=(*found.notes, "source_failed"))
                await self._store(found, found_source.simplified)
                return found
        if is_browser(track.source_app) and failed < attempted:
            # SPEC v1.34: a video title the rules above could not read; let the sources' own
            # search name the song, confirmed against the title.
            found, found_source, identify_broke = await self._identify(track)
            broke = broke or identify_broke
            if found is not None:
                if failed:
                    return replace(found, notes=(*found.notes, "source_failed"))
                await self._store(found, found_source.simplified)
                return found
        if attempted and failed == attempted:
            return LyricResult(track, ResultKind.SOURCES_FAILED)  # not remembered: may be a network blip
        if failed or broke:
            # A source never answered and may well have the song: ask again on the next play.
            return LyricResult(track, ResultKind.NOT_FOUND, notes=("source_failed",))
        self._misses.add(track.key)
        return LyricResult(track, ResultKind.NOT_FOUND)

    def _mode(self) -> str | None:
        """Conversion identity stored with cached text (mode, plus vocabulary when there is one)."""
        return getattr(self.pipeline, "cache_key", None) or getattr(self.pipeline, "mode", None)

    def forget_misses(self) -> None:
        """Sources changed: songs not found earlier this session deserve another try."""
        self._misses.clear()

    async def _store(
        self, result: LyricResult, source_simplified: bool, raw_translations: tuple[str | None, ...] | None = None
    ) -> None:
        cand = result.candidate
        raw = None
        if cand is not None:
            raw = cand.synced_lrc if result.kind is ResultKind.SYNCED else cand.plain_text
        await self.cache.put(result, raw, source_simplified, self._mode(), raw_translations)

    async def _from_cache(self, track: TrackInfo, entry: CachedEntry) -> LyricResult:
        result = replace(entry.result, track=track)
        mode = self._mode()
        stale = entry.conversion_mode and mode and entry.conversion_mode != mode
        if stale and entry.raw_text and result.lyrics is not None:
            # Conversion mode changed since caching: rebuild from the source text, no network.
            source = _CachedSource(entry.source_simplified)
            if result.lyrics.synced:
                lyrics = parse_lrc(entry.raw_text).lyrics
                lyrics = self.corrector.correct(track, self.pipeline.process(lyrics, source), result.candidate)
            else:
                lyrics = self.pipeline.process(parse_plain(entry.raw_text), source)
            translations = None
            if entry.raw_translations is not None and len(entry.raw_translations) == len(lyrics.lines):
                translations = self._convert_translations(entry.raw_translations)
            result = replace(result, lyrics=replace(lyrics, translations=translations))
            await self.cache.put(result, entry.raw_text, entry.source_simplified, mode, entry.raw_translations)
            log.info("cache entry reconverted %s -> %s for %s", entry.conversion_mode, mode, track.describe())
        if result.l3_state == L3_APPLIED and entry.l3_json and result.lyrics is not None:
            # Checked against the lines as they are now: a reconversion or new version voids it.
            # Plain lyrics get their times this way too (SPEC v1.26).
            result = replace(result, alignment=plan_from_json(entry.l3_json, result.lyrics.lines))
        return result

    async def set_offset(self, track: TrackInfo, offset: float) -> bool:
        return await self.cache.set_offset(track, offset)

    # -- manual search and binding (FR-12) ------------------------------------------

    async def search_manual(
        self, track: TrackInfo, title: str, artist: str
    ) -> tuple[list[tuple[str, ScoredCandidate]], list[str]]:
        """Every candidate the enabled sources return for a typed title and artist.

        Scored against what was typed plus the player's duration, best first;
        rejected ones are listed too (the user decides). Returns (hits, errors).
        """
        typed = replace(track, title=title.strip(), artist=artist.strip(), album="")
        hits: list[tuple[str, ScoredCandidate]] = []
        errors: list[str] = []
        for source in self.order_sources(typed):
            query = self.pipeline.prepare_query(typed, source)
            try:
                found = await self._call(source, "search", source.search(query))
            except SourceError as exc:
                errors.append(str(exc))
                continue
            hits.extend((source.name, s) for s in self._score(source, query, found, require_lyrics=False))
        named: set[tuple[str, str]] = set()
        if not typed.artist:
            hits, named = await self._search_typed_text(typed, hits, errors)
        hits.sort(key=lambda h: ((h[0], h[1].candidate.source_id) in named, _manual_rank(h[1])), reverse=True)
        return hits, list(dict.fromkeys(errors))

    async def _search_typed_text(
        self, typed: TrackInfo, hits: list[tuple[str, ScoredCandidate]], errors: list[str]
    ) -> tuple[list[tuple[str, ScoredCandidate]], set[tuple[str, str]]]:
        """SPEC v1.77: with no artist typed, the title box is searched the way a video's title is
        identified (its pieces as free text), so singer and song may come in either order. A hit
        whose title and artist both appear in what was typed is named by it: scored as that song
        and listed first. Length does not count against naming here; the duration column shows
        it. Returns the hits merged with `hits`, and the named ones' (source, id)."""
        sources = self.order_sources(typed)
        texts = video_queries(typed.title)
        asks = list(dict.fromkeys((s, q) for text in texts for s in sources for q in self._text_queries(s, text)))
        # The user is waiting: a few requests at a time per source, not one after another.
        limits = {s.name: asyncio.Semaphore(MANUAL_PARALLEL) for s in sources}

        async def ask(source: LyricSource, text: str) -> list[Candidate]:
            async with limits[source.name]:
                try:
                    return await self._call(source, "search-text", source.search_text(text))
                except SourceError as exc:
                    errors.append(str(exc))
                    return []

        answers = await asyncio.gather(*(ask(s, q) for s, q in asks))
        fold = lambda text: self.pipeline.comparison_text(text, _SIMPLIFIED)  # noqa: E731
        merged = {(source, sc.candidate.source_id): (source, sc) for source, sc in hits}
        named: set[tuple[str, str]] = set()
        any_length = replace(typed, duration=0.0)
        for (source, _), found in zip(asks, answers):
            for cand in found:
                key = (source.name, cand.source_id)
                if key in named:
                    continue
                if confirms(any_length, cand, fold):
                    as_named = replace(typed, title=cand.title, artist=cand.artist)
                    sc = score_candidate(as_named, cand, self.scoring, False, self._title_transform(source))
                    merged[key] = (source.name, sc)
                    named.add(key)
                elif key not in merged:
                    query = self.pipeline.prepare_query(typed, source)
                    merged[key] = (source.name, self._score(source, query, [cand], require_lyrics=False)[0])
        return list(merged.values()), named

    async def fill(self, source_name: str, cand: Candidate) -> Candidate:
        """The candidate with its lyrics (for sources that list hits without them)."""
        source = self._source_named(source_name)
        if source is None:
            raise SourceError(f"{source_name}: source is disabled")
        if source.lyrics_inline or cand.has_synced or cand.has_plain or cand.instrumental:
            return cand
        return await self._call(source, "fetch", source.fetch_lyrics(cand))

    def preview(self, track: TrackInfo, source_name: str, cand: Candidate) -> LyricResult | None:
        """How a filled candidate would look on screen, without storing anything."""
        source = self._source_named(source_name)
        if source is None:
            return None
        return self._build(track, source, ScoredCandidate(cand, 0.0, 0.0, 0.0, None))

    async def bind(self, track: TrackInfo, source_name: str, cand: Candidate, score: float | None = None) -> LyricResult:
        """Use this version for the song from now on (FR-12). The old entry, its
        offset and translations go: they belonged to the previous version."""
        source = self._source_named(source_name)
        if source is None:
            raise SourceError(f"{source_name}: source is disabled")
        filled = await self.fill(source_name, cand)
        result = self._build(track, source, ScoredCandidate(filled, score or 0.0, 0.0, 0.0, None))
        if result is None:
            raise SourceError(f"{source_name} {cand.source_id}: no lyrics in this version")
        mismatch = track.duration > 0 and filled.duration > 0 and abs(track.duration - filled.duration) > self.scoring.max_duration_diff
        result = replace(result, bound=True, timeline_checked=True, alternatives=(), version_mismatch=mismatch)
        await self.cache.remove(track)
        await self._store(result, source.simplified)
        self._misses.discard(track.key)
        log.info("bound %s %s to %s", source_name, cand.source_id, track.describe())
        return result

    async def save_edit(
        self, track: TrackInfo, lines: Sequence[LyricLine], translations: Sequence[str | None] | None = None
    ) -> LyricResult:
        """The user's edited version (FR-17): bound like a manual pick, kept in time order.
        `translations` follow `lines` one to one (None where a line has none)."""
        order = sorted(range(len(lines)), key=lambda i: lines[i].start)
        ordered = tuple(lines[i] for i in order)
        trans = tuple(translations[i] for i in order) if translations is not None else None
        lrc = format_lrc(ordered)
        cand = Candidate(EDITED_SOURCE, "edited", track.title, track.artist, track.album, track.duration, lrc)
        result = LyricResult(
            track, ResultKind.SYNCED, Lyrics(ordered, True, trans), cand, None, bound=True, timeline_checked=True, alternatives=()
        )
        await self.cache.save_edit(result, lrc, self._mode())
        self._misses.discard(track.key)
        log.info("edited version saved for %s (%d lines)", track.describe(), len(ordered))
        return result

    async def save_edit_plain(self, track: TrackInfo, texts: Sequence[str]) -> LyricResult:
        """An edited version without times (SPEC v1.26): pasted or retyped text the user did
        not time. Bound like any edit, but L3 may give it times; timing it by hand makes
        it an ordinary edited version again."""
        lines = tuple(LyricLine(0.0, text.strip()) for text in texts if text.strip())
        plain = "\n".join(line.text for line in lines)
        cand = Candidate(EDITED_SOURCE, "edited", track.title, track.artist, track.album, track.duration, None, plain)
        result = LyricResult(
            track, ResultKind.PLAIN_ONLY, Lyrics(lines, False), cand, None, bound=True, timeline_checked=True, alternatives=()
        )
        await self.cache.save_edit(result, plain, self._mode())
        self._misses.discard(track.key)
        log.info("untimed edited version saved for %s (%d lines)", track.describe(), len(lines))
        return result

    async def revert_edit(self, track: TrackInfo) -> bool:
        reverted = await self.cache.revert_edit(track)
        self._misses.discard(track.key)
        log.info("edit reverted for %s: %s", track.describe(), reverted)
        return reverted

    # -- L3 forced alignment (FR-21, FR-22) -------------------------------------------

    async def store_alignment(
        self, track: TrackInfo, lines: Sequence[LyricLine], segments: list, pos0: float, seconds: float
    ) -> tuple[Plan | None, LyricResult | None]:
        """Judge an alignment of `lines` and keep it with the song. Returns the plan and the
        song's result as it now shows, or (None, None) when the stored lines changed meanwhile."""
        entry = await self.cache.get(track)
        if entry is None or entry.result.lyrics is None or lines_hash(entry.result.lyrics.lines) != lines_hash(lines):
            log.info("L3 result for %s discarded: the lyrics changed since recording", track.describe())
            return None, None
        # A version of another length has no source times to trust: time it like plain lyrics (SPEC v1.29).
        timed = entry.result.lyrics.synced and not entry.result.version_mismatch
        judged = (plan if timed else plan_plain)(lines, segments, pos0, seconds)
        await self.cache.save_alignment(track, L3_APPLIED if judged.usable else L3_UNUSABLE, plan_to_json(judged, lines))
        if judged.usable and entry.result.version_mismatch:
            await self.cache.set_offset(track, 0.0)  # the cue's offset was for the source times
        return judged, await self._from_cache(track, await self.cache.get(track))

    async def alignment_failed(self, track: TrackInfo) -> None:
        await self.cache.save_alignment(track, L3_FAILED)

    async def revert_alignment(self, track: TrackInfo) -> bool:
        """FR-22: back to the source times, and never align this song again."""
        return await self.cache.save_alignment(track, L3_REVERTED)

    async def unbind(self, track: TrackInfo) -> None:
        """Back to automatic matching: forget the stored version."""
        await self.cache.remove(track)
        self._misses.discard(track.key)
        log.info("unbound %s", track.describe())

    # -- second pass: language re-check, translation and L1 (English songs) -----------

    @staticmethod
    def needs_enrichment(result: LyricResult) -> bool:
        return (
            result.kind is ResultKind.SYNCED
            and result.lyrics is not None
            and (result.lyrics.translations is None or not result.timeline_checked)
        )

    def _convert_translations(self, raw: tuple[str | None, ...]) -> tuple[str | None, ...]:
        # Translations come from NetEase, a simplified source: convert every line.
        with self.pipeline.session():
            return tuple(self.pipeline.convert_line(t, force=True) if t else None for t in raw)

    def _with_translations(self, result: LyricResult, raw: tuple[str | None, ...]) -> LyricResult:
        return replace(result, lyrics=replace(result.lyrics, translations=self._convert_translations(raw)))

    def _timeline(self, cand: Candidate, source: LyricSource):
        """A version's lines in display script, so text comparison works across sources."""
        return self.pipeline.process(parse_lrc(cand.synced_lrc or "").lyrics, source).lines

    async def enrich(self, result: LyricResult) -> LyricResult:
        """Second pass, after the main lyrics are on screen (SPEC v1.9).

        1. L1 timeline consensus: switch versions when the main source's own
           versions disagree and a majority (NetEase as third vote) says so.
        2. Language re-check from the lyric text; English songs get NetEase
           translation lines.
        NetEase is asked at most once per pass. Always returns the updated
        result (use visibly_changed to decide whether to redraw). Parts whose
        lookup failed are not marked done, so they are retried on the next play.
        """
        if not self.needs_enrichment(result):
            return result
        track = result.track
        self._record_for(track)
        initial = detect_from_metadata(track.title, track.artist)
        language = detect_from_lyrics(line.text for line in result.lyrics.lines) or initial
        if language is not initial:
            log.info("language re-judged %s -> %s for %s", initial.value, language.value, track.describe())
        translator = self._source_named(TRANSLATOR)
        asked: dict[str, object] = {}

        async def netease():
            if "answer" not in asked:
                asked["answer"] = None
                if translator is not None:
                    try:
                        asked["answer"] = (await self._query_source(translator, self.pipeline.prepare_query(self.searchable(track), translator)))[0]
                    except SourceError as exc:
                        log.warning("netease lookup failed: %s", exc)
                        asked["answer"] = _FAILED
            return asked["answer"]

        updated, timeline_done = await self._check_timeline(result, netease, translator)
        updated, raw_translations = await self._translate(updated, language, translator, netease)
        if not timeline_done:
            updated = replace(updated, timeline_checked=False)
        final_source = self._source_named(updated.candidate.source) if updated.candidate else None
        await self._store(updated, bool(final_source and final_source.simplified), raw_translations)

        return updated

    async def _check_timeline(self, result: LyricResult, netease, translator) -> tuple[LyricResult, bool]:
        """L1 consensus. Returns (result, done)."""
        if result.timeline_checked:
            return result, True
        cand = result.candidate
        if abs(result.offset) > 1e-9:
            log.info("L1 skipped for %s: manual offset %+.1fs takes priority", result.track.describe(), result.offset)
            return replace(result, timeline_checked=True), True
        source = self._source_named(cand.source) if cand else None
        if source is None or not source.lyrics_inline:
            return replace(result, timeline_checked=True), True  # no free versions to compare (NetEase main)

        versions = result.alternatives
        if versions is None:
            try:
                found = await self._search_versions(source, self.pipeline.prepare_query(self.searchable(result.track), source))
            except SourceError as exc:
                log.warning("L1 version search failed: %s", exc)
                return result, False
            versions = tuple((s.candidate, s.score) for s in found if s.candidate.source_id != cand.source_id)[:MAX_VERSIONS]
        result = replace(result, alternatives=versions)

        main = result.lyrics.lines
        points = [(0.0, cand, result.score or 0.0, source)]  # (shift vs main, version, score, source)
        for alt, score in versions:
            shift = median_offset(main, self._timeline(alt, source))
            if shift is not None:
                points.append((shift, alt, score, source))
        if not any(abs(p[0]) > L1_OFFSET_LIMIT for p in points[1:]):
            return replace(result, timeline_checked=True), True

        answer = await netease()
        if answer is _FAILED:
            return result, False
        if answer is not None and answer.candidate.has_synced:
            shift = median_offset(main, self._timeline(answer.candidate, translator))
            if shift is not None:
                points.append((shift, answer.candidate, answer.score, translator))

        def group(center: float) -> list:
            return [p for p in points if abs(p[0] - center) <= L1_OFFSET_LIMIT]

        current = group(0.0)
        majority = max((group(p[0]) for p in points), key=len)
        label = f"{cand.source} {cand.source_id}"
        if len(majority) >= 2 and len(majority) > len(current):
            # Highest score wins; on a tie prefer the main source (no extra script conversion).
            shift, winner, score, winner_source = max(majority, key=lambda p: (p[2], p[3] is source))
            switched = self._build(result.track, winner_source, ScoredCandidate(winner, score, 0.0, 0.0, None))
            if switched is not None and switched.kind is ResultKind.SYNCED:
                log.info(
                    "L1: %s runs %+.1fs against %d agreeing versions; switched to %s %s",
                    label, -shift, len(majority), winner_source.name, winner.source_id,
                )
                return (
                    replace(
                        switched,
                        offset=result.offset,
                        alternatives=versions,
                        timeline_checked=True,
                        notes=(*switched.notes, "l1_switched"),
                    ),
                    True,
                )
        log.info("L1: versions of %s disagree but no majority (%d vs %d); keeping it", label, len(majority), len(current))
        return replace(result, timeline_checked=True), True

    async def _translate(
        self, result: LyricResult, language: Language, translator, netease
    ) -> tuple[LyricResult, tuple[str | None, ...] | None]:
        """English songs get NetEase translation lines. Returns (result, raw translations)."""
        lyrics = result.lyrics
        if lyrics.translations is not None:
            return result, None  # already checked; stored raw text is kept
        if language is not Language.EN:
            return replace(result, lyrics=replace(lyrics, translations=())), ()
        if translator is None:
            return result, None  # NetEase disabled: checked again once it is enabled
        cand = result.candidate
        if cand is not None and cand.source == translator.name and cand.synced_lrc:
            raw = align_same_source(parse_lrc(cand.synced_lrc).lyrics.lines, parse_translation(cand.translation_lrc))
            return self._with_translations(result, raw), raw
        answer = await netease()
        if answer is _FAILED:
            return result, None  # retried on the next play
        if answer is None or not answer.candidate.has_synced:
            return replace(result, lyrics=replace(lyrics, translations=())), ()
        other = parse_lrc(answer.candidate.synced_lrc).lyrics.lines
        translations = parse_translation(answer.candidate.translation_lrc)
        if cand is not None and cand.source == translator.name and cand.source_id == answer.candidate.source_id:
            raw = align_same_source(other, translations)  # cached NetEase entry: same timestamps
        else:
            raw = align_translations(lyrics.lines, other, translations)
        return self._with_translations(result, raw), raw

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()


class LyricService(QObject):
    resultReady = pyqtSignal(object)  # LyricResult: show it
    resultChecked = pyqtSignal(object)  # LyricResult: background pass done, nothing to redraw

    def __init__(
        self, resolver: LyricResolver, parent: QObject | None = None, retry_delay: float = RETRY_AFTER_FAILURE
    ) -> None:
        super().__init__(parent)
        self.resolver = resolver
        self.retry_delay = retry_delay
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._current: tuple[str, str, str] | None = None  # key of the song last requested

    def request(self, track: TrackInfo) -> None:
        self.cancel()
        self._current = track.key
        self._generation += 1
        self._task = spawn(self._run(track, self._generation))

    async def bind(self, track: TrackInfo, source_name: str, cand: Candidate, score: float | None = None) -> LyricResult:
        """FR-12. Shown at once when it is the current song; raises SourceError on failure."""
        result = await self.resolver.bind(track, source_name, cand, score)
        if track.key == self._current:
            self._show_now(result)
        return result

    async def unbind(self, track: TrackInfo) -> None:
        await self.resolver.unbind(track)
        if track.key == self._current:
            self.request(track)

    def _show_now(self, result: LyricResult) -> None:
        """A result made by the user for the current song: replace whatever is in flight."""
        self.cancel()
        self._current = result.track.key
        self._generation += 1
        self.resultReady.emit(result)
        self._task = spawn(self._enrich(result, self._generation))

    async def save_edit(self, track: TrackInfo, lines, translations=None) -> LyricResult:
        """FR-17. Shown at once when it is the current song."""
        result = await self.resolver.save_edit(track, lines, translations)
        if track.key == self._current:
            self._show_now(result)
        return result

    async def save_edit_plain(self, track: TrackInfo, texts) -> LyricResult:
        """SPEC v1.26. Shown at once when it is the current song."""
        result = await self.resolver.save_edit_plain(track, texts)
        if track.key == self._current:
            self._show_now(result)
        return result

    async def revert_edit(self, track: TrackInfo) -> None:
        await self.resolver.revert_edit(track)
        if track.key == self._current:
            self.request(track)

    async def store_alignment(self, track: TrackInfo, lines, segments, pos0: float, seconds: float) -> Plan | None:
        """FR-21. A song playing again right now switches to the aligned times at once."""
        judged, result = await self.resolver.store_alignment(track, lines, segments, pos0, seconds)
        if result is not None and track.key == self._current:
            self._show_now(result)
        return judged

    async def revert_alignment(self, track: TrackInfo) -> None:
        await self.resolver.revert_alignment(track)
        if track.key == self._current:
            self.request(track)

    def save_offset(self, track: TrackInfo, offset: float) -> None:
        spawn(self.resolver.set_offset(track, offset))

    def cancel(self) -> None:
        self._generation += 1
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self, track: TrackInfo, generation: int) -> None:
        try:
            result = await self.resolver.resolve(track)
        except asyncio.CancelledError:
            log.debug("query cancelled: %s", track.describe())
            raise
        except Exception:
            log.exception("resolver crashed for %s", track.describe())
            result = LyricResult(track, ResultKind.SOURCES_FAILED)
        if generation != self._generation:
            log.debug("dropping stale result for %s", track.describe())
            return
        log.info(
            "lyrics %s for %s (score=%s, cache=%s)", result.kind.value, track.describe(), result.score, result.from_cache
        )
        self.resultReady.emit(result)
        if result.kind is ResultKind.SOURCES_FAILED or "source_failed" in result.notes:
            await self._retry(result, generation)
            return
        await self._enrich(result, generation)

    async def _retry(self, first: LyricResult, generation: int) -> None:
        """A source failed: ask once more while the song still plays (SPEC v1.23).

        Runs inside the request's own task, so a track change, a binding or an
        edit cancels it like any other in-flight query.
        """
        await asyncio.sleep(self.retry_delay)
        if generation != self._generation:
            return
        track = first.track
        try:
            result = await self.resolver.resolve(track)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("resolver crashed for %s", track.describe())
            return
        if generation != self._generation:
            return
        log.info("lyrics %s for %s on the second try (score=%s)", result.kind.value, track.describe(), result.score)
        if result.kind is first.kind and not visibly_changed(first, result):
            self.resultChecked.emit(result)  # nothing new: keep the screen as it is
            return
        self.resultReady.emit(result)
        await self._enrich(result, generation)

    async def _enrich(self, result: LyricResult, generation: int) -> None:
        track = result.track
        if not self.resolver.needs_enrichment(result):
            return
        try:
            enriched = await self.resolver.enrich(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("enrichment crashed for %s", track.describe())
            return
        if generation != self._generation:
            return
        if visibly_changed(result, enriched):
            log.info("lyrics updated for %s (translation=%s)", track.describe(), enriched.lyrics.has_translation)
            self.resultReady.emit(enriched)
        else:
            self.resultChecked.emit(enriched)

    async def aclose(self) -> None:
        self.cancel()
        await self.resolver.aclose()
