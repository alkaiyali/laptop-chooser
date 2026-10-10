#!/usr/bin/env python3
"""Normalize the Jarir + eXtra in-stock datasets into one comparable schema.

Reads:   data/laptops_in_stock.jsonl (Jarir)
         data/extra_laptops_in_stock.jsonl (eXtra)
Writes:  data/unified_laptops.json       (source of truth for the chooser)
         data/unified_laptops.csv
         viewer/laptops.js               (window.LAPTOPS = [...]; works over file://)

Normalized fields: cpu{text,brand,family,gen,cores,score}, ram, ram_type,
storage, storage_type, gpu{text,brand,class,vram,score}, screen{inches,
width,height,res,res_label,panel,hz,touch}, os, weight_kg, battery, warranty,
plus perf_score and value_score (performance per 1000 SAR).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmarks import (  # noqa: E402
    Benchmarks,
    CPU_SINGLE_WEIGHT,
    igpu_name_for_cpu,
    lookup_cpu,
    lookup_gpu,
    scale_cpu,
    scale_cpu_single,
    scale_gpu,
)
from notebookcheck import (  # noqa: E402
    NBC_MIN_N,
    fit_pm_equiv,
    load_nbc,
    lookup_nbc,
    r23_multi,
    r23_single,
)
from common import DATA_DIR, log  # noqa: E402
from dedupe import apply_dedupe  # noqa: E402
from history import append_snapshot, load_stats  # noqa: E402
from memory import apply_memory  # noqa: E402
from moat import sanitize_meta, sanitize_records  # noqa: E402
from quality import estimated_issue_rate, is_junk, seller_note  # noqa: E402
from ratings import apply_ratings, BUILD_LABELS  # noqa: E402
from sanity import flag_prices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = ROOT / "viewer"

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

JUNK = {"no", "n/a", "none", "not specified", "-", "", "shared", "nan"}


def _num(text, pattern, default=None):
    m = re.search(pattern, text, re.I)
    if not m:
        return default
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return default


def _first_text(rec: dict, *labels):
    for label in labels:
        value = rec.get("specs", {}).get(label)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in JUNK:
            return re.sub(r"\s+", " ", value.strip())
    return None


def _all_specs(rec: dict) -> str:
    """Everything searchable in the record (spec values + name + description)."""
    parts = [rec.get("name") or "", rec.get("short_description") or "", rec.get("description") or ""]
    parts += [str(v) for v in (rec.get("specs") or {}).values()]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

CPU_CODE = re.compile(
    r"\b("
    r"i[3579]-\d{3,5}[A-Z]{0,4}|"
    r"Ultra\s+[3579]\s+\d{3}[A-Z]{0,4}|"
    r"Core\s+[3579][-\s]\d{3}[A-Z]{0,4}|"
    r"Ryzen\s+(?:AI\s+)?[3579][-\s]\d{3}[A-Z]{0,4}|"
    r"Xeon\s+[WE]-?\d{4,5}[A-Z]{0,4}|"
    r"\(?(\d{1,2})(?:st|nd|rd|th)\s+Gen\)?|"
    r"N\d{2,4}|"
    r"[AMW]\d(?:x)?(?:-\d+[A-Z]*)?|"
    r"X[12]P?[-\s]?\d{1,3}[-\s]?\d{0,3}|"
    r"Snapdragon\s+X\s?(?:2\s?)?(?:Elite|Plus)?"
    r")\b",
    re.I,
)


CPU_FAMILY_WORD = re.compile(r"core|ryzen|ultra|snapdragon|athlon|\bM\d|\bA\d{2}", re.I)


def combine_cpu_text(*candidates) -> str:
    """Pick the most specific CPU string and keep the brand prefix.

    If the winner is a bare model code (e.g. "350", "275HX") and another
    candidate carries the family ("Intel Core Ultra 9"), the two are merged so
    the family is not lost.
    """
    valid: list[tuple[float, str]] = []
    brand = None
    for raw in candidates:
        if not raw or not isinstance(raw, str):
            continue
        text = re.sub(r"\s+", " ", raw.strip())
        if not text or text.lower() in JUNK:
            continue
        low = text.lower()
        if "intel" in low:
            brand = brand or "Intel"
        elif "amd" in low or "ryzen" in low or "athlon" in low:
            brand = brand or "AMD"
        elif "apple" in low or re.search(r"\bM\d\b", text, re.I):
            brand = brand or "Apple"
        elif "qualcomm" in low or "snapdragon" in low:
            brand = brand or "Qualcomm"
        score = len(re.findall(r"\d", text)) * 2 + len(re.findall(r"[A-Za-z]", text)) * 0.05
        valid.append((score, text))
    if not valid:
        return ""
    ranked = sorted(valid, reverse=True)
    best = ranked[0][1]
    if not CPU_FAMILY_WORD.search(best):
        for _, other in ranked[1:]:
            if CPU_FAMILY_WORD.search(other):
                best = f"{other} {best}"
                break
    if brand and brand.lower() not in best.lower():
        best = f"{brand} {best}"
    return re.sub(r"\s+", " ", best)


def canonical_cpu_model(brand: str | None, family: str | None, model: str | None) -> str:
    """Rewrite a parsed CPU model into the vendor's official style.

    Intel Core i5-13420H · Core Ultra 7 155H · Core 5 120U · N150
    AMD Ryzen 5 40 · Ryzen AI 7 445 · Ryzen 7 5825U
    Apple M5 Pro · Qualcomm Snapdragon X Elite
    """
    s = re.sub(r"\s+", " ", model or "").strip()
    fam = (family or "").strip()
    if not s:
        return fam

    if brand == "Intel":
        if re.match(r"^i[3579][-\s]", s, re.I):
            s = re.sub(r"^(i[3579])[-\s]+", r"\1-", s, flags=re.I)
            return f"Core {s}"
        m = re.match(r"^(?:Core\s+)?Ultra\s+(X?)([3579])[-\s]*(.*)$", s, re.I)
        if m:
            rest = re.sub(r"[-\s]+", " ", m.group(3)).strip()
            return f"Core Ultra {m.group(1).upper()}{m.group(2)} {rest}".strip()
        # new naming without the i: "Core i5-120U" / "Core i3 100U" -> "Core 5 120U"
        m = re.match(r"^Core\s+i([3579])[-\s]+(\d{3}[A-Z0-9]*)$", s, re.I)
        if m:
            return f"Core {m.group(1)} {m.group(2)}"
        m = re.match(r"^Core\s+([3579])[-\s]*(.*)$", s, re.I)
        if m:
            rest = re.sub(r"[-\s]+", " ", m.group(2)).strip()
            return f"Core {m.group(1)} {rest}".strip()
        if re.match(r"^N\d", s, re.I):
            return s.upper()
        return s

    if brand == "AMD":
        m = re.match(r"^Ryzen\s+(AI[-\s]+)?([3579])[-\s]*(.*)$", s, re.I)
        if m:
            rest = re.sub(r"[-\s]+", " ", m.group(3)).strip()
            prefix = "Ryzen AI " if m.group(1) else "Ryzen "
            s = f"{prefix}{m.group(2)} {rest}".strip()
        elif not s.lower().startswith(("ryzen", "athlon")):
            s = fam or s
        s = re.sub(r"(\d)([a-z]{1,3})$", lambda mm: mm.group(1) + mm.group(2).upper(), s)
        s = re.sub(r"\b(hx|hs|hk|hq)\b", lambda mm: mm.group(1).upper(), s, flags=re.I)
        return re.sub(r"\b(HX|HS|H|U)(\d{3})\b", r"\1 \2", s)

    if brand == "Apple":
        m = re.match(r"^([MA]\d+)\s*(Pro|Max|Ultra)?$", s, re.I)
        if m:
            variant = f" {m.group(2).title()}" if m.group(2) else ""
            return f"{m.group(1).upper()}{variant}"
        return s

    if brand == "Qualcomm":
        s = re.sub(r"^snapdragon", "Snapdragon", s, flags=re.I)
        s = re.sub(r"\bx2?\b", lambda mm: mm.group(0).upper(), s, flags=re.I)
        return re.sub(r"\b(elite|plus)\b", lambda mm: mm.group(1).title(), s, flags=re.I)

    return s


def parse_cpu(text: str) -> dict:
    out = {"text": None, "brand": None, "family": None, "model": None, "gen": None, "cores": None, "score": None}
    if not text:
        return out
    # vendor spec sheets write "AMD Ryzen™ AI 5 330" / "Intel® Core™ 5" — the marks
    # would break every pattern below
    text = re.sub(r"\s+", " ", re.sub(r"[™®©]", " ", text)).strip()
    text = re.sub(r"\bRyzen\s+[Aa][l1]\b", "Ryzen AI", text)      # "Ryzen Al 7 350" typo
    out["text"] = text
    t = text

    # brand
    if re.search(r"intel", t, re.I) or re.search(r"\b(i[3579]-\d+|u[3579][- ]\d|core[-\s]+[3579i]|ultra\s+[3579])", t, re.I):
        out["brand"] = "Intel"
    elif re.search(r"amd|ryzen|athlon|mendocino", t, re.I):
        out["brand"] = "AMD"
    elif re.search(r"apple|^\s*M\d|\bA\d{1,2}\s+(?:Pro|Max)?\s*(?:chip|bionic)", t, re.I):
        out["brand"] = "Apple"
    elif re.search(r"qualcomm|snapdragon", t, re.I):
        out["brand"] = "Qualcomm"

    # family + model + tier
    tier = cur_gen = None
    if out["brand"] == "Intel":
        xeon = re.search(r"\b(Xeon\s+[WE]-?\d{4,5}[A-Z]{0,4})\b", t, re.I)
        if xeon:
            out["family"] = "Xeon"
            out["model"] = re.sub(r"\bxeon\b", "Xeon", xeon.group(1), flags=re.I)
            out["model"] = re.sub(r"\b([WE])\s*-?\s*(\d{4,5}[A-Za-z]{0,4})",
                                  lambda mm: f"{mm.group(1).upper()}-{mm.group(2).upper()}",
                                  out["model"], flags=re.I)
        m = re.search(r"(Core\s+Ultra|Core|Ultra)[-\s]*(X?i?[3579])\b", t, re.I)
        if m:
            token = m.group(2).upper()
            tier = int(re.sub(r"[XI]", "", token))
            if "ultra" in m.group(1).lower():
                out["family"] = f"Core Ultra {'X' if 'X' in token else ''}{tier}"
            else:
                out["family"] = f"Core {'i' if 'I' in token else ''}{tier}"
        elif re.search(r"\bN\d{2,4}\b", t):
            out["family"] = "N-series"
            tier = 1
        else:
            m = re.search(r"\bu([3579])[- ]?\d{3}", t, re.I)
            if m:
                tier = int(m.group(1))
                out["family"] = f"Core Ultra {tier}"
        m = re.search(r"\b(i[3579][-\s]\d{4,5}[A-Z]{0,4})\b", t, re.I)
        if m:
            out["model"] = _pretty_cpu_model(re.sub(r"^I", "i", m.group(1)))
        else:
            m = re.search(r"\b(Ultra\s+X?[3579][-\s]\d{3}[A-Z]{0,4})\b", t, re.I)
            if m:
                out["model"] = _pretty_cpu_model(m.group(1))
            else:
                m = re.search(r"\b(Core\s+i?[3579][-\s]?\d{3}[A-Z]{0,4})\b", t, re.I)
                if m:
                    out["model"] = _pretty_cpu_model(m.group(1))
                else:
                    m = re.search(r"\bu([3579])[- ]?(\d{3}[A-Z]{0,4})", t, re.I)
                    if m:
                        out["model"] = f"Ultra {m.group(1)} {m.group(2).upper()}"
        if not out["model"]:
            m = re.search(r"\b(Core\s+[3579]\s+processor\s+\d{3}[A-Z]{0,4})\b", t, re.I)
            if m:
                out["model"] = _pretty_cpu_model(re.sub(r"\s+processor\s+", " ", m.group(1), flags=re.I))
    elif out["brand"] == "AMD":
        m = re.search(r"Ryzen\s+AI\s+MAX\+?\s*\d{3}", t, re.I)
        if m:
            tier = 9
            out["family"] = "Ryzen AI MAX+"
            out["model"] = re.sub(r"\s+", " ", m.group(0))
        else:
            m = re.search(r"Ryzen\s+(AI[-\s]+)?([3579])\b", t, re.I)
            if m:
                tier = int(m.group(2))
                out["family"] = ("Ryzen AI " if m.group(1) else "Ryzen ") + m.group(2)
            elif re.search(r"athlon", t, re.I):
                out["family"] = "Athlon"
                tier = 1
            m = re.search(r"\bRyzen\s+(?:AI[-\s]+)?[3579][-\s]*(?:HX|HS|H|U)?[-\s]*\d{2,5}[A-Z]{0,4}", t, re.I)
            if m:
                raw = re.sub(r"\s+", " ", m.group(0)).strip()
                raw = re.sub(r"^ryzen", "Ryzen", raw, flags=re.I)
                out["model"] = _pretty_cpu_model(raw)
    elif out["brand"] == "Apple":
        m = re.search(r"\b(M\d+)\s*(Pro|Max|Ultra)?\b", t, re.I)
        if m:
            variant = (m.group(2) or "").strip().title()
            out["model"] = f"{m.group(1).upper()} {variant}".strip()
            out["family"] = out["model"]
            tier = int(m.group(1)[1:])
            cur_gen = tier
        else:
            m = re.search(r"\b(A\d{2})\s*(Pro|Max)?\b", t, re.I)
            if m:
                variant = (m.group(2) or "").strip().title()
                out["model"] = f"{m.group(1).upper()} {variant}".strip()
                out["family"] = out["model"]
    elif out["brand"] == "Qualcomm":
        m = re.search(r"Snapdragon\s+X\s?(2\s?)?(Elite|Plus)?", t, re.I)
        if m:
            fam = re.sub(r"\s+", " ", m.group(0)).strip()
            fam = re.sub(r"^snapdragon", "Snapdragon", fam, flags=re.I)
            fam = re.sub(r"\bx2?\b", lambda mm: mm.group(0).upper(), fam, flags=re.I)
            fam = re.sub(r"\b(elite|plus)\b", lambda mm: mm.group(1).title(), fam, flags=re.I)
            out["family"] = fam
            out["model"] = out["family"]

    # family fallbacks for when the family word was lost while merging spec + title
    if not out["family"] and out["brand"] == "Intel":
        m = re.search(r"\bi([3579])[-\s]?\d", t)
        if m:
            tier = int(m.group(1))
            out["family"] = f"Core i{tier}"
    if not out["family"] and out["brand"] == "AMD":
        m = re.search(r"\bR([3579])\b", t, re.I)           # Mendocino R5 / R7
        if m:
            tier = int(m.group(1))
            out["family"] = f"Ryzen {tier}"
        elif re.search(r"\b[3579]\d{3}[A-Z]{0,4}\b", t):
            tier = 5
            out["family"] = "Ryzen"
    if not out["family"] and out["brand"] == "Qualcomm":
        m = re.search(r"\bX([12])([PE])", t, re.I)
        if m:
            gen, kind = m.group(1), m.group(2).upper()
            out["family"] = f"Snapdragon X{'2 ' if gen == '2' else ' '}{'Elite' if kind == 'E' else 'Plus'}"
            out["model"] = out["family"]
        else:
            out["family"] = "Snapdragon"

    if not out["model"] and out["family"]:
        out["model"] = out["family"]

    # unify the model spelling with the vendor's official name and rebuild text
    if out["brand"]:
        out["model"] = canonical_cpu_model(out["brand"], out["family"], out["model"])
        if out["brand"] == "Intel" and out["model"] and out["family"]:
            fam = out["family"]
            # merchant data sometimes mixes families: "Core Ultra 7" + model "i5-5350U"
            if ("Ultra" in fam) != ("Ultra" in out["model"]):
                out["model"] = fam
            m = re.match(r"^Core i([3579])-", out["model"])
            if m and fam.startswith("Core i") and m.group(1) != fam[-1:]:
                out["model"] = fam
        if out["model"]:
            out["text"] = f"{out['brand']} {out['model']}"

    # generation (Intel/AMD)
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)\s+Gen", t, re.I)
    if m:
        cur_gen = int(m.group(1))
    if out["brand"] == "Intel" and cur_gen is None and out["model"]:
        # i5-1334U -> 13th gen, i5-10210U -> 10th gen, i7-8550U -> 8th gen
        m = re.search(r"i[3579][-\s](\d{3,5})", out["model"])
        if m:
            digits = m.group(1)
            two = int(digits[:2])
            if 10 <= two <= 14:
                cur_gen = two
            elif digits[0] in "789":
                cur_gen = int(digits[0])
    out["gen"] = cur_gen

    # cores
    m = re.search(r"(\d{1,2})\s*(?:cores?|core)\b", t, re.I)
    if m:
        out["cores"] = int(m.group(1))

    # score comes from real PassMark benchmarks (see apply_benchmarks below),
    # never from a heuristic
    out["score"] = None
    return out


_BENCH: Benchmarks | None = None
_NBC = None  # None = not tried yet, False = snapshot missing
_FIT: tuple[float, float, int] | None = None
_FIT_SINGLE: tuple[float, float, int] | None = None


def _bench() -> Benchmarks:
    global _BENCH
    if _BENCH is None:
        _BENCH = Benchmarks()
    return _BENCH


def _nbc():
    """Notebookcheck snapshot (or None); also fits the R23->PM scales once."""
    global _NBC, _FIT, _FIT_SINGLE
    if _NBC is None:
        _NBC = load_nbc() or False
        if _NBC:
            _FIT = fit_pm_equiv(_bench().cpu)
            _FIT_SINGLE = fit_pm_equiv(_bench().cpu, single=True)
    return _NBC or None


def apply_benchmarks(cpu: dict, gpu: dict) -> None:
    """Attach real benchmark marks + 0-100 scores to a CPU/GPU pair (in place).

    CPU precedence: Notebookcheck Cinebench R23 with n >= NBC_MIN_N
    (review-grade, controlled runs) -> PassMark CPU Mark + Single Thread Mark
    (universal).  R23 medians convert to equivalent PassMark marks through the
    empirical log-log fits over the snapshot overlap (multi r ≈ 0.98, single
    r ≈ 0.98), so every source shares one scale.  The CPU score is
    ``CPU_SINGLE_WEIGHT`` single-thread + the rest multithread: single-core
    speed is what makes a machine feel fast.  GPU is always PassMark G3D
    (see benchmarks.py).
    """
    bench = _bench()
    mark, single, pname, mapping = None, None, None, None
    source, extra = "passmark", {}
    nbc = _nbc()
    if nbc:
        row, nmapping = lookup_nbc(nbc, cpu.get("text"))
        v, n = r23_multi(row)
        if v and n >= NBC_MIN_N and _FIT and _FIT[2] >= 8:
            a, b, _c = _FIT
            mark = int(math.exp(a + b * math.log(v)))
            pname, mapping = row["name"], nmapping
            source, extra = "notebookcheck", {"r23": v, "n": n}
            sv, sn = r23_single(row)
            if sv and sn >= NBC_MIN_N and _FIT_SINGLE and _FIT_SINGLE[2] >= 8:
                a2, b2, _c2 = _FIT_SINGLE
                single = int(math.exp(a2 + b2 * math.log(sv)))
                extra.update({"r23_single": sv, "n_single": sn})
    if mark is None:
        mark, single, pname, mapping = lookup_cpu(bench, cpu.get("text"))
    elif single is None:
        # NBC multi is review-grade but the row has no usable single median:
        # borrow the PassMark Single Thread Mark for the same chip
        _, single, _, _ = lookup_cpu(bench, cpu.get("text"))
    score_multi = scale_cpu(mark)
    score_single = scale_cpu_single(single)
    if score_multi is not None and score_single is not None:
        cpu["score"] = round(CPU_SINGLE_WEIGHT * score_single
                             + (1 - CPU_SINGLE_WEIGHT) * score_multi, 1)
    else:
        cpu["score"] = score_single if score_multi is None else score_multi
    cpu["benchmark"] = (
        {"source": source, "mark": mark, "single": single, "name": pname, "mapping": mapping,
         "score_multi": score_multi, "score_single": score_single, **extra}
        if mark else None
    )
    gmark, gname, gmapping = lookup_gpu(bench, gpu, cpu.get("text"))
    if gmapping == "apple-cpu-proxy":
        # PassMark publishes no Apple Silicon G3D rows: the GPU score follows
        # the M-chip's own CPU score (core counts are kept for later use)
        gmark, gname = mark, pname
        gpu["score"] = cpu["score"]
    else:
        gpu["score"] = scale_gpu(gmark)
    gpu["benchmark"] = (
        {"source": "passmark", "mark": gmark, "name": gname, "mapping": gmapping}
        if gmark else None
    )


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
# Only capacity values that are actually published in the listing are used —
# never estimated from cell count or chassis. DataBlitz descriptions carry the
# manufacturer's lines ("58 Wh 5100 mAh 11.4 V 3-cell Li-ion battery pack",
# "3-cell, 41 Wh Li-ion polymer"); Jarir lists capacity only on the ThinkBook
# Plus G5 hybrid (two batteries, kept as raw detail). eXtra and noon publish
# nothing beyond chemistry, so those records keep only the chemistry text.
BATTERY_WH = re.compile(r"(?<![\w.])(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:wh|whrs?)(?![\w])", re.I)
BATTERY_MAH = re.compile(r"(?<![\w.])(\d{4,5})\s*mah(?![\w])", re.I)


def parse_battery_capacity(text: str | None) -> tuple[float | None, int | None, str | None]:
    """(wh, mah, raw detail) from free text; numeric only when unambiguous.

    A listing with several different values (the ThinkBook Plus hybrid's
    "Station: 75Wh/Tab: 38.7Wh") keeps a raw detail string and no number,
    because picking one would be a guess.
    """
    t = re.sub(r"\s+", " ", text or "")
    if not t:
        return None, None, None
    wh_hits = [float(v.replace(",", ".")) for v in BATTERY_WH.findall(t)]
    mah_hits = [int(v) for v in BATTERY_MAH.findall(t)]
    if not wh_hits and not mah_hits:
        return None, None, None
    wh = wh_hits[0] if len(set(wh_hits)) == 1 else None
    mah = mah_hits[0] if len(set(mah_hits)) == 1 else None
    bits = []
    if wh_hits:
        bits.append(" · ".join(f"{v:g} Wh" for v in dict.fromkeys(wh_hits)))
    if mah_hits:
        bits.append(" · ".join(f"{v:,} mAh" for v in dict.fromkeys(mah_hits)))
    return wh, mah, " · ".join(bits)


def _battery_chem_label(battery_type: str | None) -> str | None:
    """Compact chemistry + cell count from a store's battery type text."""
    t = (battery_type or "").strip()
    if not t or t.lower() in ("type", "and power", "size", "pack"):
        return None
    chem = "Li-Po" if re.search(r"poly", t, re.I) else ("Li-ion" if re.search(r"li[- ]?i(on)?\b", t, re.I) else "")
    m = re.search(r"(\d+)\s*cell", t, re.I)
    cells = f"{m.group(1)}-cell" if m else ""
    label = " ".join(filter(None, [chem, cells]))
    return label or None


def compute_value_score(perf: float | None, price: float | None) -> float | None:
    """Balanced value score (performance per price with base ownership offset)."""
    if perf is None or not price:
        return None
    return round((perf / (price + 1500)) * 2000, 2)


def battery_points_for(wh: float | None) -> float:
    """0-4 performance points for a published capacity (35-100Wh curve)."""
    if not wh:
        return 0.0
    return round(min(4.0, max(0.0, (wh - 35) / 65 * 4.0)), 1)


def compute_quality_score(rec: dict) -> float:
    """0-100 physical hardware & quality index (Display 35%, Build 30%, Battery 20%, Storage 15%)."""
    d_score = rec.get("display_score") or 6.0
    b_score = rec.get("build_score") or 5.5
    wh = rec.get("battery_wh") or 50.0
    storage = rec.get("storage") or 512
    screen = rec.get("screen") or {}
    touch = screen.get("touch") or False
    inches = screen.get("inches") or 0.0
    gpu = rec.get("gpu") or {}
    is_dgpu = gpu.get("class") == "dedicated"

    # 1. Display Panel Quality (35.0 pts max)
    disp_pts = (d_score / 10.0) * 35.0

    # 2. Chassis & Build Quality (30.0 pts max, anchored at 4.5 plastic floor)
    # The lowest catalog build score is 4.5 (basic plastic); anchoring at 4.5
    # ensures cheap plastic is penalized and not gifted 15 free points, rewarding
    # all-metal (8.0-8.8) and CNC unibody (9.5+) with a decisive 20-30 pt advantage.
    build_pts = min(30.0, max(0.0, (b_score - 4.5) / 5.0 * 30.0))

    # 3. Battery & Mobility (20.0 pts max across 35-100 Wh curve)
    batt_pts = min(1.0, max(0.0, (wh - 35.0) / 65.0)) * 20.0

    # 4. Storage Capacity (15.0 pts max)
    if storage <= 256:
        stor_pts = 5.0
    elif storage <= 512:
        stor_pts = 8.0
    elif storage <= 1024:
        stor_pts = 12.0
    else:
        stor_pts = 15.0

    # Modifiers: Touchscreen (+1.5), 16"+ panel (+1.0)
    extra = (1.5 if touch else 0.0) + (1.0 if inches >= 16.0 else 0.0)
    # dGPU idle battery penalty
    if is_dgpu and wh < 65:
        extra -= 2.0

    return round(min(100.0, max(0.0, disp_pts + build_pts + batt_pts + stor_pts + extra)), 1)


# --- model-number lookup: carry published capacity onto identical hardware -----
# Only full part numbers (exact SKU identity) and chassis codes whose published
# capacities all agree are used; anything ambiguous is skipped, never guessed.
_BATT_EXACT_PATTERNS = [
    re.compile(r"^[A-Z]{1,2}\d{3,4}[A-Z]{1,3}-[A-Z0-9]{3,10}$"),            # ASUS  X1504VA-BQ4472WSM, FX608JHR-RV001W, G615JMR-S5012W
    re.compile(r"^\d{2}[A-Z]{2}\d{2}[A-Z0-9]{3,10}$"),                     # Lenovo MTM 83RR00LHPH
    re.compile(r"^[A-Z0-9]{3,6}-\d{3,4}[A-Z]{2,4}$"),                      # MSI  B14WFK-1046PH
    re.compile(r"^[A-Z]{2,3}\d{2}-\d{2}[A-Z0-9]{0,3}-[A-Z0-9]{3,6}$"),     # Acer AL14-45P-R10U
    re.compile(r"^(?=(?:.*[A-Z]){4,})(?=(?:.*\d){2,})[A-Z0-9]{10,14}$"),   # Gigabyte 6ZJM6PHE64SH
    re.compile(r"^\d{2}-[A-Z]{2}\d{3,4}[A-Z]{2,3}$"),                      # HP 15-FD1037NX
    re.compile(r"^[A-Z]\d{2}[A-Z]{4}$"),                                   # HP C26BGEA
    re.compile(r"^[A-Z]{2,3}\d?-\d{5}$"),                                  # Microsoft SKU ZHX-00008, EP2-27648
]
_BATT_CHASSIS_PATTERNS = [
    re.compile(r"^[A-Z]{1,2}\d{3,4}[A-Z]{1,3}$"),                          # ASUS X1504VA, UX3405CA
    re.compile(r"^\d{2}[A-Z]{2,4}\d{1,2}[A-Z]?$"),                         # Lenovo 15IRX10, 15ARP10E
    re.compile(r"^[A-Z]\d{2}[A-Z]{1,3}$"),                                 # MSI F13MG, B14WFK
    re.compile(r"^[A-Z]\d[A-Z]{2,3}$"),                                    # MSI C1WF, F3HMG
]
_BATT_APPLE = re.compile(r"^M(?=(?:.*[A-Z]){1,})(?=(?:.*\d){1,})[A-Z0-9]{4,6}$")  # MKGQ3, MHFF4AB
_BATT_TOKEN_JUNK = re.compile(
    r"^(?:DDR\d\w*|LPDDR\d\w*|RTX\d*\w*|GTX\d*\w*|GEN\d\w*|WIFI\d?\w*|PCIE\d?\w*|"
    r"NVME\d?\w*|USB\d?\w*|M2\w*|BT\d+|SATA\w*|TYPE\w*|HDMI\d?\w*|\d{1,4}(?:GB|TB|HZ|W|WH|MAH)|"
    r"CORES?|CELLS?|INCH|MUX)$", re.I)
# spec words that discredit a token outright (they never appear in real model codes)
_BATT_TOKEN_MENTIONS = re.compile(
    r"FHD|UHD|QHD|WUXGA|WQXGA|IPS|OLED|TOUCH|WINDOWS|OFFICE|BLUETOOTH|NITS|TOPS|MHZ|GHZ|"
    r"CORES?|CELLS?|INCH|MINI|PRO\b|MAX\b|PLUS\b|"
    r"\d+GB|\d+TB|SSD|NVME|PCIE|SATA|DDR|HDMI|WIFI|RTX|GTX|USB", re.I)
# hyphen-aware so full part numbers ("15-fd0014nx", "X1504VA-BQ4472WSM") survive
_BATT_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]+(?:-[A-Za-z0-9]+){0,3})(?![A-Za-z0-9])")


def _battery_tokens(rec: dict, want_chassis: bool) -> set[str]:
    """Part-number / chassis tokens from a record's text (tight patterns only).

    Both the full hyphenated token ("15-FD1037NX") and its segments ("B13WEKG"
    in "B13WEKG-1225PH") are considered, so a full part number never hides the
    chassis it also contains.
    """
    text = _all_specs(rec)
    apple = (rec.get("brand") or "").lower() == "apple" or "macbook" in (rec.get("name") or "").lower()
    patterns = _BATT_EXACT_PATTERNS if not want_chassis else _BATT_CHASSIS_PATTERNS
    out: set[str] = set()
    for m in _BATT_TOKEN.finditer(text):
        full = m.group(0).upper()
        if apple:
            compact = full.replace("-", "")
            if (5 <= len(compact) <= 20 and _BATT_APPLE.match(compact)
                    and not _BATT_TOKEN_JUNK.match(compact) and not _BATT_TOKEN_MENTIONS.search(compact)):
                out.add(compact)
            continue
        for t in [full, *(seg for seg in full.split("-") if seg != full)]:
            if not (5 <= len(t) <= 20):
                continue
            if not re.search(r"[A-Z]", t) or not re.search(r"\d", t):
                continue
            if _BATT_TOKEN_JUNK.match(t) or _BATT_TOKEN_MENTIONS.search(t):
                continue
            if any(p.match(t) for p in patterns):
                out.add(t)
                break
    return out


# --- curated spec-sheet lookup: capacities the stores never publish -----------
# data/battery_specs.json is hand-maintained from manufacturer spec sheets and
# carries a source URL per entry. Only one capacity per token is allowed; models
# sold with several battery options are listed as ambiguous and never applied.
_BATT_SPECS_PATH = DATA_DIR / "battery_specs.json"
_BATT_SPECS: dict | None = None


def _load_battery_specs() -> tuple[dict[str, tuple[float | None, int | None, str]], dict[str, tuple[float | None, int | None, str]], list, dict]:
    """(model_map, chassis_map, family_rules, stats) from data/battery_specs.json.

    Entries: {"wh": 60, "source": "https://..."} — a null wh, an explicit
    "ambiguous" flag or an out-of-range value keeps the token out of the usable
    maps and counts it. "families" is a list of {"match", "wh", "source"} where
    match is a case-insensitive regex tried against the listing text only after
    no part-number/chassis token matched (brands that publish no codes at all).
    """
    global _BATT_SPECS
    if _BATT_SPECS is None:
        try:
            _BATT_SPECS = json.loads(_BATT_SPECS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("battery_specs.json unavailable: %s", exc)
            _BATT_SPECS = {}
    models: dict[str, tuple[float | None, int | None, str]] = {}
    chassis: dict[str, tuple[float | None, int | None, str]] = {}
    stats = {"entries": 0, "skipped": 0, "invalid": 0}
    for kind, dest in (("models", models), ("chassis", chassis)):
        for token, entry in sorted((_BATT_SPECS.get(kind) or {}).items()):
            stats["entries"] += 1
            if not isinstance(entry, dict) or entry.get("ambiguous") or entry.get("wh") is None:
                stats["skipped"] += 1
                continue
            wh = entry.get("wh")
            if not isinstance(wh, (int, float)) or not 15 <= float(wh) <= 120:
                stats["invalid"] += 1
                log.warning("battery_specs: implausible wh for %s: %r", token, wh)
                continue
            dest[token.upper()] = (float(wh), entry.get("mah"), entry.get("source") or "")
    families: list[tuple[re.Pattern, float, str]] = []
    for entry in _BATT_SPECS.get("families") or []:
        stats["entries"] += 1
        try:
            wh = float(entry.get("wh"))
            pattern = re.compile(entry.get("match") or "", re.I)
            if not entry.get("match") or not 15 <= wh <= 120:
                raise ValueError(entry)
        except Exception:  # noqa: BLE001
            stats["invalid"] += 1
            log.warning("battery_specs: bad family entry: %r", entry)
            continue
        families.append((pattern, wh, entry.get("source") or ""))
    return models, chassis, families, stats


def apply_battery_lookup(records: list[dict]) -> dict:
    """Fill missing capacity from identical model numbers / agreeing chassis.

    Two pools feed the lookup: every store listing that published a capacity
    (provenance `model-match` / `chassis-match`) and the curated manufacturer
    spec sheets in data/battery_specs.json (provenance `spec-model` /
    `spec-chassis`, with the source URL attached). A target record is only
    filled when a token maps to exactly one value across both pools; ambiguous
    tokens (e.g. chassis 15IRX10 = 60Wh on the LOQ but 80Wh elsewhere) are
    skipped, never guessed.
    """
    spec_models, spec_chassis, spec_families, spec_stats = _load_battery_specs()

    exact_pool: dict[str, set] = {}
    chassis_pool: dict[str, set] = {}
    for rec in records:
        wh, mah = rec.get("battery_wh"), rec.get("battery_mah")
        if not (wh or mah):
            continue
        value = (wh, mah)
        for t in _battery_tokens(rec, want_chassis=False):
            exact_pool.setdefault(t, set()).add(value)
        for t in _battery_tokens(rec, want_chassis=True):
            chassis_pool.setdefault(t, set()).add(value)
    exact_ok = {t: next(iter(v)) for t, v in exact_pool.items() if len(v) == 1}
    chassis_ok = {t: next(iter(v)) for t, v in chassis_pool.items() if len(v) == 1 and t not in exact_ok}
    ambiguous = sum(1 for v in list(exact_pool.values()) + list(chassis_pool.values()) if len(v) > 1)

    # spec-sheet values join only when they do not contradict the listings;
    # a conflict (same token, different capacity) is treated as ambiguous
    def spec_ok(specs: dict, published: dict, conflicts: list) -> dict:
        ok: dict[str, tuple] = {}
        for t, entry in specs.items():
            if t in published:
                if published[t] != entry[:2]:
                    conflicts.append(t)
                else:
                    continue  # listings carry their own provenance
                continue
            ok[t] = entry
        return ok
    spec_conflicts: list[str] = []
    exact_spec = spec_ok(spec_models, exact_ok, spec_conflicts)
    chassis_spec = {t: e for t, e in spec_ok(spec_chassis, chassis_ok, spec_conflicts).items()
                    if t not in exact_ok and t not in exact_spec}

    stats = {"model": 0, "chassis": 0, "spec_model": 0, "spec_chassis": 0, "spec_family": 0,
             "ambiguous": ambiguous, "spec_entries": spec_stats["entries"],
             "spec_skipped": spec_stats["skipped"], "spec_invalid": spec_stats["invalid"],
             "spec_conflicts": len(spec_conflicts)}
    for rec in records:
        if rec.get("battery_wh") or rec.get("battery_mah"):
            continue
        match = None
        for t in _battery_tokens(rec, want_chassis=False):
            if t in exact_ok:
                match = ("model-match", (*exact_ok[t], ""))
                break
            if t in exact_spec:
                match = ("spec-model", exact_spec[t])
                break
        if not match:
            for t in _battery_tokens(rec, want_chassis=True):
                if t in chassis_ok:
                    match = ("chassis-match", (*chassis_ok[t], ""))
                    break
                if t in chassis_spec:
                    match = ("spec-chassis", chassis_spec[t])
                    break
        if not match:  # brands that never publish model codes (Surface, MateBook...)
            hay = f"{rec.get('name') or ''} {rec.get('short_description') or ''} {rec.get('description') or ''}"
            for rx, wh, source_url in spec_families:
                if rx.search(hay):
                    match = ("spec-family", (wh, None, source_url))
                    break
        if not match:
            continue
        source, (wh, mah, source_url) = match
        rec["battery_wh"], rec["battery_mah"] = wh, mah
        rec["battery_detail"] = " · ".join(filter(None, [
            f"{wh:g} Wh" if wh else "", f"{mah:,} mAh" if mah else ""])) or None
        rec["battery_label"] = " · ".join(filter(None, [
            f"{wh:g} Wh" if wh else (f"{mah:,} mAh" if mah else ""),
            _battery_chem_label(rec.get("battery")),
        ])) or rec["battery_detail"]
        rec["battery_source"] = source
        rec["battery_source_url"] = source_url or None
        rec["battery_points"] = battery_points_for(wh)
        if source == "model-match":
            stats["model"] += 1
        elif source == "chassis-match":
            stats["chassis"] += 1
        elif source == "spec-model":
            stats["spec_model"] += 1
        elif source == "spec-chassis":
            stats["spec_chassis"] += 1
        else:
            stats["spec_family"] += 1
    return stats


# --- estimated layer: similar-listing inference for the tokens-less tail -------
# Only ever applied to records still empty after the published/spec lookups, and
# always labelled `estimated` in battery_source/label so it can never be mistaken
# for a published value. Estimates earn capacity points like any other battery
# so unlisted retailer listings are not penalized for missing documentation.
_EST_LINE_STOPWORDS = {
    "laptop", "notebook", "gaming", "with", "inch", "the", "and", "new", "version",
    "upgraded", "ai", "pc", "convertible", "touch", "essential", "series", "refresh",
    "windows", "dos", "oled", "ips", "fhd", "wuxga", "wqxga", "2", "in", "1",
}
_EST_SCREEN_NUMBERS = {"11", "12", "13", "14", "15", "16", "17", "18"}
_EST_MIN_SAMPLES = 3


def _est_line_words(rec: dict) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9]{1,}", (rec.get("short_name") or "").lower())
    return [
        w for w in words
        if w not in _EST_LINE_STOPWORDS
        and not (w.isdigit() and w in _EST_SCREEN_NUMBERS)
    ]


def _est_class(rec: dict) -> str:
    if (rec.get("gpu") or {}).get("class") == "dedicated" or "Gaming" in (rec.get("tags") or []):
        return "gaming"
    if (rec.get("build_score") or 0) >= 8:
        return "premium"
    return "office"


def apply_battery_estimates(records: list[dict]) -> dict:
    """Fill the still-empty tail from the mode of similar known listings.

    Levels, first with >= _EST_MIN_SAMPLES known values wins:
      brand + two line words + screen size, brand + first line word + size,
      product class + brand + size. The mode (ties -> median) becomes
    `battery_wh` with battery_source="estimated", a note describing the pool,
    and zero performance points.
    """
    known = [r for r in records if r.get("battery_wh")]
    pools: dict[tuple, list[float]] = {}
    for r in known:
        brand = (r.get("brand") or "?").lower()
        size = (r.get("screen") or {}).get("inches")
        size = round(size) if size else None
        words = _est_line_words(r)
        for key in (
            ("line2", brand, tuple(words[:2]), size),
            ("line1", brand, words[0] if words else "", size),
            ("class", _est_class(r), brand, size),
            ("line2ns", brand, tuple(words[:2]), None),
            ("line1ns", brand, words[0] if words else "", None),
            ("brandsize", brand, None, size),
        ):
            pools.setdefault(key, []).append(r["battery_wh"])

    stats = {"estimated": 0, "line2": 0, "line1": 0, "class": 0,
             "line2ns": 0, "line1ns": 0, "brandsize": 0, "unmatched": 0}
    for rec in records:
        if rec.get("battery_wh") or rec.get("battery_mah"):
            continue
        brand = (rec.get("brand") or "?").lower()
        size = (rec.get("screen") or {}).get("inches")
        size = round(size) if size else None
        words = _est_line_words(rec)
        match = None
        for level, key, label, min_n in (
            ("line2", ("line2", brand, tuple(words[:2]), size), " ".join(words[:2]), _EST_MIN_SAMPLES),
            ("line1", ("line1", brand, words[0] if words else "", size), words[0] if words else "", _EST_MIN_SAMPLES),
            ("class", ("class", _est_class(rec), brand, size), f"{_est_class(rec)} {brand}", _EST_MIN_SAMPLES),
            ("line2ns", ("line2ns", brand, tuple(words[:2]), None), " ".join(words[:2]), _EST_MIN_SAMPLES),
            ("line1ns", ("line1ns", brand, words[0] if words else "", None), words[0] if words else "", _EST_MIN_SAMPLES),
            ("brandsize", ("brandsize", brand, None, size), brand, 5),
        ):
            pool = pools.get(key)
            if pool and len(pool) >= min_n:
                counts = sorted(((pool.count(v), v) for v in set(pool)), reverse=True)
                wh = counts[0][1] if counts[0][0] > 1 else statistics.median(pool)
                match = (level, round(float(wh), 1), len(pool), label)
                break
        if not match:
            stats["unmatched"] += 1
            continue
        level, wh, n, label = match
        rec["battery_wh"] = wh
        rec["battery_label"] = " · ".join(filter(None, [
            f"~{wh:g} Wh", _battery_chem_label(rec.get("battery"))])) or f"~{wh:g} Wh"
        rec["battery_detail"] = f"~{wh:g} Wh (estimated)"
        rec["battery_source"] = "estimated"
        rec["battery_source_url"] = None
        rec["battery_estimate"] = (
            f"mode of {n} similar {label} {size or ''}\" listings".replace('  ', ' ').strip())
        rec["battery_points"] = battery_points_for(wh)
        stats["estimated"] += 1
        stats[level] += 1
    return stats


# ---------------------------------------------------------------------------
# RAM / storage
# ---------------------------------------------------------------------------

_RAM_SIZES = {2, 4, 6, 8, 12, 16, 18, 20, 24, 32, 36, 48, 64, 96, 128, 192}


def parse_ram(rec: dict) -> tuple[int | None, str | None]:
    # The title states the configured RAM (".../64GB RAM DDR5/1TB SSD/...") —
    # prefer it over spec tables, which sometimes carry the GPU's VRAM or a
    # maximum (noon lists "RAM: 16 GB" for a 64 GB Omen because that row was
    # the RTX 5080's 16 GB).  "16GBx2 RAM" / "2x16GB RAM" multiply out.  A
    # title value that isn't a real capacity (noon's "34GB RAM" Surface) falls
    # back to the spec table.
    name = rec.get("name") or ""
    spec_raw = _first_text(rec, "RAM", "RAM SIZE", "Memory", "capa") or ""
    m = (re.search(r"\b(\d{1,3})\s*G(?:B)?\s*[x×]\s*(\d)\s*RAM\b", name, re.I)
         or re.search(r"\b(\d)\s*[x×]\s*(\d{1,3})\s*G(?:B)?\s*RAM\b", name, re.I))
    title_gb = int(m.group(1)) * int(m.group(2)) if m else None
    if title_gb is None:
        m = re.search(r"\b(\d{1,3})\s*G(?:B)?\s*RAM\b", name, re.I)
        if m:
            title_gb = int(m.group(1))
    sm = re.search(r"(\d{1,3})\s*(GB|TB)", spec_raw, re.I)
    spec_gb = int(sm.group(1)) * (1024 if sm.group(2).upper() == "TB" else 1) if sm else None
    if title_gb is not None and (title_gb in _RAM_SIZES or spec_gb is None):
        raw = f"{title_gb}GB"
    else:
        raw = spec_raw
    # a "Memory: Up to 64GB" spec-table value is the ceiling, not what's fitted:
    # recover the installed capacity from the title, else leave it unknown
    if raw and re.match(r"\s*(up to|max(?:imum)?)\b", raw, re.I):
        m = re.search(r"\b(\d{1,3})\s*GB\s*[x×]\s*(\d)\b", name, re.I)
        if m:
            raw = f"{int(m.group(1)) * int(m.group(2))}GB"
        else:
            m = re.search(r"\b(\d{1,3})\s*GB\s*RAM\b", name, re.I)
            raw = m.group(0) if m else ""
    # "Station: 32 GB RAM/Tab: 12 GB RAM" -> first value
    m = re.search(r"(\d{1,3})\s*(GB|TB)", raw, re.I)
    gb = None
    if m:
        gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
    ram_type = _first_text(rec, "RAM Type", "Memory Type")
    if not ram_type:
        joined = _all_specs(rec)
        m = re.search(r"\b(LPDDR\d[X]?|DDR\d[X]?)\b", joined, re.I)
        if m:
            ram_type = m.group(1).upper()
    # GDDR is GPU memory that some specs label as "Memory Type"; never RAM
    if ram_type and ram_type.upper().startswith("GDDR"):
        ram_type = None
    return gb, ram_type.upper() if ram_type else None


def parse_storage(rec: dict) -> tuple[int | None, str | None]:
    texts = [
        _first_text(rec, "capacity", "Total Storage (Capacity)", "Solid State Drive Capacity", "Storage"),
        _first_text(rec, "Hard Drive Capacity"),
    ]
    best_gb = None
    best_type = None
    for text in texts:
        if not text:
            continue
        for m in re.finditer(r"(\d{1,4}(?:\.\d)?)\s*(GB|TB|MB)", text, re.I):
            size = float(m.group(1))
            unit = m.group(2).upper()
            gb = size * 1024 if unit == "TB" else (size / 1024 if unit == "MB" else size)
            if gb < 1:  # "512 MB" is a Jarir typo for 512 GB
                gb = size
            if best_gb is None or gb > best_gb:
                best_gb = gb
        if re.search(r"SSD|NVMe|PCIe|M\.2", text, re.I):
            best_type = "SSD"
        elif re.search(r"HDD", text, re.I):
            best_type = best_type or "HDD"
        elif re.search(r"eMMC", text, re.I):
            best_type = best_type or "eMMC"
    if best_gb is not None:
        best_gb = int(best_gb)
    if best_type is None and best_gb:
        best_type = "SSD"
    return best_gb, best_type


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------

def parse_gpu(rec: dict) -> dict:
    out = {"text": None, "brand": None, "model": None, "class": None, "vram": None, "score": None, "inferred": False}
    text = _first_text(rec, "graphics card", "Graphics Controller Model") or ""
    extra_brand = _first_text(rec, "Graphics Controller Brand") or ""
    class_text = _first_text(rec, "Graphics Memory (Integrated/Dedicated)") or ""
    combined = f"{text} {extra_brand}".strip()
    joined = _all_specs(rec)
    out["text"] = text or extra_brand or None
    if not combined:
        return out

    is_integrated_label = bool(re.fullmatch(r"\s*integrated\s*", text, re.I))
    if re.search(r"nvidia|geforce|rtx|gtx", combined, re.I) and not is_integrated_label:
        out["brand"] = "NVIDIA"
    elif re.search(r"radeon|amd", combined, re.I):
        out["brand"] = "AMD"
    elif re.search(r"qualcomm|adreno", combined, re.I):
        out["brand"] = "Qualcomm"
    elif re.search(r"apple", combined, re.I) or re.search(r"\d+[- ]core\s+gpu", combined, re.I):
        out["brand"] = "Apple"
    elif re.search(r"intel|arc|uhd|iris", combined, re.I):
        out["brand"] = "Intel"

    m = re.search(r"\b(RTX\s?\d{3,4}\s?(?:Ti|Super)?|GTX\s?\d{3,4}\s?(?:Ti|Super)?)\b", combined, re.I)
    if m:
        out["model"] = re.sub(r"\s+", " ", m.group(1)).replace("  ", " ").strip()
        out["model"] = re.sub(r"^(rtx|gtx)", lambda mm: mm.group(1).upper(), out["model"], flags=re.I)
    if not out["model"]:
        m = re.search(r"\b(Radeon\s+[\w]+(?:\s+\w+)?|Arc\s+[\w]+\s?[\w]*|Adreno[\w ]*|\d+[- ]core\s+GPU|UHD Graphics|Iris Xe Graphics)\b", combined, re.I)
        if m:
            out["model"] = re.sub(r"\s+", " ", m.group(1)).strip()
            out["model"] = re.sub(r"(\d+)\s+core", r"\1-core", out["model"], flags=re.I)

    dedicated = bool(re.search(r"nvidia|geforce|rtx|gtx|rx\s?\d|radeon rx", combined, re.I))
    if class_text:
        dedicated = "dedicated" in class_text.lower()
    if is_integrated_label:
        dedicated = False
    if re.search(r"\bintegrated\b", combined, re.I) and not re.search(r"nvidia|rtx|gtx", text, re.I):
        dedicated = False
    out["class"] = "dedicated" if dedicated else "integrated"

    # VRAM
    m = re.search(r"(\d{1,2})\s*GB(?:\s*GDDR\d)?", text)
    if not m:
        cap = _first_text(rec, "Graphics Memory Capacity") or ""
        m = re.search(r"(\d{1,2})\s*GB", cap)
    if m:
        out["vram"] = int(m.group(1))

    # score comes from real PassMark benchmarks (see apply_benchmarks above),
    # never from a heuristic
    out["score"] = None
    if out["model"]:
        out["model"] = _pretty_gpu_model(out["model"])
        out["model"] = re.sub(r"(\d+)-Core\b", lambda m: f"{m.group(1)}-core", out["model"])
    return out


GPU_WORD_CASE = {
    "radeon": "Radeon", "geforce": "GeForce", "arc": "Arc", "adreno": "Adreno",
    "intel": "Intel", "amd": "AMD", "nvidia": "NVIDIA", "uhd": "UHD",
    "graphics": "Graphics", "gpu": "GPU", "iris": "Iris", "xe": "Xe",
    "vega": "Vega",
}


def _pretty_gpu_model(model: str) -> str:
    text = re.sub(r"\s+", " ", model).strip()
    # 610m -> 610M, 140v -> 140V, 275hx -> 275HX
    text = re.sub(r"(\d)([a-z]{1,3})(?=$| )", lambda mm: mm.group(1) + mm.group(2).upper(), text)
    words = [GPU_WORD_CASE.get(w.lower(), w) for w in text.split(" ")]
    return " ".join(words)


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

def parse_screen(rec: dict) -> dict:
    out = {"inches": None, "width": None, "height": None, "res": None, "res_label": None,
           "panel": None, "hz": None, "touch": False}

    size_text = _first_text(rec, "display size", "Screen Size") or ""
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:\"|”|in\b|inch|inches)", size_text, re.I)
    if not m:
        m = re.search(r"(\d{1,2}[.,]\d|\b1[2-8]\b)", size_text)
    if m:
        out["inches"] = float(m.group(1).replace(",", "."))

    res_text = _first_text(rec, "display resolution", "Screen Resolution", "Display Resolution") or ""
    m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,4})", res_text, re.I)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        # noon typo class: "19201x1080" — an extra digit slipped into the width.
        # Collapse implausible 5-digit dimensions back to their 4-digit form.
        if w > 5000 and 1000 <= w // 10 <= 4999:
            w //= 10
        if h > 5000 and 1000 <= h // 10 <= 4999:
            h //= 10
        if h > w:  # portrait
            w, h = h, w
        out["width"], out["height"] = w, h
        out["res"] = f"{w}×{h}"
        out["res_label"] = _res_label(w, h)

    type_text = " ".join(filter(None, [_first_text(rec, "display type", "Display Type", "Screen Type"),
                                       _first_text(rec, "display resolution")]))
    joined = _all_specs(rec)
    search_text = f"{type_text} {joined} {rec.get('name') or ''} {rec.get('short_description') or ''} {rec.get('model') or ''}"
    if re.search(r"\boled\b", search_text, re.I):
        out["panel"] = "OLED"
    elif re.search(r"mini[- ]?led|\bxdr\b", search_text, re.I):
        out["panel"] = "Mini-LED"
    elif re.search(r"liquid retina|retina", search_text, re.I):
        out["panel"] = "Retina"
    elif re.search(r"\bips\b", search_text, re.I):
        out["panel"] = "IPS"
    elif re.search(r"\bva\b", search_text, re.I):
        out["panel"] = "VA"
    elif re.search(r"\btn\b|twisted nematic", search_text, re.I):
        out["panel"] = "TN"
    elif re.search(r"pixelsense", search_text, re.I):
        out["panel"] = "PixelSense"

    hz_text = " ".join(filter(None, [_first_text(rec, "Screen Refresh Rate"), type_text]))
    m = re.search(r"(\d{2,3})\s*hz", hz_text, re.I)
    if m and 30 <= int(m.group(1)) <= 600:
        out["hz"] = int(m.group(1))

    touch = _first_text(rec, "touch display", "Touchscreen", "Touch Screen")
    if touch and touch.strip().lower().startswith("yes"):
        out["touch"] = True
    if re.search(r"\bmulti[- ]touch|touch display|\btouch\b", type_text, re.I):
        out["touch"] = True
    return out


def _res_label(w: int, h: int) -> str:
    if w >= 3840:
        return "4K UHD"
    if w >= 3200:
        return "3K+"
    if w >= 2880:
        return "3K"
    if w >= 2560:
        return "QHD/WQXGA"
    if w >= 2304:
        return "2.3K"
    if w >= 2160:
        return "2K"
    if w >= 1920:
        return "WUXGA" if h >= 1200 else "FHD"
    if w >= 1600:
        return "HD+"
    return "HD"


# ---------------------------------------------------------------------------
# OS / weight / misc
# ---------------------------------------------------------------------------

def parse_os(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    if "win" in low:
        version = "Windows 11" if "11" in low else ("Windows 10" if "10" in low else "Windows")
        if "pro" in low:
            return f"{version} Pro"
        if "home" in low:
            return f"{version} Home"
        return version
    if "mac" in low:
        return "macOS"
    if "dos" in low or low in ("no os", "no"):
        return "DOS (no OS)"
    if "chrome" in low:
        return "ChromeOS"
    if "android" in low:
        return "Android"
    if "linux" in low or "ubuntu" in low:
        return "Linux"
    return t


WEIGHT_MIN_KG, WEIGHT_MAX_KG = 0.3, 5.5   # plausible laptop mass; outside = bad data


def parse_weight(text: str | None) -> float | None:
    if not text:
        return None
    t = text.lower()
    if t.strip() in JUNK:
        return None
    # Jarir writes sub-1 kg specs without the leading zero (".98 kg"), so the
    # number pattern must accept a leading dot/comma — otherwise ".98" reads as 98.
    m = re.search(r"((?:\d+(?:[.,]\d+)?|[.,]\d+))\s*kg", t)
    if m:
        kg = float(m.group(1).replace(",", "."))
    else:
        m = re.search(r"((?:\d+(?:[.,]\d+)?|[.,]\d+))\s*(?:g(?!b)|grams?)", t)
        if not m:
            return None
        kg = float(m.group(1).replace(",", ".")) / 1000
    # Stores sometimes publish boxed/shipping weight (eXtra: "35.4 KG") or a
    # corrupted figure. Unknown beats wrong: drop anything outside a laptop's
    # physical range.
    if not (WEIGHT_MIN_KG <= kg <= WEIGHT_MAX_KG):
        return None
    return round(kg, 3)


def parse_warranty(rec: dict) -> float | None:
    value = rec.get("warranty_years")
    if value:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return None


GENERIC_GPU = re.compile(
    r"^(integrated( intel graphics| amd radeon graphics)?|intel graphics|amd radeon graphics|"
    r"radeon graphics|dedicated|shared|qualcomm adreno gpu|apple integrated gpu|"
    r"intel integrated graphics|intel uma graphics)$",
    re.I,
)


def _pretty_cpu_model(text: str) -> str:
    """Normalise model casing: 'ultra 7-255u' -> 'Ultra 7-255U', 'i7 1355u' -> 'i7 1355U'."""
    s = re.sub(r"\s+", " ", text).strip()
    s = re.sub(r"^(ultra|core|ryzen|intel|amd)\b", lambda mm: mm.group(1).title(), s, flags=re.I)
    s = re.sub(r"^Core\s+I(?=\d)", "Core i", s)
    return re.sub(r"(\d)([a-z]{1,3})$", lambda mm: mm.group(1) + mm.group(2).upper(), s)


def cpu_is_generic(cpu: dict) -> bool:
    """True when the CPU has no model code (e.g. just "Ryzen 5" or "Core 3")."""
    return not re.search(r"\d", cpu.get("model") or "")


def cpu_detail(text: str | None) -> int:
    """Length of the longest model-code token — "120U" beats "120"."""
    tokens = re.findall(r"\b[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b", text or "")
    return max((len(t) for t in tokens), default=0)


def gpu_is_generic(gpu: dict) -> bool:
    """True for inferred GPUs or vendor-level placeholders such as "Integrated"."""
    if gpu.get("inferred"):
        return True
    if not gpu.get("model"):
        return True
    return bool(GENERIC_GPU.match((gpu.get("text") or "").strip()))


def specs_from_title(name: str | None) -> dict:
    """Best-effort spec extraction from the product title.

    Used as a fallback for marketplace items whose merchant data is sparse
    (their titles are usually very descriptive: 'i7-14650hx, 16gb ram, 1tb ssd').
    """
    out: dict = {}
    t = re.sub(r"\s+", " ", re.sub(r"[™®©]", " ", (name or "").lower()))
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)      # – — − -> -
    if not t:
        return out
    # "8GB+256GB" style (Apple/ultrabook titles) -> RAM + storage in one token
    m = re.search(r"\b(\d{1,3})\s?gb\s*\+\s*(\d{1,4})\s?gb\b", t)
    if m:
        out["ram"] = int(m.group(1))
        out["storage"] = int(m.group(2))

    # storage (so a bare "512gb" RAM fallback can't steal it)
    m = (None if out.get("storage") else
         re.search(r"(\d{1,4})\s?(?:gb)?\s*(?:pcie\s*)?(?:nvme\s*)?(?:m\.?2\s*)?(?:ssd|sdd|hdd|emmc|ufs|storange|storage)", t)
         or re.search(r"(\d(?:\.\d)?)\s?tb\s*(?:ssd|sdd|nvme|hdd|pcie)?", t)
         or re.search(r"(?:^|[/,|])\s*(\d{3,4})\s?gb\s*(?=[/,|]|$)", t))
    if m:
        value = float(m.group(1))
        out["storage"] = int(value * 1024) if "tb" in m.group(0) else int(value)

    # RAM: "16gb ddr5 ram", "16 ram ddr5", "8 ddr4 ram", "16gb ddr5", "/8gb/"
    m = (re.search(r"(\d{1,3})\s*(?:gb|g)?\s*(?:ddr\d[x]?|lpddr\d[x]?)?\s*ram\b", t)
         or re.search(r"(\d{1,3})\s?gb\s+(?:ddr\d[x]?|lpddr\d[x]?)", t)
         or re.search(r"(?:^|[/,|])\s*(\d{1,3})\s?gb\s*(?=[/,|]|$)", t))
    if m:
        ram = int(m.group(1))
        if not (out.get("storage") == ram and len(m.group(1)) >= 3):  # not the storage value
            out["ram"] = ram
    if "ram" not in out:
        # last resort: a plausible GB value that is not the storage and not GPU VRAM
        for mm in re.finditer(r"(\d{1,3})\s?gb", t):
            value = int(mm.group(1))
            if value == out.get("storage") or not (4 <= value <= 192):
                continue
            before = t[max(0, mm.start() - 14):mm.start()]
            after = t[mm.end():min(len(t), mm.end() + 14)]
            if re.search(r"rtx|gtx|geforce|radeon|arc|graphics|vram", before):
                continue
            if re.search(r"ufs|msd|emmc|ssd|hdd|rom", after):
                continue
            out["ram"] = value
            break
    cpu_patterns = (
        r"apple\s+a\d{1,2}(?:\s+(?:pro|max))?"
        r"|a\d{1,2}\s+(?:pro|max)?\s*(?:chip|bionic)",
        r"(?:apple\s+)?m\d\s+(?:pro|max|ultra)\s+(?:chip|processor)",
        r"m\d\s+(?:chip|processor)",
        r"apple\s+m\d(?:\s+(?:pro|max|ultra))?",
        r"(?:intel\s+)?core[-\s]+(?:ultra[-\s]+)?i?[3579](?:[-\s]\d{3,4}[a-z0-9]{0,6})?",
        r"(?:intel\s+)?ultra\s+[3579](?:[-\s]\d{3}[a-z0-9]{0,6})?",
        r"\bi[3579]-\d{3,5}[a-z0-9]{0,6}",
        r"\bu[3579][-\s]\d{3}[a-z0-9]{0,6}",
        r"(?:intel\s+)?xeon\s+[we]-?\d{4,5}[a-z0-9]{0,4}",
        r"(?:intel\s+)?celeron(?:\s+n?\d{3,4})?",
        r"(?:intel\s+)?pentium(?:\s+(?:gold|silver))?(?:\s+\d{3,5}[a-z]?)?",
        r"\bn\d{3,4}\b",
        r"(?:amd\s+)?ryzen\s+(?:ai\s+)?(?:max\+\s*\d{3}|[3579][-\s]*(?:hx\s*)?[-\s]?\d{2,4}[a-z0-9]{0,8}|[3579])",
        r"(?:amd\s+)?mendocino\s+r[3579]",
        r"(?:amd\s+)?athlon(?:\s+(?:silver|gold))?(?:\s+\d{1,4}[a-z]?)?",
        r"snapdragon(?:\s+x2?\s*(?:elite|plus)?(?:\s+[\w-]+)?)?",
    )
    for pattern in cpu_patterns:
        m = re.search(pattern, t, re.I)
        if m:
            out["cpu"] = m.group(0).strip()
            break
    m = re.search(
        r"(rtx\s?\d{4}\s?(?:ti|super)?|gtx\s?\d{4}"
        r"|radeon\s+(?:rx\s?)?\d{3,4}[a-z]*(?:\s+graphics)?"
        r"|radeon\s+vega(?:\s+graphics)?"
        r"|(?:amd\s+)?radeon\s+graphics"
        r"|intel\s+(?:arc\s+[\w\d]+(?:\s+graphics)?|iris\s+xe(?:\s+graphics)?|uhd(?:\s+graphics)?)"
        r"|adreno(?:\s+gpu)?|(?:apple\s+)?\d+[- ]core\s+gpu)",
        t,
    )
    if m:
        out["gpu"] = m.group(1)
    m = re.search(r"(\d{1,2}(?:\.\d)?)\s?(?:inch|inches|\")", t)
    if m:
        out["inches"] = float(m.group(1))
    if re.search(r"win(dows)?\s?11", t):
        out["os"] = "Windows 11"
    elif re.search(r"mac ?os|os x|big sur|monterey|ventura|sonoma|sequoia|tahoe", t):
        out["os"] = "macOS"
    elif re.search(r"chrome ?os|chromebook", t):
        out["os"] = "Chrome OS"
    elif re.search(r"\bdos\b|no windows|without windows", t):
        out["os"] = "DOS (no OS)"
    return out


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

_DBT_SPEC_LABELS = {
    "chip": "Processor Type", "processor": "Processor Type", "cpu": "Processor Type",
    "memory": "RAM", "ram": "RAM", "total system memory": "RAM",
    "storage": "Storage", "storage capacity": "Storage",
    "display": "Display", "screen": "Display", "screen size": "Screen Size",
    "panel features": "Panel Features",
    "model": "Model Number", "model number": "Model Number",
    "operating system": "Operating System", "os": "Operating System",
    "graphics": "Graphics Card", "graphics card": "Graphics Card",
    "size and weight": "Weight", "weight": "Weight",
    "battery and power": "Battery Type", "battery": "Battery Type",
}


def _datablitz_spec_block(description: str | None) -> dict:
    """Parse the label / value list DataBlitz puts in its product descriptions.

    Two layouts exist: the older one starts at a "Specifications:" header, the
    ASUS-style one interleaves section headers ("System & Processor", "Display &
    Memory" …).  Scan every line for a known label instead of requiring the
    header, and skip section headers when picking up the value.
    """
    lines = [line.strip() for line in (description or "").splitlines()]
    out: dict = {}
    i = 0
    while i < len(lines):
        label = lines[i].lower().rstrip(":").strip()
        if label in _DBT_SPEC_LABELS:
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            value = lines[j].strip() if j < len(lines) else ""
            is_section = " & " in value or value.endswith("&") or value.lower().rstrip(":") in _DBT_SPEC_LABELS
            if value and not is_section:
                out.setdefault(_DBT_SPEC_LABELS[label], value)
                i = j + 1
                continue
        i += 1
    return out


def specs_from_pcx(description: str | None) -> dict:
    """Specs for a PC Express (Shopify) listing.

    Their bodies are flat ``SPECIFICATION Operating System <os> Processor <cpu>
    Graphics <gpu> Display <screen> Memory <ram> Storage <disk> …`` runs with no
    delimiters, so slice the text between known labels (first occurrence wins,
    values capped — a missing label just ends the previous value).
    """
    out: dict = {}
    text = re.sub(r"\s+", " ", description or "").strip()
    if not text:
        return out
    labels = ["Operating System", "Processor", "Graphics", "Display", "Memory",
              "Storage", "Model", "Weight", "Battery", "Warranty"]
    # NB: no "Color" — it fires inside "color gamut" and shreds the Display value
    hits = []
    for label in labels:
        m = re.search(r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", text, re.I)
        if m:
            hits.append((m.start(), m.end(), label))
    hits.sort()
    for i, (start, end, label) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        value = text[end:stop].strip(" :|-")[:150].strip()
        if value:
            out.setdefault(label, value)
    if "Memory" in out:
        out.setdefault("RAM", out["Memory"])
    if "Processor" in out:
        out.setdefault("Processor Type", out["Processor"])
    # parse_screen reads Screen Size / Display Resolution / Display Type, so
    # split the Display blob for it ("15.6” Full-HD" / "1920x1080" / "IPS")
    disp = out.get("Display", "")
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:\"|”|inch)", disp, re.I)
    if m and not re.search(r"^\d{3,}$", m.group(1)):
        out.setdefault("Screen Size", f"{m.group(1).replace(',', '.')} inch")
    m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,4})", disp)
    if m:
        out.setdefault("Display Resolution", f"{m.group(1)}x{m.group(2)}")
    m = re.search(r"\b(OLED|Mini-LED|IPS|VA|TN)\b", disp, re.I)
    if m:
        out.setdefault("Display Type", m.group(1).upper())
    m = re.search(r"(\d{2,3})\s*hz", disp, re.I)
    if m:
        out.setdefault("Screen Refresh Rate", f"{m.group(1)}Hz")
    return out


def specs_from_datablitz(name: str | None, description: str | None = None) -> dict:
    """Specs for a DataBlitz (Shopify) listing.

    Their titles look like ``Asus Vivobook 15 X1504VA-BQ4472WSM Laptop (Quiet Blue)
    | 15.6" FHD (1920x1080) IPS | Core 5 120U | 8GB RAM | 512GB SSD | Intel Graphics
    | Windows 11 Home | ...`` — the pipe-separated segments after the model carry the
    screen details and OS, while CPU/RAM/storage/GPU are picked up by the title
    fallback like marketplace items.  Weight and battery come from the description.
    """
    out: dict = {}
    segments = [s.strip() for s in re.split(r"\s*\|\s*", name or "") if s.strip()]
    headline = segments[0] if segments else ""

    # the description often carries a proper spec table ("Specifications:" then
    # alternating label / value lines) — that is where Apple listings keep their
    # chip, memory, display, weight and OS
    for key, value in _datablitz_spec_block(description).items():
        out.setdefault(key, value)
    display = out.pop("Display", None)
    feats = out.get("Panel Features") or ""
    disp_all = " ".join(filter(None, [display, feats]))
    if display:
        m = re.search(r"([\d.]+)\s*(?:[- ]?inch|[""”])", display, re.I)
        if m:
            out.setdefault("Screen Size", m.group(0))
        m = re.search(r"(\d{3,5})\s*(?:-by-|[x×])\s*(\d{3,4})", display, re.I)
        if m:
            out.setdefault("Display Resolution", f"{m.group(1)}x{m.group(2)}")
    if disp_all:
        if re.search(r"\bxdr\b", disp_all, re.I):
            out.setdefault("Display Type", "Mini-LED")
        elif re.search(r"liquid retina|retina", disp_all, re.I):
            out.setdefault("Display Type", "Retina")
        elif re.search(r"\boled\b", disp_all, re.I):
            out.setdefault("Display Type", "OLED")
        elif re.search(r"\bips\b", disp_all, re.I):
            out.setdefault("Display Type", "IPS")
        elif re.search(r"\bva\b", disp_all, re.I):
            out.setdefault("Display Type", "VA")
        elif re.search(r"\btn\b", disp_all, re.I):
            out.setdefault("Display Type", "TN")
        m = re.search(r"(\d{2,3})\s*hz", disp_all, re.I)
        if m:
            out.setdefault("Screen Refresh Rate", f"{m.group(1)}Hz")

    for seg in segments[1:]:
        low = seg.lower()
        if "Screen Size" not in out:
            m = (re.search(r'\d{1,2}(?:\.\d)?\s*(?:"|”|inch|inches)', seg, re.I)
                 or re.match(r"\s*(\d{1,2}(?:\.\d)?)\s+(?=(?:wuxga|wqxga|fhd|qhd|uhd|2k|3k|4k|oled|ips|va|tn|touch))", seg, re.I))
            if m:
                out["Screen Size"] = m.group(0)
        if "Display Resolution" not in out:
            m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,4})", seg)
            if m:
                out["Display Resolution"] = f"{m.group(1)}x{m.group(2)}"
        if "Display Type" not in out:
            if re.search(r"oled", low):
                out["Display Type"] = "OLED"
            elif re.search(r"mini-?led|\bxdr\b", low):
                out["Display Type"] = "Mini-LED"
            elif re.search(r"\bips\b", low):
                out["Display Type"] = "IPS"
            elif re.search(r"\bva\b", low):
                out["Display Type"] = "VA"
            elif re.search(r"\btn\b", low):
                out["Display Type"] = "TN"
        if "Screen Refresh Rate" not in out:
            hm = re.search(r"(\d{2,3})\s*hz", low)
            if hm:
                out["Screen Refresh Rate"] = f"{hm.group(1)}Hz"
        if "Touch Display" not in out and re.search(r"\btouch", low):
            out["Touch Display"] = "Yes"
        if "Operating System" not in out and re.search(r"windows\s+\d+|macos|\bdos\b|no os|freedos", low):
            out["Operating System"] = seg

    m = re.search(r"\b([A-Z0-9]{1,12}(?:-[A-Z0-9][A-Z0-9/.]{2,}){1,3})\b", headline)
    if m:
        out["Model Number"] = m.group(1)

    body = description or ""
    m = (re.search(r"weight[:\s]+([\d.]+)\s*(?:kg|kilograms?)", body, re.I)
         or re.search(r"([\d.]+)\s*(?:kg|kilograms?)\s+weight", body, re.I))
    if m:
        out["Weight"] = f"{m.group(1)} kg"
    # description prose ("Battery: 3-cell, 41 Wh Li-ion") — only accept values
    # that actually look like battery specs, or the label line itself gets eaten
    m = re.search(r"battery(?:\s+(?:type|capacity|and power))?\s*[:\-]\s*([^\n.]{3,80})", body, re.I)
    if m:
        value = m.group(1).strip()
        if re.search(r"li[- ]?ion|li[- ]?poly|polymer|\bwh\b|\bmah\b|\d[- ]?cell", value, re.I):
            out.setdefault("Battery Type", value)

    # display facts the title doesn't carry (brightness, colour gamut) and the
    # material evidence from HP's "Sustainable impact specifications" line.
    # ratings._blob reads spec values, and the full description isn't kept.
    scope = " ".join(filter(None, [display, body]))
    m = re.search(r"(\d{3,4})\s*nits", scope, re.I)
    if m:
        out.setdefault("Brightness", f"{m.group(1)} nits")
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(sRGB|NTSC|DCI[- ]?P3|Adobe RGB)", scope, re.I)
    if m:
        out.setdefault("Color gamut", f"{m.group(1)}% {m.group(2)}")
    m = re.search(r"sustainable impact specifications[:\s]+([^\n]{1,200})", body, re.I)
    if m:
        out.setdefault("Sustainable impact", m.group(1).strip())

    # DataBlitz spec tables often quote the *maximum* memory ("Up to 64GB DDR5")
    # while the installed capacity lives in the title ("16GB RAM", "16GBx2 RAM")
    # — never let the ceiling masquerade as what's fitted.
    ram_val = out.get("RAM")
    if ram_val and re.match(r"\s*(up to|max(?:imum)?)\b", ram_val, re.I):
        m = re.search(r"\b(\d{1,3})\s*GB\s*[x×]\s*(\d)\b", name or "", re.I)
        if m:
            out["RAM"] = f"{int(m.group(1)) * int(m.group(2))}GB"
        else:
            m = re.search(r"\b(\d{1,3})\s*GB\s*RAM\b", name or "", re.I)
            if m:
                out["RAM"] = m.group(0)
            else:
                out.pop("RAM", None)
    return out


def specs_from_noon(name: str | None) -> dict:
    """Display/OS details from a noon title.

    noon titles read like "Vivobook 16 Laptop With 16 Inch WUXGA (1920x1200) Display,
    Intel Core Ultra 7-255H Processor/16GB RAM/512GB SSD/Windows 11 Home" — CPU, RAM
    and storage come from the shared title fallback, this pulls the display and OS.
    """
    out: dict = {}
    text = re.sub(r"\s+", " ", name or "")
    low = text.lower()

    m = re.search(r"(\d{1,2}(?:\.\d)?)\s*(?:[- ]?inch|\"|”)", low)
    if m:
        out["Screen Size"] = m.group(0)
    m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,4})", low)
    if m:
        out["Display Resolution"] = f"{m.group(1)}x{m.group(2)}"
    else:
        for label, res in ((r"wqxga", "2560x1600"), (r"wuxga", "1920x1200"), (r"qhd", "2560x1440"),
                           (r"full hd", "1920x1080"), (r"\bfhd\b", "1920x1080"),
                           (r"\buhd\b(?!\s*graphics)", "3840x2160"), (r"\b4k\b", "3840x2160"),
                           (r"\b2k\b", "1920x1200"), (r"retina", "2560x1600"),
                           (r"\bhd\b", "1366x768")):
            if re.search(label, low):
                out["Display Resolution"] = res
                break
    if re.search(r"oled", low):
        out["Display Type"] = "OLED"
    elif re.search(r"mini-?led|\bxdr\b", low):
        out["Display Type"] = "Mini-LED"
    elif re.search(r"liquid retina|retina", low):
        out["Display Type"] = "Retina"
    elif re.search(r"\bips\b", low):
        out["Display Type"] = "IPS"
    elif re.search(r"\btn\b", low):
        out["Display Type"] = "TN"
    m = re.search(r"(\d{2,3})\s*hz", low)
    if m:
        out["Screen Refresh Rate"] = f"{m.group(1)}Hz"
    if re.search(r"\btouch", low):
        out["Touch Display"] = "Yes"
    if "Operating System" not in out:
        m = re.search(r"(windows\s*11\s*(?:home|pro)?|windows\s*10|macos|chrome\s*os|dos)", low)
        if m:
            out["Operating System"] = m.group(1).title()
    return out


def _score_screen(screen: dict) -> float:
    """Form-factor screen bonuses (touch and physical panel size BOM).

    Panel quality, resolution, color gamut, and refresh rate are scored
    exclusively via display_points in apply_quality_points to avoid double-counting.
    """
    score = 0.0
    if screen.get("touch"):
        score += 0.3
    inches = screen.get("inches") or 0
    if inches >= 17:
        score += 0.5          # 17–18" panels cost more BOM and define the big gaming class
    elif inches >= 16:
        score += 0.3
    return score


# Conservative idle footprints (GB) subtracted from installed RAM to get what
# the buyer can actually use. Estimates, not measurements: a fresh Windows 11
# idles near 3GB and OEM builds with background agents near 5GB; integrated
# GPUs carve 0.5–2GB (1GB typical, dynamic). DOS ships nothing resident.
OS_RESERVE = [
    ("windows", 4.0),
    ("macos", 3.5),
    ("chrome", 2.0),
    ("linux", 1.5),
    ("android", 2.0),
    # DOS ships nothing resident, but the buyer installs Windows the same
    # week — score the machine they'll actually run, not the out-of-box idle
    ("dos", 4.0),
]


def usable_ram(ram: int | None, os_name: str | None, gpu: dict) -> float | None:
    """Installed RAM minus OS footprint minus iGPU carve-out (None if unknown)."""
    if not ram:
        return None
    low = (os_name or "").lower()
    reserve = 3.0  # unknown OS: assume Windows-like (most of them are)
    for key, gb in OS_RESERVE:
        if key in low:
            reserve = gb
            break
    if (gpu or {}).get("class") == "integrated":
        reserve += 1.0
    return round(max(ram - reserve, 0), 1)


def build_tags(rec: dict, cpu: dict, gpu: dict, screen: dict, ram, storage, os_name) -> list[str]:
    tags = []
    if gpu.get("class") == "dedicated":
        tags.append("Gaming")
    elif (cpu.get("score") or 0) >= 75:
        # PassMark log scale: ~top 40% of laptop CPUs (i7-13620H class and up)
        tags.append("Performance")
    if screen.get("panel") == "OLED":
        tags.append("OLED")
    if screen.get("touch"):
        tags.append("Touch")
    if os_name and os_name.startswith("Windows 11 Pro"):
        tags.append("Win Pro")
    if os_name == "macOS":
        tags.append("macOS")
    if os_name and os_name.startswith("DOS"):
        tags.append("No OS")
    if ram and ram >= 32:
        tags.append("32GB+")
    usable = usable_ram(ram, os_name, gpu)
    if usable is not None and usable <= 8:
        tags.append("Low RAM")
    weight = parse_weight(_first_text(rec, "weight", "Weight"))
    if weight and weight <= 1.5:
        tags.append("Light")
    extras = " ".join([rec.get("name") or "", _first_text(rec, "form factor") or "",
                       _first_text(rec, "display type") or ""]).lower()
    if re.search(r"2-in-1|convertible|flip|x360|detachable", extras):
        tags.append("2-in-1")
    if _first_text(rec, "NPU (Neural Processing Unit)", "NPU Processor", "TOPs") or re.search(r"\bAI\b", rec.get("name") or ""):
        tags.append("AI")
    return tags


def apply_ram_ceiling(records: list[dict]) -> int:
    """Surcharge machines permanently stuck at <=8GB (soldered trap).

    Runs after apply_memory, so ram_max_gb is known. A machine that can
    never exceed 8GB loses 3 perf points on top of the <=8GB capacity cliff
    (your sister's 8GB MacBook case); upgradable 8GB machines only carry the
    capacity hit. Value is recomputed. Returns the surcharged count.
    """
    hit = 0
    for rec in records:
        if rec.get("perf_score") is None:
            continue
        if (rec.get("ram") or 99) > 8:
            continue
        ceiling = rec.get("ram_max_gb")
        if ceiling is None and rec.get("ram_slots") == 0:
            ceiling = rec.get("ram")  # soldered, no slots: you're stuck with what's fitted
        if ceiling is not None and ceiling <= 8:
            rec["perf_score"] = round(rec["perf_score"] - 3, 1)
            rec["ram_trap"] = True
            hit += 1
    return hit


# RAM generation: DDR5/LPDDR5(X) is faster and more future-proof than DDR4;
# unknown stays neutral.
_RAM_GEN_POINTS = {
    "DDR5": 0.75, "LPDDR5": 0.75, "LPDDR5X": 0.75,
    "DDR4": -0.5, "LPDDR4": -0.5, "LPDDR4X": -0.5,
}


def cpu_efficiency_points(cpu: dict | None) -> float:
    """Perf/watt tier for a parsed CPU (+2.0 to -1.5, unknown neutral).

    Applied to overall_score (what the machine is worth owning), never to
    perf_score (what the silicon benches plugged in): a 55W+ HX chip that
    benches higher keeps its higher perf_score even though it runs hotter,
    louder and dies unplugged.  TDP is not in the listings, so tiers go by
    silicon class + suffix (the reliable proxy):

    * +2.0 ARM (Apple Silicon, Snapdragon X/X2) — by far the most efficient
    * +1.0 efficient x86: N-series (6W), V-suffix (Lunar Lake), U-suffix
      (15W), HS-suffix (35W efficient), Ryzen AI non-HX (Strix/Krackan 15-28W)
    * 0 neutral: H/P-suffix mainstream, bare numbers with no suffix, unknown
    * -1.5 hungry desktop-replacement: HX/HK/HQ, Xeon, Ryzen AI MAX/MAX+
    """
    if not cpu:
        return 0.0
    brand = cpu.get("brand") or ""
    t = (cpu.get("text") or "").upper()
    if brand in ("Apple", "Qualcomm") or "SNAPDRAGON" in t:
        return 2.0
    # hungry first: overrides any other marker (e.g. "AI 9 HX 370" is HX)
    if re.search(r"(HX|HK|HQ)\b", t) or "XEON" in t or "AI MAX" in t or re.search(r"\bMAX\+?\b", t):
        return -1.5
    if re.search(r"\bN\d{2,4}\b", t) or "N-SERIES" in t or re.search(r"\dV\b", t):
        return 1.0
    if re.search(r"\dU\b", t) or re.search(r"HS\b", t) or "RYZEN AI" in t:
        return 1.0
    return 0.0

# Fixed VRAM per laptop GPU model, used when the listing doesn't state VRAM.
# RTX 3050 is the 6GB-max config (the 4GB variant is even lower, same verdict).
_VRAM_BY_MODEL = {
    "RTX 2050": 4, "RTX 3050": 6, "RTX 3060": 6, "RTX 3070": 8, "RTX 3070 TI": 8,
    "RTX 3080": 8, "RTX 3080 TI": 16,
    "RTX 4050": 6, "RTX 4060": 8, "RTX 4070": 8, "RTX 4080": 12, "RTX 4090": 16,
    "RTX 5050": 8, "RTX 5060": 8, "RTX 5070": 8, "RTX 5070 TI": 12,
    "RTX 5080": 16, "RTX 5090": 24,
    "GTX 1650": 4, "GTX 1660 TI": 6,
    "RTX 500": 4, "RTX 1000": 6, "RTX 2000": 8, "RTX 3000": 8, "RTX 3500": 12,
    "RTX 4000": 12, "RTX 5000": 16,
}


def _gpu_vram(gpu: dict) -> tuple[int | None, bool]:
    """(VRAM GB, inferred) — the listing's value first, else the model's config."""
    vram = gpu.get("vram")
    if vram:
        return int(vram), False
    model = re.sub(r"\s+", " ", (gpu.get("model") or gpu.get("text") or "")).strip().upper()
    m = re.search(r"(?:GEFORCE\s+)?(RTX|GTX)\s?([A-Z]?)(\d{3,4})\s?(TI|SUPER|ADA)?", model)
    if not m:
        return None, False
    key = f"{m.group(1)} {m.group(3)}" + (f" {m.group(4)}" if m.group(4) and m.group(4) != 'ADA' else "")
    return _VRAM_BY_MODEL.get(key), True


def apply_model_codes(records: list[dict]) -> int:
    """Fill missing part numbers from the identical configuration at another store.

    eXtra publishes model_number (and Jarir mpn) on most rows; noon's API
    usually carries no code, but the same machine at eXtra does.  Uses the
    dedupe configuration identity, so only truly identical configs share a code.
    Colorways get distinct part numbers (Midnight MDHF4 vs Sky Blue MDHJ4),
    so a fill additionally requires the same color when both sides name one —
    otherwise every noon M5 Air would inherit whichever eXtra color was seen
    first (code-override).
    """
    from dedupe import config_key, _line_compatible, _line_tokens  # local import: circular

    def _color(rec: dict) -> str | None:
        c = (rec.get("color") or "").strip().lower()
        return c or None

    donors: dict[tuple, list[tuple[str, tuple[str, ...] | None, str | None]]] = {}
    for rec in records:
        code = rec.get("model_code")
        if not code:
            continue
        key = config_key(rec)
        if key:
            donors.setdefault(key, []).append((code, _line_tokens(rec), _color(rec)))
    filled = 0
    for rec in records:
        if rec.get("model_code"):
            continue
        key = config_key(rec)
        if not key:
            continue
        cands = donors.get(key) or []
        if not cands:
            continue
        want = _color(rec)
        mine = _line_tokens(rec)
        # same key is not enough: a 250 G10 and a 15-fd can share a chip/RAM/
        # storage key but are different product lines with different part numbers
        compat = [(code, line, col) for code, line, col in cands
                  if _line_compatible(mine, line)]
        if not compat:
            continue
        pick = None
        if want:
            for code, line, col in compat:
                if col == want:
                    pick = code
                    break
            if pick is None:
                continue  # colors differ → different part numbers; leave blank
        else:
            pick = compat[0][0]
        rec["model_code"] = pick
        filled += 1
    return filled


def compute_ws_score(cpu: dict | None, gpu: dict | None, ram: int | None) -> float | None:
    """Mobile Workstation Benchmark (0-100 scale).

    Only RAM, CPU, and GPU matter:
    * CPU (35%): multi-core heavy compute (65% multi / 35% single)
    * GPU (35%): PassMark G3D compute / viewport throughput
    * RAM (30%): capacity scaled for professional datasets (8GB->25, 16GB->50, 32GB->75, 64GB->90, 128GB+->100)
    """
    if not cpu or cpu.get("score") is None or not gpu or gpu.get("score") is None or not ram:
        return None
    bm = cpu.get("benchmark") or {}
    multi = bm.get("score_multi")
    single = bm.get("score_single")
    if multi is not None and single is not None:
        c_score = 0.65 * multi + 0.35 * single
    else:
        c_score = cpu.get("score") or 40.0

    g_score = gpu.get("score") or 20.0

    if ram <= 8:
        m_score = max(10.0, ram * 3.125)
    elif ram <= 16:
        m_score = 25.0 + (ram - 8) * (25.0 / 8.0)
    elif ram <= 32:
        m_score = 50.0 + (ram - 16) * (25.0 / 16.0)
    elif ram <= 64:
        m_score = 75.0 + (ram - 32) * (15.0 / 32.0)
    else:
        m_score = min(100.0, 90.0 + (ram - 64) * (10.0 / 64.0))

    return round(0.35 * c_score + 0.35 * g_score + 0.30 * m_score, 1)


def compute_game_score(cpu: dict | None, gpu: dict | None, ram: int | None) -> float | None:
    """Gaming FPS capability (0-100 scale, G3D-based proxy — not measured fps).

    Only RAM, CPU, and GPU matter (same inputs as the workstation score,
    reweighted for how games actually behave):
    * GPU (60%): PassMark G3D — fps is GPU-bound above all else
    * CPU (25%): single-thread weighted (70% single / 30% multi) — games
      reward single-core speed, not core count
    * RAM (15%): capacity against gaming needs (8GB->30 stutters, 16GB->75
      the practical sweet spot, 32GB+->100)
    """
    if not cpu or cpu.get("score") is None or not gpu or gpu.get("score") is None or not ram:
        return None
    bm = cpu.get("benchmark") or {}
    multi = bm.get("score_multi")
    single = bm.get("score_single")
    if multi is not None and single is not None:
        c_score = 0.70 * single + 0.30 * multi
    else:
        c_score = cpu.get("score") or 40.0

    g_score = gpu.get("score") or 20.0

    if ram <= 8:
        m_score = max(10.0, ram * 3.75)
    elif ram <= 16:
        m_score = 30.0 + (ram - 8) * (45.0 / 8.0)
    else:
        m_score = min(100.0, 75.0 + (ram - 16) * (25.0 / 16.0))

    return round(0.60 * g_score + 0.25 * c_score + 0.15 * m_score, 1)


def apply_quality_points(records: list[dict]) -> dict:
    """Add quality points beyond the raw specs to perf_score (in place).

    Runs after apply_ratings / apply_memory / apply_ram_ceiling so the
    heuristics are known.  Balanced weights:

    * build 0-5: ``(build_score - 4)`` — a 5.5 mainstream plastic body earns
      1.5, a 9 (Zenbook/MacBook-class) earns 5
    * display 0-2 premium: ``(display_score - 6) * 0.5`` — the market-typical
      6/10 panel earns nothing; OLED's brightness/gamut/PPI edge earns ~1.5,
      without double-counting the resolution/panel points already in the build
    * RAM upgrade 0-2: +1.0 when slots with headroom exist, +0.5 for a ≥32GB
      ceiling and +1.0 for ≥64GB (capped); soldered or unknown stays neutral
    * RAM generation: +0.75 DDR5/LPDDR5(X), -0.5 DDR4/LPDDR4(X), unknown 0
    * GPU VRAM (dedicated only): +0.75 for 12GB+, -0.5 for 6GB or less,
      unknown stays neutral (inferred from the model when not stated)
    * CPU efficiency (perf/watt tier, on overall — never perf): +2.0 ARM
      (Apple/Snapdragon), +1.0 efficient x86 (N/V/U/HS/Ryzen AI non-HX),
      -1.5 hungry (HX/HK/HQ/Xeon/AI MAX), else neutral.  TDP is not in the
      listings, suffix is the proxy.  perf_score stays pure compute; the
      tier moves overall_score and therefore value_score.

    Value is recomputed.  Returns aggregate stats for the meta file.
    """
    stats = {"applied": 0, "build": 0, "display": 0, "ram_upgrade": 0,
             "ram_type": 0, "cpu_eff": 0, "vram": 0, "gpu_idle": 0, "points": 0.0}
    for rec in records:
        if rec.get("perf_score") is None:
            continue
        b = rec.get("build_score")
        build_pts = round(min(5.0, max(0.0, (b - 4.0))), 1) if b else 0.0
        d = rec.get("display_score")
        # panels matter: below the market-typical 6/10 costs 1.5 per point
        # (clamped at -4), above it earns 0.8 per point (clamped at +3.5)
        if d:
            raw = (d - 6.0) * (1.5 if d < 6.0 else 0.8)
            display_pts = round(min(3.5, max(-4.0, raw)), 1)
        else:
            display_pts = 0.0
        max_gb = rec.get("ram_max_gb")
        ram_pts = 0.0
        slots = rec.get("ram_slots") or 0
        fitted = rec.get("ram") or 0
        if slots > 0 and (max_gb is None or max_gb > fitted):
            ram_pts = 0.5
            if max_gb and max_gb >= 32:
                ram_pts += 0.5
        ram_pts = round(min(1.0, ram_pts), 1)
        rt = re.match(r"(LPDDR\dX?|DDR\dX?)", (rec.get("ram_type") or "").upper())
        ram_gen_pts = _RAM_GEN_POINTS.get(rt.group(1), 0.0) if rt else 0.0
        eff_pts = cpu_efficiency_points(rec.get("cpu"))
        gpu = rec.get("gpu") or {}
        vram, inferred = _gpu_vram(gpu)
        vram_pts = 0.0
        if gpu.get("class") == "dedicated" and vram:
            if inferred:
                # expose the model's fixed config for the viewer's VRAM filter
                gpu["vram"] = vram
                gpu["vram_inferred"] = True
            if vram >= 12:
                vram_pts = 0.75
            elif vram <= 6:
                vram_pts = -0.5
        # dGPU idle draw: a dedicated GPU keeps sipping power even when idle, so
        # the smaller the battery the worse the unplugged experience.  Charge it
        # by capacity (MUX/Advanced-Optimus behaviour isn't in the listing data).
        # Unknown capacity stays neutral.
        idle_pts = 0.0
        wh = rec.get("battery_wh")
        if gpu.get("class") == "dedicated" and wh:
            if wh < 55:
                idle_pts = -2.5
            elif wh < 65:
                idle_pts = -1.5
            elif wh < 80:
                idle_pts = -1.0
            elif wh < 90:
                idle_pts = -0.5
        rec["build_points"] = build_pts
        rec["display_points"] = display_pts
        rec["ram_upgrade_points"] = ram_pts
        rec["ram_type_points"] = ram_gen_pts
        rec["cpu_eff_points"] = eff_pts
        rec["vram_points"] = vram_pts
        rec["gpu_idle_points"] = idle_pts
        rec["quality_points"] = round(build_pts + display_pts, 1)
        silicon_pts = round(ram_pts + ram_gen_pts + vram_pts, 1)
        if silicon_pts:
            stats["applied"] += 1
            stats["points"] += silicon_pts
        stats["build"] += 1 if build_pts else 0
        stats["display"] += 1 if display_pts else 0
        stats["ram_upgrade"] += 1 if ram_pts else 0
        stats["ram_type"] += 1 if ram_gen_pts else 0
        stats["cpu_eff"] += 1 if eff_pts else 0
        stats["vram"] += 1 if vram_pts else 0
        stats["gpu_idle"] += 1 if idle_pts else 0
        rec["perf_score"] = round(rec["perf_score"] + silicon_pts, 1)
        non_perf = round(
            (rec.get("storage_points") or 0.0)
            + (rec.get("screen_points") or 0.0)
            + (rec.get("battery_points") or 0.0)
            + build_pts
            + display_pts
            + idle_pts,
            2
        )
        rec["non_perf_points"] = non_perf
        rec["quality_score"] = compute_quality_score(rec)
        # efficiency lives here: perf stays pure compute, the tier rewards
        # what the machine is worth owning (and therefore its value).
        rec["overall_score"] = round(0.60 * rec["perf_score"] + 0.40 * rec["quality_score"] + eff_pts, 1)
        price = rec.get("price") or 0
        rec["value_score"] = compute_value_score(rec["overall_score"], price)
        rec["ws_score"] = compute_ws_score(rec.get("cpu"), rec.get("gpu"), rec.get("ram"))
        rec["game_score"] = compute_game_score(rec.get("cpu"), rec.get("gpu"), rec.get("ram"))
        rec["game_value"] = compute_value_score(rec["game_score"], price)
    stats["points"] = round(stats["points"], 1)
    return stats


def _apple_screen(screen: dict, cpu: dict, name: str | None) -> None:
    """Fill display facts Apple never puts in marketplace titles (in place).

    Every MacBook Pro 14/16 with an M-series chip is mini-LED XDR 120Hz
    (3024×1964 / 3456×2234, unchanged since 2021); Airs are Retina LCD.
    Only fills gaps — parsed specs always win.
    """
    low = (name or "").lower()
    if "macbook" not in low:
        return
    m_chip = (cpu.get("brand") == "Apple" and not (cpu.get("family") or "").startswith("A"))
    inches = screen.get("inches") or 0
    pro_xdr = "pro" in low and m_chip and (not inches or inches >= 13.5)
    if not screen.get("panel"):
        if pro_xdr:
            screen["panel"] = "Mini-LED"
        else:
            screen["panel"] = "Retina"  # every MacBook display is Retina-class at minimum
    if screen.get("panel") == "Mini-LED" and m_chip:
        # native XDR resolutions haven't changed since 2021 — a parsed
        # 2560×1600 here is the store pasting the 13" spec sheet, fix it
        want = (3456, 2234) if 15.5 <= inches <= 17 else ((3024, 1964) if 13.5 <= inches < 15 else None)
        if want and (screen.get("width"), screen.get("height")) != want:
            screen.update(width=want[0], height=want[1], res=f"{want[0]}×{want[1]}",
                          res_label=_res_label(*want))
    if screen.get("panel") == "Mini-LED" and not screen.get("hz") and m_chip:
        screen["hz"] = 120


def _fill_known_panels(screen: dict, rec: dict) -> None:
    """Infer OLED / IPS for well-known models where marketplace listings stripped specs."""
    if screen.get("panel"):
        return
    name = ((rec.get("name") or "") + " " + (rec.get("model") or "") + " " + (rec.get("short_description") or "")).lower()
    if re.search(r"ux340[25]|ux540|ux840|zenbook.*oled|sfg14-74", name):
        screen["panel"] = "OLED"
    elif re.search(r"surface laptop", name):
        screen["panel"] = "PixelSense"


def detect_charging(rec: dict) -> tuple[str, bool | None, str]:
    """Return (charging_type, usbc_charging, charging_detail)."""
    brand = (rec.get("brand") or "").lower()
    name = ((rec.get("name") or "") + " " + (rec.get("model") or "") + " " + (rec.get("short_description") or "")).lower()
    specs = rec.get("specs") or {}
    spec_text = " ".join(f"{k} {v}" for k, v in specs.items()).lower()
    all_txt = f"{name} {spec_text}"
    gpu_class = (rec.get("gpu") or {}).get("class")

    # 1. Apple: all USB-C PD / MagSafe 3 (compatible with USB-C PD 70W+)
    if brand == "apple" or "macbook" in name:
        return "usbc_pd", True, "USB-C / MagSafe 3 (Power Delivery)"

    # 2. Explicit specs mentions of PD / Thunderbolt / USB-C charging
    if re.search(r"\b(pd 3\.0|thunderbolt 4|thunderbolt 3)\b", spec_text) or re.search(r"power delivery|type-c charging|usb-c charging|\b65w type-c\b|\b100w type-c\b", spec_text):
        return "usbc_pd", True, "USB-C Power Delivery (Confirmed)"

    # 3. Explicit disclosure of data-only USB-C
    if re.search(r"supports data transfer only and does not support charging|data transfer only.*no charging", all_txt):
        return "barrel", False, "Proprietary Barrel (USB-C is data-only)"

    # 4. Known USB-C charging families (Ultrabooks & Modern Business laptops)
    if re.search(r"zenbook|matebook|magicbook|megabook|swift|yoga|thinkpad|thinkbook|latitude|xps|surface|spectre|dragonfly|omnibook|galaxy book|expertbook", name):
        return "usbc_pd", True, "USB-C Power Delivery"

    # 5. Known barrel-only budget consumer lines
    if re.search(r"15[st]?[- ]f[cd]|hp (14|15|17|240|250|255)\b|ideapad (1|3|slim 1|slim 3)\b|aspire (1|3)\b|inspiron (15|3520|3530)\b|vivobook (14|15|16|go)\b|\bx1[456]0[45]", all_txt):
        return "barrel", False, "Proprietary Barrel (No USB-C Charging)"

    # 6. Gaming laptops
    if gpu_class == "dedicated":
        if re.search(r"legion|loq|rog|zephyrus|tuf|blade|omen", name):
            return "barrel_plus_usbc", True, "DC Barrel (Supports 100W USB-C PD Travel Charging)"
        return "barrel", False, "High-Watt DC Barrel Charger Only"

    return "unknown", None, "Charging type unconfirmed"


def detect_keyboard(rec: dict) -> tuple[bool | None, str | None]:
    """Return (keyboard_backlit, keyboard_detail)."""
    brand = (rec.get("brand") or "").lower()
    name = ((rec.get("name") or "") + " " + (rec.get("model") or "") + " " + (rec.get("short_description") or "")).lower()
    specs = rec.get("specs") or {}
    spec_text = " ".join(f"{k} {v}" for k, v in specs.items()).lower()
    all_txt = f"{name} {spec_text}"
    gpu_class = (rec.get("gpu") or {}).get("class")
    cpu_text = ((rec.get("cpu") or {}).get("text") or "").lower()

    if re.search(r"\b(rgb|backlit|backlight|back-lit)\b", all_txt):
        return True, "Backlit Keyboard"
    if re.search(r"\b(non[- ]backlit|without backlight|no backlight)\b", all_txt):
        return False, "Non-Backlit Keyboard"

    if brand == "apple" or "macbook" in name:
        return True, "Backlit Keyboard"
    if re.search(r"zenbook|yoga|xps|surface|spectre|dragonfly|blade|omen|legion|rog|zephyrus", name):
        return True, "Backlit Keyboard"
    if gpu_class == "dedicated":
        return True, "Backlit Gaming Keyboard"

    # Budget consumer tier check
    if re.search(r"15[st]?[- ]f[cd]|hp (14|15|17|240|250|255)\b|ideapad (1|slim 1)\b|aspire (1|3)\b", all_txt):
        if any(x in cpu_text for x in ["i3", "3 ", "celeron", "athlon", "n100", "n200"]):
            return False, "Non-Backlit Keyboard"

    return None, None


def detect_webcam(rec: dict) -> str | None:
    """Return webcam resolution label or None."""
    specs = rec.get("specs") or {}
    spec_text = " ".join(f"{k} {v}" for k, v in specs.items()).lower()
    all_txt = f"{(rec.get('name') or '')} {(rec.get('model') or '')} {spec_text}".lower()

    if re.search(r"1080p|fhd camera|full hd camera|1080p fhd|true vision 1080p|2mp camera|\b5mp\b|ir camera", all_txt):
        return "1080p FHD"
    if re.search(r"720p|hd camera|720p hd|true vision 720p|1mp camera", all_txt):
        return "720p HD"
    return None


def compute_dealbreakers_and_advantages(rec: dict, c_type: str, usbc_charge: bool | None,
                                       kbd_backlit: bool | None, webcam: str | None) -> tuple[list[str], list[str]]:
    deals = []
    advs = []

    # Charging
    if usbc_charge is False:
        deals.append("Barrel Charger Only (No USB-C PD)")
    elif usbc_charge is True and c_type == "usbc_pd":
        advs.append("USB-C Power Delivery")
    elif c_type == "barrel_plus_usbc":
        advs.append("Supports 100W USB-C PD Travel Charging")

    # Display panel
    screen = rec.get("screen") or {}
    panel = screen.get("panel")
    d_score = rec.get("display_score") or 6.0
    build_class = rec.get("build_class") or "mainstream"

    if panel in ("TN", "TN / Budget LCD") or (panel in (None, "LCD") and d_score <= 5.0 and build_class == "budget"):
        deals.append("Non-IPS / Budget Screen (250 nits, 45% NTSC)")
    elif panel == "OLED":
        advs.append("OLED Display")
    elif panel in ("Retina", "Mini-LED") or (panel == "IPS" and d_score >= 7.5):
        advs.append("High-Quality Display")

    # Battery
    wh = rec.get("battery_wh")
    if wh and wh <= 42.0:
        deals.append(f"Small Battery ({wh:g} Wh, ~3.5h)")
    elif wh and wh >= 70.0:
        advs.append(f"Large Battery ({wh:g} Wh)")

    # Keyboard
    if kbd_backlit is False:
        deals.append("No Keyboard Backlight")
    elif kbd_backlit is True:
        advs.append("Backlit Keyboard")

    # RAM generation on modern CPUs
    ram_type = (rec.get("ram_type") or "").upper()
    cpu_text = ((rec.get("cpu") or {}).get("text") or "").lower()
    if ram_type == "DDR4" and any(x in cpu_text for x in ["13th", "14th", "1315", "1335", "1340", "1350", "1360", "1370", "1440", "ultra", "7530", "7730", "7735", "8845"]):
        deals.append("Older DDR4 RAM on Modern CPU")

    # RAM Upgradability
    slots = rec.get("ram_slots")
    if slots == 2:
        max_gb = rec.get("ram_max_gb") or 32
        advs.append(f"2x Upgradable SO-DIMM Slots ({max_gb}GB Max)")
    elif slots == 0 and (rec.get("ram") or 0) <= 8:
        deals.append("8GB Soldered RAM (Cannot Upgrade)")

    # Webcam
    if webcam == "1080p FHD":
        advs.append("1080p FHD Webcam")

    # Chassis material
    mat = (rec.get("build_material") or "").lower()
    if any(x in mat for x in ["unibody", "all-metal", "all-aluminium", "cnc aluminium"]):
        advs.append("Premium All-Metal Chassis")

    return deals, advs


def apply_hardware_specs_and_warnings(records: list[dict]) -> dict:
    stats = {"usbc_pd": 0, "barrel": 0, "backlit": 0, "non_backlit": 0, "dealbreakers": 0, "advantages": 0}
    for rec in records:
        _fill_known_panels(rec.get("screen") or {}, rec)

        c_type, usbc_charge, c_detail = detect_charging(rec)
        rec["charging_type"] = c_type
        rec["usbc_charging"] = usbc_charge
        rec["charging_detail"] = c_detail
        if c_type == "usbc_pd":
            stats["usbc_pd"] += 1
        elif c_type == "barrel":
            stats["barrel"] += 1

        kbd_backlit, kbd_detail = detect_keyboard(rec)
        rec["keyboard_backlit"] = kbd_backlit
        rec["keyboard_detail"] = kbd_detail
        if kbd_backlit is True:
            stats["backlit"] += 1
            orig_b = rec.get("build_score") or 5.5
            new_b = min(10.0, round(orig_b + 0.4, 1))
            rec["build_score"] = new_b
            rec["build_label"] = next((lab for cut, lab in BUILD_LABELS if new_b >= cut), "Budget")
            if "backlit" not in (rec.get("build_material") or "").lower():
                rec["build_material"] = f"{rec.get('build_material') or ''} · backlit keyboard".strip(" ·")
        elif kbd_backlit is False:
            stats["non_backlit"] += 1

        webcam = detect_webcam(rec)
        rec["webcam_label"] = webcam

        deals, advs = compute_dealbreakers_and_advantages(rec, c_type, usbc_charge, kbd_backlit, webcam)
        rec["dealbreakers"] = deals
        rec["advantages"] = advs
        if deals:
            stats["dealbreakers"] += 1
        if advs:
            stats["advantages"] += 1

    return stats


# Curated corrections for listings whose own text is wrong (user-reported and
# cross-checked against the same machine at another store or the vendor docs).
# Keys are "Store:sku"; values are injected into the record's specs, so every
# parser (CPU, RAM, screen …) picks them up like a normal spec row.
SPEC_FIXES: dict[str, dict[str, str]] = {
    # units ship LPDDR4x, not the advertised DDR5 (buyer reports; the eXtra
    # listing of the same MateBook D16 16-i5-13420H states LPDDR4X)
    "noon:N70264602V": {"RAM Type": "LPDDR4X"},
    # SKU 83V60048AD is the 14IMH10 with the Ultra 5 135H; eXtra's own spec
    # row only says "Core Ultra 5" (the 125H/135H are the same chassis)
    "eXtra:100508920": {"Processor Type": "Intel Core Ultra 5 135H"},
    # user-confirmed: the "Upgraded Version" noon listing is the 83UR007FAD/W11
    # IdeaPad Slim 3 (noon's API carries no model number for it)
    "noon:N70389604V": {"Model Number": "83UR007FAD"},
    # ASUS spec sheets: this i7-1355U X1504VA is DDR4 (listing claims DDR5)
    "noon:N70215001V": {"RAM Type": "DDR4"},
    # user-verified: X1504VA-NJ121W is DDR4
    "noon:N70087658V": {"RAM Type": "DDR4"},
}


# Part-number patterns seen in merchant titles (noon especially): HP 15-fd0064nx,
# Lenovo 83UR007FAD, ASUS X1504V / UX3407QA-SB465WSM, Acer AL14-45P-R10U.
_MODEL_CODE_PATTERNS = [
    re.compile(r"\b\d{2}-[a-z]{2}\d{4}[a-z]{2}\b", re.I),
    re.compile(r"\b8[0-9][A-Z]{2}[A-Z0-9]{4,5}[A-Z]{2}\b"),
    re.compile(r"\b[A-Z]{1,2}\d{3,4}[A-Z]{1,3}(?:-[A-Z0-9]{3,12})?\b"),
    re.compile(r"\b[A-Z]{2,4}\d{2,3}[A-Z]?-\d{2,4}[A-Z]{1,3}\b"),
]


def _code_from_title(name: str | None) -> str | None:
    t = name or ""
    for rx in _MODEL_CODE_PATTERNS:
        m = rx.search(t)
        if m:
            return m.group(0).upper()
    return None


# Colorways, longest-first at match time. Jarir publishes a `color` field
# (Luna Grey, Glacier Silver …); the other stores only name it in the title
# ("...Silver", "(Indigo)"), so the same vocabulary covers both. Colors are
# *not* part of the dedupe key — one card per configuration — but they are
# carried on every record/offer so the buyer can pick a listing in their color.
_COLORS = [
    "Atmospheric Blue", "Translucent Black", "Iridescent Black", "Platinum White",
    "Eclipse Black", "Platinum Grey", "Atmosphere Blue", "Glacier Silver",
    "Zabriskie Beige", "Meteor Silver", "Natural Silver", "Arctic Grey",
    "Eclipse Grey", "Charcoal Black", "Carbon Black", "Cosmos Grey",
    "Glacier White", "Light Silver", "Mystic Silver", "Sahara Silver",
    "Shadow Black", "Urban Silver", "Starry Grey", "Quiet Blue",
    "Cool Silver", "Cloud Grey", "Cosmic Blue", "Emerald Green",
    "Jaeger Grey", "Luna Grey", "Mica Silver", "Morandi Blue",
    "Pike Silver", "Ponder Blue", "Surge Green", "Tidal Teal",
    "Matte Black", "Matte Grey", "Mixed Black",
    "Moher Grey", "Antrim Grey", "Indie Black", "Mecha Grey",
    "Off Black", "Nano Black", "Sky Blue", "Space Black",
    "Volt Green", "Core Black", "Dune", "Seashell",
    "Starlight", "Midnight", "Graphite", "Moonlight",
    "Champagne", "Burgundy", "Platinum", "Silver",
    "Black", "White", "Indigo", "Citrus",
    "Blush", "Grey", "Gray", "Blue",
    "Green", "Red", "Pink", "Gold",
    "Bronze", "Purple", "Orange", "Brown",
    "Navy", "Ivory", "Coral", "Copper",
    "Rose", "Olive", "Mint", "Lavender",
    "Beige",
]
_COLORS_BY_LEN = sorted(dict.fromkeys(_COLORS), key=len, reverse=True)


def extract_color(name: str | None, raw: str | None = None) -> str | None:
    """Colorway of a listing: merchant field first, else longest title match."""
    if raw and str(raw).strip():
        return str(raw).strip()
    t = (name or "").lower()
    if not t:
        return None
    for c in _COLORS_BY_LEN:
        if re.search(r"(?<![a-z])" + re.escape(c.lower()) + r"(?![a-z])", t):
            return c
    return None


def normalize(rec: dict, store: str) -> dict:
    # Curated corrections for listings whose own text is wrong (user-reported,
    # cross-checked against the same machine at another store / the vendor).
    # Injected as spec values so every parser picks them up.
    fixes = SPEC_FIXES.get(f"{store}:{rec.get('sku')}")
    if fixes:
        rec = {**rec, "specs": {**(rec.get("specs") or {}), **fixes}}
    raw_marketplace = (rec.get("listing") or {}).get("marketplace")
    marketplace = raw_marketplace is True or str(raw_marketplace).strip().lower() in ("true", "1", "yes")

    # stock status
    delisted = bool(rec.get("delisted"))
    in_stock = rec.get("in_stock")
    if in_stock is None:
        in_stock = False if delisted else None
    in_stock = bool(in_stock) if in_stock is not None else None
    if delisted:
        stock_note = "delisted"
    elif store == "Jarir":
        showrooms = rec.get("pickup_showrooms")
        stock_note = f"{showrooms} showrooms" if showrooms else None
    elif store in ("DataBlitz", "PCX", "Gigahertz"):
        stock_note = "Philippines" if in_stock else None
    elif store == "noon":
        stock_note = rec.get("stock_note") or ("noon" if in_stock else None)
    else:
        cities = rec.get("in_stock_cities") or []
        if in_stock and cities:
            stock_note = "Riyadh + %d cities" % (len(cities) - 1) if len(cities) > 1 else "Riyadh"
        elif rec.get("in_stock_any"):
            stock_note = "other cities only"
        else:
            stock_note = None

    if store == "noon":
        rec = {**rec, "specs": {**(rec.get("specs") or {}),
                                **specs_from_noon(rec.get("name"))}}

    if store == "Jarir":
        cpu_text = combine_cpu_text(_first_text(rec, "processor type"))
        ram, ram_type = parse_ram(rec)
        storage, storage_type = parse_storage(rec)
        gpu = parse_gpu(rec)
        screen = parse_screen(rec)
        os_name = parse_os(_first_text(rec, "operating system"))
        weight = parse_weight(_first_text(rec, "weight"))
        battery = _first_text(rec, "battery type")
        warranty = parse_warranty(rec)
        model = rec.get("model")
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("regular_price_sar")
        discount = None
        if was_price and rec.get("price_sar") and was_price > rec["price_sar"]:
            discount = round((1 - rec["price_sar"] / was_price) * 100, 1)
    elif store in ("DataBlitz", "PCX", "Gigahertz"):
        # DataBlitz/Gigahertz: the title carries the specs as
        # pipe-separated segments; PCX: a flat SPECIFICATION label run in the
        # description.  Either way CPU/RAM/storage/GPU are picked up by the
        # title fallback below; this adds the screen details, model number and
        # weight/battery lines.
        dbt = (specs_from_pcx(rec.get("description")) if store == "PCX"
               else specs_from_datablitz(rec.get("name"), rec.get("description")))
        rec = {**rec, "specs": dbt}
        cpu_text = dbt.get("Processor Type") or ""
        ram, ram_type = parse_ram(rec)
        storage, storage_type = parse_storage(rec)
        gpu = parse_gpu(rec)
        screen = parse_screen(rec)
        os_name = parse_os(dbt.get("Operating System"))
        weight = parse_weight(dbt.get("Weight"))
        battery = dbt.get("Battery Type") or dbt.get("Battery")
        warranty = None
        model = dbt.get("Model Number")
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("was_price_sar")
        price_php, was_php = rec.get("price_php"), rec.get("was_price_php")
        discount = None
        if was_php and price_php and was_php > price_php:
            discount = round((1 - price_php / was_php) * 100, 1)
    else:
        cpu_text = combine_cpu_text(_first_text(rec, "Processor Type"), _first_text(rec, "Processor Model"))
        ram, ram_type = parse_ram(rec)
        storage, storage_type = parse_storage(rec)
        gpu = parse_gpu(rec)
        screen = parse_screen(rec)
        os_name = parse_os(_first_text(rec, "Operating System"))
        weight = parse_weight(_first_text(rec, "Weight"))
        battery = _first_text(rec, "Battery Type", "battery type")
        warranty = parse_warranty(rec)
        model = _first_text(rec, "Series Name") or _first_text(rec, "Model Number") or rec.get("model_number")
        image = (rec.get("images") or [None])[0]
        was_price = rec.get("was_price_sar")
        discount = rec.get("discount_pct")

    # --- battery: capacity only when the listing publishes it (see parse_battery_capacity) ---
    battery_wh, battery_mah, battery_detail = parse_battery_capacity(_all_specs(rec))
    battery_label = " · ".join(filter(None, [
        f"{battery_wh:g} Wh" if battery_wh else (f"{battery_mah:,} mAh" if battery_mah else ""),
        _battery_chem_label(battery),
    ])) or battery_detail
    battery_source = "listed" if (battery_wh or battery_mah) else None

    # --- fallbacks from the product title (mostly marketplace items) ---
    title = specs_from_title(rec.get("name"))
    if not ram and title.get("ram"):
        ram = title["ram"]
    elif title.get("ram") and ram and title["ram"] != ram:
        # Apple configs are fixed SKUs: the title ("M5, 24GB, 1TB SSD") is
        # reliable while merchant spec tables are often copy-paste — eXtra
        # lists "16GB" for the 24GB/1TB M5 Airs (MDH94/MDHK4/MDHD4/…). Trust
        # the title for Apple when they conflict so 24GB machines don't fold
        # into the 16GB card.
        if (_title_brand(rec.get("brand")) == "Apple"
                or "macbook" in (rec.get("name") or "").lower()):
            ram = title["ram"]
    if not storage and title.get("storage"):
        storage = title["storage"]
        storage_type = storage_type or "SSD"
    if not cpu_text:
        cpu_text = title.get("cpu") or ""
    if not screen.get("inches") and title.get("inches"):
        screen["inches"] = title["inches"]
    if not os_name and title.get("os"):
        os_name = title["os"]

    # --- CPU: parse, then prefer the title when the spec is generic or less detailed ---
    cpu = parse_cpu(cpu_text)
    if title.get("cpu") and (marketplace or cpu_is_generic(cpu) or cpu_detail(title["cpu"]) > cpu_detail(cpu_text)):
        merged = combine_cpu_text(title["cpu"], cpu_text)
        if merged != cpu_text:
            cpu = parse_cpu(merged)
    if not cpu["cores"]:
        m = re.search(r"(\d{1,2})\s*(?:cores?|core)\b", _first_text(rec, "Processor Core") or "", re.I)
        if m:
            cpu["cores"] = int(m.group(1))

    # --- GPU: stored spec, else the title, else inferred integrated ---
    if title.get("gpu") and (not gpu.get("text") or gpu_is_generic(gpu)):
        title_gpu = parse_gpu({"specs": {"Graphics Controller Model": title["gpu"]}})
        if title_gpu.get("model") or not gpu.get("text"):
            gpu = title_gpu
    if not gpu.get("text") and cpu.get("brand"):
        brand = cpu["brand"]
        inferred_text = {
            "Intel": "Intel Integrated Graphics",
            "AMD": "AMD Radeon Graphics",
            "Apple": "Apple Integrated GPU",
            "Qualcomm": "Qualcomm Adreno GPU",
        }.get(brand)
        if inferred_text:
            gpu = {
                "text": inferred_text, "brand": brand, "model": None, "class": "integrated",
                "vram": None, "inferred": True, "score": None,
            }

    # --- real PassMark benchmarks (replaces the old tier heuristic) ---
    apply_benchmarks(cpu, gpu)
    # a generic "Integrated" is useless in the compare table — label the actual
    # iGPU from the CPU family (the score already used the closest measured row)
    if gpu.get("class") == "integrated" and not gpu.get("model"):
        igpu_name = igpu_name_for_cpu(cpu.get("text"))
        if igpu_name:
            gpu["model"] = igpu_name
            gpu["text"] = igpu_name

    # --- Apple displays are never fully specified in marketplace titles ---
    if _title_brand(rec.get("brand")) == "Apple" or "macbook" in (rec.get("name") or "").lower():
        _apple_screen(screen, cpu, rec.get("name"))

    # --- RAM: usable headroom relative to OS natural idle usage (8GB baseline) ---
    # Raw installed RAM is misleading: the OS kernel, background processes, and iGPU
    # frame buffer carve out 3.5-5.0GB before any user applications launch.
    # An 8GB machine only has ~3.5GB of actual usable headspace for apps (the 0.0 baseline).
    # Machines with <=8GB usable suffer a real-world -2.0 pt choke due to constant pagefile swapping.
    usable = usable_ram(ram, os_name, gpu)
    if usable is not None and usable > 0:
        cap_points = round(7.0 * math.log2(max(usable, 0.5) / 3.5), 1)
    else:
        cap_points = 0.0
    ram_choke_points = 2.0 if (usable is not None and usable <= 8) else 0.0
    ram_points = round(cap_points - ram_choke_points, 1)
    storage_points = round(min(storage or 0, 2048) / 512 * 1.5, 2)
    screen_points = _score_screen(screen)
    # Battery capacity is a premium: a bigger chassis that fits more cells is
    # worth more, and Wh is test-independent (no endurance-run assumptions).
    # Only published capacities score; unknown stays neutral (the stores that
    # don't list Wh would otherwise be systematically penalised).
    battery_points = battery_points_for(battery_wh)
    if cpu.get("score") is None or gpu.get("score") is None:
        perf = None
        quality = None
        overall = None
        init_non_perf = None
        value = None
        game = None
        game_val = None
    else:
        perf = round(0.65 * cpu["score"] + 0.15 * gpu["score"] + ram_points, 1)
        init_non_perf = round(storage_points + screen_points + battery_points, 2)
        quality = compute_quality_score({
            "battery_wh": battery_wh,
            "storage": storage,
            "screen": screen,
            "gpu": gpu,
        })
        overall = round(0.60 * perf + 0.40 * quality, 1)
        value = compute_value_score(overall, rec.get("price_sar"))
        game = compute_game_score(cpu, gpu, ram)
        game_val = compute_value_score(game, rec.get("price_sar"))

    # the part number (83V60048AD, G733ZX-KH059W …) for the compare table:
    # merchant fields first (Jarir mpn, eXtra model_number, DataBlitz spec
    # table), then a code pattern in the title
    model_code = (rec.get("model_number") or rec.get("mpn")
                  or _first_text(rec, "Model Number")
                  or (model if store == "DataBlitz" else None)
                  or _code_from_title(rec.get("name")))
    color = extract_color(rec.get("name"), rec.get("color"))
    return {
        "id": f"{store.lower()}:{rec.get('sku')}",
        "store": store,
        "sku": rec.get("sku"),
        "name": rec.get("name"),
        "short_name": short_title(rec.get("name"), _title_brand(rec.get("brand"))),
        "brand": _title_brand(rec.get("brand")),
        "model": model,
        "model_code": model_code,
        "color": color,
        "url": rec.get("url"),
        "image": image,
        "price": rec.get("price_sar"),
        "price_local": rec.get("price_local"),
        "currency": rec.get("currency"),
        "was_price": was_price,
        "discount": discount,
        "marketplace": marketplace,
        "in_stock": in_stock,
        "delisted": delisted,
        "stock_note": stock_note,
        "seller": rec.get("seller"),
        "seller_note": seller_note(rec.get("seller")) if marketplace else None,
        "seller_issue_pct": (estimated_issue_rate(rec.get("seller_positive_pct"))
                             if marketplace else None),
        "seller_rating": rec.get("seller_rating"),
        "seller_reviews": rec.get("seller_reviews"),
        "seller_positive_pct": rec.get("seller_positive_pct"),
        "cpu": cpu,
        "ram": ram,
        "ram_usable_gb": usable,
        "ram_points": ram_points,
        "ram_choke_points": ram_choke_points,
        "ram_type": ram_type,
        "storage": storage,
        "storage_type": storage_type,
        "storage_points": storage_points,
        "gpu": gpu,
        "screen": screen,
        "screen_points": screen_points,
        "os": os_name,
        "weight_kg": weight,
        "battery": battery,
        "battery_label": battery_label,
        "battery_wh": battery_wh,
        "battery_mah": battery_mah,
        "battery_detail": battery_detail,
        "battery_points": battery_points,
        "battery_source": battery_source,
        "warranty_years": warranty,
        "rating": rec.get("rating"),
        "review_count": rec.get("review_count"),
        "perf_score": perf,
        "quality_score": quality,
        "ws_score": compute_ws_score(cpu, gpu, ram),
        "game_score": game,
        "game_value": game_val,
        "non_perf_points": init_non_perf,
        "overall_score": overall,
        "value_score": value,
        "tags": build_tags(rec, cpu, gpu, screen, ram, storage, os_name),
        "short_description": rec.get("short_description") or rec.get("description"),
        "specs": rec.get("specs") or {},
    }


BRAND_MAP = {
    "lenovo": "Lenovo", "hp": "HP", "asus": "Asus", "apple": "Apple", "msi": "MSI",
    "acer": "Acer", "microsoft": "Microsoft", "dell": "Dell", "honor": "Honor",
    "huawei": "Huawei", "infiniarc": "INFINIARC", "tecno": "Tecno", "gama": "GAMA",
    "samsung": "Samsung", "razer": "Razer", "lg": "LG", "gigabyte": "Gigabyte",
}


def _title_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    return BRAND_MAP.get(brand.strip().lower(), brand.strip().title())


# ---------------------------------------------------------------------------
# Short display title: the store titles cram every spec into the name
# ("Vivobook X1504V (Upgraded Version) Laptop With 15.6 Inch Full HD (1920x1080)
# Display, Intel Core 5-120U Processor/16GB RAM..."), but every one of those
# specs already has its own row on the card. Cut the title at the first spec
# marker and keep the product line + model only.
# ---------------------------------------------------------------------------

_TITLE_CUTS = [
    r"\s+with\b",
    r"\b(?:processor|graphics\s+card|display|screen)\b",
    r"\b(?:intel|amd|qualcomm)\b",
    r"\b(?:core(?:\s+ultra)?\s*[3579i]|ultra\s+[3579])\b",
    r"\bryzen\b",
    r"\bsnapdragon\b",
    r"\bapple\s+m\d\b",
    r"\bm\d\s+(?:pro|max|ultra|chip)\b",
    r"\bi[3579][-\s]\d",
    r"\b(?:celeron|pentium|athlon|mendocino)\b",
    r"\bn\d{3}\b",
    r"\bnext\s+gen\b",
    r"\bcopilot\+?\b",
    r"\d{1,2}(?:\.\d)?\s*(?:-?\s*inch|inches|\"|”)",
    r"\b\d{1,3}\s?gb\b",
    r"\b\d\s?tb\b",
    r"\b(?:lp)?ddr[345]\b",
    r"\b(?:ssd|hdd|emmc|nvme|pcie)\b",
    r"\b(?:windows|freedos|dos|macos|chrome\s?os|linux|ubuntu)\b",
    r"\b(?:rtx|gtx|geforce|radeon|arc\s+graphics|uhd\s+graphics)\b",
    r"[,|/]\s*(?=(?:intel|amd|ryzen|core|ultra|snapdragon|qualcomm|apple|m\d\b|i[3579]|n\d{3}|"
    r"celeron|pentium|athlon|\d{1,3}\s?gb|\d\s?tb|(?:lp)?ddr[345]|ssd|hdd|emmc|nvme|"
    r"windows|dos|macos|linux|processor|cpu|gpu|rtx|gtx|geforce|radeon|display|screen|"
    r"\d{1,2}(?:\.\d)?\s*(?:-?\s*inch|\"|”)))",
    r"\blaptop\b(?=\s*(?:with\b|[,|/]|$))",
    r"\bnotebook\b(?=\s*(?:with\b|[,|/]|$))",
]

_GENERIC_TITLE_WORDS = {
    "laptop", "notebook", "gaming", "clamshell", "touch", "convertible",
    "ultrabook", "computer", "pc", "the", "new",
}


def _clean_short(s: str) -> str:
    """Trim descriptor tails from a candidate title fragment."""
    s = re.split(r"\s+(?:laptop|notebook)\s*\(", s, maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\s*[-–—]?\s*(?:clamshell|convertible|touchscreen)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*[-–—]?\s*gaming\s*$", "", s, flags=re.I)
    s = re.sub(r",?\s*\bAI\b\s*$", "", s)
    s = re.sub(r"\s*\([^)]*(?:intel|amd|ryzen|core|gb|ssd|hdd|windows|ddr)[^)]*\)\s*$", "", s, flags=re.I)
    return s.strip(" -–—,|/·(").strip()


def _usable_short(s: str) -> bool:
    if not s or len(s) < 3:
        return False
    words = s.split()
    if words[0].lower() in ("laptop", "notebook"):
        return False
    if len(words) >= 2:
        return True
    return s.lower() not in _GENERIC_TITLE_WORDS


def _title_case(s: str) -> str:
    return (s[0].upper() + s[1:]) if s[:1].islower() else s


def short_title(name: str | None, brand: str | None) -> str:
    """Product line + model only, e.g. 'Vivobook X1504V (Upgraded Version)'."""
    raw = re.sub(r"\s+", " ", (name or "").strip())
    if not raw:
        return ""
    text = re.sub(r"^(?:laptop|notebook)\s+(?!with\b)", "", raw, flags=re.I)
    if brand:
        text = re.sub(rf"^{re.escape(brand)}[\s\-–—]*", "", text, flags=re.I)
    # every spec marker is a potential cut; try the earliest usable prefix first
    cuts = sorted({m.start() for pattern in _TITLE_CUTS for m in re.finditer(pattern, text, re.I)})
    for cut in cuts:
        short = _clean_short(text[:cut])
        # prefer dropping a trailing "Laptop"/"Notebook" connector when what
        # remains is still a usable name ("Pro 15 essential Laptop" -> "Pro 15 essential")
        no_lt = re.sub(r"\s+(?:laptop|notebook)$", "", short, flags=re.I).strip()
        if _usable_short(no_lt):
            return _title_case(no_lt)
        if _usable_short(short):
            return _title_case(short)
    # no product line found (title starts with the specs) — keep original
    fallback = text.strip(" -–—,|/·").strip()
    return fallback or raw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


TOMBSTONE_DAYS = 30


def carry_tombstones(records: list[dict], hist_stats: dict) -> list[dict]:
    """Keep feed-pulled listings visible as Deleted cards (never confused w/ OOS).

    A SKU present in the previous snapshot but absent from every store file was
    pulled from the feed (e.g. eXtra marketplace listings vanish from the Unbxd
    index instead of 404ing).  Rather than dropping it silently, carry it as a
    dimmed tombstone: delisted + deleted, stale offers stripped, expiry after
    TOMBSTONE_DAYS.  SKUs that were never listed (stale feed entries, pricing-
    ladder seeds) are ignored.  Call AFTER append_snapshot so tombstones never
    pollute price history with echo rows.
    """
    try:
        previous = json.loads((DATA_DIR / "unified_laptops.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    new_ids = {r.get("id") for r in records}
    today = datetime.now(timezone.utc).date()
    # candidates: previous snapshot first (freshest truth wins), then curated
    # seeds from data/tombstones.json (full records without hist, same shape)
    candidates: dict[str, dict] = {}
    for prev in previous:
        if prev.get("id"):
            candidates.setdefault(prev["id"], prev)
    try:
        seeds = json.loads((DATA_DIR / "tombstones.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        seeds = []
    for seed in seeds:
        if seed.get("id"):
            candidates.setdefault(seed["id"], seed)
    kept = []
    for pid, prev in candidates.items():
        if pid in new_ids:
            continue                      # back in the feed: live record wins
        if is_junk(prev) or prev.get("store") == "Amazon" or (prev.get("price") and prev["price"] > 25000):
            continue                      # never resurrect junk/broken listings, Amazon, or >25k as tombstones
        since = prev.get("deleted_since")
        if since:
            try:
                age = (today - datetime.strptime(since, "%Y-%m-%d").date()).days
            except ValueError:
                continue
            if age > TOMBSTONE_DAYS:
                continue
        else:
            since = today.isoformat()
        tomb = dict(prev)
        for k in ("offers", "dup_count", "dup_max", "price_expected", "price_flag",
                  "deal_ratio", "price_local"):
            tomb.pop(k, None)
        # tombstones ride along verbatim, so re-derive the parsed weight from the
        # preserved spec sheet — otherwise a fixed parser still leaves old bad
        # values in the dataset (Jarir ".98 kg" used to read as 98 kg)
        w = parse_weight(_first_text(tomb, "weight", "Weight"))
        if w is not None:
            tomb["weight_kg"] = w
        elif tomb.get("weight_kg") and not (WEIGHT_MIN_KG <= tomb["weight_kg"] <= WEIGHT_MAX_KG):
            tomb["weight_kg"] = None      # implausible, and no spec left to re-derive from
        tomb["in_stock"] = False
        tomb["delisted"] = True
        tomb["deleted"] = True
        tomb["deleted_since"] = since
        tomb["stock_note"] = None
        tomb["tags"] = [t for t in (tomb.get("tags") or []) if t != "Deal"]
        h = hist_stats.get(tomb.get("sku"))
        tomb["hist"] = h
        pts = (h or {}).get("points") or []
        tomb["first_seen"] = pts[0][0] if pts else prev.get("first_seen")
        kept.append(tomb)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    jarir = load(DATA_DIR / "laptops.jsonl")
    extra = [r for r in load(DATA_DIR / "extra_laptops.jsonl") if r.get("is_laptop") is not False]
    datablitz = load(DATA_DIR / "datablitz_laptops.jsonl")
    pcx = load(DATA_DIR / "pcx_laptops.jsonl")
    gigahertz = load(DATA_DIR / "gigahertz_laptops.jsonl")
    noon = [r for r in load(DATA_DIR / "noon_laptops.jsonl") if not is_junk(r)]
    records = ([normalize(r, "Jarir") for r in jarir]
               + [normalize(r, "eXtra") for r in extra]
               + [normalize(r, "DataBlitz") for r in datablitz]
               + [normalize(r, "PCX") for r in pcx]
               + [normalize(r, "Gigahertz") for r in gigahertz]
               + [normalize(r, "noon") for r in noon])
    records = [r for r in records if not (r.get("price") and r["price"] > 25000)]
    records.sort(key=lambda r: (r["price"] or 0))

    # carry published capacity onto identical hardware (part number / agreeing chassis)
    battery_lookup = apply_battery_lookup(records)
    # and estimate the uncoded tail from similar listings (labelled, awarded capacity points)
    battery_estimates = apply_battery_estimates(records)

    # heuristic display + build/material ratings, and RAM upgrade options
    for rec in records:
        apply_ratings(rec)
    memory_stats = apply_memory(records, {r.get("sku"): r.get("description") for r in datablitz})
    ram_traps = apply_ram_ceiling(records)
    # hardware specs & buyer warnings (charging, keyboard backlight build bonus, webcam)
    hw_stats = apply_hardware_specs_and_warnings(records)
    # build / display-quality / RAM-upgradability premium on top of the specs
    quality_stats = apply_quality_points(records)
    # part numbers: fill gaps from the identical configuration at another store
    model_codes_filled = apply_model_codes(records)

    # fold identical cross-store listings into one entry with an offers list
    dedupe_stats = apply_dedupe(records)

    # price-sanity model over every store
    price_flags = flag_prices(records)

    # price/stock history: append today's snapshot and attach lows to every record
    append_snapshot(records)
    hist_stats = load_stats()
    for rec in records:
        h = hist_stats.get(rec["sku"])
        rec["hist"] = h
        # exact trailing-365d best from every recorded datapoint (full
        # resolution — the viewer trend is downsampled, so ship the answer)
        rec["best365"] = (h or {}).get("best365")
        rec["best365_date"] = (h or {}).get("best365_date")
        rec["window_start"] = (h or {}).get("window_start")
        # first observation = when the tracker first saw this listing
        pts = (h or {}).get("points") or []
        rec["first_seen"] = pts[0][0] if pts else None

    # listings pulled from the feed since the last snapshot stay visible as
    # Deleted tombstones (after the snapshot, so no echo rows in history)
    tombs = carry_tombstones(records, hist_stats)
    records.extend(tombs)
    records.sort(key=lambda r: (r["price"] or 0))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "unified_laptops.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    # CSV (compact, normalized columns + a few key raw specs)
    cols = ["id", "store", "sku", "brand", "name", "short_name", "color", "price", "was_price", "discount",
            "in_stock", "delisted", "deleted", "deleted_since", "stock_note", "hist_low", "hist_low_date",
            "best365", "best365_date",
            "cpu_text", "cpu_family", "cpu_score",             "cpu_mark", "cpu_single", "cpu_benchmark",
            "cpu_score_multi", "cpu_score_single",
            "ram", "ram_usable_gb", "ram_type", "storage", "storage_type",
            "gpu_text", "gpu_class", "gpu_vram", "gpu_score", "gpu_mark", "gpu_benchmark",
            "screen_inches", "screen_res",
            "screen_panel", "screen_hz", "touch", "display_score", "display_label",
            "build_score", "build_label", "build_points", "display_points", "ram_upgrade_points", "ram_type_points", "cpu_eff_points", "vram_points", "gpu_idle_points", "quality_points", "quality_score", "storage_points", "screen_points", "non_perf_points", "overall_score", "ram_slots", "ram_max_gb", "ram_note", "ram_source", "ram_detail",
            "charging_type", "usbc_charging", "charging_detail", "keyboard_backlit", "webcam_label", "dealbreakers", "advantages",
            "offers", "dup_count", "dup_max", "merged_into",
            "price_local", "currency", "price_expected", "price_flag",
            "os", "weight_kg", "battery_label", "battery_wh", "battery_mah", "battery_points", "battery_source", "perf_score", "ws_score", "game_score", "game_value", "value_score",
            "marketplace", "seller", "seller_note", "seller_issue_pct", "seller_positive_pct", "seller_reviews", "tags", "url"]
    with (DATA_DIR / "unified_laptops.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "id": r["id"], "store": r["store"], "sku": r["sku"], "brand": r["brand"],
                "name": r["name"], "short_name": r.get("short_name"), "color": r.get("color"),
                "price": r["price"], "was_price": r["was_price"],
                "discount": r["discount"],
                "in_stock": r["in_stock"], "delisted": r["delisted"], "deleted": r.get("deleted"),
                "deleted_since": r.get("deleted_since"), "stock_note": r["stock_note"],
                "hist_low": (r.get("hist") or {}).get("low"),
                "hist_low_date": (r.get("hist") or {}).get("low_date"),
                "best365": r.get("best365"),
                "best365_date": r.get("best365_date"),
                "cpu_text": r["cpu"]["text"],
                "cpu_family": r["cpu"]["family"], "cpu_score": r["cpu"]["score"],
                "cpu_mark": (r["cpu"].get("benchmark") or {}).get("mark"),
                "cpu_single": (r["cpu"].get("benchmark") or {}).get("single"),
                "cpu_benchmark": (r["cpu"].get("benchmark") or {}).get("source"),
                "cpu_score_multi": (r["cpu"].get("benchmark") or {}).get("score_multi"),
                "cpu_score_single": (r["cpu"].get("benchmark") or {}).get("score_single"),
                "ram": r["ram"], "ram_usable_gb": r.get("ram_usable_gb"), "ram_type": r["ram_type"], "storage": r["storage"],
                "storage_type": r["storage_type"], "gpu_text": r["gpu"]["text"],
                "gpu_class": r["gpu"]["class"], "gpu_vram": r["gpu"].get("vram"), "gpu_score": r["gpu"]["score"],
                "gpu_mark": (r["gpu"].get("benchmark") or {}).get("mark"),
                "gpu_benchmark": (r["gpu"].get("benchmark") or {}).get("source"),
                "screen_inches": r["screen"]["inches"],
                "screen_res": r["screen"]["res"], "screen_panel": r["screen"]["panel"],
                "screen_hz": r["screen"]["hz"], "touch": r["screen"]["touch"],
                "display_score": r["display_score"], "display_label": r["display_label"],
                "build_score": r["build_score"], "build_label": r["build_label"],
                "build_points": r.get("build_points"), "display_points": r.get("display_points"),
                "ram_upgrade_points": r.get("ram_upgrade_points"), "ram_type_points": r.get("ram_type_points"),
                "cpu_eff_points": r.get("cpu_eff_points"), "vram_points": r.get("vram_points"), "gpu_idle_points": r.get("gpu_idle_points"), "quality_points": r.get("quality_points"),
                "quality_score": r.get("quality_score"),
                "storage_points": r.get("storage_points"), "screen_points": r.get("screen_points"),
                "non_perf_points": r.get("non_perf_points"), "overall_score": r.get("overall_score"),
                "ram_slots": r.get("ram_slots"), "ram_max_gb": r.get("ram_max_gb"),
                "ram_note": r.get("ram_note"), "ram_source": r.get("ram_source"), "ram_detail": r.get("ram_detail"),
                "charging_type": r.get("charging_type"), "usbc_charging": r.get("usbc_charging"),
                "charging_detail": r.get("charging_detail"), "keyboard_backlit": r.get("keyboard_backlit"),
                "webcam_label": r.get("webcam_label"),
                "dealbreakers": "; ".join(r.get("dealbreakers") or []),
                "advantages": "; ".join(r.get("advantages") or []),
                "offers": "; ".join(f"{o['store']} {o['price']:.0f}" for o in (r.get("offers") or [])),
                "dup_count": r.get("dup_count"), "dup_max": r.get("dup_max"),
                "merged_into": r.get("merged_into"),
                "price_local": r.get("price_local"), "currency": r.get("currency"),
                "price_expected": r.get("price_expected"), "price_flag": r.get("price_flag"),
                "os": r["os"], "weight_kg": r["weight_kg"],
                "battery_label": r.get("battery_label"), "battery_wh": r.get("battery_wh"),
                "battery_mah": r.get("battery_mah"), "battery_points": r.get("battery_points"),
                "battery_source": r.get("battery_source"),
                "perf_score": r["perf_score"],
                "ws_score": r.get("ws_score"),
                "game_score": r.get("game_score"),
                "game_value": r.get("game_value"),
                "value_score": r["value_score"], "marketplace": r["marketplace"],
                "seller": r.get("seller"), "seller_note": r.get("seller_note"),
                "seller_issue_pct": r.get("seller_issue_pct"),
                "seller_positive_pct": r.get("seller_positive_pct"),
                "seller_reviews": r.get("seller_reviews"),
                "tags": "|".join(r["tags"]), "url": r["url"],
            })

    # viewer data as a plain script (works when opened via file://).
    # moat.py strips derivation/provenance and summarises the price series —
    # the viewer only ever gets the conclusions, never the evidence.
    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sanitize_records(records), ensure_ascii=False, separators=(",", ":"))
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    shortlist: list[dict] = []
    try:
        shortlist = json.loads((DATA_DIR / "shortlist.json").read_text(encoding="utf-8")).get("items", [])
    except Exception:  # noqa: BLE001
        shortlist = []
    meta = {
        "generated_at": generated_at,
        "count": len(records),
        "sources": {s: sum(1 for r in records if r["store"] == s)
                    for s in dict.fromkeys(r["store"] for r in records)},
        "dedupe": dedupe_stats,
        "tombstones": {"count": len(tombs), "ids": sorted(t["id"] for t in tombs),
                       "retention_days": TOMBSTONE_DAYS},
        "memory": memory_stats,
        "hardware": hw_stats,
        "ram_traps": ram_traps,
        "price_flags": price_flags,
        "battery_lookup": battery_lookup,
        "battery_estimates": battery_estimates,
        "quality_points": quality_stats,
        "model_codes": {"with_code": sum(1 for r in records if r.get("model_code")),
                        "filled_cross_store": model_codes_filled},
        "php_per_sar": 16.4,
        "benchmarks": {
            "source": "passmark (cpu mark + single thread, 70/30) + notebookcheck (r23 single/multi)",
            "cpu_single_weight": CPU_SINGLE_WEIGHT,
            "fetched_at": _bench().fetched_at,
            "nbc_fetched_at": (_nbc().fetched_at if _nbc() else None),
            "cpu_scored": sum(1 for r in records if (r["cpu"].get("benchmark") or {}).get("mark")),
            "cpu_single_scored": sum(1 for r in records if (r["cpu"].get("benchmark") or {}).get("single")),
            "cpu_nbc": sum(1 for r in records if (r["cpu"].get("benchmark") or {}).get("source") == "notebookcheck"),
            "gpu_scored": sum(1 for r in records if (r["gpu"].get("benchmark") or {}).get("mark")),
        },
    }
    # Affiliate tags, per store, as raw query strings (e.g.
    # NOON_AFFILIATE="utm_source=shop&utm_medium=affiliate&aff_id=XYZ" or the
    # exact `tag=`/`aff_id=` pair from the portal). Empty = links ship as-is.
    # Read from the environment at build time so no secret lives in the repo;
    # the viewer appends them via affUrl() and marks such links sponsored.
    # Only noon currently has a program (3.0%, AED 50/item cap, verified in
    # portal); Jarir/eXtra/PH have none — leave them empty until they do.
    affiliate = {
        key: os.environ.get(f"{key.upper()}_AFFILIATE", "").strip()
        for key in ("noon", "jarir", "extra", "datablitz", "pcx", "gigahertz")
    }
    affiliate = {k: v for k, v in affiliate.items() if v}
    (VIEWER_DIR / "laptops.js").write_text(
        "window.LAPTOPS_META = " + json.dumps(sanitize_meta(meta), ensure_ascii=False) + ";\n"
        + "window.SHORTLIST = " + json.dumps(shortlist, ensure_ascii=False, separators=(",", ":")) + ";\n"
        + "window.AFFILIATE = " + json.dumps(affiliate, ensure_ascii=False, separators=(",", ":")) + ";\n"
        + "window.LAPTOPS = " + payload + ";\n",
        encoding="utf-8",
    )
    # NOTE: no viewer/history.js — the full per-day series stays in data/.
    # Each record carries a summary (low/high/dates + downsampled trend).
    (DATA_DIR / "unified_laptops.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    total = len(records)
    missing = {
        "cpu": sum(1 for r in records if not r["cpu"]["text"]),
        "ram": sum(1 for r in records if not r["ram"]),
        "storage": sum(1 for r in records if not r["storage"]),
        "gpu": sum(1 for r in records if not r["gpu"]["text"]),
        "screen": sum(1 for r in records if not r["screen"]["inches"]),
        "os": sum(1 for r in records if not r["os"]),
        "weight": sum(1 for r in records if not r["weight_kg"]),
    }
    print(f"normalized {total} laptops ({'Jarir' if jarir else ''} {len(jarir)} + eXtra {len(extra)})")
    print("missing:", ", ".join(f"{k}={v}" for k, v in missing.items()))
    print("sources:", {s: sum(1 for r in records if r["store"] == s) for s in ("Jarir", "eXtra")})
    if records:
        prices = [r["price"] for r in records if r["price"]]
        print(f"price range: {min(prices):.0f} - {max(prices):.0f} SAR (median {statistics.median(prices):.0f})")
    deals = sorted((r for r in records if r.get("deal_ratio")), key=lambda r: -r["deal_ratio"])
    if deals:
        print(f"deals: {len(deals)} value outliers (>=1.5x class median)")
        for r in deals[:5]:
            print(f"   {r['deal_ratio']:.2f}x  SAR {r['price']:.0f}  perf {r['perf_score']:.0f}  "
                  f"{r.get('short_name') or r.get('name','')[:40]} ({r['store']})")
    print("wrote data/unified_laptops.json, data/unified_laptops.csv, data/unified_laptops.meta.json,")
    print("      data/price_history.jsonl, viewer/laptops.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
