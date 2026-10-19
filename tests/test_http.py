import httpx
import pytest
import respx

from lyricfloat.lyrics.sources import http
from lyricfloat.lyrics.sources.http import get_with_retry

URL = "https://example.test/api/search"
DROPPED = httpx.RemoteProtocolError("Server disconnected without sending a response.")


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url="https://example.test") as c:
        yield c


@respx.mock
async def test_dropped_connection_is_sent_once_more(client):
    route = respx.get(URL).mock(side_effect=[DROPPED, httpx.Response(200, json=[])])
    response = await get_with_retry(client, "/api/search", {"q": "x"})
    assert response.status_code == 200
    assert route.call_count == 2
    assert route.calls.last.request.url.params["q"] == "x"


@respx.mock
@pytest.mark.parametrize("error", [httpx.ReadError("reset"), httpx.ConnectError("refused")])
async def test_other_network_errors_are_retried_too(client, error):
    route = respx.get(URL).mock(side_effect=[error, httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_only_one_retry(client):
    route = respx.get(URL).mock(side_effect=[DROPPED, DROPPED, httpx.Response(200, json=[])])
    with pytest.raises(httpx.RemoteProtocolError):
        await get_with_retry(client, "/api/search", {})
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
async def test_timeouts_are_not_retried(client, error):
    route = respx.get(URL).mock(side_effect=[error, httpx.Response(200, json=[])])
    with pytest.raises(httpx.TimeoutException):
        await get_with_retry(client, "/api/search", {})
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [404, 429, 500, 502])
async def test_http_error_statuses_are_not_retried(client, status):
    route = respx.get(URL).mock(side_effect=[httpx.Response(status), httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == status
    assert route.call_count == 1


OVERLOADED = {"name": "ServerOverloaded", "message": "The server is busy, please retry in a moment", "statusCode": 503}


@pytest.fixture
def waits(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    return waited


@respx.mock
async def test_busy_server_is_asked_again_after_its_retry_after(client, waits):
    busy = httpx.Response(503, json=OVERLOADED, headers={"Retry-After": "1"})
    route = respx.get(URL).mock(side_effect=[busy, httpx.Response(200, json=[])])
    response = await get_with_retry(client, "/api/search", {"q": "x"})
    assert response.status_code == 200
    assert route.call_count == 2
    assert route.calls.last.request.url.params["q"] == "x"
    assert waits == [1.0]


@respx.mock
async def test_busy_without_retry_after_waits_a_second(client, waits):
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == 200
    assert route.call_count == 2
    assert waits == [1.0]


@respx.mock
@pytest.mark.parametrize("retry_after", ["5", "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_busy_asking_a_longer_wait_is_not_retried(client, waits, retry_after):
    busy = httpx.Response(503, headers={"Retry-After": retry_after})
    route = respx.get(URL).mock(side_effect=[busy, httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == 503
    assert route.call_count == 1
    assert waits == []


@respx.mock
async def test_busy_is_retried_only_once(client, waits):
    busy = httpx.Response(503, headers={"Retry-After": "1"})
    route = respx.get(URL).mock(side_effect=[busy, busy, httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == 503
    assert route.call_count == 2


@respx.mock
async def test_a_dropped_connection_then_busy_is_not_retried_again(client, waits):
    busy = httpx.Response(503, headers={"Retry-After": "1"})
    route = respx.get(URL).mock(side_effect=[DROPPED, busy, httpx.Response(200, json=[])])
    assert (await get_with_retry(client, "/api/search", {})).status_code == 503
    assert route.call_count == 2
    assert waits == []
