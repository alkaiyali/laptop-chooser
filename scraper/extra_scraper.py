#!/usr/bin/env python3
"""Scrape all laptops and their specifications from extra.com (Saudi Arabia).

Strategy
--------
extra.com is SAP Commerce with a Svelte PLP micro-frontend backed by the Unbxd
search API. Everything needed is public and structured:

1. Listing — Unbxd `search` API (api key + site key are in the page config):
     filter=type:PRODUCT & filter=familyEn:"Laptops"
   Each hit carries price, brand, model number, warranty, images, the product URL
   and per-city stock entries (`SA-<city>_inStock` / `SA-<city>_outOfStock`).
   The site's "In Stock" checkbox for the default city (Riyadh) filters on
   `inStock:"SA-riyadh_inStock"` — replicated here.
2. Product pages — the JSON-LD `Product` block contains the full labelled spec
   list (`additionalProperty`, 40-55 entries per laptop).

Outputs (data/):
  extra_laptops.jsonl / .csv / .db              - all laptops (in stock or not)
  extra_laptops_in_stock.jsonl / .csv / .db     - in stock in Riyadh, purchasable
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CACHE_DIR,
    DATA_DIR,
    NotFoundError,
    Fetcher,
    _clean,
    _first_number,
    extract_ld_json,
    write_jsonl,
    write_wide_csv,
)

log = logging.getLogger("extra")

UNBXD_API_KEY = "21705619e273429e5767eea44ccb1ad5"
UNBXD_SITE_KEY = "ss-unbxd-auk-extra-saudi-en-prod11541714990488"
UNBXD_BASE = f"https://search.unbxd.io/{UNBXD_API_KEY}/{UNBXD_SITE_KEY}/search"
CATEGORY_FILTER = 'familyEn:"Laptops"'
DEFAULT_CITY = "SA-riyadh"
PAGE_SIZE = 100

LAPTOP_NAME_HINTS = (
    "laptop", "notebook", "macbook", "chromebook", "ultrabook", "thinkpad", "ideapad",
    "vivobook", "zenbook", "pavilion", "probook", "elitebook", "omnibook", "victus",
    "legion", "loq ", "surface laptop", "surface pro", "surface book", "matebook",
    "magicbook", "aspire", "nitro", "predator", "katana", "cyborg", "raider", "vector",
    "stealth", "swift", "tuf gaming", "rog ", "zephyrus", "expertbook", "galaxy book",
    "alienware",
)
# accessory indicators strong enough to reject a name-only match (weak words like
# "mouse" or "webcam" also appear in laptop names/bundles, so they are not here)
ACCESSORY_HINTS = (
    "game pad", "gamepad", "portable gaming device", "laptop bag", "backpack", "sleeve",
    "cooling pad", "laptop stand", "screen protector", "hard case",
)


def is_laptop_product(record: dict) -> bool:
    """Category includes a few accessories (game pads...) — keep actual laptops.

    Core specs are the strongest signal; names are only used when the spec list
    is sparse (mostly marketplace items).
    """
    labels = {label.lower() for label in (record.get("specs") or {})}
    has_cpu = any("processor" in label for label in labels)
    has_body = any(("ram" in label) or ("storage" in label) or ("screen size" in label) for label in labels)
    if has_cpu and has_body:
        return True
    name = (record.get("name") or "").lower()
    if any(hint in name for hint in ACCESSORY_HINTS):
        return False
    return any(hint in name for hint in LAPTOP_NAME_HINTS)

CSV_BASE_COLS = [
    "sku", "name", "brand", "model_number", "mpn", "barcode",
    "price_sar", "selling_price_sar", "was_price_sar", "discount_pct", "currency",
    "availability", "in_stock", "in_stock_any", "in_stock_cities", "out_of_stock_cities",
    "home_delivery", "collect_from_store", "add_to_cart", "preorder", "coming_soon",
    "restockable", "product_status", "rating", "review_count", "warranty_years",
    "family", "sub_family", "category", "origin", "is_laptop", "page_valid", "description", "delisted", "scraped_at", "url",
]


# ---------------------------------------------------------------------------
# Unbxd listing
# ---------------------------------------------------------------------------

def _unbxd_url(filters: list[str], start: int = 0, rows: int = PAGE_SIZE) -> str:
    params = [("q", "*"), ("rows", rows), ("start", start), ("version", "V2"), ("filter", "type:PRODUCT")]
    params += [("filter", f) for f in filters]
    return UNBXD_BASE + "?" + urllib.parse.urlencode(params)


def fetch_laptops(api: Fetcher) -> list[dict]:
    """Paginate the Unbxd index for all laptops under the category."""
    products: list[dict] = []
    start = 0
    total = None
    while True:
        url = _unbxd_url([CATEGORY_FILTER], start=start)
        data = json.loads(api.get(url))
        response = data.get("response") or {}
        hits = response.get("products") or []
        total = response.get("numberOfProducts", total)
        products.extend(hits)
        log.info("listing: %s/%s laptops", len(products), total)
        if not hits or total is None or len(products) >= total:
            break
        start += PAGE_SIZE
    # dedupe by productCode, keep first
    seen: set[str] = set()
    unique = []
    for item in products:
        code = str(item.get("productCode") or item.get("uniqueId") or "")
        if code and code not in seen:
            seen.add(code)
            unique.append(item)
    return unique


def _city_list(item: dict, suffix: str) -> list[str]:
    cities = []
    for entry in item.get("inStock") or []:
        if isinstance(entry, str) and entry.endswith(suffix):
            cities.append(entry[: -len(suffix)].replace("SA-", ""))
    return sorted(cities)


def listing_to_stock(item: dict, city: str = DEFAULT_CITY) -> dict:
    city_name = city.replace("SA-", "")
    in_stock_cities = _city_list(item, "_inStock")
    out_of_stock_cities = _city_list(item, "_outOfStock")
    return {
        "in_stock": city_name in in_stock_cities,
        "cities": in_stock_cities,
        "out_of_stock_cities": out_of_stock_cities,
        "any_city": bool(item.get("inStockFlag")),
    }


# ---------------------------------------------------------------------------
# Product page
# ---------------------------------------------------------------------------

def _product_ld(html: str) -> dict:
    for block in extract_ld_json(html).values():
        items = block if isinstance(block, list) else [block]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def parse_product_page(html: str, url: str) -> dict:
    ld = _product_ld(html)
    offers = ld.get("offers") or {}
    rating = offers.get("aggregateRating") or {}

    specs: dict[str, str] = {}
    for prop in ld.get("additionalProperty") or []:
        if not isinstance(prop, dict):
            continue
        label = _clean(prop.get("name"))
        value = _clean(prop.get("value"))
        if label and value:
            specs[label] = value

    images = ld.get("image") or []
    if isinstance(images, str):
        images = [images]

    description = None
    match = re.search(r'"descriptionEn"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if match:
        try:
            description = _clean(json.loads(f'"{match.group(1)}"'))
        except json.JSONDecodeError:
            description = _clean(match.group(1))

    return {
        "sku": _clean(ld.get("sku")),
        "name": _clean(ld.get("name")),
        "brand": (ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else _clean(ld.get("brand")),
        "mpn": _clean(ld.get("mpn")),
        "gtin": _clean(ld.get("gtin13")),
        "price_sar": _first_number(offers.get("price")),
        "currency": offers.get("priceCurrency") or "SAR",
        "availability": _clean((offers.get("availability") or "").rsplit("/", 1)[-1]),
        "rating": _first_number(rating.get("ratingValue")),
        "review_count": _first_number(rating.get("reviewCount")),
        "description": description,
        "images": images,
        "specs": specs,
        "url": url,
    }


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _first_str(value):
    if isinstance(value, list):
        return _clean(value[0]) if value else None
    return _clean(value)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def merge_record(listing: dict, parsed: dict | None, *, delisted: bool) -> dict:
    stock = listing_to_stock(listing)
    parsed = parsed or {}
    product_code = str(listing.get("productCode") or listing.get("uniqueId") or parsed.get("sku") or "")

    if parsed.get("sku") and product_code and parsed["sku"] != product_code:
        log.debug("sku mismatch: listing=%s page=%s", product_code, parsed["sku"])

    price = parsed.get("price_sar") or _first_number(listing.get("price"), listing.get("sellingPrice"))
    was_price = _first_number(listing.get("wasPrice"))
    selling = _first_number(listing.get("sellingPrice"))
    discount = _first_number(listing.get("priceValueDiscountPercentage"))

    images = parsed.get("images") or []
    if not images:
        images = listing.get("imageUrl") or []
    if not images:
        media = listing.get("productMediaUrls") or []
        images = ["https:" + m if m.startswith("//") else m for m in media]

    record = {
        "sku": product_code or parsed.get("sku"),
        "name": parsed.get("name") or _clean(listing.get("title")),
        "brand": parsed.get("brand") or _first_str(listing.get("brandEn")) or _first_str(listing.get("brand")),
        "model_number": _clean(listing.get("modelNumber")),
        "mpn": parsed.get("mpn"),
        "barcode": _clean(listing.get("barCode")),
        "price_sar": price,
        "selling_price_sar": selling,
        "was_price_sar": was_price,
        "discount_pct": round(discount, 2) if discount is not None else None,
        "currency": parsed.get("currency") or listing.get("currency") or "SAR",
        "availability": "InStock" if stock["in_stock"] else ("OutOfStock" if not delisted else None),
        "jsonld_availability": parsed.get("availability"),
        "in_stock": stock["in_stock"],
        "in_stock_any": stock["any_city"],
        "in_stock_cities": stock["cities"],
        "out_of_stock_cities": stock["out_of_stock_cities"],
        "home_delivery": bool(listing.get("homeDeliveryEnabled")),
        "collect_from_store": bool(listing.get("collectFromStoreEnabled")),
        "add_to_cart": bool(listing.get("isAddToCartEnabled")),
        "preorder": _bool(listing.get("preorder")),
        "coming_soon": _bool(listing.get("comingSoon")),
        "restockable": _bool(listing.get("isReStockable")),
        "product_status": (listing.get("productStatusEn") or [None])[0] if isinstance(listing.get("productStatusEn"), list) else _clean(listing.get("productStatusEn")),
        "rating": parsed.get("rating"),
        "review_count": parsed.get("review_count"),
        "warranty_years": _clean(listing.get("yearOfWarranty")),
        "family": _clean(listing.get("familyEn")),
        "sub_family": _clean(listing.get("subFamilyEn")),
        "categories": listing.get("categoryPath") or [],
        "category": (listing.get("categoryPath") or [None])[0],
        "origin": None,
        "description": parsed.get("description") or _clean(listing.get("summaryEn")),
        "images": images,
        "specs": parsed.get("specs") or {},
        "page_valid": bool(parsed.get("sku")),
        "listing": {
            "product_code": product_code,
            "price": _first_number(listing.get("price")),
            "selling_price": selling,
            "was_price": was_price,
            "discount_pct": discount,
            "product_status": listing.get("productStatusEn"),
            "offers_facet": listing.get("offersFacet"),
            "online_exclusive": listing.get("onlineExclusive"),
            "marketplace": _bool(listing.get("isMarketPlaceItem")),
            "sold_by": listing.get("soldByFacet"),
        },
        "delisted": delisted,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": listing.get("productUrl") or parsed.get("url"),
    }
    # top specs while a product page is unavailable
    if not record["specs"] and listing.get("topXClassifAttributsEn"):
        for entry in listing["topXClassifAttributsEn"]:
            if isinstance(entry, str) and ":" in entry:
                label, value = entry.split(":", 1)
                record["specs"][label.strip()] = value.strip()
    return record


def scrape_one(pdp: Fetcher, listing: dict) -> dict:
    url = listing.get("productUrl")
    if not url:
        return merge_record(listing, None, delisted=True)
    try:
        html = pdp.get(url)
    except NotFoundError:
        return merge_record(listing, None, delisted=True)
    parsed = parse_product_page(html, url)
    if not parsed.get("sku"):
        # some CDN/bot-check responses are 200 but not the PDP - refetch once
        log.debug("refetching %s (no product JSON-LD in cached page)", url)
        try:
            html = pdp.get(url, allow_cache=False)
        except NotFoundError:
            return merge_record(listing, None, delisted=True)
        parsed = parse_product_page(html, url)
    record = merge_record(listing, parsed, delisted=False)
    record["is_laptop"] = is_laptop_product(record)
    return record


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def write_sqlite(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS specs;
            CREATE TABLE products (
                sku TEXT PRIMARY KEY,
                name TEXT, brand TEXT, model_number TEXT, mpn TEXT, barcode TEXT,
                price_sar REAL, selling_price_sar REAL, was_price_sar REAL,
                discount_pct REAL, currency TEXT, availability TEXT,
                in_stock INTEGER, in_stock_any INTEGER,
                in_stock_cities TEXT, out_of_stock_cities TEXT,
                home_delivery INTEGER, collect_from_store INTEGER, add_to_cart INTEGER,
                preorder INTEGER, coming_soon INTEGER, restockable INTEGER,
                product_status TEXT, rating REAL, review_count INTEGER,
                warranty_years TEXT, family TEXT, sub_family TEXT,
                category TEXT, categories TEXT, origin TEXT, is_laptop INTEGER,
                page_valid INTEGER, description TEXT,
                url TEXT, images TEXT, delisted INTEGER, scraped_at TEXT
            );
            CREATE TABLE specs (
                sku TEXT NOT NULL REFERENCES products(sku),
                label TEXT NOT NULL,
                value TEXT,
                position INTEGER,
                PRIMARY KEY (sku, label)
            );
            """
        )
        for rec in records:
            cur.execute(
                """INSERT OR REPLACE INTO products VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["sku"], rec["name"], rec["brand"], rec["model_number"], rec["mpn"], rec["barcode"],
                    rec["price_sar"], rec["selling_price_sar"], rec["was_price_sar"], rec["discount_pct"],
                    rec["currency"], rec["availability"],
                    int(rec["in_stock"]) if rec["in_stock"] is not None else None,
                    int(rec["in_stock_any"]) if rec["in_stock_any"] is not None else None,
                    json.dumps(rec["in_stock_cities"]), json.dumps(rec["out_of_stock_cities"]),
                    int(rec["home_delivery"]), int(rec["collect_from_store"]), int(rec["add_to_cart"]),
                    int(rec["preorder"]), int(rec["coming_soon"]), int(rec["restockable"]),
                    rec["product_status"], rec["rating"], rec["review_count"], rec["warranty_years"],
                    rec["family"], rec["sub_family"], rec["category"],
                    " > ".join(rec.get("categories") or []), rec["origin"],
                    int(bool(rec.get("is_laptop"))), int(bool(rec.get("page_valid"))), rec["description"],
                    rec["url"], json.dumps(rec.get("images") or [], ensure_ascii=False),
                    int(bool(rec.get("delisted"))), rec["scraped_at"],
                ),
            )
            for pos, (label, value) in enumerate(rec.get("specs", {}).items()):
                cur.execute("INSERT OR REPLACE INTO specs VALUES (?,?,?,?)", (rec["sku"], label, value, pos))
        conn.commit()
    finally:
        conn.close()


def write_csv(records: list[dict], path: Path) -> None:
    rows = []
    for rec in records:
        row = dict(rec)
        row["in_stock_cities"] = ",".join(rec.get("in_stock_cities") or [])
        row["out_of_stock_cities"] = f"{len(rec.get('out_of_stock_cities') or [])} cities"
        row["category"] = " > ".join(rec.get("categories") or [])
        rows.append(row)
    write_wide_csv(rows, path, CSV_BASE_COLS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="only scrape the first N products (0 = all)")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.2, help="minimum seconds between HTTP requests")
    parser.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    parser.add_argument("--max-age", type=float, default=0, metavar="HOURS",
                        help="re-fetch cached responses older than N hours (0 = never expire)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    max_age = args.max_age * 3600 if args.max_age > 0 else None
    api = Fetcher(CACHE_DIR, min_interval=args.interval, refresh=args.refresh,
                  headers={"Authorization": UNBXD_API_KEY, "Accept": "application/json"}, max_age=max_age)
    pdp = Fetcher(CACHE_DIR, min_interval=args.interval, refresh=args.refresh, max_age=max_age)

    listings = fetch_laptops(api)
    in_stock_now = sum(1 for item in listings if listing_to_stock(item)["in_stock"])
    log.info("found %s laptops (%s in stock in %s)", len(listings), in_stock_now, DEFAULT_CITY)
    if args.limit:
        listings = listings[: args.limit]
    if not listings:
        log.error("no laptops found")
        return 1

    records: list[dict] = []
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(scrape_one, pdp, item): item for item in listings}
        for i, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                rec = future.result()
                records.append(rec)
                flag = "in-stock" if rec["in_stock"] else "OOS"
                log.info("[%s/%s] %s %s", i, len(listings), flag, (rec.get("name") or "")[:80])
            except Exception as exc:  # noqa: BLE001
                failures.append((item.get("productUrl") or "", str(exc)))
                log.error("[%s/%s] FAILED %s: %s", i, len(listings), item.get("productUrl"), exc)

    records.sort(key=lambda r: (r.get("brand") or "", r.get("name") or ""))
    write_jsonl(records, DATA_DIR / "extra_laptops.jsonl")
    write_csv(records, DATA_DIR / "extra_laptops.csv")
    write_sqlite(records, DATA_DIR / "extra_laptops.db")

    in_stock = [
        r for r in records
        if r.get("in_stock") and not r.get("delisted") and r.get("page_valid") and r.get("is_laptop")
    ]
    write_jsonl(in_stock, DATA_DIR / "extra_laptops_in_stock.jsonl")
    write_csv(in_stock, DATA_DIR / "extra_laptops_in_stock.csv")
    write_sqlite(in_stock, DATA_DIR / "extra_laptops_in_stock.db")

    spec_count = sum(len(r.get("specs") or {}) for r in records)
    log.info("done: %s laptops (%s in stock), %s spec values, %s failures",
             len(records), len(in_stock), spec_count, len(failures))
    for name in ("extra_laptops.jsonl", "extra_laptops.csv", "extra_laptops.db",
                 "extra_laptops_in_stock.jsonl", "extra_laptops_in_stock.csv", "extra_laptops_in_stock.db"):
        log.info("wrote %s", DATA_DIR / name)
    if failures:
        (DATA_DIR / "extra_failures.txt").write_text(
            "\n".join(f"{u}\t{e}" for u, e in failures), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
