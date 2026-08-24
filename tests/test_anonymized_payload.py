import json

from simpsons_insight_agent.jobs import build_anonymized_payload
from simpsons_insight_agent.models import Review


def test_openai_payload_excludes_author_and_source_metadata_and_redacts_text() -> None:
    review = Review(
        id="review-1",
        business_id="business-1",
        content_hash="a" * 64,
        author_name="王小明",
        rating=1,
        text="請聯絡 test@example.com 或 0912-345-678，見 https://example.com",
        source_url="https://google.com/maps/profile/private",
    )
    payload = build_anonymized_payload([review])
    serialized = json.dumps(payload, ensure_ascii=False)

    assert set(payload[0]) == {"review_key", "rating", "published_at", "text"}
    assert "王小明" not in serialized
    assert "test@example.com" not in serialized
    assert "0912-345-678" not in serialized
    assert "https://example.com" not in serialized
    assert "profile/private" not in serialized


def test_forum_openai_payload_includes_context_but_excludes_local_identity_and_url() -> None:
    review = Review(
        id="forum-1",
        business_id="business-1",
        source="ptt",
        content_type="comment",
        source_item_id="push-1",
        content_hash="b" * 64,
        author_hash="secret-author-hash",
        title="討論 user@example.com",
        board="Food",
        rating=None,
        text="內容 0912-345-678",
        source_url="https://www.ptt.cc/bbs/Food/M.1.html",
    )
    payload = build_anonymized_payload([review])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert set(payload[0]) == {
        "review_key",
        "rating",
        "published_at",
        "text",
        "source",
        "content_type",
        "thread_title",
    }
    assert payload[0]["source"] == "ptt"
    assert "secret-author-hash" not in serialized
    assert "user@example.com" not in serialized
    assert "0912-345-678" not in serialized
    assert "ptt.cc" not in serialized
