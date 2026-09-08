from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .schemas import BusinessCandidate, ContentType, SourceKind
from .scraper import (
    BatchCallback,
    CancelCallback,
    CrawlResult,
    MapsBlockedError,
    MapsCanceledError,
    MetricsCallback,
    ProgressCallback,
    ScrapedReview,
    VerificationCallback,
)


class SourceError(RuntimeError):
    pass


class SourceBlockedError(SourceError):
    pass


class SourceNotFoundError(SourceError):
    """A removed public resource (HTTP 404/410), not a source-wide block."""


class SourceUnavailableError(SourceError):
    """A transient transport/server failure after bounded retries."""


class SourceCanceledError(SourceError):
    pass


@dataclass(slots=True)
class CollectedItem:
    source: SourceKind
    content_type: ContentType
    source_item_id: str
    content_hash: str
    text: str
    source_url: str
    title: str | None = None
    board: str | None = None
    thread_source_id: str | None = None
    parent_source_id: str | None = None
    author_name: str | None = None
    author_hash: str | None = None
    rating: int | None = None
    relative_date: str | None = None
    published_at: datetime | None = None
    date_precision: str = "unknown"
    owner_reply: str | None = None
    platform_data: dict = field(default_factory=dict)
    legacy_source_item_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SourceCollectionResult:
    source: SourceKind
    collected_count: int
    post_count: int
    comment_count: int
    complete: bool
    stop_reason: str
    checkpoint: dict = field(default_factory=dict)


ItemBatchCallback = Callable[[list[CollectedItem]], Awaitable[None]]
SourceProgressCallback = Callable[[int, int, int, str], Awaitable[None]]
SourceCancelCallback = Callable[[], Awaitable[bool]]
SourceVerificationCallback = Callable[[str], Awaitable[None]]
SourceMetricsCallback = Callable[[dict], Awaitable[None]]
SourceMetadataCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class SourceCheckpoint:
    known_keys: set[str] = field(default_factory=set)
    collected_count: int = 0
    post_count: int = 0
    comment_count: int = 0
    provider: dict = field(default_factory=dict)


@dataclass(slots=True)
class SourceCallbacks:
    on_batch: ItemBatchCallback
    on_progress: SourceProgressCallback
    is_canceled: SourceCancelCallback
    on_verification: SourceVerificationCallback
    on_metrics: SourceMetricsCallback
    on_metadata: SourceMetadataCallback


class SourceProvider(Protocol):
    source: SourceKind

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult: ...


class ReviewSource(Protocol):
    """Legacy Google Maps browser boundary retained for compatibility and test fakes."""

    async def search(
        self, query: str, headless: bool | None = None
    ) -> list[BusinessCandidate]: ...

    async def crawl(
        self,
        *,
        job_id: str,
        maps_url: str,
        target: int,
        sort_order: str,
        headless: bool,
        on_batch: BatchCallback,
        on_progress: ProgressCallback,
        on_verification: VerificationCallback,
        is_canceled: CancelCallback,
        known_keys: set[str] | None = None,
        already_collected: int = 0,
        on_metrics: MetricsCallback | None = None,
    ) -> CrawlResult: ...


class GoogleMapsSource:
    """Adapts the existing browser scraper to the generic source-provider contract."""

    source: SourceKind = "google_maps"

    def __init__(
        self,
        scraper_factory: Callable[[], ReviewSource],
        browser_lock: asyncio.Lock,
    ) -> None:
        self.scraper_factory = scraper_factory
        self.browser_lock = browser_lock

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        async def on_batch(items: list[ScrapedReview]) -> None:
            converted = [
                CollectedItem(
                    source="google_maps",
                    content_type="review",
                    source_item_id=item.source_review_id or item.content_hash,
                    content_hash=item.content_hash,
                    author_name=item.author_name,
                    rating=item.rating,
                    text=item.text,
                    relative_date=item.relative_date,
                    owner_reply=item.owner_reply,
                    source_url=item.source_url,
                )
                for item in items
            ]
            await callbacks.on_batch(converted)

        async def on_progress(current: int, _total: int | None, message: str) -> None:
            await callbacks.on_progress(current, 0, int(config["max_reviews"]), message)

        try:
            async with self.browser_lock:
                result = await self.scraper_factory().crawl(
                    job_id=str(config["_job_id"]),
                    maps_url=str(config["maps_url"]),
                    target=int(config["max_reviews"]),
                    sort_order=str(config["sort"]),
                    headless=bool(config["headless"]),
                    on_batch=on_batch,
                    on_progress=on_progress,
                    on_verification=callbacks.on_verification,
                    is_canceled=callbacks.is_canceled,
                    known_keys=checkpoint.known_keys,
                    already_collected=checkpoint.collected_count,
                    on_metrics=callbacks.on_metrics,
                )
        except MapsBlockedError as exc:
            raise SourceBlockedError(str(exc)) from exc
        except MapsCanceledError as exc:
            raise SourceCanceledError(str(exc)) from exc
        await callbacks.on_metadata(
            {
                "name": result.name,
                "address": result.address,
                "average_rating": result.average_rating,
                "total_review_count": result.total_review_count,
            }
        )
        return SourceCollectionResult(
            source=self.source,
            collected_count=result.reviews_seen,
            post_count=0,
            comment_count=0,
            complete=result.stop_reason in {"target_reached", "total_reached"},
            stop_reason=result.stop_reason,
            checkpoint={"reviews_seen": result.reviews_seen},
        )
