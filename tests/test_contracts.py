"""Source contract tests (spec test strategy): the real response formats, replayed.

Default: replay the cassettes in tests/contracts, no network. With
--record-contracts the same tests hit the live endpoints and re-record, so
a changed or broken source fails here before a user notices. Cassettes
never hold lyric text: tests/contract_scrub.py replaces it on the way in.
"""

import json
import re
from pathlib import Path

import pytest
import vcr
import yaml

from contract_scrub import scrub_json, scrub_lrc
from lyricfloat.lyrics.lrc import parse_lrc
from lyricfloat.lyrics.sources.lrclib import LrclibSource
from lyricfloat.lyrics.sources.netease import NeteaseSource
from lyricfloat.lyrics.translation import parse_translation
from lyricfloat.models import TrackInfo

CASSETTES = Path(__file__).parent / "contracts"
EN = TrackInfo("Viva La Vida", "Coldplay", "Viva La Vida or Death and All His Friends", 242.0)
ZH = TrackInfo("小幸運", "田馥甄", "我的少女時代 電影原聲帶", 265.0)


@pytest.fixture
def cassette(request):
    record = request.config.getoption("--record-contracts")
    name = f"{request.node.name}.yaml"
    if not record and not (CASSETTES / name).exists():
        pytest.skip(f"no cassette {name}; run with --record-contracts")
    from contract_scrub import scrub_response

    recorder = vcr.VCR(
        cassette_library_dir=str(CASSETTES),
        record_mode="all" if record else "none",
        match_on=["method", "scheme", "host", "path", "query"],
        decode_compressed_response=True,
        before_record_response=scrub_response,
        filter_headers=["cookie", "set-cookie", "user-agent"],
    )
    with recorder.use_cassette(name):
        yield


_TIMED = re.compile(r"^((?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+)(.*)$")


def _lyric_fields(data):
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        for field in ("syncedLyrics", "plainLyrics"):
            if isinstance(item.get(field), str):
                yield item[field]
        for block in ("lrc", "tlyric", "klyric", "romalrc", "yrc"):
            value = item.get(block)
            if isinstance(value, dict) and isinstance(value.get("lyric"), str):
                yield value["lyric"]


def test_cassettes_hold_no_lyric_text():
    """Every lyric line in every cassette must be a placeholder (copyright, SPEC v1.6)."""
    for path in CASSETTES.glob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for interaction in doc["interactions"]:
            data = json.loads(interaction["response"]["body"]["string"])
            for text in _lyric_fields(data):
                for line in text.splitlines():
                    match = _TIMED.match(line)
                    rest = (match.group(2) if match else line).strip()
                    assert not rest or re.fullmatch(r"line \d+", rest) or line.lower().startswith("[offset"), path.name


async def test_lrclib_get_contract(cassette):
    src = LrclibSource()
    cand = await src.lookup(EN)
    await src.aclose()
    assert cand is not None
    assert cand.title and cand.artist and cand.duration > 0
    assert cand.has_synced and cand.has_plain
    assert len(parse_lrc(cand.synced_lrc).lyrics.lines) > 20


async def test_lrclib_search_contract(cassette):
    src = LrclibSource()
    cands = await src.search(ZH)
    await src.aclose()
    assert cands
    assert all(c.source_id and c.title and c.duration > 0 for c in cands)
    assert any(c.has_synced for c in cands)


async def test_netease_search_contract(cassette):
    src = NeteaseSource()
    cands = await src.search(EN)
    await src.aclose()
    assert cands
    assert all(c.source_id.isdigit() and c.title and c.artist for c in cands)
    assert any(abs(c.duration - EN.duration) < 3 for c in cands)


async def test_netease_lyric_contract(cassette):
    src = NeteaseSource()
    (first, *_rest) = [c for c in await src.search(EN) if abs(c.duration - EN.duration) < 3]
    filled = await src.fetch_lyrics(first)
    await src.aclose()
    assert filled.has_synced
    assert len(parse_lrc(filled.synced_lrc).lyrics.lines) > 20
    assert len(parse_translation(filled.translation_lrc)) > 10  # English song: translation present


# --- the scrubber itself -------------------------------------------------------------


def test_scrub_keeps_timestamps_and_offset_only():
    raw = "[ar:Someone]\n[offset:+250]\n[00:01.00][00:30.00]real words here\n[00:05.50]more words\nuntimed credit"
    assert scrub_lrc(raw) == "[offset:+250]\n[00:01.00][00:30.00]line 1\n[00:05.50]line 2\nline 3"


def test_scrub_json_handles_both_sources():
    lrclib = [{"id": 1, "syncedLyrics": "[00:01.00]words", "plainLyrics": "words", "lyricsfile": "x"}]
    assert scrub_json(lrclib)[0] == {"id": 1, "syncedLyrics": "[00:01.00]line 1", "plainLyrics": "line 1", "lyricsfile": "scrubbed"}
    netease = {"lrc": {"lyric": "[00:01.00]词"}, "tlyric": {"lyric": "[00:01.00]译"}, "lyricUser": {"nickname": "someone"}}
    out = scrub_json(netease)
    assert out["lrc"]["lyric"] == "[00:01.00]line 1"
    assert out["tlyric"]["lyric"] == "[00:01.00]line 1"
    assert out["lyricUser"] == {}
