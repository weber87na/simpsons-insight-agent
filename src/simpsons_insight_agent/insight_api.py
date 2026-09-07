from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import ReportFilters, report_filters, resolve_scope, scoped_trends
from .analytics_api import router as analytics_router
from .comparison_api import router as comparison_router
from .comparisons import BrandInput as ComparisonInput
from .comparisons import comparison_result
from .db import SessionLocal, get_session
from .decisions import TERMINAL, HumanReview, TaskUpdate
from .insights import snapshot_items, trends
from .models import AgentRun, BrandComparison, DecisionPlan, ImprovementTask, PlanEvaluation, Report
from .schemas import PlanningOptions

router = APIRouter()
router.include_router(analytics_router)
router.include_router(comparison_router)


async def required(session, model, key):
    value = await session.get(model, key)
    if value is None:
        raise HTTPException(404, "找不到資料")
    return value


@router.get("/api/reports/{report_id}/trends")
async def get_trends(
    report_id: str,
    filters: ReportFilters = Depends(report_filters),
    session: AsyncSession = Depends(get_session),
):
    report = await required(session, Report, report_id)
    filters = filters.model_copy(update={"precision_policy": "interval"})
    items, scope = await resolve_scope(session, report, filters)
    try:
        return scoped_trends(items, filters, scope)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/reports/{report_id}/topics")
async def get_topics(report_id: str, session: AsyncSession = Depends(get_session)):
    report = await required(session, Report, report_id)
    return {
        "topics": report.payload.get("topics", []),
        "analysis": report.payload.get(
            "topic_analysis", {"status": "legacy", "note": "舊報告沒有主題快照"}
        ),
    }


@router.get("/api/reports/{report_id}/evidence/{item_id}")
async def report_evidence(
    report_id: str, item_id: str, session: AsyncSession = Depends(get_session)
):
    report = await required(session, Report, report_id)
    item = next((x for x in await snapshot_items(session, report) if x["id"] == item_id), None)
    if item is None:
        raise HTTPException(404, "此證據不在報告快照內")
    return item


@router.post("/api/reports/{report_id}/decisions", status_code=202)
async def start_decision(report_id: str, options: PlanningOptions, request: Request):
    coordinator = request.app.state.job_manager.decisions
    try:
        return {"plan_id": await coordinator.start(report_id, options)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def plan_view(s, plan):
    tasks = (
        await s.scalars(select(ImprovementTask).where(ImprovementTask.plan_id == plan.id))
    ).all()
    evaluations = (
        await s.scalars(
            select(PlanEvaluation)
            .where(PlanEvaluation.plan_id == plan.id)
            .order_by(PlanEvaluation.created_at)
        )
    ).all()
    runs = (await s.scalars(select(AgentRun).where(AgentRun.plan_id == plan.id))).all()
    return {
        "id": plan.id,
        "report_id": plan.report_id,
        "status": plan.status,
        "error": plan.error,
        "options": plan.options,
        "diagnosis": plan.diagnosis,
        "tasks": [{"id": t.id, **t.payload} for t in tasks],
        "evaluations": [{"id": e.id, "kind": e.kind, "payload": e.payload} for e in evaluations],
        "runs": [
            {
                "stage": r.stage,
                "status": r.status,
                "attempts": r.attempts,
                "elapsed_ms": r.elapsed_ms,
                "tools": r.tools,
                "error": r.error,
                "usage": r.output.get("usage"),
                "estimated_cost": r.output.get("estimated_cost"),
            }
            for r in runs
        ],
    }


@router.get("/api/reports/{report_id}/decisions")
async def report_decision(report_id: str, session: AsyncSession = Depends(get_session)):
    await required(session, Report, report_id)
    plan = await session.scalar(select(DecisionPlan).where(DecisionPlan.report_id == report_id))
    return await plan_view(session, plan) if plan else {"status": "NOT_STARTED"}


@router.get("/api/decisions/{plan_id}")
async def get_decision(plan_id: str, session: AsyncSession = Depends(get_session)):
    return await plan_view(session, await required(session, DecisionPlan, plan_id))


@router.get("/api/decisions/{plan_id}/events")
async def decision_events(
    plan_id: str, request: Request, session: AsyncSession = Depends(get_session)
):
    await required(session, DecisionPlan, plan_id)

    async def stream():
        previous = ""
        while not await request.is_disconnected():
            async with SessionLocal() as s:
                plan = await s.get(DecisionPlan, plan_id)
                if plan is None:
                    return
                value = await plan_view(s, plan)
            rendered = json.dumps(value, ensure_ascii=False)
            if rendered != previous:
                yield f"data: {rendered}\n\n"
                previous = rendered
            else:
                yield ": heartbeat\n\n"
            if value["status"] in TERMINAL:
                return
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.post("/api/decisions/{plan_id}/cancel", status_code=202)
async def cancel_decision(plan_id: str, session: AsyncSession = Depends(get_session)):
    plan = await required(session, DecisionPlan, plan_id)
    if plan.status not in TERMINAL:
        plan.cancel_requested = True
        if plan.status == "PENDING":
            plan.status = "CANCELED"
            report = await session.get(Report, plan.report_id)
            if report:
                report.payload = {
                    **report.payload,
                    "decision": {"plan_id": plan.id, "status": "CANCELED"},
                }
        await session.commit()
    return {"status": plan.status, "cancel_requested": plan.cancel_requested}


@router.post("/api/decisions/{plan_id}/retry", status_code=202)
async def retry_decision(plan_id: str, request: Request):
    try:
        await request.app.state.job_manager.decisions.retry(plan_id)
        return {"plan_id": plan_id}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/decisions/{plan_id}/audit")
async def audit(plan_id: str, session: AsyncSession = Depends(get_session)):
    await required(session, DecisionPlan, plan_id)
    runs = (await session.scalars(select(AgentRun).where(AgentRun.plan_id == plan_id))).all()
    return [
        {
            "stage": r.stage,
            "input": r.input,
            "output": r.output,
            "error": r.error,
            "tools": r.tools,
            "elapsed_ms": r.elapsed_ms,
            "attempts": r.attempts,
        }
        for r in runs
    ]


@router.patch("/api/improvement-tasks/{task_id}")
async def update_task(
    task_id: str, payload: TaskUpdate, session: AsyncSession = Depends(get_session)
):
    task = await required(session, ImprovementTask, task_id)
    changes = payload.model_dump(mode="json", exclude_unset=True)
    if any(changes.get(k) is None for k in changes if k not in {"start_date", "due_date"}):
        raise HTTPException(422, "非日期欄位不可設為 null")
    updated = {**task.payload, **changes}
    if (
        updated.get("start_date")
        and updated.get("due_date")
        and updated["start_date"] > updated["due_date"]
    ):
        raise HTTPException(422, "工作起始日不可晚於到期日")
    if any(k in changes and changes[k] != task.payload.get(k) for k in ("start_date", "due_date")):
        updated["schedule_mode"] = "manual"
        updated["schedule_assumption"] = "手動修改日期；請人工重新確認資源容量。"
    peers = (
        await session.scalars(
            select(ImprovementTask).where(ImprovementTask.plan_id == task.plan_id)
        )
    ).all()
    for peer in peers:
        if (
            peer.task_key in updated["dependencies"]
            and peer.payload.get("due_date")
            and updated.get("start_date")
            and peer.payload["due_date"] > updated["start_date"]
        ):
            raise HTTPException(422, "開始日早於相依工作的到期日")
        if (
            task.task_key in peer.payload["dependencies"]
            and updated.get("due_date")
            and peer.payload.get("start_date")
            and updated["due_date"] > peer.payload["start_date"]
        ):
            raise HTTPException(422, "到期日晚於後續工作的開始日")
    task.payload = updated
    await session.commit()
    return {"id": task.id, **task.payload}


@router.post("/api/decisions/{plan_id}/evaluations", status_code=201)
async def human_evaluation(
    plan_id: str, payload: HumanReview, session: AsyncSession = Depends(get_session)
):
    await required(session, DecisionPlan, plan_id)
    row = PlanEvaluation(plan_id=plan_id, kind="human", payload=payload.model_dump())
    session.add(row)
    await session.commit()
    return {"id": row.id, "kind": row.kind, "payload": row.payload}


@router.get("/api/insights/reports")
async def list_reports(session: AsyncSession = Depends(get_session)):
    reports = (
        await session.scalars(select(Report).order_by(Report.created_at.desc()).limit(200))
    ).all()
    return [
        {
            "id": r.id,
            "business_id": r.business_id,
            "name": r.payload.get("business", {}).get("name", "未命名"),
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]


@router.post("/api/comparisons", status_code=201)
async def create_comparison(payload: ComparisonInput, session: AsyncSession = Depends(get_session)):
    reports = [await required(session, Report, key) for key in payload.report_ids]
    if len({r.business_id for r in reports}) != len(reports):
        raise HTTPException(422, "品牌比較請選擇不同品牌；同品牌請使用改善前後觀察")
    row = BrandComparison(name=payload.name, config={**payload.model_dump(mode="json"), "config_version": 2})
    session.add(row)
    await session.commit()
    return {"id": row.id}


@router.get("/api/comparisons/{comparison_id}")
async def get_comparison(comparison_id: str, session: AsyncSession = Depends(get_session)):
    row = await required(session, BrandComparison, comparison_id)
    return await comparison_result(session, row)


@router.get("/api/decisions/{plan_id}/outcomes")
async def outcomes(
    plan_id: str,
    after_report_id: str,
    intervention_date: date,
    session: AsyncSession = Depends(get_session),
):
    plan = await required(session, DecisionPlan, plan_id)
    original = await required(session, Report, plan.report_id)
    followup = await required(session, Report, after_report_id)
    if original.business_id != followup.business_id:
        raise HTTPException(422, "前後觀察必須是同一品牌")
    before = trends(
        await snapshot_items(session, original),
        interval="week",
        date_from=intervention_date - timedelta(days=28),
        date_to=intervention_date - timedelta(days=1),
    )
    after = trends(
        await snapshot_items(session, followup),
        interval="week",
        date_from=intervention_date,
        date_to=intervention_date + timedelta(days=27),
    )
    return {
        "before": before,
        "after": after,
        "note": "前後各 28 日僅觀察樣本變化，需核對來源、蒐集完整度及其他事件；不推定因果。",
    }


@router.get("/comparisons", response_class=HTMLResponse)
async def comparison_page():
    return HTMLResponse(
        (Path(__file__).parent / "templates" / "comparisons.html").read_text(encoding="utf-8")
    )
