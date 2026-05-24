from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus

from playwright.sync_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .utils import clean_text, parse_rating, parse_review_count


AMAZON_SEARCH_URL = "https://www.amazon.com/s?k={query}"
AMAZON_BASE_URL = "https://www.amazon.com"


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


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
    Amazon keyword scraper using Playwright sync API.
    """

    def __init__(self, headless: bool = False, timeout_ms: int = 15_000, debug: bool = False) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.debug = debug

    def _launch_browser(self, playwright) -> Browser:
        return playwright.chromium.launch(headless=self.headless)


    def scrape(self, keyword: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """
        Scrape Amazon for the given keyword and return up to `max_results` products.
        """
        products: List[Product] = []
        seen_asins: Set[str] = set()
        debug_samples: List[Dict[str, Any]] = []

        query = quote_plus(keyword)
        search_url = AMAZON_SEARCH_URL.replace("{query}", query)

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            context = browser.new_context(
                viewport={"width": 1400, "height": 2000},
                user_agent=USER_AGENT,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                self._open_search_page(page, search_url)

                while len(products) < max_results:
                    new_products, page_debug = self._extract_page_results(
                        page=page,
                        keyword=keyword,
                        offset=len(products),
                        seen_asins=seen_asins,
                        max_results=max_results,
                    )
                    products.extend(new_products)
                    if self.debug and len(debug_samples) < 5:
                        remaining = 5 - len(debug_samples)
                        debug_samples.extend(page_debug[:remaining])

                    if len(products) >= max_results:
                        break

                    if not self._go_to_next_page(page):
                        break
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        if self.debug and debug_samples:
            print("=== DEBUG: first extracted product samples ===")
            for sample in debug_samples:
                print(sample)

        return [p.to_dict() for p in products]


    def _open_search_page(self, page: Page, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            # Continue even if the initial load was slow.
            pass


    def _extract_page_results(
        self,
        page: Page,
        keyword: str,
        offset: int,
        seen_asins: Set[str],
        max_results: int,
    ) -> Tuple[List[Product], List[Dict[str, Any]]]:
        items = page.locator('div[data-component-type="s-search-result"]')
        count = items.count()
        results: List[Product] = []
        debug_raw: List[Dict[str, Any]] = []

        for index in range(count):
            if len(results) + offset >= max_results:
                break

            card = items.nth(index)

            asin = clean_text(card.get_attribute("data-asin"))
            if not asin:
                continue
            if asin in seen_asins:
                continue

            title = self._extract_title(card)
            if not title:
                continue

            # Filter out clearly unrelated products (simple heuristics).
            title_lower = title.lower()
            if "mouse" not in title_lower:
                continue
            excluded_phrases = [
                "mouse pad",
                "mousepad",
                "charging pad",
                "keyboard and mouse",
                "mouse and keyboard",
            ]
            if any(phrase in title_lower for phrase in excluded_phrases):
                # Very simple conservative filter – skip obvious non-mouse or combos.
                continue

            href = self._extract_url(card)
            raw_price_text, parsed_price = self._parse_price_from_card(card)
            rating = self._extract_rating(card)
            reviews_count = self._extract_reviews_count(card)
            is_sponsored = self._detect_sponsored(card)

            product_url = None
            if href:
                if href.startswith("/"):
                    product_url = AMAZON_BASE_URL + href
                else:
                    product_url = href

            # Debug snapshot for this card (before validation).
            debug_raw.append(
                {
                    "asin": asin,
                    "title": title,
                    "href": href,
                    "product_url": product_url,
                    "raw_price": raw_price_text,
                    "parsed_price": parsed_price,
                    "rating": rating,
                    "reviews_count": reviews_count,
                    "is_sponsored": is_sponsored,
                }
            )

            # Output validation and normalization.
            price_value: Optional[float] = None
            if parsed_price:
                try:
                    price_value = float(parsed_price)
                except ValueError:
                    price_value = None

            product = Product(
                position=offset + len(results) + 1,
                keyword=keyword,
                asin=asin,
                title=title,
                price=price_value,
                rating=rating,
                reviews_count=reviews_count,
                is_sponsored=is_sponsored,
                product_url=product_url,
            )

            results.append(product)
            seen_asins.add(asin)

        return results, debug_raw


    def _extract_title(self, card) -> Optional[str]:
        """
        Extract title from `h2 a span` first, then fall back to `h2 span`.
        """
        try:
            primary = card.locator("h2 a span")
            target = primary if primary.count() > 0 else card.locator("h2 span")
            if target.count() == 0:
                return None
            text = target.first.inner_text(timeout=2_000)
            return clean_text(text)
        except Exception:
            return None


    def _extract_url(self, card) -> Optional[str]:
        """
        Extract href from `h2 a` (relative or absolute).
        """
        try:
            link = card.locator("h2 a")
            if link.count() == 0:
                return None
            href = link.first.get_attribute("href")
            return clean_text(href)
        except Exception:
            return None


    def _normalize_price_string(self, text: str | None) -> Optional[str]:
        """
        Normalize a raw price text into a simple '12.34' style string.
        """
        if not text:
            return None
        text = text.strip()
        import re

        match = re.search(r"[0-9]+[0-9,\.]*", text)
        if not match:
            return None
        number = match.group(0).replace(",", "")
        try:
            float(number)
        except ValueError:
            return None
        return number


    def _parse_price_from_card(self, card) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract the visible main product price from the card.

        Tries a set of selectors and returns:
        - raw_price_text: the first non-empty matched text
        - parsed_price: normalized string like '19.99', or None
        """
        selectors = [
            ".a-price .a-offscreen",
            "span.a-price span.a-offscreen",
            ".aok-offscreen",
        ]

        for selector in selectors:
            try:
                node = card.locator(selector)
                if node.count() == 0:
                    continue
                text = clean_text(node.first.inner_text(timeout=2_000))
                if not text:
                    continue
                parsed = self._normalize_price_string(text)
                if parsed:
                    return text, parsed
            except Exception:
                continue

        # As a final fallback, try whole + fraction:
        try:
            price_root = card.locator(".a-price")
            if price_root.count() > 0:
                whole_node = price_root.locator(".a-price-whole")
                fraction_node = price_root.locator(".a-price-fraction")
                if whole_node.count() > 0:
                    whole_text = clean_text(whole_node.first.inner_text(timeout=2_000)) or ""
                    fraction_text = ""
                    if fraction_node.count() > 0:
                        fraction_text = clean_text(fraction_node.first.inner_text(timeout=2_000)) or ""
                    raw = f"{whole_text}.{fraction_text}" if fraction_text else whole_text
                    parsed = self._normalize_price_string(raw)
                    if parsed:
                        return raw, parsed
        except Exception:
            pass

        return None, None


    def _extract_rating(self, card) -> Optional[float]:
        """
        Extract rating only from rating-specific elements:
        1) `i[data-cy="reviews-ratings-slot"] span.a-icon-alt`
        2) fallback: `.a-icon-alt`
        """
        try:
            primary = card.locator('i[data-cy="reviews-ratings-slot"] span.a-icon-alt')
            target = primary if primary.count() > 0 else card.locator(".a-icon-alt")
            if target.count() == 0:
                return None
            text = clean_text(target.first.inner_text(timeout=2_000))
            return parse_rating(text)
        except Exception:
            return None


    def _extract_reviews_count(self, card) -> Optional[int]:
        """
        Extract review count using review-related selectors only.
        Priority:
        1) `a[href*="#customerReviews"] span`
        2) `[aria-label$="ratings"]`
        """
        try:
            # 1) Anchor pointing to customer reviews section.
            anchor = card.locator('a[href*="#customerReviews"] span')
            if anchor.count() > 0:
                text = clean_text(anchor.first.inner_text(timeout=2_000))
                value = parse_review_count(text)
                if value is not None:
                    return value

            # 2) Elements with aria-label ending in "ratings".
            aria = card.locator('[aria-label$="ratings"]')
            if aria.count() > 0:
                text = clean_text(aria.first.inner_text(timeout=2_000))
                value = parse_review_count(text)
                if value is not None:
                    return value

            return None
        except Exception:
            return None


    def _detect_sponsored(self, card) -> bool:
        """
        Detect sponsored labels within the card only (text contains 'Sponsored').
        """
        try:
            # Prefer explicit 'Sponsored' labels using :has-text
            span_label = card.locator('span:has-text("Sponsored")')
            if span_label.count() > 0:
                return True

            div_label = card.locator('div:has-text("Sponsored")')
            if div_label.count() > 0:
                return True

            # Fallback: scan a few span/divs for 'Sponsored'
            candidate = card.locator("span, div")
            limit = min(candidate.count(), 20)
            for i in range(limit):
                text = clean_text(candidate.nth(i).inner_text(timeout=1_000))
                if text and "sponsored" in text.lower():
                    return True
        except Exception:
            pass
        return False


    def _go_to_next_page(self, page: Page) -> bool:
        """
        Navigate to the next results page if possible.
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
            return False
        return True

