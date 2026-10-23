"""
Step 3: download each product's first image.

Reads ugaoo_plants.csv (needs the "slug" and "image_url" columns written by
the scraper) and saves every image as:

    product_images/<slug>.<ext>

The file is keyed by slug because no product id exists yet at this point.
Re-running is safe: images that are already on disk are skipped.

    python image_downloader.py
"""
import logging
import mimetypes
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional
from urllib.parse import urlparse

import requests

import config
import slugs as slug_utils
from uploader import load_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("image_downloader")

KNOWN_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def find_existing_image(slug: str) -> Optional[str]:
    """Return the path of an already-downloaded image for this slug, if any
    (either loose in IMAGES_DIR, or already moved into <id>_<slug>/)."""
    if not os.path.isdir(config.IMAGES_DIR):
        return None
    for entry in os.listdir(config.IMAGES_DIR):
        full = os.path.join(config.IMAGES_DIR, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[0] == slug:
            return full
        if os.path.isdir(full) and entry.endswith("_" + slug):
            for inner in os.listdir(full):
                return os.path.join(full, inner)
    return None


def _extension_for(url: str, content_type: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in KNOWN_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in KNOWN_EXTS or guessed == ".jpe":
        return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def download_one(session: requests.Session, slug: str, url: str) -> Dict:
    if not url:
        return {"slug": slug, "ok": False, "error": "no image_url in CSV"}
    if url.startswith("//"):
        url = "https:" + url

    existing = find_existing_image(slug)
    if existing:
        return {"slug": slug, "ok": True, "skipped": True, "path": existing}

    last_error = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                ext = _extension_for(url, resp.headers.get("Content-Type", ""))
                path = os.path.join(config.IMAGES_DIR, slug + ext)
                with open(path, "wb") as f:
                    f.write(resp.content)
                return {"slug": slug, "ok": True, "path": path}
            last_error = f"HTTP {resp.status_code}"
            if 400 <= resp.status_code < 500:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(config.REQUEST_DELAY_SECONDS * attempt)
    log.warning("Could not download image for %s: %s", slug, last_error)
    return {"slug": slug, "ok": False, "error": last_error}


def download_all(csv_path: Optional[str] = None, progress_cb=None) -> Dict:
    rows = load_rows(csv_path)
    slugs = slug_utils.slugs_for_rows(rows)
    os.makedirs(config.IMAGES_DIR, exist_ok=True)

    session = requests.Session()
    session.headers.update(config.HEADERS)

    results = {"downloaded": 0, "skipped": 0, "failed": 0}
    total = len(rows)
    with ThreadPoolExecutor(max_workers=config.IMAGE_DOWNLOAD_WORKERS) as pool:
        futures = [
            pool.submit(download_one, session, slug, (row.get("image_url") or "").strip())
            for row, slug in zip(rows, slugs)
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            out = fut.result()
            if not out["ok"]:
                results["failed"] += 1
            elif out.get("skipped"):
                results["skipped"] += 1
            else:
                results["downloaded"] += 1
            if progress_cb:
                progress_cb(i, total, out)

    log.info(
        "Image download complete: %d downloaded, %d already on disk, %d failed",
        results["downloaded"], results["skipped"], results["failed"],
    )
    return results


if __name__ == "__main__":
    download_all()
