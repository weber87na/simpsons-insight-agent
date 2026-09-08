"""Small experiment API; confirmed measurement specifications are immutable."""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .insights import TAIPEI
from .models import ValidationExperiment, ValidationResult, ValidationRun
from .validation import (
    ExperimentUpdate,
    MeasurementSpec,
    ResultInput,
    ValidationOptions,
    judge_result,
)

router = APIRouter()
mutation_lock = asyncio.Lock()


async def required(session, model, key):
    row = await session.get(model, key)
    if not row:
        raise HTTPException(404, "找不到驗證資料")
    return row


def coordinator(request):
    return request.app.state.job_manager.validations


@router.post("/api/decisions/{plan_id}/validation", status_code=202)
async def start_validation(plan_id: str, payload: ValidationOptions, request: Request):
    try:
        return {"id": await coordinator(request).start(plan_id, payload)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/decisions/{plan_id}/validation")
async def get_validation(plan_id: str, session: AsyncSession = Depends(get_session)):
    from .models import DecisionPlan

    await required(session, DecisionPlan, plan_id)
    run = await session.scalar(select(ValidationRun).where(ValidationRun.plan_id == plan_id))
    if not run:
        return {"id": None}
    experiments = (await session.scalars(select(ValidationExperiment).where(ValidationExperiment.run_id == run.id).order_by(ValidationExperiment.experiment_key))).all()
    rows = []
    for e in experiments:
        results = (await session.scalars(select(ValidationResult).where(ValidationResult.experiment_id == e.id).order_by(ValidationResult.created_at, ValidationResult.id))).all()
        rows.append({"id": e.id, "status": e.status, "approved": e.approved, **e.payload,
                     "results": [{"id": r.id, "created_at": r.created_at.isoformat(), "input": r.payload, "verdict": r.verdict} for r in results]})
    # Raw stage evidence stays in the audit record, not duplicated throughout the UI response.
    return {"id": run.id, "status": run.status, "options": run.options, "error": run.error,
            "coverage": run.payload.get("coverage"), "missing_information": run.payload.get("missing_information", []),
            "review": run.payload.get("review"), "rule_errors": run.payload.get("rule_errors", []),
            "stages": {key: {k: v for k, v in stage.items() if k not in {"input", "result", "history"}} for key, stage in run.stages.items()},
            "experiments": rows}


@router.post("/api/validations/{run_id}/cancel", status_code=202)
async def cancel_validation(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await required(session, ValidationRun, run_id)
    if run.status in {"PENDING", "RUNNING"}:
        run.cancel_requested = True
        if run.status == "PENDING":
            run.status = "CANCELED"
        await session.commit()
    return {"id": run.id, "status": run.status}


@router.post("/api/validations/{run_id}/retry", status_code=202)
async def retry_validation(run_id: str, request: Request):
    try:
        await coordinator(request).retry(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": run_id}


@router.patch("/api/validation-experiments/{experiment_id}")
async def update_experiment(experiment_id: str, payload: ExperimentUpdate, session: AsyncSession = Depends(get_session)):
    async with mutation_lock:
        e = await required(session, ValidationExperiment, experiment_id)
        if "measurement" in payload.model_fields_set and payload.measurement is None:
            raise HTTPException(422, "不可清除量測規格")
        if "status" in payload.model_fields_set and payload.status is None:
            raise HTTPException(422, "狀態不可為空")
        if payload.measurement is not None:
            if e.status != "DRAFT":
                raise HTTPException(409, "開始後鎖定實驗規格")
            e.payload = {**e.payload, "measurement": payload.measurement.model_dump(mode="json")}
        if payload.status and payload.status != e.status:
            allowed = {"DRAFT": {"RUNNING", "STOPPED"}, "RUNNING": {"COMPLETED", "STOPPED"}, "COMPLETED": set(), "STOPPED": set()}
            if payload.status not in allowed[e.status]:
                raise HTTPException(409, "不允許此狀態轉換")
            if payload.status == "RUNNING":
                if not e.approved:
                    raise HTTPException(409, "審查未通過，不可開始執行")
                if not payload.confirmed or not e.payload.get("measurement"):
                    raise HTTPException(422, "開始前需確認指標、門檻、最低樣本數及期間")
                e.payload = {**e.payload, "confirmed_at": datetime.now(TAIPEI).isoformat()}
            if payload.status == "COMPLETED":
                latest = await session.scalar(select(ValidationResult).where(ValidationResult.experiment_id == e.id).order_by(ValidationResult.created_at.desc()))
                spec = MeasurementSpec.model_validate(e.payload["measurement"])
                if latest is None or datetime.now(TAIPEI).date() <= spec.after_end:
                    raise HTTPException(409, "觀察期間結束且至少回填一次後才能完成")
            e.status = payload.status
        await session.commit()
        return {"id": e.id, "status": e.status, **e.payload}


@router.post("/api/validation-experiments/{experiment_id}/results", status_code=201)
async def add_result(experiment_id: str, payload: ResultInput, session: AsyncSession = Depends(get_session)):
    async with mutation_lock:
        e = await required(session, ValidationExperiment, experiment_id)
        if e.status == "DRAFT" or not e.payload.get("confirmed_at"):
            raise HTTPException(409, "請先確認並開始實驗")
        data = payload.model_dump(mode="json")
        old = await session.scalar(select(ValidationResult).where(ValidationResult.experiment_id == e.id, ValidationResult.submission_id == payload.submission_id))
        if old:
            if old.payload != data:
                raise HTTPException(409, "相同提交識別碼不可使用不同內容")
            return {"id": old.id, "verdict": old.verdict}
        try:
            verdict = judge_result(MeasurementSpec.model_validate(e.payload["measurement"]), payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = ValidationResult(experiment_id=e.id, submission_id=payload.submission_id, payload=data, verdict=verdict)
        session.add(result)
        await session.commit()
        return {"id": result.id, "verdict": verdict}
