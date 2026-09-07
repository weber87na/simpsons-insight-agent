from __future__ import annotations

import asyncio
from datetime import date
from uuid import uuid4

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from simpsons_insight_agent.api import app
from simpsons_insight_agent.db import SessionLocal
from simpsons_insight_agent.decisions import (
    Action,
    DecisionCoordinator,
    Diagnosis,
    ExpertReview,
    Proposal,
    schedule_actions,
)
from simpsons_insight_agent.insights import cluster_topics, enrich_report, trends
from simpsons_insight_agent.models import (
    AgentRun,
    Business,
    CrawlJob,
    DecisionPlan,
    ImprovementTask,
    PlanEvaluation,
    Report,
)
from simpsons_insight_agent.reporting import build_aggregate, deterministic_summary
from simpsons_insight_agent.schemas import PlanningOptions


def item(key="r1", **values):
    return {
        "id": key,
        "text": "等候太久",
        "source": "ptt",
        "content_type": "comment",
        "sentiment": "negative",
        "aspects": ["speed_wait"],
        "key_points": ["等候時間"],
        "published_at": "2026-08-02T16:00:00+00:00",
        "date_precision": "day",
        "topic_keys": [],
        **values,
    }


def action(key="a", dependencies=None, **values):
    return Action(
        key=key,
        problem_key="p",
        title="量測尖峰出餐",
        evidence_ids=["r1"],
        steps=["記錄十筆出餐時間"],
        owner_role="店長",
        hours=7,
        cost_estimate="工時成本待估",
        prerequisites=[],
        assumptions=[],
        metric="出餐分鐘數",
        target="中位數小於 10 分鐘",
        verification="比較連續七日紀錄",
        risks=["需維持原服務品質"],
        priority=1,
        dependencies=dependencies or [],
        **values,
    ).model_dump()


def test_trend_timezone_precision_empty_bins_and_dedup():
    rows = [
        item(),
        item(),
        item("r2", date_precision="month"),
        item("r3", published_at=None),
        item("r4", sentiment="rating_only"),
    ]
    result = trends(rows, interval="week", date_from="2026-08-03", date_to="2026-08-16")
    assert result["included_count"] == 2
    assert result["excluded_count"] == 2
    assert result["points"][0]["period"] == "2026-08-03"
    assert result["points"][0]["negative_ratio"] == 1
    assert result["points"][1]["count"] == 0
    assert result["points"][1]["negative_ratio"] is None
    assert trends(rows, interval="month")["included_count"] == 3


def test_trend_filters_and_range_validation():
    rows = [item(topic_keys=["t"]), item("r2", source="dcard")]
    assert trends(rows, topic_key="t")["included_count"] == 1
    assert trends(rows, source="google_maps")["points"] == []
    with pytest.raises(ValueError):
        trends(rows, date_from="2026-09-01", date_to="2026-08-01")
    with pytest.raises(ValueError):
        trends(rows, date_from="1900-01-01", date_to="2026-08-01")


def test_semantic_topics_stable_mapping_and_ambiguity():
    rows = [
        item(),
        item("r2"),
        item("r3"),
        item("r4", sentiment="positive", negative_aspects=["speed_wait"]),
    ]
    vectors = {"r1": np.array([1.0, 0.0]), "r2": np.array([0.99, 0.01]), "r4": np.array([1.0, 0.0])}
    first, missing = cluster_topics(rows, vectors, [], "model")
    assert len(first) == 1 and first[0]["count"] == 3 and missing == 1
    second, _ = cluster_topics(rows, vectors, first, "model")
    assert second[0]["topic_key"] == first[0]["topic_key"]
    old = [{**first[0], "topic_key": "other"}, first[0]]
    ambiguous, _ = cluster_topics(rows, vectors, old, "model")
    assert ambiguous[0]["mapping"] == "new"
    changed, _ = cluster_topics(rows, vectors, first, "different-model")
    assert changed[0]["mapping"] == "new"


def test_scheduler_capacity_dependencies_and_drafts():
    rows = schedule_actions(
        [action("b", ["a"]), action()], {"start_date": "2026-08-03", "weekly_hours": 7}
    )
    assert [r["key"] for r in rows] == ["a", "b"]
    assert rows[0]["due_date"] == "2026-08-09"
    assert rows[1]["start_date"] == "2026-08-10"
    assert rows[1]["start_week"] == 2
    assert schedule_actions([action()], {})[0]["start_date"] is None
    assert schedule_actions([action()], {})[0]["schedule_mode"] == "relative_draft"
    with pytest.raises(ValueError):
        schedule_actions([action("a", ["b"]), action("b", ["a"])], {})
    with pytest.raises(ValueError):
        schedule_actions([action("a", ["missing"])], {})


@pytest.fixture
async def report_factory():
    businesses = []

    async def create(rows=None, business_id=None):
        async with SessionLocal() as s:
            if not business_id:
                business = Business(name="合成示範 " + str(uuid4()))
                s.add(business)
                await s.flush()
                business_id = business.id
                businesses.append(business.id)
            job = CrawlJob(business_id=business_id, llm_model="test", status="COMPLETED")
            s.add(job)
            await s.flush()
            rows = rows if rows is not None else [item()]
            aggregate = build_aggregate(business={"name": "合成品牌"}, reviews=rows, model_id=None)
            aggregate.update(
                schema_version=3,
                analytics_items=rows,
                topics=[],
                collection={
                    "complete": False,
                    "sources": {"ptt": {"status": "PARTIAL", "actual_count": len(rows)}},
                },
                generated_at="2026-08-03",
                item_ids=[r["id"] for r in rows],
            )
            aggregate["executive"] = deterministic_summary(aggregate)
            report = Report(job_id=job.id, business_id=business_id, payload=aggregate)
            s.add(report)
            await s.commit()
            return report

    yield create
    async with SessionLocal() as s:
        await s.execute(delete(Business).where(Business.id.in_(businesses)))
        await s.commit()


class FakeCloud:
    available = True

    def __init__(self, fail_expert=False, reject=False, invalid=False):
        self.calls = []
        self.fail_expert, self.reject, self.invalid = fail_expert, reject, invalid

    async def decision_generate(self, payload, model, schema, instruction):
        self.calls.append(schema.__name__)
        if schema is Diagnosis:
            result = Diagnosis(
                problems=[
                    {
                        "key": "p",
                        "problem": "等候時間",
                        "root_cause_hypothesis": "出餐流程可能不順",
                        "unknowns": [],
                        "evidence_ids": [
                            "invented" if self.invalid else payload["evidence"][0]["id"]
                        ],
                    }
                ]
            )
        elif schema is Proposal:
            result = Proposal(
                actions=[
                    Action.model_validate(
                        {
                            **action(),
                            "evidence_ids": payload["diagnosis"]["problems"][0]["evidence_ids"],
                        }
                    )
                ]
            )
        else:
            if self.fail_expert:
                self.fail_expert = False
                raise RuntimeError("temporary")
            result = ExpertReview(
                evidence=4,
                feasibility=4,
                resources=4,
                measurability=4,
                risk=4,
                reasons=["具備量測步驟"],
                revisions=["缺少條件"] if self.reject else [],
            )
        return result, {"input_tokens": 10, "output_tokens": 20}


async def run_plan(report, cloud, options=None):
    queue = asyncio.Queue()
    coordinator = DecisionCoordinator(cloud, queue)
    plan_id = await coordinator.start(
        report.id, options or PlanningOptions(start_date=date(2026, 8, 3), weekly_hours=7)
    )
    await coordinator.run(plan_id)
    return coordinator, plan_id


async def test_decision_checkpoints_retry_and_idempotency(report_factory):
    report = await report_factory()
    cloud = FakeCloud(fail_expert=True)
    coordinator, key = await run_plan(report, cloud)
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "PARTIAL"
    await coordinator.retry(key)
    await coordinator.run(key)
    assert cloud.calls.count("Diagnosis") == 1
    assert cloud.calls.count("Proposal") == 1
    assert await coordinator.start(report.id, PlanningOptions()) == key
    await coordinator.run(key)
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "COMPLETED"
        tasks = (
            await s.scalars(select(ImprovementTask).where(ImprovementTask.plan_id == key))
        ).all()
        assert len(tasks) == 1
        runs = (await s.scalars(select(AgentRun).where(AgentRun.plan_id == key))).all()
        assert all(r.status == "COMPLETED" for r in runs)
        assert next(r for r in runs if r.stage == "expert_0").attempts == 2


async def test_revision_bound_and_human_review(report_factory):
    report = await report_factory()
    cloud = FakeCloud(reject=True)
    _, key = await run_plan(report, cloud, PlanningOptions())
    assert cloud.calls.count("Proposal") == 3
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "NEEDS_REVIEW"
        task = await s.scalar(select(ImprovementTask).where(ImprovementTask.plan_id == key))
        assert task.payload["schedule_mode"] == "relative_draft"
        assert (
            len(
                (await s.scalars(select(PlanEvaluation).where(PlanEvaluation.plan_id == key))).all()
            )
            == 3
        )


async def test_invalid_evidence_never_creates_tasks_and_can_retry(report_factory):
    report = await report_factory()
    cloud = FakeCloud(invalid=True)
    coordinator, key = await run_plan(report, cloud)
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "PARTIAL"
        assert await s.scalar(select(ImprovementTask).where(ImprovementTask.plan_id == key)) is None
    cloud.invalid = False
    await coordinator.retry(key)
    await coordinator.run(key)
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "COMPLETED"


async def test_no_key_keeps_statistics(report_factory):
    report = await report_factory()
    cloud = FakeCloud()
    cloud.available = False
    _, key = await run_plan(report, cloud)
    assert not cloud.calls
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "PARTIAL"
        assert (await s.get(Report, report.id)).payload["sample_size"] == 1


async def test_cancel_during_model_request(report_factory):
    report = await report_factory()
    entered = asyncio.Event()

    class SlowCloud(FakeCloud):
        async def decision_generate(self, *args):
            entered.set()
            await asyncio.Event().wait()

    coordinator = DecisionCoordinator(SlowCloud(), asyncio.Queue())
    key = await coordinator.start(report.id, PlanningOptions())
    running = asyncio.create_task(coordinator.run(key))
    await asyncio.wait_for(entered.wait(), 3)
    async with SessionLocal() as s:
        plan = await s.get(DecisionPlan, key)
        plan.cancel_requested = True
        await s.commit()
    await asyncio.wait_for(running, 3)
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "CANCELED"
        assert await s.scalar(select(ImprovementTask).where(ImprovementTask.plan_id == key)) is None


async def test_api_plan_edit_review_and_comparison(report_factory):
    report = await report_factory()
    other = await report_factory([item("r2", source="dcard")])
    _, key = await run_plan(report, FakeCloud())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        page = await c.get(f"/reports/{report.id}")
        assert page.status_code == 200 and "主題與時間趨勢" in page.text
        assert (await c.get("/comparisons")).status_code == 200
        state = (await c.get(f"/api/decisions/{key}")).json()
        task = state["tasks"][0]
        invalid = await c.patch(
            f"/api/improvement-tasks/{task['id']}",
            json={"start_date": "2026-09-10", "due_date": "2026-09-01"},
        )
        assert invalid.status_code == 422
        saved = await c.patch(
            f"/api/improvement-tasks/{task['id']}",
            json={"status": "DONE", "actual_results": "完成 10 筆量測", "start_date": task["start_date"], "due_date": task["due_date"]},
        )
        assert saved.json()["actual_results"] == "完成 10 筆量測"
        assert saved.json()["schedule_mode"] == "dated"
        review = await c.post(
            f"/api/decisions/{key}/evaluations",
            json={
                "reviewer": "測試專家",
                "evidence": 4,
                "feasibility": 4,
                "resources": 3,
                "measurability": 4,
                "risk": 3,
                "reasons": ["可試行"],
                "revisions": [],
                "corrections": "先建立基準",
            },
        )
        assert review.status_code == 201
        state = (await c.get(f"/api/decisions/{key}")).json()
        assert {e["kind"] for e in state["evaluations"]} == {"ai", "human"}
        comparison = await c.post(
            "/api/comparisons",
            json={
                "name": "標竿",
                "report_ids": [report.id, other.id],
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
        )
        assert comparison.status_code == 201
        results = (await c.get("/api/comparisons/" + comparison.json()["id"])).json()
        assert len(results["brands"]) == 2
        assert any("不可直接比較" in w for w in results["warnings"])
        assert (
            await c.get(f"/api/reports/{report.id}/trends?date_from=2026-09-01&date_to=2026-08-01")
        ).status_code == 422
        assert (
            await c.get(
                f"/api/decisions/{key}/outcomes?after_report_id={other.id}&intervention_date=2026-08-03"
            )
        ).status_code == 422


async def test_old_report_read_does_not_rewrite_payload(report_factory):
    report = await report_factory()
    async with SessionLocal() as s:
        row = await s.get(Report, report.id)
        row.payload = {**row.payload, "schema_version": 2}
        original = dict(row.payload)
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get(f"/api/reports/{report.id}")).status_code == 200
        assert (await c.get(f"/api/reports/{report.id}/trends")).status_code == 200
    async with SessionLocal() as s:
        assert (await s.get(Report, report.id)).payload == original


async def test_snapshot_enrichment_is_idempotent(report_factory):
    report = await report_factory()
    async with SessionLocal() as s:
        row = await s.get(Report, report.id)
        original = dict(row.payload)
        await enrich_report(s, row, "not-downloaded")
        assert row.payload == original


async def test_shutdown_checkpoint_recovery(report_factory):
    report = await report_factory()
    entered = asyncio.Event()

    class SlowExpert(FakeCloud):
        async def decision_generate(self, payload, model, schema, instruction):
            if schema is ExpertReview:
                entered.set()
                await asyncio.Event().wait()
            return await super().decision_generate(payload, model, schema, instruction)

    old = DecisionCoordinator(SlowExpert(), asyncio.Queue())
    key = await old.start(report.id, PlanningOptions(start_date=date(2026, 8, 3), weekly_hours=7))
    running = asyncio.create_task(old.run(key))
    await asyncio.wait_for(entered.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    cloud = FakeCloud()
    queue = asyncio.Queue()
    new = DecisionCoordinator(cloud, queue)
    await new.recover()
    queued = [queue.get_nowait() for _ in range(queue.qsize())]
    assert "decision:" + key in queued
    await new.run(key)
    assert cloud.calls == ["ExpertReview"]
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "COMPLETED"


async def test_opaque_ids_are_remapped_and_evidence_is_report_scoped(report_factory):
    opaque_id = "12345678-1234-1234-1234-123456789012"
    report = await report_factory([item(opaque_id)])
    _, key = await run_plan(report, FakeCloud())
    async with SessionLocal() as s:
        task = await s.scalar(select(ImprovementTask).where(ImprovementTask.plan_id == key))
        assert task.payload["evidence_ids"] == [opaque_id]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        result = await c.get(f"/api/reports/{report.id}/evidence/{opaque_id}")
        assert result.json()["text"] == "等候太久"
        assert (await c.get(f"/api/reports/{report.id}/evidence/other")).status_code == 404


async def test_no_complaints_do_not_generate_fake_plan(report_factory):
    report = await report_factory([item(sentiment="positive")])
    cloud = FakeCloud()
    _, key = await run_plan(report, cloud)
    assert not cloud.calls
    async with SessionLocal() as s:
        assert (await s.get(DecisionPlan, key)).status == "NEEDS_REVIEW"
        assert await s.scalar(select(ImprovementTask).where(ImprovementTask.plan_id == key)) is None


async def test_auto_collection_continues_but_manual_default_waits(report_factory):
    from simpsons_insight_agent.jobs import JobManager
    from simpsons_insight_agent.models import JobSource

    report = await report_factory()
    manager = JobManager()

    async def collect(job_id):
        async with SessionLocal() as s:
            job = await s.get(CrawlJob, job_id)
            job.status = "READY_FOR_ANALYSIS"
            job.collected_count = 1
            job.collection_complete = False
            await s.commit()

    manager._process_collection = collect
    async with SessionLocal() as s:
        job = await s.get(CrawlJob, report.job_id)
        job.status = "PENDING_COLLECTION"
        job.auto_plan = False
        s.add(JobSource(job_id=job.id, source="ptt", ordinal=0))
        await s.commit()
    await manager._process_job(report.job_id)
    assert manager.queue.empty()
    async with SessionLocal() as s:
        job = await s.get(CrawlJob, report.job_id)
        assert job.status == "READY_FOR_ANALYSIS"
        job.status = "PENDING_COLLECTION"
        job.auto_plan = True
        # start_analysis deliberately ignores already completed reports only by job status.
        job.llm_model = manager.settings.openai_model_default
        await s.commit()
    await manager._process_job(report.job_id)
    assert await manager.queue.get() == report.job_id
    async with SessionLocal() as s:
        assert (await s.get(CrawlJob, report.job_id)).status == "ANALYSIS_PENDING"


async def test_report_retry_preserves_snapshot_and_topics(report_factory):
    from simpsons_insight_agent.jobs import JobManager

    report = await report_factory()
    async with SessionLocal() as s:
        row = await s.get(Report, report.id)
        row.payload = {
            **row.payload,
            "topics": [{"topic_key": "stable", "item_ids": ["r1"]}],
            "topic_analysis": {"status": "completed"},
            "trends": {"points": []},
            "decision": {"status": "COMPLETED"},
        }
        await s.commit()
    rebuilt, _ = await JobManager()._build_report(report.job_id, report.business_id, "test", False)
    assert rebuilt.payload["schema_version"] == 3
    assert rebuilt.payload["topics"][0]["topic_key"] == "stable"
    assert rebuilt.payload["analytics_items"][0]["id"] == "r1"
    assert rebuilt.payload["decision"]["status"] == "COMPLETED"
