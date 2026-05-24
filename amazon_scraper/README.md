Amazon Keyword Scraper (Playwright + Python)
============================================

This project is a small, production-style scraper that:

- **Searches Amazon by keyword**
- **Collects up to the first 50 product results**
- **Saves the data to both JSON and CSV** inside an `output/` folder

It uses:

- **Python 3.11+**
- **Playwright (sync API)** with **headful Chromium** by default (browser window visible)


Project Overview
----------------

Given a single keyword, the scraper:

1. Opens `https://www.amazon.com` in Chromium via Playwright.
2. Submits the search query.
3. Iterates through the search results pages until it collects up to 50 products or runs out of pages.
4. For each valid product card (with a non-empty ASIN), it extracts:

   - `position` (1-based global index within the full result list)
   - `keyword` (the search keyword used)
   - `asin`
   - `title`
   - `price` (float, where extractable)
   - `rating` (float, e.g. 4.5)
   - `reviews_count` (integer)
   - `is_sponsored` (boolean)
   - `product_url` (absolute Amazon URL)

5. Deduplicates by **ASIN** (only the first occurrence is kept).
6. Saves the final list to:

   - `output/<keyword_sanitized>.json`
   - `output/<keyword_sanitized>.csv`


Project Structure
-----------------

```text
amazon_scraper/
  README.md
  requirements.txt
  .gitignore
  main.py
  scraper/
    __init__.py
    amazon.py
    utils.py
```

- `main.py`: Command-line entry point.
- `scraper/amazon.py`: Amazon-specific scraping logic using Playwright.
- `scraper/utils.py`: Shared helpers (text cleanup, filenames, JSON/CSV saving).
- `output/`: Created at runtime; holds all generated JSON and CSV files.


Setup
-----

### 1. Create and enter the project folder

If you haven’t already:

```bash
mkdir amazon_scraper
cd amazon_scraper
```

Copy the project files into this `amazon_scraper` directory so that `main.py`, `requirements.txt`, `README.md`, `.gitignore`, and the `scraper/` package are present.


### 2. Create a virtual environment (Python 3.11+)

**Windows (PowerShell):**

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**macOS / Linux (bash/zsh):**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

You should see your shell prompt change to indicate the virtual environment is active (e.g. it starts with `(.venv)`).


### 3. Install Python dependencies

Upgrade `pip` (recommended) and install the required packages:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```


### 4. Install Playwright Chromium browser binaries

Playwright separates Python bindings from browser binaries. Install **Chromium**:

```bash
python -m playwright install chromium
```

If you are on macOS/Linux and using `python3`, you can also use:

```bash
python3 -m playwright install chromium
```


How to Run
----------

From inside the `amazon_scraper` directory, with the virtualenv activated:

```bash
python main.py "<keyword>"
```

The keyword can contain spaces; it will be normalized to a filesystem-safe file name when saving outputs.

### Example command

Example for the keyword **"wireless mouse"**:

```bash
python main.py "wireless mouse"
```

This will:

- Open a **headful** Chromium window (you can watch it navigate and search).
- Search Amazon for `wireless mouse`.
- Collect up to **50 products** across one or more result pages.
- Save:

  - `output/wireless_mouse.json`
  - `output/wireless_mouse.csv`


Output Files
------------

All outputs are written into the `output/` directory (created automatically if missing).

For a keyword like `wireless mouse`, you’ll get:

- `output/wireless_mouse.json`
- `output/wireless_mouse.csv`

### JSON format

`output/<keyword>.json` is a UTF-8, pretty-printed JSON array:

```json
[
  {
    "position": 1,
    "keyword": "wireless mouse",
    "asin": "B012345678",
    "title": "Example Wireless Mouse",
    "price": 19.99,
    "rating": 4.5,
    "reviews_count": 1234,
    "is_sponsored": false,
    "product_url": "https://www.amazon.com/dp/B012345678"
  },
  ...
]
```

- Missing or unparseable values are stored as `null` (e.g. unknown price or rating).

### CSV format

`output/<keyword>.csv` contains one row per product with a fixed header:

```text
position,keyword,asin,title,price,rating,reviews_count,is_sponsored,product_url
```

- Missing values are written as empty strings.
- Booleans (`is_sponsored`) are written as `True` / `False`.


Selectors and Scraping Strategy
-------------------------------

**Result items**

- Primary container:

  - `div[data-component-type="s-search-result"]`

- The scraper further filters cards by requiring a **non-empty `data-asin`**, which helps skip non-product tiles and layout elements.

**Title & product URL**

- Primary:

  - `h2 a.a-link-normal.a-text-normal`

- Fallback:

  - `h2 a.a-link-normal.s-link-style`

The `href` is converted to an absolute URL by prefixing `https://www.amazon.com` if it starts with `/`.

**Price**

- Primary:

  - `span.a-price span.a-offscreen`

- Fallback:

  - The first `span.a-offscreen` inside the card

The text is then passed through a numeric parser that extracts the first number (removing currency symbols and thousands separators).

**Rating**

- Primary:

  - `span.a-icon-alt` (typically contains text such as `"4.6 out of 5 stars"`)

The code extracts the first floating-point number from this string.

**Reviews count**

- Primary:

  - `span[aria-label$="ratings"]`

- Fallback:

  - `span.a-size-base.s-underline-text`

The text is reduced to digits only and converted to an integer.

**Sponsored flag**

- Primary:

  - `span.s-sponsored-label-text` with text containing `"Sponsored"` (case-insensitive).

- Fallback:

  - Scan a limited number of `span` / `div` elements inside the card for text containing `"Sponsored"`.

**Pagination**

- After scraping the current page, the scraper checks for a **Next** button:

  - `a.s-pagination-next:not(.s-pagination-disabled)`

- If present and enabled, it clicks the link, waits for:

  - `page.wait_for_load_state("domcontentloaded")`
  - `page.wait_for_selector('div[data-component-type="s-search-result"]')`

- Pagination continues until either:

  - 50 products have been collected, or
  - No next page is available, or
  - The next page fails to load valid results within reasonable timeouts.


Deduplication and Skipping Invalid Cards
----------------------------------------

- Cards without a non-empty `data-asin` are **skipped**.
- The scraper maintains a set of seen ASINs; if an ASIN has already been added, the card is skipped.
- This ensures:

  - No duplicate products in the final output.
  - Layout-only or “empty” result tiles are not included.


Timeouts and Basic Error Handling
---------------------------------

- A default timeout (`timeout_ms`, e.g. 15 seconds) is applied via:

  - `page.set_default_timeout(timeout_ms)`

- Navigation and selector waits are wrapped in `try/except` for:

  - `playwright.sync_api.TimeoutError` (imported as `PlaywrightTimeoutError`)

- If the initial homepage load or result selector wait times out, the scraper logs the issue by continuing gracefully and attempting to proceed with whatever content is loaded.
- Region/cookie dialogs are handled on a **best-effort** basis (they are dismissed if recognizable buttons are found), but failures to dismiss them do not crash the scraper.
- At the end of scraping, the browser context and browser are closed in a `finally` block to avoid hanging processes.


Known Limitations
-----------------

- **Amazon’s HTML is not stable**:
  - This scraper relies on CSS selectors that are correct at the time of writing.
  - If Amazon changes its layout or class names, selectors may break and need updates.

- **Anti-bot measures**:
  - This project does **not** implement:
    - Proxy rotation
    - Captcha solving
    - Cookie or session persistence
  - Aggressive or frequent scraping may trigger captchas, blocks, or throttling.
  - Use moderate request rates and consider manual observation when the browser is running.

- **Single keyword only**:
  - The current `main.py` accepts one keyword at a time.
  - Extending to multiple keywords is straightforward (loop over a list and reuse the scraper), but outside the scope of this minimal example.

- **Region & localization**:
  - This scraper targets `https://www.amazon.com`.
  - Layout and language on other regional Amazon sites (e.g. `.de`, `.co.uk`) may differ and break selectors.


Notes About Amazon Selector Changes and Anti-Bot Behavior
---------------------------------------------------------

- **Selector fragility**:
  - All selectors (e.g. `data-component-type="s-search-result"`, `h2 a.a-link-normal.a-text-normal`, `span.a-price span.a-offscreen`) are based on **current** Amazon markup.
  - If results suddenly become empty or fields stop populating:
    - Inspect the page with your browser dev tools.
    - Update the selectors in `scraper/amazon.py` to match the new structure.
    - Keep selectors as **specific as needed but no more** (avoid overfitting to brittle class combinations).

- **Best practices to reduce blocking**:
  - Do not run the scraper with very high frequency.
  - Consider adding small random delays between page loads if you notice captchas or throttling.
  - Headful mode (visible browser) often behaves more like a real user and can help with some anti-bot heuristics, but it is **not** a guarantee.

- **Legal and ethical use**:
  - Always review and respect Amazon’s terms of service.
  - Use this code for learning, testing, or internal tools where allowed.
  - For production-grade systems, add rate limiting, proper error reporting, and robust handling for captchas / blocks.


Example End-to-End Session (Windows, PowerShell)
------------------------------------------------

```bash
# From some base folder
mkdir amazon_scraper
cd amazon_scraper

# (Copy project files here)

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt

python -m playwright install chromium

python main.py "wireless mouse"
```

After the run completes, check:

- `output/wireless_mouse.json`
- `output/wireless_mouse.csv`


