from __future__ import annotations

import csv
import io

import pytest
from httpx import ASGITransport, AsyncClient
from test_insight_decisions import item
from test_insight_decisions import report_factory as report_factory

from simpsons_insight_agent.api import app
from simpsons_insight_agent.db import SessionLocal
from simpsons_insight_agent.models import BrandComparison, Report


def topic_payload():
    return {
        "name": "議題比較",
        "filters": {"any_terms": ["麵包"], "interval": "day"},
        "topics": [
            {"id": "service", "name": "服務", "any_terms": ["服務"]},
            {"id": "queue", "name": "排隊", "any_terms": ["排隊"]},
        ],
        "selected_ids": ["service", "queue"],
    }


@pytest.mark.asyncio
async def test_topics_intersection_overlap_evidence_update_and_export(report_factory):
    report = await report_factory(
        rows=[
            item(key, text=text)
            for key, text in [
                ("a", "麵包服務排隊"),
                ("b", "麵包服務"),
                ("c", "咖啡服務排隊"),
                ("d", "麵包價格"),
                ("e", "麵包排隊"),
            ]
        ]
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        body = topic_payload()
        made = await c.post(f"/api/reports/{report.id}/topic-comparisons", json=body)
        assert made.status_code == 201, made.text
        key = made.json()["id"]
        base = f"/api/comparisons/{key}"
        d = (await c.get(base)).json()
        assert [g["sample_size"] for g in d["groups"]] == [2, 2]
        assert (d["union_count"], d["overlap_count"]) == (3, 1)
        assert d["groups"][0]["trends"]["points"][0]["count"] == 2
        evidence = (await c.get(base + "/members/service/items")).json()
        assert {x["id"] for x in evidence["items"]} == {"a", "b"}
        assert (await c.get(base + "/members/service/items?all_terms=咖啡")).json()["total"] == 0
        assert (await c.get(base + "/members/service/items?source=dcard")).json()["total"] == 0
        assert (await c.get(base + "/members/missing/items")).status_code == 404
        csv_response = await c.get(base + "/export")
        rows = list(csv.DictReader(io.StringIO(csv_response.text.lstrip("\ufeff"))))
        assert sum(int(r["value"]) for r in rows if r["metric"] == "count") == 4
        assert next(r["value"] for r in rows if r["metric"] == "overlap_count") == "1"
        assert (await c.get(f"/comparisons/{key}/print")).status_code == 200
        listing = (await c.get(f"/api/reports/{report.id}/topic-comparisons")).json()
        assert any(x["id"] == key for x in listing)
        body["name"] = "新名稱"
        body["topics"][1] = {"id": "price", "name": "價格", "any_terms": ["價格"]}
        body["selected_ids"] = ["service", "price"]
        assert (await c.patch(base, json=body)).status_code == 200
        changed = (await c.get(base)).json()
        assert changed["name"] == "新名稱" and changed["overlap_count"] == 0
        body["topics"] = body["topics"][:1]
        body["selected_ids"] = ["service"]
        assert (await c.patch(base, json=body)).status_code == 200
        assert not (await c.get(base)).json()["ready"]
        body["selected_ids"] = ["missing"]
        assert (await c.patch(base, json=body)).status_code == 422


@pytest.mark.asyncio
async def test_rankings_dates_nulls_stable_order_and_group_scope(report_factory):
    report = await report_factory(
        rows=[
            item(
                "a",
                board="Food",
                source="ptt",
                thread_source_id="a",
                published_at="2026-08-01T00:00:00Z",
                metrics={"reply_count": 0},
            ),
            item(
                "b",
                board="Food",
                source="dcard",
                thread_source_id="b",
                published_at="2026-08-02T00:00:00Z",
            ),
            item("c", board=None, source="ptt", thread_source_id="c", published_at=None),
        ]
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        base = f"/api/reports/{report.id}"
        ranked = (await c.get(base + "/channels?group_by=source&sort=negative_ratio")).json()
        assert len(ranked["items"]) == 2
        assert sum(x["sample_count"] for x in ranked["items"]) == 3
        assert len((await c.get(base + "/channels")).json()["items"]) == 3
        for sort, ids in [("date_desc", ["b", "a", "c"]), ("date_asc", ["a", "b", "c"])]:
            assert [
                x["id"] for x in (await c.get(base + "/items?sort=" + sort)).json()["items"]
            ] == ids
        threads = (await c.get(base + "/threads?sort=latest_date")).json()["items"]
        assert [x["thread_source_id"] for x in threads] == ["b", "a", "c"]
        assert threads[-1]["latest_date"] is None
        assert (await c.get(base + "/threads?sort=reported_reply_count")).json()["items"][0][
            "reported_reply_count"
        ] == 0
        for endpoint in [
            "/channels?sort=invalid",
            "/channels?group_by=website",
            "/items?sort=invalid",
            "/threads?sort=invalid",
        ]:
            assert (await c.get(base + endpoint)).status_code == 422


@pytest.mark.asyncio
async def test_legacy_brand_comparison_edit_deleted_report_and_drilldown(report_factory):
    a = await report_factory(rows=[item("a", sentiment="positive")])
    b = await report_factory(rows=[item("b")])
    body = {
        "name": "舊比較",
        "report_ids": [a.id, b.id],
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
        "source": None,
    }
    async with SessionLocal() as s:
        row = BrandComparison(name="舊比較", config=body)
        s.add(row)
        await s.commit()
        key = row.id
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        data = (await c.get("/api/comparisons/" + key)).json()
        assert data["mode"] == "brands" and len(data["brands"]) == 2
        assert data["groups"][0]["pn_ratio"] is None
        assert (
            await c.get(f"/api/comparisons/{key}/members/{a.id}/items?sentiment=negative")
        ).json()["total"] == 0
        body.update(interval="day", name="修改比較")
        assert (await c.patch("/api/comparisons/" + key, json=body)).status_code == 200
        async with SessionLocal() as s:
            await s.delete(await s.get(Report, b.id))
            await s.commit()
        data = (await c.get("/api/comparisons/" + key)).json()
        assert data["missing_report_ids"] == [b.id] and len(data["groups"]) == 1
        assert (await c.patch("/api/comparisons/" + key, json=body)).status_code == 200
        assert (await c.get(f"/comparisons/{key}/print")).status_code == 200
