#!/usr/bin/env python3
"""noon.com (Saudi Arabia) laptop scraper.

Reads the storefront's public catalog API (the same JSON endpoint the noon
website itself queries when rendering the laptops category page):

    https://www.noon.com/_svc/catalog/api/v3/u/laptops?limit=50&page=N

28 pages / 1,356 offers for the laptops category.  The category mixes in accessories
(stands, sleeves, and a surprising number of paper "notebooks"), so a listing only
counts as a laptop when the title says laptop/notebook/macbook **and** it carries a
RAM or storage spec (or a "16GB RAM"-style pattern) — that leaves ~1,150 machines.

Every hit carries the price (``sale_price`` when discounted), the stock flag, rating,
seller (``store_name`` — noon itself or a marketplace partner such as eXtra) and a
small ``plp_specifications`` block with RAM, storage, OS and core count.

Writes:
    data/noon_laptops.jsonl            laptops (incl. unavailable)
    data/noon_laptops_in_stock.jsonl   the orderable ones
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CACHE_DIR, DATA_DIR, Fetcher  # noqa: E402
from quality import is_junk  # noqa: E402

log = logging.getLogger("noon")

API = "https://www.noon.com/_svc/catalog/api/v3/u/laptops?limit=50&page={page}"
SITE = "https://www.noon.com/saudi-en"
DEFAULT_PAGES = 30
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-SA,en;q=0.9",
    "x-locale": "en-sa",
    "x-mp": "noon",
    "x-platform": "web",
    "x-requested-with": "XMLHttpRequest",
    "Referer": "https://www.noon.com/saudi-en/laptops/",
}

LAPTOP_RE = re.compile(r"\b(laptop|notebook|macbook|chromebook|ultrabook)\b", re.I)
# words that never appear in a real laptop title but do in the accessories the
# category mixes in (careful: "ram"/"ssd"/"memory" ARE in laptop titles)
ACCESSORY_RE = re.compile(
    r"\b(stand|sleeve|backpack|cooling|docking|dock|keyboard|mouse|charger|skin|protector|hub|"
    r"adapter|cable|mount|riser|desk|trolley|stylus|speaker|connector|journal|notepad|sodimm|"
    r"so-dimm|dimm|trolley|lock|foam|cushion|lapdesk)\b", re.I)
# a machine names its CPU (or at least its OS) — accessories do neither
CPU_RE = re.compile(
    r"\b(?:intel\s+)?(?:core\s*(?:ultra\s*)?[3579]|ultra\s*[3579]|i[3579]-\d{3,5}|"
    r"celeron|pentium|ryzen\s+(?:ai\s+)?(?:max\+?\s*)?[3579]|snapdragon\s+x|apple\s+m\d|"
    r"\bm[1-5]\b|n\d{3,4}\b)", re.I)
RAM_IN_NAME_RE = re.compile(r"\b\d{1,3}\s?gb\s*(?:ram|ddr|lpddr)", re.I)


def _specs(hit: dict) -> dict:
    """noon's small spec block, using the labels the normalizer reads."""
    raw = hit.get("plp_specifications") or {}
    out: dict = {}
    if raw.get("RAM Size"):
        out["RAM"] = str(raw["RAM Size"])
    if raw.get("Internal Memory"):
        out["Storage"] = str(raw["Internal Memory"])
    if raw.get("Operating System"):
        os_text = str(raw["Operating System"])
        if raw.get("OS Version") and str(raw["OS Version"]).lower() not in os_text.lower():
            os_text = f"{os_text} {raw['OS Version']}"
        out["Operating System"] = os_text
    # the full plp block carries more than the three rows above — a CPU model
    # the title drops ("Core-i9 Processor" + Processor Version i9-12900H), the
    # SKU, panel details.  Map every label the normalizer reads.
    for src, dst in (
        ("Processor Version", "Processor Type"), ("Processor", "Processor Type"),
        ("Processor Type", "Processor Type"), ("CPU", "Processor Type"),
        ("Model Number", "Model Number"),
        ("Number of Cores", "Processor Core"),
        ("Panel Features", "Panel Features"),
        ("Screen Size", "Screen Size"),
        ("Display Resolution", "Display Resolution"),
        ("Display Type", "Display Type"),
        ("Graphics", "Graphics Card"), ("Graphics Card", "Graphics Card"),
    ):
        if raw.get(src) and dst not in out:
            out[dst] = str(raw[src])
    return out


def is_laptop(hit: dict) -> bool:
    name = hit.get("name") or ""
    if not LAPTOP_RE.search(name) or ACCESSORY_RE.search(name):
        return False
    specs = hit.get("plp_specifications") or {}
    machine_signal = CPU_RE.search(name) or specs.get("Operating System") or specs.get("RAM Size")
    spec_signal = specs.get("RAM Size") or specs.get("Internal Memory") or RAM_IN_NAME_RE.search(name)
    return bool(machine_signal and spec_signal)


def to_record(hit: dict) -> dict:
    list_price = hit.get("price")
    sale = hit.get("sale_price")
    price = sale if sale else list_price
    if not price:
        return {}
    was = list_price if (sale and list_price and list_price > sale) else None
    seller = (hit.get("store_name") or "noon").strip()
    images = [u for u in (hit.get("image_urls") or []) if u]
    if not images and hit.get("image_url"):
        images = [hit["image_url"]]
    rating = hit.get("product_rating") or {}
    seller_lab = hit.get("partner_ratings_sellerlab") or {}
    pdp = hit.get("pdp_url") or ""
    return {
        "store": "noon",
        "sku": hit.get("sku"),
        "name": hit.get("name"),
        "brand": (hit.get("brand") or "").strip() or None,
        "seller": seller,
        "url": SITE + pdp if pdp.startswith("/") else pdp,
        "images": images,
        "price_sar": round(float(price), 2),
        "was_price_sar": round(float(was), 2) if was else None,
        "discount_pct": (round((1 - price / was) * 100, 1) if was else None),
        "in_stock": bool(hit.get("is_buyable")),
        "delisted": False,
        "stock_note": seller if seller.lower() != "noon" else None,
        "rating": rating.get("value") or None,
        "review_count": rating.get("count") or None,
        "seller_rating": seller_lab.get("partner_rating") or None,
        "seller_reviews": seller_lab.get("num_of_rating") or None,
        "seller_positive_pct": seller_lab.get("positive_seller_rating") or None,
        "flags": hit.get("flags") or [],
        "specs": _specs(hit),
        "short_description": hit.get("name"),
        "listing": {"marketplace": seller.lower() != "noon", "offer_code": hit.get("offer_code"),
                    "catalog_sku": hit.get("catalog_sku")},
    }


def fetch_hits(fetcher: Fetcher, pages: int) -> list[dict]:
    hits: list[dict] = []
    seen: set = set()
    for page in range(1, pages + 1):
        try:
            payload = json.loads(fetcher.get(API.format(page=page)))
        except Exception as err:  # noqa: BLE001
            log.warning("page %s failed: %s", page, err)
            break
        batch = payload.get("hits") or []
        if not batch:
            break
        fresh = [h for h in batch if h.get("sku") not in seen]
        seen.update(h.get("sku") for h in fresh)
        hits.extend(fresh)
        log.info("page %s: %s hits (%s new, total %s)", page, len(batch), len(fresh), len(hits))
        if len(batch) < 50:
            break
        time.sleep(0.2)
    return hits


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
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"API pages of 50 (default {DEFAULT_PAGES})")
    parser.add_argument("--interval", type=float, default=1.0, help="minimum seconds between requests")
    parser.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    parser.add_argument("--max-age", type=float, default=0, metavar="HOURS",
                        help="re-fetch cached responses older than N hours (0 = never expire)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    max_age = args.max_age * 3600 if args.max_age > 0 else None
    fetcher = Fetcher(CACHE_DIR, min_interval=args.interval, refresh=args.refresh,
                      headers=HEADERS, max_age=max_age)

    hits = fetch_hits(fetcher, args.pages)
    laptops = [h for h in hits if is_laptop(h)]
    log.info("%s offers, %s of them laptops", len(hits), len(laptops))

    rows = [to_record(h) for h in laptops]
    rows = [r for r in rows if r]

    # silent quality filter: reseller stock (renewed/used) and machines with a CPU
    # more than three years old never make it into the data files
    kept, dropped, filtered_skus = [], {}, set()
    for rec in rows:
        reason = is_junk(rec)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            filtered_skus.add(rec["sku"])
        else:
            kept.append(rec)
    if dropped:
        log.info("silently dropped %s listings (%s)", sum(dropped.values()),
                 ", ".join(f"{k}: {v}" for k, v in sorted(dropped.items())))
    rows = kept
    rows.sort(key=lambda r: (r.get("price_sar") or 0))
    seen = {r["sku"] for r in rows}

    old = previous_records(DATA_DIR / "noon_laptops.jsonl")
    previous = [r for r in old.values() if not r.get("delisted")]
    if previous and len(rows) < len(previous) * 0.6:
        log.warning("only %s laptops parsed, previous run had %s — keeping the old file",
                    len(rows), len(previous))
        rows = previous + [r for r in old.values() if r.get("delisted")]
    else:
        delisted = []
        for sku, rec in old.items():
            if sku in filtered_skus:
                continue          # dropped by the quality filter, not "delisted"
            if sku not in seen and not rec.get("delisted"):
                rec = dict(rec)
                rec.update({"delisted": True, "in_stock": False, "stock_note": "delisted"})
                delisted.append(rec)
        if delisted:
            log.info("%s listings no longer in the category kept as delisted", len(delisted))
        rows += delisted

    write_jsonl(DATA_DIR / "noon_laptops.jsonl", rows)
    in_stock = [r for r in rows if r.get("in_stock") and not r.get("delisted")]
    write_jsonl(DATA_DIR / "noon_laptops_in_stock.jsonl", in_stock)

    prices = [r["price_sar"] for r in in_stock if r.get("price_sar")]
    sellers = len({r.get("seller") for r in in_stock})
    log.info("wrote %s laptops (%s in stock, %s sellers) · SAR %.0f-%.0f · cash %s / cache %s",
             len(rows), len(in_stock), sellers, min(prices, default=0), max(prices, default=0),
             fetcher.stats["network"], fetcher.stats["cache"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
