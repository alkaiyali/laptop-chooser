"""Cross-store de-duplication.

Jarir and eXtra both carry many of the same machines (MacBooks especially), often at
different prices.  This module groups listings that are *the same configuration* and
folds them into one record with an ``offers`` list, so the chooser shows a single entry
with both store logos and a price comparison.

Matching key
------------
``brand + product line (+ screen size for Apple) + CPU model + RAM + storage``

* product line comes from the store's own ``model`` field (falling back to the title),
  with generic words and the screen-size digit removed — so eXtra's "MacBook Pro 14"
  and Jarir's "MacBook Pro" (14") produce the same line.  Apple part numbers
  (MDHJ4AB/A, MDE64 … — one SKU per colorway) are stripped before tokenizing,
  otherwise Sky Blue and Starlight tokenize as different lines;
* CPU, RAM and storage must all be present on both sides and be identical;
* screen size is required for Apple (Air 13 vs Air 15 must never merge) and used when
  available elsewhere;
* GPU core counts are *not* part of the key — the stores disagree about them (eXtra
  lists "10-Core GPU" for machines whose own title says "16 Core").  For non-Apple
  machines a conflicting GPU model splits the group.

Guard rails
-----------
* clusters with at least two different stores are merged; a single-store
  cluster is also merged when it is the same seller offering the same
  configuration twice (e.g. noon's two `15-fd1037nx` SKUs from Best Buy at
  SAR 2,949, differing only in `was_price`) — otherwise marketplace
  duplicates show as two identical cards.  Different sellers at the same
  store are never merged, so a flagged seller is never hidden behind a
  clean one.  Colorways fold too: same seller, same configuration, one card
  with a listing per color (each Apple color is its own part number, so
  differing codes alone never split a multi-color cluster);
* identical part numbers override title noise: noon's "15-fd1037nx Laptop
  With 15.6 Inch ..." and eXtra's "Hp 15-fd1037nx 24c1 ..." tokenize
  differently (6 from 15.6 vs 7 from Ultra 7) but carry the same
  `model_code`, so they are treated as line-compatible;
* within a cluster, a listing more than 1.35x the cheapest *of its own store* (or 1.3x
  the cluster cheapest in the same country, 2.2x across countries) is treated as a
  different configuration and left alone — that keeps mis-parsed outliers (e.g. a
  6,599 Jarir Nitro V next to a 3,799 one) and different chassis/GPU variants apart;
* the cheapest in-stock listing becomes the primary record; the others get
  ``merged_into`` and are hidden by default in the viewer.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------- helpers

_NOISE = {
    "laptop", "laptops", "notebook", "notebooks", "clamshell", "ai", "aipc", "pc",
    "gaming", "game", "convertible", "detachable", "2in1", "2", "in", "1", "touch",
    "touchscreen", "screen", "display", "windows", "win", "dos", "home", "pro",
    "professional", "with", "and", "the", "intel", "amd", "apple", "processor", "cpu",
    "ssd", "hdd", "ram", "ddr4", "ddr5", "gb", "tb", "inch", "inches", "inchs", "gen",
    "generation", "series", "edition", "new", "latest", "ultra", "core", "ryzen",
    "qualcomm", "snapdragon", "mediatek", "celeron", "pentium", "athlon", "ai9", "ai7",
    "ai5", "arc", "radeon", "geforce", "graphics", "gpu", "wifi", "bluetooth", "fhd",
    "uhd", "qhd", "hd", "ips", "oled", "lcd", "touchpad", "backlit", "keyboard",
    "warranty", "year", "years", "black", "silver", "grey", "gray", "white", "blue",
    "indigo", "blush", "citrus", "midnight", "starlight", "space", "natural", "mica",
    "glacier", "cool", "solar", "luna", "carbon", "steel", "shadow", "titanium",
    # wording that long marketplace / bundle titles leak into the name
    "integrated", "dedicated", "without", "office", "basic", "everyday", "365",
    "backpack", "briefcase", "sleeve", "ems", "em", "tn", "wuxga", "fhd", "hd",
    "touch", "touchscreen", "convertible", "flip", "x360", "free", "freedos",
    "arctic", "luna", "platinum", "urban", "sky", "moonlight", "mist", "nebula",
    # spec words noon titles append ("M5 Chip", "macOS", "English/Arabic")
    "chip", "macos", "english", "arabic",
}
# only "pro" is dropped when it is the whole difference between cheap consumer lines;
# keep it for MacBook/ThinkPad/Yoga/Legion where it is meaningful
_KEEP_PRO = {"macbook", "thinkpad", "yoga", "legion", "proart", "probook", "zbook", "ideapad"}

# short chassis codes that are worth keeping in the line: x14, s16, e14, t14, g1i, x1607ca,
# irh10 ... but not long spec/model strings (15amn8, 82xq01fqph, x1504va-bq4472wsm)
_CODE_TOKEN = re.compile(r"^[a-z]{1,3}\d{1,4}[a-z]{0,3}$")

_BRAND_WORDS = {"asus", "hp", "hewlett", "packard", "lenovo", "acer", "msi", "dell",
                "microsoft", "samsung", "huawei", "honor", "xiaomi", "razer", "lg",
                "framework", "infiniarc", "tecno", "toshiba", "gigabyte"}


def _size_bucket(rec: dict) -> int | None:
    inches = (rec.get("screen") or {}).get("inches")
    if not inches:
        return None
    return int(round(float(inches)))


def _norm_code(code: str | None) -> str | None:
    """Normalized part number: 15-FD1037NX == 15fd1037nx."""
    if not code:
        return None
    norm = re.sub(r"[^a-z0-9]", "", str(code).lower())
    return norm or None


def _codes_match(a: dict, b: dict) -> bool:
    ca, cb = _norm_code(a.get("model_code")), _norm_code(b.get("model_code"))
    return bool(ca and cb and ca == cb)


def _norm_seller(rec: dict) -> str:
    return (rec.get("seller") or "").strip().lower()


def _same_store_compatible(cl: list[dict]) -> bool:
    """True when a single-store cluster is safe to fold.

    Same-store listings hide behind one card with no per-seller offers
    block, so only fold when nothing buyer-relevant differs: same seller,
    same OS and same RAM generation. Part numbers may differ across
    colorways (each Apple color is its own SKU: Midnight MDHF4 vs Sky Blue
    MDHJ4), so multiple codes fold as long as the cluster spans several
    colors — a code difference with no color difference stays split.
    """
    codes = {_norm_code(r.get("model_code")) for r in cl} - {None}
    if len(codes) > 1:
        colors = {(r.get("color") or "").strip().lower() for r in cl} - {""}
        if len(colors) < 2:
            return False
    oss = {(r.get("os") or None) for r in cl}
    if len(oss) > 1:
        return False
    rams = {(r.get("ram_type") or None) for r in cl} - {None}
    if len(rams) > 1:
        return False
    return True


def _line_compatible(a: tuple[str, ...] | None, b: tuple[str, ...] | None) -> bool:
    """True when the shorter product line is a prefix of the longer one.

    Long marketplace titles append the whole spec sheet to the family
    (``ideapad slim 3 amn 8 6 5 integrated without arctic``), so exact tuple
    equality is hopeless — the family (the first tokens) is what matters:
    'ideapad slim 3' vs 'ideapad slim 3 amn …' match, 'ideapad 5' vs 'ideapad
    slim 3' do not.
    """
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return all(x == y for x, y in zip(short, long_[:len(short)]))


# Apple part numbers (MDHJ4AB/A, MDH84, MDE64, MX2J3AB/A, MGDR4ABA …): each
# colorway is its own SKU, so the code fragments must not leak into the
# product line — otherwise Sky Blue (mdhj) and Starlight (mdhc) tokenize as
# different lines and refuse to fold. The M-chip itself (M5, M5 Pro) never
# matches: it carries no letters between M and the digit.
_APPLE_CODE = re.compile(r"\bM[A-Z]{1,4}\d[A-Z0-9]*(?:/[A-Z])?\b", re.I)


def _line_tokens(rec: dict) -> tuple[str, ...] | None:
    """Normalized product line, e.g. 'macbook pro', 'ideapad slim 5', 'nitro v'.

    Only the family words plus short chassis codes survive: model codes and the spec
    salad long marketplace titles carry (``15amn8``, ``82xq01fqph``, ``1920``, ``512``)
    are dropped so the same machine from two stores produces the same line.
    """
    size = _size_bucket(rec)
    for src in (rec.get("model"), rec.get("name")):
        if not src:
            continue
        src = _APPLE_CODE.sub(" ", str(src))
        words = re.findall(r"[a-z]+|\d+", src.lower())
        toks: list[str] = []
        for w in words:
            if w in _NOISE or w in _BRAND_WORDS:
                continue
            if w == "pro" and not (set(toks) & _KEEP_PRO):
                continue          # generic "pro" wording in consumer titles
            if w.isdigit():
                if 11 <= int(w) <= 18 or (size and int(w) == size):
                    continue      # the screen size, not a model number
                if not 2 <= int(w) <= 9:
                    continue      # spec leftovers (512, 1920, 2024 ...)
            elif any(ch.isdigit() for ch in w):
                if not _CODE_TOKEN.match(w):
                    continue      # long model codes / spec strings
            elif len(w) < 2:
                continue
            toks.append(w)
        if toks:
            return tuple(dict.fromkeys(toks))
    return None


def _chip_key(rec: dict) -> str | None:
    cpu = rec.get("cpu") or {}
    model = (cpu.get("model") or "").strip().lower()
    if not model or model in {"integrated", "unknown"}:
        return None
    model = re.sub(r"\b(intel|amd|apple|qualcomm|snapdragon)\b", " ", model)
    model = re.sub(r"\s+", " ", model).strip()
    return model or None


def _gpu_tokens(rec: dict) -> str | None:
    """Coarse GPU signature: rtx:5050, radeon:610, cores:16, uhd …

    The stores phrase the same GPU differently (“Radeon 610M Graphics” vs “Radeon
    610M”, “10-Core GPU” vs “10-core GPU”), so compare the meaningful bit only.
    """
    model = ((rec.get("gpu") or {}).get("model") or "").strip().lower()
    if not model:
        return None
    m = re.search(r"\b(rtx|gtx)\s?(\d{3,4})", model)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"(\d+)\s*-?\s*core", model)
    if m:
        return f"cores:{m.group(1)}"
    m = re.search(r"(?:radeon|rx)\s*(?:rx\s*)?(\d{3,4})", model)
    if m:
        return f"radeon:{m.group(1)}"
    for brand in ("arc", "iris", "uhd", "adreno", "mali", "qualcomm", "apple"):
        if brand in model:
            return brand
    words = [w for w in re.split(r"\W+", model)
             if w and w not in {"graphics", "gpu", "integrated", "dedicated", "amd", "intel", "nvidia"}]
    return " ".join(words) or None


def config_key(rec: dict) -> tuple | None:
    """Identity of a configuration, or None when it cannot be matched safely."""
    ram, storage = rec.get("ram"), rec.get("storage")
    chip = _chip_key(rec)
    if not (ram and storage and chip):
        return None
    brand = (rec.get("brand") or "").strip().lower()
    size = _size_bucket(rec)
    apple = "apple" in brand or "macbook" in ((rec.get("model") or "") + " " + (rec.get("name") or "")).lower()
    if apple and size is None:
        return None                       # 13" vs 15" Air must not merge
    return (brand, chip, int(ram), int(storage), size)


# ------------------------------------------------------------------- grouping

def _cluster(group: list[dict]) -> list[list[dict]]:
    """Split a same-key group into price-consistent clusters."""
    ranked = sorted(group, key=lambda r: (r.get("price") or 0))
    clusters: list[list[dict]] = []
    for rec in ranked:
        price = rec.get("price") or 0
        placed = False
        for cl in clusters:
            if not _line_compatible(_line_tokens(rec), _line_tokens(cl[0])):
                # Same part number overrides title noise: noon's "15-fd1037nx
                # Laptop With 15.6 Inch ..." vs eXtra's "Hp 15-fd1037nx 24c1 ..."
                # tokenize differently but are the same machine.
                if not _codes_match(rec, cl[0]):
                    continue
            cheap = min(x.get("price") or 0 for x in cl)
            # prices differ more between countries (shipping, taxes, warranty);
            # inside one country the same configuration should sit close together
            cross_country = any(x.get("currency") != rec.get("currency") for x in cl)
            ceiling = cheap * (2.2 if cross_country else 1.3)
            if price > ceiling:
                continue
            same_store = [x for x in cl if x["store"] == rec["store"]]
            if same_store and price > min(x.get("price") or 0 for x in same_store) * 1.35:
                continue
            # non-Apple GPUs that disagree mean different configurations
            if "apple" not in (rec.get("brand") or "").lower():
                mine, theirs = _gpu_tokens(rec), {_gpu_tokens(x) for x in cl}
                theirs.discard(None)
                if mine and theirs and mine not in theirs:
                    continue
            cl.append(rec)
            placed = True
            break
        if not placed:
            clusters.append([rec])
    return clusters


def apply_dedupe(records: list[dict]) -> dict:
    """Fold identical listings (cross-store + same-seller duplicates)."""
    for rec in records:                      # reset, in case of re-runs
        rec.pop("offers", None)
        rec.pop("merged_into", None)
        rec.pop("dup_count", None)
        rec.pop("dup_max", None)

    groups: dict[tuple, list[dict]] = {}
    for rec in records:
        key = config_key(rec)
        if key:
            groups.setdefault(key, []).append(rec)

    merged = 0
    offers_total = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        for cl in _cluster(group):
            stores = {r["store"] for r in cl}
            if len(stores) < 2:
                # Same-store duplicate? Only fold when it is the same seller
                # offering the same thing twice — never hide a competing
                # seller behind another one, nor a different OS / RAM /
                # part number (see _same_store_compatible).
                sellers = {_norm_seller(r) for r in cl}
                if len(cl) < 2 or len(sellers) != 1:
                    continue
                if not _same_store_compatible(cl):
                    continue
                if not any(_codes_match(a, b) for i, a in enumerate(cl) for b in cl[i + 1:]):
                    # without a shared part number, require line compatibility
                    # across the whole cluster (already pairwise-checked in
                    # _cluster, re-check against the first entry)
                    if not all(_line_compatible(_line_tokens(r), _line_tokens(cl[0])) for r in cl[1:]):
                        continue
            ranked = sorted(cl, key=lambda r: (
                not (r.get("in_stock") and not r.get("delisted")),   # in stock first
                (r.get("price") if r.get("price") is not None else 9e9),
                # same price: prefer the less-inflated anchor (WYSECHY-style
                # 4,899 anchors) then the more-reviewed listing
                (r.get("was_price") if r.get("was_price") is not None else 9e9),
                -(r.get("review_count") or 0),
            ))
            primary = ranked[0]
            others = ranked[1:]
            by_store: dict[str, list[dict]] = {}
            for r in cl:
                by_store.setdefault(r["store"], []).append(r)
            offers = []
            for store, rows in by_store.items():
                rows.sort(key=lambda r: ((r.get("price") or 9e9),
                                         (r.get("was_price") if r.get("was_price") is not None else 9e9),
                                         -(r.get("review_count") or 0)))
                # Deduplicate identical listings within the store:
                deduped_rows = []
                seen_variants = set()
                for r in rows:
                    vkey = (
                        r.get("price"),
                        _norm_seller(r),
                        (r.get("color") or "").strip().lower(),
                        bool(r.get("in_stock") and not r.get("delisted")),
                    )
                    if vkey not in seen_variants:
                        seen_variants.add(vkey)
                        deduped_rows.append(r)
                rows = deduped_rows
                best = rows[0]
                offers.append({
                    "store": store, "sku": best["sku"], "price": best.get("price"),
                    "was_price": best.get("was_price"), "url": best.get("url"),
                    "price_local": best.get("price_local"), "currency": best.get("currency"),
                    "in_stock": bool(best.get("in_stock") and not best.get("delisted")),
                    "stock_note": best.get("stock_note"), "variants": len(rows),
                    "seller": best.get("seller"),
                    "marketplace": best.get("marketplace"),
                    "os": best.get("os"),
                    "seller_note": best.get("seller_note"),
                    "seller_issue_pct": best.get("seller_issue_pct"),
                    "seller_positive_pct": best.get("seller_positive_pct"),
                    "seller_reviews": best.get("seller_reviews"),
                    "colors": sorted({v.get("color") for v in rows if v.get("color")}),
                    "listings": [
                        {"sku": v.get("sku"), "price": v.get("price"), "url": v.get("url"),
                          "in_stock": bool(v.get("in_stock") and not v.get("delisted")),
                          "color": v.get("color"), "seller": v.get("seller"),
                          "name": v.get("name")}
                        for v in sorted(rows, key=lambda r: (r.get("price") or 9e9))
                    ],
                })
            offers.sort(key=lambda o: o["price"] or 9e9)
            prices = [o["price"] for o in offers if o["price"]]
            primary["offers"] = offers
            primary["dup_count"] = len(cl)
            primary["dup_max"] = max(prices) - min(prices) if len(prices) > 1 else 0
            for other in others:
                other["merged_into"] = primary["id"]
            merged += len(others)
            offers_total += 1
    return {"groups": offers_total, "merged_records": merged,
            "primary_records": len(records) - merged, "total_records": len(records)}


if __name__ == "__main__":  # audit: list every merged group
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "data" / "unified_laptops.json"
    recs = json.loads(path.read_text(encoding="utf-8"))
    stats = apply_dedupe(recs)
    print(stats)
    print()
    for rec in recs:
        if not rec.get("offers"):
            continue
        price = {o["store"]: o["price"] for o in rec["offers"]}
        print(f"{rec['brand']:9s} {(rec['model'] or rec['name'])[:34]:36s} "
              f"chip={rec['cpu']['model']:<22s} {rec['ram']}GB/{rec['storage']}GB "
              f"{rec['screen']['inches']}\"  " + "  ".join(f"{k} {v:,.0f}" for k, v in price.items()))
