from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select

from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.db import SessionLocal, mark_inflight_jobs_interrupted
from simpsons_insight_agent.jobs import JobManager
from simpsons_insight_agent.models import (
    Business,
    CrawlJob,
    JobReview,
    Report,
    Review,
    ReviewAnalysis,
)
from simpsons_insight_agent.scraper import CrawlResult, ScrapedReview
from simpsons_insight_agent.sentiment import SentimentResult


def scraped(review_id: str, text: str) -> ScrapedReview:
    return ScrapedReview(
        source_review_id=review_id,
        content_hash=(review_id * 64)[:64],
        author_name="測試者",
        rating=5,
        text=text,
        relative_date="1 天前",
        owner_reply=None,
        source_url="https://google.com/maps/place/test",
    )


async def make_job(status: str = "PENDING_COLLECTION") -> tuple[str, str]:
    async with SessionLocal() as session:
        business = Business(
            name="測試商家",
            maps_url=f"https://google.com/maps/place/{status}-{id(session)}",
        )
        session.add(business)
        await session.flush()
        job = CrawlJob(
            business_id=business.id,
            status=status,
            max_reviews=2,
            llm_model="gpt-5.4-mini-2026-03-17",
        )
        session.add(job)
        await session.commit()
        return job.id, business.id


@pytest.mark.asyncio
async def test_job_review_membership_and_counts_are_scoped_per_job() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    first_job, business_id = await make_job()
    async with SessionLocal() as session:
        second = CrawlJob(
            business_id=business_id,
            status="PENDING_COLLECTION",
            max_reviews=2,
            llm_model="gpt-5.4-mini-2026-03-17",
        )
        session.add(second)
        await session.commit()
        second_job = second.id

    await manager._persist_reviews(first_job, business_id, [scraped("a", "第一則"), scraped("b", "第二則")])
    await manager._persist_reviews(second_job, business_id, [scraped("a", "第一則")])

    async with SessionLocal() as session:
        counts = {
            job_id: int(
                await session.scalar(
                    select(func.count(JobReview.review_id)).where(JobReview.job_id == job_id)
                )
                or 0
            )
            for job_id in (first_job, second_job)
        }
        jobs = {
            job.id: job
            for job in (
                await session.scalars(
                    select(CrawlJob).where(CrawlJob.id.in_({first_job, second_job}))
                )
            ).all()
        }
    assert counts == {first_job: 2, second_job: 1}
    assert jobs[first_job].collected_count == 2
    assert jobs[second_job].collected_count == 1


@pytest.mark.asyncio
async def test_delete_job_removes_crawl_data_but_keeps_shared_reviews_and_subject() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    first_job, business_id = await make_job()
    async with SessionLocal() as session:
        second = CrawlJob(
            business_id=business_id,
            status="PENDING_COLLECTION",
            max_reviews=2,
            llm_model="gpt-5.4-mini-2026-03-17",
        )
        session.add(second)
        await session.commit()
        second_job = second.id

    await manager._persist_reviews(
        first_job, business_id, [scraped("shared", "共用資料"), scraped("only-first", "只屬於第一個任務")]
    )
    await manager._persist_reviews(second_job, business_id, [scraped("shared", "共用資料")])
    async with SessionLocal() as session:
        session.add(
            Report(
                job_id=first_job,
                business_id=business_id,
                payload={"sample_size": 2},
            )
        )
        await session.commit()

    await manager.delete_job(first_job)

    async with SessionLocal() as session:
        assert await session.get(CrawlJob, first_job) is None
        assert await session.scalar(select(Report).where(Report.job_id == first_job)) is None
        assert await session.get(Business, business_id) is not None
        remaining = list(
            (await session.scalars(select(Review).where(Review.business_id == business_id))).all()
        )
        assert {review.source_review_id for review in remaining} == {"shared"}
        assert await session.scalar(select(func.count(JobReview.review_id)).where(JobReview.job_id == second_job)) == 1


@pytest.mark.asyncio
async def test_partial_collection_requires_explicit_confirmation_and_analyze_is_idempotent() -> None:
    manager = JobManager(Settings(openai_api_key=None))
    job_id, business_id = await make_job("COLLECTION_INTERRUPTED")
    await manager._persist_reviews(job_id, business_id, [scraped("partial", "部分資料")])

    with pytest.raises(ValueError, match="尚未抓滿"):
        await manager.start_analysis(
            job_id,
            model=None,
            accept_partial_collection=False,
        )

    first = await manager.start_analysis(
        job_id,
        model=None,
        accept_partial_collection=True,
    )
    second = await manager.start_analysis(
        job_id,
        model=None,
        accept_partial_collection=True,
    )
    assert first.status == second.status == "ANALYSIS_PENDING"
    assert manager.queue.qsize() == 1


class FakeScraper:
    async def crawl(self, **kwargs) -> CrawlResult:  # type: ignore[no-untyped-def]
        await kwargs["on_batch"]([scraped("collected", "服務很好")])
        await kwargs["on_progress"](1, 1, "已讀取 1 則評論")
        await kwargs["on_metrics"](
            {"visible_cards": 1, "new_reviews": 1, "parse_ms": 1.0, "persist_ms": 1.0}
        )
        return CrawlResult(
            name="測試商家",
            address=None,
            average_rating=5,
            total_review_count=1,
            reviews_seen=1,
            stop_reason="target_reached",
        )


class FakeSentiment:
    def analyze_many(self, texts: list[str]) -> list[SentimentResult]:
        return [
            SentimentResult(
                sentiment="positive",
                confidence=0.9,
                scores={"positive": 0.9, "neutral": 0.05, "negative": 0.05},
                model_id="fake-local",
                language="zh",
            )
            for _text in texts
        ]


class FakeEmbeddings:
    def encode_passages(self, texts: list[str]) -> np.ndarray:
        return np.ones((len(texts), 2), dtype=np.float32)


@pytest.mark.asyncio
async def test_collection_stops_before_manual_full_analysis() -> None:
    manager = JobManager(Settings(openai_api_key=None, sentiment_batch_size=8))
    manager.scraper = FakeScraper()  # type: ignore[assignment]
    manager.sentiment = FakeSentiment()  # type: ignore[assignment]
    manager.embeddings = FakeEmbeddings()  # type: ignore[assignment]
    job_id, _business_id = await make_job()

    await manager._process_collection(job_id)
    collected = await manager.get_job(job_id)
    assert collected.status == "READY_FOR_ANALYSIS"
    assert collected.collection_complete is True
    assert collected.last_completed_stage == "COLLECTION"
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count(ReviewAnalysis.id))) == 0
        assert await session.scalar(select(func.count(Report.id))) == 0

    await manager.start_analysis(
        job_id,
        model=None,
        accept_partial_collection=False,
    )
    await manager._process_analysis(job_id)
    analyzed = await manager.get_job(job_id)
    assert analyzed.status == "PARTIAL"
    assert analyzed.report is not None
    assert analyzed.report.payload["collection"]["complete"] is True
    async with SessionLocal() as session:
        analysis = await session.scalar(
            select(ReviewAnalysis).where(ReviewAnalysis.job_id == job_id)
        )
        assert analysis is not None and analysis.sentiment == "positive"


@pytest.mark.asyncio
async def test_restart_pauses_collection_and_requeues_analysis_from_checkpoint() -> None:
    collecting_id, _ = await make_job("COLLECTING")
    analysis_id, _ = await make_job("LOCAL_ANALYSIS")
    async with SessionLocal() as session:
        analysis = await session.get(CrawlJob, analysis_id)
        assert analysis is not None
        analysis.last_completed_stage = "COLLECTION"
        await session.commit()

    await mark_inflight_jobs_interrupted()

    async with SessionLocal() as session:
        collecting = await session.get(CrawlJob, collecting_id)
        analysis = await session.get(CrawlJob, analysis_id)
    assert collecting is not None and collecting.status == "COLLECTION_INTERRUPTED"
    assert collecting.collection_stop_reason == "application_shutdown"
    assert analysis is not None and analysis.status == "ANALYSIS_PENDING"
    assert analysis.last_completed_stage == "COLLECTION"
