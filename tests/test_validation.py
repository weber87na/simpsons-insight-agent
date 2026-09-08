"""Isolated validation flow, evidence gates, API invariants and descriptive outcomes."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, select
from test_insight_decisions import FakeCloud, item, run_plan
from test_insight_decisions import report_factory as report_factory

from simpsons_insight_agent.api import app
from simpsons_insight_agent.db import SessionLocal
from simpsons_insight_agent.models import (
    DecisionPlan,
    ValidationExperiment,
    ValidationResult,
    ValidationRun,
)
from simpsons_insight_agent.validation import (
    ExperimentDesign,
    ExperimentReview,
    MeasurementSpec,
    Observation,
    ResultInput,
    ValidationCoordinator,
    ValidationOptions,
    judge_result,
    select_evidence,
)


def measurement(**changes):
    return MeasurementSpec.model_validate({
        "metric": "申請完成率", "metric_kind": "ratio", "unit": "%", "direction": "increase",
        "threshold": 10, "minimum_sample": 10, "before_start": "2026-07-01",
        "before_end": "2026-07-07", "after_start": "2026-07-08", "after_end": "2026-07-14", **changes,
    })


def result(**changes):
    return ResultInput.model_validate({"submission_id": "first", "before": {"count": 10, "successes": 6}, "after": {"count": 10, "successes": 7}, "comparable": True, **changes})


@pytest.mark.parametrize(("changes", "today", "expected"), [
    ({}, date(2026, 7, 15), "MET"),
    ({"after": {"count": 10, "successes": 6}}, date(2026, 7, 15), "NOT_MET"),
    ({"after": {}}, date(2026, 7, 15), "INSUFFICIENT"),
    ({"after": {"count": 2, "successes": 2}, "comparable": False}, date(2026, 7, 14), "INSUFFICIENT"),
    ({"comparable": False}, date(2026, 7, 14), "OBSERVING"),
    ({"comparable": False}, date(2026, 7, 15), "REVIEW"),
    ({"confounders": ["期末申請高峰"]}, date(2026, 7, 15), "REVIEW"),
])
def test_verdict_order_and_percentage_points(changes, today, expected):
    verdict = judge_result(measurement(), result(**changes), today)
    assert verdict["verdict"] == expected
    assert "不代表因果" in verdict["note"]
    if expected == "MET":
        assert verdict["delta"] == 10


def test_mean_and_invalid_measurements():
    spec = measurement(metric_kind="mean", metric="等待", unit="分鐘", direction="decrease", threshold=2)
    data = result(before={"count": 10, "mean": 5.1}, after={"count": 10, "mean": 3.1})
    assert judge_result(spec, data, date(2026, 7, 15))["verdict"] == "MET"
    for raw in [{"count": 0}, {"count": -1}, {"count": 1.1}, {"count": 2, "successes": 3}, {"mean": float("nan")}]:
        with pytest.raises(ValidationError):
            Observation.model_validate(raw)
    for fields in [{"after_start": "2026-07-07"}, {"after_end": "2026-06-01"}, {"threshold": 101}, {"threshold": 0}]:
        with pytest.raises(ValidationError):
            measurement(**fields)
    with pytest.raises(ValueError):
        judge_result(spec, result(), date(2026, 7, 15))


def test_experiment_limit_and_strict_fields():
    with pytest.raises(ValidationError):
        ExperimentDesign.model_validate({"experiments": [{}, {}, {}, {}], "missing_information": []})
    with pytest.raises(ValidationError):
        ResultInput.model_validate({**result().model_dump(), "verdict": "MET"})


class ValidationCloud:
    available = True

    def __init__(self, reject=False, invalid=False, fail=False, empty=False):
        self.reject, self.invalid, self.fail, self.empty = reject, invalid, fail, empty
        self.calls = []

    async def decision_generate(self, payload, model, schema, instruction):
        self.calls.append(schema.__name__)
        if schema is ExperimentReview:
            if self.fail:
                self.fail = False
                raise RuntimeError("model unavailable")
            return ExperimentReview(approved=not self.reject, reasons=["合成審查：可量測" if not self.reject else "資源不可行"]), {"input_tokens": 4}
        p = payload["problems"][0]
        support = p["evidence_ids"][0]
        assert support.startswith("e")
        counter = [x["id"] for x in payload["evidence"] if x["id"] != support][:1]
        if self.invalid:
            counter = ["cross-report-id"]
        spec = {"key": "exp1", "problem_key": p["key"], "title": "合成案例：試用申請指引", "journey_stage": payload["journey_stages"][1],
                "hypothesis": "資訊不清楚可能增加重複詢問", "alternative_explanations": ["申請高峰也可能造成等候"],
                "support_evidence_ids": [support], "counter_evidence_ids": counter, "counter_note": "反證僅代表其他經驗，不證明根因",
                "steps": ["試用一頁申請說明並記錄完成情況"], "owner_role": "服務窗口", "hours": 2, "cost_estimate": "使用既有文件",
                "metric": "申請完成率", "metric_kind": "ratio", "unit": "%", "observation_days": 7, "stop_conditions": ["說明造成誤解時停止"]}
        return ExperimentDesign(experiments=[] if self.empty else [spec], missing_information=["請補充流程資料"] if self.empty else []), {"input_tokens": 8}


async def create_validation(report, cloud=None, context="campus"):
    await run_plan(report, FakeCloud())
    async with SessionLocal() as s:
        plan = await s.scalar(select(DecisionPlan).where(DecisionPlan.report_id == report.id))
    queue = asyncio.Queue()
    coordinator = ValidationCoordinator(cloud or ValidationCloud(), queue)
    key = await coordinator.start(plan.id, ValidationOptions(context=context, start_date=date(2026, 7, 8), weekly_hours=7))
    return coordinator, plan, key


def test_evidence_round_robin_and_whitelist():
    rows = [item(str(i), aspects=["service"], source="ptt" if i % 2 else "dcard", text="文字" * 500, source_url="https://example.org", author="private") for i in range(90)]
    rows += [item("counter", sentiment="positive", aspects=["service"], source="google_maps")]
    problems = [{"key": "p", "evidence_ids": ["89"]}]
    selected, coverage = select_evidence(rows, problems)
    assert len(selected) == 60 and selected[0]["id"] == "89"
    assert "counter" in {x["id"] for x in selected}
    assert all(len(x["text"]) == 800 or x["id"] == "counter" for x in selected)
    assert all("author" not in x and "source_url" not in x for x in selected)
    assert coverage["excluded"] == 31
    assert select_evidence(list(reversed(rows)), problems) == (selected, coverage)


@pytest.mark.asyncio
async def test_flow_api_lock_results_history_and_cascade(report_factory, monkeypatch):
    report = await report_factory(rows=[item(), item("counter", sentiment="positive")])
    coordinator, plan, key = await create_validation(report)
    monkeypatch.setattr(app.state, "job_manager", SimpleNamespace(validations=coordinator), raising=False)
    assert await coordinator.start(plan.id, ValidationOptions(context="business")) == key
    await coordinator.run(key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        data = (await client.get(f"/api/decisions/{plan.id}/validation")).json()
        assert data["status"] == "COMPLETED" and data["options"]["context"] == "campus"
        e = data["experiments"][0]
        url = f"/api/validation-experiments/{e['id']}"
        assert (await client.post(url + "/results", json=result().model_dump(mode="json"))).status_code == 409
        assert (await client.patch(url, json={"status": "RUNNING"})).status_code == 422
        started = await client.patch(url, json={"measurement": measurement().model_dump(mode="json"), "status": "RUNNING", "confirmed": True})
        assert started.status_code == 200
        assert (await client.patch(url, json={"measurement": measurement(threshold=20).model_dump(mode="json")})).status_code == 409
        assert (await client.patch(url, json={"status": "DRAFT"})).status_code == 409
        first = await client.post(url + "/results", json=result().model_dump(mode="json"))
        assert first.status_code == 201 and first.json()["verdict"]["verdict"] == "MET"
        duplicate = await client.post(url + "/results", json=result().model_dump(mode="json"))
        assert duplicate.json()["id"] == first.json()["id"]
        conflict = await client.post(url + "/results", json=result(notes="changed").model_dump(mode="json"))
        assert conflict.status_code == 409
        await client.post(url + "/results", json=result(submission_id="second", comparable=False).model_dump(mode="json"))
        assert (await client.patch(url, json={"status": "COMPLETED"})).status_code == 200
        data = (await client.get(f"/api/decisions/{plan.id}/validation")).json()
        assert len(data["experiments"][0]["results"]) == 2
    async with SessionLocal() as s:
        original = await s.get(DecisionPlan, plan.id)
        assert original.diagnosis == plan.diagnosis
        await s.execute(delete(DecisionPlan).where(DecisionPlan.id == plan.id))
        await s.commit()
        assert await s.get(ValidationRun, key) is None
        assert await s.get(ValidationExperiment, e["id"]) is None
        assert await s.get(ValidationResult, first.json()["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reject", "invalid", "empty"])
async def test_rejected_invalid_and_empty_never_executable(report_factory, mode):
    report = await report_factory()
    cloud = ValidationCloud(**{mode: True})
    coordinator, plan, key = await create_validation(report, cloud)
    await coordinator.run(key)
    async with SessionLocal() as s:
        run = await s.get(ValidationRun, key)
        experiments = (await s.scalars(select(ValidationExperiment).where(ValidationExperiment.run_id == key))).all()
        assert all(not e.approved for e in experiments)
        if mode == "invalid":
            assert run.status == "PARTIAL" and not experiments
        else:
            assert run.status == "NEEDS_REVIEW"
        if mode == "reject":
            assert len(cloud.calls) == 4 and len(experiments) == 1
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.patch(f"/api/validation-experiments/{experiments[0].id}", json={"measurement": measurement().model_dump(mode="json"), "status": "RUNNING", "confirmed": True})
                assert response.status_code == 409


@pytest.mark.asyncio
async def test_retry_keeps_design_and_recovery_does_not_duplicate(report_factory):
    report = await report_factory()
    cloud = ValidationCloud(fail=True)
    coordinator, plan, key = await create_validation(report, cloud, "business")
    await coordinator.run(key)
    await coordinator.retry(key)
    await coordinator.recover()
    await coordinator.run(key)
    await coordinator.run(key)
    async with SessionLocal() as s:
        run = await s.get(ValidationRun, key)
        assert run.status == "COMPLETED", run.payload.get("rule_errors")
        assert cloud.calls == ["ExperimentDesign", "ExperimentReview", "ExperimentReview"]
        assert run.stages["review_0"]["attempts"] == 2
        assert len((await s.scalars(select(ValidationExperiment).where(ValidationExperiment.run_id == key))).all()) == 1


@pytest.mark.asyncio
async def test_cancel_during_model_and_shutdown_recovery(report_factory):
    report = await report_factory()
    entered = asyncio.Event()

    class SlowCloud(ValidationCloud):
        async def decision_generate(self, *args):
            entered.set()
            await asyncio.Future()

    coordinator, plan, key = await create_validation(report, SlowCloud())
    task = asyncio.create_task(coordinator.run(key))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with SessionLocal() as s:
        run = await s.get(ValidationRun, key)
        assert run.status == "RUNNING" and run.stages["design_0"]["status"] == "INTERRUPTED"
    entered.clear()
    await coordinator.recover()
    task = asyncio.create_task(coordinator.run(key))
    await asyncio.wait_for(entered.wait(), 5)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/validations/{key}/cancel")).status_code == 202
    await asyncio.wait_for(task, 5)
    coordinator.cloud = ValidationCloud()
    await coordinator.retry(key)
    await coordinator.run(key)
    async with SessionLocal() as s:
        run = await s.get(ValidationRun, key)
        assert run.status == "COMPLETED", run.payload.get("rule_errors")


@pytest.mark.asyncio
async def test_no_key_and_invalid_parent(report_factory):
    report = await report_factory()
    cloud = ValidationCloud()
    cloud.available = False
    coordinator, plan, key = await create_validation(report, cloud)
    await coordinator.run(key)
    async with SessionLocal() as s:
        assert (await s.get(ValidationRun, key)).status == "PARTIAL"
    other = await report_factory()
    await run_plan(other, FakeCloud())
    async with SessionLocal() as s:
        p = await s.scalar(select(DecisionPlan).where(DecisionPlan.report_id == other.id))
        p.diagnosis = {"problems": [{"key": "p", "evidence_ids": ["foreign-id"]}]}
        await s.commit()
    with pytest.raises(ValueError):
        await coordinator.start(p.id, ValidationOptions())
