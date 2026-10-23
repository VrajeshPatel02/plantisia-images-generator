"""Helpers shared by test_cloudflare.py and test_openai.py."""
import html
import io
import os

import requests
from PIL import Image

import config
import variant_images as vi


def resolve_reference(args, out_dir: str):
    """Return a local path to the reference photo, or None."""
    if args.no_reference:
        return None
    if args.reference:
        if args.reference.lower().startswith(("http://", "https://")):
            resp = requests.get(args.reference, timeout=60)
            resp.raise_for_status()
            img = vi.flatten_to_white(Image.open(io.BytesIO(resp.content)))
            path = os.path.join(out_dir, "reference_source.jpg")
            img.save(path, "JPEG", quality=92)
            return path
        return args.reference if os.path.exists(args.reference) else None
    if args.product_id:
        if args.api_url:
            config.CREATE_PRODUCT_ENDPOINT = args.api_url
        v = vi.Variant(args.product_id, "0", args.name, args.label, args.slug or "")
        return vi.get_reference(v)
    return None


def save_reference_copy(ref_path: str, out_dir: str, name: str = "reference.jpg") -> str:
    """Save a (white-background) copy of the reference photo next to the results for the contact sheet."""
    vi.flatten_to_white(Image.open(ref_path)).save(os.path.join(out_dir, name), "JPEG", quality=90)
    return name


def write_preview(out_dir, name, label, reference_file, model_note, items):
    ref_card = ""
    if reference_file:
        ref_card = (f'<figure class="ref"><img src="{html.escape(reference_file)}" alt="reference">'
                    "<figcaption><b>Reference photo</b> (your product image)</figcaption></figure>")
    cards = "".join(
        f'<figure><img src="{html.escape(f)}" alt="{html.escape(key)}">'
        f"<figcaption><b>{i}. {html.escape(key)}</b> <small>({secs:.1f}s)</small>"
        f"<br><small>{html.escape(prompt)}</small></figcaption></figure>"
        for i, (f, key, prompt, secs) in enumerate(items, 1)
    )
    page = f"""<!doctype html><meta charset="utf-8"><title>Cloudflare sample - {html.escape(name)}</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:24px;background:#f6f7f8;color:#222}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
 figure{{margin:0;background:#fff;border-radius:8px;padding:10px;box-shadow:0 1px 4px #0002}}
 figure.ref{{outline:3px solid #2b7de9}}
 img{{width:100%;border-radius:6px;display:block}} figcaption{{margin-top:8px;font-size:14px}}
 small{{color:#666}}
</style>
<h2>{html.escape(name)} <small>({html.escape(label)})</small></h2>
<p><small>{html.escape(model_note)}</small></p>
<div class="grid">{ref_card}{cards}</div>"""
    path = os.path.join(out_dir, "preview.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


