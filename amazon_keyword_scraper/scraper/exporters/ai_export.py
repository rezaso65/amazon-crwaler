from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from scraper.utils import OUTPUT_DIR


def build_ai_export(
    *,
    output_dir: Path = OUTPUT_DIR,
    database_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a structured, AI-friendly export from local output files and,
    when available, PostgreSQL tables.
    """
    file_payloads = _load_file_payloads(output_dir)
    db_payload = _load_database_payload(database_url) if database_url else None

    products = _products_from_files(file_payloads)
    offer_runs = _offer_runs_from_files(file_payloads)
    keyword_research = _keyword_research_from_files(file_payloads)

    export = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "AI-friendly export for product, seller offer, image, and keyword research analysis.",
        "sources": {
            "local_output_dir": str(output_dir.resolve()),
            "database_included": db_payload is not None,
            "source_files": [payload["path"] for payload in file_payloads],
        },
        "entities": {
            "products": products,
            "offer_runs": offer_runs,
            "keyword_research": keyword_research,
            "database_snapshot": db_payload,
        },
        "relationships": _build_relationships(products, offer_runs, keyword_research, db_payload),
    }
    export["summary"] = _build_summary(export)
    return export


def write_ai_export(export: Mapping[str, Any], target_dir: Path) -> tuple[Path, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / "ai_export.json"
    jsonl_path = target_dir / "ai_export.jsonl"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2, default=_json_default)

    jsonl_records = _flatten_jsonl(export)
    with jsonl_path.open("w", encoding="utf-8") as f:
        for record in jsonl_records:
            f.write(json.dumps(record, ensure_ascii=False, default=_json_default))
            f.write("\n")

    return json_path, jsonl_path


def _load_file_payloads(output_dir: Path) -> List[Dict[str, Any]]:
    if not output_dir.exists():
        return []

    payloads: List[Dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*.json")):
        if path.name == "ai_export.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payloads.append({"path": str(path.resolve()), "relative_path": str(path.relative_to(output_dir)), "data": data})
    return payloads


def _products_from_files(file_payloads: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    products: Dict[str, Dict[str, Any]] = {}

    for payload in file_payloads:
        data = payload["data"]
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or "asin" not in item or "title" not in item:
                    continue
                asin = str(item.get("asin") or "")
                if not asin:
                    continue
                products.setdefault(
                    asin,
                    {
                        "asin": asin,
                        "title": item.get("title"),
                        "product_url": item.get("product_url"),
                        "keywords": [],
                        "observations": [],
                    },
                )
                keyword = item.get("keyword")
                if keyword and keyword not in products[asin]["keywords"]:
                    products[asin]["keywords"].append(keyword)
                products[asin]["observations"].append({"source_file": payload["relative_path"], "data": item})

        if isinstance(data, dict) and data.get("asin") and data.get("product_url"):
            asin = str(data["asin"])
            products.setdefault(
                asin,
                {
                    "asin": asin,
                    "title": data.get("product_title"),
                    "product_url": data.get("product_url"),
                    "keywords": [],
                    "observations": [],
                },
            )
            products[asin]["title"] = data.get("product_title") or products[asin].get("title")
            products[asin]["product_url"] = data.get("product_url") or products[asin].get("product_url")
            products[asin]["images"] = data.get("images", [])
            products[asin]["observations"].append({"source_file": payload["relative_path"], "data_type": "offer_run"})

    return list(products.values())


def _offer_runs_from_files(file_payloads: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    for payload in file_payloads:
        data = payload["data"]
        if not isinstance(data, dict):
            continue
        if "offers" not in data or "asin" not in data:
            continue
        runs.append(
            {
                "source_file": payload["relative_path"],
                "phase": data.get("phase"),
                "asin": data.get("asin"),
                "product_url": data.get("product_url"),
                "product_title": data.get("product_title"),
                "offers_count": data.get("offers_count", len(data.get("offers", []))),
                "images_count": data.get("images_count", len(data.get("images", []))),
                "offers": data.get("offers", []),
                "images": data.get("images", []),
            }
        )
    return runs


def _keyword_research_from_files(file_payloads: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    research: List[Dict[str, Any]] = []
    for payload in file_payloads:
        data = payload["data"]
        if isinstance(data, dict) and data.get("source") == "alexa" and data.get("keyword"):
            item = dict(data)
            item["source_file"] = payload["relative_path"]
            research.append(item)
        elif isinstance(data, list) and payload["relative_path"].replace("\\", "/") == "alexa_keywords/summary.json":
            for item in data:
                if isinstance(item, dict) and item.get("keyword"):
                    summary_item = dict(item)
                    summary_item["source_file"] = payload["relative_path"]
                    research.append(summary_item)

    deduped: Dict[str, Dict[str, Any]] = {}
    for item in research:
        key = f"{item.get('source')}::{item.get('keyword')}::{item.get('searched_at')}"
        deduped[key] = item
    return list(deduped.values())


def _load_database_payload(database_url: Optional[str]) -> Optional[Dict[str, Any]]:
    if not database_url:
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return {"error": "psycopg is not installed; database snapshot was skipped."}

    queries = {
        "products": "SELECT asin, canonical_url, title, first_seen_at, last_seen_at FROM products ORDER BY asin",
        "sellers": "SELECT seller_key, seller_id, name, profile_url, first_seen_at, last_seen_at FROM sellers ORDER BY seller_key",
        "seller_offers": """
            SELECT id, scrape_run_id, asin, seller_key, position, price, raw_price, shipping,
                   condition, delivery, ships_from, sold_by, seller_rating,
                   seller_reviews_count, captured_at
            FROM seller_offers
            ORDER BY scrape_run_id, position
        """,
        "product_images": """
            SELECT id, asin, image_type, position, source_url, alt_text, content_type,
                   byte_size, first_seen_at, last_seen_at
            FROM product_images
            ORDER BY asin, position
        """,
        "scrape_runs": "SELECT id, phase, source_url, asin, started_at, finished_at FROM scrape_runs ORDER BY id",
    }

    snapshot: Dict[str, Any] = {}
    try:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                for name, query in queries.items():
                    try:
                        cur.execute(query)
                        snapshot[name] = [_json_clean(row) for row in cur.fetchall()]
                    except Exception as exc:
                        snapshot[name] = {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}

    return snapshot


def _build_relationships(
    products: List[Mapping[str, Any]],
    offer_runs: List[Mapping[str, Any]],
    keyword_research: List[Mapping[str, Any]],
    db_payload: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    relationships: List[Dict[str, Any]] = []

    for product in products:
        asin = product.get("asin")
        for keyword in product.get("keywords", []):
            relationships.append({"type": "keyword_found_product", "keyword": keyword, "asin": asin})

    for run in offer_runs:
        asin = run.get("asin")
        for offer in run.get("offers", []):
            relationships.append(
                {
                    "type": "seller_offered_product",
                    "asin": asin,
                    "seller_id": offer.get("seller_id"),
                    "seller_name": offer.get("seller_name") or offer.get("sold_by"),
                    "price": offer.get("price"),
                }
            )
        for image in run.get("images", []):
            relationships.append(
                {
                    "type": "product_has_image",
                    "asin": asin,
                    "image_type": image.get("image_type"),
                    "source_url": image.get("source_url"),
                }
            )

    for item in keyword_research:
        relationships.append(
            {
                "type": "keyword_researched_in_source",
                "keyword": item.get("keyword"),
                "source": item.get("source"),
                "source_status": item.get("source_status"),
            }
        )

    if db_payload and isinstance(db_payload.get("seller_offers"), list):
        for offer in db_payload["seller_offers"]:
            relationships.append(
                {
                    "type": "db_seller_offer_snapshot",
                    "scrape_run_id": offer.get("scrape_run_id"),
                    "asin": offer.get("asin"),
                    "seller_key": offer.get("seller_key"),
                    "price": offer.get("price"),
                }
            )

    return relationships


def _build_summary(export: Mapping[str, Any]) -> Dict[str, Any]:
    entities = export["entities"]
    db_snapshot = entities.get("database_snapshot") or {}
    return {
        "products_count": len(entities.get("products", [])),
        "offer_runs_count": len(entities.get("offer_runs", [])),
        "keyword_research_count": len(entities.get("keyword_research", [])),
        "relationships_count": len(export.get("relationships", [])),
        "db_products_count": len(db_snapshot.get("products", [])) if isinstance(db_snapshot.get("products"), list) else 0,
        "db_sellers_count": len(db_snapshot.get("sellers", [])) if isinstance(db_snapshot.get("sellers"), list) else 0,
        "db_offers_count": len(db_snapshot.get("seller_offers", [])) if isinstance(db_snapshot.get("seller_offers"), list) else 0,
        "db_images_count": len(db_snapshot.get("product_images", [])) if isinstance(db_snapshot.get("product_images"), list) else 0,
    }


def _flatten_jsonl(export: Mapping[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = [
        {"record_type": "export_metadata", "data": {k: export[k] for k in ("schema_version", "generated_at", "purpose", "sources", "summary")}}
    ]

    for product in export["entities"].get("products", []):
        records.append({"record_type": "product", "data": product})
    for run in export["entities"].get("offer_runs", []):
        records.append({"record_type": "offer_run", "data": run})
    for item in export["entities"].get("keyword_research", []):
        records.append({"record_type": "keyword_research", "data": item})
    for relationship in export.get("relationships", []):
        records.append({"record_type": "relationship", "data": relationship})

    db_snapshot = export["entities"].get("database_snapshot")
    if isinstance(db_snapshot, dict):
        for table_name, rows in db_snapshot.items():
            if isinstance(rows, list):
                for row in rows:
                    records.append({"record_type": f"db_{table_name}", "data": row})
    return records


def _json_clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    return _json_default(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return {"bytes_length": len(value)}
    return value
