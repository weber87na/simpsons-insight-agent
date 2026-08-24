import json

from simpsons_insight_agent.privacy import normalize_text, redact_pii, sanitize_for_openai


def test_normalize_and_redact_known_identifiers() -> None:
    raw = (
        "  聯絡 test@example.com，電話 0912-345-678，"
        "網站 https://example.com/a，帳號 @somebody，訂單 123456789  "
    )
    value = redact_pii(raw)

    assert "test@example.com" not in value
    assert "0912-345-678" not in value
    assert "https://example.com/a" not in value
    assert "@somebody" not in value
    assert "123456789" not in value
    assert "[電子郵件已遮罩]" in value
    assert normalize_text("Ａ  \u200b B") == "A B"


def test_openai_boundary_removes_urls_authors_and_subject_identifiers_recursively() -> None:
    value = sanitize_for_openai(
        {
            "business": {"name": "品牌", "address": "私人地址"},
            "top_threads": [
                {
                    "title": "聯絡 user@example.com",
                    "source_url": "https://www.ptt.cc/bbs/Food/M.1.html",
                    "board": "Food",
                }
            ],
            "items": [{"text": "電話 0912-345-678", "author_hash": "secret"}],
        }
    )
    serialized = json.dumps(value, ensure_ascii=False)
    assert "business" not in value
    assert "ptt.cc" not in serialized
    assert "Food" not in serialized
    assert "secret" not in serialized
    assert "user@example.com" not in serialized
    assert "0912-345-678" not in serialized
