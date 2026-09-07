"""Real Chromium test, isolated SQLite fixture and no application lifespan/models."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_insight_decisions import FakeCloud, item, run_plan
from test_insight_decisions import report_factory as report_factory

from simpsons_insight_agent.api import app


@pytest.mark.asyncio
async def test_operational_report_in_chromium(report_factory, tmp_path):
    report = await report_factory(
        rows=[
            item(
                "r1" if i == 0 else str(i),
                title="餐飲討論",
                text="麵包 美味 服務",
                sentiment="positive" if i % 2 else "negative",
                board="Food",
                thread_source_id="thread",
            )
            for i in range(30)
        ]
    )
    other = await report_factory(rows=[item("other", text="麵包 服務", sentiment="positive")])
    await run_plan(report, FakeCloud())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{port}/reports/{report.id}")
            ids = await page.locator("[id]").evaluate_all("(nodes)=>nodes.map(n=>n.id)")
            assert len(ids) == len(set(ids)), "Duplicate DOM IDs"
            await expect(page.locator("#trend-note")).to_contain_text("已套用")
            await expect(page.locator("#keyword-cloud button")).not_to_have_count(0)
            delayed_done = asyncio.Event()

            async def delayed_summary(route):
                response = await route.fetch()
                if "sentiment=negative" in route.request.url:
                    await asyncio.sleep(0.3)
                await route.fulfill(response=response)
                if "sentiment=negative" in route.request.url:
                    delayed_done.set()

            await page.route("**/summary?*", delayed_summary)
            await page.locator("#analytics-filter-sentiment").select_option("negative")
            await page.locator("#trend-form button").click()
            await page.locator("#analytics-filter-sentiment").select_option("positive")
            await page.locator("#trend-form button").click()
            await expect(page.locator("#trend-note")).to_contain_text("情緒：正面")
            await expect(page.locator("#trend-note")).to_contain_text("納入 15 筆")
            await asyncio.wait_for(delayed_done.wait(), 5)
            await page.unroute("**/summary?*", delayed_summary)
            await page.locator("#analytics-filter-sentiment").select_option("")
            await page.locator("#trend-interval").select_option("day")
            await page.locator("#trend-from").fill("2026-08-03")
            await page.locator("#trend-to").fill("2026-08-04")
            await page.locator("#analytics-filter-any").fill("麵包、咖啡")
            await expect(page.locator("#trend-note")).to_contain_text("尚未套用")
            await page.locator("#trend-form button").click()
            await expect(page.locator("#trend-note")).to_contain_text("納入 30 筆")
            await page.locator("#trend-charts").scroll_into_view_if_needed()
            await page.screenshot(path=str(tmp_path / "analytics-trends.png"))
            point = page.locator("#trend-charts circle").first
            await point.focus()
            await page.keyboard.press("Enter")
            await expect(page.locator("dialog h2")).to_contain_text("30 筆")
            await expect(page.locator("dialog article")).to_have_count(25)
            await page.get_by_role("button", name="下一頁", exact=True).last.click()
            await expect(page.locator("dialog article")).to_have_count(5)
            await page.keyboard.press("Escape")
            await expect(page.locator("dialog")).to_have_count(0)
            await expect(point).to_be_focused()
            await page.locator("#keyword-cloud button").filter(has_text="麵包").click()
            await expect(page.locator("dialog h2")).to_contain_text("30 筆")
            await (
                page.locator("dialog")
                .get_by_role("button", name="改善工作：", exact=False)
                .first.click()
            )
            await expect(page.locator("dialog")).to_have_count(0)
            href = await page.locator("#filtered-print").get_attribute("href")
            await page.goto(f"http://127.0.0.1:{port}" + href)
            await expect(page.get_by_role("heading", name="統計總覽")).to_be_visible()
            await page.emulate_media(media="print")
            await page.screenshot(path=str(tmp_path / "analytics-print.png"), full_page=True)
            await page.pdf(
                path=str(tmp_path / "analytics-report.pdf"), format="A4", print_background=True
            )
            await page.emulate_media(media="screen")
            await page.goto(f"http://127.0.0.1:{port}/comparisons")
            await page.locator("#comparison-reports").select_option([report.id, other.id])
            await page.locator("#comparison-from").fill("2026-08-03")
            await page.locator("#comparison-to").fill("2026-08-04")
            await page.locator("#comparison-interval").select_option("day")
            await page.locator("#compare-form button").click()
            await expect(page.locator("#comparison-results > .two-column svg")).to_have_count(3)
            await page.locator("#comparison-name").fill("更新的品牌比較")
            await page.locator("#comparison-submit").click()
            await expect(page.locator("#comparison-results h2")).to_have_text("更新的品牌比較")
            await page.locator('#comparison-results circle[aria-label$=" 30"]').first.focus()
            await page.keyboard.press("Enter")
            await expect(page.locator("dialog h2")).to_contain_text("30 筆")
            await page.keyboard.press("Escape")
            await page.goto(f"http://127.0.0.1:{port}/reports/{report.id}")
            await expect(page.locator("#trend-note")).to_contain_text("已套用")
            await page.locator("#analytics-filter-any").fill("麵包")
            await page.locator("#trend-form button").click()
            await expect(page.locator("#trend-note")).to_contain_text("包含任一詞：麵包")
            await page.locator('#topic-editor-cards [data-field="any_terms"]').nth(1).fill("麵包")
            await page.locator("#topic-comparison-name").fill("服務與麵包")
            await page.locator("#topic-save").click()
            await expect(page.locator("#topic-editor-status")).to_contain_text("已保存")
            topic_id = await page.locator("#topic-saved").input_value()
            await page.goto(
                f"http://127.0.0.1:{port}/reports/{report.id}?topic_comparison={topic_id}"
            )
            await expect(page.locator("#topic-editor-status")).to_contain_text(
                "已載入主題卡及保存的共用條件"
            )
            await expect(page.locator("#analytics-filter-any")).to_have_value("麵包")
            await page.locator("#topic-compare").click()
            await expect(page.locator("#comparison-results")).to_contain_text(
                "聯集 30 筆；同時命中至少兩個議題 30 筆"
            )
            await page.locator("#comparison-results circle").first.focus()
            await page.keyboard.press("Enter")
            await expect(page.locator("dialog h2")).to_contain_text("30 筆")
            await page.keyboard.press("Escape")
            await page.get_by_label("服務 情緒分布顯示方式").select_option("ratio")
            figure = page.locator("#comparison-results figure").first
            await figure.locator(".chart-legend button").first.click()
            for kind in ["PNG", "JPEG"]:
                async with page.expect_download() as pending:
                    await figure.get_by_role("button", name="下載 " + kind, exact=True).click()
                download = await pending.value
                target = tmp_path / ("comparison." + kind.lower())
                await download.save_as(target)
                data = target.read_bytes()
                assert len(data) > 10000
                if kind == "PNG":
                    assert int.from_bytes(data[16:20], "big") == 1200
                else:
                    assert data[:2] == b"\xff\xd8"
            response = await page.request.get(
                f"http://127.0.0.1:{port}/api/comparisons/{topic_id}/export"
            )
            assert response.ok
            assert "overlap_count" in await response.text()
            await page.screenshot(path=str(tmp_path / "topic-comparison.png"), full_page=True)
            await page.goto(f"http://127.0.0.1:{port}/comparisons/{topic_id}/print")
            await expect(page.locator("svg")).not_to_have_count(0)
            await page.emulate_media(media="print")
            await page.pdf(
                path=str(tmp_path / "comparison.pdf"), format="A4", print_background=True
            )
            assert not errors, errors
            print("Browser artifacts:", tmp_path)
            await browser.close()
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()
