"""Local lyric cache (FR-06, FR-07).

SQLite in %LOCALAPPDATA%\\LyricFloat. Keyed by normalized artist + title
(after simplified-to-traditional detection on the player metadata) with
a ±2 s duration tolerance, so different players reporting slightly
different durations still hit. Converted lines are stored for display;
the source text is kept too, so a change of conversion mode can rebuild
an entry without going back to the network. No size limit (SPEC v1.6).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from lyricfloat import APP_NAME
from lyricfloat.align.apply import language_of, script_language
from lyricfloat.lyrics.normalize import normalize_artist, normalize_title
from lyricfloat.models import Candidate, LyricLine, LyricResult, Lyrics, ResultKind, TrackInfo

log = logging.getLogger(__name__)

DURATION_TOLERANCE = 2.0
# 2: translation columns, 3: timeline check, 4: edit snapshot, 5: L3, 6-7: L3 redo, 8: versions of another length,
# 9: L3 redo of songs aligned in the wrong language
SCHEMA_VERSION = 9
CACHEABLE = (ResultKind.SYNCED, ResultKind.PLAIN_ONLY, ResultKind.INSTRUMENTAL)
EDITED_SOURCE = "local"  # candidate source of the user's edited version (FR-17)

# L3 states per song (FR-21). No state: never aligned, so the next full play is recorded.
L3_APPLIED = "applied"
L3_UNUSABLE = "unusable"  # aligned, but too few sure lines: the source times stay
L3_FAILED = "failed"  # the worker failed; tried again on a later play
L3_GAVE_UP = "gave_up"  # failed L3_MAX_ATTEMPTS times
L3_REVERTED = "reverted"  # the user restored the source times (FR-22): never again
L3_MAX_ATTEMPTS = 3

KeyFn = Callable[[TrackInfo], tuple[str, str]]


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def plain_key(track: TrackInfo) -> tuple[str, str]:
    return normalize_artist(track.artist), normalize_title(track.title)


@dataclass(frozen=True, slots=True)
class CachedEntry:
    result: LyricResult
    raw_text: str | None  # source text before conversion (LRC or plain)
    source_simplified: bool
    conversion_mode: str | None  # mode used for the stored lines
    raw_translations: tuple[str | None, ...] | None = None  # translations before conversion
    l3_json: str | None = None  # stored alignment (align.apply.plan_to_json)


class LyricCache(Protocol):
    async def get(self, track: TrackInfo) -> CachedEntry | None: ...

    async def put(
        self,
        result: LyricResult,
        raw_text: str | None,
        source_simplified: bool,
        conversion_mode: str | None,
        raw_translations: tuple[str | None, ...] | None = None,
    ) -> None: ...

    async def set_offset(self, track: TrackInfo, offset: float) -> bool: ...

    async def remove(self, track: TrackInfo) -> bool: ...

    async def save_edit(self, result: LyricResult, raw_text: str, conversion_mode: str | None) -> None: ...

    async def revert_edit(self, track: TrackInfo) -> bool: ...

    async def save_alignment(self, track: TrackInfo, state: str, plan_json: str | None = None) -> bool: ...


class NullCache:
    async def get(self, track: TrackInfo) -> CachedEntry | None:
        return None

    async def put(self, result, raw_text, source_simplified, conversion_mode, raw_translations=None) -> None:
        return None

    async def set_offset(self, track: TrackInfo, offset: float) -> bool:
        return False

    async def remove(self, track: TrackInfo) -> bool:
        return False

    async def save_edit(self, result: LyricResult, raw_text: str, conversion_mode: str | None) -> None:
        return None

    async def revert_edit(self, track: TrackInfo) -> bool:
        return False

    async def save_alignment(self, track: TrackInfo, state: str, plan_json: str | None = None) -> bool:
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics (
    id INTEGER PRIMARY KEY,
    artist_key TEXT NOT NULL,
    title_key TEXT NOT NULL,
    duration REAL NOT NULL,
    kind TEXT NOT NULL,
    synced INTEGER NOT NULL,
    lines_json TEXT,
    raw_text TEXT,
    source TEXT,
    source_id TEXT,
    source_simplified INTEGER NOT NULL DEFAULT 0,
    conversion_mode TEXT,
    cand_title TEXT,
    cand_artist TEXT,
    cand_duration REAL,
    score REAL,
    offset REAL NOT NULL DEFAULT 0,
    bound INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lyrics_key ON lyrics(artist_key, title_key);
"""

# Columns added after the first schema; applied to older files on open.
_ADDED_COLUMNS = {
    "translations_json": "TEXT",  # null: not checked yet; []: checked, none
    "translations_raw_json": "TEXT",
    "timeline_checked": "INTEGER",  # null on older rows: they get the L1 check on their next play
    "original_json": "TEXT",  # FR-17: the row as it was before the user's first edit
    "l3_json": "TEXT",  # FR-21 alignment of the stored lines
    "l3_state": "TEXT",
    "l3_attempts": "INTEGER",
    "version_mismatch": "INTEGER",  # SPEC v1.29: the version is not the player's length (an MV)
}

# On refresh, a None for these keeps what is stored (e.g. a cache hit has no source text to resend).
_KEEP_IF_NONE = ("raw_text", "translations_json", "translations_raw_json")


class SqliteCache:
    def __init__(self, path: Path | None = None, key: KeyFn = plain_key) -> None:
        self.path = path or default_cache_dir() / "cache.sqlite3"
        self.key = key
        self._db: sqlite3.Connection | None = self._open()

    def _open(self) -> sqlite3.Connection | None:
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            # A damaged file must not take the app down: set it aside and start fresh.
            log.warning("cache unreadable (%s), starting a new one", exc)
            try:
                self.path.rename(self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}"))
                return self._connect()
            except (OSError, sqlite3.DatabaseError) as exc2:
                log.error("cache disabled: %s", exc2)
                return None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            old_version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = {row[1] for row in db.execute("PRAGMA table_info(lyrics)")}
            for column, kind in _ADDED_COLUMNS.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE lyrics ADD COLUMN {column} {kind}")
            if old_version == 5:
                # v5 judged alignments whose segments did not line up with the lyrics as
                # unusable; the worker now hands words back per line, so try them again.
                db.execute("UPDATE lyrics SET l3_state = NULL, l3_json = NULL WHERE l3_state = ?", (L3_UNUSABLE,))
            if 0 < old_version < 7:
                # Before v7 a recording's start came from the report nearest it, which Spotify
                # sends early at a track change: aligned songs could run late. Din chose to redo
                # them (SPEC v1.28); the source times were never touched, reverted songs stay so.
                db.execute("UPDATE lyrics SET l3_state = NULL, l3_json = NULL, l3_attempts = NULL WHERE l3_state = ?", (L3_APPLIED,))
            if 0 < old_version < 9:
                # Before v9 NetEase's leading "作词 : 某人" made English songs align as Chinese.
                # Din chose to redo them (SPEC v1.82); reverted songs stay so.
                _redo_wrong_languages(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()
        except sqlite3.DatabaseError:
            db.close()  # Windows keeps the file locked otherwise, and it cannot be set aside
            raise
        return db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _find(self, track: TrackInfo) -> sqlite3.Row | None:
        if self._db is None:
            return None
        artist_key, title_key = self.key(track)
        rows = self._db.execute(
            "SELECT * FROM lyrics WHERE artist_key = ? AND title_key = ? ORDER BY updated_at DESC",
            (artist_key, title_key),
        ).fetchall()
        if not rows:
            return None
        if track.duration <= 0:
            return rows[0]  # no duration to compare: most recent entry
        matches = [r for r in rows if r["duration"] <= 0 or abs(r["duration"] - track.duration) <= DURATION_TOLERANCE]
        if not matches:
            return None
        return min(matches, key=lambda r: abs(r["duration"] - track.duration) if r["duration"] > 0 else DURATION_TOLERANCE)

    async def get(self, track: TrackInfo) -> CachedEntry | None:
        row = self._find(track)
        if row is None:
            return None
        lyrics = None
        if row["lines_json"]:
            lines = tuple(LyricLine(float(s), str(t)) for s, t in json.loads(row["lines_json"]))
            translations = _load_tuple(row["translations_json"])
            if translations is not None and translations and len(translations) != len(lines):
                translations = None  # out of step with the lines: check again
            lyrics = Lyrics(lines, synced=bool(row["synced"]), translations=translations)
        candidate = Candidate(
            source=row["source"] or "",
            source_id=row["source_id"] or "",
            title=row["cand_title"] or "",
            artist=row["cand_artist"] or "",
            duration=row["cand_duration"] or 0.0,
        )
        result = LyricResult(
            track=track,
            kind=ResultKind(row["kind"]),
            lyrics=lyrics,
            candidate=candidate,
            score=row["score"],
            offset=row["offset"],
            from_cache=True,
            timeline_checked=bool(row["timeline_checked"]),
            bound=bool(row["bound"]),
            version_mismatch=bool(row["version_mismatch"]),
            l3_state=row["l3_state"],
        )
        return CachedEntry(
            result,
            row["raw_text"],
            bool(row["source_simplified"]),
            row["conversion_mode"],
            _load_tuple(row["translations_raw_json"]),
            row["l3_json"],
        )

    async def put(
        self,
        result: LyricResult,
        raw_text: str | None,
        source_simplified: bool,
        conversion_mode: str | None,
        raw_translations: tuple[str | None, ...] | None = None,
    ) -> None:
        if self._db is None or result.kind not in CACHEABLE:
            return
        artist_key, title_key = self.key(result.track)
        lines_json = (
            json.dumps([[line.start, line.text] for line in result.lyrics.lines], ensure_ascii=False)
            if result.lyrics
            else None
        )
        cand = result.candidate or Candidate("", "", "", "")
        values = {
            "kind": result.kind.value,
            "synced": int(bool(result.lyrics and result.lyrics.synced)),
            "lines_json": lines_json,
            "raw_text": raw_text,
            "source": cand.source,
            "source_id": cand.source_id,
            "source_simplified": int(source_simplified),
            "conversion_mode": conversion_mode,
            "cand_title": cand.title,
            "cand_artist": cand.artist,
            "cand_duration": cand.duration,
            "score": result.score,
            "translations_json": _dump_tuple(result.lyrics.translations if result.lyrics else None),
            "translations_raw_json": _dump_tuple(raw_translations),
            "timeline_checked": int(result.timeline_checked),
            "bound": int(result.bound),
            "version_mismatch": int(result.version_mismatch),
            "updated_at": time.time(),
        }
        existing = self._find(result.track)
        if existing is not None and existing["bound"] and not result.bound:
            log.info("cache keeps the bound version of %s", result.track.describe())
            return
        if existing is not None:
            # Keep the user's offset and binding; refresh the content.
            for key in _KEEP_IF_NONE:
                if values[key] is None:
                    values[key] = existing[key]
            sets = ", ".join(f"{k} = :{k}" for k in values)
            self._db.execute(f"UPDATE lyrics SET {sets} WHERE id = :id", {**values, "id": existing["id"]})
        else:
            values.update(
                artist_key=artist_key,
                title_key=title_key,
                duration=result.track.duration,
                created_at=values["updated_at"],
            )
            cols = ", ".join(values)
            self._db.execute(f"INSERT INTO lyrics ({cols}) VALUES ({', '.join(':' + k for k in values)})", values)
        self._db.commit()

    async def set_offset(self, track: TrackInfo, offset: float) -> bool:
        row = self._find(track)
        if row is None or self._db is None:
            return False
        self._db.execute(
            "UPDATE lyrics SET offset = ?, updated_at = ? WHERE id = ?", (round(offset, 3), time.time(), row["id"])
        )
        self._db.commit()
        return True

    async def save_edit(self, result: LyricResult, raw_text: str, conversion_mode: str | None) -> None:
        """Store the user's edited version (FR-17), keeping the version it replaced.

        The first edit snapshots the row as it was; later edits keep that
        snapshot, so reverting always returns to the version before any edit.
        The edit's times already include the offset, so the offset goes to 0.
        """
        if self._db is None:
            return
        existing = self._find(result.track)
        snapshot = None
        if existing is not None:
            snapshot = existing["original_json"] or json.dumps(
                {k: existing[k] for k in existing.keys() if k not in ("id", "original_json")}, ensure_ascii=False
            )
        raw_translations = result.lyrics.translations if result.lyrics else None
        await self.put(result, raw_text, False, conversion_mode, raw_translations)
        row = self._find(result.track)
        if row is not None:
            # put() keeps stored translations when given none; an edit must not inherit the old lines'.
            translations = _dump_tuple(raw_translations)
            self._db.execute(
                "UPDATE lyrics SET original_json = ?, offset = 0, translations_json = ?, translations_raw_json = ?,"
                " l3_json = NULL, l3_state = NULL, l3_attempts = NULL WHERE id = ?",
                (snapshot, translations, translations, row["id"]),
            )
            self._db.commit()

    async def revert_edit(self, track: TrackInfo) -> bool:
        """Back to the version before the first edit; an edit with nothing before it is removed."""
        row = self._find(track)
        if row is None or self._db is None or row["source"] != EDITED_SOURCE:
            return False
        if not row["original_json"]:
            self._db.execute("DELETE FROM lyrics WHERE id = ?", (row["id"],))
        else:
            saved = {k: v for k, v in json.loads(row["original_json"]).items() if k not in ("id", "original_json")}
            sets = ", ".join(f"{k} = :{k}" for k in saved)
            self._db.execute(f"UPDATE lyrics SET {sets}, original_json = NULL WHERE id = :id", {**saved, "id": row["id"]})
        self._db.commit()
        return True

    async def save_alignment(self, track: TrackInfo, state: str, plan_json: str | None = None) -> bool:
        """Record an L3 outcome (FR-21). Failures count up to L3_MAX_ATTEMPTS, then L3 gives up."""
        row = self._find(track)
        if row is None or self._db is None:
            return False
        attempts = (row["l3_attempts"] or 0) + (state == L3_FAILED)
        if state == L3_FAILED and attempts >= L3_MAX_ATTEMPTS:
            state = L3_GAVE_UP
        json_value = plan_json if plan_json is not None else row["l3_json"]
        self._db.execute(
            "UPDATE lyrics SET l3_state = ?, l3_json = ?, l3_attempts = ? WHERE id = ?", (state, json_value, attempts, row["id"])
        )
        self._db.commit()
        return True

    async def remove(self, track: TrackInfo) -> bool:
        """Forget the song (binding or unbinding, FR-12): the next play searches again."""
        row = self._find(track)
        if row is None or self._db is None:
            return False
        self._db.execute("DELETE FROM lyrics WHERE id = ?", (row["id"],))
        self._db.commit()
        return True


def _redo_wrong_languages(db: sqlite3.Connection) -> None:
    """Forget the L3 outcome of songs whose language changes once leading credits do not count."""
    redo = []
    rows = db.execute("SELECT id, lines_json FROM lyrics WHERE l3_state IS NOT NULL AND l3_state != ? AND lines_json IS NOT NULL", (L3_REVERTED,))
    for row in rows:
        lines = tuple(LyricLine(float(s), str(t)) for s, t in json.loads(row["lines_json"]))
        if script_language("".join(line.text for line in lines)) != language_of(lines):
            redo.append((row["id"],))
    db.executemany("UPDATE lyrics SET l3_state = NULL, l3_json = NULL, l3_attempts = NULL WHERE id = ?", redo)
    if redo:
        log.info("cache: %d songs aligned in the wrong language will be aligned again", len(redo))


def _dump_tuple(values: tuple[str | None, ...] | None) -> str | None:
    return None if values is None else json.dumps(list(values), ensure_ascii=False)


def _load_tuple(text: str | None) -> tuple[str | None, ...] | None:
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return tuple(data) if isinstance(data, list) else None


def with_track(entry: CachedEntry, track: TrackInfo) -> CachedEntry:
    return replace(entry, result=replace(entry.result, track=track))
