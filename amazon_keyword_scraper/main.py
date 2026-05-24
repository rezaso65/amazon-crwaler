from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from scraper import AmazonScraper
from scraper.utils import OUTPUT_DIR, ensure_output_dir, safe_filename, save_csv, save_json


FIELDS = [
    "position",
    "keyword",
    "asin",
    "title",
    "main_price",
    "secondary_price",
    "rating",
    "reviews_count",
    "badge_text",
    "is_sponsored",
    "product_url",
]


def parse_args(argv: List[str]) -> tuple[Optional[str], bool, bool]:
    """
    Argument parser:
    - positional keyword (optional in batch mode)
    - flags:
      --debug : enable debug prints
      --batch : read keywords from keywords.txt
    """
    args = argv[1:]
    debug = "--debug" in args
    batch = "--batch" in args

    # Remove flags
    positional = [a for a in args if not a.startswith("--")]

    if batch:
        keyword = None
    else:
        if not positional:
            print("Usage: python main.py \"<keyword>\" [--debug] [--batch]")
            sys.exit(1)
        keyword = " ".join(positional).strip()
        if not keyword:
            print("Error: keyword must not be empty.")
            sys.exit(1)

    return keyword, debug, batch


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


def main(argv: List[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv

    keyword, debug, batch = parse_args(argv)

    if batch:
        # Batch mode: read up to 50 keywords from keywords.txt
        keywords_file = Path("keywords.txt")
        if not keywords_file.exists():
            print("[ERROR] keywords.txt not found. Please create it with one keyword per line.")
            sys.exit(1)

        raw = keywords_file.read_text(encoding="utf-8").splitlines()
        keywords = [line.strip() for line in raw if line.strip()]
        if not keywords:
            print("[ERROR] keywords.txt is empty.")
            sys.exit(1)

        keywords = keywords[:50]
        print(f"Running batch mode for {len(keywords)} keywords (debug={debug}).")
        for idx, kw in enumerate(keywords, start=1):
            print(f"\n=== [{idx}/{len(keywords)}] {kw!r} ===")
            run_single(kw, debug=debug)
        print("\nBatch scraping completed.")
    else:
        run_single(keyword, debug=debug)


if __name__ == "__main__":
    main()

