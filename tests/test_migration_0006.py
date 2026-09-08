"""Exercise a real 0005 database without new tables, not only fresh metadata."""

import sqlite3

from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command


def test_validation_migration_preserves_existing_data(tmp_path, monkeypatch):
    from simpsons_insight_agent.config import get_settings
    from simpsons_insight_agent.models import Business, CrawlJob, DecisionPlan, Report

    path = tmp_path / "upgrade.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + path.as_posix())
    get_settings.cache_clear()
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0005_decision")
        with sqlite3.connect(path) as db:
            # 0001 currently uses current metadata: remove new tables to emulate an old installation.
            for table in ["validation_results", "validation_experiments", "validation_runs"]:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            db.execute("CREATE TABLE migration_sentinel (value TEXT)")
            db.execute("INSERT INTO migration_sentinel VALUES ('keep original data')")
            prior = {name: sql for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")}
        engine = create_engine("sqlite:///" + path.as_posix())
        with Session(engine) as s:
            b = Business(name="既有商家")
            s.add(b)
            s.flush()
            job = CrawlJob(business_id=b.id, status="COMPLETED", llm_model="test")
            s.add(job)
            s.flush()
            report = Report(job_id=job.id, business_id=b.id, payload={"preserve": "original report"})
            s.add(report)
            s.flush()
            plan = DecisionPlan(report_id=report.id, diagnosis={"preserve": "original diagnosis"})
            s.add(plan)
            s.commit()
        engine.dispose()
        command.upgrade(config, "head")
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0006_validation"
            assert db.execute("SELECT value FROM migration_sentinel").fetchone()[0] == "keep original data"
            after = {name: sql for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")}
            for name, sql in prior.items():
                assert after[name] == sql
            assert {"validation_runs", "validation_experiments", "validation_results"} <= set(after)
            assert "original report" in db.execute("SELECT payload FROM reports").fetchone()[0]
            assert "original diagnosis" in db.execute("SELECT diagnosis FROM decision_plans").fetchone()[0]
        command.downgrade(config, "0005_decision")
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM migration_sentinel").fetchone()[0] == "keep original data"
    finally:
        get_settings.cache_clear()
