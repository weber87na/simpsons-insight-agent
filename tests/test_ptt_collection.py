from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
import pytest

from simpsons_insight_agent.author_privacy import AuthorHasher
from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.forum_sources import (
    PttSource,
    _ptt_push_datetime,
    parse_ptt_article,
    parse_ptt_search,
)
from simpsons_insight_agent.sources import (
    CollectedItem,
    SourceCallbacks,
    SourceCanceledError,
    SourceCheckpoint,
    SourceCollectionResult,
)

PTT = "https://www.ptt.cc"
ARTICLE = "/bbs/Food/M.1767225000.A.001.html"
SECOND = "/bbs/Food/M.1767224000.A.002.html"
THIRD = "/bbs/Food/M.1767223000.A.003.html"


def article(*pushes: str, date: str = "Thu Jan 01 00:10:00 2026") -> str:
    fields = {"作者": "alice (匿名)", "標題": "測試文章", "時間": date}
    metadata = "".join(
        f'<div class="article-metaline"><span class="article-meta-tag">{label}</span>'
        f'<span class="article-meta-value">{value}</span></div>'
        for label, value in fields.items()
    )
    return f'<div id="main-content">{metadata}正文<br>第二行{"".join(pushes)}</div>'


def push(text: str = "留言", author: str = "bob", time: str = "01/01 00:20") -> str:
    return (
        '<div class="push"><span class="push-tag">推</span>'
        f'<span class="push-userid">{author}</span>'
        f'<span class="push-content">: {text}</span>'
        f'<span class="push-ipdatetime">{time}</span></div>'
    )


def search(*paths: str, previous: str | None = None) -> str:
    links = "".join(f'<a href="{path}">文章</a>' for path in paths)
    if previous:
        links += f'<a href="{escape(previous, quote=True)}">‹ 上頁</a>'
    return f'<div class="r-list-container">{links}</div>'


def settings(tmp_path: Path) -> Settings:
    return Settings(author_hash_key_path=tmp_path / "author.key")


def config(**overrides: object) -> dict:
    return {
        "boards": ["Food"], "keywords": ["測試"],
        "date_from": "2026-01-01", "date_to": "2026-01-01",
        "max_posts": 10, "max_comments": 20, "max_comments_per_thread": 10,
        "max_search_pages": 20, **overrides,
    }


class Collector:
    def __init__(self) -> None:
        self.items: list[CollectedItem] = []
        self.metrics: list[dict] = []
        self.canceled = False

    async def batch(self, values: list[CollectedItem]) -> None:
        self.items.extend(values)

    async def cancel(self) -> bool:
        return self.canceled

    async def metric(self, values: dict) -> None:
        self.metrics.append(values)

    async def ignore(self, *_args: object) -> None:
        pass

    def callbacks(self) -> SourceCallbacks:
        return SourceCallbacks(
            self.batch, self.ignore, self.cancel, self.ignore, self.metric, self.ignore
        )


async def collect(
    tmp_path: Path, pages: dict[str, str | int], options: dict | None = None,
    checkpoint: SourceCheckpoint | None = None, collector: Collector | None = None,
) -> tuple[SourceCollectionResult, Collector, list[str]]:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        requests.append(path)
        response = pages.get(path, pages.get(request.url.path))
        assert response is not None, f"Unexpected request: {path}"
        return httpx.Response(
            response if isinstance(response, int) else 200,
            text=response if isinstance(response, str) else "", request=request,
        )

    receiver = collector or Collector()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PttSource(settings(tmp_path), client=client)
        source.fetcher.interval = 0
        result = await source.collect(
            config=options or config(), checkpoint=checkpoint or SourceCheckpoint(),
            callbacks=receiver.callbacks(),
        )
    return result, receiver, requests


def test_ptt_local_time_and_missing_board_do_not_shift_metadata(tmp_path: Path) -> None:
    items = parse_ptt_article(article(push()), PTT + ARTICLE, AuthorHasher(settings(tmp_path)))
    assert items[0].title == "測試文章"
    assert items[0].board == "Food"
    assert items[0].published_at == datetime(2025, 12, 31, 16, 10, tzinfo=UTC)
    assert items[1].published_at == datetime(2025, 12, 31, 16, 20, tzinfo=UTC)
    assert items[0].text == "正文 第二行"


def test_ptt_missing_metadata_uses_url_timestamp_and_board(tmp_path: Path) -> None:
    items = parse_ptt_article(
        '<div id="main-content">只有內文<br><script>secret()</script>繼續</div>',
        PTT + ARTICLE, AuthorHasher(settings(tmp_path)),
    )
    assert items[0].published_at == datetime.fromtimestamp(1767225000, UTC)
    assert items[0].board == "Food"
    assert items[0].title is None
    assert items[0].text == "只有內文 繼續"


def test_ptt_bad_article_date_falls_back_without_crashing(tmp_path: Path) -> None:
    result = parse_ptt_article(
        article(date="garbage"), PTT + ARTICLE, AuthorHasher(settings(tmp_path))
    )
    assert result[0].published_at == datetime.fromtimestamp(1767225000, UTC)


@pytest.mark.parametrize("value", ["13/01 10:10", "02/30 10:10", "01/01 99:99", "1.2.3.4"])
def test_ptt_invalid_push_dates_are_unknown(value: str) -> None:
    assert _ptt_push_datetime(value, datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_ptt_push_year_uses_local_new_year_and_accepts_leap_day() -> None:
    taiwan = timezone(timedelta(hours=8))
    assert _ptt_push_datetime(
        "01/01 00:05", datetime(2025, 12, 31, 23, 55, tzinfo=taiwan)
    ) == datetime(2025, 12, 31, 16, 5, tzinfo=UTC)
    assert _ptt_push_datetime(
        "02/29 12:00", datetime(2024, 2, 28, 12, tzinfo=taiwan)
    ) == datetime(2024, 2, 29, 4, tzinfo=UTC)


def test_push_ids_survive_other_push_removal_and_ip_changes(tmp_path: Path) -> None:
    hasher = AuthorHasher(settings(tmp_path))
    original = parse_ptt_article(
        article(push("早先留言", "carol"), push(time="1.2.3.4 01/01 00:20")),
        PTT + ARTICLE, hasher,
    )
    changed = parse_ptt_article(
        article(push(time="2001:db8::1 1/01 00:20")), PTT + ARTICLE, hasher
    )
    assert original[2].source_item_id == changed[1].source_item_id
    assert original[2].content_hash == changed[1].content_hash
    persisted = str([asdict(item) for item in [*original, *changed]])
    assert "1.2.3.4" not in persisted
    assert "2001:db8::1" not in persisted
    assert "bob" not in persisted
    assert changed[1].relative_date == "01/01 00:20"


def test_identical_push_occurrences_remain_distinct(tmp_path: Path) -> None:
    items = parse_ptt_article(
        article(push(), push()), PTT + ARTICLE, AuthorHasher(settings(tmp_path))
    )
    assert len(items) == 3
    assert items[1].source_item_id != items[2].source_item_id


def test_search_ignores_cross_board_and_unsafe_previous_links() -> None:
    html = search(ARTICLE, ARTICLE, "/bbs/Other/M.1.A.001.html")
    html += '<a href="https://evil.example/bbs/Food/search">上頁</a>'
    html += '<a href="/bbs/Food/M.1.A.001.html">上頁</a>'
    links, previous = parse_ptt_search(html, "Food")
    assert links == [ARTICLE, "/bbs/Food/M.1.A.001.html"]
    assert previous is None


@pytest.mark.asyncio
async def test_date_filter_uses_taiwan_calendar_and_deduplicates_across_queries(tmp_path: Path) -> None:
    result, receiver, calls = await collect(
        tmp_path, {"/bbs/Food/search": search(ARTICLE), ARTICLE: article(push())},
        config(keywords=["a", "b", "a"]),
    )
    assert result.post_count == 1
    assert result.comment_count == 1
    assert result.complete is True
    assert result.stop_reason == "search_exhausted"
    assert calls.count(ARTICLE) == 1
    assert len(receiver.items) == 2


@pytest.mark.asyncio
async def test_post_budget_stops_fetching_unrelated_articles(tmp_path: Path) -> None:
    result, receiver, calls = await collect(
        tmp_path, {"/bbs/Food/search": search(ARTICLE, SECOND, THIRD), ARTICLE: article(push())},
        config(max_posts=1, keywords=["a", "b"]),
    )
    assert result.stop_reason == "post_limit"
    assert result.complete is False
    assert len(calls) == 2
    assert len(receiver.items) == 2


@pytest.mark.asyncio
async def test_both_targets_reached_reports_complete(tmp_path: Path) -> None:
    result, _, _ = await collect(
        tmp_path, {"/bbs/Food/search": search(ARTICLE), ARTICLE: article(push())},
        config(max_posts=1, max_comments=1),
    )
    assert result.complete is True
    assert result.stop_reason == "target_reached"


@pytest.mark.asyncio
async def test_thread_comment_cap_is_partial_and_resume_does_not_creep(tmp_path: Path) -> None:
    pages = {"/bbs/Food/search": search(ARTICLE), ARTICLE: article(push("一"), push("二"))}
    options = config(max_comments_per_thread=1)
    result, receiver, _ = await collect(tmp_path, pages, options)
    assert result.complete is False
    assert result.stop_reason == "thread_comment_limit"
    assert result.comment_count == 1
    state = SourceCheckpoint(
        known_keys={item.source_item_id for item in receiver.items},
        post_count=1, comment_count=1, collected_count=2, provider=result.checkpoint,
    )
    resumed, after, calls = await collect(tmp_path, pages, options, state)
    assert resumed.comment_count == 1
    assert resumed.complete is False
    assert not after.items
    assert ARTICLE not in calls


@pytest.mark.asyncio
async def test_deleted_article_does_not_prevent_later_article_collection(tmp_path: Path) -> None:
    result, _, calls = await collect(tmp_path, {
        "/bbs/Food/search": search(ARTICLE, SECOND, THIRD),
        ARTICLE: 404, SECOND: 410, THIRD: article(push()),
    })
    assert result.post_count == 1
    assert result.complete is False
    assert result.stop_reason == "article_unavailable"
    assert result.checkpoint["missing_articles"] == 2
    assert THIRD in calls


@pytest.mark.asyncio
async def test_pagination_cycle_and_explicit_page_cap(tmp_path: Path) -> None:
    initial = "/bbs/Food/search?q=test"
    second = "/bbs/Food/search?page=2&q=test"
    pages = {initial: search(ARTICLE, previous=second), second: search(SECOND, previous=initial),
             ARTICLE: article(), SECOND: article()}
    result, _, calls = await collect(tmp_path, pages, config(keywords=["test"]))
    assert result.stop_reason == "pagination_cycle"
    assert result.complete is False
    assert len(calls) == 4
    limited, _, calls = await collect(tmp_path, pages, config(keywords=["test"], max_search_pages=1))
    assert limited.stop_reason == "page_limit"
    assert limited.complete is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancel_after_first_batch_prevents_next_article_fetch(tmp_path: Path) -> None:
    class CancelAfterBatch(Collector):
        async def batch(self, values: list[CollectedItem]) -> None:
            await super().batch(values)
            self.canceled = True

    receiver = CancelAfterBatch()
    with pytest.raises(SourceCanceledError):
        await collect(
            tmp_path, {"/bbs/Food/search": search(ARTICLE, SECOND), ARTICLE: article(push())},
            collector=receiver,
        )
    assert len(receiver.items) == 2
    assert receiver.metrics[-1]["checkpoint"]["thread_urls"] == [PTT + ARTICLE]


@pytest.mark.asyncio
async def test_resume_existing_post_at_post_limit_collects_unfinished_comments(tmp_path: Path) -> None:
    hasher = AuthorHasher(settings(tmp_path))
    original = parse_ptt_article(article(push()), PTT + ARTICLE, hasher)
    state = SourceCheckpoint(
        known_keys={original[0].source_item_id}, post_count=1, collected_count=1,
        provider={"thread_urls": [PTT + ARTICLE], "thread_comment_counts": {original[0].source_item_id: 0}},
    )
    result, receiver, calls = await collect(
        tmp_path, {ARTICLE: article(push())}, config(max_posts=1, max_comments=1), state,
    )
    assert result.stop_reason == "target_reached"
    assert [item.content_type for item in receiver.items] == ["comment"]
    assert calls == [ARTICLE]


@pytest.mark.asyncio
async def test_empty_valid_search_is_complete_but_unexpected_html_is_partial(tmp_path: Path) -> None:
    empty, _, _ = await collect(tmp_path, {"/bbs/Food/search": search()})
    assert empty.complete is True
    partial, _, _ = await collect(tmp_path, {"/bbs/Food/search": "<html>maintenance</html>"})
    assert partial.complete is False
    assert partial.stop_reason == "article_parse_partial"


@pytest.mark.asyncio
async def test_resume_durable_thread_count_prevents_cap_creep_after_push_deletion(tmp_path: Path) -> None:
    original = parse_ptt_article(
        article(push("已刪除的留言")), PTT + ARTICLE, AuthorHasher(settings(tmp_path))
    )
    state = SourceCheckpoint(
        known_keys={item.source_item_id for item in original}, post_count=1,
        comment_count=1, collected_count=2,
        provider={"thread_urls": [PTT + ARTICLE], "thread_comment_counts": {original[0].source_item_id: 1}},
    )
    result, receiver, calls = await collect(
        tmp_path, {ARTICLE: article(push("新留言"))},
        config(max_posts=1, max_comments_per_thread=1), state,
    )
    assert result.comment_count == 1
    assert result.stop_reason == "thread_comment_limit"
    assert not receiver.items
    assert calls == [ARTICLE]


@pytest.mark.asyncio
async def test_resume_at_post_cap_without_thread_urls_does_not_scan_every_query(tmp_path: Path) -> None:
    result, _, calls = await collect(
        tmp_path, {}, config(max_posts=1, keywords=["a", "b", "c"]),
        SourceCheckpoint(post_count=1, collected_count=1),
    )
    assert not calls
    assert result.stop_reason == "post_limit"
    assert result.complete is False


@pytest.mark.asyncio
async def test_global_comment_truncation_is_partial_until_both_targets_are_reached(tmp_path: Path) -> None:
    pages = {"/bbs/Food/search": search(ARTICLE), ARTICLE: article(push("一"), push("二"))}
    result, receiver, _ = await collect(tmp_path, pages, config(max_comments=1))
    assert result.post_count == 1
    assert result.comment_count == 1
    assert len(receiver.items) == 2
    assert result.complete is False
    assert result.stop_reason == "comment_limit"
    assert "comment_limit" in result.checkpoint["partial_reasons"]
    assert PTT + ARTICLE not in result.checkpoint["processed_urls"]

    # Raising the comment budget must revisit the unfinished article and collect
    # its remaining comment exactly once, without re-inserting its existing post.
    state = SourceCheckpoint(
        known_keys={item.source_item_id for item in receiver.items},
        post_count=1, comment_count=1, collected_count=2, provider=result.checkpoint,
    )
    resumed, after, _ = await collect(tmp_path, pages, config(max_comments=2), state)
    assert resumed.comment_count == 2
    assert resumed.complete is True
    assert [item.text for item in after.items] == ["二"]


@pytest.mark.asyncio
async def test_resume_legacy_push_identity_does_not_insert_existing_comment_again(tmp_path: Path) -> None:
    # Golden identity generated by the original positional-ID implementation.
    legacy_comment_id = "3534b4e032ab5e630910b605f62506f1cbc6bb32e938cbd162fceecda522bff3"
    parsed = parse_ptt_article(
        article(push()), PTT + ARTICLE, AuthorHasher(settings(tmp_path))
    )
    assert parsed[1].source_item_id != legacy_comment_id
    assert parsed[1].legacy_source_item_ids == [legacy_comment_id]
    state = SourceCheckpoint(
        known_keys={parsed[0].source_item_id, legacy_comment_id},
        post_count=1, comment_count=1, collected_count=2,
        provider={"thread_urls": [PTT + ARTICLE]},
    )
    result, receiver, calls = await collect(
        tmp_path, {ARTICLE: article(push(), push("新的留言"))},
        config(max_posts=1, max_comments=2), state,
    )
    assert result.post_count == 1
    assert result.comment_count == 2
    assert result.complete is True
    assert [item.text for item in receiver.items] == ["新的留言"]
    assert calls == [ARTICLE]
