"""Real browser, local test server, synthetic model; no live collection or model calls."""

import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_insight_decisions import FakeCloud, item, run_plan
from test_insight_decisions import report_factory as report_factory
from test_validation import ValidationCloud

from simpsons_insight_agent.api import app
from simpsons_insight_agent.validation import ValidationCoordinator


@pytest.mark.asyncio
async def test_validation_complete_browser_flow(report_factory, monkeypatch, tmp_path):
    report = await report_factory(rows=[item(), item("counter", sentiment="positive")])
    await run_plan(report, FakeCloud())
    queue = asyncio.Queue()
    coordinator = ValidationCoordinator(ValidationCloud(), queue)
    monkeypatch.setattr(app.state, "job_manager", SimpleNamespace(validations=coordinator), raising=False)
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
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.goto(f"http://127.0.0.1:{port}/reports/{report.id}")
            await expect(page.locator("#validation-form")).to_be_visible()
            await page.locator('#validation-form [name="context"]').select_option("business")
            await page.locator('#validation-form [name="context"]').select_option("campus")
            await page.locator('#validation-form [name="weekly_hours"]').fill("2")
            await page.locator("#validation-form button").click()
            await expect(page.locator("#validation-status")).to_contain_text("排隊中")
            # Drain the real queued identifier using the isolated model substitute.
            key = coordinator.queue.get_nowait().split(":", 1)[1]
            await coordinator.run(key)
            coordinator.queue.task_done()
            await page.locator("#validation-refresh").click()
            card = page.locator("#validation-cards article").first
            await expect(card).to_contain_text("審查通過")
            await card.get_by_role("button", name="相反證據（1）").click()
            await expect(page.locator("dialog")).to_contain_text("等候太久")
            await page.keyboard.press("Escape")
            form = card.get_by_role("form", name="確認量測規格")
            for key, value in {"threshold": "10", "minimum_sample": "10", "before_start": "2026-07-01", "before_end": "2026-07-07", "after_start": "2026-07-08", "after_end": "2026-07-14"}.items():
                await form.locator(f'[name="{key}"]').fill(value)
            await form.locator('[name="confirmed"]').check()
            await form.get_by_role("button", name="確認並開始實驗").click()
            await expect(card).to_contain_text("已鎖定")
            result_form = card.get_by_role("form", name="回填實驗結果")
            for key, value in {"before_count": "10", "before_value": "6", "after_count": "10", "after_value": "8"}.items():
                await result_form.locator(f'[name="{key}"]').fill(value)
            await result_form.locator('[name="comparable"]').select_option("true")
            await result_form.get_by_role("button", name="保存結果並判讀").click()
            await expect(card.locator(".validation-result")).to_have_count(1)
            await expect(card).to_contain_text("達到預設目標")
            await page.reload()
            await expect(page.locator(".validation-result")).to_have_count(1)
            await card.get_by_role("button", name="標記實驗完成").click()
            await expect(card).to_contain_text("已完成")
            await page.locator("#validation-panel").screenshot(path=str(tmp_path / "validation-mvp.png"))
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()
