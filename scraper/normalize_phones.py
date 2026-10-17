#!/usr/bin/env python3
"""Normalize the Jarir + eXtra + GreenTelcom + Ichiban in-stock phone datasets into one comparable schema.

Reads:   data/jarir_phones_in_stock.jsonl (Jarir smartphones)
         data/extra_phones_in_stock.jsonl (eXtra Smartphone sub-family)
         data/greentelcom_phones_in_stock.jsonl (GreenTelcom Smartphones, PHP)
         data/ichiban_phones_in_stock.jsonl (Ichiban Tekno Smartphones, PHP)
Writes:  data/unified_phones.json       (source of truth for the phone chooser)
         data/unified_phones.csv
         viewer/phones.js               (window.PHONES = [...]; works over file://)

Normalized fields: soc{text,vendor,model,score}, ram, storage, display{inches,
type}, battery_mah, camera{rear_mp,front_mp}, network, os, plus perf_score and
value_score (overall per SAR with a 2500 SAR calibration floor so the median
phone reads "fair" on the same value bands as laptops).

Conscious v1 limits (documented, not hidden): no price history yet (each run
rewrites the snapshot; history starts when this ships), no cross-run
tombstones, no peer-price sanity model. Dedupe folds identical configs
(brand + SoC + RAM + storage) across the two stores into one card.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA_DIR, log  # noqa: E402
from phone_benchmarks import lookup as lookup_soc, scale as scale_soc  # noqa: E402
from phone_cameras import lookup as lookup_sensor, sensor_points  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = ROOT / "viewer"

VALUE_FLOOR_SAR = 2500.0
# The floor is a regularization constant, not a physical cost: it is set so
# the median phone reads "fair" (20-28) on the same value bands as laptops,
# keeping value scores comparable across the two catalogs.

JUNK = {"no", "n/a", "none", "not specified", "-", "", "nan"}


def _first_text(rec: dict, *labels: str) -> str | None:
    lower = {k.lower(): k for k in (rec.get("specs") or {})}
    for label in labels:
        key = lower.get(label.lower())
        if key is not None:
            value = (rec.get("specs") or {})[key]
            if isinstance(value, str) and value.strip() and value.strip().lower() not in JUNK:
                return re.sub(r"\s+", " ", value.strip())
    return None


def _all_specs(rec: dict) -> str:
    parts = [rec.get("name") or "", rec.get("short_description") or "", rec.get("description") or ""]
    parts += [str(v) for v in (rec.get("specs") or {}).values()]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# field parsers
# ---------------------------------------------------------------------------

def parse_soc(rec: dict) -> dict:
    candidates = [
        _first_text(rec, "chipset", "Chipset", "Processor", "Processor Core",
                    "Processor Type", "Processor Model", "Chips", "Chip"),
        # bare CPU rows ("Octa Core CPU") only ever back up a failed Chipset:
        # a modem fragment ("X70") must not shadow "Snapdragon 8 Gen 2"
        _first_text(rec, "CPU"),
    ]
    texts = [re.sub(r"\s+", " ", t).strip() for t in candidates if t]
    text = texts[0] if texts else ""
    out = {"text": text or None, "vendor": None, "model": None,
           "score": None, "benchmark": None}
    if not text:
        return out
    low = text.lower()
    if "apple" in low or re.search(r"\bA1[5-9]\b|\bA20\b", text):
        out["vendor"] = "Apple"
    elif "snapdragon" in low or "qualcomm" in low:
        out["vendor"] = "Qualcomm"
    elif "dimensity" in low or "helio" in low or "mediatek" in low:
        out["vendor"] = "MediaTek"
    elif "exynos" in low:
        out["vendor"] = "Samsung"
    elif "tensor" in low or "google" in low:
        out["vendor"] = "Google"
    elif "unisoc" in low or "tiger" in low:
        out["vendor"] = "Unisoc"
    elif "kirin" in low:
        out["vendor"] = "Huawei"
    mark, name, mapping, used = None, None, None, text
    for cand in texts:
        mark, name, mapping = lookup_soc(cand)
        if mark:
            used = cand
            break
    out["benchmark"] = ({"source": "antutu_v10", "mark": mark, "name": name,
                         "mapping": mapping} if mark else None)
    out["score"] = scale_soc(mark)
    out["model"] = name
    if mark:
        # the vendor follows the winning text (a modem fragment up front
        # must not label the record)
        low = used.lower()
        for key, vendor in (("apple", "Apple"), ("snapdragon", "Qualcomm"),
                            ("qualcomm", "Qualcomm"), ("dimensity", "MediaTek"),
                            ("helio", "MediaTek"), ("mediatek", "MediaTek"),
                            ("exynos", "Samsung"), ("tensor", "Google"),
                            ("unisoc", "Unisoc"), ("tiger", "Unisoc"),
                            ("kirin", "Huawei")):
            if key in low:
                out["vendor"] = vendor
                break
        if re.search(r"\bA1[5-9]\b|\bA20\b", used):
            out["vendor"] = "Apple"
    return out


def parse_ram(rec: dict) -> int | None:
    for label in ("RAM SIZE", "RAM", "Memory"):
        raw = _first_text(rec, label)
        if not raw:
            continue
        # a "Memory" row describing ROM ("ROM / 256 GB ROM") is storage, not
        # RAM — it must not shadow the fallbacks below
        if re.search(r"ROM", raw, re.I) and not re.search(r"\bRAM\b", raw, re.I):
            continue
        # "12/16 GB" style pairs first: cheapest variant wins
        m = re.search(r"(\d{1,3})\s*/\s*\d+\s*(GB|TB)", raw, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d{1,3})\s*(GB|TB)", raw, re.I)
        if m:
            gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
            if gb <= 32:
                return gb
            # implausible for phone RAM (a storage figure in a RAM row):
            # keep looking instead of poisoning the title fallback
            continue
    # title fallback (".../12GB RAM/..."); iPhones never list RAM — stays None
    m = re.search(r"\b(\d{1,2})\s*GB\s*RAM\b", rec.get("name") or "", re.I)
    if m and int(m.group(1)) <= 32:
        return int(m.group(1))
    # GreenTelcom names carry "X – 12GB | 512GB": RAM is the pre-pipe number
    m = re.search(r"\b(\d{1,2})\s*GB\s*[|/]", rec.get("name") or "")
    if m and int(m.group(1)) <= 32:
        return int(m.group(1))
    # any other RAM-worded row ("Expandable RAM Capacity: 6 GB/8 GB"):
    # pair-first ("6+64/..." → 6), else first GB figure
    for key, val in (rec.get("specs") or {}).items():
        if not re.search(r"\bram\b", key, re.I) or not isinstance(val, str):
            continue
        m = (re.search(r"(\d{1,2})\s*(?:GB\s*)?\+\s*\d+", val)
             or re.search(r"(\d{1,3})\s*(GB|TB)", val, re.I))
        if m and int(m.group(1)) <= 32:
            return int(m.group(1))
    return None


def parse_storage(rec: dict) -> int | None:
    for label in ("Memory (internal)", "capacity", "Storage", "Memory",
                  "Total Storage (Capacity)"):
        raw = _first_text(rec, label)
        if not raw:
            continue
        best = None
        for m in re.finditer(r"(\d{1,4}(?:\.\d)?)\s*(GB|TB)", raw, re.I):
            gb = float(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
            if best is None or gb > best:
                best = gb
        if best:
            # a bare "Memory" row holding a RAM-sized figure ("8GB") is the
            # RAM row, not storage — don't let it shadow the title fallback
            if label.lower() == "memory" and best <= 64:
                continue
            return int(best)
    # title fallback: the LAST capacity token ("2TB", "256GB"). Last, because
    # "X – 4GB | 128GB" style names lead with the RAM figure
    hits = list(re.finditer(r"\b(\d{1,4})\s*(GB|TB)\b", rec.get("name") or "", re.I))
    if hits:
        m = hits[-1]
        return int(int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1))
    return None


def _inches(text: str | None) -> float | None:
    """First plausible phone screen size in free text (3.5–8.5″ backstop)."""
    if not text:
        return None
    for m in re.finditer(r"(\d+(?:\.\d+)?)[\s\-‑–—]*(?:\"|″|”|inch)", text, re.I):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 3.5 <= v <= 8.5:
            return v
    return None


def parse_display(rec: dict) -> dict:
    out: dict = {"inches": None, "type": None}
    raw = _first_text(rec, "display size", "Screen Size (inch)", "Screen Size",
                      "Display", "Screen", "Size") or ""
    out["inches"] = _inches(raw)
    if out["inches"] is None:  # title fallback ("6.9 inch")
        out["inches"] = _inches(rec.get("name"))
    if out["inches"] is None:  # jammed spec prose ("Display 6.78-inch ...")
        out["inches"] = _inches(_all_specs(rec))
    dtype = _first_text(rec, "display type", "Screen Type", "Panel") or ""
    if not dtype:
        # bare "Type" rows are panel tech on some templates ("AMOLED") and
        # RAM/storage junk on others ("LPDDR4X RAM + ...") — trust short,
        # digit-free values only
        t = _first_text(rec, "Type") or ""
        if t and len(t) <= 24 and not re.search(r"\d", t):
            dtype = t
    if dtype:
        low = dtype.lower()
        if any(k in low for k in ("amoled", "ltpo", "oled", "super retina xdr")):
            out["type"] = "OLED"
        elif "retina" in low:
            out["type"] = "Retina"
        elif any(k in low for k in ("ips", "lcd", "tft", "pls")):
            out["type"] = "LCD"
        elif len(dtype) <= 32:
            out["type"] = dtype
        # longer free text (full-sentence display blurbs) stays untyped: the
        # inches carry the size points and the type scores neutral
    return out


def _mah_number(text: str) -> list[int]:
    return [int(v.replace(",", "")) for v in re.findall(r"(\d[\d,]{2,5})\s*mAh", text, re.I)]


def _mah_agree(hits: list[int]) -> int | None:
    if not hits:
        return None
    if len(set(hits)) > 1 and max(hits) - min(hits) > 0.05 * max(hits):
        return None  # genuinely ambiguous (multi-battery hybrids)
    # near-agreement (typical vs rated, "7,500mAh ... rated 7320") → first wins
    return hits[0]


def parse_battery(rec: dict) -> int | None:
    raw = (_first_text(rec, "Battery Size (mAH)", "Battery Capacity",
                       "battery capacity", "Battery", "Battery & Charging")
           or "")
    hit = _mah_agree(_mah_number(raw))
    if hit is not None:
        return hit
    # spec tables that file mAh under model codes ("LBU-LX2: 7500mAh"):
    # scan the whole listing with the same agreement rule
    return _mah_agree(_mah_number(_all_specs(rec)))


def _mp(text: str | None) -> float | None:
    if not text:
        return None
    # bare "M" must be uppercase ("50M / 8M"): lowercase "6m" in prose
    # ("immersible up to 6m for 30 min") is meters, not megapixels
    ms = [float(m.group(1)) for m in
          re.finditer(r"(\d{1,3}(?:\.\d)?)\s*(?:/\s*)?(?:[mM][pP]|[mM][eE][gG][aA][pP][iI][xX][eE][lL][sS]?|M\b)", text)]
    return max(ms) if ms else None


def _mp_all(values: list[str | None]) -> float | None:
    best: float | None = None
    for v in values:
        if not isinstance(v, str):
            continue
        hit = _mp(v)
        if hit is not None and (best is None or hit > best):
            best = hit
    return best


def parse_cameras(rec: dict) -> dict:
    specs = rec.get("specs") or {}
    jarir = _first_text(rec, "cam resolution") or ""
    rear_raw, front_raw = jarir, ""
    if "front" in jarir.lower():
        parts = re.split(r"front\s*:", jarir, flags=re.I)
        rear_raw, front_raw = parts[0], parts[1] if len(parts) > 1 else ""
    rear = _mp(rear_raw) or _mp(_first_text(rec, "Rear Camera"))
    if rear is None:
        # spec tables that file MP under model prose ("200.0 M" in a Samsung
        # dump): max over non-front rows so a big selfie cam can't leak in
        rear = _mp_all([v for k, v in specs.items()
                        if "front" not in k.lower()])
    front = _mp(front_raw) or _mp(_first_text(rec, "Front Camera Resolution", "Front Camera"))
    if front is None:
        front = _mp_all([v for k, v in specs.items()
                         if "front" in k.lower()])
    return {"rear_mp": rear, "front_mp": front}


def parse_network(rec: dict) -> str | None:
    net = _first_text(rec, "supported network", "Network", "Cellular",
                      "Cellular Network", "Connectivity & Network") or ""
    if not net:
        return "5G" if re.search(r"\b5G\b", rec.get("name") or "", re.I) else None
    return net


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def ram_points(ram: int | None) -> float:
    if ram is None:
        return 4.0  # neutral: between a 6 GB and an 8 GB config
    return round(6.0 * math.log2(max(ram, 4) / 4.0), 1)


def storage_points(storage: int | None) -> float:
    if not storage:
        return 3.0
    if storage <= 64:
        return 2.0
    if storage <= 128:
        return 4.0
    if storage <= 256:
        return 6.0
    if storage <= 512:
        return 7.5
    return 9.0


def _brightest_f(blob: str) -> float | None:
    """Brightest f-number in free text (f/1.8, ƒ/1.8, F1.8); None when absent."""
    hits = []
    for m in re.finditer(r"[fƒF][/\s]?(\d(?:\.\d+)?)\b", blob):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 0.9 <= v <= 5.6:  # plausible phone apertures; rejects "F2 028" junk
            hits.append(v)
    return min(hits) if hits else None


def _rear_lens_count(specs: dict) -> int:
    """Max MP-figure count in any single camera-labelled spec value."""
    best = 0
    for key, val in (specs or {}).items():
        if not isinstance(val, str) or "cam" not in key.lower():
            continue
        n = len(re.findall(r"\d{1,3}(?:\.\d)?\s*(?:/\s*)?(?:[mM][pP]|[mM][eE][gG][aA][pP][iI][xX][eE][lL][sS]?|M\b)", val))
        best = max(best, n)
    return best


def compute_camera_score(cameras: dict, blob: str = "", specs: dict | None = None) -> float:
    """Heuristic 0-100 camera rating from listing specs (no lab testing).

    MP-anchored so every listing scores, with hardware-feature bonuses where
    the prose names them: main-sensor resolution, sensor model/size
    (``phone_cameras`` lookup), secondary optics (ultrawide / telephoto /
    periscope), optical stabilization, aperture brightness, front camera and
    video ceiling.
    Absent signals score neutral-or-zero (documented per component), never
    punitive: a missing spec row is a missing row, not a bad camera.
    """
    low = blob.lower()
    rear = cameras.get("rear_mp")
    front = cameras.get("front_mp")

    # main rear sensor: MP base 0-40 (median listing is 50MP -> 30) ...
    if rear is None:
        base = 18.0
    elif rear >= 200:
        base = 40.0
    elif rear >= 100:
        base = 36.0
    elif rear >= 64:
        base = 32.0
    elif rear >= 48:
        base = 30.0
    elif rear >= 32:
        base = 26.0
    elif rear >= 24:
        base = 22.0
    elif rear >= 12:
        base = 17.0
    elif rear >= 8:
        base = 11.0
    else:
        base = 7.0
    # ... plus sensor-size bonus 0-6 (1-inch -> 6, 1/1.56" -> 3, unknown -> 2)
    size_in, _sensor_name, _mapping = lookup_sensor(blob)
    main = base + sensor_points(size_in)

    # secondary optics: 0-20
    lenses = 0.0
    if "ultra" in low and "wide" in low:
        lenses += 8.0
    if "periscope" in low:
        lenses += 10.0
    elif "telephoto" in low or "tele " in low or " optical zoom" in low:
        lenses += 8.0
    if "macro" in low or "depth" in low:
        lenses += 2.0
    if _rear_lens_count(specs or {}) >= 3 or "triple" in low or "quad" in low:
        lenses += 2.0
    lenses = min(20.0, lenses)

    # stabilization: 0-8
    ois = 8.0 if ("ois" in low or "optical image stabil" in low) else 0.0

    # aperture brightness: 0-6 (unknown -> 3: nearly all phones are f/1.6-2.0)
    fnum = _brightest_f(blob)
    if fnum is None:
        ap = 3.0
    elif fnum <= 1.6:
        ap = 6.0
    elif fnum <= 1.8:
        ap = 5.0
    elif fnum <= 2.0:
        ap = 4.0
    elif fnum <= 2.2:
        ap = 2.0
    else:
        ap = 1.0

    # front camera: 0-12 (median listing is 12MP -> 7)
    if front is None:
        front_pts = 5.0
    elif front >= 32:
        front_pts = 12.0
    elif front >= 24:
        front_pts = 10.0
    elif front >= 16:
        front_pts = 8.0
    elif front >= 12:
        front_pts = 7.0
    elif front >= 8:
        front_pts = 5.0
    elif front >= 5:
        front_pts = 3.0
    else:
        front_pts = 2.0

    # video ceiling: 0-8
    if re.search(r"8\s*k\b[^.]{0,30}(video|record|fps)|(video|record)[^.]{0,30}8\s*k\b", low):
        vid = 8.0
    elif re.search(r"4\s*k\b[^.]{0,20}60\s*fps|60\s*fps[^.]{0,20}4\s*k\b", low):
        vid = 7.0
    elif re.search(r"\b4\s*k\b", low):
        vid = 5.0
    else:
        vid = 0.0

    return round(min(100.0, main + lenses + ois + ap + front_pts + vid), 1)


def compute_perf(soc: dict, ram: int | None, storage: int | None) -> float | None:
    if soc.get("score") is None:
        return None
    return round(0.75 * soc["score"] + ram_points(ram) + storage_points(storage), 1)


def compute_quality(display: dict, cameras: dict, mah: int | None,
                    storage: int | None, blob: str = "",
                    specs: dict | None = None) -> float:
    dtype = display.get("type") or ""
    if dtype == "OLED":
        disp = 30.0
    elif dtype == "Retina":
        disp = 26.0
    elif dtype == "LCD":
        disp = 20.0
    else:
        disp = 20.0
    inches = display.get("inches") or 0
    disp += 3.0 if inches >= 6.7 else (2.0 if inches >= 6.3 else 0.0)
    disp = min(35.0, disp)

    cam = compute_camera_score(cameras, blob, specs) / 4.0  # 0-100 -> 25 pts

    batt = min(25.0, max(0.0, (mah - 3000) / 3000 * 25.0)) if mah else 12.0

    if not storage:
        stor = 8.0
    elif storage <= 64:
        stor = 5.0
    elif storage <= 128:
        stor = 8.0
    elif storage <= 256:
        stor = 11.0
    elif storage <= 512:
        stor = 13.0
    else:
        stor = 15.0
    return round(min(100.0, disp + cam + batt + stor), 1)


def compute_value(overall: float | None, price: float | None) -> float | None:
    if overall is None or not price:
        return None
    return round((overall / (price + VALUE_FLOOR_SAR)) * 2000, 2)


# iPhones never list RAM in any store spec table; these are fixed-SKU
# hardware facts (Apple tech specs + teardowns), longest-prefix match on the
# product-line words. Every other brand publishes RAM per listing.
KNOWN_RAM: dict[tuple, int] = {
    ("iphone", "18", "pro", "max"): 12,
    ("iphone", "18", "pro"): 12,
    ("iphone", "18",): 12,
    ("iphone", "17", "pro", "max"): 12,
    ("iphone", "17", "pro"): 12,
    ("iphone", "17", "air"): 12,
    ("iphone", "17",): 8,
    ("iphone", "16", "pro", "max"): 8,
    ("iphone", "16", "pro"): 8,
    ("iphone", "16e",): 8,
    ("iphone", "16",): 8,
    ("iphone", "15", "pro", "max"): 8,
    ("iphone", "15", "pro"): 8,
    ("iphone", "15",): 6,
    ("iphone", "14",): 6,
    ("iphone", "13",): 4,
}


def raw_line_words(name: str | None, brand_raw: str | None) -> tuple:
    """Product-line words from a raw listing title ('Apple iPhone 17 Pro Max,
    5G, ...' -> ('iphone','17','pro','max')). Leading Renewed/Refurbished
    markers are stripped so fixed-SKU lookups still match."""
    head = re.sub(r"\s+", " ", (name or "").split(",")[0].strip())
    # strip refurb markers + brand repeatedly: "Renewed Grade B Apple iPhone 13"
    # needs renewed -> grade -> brand passes to reach the line words
    for _ in range(3):
        new = re.sub(r"^(renewed|refurbished|pre-owned|used|grade\s+[a-c])\b[\s\-–—]*",
                     "", head, flags=re.I)
        if brand_raw:
            new = re.sub(rf"^{re.escape(brand_raw.strip())}[\s\-–—]*", "", new, flags=re.I)
        if new == head:
            break
        head = new
    # 4G/5G stay: they split modem variants of one line (A07 vs A07 5G are
    # different chips), and fixed-SKU lookups match on shorter prefixes anyway
    words = re.findall(r"[a-z0-9]+", head.lower())
    return tuple(words[:4])


KNOWN_SOC: dict[tuple, str] = {
    ("iphone", "18", "pro", "max"): "Apple A20 Pro",
    ("iphone", "18", "pro"): "Apple A20 Pro",
    ("iphone", "18",): "Apple A20 Pro",
    ("iphone", "17", "pro", "max"): "Apple A19 Pro",
    ("iphone", "17", "pro"): "Apple A19 Pro",
    ("iphone", "17", "air"): "Apple A19 Pro",
    ("iphone", "17",): "Apple A19",
    ("iphone", "16", "pro", "max"): "Apple A18 Pro",
    ("iphone", "16", "pro"): "Apple A18 Pro",
    ("iphone", "16e",): "Apple A18",
    ("iphone", "16",): "Apple A18",
    ("iphone", "15", "pro", "max"): "Apple A17 Pro",
    ("iphone", "15", "pro"): "Apple A17 Pro",
    ("iphone", "15",): "Apple A16 Bionic",
    ("iphone", "14",): "Apple A15 Bionic",
    ("iphone", "13",): "Apple A15 Bionic",
    # Samsung flagships: fixed silicon per generation in this region (S25 is
    # Snapdragon worldwide; KSA S24 units are Exynos except the Ultra; the
    # S26 Ultra follows the Ultra-gets-Snapdragon pattern)
    ("galaxy", "s26", "ultra"): "Snapdragon 8 Elite Gen 5",
    ("galaxy", "s26",): "Exynos 2600",
    ("galaxy", "s25", "ultra"): "Snapdragon 8 Elite",
    ("galaxy", "s25", "fe"): "Exynos 2400",
    ("galaxy", "s25",): "Snapdragon 8 Elite",
    ("galaxy", "s24", "ultra"): "Snapdragon 8 Gen 3",
    ("galaxy", "s24",): "Exynos 2400",
    ("galaxy", "z", "fold", "7"): "Snapdragon 8 Elite",
    ("galaxy", "a07", "5g"): "Dimensity 6300",
    ("galaxy", "a07",): "Helio G99",
}


def known_soc(words: tuple) -> str | None:
    for n in range(min(4, len(words)), 0, -1):
        if words[:n] in KNOWN_SOC:
            return KNOWN_SOC[words[:n]]
    return None


def known_ram(words: tuple) -> int | None:
    for n in range(min(4, len(words)), 0, -1):
        if words[:n] in KNOWN_RAM:
            return KNOWN_RAM[words[:n]]
    return None


def battery_donor_map(raws: list[tuple[dict, str]]) -> dict[tuple, int]:
    """(brand, line words) -> agreed mAh across listings that publish one.

    eXtra publishes mAh per SKU; Jarir publishes chemistry only. A target is
    filled only when every donor for its product line agrees within 100 mAh
    (Pro vs Pro Max are different line words, so they never pool together).
    """
    pools: dict[tuple, set] = {}
    for rec, _store in raws:
        mah = parse_battery(rec)
        if not mah:
            continue
        key = ((rec.get("brand") or "").lower(), raw_line_words(rec.get("name"), rec.get("brand")))
        if len(key[1]) >= 2:
            pools.setdefault(key, set()).add(mah)
    return {k: int(statistics.median(v)) for k, v in pools.items()
            if v and max(v) - min(v) <= 100}


def soc_donor_map(raws: list[tuple[dict, str]]) -> dict[tuple, str]:
    """(brand, line words, ram, storage) -> agreed table SoC name.

    Listings that only say "Octa-Core" borrow the chip from the same
    configuration named at the other store; every donor must agree on the
    same table entry, otherwise the target stays unscored.
    """
    pools: dict[tuple, set] = {}
    for rec, _store in raws:
        mark, name, _mapping = lookup_soc(
            (_first_text(rec, "chipset", "Processor Core", "Processor Type",
                         "Processor Model") or ""))
        if not mark or not name or name.startswith("median of "):
            continue
        ram = parse_ram(rec)
        storage = parse_storage(rec)
        key = ((rec.get("brand") or "").lower(),
               raw_line_words(rec.get("name"), rec.get("brand")), ram, storage)
        if len(key[1]) >= 2:
            pools.setdefault(key, set()).add(name)
    return {k: next(iter(v)) for k, v in pools.items() if len(v) == 1}


def soc_share_lookup(donors: dict[tuple, str], brand: str | None,
                     words: tuple, ram: int | None, storage: int | None) -> str | None:
    """Agreeing donor chip for a config, tolerating unknown RAM/storage.

    A donor group matches when brand + line words are equal and every
    capacity known on both sides agrees (an unknown side never contradicts).
    Sharing happens only when ALL matching groups name the same chip —
    so a 4G/5G line split (different known RAM) never pools together.
    """
    if len(words) < 2:
        return None
    names = set()
    for (b, w, dram, dstor), name in donors.items():
        if b != (brand or "").lower() or w != words:
            continue
        if ram is not None and dram is not None and ram != dram:
            continue
        if storage is not None and dstor is not None and storage != dstor:
            continue
        names.add(name)
    return next(iter(names)) if len(names) == 1 else None


def vendor_of_table(name: str) -> str | None:
    low = name.lower()
    if low.startswith("apple"):
        return "Apple"
    if "snapdragon" in low or "qualcomm" in low:
        return "Qualcomm"
    if "dimensity" in low or "helio" in low:
        return "MediaTek"
    if "exynos" in low:
        return "Samsung"
    if "tensor" in low:
        return "Google"
    if "unisoc" in low:
        return "Unisoc"
    if "kirin" in low:
        return "Huawei"
    return None


BRAND_MAP = {
    "apple": "Apple", "samsung": "Samsung", "xiaomi": "Xiaomi",
    "honor": "Honor", "huawei": "Huawei", "oppo": "Oppo", "vivo": "Vivo",
    "realme": "Realme", "oneplus": "OnePlus", "nothing": "Nothing",
    "infinix": "Infinix", "tecno": "Tecno", "motorola": "Motorola",
    "nokia": "Nokia", "sony": "Sony", "google": "Google",
}


def brand_of(raw: str | None) -> str | None:
    if not raw:
        return None
    return BRAND_MAP.get(raw.strip().lower(), raw.strip().title())


def short_name(name: str | None, brand: str | None) -> str:
    raw = re.sub(r"\s+", " ", (name or "").strip())
    if not raw:
        return ""
    head = raw.split(",")[0].strip()
    if brand:
        head = re.sub(rf"^{re.escape(brand)}[\s\-–—]*", "", head, flags=re.I)
    return head or raw


def build_tags(rec: dict, soc: dict, mah: int | None, storage: int | None) -> list[str]:
    tags = []
    if re.search(r"\b5G\b", rec.get("network") or ""):
        tags.append("5G")
    if rec.get("renewed"):
        tags.append("Renewed")
    if (soc.get("score") or 0) >= 80:
        tags.append("Flagship chip")
    if mah and mah >= 5000:
        tags.append("Big battery")
    if storage and storage >= 512:
        tags.append("Big storage")
    return tags


def normalize(rec: dict, store: str, hints: dict | None = None) -> dict:
    delisted = bool(rec.get("delisted"))
    in_stock = rec.get("in_stock")
    if in_stock is None:
        in_stock = False if delisted else None

    if store == "Jarir":
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("regular_price_sar")
        color = rec.get("color")
    elif store == "GreenTelcom":
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("was_price_sar")
        color = rec.get("color")
    elif store == "Ichiban":
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("was_price_sar")
        color = rec.get("color")
    else:
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("was_price_sar")
        color = _first_text(rec, "Color")
    discount = None
    if was_price and rec.get("price_sar") and was_price > rec["price_sar"]:
        discount = round((1 - rec["price_sar"] / was_price) * 100, 1)
    if discount is None and store == "eXtra":
        discount = rec.get("discount_pct")

    soc = parse_soc(rec)
    if soc.get("score") is None and hints and hints.get("soc"):
        mark, name, _mapping = lookup_soc(hints["soc"])
        if mark:
            soc["model"] = name
            soc["vendor"] = soc.get("vendor") or vendor_of_table(name)
            soc["score"] = scale_soc(mark)
            soc["benchmark"] = {"source": "antutu_v10", "mark": mark,
                                "name": name, "mapping": "config-match"}
    if soc.get("score") is None:
        # fixed-SKU last resort (iPhones, Samsung flagships): only after the
        # listed text and the cross-store share both fail, so evidence always
        # beats the model-line guess (an Octa-Core A07 5G must not inherit
        # the 4G line's chip)
        guess = known_soc(raw_line_words(rec.get("name"), rec.get("brand")))
        if guess:
            mark, name, _mapping = lookup_soc(guess)
            if mark:
                soc["text"] = soc.get("text") or guess
                soc["model"] = name
                soc["vendor"] = soc.get("vendor") or vendor_of_table(name)
                soc["score"] = scale_soc(mark)
                soc["benchmark"] = {"source": "antutu_v10", "mark": mark,
                                    "name": name, "mapping": "known-model"}
    ram = parse_ram(rec)
    ram_source = "listed" if ram is not None else None
    if ram is None and hints:
        ram = hints.get("ram")
        ram_source = "known-model" if ram is not None else None
    storage = parse_storage(rec)
    display = parse_display(rec)
    mah = parse_battery(rec)
    battery_source = "listed" if mah is not None else None
    if mah is None and hints and hints.get("mah") is not None:
        mah, battery_source = hints["mah"], "config-match"
    cameras = parse_cameras(rec)
    network = parse_network(rec)
    os_name = _first_text(rec, "operating system", "Operating System", "OS", "Software")

    perf = compute_perf(soc, ram, storage)
    if perf is None:
        cam_score = quality = overall = value = None
    else:
        blob = _all_specs(rec)
        cam_score = compute_camera_score(cameras, blob, rec.get("specs"))
        quality = compute_quality(display, cameras, mah, storage, blob, rec.get("specs"))
        overall = round(0.60 * perf + 0.40 * quality, 1)
        value = compute_value(overall, rec.get("price_sar"))

    brand = brand_of(rec.get("brand"))
    renewed = bool(re.search(r"\brenewed\b|\brefurbished\b|\bpre-owned\b|\bused\b|\bgrade\s*[abc]\b",
                             rec.get("name") or "", re.I))
    return {
        "id": f"{store.lower()}:{rec.get('sku')}",
        "store": store,
        "sku": rec.get("sku"),
        "name": rec.get("name"),
        "short_name": short_name(rec.get("name"), brand),
        "brand": brand,
        "renewed": renewed,
        "url": rec.get("url"),
        "image": image,
        "price": rec.get("price_sar"),
        "price_local": rec.get("price_local"),
        "currency": rec.get("currency") or "SAR",
        "was_price": was_price,
        "discount": discount,
        "in_stock": bool(in_stock) if in_stock is not None else None,
        "delisted": delisted,
        "soc": soc,
        "ram": ram,
        "ram_source": ram_source,
        "storage": storage,
        "display": display,
        "battery_mah": mah,
        "battery_source": battery_source,
        "cameras": cameras,
        "network": network,
        "os": os_name,
        "color": color,
        "perf_score": perf,
        "quality_score": quality,
        "camera_score": cam_score,
        "overall_score": overall,
        "value_score": value,
        "tags": build_tags({"network": network, "renewed": renewed}, soc, mah, storage),
        "specs": rec.get("specs") or {},
    }


# Colorway/bundle descriptors that must not split (or merge) dedupe lines:
# "A300 Deep Cherry" and "A300 Midnight Black" are the same phone.
_LINE_NOISE = {
    "black", "white", "green", "red", "blue", "gold", "silver", "grey", "gray",
    "pink", "purple", "orange", "brown", "beige", "ivory", "coral", "bronze",
    "rose", "mint", "lavender", "navy", "cyan", "titanium", "chrome",
    "graphite", "midnight", "starlight", "glacier", "cosmic", "arctic",
    "olive", "teal", "maroon", "burgundy", "khaki", "champagne", "violet",
    "magenta", "yellow", "ochre", "sand", "clay", "forest", "moonlight",
    "cherry", "lake", "cloud", "comet", "natural", "deep", "sunset",
    "ea159", "bundle",
}


def line_key(rec: dict) -> tuple:
    """Product-line identity for dedupe: first 4 non-color words."""
    words = raw_line_words(rec.get("name"), rec.get("brand"))
    return tuple(w for w in words if w not in _LINE_NOISE)[:4]


def dedupe_key(rec: dict) -> tuple | None:
    soc_model = ((rec.get("soc") or {}).get("model") or "").lower()
    line = line_key(rec)
    if not rec.get("brand") or not soc_model or len(line) < 2:
        return None
    inches = (rec.get("display") or {}).get("inches")
    inches = round(inches, 1) if inches else None
    # new vs renewed never fold: different markets, incomparable prices
    return (rec["brand"].lower(), line, soc_model, rec.get("ram"),
            rec.get("storage"), inches, bool(rec.get("renewed")))


def apply_dedupe(records: list[dict]) -> dict:
    """Fold identical configs across stores into one card with an offers list."""
    groups: dict[tuple, list[dict]] = {}
    for rec in records:
        key = dedupe_key(rec)
        if key is None:
            continue
        groups.setdefault(key, []).append(rec)
    stats = {"groups": 0, "folded": 0}
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (r["price"] is None, r["price"] or 0))
        primary = members[0]
        primary["offers"] = [
            {"store": m["store"], "price": m["price"], "url": m["url"],
             "in_stock": m["in_stock"], "sku": m["sku"]}
            for m in members
        ]
        primary["dup_count"] = len(members)
        for m in members[1:]:
            m["merged_into"] = primary["id"]
        stats["groups"] += 1
        stats["folded"] += len(members) - 1
    return stats


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    jarir = [r for r in load(DATA_DIR / "jarir_phones_in_stock.jsonl") if r.get("is_phone") is not False]
    extra = [r for r in load(DATA_DIR / "extra_phones_in_stock.jsonl") if r.get("is_phone") is not False]
    green = [r for r in load(DATA_DIR / "greentelcom_phones_in_stock.jsonl") if r.get("is_phone") is not False]
    ichiban = [r for r in load(DATA_DIR / "ichiban_phones_in_stock.jsonl") if r.get("is_phone") is not False]
    raws = [(r, "Jarir") for r in jarir] + [(r, "eXtra") for r in extra] + [(r, "GreenTelcom") for r in green] + [(r, "Ichiban") for r in ichiban]
    donors = battery_donor_map(raws)
    soc_donors = soc_donor_map(raws)

    def hints_for(rec: dict) -> dict:
        words = raw_line_words(rec.get("name"), rec.get("brand"))
        key = ((rec.get("brand") or "").lower(), words)
        ram, storage = parse_ram(rec), parse_storage(rec)
        return {
            "ram": known_ram(words),
            "mah": donors.get(key),
            "soc": soc_share_lookup(soc_donors, rec.get("brand"), words, ram, storage),
        }

    records = ([normalize(r, "Jarir", hints_for(r)) for r in jarir]
               + [normalize(r, "eXtra", hints_for(r)) for r in extra]
               + [normalize(r, "GreenTelcom", hints_for(r)) for r in green]
               + [normalize(r, "Ichiban", hints_for(r)) for r in ichiban])
    records = [r for r in records if not (r.get("price") and r["price"] > 15000)]
    records.sort(key=lambda r: (r["price"] or 0))

    dedupe_stats = apply_dedupe(records)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "unified_phones.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    cols = ["id", "store", "sku", "brand", "name", "short_name", "color",
            "price", "price_local", "currency", "was_price", "discount", "in_stock", "delisted", "renewed",
            "soc_text", "soc_vendor", "soc_model", "soc_mark", "soc_score", "soc_mapping",
            "ram", "ram_source", "storage", "screen_inches", "screen_type",
            "battery_mah", "battery_source", "rear_mp", "front_mp", "network", "os",
            "perf_score", "quality_score", "camera_score", "overall_score", "value_score",
            "offers", "merged_into", "tags", "url"]
    with (DATA_DIR / "unified_phones.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in records:
            bm = (r["soc"].get("benchmark") or {})
            writer.writerow({
                "id": r["id"], "store": r["store"], "sku": r["sku"],
                "brand": r["brand"], "name": r["name"],
                "short_name": r.get("short_name"), "color": r.get("color"),
                "price": r["price"], "price_local": r.get("price_local"),
                "currency": r.get("currency"), "was_price": r["was_price"],
                "discount": r["discount"], "in_stock": r["in_stock"],
                "delisted": r["delisted"], "renewed": r.get("renewed"),
                "soc_text": r["soc"]["text"], "soc_vendor": r["soc"]["vendor"],
                "soc_model": r["soc"]["model"], "soc_mark": bm.get("mark"),
                "soc_score": r["soc"]["score"], "soc_mapping": bm.get("mapping"),
                "ram": r["ram"], "ram_source": r.get("ram_source"), "storage": r["storage"],
                "screen_inches": r["display"]["inches"],
                "screen_type": r["display"]["type"],
                "battery_mah": r["battery_mah"], "battery_source": r.get("battery_source"),
                "rear_mp": r["cameras"]["rear_mp"],
                "front_mp": r["cameras"]["front_mp"],
                "network": r["network"], "os": r["os"],
                "perf_score": r["perf_score"], "quality_score": r["quality_score"],
                "camera_score": r.get("camera_score"),
                "overall_score": r["overall_score"], "value_score": r["value_score"],
                "offers": "; ".join(f"{o['store']} {o['price']:.0f}" for o in (r.get("offers") or [])),
                "merged_into": r.get("merged_into"),
                "tags": "|".join(r["tags"]), "url": r["url"],
            })

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {
        "generated_at": generated_at,
        "count": len(records),
        "sources": {s: sum(1 for r in records if r["store"] == s)
                    for s in dict.fromkeys(r["store"] for r in records)},
        "dedupe": dedupe_stats,
        "value_floor_sar": VALUE_FLOOR_SAR,
        "soc_scored": sum(1 for r in records if r["soc"].get("score") is not None),
        "ram_known_model": sum(1 for r in records if r.get("ram_source") == "known-model"),
        "battery_shared": sum(1 for r in records if r.get("battery_source") == "config-match"),
    }
    (DATA_DIR / "unified_phones.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    (VIEWER_DIR / "phones.js").write_text(
        "window.PHONES_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
        + "window.PHONES = " + payload + ";\n",
        encoding="utf-8",
    )

    total = len(records)
    missing = {
        "soc": sum(1 for r in records if not r["soc"]["text"]),
        "soc_score": sum(1 for r in records if r["soc"]["score"] is None),
        "ram": sum(1 for r in records if not r["ram"]),
        "storage": sum(1 for r in records if not r["storage"]),
        "battery": sum(1 for r in records if not r["battery_mah"]),
        "display": sum(1 for r in records if not r["display"]["inches"]),
    }
    print(f"normalized {total} phones (Jarir {len(jarir)} + eXtra {len(extra)} + GreenTelcom {len(green)} + Ichiban {len(ichiban)})")
    print("missing:", ", ".join(f"{k}={v}" for k, v in missing.items()))
    if records:
        prices = [r["price"] for r in records if r["price"]]
        if prices:
            print(f"price range: {min(prices):.0f} - {max(prices):.0f} SAR "
                  f"(median {statistics.median(prices):.0f})")
    top = sorted((r for r in records if r.get("value_score")),
                 key=lambda r: -r["value_score"])[:5]
    for r in top:
        print(f"  value {r['value_score']:.1f} {r['price']:.0f} SAR "
              f"{r['brand']} {r.get('short_name')} [{r['store']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
