"""Seed a NEW isolated demo database; does not touch the user's normal database.
Usage: python scripts/seed_decision_demo.py --database data/decision-demo.db
The artificial topic vectors are for UI demonstrations, not a model benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/decision-demo.db")
    args = parser.parse_args()
    path = Path(args.database).resolve()
    if path.exists():
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
    import numpy as np

    from simpsons_insight_agent.config import get_settings
    from simpsons_insight_agent.db import SessionLocal, dispose_db
    from simpsons_insight_agent.insights import enrich_report
    from simpsons_insight_agent.models import (
        Business,
        CrawlJob,
        JobReview,
        Report,
        Review,
        ReviewAnalysis,
        ReviewEmbedding,
    )
    from simpsons_insight_agent.reporting import build_aggregate, deterministic_summary

    fixture = json.loads(
        (ROOT / "tests/fixtures/decision/synthetic_reviews.json").read_text(encoding="utf-8-sig")
    )
    report_ids = []
    async with SessionLocal() as s:
        for brand_index, name in enumerate(fixture["brands"]):
            b = Business(name=name)
            s.add(b)
            await s.flush()
            job = CrawlJob(
                business_id=b.id,
                status="COMPLETED",
                collection_complete=True,
                llm_model=get_settings().openai_model_default,
                progress=1,
                collected_count=24,
            )
            s.add(job)
            await s.flush()
            aggregate_rows = []
            for i in range(24):
                row = fixture["reviews"][(i + brand_index) % len(fixture["reviews"])]
                when = datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i * 2)
                r = Review(
                    business_id=b.id,
                    source=["ptt", "dcard"][i % 2],
                    content_hash=f"synthetic-{i}",
                    text=row["text"],
                    redacted_text=row["text"],
                    published_at_estimated=when,
                    date_precision="day",
                    platform_data={"synthetic": True},
                )
                s.add(r)
                await s.flush()
                s.add(JobReview(job_id=job.id, review_id=r.id, ordinal=i))
                s.add(
                    ReviewAnalysis(
                        job_id=job.id,
                        review_id=r.id,
                        sentiment=row["sentiment"],
                        confidence=1,
                        aspects=[row["aspect"]],
                        negative_aspects=[row["aspect"]] if row["topic"] is not None else [],
                        key_points=[
                            "等候資訊不足"
                            if row["topic"] == 0
                            else "漏單處理"
                            if row["topic"] == 1
                            else "正常服務"
                        ],
                        local_model_id="synthetic-labels",
                        cloud_status="COMPLETED",
                    )
                )
                if row["topic"] is not None:
                    v = np.array([1.0, 0.0] if row["topic"] == 0 else [0.0, 1.0], dtype=np.float32)
                    s.add(
                        ReviewEmbedding(
                            review_id=r.id,
                            model_id="synthetic-demo-vectors",
                            dimension=2,
                            vector=v.tobytes(),
                        )
                    )
                aggregate_rows.append(
                    {
                        "id": r.id,
                        "source": r.source,
                        "text": r.text,
                        "sentiment": row["sentiment"],
                        "aspects": [row["aspect"]],
                        "published_at_estimated": when,
                        "date_precision": "day",
                    }
                )
            payload = build_aggregate(
                business={"id": b.id, "name": name}, reviews=aggregate_rows, model_id=None
            )
            payload["collection"] = {
                "complete": True,
                "sources": {
                    source: {"status": "COMPLETE", "actual_count": 12, "complete": True}
                    for source in ["ptt", "dcard"]
                },
            }
            payload["executive"] = deterministic_summary(payload)
            payload["synthetic_demo"] = True
            payload["item_ids"] = [r["id"] for r in aggregate_rows]
            report = Report(job_id=job.id, business_id=b.id, payload=payload)
            s.add(report)
            await s.flush()
            await enrich_report(s, report, "synthetic-demo-vectors")
            report_ids.append(report.id)
        await s.commit()
    manifest = path.with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {"database": str(path), "report_ids": report_ids, "synthetic": True},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    await dispose_db()
    print(manifest)


if __name__ == "__main__":
    main()
