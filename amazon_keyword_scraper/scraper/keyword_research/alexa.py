from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from scraper.utils import clean_text

from .models import KeywordResearchResult


DEFAULT_ALEXA_SEARCH_URL_TEMPLATE = "https://www.alexa.com/search?q={query}"
ALEXA_RETIRED_NOTE = (
    "Alexa Internet's public traffic ranking and SEO tools were retired on 2022-05-01. "
    "This result records the keyword request separately so downstream AI/export steps can "
    "distinguish unavailable Alexa data from missing scraper output."
)


class AlexaKeywordResearcher:
    """
    Keyword research adapter for Alexa-style keyword lookups.

    Alexa Internet is retired, so the default mode records a structured
    unavailable result. Set live=True to attempt a best-effort browser fetch
    against the configured search URL template.
    """

    def __init__(
        self,
        *,
        search_url_template: str = DEFAULT_ALEXA_SEARCH_URL_TEMPLATE,
        headless: bool = False,
        timeout_ms: int = 15_000,
        live: bool = False,
    ) -> None:
        self.search_url_template = search_url_template
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.live = live

    def research_many(self, keywords: Iterable[str]) -> List[Dict[str, Any]]:
        return [self.research(keyword).to_dict() for keyword in keywords if keyword.strip()]

    def research(self, keyword: str) -> KeywordResearchResult:
        keyword = keyword.strip()
        query_url = self._query_url(keyword)
        searched_at = datetime.now(timezone.utc).isoformat()

        if not self.live:
            return KeywordResearchResult(
                keyword=keyword,
                source="alexa",
                source_status="unavailable_retired",
                query_url=query_url,
                searched_at=searched_at,
                result_count=0,
                results=[],
                notes=ALEXA_RETIRED_NOTE,
            )

        return self._research_live(keyword=keyword, query_url=query_url, searched_at=searched_at)

    def _query_url(self, keyword: str) -> str:
        query = quote_plus(keyword)
        return self.search_url_template.replace("{query}", query).replace("{keyword}", query)

    def _research_live(self, keyword: str, query_url: str, searched_at: str) -> KeywordResearchResult:
        snippets: List[Dict[str, Any]] = []
        page_title: Optional[str] = None
        status = "live_attempted"
        notes = ALEXA_RETIRED_NOTE

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(viewport={"width": 1366, "height": 1600})
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                try:
                    page.goto(query_url, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    status = "live_timeout"

                try:
                    page_title = clean_text(page.title())
                except Exception:
                    page_title = None

                snippets = self._extract_visible_links(page)
                if snippets:
                    status = "live_results_extracted"
                    notes = "Live page was reachable; extracted visible links/snippets from the configured Alexa URL."
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        if page_title:
            snippets.insert(0, {"type": "page_title", "title": page_title, "url": query_url})

        return KeywordResearchResult(
            keyword=keyword,
            source="alexa",
            source_status=status,
            query_url=query_url,
            searched_at=searched_at,
            result_count=len(snippets),
            results=snippets,
            notes=notes,
        )

    def _extract_visible_links(self, page) -> List[Dict[str, Any]]:
        links = page.locator("a[href]")
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for index in range(min(links.count(), 50)):
            try:
                link = links.nth(index)
                text = clean_text(link.inner_text(timeout=1_000))
                href = clean_text(link.get_attribute("href"))
            except Exception:
                continue

            if not href or href in seen:
                continue
            seen.add(href)
            results.append({"type": "link", "title": text, "url": href})

        return results
