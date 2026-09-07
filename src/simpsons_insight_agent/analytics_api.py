from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import (
    TOKENIZER_VERSION,
    ReportFilters,
    brand_terms,
    channel_rows,
    csv_safe,
    keyword_rows,
    report_filters,
    resolve_scope,
    scoped_trends,
    sorted_threads,
    statistics,
)
from .db import get_session
from .models import DecisionPlan, ImprovementTask, Report

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def report_scope(report_id, session, filters):
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "找不到報告")
    items, scope = await resolve_scope(session, report, filters)
    return report, items, scope


@router.get("/api/reports/{report_id}/summary")
async def summary(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    session: AsyncSession = Depends(get_session),
):
    _, items, scope = await report_scope(report_id, session, filters)
    return {**statistics(items), "scope": scope}


@router.get("/api/reports/{report_id}/channels")
async def channels(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    group_by: str = Query("channel", pattern="^(source|channel)$"),
    sort: str = Query("sample_count", pattern="^(sample_count|negative_count|negative_ratio)$"),
    session: AsyncSession = Depends(get_session),
):
    _, items, scope = await report_scope(report_id, session, filters)
    return {
        "items": channel_rows(items, group_by, sort),
        "scope": scope,
        "group_by": group_by,
        "sort": sort,
    }


@router.get("/api/reports/{report_id}/threads")
async def threads(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    sort: str = Query(
        "collected_count", pattern="^(collected_count|reported_reply_count|latest_date)$"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    _, items, scope = await report_scope(report_id, session, filters)
    rows = sorted_threads(items, sort)
    return {
        "items": rows[(page - 1) * page_size : page * page_size],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "scope": scope,
    }


@router.get("/api/reports/{report_id}/keywords")
async def keywords(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    metric: str = Query("term_frequency", pattern="^(term_frequency|document_frequency)$"),
    limit: int = Query(30, ge=1, le=100),
    show_brand: bool = True,
    session: AsyncSession = Depends(get_session),
):
    report, items, scope = await report_scope(report_id, session, filters)
    return {
        "items": keyword_rows(items, brand_terms(report), metric, limit, show_brand),
        "tokenizer_version": TOKENIZER_VERSION,
        "scope": scope,
    }


@router.get("/api/reports/{report_id}/trends/export")
async def trend_export(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    format: str = Query("csv", pattern="^csv$"),
    session: AsyncSession = Depends(get_session),
):
    filters = filters.model_copy(update={"precision_policy": "interval"})
    _, items, scope = await report_scope(report_id, session, filters)
    result = scoped_trends(items, filters, scope)
    output = io.StringIO()
    fields = [
        "period",
        "period_end",
        "count",
        "classified_count",
        "positive_count",
        "neutral_count",
        "negative_count",
        "rating_only_count",
        "unknown_count",
        "negative_ratio",
        "pn_ratio",
        "scope_key",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for point in result["points"]:
        writer.writerow(
            {k: csv_safe(v) for k, v in {**point, "scope_key": scope["scope_key"]}.items()}
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="trends-{report_id}.csv"'},
    )


@router.get("/reports/{report_id}/print")
async def print_report(
    request: Request,
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    session: AsyncSession = Depends(get_session),
):
    filters = filters.model_copy(update={"precision_policy": "interval"})
    report, items, scope = await report_scope(report_id, session, filters)
    plan = await session.scalar(select(DecisionPlan).where(DecisionPlan.report_id == report_id))
    tasks = (
        list(
            (
                await session.scalars(
                    select(ImprovementTask).where(ImprovementTask.plan_id == plan.id)
                )
            ).all()
        )
        if plan
        else []
    )
    return templates.TemplateResponse(
        request=request,
        name="analytics_print.html",
        context={
            "report": report,
            "scope": scope,
            "summary": statistics(items),
            "trend": scoped_trends(items, filters, scope),
            "channels": channel_rows(items)[:10],
            "keywords": keyword_rows(items, brand_terms(report), limit=10),
            "tasks": tasks,
        },
    )
