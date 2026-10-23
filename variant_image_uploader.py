"""
Step 6: upload the generated variant images to your backend.

For every row in variants.csv it takes the images in
    variant_images/<product_id>_<variant_id>[_<slug>]/
and sends them in ONE request to
    POST /api/products/{productId}/images/variant/{variantId}/bulk   (multipart, key "files")

Files the server confirms are moved into an "uploaded/" subfolder, so a
re-run only sends what's still pending and never creates duplicates.
Anything that fails stays in place and is listed in failed_variant_uploads.csv.

    python variant_image_uploader.py
"""
import csv
import logging
import mimetypes
import os
import shutil
import time
from typing import Dict, List, Optional

import requests

import config
from variant_images import IMAGE_EXTS, Variant, load_variants, variant_dir

log = logging.getLogger("variant_image_uploader")


def _pending_files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and f.lower().endswith(IMAGE_EXTS)
    )


def _mark_uploaded(folder: str, names: List[str]) -> None:
    done = os.path.join(folder, "uploaded")
    os.makedirs(done, exist_ok=True)
    for n in names:
        shutil.move(os.path.join(folder, n), os.path.join(done, n))


def upload_variant(session: requests.Session, v: Variant) -> Dict:
    folder = variant_dir(v)
    names = _pending_files(folder)
    if not names:
        return {"status": "nothing", "variant": v}

    url = f"{config.CREATE_PRODUCT_ENDPOINT.rstrip('/')}/{v.product_id}/images/variant/{v.variant_id}/bulk"
    last_error = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        handles = []
        try:
            files = []
            for n in names:
                fh = open(os.path.join(folder, n), "rb")
                handles.append(fh)
                files.append(("files", (n, fh, mimetypes.guess_type(n)[0] or "image/jpeg")))
            resp = session.post(url, files=files, timeout=180)
        except requests.RequestException as exc:
            last_error = str(exc)
            resp = None
        finally:
            for fh in handles:
                fh.close()

        if resp is not None:
            if resp.status_code == 201:
                ok_names = names
                try:
                    body = resp.json()
                    results = body.get("results")
                    if isinstance(results, list) and results:
                        ok = {r.get("fileName") for r in results if r.get("success")}
                        if ok:
                            ok_names = [n for n in names if n in ok]
                        elif body.get("failureCount", 0):
                            ok_names = []
                except ValueError:
                    pass
                _mark_uploaded(folder, ok_names)
                failed = len(names) - len(ok_names)
                if failed:
                    return {"status": "partial", "variant": v, "uploaded": len(ok_names),
                            "error": f"{failed} of {len(names)} files rejected by server"}
                return {"status": "ok", "variant": v, "uploaded": len(ok_names)}
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            log.warning("Variant %s/%s upload failed (attempt %d/%d): %s",
                        v.product_id, v.variant_id, attempt, config.MAX_RETRIES, last_error)
            if 400 <= resp.status_code < 500:
                break
        time.sleep(config.UPLOAD_DELAY_SECONDS * attempt)
    return {"status": "failed", "variant": v, "error": last_error}


def upload_all_variant_images(variants_csv: Optional[str] = None, progress_cb=None) -> Dict:
    variants = load_variants(variants_csv)
    session = requests.Session()
    session.headers.update(config.AUTH_HEADERS)

    results = {"uploaded_images": 0, "variants_ok": 0, "variants_skipped": 0, "variants_failed": 0, "failures": []}
    for i, v in enumerate(variants, 1):
        out = upload_variant(session, v)
        if out["status"] == "nothing":
            results["variants_skipped"] += 1
        else:
            results["uploaded_images"] += out.get("uploaded", 0)
            if out["status"] == "ok":
                results["variants_ok"] += 1
                log.info("Variant %s/%s: uploaded %d image(s)", v.product_id, v.variant_id, out["uploaded"])
            else:
                results["variants_failed"] += 1
                results["failures"].append({"product_id": v.product_id, "variant_id": v.variant_id,
                                            "error": out.get("error")})
        if progress_cb:
            progress_cb(i, len(variants), out)

    if results["failures"]:
        with open(config.FAILED_VARIANT_UPLOADS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["product_id", "variant_id", "error"])
            w.writeheader()
            w.writerows(results["failures"])
        log.info("Wrote %d failures to %s", len(results["failures"]), config.FAILED_VARIANT_UPLOADS_CSV)
    log.info("Variant upload complete: %d variants ok (%d images), %d nothing to upload, %d failed",
             results["variants_ok"], results["uploaded_images"], results["variants_skipped"],
             results["variants_failed"])
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    upload_all_variant_images()
