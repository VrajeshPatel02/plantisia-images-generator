"""
Try the Cloudflare image generator on ONE sample variant, using your real product photo as the reference.

Pick where the reference photo comes from (any one of these):

    python test_cloudflare.py --product-id 12 --name "Money Plant" --label "small, hanging pot"
        -> downloads product 12's photo from your API (GET /api/products/12/images, uses your token)
    python test_cloudflare.py --reference path/or/url/to/photo.jpg --name "Money Plant"
    python test_cloudflare.py --product-id 12 --slug money-plant --name "Money Plant"
        -> looks in product_images/ (downloaded by the earlier steps) first, then your API
    python test_cloudflare.py --no-reference --name "Money Plant"
        -> text-only (FLUX.1 schnell), the old behaviour, for comparison

Generates up to 5 angles into ./sample_output/ and writes sample_output/preview.html: a contact sheet
with the reference photo first, then each generated image and its prompt. Uses a few free-quota images.
Nothing is uploaded anywhere.
"""
import argparse
import os
import sys
import time
import webbrowser

import config
import variant_images as vi
from sample_utils import resolve_reference, save_reference_copy, write_preview

SETUP_HELP = """
Cloudflare credentials not found. One-time setup (free, no card):

  1. Sign up / log in at https://dash.cloudflare.com
  2. Open the "Workers AI" page, click "Use REST API"
  3. Click "Create a Workers AI API Token" -> "Create API Token" -> copy the token
  4. On the same page copy your "Account ID"
  5. Set both as environment variables, then re-run this script:

     Windows (PowerShell):   $env:CF_ACCOUNT_ID="your-account-id"
                             $env:CF_API_TOKEN="your-token"
     macOS / Linux:          export CF_ACCOUNT_ID="your-account-id"
                             export CF_API_TOKEN="your-token"

  (A custom token needs both "Workers AI - Read" and "Workers AI - Edit" permissions.)
"""

NO_REFERENCE_HELP = """
No reference photo given. Use one of:
  --product-id N      fetch product N's photo from your API (backend must be running; uses your token)
  --reference X       a local image path or an image URL
  --slug S            with --product-id, use the earlier-downloaded file in product_images/
  --no-reference      text-only generation (produces a lookalike, not your actual plant)
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a sample variant with Cloudflare, guided by a real product photo")
    ap.add_argument("--name", default="Snake Plant - Futura Superba")
    ap.add_argument("--label", default="small, white ceramic pot")
    ap.add_argument("--count", type=int, default=5, help="angles to generate (1-5)")
    ap.add_argument("--out", default="sample_output")
    ap.add_argument("--product-id", default=None, help="fetch this product's photo from your API")
    ap.add_argument("--slug", default=None, help="product slug (finds the file in product_images/ first)")
    ap.add_argument("--reference", default=None, help="local image path or image URL to use as the reference")
    ap.add_argument("--no-reference", action="store_true", help="text-only generation (no real-photo guidance)")
    ap.add_argument("--api-url", default=None, help="override products endpoint (default: config.CREATE_PRODUCT_ENDPOINT)")
    ap.add_argument("--no-open", action="store_true", help="don't open the preview in a browser")
    args = ap.parse_args(argv)

    if not (config.CF_ACCOUNT_ID and config.CF_API_TOKEN):
        print(SETUP_HELP)
        return 1

    os.makedirs(args.out, exist_ok=True)
    ref_path = resolve_reference(args, args.out)
    if not ref_path and not args.no_reference:
        print(NO_REFERENCE_HELP if not (args.product_id or args.reference)
              else "\nCould not get a reference photo (see the warning above). "
                   "Check the product id, that the backend is running, and your token.")
        return 3

    reference_file = None
    if ref_path:
        reference_file = save_reference_copy(ref_path, args.out)
        print(f"Reference photo: {ref_path}")
    model = config.CF_REFERENCE_MODEL if ref_path else config.CF_MODEL
    model_note = f"Model: {model} | reference: {'yes' if ref_path else 'no (text only)'}"
    print(model_note)

    provider = vi.CloudflareProvider()
    variant = vi.Variant(args.product_id or "0", "0", args.name, args.label, args.slug or "")
    angles = vi.ANGLES[: max(1, min(args.count, len(vi.ANGLES)))]

    items = []
    for i, (key, desc) in enumerate(angles, 1):
        prompt = vi.build_prompt(variant, key, desc, has_reference=bool(ref_path))
        fname = f"{i:02d}_{key}.jpg"
        print(f"[{i}/{len(angles)}] {key}: generating...", flush=True)
        start = time.time()
        try:
            raw = vi.generate_with_retry(provider, prompt, ref_path)
        except vi.QuotaExhausted as exc:
            print(f"Free daily quota reached ({str(exc)[:120]}). Try again after 00:00 UTC.")
            break
        except vi.FatalProviderError as exc:
            print(f"  rejected: {str(exc)[:400]}")
            print("  -> Check CF_ACCOUNT_ID / CF_API_TOKEN and that the token has Workers AI Read + Edit.")
            break
        except vi.PermanentError as exc:
            print(f"  failed: {str(exc)[:400]}")
            continue
        vi.save_as_jpeg(raw, os.path.join(args.out, fname))
        items.append((fname, key, prompt, time.time() - start))
        print(f"  saved {os.path.join(args.out, fname)} ({time.time() - start:.1f}s)")

    if not items:
        print("No images were generated.")
        return 2
    preview = write_preview(args.out, args.name, args.label, reference_file, model_note, items)
    print(f"\nDone: {len(items)} image(s). Open {preview}")
    print("Tip: note how much of your free quota this used in the Cloudflare dashboard (Workers AI -> Usage) "
          "to work out how many images per day you can generate.")
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(preview))
    return 0


if __name__ == "__main__":
    sys.exit(main())
