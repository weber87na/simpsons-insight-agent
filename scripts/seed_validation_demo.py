"""Create a new, isolated, explicitly synthetic campus validation demonstration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/validation-demo.db")
    args = parser.parse_args()
    path = Path(args.database).resolve()
    if path.exists() or path.with_suffix(".json").exists():
        raise SystemExit("資料庫已存在；請指定新檔名，避免覆寫。")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + path.as_posix()
    from alembic.config import Config

    from alembic import command

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    asyncio.run(seed(path))


async def seed(path):
    from simpsons_insight_agent.config import get_settings
    from simpsons_insight_agent.db import SessionLocal, dispose_db
    from simpsons_insight_agent.models import (
        Business,
        CrawlJob,
        DecisionPlan,
        Report,
        ValidationRun,
    )
    from simpsons_insight_agent.reporting import build_aggregate, deterministic_summary
    from simpsons_insight_agent.validation import (
        ExperimentDesign,
        ExperimentReview,
        ValidationCoordinator,
        ValidationOptions,
    )

    # Handwritten data and model substitute verify workflow only, never model quality.
    rows = []
    texts = [
        ("negative", "申請說明沒寫需要哪些文件，我又跑了一趟。"),
        ("positive", "照著申請頁面的清單準備文件，這次一次完成。"),
        ("neutral", "期末申請人數較多，等待時間可能比較長。"),
    ]
    for i, (sentiment, text) in enumerate(texts):
        rows.append({"id": f"synthetic-campus-{i}", "text": text, "source": "ptt", "content_type": "post",
                     "sentiment": sentiment, "aspects": ["service"], "negative_aspects": ["service"] if sentiment == "negative" else [],
                     "published_at": (date.today() - timedelta(days=30-i)).isoformat(), "date_precision": "day", "topic_keys": [],
                     "title": "自寫合成校園服務案例", "key_points": ["申請文件資訊"], "rating": None})
    async with SessionLocal() as s:
        business = Business(name="合成校園服務案例（非高科大真實評論）")
        s.add(business)
        await s.flush()
        job = CrawlJob(business_id=business.id, status="COMPLETED", llm_model=get_settings().openai_model_default, collected_count=3, progress=1)
        s.add(job)
        await s.flush()
        payload = build_aggregate(business={"name": business.name}, reviews=rows, model_id=None)
        payload.update(schema_version=3, analytics_items=rows, topics=[], item_ids=[r["id"] for r in rows], synthetic=True,
                       collection={"complete": False, "sources": {}}, generated_at=date.today().isoformat())
        payload["executive"] = deterministic_summary(payload)
        report = Report(job_id=job.id, business_id=business.id, payload=payload)
        s.add(report)
        await s.flush()
        plan = DecisionPlan(report_id=report.id, status="NEEDS_REVIEW", options={}, diagnosis={"problems": [{"key": "documents", "problem": "合成情境：申請文件資訊可能不清楚", "evidence_ids": [rows[0]["id"]], "root_cause_hypothesis": "說明資訊可能不完整", "unknowns": ["需查看實際流程"]}], "synthetic": True})
        s.add(plan)
        await s.commit()

    class SyntheticCloud:
        available = True

        async def decision_generate(self, payload, model, schema, instruction):
            if schema is ExperimentReview:
                return ExperimentReview(approved=True, reasons=["合成替身審查；僅供操作示範，非模型評估"]), {}
            support = payload["problems"][0]["evidence_ids"][0]
            counter = [r["id"] for r in payload["evidence"] if r["sentiment"] == "positive"]
            return ExperimentDesign(experiments=[{
                "key": "document-guide", "problem_key": "documents", "title": "合成實驗：試用一頁申請文件清單",
                "journey_stage": "詢問／申請", "hypothesis": "提供清單可能提升一次完成率",
                "alternative_explanations": ["高峰人潮而非文件說明可能造成等待"], "support_evidence_ids": [support],
                "counter_evidence_ids": counter, "counter_note": "有人依既有清單完成，尚不能認定說明一定不足。",
                "steps": ["確認既有清單與實際文件一致", "在單一服務窗口試用清單", "以相同口徑記錄前後完成數與總數"],
                "owner_role": "服務窗口承辦人", "hours": 2, "cost_estimate": "沿用既有文件；需人工確認工時",
                "metric": "一次申請完成率", "metric_kind": "ratio", "unit": "%", "observation_days": 7,
                "stop_conditions": ["清單內容與正式規定不一致即停止"],
            }], missing_information=[]), {}

    coordinator = ValidationCoordinator(SyntheticCloud(), asyncio.Queue())
    run_id = await coordinator.start(plan.id, ValidationOptions(start_date=date.today(), weekly_hours=2))
    await coordinator.run(run_id)
    async with SessionLocal() as s:
        generated = await s.get(ValidationRun, run_id)
        if generated.status != "COMPLETED":
            raise RuntimeError(f"合成流程未完成：{generated.status} {generated.error}")
    manifest = {"synthetic": True, "model_substitute": True, "database": str(path), "report_id": report.id,
                "plan_id": plan.id, "validation_id": run_id, "url": f"http://127.0.0.1:8000/reports/{report.id}"}
    path.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    await dispose_db()


if __name__ == "__main__":
    main()
