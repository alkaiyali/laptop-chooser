#!/usr/bin/env python3
"""Moat-safe public payload for the chooser.

`data/unified_laptops.json` is the internal source of truth: it carries the
*derivation* of the badges — source URLs, cited evidence, the comparable-peer
median, the scoring-detail counters — plus the full per-day price series. That
accumulated work is what a competitor cannot re-scrape, so it stays on the host.

`normalize.py` calls :func:`sanitize_records` / :func:`sanitize_meta` before
writing `viewer/laptops.js`. The shipped payload keeps everything the UI needs
to render (prices, specs, scores, badges, filters, tooltips) and drops the
evidence and the raw series:

  dropped      battery_source_url, battery_estimate, ram_detail,
               short_description, dup_count/dup_max, unused scoring counters,
               benchmark `mapping` provenance
  compressed   hist.points -> low/high/dates + first/last + 24-point
               downsampled trend; `any_in_stock` precomputed, so the raw
               day-by-day series never leaves the host
  exact        best365/best365_date: trailing-365d minimum over EVERY recorded
               datapoint, computed at build time (the shipped trend is
               downsampled, so the exact answer ships as two scalars)

Kept deliberately (rendered conclusions, and the methodology is already public
in README/Field Notes): price_expected, the per-component score breakdowns,
ram_source tier labels, seller warning fields.

Raw data stays in `data/`; only `viewer/` is ever served.
"""
from __future__ import annotations

import copy

# Present in the internal record, never rendered by the viewer, and/or part of
# the irreproducible curation (sources, cited evidence, pooling method).
DROP = frozenset({
    "battery_source_url",   # manufacturer spec-sheet links — the citation set
    "battery_estimate",     # "mode of 11 similar matebook 14 listings" — pooling method
    "ram_detail",           # teardown/spec citations behind the RAM verdict
    "short_description",    # raw store copy, unused by the UI
    "dup_count", "dup_max", # dedupe internals
    # scoring internals that no view renders (the totals ship instead)
    "battery_points", "build_points", "display_points", "screen_points",
    "storage_points", "gpu_idle_points", "non_perf_points", "quality_points",
    # store fields never referenced after normalization
    "rating", "review_count", "seller_rating", "charging_type", "build_class",
})

# cpu.benchmark / gpu.benchmark: keep only what tooltips and the WS/game
# fallback actually read. `mapping` says how the chip was resolved and `source`
# is used by benchTip; keep source, drop mapping.
BENCH_DROP = frozenset({"mapping"})

MAX_POINTS = 24


def _compress_hist(hist: dict | None) -> dict | None:
    """Full day-by-day series -> summary + downsampled trend for the sparkline."""
    if not isinstance(hist, dict):
        return None
    out = {k: hist.get(k) for k in
           ("low", "low_date", "high", "high_date", "observed", "observations")}
    pts = hist.get("points") or []
    out["any_in_stock"] = any(len(p) > 2 and p[2] for p in pts)
    if not pts:
        out["points"] = []
        return out
    if len(pts) <= MAX_POINTS:
        sampled = pts
    else:
        step = (len(pts) - 1) / (MAX_POINTS - 1)
        idx = sorted({round(i * step) for i in range(MAX_POINTS)})
        sampled = [pts[i] for i in idx]
    # keep [date, price] — the sparkline's only inputs; stock is summarised above
    out["points"] = [[p[0], p[1]] for p in sampled]
    return out


def _clean_part(part) -> dict | None:
    if not isinstance(part, dict):
        return part
    part = copy.deepcopy(part)
    bm = part.get("benchmark")
    if isinstance(bm, dict):
        bm = {k: v for k, v in bm.items() if k not in BENCH_DROP}
        part["benchmark"] = bm or None
    return part


def sanitize_records(records: list[dict]) -> list[dict]:
    """Deep-copied records: derivation stripped, price series summarised."""
    out = []
    for rec in records:
        r = {k: copy.deepcopy(v) for k, v in rec.items() if k not in DROP}
        r["hist"] = _compress_hist(rec.get("hist"))
        for key in ("cpu", "gpu"):
            if key in r:
                r[key] = _clean_part(r[key])
        out.append(r)
    return out


META_KEEP = ("generated_at", "count", "sources", "php_per_sar")


def sanitize_meta(meta: dict) -> dict:
    """Keep the freshness/count header; drop the pipeline statistics block."""
    return {k: meta[k] for k in META_KEEP if k in meta}
