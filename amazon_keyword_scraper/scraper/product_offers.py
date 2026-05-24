from __future__ import annotations

import re
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import Browser, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .amazon_scraper import AMAZON_BASE_URL, USER_AGENT
from .utils import clean_text


ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/product/|asin=)([A-Z0-9]{10})")


@dataclass
class SellerOffer:
    position: int
    asin: str
    product_url: str
    product_title: Optional[str]
    seller_name: Optional[str]
    seller_id: Optional[str]
    seller_profile_url: Optional[str]
    price: Optional[float]
    raw_price: Optional[str]
    shipping: Optional[str]
    condition: Optional[str]
    delivery: Optional[str]
    ships_from: Optional[str]
    sold_by: Optional[str]
    seller_rating: Optional[str]
    seller_reviews_count: Optional[int]
    offer_source: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProductImage:
    asin: str
    image_type: str
    position: int
    source_url: str
    alt_text: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SimilarProduct:
    asin: str
    title: Optional[str]
    product_url: str
    image_url: Optional[str]
    source: str
    seller_name: Optional[str] = None
    seller_id: Optional[str] = None
    seller_profile_url: Optional[str] = None
    price: Optional[float] = None
    raw_price: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProductOfferScraper:
    """
    Scrapes seller offers for a single Amazon product URL.
    """

    def __init__(self, headless: bool = False, timeout_ms: int = 15_000, debug: bool = False) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.debug = debug

    def scrape(
        self,
        product_url: str,
        max_offers: int = 100,
        max_similar_products: int = 12,
    ) -> Dict[str, Any]:
        asin = self.extract_asin(product_url)
        canonical_url = self._canonical_product_url(asin)

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            context = browser.new_context(
                viewport={"width": 1400, "height": 1800},
                user_agent=USER_AGENT,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                product_title = self._read_product_title(page, product_url)
                images = self._read_product_images(page, asin)
                buybox_offer = self._read_buybox_offer(page, asin, canonical_url, product_title)
                offer_diagnostics = self._offer_diagnostics(page)
                similar_products = self._read_similar_products(page, asin, max_similar_products)
                offers = self._read_aod_modal_offers(page, asin, canonical_url, product_title, max_offers)
                offer_source = "aod_modal" if offers else None
                if not offers:
                    offers = self._read_offers(page, asin, canonical_url, product_title, max_offers)
                    offer_source = "offer_listing" if offers else None
                if not offers and buybox_offer:
                    offers = [buybox_offer]
                    offer_source = "buy_box_fallback"
                if not offer_source:
                    offer_source = "none"
                similar_products = self._enrich_similar_products_with_sellers(page, similar_products)
                similar_sellers = self._similar_sellers_from_products(similar_products)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        return {
            "phase": "phase_2_product_media_and_postgres",
            "asin": asin,
            "product_url": canonical_url,
            "product_title": product_title,
            "offers_count": len(offers),
            "images_count": len(images),
            "offer_source": offer_source,
            "offer_diagnostics": offer_diagnostics,
            "similar_products_count": len(similar_products),
            "similar_sellers_count": len(similar_sellers),
            "similar_products": [item.to_dict() for item in similar_products],
            "similar_sellers": similar_sellers,
            "offers": [offer.to_dict() for offer in offers],
            "images": [image.to_dict() for image in images],
        }

    def _launch_browser(self, playwright) -> Browser:
        try:
            return playwright.chromium.launch(headless=self.headless)
        except PlaywrightError as exc:
            executable_path = _system_browser_path()
            if not executable_path:
                raise
            if self.debug:
                warning = f"[WARN] Playwright Chromium unavailable. Falling back to: {executable_path}"
                print(warning.encode("ascii", errors="ignore").decode("ascii"))
            return playwright.chromium.launch(headless=self.headless, executable_path=executable_path)

    @staticmethod
    def extract_asin(product_url: str) -> str:
        match = ASIN_RE.search(product_url)
        if not match:
            raise ValueError("Could not extract a 10-character ASIN from the product URL.")
        return match.group(1)

    @staticmethod
    def _canonical_product_url(asin: str) -> str:
        return f"{AMAZON_BASE_URL}/dp/{asin}"

    def _read_product_title(self, page: Page, product_url: str) -> Optional[str]:
        self._goto_page(page, product_url)

        for selector in ("#productTitle", "span#productTitle", "h1 span"):
            try:
                node = page.locator(selector)
                if node.count() > 0:
                    title = clean_text(node.first.inner_text(timeout=2_000))
                    if title:
                        return title
            except Exception:
                continue
        return None

    def _read_product_images(self, page: Page, asin: str) -> List[ProductImage]:
        images: List[ProductImage] = []
        seen_urls: set[str] = set()

        def add_image(image_type: str, source_url: Optional[str], alt_text: Optional[str]) -> None:
            source_url_clean = clean_text(source_url)
            if not source_url_clean:
                return
            if source_url_clean.startswith("//"):
                source_url_clean = f"https:{source_url_clean}"
            source_url_clean = urljoin(AMAZON_BASE_URL, source_url_clean)
            if source_url_clean in seen_urls:
                return
            seen_urls.add(source_url_clean)
            images.append(
                ProductImage(
                    asin=asin,
                    image_type=image_type,
                    position=len(images) + 1,
                    source_url=source_url_clean,
                    alt_text=clean_text(alt_text),
                )
            )

        try:
            landing = page.locator("#landingImage, #imgTagWrapperId img")
            if landing.count() > 0:
                node = landing.first
                alt_text = node.get_attribute("alt")
                dynamic_images = self._extract_dynamic_image_urls(node.get_attribute("data-a-dynamic-image"))
                if dynamic_images:
                    for url in dynamic_images:
                        add_image("main", url, alt_text)
                else:
                    add_image("main", node.get_attribute("data-old-hires") or node.get_attribute("src"), alt_text)
        except Exception:
            pass

        gallery = page.locator("#altImages img")
        for index in range(min(gallery.count(), 30)):
            try:
                node = gallery.nth(index)
                add_image("gallery", node.get_attribute("data-old-hires") or node.get_attribute("src"), node.get_attribute("alt"))
            except Exception:
                continue

        description = page.locator("#aplus img, #aplus_feature_div img, #productDescription img, #feature-bullets img")
        for index in range(min(description.count(), 80)):
            try:
                node = description.nth(index)
                add_image("description", node.get_attribute("data-src") or node.get_attribute("src"), node.get_attribute("alt"))
            except Exception:
                continue

        return images

    @staticmethod
    def _extract_dynamic_image_urls(raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, dict):
            return []
        return [url for url in parsed if isinstance(url, str)]

    def _read_buybox_offer(
        self,
        page: Page,
        asin: str,
        product_url: str,
        product_title: Optional[str],
    ) -> Optional[SellerOffer]:
        sold_by = self._extract_labeled_value(page, "Sold by")
        ships_from = self._extract_labeled_value(page, "Ships from")
        seller_name, seller_url = self._extract_buybox_seller(page)
        seller_url = urljoin(AMAZON_BASE_URL, seller_url) if seller_url else None
        seller_name = seller_name or sold_by

        raw_price = self._extract_text(page, ["#corePrice_feature_div .a-offscreen", ".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice"])
        price = self._extract_price(page)
        shipping = self._extract_text(page, ["#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE", "#deliveryBlockMessage", "[data-csa-c-delivery-price]"])
        delivery = self._extract_text(page, ["#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE", "#deliveryBlockMessage"])

        if not (seller_name or sold_by or ships_from or raw_price or price is not None):
            return None

        return SellerOffer(
            position=1,
            asin=asin,
            product_url=product_url,
            product_title=product_title,
            seller_name=seller_name,
            seller_id=self._extract_seller_id(seller_url),
            seller_profile_url=seller_url,
            price=price,
            raw_price=raw_price,
            shipping=shipping,
            condition=self._extract_text(page, ["#availability"]) or "Buy box",
            delivery=delivery,
            ships_from=ships_from,
            sold_by=sold_by or seller_name,
            seller_rating=None,
            seller_reviews_count=None,
            offer_source="buy_box_fallback",
        )

    def _read_similar_products(self, page: Page, asin: str, max_products: int) -> List[SimilarProduct]:
        if max_products <= 0:
            return []
        try:
            raw_items = page.evaluate(
                """
                ({ currentAsin, maxProducts }) => {
                  const asinPattern = /(?:\\/dp\\/|\\/gp\\/product\\/)([A-Z0-9]{10})/;
                  const sourceHints = [
                    ['sp_detail', 'sponsored_related'],
                    ['similarities_feature_div', 'similar_products'],
                    ['purchase-sims-feature', 'purchase_similarities'],
                    ['dp-sims', 'similar_products'],
                    ['comparison_table', 'comparison_table'],
                    ['HLCXComparisonWidget', 'comparison_widget'],
                    ['desktop-dp-sims_session-similarities-sims-feature', 'similar_products']
                  ];
                  const seen = new Set();
                  const items = [];
                  for (const link of document.querySelectorAll('a[href*="/dp/"], a[href*="/gp/product/"]')) {
                    const href = link.href || link.getAttribute('href') || '';
                    const match = href.match(asinPattern);
                    if (!match) continue;
                    const asin = match[1];
                    if (!asin || asin === currentAsin || seen.has(asin)) continue;
                    const text = (link.getAttribute('aria-label') || link.getAttribute('title') || link.innerText || '').trim();
                    const container = link.closest('[data-asin], .a-carousel-card, .sponsored-products-truncator-truncated, li, div') || link.parentElement;
                    const img = container ? container.querySelector('img') : link.querySelector('img');
                    const imageUrl = img ? (img.currentSrc || img.src || img.getAttribute('data-src')) : null;
                    let source = 'related_link';
                    for (const [idPart, label] of sourceHints) {
                      if (link.closest(`[id*="${idPart}"]`)) {
                        source = label;
                        break;
                      }
                    }
                    seen.add(asin);
                    items.push({ asin, title: text || null, product_url: `https://www.amazon.com/dp/${asin}`, image_url: imageUrl, source });
                    if (items.length >= maxProducts) break;
                  }
                  return items;
                }
                """,
                {"currentAsin": asin, "maxProducts": max_products},
            )
        except Exception:
            return []

        products: List[SimilarProduct] = []
        for item in raw_items or []:
            item_asin = clean_text(item.get("asin"))
            if not item_asin:
                continue
            image_url = clean_text(item.get("image_url"))
            if image_url and image_url.startswith("//"):
                image_url = f"https:{image_url}"
            products.append(
                SimilarProduct(
                    asin=item_asin,
                    title=clean_text(item.get("title")),
                    product_url=f"{AMAZON_BASE_URL}/dp/{item_asin}",
                    image_url=image_url,
                    source=clean_text(item.get("source")) or "related_link",
                )
            )
        return products

    def _enrich_similar_products_with_sellers(self, page: Page, products: List[SimilarProduct]) -> List[SimilarProduct]:
        for product in products:
            try:
                self._goto_page(page, product.product_url, attempts=2)
                title = self._read_current_product_title(page)
                offer = self._read_buybox_offer(page, product.asin, product.product_url, title or product.title)
                product.title = title or product.title
                if offer:
                    product.seller_name = offer.seller_name or offer.sold_by
                    product.seller_id = offer.seller_id
                    product.seller_profile_url = offer.seller_profile_url
                    product.price = offer.price
                    product.raw_price = offer.raw_price
            except Exception:
                continue
        return products

    def _read_current_product_title(self, page: Page) -> Optional[str]:
        for selector in ("#productTitle", "span#productTitle", "h1 span"):
            try:
                node = page.locator(selector)
                if node.count() > 0:
                    title = clean_text(node.first.inner_text(timeout=2_000))
                    if title:
                        return title
            except Exception:
                continue
        return None

    def _similar_sellers_from_products(self, products: List[SimilarProduct]) -> List[Dict[str, Any]]:
        sellers: Dict[str, Dict[str, Any]] = {}
        for product in products:
            seller_key = product.seller_id or product.seller_profile_url or product.seller_name
            if not seller_key:
                continue
            if seller_key not in sellers:
                sellers[seller_key] = {
                    "seller_name": product.seller_name,
                    "seller_id": product.seller_id,
                    "seller_profile_url": product.seller_profile_url,
                    "similar_product_count": 0,
                    "similar_product_asins": [],
                }
            sellers[seller_key]["similar_product_count"] += 1
            sellers[seller_key]["similar_product_asins"].append(product.asin)
        return list(sellers.values())

    def _extract_buybox_seller(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        selectors = [
            "#sellerProfileTriggerId",
            "#merchant-info a",
            "a[href*='seller=']",
        ]
        for selector in selectors:
            try:
                node = page.locator(selector)
                if node.count() == 0:
                    continue
                name = clean_text(node.first.inner_text(timeout=2_000))
                href = clean_text(node.first.get_attribute("href"))
                if name or href:
                    return name, href
            except Exception:
                continue
        return None, None

    def _read_offers(
        self,
        page: Page,
        asin: str,
        product_url: str,
        product_title: Optional[str],
        max_offers: int,
    ) -> List[SellerOffer]:
        offers_url = f"{AMAZON_BASE_URL}/gp/offer-listing/{asin}?ie=UTF8&condition=all"
        self._goto_page(page, offers_url)

        try:
            page.wait_for_selector("#aod-offer, .olpOffer", timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            pass

        cards = self._offer_cards(page)
        offers: List[SellerOffer] = []

        offers = self._extract_offer_cards(cards, asin, product_url, product_title, max_offers, "offer_listing")

        return offers

    def _read_aod_modal_offers(
        self,
        page: Page,
        asin: str,
        product_url: str,
        product_title: Optional[str],
        max_offers: int,
    ) -> List[SellerOffer]:
        trigger_selectors = [
            "#aod-ingress-link",
            "#buybox-see-all-buying-choices",
            "#olpLinkWidget_feature_div a",
            "span:has-text('See All Buying Options')",
            "a:has-text('See All Buying Options')",
            "input[aria-labelledby*='buybox-see-all-buying-choices']",
        ]

        for selector in trigger_selectors:
            try:
                trigger = page.locator(selector)
                if trigger.count() == 0:
                    continue
                trigger.first.click(timeout=3_000)
                page.wait_for_selector("#aod-offer", timeout=self.timeout_ms)
                cards = page.locator("#aod-offer")
                return self._extract_offer_cards(cards, asin, product_url, product_title, max_offers, "aod_modal")
            except Exception:
                continue
        return []

    def _extract_offer_cards(
        self,
        cards,
        asin: str,
        product_url: str,
        product_title: Optional[str],
        max_offers: int,
        offer_source: str,
    ) -> List[SellerOffer]:
        offers: List[SellerOffer] = []
        for index in range(min(cards.count(), max_offers)):
            card = cards.nth(index)
            seller_name, seller_url = self._extract_seller(card)
            seller_url = urljoin(AMAZON_BASE_URL, seller_url) if seller_url else None

            offer = SellerOffer(
                position=len(offers) + 1,
                asin=asin,
                product_url=product_url,
                product_title=product_title,
                seller_name=seller_name,
                seller_id=self._extract_seller_id(seller_url),
                seller_profile_url=seller_url,
                price=self._extract_price(card),
                raw_price=self._extract_text(card, [".a-price .a-offscreen", ".olpOfferPrice", ".a-offscreen"]),
                shipping=self._extract_text(card, ["#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE", ".olpShippingInfo", "[id*='shipping']"]),
                condition=self._extract_text(card, ["#aod-offer-heading", ".olpCondition", "[id*='condition']"]),
                delivery=self._extract_text(card, ["[id*='DELIVERY_BLOCK']", ".aod-delivery-promise", ".olpDeliveryColumn"]),
                ships_from=self._extract_labeled_value(card, "Ships from"),
                sold_by=self._extract_labeled_value(card, "Sold by") or seller_name,
                seller_rating=self._extract_text(card, ["[aria-label*='positive']", ".aod-seller-rating-count-class", ".olpSellerColumn"]),
                seller_reviews_count=self._extract_reviews_count(card),
                offer_source=offer_source,
            )

            if offer.seller_name or offer.price is not None or offer.raw_price:
                offers.append(offer)
        return offers

    def _offer_diagnostics(self, page: Page) -> Dict[str, Any]:
        selectors = {
            "aod_ingress": "#aod-ingress-link",
            "buybox_see_all_choices": "#buybox-see-all-buying-choices",
            "olp_link_widget": "#olpLinkWidget_feature_div a",
            "offer_listing_links": 'a[href*="offer-listing"]',
            "aod_links": 'a[href*="aod=1"]',
        }
        diagnostics: Dict[str, Any] = {}
        for key, selector in selectors.items():
            try:
                diagnostics[key] = page.locator(selector).count()
            except Exception:
                diagnostics[key] = None
        try:
            body = page.locator("body").inner_text(timeout=2_000).lower()
            diagnostics["mentions_other_sellers"] = "other sellers" in body
            diagnostics["mentions_buying_options"] = "buying options" in body
        except Exception:
            diagnostics["mentions_other_sellers"] = None
            diagnostics["mentions_buying_options"] = None
        return diagnostics

    def _goto_page(self, page: Page, url: str, attempts: int = 3) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                if self.debug:
                    message = f"[WARN] Navigation attempt {attempt}/{attempts} failed for {url}: {exc}"
                    print(message.encode("ascii", errors="ignore").decode("ascii"))
        if last_error:
            raise last_error

    def _offer_cards(self, page: Page):
        for selector in ("#aod-offer", ".olpOffer"):
            cards = page.locator(selector)
            if cards.count() > 0:
                return cards
        return page.locator("#aod-offer")

    def _extract_seller(self, card) -> tuple[Optional[str], Optional[str]]:
        selectors = [
            "#aod-offer-soldBy a",
            "a[href*='seller=']",
            "a[href*='shops/']",
            ".olpSellerName a",
        ]
        for selector in selectors:
            try:
                node = card.locator(selector)
                if node.count() == 0:
                    continue
                name = clean_text(node.first.inner_text(timeout=2_000))
                href = clean_text(node.first.get_attribute("href"))
                if name or href:
                    return name, href
            except Exception:
                continue

        sold_by = self._extract_labeled_value(card, "Sold by")
        return sold_by, None

    def _extract_text(self, card, selectors: List[str]) -> Optional[str]:
        for selector in selectors:
            try:
                node = card.locator(selector)
                if node.count() == 0:
                    continue
                text = clean_text(node.first.inner_text(timeout=2_000))
                if text:
                    return text
            except Exception:
                continue
        return None

    def _extract_price(self, card) -> Optional[float]:
        raw = self._extract_text(card, [".a-price .a-offscreen", ".olpOfferPrice", ".a-offscreen"])
        if not raw:
            return None
        match = re.search(r"[0-9]+[0-9,\.]*", raw)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    def _extract_labeled_value(self, card, label: str) -> Optional[str]:
        try:
            text = clean_text(card.inner_text(timeout=2_000))
        except Exception:
            return None
        if not text:
            return None

        pattern = re.compile(rf"{re.escape(label)}\s+(.+?)(?:\n|$)", re.IGNORECASE)
        match = pattern.search(text)
        return clean_text(match.group(1)) if match else None

    @staticmethod
    def _extract_seller_id(seller_url: Optional[str]) -> Optional[str]:
        if not seller_url:
            return None
        match = re.search(r"(?:seller=|me=)([A-Z0-9]+)", seller_url)
        return match.group(1) if match else None

    def _extract_reviews_count(self, card) -> Optional[int]:
        text = self._extract_text(card, [".aod-seller-rating-count-class", ".olpSellerColumn", "[aria-label*='ratings']"])
        if not text:
            return None
        match = re.search(r"([0-9][0-9,]*)\s+(?:ratings|reviews)", text, re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None


def _system_browser_path() -> Optional[str]:
    configured = os.getenv("BROWSER_EXECUTABLE_PATH")
    candidates = [
        configured,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None
