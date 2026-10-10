#!/usr/bin/env python3
"""Real PassMark CPU/GPU benchmarks for the laptop chooser.

The chooser used to rank machines with a hand-rolled heuristic
(``_cpu_score`` / ``_gpu_score`` in ``normalize.py``).  This module replaces
that with measured numbers:

* CPU: PassMark **CPU Mark** (multithreaded aggregate) and **Single Thread**
  Mark, both from millions of submissions and updated daily.  The two are
  combined into the CPU score as 70% single / 30% multi (``CPU_SINGLE_WEIGHT``):
  everyday snappiness and gaming are dominated by single-core speed, so core
  count only moves the score a little.
* GPU: PassMark **G3D Mark**, preferring the ``... Laptop GPU`` variants
  because every machine in this dataset is a laptop

Why PassMark and not Geekbench / Notebookcheck / UserBenchmark:

* one vendor covers both CPU and GPU with the same methodology, so the two
  halves of ``perf_score`` stay comparable
* the chart pages are static HTML that parse reliably (Geekbench's browser
  answers 403 to simple fetches; Notebookcheck is per-laptop, not per-chip)
* daily updates, huge sample sizes, and explicit laptop/mobile charts

Update strategy (deliberately manual): the measured tables are checked in as
``data/benchmarks/cpu_passmark.json`` + ``gpu_passmark.json``.  Refresh them
with ``python3 scraper/benchmarks.py --fetch`` (honours ``--max-age`` like
the store scrapers); ``normalize.py`` itself never touches the network and
just reads the checked-in snapshots.

Two deliberate approximations, both flagged per-record in
``cpu["benchmark"]["mapping"]`` / ``gpu["benchmark"]["mapping"]``:

* integrated GPUs have no G3D row of their own (``Intel Integrated
  Graphics``, ``Radeon Graphics`` ...), so they are resolved through the
  host CPU family (``cpu-family`` mapping, see ``igpu_for_cpu``)
* Apple Silicon GPUs have no G3D rows at all, so the GPU score proxies the
  CPU score of the same M-chip (``apple-cpu-proxy`` mapping)
* bare family names with no model (``AMD Ryzen 7``, ``Core i7`` ...) resolve
  to the median mark of that family's mobile entries (``family-median``)
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Fetcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "benchmarks"
DATA_DIR = ROOT / "data" / "benchmarks"
OVERRIDES_PATH = DATA_DIR / "overrides.json"

CPU_CHART_URLS = [
    "https://www.cpubenchmark.net/laptop.html",
    "https://www.cpubenchmark.net/multithread",
    "https://www.cpubenchmark.net/multithread/page2",
    "https://www.cpubenchmark.net/multithread/page3",
    "https://www.cpubenchmark.net/multithread/page4",
]
CPU_SINGLE_CHART_URLS = [
    "https://www.cpubenchmark.net/singlethread.html",
]
GPU_CHART_URLS = [
    "https://www.videocardbenchmark.net/high_end_gpus.html",
    "https://www.videocardbenchmark.net/mid_range_gpus.html",
    "https://www.videocardbenchmark.net/midlow_range_gpus.html",
    "https://www.videocardbenchmark.net/low_end_gpus.html",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# log-scale anchors: raw marks outside [MIN, MAX] clip to 0 / 100.
CPU_MIN, CPU_MAX = 1000, 65000                      # multithread CPU Mark
CPU_SINGLE_MIN, CPU_SINGLE_MAX = 700, 7000          # Single Thread Mark
                                                    # (headroom: top laptop
                                                    # mark ~5.9k, like multi)
GPU_MIN, GPU_MAX = 500, 42000

# CPU score = w * single-thread + (1 - w) * multithread. Single-core dominates
# because it is what makes a machine feel fast (web, office, games).
CPU_SINGLE_WEIGHT = 0.7


# ---------------------------------------------------------------------------
# snapshot fetch + parse
# ---------------------------------------------------------------------------

def parse_chart_html(page: str, base: str) -> list[dict]:
    """Parse one PassMark chart page into [{id, name, mark, url}].

    Each chart (mark / value / date tabs) repeats the same entries, so callers
    must dedupe by id keeping the first hit (the CPU/G3D Mark tab).
    """
    out: list[dict] = []
    for chunk in page.split("<li")[1:]:
        chunk = chunk[:3000]
        m1 = re.search(r'href="([^"]+)"', chunk)
        m2 = re.search(r'prdname"[^>]*>(.*?)</span>', chunk)
        m3 = re.search(r'class="count">([\d,]+)</span>', chunk)
        if not (m1 and m2 and m3):
            continue
        href = htmlmod.unescape(m1.group(1))
        mid = re.search(r"[?&]id=(\d+)", href)
        if not mid:
            continue
        try:
            mark = int(m3.group(1).replace(",", ""))
        except ValueError:
            continue
        name = htmlmod.unescape(m2.group(1)).strip()
        if not name or mark <= 0:
            continue
        if href.startswith("/"):
            href = base + href
        elif href.startswith("gpu.php") or href.startswith("cpu.php"):
            href = base + "/" + href
        out.append({"id": mid.group(1), "name": name, "mark": mark, "url": href})
    return out


def fetch_snapshots(max_age_hours: float | None = 48) -> dict:
    """Download every chart page and rewrite the checked-in snapshots."""
    fetcher = Fetcher(
        CACHE_DIR,
        min_interval=1.0,
        headers=HEADERS,
        max_age=(max_age_hours * 3600) if max_age_hours else None,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats: dict = {"fetched_at": fetched_at, "cpu": {}, "gpu": {}}

    for kind, urls, path, base in (
        ("cpu", CPU_CHART_URLS, DATA_DIR / "cpu_passmark.json", "https://www.cpubenchmark.net"),
        ("gpu", GPU_CHART_URLS, DATA_DIR / "gpu_passmark.json", "https://www.videocardbenchmark.net"),
    ):
        seen: dict[str, dict] = {}
        for url in urls:
            try:
                page = fetcher.get(url)
            except Exception as exc:  # noqa: BLE001
                print(f"[benchmarks] WARN: {url}: {exc}")
                continue
            for entry in parse_chart_html(page, base):
                seen.setdefault(entry["id"], entry)
        # CPU only: attach the Single Thread Mark for every entry we already know
        if kind == "cpu":
            urls = urls + CPU_SINGLE_CHART_URLS
            for url in CPU_SINGLE_CHART_URLS:
                try:
                    page = fetcher.get(url)
                except Exception as exc:  # noqa: BLE001
                    print(f"[benchmarks] WARN: {url}: {exc}")
                    continue
                for entry in parse_chart_html(page, base):
                    row = seen.get(entry["id"])
                    if row and row.get("mark"):
                        row["single"] = entry["mark"]
        payload = {
            "fetched_at": fetched_at,
            "sources": urls,
            "count": len(seen),
            "entries": sorted(seen.values(), key=lambda e: -e["mark"]),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        stats[kind] = {"entries": len(seen), "path": str(path)}
        if kind == "cpu":
            stats[kind]["with_single"] = sum(1 for e in seen.values() if e.get("single"))
        print(f"[benchmarks] {kind}: {len(seen)} entries -> {path}")
    print(f"[benchmarks] network={fetcher.stats['network']} cache={fetcher.stats['cache']}")
    return stats


# ---------------------------------------------------------------------------
# snapshot loading + matching helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = re.sub(r"[™®©]", " ", text or "").upper()
    text = re.sub(r"[^A-Z0-9+ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _load_entries(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    except Exception:  # noqa: BLE001
        return []


def _load_overrides() -> dict:
    try:
        return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


class Benchmarks:
    """Checked-in PassMark tables plus the matcher state built over them."""

    def __init__(self) -> None:
        self.cpu = _load_entries(DATA_DIR / "cpu_passmark.json")
        self.gpu = _load_entries(DATA_DIR / "gpu_passmark.json")
        self.overrides = _load_overrides()
        try:
            self.fetched_at = json.loads(
                (DATA_DIR / "cpu_passmark.json").read_text(encoding="utf-8")
            ).get("fetched_at")
        except Exception:  # noqa: BLE001
            self.fetched_at = None

        self._cpu_exact = {_norm(e["name"]): e for e in self.cpu}
        self._cpu_names = [(e, _norm(e["name"])) for e in self.cpu]
        self._gpu_exact = {_norm(e["name"]): e for e in self.gpu}
        self._gpu_names = [(e, _norm(e["name"])) for e in self.gpu]
        self._family_cache: dict[str, int | None] = {}

    # -- generic name resolution -----------------------------------------
    def _resolve(self, wanted: str, names: list[tuple[dict, str]]) -> dict | None:
        """Resolve a wanted PassMark name: exact hit, else unique substring."""
        key = _norm(wanted)
        for entry, norm in names:
            if norm == key:
                return entry
        hits = [entry for entry, norm in names if key and key in norm]
        return hits[0] if len(hits) == 1 else None


# Named families whose bare name maps to one base PassMark entry.
# (our normalized cpu text -> unique PassMark substring; see module docstring)
CPU_NAMED = {
    "APPLE M5": "Apple M5 10 Core",
    "APPLE M5 PRO": "Apple M5 Pro 15 Core",
    "APPLE M5 MAX": "Apple M5 Max 18 Core",
    "APPLE M4": "Apple M4 10 Core",
    "APPLE M4 PRO": "Apple M4 Pro 12 Core",
    "APPLE M4 MAX": "Apple M4 Max 14 Core",
    "APPLE M3 PRO": "Apple M3 Pro 11 Core",
    "APPLE M3 MAX": "Apple M3 Max 14 Core",
    "APPLE M1 PRO": "Apple M1 Pro 8 Core",
    "APPLE M2 PRO": "Apple M2 Pro 10 Core",
    "QUALCOMM SNAPDRAGON X PLUS": "Snapdragon X Plus - X1P42100",
    "QUALCOMM SNAPDRAGON X ELITE": "Qualcomm Snapdragon X Elite - X1E001DE",
    "QUALCOMM SNAPDRAGON X": "Snapdragon X - X126100",
    "QUALCOMM SNAPDRAGON": "Snapdragon X Plus - X1P42100",
    "QUALCOMM SNAPDRAGON X2 ELITE": "Snapdragon X2 Elite - X2E80100",
    "QUALCOMM SNAPDRAGON X2 PLUS": "Snapdragon X2 Plus - X2P42100",
    "QUALCOMM SNAPDRAGON X2": "Snapdragon X2 Plus - X2P42100",
    "INTEL CORE ULTRA 9 290HX": "Intel Core Ultra 9 290HX Plus",
    "INTEL CORE ULTRA X9": "Intel Core Ultra X9 378H",
    "INTEL N SERIES": "Intel N150",
}

# (our-text pattern, snapshot-name pattern); first hit wins, mobilePnly for
# Intel/AMD so desktop chips don't skew the median (Apple/Qualcomm exempt).
FAMILY_PATTERNS = [
    (r"RYZEN AI MAX", r"RYZEN AI MAX", True),
    (r"RYZEN AI 9", r"RYZEN AI 9", True),
    (r"RYZEN AI 7", r"RYZEN AI 7", True),
    (r"RYZEN AI 5", r"RYZEN AI 5", True),
    (r"RYZEN AI", r"RYZEN AI", True),
    (r"RYZEN 9", r"RYZEN 9", True),
    (r"RYZEN 7", r"RYZEN 7", True),
    (r"RYZEN 5", r"RYZEN 5", True),
    (r"RYZEN 3", r"RYZEN 3", True),
    (r"ATHLON", r"ATHLON", True),
    (r"ULTRA X9", r"ULTRA X9", True),
    (r"ULTRA X7", r"ULTRA X7", True),
    (r"ULTRA 9", r"ULTRA 9", True),
    (r"ULTRA 7", r"ULTRA 7", True),
    (r"ULTRA 5", r"ULTRA 5", True),
    (r"\bCORE I9\b", r"CORE I9", True),
    (r"\bCORE I7\b", r"CORE I7", True),
    (r"\bCORE I5\b", r"CORE I5", True),
    (r"\bCORE I3\b", r"CORE I3", True),
    (r"\bCORE 9\b", r"CORE 9", True),
    (r"\bCORE 7\b", r"CORE 7", True),
    (r"\bCORE 5\b", r"CORE 5", True),
    (r"\bCORE 3\b", r"CORE 3", True),
    (r"SNAPDRAGON X2 ELITE", r"X2 ELITE", False),
    (r"SNAPDRAGON X2 PLUS", r"X2 PLUS", False),
    (r"SNAPDRAGON X ELITE", r"X ELITE", False),
    (r"SNAPDRAGON X PLUS", r"X PLUS", False),
    (r"SNAPDRAGON", r"SNAPDRAGON", False),
    (r"M5 MAX", r"M5 MAX", False),
    (r"M5 PRO", r"M5 PRO", False),
    (r"\bM5\b", r"\bM5\b", False),
    (r"M4 MAX", r"M4 MAX", False),
    (r"M4 PRO", r"M4 PRO", False),
    (r"\bM4\b", r"\bM4\b", False),
    (r"A18", r"A18", False),
]

_MOBILE_SUFFIX = re.compile(r"(HX|HS|[UHV])\b")


def _brand_of(text_norm: str) -> str | None:
    if "INTEL" in text_norm or re.search(r"\bI[3579]\b", text_norm):
        return "INTEL"
    if "RYZEN" in text_norm or "ATHLON" in text_norm or re.search(r"\bAMD\b", text_norm):
        return "AMD"
    if "APPLE" in text_norm or re.search(r"\bM\d\b", text_norm):
        return "APPLE"
    if "SNAPDRAGON" in text_norm or "QUALCOMM" in text_norm:
        return "QUALCOMM"
    return None


def family_median(bench: Benchmarks, text_norm: str) -> tuple[int | None, int | None, str | None]:
    """(median multi, median single) marks of the named family (or Nones)."""
    for ours, theirs, mobile_only in FAMILY_PATTERNS:
        if not re.search(ours, text_norm):
            continue
        if theirs in bench._family_cache:
            hit, single = bench._family_cache[theirs]
            return (hit, single, "family-median") if hit else (None, None, None)
        rx = re.compile(theirs)
        rows = [
            (e["mark"], e.get("single"))
            for e, norm in bench._cpu_names
            if e.get("mark") and rx.search(norm) and (not mobile_only or _MOBILE_SUFFIX.search(norm))
        ]
        hit = int(statistics.median([m for m, _ in rows])) if rows else None
        singles = [s for _, s in rows if s]
        single = int(statistics.median(singles)) if singles else None
        bench._family_cache[theirs] = (hit, single)
        return (hit, single, "family-median") if hit else (None, None, None)
    return None, None, None


def _trailing_token(text_norm: str) -> str | None:
    toks = re.findall(r"X[79]\b|[A-Z]?\d{2,5}[A-Z]{0,3}\b", text_norm)
    return toks[-1] if toks else None


def lookup_cpu(bench: Benchmarks, text: str | None) -> tuple[int | None, int | None, str | None, str | None]:
    """Return (multi mark, single mark, passmark_name, mapping) for a parsed CPU text."""
    if not text or not text.strip():
        return None, None, None, None
    key = _norm(text)

    entry = bench._cpu_exact.get(key)
    if entry:
        return entry["mark"], entry.get("single"), entry["name"], "exact"

    # hand overrides: {"cpu": {our_text: passmark_name_or_substring}}
    want = (bench.overrides.get("cpu") or {}).get(text.strip())
    if want:
        hit = bench._resolve(want, bench._cpu_names)
        if hit:
            return hit["mark"], hit.get("single"), hit["name"], "override"

    named = CPU_NAMED.get(key)
    if named:
        hit = bench._resolve(named, bench._cpu_names)
        if hit:
            return hit["mark"], hit.get("single"), hit["name"], "named-family"

    brand = _brand_of(key)

    def brand_ok(norm: str) -> bool:
        if not brand:
            return True
        if brand == "INTEL":
            return "INTEL" in norm or "CORE" in norm or "CELERON" in norm or "PENTIUM" in norm or norm.startswith("N1")
        if brand == "AMD":
            return "RYZEN" in norm or "ATHLON" in norm or "AMD" in norm
        if brand == "APPLE":
            return "APPLE" in norm or re.search(r"\bM\d\b", norm) is not None
        if brand == "QUALCOMM":
            return "SNAPDRAGON" in norm or "QUALCOMM" in norm
        return True

    token = _trailing_token(key)
    if token:
        # 1. token as a standalone word ("258V" -> Ultra 7 258V)
        rx = re.compile(r"(?<![A-Z0-9])" + re.escape(token) + r"(?![A-Z0-9])")
        cands = [e for e, norm in bench._cpu_names if rx.search(norm) and brand_ok(norm)]
        if len(cands) == 1:
            return cands[0]["mark"], cands[0].get("single"), cands[0]["name"], "token"
        # 2. bare number gains a suffix ("150" -> 150U, "13620" -> 13620H)
        if re.fullmatch(r"\d{2,5}", token):
            hits: list[dict] = []
            for suffix in ("U", "H", "HX"):
                rx2 = re.compile(r"(?<![A-Z0-9])" + re.escape(token + suffix) + r"(?![A-Z0-9])")
                hits += [e for e, norm in bench._cpu_names if rx2.search(norm) and brand_ok(norm)]
            if len(hits) == 1:
                return hits[0]["mark"], hits[0].get("single"), hits[0]["name"], "suffix-repair"
        # 3. U/V mixup ("226U" -> 226V, merchant typo class)
        if token[-1:] in ("U", "V"):
            swap = token[:-1] + ("V" if token.endswith("U") else "U")
            rx3 = re.compile(r"(?<![A-Z0-9])" + re.escape(swap) + r"(?![A-Z0-9])")
            cands3 = [e for e, norm in bench._cpu_names if rx3.search(norm) and brand_ok(norm)]
            if len(cands3) == 1:
                return cands3[0]["mark"], cands3[0].get("single"), cands3[0]["name"], "u-v-swap"
        # 4. tier mixup, same number+suffix ("Ultra 9 258V" -> Ultra 7 258V)
        m = re.match(r"([3579]?)(\d{2,4}[A-Z]{1,3})$", token)
        if m:
            rx4 = re.compile(r"(?<![0-9])[3579]?" + re.escape(m.group(2)) + r"(?![0-9A-Z])")
            cands4 = [e for e, norm in bench._cpu_names if rx4.search(norm) and brand_ok(norm)]
            if len(cands4) == 1:
                return cands4[0]["mark"], cands4[0].get("single"), cands4[0]["name"], "tier-relaxed"

    mark, single, mapping = family_median(bench, key)
    if mark:
        return mark, single, f"median of {mapping and key}", mapping
    return None, None, None, None


# ---------------------------------------------------------------------------
# GPU: dedicated direct, integrated via the host CPU family
# ---------------------------------------------------------------------------

def _resolve_gpu(bench: Benchmarks, wanted: str) -> dict | None:
    return bench._resolve(wanted, bench._gpu_names)


def igpu_for_cpu(cpu_text: str | None) -> str | None:
    """PassMark G3D name for a host CPU's integrated GPU (or None).

    Rules follow silicon generations; each choice is the closest measured
    G3D row, documented here because the stores only write ``Integrated``.
    """
    t = _norm(cpu_text or "")
    if not t:
        return None
    if _brand_of(t) == "APPLE":
        return None  # no Apple G3D rows exist; caller proxies the CPU score
    if "SNAPDRAGON" in t or "QUALCOMM" in t or "ORYON" in t:
        if "X2" in t:
            return "Qualcomm Adreno X2-85 GPU" if "ELITE" in t else "Qualcomm Adreno X2-45 GPU"
        if "ELITE" in t or "PLUS" in t:
            return "Qualcomm Adreno X1-85 GPU"
        if re.search(r"\bX\b", t):
            return "Qualcomm Adreno X1-45 GPU"
        return "Qualcomm Adreno X1-85 GPU"
    if "RYZEN" in t or "ATHLON" in t or "AMD" in t:
        if "AI MAX" in t:
            return "Radeon 890M"  # 8060S has one row (18063) but only a single sample; 890M is the stable pick
        if "AI 9" in t:
            return "Radeon 890M"
        if "AI 7" in t:
            return "Radeon 840M" if "445" in t else "Radeon 860M"
        if "AI 5" in t or re.search(r"\bAI\b", t):
            return "Radeon 840M"
        for sku in ("8940HX", "8945HX", "9955HX"):
            if sku in t:
                return "Radeon 610M"
        if re.search(r"8745H[XS]?|8745H\b|8840H[SX]?", t):
            return "Radeon 780M"
        if re.search(r"7735H[SX]?|7735U|6800H\b", t):
            return "Radeon 680M"
        if "170" in t and "RYZEN 7" in t:
            return "Radeon 680M"
        if re.search(r"7640H[SX]?|7640U|7545U|7540U|8540U", t):
            return "Radeon 760M"
        if re.search(r"7535H[SXU]?|7530U|7533HS|6600H|6600U", t):
            return "Radeon 660M"
        if re.search(r"\b(58\d\d|59\d\d)\w*\b|\b(5[567]00U|7430U)\b", t):
            # Barcelo / Cezanne / Lucienne (Vega 7/8 silicon, e.g. 5825U,
            # 5800H, 5500U): no Vega G3D rows, 660M is the closest equivalent
            return "Radeon 660M"
        if re.search(r"\b(5[34]00U|7320U|7120|7220|7335U)\b", t):
            return "Radeon 610M"
        if re.search(r"7520U|7320U|7120|7220|7335U", t):
            return "Radeon 610M"
        if re.search(r"RYZEN 9|RYZEN 7", t):
            return "Radeon 780M"
        if "RYZEN 5" in t:
            return "Radeon 840M" if re.search(r"2\d\d|1[56]0", t) else "Radeon 660M"
        return "Radeon 610M"
    # Intel
    if re.search(r"2\d\dV\b", t):
        return "Intel Arc 130V GPU" if re.search(r"ULTRA 5", t) else "Intel Arc 140V GPU"
    m3xx = re.search(r"\b(32\d|33\d|34\d|35\d|36\d|38\d)([A-Z]{0,2})\b", t)
    if m3xx and "N355" not in t and "CORE 3" not in t:
        # Panther Lake numbers are exclusive to that generation (Lunar Lake
        # is 2xxV, Arrow/Meteor Lake 1xx/2xx). N355 / Core 3 are Twin Lake.
        if m3xx.group(2) in ("H", "HX") or "X9" in t or "X7" in t:
            return "Intel Arc B390 GPU"
        return "Intel Arc 140T GPU"
    # Raptor Lake-H Refresh ("Core Series 2", 2025, non-Ultra): Core 9 270H /
    # Core 7 250H carry Iris Xe 96EU (Notebookcheck), 240H/220H/210H are the
    # cut-down UHD parts. The old blanket rule sent all of them to Arc 140T
    # (Arrow Lake) and inflated their GPU score by ~20 points.
    if re.search(r"\b(270H|250H)\b", t):
        return "Intel Iris Xe"
    if re.search(r"\b(240H|220H|210H)\b", t):
        return "Intel UHD Graphics"
    # Meteor Lake-H (Ultra 1xxH): Arc Graphics (Xe-LPG) — PassMark has no row
    # for it, so Arc 140V (same 8-core class, one generation newer) is the proxy
    if re.search(r"ULTRA [3579] 1\d{2}H\b|\b(125H|135H|155H|165H|185H)\b", t):
        return "Intel Arc 140V GPU"
    # Arrow Lake-H (Ultra 2xxH): Arc 140T / 130T (Xe2)
    if re.search(r"ULTRA|255H|265H|285H", t):
        return "Intel Arc 140T GPU" if re.search(r"(H|HX|P)\b", t) else "Intel Arc 130T GPU"
    if re.search(r"\bN\d{3}\b|^N1", t) or "N-SERIES" in t:
        return "Intel UHD Graphics"
    if "HX" in t:
        return "Intel UHD Graphics"
    cutdown_h = ("13620H", "13420H", "12450H", "12500H", "12650H")
    if any(s in t for s in cutdown_h):
        return "Intel UHD Graphics"
    if re.search(r"(I[79]|CORE [79]|CORE 9)\b", t):
        return "Intel Iris Xe"
    if re.search(r"(I5|CORE 5)\b", t):
        return "Intel Iris Xe" if re.search(r"U\b", t) else "Intel UHD Graphics"
    if re.search(r"2\d\dH\b", t):
        return "Intel Arc 140T GPU"  # Arrow Lake-H refresh (Core 200H)
    return "Intel UHD Graphics"


def igpu_name_for_cpu(cpu_text: str | None) -> str | None:
    """Marketing name of the CPU's integrated GPU, for display only.

    Scoring still goes through ``igpu_for_cpu`` (the closest *measured* PassMark
    row).  This is the honest label for the compare table: a Meteor Lake-H 135H
    is "Intel Arc Graphics" even though its score uses the Arc 140V proxy.
    """
    t = _norm(cpu_text or "")
    if not t:
        return None
    if _brand_of(t) == "APPLE":
        return "Apple GPU (M-series)"
    if "SNAPDRAGON" in t or "QUALCOMM" in t or "ORYON" in t:
        return igpu_for_cpu(cpu_text)              # Adreno names are the real ones
    if "RYZEN" in t or "ATHLON" in t or "AMD" in t:
        name = igpu_for_cpu(cpu_text)
        if name == "Radeon 660M" and re.search(r"\b(5[5-9]\d\d|7430U)\b", t):
            return "Radeon Vega (660M-class)"      # Vega has no G3D row; 660M is a proxy
        return name                                # Radeon 780M/760M/840M/860M are real
    # Intel
    if re.search(r"\bN\d{3}\b|^N1|N-SERIES", t):
        return "Intel UHD Graphics"
    if re.search(r"\b(120U|150U|270H|250H)\b", t):
        return "Intel Iris Xe"                     # Raptor Lake refresh
    if re.search(r"\b(240H|220H|210H)\b", t):
        return "Intel UHD Graphics"
    if re.search(r"ULTRA [3579] 1\d{2}H\b|\b(125H|135H|155H|165H|185H)\b", t):
        return "Intel Arc Graphics"                # Meteor Lake-H (Xe-LPG)
    if re.search(r"ULTRA [3579] 1\d{2}U\b|\b(125U|135U|155U|165U)\b", t):
        return "Intel Graphics"                    # Meteor Lake-U (4 Xe cores)
    if "ULTRA" in t and re.search(r"\b2\d\dH\b", t):
        return "Intel Arc Graphics"                # Arrow Lake-H (Xe-LPG+)
    if "ULTRA" in t and re.search(r"\b2\d\dHX\b", t):
        return "Intel Graphics"                    # Arrow Lake-HX (small iGPU)
    if "ULTRA" in t and re.search(r"\b2\d\dU\b", t):
        return "Intel Graphics"                    # Arrow Lake-U
    if re.search(r"\b2\d\dV\b", t) or re.search(r"ULTRA [3579] 3\d\d", t):
        return igpu_for_cpu(cpu_text)              # Lunar Lake / Panther Lake: real Arc names
    return igpu_for_cpu(cpu_text)


def lookup_apple_gpu(cpu_text: str | None, gpu: dict) -> tuple[int | None, str | None, str | None]:
    """Return (mark, passmark_name, mapping) for Apple Silicon GPUs based on architecture and core count."""
    c_low = (cpu_text or "").lower()
    g_low = ((gpu.get("model") or "") + " " + (gpu.get("text") or "")).lower()
    m = re.search(r"(\d+)\s*[- ]?core", g_low)
    cores = int(m.group(1)) if m else None

    if "a18" in c_low:
        gen, rate = "A18", 700
        cores = cores or 5
    elif "m5" in c_low:
        gen, rate = "M5", 980
        cores = cores or (32 if "max" in c_low else (16 if "pro" in c_low else 10))
    elif "m4" in c_low:
        gen, rate = "M4", 900
        cores = cores or (32 if "max" in c_low else (16 if "pro" in c_low else 10))
    elif "m3" in c_low:
        gen, rate = "M3", 800
        cores = cores or (30 if "max" in c_low else (14 if "pro" in c_low else 10))
    elif "m2" in c_low:
        gen, rate = "M2", 700
        cores = cores or (30 if "max" in c_low else (16 if "pro" in c_low else 10))
    else:  # M1 or generic
        gen, rate = "M1", 620
        cores = cores or (24 if "max" in c_low else (14 if "pro" in c_low else 8))

    eff_cores = cores if cores <= 16 else (16 + (cores - 16) * 0.82)
    mark = int(round(eff_cores * rate))
    name = f"Apple {gen} {cores}-core GPU"
    return mark, name, "apple-core-model"


def lookup_gpu(
    bench: Benchmarks,
    gpu: dict,
    cpu_text: str | None,
) -> tuple[int | None, str | None, str | None]:
    """Return (mark, passmark_name, mapping) for a parsed GPU + host CPU."""
    model = (gpu.get("model") or "").upper().strip()
    text = (gpu.get("text") or "").strip()

    # hand overrides first: {"gpu": {"<cpu> || <gpu model>": passmark_name}}
    if text or model:
        want = (bench.overrides.get("gpu") or {}).get(f"{cpu_text} || {model or text}")
        if want:
            hit = _resolve_gpu(bench, want)
            if hit:
                return hit["mark"], hit["name"], "override"

    # Apple Silicon GPU core-based mapping
    if _brand_of(_norm(cpu_text or "")) == "APPLE" or re.search(r"\d+-CORE GPU", model):
        return lookup_apple_gpu(cpu_text, gpu)

    target_str = f"{model} {text}".upper()
    m = re.search(r"(?:GEFORCE\s+)?RTX\s?([A-Z]?)(\d{3,4})\s?(TI|SUPER)?", target_str)
    if m:
        prefix, num, variant = m.group(1), m.group(2), (m.group(3) or "").upper()
        base = f"RTX {num}" + (f" {variant.title()}" if variant else "")
        if num == "3050" and not variant and not prefix:
            vram = gpu.get("vram")
            if vram == 6:
                hit = _resolve_gpu(bench, "GeForce RTX 3050 6GB Laptop GPU")
            elif vram == 4:
                hit = _resolve_gpu(bench, "GeForce RTX 3050 4GB Laptop GPU")
            else:
                hit = _resolve_gpu(bench, "GeForce RTX 3050 Laptop GPU")
            if hit:
                return hit["mark"], hit["name"], "direct-laptop"
        candidates = [
            f"GeForce {base} Laptop GPU",
            f"GeForce {base}",
            f"RTX {num} Ada Generation Laptop GPU",
            f"RTX {num} Ada Generation",
            f"RTX A{num} Laptop GPU",
            f"RTX A{num}",
            f"Quadro RTX {num} (Mobile)",
            f"Quadro RTX {num}",
        ]
        if prefix:
            candidates.insert(0, f"RTX {prefix}{num} Laptop GPU")
            candidates.insert(1, f"RTX {prefix}{num}")
        for want_name in candidates:
            hit = _resolve_gpu(bench, want_name)
            if hit:
                mapping = "direct-laptop" if ("Laptop" in hit["name"] or "Mobile" in hit["name"]) else "direct"
                return hit["mark"], hit["name"], mapping
    m = re.search(r"GTX\s?(\d{3,4})", model)
    if m:
        hit = _resolve_gpu(bench, f"GeForce GTX {m.group(1)}")
        if hit:
            return hit["mark"], hit["name"], "direct"
    m = re.search(r"RADEON\s+(\d{3,4})M\b", model)
    if m:
        num = m.group(1)
        if num == "680" and cpu_text and "170" in cpu_text:
            hit = _resolve_gpu(bench, "Radeon 680M Ryzen 7 170")
            if hit:
                return hit["mark"], hit["name"], "direct-per-sku"
        if num == "660" and cpu_text:
            sku = _trailing_token(_norm(cpu_text)) or ""
            if sku:
                hit = _resolve_gpu(bench, f"Radeon 660M Ryzen 5 {sku}")
                if hit:
                    return hit["mark"], hit["name"], "direct-per-sku"
        hit = _resolve_gpu(bench, f"Radeon {num}M")
        if hit:
            return hit["mark"], hit["name"], "direct"
    if "VEGA" in model or "VEGA" in (gpu.get("text") or "").upper():
        # Barcelo / Cezanne / Lucienne iGPUs: no Vega rows exist in G3D;
        # 660M (RDNA2 6CU) is the closest measured equivalent
        hit = _resolve_gpu(bench, "Radeon 660M")
        if hit:
            return hit["mark"], hit["name"], "vega-proxy"
    if "ARC" in model:
        for key in ("140V", "140T", "130V", "130T", "390"):
            if key in model:
                hit = _resolve_gpu(bench, f"Intel Arc {key} GPU") or _resolve_gpu(
                    bench, f"Intel Arc {key}"
                )
                if hit:
                    return hit["mark"], hit["name"], "direct"
    if "IRIS" in model:
        hit = _resolve_gpu(bench, "Intel Iris Xe")
        if hit:
            return hit["mark"], hit["name"], "direct"
    if "UHD" in model:
        hit = _resolve_gpu(bench, "Intel UHD Graphics")
        if hit:
            return hit["mark"], hit["name"], "direct"

    # generic / integrated: resolve through the host CPU family
    want_igpu = igpu_for_cpu(cpu_text)
    if want_igpu:
        hit = _resolve_gpu(bench, want_igpu)
        if hit:
            return hit["mark"], hit["name"], "cpu-family"
    return None, None, None


# ---------------------------------------------------------------------------
# 0-100 scaling (keeps the perf/value bars and sanity model on one scale)
# ---------------------------------------------------------------------------

def _log_scale(mark: int | None, lo: int, hi: int) -> float | None:
    if not mark or mark <= 0:
        return None
    lo_l, hi_l = math.log(lo), math.log(hi)
    v = (math.log(max(mark, lo)) - lo_l) / (hi_l - lo_l)
    return round(min(max(v, 0.0), 1.0) * 100, 1)


def scale_cpu(mark: int | None) -> float | None:
    return _log_scale(mark, CPU_MIN, CPU_MAX)


def scale_cpu_single(mark: int | None) -> float | None:
    return _log_scale(mark, CPU_SINGLE_MIN, CPU_SINGLE_MAX)


def scale_gpu(mark: int | None) -> float | None:
    return _log_scale(mark, GPU_MIN, GPU_MAX)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download chart pages and rewrite data/benchmarks/*.json")
    parser.add_argument("--max-age", type=float, default=48, metavar="HOURS",
                        help="re-fetch only pages older than this (default: 48, 0 = always)")
    parser.add_argument("--check", action="store_true", help="match every dataset CPU/GPU and report coverage")
    args = parser.parse_args()

    if args.fetch:
        fetch_snapshots(None if args.max_age == 0 else args.max_age)
        return 0

    bench = Benchmarks()
    print(f"cpu entries: {len(bench.cpu)}  gpu entries: {len(bench.gpu)}  fetched_at: {bench.fetched_at}")
    if not bench.cpu or not bench.gpu:
        print("snapshots missing — run: python3 scraper/benchmarks.py --fetch")
        return 1

    if args.check:
        from collections import Counter
        data_path = ROOT / "data" / "unified_laptops.json"
        try:
            records = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"cannot read {data_path}: {exc}")
            return 1
        cpu_map: Counter = Counter()
        gpu_map: Counter = Counter()
        missing_cpu: Counter = Counter()
        missing_gpu: Counter = Counter()
        no_single = 0
        for rec in records:
            cpu = rec.get("cpu") or {}
            gpu = rec.get("gpu") or {}
            mark, single, _name, mapping = lookup_cpu(bench, cpu.get("text"))
            cpu_map[mapping or "MISSING"] += 1
            if not mark:
                missing_cpu[cpu.get("text") or "NONE"] += 1
            elif not single:
                no_single += 1
            gmark, _gname, gmapping = lookup_gpu(bench, gpu, cpu.get("text"))
            if gmapping == "apple-cpu-proxy":
                gmark = mark
            gpu_map[gmapping or "MISSING"] += 1
            if not gmark:
                missing_gpu[f"{cpu.get('text')} || {gpu.get('model') or gpu.get('text')}"] += 1
        print("cpu mappings:", dict(cpu_map))
        print(f"cpu matched but no Single Thread mark: {no_single}")
        print("gpu mappings:", dict(gpu_map))
        print(f"-- unmatched cpus ({len(missing_cpu)}):")
        for k, n in missing_cpu.most_common(25):
            print(f"   {n:3d} x {k}")
        print(f"-- unmatched gpus ({len(missing_gpu)}):")
        for k, n in missing_gpu.most_common(25):
            print(f"   {n:3d} x {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
