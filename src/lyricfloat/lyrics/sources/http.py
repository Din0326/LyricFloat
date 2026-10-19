"""HTTP helpers shared by the network sources."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# The connection dropped before any response came back, so the GET can simply be sent
# again. Timeouts are left out: that request's 3 s budget is already spent (SPEC v1.12).
RETRYABLE = (httpx.NetworkError, httpx.RemoteProtocolError)

# LRCLIB answers 503 "ServerOverloaded" with Retry-After: 1 while it is busy (23 times in one
# hour of Din's listening on 2026-10-08); a request a second later usually goes through (SPEC v1.65).
OVERLOADED = 503
MAX_RETRY_AFTER = 1.0  # seconds: a longer wait would not fit the request's 3 s budget


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait before asking again, or None when the server wants longer than we can wait."""
    value = response.headers.get("retry-after")
    if value is None:
        return MAX_RETRY_AFTER
    try:
        seconds = float(value)
    except ValueError:  # an HTTP date: a wait of its own
        return None
    return seconds if 0 <= seconds <= MAX_RETRY_AFTER else None


async def get_with_retry(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> httpx.Response:
    """GET, sent once more when the connection drops without an answer, or when the
    server is busy (503) and asks for a wait we can afford.

    NetEase sometimes closes a connection without responding ("Server disconnected
    without sending a response", seen 2026-10-07 while skipping songs quickly).
    """
    try:
        response = await client.get(path, params=params)
    except RETRYABLE as exc:
        log.info("%s%s: %s, retrying once", client.base_url, path, type(exc).__name__)
        return await client.get(path, params=params)
    if response.status_code == OVERLOADED and (delay := _retry_after(response)) is not None:
        log.info("%s%s: HTTP 503, retrying once in %.1fs", client.base_url, path, delay)
        await asyncio.sleep(delay)
        return await client.get(path, params=params)
    return response
