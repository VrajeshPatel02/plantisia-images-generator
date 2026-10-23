"""
Step 5: generate 4-5 multi-angle AI images for every product variant.

Input  : variants.csv  (product_id, variant_id, product_name, variant_label[, slug])
Output : variant_images/<product_id>_<variant_id>[_<slug>]/01_front.jpg ... 05_lifestyle.jpg

Providers (config.IMAGE_PROVIDER or --provider):
  cloudflare  Free daily quota (10,000 neurons/day, no card). With a reference photo it
              uses FLUX.2 [klein] (image-guided); without one, FLUX.1 schnell (text only).
              Stops cleanly when the quota is used up - run again the next day.
  openai      gpt-image (paid), also image-guided via the reference photo.

Reference photo = the product's real image already stored in your database. It is looked up in
this order: local product_images/ folder -> reference_cache/<product_id>.jpg -> your API
(GET /api/products/{id}/images), then cached. Variants with no reference are skipped
(config.REQUIRE_REFERENCE) rather than generating a lookalike plant.

Examples:
    python variant_images.py --dry-run          # counts + how many days it will take
    python variant_images.py                    # generate (resumable)
    python variant_images.py --provider openai --limit 20
"""
import argparse
import base64
import csv
import io
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from PIL import Image

import config

log = logging.getLogger("variant_images")

# (file key, camera description). The first IMAGES_PER_VARIANT entries are used.
ANGLES = [
    ("front", "front view, plant and pot centered, eye level"),
    ("side", "three-quarter side view from the left, showing the shape of the plant and the pot"),
    ("top", "top-down overhead view showing how the leaves are arranged"),
    ("closeup", "close-up detail shot of the leaves and their texture, shallow depth of field"),
    ("lifestyle", "placed in a bright modern living room next to a window, natural light, lifestyle photography"),
]
STUDIO_BG = "plain white studio background, soft diffused studio lighting"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


class QuotaExhausted(Exception):
    """Daily free quota / billing limit hit - stop the run, resume later."""


class TransientError(Exception):
    """Rate limit / 5xx / network blip - worth retrying."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class FatalProviderError(Exception):
    """Bad API key / no access / billing not set up - every request would fail, so stop the run."""


class PermanentError(Exception):
    """This prompt was rejected (e.g. safety filter) - skip the image."""


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------
@dataclass
class Variant:
    product_id: str
    variant_id: str
    product_name: str
    variant_label: str = ""
    slug: str = ""


def load_variants(path: Optional[str] = None) -> List[Variant]:
    path = path or config.VARIANTS_CSV
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not (row.get("product_id") and row.get("variant_id")):
                continue
            out.append(Variant(
                product_id=row["product_id"].strip(),
                variant_id=row["variant_id"].strip(),
                product_name=(row.get("product_name") or "").strip(),
                variant_label=(row.get("variant_label") or "").strip(),
                slug=(row.get("slug") or "").strip(),
            ))
    return out


def variant_dir(v: Variant) -> str:
    name = f"{v.product_id}_{v.variant_id}" + (f"_{v.slug}" if v.slug else "")
    return os.path.join(config.VARIANT_IMAGES_DIR, name)


def build_prompt(v: Variant, key: str, angle: str, has_reference: bool = False) -> str:
    background = "" if key == "lifestyle" else f" {STUDIO_BG}."
    if has_reference:
        variant = f" Show it as this product variant: {v.variant_label}." if v.variant_label else ""
        return (
            f"Professional e-commerce product photo of the exact same plant shown in the reference photo (image 0)"
            f"{' (' + v.product_name + ')' if v.product_name else ''}. Keep the leaf shape, leaf colour, "
            f"markings and growth habit identical to the reference photo.{variant} {angle}.{background} "
            "Sharp focus, realistic natural colors, high detail, no text, no watermark, no people."
        )
    subject = v.product_name + (f" ({v.variant_label})" if v.variant_label else "")
    return (
        f"Professional e-commerce product photo of a {subject}, in its pot. {angle}.{background} "
        "Sharp focus, realistic natural colors, high detail, no text, no watermark, no people."
    )


def find_reference(v: Variant) -> Optional[str]:
    """The product's real photo from the earlier image steps (used by OpenAI edit mode)."""
    if not v.slug or not os.path.isdir(config.IMAGES_DIR):
        return None
    folder = os.path.join(config.IMAGES_DIR, f"{v.product_id}_{v.slug}")
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(IMAGE_EXTS):
                return os.path.join(folder, f)
    for f in os.listdir(config.IMAGES_DIR):
        if os.path.splitext(f)[0] == v.slug and f.lower().endswith(IMAGE_EXTS):
            return os.path.join(config.IMAGES_DIR, f)
    return None


_REF_CACHE: Dict[str, Optional[str]] = {}


def flatten_to_white(img: Image.Image) -> Image.Image:
    """Convert to RGB, compositing transparency onto WHITE. (A plain .convert('RGB') turns transparent
    pixels black, which gives the model a black-background reference and hurts accuracy.)"""
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _cache_path(product_id: str) -> str:
    return os.path.join(config.REFERENCE_CACHE_DIR, f"{product_id}.jpg")


def _pick_image_url(items) -> Optional[str]:
    """Choose the product's main photo from GET /api/products/{id}/images.
    Field names are assumed (imageUrl / isPrimary / variantId) - adjust here if your DTO differs."""
    if not isinstance(items, list):
        return None

    def url_of(item):
        for k in ("imageUrl", "url", "image_url", "secureUrl"):
            if item.get(k):
                return item[k]
        return None

    general = [i for i in items if not (i.get("variantId") or i.get("variant_id"))] or items
    primary = [i for i in general if i.get("isPrimary") or i.get("primary")]
    for item in primary + general:
        if url_of(item):
            return url_of(item)
    return None


def fetch_reference_from_api(product_id: str) -> Optional[str]:
    """Download the product's stored photo via your API and cache it locally."""
    url = f"{config.CREATE_PRODUCT_ENDPOINT.rstrip('/')}/{product_id}/images"
    try:
        resp = requests.get(url, headers=config.AUTH_HEADERS, timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            log.warning("Could not list images for product %s: HTTP %s", product_id, resp.status_code)
            return None
        img_url = _pick_image_url(resp.json())
        if not img_url:
            log.warning("Product %s has no images in the API", product_id)
            return None
        raw = requests.get(img_url, timeout=60)
        raw.raise_for_status()
        img = flatten_to_white(Image.open(io.BytesIO(raw.content)))
        os.makedirs(config.REFERENCE_CACHE_DIR, exist_ok=True)
        path = _cache_path(product_id)
        img.save(path, "JPEG", quality=92)
        return path
    except (requests.RequestException, ValueError, OSError) as exc:
        log.warning("Could not fetch reference photo for product %s: %s", product_id, exc)
        return None


def get_reference(v: Variant) -> Optional[str]:
    """Local product_images/ file -> cache -> API. Result is memoised per product for the run."""
    if not config.USE_REFERENCE_IMAGE:
        return None
    if v.product_id in _REF_CACHE:
        return _REF_CACHE[v.product_id]
    path = find_reference(v)
    if not path and os.path.exists(_cache_path(v.product_id)):
        path = _cache_path(v.product_id)
    if not path:
        path = fetch_reference_from_api(v.product_id)
    _REF_CACHE[v.product_id] = path
    return path


def prepare_reference_bytes(path: str, max_side: Optional[int] = None) -> bytes:
    """Flatten transparency onto white and shrink (keeping aspect ratio) to max_side.
    Cloudflare needs < 512px (config.REFERENCE_MAX_SIDE); OpenAI uses config.OPENAI_REFERENCE_MAX_SIDE."""
    max_side = max_side or config.REFERENCE_MAX_SIDE
    img = flatten_to_white(Image.open(path))
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def decode_image_response(resp: requests.Response) -> bytes:
    """Cloudflare returns either JSON {"result": {"image": <base64>}} or raw image bytes."""
    if resp.headers.get("Content-Type", "").startswith("image/"):
        return resp.content
    try:
        return base64.b64decode(resp.json()["result"]["image"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError(f"Unexpected response shape: {resp.text[:200]}") from exc


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _retry_after(resp: requests.Response) -> Optional[float]:
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None


def _classify(resp: requests.Response, quota_words) -> None:
    """Raise the right exception for a non-OK response."""
    text = resp.text[:400]
    low = text.lower()
    if any(w in low for w in quota_words) and resp.status_code in (402, 403, 429):
        raise QuotaExhausted(text)
    if resp.status_code in (401, 403):
        raise FatalProviderError(f"HTTP {resp.status_code}: {text}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientError(f"HTTP {resp.status_code}: {text}", _retry_after(resp))
    raise PermanentError(f"HTTP {resp.status_code}: {text}")


class CloudflareProvider:
    name = "cloudflare"

    def __init__(self):
        if not (config.CF_ACCOUNT_ID and config.CF_API_TOKEN):
            raise RuntimeError("Set CF_ACCOUNT_ID and CF_API_TOKEN environment variables for the Cloudflare provider.")
        self.headers = {"Authorization": f"Bearer {config.CF_API_TOKEN}"}

    def _run_url(self, model: str) -> str:
        return f"{config.CF_BASE_URL}/accounts/{config.CF_ACCOUNT_ID}/ai/run/{model}"

    def generate(self, prompt: str, reference: Optional[str] = None) -> bytes:
        try:
            if reference:
                # FLUX.2 [klein]: multipart form-data, reference photo as input_image_0
                size = str(config.CF_IMAGE_SIZE)
                resp = requests.post(
                    self._run_url(config.CF_REFERENCE_MODEL),
                    headers=self.headers,
                    files={
                        "prompt": (None, prompt),
                        "width": (None, size),
                        "height": (None, size),
                        "input_image_0": ("reference.jpg", prepare_reference_bytes(reference), "image/jpeg"),
                    },
                    timeout=180,
                )
            else:
                # FLUX.1 [schnell]: text only
                resp = requests.post(
                    self._run_url(config.CF_MODEL), headers=self.headers,
                    json={"prompt": prompt[:2000], "steps": config.CF_STEPS}, timeout=120,
                )
        except requests.RequestException as exc:
            raise TransientError(str(exc))
        if resp.status_code != 200:
            _classify(resp, ("neuron", "quota", "daily", "allocation", "limit exceeded"))
        return decode_image_response(resp)


# Approximate USD per 1024x1024 output image, from OpenAI's token pricing as reported in Sept 2026.
# Reference-image input adds roughly another $0.01-0.02. Always confirm on OpenAI's pricing page.
APPROX_OUTPUT_COST = {
    "gpt-image-2": {"low": 0.006, "medium": 0.053, "high": 0.211},
    # 2.5 models: OpenAI says token RATES match gpt-image-2 but does not publish tokens-per-image,
    # so these reuse the gpt-image-2 numbers as an assumption. Check usage.output_tokens on a real run.
    "gpt-image-2.5-flare": {"low": 0.006, "medium": 0.053, "high": 0.211},
    "gpt-image-2.5-sunburst": {"low": 0.006, "medium": 0.053, "high": 0.211},
    "gpt-image-1.5": {"low": 0.009, "medium": 0.034, "high": 0.133},
    "gpt-image-1": {"low": 0.011, "medium": 0.042, "high": 0.167},
    "gpt-image-1-mini": {"low": 0.005, "medium": 0.011, "high": 0.036},
}
APPROX_REFERENCE_INPUT_COST = 0.015


def estimate_cost_per_image(model: str, quality: str, with_reference: bool = True) -> Optional[float]:
    table = APPROX_OUTPUT_COST.get(model)
    if not table or quality not in table:
        return None
    return table[quality] + (APPROX_REFERENCE_INPUT_COST if with_reference else 0.0)


class OpenAIProvider:
    """gpt-image models. With a reference photo it uses POST /images/edits (image[] upload); otherwise
    POST /images/generations. gpt-image-2 always uses high-fidelity references and REJECTS input_fidelity;
    gpt-image-1 / 1.5 accept input_fidelity=high, which we send only for those models (config.OPENAI_INPUT_FIDELITY)."""
    name = "openai"

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise RuntimeError("Set the OPENAI_API_KEY environment variable for the OpenAI provider.")
        self.headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        self.last_usage = None
        fid = (config.OPENAI_INPUT_FIDELITY or "").lower()
        if fid == "auto":
            self.use_fidelity = config.OPENAI_IMAGE_MODEL in ("gpt-image-1", "gpt-image-1.5")
        else:
            self.use_fidelity = fid in ("high", "low")
        self.fidelity_value = "high" if fid in ("auto", "high") else fid

    def _post(self, base, prompt, reference):
        data = {
            "model": config.OPENAI_IMAGE_MODEL,
            "prompt": prompt,
            "size": config.OPENAI_IMAGE_SIZE,
            "quality": config.OPENAI_IMAGE_QUALITY,
            "n": "1",
        }
        if reference:
            if self.use_fidelity:
                data["input_fidelity"] = self.fidelity_value
            ref_bytes = prepare_reference_bytes(reference, config.OPENAI_REFERENCE_MAX_SIDE)
            return requests.post(f"{base}/images/edits", headers=self.headers, data=data,
                                 files={"image[]": ("reference.jpg", ref_bytes, "image/jpeg")}, timeout=300)
        return requests.post(f"{base}/images/generations", headers=self.headers, json=data, timeout=300)

    def generate(self, prompt: str, reference: Optional[str] = None) -> bytes:
        use_ref = reference if config.USE_REFERENCE_IMAGE else None
        try:
            resp = self._post(config.OPENAI_BASE_URL, prompt, use_ref)
            if resp.status_code == 400 and "input_fidelity" in resp.text and self.use_fidelity:
                log.info("Model rejected input_fidelity - retrying without it")
                self.use_fidelity = False
                resp = self._post(config.OPENAI_BASE_URL, prompt, use_ref)
        except requests.RequestException as exc:
            raise TransientError(str(exc))
        if resp.status_code != 200:
            _classify(resp, ("insufficient_quota", "billing_hard_limit", "exceeded your current quota"))
        try:
            body = resp.json()
            self.last_usage = body.get("usage")
            return base64.b64decode(body["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PermanentError(f"Unexpected response shape: {resp.text[:200]}") from exc


def get_provider(name: Optional[str] = None):
    name = (name or config.IMAGE_PROVIDER).lower()
    if name == "cloudflare":
        return CloudflareProvider()
    if name == "openai":
        return OpenAIProvider()
    raise ValueError(f"Unknown image provider {name!r} (use 'cloudflare' or 'openai')")


def generate_with_retry(provider, prompt: str, reference: Optional[str]) -> bytes:
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            return provider.generate(prompt, reference)
        except TransientError as exc:
            if attempt == attempts:
                raise PermanentError(f"gave up after {attempts} attempts: {exc}")
            wait = exc.retry_after if exc.retry_after is not None else min(60, 2 ** attempt * 3)
            log.warning("Transient error (%s) - retrying in %.0fs", str(exc)[:120], wait)
            time.sleep(wait)


def save_as_jpeg(raw: bytes, path: str) -> None:
    img = flatten_to_white(Image.open(io.BytesIO(raw)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "JPEG", quality=config.JPEG_QUALITY, optimize=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def plan_tasks(variants: List[Variant]) -> Dict:
    """Work out which images still need generating (existing/uploaded ones are skipped)."""
    angles = ANGLES[: max(1, min(config.IMAGES_PER_VARIANT, len(ANGLES)))]
    tasks, done = [], 0
    for v in variants:
        folder = variant_dir(v)
        for i, (key, desc) in enumerate(angles, 1):
            fname = f"{i:02d}_{key}.jpg"
            if os.path.exists(os.path.join(folder, fname)) or os.path.exists(os.path.join(folder, "uploaded", fname)):
                done += 1
                continue
            tasks.append((v, os.path.join(folder, fname), key, desc))
    return {"tasks": tasks, "done": done, "total": len(variants) * len(angles)}


def generate_all(variants_csv: Optional[str] = None, provider_name: Optional[str] = None,
                 limit: Optional[int] = None, dry_run: bool = False, progress_cb=None) -> Dict:
    variants = load_variants(variants_csv)
    plan = plan_tasks(variants)
    tasks = plan["tasks"]
    pname = (provider_name or config.IMAGE_PROVIDER).lower()
    if limit is None:
        limit = config.MAX_IMAGES_PER_RUN if pname == "cloudflare" else 0
    batch = tasks[:limit] if limit else tasks

    summary = {"variants": len(variants), "total": plan["total"], "already_done": plan["done"],
               "pending": len(tasks), "this_run": len(batch), "generated": 0, "failed": 0,
               "no_reference": 0, "quota_hit": False,
               "runs_needed": math.ceil(len(tasks) / limit) if limit else 1}
    log.info("%d variants -> %d images total | %d done | %d pending | this run: %d | runs needed at this limit: %d",
             summary["variants"], summary["total"], summary["already_done"], summary["pending"],
             summary["this_run"], summary["runs_needed"])
    if dry_run:
        if pname == "openai":
            per = estimate_cost_per_image(config.OPENAI_IMAGE_MODEL, config.OPENAI_IMAGE_QUALITY, config.USE_REFERENCE_IMAGE)
            if per:
                log.info("Rough OpenAI cost (%s, %s): ~$%.3f/image -> ~$%.0f for the %d pending images "
                         "(approximate - check OpenAI's pricing page)", config.OPENAI_IMAGE_MODEL,
                         config.OPENAI_IMAGE_QUALITY, per, per * len(tasks), len(tasks))
        for v, path, key, desc in batch[:3]:
            has_ref = bool(find_reference(v)) or os.path.exists(_cache_path(v.product_id))
            log.info("sample prompt (reference=%s) for %s: %s", has_ref, os.path.basename(path),
                     build_prompt(v, key, desc, has_ref))
        return summary

    provider = get_provider(pname)
    skipped_products = set()
    for i, (v, path, key, desc) in enumerate(batch, 1):
        ref = get_reference(v)
        if config.USE_REFERENCE_IMAGE and not ref and config.REQUIRE_REFERENCE:
            if v.product_id not in skipped_products:
                skipped_products.add(v.product_id)
                log.warning("No reference photo for product %s (%s) - skipping its variants. Upload a product "
                            "image first, or set REQUIRE_REFERENCE=False for text-only generation.",
                            v.product_id, v.product_name)
            summary["no_reference"] += 1
            continue
        prompt = build_prompt(v, key, desc, has_reference=bool(ref))
        try:
            raw = generate_with_retry(provider, prompt, ref)
            save_as_jpeg(raw, path)
            summary["generated"] += 1
            log.info("[%d/%d] saved %s%s", i, len(batch), path, " (from reference)" if ref else "")
        except QuotaExhausted as exc:
            summary["quota_hit"] = True
            log.warning("Quota/billing limit reached (%s). Progress is saved - run again later "
                        "(Cloudflare resets 00:00 UTC; for OpenAI add credits).", str(exc)[:150])
            break
        except FatalProviderError as exc:
            summary["fatal"] = str(exc)
            log.error("Stopping: the provider rejected our credentials/access (%s). Check your API key, "
                      "billing and (for OpenAI) organization verification.", str(exc)[:200])
            break
        except PermanentError as exc:
            summary["failed"] += 1
            log.error("Skipping %s: %s", os.path.basename(path), str(exc)[:200])
        if progress_cb:
            progress_cb(i, len(batch), {"path": path})
        time.sleep(config.GENERATE_DELAY_SECONDS)

    log.info("Generation finished: %d generated, %d failed, %d skipped (no reference), quota_hit=%s",
             summary["generated"], summary["failed"], summary["no_reference"], summary["quota_hit"])
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Generate multi-angle variant images")
    ap.add_argument("--variants", default=None, help="path to variants.csv")
    ap.add_argument("--provider", choices=["cloudflare", "openai"], default=None)
    ap.add_argument("--limit", type=int, default=None, help="max images this run (0 = unlimited)")
    ap.add_argument("--dry-run", action="store_true", help="only print counts and sample prompts")
    ap.add_argument("--model", default=None, help=f"OpenAI model (default {config.OPENAI_IMAGE_MODEL})")
    ap.add_argument("--quality", choices=["low", "medium", "high"], default=None,
                    help=f"OpenAI quality (default {config.OPENAI_IMAGE_QUALITY})")
    args = ap.parse_args()
    if args.model:
        config.OPENAI_IMAGE_MODEL = args.model
    if args.quality:
        config.OPENAI_IMAGE_QUALITY = args.quality
    generate_all(args.variants, args.provider, args.limit, args.dry_run)
