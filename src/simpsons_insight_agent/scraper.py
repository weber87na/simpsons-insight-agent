from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.async_api import BrowserContext, Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import Settings, get_settings
from .privacy import normalize_text
from .schemas import BusinessCandidate


class MapsScraperError(RuntimeError):
    pass


class MapsBlockedError(MapsScraperError):
    pass


class MapsCanceledError(MapsScraperError):
    pass


@dataclass(slots=True)
class ScrapedReview:
    source_review_id: str | None
    content_hash: str
    author_name: str | None
    rating: int | None
    text: str
    relative_date: str | None
    owner_reply: str | None
    source_url: str


@dataclass(slots=True)
class CrawlResult:
    name: str
    address: str | None
    average_rating: float | None
    total_review_count: int | None
    reviews_seen: int
    stop_reason: str


BatchCallback = Callable[[list[ScrapedReview]], Awaitable[None]]
ProgressCallback = Callable[[int, int | None, str], Awaitable[None]]
VerificationCallback = Callable[[str], Awaitable[None]]
CancelCallback = Callable[[], Awaitable[bool]]
MetricsCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class MapsDomAdapter:
    """All Google Maps DOM assumptions live in this adapter."""

    review_card_selectors: tuple[str, ...] = (
        "div.jftiEf",
        'div[data-review-id][role="article"]',
        'div[data-review-id]',
    )
    candidate_card_selectors: tuple[str, ...] = ("div.Nv2PK", 'div[role="feed"] > div')
    review_text_selectors: tuple[str, ...] = ("span.wiI7pd", ".MyEned span", ".review-full-text")
    author_selectors: tuple[str, ...] = ("div.d4r55", ".TSUbDb", "button[aria-label]")
    date_selectors: tuple[str, ...] = ("span.rsqaWe", ".dehysf")
    owner_reply_selectors: tuple[str, ...] = ("div.CDe7pd", "div.owner-response")

    async def accept_consent(self, page: Page) -> None:
        for label in ("全部接受", "接受全部", "Accept all", "I agree", "同意"):
            button = page.get_by_role("button", name=re.compile(label, re.IGNORECASE))
            if await button.count():
                try:
                    await button.first.click(timeout=2_000)
                    await page.wait_for_timeout(500)
                    return
                except Exception:
                    continue

    async def is_blocked(self, page: Page) -> bool:
        if "/sorry/" in page.url:
            return True
        body = (await page.locator("body").inner_text(timeout=3_000)).lower()
        markers = (
            "unusual traffic",
            "not a robot",
            "異常流量",
            "不是機器人",
            "驗證您的身分",
            "verify you are human",
        )
        if any(marker in body for marker in markers):
            return True
        return bool(await page.locator('iframe[src*="recaptcha"]').count())

    async def search_candidates(self, page: Page, limit: int = 8) -> list[BusinessCandidate]:
        candidates: list[BusinessCandidate] = []
        seen: set[str] = set()

        # Google Maps may render the single best match in-place while keeping the
        # URL at /maps/search/..., so URL matching alone is not enough here.
        if await self._has_current_place(page):
            return [await self.read_current_place(page)]

        for selector in self.candidate_card_selectors:
            cards = page.locator(selector)
            count = min(await cards.count(), limit)
            for index in range(count):
                card = cards.nth(index)
                candidate = await self._parse_candidate_card(card, page)
                if candidate is None or candidate.maps_url in seen:
                    continue
                seen.add(candidate.maps_url)
                candidates.append(candidate)
            if candidates:
                return candidates

        # Keep a fallback for Google layout changes where place links remain but
        # their old card wrapper class no longer exists.
        links = page.locator('a[href*="/maps/place/"]')
        for index in range(min(await links.count(), limit)):
            anchor = links.nth(index)
            href = await anchor.get_attribute("href")
            if not await anchor.count():
                continue
            if not href or href in seen:
                continue
            card = anchor.locator("xpath=ancestor::*[@role='article'][1]")
            if not await card.count():
                card = anchor.locator("xpath=..").first
            candidate = await self._parse_candidate_card(card, page, anchor=anchor)
            if candidate is None or candidate.maps_url in seen:
                continue
            seen.add(candidate.maps_url)
            candidates.append(candidate)
        return candidates

    async def wait_for_search_results(self, page: Page, timeout_ms: int = 12_000) -> None:
        """Wait for either a result list or a single-place detail view."""

        result_root = page.locator(
            'button[data-item-id="address"], h1.DUwDvf, main h1, '
            'div[role="feed"], div.Nv2PK, a[href*="/maps/place/"]'
        ).first
        try:
            await result_root.wait_for(state="attached", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Let search_candidates return an empty list and keep the existing
            # user-facing "找不到候選商家" response when Google has no result.
            return

    async def _has_current_place(self, page: Page) -> bool:
        if await page.locator('div[role="feed"]').count():
            return False
        heading = page.locator("h1.DUwDvf, main h1, h1").first
        address = page.locator('button[data-item-id="address"]').first
        return await heading.count() > 0 and await address.count() > 0

    async def _parse_candidate_card(
        self,
        card: Locator,
        page: Page,
        *,
        anchor: Locator | None = None,
    ) -> BusinessCandidate | None:
        anchor = anchor or card.locator('a[href*="/maps/place/"]').first
        if not await anchor.count():
            return None
        href = await anchor.get_attribute("href")
        if not href:
            return None
        maps_url = urljoin(page.url, href)
        name = await anchor.get_attribute("aria-label") or await _first_text(card, (".qBF1Pd",))
        card_text = normalize_text(await card.inner_text())
        rating = _parse_float(await _first_text(card, ("span.MW4etd",)))
        review_count = _parse_int(await _first_text(card, ("span.UY7F9",)))
        return BusinessCandidate(
            name=name or "未命名商家",
            maps_url=maps_url,
            address=_guess_address(card_text, name or ""),
            average_rating=rating,
            total_review_count=review_count,
        )

    async def read_current_place(self, page: Page) -> BusinessCandidate:
        name = await _first_text(page.locator("body"), ("h1.DUwDvf", "h1")) or "未命名商家"
        address = None
        address_button = page.locator('button[data-item-id="address"]')
        if await address_button.count():
            address = _strip_prefixed_label(await address_button.first.get_attribute("aria-label"), "地址")
            address = address or normalize_text(await address_button.first.inner_text())

        rating_text = await _first_text(page.locator("body"), ("div.F7nice span[aria-hidden=true]",))
        review_button = page.locator('button[jsaction*="reviewChart.moreReviews"]').first
        review_label = await review_button.get_attribute("aria-label") if await review_button.count() else None
        return BusinessCandidate(
            name=normalize_text(name),
            maps_url=page.url,
            address=address,
            average_rating=_parse_float(rating_text),
            total_review_count=_parse_int(review_label),
        )

    async def open_reviews(self, page: Page) -> None:
        if await self.review_cards(page).count():
            return
        selectors = (
            'button[jsaction*="reviewChart.moreReviews"]',
            'button[aria-label*="則評論"]',
            'button[aria-label*="reviews"]',
            'button[aria-label*="Reviews"]',
            'button[role="tab"][aria-label*="評論"]',
            'button[aria-label*="的評論"]',
        )
        excluded_labels = ("撰寫", "搜尋", "排序", "篩選", "write", "search", "sort", "filter")
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                button = locator.nth(index)
                label = (await button.get_attribute("aria-label") or "").casefold()
                if any(blocked in label for blocked in excluded_labels):
                    continue
                try:
                    await button.click(timeout=5_000)
                    await self.review_cards(page).first.wait_for(timeout=12_000)
                    return
                except Exception:
                    continue
        raise MapsScraperError("找不到 Google Maps 評論按鈕；頁面結構可能已變更。")

    async def select_sort(self, page: Page, sort_order: str) -> None:
        if sort_order == "relevant":
            return
        sort_button = page.locator(
            'button[aria-label*="排序"], button[aria-label*="Sort reviews"], '
            'button[data-value="Sort"]'
        )
        if not await sort_button.count():
            return
        try:
            await sort_button.first.click(timeout=4_000)
            for label in ("最新", "Newest"):
                option = page.get_by_role("menuitemradio", name=re.compile(label, re.IGNORECASE))
                if not await option.count():
                    option = page.get_by_text(label, exact=True)
                if await option.count():
                    await option.first.click(timeout=4_000)
                    await page.wait_for_timeout(800)
                    return
        except Exception:
            return

    def review_cards(self, page: Page) -> Locator:
        return page.locator(", ".join(self.review_card_selectors))

    async def wait_for_review_cards(self, page: Page, timeout_ms: int = 5_000) -> bool:
        """Wait through transient Maps re-renders without treating them as a hard failure."""

        try:
            await self.review_cards(page).first.wait_for(state="attached", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            return False
        return bool(await self.review_cards(page).count())

    async def expand_visible_reviews(self, page: Page) -> None:
        buttons = page.locator(
            'button.w8nwRe, button[aria-label="更多"], button[aria-label="More"], '
            'button:has-text("更多"), button:has-text("More")'
        )
        for index in range(min(await buttons.count(), 80)):
            try:
                await buttons.nth(index).click(timeout=800)
            except Exception:
                continue

    async def parse_review(self, card: Locator, source_url: str) -> ScrapedReview | None:
        try:
            source_id = await card.get_attribute("data-review-id")
            author = await _first_text(card, self.author_selectors)
            text = await _first_text(card, self.review_text_selectors) or ""
            relative_date = await _first_text(card, self.date_selectors)
            owner_reply = await _first_text(card, self.owner_reply_selectors)
            rating_label = await _first_attribute(
                card,
                ("span.kvMYJc", 'span[role="img"][aria-label*="星"]', 'span[role="img"]'),
                "aria-label",
            )
            rating = _parse_int(rating_label)
            content_hash = hashlib.sha256(
                "|".join(
                    (
                        _canonical_place_url(source_url),
                        normalize_text(author),
                        str(rating or ""),
                        normalize_text(text),
                    )
                ).encode("utf-8")
            ).hexdigest()
            if not any((source_id, author, text, rating)):
                return None
            return ScrapedReview(
                source_review_id=source_id,
                content_hash=content_hash,
                author_name=normalize_text(author) or None,
                rating=rating,
                text=normalize_text(text),
                relative_date=normalize_text(relative_date) or None,
                owner_reply=normalize_text(owner_reply) or None,
                source_url=source_url,
            )
        except Exception:
            return None

    async def parse_visible_reviews(
        self,
        page: Page,
        source_url: str,
        seen_keys: set[str],
    ) -> tuple[list[ScrapedReview], int]:
        """Read all visible cards in one browser round-trip and return only unseen reviews."""
        cards = self.review_cards(page)
        raw_items = await cards.evaluate_all(
            """
            (nodes, selectors) => nodes.map((card) => {
              const textOf = (values) => {
                for (const selector of values) {
                  const node = card.querySelector(selector);
                  const value = node?.textContent?.trim();
                  if (value) return value;
                }
                return null;
              };
              const attrOf = (values, attribute) => {
                for (const selector of values) {
                  const node = card.querySelector(selector);
                  const value = node?.getAttribute(attribute);
                  if (value) return value;
                }
                return null;
              };
              return {
                source_review_id: card.getAttribute("data-review-id"),
                author_name: textOf(selectors.author),
                text: textOf(selectors.text) || "",
                relative_date: textOf(selectors.date),
                owner_reply: textOf(selectors.reply),
                rating_label: attrOf(selectors.rating, "aria-label"),
              };
            })
            """,
            {
                "author": list(self.author_selectors),
                "text": list(self.review_text_selectors),
                "date": list(self.date_selectors),
                "reply": list(self.owner_reply_selectors),
                "rating": [
                    "span.kvMYJc",
                    'span[role="img"][aria-label*="星"]',
                    'span[role="img"]',
                ],
            },
        )
        reviews: list[ScrapedReview] = []
        for raw in raw_items:
            review = _review_from_raw(raw, source_url)
            if review is None:
                continue
            key = review.source_review_id or review.content_hash
            if key in seen_keys:
                continue
            seen_keys.add(key)
            reviews.append(review)
        return reviews, len(raw_items)

@dataclass(slots=True)
class MapsScraper:
    settings: Settings = field(default_factory=get_settings)
    adapter: MapsDomAdapter = field(default_factory=MapsDomAdapter)

    async def search(self, query: str, headless: bool | None = None) -> list[BusinessCandidate]:
        direct = _is_maps_url(query)
        url = query if direct else f"https://www.google.com/maps/search/{quote_plus(query)}?hl={self.settings.scrape_locale}"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.headless if headless is None else headless
            )
            try:
                context = await browser.new_context(locale=self.settings.scrape_locale)
                page = await context.new_page()
                page.set_default_timeout(self.settings.scrape_timeout_ms)
                await page.goto(url, wait_until="domcontentloaded")
                await self.adapter.accept_consent(page)
                if await self.adapter.is_blocked(page):
                    raise MapsBlockedError("Google 要求驗證，請改用可見瀏覽器後重試。")
                await self.adapter.wait_for_search_results(
                    page, timeout_ms=min(self.settings.scrape_timeout_ms, 12_000)
                )
                return await self.adapter.search_candidates(page)
            finally:
                await browser.close()

    async def crawl(
        self,
        *,
        job_id: str,
        maps_url: str,
        target: int,
        sort_order: str,
        headless: bool,
        on_batch: BatchCallback,
        on_progress: ProgressCallback,
        on_verification: VerificationCallback,
        is_canceled: CancelCallback,
        known_keys: set[str] | None = None,
        already_collected: int = 0,
        on_metrics: MetricsCallback | None = None,
    ) -> CrawlResult:
        diagnostic_dir = self.settings.diagnostics_dir / job_id
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        context: BrowserContext | None = None
        page: Page | None = None
        pending: list[ScrapedReview] = []
        tracing_started = False
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.settings.browser_profile_dir),
                    headless=headless,
                    locale=self.settings.scrape_locale,
                    viewport={"width": 1440, "height": 960},
                )
                if self.settings.diagnostics_trace:
                    await context.tracing.start(screenshots=True, snapshots=True, sources=False)
                    tracing_started = True
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(self.settings.scrape_timeout_ms)
                await page.goto(_with_locale(maps_url, self.settings.scrape_locale), wait_until="domcontentloaded")
                await self.adapter.accept_consent(page)
                await self._wait_if_blocked(page, headless, on_verification, is_canceled)
                await self.adapter.wait_for_search_results(
                    page, timeout_ms=min(self.settings.scrape_timeout_ms, 12_000)
                )

                place = await self.adapter.read_current_place(page)
                await on_progress(0, place.total_review_count, "正在開啟評論視窗")
                await self.adapter.open_reviews(page)
                await self.adapter.select_sort(page, sort_order)

                seen_keys = set(known_keys or ())
                newly_collected = 0
                no_growth = 0
                low_sample_recoveries = 0
                stop_reason = "no_growth"

                while True:
                    if await is_canceled():
                        raise MapsCanceledError("任務已取消")
                    await self._wait_if_blocked(page, headless, on_verification, is_canceled)
                    started = time.perf_counter()
                    reviews, visible_count = await self.adapter.parse_visible_reviews(
                        page, place.maps_url, seen_keys
                    )
                    parse_ms = round((time.perf_counter() - started) * 1000, 1)
                    before = newly_collected
                    for review in reviews:
                        pending.append(review)
                        newly_collected += 1
                        if len(pending) >= self.settings.persist_batch_size:
                            persist_started = time.perf_counter()
                            persisted_count = len(pending)
                            await on_batch(pending[:])
                            persist_ms = round((time.perf_counter() - persist_started) * 1000, 1)
                            pending.clear()
                            if on_metrics:
                                await on_metrics(
                                    {
                                        "visible_cards": visible_count,
                                        "new_reviews": persisted_count,
                                        "parse_ms": parse_ms,
                                        "persist_ms": persist_ms,
                                        "collected": already_collected + newly_collected,
                                    }
                                )
                        if already_collected + newly_collected >= target:
                            stop_reason = "target_reached"
                            break

                    current = min(already_collected + newly_collected, target)
                    await on_progress(
                        current,
                        place.total_review_count,
                        f"已讀取 {current} 則評論",
                    )
                    if current >= target:
                        break
                    if place.total_review_count and current >= place.total_review_count:
                        stop_reason = "total_reached"
                        break

                    no_growth = no_growth + 1 if newly_collected == before else 0
                    if no_growth >= self.settings.no_growth_limit:
                        low_sample_threshold = min(
                            target,
                            max(self.settings.persist_batch_size, 10),
                        )
                        if (
                            current < low_sample_threshold
                            and low_sample_recoveries < self.settings.browser_restart_limit
                        ):
                            low_sample_recoveries += 1
                            no_growth = 0
                            await on_progress(
                                current,
                                place.total_review_count,
                                "評論清單載入不完整，正在重新開啟後續評論",
                            )
                            if on_metrics:
                                await on_metrics(
                                    {
                                        "visible_cards": visible_count,
                                        "new_reviews": 0,
                                        "collected": current,
                                        "stop_reason": "recovering_low_sample",
                                        "recovery_attempt": low_sample_recoveries,
                                    }
                                )
                            await page.goto(
                                _with_locale(maps_url, self.settings.scrape_locale),
                                wait_until="domcontentloaded",
                            )
                            await self.adapter.accept_consent(page)
                            await self._wait_if_blocked(
                                page, headless, on_verification, is_canceled
                            )
                            await self.adapter.wait_for_search_results(
                                page,
                                timeout_ms=min(
                                    self.settings.scrape_timeout_ms,
                                    12_000,
                                ),
                            )
                            await self.adapter.open_reviews(page)
                            await self.adapter.select_sort(page, sort_order)
                            continue
                        break
                    await self._scroll_reviews(page)

                if already_collected + newly_collected == 0:
                    raise MapsScraperError(
                        "評論視窗已開啟，但沒有任何評論可解析；已保存診斷資料。"
                    )
                if pending:
                    persist_started = time.perf_counter()
                    final_count = len(pending)
                    await on_batch(pending)
                    if on_metrics:
                        await on_metrics(
                            {
                                "visible_cards": visible_count,
                                "new_reviews": final_count,
                                "parse_ms": parse_ms,
                                "persist_ms": round(
                                    (time.perf_counter() - persist_started) * 1000, 1
                                ),
                                "collected": already_collected + newly_collected,
                                "stop_reason": stop_reason,
                            }
                        )
                    pending.clear()
                return CrawlResult(
                    name=place.name,
                    address=place.address,
                    average_rating=place.average_rating,
                    total_review_count=place.total_review_count,
                    reviews_seen=min(already_collected + newly_collected, target),
                    stop_reason=stop_reason,
                )
            except MapsCanceledError:
                if pending:
                    await on_batch(pending)
                raise
            except Exception:
                if pending:
                    await on_batch(pending)
                if page is not None:
                    await self._save_diagnostics(page, diagnostic_dir)
                raise
            finally:
                if context is not None:
                    try:
                        if tracing_started:
                            await context.tracing.stop(path=str(diagnostic_dir / "trace.zip"))
                    except Exception:
                        pass
                    await context.close()

    async def _wait_if_blocked(
        self,
        page: Page,
        headless: bool,
        on_verification: VerificationCallback,
        is_canceled: CancelCallback,
    ) -> None:
        if not await self.adapter.is_blocked(page):
            return
        if headless:
            raise MapsBlockedError("無頭瀏覽器遇到 CAPTCHA 或異常流量驗證。")
        await on_verification("Google 要求人工驗證；請在瀏覽器完成後按「繼續」。")
        if await is_canceled():
            raise MapsCanceledError("任務已取消")
        await page.wait_for_timeout(500)
        if await self.adapter.is_blocked(page):
            raise MapsBlockedError("人工驗證尚未完成或頁面仍被阻擋。")

    async def _scroll_reviews(self, page: Page) -> None:
        # Maps replaces the review list while sorting and while virtualizing a
        # long list. A zero-card instant is therefore recoverable, not fatal.
        if not await self.adapter.wait_for_review_cards(
            page, timeout_ms=max(self.settings.scroll_wait_ms * 2, 2_500)
        ):
            return

        selector = ", ".join(self.adapter.review_card_selectors)
        previous = await page.evaluate(
            """
            (selector) => {
              const cards = [...document.querySelectorAll(selector)];
              const last = cards.at(-1);
              if (!last) return null;
              return last.getAttribute("data-review-id") || last.textContent?.slice(0, 160) || null;
            }
            """,
            selector,
        )

        # Scroll the nearest actual overflow container. This survives class-name
        # changes and virtualized lists where scrolling the last card alone no
        # longer causes Google Maps to request another page.
        await page.evaluate(
            """
            (selector) => {
              const cards = [...document.querySelectorAll(selector)];
              const last = cards.at(-1);
              if (!last) return false;
              let node = last.parentElement;
              let scroller = null;
              while (node && node !== document.body) {
                const style = getComputedStyle(node);
                const canOverflow = /(auto|scroll|overlay)/.test(style.overflowY);
                if (node.scrollHeight > node.clientHeight + 24 && canOverflow) {
                  scroller = node;
                  break;
                }
                node = node.parentElement;
              }
              if (scroller) {
                scroller.scrollTop = Math.max(scroller.scrollHeight - scroller.clientHeight, 0);
                last.scrollIntoView({block: "end", behavior: "auto"});
                scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
              } else {
                last.scrollIntoView({block: "end", behavior: "auto"});
              }
              return true;
            }
            """,
            selector,
        )
        try:
            await self.adapter.review_cards(page).last.hover(timeout=2_000)
            await page.mouse.wheel(0, 2_800)
        except Exception:
            pass
        try:
            await page.wait_for_function(
                """
                ({selector, previous}) => {
                  const cards = document.querySelectorAll(selector);
                  if (!cards.length) return false;
                  const last = cards[cards.length - 1];
                  const current = last.getAttribute("data-review-id")
                    || last.textContent?.slice(0, 160)
                    || null;
                  return current !== previous;
                }
                """,
                arg={"selector": selector, "previous": previous},
                timeout=max(self.settings.scroll_wait_ms, 3_000),
            )
        except PlaywrightTimeoutError:
            pass

    async def _save_diagnostics(self, page: Page, directory: Path) -> None:
        try:
            await page.screenshot(path=str(directory / "failure.png"), full_page=True)
            (directory / "failure.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass


async def _first_text(root: Locator, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        locator = root.locator(selector)
        if await locator.count():
            try:
                value = normalize_text(await locator.first.inner_text(timeout=1_000))
                if value:
                    return value
            except Exception:
                continue
    return None


async def _first_attribute(root: Locator, selectors: tuple[str, ...], attribute: str) -> str | None:
    for selector in selectors:
        locator = root.locator(selector)
        if await locator.count():
            try:
                value = await locator.first.get_attribute(attribute, timeout=1_000)
                if value:
                    return value
            except Exception:
                continue
    return None


def _review_from_raw(raw: dict, source_url: str) -> ScrapedReview | None:
    source_id = raw.get("source_review_id")
    author = normalize_text(raw.get("author_name"))
    text = normalize_text(raw.get("text"))
    rating = _parse_int(raw.get("rating_label"))
    if not any((source_id, author, text, rating)):
        return None
    content_hash = hashlib.sha256(
        "|".join(
            (
                _canonical_place_url(source_url),
                author,
                str(rating or ""),
                text,
            )
        ).encode("utf-8")
    ).hexdigest()
    return ScrapedReview(
        source_review_id=source_id,
        content_hash=content_hash,
        author_name=author or None,
        rating=rating,
        text=text,
        relative_date=normalize_text(raw.get("relative_date")) or None,
        owner_reply=normalize_text(raw.get("owner_reply")) or None,
        source_url=source_url,
    )


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    compact = value.replace(",", "").replace("，", "")
    match = re.search(r"\d+", compact)
    return int(match.group(0)) if match else None


def _guess_address(card_text: str, name: str) -> str | None:
    lines = [line.strip() for line in card_text.splitlines() if line.strip()]
    for line in lines:
        if line == name or re.fullmatch(r"[\d.,() ]+", line):
            continue
        if any(token in line for token in ("路", "街", "巷", "號", "區", "Road", "Street", "Ave")):
            return line
    return None


def _strip_prefixed_label(value: str | None, label: str) -> str | None:
    if not value:
        return None
    return normalize_text(re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", value))


def _is_maps_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    is_google = hostname in {"google.com", "google.com.tw"} or hostname.endswith(
        (".google.com", ".google.com.tw")
    )
    is_short_link = hostname in {"goo.gl", "maps.app.goo.gl"}
    return parsed.scheme in {"http", "https"} and (
        is_short_link or (is_google and parsed.path.startswith("/maps"))
    )


def _canonical_place_url(value: str) -> str:
    return value.split("?", 1)[0].split("/data=", 1)[0].rstrip("/")


def _with_locale(value: str, locale: str) -> str:
    separator = "&" if "?" in value else "?"
    return value if "hl=" in value else f"{value}{separator}hl={locale}"
