from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .analytics import (
    ReportFilters,
    csv_safe,
    report_filters,
    resolve_scope,
    review_view,
    sorted_items,
)
from .author_privacy import AuthorHasher
from .config import get_settings
from .db import dispose_db, get_session, init_db, mark_inflight_jobs_interrupted
from .imports import MAX_IMPORT_BYTES, dcard_template, parse_dcard_import
from .insight_api import router as insight_router
from .jobs import JobManager
from .models import (
    STREAM_END_JOB_STATUSES,
    Business,
    CrawlJob,
    Report,
    Review,
    ReviewAnalysis,
    SourceImport,
)
from .qa import QuestionService
from .schemas import (
    BusinessSearchRequest,
    BusinessSearchResponse,
    CreateJobRequest,
    JobResponse,
    PaginatedReviews,
    QuestionRequest,
    QuestionResponse,
    ReportResponse,
    ReviewResponse,
    SourceImportResponse,
    StartAnalysisRequest,
)

settings = get_settings()
package_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(package_dir / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await mark_inflight_jobs_interrupted()
    manager = JobManager(settings)
    await manager.start()
    app.state.job_manager = manager
    app.state.question_service = QuestionService(settings)
    yield
    await manager.stop()
    await dispose_db()


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)

app.include_router(insight_router)
app.mount("/static", StaticFiles(directory=str(package_dir / "static")), name="static")


def manager_from(request: Request) -> JobManager:
    return request.app.state.job_manager


def qa_from(request: Request) -> QuestionService:
    return request.app.state.question_service


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    jobs = list(
        (
            await session.scalars(
                select(CrawlJob)
                .options(
                    selectinload(CrawlJob.business),
                    selectinload(CrawlJob.report),
                    selectinload(CrawlJob.source_runs),
                )
                .order_by(CrawlJob.created_at.desc())
                .limit(20)
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "default_model": settings.openai_model_default,
            "premium_model": settings.openai_model_premium,
            "default_headless": settings.headless,
            "max_reviews": settings.max_reviews,
        },
    )


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_page(
    report_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "找不到報告")
    business = await session.get(Business, report.business_id)
    report.payload = _normalize_report_payload(report.payload)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report": report,
            "business": business,
            "payload_json": json.dumps(report.payload, ensure_ascii=False),
            "default_model": settings.openai_model_default,
            "premium_model": settings.openai_model_premium,
        },
    )


@app.post("/api/businesses/search", response_model=BusinessSearchResponse)
@app.post("/api/sources/google-maps/search", response_model=BusinessSearchResponse)
async def search_businesses(
    payload: BusinessSearchRequest,
    manager: JobManager = Depends(manager_from),
) -> BusinessSearchResponse:
    try:
        candidates = await manager.search(payload.query, payload.headless)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    direct = payload.query.strip().lower().startswith(("http://", "https://"))
    return BusinessSearchResponse(candidates=candidates, direct_url=direct)


@app.get("/api/source-imports/dcard/template")
async def get_dcard_import_template(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
) -> Response:
    content, media_type = dcard_template(format)
    suffix = "json" if format == "json" else "csv"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="dcard-import-template.{suffix}"'},
    )


@app.post(
    "/api/source-imports/dcard",
    response_model=SourceImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_dcard_import(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> SourceImportResponse:
    filename = file.filename or "dcard-import"
    content = await file.read(MAX_IMPORT_BYTES + 1)
    digest = hashlib.sha256(content).hexdigest()
    try:
        rows = parse_dcard_import(filename, content, AuthorHasher(settings))
    except ValueError as exc:
        session.add(
            SourceImport(
                source="dcard",
                filename=filename[:500],
                sha256=digest,
                payload=[],
                row_count=0,
                validation_status="INVALID",
                validation_errors=[str(exc)],
            )
        )
        await session.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    source_import = SourceImport(
        source="dcard",
        filename=filename[:500],
        sha256=digest,
        payload=rows,
        row_count=len(rows),
        validation_status="VALID",
        validation_errors=[],
    )
    session.add(source_import)
    await session.commit()
    await session.refresh(source_import)
    return SourceImportResponse(
        import_id=source_import.id,
        filename=source_import.filename,
        row_count=source_import.row_count,
        sha256=source_import.sha256,
        validation_status=source_import.validation_status,
    )


@app.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: CreateJobRequest,
    manager: JobManager = Depends(manager_from),
) -> JobResponse:
    try:
        job = await manager.create_job(payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _job_response(job)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, manager: JobManager = Depends(manager_from)) -> JobResponse:
    try:
        job = await manager.get_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _job_response(job)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, manager: JobManager = Depends(manager_from)):
    async def stream():
        last_payload = ""
        while not await request.is_disconnected():
            try:
                job = await manager.get_job(job_id)
            except LookupError:
                yield 'event: error\ndata: {"error":"not_found"}\n\n'
                return
            payload = _job_response(job).model_dump_json()
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            auto_continuing = (
                job.auto_plan
                and not job.cancel_requested
                and job.collected_count > 0
                and job.last_completed_stage in {None, "COLLECTION"}
                and job.status
                in {"READY_FOR_ANALYSIS", "COLLECTION_INTERRUPTED", "BLOCKED", "FAILED"}
            )
            if job.status in STREAM_END_JOB_STATUSES and not auto_continuing:
                return
            await asyncio.sleep(0.75)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/resume", response_model=JobResponse)
async def resume_job(job_id: str, manager: JobManager = Depends(manager_from)) -> JobResponse:
    try:
        return _job_response(await manager.resume(job_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str, manager: JobManager = Depends(manager_from)) -> JobResponse:
    try:
        return _job_response(await manager.cancel(job_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str, manager: JobManager = Depends(manager_from)) -> Response:
    try:
        await manager.delete_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/jobs/{job_id}/analyze", response_model=JobResponse, status_code=202)
async def analyze_job(
    job_id: str,
    payload: StartAnalysisRequest,
    manager: JobManager = Depends(manager_from),
) -> JobResponse:
    try:
        job = await manager.start_analysis(
            job_id,
            model=payload.llm_model,
            accept_partial_collection=payload.accept_partial_collection,
        )
        return _job_response(job)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/reports/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str, session: AsyncSession = Depends(get_session)
) -> ReportResponse:
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "找不到報告")
    return _report_response(report)


@app.get("/api/reports/{report_id}/reviews", response_model=PaginatedReviews)
@app.get("/api/reports/{report_id}/items", response_model=PaginatedReviews)
async def get_report_reviews(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    sort: str = Query("original", pattern="^(original|date_desc|date_asc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> PaginatedReviews:
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "找不到報告")
    items, scope = await resolve_scope(session, report, filters)
    items = sorted_items(items, sort)
    return PaginatedReviews(items=[review_view(x) for x in items[(page-1)*page_size:page*page_size]],
                            total=len(items), page=page, page_size=page_size, scope=scope)


@app.get("/api/reports/{report_id}/export")
async def export_report(
    report_id: str,
    format: str = Query(pattern="^(csv|json)$"),
    filters: ReportFilters = Depends(report_filters),
    session: AsyncSession = Depends(get_session),
):
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "找不到報告")
    items, scope = await resolve_scope(session, report, filters)
    rows = [review_view(x).model_dump(mode="json") for x in items]
    if format == "json":
        return JSONResponse(
            {"report": _normalize_report_payload(report.payload), "items": rows, "reviews": rows, "scope": scope}
        )

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "source",
            "content_type",
            "title",
            "board",
            "thread_source_id",
            "source_url",
            "rating",
            "text",
            "relative_date",
            "sentiment",
            "confidence",
            "rating_text_conflict",
            "aspects",
            "key_points",
        ],
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        row["aspects"] = "|".join(row["aspects"])
        row["key_points"] = "|".join(row["key_points"])
        writer.writerow({k: csv_safe(v) for k, v in row.items()})
    content = "\ufeff" + output.getvalue()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{report_id}.csv"'},
    )


@app.post("/api/reports/{report_id}/questions", response_model=QuestionResponse)
async def ask_report(
    report_id: str,
    payload: QuestionRequest,
    service: QuestionService = Depends(qa_from),
) -> QuestionResponse:
    try:
        session_id, answer = await service.answer(
            report_id=report_id,
            question=payload.question,
            session_id=payload.session_id,
            model=payload.llm_model,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return QuestionResponse(
        session_id=session_id,
        answer=answer.answer,
        evidence_review_ids=answer.evidence_review_ids,
        limitations=answer.limitations,
    )


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> dict:
    await session.scalar(select(func.count(Business.id)))
    return {"status": "ok"}


def _sentiment_priority():
    """Keep the report inbox focused on complaints before praise."""
    return case(
        (ReviewAnalysis.sentiment == "negative", 0),
        (ReviewAnalysis.sentiment == "neutral", 1),
        (ReviewAnalysis.sentiment == "positive", 2),
        (ReviewAnalysis.sentiment == "rating_only", 3),
        else_=4,
    )


def _job_response(job: CrawlJob) -> JobResponse:
    report_id = job.report.id if "report" in job.__dict__ and job.report else None
    source_runs = list(job.source_runs) if "source_runs" in job.__dict__ else []
    has_incomplete_source = any(not run.collection_complete for run in source_runs)
    return JobResponse(
        auto_plan=job.auto_plan,
        id=job.id,
        business_id=job.business_id,
        subject_id=job.business_id,
        status=job.status,
        max_reviews=job.max_reviews,
        sort_order=job.sort_order,
        headless=job.headless,
        llm_model=job.llm_model,
        collected_count=job.collected_count,
        processed_count=job.processed_count,
        progress=job.progress,
        message=job.message,
        error=job.error,
        cancel_requested=job.cancel_requested,
        collection_complete=job.collection_complete,
        collection_stop_reason=job.collection_stop_reason,
        can_resume_collection=(
            job.status in {"COLLECTION_INTERRUPTED", "BLOCKED"}
            or (job.status == "READY_FOR_ANALYSIS" and not job.collection_complete)
            or (job.status == "FAILED" and has_incomplete_source)
        ),
        can_start_analysis=job.collected_count > 0
        and job.status in {"READY_FOR_ANALYSIS", "COLLECTION_INTERRUPTED", "BLOCKED", "FAILED"},
        last_completed_stage=job.last_completed_stage,
        degraded_reasons=list(job.degraded_reasons or []),
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        report_id=report_id,
        sources=[
            {
                "source": run.source,
                "status": run.status,
                "collected_count": run.collected_count,
                "post_count": run.post_count,
                "comment_count": run.comment_count,
                "target_count": run.target_count,
                "collection_complete": run.collection_complete,
                "stop_reason": run.stop_reason,
                "error": run.error,
                "attempt_count": run.attempt_count,
                "config": run.config,
            }
            for run in source_runs
        ],
    )


def _report_response(report: Report) -> ReportResponse:
    return ReportResponse(
        id=report.id,
        job_id=report.job_id,
        business_id=report.business_id,
        model_id=report.model_id,
        status=report.status,
        payload=_normalize_report_payload(report.payload),
        created_at=report.created_at,
    )


def _review_response(review: Review, analysis: ReviewAnalysis | None = None) -> ReviewResponse:
    return ReviewResponse(
        id=review.id,
        source=review.source or "google_maps",
        content_type=review.content_type or "review",
        title=review.title,
        board=review.board,
        thread_source_id=review.thread_source_id,
        source_url=review.source_url,
        platform_data=review.platform_data or {},
        rating=review.rating,
        text=review.text,
        relative_date=review.relative_date,
        published_at_estimated=review.published_at_estimated,
        date_precision=review.date_precision,
        owner_reply=review.owner_reply,
        language=review.language,
        sentiment=analysis.sentiment if analysis else None,
        confidence=analysis.confidence if analysis else None,
        rating_sentiment=analysis.rating_sentiment if analysis else None,
        rating_text_conflict=analysis.rating_text_conflict if analysis else False,
        aspects=analysis.aspects if analysis else [],
        key_points=analysis.key_points if analysis else [],
    )


def _normalize_report_payload(payload: dict) -> dict:
    if payload.get("schema_version") in {2, 3, 4}:
        return payload
    legacy = dict(payload)
    overall = {
        "sample_size": legacy.get("sample_size", 0),
        "text_review_count": legacy.get("text_review_count", 0),
        "rating_distribution": legacy.get("rating_distribution") or {},
        "sentiment_distribution": legacy.get("sentiment_distribution") or {},
        "rating_text_conflict_count": legacy.get("rating_text_conflict_count", 0),
        "aspects": legacy.get("aspects") or {},
        "monthly_trend": legacy.get("monthly_trend") or {},
        "common_praise": legacy.get("common_praise") or [],
        "common_complaints": legacy.get("common_complaints") or [],
        "representative_positive": legacy.get("representative_positive") or [],
        "representative_negative": legacy.get("representative_negative") or [],
    }
    legacy.setdefault(
        "collection",
        {
            "target_count": overall["sample_size"],
            "actual_count": overall["sample_size"],
            "complete": True,
            "stop_reason": "legacy_report",
            "sources": {},
        },
    )
    collection = dict(legacy["collection"] or {})
    collection.setdefault("complete", True)
    if not collection.get("sources"):
        collection["sources"] = {
            "google_maps": {
                "status": "COMPLETE" if collection["complete"] else "PARTIAL",
                "actual_count": collection.get("actual_count", overall["sample_size"]),
                "post_count": 0,
                "comment_count": 0,
                "complete": collection["complete"],
                "stop_reason": collection.get("stop_reason") or "legacy_report",
                "error": None,
            }
        }
    legacy["collection"] = collection
    legacy.update(overall)
    legacy.update(
        {
            "schema_version": 2,
            "subject": legacy.get("business", {}),
            "overall": overall,
            "sources": {"google_maps": overall},
            "content_types": {"review": legacy.get("sample_size", 0)},
            "platform_signals": {},
            "top_threads": [],
        }
    )
    executive = dict(legacy.get("executive") or {})
    executive.setdefault("summary", "舊版報告未提供管理摘要。")
    for key in ("strengths", "weaknesses", "risks", "recommendations"):
        executive.setdefault(key, [])
    executive.setdefault("source_differences", [])
    legacy["executive"] = executive
    return legacy
