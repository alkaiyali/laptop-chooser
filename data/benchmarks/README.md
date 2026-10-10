# CPU/GPU benchmark snapshots (PassMark + Notebookcheck)

`normalize.py` scores every laptop with **measured** numbers instead of a
heuristic. The CPU score is **70% single-core / 30% multi-core**
(`benchmarks.CPU_SINGLE_WEIGHT`): single-core is what makes a machine feel
fast. Per chip the best available source wins:

1. **Notebookcheck Cinebench R23 single + multi** (`nbc_cpu.json`, review
   aggregate, `n >= 3` laptops) — controlled reviews
2. **PassMark Single Thread Mark + CPU Mark** (`cpu_passmark.json`) —
   universal fallback, updated daily

R23 medians convert to equivalent PassMark marks through empirical log-log
fits over the snapshot overlap (multi r ≈ 0.98, single r ≈ 0.98), so both
sources share one 0–100 log scale. Per-record provenance
(`cpu.benchmark`: `source` + `mapping` + `n`/`n_single` for NBC rows) shows
which source scored every laptop — hover the Performance bar in the viewer.

GPU is always PassMark **G3D Mark** (`gpu_passmark.json`), `... Laptop GPU`
variant for dedicated cards (Notebookcheck's GPU list has only 142 cards,
no Apple M2+, and 3DMark-only columns — strictly less useful here).

* CPU → PassMark **CPU Mark** (multi) + **Single Thread Mark**
  (`cpu_passmark.json`, `single` field)
* GPU → PassMark **G3D Mark**, `... Laptop GPU` variant when it exists
  (`gpu_passmark.json`)

Both files are checked in. `normalize.py` reads them offline and never
touches the network.

## Refresh (manual, quarterly is plenty)

```bash
python3 scraper/benchmarks.py --fetch            # re-fetch pages older than 48h
python3 scraper/benchmarks.py --fetch --max-age 0 # force re-fetch everything
python3 scraper/benchmarks.py --check            # coverage of our CPUs/GPUs
python3 scraper/notebookcheck.py --fetch         # refresh the NBC review snapshot
python3 scraper/notebookcheck.py --check         # R23 coverage (n>=3) vs fallback
python3 scraper/normalize.py                     # rebuild scores + viewer
```

Why no Geekbench: browser.geekbench.com answers 403 to any non-browser
fetch (bot protection) on every endpoint tried, so it cannot be snapshotted
reliably. No real loss — Notebookcheck's table already contains Geekbench
6.7 medians per chip, the better (controlled-run) version of that data.

Sources baked into the snapshots (see each file's `sources` + `fetched_at`):

* CPU: `cpubenchmark.net/laptop.html` (newest naming) + `/multithread`
  pages 1–4 (full database incl. Raptor Lake, N-series, old Athlons) +
  `/singlethread.html` (Single Thread Mark)
* GPU: `videocardbenchmark.net/{high_end,mid_range,midlow_range,low_end}_gpus.html`

## Known approximations

Per-record provenance lives in `cpu.benchmark` / `gpu.benchmark`
(`mapping` field, also in the CSV as `cpu_benchmark` / `gpu_benchmark`):

| mapping | meaning |
|---|---|
| `exact` / `direct-laptop` / `direct` | exact PassMark row (the common case) |
| `token` / `suffix-repair` / `u-v-swap` / `tier-relaxed` | merchant typo class, e.g. `Ultra 9 258V` → `Ultra 7 258V`, `Core 5 322` → `Ultra 5 322`, `Core 7 150` → `150U` |
| `named-family` | bare Apple/Snapdragon name → base SKU (`M5` → `M5 10 Core`, `X Plus` → `X1P42100`, `X2 Elite` → `X2E80100`) |
| `cpu-family` | integrated GPU resolved through the host CPU (e.g. Lunar Lake-V → Arc 140V/130V, Panther Lake-H/X → Arc B390, Arrow Lake-H → Arc 140T, Meteor Lake-H → Arc 140V proxy, Raptor Lake-H refresh → Iris Xe on 270H/250H and UHD below, Strix `AI 9/7/5` → 890M/860M/840M, Raptor Lake-U i7 → Iris Xe) |
| `vega-proxy` | Barcelo/Cezanne/Lucienne Vega iGPU → 660M (no Vega G3D rows exist); Vega 6 parts (5300U/5400U) → 610M |
| `family-median` | bare family name (`Ryzen 7`, `Core i7` ...) → median of that family's mobile entries |
| `apple-cpu-proxy` | Apple GPUs have no G3D rows, so the GPU score equals the M-chip's CPU score |
| `override` | hand fix from `overrides.json` (`i7-1335U` → `i7-1355U`, `N-series` → `N150`, `Core 3 355` → `Core 3 N355` — merchant-dropped N) |

Scores are `100 * log(mark) / log(range)` (CPU multi 1k–65k, CPU single
0.7k–7k, GPU 0.5k–42k) so the old 0–100 `perf_score` / `value_score` /
sanity-model scale stays comparable.
