"""
Uploads rows from the scraped CSV to your Spring Boot backend.

Your ProductController only exposes a single-item POST /api/products (no
true bulk endpoint), so "bulk upload" here means: fire a POST per row,
in parallel (a small thread pool) with retries and a short delay so we
don't hammer the server, and collect any failures into failed_uploads.csv
so you can retry just those.

Run directly for a CLI upload:
    python uploader.py
"""
import csv
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

import config
import slugs as slug_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("uploader")


def load_rows(csv_path: Optional[str] = None) -> List[Dict]:
    csv_path = csv_path or config.CSV_PATH
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_payload(row: Dict, slug: str) -> Dict:
    """Build the exact JSON body expected by ProductRequestDto.

    Only fields that exist on the DTO are sent - water/light/product_url
    live in the CSV for reference but are never part of this payload.
    """

    def as_decimal(value: str):
        try:
            return float(value) if value not in ("", None) else None
        except ValueError:
            return None

    short_description = (row.get("short_description") or "").strip()
    if len(short_description) > config.SHORT_DESCRIPTION_MAX_LEN:
        short_description = short_description[: config.SHORT_DESCRIPTION_MAX_LEN]

    name = (row.get("name") or "").strip()[: config.NAME_MAX_LEN]

    base_price = as_decimal(row.get("base_price"))
    discounted_price = as_decimal(row.get("discounted_price"))
    # basePrice is @NotNull on the DTO - fall back to discounted_price if the
    # scrape only found one price.
    if base_price is None:
        base_price = discounted_price

    # Some products on the site have compare_at_price < price (a stale/wrong
    # "was" price on Ugaoo's end), which scrapes as discounted > base and
    # trips the API's "discounted price cannot exceed base price" check.
    # Base price should always be the higher of the two, so reorder them
    # rather than dropping the product.
    if (
        base_price is not None
        and discounted_price is not None
        and discounted_price > base_price
    ):
        base_price, discounted_price = discounted_price, base_price

    return {
        "name": name,
        "slug": slug,
        "shortDescription": short_description or None,
        "description": (row.get("description") or "").strip() or None,
        "basePrice": base_price,
        "discountedPrice": discounted_price,
        "isActive": config.DEFAULT_IS_ACTIVE,
        "categoryId": config.CATEGORY_ID,
    }


def create_product(session: requests.Session, row: Dict, slug: str) -> Dict:
    payload = to_payload(row, slug)
    last_error = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.post(config.CREATE_PRODUCT_ENDPOINT, json=payload, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 201:
                return {"ok": True, "name": row.get("name"), "status": resp.status_code}
            body = resp.text[:300]
            # Re-running the same CSV (after fixing earlier failures, or just
            # by habit) will re-hit products that already made it into the DB
            # last time. That's not really a failure - treat "slug already
            # exists" as an idempotent skip instead of an error.
            if resp.status_code == 409 and "already exists" in body.lower():
                log.info("Skipping %r - already exists (slug %r)", row.get("name"), slug)
                return {"ok": True, "skipped": True, "name": row.get("name"), "status": resp.status_code}
            last_error = f"HTTP {resp.status_code}: {body}"
            log.warning("Create failed for %r (attempt %d/%d): %s", row.get("name"), attempt, config.MAX_RETRIES, last_error)
            # 4xx responses (validation/conflict errors) mean the payload
            # itself is rejected - retrying the exact same body won't help,
            # so don't burn the remaining attempts on it.
            if 400 <= resp.status_code < 500:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("Request error for %r (attempt %d/%d): %s", row.get("name"), attempt, config.MAX_RETRIES, last_error)
        time.sleep(config.UPLOAD_DELAY_SECONDS * attempt)
    return {"ok": False, "name": row.get("name"), "error": last_error}


def bulk_upload(csv_path: Optional[str] = None, progress_cb=None) -> Dict:
    rows = load_rows(csv_path)
    # The slug comes from the CSV's "slug" column (written by the scraper) so
    # the image steps use exactly the same value. Older CSVs without that
    # column fall back to generating from names.
    slugs = slug_utils.slugs_for_rows(rows)

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    session.headers.update(config.AUTH_HEADERS)

    results = {"success": 0, "skipped": 0, "failed": 0, "failures": []}
    total = len(rows)

    with ThreadPoolExecutor(max_workers=config.UPLOAD_WORKERS) as pool:
        futures = {
            pool.submit(create_product, session, row, slug): row
            for row, slug in zip(rows, slugs)
        }
        for i, future in enumerate(as_completed(futures), 1):
            outcome = future.result()
            if outcome["ok"] and outcome.get("skipped"):
                results["skipped"] += 1
            elif outcome["ok"]:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["failures"].append({**futures[future], "error": outcome.get("error")})
            if progress_cb:
                progress_cb(i, total, outcome)

    if results["failures"]:
        with open(config.FAILED_UPLOADS_CSV, "w", newline="", encoding="utf-8") as f:
            fieldnames = list(results["failures"][0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results["failures"])
        log.info("Wrote %d failed rows to %s", len(results["failures"]), config.FAILED_UPLOADS_CSV)

    log.info(
        "Upload complete: %d created, %d already existed (skipped), %d failed",
        results["success"], results["skipped"], results["failed"],
    )
    return results


if __name__ == "__main__":
    bulk_upload()
