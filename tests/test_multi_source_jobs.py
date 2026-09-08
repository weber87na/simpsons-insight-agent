from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.db import SessionLocal
from simpsons_insight_agent.jobs import JobManager
from simpsons_insight_agent.models import JobReview, JobSource, Review
from simpsons_insight_agent.schemas import CreateJobRequest
from simpsons_insight_agent.sources import (
    CollectedItem,
    SourceBlockedError,
    SourceCallbacks,
    SourceCheckpoint,
    SourceCollectionResult,
)


def item(source: str, content_type: str, source_id: str, text: str) -> CollectedItem:
    return CollectedItem(
        source=source,
        content_type=content_type,
        source_item_id=source_id,
        thread_source_id=source_id if content_type == "post" else "thread-1",
        parent_source_id="thread-1" if content_type == "comment" else None,
        content_hash=(f"{source}-{source_id}" * 64)[:64],
        text=text,
        source_url=(
            "https://www.google.com/maps/place/test"
            if source == "google_maps"
            else (
                "https://www.ptt.cc/bbs/Food/M.1.html"
                if source == "ptt"
                else "https://www.dcard.tw/f/food/p/256789012"
            )
        ),
        title="測試討論",
        board="Food" if source == "ptt" else "food",
        author_hash="a" * 64 if source != "google_maps" else None,
        rating=5 if source == "google_maps" else None,
    )


class FakeProvider:
    def __init__(
        self,
        source: str,
        items: list[CollectedItem],
        order: list[str],
        *,
        blocked: bool = False,
    ) -> None:
        self.source = source
        self.items = items
        self.order = order
        self.blocked = blocked

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        self.order.append(self.source)
        if self.blocked:
            raise SourceBlockedError("fixture blocked")
        fresh = [
            value
            for value in self.items
            if value.source_item_id not in checkpoint.known_keys
        ]
        await callbacks.on_batch(fresh)
        posts = checkpoint.post_count + sum(value.content_type == "post" for value in fresh)
        comments = checkpoint.comment_count + sum(
            value.content_type == "comment" for value in fresh
        )
        await callbacks.on_progress(posts, comments, max(len(self.items), 1), "fixture")
        return SourceCollectionResult(
            source=self.source,
            collected_count=checkpoint.collected_count + len(fresh),
            post_count=posts,
            comment_count=comments,
            complete=True,
            stop_reason="fixture_complete",
            checkpoint={"fixture": True},
        )


def three_source_request() -> CreateJobRequest:
    return CreateJobRequest.model_validate(
        {
            "subject": {
                "kind": "brand",
                "name": f"跨平台-{uuid.uuid4()}",
                "aliases": ["範例牌"],
            },
            "sources": [
                {
                    "source": "google_maps",
                    "maps_url": f"https://www.google.com/maps/place/{uuid.uuid4()}",
                    "max_reviews": 1,
                },
                {
                    "source": "ptt",
                    "boards": ["Food"],
                    "keywords": ["範例牌"],
                    "date_from": "2025-01-01",
                    "date_to": "2026-12-31",
                    "max_posts": 1,
                    "max_comments": 0,
                },
                {
                    "source": "dcard",
                    "urls": ["https://www.dcard.tw/f/food/p/256789012"],
                    "date_from": "2025-01-01",
                    "date_to": "2026-12-31",
                    "max_posts": 1,
                    "max_comments": 0,
                    "acknowledge_terms": True,
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_sources_run_in_order_and_one_failure_does_not_block_others() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    order: list[str] = []
    manager.providers = {
        "google_maps": FakeProvider(
            "google_maps", [item("google_maps", "review", "same", "Google 內容")], order
        ),
        "ptt": FakeProvider("ptt", [], order, blocked=True),
        "dcard": FakeProvider(
            "dcard", [item("dcard", "post", "same", "Dcard 內容")], order
        ),
    }
    job = await manager.create_job(three_source_request())

    await manager._process_collection(job.id)
    stored = await manager.get_job(job.id)
    assert order == ["google_maps", "ptt", "dcard"]
    assert stored.status == "READY_FOR_ANALYSIS"
    assert stored.collected_count == 2
    assert stored.collection_complete is False

    async with SessionLocal() as session:
        runs = list(
            (
                await session.scalars(
                    select(JobSource)
                    .where(JobSource.job_id == job.id)
                    .order_by(JobSource.ordinal)
                )
            ).all()
        )
        reviews = list(
            (
                await session.scalars(
                    select(Review)
                    .join(JobReview, JobReview.review_id == Review.id)
                    .where(JobReview.job_id == job.id)
                )
            ).all()
        )
    assert [run.status for run in runs] == ["COMPLETE", "BLOCKED", "COMPLETE"]
    assert {(review.source, review.source_item_id) for review in reviews} == {
        ("google_maps", "same"),
        ("dcard", "same"),
    }
    forum = next(review for review in reviews if review.source == "dcard")
    assert forum.author_name is None
    assert forum.author_hash == "a" * 64


class ResumeProvider:
    source = "ptt"

    def __init__(self) -> None:
        self.calls = 0
        self.checkpoints: list[set[str]] = []

    async def collect(
        self,
        *,
        config: dict,
        checkpoint: SourceCheckpoint,
        callbacks: SourceCallbacks,
    ) -> SourceCollectionResult:
        self.calls += 1
        self.checkpoints.append(set(checkpoint.known_keys))
        post = item("ptt", "post", "post-1", "第一篇")
        comment = item("ptt", "comment", "comment-1", "推")
        await callbacks.on_batch([post] if self.calls == 1 else [post, comment])
        await callbacks.on_progress(1, 0 if self.calls == 1 else 1, 2, "resume fixture")
        return SourceCollectionResult(
            source="ptt",
            collected_count=1 if self.calls == 1 else 2,
            post_count=1,
            comment_count=0 if self.calls == 1 else 1,
            complete=self.calls > 1,
            stop_reason="temporary" if self.calls == 1 else "search_exhausted",
            checkpoint={"call": self.calls},
        )


@pytest.mark.asyncio
async def test_resume_only_retries_incomplete_source_and_deduplicates_checkpoint() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    provider = ResumeProvider()
    manager.providers["ptt"] = provider
    request = CreateJobRequest.model_validate(
        {
            "subject": {"kind": "brand", "name": f"續抓-{uuid.uuid4()}"},
            "sources": [
                {
                    "source": "ptt",
                    "boards": ["Food"],
                    "keywords": ["續抓"],
                    "date_from": "2025-01-01",
                    "date_to": "2026-12-31",
                    "max_posts": 1,
                    "max_comments": 1,
                }
            ],
        }
    )
    job = await manager.create_job(request)
    await manager._process_collection(job.id)
    first = await manager.get_job(job.id)
    assert first.collected_count == 1
    assert first.collection_complete is False

    await manager.resume(job.id)
    await manager._process_collection(job.id)
    second = await manager.get_job(job.id)
    assert second.collected_count == 2
    assert second.collection_complete is True
    assert provider.calls == 2
    assert "post-1" in provider.checkpoints[1]

    async with SessionLocal() as session:
        reviews = list(
            (
                await session.scalars(
                    select(Review)
                    .join(JobReview, JobReview.review_id == Review.id)
                    .where(JobReview.job_id == job.id)
                )
            ).all()
        )
    assert {review.source_item_id for review in reviews} == {"post-1", "comment-1"}


@pytest.mark.parametrize(
    ("reason", "diagnostic", "empty_status"),
    [
        ("public_source_blocked", "blocked_reason", "BLOCKED"),
        ("public_source_unavailable", "unavailable_reason", "FAILED"),
    ],
)
@pytest.mark.parametrize("with_import", [False, True])
async def test_dcard_public_failure_preserves_import_and_reports_status(
    reason: str, diagnostic: str, empty_status: str, with_import: bool
) -> None:
    manager = JobManager(Settings(openai_api_key=None))

    class PartialDcard:
        source = "dcard"

        async def collect(self, *, config, checkpoint, callbacks):
            if with_import:
                await callbacks.on_batch([item("dcard", "post", f"import-{uuid.uuid4()}", "匯入保留")])
            return SourceCollectionResult(
                source="dcard", collected_count=int(with_import), post_count=int(with_import),
                comment_count=0, complete=False, stop_reason=reason,
                checkpoint={diagnostic: "fixture diagnosis"},
            )

    manager.providers["dcard"] = PartialDcard()
    request = three_source_request()
    request.sources = [source for source in request.sources if source.source == "dcard"]
    job = await manager.create_job(request)
    await manager._process_collection(job.id)
    stored = await manager.get_job(job.id)
    async with SessionLocal() as session:
        run = await session.scalar(select(JobSource).where(JobSource.job_id == job.id))
        assert run.status == ("PARTIAL" if with_import else empty_status)
        assert run.error == "fixture diagnosis"
        assert run.stop_reason == reason
    assert stored.collected_count == int(with_import)
    assert stored.collection_complete is False
    assert stored.status == ("READY_FOR_ANALYSIS" if with_import else empty_status)


async def test_checkpoint_is_durable_before_provider_interrupts() -> None:
    manager = JobManager(Settings(openai_api_key=None))

    class InterruptedPtt:
        source = "ptt"

        async def collect(self, *, config, checkpoint, callbacks):
            post = item("ptt", "post", f"post-{uuid.uuid4()}", "已儲存")
            comment = item("ptt", "comment", f"comment-{uuid.uuid4()}", "已儲存的推文")
            comment.thread_source_id = post.source_item_id
            await callbacks.on_batch([post, comment])
            await callbacks.on_metrics({"checkpoint": {"cursor": "page-2"}, "pages_fetched": 1})
            raise SourceBlockedError("next page blocked")

    manager.providers["ptt"] = InterruptedPtt()
    request = three_source_request()
    request.sources = [source for source in request.sources if source.source == "ptt"]
    job = await manager.create_job(request)
    await manager._process_collection(job.id)
    async with SessionLocal() as session:
        run = await session.scalar(select(JobSource).where(JobSource.job_id == job.id))
        assert run.checkpoint == {"cursor": "page-2"}
        assert run.collected_count == 2
    recovered = await manager._forum_checkpoint(job.id, "ptt")
    assert len(recovered["thread_ids"]) == 1
    assert recovered["thread_comment_counts"][recovered["thread_ids"][0]] == 1
    assert recovered["thread_urls"] == ["https://www.ptt.cc/bbs/Food/M.1.html"]


async def test_old_source_identity_alias_reuses_persisted_review() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    request = three_source_request()
    request.sources = [source for source in request.sources if source.source == "ptt"]
    first_job = await manager.create_job(request)
    old_id, new_id = f"old-{uuid.uuid4()}", f"new-{uuid.uuid4()}"
    old = item("ptt", "comment", old_id, "既有推文")
    await manager._persist_items(first_job.id, first_job.business_id, [old])
    new = item("ptt", "comment", new_id, "既有推文")
    new.legacy_source_item_ids = [old_id]
    await manager._persist_items(first_job.id, first_job.business_id, [new])
    assert await manager._job_collected_count(first_job.id) == 1
    second_job = await manager.create_job(request)
    await manager._persist_items(second_job.id, second_job.business_id, [new])
    assert await manager._job_collected_count(second_job.id) == 1
    async with SessionLocal() as session:
        reviews = list((await session.scalars(select(Review).where(
            Review.business_id == first_job.business_id
        ))).all())
        assert len(reviews) == 1
        assert reviews[0].source_item_id == old_id
