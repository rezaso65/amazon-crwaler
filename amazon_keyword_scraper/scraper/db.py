from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id BIGSERIAL PRIMARY KEY,
    phase TEXT NOT NULL,
    source_url TEXT NOT NULL,
    asin TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    asin TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    title TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sellers (
    seller_key TEXT PRIMARY KEY,
    seller_id TEXT,
    name TEXT,
    profile_url TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller_offers (
    id BIGSERIAL PRIMARY KEY,
    scrape_run_id BIGINT NOT NULL REFERENCES scrape_runs(id) ON DELETE CASCADE,
    asin TEXT NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
    seller_key TEXT REFERENCES sellers(seller_key) ON DELETE SET NULL,
    position INTEGER NOT NULL,
    price NUMERIC(12, 2),
    raw_price TEXT,
    shipping TEXT,
    condition TEXT,
    delivery TEXT,
    ships_from TEXT,
    sold_by TEXT,
    seller_rating TEXT,
    seller_reviews_count INTEGER,
    offer_source TEXT,
    raw_payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scrape_run_id, asin, position)
);

CREATE TABLE IF NOT EXISTS product_images (
    id BIGSERIAL PRIMARY KEY,
    asin TEXT NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
    image_type TEXT NOT NULL,
    position INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    alt_text TEXT,
    content_type TEXT,
    image_bytes BYTEA,
    byte_size INTEGER,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (asin, source_url)
);
"""


def save_product_offer_result(
    database_url: str,
    result: Mapping[str, Any],
    *,
    download_images: bool = True,
) -> int:
    """
    Persist a product-offer scrape result into PostgreSQL.
    Returns the created scrape_runs.id.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for PostgreSQL storage. Run: pip install -r requirements.txt") from exc

    now = datetime.now(timezone.utc)
    asin = str(result["asin"])
    source_url = str(result["product_url"])

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(
                """
                INSERT INTO scrape_runs (phase, source_url, asin, started_at, finished_at, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    result.get("phase", "phase_2_product_media_and_postgres"),
                    source_url,
                    asin,
                    now,
                    now,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            scrape_run_id = int(cur.fetchone()[0])

            cur.execute(
                """
                INSERT INTO products (asin, canonical_url, title, last_seen_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (asin) DO UPDATE SET
                    canonical_url = EXCLUDED.canonical_url,
                    title = COALESCE(EXCLUDED.title, products.title),
                    last_seen_at = now()
                """,
                (asin, source_url, result.get("product_title")),
            )

            for image in result.get("images", []):
                content_type = None
                image_bytes = None
                byte_size = None
                if download_images:
                    content_type, image_bytes = _download_image_bytes(image.get("source_url"))
                    byte_size = len(image_bytes) if image_bytes else None

                cur.execute(
                    """
                    INSERT INTO product_images (
                        asin, image_type, position, source_url, alt_text,
                        content_type, image_bytes, byte_size, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (asin, source_url) DO UPDATE SET
                        image_type = EXCLUDED.image_type,
                        position = EXCLUDED.position,
                        alt_text = COALESCE(EXCLUDED.alt_text, product_images.alt_text),
                        content_type = COALESCE(EXCLUDED.content_type, product_images.content_type),
                        image_bytes = COALESCE(EXCLUDED.image_bytes, product_images.image_bytes),
                        byte_size = COALESCE(EXCLUDED.byte_size, product_images.byte_size),
                        last_seen_at = now()
                    """,
                    (
                        asin,
                        image.get("image_type"),
                        image.get("position"),
                        image.get("source_url"),
                        image.get("alt_text"),
                        content_type,
                        image_bytes,
                        byte_size,
                    ),
                )

            for offer in result.get("offers", []):
                seller_key = _seller_key(offer)
                if seller_key:
                    cur.execute(
                        """
                        INSERT INTO sellers (seller_key, seller_id, name, profile_url, last_seen_at)
                        VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT (seller_key) DO UPDATE SET
                            seller_id = COALESCE(EXCLUDED.seller_id, sellers.seller_id),
                            name = COALESCE(EXCLUDED.name, sellers.name),
                            profile_url = COALESCE(EXCLUDED.profile_url, sellers.profile_url),
                            last_seen_at = now()
                        """,
                        (
                            seller_key,
                            offer.get("seller_id"),
                            offer.get("seller_name") or offer.get("sold_by"),
                            offer.get("seller_profile_url"),
                        ),
                    )

                cur.execute(
                    """
                    INSERT INTO seller_offers (
                        scrape_run_id, asin, seller_key, position, price, raw_price,
                        shipping, condition, delivery, ships_from, sold_by,
                        seller_rating, seller_reviews_count, offer_source, raw_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (scrape_run_id, asin, position) DO NOTHING
                    """,
                    (
                        scrape_run_id,
                        asin,
                        seller_key,
                        offer.get("position"),
                        offer.get("price"),
                        offer.get("raw_price"),
                        offer.get("shipping"),
                        offer.get("condition"),
                        offer.get("delivery"),
                        offer.get("ships_from"),
                        offer.get("sold_by"),
                        offer.get("seller_rating"),
                        offer.get("seller_reviews_count"),
                        offer.get("offer_source"),
                        json.dumps(offer, ensure_ascii=False),
                    ),
                )

        conn.commit()

    return scrape_run_id


def _seller_key(offer: Mapping[str, Any]) -> Optional[str]:
    seller_id = offer.get("seller_id")
    if seller_id:
        return f"id:{seller_id}"
    profile_url = offer.get("seller_profile_url")
    if profile_url:
        return f"url:{profile_url}"
    name = offer.get("seller_name") or offer.get("sold_by")
    if name:
        return f"name:{str(name).strip().lower()}"
    return None


def _download_image_bytes(source_url: Optional[str]) -> tuple[Optional[str], Optional[bytes]]:
    if not source_url:
        return None, None
    try:
        response = requests.get(
            source_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return None, None

    content_type = response.headers.get("content-type")
    return content_type, response.content
