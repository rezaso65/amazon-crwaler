from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from scraper import AmazonScraper
from scraper.utils import (
    OUTPUT_DIR,
    ensure_output_dir,
    safe_filename,
    save_to_csv,
    save_to_json,
)


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


def parse_args(argv: List[str]) -> str:
    """
    Very small argument parser: expects a single positional keyword.
    """
    if len(argv) < 2:
        print("Usage: python main.py \"<keyword>\"")
        sys.exit(1)
    keyword = " ".join(argv[1:]).strip()
    if not keyword:
        print("Error: keyword must not be empty.")
        sys.exit(1)
    return keyword


def main(argv: List[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv

    keyword = parse_args(argv)
    print(f"Searching Amazon for keyword: {keyword!r}")

    scraper = AmazonScraper(headless=False)
    try:
        products = scraper.search_keyword(keyword=keyword, max_results=50)
    except Exception as exc:
        print(f"An unexpected error occurred while scraping: {exc}")
        sys.exit(1)

    if not products:
        print("No products were found. Exiting without writing output.")
        sys.exit(0)

    ensure_output_dir()
    filename_stem = safe_filename(keyword)

    json_path = OUTPUT_DIR / f"{filename_stem}.json"
    csv_path = OUTPUT_DIR / f"{filename_stem}.csv"

    # `products` is already a list of dictionaries from the scraper.
    save_to_json(products, json_path)
    save_to_csv(products, csv_path, field_order=FIELDS)

    print(f"Successfully scraped {len(products)} products.")
    print(f"JSON saved to: {Path(json_path).resolve()}")
    print(f"CSV  saved to: {Path(csv_path).resolve()}")


if __name__ == "__main__":
    main()

