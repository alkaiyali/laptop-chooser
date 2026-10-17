#!/usr/bin/env python3
"""Phone camera-sensor lookup for the phone chooser (curated table).

Sensor model names show up in a minority of listings ("Sensor: Sony IMX766",
"50MP Sony IMX882 OIS", "Sony LYTIA 700C"), but when present the model pins
down the physical sensor size — the single most meaningful hardware fact
about a phone camera after resolution. Sizes live in
``data/benchmarks/phone_camera_sensors.json`` (optical format, inch).

Matching (first hit wins): exact name -> unique substring -> directly
stated optical format (``1/1.56"`` in the prose). Several matches resolve
to the largest: the main sensor dwarfs front/ultrawide companions, so the
max is the main. Returns (size_in, table name, mapping) with mapping None
when nothing matched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = ROOT / "data" / "benchmarks" / "phone_camera_sensors.json"

_table: dict | None = None


def _load() -> dict:
    global _table
    if _table is None:
        try:
            _table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _table = {"entries": []}
    return _table


def entries() -> list[dict]:
    return _load().get("entries", [])


def _norm(text: str) -> str:
    text = (text or "").upper()
    text = text.replace("LYTIA", "LYT")  # "Sony LYTIA 700C" -> LYT 700C
    text = re.sub(r"[^A-Z0-9]", "", text)  # "LYT-700C" -> "LYT700C"
    return text


def _keys(entry: dict) -> list[str]:
    names = [entry["name"]] + list(entry.get("aka") or [])
    return [_norm(n) for n in names]


def lookup(text: str | None) -> tuple[float | None, str | None, str | None]:
    """Return (sensor size in inch, table name, mapping) for free text."""
    rows = entries()
    if not text or not text.strip() or not rows:
        return None, None, None
    key = _norm(text)

    for entry in rows:  # exact model token
        if any(k and k == key for k in _keys(entry)):
            return entry["size_in"], entry["name"], "exact"
    cands = []
    for entry in rows:  # unique substring ("IMX882" in "...SONYIMX882OIS...")
        if any(k and k in key for k in _keys(entry)):
            cands.append(entry)
    if len(cands) == 1:
        return cands[0]["size_in"], cands[0]["name"], "substring"
    if len(cands) > 1:  # several named (main + front): the main is biggest
        best = max(cands, key=lambda e: e["size_in"])
        return best["size_in"], best["name"], "substring-max"

    m = re.search(r"1/(\d(?:\.\d+)?)\s*[\"″”]", text)  # stated "1/1.56""
    if m:
        try:
            return round(1 / float(m.group(1)), 3), "stated-size", "size-stated"
        except (ValueError, ZeroDivisionError):
            pass
    return None, None, None


def sensor_points(size_in: float | None) -> float:
    """Size bonus 0-6 for the camera rating; unknown scores neutral 2."""
    if size_in is None:
        return 2.0
    if size_in >= 0.95:
        return 6.0
    if size_in >= 0.75:
        return 5.0
    if size_in >= 0.65:
        return 4.0
    if size_in >= 0.55:
        return 3.0
    if size_in >= 0.45:
        return 2.0
    return 1.0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sensor", nargs="?", help='sensor string, e.g. "Sony LYTIA 700C"')
    args = parser.parse_args()
    print(f"table: {len(entries())} entries ({TABLE_PATH})")
    for demo in [args.sensor] if args.sensor else [
        "Sensor: Sony IMX766 / Sony IMX615",
        "50MP Sony IMX882 OIS + 8MP ultra-wide",
        "Sony LYTIA 700C",
        "200MP Samsung HP2 with OIS",
        "1/1.56\" Super Retina camera",
        "Helio G88 processor",
    ]:
        size, name, mapping = lookup(demo)
        print(f"  {demo!r} -> {size} ({name}, {mapping}) pts={sensor_points(size)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
