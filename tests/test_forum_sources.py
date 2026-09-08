from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta, timezone
from pathlib import Path

import httpx
import pytest

import simpsons_insight_agent.forum_sources as forum_sources
from simpsons_insight_agent.author_privacy import AuthorHasher
from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.forum_sources import (
    _HttpFetcher,
    parse_dcard_article,
    parse_ptt_article,
    parse_ptt_search,
)
from simpsons_insight_agent.sources import SourceBlockedError

FIXTURES = Path(__file__).parent / "fixtures"


def hasher(tmp_path: Path) -> AuthorHasher:
    return AuthorHasher(Settings(author_hash_key_path=tmp_path / "author.key"))


def test_ptt_search_and_article_parser_preserve_duplicate_pushes_without_authors(
    tmp_path: Path,
) -> None:
    links, previous = parse_ptt_search(
        (FIXTURES / "ptt_search.html").read_text(encoding="utf-8"),
        "Food",
    )
    assert links == [
        "/bbs/Food/M.1767225000.A.001.html",
        "/bbs/Food/M.1767224000.A.002.html",
    ]
    assert previous == "/bbs/Food/search?page=2&q=%E7%AF%84%E4%BE%8B"

    items = parse_ptt_article(
        (FIXTURES / "ptt_article.html").read_text(encoding="utf-8"),
        "https://www.ptt.cc/bbs/Food/M.1767225000.A.001.html",
        hasher(tmp_path),
    )
    assert len(items) == 4
    assert items[0].content_type == "post"
    assert items[0].board == "Food"
    assert items[0].text == "產品口感很好 服務也很親切"
    assert [item.platform_data["signal"] for item in items[1:]] == [
        "push",
        "push",
        "boo",
    ]
    assert items[1].source_item_id != items[2].source_item_id
    assert all(
        item.published_at
        and item.published_at.astimezone(timezone(timedelta(hours=8))).year == 2026
        for item in items[1:]
    )
    serialized = str([asdict(item) for item in items])
    assert "alice" not in serialized
    assert "bob" not in serialized
    assert "charlie" not in serialized


def test_dcard_public_page_marks_partial_comments_and_discards_author_names(
    tmp_path: Path,
) -> None:
    items, complete = parse_dcard_article(
        (FIXTURES / "dcard_article_partial.html").read_text(encoding="utf-8"),
        "https://www.dcard.tw/f/food/p/256789012",
        hasher(tmp_path),
    )
    assert complete is False
    assert [item.content_type for item in items] == ["post", "comment", "comment"]
    assert items[0].platform_data["reaction_count"] == 42
    assert items[1].platform_data["reaction_count"] == 5
    serialized = str([asdict(item) for item in items])
    assert "秘密卡稱" not in serialized
    assert "卡友甲" not in serialized
    assert "卡友乙" not in serialized


@pytest.mark.asyncio
async def test_http_fetcher_rejects_cross_domain_and_over18_redirects() -> None:
    responses = {
        "https://www.dcard.tw/f/food/p/1": httpx.Response(
            302, headers={"location": "https://evil.example/steal"}
        ),
        "https://www.ptt.cc/bbs/Gossiping/index.html": httpx.Response(
            302, headers={"location": "/ask/over18?from=/bbs/Gossiping/index.html"}
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses[str(request.url)]
        response.request = request
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dcard = _HttpFetcher(Settings(), 0, {"www.dcard.tw"}, client)
        with pytest.raises(SourceBlockedError, match="未允許網域"):
            await dcard.get("https://www.dcard.tw/f/food/p/1")
        ptt = _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client)
        with pytest.raises(SourceBlockedError, match="限制級"):
            await ptt.get("https://www.ptt.cc/bbs/Gossiping/index.html")


@pytest.mark.asyncio
async def test_http_fetcher_honors_retry_after_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    waits: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(forum_sources.asyncio, "sleep", fake_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client)
        assert await fetcher.get("https://www.ptt.cc/bbs/Food/index.html") == "ok"
    assert calls == 2
    assert waits == [0.0]


@pytest.mark.asyncio
async def test_http_fetcher_marks_known_block_page() -> None:
    body = (FIXTURES / "ptt_blocked.html").read_text(encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = _HttpFetcher(Settings(), 0, {"www.ptt.cc"}, client)
        with pytest.raises(SourceBlockedError, match="阻擋頁"):
            await fetcher.get("https://www.ptt.cc/bbs/Food/index.html")
