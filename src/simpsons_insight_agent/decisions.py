"""Bounded specialist workflow with durable checkpoints and evidence-gated plans."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import SessionLocal
from .models import AgentRun, DecisionPlan, ImprovementTask, JobEvent, PlanEvaluation, Report
from .privacy import sanitize_for_openai
from .schemas import PlanningOptions

RULE_VERSION = "practicality-v1"
TERMINAL = {"COMPLETED", "NEEDS_REVIEW", "PARTIAL", "CANCELED"}


class Problem(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    problem: str
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    root_cause_hypothesis: str
    unknowns: list[str]


class Diagnosis(BaseModel):
    problems: list[Problem] = Field(max_length=10)


class Action(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    problem_key: str
    title: str
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    steps: list[str] = Field(min_length=1, max_length=10)
    owner_role: str
    hours: float = Field(gt=0, le=1000)
    cost_estimate: str
    prerequisites: list[str]
    assumptions: list[str]
    metric: str
    target: str
    verification: str
    risks: list[str]
    priority: int = Field(ge=1, le=5)
    dependencies: list[str]


class Proposal(BaseModel):
    actions: list[Action] = Field(min_length=1, max_length=15)


class ExpertReview(BaseModel):
    evidence: int = Field(ge=1, le=5)
    feasibility: int = Field(ge=1, le=5)
    resources: int = Field(ge=1, le=5)
    measurability: int = Field(ge=1, le=5)
    risk: int = Field(ge=1, le=5)
    reasons: list[str] = Field(min_length=1)
    revisions: list[str]


class HumanReview(ExpertReview):
    reviewer: str = Field(min_length=1, max_length=100)
    corrections: str = Field(default="", max_length=5000)


class TaskUpdate(BaseModel):
    owner_role: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    due_date: date | None = None
    status: Literal["TODO", "IN_PROGRESS", "DONE", "BLOCKED"] | None = None
    actual_results: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)


def schedule_actions(actions: list[dict], options: dict) -> list[dict]:
    remaining = {a["key"]: a for a in actions}
    if len(remaining) != len(actions):
        raise ValueError("工作 key 重複")
    if any(dep not in remaining for a in actions for dep in a["dependencies"]):
        raise ValueError("相依工作不存在")
    capacity = options.get("weekly_hours") or 5.0
    absolute = bool(options.get("weekly_hours") and options.get("start_date"))
    start = date.fromisoformat(options["start_date"]) if absolute else None
    cursor = 0.0
    result = []
    while remaining:
        ready = [
            a for a in remaining.values() if all(d not in remaining for d in a["dependencies"])
        ]
        if not ready:
            raise ValueError("工作相依形成循環")
        action = sorted(ready, key=lambda a: (a["priority"], a["key"]))[0]
        first_day = math.floor(cursor / (capacity / 7))
        first_week = math.floor(cursor / capacity)
        cursor += action["hours"]
        last_week = max(first_week, math.ceil(cursor / capacity) - 1)
        result.append(
            {
                **action,
                "start_week": first_week + 1,
                "due_week": last_week + 1,
                "start_date": (start + timedelta(days=first_day)).isoformat() if start else None,
                "due_date": (
                    start + timedelta(days=max(first_day, math.ceil(cursor / (capacity / 7)) - 1))
                ).isoformat()
                if start
                else None,
                "schedule_mode": "dated" if absolute else "relative_draft",
                "schedule_assumption": None
                if absolute
                else f"相對草案，每週 {capacity:g} 小時；待確認開始日期及資源。",
                "status": "TODO",
                "actual_results": "",
                "notes": "",
            }
        )
        del remaining[action["key"]]
    return result


def evaluate_rules(proposal, diagnosis, evidence_ids, options, expert):
    reasons = []
    problems = {p["key"]: p for p in diagnosis["problems"]}
    for a in proposal["actions"]:
        p = problems.get(a["problem_key"])
        if (
            not p
            or not set(a["evidence_ids"]).issubset(evidence_ids)
            or not set(a["evidence_ids"]).issubset(set(p["evidence_ids"]) if p else set())
        ):
            reasons.append(f"{a['key']}：問題或引用證據無效")
        if not all(
            str(a[k]).strip()
            for k in ("owner_role", "cost_estimate", "metric", "target", "verification")
        ) or any(not step.strip() for step in a["steps"]):
            reasons.append(f"{a['key']}：缺少負責角色、成本、步驟或可衡量目標")
    try:
        schedule_actions(proposal["actions"], options)
    except ValueError as exc:
        reasons.append(str(exc))
    if not options.get("weekly_hours") or not options.get("start_date"):
        reasons.append("資源或開始日期未確認；僅能提供相對時程草案")
    if any(p["unknowns"] for p in problems.values()):
        reasons.append("根因仍有待確認資訊，請先驗證假設")
    if any(a["prerequisites"] or a["assumptions"] for a in proposal["actions"]):
        reasons.append("方案的前置條件與假設需人工確認")
    if (
        min(expert[k] for k in ("evidence", "feasibility", "resources", "measurability", "risk"))
        < 3
        or expert["revisions"]
    ):
        reasons.extend(expert["revisions"] or ["專家評分未達每項 3 分門檻"])
    return {"version": RULE_VERSION, "passed": not reasons, "reasons": reasons, "expert": expert}


class DecisionCoordinator:
    def __init__(self, cloud, queue):
        self.cloud = cloud
        self.queue = queue
        self.lock = asyncio.Lock()

    async def recover(self):
        async with SessionLocal() as s:
            plans = (
                await s.scalars(
                    select(DecisionPlan).where(DecisionPlan.status.in_(["RUNNING", "PENDING"]))
                )
            ).all()
            for plan in plans:
                plan.status = "PENDING"
                await self.queue.put("decision:" + plan.id)
            await s.commit()

    async def start(self, report_id, options: PlanningOptions):
        async with self.lock, SessionLocal() as s:
            report = await s.get(Report, report_id)
            if report is None:
                raise LookupError("找不到報告")
            plan = await s.scalar(select(DecisionPlan).where(DecisionPlan.report_id == report_id))
            if plan:
                return plan.id
            plan = DecisionPlan(report_id=report_id, options=options.model_dump(mode="json"))
            s.add(plan)
            await s.flush()
            report.payload = {
                **report.payload,
                "decision": {"plan_id": plan.id, "status": "PENDING"},
            }
            await s.commit()
            await self.queue.put("decision:" + plan.id)
            return plan.id

    async def retry(self, plan_id):
        async with self.lock, SessionLocal() as s:
            plan = await s.get(DecisionPlan, plan_id)
            if plan is None:
                raise LookupError("找不到改善計畫")
            if plan.status not in {"PARTIAL", "CANCELED"}:
                return
            plan.status, plan.error, plan.cancel_requested = "PENDING", None, False
            await s.commit()
            await self.queue.put("decision:" + plan.id)

    async def canceled(self, plan_id):
        async with SessionLocal() as s:
            plan = await s.get(DecisionPlan, plan_id)
            return plan is None or plan.cancel_requested

    async def finish(self, plan_id, status, error=None):
        async with SessionLocal() as s:
            plan = await s.get(DecisionPlan, plan_id)
            if plan is None:
                return
            plan.status, plan.error = status, error
            report = await s.get(Report, plan.report_id)
            if report:
                report.payload = {
                    **report.payload,
                    "decision": {"plan_id": plan.id, "status": status, "error": error},
                }
                s.add(
                    JobEvent(
                        job_id=report.job_id,
                        event_type="decision",
                        payload={"plan_id": plan.id, "status": status},
                    )
                )
            await s.commit()

    async def stage(self, plan_id, stage, payload, schema, instruction, tool=None):
        if await self.canceled(plan_id):
            raise asyncio.CancelledError
        async with SessionLocal() as s:
            run = await s.scalar(
                select(AgentRun).where(AgentRun.plan_id == plan_id, AgentRun.stage == stage)
            )
            if run and run.status == "COMPLETED":
                return run.output["result"]
            if run is None:
                run = AgentRun(plan_id=plan_id, stage=stage, attempts=0)
                s.add(run)
            if run.attempts >= 3:
                raise ValueError("單階段已達三次嘗試上限")
            if run.attempts:
                history = list((run.output or {}).get("attempt_history", []))
                history.append(
                    {
                        "attempt": run.attempts,
                        "status": run.status,
                        "error": run.error,
                        "elapsed_ms": run.elapsed_ms,
                    }
                )
                run.output = {**(run.output or {}), "attempt_history": history}
            run.attempts += 1
            run.status, run.error, run.input = "RUNNING", None, sanitize_for_openai(payload)
            run.tools = ["schedule_actions" if tool else "structured_model", "report_evidence"]
            await s.commit()
            run_id = run.id
        started = time.monotonic()
        work = None
        evidence_map = {}
        try:
            if tool:
                result, usage = tool(), {}
            else:
                async with SessionLocal() as s:
                    plan = await s.get(DecisionPlan, plan_id)
                    report = await s.get(Report, plan.report_id)
                    from .models import CrawlJob

                    job = await s.get(CrawlJob, report.job_id)
                    model = job.llm_model
                evidence_map = {
                    x["id"]: f"e{i:03d}" for i, x in enumerate(payload.get("evidence", []))
                }

                def map_ids(value, mapping, field=""):
                    if isinstance(value, dict):
                        return {k: map_ids(v, mapping, k) for k, v in value.items()}
                    if isinstance(value, list):
                        return [map_ids(v, mapping, field) for v in value]
                    return (
                        mapping.get(value, value)
                        if isinstance(value, str) and field in {"id", "evidence_ids"}
                        else value
                    )

                cloud_payload = map_ids(payload, evidence_map)
                async with SessionLocal() as s:
                    run = await s.get(AgentRun, run_id)
                    if run:
                        run.input = sanitize_for_openai(cloud_payload)
                        await s.commit()
                work = asyncio.create_task(
                    self.cloud.decision_generate(cloud_payload, model, schema, instruction)
                )
                while not work.done():
                    await asyncio.wait({work}, timeout=0.25)
                    if await self.canceled(plan_id):
                        raise asyncio.CancelledError
                    if time.monotonic() - started > 120:
                        raise TimeoutError("Agent 超過 120 秒")
                parsed, usage = work.result()
                result = map_ids(
                    parsed.model_dump(mode="json"), {v: k for k, v in evidence_map.items()}
                )
            if schema is Diagnosis:
                allowed = {x["id"] for x in payload["evidence"]}
                if any(
                    not set(p["evidence_ids"]).issubset(allowed) for p in result["problems"]
                ) or len({p["key"] for p in result["problems"]}) != len(result["problems"]):
                    raise ValueError("診斷引用無效或 key 重複")
            async with SessionLocal() as s:
                run = await s.get(AgentRun, run_id)
                if run:
                    run.status = "COMPLETED"
                    run.output = {
                        **(run.output or {}),
                        "result": result,
                        "usage": usage,
                        "estimated_cost": None,
                        "evidence_key_map": evidence_map,
                    }
                    run.elapsed_ms = round((time.monotonic() - started) * 1000)
                    await s.commit()
            return result
        except BaseException as exc:
            if work and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            async with SessionLocal() as s:
                run = await s.get(AgentRun, run_id)
                if run:
                    run.status = (
                        "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
                    )
                    run.error = type(exc).__name__
                    run.elapsed_ms = round((time.monotonic() - started) * 1000)
                    await s.commit()
            raise

    async def run(self, plan_id):
        async with SessionLocal() as s:
            plan = await s.get(DecisionPlan, plan_id)
            if not plan or plan.status != "PENDING":
                return
            report = await s.get(Report, plan.report_id)
            options = plan.options
            from .insights import snapshot_items

            items = await snapshot_items(s, report)
            # Limit context; expose coverage and preserve all source evidence locally.
            evidence = [
                x
                for x in items
                if (x.get("sentiment") == "negative" or x.get("negative_aspects")) and x["text"]
            ][:60]
            evidence = [{**x, "text": x["text"][:800]} for x in evidence]
        await self.finish(plan_id, "RUNNING")
        try:
            if await self.canceled(plan_id):
                raise asyncio.CancelledError
            if not self.cloud.available:
                await self.finish(plan_id, "PARTIAL", "未設定模型金鑰；統計報告仍可使用")
                return
            if not evidence:
                await self.finish(
                    plan_id, "NEEDS_REVIEW", "沒有可引用的負面文字證據，不生成改善方案"
                )
                return
            async with asyncio.timeout(600):
                diagnosis = await self.stage(
                    plan_id,
                    "diagnosis",
                    {"evidence": evidence},
                    Diagnosis,
                    "你是診斷 Agent。只根據證據找出問題，根因一律是待驗證假設；列出不確定資訊。key 必須唯一，evidence_ids 使用輸入 id。",
                )
                ids = {x["id"] for x in evidence}
                if len({p["key"] for p in diagnosis["problems"]}) != len(
                    diagnosis["problems"]
                ) or any(not set(p["evidence_ids"]).issubset(ids) for p in diagnosis["problems"]):
                    raise ValueError("診斷引用不在報告內")
                if not diagnosis["problems"]:
                    await self.finish(plan_id, "NEEDS_REVIEW", "證據不足以提出問題")
                    return
                async with SessionLocal() as s:
                    current = await s.get(DecisionPlan, plan_id)
                    if current is None:
                        return
                    current.diagnosis = {
                        **diagnosis,
                        "evidence_count": len(evidence),
                        "negative_count": sum(
                            x.get("sentiment") == "negative" or bool(x.get("negative_aspects"))
                            for x in items
                        ),
                        "limitations": ["最多選取 60 筆抱怨，每筆最多 800 字；根因為假設。"],
                    }
                    await s.commit()
                feedback: dict = {}
                proposal: dict = {}
                evaluation: dict = {}
                for revision in range(3):
                    proposal = await self.stage(
                        plan_id,
                        f"proposal_{revision}",
                        {
                            "diagnosis": diagnosis,
                            "evidence": evidence,
                            "resources": options,
                            "previous": proposal,
                            "feedback": feedback,
                        },
                        Proposal,
                        "你是改善方案 Agent。提出可執行且可衡量的方案、步驟、角色、估計工時與成本、前置條件、假設、風險與相依工作。priority 1 最優先，key 唯一。未知資源不可虛構，標示假設；引用對應問題的證據。依退回理由修訂。",
                    )
                    expert = await self.stage(
                        plan_id,
                        f"expert_{revision}",
                        {
                            "diagnosis": diagnosis,
                            "evidence": evidence,
                            "proposal": proposal,
                            "resources": options,
                        },
                        ExpertReview,
                        "你是獨立評估 Agent。以 1–5 分評估證據支持、實用性、資源、衡量方式及風險控制，5 為最佳。檢查是否真能解決問題；不把根因假設當作事實。給出理由與必要修訂。",
                    )
                    evaluation = evaluate_rules(proposal, diagnosis, ids, options, expert)
                    feedback = evaluation
                    async with SessionLocal() as s:
                        existing = (
                            await s.scalars(
                                select(PlanEvaluation).where(
                                    PlanEvaluation.plan_id == plan_id, PlanEvaluation.kind == "ai"
                                )
                            )
                        ).all()
                        if not any(e.payload.get("revision") == revision for e in existing):
                            s.add(
                                PlanEvaluation(
                                    plan_id=plan_id,
                                    kind="ai",
                                    payload={**evaluation, "revision": revision},
                                )
                            )
                            await s.commit()
                    if evaluation["passed"]:
                        break
                # Invalid evidence never becomes a published task, even after revision limits.
                problems = {p["key"]: p for p in diagnosis["problems"]}
                safe = [
                    a
                    for a in proposal["actions"]
                    if a["problem_key"] in problems
                    and set(a["evidence_ids"]).issubset(
                        ids & set(problems[a["problem_key"]]["evidence_ids"])
                    )
                ]
                try:
                    schedule_actions(safe, options)
                except ValueError as exc:
                    await self.finish(
                        plan_id, "NEEDS_REVIEW", f"修訂後排程仍無效：{exc}；請檢視評估與執行紀錄"
                    )
                    return
                if not safe:
                    await self.finish(
                        plan_id, "NEEDS_REVIEW", "修訂後仍無有效引用方案；請檢視評估與執行紀錄"
                    )
                    return
                scheduled = await self.stage(
                    plan_id,
                    "schedule",
                    {"actions": safe, "options": options},
                    None,
                    "",
                    tool=lambda: {"tasks": schedule_actions(safe, options)},
                )
                if await self.canceled(plan_id):
                    raise asyncio.CancelledError
                async with SessionLocal() as s:
                    plan = await s.get(DecisionPlan, plan_id)
                    if plan is None:
                        return
                    plan.diagnosis = {
                        **diagnosis,
                        "evidence_count": len(evidence),
                        "negative_count": sum(x.get("sentiment") == "negative" for x in items),
                        "limitations": ["最多選取 60 筆負評，每筆最多 800 字；根因為假設。"],
                    }
                    existing_keys = set(
                        (
                            await s.scalars(
                                select(ImprovementTask.task_key).where(
                                    ImprovementTask.plan_id == plan_id
                                )
                            )
                        ).all()
                    )
                    for task in scheduled["tasks"]:
                        if task["key"] not in existing_keys:
                            s.add(
                                ImprovementTask(plan_id=plan_id, task_key=task["key"], payload=task)
                            )
                    await s.commit()
                await self.finish(plan_id, "COMPLETED" if evaluation["passed"] else "NEEDS_REVIEW")
        except asyncio.CancelledError:
            if await self.canceled(plan_id):
                await self.finish(plan_id, "CANCELED")
            else:
                await self.finish(plan_id, "PENDING", "應用程式中斷，下次啟動續跑")
                raise
        except Exception as exc:
            await self.finish(
                plan_id, "PARTIAL", f"{type(exc).__name__}：決策未完成，可重試；統計報告仍可使用"
            )
