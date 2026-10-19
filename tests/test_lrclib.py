import httpx
import pytest
import respx

from lyricfloat.lyrics.sources.base import SourceError
from lyricfloat.lyrics.sources.lrclib import BASE_URL, USER_AGENT, LrclibSource, make_client
from lyricfloat.models import TrackInfo

TRACK = TrackInfo(
    title="Paper Lanterns (2011 Remaster)",
    artist="The Example Band & Guest",
    album="Lights",
    duration=200.4,
)

ITEM = {
    "id": 42,
    "trackName": "Paper Lanterns",
    "artistName": "The Example Band",
    "albumName": "Lights",
    "duration": 200.0,
    "instrumental": False,
    "plainLyrics": "first line\nsecond line",
    "syncedLyrics": "[00:01.00]first line\n[00:05.00]second line",
}


@pytest.fixture
async def source():
    src = LrclibSource(make_client())
    yield src
    await src._client.aclose()


@respx.mock
async def test_lookup_sends_full_metadata_and_user_agent(source):
    route = respx.get(f"{BASE_URL}/api/get").mock(return_value=httpx.Response(200, json=ITEM))
    cand = await source.lookup(TRACK)

    request = route.calls.last.request
    assert request.headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("LyricFloat/")
    params = dict(request.url.params)
    assert params == {
        "track_name": TRACK.title,
        "artist_name": TRACK.artist,
        "album_name": "Lights",
        "duration": "200",
    }
    assert cand.source == "lrclib"
    assert cand.source_id == "42"
    assert cand.has_synced and cand.has_plain
    assert cand.duration == 200.0


@respx.mock
async def test_lookup_omits_unknown_album_and_duration(source):
    route = respx.get(f"{BASE_URL}/api/get").mock(return_value=httpx.Response(200, json=ITEM))
    await source.lookup(TrackInfo(title="Paper Lanterns", artist="The Example Band"))
    params = dict(route.calls.last.request.url.params)
    assert "album_name" not in params
    assert "duration" not in params


@respx.mock
async def test_lookup_not_found_returns_none(source):
    respx.get(f"{BASE_URL}/api/get").mock(
        return_value=httpx.Response(404, json={"name": "TrackNotFound", "statusCode": 404})
    )
    assert await source.lookup(TRACK) is None


@respx.mock
async def test_search_uses_cleaned_title_and_primary_artist(source):
    route = respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json=[ITEM, ITEM]))
    cands = await source.search(TRACK)
    params = dict(route.calls.last.request.url.params)
    assert params == {"track_name": "paper lanterns", "artist_name": "the example band"}
    assert len(cands) == 2


@respx.mock
async def test_search_without_artist_sends_title_only(source):
    route = respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json=[]))
    assert await source.search(TrackInfo(title="Paper Lanterns", artist="")) == []
    assert dict(route.calls.last.request.url.params) == {"track_name": "paper lanterns"}


@respx.mock
async def test_an_empty_artist_search_asks_by_title_alone(source):
    # SPEC v1.33: the entry may name another of the song's artists.
    other = {**ITEM, "artistName": "Someone Else"}
    route = respx.get(f"{BASE_URL}/api/search").mock(side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[other])])
    (cand,) = await source.search(TRACK)
    assert [dict(c.request.url.params) for c in route.calls] == [
        {"track_name": "paper lanterns", "artist_name": "the example band"},
        {"track_name": "paper lanterns"},
    ]
    assert cand.title_only


@respx.mock
async def test_hits_found_with_the_artist_are_not_title_only(source):
    route = respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json=[ITEM]))
    (cand,) = await source.search(TRACK)
    assert route.call_count == 1 and not cand.title_only


@respx.mock
async def test_instrumental_and_missing_lyrics_map_cleanly(source):
    item = {**ITEM, "instrumental": True, "plainLyrics": None, "syncedLyrics": None}
    respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json=[item]))
    (cand,) = await source.search(TRACK)
    assert cand.instrumental
    assert not cand.has_synced and not cand.has_plain


@respx.mock
@pytest.mark.parametrize("status", [500, 429, 503])
async def test_http_errors_raise_source_error(source, status):
    # A busy 503 is asked again once (SPEC v1.65): with no wait here, and busy again.
    respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(status, headers={"Retry-After": "0"}))
    with pytest.raises(SourceError):
        await source.search(TRACK)


@respx.mock
async def test_network_errors_raise_source_error(source):
    respx.get(f"{BASE_URL}/api/get").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(SourceError):
        await source.lookup(TRACK)


@respx.mock
async def test_dropped_connection_is_retried_once(source):
    dropped = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    route = respx.get(f"{BASE_URL}/api/search").mock(side_effect=[dropped, httpx.Response(200, json=[ITEM])])
    assert [c.source_id for c in await source.search(TRACK)] == ["42"]
    assert route.call_count == 2
    route.mock(side_effect=[dropped, dropped])
    with pytest.raises(SourceError):
        await source.search(TRACK)


@respx.mock
async def test_bad_payload_raises_source_error(source):
    respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(SourceError):
        await source.search(TRACK)


@respx.mock
async def test_free_text_search_uses_q(source):
    route = respx.get(f"{BASE_URL}/api/search").mock(return_value=httpx.Response(200, json=[ITEM]))
    (cand,) = await source.search_text("两难-加木")  # SPEC v1.34
    assert dict(route.calls.last.request.url.params) == {"q": "两难-加木"}
    assert not cand.title_only
