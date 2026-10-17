#!/usr/bin/env python3
"""GreenTelcom (Philippines) phone scraper.

https://www.greentelcom.ph is WooCommerce. Its public Store API exposes the
whole Smartphones category as JSON — no HTML walking needed::

    /wp-json/wc/store/v1/products?category=42&per_page=100&page=N

Each product carries PHP prices (minor units), stock flags, images, attribute
terms (Colors, Internal Storage) and a long-form description that reads like
a spec sheet ("Chipset\\n\\nKirin 9030S (5 nm)", "RAM\\n\\n12 GB",
"Processor: MediaTek Helio G91-Ultra ..."). The description is reduced to a
{label: value} spec table (see description_specs); everything downstream
parses that table like any other store.

Pricing nuance: a variable product's Store API price is the cheapest variation
while its description may list a storage range ("256GB or 512GB") — the range
is collapsed to its first (cheapest) value so price and specs describe the
same configuration.

Outputs (data/):
  greentelcom_phones.jsonl / .csv            - all smartphone listings
  greentelcom_phones_in_stock.jsonl / .csv   - purchasable + in stock

Prices stay in pesos (``price_php``) but are also converted to SAR at a
reference rate so GreenTelcom entries compare next to Jarir and eXtra — the
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

log = logging.getLogger("greentelcom")

BASE = "https://www.greentelcom.ph"
API = BASE + "/wp-json/wc/store/v1/products"
SMARTPHONES_CATEGORY = 42
PER_PAGE = 100
PAGE_LIMIT = 12                      # 12 x 100 = 1200 products, plenty (cat: ~544)
DEFAULT_PHP_RATE = 16.4              # PHP per 1 SAR (same as the other PH stores)

# category is smartphones-only, but guard against accessories that slipped in
NON_PHONE_HINTS = (
    "case", "cover", "charger", "cable", "protector", "tempered", "headset",
    "holder", "mount", "stand", "strap", "watch", "buds", "powerbank",
    "power bank", "speaker", "selfie stick", "lens", "gimbal", "microphone",
    "hub", "dock", "keyboard", "mouse", "stylus", "film", "pouch", "sleeve",
    "bag", "wallet", "ring", "adapter", "converter",
)

# section headers that terminate a multi-line spec value (see description_specs)
HEADERS = {    "display", "chipset", "processor", "cpu", "gpu", "graphics", "ram",
    "memory", "memory & storage", "storage", "rear camera", "front camera",
    "triple cameras", "dual cameras", "cameras", "os", "software", "weight",
    "size", "colors", "color", "cellular", "network", "connectivity",
    "connectivity & network", "battery", "battery & charging", "sim card",
    "sim", "build materials", "durability", "dimensions & weight",
    "performance", "performance and storage", "design and display",
    "audio and sensors", "sound", "bluetooth", "wi-fi", "nfc", "location",
    "security & durability", "sensors", "dimensions", "expansion", "type",
    "variants", "protection & features", "brightness", "features", "video",
    "operating system", "refresh rate", "resolution",
}

# jammed Xiaomi-style lines run "Label: valueNext label: value" with no markup
# between them — split before every "Capitalized Words:" boundary (the space
# is included: some joints are glued like "1080Refresh", others spaced)
_JAM_SPLIT = re.compile(r"(?<=[a-z0-9)%.\"»\] ])(?=[A-Z][A-Za-z /&+.-]{1,28}:)")


def _label_ok(key: str) -> bool:
    """True for real spec labels; rejects value fragments with a colon."""
    key = key.strip()
    return (bool(key) and key[0].isalpha() and len(key) <= 30
            and "(" not in key and ")" not in key
            and "（" not in key and "）" not in key)

CSV_BASE_COLS = [
    "sku", "name", "brand", "color", "price_php", "price_sar",
    "was_price_php", "was_price_sar", "currency", "in_stock", "is_purchasable",
    "on_sale", "is_phone", "page_valid", "delisted", "scraped_at", "url",
]


def _text_lines(desc_html: str) -> list[str]:
    text = re.sub(r"<br\s*/?>", "\n", desc_html or "", flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return [ln.strip() for ln in htmlmod.unescape(text).split("\n") if ln.strip()]


def description_specs(desc_html: str, name: str | None = None) -> dict[str, str]:
    """Reduce the long-form description to {label: value}.

    Two shapes occur: same-line pairs ("Processor: MediaTek Helio G91-Ultra")
    and header-alone lines whose value follows ("Chipset" / "Kirin 9030S").
    A bare key ("Triple Cameras:") collects the following lines so multi-lens
    blocks stay together. Camera-ish keys merge into Rear/Front Camera.
    """
    lines = _text_lines(desc_html)
    # break jammed "valueNext Label:" runs into separate fragments first
    split: list[str] = []
    for ln in lines:
        split.extend(p for p in _JAM_SPLIT.split(ln) if p.strip())
    lines = split
    specs: dict[str, str] = {}

    def continuation(j: int) -> tuple[list[str], int]:
        """Following lines belonging to a header: plain lines and colon lines
        that fail the label guard (e.g. "4G LTE Dual SIM (Hybrid slot: ...)").
        Stops at real headers and valid "Label: value" lines."""
        vals: list[str] = []
        while j < len(lines) and len(vals) < 4:
            nxt = lines[j]
            if nxt.lower() in HEADERS:
                break
            if ":" in nxt:
                k2, _, v2 = nxt.partition(":")
                if v2.strip() and _label_ok(k2):
                    break
                vals.append(nxt)  # guard-failed colon line: continuation text
            else:
                vals.append(nxt)
            j += 1
        return vals, j

    def store(key: str, value: str) -> None:
        key = re.sub(r"\s+", " ", key.strip().strip("-–—•").strip())
        value = re.sub(r"\s+", " ", value.strip())
        if not key or not value or len(key) > 48:
            return
        low = key.lower()
        if "rear" in low and "cam" in low:
            key = "Rear Camera"
        elif "front" in low and "cam" in low:
            key = "Front Camera"
        if key in specs:
            specs[key] = specs[key] + " / " + value
        else:
            specs[key] = value

    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            if val.strip() and _label_ok(key):
                store(key, val)
            elif not val.strip() and _label_ok(key):
                # bare key ("Triple Cameras:"): collect the block below it
                vals, j = continuation(i + 1)
                if vals:
                    store(key, " / ".join(vals))
                i = j - 1
            # guard-failed colon lines are skipped here; a header collector
            # above picks them up as continuation text when they belong to one
        elif len(line) <= 40 and _label_ok(line):
            # header-alone line: value is the following line(s)
            vals, j = continuation(i + 1)
            if vals:
                store(line, " / ".join(vals))
                i = j - 1
        i += 1
    _collapse_variants(specs)
    # The product name IS the variant identity ("Redmi 15 – 8GB | 256GB") while
    # the description template is often shared with a sibling variant — trust
    # the name when it states an explicit RAM | storage pair
    m = re.search(r"(\d{1,2})\s*(GB|TB)\s*\|\s*(\d{2,4})\s*(GB|TB)", name or "")
    if m and int(m.group(1)) <= 16:
        ram = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
        stor = int(m.group(3)) * (1024 if m.group(4).upper() == "TB" else 1)
        specs["RAM"] = f"{ram}GB"
        specs["Storage"] = f"{stor}GB"
    return specs


def _collapse_variants(specs: dict[str, str]) -> None:
    """Collapse multi-variant storage/RAM to the base (cheapest) config.

    The Store API price is the cheapest variation, so "256GB or 512GB",
    "8GB+128GB / 8GB+256GB" and "128GB, 256GB, 512GB" all describe the base
    unit as their first alternative — keeping the max would attach a 512GB
    spec to a 128GB price. Variant-carrying keys share one rule, first
    applicable wins: "A or B" → A; "A+B" pair → B for storage (A is the RAM
    half); comma/slash lists → first segment carrying a GB figure.
    """
    for key in list(specs):
        if key.lower() not in ("storage", "capacity", "ram & storage",
                               "memory & storage", "memory", "variants",
                               "storage and ram", "combination",
                               "ram and rom capacities"):
            continue
        val = specs[key]
        if " or " in val:
            specs[key] = val.split(" or ")[0].strip()
            continue
        m = re.search(r"(\d{1,2})\s*(?:GB\s*)?\+\s*(\d{2,4})\s*(?:GB)?", val)
        if m:
            specs[key] = f"{m.group(2)}GB"
            # the pair's first half is the RAM figure — keep it before the
            # value is collapsed (a later RAM scan only sees "128GB")
            specs.setdefault("RAM", f"{m.group(1)}GB")
            specs.setdefault("Storage", f"{m.group(2)}GB")
            continue
        if "," in val or "/" in val:
            for seg in re.split(r"[,/]", val):
                if re.search(r"\d\s*(GB|TB)", seg, re.I):
                    specs[key] = seg.strip()
                    break
    # RAM from the first pair of a variant list ("6+64/6+128/8+128GB" → 6)
    if "RAM" not in specs:
        for key in ("Variants", "Storage", "RAM & Storage", "Memory & Storage",
                    "Memory", "RAM and ROM Capacities", "Storage and RAM",
                    "Combination"):
            val = specs.get(key)
            if not val:
                continue
            m = re.search(r"(\d{1,2})\s*GB\s*\+", val)
            if m:
                specs["RAM"] = f"{m.group(1)}GB"
                break


def is_phone_product(record: dict) -> bool:
    name = (record.get("name") or "").lower()
    if any(hint in name for hint in NON_PHONE_HINTS):
        return False
    return True


def _minor_to_major(value) -> float | None:
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def to_record(product: dict, php_rate: float) -> dict:
    prices = product.get("prices") or {}
    price = _minor_to_major(prices.get("price"))
    regular = _minor_to_major(prices.get("regular_price"))
    sale = _minor_to_major(prices.get("sale_price"))
    was = None
    if product.get("on_sale") and regular and sale and regular > sale:
        was = regular
    elif regular and price and regular > price:
        was = regular

    name = _clean(htmlmod.unescape(product.get("name") or ""))
    brand = (name or "").split()[0] if name else None
    images = [im.get("src") for im in (product.get("images") or []) if im.get("src")]

    color = None
    for attr in product.get("attributes") or []:
        if (attr.get("name") or "").lower() in ("colors", "color", "colour"):
            terms = [t.get("name") for t in (attr.get("terms") or []) if t.get("name")]
            if terms:
                color = _clean(terms[0])
            break

    specs = description_specs(product.get("description") or "", name)
    in_stock = bool(product.get("is_in_stock")) and bool(product.get("is_purchasable"))
    record = {
        "sku": str(product.get("sku") or product.get("id") or ""),
        "name": name,
        "brand": brand,
        "color": color,
        "price_php": price,
        "price_sar": round(price / php_rate, 2) if price else None,
        "was_price_php": was,
        "was_price_sar": round(was / php_rate, 2) if was else None,
        "currency": "PHP",
        "price_local": price,
        "availability": "InStock" if in_stock else "OutOfStock",
        "in_stock": in_stock,
        "is_purchasable": bool(product.get("is_purchasable")),
        "on_sale": bool(product.get("on_sale")),
        "stock_text": ((product.get("stock_availability") or {}).get("text")),
        "images": images,
        "specs": specs,
        "categories": [c.get("slug") for c in (product.get("categories") or [])],
        "page_valid": True,
        "delisted": False,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": product.get("permalink"),
    }
    record["is_phone"] = is_phone_product(record)
    return record


def fetch_products(fetcher: Fetcher, limit: int = 0) -> list[dict]:
    """Paginate the Store API smartphones category; each product once."""
    from urllib.parse import urlencode

    seen: set = set()
    products: list[dict] = []
    for page in range(1, PAGE_LIMIT + 1):
        url = API + "?" + urlencode({"category": SMARTPHONES_CATEGORY,
                                     "per_page": PER_PAGE, "page": page})
        try:
            batch = json.loads(fetcher.get(url, allow_cache=limit == 0))
        except Exception as err:  # noqa: BLE001
            log.warning("page %s failed: %s", page, err)
            break
        if not isinstance(batch, list) or not batch:
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
    records = [to_record(p, args.php_rate) for p in products]
    phones = [r for r in records if r.get("is_phone")]
    log.info("%s phones (%s rejected as accessories)",
             len(phones), len(records) - len(phones))

    records = sorted(phones, key=lambda r: (r.get("price_sar") or 0))
    write_jsonl(records, DATA_DIR / "greentelcom_phones.jsonl")
    write_csv(records, DATA_DIR / "greentelcom_phones.csv")

    in_stock = [r for r in records if r.get("in_stock")]
    write_jsonl(in_stock, DATA_DIR / "greentelcom_phones_in_stock.jsonl")
    write_csv(in_stock, DATA_DIR / "greentelcom_phones_in_stock.csv")

    spec_count = sum(len(r.get("specs") or {}) for r in records)
    log.info("done: %s phones (%s in stock), %s spec values · network %s / cache %s",
             len(records), len(in_stock), spec_count,
             fetcher.stats["network"], fetcher.stats["cache"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
