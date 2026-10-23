"""
Scraper for https://www.ugaoo.com/collections/plants (8 pages).

Strategy
--------
1. Shopify stores expose a JSON feed for any collection:
       https://www.ugaoo.com/collections/plants/products.json?limit=250&page=N
   This is far more reliable than parsing HTML for name / price / full
   description, so we try it first for every page.

2. If that endpoint is ever blocked/disabled, we fall back to parsing the
   rendered collection HTML with a generic heuristic (find every
   "/products/..." link, walk up to its card container, pull price text
   with a regex). This is intentionally defensive rather than tied to one
   theme's exact CSS classes, since theme markup can change.

3. Some fields (short tagline, Water schedule, Light requirement) live only
   on the product detail page (in the small "care list" callouts), so each
   product is visited once more to fill those in.

Run directly for a CLI scrape:
    python scraper.py
"""
import csv
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

import config
import slugs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper")

PRICE_RE = re.compile(r"₹\s?([\d,]+(?:\.\d+)?)")


@dataclass
class Plant:
    name: str = ""
    short_description: str = ""
    description: str = ""
    water: str = ""
    light: str = ""
    base_price: str = ""
    discounted_price: str = ""
    product_url: str = ""
    image_url: str = ""
    slug: str = ""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(config.HEADERS)
    return s


def fetch(session: requests.Session, url: str, params: Optional[dict] = None, as_json: bool = False):
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                time.sleep(config.REQUEST_DELAY_SECONDS)
                return resp.json() if as_json else resp.text
            log.warning("GET %s -> HTTP %s (attempt %d/%d)", url, resp.status_code, attempt, config.MAX_RETRIES)
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("GET %s failed: %s (attempt %d/%d)", url, exc, attempt, config.MAX_RETRIES)
        time.sleep(config.REQUEST_DELAY_SECONDS * attempt)
    if last_exc:
        log.error("Giving up on %s: %s", url, last_exc)
    return None


def clean_text(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    soup = BeautifulSoup(html_or_text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Plan A: Shopify products.json feed
# ---------------------------------------------------------------------------
def fetch_collection_json(session: requests.Session, page: int) -> List[dict]:
    url = f"{config.COLLECTION_URL}/products.json"
    data = fetch(session, url, params={"limit": 250, "page": page}, as_json=True)
    if not data:
        return []
    return data.get("products", [])


def product_from_json(p: dict) -> Plant:
    plant = Plant()
    plant.name = (p.get("title") or "").strip()
    plant.product_url = f"{config.BASE_URL}/products/{p.get('handle')}"
    plant.description = clean_text(p.get("body_html", ""))
    variants = p.get("variants") or [{}]
    v = variants[0]
    price = v.get("price")
    compare_at = v.get("compare_at_price")
    plant.discounted_price = str(price) if price else ""
    plant.base_price = str(compare_at) if compare_at else plant.discounted_price

    # First/featured image: Shopify exposes it as "image" and in "images"[0]
    images = p.get("images") or []
    featured = p.get("image") or (images[0] if images else {}) or {}
    plant.image_url = (featured.get("src") or "").strip()
    return plant


# ---------------------------------------------------------------------------
# Plan B: generic HTML fallback for the collection listing
# ---------------------------------------------------------------------------
def parse_collection_html(html: str) -> List[Plant]:
    soup = BeautifulSoup(html, "html.parser")
    plants: List[Plant] = []
    seen = set()

    anchors = [a for a in soup.find_all("a", href=True) if "/products/" in a["href"]]
    for a in anchors:
        href = a["href"].split("?")[0]
        if href in seen:
            continue

        # Walk up until we find a container with enough text to be a "card"
        card = a
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if len(card.get_text(strip=True)) > 20:
                break

        card_text = card.get_text(separator="\n", strip=True)
        lines = [l for l in card_text.split("\n") if l.strip()]
        prices = PRICE_RE.findall(card_text)
        if not prices:
            continue  # not a real product card

        seen.add(href)
        plant = Plant()
        plant.product_url = config.BASE_URL + href if href.startswith("/") else href
        plant.name = a.get_text(strip=True) or (a.get("title") or "")

        img = card.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if not src and img.get("srcset"):
                src = img["srcset"].split(",")[0].strip().split(" ")[0]
            if src.startswith("//"):
                src = "https:" + src
            plant.image_url = src

        plant.short_description = ""
        for i, line in enumerate(lines):
            if plant.name and plant.name in line and i + 1 < len(lines):
                nxt = lines[i + 1]
                if "₹" not in nxt:
                    plant.short_description = nxt
                break

        if len(prices) >= 2:
            plant.discounted_price = prices[0].replace(",", "")
            plant.base_price = prices[1].replace(",", "")
        else:
            plant.discounted_price = prices[0].replace(",", "")
            plant.base_price = plant.discounted_price

        plants.append(plant)
    return plants


def collect_product_list(session: requests.Session) -> List[Plant]:
    all_plants: Dict[str, Plant] = {}
    for page in range(1, config.TOTAL_PAGES + 1):
        log.info("Fetching collection page %d/%d ...", page, config.TOTAL_PAGES)
        products = fetch_collection_json(session, page)
        if products:
            for p in products:
                plant = product_from_json(p)
                if plant.name:
                    all_plants[plant.product_url] = plant
            continue

        log.info("JSON feed empty for page %d, falling back to HTML parsing", page)
        html = fetch(session, config.COLLECTION_URL, params={"page": page})
        if not html:
            continue
        for plant in parse_collection_html(html):
            all_plants.setdefault(plant.product_url, plant)

    log.info("Found %d unique products across %d pages", len(all_plants), config.TOTAL_PAGES)
    return list(all_plants.values())


# ---------------------------------------------------------------------------
# Product detail page: short description, water, light
# ---------------------------------------------------------------------------
CARE_CLASS_HINT = "care-list"


def enrich_from_product_page(session: requests.Session, plant: Plant) -> Plant:
    html = fetch(session, plant.product_url)
    if not html:
        return plant
    soup = BeautifulSoup(html, "html.parser")

    if not plant.description:
        heading = soup.find(string=re.compile(r"about the product", re.I))
        if heading and heading.parent:
            container = heading.find_parent(["div", "section"]) or heading.parent
            plant.description = clean_text(str(container))

    if not plant.short_description:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            plant.short_description = meta["content"].strip()

    if not plant.image_url:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            plant.image_url = og["content"].strip()

    care_items = soup.find_all(class_=re.compile(CARE_CLASS_HINT, re.I))
    care_texts = [clean_text(c.get_text()) for c in care_items]
    if not care_texts:
        care_texts = [clean_text(t) for t in soup.stripped_strings]

    for t in care_texts:
        low = t.lower()
        if not plant.water and low.startswith("water"):
            plant.water = t
        if not plant.light and ("light" in low or "sun" in low) and "download" not in low:
            plant.light = t
        if plant.water and plant.light:
            break

    return plant


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def scrape_all(progress_cb=None) -> List[Plant]:
    session = make_session()
    plants = collect_product_list(session)
    enriched = []
    total = len(plants)
    for i, plant in enumerate(plants, 1):
        log.info("[%d/%d] Enriching: %s", i, total, plant.name)
        enriched.append(enrich_from_product_page(session, plant))
        if progress_cb:
            progress_cb(i, total, plant)
    return enriched


def save_csv(plants: List[Plant], path: Optional[str] = None) -> None:
    path = path or config.CSV_PATH
    # Generate every slug once, here, so later steps (product upload, image
    # download/upload) all read the same value from the CSV.
    for plant, slug in zip(plants, slugs.make_unique_slugs([p.name for p in plants])):
        plant.slug = slug
    fieldnames = [
        "name", "slug", "short_description", "description",
        "water", "light", "base_price", "discounted_price",
        "product_url", "image_url",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in plants:
            writer.writerow(asdict(p))
    log.info("Saved %d rows to %s", len(plants), path)


if __name__ == "__main__":
    results = scrape_all()
    save_csv(results)
