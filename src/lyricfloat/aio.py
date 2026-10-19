"""Small asyncio helpers shared by the Qt-facing objects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    """Schedule a coroutine on the running loop (qasync in the app).

    Without a running loop (e.g. a synchronous test) the coroutine is closed
    instead of being left un-awaited.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        log.debug("no running event loop, skipped %s", getattr(coro, "__qualname__", coro))
        return None
    return loop.create_task(coro)
