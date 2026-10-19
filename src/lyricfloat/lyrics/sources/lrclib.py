"""LRCLIB source (https://lrclib.net) — open, keyless, returns LRC inline."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx

from lyricfloat import APP_NAME, __version__
from lyricfloat.lyrics.normalize import normalize_title, split_artists
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.lyrics.sources.http import get_with_retry
from lyricfloat.models import Candidate, TrackInfo

BASE_URL = "https://lrclib.net"
USER_AGENT = f"{APP_NAME}/{__version__} (personal use, non-distributed)"
REQUEST_TIMEOUT = 3.0  # seconds, per HTTP request (SPEC v1.4)


def make_client(timeout: float = REQUEST_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


def _to_candidate(item: dict[str, Any]) -> Candidate:
    return Candidate(
        source=LrclibSource.name,
        source_id=str(item.get("id", "")),
        title=item.get("trackName") or item.get("name") or "",
        artist=item.get("artistName") or "",
        album=item.get("albumName") or "",
        duration=float(item.get("duration") or 0.0),
        synced_lrc=item.get("syncedLyrics") or None,
        plain_text=item.get("plainLyrics") or None,
        instrumental=bool(item.get("instrumental")),
    )


class LrclibSource(LyricSource):
    name = "lrclib"
    simplified = False

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or make_client()
        self._owns_client = client is None

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await get_with_retry(self._client, path, params)
        except httpx.HTTPError as exc:
            raise SourceError(f"lrclib {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(f"lrclib {path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"lrclib {path}: invalid JSON") from exc

    async def lookup(self, track: TrackInfo) -> Candidate | None:
        params: dict[str, Any] = {"track_name": track.title, "artist_name": track.artist}
        if track.album:
            params["album_name"] = track.album
        if track.duration > 0:
            params["duration"] = round(track.duration)
        data = await self._get_json("/api/get", params)
        if not isinstance(data, dict):
            return None
        return _to_candidate(data)

    async def search(self, track: TrackInfo) -> list[Candidate]:
        # Version notes (Remaster, Live, bracketed translations) hurt LRCLIB recall.
        params: dict[str, Any] = {"track_name": normalize_title(track.title)}
        artists = split_artists(track.artist)
        if artists:
            params["artist_name"] = artists[0]
        found = await self._search(params)
        if found or "artist_name" not in params:
            return found
        # LRCLIB may file the song under another of its artists (a YouTube video names one,
        # the entry another): ask by title alone, and let scoring check the artist (SPEC v1.33).
        return [replace(c, title_only=True) for c in await self._search({"track_name": params["track_name"]})]

    async def search_text(self, text: str) -> list[Candidate]:
        return await self._search({"q": text})

    async def _search(self, params: dict[str, Any]) -> list[Candidate]:
        data = await self._get_json("/api/search", params)
        if data is None:
            return []
        if not isinstance(data, list):
            raise SourceError("lrclib /api/search: expected a list")
        return [_to_candidate(item) for item in data if isinstance(item, dict)]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
