# Laptop Chooser

Compare every laptop across six stores in one normalized catalog: benchmark-calibrated performance/value scores, a 25-peer
typical-price model, deal alerts, and Pareto-frontier sweet-spot detection.

![Laptop Chooser catalog with value-map chart](docs/screenshot.png)

**Live demo:** https://laptop-chooser.holistic.workers.dev

## Features

- **Start from the use case** — pick Everyday, Study, Gaming, Creative & dev or Travel plus a budget; the score and the hard requirements follow
- **One verdict per laptop** — Great deal / Sweet spot / Good price / Fair / Pricey, from a 25-peer typical-price model and Pareto-knee detection (see [`docs/scoring.md`](docs/scoring.md))
- **Dense, sortable list** with benchmark-calibrated scores, or a **price-vs-score map** with the frontier line and drag-to-zoom price range
- **Detail drawer** — every store's offer, price history, "cheaper with a similar score" alternatives, full spec sheet
- **Price watch** — only meaningful drops (8%+ into good-price territory) and new deals; the badge counts what's new since your last visit
- **Saved list and compare** (up to 3, best value per row highlighted), light/dark themes, mobile layout with bottom nav
- **Cross-store dedupe** — identical configs fold into one entry quoting the cheapest in-stock offer
- The previous UI is still available at `classic.html`

## Tech stack

Python scrapers (requests, SQLite/JSONL) · single-file HTML/JS viewer (hand-rolled SVG, no framework, no build step; `viewer/index.html`, previous UI in `viewer/classic.html`) · Cloudflare Workers static hosting.

## Run it

```bash
pip install requests

# Fastest: open the catalog (data snapshot ships in viewer/laptops.js)
xdg-open viewer/index.html

# Or serve locally with auto-refresh when the snapshot is older than 24h
python3 app.py

# Refresh the snapshot (re-fetches only stale pages; daily via cron)
./update.sh
./update.sh --force   # ignore freshness check

# Ship it: one self-contained HTML file on a Cloudflare Worker
python3 build_worker.py
cd deploy && npx wrangler deploy
```

`data/` (raw scrapes, price history) is generated locally by the scrapers and is
**not versioned** — clone, open the viewer, and it works. See
[`docs/scoring.md`](docs/scoring.md) and [`docs/scoring_rationale.md`](docs/scoring_rationale.md)
for methodology.

## Layout

```
scraper/       # per-store scrapers, normalize.py, benchmarks, ratings, dedupe, sanity, moat
viewer/        # the chooser: index.html + generated laptops.js payload + store logos
app.py         # local launcher: freshness check + background refresh + static server
update.sh      # refresh scrapers + normalize (skipped when snapshot is fresh)
build_worker.py# packages viewer/ into deploy/public/index.html (single file)
deploy/        # generated Worker output (git-ignored; rebuild with build_worker.py)
reports/       # generated pareto / scrape-progress snapshots
docs/          # screenshot, scoring methodology, scoring rationale
data/          # local-only generated datasets (git-ignored, never served)
dev/           # local-only review/debug pages (git-ignored, never served)
cache/ logs/   # fetcher cache, cron output (git-ignored)
```

## Data & privacy boundary

Only `viewer/` is ever served (sanitized payload — `scraper/moat.py` strips
derivation such as spec-sheet source URLs and teardown citations, and summarizes
price history to a sparkline). Raw per-store scrapes, the full day-by-day price
series, and debug review pages stay on the operator's machine and are excluded
from version control. Please respect the stores' terms of service when scraping.
