from __future__ import annotations

import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scraper import ProductOfferScraper
from scraper.db import save_product_offer_result
from scraper.exporters.ai_export import build_ai_export, write_ai_export
from scraper.keyword_research import AlexaKeywordResearcher
from scraper.utils import OUTPUT_DIR, safe_filename, save_csv, save_json_data


BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
ALEXA_OUTPUT_DIR = OUTPUT_DIR / "alexa_keywords"
AI_EXPORT_DIR = OUTPUT_DIR / "ai_export"

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

ALEXA_FIELDS = [
    "keyword",
    "source",
    "source_status",
    "query_url",
    "searched_at",
    "result_count",
    "notes",
]


class ProductOffersRequest(BaseModel):
    product_url: str = Field(min_length=10)
    database_url: Optional[str] = None
    persist_db: bool = True
    headless: bool = False
    debug: bool = False
    max_offers: int = Field(default=100, ge=1, le=500)
    max_similar_products: int = Field(default=12, ge=0, le=50)


class AlexaKeywordsRequest(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    alexa_template: str = "https://www.alexa.com/search?q={query}"
    live_alexa: bool = False
    headless: bool = False


class AiExportRequest(BaseModel):
    database_url: Optional[str] = None


class JobRecord(BaseModel):
    id: str
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress_percent: int = 0
    current_step: str = "Queued"
    logs: List[str]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


load_dotenv()
app = FastAPI(title="Amazon Crawler Console API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=2)
jobs: Dict[str, JobRecord] = {}
jobs_lock = Lock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": _now()}


@app.get("/api/jobs")
def list_jobs() -> Dict[str, Any]:
    with jobs_lock:
        ordered = sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)
        return {"jobs": [_model_dump(job) for job in ordered]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return _model_dump(job)


@app.post("/api/jobs/product-offers")
def start_product_offers(request: ProductOffersRequest) -> Dict[str, Any]:
    job = _create_job("product_offers")
    executor.submit(_run_job, job.id, lambda: _product_offers_task(job.id, request))
    return _model_dump(job)


@app.post("/api/jobs/alexa-keywords")
def start_alexa_keywords(request: AlexaKeywordsRequest) -> Dict[str, Any]:
    keywords = _normalize_keywords(request.keywords)
    if not keywords:
        raise HTTPException(status_code=400, detail="At least one keyword is required")
    request.keywords = keywords
    job = _create_job("alexa_keywords")
    executor.submit(_run_job, job.id, lambda: _alexa_keywords_task(job.id, request))
    return _model_dump(job)


@app.post("/api/jobs/ai-export")
def start_ai_export(request: AiExportRequest) -> Dict[str, Any]:
    job = _create_job("ai_export")
    executor.submit(_run_job, job.id, lambda: _ai_export_task(job.id, request))
    return _model_dump(job)


if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
if OUTPUT_DIR.exists():
    app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")


def _product_offers_task(job_id: str, request: ProductOffersRequest) -> Dict[str, Any]:
    _progress(job_id, 5, "Preparing product offer scrape")
    _log(job_id, f"Scraping product offers: {request.product_url}")
    scraper = ProductOfferScraper(headless=request.headless, debug=request.debug)
    _progress(job_id, 15, "Opening Amazon product and offer pages")
    result = scraper.scrape(
        product_url=request.product_url,
        max_offers=request.max_offers,
        max_similar_products=request.max_similar_products,
    )

    asin = result["asin"]
    offers = result["offers"]
    _progress(job_id, 65, f"Extracted {len(offers)} offers for ASIN {asin}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"offers_{safe_filename(asin)}.json"
    csv_path = OUTPUT_DIR / f"offers_{safe_filename(asin)}.csv"
    _progress(job_id, 75, "Saving JSON and CSV outputs")
    save_json_data(result, json_path)
    save_csv(offers, csv_path, field_order=OFFER_FIELDS)
    _log(job_id, f"Saved {len(offers)} offers for ASIN {asin}")

    scrape_run_id = None
    if request.persist_db and request.database_url:
        _progress(job_id, 85, "Saving PostgreSQL product, seller, offer, and image rows")
        _log(job_id, "Saving product, sellers, offers, and images to PostgreSQL")
        scrape_run_id = save_product_offer_result(request.database_url, result, download_images=True)
        _log(job_id, f"PostgreSQL scrape_run_id={scrape_run_id}")
    elif request.persist_db:
        _log(job_id, "PostgreSQL skipped because DATABASE_URL was not provided")

    _progress(job_id, 95, "Packaging product offer result")
    return {
        "asin": asin,
        "offers_count": len(offers),
        "images_count": result.get("images_count", 0),
        "similar_products_count": result.get("similar_products_count", 0),
        "similar_sellers_count": result.get("similar_sellers_count", 0),
        "json_path": str(json_path.resolve()),
        "csv_path": str(csv_path.resolve()),
        "scrape_run_id": scrape_run_id,
    }


def _alexa_keywords_task(job_id: str, request: AlexaKeywordsRequest) -> Dict[str, Any]:
    _progress(job_id, 5, "Preparing keyword research")
    _log(job_id, f"Running Alexa keyword research for {len(request.keywords)} keywords")
    researcher = AlexaKeywordResearcher(
        search_url_template=request.alexa_template,
        headless=request.headless,
        live=request.live_alexa,
    )

    ALEXA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for index, keyword in enumerate(request.keywords, start=1):
        percent = 10 + int((index - 1) / max(len(request.keywords), 1) * 75)
        _progress(job_id, percent, f"Researching keyword {index}/{len(request.keywords)}")
        _log(job_id, f"[{index}/{len(request.keywords)}] {keyword}")
        result = researcher.research(keyword).to_dict()
        results.append(result)
        save_json_data(result, ALEXA_OUTPUT_DIR / f"{safe_filename(keyword)}.json")

    _progress(job_id, 90, "Saving keyword research summary files")
    summary_json_path = ALEXA_OUTPUT_DIR / "summary.json"
    summary_csv_path = ALEXA_OUTPUT_DIR / "summary.csv"
    save_json_data(results, summary_json_path)
    save_csv(results, summary_csv_path, field_order=ALEXA_FIELDS)

    return {
        "keywords_count": len(results),
        "summary_json_path": str(summary_json_path.resolve()),
        "summary_csv_path": str(summary_csv_path.resolve()),
    }


def _ai_export_task(job_id: str, request: AiExportRequest) -> Dict[str, Any]:
    _progress(job_id, 10, "Preparing AI export")
    _log(job_id, "Building AI-friendly export")
    _progress(job_id, 35, "Reading local outputs and optional PostgreSQL snapshot")
    export = build_ai_export(output_dir=OUTPUT_DIR, database_url=request.database_url)
    _progress(job_id, 80, "Writing JSON and JSONL exports")
    json_path, jsonl_path = write_ai_export(export, AI_EXPORT_DIR)
    return {
        "json_path": str(json_path.resolve()),
        "jsonl_path": str(jsonl_path.resolve()),
        "summary": export["summary"],
    }


def _create_job(kind: str) -> JobRecord:
    now = _now()
    job = JobRecord(
        id=str(uuid.uuid4()),
        kind=kind,
        status="queued",
        created_at=now,
        updated_at=now,
        logs=["Queued"],
    )
    with jobs_lock:
        jobs[job.id] = job
    return job


def _run_job(job_id: str, task: Callable[[], Dict[str, Any]]) -> None:
    _update_job(job_id, status="running", started_at=_now(), progress_percent=1, current_step="Started")
    _log(job_id, "Started")
    try:
        result = task()
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc), finished_at=_now(), current_step="Failed")
        _log(job_id, traceback.format_exc())
        return
    _update_job(job_id, status="succeeded", result=result, finished_at=_now(), progress_percent=100, current_step="Finished")
    _log(job_id, "Finished")


def _update_job(
    job_id: str,
    *,
    status: Optional[Literal["queued", "running", "succeeded", "failed"]] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    progress_percent: Optional[int] = None,
    current_step: Optional[str] = None,
) -> None:
    with jobs_lock:
        job = jobs[job_id]
        if status:
            job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if started_at is not None:
            job.started_at = started_at
        if finished_at is not None:
            job.finished_at = finished_at
        if progress_percent is not None:
            job.progress_percent = max(0, min(100, progress_percent))
        if current_step is not None:
            job.current_step = current_step
        job.updated_at = _now()


def _log(job_id: str, message: str) -> None:
    timestamped = f"{_now()} {message}"
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job.logs.append(timestamped)
            job.updated_at = _now()


def _progress(job_id: str, progress_percent: int, current_step: str) -> None:
    _update_job(job_id, progress_percent=progress_percent, current_step=current_step)
    _log(job_id, current_step)


def _normalize_keywords(keywords: List[str]) -> List[str]:
    seen: set[str] = set()
    normalized: List[str] = []
    for keyword in keywords:
        value = keyword.strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_dump(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()
