from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import ReportFilters, csv_safe, report_filters, review_view, sorted_items
from .comparisons import BrandInput, TopicInput, comparison_result, member_evidence
from .db import get_session
from .models import BrandComparison, Report

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def required_comparison(session, key):
    row = await session.get(BrandComparison, key)
    if row is None:
        raise HTTPException(404, "找不到比較設定")
    return row


@router.post("/api/reports/{report_id}/topic-comparisons", status_code=201)
async def create_topics(
    report_id: str, payload: TopicInput, session: AsyncSession = Depends(get_session)
):
    if await session.get(Report, report_id) is None:
        raise HTTPException(404, "找不到報告")
    row = BrandComparison(
        name=payload.name,
        config={**payload.model_dump(mode="json"), "report_id": report_id, "config_version": 2},
    )
    session.add(row)
    await session.commit()
    return {"id": row.id}


@router.get("/api/reports/{report_id}/topic-comparisons")
async def list_topics(report_id: str, session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(BrandComparison))).all()
    return [
        {"id": r.id, "name": r.name}
        for r in rows
        if r.config.get("mode") == "topics" and r.config.get("report_id") == report_id
    ]


@router.patch("/api/comparisons/{comparison_id}")
async def update_comparison(
    comparison_id: str,
    payload: TopicInput | BrandInput,
    session: AsyncSession = Depends(get_session),
):
    row = await required_comparison(session, comparison_id)
    if payload.mode != row.config.get("mode", "brands"):
        raise HTTPException(422, "不能改變比較模式")
    config = payload.model_dump(mode="json")
    if payload.mode == "topics":
        config["report_id"] = row.config["report_id"]
    else:
        # Preserve references to deleted reports when editing an existing comparison.
        reports = [await session.get(Report, key) for key in payload.report_ids]
        if any(
            r is None and key not in row.config["report_ids"]
            for key, r in zip(payload.report_ids, reports, strict=True)
        ):
            raise HTTPException(404, "新增的報告不存在")
        available = [r for r in reports if r is not None]
        if len({r.business_id for r in available}) != len(available):
            raise HTTPException(422, "品牌比較需選不同品牌")
    row.config = {**config, "config_version": 2}
    row.name = payload.name
    await session.commit()
    return {"id": row.id}


@router.get("/api/comparisons/{comparison_id}/members/{member_id}/items")
async def comparison_items(
    comparison_id: str,
    member_id: str,
    filters: ReportFilters = Depends(report_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("original", pattern="^(original|date_desc|date_asc)$"),
    format: str | None = Query(None, pattern="^csv$"),
    session: AsyncSession = Depends(get_session),
):
    row = await required_comparison(session, comparison_id)
    report, items, scope = await member_evidence(session, row, member_id, filters)
    items = sorted_items(items, sort)
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "id",
                "source",
                "title",
                "text",
                "board",
                "published_at_estimated",
                "date_precision",
                "sentiment",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for item in items:
            writer.writerow(
                {k: csv_safe(v) for k, v in review_view(item).model_dump(mode="json").items()}
            )
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="comparison-evidence.csv"'},
        )
    return {
        "items": [
            review_view(x).model_dump(mode="json")
            for x in items[(page - 1) * page_size : page * page_size]
        ],
        "total": len(items),
        "page": page,
        "page_size": page_size,
        "scope": scope,
        "report_id": report.id,
    }


@router.get("/api/comparisons/{comparison_id}/export")
async def export_comparison(
    comparison_id: str,
    format: str = Query("csv", pattern="^csv$"),
    session: AsyncSession = Depends(get_session),
):
    result = await comparison_result(session, await required_comparison(session, comparison_id))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["comparison", "member", "report_id", "period", "metric", "value", "scope_key"])
    for group in result["groups"]:
        for point in group["trends"]["points"]:
            for metric in (
                "count",
                "positive_count",
                "neutral_count",
                "negative_count",
                "unknown_count",
                "rating_only_count",
                "classified_count",
                "negative_ratio",
                "pn_ratio",
            ):
                writer.writerow(
                    [
                        csv_safe(result["name"]),
                        csv_safe(group["name"]),
                        group["report_id"],
                        point["period"],
                        metric,
                        point[metric],
                        group["scope"]["scope_key"],
                    ]
                )
            for source, count in point["source_counts"].items():
                writer.writerow(
                    [
                        csv_safe(result["name"]),
                        csv_safe(group["name"]),
                        group["report_id"],
                        point["period"],
                        f"source:{source}",
                        count,
                        group["scope"]["scope_key"],
                    ]
                )
    if result["mode"] == "topics":
        for metric in ("union_count", "overlap_count"):
            writer.writerow(
                [
                    csv_safe(result["name"]),
                    "全部選取議題",
                    "",
                    "全部期間",
                    metric,
                    result[metric],
                    "",
                ]
            )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="comparison-{comparison_id}.csv"'},
    )


@router.get("/comparisons/{comparison_id}/print")
async def print_comparison(
    request: Request, comparison_id: str, session: AsyncSession = Depends(get_session)
):
    result = await comparison_result(session, await required_comparison(session, comparison_id))
    return templates.TemplateResponse(
        request=request, name="comparison_print.html", context={"data": jsonable_encoder(result)}
    )
