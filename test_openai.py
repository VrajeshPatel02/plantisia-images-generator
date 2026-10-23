"""
Try OpenAI's image model on ONE sample variant, guided by your real product photo.

    python test_openai.py --product-id 12 --name "Turtle Vine" --label "small, hanging pot"
        -> downloads product 12's photo from your API (GET /api/products/12/images, uses your token)
    python test_openai.py --reference photo.jpg --name "Turtle Vine" --label "small, hanging pot"
    python test_openai.py --product-id 12 --slug turtle-vine --quality low --count 2
    python test_openai.py --product-id 12 --model gpt-image-1.5        # try another model
    python test_openai.py --no-reference --name "Turtle Vine"          # text-only, to compare

Generates up to 5 angles into ./sample_output_openai/ and writes preview.html (reference photo first,
then each result with its prompt). This SPENDS MONEY on your OpenAI account - roughly 2-8 cents per image
depending on model/quality; the script prints an estimate first and the real token usage after each image.
Nothing is uploaded to your backend.
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
OPENAI_API_KEY not found. One-time setup:

  1. Log in at https://platform.openai.com  (the API is billed separately from a ChatGPT subscription)
  2. Add a payment method / prepaid credits:  Settings -> Billing  (a few dollars is plenty for testing)
  3. Create a key:  https://platform.openai.com/api-keys  -> "Create new secret key" -> copy it
  4. Image models may require "Organization verification" (Settings -> Organization -> General -> Verify).
     If you get a 403 mentioning verification, complete it and wait a few minutes.
  5. Set the key as an environment variable, then re-run:

     Windows (PowerShell):   $env:OPENAI_API_KEY="sk-..."
     macOS / Linux:          export OPENAI_API_KEY="sk-..."

  Never paste the key into code or commit it to git.
"""

NO_REFERENCE_HELP = """
No reference photo given. Use one of:
  --product-id N      fetch product N's photo from your API (backend must be running; uses your token)
  --reference X       a local image path or an image URL
  --slug S            with --product-id, use the earlier-downloaded file in product_images/
  --no-reference      text-only generation (produces a lookalike, not your actual plant)
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a sample variant with OpenAI, guided by a real product photo")
    ap.add_argument("--name", default="Snake Plant - Futura Superba")
    ap.add_argument("--label", default="small, white ceramic pot")
    ap.add_argument("--count", type=int, default=5, help="angles to generate (1-5)")
    ap.add_argument("--out", default="sample_output_openai")
    ap.add_argument("--model", default=None, help=f"default: {config.OPENAI_IMAGE_MODEL}")
    ap.add_argument("--quality", choices=["low", "medium", "high"], default=None,
                    help=f"default: {config.OPENAI_IMAGE_QUALITY}")
    ap.add_argument("--product-id", default=None, help="fetch this product's photo from your API")
    ap.add_argument("--slug", default=None, help="product slug (finds the file in product_images/ first)")
    ap.add_argument("--reference", default=None, help="local image path or image URL to use as the reference")
    ap.add_argument("--no-reference", action="store_true", help="text-only generation")
    ap.add_argument("--api-url", default=None, help="override products endpoint (default: config.CREATE_PRODUCT_ENDPOINT)")
    ap.add_argument("--no-open", action="store_true", help="don't open the preview in a browser")
    args = ap.parse_args(argv)

    if not config.OPENAI_API_KEY:
        print(SETUP_HELP)
        return 1
    if args.model:
        config.OPENAI_IMAGE_MODEL = args.model
    if args.quality:
        config.OPENAI_IMAGE_QUALITY = args.quality

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

    angles = vi.ANGLES[: max(1, min(args.count, len(vi.ANGLES)))]
    per = vi.estimate_cost_per_image(config.OPENAI_IMAGE_MODEL, config.OPENAI_IMAGE_QUALITY, bool(ref_path))
    est = f"~${per:.3f}/image, ~${per * len(angles):.2f} for this test (rough estimate)" if per else "cost estimate unavailable for this model"
    model_note = (f"Model: {config.OPENAI_IMAGE_MODEL} | quality: {config.OPENAI_IMAGE_QUALITY} | "
                  f"size: {config.OPENAI_IMAGE_SIZE} | reference: {'yes' if ref_path else 'no (text only)'} | {est}")
    print(model_note)

    provider = vi.OpenAIProvider()
    variant = vi.Variant(args.product_id or "0", "0", args.name, args.label, args.slug or "")

    items = []
    for i, (key, desc) in enumerate(angles, 1):
        prompt = vi.build_prompt(variant, key, desc, has_reference=bool(ref_path))
        fname = f"{i:02d}_{key}.jpg"
        print(f"[{i}/{len(angles)}] {key}: generating...", flush=True)
        start = time.time()
        try:
            raw = vi.generate_with_retry(provider, prompt, ref_path)
        except vi.QuotaExhausted as exc:
            print(f"Out of credits / billing limit reached: {str(exc)[:200]}\n  -> Add credits in Settings -> Billing.")
            break
        except vi.FatalProviderError as exc:
            print(f"  rejected: {str(exc)[:400]}")
            print("  -> Check OPENAI_API_KEY, billing, and organization verification (see setup steps above).")
            break
        except vi.PermanentError as exc:
            print(f"  failed: {str(exc)[:400]}")
            continue
        vi.save_as_jpeg(raw, os.path.join(args.out, fname))
        items.append((fname, key, prompt, time.time() - start))
        usage = f" | usage: {provider.last_usage}" if provider.last_usage else ""
        print(f"  saved {os.path.join(args.out, fname)} ({time.time() - start:.1f}s){usage}")

    if not items:
        print("No images were generated.")
        return 2
    preview = write_preview(args.out, args.name, args.label, reference_file, model_note, items)
    print(f"\nDone: {len(items)} image(s). Open {preview}")
    print("Check Usage at https://platform.openai.com/usage for the real cost of this test.")
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(preview))
    return 0


if __name__ == "__main__":
    sys.exit(main())
