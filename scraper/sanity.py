"""Price sanity check over all stores.

A k-nearest-neighbour price model in (performance score, storage, RAM, build quality) space finds the
25 most comparable machines for every listing and takes their median price as the
expectation for that configuration:

* below 35% of it  -> ``price_flag = "too cheap"`` (something is off: wrong specs in
  the title, a grey-market listing, or a genuine outlier)
* above 2.2x of it -> ``price_flag = "above market"``
* value >= 1.5x the comparable median -> a **deal outlier** (``deal_ratio`` +
  "Deal" tag): far better perf-per-riyal than machines of the same class.  Only
  in-stock, non-flagged listings qualify — it is an alert, not a verdict.

The viewer shows the expectation next to the price ("typical SAR 7,564") and can hide
the unusually cheap ones.  No network involved.
"""

from __future__ import annotations

import math
import statistics

K_NEIGHBOURS = 25
MIN_EXPECTED = 1500          # don't judge machines this cheap — the spread is huge
CHEAP_RATIO = 0.35           # below 35% of comparable price => "too cheap" (anomalies, errors)
DEAR_RATIO = 2.2             # more than 2.2x the comparable price => "above market"
DEAL_RATIO = 1.5             # value >= 1.5x the comparable median => deal outlier


def _feature(rec: dict) -> tuple[float, float, float, float]:
    perf = (rec.get("perf_score") or 0) / 4.0
    storage = math.log2(max(rec.get("storage") or 256, 128))
    ram = (rec.get("ram") or 8) / 6.0
    build = (rec.get("build_score") or 5.5) / 1.5
    return perf, storage, ram, build


def _distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    # performance dominates, then build class, storage, and RAM
    return (a[0] - b[0]) ** 2 * 2.0 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2 * 1.5


def flag_prices(records: list[dict], k: int = K_NEIGHBOURS) -> dict:
    """Attach price_expected / price_flag / deal_ratio to every record (in place)."""
    priced = [r for r in records if r.get("price")]
    features = {id(r): _feature(r) for r in priced}
    flagged = {"too cheap": 0, "above market": 0, "deals": 0}

    for rec in priced:
        mine = features[id(rec)]
        peers = sorted((r for r in priced if r is not rec),
                       key=lambda r: _distance(mine, features[id(r)]))[:k]
        comparable = [p["price"] for p in peers if p.get("price")]
        if len(comparable) < 8:
            continue
        expected = statistics.median(comparable)
        rec["price_expected"] = int(round(expected))
        # deal outlier: much better perf-per-riyal than comparable machines.
        # Only clean, buyable listings — an out-of-stock price is not a deal
        # you can act on.
        peer_values = [p["value_score"] for p in peers if p.get("value_score")]
        if (rec.get("value_score") and len(peer_values) >= 8
                and rec.get("in_stock") and not rec.get("delisted")):
            peer_median = statistics.median(peer_values)
            if peer_median and rec["value_score"] >= peer_median * DEAL_RATIO:
                rec["deal_ratio"] = round(rec["value_score"] / peer_median, 2)
                rec.setdefault("tags", []).append("Deal")
                flagged["deals"] += 1
        if expected < MIN_EXPECTED:
            continue
        if rec["price"] < expected * CHEAP_RATIO:
            rec["price_flag"] = "too cheap"
            flagged["too cheap"] += 1
        elif rec["price"] > expected * DEAR_RATIO:
            rec["price_flag"] = "above market"
            flagged["above market"] += 1
    return flagged
