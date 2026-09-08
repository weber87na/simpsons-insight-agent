import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simpsons_insight_agent.api import _source_diagnostics, app, settings


def test_app_starts_and_serves_local_pages() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        page = client.get("/")
        assert page.status_code == 200
        assert "開始分析目前評論" in page.text


def test_api_returns_422_before_enqueuing_invalid_job() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={
                "maps_url": "https://example.com/not-maps",
                "name": "錯誤網址",
                "max_reviews": 501,
                "llm_model": "unknown-model",
            },
        )
        assert response.status_code == 422


def test_dcard_import_template_and_strict_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "author_hash_key_path", tmp_path / "api-author.key")
    row = {
        "item_type": "post",
        "source_item_id": "256789012",
        "thread_id": "256789012",
        "parent_id": "",
        "title": "公開文章",
        "text": "公開內容",
        "published_at": "2026-01-01T12:00:00+08:00",
        "forum": "food",
        "source_url": "https://www.dcard.tw/f/food/p/256789012",
        "author": "不應回傳",
        "reaction_count": 1,
    }
    with TestClient(app) as client:
        template = client.get("/api/source-imports/dcard/template?format=json")
        assert template.status_code == 200
        assert set(template.json()[0]) == set(row)

        response = client.post(
            "/api/source-imports/dcard",
            files={
                "file": (
                    "items.json",
                    json.dumps([row], ensure_ascii=False).encode(),
                    "application/json",
                )
            },
        )
        assert response.status_code == 201
        assert response.json()["row_count"] == 1
        assert response.json()["validation_status"] == "VALID"
        assert "不應回傳" not in response.text

        row["source_url"] = "https://evil.example/f/food/p/1"
        invalid = client.post(
            "/api/source-imports/dcard",
            files={"file": ("bad.json", json.dumps([row]).encode(), "application/json")},
        )
        assert invalid.status_code == 422


def test_source_diagnostics_expose_only_safe_counts_and_known_reasons() -> None:
    assert _source_diagnostics({
        "pages_fetched": 3,
        "missing_articles": 1,
        "article_requests": "invalid",
        "duplicate_items": True,
        "collection_scope": "public_html",
        "thread_ids": ["private-id"],
        "thread_urls": ["https://example.test/private"],
        "partial_reasons": ["article_unavailable", "article_unavailable", "private-id", {}],
    }) == {
        "pages_fetched": 3,
        "missing_articles": 1,
        "collection_scope": "public_html",
        "partial_reasons": ["article_unavailable"],
    }
