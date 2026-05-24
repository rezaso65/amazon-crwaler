from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

import pandas as pd


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


def parse_rating(text: str | None) -> Optional[float]:
    """
    Parse rating from strings like '4.5 out of 5 stars'.
    Returns a float or None.
    """
    if not text:
        return None
    text = text.strip()
    match = re.search(r"([0-9]*\.?[0-9]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_review_count(text: str | None) -> Optional[int]:
    """
    Parse review count from text like '1,234 ratings'.
    Returns an int or None.
    """
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


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


def save_json(records: List[Mapping[str, Any]], path: os.PathLike[str] | str) -> None:
    """
    Save list of dict-like records as pretty-printed JSON.
    Uses pandas only for consistency with the CSV writer.
    """
    ensure_output_dir()
    # Use json directly to keep control over structure.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(records), f, ensure_ascii=False, indent=2)


def save_csv(
    records: Iterable[Mapping[str, Any]],
    path: os.PathLike[str] | str,
    field_order: List[str] | None = None,
) -> None:
    """
    Save iterable of dict-like records as CSV.
    If field_order is provided, columns are ordered accordingly.
    """
    ensure_output_dir()
    df = pd.DataFrame(list(records))
    if field_order is not None:
        # Ensure all requested columns exist in the frame.
        for col in field_order:
            if col not in df.columns:
                df[col] = None
        df = df[field_order]
    df.to_csv(path, index=False)

