from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from simpsons_insight_agent.schemas import DcardSourceConfig, JobSourceResponse, PttSourceConfig


def test_forum_search_defaults_and_normalized_inputs() -> None:
    ptt = PttSourceConfig(boards=[" Food ", "Food"], keywords=["店家", " 店家 "])
    assert ptt.boards == ["Food"]
    assert ptt.keywords == ["店家"]
    assert ptt.max_search_pages == 20
    dcard = DcardSourceConfig(
        keywords=[" 店家 ", "店家"], forums=["Food", " food "], acknowledge_terms=True
    )
    assert dcard.keywords == ["店家"]
    assert dcard.forums == ["food"]
    assert dcard.max_search_pages == 5
    assert dcard.urls == []
    assert dcard.import_ids == []


@pytest.mark.parametrize("model,maximum", [(PttSourceConfig, 100), (DcardSourceConfig, 20)])
@pytest.mark.parametrize("value", [0, -1, 101, True, 1.5, "5"])
def test_search_page_budgets_require_bounded_integer(model: type, maximum: int, value: object) -> None:
    config = {"keywords": ["店家"], "max_search_pages": value}
    config.update({"boards": ["Food"]} if model is PttSourceConfig else {"acknowledge_terms": True})
    with pytest.raises(ValidationError):
        model.model_validate(config)
    config["max_search_pages"] = maximum
    assert model.model_validate(config).max_search_pages == maximum
    config["max_search_pages"] = maximum + 1
    with pytest.raises(ValidationError):
        model.model_validate(config)


@pytest.mark.parametrize(
    "config",
    [
        {"keywords": [" "]}, {"keywords": ["x" * 101]}, {"keywords": ["a"] * 11},
        {"keywords": [False]}, {"keywords": ["ok"], "forums": ["../food"]},
        {"keywords": ["ok"], "forums": [""]}, {"keywords": ["ok"], "forums": ["food"] * 11},
        {"keywords": ["ok"], "forums": ["美食"]}, {"import_ids": [" "]},
        {"forums": ["food"]}, {},
    ],
)
def test_dcard_rejects_invalid_search_inputs(config: dict) -> None:
    with pytest.raises(ValidationError):
        DcardSourceConfig.model_validate({**config, "acknowledge_terms": True})


def test_dcard_keeps_existing_import_and_url_modes() -> None:
    imported = DcardSourceConfig(import_ids=["batch-1", " batch-1 "], acknowledge_terms=True)
    assert imported.import_ids == ["batch-1"]
    assert imported.keywords == []
    url = DcardSourceConfig(urls=["https://www.dcard.tw/f/food/p/123"], acknowledge_terms=True)
    assert url.keywords == []


def test_dcard_canonicalizes_share_urls_and_deduplicates() -> None:
    config = DcardSourceConfig(
        urls=[
            "https://www.dcard.tw:443/f/Food/p/123/?utm_source=share&ref=app#comment-2",
            "https://www.dcard.tw/f/food/p/123",
        ],
        acknowledge_terms=True,
    )
    assert [str(url) for url in config.urls] == ["https://www.dcard.tw/f/food/p/123"]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.dcard.tw/f/food/p/123", "https://evil.test/f/food/p/123",
        "https://www.dcard.tw.evil.test/f/food/p/123", "https://user@www.dcard.tw/f/food/p/123",
        "https://www.dcard.tw:8443/f/food/p/123", "https://www.dcard.tw/api/v2/posts/123",
        "https://www.dcard.tw/f/food/p/123?redirect=https://evil.test",
        "https://www.dcard.tw/f/food/p/not-a-number",
    ],
)
def test_dcard_urls_keep_public_host_and_route_constraints(url: str) -> None:
    with pytest.raises(ValidationError):
        DcardSourceConfig(urls=[url], acknowledge_terms=True)


def test_dcard_requires_terms_and_valid_date_range_for_search() -> None:
    with pytest.raises(ValidationError, match="條款"):
        DcardSourceConfig(keywords=["店家"])
    with pytest.raises(ValidationError, match="起始日"):
        DcardSourceConfig(
            keywords=["店家"], acknowledge_terms=True,
            date_from=date(2026, 2, 1), date_to=date(2026, 1, 1),
        )


def test_source_response_diagnostics_are_optional_and_serialized() -> None:
    values = {
        "source": "ptt", "status": "PARTIAL", "collected_count": 3, "post_count": 1,
        "comment_count": 2, "target_count": 50, "collection_complete": False,
        "stop_reason": "page_limit", "error": None, "attempt_count": 1, "config": {},
    }
    assert JobSourceResponse(**values).diagnostics == {}
    diagnostics = {"pages_fetched": 20, "partial_reasons": ["page_limit"]}
    response = JobSourceResponse(**values, diagnostics=diagnostics)
    assert response.model_dump()["diagnostics"] == diagnostics
