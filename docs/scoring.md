# Scoring & data methodology

How the pipeline turns six stores' listings into comparable `perf_score` /
`value_score` numbers. For the *why* behind the component weights, see
[`scoring_rationale.md`](scoring_rationale.md). The viewer also documents this
in-app under *Field Notes → How Scores are Measured*.

## Benchmarks, not heuristics

`cpu_score` / `gpu_score` are measured numbers (see `data/benchmarks/README.md`
for sources and refresh — that folder ships with the repo):

| Component | Basis |
|---|---|
| CPU (65% of silicon) | 70% single-core / 30% multi-core. Cinebench R23 single+multi (Notebookcheck aggregate, ≥3 runs) where available, else PassMark Single Thread + CPU Mark — log-scaled onto one 0–100 scale (r ≈ 0.98 over the shared-chip overlap) |
| GPU (15% of silicon) | PassMark G3D Mark (`Laptop GPU` variant for dedicated cards); iGPUs resolve through the host CPU family; Apple Silicon scales by architecture + core count |
| RAM / storage | **Usable** RAM = installed − OS footprint (~4 GB on Win/macOS) − 1 GB iGPU carve-out, floored at 0.5 GB, on a diminishing uncapped curve (`7.0 · log2(usable/3.5)`: 16 GB ≈ +11.6, 32 GB ≈ +20.6, 64 GB ≈ +28.5) + **−2 choke penalty below 6 GB usable** (every 8 GB config; 12 GB is clear) + 3 pt surcharge for permanently soldered ≤8 GB. Unknown OS reserves like Windows (4 GB) |
| Silicon extras (on `perf_score`) | RAM upgrade headroom 0–1, RAM generation (+0.75 DDR5/LPDDR5(X), −0.5 DDR4), dGPU VRAM (+0.75 ≥12 GB, −0.5 ≤6 GB) |
| Quality index (`quality_score`, 0–100) | Display `(display_score/10)·35` + build `clamp((build_score−4.5)/5)·30` + battery `clamp((Wh−35)/65)·20` + storage tier 5–15, +1.5 touch, +1.0 for 16″+, −2 for a dGPU on <65 Wh. The **only** place display, build, battery and storage are scored |
| CPU efficiency | Perf/watt tier on `overall_score` only (never `perf_score`): +2.0 ARM, +1.0 low-power x86 (N/V/U, ~6–17 W), −1.5 hungry HX/HK/HQ/Xeon/AI MAX, else neutral — the 28–45 W mainstream (Intel H/P, AMD HS, Ryzen AI 300/400) scores 0 |
| Portability | Size-normalized weight on `overall_score` only: ±1.5 max (1 pt per 0.5 kg) vs the screen-size class expectation — 13″ class (≤13.9″) 1.2 kg, 14″ 1.4, 15″ (≤15.8″, so 15.6″ is a 15) 1.6, 16″ 1.7, 17″ 2.4, 18″ 3.0, +0.5 kg dGPU allowance; unknown weight/size neutral |
| Battery | Quality index: 0–20 pts over 35–100 Wh. Published / spec-sheet / matched capacities score as-is; estimates score blended toward the screen-size class median (see below); unknown scores the class median — neutral, never a penalty |
| Gaming | `game_score` = 60% GPU + 25% single-weighted CPU + 15% gaming RAM curve; `game_value` = FPS/riyal through the same affine formula as value |

Scores are `100 · log(mark) / log(range)` so the 0–100 scale stays stable.

## Value score

`value_score = (overall_score ÷ (price + 1500)) × 2000`, where
`overall_score = 0.60 × perf_score + 0.40 × quality_score + CPU efficiency`.
The 1,500 SAR fixed-ownership floor kills the divide-by-near-zero hyperbola so
obsolete cheap machines can't top the rankings. 20–28 ≈ fair, 28–35 ≈ great
deal, 35+ ≈ clearance anomaly.

## Typical-price model (`scraper/sanity.py`)

For every listing, the 25 nearest peers in (perf, storage, RAM, build) space set
a **typical price** (their median):

- below 35% of it → `price_flag = "too cheap"` (⚠ unusually cheap badge + hide filter)
- above 2.2× of it → `price_flag = "above market"`
- value ≥ 1.5× the peer median value → 🔥 Deal badge (in-stock, clean listings only)

Median, not mean: one mis-scraped or flagship outlier can't drag the expectation.

## Dedupe (`scraper/dedupe.py`)

Same configuration (model line + CPU + RAM + storage + screen size; identical
part number overrides title noise) across stores folds into one card with per-store
offers, cheapest in-stock price first. Different sellers or OS editions never fold.

## Battery provenance

Cards show a battery row when a real number exists. Gaps are filled in layers —
identical part number / agreeing chassis carry-over, a hand-curated spec-sheet
table keyed by part/chassis/family, the same configuration + product line at
another store (all donors must agree within 0.5 Wh), then estimates (`~70 Wh`)
from the mode of similar listings. Every value keeps `battery_source`
provenance (`listed` / `model-match` / `chassis-match` / `spec-model` /
`spec-chassis` / `spec-family` / `config-match` / `estimated`).

Estimates still score, so a store that omits the spec doesn't sink its
listings — but the scored capacity (`battery_score_wh`) is
`class_median + w · (estimate − class_median)`, with `w` set by how well each
match level predicted held-out published capacities (leave-one-out over 1,128,
2026-10-11):

| Match level | Held-out error (est. vs class median) | w |
|---|---|---|
| brand + 2 series words + size | 2.6 vs 7.8 Wh | 0.8 |
| brand + series word + size | 5.1 vs 10.0 Wh | 0.75 |
| brand + 2 series words, any size | 7.5 vs 9.4 Wh | 0.6 |
| brand + series word, any size | 10.1 vs 9.7 Wh | 0.5 |
| class + brand + size | 7.3 vs 8.4 Wh | 0.4 |
| brand + size | 14.2 vs 13.6 Wh | 0.3 |

Nothing matched (or mAh with no voltage) → the class+size median itself.

## Display & build ratings (`scraper/ratings.py`)

Heuristic 1–10 scores: panel tech + resolution/density + refresh +
brightness/gamut for display; product-line materials adjusted by material words
for build. Sortable and filterable in the viewer.
