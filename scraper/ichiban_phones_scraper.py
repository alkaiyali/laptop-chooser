#!/usr/bin/env python3
"""Ichiban Tekno (Philippines) phone scraper.

https://shop.ichibantekno.com is Shopify. Its public products API exposes
the whole Smart Phones collection as JSON — no HTML walking needed::

    /collections/smart-phones/products.json?limit=250&page=N

Each product carries PHP variant prices (strings), compare-at prices,
availability flags, SKUs, images and a body_html spec list that reads like
"<li>RAM: 3GB / 4GB</li> ... <li>Processor: Helio A22 ...</li>". The list
is reduced to a {label: value} spec table; everything downstream parses
that table like any other store.

One record is emitted per variant: variants are the purchasable configs
("Dawn Purple / 4GB+128GB" vs "Dawn Purple / 4GB+256GB" differ in price,
RAM and storage), and the variant's "8GB+256GB" option pins the exact
RAM/storage over the product-level range ("RAM: 3GB / 4GB").

Outputs (data/):
  ichiban_phones.jsonl / .csv            - all smartphone variants
  ichiban_phones_in_stock.jsonl / .csv   - available variants only

Prices stay in pesos (``price_php``) but are also converted to SAR at a
reference rate so Ichiban entries compare next to Jarir and eXtra — the
default 16.4 PHP per SAR matches the other PH stores and can be overridden
with ``--php-rate``.
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CACHE_DIR,
    DATA_DIR,
    Fetcher,
    _clean,
    write_jsonl,
    write_wide_csv,
)

log = logging.getLogger("ichiban")

BASE = "https://shop.ichibantekno.com"
COLLECTION = BASE + "/collections/smart-phones/products.json"
PER_PAGE = 250
PAGE_LIMIT = 10
DEFAULT_PHP_RATE = 16.4  # PHP per 1 SAR (same as the other PH stores)

# collection is smartphones-only, but guard against accessories that slipped in
NON_PHONE_HINTS = (
    "case", "cover", "charger", "cable", "protector", "tempered", "headset",
    "holder", "mount", "stand", "strap", "watch", "buds", "powerbank",
    "power bank", "speaker", "selfie stick", "lens", "gimbal", "microphone",
    "hub", "dock", "keyboard", "mouse", "stylus", "film", "pouch", "sleeve",
    "bag", "wallet", "ring", "adapter", "converter", "tablet", "ipad",
)

BRAND_TAGS = {
    "apple": "Apple", "samsung": "Samsung", "xiaomi": "Xiaomi",
    "honor": "Honor", "huawei": "Huawei", "oppo": "Oppo", "vivo": "Vivo",
    "realme": "Realme", "oneplus": "OnePlus", "nothing": "Nothing",
    "infinix": "Infinix", "tecno": "Tecno", "itel": "Itel",
    "motorola": "Motorola", "nokia": "Nokia", "sony": "Sony",
    "google": "Google",
}

CSV_BASE_COLS = [
    "sku", "name", "brand", "color", "price_php", "price_sar",
    "was_price_php", "was_price_sar", "currency", "in_stock", "is_phone",
    "page_valid", "delisted", "scraped_at", "url",
]


def body_specs(body_html: str) -> dict[str, str]:
    """Reduce the product body_html <li> list to {label: value}."""
    text = re.sub(r"<br\s*/?>", "\n", body_html or "", flags=re.I)
    text = re.sub(r"</(p|div|li|ul|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    specs: dict[str, str] = {}
    for line in (ln.strip() for ln in htmlmod.unescape(text).split("\n")):
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = re.sub(r"\s+", " ", key.strip())
        val = re.sub(r"\s+", " ", val.strip())
        if not key or not val or len(key) > 48:
            continue
        low = key.lower()
        if "rear" in low and "cam" in low:
            key = "Rear Camera"
        elif "front" in low and "cam" in low:
            key = "Front Camera"
        if key in specs:
            specs[key] = specs[key] + " / " + val
        else:
            specs[key] = val
    return specs


def brand_of(product: dict) -> str | None:
    for tag in product.get("tags") or []:
        low = tag.strip().lower()
        if low in BRAND_TAGS:
            return BRAND_TAGS[low]
        for key, brand in BRAND_TAGS.items():
            if key in low and key not in ("new",):
                # "xiaomi phones" -> Xiaomi
                return brand
    title = (product.get("title") or "").strip()
    first = title.split()[0].lower() if title else ""
    if first in BRAND_TAGS:
        return BRAND_TAGS[first]
    return title.split()[0] if title else None


def variant_config(option2: str | None) -> tuple[int | None, int | None]:
    """'8GB+256GB' -> (8, 256); None-safe."""
    if not option2:
        return None, None
    m = re.search(r"(\d{1,2})\s*GB\s*\+\s*(\d{2,4})\s*GB", option2, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def is_phone_product(product: dict) -> bool:
    if (product.get("product_type") or "").lower() not in ("mobile phones", "smartphones", ""):
        return False
    title = (product.get("title") or "").lower()
    # every listing in this collection names itself a Smartphone; a trailing
    # "Fast Charger" (charging speed) must not read as a charger accessory
    if "smartphone" in title:
        return True
    hay = title + " " + " ".join(product.get("tags") or []).lower()
    if any(re.search(r"\b" + re.escape(hint) + r"\b", hay) for hint in NON_PHONE_HINTS):
        return False
    return True


def to_records(product: dict, php_rate: float) -> list[dict]:
    title = _clean(product.get("title"))
    handle = product.get("handle")
    brand = brand_of(product)
    specs = body_specs(product.get("body_html") or "")
    if not any("network" in k.lower() or "cellular" in k.lower() for k in specs):
        if re.search(r"\b5G\b", title or ""):
            specs.setdefault("Network", "5G")
    images = [im.get("src") for im in (product.get("images") or []) if im.get("src")]
    variants = product.get("variants") or []
    is_phone = is_phone_product(product)
    records = []
    for var in variants:
        try:
            price = float(var.get("price")) if var.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        was = None
        try:
            cmp_ = float(var.get("compare_at_price")) if var.get("compare_at_price") not in (None, "") else None
        except (TypeError, ValueError):
            cmp_ = None
        if cmp_ and price and cmp_ > price:
            was = cmp_
        color = _clean(var.get("option1"))
        ram, storage = variant_config(var.get("option2"))
        vspecs = dict(specs)
        if ram is not None:
            vspecs["RAM"] = f"{ram}GB"
        if storage is not None:
            vspecs["Storage"] = f"{storage}GB"
        if color:
            vspecs.setdefault("Color", color)
        vtitle = _clean(var.get("title"))
        name = title if len(variants) == 1 else f"{title} – {vtitle}" if vtitle not in (None, "Default Title") else title
        sku = str(var.get("sku") or f"{product.get('id')}-{var.get('id')}" or "")
        feat = (var.get("featured_image") or {})
        vimg = feat.get("src") if isinstance(feat, dict) else None
        in_stock = bool(var.get("available"))
        records.append({
            "sku": sku,
            "name": name,
            "brand": brand,
            "color": color,
            "price_php": round(price, 2) if price is not None else None,
            "price_sar": round(price / php_rate, 2) if price else None,
            "was_price_php": round(was, 2) if was else None,
            "was_price_sar": round(was / php_rate, 2) if was else None,
            "currency": "PHP",
            "price_local": round(price, 2) if price is not None else None,
            "availability": "InStock" if in_stock else "OutOfStock",
            "in_stock": in_stock,
            "on_sale": bool(was),
            "images": ([vimg] + [i for i in images if i != vimg]) if vimg else images,
            "specs": vspecs,
            "is_phone": is_phone,
            "page_valid": True,
            "delisted": False,
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "url": f"{BASE}/products/{handle}?variant={var.get('id')}" if handle else None,
        })
    return records


def fetch_products(fetcher: Fetcher, limit: int = 0) -> list[dict]:
    """Paginate the collection products API; each product once."""
    seen: set = set()
    products: list[dict] = []
    for page in range(1, PAGE_LIMIT + 1):
        url = f"{COLLECTION}?limit={PER_PAGE}&page={page}"
        try:
            batch = json.loads(fetcher.get(url, allow_cache=limit == 0)).get("products", [])
        except Exception as err:  # noqa: BLE001
            log.warning("page %s failed: %s", page, err)
            break
        if not batch:
            break
        fresh = [p for p in batch if p.get("id") not in seen]
        seen.update(p.get("id") for p in fresh)
        products.extend(fresh)
        log.info("page %s: %s products (%s new, total %s)",
                 page, len(batch), len(fresh), len(products))
        if limit and len(products) >= limit:
            return products[:limit]
        if len(batch) < PER_PAGE:
            break
    return products


def write_csv(records: list[dict], path: Path) -> None:
    write_wide_csv(records, path, CSV_BASE_COLS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--php-rate", type=float, default=DEFAULT_PHP_RATE,
                        help=f"pesos per 1 SAR (default {DEFAULT_PHP_RATE})")
    parser.add_argument("--limit", type=int, default=0, help="only read the first N products (0 = all)")
    parser.add_argument("--interval", type=float, default=0.5, help="minimum seconds between requests")
    parser.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    parser.add_argument("--max-age", type=float, default=0, metavar="HOURS",
                        help="re-fetch cached responses older than N hours (0 = never expire)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    max_age = args.max_age * 3600 if args.max_age > 0 else None
    fetcher = Fetcher(CACHE_DIR, min_interval=args.interval, refresh=args.refresh,
                      max_age=max_age, headers={"Accept": "application/json"})

    products = fetch_products(fetcher, args.limit)
    log.info("read %s store products", len(products))
    records = [rec for p in products for rec in to_records(p, args.php_rate)]
    phones = [r for r in records if r.get("is_phone")]
    log.info("%s variants (%s rejected as accessories)",
             len(phones), len(records) - len(phones))

    records = sorted(phones, key=lambda r: (r.get("price_sar") or 0))
    write_jsonl(records, DATA_DIR / "ichiban_phones.jsonl")
    write_csv(records, DATA_DIR / "ichiban_phones.csv")

    in_stock = [r for r in records if r.get("in_stock")]
    write_jsonl(in_stock, DATA_DIR / "ichiban_phones_in_stock.jsonl")
    write_csv(in_stock, DATA_DIR / "ichiban_phones_in_stock.csv")

    spec_count = sum(len(r.get("specs") or {}) for r in records)
    log.info("done: %s variants (%s in stock), %s spec values · network %s / cache %s",
             len(records), len(in_stock), spec_count,
             fetcher.stats["network"], fetcher.stats["cache"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
