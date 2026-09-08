from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from simpsons_insight_agent.author_privacy import AuthorHasher
from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.forum_sources import (
    DcardSource,
    _dcard_search_links,
    dcard_import_item,
    parse_dcard_article,
)
from simpsons_insight_agent.sources import (
    CollectedItem,
    SourceCallbacks,
    SourceCanceledError,
    SourceCheckpoint,
)

BASE = "https://www.dcard.tw"
URL = f"{BASE}/f/food/p/256789012"


def hasher(tmp_path: Path) -> AuthorHasher:
    return AuthorHasher(Settings(author_hash_key_path=tmp_path / "dcard.key"))


def article(post_id: str = "256789012", **overrides: object) -> dict:
    return {
        "@type": "DiscussionForumPosting", "id": post_id,
        "headline": "範例品牌使用心得", "articleBody": "產品品質不錯，客服可以改善。",
        "datePublished": "2026-01-15T10:00:00+08:00", "commentCount": 0,
        **overrides,
    }


def comment(comment_id: str, **overrides: object) -> dict:
    return {
        "commentId": comment_id, "postId": "256789012", "floor": 1,
        "content": f"留言 {comment_id}", "createdAt": "2026-01-15T11:00:00+08:00",
        **overrides,
    }


def html(payload: object) -> str:
    return '<html><script type="application/ld+json">' + json.dumps(payload) + "</script></html>"


def import_row(kind: str = "comment", source_id: str = "c1", **overrides: object) -> dict:
    return {
        "source_url": URL, "item_type": kind, "source_item_id": source_id,
        "thread_id": "256789012", "text": "匯入內容", "published_at": "2026-01-15T12:00:00+08:00",
        "reaction_count": 0, **overrides,
    }


def config(**overrides: object) -> dict:
    return {
        "urls": [], "keywords": [], "forums": [], "max_search_pages": 5,
        "date_from": "2026-01-01", "date_to": "2026-01-31",
        "max_posts": 50, "max_comments": 500, "max_comments_per_thread": 100,
        **overrides,
    }


@dataclass
class Capture:
    items: list[CollectedItem] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)
    cancel_checks: int = 0
    cancel_after: int | None = None

    async def batch(self, items: list[CollectedItem]) -> None:
        self.items.extend(items)

    async def canceled(self) -> bool:
        self.cancel_checks += 1
        return self.cancel_after is not None and self.cancel_checks >= self.cancel_after

    async def on_metrics(self, value: dict) -> None:
        self.metrics.append(value)

    async def noop(self, *args: object) -> None:
        pass

    def callbacks(self) -> SourceCallbacks:
        return SourceCallbacks(self.batch, self.noop, self.canceled, self.noop, self.on_metrics, self.noop)


async def collect(tmp_path: Path, options: dict, responses: dict[str, str | int],
                  checkpoint: SourceCheckpoint | None = None, capture: Capture | None = None):
    requests: list[str] = []
    capture = capture or Capture()

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        requests.append(key)
        value = responses[key]
        return httpx.Response(value if isinstance(value, int) else 200,
                              text=value if isinstance(value, str) else "", request=request)

    settings = Settings(author_hash_key_path=tmp_path / "dcard.key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = DcardSource(settings, client=client)
        source.fetcher.interval = 0
        result = await source.collect(config=options, checkpoint=checkpoint or SourceCheckpoint(),
                                      callbacks=capture.callbacks())
    return result, capture, requests


def test_article_and_comments_are_scoped_to_requested_thread(tmp_path: Path) -> None:
    payload = [
        comment("first-comment", commentCount=999),
        article("999", comments=[comment("foreign", postId=999)]),
        article(comments=[comment("c1"), comment("wrong", postId=999)], commentCount=1),
        comment("orphan", postId=999),
    ]
    items, complete = parse_dcard_article(html(payload), URL, hasher(tmp_path))
    assert [item.source_item_id for item in items] == ["256789012", "first-comment", "c1"]
    assert items[0].text == "產品品質不錯,客服可以改善。"
    assert complete is True


@pytest.mark.parametrize("payload", [
    {"id": "256789012", "excerpt": "搜尋摘要", "title": "搜尋結果"},
    {"headline": "無法辨識來源的建議文章", "articleBody": "非本篇"},
    comment("c1"),
])
def test_excerpts_and_unbound_records_are_not_full_articles(tmp_path: Path, payload: dict) -> None:
    page = '<meta property="og:description" content="備援摘要">' + html(payload)
    assert parse_dcard_article(page, URL, hasher(tmp_path)) == ([], False)


def test_json_ld_url_identity_deleted_and_invalid_counts(tmp_path: Path) -> None:
    record = article(comments=[comment("c1", likeCount="1,234"),
                               comment("deleted", content="", deleted=True)],
                     commentCount="2", likeCount="not numeric")
    record.pop("id")
    record["mainEntityOfPage"] = {"@id": URL}
    items, complete = parse_dcard_article(html(record), URL, hasher(tmp_path))
    assert complete is True
    assert len(items) == 2
    assert items[0].platform_data["reaction_count"] == 0
    assert items[1].platform_data["reaction_count"] == 1234
    assert items[0].platform_data["loaded_comment_count"] == 2


def test_unknown_comment_count_cannot_claim_completeness(tmp_path: Path) -> None:
    record = article()
    record.pop("commentCount")
    items, complete = parse_dcard_article(html(record), URL, hasher(tmp_path))
    assert len(items) == 1
    assert complete is False


def test_unrelated_nested_text_is_not_a_comment(tmp_path: Path) -> None:
    record = article(commentCount=1, comments=[comment("c1", metadata={"text": "內部 metadata"})])
    items, complete = parse_dcard_article(html(record), URL, hasher(tmp_path))
    assert [item.source_item_id for item in items] == ["256789012", "c1"]
    assert complete is True


def test_same_text_comments_without_ids_keep_distinct_authors_and_dates(tmp_path: Path) -> None:
    record = article(commentCount=2, comment=[
        {"@type": "Comment", "text": "推", "author": {"name": "甲"},
         "datePublished": "2026-01-15T10:00:00+08:00"},
        {"@type": "Comment", "text": "推", "author": {"name": "乙"},
         "datePublished": "2026-01-15T11:00:00+08:00"},
    ])
    items, complete = parse_dcard_article(html(record), URL, hasher(tmp_path))
    assert len(items) == 3
    assert items[1].source_item_id != items[2].source_item_id
    assert complete is True


def test_json_ld_and_hydrated_comment_are_one_logical_comment(tmp_path: Path) -> None:
    record = article(commentCount=2, comment=[
        {"@type": "Comment", "text": "推", "floor": 1, "author": {"name": "甲"},
         "datePublished": "2026-01-15T10:00:00+08:00"},
    ])
    hydration = {"props": {"comments": [comment("c1", content="推", floor=1,
        author={"name": "甲"}, createdAt="2026-01-15T02:00:00Z", likeCount=10)]}}
    items, complete = parse_dcard_article(html(record) + html(hydration), URL, hasher(tmp_path))
    assert [item.source_item_id for item in items] == ["256789012", "c1"]
    assert items[1].platform_data["reaction_count"] == 10
    assert items[0].platform_data["loaded_comment_count"] == 1
    assert complete is False


def test_search_links_filter_forums_hosts_and_next_scope() -> None:
    page = f'''<a href="/f/food/p/1?utm_source=share">a</a>
    <a href="{BASE}/f/food/p/1#comments">duplicate</a>
    <a href="/f/pet/p/2">wrong forum</a>
    <a href="https://evil.example/f/food/p/3">external</a>
    <a rel="next" href="/search?query=brand&forum=pet&page=2">next</a>
    <a rel="next" href="/search?query=brand&forum=food&page=2">next</a>'''
    links, next_url, exhausted = _dcard_search_links(page, f"{BASE}/search?query=brand&forum=food", {"food"})
    assert links == [f"{BASE}/f/food/p/1"]
    assert next_url == f"{BASE}/search?query=brand&forum=food&page=2"
    assert exhausted is False


def test_import_post_uses_canonical_public_identity(tmp_path: Path) -> None:
    imported = dcard_import_item(import_row("post", "", source_url=URL + "/"), hasher(tmp_path))
    assert imported.source_item_id == "256789012"
    assert imported.source_url == URL
    assert len(imported.legacy_source_item_ids) == 1


@pytest.mark.asyncio
async def test_existing_import_hash_matches_canonical_post_on_resume(tmp_path: Path) -> None:
    row = import_row("post", "")
    imported = dcard_import_item(row, hasher(tmp_path))
    checkpoint = SourceCheckpoint(known_keys=set(imported.legacy_source_item_ids), post_count=1)
    result, capture, _ = await collect(tmp_path, config(import_records=[row]), {}, checkpoint)
    assert result.post_count == 1
    assert capture.items == []


@pytest.mark.asyncio
async def test_public_search_pagination_is_bounded_and_deduplicated(tmp_path: Path) -> None:
    search = f"{BASE}/search?query=brand&forum=food"
    next_page = search + "&page=2"
    result, captured, requests = await collect(tmp_path, config(keywords=["brand"], forums=["food"], max_search_pages=1), {
        search: f'<a href="{URL}">post</a><a href="{URL}?utm_source=share">duplicate</a>'
                f'<a rel="next" href="{next_page}">next</a>',
        URL: html(article()),
    })
    assert requests == [search, URL]
    assert len(captured.items) == 1
    assert result.stop_reason == "page_limit"
    assert result.complete is False


@pytest.mark.asyncio
async def test_public_search_follows_only_actual_next_links(tmp_path: Path) -> None:
    search = f"{BASE}/search?query=brand"
    next_page = search + "&page=2"
    result, captured, requests = await collect(tmp_path, config(keywords=["brand"]), {
        search: f'<a href="{URL}">post</a><a href="{next_page}">下一頁</a>',
        URL: html(article()), next_page: '<p>沒有更多結果</p>',
    })
    assert requests == [search, URL, next_page]
    assert len(captured.items) == 1
    assert result.complete is True


@pytest.mark.asyncio
async def test_empty_dynamic_search_is_partial(tmp_path: Path) -> None:
    result, _, _ = await collect(tmp_path, config(keywords=["brand"]), {
        f"{BASE}/search?query=brand": '<div id="__next"></div>',
    })
    assert result.stop_reason == "public_search_partial"
    assert result.complete is False


@pytest.mark.asyncio
async def test_post_cap_stops_unnecessary_article_and_search_fetches(tmp_path: Path) -> None:
    result, captured, requests = await collect(tmp_path, config(
        urls=[URL, URL + "/", f"{BASE}/f/food/p/2"], keywords=["brand"], max_posts=1,
    ), {URL: html(article())})
    assert requests == [URL]
    assert len(captured.items) == 1
    assert result.stop_reason == "post_limit"
    assert result.complete is False


@pytest.mark.asyncio
async def test_import_duplicates_and_public_post_identity_share_limits(tmp_path: Path) -> None:
    row = import_row()
    result, captured, requests = await collect(tmp_path, config(urls=[URL], import_records=[
        import_row("post", ""), row, row, import_row(source_id="c2"),
    ], max_comments_per_thread=1), {URL: html(article())})
    assert requests == [URL]
    assert [item.source_item_id for item in captured.items] == ["256789012", "c1"]
    assert (result.post_count, result.comment_count) == (1, 1)


@pytest.mark.asyncio
async def test_comment_cap_is_cumulative_across_public_and_imports(tmp_path: Path) -> None:
    result, captured, _ = await collect(tmp_path, config(urls=[URL], max_comments_per_thread=2,
        import_records=[import_row(source_id="c2"), import_row(source_id="c3")]), {
        URL: html(article(comments=[comment("c1")], commentCount=1)),
    })
    assert [item.source_item_id for item in captured.items] == ["256789012", "c1", "c2"]
    assert result.checkpoint["thread_comment_counts"] == {"256789012": 2}
    assert result.stop_reason == "thread_comment_limit"
    assert result.complete is False
    assert result.checkpoint["comment_limit_reached"] is True


@pytest.mark.asyncio
async def test_resume_enforces_existing_thread_counts(tmp_path: Path) -> None:
    checkpoint = SourceCheckpoint(known_keys={"256789012", "old-comment"}, post_count=1, comment_count=1,
        provider={"thread_comment_counts": {"256789012": 1}, "thread_ids": ["256789012"]})
    result, captured, requests = await collect(tmp_path, config(urls=[URL], max_posts=1,
        max_comments_per_thread=1, import_records=[import_row(source_id="new-comment")]), {}, checkpoint)
    assert requests == []
    assert captured.items == []
    assert result.comment_count == 1


@pytest.mark.asyncio
async def test_resume_keyword_discovered_thread_at_post_cap(tmp_path: Path) -> None:
    checkpoint = SourceCheckpoint(known_keys={"256789012", "c1"}, post_count=1, comment_count=1,
        provider={"thread_comment_counts": {"256789012": 1}, "thread_ids": ["256789012"],
                  "thread_urls": [URL]})
    result, captured, requests = await collect(tmp_path, config(keywords=["brand"], max_posts=1,
        max_comments=2), {URL: html(article(comments=[comment("c1"), comment("c2")], commentCount=2))}, checkpoint)
    assert requests == [URL]
    assert [item.source_item_id for item in captured.items] == ["c2"]
    assert result.comment_count == 2
    assert result.stop_reason == "target_reached"
    assert result.complete is True


@pytest.mark.asyncio
async def test_both_targets_satisfied_reports_target_with_coverage_caveat(tmp_path: Path) -> None:
    result, _, _ = await collect(tmp_path, config(urls=[URL], max_posts=1, max_comments=1), {
        URL: html(article(comments=[comment("c1")], commentCount=100)),
    })
    assert result.stop_reason == "target_reached"
    assert result.complete is True
    assert "public_page_partial" in result.checkpoint["partial_reasons"]


@pytest.mark.asyncio
async def test_global_comment_truncation_is_partial_until_both_targets_reached(tmp_path: Path) -> None:
    result, captured, _ = await collect(tmp_path, config(urls=[URL], max_posts=10, max_comments=1), {
        URL: html(article(comments=[comment("c1"), comment("c2")], commentCount=2)),
    })
    assert len(captured.items) == 2
    assert result.stop_reason == "comment_limit"
    assert result.complete is False
    assert "comment_limit" in result.checkpoint["partial_reasons"]


@pytest.mark.asyncio
async def test_comment_collection_disabled_does_not_report_truncation(tmp_path: Path) -> None:
    result, _, _ = await collect(tmp_path, config(urls=[URL], max_comments=0), {
        URL: html(article(comments=[comment("c1")], commentCount=1)),
    })
    assert result.complete is True
    assert result.checkpoint["comment_limit_reached"] is False


@pytest.mark.asyncio
async def test_import_only_resume_does_not_fetch_stored_thread_urls(tmp_path: Path) -> None:
    checkpoint = SourceCheckpoint(known_keys={"256789012"}, post_count=1,
        provider={"thread_urls": [URL], "thread_ids": ["256789012"]})
    result, _, requests = await collect(tmp_path, config(import_records=[import_row()]), {}, checkpoint)
    assert requests == []
    assert result.comment_count == 1


@pytest.mark.asyncio
async def test_dates_use_taiwan_midnight_and_naive_time(tmp_path: Path) -> None:
    result, captured, _ = await collect(tmp_path, config(date_from="2026-01-15", date_to="2026-01-15",
        import_records=[import_row("post", "", published_at="2026-01-14T16:10:00Z"),
                        import_row(published_at="2026-01-15T00:30:00")]), {})
    assert (result.post_count, result.comment_count) == (1, 1)
    assert captured.items[1].published_at.isoformat() == "2026-01-14T16:30:00+00:00"


@pytest.mark.asyncio
async def test_unknown_post_date_is_partial_and_excluded(tmp_path: Path) -> None:
    result, captured, _ = await collect(tmp_path, config(urls=[URL]), {URL: html(article(datePublished=None))})
    assert captured.items == []
    assert result.stop_reason == "unknown_date"
    assert result.complete is False


@pytest.mark.asyncio
async def test_public_block_stops_network_but_preserves_imports(tmp_path: Path) -> None:
    result, captured, requests = await collect(tmp_path, config(urls=[URL, f"{BASE}/f/food/p/2"],
        keywords=["brand"], import_records=[import_row()]), {URL: 403})
    assert requests == [URL]
    assert len(captured.items) == 1
    assert result.stop_reason == "public_source_blocked"
    assert result.complete is False
    assert "HTTP 403" in result.checkpoint["blocked_reason"]


@pytest.mark.asyncio
async def test_missing_article_does_not_abort_remaining_inputs(tmp_path: Path) -> None:
    second = f"{BASE}/f/food/p/2"
    result, captured, requests = await collect(tmp_path, config(urls=[URL, second]), {
        URL: 404, second: html(article("2")),
    })
    assert requests == [URL, second]
    assert [item.source_item_id for item in captured.items] == ["2"]
    assert result.complete is False
    assert result.stop_reason == "source_unavailable"


@pytest.mark.asyncio
async def test_import_only_collection_is_cancelable(tmp_path: Path) -> None:
    captured = Capture(cancel_after=2)
    with pytest.raises(SourceCanceledError):
        await collect(tmp_path, config(import_records=[import_row(), import_row(source_id="c2")]), {}, capture=captured)
    assert captured.items == []
