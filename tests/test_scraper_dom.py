from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from review_agent.config import Settings
from review_agent.scraper import MapsDomAdapter, MapsScraper

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_id", "expected_rating", "expected_text"),
    [
        ("reviews_zh.html", "zh-001", 5, "餐點很好吃"),
        ("reviews_en.html", "en-001", 1, "Very slow service"),
    ],
)
async def test_review_card_parser_supports_zh_and_en_fixtures(
    filename: str,
    expected_id: str,
    expected_rating: int,
    expected_text: str,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content((FIXTURES / filename).read_text(encoding="utf-8"))
        adapter = MapsDomAdapter()
        review = await adapter.parse_review(adapter.review_cards(page).first, "https://google.com/maps/place/x")
        await browser.close()

    assert review is not None
    assert review.source_review_id == expected_id
    assert review.rating == expected_rating
    assert expected_text in review.text


@pytest.mark.asyncio
async def test_rating_only_review_and_block_detection() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "reviews_zh.html").read_text(encoding="utf-8"))
        adapter = MapsDomAdapter()
        review = await adapter.parse_review(adapter.review_cards(page).nth(1), page.url)
        assert review is not None and review.text == ""

        await page.set_content("<body><h1>Unusual traffic</h1><iframe src='recaptcha'></iframe></body>")
        assert await adapter.is_blocked(page) is True
        await browser.close()


@pytest.mark.asyncio
async def test_open_reviews_ignores_write_review_and_uses_review_tab() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <button aria-label="撰寫評論" onclick="document.body.dataset.login = 'opened'">
              撰寫評論
            </button>
            <button id="review-tab" role="tab" aria-label="對「五福蛋包飯」的評論">評論</button>
            <script>
              document.querySelector("#review-tab").addEventListener("click", () => {
                const card = document.createElement("div");
                card.className = "jftiEf";
                card.dataset.reviewId = "review-1";
                card.textContent = "評論內容";
                document.body.append(card);
              });
            </script>
            """
        )
        adapter = MapsDomAdapter()
        await adapter.open_reviews(page)
        login_opened = await page.locator("body").get_attribute("data-login")
        review_count = await adapter.review_cards(page).count()
        await browser.close()

    assert login_opened is None
    assert review_count == 1


@pytest.mark.asyncio
async def test_search_candidates_reads_single_place_rendered_inside_search_url() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <main>
              <h1>五福蛋包飯</h1>
              <button data-item-id="address" aria-label="地址: 820高雄市岡山區壽華路126號">
                820高雄市岡山區壽華路126號
              </button>
            </main>
            """
        )
        adapter = MapsDomAdapter()
        candidates = await adapter.search_candidates(page)
        await browser.close()

    assert len(candidates) == 1
    assert candidates[0].name == "五福蛋包飯"
    assert candidates[0].address == "820高雄市岡山區壽華路126號"


@pytest.mark.asyncio
async def test_search_waits_for_dynamic_place_details() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <script>
              setTimeout(() => {
                document.body.innerHTML = `
                  <h1>五福蛋包飯</h1>
                  <button data-item-id="address" aria-label="地址: 820高雄市岡山區壽華路126號"></button>
                `;
              }, 100);
            </script>
            """
        )
        adapter = MapsDomAdapter()
        await adapter.wait_for_search_results(page, timeout_ms=1_000)
        candidates = await adapter.search_candidates(page)
        await browser.close()

    assert candidates[0].name == "五福蛋包飯"


@pytest.mark.asyncio
async def test_batch_parser_returns_each_of_500_cards_only_once() -> None:
    cards = "".join(
        f"<div class='jftiEf' data-review-id='r-{index}'>"
        f"<div class='d4r55'>作者 {index}</div>"
        "<span class='kvMYJc' aria-label='5 顆星'></span>"
        f"<span class='wiI7pd'>評論 {index}</span><span class='rsqaWe'>1 天前</span></div>"
        for index in range(500)
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(f"<body>{cards}</body>")
        adapter = MapsDomAdapter()
        seen: set[str] = set()
        first, visible = await adapter.parse_visible_reviews(
            page, "https://google.com/maps/place/x", seen
        )
        second, visible_again = await adapter.parse_visible_reviews(
            page, "https://google.com/maps/place/x", seen
        )
        await browser.close()

    assert visible == visible_again == 500
    assert len(first) == 500
    assert second == []


@pytest.mark.asyncio
async def test_scroll_reviews_uses_nearest_overflow_container() -> None:
    cards = "".join(
        f"<div class='jftiEf' data-review-id='r-{index}' style='height:80px'>評論</div>"
        for index in range(20)
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            f"<div id='review-panel' style='height:240px;overflow-y:auto'>{cards}</div>"
        )
        scraper = MapsScraper(settings=Settings(scroll_wait_ms=50))
        await scraper._scroll_reviews(page)
        scroll_top = await page.locator("#review-panel").evaluate("node => node.scrollTop")
        await browser.close()

    assert scroll_top > 0


@pytest.mark.asyncio
async def test_scroll_reviews_recovers_when_cards_are_temporarily_replaced() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <div id="review-panel" style="height:100px;overflow-y:auto"></div>
            <script>
              setTimeout(() => {
                document.querySelector("#review-panel").innerHTML =
                  `<div class="jftiEf" data-review-id="late" style="height:300px">評論</div>`;
              }, 100);
            </script>
            """
        )
        scraper = MapsScraper(settings=Settings(scroll_wait_ms=100))
        await scraper._scroll_reviews(page)
        count = await scraper.adapter.review_cards(page).count()
        await browser.close()

    assert count == 1
