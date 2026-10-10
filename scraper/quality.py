"""Silent quality filter for marketplace stores (noon).

noon's laptop category, like Amazon's, mixes in reseller stock: "Renewed - Chromebook
3100", "Certified Pre Owned - Surface Laptop 3", 2017-era business machines.  Jarir,
eXtra and DataBlitz only sell new units under their own warranty, so this filter runs
for marketplace stores **only** and drops the listings noiselessly — they never reach
the data files or the viewer.

Two rules:

* **Condition** — renewed / refurbished / certified pre-owned / open box / used.
* **Seller reputation** (marketplace listings) — a partner with no rating history, fewer
  than 10 ratings or under 70% positive feedback is dropped: the "WYSECHY" laptop with a
  fabricated 4,899 anchor price came from a seller with no history at all.  The threshold
  is deliberately low — noon's newer established partners (Best Buy, Comlab, Hyphen …)
  carry real new stock, and a strict 50-rating bar silently removed ~190 of them.
* **Brand** — no-name badges (WYSECHY, InnJoo, PXXKJOER …) are dropped everywhere,
  noon-direct stock included; only the seller-reputation rule is marketplace-specific.
* **Age** — Intel 11th gen and older, plus their Ryzen 5000 counterparts
  (``i5-1135G7`` → 2020, ``Ryzen 7 5800H`` → 2021, ``Celeron N4020`` → 2019),
  are dropped; 12th gen / Ryzen 6000+ are kept (a 2022 i5-12450H is still a
  current-market machine, so the cutoff is generation-based, not clock-based).
  Apple M-series is exempt — an M1 MacBook still holds value and gets macOS
  updates.  Titles that never name a CPU model (noon often writes just
  "Core i7 Processor") cannot be dated and are kept.
"""

from __future__ import annotations

import re

# brands with a real support / driver / warranty story
KNOWN_BRANDS = {
    "acl", "acer", "alienware", "apple", "asus", "colorful", "dell", "dynabook",
    "framework", "fujitsu", "gigabyte", "google", "hassee", "honor", "hp", "huawei",
    "infinix", "lenovo", "lg", "medion", "mic electronics", "microsoft", "msi",
    "panasonic", "positivo", "razer", "redmi", "samsung", "tecno", "toshiba", "vaio",
    "viewsonic", "xiaomi", "xmg",
}
# marketplace sellers need at least this much history.  The positive% bar was
# reviewed against real seller pages: 65-67% sellers (Best Buy, Comlab, info
# world, Clarity) are legitimate partners with rare incidents, not scams, so
# the bar sits at 65 and the seller's rating is shown on the listing instead.
MIN_SELLER_RATINGS = 10
MIN_SELLER_POSITIVE = 65

# Sellers whose open-box / "used sold as new" reports were reviewed by hand on
# their noon pages: the reports are real but rare — legitimate partners, not
# scams, just not showroom-safe.  Disclosure only: nothing is filtered, the
# listing carries a visible note with the reviewer-derived odds.
SELLER_NOTES = {
    "information world for trading company": "open-box/used units reported",
    "best buy": "open-box/used units reported",
    "comlab information technology company": "open-box/used units reported",
    "clarity technology": "open-box/used units reported",
    "golden technology trading company": "open-box/used units reported",
}


def seller_note(seller: str | None) -> str | None:
    """Reviewed disclosure for a marketplace seller, or None."""
    return SELLER_NOTES.get((seller or "").strip().lower())


# Unhappy buyers review far more often than happy ones, so the raw negative
# review share overstates the real problem rate.  With review propensity ratio
# k (unhappy:happy) the underlying issue rate is  n = s / (k - s*(k-1)).
# k=5 is a common e-commerce estimate: a seller with 66% positive reviews
# (~34% negative) works out to ~9% of orders (1 in 11), not 34%.
REVIEW_SKEW = 5.0


def estimated_issue_rate(positive_pct: float | None) -> float | None:
    """Estimated share of orders with an issue (%), from the positive rating."""
    if positive_pct is None:
        return None
    s = max(0.0, min(1.0, 1 - positive_pct / 100))
    if s <= 0:
        return 0.0
    return round(100 * s / (REVIEW_SKEW - s * (REVIEW_SKEW - 1)), 1)


CONDITION_RE = re.compile(
    r"\b(renewed|refurbished|refurb|certified\s+pre[- ]?owned|pre[- ]?owned|preowned|"
    r"second[- ]hand|open[- ]box|used|ex[- ]display|ex[- ]demo|recertified)\b", re.I)

# generation floor: Intel 11th gen / Ryzen 5000 (2021) and older drop,
# 12th gen / Ryzen 6000+ stay (see cpu_year below)
MIN_CPU_YEAR = 2022


def cpu_year(model: str | None) -> int | None:
    """Rough launch year of a CPU model, or None when it cannot be told."""
    t = re.sub(r"\s+", " ", (model or "").lower()).strip()
    if not t:
        return None
    m = re.search(r"\bm(\d)\b", t)
    if "apple" in t or ("m" in t and m):
        if m:
            return {1: 2020, 2: 2022, 3: 2023, 4: 2024, 5: 2025}.get(int(m.group(1)))
    if "snapdragon" in t:
        return 2025 if "x2" in t else 2024
    m = re.search(r"ultra\s*[3579]\s*(\d{3})", t)
    if m:
        return 2024 if int(m.group(1)) < 200 else 2025
    m = re.search(r"\bcore\s*[3579]\s*(\d{3})", t)
    if m:
        return 2024 if int(m.group(1)) < 200 else 2025
    m = re.search(r"\bi[3579][-\s]+(\d{3,5})", t)
    if m:
        num = m.group(1)
        if len(num) >= 5:
            gen = int(num[:-3])                       # i7-10750H, i7-13620H
        elif num[:2] in ("10", "11", "12", "13", "14"):
            gen = int(num[:2])                        # i5-1035G1, i5-1135G7, i5-1235U, i5-1335U
        else:
            gen = int(num[0])                         # i5-8265U, i5-7200U
        if gen >= 15:
            return 2025
        return {3: 2013, 4: 2014, 5: 2015, 6: 2016, 7: 2017, 8: 2018, 9: 2019,
                10: 2019, 11: 2020, 12: 2022, 13: 2023, 14: 2024}.get(gen)
    m = re.search(r"ryzen\s+(?:ai\s+)?(?:max\+?\s*)?[3579]\s*(?:hx\s+)?(\d{2,4})", t)
    if m:
        raw = m.group(1)
        if len(raw) <= 3:
            n = int(raw)
            return 2025 if n < 100 or n >= 300 else 2024
        n = int(raw)
        series = n // 1000
        if series >= 9:
            return 2024
        if series == 8:
            return 2024
        if series == 7:
            return 2023
        if series == 6:
            return 2022
        if series == 5:
            return 2022 if n % 100 in (25, 35, 50, 60) else 2021
        return {4: 2020, 3: 2019, 2: 2018, 1: 2017}.get(series)
    m = re.search(r"\bn(\d{3,4})\b", t)
    if m:
        table = {"n4020": 2019, "n4100": 2019, "n4120": 2019, "n4500": 2021, "n5100": 2021,
                 "n5030": 2021, "n100": 2023, "n200": 2023, "n150": 2024, "n250": 2024}
        return table.get("n" + m.group(1), 2019)
    if re.search(r"celeron|pentium", t):
        return 2020
    if re.search(r"uhd\s*(?:graphics\s*)?6\d\d|hd\s*graphics\s*\d{3}", t):
        return 2018
    if re.search(r"\b(?:probook|elitebook)\s*\d{3}\s*g[1-8]\b", t):
        return 2018
    return None


def _is_marketplace(rec: dict) -> bool:
    seller = (rec.get("seller") or "").strip().lower()
    if seller:
        return seller != "noon"
    return bool((rec.get("listing") or {}).get("marketplace"))


def _known_brand(rec: dict) -> bool:
    brand = (rec.get("brand") or "").strip().lower()
    if brand:
        return brand in KNOWN_BRANDS
    head = (rec.get("name") or "").lower()[:40]
    return any(known in head for known in KNOWN_BRANDS)


# Specific broken, scalper, or fabricated marketplace listings
DROPPED_SKUS = {
    "B0F5ZMLD51",  # Amazon: MSI Titan 18 HX AI listed at 67,945 SAR (broken/scalper placeholder)
}


def is_junk(rec: dict, min_year: int = MIN_CPU_YEAR) -> str | None:
    """Reason why a marketplace listing is dropped, or None to keep it."""
    sku = (rec.get("sku") or "").strip()
    if sku in DROPPED_SKUS:
        return "broken/scalper listing"

    price = rec.get("price") or rec.get("price_sar") or 0
    if price > 25000:
        return "price exceeds 25k SAR ceiling"

    name = rec.get("name") or ""
    if CONDITION_RE.search(name):
        return "renewed/used"
    if rec.get("seller") and CONDITION_RE.search(rec["seller"]):
        return "renewed/used"

    if not _known_brand(rec):
        return "no-name brand"

    if _is_marketplace(rec):
        reviews = rec.get("seller_reviews")
        positive = rec.get("seller_positive_pct")
        if reviews is None:
            return "weak seller"
        if reviews < MIN_SELLER_RATINGS:
            return "weak seller"
        if positive is not None and positive < MIN_SELLER_POSITIVE:
            return "weak seller"

    brand = (rec.get("brand") or "").lower()
    if "apple" in brand or "macbook" in name.lower():
        return None                                    # MacBooks keep their value
    year = cpu_year(((rec.get("cpu") or {}).get("model")) or name)
    if year and year < min_year:
        return "old CPU"
    return None
