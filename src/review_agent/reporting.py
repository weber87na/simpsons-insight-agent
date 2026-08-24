from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

ASPECT_LABELS = {
    "product_quality": "產品／品質",
    "service": "服務",
    "price_value": "價格／價值",
    "environment": "環境",
    "speed_wait": "效率／等候",
    "convenience_accessibility": "便利／可及性",
    "brand_reputation": "品牌聲譽",
    "marketing_communication": "行銷／溝通",
    "workplace": "職場／雇主",
    "trust_safety": "信任／安全",
    "other": "其他",
}


def build_aggregate(
    *,
    business: dict,
    reviews: list[dict],
    model_id: str | None,
) -> dict:
    normalized = [
        {
            **item,
            "source": item.get("source") or "google_maps",
            "content_type": item.get("content_type") or "review",
        }
        for item in reviews
    ]
    overall = _aggregate_scope(normalized)
    source_names = list(dict.fromkeys(item["source"] for item in normalized))
    sources = {
        source: _aggregate_scope([item for item in normalized if item["source"] == source])
        for source in source_names
    }
    local_models = sorted(
        {
            str(item["local_model_id"])
            for item in normalized
            if item.get("local_model_id")
        }
    )
    sentiment_values = [item.get("sentiment") for item in normalized]
    sentiment_status = (
        "no_data"
        if not normalized
        else "completed"
        if all(sentiment_values)
        else "partial"
    )
    content_types = Counter(item["content_type"] for item in normalized)
    signals = Counter(
        str((item.get("platform_data") or {}).get("signal"))
        for item in normalized
        if (item.get("platform_data") or {}).get("signal")
    )
    thread_counts: Counter[tuple[str, str, str]] = Counter()
    thread_interactions: Counter[tuple[str, str, str]] = Counter()
    thread_meta: dict[tuple[str, str, str], dict[str, str | None]] = {}
    for item in normalized:
        thread_id = item.get("thread_source_id")
        if thread_id:
            key = (item["source"], str(thread_id), item.get("title") or "未命名討論串")
            thread_counts[key] += 1
            thread_interactions[key] += int(
                (item.get("platform_data") or {}).get("reaction_count") or 0
            )
            thread_meta[key] = {
                "source_url": item.get("source_url"),
                "board": item.get("board"),
            }

    result = {
        "schema_version": 2,
        "business": business,
        "subject": business,
        **overall,
        "overall": overall,
        "sources": sources,
        "sentiment_analysis": {
            "status": sentiment_status,
            "text_item_count": overall["text_review_count"],
            "rating_only_count": overall["sentiment_distribution"].get("rating_only", 0),
            "models": local_models,
        },
        "content_types": dict(content_types),
        "platform_signals": dict(signals),
        "top_threads": [
            {
                "source": source,
                "thread_id": thread_id,
                "title": title,
                "item_count": thread_counts[(source, thread_id, title)],
                "interaction_count": thread_interactions[(source, thread_id, title)],
                **thread_meta[(source, thread_id, title)],
            }
            for source, thread_id, title in sorted(
                thread_counts,
                key=lambda key: (thread_interactions[key], thread_counts[key]),
                reverse=True,
            )[:10]
        ],
        "llm_model": model_id,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    return result


def _aggregate_scope(items: list[dict]) -> dict:
    rating_counts = Counter(
        str(item["rating"])
        for item in items
        if item.get("source") == "google_maps" and item.get("rating")
    )
    sentiment_counts = Counter(
        item.get("sentiment", "unknown") for item in items if item.get("sentiment")
    )
    conflict_count = sum(
        bool(item.get("rating_text_conflict"))
        for item in items
        if item.get("source") == "google_maps"
    )
    aspect_counts: dict[str, Counter] = defaultdict(Counter)
    monthly: dict[str, Counter] = defaultdict(Counter)
    praise_points: Counter[str] = Counter()
    complaint_points: Counter[str] = Counter()

    for item in items:
        sentiment = item.get("sentiment") or "unknown"
        for aspect in item.get("aspects", []):
            aspect_counts[aspect][sentiment] += 1
        points = [str(point).strip() for point in item.get("key_points", []) if str(point).strip()]
        if sentiment == "positive":
            praise_points.update(points)
        elif sentiment == "negative":
            complaint_points.update(points)
        published = item.get("published_at_estimated")
        if isinstance(published, datetime) and item.get("date_precision") in {
            "day",
            "week",
            "month",
        }:
            monthly[published.strftime("%Y-%m")][sentiment] += 1

    positive = sorted(
        (item for item in items if item.get("sentiment") == "positive" and item.get("text")),
        key=lambda item: item.get("confidence", 0),
        reverse=True,
    )[:5]
    negative = sorted(
        (item for item in items if item.get("sentiment") == "negative" and item.get("text")),
        key=lambda item: item.get("confidence", 0),
        reverse=True,
    )[:5]

    return {
        "sample_size": len(items),
        "text_review_count": sum(bool(item.get("text")) for item in items),
        "rating_distribution": dict(sorted(rating_counts.items())),
        "sentiment_distribution": dict(sentiment_counts),
        "rating_text_conflict_count": conflict_count,
        "aspects": {
            key: {"label": ASPECT_LABELS.get(key, key), **dict(counts)}
            for key, counts in aspect_counts.items()
        },
        "monthly_trend": {key: dict(monthly[key]) for key in sorted(monthly)},
        "common_praise": [
            {"text": text, "count": count} for text, count in praise_points.most_common(8)
        ],
        "common_complaints": [
            {"text": text, "count": count} for text, count in complaint_points.most_common(8)
        ],
        "representative_positive": [_representative(item) for item in positive],
        "representative_negative": [_representative(item) for item in negative],
    }


def deterministic_summary(aggregate: dict) -> dict:
    sentiments = aggregate.get("sentiment_distribution", {})
    total = max(sum(sentiments.values()), 1)
    positive = sentiments.get("positive", 0)
    negative = sentiments.get("negative", 0)
    aspects = aggregate.get("aspects", {})
    negative_aspects = sorted(
        aspects.values(), key=lambda item: item.get("negative", 0), reverse=True
    )
    top_issue = negative_aspects[0]["label"] if negative_aspects else "尚無足夠面向資料"
    source_differences = [
        f"{source}：{values.get('sample_size', 0)} 筆樣本"
        for source, values in aggregate.get("sources", {}).items()
    ]
    return {
        "summary": (
            f"共分析 {aggregate.get('sample_size', 0)} 筆跨平台內容；"
            f"正面約 {positive / total:.0%}、負面約 {negative / total:.0%}。"
        ),
        "strengths": ["請查看正面代表內容與各來源情感分布。"],
        "weaknesses": [f"目前負面意見較集中的面向：{top_issue}。"],
        "risks": ["各平台使用族群與內容型態不同，整體比例不是母體民調。"],
        "recommendations": ["優先人工檢視高信心負面內容並比較各來源差異。"],
        "source_differences": source_differences,
    }


def _representative(item: dict) -> dict:
    text = item.get("redacted_text") or item.get("text") or ""
    return {
        "review_id": item["id"],
        "source": item.get("source") or "google_maps",
        "content_type": item.get("content_type") or "review",
        "title": item.get("title"),
        "rating": item.get("rating"),
        "text": text[:300],
        "confidence": item.get("confidence"),
    }
