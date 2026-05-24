from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv

from scraper import AmazonScraper, ProductOfferScraper
from scraper.db import save_product_offer_result
from scraper.exporters.ai_export import build_ai_export, write_ai_export
from scraper.keyword_research import AlexaKeywordResearcher
from scraper.utils import OUTPUT_DIR, ensure_output_dir, safe_filename, save_csv, save_json, save_json_data


FIELDS = [
    "position",
    "keyword",
    "asin",
    "title",
    "price",
    "rating",
    "reviews_count",
    "is_sponsored",
    "product_url",
]

OFFER_FIELDS = [
    "position",
    "asin",
    "product_url",
    "product_title",
    "seller_name",
    "seller_id",
    "seller_profile_url",
    "price",
    "raw_price",
    "shipping",
    "condition",
    "delivery",
    "ships_from",
    "sold_by",
    "seller_rating",
    "seller_reviews_count",
    "offer_source",
]

ALEXA_OUTPUT_DIR = OUTPUT_DIR / "alexa_keywords"
ALEXA_FIELDS = [
    "keyword",
    "source",
    "source_status",
    "query_url",
    "searched_at",
    "result_count",
    "notes",
]
AI_EXPORT_DIR = OUTPUT_DIR / "ai_export"


def _flag_value(args: List[str], flag: str) -> Optional[str]:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        print(f"Error: {flag} requires a value.")
        sys.exit(1)
    return args[index + 1].strip()


def parse_args(argv: List[str]) -> tuple[Optional[str], Optional[str], Optional[str], bool, bool, bool, bool, bool]:
    """
    Argument parser:
    - positional keyword (optional in batch mode)
    - flags:
      --product-url <url> : scrape seller offers for one product
      --alexa-keywords [path] : run phase 3 for keywords in a file, defaults to keywords.txt
      --ai-export : build AI-friendly JSON and JSONL exports
      --live-alexa : attempt a live browser fetch against the configured Alexa URL
      --debug : enable debug prints
      --batch : read keywords from keywords.txt
      --no-db : skip PostgreSQL persistence even when DATABASE_URL is set
    """
    args = argv[1:]
    debug = "--debug" in args
    batch = "--batch" in args
    no_db = "--no-db" in args
    ai_export = "--ai-export" in args
    live_alexa = "--live-alexa" in args
    product_url = _flag_value(args, "--product-url")
    alexa_keywords_path = _optional_flag_value(args, "--alexa-keywords", default="keywords.txt")

    # Remove flags
    skip_next = False
    positional: List[str] = []
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"--product-url", "--alexa-keywords"}:
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        positional.append(arg)

    if ai_export:
        keyword = None
    elif alexa_keywords_path:
        keyword = None
    elif product_url:
        keyword = None
    elif batch:
        keyword = None
    else:
        if not positional:
            print('Usage: python main.py "<keyword>" [--debug] [--batch]')
            print('       python main.py --product-url "<amazon_product_url>" [--debug]')
            print('       python main.py --alexa-keywords [keywords.txt] [--live-alexa]')
            print("       python main.py --ai-export")
            sys.exit(1)
        keyword = " ".join(positional).strip()
        if not keyword:
            print("Error: keyword must not be empty.")
            sys.exit(1)

    return keyword, product_url, alexa_keywords_path, debug, batch, no_db, live_alexa, ai_export


def _optional_flag_value(args: List[str], flag: str, default: str) -> Optional[str]:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args) or args[index + 1].startswith("--"):
        return default
    return args[index + 1].strip()


def load_keywords_file(path: Path) -> List[str]:
    if not path.exists():
        print(f"[ERROR] Keywords file not found: {path}")
        sys.exit(1)

    raw = path.read_text(encoding="utf-8").splitlines()
    keywords = [line.strip() for line in raw if line.strip()]
    if not keywords:
        print(f"[ERROR] Keywords file is empty: {path}")
        sys.exit(1)
    return keywords


def run_single(keyword: str, debug: bool) -> None:
    print(f"Searching Amazon for keyword: {keyword!r} (debug={debug})")

    scraper = AmazonScraper(headless=False, debug=debug)
    try:
        products = scraper.scrape(keyword=keyword, max_results=50)
    except Exception as exc:
        print(f"[ERROR] An unexpected error occurred while scraping {keyword!r}: {exc}")
        return

    if not products:
        print(f"[WARN] No products were found for {keyword!r}. Skipping output.")
        return

    ensure_output_dir()
    filename_stem = safe_filename(keyword)

    json_path = OUTPUT_DIR / f"{filename_stem}.json"
    csv_path = OUTPUT_DIR / f"{filename_stem}.csv"

    save_json(products, json_path)
    save_csv(products, csv_path, field_order=FIELDS)

    print(f"[OK] {len(products)} products for {keyword!r}.")
    print(f"JSON saved to: {Path(json_path).resolve()}")
    print(f"CSV  saved to: {Path(csv_path).resolve()}")


def run_product_offers(product_url: str, debug: bool, persist_db: bool) -> None:
    print(f"Scraping seller offers for product URL: {product_url!r} (debug={debug})")

    scraper = ProductOfferScraper(headless=False, debug=debug)
    try:
        result = scraper.scrape(product_url=product_url, max_offers=100)
    except Exception as exc:
        print(f"[ERROR] Could not scrape seller offers: {exc}")
        return

    asin = result["asin"]
    offers = result["offers"]
    ensure_output_dir()

    json_path = OUTPUT_DIR / f"offers_{safe_filename(asin)}.json"
    csv_path = OUTPUT_DIR / f"offers_{safe_filename(asin)}.csv"

    save_json_data(result, json_path)
    save_csv(offers, csv_path, field_order=OFFER_FIELDS)

    print(f"[OK] {len(offers)} seller offers for ASIN {asin}.")
    print(f"JSON saved to: {Path(json_path).resolve()}")
    print(f"CSV  saved to: {Path(csv_path).resolve()}")

    database_url = os.getenv("DATABASE_URL")
    if not persist_db:
        print("PostgreSQL save skipped (--no-db).")
        return
    if not database_url:
        print("PostgreSQL save skipped (DATABASE_URL is not set).")
        return

    try:
        scrape_run_id = save_product_offer_result(database_url, result, download_images=True)
    except Exception as exc:
        print(f"[ERROR] PostgreSQL save failed: {exc}")
        return

    print(f"PostgreSQL saved scrape_run_id={scrape_run_id}.")


def run_alexa_keywords(keywords_path: str, live_alexa: bool) -> None:
    path = Path(keywords_path)
    keywords = load_keywords_file(path)
    search_url_template = os.getenv("ALEXA_SEARCH_URL_TEMPLATE", "https://www.alexa.com/search?q={query}")

    researcher = AlexaKeywordResearcher(
        search_url_template=search_url_template,
        headless=False,
        live=live_alexa,
    )

    ALEXA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Running Alexa keyword research for {len(keywords)} keywords (live={live_alexa}).")

    results: List[dict[str, Any]] = []
    for index, keyword in enumerate(keywords, start=1):
        print(f"=== [{index}/{len(keywords)}] {keyword!r} ===")
        result = researcher.research(keyword).to_dict()
        results.append(result)

        result_path = ALEXA_OUTPUT_DIR / f"{safe_filename(keyword)}.json"
        save_json_data(result, result_path)

    summary_json_path = ALEXA_OUTPUT_DIR / "summary.json"
    summary_csv_path = ALEXA_OUTPUT_DIR / "summary.csv"
    save_json_data(results, summary_json_path)
    save_csv(results, summary_csv_path, field_order=ALEXA_FIELDS)

    print(f"[OK] Alexa keyword research completed.")
    print(f"Summary JSON saved to: {Path(summary_json_path).resolve()}")
    print(f"Summary CSV  saved to: {Path(summary_csv_path).resolve()}")


def run_ai_export() -> None:
    database_url = os.getenv("DATABASE_URL")
    export = build_ai_export(output_dir=OUTPUT_DIR, database_url=database_url)
    json_path, jsonl_path = write_ai_export(export, AI_EXPORT_DIR)

    print("[OK] AI-friendly export completed.")
    print(f"JSON saved to: {Path(json_path).resolve()}")
    print(f"JSONL saved to: {Path(jsonl_path).resolve()}")
    print(f"Summary: {export['summary']}")


def main(argv: List[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv
    load_dotenv()

    keyword, product_url, alexa_keywords_path, debug, batch, no_db, live_alexa, ai_export = parse_args(argv)

    if ai_export:
        run_ai_export()
    elif alexa_keywords_path:
        run_alexa_keywords(keywords_path=alexa_keywords_path, live_alexa=live_alexa)
    elif product_url:
        run_product_offers(product_url=product_url, debug=debug, persist_db=not no_db)
    elif batch:
        # Batch mode: read up to 50 keywords from keywords.txt
        keywords = load_keywords_file(Path("keywords.txt"))[:50]
        print(f"Running batch mode for {len(keywords)} keywords (debug={debug}).")
        for idx, kw in enumerate(keywords, start=1):
            print(f"\n=== [{idx}/{len(keywords)}] {kw!r} ===")
            run_single(kw, debug=debug)
        print("\nBatch scraping completed.")
    else:
        run_single(keyword, debug=debug)


if __name__ == "__main__":
    main()

