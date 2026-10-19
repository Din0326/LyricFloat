"""Remove lyric text from recorded responses before they are written to disk (SPEC v1.6).

Timestamps and the JSON structure stay, so the parsers still meet the real
format; every line's text becomes "line N". Uploader names are blanked.
"""

from __future__ import annotations

import json
import re
from typing import Any

_LEADING_TAGS = re.compile(r"^((?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+)")
_ID_TAG = re.compile(r"^\s*\[[A-Za-z#]+\s*:.*\]\s*$")

LRCLIB_TEXT_FIELDS = ("syncedLyrics", "plainLyrics", "lyricsfile")
NETEASE_LYRIC_BLOCKS = ("lrc", "tlyric", "klyric", "romalrc", "yrc", "ytlrc", "yromalrc")
NETEASE_USER_FIELDS = ("lyricUser", "transUser")


def scrub_lrc(text: str) -> str:
    out: list[str] = []
    n = 0
    for line in text.splitlines():
        tags = _LEADING_TAGS.match(line)
        if tags:
            n += 1
            out.append(f"{tags.group(1)}line {n}")
        elif _ID_TAG.match(line):
            out.append(line if line.lower().lstrip().startswith("[offset") else "")
        elif line.strip():
            n += 1
            out.append(f"line {n}")  # untimed text (plain lyrics, credits, word-timing JSON)
    return "\n".join(l for l in out if l != "")


def _scrub_lrclib_item(item: dict[str, Any]) -> None:
    for field in LRCLIB_TEXT_FIELDS:
        if isinstance(item.get(field), str):
            item[field] = scrub_lrc(item[field]) if field != "lyricsfile" else "scrubbed"


def scrub_json(data: Any) -> Any:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _scrub_lrclib_item(item)
        return data
    if isinstance(data, dict):
        _scrub_lrclib_item(data)
        for block in NETEASE_LYRIC_BLOCKS:
            value = data.get(block)
            if isinstance(value, dict) and isinstance(value.get("lyric"), str):
                value["lyric"] = scrub_lrc(value["lyric"])
        for field in NETEASE_USER_FIELDS:
            if field in data:
                data[field] = {}
    return data


def scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """vcrpy before_record_response hook."""
    body = response.get("body", {}).get("string")
    if not body:
        return response
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError):
        return response
    cleaned = json.dumps(scrub_json(data), ensure_ascii=False).encode("utf-8")
    response["body"]["string"] = cleaned
    headers = response.get("headers", {})
    for key in list(headers):
        if key.lower() == "content-length":
            headers[key] = [str(len(cleaned))]
        if key.lower() in ("set-cookie", "content-encoding"):
            del headers[key]
    return response
