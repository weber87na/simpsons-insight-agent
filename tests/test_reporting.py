from datetime import UTC, datetime

from simpsons_insight_agent.reporting import build_aggregate


def test_deterministic_aggregation_counts_conflicts_aspects_and_months() -> None:
    reviews = [
        {
            "id": "r1",
            "rating": 5,
            "text": "很好",
            "redacted_text": "很好",
            "sentiment": "positive",
            "confidence": 0.9,
            "rating_text_conflict": False,
            "aspects": ["service"],
            "key_points": ["服務親切", "服務親切"],
            "published_at_estimated": datetime(2026, 8, 1, tzinfo=UTC),
            "date_precision": "day",
        },
        {
            "id": "r2",
            "rating": 5,
            "text": "很慢",
            "redacted_text": "很慢",
            "sentiment": "negative",
            "confidence": 0.8,
            "rating_text_conflict": True,
            "aspects": ["speed_wait"],
            "key_points": ["等候太久"],
            "published_at_estimated": datetime(2025, 1, 1, tzinfo=UTC),
            "date_precision": "year",
        },
    ]
    result = build_aggregate(business={"name": "測試"}, reviews=reviews, model_id="model")

    assert result["sample_size"] == 2
    assert result["rating_text_conflict_count"] == 1
    assert result["aspects"]["service"]["positive"] == 1
    assert result["common_praise"][0] == {"text": "服務親切", "count": 2}
    assert result["common_complaints"][0]["text"] == "等候太久"
    assert "2026-08" in result["monthly_trend"]
    assert "2025-01" not in result["monthly_trend"]


def test_cross_platform_aggregation_weights_each_item_and_limits_ratings_to_google() -> None:
    reviews = [
        {
            "id": "google",
            "source": "google_maps",
            "content_type": "review",
            "rating": 5,
            "text": "很好",
            "sentiment": "positive",
            "confidence": 0.9,
            "rating_text_conflict": False,
            "aspects": [],
            "key_points": [],
            "date_precision": "unknown",
        },
        {
            "id": "ptt-post",
            "source": "ptt",
            "content_type": "post",
            "rating": None,
            "text": "普通",
            "sentiment": "neutral",
            "confidence": 0.8,
            "rating_text_conflict": False,
            "aspects": [],
            "key_points": [],
            "date_precision": "unknown",
            "platform_data": {},
        },
        {
            "id": "ptt-push",
            "source": "ptt",
            "content_type": "comment",
            "rating": None,
            "text": "推",
            "sentiment": "positive",
            "confidence": 0.95,
            "rating_text_conflict": False,
            "aspects": [],
            "key_points": [],
            "date_precision": "unknown",
            "platform_data": {"signal": "push"},
        },
    ]
    result = build_aggregate(business={"name": "品牌"}, reviews=reviews, model_id=None)
    assert result["sample_size"] == 3
    assert result["sentiment_distribution"] == {"positive": 2, "neutral": 1}
    assert result["sources"]["google_maps"]["sample_size"] == 1
    assert result["sources"]["ptt"]["sample_size"] == 2
    assert result["rating_distribution"] == {"5": 1}
    assert result["sources"]["ptt"]["rating_distribution"] == {}
    assert result["content_types"] == {"review": 1, "post": 1, "comment": 1}
    assert result["platform_signals"] == {"push": 1}
