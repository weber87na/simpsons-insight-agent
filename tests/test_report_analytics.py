from __future__ import annotations

import csv
import io

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from test_insight_decisions import item
from test_insight_decisions import report_factory as report_factory

from simpsons_insight_agent.analytics import (
    ReportFilters,
    filter_items,
    keyword_rows,
    statistics,
    tokens,
)
from simpsons_insight_agent.api import _normalize_report_payload, app
from simpsons_insight_agent.db import SessionLocal
from simpsons_insight_agent.insights import enrich_report
from simpsons_insight_agent.models import Report


@pytest.mark.asyncio
async def test_new_snapshot_freezes_live_review_metadata(report_factory):
    from datetime import UTC, datetime

    from simpsons_insight_agent.models import JobReview, Review, ReviewAnalysis

    report = await report_factory(rows=[])
    async with SessionLocal() as session:
        saved = await session.get(Report, report.id)
        saved.payload = {k: v for k, v in saved.payload.items() if k != "analytics_items"}
        review = Review(
            business_id=report.business_id,
            content_hash="frozen",
            source="ptt",
            content_type="post",
            title="原始標題",
            text="麵包很好吃",
            board="Food",
            source_url="https://example.com/thread",
            thread_source_id="frozen",
            platform_data={"reaction_count": 8, "signal": "push", "author": "must not leak"},
            published_at_estimated=datetime(2026, 8, 2, 16, tzinfo=UTC),
            date_precision="day",
        )
        session.add(review)
        await session.flush()
        session.add(JobReview(job_id=report.job_id, review_id=review.id, ordinal=0))
        session.add(
            ReviewAnalysis(
                job_id=report.job_id,
                review_id=review.id,
                sentiment="positive",
                local_model_id="test",
            )
        )
        await session.commit()
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
            before = (await c.get(f"/api/reports/{report.id}/items")).json()
            assert before["scope"]["data_basis"] == "legacy_live"
        await enrich_report(session, saved, "unused-model")
        await session.commit()
        assert saved.payload["schema_version"] == 4
        review.title = "續抓更新"
        review.text = "不同內容"
        review.board = "Gossiping"
        review.platform_data = {"reaction_count": 999}
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        response = (await c.get(f"/api/reports/{report.id}/items")).json()
        entry = response["items"][0]
        assert entry["title"] == "原始標題" and entry["board"] == "Food"
        assert entry["platform_data"] == {"reaction_count": 8, "signal": "push"}
        assert response["scope"]["data_basis"] == "snapshot"
        assert "author" not in str(response)


def test_filters_terms_precision_and_zero_denominator():
    rows = [
        item("a", text="ＡＢＣ 麵包 美味"),
        item("b", text="abc 麵包 售完"),
        item("c", text="abc 麵包", date_precision="month"),
        item("d", published_at=None),
    ]
    f = ReportFilters(
        interval="day",
        precision_policy="interval",
        any_terms=["ＡＢＣ"],
        all_terms=["麵包"],
        exclude_terms=["售完"],
    )
    selected, counts = filter_items(rows + rows[:1], f)
    assert [x["id"] for x in selected] == ["a"]
    assert counts["imprecise_date_count"] == 1
    assert counts["deduplicated_count"] == 4
    assert statistics([])["pn_reason"] == "no_classified_text"
    assert statistics([item(sentiment="positive")])["pn_reason"] == "no_negative"
    with pytest.raises(ValidationError):
        ReportFilters(all_terms=[" "])
    with pytest.raises(ValidationError):
        ReportFilters(date_from="2026-09-07", date_to="2026-09-01")


def test_keyword_frequency_and_evidence_membership():
    rows = [item("a", text="麵包 麵包 美味"), item("b", text="麵包 服務")]
    words = keyword_rows(rows)
    bread = next(x for x in words if x["term"] == "麵包")
    assert (bread["term_frequency"], bread["document_frequency"]) == (3, 2)
    selected, _ = filter_items(rows, ReportFilters(keyword="麵包"))
    assert len(selected) == bread["evidence_count"]
    assert "https" not in tokens(item(text="https 123 !!!"))


@pytest.mark.asyncio
async def test_scope_chart_items_exports_and_print(report_factory):
    rows = [
        item(
            "a",
            title="=danger",
            text="麵包 美味",
            sentiment="positive",
            board="Food",
            thread_source_id="t",
            metrics={"reply_count": 80},
        ),
        item("b", text="麵包 等候", board="Food", thread_source_id="t"),
        item("c", date_precision="month"),
        item("d", published_at=None),
        item("e", sentiment="rating_only"),
    ]
    report = await report_factory(rows=rows)
    async with SessionLocal() as session:
        saved = await session.get(Report, report.id)
        saved.payload = {**saved.payload, "schema_version": 4}
        await session.commit()
    q = "interval=day&precision_policy=interval&date_from=2026-08-03&date_to=2026-08-04"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        base = f"/api/reports/{report.id}"
        summary = (await c.get(base + "/summary?" + q)).json()
        trend = (await c.get(base + "/trends?" + q)).json()
        items = (await c.get(base + "/items?" + q)).json()
        exported = (await c.get(base + "/export?format=json&" + q)).json()
        assert summary["count"] == items["total"] == len(exported["items"]) == 3
        assert sum(p["count"] for p in trend["points"]) == 3
        assert len({x["scope"]["scope_key"] for x in [summary, trend, items, exported]}) == 1
        assert exported["items"] == exported["reviews"]
        assert exported["report"]["schema_version"] == 4
        assert trend["points"][0]["pn_ratio"] == 1
        assert trend["points"][1]["pn_ratio"] is None
        assert trend["points"][0]["source_counts"] == {"ptt": 3}
        threads = (await c.get(base + "/threads?" + q)).json()
        assert threads["items"][0]["collected_count"] == 2
        assert threads["items"][0]["reported_reply_count"] == 80
        csv_response = await c.get(base + "/export?format=csv&" + q)
        assert "'=danger" in csv_response.text
        trend_csv = await c.get(base + "/trends/export?" + q)
        parsed = list(csv.DictReader(io.StringIO(trend_csv.text.lstrip("\ufeff"))))
        assert sum(int(x["count"]) for x in parsed) == 3
        printed = await c.get(f"/reports/{report.id}/print?" + q)
        assert printed.status_code == 200, printed.text
        assert summary["scope"]["scope_key"] in printed.text
        assert "window.print()" in printed.text
        for query in [
            "all_terms=",
            "interval=year",
            "page_size=101",
            "date_from=2026-09-01&date_to=2026-01-01",
        ]:
            assert (await c.get(base + "/items?" + query)).status_code == 422


@pytest.mark.asyncio
async def test_old_snapshot_missing_fields_and_retry_preserved(report_factory):
    report = await report_factory()
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        result = (await c.get(f"/api/reports/{report.id}/items")).json()
        assert result["items"][0]["title"] is None
        assert "title" in result["scope"]["unavailable_fields"]
        assert result["scope"]["data_basis"] == "snapshot"
    async with SessionLocal() as session:
        saved = await session.get(Report, report.id)
        previous = dict(saved.payload)
        await enrich_report(session, saved, "unused-model")
        assert saved.payload == previous
    assert (
        _normalize_report_payload({"schema_version": 4, "overall": {"sample_size": 7}})["overall"][
            "sample_size"
        ]
        == 7
    )


@pytest.mark.asyncio
async def test_day_comparison_reuses_scope(report_factory):
    first = await report_factory(rows=[item("a"), item("b", date_precision="month")])
    second = await report_factory(rows=[item("c", sentiment="positive")])
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        made = await c.post(
            "/api/comparisons",
            json={
                "name": "每日比較",
                "report_ids": [first.id, second.id],
                "date_from": "2026-08-03",
                "date_to": "2026-08-04",
                "interval": "day",
            },
        )
        assert made.status_code == 201
        data = (await c.get("/api/comparisons/" + made.json()["id"])).json()
        assert [b["sample_size"] for b in data["brands"]] == [1, 1]
        assert all(b["trends"]["interval"] == "day" for b in data["brands"])
