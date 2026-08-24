from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from review_agent.config import Settings
from review_agent.forum_sources import DcardSource, PttSource
from review_agent.schemas import DcardSourceConfig
from review_agent.sources import CollectedItem, SourceCallbacks, SourceCheckpoint


def callbacks(items: list[CollectedItem]) -> SourceCallbacks:
    async def on_batch(batch: list[CollectedItem]) -> None:
        items.extend(batch)

    async def on_progress(
        _posts: int, _comments: int, _target: int, _message: str
    ) -> None:
        return None

    async def is_canceled() -> bool:
        return False

    async def no_message(_message: str) -> None:
        return None

    async def no_values(_values: dict) -> None:
        return None

    return SourceCallbacks(
        on_batch=on_batch,
        on_progress=on_progress,
        is_canceled=is_canceled,
        on_verification=no_message,
        on_metrics=no_values,
        on_metadata=no_values,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PTT_TESTS") != "1",
    reason="set RUN_LIVE_PTT_TESTS=1 to run the respectful one-article smoke test",
)
async def test_live_ptt_one_article(tmp_path: Path) -> None:
    gathered: list[CollectedItem] = []
    source = PttSource(Settings(author_hash_key_path=tmp_path / "ptt-live.key"))
    result = await source.collect(
        config={
            "boards": ["Food"],
            "keywords": ["食記"],
            "date_from": (date.today() - timedelta(days=365)).isoformat(),
            "date_to": date.today().isoformat(),
            "max_posts": 1,
            "max_comments": 3,
            "max_comments_per_thread": 3,
        },
        checkpoint=SourceCheckpoint(),
        callbacks=callbacks(gathered),
    )
    assert result.post_count <= 1
    assert result.comment_count <= 3
    assert gathered


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_DCARD_URL_TESTS") != "1" or not os.getenv("LIVE_DCARD_URL"),
    reason="set RUN_LIVE_DCARD_URL_TESTS=1 and LIVE_DCARD_URL to run one public URL",
)
async def test_live_dcard_one_public_url(tmp_path: Path) -> None:
    gathered: list[CollectedItem] = []
    config = DcardSourceConfig.model_validate(
        {
            "urls": [os.environ["LIVE_DCARD_URL"]],
            "date_from": (date.today() - timedelta(days=365)).isoformat(),
            "date_to": date.today().isoformat(),
            "max_posts": 1,
            "max_comments": 3,
            "max_comments_per_thread": 3,
            "acknowledge_terms": True,
        }
    ).model_dump(mode="json")
    source = DcardSource(Settings(author_hash_key_path=tmp_path / "dcard-live.key"))
    result = await source.collect(
        config=config,
        checkpoint=SourceCheckpoint(),
        callbacks=callbacks(gathered),
    )
    assert result.post_count <= 1
    assert result.comment_count <= 3
    assert gathered
