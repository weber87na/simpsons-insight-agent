import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from review_agent.config import Settings
from review_agent.db import build_engine
from review_agent.models import Base, Business, Review


@pytest.mark.asyncio
async def test_review_content_hash_is_unique_per_business(tmp_path) -> None:
    database = tmp_path / "test.db"
    engine = build_engine(Settings(database_url=f"sqlite+aiosqlite:///{database.as_posix()}"))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        business = Business(name="店", maps_url="https://google.com/maps/place/x")
        session.add(business)
        await session.flush()
        session.add(Review(business_id=business.id, content_hash="a" * 64, text="一"))
        await session.commit()

        session.add(Review(business_id=business.id, content_hash="a" * 64, text="二"))
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()
