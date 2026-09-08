from __future__ import annotations

import httpx
import pytest

import simpsons_insight_agent.forum_sources as forums
from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.forum_sources import _HttpFetcher, _retry_after_seconds
from simpsons_insight_agent.sources import (
    SourceBlockedError,
    SourceCanceledError,
    SourceNotFoundError,
    SourceUnavailableError,
)

URL = "https://www.ptt.cc/bbs/Food/index.html"


@pytest.mark.parametrize("status", [404, 410])
async def test_deleted_resource_is_not_a_source_block(status: int) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, request=request)
    )) as client:
        with pytest.raises(SourceNotFoundError):
            await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(URL)


@pytest.mark.parametrize("failure", ["timeout", "server"])
async def test_transient_failures_retry_then_succeed(failure: str, monkeypatch) -> None:
    calls = 0
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("fixture", request=request)
            return httpx.Response(503, request=request)
        return httpx.Response(200, text="ok", request=request)

    monkeypatch.setattr(forums.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(URL) == "ok"
    assert calls == 3
    assert waits == [1.0, 2.0]


async def test_persistent_server_error_has_bounded_retries(monkeypatch) -> None:
    calls = 0

    async def sleep(_seconds):
        pass

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(502, request=request)

    monkeypatch.setattr(forums.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceUnavailableError, match="502"):
            await _HttpFetcher(Settings(source_http_retries=1), 0, {"www.ptt.cc"}, client).get(URL)
    assert calls == 2


async def test_injected_client_cannot_follow_unchecked_redirect() -> None:
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(SourceBlockedError, match="未允許網域"):
            await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(URL)
    assert urls == [URL]


@pytest.mark.parametrize("url", [
    "https://user:password@www.ptt.cc/bbs/Food/index.html",
    "https://www.ptt.cc:wrong/bbs/Food/index.html",
    "https://www.ptt.cc/ask/over18",
    "https://www.ptt.cc/login",
])
async def test_unsafe_or_restricted_url_rejected_before_network(url) -> None:
    def handler(_request):
        pytest.fail("must reject before sending a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceBlockedError):
            await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(url)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "not-a-date"])
def test_retry_after_invalid_values_are_finite(value) -> None:
    assert _retry_after_seconds(value) == 1.0


async def test_cancellation_interrupts_retry_after(monkeypatch) -> None:
    canceled = False
    waits = []

    async def sleep(seconds):
        nonlocal canceled
        waits.append(seconds)
        canceled = True

    async def is_canceled():
        return canceled

    monkeypatch.setattr(forums.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(429, headers={"retry-after": "30"})
    )) as client:
        with pytest.raises(SourceCanceledError):
            await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(URL, is_canceled=is_canceled)
    assert waits == [0.25]


async def test_article_quoting_error_message_is_not_blocked() -> None:
    body = '<html><title>PTT 討論</title><div id="main-content">如何解決 access denied？</div></html>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, text=body)
    )) as client:
        assert await _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client).get(URL) == body
