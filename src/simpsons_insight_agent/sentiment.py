from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lingua import LanguageDetectorBuilder

from .config import Settings, get_settings
from .privacy import normalize_text


@dataclass(slots=True)
class SentimentResult:
    sentiment: str
    confidence: float
    scores: dict[str, float]
    model_id: str
    language: str | None


_TAIWAN_POSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?<!不)(?:好吃|好喝|美味|過癮|涮嘴|推薦|值得|滿意|親切|新鮮|讚)",
        r"(?:會|想|值得)(?:再來|回訪)|下次還會",
        r"吃(?:了|超過)?\s*\d+\s*年|從.{1,12}開始到現在|老顧客|老主顧",
        r"love|great|excellent|friendly|recommend",
    )
)

_TAIWAN_NEGATIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"難吃|踩雷|雷店|失望|糟糕|退步|變差|態度差|不推薦",
        r"沒滋沒味|沒味道|不入味|太淡|死鹹|太鹹|太油|油膩|不新鮮",
        r"不是(?:炒飯|蛋包飯|麵|湯)|醬油拌飯|像在吃白飯|只是白飯",
        r"等(?:了|超過)?.{0,8}(?:分鐘|小時)|太慢|漏單|送錯|沒熟|異物",
        r"terrible|bad|slow|rude|disappointed|never again",
    )
)


class SentimentAnalyzer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pipeline: Any = None
        self._tokenizer: Any = None
        self._model: Any = None
        self._device = -1
        self._load_error: str | None = None
        self._language_detector = LanguageDetectorBuilder.from_all_languages().build()

    def analyze(self, text: str) -> SentimentResult:
        normalized = normalize_text(text)
        language = self.detect_language(normalized)
        if not normalized:
            return SentimentResult(
                sentiment="rating_only",
                confidence=1.0,
                scores={},
                model_id=self.settings.sentiment_model,
                language=language,
            )

        try:
            self._ensure_pipeline()
            try:
                totals, total_weight = self._predict_chunks(normalized)
            except Exception:
                if self._device != 0:
                    raise
                self._move_pipeline_to_cpu()
                totals, total_weight = self._predict_chunks(normalized)
            scores = {key: value / max(total_weight, 1) for key, value in totals.items()}
            sentiment = max(scores, key=scores.get)  # type: ignore[arg-type]
            return SentimentResult(
                sentiment=sentiment,
                confidence=scores[sentiment],
                scores=scores,
                model_id=self.settings.sentiment_model,
                language=language,
            )
        except Exception as exc:
            self._load_error = str(exc)
            return self._heuristic(normalized, language)

    def analyze_many(self, texts: list[str]) -> list[SentimentResult]:
        normalized = [normalize_text(text) for text in texts]
        languages = [self.detect_language(text) for text in normalized]
        results: list[SentimentResult | None] = [None] * len(texts)
        active = [index for index, text in enumerate(normalized) if text]
        for index, text in enumerate(normalized):
            if not text:
                results[index] = SentimentResult(
                    sentiment="rating_only",
                    confidence=1.0,
                    scores={},
                    model_id=self.settings.sentiment_model,
                    language=languages[index],
                )
        if not active:
            return [item for item in results if item is not None]

        try:
            self._ensure_pipeline()
            values = [normalized[index] for index in active]
            try:
                raw_results = self._pipeline(
                    values,
                    top_k=None,
                    truncation=True,
                    batch_size=self.settings.sentiment_batch_size,
                )
            except Exception:
                if self._device != 0:
                    raise
                self._move_pipeline_to_cpu()
                raw_results = self._pipeline(
                    values,
                    top_k=None,
                    truncation=True,
                    batch_size=self.settings.sentiment_batch_size,
                )
            for index, raw in zip(active, raw_results, strict=True):
                rows = raw[0] if raw and isinstance(raw[0], list) else raw
                scores = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
                for item in rows:
                    scores[_canonical_label(str(item["label"]))] += float(item["score"])
                sentiment = max(scores, key=scores.get)  # type: ignore[arg-type]
                results[index] = SentimentResult(
                    sentiment=sentiment,
                    confidence=scores[sentiment],
                    scores=scores,
                    model_id=self.settings.sentiment_model,
                    language=languages[index],
                )
        except Exception as exc:
            self._load_error = str(exc)
            for index in active:
                results[index] = self._heuristic(normalized[index], languages[index])
        return [item for item in results if item is not None]

    def detect_language(self, text: str) -> str | None:
        if not text:
            return None
        try:
            language = self._language_detector.detect_language_of(text)
            return language.iso_code_639_1.name.lower() if language else None
        except Exception:
            return None

    def _ensure_pipeline(self) -> None:
        if self._pipeline is not None:
            return
        if self._load_error:
            raise RuntimeError(self._load_error)
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.settings.sentiment_model,
            cache_dir=self.settings.model_cache_dir,
            local_files_only=self.settings.model_local_files_only,
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.settings.sentiment_model,
            cache_dir=self.settings.model_cache_dir,
            local_files_only=self.settings.model_local_files_only,
            use_safetensors=True,
        )
        self._device = 0 if torch.cuda.is_available() else -1
        self._pipeline = pipeline(
            "text-classification",
            model=self._model,
            tokenizer=self._tokenizer,
            device=self._device,
        )

    def _predict_chunks(self, text: str) -> tuple[dict[str, float], int]:
        totals = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        total_weight = 0
        for chunk in self._chunks(text):
            raw = self._pipeline(chunk, top_k=None, truncation=True)
            result = raw[0] if raw and isinstance(raw[0], list) else raw
            weight = max(len(chunk), 1)
            for item in result:
                label = _canonical_label(str(item["label"]))
                if label in totals:
                    totals[label] += float(item["score"]) * weight
            total_weight += weight
        return totals, total_weight

    def _move_pipeline_to_cpu(self) -> None:
        from transformers import pipeline

        self._device = -1
        self._model.to("cpu")
        self._pipeline = pipeline(
            "text-classification",
            model=self._model,
            tokenizer=self._tokenizer,
            device=-1,
        )

    def _chunks(self, text: str) -> list[str]:
        token_ids = self._tokenizer.encode(text, add_special_tokens=False)
        if len(token_ids) <= 510:
            return [text]
        chunks: list[str] = []
        for start in range(0, len(token_ids), 448):
            piece = token_ids[start : start + 510]
            chunks.append(self._tokenizer.decode(piece, skip_special_tokens=True))
        return chunks

    def _heuristic(self, text: str, language: str | None) -> SentimentResult:
        positive_words = (
            "好吃",
            "推薦",
            "親切",
            "滿意",
            "優秀",
            "乾淨",
            "方便",
            "love",
            "great",
            "excellent",
            "friendly",
            "recommend",
        )
        negative_words = (
            "難吃",
            "失望",
            "糟糕",
            "態度差",
            "太慢",
            "髒",
            "不推薦",
            "terrible",
            "bad",
            "slow",
            "rude",
            "disappointed",
        )
        lowered = text.lower()
        positive = sum(lowered.count(word) for word in positive_words)
        negative = sum(lowered.count(word) for word in negative_words)
        if positive == negative == 0:
            sentiment, confidence = "neutral", 0.5
        elif positive >= negative:
            sentiment, confidence = "positive", min(0.55 + 0.08 * (positive - negative), 0.85)
        else:
            sentiment, confidence = "negative", min(0.55 + 0.08 * (negative - positive), 0.85)
        remaining = max(1.0 - confidence, 0.0)
        scores = {
            "positive": confidence if sentiment == "positive" else remaining / 2,
            "neutral": confidence if sentiment == "neutral" else remaining / 2,
            "negative": confidence if sentiment == "negative" else remaining / 2,
        }
        return SentimentResult(
            sentiment=sentiment,
            confidence=confidence,
            scores=scores,
            model_id="heuristic-fallback",
            language=language,
        )


def rating_sentiment(rating: int | None) -> str | None:
    if rating is None:
        return None
    if rating <= 2:
        return "negative"
    if rating == 3:
        return "neutral"
    return "positive"


def calibrate_sentiment(
    result: SentimentResult,
    text: str,
    rating: int | None = None,
) -> SentimentResult:
    """Blend model output with Taiwanese review phrasing and an optional star prior.

    Text remains the primary signal. Star ratings resolve ambiguous language but
    explicit complaint or praise phrases can still override a conflicting rating.
    """

    normalized = normalize_text(text)
    if not normalized or result.sentiment == "rating_only":
        return result

    scores = {
        label: max(float(result.scores.get(label, 0.0)), 0.0)
        for label in ("positive", "neutral", "negative")
    }
    if not any(scores.values()):
        scores[result.sentiment] = max(result.confidence, 0.5)

    positive_hits = sum(bool(pattern.search(normalized)) for pattern in _TAIWAN_POSITIVE_PATTERNS)
    negative_hits = sum(bool(pattern.search(normalized)) for pattern in _TAIWAN_NEGATIVE_PATTERNS)
    scores["positive"] += 0.62 * positive_hits
    scores["negative"] += 0.62 * negative_hits

    if rating == 5:
        scores["positive"] += 0.52
    elif rating == 4:
        scores["positive"] += 0.34
    elif rating == 3:
        scores["neutral"] += 0.18
    elif rating == 2:
        scores["negative"] += 0.34
    elif rating == 1:
        scores["negative"] += 0.52

    total = max(sum(scores.values()), 1.0)
    normalized_scores = {label: value / total for label, value in scores.items()}
    sentiment = max(normalized_scores, key=normalized_scores.get)  # type: ignore[arg-type]
    calibrated = positive_hits > 0 or negative_hits > 0 or rating is not None
    model_id = result.model_id
    if calibrated and not model_id.endswith("+tw-context-v1"):
        model_id = f"{model_id}+tw-context-v1"
    return SentimentResult(
        sentiment=sentiment,
        confidence=normalized_scores[sentiment],
        scores=normalized_scores,
        model_id=model_id,
        language=result.language,
    )


def _canonical_label(label: str) -> str:
    lowered = label.lower()
    if "pos" in lowered or lowered in {"label_2", "2"}:
        return "positive"
    if "neg" in lowered or lowered in {"label_0", "0"}:
        return "negative"
    return "neutral"
