import httpx
import pytest
import respx

from lyricfloat.lyrics.chinese import ChineseConverter
from lyricfloat.lyrics.pipeline import OpenCCPipeline
from lyricfloat.lyrics.service import LyricResolver
from lyricfloat.lyrics.sources.base import SourceError
from lyricfloat.lyrics.sources.netease import BASE_URL, NeteaseSource, make_client
from lyricfloat.models import ResultKind, TrackInfo

TRACK = TrackInfo("紙燈籠 (Live)", "範例樂團 & 客座歌手", "", 200.0, "Spotify.exe")
SEARCH = f"{BASE_URL}/api/cloudsearch/pc"
LYRIC = f"{BASE_URL}/api/song/lyric"


def song(sid, name="纸灯笼", artist="范例乐团", dt=200_000):
    return {"id": sid, "name": name, "ar": [{"id": 1, "name": artist}], "al": {"name": "专辑"}, "dt": dt}


def lyric_payload(lrc="[00:01.00]第一行\n[00:05.00]第二行", tlyric=None, **extra):
    data = {"code": 200, "lrc": {"version": 1, "lyric": lrc}}
    if tlyric is not None:
        data["tlyric"] = {"version": 1, "lyric": tlyric}
    data.update(extra)
    return data


@pytest.fixture
async def source():
    src = NeteaseSource(make_client())
    yield src
    await src._client.aclose()


@respx.mock
async def test_search_maps_songs_and_sends_clean_terms(source):
    route = respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json={"code": 200, "result": {"songs": [song(11), song(12, dt=260_000)]}})
    )
    cands = await source.search(TRACK)
    params = dict(route.calls.last.request.url.params)
    assert params["s"] == "紙燈籠 範例樂團"  # version note stripped, primary artist only
    assert params["type"] == "1"
    assert params["limit"] == "30"  # exact matches can rank well below the top 10
    assert route.calls.last.request.headers["referer"] == BASE_URL + "/"
    assert "cookie" not in route.calls.last.request.headers
    assert [c.source_id for c in cands] == ["11", "12"]
    assert cands[0].duration == 200.0 and cands[1].duration == 260.0
    assert cands[0].artist == "范例乐团" and cands[0].album == "专辑"
    assert not cands[0].has_synced  # lyrics come later


@respx.mock
async def test_search_survives_one_dropped_connection(source):
    route = respx.get(SEARCH).mock(
        side_effect=[
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.Response(200, json={"code": 200, "result": {"songs": [song(11)]}}),
        ]
    )
    assert [c.source_id for c in await source.search(TRACK)] == ["11"]
    assert route.call_count == 2


@respx.mock
async def test_empty_search_result(source):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200, "result": {"songCount": 0}}))
    assert await source.search(TRACK) == []
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200}))
    assert await source.search(TRACK) == []


@respx.mock
async def test_encrypted_string_result_is_treated_as_broken(source):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200, "result": "35b1748964af8a7c"}))
    with pytest.raises(SourceError):
        await source.search(TRACK)


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(503, headers={"Retry-After": "0"}), httpx.Response(200, json={"code": -460, "message": "x"})])
async def test_errors_raise_source_error(source, response):
    respx.get(SEARCH).mock(return_value=response)
    with pytest.raises(SourceError):
        await source.search(TRACK)


@respx.mock
async def test_fetch_lyrics_with_translation(source):
    respx.get(LYRIC).mock(
        return_value=httpx.Response(200, json=lyric_payload(tlyric="[00:01.00]line one\n[00:05.00]line two"))
    )
    (cand,) = [c for c in [await source.fetch_lyrics(_cand())]]
    assert cand.has_synced
    assert cand.translation_lrc.startswith("[00:01.00]")
    assert not cand.instrumental


@respx.mock
async def test_fetch_lyrics_without_timestamps_is_plain(source):
    respx.get(LYRIC).mock(return_value=httpx.Response(200, json=lyric_payload(lrc="第一行\n第二行")))
    cand = await source.fetch_lyrics(_cand())
    assert not cand.has_synced and cand.has_plain


@respx.mock
async def test_fetch_lyrics_instrumental(source):
    respx.get(LYRIC).mock(return_value=httpx.Response(200, json={"code": 200, "nolyric": True}))
    cand = await source.fetch_lyrics(_cand())
    assert cand.instrumental and not cand.has_synced and not cand.has_plain


@respx.mock
async def test_fetch_lyrics_uncollected(source):
    respx.get(LYRIC).mock(return_value=httpx.Response(200, json={"code": 200, "uncollected": True}))
    cand = await source.fetch_lyrics(_cand())
    assert not (cand.has_synced or cand.has_plain or cand.instrumental)


PIPE = OpenCCPipeline(converter=ChineseConverter())


def resolver(source):
    # Real pipeline: the traditional query goes through t2s to match NetEase's simplified titles.
    return LyricResolver([source], pipeline=PIPE)


def _cand():
    from lyricfloat.models import Candidate

    return Candidate("netease", "11", "纸灯笼", "范例乐团", "专辑", 200.0)


# --- through the resolver: metadata scoring, then lazy fetch -------------------------


@respx.mock
async def test_resolver_fetches_only_the_best_metadata_match(source):
    respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "result": {"songs": [song(1, name="另一首歌"), song(2), song(3, dt=300_000)]}},
        )
    )
    lyric_route = respx.get(LYRIC).mock(return_value=httpx.Response(200, json=lyric_payload()))
    result = await resolver(source).resolve(TRACK)
    assert result.kind is ResultKind.SYNCED
    assert result.candidate.source_id == "2"
    assert [dict(c.request.url.params)["id"] for c in lyric_route.calls] == ["2"]
    # Simplified source: every line converted to traditional.
    assert [line.text for line in result.lyrics.lines] == ["第一行", "第二行"]


@respx.mock
async def test_resolver_tries_the_next_match_when_one_has_no_lyrics(source):
    respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json={"code": 200, "result": {"songs": [song(2), song(4, dt=201_000)]}})
    )

    def lyric(request):
        if request.url.params["id"] == "2":
            return httpx.Response(200, json={"code": 200, "uncollected": True})
        return httpx.Response(200, json=lyric_payload())

    route = respx.get(LYRIC).mock(side_effect=lyric)
    result = await resolver(source).resolve(TRACK)
    assert result.candidate.source_id == "4"
    assert route.call_count == 2


@respx.mock
async def test_resolver_caps_lyric_fetches(source):
    songs = [song(i, dt=200_000 + i * 100) for i in range(1, 7)]
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200, "result": {"songs": songs}}))
    route = respx.get(LYRIC).mock(return_value=httpx.Response(200, json={"code": 200, "uncollected": True}))
    result = await resolver(source).resolve(TRACK)
    assert result.kind is ResultKind.NOT_FOUND
    assert route.call_count == 3


@respx.mock
async def test_all_fetches_failing_counts_as_source_failure(source):
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200, "result": {"songs": [song(2)]}}))
    respx.get(LYRIC).mock(return_value=httpx.Response(500))
    assert (await resolver(source).resolve(TRACK)).kind is ResultKind.SOURCES_FAILED


@respx.mock
async def test_free_text_search_sends_the_text_as_is(source):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"code": 200, "result": {"songs": [song(11)]}}))
    cands = await source.search_text("两难-加木")  # SPEC v1.34
    assert dict(route.calls.last.request.url.params)["s"] == "两难-加木"
    assert [c.source_id for c in cands] == ["11"]
