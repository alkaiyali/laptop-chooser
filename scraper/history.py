#!/usr/bin/env python3
"""Price/stock history for the laptop chooser.

Every pipeline run appends one observation per SKU per day to
``data/price_history.jsonl`` (a later run on the same day replaces that day's
row, so re-running never creates duplicates).

``data/price_history_seed.jsonl`` can hold older observations — e.g. prices
captured by the "Pricing Ladder" research — and is merged in automatically.

normalize.py calls :func:`append_snapshot` and :func:`load_stats`, then embeds
``hist`` (low / low_date / high / high_date / observed / observations / points)
into every record. The full series stays in ``data/price_history.jsonl``;
``scraper/moat.py`` summarises and downsamples it for the shipped payload.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from common import DATA_DIR

HISTORY = DATA_DIR / "price_history.jsonl"
SEED = DATA_DIR / "price_history_seed.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def append_snapshot(records: list[dict], day: str | None = None) -> None:
    """Record today's price/stock for every SKU, keeping one row per day."""
    day = day or date.today().isoformat()
    merged: dict[str, dict] = {}

    def key(row: dict) -> str:
        return f"{row.get('sku')}|{row.get('date')}"

    # oldest first so the current snapshot overwrites same-day seed rows
    for row in _read_jsonl(SEED):
        if row.get("sku") and row.get("price") is not None:
            merged[key(row)] = row
    for row in _read_jsonl(HISTORY):
        if row.get("sku") and row.get("price") is not None:
            merged[key(row)] = row
    for rec in records:
        price = rec.get("price")
        if not rec.get("sku") or price is None:
            continue
        row = {
            "date": day,
            "sku": rec["sku"],
            "store": rec.get("store"),
            "price": round(float(price), 2),
            "in_stock": bool(rec.get("in_stock")) and not rec.get("delisted"),
            "name": (rec.get("name") or "")[:120],
        }
        merged[key(row)] = row

    ordered = sorted(merged.values(), key=lambda r: (r["date"], str(r["sku"])))
    HISTORY.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered), encoding="utf-8"
    )


def load_stats() -> dict[str, dict]:
    """Per-SKU series + historical low/high from everything recorded so far."""
    series: dict[str, list[dict]] = defaultdict(list)
    for row in _read_jsonl(HISTORY):
        if row.get("sku") and row.get("price") is not None:
            series[str(row["sku"])].append(row)

    stats: dict[str, dict] = {}
    # Trailing-365-day window ends at the latest observation anywhere, so every
    # SKU's "best in the last year" is measured over the same window. Uses the
    # full-resolution series (every recorded datapoint), not the downsampled
    # trend that ships to the viewer.
    end = max((r["date"] for rows in series.values() for r in rows), default=None)
    start = _shift_years(end) if end else None
    for sku, rows in series.items():
        rows.sort(key=lambda r: r["date"])
        priced = [r for r in rows if r.get("price") is not None]
        if not priced:
            continue
        low_row = min(priced, key=lambda r: r["price"])
        high_row = max(priced, key=lambda r: r["price"])
        in_window = [r for r in priced if r["date"] >= start]
        best_row = min(in_window, key=lambda r: r["price"]) if in_window else None
        stats[sku] = {
            "points": [[r["date"], r["price"], bool(r.get("in_stock"))] for r in priced],
            "low": low_row["price"],
            "low_date": low_row["date"],
            "high": high_row["price"],
            "high_date": high_row["date"],
            "observed": priced[-1]["date"],
            "observations": len(priced),
            "best365": best_row["price"] if best_row else None,
            "best365_date": best_row["date"] if best_row else None,
            "window_start": start,
        }
    return stats


def _shift_years(day: str) -> str:
    """Same calendar date one year earlier (clamped, e.g. Feb 29 -> Feb 28)."""
    from datetime import date as _date

    d = _date.fromisoformat(day)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return d.replace(year=d.year - 1, day=28).isoformat()
