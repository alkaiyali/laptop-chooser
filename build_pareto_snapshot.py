#!/usr/bin/env python3
"""Build a snapshot-vs-snapshot pareto chart (Oct-06 vs fresh).

Reads:
  data/unified_laptops.2026-10-06.json  (previous snapshot, backed up before refresh)
  data/unified_laptops.json             (fresh snapshot after Jarir+eXtra re-scrape)
  reports/pareto_before_after.html     (template — structure/tabs/charts reused)

Writes a NEW file (never overwrites the existing charts):
  reports/pareto_2026-10-06_vs_<NEWDATE>.html

Each point: was = previous-snapshot price, price = fresh price.
Tabs: Jarir / eXtra / All (Jarir+eXtra) / Shifts (price changed).
Includes merged duplicates as separate per-SKU points, like the previous chart.
"""
from __future__ import annotations
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OLD = ROOT / "data" / "unified_laptops.2026-10-06.json"
NEW = ROOT / "data" / "unified_laptops.json"
TEMPLATE = ROOT / "reports" / "pareto_before_after.html"

STORES = ("Jarir", "eXtra")


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def point(new: dict, old_price: float) -> dict | None:
    price = new.get("price")
    if not price or not old_price:
        return None
    try:
        price = float(price)
        was = float(old_price)
    except (TypeError, ValueError):
        return None
    if price <= 0 or was <= 0:
        return None
    cpu = new.get("cpu") or {}
    return {
        "id": new.get("id"),
        "store": new.get("store"),
        "name": new.get("short_name") or new.get("name"),
        "price": round(price, 2),
        "was": round(was, 2),
        "discount": round((was - price) / was * 100, 1) if was else 0.0,
        "overall": new.get("overall_score"),
        "perf": new.get("perf_score"),
        "value": new.get("value_score"),
        "cpu": cpu.get("text") if isinstance(cpu, dict) else cpu,
        "ram": str(new.get("ram")) if new.get("ram") is not None else "?",
        "stor": str(new.get("storage")) if new.get("storage") is not None else "?",
        "url": new.get("url"),
    }


def main() -> int:
    old_list = load(OLD)
    new_list = load(NEW)
    old_by_id = {r.get("id"): r for r in old_list}
    new_by_id = {r.get("id"): r for r in new_list}

    print(f"old snapshot: {len(old_list)} records; new snapshot: {len(new_list)} records")

    groups: dict[str, list] = {"jarir": [], "extra": []}
    for r in new_list:
        store = r.get("store")
        if store not in STORES:
            continue
        if not r.get("in_stock"):
            continue
        if r.get("delisted") or r.get("deleted"):
            continue
        if not r.get("price"):
            continue
        o = old_by_id.get(r.get("id"))
        if not o or not o.get("price"):
            continue  # new SKU or removed SKU — not comparable snapshot-vs-snapshot
        pt = point(r, o.get("price"))
        if not pt:
            continue
        key = "jarir" if store == "Jarir" else "extra"
        # flag stock changes for the tooltip/table context
        if not o.get("in_stock"):
            pt["back_in_stock"] = True
        groups[key].append(pt)

    all_pts = groups["jarir"] + groups["extra"]
    shifts = [p for p in all_pts if abs(p["price"] - p["was"]) > 0.5]
    # stock-status changes worth flagging even without price move
    new_skus = [r for r in new_list if r.get("store") in STORES
                and r.get("in_stock") and not r.get("delisted") and not r.get("deleted")
                and r.get("price") and r.get("id") not in old_by_id]
    gone_skus = [r for r in old_list if r.get("store") in STORES
                 and r.get("id") not in new_by_id]
    print(f"jarir comparable: {len(groups['jarir'])}, extra comparable: {len(groups['extra'])}, "
          f"shifts: {len(shifts)}, new SKUs: {len(new_skus)}, gone SKUs: {len(gone_skus)}")

    store_data = {
        "jarir": sorted(groups["jarir"], key=lambda r: r["price"]),
        "extra": sorted(groups["extra"], key=lambda r: r["price"]),
        "all": sorted(all_pts, key=lambda r: r["price"]),
        "shifts": sorted(shifts, key=lambda r: r["price"]),
    }

    tpl = TEMPLATE.read_text(encoding="utf-8")
    today = date.today().isoformat()  # fresh-snapshot date
    new_label = f"pareto_2026-10-06_vs_{today}.html"
    out = ROOT / "reports" / new_label

    # --- patch template text ---
    # title
    tpl = tpl.replace(
        "<title>Laptop Pareto Overlaid — Before vs After</title>",
        "<title>Laptop Pareto — Snapshot vs Snapshot (Oct-06 vs Fresh)</title>",
    )
    # header: replace the h1 line + subtitle paragraph
    tpl = re.sub(
        r"<h1>.*?</h1>",
        f"<h1>Laptop Market Pareto — Snapshot vs Snapshot <span class=\"badge-tag\">2026-10-06 vs {today}</span></h1>",
        tpl, count=1, flags=re.S,
    )
    tpl = re.sub(
        r"<p class=\"sub\">.*?</p>",
        "<p class=\"sub\">X = price (log scale), Y = overall_score. Grey hollow = <b>2026-10-06 snapshot price</b>, "
        f"blue = <b>fresh price ({today}, Jarir + eXtra re-scraped)</b>, thin line = price move "
        "(leftward = price drop, rightward = hike). Dashed grey = Oct-06 frontier, solid orange = fresh frontier. "
        "Per-SKU points (merged duplicates shown separately, like the previous chart).</p>",
        tpl, count=1, flags=re.S,
    )
    # legend: was_price -> snapshot wording
    tpl = tpl.replace("was_price (before)", "Oct-06 price (before)")
    tpl = tpl.replace("current price (after)", f"fresh price ({today})")
    # tabs: rebuild with live counts (Jarir / eXtra / All / Shifts only — noon not re-scraped)
    tabs_html = (
        f'<button class="tab active" data-store="jarir">Jarir ({len(store_data["jarir"])})</button>\n'
        f'  <button class="tab" data-store="extra">eXtra ({len(store_data["extra"])})</button>\n'
        f'  <button class="tab" data-store="all">Jarir+eXtra ({len(store_data["all"])})</button>\n'
        f'  <button class="tab" data-store="shifts">Snapshot Shifts ({len(store_data["shifts"])})</button>'
    )
    tpl = re.sub(r'<div class="tabs">.*?</div>', f'<div class="tabs">\n  {tabs_html}\n</div>',
                 tpl, count=1, flags=re.S)
    # data payload
    payload = json.dumps(store_data, ensure_ascii=False)
    tpl = re.sub(r'const STORE_DATA = \{.*?\};\nlet', f'const STORE_DATA = {payload};\nlet',
                 tpl, count=1, flags=re.S)
    # meta line wording: before/after -> snapshot dates
    tpl = tpl.replace(
        "Before frontier: ${setB.size} pts (dashed) · After frontier: ${setA.size} pts (orange)",
        f"Oct-06 frontier: ${{setB.size}} pts (dashed) · Fresh ({today}) frontier: ${{setA.size}} pts (orange)",
    )

    out.write_text(tpl, encoding="utf-8")
    print(f"wrote {out.name} ({out.stat().st_size/1024:.0f} KB)")
    print(f"left untouched: reports/jarir_pareto.html, reports/pareto_before_after.html")
    if new_skus:
        print("new SKUs (in fresh, not in Oct-06; not plotted):")
        for r in sorted(new_skus, key=lambda r: r.get("price") or 0)[:15]:
            print(f"  + {r.get('id')} {r.get('short_name')} {r.get('price')}")
    if gone_skus:
        print("gone SKUs (in Oct-06, not in fresh; not plotted):")
        for r in sorted(gone_skus, key=lambda r: (r.get('id') or ''))[:15]:
            print(f"  - {r.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
