"""Shared, immutable report scopes for charts, evidence and exports."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from datetime import date
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from .insights import local_date, snapshot_items, trends


def normalized(text):
    return unicodedata.normalize("NFKC", text or "").casefold()


class ReportFilters(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    interval: Literal["day", "week", "month"] = "month"
    precision_policy: Literal["all", "interval"] = "all"
    source: Literal["google_maps", "ptt", "dcard"] | None = None
    content_type: Literal["review", "post", "comment"] | None = None
    sentiment: Literal["positive", "neutral", "negative", "rating_only", "unknown"] | None = None
    aspect: str | None = None
    topic_key: str | None = None
    board: str | None = None
    channel_label: str | None = None
    thread_source_id: str | None = None
    review_id: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    q: str | None = Field(None, max_length=500)
    any_terms: list[str] = Field(default_factory=list, max_length=20)
    all_terms: list[str] = Field(default_factory=list, max_length=20)
    exclude_terms: list[str] = Field(default_factory=list, max_length=20)
    keyword: str | None = Field(None, max_length=100)

    @model_validator(mode="after")
    def validate_filters(self):
        if self.date_from and self.date_to:
            if self.date_from > self.date_to or (self.date_to - self.date_from).days > 366 * 30:
                raise ValueError("日期範圍需依序且不超過30年")
        for field in ("any_terms", "all_terms", "exclude_terms"):
            values = [normalized(v.strip()) for v in getattr(self, field)]
            if any(not v or len(v) > 100 for v in values):
                raise ValueError(f"{field} 每詞需1至100字")
            setattr(self, field, sorted(set(values)))
        return self


def report_filters(request: Request) -> ReportFilters:
    params = request.query_params
    fields = ReportFilters.model_fields
    data = {k: params[k] for k in fields if k in params}
    for key in ("any_terms", "all_terms", "exclude_terms"):
        if key in params:
            data[key] = params.getlist(key)
    try:
        return ReportFilters.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_context=False, include_url=False)) from exc


def safe_url(value):
    try:
        parsed = urlsplit(value or "")
        return (
            value
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
            else None
        )
    except ValueError:
        return None


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def platform_snapshot(data):
    allowed = {
        "reply_count",
        "comment_count",
        "like_count",
        "push_count",
        "boo_count",
        "reaction_count",
        "floor",
    }
    result = {
        k: v for k, v in (data or {}).items() if k in allowed and type(v) in {int, float} and v >= 0
    }
    if (data or {}).get("signal") in {"push", "boo", "neutral"}:
        result["signal"] = data["signal"]
    return result


STOPWORDS = frozenset(
    "這個 那個 我們 你們 他們 自己 就是 但是 因為 所以 然後 可以 一個 沒有 還是 已經 真的 非常 的 了 和 是 在 有 the and for this that with https http www com".split()
)
TOKENIZER_VERSION = "jieba-0.42.1-restaurant-zh-tw-v1-stop-v1"
# Versioned supplement to jieba's bundled dictionary; no runtime download/model.
RESTAURANT_TERMS = tuple(
    "麵包 麵條 牛肉麵 小籠包 鼎泰豐 春水堂 餐廳 餐點 餐具 餐飲 飲料 飲品 服務 服務員 店員 態度 環境 衛生 衛生紙 清潔 廁所 乾淨 髒亂 美味 好吃 難吃 等候 排隊 等待 出餐 效率 速度 價格 價位 昂貴 便宜 份量 品質 食材 新鮮 口味 口感 湯頭 咖啡 珍珠 奶茶 珍珠奶茶 外帶 內用 外送 訂位 預約 菜單 結帳 結賬 買單 停車 交通 位置 分店 商家 店家 品牌 推薦 再訪 回訪 冷氣 空調 吵雜 客人 顧客 抱怨 改善 優點 缺點".split()
)


@lru_cache(maxsize=4)
def tokenizer(brand):
    import jieba

    engine = jieba.Tokenizer()
    engine.initialize()
    for term in (*RESTAURANT_TERMS, *brand):
        if term:
            engine.add_word(normalized(term))
    return engine


def tokens(item, brand=()):
    text = normalized((item.get("title") or "") + " " + (item.get("text") or ""))
    return [
        t
        for t in tokenizer(tuple(brand)).cut(text, HMM=False)
        if len(t.strip()) >= 2
        and t not in STOPWORDS
        and not t.isnumeric()
        and all(c.isalnum() for c in t)
    ]


def brand_terms(report):
    subject = report.payload.get("subject") or report.payload.get("business") or {}
    return tuple(sorted(set([subject.get("name", ""), *subject.get("aliases", [])])))


def filter_items(items, filters, brand=()):
    selected = []
    unknown = imprecise = 0
    unique = {x["id"]: x for x in items}
    for x in unique.values():
        if filters.channel_label and filters.channel_label != (
            x.get("channel_label") or x.get("board") or "未提供頻道"
        ):
            continue
        if any(
            getattr(filters, k) is not None and x.get(k) != getattr(filters, k)
            for k in ("source", "content_type", "board", "thread_source_id", "rating")
        ):
            continue
        if filters.review_id and x["id"] != filters.review_id:
            continue
        if filters.sentiment and (x.get("sentiment") or "unknown") != filters.sentiment:
            continue
        if filters.aspect and filters.aspect not in x.get("aspects", []):
            continue
        if filters.topic_key and filters.topic_key not in x.get("topic_keys", []):
            continue
        if filters.q and filters.q not in (x.get("text") or ""):
            continue
        text = normalized((x.get("title") or "") + " " + (x.get("text") or ""))
        if filters.any_terms and not any(t in text for t in filters.any_terms):
            continue
        if not all(t in text for t in filters.all_terms) or any(
            t in text for t in filters.exclude_terms
        ):
            continue
        if filters.keyword and normalized(filters.keyword) not in tokens(x, brand):
            continue
        day = local_date(x.get("published_at"))
        if day and (
            filters.date_from
            and day < filters.date_from
            or filters.date_to
            and day > filters.date_to
        ):
            continue
        if not day and (
            filters.date_from or filters.date_to or filters.precision_policy == "interval"
        ):
            unknown += 1
            continue
        allowed = {"day": {"day"}, "week": {"day", "week"}, "month": {"day", "week", "month"}}
        if (
            filters.precision_policy == "interval"
            and x.get("date_precision") not in allowed[filters.interval]
        ):
            imprecise += 1
            continue
        selected.append(x)
    return selected, {
        "raw_count": len(items),
        "deduplicated_count": len(unique),
        "included_count": len(selected),
        "unknown_date_count": unknown,
        "imprecise_date_count": imprecise,
        "excluded_count": unknown + imprecise,
    }


async def resolve_scope(session, report, filters):
    items = await snapshot_items(session, report)
    selected, counts = filter_items(items, filters, brand_terms(report))
    snapshot = "analytics_items" in report.payload
    version = report.payload.get("schema_version", 1)
    conditions = filters.model_dump(mode="json")
    # Content digest also distinguishes changing legacy-live data.
    digest = hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    key = hashlib.sha256(
        json.dumps([report.id, version, digest, conditions], sort_keys=True).encode()
    ).hexdigest()[:24]
    metadata = ("title", "board", "thread_source_id", "source_url", "metrics", "rating")
    unavailable = [k for k in metadata if items and not any(k in x for x in items)]
    return selected, {
        **counts,
        "filters": conditions,
        "scope_key": key,
        "data_basis": "snapshot" if snapshot else "legacy_live",
        "snapshot_version": version,
        "unavailable_fields": unavailable,
        "collection": report.payload.get("collection", {}),
    }


def statistics(items):
    counts = Counter(x.get("sentiment") or "unknown" for x in items)
    classified = sum(counts[k] for k in ("positive", "neutral", "negative"))
    neg = counts["negative"]
    return {
        "count": len(items),
        "classified_count": classified,
        **{
            f"{k}_count": counts[k]
            for k in ("positive", "neutral", "negative", "rating_only", "unknown")
        },
        "negative_ratio": neg / classified if classified else None,
        "pn_ratio": counts["positive"] / neg if neg else None,
        "pn_reason": None if neg else "no_negative" if classified else "no_classified_text",
        "source_counts": dict(Counter(x["source"] for x in items)),
        "thread_count": len(
            {(x["source"], x["thread_source_id"]) for x in items if x.get("thread_source_id")}
        ),
    }


def scoped_trends(items, filters, scope):
    result = trends(
        items, interval=filters.interval, date_from=filters.date_from, date_to=filters.date_to
    )
    result.update(scope=scope, excluded_count=scope["excluded_count"])
    result["average_count"] = (
        sum(p["count"] for p in result["points"]) / len(result["points"])
        if result["points"]
        else None
    )
    return result


def channel_rows(items, group_by="channel", sort="sample_count"):
    groups: dict[tuple, list] = {}
    for item in items:
        key = (item["source"], item.get("channel_label") or item.get("board") or "未提供頻道")
        if group_by == "source":
            key = (item["source"], item["source"])
        groups.setdefault(key, []).append(item)
    return sorted(
        [
            {
                "source": s,
                "channel_label": label,
                "board": rows[0].get("board"),
                "sample_count": len(rows),
                **statistics(rows),
            }
            for (s, label), rows in groups.items()
        ],
        key=lambda x: (-(x[sort] if x[sort] is not None else -1), x["source"], x["channel_label"]),
    )


def thread_rows(items):
    groups: dict[tuple, list] = {}
    for item in items:
        if item.get("thread_source_id"):
            groups.setdefault((item["source"], item["thread_source_id"]), []).append(item)
    result = []
    for (source, key), rows in groups.items():
        head = next((x for x in rows if x.get("content_type") == "post"), rows[0])
        replies = [
            (x.get("metrics") or {}).get(
                "reply_count", (x.get("metrics") or {}).get("comment_count")
            )
            for x in rows
        ]
        valid_replies = [v for v in replies if isinstance(v, (int, float))]
        result.append(
            {
                "source": source,
                "thread_source_id": key,
                "title": head.get("title"),
                "collected_count": len(rows),
                "reported_reply_count": max(valid_replies) if valid_replies else None,
                "latest_date": max(
                    (
                        local_date(x.get("published_at"))
                        for x in rows
                        if local_date(x.get("published_at"))
                    ),
                    default=None,
                ),
                "date_precision": next(
                    (
                        x.get("date_precision")
                        for x in sorted_items(rows, "date_desc")
                        if x.get("published_at")
                    ),
                    "unknown",
                ),
                "board": head.get("board"),
                "source_url": safe_url(head.get("source_url")),
                "excerpt": (head.get("text") or "")[:300],
            }
        )
    return result


def sorted_items(items, sort="original"):
    if sort == "original":
        return items
    return sorted(
        items,
        key=lambda x: (
            local_date(x.get("published_at")) is None,
            (
                local_date(x.get("published_at")).toordinal()
                if local_date(x.get("published_at"))
                else 0
            )
            * (-1 if sort == "date_desc" else 1),
            x["source"],
            x["id"],
        ),
    )


def sorted_threads(items, sort="collected_count"):
    return sorted(
        thread_rows(items),
        key=lambda x: (
            x[sort] is None,
            -(x[sort].toordinal() if sort == "latest_date" and x[sort] else x[sort] or 0),
            x["source"],
            x["thread_source_id"],
        ),
    )


def keyword_rows(items, brand=(), metric="term_frequency", limit=30, show_brand=True):
    tf: Counter = Counter()
    df: Counter = Counter()
    for item in items:
        words = tokens(item, brand)
        tf.update(words)
        df.update(set(words))
    hidden = set() if show_brand else {normalized(t) for t in brand}
    rows = [
        {"term": t, "term_frequency": n, "document_frequency": df[t], "evidence_count": df[t]}
        for t, n in tf.items()
        if t not in hidden
    ]
    return sorted(rows, key=lambda x: (-x[metric], x["term"]))[:limit]


def review_view(item):
    from .schemas import ReviewResponse

    values = {
        "rating": None,
        "relative_date": None,
        "published_at_estimated": item.get("published_at"),
        "date_precision": "unknown",
        "owner_reply": None,
        "language": None,
        "sentiment": None,
        "confidence": None,
        "rating_sentiment": None,
        "rating_text_conflict": False,
        "aspects": [],
        "key_points": [],
        **item,
        "platform_data": platform_snapshot(item.get("platform_data") or item.get("metrics")),
    }
    values["source_url"] = safe_url(values.get("source_url"))
    return ReviewResponse.model_validate(values)
