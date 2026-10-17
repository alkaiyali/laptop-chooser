#!/usr/bin/env python3
"""Phone SoC benchmarks for the phone chooser (AnTuTu v10, curated table).

The laptop pipeline reads measured PassMark/Notebookcheck snapshots
(``data/benchmarks/``); phones get the same treatment with the mobile-native
metric: AnTuTu v10 totals, curated in ``data/benchmarks/phone_soc.json`` as
approximate medians of published device runs (Oct 2025). Apple rows are iOS
AnTuTu runs, cross-comparable with Android rows within ~10%.

Matching (first hit wins): exact name -> unique substring -> token repair
("8 Elite" -> Snapdragon 8 Elite, "A20 Pro" -> Apple A20 Pro) -> family
median ("Snapdragon 7" -> median of the 7-series rows). The match kind is
reported per record as ``soc.benchmark.mapping`` so the viewer can label
approximations honestly.

Scores are 0-100 on a log scale over [MIN, MAX] so the phone scale stays
stable as new chips are appended to the table.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = ROOT / "data" / "benchmarks" / "phone_soc.json"

MIN_MARK, MAX_MARK = 200_000, 3_200_000

_table: dict | None = None


def _load() -> dict:
    global _table
    if _table is None:
        try:
            _table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _table = {"entries": []}
    return _table


def _norm(text: str) -> str:
    text = re.sub(r"[™®©]", " ", text or "").upper()
    text = re.sub(r"\bBIONIC\b", " ", text)
    text = re.sub(r"\bCHIP\b", " ", text)
    text = re.sub(r"\bQUALCOM\b", "QUALCOMM", text)  # merchant typo
    text = re.sub(r"\bD(\d{3,4})\b", r"DIMENSITY \1", text)  # "D7300" -> Dimensity 7300
    text = re.sub(r"\bGEN(\d)", r"GEN \1", text)  # "GEN2" -> "GEN 2"
    text = re.sub(r"[^A-Z0-9+ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def entries() -> list[dict]:
    return _load().get("entries", [])


def scale(mark: int | None) -> float | None:
    """Log-scale an AnTuTu mark onto 0-100."""
    if not mark or mark <= 0:
        return None
    lo_l, hi_l = math.log(MIN_MARK), math.log(MAX_MARK)
    v = (math.log(max(mark, MIN_MARK)) - lo_l) / (hi_l - lo_l)
    return round(min(max(v, 0.0), 1.0) * 100, 1)


# Part-number / vendor-code aliases the generic matcher cannot resolve:
# Qualcomm SMxxxx and MediaTek MTxxxx codes, MTK short forms.
ALIASES = {
    "SM8750": "Snapdragon 8 Elite",
    "QUALCOMM SNAPDRAGON SM8750": "Snapdragon 8 Elite",
    "SM7325": "Snapdragon 778G",
    "QUALCOMM SM7325": "Snapdragon 778G",
    "SM7653": "Snapdragon 7s Gen 3",
    "MT6989": "Dimensity 9300",
    "MEDIATEK G81 ULTRA": "Helio G81",
    "MEDIATEK HELIO G81": "Helio G81",
}


# (our-text pattern, table-name pattern); first hit wins
FAMILY_PATTERNS = [
    (r"DIMENSITY\s?95", r"DIMENSITY 95"),
    (r"8\s?ELITE", r"8 ELITE"),
    (r"8\s?GEN\s?3", r"8 GEN 3"),
    (r"8\s?GEN\s?2", r"8 GEN 2"),
    (r"DIMENSITY\s?94", r"DIMENSITY 94"),
    (r"DIMENSITY\s?93", r"DIMENSITY 93"),
    (r"DIMENSITY\s?92", r"DIMENSITY 92"),
    (r"DIMENSITY\s?84", r"DIMENSITY 84"),
    (r"DIMENSITY\s?8350", r"DIMENSITY 8350"),
    (r"DIMENSITY\s?83", r"DIMENSITY 83"),
    (r"DIMENSITY\s?82", r"DIMENSITY 82"),
    (r"DIMENSITY\s?74", r"DIMENSITY 74"),
    (r"DIMENSITY\s?73", r"DIMENSITY 73"),
    (r"DIMENSITY\s?72", r"DIMENSITY 72"),
    (r"SNAPDRAGON\s?7", r"SNAPDRAGON 7"),
    (r"SNAPDRAGON\s?6", r"SNAPDRAGON 6"),
    (r"SNAPDRAGON\s?4", r"SNAPDRAGON 4"),
    (r"SNAPDRAGON\s?778", r"SNAPDRAGON 778"),
    (r"EXYNOS\s?26", r"EXYNOS 26"),
    (r"EXYNOS\s?24", r"EXYNOS 24"),
    (r"EXYNOS\s?22", r"EXYNOS 22"),
    (r"EXYNOS\s?16", r"EXYNOS 16"),
    (r"EXYNOS\s?15", r"EXYNOS 15"),
    (r"EXYNOS\s?14", r"EXYNOS 14"),
    (r"EXYNOS\s?13", r"EXYNOS 13"),
    (r"TENSOR\s?G5", r"TENSOR G5"),
    (r"TENSOR\s?G4", r"TENSOR G4"),
    (r"TENSOR\s?G3", r"TENSOR G3"),
    (r"KIRIN\s?90", r"KIRIN 90"),
    (r"KIRIN\s?80", r"KIRIN 80"),
    (r"HELIO\s?G10", r"HELIO G10"),
    (r"HELIO\s?G9", r"HELIO G9"),
    (r"HELIO\s?G8", r"HELIO G8"),
    (r"\bA1[6789]\b", None),  # Apple A-series: handled by token rule below
    (r"UNISOC\s?T", r"UNISOC T"),
]


def lookup(text: str | None) -> tuple[int | None, str | None, str | None]:
    """Return (antutu mark, table name, mapping) for a free-text SoC string."""
    rows = entries()
    if not text or not text.strip() or not rows:
        return None, None, None
    key = _norm(text)
    by_norm = {_norm(e["name"]): e for e in rows}

    def hit(entry: dict, mapping: str) -> tuple[int | None, str | None, str | None]:
        # estimated table rows (post-cutoff chips) keep honest provenance
        if entry.get("est"):
            mapping = "estimate"
        return entry["antutu_v10"], entry["name"], mapping

    if key in by_norm:
        return hit(by_norm[key], "exact")

    alias = ALIASES.get(key) or ALIASES.get(key.split(" ")[0])
    if alias:
        target = by_norm.get(_norm(alias))
        if target:
            return hit(target, "alias")

    # Apple "A20 Pro" / "A18" short forms the stores use
    m = re.search(r"\bA(1[5-9]|20)\b\s*(PRO|MAX)?", key)
    if m:
        want = f"APPLE A{m.group(1)}" + (f" {m.group(2).title()}" if m.group(2) else "")
        found = by_norm.get(_norm(want))
        if found:
            return hit(found, "apple-short")
        fam = [e for e in rows if _norm(e["name"]).startswith(_norm(f"Apple A{m.group(1)}"))]
        if fam:
            med = int(statistics.median(e["antutu_v10"] for e in fam))
            return med, fam[0]["name"], "apple-family"

    # unique substring ("Dimensity 7300" inside "MediaTek Dimensity 7300")
    hits = [e for e in rows if _norm(e["name"]) and _norm(e["name"]) in key]
    if len(hits) == 1:
        return hit(hits[0], "substring")
    rev = [e for e in rows if key and key in _norm(e["name"])]
    if len(rev) == 1:
        return hit(rev[0], "substring-rev")

    # token repair: trailing model token as standalone word
    toks = re.findall(r"[A-Z]?\d{3,4}[A-Z]{0,3}\b|\b8 ELITE\b|\bA\d\d\b", key)
    for tok in toks[::-1]:
        rx = re.compile(r"(?<![A-Z0-9])" + re.escape(tok) + r"(?![A-Z0-9])")
        cands = [e for e in rows if rx.search(_norm(e["name"]))]
        if len(cands) == 1:
            return hit(cands[0], "token")

    # family median
    for ours, theirs in FAMILY_PATTERNS:
        if not re.search(ours, key) or theirs is None:
            continue
        pool = [e["antutu_v10"] for e in rows if re.search(theirs, _norm(e["name"]))]
        if pool:
            med = int(statistics.median(pool))
            return med, f"median of {theirs}", "family-median"
    return None, None, None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("soc", nargs="?", help='SoC string to test, e.g. "Snapdragon 8 Gen 3"')
    args = parser.parse_args()
    rows = entries()
    print(f"table: {len(rows)} entries ({TABLE_PATH})")
    if args.soc:
        mark, name, mapping = lookup(args.soc)
        print(f"{args.soc!r} -> {mark} ({name}, {mapping}) score={scale(mark)}")
    else:
        for demo in ["Apple A20 Pro", "A18 Pro Bionic chip", "Snapdragon 8 Gen 3",
                     "Dimensity 7300", "Exynos 1580", "Helio G99", "Tensor G4"]:
            mark, name, mapping = lookup(demo)
            print(f"  {demo!r} -> {mark} ({name}, {mapping}) score={scale(mark)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
