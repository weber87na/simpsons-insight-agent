from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, inspect, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings
from .models import CrawlJob, JobSource

EXPECTED_SCHEMA_REVISION = "0006_validation"


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    engine = create_async_engine(settings.database_url, future=True)

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine = build_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.connect() as connection:
        revision = await connection.run_sync(_schema_revision)
    if revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError(
            "資料庫 schema 尚未更新；請先執行 `uv run --no-sync alembic upgrade head`。"
        )


def _schema_revision(connection: Any) -> str | None:
    if "alembic_version" not in inspect(connection).get_table_names():
        return None
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


async def mark_inflight_jobs_interrupted() -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(CrawlJob)
            .where(CrawlJob.status.in_({"COLLECTING", "WAITING_FOR_USER"}))
            .values(
                status="COLLECTION_INTERRUPTED",
                collection_complete=False,
                collection_stop_reason="application_shutdown",
                message="應用程式上次執行時中斷，可按續跑重新開始。",
            )
        )
        await session.execute(
            update(JobSource)
            .where(JobSource.status == "COLLECTING")
            .values(
                status="PARTIAL",
                collection_complete=False,
                stop_reason="application_shutdown",
            )
        )
        await session.execute(
            update(CrawlJob)
            .where(CrawlJob.status.in_({"LOCAL_ANALYSIS", "EMBEDDING", "CLOUD_ANALYSIS", "REPORTING"}))
            .values(
                status="ANALYSIS_PENDING",
                message="應用程式上次於分析階段中斷，將從 checkpoint 接續。",
            )
        )
        await session.commit()


async def dispose_db() -> None:
    await engine.dispose()
