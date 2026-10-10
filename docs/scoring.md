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
| RAM / storage | **Usable** RAM = installed − OS footprint (~4 GB on Win/macOS) − 1 GB iGPU carve-out, on a diminishing curve (`5.0 · log2(usable/3.5)`, up to 18 pts) + **−2 choke penalty at ≤8 GB usable** + 3 pt surcharge for permanently soldered ≤8 GB; storage points up to 2 TB |
| Quality pass | Build rating 0–5, display curve around the market-typical 6/10 panel (below 6 costs 1.5/pt, above earns 0.8/pt), RAM headroom/generation, VRAM, and a dGPU idle-draw charge by battery capacity — so `perf_score`/`value_score` reflect the whole machine |
| CPU efficiency | Perf/watt tier on `overall_score` only (never `perf_score`): +2.0 ARM, +1.0 efficient x86, −1.5 hungry HX/HK/HQ/Xeon, else neutral |
| Portability | Size-normalized weight on `overall_score` only: ±1.5 max vs the screen-size class expectation (+0.5 kg dGPU allowance); unknown weight/size neutral |
| Battery | **Published** Wh only → 0–4 pts over 35–100 Wh. Unknown stays neutral (no endurance assumptions) |
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
table keyed by part/chassis/family, then guarded estimates (`~70 Wh`) that earn
**no** points and can't move rankings. Every value keeps `battery_source`
provenance (`listed` / `model-match` / `chassis-match` / `spec-model` /
`spec-chassis` / `spec-family` / `estimated`).

## Display & build ratings (`scraper/ratings.py`)

Heuristic 1–10 scores: panel tech + resolution/density + refresh +
brightness/gamut for display; product-line materials adjusted by material words
for build. Sortable and filterable in the viewer.
