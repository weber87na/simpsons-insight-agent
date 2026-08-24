from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from simpsons_insight_agent.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_0003_fixture_upgrades_without_losing_google_data(tmp_path: Path) -> None:
    database = tmp_path / "legacy-0003.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0003_job_events');
        CREATE TABLE businesses (
          id VARCHAR(36) PRIMARY KEY, maps_url TEXT NOT NULL UNIQUE, name VARCHAR(500) NOT NULL,
          address TEXT, average_rating FLOAT, total_review_count INTEGER,
          created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE crawl_jobs (
          id VARCHAR(36) PRIMARY KEY, business_id VARCHAR(36) NOT NULL, status VARCHAR(40) NOT NULL,
          max_reviews INTEGER NOT NULL, sort_order VARCHAR(20) NOT NULL, headless BOOLEAN NOT NULL,
          llm_model VARCHAR(100) NOT NULL, collected_count INTEGER NOT NULL,
          processed_count INTEGER NOT NULL, progress FLOAT NOT NULL, message TEXT, error TEXT,
          cancel_requested BOOLEAN NOT NULL, collection_complete BOOLEAN NOT NULL,
          collection_stop_reason VARCHAR(100), last_completed_stage VARCHAR(40),
          degraded_reasons JSON NOT NULL, attempt_count INTEGER NOT NULL,
          created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, started_at DATETIME,
          finished_at DATETIME, FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE reviews (
          id VARCHAR(36) PRIMARY KEY, business_id VARCHAR(36) NOT NULL,
          source_review_id VARCHAR(500), content_hash VARCHAR(64) NOT NULL,
          author_name VARCHAR(500), rating INTEGER, text TEXT NOT NULL,
          relative_date VARCHAR(200), published_at_estimated DATETIME,
          date_precision VARCHAR(20) NOT NULL, owner_reply TEXT, source_url TEXT,
          language VARCHAR(20), redacted_text TEXT, scraped_at DATETIME NOT NULL,
          CONSTRAINT uq_review_business_hash UNIQUE (business_id, content_hash),
          CONSTRAINT uq_review_business_source UNIQUE (business_id, source_review_id),
          FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
        CREATE TABLE job_reviews (
          job_id VARCHAR(36) NOT NULL, review_id VARCHAR(36) NOT NULL, ordinal INTEGER NOT NULL,
          observed_at DATETIME NOT NULL, PRIMARY KEY(job_id, review_id)
        );
        CREATE TABLE reports (
          id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36) NOT NULL UNIQUE,
          business_id VARCHAR(36) NOT NULL, model_id VARCHAR(100), status VARCHAR(30) NOT NULL,
          payload JSON NOT NULL, created_at DATETIME NOT NULL
        );
        """
    )
    timestamp = "2026-01-01 00:00:00"
    connection.execute(
        "INSERT INTO businesses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("business-1", "https://www.google.com/maps/place/legacy", "舊商家", "台北", 4.5, 1, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO crawl_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-1", "business-1", "COMPLETED", 500, "newest", 1,
            "gpt-5.4-mini-2026-03-17", 1, 1, 1.0, "完成", None, 0, 1,
            "target_reached", "REPORTING", "[]", 2, timestamp, timestamp, timestamp, timestamp,
        ),
    )
    connection.execute(
        "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "review-1", "business-1", "google-review-1", "a" * 64, "Google 作者", 5,
            "很好", "1 天前", timestamp, "day", None,
            "https://www.google.com/maps/place/legacy", "zh", "很好", timestamp,
        ),
    )
    connection.execute(
        "INSERT INTO job_reviews VALUES (?, ?, ?, ?)",
        ("job-1", "review-1", 1, timestamp),
    )
    report_payload = {"sample_size": 1, "review_ids": ["review-1"]}
    connection.execute(
        "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("report-1", "job-1", "business-1", None, "PARTIAL", json.dumps(report_payload), timestamp),
    )
    connection.commit()
    connection.close()

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    get_settings.cache_clear()
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0004_multi_source"
    assert connection.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM crawl_jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    review = connection.execute(
        "SELECT source, content_type, source_item_id, text FROM reviews"
    ).fetchone()
    assert tuple(review) == ("google_maps", "review", "google-review-1", "很好")
    source_run = connection.execute(
        "SELECT source, status, collected_count, config FROM job_sources"
    ).fetchone()
    assert source_run[0:3] == ("google_maps", "COMPLETE", 1)
    assert json.loads(source_run[3])["max_reviews"] == 500
    assert json.loads(source_run[3])["maps_url"] == "https://www.google.com/maps/place/legacy"
    assert json.loads(connection.execute("SELECT payload FROM reports").fetchone()[0]) == report_payload
    maps_url_info = next(
        row for row in connection.execute("PRAGMA table_info(businesses)") if row[1] == "maps_url"
    )
    assert maps_url_info[3] == 0
    assert connection.execute("SELECT subject_key FROM businesses").fetchone()[0]
    connection.close()
