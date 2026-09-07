"""Versioned, report-scoped analytics; never infer population prevalence from samples."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
from sqlalchemy import select

from .models import JobReview, Report, Review, ReviewAnalysis, ReviewEmbedding, TopicVersion
from .privacy import redact_pii

TAIPEI = timezone(timedelta(hours=8))
TOPIC_VERSION = "centroid-v1"


def local_date(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(TAIPEI).date()


def bucket(day: date, interval: str) -> date:
    if interval == "day":
        return day
    return day - timedelta(days=day.weekday()) if interval == "week" else day.replace(day=1)


def next_bucket(day: date, interval: str) -> date:
    if interval == "day":
        return day + timedelta(days=1)
    if interval == "week":
        return day + timedelta(days=7)
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def trends(
    items: list[dict],
    *,
    interval="month",
    date_from=None,
    date_to=None,
    source=None,
    topic_key=None,
):
    if interval not in {"day", "week", "month"}:
        raise ValueError("interval 必須是 day、week 或 month")
    start = date.fromisoformat(date_from) if isinstance(date_from, str) else date_from
    end = date.fromisoformat(date_to) if isinstance(date_to, str) else date_to
    if start and end and start > end:
        raise ValueError("起始日不可晚於結束日")
    scoped = {
        x["id"]: x
        for x in items
        if (not source or x["source"] == source)
        and (not topic_key or topic_key in x.get("topic_keys", []))
    }
    eligible = []
    excluded = 0
    for x in scoped.values():
        day = local_date(x.get("published_at"))
        precision = x.get("date_precision")
        if day is None or precision not in (
            {"day"} if interval == "day" else {"day", "week"} if interval == "week" else {"day", "week", "month"}
        ):
            excluded += 1
            continue
        if start and day < start or end and day > end:
            continue
        eligible.append((day, x))
    start = start or (min(d for d, _ in eligible) if eligible else end)
    end = end or (max(d for d, _ in eligible) if eligible else start)
    points = []
    if start and end:
        if start > end or (end - start).days > 366 * 30:
            raise ValueError("分析範圍需介於 0 至 30 年")
        counts: dict[date, Counter] = {}
        sources: dict[date, Counter] = {}
        for day, x in eligible:
            sources.setdefault(bucket(day, interval), Counter())[x["source"]] += 1
            counts.setdefault(bucket(day, interval), Counter())[
                x.get("sentiment") or "unknown"
            ] += 1
        cursor = bucket(start, interval)
        while cursor <= end:
            c = counts.get(cursor, Counter())
            classified = sum(c[s] for s in ("negative", "neutral", "positive"))
            points.append(
                {
                    "period": cursor.isoformat(),
                    "period_end": min(end, next_bucket(cursor, interval)-timedelta(days=1)).isoformat(),
                    "positive_count": c["positive"],
                    "neutral_count": c["neutral"],
                    "rating_only_count": c["rating_only"],
                    "unknown_count": c["unknown"],
                    "pn_ratio": c["positive"]/c["negative"] if c["negative"] else None,
                    "pn_reason": None if c["negative"] else "no_negative" if classified else "no_classified_text",
                    "source_counts": dict(sources.get(cursor, {})),
                    "count": sum(c.values()),
                    "classified_count": classified,
                    "negative_count": c["negative"],
                    "negative_ratio": c["negative"] / classified if classified else None,
                }
            )
            cursor = next_bucket(cursor, interval)
    return {
        "interval": interval,
        "timezone": "Asia/Taipei",
        "date_from": start.isoformat() if start else None,
        "date_to": end.isoformat() if end else None,
        "excluded_count": excluded,
        "included_count": len(eligible),
        "points": points,
        "note": "負評比例以已分類文字為分母；相對日期為估計值，統計僅代表已蒐集樣本。",
    }


async def snapshot_items(session, report):
    from .analytics import platform_snapshot, safe_url
    # V3 snapshots prevent later recollection from changing historical reports.
    if "analytics_items" in report.payload:
        return report.payload["analytics_items"]
    rows = (
        await session.execute(
            select(Review, ReviewAnalysis)
            .join(JobReview, JobReview.review_id == Review.id)
            .outerjoin(
                ReviewAnalysis,
                (ReviewAnalysis.review_id == Review.id) & (ReviewAnalysis.job_id == report.job_id),
            )
            .where(JobReview.job_id == report.job_id)
            .order_by(JobReview.ordinal)
        )
    ).all()
    return [
        {
            "id": r.id,
            "title": redact_pii(r.title) if r.title else None,
            "board": r.board,
            "thread_source_id": r.thread_source_id,
            "parent_source_id": r.parent_source_id,
            "source_url": safe_url(r.source_url),
            "rating": r.rating,
            "relative_date": r.relative_date,
            "owner_reply": redact_pii(r.owner_reply) if r.owner_reply else None,
            "language": r.language,
            "confidence": a.confidence if a else None,
            "rating_sentiment": a.rating_sentiment if a else None,
            "rating_text_conflict": a.rating_text_conflict if a else False,
            "channel_label": (report.payload.get("subject") or report.payload.get("business") or {}).get("name") if r.source == "google_maps" else r.board,
            "metrics": {k: v for k, v in (r.platform_data or {}).items() if k in {"reply_count", "comment_count", "like_count", "push_count", "boo_count", "reaction_count"} and isinstance(v, (int, float)) and v >= 0},
            "platform_data": platform_snapshot(r.platform_data),
            "source": r.source,
            "content_type": r.content_type,
            "text": redact_pii(r.redacted_text or r.text),
            "published_at": r.published_at_estimated.isoformat()
            if r.published_at_estimated
            else None,
            "date_precision": r.date_precision,
            "sentiment": a.sentiment if a else None,
            "aspects": a.aspects if a else [],
            "negative_aspects": a.negative_aspects if a else [],
            "key_points": [redact_pii(k) for k in (a.key_points or [])] if a else [],
            "topic_keys": [],
        }
        for r, a in rows
    ]


def keywords(items):
    terms: Counter = Counter()
    for item in items:
        points = item.get("key_points") or []
        if points:
            terms.update(set(points))
        else:
            words = re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", item["text"])
            terms.update(set(w[:16] for w in words if w not in {"這個", "真的", "但是", "覺得"}))
    return [word for word, _ in terms.most_common(5)]


def cluster_topics(items, vectors, previous, model_id):
    groups: list[dict] = []
    missing = 0
    for item in sorted(items, key=lambda x: x["id"]):
        if (item.get("sentiment") != "negative" and not item.get("negative_aspects")) or not item["text"].strip():
            continue
        v = vectors.get(item["id"])
        if v is None or not np.all(np.isfinite(v)) or not np.linalg.norm(v):
            missing += 1
            continue
        v = v / np.linalg.norm(v)
        similarities = [
            float(np.dot(g["center"], v)) if g["center"].shape == v.shape else -1 for g in groups
        ]
        best = int(np.argmax(similarities)) if similarities else -1
        if best >= 0 and similarities[best] >= 0.80:
            g = groups[best]
            g["items"].append(item)
            g["sum"] += v
            g["center"] = g["sum"] / np.linalg.norm(g["sum"])
        else:
            groups.append({"items": [item], "sum": v.copy(), "center": v.copy()})
    topics = []
    used: set[str] = set()
    for g in groups:
        center = g["center"]
        matches = []
        for old in previous:
            if old.get("embedding_model") == model_id and len(old.get("centroid", [])) == len(
                center
            ):
                matches.append((float(np.dot(center, old["centroid"])), old["topic_key"]))
        matches.sort(reverse=True)
        match = (
            matches[0]
            if matches
            and matches[0][0] >= 0.90
            and (len(matches) == 1 or matches[0][0] - matches[1][0] >= 0.05)
            else None
        )
        key = match[1] if match and match[1] not in used else str(uuid4())
        used.add(key)
        words = keywords(g["items"])
        topics.append(
            {
                "topic_key": key,
                "name": words[0] if words else "待命名抱怨",
                "keywords": words,
                "item_ids": [x["id"] for x in g["items"]],
                "count": len(g["items"]),
                "provisional": len(g["items"]) < 2,
                "mapping": "matched" if match and key == match[1] else "new",
                "centroid": center.tolist(),
                "embedding_model": model_id,
                "version": TOPIC_VERSION,
            }
        )
        for item in g["items"]:
            item["topic_keys"] = [key]
    return sorted(topics, key=lambda x: -x["count"]), missing


async def enrich_report(session, report, model_id):
    if "analytics_items" in report.payload:
        return
    items = await snapshot_items(session, report)
    embeddings = (
        await session.scalars(
            select(ReviewEmbedding).where(
                ReviewEmbedding.review_id.in_([x["id"] for x in items]),
                ReviewEmbedding.model_id == model_id,
            )
        )
    ).all()
    vectors: dict[str, np.ndarray] = {
        e.review_id: np.frombuffer(e.vector, dtype=np.float32, count=e.dimension)
        for e in embeddings
    }
    old_report = await session.scalar(
        select(Report)
        .where(
            Report.business_id == report.business_id,
            Report.id != report.id,
            Report.created_at < report.created_at,
        )
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    previous = (
        list(
            (
                await session.scalars(
                    select(TopicVersion).where(TopicVersion.report_id == old_report.id)
                )
            ).all()
        )
        if old_report
        else []
    )
    topics, missing = cluster_topics(items, vectors, [t.payload for t in previous], model_id)
    for topic in topics:
        session.add(TopicVersion(report_id=report.id, topic_key=topic["topic_key"], payload=topic))
    public = [{k: v for k, v in t.items() if k != "centroid"} for t in topics]
    report.payload = {
        **report.payload,
        "schema_version": 4,
        "analytics_items": items,
        "topics": public,
        "topic_analysis": {
            "version": TOPIC_VERSION,
            "status": "partial" if missing else "completed",
            "missing_embeddings": missing,
        },
        "trends": trends(items),
        "decision": {"status": "NOT_STARTED"},
    }
