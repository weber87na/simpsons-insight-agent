import pytest
from pydantic import ValidationError

from simpsons_insight_agent.schemas import CreateJobRequest, DcardSourceConfig, PttSourceConfig


def valid_payload() -> dict:
    return {
        "maps_url": "https://www.google.com/maps/place/test",
        "name": "測試商家",
        "max_reviews": 10,
        "llm_model": "gpt-5.4-mini-2026-03-17",
    }


def test_create_job_contract_accepts_supported_values() -> None:
    request = CreateJobRequest.model_validate(valid_payload())
    assert request.max_reviews == 10


def test_create_job_contract_accepts_taiwan_maps_domain() -> None:
    payload = valid_payload()
    payload["maps_url"] = "https://www.google.com.tw/maps/place/test"
    assert CreateJobRequest.model_validate(payload).maps_url.host == "www.google.com.tw"


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_reviews", 501), ("llm_model", "unknown-model"), ("maps_url", "https://example.com")],
)
def test_create_job_contract_rejects_unsafe_values(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(payload)


def test_multi_source_contract_accepts_ptt_only_subject() -> None:
    request = CreateJobRequest.model_validate(
        {
            "subject": {"kind": "brand", "name": "測試品牌", "aliases": ["別名"]},
            "sources": [
                {
                    "source": "ptt",
                    "boards": ["Food"],
                    "keywords": ["測試品牌"],
                    "date_from": "2025-01-01",
                    "date_to": "2026-01-01",
                }
            ],
        }
    )
    assert request.subject_input().kind == "brand"
    assert request.source_configs()[0].source == "ptt"


def test_forum_contract_enforces_per_thread_cap_and_strict_dcard_url() -> None:
    with pytest.raises(ValidationError):
        PttSourceConfig.model_validate(
            {
                "boards": ["Food"],
                "keywords": ["品牌"],
                "max_comments_per_thread": 101,
            }
        )
    with pytest.raises(ValidationError):
        DcardSourceConfig.model_validate(
            {
                "urls": ["https://www.dcard.tw/f/food/p/123?redirect=https://evil.example"],
                "acknowledge_terms": True,
            }
        )
