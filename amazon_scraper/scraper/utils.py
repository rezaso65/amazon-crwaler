import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Mapping


OUTPUT_DIR = Path("output")


def ensure_output_dir() -> Path:
    """
    Ensure that the output directory exists and return its Path.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def clean_text(value: str | None) -> str | None:
    """
    Normalize whitespace and strip leading/trailing spaces.
    Returns None if the resulting string is empty or the input is None.
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def safe_filename(value: str, max_length: int = 100) -> str:
    """
    Turn an arbitrary string into a filesystem-safe filename stem.
    """
    value = value.strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9._-]", "", value)
    if not value:
        value = "output"
    return value[:max_length]


def parse_price(raw: str | None) -> float | None:
    """
    Extract a numeric price from a raw price string (e.g. '$12.34').
    Returns None if no reasonable number can be found.
    """
    if not raw:
        return None
    # Keep digits, commas, dots
    match = re.findall(r"[0-9]+[0-9,\.]*", raw)
    if not match:
        return None
    number = match[0].replace(",", "")
    try:
        return float(number)
    except ValueError:
        return None


def save_json(records: List[Mapping[str, Any]], path: os.PathLike[str] | str) -> None:
    """
    Save list of dict-like records as pretty-printed JSON.
    """
    ensure_output_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def save_csv(
    records: Iterable[Mapping[str, Any]],
    path: os.PathLike[str] | str,
    field_order: List[str],
) -> None:
    """
    Save iterable of dict-like records as CSV with a fixed field order.
    Missing keys are written as empty strings.
    """
    import csv

    ensure_output_dir()
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=field_order, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row: dict[str, Any] = {}
            for field in field_order:
                value = record.get(field)
                if value is None:
                    row[field] = ""
                else:
                    row[field] = str(value)
            writer.writerow(row)


def save_to_json(records: List[Mapping[str, Any]], path: os.PathLike[str] | str) -> None:
    """
    Public helper for saving records to JSON.
    Thin wrapper around `save_json` to keep naming consistent.
    """
    save_json(records, path)


def save_to_csv(
    records: Iterable[Mapping[str, Any]],
    path: os.PathLike[str] | str,
    field_order: List[str],
) -> None:
    """
    Public helper for saving records to CSV.
    Thin wrapper around `save_csv` to keep naming consistent.
    """
    save_csv(records, path, field_order=field_order)

