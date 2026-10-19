"""NetEase Cloud Music source: non-public endpoints, simplified Chinese content.

Endpoints checked on 2026-10-07 (SPEC v1.6): /api/cloudsearch/pc for
search (the older /api/search/get/web now returns an encrypted string)
and /api/song/lyric for lyrics plus translation. No account cookie is
ever sent. Failures surface as SourceError so the resolver can skip
the source silently.
"""

from __future__ import annotations

from typing import Any

import httpx

from lyricfloat import APP_NAME, __version__
from lyricfloat.lyrics.lrc import parse_lrc
from lyricfloat.lyrics.normalize import normalize_title, split_artists
from lyricfloat.lyrics.sources.base import LyricSource, SourceError
from lyricfloat.lyrics.sources.http import get_with_retry
from lyricfloat.lyrics.sources.lrclib import REQUEST_TIMEOUT
from lyricfloat.models import Candidate, TrackInfo

BASE_URL = "https://music.163.com"
USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {APP_NAME}/{__version__}"
# An exact match can rank far down: one song came 24th of 24 on 2026-10-07. The whole
# page is still a single request, and lyric fetches stay capped by the resolver.
SEARCH_LIMIT = 30


def make_client(timeout: float = REQUEST_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )


def _lyric_text(data: dict[str, Any], key: str) -> str | None:
    block = data.get(key)
    if isinstance(block, dict):
        text = block.get("lyric")
        if isinstance(text, str) and text.strip():
            return text
    return None


class NeteaseSource(LyricSource):
    name = "netease"
    simplified = True
    lyrics_inline = False

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or make_client()
        self._owns_client = client is None

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await get_with_retry(self._client, path, params)
        except httpx.HTTPError as exc:
            raise SourceError(f"netease {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(f"netease {path}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"netease {path}: invalid JSON") from exc
        if not isinstance(data, dict):
            raise SourceError(f"netease {path}: expected an object")
        code = data.get("code")
        if code not in (None, 200):
            raise SourceError(f"netease {path}: code {code}")
        return data

    async def search(self, track: TrackInfo) -> list[Candidate]:
        terms = [normalize_title(track.title)]
        artists = split_artists(track.artist)
        if artists:
            terms.append(artists[0])
        return await self.search_text(" ".join(terms))

    async def search_text(self, text: str) -> list[Candidate]:
        """cloudsearch is a free-text search already: title and artist go in one string."""
        data = await self._get_json("/api/cloudsearch/pc", {"s": text, "type": 1, "limit": SEARCH_LIMIT, "offset": 0})
        result = data.get("result")
        if result is None:
            return []
        if not isinstance(result, dict):
            # The old web endpoint started returning an encrypted string here; treat it as broken.
            raise SourceError("netease search: unexpected result format")
        songs = result.get("songs") or []
        candidates = []
        for song in songs:
            if not isinstance(song, dict) or "id" not in song:
                continue
            artists_list = song.get("ar") or song.get("artists") or []
            album = song.get("al") or song.get("album") or {}
            candidates.append(
                Candidate(
                    source=self.name,
                    source_id=str(song["id"]),
                    title=str(song.get("name") or ""),
                    artist=" / ".join(str(a.get("name", "")) for a in artists_list if isinstance(a, dict)),
                    album=str(album.get("name") or "") if isinstance(album, dict) else "",
                    duration=float(song.get("dt") or song.get("duration") or 0) / 1000.0,
                )
            )
        return candidates

    async def fetch_lyrics(self, candidate: Candidate) -> Candidate:
        data = await self._get_json("/api/song/lyric", {"id": candidate.source_id, "lv": 1, "kv": 1, "tv": -1})
        lrc = _lyric_text(data, "lrc")
        translation = _lyric_text(data, "tlyric")
        instrumental = bool(data.get("nolyric"))
        # Some entries have lyrics without timestamps: those count as plain text.
        synced = lrc if lrc and parse_lrc(lrc).lyrics.lines else None
        plain = lrc if lrc and synced is None else None
        return Candidate(
            source=candidate.source,
            source_id=candidate.source_id,
            title=candidate.title,
            artist=candidate.artist,
            album=candidate.album,
            duration=candidate.duration,
            synced_lrc=synced,
            plain_text=plain,
            instrumental=instrumental,
            translation_lrc=translation,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
