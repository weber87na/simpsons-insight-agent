from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import selectinload

from .cloud import OpenAIService, ReviewInsight, build_cloud_batches
from .config import Settings, get_settings
from .dateparse import parse_relative_date
from .db import SessionLocal
from .decisions import DecisionCoordinator
from .embeddings import EmbeddingService, vector_to_bytes
from .forum_sources import DcardSource, PttSource
from .insights import enrich_report
from .models import (
    Business,
    CrawlJob,
    JobEvent,
    JobReview,
    JobSource,
    Report,
    Review,
    ReviewAnalysis,
    ReviewEmbedding,
    SourceImport,
    utcnow,
)
from .privacy import redact_pii
from .reporting import build_aggregate, deterministic_summary
from .schemas import BusinessCandidate, CreateJobRequest, GoogleMapsSourceConfig, PlanningOptions
from .scraper import (
    CrawlResult,
    MapsBlockedError,
    MapsCanceledError,
    MapsScraper,
    ScrapedReview,
)
from .sentiment import (
    SentimentAnalyzer,
    SentimentResult,
    calibrate_sentiment,
    rating_sentiment,
)
from .sources import (
    CollectedItem,
    GoogleMapsSource,
    ReviewSource,
    SourceBlockedError,
    SourceCallbacks,
    SourceCanceledError,
    SourceCheckpoint,
    SourceProvider,
    SourceUnavailableError,
)

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.browser_lock = asyncio.Lock()
        self.scraper: ReviewSource = MapsScraper(self.settings)
        self.providers: dict[str, SourceProvider] = {
            "google_maps": GoogleMapsSource(lambda: self.scraper, self.browser_lock),
            "ptt": PttSource(self.settings),
            "dcard": DcardSource(self.settings),
        }
        self.sentiment = SentimentAnalyzer(self.settings)
        self.embeddings = EmbeddingService(self.settings)
        self.openai = OpenAIService(self.settings)
        self.decisions = DecisionCoordinator(self.openai, self.queue)
        self._worker: asyncio.Task | None = None
        self._verification_events: dict[str, asyncio.Event] = {}
        self._active_jobs: set[str] = set()
        self._deleting_jobs: set[str] = set()

    async def start(self) -> None:
        await self.decisions.recover()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._worker_loop(), name="simpsons-insight-agent-worker"
            )
        async with SessionLocal() as session:
            result = await session.scalars(
                select(CrawlJob.id).where(
                    or_(
                        CrawlJob.status.in_({"PENDING_COLLECTION", "ANALYSIS_PENDING"}),
                        and_(
                            CrawlJob.auto_plan.is_(True),
                            CrawlJob.cancel_requested.is_(False),
                            CrawlJob.collected_count > 0,
                            or_(
                                CrawlJob.last_completed_stage.is_(None),
                                CrawlJob.last_completed_stage == "COLLECTION",
                            ),
                            CrawlJob.status.in_(
                                {
                                    "READY_FOR_ANALYSIS",
                                    "COLLECTION_INTERRUPTED",
                                    "BLOCKED",
                                    "FAILED",
                                }
                            ),
                        ),
                    )
                )
            )
            for job_id in result.all():
                await self.queue.put(job_id)

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None

    async def search(self, query: str, headless: bool | None = None) -> list[BusinessCandidate]:
        async with self.browser_lock:
            return await self.scraper.search(query, headless)

    async def create_job(self, request: CreateJobRequest) -> CrawlJob:
        subject_input = request.subject_input()
        configs = request.source_configs()
        google_config = next(
            (item for item in configs if item.source == "google_maps"),
            None,
        )
        maps_url = (
            str(google_config.maps_url)
            if isinstance(google_config, GoogleMapsSourceConfig)
            else None
        )
        subject_key = _subject_key(subject_input.kind, subject_input.name, subject_input.address)
        async with SessionLocal() as session:
            business = None
            if maps_url:
                business = await session.scalar(
                    select(Business).where(Business.maps_url == maps_url)
                )
            if business is None:
                business = await session.scalar(
                    select(Business).where(Business.subject_key == subject_key)
                )
            if business is None:
                business = Business(
                    maps_url=maps_url,
                    name=subject_input.name,
                    address=subject_input.address,
                    subject_kind=subject_input.kind,
                    aliases=subject_input.aliases,
                    subject_key=subject_key,
                    average_rating=google_config.average_rating if google_config else None,
                    total_review_count=google_config.total_review_count if google_config else None,
                )
                session.add(business)
                await session.flush()
            else:
                business.name = subject_input.name
                business.address = subject_input.address or business.address
                business.subject_kind = subject_input.kind
                business.aliases = subject_input.aliases
                business.subject_key = business.subject_key or subject_key
                if maps_url:
                    business.maps_url = maps_url
                if google_config and google_config.average_rating is not None:
                    business.average_rating = google_config.average_rating
                if google_config and google_config.total_review_count is not None:
                    business.total_review_count = google_config.total_review_count

            import_ids = {
                import_id
                for config in configs
                if config.source == "dcard"
                for import_id in config.import_ids
            }
            source_imports: list[SourceImport] = []
            if import_ids:
                source_imports = list(
                    (
                        await session.scalars(
                            select(SourceImport).where(SourceImport.id.in_(import_ids))
                        )
                    ).all()
                )
                found_imports = {item.id for item in source_imports}
                missing = import_ids - found_imports
                if missing:
                    raise ValueError(f"找不到 Dcard 匯入批次：{', '.join(sorted(missing))}")
                invalid = [
                    item.filename for item in source_imports if item.validation_status != "VALID"
                ]
                if invalid:
                    raise ValueError(f"Dcard 匯入批次驗證未通過：{', '.join(invalid)}")
                used = [item.filename for item in source_imports if item.job_id]
                if used:
                    raise ValueError(f"Dcard 匯入批次已被其他任務使用：{', '.join(used)}")

            target_count = sum(_source_target(config.model_dump(mode="json")) for config in configs)
            legacy_google = (
                google_config if isinstance(google_config, GoogleMapsSourceConfig) else None
            )

            job = CrawlJob(
                business_id=business.id,
                status="PENDING_COLLECTION",
                max_reviews=target_count,
                sort_order=legacy_google.sort if legacy_google else "newest",
                headless=legacy_google.headless if legacy_google else False,
                llm_model=request.llm_model or self.settings.openai_model_default,
                message="任務已排入佇列",
                auto_plan=request.auto_plan,
                planning_options=request.planning_options.model_dump(mode="json"),
            )
            session.add(job)
            await session.flush()
            for source_import in source_imports:
                source_import.job_id = job.id
            ordering = {"google_maps": 0, "ptt": 1, "dcard": 2}
            for config in sorted(configs, key=lambda item: ordering[item.source]):
                payload = config.model_dump(mode="json")
                session.add(
                    JobSource(
                        job_id=job.id,
                        source=config.source,
                        ordinal=ordering[config.source],
                        config=payload,
                        status="PENDING",
                        target_count=_source_target(payload),
                    )
                )
            await session.commit()
            await session.refresh(job, attribute_names=["source_runs"])
        await self.queue.put(job.id)
        return job

    async def resume(self, job_id: str) -> CrawlJob:
        event = self._verification_events.get(job_id)
        if event is not None:
            event.set()
            await self._set_job(job_id, status="COLLECTING", message="人工驗證完成，繼續蒐集")
        else:
            async with SessionLocal() as session:
                job = await session.get(CrawlJob, job_id)
                if job is None:
                    raise LookupError("找不到任務")
                source_runs = list(
                    (
                        await session.scalars(select(JobSource).where(JobSource.job_id == job_id))
                    ).all()
                )
                has_incomplete_source = any(
                    not source_run.collection_complete for source_run in source_runs
                )
                can_resume_ready = (
                    job.status == "READY_FOR_ANALYSIS" and not job.collection_complete
                )
                can_resume_failed = job.status == "FAILED" and has_incomplete_source
                if (
                    job.status not in {"COLLECTION_INTERRUPTED", "BLOCKED"}
                    and not can_resume_ready
                    and not can_resume_failed
                ):
                    raise ValueError("此任務目前不能續跑")
                if job.last_completed_stage not in {None, "COLLECTION"}:
                    raise ValueError("分析已經開始，不能再向此任務追加評論")
                job.status = "PENDING_COLLECTION"
                job.cancel_requested = False
                job.error = None
                job.message = "蒐集任務已重新排入佇列"
                job.finished_at = None
                for source_run in source_runs:
                    if not source_run.collection_complete:
                        source_run.status = "PENDING"
                        source_run.error = None
                await session.commit()
            await self.queue.put(job_id)
        return await self.get_job(job_id)

    async def start_analysis(
        self,
        job_id: str,
        *,
        model: str | None,
        accept_partial_collection: bool,
    ) -> CrawlJob:
        async with SessionLocal() as session:
            job = await session.scalar(
                select(CrawlJob).options(selectinload(CrawlJob.report)).where(CrawlJob.id == job_id)
            )
            if job is None:
                raise LookupError("找不到任務")
            if job.status in {
                "ANALYSIS_PENDING",
                "LOCAL_ANALYSIS",
                "EMBEDDING",
                "CLOUD_ANALYSIS",
                "REPORTING",
                "COMPLETED",
                "PARTIAL",
            }:
                return job
            if job.status not in {
                "READY_FOR_ANALYSIS",
                "COLLECTION_INTERRUPTED",
                "BLOCKED",
                "FAILED",
            }:
                raise ValueError("評論尚未蒐集完成，不能開始分析")
            if job.collected_count <= 0:
                raise ValueError("目前沒有可分析的評論")
            if not job.collection_complete and not accept_partial_collection:
                raise ValueError("評論尚未抓滿；請確認要分析目前的部分資料")
            selected_model = model or job.llm_model or self.settings.openai_model_default
            if selected_model not in self.settings.allowed_llm_models:
                raise ValueError("未知模型")
            job.llm_model = selected_model
            job.status = "ANALYSIS_PENDING"
            job.cancel_requested = False
            job.error = None
            job.finished_at = None
            job.message = "分析已排入佇列"
            job.last_completed_stage = job.last_completed_stage or "COLLECTION"
            job.degraded_reasons = []
            await session.commit()
        await self.queue.put(job_id)
        return await self.get_job(job_id)

    async def cancel(self, job_id: str) -> CrawlJob:
        async with SessionLocal() as session:
            job = await session.get(CrawlJob, job_id)
            if job is None:
                raise LookupError("找不到任務")
            job.cancel_requested = True
            job.message = "正在取消任務"
            await session.commit()
        event = self._verification_events.get(job_id)
        if event:
            event.set()
        return await self.get_job(job_id)

    async def delete_job(self, job_id: str) -> None:
        """Delete a task and the crawl data that belongs only to that task.

        Reviews are shared by subject across runs, so a review is removed only
        when no other task still links to it. The business/subject record is
        intentionally kept so a user can analyse the same subject again.
        """
        self._deleting_jobs.add(job_id)
        try:
            async with SessionLocal() as session:
                job = await session.get(CrawlJob, job_id)
                if job is None:
                    raise LookupError("找不到任務")
                is_active = job_id in self._active_jobs
                if is_active:
                    job.cancel_requested = True
                    job.message = "正在刪除任務，等待目前蒐集／分析安全停止"
                    await session.commit()

            if is_active:
                verification_event = self._verification_events.get(job_id)
                if verification_event:
                    verification_event.set()
                deadline = asyncio.get_running_loop().time() + 30
                while job_id in self._active_jobs:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ValueError("任務仍在執行，請稍後再試")
                    await asyncio.sleep(0.05)

            async with SessionLocal() as session:
                job = await session.get(CrawlJob, job_id)
                if job is None:
                    return

                review_ids = set(
                    (
                        await session.scalars(
                            select(JobReview.review_id).where(JobReview.job_id == job_id)
                        )
                    ).all()
                )
                shared_review_ids = set()
                if review_ids:
                    shared_review_ids = set(
                        (
                            await session.scalars(
                                select(JobReview.review_id).where(
                                    JobReview.review_id.in_(review_ids),
                                    JobReview.job_id != job_id,
                                )
                            )
                        ).all()
                    )
                orphan_review_ids = review_ids - shared_review_ids

                # Delete child rows explicitly so this also works when the
                # ORM identity map did not load every relationship first.
                await session.execute(delete(ReviewAnalysis).where(ReviewAnalysis.job_id == job_id))
                await session.execute(delete(JobEvent).where(JobEvent.job_id == job_id))
                await session.execute(delete(JobReview).where(JobReview.job_id == job_id))
                await session.execute(delete(SourceImport).where(SourceImport.job_id == job_id))
                await session.execute(delete(JobSource).where(JobSource.job_id == job_id))
                await session.execute(delete(Report).where(Report.job_id == job_id))

                if orphan_review_ids:
                    await session.execute(
                        delete(ReviewEmbedding).where(
                            ReviewEmbedding.review_id.in_(orphan_review_ids)
                        )
                    )
                    await session.execute(delete(Review).where(Review.id.in_(orphan_review_ids)))

                await session.execute(delete(CrawlJob).where(CrawlJob.id == job_id))
                await session.commit()
        finally:
            self._deleting_jobs.discard(job_id)

    async def get_job(self, job_id: str) -> CrawlJob:
        async with SessionLocal() as session:
            job = await session.scalar(
                select(CrawlJob)
                .options(selectinload(CrawlJob.report), selectinload(CrawlJob.source_runs))
                .where(CrawlJob.id == job_id)
            )
            if job is None:
                raise LookupError("找不到任務")
            return job

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background job failed: %s", job_id)
            finally:
                self.queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        if job_id.startswith("decision:"):
            await self.decisions.run(job_id.split(":", 1)[1])
            return
        if job_id in self._deleting_jobs:
            return
        self._active_jobs.add(job_id)
        try:
            if job_id in self._deleting_jobs:
                return
            async with SessionLocal() as session:
                status = await session.scalar(select(CrawlJob.status).where(CrawlJob.id == job_id))
            if status == "ANALYSIS_PENDING":
                await self._process_analysis(job_id)
            elif status == "PENDING_COLLECTION":
                await self._process_collection(job_id)
                await self._auto_analyze(job_id)
            elif status in {"READY_FOR_ANALYSIS", "COLLECTION_INTERRUPTED", "BLOCKED", "FAILED"}:
                await self._auto_analyze(job_id)
        finally:
            self._active_jobs.discard(job_id)

    async def _auto_analyze(self, job_id: str) -> None:
        async with SessionLocal() as session:
            job = await session.get(CrawlJob, job_id)
        if (
            job
            and job.auto_plan
            and not job.cancel_requested
            and job.collected_count > 0
            and job.last_completed_stage in {None, "COLLECTION"}
            and job.status in {"READY_FOR_ANALYSIS", "COLLECTION_INTERRUPTED", "BLOCKED", "FAILED"}
        ):
            await self.start_analysis(job_id, model=None, accept_partial_collection=True)

    async def _process_collection(self, job_id: str) -> None:
        async with SessionLocal() as session:
            has_source_runs = bool(
                await session.scalar(
                    select(func.count(JobSource.id)).where(JobSource.job_id == job_id)
                )
            )
        if has_source_runs:
            await self._process_multi_collection(job_id)
            return
        result: CrawlResult | None = None
        try:
            job, business = await self._load_job_business(job_id)
            if job.cancel_requested:
                await self._finish(job_id, "CANCELED", "任務已取消")
                return
            await self._set_job(
                job_id,
                status="COLLECTING",
                started_at=job.started_at or utcnow(),
                message="正在啟動 Chromium",
                progress=0.02,
            )

            async def on_batch(items: list[ScrapedReview]) -> None:
                await self._persist_reviews(job_id, business.id, items)

            async def on_progress(current: int, total: int | None, message: str) -> None:
                denominator = min(job.max_reviews, total) if total else job.max_reviews
                progress = 0.05 + 0.45 * min(current / max(denominator, 1), 1.0)
                await self._set_job(
                    job_id,
                    collected_count=current,
                    progress=progress,
                    message=message,
                )

            async def on_verification(message: str) -> None:
                event = asyncio.Event()
                self._verification_events[job_id] = event
                await self._set_job(job_id, status="WAITING_FOR_USER", message=message)
                await event.wait()
                self._verification_events.pop(job_id, None)

            async def is_canceled() -> bool:
                return await self._is_job_canceled(job_id)

            async def on_metrics(values: dict) -> None:
                logger.info("crawl_batch", extra={"job_id": job_id, **values})
                await self._record_event(job_id, "collection_batch", values)

            for attempt in range(self.settings.browser_restart_limit + 1):
                known_keys, already_collected = await self._collection_checkpoint(job_id)
                await self._set_job(job_id, attempt_count=job.attempt_count + attempt + 1)
                try:
                    async with self.browser_lock:
                        result = await self.scraper.crawl(
                            job_id=job_id,
                            maps_url=business.maps_url,
                            target=job.max_reviews,
                            sort_order=job.sort_order,
                            headless=job.headless,
                            on_batch=on_batch,
                            on_progress=on_progress,
                            on_verification=on_verification,
                            is_canceled=is_canceled,
                            known_keys=known_keys,
                            already_collected=already_collected,
                            on_metrics=on_metrics,
                        )
                    break
                except Exception as exc:
                    if (
                        type(exc).__name__ == "TargetClosedError"
                        and attempt < self.settings.browser_restart_limit
                        and not await self._is_job_canceled(job_id)
                    ):
                        await self._set_job(
                            job_id, message="瀏覽器意外關閉，正在從 checkpoint 重試"
                        )
                        continue
                    raise

            if result is None:
                raise RuntimeError("蒐集程序沒有回傳結果")
            await self._update_business(business.id, result)
            complete = result.stop_reason in {"target_reached", "total_reached"}
            message = (
                f"蒐集完成，共 {result.reviews_seen} 則；請按「開始分析」。"
                if complete
                else f"目前蒐集 {result.reviews_seen} 則，因 {result.stop_reason} 停止；可續抓或分析目前資料。"
            )
            await self._set_job(
                job_id,
                status="READY_FOR_ANALYSIS",
                collection_complete=complete,
                collection_stop_reason=result.stop_reason,
                last_completed_stage="COLLECTION",
                progress=0.5,
                message=message,
                error=None,
            )
        except MapsCanceledError:
            await self._finish(job_id, "CANCELED", "任務已取消")
        except MapsBlockedError as exc:
            count = await self._job_collected_count(job_id)
            if count:
                await self._set_job(
                    job_id,
                    status="COLLECTION_INTERRUPTED",
                    collection_complete=False,
                    collection_stop_reason="blocked",
                    last_completed_stage="COLLECTION",
                    message=f"{exc} 可續抓或分析目前資料。",
                )
            else:
                await self._finish(job_id, "BLOCKED", str(exc))
        except asyncio.CancelledError:
            await self._set_job(
                job_id,
                status="COLLECTION_INTERRUPTED",
                collection_complete=False,
                collection_stop_reason="application_shutdown",
                message="應用程式關閉，蒐集已中斷",
            )
            raise
        except Exception as exc:
            count = await self._job_collected_count(job_id)
            if count:
                await self._set_job(
                    job_id,
                    status="COLLECTION_INTERRUPTED",
                    collection_complete=False,
                    collection_stop_reason=type(exc).__name__,
                    last_completed_stage="COLLECTION",
                    message="蒐集意外中斷；可續抓或分析目前資料。",
                    error=f"{type(exc).__name__}: {str(exc)[:500]}",
                )
            else:
                await self._finish(
                    job_id,
                    "FAILED",
                    "蒐集失敗；詳細診斷保存在本機 data/diagnostics。",
                    error=f"{type(exc).__name__}: {str(exc)[:500]}",
                )

    async def _process_multi_collection(self, job_id: str) -> None:
        job, business = await self._load_job_business(job_id)
        if job.cancel_requested:
            await self._finish(job_id, "CANCELED", "任務已取消")
            return
        async with SessionLocal() as session:
            source_runs = list(
                (
                    await session.scalars(
                        select(JobSource)
                        .where(JobSource.job_id == job_id)
                        .order_by(JobSource.ordinal)
                    )
                ).all()
            )
        await self._set_job(
            job_id,
            status="COLLECTING",
            started_at=job.started_at or utcnow(),
            message="正在啟動跨平台蒐集",
            progress=0.02,
        )
        source_total = max(len(source_runs), 1)
        interrupted = False
        for source_index, source_run in enumerate(source_runs):
            if source_run.collection_complete:
                continue
            if await self._is_job_canceled(job_id):
                await self._finish(job_id, "CANCELED", "任務已取消")
                return
            await self._set_source_run(
                source_run.id,
                status="COLLECTING",
                error=None,
                attempt_count=source_run.attempt_count + 1,
            )
            await self._set_job(job_id, message=f"正在蒐集 {source_run.source}")
            known_keys, already_collected = await self._source_checkpoint(job_id, source_run.source)
            _, existing_posts, existing_comments = await self._source_counts(
                job_id, source_run.source
            )

            async def on_batch(items: list[CollectedItem]) -> None:
                await self._persist_items(job_id, business.id, items)

            async def on_progress(
                posts: int,
                comments: int,
                target: int,
                message: str,
                _source_index: int = source_index,
                _source_run: JobSource = source_run,
            ) -> None:
                current = posts + comments
                source_fraction = min(current / max(target, 1), 1.0)
                progress = 0.05 + 0.45 * (_source_index + source_fraction) / source_total
                await self._set_source_run(
                    _source_run.id,
                    collected_count=current,
                    post_count=posts if _source_run.source != "google_maps" else 0,
                    comment_count=comments,
                )
                await self._set_job(job_id, progress=progress, message=message)

            async def is_canceled() -> bool:
                return await self._is_job_canceled(job_id)

            async def on_verification(message: str) -> None:
                event = asyncio.Event()
                self._verification_events[job_id] = event
                await self._set_job(job_id, status="WAITING_FOR_USER", message=message)
                await event.wait()
                self._verification_events.pop(job_id, None)

            async def on_metrics(
                values: dict,
                _source: str = source_run.source,
                _run_id: str = source_run.id,
            ) -> None:
                metrics = dict(values)
                provider_checkpoint = metrics.pop("checkpoint", None)
                if isinstance(provider_checkpoint, dict):
                    # Providers emit only after the corresponding batch is committed.
                    await self._set_source_run(_run_id, checkpoint=provider_checkpoint)
                await self._record_event(
                    job_id,
                    "collection_batch",
                    {"source": _source, **metrics},
                )

            async def on_metadata(values: dict) -> None:
                await self._update_business_metadata(business.id, values)

            try:
                config = await self._resolved_source_config(source_run)
                config["_job_id"] = job_id
                provider = self.providers[source_run.source]
                provider_checkpoint = dict(source_run.checkpoint or {})
                if source_run.source in {"ptt", "dcard"}:
                    provider_checkpoint.update(
                        await self._forum_checkpoint(job_id, source_run.source)
                    )
                result = await provider.collect(
                    config=config,
                    checkpoint=SourceCheckpoint(
                        known_keys=known_keys,
                        collected_count=already_collected,
                        post_count=existing_posts,
                        comment_count=existing_comments,
                        provider=provider_checkpoint,
                    ),
                    callbacks=SourceCallbacks(
                        on_batch=on_batch,
                        on_progress=on_progress,
                        is_canceled=is_canceled,
                        on_verification=on_verification,
                        on_metrics=on_metrics,
                        on_metadata=on_metadata,
                    ),
                )
                count, post_count, comment_count = await self._source_counts(
                    job_id, source_run.source
                )
                status = "COMPLETE" if result.complete else "PARTIAL"
                source_error = None
                if result.stop_reason == "public_source_blocked":
                    status = "PARTIAL" if count else "BLOCKED"
                    source_error = str(result.checkpoint.get("blocked_reason") or "公開來源拒絕請求")[:500]
                elif result.stop_reason == "public_source_unavailable":
                    status = "PARTIAL" if count else "FAILED"
                    source_error = str(result.checkpoint.get("unavailable_reason") or "公開來源暫時無法使用")[:500]
                await self._set_source_run(
                    source_run.id,
                    status=status,
                    collected_count=count,
                    post_count=post_count,
                    comment_count=comment_count,
                    collection_complete=result.complete,
                    stop_reason=result.stop_reason,
                    checkpoint=result.checkpoint,
                    error=source_error,
                )
            except (MapsCanceledError, SourceCanceledError):
                count, post_count, comment_count = await self._source_counts(
                    job_id, source_run.source
                )
                await self._set_source_run(
                    source_run.id,
                    status="PARTIAL",
                    collected_count=count,
                    post_count=post_count,
                    comment_count=comment_count,
                    collection_complete=False,
                    stop_reason="canceled",
                )
                await self._finish(job_id, "CANCELED", "任務已取消")
                return
            except (MapsBlockedError, SourceBlockedError) as exc:
                count, post_count, comment_count = await self._source_counts(
                    job_id, source_run.source
                )
                await self._set_source_run(
                    source_run.id,
                    status="PARTIAL" if count else "BLOCKED",
                    collected_count=count,
                    post_count=post_count,
                    comment_count=comment_count,
                    collection_complete=False,
                    stop_reason="blocked",
                    error=str(exc)[:500],
                )
                interrupted = True
            except SourceUnavailableError as exc:
                count, post_count, comment_count = await self._source_counts(
                    job_id, source_run.source
                )
                await self._set_source_run(
                    source_run.id,
                    status="PARTIAL" if count else "FAILED",
                    collected_count=count,
                    post_count=post_count,
                    comment_count=comment_count,
                    collection_complete=False,
                    stop_reason="source_unavailable",
                    error=str(exc)[:500],
                )
                interrupted = True
            except asyncio.CancelledError:
                await self._set_source_run(
                    source_run.id,
                    status="PARTIAL",
                    collection_complete=False,
                    stop_reason="application_shutdown",
                )
                await self._set_job(
                    job_id,
                    status="COLLECTION_INTERRUPTED",
                    collection_complete=False,
                    collection_stop_reason="application_shutdown",
                    message="應用程式關閉，蒐集已中斷",
                )
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Source collection failed: %s", source_run.source)
                count, post_count, comment_count = await self._source_counts(
                    job_id, source_run.source
                )
                await self._set_source_run(
                    source_run.id,
                    status="PARTIAL" if count else "FAILED",
                    collected_count=count,
                    post_count=post_count,
                    comment_count=comment_count,
                    collection_complete=False,
                    stop_reason=type(exc).__name__,
                    error=f"{type(exc).__name__}: {str(exc)[:450]}",
                )
                interrupted = True

        async with SessionLocal() as session:
            refreshed = list(
                (await session.scalars(select(JobSource).where(JobSource.job_id == job_id))).all()
            )
        count = await self._job_collected_count(job_id)
        complete = all(run.collection_complete for run in refreshed)
        interrupted = interrupted or not complete
        reasons = [
            f"{run.source}:{run.stop_reason}"
            for run in refreshed
            if run.stop_reason and not run.collection_complete
        ]
        if count == 0:
            terminal_status = (
                "BLOCKED" if any(run.status == "BLOCKED" for run in refreshed) else "FAILED"
            )
            await self._finish(
                job_id,
                terminal_status,
                "所有來源都沒有可分析的內容。",
                error="；".join(reasons)[:500] or None,
            )
            await self._set_job(
                job_id,
                collection_complete=False,
                collection_stop_reason=";".join(reasons)[:100] or "no_data",
            )
            return
        message = (
            f"蒐集完成，共 {count} 筆內容；請按「開始分析」。"
            if complete
            else f"目前蒐集 {count} 筆，部分來源未完整；可續抓或分析目前資料。"
        )
        await self._set_job(
            job_id,
            status="READY_FOR_ANALYSIS",
            collected_count=count,
            collection_complete=complete,
            collection_stop_reason=";".join(reasons)[:100] if interrupted else "all_complete",
            last_completed_stage="COLLECTION",
            progress=0.5,
            message=message,
            error=None,
        )

    async def _resolved_source_config(self, source_run: JobSource) -> dict:
        config = dict(source_run.config or {})
        if source_run.source == "google_maps" and not config.get("maps_url"):
            async with SessionLocal() as session:
                maps_url = await session.scalar(
                    select(Business.maps_url)
                    .join(CrawlJob, CrawlJob.business_id == Business.id)
                    .where(CrawlJob.id == source_run.job_id)
                )
            if not maps_url:
                raise ValueError("既有 Google 任務缺少 Maps URL")
            config["maps_url"] = maps_url
        if source_run.source != "dcard" or not config.get("import_ids"):
            return config
        async with SessionLocal() as session:
            imports = list(
                (
                    await session.scalars(
                        select(SourceImport).where(SourceImport.id.in_(config["import_ids"]))
                    )
                ).all()
            )
        config["import_records"] = [record for item in imports for record in item.payload]
        return config

    async def _source_checkpoint(self, job_id: str, source: str) -> tuple[set[str], int]:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(Review.source_item_id, Review.content_hash)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id, Review.source == source)
            )
            keys: set[str] = set()
            count = 0
            for source_id, content_hash in rows:
                count += 1
                if source_id:
                    keys.add(source_id)
                keys.add(content_hash)
            return keys, count

    async def _source_counts(self, job_id: str, source: str) -> tuple[int, int, int]:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(Review.content_type, func.count(Review.id))
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id, Review.source == source)
                .group_by(Review.content_type)
            )
            counts = {kind: int(value) for kind, value in rows}
        total = sum(counts.values())
        return total, counts.get("post", 0), counts.get("comment", 0)

    async def _forum_checkpoint(self, job_id: str, source: str) -> dict:
        """Recover thread limits from committed rows, even after an interrupted callback."""
        async with SessionLocal() as session:
            rows = await session.execute(
                select(Review.content_type, Review.thread_source_id, Review.source_item_id, Review.source_url)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id, Review.source == source)
            )
            comment_counts: dict[str, int] = {}
            thread_ids: set[str] = set()
            thread_urls: set[str] = set()
            for content_type, thread_id, source_id, source_url in rows:
                thread = thread_id or source_id
                if not thread:
                    continue
                if content_type == "comment":
                    comment_counts[thread] = comment_counts.get(thread, 0) + 1
                elif content_type == "post":
                    thread_ids.add(thread)
                    if source_url:
                        thread_urls.add(source_url)
        return {
            "thread_comment_counts": comment_counts,
            "thread_ids": sorted(thread_ids),
            "thread_urls": sorted(thread_urls),
        }

    async def _set_source_run(self, source_run_id: str, **values: object) -> None:
        async with SessionLocal() as session:
            source_run = await session.get(JobSource, source_run_id)
            if source_run is None:
                return
            for key, value in values.items():
                setattr(source_run, key, value)
            source_run.updated_at = datetime.now(UTC)
            await session.commit()

    async def _process_analysis(self, job_id: str) -> None:
        partial_reasons: list[str] = []
        try:
            job, business = await self._load_job_business(job_id)
            if job.cancel_requested:
                await self._finish(job_id, "CANCELED", "任務已取消")
                return
            reviews = await self._reviews_for_job(job_id)
            if not reviews:
                raise RuntimeError("任務沒有可分析的評論")

            if not _stage_at_least(job.last_completed_stage, "LOCAL_ANALYSIS"):
                await self._set_job(
                    job_id,
                    status="LOCAL_ANALYSIS",
                    processed_count=0,
                    progress=0.52,
                    message="正在執行本地情感分析",
                )
                fallback_count = await self._local_analysis(job_id, reviews)
                if fallback_count:
                    partial_reasons.append(
                        f"{fallback_count} 則評論因本地模型不可用而使用規則備援。"
                    )
                await self._set_job(job_id, last_completed_stage="LOCAL_ANALYSIS")

            if not _stage_at_least(job.last_completed_stage, "EMBEDDING"):
                await self._set_job(
                    job_id,
                    status="EMBEDDING",
                    progress=0.7,
                    message="正在建立評論向量",
                )
                try:
                    await self._create_embeddings(reviews)
                except Exception:
                    partial_reasons.append("本地向量模型載入失敗，問答將使用關鍵字備援。")
                await self._set_job(job_id, last_completed_stage="EMBEDDING")

            if await self._is_job_canceled(job_id):
                raise MapsCanceledError("任務已取消")
            cloud_ok = _stage_at_least(job.last_completed_stage, "CLOUD_ANALYSIS")
            if not cloud_ok and self.openai.available:
                await self._set_job(
                    job_id,
                    status="CLOUD_ANALYSIS",
                    progress=0.76,
                    message="正在執行 OpenAI 面向分析",
                )
                try:
                    await self._cloud_analysis(job_id, reviews, job.llm_model)
                    cloud_ok = True
                    await self._set_job(job_id, last_completed_stage="CLOUD_ANALYSIS")
                except MapsCanceledError:
                    raise
                except Exception as exc:
                    partial_reasons.append(f"OpenAI 面向分析失敗：{type(exc).__name__}")
            elif not cloud_ok:
                partial_reasons.append("未設定 OPENAI_API_KEY，已略過雲端面向分析與摘要。")

            if await self._is_job_canceled(job_id):
                raise MapsCanceledError("任務已取消")
            await self._set_job(
                job_id,
                status="REPORTING",
                progress=0.9,
                message="正在產生報告",
            )
            report, summary_cloud_ok = await self._build_report(
                job_id, business.id, job.llm_model, cloud_ok
            )
            if job.auto_plan:
                self.decisions.cloud = self.openai
                await self.decisions.start(
                    report.id, PlanningOptions.model_validate(job.planning_options)
                )
            if cloud_ok and not summary_cloud_ok:
                partial_reasons.append("OpenAI 管理摘要失敗。")
            await self._set_job(
                job_id,
                last_completed_stage="REPORTING",
                degraded_reasons=partial_reasons,
            )
            if partial_reasons:
                await self._finish(job_id, "PARTIAL", " ".join(partial_reasons), progress=1.0)
            else:
                await self._finish(job_id, "COMPLETED", "分析完成", progress=1.0)
        except MapsCanceledError:
            await self._finish(job_id, "CANCELED", "任務已取消")
        except asyncio.CancelledError:
            await self._set_job(
                job_id, status="ANALYSIS_PENDING", message="應用程式關閉，將於下次啟動續跑"
            )
            raise
        except Exception as exc:
            await self._finish(
                job_id,
                "FAILED",
                "分析執行失敗，可再次按開始分析重試。",
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
            )

    async def _persist_reviews(
        self, job_id: str, business_id: str, items: list[ScrapedReview]
    ) -> None:
        await self._persist_items(
            job_id,
            business_id,
            [
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
            ],
        )

    async def _persist_items(
        self, job_id: str, business_id: str, items: list[CollectedItem]
    ) -> None:
        if not items:
            return
        async with SessionLocal() as session:
            source_kinds = {item.source for item in items}
            source_ids = {item.source_item_id for item in items if item.source_item_id}
            source_ids.update(alias for item in items for alias in item.legacy_source_item_ids)
            hashes = {item.content_hash for item in items}
            conditions = [Review.content_hash.in_(hashes)]
            if source_ids:
                conditions.append(Review.source_item_id.in_(source_ids))
            existing_reviews = list(
                (
                    await session.scalars(
                        select(Review).where(
                            Review.business_id == business_id,
                            Review.source.in_(source_kinds),
                            or_(*conditions),
                        )
                    )
                ).all()
            )
            by_source = {
                (review.source, review.source_item_id): review
                for review in existing_reviews
                if review.source_item_id
            }
            by_hash = {(review.source, review.content_hash): review for review in existing_reviews}
            observed: list[Review] = []
            for item in items:
                existing = by_source.get((item.source, item.source_item_id)) or by_hash.get(
                    (item.source, item.content_hash)
                )
                if existing is None:
                    existing = next(
                        (by_source[(item.source, alias)] for alias in item.legacy_source_item_ids
                         if (item.source, alias) in by_source),
                        None,
                    )
                parsed = parse_relative_date(item.relative_date)
                published_at = item.published_at or parsed.estimated_at
                precision = item.date_precision if item.published_at else parsed.precision
                if existing:
                    by_source[(item.source, item.source_item_id)] = existing
                    existing.text = item.text or existing.text
                    existing.relative_date = item.relative_date or existing.relative_date
                    existing.owner_reply = item.owner_reply or existing.owner_reply
                    existing.published_at_estimated = published_at
                    existing.date_precision = precision
                    existing.rating = item.rating if item.rating is not None else existing.rating
                    existing.title = item.title or existing.title
                    existing.board = item.board or existing.board
                    existing.platform_data = item.platform_data or existing.platform_data
                    observed.append(existing)
                    continue
                legacy_source_id = (
                    item.source_item_id
                    if item.source == "google_maps"
                    else f"{item.source}:{item.source_item_id}"[:500]
                )
                review = Review(
                    business_id=business_id,
                    source_review_id=legacy_source_id,
                    source=item.source,
                    content_type=item.content_type,
                    source_item_id=item.source_item_id,
                    thread_source_id=item.thread_source_id,
                    parent_source_id=item.parent_source_id,
                    content_hash=item.content_hash,
                    author_name=item.author_name,
                    author_hash=item.author_hash,
                    title=item.title,
                    board=item.board,
                    platform_data=item.platform_data,
                    rating=item.rating,
                    text=item.text,
                    relative_date=item.relative_date,
                    published_at_estimated=published_at,
                    date_precision=precision,
                    owner_reply=item.owner_reply,
                    source_url=item.source_url,
                )
                session.add(review)
                observed.append(review)
                by_source[(item.source, item.source_item_id)] = review
                by_hash[(item.source, item.content_hash)] = review
            await session.flush()

            review_ids = {review.id for review in observed}
            linked_ids = set(
                (
                    await session.scalars(
                        select(JobReview.review_id).where(
                            JobReview.job_id == job_id,
                            JobReview.review_id.in_(review_ids),
                        )
                    )
                ).all()
            )
            ordinal = int(
                await session.scalar(
                    select(func.max(JobReview.ordinal)).where(JobReview.job_id == job_id)
                )
                or 0
            )
            for review in observed:
                if review.id in linked_ids:
                    continue
                ordinal += 1
                session.add(JobReview(job_id=job_id, review_id=review.id, ordinal=ordinal))
                linked_ids.add(review.id)
            await session.flush()
            count = await session.scalar(
                select(func.count(JobReview.review_id)).where(JobReview.job_id == job_id)
            )
            job = await session.get(CrawlJob, job_id)
            if job:
                job.collected_count = int(count or 0)
            await session.commit()

    async def _local_analysis(self, job_id: str, reviews: list[Review]) -> int:
        total = max(len(reviews), 1)
        fallback_count = 0
        batch_size = self.settings.sentiment_batch_size
        for start in range(0, len(reviews), batch_size):
            if await self._is_job_canceled(job_id):
                raise MapsCanceledError("任務已取消")
            batch = reviews[start : start + batch_size]
            results = await asyncio.to_thread(
                self.sentiment.analyze_many, [review.text for review in batch]
            )
            for index, review in enumerate(batch):
                signal = (review.platform_data or {}).get("signal")
                if (
                    review.source == "ptt"
                    and review.content_type == "comment"
                    and len(review.text) <= 4
                    and signal in {"push", "boo"}
                ):
                    sentiment = "positive" if signal == "push" else "negative"
                    results[index] = SentimentResult(
                        sentiment=sentiment,
                        confidence=0.95,
                        scores={sentiment: 0.95, "neutral": 0.05},
                        model_id="ptt-signal-rule",
                        language=results[index].language,
                    )
                results[index] = calibrate_sentiment(results[index], review.text, review.rating)
            fallback_count += sum(
                bool(review.text) and result.model_id.startswith("heuristic-fallback")
                for review, result in zip(batch, results, strict=True)
            )
            async with SessionLocal() as session:
                review_ids = [review.id for review in batch]
                stored = list(
                    (await session.scalars(select(Review).where(Review.id.in_(review_ids)))).all()
                )
                stored_by_id = {review.id: review for review in stored}
                analyses = list(
                    (
                        await session.scalars(
                            select(ReviewAnalysis).where(
                                ReviewAnalysis.job_id == job_id,
                                ReviewAnalysis.review_id.in_(review_ids),
                            )
                        )
                    ).all()
                )
                analysis_by_review = {analysis.review_id: analysis for analysis in analyses}
                for review, result in zip(batch, results, strict=True):
                    stored_review = stored_by_id.get(review.id)
                    if stored_review is None:
                        continue
                    stored_review.redacted_text = redact_pii(stored_review.text)
                    stored_review.language = result.language
                    rated = rating_sentiment(stored_review.rating)
                    conflict = (
                        result.sentiment != "rating_only"
                        and rated is not None
                        and result.sentiment != rated
                    )
                    analysis = analysis_by_review.get(review.id)
                    if analysis is None:
                        analysis = ReviewAnalysis(
                            job_id=job_id,
                            review_id=review.id,
                            sentiment=result.sentiment,
                            confidence=result.confidence,
                            sentiment_scores=result.scores,
                            rating_sentiment=rated,
                            rating_text_conflict=conflict,
                            local_model_id=result.model_id,
                        )
                        session.add(analysis)
                    else:
                        analysis.sentiment = result.sentiment
                        analysis.confidence = result.confidence
                        analysis.sentiment_scores = result.scores
                        analysis.rating_sentiment = rated
                        analysis.rating_text_conflict = conflict
                        analysis.local_model_id = result.model_id
                job = await session.get(CrawlJob, job_id)
                if job:
                    processed = min(start + len(batch), len(reviews))
                    job.processed_count = processed
                    job.progress = 0.52 + 0.18 * processed / total
                    job.message = f"已完成 {processed}/{total} 則本地情感分析"
                await session.commit()
        return fallback_count

    async def _create_embeddings(self, reviews: list[Review]) -> None:
        review_ids = [review.id for review in reviews if review.text]
        async with SessionLocal() as session:
            existing_ids = set(
                (
                    await session.scalars(
                        select(ReviewEmbedding.review_id).where(
                            ReviewEmbedding.review_id.in_(review_ids),
                            ReviewEmbedding.model_id == self.settings.embedding_model,
                        )
                    )
                ).all()
            )
        values = [
            (review.id, redact_pii(review.text))
            for review in reviews
            if review.text and review.id not in existing_ids
        ]
        if not values:
            return
        vectors = await asyncio.to_thread(
            self.embeddings.encode_passages, [text for _, text in values]
        )
        async with SessionLocal() as session:
            for (review_id, _), vector in zip(values, vectors, strict=True):
                raw, dimension = vector_to_bytes(vector)
                session.add(
                    ReviewEmbedding(
                        review_id=review_id,
                        model_id=self.settings.embedding_model,
                        dimension=dimension,
                        vector=raw,
                    )
                )
            await session.commit()

    async def _cloud_analysis(self, job_id: str, reviews: list[Review], model: str) -> None:
        async with SessionLocal() as session:
            completed_ids = set(
                (
                    await session.scalars(
                        select(ReviewAnalysis.review_id).where(
                            ReviewAnalysis.job_id == job_id,
                            ReviewAnalysis.cloud_model_id == model,
                            ReviewAnalysis.cloud_status == "COMPLETED",
                        )
                    )
                ).all()
            )
        payload = [
            item
            for item in build_anonymized_payload(reviews)
            if item["review_key"] not in completed_ids
        ]
        batches = build_cloud_batches(
            payload,
            max_reviews=self.settings.cloud_batch_reviews,
            max_chars=self.settings.cloud_batch_chars,
        )
        for index, batch in enumerate(batches, start=1):
            if await self._is_job_canceled(job_id):
                raise MapsCanceledError("任務已取消")
            expected = {item["review_key"] for item in batch}
            insights: dict[str, ReviewInsight] = {}
            remaining = batch
            for _attempt in range(2):
                result = await self.openai.analyze_batch(remaining, model)
                for insight in result.reviews:
                    if insight.review_key in expected:
                        insights[insight.review_key] = insight
                missing = expected - insights.keys()
                if not missing:
                    break
                remaining = [item for item in batch if item["review_key"] in missing]
            missing = expected - insights.keys()
            if missing:
                raise RuntimeError(f"OpenAI 批次缺少 {len(missing)} 則評論結果")
            async with SessionLocal() as session:
                analyses = list(
                    (
                        await session.scalars(
                            select(ReviewAnalysis).where(
                                ReviewAnalysis.job_id == job_id,
                                ReviewAnalysis.review_id.in_(expected),
                            )
                        )
                    ).all()
                )
                for analysis in analyses:
                    stored_insight = insights.get(analysis.review_id)
                    if analysis:
                        if stored_insight is None:
                            continue
                        analysis.aspects = [str(item.value) for item in stored_insight.aspects]
                        analysis.negative_aspects = [
                            str(item.value) for item in stored_insight.negative_aspects
                        ]
                        analysis.key_points = stored_insight.key_points
                        analysis.cloud_model_id = model
                        analysis.cloud_status = "COMPLETED"
                job = await session.get(CrawlJob, job_id)
                if job:
                    job.progress = 0.72 + 0.16 * index / max(len(batches), 1)
                    job.message = f"OpenAI 面向分析批次 {index}/{len(batches)}"
                await session.commit()

    async def _build_report(
        self, job_id: str, business_id: str, model: str, cloud_ok: bool
    ) -> tuple[Report, bool]:
        async with SessionLocal() as session:
            existing_report = await session.scalar(select(Report).where(Report.job_id == job_id))
            if existing_report and existing_report.payload.get("schema_version") in {3, 4} and "analytics_items" in existing_report.payload:
                return existing_report, existing_report.status == "READY"
            business = await session.get(Business, business_id)
            job = await session.get(CrawlJob, job_id)
            result = await session.execute(
                select(Review, JobReview.ordinal)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id)
                .order_by(JobReview.ordinal)
            )
            review_rows = list(result.all())
            analyses = list(
                (
                    await session.scalars(
                        select(ReviewAnalysis).where(ReviewAnalysis.job_id == job_id)
                    )
                ).all()
            )
            analysis_by_review = {analysis.review_id: analysis for analysis in analyses}
            rows = []
            for review, _ordinal in review_rows:
                analysis = analysis_by_review.get(review.id)
                rows.append(
                    {
                        "id": review.id,
                        "source": review.source,
                        "content_type": review.content_type,
                        "title": review.title,
                        "board": review.board,
                        "thread_source_id": review.thread_source_id,
                        "source_url": review.source_url,
                        "platform_data": review.platform_data,
                        "rating": review.rating,
                        "text": review.text,
                        "redacted_text": review.redacted_text,
                        "published_at_estimated": review.published_at_estimated,
                        "date_precision": review.date_precision,
                        "sentiment": analysis.sentiment if analysis else None,
                        "confidence": analysis.confidence if analysis else None,
                        "local_model_id": analysis.local_model_id if analysis else None,
                        "rating_text_conflict": analysis.rating_text_conflict
                        if analysis
                        else False,
                        "aspects": analysis.aspects if analysis else [],
                        "key_points": analysis.key_points if analysis else [],
                    }
                )
            business_payload = {
                "id": business.id if business else business_id,
                "name": business.name if business else "未知商家",
                "kind": business.subject_kind if business else "business",
                "aliases": list(business.aliases or []) if business else [],
                "address": business.address if business else None,
                "average_rating": business.average_rating if business else None,
                "total_review_count": business.total_review_count if business else None,
            }
            source_runs = list(
                (
                    await session.scalars(
                        select(JobSource)
                        .where(JobSource.job_id == job_id)
                        .order_by(JobSource.ordinal)
                    )
                ).all()
            )
            collection_payload = {
                "target_count": job.max_reviews if job else self.settings.max_reviews,
                "actual_count": len(rows),
                "complete": bool(job.collection_complete) if job else False,
                "stop_reason": job.collection_stop_reason if job else None,
                "sources": {
                    run.source: {
                        "status": run.status,
                        "target_count": run.target_count,
                        "actual_count": run.collected_count,
                        "post_count": run.post_count,
                        "comment_count": run.comment_count,
                        "complete": run.collection_complete,
                        "stop_reason": run.stop_reason,
                        "error": run.error,
                    }
                    for run in source_runs
                },
            }

        aggregate = build_aggregate(
            business=business_payload,
            reviews=rows,
            model_id=model if cloud_ok else None,
        )
        aggregate["review_ids"] = [row["id"] for row in rows]
        aggregate["item_ids"] = aggregate["review_ids"]
        aggregate["collection"] = collection_payload
        summary = deterministic_summary(aggregate)
        summary_cloud_ok = False
        if cloud_ok:
            try:
                executive = await self.openai.executive_summary(aggregate, model)
                summary = executive.model_dump()
                summary_cloud_ok = True
            except Exception:  # noqa: BLE001
                logger.exception("Executive summary failed for job %s", job_id)
        aggregate["executive"] = summary

        async with SessionLocal() as session:
            report = await session.scalar(select(Report).where(Report.job_id == job_id))
            if report is None:
                report = Report(
                    job_id=job_id,
                    business_id=business_id,
                    model_id=model if cloud_ok else None,
                    status="READY" if summary_cloud_ok else "PARTIAL",
                    payload=aggregate,
                )
                session.add(report)
            else:
                report.model_id = model if cloud_ok else None
                report.status = "READY" if summary_cloud_ok else "PARTIAL"
                aggregate.update(
                    {
                        key: report.payload[key]
                        for key in (
                            "schema_version",
                            "analytics_items",
                            "topics",
                            "topic_analysis",
                            "trends",
                            "decision",
                        )
                        if key in report.payload
                    }
                )
                report.payload = aggregate
            await session.flush()
            await enrich_report(session, report, self.settings.embedding_model)
            await session.commit()
            await session.refresh(report)
            return report, summary_cloud_ok

    async def _reviews_for_job(self, job_id: str) -> list[Review]:
        async with SessionLocal() as session:
            result = await session.scalars(
                select(Review)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id)
                .order_by(JobReview.ordinal)
            )
            return list(result.all())

    async def _collection_checkpoint(self, job_id: str) -> tuple[set[str], int]:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(Review.source_review_id, Review.content_hash)
                .join(JobReview, JobReview.review_id == Review.id)
                .where(JobReview.job_id == job_id)
            )
            keys: set[str] = set()
            count = 0
            for source_id, content_hash in rows:
                count += 1
                if source_id:
                    keys.add(source_id)
                keys.add(content_hash)
            return keys, count

    async def _load_job_business(self, job_id: str) -> tuple[CrawlJob, Business]:
        async with SessionLocal() as session:
            job = await session.get(CrawlJob, job_id)
            if job is None:
                raise LookupError("找不到任務")
            business = await session.get(Business, job.business_id)
            if business is None:
                raise LookupError("找不到商家")
            return job, business

    async def _update_business(self, business_id: str, result: CrawlResult) -> None:
        async with SessionLocal() as session:
            business = await session.get(Business, business_id)
            if business:
                business.name = result.name or business.name
                business.address = result.address or business.address
                if result.average_rating is not None:
                    business.average_rating = result.average_rating
                if result.total_review_count is not None:
                    business.total_review_count = result.total_review_count
                await session.commit()

    async def _update_business_metadata(self, business_id: str, values: dict) -> None:
        async with SessionLocal() as session:
            business = await session.get(Business, business_id)
            if business:
                business.name = values.get("name") or business.name
                business.address = values.get("address") or business.address
                if values.get("average_rating") is not None:
                    business.average_rating = values["average_rating"]
                if values.get("total_review_count") is not None:
                    business.total_review_count = values["total_review_count"]
                await session.commit()

    async def _job_collected_count(self, job_id: str) -> int:
        async with SessionLocal() as session:
            value = await session.scalar(
                select(func.count(JobReview.review_id)).where(JobReview.job_id == job_id)
            )
            return int(value or 0)

    async def _is_job_canceled(self, job_id: str) -> bool:
        async with SessionLocal() as session:
            value = await session.scalar(
                select(CrawlJob.cancel_requested).where(CrawlJob.id == job_id)
            )
            return bool(value)

    async def _set_job(self, job_id: str, **values: object) -> None:
        async with SessionLocal() as session:
            job = await session.get(CrawlJob, job_id)
            if job is None:
                return
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(UTC)
            await session.commit()

    async def _record_event(self, job_id: str, event_type: str, payload: dict) -> None:
        async with SessionLocal() as session:
            session.add(JobEvent(job_id=job_id, event_type=event_type, payload=payload))
            await session.commit()

    async def _finish(
        self,
        job_id: str,
        status: str,
        message: str,
        *,
        error: str | None = None,
        progress: float | None = None,
    ) -> None:
        values: dict[str, object] = {
            "status": status,
            "message": message,
            "error": error,
            "finished_at": utcnow(),
        }
        if progress is not None:
            values["progress"] = progress
        await self._set_job(job_id, **values)


def build_anonymized_payload(reviews: list[Review]) -> list[dict]:
    payload: list[dict] = []
    for review in reviews:
        if not review.text:
            continue
        item = {
            "review_key": review.id,
            "rating": review.rating,
            "published_at": review.published_at_estimated.isoformat()
            if review.published_at_estimated
            else None,
            "text": redact_pii(review.text),
        }
        source = review.source or "google_maps"
        if source != "google_maps":
            item.update(
                {
                    "source": source,
                    "content_type": review.content_type or "review",
                    "thread_title": redact_pii(review.title),
                }
            )
        payload.append(item)
    return payload


def _stage_at_least(current: str | None, expected: str) -> bool:
    stages = ["COLLECTION", "LOCAL_ANALYSIS", "EMBEDDING", "CLOUD_ANALYSIS", "REPORTING"]
    if current not in stages or expected not in stages:
        return False
    return stages.index(current) >= stages.index(expected)


def _subject_key(kind: str, name: str, address: str | None) -> str:
    normalized = "|".join(part.strip().casefold() for part in (kind, name, address or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source_target(config: dict) -> int:
    source = config.get("source")
    if source == "google_maps":
        return int(config.get("max_reviews", 500))
    return int(config.get("max_posts", 0)) + int(config.get("max_comments", 0))
