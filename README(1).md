# Ugaoo Plants — Scraper & Uploader

Scrapes all plants from https://www.ugaoo.com/collections/plants (8 pages),
writes a CSV, and uploads each row to your Spring Boot backend
(`POST /api/products`).

## Setup

```bash
pip install -r requirements.txt
```

Python 3.9+ recommended. `tkinter` ships with standard Python on
Windows/macOS; on Linux install it separately if needed, e.g.
`sudo apt install python3-tk`.

## Run

GUI (recommended):
```bash
python gui.py
```
Click **1. Scrape Ugaoo Plants** first, then **2. Upload CSV to Database**.
Both run in the background so the window stays responsive, and progress /
errors stream into the log box.

CLI, if you'd rather script it:
```bash
python scraper.py     # writes ugaoo_plants.csv
python uploader.py    # uploads ugaoo_plants.csv to the API
```

## How the scraper works

1. **Collection pages (1–8):** Ugaoo runs on Shopify, so the scraper first
   tries Shopify's built-in JSON feed
   `https://www.ugaoo.com/collections/plants/products.json?limit=250&page=N`.
   This is far more reliable than HTML parsing for name, price, and the
   full description. If that feed is ever disabled, it automatically falls
   back to parsing the rendered HTML.
2. **Product detail pages:** Water schedule, Light requirement, and the
   short tagline aren't in the JSON feed, so each product page is visited
   once more and parsed for the small care-list callouts
   ("Water once a week", "Needs bright indirect sunlight", etc.).
3. Everything is written to `ugaoo_plants.csv` with columns:
   `name, short_description, description, water, light, base_price,
   discounted_price, product_url`.

**Important:** I could not run this scraper against the live site from
where I built it (the sandbox's network is locked to a small allowlist of
package registries and doesn't include ugaoo.com), so the HTML-fallback
selectors and the care-list parsing are based on the page content I could
retrieve via search/fetch tools, not a live test run. Run `python
scraper.py` yourself first and spot-check `ugaoo_plants.csv` — if
`short_description`, `water`, or `light` come back empty for many rows,
open one product page in your browser, view source, and tell me what the
care-list markup looks like (or share a snippet) and I'll tighten the
selectors.

## How the uploader works

Your `ProductController` only has a single-item `POST /api/products` (no
true bulk endpoint), so "bulk upload" means: loop over every CSV row and
POST it, using a small thread pool (`UPLOAD_WORKERS` in `config.py`) for
speed, with retries. Rows that fail after retries are written to
`failed_uploads.csv` so you can fix and retry just those.

### ⚠️ Field mapping — check this before uploading

I only saw your controller, not `ProductRequestDto`. Open
`ProductRequestDto.java` and compare its field names against
`config.FIELD_MAPPING`:

```python
FIELD_MAPPING = {
    "name": "name",
    "short_description": "shortDescription",
    "description": "description",
    "water": "water",
    "light": "light",
    "base_price": "basePrice",
    "discounted_price": "discountedPrice",
}
```

The left side is the CSV column (fixed). The right side is the JSON key
sent to your API — edit those to match your DTO exactly. Also set
`CATEGORY_ID` to your real "Plants" category id, and `API_BASE_URL` /
`CREATE_PRODUCT_ENDPOINT` if your backend isn't on `localhost:8080`.

## A note on scraping etiquette

The scraper adds a delay between requests (`REQUEST_DELAY_SECONDS` in
`config.py`, default 1s) and retries with backoff rather than hammering
the site. Please check ugaoo.com's Terms of Service / robots.txt before
running this at scale, and keep the delay reasonable.

## Product images (steps 3 & 4)

The scraper now also writes two extra CSV columns: `slug` (generated once,
so every later step uses the identical value) and `image_url` (the first
product image, taken from Shopify's JSON feed).

```bash
python image_downloader.py   # saves product_images/<slug>.<ext>
python image_uploader.py     # uploads them to your API
```
(or use buttons 3 and 4 in the GUI)

`image_uploader.py` first pages through `GET /api/products?categoryId=...`
to build a `slug -> productId` map straight from your database (saved as
`product_id_map.csv`), so it works even for products created in earlier
runs. Each image is then sent to `POST /api/products/{id}/images`
(multipart key `file`) and, on success, moved into
`product_images/<id>_<slug>/`. Re-running is safe: filed images and products
that already have images on the server are skipped, so nothing is uploaded
twice. Failures are written to `failed_image_uploads.csv`.

Run order: 1 scrape -> 2 upload products -> 3 download images -> 4 upload images.
Step 3 only needs the CSV, so it can run any time after step 1; step 4 needs
the products to exist in your DB (step 2).

**Re-scraping note:** slugs come from product names (`-2`, `-3` suffixes for
duplicate names, in scrape order). If you re-scrape and the order of
same-named products changes, those suffixes could shift, so keep the
original CSV once products are uploaded.

## Variant images (steps 5 & 6) - AI generated, multi-angle

1. Create `variants.csv` (see `variants_template.csv`) with one row per variant:
   `product_id, variant_id, product_name, variant_label[, slug]`.
   `variant_label` is free text that goes into the prompt, e.g. "Small, white ceramic pot".
   `slug` is optional (used for folder names and, with OpenAI, to find the product's real photo).
2. Pick a provider and set its key as an environment variable (never hardcode keys):

   | Provider | Cost | Env vars |
   |---|---|---|
   | `cloudflare` (FLUX.1 schnell) | free daily quota (10,000 neurons/day, no card) | `CF_ACCOUNT_ID`, `CF_API_TOKEN` |
   | `openai` (gpt-image) | paid per image | `OPENAI_API_KEY` (`OPENAI_IMAGE_MODEL` to override the model name) |

3. Preview, then generate:
```bash
python variant_images.py --dry-run     # counts, sample prompts, how many runs it will take
python variant_images.py               # generate; safe to stop/re-run, existing files are skipped
python variant_images.py --provider openai --limit 20
```
Images are saved as JPEG (keeps each file small - Spring's default upload limit is 1MB per file)
in `variant_images/<product_id>_<variant_id>_<slug>/01_front.jpg ... 05_lifestyle.jpg`
(front, side, top, close-up, lifestyle). Set `IMAGES_PER_VARIANT = 4` in `config.py` to drop the lifestyle shot.

4. Upload (one bulk request per variant to `POST /api/products/{pid}/images/variant/{vid}/bulk`):
```bash
python variant_image_uploader.py
```
Confirmed files move into an `uploaded/` subfolder; re-runs only send what's pending.
Failures are listed in `failed_variant_uploads.csv`.

**Free-tier maths:** 189 products x 2 variants x 5 images = 1,890 images (1,512 at 4 each).
Cloudflare's free quota is roughly 170-230 images/day, so expect ~9-13 daily runs.
The script caps itself at `MAX_IMAGES_PER_RUN = 150` per run and also stops cleanly if the quota runs out earlier.

**Caveat:** text-to-image models draw *a* plant matching the name/label, not your exact plant or pot.
The OpenAI provider can use your real product photo as a reference (better consistency); Cloudflare cannot.
Consider labeling these as illustrative, or using them as placeholders until real photos exist.

### Use your real product photo as the reference (recommended)

Text-only generation draws *a* plant, not *your* plant. So generation is guided by the product's
real photo already stored in your database. With a reference, Cloudflare uses **FLUX.2 [klein] 4B**
(multipart request, reference sent as `input_image_0`, shrunk to <512px as the API requires);
without one it falls back to FLUX.1 schnell (text only).

The reference is looked up in order: `product_images/` (from steps 3-4) -> `reference_cache/<product_id>.jpg`
-> your API `GET /api/products/{id}/images` (picks the primary non-variant image; assumes the fields
`imageUrl`, `isPrimary`, `variantId` - adjust `_pick_image_url` in `variant_images.py` if your DTO differs).
Variants whose product has no photo are **skipped** (`REQUIRE_REFERENCE = True`) instead of inventing a lookalike.

### Try Cloudflare on one sample first

```bash
python test_cloudflare.py --product-id 12 --name "Money Plant" --label "small, hanging pot"
python test_cloudflare.py --reference photo.jpg --name "Syngonium Pixie" --label "small, black plastic pot"
python test_cloudflare.py --no-reference --name "Money Plant"      # text-only, to compare
```
Generates ~5 images from the real photo, uploads nothing, and opens `sample_output/preview.html` with
the **reference photo first**, then each result and its prompt. Judge accuracy there before the full run.
Afterwards check Workers AI -> Usage in the Cloudflare dashboard: images/day = 10,000 / neurons per image,
then set `MAX_IMAGES_PER_RUN` (default 60) and `CF_IMAGE_SIZE` (default 768; smaller = cheaper) in `config.py`.

Cloudflare setup (free, no card): dash.cloudflare.com -> Workers AI -> "Use REST API" ->
"Create a Workers AI API Token" (copy it) and copy the Account ID from the same page, then set
`CF_ACCOUNT_ID` and `CF_API_TOKEN` environment variables (the script prints the exact commands
for your OS if they're missing).

## OpenAI image generation (better reference fidelity)

Cloudflare's klein model didn't reproduce the real plant well in testing, so OpenAI's image
editing is the recommended provider when accuracy matters. It costs money (billed per token).

**Setup**
1. Log in at platform.openai.com (API billing is separate from a ChatGPT subscription).
2. Settings -> Billing: add a payment method / prepaid credits.
3. Create a key at platform.openai.com/api-keys and set it as an environment variable:
   - Windows PowerShell: `$env:OPENAI_API_KEY="sk-..."`
   - macOS / Linux: `export OPENAI_API_KEY="sk-..."`
4. Image models may need *Organization verification* (Settings -> Organization -> General). A 403 that
   mentions verification means you must complete it first. Never commit your key.

**Try it on one product first**
```bash
python test_openai.py --product-id 12 --name "Turtle Vine" --label "small, hanging pot" --quality low --count 2
python test_openai.py --product-id 12 --name "Turtle Vine" --label "small, hanging pot"        # all 5 angles
python test_openai.py --product-id 12 --model gpt-image-1.5                                      # compare models
```
Results (reference photo first) go to `sample_output_openai/preview.html`. The script prints a cost estimate
up front and the real token usage after each image.

**Models** (set `OPENAI_IMAGE_MODEL` or `--model`): default `gpt-image-2` (always high-fidelity to the reference;
the code never sends `input_fidelity` to it, because it rejects that parameter). `gpt-image-1.5` and `gpt-image-1`
get `input_fidelity=high` automatically; `gpt-image-1-mini` is cheapest; `gpt-image-2.5-sunburst` is newer and
marketed for stronger editing. `gpt-image-1` is reportedly deprecated on 2026-10-23. Confirm names in OpenAI's docs.

**Rough cost** (approximate; per 1024x1024 image incl. reference input, from OpenAI's token pricing as
reported in Sept 2026 - verify on OpenAI's pricing page):

| Quality | gpt-image-2 | 1,890 images |
|---|---|---|
| low | ~$0.02 | ~$40 |
| medium | ~$0.07 | ~$130 |

`low` is now the default quality (`OPENAI_IMAGE_QUALITY` in `config.py`, or `--quality medium` on the CLI).

`python variant_images.py --provider openai --dry-run` prints this estimate for your real pending count.
Tip: run `--limit 20` first, check Usage in the OpenAI dashboard, then scale up. OpenAI's Batch API
(about 50% cheaper) isn't wired in yet.

**Full run:** `python variant_images.py --provider openai` (or GUI step 5 with Provider = openai). Variants
whose product has no photo are skipped; a bad key / missing billing stops the run immediately; rate limits
(429) are retried honoring `Retry-After`.

**Transparent product photos:** product images with transparent backgrounds used to be flattened onto *black*
before being sent as the reference. They're now flattened onto white. If you ran earlier tests, delete
`reference_cache/` (and old `sample_output*` folders) so references are re-fetched correctly.

### How long does the OpenAI run take?

The generator is sequential (one image at a time + a 1s pause), so total time ~ N x (seconds per image + 1).
A third-party measurement of gpt-image-2 at 1024x1024 reported roughly: low 17-20s, medium ~41s, high 117-132s
(text-to-image; reference edits may add some seconds). For 1,000 images that is about 5-6 hours at low
(allow up to ~11 h if edits run closer to 40s), ~12 h at medium and ~34 h at high.
Measure your own: `python test_openai.py --product-id 12 --quality low --count 2` prints seconds per image.

### Before you buy OpenAI API credits (checked against OpenAI's own docs, Oct 2026)

Confirmed in the official docs: the Image API **edit endpoint** accepts reference images (`image[]`) and returns
`b64_json`; `gpt-image-2`, `gpt-image-2.5-flare` and `gpt-image-2.5-sunburst` are all supported there; `quality` low/medium/high;
`input_fidelity` must be omitted for gpt-image-2 (the code does).

Prices (image tokens, per 1M): input $8, output $30 standard; $4 / $15 with the Batch API. At 1024x1024 low quality a
gpt-image-2 image is ~196 output tokens (~$0.006); the reference photo adds input tokens (see `usage.input_tokens`
printed by `test_openai.py` for your real number).

Checklist:
1. Pay $5+ at platform.openai.com -> Billing (API credits are separate from ChatGPT Go). $5 paid = Tier 1, which has a
   **$100/month usage limit** (fine for the low-quality run; a ~$130 medium run would not fit).
2. Complete Organization Verification if asked (Settings -> Organization -> General) - GPT Image models may require it.
3. Check Settings -> Limits for your images-per-minute limit.
4. Test cheaply on 2-3 products with each model, `--quality low`:
   `python test_openai.py --product-id 12 --model gpt-image-2.5-flare --quality low`
   (also `gpt-image-2.5-sunburst` and `gpt-image-2`). OpenAI suggests Flare when speed matters, Sunburst when
   quality/editing precision matters. Compare quality, seconds and the printed token usage.
5. Only then run the full job. Start with about $10 of credit; top up as needed.
Not yet verified: whether the Batch API (half price) accepts reference-photo edit requests - the script doesn't use it.
