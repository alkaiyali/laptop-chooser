#!/usr/bin/env python3
"""DataBlitz (Philippines) laptop scraper.

https://ecommerce.datablitz.com.ph is a Shopify store.  Its two laptop collections
expose a public product feed (``/collections/<handle>/products.json``, 250 products
per page) with prices, per-variant availability, images, tags and descriptions — so
the whole scrape is ~8 requests instead of walking the 25k-product storefront.

The same filters as the store's own laptop search are applied:

    https://ecommerce.datablitz.com.ph/search?type=product&q=laptop
        &pf_st_availability=true&pf_p_price=30000%3A299995

i.e. in stock only (:attr:`AVAILABLE_ONLY`) and PHP 30,000-299,995
(:data:`PRICE_MIN_PHP` / :data:`PRICE_MAX_PHP`), writing:

    data/datablitz_laptops.jsonl            matching laptop listings
    data/datablitz_laptops_in_stock.jsonl   the in-stock subset

Prices stay in pesos (``price_php``) but are also converted to SAR at a reference
rate so DataBlitz entries can be compared and filtered next to Jarir and eXtra —
the default 16.4 PHP per SAR is the rate used in the Pricing Ladder research and
can be overridden with ``--php-rate``.

Listings that disappear from the feed are kept with ``delisted: true`` (last known
price) so the chooser can show them greyed out instead of silently dropping them.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CACHE_DIR, DATA_DIR, Fetcher  # noqa: E402

log = logging.getLogger("datablitz")

BASE = "https://ecommerce.datablitz.com.ph"
# the store's own laptop collections (the two handles behind its laptop search)
COLLECTIONS = ("computer-gaming-laptops", "computer-non-gaming-laptops")
FEED = BASE + "/collections/{handle}/products.json?limit=250&page={page}"
PAGE_LIMIT = 12                      # 12 x 250 = 3k products per collection, plenty
DEFAULT_PHP_RATE = 16.4              # PHP per 1 SAR (Pricing Ladder, Aug 2026)
PRICE_MIN_PHP = 30_000               # same window as the store's search filter
PRICE_MAX_PHP = 299_995
AVAILABLE_ONLY = True                # pf_st_availability=true


def _plain(html_text: str | None) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def is_laptop(product: dict) -> bool:
    """The collections are laptop-only, but guard against accessories that slipped in."""
    title = (product.get("title") or "").lower()
    ptype = (product.get("product_type") or "").lower()
    if re.search(r"\blaptop-(memory|ram|stand|sleeve|bag|briefcase|cooling|dock|fan|case|"
                 r"skin|charger|keyboard|mouse|mat|riser|table)\b", title):
        return False
    return "laptop" in ptype or bool(re.search(r"\blaptop\b", title))


def _price(product: dict) -> tuple[float | None, float | None, bool, list[dict]]:
    """(price, was_price, any_available, variants) over all Shopify variants."""
    prices, was, available = [], [], False
    variants = []
    for v in product.get("variants") or []:
        try:
            price = float(v.get("price"))
        except (TypeError, ValueError):
            continue
        compare = v.get("compare_at_price")
        try:
            compare = float(compare) if compare not in (None, "") else None
        except (TypeError, ValueError):
            compare = None
        if v.get("available"):
            available = True
        prices.append(price)
        if compare and compare > price:
            was.append(compare)
        variants.append({"title": v.get("title"), "sku": v.get("sku"), "price": price,
                         "available": bool(v.get("available")), "grams": v.get("grams")})
    if not prices:
        return None, None, available, variants
    return min(prices), (max(was) if was else None), available, variants


def to_record(product: dict, php_rate: float) -> dict:
    price, was, in_stock, variants = _price(product)
    if price is None:
        return {}
    images = [i.get("src") for i in (product.get("images") or []) if i.get("src")]
    sku = next((v["sku"] for v in variants if v.get("sku")), str(product.get("id")))
    description = _plain(product.get("body_html"))
    return {
        "store": "DataBlitz",
        "sku": sku,
        "name": product.get("title"),
        "brand": product.get("vendor"),
        "product_type": product.get("product_type"),
        "url": f"{BASE}/products/{product.get('handle')}",
        "images": images,
        "price_php": round(price, 2),
        "price_sar": round(price / php_rate),
        "was_price_php": round(was, 2) if was else None,
        "was_price_sar": round(was / php_rate) if was else None,
        "currency": "PHP",
        "price_local": round(price, 2),
        "in_stock": in_stock,
        "delisted": False,
        "stock_note": None,
        "variants": variants,
        "tags": product.get("tags") or [],
        "short_description": description[:600],
        "description": description[:4000],
        "listing": {"marketplace": False, "handle": product.get("handle"),
                    "product_id": product.get("id"),
                    "published_at": product.get("published_at")},
    }


def fetch_products(fetcher: Fetcher, limit: int = 0) -> list[dict]:
    """Walk the two laptop collections; each product is returned once."""
    seen: set = set()
    products: list[dict] = []
    for handle in COLLECTIONS:
        for page in range(1, PAGE_LIMIT + 1):
            url = FEED.format(handle=handle, page=page)
            try:
                payload = json.loads(fetcher.get(url, allow_cache=limit == 0))
            except Exception as err:  # noqa: BLE001
                log.warning("%s page %s failed: %s", handle, page, err)
                break
            batch = payload.get("products") or []
            if not batch:
                break
            fresh = [p for p in batch if p.get("id") not in seen]
            seen.update(p.get("id") for p in fresh)
            products.extend(fresh)
            log.info("%s page %s: %s products (%s new, total %s)",
                     handle, page, len(batch), len(fresh), len(products))
            if limit and len(products) >= limit:
                return products[:limit]
    return products


def previous_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    for line in path.open(encoding="utf-8"):
        if line.strip():
            rec = json.loads(line)
            out[rec.get("sku")] = rec
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--php-rate", type=float, default=DEFAULT_PHP_RATE,
                        help=f"pesos per 1 SAR (default {DEFAULT_PHP_RATE})")
    parser.add_argument("--limit", type=int, default=0, help="only read the first N products (0 = all)")
    parser.add_argument("--min-price", type=float, default=PRICE_MIN_PHP,
                        help=f"minimum price in PHP (default {PRICE_MIN_PHP:,})")
    parser.add_argument("--max-price", type=float, default=PRICE_MAX_PHP,
                        help=f"maximum price in PHP (default {PRICE_MAX_PHP:,})")
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
    laptops = [p for p in products if is_laptop(p)]
    log.info("read %s collection products, %s of them laptops", len(products), len(laptops))

    # the store's own search filters: availability first, then the price window
    def wanted(p: dict) -> bool:
        price, _was, available, _v = _price(p)
        if price is None:
            return False
        if AVAILABLE_ONLY and not available:
            return False
        return args.min_price <= price <= args.max_price

    laptops = [p for p in laptops if wanted(p)]
    log.info("%s laptops in stock within PHP %s-%s", len(laptops), f"{args.min_price:,}", f"{args.max_price:,}")

    current = [to_record(p, args.php_rate) for p in laptops]
    current = [r for r in current if r]
    seen = {r["sku"] for r in current}

    # keep listings that vanished from the feed (delisted, last known price)
    old = previous_records(DATA_DIR / "datablitz_laptops.jsonl")
    delisted = []
    for sku, rec in old.items():
        if sku not in seen and not rec.get("delisted"):
            rec = dict(rec)
            rec.update({"delisted": True, "in_stock": False, "stock_note": "delisted"})
            delisted.append(rec)
    if delisted:
        log.info("%s listings no longer in the feed kept as delisted", len(delisted))

    rows = sorted(current + delisted, key=lambda r: (r.get("price_sar") or 0))
    write_jsonl(DATA_DIR / "datablitz_laptops.jsonl", rows)
    in_stock = [r for r in rows if r.get("in_stock") and not r.get("delisted")]
    write_jsonl(DATA_DIR / "datablitz_laptops_in_stock.jsonl", in_stock)

    prices = [r["price_php"] for r in in_stock if r.get("price_php")]
    log.info("wrote %s laptops (%s in stock) · PHP %.0f-%.0f · cash %s / cache %s",
             len(rows), len(in_stock), min(prices, default=0), max(prices, default=0),
             fetcher.stats["network"], fetcher.stats["cache"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
