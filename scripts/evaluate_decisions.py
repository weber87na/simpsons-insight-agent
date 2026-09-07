"""Create blinded decision evaluation packets from an isolated synthetic demo database.
No model calls without --live. Prices must be supplied explicitly; unknown cost is null.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument("--input-usd-per-million", type=float)
    p.add_argument("--output-usd-per-million", type=float)
    args = p.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not manifest.get("synthetic"):
        raise SystemExit("只接受本專案合成示範資料 manifest")
    output = Path(args.output)
    if output.exists():
        raise SystemExit("輸出資料夾已存在；請使用新資料夾，保留評估版本。")
    output.mkdir(parents=True)
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + Path(manifest["database"]).as_posix()
    asyncio.run(run(args, manifest, output))


async def run(args, manifest, output):
    from pydantic import BaseModel
    from sqlalchemy import select

    from simpsons_insight_agent.cloud import OpenAIService
    from simpsons_insight_agent.config import get_settings
    from simpsons_insight_agent.db import SessionLocal, dispose_db
    from simpsons_insight_agent.decisions import (
        DecisionCoordinator,
        Diagnosis,
        ExpertReview,
        Proposal,
        schedule_actions,
    )
    from simpsons_insight_agent.insight_api import plan_view
    from simpsons_insight_agent.models import AgentRun, CrawlJob, DecisionPlan, Report
    from simpsons_insight_agent.schemas import PlanningOptions

    class SingleResult(BaseModel):
        diagnosis: Diagnosis
        proposal: Proposal
        evaluation: ExpertReview

    settings = get_settings()
    cloud = OpenAIService(settings)
    if args.live and not cloud.available:
        raise SystemExit("--live 需要 OPENAI_API_KEY；未產生模型輸出。")
    async with SessionLocal() as s:
        report = await s.get(Report, manifest["report_ids"][0])
        evidence = [
            x
            for x in report.payload["analytics_items"]
            if x["sentiment"] == "negative" or x.get("negative_aspects")
        ][:60]
        evidence_keys = {x["id"]: f"e{i:03d}" for i, x in enumerate(evidence)}
        for i, x in enumerate(evidence):
            evidence[i] = {**x, "id": f"e{i:03d}", "text": x["text"][:800]}
        baseline = report.payload["executive"]
    options = PlanningOptions(
        start_date="2026-09-07",
        weekly_hours=10,
        constraints="只有店長每週十小時可投入；不新增設備、不調整售價。",
    )
    outputs = {"current_summary": baseline}
    metrics = {
        "current_summary": {
            "status": "completed",
            "kind": "deterministic_baseline",
            "elapsed_ms": 0,
            "usage": {},
            "estimated_cost_usd": 0,
        },
        "single_agent": {"status": "not_run"},
        "multi_agent": {"status": "not_run"},
    }
    if args.live:
        start = time.monotonic()
        try:
            result, usage = await asyncio.wait_for(
                cloud.decision_generate(
                    {"evidence": evidence, "resources": options.model_dump(mode="json")},
                    settings.openai_model_default,
                    SingleResult,
                    "請一次完成口碑診斷、具體改善計畫及自我評估，根因須標示假設；使用輸入的證據 id。",
                ),
                120,
            )
            outputs["single_agent"] = {
                **result.model_dump(),
                "schedule": schedule_actions(
                    result.proposal.model_dump()["actions"], options.model_dump(mode="json")
                ),
            }
            metrics["single_agent"] = {
                "status": "completed",
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "usage": usage,
            }
        except Exception as exc:
            metrics["single_agent"] = {
                "status": "failed",
                "error": type(exc).__name__,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            }
        async with SessionLocal() as s:
            benchmark_job = CrawlJob(
                business_id=report.business_id,
                status="COMPLETED",
                llm_model=settings.openai_model_default,
            )
            s.add(benchmark_job)
            await s.flush()
            benchmark_report = Report(
                job_id=benchmark_job.id,
                business_id=report.business_id,
                payload={**report.payload, "decision": {"status": "NOT_STARTED"}},
            )
            s.add(benchmark_report)
            await s.commit()
        coordinator = DecisionCoordinator(cloud, asyncio.Queue())
        key = await coordinator.start(benchmark_report.id, options)
        start = time.monotonic()
        await coordinator.run(key)
        async with SessionLocal() as s:
            plan = await s.get(DecisionPlan, key)
            data = await plan_view(s, plan)
            runs = (await s.scalars(select(AgentRun).where(AgentRun.plan_id == key))).all()
            usage = {
                k: sum(r.output.get("usage", {}).get(k, 0) for r in runs)
                for k in ("input_tokens", "output_tokens")
            }
            metrics["multi_agent"] = {
                "status": plan.status,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "cumulative_agent_ms": sum(r.elapsed_ms for r in runs),
                "usage": usage,
                "checkpoint_reuse": any(r.attempts > 1 for r in runs),
            }
            if data["tasks"]:
                outputs["multi_agent"] = {
                    "diagnosis": data["diagnosis"],
                    "actions": [
                        {k: v for k, v in task.items() if k != "id"} for task in data["tasks"]
                    ],
                }
    for value in metrics.values():
        if (
            value.get("usage")
            and args.input_usd_per_million is not None
            and args.output_usd_per_million is not None
        ):
            value["estimated_cost_usd"] = (
                value["usage"].get("input_tokens", 0) * args.input_usd_per_million
                + value["usage"].get("output_tokens", 0) * args.output_usd_per_million
            ) / 1_000_000
        else:
            value.setdefault("estimated_cost_usd", None)

    def anonymous_ids(value):
        if isinstance(value, dict):
            return {k: anonymous_ids(v) for k, v in value.items()}
        if isinstance(value, list):
            return [anonymous_ids(v) for v in value]
        return evidence_keys.get(value, value) if isinstance(value, str) else value

    outputs = anonymous_ids(outputs)
    methods = list(outputs)
    random.SystemRandom().shuffle(methods)
    mapping = {chr(65 + i): method for i, method in enumerate(methods)}
    # Keep the answer key and telemetry out of the reviewer directory.
    reviewer = output / "reviewer"
    reviewer.mkdir()
    for label, method in mapping.items():
        (reviewer / f"{label}.json").write_text(
            json.dumps(outputs[method], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (reviewer / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (reviewer / "ratings.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["reviewer", "label", "evidence_1_5", "specificity_1_5", "feasibility_1_5", "notes"]
        )
        for label in mapping:
            writer.writerow(["", label, "", "", "", ""])
    digest = hashlib.sha256(
        (ROOT / "tests/fixtures/decision/synthetic_reviews.json").read_bytes()
    ).hexdigest()
    (output / "researcher.json").write_text(
        json.dumps(
            {
                "mapping": mapping,
                "metrics": metrics,
                "fixture_sha256": digest,
                "model": settings.openai_model_default,
                "live": args.live,
                "human_evaluation": "pending",
                "limitations": [
                    "單次合成案例不能證明效益；至少三位評估者，先獨立評分再解盲。",
                    "格式差异可能透露方法，盲評只隱藏方法名稱。",
                    "成本為成功請求 token 估算；失敗請求費用及歷史重试可能缺漏。",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    await dispose_db()
    print(output)


if __name__ == "__main__":
    main()
