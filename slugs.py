"""
Shared slug helpers.

ProductRequestDto requires a slug matching ^[a-z0-9]+(-[a-z0-9]+)*$ (max 160
chars). The slug is generated ONCE by the scraper and written to the CSV;
every later step (product upload, image download, image upload) reads that
same value instead of re-deriving it, so they can never drift apart.
"""
import re
from typing import Dict, List, Optional

import config

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(name: str, max_len: Optional[int] = None) -> str:
    max_len = max_len or config.SLUG_MAX_LEN
    base = _SLUG_INVALID_CHARS.sub("-", (name or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base) or "product"
    return base[:max_len].rstrip("-") or "product"


def make_unique_slugs(names: List[str]) -> List[str]:
    """Slugify every name, appending -2, -3, ... on collisions so every slug
    in the batch is unique and within the DTO's length limit."""
    seen: Dict[str, int] = {}
    slugs = []
    for name in names:
        base = slugify(name)
        count = seen.get(base, 0)
        if count == 0:
            slug = base
        else:
            suffix = f"-{count + 1}"
            slug = base[: config.SLUG_MAX_LEN - len(suffix)] + suffix
        seen[base] = count + 1
        slugs.append(slug)
    return slugs


def slugs_for_rows(rows: List[Dict]) -> List[str]:
    """Use the CSV's own 'slug' column when present (the normal case); fall
    back to generating from names for older CSVs that predate the column."""
    if rows and all((r.get("slug") or "").strip() for r in rows):
        return [r["slug"].strip() for r in rows]
    return make_unique_slugs([r.get("name", "") for r in rows])
