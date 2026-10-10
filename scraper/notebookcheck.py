#!/usr/bin/env python3
"""Notebookcheck mobile-CPU benchmarks for the laptop chooser.

Notebookcheck reviews laptops under controlled conditions and aggregates
per-chip medians (plus sample counts and min/max ranges) across every
machine they tested.  Their benchmark list is the review-grade complement
to PassMark's crowd-sourced Single Thread Mark + CPU Mark:

* primary metrics: **Cinebench R23 single-core and multi-core** (industry-
  standard responsiveness + sustained render throughput, the columns with the
  widest review coverage).  ``normalize.apply_benchmarks`` combines them on
  the PassMark scale as 70% single / 30% multi.
* the same rows also carry Geekbench 6.7 / 5.5, Blender, 7-Zip, x265,
  CrossMark, WebXPRT and R15 medians with sample counts — stored in the
  snapshot for tooltips and future use

Why not Geekbench directly: browser.geekbench.com answers 403 to any
non-browser fetch (bot protection), so there is no reliable way to snapshot
it.  Notebookcheck's table already contains Geekbench 6.7 medians per chip,
which is the better version of that data anyway (controlled runs).

Why not Notebookcheck alone: brand-new chips land here thin or estimated
(``n=0`` rows for M5, Snapdragon X2, Panther Lake-U …) and Apple Silicon /
Snapdragon coverage trails PassMark.  So the precedence in
``normalize.apply_benchmarks`` is:

    NBC Cinebench R23 single + multi with n >= NBC_MIN_N  ->  PassMark
    Single Thread Mark + CPU Mark

Both sources are log-normalized onto the shared 0-100 scale: at snapshot load
we fit log(PassMark) = a + b * log(R23) over the exact-name overlap (r ≈ 0.98
for multi and single) and convert R23 through those fits, so a 75 means the
same thing either way.  Provenance (``source`` + ``n``) is stored per record,
never hidden.

Snapshots are checked in (manual strategy, like PassMark)::

    python3 scraper/notebookcheck.py --fetch   # refresh data/benchmarks/nbc_cpu.json
    python3 scraper/notebookcheck.py --check   # coverage of our dataset CPUs
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Fetcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "benchmarks"
DATA_DIR = ROOT / "data" / "benchmarks"
SNAPSHOT_PATH = DATA_DIR / "nbc_cpu.json"

LIST_URL = "https://www.notebookcheck.net/Mobile-Processors-Benchmark-List.2436.0.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Cinebench R23 medians below this sample count are not review-grade;
# those chips fall back to PassMark (see module docstring).
NBC_MIN_N = 3

# bv_* column ids on the benchmark list -> (key, larger_is_multi)
_COLUMNS = {
    "bv_244": ("r15", True),
    "bv_768": ("r23", True),
    "bv_717": ("gb55", False),   # single multi cell in practice
    "bv_967": ("gb67", True),
    "bv_552": ("zip", True),
    "bv_496": ("blender", False),  # seconds: smaller is better
    "bv_560": ("x265", False),
    "bv_860": ("crossmark", False),
    "bv_918": ("webxprt", False),
}


def _fold(text: str) -> str:
    text = htmlmod.unescape(text or "").upper()
    text = re.sub(r"[™®©]", " ", text)
    return re.sub(r"[\s\-–—]+", " ", re.sub(r"[^A-Z0-9+ ]", " ", text)).strip()


def parse_list_html(page: str) -> list[dict]:
    """Parse the benchmark list into [{name, url, benches:{key: {multi:{v,n}, single:{v,n} or value:{v,n}}}}]."""
    out: list[dict] = []
    for row in page.split("</tr>"):
        m = re.search(
            r'<a href="(https://www\.notebookcheck\.net/[^"]+?\.html)">([^<]+)</a>', row
        )
        if not m or "Benchmarks-and-Specs" not in m.group(1):
            continue
        url, name = m.group(1), htmlmod.unescape(m.group(2)).strip()
        benches: dict[str, dict] = {}
        for cm in re.finditer(
            r'<td class="value (bv_\w+)">.*?<span class="bl_med_val_\d+_\d+">([^<]+)</span>(?:<sup>n(\d+)</sup>)?',
            row,
            re.S,
        ):
            cls, raw, n = cm.group(1), cm.group(2).strip(), cm.group(3)
            if cls not in _COLUMNS:
                continue
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue
            key, larger_is_multi = _COLUMNS[cls]
            slot = benches.setdefault(key, {})
            if larger_is_multi:
                # multi + single share one column class; multi is the larger value
                if "multi" not in slot or value > slot["multi"]["v"]:
                    if "multi" in slot:
                        slot["single"] = slot["multi"]
                    slot["multi"] = {"v": value, "n": int(n) if n else 0}
                elif "single" not in slot or value > slot["single"]["v"]:
                    slot["single"] = {"v": value, "n": int(n) if n else 0}
            else:
                slot["value"] = {"v": value, "n": int(n) if n else 0}
        if benches:
            out.append({"name": name, "url": url, "benches": benches})
    return out


def fetch_snapshot(max_age_hours: float | None = 48) -> dict:
    fetcher = Fetcher(
        CACHE_DIR,
        min_interval=1.0,
        headers=HEADERS,
        max_age=(max_age_hours * 3600) if max_age_hours else None,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    page = fetcher.get(LIST_URL)
    entries = parse_list_html(page)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [LIST_URL],
        "count": len(entries),
        "entries": entries,
    }
    SNAPSHOT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"[notebookcheck] {len(entries)} chips -> {SNAPSHOT_PATH}")
    return {"entries": len(entries)}


# ---------------------------------------------------------------------------
# matching our parsed CPU texts to NBC rows
# ---------------------------------------------------------------------------

# our folded cpu text -> NBC row name (exact preferred, unique substring ok).
# Only needed where names genuinely differ (hyphenation is folded already).
NBC_NAMED = {
    "APPLE M5": "Apple M5 10-Core",
    "APPLE M5 PRO": "Apple M5 Pro 15-Core",
    "APPLE M5 MAX": "Apple M5 Max 18-Core",
    "APPLE M4": "Apple M4 10-Core",
    "APPLE M4 PRO": "Apple M4 Pro 12-Core",
    "APPLE M4 MAX": "Apple M4 Max 14-Core",
    "APPLE M3 PRO": "Apple M3 Pro 11-Core",
    "APPLE M3 MAX": "Apple M3 Max 14-Core",
    "APPLE M1 PRO": "Apple M1 Pro 8 Core",
    "APPLE M2 PRO": "Apple M2 Pro 10 Core",
    "APPLE A18 PRO": "Apple A18 Pro",
    "QUALCOMM SNAPDRAGON X PLUS": "Qualcomm Snapdragon X Plus X1P-64-100",
    "QUALCOMM SNAPDRAGON X ELITE": "Qualcomm Snapdragon X Elite X1E-00-1DE",
    "QUALCOMM SNAPDRAGON X": "Qualcomm Snapdragon X X1-26-100",
    "QUALCOMM SNAPDRAGON X2 ELITE": "Qualcomm Snapdragon X2 Elite X2E-80-100",
    "QUALCOMM SNAPDRAGON X2 PLUS": "Qualcomm Snapdragon X2 Plus X2P-64-100",
    "INTEL CORE ULTRA 9 290HX": "Intel Core Ultra 9 290HX Plus",
    "INTEL CORE ULTRA X9": "Intel Core Ultra X9 378H",
    "INTEL CORE I7 1335U": "Intel Core i7-1355U",
    "INTEL CORE ULTRA 5 120U": "Intel Core 5 120U",
    "INTEL CORE 3 355": "Intel Core 3 N355",
    "INTEL N SERIES": "Intel N150",
}


class NbcData:
    def __init__(self, entries: list[dict], fetched_at: str | None) -> None:
        self.entries = entries
        self.fetched_at = fetched_at
        self._exact = {_fold(e["name"]): e for e in entries}
        self._names = [(e, _fold(e["name"])) for e in entries]

    def resolve(self, wanted: str) -> dict | None:
        key = _fold(wanted)
        for entry, norm in self._names:
            if norm == key:
                return entry
        hits = [entry for entry, norm in self._names if key and key in norm]
        return hits[0] if len(hits) == 1 else None


def load_nbc() -> NbcData | None:
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    entries = payload.get("entries") or []
    if not entries:
        return None
    return NbcData(entries, payload.get("fetched_at"))


def _trailing_token(text_folded: str) -> str | None:
    toks = re.findall(r"X[79]\b|[A-Z]?\d{2,5}[A-Z]{0,3}\b", text_folded)
    return toks[-1] if toks else None


def _brand_of(key: str) -> str | None:
    if "INTEL" in key:
        return "INTEL"
    if "RYZEN" in key or "ATHLON" in key or "AMD" in key:
        return "AMD"
    if "APPLE" in key or re.search(r"\bM\d\b", key):
        return "APPLE"
    if "SNAPDRAGON" in key or "QUALCOMM" in key:
        return "QUALCOMM"
    return None


def lookup_nbc(nbc: NbcData, text: str | None) -> tuple[dict | None, str | None]:
    """Return (row, mapping) for a parsed CPU text, or (None, None)."""
    if not text or not text.strip():
        return None, None
    key = _fold(text)
    entry = nbc._exact.get(key)
    if entry:
        return entry, "nbc-exact"
    named = NBC_NAMED.get(key)
    if named:
        hit = nbc.resolve(named)
        if hit:
            return hit, "nbc-named"
    brand = _brand_of(key)

    def brand_ok(norm: str) -> bool:
        if not brand:
            return True
        if brand == "INTEL":
            return "INTEL" in norm
        if brand == "AMD":
            return "RYZEN" in norm or "ATHLON" in norm or "AMD" in norm
        if brand == "APPLE":
            return "APPLE" in norm
        if brand == "QUALCOMM":
            return "SNAPDRAGON" in norm or "QUALCOMM" in norm
        return True

    token = _trailing_token(key)
    if token:
        rx = re.compile(r"(?<![A-Z0-9])" + re.escape(token) + r"(?![A-Z0-9])")
        cands = [e for e, norm in nbc._names if rx.search(norm) and brand_ok(norm)]
        if len(cands) == 1:
            return cands[0], "nbc-token"
        if re.fullmatch(r"\d{2,5}", token):
            hits: list[dict] = []
            for suffix in ("U", "H", "HX"):
                rx2 = re.compile(r"(?<![A-Z0-9])" + re.escape(token + suffix) + r"(?![A-Z0-9])")
                hits += [e for e, norm in nbc._names if rx2.search(norm) and brand_ok(norm)]
            if len(hits) == 1:
                return hits[0], "nbc-suffix-repair"
        if token[-1:] in ("U", "V"):
            swap = token[:-1] + ("V" if token.endswith("U") else "U")
            rx3 = re.compile(r"(?<![A-Z0-9])" + re.escape(swap) + r"(?![A-Z0-9])")
            cands3 = [e for e, norm in nbc._names if rx3.search(norm) and brand_ok(norm)]
            if len(cands3) == 1:
                return cands3[0], "nbc-u-v-swap"
        m = re.match(r"([3579]?)(\d{2,4}[A-Z]{1,3})$", token)
        if m:
            rx4 = re.compile(r"(?<![0-9])[3579]?" + re.escape(m.group(2)) + r"(?![0-9A-Z])")
            cands4 = [e for e, norm in nbc._names if rx4.search(norm) and brand_ok(norm)]
            if len(cands4) == 1:
                return cands4[0], "nbc-tier-relaxed"
    return None, None


def r23_multi(row: dict | None) -> tuple[float | None, int]:
    """(median value, sample count) of Cinebench R23 multi, or (None, 0)."""
    if not row:
        return None, 0
    multi = (row.get("benches") or {}).get("r23", {}).get("multi") or {}
    v, n = multi.get("v"), multi.get("n", 0)
    return (float(v), int(n)) if v else (None, 0)


def r23_single(row: dict | None) -> tuple[float | None, int]:
    """(median value, sample count) of Cinebench R23 single, or (None, 0)."""
    if not row:
        return None, 0
    single = (row.get("benches") or {}).get("r23", {}).get("single") or {}
    v, n = single.get("v"), single.get("n", 0)
    return (float(v), int(n)) if v else (None, 0)


def fit_pm_equiv(pm_entries: list[dict], single: bool = False) -> tuple[float, float, int]:
    """Fit log(PassMark) = a + b*log(R23) over exact-name overlap rows.

    ``single=False`` maps R23 multi to PassMark CPU Mark; ``single=True`` maps
    R23 single to the PassMark Single Thread Mark.  Returns (a, b, overlap_count)
    so R23 medians convert to equivalent PassMark marks and both sources share
    one 0-100 scale.  The fit is outlier-trimmed: ancient/desktop rows
    occasionally carry junk cells (wrong units), which must not tilt the laptop
    scale.
    """
    nbc = load_nbc()
    if nbc is None:
        return 0.0, 1.0, 0
    pm_key = "single" if single else "mark"
    pm_floor = 200 if single else 1000
    r23_floor = 500 if single else 1000
    pm_exact: dict[str, int] = {}
    for e in pm_entries:
        val = e.get(pm_key)
        if val:
            pm_exact.setdefault(_fold(e["name"]), val)
    pts = []
    for entry, norm in nbc._names:
        mark = pm_exact.get(norm)
        v, n = r23_single(entry) if single else r23_multi(entry)
        if mark and v and v > r23_floor and mark > pm_floor and n >= NBC_MIN_N:
            pts.append((math.log(v), math.log(mark)))

    def ols(ps: list[tuple[float, float]]) -> tuple[float, float]:
        mx = sum(x for x, _ in ps) / len(ps)
        my = sum(y for _, y in ps) / len(ps)
        var = sum((x - mx) ** 2 for x, _ in ps)
        b = sum((x - mx) * (y - my) for x, y in ps) / var if var else 1.0
        return my - b * mx, b

    if len(pts) < 8:
        return 0.0, 1.0, len(pts)
    a, b = ols(pts)
    for _ in range(3):  # trim the 5% worst residuals, refit
        scored = sorted(pts, key=lambda p: abs(p[1] - (a + b * p[0])), reverse=True)
        pts = scored[len(scored) // 20:] or scored
        if len(pts) < 8:
            break
        a, b = ols(pts)
    if not 0.5 <= b <= 1.6:  # fit failed sanity: fall back to identity
        return 0.0, 1.0, len(pts)
    return a, b, len(pts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--max-age", type=float, default=48, metavar="HOURS")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.fetch:
        fetch_snapshot(None if args.max_age == 0 else args.max_age)
        return 0
    nbc = load_nbc()
    if nbc is None:
        print("snapshot missing — run: python3 scraper/notebookcheck.py --fetch")
        return 1
    print(f"nbc chips: {len(nbc.entries)}  fetched_at: {nbc.fetched_at}")
    if args.check:
        from collections import Counter
        records = json.loads((ROOT / "data" / "unified_laptops.json").read_text(encoding="utf-8"))
        by_map: Counter = Counter()
        missing: Counter = Counter()
        thin: Counter = Counter()
        for rec in records:
            cpu = rec.get("cpu") or {}
            row, mapping = lookup_nbc(nbc, cpu.get("text"))
            v, n = r23_multi(row)
            if v and n >= NBC_MIN_N:
                by_map[f"{mapping} n>={NBC_MIN_N}"] += 1
            elif v:
                by_map["thin-sample fallback"] += 1
                thin[cpu.get("text") or "NONE"] += 1
            else:
                by_map["no-row fallback"] += 1
                missing[cpu.get("text") or "NONE"] += 1
        print("nbc r23 coverage:", dict(by_map))
        print(f"-- thin sample ({len(thin)}):")
        for k, c in thin.most_common(20):
            print(f"   {c:3d} x {k}")
        print(f"-- no row ({len(missing)}):")
        for k, c in missing.most_common(20):
            print(f"   {c:3d} x {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
