"""
Step 4: upload the downloaded images to your backend.

1. Pages through  GET /api/products?categoryId=...  and builds a
   slug -> product id map straight from the database (so it also covers
   products created in earlier runs). Saved to product_id_map.csv.
2. For every product in the CSV that has a downloaded image in
   product_images/<slug>.<ext>, sends
       POST /api/products/{id}/images     (multipart, key "file")
3. On success the file is moved into  product_images/<id>_<slug>/  so a
   re-run never uploads the same image twice. As a second safeguard, a
   product that already has images on the server (GET .../images) is skipped.

    python image_uploader.py
"""
import csv
import logging
import mimetypes
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

import config
import slugs as slug_utils
from uploader import load_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("image_uploader")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(config.AUTH_HEADERS)
    return s


# ---------------------------------------------------------------------------
# slug -> product id
# ---------------------------------------------------------------------------
def fetch_slug_id_map(session: requests.Session) -> Dict[str, int]:
    """Walk GET /api/products (Spring Page response) and map slug -> id."""
    mapping: Dict[str, int] = {}
    page = 0
    while True:
        params = {
            "page": page,
            "size": config.LIST_PAGE_SIZE,
            "sortBy": "id",
            "direction": "ASC",
        }
        if config.CATEGORY_ID is not None:
            params["categoryId"] = config.CATEGORY_ID
        resp = session.get(config.CREATE_PRODUCT_ENDPOINT, params=params, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"GET products page {page} failed: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        content = data.get("content", [])
        for item in content:
            if item.get("slug") and item.get("id") is not None:
                mapping[item["slug"]] = item["id"]
        log.info("Fetched products page %d (%d items, %d mapped so far)", page, len(content), len(mapping))
        if data.get("last", True) or not content:
            break
        page += 1

    with open(config.PRODUCT_ID_MAP_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slug", "product_id"])
        for slug, pid in mapping.items():
            writer.writerow([slug, pid])
    log.info("Saved slug -> id map (%d products) to %s", len(mapping), config.PRODUCT_ID_MAP_CSV)
    return mapping


# ---------------------------------------------------------------------------
# helpers for locating files
# ---------------------------------------------------------------------------
def find_loose_image(slug: str) -> Optional[str]:
    """product_images/<slug>.<ext> (not yet uploaded)."""
    if not os.path.isdir(config.IMAGES_DIR):
        return None
    for entry in os.listdir(config.IMAGES_DIR):
        full = os.path.join(config.IMAGES_DIR, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[0] == slug:
            return full
    return None


def already_filed(product_id: int, slug: str) -> bool:
    folder = os.path.join(config.IMAGES_DIR, f"{product_id}_{slug}")
    return os.path.isdir(folder) and bool(os.listdir(folder))


# ---------------------------------------------------------------------------
# upload one
# ---------------------------------------------------------------------------
def upload_one(session: requests.Session, slug: str, product_id: int, path: str) -> Dict:
    base = f"{config.CREATE_PRODUCT_ENDPOINT.rstrip('/')}/{product_id}/images"

    # Safeguard against duplicates: skip if the server already has images.
    try:
        existing = session.get(base, timeout=config.REQUEST_TIMEOUT)
        if existing.status_code == 200 and existing.json():
            return {"ok": True, "skipped": True, "slug": slug, "product_id": product_id}
    except (requests.RequestException, ValueError):
        pass  # can't tell - fall through and try the upload

    last_error = None
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            with open(path, "rb") as fh:
                resp = session.post(
                    base,
                    files={"file": (os.path.basename(path), fh, mime)},
                    timeout=max(config.REQUEST_TIMEOUT, 60),
                )
            if resp.status_code == 201:
                folder = os.path.join(config.IMAGES_DIR, f"{product_id}_{slug}")
                os.makedirs(folder, exist_ok=True)
                shutil.move(path, os.path.join(folder, os.path.basename(path)))
                return {"ok": True, "slug": slug, "product_id": product_id}
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            log.warning("Image upload failed for %s (attempt %d/%d): %s", slug, attempt, config.MAX_RETRIES, last_error)
            if 400 <= resp.status_code < 500:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("Image upload error for %s (attempt %d/%d): %s", slug, attempt, config.MAX_RETRIES, last_error)
        time.sleep(config.UPLOAD_DELAY_SECONDS * attempt)
    return {"ok": False, "slug": slug, "product_id": product_id, "error": last_error}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def upload_all_images(csv_path: Optional[str] = None, progress_cb=None) -> Dict:
    rows = load_rows(csv_path)
    csv_slugs = slug_utils.slugs_for_rows(rows)
    session = _session()

    slug_to_id = fetch_slug_id_map(session)

    results = {"uploaded": 0, "skipped": 0, "failed": 0, "no_product": 0, "no_image": 0, "failures": []}
    jobs: List[tuple] = []
    for slug in csv_slugs:
        pid = slug_to_id.get(slug)
        if pid is None:
            results["no_product"] += 1
            log.warning("No product with slug %r found in the API - was it uploaded?", slug)
            continue
        if already_filed(pid, slug):
            results["skipped"] += 1
            continue
        path = find_loose_image(slug)
        if not path:
            results["no_image"] += 1
            log.warning("No downloaded image for %r - run the image download step first", slug)
            continue
        jobs.append((slug, pid, path))

    total = len(jobs)
    log.info("%d image(s) to upload", total)
    with ThreadPoolExecutor(max_workers=config.IMAGE_UPLOAD_WORKERS) as pool:
        futures = [pool.submit(upload_one, session, s, pid, p) for s, pid, p in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            out = fut.result()
            if out["ok"] and out.get("skipped"):
                results["skipped"] += 1
            elif out["ok"]:
                results["uploaded"] += 1
            else:
                results["failed"] += 1
                results["failures"].append(out)
            if progress_cb:
                progress_cb(i, total, out)

    if results["failures"]:
        with open(config.FAILED_IMAGE_UPLOADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["slug", "product_id", "error"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results["failures"])
        log.info("Wrote %d failed image uploads to %s", len(results["failures"]), config.FAILED_IMAGE_UPLOADS_CSV)

    log.info(
        "Image upload complete: %d uploaded, %d skipped, %d failed, %d no product in API, %d no local image",
        results["uploaded"], results["skipped"], results["failed"], results["no_product"], results["no_image"],
    )
    return results


if __name__ == "__main__":
    upload_all_images()
