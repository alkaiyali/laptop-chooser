#!/usr/bin/env python3
"""Scrape all phones and their full specifications from Jarir's "Smartphones" category.

Strategy
--------
1. Category listing (https://www.jarir.com/sa-en/smartphones.html?page=N):
   product URLs come from the embedded JSON-LD ItemList (12 products per page).
2. Product pages: full specs come from the Nuxt `__NUXT_DATA__` payload:
     - product object holds raw attribute codes (e.g. `jarir_prcr`) and resolved
       display values (e.g. `jarir_prcr` -> "Apple A19 Pro")
     - `$sattributes-store` maps attribute codes -> human labels
     - `$sspecification-cache` lists the attribute codes shown in the spec table
   JSON-LD product data is used as a fallback / for curated fields.

Outputs (data/):
  jarir_phones.jsonl  - one JSON object per phone (source of truth)
  jarir_phones.csv    - wide table, one row per phone, one column per spec label
  jarir_phones.db     - SQLite: products + specs (+ products_wide view)

Only the Python standard library and `requests` are required.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CACHE_DIR,
    DATA_DIR,
    JUNK_VALUES,
    NotFoundError,
    Fetcher,
    _clean,
    _first_number,
    extract_ld_json,
    write_jsonl,
    write_wide_csv,
)
from devalue import decode_nuxt_payload  # noqa: E402

BASE_URL = "https://www.jarir.com"
CATEGORY_URL = f"{BASE_URL}/sa-en/smartphones.html"

log = logging.getLogger("jarir-phones")

# jarir_* attributes that are pricing/system fields, not spec-table entries
NON_SPEC_ATTRS = {
    "final_price",
    "tier_price",
    "master_tag",
    "mega_discount",
    "media_gallery_asset",
    "special_price",
    "special_price_ex_vat",
    "price",
}


# ---------------------------------------------------------------------------
# HTML / payload extraction helpers
# ---------------------------------------------------------------------------

SCRIPT_RE = re.compile(r'<script[^>]*\b(?:id="([^"]*)")?[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
NUXT_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)


def extract_nuxt_data(html: str) -> dict | None:
    match = NUXT_RE.search(html)
    if not match:
        return None
    try:
        return decode_nuxt_payload(match.group(1))
    except Exception as exc:  # noqa: BLE001
        log.debug("nuxt decode failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Category listing
# ---------------------------------------------------------------------------

def parse_category_page(html: str) -> tuple[int | None, list[dict]]:
    ld = extract_ld_json(html)
    graph = (ld.get("category-structured-data") or {}).get("@graph", [])
    item_list = next((g for g in graph if g.get("@type") == "ItemList"), None)
    if not item_list:
        return None, []
    total = item_list.get("numberOfItems")
    products = []
    for entry in item_list.get("itemListElement", []):
        item = entry.get("item") or {}
        offers = item.get("offers") or {}
        products.append(
            {
                "sku": str(item.get("sku") or "").strip(),
                "name": _clean(item.get("name")),
                "url": item.get("url"),
                "listing_price": _first_number(offers.get("price")),
                "listing_currency": offers.get("priceCurrency"),
                "listing_availability": (offers.get("availability") or "").rsplit("/", 1)[-1] or None,
                "listing_image": item.get("image"),
                "brand": (item.get("brand") or {}).get("name"),
                "model": item.get("model"),
                "mpn": item.get("mpn"),
            }
        )
    return total, products


def site_is_out_of_stock(item: dict) -> bool:
    """Replica of Jarir's own isOutOfStock() logic (front-end bundle).

    Order: is_stock_available -> stock_availablity -> stock.is_in_stock.
    """
    value = item.get("is_stock_available")
    if value is False or value == 0 or value == "0":
        return True
    if value is True or value == 1 or value == "1":
        return False
    status = str(item.get("stock_availablity") or "").upper()
    if status == "OUTOFSTOCK":
        return True
    if status in ("INSTOCK", "PREORDER", "BACKORDER"):
        return False
    stock = item.get("stock") or {}
    return stock.get("is_in_stock") is False


def parse_listing_stock(html: str) -> dict[str, dict]:
    """Stock data per SKU from the PLP's embedded product objects.

    These are the exact objects Jarir's "Exclude Out of Stock" filter runs
    against; `is_stock_available` is a 0/1/None facet and the Magento `stock`
    block carries qty/is_in_stock as a fallback.
    """
    data = extract_nuxt_data(html) or {}
    queries = ((data.get("state") or {}).get("$svue-query-state") or {}).get("queries") or []
    out: dict[str, dict] = {}
    for query in queries:
        try:
            hits = query["state"]["data"]["pages"][0]["data"]["hits"]["hits"]
        except (KeyError, TypeError, IndexError):
            continue
        for hit in hits:
            item = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(item, dict) or not item.get("sku"):
                continue
            stock = item.get("stock") or {}
            showrooms = item.get("available_shr")
            out[str(item["sku"])] = {
                "in_stock": not site_is_out_of_stock(item),
                "stock_attr": item.get("is_stock_available"),
                "stock_flag": item.get("klevu_stock_flag"),
                "stock_availablity": item.get("stock_availablity"),
                "stock_is_in_stock": stock.get("is_in_stock"),
                "stock_qty": stock.get("qty"),
                "display_button": item.get("display_button"),
                "pickup_showrooms": len(showrooms) if isinstance(showrooms, list) else None,
            }
    return out


def scrape_listing(fetcher: Fetcher, max_pages: int = 60) -> list[dict]:
    """Walk ?page=N until the ItemList is empty; return deduped product stubs."""
    stubs: dict[str, dict] = {}
    page = 1
    while page <= max_pages:
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}?page={page}"
        try:
            html = fetcher.get(url)
        except Exception as exc:  # noqa: BLE001
            log.error("listing page %s failed: %s", page, exc)
            break
        total, products = parse_category_page(html)
        if not products:
            log.info("listing page %s: no products, stopping (total reported: %s)", page, total)
            break
        stock_map = parse_listing_stock(html)
        new = 0
        for p in products:
            if p["sku"] and p["sku"] not in stubs:
                p["stock"] = stock_map.get(p["sku"])
                stubs[p["sku"]] = p
                new += 1
        log.info("listing page %s: %s products (%s new, %s unique so far)", page, len(products), new, len(stubs))
        if total and len(stubs) >= total:
            break
        page += 1
    return list(stubs.values())


# ---------------------------------------------------------------------------
# Product page parsing
# ---------------------------------------------------------------------------

def _resolve_spec_value(product: dict, code: str):
    value = product.get(code)
    if value in (None, ""):
        base = code[len("jarir_"):] if code.startswith("jarir_") else code
        value = product.get(base)
    return value


def extract_specs(product: dict, attrs_store: dict, spec_sets: dict) -> dict[str, str]:
    """Build {label: value} for the spec table, preserving store order."""
    codes: list[str] = []
    for spec_set in spec_sets.values():
        codes.extend(str(spec_set.get("attributes", "")).split(","))
    if not codes:
        codes = [k for k in product if k.startswith("jarir_")]
    seen_bases: set[str] = set()
    specs: dict[str, str] = {}
    for code in codes:
        code = code.strip()
        if not code:
            continue
        base = code[len("jarir_"):] if code.startswith("jarir_") else code
        if base in seen_bases or base in NON_SPEC_ATTRS:
            continue
        seen_bases.add(base)
        attr = attrs_store.get(code) or attrs_store.get(base) or {}
        label = attr.get("frontend_label") or attr.get("default_frontend_label") or base
        value = _clean(_resolve_spec_value(product, code))
        if value is None and code != f"jarir_{base}":
            value = _clean(_resolve_spec_value(product, f"jarir_{base}"))
        if value is not None:
            specs[str(label)] = value
    return specs


def parse_product_page(html: str, url: str) -> dict:
    ld = extract_ld_json(html)
    graph = (ld.get("product-structured-data") or {}).get("@graph", [])
    ld_product = next((g for g in graph if g.get("@type") == "Product"), {}) or {}
    offers = ld_product.get("offers") or {}
    key_specs = {
        p.get("name"): _clean(p.get("value"))
        for p in ld_product.get("additionalProperty", [])
        if isinstance(p, dict) and p.get("name")
    }
    key_specs = {k: v for k, v in key_specs.items() if v}

    data = extract_nuxt_data(html) or {}
    page_data = data.get("data") or {}
    slug = urlparse(url).path.rsplit("/", 1)[-1]
    product = {}
    for key, value in page_data.items():
        if key.startswith("page-") and key.endswith(".html") and isinstance(value, dict) and "product" in value:
            if key == f"page-{slug}" or not product:
                product = value.get("product") or {}
            if key == f"page-{slug}":
                break

    state = data.get("state") or {}
    attrs_store = (state.get("$sattributes-store") or {}).get("listByCode") or {}
    spec_sets = (state.get("$sspecification-cache") or {}).get("data") or {}
    specs = extract_specs(product, attrs_store, spec_sets) if product else {}

    brand = _clean(product.get("brand")) or (ld_product.get("brand") or {}).get("name")
    model = _clean(product.get("model")) or ld_product.get("model")
    mpn = _clean(product.get("mpn")) or ld_product.get("mpn")
    gtin = _clean(product.get("bar_code1")) or _clean(ld_product.get("gtin13")) or _clean(product.get("gtin"))

    price = _first_number(product.get("final_price"), offers.get("price"))
    regular = _first_number(product.get("regular_price"))
    special = _first_number(product.get("special_price"))
    jsonld_availability = _clean((offers.get("availability") or "").rsplit("/", 1)[-1])

    # Product-page stock fields; final stock verdict is resolved against the
    # listing object in `_merge_stock` (that is the source of the site filter).
    stock_attr = product.get("is_stock_available")
    stock_flag = product.get("klevu_stock_flag")
    showrooms = product.get("available_shr")
    pickup_showrooms = len(showrooms) if isinstance(showrooms, list) else None
    if stock_attr is not None:
        pdp_in_stock = bool(stock_attr)
    elif stock_flag is not None:
        pdp_in_stock = bool(stock_flag)
    elif jsonld_availability:
        pdp_in_stock = jsonld_availability.lower() == "instock"
    else:
        pdp_in_stock = None

    categories = []
    for cat in product.get("category") or []:
        if isinstance(cat, dict) and cat.get("name"):
            categories.append(cat["name"])

    images = ld_product.get("image") or []
    if isinstance(images, str):
        images = [images]
    gallery = []
    for img in product.get("media_gallery") or []:
        if isinstance(img, dict):
            candidate = img.get("file") or img.get("url") or img.get("image")
        else:
            candidate = img
        if isinstance(candidate, str) and candidate.startswith("http"):
            gallery.append(candidate)

    record = {
        "sku": str(product.get("sku") or ld_product.get("sku") or "").strip(),
        "name": _clean(product.get("name")) or _clean(ld_product.get("name")),
        "url": url,
        "brand": brand,
        "model": model,
        "series": _clean(product.get("seri")),
        "mpn": mpn,
        "gtin": gtin,
        "color": _clean(product.get("jarir_colo")) or _clean(ld_product.get("color")),
        "price_sar": price,
        "regular_price_sar": regular,
        "special_price_sar": special,
        "currency": offers.get("priceCurrency") or "SAR",
        "availability": "InStock" if pdp_in_stock else ("OutOfStock" if pdp_in_stock is False else jsonld_availability),
        "jsonld_availability": jsonld_availability,
        "in_stock": pdp_in_stock,
        "stock_source": "product_page",
        "stock_attr": stock_attr,
        "stock_flag": stock_flag,
        "stock_is_in_stock": None,
        "stock_qty": None,
        "display_button": _clean(product.get("display_button")),
        "pickup_showrooms": pickup_showrooms,
        "home_delivery": product.get("homedelivery_enable"),
        "showroom_collection": product.get("showroom_collection"),
        "fulfilment_type": _clean(product.get("fulfilment_type")),
        "rating": _first_number(product.get("rating_summary")),
        "review_count": _first_number(product.get("reviews_count")),
        "short_description": _clean(product.get("short_description")),
        "description": _clean(ld_product.get("description")) or _clean(product.get("meta_description")),
        "categories": categories,
        "category": categories[0] if categories else None,
        "origin_country": _clean(product.get("origin_country")),
        "created_at": _clean(product.get("created_at")),
        "updated_at": _clean(product.get("updated_at")),
        "images": images if isinstance(images, list) else [images],
        "key_specs": key_specs,
        "specs": specs,
        "delisted": False,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return record


def _empty_record(stub: dict) -> dict:
    """Minimal record for a listing entry whose product page is gone (404)."""
    return {
        "sku": stub.get("sku"),
        "name": stub.get("name"),
        "url": stub.get("url"),
        "brand": stub.get("brand"),
        "model": stub.get("model"),
        "series": None,
        "mpn": stub.get("mpn"),
        "gtin": None,
        "color": None,
        "price_sar": stub.get("listing_price"),
        "regular_price_sar": None,
        "special_price_sar": None,
        "currency": stub.get("listing_currency") or "SAR",
        "availability": stub.get("listing_availability"),
        "jsonld_availability": stub.get("listing_availability"),
        "in_stock": None,
        "stock_source": None,
        "stock_attr": None,
        "stock_flag": None,
        "stock_is_in_stock": None,
        "stock_qty": None,
        "display_button": None,
        "pickup_showrooms": None,
        "home_delivery": None,
        "showroom_collection": None,
        "fulfilment_type": None,
        "rating": None,
        "review_count": None,
        "short_description": None,
        "description": None,
        "categories": [],
        "category": None,
        "origin_country": None,
        "created_at": None,
        "updated_at": None,
        "images": [stub["listing_image"]] if stub.get("listing_image") else [],
        "key_specs": {},
        "specs": {},
        "delisted": True,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

CSV_BASE_COLS = [
    "sku", "name", "brand", "model", "series", "mpn", "gtin", "color",
    "price_sar", "regular_price_sar", "special_price_sar", "currency",
    "availability", "in_stock", "stock_source", "stock_attr", "stock_flag",
    "stock_is_in_stock", "stock_qty", "display_button", "pickup_showrooms",
    "home_delivery", "showroom_collection", "fulfilment_type", "jsonld_availability",
    "category", "origin_country",
    "short_description", "description", "delisted", "scraped_at", "url",
]


def write_csv(records: list[dict], path: Path) -> None:
    for rec in records:
        rec["category"] = " > ".join(rec.get("categories") or [])
    write_wide_csv(records, path, CSV_BASE_COLS)


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
                name TEXT, brand TEXT, model TEXT, series TEXT, mpn TEXT, gtin TEXT,
                color TEXT,
                price_sar REAL, regular_price_sar REAL, special_price_sar REAL,
                currency TEXT, availability TEXT, in_stock INTEGER,
                stock_source TEXT, stock_attr INTEGER, stock_flag INTEGER,
                stock_is_in_stock INTEGER, stock_qty INTEGER, display_button TEXT,
                pickup_showrooms INTEGER, home_delivery INTEGER,
                showroom_collection INTEGER, fulfilment_type TEXT,
                jsonld_availability TEXT,
                category TEXT, categories TEXT, origin_country TEXT,
                short_description TEXT, description TEXT,
                url TEXT, images TEXT, key_specs TEXT, delisted INTEGER, scraped_at TEXT
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
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["sku"], rec["name"], rec["brand"], rec["model"], rec["series"],
                    rec["mpn"], rec["gtin"], rec["color"], rec["price_sar"],
                    rec["regular_price_sar"], rec["special_price_sar"], rec["currency"],
                    rec["availability"], int(rec["in_stock"]) if rec["in_stock"] is not None else None,
                    rec.get("stock_source"), rec.get("stock_attr"), rec.get("stock_flag"),
                    _int(rec.get("stock_is_in_stock")), rec.get("stock_qty"), rec.get("display_button"),
                    rec.get("pickup_showrooms"),
                    int(bool(rec["home_delivery"])) if rec.get("home_delivery") is not None else None,
                    int(bool(rec["showroom_collection"])) if rec.get("showroom_collection") is not None else None,
                    rec.get("fulfilment_type"), rec.get("jsonld_availability"), rec.get("category"),
                    " > ".join(rec.get("categories") or []), rec["origin_country"],
                    rec["short_description"], rec["description"], rec["url"],
                    json.dumps(rec.get("images") or [], ensure_ascii=False),
                    json.dumps(rec.get("key_specs") or {}, ensure_ascii=False),
                    int(bool(rec.get("delisted"))), rec.get("scraped_at"),
                ),
            )
            for pos, (label, value) in enumerate(rec.get("specs", {}).items()):
                cur.execute("INSERT OR REPLACE INTO specs VALUES (?,?,?,?)", (rec["sku"], label, value, pos))
        conn.commit()
    finally:
        conn.close()


PHONE_NAME_HINTS = (
    "iphone", "galaxy s", "galaxy z", "galaxy a", "galaxy m",
    "pixel", "redmi", "poco", "xiaomi", "honor", "huawei",
    "oppo", "vivo", "realme", "oneplus", "nothing phone",
    "infinix", "tecno", "motorola", "nokia", "sony xperia",
    "smartphone", "5g mobile",
)
# strong reject signals: tablets, wearables and accessories share the category
NON_PHONE_HINTS = (
    "tablet", "ipad", "watch", "buds", "airpods", "case", "cover",
    "charger", "cable", "protector", "stand", "holder", "stylus",
    "keyboard", "speaker", "power bank",
)


def is_phone_product(record: dict) -> bool:
    """The smartphones category also lists tablets/watches/accessories.

    Spec rows are the strongest signal (chipset + capacity + cameras);
    names are the fallback for sparse marketplace-style rows.
    """
    labels = {label.lower() for label in (record.get("specs") or {})}
    has_soc = any("chipset" in label for label in labels)
    has_cap = any(label == "capacity" for label in labels)
    has_cam = any("cam resolution" in label or "rear camera" in label for label in labels)
    if has_soc and (has_cap or has_cam):
        ptype = ((record.get("specs") or {}).get("Product Type") or "").lower()
        if ptype and "tablet" in ptype:
            return False
        return True
    name = (record.get("name") or "").lower()
    if any(hint in name for hint in NON_PHONE_HINTS):
        return False
    return any(hint in name for hint in PHONE_NAME_HINTS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="only scrape the first N products (0 = all)")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.25, help="minimum seconds between HTTP requests")
    parser.add_argument("--refresh", action="store_true", help="ignore the on-disk HTML cache")
    parser.add_argument("--max-age", type=float, default=0, metavar="HOURS",
                        help="re-fetch cached pages older than N hours (0 = never expire)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    fetcher = Fetcher(CACHE_DIR, min_interval=args.interval, refresh=args.refresh,
                      max_age=(args.max_age * 3600 if args.max_age > 0 else None))

    log.info("fetching category listing ...")
    stubs = scrape_listing(fetcher)
    if args.limit:
        stubs = stubs[: args.limit]
    log.info("unique products to scrape: %s", len(stubs))
    if not stubs:
        log.error("no products found")
        return 1

    records: list[dict] = []
    failures: list[tuple[str, str]] = []
    delisted: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_scrape_one, fetcher, stub): stub for stub in stubs}
        for i, future in enumerate(as_completed(futures), 1):
            stub = futures[future]
            try:
                rec = future.result()
                rec["listing"] = stub
                records.append(rec)
                log.info("[%s/%s] %s", i, len(stubs), (rec.get("name") or stub.get("name") or stub["url"])[:90])
            except NotFoundError:
                delisted.append(stub["url"])
                records.append(_empty_record(stub))
                log.warning("[%s/%s] delisted (404): %s", i, len(stubs), stub["url"])
            except Exception as exc:  # noqa: BLE001
                failures.append((stub["url"], str(exc)))
                log.error("[%s/%s] FAILED %s: %s", i, len(stubs), stub["url"], exc)

    records.sort(key=lambda r: (r.get("brand") or "", r.get("name") or ""))
    for rec in records:
        rec["is_phone"] = is_phone_product(rec)
    write_jsonl(records, DATA_DIR / "jarir_phones.jsonl")
    write_csv(records, DATA_DIR / "jarir_phones.csv")
    write_sqlite(records, DATA_DIR / "jarir_phones.db")

    # filtered view: only purchasable products (excludes delisted listings and
    # anything the site's "Exclude Out of Stock" filter would hide)
    in_stock = [r for r in records
                if not r.get("delisted") and r.get("in_stock") and r.get("is_phone")]
    write_jsonl(in_stock, DATA_DIR / "jarir_phones_in_stock.jsonl")
    write_csv(in_stock, DATA_DIR / "jarir_phones_in_stock.csv")
    write_sqlite(in_stock, DATA_DIR / "jarir_phones_in_stock.db")

    spec_count = sum(len(r.get("specs") or {}) for r in records)
    log.info(
        "done: %s phones (%s in stock), %s spec values total, %s delisted, %s failures",
        len(records), len(in_stock), spec_count, len(delisted), len(failures),
    )
    log.info("wrote %s", DATA_DIR / "jarir_phones.jsonl")
    log.info("wrote %s", DATA_DIR / "jarir_phones.csv")
    log.info("wrote %s", DATA_DIR / "jarir_phones.db")
    log.info("wrote %s", DATA_DIR / "jarir_phones_in_stock.jsonl")
    log.info("wrote %s", DATA_DIR / "jarir_phones_in_stock.csv")
    log.info("wrote %s", DATA_DIR / "jarir_phones_in_stock.db")
    if delisted:
        (DATA_DIR / "delisted.txt").write_text("\n".join(delisted), encoding="utf-8")
    if failures:
        (DATA_DIR / "failures.txt").write_text("\n".join(f"{u}\t{e}" for u, e in failures), encoding="utf-8")
    return 0


def _scrape_one(fetcher: Fetcher, stub: dict) -> dict:
    html = fetcher.get(stub["url"])
    rec = parse_product_page(html, stub["url"])
    # fill gaps from the listing stub
    for key in ("name", "brand", "model", "mpn"):
        if not rec.get(key):
            rec[key] = stub.get(key)
    if rec.get("price_sar") is None:
        rec["price_sar"] = stub.get("listing_price")
    _merge_stock(rec, stub)
    return rec


def _merge_stock(rec: dict, stub: dict) -> None:
    """Resolve the final stock verdict from the site filter's own data.

    The PLP product object (captured while walking the listing) is what the
    "Exclude Out of Stock" checkbox filters on, so it wins over the product
    page fields. The PDP fills any gaps (e.g. showroom counts).
    """
    stock = stub.get("stock") or {}
    # these fields describe the listing-index object (the filter's source of
    # truth), so take them as-is, including None
    for src in ("stock_attr", "stock_flag", "stock_is_in_stock", "stock_qty", "display_button"):
        rec[src] = stock.get(src)
    if stock.get("pickup_showrooms") is not None:
        rec["pickup_showrooms"] = stock["pickup_showrooms"]
    if stock.get("in_stock") is not None:
        rec["in_stock"] = stock["in_stock"]
        rec["stock_source"] = "listing"
    if rec.get("in_stock") is None:
        rec["in_stock"] = stub.get("listing_availability", "").lower() == "instock" if stub.get("listing_availability") else None
        rec["stock_source"] = "jsonld"
    rec["availability"] = "InStock" if rec.get("in_stock") else ("OutOfStock" if rec.get("in_stock") is False else rec.get("jsonld_availability"))


def _int(value):
    if value is None:
        return None
    return int(bool(value))


if __name__ == "__main__":
    raise SystemExit(main())
