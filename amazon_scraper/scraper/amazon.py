from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set

from playwright.sync_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .utils import clean_text, parse_price


AMAZON_BASE_URL = "https://www.amazon.com"


@dataclass
class Product:
    position: int
    keyword: str
    asin: Optional[str]
    title: Optional[str]
    price: Optional[float]
    rating: Optional[float]
    reviews_count: Optional[int]
    is_sponsored: bool
    product_url: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AmazonScraper:
    """
    Simple Amazon search scraper using Playwright (sync API).
    """

    def __init__(
        self,
        headless: bool = False,
        timeout_ms: int = 15_000,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms

    def _open_browser(self, playwright) -> Browser:
        browser = playwright.chromium.launch(headless=self.headless, slow_mo=50)
        return browser

    def search_keyword(self, keyword: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """
        Entry point: perform a keyword search and collect up to `max_results` products.
        Returns a list of plain dictionaries ready for serialization.
        """
        products: List[Product] = []
        seen_asins: Set[str] = set()

        with sync_playwright() as p:
            browser = self._open_browser(p)
            context = browser.new_context()
            page = context.new_page()

            try:
                self._perform_search(page, keyword)
                page_index = 1
                while len(products) < max_results:
                    new_products = self._extract_page_results(
                        page=page,
                        keyword=keyword,
                        offset=len(products),
                        seen_asins=seen_asins,
                        max_results=max_results,
                    )
                    products.extend(new_products)

                    if len(products) >= max_results:
                        break

                    has_next = self._go_to_next_page(page, page_index)
                    if not has_next:
                        break
                    page_index += 1
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        # Convert dataclass instances to plain dicts before returning.
        return [p.to_dict() for p in products]

    def _perform_search(self, page: Page, keyword: str) -> None:
        """
        Open Amazon homepage and submit the search query.
        """
        page.set_default_timeout(self.timeout_ms)

        try:
            page.goto(AMAZON_BASE_URL, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            # Try to continue even if the initial load was slow.
            pass

        # Accept cookie / region dialogs if they appear (best-effort, selectors may vary).
        self._best_effort_close_overlays(page)

        search_box = page.locator("input#twotabsearchtextbox")
        search_box.fill(keyword)
        search_box.press("Enter")

        try:
            page.wait_for_selector('div[data-component-type="s-search-result"]', timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            # If no results show up, we still proceed and see what is available.
            pass

    def _extract_page_results(
        self,
        page: Page,
        keyword: str,
        offset: int,
        seen_asins: Set[str],
        max_results: int,
    ) -> List[Product]:
        """
        Extract product cards from the current search results page.
        """
        items = page.locator('div[data-component-type="s-search-result"]')
        count = items.count()
        results: List[Product] = []

        for index in range(count):
            if len(results) + offset >= max_results:
                break

            card = items.nth(index)

            asin = clean_text(card.get_attribute("data-asin"))
            if not asin:
                continue
            if asin in seen_asins:
                continue

            title, url = self._extract_title_and_url(card)
            price = self._extract_price(card)
            rating = self._extract_rating(card)
            reviews_count = self._extract_reviews_count(card)
            is_sponsored = self._detect_sponsored(card)

            if url and url.startswith("/"):
                url = AMAZON_BASE_URL + url

            product = Product(
                position=offset + len(results) + 1,
                keyword=keyword,
                asin=asin,
                title=title,
                price=price,
                rating=rating,
                reviews_count=reviews_count,
                is_sponsored=is_sponsored,
                product_url=url,
            )

            results.append(product)
            seen_asins.add(asin)

        return results

    def _extract_title_and_url(self, card) -> tuple[Optional[str], Optional[str]]:
        """
        Extract title text and product URL from a result card.
        Uses a primary and a fallback selector for robustness.
        """
        # Primary selector: common Amazon search result link
        title_link = card.locator("h2 a.a-link-normal.a-text-normal")
        if title_link.count() == 0:
            # Fallback selector: variant used on some layouts
            title_link = card.locator("h2 a.a-link-normal.s-link-style")

        if title_link.count() == 0:
            return None, None

        title = clean_text(title_link.first.inner_text(timeout=2_000))
        href = title_link.first.get_attribute("href")
        href = clean_text(href)
        return title, href

    def _extract_price(self, card) -> Optional[float]:
        """
        Extract price from a result card using primary and fallback selectors.
        """
        # Primary: typical Amazon price container
        price_node = card.locator("span.a-price span.a-offscreen")
        raw = None
        if price_node.count() > 0:
            try:
                raw = clean_text(price_node.first.inner_text(timeout=2_000))
            except PlaywrightTimeoutError:
                raw = None

        if not raw:
            # Fallback: any offscreen span that might carry a price
            fallback_price = card.locator("span.a-offscreen")
            if fallback_price.count() > 0:
                try:
                    raw = clean_text(fallback_price.first.inner_text(timeout=2_000))
                except PlaywrightTimeoutError:
                    raw = None

        return parse_price(raw)

    def _extract_rating(self, card) -> Optional[float]:
        """
        Extract rating as a float, e.g. '4.6 out of 5 stars'.
        """
        rating_node = card.locator("span.a-icon-alt")
        if rating_node.count() == 0:
            return None
        try:
            raw = clean_text(rating_node.first.inner_text(timeout=2_000))
        except PlaywrightTimeoutError:
            return None
        if not raw:
            return None

        # Expect something like "4.6 out of 5 stars"
        import re

        match = re.search(r"([0-9]*\.?[0-9]+)", raw)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _extract_reviews_count(self, card) -> Optional[int]:
        """
        Extract number of reviews from a result card.
        """
        # ARIA-based selector often used for ratings count
        node = card.locator('span[aria-label$="ratings"]')
        if node.count() == 0:
            # Fallback: common count styling in search results
            node = card.locator("span.a-size-base.s-underline-text")

        if node.count() == 0:
            return None

        try:
            raw = clean_text(node.first.inner_text(timeout=2_000))
        except PlaywrightTimeoutError:
            return None

        if not raw:
            return None

        import re

        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    def _detect_sponsored(self, card) -> bool:
        """
        Heuristically detect if a card is sponsored.
        """
        # Primary: dedicated sponsored label
        label = card.locator("span.s-sponsored-label-text")
        if label.count() > 0:
            try:
                raw = clean_text(label.first.inner_text(timeout=2_000))
                if raw and "sponsored" in raw.lower():
                    return True
            except PlaywrightTimeoutError:
                pass

        # Fallback: text snippets that contain 'Sponsored'
        text_candidates = card.locator("span, div")
        try:
            count = min(text_candidates.count(), 20)
        except Exception:
            count = 0

        for i in range(count):
            try:
                raw = clean_text(text_candidates.nth(i).inner_text(timeout=1_000))
            except PlaywrightTimeoutError:
                continue
            if raw and "sponsored" in raw.lower():
                return True

        return False

    def _go_to_next_page(self, page: Page, page_index: int) -> bool:
        """
        Navigate to the next search results page if possible.
        Returns True if a next page was opened, False otherwise.
        """
        next_button = page.locator("a.s-pagination-next:not(.s-pagination-disabled)")
        if next_button.count() == 0:
            return False

        try:
            next_button.first.click()
        except PlaywrightTimeoutError:
            return False

        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_selector('div[data-component-type="s-search-result"]', timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            # If the next page never loads results, stop paginating.
            return False

        return True

    def _best_effort_close_overlays(self, page: Page) -> None:
        """
        Try to dismiss region / cookie / sign-in overlays that sometimes appear.
        This is heuristic and best-effort only.
        """
        # These selectors are intentionally loose and wrapped in try/except
        # so that failures do not break the main flow.
        try:
            # Cookie consent or dismiss buttons (EU/other regions)
            buttons = page.locator("input#sp-cc-accept, button#sp-cc-accept")
            if buttons.count() > 0:
                buttons.first.click(timeout=2_000)
        except Exception:
            pass

        try:
            # Close region selection banner if present
            dismiss = page.locator("input[name='glowDoneButton'], button[name='glowDoneButton']")
            if dismiss.count() > 0:
                dismiss.first.click(timeout=2_000)
        except Exception:
            pass

