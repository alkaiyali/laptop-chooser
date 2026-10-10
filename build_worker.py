#!/usr/bin/env python3
"""Build a single self-contained chooser HTML for a Cloudflare Worker.

Reads the current viewer payload (viewer/laptops.js, written by normalize.py),
trims the fields the viewer never touches, inlines the data and the store logos
into viewer/index.html (and the previous UI, viewer/classic.html), and writes:

    deploy/public/index.html    one file — data, logos and code inside
    deploy/public/classic.html  the previous UI, same treatment
    deploy/public/robots.txt
    deploy/wrangler.jsonc       assets-only Worker config
    deploy/README.md            deploy instructions

Run after the scraper/normalizer refresh:

    python3 build_worker.py
    cd deploy && npx wrangler deploy

No server, no build step at runtime: deploy/public/index.html also opens
straight from disk (file://) for local checking.
"""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
OUT = ROOT / "deploy"
PUBLIC = OUT / "public"

MAX_HIST_POINTS = 12   # sparkline is 116px wide; 12 points is plenty (first/last kept)

# Fields present in the payload but never read by viewer/index.html
DROP_FIELDS = ("battery", "battery_mah", "ram_soldered")
DROP_NESTED = (
    ("cpu", "cores"), ("cpu", "gen"),
    ("gpu", "inferred"), ("gpu", "vram_inferred"),
    ("hist", "high"), ("hist", "high_date"),
)


def grab(js: str, name: str):
    """Extract `window.<name> = <json>;` from the payload file."""
    m = re.search(rf"window\.{name}\s*=\s*(.*?);\n", js, re.S)
    if not m:
        raise SystemExit(f"window.{name} not found in viewer/laptops.js")
    return json.loads(m.group(1))


def downsample(points: list, n: int) -> list:
    if len(points) <= n:
        return points
    idx = sorted({round(i * (len(points) - 1) / (n - 1)) for i in range(n)})
    return [points[i] for i in idx]


def trim(rec: dict) -> dict:
    for k in DROP_FIELDS:
        rec.pop(k, None)
    for parent, k in DROP_NESTED:
        d = rec.get(parent)
        if isinstance(d, dict):
            d.pop(k, None)
    offers = rec.get("offers")
    if isinstance(offers, list):          # per-offer copy the viewer never reads
        for o in offers:
            if isinstance(o, dict):
                o.pop("variants", None)
    # `specs` is only used as a search haystack (Object.values(...).join(" ")):
    # values-only keeps that identical and drops all the long label keys.
    specs = rec.get("specs")
    if isinstance(specs, dict):
        seen, vals = set(), []
        for v in specs.values():
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s)
                vals.append(s)
        rec["specs"] = vals
    h = rec.get("hist")
    if isinstance(h, dict) and isinstance(h.get("points"), list):
        h["points"] = downsample(h["points"], MAX_HIST_POINTS)
    return rec


def data_uri(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix == ".svg":
        return "data:image/svg+xml;base64," + base64.b64encode(raw).decode()
    try:  # logos are flat art: lossless WebP usually beats PNG, browsers all take it
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        buf = io.BytesIO()
        im.save(buf, "WEBP", lossless=True, quality=100, method=6)
        if buf.tell() < len(raw):
            return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def main() -> None:
    payload_js = (VIEWER / "laptops.js").read_text()

    meta = grab(payload_js, "LAPTOPS_META")
    shortlist = grab(payload_js, "SHORTLIST")
    affiliate = grab(payload_js, "AFFILIATE")
    laptops = grab(payload_js, "LAPTOPS")

    raw_before = len(payload_js)
    laptops = [trim(r) for r in laptops]

    js = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    data_block = "\n".join((
        "window.LAPTOPS_META = " + js(meta) + ";",
        "window.SHORTLIST = " + js(shortlist) + ";",
        "window.AFFILIATE = " + js(affiliate) + ";",
        "window.LAPTOPS = " + js(laptops) + ";",
    ))

    logos = {logo.name: data_uri(logo) for logo in sorted((VIEWER / "logos").iterdir())}
    PUBLIC.mkdir(parents=True, exist_ok=True)
    # index.html is the current chooser; classic.html is the previous UI, kept reachable
    for page in ("index.html", "classic.html"):
        html = (VIEWER / page).read_text()
        # inline the store logos (paths live in the STORE_LOGO map)
        for name, uri in logos.items():
            html = html.replace(f'"logos/{name}"', f'"{uri}"')
        tag = '<script src="laptops.js"></script>'
        if tag not in html:
            raise SystemExit(f"laptops.js script tag not found in viewer/{page}")
        html = html.replace(tag, "<script>\n" + data_block + "\n</script>")
        (PUBLIC / page).write_text(html)
    (PUBLIC / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (OUT / "wrangler.jsonc").write_text("""{
  // Assets-only Worker: no script, no bindings — Cloudflare serves the files
  // in public/ directly (index.html at /) and compresses them at the edge.
  "name": "laptop-chooser",
  "compatibility_date": "2026-09-26",
  "assets": { "directory": "./public" }
}
""")
    (OUT / "README.md").write_text("""# Laptop chooser — Cloudflare Worker (single file)

`public/index.html` is the whole app: data, logos and code in one file, built
by `../build_worker.py` from the latest scrape snapshot.

    npx wrangler deploy      # publishes the assets-only Worker
    npx wrangler dev         # local preview at http://localhost:8787

Rebuild after a data refresh: `python3 build_worker.py` (repo root), then deploy.

Notes
- Shortlist changes persist in the browser (`localStorage`); there is no server
  API, so `/api/shortlist` is simply absent — same behaviour as opening the
  file directly. The seeded shortlist ships inside the HTML.
- `robots.txt` disallows everything; delete the file and redeploy if you want
  the page indexed.
- Prices/specs are a point-in-time snapshot: `LAPTOPS_META.generated_at` is the
  build time shown in the header.
""")

    gz = lambda b: (lambda z: f"{len(z)/1e6:.2f} MB")(__import__("gzip").compress(b, 9))
    out = (PUBLIC / "index.html").read_bytes()
    print(f"payload in : {raw_before/1e6:.2f} MB (laptops.js)")
    print(f"data block : {len(data_block)/1e6:.2f} MB")
    print(f"index.html : {len(out)/1e6:.2f} MB raw · {gz(out)} gzip")
    print(f"wrote {PUBLIC/'index.html'}")


if __name__ == "__main__":
    main()
