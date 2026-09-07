"""Brand and topic comparisons share report scopes; topic membership may overlap."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import (
    ReportFilters,
    brand_terms,
    channel_rows,
    filter_items,
    resolve_scope,
    scoped_trends,
    sorted_threads,
    statistics,
)
from .insights import local_date
from .models import Report


class BrandInput(ReportFilters):
    name: str = Field(min_length=1, max_length=200)
    report_ids: list[str] = Field(min_length=2, max_length=5)
    date_from: date
    date_to: date
    mode: Literal["brands"] = "brands"

    @model_validator(mode="after")
    def distinct_reports(self):
        if len(set(self.report_ids)) != len(self.report_ids):
            raise ValueError("不可重複選擇同一報告")
        if self.topic_key:
            raise ValueError("跨品牌不能共用語意主題鍵")
        return self


class TopicDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    any_terms: list[str] = Field(default_factory=list, max_length=20)
    all_terms: list[str] = Field(default_factory=list, max_length=20)
    exclude_terms: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def terms(self):
        validated = ReportFilters(**self.model_dump())
        for key in ("any_terms", "all_terms", "exclude_terms"):
            setattr(self, key, getattr(validated, key))
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("主題名稱不可空白")
        return self


class TopicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["topics"] = "topics"
    name: str = Field(min_length=1, max_length=200)
    filters: ReportFilters = Field(default_factory=ReportFilters)
    topics: list[TopicDefinition] = Field(min_length=1, max_length=5)
    selected_ids: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def valid_selection(self):
        ids = [t.id for t in self.topics]
        if len(set(ids)) != len(ids) or len(set(self.selected_ids)) != len(self.selected_ids):
            raise ValueError("主題或選取ID不可重複")
        if not set(self.selected_ids) <= set(ids):
            raise ValueError("選取的主題不存在")
        return self


def base_filters(config):
    values = config.get("filters", {}) if config.get("mode") == "topics" else config
    return ReportFilters.model_validate({**values, "precision_policy": "interval"})


async def comparison_members(session, row):
    config = row.config
    filters = base_filters(config)
    topic_mode = config.get("mode") == "topics"
    references = [config["report_id"]] if topic_mode else config["report_ids"]
    reports = {}
    missing = []
    for key in references:
        report = await session.get(Report, key)
        if report is None:
            missing.append(key)
            continue
        items, scope = await resolve_scope(session, report, filters)
        reports[key] = (report, items, scope)
    if topic_mode and reports:
        mother = next(iter(reports.values()))[1]
        dates = [
            local_date(x.get("published_at")) for x in mother if local_date(x.get("published_at"))
        ]
        filters = ReportFilters.model_validate(
            {
                **filters.model_dump(),
                "date_from": filters.date_from or min(dates, default=filters.date_to),
                "date_to": filters.date_to or max(dates, default=filters.date_from),
            }
        )
    members = []
    if topic_mode:
        for topic in config["topics"]:
            if topic["id"] not in config["selected_ids"] or config["report_id"] not in reports:
                continue
            report, mother, base = reports[config["report_id"]]
            # Sequential intersection is intentional: OR groups must not be merged.
            items, _ = filter_items(mother, ReportFilters(**topic), brand_terms(report))
            scope = {
                **base,
                "included_count": len(items),
                "topic_conditions": topic,
                "scope_key": hashlib.sha256(
                    json.dumps([base["scope_key"], topic], sort_keys=True).encode()
                ).hexdigest()[:24],
            }
            members.append((topic["id"], topic["name"], report, items, scope))
    else:
        for key, (report, items, scope) in reports.items():
            name = (report.payload.get("subject") or report.payload.get("business") or {}).get(
                "name", "未命名"
            )
            members.append((key, name, report, items, scope))
    return members, filters, missing


async def comparison_result(session, row):
    members, filters, missing = await comparison_members(session, row)
    groups = []
    warnings = ["統計僅代表已蒐集樣本，不代表市場占有率。"]
    if missing:
        warnings.append("部分原始報告已刪除，無法完整比較")
    memberships: Counter = Counter()
    for key, name, report, items, scope in members:
        stat = statistics(items)
        aspects = Counter(
            a for x in items if x.get("sentiment") == "negative" for a in set(x.get("aspects", []))
        )
        ids = {x["id"] for x in items}
        memberships.update((report.id, i) for i in ids)
        dates = [
            local_date(x.get("published_at")) for x in items if local_date(x.get("published_at"))
        ]
        topics = [
            {
                "name": t["name"],
                "keywords": t.get("keywords", []),
                "count": len(ids & set(t["item_ids"])),
            }
            for t in report.payload.get("topics", [])
            if ids & set(t["item_ids"])
        ]
        if not scope["collection"].get("complete", False) or scope["excluded_count"]:
            warnings.append(f"{name}：來源未完整或日期精度不足")
        if scope["data_basis"] == "legacy_live":
            warnings.append(f"{name}：舊報告使用目前資料，並非不可變快照")
        groups.append(
            {
                "key": key,
                "report_id": report.id,
                "name": name,
                "sample_size": len(items),
                **stat,
                "summary": stat,
                "sources": stat["source_counts"],
                "scope": scope,
                "trends": scoped_trends(items, filters, scope),
                "topics": topics,
                "channels": channel_rows(items),
                "source_rankings": channel_rows(items, "source"),
                "threads": sorted_threads(items)[:25],
                "complaint_aspects": dict(aspects),
                "complaint_aspect_ratios": {
                    k: v / stat["classified_count"] if stat["classified_count"] else None
                    for k, v in aspects.items()
                },
                "observed_from": min(dates, default=None),
                "observed_to": max(dates, default=None),
            }
        )
    if (
        len(
            {
                (g["observed_from"], g["observed_to"], tuple(sorted(g["sources"].items())))
                for g in groups
            }
        )
        > 1
    ):
        warnings.append("來源組成或實際資料期間不同，不可直接比較高低；請勿只按總量判斷優劣。")
    topics_mode = row.config.get("mode") == "topics"
    return {
        "id": row.id,
        "name": row.name,
        "mode": "topics" if topics_mode else "brands",
        "config": row.config,
        "groups": groups,
        "brands": groups if not topics_mode else [],
        "ready": len(groups) >= 2,
        "missing_report_ids": missing,
        "warnings": warnings,
        "filters": filters.model_dump(mode="json"),
        "common_complaint_aspects": sorted(
            set.intersection(*(set(g["complaint_aspects"]) for g in groups))
        )
        if groups
        else [],
        "union_count": len(memberships) if topics_mode else None,
        "overlap_count": sum(n >= 2 for n in memberships.values()) if topics_mode else None,
    }


async def member_evidence(session, row, key, extra):
    members, _, _ = await comparison_members(session, row)
    member = next((m for m in members if m[0] == key), None)
    if member is None:
        raise HTTPException(404, "比較對象不存在或原報告已刪除")
    _, name, report, items, scope = member
    filtered, counts = filter_items(items, extra, brand_terms(report))
    return (
        report,
        filtered,
        {
            **scope,
            "included_count": len(filtered),
            "drilldown_filters": extra.model_dump(mode="json"),
            "scope_key": hashlib.sha256(
                json.dumps(
                    [scope["scope_key"], extra.model_dump(mode="json")], sort_keys=True
                ).encode()
            ).hexdigest()[:24],
            "drilldown_excluded_count": counts["excluded_count"],
            "comparison_name": row.name,
            "member_name": name,
        },
    )
