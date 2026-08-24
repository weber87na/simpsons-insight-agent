from types import MethodType

import pytest

from review_agent.config import Settings
from review_agent.sentiment import (
    SentimentAnalyzer,
    SentimentResult,
    calibrate_sentiment,
    rating_sentiment,
)


def make_analyzer(output) -> SentimentAnalyzer:  # type: ignore[no-untyped-def]
    analyzer = SentimentAnalyzer.__new__(SentimentAnalyzer)
    analyzer.settings = Settings()
    analyzer._pipeline = lambda *_args, **_kwargs: output
    analyzer._tokenizer = object()
    analyzer._model = object()
    analyzer._device = -1
    analyzer._load_error = None
    analyzer.detect_language = MethodType(lambda _self, _text: "zh", analyzer)
    analyzer._ensure_pipeline = MethodType(lambda _self: None, analyzer)
    analyzer._chunks = MethodType(lambda _self, text: [text], analyzer)
    return analyzer


@pytest.mark.parametrize(
    "output",
    [
        [
            {"label": "positive", "score": 0.8},
            {"label": "neutral", "score": 0.1},
            {"label": "negative", "score": 0.1},
        ],
        [[
            {"label": "positive", "score": 0.8},
            {"label": "neutral", "score": 0.1},
            {"label": "negative", "score": 0.1},
        ]],
    ],
)
def test_pipeline_accepts_flat_and_nested_transformers_output(output) -> None:  # type: ignore[no-untyped-def]
    result = make_analyzer(output).analyze("非常推薦")
    assert result.sentiment == "positive"
    assert result.confidence == pytest.approx(0.8)


def test_blank_review_is_rating_only() -> None:
    result = make_analyzer([]).analyze("   ")
    assert result.sentiment == "rating_only"


def test_batch_analysis_preserves_input_order_and_rating_only_rows() -> None:
    analyzer = make_analyzer([])
    analyzer._pipeline = lambda values, **_kwargs: [  # type: ignore[method-assign]
        [
            {"label": "positive", "score": 0.8},
            {"label": "neutral", "score": 0.1},
            {"label": "negative", "score": 0.1},
        ]
        for _value in values
    ]
    results = analyzer.analyze_many(["很好", "", "推薦"])
    assert [item.sentiment for item in results] == ["positive", "rating_only", "positive"]


@pytest.mark.parametrize(
    ("rating", "sentiment"),
    [(1, "negative"), (2, "negative"), (3, "neutral"), (4, "positive"), (5, "positive")],
)
def test_rating_sentiment_is_independent(rating: int, sentiment: str) -> None:
    assert rating_sentiment(rating) == sentiment


def model_result(
    sentiment: str,
    *,
    positive: float,
    neutral: float,
    negative: float,
) -> SentimentResult:
    return SentimentResult(
        sentiment=sentiment,
        confidence=max(positive, neutral, negative),
        scores={"positive": positive, "neutral": neutral, "negative": negative},
        model_id="test-model",
        language="zh",
    )


def test_taiwan_context_corrects_long_term_customer_review_with_five_stars() -> None:
    text = "從國中開始到現在，吃超過30年了，好一陣子沒來，今天加料、加飯，過癮。"
    result = calibrate_sentiment(
        model_result("negative", positive=0.08, neutral=0.12, negative=0.8),
        text,
        rating=5,
    )

    assert result.sentiment == "positive"
    assert result.model_id.endswith("+tw-context-v1")


def test_taiwan_context_detects_indirect_food_complaint_despite_three_stars() -> None:
    text = "這炒飯不是炒飯，沒滋沒味，醬油拌飯吧，像在吃白飯。"
    result = calibrate_sentiment(
        model_result("neutral", positive=0.1, neutral=0.75, negative=0.15),
        text,
        rating=3,
    )

    assert result.sentiment == "negative"


def test_explicit_complaint_can_override_high_star_rating() -> None:
    result = calibrate_sentiment(
        model_result("positive", positive=0.65, neutral=0.2, negative=0.15),
        "漏單又等了超過一小時，態度差，不推薦。",
        rating=5,
    )

    assert result.sentiment == "negative"
